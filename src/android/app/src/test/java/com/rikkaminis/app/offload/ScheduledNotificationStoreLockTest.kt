package com.rikkaminis.app.offload

import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.lang.reflect.Modifier
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * [da1028-A] Mutual exclusion of the scheduled-notification ledger, driven
 * through the REAL read-modify-write loop with an in-memory prefs double.
 *
 * Why this can run off-device: unit tests execute against android.jar's stub
 * `Context` (`android.testOptions.unitTests.isReturnDefaultValues = true`), so
 * `context.getSharedPreferences` returns null and any test that needs the
 * ledger would NPE at construction. The store therefore takes its prefs handle
 * as a constructor parameter — production callers keep passing a `Context` —
 * and this test supplies [FakePrefs].
 *
 * The defect under test is NOT "SharedPreferences is slow": the ledger is a
 * whole-blob read-modify-write, and two writers that read the same blob and
 * then write it back lose one of the two entries. Before the fix the store's
 * `lock` was an instance field while TWO stores exist in the process
 * (NotificationOffloadHandler:55 builds one, ScheduledNotificationReceiver:113
 * builds another), so the two writers never excluded each other and the
 * `remove()` a fired notification performs could be rolled back by a
 * concurrent `add()` — a row that resurfaces with its AlarmManager alarm
 * already gone.
 *
 * Expectations below are literal (never derived from the code under test), and
 * the interleavings are forced by [FakePrefs.armRendezvous] rather than left to
 * chance: with the per-instance lock the loss is deterministic, so a mutation
 * that puts the lock back on the instance turns these tests red every run
 * instead of "sometimes".
 */
class ScheduledNotificationStoreLockTest {

    private fun entry(id: String): JSONObject = JSONObject()
        .put("id", id)
        // Far future: `add`'s > MAX_ENTRIES sweep must never drop these.
        .put("trigger_at_ms", System.currentTimeMillis() + 3_600_000L)

    private fun ids(ledger: JSONArray): List<String> =
        (0 until ledger.length()).map { ledger.getJSONObject(it).optString("id") }

    @Test
    fun lockIsProcessWideNotPerInstance() {
        val field = ScheduledNotificationStore::class.java.getDeclaredField("lock")
        assertTrue(
            "the ledger lock must have static storage (companion object): the handler " +
                "and the receiver each build their own store, so an instance-level lock " +
                "gives every writer its own monitor and they never exclude each other",
            Modifier.isStatic(field.modifiers),
        )
    }

    @Test
    fun concurrentWritersDoNotLoseEntries() {
        val prefs = FakePrefs()
        val handlerSide = ScheduledNotificationStore(prefs)
        val receiverSide = ScheduledNotificationStore(prefs)
        val rounds = 40
        var lost = 0

        for (round in 0 until rounds) {
            prefs.armRendezvous()
            val go = CountDownLatch(1)
            val t1 = Thread { go.await(); handlerSide.add(entry("a$round")) }
            val t2 = Thread { go.await(); receiverSide.add(entry("b$round")) }
            t1.start(); t2.start(); go.countDown()
            t1.join(5_000); t2.join(5_000)
            val survivors = ids(handlerSide.loadAll())
            if ("a$round" !in survivors || "b$round" !in survivors) lost++
        }

        assertEquals(
            "an add() that returned must still be readable (rounds losing an entry: $lost/$rounds)",
            0, lost,
        )
        assertEquals(2 * rounds, handlerSide.loadAll().length())
    }

    @Test
    fun removeIsNotRolledBackByConcurrentAdd() {
        val prefs = FakePrefs()
        val receiverSide = ScheduledNotificationStore(prefs)
        val handlerSide = ScheduledNotificationStore(prefs)
        receiverSide.add(entry("victim"))

        prefs.armRendezvous()
        val go = CountDownLatch(1)
        // The fired notification drops its own row while a schedule lands.
        val t1 = Thread { go.await(); receiverSide.remove("victim") }
        val t2 = Thread { go.await(); handlerSide.add(entry("newcomer")) }
        t1.start(); t2.start(); go.countDown()
        t1.join(5_000); t2.join(5_000)

        val survivors = ids(handlerSide.loadAll())
        assertTrue(
            "the fired row must stay removed (ledger: $survivors)",
            "victim" !in survivors,
        )
        assertTrue(
            "the concurrent add() must survive (ledger: $survivors)",
            "newcomer" in survivors,
        )
    }

    @Test
    fun readersDoNotSlipPastAnInFlightWrite() {
        val prefs = FakePrefs()
        val store = ScheduledNotificationStore(prefs)
        val writeStarted = CountDownLatch(1)
        val releaseWrite = CountDownLatch(1)
        prefs.parkApply(releaseWrite, writeStarted)

        val writer = Thread { store.add(entry("late")) }
        writer.start()
        assertTrue("writer never reached its write", writeStarted.await(5, TimeUnit.SECONDS))

        // The writer is inside its critical section with nothing published yet.
        // Neither reader may observe the pre-write blob: `android-notification
        // pending` (loadAll) and `cancel --id` (get) would report an alarm as
        // absent while it is being registered. Both readers start while the
        // write is parked, and each has its own assertion so a mutation that
        // unguardes only one of them points at the right one.
        var viaGet: JSONObject? = null
        var viaLoadAll: List<String>? = null
        val readerGet = Thread { viaGet = store.get("late") }
        val readerLoadAll = Thread { viaLoadAll = ids(store.loadAll()) }
        readerGet.start()
        readerLoadAll.start()
        Thread.sleep(150)
        releaseWrite.countDown()
        writer.join(5_000); readerGet.join(5_000); readerLoadAll.join(5_000)

        assertNotNull("get() observed the ledger before the in-flight add() published", viaGet)
        assertEquals(
            "loadAll() observed the ledger before the in-flight add() published",
            listOf("late"), viaLoadAll,
        )
    }

    @Test
    fun ledgerRoundTripStillWorks() {
        val store = ScheduledNotificationStore(FakePrefs())
        store.add(entry("one"))
        store.add(entry("two"))
        assertNotNull(store.get("one"))
        assertEquals(2, store.loadAll().length())
        assertTrue(store.remove("one"))
        assertNull(store.get("one"))
        assertEquals(1, store.loadAll().length())
        store.sweepExpired()
        assertEquals(1, store.loadAll().length())
        store.clear()
        assertEquals(0, store.loadAll().length())
    }

    /**
     * In-memory `SharedPreferences` double.
     *
     * Two hooks make the interleavings deterministic instead of flaky:
     *  - [armRendezvous] makes the FIRST `getString` of a round block until a
     *    second reader arrives (bounded, so it still completes when the second
     *    writer is queued behind the process-wide lock). Both writers therefore
     *    hold the same snapshot before either write can land — exactly the
     *    lost-update window.
     *  - [parkApply] holds `apply()` (and with it the store's critical section)
     *    open so a reader can be shown to be waiting on it.
     */
    private class FakePrefs : SharedPreferences {
        private val map = ConcurrentHashMap<String, String>()

        @Volatile
        private var roundLatch: CountDownLatch? = null
        private val arrivals = AtomicInteger(0)

        @Volatile
        private var parkLatch: CountDownLatch? = null

        @Volatile
        private var parkedLatch: CountDownLatch? = null

        fun armRendezvous() {
            arrivals.set(0)
            roundLatch = CountDownLatch(1)
        }

        fun parkApply(release: CountDownLatch, parked: CountDownLatch) {
            parkLatch = release
            parkedLatch = parked
        }

        /** First caller waits (bounded) for a second one; see the class KDoc. */
        private fun rendezvous() {
            val latch = roundLatch ?: return
            if (arrivals.incrementAndGet() == 1) {
                latch.await(30, TimeUnit.MILLISECONDS)
            } else {
                latch.countDown()
            }
        }

        private fun parkIfArmed() {
            val latch = parkLatch ?: return
            parkedLatch?.countDown()
            latch.await(5, TimeUnit.SECONDS)
        }

        override fun getAll(): MutableMap<String, *> = ConcurrentHashMap(map)

        override fun getString(key: String?, defValue: String?): String? {
            val value = map[key]
            rendezvous()
            return value
        }

        override fun getStringSet(key: String?, defValues: MutableSet<String>?): MutableSet<String>? =
            defValues

        override fun getInt(key: String?, defValue: Int): Int = defValue

        override fun getLong(key: String?, defValue: Long): Long = defValue

        override fun getFloat(key: String?, defValue: Float): Float = defValue

        override fun getBoolean(key: String?, defValue: Boolean): Boolean = defValue

        override fun contains(key: String?): Boolean = map.containsKey(key)

        override fun edit(): SharedPreferences.Editor = FakeEditor()

        override fun registerOnSharedPreferenceChangeListener(
            listener: SharedPreferences.OnSharedPreferenceChangeListener?,
        ) = Unit

        override fun unregisterOnSharedPreferenceChangeListener(
            listener: SharedPreferences.OnSharedPreferenceChangeListener?,
        ) = Unit

        private inner class FakeEditor : SharedPreferences.Editor {
            private val pending = HashMap<String, String?>()
            private val removals = HashSet<String>()
            private var cleared = false

            override fun putString(key: String?, value: String?): SharedPreferences.Editor {
                pending[key.orEmpty()] = value
                return this
            }

            override fun putStringSet(
                key: String?,
                values: MutableSet<String>?,
            ): SharedPreferences.Editor = this

            override fun putInt(key: String?, value: Int): SharedPreferences.Editor = this

            override fun putLong(key: String?, value: Long): SharedPreferences.Editor = this

            override fun putFloat(key: String?, value: Float): SharedPreferences.Editor = this

            override fun putBoolean(key: String?, value: Boolean): SharedPreferences.Editor = this

            override fun remove(key: String?): SharedPreferences.Editor {
                key?.let { removals.add(it) }
                return this
            }

            override fun clear(): SharedPreferences.Editor {
                cleared = true
                return this
            }

            override fun commit(): Boolean {
                publish()
                return true
            }

            override fun apply() {
                parkIfArmed()
                publish()
            }

            private fun publish() {
                if (cleared) map.clear()
                for (key in removals) map.remove(key)
                for ((key, value) in pending) {
                    if (value == null) map.remove(key) else map[key] = value
                }
            }
        }
    }
}

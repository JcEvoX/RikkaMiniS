package com.rikkaminis.app.browser

import java.lang.reflect.Method
import java.util.Collections
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1022 ISSUE-08] BrowserHistoryStore.save() must snapshot the
 * live list under its own monitor.
 *
 * Defect shape being pinned: record()/clear() call save() right AFTER
 * releasing synchronized(entries), and save() iterated `entries` unlocked.
 * A concurrent record() mid-iteration throws ConcurrentModificationException
 * — swallowed by save()'s catch, so the persistence silently no-ops (and a
 * torn view can be written when the iteration does complete).
 *
 * The store is Android-bound (Context.filesDir) and its constructor is
 * private, so the instance is allocated without a constructor (sun.misc.
 * Unsafe) and the private `entries` field is replaced with a spy list that
 * records, at every iterator() call, whether the calling thread holds the
 * list's monitor. The file write then fails on the null context and is
 * swallowed by save()'s own catch — irrelevant: the discriminator is WHO
 * holds the monitor while the list is walked, not what lands on disk.
 */
class BrowserHistorySnapshotTest {

    /** Delegation spy over a real ArrayList; iteration is the observed op. */
    private class SpyList : AbstractMutableList<BrowserHistoryStore.Entry>() {
        val backing = ArrayList<BrowserHistoryStore.Entry>()
        @Volatile var iteratedUnderLock = false
        @Volatile var iteratedWithoutLock = false

        override val size: Int get() = backing.size
        override fun get(index: Int): BrowserHistoryStore.Entry = backing[index]
        override fun add(index: Int, element: BrowserHistoryStore.Entry) = backing.add(index, element)
        override fun removeAt(index: Int): BrowserHistoryStore.Entry = backing.removeAt(index)
        override fun set(index: Int, element: BrowserHistoryStore.Entry): BrowserHistoryStore.Entry =
            backing.set(index, element)

        override fun iterator(): MutableIterator<BrowserHistoryStore.Entry> {
            // The monitor is the `entries` field object itself — exactly what
            // save()/record() synchronize on.
            if (Thread.holdsLock(this)) iteratedUnderLock = true else iteratedWithoutLock = true
            return backing.iterator()
        }
    }

    // ── plumbing: allocate without constructor, swap in the spy list ──────

    @Suppress("PrivateApi")
    private fun allocateStore(): BrowserHistoryStore {
        val unsafeClass = Class.forName("sun.misc.Unsafe")
        val f = unsafeClass.getDeclaredField("theUnsafe")
        f.isAccessible = true
        val unsafe = f.get(null)
        return unsafeClass.getMethod("allocateInstance", Class::class.java)
            .invoke(unsafe, BrowserHistoryStore::class.java) as BrowserHistoryStore
    }

    private fun installEntries(store: BrowserHistoryStore, spy: SpyList) {
        val f = BrowserHistoryStore::class.java.getDeclaredField("entries")
        f.isAccessible = true
        f.set(store, spy)
    }

    private fun saveMethod(): Method {
        val m = BrowserHistoryStore::class.java.getDeclaredMethod("save")
        m.isAccessible = true
        return m
    }

    private fun entry(url: String) = BrowserHistoryStore.Entry(url = url, title = "t-$url")

    // ── tests ─────────────────────────────────────────────────────────────

    /**
     * Direct save() call: EVERY walk of the list must happen under its
     * monitor. Mutations that flip this red:
     *  - reverting save() to `for (entry in entries)` (unlocked walk),
     *  - replacing the snapshot with an unlocked `entries.toList()`.
     */
    @Test
    fun saveWalksTheListOnlyUnderItsMonitor() {
        val store = allocateStore()
        val spy = SpyList()
        installEntries(store, spy)
        spy.backing.add(entry("https://a.example/1"))
        spy.backing.add(entry("https://b.example/2"))

        saveMethod().invoke(store)

        assertTrue("save() must walk entries inside synchronized(entries)", spy.iteratedUnderLock)
        assertFalse("save() walked the live list OUTSIDE its monitor", spy.iteratedWithoutLock)
    }

    /**
     * The real call shape: record() releases the monitor and THEN calls
     * save() — hammered from several threads. With the snapshot fix the
     * unlocked walk never happens; reverting save() flags it on the very
     * first record(), from any thread.
     */
    @Test
    fun concurrentRecordNeverWalksTheListUnlocked() {
        val store = allocateStore()
        val spy = SpyList()
        installEntries(store, spy)

        val errors = Collections.synchronizedList(mutableListOf<Throwable>())
        val threads = (0 until 4).map { t ->
            Thread {
                try {
                    for (i in 0 until 250) {
                        store.record("https://e$t.example/$i", "title")
                    }
                } catch (e: Throwable) {
                    errors.add(e)
                }
            }.also { it.start() }
        }
        threads.forEach { it.join() }

        assertEquals("record() must not escape an exception to its caller", emptyList<Throwable>(), errors.toList())
        assertEquals(1000, spy.size)
        assertFalse("record() -> save() walked the live list OUTSIDE its monitor", spy.iteratedWithoutLock)
        assertTrue("the snapshot walk should have happened under the monitor", spy.iteratedUnderLock)
    }

    /**
     * The other half of the contract: the lock covers ONLY the shallow copy.
     * A snapshot of a big list must not serialize/hit the disk while the
     * monitor is held (record()/clear() would stall on a slow flash write).
     * Probed by holding the monitor on this thread and asking another thread
     * to snapshot-and-serialize: save() completes only once we release.
     */
    @Test
    fun saveWaitsForTheMonitorButHoldsItBriefly() {
        val store = allocateStore()
        val spy = SpyList()
        installEntries(store, spy)
        for (i in 0 until 50) spy.backing.add(entry("https://c.example/$i"))

        val saveThread = Thread { saveMethod().invoke(store) }
        // Pre-hold the monitor: save() must block on the snapshot, and then
        // finish the (file-write-fails, swallowed) rest without the monitor.
        val blocker = Thread {
            synchronized(spy) {
                saveThread.start()
                Thread.sleep(150)
            }
        }
        blocker.start()
        Thread.sleep(80)
        assertTrue(
            "save() proceeded while another thread still held the entries monitor",
            saveThread.isAlive,
        )
        blocker.join()
        saveThread.join(5_000)
        assertFalse("save() never completed once the monitor was released", saveThread.isAlive)
    }

    // ── store-owned refresh signal (dv15-B3) ───────────────────────────────

    /**
     * Installs a real counter where the constructor would have put one.
     *
     * `_revision` is nullable precisely because instances built here skipped
     * the constructor — the store substitutes a static placeholder on those,
     * which is what [recordOnAConstructorSkippedInstanceDoesNotThrow] pins.
     */
    @Suppress("PrivateApi")
    private fun installRevision(store: BrowserHistoryStore, flow: MutableStateFlow<Int>) {
        val f = BrowserHistoryStore::class.java.getDeclaredField("_revision")
        f.isAccessible = true
        f.set(store, flow)
    }

    /**
     * `BrowserHistorySheet` keys its memoized snapshot on
     * `historyStore.revision` instead of a panel-local counter, so the counter
     * must advance on every mutation that can change `getEntries()`.
     *
     * Expectations are written as literals on purpose: deriving them from the
     * store's own constants would let a mutation move the expectation with it
     * and pass.
     */
    @Test
    fun revisionAdvancesOnRecordAndClear() {
        val store = allocateStore()
        val spy = SpyList()
        installEntries(store, spy)
        val revision = MutableStateFlow(0)
        installRevision(store, revision)

        store.record("https://a.example/1", "t")
        assertEquals(1, revision.value)
        store.record("https://a.example/2", "t")
        assertEquals(2, revision.value)
        store.clear()
        assertEquals(3, revision.value)
    }

    /** A consecutive repeat is not stored, so it must not re-notify observers. */
    @Test
    fun aRepeatedVisitDoesNotAdvanceRevision() {
        val store = allocateStore()
        val spy = SpyList()
        installEntries(store, spy)
        val revision = MutableStateFlow(0)
        installRevision(store, revision)

        store.record("https://a.example/1", "t")
        store.record("https://a.example/1", "t")

        assertEquals(1, revision.value)
        assertEquals(1, spy.backing.size)
    }

    /** Nothing dropped ⇒ nothing to invalidate. */
    @Test
    fun clearingAnAlreadyEmptyStoreDoesNotAdvanceRevision() {
        val store = allocateStore()
        val spy = SpyList()
        installEntries(store, spy)
        val revision = MutableStateFlow(0)
        installRevision(store, revision)

        store.clear()

        assertEquals(0, revision.value)
    }

    /**
     * The constructor-skipped path every test in this class walks: `_revision`
     * is null here, and `record()` must still store the visit instead of
     * NPE-ing on the way through the bump.
     */
    @Test
    fun recordOnAConstructorSkippedInstanceDoesNotThrow() {
        val store = allocateStore()
        val spy = SpyList()
        installEntries(store, spy)

        store.record("https://b.example/1", "t")

        assertEquals(1, spy.backing.size)
    }
}

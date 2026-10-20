package com.rikkaminis.app.offload

import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv15-A3] Same-id ledger semantics for the scheduled-notification store:
 *  - add() is an IDEMPOTENT upsert — scheduling the same id again replaces its
 *    previous row, mirroring AlarmManager's FLAG_UPDATE_CURRENT replace
 *    (NotificationOffloadHandler re-registers the alarm in place);
 *  - remove() clears EVERY row with the id — the duplicates the old
 *    first-match remove left behind were the ghost rows `android-notification
 *    pending` kept listing after the alarm fired or was cancelled.
 *
 * Runs off-device exactly like ScheduledNotificationStoreLockTest: the store
 * takes its prefs handle as a constructor parameter and this test supplies an
 * in-memory double. All expectations are dead literals (never derived from the
 * code under test), so a mutation that changes the semantics turns these red
 * instead of silently re-baselining.
 */
class ScheduledNotificationStoreIdempotencyTest {

    private fun ids(ledger: JSONArray): List<String> =
        (0 until ledger.length()).map { ledger.getJSONObject(it).optString("id") }

    private fun store(seed: String?): ScheduledNotificationStore =
        ScheduledNotificationStore(FakePrefs(seed))

    @Test
    fun addReplacesSameIdInsteadOfStacking() {
        val s = store(
            "[{\"id\":\"alpha\",\"title\":\"old\",\"trigger_at_ms\":4102444800000}," +
                "{\"id\":\"beta\",\"title\":\"keep\",\"trigger_at_ms\":4102444900000}]",
        )
        s.add(
            JSONObject()
                .put("id", "alpha")
                .put("title", "new")
                .put("trigger_at_ms", 4102444809999L),
        )

        val ledger = s.loadAll()
        assertEquals("re-scheduling alpha must replace, not stack (ledger ids: ${ids(ledger)})", 2, ledger.length())
        assertEquals(listOf("beta", "alpha"), ids(ledger))
        val alpha = s.get("alpha")!!
        assertEquals("new", alpha.optString("title"))
        assertEquals(4102444809999L, alpha.optLong("trigger_at_ms"))
        assertEquals(4102444900000L, s.get("beta")!!.optLong("trigger_at_ms"))
    }

    @Test
    fun removeClearsEveryRowWithTheId() {
        val s = store(
            "[{\"id\":\"alpha\",\"trigger_at_ms\":4102444800000}," +
                "{\"id\":\"alpha\",\"trigger_at_ms\":4102444805000}," +
                "{\"id\":\"beta\",\"trigger_at_ms\":4102444900000}]",
        )

        assertTrue("first remove must report a removal", s.remove("alpha"))
        val ledger = s.loadAll()
        assertEquals("every alpha row must be gone (remaining: ${ids(ledger)})", 1, ledger.length())
        assertEquals(listOf("beta"), ids(ledger))
        assertFalse("second remove must report nothing left", s.remove("alpha"))
    }

    @Test
    fun removeUnknownIdChangesNothing() {
        val s = store("[{\"id\":\"alpha\",\"trigger_at_ms\":4102444800000}]")
        assertFalse(s.remove("gamma"))
        assertEquals(1, s.loadAll().length())
        assertEquals(listOf("alpha"), ids(s.loadAll()))
    }

    @Test
    fun legacyDuplicateBlobStillReadsVerbatim() {
        val s = store(
            "[{\"id\":\"alpha\",\"title\":\"first\",\"body\":\"b1\",\"trigger_at_ms\":4102444800000,\"request_code\":111}," +
                "{\"id\":\"alpha\",\"title\":\"second\",\"body\":\"b2\",\"trigger_at_ms\":4102444805000,\"request_code\":111}," +
                "{\"id\":\"beta\",\"title\":\"third\",\"body\":\"b3\",\"trigger_at_ms\":4102444900000,\"request_code\":222}]",
        )

        val ledger = s.loadAll()
        assertEquals("a blob written by the old app version must load unchanged", 3, ledger.length())
        assertEquals("first", ledger.getJSONObject(0).optString("title"))
        assertEquals("second", ledger.getJSONObject(1).optString("title"))
        assertEquals("b2", ledger.getJSONObject(1).optString("body"))
        assertEquals(111, ledger.getJSONObject(1).optInt("request_code"))
        assertEquals("third", ledger.getJSONObject(2).optString("title"))
        assertEquals(222, ledger.getJSONObject(2).optInt("request_code"))
    }

    @Test
    fun removeAllByIdDropsEveryMatchAndReports() {
        val list = JSONArray()
            .put(JSONObject().put("id", "gamma"))
            .put(JSONObject().put("id", "alpha"))
            .put(JSONObject().put("id", "alpha"))

        assertTrue(removeAllById(list, "alpha"))
        assertEquals(1, list.length())
        assertEquals("gamma", list.getJSONObject(0).optString("id"))
        assertFalse("nothing left to remove", removeAllById(list, "alpha"))
        assertFalse("unrelated id never matches", removeAllById(list, "delta"))
        assertEquals(1, list.length())
    }

    /**
     * In-memory prefs double — no rendezvous/park hooks needed here: the
     * idempotency contract is deterministic (no interleavings to force).
     * `getString` always returns the whole blob regardless of key, so tests
     * seed state without hardcoding the store's private prefs key.
     */
    private class FakePrefs(initialBlob: String? = null) : SharedPreferences {
        var blob: String? = initialBlob

        override fun getAll(): MutableMap<String, *> = mutableMapOf<String, String>()
        override fun getString(key: String?, defValue: String?): String? = blob
        override fun getStringSet(key: String?, defValues: MutableSet<String>?): MutableSet<String>? = defValues
        override fun getInt(key: String?, defValue: Int): Int = defValue
        override fun getLong(key: String?, defValue: Long): Long = defValue
        override fun getFloat(key: String?, defValue: Float): Float = defValue
        override fun getBoolean(key: String?, defValue: Boolean): Boolean = defValue
        override fun contains(key: String?): Boolean = blob != null
        override fun edit(): SharedPreferences.Editor = FakeEditor()
        override fun registerOnSharedPreferenceChangeListener(
            listener: SharedPreferences.OnSharedPreferenceChangeListener?,
        ) = Unit

        override fun unregisterOnSharedPreferenceChangeListener(
            listener: SharedPreferences.OnSharedPreferenceChangeListener?,
        ) = Unit

        private inner class FakeEditor : SharedPreferences.Editor {
            override fun putString(key: String?, value: String?): SharedPreferences.Editor {
                blob = value
                return this
            }

            override fun putStringSet(key: String?, values: MutableSet<String>?): SharedPreferences.Editor = this
            override fun putInt(key: String?, value: Int): SharedPreferences.Editor = this
            override fun putLong(key: String?, value: Long): SharedPreferences.Editor = this
            override fun putFloat(key: String?, value: Float): SharedPreferences.Editor = this
            override fun putBoolean(key: String?, value: Boolean): SharedPreferences.Editor = this
            override fun remove(key: String?): SharedPreferences.Editor {
                blob = null
                return this
            }

            override fun clear(): SharedPreferences.Editor = this
            override fun commit(): Boolean = true
            override fun apply() {}
        }
    }
}

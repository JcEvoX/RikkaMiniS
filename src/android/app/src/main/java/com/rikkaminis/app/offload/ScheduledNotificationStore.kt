package com.rikkaminis.app.offload

import android.content.Context
import android.content.SharedPreferences
import com.rikkaminis.app.logging.AppLogger
import org.json.JSONArray
import org.json.JSONObject

/**
 * Lightweight SharedPreferences-backed registry of currently-scheduled
 * `android-notification schedule` notifications.
 *
 * Used so that:
 *   - `android-notification pending` can list what's still queued
 *   - `android-notification cancel --id` can remove a specific entry
 *   - the receiver can drop entries on fire so they don't linger
 *
 * The app already maintains a similar prefs registry for alarms in
 * AlarmOffloadManager; we deliberately keep this separate so the two
 * surfaces don't collide on key names or fire semantics.
 */
internal class ScheduledNotificationStore(
    private val prefs: SharedPreferences,
) {

    /**
     * Production entry point — callers keep writing
     * `ScheduledNotificationStore(context)`.
     *
     * [da1028-A] A `Context` is used for exactly one thing here (the prefs
     * handle), so it is a constructor parameter: that lets the unit test drive
     * the read-modify-write loop below with an in-memory double. Unit tests run
     * against android.jar's stub `Context`
     * (`android.testOptions.unitTests.isReturnDefaultValues = true`), whose
     * `getSharedPreferences` returns null — untestable off-device otherwise.
     */
    internal constructor(context: Context) : this(
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE),
    )

    // [audit-0919 F-283] Every write below is a whole-blob read-modify-write
    // (`loadAll()` → mutate → `putString(entire JSON)`). Without mutual
    // exclusion two concurrent writers each read the same snapshot and the
    // second write silently discards the first writer's entry. The handler
    // that owns this store runs on the NativeOffloadServer worker pool
    // (NativeOffload.kt:242, sized by ConcurrencyPrefs.maxConcurrentSessions(),
    // default 2), so two overlapping `android-notification schedule` calls are
    // enough to trigger it — reproduced at 20/26/29/32/37 surviving entries out
    // of 100 (63-80% loss) in wave2-d1/exp_d1/ExpA.java.
    //
    // The consequence is worse than a lost bookkeeping row: the AlarmManager
    // alarm is registered BEFORE `add()` (NotificationOffloadHandler
    // `scheduleDeferred`: `am.setExactAndAllowWhileIdle(...)` then
    // `store.add(entry)`), so a dropped entry leaves an alarm that fires but
    // cannot be listed by `pending` nor cancelled by `cancel --id` (get()
    // returns null) — the user can only wait for it.
    //
    // [da1028-A] The lock therefore lives in the COMPANION, not on the
    // instance: the handler (NotificationOffloadHandler:55) and the receiver
    // (ScheduledNotificationReceiver:113) each construct their own store, both
    // in the same process. An instance-level lock handed each of them a private
    // mutex, so the two writers above never excluded each other and the
    // `remove()` that a fired notification performs to stop its row lingering
    // could be rolled back by a concurrent `add()`: the fired alarm's row comes
    // back in `pending` with its AlarmManager alarm already gone (or, when the
    // add is the one that loses, the same orphan state this class exists to
    // prevent). One process-wide lock is the only granularity that matches the
    // single SharedPreferences file both instances write to.

    /**
     * [audit-0919 F-283] Soft ceiling. Exceeding it triggers a sweep of
     * already-expired entries plus a loud log — it deliberately does NOT drop
     * the oldest entry: dropping one would recreate exactly the "alarm is
     * registered but the ledger has no row" state this class is being fixed
     * for. 1000 rows ≈ 15 KB of JSON, so unbounded growth is bounded by the
     * sweep, not by data loss.
     */
    fun add(entry: JSONObject) {
        synchronized(lock) {
            val list = loadAll()
            // [dv15-A3] Idempotent upsert. scheduleDeferred re-registers the same
            // id with FLAG_UPDATE_CURRENT (NotificationOffloadHandler replaces the
            // AlarmManager alarm in place), so scheduling the same id again is a
            // RE-ARM, not a second alarm. The ledger must mirror that: drop the
            // previous row(s) for this id before appending, or `pending` grows one
            // ghost row per re-schedule and those rows also survive fire/cancel
            // (remove() used to clear only the first).
            removeAllById(list, entry.optString("id"))
            list.put(entry)
            val capped = if (list.length() > MAX_ENTRIES) sweepExpiredIn(list) else list
            if (capped.length() > MAX_ENTRIES) {
                AppLogger.error(
                    TAG,
                    "scheduled-notification ledger has ${capped.length()} entries (> $MAX_ENTRIES) — " +
                        "not dropping any (an orphan alarm cannot be cancelled); " +
                        "run `android-notification pending` to sweep",
                )
            }
            prefs.edit().putString(KEY, capped.toString()).apply()
        }
    }

    /**
     * [dv15-A3] Removes EVERY row with this id, not just the first. Rows sharing
     * an id are ghosts of ONE logical alarm: the AlarmManager side keeps only the
     * latest pending intent for that id (same requestCode, FLAG_UPDATE_CURRENT),
     * so deleting the first match and returning left the duplicates behind —
     * both callers (the receiver's fire path and cancel --id) want the whole id
     * gone. Returns whether anything was removed; unchanged for the normal
     * single-row case.
     */
    fun remove(id: String): Boolean {
        synchronized(lock) {
            val list = loadAll()
            if (removeAllById(list, id)) {
                prefs.edit().putString(KEY, list.toString()).apply()
                return true
            }
            return false
        }
    }

    fun clear() {
        synchronized(lock) {
            prefs.edit().remove(KEY).apply()
        }
    }

    /**
     * [da1028-A] Both readers take [lock] too. Readers used to run unsynchronized: a
     * `get`/`loadAll` that started while a writer was inside its
     * read-modify-write returned the pre-write blob — `android-notification
     * pending` and `cancel --id` (which looks the row up with [get]) then
     * reported an alarm as absent while it was in fact registered, or the
     * reverse. Same lock as the writers, so a reader can no longer observe a
     * half-applied ledger.
     */
    fun get(id: String): JSONObject? = synchronized(lock) {
        val list = loadAll()
        for (i in 0 until list.length()) {
            val o = list.getJSONObject(i)
            if (o.optString("id") == id) return@synchronized o
        }
        null
    }

    fun loadAll(): JSONArray = synchronized(lock) {
        val raw = prefs.getString(KEY, null) ?: return@synchronized JSONArray()
        try { JSONArray(raw) } catch (_: Exception) { JSONArray() }
    }

    /**
     * Drop entries whose triggerAtMs has already passed. Receiver also
     * cleans up on fire, but if it was killed by the OS between schedule
     * and trigger the prefs entry would otherwise stick around.
     */
    fun sweepExpired() {
        synchronized(lock) {
            val list = loadAll()
            val kept = sweepExpiredIn(list)
            if (kept.length() != list.length()) {
                prefs.edit().putString(KEY, kept.toString()).apply()
            }
        }
    }

    /**
     * [audit-0919 F-283] Pure filter extracted so both [add]'s cap path and
     * [sweepExpired] share one definition of "expired". Caller holds [lock].
     */
    private fun sweepExpiredIn(list: JSONArray): JSONArray {
        val now = System.currentTimeMillis()
        val kept = JSONArray()
        for (i in 0 until list.length()) {
            val o = list.getJSONObject(i)
            if (o.optLong("trigger_at_ms", Long.MAX_VALUE) > now) {
                kept.put(o)
            }
        }
        return kept
    }

    companion object {
        /**
         * [da1028-A] Process-wide mutex — see the long note on the writers
         * above. `companion object` gives it static storage, so every store
         * instance (handler, receiver, any future construction site) shares the
         * one monitor; an instance-level `private val lock = Any()` does not.
         * `synchronized` is re-entrant, so the writers may keep calling the
         * now-guarded [loadAll] from inside their own critical section.
         */
        private val lock = Any()

        private const val TAG = "ScheduledNotifStore"
        private const val PREFS = "minis_scheduled_notifications"
        private const val KEY = "scheduled"
        private const val MAX_ENTRIES = 1_000
    }
}

/**
 * [dv15-A3] Single source for "locate by id and drop EVERY match" on the
 * ledger array. Both [ScheduledNotificationStore.add] (idempotent upsert — a
 * re-schedule replaces its previous row, mirroring AlarmManager's
 * FLAG_UPDATE_CURRENT replace) and [ScheduledNotificationStore.remove] (bulk
 * clear for the fire and cancel paths) must go through this one helper;
 * scripts/scan/router_lock_check.py pins that contract by name.
 *
 * Pure org.json manipulation, no Android types — JVM unit tests exercise it
 * directly (ScheduledNotificationStoreIdempotencyTest) without a prefs double.
 *
 * Iterates BACKWARD so removing the current element never shifts the indices
 * still to visit, and keeps scanning after a match: an early return after the
 * first hit is exactly the ghost-row defect this exists to prevent.
 */
internal fun removeAllById(list: JSONArray, id: String): Boolean {
    var removed = false
    var i = list.length() - 1
    while (i >= 0) {
        if (list.getJSONObject(i).optString("id") == id) {
            list.remove(i)
            removed = true
        }
        i--
    }
    return removed
}

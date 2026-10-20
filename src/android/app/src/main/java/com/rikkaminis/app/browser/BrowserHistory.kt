package com.rikkaminis.app.browser

import android.content.Context
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.URI
import java.util.UUID

/**
 * Tracks browser history with 7-day retention, mirroring iOS BrowserHistoryStore.
 */
class BrowserHistoryStore private constructor(private val context: Context) {

    companion object {
        private const val TAG = "BrowserHistory"
        private const val MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000L // 7 days
        private const val FILENAME = "browser_history.json"

        /**
         * [dv15-B3] Stand-in for `_revision` on instances whose constructor
         * never ran: `BrowserHistorySnapshotTest` allocates the store with
         * `Unsafe.allocateInstance` (private constructor, Context-bound) and
         * then calls `record()` on it, so every instance field it did not set
         * is null there. Substituting a real flow keeps those paths on one
         * code path instead of special-casing "am I a fake".
         */
        private val revisionPlaceholder = MutableStateFlow(0)

        @Volatile
        private var instance: BrowserHistoryStore? = null

        fun getInstance(context: Context): BrowserHistoryStore {
            return instance ?: synchronized(this) {
                instance ?: BrowserHistoryStore(context.applicationContext).also { instance = it }
            }
        }
    }

    data class Entry(
        val id: String = UUID.randomUUID().toString(),
        val url: String,
        val title: String,
        val timestamp: Long = System.currentTimeMillis(),
        val domain: String = extractDomain(url),
    )

    private val entries = mutableListOf<Entry>()

    /**
     * [dv15-B3] Store-owned refresh signal: bumped by every mutation of
     * [entries], so a panel can key its memoized snapshot on it and be
     * invalidated exactly when the list actually changed — including changes
     * this panel did not make (a `record()` from another browser session, the
     * 7-day retention prune).
     *
     * Nullable on purpose: `BrowserHistorySnapshotTest` allocates the store
     * with `Unsafe.allocateInstance`, which skips the constructor, and its
     * concurrency case calls `record()` on that instance — so the bump path
     * must tolerate the field being null. Reads and writes go through
     * [revision] / [bumpRevision], which substitute [revisionPlaceholder].
     */
    private val _revision: MutableStateFlow<Int>? = MutableStateFlow(0)

    /** Observable count of this store's mutations — see [_revision]. */
    val revision: StateFlow<Int> get() = _revision ?: revisionPlaceholder

    private fun bumpRevision() {
        (_revision ?: revisionPlaceholder).update { it + 1 }
    }

    init {
        load()
    }

    fun record(url: String, title: String) {
        if (url.isEmpty()) return
        synchronized(entries) {
            // Deduplicate consecutive visits to same URL
            if (entries.lastOrNull()?.url == url) return

            entries.add(Entry(url = url, title = title))
            pruneOldLocked()
        }
        // [dv15-B3] entries changed (the early return above is the only path
        // that reaches here without a change) — let observers see it before
        // the flash write, which is the slow half.
        bumpRevision()
        save()
    }

    fun getEntries(): List<Entry> = synchronized(entries) { entries.sortedByDescending { it.timestamp } }

    fun search(query: String): List<Entry> {
        if (query.isBlank()) return getEntries()
        val q = query.lowercase()
        return synchronized(entries) {
            entries.filter {
                it.title.lowercase().contains(q) || it.url.lowercase().contains(q)
            }.sortedByDescending { it.timestamp }
        }
    }

    /**
     * Group entries into day buckets labelled with caller-supplied strings.
     *
     * [deep-audit-v14 ISSUE-08] The bucket labels used to be the English
     * literals `"Today"` / `"Yesterday"` baked in here, which bypassed every
     * translation (7 locales ship `browser_history_*` keys): a German user saw
     * "Today" above a fully German list. The labels are now required
     * parameters rather than `= "Today"` defaults — a default would let a
     * caller that forgot to pass them silently fall back to English again,
     * which is the defect this fixes.
     */
    fun groupedByDay(todayLabel: String, yesterdayLabel: String): Map<String, List<Entry>> {
        val now = System.currentTimeMillis()
        return getEntries().groupBy { entry ->
            browserHistoryDayLabel(entry.timestamp, now, todayLabel, yesterdayLabel)
        }
    }

    /** Get unique domains from history (for cookie domain listing). */
    fun uniqueDomains(): List<String> {
        return synchronized(entries) { entries.map { it.domain }.filter { it.isNotEmpty() }.distinct().sorted() }
    }

    fun clear() {
        // [dv15-B3] bump only when something was actually dropped: observers
        // do not need a recomposition for a no-op clear.
        val hadEntries = synchronized(entries) {
            val had = entries.isNotEmpty()
            entries.clear()
            had
        }
        if (hadEntries) bumpRevision()
        save()
    }

    private fun pruneOld() {
        synchronized(entries) { pruneOldLocked() }
    }

    // [fix/audit-s6m2] callers must hold the entries lock — this is a
    // check-then-act mutation (removeAll scans + mutates) that must be atomic
    // against concurrent record()/clear().
    private fun pruneOldLocked() {
        val cutoff = System.currentTimeMillis() - MAX_AGE_MS
        entries.removeAll { it.timestamp < cutoff }
    }

    private fun save() {
        try {
            // [da1022-C] Lock ONLY the shallow copy: record()/clear() mutate
            // `entries` under this same monitor, and save() is called from
            // both of them — serializing while iterating `entries` directly
            // can throw ConcurrentModificationException (and, worse, persist
            // a torn view). The copy itself is O(n) but allocation-only; keep
            // the JSON building and file write OUTSIDE the lock so a slow
            // flash write never blocks record().
            val snapshot = synchronized(entries) { entries.toList() }
            val file = File(context.filesDir, FILENAME)
            val array = JSONArray()
            for (entry in snapshot) {
                val obj = JSONObject()
                obj.put("id", entry.id)
                obj.put("url", entry.url)
                obj.put("title", entry.title)
                obj.put("timestamp", entry.timestamp)
                obj.put("domain", entry.domain)
                array.put(obj)
            }
            file.writeText(array.toString())
        } catch (e: Exception) {
            Log.w(TAG, "Failed to save history: ${e.message}")
        }
    }

    private fun load() {
        try {
            val file = File(context.filesDir, FILENAME)
            if (!file.exists()) return
            val array = JSONArray(file.readText())
            for (i in 0 until array.length()) {
                val obj = array.getJSONObject(i)
                entries.add(Entry(
                    id = obj.optString("id", UUID.randomUUID().toString()),
                    url = obj.optString("url", ""),
                    title = obj.optString("title", ""),
                    timestamp = obj.optLong("timestamp", 0),
                    domain = obj.optString("domain", ""),
                ))
            }
            pruneOld()
            // [dv15-B3] a load that found a file changed `entries` — and the
            // retention prune above can drop rows on its own.
            bumpRevision()
        } catch (e: Exception) {
            Log.w(TAG, "Failed to load history: ${e.message}")
        }
    }
}

private fun extractDomain(url: String): String {
    return try {
        URI(url).host ?: ""
    } catch (_: Exception) {
        ""
    }
}

/**
 * [deep-audit-v14 ISSUE-08] Localized day-bucket label for one history entry.
 *
 * Top-level and pure on purpose: it is the part that used to be hardcoded to
 * English, and being free of `Context`/Compose it is the one part a JVM unit
 * test can run for real (`BrowserHistorySheetContractTest`) instead of
 * asserting on source text. [now] is a parameter so "today"/"yesterday" are
 * deterministic under test.
 *
 * Entries older than yesterday keep the numeric `M/D` form — a real
 * localized date would need a locale-aware formatter, and the buckets are
 * already disambiguated by year-agnostic ordering (see `groupedByDay`).
 */
internal fun browserHistoryDayLabel(
    timestamp: Long,
    now: Long,
    todayLabel: String,
    yesterdayLabel: String,
): String {
    val cal = java.util.Calendar.getInstance().apply { timeInMillis = timestamp }
    val today = java.util.Calendar.getInstance().apply { timeInMillis = now }
    val yesterday = (today.clone() as java.util.Calendar).apply {
        add(java.util.Calendar.DAY_OF_YEAR, -1)
    }
    return when {
        sameCalendarDay(cal, today) -> todayLabel
        sameCalendarDay(cal, yesterday) -> yesterdayLabel
        else -> {
            val month = cal.get(java.util.Calendar.MONTH) + 1
            val day = cal.get(java.util.Calendar.DAY_OF_MONTH)
            "$month/$day"
        }
    }
}

private fun sameCalendarDay(a: java.util.Calendar, b: java.util.Calendar): Boolean =
    a.get(java.util.Calendar.YEAR) == b.get(java.util.Calendar.YEAR) &&
        a.get(java.util.Calendar.DAY_OF_YEAR) == b.get(java.util.Calendar.DAY_OF_YEAR)

/**
 * [deep-audit-v14 ISSUE-08] Identity of the history snapshot a derived list was
 * built from — `(count, newest entry id)`.
 *
 * The sheet keys its `remember` on this instead of on the bare count: a
 * `record()` that also prunes one entry out of the 7-day window leaves the
 * count unchanged, and a size-only key cannot see that replacement. Pure, so
 * `BrowserHistorySheetContractTest` pins the "equal count, different content ⇒
 * different key" decision on the JVM.
 */
internal fun historySnapshotKey(entries: List<BrowserHistoryStore.Entry>): Pair<Int, String?> =
    entries.size to entries.firstOrNull()?.id


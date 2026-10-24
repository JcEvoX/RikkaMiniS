package com.rikkaminis.app.workspace

import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Executes one memory-rollup pass. By default it selects the largest completed
 * daily log that has not yet been distilled and contains at least one stable
 * entry. A specific `yyyy-MM-dd` date can be supplied for deterministic or
 * targeted rollups. The source log is never modified.
 */
class MemoryRollupRunner(
    private val memoryDir: File,
    clock: () -> Date = { Date() },
) {
    companion object {
        private const val TAG = "MemoryRollupRunner"
        const val DEFAULT_FILE_NAME = MemoryRollupEngine.ROLLUP_FILE
    }

    private val now: () -> Date = clock

    enum class Outcome {
        /** Rollup written for the selected daily log. */
        ROLLED_UP,

        /** The selected log was already distilled (idempotent). */
        SKIPPED_ALREADY,

        /** No daily log file to roll up (no logs, all distilled). */
        NO_LOG_YESTERDAY,

        /** Log existed but nothing in it qualified as a stable rule. */
        NOTHING_TO_DISTILL,

        /** I/O failure reading or writing. */
        ERROR,
    }

    /**
     * Roll up one daily log. When [dateStr] is null (the common agent path),
     * pick the largest `yyyy-MM-dd.md` file that is not yet covered by an
     * existing `MEMORY-ROLLUP.md` section and that actually contains
     * distillable entries — not just "yesterday". This fixes the mismatch
     * where a big old log could never be distilled once the calendar slid
     * past it. Explicitly passing [dateStr] bypasses the selection heuristic.
     */
    fun runOnce(dateStr: String? = null): Outcome {
        return try {
            val dateFmt = SimpleDateFormat("yyyy-MM-dd", Locale.US)

            // Explicit date wins; otherwise pick the best "not yet rolled up"
            // daily log (largest, distillable, excluding today).
            val resolvedDate: String = if (dateStr != null) {
                dateStr
            } else {
                pickLargestEligibleDate(dateFmt) ?: return Outcome.NO_LOG_YESTERDAY
            }

            val logFile = File(memoryDir, "$resolvedDate.md")
            if (!logFile.exists() || logFile.length() == 0L) {
                Log.i(TAG, "no daily log for $resolvedDate — nothing to roll up")
                return Outcome.NO_LOG_YESTERDAY
            }

            val rollupFile = File(memoryDir, MemoryRollupEngine.ROLLUP_FILE)
            if (rollupFile.exists()) {
                val existing = runCatching { rollupFile.readText() }.getOrDefault("")
                if (MemoryRollupEngine.hasRollupForDate(existing, resolvedDate)) {
                    Log.i(TAG, "$resolvedDate already rolled up — skip (idempotent)")
                    return Outcome.SKIPPED_ALREADY
                }
            }

            val logText = logFile.readText()
            val entries = MemoryRollupEngine.extractEntries(logText)
            // [dv28-A3] Candidacy owns the content dimension now, so the picker
            // can only select a log that holds a distillable entry. This branch
            // therefore only fires on an EXPLICIT date (which bypasses the
            // picker) or through the picker's last-resort "no eligible log"
            // branch below — both are intentional: the Outcome contract stays,
            // and a targeted `memory_rollup 2026-08-11` on a transient-only log
            // still answers "nothing to distill" instead of silently no-op'ing.
            val stableEntries = MemoryRollupEngine.distillableEntries(entries)
            if (stableEntries.isEmpty()) {
                Log.i(TAG, "$resolvedDate.md had no distillable rules (${entries.size} entries, all transient)")
                return Outcome.NOTHING_TO_DISTILL
            }

            val timeFmt = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US)
            val generatedAt = timeFmt.format(now())
            val section = MemoryRollupEngine.buildRollupText(resolvedDate, entries, generatedAt)
            if (section.isEmpty()) {
                Log.i(TAG, "$resolvedDate.md produced an empty rollup section")
                return Outcome.NOTHING_TO_DISTILL
            }

            // Append the section; create the rollup file on first run.
            if (rollupFile.exists()) {
                rollupFile.appendText("\n$section")
            } else {
                rollupFile.writeText(section)
            }
            Log.i(
                TAG,
                "rolled up $resolvedDate.md → ${MemoryRollupEngine.ROLLUP_FILE} " +
                    "(${stableEntries.size}/${entries.size} entries distilled)",
            )
            Outcome.ROLLED_UP
        } catch (t: Throwable) {
            Log.e(TAG, "rollup failed", t)
            Outcome.ERROR
        }
    }

    /**
     * Choose the daily log to roll up when no explicit date was given.
     * Excludes today (the current day is still being written). Ordered by
     * "biggest, oldest first": among eligible files (existing, non-empty,
     * not yet in the rollup, with at least one distillable entry) the largest
     * wins; ties break to the oldest. Returns null when nothing qualifies.
     */
    private fun pickLargestEligibleDate(dateFmt: SimpleDateFormat): String? {
        val rollupFile = File(memoryDir, MemoryRollupEngine.ROLLUP_FILE)
        val rollupContent =
            if (rollupFile.exists()) runCatching { rollupFile.readText() }.getOrDefault("") else ""
        val todayStr = dateFmt.format(now())
        val files: Array<File>? = memoryDir.listFiles { f -> f.isFile && f.name.endsWith(".md") }
        if (files.isNullOrEmpty()) return null

        val eligible = files
            .mapNotNull { f ->
                // [dv15-A4] Candidacy judged by the shared predicate — the same
                // one the prompt-side size hint uses (MemoryRepository
                // .dailyLogSizes). Both sides used to spell the date shape and
                // the "already distilled" cutoff themselves, and the hint's copy
                // had lost them.
                // [dv28-A3] The content dimension is part of that predicate too,
                // so this picker no longer extracts/classifies: one decision,
                // both sides.
                if (!MemoryRollupEngine.isRollupCandidate(
                        f.name,
                        f.length(),
                        todayStr,
                        rollupContent,
                    ) { runCatching { f.readText() }.getOrNull() }
                ) {
                    return@mapNotNull null
                }
                f to f.name.removeSuffix(".md")
            }
            .sortedWith(compareByDescending<Pair<File, String>> { it.first.length() }.thenBy { it.second })

        if (eligible.isNotEmpty()) return eligible.first().second

        // If no eligible log remains, select the largest non-empty old log so
        // callers still receive SKIPPED_ALREADY or NOTHING_TO_DISTILL rather
        // than a misleading "no log" result.
        return files
            .mapNotNull { f ->
                if (!MemoryRollupEngine.isPastDailyLog(f.name, f.length(), todayStr)) {
                    return@mapNotNull null
                }
                f to f.name.removeSuffix(".md")
            }
            .sortedWith(compareByDescending<Pair<File, String>> { it.first.length() }.thenBy { it.second })
            .firstOrNull()
            ?.second
    }
}
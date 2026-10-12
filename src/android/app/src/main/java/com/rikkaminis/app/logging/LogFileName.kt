package com.rikkaminis.app.logging

import java.io.File
import java.io.RandomAccessFile

/**
 * [T-log-single-writer] Per-process daily log file names.
 *
 * The main process keeps the historical `<prefix>-<date>.log`; any other
 * process (`:modelservice`, future `:toolservice`) writes
 * `<prefix>-<date>.<tag>.log`. A single file therefore never has two writers,
 * which makes the O_APPEND interleaving measured on 2026-09-18 structurally
 * impossible: after d09f3adf the worker process began writing the same
 * daily files as the main process, and the two writers' buffered flushes
 * produced out-of-order lines (main lagging the worker by 4-24s on batch
 * flush) plus one garbled line where two fragments landed in a single line.
 *
 * The setting page's `minis-` prefix filter (AppLogger.listLogFileMetas)
 * keeps matching the suffixed variants, so worker files stay visible there.
 *
 * [T-log-roll-1012] Debug-roll naming. The debug channel rotates by renaming
 * the over-cap file to `...N.log` where N REPLACES an existing trailing
 * `.<int>` segment instead of blindly appending `.1`:
 *
 *   debug-2026-10-12.modelservice.log   → roll 1 → debug-2026-10-12.modelservice.1.log
 *   debug-2026-10-12.modelservice.1.log → roll 2 → debug-2026-10-12.modelservice.2.log
 *
 * Why: the previous scheme renamed every roll to the SAME `.1.log` name and
 * deleted whatever was already there, so a fresh roll destroyed the previous
 * roll's bytes — measured 2026-10-12: a 211 MB roll survived ~7 minutes
 * before the next rotation overwrote it, destroying the very evidence the
 * roll existed to keep. With shifting indices the newest roll always lands
 * on the highest index and older rolls keep their bytes until they are the
 * ones evicted by [AppLogger.DEBUG_MAX_ROLLS].
 */
internal fun channelFileName(prefix: String, date: String, processSuffix: String?): String =
    if (processSuffix.isNullOrEmpty()) "$prefix-$date.log"
    else "$prefix-$date.$processSuffix.log"

/**
 * The name base's current roll would produce: replaces a trailing `.<int>`
 * segment (before the final `.log`) with [rollIndex], or appends one.
 * `debug-2026-10-12.modelservice.log` + 1 → `debug-2026-10-12.modelservice.1.log`;
 * `debug-2026-10-12.modelservice.1.log` + 2 → `debug-2026-10-12.modelservice.2.log`.
 */
internal fun debugRollFileName(baseName: String, rollIndex: Int): String {
    val stem = baseName.substringBeforeLast(".log")
    val shifted = Regex("\\.\\d+$").replace(stem, ".$rollIndex")
    return if (shifted == stem) "$stem.$rollIndex.log" else "$shifted.log"
}

/**
 * Highest existing roll index for [baseName] among [names] (its own process
 * family), or 0 when none. `debug-…modelservice.log` scans
 * `debug-…modelservice.<int>.log` siblings only — `.log`-suffixed names whose
 * middle segment is not a bare integer (error snapshots, other days) never match.
 */
internal fun highestRollIndex(names: Collection<String>, baseName: String): Int {
    val stem = baseName.substringBeforeLast(".log")
    var highest = 0
    for (name in names) {
        if (!name.startsWith("$stem.") || !name.endsWith(".log")) continue
        val mid = name.removePrefix("$stem.").removeSuffix(".log")
        mid.toIntOrNull()?.let { if (it > highest) highest = it }
    }
    return highest
}

/**
 * [T-log-tail-heal-1012] Repair a torn tail left by an interrupted write.
 *
 * The daily file names are per-PROCESS but long-lived across process
 * generations: a kill / silent_kill that lands inside a buffered write
 * (JVM StreamEncoder flushes in 8 KB blocks) leaves a trailing fragment
 * WITHOUT a newline, and the next process's first append glues onto it —
 * corrupting two log lines into one. Measured 2026-10-12: 26 torn lines in
 * the main file / 78 in the worker file, every one glued to the next
 * process's session-start line.
 *
 * If [file] exists and does not end with `\n`, appends a newline plus
 * [markerLine] so both fragments become parseable lines again. Appending,
 * never truncating — the fragment itself may hold the only copy of that log
 * line. Returns true when a heal was performed. Best-effort: any I/O error
 * returns false and must never break logging.
 *
 * Pure JVM (no Android imports) so the tail logic is unit-testable.
 */
internal fun healTornTail(file: File, markerLine: String): Boolean {
    return try {
        if (!file.exists() || file.length() == 0L) return false
        RandomAccessFile(file, "rw").use { raf ->
            val len = raf.length()
            if (len == 0L) return false
            raf.seek(len - 1)
            if (raf.read() == '\n'.code) return false
            raf.seek(len)
            raf.write(("\n$markerLine\n").toByteArray(Charsets.UTF_8))
            true
        }
    } catch (_: Exception) {
        false
    }
}

/**
 * `/proc/self/cmdline` → per-process suffix: `com.rikkaminis.app:modelservice`
 * → `modelservice`; the main process (name equals [packageName]) / null /
 * empty → null (historical main-process file name).
 */
internal fun processLogSuffix(cmdline: String?, packageName: String): String? {
    val p = cmdline?.replace("\u0000", "")?.trim() ?: return null
    if (p.isEmpty() || p == packageName) return null
    return p.substringAfterLast(':', "").ifEmpty { null }
}

/**
 * One-shot `/proc/self/cmdline` read; null on any failure so the caller falls
 * back to the historical main-process file name rather than failing init.
 */
internal fun readOwnCmdline(): String? = runCatching {
    File("/proc/self/cmdline").readBytes().toString(Charsets.UTF_8)
}.getOrNull()

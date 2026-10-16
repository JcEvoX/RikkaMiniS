package com.rikkaminis.app.sandbox.offload

import java.io.File
import java.io.IOException
import java.io.RandomAccessFile

/**
 * Bounded, incremental line reader for append-only JSONL files
 * (stream.jsonl in the model-exec protocol).
 *
 * Guarantees:
 *   - per-call read is capped at [maxReadBytes]; a big write longer than that
 *     is consumed in bounded windows, never pulled whole into memory.
 *   - a single line longer than [maxLineBytes] is reported as
 *     [ReadResult.OversizedLine] and dropped, never ballooned in memory.
 *     A window that contains newlines can still hold an oversized line
 *     ([maxReadBytes] may exceed [maxLineBytes]); such a line is dropped and
 *     reported via [ReadResult.Lines.oversizedLines] while the well-formed
 *     lines around it are still returned — a neighbor's bulk must not cost
 *     the agent its good frames.
 *   - an unterminated trailing line is NOT consumed: the caller keeps the
 *     returned `newOffset` and simply re-polls from there, so the partial
 *     tail is re-read together with whatever the producer appends — a chunk
 *     is never split or lost across polls.
 *
 * Why no leftover buffer: the original ChatStreamOffloadHandler advances its
 * byte offset only to the last complete newline, so a trailing partial line is
 * naturally re-read on the next poll together with new bytes. Keeping a separate
 * in-memory leftover would DOUBLE-COUNT the partial region on re-read. This
 * class matches that proven, allocation-friendly pattern.
 *
 * Pure JVM (java.io). Single-threaded consumer only.
 */
class BoundedLineReader(
    private val maxReadBytes: Int = PayloadSizePolicy.MAX_POLL_READ_BYTES,
    private val maxLineBytes: Int = 256 * 1024,
) {

    sealed class ReadResult {
        /** Complete newline-terminated lines, and the new byte offset (just past
         *  the last consumed newline). [oversizedLines] lists the lines in this
         *  window whose content exceeded [maxLineBytes]: they were dropped by
         *  the reader (never decoded) and are already skipped by [newOffset] —
         *  the caller only needs to report them, not to advance past them. */
        data class Lines(
            val lines: List<String>,
            val newOffset: Long,
            val oversizedLines: List<OversizedLine> = emptyList(),
        ) : ReadResult()

        /** A single unterminated line grew past [maxLineBytes] in this window; it
         *  was dropped. [lineStartOffset] is where the oversized line began.
         *  (Oversized lines found INSIDE a newline-containing window are reported
         *  through [Lines.oversizedLines] instead, so the window's well-formed
         *  lines are not lost.) */
        data class OversizedLine(val lineStartOffset: Long, val lineByteLength: Long) : ReadResult()

        /** No complete new line available yet (only a partial trail, or nothing new). */
        object Partial : ReadResult()
    }

    /**
     * Read bytes in `[offset, newLen)` and split into complete lines.
     *
     * @param file the append-only JSONL file
     * @param offset byte offset at which to start reading
     * @param newLen current file length
     */
    fun readAppended(file: File, offset: Long, newLen: Long): ReadResult {
        if (offset >= newLen) return ReadResult.Partial
        val window = (newLen - offset).coerceAtMost(maxReadBytes.toLong())
        if (window <= 0) return ReadResult.Partial

        val bytes = ByteArray(window.toInt())
        val got = try {
            RandomAccessFile(file, "r").use { raf ->
                raf.seek(offset)
                val n = raf.read(bytes, 0, bytes.size)
                if (n < 0) 0 else n
            }
        } catch (e: IOException) {
            return ReadResult.Partial
        }
        if (got <= 0) return ReadResult.Partial

        // window may have been truncated by maxReadBytes mid-line. If there is no
        // newline at all in what we read, it is either a fresh partial (Partial)
        // or an oversized single line (OversizedLine) — never keep it.
        val consumed = ByteArray(got)
        System.arraycopy(bytes, 0, consumed, 0, got)

        // Find last newline byte in this window.
        var lastNl = -1
        for (i in consumed.indices) {
            if (consumed[i] == '\n'.code.toByte()) lastNl = i
        }

        if (lastNl < 0) {
            // No complete line in the window.
            return if (got > maxLineBytes) {
                ReadResult.OversizedLine(offset, got.toLong())
            } else {
                ReadResult.Partial
            }
        }

        // Complete region is [0, lastNl); the trailing partial tail is [lastNl+1, got)
        // and is intentionally NOT consumed — the caller re-reads it next poll.
        val newOffset = offset + lastNl + 1
        // [deep-audit-1018 ISSUE-06] maxReadBytes (1 MiB by default) is larger
        // than maxLineBytes (256 KiB), so a window CAN hold a complete line that
        // is itself oversized. Enforce the cap per line here too — otherwise the
        // oversized line is decoded whole and returned, breaking the class KDoc
        // ("never ballooned") while its neighbours are perfectly good frames.
        val decoded = decodeLinesBounded(consumed, lastNl, offset)
        return ReadResult.Lines(decoded.first, newOffset, decoded.second)
    }

    /**
     * Split the complete region `[0, regionEnd)` into decoded lines, dropping
     * (without decoding) any line whose content exceeds [maxLineBytes].
     *
     * Byte-level scanning is UTF-8 safe: `'\n'` (0x0A) never occurs inside a
     * multi-byte sequence. The compared length excludes the terminating `'\n'`
     * (matching the no-newline branch, which compares the raw byte count) but
     * includes a trailing `'\r'`.
     *
     * Returns the decoded lines (in order, with the oversized ones omitted) and
     * the [ReadResult.OversizedLine] reports for the dropped ones.
     */
    private fun decodeLinesBounded(
        consumed: ByteArray,
        regionEnd: Int,
        windowOffset: Long,
    ): Pair<List<String>, List<ReadResult.OversizedLine>> {
        if (regionEnd <= 0) return emptyList<String>() to emptyList()
        val lines = ArrayList<String>()
        val oversized = ArrayList<ReadResult.OversizedLine>()
        var lineStart = 0
        var i = 0
        while (i < regionEnd) {
            if (consumed[i] == '\n'.code.toByte()) {
                appendDecodedLine(consumed, lineStart, i, windowOffset, lines, oversized)
                lineStart = i + 1
            }
            i++
        }
        // Trailing segment. When the region itself ends with '\n' (an empty line
        // before the final terminator) this appends "" — same as the split('\n')
        // this replaces.
        appendDecodedLine(consumed, lineStart, regionEnd, windowOffset, lines, oversized)
        return lines to oversized
    }

    private fun appendDecodedLine(
        consumed: ByteArray,
        start: Int,
        end: Int,
        windowOffset: Long,
        lines: MutableList<String>,
        oversized: MutableList<ReadResult.OversizedLine>,
    ) {
        val contentLen = end - start
        if (contentLen > maxLineBytes) {
            oversized.add(ReadResult.OversizedLine(windowOffset + start, contentLen.toLong()))
            return
        }
        val text = String(consumed, start, contentLen, Charsets.UTF_8)
        lines.add(if (text.endsWith('\r')) text.dropLast(1) else text)
    }
}

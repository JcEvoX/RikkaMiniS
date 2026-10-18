package com.rikkaminis.app.ui.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [da1026-F] deep-audit-1026 ISSUE-06 裂口 A — [readCsvPreviewSlice] must bound
 * BOTH the row count and the bytes read for the CSV/TSV preview.
 *
 * Pre-fix (evidence: `probe/pre-grep.txt` @ dc04fc3) CsvPreview streamed
 * `bufferedReader().readLine()` with a 200-ROW cap and nothing else. A row cap
 * is not a memory bound: `readLine()` materialises the whole line before the
 * cap can look at it, so a CSV whose single line is hundreds of MB grew one
 * String to the file size and died with `OutOfMemoryError` — an `Error`, so the
 * caller's `catch (e: Exception)` never saw it and the process was killed. This
 * is the same class of bug [rv1014-E1] fixed for TextPreview / MarkdownPreview
 * / JsonPreview and [da1020-08] fixed for buildPrintHtml; CsvPreview was the
 * one site left because the gate only looked for `.readBytes()`.
 *
 * All expected values are hard literals — never derived from
 * MAX_TEXT_PREVIEW_BYTES / MAX_CSV_PREVIEW_ROWS — so mutating a production
 * constant cannot silently turn a failing expectation green.
 */
class FilePreviewCsvBoundedTest {

    private fun write(body: String): File {
        val f = File.createTempFile("da1026-csv", ".csv")
        f.deleteOnExit()
        f.writeText(body, Charsets.UTF_8)
        return f
    }

    // ── ① ordinary small CSV ────────────────────────────────────────────────

    @Test
    fun `small csv parses every row as a cell list`() {
        val s = readCsvPreviewSlice(write("h1,h2\na,b\nc,d\n"), ',', 200)
        assertEquals(
            listOf(listOf("h1", "h2"), listOf("a", "b"), listOf("c", "d")),
            s.rows,
        )
        assertFalse(s.byteTruncated)
        assertFalse(s.rowTruncated)
    }

    @Test
    fun `a terminating newline does not produce a phantom empty row`() {
        assertEquals(3, readCsvPreviewSlice(write("a\nb\nc\n"), ',', 200).rows.size)
    }

    @Test
    fun `empty file yields no rows and no truncation verdicts`() {
        val s = readCsvPreviewSlice(write(""), ',', 200)
        assertEquals(emptyList<List<String>>(), s.rows)
        assertFalse(s.byteTruncated)
        assertFalse(s.rowTruncated)
    }

    @Test
    fun `crlf endings keep the carriage return out of the last cell`() {
        val s = readCsvPreviewSlice(write("h1,h2\r\na,b\r\n"), ',', 200)
        assertEquals(listOf(listOf("h1", "h2"), listOf("a", "b")), s.rows)
    }

    @Test
    fun `quoted separator stays inside its cell`() {
        val s = readCsvPreviewSlice(write("\"a,b\",c\n"), ',', 200)
        assertEquals(listOf(listOf("a,b", "c")), s.rows)
    }

    @Test
    fun `tsv splits on tab`() {
        val s = readCsvPreviewSlice(write("a\tb\n"), '\t', 200)
        assertEquals(listOf(listOf("a", "b")), s.rows)
    }

    // ── ② exactly at the byte cap ───────────────────────────────────────────

    /** One 512-byte physical line: "r," + 509 'x' + LF. */
    private fun row512() = "r," + "x".repeat(509) + "\n"

    @Test
    fun `file exactly at the byte cap is not marked truncated and keeps every row`() {
        val body = row512().repeat(1000)
        assertEquals(512_000, body.length) // fixture sanity: exactly the cap
        val s = readCsvPreviewSlice(write(body), ',', 100_000)
        assertFalse(s.byteTruncated)
        assertFalse(s.rowTruncated)
        assertEquals(1000, s.rows.size)
        assertEquals(listOf("r", "x".repeat(509)), s.rows[0])
        assertEquals(listOf("r", "x".repeat(509)), s.rows[999])
    }

    @Test
    fun `row cap trips when the file holds more rows than maxRows`() {
        val body = row512().repeat(1000)
        val s = readCsvPreviewSlice(write(body), ',', 200)
        assertFalse(s.byteTruncated)
        assertTrue(s.rowTruncated)
        assertEquals(200, s.rows.size)
        assertEquals(509, s.rows[0][1].length)
    }

    // ── ③ over the cap, including a single 10 MiB line ──────────────────────

    @Test
    fun `single 10mib line is byte-capped instead of materialised`() {
        val body = "x".repeat(10 * 1024 * 1024) // one line, no newline at all
        val s = readCsvPreviewSlice(write(body), ',', 200)
        assertTrue(s.byteTruncated)
        assertFalse(s.rowTruncated)
        // The only "line" is the byte-cap fragment. It is DISCARDED, not
        // rendered as a half row that would look complete.
        assertEquals(0, s.rows.size)
    }

    @Test
    fun `byte cap keeps the complete rows and drops only the fragment`() {
        val body = "a,b\nc,d\n" + "y".repeat(600_000)
        val s = readCsvPreviewSlice(write(body), ',', 200)
        assertTrue(s.byteTruncated)
        assertFalse(s.rowTruncated)
        assertEquals(listOf(listOf("a", "b"), listOf("c", "d")), s.rows)
    }

    @Test
    fun `a cut landing exactly on a newline drops the last complete row (documented over-drop)`() {
        // 1000 complete 512-byte rows, then more bytes: the cap lands on row
        // 1000's newline, so row 1000 is complete — yet the policy is "never
        // render a row that might be partial", so it is dropped too. 999, not
        // 1000. Bounded over-drop, never an under-drop.
        val s = readCsvPreviewSlice(write(row512().repeat(1000) + row512()), ',', 100_000)
        assertTrue(s.byteTruncated)
        assertFalse(s.rowTruncated)
        assertEquals(999, s.rows.size)
    }

    // ── ④ multi-byte UTF-8 straddling the cap ───────────────────────────────

    @Test
    fun `byte cap landing mid utf8 sequence leaks neither a stump nor the fragment`() {
        // 127999 rows of "a,b\n" = 511996 bytes, + "xxx" = 511999, then "é"
        // whose two bytes straddle byte 512000, then a tail that is never read.
        val body = "a,b\n".repeat(127_999) + "xxx" + "é" + "tail,z\n"
        val s = readCsvPreviewSlice(write(body), ',', 200)
        assertTrue(s.byteTruncated)
        assertTrue(s.rowTruncated)
        assertEquals(200, s.rows.size)
        assertEquals(listOf("a", "b"), s.rows[0])
        assertEquals(listOf("a", "b"), s.rows[199])
        // No replacement char and no fragment text may reach a rendered cell.
        assertTrue(s.rows.none { row -> row.any { cell -> cell.contains('\uFFFD') || cell.contains("xxx") } })
    }

    // ── the helper itself (readLine-equivalent splitting) ───────────────────

    @Test
    fun `splitPreviewLines mirrors readLine for every terminator shape`() {
        assertEquals(emptyList<String>(), splitPreviewLines(""))
        assertEquals(listOf("a"), splitPreviewLines("a"))
        assertEquals(listOf("a"), splitPreviewLines("a\n"))
        assertEquals(listOf("a", "b"), splitPreviewLines("a\nb"))
        assertEquals(listOf(""), splitPreviewLines("\n"))
        assertEquals(listOf("", ""), splitPreviewLines("\n\n"))
        assertEquals(listOf("a"), splitPreviewLines("a\r\n"))
        assertEquals(listOf("a", "b"), splitPreviewLines("a\rb"))
        assertEquals(listOf("a", "b"), splitPreviewLines("a\r\nb\r\n"))
    }
}

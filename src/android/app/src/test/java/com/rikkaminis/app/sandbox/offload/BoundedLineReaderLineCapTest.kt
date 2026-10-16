package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * [deep-audit-1018 ISSUE-06] `BoundedLineReader` 逐行长度上限：**窗口内含换行时**
 * 也要按 `maxLineBytes` 校验。
 *
 * 类 KDoc 承诺 "a single line longer than maxLineBytes is reported ... and
 * dropped, never ballooned in memory"，但 `lastNl >= 0` 分支把整个 region 一次
 * 解码后 split —— 默认 `maxReadBytes`（1 MiB）> `maxLineBytes`（256 KiB），所以
 * 窗口里完全可以出现"完整但超长"的一行：它被整行解码并返回，承诺被破坏。
 *
 * 修法保持两条语义：
 *  - 超长行不返回（不解码），经 `Lines.oversizedLines` 上报；
 *  - 同窗口内**合法**的行照旧返回（邻行的体积不能连坐丢掉 agent 的帧），
 *    且 `newOffset` 已越过被丢的行，下一次 poll 不会重读它（无饿死/死循环）。
 *
 * 期望值全部写死字面量（不从实现推导）。
 */
class BoundedLineReaderLineCapTest {

    @org.junit.Rule
    @JvmField
    val tmp = TemporaryFolder()

    private fun file(name: String, content: String): File {
        val f = tmp.newFile(name)
        f.writeText(content)
        return f
    }

    @Test
    fun `oversized line inside a newline window is dropped and reported`() {
        // "a\n" (2B) + 9×"B" (9B, > cap 8) + "\n" + "c\n" (2B)
        val content = "a\n" + "B".repeat(9) + "\nc\n"
        val f = file("s.jsonl", content)
        val r = BoundedLineReader(maxReadBytes = 1024, maxLineBytes = 8)

        val res = r.readAppended(f, 0, f.length())
        assertTrue("must still be Lines (good frames survive)", res is BoundedLineReader.ReadResult.Lines)
        val lines = res as BoundedLineReader.ReadResult.Lines
        assertEquals(listOf("a", "c"), lines.lines)
        assertEquals(1, lines.oversizedLines.size)
        assertEquals(2L, lines.oversizedLines[0].lineStartOffset)
        assertEquals(9L, lines.oversizedLines[0].lineByteLength)
        assertEquals(content.length.toLong(), lines.newOffset)
    }

    @Test
    fun `line exactly at the cap is kept`() {
        val content = "12345678\n"
        val f = file("s.jsonl", content)
        val r = BoundedLineReader(maxReadBytes = 1024, maxLineBytes = 8)

        val lines = r.readAppended(f, 0, f.length()) as BoundedLineReader.ReadResult.Lines
        assertEquals(listOf("12345678"), lines.lines)
        assertTrue(lines.oversizedLines.isEmpty())
        assertEquals(9L, lines.newOffset)
    }

    @Test
    fun `line one byte over the cap is dropped`() {
        val content = "123456789\n"
        val f = file("s.jsonl", content)
        val r = BoundedLineReader(maxReadBytes = 1024, maxLineBytes = 8)

        val lines = r.readAppended(f, 0, f.length()) as BoundedLineReader.ReadResult.Lines
        assertTrue(lines.lines.isEmpty())
        assertEquals(1, lines.oversizedLines.size)
        assertEquals(0L, lines.oversizedLines[0].lineStartOffset)
        assertEquals(9L, lines.oversizedLines[0].lineByteLength)
        assertEquals(10L, lines.newOffset)
    }

    @Test
    fun `window with no newline and content at or under the cap stays Partial`() {
        // No newline at all, content fits under the cap: the lastNl < 0 branch
        // keeps its original semantics — nothing is dropped, nothing advanced.
        val f = file("s.jsonl", "abc")
        val r = BoundedLineReader(maxReadBytes = 1024, maxLineBytes = 8)
        val res = r.readAppended(f, 0, f.length())
        assertTrue(res is BoundedLineReader.ReadResult.Partial)

        // Exactly at the cap, still no newline: Partial, not OversizedLine.
        val g = file("g.jsonl", "Z".repeat(8))
        val rg = BoundedLineReader(maxReadBytes = 1024, maxLineBytes = 8)
        assertTrue(rg.readAppended(g, 0, g.length()) is BoundedLineReader.ReadResult.Partial)
    }

    @Test
    fun `unterminated oversized fragment still reports OversizedLine`() {
        // No newline at all: the lastNl < 0 branch keeps its original semantics.
        val f = file("s.jsonl", "Z".repeat(64))
        val r = BoundedLineReader(maxReadBytes = 1024, maxLineBytes = 8)

        val res = r.readAppended(f, 0, f.length())
        assertTrue(res is BoundedLineReader.ReadResult.OversizedLine)
        val ov = res as BoundedLineReader.ReadResult.OversizedLine
        assertEquals(0L, ov.lineStartOffset)
        assertEquals(64L, ov.lineByteLength)
    }

    @Test
    fun `a dropped line is not re-read on the next poll`() {
        val content = "a\n" + "B".repeat(9) + "\n"
        val f = file("s.jsonl", content)
        val r = BoundedLineReader(maxReadBytes = 1024, maxLineBytes = 8)

        val first = r.readAppended(f, 0, f.length()) as BoundedLineReader.ReadResult.Lines
        // ["a"]: the oversized B-line is dropped; the trailing segment here IS
        // the oversized line (not an empty string), so nothing else survives.
        assertEquals(listOf("a"), first.lines)
        assertEquals(1, first.oversizedLines.size)
        assertEquals(12L, first.newOffset)

        // Nothing left to read: the oversized line must not be re-reported
        // forever (that would be an infinite poll loop), and the reader must
        // report Partial, not a repeat of the dropped line.
        assertTrue(r.readAppended(f, first.newOffset, f.length()) is BoundedLineReader.ReadResult.Partial)
    }

    @Test
    fun `reported offsets are absolute, not window relative`() {
        // First window ends exactly at the "head" newline; the oversized line
        // starts at absolute offset 5, not at the window's 0.
        val content = "head\n" + "B".repeat(9) + "\n"
        val f = file("s.jsonl", content)
        // Window (10B) is wide enough for the whole oversized line even though
        // the line (9B) exceeds the cap (8B) — exactly the case ISSUE-06 missed.
        val r = BoundedLineReader(maxReadBytes = 10, maxLineBytes = 8)

        val first = r.readAppended(f, 0, f.length()) as BoundedLineReader.ReadResult.Lines
        assertEquals(listOf("head"), first.lines)
        assertEquals(5L, first.newOffset)

        val second = r.readAppended(f, first.newOffset, f.length()) as BoundedLineReader.ReadResult.Lines
        assertTrue(second.lines.isEmpty())
        assertEquals(1, second.oversizedLines.size)
        assertEquals(5L, second.oversizedLines[0].lineStartOffset)
        assertEquals(9L, second.oversizedLines[0].lineByteLength)
        assertEquals(content.length.toLong(), second.newOffset)
    }

    @Test
    fun `CRLF lines keep their CR stripped`() {
        val content = "abc\r\ndefg\r\n"
        val f = file("s.jsonl", content)
        val r = BoundedLineReader(maxReadBytes = 1024, maxLineBytes = 8)

        val lines = r.readAppended(f, 0, f.length()) as BoundedLineReader.ReadResult.Lines
        assertEquals(listOf("abc", "defg"), lines.lines)
        assertTrue(lines.oversizedLines.isEmpty())
        assertEquals(content.length.toLong(), lines.newOffset)
    }

    @Test
    fun `empty complete region yields no lines but a leading empty line is kept`() {
        // Region is exactly one newline wide → empty region → no lines.
        val a = file("a.jsonl", "\n\n")
        val ra = BoundedLineReader(maxReadBytes = 1, maxLineBytes = 8)
        val la = ra.readAppended(a, 0, a.length()) as BoundedLineReader.ReadResult.Lines
        assertTrue(la.lines.isEmpty())
        assertEquals(1L, la.newOffset)

        // A newline-terminated empty line inside the region is a real line
        // (same as the split('\n') this replaced).
        val b = file("b.jsonl", "\nx\n")
        val rb = BoundedLineReader(maxReadBytes = 1024, maxLineBytes = 8)
        val lb = rb.readAppended(b, 0, b.length()) as BoundedLineReader.ReadResult.Lines
        assertEquals(listOf("", "x"), lb.lines)
        assertEquals(3L, lb.newOffset)
    }
}

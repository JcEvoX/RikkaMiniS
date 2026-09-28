package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * JVM tests for the pure formatting functions in [ChatToolFormatting].
 *
 * These functions are defined in ChatToolFormatting.kt alongside
 * Compose-dependent functions (toolAccentColor, toolIconFor). Since
 * the JVM only resolves constant pool entries lazily, calling only
 * pure functions from tests works without Compose on the classpath.
 */
class ChatFormattingTest {

    // ── formatStepDuration ────────────────────────────────────────────

    @Test
    fun `formatStepDuration 0 seconds`() {
        assertEquals("0s", formatStepDuration(0, false))
    }

    @Test
    fun `formatStepDuration under 60 seconds`() {
        assertEquals("45s", formatStepDuration(45, false))
    }

    @Test
    fun `formatStepDuration exactly 60 seconds`() {
        assertEquals("1m", formatStepDuration(60, false))
    }

    @Test
    fun `formatStepDuration 90 seconds`() {
        assertEquals("1m30s", formatStepDuration(90, false))
    }

    @Test
    fun `formatStepDuration exactly 1 hour`() {
        assertEquals("1h", formatStepDuration(3600, false))
    }

    @Test
    fun `formatStepDuration 1h 12m`() {
        assertEquals("1h12m", formatStepDuration(4320, false))
    }

    @Test
    fun `formatStepDuration 2h 0m`() {
        assertEquals("2h", formatStepDuration(7200, false))
    }

    @Test
    fun `formatStepDuration still running suffix`() {
        assertEquals("12s…", formatStepDuration(12, true))
    }

    @Test
    fun `formatStepDuration still running with minutes`() {
        assertEquals("2m30s…", formatStepDuration(150, true))
    }

    @Test
    fun `formatStepDuration negative clamps to 0`() {
        assertEquals("0s", formatStepDuration(-5, false))
    }

    @Test
    fun `formatStepDuration 59 seconds`() {
        assertEquals("59s", formatStepDuration(59, false))
    }

    @Test
    fun `formatStepDuration 1s with still running`() {
        assertEquals("1s…", formatStepDuration(1, true))
    }

    // ── formatToolDuration ────────────────────────────────────────────

    @Test
    fun `formatToolDuration under 1 second`() {
        assertEquals("0.5s", formatToolDuration(500))
    }

    @Test
    fun `formatToolDuration exactly 1 second`() {
        assertEquals("1s", formatToolDuration(1000))
    }

    @Test
    fun `formatToolDuration 45 seconds`() {
        assertEquals("45s", formatToolDuration(45000))
    }

    @Test
    fun `formatToolDuration 1 minute`() {
        assertEquals("1m 0s", formatToolDuration(60000))
    }

    @Test
    fun `formatToolDuration 2m 30s`() {
        assertEquals("2m 30s", formatToolDuration(150000))
    }

    @Test
    fun `formatToolDuration 0 ms`() {
        assertEquals("0.0s", formatToolDuration(0))
    }

    // ── toolDisplayName ───────────────────────────────────────────────

    @Test
    fun `toolDisplayName shell_execute`() {
        assertEquals("terminal", toolDisplayName("shell_execute"))
    }

    @Test
    fun `toolDisplayName file_read`() {
        assertEquals("file reader", toolDisplayName("file_read"))
    }

    @Test
    fun `toolDisplayName unknown tool returns name`() {
        assertEquals("my_custom_tool", toolDisplayName("my_custom_tool"))
    }

    @Test
    fun `toolDisplayName browser_use`() {
        assertEquals("browser", toolDisplayName("browser_use"))
    }

    // ── toolTitleLabel ────────────────────────────────────────────────

    @Test
    fun `toolTitleLabel shell_execute`() {
        assertEquals("RikkaMinis is using Shell", toolTitleLabel("shell_execute"))
    }

    @Test
    fun `toolTitleLabel unknown tool uses display name`() {
        assertEquals("RikkaMinis is using my_custom_tool", toolTitleLabel("my_custom_tool"))
    }

    @Test
    fun `toolTitleLabel memory_write`() {
        assertEquals("RikkaMinis is using Memory", toolTitleLabel("memory_write"))
    }

    @Test
    fun `toolTitleLabel web_search`() {
        assertEquals("RikkaMinis is using Search", toolTitleLabel("web_search"))
    }

    // ── extractThinkingTitle ──────────────────────────────────────────

    @Test
    fun `extractThinkingTitle prefers the newest phrase of a concatenated run`() {
        // Real shape, session 4a9ba4d3 (2026-09-28 11:32): two action phrases
        // concatenated on one line with no separator. The whole-line rule ported
        // from rikkahub would return the merged blob; the newest span is the one
        // that says what the model is doing NOW.
        assertEquals(
            "我把子包层级也数清楚",
            extractThinkingTitle("**我统计子包层级****我把子包层级也数清楚**"),
        )
    }

    @Test
    fun `extractThinkingTitle reads the last standalone bold line`() {
        // Byte-exact tail of the real leaked thinking row in session 9068927b
        // (2026-09-27 17:59): English narration above a standalone bold line.
        val text = "Need verify current branch tests perhaps run actual jvm harness? Existing evidence says 4/4, " +
            "but our independent audit maybe no need, still evidence. Could inspect run.sh perhaps it failed " +
            "due hidden output no report. Use run.sh output.\n" +
            "**最后补读未合并档案分支的完整 diff，并确认远端指针；代码主线不再改动。**\n"
        assertEquals(
            "最后补读未合并档案分支的完整 diff，并确认远端指针；代码主线不再改动。",
            extractThinkingTitle(text),
        )
    }

    @Test
    fun `extractThinkingTitle takes the leading phrase when prose follows it`() {
        assertEquals(
            "Clarifying the scope first…",
            extractThinkingTitle("**Clarifying the scope first…** Before listing files I would pin down the root."),
        )
    }

    @Test
    fun `extractThinkingTitle ignores inline emphasis`() {
        // Line does not LEAD with bold, so it is prose, not an action label.
        assertNull(extractThinkingTitle("This is **emphasis** inside a sentence.\nSecond line, still prose."))
    }

    @Test
    fun `extractThinkingTitle ignores a phrase inside a fenced code block`() {
        // The window's tail sits inside an unterminated fence → refuse rather
        // than pull `code` lines out as a title.
        assertNull(extractThinkingTitle("**old phrase**\n```\nshell **glob** here\n"))
    }

    @Test
    fun `extractThinkingTitle skips an over-long bold paragraph and keeps scanning`() {
        val long = "**" + "x".repeat(THINKING_TITLE_MAX_CHARS + 1) + "**"
        assertEquals("短标题", extractThinkingTitle("**短标题**\n$long\n"))
    }

    @Test
    fun `extractThinkingTitle returns null without any bold`() {
        assertNull(extractThinkingTitle("plain reasoning text\nno bold anywhere\n"))
    }

    @Test
    fun `extractThinkingTitle returns null for empty input`() {
        assertNull(extractThinkingTitle(""))
    }

    @Test
    fun `extractThinkingTitle ignores a phrase older than the scan budget`() {
        // Documents the tail-window ceiling: cost per call must stay O(budget)
        // because a live block re-runs this on every content-length change.
        val text = "**很久以前的动作**\n" + "y".repeat(THINKING_TITLE_SCAN_BUDGET + 50)
        assertNull(extractThinkingTitle(text))
    }

    @Test
    fun `extractThinkingTitle never fabricates a title from a partially scanned line`() {
        // The scan window starts EXACTLY at a bold span, while in the real line
        // that span is inline — the z-run in front of it was cut away by the
        // window. Without the fragment guard the leading `**` of the fragment
        // reads as a line-leading action label and gets reported as a title.
        val text = "z".repeat(1000) + "**界内片段**" + "\n" + "w".repeat(THINKING_TITLE_SCAN_BUDGET - 9)
        assertNull(extractThinkingTitle(text))
    }
}
package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1023 ISSUE-07] Pins the pure half of
 * `SessionMemorySheet.buildAutoInjectedItems`.
 *
 * ## The bug being pinned
 *
 * `autoItems` was `remember(memoryRepository, context) { … }` — both keys are
 * stable for the life of the sheet, so the list (including the FULL cached
 * content of every auto-injected file) was computed once per sheet open and
 * never recomputed, while three flows inside the same sheet rewrite exactly
 * those files: AutoFile save, Write-record save, Write-record revoke. After a
 * save the list kept showing the old line count, and re-opening a file fed
 * the STALE cached content into the editor — so the next save spliced old
 * text over the file the user had just fixed.
 *
 * The fix adds an `autoItemsRefreshTick` remember key bumped on every
 * successful mutation. That tick is only worth anything if the rebuilt item
 * really carries the NEW text/count — which is what `autoItemFor` /
 * `dailyAutoItemFor` produce. These tests pin that contract with literal
 * expectations; they double as the mutation target for the "refresh tick
 * wiring exists in source" half, which lives in the scanner gate
 * (`memory_ui_wiring_check.py`) because Compose remember semantics cannot be
 * instantiated on the JVM.
 *
 * Expected strings are written as LITERALS, not derived from production
 * constants — a mutation that changes the format string must show up as a
 * red test, not as a still-green one.
 */
class SessionMemoryAutoItemsTest {

    // ------------------------------------------------------------------
    // autoItemFor — the always-present files (SOUL.md / GLOBAL.md)
    // ------------------------------------------------------------------

    @Test
    fun nonBlankContentBecomesFullItemWithLiteralLineCount() {
        val item = autoItemFor(
            fileName = "GLOBAL.md",
            displayName = "GLOBAL.md",
            content = "line one\nline two\nline three",
            emptyLabel = "(empty)",
        )
        assertEquals("GLOBAL.md", item.name)
        assertEquals("GLOBAL.md", item.fileName)
        // literal: 3 lines -> "3 lines (full)"
        assertEquals("3 lines (full)", item.detail)
        assertEquals("line one\nline two\nline three", item.content)
    }

    @Test
    fun blankContentBecomesEmptyItemAndKeepsEmptyLabel() {
        // "" and blank-only content are BOTH the empty case (isNotBlank gate),
        // and the empty label from resources is preserved verbatim.
        for (blank in listOf("", "   \n  \t")) {
            val item = autoItemFor(
                fileName = "SOUL.md",
                displayName = "SOUL.md",
                content = blank,
                emptyLabel = "(empty file)",
            )
            assertEquals("SOUL.md", item.name)
            assertEquals("(empty file)", item.detail)
            assertEquals("", item.content)
        }
    }

    @Test
    fun rebuiltItemReflectsNewContentNotTheOldSnapshot() {
        // The ISSUE-07 regression: after a save, the item shown by the list
        // must carry the new bytes. Old snapshot "a\nb" (2 lines) -> saved
        // "a\nb\nc\nd" (4 lines): the rebuilt item shows the new count AND
        // the new cached content that the next editor open will use.
        val before = autoItemFor("GLOBAL.md", "GLOBAL.md", "a\nb", "(empty)")
        val after = autoItemFor("GLOBAL.md", "GLOBAL.md", "a\nb\nc\nd", "(empty)")
        assertEquals("2 lines (full)", before.detail)
        assertEquals("4 lines (full)", after.detail)
        assertEquals("a\nb\nc\nd", after.content)
        assertNotEquals(before.detail, after.detail)
        assertNotEquals(before.content, after.content)
    }

    @Test
    fun trailingNewlineCountsAsLineSeparatorOnly() {
        // "a\nb\n".lines() == ["a","b",""] -> 3. Pinned so a Kotlin stdlib
        // behaviour change cannot silently shift the shown count.
        val item = autoItemFor("SOUL.md", "SOUL.md", "a\nb\n", "(empty)")
        assertEquals("3 lines (full)", item.detail)
    }

    // ------------------------------------------------------------------
    // dailyAutoItemFor — today / yesterday logs
    // ------------------------------------------------------------------

    @Test
    fun dailyBlankContentYieldsNullItem() {
        assertNull(dailyAutoItemFor("2026-10-17.md", "Today — 2026-10-17.md", ""))
        assertNull(dailyAutoItemFor("2026-10-16.md", "Yesterday — 2026-10-16.md", "  \n\t"))
    }

    @Test
    fun dailyShortLogShowsFullLineCount() {
        val item = dailyAutoItemFor(
            "2026-10-17.md",
            "Today — 2026-10-17.md",
            "alpha\nbeta",
        )
        assertNotNull(item)
        assertEquals("Today — 2026-10-17.md", item!!.name)
        assertEquals("2026-10-17.md", item.fileName)
        assertEquals("2 lines (full)", item.detail)
        assertEquals("alpha\nbeta", item.content)
    }

    @Test
    fun dailyLongLogShowsInjectionCapFraction() {
        // Exactly 200 lines is still "(full)" — the cap only applies when
        // lineCount > 200. 201 lines -> "200/201 lines injected".
        val exactly200 = (1..200).joinToString("\n") { "line$it" }
        val exactly201 = (1..201).joinToString("\n") { "line$it" }
        assertEquals("200 lines (full)", dailyAutoItemFor("d.md", "d", exactly200)!!.detail)
        assertEquals("200/201 lines injected", dailyAutoItemFor("d.md", "d", exactly201)!!.detail)
    }

    @Test
    fun dailyCapFollowsExplicitInjectLines() {
        // [dv16-B08] The cap is the user-tunable injection budget, not a
        // hard-coded literal: an explicit maxInjectLines must flow into both
        // the fraction and the (full) branch. With the old hard-coded 200 the
        // 150-line log at cap 120 showed "150 lines (full)" — a lie.
        val lines150 = (1..150).joinToString("\n") { "line$it" }
        val lines300 = (1..300).joinToString("\n") { "line$it" }
        assertEquals(
            "120/150 lines injected",
            dailyAutoItemFor("d.md", "d", lines150, maxInjectLines = 120)!!.detail)
        assertEquals(
            "300 lines (full)",
            dailyAutoItemFor("d.md", "d", lines300, maxInjectLines = 500)!!.detail)
    }

    @Test
    fun dailyExplicitCapEqualToLineCountStaysFull() {
        // cap == lineCount is still "(full)" — only lineCount > cap truncates.
        val lines120 = (1..120).joinToString("\n") { "line$it" }
        assertEquals(
            "120 lines (full)",
            dailyAutoItemFor("d.md", "d", lines120, maxInjectLines = 120)!!.detail)
    }

    @Test
    fun dailyRebuiltItemReflectsGrownLogNotStaleCount() {
        // The stale-snapshot failure mode on the daily log: the user grows
        // today's log past the cap inside the sheet; the rebuilt item must
        // report the new fraction, not the one captured at sheet open.
        val small = (1..50).joinToString("\n") { "l$it" }
        val grown = (1..240).joinToString("\n") { "l$it" }
        assertEquals("50 lines (full)", dailyAutoItemFor("t.md", "t", small)!!.detail)
        assertEquals("200/240 lines injected", dailyAutoItemFor("t.md", "t", grown)!!.detail)
    }

    @Test
    fun autoItemRoundTripsThroughFileNameAndContent() {
        // The editor path keys off fileName and opens item.content: both must
        // survive a rebuild unchanged in identity (fileName) while content
        // tracks disk.
        val item = dailyAutoItemFor("2026-10-17.md", "Today — 2026-10-17.md", "x\ny\nz")
        assertEquals("2026-10-17.md", item!!.fileName)
        assertTrue(item.content.contains("x\ny\nz"))
    }
}

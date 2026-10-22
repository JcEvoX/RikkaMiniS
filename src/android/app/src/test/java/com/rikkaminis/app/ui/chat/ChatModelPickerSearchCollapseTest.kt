package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [deep-audit-1041 B-04] Searching in the chat model picker must actually show
 * the hits.
 *
 * `collapsedInstanceIds` seeds to every enabled instance id, i.e. every provider
 * section starts folded, showing one summary row (`selectedEntry ?: firstOrNull`)
 * plus a count. The section read used to ignore the search box entirely, so a
 * query that matched five models in one provider still rendered exactly one line
 * — and when the already-selected model also matched the query, the summary row
 * preferred it over the rank-1 hit, so the user saw the model they already had
 * and concluded the target did not exist. The shared `ModelEntryPicker` has
 * always forced its sections open while searching; this sheet kept a private
 * copy of the state and the private copy missed that clause.
 *
 * The repair is derived at the read site — `searchText.isEmpty() && …` — and
 * never written back into the set, so clearing the query restores the user's own
 * expand/collapse choices byte for byte.
 *
 * These are shape assertions by necessity (a Composable render loop cannot be
 * driven from a JVM unit test); each arm names the property it pins, and each
 * was verified red against the pre-fix text (the L3 probe in this branch's
 * report) so the probe cannot quietly stop looking.
 */
class ChatModelPickerSearchCollapseTest {

    private val sheetRel =
        "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatModelPickerSheet.kt"

    /**
     * Gradle runs unit tests with `user.dir` = the module dir (`src/android/app`)
     * while a local JVM run usually starts at the repo root, so resolve by
     * walking up until the path exists (the idiom `MediaHostSourceKeyTest`
     * established).
     */
    private fun repoFile(rel: String): File {
        var dir: File? = File(System.getProperty("user.dir"))
        while (dir != null) {
            val f = File(dir, rel)
            if (f.exists()) return f
            dir = dir.parentFile
        }
        throw AssertionError(
            "$rel not found from user.dir=${System.getProperty("user.dir")}"
        )
    }

    private fun sheet(): String = repoFile(sheetRel).readText()

    /** The expression after `val isCollapsed =` up to the end of the statement. */
    private fun collapseDecision(code: String): String {
        val at = code.indexOf("val isCollapsed =")
        assertTrue("the picker has no `val isCollapsed =` section decision", at >= 0)
        val lineEnd = code.indexOf('\n', at)
        assertTrue("`val isCollapsed =` has no expression", lineEnd > at)
        return code.substring(at + "val isCollapsed =".length, lineEnd).trim()
    }

    @Test
    fun `a query forces every provider section open`() {
        val decision = collapseDecision(sheet())
        assertTrue(
            "the section collapse read must consult the search box — without the clause a " +
                "matching provider stays folded and hides every hit past the first: $decision",
            decision.contains("searchText.isEmpty()"),
        )
        assertTrue(
            "the empty-query test must GATE the collapse set (conjoined), not merely appear " +
                "next to it: $decision",
            decision.contains("&&"),
        )
        assertTrue(
            "the gate must sit on the collapse lookup itself: $decision",
            decision.contains("collapsedInstanceIds"),
        )
    }

    @Test
    fun `the collapse set is never rewritten by the search, so clearing it restores the sections`() {
        val code = sheet()
        val assigns = Regex("""collapsedInstanceIds\s*=(?!=)""").findAll(code).toList()
        assertEquals(
            "exactly one assignment site expected (the chevron toggle); a second one usually " +
                "means the search cleared the set instead of gating the read: " +
                assigns.map { code.substring(it.range.first, minOf(code.length, it.range.first + 80)) },
            1,
            assigns.size,
        )
        val assignment = code.substring(
            assigns[0].range.first,
            minOf(code.length, assigns[0].range.first + 240),
        )
        assertTrue(
            "the toggle must branch on `isCollapsed` (the gated read), not on the raw set: " +
                assignment,
            assignment.contains("if (isCollapsed)"),
        )
        assertTrue(
            "the search must never write into the collapse set — otherwise clearing the query " +
                "leaves every section permanently expanded: $assignment",
            !assignment.contains("searchText"),
        )
    }

    @Test
    fun `the collapsed summary is only reachable while actually collapsed`() {
        val code = sheet()
        val branch = code.indexOf("if (isCollapsed) {")
        assertTrue(
            "the collapsed summary branch must still exist (`if (isCollapsed) {`) — the " +
                "expanded path renders the entry list instead",
            branch >= 0,
        )
        val summary = code.indexOf("val selectedEntry = entries.firstOrNull { it.id == activeEntryId")
        assertTrue("the collapsed summary row is gone", summary >= 0)
        assertTrue(
            "the summary row must live INSIDE the collapsed branch: while a query is active " +
                "`isCollapsed` is false, so the selected-entry bias cannot steal a hit",
            summary > branch,
        )
    }

    @Test
    fun `without a query the sections still start collapsed`() {
        val code = sheet()
        assertTrue(
            "the default must stay `all sections collapsed` — the seed has to keep deriving " +
                "from the enabled instance set",
            code.contains("var collapsedInstanceIds by remember(allInstanceIds)"),
        )
        assertTrue(
            "the gate must read the query's EMPTINESS; inverting it folds every section while " +
                "searching: ${collapseDecision(code)}",
            !collapseDecision(code).contains("searchText.isNotEmpty()"),
        )
    }
}

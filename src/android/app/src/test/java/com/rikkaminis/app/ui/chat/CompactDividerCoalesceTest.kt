package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/da1016-chat-sysinfo] 8c — the compact-divider predicate must read the
 * coalesced row as a whole.
 *
 * `appendSystemInfo` merges every same-`iconKind` call landing inside the
 * 200 ms `SYSINFO_COALESCE_WINDOW_MS` window into ONE ChatMessage whose
 * `toolBlocks` accumulate in call order, and the resulting message keeps the
 * FIRST block's id. The divider is identified by "compact block WITH payload"
 * — but the payload block can therefore sit at any index. The old
 * `toolBlocks.firstOrNull()` shape reported false for a coalesced row, which
 * silently disabled both of its consumers:
 * `applyCompactGreyedRange` / `neutralizeCompactArtifacts` (patch 8c) and
 * `revertCompact` (ChatViewModel) stopped recognising the row.
 *
 * Runs against the REAL predicate (the app's own `internal` extension), not a
 * copy of its expression — a copied expression would pass even after the
 * production source drifted back.
 */
class CompactDividerCoalesceTest {

    private fun block(
        id: String,
        toolName: String,
        payload: String = "",
    ): AssistantBlock = AssistantBlock(
        id = id,
        kind = "info",
        content = "text of $id",
        toolName = toolName,
        toolArgs = payload,
    )

    private fun systemRow(id: String, vararg blocks: AssistantBlock): ChatMessage =
        ChatMessage(id = id, role = "system", content = "", toolBlocks = blocks.toList())

    private fun plain(id: String, role: String): ChatMessage =
        ChatMessage(id = id, role = role, content = "body of $id")

    /** The divider card as `appendSystemInfo(..., "compact", payload = summary)` builds it. */
    private fun divider(id: String) = systemRow(id, block("$id-b1", "compact", "SUMMARY"))

    /** A `iconKind = "compact"` notice with no payload (trim / context-full / failure). */
    private fun compactNotice(id: String) = systemRow(id, block("$id-b1", "compact"))

    /**
     * The coalesced shape: an earlier "compact" notice and the divider landed
     * in the same window, so the payload block is NOT at index 0.
     */
    private fun coalescedNoticeThenDivider(id: String) = systemRow(
        id,
        block("$id-b1", "compact"),
        block("$id-b2", "compact", "SUMMARY"),
    )

    // ── predicate: single-block rows (unchanged behaviour) ──────────

    @Test
    fun `single compact block carrying a payload is a divider`() {
        assertTrue(divider("d1").isCompactDividerRow())
    }

    @Test
    fun `single compact block without a payload is not a divider`() {
        assertFalse(compactNotice("n1").isCompactDividerRow())
    }

    @Test
    fun `a system row with no blocks is not a divider`() {
        assertFalse(systemRow("empty").isCompactDividerRow())
    }

    // ── predicate: coalesced rows (the 8c differential arms) ────────

    @Test
    fun `coalesced row whose LAST block carries the payload is a divider`() {
        // The differential arm: pre-fix this assertion FAILS (firstOrNull()
        // sees the payload-less notice block at index 0).
        assertTrue(coalescedNoticeThenDivider("c1").isCompactDividerRow())
    }

    @Test
    fun `coalesced row with several notices before the divider is a divider`() {
        val row = systemRow(
            "c2",
            block("c2-b1", "compact"),
            block("c2-b2", "compact"),
            block("c2-b3", "compact", "SUMMARY"),
        )
        assertTrue(row.isCompactDividerRow())
    }

    @Test
    fun `coalesced row with no payload anywhere is not a divider`() {
        val row = systemRow(
            "c3",
            block("c3-b1", "compact"),
            block("c3-b2", "compact"),
        )
        assertFalse(row.isCompactDividerRow())
    }

    @Test
    fun `a payload on a NON-compact block does not make a divider`() {
        // Guards the cheap mis-implementation `any { it.toolArgs.isNotEmpty() }`:
        // the payload is only meaningful on a "compact" block.
        val row = systemRow(
            "c4",
            block("c4-b1", "compact"),
            block("c4-b2", "info", "some detail"),
        )
        assertFalse(row.isCompactDividerRow())
    }

    @Test
    fun `the role gate still wins over a payload-bearing compact block`() {
        val userRow = ChatMessage(
            id = "u1",
            role = "user",
            content = "hi",
            toolBlocks = listOf(block("u1-b1", "compact", "SUMMARY")),
        )
        val assistantRow = ChatMessage(
            id = "a1",
            role = "assistant",
            content = "hi",
            toolBlocks = listOf(block("a1-b1", "compact", "SUMMARY")),
        )
        assertFalse(userRow.isCompactDividerRow())
        assertFalse(assistantRow.isCompactDividerRow())
    }

    // ── consumers: the two call sites the predicate feeds ───────────

    @Test
    fun `neutralizeCompactArtifacts drops a coalesced divider row`() {
        val h = listOf(
            plain("u1", "user"),
            coalescedNoticeThenDivider("c1"),
            plain("a1", "assistant"),
        )
        val out = neutralizeCompactArtifacts(h)
        // Pre-fix the coalesced row survived.
        assertEquals(listOf("u1", "a1"), out.map { it.id })
    }

    @Test
    fun `neutralizeCompactArtifacts keeps the payload-less notice row`() {
        // A row whose compact blocks carry no payload is a notice, not a card:
        // it must stay (message-level strip cannot split a merged row).
        val h = listOf(
            plain("u1", "user"),
            systemRow("c2", block("c2-b1", "compact"), block("c2-b2", "compact")),
        )
        val out = neutralizeCompactArtifacts(h)
        assertEquals(listOf("u1", "c2"), out.map { it.id })
    }

    @Test
    fun `applyCompactGreyedRange drops a coalesced divider row and greys the folded tail`() {
        val h = listOf(
            plain("u1", "user"),
            coalescedNoticeThenDivider("c1"),
            plain("a1", "assistant"),
            plain("u2", "user"),
        )
        val out = applyCompactGreyedRange(h, "a1")
        // The divider row is stripped (pre-fix it stayed in the list, so the
        // transcript kept a stale card next to the freshly re-added one)...
        assertEquals(listOf("u1", "a1", "u2"), out.map { it.id })
        // ...and everything up to and including the anchor row is greyed.
        assertEquals(listOf(true, true, false), out.map { it.isCompactedHistory })
    }
}

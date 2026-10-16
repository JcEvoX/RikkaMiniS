package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/da1018-compact-pin] Truth-table tests: the two formerly hand-rolled
 * "tool-result carrier" tests in the compaction domain now resolve EXACTLY
 * like [isToolResultOnly] — including the empty-`contentParts` row, where
 * both old inline forms (`any { !ToolResult }` and `!all { ToolResult }`)
 * evaluated vacuously the WRONG way and misclassified a plain-text user
 * prompt as a tool-result carrier.
 *
 * Truth table (the three shapes that matter):
 *
 *  shape                        | isToolResultOnly | old :124 `any` | old :449 `!all`
 *  -----------------------------|------------------|----------------|----------------
 *  contentParts = []            | false            | false (WRONG)  | false (WRONG)
 *  all parts are ToolResult     | true             | true           | true
 *  mixed / Text present         | false            | true           | true
 *
 * Post-fix both sites are `!isToolResultOnly()`, so every row matches the
 * single truth source.
 */
class ChatCompactionLogicTruthTableTest {

    private fun plainTextUser(id: String? = null) =
        LLMMessage(role = LLMMessage.Role.USER, content = "typed prompt", dbMessageId = id)

    private fun assistant(id: String? = "a") = LLMMessage(
        role = LLMMessage.Role.ASSISTANT,
        content = "ok",
        contentParts = listOf(AgentContentPart.Text("ok")),
        dbMessageId = id,
    )

    private fun toolResultUser(id: String? = null) = LLMMessage(
        role = LLMMessage.Role.USER,
        content = "",
        contentParts = listOf(AgentContentPart.ToolResult("t", "n", "r")),
        dbMessageId = id,
    )

    private fun doubleFilledUser(id: String? = null) = LLMMessage(
        role = LLMMessage.Role.USER,
        content = "typed prompt",
        contentParts = listOf(AgentContentPart.Text("typed prompt")),
        dbMessageId = id,
    )

    // ── :124 — resolveCompactAnchorIdx sole-prompt guard ────────
    // Semantic intent: "history[0] is a sole unanswered user prompt with
    // visible content → compacting it would swallow the only instruction;
    // abort". The old `any` form was false on the empty-parts shape, so the
    // MOST COMMON prompt shape did NOT abort and could be compacted away.

    @Test
    fun `sole plain text prompt aborts compaction - anchor minus one`() {
        // Pre-fix: `any` on empty contentParts → false → guard failed →
        // anchor 0 → the only instruction was compacted. Now aborts.
        val h = listOf(plainTextUser("u1"))
        assertEquals(-1, resolveCompactAnchorIdx(h, null))
    }

    @Test
    fun `sole double filled prompt aborts compaction - anchor minus one`() {
        val h = listOf(doubleFilledUser("u1"))
        assertEquals(-1, resolveCompactAnchorIdx(h, null))
    }

    @Test
    fun `sole tool result carrier does not abort`() {
        // Tool-result carrier is NOT a user instruction; the sole-prompt
        // guard must not fire for it (unchanged behaviour).
        val h = listOf(toolResultUser("u1"))
        assertEquals(0, resolveCompactAnchorIdx(h, null))
    }

    @Test
    fun `plain prompt answered by an assistant does not take the abort branch`() {
        // The abort branch is for a SOLE unanswered prompt only; an assistant
        // turn after it keeps the guard's `drop(1).none { ASSISTANT }` false.
        val h = listOf(plainTextUser("u1"), assistant("a1"))
        assertEquals(0, resolveCompactAnchorIdx(h, null))
    }

    // ── :449 — injectSummaryIntoPostAnchor injection target ─────
    // Semantic intent: "inject the summary into the first post-anchor USER
    // message that is not a tool-result carrier". The old `!all` form was
    // false on the empty-parts shape, so the plain instruction never got
    // the summary inlined (it landed as a standalone head instead).

    private val wrapped = "<context-summary>sum</context-summary>"

    @Test
    fun `summary inlines into the plain text instruction`() {
        // Pre-fix: `!all` on empty contentParts → false → target skipped →
        // standalone head. Now the plain prompt IS the target.
        val out = injectSummaryIntoPostAnchor(listOf(plainTextUser("u2")), wrapped)
        assertEquals(1, out.size)
        val target = out[0]
        assertTrue(target.content.startsWith(wrapped))
        // A message with NO Text part gets a leading Text part carrying the
        // summary (serialization reads contentParts when present); the typed
        // text stays only in `content`.
        val textParts = target.contentParts.filterIsInstance<AgentContentPart.Text>()
        assertEquals(listOf(wrapped), textParts.map { it.text })
    }

    @Test
    fun `summary skips tool result carriers and inlines into the next user turn`() {
        val out = injectSummaryIntoPostAnchor(
            listOf(
                toolResultUser("r1"),
                plainTextUser("u2"),
            ),
            wrapped,
        )
        assertEquals(2, out.size)
        // r1 untouched
        assertTrue(out[0].contentParts.all { it is AgentContentPart.ToolResult })
        // u2 got the summary
        assertTrue(out[1].content.startsWith(wrapped))
    }

    @Test
    fun `all tool result carriers falls back to a standalone summary head`() {
        val out = injectSummaryIntoPostAnchor(listOf(toolResultUser("r1")), wrapped)
        assertEquals(2, out.size)
        assertEquals(LLMMessage.Role.USER, out[1].role)
        assertEquals(wrapped, out[1].content)
        // standalone head is double-filled too (serialization reads parts)
        assertEquals(
            listOf(wrapped),
            out[1].contentParts.filterIsInstance<AgentContentPart.Text>().map { it.text },
        )
    }

    @Test
    fun `injected summary precedes earlier user content without losing it`() {
        val out = injectSummaryIntoPostAnchor(
            listOf(doubleFilledUser("u2")),
            wrapped,
        )
        assertEquals(wrapped + "\n\ntyped prompt", out[0].content)
        assertEquals(
            listOf(wrapped + "\n\ntyped prompt"),
            out[0].contentParts.filterIsInstance<AgentContentPart.Text>().map { it.text },
        )
    }

    // ── truth-source alignment (both sites) ─────────────────────

    @Test
    fun `isToolResultOnly truth table matches both reworked sites`() {
        // Row-by-row: empty / all-ToolResult / Text-present.
        // (a) empty parts
        assertFalse(plainTextUser().isToolResultOnly())
        // (b) pure tool-result carrier
        assertTrue(toolResultUser().isToolResultOnly())
        // (c) mixed — Text present alongside a ToolResult
        val mixed = LLMMessage(
            role = LLMMessage.Role.USER,
            content = "typed",
            contentParts = listOf(
                AgentContentPart.Text("typed"),
                AgentContentPart.ToolResult("t", "n", "r"),
            ),
        )
        assertFalse(mixed.isToolResultOnly())
        // Both reworked sites consume the same predicate, so these rows ARE
        // the sites' rows — verified behaviourally by the tests above.
    }
}

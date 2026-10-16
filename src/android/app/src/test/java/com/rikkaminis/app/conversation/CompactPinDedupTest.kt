package com.rikkaminis.app.conversation

import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/da1018-compact-pin] JVM tests for the same-message de-duplication in
 * [extractPinnedUserMessages].
 *
 * Production USER messages are DOUBLE-FILLED — the same text lives in both
 * `content` and the first [AgentContentPart.Text] (ChatViewModel send path
 * 3713-3714, plus every engine-side reminder: AgentLoopEngine, ChatToolExecutors,
 * EmptyTurnRecovery, SanitizeAgentHistory). The pre-fix collector read BOTH
 * copies into the pin list, so every user line was pinned twice: the
 * [PINNED_USER_MESSAGES_TOKEN_LIMIT] budget was consumed at 2x the real
 * content (the over-cap walk dropped older real messages early) and the
 * summary string that rides every request carried the text twice.
 *
 * The fix de-duplicates WITHIN a message only: distinct parts (captions,
 * `[image …]` placeholders, attached-files XML) are still pinned, insertion
 * order is preserved, and identical text in two DIFFERENT messages is still
 * pinned twice (the pin stays a chronological record, not a global set).
 */
class CompactPinDedupTest {

    private fun user(content: String, parts: List<AgentContentPart> = emptyList()) =
        LLMMessage(role = LLMMessage.Role.USER, content = content, contentParts = parts)

    private fun assistant(content: String) =
        LLMMessage(role = LLMMessage.Role.ASSISTANT, content = content)

    private fun toolResultUser() = LLMMessage(
        role = LLMMessage.Role.USER,
        content = "",
        contentParts = listOf(AgentContentPart.ToolResult("t", "n", "r")),
    )

    // 1) Double-filled (content == the only Text part) → pinned ONCE.
    @Test
    fun `double filled user message is pinned once`() {
        val msgs = listOf(
            user("deploy the staging branch", listOf(AgentContentPart.Text("deploy the staging branch"))),
            assistant("ok"),
        )
        assertEquals(
            listOf("deploy the staging branch"),
            extractPinnedUserMessages(msgs, PINNED_USER_MESSAGES_TOKEN_LIMIT),
        )
    }

    // 2) content != the part text → the DISTINCT texts are both pinned.
    @Test
    fun `distinct part text is pinned alongside content`() {
        val caption = "[attached image: /var/minis/attachments/uploads/x.png]"
        val msgs = listOf(
            user("hi", listOf(AgentContentPart.Text("hi"), AgentContentPart.Text(caption))),
        )
        assertEquals(listOf("hi", caption), extractPinnedUserMessages(msgs))
    }

    // 3) Over the cap → newest-first trim + earliest pinned unconditionally.
    //    With de-dup the 3 distinct texts (300 tok) fit differently than the
    //    6 pre-fix copies (600 tok): pre-fix the walk stopped after `late`,
    //    dropping `mid` that would have fit.
    @Test
    fun `over cap keeps newest that fit plus the earliest, on the deduped budget`() {
        val early = "E".repeat(400) // 100 tokens
        val mid = "M".repeat(400)   // 100 tokens
        val late = "L".repeat(400)  // 100 tokens
        val msgs = listOf(
            user(early, listOf(AgentContentPart.Text(early))),
            user(mid, listOf(AgentContentPart.Text(mid))),
            user(late, listOf(AgentContentPart.Text(late))),
        )
        assertEquals(listOf(early, mid, late), extractPinnedUserMessages(msgs, 250L))
    }

    @Test
    fun `single message larger than the cap is still pinned alone`() {
        val huge = "H".repeat(4000) // 1000 tokens
        assertEquals(listOf(huge), extractPinnedUserMessages(listOf(user(huge)), 500L))
    }

    // 4) Pure tool-result message is skipped (unchanged semantics).
    @Test
    fun `pure tool result user message is skipped`() {
        val msgs = listOf(user("real"), toolResultUser())
        assertEquals(listOf("real"), extractPinnedUserMessages(msgs))
    }

    // 5) content empty but parts carry text → collect from parts.
    @Test
    fun `content empty collects the text parts`() {
        val msgs = listOf(user("", listOf(AgentContentPart.Text("from part"))))
        assertEquals(listOf("from part"), extractPinnedUserMessages(msgs))
    }

    // 6) Empty input → empty list.
    @Test
    fun `empty input yields an empty list`() {
        assertTrue(extractPinnedUserMessages(emptyList()).isEmpty())
    }

    // Boundary of the de-dup scope: identical text in two messages is kept.
    @Test
    fun `same text in two different messages is kept twice`() {
        val msgs = listOf(user("repeat"), assistant("a"), user("repeat"))
        assertEquals(listOf("repeat", "repeat"), extractPinnedUserMessages(msgs))
    }

    // Duplicate Text parts inside one message also collapse.
    @Test
    fun `duplicate text parts collapse within one message`() {
        val msgs = listOf(user("", listOf(AgentContentPart.Text("same"), AgentContentPart.Text("same"))))
        assertEquals(listOf("same"), extractPinnedUserMessages(msgs))
    }
}

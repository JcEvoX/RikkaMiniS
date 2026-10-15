package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM tests for the [fix/audit-1015] context-linkage fixes:
 *  - [injectSummaryIntoPostAnchor] (BUG-01: summary injected into content
 *    only was 100% silently dropped by serialization, which reads
 *    contentParts when present),
 *  - [buildConversationTextForSummary] (BUG-01B: double-filled user
 *    messages reached the summarizer twice),
 *  - [sanitizeAgentHistoryMessagesImpl] (ARCH-03: placeholder tool_results
 *    must precede text within the same user message).
 *
 * Expectations are hard-coded literals (ground truth per evidence
 * discipline), never derived from the implementation under test.
 */
class Audit1015ContextFixTest {

    private val SUMMARY = "<context-summary>Earlier chat condensed.</context-summary>"

    // ── injectSummaryIntoPostAnchor ─────────────────────────────

    @Test
    fun `inject into target with contentParts updates both content and parts`() {
        val postAnchor = listOf(
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "current question",
                contentParts = listOf(AgentContentPart.Text("current question")),
            ),
        )
        val out = injectSummaryIntoPostAnchor(postAnchor, SUMMARY)
        assertEquals(1, out.size)
        val injected = out[0]
        // content carries the summary
        assertTrue(injected.content.startsWith("<context-summary>Earlier chat condensed.</context-summary>\n\ncurrent question"))
        // contentParts carry the summary too — the literal serialization gate
        assertEquals(1, injected.contentParts.size)
        val part = injected.contentParts[0] as AgentContentPart.Text
        assertEquals(
            "<context-summary>Earlier chat condensed.</context-summary>\n\ncurrent question",
            part.text,
        )
    }

    @Test
    fun `multi Text-part target takes summary on FIRST part only`() {
        // [fix/prewindow-budget-1015] A user message can carry SEVERAL Text
        // parts (typed text + per-image captions + attached-files XML).
        // Serialization emits every part in order, so prepending the summary
        // to each Text part duplicated it once per part. Only the FIRST
        // Text part takes it; later parts and non-Text parts stay verbatim.
        val postAnchor = listOf(
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "current question",
                contentParts = listOf(
                    AgentContentPart.Text("current question"),
                    AgentContentPart.ImageData(ByteArray(1), "image/png"),
                    AgentContentPart.Text("caption for image"),
                ),
            ),
        )
        val out = injectSummaryIntoPostAnchor(postAnchor, SUMMARY)
        assertEquals(1, out.size)
        val injected = out[0]
        assertEquals(3, injected.contentParts.size)
        val first = injected.contentParts[0] as AgentContentPart.Text
        assertEquals(
            "<context-summary>Earlier chat condensed.</context-summary>\n\ncurrent question",
            first.text,
        )
        // Second Text part must NOT carry the summary (old map() behavior
        // duplicated it here — the exact regression this pins).
        val third = injected.contentParts[2] as AgentContentPart.Text
        assertEquals("caption for image", third.text)
        assertTrue(injected.contentParts[1] is AgentContentPart.ImageData)
        // content field still gets the summary exactly once (serialization
        // ignores it when parts are present, but the double-fill contract
        // keeps it in sync for part-less exits).
        assertTrue(injected.content.startsWith(
            "<context-summary>Earlier chat condensed.</context-summary>\n\ncurrent question",
        ))
        assertEquals(1, injected.content.split("<context-summary>").size - 1)
    }

    @Test
    fun `inject into target without Text part inserts leading Text part`() {
        // A user message carrying an ImageData part but no Text part.
        val postAnchor = listOf(
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "look at this",
                contentParts = listOf(
                    AgentContentPart.ImageData(ByteArray(1), "image/png"),
                ),
            ),
        )
        val out = injectSummaryIntoPostAnchor(postAnchor, SUMMARY)
        val injected = out[0]
        assertEquals(2, injected.contentParts.size)
        val head = injected.contentParts[0] as AgentContentPart.Text
        assertEquals(SUMMARY, head.text)
        assertTrue(injected.contentParts[1] is AgentContentPart.ImageData)
        assertTrue(injected.content.startsWith(SUMMARY + "\n\nlook at this"))
    }

    @Test
    fun `no post-anchor user message appends standalone summary turn with Text part`() {
        val out = injectSummaryIntoPostAnchor(emptyList(), SUMMARY)
        assertEquals(1, out.size)
        val turn = out[0]
        assertEquals(LLMMessage.Role.USER, turn.role)
        assertEquals(SUMMARY, turn.content)
        assertEquals(1, turn.contentParts.size)
        assertEquals(SUMMARY, (turn.contentParts[0] as AgentContentPart.Text).text)
    }

    @Test
    fun `tool_result-only user message is skipped as injection target`() {
        val postAnchor = listOf(
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "",
                contentParts = listOf(
                    AgentContentPart.ToolResult("t1", "run", "ok"),
                ),
            ),
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "real follow-up",
                contentParts = listOf(AgentContentPart.Text("real follow-up")),
            ),
        )
        val out = injectSummaryIntoPostAnchor(postAnchor, SUMMARY)
        assertEquals(2, out.size)
        // first message untouched (still tool_result-only)
        assertTrue(out[0].contentParts[0] is AgentContentPart.ToolResult)
        // summary landed on the second message
        val part = out[1].contentParts[0] as AgentContentPart.Text
        assertEquals(
            "<context-summary>Earlier chat condensed.</context-summary>\n\nreal follow-up",
            part.text,
        )
    }

    @Test
    fun `messages before injection target are preserved`() {
        val postAnchor = listOf(
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "",
                contentParts = listOf(
                    AgentContentPart.ToolResult("t1", "run", "ok"),
                ),
            ),
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "q",
                contentParts = listOf(AgentContentPart.Text("q")),
            ),
        )
        val out = injectSummaryIntoPostAnchor(postAnchor, SUMMARY)
        assertEquals(2, out.size)
        assertTrue(out[0].contentParts[0] is AgentContentPart.ToolResult)
        assertEquals("q", (out[1].contentParts[0] as AgentContentPart.Text).text.substringAfter("\n\n"))
    }

    // ── buildConversationTextForSummary (BUG-01B) ───────────────

    @Test
    fun `double-filled user message renders text once`() {
        val history = listOf(
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "hello there",
                contentParts = listOf(AgentContentPart.Text("hello there")),
            ),
        )
        val out = buildConversationTextForSummary(history)
        assertEquals("user: hello there\n", out)
    }

    @Test
    fun `content-only message falls back to content`() {
        val history = listOf(
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "plain text",
                contentParts = emptyList(),
            ),
        )
        val out = buildConversationTextForSummary(history)
        assertEquals("user: plain text\n", out)
    }

    @Test
    fun `mixed history keeps tool parts and emits user text once`() {
        val history = listOf(
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "run it",
                contentParts = listOf(AgentContentPart.Text("run it")),
            ),
            LLMMessage(
                role = LLMMessage.Role.ASSISTANT,
                content = "x",
                contentParts = listOf(
                    AgentContentPart.ToolUse("u1", "shell", org.json.JSONObject("""{"cmd":"ls"}""")),
                ),
            ),
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "",
                contentParts = listOf(
                    AgentContentPart.ToolResult("u1", "shell", "file.txt"),
                ),
            ),
        )
        val out = buildConversationTextForSummary(history)
        assertEquals(
            "user: run it\n" +
                "assistant: x\n" +
                "assistant [tool:shell]: {\"cmd\":\"ls\"}\n" +
                "user [result:shell]: file.txt\n",
            out,
        )
    }

    // ── sanitizeAgentHistoryMessagesImpl (ARCH-03) ──────────────

    @Test
    fun `placeholder tool_results precede existing parts in merged user message`() {
        val messages = mutableListOf(
            LLMMessage(
                role = LLMMessage.Role.ASSISTANT,
                content = "",
                contentParts = listOf(
                    AgentContentPart.ToolUse("u1", "shell", org.json.JSONObject()),
                ),
            ),
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "",
                contentParts = listOf(AgentContentPart.Text("user text")),
            ),
        )
        sanitizeAgentHistoryMessagesImpl(messages)
        val merged = messages[1]
        assertEquals(2, merged.contentParts.size)
        // tool_result FIRST, text after — Anthropic ordering requirement
        assertTrue(merged.contentParts[0] is AgentContentPart.ToolResult)
        assertTrue(merged.contentParts[1] is AgentContentPart.Text)
        assertEquals("user text", (merged.contentParts[1] as AgentContentPart.Text).text)
    }

    @Test
    fun `plain-content user neighbour is lifted into Text part`() {
        val messages = mutableListOf(
            LLMMessage(
                role = LLMMessage.Role.ASSISTANT,
                content = "",
                contentParts = listOf(
                    AgentContentPart.ToolUse("u1", "shell", org.json.JSONObject()),
                ),
            ),
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = "original text only",
                contentParts = emptyList(),
            ),
        )
        sanitizeAgentHistoryMessagesImpl(messages)
        val merged = messages[1]
        // content must be empty once lifted (serialization reads parts)
        assertEquals("", merged.content)
        assertEquals(2, merged.contentParts.size)
        assertTrue(merged.contentParts[0] is AgentContentPart.ToolResult)
        assertEquals(
            "original text only",
            (merged.contentParts[1] as AgentContentPart.Text).text,
        )
    }
}

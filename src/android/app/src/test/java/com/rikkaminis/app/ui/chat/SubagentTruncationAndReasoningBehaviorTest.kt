package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage
import com.rikkaminis.app.data.model.LLMStreamChunk
import com.rikkaminis.app.tools.SubagentSkill
import com.rikkaminis.app.tools.ToolExecutionResult
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1018 ISSUE-01 / ISSUE-02] Behavioural coverage of the sub-agent
 * loop itself — not source text.
 *
 * `runSubagentLoop` takes its provider stream and tool executor as injected
 * lambdas (ChatToolExecutorsTest established that harness), so the two P0/P1
 * defects can be executed end-to-end in plain JVM:
 *
 *  ISSUE-01: the loop used to dispatch EVERY tool call it received, whatever
 *  the turn's truncation state. A stream cut off mid-flight leaves a prefix
 *  argument object, and ToolJsonRepair Strategy 1 closes such a prefix into
 *  valid JSON — so the loop silently RAN a half-formed dangerous command.
 *  The refusal now comes from TruncatedToolCallPolicy (both truncation
 *  sources) and keeps the tool_use/tool_result pairing.
 *
 *  ISSUE-02: ThinkingDelta / ReasoningContent chunks fell into `else -> {}`
 *  and all three ASSISTANT history entries were written without
 *  reasoningContent. The observable consequence: the NEXT request's history —
 *  what the provider actually re-reads — carried no reasoning. That is
 *  exactly what these tests assert: the captured `messages` snapshots.
 */
class SubagentTruncationAndReasoningBehaviorTest {

    private fun config(maxTurns: Int = 4) = SubagentSkill.SubagentConfig(
        isSubagent = true, maxTurns = maxTurns, maxOutputTokens = 100,
    )

    private class ToolCall(val id: String, val name: String)

    private fun loop(
        turns: List<List<LLMStreamChunk>>,
        executed: MutableList<ToolCall>,
        snapshots: MutableList<List<LLMMessage>>,
        maxTurns: Int = 4,
    ): ToolExecutionResult = runBlocking {
        var call = 0
        runSubagentLoop(
            skillName = "s", query = "q", title = "t", config = config(maxTurns),
            systemPrompt = "sys",
            streamProvider = { messages ->
                snapshots.add(messages.toList())
                val chunks = turns[minOf(call, turns.lastIndex)]
                call++
                flowOf(*chunks.toTypedArray())
            },
            executeSubTool = { name, argsJson ->
                executed.add(ToolCall("id1", name))
                ToolExecutionResult("ran $name with ${argsJson}", true)
            },
            knownToolNames = listOf("file_read", "shell_execute"),
            log = {},
        )
    }

    private fun toolCall(id: String = "id1", name: String = "shell_execute") =
        LLMStreamChunk.ToolCallComplete(id, name, org.json.JSONObject())

    private fun assistantEntries(messages: List<LLMMessage>) =
        messages.filter { it.role == LLMMessage.Role.ASSISTANT }

    // ── ISSUE-01: truncated turns must not execute ─────────────────────

    @Test
    fun `ceiling-truncated turn does NOT execute the tool call`() {
        val executed = mutableListOf<ToolCall>()
        val snaps = mutableListOf<List<LLMMessage>>()
        val result = loop(
            turns = listOf(
                listOf(
                    LLMStreamChunk.Text("about to run"),
                    toolCall(),
                    LLMStreamChunk.Finished("length", truncated = false),
                ),
                listOf(LLMStreamChunk.Text("re-issued cleanly")),
            ),
            executed = executed,
            snapshots = snaps,
        )
        assertTrue(result.success)
        assertEquals(
            "a ceiling-truncated turn must not touch the executor",
            emptyList<String>(), executed.map { it.name },
        )
        // The next request must see a refusal tool_result, paired with the call.
        val second = snaps[1]
        val refusal = second.last {
            (it.contentParts.firstOrNull() as? AgentContentPart.ToolResult) != null
        }
        val part = refusal.contentParts.first() as AgentContentPart.ToolResult
        assertEquals("id1", part.id)
        assertTrue(part.isError)
        assertTrue(part.content.contains("[TRUNCATED]"))
        assertTrue(part.content.contains("was NOT executed"))
        // …and the assistant message still carries the tool_use (pairing shape).
        val assistant = assistantEntries(second).last()
        assertTrue((assistant.contentParts.firstOrNull() as? AgentContentPart.ToolUse)?.id == "id1")
    }

    @Test
    fun `EOF-truncated tool-call-only turn does NOT execute — the ISSUE-01 class`() {
        // No finish_reason at all, truncated=true: this is the shape
        // OpenAIProvider only started emitting once its EOF guard counted
        // toolCallEventCount. Before the fix the loop executed the call.
        val executed = mutableListOf<ToolCall>()
        val snaps = mutableListOf<List<LLMMessage>>()
        val result = loop(
            turns = listOf(
                listOf(toolCall(), LLMStreamChunk.Finished(null, truncated = true)),
                listOf(LLMStreamChunk.Text("ok, re-issued")),
            ),
            executed = executed,
            snapshots = snaps,
        )
        assertTrue(result.success)
        assertEquals(emptyList<String>(), executed.map { it.name })
        val refusalPart = snaps[1].last {
            (it.contentParts.firstOrNull() as? AgentContentPart.ToolResult) != null
        }.contentParts.first() as AgentContentPart.ToolResult
        assertTrue(refusalPart.content.contains("stream ended"))
    }

    @Test
    fun `clean turn still executes — the refusal is truncation-scoped`() {
        // Control arm: if this went red, the guard would be refusing everything
        // and "no execution" would prove nothing.
        val executed = mutableListOf<ToolCall>()
        val snaps = mutableListOf<List<LLMMessage>>()
        loop(
            turns = listOf(
                listOf(
                    LLMStreamChunk.Text("running"),
                    toolCall(name = "file_read"),
                    LLMStreamChunk.Finished("stop", truncated = false),
                ),
                listOf(LLMStreamChunk.Text("done")),
            ),
            executed = executed,
            snapshots = snaps,
        )
        assertEquals(listOf("file_read"), executed.map { it.name })
        val resultPart = snaps[1].last {
            (it.contentParts.firstOrNull() as? AgentContentPart.ToolResult) != null
        }.contentParts.first() as AgentContentPart.ToolResult
        assertFalse(resultPart.isError)
        assertFalse(resultPart.content.contains("[TRUNCATED]"))
    }

    @Test
    fun `after a refusal the loop continues so the model can re-issue`() {
        val executed = mutableListOf<ToolCall>()
        val snaps = mutableListOf<List<LLMMessage>>()
        val result = loop(
            turns = listOf(
                listOf(toolCall(), LLMStreamChunk.Finished("length", truncated = false)),
                listOf(
                    LLMStreamChunk.Text("now with full arguments"),
                    toolCall(id = "id2", name = "file_read"),
                    LLMStreamChunk.Finished("tool_calls", truncated = false),
                ),
                listOf(LLMStreamChunk.Text("finished")),
            ),
            executed = executed,
            snapshots = snaps,
        )
        assertTrue(result.success)
        // Only the SECOND (clean) turn's call ran; the truncated one never did.
        assertEquals(listOf("file_read"), executed.map { it.name })
        assertEquals(3, snaps.size)
    }

    // ── ISSUE-02: reasoning must reach the next request ────────────────

    @Test
    fun `thinking deltas appear in the assistant entry the next request sees`() {
        val snaps = mutableListOf<List<LLMMessage>>()
        loop(
            turns = listOf(
                listOf(
                    LLMStreamChunk.ThinkingDelta("step one "),
                    LLMStreamChunk.ThinkingDelta("step two"),
                    LLMStreamChunk.Text("running"),
                    toolCall(name = "file_read"),
                    LLMStreamChunk.Finished("tool_calls", truncated = false),
                ),
                listOf(LLMStreamChunk.Text("done")),
            ),
            executed = mutableListOf(),
            snapshots = snaps,
        )
        assertEquals("step one step two", assistantEntries(snaps[1]).last().reasoningContent)
    }

    @Test
    fun `opaque reasoning blob wins over the visible thinking`() {
        val snaps = mutableListOf<List<LLMMessage>>()
        loop(
            turns = listOf(
                listOf(
                    LLMStreamChunk.ThinkingDelta("visible"),
                    LLMStreamChunk.ReasoningContent("encrypted_reasoning"),
                    LLMStreamChunk.Text("running"),
                    toolCall(name = "file_read"),
                    LLMStreamChunk.Finished("tool_calls", truncated = false),
                ),
                listOf(LLMStreamChunk.Text("done")),
            ),
            executed = mutableListOf(),
            snapshots = snaps,
        )
        assertEquals("encrypted_reasoning", assistantEntries(snaps[1]).last().reasoningContent)
    }

    @Test
    fun `empty reasoning blob round-trips as empty string`() {
        // DeepSeek V4 non-thinking turns: "" must come back as "" — the
        // provider asked for that exact value. Baseline collapsed it to null.
        val snaps = mutableListOf<List<LLMMessage>>()
        loop(
            turns = listOf(
                listOf(
                    LLMStreamChunk.ReasoningContent(""),
                    LLMStreamChunk.Text("running"),
                    toolCall(name = "file_read"),
                    LLMStreamChunk.Finished("tool_calls", truncated = false),
                ),
                listOf(LLMStreamChunk.Text("done")),
            ),
            executed = mutableListOf(),
            snapshots = snaps,
        )
        assertEquals("", assistantEntries(snaps[1]).last().reasoningContent)
    }

    @Test
    fun `a turn with no reasoning keeps a null field`() {
        val snaps = mutableListOf<List<LLMMessage>>()
        loop(
            turns = listOf(
                listOf(
                    LLMStreamChunk.Text("running"),
                    toolCall(name = "file_read"),
                    LLMStreamChunk.Finished("tool_calls", truncated = false),
                ),
                listOf(LLMStreamChunk.Text("done")),
            ),
            executed = mutableListOf(),
            snapshots = snaps,
        )
        assertNull(assistantEntries(snaps[1]).last().reasoningContent)
    }

    @Test
    fun `reasoning never bleeds across turns`() {
        val snaps = mutableListOf<List<LLMMessage>>()
        loop(
            turns = listOf(
                listOf(
                    LLMStreamChunk.ThinkingDelta("r1"),
                    LLMStreamChunk.Text("first"),
                    toolCall(name = "file_read"),
                    LLMStreamChunk.Finished("tool_calls", truncated = false),
                ),
                listOf(
                    LLMStreamChunk.ThinkingDelta("r2"),
                    LLMStreamChunk.Text("second"),
                    toolCall(id = "id2", name = "file_read"),
                    LLMStreamChunk.Finished("tool_calls", truncated = false),
                ),
                listOf(LLMStreamChunk.Text("done")),
            ),
            executed = mutableListOf(),
            snapshots = snaps,
            maxTurns = 5,
        )
        val assistants = assistantEntries(snaps[2])
        assertEquals(2, assistants.size)
        assertEquals("r1", assistants[0].reasoningContent)
        assertEquals("r2", assistants[1].reasoningContent)
    }

    @Test
    fun `a text-only truncated continuation carries its reasoning too`() {
        // The third hand-back site: the truncation-continuation branch.
        val snaps = mutableListOf<List<LLMMessage>>()
        loop(
            turns = listOf(
                listOf(
                    LLMStreamChunk.ThinkingDelta("thinking-to-carry"),
                    LLMStreamChunk.Text("half a sentence"),
                    LLMStreamChunk.Finished("length", truncated = false),
                ),
                listOf(LLMStreamChunk.Text("the rest")),
            ),
            executed = mutableListOf(),
            snapshots = snaps,
        )
        assertEquals(2, snaps.size)
        assertEquals("thinking-to-carry", assistantEntries(snaps[1]).last().reasoningContent)
    }
}

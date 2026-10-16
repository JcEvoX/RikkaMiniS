package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.data.model.LLMMessage
import com.rikkaminis.app.data.model.LLMStreamChunk
import com.rikkaminis.app.tools.SubagentSkill
import com.rikkaminis.app.tools.ToolExecutionResult
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * FE-5 route B tests for the tool-execution layer extracted from
 * ChatViewModel (ChatToolExecutors.kt / ChatShellExecution.kt).
 *
 * JVM-testable surface: wrapForBash wire format, linuxPathToMinisURL,
 * the sub-agent loop's turn/termination semantics (with the provider
 * stream + tool executor injected as fakes), and the memory record sink
 * contract. The Android-bound executors (browser, shell coordinator,
 * persistBrowserArtifact) are covered by CI's full suite.
 */
class ChatToolExecutorsTest {

    // ── wrapForBash ───────────────────────────────────────────────

    @Test
    fun `wrapForBash guards on command -v bash with sentinel 119`() {
        val w = wrapForBash("echo hi")
        assertTrue(w.startsWith("( command -v bash >/dev/null 2>&1 || exit 119; "))
        assertTrue(w.contains("| base64 -d > /tmp/.minis-exec-\$\$.sh"))
        assertTrue(w.endsWith("exit " + '$' + "rc )"))
    }

    @Test
    fun `wrapForBash encodes script with guaranteed trailing newline`() {
        val w = wrapForBash("echo hi")
        // decoded payload must end with newline — base64 of "echo hi\n"
        val expected = android.util.Base64.encodeToString(
            "echo hi\n".toByteArray(Charsets.UTF_8), android.util.Base64.NO_WRAP)
        assertTrue(w.contains("printf %s '$expected'"))
    }

    @Test
    fun `wrapForBash keeps already-newlined script unchanged payload`() {
        val w = wrapForBash("echo hi\n")
        val expected = android.util.Base64.encodeToString(
            "echo hi\n".toByteArray(Charsets.UTF_8), android.util.Base64.NO_WRAP)
        assertTrue(w.contains("'$expected'"))
    }

    // ── linuxPathToMinisURL ───────────────────────────────────────

    @Test
    fun `browser path maps to minis url`() {
        assertEquals("minis://browser/s.jpg", linuxPathToMinisURL("/var/minis/browser/s.jpg"))
    }

    @Test
    fun `non-minis path returns null and namespaces are preserved`() {
        assertNull(linuxPathToMinisURL("/tmp/x"))
        assertNull(linuxPathToMinisURL("/var/minis/"))
        assertEquals("minis://workspace/f.txt", linuxPathToMinisURL("/var/minis/workspace/f.txt"))
        assertEquals("minis://browser/s.jpg", linuxPathToMinisURL("/var/minis/browser/s.jpg"))
    }

    // ── streamedLinesForDisplay (per-tool accumulation + 50-line window) ──

    @Test
    fun `display buffer accumulates and trims to last 50 lines`() {
        val key = "test-${System.nanoTime()}"
        resetDisplayBuffer(key)
        var last: String? = null
        for (i in 1..60) {
            last = streamedLinesForDisplay("line$i", key)
        }
        assertEquals(50, last!!.lines().size)
        assertEquals("line60", last.lines().last())
        resetDisplayBuffer(key)
    }

    @Test
    fun `display buffer resets between tools`() {
        val key = "test-${System.nanoTime()}"
        resetDisplayBuffer(key)
        streamedLinesForDisplay("a", key)
        resetDisplayBuffer(key)
        val out = streamedLinesForDisplay("b", key)
        assertEquals("b", out)
        resetDisplayBuffer(key)
    }

    @Test
    fun `partial line replaces the previous fragment instead of appending`() {
        val key = "test-${System.nanoTime()}"
        resetDisplayBuffer(key)
        streamedLinesForDisplay("b12: in_progress No", key, isPartial = true)
        // Still the same line growing — one row, not two.
        assertEquals("b12: in_progress No", streamedLinesForDisplay("b12: in_progress No", key, isPartial = true))
        val out = streamedLinesForDisplay("b12: in_progress None", key, isPartial = false)
        assertEquals("b12: in_progress None", out)
        resetDisplayBuffer(key)
    }

    @Test
    fun `partial fragment is superseded by its completed line`() {
        val key = "test-${System.nanoTime()}"
        resetDisplayBuffer(key)
        streamedLinesForDisplay("Wed", key, isPartial = true)
        val out = streamedLinesForDisplay("Wed Sep  9 16:43:51 UTC 2026", key, isPartial = false)
        assertEquals("Wed Sep  9 16:43:51 UTC 2026", out)
        resetDisplayBuffer(key)
    }

    // ── runSubagentLoop ───────────────────────────────────────────

    private fun config(maxTurns: Int = 4) = SubagentSkill.SubagentConfig(
        isSubagent = true, maxTurns = maxTurns, maxOutputTokens = 100,
    )

    @Test
    fun `subagent loop returns after natural finish (no tool calls)`() {
        var streamCalls = 0
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "Sub-agent: s", config = config(),
                systemPrompt = "sys",
                streamProvider = { _ ->
                    streamCalls++
                    kotlinx.coroutines.flow.flowOf(LLMStreamChunk.Text("answer text"))
                },
                executeSubTool = { n, _ -> ToolExecutionResult("out-$n", true) },
                knownToolNames = listOf("file_read"),
                log = {},
            )
        }
        assertTrue(result.success)
        assertTrue(result.output.contains("answer text"))
        assertEquals(1, streamCalls)
        assertTrue(result.output.contains("completed in 1 turn"))
    }

    @Test
    fun `subagent loop executes tool calls then finishes`() {
        val executed = mutableListOf<String>()
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { messages ->
                    // first call emits a tool call, second call finishes
                    if (messages.size == 1) {
                        kotlinx.coroutines.flow.flowOf(
                            LLMStreamChunk.Text("need tool"),
                            LLMStreamChunk.ToolCallComplete("id1", "file_read", org.json.JSONObject()),
                        )
                    } else {
                        kotlinx.coroutines.flow.flowOf(LLMStreamChunk.Text("final answer"))
                    }
                },
                executeSubTool = { n, _ ->
                    executed.add(n)
                    ToolExecutionResult("tool output", true)
                },
                knownToolNames = listOf("file_read"),
                log = {},
            )
        }
        assertTrue(result.success)
        assertEquals(listOf("file_read"), executed)
        assertTrue(result.output.contains("need tool"))
        assertTrue(result.output.contains("final answer"))
        assertTrue(result.output.contains("completed in 2 turn"))
    }

    @Test
    fun `subagent loop strips restated tool-call copy from result text`() {
        // A reply carrying a RESTATED call markup (character drift drops the
        // underscores) beside the REAL call: the copy must not reach the
        // result text, the real call must still execute.
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { messages ->
                    if (messages.size == 1) {
                        kotlinx.coroutines.flow.flowOf(
                            LLMStreamChunk.Text("call <invoke name=\"shellexecute\">x</invoke> now"),
                            LLMStreamChunk.ToolCallComplete("id1", "file_read", org.json.JSONObject()),
                        )
                    } else {
                        kotlinx.coroutines.flow.flowOf(LLMStreamChunk.Text("done"))
                    }
                },
                executeSubTool = { _, _ -> ToolExecutionResult("out", true) },
                knownToolNames = listOf("file_read", "shell_execute"),
                log = {},
            )
        }
        assertTrue(result.success)
        assertTrue(result.output.contains("done"))
        assertTrue(result.output.contains("now"))
        assertFalse(result.output.contains("shellexecute"))
    }

    @Test
    fun `subagent loop refills instead of finishing when only a residue turn`() {
        // A turn whose text is ONLY a restated call and nothing parses:
        // the loop must hand back with a reminder (bounded) instead of
        // reading as a clean completion.
        var streamCalls = 0
        val lastMessages = mutableListOf<Int>()
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(maxTurns = 4),
                systemPrompt = "sys",
                streamProvider = { messages ->
                    streamCalls++
                    lastMessages.add(messages.size)
                    if (messages.size == 1) {
                        kotlinx.coroutines.flow.flowOf(
                            LLMStreamChunk.Text("<invoke name=\"shellexecute\">fix it</invoke>"),
                        )
                    } else {
                        kotlinx.coroutines.flow.flowOf(LLMStreamChunk.Text("ok, refilled"))
                    }
                },
                executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                knownToolNames = listOf("file_read", "shell_execute"),
                log = {},
            )
        }
        // Turn 1: residue only → refill (2nd request sees history grown past
        // the initial user message). Turn 2: plain text → natural finish.
        assertEquals(2, streamCalls)
        assertEquals(listOf(1, 3), lastMessages)
        assertTrue(result.output.contains("ok, refilled"))
    }

    @Test
    fun `subagent residue false positive on unknown tool name finishes naturally`() {
        // A markup-shaped tag naming an UNKNOWN tool must NOT be eaten (the
        // policy refuses to guess) — the loop finishes naturally on turn 1
        // and the text passes through untouched.
        var streamCalls = 0
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { _ ->
                    streamCalls++
                    kotlinx.coroutines.flow.flowOf(
                        LLMStreamChunk.Text("the <div name=\"viewport\">widget</div> tag"),
                    )
                },
                executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                knownToolNames = listOf("file_read", "shell_execute"),
                log = {},
            )
        }
        assertTrue(result.success)
        assertEquals(1, streamCalls)
        assertTrue(result.output.contains("viewport"))
    }

    @Test
    fun `subagent loop caps at max turns`() {
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t",
                config = config(maxTurns = 2), systemPrompt = "sys",
                streamProvider = { _ ->
                    kotlinx.coroutines.flow.flowOf(
                        LLMStreamChunk.Text("t"),
                        LLMStreamChunk.ToolCallComplete("id", "file_read", org.json.JSONObject()),
                    )
                },
                executeSubTool = { _, _ -> ToolExecutionResult("out", true) },
                knownToolNames = listOf("file_read"),
                log = {},
            )
        }
        assertTrue(result.success)
        assertTrue(result.output.contains("[Sub-agent reached max turns (2)]"))
    }

    @Test
    fun `subagent loop surfaces stream exception as failure`() {
        // An exception mid-stream skips the post-collect append (both in the
        // original VM code and the extracted engine) — the emission lands in
        // the local builder but resultSb never sees it. The error shape is:
        // header + cause; no partial line.
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { _ ->
                    kotlinx.coroutines.flow.flow {
                        emit(LLMStreamChunk.Text("partial"))
                        throw RuntimeException("gateway exploded")
                    }
                },
                executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                knownToolNames = listOf("file_read"),
                log = {},
            )
        }
        assertTrue("success should be false: " + result.output, !result.success)
        assertTrue("missing error header: " + result.output, result.output.contains("encountered an error after 1 turn"))
        assertTrue("missing cause: " + result.output, result.output.contains("gateway exploded"))
    }

    @Test
    fun `subagent empty output completes success with notice`() {
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { _ -> kotlinx.coroutines.flow.emptyFlow() },
                executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                knownToolNames = listOf("file_read"),
                log = {},
            )
        }
        assertTrue(result.success)
        assertTrue(result.output.contains("completed in 1 turn(s) with no output"))
    }

    // ── [fix/sweep1014-b19] truncated sub-agent turns ─────────────

    @Test
    fun `subagent truncated turn continues with reminder instead of finishing`() {
        // Turn 1 cut at the output ceiling (stopReason=length): was silently
        // treated as a clean finish. Must continue — the assistant partial
        // stays in history as prefill + the same lengthWallReminder the main
        // loop injects.
        val turn2Messages = mutableListOf<List<LLMMessage>>()
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { messages ->
                    if (messages.size == 1) {
                        kotlinx.coroutines.flow.flowOf(
                            LLMStreamChunk.Text("the answer is partial"),
                            LLMStreamChunk.Finished("length"),
                        )
                    } else {
                        turn2Messages.add(messages.toList())
                        kotlinx.coroutines.flow.flowOf(
                            LLMStreamChunk.Text("and here is the rest"),
                            LLMStreamChunk.Finished("end_turn"),
                        )
                    }
                },
                executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                knownToolNames = listOf("file_read"),
                log = {},
            )
        }
        assertTrue(result.success)
        assertTrue(result.output.contains("the answer is partial"))
        assertTrue(result.output.contains("and here is the rest"))
        // Continued → no truncation note on a successful completion.
        assertFalse(result.output.contains("cut off at the model's output limit"))
        // Turn 2 saw: user query + assistant prefill + continuation reminder.
        assertEquals(1, turn2Messages.size)
        val msgs = turn2Messages.first()
        assertEquals(3, msgs.size)
        assertEquals(LLMMessage.Role.USER, msgs[0].role)
        assertEquals(LLMMessage.Role.ASSISTANT, msgs[1].role)
        assertEquals("the answer is partial", msgs[1].content)
        assertEquals(LLMMessage.Role.USER, msgs[2].role)
        assertTrue(msgs[2].content.contains("cut off mid-sentence"))
        assertTrue(msgs[2].content.contains("the answer is partial"))
    }

    @Test
    fun `subagent eof truncation continues`() {
        // Stream-level truncation (Finished.truncated=true, no stopReason)
        // must also continue, not read as a clean finish.
        var streamCalls = 0
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { messages ->
                    streamCalls++
                    if (messages.size == 1) {
                        kotlinx.coroutines.flow.flowOf(
                            LLMStreamChunk.Text("partial before eof"),
                            LLMStreamChunk.Finished(null, truncated = true),
                        )
                    } else {
                        kotlinx.coroutines.flow.flowOf(
                            LLMStreamChunk.Text("done after eof"),
                            LLMStreamChunk.Finished("end_turn"),
                        )
                    }
                },
                executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                knownToolNames = listOf("file_read"),
                log = {},
            )
        }
        assertEquals(2, streamCalls)
        assertTrue(result.output.contains("partial before eof"))
        assertTrue(result.output.contains("done after eof"))
    }

    @Test
    fun `subagent empty truncated turn carries note instead of re-billing`() {
        // Empty text + length: the whole budget burned producing nothing —
        // no continuation (re-billing for the same wall), but the note is
        // carried instead of passing as a clean empty completion.
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { _ ->
                    kotlinx.coroutines.flow.flowOf(
                        LLMStreamChunk.Text("   "),
                        LLMStreamChunk.Finished("length"),
                    )
                },
                executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                knownToolNames = listOf("file_read"),
                log = {},
            )
        }
        assertTrue(result.success)
        assertTrue(result.output.contains("completed in 1 turn(s) with no output"))
        assertTrue(result.output.contains("cut off at the model's output limit"))
    }

    @Test
    fun `subagent truncation continuation is bounded by the ceiling`() {
        // Every turn truncates with non-empty text: continues up to
        // MAX_SUBAGENT_TRUNCATION_CONTINUES (4), then finishes with the
        // truncation note. Each continue consumes a turn.
        var streamCalls = 0
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t",
                config = config(maxTurns = 8), systemPrompt = "sys",
                streamProvider = { _ ->
                    streamCalls++
                    kotlinx.coroutines.flow.flowOf(
                        LLMStreamChunk.Text("part-${streamCalls}"),
                        LLMStreamChunk.Finished("length"),
                    )
                },
                executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                knownToolNames = listOf("file_read"),
                log = {},
            )
        }
        // 4 continues + the final truncated turn.
        assertEquals(5, streamCalls)
        assertTrue(result.success)
        assertTrue(result.output.contains("part-"))
        assertTrue(result.output.contains("cut off at the model's output limit"))
        assertTrue(result.output.contains("completed in 5 turn"))
    }

    @Test
    fun `subagent clean finish carries no truncation note`() {
        // Explicit clean finish (stopReason=null, truncated=false) must not
        // trip the truncation path.
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { _ ->
                    kotlinx.coroutines.flow.flowOf(
                        LLMStreamChunk.Text("clean answer"),
                        LLMStreamChunk.Finished(null),
                    )
                },
                executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                knownToolNames = listOf("file_read"),
                log = {},
            )
        }
        assertTrue(result.success)
        assertTrue(result.output.contains("clean answer"))
        assertFalse(result.output.contains("cut off at the model's output limit"))
    }

    @Test
    fun `subagent tool failure without double Error prefix`() {
        // Sub-tool failures already carry the "Error: " prefix — the loop
        // must not stack a second one ("Error: Error: ...").
        val turn2Messages = mutableListOf<List<LLMMessage>>()
        val result = kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { messages ->
                    if (messages.size == 1) {
                        kotlinx.coroutines.flow.flowOf(
                            LLMStreamChunk.ToolCallComplete("id1", "file_read", org.json.JSONObject()),
                        )
                    } else {
                        turn2Messages.add(messages.toList())
                        kotlinx.coroutines.flow.flowOf(LLMStreamChunk.Text("noted"))
                    }
                },
                executeSubTool = { _, _ ->
                    ToolExecutionResult("Error: Agent budget exhausted (shell_commands)", false)
                },
                knownToolNames = listOf("file_read"),
                log = {},
            )
        }
        assertTrue(result.success)
        val toolResultMsg = turn2Messages.first().last { it.role == LLMMessage.Role.USER }
        assertFalse(toolResultMsg.content.contains("Error: Error:"))
        assertTrue(toolResultMsg.content.contains("Error: Agent budget exhausted"))
    }

    // ── [fix/da1017-subagent-residue] turn hand-back contract ─────
    //
    // Every path that hands a turn back to the model (`continue`: residue
    // refill, truncation continuation) must record the turn's VISIBLE text in
    // history first — the next request is the model's only view of what it
    // wrote. The residue path used to skip it, so the model restated the
    // prose on the retry and `resultSb` (the sub-agent's persisted result,
    // which the main agent then reads as its own) carried it twice.

    private val residueProse = "Here is my plan. <invoke name=\"shellexecute\">ls -la</invoke>"

    @Test
    fun `appendSubagentTurnText appends non-blank text and skips blank`() {
        val history = mutableListOf(LLMMessage(role = LLMMessage.Role.USER, content = "q"))
        assertTrue(appendSubagentTurnText(history, "turn text"))
        assertEquals(2, history.size)
        assertEquals(LLMMessage.Role.ASSISTANT, history[1].role)
        assertEquals("turn text", history[1].content)
        // A blank turn appends NOTHING: the providers filter
        // (content.isEmpty() && contentParts.isEmpty()) messages out of the
        // request, so a blank shell is invisible to the model while still
        // breaking role alternation in memory.
        assertFalse(appendSubagentTurnText(history, "   "))
        assertFalse(appendSubagentTurnText(history, ""))
        assertEquals(2, history.size)
    }

    @Test
    fun `residue refill request carries the turn text as an assistant message`() {
        val requests = mutableListOf<List<LLMMessage>>()
        kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { messages ->
                    requests.add(messages.toList())
                    if (messages.size == 1) {
                        kotlinx.coroutines.flow.flowOf(LLMStreamChunk.Text(residueProse))
                    } else {
                        kotlinx.coroutines.flow.flowOf(LLMStreamChunk.Text("done"))
                    }
                },
                executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                knownToolNames = listOf("file_read", "shell_execute"),
                log = {},
            )
        }
        assertEquals(2, requests.size)
        val second = requests[1]
        // query → the turn the model just wrote → refill nudge. Three
        // messages, alternating: the assistant turn is present, so the bridge
        // must NOT synthesize a second one.
        assertEquals(
            listOf(LLMMessage.Role.USER, LLMMessage.Role.ASSISTANT, LLMMessage.Role.USER),
            second.map { it.role },
        )
        val assistant = second[1]
        assertTrue("assistant turn missing prose: '${assistant.content}'",
            assistant.content.contains("Here is my plan."))
        assertFalse("residue markup leaked into history: '${assistant.content}'",
            assistant.content.contains("shellexecute"))
    }

    @Test
    fun `residue-only turn adds no blank assistant shell`() {
        val requests = mutableListOf<List<LLMMessage>>()
        kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { messages ->
                    requests.add(messages.toList())
                    if (messages.size == 1) {
                        kotlinx.coroutines.flow.flowOf(
                            LLMStreamChunk.Text("<invoke name=\"shellexecute\">ls</invoke>"),
                        )
                    } else {
                        kotlinx.coroutines.flow.flowOf(LLMStreamChunk.Text("done"))
                    }
                },
                executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                knownToolNames = listOf("file_read", "shell_execute"),
                log = {},
            )
        }
        val second = requests[1]
        assertEquals(
            "blank shells in request: ${second.map { it.role }}",
            0,
            second.count { it.content.isBlank() && it.contentParts.isEmpty() },
        )
        assertTrue(second.any { it.content.contains("Re-emit it NOW") })
    }

    @Test
    fun `tool dispatch turn history shape is unchanged`() {
        // Zero-regression arm: the echo helper must not fire on a turn that
        // parsed a real call (baseline shape: text + tool_use part, then the
        // tool result as a user message).
        val requests = mutableListOf<List<LLMMessage>>()
        kotlinx.coroutines.runBlocking {
            runSubagentLoop(
                skillName = "s", query = "q", title = "t", config = config(),
                systemPrompt = "sys",
                streamProvider = { messages ->
                    requests.add(messages.toList())
                    if (messages.size == 1) {
                        kotlinx.coroutines.flow.flowOf(
                            LLMStreamChunk.Text("need tool"),
                            LLMStreamChunk.ToolCallComplete("id1", "file_read", org.json.JSONObject()),
                        )
                    } else {
                        kotlinx.coroutines.flow.flowOf(LLMStreamChunk.Text("final"))
                    }
                },
                executeSubTool = { _, _ -> ToolExecutionResult("tool output", true) },
                knownToolNames = listOf("file_read", "shell_execute"),
                log = {},
            )
        }
        val second = requests[1]
        assertEquals(3, second.size)
        assertEquals(
            listOf(LLMMessage.Role.USER, LLMMessage.Role.ASSISTANT, LLMMessage.Role.USER),
            second.map { it.role },
        )
        assertEquals("need tool", second[1].content)
        assertEquals(
            1,
            second[1].contentParts.filterIsInstance<com.rikkaminis.app.data.model.AgentContentPart.ToolUse>().size,
        )
    }

    @Test
    fun `subagent cancellation propagates instead of becoming a failure result`() {
        // A stop is not a sub-agent failure: the generic catch used to swallow
        // CancellationException and hand back "Sub-agent '<name>' encountered
        // an error after N turn(s)" — a stop read as a crash, attributed to
        // the model.
        var returned: ToolExecutionResult? = null
        val thrown: Throwable? = try {
            kotlinx.coroutines.runBlocking {
                returned = runSubagentLoop(
                    skillName = "s", query = "q", title = "t", config = config(),
                    systemPrompt = "sys",
                    streamProvider = { throw kotlinx.coroutines.CancellationException("stop pressed") },
                    executeSubTool = { _, _ -> ToolExecutionResult("x", true) },
                    knownToolNames = listOf("file_read"),
                    log = {},
                )
            }
            null
        } catch (t: Throwable) {
            t
        }
        assertNull("the loop returned $returned instead of propagating", returned)
        assertTrue("expected CancellationException, got ${thrown?.javaClass}", thrown is kotlinx.coroutines.CancellationException)
    }
}

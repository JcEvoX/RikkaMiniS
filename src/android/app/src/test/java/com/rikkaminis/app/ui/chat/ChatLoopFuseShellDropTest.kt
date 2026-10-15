package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [audit-1015 BUG-03A/3B/3C] Regression tests.
 *
 * BUG-03A: the deterministic-empty fast-exit sets terminalErrorSurfaced=true
 * and falls through to the empty length-wall retry gate — without the flag
 * check the gate re-bills the full input for one more provably identical
 * empty request AFTER the terminal banner was already shown.
 *
 * BUG-03B/3C: a blank assistant shell (empty content + no parts + no
 * reasoning) must be dropped before (a) the empty length-wall retry and
 * (b) the nudge paths' role-alternation bridge — the provider request
 * builders filter blank shells out, so an undropped shell turns
 * [..., shell, USER] into [..., USER, USER] (Anthropic hard 400).
 *
 * Expected values are literal ground truth (not derived from the constants).
 */
class ChatLoopFuseShellDropTest {

    // ── BUG-03A: the deterministic fuse must stop the retry gate ─────────

    @Test
    fun `fuse with banner refuses the retry at first hit`() {
        // hits=2 < 3 and context NOT exhausted → legacy gate would say true;
        // the banner is already on screen (terminalErrorSurfaced=true).
        assertFalse(
            shouldContinueAfterDeterministicFuse(
                terminalErrorSurfaced = true, hits = 2, contextExhausted = false,
            )
        )
    }

    @Test
    fun `fuse with banner refuses the retry at every hit count`() {
        for (hits in 1..5) {
            assertFalse(
                "fused run must refuse at hits=$hits",
                shouldContinueAfterDeterministicFuse(
                    terminalErrorSurfaced = true, hits = hits, contextExhausted = false,
                ),
            )
        }
    }

    @Test
    fun `fuse with banner refuses even when context NOT exhausted`() {
        // The exact hijack: hits=2 < 3, contextExhausted=false — the legacy
        // gate returned true and burned the third request.
        assertFalse(
            shouldContinueAfterDeterministicFuse(
                terminalErrorSurfaced = true, hits = 2, contextExhausted = false,
            )
        )
    }

    @Test
    fun `no banner keeps the legacy gate semantics`() {
        // terminalErrorSurfaced=false → delegates to shouldRetryEmptyLengthWall.
        assertTrue(
            shouldContinueAfterDeterministicFuse(
                terminalErrorSurfaced = false, hits = 2, contextExhausted = false,
            )
        )
        // Over its own 3-hit budget → refuse.
        assertFalse(
            shouldContinueAfterDeterministicFuse(
                terminalErrorSurfaced = false, hits = 3, contextExhausted = false,
            )
        )
        // Context exhausted → refuse (the original gate's own rule).
        assertFalse(
            shouldContinueAfterDeterministicFuse(
                terminalErrorSurfaced = false, hits = 1, contextExhausted = true,
            )
        )
    }

    // ── BUG-03B/3C: the blank-shell predicate (three fields) ────────────

    private fun blankShell(reasoning: String? = null) = LLMMessage(
        role = LLMMessage.Role.ASSISTANT,
        content = "",
        contentParts = emptyList(),
        reasoningContent = reasoning,
    )

    @Test
    fun `blank assistant shell is detected`() {
        assertTrue(isBlankAssistantShell(blankShell(reasoning = null)))
        assertTrue(isBlankAssistantShell(blankShell(reasoning = "")))
    }

    @Test
    fun `shell with reasoningContent is NOT blank`() {
        // Mutation-negative-control target: dropping the reasoning check would
        // silently discard reasoning that round-trips on the next request.
        assertFalse(isBlankAssistantShell(blankShell(reasoning = "thinking...")))
    }

    @Test
    fun `shell with content or parts is NOT blank`() {
        assertFalse(
            isBlankAssistantShell(
                LLMMessage(LLMMessage.Role.ASSISTANT, "partial", contentParts = emptyList())
            )
        )
        assertFalse(
            isBlankAssistantShell(
                LLMMessage(
                    LLMMessage.Role.ASSISTANT, "",
                    contentParts = listOf(AgentContentPart.Text("x")),
                )
            )
        )
    }

    @Test
    fun `user message is never a blank assistant shell`() {
        assertFalse(
            isBlankAssistantShell(
                LLMMessage(LLMMessage.Role.USER, "", contentParts = emptyList())
            )
        )
    }

    // ── BUG-03B: the trailing drop before the retry ─────────────────────

    @Test
    fun `trailing blank shell is dropped before retry`() {
        val history = mutableListOf(
            LLMMessage(LLMMessage.Role.USER, "question", contentParts = listOf(AgentContentPart.Text("question"))),
            blankShell(),
        )
        dropTrailingBlankAssistantShell(history)
        assertEquals(1, history.size)
        assertEquals(LLMMessage.Role.USER, history[0].role)
    }

    @Test
    fun `trailing shell with reasoning is kept`() {
        val history = mutableListOf(
            LLMMessage(LLMMessage.Role.USER, "q", contentParts = listOf(AgentContentPart.Text("q"))),
            blankShell(reasoning = "kept"),
        )
        dropTrailingBlankAssistantShell(history)
        assertEquals(2, history.size)
        assertEquals("kept", history[1].reasoningContent)
    }

    @Test
    fun `drop is a no-op on empty history and non-blank tails`() {
        dropTrailingBlankAssistantShell(mutableListOf())
        val history = mutableListOf(
            LLMMessage(LLMMessage.Role.ASSISTANT, "answer", contentParts = emptyList()),
        )
        dropTrailingBlankAssistantShell(history)
        assertEquals(1, history.size)
    }

    // ── BUG-03C: drop-before-bridge removes the USER,USER shape ─────────

    @Test
    fun `blank shell + nudge append yields no consecutive user roles`() {
        val history = mutableListOf(
            LLMMessage(LLMMessage.Role.USER, "q", contentParts = listOf(AgentContentPart.Text("q"))),
            blankShell(),
        )
        dropTrailingBlankAssistantShell(history)
        ensureRoleAlternationBeforeUserAppend(history)
        history.add(LLMMessage(LLMMessage.Role.USER, "nudge", contentParts = listOf(AgentContentPart.Text("nudge"))))
        // After the drop the real tail is USER; the bridge inserted an
        // ASSISTANT between it and the nudge → no USER,USER anywhere.
        for (i in 1 until history.size) {
            assertFalse(
                "consecutive user roles at index $i",
                history[i - 1].role == LLMMessage.Role.USER && history[i].role == LLMMessage.Role.USER,
            )
        }
        assertEquals(listOf(LLMMessage.Role.USER, LLMMessage.Role.ASSISTANT, LLMMessage.Role.USER), history.map { it.role })
    }

    @Test
    fun `tool_result tail + nudge still bridges (drop is a no-op there)`() {
        val history = mutableListOf(
            LLMMessage(LLMMessage.Role.USER, "tool_result", contentParts = emptyList()),
        )
        dropTrailingBlankAssistantShell(history)
        ensureRoleAlternationBeforeUserAppend(history)
        history.add(LLMMessage(LLMMessage.Role.USER, "nudge", contentParts = listOf(AgentContentPart.Text("nudge"))))
        assertEquals(
            listOf(LLMMessage.Role.USER, LLMMessage.Role.ASSISTANT, LLMMessage.Role.USER),
            history.map { it.role },
        )
    }
}

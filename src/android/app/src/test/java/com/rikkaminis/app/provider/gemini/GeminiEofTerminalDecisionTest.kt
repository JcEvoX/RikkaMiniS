package com.rikkaminis.app.provider.gemini

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1045 A-02] Boundary table for the terminal chunk Gemini's SSE
 * loop emits at EOF.
 *
 * The defect this pins: the loop's `else` arm used to send
 * `Finished(lastFinishReason ?: "end_turn")`. On a FULLY EMPTY EOF
 * (no finishReason, no text, no tool payload) that fabricated a stop reason,
 * and `failOnSilentEmptyCompletion` (LLMProvider.kt:239) decides by
 * `Finished.stopReason != null` — so an upstream that dropped the SSE body
 * produced a "clean" empty assistant turn instead of a retryable
 * TransientError. The KDoc of [shouldFlagEofTruncated] promises that case is
 * left to `failOnSilentEmptyCompletion`; the fabricated stop reason is what
 * made that promise unenforceable.
 *
 * Expected values are hand-written literals (never derived from the code
 * under test — see evidence-discipline).
 */
class GeminiEofTerminalDecisionTest {

    @Test
    fun `empty EOF emits no stop reason so the empty-stream operator owns it`() {
        val terminal = geminiEofTerminal(null, 0, 0)
        assertNull(terminal.stopReason)
        assertFalse(terminal.truncated)
    }

    @Test
    fun `text-only EOF is truncated and carries no stop reason`() {
        val terminal = geminiEofTerminal(null, 7, 0)
        assertNull(terminal.stopReason)
        assertTrue(terminal.truncated)
    }

    @Test
    fun `tool-only EOF is truncated and carries no stop reason`() {
        val terminal = geminiEofTerminal(null, 0, 42)
        assertNull(terminal.stopReason)
        assertTrue(terminal.truncated)
    }

    @Test
    fun `a real finish reason is passed through verbatim`() {
        val terminal = geminiEofTerminal("stop", 12, 0)
        assertEquals("stop", terminal.stopReason)
        assertFalse(terminal.truncated)
    }

    @Test
    fun `MAX_TOKENS is a clean finish even with a tool payload`() {
        val terminal = geminiEofTerminal("MAX_TOKENS", 0, 3)
        assertEquals("MAX_TOKENS", terminal.stopReason)
        assertFalse(terminal.truncated)
    }

    @Test
    fun `no finish reason is ever invented for an empty EOF`() {
        // The single assertion the whole fix exists for: the old code sent
        // "end_turn" here. Anything non-null is a regression.
        val stopReasons = listOf(
            geminiEofTerminal(null, 0, 0).stopReason,
            geminiEofTerminal(null, 0, 0).stopReason,
        )
        assertTrue(stopReasons.all { it == null })
    }
}

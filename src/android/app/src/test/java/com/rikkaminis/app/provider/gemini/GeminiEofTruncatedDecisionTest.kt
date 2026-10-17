package com.rikkaminis.app.provider.gemini

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1019 ISSUE-04] Boundary table for the Gemini SSE EOF verdict.
 *
 * Before the fix the loop's own condition was `lastFinishReason == null &&
 * contentChars > 0`, and `contentChars` only ever counted text/thinking — so a
 * turn that streamed nothing but `functionCall` parts and then lost the
 * connection (no finishReason) fell through to a clean `Finished("end_turn")`
 * and the agent loop ran an incomplete tool set as if the turn had finished.
 * The condition now lives in [shouldFlagEofTruncated], which also takes the
 * tool-payload counter (AnthropicProvider's `toolChars` counterpart).
 *
 * Expected values are hand-written literals (never derived from the code under
 * test — see evidence-discipline): a mutation that drops either counter from
 * the verdict flips exactly the rows below that carry that counter.
 */
class GeminiEofTruncatedDecisionTest {

    @Test
    fun `pure-tool EOF without a finishReason is truncated`() {
        // The ISSUE-04 case: tool payload present, no text/thinking at all.
        assertTrue(shouldFlagEofTruncated(null, 0, 42))
    }

    @Test
    fun `fully empty EOF stays with failOnSilentEmptyCompletion`() {
        assertFalse(shouldFlagEofTruncated(null, 0, 0))
    }

    @Test
    fun `a clean finishReason wins even when tool calls were delivered`() {
        assertFalse(shouldFlagEofTruncated("stop", 0, 42))
    }

    @Test
    fun `text-only EOF is still truncated`() {
        assertTrue(shouldFlagEofTruncated(null, 7, 0))
    }

    @Test
    fun `a one-char tool payload already counts as stream activity`() {
        assertTrue(shouldFlagEofTruncated(null, 0, 1))
    }

    @Test
    fun `MAX_TOKENS finishReason is a clean finish regardless of counters`() {
        assertFalse(shouldFlagEofTruncated("MAX_TOKENS", 500, 3))
    }
}

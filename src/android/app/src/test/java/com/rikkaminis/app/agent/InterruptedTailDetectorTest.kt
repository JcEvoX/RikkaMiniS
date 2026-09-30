package com.rikkaminis.app.agent

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM tests for [InterruptedTailDetector] — the "is this session's tail an
 * interrupted agent loop?" rule that decides whether the user gets a Resume
 * affordance at all (GH#262/#263).
 *
 * A wrong answer is invisible in code review: either the Resume banner never
 * appears (a reply-less user tail used to be completely unrecoverable) or it
 * appears over a turn that is merely still waiting. Every expectation below is
 * a literal [InterruptedTailShape] constant — none is derived from the code
 * under test, so a mutation of the rule fails here rather than masquerading as
 * a pass.
 *
 * Liveness is deliberately NOT tested here: [InterruptedTailDetector] answers
 * "what shape is this tail", and the callers own the gate
 * (`!isStreaming && !SessionActivityTracker.isActive(sid)`). The gate itself is
 * covered by the sandbox harness, which also textually verifies the call site
 * still carries it.
 */
class InterruptedTailDetectorTest {

    // ─── helpers ─────────────────────────────────────────────────────────────

    private fun user(vararg kinds: InterruptedTailPartKind, firstText: String? = null) =
        InterruptedTailSnapshot("user", kinds.toList(), firstText)

    private fun assistant(vararg kinds: InterruptedTailPartKind, firstText: String? = null) =
        InterruptedTailSnapshot("assistant", kinds.toList(), firstText)

    // ─── Case D: reply-less user turn (the shape this test suite exists for) ─

    @Test
    fun `plain text user turn with no reply is Case D`() {
        val snap = user(InterruptedTailPartKind.TEXT, firstText = "hello, help me")
        assertEquals(InterruptedTailShape.UNANSWERED_USER_TURN, InterruptedTailDetector.classify(snap))
        assertTrue(InterruptedTailDetector.isInterrupted(snap))
    }

    @Test
    fun `user turn with text and image parts is still Case D`() {
        val snap = user(
            InterruptedTailPartKind.TEXT,
            InterruptedTailPartKind.OTHER,
            firstText = "look at this",
        )
        assertEquals(InterruptedTailShape.UNANSWERED_USER_TURN, InterruptedTailDetector.classify(snap))
    }

    @Test
    fun `user turn mixing toolResult and text is Case D not Case A`() {
        // Case A requires ALL parts to be tool_result. A mixed tail means the
        // turn never completed, so it falls through to Case D.
        val snap = user(
            InterruptedTailPartKind.TOOL_RESULT,
            InterruptedTailPartKind.TEXT,
            firstText = "and now?",
        )
        assertEquals(InterruptedTailShape.UNANSWERED_USER_TURN, InterruptedTailDetector.classify(snap))
    }

    @Test
    fun `first part is toolUse but text is present later is Case D`() {
        // firstText is null (the first part is not text) — the classifier must
        // not NPE and must not mistake this for the continue reminder.
        val snap = user(InterruptedTailPartKind.TOOL_USE, InterruptedTailPartKind.TEXT)
        assertEquals(InterruptedTailShape.UNANSWERED_USER_TURN, InterruptedTailDetector.classify(snap))
    }

    @Test
    fun `empty user turn is not recoverable`() {
        // Nothing to answer: re-sending would post a content-less message the
        // API rejects, so an empty tail must stay NONE.
        assertEquals(InterruptedTailShape.NONE, InterruptedTailDetector.classify(user()))
    }

    @Test
    fun `null tail is not interrupted`() {
        assertEquals(InterruptedTailShape.NONE, InterruptedTailDetector.classify(null))
        assertFalse(InterruptedTailDetector.isInterrupted(null))
    }

    // ─── Case A: tools ran, follow-up model call never fired ─────────────────

    @Test
    fun `all-toolResult user tail is Case A`() {
        val snap = user(
            InterruptedTailPartKind.TOOL_RESULT,
            InterruptedTailPartKind.TOOL_RESULT,
        )
        assertEquals(InterruptedTailShape.TOOL_RESULT_TAIL, InterruptedTailDetector.classify(snap))
        assertTrue(InterruptedTailDetector.isInterrupted(snap))
    }

    @Test
    fun `single toolResult user tail is Case A`() {
        val snap = user(InterruptedTailPartKind.TOOL_RESULT)
        assertEquals(InterruptedTailShape.TOOL_RESULT_TAIL, InterruptedTailDetector.classify(snap))
    }

    // ─── Case B: model asked for tools that never executed ───────────────────

    @Test
    fun `assistant tail with any toolUse is Case B`() {
        val snap = assistant(InterruptedTailPartKind.TOOL_USE)
        assertEquals(InterruptedTailShape.ASSISTANT_TOOL_USE, InterruptedTailDetector.classify(snap))
        assertTrue(InterruptedTailDetector.isInterrupted(snap))
    }

    @Test
    fun `assistant tail mixing text and toolUse is Case B`() {
        val snap = assistant(
            InterruptedTailPartKind.TEXT,
            InterruptedTailPartKind.TOOL_USE,
            firstText = "let me check that",
        )
        assertEquals(InterruptedTailShape.ASSISTANT_TOOL_USE, InterruptedTailDetector.classify(snap))
    }

    // ─── Case C: synthetic continue reminder committed but loop never resumed ─

    @Test
    fun `single continue reminder text part is Case C`() {
        val snap = user(
            InterruptedTailPartKind.TEXT,
            firstText = "<system-reminder>The user stopped the previous response but now wants to continue. Pick up exactly where you left off.</system-reminder>",
        )
        assertEquals(InterruptedTailShape.CONTINUE_REMINDER, InterruptedTailDetector.classify(snap))
        assertTrue(InterruptedTailDetector.isInterrupted(snap))
    }

    @Test
    fun `marker match is substring based`() {
        // The marker can be embedded in longer text; the check is contains(),
        // not equals().
        val snap = user(InterruptedTailPartKind.TEXT, firstText = "prefix The user stopped the previous response suffix")
        assertEquals(InterruptedTailShape.CONTINUE_REMINDER, InterruptedTailDetector.classify(snap))
    }

    @Test
    fun `reminder text alongside another part is Case D not Case C`() {
        // Case C is specifically the single synthetic reminder; anything else
        // is an ordinary unanswered turn.
        val snap = user(
            InterruptedTailPartKind.TEXT,
            InterruptedTailPartKind.TOOL_RESULT,
            firstText = "The user stopped the previous response",
        )
        assertEquals(InterruptedTailShape.UNANSWERED_USER_TURN, InterruptedTailDetector.classify(snap))
    }

    @Test
    fun `reminder text without a TEXT kind is not Case C`() {
        // Guards the ordering: isContinueReminder must require the first kind
        // to be TEXT, otherwise a tool_use-first tail carrying reminder-shaped
        // text would be misclassified.
        val snap = user(
            InterruptedTailPartKind.TOOL_RESULT,
            firstText = "The user stopped the previous response",
        )
        assertEquals(InterruptedTailShape.TOOL_RESULT_TAIL, InterruptedTailDetector.classify(snap))
    }

    // ─── not interrupted ─────────────────────────────────────────────────────

    @Test
    fun `completed assistant reply is not interrupted`() {
        val snap = assistant(InterruptedTailPartKind.TEXT, firstText = "here is the answer")
        assertEquals(InterruptedTailShape.NONE, InterruptedTailDetector.classify(snap))
        assertFalse(InterruptedTailDetector.isInterrupted(snap))
    }

    @Test
    fun `assistant reply with only images is not interrupted`() {
        assertEquals(
            InterruptedTailShape.NONE,
            InterruptedTailDetector.classify(assistant(InterruptedTailPartKind.OTHER)),
        )
    }

    @Test
    fun `unknown role is not interrupted`() {
        assertEquals(
            InterruptedTailShape.NONE,
            InterruptedTailDetector.classify(InterruptedTailSnapshot("system", listOf(InterruptedTailPartKind.TEXT), "hi")),
        )
        assertEquals(
            InterruptedTailShape.NONE,
            InterruptedTailDetector.classify(InterruptedTailSnapshot("tool", emptyList(), null)),
        )
    }

    @Test
    fun `role is case insensitive`() {
        assertEquals(
            InterruptedTailShape.UNANSWERED_USER_TURN,
            InterruptedTailDetector.classify(InterruptedTailSnapshot("USER", listOf(InterruptedTailPartKind.TEXT), "hi")),
        )
        assertEquals(
            InterruptedTailShape.NONE,
            InterruptedTailDetector.classify(InterruptedTailSnapshot("Assistant", listOf(InterruptedTailPartKind.TEXT), "hi")),
        )
        assertEquals(
            InterruptedTailShape.ASSISTANT_TOOL_USE,
            InterruptedTailDetector.classify(InterruptedTailSnapshot("ASSISTANT", listOf(InterruptedTailPartKind.TOOL_USE), null)),
        )
    }
}

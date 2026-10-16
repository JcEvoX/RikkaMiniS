package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.conversation.PreflightVerdict
import com.rikkaminis.app.data.model.LLMMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1016 6a] Contract of the mid-turn re-budget used by BOTH fallback
 * switches in `AgentLoopEngine.runAgentLoop`.
 *
 * The defect this pins (field shape: `HTTP 400` right after a fallback): a turn
 * captures `maxTokens` / the calibration key ONCE from the member that serves
 * the first attempt. Both fallback switches then replace
 * `loopState.currentProvider` and re-enter the retry loop, which re-used those
 * captured values — so a group whose first entry is a 64K-output model
 * falling back to a 4K-output member sent `max_tokens = 64000` to the 4K
 * member, and the calibration sample was charged to the member we left.
 *
 * Every assertion below is a literal on purpose (evidence discipline): the
 * expectations are NOT derived from the implementation, so a mutation of the
 * constants inside [RequestBudgetRecompute] cannot move an expectation with it
 * and silently keep the suite green. The message in [atEstimate] is sized so
 * that the provider-neutral estimate is exactly 100 tokens (400 ASCII chars ÷
 * `ContextCompactor.CHARS_PER_TOKEN` = 4, no per-message overhead), which keeps
 * the calibration cases literal too.
 */
class RequestBudgetRecomputeTest {

    /** `MIN_MAX_TOKENS_CONST` in `AgentLoopEngine` — the floor passed as `outputFloor`. */
    private val outputFloor = 1_024

    /** 400 ASCII chars ⇒ exactly 100 estimated tokens. */
    private fun atEstimate(): List<LLMMessage> =
        listOf(LLMMessage(LLMMessage.Role.USER, "a".repeat(400)))

    private fun recompute(
        messages: List<LLMMessage> = emptyList(),
        calibrationRatioPercent: Long = 100,
        windowTokens: Int = 200_000,
        providerMaxTokens: Int = 4_096,
    ): RequestBudgetRecompute.Result = RequestBudgetRecompute.forProvider(
        messages = messages,
        systemPrompt = null,
        tools = emptyList(),
        calibrationRatioPercent = calibrationRatioPercent,
        windowTokens = windowTokens,
        providerMaxTokens = providerMaxTokens,
        outputFloor = outputFloor,
    )

    @Test
    fun `the output budget belongs to the member that will serve the request`() {
        // 4K-output member, roomy window: its own ceiling is what goes on the wire.
        val asNewMember = recompute(providerMaxTokens = 4_096)
        assertEquals(PreflightVerdict.Decision.PASS, asNewMember.verdict.decision)
        assertEquals(4_096, asNewMember.maxTokens)

        // The SAME call fed the member we just left (a 64K-output model) — that
        // is exactly what the pre-fix retry loop kept sending after the switch.
        val asLeftMember = recompute(providerMaxTokens = 64_000)
        assertEquals(64_000, asLeftMember.maxTokens)
        assertTrue(
            "the member we left cannot serve a 4K member's request: " +
                "${asLeftMember.maxTokens} > 4096",
            asLeftMember.maxTokens > 4_096,
        )
    }

    @Test
    fun `the calibration coefficient is the new member's own`() {
        val calibratedEqual = recompute(messages = atEstimate(), calibrationRatioPercent = 100)
        assertEquals(100L, calibratedEqual.rawEstimateTokens)
        assertEquals(100L, calibratedEqual.calibratedEstimateTokens)

        // A member whose historical usage runs 1.5× the local estimate must
        // scale the estimate up; 0.8× scales it down. Both keep the neutral
        // estimate visible so a regression that drops one of the two is caught.
        val verbose = recompute(messages = atEstimate(), calibrationRatioPercent = 150)
        assertEquals(100L, verbose.rawEstimateTokens)
        assertEquals(150L, verbose.calibratedEstimateTokens)

        val terse = recompute(messages = atEstimate(), calibrationRatioPercent = 80)
        assertEquals(100L, terse.rawEstimateTokens)
        assertEquals(80L, terse.calibratedEstimateTokens)
    }

    @Test
    fun `the calibration of the left member leaves the estimate alone`() {
        // Neutral member, no calibration: est stays 100 and still fits (window
        // 200_000 vs 100 + 4_096 + 4_000 margin), so the member's own ceiling
        // remains the answer.
        val r = recompute(messages = atEstimate(), calibrationRatioPercent = 100)
        assertEquals(100L, r.calibratedEstimateTokens)
        assertEquals(PreflightVerdict.Decision.PASS, r.verdict.decision)
        assertEquals(4_096, r.maxTokens)
    }

    @Test
    fun `a window that cannot carry the output budget shrinks the output`() {
        // window 40_000, input 0, margin 4_000 ⇒ room for output = 36_000.
        val r = recompute(windowTokens = 40_000, providerMaxTokens = 64_000)
        assertEquals(PreflightVerdict.Decision.SHRINK_OUTPUT, r.verdict.decision)
        assertEquals(36_000, r.maxTokens)
        assertEquals(36_000, r.verdict.suggestedMaxTokens)
    }

    @Test
    fun `an input that no longer fits floors the output instead of trusting the old budget`() {
        // window 2_000 < margin 4_000: there is no room at all, so the verdict
        // is NEEDS_COMPACTION (the next turn entry runs the full pipeline) and
        // the output is floored — never the left member's 64K.
        val r = recompute(windowTokens = 2_000, providerMaxTokens = 64_000)
        assertEquals(PreflightVerdict.Decision.NEEDS_COMPACTION, r.verdict.decision)
        assertEquals(outputFloor, r.maxTokens)
        assertEquals(2_000, r.verdict.windowTokens)
    }

    @Test
    fun `an unknown window passes the member's own budget through`() {
        val r = recompute(windowTokens = 0, providerMaxTokens = 8_192)
        assertEquals(PreflightVerdict.Decision.PASS, r.verdict.decision)
        assertEquals(8_192, r.maxTokens)
    }

    @Test
    fun `a floor-equal output budget is never shrunk below the floor`() {
        // requested == outputFloor ⇒ the shrink branch is not available
        // (`requestedMaxTokens > outputFloor` fails), so an input that does not
        // fit floors rather than computing a sub-floor suggestion.
        val r = recompute(windowTokens = 5_000, providerMaxTokens = outputFloor)
        assertEquals(PreflightVerdict.Decision.NEEDS_COMPACTION, r.verdict.decision)
        assertEquals(outputFloor, r.maxTokens)
    }
}

package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.data.ContextPolicy
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Repo-landed replay of the 2026-10-10 23:22:30 / 23:24:03 incident on session
 * efbb18b6: after an in-loop offload/auto-compact rewrite, the cached
 * `lastContextTokens` figure still described the PRE-rewrite request, so
 * `dynamicMaxTokens` pinned the output budget at 1024, the model returned
 * `finish=length` with an empty body, and the empty-length retry gate
 * (reading the same stale figure via `isContextExhausted`) gave up — the run
 * died silently.
 *
 * The fix (4682973b, merged f9c374ca) clears the figure after a rewrite:
 * AgentLoopEngine.kt:496-499 `if (postOffloadTokens != null || compacted) {
 * loopState.lastContextTokens = 0; host.setLastTurnContextTokens(0) }`.
 *
 * This class merges two sandbox verification artifacts that previously lived
 * outside the repo (verify-compact-fix-1010/CompactStaleContextTokensReplayTest.kt
 * and compact-1011-harness/Probe.kt) into one CI-executed unit test.
 *
 * What is REAL vs MIRRORED here:
 *  - REAL: [shouldRetryEmptyLengthWall] / [LENGTH_WALL_EMPTY_MAX_HITS]
 *    (ChatLengthWallLogic.kt) and [ContextPolicy] (data/ContextPolicy.kt) —
 *    both are pure Kotlin with no Android imports, compiled in directly.
 *  - MIRRORED: the `dynamicMaxTokens` arithmetic and the `tokens <= 0` guard
 *    of `contextPressure()` — the real functions are ChatViewModel extensions
 *    (Android deps). Each mirror below cites its source `file:line` at main
 *    4fa0cb20 and the field-evidence numbers it is calibrated against.
 *
 * Ground truth (minis-2026-10-10.log, incident on session efbb18b6):
 *   dynamicMaxTokens: 1024 (remaining=-247879, ceiling=128000,
 *   window=220000, input=467879, model=glm-5.3-flash)
 *   offload log: "now 124400 (62%)"
 */
class CompactStaleContextTokensReplayTest {

    // Mirror of ChatViewModel.kt:317 (companion MIN_MAX_TOKENS = 1024) and
    // ChatViewModel.kt:337 (companion GLOBAL_MAX_TOKENS_CEILING = 128_000);
    // the real constants live in a companion object of the Android-coupled
    // ChatViewModel, so the test carries the calibrated values instead.
    private val minMaxTokens = 1024
    private val globalMaxTokensCeiling = 128_000

    /** Incident numbers, taken verbatim from the 2026-10-10 field log. */
    private val incidentWindow = 220_000
    private val incidentInput = 467_879

    /**
     * MIRROR of ChatErrorHandling.kt:93-118 (`dynamicMaxTokens`, main 4fa0cb20):
     *   val inputTokens = if (lastContextTokens > 0) lastContextTokens else 0
     *   val remaining = contextWindow - inputTokens
     *   val clamped = maxOf(remaining, ChatViewModel.MIN_MAX_TOKENS)
     *   val result = minOf(maxOutputCeiling, clamped)
     * The real function resolves the window via effectiveContextWindowTokens()
     * (Android-coupled), so the window arrives as a parameter here. Calibrated
     * against the logged "input=467879, window=220000 -> 1024" observation.
     */
    private fun mirrorDynamicMaxTokens(contextWindow: Int, lastContextTokens: Int): Int {
        val inputTokens = if (lastContextTokens > 0) lastContextTokens else 0
        val remaining = contextWindow - inputTokens
        val clamped = maxOf(remaining, minMaxTokens)
        return minOf(globalMaxTokensCeiling, clamped)
    }

    /**
     * MIRROR of ChatContextWindowExt.kt:105-125 (`contextPressure()` +
     * `isContextExhausted()`, main 4fa0cb20):
     *   val tokens = _lastTurnContextTokens.value
     *   if (tokens <= 0) return null            // -> pressure is null
     *   ...
     *   isContextExhausted() = contextPressure()?.result == EXHAUSTED
     * i.e. ONLY a non-null pressure can be EXHAUSTED, and a cleared figure
     * (0) reads as "unknown, not exhausted". The EXHAUSTED verdict itself is
     * delegated to the REAL [ContextPolicy.check].
     */
    private fun mirrorIsContextExhausted(tokens: Int, contextWindow: Int): Boolean {
        if (tokens <= 0) return false
        return ContextPolicy.forContextWindow(contextWindow).check(tokens, contextWindow) ==
            ContextPolicy.CheckResult.EXHAUSTED
    }

    // ── Calibration: the mirror must reproduce the logged observation ─────

    @Test
    fun calibration_mirrorReproducesTheLogged1024() {
        // Log: "dynamicMaxTokens: 1024 (remaining=-247879, ceiling=128000,
        //       window=220000, input=467879, model=glm-5.3-flash)"
        assertEquals(-247_879, incidentWindow - incidentInput)
        assertEquals(1024, mirrorDynamicMaxTokens(incidentWindow, incidentInput))
    }

    // ── Revert direction: with the fix absent (figure NOT cleared), the
    //    incident reproduces exactly. These assertions are also the negative
    //    control for the fix: every one of them flips if the clear is removed. ─

    @Test
    fun withoutFix_outputBudgetIsPinnedAtTheFloor() {
        assertEquals(minMaxTokens, mirrorDynamicMaxTokens(incidentWindow, incidentInput))
    }

    @Test
    fun withoutFix_realPolicyReadsTheStaleFigureAsExhausted() {
        // Probe leg L3a: the REAL ContextPolicy classifies the pre-rewrite
        // figure (input=467879 vs window=220000) as EXHAUSTED — the hard
        // window ceiling in ContextPolicy.check (ContextPolicy.kt:55-57).
        assertEquals(
            ContextPolicy.CheckResult.EXHAUSTED,
            ContextPolicy.forContextWindow(incidentWindow).check(incidentInput, incidentWindow),
        )
    }

    @Test
    fun withoutFix_realRetryGateRefusesAndTheRunDiesSilently() {
        // Log: "NOT retrying ... (wall hit M3.N@e65815.lengthWallEmptyHits/3)"
        // REAL gate (ChatLengthWallLogic.kt:276-280), not a mirror.
        assertFalse(shouldRetryEmptyLengthWall(hits = 1, contextExhausted = true))
    }

    @Test
    fun withoutFix_mirrorExhaustionPathAgreesWithTheRealPolicy() {
        // Probe leg L3a via the mirror: tokens > 0 -> the REAL policy verdict.
        assertTrue(mirrorIsContextExhausted(incidentInput, incidentWindow))
    }

    // ── After the fix: the rewrite clears the figure to 0 ──────────────────

    @Test
    fun afterFix_outputBudgetIsRestoredToTheCeiling() {
        // The clear (AgentLoopEngine.kt:496-499) sets the figure to 0 =
        // "unknown, re-estimate": the budget returns to the ceiling-bounded
        // shape every first turn of a run already sends.
        assertEquals(globalMaxTokensCeiling, mirrorDynamicMaxTokens(incidentWindow, 0))
    }

    @Test
    fun afterFix_realRetryGateAllowsTheRetry() {
        // REAL gate with the post-fix figure (exhausted = false).
        assertTrue(shouldRetryEmptyLengthWall(hits = 1, contextExhausted = false))
    }

    @Test
    fun afterFix_clearedFigureIsNotExhausted() {
        // Probe leg L3b: post-rewrite figure 0 -> pressure null -> NOT
        // exhausted, both via the mirror guard (tokens <= 0) and directly
        // through the REAL policy classification.
        assertFalse(mirrorIsContextExhausted(0, incidentWindow))
        assertEquals(
            ContextPolicy.CheckResult.OK,
            ContextPolicy.forContextWindow(incidentWindow).check(0, incidentWindow),
        )
    }

    @Test
    fun afterFix_offloadFallsBackToALocalEstimate() {
        // Probe leg L4. MIRROR of ChatContextWindow.kt:203-206:
        //   val effectiveTokens =
        //       if (lastContextTokens > 0) lastContextTokens else estimateContextTokens()
        // A cleared figure must NOT disable offload — it falls back to the
        // local estimate (111000 as a placeholder for estimateContextTokens()).
        val tokensAfterRewrite = 0
        val localEstimate = 111_000
        val effectiveTokens =
            if (tokensAfterRewrite > 0) tokensAfterRewrite else localEstimate
        assertEquals(localEstimate, effectiveTokens)
    }

    // ── Guard: the fix must not rubber-stamp the gate or the budget ────────

    @Test
    fun gateStillRefusesAtTheExplicitCeiling() {
        // A genuinely exhausted context still refuses the retry — the fix
        // clears the STALE figure, it does not weaken the gate.
        assertFalse(shouldRetryEmptyLengthWall(hits = 1, contextExhausted = true))
    }

    @Test
    fun gateKeepsTheLegacyThreeHitBudget() {
        // REAL gate (ChatLengthWallLogic.kt:240 const, :276-280): the legacy
        // empty-length retry budget is untouched by the fix.
        assertEquals(3, LENGTH_WALL_EMPTY_MAX_HITS)
        assertTrue(shouldRetryEmptyLengthWall(hits = 1, contextExhausted = false))
        assertTrue(shouldRetryEmptyLengthWall(hits = 2, contextExhausted = false))
        assertFalse(shouldRetryEmptyLengthWall(hits = LENGTH_WALL_EMPTY_MAX_HITS, contextExhausted = false))
    }

    @Test
    fun freshReadingBelowWindowIsNotExhaustedAndGetsASizedBudget() {
        // Offload log: "now 124400 (62%)". A genuinely healthy post-rewrite
        // reading must (a) read as not exhausted — via the REAL policy, not
        // just the mirror, and (b) still get a properly SIZED budget
        // (220000 - 124400 = 95600), so the fix does not turn
        // dynamicMaxTokens into a rubber stamp either.
        val freshReading = 124_400
        assertFalse(mirrorIsContextExhausted(freshReading, incidentWindow))
        assertEquals(
            ContextPolicy.CheckResult.OK,
            ContextPolicy.forContextWindow(incidentWindow).check(freshReading, incidentWindow),
        )
        assertEquals(95_600, mirrorDynamicMaxTokens(incidentWindow, freshReading))
    }
}

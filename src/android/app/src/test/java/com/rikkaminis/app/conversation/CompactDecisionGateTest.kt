package com.rikkaminis.app.conversation

import com.rikkaminis.app.data.ContextPolicy
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/compact-pregate-exhausted-1016] + [fix/compact-escalation-tier-contract-1016]
 *
 * JVM device for the two auto-compact governance fixes on the agent-loop entry.
 *
 * (a) loop pre-gate: `maybeAutoCompactInLoop` used to walk into [ContextCompactor.decide]
 *     only on NEEDS_COMPACT (plus the one-shot offload escalation). Because
 *     [ContextPolicy.check] answers EXHAUSTED the moment `tokens >= window` and
 *     NEEDS_COMPACT only strictly below it, the one state `decide()` adjudicates
 *     into RESCUE / EXHAUSTED could not reach `decide()` from this caller unless
 *     this very turn's offload pass happened to raise the escalation flag. The
 *     once-per-user-action rescue was therefore squeezed behind a door that was
 *     closed exactly when it was needed. [ContextCompactor.loopGateOpens] now
 *     admits EXHAUSTED as well.
 * (b) escalation tier contract: `escalatedFromOffload` bypasses the band check in
 *     `decide()` ([T-ctx-offload-escalation] — the caller proved offload can no
 *     longer shrink the context, so waiting for the compact line only lets it
 *     climb). On an `exhaustedOnly` tier (`compactThreshold == 0`, document
 *     contract "too small for auto-compact; only surface .exhausted") that bypass
 *     fabricated a compact line the tier does not have. It now bypasses the band
 *     only when the tier actually has one.
 *
 * Every expectation below is a LITERAL (JVM self-reference-trap discipline: an
 * expectation derived from the code under test cannot fail when the code
 * changes). The tier threshold table is pinned in the same table by asserting
 * `policy.check` at every coordinate — so a change to either the tier table or
 * `decide`'s reading of it lands here as a red.
 *
 * The send entry (`maybeTriggerAutoCompact`) deliberately does NOT adopt the
 * widened gate: there EXHAUSTED means the send was already blocked and recovery
 * belongs to the context-full dialog (new session / explicit /compact). That
 * asymmetry is pinned by the scan gate `compact_decision_gate_check.py`, not
 * here (it is a source-shape property, not a pure-function one).
 */
class CompactDecisionGateTest {

    private data class Row(
        val tier: String,
        val window: Int,
        val tokens: Int,
        val escalated: Boolean,
        val rescueAttempted: Boolean,
        val expectedCheck: ContextPolicy.CheckResult,
        val expected: ContextCompactor.Decision,
    )

    private fun run(row: Row): ContextCompactor.Decision =
        ContextCompactor.decide(
            estimatedTokens = row.tokens,
            contextWindow = row.window,
            policy = ContextPolicy.forContextWindow(row.window),
            tailTokens = 40_000,
            isCompacting = false,
            lastAutoCompactAtMs = Long.MIN_VALUE,
            rescueAttempted = row.rescueAttempted,
            escalatedFromOffload = row.escalated,
        )

    // ── (a) the loop pre-gate itself ────────────────────────────────────

    @Test
    fun `loopGateOpens admits every verdict decide can act on - EXHAUSTED included`() {
        // EXHAUSTED: the fix. Pre-fix the caller computed `inCompactBand(name)
        // || escalated`, which is FALSE here, so decide() was never reached.
        assertTrue(ContextCompactor.loopGateOpens(ContextPolicy.CheckResult.EXHAUSTED, false))
        assertTrue(ContextCompactor.loopGateOpens(ContextPolicy.CheckResult.EXHAUSTED, true))
        // NEEDS_COMPACT: unchanged.
        assertTrue(ContextCompactor.loopGateOpens(ContextPolicy.CheckResult.NEEDS_COMPACT, false))
        assertTrue(ContextCompactor.loopGateOpens(ContextPolicy.CheckResult.NEEDS_COMPACT, true))
        // OK: only with the escalation exception (the band bypass's entry).
        assertFalse(ContextCompactor.loopGateOpens(ContextPolicy.CheckResult.OK, false))
        assertTrue(ContextCompactor.loopGateOpens(ContextPolicy.CheckResult.OK, true))
    }

    @Test
    fun `EXHAUSTED context now reaches the RESCUE verdict from the loop path`() {
        // The composed claim the fix exists for: gate output AND decide output at
        // the same coordinate. Pre-fix the first term was false, so the second
        // (the only in-loop RESCUE) was unreachable from this caller.
        val window = 128_000
        val verdict = ContextPolicy.forContextWindow(window).check(window, window)
        assertEquals(ContextPolicy.CheckResult.EXHAUSTED, verdict)
        assertTrue(ContextCompactor.loopGateOpens(verdict, false))
        assertEquals(
            ContextCompactor.Decision.RESCUE,
            ContextCompactor.decide(
                estimatedTokens = window,
                contextWindow = window,
                policy = ContextPolicy.forContextWindow(window),
                tailTokens = 40_000,
                isCompacting = false,
                lastAutoCompactAtMs = Long.MIN_VALUE,
                rescueAttempted = false,
            ),
        )
    }

    // ── (b) the decision matrix: 3 tiers × 3 token levels × {escalated} × {rescue} ──
    //
    // Tier windows (ContextPolicy.forContextWindow), pinned literally:
    //   small 50_000  -> exhaustedOnly, compactThreshold 0, offload line 40_000
    //   mid   100_000 -> compact 85_000 (85% cap), offload 75_000 (75% cap)
    //   large 128_000 -> compact 108_000 (W-20K), offload 88_000 (W-40K)
    // Token levels per tier: below the compact line, in the compact band, at the window.
    // rescueAttempted is only read on the at-window branch, so it only multiplies there.

    private val matrix = listOf(
        // ── small tier: exhaustedOnly, no compact line at all ──
        Row("small", 50_000, 30_000, false, true, ContextPolicy.CheckResult.OK, ContextCompactor.Decision.OK),
        Row("small", 50_000, 30_000, true, true, ContextPolicy.CheckResult.OK, ContextCompactor.Decision.OK),
        // 45_000 is EXHAUSTED by the offload-as-exhaust-line rule, yet BELOW the
        // window: decide() must still refuse — this tier has no compact line, and
        // escalation must not invent one. (Pre-fix, escalated=true -> AUTO_COMPACT
        // here: that is the (b) bug, and the mutation-control arm.)
        Row("small", 50_000, 45_000, false, true, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.OK),
        Row("small", 50_000, 45_000, true, true, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.OK),
        // At the window every tier hands the ceiling to the rescue gate.
        Row("small", 50_000, 50_000, false, false, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.RESCUE),
        Row("small", 50_000, 50_000, false, true, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.EXHAUSTED),
        Row("small", 50_000, 50_000, true, false, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.RESCUE),
        Row("small", 50_000, 50_000, true, true, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.EXHAUSTED),

        // ── mid tier: has a compact line (85_000) ──
        Row("mid", 100_000, 60_000, false, true, ContextPolicy.CheckResult.OK, ContextCompactor.Decision.OK),
        // escalation still works where a compact line exists: OK + escalated -> compact.
        Row("mid", 100_000, 60_000, true, true, ContextPolicy.CheckResult.OK, ContextCompactor.Decision.AUTO_COMPACT),
        Row("mid", 100_000, 90_000, false, true, ContextPolicy.CheckResult.NEEDS_COMPACT, ContextCompactor.Decision.AUTO_COMPACT),
        Row("mid", 100_000, 90_000, true, true, ContextPolicy.CheckResult.NEEDS_COMPACT, ContextCompactor.Decision.AUTO_COMPACT),
        Row("mid", 100_000, 100_000, false, false, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.RESCUE),
        Row("mid", 100_000, 100_000, false, true, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.EXHAUSTED),
        Row("mid", 100_000, 100_000, true, false, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.RESCUE),
        Row("mid", 100_000, 100_000, true, true, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.EXHAUSTED),

        // ── large tier: compact 108_000 ──
        Row("large", 128_000, 80_000, false, true, ContextPolicy.CheckResult.OK, ContextCompactor.Decision.OK),
        Row("large", 128_000, 80_000, true, true, ContextPolicy.CheckResult.OK, ContextCompactor.Decision.AUTO_COMPACT),
        Row("large", 128_000, 110_000, false, true, ContextPolicy.CheckResult.NEEDS_COMPACT, ContextCompactor.Decision.AUTO_COMPACT),
        Row("large", 128_000, 110_000, true, true, ContextPolicy.CheckResult.NEEDS_COMPACT, ContextCompactor.Decision.AUTO_COMPACT),
        Row("large", 128_000, 128_000, false, false, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.RESCUE),
        Row("large", 128_000, 128_000, false, true, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.EXHAUSTED),
        Row("large", 128_000, 128_000, true, false, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.RESCUE),
        Row("large", 128_000, 128_000, true, true, ContextPolicy.CheckResult.EXHAUSTED, ContextCompactor.Decision.EXHAUSTED),
    )

    @Test
    fun `decision matrix - tier check is pinned and decide follows the tier contract`() {
        for (row in matrix) {
            val policy = ContextPolicy.forContextWindow(row.window)
            val where = "${row.tier} window=${row.window} tokens=${row.tokens} " +
                "escalated=${row.escalated} rescueAttempted=${row.rescueAttempted}"
            // 1) the tier table itself (so a threshold edit cannot silently move the matrix)
            assertEquals("check @ $where", row.expectedCheck, policy.check(row.tokens, row.window))
            // 2) the adjudication
            assertEquals("decide @ $where", row.expected, run(row))
        }
    }

    @Test
    fun `the matrix actually covers the fix's two arms`() {
        // Guard against a future edit that trims the table down to rows which
        // cannot distinguish pre-fix from post-fix behaviour.
        val smallEscalated = matrix.count { it.tier == "small" && it.escalated && it.expected == ContextCompactor.Decision.OK }
        val rescueRows = matrix.count { it.expected == ContextCompactor.Decision.RESCUE }
        val exhaustedRows = matrix.count { it.expected == ContextCompactor.Decision.EXHAUSTED }
        val autoRows = matrix.count { it.expected == ContextCompactor.Decision.AUTO_COMPACT }
        assertEquals("small-tier escalation-refusal arms", 2, smallEscalated)
        assertEquals("rescue arms", 6, rescueRows)
        assertEquals("exhausted arms", 6, exhaustedRows)
        assertEquals("auto-compact arms", 6, autoRows)
    }
}

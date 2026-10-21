package com.rikkaminis.app.browser

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/dv17-1 B-01] (新增) the challenge renderer that carries risk-challenge
 * information to the agent.
 *
 * Why this test exists: the four structured fields on BrowserActionResult
 * (riskChallengeDetected / riskChallengeKind / recommendedNextAction /
 * throttleDelayMs) were written by BrowserTabPool and read by nobody —
 * ChatToolExecutors consumes `result.text` alone — so a Cloudflare / captcha /
 * 429 page reached the model as undistinguished text and it retried blindly.
 * [BrowserRiskControl.challengeNoticeLine] is the line that now lands in that
 * text; these assertions are the literal contract of that line.
 *
 * Every expectation below is a hand-written literal (never derived from the
 * function under test or from its constants), so changing the wording without
 * meaning to trips here instead of silently degrading the agent's signal.
 */
class BrowserRiskChallengeTextTest {

    @Test
    fun noChallengeRendersNoLine() {
        // Absent / blank halves mean "nothing to report": the caller must not
        // append anything, or every browser result would carry noise.
        assertNull(
            BrowserRiskControl.challengeNoticeLine(
                kind = null,
                recommendedNextAction = "wait_before_retrying_and_reduce_request_rate",
                throttleDelayMs = 0L,
            ),
        )
        assertNull(
            BrowserRiskControl.challengeNoticeLine(
                kind = "cloudflare_challenge",
                recommendedNextAction = null,
                throttleDelayMs = 0L,
            ),
        )
        assertNull(
            BrowserRiskControl.challengeNoticeLine(
                kind = "   ",
                recommendedNextAction = "ask_user_to_complete_verification_manually",
                throttleDelayMs = 0L,
            ),
        )
        assertNull(
            BrowserRiskControl.challengeNoticeLine(
                kind = "cloudflare_challenge",
                recommendedNextAction = "  ",
                throttleDelayMs = 0L,
            ),
        )
    }

    @Test
    fun cloudflareChallengeNamesKindAndNextAction() {
        assertEquals(
            "[risk] cloudflare_challenge — next: ask_user_to_complete_verification_manually",
            BrowserRiskControl.challengeNoticeLine(
                kind = "cloudflare_challenge",
                recommendedNextAction = "ask_user_to_complete_verification_manually",
                throttleDelayMs = 0L,
            ),
        )
    }

    @Test
    fun rateLimitedChallengeNamesKindAndNextAction() {
        assertEquals(
            "[risk] rate_limited — next: wait_before_retrying_and_reduce_request_rate",
            BrowserRiskControl.challengeNoticeLine(
                kind = "rate_limited",
                recommendedNextAction = "wait_before_retrying_and_reduce_request_rate",
                throttleDelayMs = 0L,
            ),
        )
    }

    @Test
    fun accessDeniedChallengeNamesKindAndNextAction() {
        assertEquals(
            "[risk] access_denied — next: stop_automatic_retry_and_use_manual_access",
            BrowserRiskControl.challengeNoticeLine(
                kind = "access_denied",
                recommendedNextAction = "stop_automatic_retry_and_use_manual_access",
                throttleDelayMs = 0L,
            ),
        )
    }

    @Test
    fun throttleDelayIsSurfacedOnlyWhenPositive() {
        assertEquals(
            "[risk] captcha_challenge — next: ask_user_to_complete_verification_manually",
            BrowserRiskControl.challengeNoticeLine(
                kind = "captcha_challenge",
                recommendedNextAction = "ask_user_to_complete_verification_manually",
                throttleDelayMs = 0L,
            ),
        )
        assertEquals(
            "[risk] rate_limited — next: wait_before_retrying_and_reduce_request_rate (throttled 730ms)",
            BrowserRiskControl.challengeNoticeLine(
                kind = "rate_limited",
                recommendedNextAction = "wait_before_retrying_and_reduce_request_rate",
                throttleDelayMs = 730L,
            ),
        )
    }

    @Test
    fun theDetectorOutputRendersIntoTheExpectedLine() {
        // End-to-end over the two public halves: what detectChallenge() reports
        // is exactly what the agent then reads in result.text.
        val challenge = BrowserRiskControl.detectChallenge(statusCode = 429)!!
        val line = BrowserRiskControl.challengeNoticeLine(
            kind = challenge.kind,
            recommendedNextAction = challenge.recommendedNextAction,
            throttleDelayMs = 0L,
        )
        assertEquals(
            "[risk] rate_limited — next: wait_before_retrying_and_reduce_request_rate",
            line,
        )
        val forbidden = BrowserRiskControl.detectChallenge(statusCode = 403)!!
        assertEquals(
            "[risk] access_denied — next: stop_automatic_retry_and_use_manual_access",
            BrowserRiskControl.challengeNoticeLine(
                kind = forbidden.kind,
                recommendedNextAction = forbidden.recommendedNextAction,
                throttleDelayMs = 0L,
            ),
        )
    }

    @Test
    fun theLineCarriesBothHalvesSeparately() {
        val line = BrowserRiskControl.challengeNoticeLine(
            kind = "search_engine_challenge",
            recommendedNextAction = "ask_user_to_complete_verification_manually",
            throttleDelayMs = 0L,
        )!!
        assertTrue(line, line.contains("search_engine_challenge"))
        assertTrue(line, line.contains("ask_user_to_complete_verification_manually"))
    }
}

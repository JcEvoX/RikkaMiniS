package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** [feat/continuous-work] Pure policy tests. */
class ContinuousWorkPolicyTest {

    private fun shouldContinue(
        ranToolThisRun: Boolean = true,
        finalText: String = "working on it",
        roundsUsed: Int = 0,
        maxRounds: Int = 5,
        terminalError: Boolean = false,
    ) = ContinuousWorkPolicy.shouldContinue(
        ranToolThisRun = ranToolThisRun,
        finalText = finalText,
        roundsUsed = roundsUsed,
        maxRounds = maxRounds,
        terminalError = terminalError,
    )

    // ── gating ─────────────────────────────────────────────────────────────

    @Test
    fun `disabled max never continues`() {
        assertFalse(shouldContinue(maxRounds = 0))
        assertFalse(shouldContinue(maxRounds = -1))
    }

    @Test
    fun `terminal error never continues`() {
        assertFalse(shouldContinue(terminalError = true))
    }

    @Test
    fun `exhausted rounds never continue`() {
        assertFalse(shouldContinue(roundsUsed = 5, maxRounds = 5))
        assertFalse(shouldContinue(roundsUsed = 6, maxRounds = 5))
    }

    @Test
    fun `plain chat without tools never continues`() {
        assertFalse(shouldContinue(ranToolThisRun = false, finalText = "Hello! How can I help?"))
    }

    @Test
    fun `too-short reply never continues`() {
        assertFalse(shouldContinue(finalText = "ok"))
        assertFalse(shouldContinue(finalText = "   "))
    }

    // ── done markers ───────────────────────────────────────────────────────

    @Test
    fun `done markers stop auto-continue`() {
        for (marker in listOf("[DONE]", "[任务完成]", "任务已完成", "全部完成", "已完成所有", "没有更多", "无需继续")) {
            assertTrue("expected done: $marker", ContinuousWorkPolicy.looksDone("$marker — everything is in place"))
            assertFalse("expected stop: $marker", shouldContinue(finalText = "$marker — everything is in place"))
        }
    }

    @Test
    fun `done marker match is case insensitive`() {
        assertTrue(ContinuousWorkPolicy.looksDone("all good [done]"))
        assertFalse(shouldContinue(finalText = "all good [done]"))
    }

    // ── happy path ─────────────────────────────────────────────────────────

    @Test
    fun `tool run with unfinished reply continues`() {
        assertTrue(shouldContinue(roundsUsed = 0, maxRounds = 5))
        assertTrue(shouldContinue(roundsUsed = 4, maxRounds = 5))
    }

    @Test
    fun `continue prompt carries the done escape hatch`() {
        val prompt = ContinuousWorkPolicy.continuePrompt()
        assertTrue(prompt.contains("[DONE]"))
        assertTrue(prompt.contains("Auto-continue"))
    }
}
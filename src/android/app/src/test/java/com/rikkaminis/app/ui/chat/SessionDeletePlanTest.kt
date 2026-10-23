package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv27-B4 / deep-audit-1047 B-06] — the UI-initiated session deletion used
 * to remove the row + the recursive session directory FIRST and only then
 * release the resident [ChatViewModel] from `ChatViewModelStore`.
 *
 * That order left two live windows:
 *
 *   1. The resident view model kept its `streamJob` and its `BrowserTabPool`,
 *      so a write landing during the delete recreated
 *      `minis-sessions/<id>/…` / `browser_tabs/<id>.json` inside the tree that
 *      had just been removed — the deleted session came back as an empty ghost
 *      directory.
 *   2. `release` cancels the stream's `viewModelScope`; the cancelled job's
 *      `finally` then publishes an overlay excerpt / sets the session inactive
 *      on IO *after* `SessionBadgeStore.clear(id)` had already run, so the
 *      deleted id lit its completion badge / floating excerpt back up.
 *
 * The order now lives in exactly one place — [sessionDeletePlan] — so it can
 * be pinned as behaviour instead of being re-derived from a Compose-only file.
 *
 * The expectations below are LITERALS on purpose: deriving them from the
 * production constants would make a mutation of those constants look like a
 * PASS (the JVM-harness self-reference trap).
 */
class SessionDeletePlanTest {

    @Test
    fun `the plan is release then delete then clear`() {
        assertEquals(
            listOf(
                SessionDeleteStep.RELEASE_VIEW_MODEL,
                SessionDeleteStep.DELETE_ROW,
                SessionDeleteStep.CLEAR_BADGE,
            ),
            sessionDeletePlan(),
        )
    }

    @Test
    fun `the release step comes strictly before the delete step`() {
        val order = sessionDeletePlan()
        val release = order.indexOf(SessionDeleteStep.RELEASE_VIEW_MODEL)
        val delete = order.indexOf(SessionDeleteStep.DELETE_ROW)
        val clear = order.indexOf(SessionDeleteStep.CLEAR_BADGE)
        assertTrue("release must be present in the plan", release >= 0)
        assertTrue("delete must be present in the plan", delete >= 0)
        assertTrue("clear must be present in the plan", clear >= 0)
        assertTrue(
            "release ($release) must precede delete ($delete) — deleting first " +
                "leaves the producer running and resurrects the deleted tree",
            release < delete,
        )
    }

    @Test
    fun `the badge is cleared after the row is gone`() {
        val order = sessionDeletePlan()
        assertTrue(
            "clear (${order.indexOf(SessionDeleteStep.CLEAR_BADGE)}) must come " +
                "after delete (${order.indexOf(SessionDeleteStep.DELETE_ROW)}) — " +
                "clearing first would leave a live session unbadged when the " +
                "delete fails",
            order.indexOf(SessionDeleteStep.CLEAR_BADGE) >
                order.indexOf(SessionDeleteStep.DELETE_ROW),
        )
    }

    @Test
    fun `the plan has no duplicate and no unknown step`() {
        val order = sessionDeletePlan()
        assertEquals("the plan must not repeat a step", order.size, order.toSet().size)
        assertEquals(
            "the plan must cover exactly the three known steps",
            setOf(
                SessionDeleteStep.RELEASE_VIEW_MODEL,
                SessionDeleteStep.DELETE_ROW,
                SessionDeleteStep.CLEAR_BADGE,
            ),
            order.toSet(),
        )
    }

    /**
     * Negative control: the BASELINE order (delete → release → clear) must
     * fail the ordering assertion this class exists to protect. If the
     * baseline shape ever passes, the assertion has lost its teeth.
     */
    @Test
    fun `negative control - the baseline order fails the release-before-delete check`() {
        val baseline = listOf(
            SessionDeleteStep.DELETE_ROW,
            SessionDeleteStep.RELEASE_VIEW_MODEL,
            SessionDeleteStep.CLEAR_BADGE,
        )
        val release = baseline.indexOf(SessionDeleteStep.RELEASE_VIEW_MODEL)
        val delete = baseline.indexOf(SessionDeleteStep.DELETE_ROW)
        assertTrue(
            "the baseline order must NOT satisfy release-before-delete",
            !(release < delete),
        )
    }
}

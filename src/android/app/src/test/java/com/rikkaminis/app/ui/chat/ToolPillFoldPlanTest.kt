package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [T-android-tool-pill-fold] JVM tests for the tool-pill fold decision.
 *
 * The numbers here are LITERALS on purpose: 8 inline / fold above 12 IS the
 * contract this change ships with, so retuning either constant must be a
 * deliberate edit to this file too. Expectations derived from the constant
 * under test would let a mutation (e.g. `isPillVisible` flipping `<` to `<=`)
 * pass silently.
 *
 * The two properties that actually matter:
 *  - a long turn's pill wall is bounded (fold actually applies), and
 *  - nothing is ever dropped: inline pills + hiddenCount == total, always.
 */
class ToolPillFoldPlanTest {

    @Test fun `frozen 300-pill turn folds to 8 inline and reports the rest`() {
        val plan = planToolPillFold(isStreaming = false, totalPills = 300, expanded = false)
        assertTrue(plan.foldable)
        assertTrue(plan.applied)
        assertEquals(292, plan.hiddenCount)
        assertTrue(plan.isPillVisible(0))
        assertTrue(plan.isPillVisible(7))
        assertFalse(plan.isPillVisible(8))
        assertFalse(plan.isPillVisible(299))
    }

    @Test fun `streaming turn never folds — the live pill stays on screen`() {
        val plan = planToolPillFold(isStreaming = true, totalPills = 300, expanded = false)
        assertFalse(plan.foldable)
        assertFalse(plan.applied)
        assertEquals(0, plan.hiddenCount)
        assertTrue(plan.isPillVisible(8))
        assertTrue(plan.isPillVisible(299))
    }

    @Test fun `expanding brings every pill back but keeps the toggle row legitimate`() {
        val plan = planToolPillFold(isStreaming = false, totalPills = 300, expanded = true)
        assertTrue(plan.foldable)
        assertFalse(plan.applied)
        // Still 292: the row stays put and offers the collapse-back toggle.
        assertEquals(292, plan.hiddenCount)
        assertTrue(plan.isPillVisible(8))
        assertTrue(plan.isPillVisible(299))
    }

    @Test fun `below the trigger the fold must not tax the common turn`() {
        // 9 pills is an ordinary agent turn — all of them render.
        val nine = planToolPillFold(isStreaming = false, totalPills = 9, expanded = false)
        assertFalse(nine.foldable)
        assertEquals(0, nine.hiddenCount)
        assertTrue(nine.isPillVisible(8))
        // Exactly at the trigger is still below it (trigger means "more than").
        val twelve = planToolPillFold(isStreaming = false, totalPills = 12, expanded = false)
        assertFalse(twelve.foldable)
        assertTrue(twelve.isPillVisible(11))
    }

    @Test fun `one past the trigger folds the tail`() {
        val plan = planToolPillFold(isStreaming = false, totalPills = 13, expanded = false)
        assertTrue(plan.foldable)
        assertTrue(plan.applied)
        assertEquals(5, plan.hiddenCount)
        assertTrue(plan.isPillVisible(7))
        assertFalse(plan.isPillVisible(8))
    }

    @Test fun `no content is dropped for any pill count`() {
        // Ranges and counts are literals, not derived from the plan under
        // test: <=12 pills render in full, >=13 render 8 + a fold row.
        for (total in 0..12) {
            val plan = planToolPillFold(isStreaming = false, totalPills = total, expanded = false)
            val inline = (0 until total).count { plan.isPillVisible(it) }
            assertFalse("total=$total", plan.foldable)
            assertFalse("total=$total", plan.applied)
            assertEquals("total=$total", total, inline)
            assertEquals("total=$total", 0, plan.hiddenCount)
        }
        for (total in 13..40) {
            val plan = planToolPillFold(isStreaming = false, totalPills = total, expanded = false)
            val inline = (0 until total).count { plan.isPillVisible(it) }
            assertTrue("total=$total", plan.applied)
            assertEquals("total=$total", 8, inline)
            assertEquals("total=$total", total - 8, plan.hiddenCount)
            // The fold never loses a pill: inline + hidden covers the turn.
            assertEquals("total=$total", total, inline + plan.hiddenCount)
        }
    }

    @Test fun `empty and degenerate counts are safe`() {
        val empty = planToolPillFold(isStreaming = false, totalPills = 0, expanded = false)
        assertFalse(empty.foldable)
        assertEquals(0, empty.hiddenCount)
        val negative = planToolPillFold(isStreaming = false, totalPills = -3, expanded = false)
        assertFalse(negative.foldable)
        assertEquals(0, negative.hiddenCount)
    }

    @Test fun `trigger and limit are injectable so the seam stays honest`() {
        val plan = planToolPillFold(
            isStreaming = false,
            totalPills = 4,
            expanded = false,
            trigger = 3,
            limit = 2,
        )
        assertTrue(plan.foldable)
        assertTrue(plan.applied)
        assertEquals(2, plan.hiddenCount)
        assertTrue(plan.isPillVisible(1))
        assertFalse(plan.isPillVisible(2))
        // Degenerate seam: a limit above the pill count must clamp the label
        // to 0 rather than announce a negative number of hidden pills.
        val clamped = planToolPillFold(
            isStreaming = false,
            totalPills = 3,
            expanded = false,
            trigger = 0,
            limit = 8,
        )
        assertTrue(clamped.foldable)
        assertEquals(0, clamped.hiddenCount)
    }
}

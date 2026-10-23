package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1047 B-04] The group-selection decision, tested as the value it
 * is rather than through a `ChatViewModel` (which cannot be instantiated off
 * device).
 *
 * Each test below names the defect it pins, so a failure tells you which half
 * of the disconnect came back:
 *  - the top bar showing a group that never resolved (C-SEL-1);
 *  - the session binding to the member the user *asked for* instead of the one
 *    resolution actually landed on (C-SEL-2).
 */
class GroupSelectionPlanTest {

    // ── C-SEL-1: an unresolved group must not publish ───────────────────────

    @Test
    fun `an unresolved group publishes nothing`() {
        val plan = planGroupSelect(resolved = false, resolvedId = "resolved", requestedId = "requested")
        assertFalse(
            "a group that resolved to nothing must leave the previous selection alone",
            plan.applyUi,
        )
    }

    @Test
    fun `an unresolved group carries no entry id at all`() {
        val plan = planGroupSelect(resolved = false, resolvedId = "resolved", requestedId = "requested")
        assertNull(
            "an unresolved group has no serving member, so there is no id to persist",
            plan.effectiveEntryId,
        )
    }

    @Test
    fun `an unresolved group publishes nothing even when resolution left an id behind`() {
        // The dangerous shape: resolution ran, published _activeEntryId from an
        // EARLIER turn, and then declined. Carrying that stale id forward would
        // bind the session to a member the user never picked in this call.
        val plan = planGroupSelect(resolved = false, resolvedId = "carried-over", requestedId = "requested")
        assertFalse("unresolved wins over any id", plan.applyUi)
        assertNull("unresolved wins over any id", plan.effectiveEntryId)
    }

    // ── C-SEL-2: one id per call, and it is the resolved one ────────────────

    @Test
    fun `a fall back hands back the member that actually serves`() {
        val plan = planGroupSelect(resolved = true, resolvedId = "resolved", requestedId = "requested")
        assertEquals(
            "resolution may fall back off the member the user tapped, so the "
                + "resolved id — not the requested one — is what every consumer "
                + "must write",
            "resolved",
            plan.effectiveEntryId,
        )
    }

    @Test
    fun `a resolved pick is published`() {
        val plan = planGroupSelect(resolved = true, resolvedId = "resolved", requestedId = "requested")
        assertTrue("a resolved group is the one case that may move the top bar", plan.applyUi)
    }

    @Test
    fun `a resolution that named no entry falls back to the requested one`() {
        // resolveProviderFromGroup(groupId) with no preferred member: nothing
        // was requested, but the group did resolve — the caller's id is all we
        // have, and it is the id the caller is about to activate anyway.
        val plan = planGroupSelect(resolved = true, resolvedId = null, requestedId = "requested")
        assertTrue(plan.applyUi)
        assertEquals("requested", plan.effectiveEntryId)
    }

    @Test
    fun `the plan is a value so two calls with the same inputs agree`() {
        val a = planGroupSelect(resolved = true, resolvedId = "x", requestedId = "y")
        val b = planGroupSelect(resolved = true, resolvedId = "x", requestedId = "y")
        assertEquals("the plan must be pure — callers re-derive it", a, b)
        assertEquals("applyUi and the id stay consistent", a.effectiveEntryId != null, a.applyUi)
    }
}

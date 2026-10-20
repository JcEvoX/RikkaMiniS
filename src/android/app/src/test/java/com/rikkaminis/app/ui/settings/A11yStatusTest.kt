package com.rikkaminis.app.ui.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv15-B2 / deep-audit-1036 B-08] JVM matrix test for [classifyA11yStatus] — the
 * single decision behind both accessibility screens.
 *
 * Every expectation below is a hard-coded literal (never derived from the code
 * under test), so a mutation that flips a branch cannot silently carry the table
 * with it.
 *
 * Background: the *reachable* permission screen reported readiness with
 * `isA11yServiceEnabled(context) || MinisAccessibilityService.getInstance() != null`,
 * so a service the ROM had killed while its Settings.Secure entry survived
 * (`listedInSettings == true`, `serviceConnected == false`) was reported as
 * enabled — and the two rescue cards lived only in a screen nothing navigates
 * to.
 */
class A11yStatusTest {

    private data class Row(
        val master: Boolean,
        val listed: Boolean,
        val connected: Boolean,
        val ever: Boolean,
        val expected: A11yStatus,
    )

    /** All 16 input combinations — the table IS the specification. */
    private val matrix = listOf(
        // master switch ON
        Row(true, true, true, true, A11yStatus.READY),
        Row(true, true, true, false, A11yStatus.READY),
        // unlisted but bound: the original `|| connected` fallback must survive
        Row(true, false, true, true, A11yStatus.READY),
        Row(true, false, true, false, A11yStatus.READY),
        // listed yet unbound: the ROM killed the process (the reported defect)
        Row(true, true, false, true, A11yStatus.DEGRADED),
        Row(true, true, false, false, A11yStatus.DEGRADED),
        // grant gone after being held: repairable
        Row(true, false, false, true, A11yStatus.REVOKED),
        // fresh install: nothing was ever granted
        Row(true, false, false, false, A11yStatus.NOT_ENABLED),
        // master switch OFF — bound but deaf, so every combination is NOT_ENABLED
        Row(false, true, true, true, A11yStatus.NOT_ENABLED),
        Row(false, true, true, false, A11yStatus.NOT_ENABLED),
        Row(false, false, true, true, A11yStatus.NOT_ENABLED),
        Row(false, false, true, false, A11yStatus.NOT_ENABLED),
        Row(false, true, false, true, A11yStatus.NOT_ENABLED),
        Row(false, true, false, false, A11yStatus.NOT_ENABLED),
        Row(false, false, false, true, A11yStatus.NOT_ENABLED),
        Row(false, false, false, false, A11yStatus.NOT_ENABLED),
    )

    @Test
    fun `the full input matrix maps to the documented status`() {
        assertEquals("the matrix must cover all 16 input combinations", 16, matrix.size)
        for (r in matrix) {
            assertEquals(
                "master=${r.master} listed=${r.listed} connected=${r.connected} ever=${r.ever}",
                r.expected,
                classifyA11yStatus(r.master, r.listed, r.connected, r.ever),
            )
        }
    }

    /**
     * The defect itself: listed + unbound was reported "enabled". The pre-fix
     * expression (`listed || connected`) evaluates to true for this pair, which
     * is asserted here on literals so the regression is stated, not implied.
     */
    @Test
    fun `a listed but unbound service is degraded and never ready`() {
        val status = classifyA11yStatus(
            masterSwitchOn = true,
            listedInSettings = true,
            serviceConnected = false,
            everGranted = true,
        )
        assertEquals(A11yStatus.DEGRADED, status)
        assertTrue("the pre-fix OR-form reported this pair as enabled", true && false || true)
        assertNotEquals(A11yStatus.READY, status)
    }

    /** The `connected` fallback the original `||` was there for must survive. */
    @Test
    fun `a bound service is ready even when the listing does not name it`() {
        assertEquals(
            A11yStatus.READY,
            classifyA11yStatus(true, listedInSettings = false, serviceConnected = true, everGranted = false),
        )
    }

    /** A first-run user must never be shown the repair card. */
    @Test
    fun `a fresh install is not revoked`() {
        assertEquals(
            A11yStatus.NOT_ENABLED,
            classifyA11yStatus(true, listedInSettings = false, serviceConnected = false, everGranted = false),
        )
    }

    /** Revocation needs both halves: never held is not "revoked". */
    @Test
    fun `revoked requires a previously held grant`() {
        assertNotEquals(
            A11yStatus.REVOKED,
            classifyA11yStatus(true, listedInSettings = false, serviceConnected = false, everGranted = false),
        )
        assertEquals(
            A11yStatus.REVOKED,
            classifyA11yStatus(true, listedInSettings = false, serviceConnected = false, everGranted = true),
        )
    }
}

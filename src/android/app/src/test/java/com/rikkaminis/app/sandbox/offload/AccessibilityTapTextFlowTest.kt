package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1017 G3] The decision table `tap text` now goes through.
 *
 * Before: `if (n.isClickable) n.performAction(ACTION_CLICK) else tapXYRaw(...)`
 * with both results discarded and `ok` returned unconditionally — the CLI
 * reported a landed tap for nodes that refused the action and for gestures the
 * system cancelled. The table is Android-free (`AccessibilityNodeInfo` cannot be
 * instantiated on the JVM), which is exactly the boundary the fix introduced.
 */
class AccessibilityTapTextFlowTest {

    @Test
    fun `a clickable node that accepts ACTION_CLICK needs no fallback`() {
        assertEquals(TapTextFlow.Stage.CLICKED, TapTextFlow.plan(true, true))
    }

    @Test
    fun `a clickable node that refuses ACTION_CLICK falls back to coordinates`() {
        assertEquals(TapTextFlow.Stage.COORDINATE_FALLBACK, TapTextFlow.plan(true, false))
    }

    @Test
    fun `a node that does not claim to be clickable taps its centre`() {
        assertEquals(TapTextFlow.Stage.COORDINATE_FALLBACK, TapTextFlow.plan(false, false))
    }

    @Test
    fun `a non-clickable node is never reported as clicked`() {
        // performAction is not even offered in this case; a stale `true` must not
        // short-circuit the coordinate tap into a claimed click.
        assertEquals(TapTextFlow.Stage.COORDINATE_FALLBACK, TapTextFlow.plan(false, true))
    }

    @Test
    fun `only exit code 0 counts as a successful tap`() {
        assertTrue(TapTextFlow.fallbackSucceeded(0))
        assertFalse(TapTextFlow.fallbackSucceeded(1))
        assertFalse(TapTextFlow.fallbackSucceeded(2))
        assertFalse(TapTextFlow.fallbackSucceeded(-1))
    }
}

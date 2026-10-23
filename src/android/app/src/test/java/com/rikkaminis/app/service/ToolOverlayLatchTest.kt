package com.rikkaminis.app.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM tests for the floating-overlay **intent latch** (deep-audit-1046 S-06).
 *
 * The bug: `ToolOverlayController.show()` publishes the capsule from a
 * **posted** Runnable, so `isShown` (flipped only once that Runnable reaches
 * `attach()`) is false for at least one main-thread queue hop. All three
 * hide sites were gated on it (`if (isShown) hide()`), so a state flip
 * landing inside that hop — app came to the foreground, camera suppress,
 * toggle off, task finished — was dropped, and the still-queued attach then
 * painted the capsule over the chat or the camera preview with no later
 * state frame to clear it.
 *
 * The fix publishes the intent synchronously at both entry points and
 * re-reads it inside the posted block. [OverlayQueueSim] below is a pure
 * transcription of that latch (the WindowManager half of the controller is
 * Android-only and cannot run on a JVM), and it drives the **real**
 * production decision functions from `ToolOverlayPolicy.kt`.
 *
 * Every expectation is a literal — none is derived from the code under
 * test — so a mutation of the rule fails here instead of masquerading as a
 * pass. The wiring itself (which entry point writes the latch, and where the
 * guard sits relative to `attach()`) is pinned by the gate
 * `scripts/scan/overlay_show_hide_latch_check.py`.
 */
class ToolOverlayLatchTest {

    /**
     * Pure model of `ToolOverlayController`'s main-thread queue: `show()`
     * publishes the intent and queues an attach; `hide()` clears the intent
     * and queues a detach. The order of the queue is the whole point.
     */
    private class OverlayQueueSim {
        private val queue = ArrayDeque<() -> Unit>()

        /** The latch the controller publishes synchronously at its entries. */
        var desired: Boolean = false

        /** The controller's real attach state (flipped inside attach()). */
        var isShown: Boolean = false

        var attached: Boolean = false
        var attachCount: Int = 0

        fun show() {
            desired = true
            queue.addLast {
                // the posted block re-reads the latch BEFORE attaching
                if (!shouldRunShowPost(desired)) return@addLast
                if (!attached) {
                    attached = true
                    isShown = true
                    attachCount++
                }
            }
        }

        fun hide() {
            desired = false
            queue.addLast {
                if (attached) {
                    attached = false
                    isShown = false
                }
            }
        }

        fun drain() {
            while (queue.isNotEmpty()) queue.removeFirst().invoke()
        }
    }

    @Test
    fun hideInsideTheQueueHopCancelsThePendingAttach() {
        val sim = OverlayQueueSim()
        sim.show()
        sim.hide()
        sim.drain()
        assertEquals(
            "attach() must never run when the intent was withdrawn first",
            0,
            sim.attachCount,
        )
        assertFalse("no capsule may be left on screen", sim.attached)
    }

    @Test
    fun showThenDrainAttaches() {
        val sim = OverlayQueueSim()
        sim.show()
        sim.drain()
        assertTrue("a live intent must still paint the capsule", sim.attached)
        assertEquals("exactly one attach for one show", 1, sim.attachCount)
    }

    @Test
    fun lastIntentWins() {
        val sim = OverlayQueueSim()
        sim.show()
        sim.hide()
        sim.show()
        sim.drain()
        assertTrue("re-showing inside one hop must end attached", sim.attached)
        // two attaches, one detach: the first show paints, the hide detaches,
        // the second show paints again — and the capsule ends up ON SCREEN,
        // which is the point: the LAST intent is the one that survives.
        assertEquals(2, sim.attachCount)
    }

    @Test
    fun withdrawnHideDoesNotSuppressALaterShow() {
        val sim = OverlayQueueSim()
        sim.hide()
        sim.show()
        sim.drain()
        assertTrue("a withdrawn hide must not suppress a later show", sim.attached)
        assertEquals(1, sim.attachCount)
    }

    @Test
    fun doubleHideIsHarmless() {
        val sim = OverlayQueueSim()
        sim.hide()
        sim.hide()
        sim.drain()
        assertFalse(sim.attached)
        assertEquals(0, sim.attachCount)
    }

    // ---- the real production decision functions -------------------------

    @Test
    fun shouldRunShowPostFollowsTheLatch() {
        assertTrue(shouldRunShowPost(true))
        assertFalse(shouldRunShowPost(false))
    }

    @Test
    fun resolveOverlayIntentRefusesForegroundAndCamera() {
        // busy, permitted, backgrounded, no camera → shown
        assertTrue(
            resolveOverlayIntent(
                wantShown = true,
                permission = true,
                foreground = false,
                camera = false,
                hasPending = false,
            ),
        )
        // the very same state with the app in the foreground → hidden
        assertFalse(
            resolveOverlayIntent(
                wantShown = true,
                permission = true,
                foreground = true,
                camera = false,
                hasPending = false,
            ),
        )
        // camera suppress wins over a running tool
        assertFalse(
            resolveOverlayIntent(
                wantShown = true,
                permission = true,
                foreground = false,
                camera = true,
                hasPending = false,
            ),
        )
        // no overlay permission → hidden
        assertFalse(
            resolveOverlayIntent(
                wantShown = true,
                permission = false,
                foreground = false,
                camera = false,
                hasPending = false,
            ),
        )
    }

    @Test
    fun resolveOverlayIntentKeepsTheCompletionLinger() {
        // nothing running, but a completion is pending → the capsule lingers
        assertTrue(
            resolveOverlayIntent(
                wantShown = false,
                permission = true,
                foreground = false,
                camera = false,
                hasPending = true,
            ),
        )
        // nothing running and nothing pending → hidden
        assertFalse(
            resolveOverlayIntent(
                wantShown = false,
                permission = true,
                foreground = false,
                camera = false,
                hasPending = false,
            ),
        )
    }
}

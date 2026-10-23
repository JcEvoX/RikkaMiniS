package com.rikkaminis.app.ui.preview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1046 S-07] Behavioural half of the offscreen-WebView teardown
 * contract. The source-shape half ("both hosts latch before they touch the
 * view, both deferred bodies are guarded, no bare `removeCallbacks(null)`") is
 * pinned mechanically by `scripts/scan/browser_destroy_wakeup_check.py`.
 *
 * Bug context: `AndroidView.onRelease` in `KaTeXView.kt` / `FilePreviewScreen.kt`
 * called `removeCallbacks(null)`, which AOSP defines as a no-op (it is guarded on
 * `action != null`). A capture block scheduled 100 ms before the row left the
 * composition therefore still ran on the destroyed WebView: `draw()` produced an
 * empty bitmap and it was written into the process-wide `KaTeXRendererCache`
 * under that formula's key, so the formula rendered blank for the rest of the
 * process.
 *
 * Expected values below are DEAD LITERALS (counts and step orders written out by
 * hand), never derived from the production constants — a mutation to
 * `releasePlan` must fail here, which is exactly what the PRE/mutation arms in
 * `fix-reports/FIX-D7.md` §5 exercise.
 */
class WebViewReleaseLatchTest {

    @Test
    fun `deferred work is allowed until the view is released`() {
        val latch = WebViewReleaseLatch()
        assertFalse("a fresh latch must report not-released", latch.isReleased)
        assertTrue("deferred work must run on a live view", latch.canRunDeferred())
    }

    @Test
    fun `deferred work stands down after release`() {
        val latch = WebViewReleaseLatch()
        var ran = 0
        // The exact shape of a production deferred body: guard first, then work.
        val block = Runnable { if (latch.canRunDeferred()) ran += 1 }

        block.run()
        latch.release()
        block.run()

        assertEquals("only the pre-release run may execute (dead literal: 1)", 1, ran)
        assertFalse("a released latch is released forever", latch.canRunDeferred())
        assertTrue("release must latch isReleased", latch.isReleased)
    }

    @Test
    fun `release hands back every tracked runnable for removal by identity`() {
        val latch = WebViewReleaseLatch()
        val first = Runnable { }
        val second = Runnable { }
        latch.track(first)
        latch.track(second)

        val outstanding = latch.release()

        assertEquals("both tracked runnables must come back (dead literal: 2)", 2, outstanding.size)
        assertTrue("the first tracked runnable must be handed back", first in outstanding)
        assertTrue("the second tracked runnable must be handed back", second in outstanding)
    }

    @Test
    fun `release is idempotent so a double teardown removes nothing twice`() {
        val latch = WebViewReleaseLatch()
        latch.track(Runnable { })

        assertEquals("first release returns the outstanding task (dead literal: 1)", 1, latch.release().size)
        assertEquals("second release must return nothing (dead literal: 0)", 0, latch.release().size)
    }

    @Test
    fun `a runnable registered after release is not handed to the teardown`() {
        val latch = WebViewReleaseLatch()
        latch.release()

        latch.track(Runnable { })

        assertEquals(
            "a post-release registration must be dropped, not resurrected (dead literal: 0)",
            0,
            latch.release().size,
        )
    }

    @Test
    fun `release drains the registry and ignores later registrations`() {
        val latch = WebViewReleaseLatch()
        latch.track(Runnable { })
        latch.release()

        assertEquals("the registry must be empty after release (dead literal: 0)", 0, latch.pendingCount)
        latch.track(Runnable { })
        assertEquals(
            "a registration that lands after release must be dropped (dead literal: 0)",
            0,
            latch.pendingCount,
        )
    }

    @Test
    fun `release plan of an attached view detaches before it drops or destroys`() {
        val plan = releasePlan(attached = true)

        assertEquals(
            "the whole attached-view order is the contract (dead literal)",
            listOf(
                ReleaseStep.DETACH,
                ReleaseStep.DROP_DEFERRED,
                ReleaseStep.STOP_LOADING,
                ReleaseStep.DESTROY,
            ),
            plan,
        )
    }

    @Test
    fun `release plan of a detached view skips only the detach`() {
        val plan = releasePlan(attached = false)

        assertEquals(
            "a view that already left its parent needs no detach (dead literal)",
            listOf(
                ReleaseStep.DROP_DEFERRED,
                ReleaseStep.STOP_LOADING,
                ReleaseStep.DESTROY,
            ),
            plan,
        )
    }

    @Test
    fun `every plan drops the deferred work before the renderer is stopped`() {
        for (attached in listOf(true, false)) {
            val plan = releasePlan(attached)
            assertTrue(
                "drop must precede stopLoading/destroy (attached=$attached): $plan",
                plan.indexOf(ReleaseStep.DROP_DEFERRED) < plan.indexOf(ReleaseStep.STOP_LOADING),
            )
            assertEquals(
                "destroy must be the last step (attached=$attached): $plan",
                ReleaseStep.DESTROY,
                plan.last(),
            )
        }
    }
}

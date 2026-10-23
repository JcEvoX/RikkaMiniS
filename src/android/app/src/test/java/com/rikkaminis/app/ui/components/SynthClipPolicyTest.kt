package com.rikkaminis.app.ui.components

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.atomic.AtomicBoolean

/**
 * [dv26/G10] Drives the four lifecycle orders of `synthesizeTestClip` through
 * [SynthClipPolicy].
 *
 * The Android module cannot run the real coroutine + system TTS engine on the
 * JVM, so this test models only the *event order* (init callback / synth line /
 * cancellation / late write / progress callback) and lets production's own
 * policy functions decide every step. Expected values are literals on purpose:
 * deriving them from the policy would turn every mutation into a PASS.
 */
class SynthClipPolicyTest {

    /** Event order of one `synthesizeTestClip` call; policy decides each step. */
    private class Lifecycle {
        val cancelled = AtomicBoolean(false)
        var engineTouched = false
        var synthDispatched = false
        var fileOnDisk = false
        var resultDelivered = false

        /**
         * `TextToSpeech(context) { … }` body up to the progress listener.
         * Returns true when it ran (i.e. production would reach [synthLine]).
         */
        fun initCallback(): Boolean {
            if (!SynthClipPolicy.mayTouchEngine(cancelled.get())) return false
            engineTouched = true
            return true
        }

        /** The `synthesizeToFile` line, several statements after [initCallback]. */
        fun synthLine() {
            if (!SynthClipPolicy.maySynthesize(cancelled.get())) return
            synthDispatched = true
        }

        /** The engine finishes the write (can land at any later point). */
        fun writeLands() {
            if (synthDispatched) fileOnDisk = true
        }

        /** `invokeOnCancellation`: latch + shutdown + re-delete. */
        fun cancelPath() {
            cancelled.set(true)
            fileOnDisk = false
        }

        /** `finally { file.delete() }`. */
        fun exitPath() {
            fileOnDisk = false
        }

        /** `finish(ok)` from onDone/onError. */
        fun progressCallback() {
            if (SynthClipPolicy.mustDropOnCallback(cancelled.get())) fileOnDisk = false
            if (!cancelled.get()) resultDelivered = true
        }
    }

    @Test
    fun normalCompletionStillSynthesizesAndDelivers() {
        val l = Lifecycle()
        l.initCallback()
        l.synthLine()
        l.writeLands()
        // The clip must survive until the caller has read it: the success path
        // is `takeIf { it }?.let { file.readBytes() }` before the finally delete.
        assertTrue("the clip must be on disk for the caller to read", l.fileOnDisk)
        l.progressCallback()
        assertTrue("onDone must deliver the result", l.resultDelivered)
        assertTrue("a delivered clip must not be dropped by the callback", l.fileOnDisk)
        l.exitPath()
        assertFalse(l.fileOnDisk)
    }

    @Test
    fun lateInitCallbackNeverTouchesTheEngineOrDispatchesTheWrite() {
        val l = Lifecycle()
        l.cancelPath()          // 15 s timeout / sheet dismissed while initialising
        // the engine's init callback only lands now; an aborting callback never
        // reaches the write line
        if (l.initCallback()) l.synthLine()
        l.writeLands()
        assertFalse("a late init callback must not touch the engine", l.engineTouched)
        assertFalse("a late init callback must not dispatch synthesizeToFile", l.synthDispatched)
        assertFalse("no clip may exist after cancellation", l.fileOnDisk)
    }

    @Test
    fun cancelInsideTheInitWindowStillAbortsTheWrite() {
        val l = Lifecycle()
        l.initCallback()        // engine touched, language + listener set …
        l.cancelPath()          // … cancellation lands inside that window …
        l.synthLine()           // … and the write must not be dispatched
        l.writeLands()
        assertTrue(l.engineTouched)
        assertFalse("the window between init and synthesizeToFile must re-check", l.synthDispatched)
        assertFalse(l.fileOnDisk)
    }

    @Test
    fun writeThatLandsAfterCancellationIsDroppedByTheCallback() {
        val l = Lifecycle()
        l.initCallback()
        l.synthLine()           // dispatched …
        l.cancelPath()          // … cancelled before the engine finished
        l.writeLands()          // the engine completes the write anyway
        assertTrue("the late write does land on disk", l.fileOnDisk)
        l.progressCallback()    // onDone after cancellation
        assertFalse("a post-cancel write must be dropped by the callback", l.fileOnDisk)
        assertFalse("nothing is delivered after cancellation", l.resultDelivered)
    }
}

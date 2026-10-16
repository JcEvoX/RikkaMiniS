package com.rikkaminis.app.accessibility

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1017 G1] Pins the hand-off that stops a screenshot callback which
 * arrives *after* the caller's timeout from parking a full-screen bitmap where
 * nobody will ever read — or recycle — it.
 *
 * ## What the old shape cost
 *
 * `captureScreenshot` waited on a latch with a 5 s budget and then returned
 * whatever sat in its `resultRef`. The platform's `TakeScreenshotCallback`
 * cannot be unregistered, so a callback arriving at 5.2 s still ran its body:
 * `wrapHardwareBuffer` → `bmp.copy(ARGB_8888)` (a screen-sized software bitmap,
 * ~10–15 MB at 1080×2400) → `resultRef.set(...)` on a reference the caller had
 * already abandoned. Nobody read it, nobody recycled it — one leaked
 * screen-sized allocation per timed-out capture, for the life of the process.
 *
 * ## What is pinned here
 *
 * [LateValueSlot] is the arbitration: the producer peeks [LateValueSlot.isCallerWaiting]
 * before allocating and publishes atomically; the caller [LateValueSlot.exit]s,
 * recycling anything a late producer left behind. Every payload has exactly one
 * owner. `AccessibilityNodeInfo`-free and JVM-pure, so it is tested directly
 * instead of through a device.
 */
class ScreenshotLateCallbackTest {

    /** Stand-in for `ShotResult`: the recycle counter plays the bitmap's. */
    private class Payload(val name: String) {
        var recycles = 0
        fun recycle() { recycles++ }
    }

    private class Recorder {
        val discarded = mutableListOf<String>()
        fun hook(p: Payload) = discarded.add(p.name)
    }

    // ── the leak (pre-fix behaviour) ─────────────────────────────────────

    @Test
    fun `a value published after the caller exited is discarded, not parked`() {
        val rec = Recorder()
        val fallback = Payload("timeout")
        val slot = LateValueSlot(fallback, rec::hook)

        // The caller gave up: it keeps its own fallback and closes the slot.
        assertSame(fallback, slot.exit(fallback))
        assertFalse(slot.isCallerWaiting())

        // 5.2 s later the platform delivers the screenshot anyway.
        val late = Payload("late-screenshot")
        assertFalse("a closed slot must refuse the publish", slot.publish(late))

        assertEquals(listOf("late-screenshot"), rec.discarded)
        assertNull("nothing may stay reachable in a closed slot", slot.current())
    }

    @Test
    fun `a value already in the slot when the caller gives up is discarded on exit`() {
        val rec = Recorder()
        val fallback = Payload("timeout")
        val slot = LateValueSlot(fallback, rec::hook)

        // Landed before the caller closed the slot (publish wins that race)...
        val late = Payload("late-screenshot")
        assertTrue(slot.publish(late))
        // ...but the caller returns its TIMEOUT result, so the frame has no owner.
        assertSame(late, slot.exit(fallback))

        assertEquals(listOf("late-screenshot"), rec.discarded)
    }

    // ── the success path must not change ─────────────────────────────────

    @Test
    fun `a value published while the caller waits is retained and taken, never discarded`() {
        val rec = Recorder()
        val fallback = Payload("timeout")
        val slot = LateValueSlot(fallback, rec::hook)

        val shot = Payload("screenshot")
        assertTrue(slot.isCallerWaiting())
        assertTrue(slot.publish(shot))
        assertSame(shot, slot.current())

        // Success path: the caller reads it back and keeps it for its own use.
        assertSame(shot, slot.exit(shot))
        assertEquals("the fallback is not a payload", emptyList<String>(), rec.discarded)
        assertEquals("the caller owns the bitmap and recycles it itself", 0, shot.recycles)
    }

    @Test
    fun `the caller's own fallback is never handed to the discard hook`() {
        val rec = Recorder()
        val fallback = Payload("timeout")

        val timeoutPath = LateValueSlot(fallback, rec::hook)
        assertSame(fallback, timeoutPath.exit(fallback))

        val neverRead = LateValueSlot(fallback, rec::hook)
        assertSame(fallback, neverRead.exit(null))
        assertFalse(neverRead.isCallerWaiting())

        assertEquals(emptyList<String>(), rec.discarded)
        assertEquals(0, fallback.recycles)
    }

    @Test
    fun `a second publish discards the payload it displaces`() {
        val rec = Recorder()
        val fallback = Payload("timeout")
        val slot = LateValueSlot(fallback, rec::hook)

        val first = Payload("first")
        val second = Payload("second")
        assertTrue(slot.publish(first))
        assertTrue(slot.publish(second))

        assertEquals("the displaced payload has no owner left", listOf("first"), rec.discarded)
        assertSame(second, slot.current())
    }

    // ── the race itself ──────────────────────────────────────────────────

    /**
     * Both sides run without a lock, so the interleaving varies run to run. The
     * assertion is therefore the invariant that must hold under *any*
     * interleaving — the payload is owned exactly once — rather than which side
     * won (the deterministic tests above cover each winner).
     *
     * The caller mirrors `captureScreenshot`'s two phases: peek at the slot, then
     * keep what it read and hand the rest over. The payload is owned by the
     * caller only when that is the value it kept.
     */
    @Test
    fun `racing exit against publish never loses or double-owns a payload`() {
        val iterations = 500
        var callerOwns = 0
        var discardedCount = 0

        for (i in 0 until iterations) {
            val fallback = Payload("timeout")
            val payload = Payload("screenshot")
            var discarded = 0
            val slot = LateValueSlot(fallback) { discarded++ }

            val producer = Thread {
                Thread.yield()
                slot.publish(payload)
            }
            producer.start()
            Thread.yield()
            val kept = slot.current() ?: fallback
            slot.exit(kept)
            producer.join()

            // Exactly one owner: the caller kept it, or the hook took it. Never
            // both, never neither (the pre-fix shape parked it for nobody).
            val ownedByCaller = if (kept === payload) 1 else 0
            assertEquals(
                "iteration $i: payload neither kept nor discarded",
                1,
                ownedByCaller + discarded,
            )
            callerOwns += ownedByCaller
            discardedCount += discarded
        }

        assertEquals(iterations, callerOwns + discardedCount)
    }
}

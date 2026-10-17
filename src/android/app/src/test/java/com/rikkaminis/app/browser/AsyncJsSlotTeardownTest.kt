package com.rikkaminis.app.browser

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1019 ISSUE-08] Teardown semantics of `AsyncJsSlot.takeCurrent()`
 * — the entry `BrowserUseManager.destroy()` and `onRendererGone` use to wake
 * an in-flight async-JS waiter instead of leaving it to burn its 30s/60s
 * timeout on bridge callbacks that can never arrive again.
 *
 * Expectations are literals, never derived from the implementation.
 */
class AsyncJsSlotTeardownTest {

    @Test
    fun `takeCurrent returns the in-flight value and clears the slot`() {
        val slot = AsyncJsSlot<String>()
        slot.begin("t1", "v1")
        assertEquals("v1", slot.takeCurrent())
        assertNull("the slot must be empty after the take", slot.ifOwner("t1"))
        assertNull("a second take must find nothing", slot.takeCurrent())
    }

    @Test
    fun `takeCurrent on a free slot is a null no-op`() {
        val slot = AsyncJsSlot<String>()
        assertNull(slot.takeCurrent())
    }

    @Test
    fun `takeCurrent is mutually exclusive with finish - finished first`() {
        val slot = AsyncJsSlot<String>()
        slot.begin("t1", "v1")
        assertTrue(slot.finish("t1"))
        assertNull("teardown after a completed request must find nothing", slot.takeCurrent())
    }

    @Test
    fun `takeCurrent is mutually exclusive with finish - taken first`() {
        val slot = AsyncJsSlot<String>()
        slot.begin("t1", "v1")
        assertEquals("v1", slot.takeCurrent())
        assertFalse("finish must refuse the emptied slot", slot.finish("t1"))
    }

    @Test
    fun `a request begun after the take keeps its own slot`() {
        val slot = AsyncJsSlot<String>()
        slot.begin("old", "old-value")
        assertEquals("old-value", slot.takeCurrent())
        assertNull(slot.begin("new", "new-value"))
        assertEquals("the newer request still owns the slot", "new-value", slot.ifOwner("new"))
    }

    /**
     * The exact teardown shape destroy()/onRendererGone rely on: the taken
     * deferred is completed with the bridge's reject-shaped error, so
     * executeJS's consumer surfaces it as a real error instead of waiting out
     * the timeout — and it is `{"error":…}` (JSON), never a bare text payload,
     * which executeJS would report as a SUCCESSFUL result via its "not JSON"
     * fallback branch.
     */
    @Test
    fun `teardown completes a real waiter immediately with a structured error`() {
        val slot = AsyncJsSlot<CompletableDeferred<String>>()
        val deferred = CompletableDeferred<String>()
        slot.begin("t1", deferred)

        val taken = slot.takeCurrent()
        assertSame("the teardown must hand back the very deferred in flight", deferred, taken)
        taken!!.complete("{\"error\":\"WebView was destroyed before the async JS request completed\"}")

        assertEquals(
            "{\"error\":\"WebView was destroyed before the async JS request completed\"}",
            runBlocking { deferred.await() },
        )
        assertNull("slot must be free again for the next request", slot.takeCurrent())
    }

    /**
     * The waiter that already resolved must never be failed by a teardown:
     * resolve (bridge callback) and takeCurrent race, and whichever runs first
     * wins; the loser must observe an already-completed deferred it can only
     * no-op on. `CompletableDeferred.complete` is idempotent, so a late
     * teardown on a completed waiter cannot rewrite its result.
     */
    @Test
    fun `a late teardown cannot rewrite an already-resolved waiter`() {
        val slot = AsyncJsSlot<CompletableDeferred<String>>()
        val deferred = CompletableDeferred<String>()
        slot.begin("t1", deferred)
        deferred.complete("{\"ok\":true}")

        val taken = slot.takeCurrent()
        assertSame(deferred, taken)
        // The teardown's error lands on an already-completed deferred — a no-op.
        val landed = taken!!.complete("{\"error\":\"WebView was destroyed before the async JS request completed\"}")
        assertFalse("second completion must be refused", landed)
        assertEquals("{\"ok\":true}", runBlocking { deferred.await() })
    }
}

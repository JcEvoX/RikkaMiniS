package com.rikkaminis.app.browser

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicReference

/**
 * [deep-audit-1016c ISSUE-03B] State-machine tests for [AsyncJsSlot] — the
 * single-slot registry that replaced the two un-@Volatile fields
 * (`asyncJsDeferred` / `asyncJsActiveToken`) plus the unconditional,
 * token-less `finishAsyncJsRequest()`.
 *
 * Expectations are literals (probe-observer values), never derived from the
 * implementation.
 */
class AsyncJsSlotTest {

    // ── begin ───────────────────────────────────────────────────────────

    @Test
    fun `begin on a free slot reports no eviction and installs the owner`() {
        val slot = AsyncJsSlot<String>()
        assertNull("a free slot must not evict anything", slot.begin("t1", "v1"))
        assertEquals("v1", slot.ifOwner("t1"))
    }

    @Test
    fun `begin on an occupied slot returns the evicted value for the caller to fail`() {
        val slot = AsyncJsSlot<String>()
        slot.begin("t1", "v1")
        assertEquals("the evicted value must be handed back", "v1", slot.begin("t2", "v2"))
        assertEquals("v2", slot.ifOwner("t2"))
        assertNull("the evicted token must no longer own the slot", slot.ifOwner("t1"))
    }

    // ── finish ──────────────────────────────────────────────────────────

    @Test
    fun `finish clears the slot when the token owns it`() {
        val slot = AsyncJsSlot<String>()
        slot.begin("t1", "v1")
        assertTrue(slot.finish("t1"))
        assertNull(slot.ifOwner("t1"))
    }

    @Test
    fun `finish on an empty slot is a no-op returning false`() {
        val slot = AsyncJsSlot<String>()
        assertFalse(slot.finish("t1"))
    }

    @Test
    fun `finish with a foreign token leaves the real owner untouched`() {
        val slot = AsyncJsSlot<String>()
        slot.begin("t1", "v1")
        assertFalse("a token that does not own the slot must not clear it", slot.finish("other"))
        assertEquals("v1", slot.ifOwner("t1"))
    }

    /**
     * The exact defect the fix targets: a timed-out request finishing late
     * (or a cancelled one) must not clear the slot a NEWER request owns —
     * under the old unconditional clear the newer request's resolve landed on
     * a null slot and its waiter burned the full 30s/60s timeout.
     */
    @Test
    fun `a superseded request finishing late must not clear the newer owner`() {
        val slot = AsyncJsSlot<String>()
        slot.begin("timed-out", "old")
        // A newer request begins while the old one is still unwinding.
        val evicted = slot.begin("in-flight", "new")
        assertEquals("old", evicted)

        assertFalse(
            "the old request's cleanup must not clear the new request's slot",
            slot.finish("timed-out"),
        )
        assertEquals("the newer request still owns the slot", "new", slot.ifOwner("in-flight"))
        assertTrue(slot.finish("in-flight"))
        assertNull(slot.ifOwner("in-flight"))
    }

    @Test
    fun `ifOwner rejects a stale token while the slot is occupied`() {
        val slot = AsyncJsSlot<String>()
        slot.begin("t1", "v1")
        assertNull(slot.ifOwner("t0"))
        assertNull(slot.ifOwner(""))
    }

    // ── concurrency: only the owner clears, pairings never tear ─────────

    @Test
    fun `concurrent begin and finish rounds never clear a foreign slot and never tear a pair`() {
        val slots = AsyncJsSlot<String>()
        val threads = 4
        val rounds = 5_000
        val start = CountDownLatch(1)
        val failure = AtomicReference<String?>(null)
        val workers = (0 until threads).map { t ->
            Thread {
                start.await()
                for (r in 0 until rounds) {
                    val token = "t$t-$r"
                    val value = "v$t-$r"
                    slots.begin(token, value)
                    Thread.yield()
                    // This thread's own token must only ever map to its own
                    // value (a torn token/value pairing would surface here).
                    val seen = slots.ifOwner(token)
                    if (seen != null && seen != value) {
                        failure.compareAndSet(null, "torn pair: token=$token value=$seen")
                    }
                    Thread.yield()
                    val cleared = slots.finish(token)
                    if (!cleared && slots.ifOwner(token) != null) {
                        // finish refused (a newer request owns the slot) yet the
                        // token still reports ownership — contradictory state.
                        failure.compareAndSet(null, "foreign owner survived finish: $token")
                    }
                }
            }
        }
        workers.forEach { it.start() }
        start.countDown()
        workers.forEach { it.join(30_000) }
        assertTrue(
            "all workers must finish within the join window",
            workers.none { it.isAlive },
        )
        assertNull("no invariant violation: ${failure.get()}", failure.get())
    }
}

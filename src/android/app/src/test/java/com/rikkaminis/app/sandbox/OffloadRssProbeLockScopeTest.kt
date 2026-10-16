package com.rikkaminis.app.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * [fix/da1016-rssprobe-lock-scope] deep-audit-1016c ISSUE-11 (P2).
 *
 * Pins the lock-scope contract of [OffloadRssProbe.record] / [OffloadRssProbe.summary]:
 * the increment, the LEAK-SUSPECT winner flag, the governance decision and the
 * cumulative reset must form ONE critical section. Before the fix only the four
 * field increments sat inside `synchronized(st)`; the threshold checks, the
 * `st.leaked = true` write and `st.totalDeltaKb = 0L` reset all ran outside it,
 * so two offload workers (Semaphore(2), NativeOffload.kt:116/160) could
 *  (1) lose an increment between the toGovern check and the reset,
 *  (2) both pass `!st.leaked` / `toGovern` → duplicate WARN + concurrent
 *      double execution of the high-cost [OffloadRssProbe.governanceHook].
 *
 * CI observability note: unit tests run with `isReturnDefaultValues = true`, so
 * `android.util.Log` returns defaults and cannot be counted here. The WARN
 * *line* count is pinned by the sandbox probe rig instead (see fix-reports);
 * the CI-visible contract is the winner flag itself plus the
 * conservation invariants below (count / cumulative / hook calls).
 *
 * Expected values are written as literals, NOT derived from the constants under
 * test (self-reference trap: LEAK_SUSPECT_KB = 1 GiB = 1_048_576 kB,
 * CUMULATIVE_GOVERN_KB = 256 MiB = 262_144 kB, PEAK_GOVERN_KB = 64 MiB = 65_536 kB).
 */
class OffloadRssProbeLockScopeTest {

    @Before
    fun setUp() {
        OffloadRssProbe.reset()
        OffloadRssProbe.governanceHook = {}
    }

    // ---- helpers: read the summary text (the only public observation surface) ----

    private fun lineFor(summary: String, handler: String): String =
        summary.lines().firstOrNull { it.trimStart().startsWith("$handler:") }
            ?: error("handler '$handler' missing from summary:\n$summary")

    private fun fieldOf(summary: String, handler: String, key: String): Long {
        val line = lineFor(summary, handler)
        val token = line.trimStart().substringAfter("$handler: ").split(" ")
            .firstOrNull { it.startsWith("$key=") }
            ?: error("field '$key' missing in line: $line")
        return token.substringAfter("=").removeSuffix("MB").toLong()
    }

    // ---- decision pure function: single-threaded winner semantics ----

    @Test
    fun `decide first caller crossing 1GiB wins the leak flag exactly once`() {
        val st = OffloadRssProbe.Stats()
        st.totalDeltaKb = 1_000_000L
        st.count = 5
        // totalAfter = 1_050_000 ≥ 1_048_576 → winner; also ≥ 262_144 → govern.
        val d = OffloadRssProbe.applyAndDecideLocked(st, 50_000L)
        assertTrue(d.warnLeak)
        assertTrue(st.leaked)
        assertEquals(1_050_000L, d.totalDeltaKb) // pre-reset snapshot
        assertEquals(6, d.count)
        assertEquals(175_000L, d.avgDeltaKb)      // 1_050_000 / 6, same source
        assertTrue(d.govern)
        assertTrue(d.governCumulative)
        assertEquals(0L, st.totalDeltaKb)         // reset happened inside the section
        // A later caller must NOT win again, even when it re-crosses the threshold.
        val d2 = OffloadRssProbe.applyAndDecideLocked(st, 1_048_576L)
        assertFalse(d2.warnLeak)
    }

    @Test
    fun `two callers of one Stats cannot both win the leak flag`() {
        val st = OffloadRssProbe.Stats()
        st.totalDeltaKb = 1_000_000L
        val d1 = OffloadRssProbe.applyAndDecideLocked(st, 50_000L)
        val d2 = OffloadRssProbe.applyAndDecideLocked(st, 50_000L)
        assertTrue(d1.warnLeak)
        assertFalse(d2.warnLeak)
        assertEquals(2, d2.count)
    }

    @Test
    fun `decide single peak govern uses non-cumulative reason and still resets`() {
        val st = OffloadRssProbe.Stats()
        // delta 70_000 ≥ 65_536 (peak) while totalAfter 70_000 < 262_144 (cumulative).
        val d = OffloadRssProbe.applyAndDecideLocked(st, 70_000L)
        assertTrue(d.govern)
        assertFalse(d.governCumulative)
        assertFalse(d.warnLeak)
        assertEquals(0L, st.totalDeltaKb) // matches pre-fix behaviour: either reason resets
    }

    @Test
    fun `decide below both thresholds mutates only the four counters`() {
        val st = OffloadRssProbe.Stats()
        val d = OffloadRssProbe.applyAndDecideLocked(st, 10_000L)
        assertFalse(d.govern)
        assertFalse(d.warnLeak)
        assertEquals(10_000L, st.totalDeltaKb)
        assertEquals(1, d.count)
        assertEquals(10_000L, d.avgDeltaKb)
        assertEquals(10_000L, d.peakDeltaKb)
    }

    @Test
    fun `decide keeps peak across a governance reset`() {
        val st = OffloadRssProbe.Stats()
        OffloadRssProbe.applyAndDecideLocked(st, 50_000L)
        val d = OffloadRssProbe.applyAndDecideLocked(st, 5_000L)
        assertEquals(50_000L, d.peakDeltaKb)
    }

    @Test
    fun `decide records negative delta truthfully`() {
        val st = OffloadRssProbe.Stats()
        st.totalDeltaKb = 20_000L
        val d = OffloadRssProbe.applyAndDecideLocked(st, -20_000L)
        assertEquals(0L, st.totalDeltaKb)
        assertEquals(0L, d.avgDeltaKb) // totalAfter 0 / count 1
        assertFalse(d.govern)
    }

    // ---- record path: single-threaded governance/reset step ----

    @Test
    fun `record fires governance exactly once at the crossing and resets`() {
        var fired = 0
        OffloadRssProbe.governanceHook = { fired++ }
        // 27 × 10_000 kB: the 27th call reaches 270_000 ≥ 262_144 → one govern + reset.
        repeat(27) { OffloadRssProbe.record("h1", 1_000L, 1_000L + 10_000L) }
        assertEquals(1, fired)
        val s = OffloadRssProbe.summary()
        assertEquals(0L, fieldOf(s, "h1", "cum"))
        assertEquals(27L, fieldOf(s, "h1", "count"))
    }

    // ---- concurrent arms ----

    @Test
    fun `concurrent records conserve count and governance in lockstep`() {
        val threads = 8
        val perThread = 64
        val deltaKb = 32_768L // 32 MiB: below the 64 MiB single-peak rule, so only
        // the cumulative rule fires — every 8th call lands exactly on 262_144.
        val hookCalls = AtomicInteger(0)
        OffloadRssProbe.governanceHook = { hookCalls.incrementAndGet() }

        val errors = ConcurrentLinkedQueue<Throwable>()
        val start = CountDownLatch(1)
        val done = CountDownLatch(threads)
        repeat(threads) {
            Thread {
                try {
                    start.await()
                    repeat(perThread) {
                        OffloadRssProbe.record("hot", 0L, deltaKb)
                        // Widening interleave window (1014 FIX-RV-F lesson): a tight
                        // loop starves the other side and hides lost updates.
                        Thread.yield()
                    }
                } catch (t: Throwable) {
                    errors.add(t)
                } finally {
                    done.countDown()
                }
            }.start()
        }
        start.countDown()
        done.await()
        assertTrue("worker errors: $errors", errors.isEmpty())

        val s = OffloadRssProbe.summary()
        // 512 calls → count is exact; every 8th call governs → 64 hook calls;
        // each reset consumed exactly 262_144 → final cum = 0.
        assertEquals(512L, fieldOf(s, "hot", "count"))
        assertEquals(64, hookCalls.get())
        assertEquals(0L, fieldOf(s, "hot", "cum"))
        // Conservation: Σdelta == finalCum + (govern count × per-reset amount).
        // "governance fired == cumulative was reset" is asserted through this sum.
        assertEquals(16_777_216L, fieldOf(s, "hot", "cum") + hookCalls.get() * 262_144L)
    }

    @Test
    fun `concurrent records past the leak threshold flag the handler once`() {
        val hookFires = AtomicInteger(0)
        OffloadRssProbe.governanceHook = { hookFires.incrementAndGet() }
        // First call crosses LEAK_SUSPECT_KB (1_048_576) → single winner.
        OffloadRssProbe.record("hot", 0L, 1_048_576L)
        assertEquals(1, hookFires.get()) // also governs (≥262_144) and resets
        // 8 × 64 × 131_072 afterwards: each delta (128 MiB) also crosses the
        // 64 MiB single-peak rule → every call governs, so the hook fires once
        // per call; but leaked stays set, so there is never a second WARN winner.
        val threads = 8
        val perThread = 64
        val start = CountDownLatch(1)
        val done = CountDownLatch(threads)
        repeat(threads) {
            Thread {
                try {
                    start.await()
                    repeat(perThread) {
                        OffloadRssProbe.record("hot", 0L, 131_072L)
                        Thread.yield()
                    }
                } finally {
                    done.countDown()
                }
            }.start()
        }
        start.countDown()
        done.await()
        val s = OffloadRssProbe.summary()
        assertEquals(513L, fieldOf(s, "hot", "count"))
        assertEquals(513, hookFires.get()) // 1 pre-warm + 512 governs, in lockstep
        // exactly one LEAK-SUSPECT marker — the flag never double-fires.
        assertEquals(1, lineFor(s, "hot").split("[LEAK-SUSPECT]").size - 1)
    }

    @Test
    fun `summary stays self consistent while records run concurrently`() {
        val stop = AtomicBoolean(false)
        val errors = ConcurrentLinkedQueue<Throwable>()
        val recorders = (1..6).map {
            Thread {
                try {
                    while (!stop.get()) {
                        OffloadRssProbe.record("mix", 0L, 131_072L)
                        Thread.yield()
                    }
                } catch (t: Throwable) {
                    errors.add(t)
                }
            }.apply { start() }
        }
        var seen = 0
        var lastCount = 0L
        repeat(300) {
            val s = OffloadRssProbe.summary()
            val cum = fieldOf(s, "mix", "cum")
            val count = fieldOf(s, "mix", "count")
            val avg = fieldOf(s, "mix", "avg")
            // Snapshot must be internally consistent: avg comes from the same
            // critical section as cum and count (pre-fix they were three racy reads).
            assertEquals(cum / count, avg)
            // count only grows (recorders never reset it).
            assertTrue("count went backwards: $lastCount → $count", count >= lastCount)
            lastCount = count
            seen += 1
        }
        stop.set(true)
        recorders.forEach { it.join() }
        assertTrue("worker errors: $errors", errors.isEmpty())
        assertEquals(300, seen)
        assertTrue(lastCount > 0L)
    }
}

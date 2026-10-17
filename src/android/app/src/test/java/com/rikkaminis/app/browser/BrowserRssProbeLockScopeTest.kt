package com.rikkaminis.app.browser

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CountDownLatch

/**
 * [fix/da1019-rssprobe-lock-scope] deep-audit-1019 ISSUE-09 (P2) — lock-scope rig
 * for [BrowserRssProbe], the sibling of
 * [com.rikkaminis.app.sandbox.OffloadRssProbeLockScopeTest] (da1016c ISSUE-11).
 *
 * Before the fix `record()` had no critical section at all: the compound
 * `+=` / peak sequences lost updates under concurrent browser actions
 * (BrowserTabPool runs actions on the main-process WebView pool), the
 * `!st.leaked` check-then-act let two callers print the single-shot
 * LEAK-SUSPECT WARN twice, and `summary()` read three independently racy
 * fields per line.
 *
 * CI observability note: unit tests run with `isReturnDefaultValues = true`, so
 * `android.util.Log` returns defaults and cannot be counted here. The WARN
 * *line* count is pinned by the sandbox probe rig (see fix-reports); the
 * CI-visible contract is the winner flag itself plus the conservation
 * invariants below (count / cumulative / single `[LEAK-SUSPECT]` marker).
 *
 * Expected values are written as literals, NOT derived from the constants under
 * test (self-reference trap: LEAK_SUSPECT_KB = 1 GiB = 1_048_576 kB).
 */
class BrowserRssProbeLockScopeTest {

    @Before
    fun setUp() {
        BrowserRssProbe.reset()
    }

    // ---- helpers: read the summary text (the only public observation surface) ----

    private fun lineFor(summary: String, action: String): String =
        summary.lines().firstOrNull { it.trimStart().startsWith("$action:") }
            ?: error("action '$action' missing from summary:\n$summary")

    private fun fieldOf(summary: String, action: String, key: String): Long {
        val line = lineFor(summary, action)
        val token = line.trimStart().substringAfter("$action: ").split(" ")
            .firstOrNull { it.startsWith("$key=") }
            ?: error("field '$key' missing in line: $line")
        return token.substringAfter("=").removeSuffix("MB").toLong()
    }

    // ---- decision pure function: single-threaded winner semantics ----

    @Test
    fun `decision first caller crossing 1GiB wins the leak flag exactly once`() {
        val st = BrowserRssProbe.Stats()
        st.totalDeltaKb = 1_000_000L
        // totalAfter = 1_050_000 ≥ 1_048_576 → winner.
        val d = BrowserRssProbe.applyAndDecideLocked(st, 50_000L)
        assertTrue(d.warnLeak)
        assertTrue(st.leaked)
        assertEquals(1_050_000L, d.totalDeltaKb)
        assertEquals(1, d.count)
        assertEquals(1_050_000L, d.avgDeltaKb) // count == 1 → avg == total, same source
        assertEquals(50_000L, d.peakDeltaKb)
        // A later caller must NOT win again, even when it re-crosses the threshold.
        val d2 = BrowserRssProbe.applyAndDecideLocked(st, 1_048_576L)
        assertFalse(d2.warnLeak)
        assertEquals(2, d2.count)
        assertEquals(2, st.count)
    }

    @Test
    fun `decision one kB below the threshold never flags`() {
        val st = BrowserRssProbe.Stats()
        val d = BrowserRssProbe.applyAndDecideLocked(st, 1_048_575L)
        assertFalse(d.warnLeak)
        assertFalse(st.leaked)
        assertEquals(1_048_575L, d.totalDeltaKb)
        assertEquals(1, d.count)
    }

    @Test
    fun `decision keeps peak across a negative delta and records it truthfully`() {
        val st = BrowserRssProbe.Stats()
        BrowserRssProbe.applyAndDecideLocked(st, 40_000L)
        val d = BrowserRssProbe.applyAndDecideLocked(st, -20_000L)
        assertEquals(20_000L, d.totalDeltaKb)
        assertEquals(10_000L, d.avgDeltaKb)  // 20_000 / 2
        assertEquals(40_000L, d.peakDeltaKb) // peak survives a negative call
        assertEquals(-20_000L, st.lastDeltaKb)
        assertFalse(d.warnLeak)
    }

    // ---- concurrent arms ----

    @Test
    fun `concurrent records conserve count and cumulative delta`() {
        val threads = 8
        val perThread = 64
        val errors = ConcurrentLinkedQueue<Throwable>()
        val start = CountDownLatch(1)
        val done = CountDownLatch(threads)
        repeat(threads) {
            Thread {
                try {
                    start.await()
                    repeat(perThread) {
                        BrowserRssProbe.record("hot", 0L, 1_024L) // exactly +1MB/call
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

        val s = BrowserRssProbe.summary()
        // 8 × 64 = 512 calls × 1_024 kB → cum 512MB, avg 1MB, peak 1MB (literals).
        // 512MB < 1GiB → this action must never be flagged.
        assertEquals(512L, fieldOf(s, "hot", "count"))
        assertEquals(512L, fieldOf(s, "hot", "cum"))
        assertEquals(1L, fieldOf(s, "hot", "avg"))
        assertEquals(1L, fieldOf(s, "hot", "peak"))
        assertEquals(1L, fieldOf(s, "hot", "last"))
        assertFalse(lineFor(s, "hot").contains("[LEAK-SUSPECT]"))
    }

    @Test
    fun `concurrent records past the leak threshold flag the action exactly once`() {
        val threads = 8
        val perThread = 128
        val errors = ConcurrentLinkedQueue<Throwable>()
        val start = CountDownLatch(1)
        val done = CountDownLatch(threads)
        repeat(threads) {
            Thread {
                try {
                    start.await()
                    repeat(perThread) {
                        // 1024 calls × 1_024 kB = 1_048_576 kB: the LAST call lands
                        // exactly on LEAK_SUSPECT_KB, so pre-fix racers could each
                        // pass `!st.leaked` and print the WARN more than once.
                        BrowserRssProbe.record("hot", 0L, 1_024L)
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

        val s = BrowserRssProbe.summary()
        assertEquals(1024L, fieldOf(s, "hot", "count"))
        assertEquals(1024L, fieldOf(s, "hot", "cum"))
        // exactly one LEAK-SUSPECT marker — the winner flag never double-fires.
        assertEquals(1, lineFor(s, "hot").split("[LEAK-SUSPECT]").size - 1)
    }

    @Test
    fun `summary stays self consistent while records run concurrently`() {
        val stop = java.util.concurrent.atomic.AtomicBoolean(false)
        val errors = ConcurrentLinkedQueue<Throwable>()
        val recorders = (1..6).map {
            Thread {
                try {
                    while (!stop.get()) {
                        BrowserRssProbe.record("mix", 0L, 131_072L) // +128MB/call
                        Thread.yield()
                    }
                } catch (t: Throwable) {
                    errors.add(t)
                }
            }.apply { start() }
        }
        var seen = 0
        var lastCount = 0L
        repeat(200) {
            val s = BrowserRssProbe.summary()
            val cum = fieldOf(s, "mix", "cum")
            val count = fieldOf(s, "mix", "count")
            val avg = fieldOf(s, "mix", "avg")
            // Snapshot must be internally consistent: avg comes from the same
            // critical section as cum and count (pre-fix they were three racy reads).
            assertEquals(cum / count, avg)
            // count only grows (this probe never resets the cumulative value).
            assertTrue("count went backwards: $lastCount → $count", count >= lastCount)
            lastCount = count
            seen += 1
        }
        stop.set(true)
        recorders.forEach { it.join() }
        assertTrue("worker errors: $errors", errors.isEmpty())
        assertEquals(200, seen)
        assertTrue(lastCount > 0L)
    }
}

package com.rikkaminis.app.provider

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CountDownLatch

/**
 * [fix/da1019-rssprobe-lock-scope] deep-audit-1019 ISSUE-09 (P2) — lock-scope rig
 * for [ProviderRssProbe], the sibling of
 * [com.rikkaminis.app.sandbox.OffloadRssProbeLockScopeTest] (da1016c ISSUE-11).
 *
 * Before the fix `synchronized(st)` guarded only the seven field updates; the
 * `Log.i` observation line, the `decideLeakSuspect(st, …)` read and the
 * `if (!st.leaked) st.leaked = true` winner write all sat OUTSIDE the section, so
 * parallel sub-agents entering `LLMProvider.sendMessage` / `streamMessage` could
 *  (1) both pass `!st.leaked` → the single-shot LEAK-SUSPECT WARN printed twice,
 *  (2) decide on a torn count / lowestPostCallRssKb / totalDeltaKb (another
 *      caller's increment landing between the update and the decision),
 *  (3) log three independently racy fields per observation line.
 *
 * CI observability note: unit tests run with `isReturnDefaultValues = true`, so
 * `android.util.Log` returns defaults and cannot be counted here. The WARN *line*
 * count is pinned by the sandbox probe rig (see fix-reports); the CI-visible
 * contract is the winner condition + the conservation invariants below.
 *
 * Expected values are literals, NOT derived from the constants under test
 * (self-reference trap: PEAK_SUSPECT_KB = 512 MiB = 524_288 kB,
 * POST_IDLE_DRIFT_KB = 256 MiB = 262_144 kB, POST_IDLE_MIN_CALLS = 3,
 * LEAK_SUSPECT_CUM_KB = 1 GiB = 1_048_576 kB).
 */
class ProviderRssProbeLockScopeTest {

    @Before
    fun setUp() {
        ProviderRssProbe.reset()
    }

    // ---- helpers ----

    private fun lineFor(summary: String, kind: String): String =
        summary.lines().firstOrNull { it.trimStart().startsWith("$kind:") }
            ?: error("kind '$kind' missing from summary:\n$summary")

    private fun fieldOf(summary: String, kind: String, key: String): Long {
        val line = lineFor(summary, kind)
        val token = line.trimStart().substringAfter("$kind: ").split(" ")
            .firstOrNull { it.startsWith("$key=") }
            ?: error("field '$key' missing in line: $line")
        return token.substringAfter("=").removeSuffix("MB").toLong()
    }

    private fun rec(kind: String, before: Long, after: Long, peak: Long = -1L) =
        ProviderRssProbe.ProbeRecord(kind = kind, beforeRss = before, afterRss = after, peakRss = peak)

    // ---- decision pure function: winner semantics per condition ----

    @Test
    fun `decision peak explosion wins the leak flag exactly once`() {
        val st = ProviderRssProbe.Stats()
        // peakDelta 600_000 kB ≥ 524_288 kB (512 MiB) → condition A.
        val d = ProviderRssProbe.applyAndDecideLocked(
            st, 0L, 600_000L, rec("sendMessage:mock", 100_000L, 100_000L),
        )
        assertEquals("PEAK-DELTA-585MB>=512MB", d.warnCondition) // 600_000/1024 = 585
        assertTrue(st.leaked)
        assertEquals(1, d.count)
        assertEquals(600_000L, d.peakDeltaMaxKb)
        // A later caller with the same peak must NOT win again.
        val d2 = ProviderRssProbe.applyAndDecideLocked(
            st, 0L, 600_000L, rec("sendMessage:mock", 100_000L, 100_000L),
        )
        assertEquals(null, d2.warnCondition)
        assertEquals(2, d2.count)
    }

    @Test
    fun `decision post-idle drift flags only from the third call`() {
        val st = ProviderRssProbe.Stats()
        // baseline 200_000 with drift 0 on calls 1–2 (count < 3 and no drift).
        val d1 = ProviderRssProbe.applyAndDecideLocked(st, 0L, 0L, rec("streamMessage:mock", 100_000L, 200_000L))
        val d2 = ProviderRssProbe.applyAndDecideLocked(st, 0L, 0L, rec("streamMessage:mock", 100_000L, 200_000L))
        assertEquals(null, d1.warnCondition)
        assertEquals(null, d2.warnCondition)
        // Call 3: count == 3, drift = 500_000 − 200_000 = 300_000 kB ≥ 262_144 kB → B.
        val d3 = ProviderRssProbe.applyAndDecideLocked(st, 0L, 0L, rec("streamMessage:mock", 100_000L, 500_000L))
        assertEquals("POST-IDLE-DRIFT-292MB>=256MB", d3.warnCondition) // 300_000/1024 = 292
        assertEquals(3, d3.count)
        assertEquals(500_000L, d3.postCallRssKb)
        assertEquals(200_000L, st.lowestPostCallRssKb) // baseline survives upward drift
    }

    @Test
    fun `decision cumulative fallback still flags when A and B stay silent`() {
        val st = ProviderRssProbe.Stats()
        st.totalDeltaKb = 1_000_000L
        // totalAfter 1_050_000 ≥ 1_048_576 → C (legacy cumulative rule).
        val d = ProviderRssProbe.applyAndDecideLocked(st, 50_000L, 0L, rec("sendMessage:mock", 0L, 0L))
        assertEquals("CUM-1025MB>=1024MB", d.warnCondition)
        // Behaviour pinned: this probe has NO governance semantics — the
        // cumulative value is deliberately not reset (unlike OffloadRssProbe).
        assertEquals(1_050_000L, d.totalDeltaKb)
        assertEquals(1_050_000L, st.totalDeltaKb)
    }

    @Test
    fun `decision healthy recovery never flags`() {
        val st = ProviderRssProbe.Stats()
        val ds = listOf(105_000L, 103_000L, 101_000L, 100_000L).map {
            ProviderRssProbe.applyAndDecideLocked(st, 1_000L, 0L, rec("sendMessage:mock", 100_000L, it))
        }
        ds.forEach { assertEquals(null, it.warnCondition) }
        assertEquals(4, ds.last().count)
        assertEquals(100_000L, st.lowestPostCallRssKb)
        assertEquals(4_000L, st.totalDeltaKb)
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
                        // +1_024 kB per call, constant afterRss → no post-idle drift,
                        // cum 512MB < 1GiB → this kind must never be flagged.
                        ProviderRssProbe.record(rec("sendMessage:mock", 100_000L, 101_024L))
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

        val s = ProviderRssProbe.summary()
        // 8 × 64 = 512 calls × 1_024 kB → cum 512MB, avg 1MB (literals).
        // afterRss 101_024 kB → postRss 98MB; peakRss unknown → peakΔmax stays 0.
        assertEquals(512L, fieldOf(s, "sendMessage:mock", "count"))
        assertEquals(512L, fieldOf(s, "sendMessage:mock", "cum"))
        assertEquals(1L, fieldOf(s, "sendMessage:mock", "avg"))
        assertEquals(98L, fieldOf(s, "sendMessage:mock", "postRss"))
        assertEquals(0L, fieldOf(s, "sendMessage:mock", "peakΔmax"))
        assertFalse(lineFor(s, "sendMessage:mock").contains("[LEAK-SUSPECT]"))
    }

    @Test
    fun `concurrent records past the cumulative threshold flag the kind exactly once`() {
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
                        // exactly on LEAK_SUSPECT_CUM_KB, so pre-fix racers could each
                        // pass `!st.leaked` and print the WARN more than once.
                        ProviderRssProbe.record(rec("sendMessage:mock", 100_000L, 101_024L))
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

        val s = ProviderRssProbe.summary()
        assertEquals(1024L, fieldOf(s, "sendMessage:mock", "count"))
        assertEquals(1024L, fieldOf(s, "sendMessage:mock", "cum"))
        assertEquals(1, lineFor(s, "sendMessage:mock").split("[LEAK-SUSPECT]").size - 1)
    }

    @Test
    fun `summary stays self consistent while records run concurrently`() {
        val stop = java.util.concurrent.atomic.AtomicBoolean(false)
        val errors = ConcurrentLinkedQueue<Throwable>()
        val recorders = (1..6).map {
            Thread {
                try {
                    while (!stop.get()) {
                        ProviderRssProbe.record(rec("streamMessage:mock", 100_000L, 231_072L)) // +128MB
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
            val s = ProviderRssProbe.summary()
            val cum = fieldOf(s, "streamMessage:mock", "cum")
            val count = fieldOf(s, "streamMessage:mock", "count")
            val avg = fieldOf(s, "streamMessage:mock", "avg")
            // Snapshot must be internally consistent: avg/cum/count come from ONE
            // critical section (pre-fix they were three racy reads).
            assertEquals(cum / count, avg)
            // count only grows (no governance reset in this probe).
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

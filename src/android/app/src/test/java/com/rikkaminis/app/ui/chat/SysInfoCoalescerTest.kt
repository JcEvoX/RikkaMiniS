package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Collections
import java.util.concurrent.CountDownLatch

/**
 * [FIX-G da1016-sysinfo-race] Pins the state machine of [SysInfoCoalescer] — the
 * single home of the system-notice coalesce window.
 *
 * Why it matters: the window is reached from Main (checkContextBeforeSend,
 * `/compact`, revertCompact, setProviderName) *and* from the agent loop's IO
 * coroutine (ChatContextWindow.trimContextHistoryWindow ← AgentLoopEngine
 * :658 / :864). Before the fix the window lived in four loose `pendingSysInfo*`
 * fields, so an interleaved IO append + Main flush could lose a notice, publish a
 * ChatMessage whose id belonged to a different window than its toolBlocks, or
 * tear the backing ArrayList on the UI thread.
 *
 * The generic parameter is a local stand-in on purpose: the coalescer must stay
 * free of Android/ViewModel dependencies, so the test proves it via a plain data
 * class instead of AssistantBlock.
 */
class SysInfoCoalescerTest {

    private data class Blk(val id: String)

    private class Recorder {
        val windows = mutableListOf<SysInfoCoalescer.Window<Blk>>()
        val publish: (SysInfoCoalescer.Window<Blk>) -> Unit = { w -> windows.add(w) }
        fun ids(): List<String> = windows.flatMap { w -> w.blocks.map { it.id } }
        fun kinds(): List<String> = windows.map { it.iconKind }
        fun firstIds(): List<String> = windows.map { it.firstId }
        fun blockIds(): List<List<String>> = windows.map { w -> w.blocks.map { it.id } }
    }

    private fun coalescer() = SysInfoCoalescer<Blk> { it.id }

    @Test
    fun `same iconKind merges into one window in call order`() {
        val c = coalescer()
        val r = Recorder()
        c.append(Blk("a"), "compact", null, r.publish)
        c.append(Blk("b"), "compact", null, r.publish)
        c.append(Blk("c"), "compact", null, r.publish)
        assertEquals("nothing must be published inside the window", 0, r.windows.size)
        c.flush(r.publish)
        assertEquals(1, r.windows.size)
        assertEquals(listOf("a", "b", "c"), r.ids())
        assertEquals("window id is the FIRST block id", "a", r.firstIds()[0])
        assertEquals("compact", r.kinds()[0])
    }

    @Test
    fun `different iconKind flushes first and preserves order`() {
        val c = coalescer()
        val r = Recorder()
        c.append(Blk("a"), "compact", null, r.publish)
        c.append(Blk("b"), "memory", null, r.publish)
        assertEquals("kind change must flush the previous window", 1, r.windows.size)
        assertEquals(listOf("a"), r.ids())
        assertEquals(listOf("compact"), r.kinds())
        c.append(Blk("c"), "memory", null, r.publish)
        c.flush(r.publish)
        assertEquals(listOf("a", "b", "c"), r.ids())
        assertEquals(listOf("compact", "memory"), r.kinds())
        assertEquals(listOf("a", "b"), r.firstIds())
    }

    @Test
    fun `last non-null payload wins`() {
        val c = coalescer()
        val r = Recorder()
        c.append(Blk("a"), "compact", "p1", r.publish)
        c.append(Blk("b"), "compact", null, r.publish)
        c.flush(r.publish)
        assertEquals("a null payload must not clear an earlier one", "p1", r.windows[0].payload)

        val c2 = coalescer()
        val r2 = Recorder()
        c2.append(Blk("a"), "compact", null, r2.publish)
        c2.append(Blk("b"), "compact", "p2", r2.publish)
        c2.append(Blk("c"), "compact", null, r2.publish)
        c2.flush(r2.publish)
        assertEquals("p2", r2.windows[0].payload)

        val c3 = coalescer()
        val r3 = Recorder()
        c3.append(Blk("a"), "compact", "p3", r3.publish)
        c3.append(Blk("b"), "compact", "p4", r3.publish)
        c3.flush(r3.publish)
        assertEquals("p4", r3.windows[0].payload)
    }

    @Test
    fun `flush clears the window so the next window starts fresh`() {
        val c = coalescer()
        val r = Recorder()
        c.append(Blk("a"), "compact", "old", r.publish)
        c.flush(r.publish)
        c.append(Blk("b"), "compact", null, r.publish)
        c.flush(r.publish)
        assertEquals(2, r.windows.size)
        assertEquals(listOf("a", "b"), r.firstIds())
        assertEquals(listOf(listOf("a"), listOf("b")), r.blockIds())
        assertEquals("old", r.windows[0].payload)
        assertEquals("payload must not leak into the next window", null, r.windows[1].payload)
    }

    @Test
    fun `flush without a window publishes nothing`() {
        val c = coalescer()
        val r = Recorder()
        c.flush(r.publish)
        assertEquals("an empty coalescer must not emit a card", 0, r.windows.size)
        c.append(Blk("a"), "compact", null, r.publish)
        c.flush(r.publish)
        c.flush(r.publish)
        assertEquals("a late timer firing after the drain is a no-op", 1, r.windows.size)
        assertEquals(listOf("a"), r.ids())
    }

    @Test
    fun `kind chain x x y y x windows in call order`() {
        val c = coalescer()
        val r = Recorder()
        val script = listOf("x" to 1, "x" to 2, "y" to 3, "y" to 4, "x" to 5)
        for ((kind, n) in script) c.append(Blk("b$n"), kind, null, r.publish)
        c.flush(r.publish)
        assertEquals(listOf("x", "y", "x"), r.kinds())
        assertEquals(listOf(listOf("b1", "b2"), listOf("b3", "b4"), listOf("b5")), r.blockIds())
        assertEquals(listOf("b1", "b3", "b5"), r.firstIds())
    }

    /**
     * The race the class exists for: appenders and flushers on two threads must
     * not lose, duplicate or corrupt a block. Override the pressure with
     * `-Dsysinfo.race.iters=N` (the local mutation harness cranks it up).
     */
    @Test
    fun `concurrent append and flush conserve every block`() {
        val iterations = System.getProperty("sysinfo.race.iters")?.toIntOrNull() ?: 500
        val c = coalescer()
        val published = Collections.synchronizedList(mutableListOf<Blk>())
        val publish: (SysInfoCoalescer.Window<Blk>) -> Unit = { w ->
            if (w.blocks.isEmpty()) throw IllegalStateException("empty window published")
            published.addAll(w.blocks)
        }
        val appenders = 2
        val flushers = 2
        val start = CountDownLatch(1)
        val failures = Collections.synchronizedList(mutableListOf<Throwable>())
        val threads = mutableListOf<Thread>()
        for (t in 0 until appenders) {
            threads.add(
                Thread {
                    start.await()
                    try {
                        for (i in 0 until iterations) {
                            c.append(Blk("t$t-$i"), "compact", null, publish)
                            Thread.yield()
                        }
                    } catch (e: Throwable) {
                        failures.add(e)
                    }
                },
            )
        }
        for (t in 0 until flushers) {
            threads.add(
                Thread {
                    start.await()
                    try {
                        for (i in 0 until iterations) {
                            c.flush(publish)
                            Thread.yield()
                        }
                    } catch (e: Throwable) {
                        failures.add(e)
                    }
                },
            )
        }
        threads.forEach { it.start() }
        start.countDown()
        threads.forEach { it.join() }
        c.flush(publish)

        assertTrue(
            "worker failures: " + failures.joinToString(" | "),
            failures.isEmpty(),
        )
        val total = appenders * iterations
        assertEquals("blocks published", total, published.size)
        assertEquals(
            "no duplicated blocks",
            total,
            published.map { it.id }.toSet().size,
        )
    }
}

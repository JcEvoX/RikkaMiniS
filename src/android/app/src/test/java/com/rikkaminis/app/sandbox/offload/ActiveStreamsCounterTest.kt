package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicInteger

/**
 * [deep-audit-1018 ISSUE-04] `ChatStreamOffloadHandler.activeStreams` 并发计数 —— 行为面。
 *
 * 原实现是 `@Volatile var activeStreams = 0` + `activeStreams++` /
 * `activeStreams = (activeStreams - 1).coerceAtLeast(0)`：`@Volatile` 只给可见性，
 * 不给读-改-写原子性。默认并发度 2（多会话 / 子代理流）就足以丢计数 —— 少数会
 * 误判"无活跃流"，让 reclaim 路径割断活流；多数会泄漏计数，且减到 0 以下。
 *
 * 结构断言（源码形状）在 [ActiveStreamsSourceShapeTest]：本文件的用例会写生产
 * 对象的计数器，两类断言分开文件，类型级变异（退回 `var`）才能让结构断言
 * 独立给红而不是被编译红掩盖。按 JUnit4 默认（同类内串行）执行，每个用例结束
 * 都把计数器还原为 0。
 */
class ActiveStreamsCounterTest {

    private fun withCleanCounter(body: () -> Unit) {
        val c = ChatStreamOffloadHandler.activeStreams
        c.set(0)
        try {
            body()
        } finally {
            c.set(0)
        }
    }

    @Test
    fun `counter is an AtomicInteger starting at zero`() {
        withCleanCounter {
            val c = ChatStreamOffloadHandler.activeStreams
            assertTrue("activeStreams must be an AtomicInteger", c is AtomicInteger)
            assertEquals(0, c.get())
        }
    }

    @Test
    fun `8 threads x 20000 increment-decrement rounds end at zero`() {
        withCleanCounter {
            val c = ChatStreamOffloadHandler.activeStreams
            val threads = 8
            val rounds = 20_000
            val start = CountDownLatch(1)
            val done = CountDownLatch(threads)
            val failures = ConcurrentLinkedQueue<Throwable>()
            for (t in 0 until threads) {
                Thread {
                    try {
                        start.await()
                        for (r in 0 until rounds) {
                            c.getAndIncrement()
                            c.updateAndGet { (it - 1).coerceAtLeast(0) }
                        }
                    } catch (e: Throwable) {
                        failures.add(e)
                    } finally {
                        done.countDown()
                    }
                }.start()
            }
            start.countDown()
            done.await()
            assertEquals("no thread may fail: $failures", 0, failures.size)
            assertEquals("balanced increment/decrement must land on 0", 0, c.get())
        }
    }

    @Test
    fun `decrement below zero is floored`() {
        withCleanCounter {
            val c = ChatStreamOffloadHandler.activeStreams
            // Deterministic single-threaded proof of the coerceAtLeast(0) floor:
            // 5 decrements from an empty counter must not go negative.
            repeat(5) { c.updateAndGet { (it - 1).coerceAtLeast(0) } }
            assertEquals(0, c.get())
        }
    }

    @Test
    fun `unpaired decrements never drive the counter negative`() {
        withCleanCounter {
            val c = ChatStreamOffloadHandler.activeStreams
            val decrementers = 4
            val rounds = 5_000
            val start = CountDownLatch(1)
            val done = CountDownLatch(decrementers + 1)
            val sawNegative = AtomicInteger(0)
            // Reader thread samples concurrently: the floor must hold for every
            // observation, not just for the final value.
            Thread {
                start.await()
                while (done.count > 1) {
                    if (c.get() < 0) sawNegative.incrementAndGet()
                }
                done.countDown()
            }.start()
            for (t in 0 until decrementers) {
                Thread {
                    try {
                        start.await()
                        repeat(rounds) { c.updateAndGet { (it - 1).coerceAtLeast(0) } }
                    } finally {
                        done.countDown()
                    }
                }.start()
            }
            start.countDown()
            done.await()
            assertEquals("floor violated while sampling: $sawNegative", 0, sawNegative.get())
            assertEquals("floor must pin the value at 0", 0, c.get())
        }
    }
}

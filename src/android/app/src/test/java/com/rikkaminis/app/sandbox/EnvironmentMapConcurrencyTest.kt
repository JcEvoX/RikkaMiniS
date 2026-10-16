package com.rikkaminis.app.sandbox

import java.util.concurrent.ConcurrentHashMap
import java.util.ConcurrentModificationException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1016 FIX-C 3b] — `PRootKernel.customEnvironment`.
 *
 * Writers run at arbitrary times (`boot` seeds PATH/LANG/TZ/PROOT_*,
 * `updateTimezone` / `updateProxy` are driven by broadcasts) while shell
 * startup iterates the map to build the child envp (`PersistentShell`,
 * `ShellExecutor`, `TerminalSession`). The sibling [PRootKernel.bindMounts] got
 * a ConcurrentHashMap in [audit-0917] for exactly this race; this map was left
 * behind in the same declaration block.
 *
 * These tests assert on the *production* map (the object is loadable on the
 * host: no Android calls in its initializer), plus the two claims the KDoc
 * relies on — weak consistency during iteration, and `putIfAbsent` staying
 * atomic even though the declared type is `MutableMap`.
 */
class EnvironmentMapConcurrencyTest {

    @Test
    fun `the environment map and its sibling are concurrent maps`() {
        assertTrue(
            "customEnvironment must not be a plain LinkedHashMap (CME risk while a shell starts): " +
                PRootKernel.customEnvironment.javaClass.name,
            PRootKernel.customEnvironment is ConcurrentHashMap<*, *>,
        )
        assertTrue(
            "bindMounts is the sibling the audit-0917 fix already converted",
            PRootKernel.bindMounts is ConcurrentHashMap<*, *>,
        )
        // Declared type must stay MutableMap — three call sites iterate it and
        // would not compile against a narrower type.
        val declared: MutableMap<String, String> = PRootKernel.customEnvironment
        declared["FIX_C_DECLARED_TYPE_PROBE"] = "1"
        assertEquals("1", PRootKernel.customEnvironment["FIX_C_DECLARED_TYPE_PROBE"])
        PRootKernel.customEnvironment.remove("FIX_C_DECLARED_TYPE_PROBE")
    }

    /**
     * The race the fix removes: writers adding keys while a shell-startup
     * iteration is in flight. On a LinkedHashMap this throws
     * [ConcurrentModificationException]; on a ConcurrentHashMap the iterator is
     * weakly consistent and simply may or may not see the new key.
     */
    @Test
    fun `iteration survives concurrent writers`() {
        val pool = Executors.newFixedThreadPool(8)
        val start = CountDownLatch(1)
        // Readers spin until the writers are done — that is the window the test
        // needs (iteration overlapping mutation), and it is bounded by `stop`
        // rather than by "no failure yet", which never ends on a healthy map.
        val stop = java.util.concurrent.atomic.AtomicBoolean(false)
        val failure = AtomicReference<Throwable>()
        val iterations = AtomicInteger()
        try {
            val readers = (1..4).map {
                pool.submit {
                    start.await()
                    try {
                        while (!stop.get() && !Thread.currentThread().isInterrupted && failure.get() == null) {
                            for ((_, v) in PRootKernel.customEnvironment) {
                                if (v.isEmpty()) throw IllegalStateException("torn value")
                            }
                            iterations.incrementAndGet()
                        }
                    } catch (t: Throwable) {
                        failure.compareAndSet(null, t)
                    }
                }
            }
            val writers = (1..4).map { w ->
                pool.submit {
                    start.await()
                    try {
                        var i = 0
                        while (i < 20_000) {
                            PRootKernel.customEnvironment["FIX_C_CONCURRENT_$w"] = "v$i"
                            if (i % 16 == 0) PRootKernel.customEnvironment.remove("FIX_C_CONCURRENT_$w")
                            PRootKernel.customEnvironment.putIfAbsent("FIX_C_ABSENT_$w", "boot")
                            i++
                        }
                    } catch (t: Throwable) {
                        failure.compareAndSet(null, t)
                    }
                }
            }
            start.countDown()
            // Await the writers first (they are bounded), then stop the
            // readers and await them — a reader only ends when `stop` is set.
            writers.forEach { it.get(60, TimeUnit.SECONDS) }
            stop.set(true)
            readers.forEach { it.get(10, TimeUnit.SECONDS) }
        } finally {
            pool.shutdownNow()
            for (k in listOf("FIX_C_CONCURRENT_1", "FIX_C_CONCURRENT_2", "FIX_C_CONCURRENT_3", "FIX_C_CONCURRENT_4",
                             "FIX_C_ABSENT_1", "FIX_C_ABSENT_2", "FIX_C_ABSENT_3", "FIX_C_ABSENT_4")) {
                PRootKernel.customEnvironment.remove(k)
            }
        }
        val t = failure.get()
        if (t != null) {
            assertTrue(
                "concurrent iteration of customEnvironment failed with ${t.javaClass.name}: ${t.message}",
                t !is ConcurrentModificationException,
            )
        }
        assertTrue("the readers must have actually iterated", iterations.get() > 0)
    }

    /**
     * `putIfAbsent` is the one operation whose *atomicity* the KDoc claims
     * survives the `MutableMap` declared type (Kotlin delegates to
     * `java.util.Map.putIfAbsent`). Racing 16 callers on one key: exactly one
     * may observe `null` — a get-then-put fallback would let several through.
     */
    @Test
    fun `putIfAbsent stays atomic through the MutableMap declared type`() {
        val key = "FIX_C_PUT_IF_ABSENT"
        val map: MutableMap<String, String> = PRootKernel.customEnvironment
        map.remove(key)
        val pool = Executors.newFixedThreadPool(16)
        val start = CountDownLatch(1)
        val winners = AtomicInteger()
        val losers = AtomicInteger()
        try {
            val tasks = (1..16).map { i ->
                pool.submit {
                    start.await()
                    val previous = map.putIfAbsent(key, "v$i")
                    if (previous == null) winners.incrementAndGet() else losers.incrementAndGet()
                }
            }
            start.countDown()
            tasks.forEach { it.get(30, TimeUnit.SECONDS) }
        } finally {
            pool.shutdownNow()
            PRootKernel.customEnvironment.remove(key)
        }
        assertEquals("exactly one caller may win the race", 1, winners.get())
        assertEquals(15, losers.get())
        assertNull("the probe key must be cleaned up", PRootKernel.customEnvironment[key])
    }
}

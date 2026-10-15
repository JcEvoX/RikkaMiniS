package com.rikkaminis.app.provider

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

/**
 * [fix/audit2-cancelbridge-1015] The shared bridge's contract, without sockets:
 * the three load-bearing semantics plus the UNDISPATCHED registration. The
 * end-to-end halves (real providers against a real wedged upstream) are locked
 * by `OpenAIStreamCancelBridgeTest` (OpenAI) and
 * `ProviderCancelBridgeAnthropicGeminiTest` (Anthropic / Gemini).
 */
class StreamCancelBridgeTest {

    private class WorkerCancelled : CancellationException("worker cancelled")

    private fun newScope() = CoroutineScope(Dispatchers.Default + Job())

    @Test
    fun `cancelled incomplete stream tears the transport down`() {
        val scope = newScope()
        val calls = AtomicInteger(0)
        val causeAtCall = AtomicReference<Throwable?>(null)
        val latch = CountDownLatch(1)
        lateinit var bridge: StreamCancelBridge
        val t0 = System.nanoTime()
        bridge = StreamCancelBridge(scope) {
            calls.incrementAndGet()
            causeAtCall.set(bridge.consumerCancellationCause)
            latch.countDown()
        }
        // Let the bridge actually register before cancelling, the way a real
        // worker cancel arrives (after the stream has been running for a while).
        Thread.sleep(150)
        scope.cancel(WorkerCancelled())

        assertTrue("transport must be torn down", latch.await(1, TimeUnit.SECONDS))
        assertTrue("teardown is prompt", (System.nanoTime() - t0) / 1_000_000 < 1000)
        assertNotNull("cancel cause is readable", causeAtCall.get())
        assertTrue("cause is a CancellationException", causeAtCall.get() is CancellationException)
        assertNull("cause is cause-less (kotlinx's own teardown signal)", causeAtCall.get()!!.cause)
        assertNotNull("cause classifies as a consumer-side cancel", causeAtCall.get()!!.asConsumerSideCancellation())
    }

    @Test
    fun `cancel cause is recorded BEFORE the transport is torn down`() {
        val scope = newScope()
        val causeAtCall = AtomicReference<Throwable?>(null)
        val latch = CountDownLatch(1)
        lateinit var bridge: StreamCancelBridge
        bridge = StreamCancelBridge(scope) {
            causeAtCall.set(bridge.consumerCancellationCause)
            latch.countDown()
        }
        Thread.sleep(150)
        scope.cancel(WorkerCancelled())
        assertTrue(latch.await(1, TimeUnit.SECONDS))
        // The socket closing makes the read fail with `SocketException: Socket
        // closed`; the catch must be able to ask the bridge *why*.
        assertNotNull("reason must already be readable inside the teardown", causeAtCall.get())
    }

    @Test
    fun `completed stream is left alone even when its scope is cancelled later`() {
        val scope = newScope()
        val calls = AtomicInteger(0)
        val bridge = StreamCancelBridge(scope) { calls.incrementAndGet() }
        bridge.markStreamCompleted()
        scope.cancel(WorkerCancelled())
        Thread.sleep(400)
        assertEquals("a finished stream must not be cancelled", 0, calls.get())
        assertNull("a finished stream records no cancel cause", bridge.consumerCancellationCause)
    }

    @Test
    fun `dispose after completion does not tear the transport down`() {
        val scope = newScope()
        val calls = AtomicInteger(0)
        val bridge = StreamCancelBridge(scope) { calls.incrementAndGet() }
        bridge.markStreamCompleted()
        bridge.dispose()
        Thread.sleep(400)
        assertEquals(0, calls.get())
    }

    @Test
    fun `live stream does not tear itself down`() {
        val scope = newScope()
        val calls = AtomicInteger(0)
        StreamCancelBridge(scope) { calls.incrementAndGet() }
        Thread.sleep(400)
        assertEquals("no spurious teardown while the stream runs", 0, calls.get())
        scope.cancel()
    }

    @Test
    fun `immediate cancel after install still tears the transport down`() {
        // The registration must land before the constructor returns: a cancel
        // that arrives with no dispatch delay must not fall into a hole.
        val scope = newScope()
        val latch = CountDownLatch(1)
        StreamCancelBridge(scope) { latch.countDown() }
        scope.cancel(WorkerCancelled())
        assertTrue("registered in time", latch.await(1, TimeUnit.SECONDS))
    }

    @Test
    fun `throwing transport is contained and the cause survives`() {
        val scope = newScope()
        val calls = AtomicInteger(0)
        val bridge = StreamCancelBridge(scope) {
            calls.incrementAndGet()
            throw IOException("transport blew up")
        }
        Thread.sleep(150)
        scope.cancel(WorkerCancelled())
        Thread.sleep(400)
        assertEquals("teardown attempted exactly once", 1, calls.get())
        assertNotNull("cause still recorded when the transport throws", bridge.consumerCancellationCause)
    }

    @Test
    fun `consumer failure with a real cause is not reclassified`() {
        // kotlinx wraps a downstream failure as `Channel was consumed, consumer
        // had failed` WITH the original exception in `cause` — that stays an
        // error. Only the cause-less CE is a consumer-side cancel.
        val failure = CancellationException("Channel was consumed, consumer had failed", IOException("boom"))
        assertNull(failure.asConsumerSideCancellation())
        assertNotNull(WorkerCancelled().asConsumerSideCancellation())
    }
}

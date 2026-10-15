package com.rikkaminis.app.provider

import com.rikkaminis.app.data.model.LLMMessage
import com.rikkaminis.app.data.model.LLMModel
import com.rikkaminis.app.provider.anthropic.AnthropicProvider
import com.rikkaminis.app.provider.gemini.GeminiProvider
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

/**
 * [fix/audit2-cancelbridge-1015] The Anthropic / Gemini halves of the shared
 * stream-cancellation bridge, against a REAL wedged upstream.
 *
 * The OpenAI half of this behaviour is locked by `OpenAIStreamCancelBridgeTest`
 * (the bridge was born there, and its `StreamingSseServer` stays byte-identical
 * as an independent oracle — which is why this file carries its own small
 * server instead of sharing one). BUG-08's other half is that
 * AnthropicProvider / GeminiProvider registered nothing before their blocking
 * read and had an empty `awaitClose()`, so a user cancel left the stream parked
 * in `reader.readLine()` until the upstream spoke again or the generation
 * readTimeout (30 min) fired.
 *
 * Every assertion is a measured observable, never inferred from the code:
 *  - `job completes while the upstream is silent` — cancelling the collecting
 *    job returns promptly even though the SSE body never terminates. The
 *    literal bound is 10s; the observable regression is *minutes* (until the
 *    upstream speaks or the 30-minute readTimeout), so anything under 10s can
 *    only be the bridge tearing the socket down.
 *  - `server observes the peer close` — EOF/RST within 10s of the cancel,
 *    recorded at the server end of the same socket (the strongest available
 *    ground truth that the transport really was torn down).
 *  - `no chunk after the cancel` — a cancelled stream must not emit again (the
 *    count at cancel time is captured, not assumed).
 */
class ProviderCancelBridgeAnthropicGeminiTest {

    private companion object {
        const val CANCEL_AFTER_MS = 300L
        const val PROMPT_BOUND_MS = 10_000L

        val ANTHROPIC_START =
            """data: {"type":"message_start","message":{"usage":{"input_tokens":5,"output_tokens":1}}}"""
        val GEMINI_START =
            """data: {"candidates":[{"content":{"parts":[{"text":"hi"}],"role":"model"}}]}"""
    }

    private val servers = mutableListOf<ProbeServer>()

    @After
    fun tearDown() {
        servers.forEach { runCatching { it.close() } }
        servers.clear()
    }

    // -- Anthropic --

    @Test
    fun `anthropic cancel completes while the upstream is silent`() {
        val server = WedgingSseServer(ANTHROPIC_START).also { servers += it }
        val provider = AnthropicProvider(
            apiKey = "test-key",
            model = LLMModel.claudeHaiku45,
            basePath = "http://127.0.0.1:${server.port}",
        )
        val outcome = runStream(provider)
        assertTrue("stream was alive before the cancel (chunk emitted)", outcome.aliveBeforeCancel)
        assertFalse("stream was still running at cancel time", outcome.finishedBeforeCancel)
        assertTrue(
            "collecting job should complete promptly (took ${outcome.elapsedMs}ms; a wedged " +
                "read only ends when the upstream speaks or the generation readTimeout fires)",
            outcome.finished && outcome.elapsedMs < PROMPT_BOUND_MS,
        )
        assertTrue(
            "server should observe the peer close (took ${outcome.peerCloseMs}ms)",
            outcome.peerCloseMs in 0 until PROMPT_BOUND_MS,
        )
        assertTrue("stream had emitted before the cancel", outcome.chunksAtCancel > 0)
        assertEquals(
            "no chunk may arrive after the cancel (before=${outcome.chunksAtCancel}, after=${outcome.chunks})",
            outcome.chunksAtCancel,
            outcome.chunks,
        )
    }

    @Test
    fun `gemini cancel completes while the upstream is silent`() {
        val server = WedgingSseServer(GEMINI_START).also { servers += it }
        val provider = GeminiProvider(
            apiKey = "test-key",
            model = LLMModel.gemini25Flash,
            basePath = "http://127.0.0.1:${server.port}",
        )
        val outcome = runStream(provider)
        assertTrue("stream was alive before the cancel (chunk emitted)", outcome.aliveBeforeCancel)
        assertFalse("stream was still running at cancel time", outcome.finishedBeforeCancel)
        assertTrue(
            "collecting job should complete promptly (took ${outcome.elapsedMs}ms; a wedged " +
                "read only ends when the upstream speaks or the generation readTimeout fires)",
            outcome.finished && outcome.elapsedMs < PROMPT_BOUND_MS,
        )
        assertTrue(
            "server should observe the peer close (took ${outcome.peerCloseMs}ms)",
            outcome.peerCloseMs in 0 until PROMPT_BOUND_MS,
        )
        assertTrue("stream had emitted before the cancel", outcome.chunksAtCancel > 0)
        assertEquals(
            "no chunk may arrive after the cancel (before=${outcome.chunksAtCancel}, after=${outcome.chunks})",
            outcome.chunksAtCancel,
            outcome.chunks,
        )
    }

    // -- connect / header window (audit3 F-05) --
    //
    // The arms above cancel AFTER the first chunk, i.e. inside
    // `reader.readLine()`. These two cancel EARLIER: the upstream has accepted
    // the connection and swallowed the request but has not sent a single
    // response byte, so the client is parked inside `call.execute()` waiting
    // for the status line. That window is where audit2's placement (bridge
    // registered after `execute()`) could not reach a cancel at all: the
    // collecting job is cancelled, the bridge does not exist yet, and the TTFB
    // watchdog dies with the scope — so the socket read runs on to the
    // generation readTimeout. Measured pre-fix: not finished at the 10s bound;
    // post-fix: finished + peer close in milliseconds.
    //
    // The server's `requestSeenAtMs` is the ground truth that the client really
    // is inside `execute()` (it can only have sent the request by calling it)
    // and that it has been given nothing back.

    @Test
    fun `anthropic cancel during the connect window tears the call down`() {
        val server = SilentConnectServer().also { servers += it }
        val provider = AnthropicProvider(
            apiKey = "test-key",
            model = LLMModel.claudeHaiku45,
            basePath = "http://127.0.0.1:${server.port}",
        )
        val outcome = runConnectWindow(provider, server)
        assertTrue(
            "the request must reach the server without an answer (client is in call.execute())",
            outcome.requestSeen,
        )
        assertEquals("nothing may be emitted before the response headers arrive", 0, outcome.chunksAtCancel)
        assertTrue(
            "collecting job should complete promptly (took ${outcome.elapsedMs}ms; without a bridge " +
                "in front of call.execute() a cancel here only ends when the upstream speaks or the " +
                "generation readTimeout fires)",
            outcome.finished && outcome.elapsedMs < PROMPT_BOUND_MS,
        )
        assertTrue(
            "server should observe the peer close (took ${outcome.peerCloseMs}ms)",
            outcome.peerCloseMs in 0 until PROMPT_BOUND_MS,
        )
        assertEquals(
            "no chunk may arrive after the cancel (before=${outcome.chunksAtCancel}, after=${outcome.chunks})",
            outcome.chunksAtCancel,
            outcome.chunks,
        )
        // The job cannot end cleanly here: our cancel closes the socket under the
        // blocking `call.execute()`, so the stream surfaces a SocketException.
        // Assert on it rather than letting an uncaught failure escape the test —
        // a `launch` would hand it to the JVM-wide handler and
        // kotlinx-coroutines-test charges that to the NEXT `runTest` class in
        // this JVM (CI run #2132: SessionConcurrencyManagerTest failed with
        // UncaughtExceptionsBeforeTest).
        assertTrue(
            "the collecting job may only fail with the socket close our own cancel caused (got " +
                "${outcome.failures.map { it.javaClass.simpleName + ": " + it.message }})",
            outcome.failures.all { it is java.io.IOException },
        )
    }

    @Test
    fun `gemini cancel during the connect window tears the call down`() {
        val server = SilentConnectServer().also { servers += it }
        val provider = GeminiProvider(
            apiKey = "test-key",
            model = LLMModel.gemini25Flash,
            basePath = "http://127.0.0.1:${server.port}",
        )
        val outcome = runConnectWindow(provider, server)
        assertTrue(
            "the request must reach the server without an answer (client is in call.execute())",
            outcome.requestSeen,
        )
        assertEquals("nothing may be emitted before the response headers arrive", 0, outcome.chunksAtCancel)
        assertTrue(
            "collecting job should complete promptly (took ${outcome.elapsedMs}ms; without a bridge " +
                "in front of call.execute() a cancel here only ends when the upstream speaks or the " +
                "generation readTimeout fires)",
            outcome.finished && outcome.elapsedMs < PROMPT_BOUND_MS,
        )
        assertTrue(
            "server should observe the peer close (took ${outcome.peerCloseMs}ms)",
            outcome.peerCloseMs in 0 until PROMPT_BOUND_MS,
        )
        assertEquals(
            "no chunk may arrive after the cancel (before=${outcome.chunksAtCancel}, after=${outcome.chunks})",
            outcome.chunksAtCancel,
            outcome.chunks,
        )
        // The job cannot end cleanly here: our cancel closes the socket under the
        // blocking `call.execute()`, so the stream surfaces a SocketException.
        // Assert on it rather than letting an uncaught failure escape the test —
        // a `launch` would hand it to the JVM-wide handler and
        // kotlinx-coroutines-test charges that to the NEXT `runTest` class in
        // this JVM (CI run #2132: SessionConcurrencyManagerTest failed with
        // UncaughtExceptionsBeforeTest).
        assertTrue(
            "the collecting job may only fail with the socket close our own cancel caused (got " +
                "${outcome.failures.map { it.javaClass.simpleName + ": " + it.message }})",
            outcome.failures.all { it is java.io.IOException },
        )
    }

    /**
     * Drives one real stream against a server that never answers and cancels it
     * once the request has provably been received (⇒ the client is blocked in
     * `call.execute()`).
     */
    private fun runConnectWindow(provider: LLMProvider, server: SilentConnectServer): ConnectOutcome {
        val collected = AtomicInteger(0)
        val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
        // This job can only ever end one way: the cancel is delivered by closing
        // the socket under the blocking `call.execute()`, so the stream surfaces
        // a SocketException — an IOException, NOT a CancellationException. A bare
        // `launch` hands an uncaught non-cancellation failure to the JVM-wide
        // handler; kotlinx-coroutines-test records it and charges it to the NEXT
        // `runTest` class in the same JVM (CI run #2132: SessionConcurrencyManagerTest
        // failed with UncaughtExceptionsBeforeTest, with this SocketException as the
        // registered cause). Collect it here and assert on it instead of leaking it.
        val failures = CopyOnWriteArrayList<Throwable>()
        val job = scope.launch(CoroutineExceptionHandler { _, e -> failures += e }) {
            provider.streamMessage(
                messages = listOf(LLMMessage(LLMMessage.Role.USER, "hi")),
                systemPrompt = null,
                maxTokens = 64,
            ).collect { collected.incrementAndGet() }
        }

        val requestDeadline = System.currentTimeMillis() + PROMPT_BOUND_MS
        while (server.requestSeenAtMs.get() < 0 && !job.isCompleted &&
            System.currentTimeMillis() < requestDeadline
        ) {
            Thread.sleep(2)
        }
        val requestSeen = server.requestSeenAtMs.get() > 0

        Thread.sleep(CANCEL_AFTER_MS)
        val t0 = System.currentTimeMillis()
        assertFalse("cancel must arrive while the stream is still running", job.isCompleted)
        val chunksAtCancel = collected.get()
        job.cancel(WorkerCancelled())

        val finishDeadline = System.currentTimeMillis() + PROMPT_BOUND_MS
        while (!job.isCompleted && System.currentTimeMillis() < finishDeadline) Thread.sleep(2)
        val elapsedMs = System.currentTimeMillis() - t0
        val finished = job.isCompleted

        val peerDeadline = System.currentTimeMillis() + PROMPT_BOUND_MS
        while (serverPeerCloseAt() < 0 && System.currentTimeMillis() < peerDeadline) Thread.sleep(2)
        val peerCloseMs = serverPeerCloseAt() - t0

        runCatching { scope.cancel() }
        return ConnectOutcome(
            requestSeen = requestSeen,
            finished = finished,
            elapsedMs = elapsedMs,
            peerCloseMs = peerCloseMs,
            chunksAtCancel = chunksAtCancel,
            chunks = collected.get(),
            failures = failures.toList(),
        )
    }

    private data class ConnectOutcome(
        val requestSeen: Boolean,
        val finished: Boolean,
        val elapsedMs: Long,
        val peerCloseMs: Long,
        val chunksAtCancel: Int,
        val chunks: Int,
        /** Anything the collecting job failed with (see [runConnectWindow]). */
        val failures: List<Throwable>,
    )

    /**
     * Drives one real stream against [provider] and measures it:
     * wait until a chunk proves the stream is alive, assert it is still
     * running, cancel the collecting job (the worker's cancel watcher does the
     * same), then measure how long the stream takes to actually finish and how
     * long until the server sees the socket go.
     */
    private fun runStream(provider: LLMProvider): Outcome {
        val collected = AtomicInteger(0)
        val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
        val job = scope.launch {
            provider.streamMessage(
                messages = listOf(LLMMessage(LLMMessage.Role.USER, "hi")),
                systemPrompt = null,
                maxTokens = 64,
            ).collect { collected.incrementAndGet() }
        }

        val aliveDeadline = System.currentTimeMillis() + 10_000
        while (collected.get() == 0 && !job.isCompleted && System.currentTimeMillis() < aliveDeadline) {
            Thread.sleep(5)
        }
        val aliveBeforeCancel = collected.get() > 0
        val finishedBeforeCancel = job.isCompleted

        Thread.sleep(CANCEL_AFTER_MS)
        val t0 = System.currentTimeMillis()
        assertFalse("cancel must arrive while the stream is still running", job.isCompleted)
        // The upstream is silent from here on, so any *further* chunk can only
        // be a late write from a stream that was supposed to be cancelled. The
        // count is captured rather than hardcoded: how many chunks one row
        // produces is each provider's business (Anthropic's message_start
        // carries usage, for instance).
        val chunksAtCancel = collected.get()
        job.cancel(WorkerCancelled())

        val finishDeadline = System.currentTimeMillis() + PROMPT_BOUND_MS
        while (!job.isCompleted && System.currentTimeMillis() < finishDeadline) Thread.sleep(2)
        val elapsedMs = System.currentTimeMillis() - t0
        val finished = job.isCompleted

        // Give the server-side observation a moment to land, then read it.
        val peerDeadline = System.currentTimeMillis() + PROMPT_BOUND_MS
        while (serverPeerCloseAt() < 0 && System.currentTimeMillis() < peerDeadline) Thread.sleep(2)
        val peerCloseMs = serverPeerCloseAt() - t0

        runCatching { scope.cancel() }
        return Outcome(
            aliveBeforeCancel = aliveBeforeCancel,
            finishedBeforeCancel = finishedBeforeCancel,
            finished = finished,
            elapsedMs = elapsedMs,
            peerCloseMs = peerCloseMs,
            chunksAtCancel = chunksAtCancel,
            chunks = collected.get(),
        )
    }

    private fun serverPeerCloseAt(): Long = servers.firstOrNull()?.peerClosedAtMs?.get() ?: -1

    private data class Outcome(
        val aliveBeforeCancel: Boolean,
        val finishedBeforeCancel: Boolean,
        val finished: Boolean,
        val elapsedMs: Long,
        val peerCloseMs: Long,
        val chunksAtCancel: Int,
        val chunks: Int,
    )

    /**
     * What the test needs from either stub upstream. Both record the one thing
     * that cannot be expressed with MockWebServer's public API — whether the
     * peer actually disappeared — from the server end of the socket.
     */
    private interface ProbeServer {
        val peerClosedAtMs: AtomicLong
        fun close()
    }

    /**
     * A real TCP server that answers once (headers + one `data:` row) and then
     * goes SILENT, holding the connection open.
     */
    private class WedgingSseServer(private val firstRow: String) : ProbeServer {
        private val server = ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"))
        val port: Int get() = server.localPort
        override val peerClosedAtMs = AtomicLong(-1)

        init { thread(isDaemon = true) { runCatching { acceptLoop() } } }

        private fun acceptLoop() {
            while (true) {
                val sock = try { server.accept() } catch (_: Throwable) { return }
                thread(isDaemon = true) { runCatching { serve(sock) } }
            }
        }

        private fun serve(sock: Socket) {
            val ins: InputStream = sock.getInputStream()
            val out = sock.getOutputStream()

            if (!readRequest(ins)) return

            // no Content-Length / no chunked → body = to EOF, connection held
            out.write(("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n" +
                "Cache-Control: no-cache\r\n\r\n").toByteArray())
            out.write((firstRow + "\n").toByteArray())
            out.flush()

            awaitPeerGone(ins, peerClosedAtMs)
        }

        override fun close() { runCatching { server.close() } }
    }

    /**
     * [fix/audit3-cancel-retry-1015 F-05] A real TCP server for the CONNECT /
     * HEADER window: it accepts the connection, swallows the entire request, and
     * then writes nothing at all — no status line, no headers, no body. The
     * client is therefore parked inside `call.execute()` waiting for the status
     * line, which is the window the connect-window arms exercise.
     *
     * `requestSeenAtMs` is the arm's ground truth that the client really is in
     * that window: OkHttp only writes the request from inside `execute()`, and
     * any non-negative answer to "did the request arrive" proves it has been
     * called and has received nothing back.
     */
    private class SilentConnectServer : ProbeServer {
        private val server = ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"))
        val port: Int get() = server.localPort
        val requestSeenAtMs = AtomicLong(-1)
        override val peerClosedAtMs = AtomicLong(-1)

        init { thread(isDaemon = true) { runCatching { acceptLoop() } } }

        private fun acceptLoop() {
            while (true) {
                val sock = try { server.accept() } catch (_: Throwable) { return }
                thread(isDaemon = true) { runCatching { serve(sock) } }
            }
        }

        private fun serve(sock: Socket) {
            val ins: InputStream = sock.getInputStream()
            if (!readRequest(ins)) return
            requestSeenAtMs.compareAndSet(-1, System.currentTimeMillis())
            // Deliberately no response bytes: this is the header wait.
            awaitPeerGone(ins, peerClosedAtMs)
        }

        override fun close() { runCatching { server.close() } }
    }

    private class WorkerCancelled : CancellationException("worker cancelled")
}

/** Reads the request line + headers (+ any Content-Length body) off [ins].
 *  False when the peer left before the request was complete.
 *  Top-level so the file-private stub servers above can use it too. */
private fun readRequest(ins: InputStream): Boolean {
    val head = ByteArrayOutputStream()
    while (true) {
        val b = ins.read()
        if (b == -1) return false
        head.write(b)
        if (head.toString("ISO-8859-1").endsWith("\r\n\r\n")) break
    }
    val headStr = head.toString("ISO-8859-1")
    val contentLength = headStr.lineSequence()
        .firstOrNull { it.lowercase().startsWith("content-length:") }
        ?.substringAfter(":")?.trim()?.toIntOrNull() ?: 0
    var remaining = contentLength
    val buf = ByteArray(8192)
    while (remaining > 0) {
        val n = ins.read(buf, 0, minOf(remaining, buf.size))
        if (n == -1) return false
        remaining -= n
    }
    return true
}

/** Blocks until the peer goes away (or the test shuts us down), recording the
 *  instant it does — the observable that proves the call was torn down. */
private fun awaitPeerGone(ins: InputStream, peerClosedAtMs: AtomicLong) {
    val buf = ByteArray(8192)
    try {
        while (ins.read(buf) >= 0) { /* silent upstream */ }
        peerClosedAtMs.compareAndSet(-1, System.currentTimeMillis())
    } catch (_: Throwable) {
        peerClosedAtMs.compareAndSet(-1, System.currentTimeMillis())
    }
}

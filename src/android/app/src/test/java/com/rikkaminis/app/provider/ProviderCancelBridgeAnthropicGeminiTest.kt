package com.rikkaminis.app.provider

import com.rikkaminis.app.data.model.LLMMessage
import com.rikkaminis.app.data.model.LLMModel
import com.rikkaminis.app.provider.anthropic.AnthropicProvider
import com.rikkaminis.app.provider.gemini.GeminiProvider
import kotlinx.coroutines.CancellationException
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

    private val servers = mutableListOf<WedgingSseServer>()

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
     * A real TCP server that answers once (headers + one `data:` row) and then
     * goes SILENT, holding the connection open. The one thing that cannot be
     * expressed with MockWebServer's public API — whether the peer actually
     * disappeared — is recorded here from the server end of the socket.
     */
    private class WedgingSseServer(private val firstRow: String) {
        private val server = ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"))
        val port: Int get() = server.localPort
        val peerClosedAtMs = AtomicLong(-1)

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

            // request line + headers
            val head = ByteArrayOutputStream()
            while (true) {
                val b = ins.read()
                if (b == -1) return
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
                if (n == -1) return
                remaining -= n
            }

            // no Content-Length / no chunked → body = to EOF, connection held
            out.write(("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n" +
                "Cache-Control: no-cache\r\n\r\n").toByteArray())
            out.write((firstRow + "\n").toByteArray())
            out.flush()

            // block until the peer goes away (or the test shuts us down)
            try {
                while (ins.read(buf) >= 0) { /* silent upstream */ }
                peerClosedAtMs.compareAndSet(-1, System.currentTimeMillis())
            } catch (_: Throwable) {
                peerClosedAtMs.compareAndSet(-1, System.currentTimeMillis())
            }
        }

        fun close() { runCatching { server.close() } }
    }

    private class WorkerCancelled : CancellationException("worker cancelled")
}

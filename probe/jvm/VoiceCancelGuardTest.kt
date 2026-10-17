package probe

import kotlinx.coroutines.*
import okhttp3.*
import okio.Timeout
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Test
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/** Fake okhttp3.Call that blocks inside execute() until cancelled or released. */
class FakeCall(private val request: Request) : Call {
    val cancelCount = AtomicInteger(0)
    private val executed = AtomicBoolean(false)
    private val canceled = AtomicBoolean(false)
    private val latch = CountDownLatch(1)

    override fun request(): Request = request
    override fun execute(): Response {
        executed.set(true)
        latch.await()
        if (canceled.get()) throw java.io.IOException("Canceled")
        return Response.Builder()
            .request(request).protocol(Protocol.HTTP_1_1).code(200).message("OK")
            .body("{}".toResponseBody("application/json".toMediaTypeOrNull()))
            .build()
    }
    override fun cancel() { canceled.set(true); cancelCount.incrementAndGet(); latch.countDown() }
    fun release() { latch.countDown() }
    override fun isExecuted(): Boolean = executed.get()
    override fun isCanceled(): Boolean = canceled.get()
    override fun enqueue(responseCallback: Callback) = throw UnsupportedOperationException()
    override fun clone(): Call = this
    override fun timeout(): Timeout = Timeout.NONE
}

fun waitExecuted(call: FakeCall, ms: Long = 5000) {
    val dl = System.currentTimeMillis() + ms
    while (!call.isExecuted() && System.currentTimeMillis() < dl) Thread.sleep(10)
    if (!call.isExecuted()) throw AssertionError("execute() never started")
    Thread.sleep(150) // let it settle inside the blocking read
}

class VoiceCancelGuardTest {

    // ------------------------------------------------------------------
    // A half — prompt cancellation of the blocking read
    // ------------------------------------------------------------------

    /** FIXED: cancel the job while the read is blocked → the Call IS cancelled
     *  promptly and the CancellationException propagates (never swallowed). */
    @Test
    fun a1_cancelWhileBlockedCancelsTheCallPromptly() {
        val call = FakeCall(Request.Builder().url("http://localhost/voice").build())
        httpClient = okhttp3.Call.Factory { call }
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        var caught: Throwable? = null
        val t0 = System.currentTimeMillis()
        val job = scope.launch {
            try {
                harnessExecuteRequest(Request.Builder().url("http://localhost/voice").build())
            } catch (e: CancellationException) {
                caught = e
                throw e
            } catch (e: Exception) {
                caught = e
            }
        }
        waitExecuted(call)
        job.cancel()
        runBlocking { withTimeout(5000) { job.join() } } // must NOT hang: the watcher interrupts the read
        val elapsed = System.currentTimeMillis() - t0
        assertTrue("join took too long ($elapsed ms) — read was not interrupted", elapsed < 4000)
        // Two deterministic cancels: the watcher hook fires PROMPTLY while the
        // read is still in flight (this is what interrupts execute()), and
        // executeOrCancel's own completion hook fires at the final state.
        assertEquals("the underlying Call must be cancelled twice (watcher + completion hook)",
                2, call.cancelCount.get())
        assertTrue("cancellation must propagate as CancellationException, got $caught",
                caught is CancellationException)
        scope.cancel()
    }

    /** BASELINE (942b0111d): bare execute() — the Call is NOT cancelled by the
     *  job cancellation; it stays in flight until the server answers. */
    @Test
    fun a2_baselineBareExecuteDoesNotCancelTheCall() {
        val call = FakeCall(Request.Builder().url("http://localhost/voice").build())
        httpClient = okhttp3.Call.Factory { call }
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        val job = scope.launch {
            withContext(Dispatchers.IO) {
                try { baselineExecuteRequest(Request.Builder().url("http://localhost/voice").build()) }
                catch (_: Exception) { }
            }
        }
        waitExecuted(call)
        job.cancel()
        Thread.sleep(300)
        assertEquals("baseline bare execute() must NOT cancel the Call on job cancellation",
                0, call.cancelCount.get())
        // release it (simulates the server finally answering) — the coroutine then completes
        call.release()
        runBlocking { withTimeout(5000) { job.join() } }
        scope.cancel()
    }

    /** FIXED: normal completion returns the body and does NOT cancel the Call
     *  (the dispose path keeps the success path zero-cancellation). */
    @Test
    fun a3_normalCompletionReturnsBodyWithoutCancelling() {
        val call = FakeCall(Request.Builder().url("http://localhost/voice").build())
        httpClient = okhttp3.Call.Factory { call }
        var body: String? = null
        runBlocking {
            val job = launch(Dispatchers.IO) {
                val bytes = harnessExecuteRequest(Request.Builder().url("http://localhost/voice").build())
                body = String(bytes, Charsets.UTF_8)
            }
            waitExecuted(call)
            call.release()
            runBlocking { withTimeout(5000) { job.join() } }
        }
        assertEquals("{}", body)
        assertEquals("normal completion must not cancel the Call (dispose path)", 0, call.cancelCount.get())
    }

    // ------------------------------------------------------------------
    // B half — chat-ASR empty-transcript guard
    // ------------------------------------------------------------------

    @Test
    fun b1_nonEmptyContentIsReturnedTrimmed() {
        val data = """{"choices":[{"message":{"content":"  hello world  "}}]}""".toByteArray()
        assertEquals("hello world", parseChatAsrTranscript(data))
    }

    @Test
    fun b2_emptyContentThrowsParse() {
        val data = """{"choices":[{"message":{"content":""}}]}""".toByteArray()
        try {
            parseChatAsrTranscript(data)
            fail("empty content must throw Parse")
        } catch (e: VoiceProviderException.Parse) {
            assertTrue("guard message: " + e.message,
                    e.message!!.contains("Empty ASR transcript in chat ASR response"))
        }
    }

    @Test
    fun b3_missingChoicesThrowsParseNotUndecodable() {
        val data = """{"unrelated":1}""".toByteArray()
        try {
            parseChatAsrTranscript(data)
            fail("missing choices must throw Parse")
        } catch (e: VoiceProviderException.Parse) {
            assertTrue("missing choices is a protocol error, not undecodable: " + e.message,
                    e.message!!.contains("Empty ASR transcript in chat ASR response"))
        }
    }

    @Test
    fun b4_missingMessageThrowsParse() {
        val data = """{"choices":[{"finish_reason":"stop"}]}""".toByteArray()
        try {
            parseChatAsrTranscript(data)
            fail("missing message must throw Parse")
        } catch (e: VoiceProviderException.Parse) {
            assertTrue(e.message!!.contains("Empty ASR transcript in chat ASR response"))
        }
    }

    @Test
    fun b5_undecodableBodyThrowsUndecodableParse() {
        try {
            parseChatAsrTranscript("not json at all".toByteArray())
            fail("undecodable body must throw Parse")
        } catch (e: VoiceProviderException.Parse) {
            assertTrue(e.message!!.contains("Undecodable chat ASR response"))
        }
    }

    /** BASELINE: the old inline tail returned text="" on empty content — the
     *  defect that mapped a malformed chat response to NO_MATCH ("heard
     *  nothing") instead of a protocol error that advances the fail-over chain. */
    @Test
    fun b6_baselineTailReturnsEmptyStringOnEmptyContent() {
        val data = """{"choices":[{"message":{"content":""}}]}""".toByteArray()
        val resp = baselineParseChatAsrText(data)
        assertEquals("", resp.text)
    }

    // ------------------------------------------------------------------
    // C half — UA override pass-through semantics (extracted real function)
    // ------------------------------------------------------------------

    private fun uaOf(builder: Request.Builder): String? =
        builder.build().header("User-Agent")

    @Test
    fun c1_customUserAgentWinsOverBuilderUa() {
        val b = Request.Builder().url("http://localhost/").header("User-Agent", "codex_cli_rs/1.0")
        b.applyUserAgentOverride("MyUA/2.0")
        assertEquals("MyUA/2.0", uaOf(b))
    }

    @Test
    fun c2_nullCustomFallsBackToBrandedDefault() {
        val b = Request.Builder().url("http://localhost/").header("User-Agent", "codex_cli_rs/1.0")
        b.applyUserAgentOverride(null)
        assertEquals("Minis-Test/1.0 (Android 99; sandbox)", uaOf(b))
    }

    @Test
    fun c3_nullDefaultPreservesBuilderUa() {
        val b = Request.Builder().url("http://localhost/").header("User-Agent", "codex_cli_rs/1.0")
        b.applyUserAgentOverride(null, null)
        assertEquals("codex_cli_rs/1.0", uaOf(b))
    }

    @Test
    fun c4_blankCustomFallsBackToBrandedDefault() {
        val b = Request.Builder().url("http://localhost/").header("User-Agent", "codex_cli_rs/1.0")
        b.applyUserAgentOverride("   ")
        assertEquals("Minis-Test/1.0 (Android 99; sandbox)", uaOf(b))
    }
}

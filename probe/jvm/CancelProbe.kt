package probe

import kotlinx.coroutines.*
import okhttp3.*
import okio.Timeout
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.ResponseBody.Companion.toResponseBody
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import com.rikkaminis.app.provider.executeOrCancel

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
            .request(request)
            .protocol(Protocol.HTTP_1_1)
            .code(200)
            .message("OK")
            .body("{}".toResponseBody("application/json".toMediaTypeOrNull()))
            .build()
    }
    override fun cancel() {
        canceled.set(true)
        cancelCount.incrementAndGet()
        latch.countDown()
    }
    fun release() { latch.countDown() }
    override fun isExecuted(): Boolean = executed.get()
    override fun isCanceled(): Boolean = canceled.get()
    override fun enqueue(responseCallback: Callback) = throw UnsupportedOperationException()
    override fun clone(): Call = this
    override fun timeout(): Timeout = Timeout.NONE
}

object CancelProbe {
    fun waitExecuted(call: FakeCall, ms: Long = 5000) {
        val dl = System.currentTimeMillis() + ms
        while (!call.isExecuted() && System.currentTimeMillis() < dl) Thread.sleep(10)
        if (!call.isExecuted()) throw AssertionError("execute() never started")
        Thread.sleep(150) // let it settle inside the blocking read
    }

    /** Cancel the job while the call is blocked; return (cancelCount, elapsedMs). */
    fun probeCancelBlocking(): Pair<Int, Long> {
        val call = FakeCall(Request.Builder().url("http://localhost/").build())
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        val job = scope.launch {
            try {
                call.executeOrCancel()
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
            }
        }
        waitExecuted(call)
        val t0 = System.currentTimeMillis()
        job.cancel()
        runBlocking { withTimeout(10000) { job.join() } }
        val elapsed = System.currentTimeMillis() - t0
        val count = call.cancelCount.get()
        scope.cancel()
        return count to elapsed
    }

    /** Baseline mirror: bare execute() — cancel the job while blocked. */
    fun probeBaselineBlocking(): Int {
        val call = FakeCall(Request.Builder().url("http://localhost/").build())
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        val job = scope.launch {
            withContext(Dispatchers.IO) {
                try { call.execute().use { } } catch (_: Exception) { }
            }
        }
        waitExecuted(call)
        job.cancel()
        Thread.sleep(300)
        // In the baseline the call is NOT cancelled by the job cancellation:
        // release it manually (simulates the server finally answering) and
        // confirm no cancel() ever arrived.
        call.release()
        runBlocking { withTimeout(10000) { job.join() } }
        val count = call.cancelCount.get()
        scope.cancel()
        return count
    }
}

fun main() {
    val (count, elapsed) = CancelProbe.probeCancelBlocking()
    println("FIXED_ARM cancelCount=$count elapsedMs=$elapsed")
    val base = CancelProbe.probeBaselineBlocking()
    println("BASELINE_ARM cancelCount=$base")
}

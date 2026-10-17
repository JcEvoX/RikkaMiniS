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

class FakeCall2(private val request: Request) : Call {
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

/** Variant A: real executeOrCancel (one-arg invokeOnCompletion), cancel while blocked. */
fun armExecuteOrCancel(): String {
    val call = FakeCall2(Request.Builder().url("http://localhost/").build())
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    val job = scope.launch {
        try { call.executeOrCancel() } catch (e: CancellationException) { throw e } catch (_: Exception) { }
    }
    // wait until blocked
    while (!call.isExecuted()) Thread.sleep(10)
    Thread.sleep(150)
    job.cancel()
    Thread.sleep(2000)
    val promptCount = call.cancelCount.get()
    call.release()
    runBlocking { withTimeout(5000) { job.join() } }
    scope.cancel()
    return "promptCancelCount=$promptCount afterReleaseCancelCount=${call.cancelCount.get()}"
}

/** Variant B: child-watcher job (public API) + executeOrCancel, cancel while blocked. */
fun armChildWatcher(): String {
    val call = FakeCall2(Request.Builder().url("http://localhost/").build())
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    val t0 = System.currentTimeMillis()
    val job = scope.launch {
        val watchJob = launch { awaitCancellation() }
        watchJob.invokeOnCompletion { _ -> call.cancel() }
        try { call.executeOrCancel() } catch (e: CancellationException) { throw e } catch (_: Exception) { }
        watchJob.cancel()
    }
    while (!call.isExecuted()) Thread.sleep(10)
    Thread.sleep(150)
    job.cancel()
    runBlocking { withTimeout(5000) { job.join() } }
    val elapsed = System.currentTimeMillis() - t0
    val promptCount = call.cancelCount.get()
    scope.cancel()
    return "joinElapsedMs=$elapsed promptCancelCount=$promptCount"
}

fun main() {
    println("ARM_EXECUTE_OR_CANCEL  " + armExecuteOrCancel())
    println("ARM_CHILD_WATCHER      " + armChildWatcher())
}

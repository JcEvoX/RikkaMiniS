package com.rikkaminis.app.provider

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.launch
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * [fix/audit2-cancelbridge-1015] Shared cancellation BRIDGE for a blocking
 * provider stream — one implementation for OpenAI / Anthropic / Gemini.
 *
 * ## The bug this fixes
 *
 * A provider stream is a `callbackFlow` whose body is SYNCHRONOUS: it blocks in
 * `call.execute()` and then in `reader.readLine()` on the IO pool (see the
 * [fix/provider-stream-flowon] note in each provider). A blocking read is NOT
 * interruptible by coroutine cancellation — measured with a real wedged server:
 * cancelling the outer job, the inner job, a wrapping coroutineScope, and a
 * `withTimeoutOrNull` each still ran the full budget, while `call.cancel()`
 * stopped it in 324ms.
 *
 * `awaitClose { call.cancel() }` cannot cover that window: it is registered only
 * AFTER the loop exits, so a wedged read never reaches it. This bridge is
 * launched INSIDE the flow body, registered BEFORE the first blocking call, so
 * cancelling the producer scope reaches it even while the body is stuck — its
 * transport teardown then closes the socket and unblocks the read.
 *
 * Measured with the real nesting (outer runBlocking -> inner runBlocking ->
 * callbackFlow + flowOn(IO)): 335ms with the bridge vs 60018ms without, user
 * cancel at 300ms.
 *
 * Pairs with the worker's cancel watcher (ModelExecutionService), which cancels
 * the collecting job when the user's cancel file appears — that is what reaches
 * this bridge.
 *
 * ## The three semantics (all load-bearing)
 *
 * 1. **`catch`, not `finally`** — a cancelled coroutine's `CancellationException`
 *    IS its cancellation cause, and that is the only public way to read it
 *    (`Job.getCancellationException` is internal kotlinx API). `finally` would
 *    see the teardown without the reason.
 * 2. **The cause is recorded BEFORE the transport is torn down** — closing the
 *    socket makes the blocking read fail with `SocketException: Socket closed`,
 *    an exception that says "the socket closed", not *why*. The catch that sees
 *    it re-asks this bridge for the reason, so a user cancel is not re-logged as
 *    a transport failure ([F-177]'s classification, second door — see
 *    [asConsumerSideCancellation]).
 * 3. **Only tear the transport down when we are being CANCELLED** — on normal
 *    completion the body is already fully consumed and OkHttp may return the
 *    connection to the pool; cancelling then would throw away a reusable
 *    connection for no reason. Hence [markStreamCompleted] in the body's
 *    `finally`.
 *
 * ## Contract
 *
 * Construct it inside the `callbackFlow` body, before the first blocking call:
 *
 * ```
 * val cancelBridge = StreamCancelBridge(scope = this) { call.cancel() }
 * ```
 * then `markStreamCompleted()` in the body's `finally` (before the response is
 * released) and `dispose()` once the body has left the loop. Read the reason
 * with [consumerCancellationCause] when classifying a stream failure.
 *
 * ## One deliberate refinement over the original inline version
 *
 * The bridge is launched `UNDISPATCHED` so that its registration lands BEFORE
 * the constructor returns. With a plain `launch {}` the body only starts on a
 * later dispatch, so a cancel arriving in that window found no handler at all
 * and the stream stayed wedged exactly as before the fix (measured: transport
 * never invoked when the scope was cancelled immediately after construction;
 * invoked in 218ms with the cancel 150ms later). Nothing about the observable
 * cancel semantics changes — the bridge still waits for cancellation and still
 * runs its teardown on its own dispatcher — it is simply registered in time.
 */
internal class StreamCancelBridge(
    scope: CoroutineScope,
    private val onCancelTransport: () -> Unit,
) {
    /** Set once the stream body has been fully consumed. */
    private val streamCompleted = AtomicBoolean(false)

    /** The bridge's OWN cancellation cause; see semantics 2 above. */
    private val cancelCause = AtomicReference<Throwable?>(null)

    private val job: Job = scope.launch(start = CoroutineStart.UNDISPATCHED) {
        val cause: Throwable? = try {
            awaitCancellation()
            null
        } catch (e: CancellationException) {
            e
        }
        if (!streamCompleted.get()) {
            cancelCause.set(cause)
            try {
                onCancelTransport()
            } catch (_: Exception) {
                // Teardown must never mask the cancellation itself.
            }
        }
    }

    /**
     * Why this stream was torn down, when the teardown came from cancellation
     * rather than from the transport. Null while the stream is still running, on
     * a clean completion, and on any path that never cancelled the transport.
     */
    internal val consumerCancellationCause: Throwable?
        get() = cancelCause.get()

    /**
     * Declare the stream body fully consumed, so the bridge stops treating a
     * later scope cancellation as "this stream was cancelled".
     */
    internal fun markStreamCompleted() {
        streamCompleted.set(true)
    }

    /** Retire the bridge once the body has left the read loop. */
    internal fun dispose() {
        job.cancel()
    }
}

/**
 * [F-177] Provider-side classification of a stream failure.
 *
 * ## The bug this fixes
 *
 * A provider's stream is a `callbackFlow`: the SSE loop runs on the COLLECTOR's
 * dispatcher, so when the worker's collector is torn down mid-stream the
 * producer's `catch (e: Exception)` sees a kotlinx-generated wrapper —
 * `CancellationException: Channel was consumed, consumer had failed`, with the
 * consumer's real exception in `cause` (measured: `f177/exp_f177i`).
 * Production logged it as a hard error for a *user-initiated* cancel:
 *
 * ```
 * [ERROR] [OpenAIProvider] [T321] stream parse exception:
 *   CancellationException: Channel was consumed, consumer had failed
 * ```
 *
 * The worker had thrown `ModelExecutionCancelledException` from its collector
 * when the main process wrote `run-<uuid>/cancel` — i.e. the user tapped stop.
 * The provider then ran `cancel("Stream error", mapError(e))`, which `mapError`
 * resolved to `LLMError.Unknown`: a cancellation was accounted as an unknown
 * provider error, and the log line named the wrapper instead of the cause.
 *
 * ## The rule
 *
 * A `CancellationException` **with no cause** is the coroutine machinery's own
 * signal that this stream was torn down — the enclosing job died / the consumer
 * went away. It is not a stream-parse failure and must not be logged or
 * classified as one.
 *
 * A `CancellationException` **with a cause** is different: kotlinx uses it to
 * carry a *real* downstream failure (`exp_f177i`: a consumer that died of
 * `IOException` also produces `Channel was consumed, consumer had failed`, but
 * with the IOException in `cause`). That must stay an error — hence the
 * `cause == null` guard rather than a bare `is CancellationException` check.
 *
 * This predicate can only ever reclassify the no-cause case, so a genuine
 * stream error cannot be swallowed by it (locked by
 * `ProviderStreamCancellationTest`).
 *
 * Returns the throwable itself when it qualifies, so callers can hand it
 * straight to `ProducerScope.cancel(cause)` without a cast.
 */
internal fun Throwable.asConsumerSideCancellation(): CancellationException? =
    if (this is CancellationException && cause == null) this else null

/**
 * [F-177] Render a throwable's cause chain for diagnostics.
 *
 * The pre-fix log printed only `javaClass.simpleName: message` of the outermost
 * throwable, so the actual failure sat one level down and was never shown —
 * `e.cause` appeared 0 times in `OpenAIProvider.kt` while the worker was logging
 * the real reason (`streamMessage threw: cancelled`) in a different process.
 * Reading a single line could not answer "why did this stream die".
 *
 * Bounded on purpose: the chain of a stream failure is short, and an unbounded
 * walk risks a self-referential chain turning a log line into a hang.
 */
internal fun Throwable.causeChainSummary(maxDepth: Int = 4): String {
    val sb = StringBuilder()
    var current: Throwable? = this
    var depth = 0
    while (current != null && depth < maxDepth) {
        if (depth > 0) sb.append(" <- ")
        sb.append(current.javaClass.simpleName).append(": ").append(current.message ?: "(no message)")
        current = current.cause
        depth++
    }
    return sb.toString()
}

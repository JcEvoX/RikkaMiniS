package com.rikkaminis.app.ui.preview

import java.util.concurrent.atomic.AtomicBoolean

/**
 * Destroy latch + deferred-work registry for ONE offscreen WebView.
 *
 * [deep-audit-1046 S-07] Both `AndroidView` preview hosts (`ui/markdown/KaTeXView.kt`,
 * `ui/sandbox/FilePreviewScreen.kt`) schedule work on their WebView through the
 * View queue (`post { … loadUrl }`, `post { … postDelayed(capture, 100) }`) and
 * then destroy that WebView in `onRelease`. Nothing cancelled the queued work:
 * the teardown called `webView.removeCallbacks(null)`, which AOSP defines as
 * `if (action != null) { … } return false;` — with a null argument it removes
 * NOTHING. The 100 ms capture block therefore still ran on a destroyed WebView,
 * `draw(canvas)` produced an empty bitmap and `KaTeXRendererCache.cache.put(…)`
 * stored it under that formula's key: the formula rendered blank for the rest of
 * the process, from a cache hit that never expires for that key.
 *
 * Deliberately Android-free (`java.util.concurrent` only), so the contract this
 * class encodes — "after release, no deferred block runs and every outstanding
 * runnable is handed back for removal by identity" — is JVM-testable.
 * `WebViewReleaseLatchTest` drives it directly.
 *
 * Why a latch AND a removal list: removal is what actually stops the block, but
 * it can only reach runnables that were registered before the release; the latch
 * is the second half, covering a block that was posted between the snapshot and
 * the destroy (and any future `post` nobody remembered to register).
 */
internal class WebViewReleaseLatch {

    private val released = AtomicBoolean(false)

    private val deferred = mutableListOf<Runnable>()

    /** True once [release] ran; kept as a property so guards read fluently. */
    val isReleased: Boolean get() = released.get()

    /**
     * Cheap guard for the FIRST statement of every `post`/`postDelayed` body.
     * Fail-closed: once [release] ran this is false forever.
     */
    fun canRunDeferred(): Boolean = !released.get()

    /**
     * Remember a runnable so [release] can remove it BY IDENTITY
     * (`View.removeCallbacks(runnable)`), which is the only form that works:
     * the `null` form is a documented no-op. Registering after [release] is
     * dropped — the release snapshot already left, and that runnable's own
     * latch guard is what keeps it from doing damage.
     */
    fun track(task: Runnable) {
        synchronized(deferred) {
            if (released.get()) return
            deferred.add(task)
        }
    }

    /** Number of runnables still waiting for [release]. */
    val pendingCount: Int get() = synchronized(deferred) { deferred.size }

    /**
     * Latch the release and hand back every still-outstanding runnable.
     *
     * Idempotent and safe from any thread: only the first call flips the latch
     * and only that call returns the tasks, so a teardown reached twice (key
     * change + screen exit) never removes the same runnable twice.
     */
    fun release(): List<Runnable> =
        synchronized(deferred) {
            if (!released.compareAndSet(false, true)) return emptyList()
            val outstanding = deferred.toList()
            deferred.clear()
            outstanding
        }
}

/**
 * One step of the WebView teardown. The ORDER is the whole point — see
 * [releasePlan].
 */
internal enum class ReleaseStep {
    /** Drop the view from its parent BEFORE anything else that touches it. */
    DETACH,

    /** Remove the runnables registered with [WebViewReleaseLatch.track]. */
    DROP_DEFERRED,

    STOP_LOADING,

    DESTROY,
}

/**
 * The teardown sequence, derived instead of hand-sequenced at each call site so
 * the two hosts cannot drift apart.
 *
 * [detached] (`attached` in the producer) decides whether [ReleaseStep.DETACH]
 * is part of the plan: `WebView.destroy()` on a view that is still attached is
 * undefined behaviour on some OEMs (ConnectionTerminated / missing renderer),
 * and detaching also keeps any bulk `removeCallbacks` from reaching the shared
 * window handler. Removing the tracked runnables comes before
 * [ReleaseStep.STOP_LOADING] because a queued capture that runs after
 * `stopLoading()` still draws on a dying renderer.
 */
internal fun releasePlan(attached: Boolean): List<ReleaseStep> =
    if (attached) {
        listOf(
            ReleaseStep.DETACH,
            ReleaseStep.DROP_DEFERRED,
            ReleaseStep.STOP_LOADING,
            ReleaseStep.DESTROY,
        )
    } else {
        listOf(
            ReleaseStep.DROP_DEFERRED,
            ReleaseStep.STOP_LOADING,
            ReleaseStep.DESTROY,
        )
    }

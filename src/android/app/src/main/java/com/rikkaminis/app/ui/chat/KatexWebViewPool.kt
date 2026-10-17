package com.rikkaminis.app.ui.chat

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.util.LruCache
import android.webkit.JavascriptInterface
import android.webkit.WebSettings
import android.webkit.WebView
import androidx.compose.ui.unit.IntSize
import com.rikkaminis.app.browser.SafeWebViewClient
import com.rikkaminis.app.ui.markdown.internalFitCaptureSize
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull

/**
 * T155: single offscreen WebView that renders LaTeX (and mhchem chemistry)
 * via KaTeX, returning a Bitmap for inline-bubble display in markdown.
 *
 * Mirrors iOS KaTeXRenderer: one WebView is loaded from assets once,
 * each render call evaluates `renderMath(...)` JS, the bridge fires
 * `AndroidBridge.onRendered(w, h, err)` once KaTeX finishes laying out
 * the formula, then we snapshot the WebView with `view.draw(Canvas)`
 * after resizing it to the measured CSS size. Results are cached by
 * (latex, displayMode, fontSize, isDark) so re-renders during recompose
 * are free.
 *
 * Concurrency: a single Mutex serializes render requests because the
 * JS bridge has only one pending callback slot. A queue of requests
 * sharing the same WebView is enough — math formulas are small and
 * complete in tens of ms each once KaTeX is warm.
 */
internal class KatexRenderResult(val bitmap: Bitmap, val size: IntSize)

internal object KatexWebViewPool {

    private const val TAG = "KatexWebViewPool"
    private const val ASSET_HTML = "file:///android_asset/katex/katex-render.html"
    private const val DEFAULT_FONT_SIZE_PX = 17
    private const val RENDER_TIMEOUT_MS = 4_000L
    private const val KATEX_CACHE_MAX_KB = 64 * 1024
    // Layout viewport for the offscreen WebView, in PHYSICAL pixels. KaTeX
    // needs this many px of width to lay out a formula before reporting its
    // measured width via getBoundingClientRect(). On a density-2.625 device
    // 8192 px ≈ 3120 CSS px ≈ enough for any sane single-line display
    // formula or wide matrix. Smaller values caused wide formulas like
    // `I_c = W_c^{\text{non-private}} \cdot \alpha_c + ...` (≈ 1000 CSS
    // px wide) to be clipped at layout time, and the resulting bitmap
    // showed only the leftmost portion.
    private const val LAYOUT_PIXELS = 8192

    private val mainHandler = Handler(Looper.getMainLooper())
    private val mutex = Mutex()
    /**
     * [fix/audit-b22 / T2-L2] Drop every cached formula bitmap. The cache is
     * byte-budgeted (64 MB) but was never reclaimed under memory pressure, so
     * on a small-heap device it could sit on a large chunk of native bitmaps
     * while the app was being pushed into OOM. Called from MinisApp's CRITICAL
     * trim branch; the next render simply re-renders as a cache miss.
     */
    fun clearRenderCacheForMemoryPressure() {
        runCatching { cache.evictAll() }
    }

    private val cache = object : LruCache<String, KatexRenderResult>(KATEX_CACHE_MAX_KB) {
        override fun sizeOf(key: String, value: KatexRenderResult): Int =
            (value.bitmap.byteCount / 1024).coerceAtLeast(1)
    }

    @Volatile
    private var webView: WebView? = null

    /**
     * [da1019-ISSUE-07] Load coordination for the offscreen WebView. The
     * ready flag and the waiters' deferred used to be two fields juggled
     * separately, so `awaitReady()` could decide "not ready" and then create a
     * deferred in the same instant `onPageFinished` was completing a
     * (still-null) one. See [KatexReadyGate].
     */
    private val readyGate = KatexReadyGate()

    @Volatile
    private var pending: PendingRender? = null
    private var nextRenderToken = 0L

    private class PendingRender(
        val token: Long,
        val deferred: CompletableDeferred<Triple<Int, Int, String>>,
    )

    /**
     * Render [latex] via KaTeX. Returns null on error / timeout. Callers
     * are expected to fall back to displaying the raw LaTeX text.
     */
    suspend fun render(
        context: Context,
        latex: String,
        displayMode: Boolean,
        isDark: Boolean,
        fontSizePx: Int = DEFAULT_FONT_SIZE_PX,
    ): KatexRenderResult? {
        val key = cacheKey(latex, displayMode, isDark, fontSizePx)
        cache.get(key)?.let { return it }
        return mutex.withLock {
            cache.get(key)?.let { return@withLock it }
            val result = renderInternal(context.applicationContext, latex, displayMode, isDark, fontSizePx)
            if (result != null) cache.put(key, result)
            result
        }
    }

    private suspend fun renderInternal(
        appContext: Context,
        latex: String,
        displayMode: Boolean,
        isDark: Boolean,
        fontSizePx: Int,
    ): KatexRenderResult? {
        val wv = ensureWebView(appContext) ?: return null
        if (!awaitReady()) return null

        val deferred = CompletableDeferred<Triple<Int, Int, String>>()
        val token = ++nextRenderToken
        val request = PendingRender(token, deferred)
        pending = request

        val js = "renderMath(" +
            "'${latex.escapeForJs()}', " +
            "$displayMode, " +
            "$fontSizePx, " +
            "$isDark, " +
            "$token" +
            ")"
        runOnMain { wv.evaluateJavascript(js, null) }

        val (w, h, err) = withTimeoutOrNull(RENDER_TIMEOUT_MS) { deferred.await() }
            ?: Triple(0, 0, "timeout")
        // Clear only our own request. The mutex normally serializes this, but
        // identity is still the safety net for cancellation/late callbacks.
        if (pending?.token == request.token) pending = null
        if (err.isNotEmpty() || w <= 0 || h <= 0) {
            android.util.Log.w(TAG, "render failed latex='${latex.take(40)}' err=$err w=$w h=$h")
            return null
        }
        return runOnMainSync { snapshot(wv, w, h) }
    }

    private suspend fun ensureWebView(appContext: Context): WebView? {
        webView?.let { return it }
        return runOnMainSync { createWebView(appContext) }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun createWebView(appContext: Context): WebView? {
        webView?.let { return it }
        val wv = WebView(appContext).apply {
            // Offscreen layout — a real frame is required so KaTeX can measure
            // the formula. We resize to the measured size before snapshotting.
            measure(
                android.view.View.MeasureSpec.makeMeasureSpec(LAYOUT_PIXELS, android.view.View.MeasureSpec.EXACTLY),
                android.view.View.MeasureSpec.makeMeasureSpec(LAYOUT_PIXELS, android.view.View.MeasureSpec.EXACTLY),
            )
            layout(0, 0, LAYOUT_PIXELS, LAYOUT_PIXELS)
            // T208-6: force software layer — hardware-accelerated WebView
            // drawn onto a software Canvas via wv.draw() returns stale or
            // empty pixels because the GPU layer's framebuffer is opaque
            // to software readback. With LAYER_TYPE_SOFTWARE, draw() walks
            // the actual display list and paints into the destination
            // bitmap. (Symptom this fixes: each captured bitmap had the
            // right dimensions but the pixels were the *previous* render's
            // formula — Σ in the integral cell, P=[…] in the matrix cell
            // missing its leading mathbf, etc.)
            setLayerType(android.view.View.LAYER_TYPE_SOFTWARE, null)
            setBackgroundColor(Color.TRANSPARENT)
            settings.javaScriptEnabled = true
            settings.allowFileAccess = true
            settings.cacheMode = WebSettings.LOAD_NO_CACHE
            addJavascriptInterface(JsBridge { w, h, err, callbackToken ->
                val current = pending
                if (current != null && current.token == callbackToken) {
                    current.deferred.complete(Triple(w, h, err))
                }
            }, "AndroidBridge")
            webViewClient = object : SafeWebViewClient() {
                override fun onPageFinished(view: WebView?, url: String?) {
                    super.onPageFinished(view, url)
                    // [da1019-ISSUE-07] Flipping the ready flag and waking the
                    // waiters is ONE atomic step inside the gate; splitting it
                    // (as this used to) let a waiter register a deferred after
                    // the flag was set but before it was visible here.
                    readyGate.markPageFinished()
                }

                /**
                 * [GH#341] The shared offscreen renderer died. Every formula
                 * currently waiting on [readyDeferred] / [pending] would
                 * otherwise block until [RENDER_TIMEOUT_MS] and then report a
                 * generic "timeout" — indistinguishable from a slow render.
                 * Complete them now with a message that names the real cause.
                 *
                 * The pool slot is also cleared so the next [render] rebuilds
                 * the WebView instead of reusing a dead instance.
                 */
                override fun onRendererGone(view: WebView?) {
                    // [sweep1014-P1b] completeExceptionally, NOT complete(Unit):
                    // a plain Unit completion made awaitReady() report ok=true
                    // to waiters that had already grabbed the deferred, so they
                    // went on to evaluateJavascript on the dead WebView and
                    // burned the full 4s timeout. Exceptional completion lets
                    // awaitReady() see the real failure and return false.
                    // [da1019-ISSUE-07] Taking the waiter and clearing the slot
                    // happen under the gate's lock, so a waiter that arrives
                    // concurrently either gets failed here or sees `ready ==
                    // false` and registers a fresh deferred — never a stranded
                    // one.
                    readyGate.markRendererGone(
                        IllegalStateException("renderer process was killed by the system"),
                    )
                    val current = pending
                    pending = null
                    current?.deferred?.complete(
                        Triple(0, 0, "renderer process was killed by the system"),
                    )
                    if (webView === view) webView = null
                    // Overriding this hook replaces the base implementation, so
                    // the dead instance has to be discarded here: a WebView whose
                    // renderer is gone still holds a renderer handle slot.
                    destroyDeadWebView(view)
                    // ponytail: 只失效池槽，不主动重建 | 天花板: 下一次 render()
                    // 会走 ensureWebView 重建（`webView == null` 即触发），所以
                    // 重建是惰性的——在下次渲染前，公式显示占位符 | 升级触发:
                    // 真机日志连续出现 "renderer process was killed by the
                    // system" 且伴随可见的公式空白期，届时在此处预热重建。
                }
            }
            loadUrl(ASSET_HTML)
        }
        webView = wv
        return wv
    }

    private fun snapshot(wv: WebView, w: Int, h: Int): KatexRenderResult? {
        return try {
            val density = wv.context.resources.displayMetrics.density
            // KaTeX reports `w`/`h` in CSS pixels (initial-scale=1.0 → 1 CSS px = 1 dp).
            // The bitmap holds physical pixels (= CSS px × density). T208-5.
            //
            // Important (T208-6): do NOT resize the WebView before drawing.
            // Resizing the body changes its CSS width, which causes the
            // already-laid-out KaTeX HTML to reflow — and reflow shifts the
            // span's left edge / triggers wrap. The previous code resized to
            // pxW × pxH then drew, which captured the post-reflow layout
            // (sometimes with the leading glyph clipped, e.g. the `Σ` cut
            // from `\sum_{i=1}^{n} i^2`). Instead we keep the WebView at its
            // full LAYOUT_PIXELS canvas and let canvas clipping crop to the
            // formula's rectangle at (0,0)–(pxW,pxH). Equivalent to a CSS
            // overflow-hidden viewport of exactly (pxW, pxH) physical px.
            val pxW = (w * density).toInt().coerceAtLeast(1)
            val pxH = (h * density).toInt().coerceAtLeast(1)
            // [fix/memory-hardening-capture-cap] `w`/`h` are KaTeX's JS-reported
            // content box — bound the capture before allocating (see
            // KatexCaptureLimit). Past the cap the bitmap is scaled down and
            // drawn with canvas.scale so the whole formula still lands, at the
            // layout size reported above (unchanged).
            val fit = internalFitCaptureSize(pxW, pxH)
            if (!fit.ok) {
                android.util.Log.w(TAG, "snapshot size rejected ${pxW}x$pxH")
                return null
            }
            if (fit.scale < 1f) {
                android.util.Log.w(
                    TAG,
                    "snapshot ${pxW}x$pxH exceeds cap — scaled to ${fit.width}x${fit.height}"
                )
            }
            val bitmap = Bitmap.createBitmap(fit.width, fit.height, Bitmap.Config.ARGB_8888)
            val canvas = Canvas(bitmap)
            if (fit.scale < 1f) canvas.scale(fit.scale, fit.scale)
            wv.draw(canvas)
            KatexRenderResult(bitmap, IntSize(w, h))
        } catch (e: Throwable) {
            android.util.Log.w(TAG, "snapshot failed: ${e.message}")
            null
        }
    }

    private suspend fun awaitReady(): Boolean {
        // [da1019-ISSUE-07] The "am I ready / give me the awaitable" pair runs
        // under one lock inside the gate, so a callback landing in what used to
        // be the gap between the two steps can no longer strand a freshly
        // created deferred nobody would complete (that path burned the full
        // RENDER_TIMEOUT_MS and dropped the first formula to the raw-LaTeX
        // placeholder).
        // [sweep1014-P1b] onRendererGone completes the deferred exceptionally —
        // await() throws, which must surface as ok=false (caller falls back to
        // the placeholder and the next render rebuilds the WebView), never
        // propagate as a coroutine crash.
        // [rv1014-E6] CancellationException of the CALLER is rethrown by the
        // gate, so a cancelled coroutine still unwinds; swallowing it made
        // renderInternal's withLock block run its fallback path instead.
        val failure = readyGate.await(RENDER_TIMEOUT_MS)
        if (failure is ReadyFailure.RendererGone) {
            android.util.Log.w(TAG, "WebView died before ready: ${failure.cause.message}")
        }
        if (failure != null) android.util.Log.w(TAG, "WebView never reached ready state")
        return failure == null
    }

    private inline fun runOnMain(crossinline block: () -> Unit) {
        if (Looper.myLooper() == Looper.getMainLooper()) block()
        else mainHandler.post { block() }
    }

    private suspend fun <T> runOnMainSync(block: () -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        val out = CompletableDeferred<T>()
        mainHandler.post {
            try {
                out.complete(block())
            } catch (t: Throwable) {
                out.completeExceptionally(t)
            }
        }
        return out.await()
    }

    private fun cacheKey(latex: String, displayMode: Boolean, isDark: Boolean, fontSizePx: Int): String {
        val d = if (displayMode) 'D' else 'I'
        val k = if (isDark) 'k' else 'l'
        return "$d:$k:$fontSizePx:$latex"
    }

    private fun String.escapeForJs(): String =
        this.replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", "\\n")
            .replace("\r", "")

    private class JsBridge(private val onResult: (Int, Int, String, Long) -> Unit) {
        @JavascriptInterface
        fun onRendered(width: Int, height: Int, error: String, token: Long) {
            onResult(width, height, error, token)
        }
    }
}

/**
 * [da1019-ISSUE-07] How the pool's load coordination failed before this type
 * existed: `awaitReady()` checked `isReady` and then — as a SEPARATE step —
 * created/registered `readyDeferred`. Both fields were `@Volatile`, but the
 * compound check-then-act was not atomic, so this interleaving was possible:
 *
 *  1. background renderer: `if (isReady) return true` observes false;
 *  2. MAIN thread, `onPageFinished`: `isReady = true` +
 *     `readyDeferred?.complete(Unit)` (null → no-op);
 *  3. background renderer: `readyDeferred = CompletableDeferred()` — a fresh
 *     deferred nobody will ever complete;
 *  4. the waiting render then burned the full 4s render budget and fell back
 *     to the raw-LaTeX placeholder for the first formula.
 *
 * The gate makes "flip the ready flag" and "grab the waiters' deferred" one
 * atomic step (a single small lock), so only two orderings remain: a waiter
 * that registers first is guaranteed a completer, and a waiter that arrives
 * after the callback short-circuits on the flag.
 *
 * Pure Kotlin (no android.* dependency) so the interleavings are exercised by
 * the JVM probe (`probe/jvm`) against the exact production source, not only
 * on device.
 */
internal sealed interface ReadyFailure {
    /** The page did not finish loading within the caller's budget. */
    object Timeout : ReadyFailure

    /** The renderer died (or the wait failed) — [cause] names the real reason. */
    data class RendererGone(val cause: Throwable) : ReadyFailure
}

/**
 * Ready/waiter coordination between a main-thread "page finished" callback and
 * background waiters. All mutation of [ready]/[waiter] happens inside
 * `synchronized(lock)`; `complete`/`completeExceptionally` are invoked OUTSIDE
 * the lock (never resume a coroutine while holding a monitor).
 */
internal class KatexReadyGate {
    @Volatile
    private var ready = false
    private var waiter: CompletableDeferred<Unit>? = null
    private val lock = Any()

    /** Main thread: the page finished loading — wake every current waiter. */
    fun markPageFinished() {
        val d = synchronized(lock) {
            ready = true
            waiter
        }
        d?.complete(Unit)
    }

    /**
     * The renderer died: fail the waiter (if any) with [cause]; the pool slot
     * is dropped by the caller so the next render rebuilds the WebView.
     */
    fun markRendererGone(cause: Throwable) {
        val d = synchronized(lock) {
            ready = false
            waiter.also { waiter = null }
        }
        d?.completeExceptionally(cause)
    }

    /**
     * Wait until the page is ready, at most [timeoutMs].
     *
     * @return null when the page is (or became) ready;
     *         [ReadyFailure.Timeout] when the budget elapsed first;
     *         [ReadyFailure.RendererGone] when the renderer died while
     *         waiting. `CancellationException` of the CALLING coroutine is
     *         rethrown (see rv1014-E6: structured concurrency must unwind).
     */
    suspend fun await(timeoutMs: Long): ReadyFailure? {
        val d = synchronized(lock) {
            if (ready) return null
            waiter ?: CompletableDeferred<Unit>().also { waiter = it }
        }
        return try {
            if (withTimeoutOrNull(timeoutMs) { d.await() } == null) ReadyFailure.Timeout else null
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            ReadyFailure.RendererGone(e)
        }
    }
}

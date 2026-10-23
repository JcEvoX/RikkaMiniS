package com.rikkaminis.app.browser

import java.io.File
import java.io.FileNotFoundException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1045 B-02] The WebView teardown / JS-dispatch contract, on the JVM.
 *
 * The defect was a *queue* choice, not a logic error: `fetchBlobDownload`
 * injected its FileReader with `webView.post { … }`, which on a DETACHED WebView
 * parks the runnable in `HandlerActionQueue` until a later attach — never, for
 * a headless pool tab. The same ticket covered two more faces of "a dead or
 * off-screen WebView still gets written": `setUserAgent` (both overloads) and
 * the holder's desktop toggle wrote `settings.userAgentString` / `reload()`
 * with no `destroyed` latch, and neither `destroy()` dropped the view's queued
 * callbacks before `webView.destroy()`.
 *
 * A WebView cannot be instantiated on the JVM, so the arms are split:
 *
 *  1. **[jsDispatchFor] — the three-state decision**, exercised as a real
 *     object (it lives at the top level of `BrowserUseManager.kt`, next to
 *     `MinisMainDocGate` / `decodeDataUrlPayload`). Expectations are literals;
 *     nothing is derived from a production constant.
 *  2. **source shapes** — the *wiring* around that decision, asserted on the
 *     real files the way `BrowserTabPoolDisposeScopeTest` pins `dispose()`:
 *     who is gated by the latch, and that callbacks are cleared before the
 *     WebView dies. The mutation arms (each shape reverted → exactly one
 *     criterion red) live in `scripts/scan/browser_destroy_wakeup_check.py`,
 *     which also runs them in CI; this class is the in-suite tripwire so the
 *     contract cannot be silently dropped while the gate file is edited.
 */
class JsDispatchDecisionTest {

    // ── arm 1: the decision ─────────────────────────────────────────────────

    @Test
    fun `a destroyed WebView is refused on any thread`() {
        assertEquals(
            "main thread, destroyed — every WebView method throws on a destroyed instance",
            JsDispatch.REJECT_DESTROYED,
            jsDispatchFor(destroyed = true, onMainThread = true),
        )
        assertEquals(
            "off main, destroyed — the thread hop must not be an escape hatch",
            JsDispatch.REJECT_DESTROYED,
            jsDispatchFor(destroyed = true, onMainThread = false),
        )
    }

    @Test
    fun `a live tab already on the main thread runs inline`() {
        assertEquals(
            "no hop needed — the DownloadListener callback already arrives on main",
            JsDispatch.INLINE,
            jsDispatchFor(destroyed = false, onMainThread = true),
        )
    }

    @Test
    fun `work from another thread hops to the main Looper, never a View queue`() {
        assertEquals(
            "the hop must be the Looper queue: View.post on a detached tab never drains",
            JsDispatch.HOP_TO_MAIN,
            jsDispatchFor(destroyed = false, onMainThread = false),
        )
    }

    @Test
    fun `the decision covers all four inputs and only ever returns a known state`() {
        val seen = mutableSetOf<JsDispatch>()
        for (destroyed in listOf(false, true)) {
            for (onMain in listOf(false, true)) {
                val out = jsDispatchFor(destroyed, onMain)
                assertTrue("unexpected state $out", out in JsDispatch.values())
                seen.add(out)
                if (destroyed) {
                    assertEquals("destroyed must win over the thread", JsDispatch.REJECT_DESTROYED, out)
                } else {
                    assertFalse("a live tab must never be refused", out == JsDispatch.REJECT_DESTROYED)
                }
            }
        }
        assertEquals("all three states are reachable", 3, seen.size)
    }

    // ── arm 2: the wiring on the real files ─────────────────────────────────

    @Test
    fun `the blob download arms its credential only behind the latch`() {
        val body = codeOnly(bodiesAfter(manager(), "fun fetchBlobDownload(").single())
        val latch = body.indexOf("if (destroyed)")
        val arm = body.indexOf("blobDownloadActiveToken = token")
        assertTrue("fetchBlobDownload must check the destroyed latch", latch >= 0)
        assertTrue("fetchBlobDownload must arm the one-shot token", arm >= 0)
        assertTrue(
            "the latch must come BEFORE the arm: a refused dispatch must not leave " +
                "a bridge credential armed for a callback that can never arrive",
            latch < arm,
        )
        assertFalse(
            "the injection must not go through the WebView's View post queue " +
                "(a detached pool tab never drains it)",
            body.contains("webView.post"),
        )
        assertTrue(
            "the injection must still reach the page",
            body.contains("evaluateJavascript("),
        )
    }

    @Test
    fun `every user-agent switch consults the latch before writing`() {
        val switches = bodiesAfter(manager(), "fun setUserAgent(") +
            bodiesAfter(holder(), "fun toggleDesktopMode(")
        assertEquals("two manager overloads + the holder toggle", 3, switches.size)
        for (raw in switches) {
            val body = codeOnly(raw)
            assertTrue(
                "a UA switch that writes settings/reloads without the latch crashes " +
                    "with 'Attempt to perform WebView method on a destroyed WebView'",
                body.contains("destroyed"),
            )
        }
    }

    @Test
    fun `both destroy bodies drop the view's queued callbacks before destroying it`() {
        for ((label, src) in listOf("BrowserUseManager.kt" to manager(), "WebViewHolder.kt" to holder())) {
            val body = codeOnly(bodiesAfter(src, "fun destroy()").single())
            val clear = body.indexOf("removeCallbacks(null)")
            val teardown = body.indexOf("webView.destroy()")
            assertTrue("$label: destroy() must call removeCallbacks(null)", clear >= 0)
            assertTrue("$label: destroy() must still destroy the WebView", teardown >= 0)
            assertTrue("$label: callbacks must be cleared BEFORE webView.destroy()", clear < teardown)
            val detach = minOf(
                body.indexOf("removeView(").takeIf { it >= 0 } ?: Int.MAX_VALUE,
                body.indexOf("detach(").takeIf { it >= 0 } ?: Int.MAX_VALUE,
            )
            assertTrue(
                "$label: the view must be detached before the callbacks are cleared — " +
                    "removeCallbacks(null) on an ATTACHED view drains the shared window " +
                    "handler's null-token messages",
                detach < clear,
            )
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private fun manager(): String =
        readRepoFile("src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserUseManager.kt")

    private fun holder(): String =
        readRepoFile("src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebViewHolder.kt")

    private fun readRepoFile(relative: String): String {
        var dir: File? = File(System.getProperty("user.dir") ?: ".")
        while (dir != null) {
            val f = File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw FileNotFoundException("$relative not found from user.dir=${System.getProperty("user.dir")}")
    }

    /**
     * Body with its comments blanked. A KDoc that *mentions* a shape is not
     * that shape — the production comments here name `View.post` and
     * `removeCallbacks(null)` on purpose, and a raw `contains` would read them
     * as code.
     */
    private fun codeOnly(body: String): String {
        val out = StringBuilder()
        var i = 0
        while (i < body.length) {
            if (body.startsWith("//", i)) {
                val j = body.indexOf('\n', i)
                i = if (j < 0) body.length else j
                continue
            }
            if (body.startsWith("/*", i)) {
                val j = body.indexOf("*/", i + 2)
                i = if (j < 0) body.length else j + 2
                continue
            }
            out.append(body[i])
            i++
        }
        return out.toString()
    }

    /** Body of every `marker` occurrence — brace-balanced, raw strings skipped. */
    private fun bodiesAfter(src: String, marker: String): List<String> {
        val out = mutableListOf<String>()
        var from = 0
        while (true) {
            val at = src.indexOf(marker, from)
            if (at < 0) return out
            val brace = src.indexOf('{', at)
            if (brace < 0) return out
            val body = bodyAt(src, brace)
            out.add(body)
            from = brace + body.length
        }
    }

    private fun bodyAt(src: String, braceAt: Int): String {
        var depth = 0
        var k = braceAt
        while (k < src.length) {
            if (src.startsWith("\"\"\"", k)) {
                val end = src.indexOf("\"\"\"", k + 3)
                k = if (end < 0) src.length else end + 3
                continue
            }
            val c = src[k]
            if (c == '"' || c == '\'') {
                k = skipString(src, k)
                continue
            }
            if (c == '{') {
                depth++
            } else if (c == '}') {
                depth--
                if (depth == 0) return src.substring(braceAt, k + 1)
            }
            k++
        }
        return src.substring(braceAt)
    }

    private fun skipString(src: String, at: Int): Int {
        val quote = src[at]
        var i = at + 1
        while (i < src.length && src[i] != quote) {
            i += if (src[i] == '\\') 2 else 1
        }
        return i + 1
    }
}

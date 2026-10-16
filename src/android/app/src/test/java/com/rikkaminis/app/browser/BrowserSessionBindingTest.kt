package com.rikkaminis.app.browser

import java.io.File
import java.io.FileNotFoundException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1016 9c/9d] Session binding for the shared shell browser pool,
 * and the session-scoped viewport, pinned on the JVM.
 *
 * Both decisions live in the pure [BrowserSessionBinding] because the code
 * around them needs a Context, a WebView and a filesystem. The arms that
 * matter:
 *
 *  - 9c: `minis-browser-use` must follow the *requesting* chat session, or
 *    minis:// resolution and downloads land in the placeholder session tree.
 *    Re-binding to the same session must stay a no-op — dropping the viewport
 *    override on every call would break the multi-call `set_viewport` …
 *    `get_page_info` sequence.
 *  - 9d: "the new session has no override" and "keep the previous session's
 *    override" must not be the same code path; a partial/degenerate pair
 *    collapses to no-override instead of leaving the old size applied.
 *
 * Expectations are literals; nothing is derived from a production constant, so
 * a changed constant shows up as a red test rather than a silently adjusted
 * expectation.
 */
class BrowserSessionBindingTest {

    // ── 9c: re-pointing the shared pool at a session ─────────────────────

    @Test
    fun `same session is a no-op`() {
        val decision = BrowserSessionBinding.decideRebind("session-a", "session-a")
        assertFalse("a repeat call must not re-bind", decision.rebind)
        assertFalse("a repeat call must not drop the viewport override", decision.resetViewport)
    }

    @Test
    fun `different session rebinds and drops the previous override`() {
        val decision = BrowserSessionBinding.decideRebind("session-a", "session-c")
        assertTrue("a new session must re-bind", decision.rebind)
        assertTrue("the previous session's viewport must not carry over", decision.resetViewport)
    }

    @Test
    fun `never bound pool counts as a change`() {
        val decision = BrowserSessionBinding.decideRebind(null, "session-a")
        assertTrue("a pool with no session must bind", decision.rebind)
        assertTrue("no session means no override to preserve", decision.resetViewport)
    }

    // ── 9d: viewport restored from a session's persisted state ───────────

    @Test
    fun `a complete positive pair is restored verbatim`() {
        val viewport = BrowserSessionBinding.viewportForLoadedSession(390, 844)
        assertEquals("width", 390, viewport.width)
        assertEquals("height", 844, viewport.height)
        assertTrue("390x844 is a real override", viewport.hasOverride)
    }

    @Test
    fun `absent override collapses to no-override`() {
        val viewport = BrowserSessionBinding.viewportForLoadedSession(0, 0)
        assertEquals("width", 0, viewport.width)
        assertEquals("height", 0, viewport.height)
        assertFalse("0x0 means no override", viewport.hasOverride)
    }

    @Test
    fun `half a viewport is not an override`() {
        // The pre-9d shape skipped the write entirely for these, which left the
        // previous session's size applied — the value the flow must land on is
        // "no override", not "whatever was there".
        for ((w, h) in listOf(390 to 0, 0 to 844)) {
            val viewport = BrowserSessionBinding.viewportForLoadedSession(w, h)
            assertEquals("width for ${w}x$h", 0, viewport.width)
            assertEquals("height for ${w}x$h", 0, viewport.height)
            assertFalse("${w}x$h is not an override", viewport.hasOverride)
        }
    }

    @Test
    fun `corrupt negative values do not resurrect the old size`() {
        val viewport = BrowserSessionBinding.viewportForLoadedSession(-390, 844)
        assertEquals("width", 0, viewport.width)
        assertEquals("height", 0, viewport.height)
        assertFalse("a negative width is corrupt, not an override", viewport.hasOverride)
    }

    // ── production wiring (source shape) ────────────────────────────────

    @Test
    fun `bindSessionContextOnly rebinds without loading persisted state`() {
        val body = memberSlice(readRepoFile(TabPoolPath), "fun bindSessionContextOnly(")
        assertTrue(
            "bindSessionContextOnly no longer consults the binding decision:\n$body",
            body.contains("BrowserSessionBinding.decideRebind("),
        )
        assertTrue(
            "the bound session must be stamped onto live tabs as well",
            body.contains("it.manager.minisSessionId = sessionId"),
        )
        assertTrue(
            "a session change must drop the previous viewport override",
            body.contains("if (decision.resetViewport) {") &&
                body.contains("_sessionViewportWidth.value = 0") &&
                body.contains("_sessionViewportHeight.value = 0"),
        )
        // The whole point of the context-only bind: setSession() also loads the
        // new session's browser_tabs/<sid>.json, which would drag that session's
        // saved tabs into the shared driver pool on every shell call.
        assertFalse(
            "bindSessionContextOnly must not load another session's persisted state:\n$body",
            body.contains("loadSavedState("),
        )
    }

    @Test
    fun `the shell handler binds the pool to the requesting session before executing`() {
        val src = readRepoFile(OffloadHandlerPath)
        val idxBind = src.indexOf("bindSessionContextOnly(")
        val idxExec = src.indexOf("sharedBrowserTabPool.execute(")
        assertTrue(
            "the shell handler no longer binds the shared pool to the requesting session",
            idxBind >= 0,
        )
        assertTrue(
            "the binding must happen before the pool executes the action " +
                "(bind@$idxBind execute@$idxExec)",
            idxBind < idxExec,
        )
        assertTrue(
            "the binding must use the request's session id",
            src.substring(maxOf(0, idxBind - 240), idxBind).contains("input.sessionId?.let"),
        )
        assertFalse(
            "the handler must not re-point the pool through setSession (it loads " +
                "another session's persisted tabs)",
            src.contains("sharedBrowserTabPool.setSession("),
        )
    }

    @Test
    fun `loadSavedState clears a stale viewport on both no-override paths`() {
        val body = memberSlice(readRepoFile(TabPoolPath), "private fun loadSavedState(")

        // arm 1: the session has no state file at all — the pre-9d code cleared
        // savedURLs here (rv1014-E5) but left the previous viewport applied.
        val noFile = body.substringAfter("if (!file.exists()) {").substringBefore("return")
        assertTrue(
            "the no-state-file branch must drop the previous session's viewport:\n$noFile",
            noFile.contains("_sessionViewportWidth.value = 0") &&
                noFile.contains("_sessionViewportHeight.value = 0"),
        )

        // arm 2: a state file exists but carries no override — the write must be
        // unconditional, so "no override" cannot mean "keep the old one".
        val idxDecision = body.indexOf("BrowserSessionBinding.viewportForLoadedSession(")
        val idxWidth = body.indexOf("_sessionViewportWidth.value = restoredViewport.width")
        val idxHeight = body.indexOf("_sessionViewportHeight.value = restoredViewport.height")
        assertTrue("loadSavedState no longer uses the viewport decision core:\n$body", idxDecision >= 0)
        assertTrue("the restored width must come from the decision core", idxWidth > idxDecision)
        assertTrue("the restored height must come from the decision core", idxHeight > idxWidth)
        assertFalse(
            "loadSavedState must not gate the write on a positive pair again — " +
                "skipping it is exactly how the previous session's size survived:\n$body",
            body.contains("if (w > 0 && h > 0) {"),
        )
    }

    // ── helpers ─────────────────────────────────────────────────────────

    private fun readRepoFile(relative: String): String {
        // Gradle (CI) sets user.dir to the module dir; a sandbox JVM run may
        // start anywhere under the repo — walk up instead of assuming a depth.
        var dir: File? = File(System.getProperty("user.dir"))
        while (dir != null) {
            val f = File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw FileNotFoundException("$relative not found from user.dir=${System.getProperty("user.dir")}")
    }

    /** Lines of the member declared at [marker], up to the next sibling member. */
    private fun memberSlice(src: String, marker: String): String {
        val lines = src.lines()
        val start = lines.indexOfFirst { it.contains(marker) }
        assertTrue("marker not found: $marker", start >= 0)
        val end = (start + 1 until lines.size).firstOrNull { i ->
            val t = lines[i]
            t.startsWith("    /**") || t.startsWith("    @") ||
                t.startsWith("    fun ") || t.startsWith("    internal fun ") ||
                t.startsWith("    private fun ") ||
                t.startsWith("    internal val ") || t.startsWith("    private val ") ||
                t.startsWith("    // --")
        } ?: lines.size
        return lines.subList(start, end).joinToString("\n")
    }

    private companion object {
        private const val TabPoolPath =
            "src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserTabPool.kt"
        private const val OffloadHandlerPath =
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/BrowserUseOffloadHandler.kt"
    }
}

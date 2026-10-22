package com.rikkaminis.app.sandbox.offload

import java.io.File
import java.io.FileNotFoundException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv22-A6 A-06] — `android-clipboard clear` used to carry two defects in
 * one line each:
 *
 *   1. `setPrimaryClip(ClipData.newPlainText("", ""))` was reported as
 *      `{"cleared": true}`, but the clipboard still held that empty item —
 *      `status` kept answering `has_content: true`, `item_count: 1`. On
 *      Android 10+ `ClipboardManager.clearPrimaryClip()` is the call that
 *      really empties the clipboard; the empty item remains the only
 *      reliable shape on API 26–28, so the fix splits on the platform level.
 *   2. Its background guard was the only one of get/set/clear/status missing
 *      the `&& SDK_INT >= Q` half, so a background clear on 8.0–9.0 — which
 *      those releases allow — was refused as `clipboard_requires_foreground`.
 *
 * [ClipboardOffloadHandler.clearAction] owns the platform split (pinned here
 * as behaviour, on literals); the source-shape tests pin that `doClear`
 * really routes through that decision, keeps both write arms, and keeps the
 * shared guard shape. Every negative control mutates a copy of the real
 * source and asserts the very same check that passes on the tree goes red
 * on the mutation — including the comments-stripped variant, because the
 * production comments *quote* both the old and the new spelling.
 */
class ClipboardClearPlanTest {

    private val handler =
        "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ClipboardOffloadHandler.kt"

    // ── the decision, as behaviour (literal expectations) ───────────────────

    @Test
    fun `Q and later clears through the platform call`() {
        assertEquals(
            ClipboardClearAction.PlatformClear,
            ClipboardOffloadHandler.clearAction(atLeastQ = true),
        )
    }

    @Test
    fun `pre-Q keeps the empty string fallback`() {
        assertEquals(
            ClipboardClearAction.EmptyStringFallback,
            ClipboardOffloadHandler.clearAction(atLeastQ = false),
        )
    }

    // ── the source shape: doClear routes through the decision ───────────────

    @Test
    fun `doClear reaches the platform clear AND keeps the pre-Q write`() {
        val body = doClearBody(readHandler())
        assertTrue(
            "clear must call the platform clearPrimaryClip() on Q+",
            body.contains("cm.clearPrimaryClip()"),
        )
        assertTrue(
            "the API 26-28 fallback write must survive (clearPrimaryClip is not reliable pre-Q)",
            body.contains("""setPrimaryClip(ClipData.newPlainText("", ""))"""),
        )
        assertTrue(
            "doClear must consult the platform decision instead of branching ad hoc",
            body.contains("clearAction("),
        )
    }

    @Test
    fun `negative control - reverting clear to the empty-string write must fail the same check`() {
        // The mutation targets `cm.clearPrimaryClip()` — the KDoc quotes the
        // bare name, so the `cm.`-prefixed call is unique to the code.
        val mutated = readHandler().replace(
            "cm.clearPrimaryClip()",
            """cm.setPrimaryClip(ClipData.newPlainText("", ""))""",
        )
        assertFalse(
            "the mutation must actually apply to this file",
            mutated == readHandler(),
        )
        assertFalse(
            "the mutated doClear lost the platform clear — the check above must go red",
            doClearBody(mutated).contains("cm.clearPrimaryClip()"),
        )
    }

    @Test
    fun `the four foreground guards share one shape`() {
        val code = codeOnly(readHandler())
        val guarded = Regex(
            """!isAppForeground\(\)\s*&&\s*Build\.VERSION\.SDK_INT\s*>=\s*Build\.VERSION_CODES\.Q""",
        ).findAll(code).count()
        assertTrue(
            "get/set/clear/status each guard once (found $guarded)",
            guarded >= 4,
        )
        assertFalse(
            "no guard may drop the SDK half — a pre-Q background clear is legal",
            Regex("""!isAppForeground\(\)(?!\s*&&)""").containsMatchIn(code),
        )
    }

    @Test
    fun `negative control - the baseline bare guard must fail the same check`() {
        // Mutated inside the doClear member only, so the other three guards
        // stay intact and the catch is attributable to the clear path.
        val code = codeOnly(readHandler())
        val body = braceSlice(code, "private fun doClear(")
        val mutatedBody = body.replaceFirst(
            "!isAppForeground() && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q",
            "!isAppForeground()",
        )
        assertFalse("the mutation must actually apply to doClear", mutatedBody == body)
        val mutated = code.replace(body, mutatedBody)
        assertTrue(
            "the baseline bare guard (pre-Q clear refused) must be caught",
            Regex("""!isAppForeground\(\)(?!\s*&&)""").containsMatchIn(mutated),
        )
        val stillGuarded = Regex(
            """!isAppForeground\(\)\s*&&\s*Build\.VERSION\.SDK_INT\s*>=\s*Build\.VERSION_CODES\.Q""",
        ).findAll(mutated).count()
        assertEquals("exactly the doClear guard was un-shaped", 3, stillGuarded)
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /** Comments stripped first: the production KDoc quotes both spellings. */
    private fun doClearBody(src: String): String =
        braceSlice(codeOnly(src), "private fun doClear(")

    private fun readHandler(): String {
        val f = File(repoRoot(), handler)
        if (!f.exists()) throw FileNotFoundException(f.path)
        return f.readText()
    }

    private fun repoRoot(): File {
        System.getProperty("probe.repo")?.let { return File(it) }
        var dir: File? = File(System.getProperty("user.dir"))
        while (dir != null) {
            if (File(dir, handler).exists()) return dir
            dir = dir.parentFile
        }
        throw FileNotFoundException(
            "repo root not found from user.dir=${System.getProperty("user.dir")}",
        )
    }

    /** `src` from the first `{` after [marker], balanced. */
    private fun braceSlice(src: String, marker: String): String {
        val at = src.indexOf(marker)
        assertTrue("marker not found: $marker", at >= 0)
        val open = src.indexOf('{', at)
        assertTrue("no block after: $marker", open >= 0)
        var depth = 0
        var k = open
        while (k < src.length) {
            when (src[k]) {
                '{' -> depth += 1
                '}' -> {
                    depth -= 1
                    if (depth == 0) return src.substring(open, k + 1)
                }
            }
            k += 1
        }
        assertTrue("unbalanced braces after: $marker", false)
        return ""
    }

    /**
     * Comments stripped — hand-written linear scan on purpose (the regex
     * form recurses once per character of a block comment and blew the JVM
     * stack on the 64 KB FastioOffloadHandler; see
     * [OffloadPayloadJsonShapeTest]).
     */
    private fun codeOnly(src: String): String {
        val out = StringBuilder(src.length)
        var i = 0
        while (i < src.length) {
            val c = src[i]
            if (c == '/' && i + 1 < src.length && src[i + 1] == '/') {
                while (i < src.length && src[i] != '\n') i++
            } else if (c == '/' && i + 1 < src.length && src[i + 1] == '*') {
                i += 2
                while (i + 1 < src.length && !(src[i] == '*' && src[i + 1] == '/')) i++
                i = if (i + 2 > src.length) src.length else i + 2
            } else {
                out.append(c)
                i += 1
            }
        }
        return out.toString()
    }
}

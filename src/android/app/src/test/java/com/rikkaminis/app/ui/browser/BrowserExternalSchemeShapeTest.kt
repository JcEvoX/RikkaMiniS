package com.rikkaminis.app.ui.browser

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.FileNotFoundException

/**
 * [deep-audit-1046 FIX-F9] Source-shape pins for the `intent://` path that is
 * fed by arbitrary WEB PAGES.
 *
 * The defect was a scope problem as much as a code problem: this handler
 * carried `Intent.parseUri(url, …)` + `selector = null` as its entire
 * hardening, and no scanner in `scripts/scan/` had ever read the file (the R2
 * criterion only walked `sandbox/offload/`). The gate now derives its scope
 * from the tree; these pins keep the shape itself honest from the test side —
 * delegation to the shared sanitizer, scheme normalisation before parsing, and
 * a scheme-gated `browser_fallback_url`.
 *
 * Repo root comes from -Dprobe.repo when set, else it walks up from user.dir.
 */
class BrowserExternalSchemeShapeTest {

    private val handler =
        "src/android/app/src/main/java/com/rikkaminis/app/ui/browser/BrowserExternalSchemeHandler.kt"

    @Test
    fun `the parsed intent goes through the shared sanitizer`() {
        val body = declarationSlice(readRepoFile(handler), "private fun handleIntentScheme(")
        assertTrue(
            "the browser path must share the same hardening as android-open",
            body.contains("IntentSanitizer.sanitize(this, context)"),
        )
        // The regression to pin: a second, weaker copy of the sequence.
        assertFalse(
            "a local strip copy came back in handleIntentScheme",
            body.contains("removeFlags("),
        )
        assertTrue("CATEGORY_BROWSABLE is part of the routing contract",
            body.contains("addCategory(Intent.CATEGORY_BROWSABLE)"))
        assertTrue(body.contains("Intent.FLAG_ACTIVITY_NEW_TASK"))
    }

    @Test
    fun `the page's raw casing never reaches the parser`() {
        val body = declarationSlice(readRepoFile(handler), "private fun handleIntentScheme(")
        // `Intent.parseUri` matches `intent:` / `android-app:` case-SENSITIVELY;
        // a page writing `INTENT://…` used to degrade into ACTION_VIEW over the
        // raw string (`ERR_UNKNOWN_URL_SCHEME` for the user).
        assertTrue(
            "parseUri must receive a scheme-normalised copy",
            body.contains("Intent.parseUri(lowercaseIntentScheme(url), Intent.URI_INTENT_SCHEME)"),
        )
        assertFalse("raw page casing handed to parseUri", body.contains("Intent.parseUri(url,"))
    }

    @Test
    fun `browser_fallback_url is scheme-gated before the second launch`() {
        val body = declarationSlice(readRepoFile(handler), "private fun handleIntentScheme(")
        assertTrue(
            "page-supplied fallback must be filtered down to http(s)",
            body.contains("IntentSanitizePolicy.allowedFallbackUrl("),
        )
        // The raw extra must never be the thing that gets launched.
        assertFalse(
            "raw browser_fallback_url reached a launch",
            body.contains("Intent(Intent.ACTION_VIEW, fallback.toUri())") &&
                !body.contains("allowedFallbackUrl("),
        )
    }

    @Test
    fun `the KDoc no longer claims a strip the code never performed`() {
        val src = readRepoFile(handler)
        // [deep-audit-1046 FIX-F9] the header claimed `We strip
        // FLAG_ACTIVITY_NEW_DOCUMENT` with 0 occurrences in the code.
        assertFalse(
            "KDoc claims a NEW_DOCUMENT strip that does not exist",
            src.contains("FLAG_ACTIVITY_NEW_DOCUMENT"),
        )
        assertTrue("the KDoc must say what actually happens now",
            src.contains("IntentSanitizer"))
    }

    // ── helpers (same technique as PhotosImportShapeTest) ─────────────────

    private fun readRepoFile(relative: String): String {
        val f = File(repoRoot(), relative)
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
        throw FileNotFoundException("repo root not found from user.dir=${System.getProperty("user.dir")}")
    }

    private fun declarationSlice(src: String, marker: String): String {
        val lines = src.lines()
        val start = lines.indexOfFirst { it.contains(marker) }
        assertTrue("marker not found: $marker", start >= 0)
        val end = (start + 1 until lines.size).firstOrNull { i ->
            val t = lines[i]
            t.startsWith("    /**") || t.startsWith("    @") ||
                t.startsWith("    fun ") || t.startsWith("    internal fun ") ||
                t.startsWith("    private fun ") || t.startsWith("    public fun ") ||
                t.startsWith("    internal val ") || t.startsWith("    private val ")
        } ?: lines.size
        return lines.subList(start, end).joinToString("\n")
    }
}

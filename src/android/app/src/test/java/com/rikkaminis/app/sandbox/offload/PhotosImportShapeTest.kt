package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.FileNotFoundException

/**
 * [deep-audit-1016c FIX-B A + B] Wiring/source-shape pins for the two shipped
 * fixes. Behavioural halves live in PhotosDateRangePureTest /
 * PhotosImportJoinTest (pure logic) and in the repo's own
 * SessionPathResolutionTest (session-aware resolver, three arms). What is left
 * — "is the handler actually CALLING those, and did the old shapes come back?"
 * — is only observable in the source, so this class asserts on the source of
 * the exact declaration that was changed (same technique as
 * SessionPathResolutionTest / SessionScopedPathGuardTest).
 *
 * Repo root comes from -Dprobe.repo when set, else it walks up from user.dir.
 */
class PhotosImportShapeTest {

    private val photos = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/PhotosOffloadHandler.kt"
    private val open = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/OpenOffloadHandler.kt"

    // ── A: import path resolution ─────────────────────────────────────────

    @Test
    fun `import resolves a guest path through the session-aware resolver`() {
        val body = declarationSlice(readRepoFile(photos), "private fun handleImport(")
        assertTrue("handleImport must take the request (session id + cwd)", body.contains("request: NativeOffloadRequest"))
        assertTrue("must normalize against the guest cwd", body.contains("GuestPathMapper.absoluteGuestPath(request.cwd,"))
        assertTrue(
            "must resolve through the session-aware resolver",
            body.contains("PRootKernel.resolvePathForSession(request.sessionId,"),
        )
    }

    @Test
    fun `import never reads a raw host File and never falls back to the global resolver`() {
        val body = declarationSlice(readRepoFile(photos), "private fun handleImport(")
        for (forbidden in listOf("File(rawPath)", "File(path)", "resolveHostPath(", "resolveGuestHostPath(")) {
            assertFalse("bare host access came back in handleImport: $forbidden", body.contains(forbidden))
        }
        // Strongest form of the same claim: this declaration builds NO File at
        // all. That also catches the `?: File(guestPath)` shape, which a
        // list of forbidden spellings would miss.
        assertFalse(
            "handleImport must not construct any File(...) — the resolver returns it",
            Regex("""\bFile\(""").containsMatchIn(body),
        )
        // The old shape was `val path = args.get("path")` + `File(path)`; the
        // new one must keep the raw value and only derive a guest path from it.
        assertTrue(body.contains("val rawPath = args.get(\"path\")"))
    }

    @Test
    fun `import failures are structured and distinguish the two halves`() {
        val body = declarationSlice(readRepoFile(photos), "private fun handleImport(")
        assertTrue(body.contains("\"path_outside_sandbox\""))
        assertTrue(body.contains("\"no_file\""))
        assertTrue("guest path echoed back for self-correction", body.contains("guestPath,"))
    }

    @Test
    fun `import rolls back an inserted row when the write fails`() {
        val body = declarationSlice(readRepoFile(photos), "private fun handleImport(")
        assertTrue("inserted uri must be remembered", body.contains("var insertedUri: Uri? = null"))
        assertTrue("failed write must delete the pending row", body.contains("contentResolver.delete(uri, null, null)"))
    }

    @Test
    fun `export stays a host-side writer (untouched by this fix)`() {
        val body = declarationSlice(readRepoFile(photos), "private fun handleExport(")
        assertTrue(body.contains("filesDir"))
        assertFalse("export must not be dragged into guest path resolution", body.contains("resolvePathForSession("))
    }

    // ── B: intent hardening ───────────────────────────────────────────────

    @Test
    fun `parsed intent URIs are stripped of grants, clipData and same-package targets`() {
        val body = declarationSlice(readRepoFile(open), "Intent.parseUri(url, Intent.URI_INTENT_SCHEME)")
        for (flag in listOf(
            "FLAG_GRANT_READ_URI_PERMISSION",
            "FLAG_GRANT_WRITE_URI_PERMISSION",
            "FLAG_GRANT_PERSISTABLE_URI_PERMISSION",
            "FLAG_GRANT_PREFIX_URI_PERMISSION",
        )) {
            assertTrue("grant flag not stripped: $flag", body.contains(flag))
        }
        assertTrue(body.contains("removeFlags("))
        assertTrue(body.contains("clipData = null"))
        assertTrue(body.contains("component?.packageName == context.packageName) component = null"))
        // No regression of the historical behaviour:
        assertTrue(body.contains("FLAG_ACTIVITY_NEW_TASK"))
        assertTrue(body.contains("selector = null"))
    }

    @Test
    fun `plain ACTION_VIEW path is untouched and intent URIs keep their semantics`() {
        val src = readRepoFile(open)
        val body = declarationSlice(src, "Intent.parseUri(url, Intent.URI_INTENT_SCHEME)")
        assertTrue(body.contains("Intent(Intent.ACTION_VIEW, Uri.parse(url))"))
        assertTrue(src.contains("url.startsWith(\"intent:\")"))
    }

    // ── C: date-range wiring ──────────────────────────────────────────────

    @Test
    fun `date-only --end is aligned at the call site and days walk the calendar`() {
        // [da1032-C ISSUE-04] the range resolution moved into the shared pure
        // core `resolvePhotoRange`, which `resolveDateRange` now delegates to.
        // Every assertion below is unchanged — only the slice is re-anchored on
        // the declaration that OWNS the decision (bind the decision point, not
        // the call site: deep-audit-1031-D / COMMON.md §4 re-anchor rule).
        val src = readRepoFile(photos)
        val core = topLevelSlice(src, "internal fun resolvePhotoRange(")
        assertTrue("date-only end must be detected", core.contains("isDateOnly("))
        assertTrue("end must be aligned to end-of-day", core.contains("alignDateOnlyEndMs("))
        assertTrue("--days must walk the calendar", core.contains("daysAgoStartOfDayMs("))
        assertFalse("24h constant arithmetic came back", core.contains("24L * 60 * 60 * 1000"))
        assertFalse("24h constant arithmetic came back", core.contains("24 * 60 * 60 * 1000"))
        // …and the handler must still REACH that core from its own member:
        assertTrue(
            "the handler must still resolve the window through the shared core",
            declarationSlice(src, "private fun resolveDateRange(").contains("resolvePhotoRange("),
        )
    }

    // ── helpers ───────────────────────────────────────────────────────────

    private fun repoRoot(): File {
        System.getProperty("probe.repo")?.let { return File(it) }
        var dir: File? = File(System.getProperty("user.dir"))
        while (dir != null) {
            if (File(dir, photos).exists()) return dir
            dir = dir.parentFile
        }
        throw FileNotFoundException("repo root not found from user.dir=${System.getProperty("user.dir")}")
    }

    private fun readRepoFile(relative: String): String {
        val f = File(repoRoot(), relative)
        if (!f.exists()) throw FileNotFoundException(f.path)
        return f.readText()
    }

    /** Source lines of the declaration at [marker] up to the next member. */
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

    /**
     * [da1032-C] Source lines of the TOP-LEVEL declaration at [marker], up to
     * the next top-level one (or EOF). `declarationSlice` above only knows how
     * to cut members of a class — the range decision now lives in a top-level
     * `internal fun`, which sits at column 0 and would otherwise swallow the
     * rest of the file into the slice.
     */
    private fun topLevelSlice(src: String, marker: String): String {
        val lines = src.lines()
        val start = lines.indexOfFirst { it.contains(marker) }
        assertTrue("marker not found: $marker", start >= 0)
        // Only a real declaration keyword ends the slice. A bare `): Foo {`
        // (the closing paren of a multi-line signature) sits at column 0 too
        // and must NOT be mistaken for the next top-level declaration —
        // measured: that mistake truncated this slice to 7 lines of signature.
        val end = (start + 1 until lines.size).firstOrNull { i ->
            val t = lines[i]
            t.isNotBlank() && !t.startsWith(" ") && !t.startsWith("\t") && !t.startsWith("}") &&
                (t.startsWith("internal ") || t.startsWith("private ") ||
                    t.startsWith("public ") || t.startsWith("fun ") ||
                    t.startsWith("class ") || t.startsWith("object ") ||
                    t.startsWith("sealed ") || t.startsWith("data class ") ||
                    t.startsWith("enum ") || t.startsWith("@") ||
                    t.startsWith("val ") || t.startsWith("const ") || t.startsWith("//"))
        } ?: lines.size
        return lines.subList(start, end).joinToString("\n")
    }
}

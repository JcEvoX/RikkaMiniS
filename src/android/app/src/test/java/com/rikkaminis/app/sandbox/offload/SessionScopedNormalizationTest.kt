package com.rikkaminis.app.sandbox.offload

import java.io.File
import java.util.concurrent.atomic.AtomicInteger
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import com.rikkaminis.app.sandbox.hasDotDotSegment

/**
 * Session-scoped write classification for malformed guest paths
 * ([deep-audit-1016 FIX-C 2a]).
 *
 * `ModelUseOffloadHandler.classifySessionPath` is the third copy of the
 * "is this session-scoped?" decision (after `PRootKernel.resolveSessionHostPath`
 * and `resolvePathForSession`) and it matched the RAW path. `--output
 * /var/minis/./workspace/out.txt` therefore failed the scope regex, fell
 * through to `NotSessionScoped`, and the global resolver wrote the file into
 * the rootfs placeholder — the session's own workspace stayed empty and every
 * other session saw the same ghost file.
 *
 * [SessionScopedPathGuardTest] owns the security decision (refusal is final,
 * never re-asked of the global resolver); this class owns the normalization
 * cursor plus the `..` rule that keeps that decision intact.
 */
class SessionScopedNormalizationTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private val filesDir: File get() = File(tmp.root, "files")
    private val sessionId = "s1"

    private fun sessionFile(subdir: String, tail: String) =
        File(filesDir, "minis-sessions/$sessionId/$subdir/$tail")

    private fun classify(linuxPath: String, sid: String? = sessionId) =
        ModelUseOffloadHandler.classifySessionPath(filesDir, sid, linuxPath)

    private fun resolved(linuxPath: String, sid: String? = sessionId): File? =
        (classify(linuxPath, sid) as? SessionScopedPathDecision.Resolved)?.file

    // ── the fix ─────────────────────────────────────────────────────────

    @Test
    fun `dot-noise and slash-noise paths classify as this session's file`() {
        filesDir.mkdirs()
        // ★ pre-fix all four were NotSessionScoped → rootfs ghost.
        assertEquals(
            sessionFile("workspace", "out.txt").absolutePath,
            resolved("/var/minis/./workspace/out.txt")?.absolutePath,
        )
        assertEquals(
            sessionFile("workspace", "out.txt").absolutePath,
            resolved("//var/minis/workspace/out.txt")?.absolutePath,
        )
        assertEquals(
            sessionFile("offloads", "x.bin").absolutePath,
            resolved("/var/minis//offloads/x.bin")?.absolutePath,
        )
        assertEquals(
            sessionFile("attachments", "a.png").absolutePath,
            resolved("/var/minis/attachments/a.png")?.absolutePath,
        )
        // Trailing slash on the bare scope still names the scope dir.
        assertEquals(
            File(filesDir, "minis-sessions/$sessionId/workspace").absolutePath,
            resolved("/var/minis/workspace/")?.absolutePath,
        )
    }

    @Test
    fun `a malformed session path never reaches the global resolver`() {
        filesDir.mkdirs()
        val globalCalls = AtomicInteger()
        val file = ModelUseOffloadHandler.sessionAwareHostFile(
            filesDir, sessionId, "/var/minis/./workspace/out.txt",
        ) { globalCalls.incrementAndGet(); File("/global/$it") }
        assertEquals(sessionFile("workspace", "out.txt").absolutePath, file?.absolutePath)
        assertEquals("the global resolver must not be a second opinion", 0, globalCalls.get())
    }

    // ── the `..` rule that protects the refusal decision ─────────────────

    @Test
    fun `a literal dotdot segment is refused before normalization`() {
        filesDir.mkdirs()
        // Before normalization, so no path is silently re-targeted:
        // `workspace/../browser/c` is an escape attempt, not a request for
        // `browser/c`.
        assertEquals(SessionScopedPathDecision.Rejected, classify("/var/minis/workspace/../browser/c"))
        assertEquals(SessionScopedPathDecision.Rejected, classify("/var/minis/./workspace/../workspace/out.txt"))
        assertEquals(SessionScopedPathDecision.Rejected, classify("/var/minis/workspace/.."))
        assertEquals(SessionScopedPathDecision.Rejected, classify("/var/minis/workspace/../../shared_prefs/x.xml"))
        // And with the refusal being final: no global fallback.
        val globalCalls = AtomicInteger()
        assertNull(
            ModelUseOffloadHandler.sessionAwareHostFile(
                filesDir, sessionId, "/var/minis/workspace/../../shared_prefs/x.xml",
            ) { globalCalls.incrementAndGet(); File("/global/$it") },
        )
        assertEquals(0, globalCalls.get())
    }

    @Test
    fun `dotdot detection is segment-wise not substring-wise`() {
        assertFalse("a..b is an ordinary file name", hasDotDotSegment("/var/minis/workspace/a..b/c.txt"))
        assertFalse(hasDotDotSegment("/var/minis/workspace/a.md"))
        assertFalse(hasDotDotSegment(""))
        assertFalse(hasDotDotSegment("..."))
        assertTrue(hasDotDotSegment(".."))
        assertTrue(hasDotDotSegment("/var/minis/workspace/../x"))
        assertFalse("guest paths use '/', a backslash is not a separator", hasDotDotSegment("..\\.."))
    }

    @Test
    fun `no session and non-scoped paths keep their old routing`() {
        filesDir.mkdirs()
        // No session → the global resolver decides (unchanged).
        assertEquals(
            SessionScopedPathDecision.NotSessionScoped,
            classify("/var/minis/./workspace/out.txt", sid = null),
        )
        // Outside the four scopes → the global resolver decides (unchanged).
        assertEquals(SessionScopedPathDecision.NotSessionScoped, classify("/var/minis/memory/GLOBAL.md"))
        assertEquals(SessionScopedPathDecision.NotSessionScoped, classify("/var/minis/shared/x.md"))
        assertEquals(SessionScopedPathDecision.NotSessionScoped, classify("/tmp/out.json"))
        // Empty path: refused rather than normalized into a scope.
        assertEquals(SessionScopedPathDecision.Rejected, classify(""))
    }
}

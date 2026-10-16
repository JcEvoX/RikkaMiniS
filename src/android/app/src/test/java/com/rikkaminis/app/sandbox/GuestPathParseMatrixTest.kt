package com.rikkaminis.app.sandbox

import java.io.File
import java.nio.file.Files as NioFiles
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * The guest → host parse matrix ([deep-audit-1016 FIX-C 2a]).
 *
 * `PRootKernel.resolveSessionHostPath` / `resolveHostPath` are Android-coupled
 * (Context / rootfsManager), so the decisions live in the pure
 * [resolveSessionGuestHostPath] / [resolveGuestHostPath] and these tests drive
 * them against a real temporary tree — including a symlink that leaves the
 * session tree, because containment is a filesystem fact (`canonicalPath`), not
 * a string fact.
 *
 * ## Why the matrix is the deliverable
 *
 * The fix normalizes the guest path *before* every prefix match. That changes
 * which branch a malformed path takes, and the only honest way to bound the
 * blast radius is to pin all three shapes — canonical, malformed, escaping —
 * side by side. `probe/parse-matrix-{pre,post}.txt` holds the same rows
 * produced by the BASELINE bodies, so the diff between the two files is the
 * behaviour change; the expectations here are hand-computed literals, not
 * values read back out of the implementation.
 *
 * Rows that deliberately changed (documented in the FIX-C report):
 *  - R3/R4/R5  `.`- and `//`-noise paths now reach the session tree — pre-fix
 *              they fell through to the rootfs [ghost]; the session kept an
 *              empty workspace while the file sat under `<rootfs>/var/minis/…`.
 *  - R10       `/var/minis/./mounts/ro/out.txt` now resolves to the REAL mount
 *              (pre-fix: rootfs ghost). The read-only guard already normalized,
 *              so pre-fix the two disagreed about which file the path named.
 *  - R11       a path whose normalization leaves `/var/minis` now resolves
 *              (pre-fix: refused) — POSIX semantics, and the resolution stays
 *              inside the sandbox (rootfs / bind mount), never a host path.
 *  - R15       `""` now returns null (pre-fix: the rootfs directory itself).
 *  - R19       `..` inside a session-shaped path: the session resolver answers
 *              with THIS session's file; only the no-session global scan still
 *              picks another session's existing file (its documented
 *              "ambiguity is acceptable" fallback — bytes are identical
 *              pre/post, see the matrix diff).
 */
class GuestPathParseMatrixTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private val sessionId = "sessA"

    private val filesDir: File get() = File(tmp.root, "files")
    private val sessionsRoot: File get() = File(filesDir, "minis-sessions")
    private val sessionA: File get() = File(sessionsRoot, "sessA")
    private val sessionB: File get() = File(sessionsRoot, "sessB")
    private val rootfs: File get() = File(tmp.root, "rootfs")

    /** The read-only external mount FIX-C 2b guards against. */
    private val readOnlyMount: File get() = File(tmp.root, "mounts/ro")

    private val perSessionSubdirs = setOf("attachments", "offloads", "workspace", "browser")

    private val bindMounts: Map<String, String>
        get() = linkedMapOf(
            "/var/minis/memory" to File(filesDir, "minis-global/memory").absolutePath,
            "/var/minis/skills" to File(filesDir, "minis-global/skills").absolutePath,
            "/var/minis/shared" to File(filesDir, "minis-global/shared").absolutePath,
            "/var/minis/logs" to File(filesDir, "logs").absolutePath,
            "/var/minis/mounts/ro" to readOnlyMount.absolutePath,
        )

    private fun fixture() {
        for (d in listOf(
            sessionA, sessionB,
            File(sessionA, "workspace"), File(sessionB, "workspace"),
            File(sessionA, "attachments"),
            readOnlyMount,
            // The pre-fix landing spot for malformed paths: the rootfs ghost.
            File(rootfs, "var/minis/workspace"),
        )) {
            d.mkdirs()
            assertTrue("fixture dir $d", d.isDirectory)
        }
        // Only session B owns this name — the cross-session bait for R19.
        File(sessionB, "workspace/secret.txt").writeText("session B")
        // A symlink that leaves the session tree (canonical containment must
        // still refuse it).
        val outside = File(tmp.root, "outside").also { it.mkdirs() }
        NioFiles.createSymbolicLink(
            File(sessionA, "workspace/escape").toPath(),
            outside.toPath(),
        )
    }

    // ── the two functions under test ────────────────────────────────────

    /** Production wiring of `PRootKernel.resolveSessionHostPath`. */
    private fun session(linuxPath: String): File? = resolveSessionGuestHostPath(
        sessionId = sessionId,
        linuxPath = linuxPath,
        filesDir = filesDir,
        perSessionSubdirs = perSessionSubdirs,
        resolveGlobal = { global(it) },
    )

    /** Production wiring of `PRootKernel.resolveHostPath`. */
    private fun global(linuxPath: String): File? = resolveGuestHostPath(
        linuxPath = linuxPath,
        bindMounts = bindMounts,
        perSessionSubdirs = perSessionSubdirs,
        sessionsRoot = sessionsRoot,
        rootfsDir = rootfs,
    )

    private fun path(f: File?) = f?.absolutePath

    // ── R1-R6: session scope, canonical vs malformed ────────────────────

    @Test
    fun `R1 canonical workspace file resolves per session, rootfs ghost globally`() {
        fixture()
        assertEquals("${sessionA.absolutePath}/workspace/report.md", path(session("/var/minis/workspace/report.md")))
        assertEquals("${rootfs.absolutePath}/var/minis/workspace/report.md", path(global("/var/minis/workspace/report.md")))
    }

    @Test
    fun `R2 bare scope path resolves to the session dir`() {
        fixture()
        assertEquals(sessionA.absolutePath + "/workspace", path(session("/var/minis/workspace")))
        val scanned = global("/var/minis/workspace")
        assertTrue(
            "no-session scan picks an existing scope dir (ambiguity is documented): $scanned",
            path(scanned) in listOf(
                File(sessionA, "workspace").absolutePath,
                File(sessionB, "workspace").absolutePath,
            ),
        )
    }

    @Test
    fun `R3 dot-noise path reaches the session tree - the reported ghost`() {
        fixture()
        // ★ the fix: pre-fix this returned <rootfs>/var/minis/workspace/report.md
        assertEquals(
            "${sessionA.absolutePath}/workspace/report.md",
            path(session("/var/minis/./workspace/report.md")),
        )
        // The global function's answer is unchanged for this row: safeResolveWithin
        // normalized the tail anyway, and both spellings name the same ghost.
        assertEquals(
            "${rootfs.absolutePath}/var/minis/workspace/report.md",
            path(global("/var/minis/./workspace/report.md")),
        )
    }

    @Test
    fun `R4 double-slash path reaches the session tree - the reported ghost`() {
        fixture()
        // ★ the fix: pre-fix the raw `startsWith("/var/minis/")` test failed
        assertEquals(
            "${sessionA.absolutePath}/workspace/report.md",
            path(session("//var/minis/workspace/report.md")),
        )
        assertEquals(
            "${rootfs.absolutePath}/var/minis/workspace/report.md",
            path(global("//var/minis/workspace/report.md")),
        )
    }

    @Test
    fun `R5 trailing slash is the bare scope path`() {
        fixture()
        assertEquals(sessionA.absolutePath + "/workspace", path(session("/var/minis/workspace/")))
    }

    @Test
    fun `R6 bare attachments scope resolves to this session's dir`() {
        fixture()
        assertEquals(sessionA.absolutePath + "/attachments", path(session("/var/minis/attachments")))
        assertEquals(File(sessionA, "attachments").absolutePath, path(global("/var/minis/attachments")))
    }

    @Test
    fun `R20 repeated inner slashes are collapsed`() {
        fixture()
        assertEquals(
            "${sessionA.absolutePath}/workspace/report.md",
            path(session("/var/minis/workspace//report.md")),
        )
    }

    // ── R7-R10: shared scopes and external mounts (the 2b subject) ──────

    @Test
    fun `R7 shared subdir is session-independent on both arms`() {
        fixture()
        val expected = File(filesDir, "minis-global/shared/notes/a.md").absolutePath
        assertEquals(expected, path(session("/var/minis/shared/notes/a.md")))
        assertEquals(expected, path(global("/var/minis/shared/notes/a.md")))
    }

    @Test
    fun `R8 memory subdir resolves through its bind mount`() {
        fixture()
        val expected = File(filesDir, "minis-global/memory/GLOBAL.md").absolutePath
        assertEquals(expected, path(session("/var/minis/memory/GLOBAL.md")))
        assertEquals(expected, path(global("/var/minis/memory/GLOBAL.md")))
    }

    @Test
    fun `R9 read-only mount resolves to the real mount (guard sees the same file)`() {
        fixture()
        val expected = File(readOnlyMount, "out.txt").absolutePath
        assertEquals(expected, path(session("/var/minis/mounts/ro/out.txt")))
        assertEquals(expected, path(global("/var/minis/mounts/ro/out.txt")))
    }

    @Test
    fun `R10 dot-noise mount path resolves to the real mount, not a rootfs ghost`() {
        fixture()
        // ★ the fix: pre-fix the resolver produced
        // <rootfs>/var/minis/mounts/ro/out.txt while isLinuxPathUnderReadOnlyMount
        // (which normalizes) reported the path as read-only — the guard and the
        // resolver disagreed about which file the path named.
        val expected = File(readOnlyMount, "out.txt").absolutePath
        assertEquals(expected, path(session("/var/minis/./mounts/ro/out.txt")))
        assertEquals(expected, path(global("/var/minis/./mounts/ro/out.txt")))
        assertEquals(expected, path(global("//var/minis/mounts/ro/out.txt")))
    }

    // ── R11-R17: escaping / degenerate input ────────────────────────────

    @Test
    fun `R11 a path that normalizes out of var-minis resolves inside the sandbox`() {
        fixture()
        // Deliberate change: pre-fix the session arm refused this (the tail
        // climbed out of the session tree). Normalized it is `/var/shared/x.md`,
        // which is the guest's own rootfs — the same file the shell would write.
        val expected = "${rootfs.absolutePath}/var/shared/x.md"
        assertEquals(expected, path(session("/var/minis/./workspace/../../shared/x.md")))
        assertEquals(expected, path(global("/var/minis/./workspace/../../shared/x.md")))
    }

    @Test
    fun `R12 absolute non-minis path falls back to the rootfs`() {
        fixture()
        val expected = "${rootfs.absolutePath}/etc/resolv.conf"
        assertEquals(expected, path(session("/etc/resolv.conf")))
        assertEquals(expected, path(global("/etc/resolv.conf")))
    }

    @Test
    fun `R13 var-minis without the trailing slash is not a scope - unchanged`() {
        fixture()
        // Pinned because the fix's normalizer turns `/var/minis/` into
        // `/var/minis`: both spellings must keep landing in the rootfs.
        val expected = "${rootfs.absolutePath}/var/minis"
        assertEquals(expected, path(session("/var/minis")))
        assertEquals(expected, path(global("/var/minis")))
    }

    @Test
    fun `R14 var-minis with the trailing slash collapses to R13 - unchanged`() {
        fixture()
        val expected = "${rootfs.absolutePath}/var/minis"
        assertEquals(expected, path(session("/var/minis/")))
        assertEquals(expected, path(global("/var/minis/")))
    }

    @Test
    fun `R15 empty path is refused instead of naming the rootfs`() {
        fixture()
        // Deliberate change: pre-fix `""` resolved to the rootfs DIR itself.
        assertNull(session(""))
        assertNull(global(""))
    }

    @Test
    fun `R16 relative path keeps resolving under the rootfs`() {
        fixture()
        val expected = "${rootfs.absolutePath}/relative/path.md"
        assertEquals(expected, path(session("relative/path.md")))
        assertEquals(expected, path(global("relative/path.md")))
    }

    @Test
    fun `R17 relative path climbing above root is refused`() {
        fixture()
        assertNull(session("../../etc/passwd"))
        assertNull(global("../../etc/passwd"))
    }

    // ── R18-R19: symlink escape and cross-session bait ──────────────────

    @Test
    fun `R18 symlink leaving the session tree is refused by the session arm`() {
        fixture()
        assertNull(session("/var/minis/workspace/escape/x.md"))
        // The no-session arm has no session tree to escape: it names the rootfs
        // placeholder (unchanged pre/post).
        assertEquals(
            "${rootfs.absolutePath}/var/minis/workspace/escape/x.md",
            path(global("/var/minis/workspace/escape/x.md")),
        )
    }

    @Test
    fun `R19 dotdot inside a session path answers with THIS session's file`() {
        fixture()
        // ★ the session arm must never answer with session B's `secret.txt`,
        // even though B owns that name and the global scan would find it.
        assertEquals(
            "${sessionA.absolutePath}/workspace/secret.txt",
            path(session("/var/minis/workspace/../workspace/secret.txt")),
        )
        // Control: the no-session arm's documented scan DOES find B's file
        // (identical behaviour pre-fix — see the matrix diff), which is exactly
        // why session-aware callers must use the session arm.
        assertEquals(
            "${sessionB.absolutePath}/workspace/secret.txt",
            path(global("/var/minis/workspace/../workspace/secret.txt")),
        )
        assertTrue(File(sessionB, "workspace/secret.txt").exists())
    }
}

package com.rikkaminis.app.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * JVM round-trip + negative-control tests for [GuestPathMapper]
 * ([T-minis-fastio]).
 *
 * The fixture below is not invented: it is the `-b` list read off a live
 * session shell with `ps -ef | grep proot` on the device (2026-09-30), so the
 * assertions run against the table proot was actually launched with — the same
 * table `minis-fastio` has to rebuild from [PRootKernel.bindMounts] +
 * [GuestPathMapper.sessionBindings].
 *
 * Every expectation is a literal. Nothing is derived from a production
 * constant, so a changed constant shows up as a red test instead of a silently
 * adjusted expectation.
 */
class GuestPathMapperTest {

    private val filesDir = "/data/user/0/com.rikkaminis.app/files"
    private val sessionId = "371e1802-0254-4ca1-9b90-0bb4cdc74656"
    private val sessionBase = "$filesDir/minis-sessions/$sessionId"
    private val rootfs = "$filesDir/alpine-rootfs"

    /** Global half of the captured `-b` list (see class doc). */
    private val globalBindings = mapOf(
        "/var/minis/memory" to "$filesDir/minis-global/memory",
        "/var/minis/skills" to "$filesDir/minis-global/skills",
        "/var/minis/shared" to "$filesDir/minis-global/shared",
        "/var/minis/mcp-servers" to "$filesDir/minis-global/mcp-servers",
        "/var/minis/logs" to "$filesDir/logs",
        "/var/minis/mounts/笔记" to "/storage/emulated/0/Documents/笔记",
        "/var/minis/mounts/万维钢·现代思维工具100讲" to "/storage/emulated/0/专栏/万维钢·现代思维工具100讲",
    )

    private fun bindings(sid: String? = sessionId): List<GuestPathMapper.Binding> =
        GuestPathMapper.sessionBindings(filesDir, sid, globalBindings, rootfs)

    private fun resolve(path: String, cwd: String = "/root", sid: String? = sessionId) =
        GuestPathMapper.resolve(path, cwd, bindings(sid))

    private fun host(path: String, cwd: String = "/root"): String? =
        (resolve(path, cwd) as? GuestPathMapper.Resolution.Ok)?.hostPath

    private fun refusal(path: String, cwd: String = "/root"): GuestPathMapper.Refusal? =
        (resolve(path, cwd) as? GuestPathMapper.Resolution.Denied)?.reason

    // ── the table itself ────────────────────────────────────────────────────

    @Test
    fun `rebuilt table equals the proot -b list captured on device`() {
        val expected = globalBindings + mapOf(
            "/var/minis/attachments" to "$sessionBase/attachments",
            "/var/minis/offloads" to "$sessionBase/offloads",
            "/var/minis/workspace" to "$sessionBase/workspace",
            "/var/minis/browser" to "$sessionBase/browser",
            "/" to rootfs,
        )
        assertEquals(expected, bindings().associate { it.guestPrefix to it.hostBase })
    }

    @Test
    fun `bindings are ordered longest guest prefix first`() {
        val lengths = bindings().map { it.guestPrefix.length }
        assertEquals(lengths.sortedDescending(), lengths)
    }

    // ── round trip: guest path in → host path out, character for character ──

    @Test
    fun `session subdirs resolve to this session's own host dir`() {
        assertEquals("$sessionBase/workspace/out/report.md", host("/var/minis/workspace/out/report.md"))
        assertEquals("$sessionBase/attachments/a.png", host("/var/minis/attachments/a.png"))
        assertEquals("$sessionBase/offloads/big.jsonl", host("/var/minis/offloads/big.jsonl"))
        assertEquals("$sessionBase/browser/page.html", host("/var/minis/browser/page.html"))
    }

    @Test
    fun `global subdirs resolve to the shared pool`() {
        assertEquals("$filesDir/minis-global/shared/work/r.md", host("/var/minis/shared/work/r.md"))
        assertEquals("$filesDir/minis-global/memory/GLOBAL.md", host("/var/minis/memory/GLOBAL.md"))
        assertEquals("$filesDir/minis-global/skills/github-ops/SKILL.md", host("/var/minis/skills/github-ops/SKILL.md"))
        assertEquals("$filesDir/logs/minis-2026-09-30.log", host("/var/minis/logs/minis-2026-09-30.log"))
    }

    @Test
    fun `user mounts win over the enclosing minis prefix, non-ascii included`() {
        assertEquals("/storage/emulated/0/Documents/笔记/a.md", host("/var/minis/mounts/笔记/a.md"))
        assertEquals(
            "/storage/emulated/0/专栏/万维钢·现代思维工具100讲/01.md",
            host("/var/minis/mounts/万维钢·现代思维工具100讲/01.md"),
        )
    }

    @Test
    fun `everything else falls through to the rootfs`() {
        assertEquals("$rootfs/etc/hosts", host("/etc/hosts"))
        assertEquals("$rootfs/tmp/scratch/a", host("/tmp/scratch/a"))
        assertEquals("$rootfs/root/.bashrc", host("/root/.bashrc"))
        // /var/minis/mounts itself is the rootfs placeholder dir, not a mount.
        assertEquals("$rootfs/var/minis/mounts", host("/var/minis/mounts"))
    }

    @Test
    fun `relative paths are joined against the guest cwd`() {
        assertEquals("$sessionBase/workspace/a/b.txt", host("a/b.txt", cwd = "/var/minis/workspace"))
        assertEquals("$sessionBase/workspace/b.txt", host("./a/../b.txt", cwd = "/var/minis/workspace"))
        assertEquals("$rootfs/tmp/x", host("../x", cwd = "/tmp/scratch"))
    }

    // ── negative controls: these must be refused, not guessed ───────────────

    @Test
    fun `dot-dot above the root is refused`() {
        assertEquals(GuestPathMapper.Refusal.CLIMBS_ABOVE_ROOT, refusal("../../etc/passwd", cwd = "/"))
        assertEquals(GuestPathMapper.Refusal.CLIMBS_ABOVE_ROOT, refusal("../x", cwd = "/"))
        // …but one that resolves back inside the guest is a normal path.
        assertEquals("$rootfs/b/a", host("../a", cwd = "/b/c"))
    }

    @Test
    fun `host pseudo filesystems are refused`() {
        assertEquals(GuestPathMapper.Refusal.PSEUDO_FS, refusal("/proc"))
        assertEquals(GuestPathMapper.Refusal.PSEUDO_FS, refusal("/proc/self/environ"))
        assertEquals(GuestPathMapper.Refusal.PSEUDO_FS, refusal("/dev/null"))
        assertEquals(GuestPathMapper.Refusal.PSEUDO_FS, refusal("/sys/class"))
        // A prefix that merely starts with the same letters is NOT pseudo-fs.
        assertEquals("$rootfs/devices/x", host("/devices/x"))
    }

    @Test
    fun `a symlink out of the binding base is refused, one that stays inside is not`() {
        val tmp = Files.createTempDirectory("fastio-mapper").toFile()
        try {
            val base = File(tmp, "base").apply { mkdirs() }
            val outside = File(tmp, "outside").apply { mkdirs() }
            File(outside, "secret.txt").writeText("x")
            File(base, "inside").apply { mkdirs() }
            File(base, "inside/ok.txt").writeText("y")
            Files.createSymbolicLink(File(base, "escape").toPath(), outside.toPath())
            Files.createSymbolicLink(File(base, "inward").toPath(), File(base, "inside").toPath())

            val table = listOf(GuestPathMapper.Binding("/data", base.absolutePath))

            // positive control: plain path inside the base
            val plain = GuestPathMapper.resolve("/data/ok.txt", "/", table) as GuestPathMapper.Resolution.Ok
            assertEquals(File(base, "ok.txt").canonicalPath, plain.hostPath)

            // positive control: symlink that stays inside the base
            val inward = GuestPathMapper.resolve("/data/inward/ok.txt", "/", table) as GuestPathMapper.Resolution.Ok
            assertEquals(File(base, "inside/ok.txt").canonicalPath, inward.hostPath)

            // negative: symlink that leaves the base
            val escape = GuestPathMapper.resolve("/data/escape/secret.txt", "/", table)
            assertEquals(
                GuestPathMapper.Refusal.HOST_ESCAPE,
                (escape as GuestPathMapper.Resolution.Denied).reason,
            )
        } finally {
            tmp.deleteRecursively()
        }
    }

    // ── binding roots (the destructive caller's extra policy) ───────────────

    @Test
    fun `bind-mount roots are flagged`() {
        val root = resolve("/") as GuestPathMapper.Resolution.Ok
        assertTrue(root.isBindingRoot)
        assertEquals(rootfs, root.hostPath)

        val workspace = resolve("/var/minis/workspace") as GuestPathMapper.Resolution.Ok
        assertTrue(workspace.isBindingRoot)

        val mount = resolve("/var/minis/mounts/笔记") as GuestPathMapper.Resolution.Ok
        assertTrue(mount.isBindingRoot)
        assertTrue(mount.binding.isExternalMount)

        // A path one level down is not a root, and a normal dir is not external.
        val file = resolve("/var/minis/workspace/x.txt") as GuestPathMapper.Resolution.Ok
        assertFalse(file.isBindingRoot)
        assertFalse(file.binding.isExternalMount)
    }

    // ── session scoping ─────────────────────────────────────────────────────

    @Test
    fun `a session-scoped subdir wins over a global entry with the same prefix`() {
        val shadowed = globalBindings + ("/var/minis/workspace" to "$filesDir/minis-global/workspace")
        val table = GuestPathMapper.sessionBindings(filesDir, sessionId, shadowed, rootfs)
        val resolved = GuestPathMapper.resolve("/var/minis/workspace/x", "/", table)
        assertEquals("$sessionBase/workspace/x", (resolved as GuestPathMapper.Resolution.Ok).hostPath)
    }

    @Test
    fun `without a session id the session subdirs fall back to the rootfs placeholder`() {
        val table = bindings(null)
        assertFalse(table.any { it.guestPrefix == "/var/minis/workspace" })
        val workspace = GuestPathMapper.resolve("/var/minis/workspace", "/", table)
        assertEquals("$rootfs/var/minis/workspace", (workspace as GuestPathMapper.Resolution.Ok).hostPath)
        // globals are unaffected
        val shared = GuestPathMapper.resolve("/var/minis/shared/a", "/", table)
        assertEquals("$filesDir/minis-global/shared/a", (shared as GuestPathMapper.Resolution.Ok).hostPath)
    }

    @Test
    fun `a session id cannot move the session base out of minis-sessions`() {
        for (evil in listOf("../evil", "a/b", "..", ".")) {
            val table = GuestPathMapper.sessionBindings(filesDir, evil, globalBindings, rootfs)
            assertFalse(
                "session id '$evil' must not produce a session binding",
                table.any { it.guestPrefix == "/var/minis/workspace" },
            )
        }
    }

    // ── pure normalization ──────────────────────────────────────────────────

    @Test
    fun `absolute guest path normalization`() {
        assertEquals("/", GuestPathMapper.absoluteGuestPath("/root", "/"))
        assertEquals("/a/b", GuestPathMapper.absoluteGuestPath("/", "a/b"))
        assertEquals("/a", GuestPathMapper.absoluteGuestPath("/a/b", ".."))
        assertEquals("/a/b", GuestPathMapper.absoluteGuestPath("/a", "b"))
        assertEquals("/a/b", GuestPathMapper.absoluteGuestPath("/", "/a//b/."))
        assertNull(GuestPathMapper.absoluteGuestPath("/", "../x"))
        assertNull(GuestPathMapper.absoluteGuestPath("/", ".."))
    }

    @Test
    fun `contained host path never leaves its base`() {
        assertEquals("/base", GuestPathMapper.containedHostPath("/base", ""))
        assertNull(GuestPathMapper.containedHostPath("/base", "/etc/passwd"))
    }
}

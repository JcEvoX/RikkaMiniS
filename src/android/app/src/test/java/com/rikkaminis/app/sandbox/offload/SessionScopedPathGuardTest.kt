package com.rikkaminis.app.sandbox.offload

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * JVM round-trip + negative-control tests for
 * [ModelUseOffloadHandler.sessionScopedJoin] ([fix/sweep1013-security R1]).
 *
 * The attack samples below are the python traversal simulations from the
 * sweep-1013 D2 report, not invented inputs:
 *   - `/var/minis/workspace/../../../debug_server_token`
 *     → `/data/data/com.rikkaminis.app/files/debug_server_token` (pre-fix)
 *   - `/var/minis/workspace/../../shared_prefs/offload_permissions.xml`
 *     → `/data/data/com.rikkaminis.app/shared_prefs/offload_permissions.xml`
 *     (pre-fix — a permission self-escalation write)
 *
 * Every positive expectation is a literal path; nothing is derived from a
 * production constant, so a changed constant shows up as a red test instead
 * of a silently adjusted expectation.
 */
class SessionScopedPathGuardTest {

    @get:Rule
    val tmp = TemporaryFolder()

    // Idempotent: safeResolveWithin only calls canonicalPath (no disk writes),
    // so a shared `files` dir is fine; mkdirs() no-ops on the second call.
    private val filesDir: File
        get() = File(tmp.root, "files").apply { mkdirs() }
    private val sessionId = "371e1802-0254-4ca1-9b90-0bb4cdc74656"

    private fun sessionTree(): String =
        "${filesDir.absolutePath}/minis-sessions/$sessionId"

    // ── positive: legitimate joins still resolve ────────────────────────

    @Test
    fun `plain workspace file resolves inside session tree`() {
        val resolved = ModelUseOffloadHandler.sessionScopedJoin(
            filesDir, sessionId, "/var/minis/workspace/report.md",
        )
        assertEquals("${sessionTree()}/workspace/report.md", resolved?.absolutePath)
    }

    @Test
    fun `bare scope path resolves to the scope dir`() {
        val resolved = ModelUseOffloadHandler.sessionScopedJoin(
            filesDir, sessionId, "/var/minis/attachments",
        )
        assertEquals("${sessionTree()}/attachments", resolved?.absolutePath)
    }

    @Test
    fun `nested offloads path resolves inside session tree`() {
        val resolved = ModelUseOffloadHandler.sessionScopedJoin(
            filesDir, sessionId, "/var/minis/offloads/model-use/a.bin",
        )
        assertEquals("${sessionTree()}/offloads/model-use/a.bin", resolved?.absolutePath)
    }

    // ── negative: the two report attack samples are rejected ────────────

    @Test
    fun `traversal into files dir root is rejected`() {
        // Pre-fix: File(base, "../../../debug_server_token") → files/debug_server_token
        assertNull(
            ModelUseOffloadHandler.sessionScopedJoin(
                filesDir, sessionId, "/var/minis/workspace/../../../debug_server_token",
            ),
        )
    }

    @Test
    fun `traversal into shared_prefs is rejected`() {
        // Pre-fix: permission self-escalation write target.
        assertNull(
            ModelUseOffloadHandler.sessionScopedJoin(
                filesDir, sessionId, "/var/minis/workspace/../../shared_prefs/offload_permissions.xml",
            ),
        )
    }

    @Test
    fun `traversal into sibling scope is rejected`() {
        assertNull(
            ModelUseOffloadHandler.sessionScopedJoin(
                filesDir, sessionId, "/var/minis/workspace/../browser/cookies",
            ),
        )
    }

    @Test
    fun `dotdot alone is rejected`() {
        assertNull(
            ModelUseOffloadHandler.sessionScopedJoin(
                filesDir, sessionId, "/var/minis/workspace/..",
            ),
        )
    }

    // ── negative: non session-scoped paths yield null (unchanged) ───────

    @Test
    fun `global scope path yields null`() {
        assertNull(
            ModelUseOffloadHandler.sessionScopedJoin(
                filesDir, sessionId, "/var/minis/shared/notes/a.md",
            ),
        )
    }

    @Test
    fun `host absolute path yields null`() {
        assertNull(
            ModelUseOffloadHandler.sessionScopedJoin(
                filesDir, sessionId, "/data/data/com.rikkaminis.app/files/debug_server_token",
            ),
        )
    }

    @Test
    fun `relative path yields null`() {
        assertNull(
            ModelUseOffloadHandler.sessionScopedJoin(filesDir, sessionId, "workspace/a.md"),
        )
    }

    // ── end-to-end: resolved join never escapes the session tree ────────

    @Test
    fun `any accepted join lands under the session tree`() {
        val samples = listOf(
            "/var/minis/workspace/report.md",
            "/var/minis/attachments/img.png",
            "/var/minis/offloads/x/y/z.txt",
            "/var/minis/browser/tabs/a.html",
        )
        val prefix = "${sessionTree()}/"
        for (linuxPath in samples) {
            val resolved = ModelUseOffloadHandler.sessionScopedJoin(filesDir, sessionId, linuxPath)
            assertTrue(
                "resolved path for $linuxPath escaped the session tree: $resolved",
                resolved?.absolutePath?.startsWith(prefix) == true,
            )
        }
    }

    // ── [fix/audit3-1015 F-08] the three-way classification ─────────────

    private fun classify(path: String, sid: String? = sessionId) =
        ModelUseOffloadHandler.classifySessionPath(filesDir, sid, path)

    @Test
    fun `legitimate scope paths classify as Resolved`() {
        val cases = mapOf(
            "/var/minis/workspace/report.md" to "${sessionTree()}/workspace/report.md",
            "/var/minis/attachments" to "${sessionTree()}/attachments",
            "/var/minis/offloads/model-use/a.bin" to "${sessionTree()}/offloads/model-use/a.bin",
            "/var/minis/browser/tabs/a.html" to "${sessionTree()}/browser/tabs/a.html",
        )
        for ((path, expected) in cases) {
            val d = classify(path)
            assertTrue("$path should be Resolved, was $d", d is SessionScopedPathDecision.Resolved)
            assertEquals(expected, (d as SessionScopedPathDecision.Resolved).file.absolutePath)
        }
    }

    @Test
    fun `paths with no session view classify as NotSessionScoped`() {
        // No session context: the global resolver is the honest answer.
        assertTrue(classify("/var/minis/workspace/report.md", sid = null) is SessionScopedPathDecision.NotSessionScoped)
        // Shared scopes are session-independent by construction.
        assertTrue(classify("/var/minis/shared/notes/a.md") is SessionScopedPathDecision.NotSessionScoped)
        assertTrue(classify("/var/minis/memory/GLOBAL.md") is SessionScopedPathDecision.NotSessionScoped)
        // Guest paths outside /var/minis (e.g. --output /tmp/out.json → rootfs).
        assertTrue(classify("/tmp/out.json") is SessionScopedPathDecision.NotSessionScoped)
        assertTrue(classify("workspace/relative.md") is SessionScopedPathDecision.NotSessionScoped)
    }

    @Test
    fun `guard-refused scope paths classify as Rejected, not NotSessionScoped`() {
        val attacks = listOf(
            "/var/minis/workspace/../../../debug_server_token",
            "/var/minis/workspace/../../shared_prefs/offload_permissions.xml",
            "/var/minis/workspace/../browser/cookies",
            "/var/minis/workspace/..",
        )
        for (path in attacks) {
            assertTrue(
                "$path must be Rejected (its session-scoped shape means a refusal is final), was ${classify(path)}",
                classify(path) is SessionScopedPathDecision.Rejected,
            )
        }
    }

    // ── [fix/audit3-1015 F-08] fail-closed resolution ───────────────────

    @Test
    fun `a Rejected path never reaches the global resolver`() {
        var globalCalls = 0
        val global: (String) -> File? = { globalCalls++; File(tmp.root, "global-placeholder") }
        val resolved = ModelUseOffloadHandler.sessionAwareHostFile(
            filesDir,
            sessionId,
            "/var/minis/workspace/../../../debug_server_token",
            global,
        )
        assertNull("a refused scope path must resolve to nothing", resolved)
        assertEquals("the global resolver must not get a second opinion", 0, globalCalls)
    }

    @Test
    fun `a NotSessionScoped path still uses the global resolver`() {
        val expected = File(filesDir, "rootfs-tmp-out.json")
        var seen: String? = null
        val resolved = ModelUseOffloadHandler.sessionAwareHostFile(filesDir, sessionId, "/tmp/out.json") {
            seen = it; expected
        }
        assertEquals("/tmp/out.json", seen)
        assertEquals(expected.absolutePath, resolved?.absolutePath)
    }

    /**
     * The concrete leak this fix closes, as a differential: with session B
     * holding a file at the same relative name, the pre-fix elvis chain
     * (`?: PRootKernel.resolveHostPath(...)`) answered the refused query with
     * session B's file — a cross-session read the caller never asked for. The
     * injected resolver below IS that global resolver, returning B's file.
     */
    @Test
    fun `a refused traversal cannot come back as another session's file`() {
        val sidB = "9c2b8f31-6c2e-4f0a-9c1d-2b6f7a5e8d10"
        val other = File(filesDir, "minis-sessions/$sidB/workspace/secret.txt")
        other.parentFile.mkdirs()
        other.writeText("session B's data")
        val globalWouldReturn = { _: String -> other }

        // The path a hostile caller passes from session A: refused by the
        // in-scope guard, but the global resolver's normalized tail
        // (`workspace/secret.txt`) matches B's real file.
        val refused = ModelUseOffloadHandler.sessionAwareHostFile(
            filesDir, sessionId, "/var/minis/workspace/../workspace/secret.txt", globalWouldReturn,
        )
        assertNull("refused path must not resolve to session B's file", refused)

        // Control: the file really is reachable through the global resolver, so
        // the assertion above is about the fail-closed decision, not about a
        // missing file.
        assertEquals(other.absolutePath, globalWouldReturn("/var/minis/workspace/secret.txt")?.absolutePath)
        assertTrue(other.exists())
    }
}

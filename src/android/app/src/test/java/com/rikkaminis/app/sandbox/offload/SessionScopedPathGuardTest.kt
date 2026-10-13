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

    private val filesDir: File get() = tmp.newFolder("files")
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
}

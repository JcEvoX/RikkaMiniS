package com.rikkaminis.app.sandbox.offload

import com.rikkaminis.app.debug.ExecScope
import com.rikkaminis.app.debug.execScopeOf
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [dv18-A05] The debug exec entry points must run under the caller's session
 * scope (ISSUE-A-05).
 *
 * `debug.modelUse.exec` and `debug.sessions.exec` rebuilt their
 * `NativeOffloadRequest` with a hardcoded `cwd = "/"` and `sessionId = null`,
 * and DebugOffloadHandler's forwarder handed the RPC only the bare argv — so
 * `minis-debug model-use run --input notes.txt` resolved the relative operand
 * against the guest root, and a caller that named a chat session still reached
 * the session-less "global" arm.
 *
 * The contract has two halves, one at each end of that hop: the forwarder
 * decides WHICH subcommands carry the scope ([execScopeParams]), and the entry
 * point maps the params onto the request
 * ([com.rikkaminis.app.debug.execScopeOf]). Both halves are pure, so the matrix
 * below drives the real implementations instead of regexing around them;
 * expectations are literal values, never derived from the code under test. The
 * source-shape arms then pin the wiring between the halves, which is where the
 * defect actually lived.
 */
class DebugExecScopeTest {

    private companion object {
        private const val RPC =
            "src/android/app/src/main/java/com/rikkaminis/app/debug/DebugRPCHandler.kt"
        private const val OFFLOAD =
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/DebugOffloadHandler.kt"
    }

    // ── arm 1: the forwarder's decision (writer side) ───────────────────────

    @Test
    fun `model-use carries the caller's session and cwd`() {
        assertEquals(
            mapOf("sessionId" to "sess-A", "cwd" to "/var/minis/workspace"),
            execScopeParams("model-use", "sess-A", "/var/minis/workspace"),
        )
        // The legacy camelCase spelling the CLI also accepts is the same decision.
        assertEquals(
            mapOf("sessionId" to "sess-A", "cwd" to "/var/minis/workspace"),
            execScopeParams("modelUse", "sess-A", "/var/minis/workspace"),
        )
    }

    @Test
    fun `a blank session is omitted so the handler keeps its global arm`() {
        // An interactive terminal has no chat session: the key must be ABSENT
        // rather than an empty value, so the RPC side falls back to null and
        // the handler's global-arm semantics stay exactly as before.
        assertEquals(mapOf("cwd" to "/root"), execScopeParams("model-use", null, "/root"))
        assertEquals(mapOf("cwd" to "/root"), execScopeParams("model-use", "", "/root"))
        assertEquals(mapOf("cwd" to "/root"), execScopeParams("model-use", "   ", "/root"))
    }

    @Test
    fun `a blank cwd is omitted rather than forwarded as an empty string`() {
        assertEquals(mapOf("sessionId" to "sess-A"), execScopeParams("modelUse", "sess-A", null))
        assertEquals(mapOf("sessionId" to "sess-A"), execScopeParams("modelUse", "sess-A", ""))
    }

    @Test
    fun `the raw call escape hatch and the device-level shell are untouched`() {
        // `call` params are the caller's, verbatim; `shizuku` drives a
        // device-level privileged shell where a guest cwd means nothing; `exec`
        // maps to debug.shellExecute, which owns its own `session` param; the
        // ls/read/write arm has its own session-id passthrough.
        for (sub in listOf("call", "shizuku", "exec", "ls", "read", "write", "minis-config")) {
            assertTrue(
                "$sub must not receive the guest session scope",
                execScopeParams(sub, "sess-A", "/var/minis/workspace").isEmpty(),
            )
        }
    }

    // ── arm 2: the entry point's mapping (reader side) ──────────────────────

    @Test
    fun `the params map back onto the request scope`() {
        val scope = execScopeOf("sess-A", "/var/minis/workspace")
        assertEquals("sess-A", scope.sessionId)
        assertEquals("/var/minis/workspace", scope.cwd)
        assertEquals(ExecScope("sess-A", "/var/minis/workspace"), scope)
    }

    @Test
    fun `absent values reproduce exactly what the entry point hardcoded before`() {
        // A client that predates the keys sends nothing: `optString(key, "")`
        // yields "" and the pre-fix request (`cwd = "/"`, `sessionId = null`)
        // must come back byte-for-byte.
        val scope = execScopeOf("", "")
        assertNull("no session ⇒ the handler's global arm", scope.sessionId)
        assertEquals("the guest root is the pre-fix cwd", "/", scope.cwd)
        assertEquals(ExecScope(null, ExecScope.DEFAULT_CWD), scope)
        // Whitespace is a blank value, not a session named " ".
        assertNull(execScopeOf("   ", "  ").sessionId)
        assertEquals("/", execScopeOf("   ", "  ").cwd)
        // Surrounding whitespace on a real value is an artifact, not part of it.
        assertEquals("sess-A", execScopeOf(" sess-A ", " /root ").sessionId)
        assertEquals("/root", execScopeOf(" sess-A ", " /root ").cwd)
    }

    // ── arm 3: round trip through the real JSON hop ─────────────────────────

    @Test
    fun `the forwarded keys survive the round trip into the request`() {
        // The forwarder puts the keys into the RPC params; the entry point reads
        // them back with `params.optString(key, "")` — the exact expression both
        // handle*Exec bodies use (see arm 4).
        for ((sessionId, cwd) in listOf(
            "sess-A" to "/var/minis/workspace",
            null to "/root",
            "sess-B" to null,
        )) {
            val params = JSONObject().put("args", JSONArray().put("run"))
            execScopeParams("modelUse", sessionId, cwd).forEach { (key, value) ->
                params.put(key, value)
            }
            val scope = execScopeOf(params.optString("sessionId", ""), params.optString("cwd", ""))
            assertEquals("sessionId of ($sessionId, $cwd)", sessionId, scope.sessionId)
            assertEquals("cwd of ($sessionId, $cwd)", cwd ?: "/", scope.cwd)
        }
    }

    @Test
    fun `a call that forwarded nothing reproduces the pre-fix request`() {
        // The interactive-terminal / legacy-client case, end to end.
        val params = JSONObject().put("args", JSONArray().put("run"))
        val scope = execScopeOf(params.optString("sessionId", ""), params.optString("cwd", ""))
        assertNull(scope.sessionId)
        assertEquals("/", scope.cwd)
    }

    // ── arm 4: production wiring (source shape) ────────────────────────────

    @Test
    fun `both scope-taking exec entry points route through the shared parse`() {
        val rpc = codeOnly(readRepoFile(RPC))
        for (fn in listOf("handleModelUseExec", "handleSessionsExec")) {
            val body = declaration(rpc, "private fun $fn(")
            assertTrue(
                "$fn must map the RPC params through the shared parse (single source):\n$body",
                body.contains(
                    "execScopeOf(params.optString(\"sessionId\", \"\"), params.optString(\"cwd\", \"\"))",
                ),
            )
            assertFalse("$fn must not hardcode cwd:\n$body", body.contains("cwd = \"/\""))
            assertFalse("$fn must not hardcode sessionId:\n$body", body.contains("sessionId = null"))
            assertTrue(
                "$fn must feed the scope into the request:\n$body",
                body.contains("cwd = scope.cwd") && body.contains("sessionId = scope.sessionId"),
            )
        }
    }

    @Test
    fun `the device-level shell entry keeps its session-less scope on purpose`() {
        // Registered negative decision (backlog §1038A-2 R2): Shizuku's exec
        // drives a device-level privileged shell where a guest cwd has no
        // meaning, so it is deliberately NOT scoped. If that ever changes,
        // change it here and the gate's DEBUG_EXEC_DEVICE_LEVEL together.
        val body = declaration(codeOnly(readRepoFile(RPC)), "private fun handleShizukuExec(")
        assertTrue(
            "the shizuku entry is the device-level exception:\n$body",
            body.contains("cwd = \"/\"") && body.contains("sessionId = null"),
        )
    }

    @Test
    fun `the forwarder injects the scope into the forwarded call`() {
        val body = declaration(codeOnly(readRepoFile(OFFLOAD)), "override fun handle(")
        assertTrue(
            "the forwarded call must carry the caller's scope:\n$body",
            body.contains("execScopeParams(sub, request.sessionId, request.cwd)"),
        )
        // The decision stays declarative — the gate asserts the same set.
        assertEquals(setOf("model-use", "modelUse"), EXEC_SCOPE_SUBCOMMANDS)
    }

    // ── helpers ────────────────────────────────────────────────────────────

    /**
     * Full declaration (header + body) whose header contains [marker],
     * brace-balanced with string literals skipped. Fails closed when the marker
     * is absent — a probe that silently slices nothing would pass on an empty
     * string.
     */
    private fun declaration(src: String, marker: String): String {
        val idx = src.indexOf(marker)
        assertTrue("declaration '$marker' not found — fail-closed", idx >= 0)
        val open = src.indexOf("{", idx)
        assertTrue("declaration '$marker' has no body — fail-closed", open >= 0)
        var depth = 0
        var inString = false
        var i = open
        while (i < src.length) {
            val c = src[i]
            if (inString) {
                if (c == '\\') {
                    i += 2
                    continue
                }
                if (c == '"') inString = false
                i++
                continue
            }
            when (c) {
                '"' -> inString = true
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) return src.substring(idx, i + 1)
                }
            }
            i++
        }
        throw AssertionError("declaration '$marker' is unbalanced — fail-closed")
    }

    /**
     * Line-based comment stripper, so a comment that merely MENTIONS a literal
     * cannot satisfy (or break) an assertion. Safe for these three files: none
     * of them contains `//` inside a string literal.
     */
    private fun codeOnly(src: String): String {
        val out = StringBuilder()
        var inBlock = false
        for (line in src.lines()) {
            var s = line
            if (inBlock) {
                val end = s.indexOf("*/")
                if (end < 0) {
                    out.append('\n')
                    continue
                }
                s = s.substring(end + 2)
                inBlock = false
            }
            val d = s.indexOf("//")
            val b = s.indexOf("/*")
            s = when {
                b >= 0 && (d < 0 || b < d) -> {
                    inBlock = true
                    s.substring(0, b)
                }
                d >= 0 -> s.substring(0, d)
                else -> s
            }
            out.append(s).append('\n')
        }
        return out.toString()
    }

    private fun readRepoFile(relative: String): String {
        var dir: File? = File(System.getProperty("user.dir") ?: ".")
        while (dir != null) {
            val f = File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw java.io.FileNotFoundException("$relative not found from user.dir=${System.getProperty("user.dir")}")
    }
}

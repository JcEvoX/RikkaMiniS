package com.rikkaminis.app.sandbox.offload

import java.io.File
import java.io.FileNotFoundException
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1031-C] ISSUE-04 B2 — `minis-debug ls|read|write` route their positional
 * through the same relative→absolute join every sibling handler uses, instead
 * of handing a relative token to DebugRPCHandler (which joins onto the rootfs
 * ROOT, not the session cwd).
 *
 * Layer split (why this file does not drive the handler end to end):
 *  - `DebugOffloadHandler.handle()` is bound to `android.content.Context`
 *    (token + file reads) and its RPC round trip needs the in-app DebugServer
 *    on 127.0.0.1:5321, so a JVM run would prove nothing about the real CLI.
 *  - So CI pins: (a) the pure join + the subcommand scoping of the post-pass,
 *    (b) the params-rewrite contract (only `path`; omitted stays omitted),
 *    (c) the *source* wiring of the private `handle()` body, and the CLI-arg
 *    shapes that are pinned verbatim by `offload_cli_args_check.py` D7/D8.
 *  - The real bodies are additionally compiled and run in the sandbox probe.
 *
 * Expectations below are literals, never derived from the code under test.
 */
class DebugOffloadHandlerTest {

    // ── part 1: the shared join (resolveMaybeRelative) drives the CLI too ────

    @Test
    fun `a relative positional joins onto the guest cwd`() {
        assertEquals(
            "/var/minis/workspace/notes.txt",
            resolveMaybeRelative("notes.txt", "/var/minis/workspace"),
        )
        assertEquals(
            "/var/minis/workspace/sub/x.json",
            resolveMaybeRelative("sub/x.json", "/var/minis/workspace"),
        )
        assertEquals(
            "/var/minis/workspace/./x.txt",
            resolveMaybeRelative("./x.txt", "/var/minis/workspace"),
        )
    }

    @Test
    fun `an absolute positional and an unusable cwd are passed through`() {
        assertEquals("/tmp/x.txt", resolveMaybeRelative("/tmp/x.txt", "/var/minis/workspace"))
        assertEquals("notes.txt", resolveMaybeRelative("notes.txt", null))
        assertEquals("notes.txt", resolveMaybeRelative("notes.txt", ""))
        assertEquals("notes.txt", resolveMaybeRelative("notes.txt", "relative/cwd"))
    }

    @Test
    fun `a trailing slash on the cwd does not double up`() {
        assertEquals(
            "/var/minis/workspace/notes.txt",
            resolveMaybeRelative("notes.txt", "/var/minis/workspace/"),
        )
    }

    // ── part 2: subcommand scoping of the post-pass ─────────────────────────

    @Test
    fun `only the path-carrying subcommands are normalized`() {
        assertEquals(setOf("ls", "read", "write"), DEBUG_PATH_SUBCOMMANDS)
        for (sub in listOf("ls", "read", "write")) {
            assertEquals(
                sub,
                "/var/minis/workspace/a.txt",
                debugPathFor(sub, "a.txt", "/var/minis/workspace"),
            )
        }
        // Pass-through tails (`exec` / `shizuku` / `model-use`) and the
        // parameter-less subcommands must never have a token rewritten.
        for (sub in listOf(
            "exec", "shizuku", "model-use", "modelUse", "screenshot", "discover", "appInfo", "call",
        )) {
            assertNull(sub, debugPathFor(sub, "a.txt", "/var/minis/workspace"))
        }
    }

    @Test
    fun `an omitted path stays omitted`() {
        assertNull(debugPathFor("ls", "", "/var/minis/workspace"))

        // `minis-debug ls` with no argument relies on the RPC's own default —
        // the post-pass must not materialise an empty `path`.
        val bare = JSONObject()
        normalizeDebugRpcPaths("ls", bare, "/var/minis/workspace")
        assertFalse("`ls` with no argument must keep its RPC default", bare.has("path"))

        val empty = JSONObject().put("path", "")
        normalizeDebugRpcPaths("ls", empty, "/var/minis/workspace")
        assertEquals("", empty.getString("path"))
    }

    @Test
    fun `the post-pass rewrites only the path field of the built params`() {
        val write = JSONObject().put("path", "notes.txt").put("content", "hi").put("encoding", "utf8")
        normalizeDebugRpcPaths("write", write, "/var/minis/workspace")
        assertEquals("/var/minis/workspace/notes.txt", write.getString("path"))
        assertEquals("hi", write.getString("content"))
        assertEquals("utf8", write.getString("encoding"))

        val read = JSONObject().put("path", "/etc/passwd").put("limit", 100)
        normalizeDebugRpcPaths("read", read, "/var/minis/workspace")
        assertEquals("/etc/passwd", read.getString("path"))
        assertEquals(100L, read.getLong("limit"))

        val exec = JSONObject().put("command", "cat notes.txt")
        normalizeDebugRpcPaths("exec", exec, "/var/minis/workspace")
        assertFalse("pass-through commands must stay verbatim", exec.has("path"))
        assertEquals("cat notes.txt", exec.getString("command"))
    }

    // ── part 3: wiring this test cannot execute (private, Context-bound) ────

    @Test
    fun `handle normalizes the built rpc params with the request cwd`() {
        val body = memberSlice(
            readRepoFile(HANDLER_REL),
            "override fun handle(request: NativeOffloadRequest)",
        )
        assertTrue(
            "the post-pass must run on the params of the RpcCall buildRpcCall returned",
            body.contains("normalizeDebugRpcPaths(sub, rpc.params, request.cwd)"),
        )
        assertTrue("the subcommand token must come from splitArgv", body.contains("val sub = argv.subcommand"))
    }

    @Test
    fun `buildRpcCall keeps the raw positional and its pinned signature`() {
        val src = readRepoFile(HANDLER_REL)
        // offload_cli_args_check.py D7/D8 pin these two strings verbatim: the
        // post-pass must not be inlined into buildRpcCall.
        assertTrue(src.contains("buildRpcCall(sub, args, argv.rawRest)"))
        assertTrue(
            src.contains(
                "private fun buildRpcCall(sub: String, args: OffloadArgs, rawRest: List<String>): RpcCall {",
            ),
        )
        // The raw positional is still what buildRpcCall reads — normalization
        // happens once, on the built params, not per subcommand branch.
        for (needle in listOf(
            "rest.firstOrNull()?.let { p.put(\"path\", it) }",
            "val p = JSONObject().put(\"path\", path)",
            "val p = JSONObject().put(\"path\", path).put(\"content\", content)",
        )) {
            assertTrue("missing raw-positional shape: $needle", src.contains(needle))
        }
    }

    @Test
    fun `the pass-through subcommand set is unchanged`() {
        val src = readRepoFile(HANDLER_REL)
        assertTrue(
            src.contains(
                "private val PASSTHROUGH_SUBCOMMANDS = setOf(\"exec\", \"shizuku\", \"model-use\", \"modelUse\")",
            ),
        )
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private fun readRepoFile(relative: String): String {
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
            val line = lines[i]
            val indent = line.takeWhile { it == ' ' }.length
            indent <= 4 && line.isNotBlank() && !line.trimStart().startsWith("//") &&
                !line.trimStart().startsWith("*") &&
                Regex("^(private|internal|public|protected|fun|val|var|@)").containsMatchIn(line.trimStart())
        } ?: lines.size
        return lines.subList(start, end).joinToString("\n")
    }

    private companion object {
        const val HANDLER_REL =
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/DebugOffloadHandler.kt"
    }
}

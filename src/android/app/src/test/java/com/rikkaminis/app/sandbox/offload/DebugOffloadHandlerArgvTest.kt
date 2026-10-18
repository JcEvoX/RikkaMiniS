package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1028-A] `minis-debug` argv routing (ISSUE-08).
 *
 * `OffloadArgs` is a GREEDY parse of the whole invocation: every `-`-prefixed
 * token anywhere becomes one of THIS command's flags, and `--key value` pairs
 * anywhere are consumed together. That is correct for options this command
 * owns, and wrong for `exec` / `shizuku` / `model-use`, whose tail is another
 * program's argv: `minis-debug exec ls -l` turned `-l` into this command's
 * boolean flag and ran a bare `ls`, and `minis-debug model-use run --model x`
 * ate the `--model x` pair. Nothing in the reply reveals the rewrite.
 *
 * The split under test (`[DebugOffloadHandler.splitArgv]`) is structural:
 * `[globals…] <subcommand> [tail…]`, and `[DebugOffloadHandler.forwardedArgv]`
 * decides per subcommand whether the tail or the greedy positionals feed the
 * RPC. Expectations below are literal argv/replies, not derived from the code
 * under test.
 */
class DebugOffloadHandlerArgvTest {

    /** The four tokens the runner strips before the handler sees anything. */
    private fun raw(vararg tokens: String): List<String> = tokens.toList()

    private data class Case(
        val argv: List<String>,
        val subcommand: String?,
        val globals: List<String>,
        val rawRest: List<String>,
        /** Expected [DebugOffloadHandler.forwardedArgv] when [subcommand] != null. */
        val forwarded: List<String>,
    )

    private val cases = listOf(
        // ── pass-through subcommands: the tail is forwarded verbatim ─────────
        Case(raw("exec", "uname", "-a"), "exec", emptyList(), listOf("uname", "-a"), listOf("uname", "-a")),
        Case(
            raw("--compact", "exec", "uname", "-a"), "exec", listOf("--compact"),
            listOf("uname", "-a"), listOf("uname", "-a"),
        ),
        Case(raw("exec", "ls", "-l"), "exec", emptyList(), listOf("ls", "-l"), listOf("ls", "-l")),
        Case(raw("exec", "--", "-v"), "exec", emptyList(), listOf("-v"), listOf("-v")),
        Case(raw("shizuku", "pm", "list", "packages"), "shizuku", emptyList(),
            listOf("pm", "list", "packages"), listOf("pm", "list", "packages")),
        Case(raw("model-use", "run", "--model", "x"), "model-use", emptyList(),
            listOf("run", "--model", "x"), listOf("run", "--model", "x")),
        Case(raw("modelUse", "run", "--input", "a.json"), "modelUse", emptyList(),
            listOf("run", "--input", "a.json"), listOf("run", "--input", "a.json")),
        Case(raw("-q", "exec", "grep", "-q", "x"), "exec", listOf("-q"),
            listOf("grep", "-q", "x"), listOf("grep", "-q", "x")),

        // ── everything else keeps the historical greedy view ────────────────
        Case(raw("ls", "/sdcard"), "ls", emptyList(), listOf("/sdcard"), listOf("/sdcard")),
        Case(raw("--compact", "ls", "/sdcard"), "ls", listOf("--compact"), listOf("/sdcard"), listOf("/sdcard")),
        Case(raw("read", "--offset", "3", "/f"), "read", emptyList(),
            listOf("--offset", "3", "/f"), listOf("/f")),
        Case(raw("read", "/f", "--limit", "10"), "read", emptyList(),
            listOf("/f", "--limit", "10"), listOf("/f")),
        Case(raw("write", "/f", "--content", "hi"), "write", emptyList(),
            listOf("/f", "--content", "hi"), listOf("/f")),
        Case(raw("call", "debug.logs.list", "--params", "{}"), "call", emptyList(),
            listOf("debug.logs.list", "--params", "{}"), listOf("debug.logs.list")),

        // ── no subcommand at all ─────────────────────────────────────────────
        Case(raw("--compact"), null, listOf("--compact"), emptyList(), emptyList()),
        Case(raw("-q", "--base64"), null, listOf("-q", "--base64"), emptyList(), emptyList()),
    )

    @Test
    fun argvRoutingTable() {
        for (c in cases) {
            val split = DebugOffloadHandler.splitArgv(c.argv)
            assertEquals("subcommand of ${c.argv}", c.subcommand, split.subcommand)
            assertEquals("globals of ${c.argv}", c.globals, split.globals)
            assertEquals("rawRest of ${c.argv}", c.rawRest, split.rawRest)
            val sub = c.subcommand
            if (sub != null) {
                val args = OffloadArgs(c.argv)
                assertEquals(
                    "forwarded argv of ${c.argv}",
                    c.forwarded,
                    DebugOffloadHandler.forwardedArgv(sub, args, split.rawRest),
                )
            }
        }
    }

    /** The regression this fix is about: the child's own `-l` reaches the child. */
    @Test
    fun childFlagsSurviveIntoTheCommandLine() {
        val split = DebugOffloadHandler.splitArgv(raw("exec", "ls", "-l"))
        val command = DebugOffloadHandler.execCommandLine(
            DebugOffloadHandler.forwardedArgv("exec", OffloadArgs(raw("exec", "ls", "-l")), split.rawRest),
        )
        assertEquals("ls -l", command)
        // The pre-fix greedy view: `-l` was swallowed, leaving a bare `ls`.
        assertEquals(listOf("exec", "ls"), OffloadArgs(raw("exec", "ls", "-l")).positional)
    }

    /**
     * A `-q` belonging to the child must not strip THIS command's envelope:
     * output shaping reads the global slice only.
     */
    @Test
    fun childQuietFlagDoesNotReachTheTopLevelEnvelope() {
        val argv = raw("exec", "grep", "-q", "x")
        val split = DebugOffloadHandler.splitArgv(argv)
        assertTrue("the greedy view sees the child's -q", OffloadArgs(argv).hasFlag("q", "quiet"))
        assertFalse("the global slice must not", OffloadArgs(split.globals).hasFlag("q", "quiet", "compact"))
        // A `-q` the USER aimed at this command still shapes the output.
        assertTrue(OffloadArgs(raw("-q", "exec", "grep", "-q", "x")).hasFlag("q"))
        assertTrue(OffloadArgs(DebugOffloadHandler.splitArgv(raw("-q", "exec", "grep", "-q", "x")).globals).hasFlag("q"))
    }

    /** `--args '["pm","list"]'` is still read from the greedy parse (compat). */
    @Test
    fun explicitArgsEnvelopeStillParsed() {
        val json = "[\"pm\",\"list\"]"
        for (argv in listOf(
            raw("shizuku", "--args", json),
            raw("--args", json, "shizuku"),
            raw("model-use", "run", "--args", json),
        )) {
            val args = OffloadArgs(argv)
            assertEquals("explicit --args of $argv", json, args.get("args"))
        }
        // Same subcommand name detection either way round.
        assertEquals("shizuku", DebugOffloadHandler.splitArgv(raw("--args", json, "shizuku")).subcommand)
        assertEquals("shizuku", DebugOffloadHandler.splitArgv(raw("shizuku", "--args", json)).subcommand)
    }

    /** `--` is the end-of-options marker, never a subcommand. */
    @Test
    fun doubleDashIsNotASubcommand() {
        val split = DebugOffloadHandler.splitArgv(raw("--compact", "exec", "--", "-v"))
        assertEquals("exec", split.subcommand)
        assertEquals(listOf("--compact"), split.globals)
        assertEquals(listOf("-v"), split.rawRest)
        // Leading marker: dropped, and the token after it is the subcommand.
        val leading = DebugOffloadHandler.splitArgv(raw("--", "exec"))
        assertEquals("exec", leading.subcommand)
        assertTrue(leading.globals.isEmpty())
        // The marker alone is not a subcommand.
        val markerOnly = DebugOffloadHandler.splitArgv(raw("--compact", "--"))
        assertNull(markerOnly.subcommand)
    }

    @Test
    fun emptyTailForwardsNothing() {
        val split = DebugOffloadHandler.splitArgv(raw("exec"))
        assertEquals("exec", split.subcommand)
        assertTrue(split.rawRest.isEmpty())
        assertEquals("", DebugOffloadHandler.execCommandLine(split.rawRest))
    }

    /** Joining is lang-exec semantics: the child's shell splits it again. */
    @Test
    fun commandLineIsSpaceJoined() {
        assertEquals("sh -c echo a b", DebugOffloadHandler.execCommandLine(listOf("sh", "-c", "echo a b")))
        assertEquals("uname -a", DebugOffloadHandler.execCommandLine(listOf("uname", "-a")))
    }
}

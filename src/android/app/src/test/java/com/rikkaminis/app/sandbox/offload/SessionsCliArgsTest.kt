package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * B26 — `minis-sessions-cli` argv handling + timestamp formatter.
 *
 * B26a: `--async` / `--help` were missing from the handler's booleanFlags
 * declaration, so `minis-sessions-cli --async send --prompt "hi"` swallowed
 * "send" as the value of --async (the next token does not start with `-`)
 * and degraded to the default `list` subcommand with async=false. OffloadArgs
 * itself is a shared class (touched by every handler) — the fix lives in
 * this handler's declaration, so the before/after contrast is pinned at the
 * parser level with the two declaration sets.
 *
 * B26b: the companion DATE_FMT was a shared SimpleDateFormat formatted from
 * cmdList / cmdSearch / cmdMessages concurrently (any handler call can land
 * on a different worker thread). It is now an immutable java.time
 * DateTimeFormatter — the same pattern as HangDetector.DATE_FORMAT /
 * CrashFileSender.STAMP_FMT. The handler needs an Application (Context +
 * ChatRepository) so the B26b side is asserted against the compiled source —
 * the FastioWiringTest shape.
 */
class SessionsCliArgsTest {

    private val handlerFile =
        File("src/main/java/com/rikkaminis/app/sandbox/offload/SessionsOffloadHandler.kt")

    // ── B26a: parser-level before/after contrast ──

    /** Old declaration set — reproduces the swallowed-subcommand failure. */
    @Test
    fun oldDeclaration_swallowsSendAsAsyncValue() {
        val args = OffloadArgs(listOf("--async", "send", "--prompt", "hi"), booleanFlags = setOf("full"))
        assertEquals("send", args.get("async")) // greedy consumption
        assertFalse(args.hasFlag("async"))
        assertTrue(args.positional.isEmpty()) // sub degrades to "list"
    }

    /** New declaration set — the shape the handler must declare from now on. */
    @Test
    fun newDeclaration_keepsSendSubcommandAndAsyncFlag() {
        val args = OffloadArgs(
            listOf("--async", "send", "--prompt", "hi"),
            booleanFlags = setOf("full", "async", "wait", "help", "h"),
        )
        assertEquals("send", args.positional.first())
        assertTrue(args.hasFlag("async"))
        assertEquals("hi", args.get("prompt"))
        assertEquals(null, args.get("async"))
    }

    // ── [dv19-A1 A-02] `--wait`, the same defect one flag over ──────────────
    //
    // `wait` was missing from that same declaration list. The KDoc of cmdSend
    // documents `--wait` as the DEFAULT mode, so it is a spelling callers
    // legitimately write; undeclared, `--wait send --prompt "hi"` stored "send"
    // as the VALUE of `--wait`, the positional list came out empty, the
    // subcommand degraded to `list` and the command answered with the session
    // list at exit 0 — a wrong answer that looks like a successful call.

    /** Old declaration set — reproduces the swallowed-subcommand failure. */
    @Test
    fun oldDeclaration_swallowsSendAsWaitValue() {
        val args = OffloadArgs(
            listOf("--wait", "send", "--prompt", "hi"),
            booleanFlags = setOf("full", "async", "help", "h"),
        )
        assertEquals("send", args.get("wait")) // greedy consumption
        assertFalse(args.hasFlag("wait"))
        assertTrue(args.positional.isEmpty()) // sub degrades to "list"
        assertEquals("hi", args.get("prompt"))
    }

    @Test
    fun newDeclaration_keepsSendSubcommandAndWaitFlag() {
        val args = OffloadArgs(
            listOf("--wait", "send", "--prompt", "hi"),
            booleanFlags = setOf("full", "async", "wait", "help", "h"),
        )
        assertEquals("send", args.positional.first())
        assertTrue(args.hasFlag("wait"))
        assertFalse(args.hasFlag("async"))
        assertEquals("hi", args.get("prompt"))
        assertEquals(null, args.get("wait"))
    }

    /** `--wait-timeout` is a value option and must keep eating its `<sec>`. */
    @Test
    fun waitClearedDoesNotTurnWaitTimeoutIntoAFlag() {
        val args = OffloadArgs(
            listOf("--wait", "send", "--prompt", "hi", "--wait-timeout", "30"),
            booleanFlags = setOf("full", "async", "wait", "help", "h"),
        )
        assertEquals("send", args.positional.first())
        assertEquals(30, args.getInt("wait-timeout"))
        assertEquals("hi", args.get("prompt"))
        assertTrue(args.hasFlag("wait"))
    }

    /** `--help` / `-h` placed before the subcommand must stay flags, not values. */
    @Test
    fun helpBeforeSubcommandStaysAFlag() {
        val args = OffloadArgs(
            listOf("--help", "list"),
            booleanFlags = setOf("full", "async", "help", "h"),
        )
        assertTrue(args.hasFlag("h", "help"))
        assertEquals("list", args.positional.first())
    }

    /** Value options keep consuming their values under the new declaration set. */
    @Test
    fun valueOptionsStillConsumeValues() {
        val args = OffloadArgs(
            listOf("send", "--prompt", "hello world", "--wait-timeout", "30"),
            booleanFlags = setOf("full", "async", "help", "h"),
        )
        assertEquals("send", args.positional.first())
        assertEquals("hello world", args.get("prompt"))
        assertEquals(30, args.getInt("wait-timeout"))
    }

    // ── B26b: companion formatter, wiring-level ──

    @Test
    fun dateFmtIsImmutableDateTimeFormatter() {
        val src = handlerFile.readText()
        assertTrue(
            "DATE_FMT must be a java.time DateTimeFormatter (thread-safe), not a shared SimpleDateFormat",
            src.contains("private val DATE_FMT: java.time.format.DateTimeFormatter"),
        )
        assertTrue(src.contains("withZone(java.time.ZoneId.systemDefault())"))
        assertFalse(
            "a companion-scope SimpleDateFormat would race across concurrent handler calls",
            Regex("private val DATE_FMT = SimpleDateFormat").containsMatchIn(src),
        )
    }

    @Test
    fun timestampCallSitesUseInstant() {
        val src = handlerFile.readText()
        val sites = Regex("DATE_FMT\\.format\\(Instant\\.ofEpochMilli\\(").findAll(src).count()
        assertTrue(
            "all 4 timestamp call sites (cmdList ×2, cmdSearch, cmdMessages) must feed Instant",
            sites == 4,
        )
    }

    @Test
    fun booleanFlagsDeclarationWired() {
        val src = handlerFile.readText()
        assertTrue(
            "the handler must declare async/wait/help/h as boolean flags",
            src.contains("booleanFlags = setOf(\"full\", \"async\", \"wait\", \"help\", \"h\")"),
        )
    }

    /** [dv19-A1 A-02] the flag must be documented, not only implied by KDoc. */
    @Test
    fun helpTextListsWaitAsAnOption() {
        val lines = handlerFile.readText().lines()
        assertTrue(
            "--wait must have its own HELP_TEXT option line (not only --wait-timeout)",
            lines.any { it.trimStart().startsWith("--wait ") && it.contains("(send only)") },
        )
    }
}

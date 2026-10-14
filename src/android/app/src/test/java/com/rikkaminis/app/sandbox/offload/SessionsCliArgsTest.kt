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
            booleanFlags = setOf("full", "async", "help", "h"),
        )
        assertEquals("send", args.positional.first())
        assertTrue(args.hasFlag("async"))
        assertEquals("hi", args.get("prompt"))
        assertEquals(null, args.get("async"))
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
            "the handler must declare async/help/h as boolean flags",
            src.contains("booleanFlags = setOf(\"full\", \"async\", \"help\", \"h\")"),
        )
    }
}

package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1038 FIX-1 / A-01a + A-01b] The argv pre-scan that finds the group
 * (shizuku) or the subcommand (fastio) must classify every leading token the way
 * the parser will — value-taking SHORT options included.
 *
 * Both defects were the same shape: the scan knew one half of the vocabulary it
 * was mirroring. A value-taking short option leading the invocation was read as
 * a bare switch, so its VALUE became the first positional and the group was
 * decided from the wrong token:
 *
 *   - `-p com.example package info` → group `com.example` (shizuku);
 *   - `-C /tmp tar -xf a.tar` → subcommand `/tmp` (fastio).
 *
 * The assertions below go through the production entry points
 * ([splitShizukuArgv] / [FastioOffloadHandler.firstSubcommandToken]) rather than
 * the walk itself, because the walk's index is only observable through them —
 * `group` IS `argv[firstPositionalIndex(...)]`, so pinning the group pins the
 * index (2 for the shizuku case below).
 */
class CliPrescanValueShortOptionTest {

    // ── A-01a · shizuku: a value-taking short option before the group ────────

    @Test
    fun `a leading -p does not become the shizuku group`() {
        val split = splitShizukuArgv(listOf("-p", "com.example", "package", "info"))
        assertEquals("package", split.group)
        assertEquals(listOf("-p", "com.example", "info"), split.withoutGroup)
    }

    @Test
    fun `every declared value short option is consumed together with its value`() {
        assertEquals(
            "activity",
            splitShizukuArgv(listOf("-a", "android.intent.action.VIEW", "activity", "start")).group,
        )
        assertEquals(
            "activity",
            splitShizukuArgv(listOf("-d", "content://media/external", "activity", "start")).group,
        )
        assertEquals(
            "activity",
            splitShizukuArgv(listOf("-c", "com.example/.Main", "activity", "start")).group,
        )
        assertEquals(
            "package",
            splitShizukuArgv(listOf("-p", "com.example", "package", "list")).group,
        )
    }

    @Test
    fun `a declared value short option after the group is unaffected`() {
        val split = splitShizukuArgv(listOf("package", "info", "-p", "com.example"))
        assertEquals("package", split.group)
        assertEquals(listOf("info", "-p", "com.example"), split.withoutGroup)
    }

    @Test
    fun `a leading switch is still read as a switch`() {
        assertEquals("package", splitShizukuArgv(listOf("--compact", "package", "list")).group)
        // `-q` is an output switch, not a value option: it must NOT swallow the
        // group that follows it (the mirror of the defect).
        assertEquals("package", splitShizukuArgv(listOf("-q", "package", "list")).group)
    }

    @Test
    fun `the split group and the parser agree on the value short option`() {
        val split = splitShizukuArgv(listOf("-p", "com.example", "package", "info"))
        // Same two-argument construction the handler builds (booleanFlags from
        // the group, the declared value-short vocabulary).
        val args = OffloadArgs(
            split.withoutGroup,
            booleanFlags = noValueFlagsFor(split.group),
            valueShortOptions = SHIZUKU_VALUE_SHORT_OPTIONS,
        )
        assertEquals("com.example", args.get("package", "p"))
        assertEquals(listOf("info"), args.positional)
        assertTrue(args.positional.none { it == "com.example" })
    }

    // ── A-01b · fastio: `-C` takes a directory, not the subcommand ───────────

    @Test
    fun `a leading -C does not become the fastio subcommand`() {
        val argv = listOf("-C", "/tmp", "tar", "-xf", "a.tar")
        assertEquals("tar", FastioOffloadHandler.firstSubcommandToken(argv))
    }

    @Test
    fun `the tar detection then declares the verbs the invocation spelled`() {
        // The point of getting the subcommand right: isTarCommand drives both the
        // cluster expansion and the declared-flag set, so `-C`/`-xf` are only
        // understood once `tar` is recognised.
        val expanded = FastioOffloadHandler.normalizeTarShorts(listOf("-C", "/tmp", "tar", "-xf", "a.tar"))
        assertEquals(listOf("--directory", "/tmp", "tar", "--extract", "a.tar"), expanded)
    }

    @Test
    fun `boolean leading shorts never consume the subcommand`() {
        // `-f`/`-r` are booleans (force/recursive) and `-f` inside a tar cluster
        // expands to nothing, so neither may swallow the token after it.
        assertEquals("rm", FastioOffloadHandler.firstSubcommandToken(listOf("-f", "rm", "/tmp/x")))
        assertEquals("cp", FastioOffloadHandler.firstSubcommandToken(listOf("-r", "cp", "a", "b")))
    }

    @Test
    fun `a single-dash long option keeps consuming its value`() {
        assertEquals(
            "tar",
            FastioOffloadHandler.firstSubcommandToken(listOf("-limit", "10", "tar", "-cf", "a.tar", "x")),
        )
    }

    @Test
    fun `a value-less -C sends both readers to the same honest answer`() {
        // `-C tar -xf a.tar`: `-C` takes the next token as its directory, so this
        // invocation names NO subcommand at all. Both readers land on the same
        // token for the same reason — the expander's guard asks the SAME question
        // (`firstSubcommandToken(argv) != "tar"`), and `tar` was consumed as the
        // directory, so it declines to expand. The parser then answers
        // `unknown subcommand 'a.tar'` (exit 2) instead of silently expanding a
        // cluster it cannot attribute. This case pins that the refusal comes from
        // the shared reader, not from a heuristic that "sees" a `tar` token.
        val argv = listOf("-C", "tar", "-xf", "a.tar")
        assertEquals("a.tar", FastioOffloadHandler.firstSubcommandToken(argv))
        assertEquals(argv, FastioOffloadHandler.normalizeTarShorts(argv))
    }
}

package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * [da1020-C] Behavior + wiring tests for the BrowserUse relative-path join
 * and the Fastio shared tar-verdict helper. Expectations are hardcoded
 * literals (never derived from the code under test). The wiring probes read
 * the REAL production sources (rkm.repo system property) so a mutation that
 * drops the join turns red here as well as at the scan gate.
 */
class OffloadArgsFixCTest {

    // ── A half: ConfigOffloadHandler.resolveMaybeRelative (the single join) ──

    @Test fun cookiesJoinRelativeWithAbsoluteCwd() {
        assertEquals("/tmp/x/cookies.json", resolveMaybeRelative("cookies.json", "/tmp/x"))
    }

    @Test fun cookiesJoinAbsoluteCwdWithTrailingSlash() {
        assertEquals("/tmp/x/cookies.json", resolveMaybeRelative("cookies.json", "/tmp/x/"))
    }

    @Test fun cookiesJoinAbsolutePathUnchanged() {
        assertEquals("/var/minis/workspace/c.json",
            resolveMaybeRelative("/var/minis/workspace/c.json", "/tmp/x"))
    }

    @Test fun cookiesJoinNullCwdFailClosed() {
        assertEquals("cookies.json", resolveMaybeRelative("cookies.json", null))
    }

    @Test fun cookiesJoinRelativeCwdFailClosed() {
        assertEquals("cookies.json", resolveMaybeRelative("cookies.json", "rel/dir"))
    }

    @Test fun cookiesJoinEmptyCwdFailClosed() {
        assertEquals("cookies.json", resolveMaybeRelative("cookies.json", ""))
    }

    // ── A half: BrowserUseOffloadHandler.resolveMaybeRelativeAll ──

    @Test fun pathsBothTokensNormalized() {
        assertEquals(
            listOf("/var/minis/workspace/a.png", "/var/minis/workspace/b.png"),
            resolveMaybeRelativeAll(listOf("a.png", "b.png"), "/var/minis/workspace"),
        )
    }

    @Test fun pathsAbsoluteTokenUnchanged() {
        assertEquals(listOf("/tmp/a.png"),
            resolveMaybeRelativeAll(listOf("/tmp/a.png"), "/var/minis/workspace"))
    }

    @Test fun pathsNullCwdFailClosed() {
        assertEquals(listOf("a.png", "b.png"),
            resolveMaybeRelativeAll(listOf("a.png", "b.png"), null))
    }

    @Test fun pathsRelativeCwdFailClosed() {
        assertEquals(listOf("a.png"),
            resolveMaybeRelativeAll(listOf("a.png"), "rel/dir"))
    }

    @Test fun pathsEmptyListStaysEmpty() {
        assertEquals(emptyList<String>(), resolveMaybeRelativeAll(emptyList(), "/tmp/x"))
    }

    // ── B half: FastioPieces.firstSubcommandToken ──

    @Test fun tarFirstToken() {
        assertEquals("tar", FastioPieces.firstSubcommandToken(listOf("tar", "-cf", "a.tar", "/tmp/x")))
    }

    @Test fun tarAfterCompact() {
        assertEquals("tar", FastioPieces.firstSubcommandToken(
            listOf("--compact", "tar", "-cf", "a.tar", "/tmp/x")))
    }

    @Test fun tarAfterQuietShort() {
        assertEquals("tar", FastioPieces.firstSubcommandToken(listOf("-q", "tar", "-cf", "a.tar")))
    }

    @Test fun tarAfterQuietLong() {
        assertEquals("tar", FastioPieces.firstSubcommandToken(
            listOf("--quiet", "tar", "-czf", "a.tar", "x")))
    }

    @Test fun rmAfterQuiet() {
        assertEquals("rm", FastioPieces.firstSubcommandToken(listOf("--quiet", "rm", "-rf", "/tmp/x")))
    }

    @Test fun duFirstTokenWithCreateFlag() {
        // the existing `du --create btree` spelling must still name `du`
        assertEquals("du", FastioPieces.firstSubcommandToken(listOf("du", "--create", "x")))
        assertEquals("du", FastioPieces.firstSubcommandToken(listOf("du", "--create", "btree")))
    }

    @Test fun compactOnlyYieldsNull() {
        assertEquals(null, FastioPieces.firstSubcommandToken(listOf("--compact")))
    }

    @Test fun emptyArgvYieldsNull() {
        assertEquals(null, FastioPieces.firstSubcommandToken(emptyList()))
    }

    @Test fun keyValueOptionSkipsItsValue() {
        // mirrors OffloadArgs' greedy rule for an UNKNOWN `--key`
        assertEquals("du", FastioPieces.firstSubcommandToken(listOf("--limit", "5", "du", "/tmp")))
    }

    @Test fun keyEqualsValueOptionIsSelfContained() {
        assertEquals("du", FastioPieces.firstSubcommandToken(listOf("--limit=5", "du", "/tmp")))
    }

    // ── B half: FastioPieces.fastioBooleanFlags (tar-verb scoping) ──

    @Test fun tarFlagsIncludeVerbs() {
        val f = FastioPieces.fastioBooleanFlags(true)
        assertTrue(f.contains("create"))
        assertTrue(f.contains("extract"))
        assertTrue(f.contains("gzip"))
        assertTrue(f.contains("verbose"))
        assertTrue(f.contains("recursive"))
    }

    @Test fun nonTarFlagsExcludeVerbs() {
        val f = FastioPieces.fastioBooleanFlags(false)
        assertFalse(f.contains("create"))
        assertFalse(f.contains("extract"))
        assertFalse(f.contains("gzip"))
        assertFalse(f.contains("verbose"))
        assertTrue(f.contains("recursive"))
        assertTrue(f.contains("force"))
        assertTrue(f.contains("ignore-case"))
    }

    // ── B half: du --create btree negative control (anti-over-fix) ──

    @Test fun duCreateBtreeKeepsFailingClosed() {
        val isTar = FastioPieces.firstSubcommandToken(listOf("du", "--create", "btree")) == "tar"
        assertFalse(isTar)
        val args = OffloadArgs(listOf("--create", "btree"),
            booleanFlags = FastioPieces.fastioBooleanFlags(isTar))
        // `--create` is NOT a declared boolean for `du`, so the parser consumes
        // `btree` as its value and `du()` sees an empty path list -> the
        // caller reports "du: missing <path>" (exit 2). Declaring the tar
        // verbs for every subcommand would instead walk `btree`.
        assertEquals(emptyList<String>(), args.positional)
    }

    // ── B half: full pre-parse flow, fixed arm ──

    @Test fun compactTarShortClusterReachesTheParser() {
        val raw = listOf("--compact", "tar", "-cf", "a.tar", "/tmp/x")
        val isTar = FastioPieces.firstSubcommandToken(raw) == "tar"
        assertTrue(isTar)
        val tokens = FastioPieces.normalizeLongOptionSpelling(FastioPieces.normalizeTarShorts(raw))
        val args = OffloadArgs(tokens, booleanFlags = FastioPieces.fastioBooleanFlags(isTar))
        assertTrue(args.hasFlag("create"))
        assertEquals(listOf("tar", "a.tar", "/tmp/x"), args.positional)
    }

    @Test fun classicTarShortClusterUnchanged() {
        val raw = listOf("tar", "-czf", "a.tar", "x")
        val isTar = FastioPieces.firstSubcommandToken(raw) == "tar"
        assertTrue(isTar)
        val tokens = FastioPieces.normalizeLongOptionSpelling(FastioPieces.normalizeTarShorts(raw))
        val args = OffloadArgs(tokens, booleanFlags = FastioPieces.fastioBooleanFlags(isTar))
        assertTrue(args.hasFlag("create"))
        assertTrue(args.hasFlag("gzip"))
        assertEquals(listOf("tar", "a.tar", "x"), args.positional)
    }

    @Test fun rmShortFlagsUnchanged() {
        val raw = listOf("rm", "-rf", "/tmp/x")
        val isTar = FastioPieces.firstSubcommandToken(raw) == "tar"
        assertFalse(isTar)
        val tokens = FastioPieces.normalizeLongOptionSpelling(FastioPieces.normalizeTarShorts(raw))
        val args = OffloadArgs(tokens, booleanFlags = FastioPieces.fastioBooleanFlags(isTar))
        assertEquals(listOf("rm", "/tmp/x"), args.positional)
        assertTrue(args.hasFlag("rf"))
    }

    @Test fun findStyleLongOptionsStillRewritten() {
        val raw = listOf("find", "/usr", "-name", "*.log")
        val tokens = FastioPieces.normalizeLongOptionSpelling(FastioPieces.normalizeTarShorts(raw))
        val args = OffloadArgs(tokens, booleanFlags = FastioPieces.fastioBooleanFlags(false))
        // `--name` is a `--key value` pair (find() reads args.get("name")),
        // NOT a boolean: the rewrite makes `*.log` the option's value instead
        // of a positional path.
        assertEquals(listOf("find", "/usr"), args.positional)
        assertEquals("*.log", args.get("name"))
    }

    // ── Wiring probes: the REAL production sources (gate-independent) ──

    private fun src(rel: String): String {
        val root = System.getProperty("rkm.repo")
            ?: throw AssertionError("rkm.repo system property not set")
        val f = File(root, rel)
        if (!f.isFile) throw AssertionError("missing source: ${f.absolutePath}")
        return f.readText()
    }

    private fun codeOnly(s: String): String {
        val noBlock = Regex("(?s)/\\*.*?\\*/").replace(s, "")
        val out = StringBuilder()
        for (line in noBlock.split("\n")) {
            out.append(line.substringBefore("//")).append("\n")
        }
        return out.toString()
    }

    @Test fun probeBrowserUseJoinsBeforeResolve() {
        val s = codeOnly(src(
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/BrowserUseOffloadHandler.kt"))
        assertTrue(s.contains("val joined = resolveMaybeRelative(cookiesFile, cwd)"))
        assertTrue(s.contains("PRootKernel.resolvePathForSession(sessionId, joined, app)"))
        assertFalse(s.contains("resolvePathForSession(sessionId, cookiesFile, app)"))
        assertTrue(s.contains("val tokens = resolveMaybeRelativeAll("))
        assertTrue(s.contains("buildInputJson(args, request.sessionId, request.cwd)"))
    }

    @Test fun probeFastioSingleSharedHelper() {
        val s = codeOnly(src(
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/FastioOffloadHandler.kt"))
        assertTrue(s.contains("val isTarCommand = firstSubcommandToken(rawArgv) == \"tar\""))
        assertTrue(s.contains("if (firstSubcommandToken(argv) != \"tar\") return argv"))
        assertFalse(s.contains("firstOrNull() == \"tar\""))
        assertFalse(s.contains("firstOrNull() != \"tar\""))
        assertTrue(s.contains("booleanFlags = fastioBooleanFlags(isTarCommand)"))
    }

    @Test fun probeBaselineShapesWouldBeRed() {
        // the fixed-tree markers must NOT be present in the BASELINE shapes:
        // a mirror of the old code must fail the same assertions the fixed
        // tree passes, so this test is its own negative control.
        val oldBrowserUse = """
            val host = PRootKernel.resolvePathForSession(sessionId, cookiesFile, app)
        """.trimIndent()
        val s = codeOnly(oldBrowserUse)
        assertFalse(s.contains("val joined = resolveMaybeRelative(cookiesFile, cwd)"))
        assertTrue(s.contains("resolvePathForSession(sessionId, cookiesFile, app)"))
    }
}

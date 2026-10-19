package com.rikkaminis.app.sandbox.offload

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * deep-audit-1017 ISSUE-08 — `android-shizuku-cli` argv handling, three defects.
 *
 * ISSUE-08a `input text`: the encoder emitted a bare `%` for spaces, but AOSP
 * `InputShellCommand.sendText` only rewrites `%s` (and passes every other `%`
 * through), so every space was typed as a literal `%`. Ground truth read from
 * the platform: `probe/pre/InputShellCommand.java` (android15-release, :371-386)
 * plus `/system/bin/input` = `cmd input "$@"`.
 *
 * ISSUE-08b `notification channel`: pkg/channelId were read out of the **raw**
 * argv tail, so any flag placed before them shifted the arguments —
 * `channel set --importance 3 com.pkg chid` read `--importance` as the package
 * and `--block com.pkg chid` fed `--block` to `cmd notification` as the target.
 * `--block`/`--unblock` were also undeclared in the handler's booleanFlags, so
 * the parser ate the package as their value. The fix reads
 * `args.positional.drop(1)` (the convention `permission appops` / `input swipe`
 * already use) and declares the two switches — the contrast below pins both the
 * declaration and the indexing, the way SessionsCliArgsTest pins B26a.
 *
 * ISSUE-08c `package clear --cache-only`: the global `pm trim-caches 1G` ran
 * while the payload still said `cleared=<pkg>`. The device's `pm help clear`
 * documents `clear [--user USER_ID] [--cache-only] PACKAGE` (per-package), and
 * AOSP `PackageManagerShellCommand.runClear` routes the flag to
 * `deleteApplicationCacheFilesAsUser`.
 *
 * ISSUE-07 `exec` option scope (deep-audit-1029): the trailing tokens of `exec`
 * are a *shell command*, not an option list, but they were handed to the
 * top-level parser verbatim. `android-shizuku-cli exec grep -q foo /p` therefore
 * read `-q` as the output-shaping `--quiet`, and `OffloadOutput.formatBody`
 * answered a non-zero exit by returning only the `error` object — dropping the
 * `data` (stdout/stderr/exitCode) that `handleExec` documents it keeps. The
 * mirror image: `--timeout-ms N`, which `EXEC_HELP` advertises and
 * `handleExec` reads from the parser, was instead glued into the `sh -c`
 * command line and executed. The fix splits the two at the argv layer
 * (`ShizukuArgv.execOptionTokens`): the parser sees the CLI's own tokens, the
 * shell sees everything else. The contrast arms below pin both directions.
 */
class ShizukuCliArgsTest {

    // ── ISSUE-08a: `input text` space encoding ──

    /** Expected value is the literal the platform rewrites, not derived from it. */
    @Test
    fun inputText_encodesSpaceAsPercentS() {
        assertEquals("hello%sworld", encodeInputText("hello world"))
    }

    @Test
    fun inputText_keepsQuotesStripped_andMultispace() {
        assertEquals("its%sfine", encodeInputText("it's fine"))
        assertEquals("a%sb%sc", encodeInputText("a b c"))
    }

    @Test
    fun inputText_leavesTextWithoutSpacesAlone() {
        assertEquals("no-spaces", encodeInputText("no-spaces"))
    }

    /**
     * Pre-fix shape kept honest: `replace(' ', '%')` yields a `%` that the
     * platform's `%s` scanner does not consume, i.e. a literal `%` is typed.
     * Asserted structurally (no `%s` pair in the output) so it holds even
     * without the platform.
     */
    @Test
    fun oldEncoder_yieldsLiteralPercentThatPlatformDoesNotConsume() {
        val old = "hello world".replace(' ', '%').replace("'", "")
        assertEquals("hello%world", old)
        assertFalse("old encoder produced an escape the platform would consume", old.contains("%s"))
    }

    // ── ISSUE-08b: `notification channel` argument positions ──

    private fun channelPos(argv: List<String>) =
        OffloadArgs(argv, booleanFlags = ShizukuOffloadHandler.NO_VALUE_FLAGS)
            .positional.drop(1)

    /** The declaration under test must cover the switches the help text lists. */
    @Test
    fun noValueFlags_coversChannelSwitches_andExcludesImportance() {
        assertTrue(ShizukuOffloadHandler.NO_VALUE_FLAGS.contains("block"))
        assertTrue(ShizukuOffloadHandler.NO_VALUE_FLAGS.contains("unblock"))
        // --importance takes a value: declaring it would orphan "3".
        assertFalse(ShizukuOffloadHandler.NO_VALUE_FLAGS.contains("importance"))
    }

    @Test
    fun channelSet_flagFirst_keepsPackageInPlace() {
        val args = OffloadArgs(
            listOf("channel", "set", "--block", "com.pkg", "chid"),
            booleanFlags = ShizukuOffloadHandler.NO_VALUE_FLAGS,
        )
        assertTrue(args.hasFlag("block"))
        val pos = args.positional.drop(1)
        assertEquals(listOf("set", "com.pkg", "chid"), pos)
        assertEquals("com.pkg", pos[1])
        assertEquals("chid", pos[2])
    }

    @Test
    fun channelSet_documentedOrder_stillParses() {
        val args = OffloadArgs(
            listOf("channel", "set", "com.pkg", "chid", "--block"),
            booleanFlags = ShizukuOffloadHandler.NO_VALUE_FLAGS,
        )
        assertTrue(args.hasFlag("block"))
        assertEquals(listOf("set", "com.pkg", "chid"), args.positional.drop(1))
    }

    @Test
    fun channelSet_importanceBeforePackage_keepsPackageInPlace() {
        val args = OffloadArgs(
            listOf("channel", "set", "--importance", "3", "com.pkg", "chid"),
            booleanFlags = ShizukuOffloadHandler.NO_VALUE_FLAGS,
        )
        assertEquals("3", args.get("importance"))
        assertEquals(listOf("set", "com.pkg", "chid"), args.positional.drop(1))
    }

    /**
     * Pre-fix declaration set (no booleanFlags): `--block` greedily eats the
     * package and the raw-argv indexing the old code used pointed at the switch
     * itself — that pair is the defect, reproduced here.
     */
    @Test
    fun oldDeclaration_swallowsPackageAsBlockValue() {
        val args = OffloadArgs(listOf("channel", "set", "--block", "com.pkg", "chid"))
        assertEquals("com.pkg", args.get("block"))
        assertFalse(args.hasFlag("block"))
        assertEquals(listOf("set", "chid"), args.positional.drop(1))
        // raw argv tail (what the old code indexed): [set, --block, com.pkg, chid]
        assertEquals("--block", listOf("set", "--block", "com.pkg", "chid")[1])
    }

    @Test
    fun channelList_compactFlagBeforePackage_keepsPackageInPlace() {
        val raw = listOf("channel", "list", "--compact", "com.pkg")
        // old indexing (raw tail after the "channel" sub): "--compact"
        assertEquals("--compact", raw.drop(1)[1])
        // new indexing (parsed positionals): "com.pkg"
        assertEquals("com.pkg", channelPos(raw)[1])
    }

    // ── ISSUE-08c: `package clear --cache-only` ──

    @Test
    fun packageClear_cacheOnly_isPerPackage_notGlobalTrim() {
        val cmd = buildPackageClearCommand("com.pkg", cacheOnly = true, user = null)
        assertEquals(listOf("pm", "clear", "--cache-only", "com.pkg"), cmd)
        assertFalse("global trim must not stand in for a per-package clear", cmd.contains("trim-caches"))
    }

    @Test
    fun packageClear_fullClear_unchangedShape() {
        assertEquals(
            listOf("pm", "clear", "com.pkg"),
            buildPackageClearCommand("com.pkg", cacheOnly = false, user = null),
        )
    }

    @Test
    fun packageClear_userOptionPrecedesPackage() {
        assertEquals(
            listOf("pm", "clear", "--cache-only", "--user", "0", "com.pkg"),
            buildPackageClearCommand("com.pkg", cacheOnly = true, user = "0"),
        )
    }

    @Test
    fun packageClear_payloadScopeIsHonest() {
        assertEquals("cache-only", clearScope(true))
        assertEquals("data+cache", clearScope(false))
    }

    // ── ISSUE-07: `exec` payload vs the CLI's own options ──

    /** The parser set the fixed wiring feeds for an `exec` invocation. */
    private fun execParserTokens(argv: List<String>) =
        OffloadArgs(
            splitShizukuArgv(argv).execOptionTokens,
            booleanFlags = ShizukuOffloadHandler.NO_VALUE_FLAGS,
        )

    /**
     * Fissure A — a sub-command's `-q` stays in the shell payload. Expectation
     * written as the literal argv tail, not as "whatever trailing holds".
     */
    @Test
    fun execSubcommandQuietFlag_staysInTheShellPayload() {
        val split = splitShizukuArgv(listOf("exec", "grep", "-q", "foo", "/p"))
        assertEquals("exec", split.group)
        assertEquals(listOf("grep", "-q", "foo", "/p"), split.trailing)
        assertEquals(emptyList<String>(), split.execOptionTokens)
    }

    @Test
    fun execQuietFlag_noLongerReachesTheOutputShaper() {
        val argv = listOf("exec", "grep", "-q", "foo", "/p")
        assertFalse(execParserTokens(argv).hasFlag("q", "quiet"))
        // Pre-fix shape: the whole argv minus the group went to the parser, so
        // `-q` became --quiet. Reproduced, not asserted as a wish.
        val old = OffloadArgs(
            splitShizukuArgv(argv).withoutGroup,
            booleanFlags = ShizukuOffloadHandler.NO_VALUE_FLAGS,
        )
        assertTrue(old.hasFlag("q", "quiet"))
    }

    /**
     * Fissure A, end to end through the shipped formatter: with the pre-fix
     * parser set the failure body loses `data`; with the fixed set it keeps it.
     */
    @Test
    fun execFailureBody_keepsData() {
        val envelope = JSONObject()
            .put("ok", false)
            .put(
                "error",
                JSONObject().put("code", "OPERATION_FAILED").put("message", "exec `grep -q foo /p`"),
            )
            .put("data", JSONObject().put("stdout", "out\n").put("exitCode", 1))
            .toString(2)
        val argv = listOf("exec", "grep", "-q", "foo", "/p")
        val fixed = OffloadOutput.formatBody(envelope, execParserTokens(argv))
        assertTrue(fixed.contains("\"stdout\""))
        assertTrue(fixed.contains("\"exitCode\""))

        val old = OffloadArgs(
            splitShizukuArgv(argv).withoutGroup,
            booleanFlags = ShizukuOffloadHandler.NO_VALUE_FLAGS,
        )
        val stripped = OffloadOutput.formatBody(envelope, old)
        assertFalse(stripped.contains("\"stdout\""))
        assertTrue(stripped.contains("OPERATION_FAILED"))
    }

    /** Fissure B — `--timeout-ms N` is the CLI's, not the shell's. */
    @Test
    fun execTrailingTimeoutOption_isClaimedByTheCli() {
        val split = splitShizukuArgv(listOf("exec", "ls", "/p", "--timeout-ms", "5000"))
        assertEquals(listOf("ls", "/p"), split.trailing)
        assertEquals(listOf("--timeout-ms", "5000"), split.execOptionTokens)
        assertEquals("5000", execParserTokens(listOf("exec", "ls", "--timeout-ms", "5000")).get("timeout-ms"))
    }

    @Test
    fun execTrailingTimeoutOption_equalsForm() {
        val argv = listOf("exec", "cmd", "--timeout-ms=2500")
        val split = splitShizukuArgv(argv)
        assertEquals(listOf("cmd"), split.trailing)
        assertEquals(listOf("--timeout-ms=2500"), split.execOptionTokens)
        assertEquals("2500", execParserTokens(argv).get("timeout-ms"))
    }

    /** Only a well-formed value is claimed; anything else reaches the shell. */
    @Test
    fun execTimeoutOption_nonNumericValueStaysInThePayload() {
        val split = splitShizukuArgv(listOf("exec", "mytool", "--timeout-ms", "fast"))
        assertEquals(listOf("mytool", "--timeout-ms", "fast"), split.trailing)
        assertEquals(emptyList<String>(), split.execOptionTokens)
        assertEquals(null, execParserTokens(listOf("exec", "mytool", "--timeout-ms", "fast")).get("timeout-ms"))
    }

    /** Regression anchor: a leading top-level option must not be swept along. */
    @Test
    fun execLeadingTopLevelOption_survives() {
        val argv = listOf("--compact", "exec", "ls")
        val split = splitShizukuArgv(argv)
        assertEquals("exec", split.group)
        assertEquals(listOf("ls"), split.trailing)
        assertEquals(listOf("--compact"), split.execOptionTokens)
        assertTrue(execParserTokens(argv).hasFlag("compact"))
    }

    /**
     * Non-exec groups are untouched by ISSUE-07: the parser still receives the
     * whole argv minus the group. `execOptionTokens` is an exec-only slot — for
     * other groups it mirrors the leading options (`--compact`) and no caller
     * reads it; the parser set is `withoutGroup`.
     */
    @Test
    fun nonExecGroup_parserSetIsUnchanged() {
        val split = splitShizukuArgv(listOf("--compact", "package", "list"))
        assertEquals("package", split.group)
        assertEquals(listOf("--compact", "list"), split.withoutGroup)
        assertEquals(listOf("list"), split.trailing)
        assertEquals(listOf("--compact"), split.execOptionTokens)
    }
}

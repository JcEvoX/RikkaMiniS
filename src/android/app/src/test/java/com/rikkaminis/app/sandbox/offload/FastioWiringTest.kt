package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Wiring probes for `minis-fastio` ([T-minis-fastio]).
 *
 * The edges this covers cannot be exercised on the JVM: registering a handler
 * with [com.rikkaminis.app.sandbox.NativeOffloadServer] needs an Application,
 * the permission registry needs SharedPreferences, and the agent-facing tool
 * line lives inside the system prompt. There is no Robolectric here, so the
 * wiring is asserted against the compiled sources themselves — the same probe
 * shape as `AppInitSkipGuardWiringTest` / `DatabaseVersionGuardTest`.
 *
 * Every expected string is a literal. Each probe has a negative control that
 * mutates a copy of the real source and asserts the very same check fails —
 * without one, a deleted line would show up as a green test that checks
 * nothing.
 */
class FastioWiringTest {

    private val catalogFile = "src/main/java/com/rikkaminis/app/sandbox/OffloadHandlerCatalog.kt"
    private val appFile = "src/main/java/com/rikkaminis/app/MinisApp.kt"
    private val permissionFile = "src/main/java/com/rikkaminis/app/offload/OffloadPermissionManager.kt"
    private val handlerFile = "src/main/java/com/rikkaminis/app/sandbox/offload/FastioOffloadHandler.kt"
    private val terminalFile = "src/main/java/com/rikkaminis/app/sandbox/TerminalSession.kt"
    private val promptFile = "src/main/java/com/rikkaminis/app/ui/chat/ChatPromptAndTools.kt"

    // ── the probes, as functions so a mutated copy can be run through them ──

    /** Catalog + registration + permission + agent-visible tool line. */
    private fun assertWired(catalog: String, app: String, permission: String, prompt: String) {
        assertTrue(
            "minis-fastio must be in the offload handler catalog (that list is what " +
                "generates the PATH stub and the --native-offload allowlist)",
            catalog.contains("\"minis-fastio\","),
        )
        assertTrue(
            "MinisApp must register the handler instance",
            app.contains("NativeOffloadServer.register(\"minis-fastio\", FastioOffloadHandler(this))"),
        )
        assertTrue(
            "MinisApp must import the handler",
            app.contains("import com.rikkaminis.app.sandbox.offload.FastioOffloadHandler"),
        )
        assertTrue(
            "the destructive path must be gated ASK_ONCE (not BYPASS)",
            permission.contains(
                "ToolPermissionInfo(\"fastio_rm\", \"minis-fastio (delete)\", " +
                    "PermissionCategory.SYSTEM, PermissionLevel.ASK_ONCE)",
            ),
        )
        // Phase 2: one row per write primitive — trusting deletion must not
        // silently authorise overwriting a different file.
        assertTrue(
            "cp must have its own ASK_ONCE row",
            permission.contains(
                "ToolPermissionInfo(\"fastio_cp\", \"minis-fastio (copy)\", " +
                    "PermissionCategory.SYSTEM, PermissionLevel.ASK_ONCE)",
            ),
        )
        assertTrue(
            "mv must have its own ASK_ONCE row",
            permission.contains(
                "ToolPermissionInfo(\"fastio_mv\", \"minis-fastio (move)\", " +
                    "PermissionCategory.SYSTEM, PermissionLevel.ASK_ONCE)",
            ),
        )
        // Phase 3: the archive primitive is a write in BOTH directions.
        assertTrue(
            "tar must have its own ASK_ONCE row",
            permission.contains(
                "ToolPermissionInfo(\"fastio_tar\", \"minis-fastio (archive)\", " +
                    "PermissionCategory.SYSTEM, PermissionLevel.ASK_ONCE)",
            ),
        )
        assertTrue(
            "the agent must be told the tool exists, or the handler is dead weight",
            prompt.contains("minis-fastio du <path>...") && prompt.contains("minis-fastio rm [-r] <path>..."),
        )
        assertTrue(
            "the agent must be told about the phase-2 primitives too, or they are dead weight",
            prompt.contains("minis-fastio find <path>") &&
                prompt.contains("minis-fastio grep <pattern>") &&
                prompt.contains("minis-fastio cp [-r] <src> <dst>") &&
                prompt.contains("minis-fastio mv <src> <dst>"),
        )
        assertTrue(
            "the agent must be told about the phase-3 archive primitive too, and that its " +
                "extract side is tar-slip guarded, or it will shell out to `tar` instead",
            prompt.contains("minis-fastio tar -cf <a.tar> <path>...") &&
                prompt.contains("minis-fastio tar -xf <a.tar> [-C dir]") &&
                prompt.contains("tar-slip"),
        )
    }

    /** The gate must be the only gate call, inside `rm`, ahead of any delete. */
    private fun assertGatePrecedesDelete(handler: String) {
        val rmIdx = handler.indexOf("private fun rm(")
        val duIdx = handler.indexOf("private fun du(")
        val gateIdx = handler.indexOf("OffloadGate.enforce(")
        val deleteIdx = handler.indexOf("deleteTree(path")
        assertTrue("rm(...) must exist", rmIdx >= 0)
        assertTrue("du(...) must exist", duIdx >= 0)
        assertTrue(
            "du is read-only and must not gate (a prompt for a directory walk is pure friction)",
            gateIdx > rmIdx,
        )
        assertTrue("the gate call must exist", gateIdx >= 0)
        assertTrue("the gate must run before the first delete", gateIdx < deleteIdx)
    }

    /**
     * `rm` must survive the argv shapes an agent actually types. Both checks
     * exist because of measured failures (verify-fastio-0930/ArgsProbe): `-rf`
     * landed as the single flag "rf" so the directory was refused, and
     * `--recursive /tmp/x` swallowed the path because the option was not
     * declared boolean.
     */
    private fun assertRmFlagHandling(handler: String) {
        assertTrue(
            "rm must accept the combined short flags an agent types (`rm -rf`)",
            handler.contains("args.hasFlag(\"r\", \"recursive\", \"rf\", \"fr\", \"R\")"),
        )
        assertTrue(
            "`--recursive` / `--force` / `--ignore-case` must be declared boolean flags, or " +
                "OffloadArgs eats the following path as the option's value",
            handler.contains("\"recursive\", \"force\", \"ignore-case\","),
        )
    }

    /**
     * The PTY builds its own session-scoped `-b` table from [sessionId], so the
     * env var the offload handlers read must carry the SAME id. Without it
     * `minis-fastio du /var/minis/workspace` reports an empty tree (rootfs
     * placeholder) while `ls` in that same terminal lists the real session dir.
     */
    private fun assertTerminalForwardsSessionId(terminal: String) {
        assertTrue(
            "the terminal must pass its session id into the env builder",
            terminal.contains("buildTermuxEnv(rootfsManager, sessionId)"),
        )
        assertTrue(
            "the env builder must export MINIS_CHAT_SESSION_ID",
            terminal.contains("envMap[\"MINIS_CHAT_SESSION_ID\"] = sessionId"),
        )
    }

    /**
     * The write primitives must be gated, and the read-only ones must not.
     * `du`/`find`/`grep` grant nothing `file_read` already has, so a prompt for
     * them would be pure friction — the same rule phase 1 pinned for `du`.
     */
    private fun assertWriteGates(handler: String) {
        val cpGate = handler.indexOf("OffloadGate.enforce(CP_TOOL_NAME")
        val mvGate = handler.indexOf("OffloadGate.enforce(MV_TOOL_NAME")
        val copyIdx = handler.indexOf("copyTree(srcPath, target, force, outcome)")
        val moveIdx = handler.indexOf("moveTree(srcPath, target, force, outcome)")
        assertTrue("cp must gate", cpGate >= 0)
        assertTrue("mv must gate", mvGate >= 0)
        assertTrue("the copy must run after cp's gate", copyIdx > cpGate)
        assertTrue("the move must run after mv's gate", moveIdx > mvGate)
        assertTrue(
            "mv must hand `force` down to the engine: a rename without REPLACE_EXISTING " +
                "fails on an existing target, so `mv --force` would report a failure for " +
                "the one thing it promises to do",
            moveIdx >= 0,
        )
        val tarBody = bodyOf(handler, "tar")
        val tarGate = tarBody.indexOf("OffloadGate.enforce(TAR_TOOL_NAME")
        assertTrue("tar must gate", tarGate >= 0)
        assertTrue(
            "the archive must be created after tar's gate",
            tarBody.indexOf("tarCreateInto(") > tarGate,
        )
        assertTrue(
            "the archive must be expanded after tar's gate",
            tarBody.indexOf("tarExtractFrom(") > tarGate,
        )
        assertTrue("find is read-only and must not gate", !bodyOf(handler, "find").contains("OffloadGate"))
        assertTrue("grep is read-only and must not gate", !bodyOf(handler, "grep").contains("OffloadGate"))
    }

    /**
     * `find`-style single-dash long options must be rewritten before OffloadArgs
     * sees them: the shared parser reads any `-word` as a short FLAG, so
     * `find /usr -name "*.log"` would otherwise set a flag called `name` and
     * pass the glob as a positional PATH — a silently wrong result.
     */
    private fun assertFindOptionSpelling(handler: String) {
        assertTrue(
            "`-name` / `-type` / `-limit` must be rewritten to the `--` spelling",
            handler.contains("\"-name\", \"-type\", \"-limit\", \"-max-entries\", \"-ignore-case\","),
        )
        assertTrue(
            "the rewrite must happen before OffloadArgs is constructed",
            handler.indexOf("normalizeLongOptionSpelling(request.argv.drop(1))") <
                handler.indexOf("booleanFlags = setOf("),
        )
    }

    /**
     * `tar` needs its own expander because OffloadArgs reads any `-word` as ONE
     * flag: `-czf out.tar src` would otherwise arrive as a flag literally named
     * "czf", the archive would be read as a MEMBER, and the command would fail
     * with a confusing "missing <archive>". It must stay scoped to `tar`,
     * because `-rf` has to keep its meaning for `rm`/`cp`.
     */
    private fun assertTarOptionSpelling(handler: String) {
        assertTrue(
            "the `tar` cluster expander must exist",
            handler.contains("private fun normalizeTarShorts("),
        )
        assertTrue(
            "it must be scoped to the tar subcommand, or `rm -rf` loses its meaning",
            handler.contains("if (argv.firstOrNull() != \"tar\") return argv"),
        )
        assertTrue(
            "it must run before OffloadArgs is constructed",
            handler.indexOf("normalizeTarShorts(request.argv.drop(1))") <
                handler.indexOf("booleanFlags = setOf("),
        )
        assertTrue(
            "`--create` / `--gzip` / `--verbose` must be boolean, or they eat the next token " +
                "(the archive path)",
            handler.contains("\"create\", \"extract\", \"gzip\", \"verbose\","),
        )
        assertTrue(
            "`tar -f` must NOT be mapped to `--file`: its value has to stay a positional, " +
                "and `--key value` would swallow it into `values`",
            handler.contains("'f' -> Unit   // the archive value stays a positional"),
        )
    }

    /** Source text of one private member function (up to the next one / section). */
    private fun bodyOf(handler: String, funName: String): String {
        val start = handler.indexOf("private fun $funName(")
        assertTrue("$funName(...) must exist", start >= 0)
        val candidates = listOf(
            handler.indexOf("\n    private fun ", start + 1),
            handler.indexOf("\n    // ──", start + 1),
        ).filter { it > 0 }
        val end = candidates.minOrNull() ?: handler.length
        return handler.substring(start, end)
    }

    // ── probes ──────────────────────────────────────────────────────────────

    @Test
    fun `handler is cataloged, registered, gated and documented`() {
        assertWired(source(catalogFile), source(appFile), source(permissionFile), source(promptFile))
    }

    @Test
    fun `rm gates before deleting and du never gates`() {
        assertGatePrecedesDelete(source(handlerFile))
    }

    @Test
    fun `write primitives gate and read-only primitives never do`() {
        assertWriteGates(source(handlerFile))
    }

    @Test
    fun `find-style single-dash options are rewritten before parsing`() {
        assertFindOptionSpelling(source(handlerFile))
    }

    @Test
    fun `the help text documents every primitive, the dialect and the refusals`() {
        val handler = source(handlerFile)
        for (expected in listOf(
            "minis-fastio du <path>...",
            "minis-fastio find <path> [opts]",
            "minis-fastio grep <pattern> <path>...",
            "minis-fastio rm [-r] <path>...",
            "minis-fastio cp [-r] <src> <dst>",
            "minis-fastio mv <src> <dst>",
            "minis-fastio tar -cf <a.tar> <path>...",
            "minis-fastio tar -xf <a.tar> [-C dir]",
            "tar_slip",
            "java.util.regex",
            "user-mounted external folders",
            "bind-mount roots",
        )) {
            assertTrue("help text must mention '$expected'", handler.contains(expected))
        }
    }

    @Test
    fun `tar clusters are expanded before parsing and only for tar`() {
        assertTarOptionSpelling(source(handlerFile))
    }

    @Test
    fun `rm accepts the flag spellings an agent actually types`() {
        assertRmFlagHandling(source(handlerFile))
    }

    @Test
    fun `the interactive terminal forwards its session id to the shell`() {
        assertTerminalForwardsSessionId(source(terminalFile))
    }

    // ── negative controls ───────────────────────────────────────────────────

    @Test
    fun `negative control - deleting the gate line makes the gate probe fail`() {
        val original = source(handlerFile)
        val mutated = original.replace(
            "OffloadGate.enforce(TOOL_NAME, DISPLAY_NAME, args, request)?.let { return it }",
            "",
        )
        assertTrue("the gate line must exist to be removable", mutated != original)

        var failed = false
        try {
            assertGatePrecedesDelete(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the gate probe must fail once the gate call is gone", failed)
    }

    @Test
    fun `negative control - downgrading the permission to BYPASS makes the wiring probe fail`() {
        val original = source(permissionFile)
        val mutated = original.replace(
            "ToolPermissionInfo(\"fastio_rm\", \"minis-fastio (delete)\", " +
                "PermissionCategory.SYSTEM, PermissionLevel.ASK_ONCE)",
            "ToolPermissionInfo(\"fastio_rm\", \"minis-fastio (delete)\", " +
                "PermissionCategory.SYSTEM, PermissionLevel.BYPASS)",
        )
        assertTrue("the ASK_ONCE entry must exist to be mutated", mutated != original)

        var failed = false
        try {
            assertWired(source(catalogFile), source(appFile), mutated, source(promptFile))
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the wiring probe must fail on a BYPASS default", failed)
    }

    @Test
    fun `negative control - dropping the catalog entry makes the wiring probe fail`() {
        val original = source(catalogFile)
        val mutated = original.replace("        \"minis-fastio\",\n", "")
        assertTrue("the catalog entry must exist to be removable", mutated != original)

        var failed = false
        try {
            assertWired(mutated, source(appFile), source(permissionFile), source(promptFile))
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the wiring probe must fail without the catalog entry", failed)
    }

    @Test
    fun `negative control - dropping the combined-flag spelling makes the flag probe fail`() {
        val original = source(handlerFile)
        val mutated = original.replace(
            "args.hasFlag(\"r\", \"recursive\", \"rf\", \"fr\", \"R\")",
            "args.hasFlag(\"r\", \"recursive\")",
        )
        assertTrue("the combined-flag list must exist to be mutated", mutated != original)

        var failed = false
        try {
            assertRmFlagHandling(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the flag probe must fail once `-rf` is no longer accepted", failed)
    }

    @Test
    fun `negative control - dropping the terminal env injection makes the session-id probe fail`() {
        val original = source(terminalFile)
        val mutated = original.replace(
            "envMap[\"MINIS_CHAT_SESSION_ID\"] = sessionId",
            "",
        )
        assertTrue("the env injection must exist to be removed", mutated != original)

        var failed = false
        try {
            assertTerminalForwardsSessionId(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the session-id probe must fail once the export is gone", failed)
    }

    @Test
    fun `negative control - dropping the cp permission row makes the wiring probe fail`() {
        val original = source(permissionFile)
        val mutated = original.replace(
            "ToolPermissionInfo(\"fastio_cp\", \"minis-fastio (copy)\", " +
                "PermissionCategory.SYSTEM, PermissionLevel.ASK_ONCE)",
            "",
        )
        assertTrue("the cp row must exist to be removable", mutated != original)

        var failed = false
        try {
            assertWired(source(catalogFile), source(appFile), mutated, source(promptFile))
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the wiring probe must fail without the cp permission row", failed)
    }

    @Test
    fun `negative control - ungating cp makes the write-gate probe fail`() {
        val original = source(handlerFile)
        val mutated = original.replace(
            "OffloadGate.enforce(CP_TOOL_NAME, CP_DISPLAY_NAME, args, request)?.let { return it }",
            "",
        )
        assertTrue("the cp gate must exist to be removable", mutated != original)

        var failed = false
        try {
            assertWriteGates(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the write-gate probe must fail once cp is ungated", failed)
    }

    @Test
    fun `negative control - gating find makes the write-gate probe fail`() {
        val original = source(handlerFile)
        val mutated = original.replace(
            "    private fun find(",
            "    private fun find(\n        @Suppress(\"UNUSED\") gated: Boolean = OffloadGate.allow(\"x\", \"x\"),",
        )
        assertTrue("find(...) must exist to be mutated", mutated != original)

        var failed = false
        try {
            assertWriteGates(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the write-gate probe must fail once find gates", failed)
    }

    @Test
    fun `negative control - dropping the -name rewrite makes the option probe fail`() {
        val original = source(handlerFile)
        val mutated = original.replace(
            "\"-name\", \"-type\", \"-limit\", \"-max-entries\", \"-ignore-case\",",
            "\"-type\", \"-limit\", \"-max-entries\", \"-ignore-case\",",
        )
        assertTrue("the alias list must exist to be mutated", mutated != original)

        var failed = false
        try {
            assertFindOptionSpelling(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the option probe must fail once `-name` is no longer rewritten", failed)
    }

    @Test
    fun `negative control - dropping the tar permission row makes the wiring probe fail`() {
        val original = source(permissionFile)
        val mutated = original.replace(
            "ToolPermissionInfo(\"fastio_tar\", \"minis-fastio (archive)\", " +
                "PermissionCategory.SYSTEM, PermissionLevel.ASK_ONCE)",
            "",
        )
        assertTrue("the tar row must exist to be removable", mutated != original)

        var failed = false
        try {
            assertWired(source(catalogFile), source(appFile), mutated, source(promptFile))
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the wiring probe must fail without the tar permission row", failed)
    }

    @Test
    fun `negative control - unscoping the tar expander makes the spelling probe fail`() {
        val original = source(handlerFile)
        val mutated = original.replace(
            "if (argv.firstOrNull() != \"tar\") return argv",
            "",
        )
        assertTrue("the subcommand guard must exist to be removable", mutated != original)

        var failed = false
        try {
            assertTarOptionSpelling(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue(
            "the spelling probe must fail once the expander applies to every subcommand",
            failed,
        )
    }

    @Test
    fun `negative control - removing tar's gate makes the write-gate probe fail`() {
        val original = source(handlerFile)
        val mutated = original.replace(
            "OffloadGate.enforce(TAR_TOOL_NAME, TAR_DISPLAY_NAME, args, request)?.let { return it }",
            "",
        )
        assertTrue("tar's gate line must exist to be removable", mutated != original)

        var failed = false
        try {
            assertWriteGates(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the write-gate probe must fail once tar no longer gates", failed)
    }

    // ── source location (same technique as DatabaseVersionGuardTest) ─────────

    private fun source(rel: String): String = sourceFile(rel).readText()

    private fun sourceFile(rel: String): File {
        val viaAndroid = "src/android/app/$rel"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel)
            if (File(d, viaAndroid).isFile) return File(d, viaAndroid)
            dir = d.parentFile
        }
        error("'$rel' not found from ${File(".").absoluteFile}")
    }
}

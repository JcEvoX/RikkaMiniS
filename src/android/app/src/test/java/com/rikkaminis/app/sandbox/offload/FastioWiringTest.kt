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
        assertTrue(
            "the agent must be told the tool exists, or the handler is dead weight",
            prompt.contains("minis-fastio du <path>...") && prompt.contains("minis-fastio rm [-r] <path>..."),
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
            "`--recursive` / `--force` must be declared boolean flags, or OffloadArgs " +
                "eats the following path as the option's value",
            handler.contains("booleanFlags = setOf(\"recursive\", \"force\")"),
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
    fun `the help text documents both primitives and the refusals`() {
        val handler = source(handlerFile)
        for (expected in listOf(
            "minis-fastio du <path>...",
            "minis-fastio rm [-r] <path>...",
            "user-mounted external folders",
            "bind-mount roots",
        )) {
            assertTrue("help text must mention '$expected'", handler.contains(expected))
        }
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

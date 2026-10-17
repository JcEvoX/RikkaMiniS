package com.rikkaminis.app.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.util.concurrent.ConcurrentHashMap

/**
 * [fix/da1024-H] ISSUE-08: a permission-level change must reach the in-memory
 * session caches, and the settings screen must re-read the level afterwards.
 *
 * Two layers, because the two halves cannot be exercised the same way on the JVM:
 *
 *  1. **Behaviour** — the "which sessions does this tool touch" decision is a
 *     pure function ([OffloadPermissionManager.sessionsContaining] /
 *     [OffloadPermissionManager.clearToolFrom]) taking a plain map, so the
 *     precise semantics ("only this tool, other tools survive, other sessions
 *     survive") are asserted against real maps. [OffloadPermissionManager.setLevel]
 *     and `resetAll` themselves need an Android `Context` (SharedPreferences),
 *     which this source set has no Robolectric for.
 *  2. **Wiring** — the remaining half is asserted against the compiled sources
 *     themselves, the same probe shape as `FastioWiringTest` /
 *     `AppInitSkipGuardWiringTest`: which calls the two mutators make, in what
 *     order, and how the two rows key their state. Every expected string is a
 *     literal, and each probe has a negative control that mutates a copy of the
 *     real source and asserts the very same probe fails — without one, a
 *     deleted line would show up as a green test that checks nothing.
 */
class OffloadPermissionResetTest {

    private val managerFile = "src/main/java/com/rikkaminis/app/offload/OffloadPermissionManager.kt"
    private val screenFile = "src/main/java/com/rikkaminis/app/ui/settings/OffloadPermissionScreen.kt"

    private fun names(vararg entries: String): MutableSet<String> =
        ConcurrentHashMap.newKeySet<String>().apply { addAll(entries) }

    // ── behaviour: the pure selection + removal step ─────────────────────────

    @Test
    fun `sessionsContaining returns exactly the sessions holding that tool`() {
        val sessions: Map<String, Set<String>> = mapOf(
            "chat-1" to names("fastio_rm", "a11y_cli"),
            "chat-2" to names("a11y_cli"),
            "offload-global" to names("fastio_rm"),
        )
        assertEquals(setOf("chat-1", "offload-global"), OffloadPermissionManager.sessionsContaining(sessions, "fastio_rm"))
        assertEquals(setOf("chat-1", "chat-2"), OffloadPermissionManager.sessionsContaining(sessions, "a11y_cli"))
        assertEquals(emptySet<String>(), OffloadPermissionManager.sessionsContaining(sessions, "shizuku_cli"))
        assertEquals(emptySet<String>(), OffloadPermissionManager.sessionsContaining(emptyMap(), "fastio_rm"))
    }

    @Test
    fun `clearToolFrom removes only the named tool and leaves every other entry alone`() {
        val cache: MutableMap<String, MutableSet<String>> = mutableMapOf(
            "chat-1" to names("fastio_rm", "a11y_cli"),
            "chat-2" to names("a11y_cli"),
            "offload-global" to names("fastio_rm"),
        )

        OffloadPermissionManager.clearToolFrom(cache, "fastio_rm")

        // the named tool is gone everywhere …
        assertEquals(setOf("a11y_cli"), cache["chat-1"])
        assertEquals(0, cache["offload-global"]!!.size)
        // … the OTHER tool's verdict survives (a level change must not re-prompt
        // for tools the user never touched) …
        assertEquals(setOf("a11y_cli"), cache["chat-2"])
        // … and no session is dropped entirely: a session's other grants are
        // still keyed under it.
        assertEquals(setOf("chat-1", "chat-2", "offload-global"), cache.keys)
    }

    @Test
    fun `clearToolFrom with an unknown tool is a no-op`() {
        val cache: MutableMap<String, MutableSet<String>> = mutableMapOf(
            "chat-1" to names("a11y_cli"),
        )
        OffloadPermissionManager.clearToolFrom(cache, "fastio_rm")
        assertEquals(setOf("a11y_cli"), cache["chat-1"])
    }

    /**
     * The counter is a LEVEL-MUTATION signal, not a cache-touch signal: the two
     * extracted cache helpers must be callable (and are called) without waking
     * every settings row, otherwise an unrelated cache sweep would invalidate
     * the row state for no reason.
     */
    @Test
    fun `the session-cache helpers do not move the revision signal`() {
        val before = OffloadPermissionManager.revision.value
        OffloadPermissionManager.clearAllSessionGrants()
        OffloadPermissionManager.clearToolSessionState("fastio_rm")
        assertEquals("only setLevel/resetAll may bump the revision", before, OffloadPermissionManager.revision.value)
    }

    // ── wiring: what the two mutators call, and in what order ────────────────

    private fun assertResetAllClearsEverySession(manager: String) {
        val resetAll = bodyOf(manager, "fun resetAll(")
        assertTrue("resetAll(...) must exist", resetAll.isNotEmpty())
        assertTrue(
            "resetAll must clear the in-memory session caches, or a cached verdict " +
                "outlives the reset and silently allows/refuses the next call",
            resetAll.contains("clearAllSessionGrants()"),
        )
        assertTrue("resetAll must bump the revision signal", resetAll.contains("bumpRevision()"))
        val clearAll = bodyOf(manager, "fun clearAllSessionGrants(")
        assertTrue("clearAllSessionGrants(...) must exist", clearAll.isNotEmpty())
        assertTrue(
            "clearAllSessionGrants must clear sessionGrants",
            clearAll.contains("sessionGrants.clear()"),
        )
        assertTrue(
            "clearAllSessionGrants must clear sessionDenials",
            clearAll.contains("sessionDenials.clear()"),
        )
        // Ordering matters: a collector woken by the bump re-reads getLevel(),
        // so the prefs mutation has to be visible before the signal moves.
        assertTrue(
            "resetAll must persist/remove the levels BEFORE bumping the revision",
            resetAll.indexOf("apply()") in 0 until resetAll.indexOf("bumpRevision()"),
        )
    }

    private fun assertSetLevelClearsThatToolOnly(manager: String) {
        val setLevel = bodyOf(manager, "fun setLevel(")
        assertTrue("setLevel(...) must exist", setLevel.isNotEmpty())
        assertTrue(
            "setLevel must drop that tool's cached verdict",
            setLevel.contains("clearToolSessionState(toolName)"),
        )
        assertTrue("setLevel must bump the revision signal", setLevel.contains("bumpRevision()"))
        assertTrue(
            "setLevel must persist the level BEFORE bumping the revision",
            setLevel.indexOf("apply()") in 0 until setLevel.indexOf("bumpRevision()"),
        )
        assertFalse(
            "setLevel must NOT clear every tool's cache — grants for untouched tools " +
                "are still valid verdicts and the user would be re-prompted for nothing",
            setLevel.contains("sessionGrants.clear()") || setLevel.contains("sessionDenials.clear()"),
        )
        val clearTool = bodyOf(manager, "fun clearToolSessionState(")
        assertTrue("clearToolSessionState(...) must exist", clearTool.isNotEmpty())
        assertTrue(
            "clearToolSessionState must cover sessionGrants",
            clearTool.contains("sessionGrants"),
        )
        assertTrue(
            "clearToolSessionState must cover sessionDenials",
            clearTool.contains("sessionDenials"),
        )
    }

    private fun assertRowsKeyedOnRevision(screen: String) {
        assertFalse(
            "OffloadPermissionScreen must not keep a keyless remember for the level — " +
                "it freezes the first-composition value, so Reset All leaves every chip stale",
            screen.contains("remember { mutableStateOf(OffloadPermissionManager.getLevel"),
        )
        assertTrue(
            "both rows must subscribe to the manager's revision signal",
            screen.contains("val revision by OffloadPermissionManager.revision.collectAsState()"),
        )
        assertTrue(
            "AgentPolicyRow must key its level on (toolName, revision)",
            screen.contains(
                "remember(toolName, revision) { mutableStateOf(OffloadPermissionManager.getLevel(toolName)) }",
            ),
        )
        assertTrue(
            "PermissionRow must key its level on (tool.toolName, revision)",
            screen.contains(
                "remember(tool.toolName, revision) { mutableStateOf(OffloadPermissionManager.getLevel(tool.toolName)) }",
            ),
        )
        assertEquals(
            "both rows own a level state — expected two subscriptions",
            2, Regex("val revision by OffloadPermissionManager\\.revision\\.collectAsState\\(\\)").findAll(screen).count(),
        )
    }

    // ── probes ───────────────────────────────────────────────────────────────

    @Test
    fun `resetAll and setLevel clear the session caches and bump the revision`() {
        val manager = source(managerFile)
        assertResetAllClearsEverySession(manager)
        assertSetLevelClearsThatToolOnly(manager)
    }

    @Test
    fun `OffloadPermissionScreen keys row level state on OffloadPermissionManager revision`() {
        assertRowsKeyedOnRevision(source(screenFile))
    }

    // ── negative controls ────────────────────────────────────────────────────

    @Test
    fun `negative control - resetAll without the cache clear fails the probe`() {
        val mutated = source(managerFile).replace("        clearAllSessionGrants()\n", "")
        assertTrue("the clearAllSessionGrants() call must exist to be removable", mutated != source(managerFile))
        assertProbeFails { assertResetAllClearsEverySession(mutated) }
    }

    @Test
    fun `negative control - an emptied clearAllSessionGrants helper fails the probe`() {
        val mutated = source(managerFile).replace(
            "    internal fun clearAllSessionGrants() {\n" +
                "        sessionGrants.clear()\n" +
                "        sessionDenials.clear()\n" +
                "    }",
            "    internal fun clearAllSessionGrants() {\n    }",
        )
        assertTrue("the helper body must exist to be emptied", mutated != source(managerFile))
        assertProbeFails { assertResetAllClearsEverySession(mutated) }
    }

    @Test
    fun `negative control - resetAll without the revision bump fails the probe`() {
        val original = source(managerFile)
        val resetAll = bodyOf(original, "fun resetAll(")
        val mutated = original.replace(resetAll, resetAll.replace("        bumpRevision()\n", ""))
        assertTrue("the bump must exist to be removable", mutated != original)
        assertProbeFails { assertResetAllClearsEverySession(mutated) }
    }

    @Test
    fun `negative control - setLevel that ignores the session cache fails the probe`() {
        val original = source(managerFile)
        val mutated = original.replace("        clearToolSessionState(toolName)\n", "")
        assertTrue("the clear call must exist to be removable", mutated != original)
        assertProbeFails { assertSetLevelClearsThatToolOnly(mutated) }
    }

    @Test
    fun `negative control - setLevel that clears every tool fails the probe`() {
        val original = source(managerFile)
        val setLevel = bodyOf(original, "fun setLevel(")
        val mutated = original.replace(
            setLevel,
            setLevel.replace("clearToolSessionState(toolName)", "clearAllSessionGrants()"),
        )
        assertTrue("the per-tool call must exist to be replaced", mutated != original)
        assertProbeFails { assertSetLevelClearsThatToolOnly(mutated) }
    }

    @Test
    fun `negative control - a keyless remember in the screen fails the probe`() {
        val original = source(screenFile)
        val mutated = original.replace(
            "    var currentLevel by remember(toolName, revision) " +
                "{ mutableStateOf(OffloadPermissionManager.getLevel(toolName)) }",
            "    var currentLevel by remember " +
                "{ mutableStateOf(OffloadPermissionManager.getLevel(toolName)) }",
        )
        assertTrue("the keyed remember must exist to be de-keyed", mutated != original)
        assertProbeFails { assertRowsKeyedOnRevision(mutated) }
    }

    @Test
    fun `negative control - a row without the revision subscription fails the probe`() {
        val original = source(screenFile)
        val mutated = original.replace(
            "    val revision by OffloadPermissionManager.revision.collectAsState()\n" +
                "    var currentLevel by remember(tool.toolName, revision) " +
                "{ mutableStateOf(OffloadPermissionManager.getLevel(tool.toolName)) }",
            "    var currentLevel by remember { mutableStateOf(OffloadPermissionManager.getLevel(tool.toolName)) }",
        )
        assertTrue("the second subscription must exist to be removable", mutated != original)
        assertProbeFails { assertRowsKeyedOnRevision(mutated) }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private fun assertProbeFails(probe: () -> Unit) {
        var failed = false
        try {
            probe()
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the probe must fail on the mutated source", failed)
    }

    /** Braces-balanced body of a function whose declaration contains [header]. */
    private fun bodyOf(src: String, header: String): String {
        val start = src.indexOf(header)
        if (start < 0) return ""
        val open = src.indexOf("{", start)
        if (open < 0) return ""
        var depth = 0
        var i = open
        while (i < src.length) {
            when (src[i]) {
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) return src.substring(open, i + 1)
                }
            }
            i++
        }
        return ""
    }

    // ── source location (same technique as FastioWiringTest) ─────────────────

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

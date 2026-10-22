package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.data.repository.mergeSessionMcpEnablement
import com.rikkaminis.app.data.repository.mergeSessionSkillEnablement
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [dv17-4] — deep-audit-1038 B-05 (P1), then [dv22-B4] — deep-audit-1042 B-04.
 *
 * `ChatInputArea`'s slash-menu list was computed unconditionally:
 *
 *     val filteredSlashCommands = remember(
 *         showSlashMenu,
 *         viewModel.slashFilter.collectAsState().value,
 *         viewModel.memoryEnabled.collectAsState().value,
 *         viewModel.thinkingLevel.collectAsState().value,   // 0 reads in the body
 *     ) { viewModel.filteredSlashCommands() }
 *     if (showSlashMenu && filteredSlashCommands.isNotEmpty()) { … }
 *
 * dv17-4 fixed the gate + the key set. dv22-B4 found the residues that shape
 * could not fix (all still in the SAME five lines):
 *
 * 1. **The compute never left the composition.** A synchronous `remember`
 *    runs on the composition (Main) thread — every keystroke with the menu
 *    open re-ran the full query burst inline. The list is now loaded in a
 *    `produceState` producer whose body hops to `Dispatchers.IO` before the
 *    call (produceState's producer inherits the composition context — the
 *    hop is explicit, 1041-B3).
 * 2. **N+M per-item queries.** The list cost one `db.rawQuery` PER SKILL plus
 *    one PER MCP SERVER (`isEnabledForSession`) on every producer run. The
 *    per-session resolution is now the batched `sessionEnablementMap` — ONE
 *    `IN (…)` SELECT per repository, precedence identical (override row →
 *    global → false) via the pure [mergeSessionSkillEnablement] /
 *    [mergeSessionMcpEnablement] cores.
 * 3. **No invalidation channel.** `setSessionOverride` writes only the DB
 *    table — the `.skills` / `.servers` flows never re-emitted, so an open
 *    menu went stale after a toggle in SessionSkillsSheet /
 *    SessionMcpsSheet. Both repositories now publish
 *    `sessionOverridesRevision` (bumped by every override-table write) and
 *    the producer keys on both revisions — a refresh on toggle, no polling.
 *
 * The screens are Compose + repository (not JVM-instantiable), so the wiring
 * contracts are pinned by source-shape assertions — the approach this repo
 * already uses (`SkillsMcpReloadOffMainTest`, `ThinkingRulesRememberKeyTest`).
 * The two pure merge cores ARE JVM-testable and get real behavioural tests
 * with hardcoded literal expectations and a counting lambda, never derived
 * from the code under test, so a mutation cannot masquerade as PASS.
 *
 * The gating criterion binds the DECISION POINT (is the call reachable with
 * `showSlashMenu == false`?) rather than one spelling: an `if (showSlashMenu)`
 * wrapper and an early-exit on `!showSlashMenu` both satisfy it.
 */
class ChatSlashCommandMemoTest {

    private val inputArea: String by lazy {
        sourceFile("src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatInputArea.kt")
    }
    private val slashExt: String by lazy {
        sourceFile("src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatViewModelSlashExt.kt")
    }
    private val skillRepo: String by lazy {
        sourceFile("src/android/app/src/main/java/com/rikkaminis/app/data/repository/SkillRepository.kt")
    }
    private val mcpRepo: String by lazy {
        sourceFile("src/android/app/src/main/java/com/rikkaminis/app/data/repository/MCPRepository.kt")
    }

    // ── the producer keys (dv17-4 set + the two dv22-B4 revision channels) ──

    @Test
    fun `producer keys are exactly the live sources of the load it guards`() {
        assertEquals(
            "the producer must key on every source `filteredSlashCommands()` reads at " +
                "load time — the filter text, the memory switch, the session scope " +
                "(the per-session override is resolved from it), both repository " +
                "lists the picker walks, AND both repositories' " +
                "sessionOverridesRevision: the override table is not mirrored by the " +
                "list flows, so without the revision keys an open menu goes stale " +
                "after a session toggle (B-04). `showSlashMenu` is deliberately NOT a " +
                "key: it is the guard now, so keying on it would be dead weight.",
            setOf(
                "viewModel.slashFilter.collectAsState().value",
                "viewModel.memoryEnabled.collectAsState().value",
                "viewModel.currentSessionId",
                "viewModel.skillRepository?.skills?.collectAsState()?.value",
                "viewModel.mcpRepository?.servers?.collectAsState()?.value",
                "viewModel.skillRepository?.sessionOverridesRevision?.collectAsState()?.value",
                "viewModel.mcpRepository?.sessionOverridesRevision?.collectAsState()?.value",
            ),
            producerKeys(codeOnly(inputArea)),
        )
    }

    @Test
    fun `no key for a source the producer body never reads`() {
        val keys = producerKeys(codeOnly(inputArea))
        assertTrue(
            "`thinkingLevel` has 0 reads inside `filteredSlashCommands()` — the " +
                "/thinking row left availableSlashCommands. Keying on it made every " +
                "thinking-notch tap recompute the list and re-run the query burst",
            keys.none { it.contains("thinkingLevel") },
        )
    }

    @Test
    fun `producer is keyed at all`() {
        assertTrue(
            "a keyless producer would freeze the list on the first load",
            producerKeys(codeOnly(inputArea)).isNotEmpty(),
        )
    }

    // ── the load is asynchronous and leaves the main thread (dv22-B4) ───────

    @Test
    fun `the menu compute runs inside the producer not at composition time`() {
        val code = codeOnly(inputArea)
        val lambda = producerLambda(code)
        assertTrue(
            "`$CALL` must run INSIDE the produceState/LaunchedEffect trailing " +
                "lambda — a composition-synchronous compute (the dv17-4 remember " +
                "shape) pays the query burst on the Main thread on every keystroke " +
                "with the menu open",
            lambda.contains(CALL),
        )
        assertEquals(
            "every `$CALL` occurrence must sit inside the producer — a second, " +
                "composition-level call site (a remember regression beside the " +
                "producer) pays the query burst on Main",
            1,
            code.split(CALL).size - 1,
        )
    }

    @Test
    fun `the producer hops to Dispatchers IO before the call`() {
        val lambda = producerLambda(codeOnly(inputArea))
        val callAt = lambda.indexOf(CALL)
        assertTrue("$CALL missing from the producer body", callAt >= 0)
        assertTrue(
            "produceState's producer inherits the composition's coroutine context " +
                "(Main) — produceState is NOT an IO context (1041-B3). Without a " +
                "preceding withContext(Dispatchers.IO) the async shape is cosmetic: " +
                "the batched queries still run on the main thread",
            lambda.substring(0, callAt).contains("withContext(Dispatchers.IO)"),
        )
    }

    // ── the gate (dv17-4, unchanged decision) ───────────────────────────────

    @Test
    fun `every filteredSlashCommands call is gated by showSlashMenu`() {
        val code = codeOnly(inputArea)
        var occurrences = 0
        var probe = code.indexOf(CALL)
        while (probe >= 0) {
            occurrences++
            probe = code.indexOf(CALL, probe + CALL.length)
        }
        assertEquals(
            "expected exactly one `$CALL` call site; a second one must be gated too",
            1,
            occurrences,
        )
        var idx = code.indexOf(CALL)
        while (idx >= 0) {
            // The guard may sit in the enclosing `if`, or in an early-exit
            // statement just above — both are legal, so the window reaches back
            // past the payload declaration into the preceding statement. 1500
            // covers the async producer sitting between the guard and the call.
            val window = code.substring(maxOf(0, idx - GATE_WINDOW), idx)
            assertTrue(
                "`$CALL` runs ungated — the list is only ever rendered under " +
                    "`showSlashMenu`, so every composition (session switch, " +
                    "thinking-notch tap, unrelated recompose) would pay the " +
                    "query burst on IO for a value the next line drops",
                window.contains("if (showSlashMenu)") || window.contains("if (!showSlashMenu)"),
            )
            idx = code.indexOf(CALL, idx + CALL.length)
        }
    }

    // ── the batched query (dv22-B4): constant count, not N+M ────────────────

    @Test
    fun `the ext body resolves session enablement through the batched map`() {
        val body = declarationBody(
            codeOnly(slashExt),
            "internal fun ChatViewModel.filteredSlashCommands()",
        )
        for (token in listOf("activeSessionId", "skillRepository", "mcpRepository", "sessionEnablementMap")) {
            assertTrue(
                "filteredSlashCommands() no longer reads `$token`; if the body changed, " +
                    "the producer key set in ChatInputArea must be re-derived to match",
                body.contains(token),
            )
        }
        assertFalse(
            "filteredSlashCommands() still calls the PER-ITEM isEnabledForSession( — " +
                "one query per row is N+M queries per producer run; the per-session " +
                "resolution must go through the batched sessionEnablementMap",
            body.contains("isEnabledForSession("),
        )
    }

    @Test
    fun `the ext body makes exactly one batched call per repository`() {
        val body = declarationBody(
            codeOnly(slashExt),
            "internal fun ChatViewModel.filteredSlashCommands()",
        )
        assertEquals(
            "expected exactly TWO sessionEnablementMap call sites (one per " +
                "repository) — a per-item call loop here is exactly the N+M shape",
            2,
            body.split("sessionEnablementMap").size - 1,
        )
    }

    @Test
    fun `the repository batched reader is one chunked IN query not a per-item loop`() {
        for (label in listOf("SkillRepository" to skillRepo, "MCPRepository" to mcpRepo)) {
            val body = declarationBody(codeOnly(label.second), "fun sessionEnablementMap(")
            assertEquals(
                "$label.first: sessionEnablementMap must issue exactly ONE rawQuery " +
                    "(per chunked batch) — more than one means a per-item loop is back",
                1,
                body.split("rawQuery").size - 1,
            )
            assertTrue(
                "$label.first: the batched reader must chunk the id list — one " +
                    "IN (…) placeholder per id runs into SQLITE_MAX_VARIABLE_NUMBER",
                body.contains("chunked(") && body.contains(" IN ("),
            )
        }
    }

    // ── the invalidation channel (dv22-B4): writes publish, no polling ──────

    @Test
    fun `every override table write bumps the published revision`() {
        for (label in listOf("SkillRepository" to skillRepo, "MCPRepository" to mcpRepo)) {
            val code = codeOnly(label.second)
            assertTrue(
                "$label.first must publish `sessionOverridesRevision` — the override " +
                    "table is not mirrored by the list flows, so without this channel " +
                    "async consumers cannot see a write",
                code.contains("val sessionOverridesRevision"),
            )
            for (mutator in listOf(
                "fun setSessionOverride(",
                "fun clearSessionOverrides(",
                "fun renameSessionOverrides(",
            )) {
                val body = declarationBody(code, mutator)
                assertTrue(
                    "$label.first.$mutator writes the session-override table without " +
                        "bumping _sessionOverridesRevision — the table moved but no " +
                        "consumer can ever see it (the B-04 staleness)",
                    body.contains("_sessionOverridesRevision.value ="),
                )
            }
        }
    }

    // ── the loading placeholder (dv22-B4 prescription) ───────────────────────

    @Test
    fun `the first-load placeholder is wired`() {
        val code = codeOnly(inputArea)
        for (literal in listOf(
            "SlashMenuLoadingPanel",
            "R.string.common_loading",
            "loaded = true",
        )) {
            assertTrue(
                "the async load needs its placeholder (dv22-B4): `$literal` — the " +
                    "first open is one IO round-trip; without the placeholder the " +
                    "popup condition degenerates back to data-presence only",
                code.contains(literal),
            )
        }
    }

    // ── the pure merge cores: precedence + counting-lambda (JVM, real) ──────

    @Test
    fun `merge precedence is override row then global then false`() {
        val globals = mapOf("a" to true, "b" to false)
        val merged = mergeSessionSkillEnablement(
            ids = listOf("a", "b", "c", "d"),
            overrideRows = mapOf("a" to false, "c" to true),
            globalEnabled = { id -> globals[id] },
        )
        // "a": override(false) beats global(true); "b": no override → global(false);
        // "c": override(true) beats absent global; "d": neither → false.
        assertEquals(mapOf("a" to false, "b" to false, "c" to true, "d" to false), merged)
    }

    @Test
    fun `merge reads globals only for ids the batch did not answer`() {
        var globalLookups = 0
        mergeSessionSkillEnablement(
            ids = listOf("a", "b", "c", "d", "e"),
            overrideRows = mapOf("a" to true, "c" to false, "e" to true),
            globalEnabled = {
                globalLookups++
                null
            },
        )
        // 5 ids, 3 answered by the ONE batched SELECT → the in-memory global
        // list is consulted for exactly the 2 leftovers, never per-row.
        assertEquals(2, globalLookups)
    }

    @Test
    fun `the mcp twin merges with the identical precedence`() {
        val merged = mergeSessionMcpEnablement(
            ids = listOf("x", "y"),
            overrideRows = mapOf("x" to true),
            globalEnabled = { id -> if (id == "y") true else null },
        )
        assertEquals(mapOf("x" to true, "y" to true), merged)
    }

    // ── regression controls: the fix must not disturb its neighbours ────────

    @Test
    fun `thinking picker wiring is untouched`() {
        val code = inputArea
        for (literal in listOf(
            "val effectiveThinkingLevelState by viewModel.thinkingLevel.collectAsState()",
            "val requestedThinkingLevelState by viewModel.requestedThinkingLevel.collectAsState()",
        )) {
            assertTrue(
                "thinking_wiring_guard.py pins the picker's two DIFFERENT sources " +
                    "(`$literal`) — collapsing them kills the capped-by-model up-arrow",
                code.contains(literal),
            )
        }
        assertTrue(
            "the [T-thinking-effective-level] rationale comment must survive — it is " +
                "the only record of why `requested` may not be fed the effective value",
            code.contains("[T-thinking-effective-level]"),
        )
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /**
     * Args + trailing-lambda of the producer that follows the
     * `val slashMenuPayload` declaration. Paren-balanced (the keys contain
     * nested `collectAsState()` calls); the trailing lambda lives AFTER the
     * arg list's closing paren, so the body is brace-balanced from the first
     * `{` after it.
     */
    private fun producerLambda(code: String): String {
        val decl = code.indexOf(PAYLOAD_DECL)
        assertTrue("`$PAYLOAD_DECL` not found", decl >= 0)
        val pm = Regex("\\b(produceState|LaunchedEffect)\\s*\\(").find(code.substring(decl))
        assertTrue("no produceState/LaunchedEffect after `$PAYLOAD_DECL`", pm != null)
        val open = decl + pm!!.range.first + pm.value.length - 1
        var depth = 0
        var i = open
        var close = -1
        while (i < code.length) {
            when (code[i]) {
                '(' -> depth++
                ')' -> {
                    depth--
                    if (depth == 0) { close = i; break }
                }
            }
            i++
        }
        assertTrue("unbalanced producer argument list", close > 0)
        var j = close + 1
        while (j < code.length && code[j] != '{') j++
        assertTrue("the producer has no trailing lambda", j < code.length)
        var bdepth = 0
        var k = j
        while (k < code.length) {
            when (code[k]) {
                '{' -> bdepth++
                '}' -> {
                    bdepth--
                    if (bdepth == 0) return code.substring(j + 1, k)
                }
            }
            k++
        }
        throw AssertionError("unbalanced producer lambda")
    }

    /** Key list of the producer call: comma-separated entries of the arg list,
     *  minus the `initialValue = …` argument (it is a value, not a key). */
    private fun producerKeys(code: String): Set<String> {
        val decl = code.indexOf(PAYLOAD_DECL)
        assertTrue("`$PAYLOAD_DECL` not found", decl >= 0)
        val pm = Regex("\\b(produceState|LaunchedEffect)\\s*\\(").find(code.substring(decl))
        assertTrue("no produceState/LaunchedEffect after `$PAYLOAD_DECL`", pm != null)
        val open = decl + pm!!.range.first + pm.value.length - 1
        var depth = 0
        var i = open
        while (i < code.length) {
            when (code[i]) {
                '(' -> depth++
                ')' -> {
                    depth--
                    if (depth == 0) break
                }
            }
            i++
        }
        assertTrue("unbalanced producer argument list", depth == 0)
        return code.substring(open + 1, i)
            .split(",")
            .map { it.trim() }
            .filter { it.isNotEmpty() && !it.startsWith("initialValue") }
            .toSet()
    }

    /** Brace-balanced body of the declaration starting at `marker`. */
    private fun declarationBody(code: String, marker: String): String {
        val start = code.indexOf(marker)
        assertTrue("marker not found: $marker", start >= 0)
        val bodyOpen = code.indexOf('{', start)
        assertTrue("no body after: $marker", bodyOpen >= 0)
        var depth = 0
        var i = bodyOpen
        while (i < code.length) {
            when (code[i]) {
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) return code.substring(bodyOpen, i + 1)
                }
            }
            i++
        }
        throw AssertionError("unbalanced body for: $marker")
    }

    /** Blank `//`-only lines (the repo's `codeOnly` convention) so a prose
     *  mention of a guard cannot satisfy a window check. */
    private fun codeOnly(src: String): String = src
        .split("\n")
        .map { if (it.trimStart().startsWith("//")) " ".repeat(it.length) else it }
        .joinToString("\n")

    private fun sourceFile(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel).readText()
            dir = d.parentFile
        }
        // CI runs unit tests with the module as the working directory; the
        // sandbox harness runs them from the repo root. Accept both.
        dir = File(".").absoluteFile
        val nested = "src/android/$rel"
        while (dir != null) {
            val d = dir
            if (File(d, nested).isFile) return File(d, nested).readText()
            dir = d.parentFile
        }
        throw AssertionError("'$rel' not found from ${File(".").absoluteFile}")
    }

    private companion object {
        const val PAYLOAD_DECL = "val slashMenuPayload"
        const val CALL = "viewModel.filteredSlashCommands()"
        const val GATE_WINDOW = 1500
    }
}

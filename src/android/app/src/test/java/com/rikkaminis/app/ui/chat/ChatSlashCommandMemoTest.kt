package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [dv17-4] — deep-audit-1038 B-05 (P1).
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
 * Three defects in five lines:
 *
 * 1. **No short-circuit.** The gate `showSlashMenu` was a KEY, not a guard, so
 *    the memo ran on every composition (entering the chat, switching sessions,
 *    every thinking-notch tap) and the result was dropped on the next line.
 *    `filteredSlashCommands()` costs one synchronous `db.rawQuery` per skill
 *    plus one per MCP server (`SkillRepository.isEnabledForSession` /
 *    `MCPRepository.isEnabledForSession`) — on the composition (Main) thread,
 *    for a menu that is not on screen.
 * 2. **A dead key.** `viewModel.thinkingLevel` has 0 reads inside the lambda
 *    (`/thinking` left `availableSlashCommands`), so the key only bought a
 *    recompute — and the query burst above — on every thinking-notch tap.
 * 3. **The live sources were missing.** The body reads `activeSessionId` and
 *    both repositories' lists; with them absent from the keys, an open menu
 *    kept showing the previous session's per-session skill/MCP enablement and
 *    never refreshed when an entry was toggled or a skill appeared on disk.
 *
 * The screens are Compose + repository (not JVM-instantiable), so the contracts
 * are pinned by source-shape assertions — the approach this repo already uses
 * (`SkillsMcpReloadOffMainTest`, `ThinkingRulesRememberKeyTest`). Expected
 * values are hardcoded literals, never derived from the code under test, so a
 * mutation cannot masquerade as PASS.
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

    // ── the keys ────────────────────────────────────────────────────────────

    @Test
    fun `memo keys are exactly the live sources of the call it guards`() {
        assertEquals(
            "the memo must key on every source `filteredSlashCommands()` reads at " +
                "call time — the filter text, the memory switch, the session scope " +
                "(the per-session override is resolved from it) and both repository " +
                "lists the picker walks. `showSlashMenu` is deliberately NOT a key: " +
                "it is the guard now, so keying on it would be dead weight.",
            setOf(
                "viewModel.slashFilter.collectAsState().value",
                "viewModel.memoryEnabled.collectAsState().value",
                "viewModel.currentSessionId",
                "viewModel.skillRepository?.skills?.collectAsState()?.value",
                "viewModel.mcpRepository?.servers?.collectAsState()?.value",
            ),
            memoKeys(codeOnly(inputArea)),
        )
    }

    @Test
    fun `no key for a source the memo body never reads`() {
        val keys = memoKeys(codeOnly(inputArea))
        assertTrue(
            "`thinkingLevel` has 0 reads inside `filteredSlashCommands()` — the " +
                "/thinking row left availableSlashCommands. Keying on it made every " +
                "thinking-notch tap recompute the list and re-run the N+M " +
                "isEnabledForSession queries",
            keys.none { it.contains("thinkingLevel") },
        )
    }

    @Test
    fun `memo is keyed at all`() {
        assertTrue(
            "a keyless remember would freeze the list on the first composition",
            memoKeys(codeOnly(inputArea)).isNotEmpty(),
        )
    }

    // ── the gate ────────────────────────────────────────────────────────────

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
            // past the memo declaration into the preceding statement.
            val window = code.substring(maxOf(0, idx - GATE_WINDOW), idx)
            assertTrue(
                "`$CALL` runs ungated — the list is only ever rendered under " +
                    "`showSlashMenu`, so every composition (session switch, " +
                    "thinking-notch tap, unrelated recompose) pays one synchronous " +
                    "db.rawQuery per skill and per MCP server on the composition " +
                    "thread for a value the next line drops",
                window.contains("if (showSlashMenu)") || window.contains("if (!showSlashMenu)"),
            )
            idx = code.indexOf(CALL, idx + CALL.length)
        }
    }

    // ── why the keys must carry the session scope (paired-source guard) ──────

    @Test
    fun `the memo body really reads the session scope and both repositories`() {
        val body = declarationBody(codeOnly(slashExt), "internal fun ChatViewModel.filteredSlashCommands()")
        for (token in listOf("activeSessionId", "skillRepository", "mcpRepository", "isEnabledForSession")) {
            assertTrue(
                "filteredSlashCommands() no longer reads `$token`; if the body changed, " +
                    "the memo key set in ChatInputArea must be re-derived to match",
                body.contains(token),
            )
        }
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

    /** Key list of the `remember(…)` that follows the `val filteredSlashCommands`
     *  declaration — paren-balanced, because the keys contain nested calls. */
    private fun memoKeys(code: String): Set<String> {
        val decl = code.indexOf(MEMO_DECL)
        assertTrue("`$MEMO_DECL` not found", decl >= 0)
        val rm = code.indexOf("remember", decl)
        assertTrue("no `remember(` after `$MEMO_DECL`", rm >= 0)
        val open = code.indexOf('(', rm)
        assertTrue("no key list after `remember`", open >= 0)
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
        assertTrue("unbalanced key list", depth == 0)
        return code.substring(open + 1, i)
            .split(",")
            .map { it.trim() }
            .filter { it.isNotEmpty() }
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
        const val MEMO_DECL = "val filteredSlashCommands"
        const val CALL = "viewModel.filteredSlashCommands()"
        const val GATE_WINDOW = 1500
    }
}

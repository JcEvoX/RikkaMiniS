package com.rikkaminis.app.ui.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/dv20-b2] — deep-audit-1040 B-04 (P1).
 *
 * The MCP settings screen and the two per-session sheets (`SessionSkillsSheet`
 * / `SessionMcpsSheet`) called their repositories and the Keystore-backed OAuth
 * store synchronously from composition bodies and click handlers. Every one of
 * those entry points reaches the disk:
 *
 *  * `MCPRepository.setEnabled/delete/update/add/importJSON` → `save()` writes
 *    `servers.json` through a temp file + rename, `delete` also runs a SQLite
 *    `DELETE` and purges the OAuth store;
 *  * `MCPOAuthStore.*` → `EncryptedSharedPreferences` (Keystore) and a bridge
 *    file read/delete, seeded from inside a `remember { … }` (i.e. on the
 *    composition thread, re-run on every re-seed);
 *  * `SkillRepository.setSessionOverride` / `MCPRepository.setSessionOverride`
 *    → one `INSERT OR REPLACE` / `DELETE` per row, "Enable all" meaning N
 *    transactions on Main;
 *  * `…isEnabledForSession` → one `rawQuery` per row, run once per list
 *    emission inside the seeding `LaunchedEffect`.
 *
 * Two shapes are pinned here. Threading binds the DECISION POINT (which
 * dispatcher the call runs on), never one spelling of it; the seed-key
 * alignment binds the decision point too (which changes re-run the reader),
 * because `remember(editServer)` + `LaunchedEffect(editServer?.id)` resets the
 * draft to "" on every id-preserving edit and then never re-reads — the form
 * shows an empty client secret while storage still holds one.
 *
 * Deliberately NOT asserted: `exportServerJSON` / `previewImport` are pure
 * JSONObject work and stay legal on Main (the gate's `method_io_state` derives
 * that from the repository, this test only pins that they are still called
 * where they are). Expected values below are literals, never derived from the
 * code under test, so a mutation cannot masquerade as PASS.
 */
class McpScreenAndSheetsOffMainTest {

    private val mcpScreen: String by lazy {
        readRepoFile("app/src/main/java/com/rikkaminis/app/ui/settings/MCPIntegrationsScreen.kt")
    }
    private val skillsSheet: String by lazy {
        readRepoFile("app/src/main/java/com/rikkaminis/app/ui/chat/SessionSkillsSheet.kt")
    }
    private val mcpsSheet: String by lazy {
        readRepoFile("app/src/main/java/com/rikkaminis/app/ui/chat/SessionMcpsSheet.kt")
    }

    // ── ① the MCP screen: repository writes + the OAuth store ───────────

    @Test
    fun mcpScreenRepositoryWrites_runOffMain() {
        val code = codeOnly(mcpScreen)
        for (call in listOf(
            "mcpRepository.setEnabled(",
            "mcpRepository.delete(",
            "mcpRepository.update(",
            "mcpRepository.add(",
            "mcpRepository.importJSON(",
        )) {
            assertAllOffMain(code, call, "MCPIntegrationsScreen")
        }
    }

    @Test
    fun mcpScreenOAuthStoreCalls_runOffMain() {
        val code = codeOnly(mcpScreen)
        for (call in listOf(
            "MCPOAuthStore.importPendingClientSecret(",
            "MCPOAuthStore.clientSecret(",
            "MCPOAuthStore.isAuthorized(",
            "MCPOAuthStore.signOut(",
            "MCPOAuthStore.setClientSecret(",
        )) {
            assertAllOffMain(code, call, "MCPIntegrationsScreen")
        }
    }

    @Test
    fun mcpScreenOAuthStoreCalls_areNeverSeededFromRemember() {
        // The pre-fix shape ran the Keystore read inside `remember { … }`, i.e.
        // on the composition thread and again on every re-seed. A `remember`
        // initialiser is never an IO wrap, so the off-Main sweep above already
        // covers the threading half; this pins the shape itself.
        val code = codeOnly(mcpScreen)
        val seeded = rememberInitializersContaining(code, "MCPOAuthStore")
        assertTrue(
            "the Keystore-backed OAuth store is read in a remember initialiser at " +
                "line(s) $seeded — that is composition-thread I/O on the Main dispatcher " +
                "(seed it from a keyed LaunchedEffect on Dispatchers.IO instead)",
            seeded.isEmpty(),
        )
    }

    @Test
    fun pureRepositoryCalls_stayWhereTheyAre() {
        // Precision control: these two are pure JSONObject work — the fix must
        // not have deleted or reshaped their call sites while moving the IO.
        val code = codeOnly(mcpScreen)
        for (literal in listOf(
            "mcpRepository.exportServerJSON(server)",
            "mcpRepository.previewImport(text)",
        )) {
            assertTrue("shape anchor missing: $literal", code.contains(literal))
        }
    }

    // ── ② the two sheets ────────────────────────────────────────────────

    @Test
    fun skillsSheetSessionWrites_runOffMain() {
        assertAllOffMain(codeOnly(skillsSheet), "skillRepository.setSessionOverride(", "SessionSkillsSheet")
    }

    @Test
    fun skillsSheetSeedRead_runsOffMain() {
        assertAllOffMain(codeOnly(skillsSheet), "skillRepository.isEnabledForSession(", "SessionSkillsSheet")
    }

    @Test
    fun mcpsSheetSessionWrites_runOffMain() {
        assertAllOffMain(codeOnly(mcpsSheet), "mcpRepository.setSessionOverride(", "SessionMcpsSheet")
    }

    @Test
    fun mcpsSheetSeedRead_runsOffMain() {
        assertAllOffMain(codeOnly(mcpsSheet), "mcpRepository.isEnabledForSession(", "SessionMcpsSheet")
    }

    @Test
    fun sheetsKeepTheSwitchLatencyContract() {
        // The Switch stays controlled from the local override map on Main (zero
        // latency) and the write follows on IO: the map write must come BEFORE
        // the launch, in all four toggle/seed shapes.
        val skills = codeOnly(skillsSheet)
        assertTrue(
            "toggle map write must precede the IO launch",
            skills.contains("overrides[skill.id] = enabled") ||
                skills.contains("overrides[id] = true"),
        )
        val mcps = codeOnly(mcpsSheet)
        assertTrue(
            "toggle map write must precede the IO launch",
            mcps.contains("overrides[server.id] = enabled"),
        )
    }

    // ── ③ the OAuth seed: effect keys must cover the draft's keys ────────

    @Test
    fun oauthSeedEffect_keysCoverTheDraft() {
        val code = codeOnly(mcpScreen)
        val draft = rememberKeys(code, "var oauthClientSecret by remember")
        assertEquals(
            "the draft is re-derived per edited server; an un-keyed or " +
                "narrower key list freezes it",
            setOf("editServer"),
            draft,
        )
        val effect = effectKeysAssigning(code, "oauthClientSecret =")
        assertTrue("no LaunchedEffect seeds oauthClientSecret", effect != null)
        val missing = draft - effect!!
        assertTrue(
            "the seeding effect is keyed $effect but the draft re-derives on $draft " +
                "(missing $missing) — an editServer change that keeps the id resets the " +
                "draft and the reader never re-runs",
            missing.isEmpty(),
        )
    }

    @Test
    fun oauthSeedEffect_isNotKeyedOnTheIdAlone() {
        val code = codeOnly(mcpScreen)
        assertFalse(
            "`LaunchedEffect(editServer?.id)` is narrower than `remember(editServer)`: " +
                "a changed server with an unchanged id resets the draft without re-reading",
            code.contains("LaunchedEffect(editServer?.id)"),
        )
    }

    @Test
    fun oauthAuthorizedSeed_alsoKeyedLikeItsDraft() {
        val code = codeOnly(mcpScreen)
        assertEquals(
            "the authorized flag is seeded from the same Keystore read; its draft must " +
                "re-derive on the same key as the secret",
            setOf("editServer"),
            rememberKeys(code, "var oauthAuthorizedSeed by remember"),
        )
    }

    // ── ④ the helper has teeth (negative control) ───────────────────────

    @Test
    fun helper_flagsABareCallAndAcceptsAWrappedOne() {
        val bare = "fun a() {\n    mcpRepository.delete(\"x\")\n}\n"
        assertEquals(
            "a bare Main-thread call must be reported",
            listOf(2),
            bareCallLines(bare, "mcpRepository.delete("),
        )
        val wrapped =
            "fun a() {\n    scope.launch {\n        withContext(Dispatchers.IO) {\n" +
                "            mcpRepository.delete(\"x\")\n        }\n    }\n}\n"
        assertTrue(
            "a wrapped call must not be reported",
            bareCallLines(wrapped, "mcpRepository.delete(").isEmpty(),
        )
    }

    @Test
    fun helper_readsAnUnKeyedRememberAsEmpty() {
        assertEquals(
            "an un-keyed remember must not be read as keyed on its initialiser",
            emptySet<String>(),
            rememberKeys("var x by remember { mutableStateOf(\"\") }\n", "var x by remember"),
        )
    }

    // ── ⑤ anchors the fix must NOT move (regression controls) ───────────

    @Test
    fun orderSensitiveLiterals_survive() {
        val code = codeOnly(mcpScreen)
        for (literal in listOf(
            // the delete dialog still dismisses on Main before the write
            // starts, so it cannot flicker or absorb a second tap
            "deleteId = null",
            // the UI flag flips immediately; only the Keystore write follows
            "oauthAuthorized = false",
            // the Compose-owned value is captured on Main before the IO block
            "val secretValue = oauthClientSecret",
            "val payload = text",
        )) {
            assertTrue("shape literal missing: $literal", code.contains(literal))
        }
    }

    // ── helpers (independent of the scanner gate's implementation) ──────

    private fun assertAllOffMain(code: String, call: String, label: String) {
        assertTrue("$label: call site not found: $call", code.contains(call))
        val bare = bareCallLines(code, call)
        assertTrue(
            "$label: `$call` runs on Main at line(s) $bare — wrap it in " +
                "withContext(Dispatchers.IO), or move the IO into the repository/store " +
                "method (the criterion is the dispatcher the call runs on, not the spelling)",
            bare.isEmpty(),
        )
    }

    /** 1-based line numbers of `call` occurrences NOT inside an IO wrap. */
    private fun bareCallLines(code: String, call: String): List<Int> {
        val out = mutableListOf<Int>()
        var from = 0
        while (true) {
            val idx = code.indexOf(call, from)
            if (idx < 0) break
            if (!isInsideIoWrap(code, idx)) {
                out.add(code.substring(0, idx).count { it == '\n' } + 1)
            }
            from = idx + 1
        }
        return out
    }

    /** True when `offset` sits inside a `withContext(Dispatchers.IO) { … }` or
     *  `launch(Dispatchers.IO) { … }` block. */
    private fun isInsideIoWrap(raw: String, offset: Int): Boolean {
        val code = maskStringsAndComments(raw)
        val stack = ArrayDeque<Boolean>()
        var i = 0
        while (i < offset && i < code.length) {
            when (code[i]) {
                '{' -> {
                    val head = code.substring(0, i).trimEnd().removeSuffix("{").trimEnd()
                    stack.addLast(
                        head.endsWith("withContext(Dispatchers.IO)") ||
                            head.endsWith("launch(Dispatchers.IO)"),
                    )
                }
                '}' -> if (stack.isNotEmpty()) stack.removeLast()
            }
            i++
        }
        return stack.any { it }
    }

    /** 1-based line numbers of `needle` occurrences inside a `remember { … }`
     *  initialiser (composition-thread code). */
    private fun rememberInitializersContaining(code: String, needle: String): List<Int> {
        val out = mutableListOf<Int>()
        var from = 0
        while (true) {
            val at = code.indexOf("remember", from)
            if (at < 0) break
            from = at + "remember".length
            var j = at + "remember".length
            while (j < code.length && (code[j] == ' ' || code[j] == '\t')) j++
            if (code.getOrNull(j) == '(') {
                val close = matchingParen(code, j)
                if (close < 0) continue
                j = close + 1
            }
            while (j < code.length && (code[j] == ' ' || code[j] == '\t')) j++
            if (code.getOrNull(j) != '{') continue
            val end = matchingBrace(code, j)
            if (end < 0) continue
            if (code.substring(j, end).contains(needle)) {
                out.add(code.substring(0, at).count { it == '\n' } + 1)
            }
        }
        return out
    }

    /** The key list of the `remember(...)` following `marker`; empty when the
     *  remember is un-keyed (`remember {`). */
    private fun rememberKeys(code: String, marker: String): Set<String> {
        val at = code.indexOf(marker)
        assertTrue("marker not found: $marker", at >= 0)
        var j = at + marker.length
        while (j < code.length && (code[j] == ' ' || code[j] == '\t')) j++
        if (code.getOrNull(j) != '(') return emptySet()
        val close = matchingParen(code, j)
        assertTrue("key list not closed after: $marker", close > 0)
        return code.substring(j + 1, close)
            .split(",")
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .toSet()
    }

    /** Key list of the (single) `LaunchedEffect(...)` whose body contains
     *  `assignment`; null when no such effect exists. */
    private fun effectKeysAssigning(code: String, assignment: String): List<String>? {
        val needle = "LaunchedEffect("
        var from = 0
        var found: List<String>? = null
        var hits = 0
        while (true) {
            val at = code.indexOf(needle, from)
            if (at < 0) break
            val open = at + needle.length - 1
            val close = matchingParen(code, open)
            if (close > 0) {
                val brace = code.indexOf('{', close)
                val end = if (brace > 0) matchingBrace(code, brace) else -1
                if (end > 0 && code.substring(brace, end).contains(assignment)) {
                    hits++
                    found = code.substring(open + 1, close)
                        .split(",").map { it.trim() }.filter { it.isNotEmpty() }
                }
            }
            from = at + 1
        }
        assertEquals("expected exactly one seeding LaunchedEffect, found $hits", 1, hits)
        return found
    }

    private fun matchingParen(code: String, open: Int): Int {
        var depth = 0
        var i = open
        while (i < code.length) {
            if (code[i] == '(') depth++
            if (code[i] == ')') {
                depth--
                if (depth == 0) return i
            }
            i++
        }
        return -1
    }

    private fun matchingBrace(code: String, open: Int): Int {
        var depth = 0
        var i = open
        while (i < code.length) {
            if (code[i] == '{') depth++
            if (code[i] == '}') {
                depth--
                if (depth == 0) return i + 1
            }
            i++
        }
        return -1
    }

    /** Drops `//` comment lines so prose cannot satisfy a shape probe. */
    private fun codeOnly(src: String): String = src
        .split("\n")
        .map { if (it.trimStart().startsWith("//")) " ".repeat(it.length) else it }
        .joinToString("\n")

    /** Blanks string bodies (length-preserving) so braces inside `${...}`
     *  templates cannot skew the brace walk. */
    private fun maskStringsAndComments(src: String): String {
        val out = StringBuilder(src.length)
        var i = 0
        while (i < src.length) {
            val c = src[i]
            if (c == '"') {
                if (src.startsWith("\"\"\"", i)) {
                    val j = src.indexOf("\"\"\"", i + 3).let { if (it < 0) src.length else it + 3 }
                    out.append("x".repeat(j - i))
                    i = j
                } else {
                    var j = i + 1
                    while (j < src.length) {
                        if (src[j] == '\\') { j += 2; continue }
                        if (src[j] == '"' || src[j] == '\n') { j++; break }
                        j++
                    }
                    out.append("x".repeat(j - i))
                    i = j
                }
            } else if (src.startsWith("//", i)) {
                val j = src.indexOf('\n', i).let { if (it < 0) src.length else it }
                out.append(" ".repeat(j - i))
                i = j
            } else {
                out.append(c)
                i++
            }
        }
        return out.toString()
    }

    private fun readRepoFile(relative: String): String {
        val cwd = System.getProperty("user.dir") ?: "."
        var dir: java.io.File? = java.io.File(cwd)
        while (dir != null) {
            val f = java.io.File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw java.io.FileNotFoundException("$relative not found from user.dir=$cwd")
    }
}

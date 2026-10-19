package com.rikkaminis.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [deep-audit-1032 ISSUE-05/07] Source-shape probe for the real chat-session id
 * at the *terminal* and *restore* seams of [MainActivity] / AppNavigation.
 *
 * A draft chat's route arg stays on `__new__<uuid>` for the whole lifetime of
 * the screen, even after the first send promoted it to a real session row. Any
 * entry point that consumes the raw nav arg binds to that orphan draft key:
 *
 *  - the terminal (ISSUE-05): the id selects `minis-sessions/<sid>/` bind mounts
 *    and `MINIS_CHAT_SESSION_ID`, so a draft/absent id gives the user a terminal
 *    whose `/var/minis/workspace` belongs to a draft dir — or to the global one —
 *    while the chat they came from lives under the promoted id;
 *  - the back-stack listener and the saved-state bundle (ISSUE-07): keep-alive
 *    presence tracks an orphan session, and a crash restore replays the draft
 *    key as a synthesized `OpenSession("__new__…")`, landing the user in a
 *    synthetic draft instead of the conversation they were in.
 *
 * These are wiring shapes across Activity ↔ NavController boundaries, so they
 * are asserted on the production text (same stance as
 * `A11yRecoverySlotShapeTest`). Each arm is also fed a pre-fix fixture so the
 * probe cannot pass by having stopped looking.
 */
class MainActivitySessionIdResolutionTest {

    private val activityRel =
        "src/android/app/src/main/java/com/rikkaminis/app/MainActivity.kt"
    private val navRel =
        "src/android/app/src/main/java/com/rikkaminis/app/ui/navigation/AppNavigation.kt"

    private val resolveExpr = Regex(
        "ChatViewModelStore\\s*\\.\\s*(?:resolveSessionId\\s*\\(|activeSessionId)"
    )

    // ── source access ────────────────────────────────────────────────────────

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

    /** Comments blanked, every offset preserved (string-literal aware). */
    private fun codeOnly(src: String): String {
        val out = StringBuilder(src.length)
        var i = 0
        val n = src.length
        while (i < n) {
            val c = src[i]
            if (c == '"' || c == '\'') {
                val end = skipLiteral(src, i)
                out.append(src, i, end)
                i = end
                continue
            }
            if (src.startsWith("//", i)) {
                var j = i
                while (j < n && src[j] != '\n') {
                    out.append(' ')
                    j++
                }
                i = j
                continue
            }
            if (src.startsWith("/*", i)) {
                val close = src.indexOf("*/", i + 2)
                val j = if (close < 0) n else close + 2
                for (k in i until j) out.append(if (src[k] == '\n') '\n' else ' ')
                i = j
                continue
            }
            out.append(c)
            i++
        }
        return out.toString()
    }

    private fun skipLiteral(code: String, i: Int): Int {
        val q = code[i]
        if (q == '"' && code.startsWith("\"\"\"", i)) {
            val j = code.indexOf("\"\"\"", i + 3)
            return if (j < 0) code.length else j + 3
        }
        var j = i + 1
        while (j < code.length) {
            if (code[j] == '\\') {
                j += 2
                continue
            }
            if (code[j] == q || code[j] == '\n') return j + 1
            j++
        }
        return code.length
    }

    private fun matchDelim(code: String, openIdx: Int, openCh: Char, closeCh: Char): Int {
        var depth = 0
        var i = openIdx
        while (i < code.length) {
            val c = code[i]
            if (c == '"' || c == '\'') {
                i = skipLiteral(code, i)
                continue
            }
            when (c) {
                openCh -> depth++
                closeCh -> {
                    depth--
                    if (depth == 0) return i
                }
            }
            i++
        }
        return -1
    }

    /** The single `{ … }` block whose declaration matches [decl], else null. */
    private fun blockAt(code: String, decl: Regex): String? {
        val matches = decl.findAll(code).toList()
        if (matches.size != 1) return null
        val open = code.indexOf('{', matches[0].range.first)
        if (open < 0) return null
        val close = matchDelim(code, open, '{', '}')
        return if (close < 0) null else code.substring(open, close + 1)
    }

    /** Local vals bound from a resolved-session expression inside [text]. */
    private fun resolvedNames(text: String): Set<String> {
        val names = mutableSetOf<String>()
        val lines = text.lines()
        val decl = Regex("^\\s*(?:val|var)\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*=")
        lines.forEachIndexed { i, line ->
            val m = decl.find(line) ?: return@forEachIndexed
            val stmt = StringBuilder(line)
            var j = i
            while (j + 1 < lines.size) {
                val cur = lines[j].trimEnd()
                val next = lines[j + 1].trim()
                if (next.isEmpty()) break
                val cont = cur.endsWith("=") || cur.endsWith("?.") || cur.endsWith(".") ||
                    cur.endsWith(",") || cur.endsWith("&&") || cur.endsWith("||") ||
                    cur.endsWith("?:") || next.startsWith("?.") || next.startsWith(".") ||
                    next.startsWith("?:") || next.startsWith("&&") || next.startsWith("||")
                if (!cont) break
                stmt.append(' ').append(next)
                j++
            }
            if (resolveExpr.containsMatchIn(stmt.toString())) names += m.groupValues[1]
        }
        return names
    }

    // ── analysers ────────────────────────────────────────────────────────────

    /** Criteria 10 + 11 on MainActivity: nav listener + saved-state bundle. */
    private fun restoreViolations(rawSrc: String): List<String> {
        val code = codeOnly(rawSrc)
        val out = mutableListOf<String>()

        val listener = blockAt(code, Regex("currentBackStackEntryFlow\\s*\\.\\s*collect\\s*\\{"))
        if (listener == null) {
            out += "MainActivity: `currentBackStackEntryFlow.collect { … }` listener block " +
                "missing or ambiguous"
        } else {
            if (!listener.contains("getString(\"sessionId\")")) {
                out += "MainActivity: the nav listener no longer reads the route `sessionId`"
            }
            val names = resolvedNames(listener)
            val writes = Regex("currentChatSessionId\\s*=\\s*([^\\n]+)").findAll(listener)
                .map { it.groupValues[1].trim() }.toList()
            if (writes.isEmpty()) {
                out += "MainActivity: the nav listener no longer assigns `currentChatSessionId`"
            }
            for (rhs in writes) {
                val ok = resolveExpr.containsMatchIn(rhs) ||
                    names.any { Regex("\\b${Regex.escape(it)}\\b").containsMatchIn(rhs) }
                if (!ok) {
                    out += "MainActivity: the nav listener assigns `currentChatSessionId` from " +
                        "an unresolved value ($rhs) — the draft key would be tracked as if it " +
                        "were a session"
                }
            }
            if (!listener.contains("resolveSessionId(")) {
                out += "MainActivity: the nav listener has no `resolveSessionId(` alias hop at all"
            }
        }

        val saver = blockAt(
            code,
            Regex("override\\s+fun\\s+onSaveInstanceState\\s*\\(\\s*outState\\s*:\\s*Bundle\\s*\\)\\s*\\{"),
        )
        if (saver == null) {
            out += "MainActivity: `override fun onSaveInstanceState(outState: Bundle) { … }` " +
                "missing or ambiguous"
        } else {
            if (!saver.contains("KEY_CURRENT_CHAT_SESSION_ID")) {
                out += "MainActivity: onSaveInstanceState no longer persists " +
                    "KEY_CURRENT_CHAT_SESSION_ID"
            } else if (!saver.contains("resolveSessionId(")) {
                out += "MainActivity: onSaveInstanceState persists the raw id — the bundle is " +
                    "replayed as a synthesized OpenSession deep link, so a draft key lands the " +
                    "user in a synthetic draft"
            }
        }
        return out
    }

    /** Criterion U6/U7 on a deep-link dispatcher: the terminal gets a real id. */
    private fun terminalDeepLinkViolations(rawSrc: String, label: String): List<String> {
        val code = codeOnly(rawSrc)
        val branch = blockAt(code, Regex("is\\s+DeepLinkAction\\.OpenTerminal\\s*->\\s*\\{"))
            ?: return listOf("$label: `is DeepLinkAction.OpenTerminal ->` branch missing or ambiguous")
        val callIdx = branch.indexOf("Routes.terminal(")
        if (callIdx < 0) return listOf("$label: branch does not navigate to `Routes.terminal(…)`")
        val open = branch.indexOf('(', callIdx)
        val close = matchDelim(branch, open, '(', ')')
        if (open < 0 || close < 0) return listOf("$label: unbalanced `Routes.terminal(` call")
        val args = branch.substring(open, close + 1)
        val names = resolvedNames(branch)
        val ok = resolveExpr.containsMatchIn(args) ||
            names.any { Regex("\\b${Regex.escape(it)}\\b").containsMatchIn(args) }
        return if (ok) {
            emptyList()
        } else {
            listOf(
                "$label: `Routes.terminal(...)` receives $args — neither a resolved id " +
                    "(`ChatViewModelStore.resolveSessionId(...)` / `.activeSessionId`) nor a " +
                    "local val bound from one, so the terminal binds to the draft key (or to " +
                    "no session at all)"
            )
        }
    }

    // ── live tree ────────────────────────────────────────────────────────────

    @Test
    fun `nav listener and saved state carry the resolved session id`() {
        val violations = restoreViolations(sourceFile(activityRel).readText())
        assertEquals("restore-seam violations: $violations", emptyList<String>(), violations)
    }

    @Test
    fun `hot terminal deep link carries a resolved session id`() {
        val violations = terminalDeepLinkViolations(sourceFile(activityRel).readText(), "MainActivity")
        assertEquals("hot deep-link violations: $violations", emptyList<String>(), violations)
    }

    @Test
    fun `cold-start terminal deep link carries a resolved session id`() {
        val violations = terminalDeepLinkViolations(sourceFile(navRel).readText(), "AppNavigation")
        assertEquals("cold deep-link violations: $violations", emptyList<String>(), violations)
    }

    @Test
    fun `new-chat entries still resolve through the single draft-id source`() {
        // "Must not change" anchor: criterion 9 of chat_session_id_binding_check.
        val code = codeOnly(sourceFile(activityRel).readText())
        assertTrue(
            "MainActivity must keep >= 2 `ComposerDraftStore.nextDraftId(` call sites",
            code.split("ComposerDraftStore.nextDraftId(").size - 1 >= 2,
        )
        assertTrue(
            "MainActivity must not hand-build a `__new__\${…}` draft id",
            !code.contains("__new__\${"),
        )
    }

    // ── teeth: the pre-fix wiring must be rejected ───────────────────────────

    private val preFixActivity = """
        class MainActivity {
            private var currentChatSessionId: String? = null
            private fun watch(navController: NavHostController) {
                navController.currentBackStackEntryFlow.collect { entry ->
                    val isChatRoute = entry.destination.route == Routes.CHAT
                    val sid = entry.arguments?.getString("sessionId").takeIf { isChatRoute }
                    if (sid != currentChatSessionId) {
                        currentChatSessionId = sid
                    }
                }
            }
            override fun onSaveInstanceState(outState: Bundle) {
                super.onSaveInstanceState(outState)
                currentChatSessionId?.let {
                    outState.putString(KEY_CURRENT_CHAT_SESSION_ID, it)
                }
            }
            private fun handleDeepLink(action: DeepLinkAction) {
                when (action) {
                    is DeepLinkAction.OpenTerminal -> {
                        nav.navigate(Routes.terminal(action.initCommand))
                    }
                }
            }
        }
    """.trimIndent()

    private val preFixNav = """
        fun AppNavigation(initialDeepLink: DeepLinkAction) {
            navController.awaitResumed()
            when (initialDeepLink) {
                is DeepLinkAction.OpenTerminal -> {
                    navController.safeNavigate(Routes.terminal(initialDeepLink.initCommand))
                }
            }
        }
    """.trimIndent()

    @Test
    fun `pre-fix restore seam is rejected`() {
        val violations = restoreViolations(preFixActivity)
        assertTrue(
            "pre-fix nav listener must be RED, got $violations",
            violations.any { it.contains("unresolved value") },
        )
        assertTrue(
            "pre-fix saved state must be RED, got $violations",
            violations.any { it.contains("persists the raw id") },
        )
    }

    @Test
    fun `pre-fix terminal deep links are rejected`() {
        val hot = terminalDeepLinkViolations(preFixActivity, "MainActivity")
        assertTrue("pre-fix hot deep link must be RED, got $hot", hot.isNotEmpty())
        val cold = terminalDeepLinkViolations(preFixNav, "AppNavigation")
        assertTrue("pre-fix cold deep link must be RED, got $cold", cold.isNotEmpty())
    }

    @Test
    fun `an unresolved resolve call does not satisfy the terminal probe`() {
        // A resolve() that is computed but not the value passed must stay RED.
        val unused = preFixActivity.replace(
            "nav.navigate(Routes.terminal(action.initCommand))",
            "val unused = ChatViewModelStore.resolveSessionId(action.sessionId)\n" +
                "                        nav.navigate(Routes.terminal(action.initCommand, sessionId = action.sessionId))",
        )
        val violations = terminalDeepLinkViolations(unused, "MainActivity")
        assertTrue(
            "an unused resolve() must not turn the terminal criterion green, got $violations",
            violations.isNotEmpty(),
        )
    }

    @Test
    fun `a resolved local val satisfies the terminal probe in any call form`() {
        // Precision: same decision point, positional argument.
        val positional = preFixActivity.replace(
            "nav.navigate(Routes.terminal(action.initCommand))",
            "val id = ChatViewModelStore.resolveSessionId(currentChatSessionId ?: \"\")\n" +
                "                        nav.navigate(Routes.terminal(action.initCommand, id))",
        )
        val violations = terminalDeepLinkViolations(positional, "MainActivity")
        assertEquals(
            "a positional resolved id must pass: $violations",
            emptyList<String>(),
            violations,
        )
    }
}

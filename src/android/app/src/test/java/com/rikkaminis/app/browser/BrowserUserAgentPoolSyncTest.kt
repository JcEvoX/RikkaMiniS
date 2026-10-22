package com.rikkaminis.app.browser

import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [da1030-B] Source-shape probe for the single-source user-agent wiring.
 *
 * Both halves of the fix sit in code the JVM unit-test runtime cannot reach:
 * `BrowserTabPool.handleSetUserAgent` needs a live `BrowserUseManager`/WebView
 * (`Dispatchers.Main` + `tab.manager.setUserAgent`), and `BrowserSettingsSheet`
 * is a `@Composable` with no Robolectric on this test source set. So the
 * invariant is asserted on the shipped sources themselves — the same probe
 * shape as `AppInitSkipGuardWiringTest.sourceFile()` / `DatabaseVersionGuardTest`
 * — with expected text written as literals that no production constant may
 * supply.
 *
 * Invariants:
 *  1. the pool routes `set_user_agent` explicitly (not through the per-tab
 *     `else`) and its handler replays the resolved viewport;
 *  2. the handler persists the SAME `browser_prefs` keys the sheet writes
 *     (one meaning, one source of truth);
 *  3. the sheet's UA selection follows the pool flow (`collectAsState()` +
 *     keyed remember);
 *  4. regression anchor: `cookieFilterText` stays an un-keyed remember.
 *
 * `mutationsAreCaught` keeps the predicates honest: each assertion family is
 * re-run on a mutated copy that removes exactly the thing it pins.
 */
class BrowserUserAgentPoolSyncTest {

    private val poolRel = "src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserTabPool.kt"
    private val sheetRel = "src/android/app/src/main/java/com/rikkaminis/app/ui/browser/BrowserSettingsSheet.kt"

    // ── shapes ────────────────────────────────────────────────────────────────

    private val rememberKeyedRegex = Regex("""var\s+selectedProfile\s+by\s+remember\s*\(([^)]*)\)""")
    private val rememberUnkeyedRegex = Regex("""var\s+selectedProfile\s+by\s+remember\s*\{""")
    private val filterKeyedRegex = Regex("""var\s+cookieFilterText\s+by\s+remember\s*\(""")
    private val filterUnkeyedRegex = Regex("""var\s+cookieFilterText\s+by\s+remember\s*\{""")

    private val profilePrefKeyLiteral = "\"user_agent_profile\""
    private val customPrefKeyLiteral = "\"custom_user_agent\""

    /** `fun handleSetUserAgent(` in the pool source. */
    private val handlerMarker = "fun handleSetUserAgent("
    private val branchLiteral = "BrowserAction.SET_USER_AGENT ->"
    private val replayLiteral = "applyViewportToAllTabs()"
    private val resolveLiteral = "resolvedViewportSize("
    private val poolFlowLiteral = "currentUserAgentProfile"

    // ── tests ─────────────────────────────────────────────────────────────────

    @Test
    fun poolRoutesSetUserAgentThroughAnExplicitBranch() {
        val pool = poolCode()
        val block = blockAt(pool, "when (input.action)")
        assertTrue(
            "set_user_agent must have an explicit pool branch, not the per-tab else",
            block.contains(branchLiteral),
        )
    }

    @Test
    fun poolHandlerReplaysTheResolvedViewport() {
        val handler = blockAt(poolCode(), handlerMarker)
        assertTrue(
            "the UA switch must replay the resolved viewport after setUserAgent resets the layout",
            handler.contains(replayLiteral),
        )
        assertTrue(
            "the handler must report the resolved viewport size",
            handler.contains(resolveLiteral),
        )
    }

    @Test
    fun poolHandlerPersistsTheSamePrefsKeysAsTheSheet() {
        val handler = blockAt(poolCode(), handlerMarker)
        val sheet = sheetCode()
        assertTrue("pool must persist the profile key", handler.contains(profilePrefKeyLiteral))
        assertTrue("sheet must write the profile key", sheet.contains(profilePrefKeyLiteral))
        assertTrue("pool must know the custom-UA key", handler.contains(customPrefKeyLiteral))
        assertTrue("sheet must write the custom-UA key", sheet.contains(customPrefKeyLiteral))
    }

    @Test
    fun sheetSelectionFollowsThePoolFlow() {
        val sheet = sheetCode()
        assertTrue(
            "the sheet must subscribe to the pool's UA flow",
            sheet.contains(".$poolFlowLiteral.collectAsState()"),
        )
        val m = rememberKeyedRegex.find(sheet)
        assertNotNull("selectedProfile must carry a remember(...) key list", m)
        val key = m!!.groupValues[1].trim()
        assertFalse("selectedProfile must not be keyed on nothing", key.isEmpty())
        val declaration = declarationWindow(sheet, key)
        assertTrue(
            "the remember key `$key` must be fed by the pool's UA flow",
            declaration != null && declaration.contains(poolFlowLiteral),
        )
    }

    @Test
    fun filterBoxStaysUnkeyed() {
        val sheet = sheetCode()
        assertTrue(
            "cookieFilterText must remain a plain un-keyed remember (regression anchor)",
            filterUnkeyedRegex.containsMatchIn(sheet),
        )
        assertFalse(
            "cookieFilterText must not gain a key — it would reset the user's typed filter",
            filterKeyedRegex.containsMatchIn(sheet),
        )
    }

    @Test
    fun mutationsAreCaught() {
        val pool = poolCode()
        val sheet = sheetCode()

        val noBranch = pool.replace("$branchLiteral handleSetUserAgent(input)\n", "")
        assertFalse(
            "dropping the explicit branch must be observable",
            blockAt(noBranch, "when (input.action)").contains(branchLiteral),
        )

        val noReplay = pool.replace(replayLiteral, "")
        assertFalse(
            "dropping the viewport replay must be observable",
            blockAt(noReplay, handlerMarker).contains(replayLiteral),
        )

        val unkeyedSheet = sheet.replace(
            "var selectedProfile by remember(poolProfile) {",
            "var selectedProfile by remember {",
        )
        assertTrue("the un-keyed shape must match the frozen pattern", rememberUnkeyedRegex.containsMatchIn(unkeyedSheet))
        assertFalse("the keyed pattern must not match the un-keyed shape", rememberKeyedRegex.containsMatchIn(unkeyedSheet))

        val nonceSheet = sheet.replace(
            "var selectedProfile by remember(poolProfile) {",
            "var selectedProfile by remember(vpNonce) {",
        )
        val nonceKey = rememberKeyedRegex.find(nonceSheet)!!.groupValues[1].trim()
        val nonceDecl = declarationWindow(nonceSheet, nonceKey)
        assertFalse(
            "a key not fed by the pool flow must not satisfy the criterion",
            nonceDecl != null && nonceDecl.contains(poolFlowLiteral),
        )

        val keyedFilter = sheet.replace(
            "var cookieFilterText by remember {",
            "var cookieFilterText by remember(cookieRefreshTick) {",
        )
        assertTrue("a keyed filter box must be observable", filterKeyedRegex.containsMatchIn(keyedFilter))
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    @Test
    fun aPresetTapDoesNotClearTheStoredCustomUserAgent() {
        val body = blockAt(poolCode(), "fun setUserAgentFromUI(")
        val guard = body.indexOf("if (customUA != null) {")
        val write = body.indexOf("customUserAgentString = customUA.ifEmpty { null }")
        assertTrue("a null custom UA must mean \"no opinion\":\n$body", guard >= 0)
        assertTrue(
            "the stored string must only be replaced inside that guard " +
                "(guard@$guard write@$write):\n$body",
            write > guard,
        )
        assertFalse(
            "an unconditional assignment is exactly the clear-on-preset defect:\n$body",
            body.contains("customUserAgentString = customUA\n"),
        )
        assertTrue(
            "CUSTOM must resolve through the stored value, not the nullable argument:\n$body",
            body.contains("storedCustomUserAgent()"),
        )
    }

    @Test
    fun theCustomProfileFallsBackToPrefsWhenTheCacheIsEmpty() {
        val pool = poolCode()
        val lines = pool.lines()
        val start = lines.indexOfFirst { it.contains("private fun storedCustomUserAgent(") }
        assertTrue("storedCustomUserAgent() is missing from the pool", start >= 0)
        val helper = lines.subList(start, minOf(lines.size, start + 6)).joinToString("\n")
        assertTrue(
            "the single source must be the pref key the sheet writes:\n$helper",
            helper.contains("getString(\"custom_user_agent\", null)"),
        )
        assertTrue(
            "an emptied field must collapse to null, not the empty string:\n$helper",
            helper.contains(".ifEmpty { null }"),
        )
        val handler = blockAt(pool, "fun handleSetUserAgent(")
        assertTrue(
            "set_user_agent must resolve CUSTOM through the single source:\n$handler",
            handler.contains("if (newProfile == UserAgentProfile.CUSTOM) storedCustomUserAgent() else null"),
        )
        assertTrue(
            "the sheet's UA field must feed the pool's copy, not just prefs",
            sheetCode().contains("tabPool.stageCustomUserAgent("),
        )
    }

    private fun poolCode(): String = stripLineComments(source(poolRel))
    private fun sheetCode(): String = stripLineComments(source(sheetRel))

    private fun source(rel: String): String = sourceFile(rel).readText()

    /**
     * Declaration window of `val <ident>`: the declaration line plus any
     * following lines indented deeper than it (a wrapped expression). Mirrors
     * the gate's `_statement_window`, so a multi-line declaration still
     * satisfies the "fed by the pool flow" criterion.
     */
    private fun declarationWindow(text: String, ident: String): String? {
        val regex = Regex("""^([ \t]*)val\s+""" + Regex.escape(ident) + """\b""", RegexOption.MULTILINE)
        val m = regex.find(text) ?: return null
        val indent = m.groupValues[1].replace("\t", "    ").length
        val lines = text.substring(m.range.first).lines()
        val out = mutableListOf(lines[0])
        for (line in lines.drop(1)) {
            if (line.isBlank()) break
            if (line.length - line.trimStart().length <= indent) break
            out.add(line)
        }
        return out.joinToString("\n")
    }

    /** From the first line containing [marker], take lines until that line's brace is balanced. */
    private fun blockAt(text: String, marker: String): String {
        val lines = text.lines()
        val idx = lines.indexOfFirst { marker in it }
        assertTrue("marker '$marker' not found in the source", idx >= 0)
        var depth = 0
        var started = false
        val buf = StringBuilder()
        for (i in idx until lines.size) {
            for (ch in lines[i]) {
                when (ch) {
                    '{' -> { depth++; started = true }
                    '}' -> depth--
                }
            }
            buf.append(lines[i]).append('\n')
            if (started && depth <= 0) break
        }
        return buf.toString()
    }

    private fun stripLineComments(src: String): String {
        val out = mutableListOf<String>()
        for (line in src.lines()) {
            var inStr = false
            var i = 0
            var cut = line.length
            while (i < line.length) {
                val c = line[i]
                if (c == '\\' && inStr) { i += 2; continue }
                if (c == '"') inStr = !inStr
                else if (c == '/' && !inStr && i + 1 < line.length && line[i + 1] == '/') { cut = i; break }
                i++
            }
            out.add(line.substring(0, cut))
        }
        return out.joinToString("\n")
    }

    // ── source location (same technique as AppInitSkipGuardWiringTest) ────────

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

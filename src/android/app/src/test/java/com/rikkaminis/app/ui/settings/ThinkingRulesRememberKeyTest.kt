package com.rikkaminis.app.ui.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/da1028-c] — deep-audit-1028 ISSUE-02.
 *
 * `ThinkingRulesSection` shows two values that are DERIVED inside
 * `ProviderRepository`:
 *   - `builtInThinkingRulesForDisplay(instanceId)` reads
 *     `config.instances[id].effectiveBaseURL` (= `customBaseURL` +
 *     `appendV1Suffix`) and filters `config.modelEntries` down to the
 *     instance, then keeps the built-ins whose `modelIds` intersect.
 *   - `firstModelId(instanceId)` reads the same `config.modelEntries`.
 *
 * Both were cached under `remember(instance.id, reloadKey)`. `reloadKey` only
 * bumps on rule mutations and `instance.id` never changes, so editing the
 * gateway URL, toggling `appendV1Suffix`, or refreshing the model catalog left
 * the section showing (and the resolution preview resolving) a stale snapshot
 * until the screen was recreated.
 *
 * The screen is Compose-only (it needs a real Context + ProviderRepository), so
 * the contract is pinned by source-shape assertions — the approach this repo
 * already uses for Compose-only screens (`SharedCacheExportPruneTest`). Expected
 * key sets are hardcoded literals and compared as SETS, never derived from the
 * code under test, so a mutation cannot masquerade as PASS. The companion
 * assertion that a non-derived `remember` stays untouched is the precision
 * control: the criterion keys on `builtInThinkingRulesForDisplay(` /
 * `firstModelId(` specifically, it is not "every remember must be keyed".
 */
class ThinkingRulesRememberKeyTest {

    private val section: String by lazy {
        readRepoFile("app/src/main/java/com/rikkaminis/app/ui/settings/ThinkingRulesSection.kt")
    }
    private val detail: String by lazy {
        readRepoFile("app/src/main/java/com/rikkaminis/app/ui/settings/ProviderDetailScreen.kt")
    }

    @Test
    fun `builtIns remember keys every source the repository call derives from`() {
        val keys = rememberKeys(codeOnly(section), "val builtIns = remember")
        assertEquals(
            "builtIns derives from effectiveBaseURL(customBaseURL, appendV1Suffix) + the " +
                "instance's modelEntries — all three must be keys",
            setOf(
                "instance.id",
                "instance.customBaseURL",
                "instance.appendV1Suffix",
                "modelEntries",
                "reloadKey",
            ),
            keys,
        )
    }

    @Test
    fun `sampleModelId remember keys the entry list`() {
        val keys = rememberKeys(codeOnly(section), "val sampleModelId = remember")
        assertEquals(
            "firstModelId reads config.modelEntries; without that key a catalog refresh " +
                "leaves the resolution preview pointing at a model that no longer exists",
            setOf("instance.id", "modelEntries", "reloadKey"),
            keys,
        )
    }

    @Test
    fun `detail screen hands the section its entry list`() {
        val code = codeOnly(detail)
        val call = code.indexOf("ThinkingRulesSection(")
        assertTrue("ThinkingRulesSection( call site not found in ProviderDetailScreen", call >= 0)
        assertFalse(
            "matched the declaration, not the call site",
            code.substring(maxOf(0, call - 6), call).contains("fun "),
        )
        val args = code.substring(call, minOf(code.length, call + 240))
        assertTrue(
            "the call site must forward `modelEntries = ...` — a section that is never given " +
                "the list cannot key on it at all",
            args.contains("modelEntries = "),
        )
        assertTrue(
            "the forwarded value must be the FULL entry list for this instance " +
                "(entriesFor), not the visible-only subset: builtInThinkingRulesForDisplay " +
                "filters all of config.modelEntries, hidden entries included",
            args.contains("modelEntries = allEntries"),
        )
    }

    // -- precision control: nothing else in the section may be dragged along --

    @Test
    fun `the Room loader and the pure UI flags keep their original keys`() {
        val code = codeOnly(section)
        assertEquals(
            "customRulesAndIds comes from the Room DAO, not from config — keying it on " +
                "modelEntries would re-query Room on every catalog refresh",
            setOf("instance.id", "reloadKey"),
            rememberKeys(code, "val customRulesAndIds = remember"),
        )
        for (flag in listOf("reloadKey", "defaultsExpanded")) {
            assertTrue(
                "`var $flag by remember {` is a pure UI flag and must stay keyless",
                code.contains("var $flag by remember {"),
            )
        }
    }

    // -- helpers --

    /** Comment lines cannot satisfy a key list. */
    private fun codeOnly(src: String): String = src
        .split("\n")
        .filterNot { it.trimStart().startsWith("//") || it.trimStart().startsWith("*") }
        .joinToString("\n")

    /** `(a, b, c)` key list of `remember` in the `marker` statement, as a set. */
    private fun rememberKeys(code: String, marker: String): Set<String> {
        val start = code.indexOf(marker)
        assertTrue("marker not found: $marker", start >= 0)
        val open = code.indexOf('(', start + marker.length)
        assertTrue("no key list after: $marker", open >= 0)
        val close = code.indexOf(") {", open)
        assertTrue("key list not closed after: $marker", close >= 0)
        return code.substring(open + 1, close)
            .split(",")
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .toSet()
    }

    private fun readRepoFile(relative: String): String {
        var dir: java.io.File? = java.io.File(System.getProperty("user.dir"))
        while (dir != null) {
            val f = java.io.File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw java.io.FileNotFoundException(
            "$relative not found from user.dir=${System.getProperty("user.dir")}",
        )
    }
}

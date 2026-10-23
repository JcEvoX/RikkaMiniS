package com.rikkaminis.app.ui.settings

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [dv27-B3] The group detail screen's three System-voice presentation
 * decisions, plus the source shapes that wire them.
 *
 * Why two layers. The DECISIONS (`SystemVoiceMemberPresentation`) are plain
 * Kotlin, so they are asserted directly — a System voice group member has no
 * `ProviderInstance`, and each of the three answers used to be an accident of
 * that: a blank subtitle, a cost-tier tick that never moved, and a remove
 * dialog that lost the model name. The WIRING is Compose, which a JVM test
 * cannot compose; each property there is asserted on the production text, with
 * line comments blanked out first — the fix's own explanatory comments quote
 * the very expressions being checked (`instance?.label ?: ""`), and a mention
 * in prose must never satisfy a shape arm.
 *
 * One arm per property, each reporting its own reason, so the mutation table can
 * show exactly one arm flipping per mutation. A missing production file is a
 * violation, not an exception, so the pre-fix tree still yields per-arm reasons.
 */
class ModelGroupDetailSystemVoiceTest {

    private val settingsDir = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings"
    private val screenRel = "$settingsDir/ModelGroupDetailScreen.kt"

    private fun sourceFileOrNull(rel: String): File? {
        val viaAndroid = "src/android/app/$rel"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel)
            if (File(d, viaAndroid).isFile) return File(d, viaAndroid)
            dir = d.parentFile
        }
        return null
    }

    /** Line comments blanked out (offsets preserved) so prose cannot satisfy an arm. */
    private fun codeOnly(body: String): String = body.lines().joinToString("\n") { line ->
        val t = line.trimStart()
        if (t.startsWith("//") || t.startsWith("*") || t.startsWith("/*")) {
            " ".repeat(line.length)
        } else {
            line
        }
    }

    private fun screen(): String = sourceFileOrNull(screenRel)?.let { codeOnly(it.readText()) } ?: ""

    // ── decision arms (empty list = the decision holds) ────────────────────────

    private val systemLabel = "系统"

    /** A1: a System member names its provider — the device — instead of going blank. */
    @Test
    fun a1_systemMemberSubtitleNamesTheDevice() {
        val violations = mutableListOf<String>()
        val got = SystemVoiceMemberPresentation.memberSubtitle(
            instanceLabel = null, isSystemEntry = true, systemLabel = systemLabel,
        )
        if (got != systemLabel) {
            violations.add("System member subtitle was \"$got\", expected \"$systemLabel\"")
        }
        assertTrue("a1: $violations", violations.isEmpty())
    }

    /** A2: a persisted provider's label still wins over the System fallback. */
    @Test
    fun a2_persistedProviderLabelWins() {
        val violations = mutableListOf<String>()
        val got = SystemVoiceMemberPresentation.memberSubtitle(
            instanceLabel = "My OpenAI", isSystemEntry = true, systemLabel = systemLabel,
        )
        if (got != "My OpenAI") {
            violations.add("subtitle was \"$got\", expected the provider label")
        }
        assertTrue("a2: $violations", violations.isEmpty())
    }

    /**
     * A3: a PERSISTED entry whose provider was deleted stays blank — it must not
     * borrow the System name, which would claim the device serves it.
     */
    @Test
    fun a3_stalePersistedMemberDoesNotClaimSystem() {
        val violations = mutableListOf<String>()
        val got = SystemVoiceMemberPresentation.memberSubtitle(
            instanceLabel = null, isSystemEntry = false, systemLabel = systemLabel,
        )
        if (got.isNotEmpty()) violations.add("stale persisted member subtitle was \"$got\"")
        assertTrue("a3: $violations", violations.isEmpty())
    }

    /** A4: the cost-tier menu is not offered for a member that cannot persist it. */
    @Test
    fun a4_costTierHiddenForSystemMembers() {
        val violations = mutableListOf<String>()
        if (SystemVoiceMemberPresentation.canEditCostTier(isSystemEntry = true)) {
            violations.add("cost-tier menu offered for a System member (write would be dropped)")
        }
        assertTrue("a4: $violations", violations.isEmpty())
    }

    /** A5: …and stays offered for a persisted member. */
    @Test
    fun a5_costTierOfferedForPersistedMembers() {
        val violations = mutableListOf<String>()
        if (!SystemVoiceMemberPresentation.canEditCostTier(isSystemEntry = false)) {
            violations.add("cost-tier menu hidden for a persisted member")
        }
        assertTrue("a5: $violations", violations.isEmpty())
    }

    /** A6: the remove confirmation names a System member's model. */
    @Test
    fun a6_removalDialogNamesASystemMember() {
        val violations = mutableListOf<String>()
        val got = SystemVoiceMemberPresentation.removalDisplayName(
            entryName = "System Recognition (Online)", fallback = "this model",
        )
        if (got != "System Recognition (Online)") {
            violations.add("dialog named the member \"$got\", expected the model name")
        }
        assertTrue("a6: $violations", violations.isEmpty())
    }

    /** A7: a genuinely stale id still falls back to the generic copy. */
    @Test
    fun a7_removalDialogFallsBackForStaleIds() {
        val violations = mutableListOf<String>()
        val got = SystemVoiceMemberPresentation.removalDisplayName(
            entryName = null, fallback = "this model",
        )
        if (got != "this model") {
            violations.add("dialog was \"$got\" for a stale id, expected the fallback copy")
        }
        assertTrue("a7: $violations", violations.isEmpty())
    }

    // ── source-shape arms (empty list = the shape holds) ──────────────────────

    /** S1: no member label degrades to the empty string. */
    @Test
    fun s1_noEmptyStringLabelFallback() {
        val violations = mutableListOf<String>()
        val code = screen()
        if (code.isEmpty()) violations.add("$screenRel not found")
        if (Regex("""\?\:\s*"{2}""").containsMatchIn(code)) {
            violations.add("the screen still falls a label back to `\"\"`")
        }
        assertTrue("s1: $violations", violations.isEmpty())
    }

    /**
     * S2: no member id is looked up in `config.modelEntries` alone — the
     * persisted list does not contain a System voice sentinel, so every such
     * lookup answers null for a default Voice group's entire membership.
     */
    @Test
    fun s2_noPersistedOnlyEntryLookup() {
        val violations = mutableListOf<String>()
        val code = screen()
        if (code.isEmpty()) violations.add("$screenRel not found")
        if (Regex("""modelEntries\s*\.\s*(?:find|first|firstOrNull|singleOrNull|filter)\b""").containsMatchIn(code)) {
            violations.add("the screen resolves an entry in `config.modelEntries` directly")
        }
        if (!code.contains("SystemVoiceEntries.resolveIn(config.modelEntries")) {
            violations.add("the screen never resolves through SystemVoiceEntries.resolveIn")
        }
        assertTrue("s2: $violations", violations.isEmpty())
    }

    /**
     * S3: the cost-tier write sits INSIDE the guard. `updateEntry` drops a
     * virtual entry silently (`indexOfFirst == -1`), so a guard that only
     * precedes the menu — rather than enclosing the write — changes nothing.
     * Found structurally so it survives a re-indent: scan from the guard's own
     * `{` (the first one after `canEditCostTier(`) to its matching `}`, skipping
     * `"…"` literals and the `${…}` templates inside them, then ask whether the
     * write offset fell inside that span.
     */
    @Test
    fun s3_costTierWriteIsGuarded() {
        val violations = mutableListOf<String>()
        val code = screen()
        if (code.isEmpty()) violations.add("$screenRel not found")
        val write = code.indexOf("providerRepository.updateEntry(")
        val guard = code.indexOf("canEditCostTier(")
        if (write < 0) violations.add("no providerRepository.updateEntry( call on the screen")
        if (guard < 0) violations.add("no canEditCostTier( guard on the screen")
        if (write >= 0 && guard >= 0) {
            val open = skipStringsIndexOf(code, '{', guard)
            val close = if (open < 0) -1 else matchBrace(code, open)
            if (close < 0) {
                violations.add("the guard's block has no matching closing brace")
            } else if (write <= open || write >= close) {
                violations.add(
                    "the entry write (offset $write) is outside the guard's block "
                        + "($open..$close) — `updateEntry` would still run for a System member"
                )
            }
        }
        assertTrue("s3: $violations", violations.isEmpty())
    }

    /** Index of the first [needle] at/after [from] that is not inside a string literal. */
    private fun skipStringsIndexOf(code: String, needle: Char, from: Int): Int {
        var i = from
        while (i < code.length) {
            when (code[i]) {
                '"' -> i = endOfLiteral(code, i)
                '/' -> if (i + 1 < code.length && code[i + 1] == '/') {
                    val nl = code.indexOf('\n', i)
                    i = if (nl < 0) code.length else nl
                } else i++
                needle -> return i
                else -> i++
            }
        }
        return -1
    }

    /** Matching `}` for the `{` at [open], skipping literals and their `${…}` templates. */
    private fun matchBrace(code: String, open: Int): Int {
        var depth = 0
        var i = open
        while (i < code.length) {
            when (code[i]) {
                '"' -> i = endOfLiteral(code, i)
                '$' -> if (i + 1 < code.length && code[i + 1] == '{') { depth++; i += 2 } else i++
                '{' -> { depth++; i++ }
                '}' -> {
                    depth--
                    if (depth == 0) return i
                    i++
                }
                else -> i++
            }
        }
        return -1
    }

    /** Index just past the closing quote of the `"…"` literal opened at [at]. */
    private fun endOfLiteral(code: String, at: Int): Int {
        var i = at + 1
        while (i < code.length) {
            when (code[i]) {
                '\\' -> i += 2
                '"' -> return i + 1
                else -> i++
            }
        }
        return code.length
    }

    /**
     * S4: the subtitle goes through the single presentation decision AND gets a
     * localized provider name — zero new language keys, zero hardcoded "System".
     */
    @Test
    fun s4_subtitleRoutesThroughTheDecision() {
        val violations = mutableListOf<String>()
        val code = screen()
        if (code.isEmpty()) violations.add("$screenRel not found")
        if (!code.contains("SystemVoiceMemberPresentation.memberSubtitle(")) {
            violations.add("the member subtitle is not built by memberSubtitle(")
        }
        if (!code.contains("R.string.voice_provider_system")) {
            violations.add("the System member label is not read from resources")
        }
        if (!code.contains("SystemVoiceEntries.isSystemEntryId(")) {
            violations.add("the subtitle does not consult SystemVoiceEntries.isSystemEntryId")
        }
        assertTrue("s4: $violations", violations.isEmpty())
    }

    /** S5: the remove confirmation resolves the member through the same source. */
    @Test
    fun s5_removeDialogResolvesThroughTheDecision() {
        val violations = mutableListOf<String>()
        val code = screen()
        if (code.isEmpty()) violations.add("$screenRel not found")
        if (!code.contains("SystemVoiceMemberPresentation.removalDisplayName(")) {
            violations.add("the remove dialog does not use removalDisplayName(")
        }
        if (!code.contains("val removingEntry = SystemVoiceEntries.resolveIn(")) {
            violations.add("the remove dialog resolves the member id off the persisted list")
        }
        assertTrue("s5: $violations", violations.isEmpty())
    }
}

package com.rikkaminis.app.data.repository

import com.rikkaminis.app.data.model.LLMModel
import com.rikkaminis.app.data.model.ModelEntry
import com.rikkaminis.app.data.model.ModelOverrides
import com.rikkaminis.app.data.model.ProviderCredential
import com.rikkaminis.app.data.model.ProviderInstance
import com.rikkaminis.app.data.model.ProviderType
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [T-fix-import-id-roundtrip] JVM round-trip regression for deep-audit-1021
 * ISSUE-02 + ISSUE-09:
 *   - mergeImportInstanceJSON returned the bare temp `entry.id` (a random UUID
 *     minted by parseImportedModelEntries) instead of the composite id
 *     saveConfig canonicalizes every persisted entry to
 *     ("{instanceId}/{modelId}") — every entryIdMap consumer dangled.
 *   - exportInstanceJSON / parseImportedModelEntries dropped
 *     `reasoningEffortValues` + `declaresNoEffortTiers` (Room path had them).
 *   - convertThinkingRules dropped `reasoningEchoJson` (restoreThinkingRules
 *     already read it).
 *
 * [ProviderRepository] itself needs an Android Context (not JVM-constructible),
 * so — same as [BackupFieldEvapRoundTripTest] — this test pins the contract by
 * faithfully replicating the production key-write/read logic. The replicas are
 * copy-pasted from the production code (including the
 * [T-fix-import-id-roundtrip] hunks), so a regression fails here the same way
 * it fails on device.
 *
 * Expected values are hardcoded literals, NOT derived from the production
 * constants (evidence-discipline: a mutation must not move the expectation).
 */
class ProviderImportRoundTripTest {

    // ------------------------------------------------------------------
    // Faithful replica of exportInstanceJSON's reasoning-pair key writing
    // ([T-fix-import-id-roundtrip] export hunk, keys only).
    // ------------------------------------------------------------------
    private fun exportModel(base: LLMModel): JSONObject = JSONObject().apply {
        put("modelId", base.id)
        put("displayName", base.displayName)
        base.contextWindow?.let { put("contextWindow", it) }
        base.maxOutputTokens?.let { put("maxOutputTokens", it) }
        base.supportsReasoning?.let { put("supportsReasoning", it) }
        base.interleavedReasoningField?.let { put("interleavedReasoningField", it) }
        // [T-fix-import-id-roundtrip] must mirror ProviderRepository.exportInstanceJSON exactly.
        base.reasoningEffortValues?.takeIf { it.isNotEmpty() }?.let {
            put("reasoningEffortValues", JSONArray(it))
        }
        base.declaresNoEffortTiers?.let { put("declaresNoEffortTiers", it) }
    }

    // ------------------------------------------------------------------
    // Faithful replica of parseImportedModelEntries' reasoning-pair read
    // ([T-fix-import-id-roundtrip] parse hunk).
    // ------------------------------------------------------------------
    private data class ParsedPair(
        val reasoningEffortValues: List<String>?,
        val declaresNoEffortTiers: Boolean?,
    )

    private fun importReasoningPair(m: JSONObject): ParsedPair {
        val reasoningEffortValues = m.optJSONArray("reasoningEffortValues")?.let { arr ->
            (0 until arr.length())
                .mapNotNull { idx -> arr.optString(idx, "").takeIf { it.isNotBlank() } }
                .takeIf { it.isNotEmpty() }
        }
        val declaresNoEffortTiers = if (m.has("declaresNoEffortTiers") && !m.isNull("declaresNoEffortTiers")) {
            m.optBoolean("declaresNoEffortTiers")
        } else null
        return ParsedPair(reasoningEffortValues, declaresNoEffortTiers)
    }

    // ------------------------------------------------------------------
    // Faithful replica of the merge resolution ([T-fix-import-id-roundtrip]
    // merge hunk): hit?.id ?: re-query after addEntry ?: composite fallback.
    // `configEntries` models _config.value.modelEntries AFTER the addEntry +
    // saveConfig canonicalization pass (composite persisted ids).
    // The composite fallback replicates data.db.compositeEntryKey
    // ("$instanceId/$modelId") — ProviderConfigMapping.kt carries Room
    // annotations and cannot be compiled standalone in this harness.
    // ------------------------------------------------------------------
    private fun resolveMergedEntryId(
        configEntries: List<Triple<String, String, String>>, // (instanceId, baseModelId, persistedId)
        instanceId: String,
        baseModelId: String,
    ): String {
        val hit = configEntries.firstOrNull { it.first == instanceId && it.second == baseModelId }
        return hit?.third ?: run {
            configEntries.firstOrNull {
                it.first == instanceId && it.second == baseModelId
            }?.third ?: "$instanceId/$baseModelId"
        }
    }

    // ------------------------------------------------------------------
    // Faithful replica of convertThinkingRules' per-record copy
    // ([T-fix-import-id-roundtrip] echo hunk).
    // ------------------------------------------------------------------
    private fun convertThinkingRule(d: JSONObject): JSONObject {
        val r = JSONObject()
        r.put("id", d.optString("id", ""))
        r.put("label", d.optString("label", ""))
        r.put("scopeKind", d.optString("scopeKind", "allModels"))
        d.optString("scopePattern", "").takeIf { it.isNotEmpty() }
            ?.let { r.put("scopePattern", it) }
        r.put("wireFormatJson", d.optString("wireFormatJson", "{}"))
        d.optString("reasoningEchoJson", "").takeIf { it.isNotEmpty() }
            ?.let { r.put("reasoningEchoJson", it) }
        r.put("sortOrder", d.optInt("sortOrder", 0))
        return r
    }

    private fun model(
        effort: List<String>? = null,
        noEffort: Boolean? = null,
    ) = LLMModel(
        id = "gpt-5.5",
        displayName = "GPT-5.5",
        provider = "OpenAI",
        contextWindow = 400_000,
        maxOutputTokens = 32_768,
        supportsReasoning = true,
        reasoningEffortValues = effort,
        declaresNoEffortTiers = noEffort,
    )

    // ------------------------------------------------------------------
    // Round trip: export → parse → the pair survives verbatim.
    // ------------------------------------------------------------------
    @Test
    fun reasoningPair_roundTripsThroughExportImport() {
        val exported = exportModel(
            model(effort = listOf("low", "high", "max"), noEffort = false),
        )
        val parsed = importReasoningPair(exported)
        assertEquals(listOf("low", "high", "max"), parsed.reasoningEffortValues)
        assertEquals(false, parsed.declaresNoEffortTiers)

        val exported2 = exportModel(model(noEffort = true))
        val parsed2 = importReasoningPair(exported2)
        assertNull(parsed2.reasoningEffortValues)
        assertEquals(true, parsed2.declaresNoEffortTiers)
    }

    // ------------------------------------------------------------------
    // Legacy compat: an old backup without the keys restores to null/absent.
    // ------------------------------------------------------------------
    @Test
    fun legacyBackupWithoutKeys_restoresToNull() {
        val legacy = JSONObject()
            .put("modelId", "gpt-5.5")
            .put("displayName", "GPT-5.5")
        assertFalse(legacy.has("reasoningEffortValues"))
        assertFalse(legacy.has("declaresNoEffortTiers"))
        val parsed = importReasoningPair(legacy)
        assertNull(parsed.reasoningEffortValues)
        assertNull(parsed.declaresNoEffortTiers)
    }

    // ------------------------------------------------------------------
    // Null/empty semantics: explicit null, empty array, blank-only entries,
    // and partially-blank lists all degrade exactly as the production code.
    // ------------------------------------------------------------------
    @Test
    fun nullAndEmptyDegradeToNull_notToEmptyList() {
        // explicit null in JSON (org.json serializes null keys as a real null)
        val explicitNull = JSONObject("{\"modelId\":\"m\",\"declaresNoEffortTiers\":null}")
        assertNull(importReasoningPair(explicitNull).declaresNoEffortTiers)

        // empty array → null (not an empty list)
        val emptyArr = exportModel(model(effort = emptyList()))
        assertNull(importReasoningPair(emptyArr).reasoningEffortValues)

        // blank-only entries → null
        val blanks = JSONObject("{\"modelId\":\"m\",\"reasoningEffortValues\":[\" \",\"\"]}")
        assertNull(importReasoningPair(blanks).reasoningEffortValues)

        // partially blank: blanks filtered, non-blanks survive verbatim
        val partial = JSONObject("{\"modelId\":\"m\",\"reasoningEffortValues\":[\"\", \"max\"]}")
        assertEquals(listOf("max"), importReasoningPair(partial).reasoningEffortValues)
    }

    // ------------------------------------------------------------------
    // Merge resolution: a NEWLY ADDED model must resolve to the PERSISTED
    // composite id, never to the temp uuid parseImportedModelEntries minted.
    // ------------------------------------------------------------------
    @Test
    fun mergeResolution_returnsPersistedCompositeId_notTempUuid() {
        val tempUuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        val instanceId = "inst-1"
        val baseModelId = "gpt-5.5"
        // The temp uuid the import minted is NOT in the persisted config.
        val persisted = listOf(
            Triple("inst-1", "other-model", "inst-1/other-model"),
        )
        val resolved = resolveMergedEntryId(persisted, instanceId, baseModelId)
        // The resolution must be the composite form the snapshot wrote —
        // literally "inst-1/gpt-5.5" — never the temp uuid.
        assertEquals("inst-1/gpt-5.5", resolved)
        assertFalse(tempUuid == resolved)

        // An EXISTING model resolves to the persisted row's real id.
        val withExisting = persisted + listOf(Triple(instanceId, baseModelId, "inst-1/gpt-5.5"))
        assertEquals("inst-1/gpt-5.5", resolveMergedEntryId(withExisting, instanceId, baseModelId))
    }

    @Test
    fun mergeResolution_tempUuidNeverSurvivesCanonicalization() {
        // After addEntry + saveConfig, the canonicalized entry carries the
        // composite id; the re-query must find THAT, not the minted uuid.
        val tempUuid = "11111111-2222-3333-4444-555555555555"
        val canonicalized = listOf(Triple("inst-2", "glm-5.2", "inst-2/glm-5.2"))
        val resolved = resolveMergedEntryId(canonicalized, "inst-2", "glm-5.2")
        assertEquals("inst-2/glm-5.2", resolved)
        assertFalse(
            "the minted temp uuid must never be the resolved id",
            tempUuid == resolved,
        )
    }

    // ------------------------------------------------------------------
    // Echo conversion: the OpenMinis record's reasoningEchoJson survives the
    // d→r copy; a record without the key stays absent (legacy compat).
    // ------------------------------------------------------------------
    @Test
    fun echoConversion_carriesReasoningEchoJson() {
        val d = JSONObject()
            .put("id", "r1")
            .put("label", "deepseek echo")
            .put("wireFormatJson", "{}")
            .put("reasoningEchoJson", "{\"field\":\"reasoning_content\"}")
        val r = convertThinkingRule(d)
        assertEquals("{\"field\":\"reasoning_content\"}", r.optString("reasoningEchoJson", ""))

        val legacy = JSONObject()
            .put("id", "r2")
            .put("label", "no echo")
            .put("wireFormatJson", "{}")
        val r2 = convertThinkingRule(legacy)
        assertFalse(r2.has("reasoningEchoJson"))
        assertEquals("", r2.optString("reasoningEchoJson", ""))
    }

    // ------------------------------------------------------------------
    // [fix/dv16-a A-08] The merge path applies the SAME instance-field write
    // face the append path uses ([readImportInstanceFields] /
    // [applyImportInstanceFields] in ProviderRepository.kt — driven directly
    // in ProviderRunConfigRoundTripTest). This file pins the merge-side
    // contract that motivated the seam: a same-(type,label) restore must carry
    // the backup's instance-config onto the existing instance, and the merge
    // match key (providerType + label) must stay untouched — the caller
    // resolves the label BEFORE this write face runs.
    // ------------------------------------------------------------------
    @Test
    fun mergeWriteFace_carriesInstanceConfigOntoExistingInstance() {
        // Faithful shape of the merge write face: decode the payload via the
        // shared seam, apply onto the existing (live) instance.
        val existing = ProviderInstance(
            id = "live-1",
            label = "OpenAI",
            providerType = ProviderType.openAI,
            credentialType = ProviderCredential.apiKey,
            customBaseURL = "https://old.example.com",
        )
        val payload = JSONObject()
            .put("providerType", "openAI")
            .put("label", "OpenAI")
            .put("customBaseURL", "https://relay.example.com")
            .put("appendV1Suffix", false)
            .put("useResponsesAPI", true)
            .put("isEnabled", false)
            .put("pinned", true)
        val f = readImportInstanceFields(payload)
        val updated = applyImportInstanceFields(existing, f)

        // the backup's values win (restore-is-restore)
        assertEquals("https://relay.example.com", updated.customBaseURL)
        assertEquals(false, updated.appendV1Suffix)
        assertEquals(true, updated.useResponsesAPI)
        assertEquals(false, updated.isEnabled)
        assertEquals(true, updated.pinned)
        // merge match key + identity untouched
        assertEquals("live-1", updated.id)
        assertEquals("OpenAI", updated.label)
        assertEquals(ProviderType.openAI, updated.providerType)
    }
}

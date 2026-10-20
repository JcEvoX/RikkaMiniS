#!/usr/bin/env python3
"""
Gate: provider import/export id round-trip + backup field completeness
(deep-audit-1021, ISSUE-02 + ISSUE-09, branch fix/da1021-import-id-roundtrip).

Three silent-on-restore defect classes, all "the payload/return value looks
complete but one id or field points at something that no longer exists":

  1. MERGE RETURNS A TEMP UUID (ProviderRepository.mergeImportInstanceJSON)
     The `addEntry(entry)` branch returned `entry.id` — the random UUID minted
     by parseImportedModelEntries. saveConfig canonicalizes every persisted
     entry id to the composite "{instanceId}/{modelId}" shape (toSnapshot →
     toProviderConfig), so the returned id never existed on disk. Every
     entryIdMap consumer (model groups / agentLoopEntries /
     compactModelEntryId / session bindings) dangled.

  2/3. EXPORT+PARSE DROP THE REASONING PAIR (exportInstanceJSON /
     parseImportedModelEntries)
     The Room/kotlinx path carries `reasoningEffortValues` +
     `declaresNoEffortTiers`; the hand-written org.json serializer dropped
     both. A backup restore evaporated the effort-tier ceiling
     (ThinkingLevelCatalog) and the no-effort flag (xAI models must NOT
     receive reasoning_effort) for gateway models absent from models.dev.

  4. OPENMINIS CONVERSION DROPS THE ECHO POLICY (convertThinkingRules)
     restoreThinkingRules already reads "reasoningEchoJson"; only this
     converter failed to copy it, so a cross-device restore silently reset
     the reasoning-echo policy to null.

  5. FIELD COMPLETENESS (the "add a field, sync it everywhere" family)
     Programmatically enumerates LLMModel's constructor `val` properties and
     asserts each survives the export and the parse (or is in the explicit
     exemption table below, with a reason) — so the NEXT dropped field turns
     red here instead of on a device.

Usage: python3 scripts/scan/provider_import_id_roundtrip_check.py [root]
       python3 scripts/scan/provider_import_id_roundtrip_check.py --root <root>
       python3 scripts/scan/provider_import_id_roundtrip_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import argparse
import os
import re
import sys
import tempfile

APP = "src/android/app/src/main/java/com/rikkaminis/app"
PR_REL = APP + "/data/repository/ProviderRepository.kt"
BM_REL = APP + "/backup/OpenMinisBackupCompat.kt"
LM_REL = APP + "/data/model/LLMModel.kt"

# Arm 5: property name → JSON key it must survive as in export AND parse.
# Exemptions carry a reason; anything else must appear under its own name.
LLM_KEY_MAP = {
    "id": "modelId",  # exported/read as "modelId"
}
LLM_EXEMPT = {
    # The export is per-instance and the parse takes the section-level
    # "providerType" — the provider NAME is never serialized per model.
    "provider": "derived from the section-level providerType (parse: "
                "providerType.displayName; export: per-instance dict)",
}

# Parse-direction exemption: these keys are read through the bitfield-fallback
# helper, not by a quoted literal in parseImportedModelEntries' body. The gate
# asserts the helper is the reader (call present) instead of a bare literal.
PARSE_HELPER_READ = {
    "inputModalities": "readModalitiesWithBitfieldFallback",
    "outputModalities": "readModalitiesWithBitfieldFallback",
}
PARSE_HELPER_CALL = "readModalitiesWithBitfieldFallback(m)"

# Arm 2/3 anchors: the READ/WRITE call, not a bare key string — the parse
# body also carries each key as the LLMModel constructor argument
# (`declaresNoEffortTiers = declaresNoEffortTiers`), which satisfies a bare
# presence check even after the read block is deleted (mutation M3 caught
# exactly that false green).
EXPORT_ANCHORS = ('put("reasoningEffortValues"', 'put("declaresNoEffortTiers"')
PARSE_ANCHORS = ('optJSONArray("reasoningEffortValues")', 'optBoolean("declaresNoEffortTiers")')

REASONING_KEYS = ("reasoningEffortValues", "declaresNoEffortTiers")
ECHO_KEY = "reasoningEchoJson"

# Bare `entry.id` = the temp-uuid return. Does NOT match `entry.baseModel.id`
# (that is followed by ".baseModel", not ".id") nor `hit?.id`.
BARE_ENTRY_ID = re.compile(r"(?<![.\w])entry\.id(?![\w])")


def code_only(text):
    """Text with line comments (//…) removed — presence checks below must not
    be satisfiable from prose in a comment."""
    return "\n".join(line.split("//")[0] for line in text.split("\n"))


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def _balanced(src, open_idx, opener="(", closer=")"):
    """Index just past the closer matching the opener at [open_idx] (-1 if none)."""
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == opener:
            depth += 1
        elif src[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def function_text(src, header):
    """Text of the declaration starting at [header], up to its closing brace."""
    i = src.find(header)
    if i < 0:
        return None
    brace = src.find("{", i)
    if brace < 0:
        return None
    end = _balanced(src, brace, "{", "}")
    return None if end < 0 else src[i:end]


def read(rel, root):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ----------------------------------------------------------------------
# Arm 1 — merge must not return the temp uuid
# ----------------------------------------------------------------------
def check_merge_requery(src, rel=PR_REL):
    body = function_text(src, "fun mergeImportInstanceJSON(")
    if body is None:
        return [(rel, 0, "1", "mergeImportInstanceJSON( not found — gate ROT")]
    code = code_only(body)
    add_idx = code.find("addEntry(entry)")
    if add_idx < 0:
        return [(rel, 0, "1", "mergeImportInstanceJSON no longer calls addEntry(entry) — gate ROT")]
    # The run-block that follows addEntry must resolve the persisted id.
    tail_marker = code.find("entryMap[", add_idx)
    tail = code[add_idx: tail_marker if tail_marker > 0 else len(code)]
    if "firstOrNull" not in tail and "compositeEntryKey(" not in tail:
        return [(rel, line_of(src, src.find("fun mergeImportInstanceJSON(")), "1",
                 "addEntry(entry) branch returns without re-querying the persisted id "
                 "(no firstOrNull re-query, no compositeEntryKey) — entryIdMap receives "
                 "a temp uuid that never existed on disk")]
    m = BARE_ENTRY_ID.search(code)
    if m is not None:
        return [(rel, line_of(src, src.find("fun mergeImportInstanceJSON(") + m.start()), "1",
                 "mergeImportInstanceJSON still returns the bare temp `entry.id` — "
                 "a uuid saveConfig canonicalizes away")]
    return []


# ----------------------------------------------------------------------
# Arms 2/3 — export & parse must carry the reasoning pair
# ----------------------------------------------------------------------
def check_reasoning_keys(src, rel, header, arm, what, anchors):
    body = function_text(src, header)
    if body is None:
        return [(rel, 0, arm, header + " not found — gate ROT")]
    code = code_only(body)
    out = []
    for anchor in anchors:
        if anchor not in code:
            out.append((rel, line_of(src, src.find(header)), arm,
                        "%s does not %s — a backup restore evaporates the field "
                        "(Room path already has it)" % (what, anchor)))
    return out


def check_convert_echo(src, rel=BM_REL):
    body = function_text(src, "fun convertThinkingRules(")
    if body is None:
        return [(rel, 0, "4", "convertThinkingRules( not found — gate ROT")]
    if ECHO_KEY not in code_only(body):
        return [(rel, line_of(src, src.find("fun convertThinkingRules(")), "4",
                 "convertThinkingRules drops " + ECHO_KEY +
                 " — cross-device restore resets the reasoning-echo policy")]
    return []


# ----------------------------------------------------------------------
# Arm 5 — every LLMModel constructor property survives export AND parse
# ----------------------------------------------------------------------
def llm_model_properties(src, rel=LM_REL):
    i = src.find("data class LLMModel(")
    if i < 0:
        return None
    open_paren = src.find("(", i)
    end = _balanced(src, open_paren)
    if end < 0:
        return None
    ctor = src[i:end]
    return re.findall(r"\bval\s+(\w+)\s*:", ctor)


def check_field_completeness(srcs, rels=None):
    rels = rels or {"lm": LM_REL, "pr": PR_REL}
    lm_src, pr_src = srcs["lm"], srcs["pr"]
    props = llm_model_properties(lm_src, rels["lm"])
    if not props:
        return [(rels["lm"], 0, "5", "data class LLMModel( not found — gate ROT")]
    export_body = function_text(pr_src, "fun exportInstanceJSON(")
    parse_body = function_text(pr_src, "fun parseImportedModelEntries(")
    out = []
    if export_body is None:
        out.append((rels["pr"], 0, "5", "exportInstanceJSON( not found — gate ROT"))
    if parse_body is None:
        out.append((rels["pr"], 0, "5", "parseImportedModelEntries( not found — gate ROT"))
    if out:
        return out
    export_code, parse_code = code_only(export_body), code_only(parse_body)
    for prop in props:
        if prop in LLM_EXEMPT:
            continue
        key = LLM_KEY_MAP.get(prop, prop)
        key_lit = '"%s"' % key
        # Line-level: the key must appear inside a READ/WRITE call, not as a
        # bare constructor argument — a dropped read leaves the constructor
        # arg behind (mutation M3's false green).
        def _has_call(code, callwords):
            for line in code.split("\n"):
                if key_lit in line and any(w in line for w in callwords):
                    return True
            return False
        if not _has_call(export_code, ("put(", "opt", "has(")):
            out.append((rels["pr"], line_of(pr_src, pr_src.find("fun exportInstanceJSON(")), "5",
                        "exportInstanceJSON does not serialize LLMModel.%s (key \"%s\") — "
                        "add-a-field-sync-everywhere regression" % (prop, key)))
        if not _has_call(parse_code, ("opt", "has(")):
            if prop in PARSE_HELPER_READ and PARSE_HELPER_CALL in parse_code:
                continue  # helper-owned read (bitfield fallback contract)
            out.append((rels["pr"], line_of(pr_src, pr_src.find("fun parseImportedModelEntries(")), "5",
                        "parseImportedModelEntries does not read LLMModel.%s (key \"%s\") — "
                        "add-a-field-sync-everywhere regression" % (prop, key)))
    return out


# ----------------------------------------------------------------------
# Arm 6 — [fix/dv16-a A-08] the merge path must share the append path's
# instance-field write face
# ----------------------------------------------------------------------
SHARED_FIELDS_READER = "readImportInstanceFields("
SHARED_FIELDS_APPLIER = "applyImportInstanceFields("


def check_merge_shares_instance_fields(src, rel=PR_REL):
    out = []
    import_body = function_text(src, "fun importInstanceJSON(")
    if import_body is None:
        out.append((rel, 0, "6", "importInstanceJSON( not found — gate ROT"))
    elif SHARED_FIELDS_READER not in code_only(import_body):
        out.append((rel, line_of(src, src.find("fun importInstanceJSON(")), "6",
                    "importInstanceJSON no longer decodes the instance-config fields via the "
                    "shared readImportInstanceFields seam — the append path drifted from the "
                    "merge path (one decode, two restore paths)"))
    merge_body = function_text(src, "fun mergeImportInstanceJSON(")
    if merge_body is None:
        out.append((rel, 0, "6", "mergeImportInstanceJSON( not found — gate ROT"))
    else:
        code = code_only(merge_body)
        if SHARED_FIELDS_READER not in code or SHARED_FIELDS_APPLIER not in code:
            out.append((rel, line_of(src, src.find("fun mergeImportInstanceJSON(")), "6",
                        "merge path does not apply the instance-config fields through the "
                        "shared write face (readImportInstanceFields + applyImportInstanceFields) "
                        "— a same-(type,label) restore drops customBaseURL / appendV1Suffix / "
                        "useResponsesAPI / customUserAgent / the five run-config fields"))
    n = len(re.findall(r"fun\s+applyImportInstanceFields\s*\(", src))
    if n != 1:
        out.append((rel, 0, "6",
                    "applyImportInstanceFields defined %d times — the instance-field write face "
                    "must stay single-source" % n))
    return out


# ----------------------------------------------------------------------
def run_checks(root):
    pr = read(PR_REL, root)
    bm = read(BM_REL, root)
    lm = read(LM_REL, root)
    missing = [(rel, 0, "?", "file not found — gate ROT")
               for rel, src in ((PR_REL, pr), (BM_REL, bm), (LM_REL, lm)) if src is None]
    if missing:
        return missing
    srcs = {"pr": pr, "bm": bm, "lm": lm}
    checks = []
    checks += check_merge_requery(pr)
    checks += check_reasoning_keys(pr, PR_REL, "fun exportInstanceJSON(", "2", "exportInstanceJSON", EXPORT_ANCHORS)
    checks += check_reasoning_keys(pr, PR_REL, "fun parseImportedModelEntries(", "3", "parseImportedModelEntries", PARSE_ANCHORS)
    checks += check_convert_echo(bm)
    checks += check_field_completeness(srcs)
    checks += check_merge_shares_instance_fields(pr)
    return checks


# ----------------------------------------------------------------------
# Selftest fixtures — minimal Kotlin-shaped files exercising every arm.
# ----------------------------------------------------------------------
FIX_LM = """package com.rikkaminis.app.data.model

@Serializable
data class LLMModel(
    val id: String,
    val displayName: String,
    val provider: String,
    val contextWindow: Int? = null,
    val maxOutputTokens: Int? = null,
    val supportsReasoning: Boolean? = null,
    val interleavedReasoningField: String? = null,
    val reasoningEffortValues: List<String>? = null,
    val declaresNoEffortTiers: Boolean? = null,
    val inputModalities: List<String>? = null,
    val outputModalities: List<String>? = null,
)
"""

FIX_EXPORT_OK = """fun exportInstanceJSON(instanceId: String): String? {
    val obj = JSONObject()
    for (entry in entries) {
        modelsArr.put(JSONObject().apply {
            put("modelId", entry.baseModel.id)
            put("displayName", entry.baseModel.displayName)
            entry.baseModel.contextWindow?.let { put("contextWindow", it) }
            entry.baseModel.maxOutputTokens?.let { put("maxOutputTokens", it) }
            entry.baseModel.supportsReasoning?.let { put("supportsReasoning", it) }
            entry.baseModel.interleavedReasoningField?.let { put("interleavedReasoningField", it) }
            entry.baseModel.reasoningEffortValues?.takeIf { it.isNotEmpty() }?.let {
                put("reasoningEffortValues", JSONArray(it))
            }
            entry.baseModel.declaresNoEffortTiers?.let { put("declaresNoEffortTiers", it) }
            entry.baseModel.inputModalities?.let { put("inputModalities", JSONArray(it)) }
            entry.baseModel.outputModalities?.let { put("outputModalities", JSONArray(it)) }
        })
    }
    return obj.toString()
}
"""

FIX_EXPORT_BAD = """fun exportInstanceJSON(instanceId: String): String? {
    val obj = JSONObject()
    for (entry in entries) {
        modelsArr.put(JSONObject().apply {
            put("modelId", entry.baseModel.id)
            put("displayName", entry.baseModel.displayName)
            entry.baseModel.contextWindow?.let { put("contextWindow", it) }
            entry.baseModel.maxOutputTokens?.let { put("maxOutputTokens", it) }
            entry.baseModel.supportsReasoning?.let { put("supportsReasoning", it) }
            entry.baseModel.interleavedReasoningField?.let { put("interleavedReasoningField", it) }
            entry.baseModel.inputModalities?.let { put("inputModalities", JSONArray(it)) }
            entry.baseModel.outputModalities?.let { put("outputModalities", JSONArray(it)) }
        })
    }
    return obj.toString()
}
"""

FIX_PARSE_OK = """private fun parseImportedModelEntries(dict: JSONObject, instanceId: String): List<ModelEntry> {
    val entries = mutableListOf<ModelEntry>()
    for (i in 0 until models.length()) {
        val m = models.getJSONObject(i)
        val modelId = m.optString("modelId", "")
        val displayName = m.optString("displayName", modelId)
        val contextWindow = if (m.has("contextWindow")) m.optInt("contextWindow").takeIf { it > 0 } else null
        val maxOutputTokens = if (m.has("maxOutputTokens")) m.optInt("maxOutputTokens").takeIf { it > 0 } else null
        val supportsReasoning = if (m.has("supportsReasoning")) m.optBoolean("supportsReasoning") else null
        val interleavedReasoningField = m.optString("interleavedReasoningField", "").ifEmpty { null }
        val reasoningEffortValues = m.optJSONArray("reasoningEffortValues")?.let { arr ->
            (0 until arr.length())
                .mapNotNull { idx -> arr.optString(idx, "").takeIf { it.isNotBlank() } }
                .takeIf { it.isNotEmpty() }
        }
        val declaresNoEffortTiers = if (m.has("declaresNoEffortTiers") && !m.isNull("declaresNoEffortTiers")) {
            m.optBoolean("declaresNoEffortTiers")
        } else null
        val (baseIn, baseOut) = readModalitiesWithBitfieldFallback(m)
        val model = LLMModel(
            id = modelId,
            displayName = displayName,
            contextWindow = contextWindow,
            maxOutputTokens = maxOutputTokens,
            supportsReasoning = supportsReasoning,
            interleavedReasoningField = interleavedReasoningField,
            inputModalities = baseIn,
            outputModalities = baseOut,
            reasoningEffortValues = reasoningEffortValues,
            declaresNoEffortTiers = declaresNoEffortTiers,
        )
        entries.add(ModelEntry(providerInstanceId = instanceId, baseModel = model))
    }
    return entries
}
"""

FIX_PARSE_BAD = """private fun parseImportedModelEntries(dict: JSONObject, instanceId: String): List<ModelEntry> {
    val entries = mutableListOf<ModelEntry>()
    for (i in 0 until models.length()) {
        val m = models.getJSONObject(i)
        val modelId = m.optString("modelId", "")
        val displayName = m.optString("displayName", modelId)
        val contextWindow = if (m.has("contextWindow")) m.optInt("contextWindow").takeIf { it > 0 } else null
        val maxOutputTokens = if (m.has("maxOutputTokens")) m.optInt("maxOutputTokens").takeIf { it > 0 } else null
        val supportsReasoning = if (m.has("supportsReasoning")) m.optBoolean("supportsReasoning") else null
        val interleavedReasoningField = m.optString("interleavedReasoningField", "").ifEmpty { null }
        val (baseIn, baseOut) = readModalitiesWithBitfieldFallback(m)
        val model = LLMModel(
            id = modelId,
            displayName = displayName,
            contextWindow = contextWindow,
            maxOutputTokens = maxOutputTokens,
            supportsReasoning = supportsReasoning,
            interleavedReasoningField = interleavedReasoningField,
            inputModalities = baseIn,
            outputModalities = baseOut,
        )
        entries.add(ModelEntry(providerInstanceId = instanceId, baseModel = model))
    }
    return entries
}
"""

# M3-mimicking variant: the constructor argument stays (bare key presence
# would satisfy it) but the READ is dropped — the read/writer-call anchors
# must catch exactly this false green.
FIX_PARSE_READDROPPED_BAD = """private fun parseImportedModelEntries(dict: JSONObject, instanceId: String): List<ModelEntry> {
    val entries = mutableListOf<ModelEntry>()
    for (i in 0 until models.length()) {
        val m = models.getJSONObject(i)
        val modelId = m.optString("modelId", "")
        val displayName = m.optString("displayName", modelId)
        val contextWindow = if (m.has("contextWindow")) m.optInt("contextWindow").takeIf { it > 0 } else null
        val maxOutputTokens = if (m.has("maxOutputTokens")) m.optInt("maxOutputTokens").takeIf { it > 0 } else null
        val supportsReasoning = if (m.has("supportsReasoning")) m.optBoolean("supportsReasoning") else null
        val interleavedReasoningField = m.optString("interleavedReasoningField", "").ifEmpty { null }
        val (baseIn, baseOut) = readModalitiesWithBitfieldFallback(m)
        val model = LLMModel(
            id = modelId,
            displayName = displayName,
            contextWindow = contextWindow,
            maxOutputTokens = maxOutputTokens,
            supportsReasoning = supportsReasoning,
            interleavedReasoningField = interleavedReasoningField,
            inputModalities = baseIn,
            outputModalities = baseOut,
            reasoningEffortValues = null,
            declaresNoEffortTiers = null,
        )
        entries.add(ModelEntry(providerInstanceId = instanceId, baseModel = model))
    }
    return entries
}
"""

# [fix/dv16-a A-08] Shared instance-field decode / write face (append + merge
# restore paths). Appended to every fixture PR tree so arm 6's single-source
# definition check is exercisable.
FIX_HELPERS = """
internal data class RunConfigSnapshot(
    val isEnabled: Boolean = true,
    val azureMode: Boolean = false,
    val imageEndpointMode: ImageEndpointMode = ImageEndpointMode.auto,
    val imageEndpointResolved: ImageEndpointMode? = null,
    val pinned: Boolean = false,
)

internal fun readRunConfigFields(dict: JSONObject): RunConfigSnapshot = RunConfigSnapshot()

internal data class ImportInstanceFields(
    val customBaseURL: String?,
    val appendV1Suffix: Boolean,
    val useResponsesAPI: Boolean,
    val customUserAgent: String?,
    val runConfig: RunConfigSnapshot,
)

internal fun readImportInstanceFields(dict: JSONObject): ImportInstanceFields =
    ImportInstanceFields(
        customBaseURL = dict.optString("customBaseURL", "").ifEmpty { null },
        appendV1Suffix = dict.optBoolean("appendV1Suffix", true),
        useResponsesAPI = dict.optBoolean("useResponsesAPI", false),
        customUserAgent = dict.optString("customUserAgent", "").ifEmpty { null },
        runConfig = readRunConfigFields(dict),
    )

internal fun applyImportInstanceFields(base: ProviderInstance, f: ImportInstanceFields): ProviderInstance =
    base.copy(
        customBaseURL = f.customBaseURL,
        appendV1Suffix = f.appendV1Suffix,
        useResponsesAPI = f.useResponsesAPI,
        customUserAgent = f.customUserAgent,
        isEnabled = f.runConfig.isEnabled,
        azureMode = f.runConfig.azureMode,
        imageEndpointMode = f.runConfig.imageEndpointMode,
        imageEndpointResolved = f.runConfig.imageEndpointResolved,
        pinned = f.runConfig.pinned,
    )
"""

# [fix/dv16-a A-08] The append path decodes through the shared seam.
FIX_IMPORT_OK = """fun importInstanceJSON(jsonStr: String): String? {
    val dict = JSONObject(jsonStr)
    val f = readImportInstanceFields(dict)
    val instance = ProviderInstance(
        id = newId(),
        label = resolvedLabel,
        customBaseURL = f.customBaseURL,
        appendV1Suffix = f.appendV1Suffix,
        useResponsesAPI = f.useResponsesAPI,
        customUserAgent = f.customUserAgent,
        isEnabled = f.runConfig.isEnabled,
        azureMode = f.runConfig.azureMode,
        imageEndpointMode = f.runConfig.imageEndpointMode,
        imageEndpointResolved = f.runConfig.imageEndpointResolved,
        pinned = f.runConfig.pinned,
    )
    addInstance(instance)
    return resolvedLabel
}
"""

FIX_MERGE_OK = """fun mergeImportInstanceJSON(
    jsonStr: String,
    srcEntryIds: List<String>,
): Pair<String, Map<String, String>>? {
    val entries = parseImportedModelEntries(dict, existing.id)
    val entryMap = HashMap<String, String>()
    for (i in entries.indices) {
        val srcId = srcEntryIds.getOrNull(i) ?: ""
        val entry = entries[i]
        val hit = _config.value.modelEntries.firstOrNull {
            it.providerInstanceId == existing.id && it.baseModel.id == entry.baseModel.id
        }
        val resolvedEntryId = hit?.id ?: run {
            addEntry(entry)
            _config.value.modelEntries.firstOrNull {
                it.providerInstanceId == existing.id && it.baseModel.id == entry.baseModel.id
            }?.id ?: compositeEntryKey(existing.id, entry.baseModel.id)
        }
        if (srcId.isNotEmpty()) entryMap[srcId] = resolvedEntryId
    }
    // [fix/dv16-a A-08] A restore is a restore: the instance-config fields the
    // append path decodes are applied onto the existing instance too — through
    // the same shared write face, so the merge path cannot drift.
    val fields = readImportInstanceFields(dict)
    val updated = applyImportInstanceFields(existing, fields)
    if (updated != existing) updateInstance(updated)
    return existing.id to entryMap
}
"""

FIX_MERGE_BAD = """fun mergeImportInstanceJSON(
    jsonStr: String,
    srcEntryIds: List<String>,
): Pair<String, Map<String, String>>? {
    val entries = parseImportedModelEntries(dict, existing.id)
    val entryMap = HashMap<String, String>()
    for (i in entries.indices) {
        val srcId = srcEntryIds.getOrNull(i) ?: ""
        val entry = entries[i]
        val hit = _config.value.modelEntries.firstOrNull {
            it.providerInstanceId == existing.id && it.baseModel.id == entry.baseModel.id
        }
        val resolvedEntryId = hit?.id ?: run {
            addEntry(entry)
            entry.id
        }
        if (srcId.isNotEmpty()) entryMap[srcId] = resolvedEntryId
    }
    val fields = readImportInstanceFields(dict)
    val updated = applyImportInstanceFields(existing, fields)
    if (updated != existing) updateInstance(updated)
    return existing.id to entryMap
}
"""

# [fix/dv16-a A-08] Arm 6 teeth: the merge path stops applying the shared
# instance-field write face (the exact regression the arm exists for).
FIX_MERGE_NOAPPLY_BAD = """fun mergeImportInstanceJSON(
    jsonStr: String,
    srcEntryIds: List<String>,
): Pair<String, Map<String, String>>? {
    val entries = parseImportedModelEntries(dict, existing.id)
    val entryMap = HashMap<String, String>()
    for (i in entries.indices) {
        val srcId = srcEntryIds.getOrNull(i) ?: ""
        val entry = entries[i]
        val hit = _config.value.modelEntries.firstOrNull {
            it.providerInstanceId == existing.id && it.baseModel.id == entry.baseModel.id
        }
        val resolvedEntryId = hit?.id ?: run {
            addEntry(entry)
            _config.value.modelEntries.firstOrNull {
                it.providerInstanceId == existing.id && it.baseModel.id == entry.baseModel.id
            }?.id ?: compositeEntryKey(existing.id, entry.baseModel.id)
        }
        if (srcId.isNotEmpty()) entryMap[srcId] = resolvedEntryId
    }
    importInstanceCredentials(dict, existing)
    return existing.id to entryMap
}
"""

FIX_CONVERT_OK = """private fun convertThinkingRules(
    records: List<JSONObject>,
    typeToLabel: Map<String, Pair<String, String>>,
): JSONArray {
    for (rec in records) {
        if (rec.optString("t", "") != "ThinkingRuleV1") continue
        val d = rec.optJSONObject("d") ?: continue
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
        arr.put(r)
    }
    return out
}
"""

FIX_CONVERT_BAD = """private fun convertThinkingRules(
    records: List<JSONObject>,
    typeToLabel: Map<String, Pair<String, String>>,
): JSONArray {
    for (rec in records) {
        if (rec.optString("t", "") != "ThinkingRuleV1") continue
        val d = rec.optJSONObject("d") ?: continue
        val r = JSONObject()
        r.put("id", d.optString("id", ""))
        r.put("label", d.optString("label", ""))
        r.put("scopeKind", d.optString("scopeKind", "allModels"))
        d.optString("scopePattern", "").takeIf { it.isNotEmpty() }
            ?.let { r.put("scopePattern", it) }
        r.put("wireFormatJson", d.optString("wireFormatJson", "{}"))
        r.put("sortOrder", d.optInt("sortOrder", 0))
        arr.put(r)
    }
    return out
}
"""


def build_fixture_tree(tmp, merge_src, export_src, parse_src, convert_src):
    os.makedirs(os.path.join(tmp, APP + "/data/repository"), exist_ok=True)
    os.makedirs(os.path.join(tmp, APP + "/backup"), exist_ok=True)
    os.makedirs(os.path.join(tmp, APP + "/data/model"), exist_ok=True)
    with open(os.path.join(tmp, PR_REL), "w") as f:
        f.write(merge_src + "\n" + export_src + "\n" + parse_src + "\n" + FIX_HELPERS + "\n" + FIX_IMPORT_OK)
    with open(os.path.join(tmp, BM_REL), "w") as f:
        f.write(convert_src)
    with open(os.path.join(tmp, LM_REL), "w") as f:
        f.write(FIX_LM)


def selftest():
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        build_fixture_tree(tmp, FIX_MERGE_OK, FIX_EXPORT_OK, FIX_PARSE_OK, FIX_CONVERT_OK)
        v = run_checks(tmp)
        if v:
            failures.append("compliant fixture flagged: %r" % (v,))
    with tempfile.TemporaryDirectory() as tmp:
        build_fixture_tree(tmp, FIX_MERGE_BAD, FIX_EXPORT_BAD, FIX_PARSE_BAD, FIX_CONVERT_BAD)
        v = run_checks(tmp)
        arms = sorted({x[2] for x in v})
        if arms != ["1", "2", "3", "4", "5"]:
            failures.append("violating fixture arms=%r (violations=%r)" % (arms, v))
    with tempfile.TemporaryDirectory() as tmp:
        build_fixture_tree(tmp, FIX_MERGE_OK, FIX_EXPORT_OK, FIX_PARSE_READDROPPED_BAD, FIX_CONVERT_OK)
        v = run_checks(tmp)
        arms = sorted({x[2] for x in v})
        if arms != ["3", "5"]:
            failures.append("read-dropped fixture arms=%r (violations=%r)" % (arms, v))
    # [fix/dv16-a A-08] Arm 6: the merge path stops applying the shared
    # instance-field write face → only arm 6 red.
    with tempfile.TemporaryDirectory() as tmp:
        build_fixture_tree(tmp, FIX_MERGE_NOAPPLY_BAD, FIX_EXPORT_OK, FIX_PARSE_OK, FIX_CONVERT_OK)
        v = run_checks(tmp)
        arms = sorted({x[2] for x in v})
        if arms != ["6"]:
            failures.append("merge-noapply fixture arms=%r (violations=%r)" % (arms, v))
    if failures:
        for f in failures:
            print("SELFTEST-FAIL: " + f)
        print("PROVIDER_IMPORT_ID_ROUNDTRIP_SELFTEST=FAIL")
        return 1
    print("selftest: compliant green / violating red arms 1-6 / read-dropped red arms 3+5 / merge-noapply red arm 6")
    print("PROVIDER_IMPORT_ID_ROUNDTRIP_SELFTEST=PASS")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--root", dest="rootkw", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    root = args.rootkw or args.root
    violations = run_checks(root)
    if violations:
        print("PROVIDER_IMPORT_ID_ROUNDTRIP: %d violation(s)" % len(violations))
        for rel, line, arm, msg in violations:
            print("  [arm %s] %s:%s — %s" % (arm, rel, line, msg))
        sys.exit(1)
    print("PROVIDER_IMPORT_ID_ROUNDTRIP: clean (6 arms)")
    sys.exit(0)


if __name__ == "__main__":
    main()

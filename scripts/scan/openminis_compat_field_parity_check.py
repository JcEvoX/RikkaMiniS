#!/usr/bin/env python3
"""
Gate: OpenMinis .minisbak conversion field parity
       (deep-audit-1022 ISSUE-04, branch fix/da1022-openminis-compat).

The OpenMinis importer (OpenMinisBackupCompat) is a THIRD serializer of the same
backup contract as ProviderRepository.exportInstanceJSON / importInstanceJSON and
ConfigBackup. Adding a field to the Android export silently evaporates for every
cross-platform (.minisbak) restore unless the converter is updated too. This gate
derives the required key sets from the Android exporters and asserts the
converter still writes them — so the NEXT dropped field turns this red instead
of turning up on a device.

Arms
  1  appendV1Suffix polarity: default-TRUE on both sides (the RikkaMinis importer
     reads it with optBoolean("appendV1Suffix", true); the Android export writes
     the key only when false). The 3-state passthrough (has-guarded write of the
     explicit value) is required; the regression shape — a default-FALSE read
     feeding a write-when-true — is forbidden. A default-false read is correct
     for useResponsesAPI (whose import default IS false) and was copy-pasted onto
     this opposite-polarity field, inverting explicit `false` into `true` (every
     request then gained a spurious /v1 suffix).
  2  provider/model key parity: every key exportInstanceJSON writes must appear in
     convertProviders (exemptions listed with reasons below).
  3  chat-session key parity: every key ConfigBackup's session export block writes
     must appear in convertChats (exemptions listed with reasons below).
  4  [deep-audit-1039 A-07] model-group key parity: every key ConfigBackup's
     modelGroups export block writes must appear in convertGroups. The group
     run-config fields (strategy / fallbackStrategy / defaultThinkingLevel /
     contextLimitTokens / lastContextLimitTokens) had a decode-side fix
     (readGroupRunConfig / applyGroupRunConfig, [fix/dv18-4 A-07]) with no
     export-side guard: a group key dropped from buildSections would silently
     stop reaching the importer that now knows how to read it.

Usage:
    python3 scripts/scan/openminis_compat_field_parity_check.py <repo_root>
    python3 scripts/scan/openminis_compat_field_parity_check.py --root <repo_root>
    python3 scripts/scan/openminis_compat_field_parity_check.py --selftest
"""

import argparse
import os
import re
import sys
import tempfile

APP = "src/android/app/src/main/java/com/rikkaminis/app"
BM_REL = APP + "/backup/OpenMinisBackupCompat.kt"
PR_REL = APP + "/data/repository/ProviderRepository.kt"
CB_REL = APP + "/backup/ConfigBackup.kt"

CONV_HEADER = "private fun convertProviders("
GROUP_HEADER = "private fun convertGroups("
CHAT_HEADER = "private fun convertChats("
EXPORT_HEADER = "fun exportInstanceJSON("
SESSIONS_ANCHOR = "val sessionJsons = sessions.map"
# [deep-audit-1039 A-07] The provider run-config keys moved behind the pure
# JVM-testable seam writeRunConfigFields (RC5 / P0-pinned + GH#68), where they
# are written as `put(RCF_*, …)` — identifier form, invisible to PUT_KEY.
RCF_HEADER = "fun writeRunConfigFields("
RCF_CONST_RE = re.compile(r'(?:private\s+)?const\s+val\s+(RCF_\w+)\s*=\s*"([^"]+)"')
RCF_PUT_RE = re.compile(r"put\(\s*(RCF_\w+)\s*,")
# [deep-audit-1039 A-07] The modelGroups export block lives inside buildSections.
GROUPS_ANCHOR = "for (group in providerRepo.config.value.modelGroups)"

# Arm 1 anchors — code anchors, never comment text.
V1_REQUIRED_GUARD = 'has("appendV1Suffix")'
V1_REQUIRED_WRITE = 'put("appendV1Suffix", inst.optBoolean("appendV1Suffix"))'
V1_FORBIDDEN = re.compile(r'optBoolean\("appendV1Suffix",\s*false\)')

# Arm 2/3 exemptions. Direction is one-way (exporter key ⊆ converter body), so a
# converter-only key (oauth*, credential base64…) is NOT an exemption — it is a
# superset and passes by construction. Anything listed here needs a reason.
PROVIDER_EXEMPT = {
    # The overrides block is copied through wholesale from a key allow-list
    # (`arrayOf("maxThinkingLevel", …)`), so those keys legitimately appear as
    # bare quoted literals rather than per-key put( calls. Presence check below
    # is literal-based precisely so this stays green.
}
SESSION_EXEMPT = {}
GROUP_EXEMPT = {}

PUT_KEY = re.compile(r'put\("(\w+)"')


def code_only(text):
    """Text with line comments (//…) removed — key presence must not be
    satisfiable from prose."""
    return "\n".join(line.split("//")[0] for line in text.split("\n"))


def _balanced(src, open_idx, opener="{", closer="}"):
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
    i = src.find(header)
    if i < 0:
        return None
    brace = src.find("{", i)
    if brace < 0:
        return None
    end = _balanced(src, brace)
    return None if end < 0 else src[i:end]


def block_text(src, anchor):
    """Text of the enclosing brace block opened right after [anchor]."""
    i = src.find(anchor)
    if i < 0:
        return None
    brace = src.find("{", i)
    if brace < 0:
        return None
    end = _balanced(src, brace)
    return None if end < 0 else src[i:end]


def read(rel, root):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def missing_keys(required, body):
    return [k for k in sorted(required) if '"%s"' % k not in body]


def rcf_const_table(pr_src):
    """{RCF_<NAME>: "wireKey"} derived from ProviderRepository's own constants —
    never a hardcoded copy (the point of the arm is that a SIXTH run-config
    constant is covered the day it lands)."""
    return {name: key for name, key in RCF_CONST_RE.findall(pr_src)}


def write_run_config_keys(pr_src):
    """Wire keys written by writeRunConfigFields: the literal puts plus every
    RCF_* identifier put mapped through the derived constant table. Raises
    KeyError on an identifier the table cannot resolve — the caller turns that
    into a fail-closed ROT, because an unmapped identifier is exactly the shape
    that used to make this arm blind. Returns an empty set when the seam is
    absent (a tree that never had the delegation has nothing to lose)."""
    body = function_text(pr_src, RCF_HEADER)
    if body is None:
        return set()
    table = rcf_const_table(pr_src)
    keys = set(PUT_KEY.findall(body))
    for ident in RCF_PUT_RE.findall(body):
        if ident not in table:
            raise KeyError(ident)
        keys.add(table[ident])
    return keys


def run(root):
    """Returns (problems, checked_count)."""
    problems = []
    bm = read(BM_REL, root)
    pr = read(PR_REL, root)
    cb = read(CB_REL, root)
    if bm is None or pr is None or cb is None:
        return ["ROT: source file missing (bm=%s pr=%s cb=%s)"
                % (bm is not None, pr is not None, cb is not None)], 0

    conv = function_text(bm, CONV_HEADER)
    groups_conv = function_text(bm, GROUP_HEADER)
    chat = function_text(bm, CHAT_HEADER)
    exp = function_text(pr, EXPORT_HEADER)
    sess = block_text(cb, SESSIONS_ANCHOR)
    grp = block_text(cb, GROUPS_ANCHOR)
    for name, txt in (("convertProviders", conv), ("convertGroups", groups_conv),
                      ("convertChats", chat), ("exportInstanceJSON", exp),
                      ("sessionJsons", sess), ("modelGroups block", grp)):
        if txt is None:
            problems.append("ROT: anchor not found for %s" % name)
    if problems:
        return problems, 0

    conv_code = code_only(conv)
    chat_code = code_only(chat)
    groups_code = code_only(groups_conv)

    # Arm 1 — appendV1Suffix three-state polarity.
    if V1_REQUIRED_GUARD not in conv_code:
        problems.append(
            "arm1: convertProviders lost the 3-state guard %s"
            % V1_REQUIRED_GUARD)
    if V1_REQUIRED_WRITE not in conv_code:
        problems.append(
            "arm1: convertProviders lost the passthrough write %s"
            % V1_REQUIRED_WRITE)
    if V1_FORBIDDEN.search(conv_code):
        problems.append(
            "arm1: default-false read on a default-true field "
            "(optBoolean(\"appendV1Suffix\", false)) — explicit false would be "
            "dropped and re-imported as true")

    # Arm 2 — provider/model key parity (derived from the Android export).
    # [deep-audit-1039 A-07] exportInstanceJSON delegates the run-config keys to
    # writeRunConfigFields, so the derived set is literal puts ∪ delegated puts
    # (RCF_* identifiers mapped through the constants table). Fail-closed: a
    # delegation that cannot be resolved is reported, never silently skipped.
    try:
        run_config_keys = write_run_config_keys(pr)
    except KeyError as exc:
        problems.append(
            "ROT: exportInstanceJSON's run-config delegation is unreadable (%s) "
            "— the five RCF_* keys would silently drop out of arm2" % exc)
        run_config_keys = set()
    if "writeRunConfigFields(" in exp and not run_config_keys:
        problems.append(
            "ROT: exportInstanceJSON delegates to writeRunConfigFields but no "
            "run-config keys could be derived from it — arm2 lost its blind spot")
    if "writeRunConfigFields(" in pr and "writeRunConfigFields(" not in exp:
        problems.append(
            "arm2: exportInstanceJSON no longer calls writeRunConfigFields — the "
            "five run-config keys would stop being exported (converter keeps "
            "them for old backups, so parity alone cannot see the drop)")
    provider_keys = (set(PUT_KEY.findall(exp)) | run_config_keys) - set(PROVIDER_EXEMPT)
    gone = missing_keys(provider_keys, conv_code)
    if gone:
        problems.append("arm2: convertProviders dropped export key(s): %s"
                        % ", ".join(gone))

    # Arm 3 — chat-session key parity (derived from ConfigBackup's export block).
    session_keys = set(PUT_KEY.findall(sess)) - set(SESSION_EXEMPT)
    gone_s = missing_keys(session_keys, chat_code)
    if gone_s:
        problems.append("arm3: convertChats dropped session export key(s): %s"
                        % ", ".join(gone_s))

    # Arm 4 [deep-audit-1039 A-07] — model-group key parity. Exported from
    # ConfigBackup's modelGroups block; the OpenMinis side reads the same
    # provider_config.json shape, so the two key sets must agree.
    group_keys = set(PUT_KEY.findall(grp)) - set(GROUP_EXEMPT)
    gone_g = missing_keys(group_keys, groups_code)
    if gone_g:
        problems.append("arm4: convertGroups dropped modelGroups export key(s): %s"
                        % ", ".join(gone_g))

    return problems, len(provider_keys) + len(session_keys) + len(group_keys)


# --------------------------------------------------------------------------
# Self-test fixtures
# --------------------------------------------------------------------------

FIX_EXPORT = """fun exportInstanceJSON(instance: ProviderInstance): JSONObject {
    return JSONObject().apply {
        put("providerType", instance.providerType.slug)
        put("label", instance.label)
        put("appendV1Suffix", false)
        put("customBaseURL", instance.customBaseURL)
        put("models", modelsArr)
        put("costTier", 1)
    }
}
"""

# [deep-audit-1039 A-07] Export shape that delegates the run-config keys the way
# the real tree does (literal puts are invisible; the RCF_* identifiers carry
# the keys). The derived-set arm must see all five.
FIX_EXPORT_DELEGATING = """private const val RCF_IS_ENABLED = "isEnabled"
private const val RCF_AZURE_MODE = "azureMode"
private const val RCF_IMG_MODE = "imageEndpointMode"
private const val RCF_IMG_RESOLVED = "imageEndpointResolved"
private const val RCF_PINNED = "pinned"

internal fun writeRunConfigFields(obj: org.json.JSONObject, instance: ProviderInstance) {
    obj.put(RCF_IS_ENABLED, instance.isEnabled)
    obj.put(RCF_AZURE_MODE, instance.azureMode)
    obj.put(RCF_IMG_MODE, instance.imageEndpointMode.name)
    instance.imageEndpointResolved?.name?.let { obj.put(RCF_IMG_RESOLVED, it) }
    obj.put(RCF_PINNED, instance.pinned)
}

fun exportInstanceJSON(instance: ProviderInstance): JSONObject {
    return JSONObject().apply {
        put("providerType", instance.providerType.slug)
        put("label", instance.label)
        writeRunConfigFields(this, instance)
    }
}
"""

# Same, but with the delegation dropped — the pre-fix blind shape: literal
# extraction finds only the two keys, the five run-config keys vanish.
FIX_EXPORT_NO_DELEGATION = """private const val RCF_IS_ENABLED = "isEnabled"
private const val RCF_AZURE_MODE = "azureMode"
private const val RCF_IMG_MODE = "imageEndpointMode"
private const val RCF_IMG_RESOLVED = "imageEndpointResolved"
private const val RCF_PINNED = "pinned"

internal fun writeRunConfigFields(obj: org.json.JSONObject, instance: ProviderInstance) {
    obj.put(RCF_IS_ENABLED, instance.isEnabled)
    obj.put(RCF_AZURE_MODE, instance.azureMode)
    obj.put(RCF_IMG_MODE, instance.imageEndpointMode.name)
    instance.imageEndpointResolved?.name?.let { obj.put(RCF_IMG_RESOLVED, it) }
    obj.put(RCF_PINNED, instance.pinned)
}

fun exportInstanceJSON(instance: ProviderInstance): JSONObject {
    return JSONObject().apply {
        put("providerType", instance.providerType.slug)
        put("label", instance.label)
    }
}
"""

# A put(IDENT) the constants table cannot resolve — fail-closed, never a skip.
FIX_EXPORT_UNMAPPED_RCF = FIX_EXPORT_DELEGATING.replace(
    "    obj.put(RCF_PINNED, instance.pinned)",
    "    obj.put(RCF_SOMETHING_NEW, instance.pinned)")

FIX_SESSIONS = """fun exportData(): JSONArray {
    val sessionJsons = sessions.map { session ->
        JSONObject().apply {
            put("id", session.id)
            put("title", session.title)
            put("pinnedAt", session.pinnedAt)
            put("editCount", session.editCount)
        }
    }
    return sessionJsons
}
"""

# [deep-audit-1039 A-07] The modelGroups export block inside buildSections.
FIX_GROUPS = """    private suspend fun buildSections(repo: ProviderRepository): JSONObject {
        val groups = JSONArray()
        for (group in providerRepo.config.value.modelGroups) {
            groups.put(JSONObject().apply {
                put("id", group.id)
                put("name", group.name)
                put("memberEntryIds", JSONArray().apply {
                    for (eid in group.memberEntryIds) put(eid)
                })
                put("strategy", group.strategy.name)
                put("fallbackStrategy", group.fallbackStrategy.name)
                group.defaultThinkingLevel?.let { put("defaultThinkingLevel", it.name) }
                group.contextLimitTokens?.let { put("contextLimitTokens", it) }
                group.lastContextLimitTokens?.let { put("lastContextLimitTokens", it) }
            })
        }
        return JSONObject().put("groups", groups)
    }
"""

FIX_GROUPS_DROPPED = FIX_GROUPS.replace(
    '                put("fallbackStrategy", group.fallbackStrategy.name)\n', "")

FIX_CONV_OK = """private fun convertProviders(pc: JSONObject, secrets: JSONObject): JSONArray {
    val obj = JSONObject()
    obj.put("providerType", inst.optString("providerType", ""))
    obj.put("label", inst.optString("label", ""))
    if (inst.has("appendV1Suffix")) {
        obj.put("appendV1Suffix", inst.optBoolean("appendV1Suffix"))
    }
    if (inst.has("customBaseURL")) {
        obj.put("customBaseURL", inst.optString("customBaseURL"))
    }
    obj.put("isEnabled", inst.optBoolean("isEnabled", true))
    obj.put("azureMode", inst.optBoolean("azureMode", false))
    obj.put("imageEndpointMode", inst.optString("imageEndpointMode", "auto"))
    if (inst.has("imageEndpointResolved")) {
        obj.put("imageEndpointResolved", inst.optString("imageEndpointResolved"))
    }
    obj.put("pinned", inst.optBoolean("pinned", false))
    val mo = JSONObject()
    mo.put("displayName", model.optString("displayName", ""))
    if (e.has("costTier") && !e.isNull("costTier")) {
        mo.put("costTier", e.optInt("costTier"))
    }
    obj.put("models", modelsArr)
    return out
}
"""

# Delegated-key variant: identical coverage, so the arm is agnostic to HOW the
# converter spells its writes (decision, not spelling).
FIX_CONV_OK_DELEGATED = FIX_CONV_OK.replace(
    '    obj.put("isEnabled", inst.optBoolean("isEnabled", true))\n'
    '    obj.put("azureMode", inst.optBoolean("azureMode", false))\n'
    '    obj.put("imageEndpointMode", inst.optString("imageEndpointMode", "auto"))\n'
    '    if (inst.has("imageEndpointResolved")) {\n'
    '        obj.put("imageEndpointResolved", inst.optString("imageEndpointResolved"))\n'
    '    }\n'
    '    obj.put("pinned", inst.optBoolean("pinned", false))\n',
    '    if (inst.has("isEnabled")) obj.put("isEnabled", inst.optBoolean("isEnabled"))\n'
    '    obj.put("azureMode", inst.optBoolean("azureMode", false))\n'
    '    obj.put("imageEndpointMode", inst.optString("imageEndpointMode", "auto"))\n'
    '    obj.put("imageEndpointResolved", inst.optString("imageEndpointResolved"))\n'
    '    obj.put("pinned", inst.optBoolean("pinned", false))\n')

FIX_CONV_BAD_V1 = """private fun convertProviders(pc: JSONObject, secrets: JSONObject): JSONArray {
    val obj = JSONObject()
    obj.put("providerType", inst.optString("providerType", ""))
    obj.put("label", inst.optString("label", ""))
    if (inst.optBoolean("appendV1Suffix", false)) obj.put("appendV1Suffix", true)
    obj.put("customBaseURL", inst.optString("customBaseURL"))
    val mo = JSONObject()
    mo.put("displayName", model.optString("displayName", ""))
    mo.put("costTier", e.optInt("costTier"))
    obj.put("models", modelsArr)
    return out
}
"""

FIX_CONV_BAD_KEY = """private fun convertProviders(pc: JSONObject, secrets: JSONObject): JSONArray {
    val obj = JSONObject()
    obj.put("providerType", inst.optString("providerType", ""))
    obj.put("label", inst.optString("label", ""))
    if (inst.has("appendV1Suffix")) {
        obj.put("appendV1Suffix", inst.optBoolean("appendV1Suffix"))
    }
    obj.put("customBaseURL", inst.optString("customBaseURL"))
    val mo = JSONObject()
    mo.put("displayName", model.optString("displayName", ""))
    obj.put("models", modelsArr)
    return out
}
"""

# Drops exactly one delegated key — the regression this arm was built for.
FIX_CONV_BAD_RCF_PINNED = FIX_CONV_OK.replace(
    '    obj.put("pinned", inst.optBoolean("pinned", false))\n', "")

FIX_GROUPS_CONV_OK = """private fun convertGroups(pc: JSONObject): JSONArray {
    val groups = pc.optJSONArray("modelGroups") ?: return JSONArray()
    val out = JSONArray()
    for (i in 0 until groups.length()) {
        val g = groups.optJSONObject(i) ?: continue
        val obj = JSONObject()
        obj.put("id", g.optString("id", ""))
        obj.put("name", g.optString("name", ""))
        obj.put("memberEntryIds", g.optJSONArray("memberEntryIds") ?: JSONArray())
        obj.put("strategy", g.optString("strategy", "fallback"))
        obj.put("fallbackStrategy", g.optString("fallbackStrategy", "default"))
        g.optString("defaultThinkingLevel", "").takeIf { it.isNotEmpty() }
            ?.let { obj.put("defaultThinkingLevel", it) }
        if (g.optInt("contextLimitTokens", 0) > 0) {
            obj.put("contextLimitTokens", g.optInt("contextLimitTokens"))
        }
        if (g.optInt("lastContextLimitTokens", 0) > 0) {
            obj.put("lastContextLimitTokens", g.optInt("lastContextLimitTokens"))
        }
        out.put(obj)
    }
    return out
}
"""

FIX_GROUPS_CONV_BAD = FIX_GROUPS_CONV_OK.replace(
    '        obj.put("fallbackStrategy", g.optString("fallbackStrategy", "default"))\n', "")

FIX_CHAT_OK = """private fun convertChats(records: List<JSONObject>): JSONArray {
    for (rec in records) {
        val o = JSONObject()
        o.put("id", s.optString("id", ""))
        o.put("title", s.optString("title", ""))
        val pinnedRaw = if (d.has("pinnedAt")) d.opt("pinnedAt") else s.opt("pinnedAt")
        isoToMillis(pinnedRaw)?.takeIf { it > 0 }?.let { o.put("pinnedAt", it) }
        o.put("editCount", d.optInt("editCount", 0))
        out.put(o)
    }
    return out
}
"""

FIX_CHAT_BAD = """private fun convertChats(records: List<JSONObject>): JSONArray {
    for (rec in records) {
        val o = JSONObject()
        o.put("id", s.optString("id", ""))
        o.put("title", s.optString("title", ""))
        o.put("editCount", d.optInt("editCount", 0))
        out.put(o)
    }
    return out
}
"""


def build_fixture_tree(tmp, conv_src, chat_src, export_src=FIX_EXPORT,
                       sessions_src=FIX_SESSIONS, groups_export_src=FIX_GROUPS,
                       groups_conv_src=FIX_GROUPS_CONV_OK):
    os.makedirs(os.path.join(tmp, APP + "/backup"), exist_ok=True)
    os.makedirs(os.path.join(tmp, APP + "/data/repository"), exist_ok=True)
    with open(os.path.join(tmp, BM_REL), "w", encoding="utf-8") as f:
        f.write(conv_src + "\n" + groups_conv_src + "\n" + chat_src)
    with open(os.path.join(tmp, PR_REL), "w", encoding="utf-8") as f:
        f.write(export_src)
    with open(os.path.join(tmp, CB_REL), "w", encoding="utf-8") as f:
        f.write(sessions_src + "\n" + groups_export_src)
    return tmp


def selftest():
    ok = True

    def arm(name, conv_src, chat_src, expect_red, export_src=FIX_EXPORT,
            groups_export_src=FIX_GROUPS, groups_conv_src=FIX_GROUPS_CONV_OK,
            needle=None):
        nonlocal ok
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture_tree(tmp, conv_src, chat_src, export_src=export_src,
                               groups_export_src=groups_export_src,
                               groups_conv_src=groups_conv_src)
            problems, checked = run(tmp)
            red = len(problems) > 0
            verdict = "RED" if red else "GREEN"
            hit = ""
            if problems:
                hit = " :: " + problems[0].split(":")[0]
            good = red == expect_red and checked > 0
            if good and needle is not None:
                good = any(needle in p for p in problems)
            print("  [%s] %-40s expect=%s got=%s%s"
                  % ("ok" if good else "XX", name,
                     "RED" if expect_red else "GREEN", verdict, hit))
            if not good:
                ok = False

    # green fixtures — the fixed shape
    arm("provider+chat+group parity OK", FIX_CONV_OK, FIX_CHAT_OK, False)
    arm("delegated run-config keys seen", FIX_CONV_OK_DELEGATED, FIX_CHAT_OK, False,
        export_src=FIX_EXPORT_DELEGATING)
    # red fixtures — one per arm
    arm("appendV1Suffix single-state", FIX_CONV_BAD_V1, FIX_CHAT_OK, True)
    arm("provider key dropped", FIX_CONV_BAD_KEY, FIX_CHAT_OK, True)
    arm("session key dropped", FIX_CONV_OK, FIX_CHAT_BAD, True)
    # [deep-audit-1039 A-07] delegated-key arms. The delegation is what makes the
    # arm see the run-config keys at all; removing it is the pre-fix blind shape
    # (literal extraction finds only the two direct puts and silently drops five).
    arm("delegated RCF key dropped", FIX_CONV_BAD_RCF_PINNED, FIX_CHAT_OK, True,
        export_src=FIX_EXPORT_DELEGATING, needle="arm2: convertProviders dropped")
    arm("delegation call removed (pre-fix blind)", FIX_CONV_OK, FIX_CHAT_OK, True,
        export_src=FIX_EXPORT_NO_DELEGATION,
        needle="no longer calls")
    arm("unmapped RCF identifier fail-closed", FIX_CONV_OK, FIX_CHAT_OK, True,
        export_src=FIX_EXPORT_UNMAPPED_RCF, needle="ROT")
    # [deep-audit-1039 A-07] group parity arms
    arm("convertGroups key dropped", FIX_CONV_OK, FIX_CHAT_OK, True,
        groups_conv_src=FIX_GROUPS_CONV_BAD,
        needle="arm4: convertGroups dropped")
    # A NEW group key in the export with no converter write must red — the
    # direction the arm is for (next dropped field turns red, not silent).
    arm("new group key needs a converter write", FIX_CONV_OK, FIX_CHAT_OK, True,
        groups_export_src=FIX_GROUPS.replace(
            '                put("id", group.id)\n',
            '                put("id", group.id)\n'
            '                put("colorTag", group.colorTag)\n'),
        needle="arm4: convertGroups dropped")

    # rotation guard — a renamed anchor must red, never silently pass
    with tempfile.TemporaryDirectory() as tmp:
        build_fixture_tree(tmp, FIX_CONV_OK, FIX_CHAT_OK)
        p = os.path.join(tmp, BM_REL)
        with open(p, "r", encoding="utf-8") as f:
            src = f.read().replace(CONV_HEADER, "private fun convertProvidersV2(")
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
        problems, _ = run(tmp)
        red = len(problems) > 0 and any("ROT" in x for x in problems)
        print("  [%s] %-40s expect=RED got=%s"
              % ("ok" if red else "XX", "anchor rotation guard (convertProviders)",
                 "RED" if problems else "GREEN"))
        if not red:
            ok = False

    with tempfile.TemporaryDirectory() as tmp:
        build_fixture_tree(tmp, FIX_CONV_OK, FIX_CHAT_OK)
        p = os.path.join(tmp, BM_REL)
        with open(p, "r", encoding="utf-8") as f:
            src = f.read().replace(GROUP_HEADER, "private fun convertGroupsV2(")
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
        problems, _ = run(tmp)
        red = len(problems) > 0 and any("ROT" in x for x in problems)
        print("  [%s] %-40s expect=RED got=%s"
              % ("ok" if red else "XX", "anchor rotation guard (convertGroups)",
                 "RED" if problems else "GREEN"))
        if not red:
            ok = False

    with tempfile.TemporaryDirectory() as tmp:
        build_fixture_tree(tmp, FIX_CONV_OK, FIX_CHAT_OK)
        p = os.path.join(tmp, BM_REL)
        with open(p, "r", encoding="utf-8") as f:
            src = f.read().replace(CHAT_HEADER, "private fun convertChatsV2(")
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
        problems, _ = run(tmp)
        red = len(problems) > 0 and any("ROT" in x for x in problems)
        print("  [%s] %-40s expect=RED got=%s"
              % ("ok" if red else "XX", "anchor rotation guard (convertChats)",
                 "RED" if problems else "GREEN"))
        if not red:
            ok = False

    # fail-closed: the exportInstanceJSON anchor itself moved
    with tempfile.TemporaryDirectory() as tmp:
        build_fixture_tree(tmp, FIX_CONV_OK, FIX_CHAT_OK)
        p = os.path.join(tmp, PR_REL)
        with open(p, "r", encoding="utf-8") as f:
            src = f.read().replace(EXPORT_HEADER, "fun exportInstanceJsonV2(")
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
        problems, _ = run(tmp)
        red = len(problems) > 0 and any("ROT" in x for x in problems)
        print("  [%s] %-40s expect=RED got=%s"
              % ("ok" if red else "XX", "anchor rotation guard (exportInstanceJSON)",
                 "RED" if problems else "GREEN"))
        if not red:
            ok = False

    # fail-closed: the modelGroups export block itself is unreadable
    with tempfile.TemporaryDirectory() as tmp:
        build_fixture_tree(tmp, FIX_CONV_OK, FIX_CHAT_OK)
        p = os.path.join(tmp, CB_REL)
        with open(p, "r", encoding="utf-8") as f:
            src = f.read()
        with open(p, "w", encoding="utf-8") as f:
            f.write(src.replace(GROUPS_ANCHOR, "for (g in providerRepo.modelGroups)"))
        problems, _ = run(tmp)
        red = len(problems) > 0 and any("ROT" in x for x in problems)
        print("  [%s] %-40s expect=RED got=%s"
              % ("ok" if red else "XX", "modelGroups anchor rotation guard",
                 "RED" if problems else "GREEN"))
        if not red:
            ok = False

    # ping-pong: green fixture stays green after the dirty fixture (no state leak)
    arm("re-green after dirty", FIX_CONV_OK, FIX_CHAT_OK, False)

    print("OPENMINIS_COMPAT_FIELD_PARITY_SELFTEST=%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=None)
    ap.add_argument("--root", dest="rootkw", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    root = args.rootkw or args.root
    if not root:
        print("usage: openminis_compat_field_parity_check.py <repo_root>")
        sys.exit(2)
    if not os.path.isdir(root):
        print("not a directory: %s" % root)
        sys.exit(2)
    problems, checked = run(root)
    if problems:
        for p in problems:
            print("openminis-compat-parity: %s" % p)
        sys.exit(1)
    print("openminis-compat-parity: OK (%d keys pinned)" % checked)
    sys.exit(0)


if __name__ == "__main__":
    main()

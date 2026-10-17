#!/usr/bin/env python3
"""
Gate: OpenMinis .minisbak conversion field parity
       (deep-audit-1022 ISSUE-04, branch fix/da1022-openminis-compat).

The OpenMinis importer (OpenMinisBackupCompat) is a THIRD serializer of the same
backup contract as ProviderRepository.exportInstanceJSON / importInstanceJSON and
ConfigBackup. Adding a field to the Android export silently evaporates for every
cross-platform (.minisbak) restore unless the converter is updated too. This gate
derives the required key sets from the Android exporters and asserts the
converter still writes them — so the NEXT dropped field turns this red instead of
turning up on a device.

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
CHAT_HEADER = "private fun convertChats("
EXPORT_HEADER = "fun exportInstanceJSON("
SESSIONS_ANCHOR = "val sessionJsons = sessions.map"

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
    chat = function_text(bm, CHAT_HEADER)
    exp = function_text(pr, EXPORT_HEADER)
    sess = block_text(cb, SESSIONS_ANCHOR)
    for name, txt in (("convertProviders", conv), ("convertChats", chat),
                      ("exportInstanceJSON", exp), ("sessionJsons", sess)):
        if txt is None:
            problems.append("ROT: anchor not found for %s" % name)
    if problems:
        return problems, 0

    conv_code = code_only(conv)
    chat_code = code_only(chat)

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
    provider_keys = set(PUT_KEY.findall(exp)) - set(PROVIDER_EXEMPT)
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

    return problems, len(provider_keys) + len(session_keys)


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
    val mo = JSONObject()
    mo.put("displayName", model.optString("displayName", ""))
    if (e.has("costTier") && !e.isNull("costTier")) {
        mo.put("costTier", e.optInt("costTier"))
    }
    obj.put("models", modelsArr)
    return out
}
"""

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
                       sessions_src=FIX_SESSIONS):
    os.makedirs(os.path.join(tmp, APP + "/backup"), exist_ok=True)
    os.makedirs(os.path.join(tmp, APP + "/data/repository"), exist_ok=True)
    with open(os.path.join(tmp, BM_REL), "w", encoding="utf-8") as f:
        f.write(conv_src + "\n" + chat_src)
    with open(os.path.join(tmp, PR_REL), "w", encoding="utf-8") as f:
        f.write(export_src)
    with open(os.path.join(tmp, CB_REL), "w", encoding="utf-8") as f:
        f.write(sessions_src)
    return tmp


def selftest():
    ok = True

    def arm(name, conv_src, chat_src, expect_red):
        nonlocal ok
        with tempfile.TemporaryDirectory() as tmp:
            build_fixture_tree(tmp, conv_src, chat_src)
            problems, checked = run(tmp)
            red = len(problems) > 0
            verdict = "RED" if red else "GREEN"
            hit = ""
            if problems:
                hit = " :: " + problems[0].split(":")[0]
            print("  [%s] %-34s expect=%s got=%s%s"
                  % ("ok" if red == expect_red else "XX", name,
                     "RED" if expect_red else "GREEN", verdict, hit))
            if red != expect_red or checked <= 0:
                ok = False

    # green fixtures — the fixed shape
    arm("provider+chat parity OK", FIX_CONV_OK, FIX_CHAT_OK, False)
    # red fixtures — one per arm
    arm("appendV1Suffix single-state", FIX_CONV_BAD_V1, FIX_CHAT_OK, True)
    arm("provider key dropped", FIX_CONV_BAD_KEY, FIX_CHAT_OK, True)
    arm("session key dropped", FIX_CONV_OK, FIX_CHAT_BAD, True)

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
        print("  [%s] %-34s expect=RED got=%s"
              % ("ok" if red else "XX", "anchor rotation guard",
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

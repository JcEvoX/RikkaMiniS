#!/usr/bin/env python3
"""ARCH-01 gate — provider write rules are single-sourced (ProviderWriteRules).

Companion to `provider_rpc_symmetry_check.py`. That gate is deliberately
*shape-tolerant* (it accepts both the pre-ARCH-01 inline predicate + literal
message and the post-ARCH-01 shared-constant form, so it does not fire on
unrelated in-flight edits). This gate pins the ARCH-01 end state itself: the
shared rule layer must exist, must be declared exactly once, and every write
face must route through it instead of carrying a copy.

Criteria (all fail-closed — a missing file or missing anchor is RED, never a
silent pass):
  W1  `ProviderWriteRules.kt` declares CUSTOM_BASE_URL_OPENROUTER_MESSAGE,
      isCustomBaseURLWriteAllowed, normalizeCustomModelId and
      canonicalModelProviderName.
  W2  CUSTOM_BASE_URL_OPENROUTER_MESSAGE is declared exactly once across the
      main source set (a second copy in ProvidersCollection /
      ProviderMutationMethods is the drift this whole layer exists to stop).
  W3  CUSTOM_USER_AGENT_OAUTH_MESSAGE is declared exactly once (it stays in
      ProvidersCollection.kt — `memory_gate_provider_fields_check` pins that
      home; ARCH-01 must not move it out).
  W4  ProviderMutationMethods.kt carries NO literal copy of either message —
      both faces must reference the shared constants.
  W5  The debug mutation faces reference the shared rules: instancesCreate and
      instancesUpdate bodies each contain CUSTOM_BASE_URL_OPENROUTER_MESSAGE,
      and the customUserAgent guard sites call customUserAgentWritable(.
  W6  modelsAdd() uses normalizeCustomModelId( and canonicalModelProviderName(
      and no longer builds the label inline.
  W7  ProvidersCollection.kt's `customBaseURL(id)` writer routes through
      isCustomBaseURLWriteAllowed( — pins ARCH-01's wiring decision, which the
      shape-tolerant symmetry gate intentionally cannot.

  W8  [deep-audit-1039 A-04] `ProviderWriteRules.kt` declares the
      normalization single source `normalizeCustomBaseURL`, and every
      customBaseURL write face consumes it instead of spelling its own
      blank/trim handling. Faces checked: the collection field writer
      (ProvidersCollection), the add payload path
      (`resolveAddCustomBaseURL`), and the debug
      `provider.instances.create` / `instancesUpdate` bodies
      (ProviderMutationMethods). fail-closed: an unknown/moved anchor is RED.

Usage:  provider_write_rules_check.py <repo-root>
        provider_write_rules_check.py --selftest
"""

import json
import os
import re
import shutil
import sys
import tempfile

SELFTEST_MARKER = "PROVIDER_WRITE_RULES_SELFTEST=PASS"

PWR_REL = "src/android/app/src/main/java/com/rikkaminis/app/config/collections/ProviderWriteRules.kt"
PC_REL = "src/android/app/src/main/java/com/rikkaminis/app/config/collections/ProvidersCollection.kt"
PM_REL = "src/android/app/src/main/java/com/rikkaminis/app/debug/ProviderMutationMethods.kt"
PCF_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/model/ProviderConfig.kt"

# [deep-audit-1039 A-04] The normalization single source, and the bare
# blank-handling spellings the customBaseURL faces used to carry on their own.
NORMALIZE_DECL = "internal fun normalizeCustomBaseURL"
NORMALIZE_CALL = "normalizeCustomBaseURL("
# A customBaseURL write face that still trims/collapses inline is the drift W8
# exists to stop. The check is scoped to the lines that TOUCH customBaseURL:
# apiKey / customUserAgent / oauthToken blank handling is a different field's
# business and must not make this criterion fire (a criterion that reds the
# tree for an unrelated sibling is a broken criterion, not a strict one).
INLINE_BLANK_PATTERNS = (
    ".ifEmpty { null }",
    "takeIf { it.isNotBlank() }",
)
# Faces that must consume the shared helper (label, file-relative path, code).
# `add` is checked inside ProvidersCollection (resolveAddCustomBaseURL body),
# so the PC-side entry is the field writer only.
BASE_URL_FACES = (
    ("ProvidersCollection.customBaseURL writer", "PC"),
    ("ProvidersCollection.resolveAddCustomBaseURL", "PC"),
    ("ProviderMutationMethods.instancesCreate", "PM"),
    ("ProviderMutationMethods.instancesUpdate", "PM"),
)


def inline_blank_on_base_url_lines(body):
    """Lines of `body` that mention customBaseURL AND spell blank handling
    inline — the drift W8 forbids. Lines about other fields are ignored."""
    offenders = []
    for line in body.splitlines():
        if "customBaseURL" not in line:
            continue
        for pat in INLINE_BLANK_PATTERNS:
            if pat in line:
                offenders.append((line.strip()[:90], pat))
    return offenders

SRC_SCAN_ROOT = "src/android/app/src/main/java"

BASE_URL_MSG_DECL = "internal const val CUSTOM_BASE_URL_OPENROUTER_MESSAGE"
UA_MSG_DECL = "internal const val CUSTOM_USER_AGENT_OAUTH_MESSAGE"

OPENROUTER_MSG = "customBaseURL is not supported for providerType=openRouter"
UA_MSG = "Custom User-Agent is only supported for third-party API-key providers."

SHARED_RULE_DECLS = (
    "internal const val CUSTOM_BASE_URL_OPENROUTER_MESSAGE",
    "internal fun isCustomBaseURLWriteAllowed",
    "internal fun normalizeCustomModelId",
    "internal fun canonicalModelProviderName",
)


def strip_comments(text):
    """Remove // and /* */ comments, respecting string literals."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def read_code(path):
    raw = read(path)
    return None if raw is None else strip_comments(raw)


def extract_function(code, name):
    """Body of `fun <name>(...)` — handles `{ }` and `= <expr>` bodies and an
    optional single-line return-type annotation (`: Foo` / `: Foo<Bar>`)."""
    m = re.search(
        r"\bfun\s+%s\s*\((?:[^()]|\([^()]*\))*\)\s*(?::[^\n={]+)?[={]" % re.escape(name),
        code,
    )
    if not m:
        return None
    opener = m.group(0)[-1]
    j = m.end() - 1
    if opener == "=":
        # expression body — skip whitespace/newlines (the body may begin on the
        # following line), then scan with bracket awareness so a multi-line call
        # such as `= ClosureField(\n  writer = { … },\n)` is captured whole.
        k = j + 1
        while k < len(code) and code[k] in " \t\r\n":
            k += 1
        depth = 0
        opened = False
        while k < len(code):
            c = code[k]
            if c in "([{":
                depth += 1
                opened = True
            elif c in ")]}":
                depth -= 1
                if opened and depth == 0:
                    return code[j:k + 1]
            elif c == "\n" and not opened:
                return code[j:k]
            k += 1
        return code[j:]
    depth = 0
    k = j
    while k < len(code):
        if code[k] == "{":
            depth += 1
        elif code[k] == "}":
            depth -= 1
            if depth == 0:
                return code[j:k + 1]
        k += 1
    return None


def count_decl(root, needle):
    """Occurrences of `needle` across every .kt file under SRC_SCAN_ROOT."""
    hits = []
    base = os.path.join(root, SRC_SCAN_ROOT)
    for dirpath, _dirnames, filenames in os.walk(base):
        for fn in filenames:
            if not fn.endswith(".kt"):
                continue
            p = os.path.join(dirpath, fn)
            code = read_code(p)
            if code is None:
                continue
            c = code.count(needle)
            if c:
                hits.append((os.path.relpath(p, root), c))
    return hits


def check_tree(root):
    detail = {k: [] for k in ("w1", "w2", "w3", "w4", "w5", "w6", "w7",
                               "w8", "w9", "fail_closed")}

    pwr = read_code(os.path.join(root, PWR_REL))
    pc = read_code(os.path.join(root, PC_REL))
    pm = read_code(os.path.join(root, PM_REL))
    if pwr is None:
        detail["fail_closed"].append("%s missing" % PWR_REL)
    if pc is None:
        detail["fail_closed"].append("%s missing" % PC_REL)
    if pm is None:
        detail["fail_closed"].append("%s missing" % PM_REL)
    if detail["fail_closed"]:
        return 1, detail

    # ---- W1 ----
    for decl in SHARED_RULE_DECLS:
        if decl not in pwr:
            detail["w1"].append("ProviderWriteRules lacks %r" % decl)

    # ---- W2 / W3 ----
    base_hits = count_decl(root, BASE_URL_MSG_DECL)
    ua_hits = count_decl(root, UA_MSG_DECL)
    if sum(c for _, c in base_hits) != 1:
        detail["w2"].append("CUSTOM_BASE_URL_OPENROUTER_MESSAGE declared %d times: %s"
                            % (sum(c for _, c in base_hits), base_hits))
    elif base_hits[0][0] != PWR_REL:
        detail["w2"].append("declared in %s, expected %s" % (base_hits[0][0], PWR_REL))
    if sum(c for _, c in ua_hits) != 1:
        detail["w3"].append("CUSTOM_USER_AGENT_OAUTH_MESSAGE declared %d times: %s"
                            % (sum(c for _, c in ua_hits), ua_hits))
    elif ua_hits[0][0] != PC_REL:
        detail["w3"].append("declared in %s, expected %s (memory-gate home)"
                            % (ua_hits[0][0], PC_REL))

    # ---- W4 ----
    if OPENROUTER_MSG in pm:
        detail["w4"].append("ProviderMutationMethods still carries an openRouter message literal")
    if UA_MSG in pm:
        detail["w4"].append("ProviderMutationMethods still carries a customUserAgent message literal")

    # ---- W5 ----
    for fname in ("instancesCreate", "instancesUpdate"):
        body = extract_function(pm, fname)
        if body is None:
            detail["fail_closed"].append("%s not found in ProviderMutationMethods" % fname)
            continue
        if "CUSTOM_BASE_URL_OPENROUTER_MESSAGE" not in body:
            detail["w5"].append("%s does not reference the shared base-URL message" % fname)
        if "customUserAgentWritable(" not in body:
            detail["w5"].append("%s does not route the UA guard through customUserAgentWritable(" % fname)

    # ---- W6 ----
    models_add = extract_function(pm, "modelsAdd")
    if models_add is None:
        detail["fail_closed"].append("modelsAdd not found in ProviderMutationMethods")
    else:
        if "normalizeCustomModelId(" not in models_add:
            detail["w6"].append("modelsAdd does not use normalizeCustomModelId(")
        if "canonicalModelProviderName(" not in models_add:
            detail["w6"].append("modelsAdd does not use canonicalModelProviderName(")
        if "instance.providerType.displayName" in models_add:
            detail["w6"].append("modelsAdd still builds the provider label inline")

    # ---- W7 ----
    writer = extract_function(pc, "customBaseURL")
    if writer is None:
        detail["fail_closed"].append("customBaseURL(id) writer not found in ProvidersCollection")
    else:
        if "isCustomBaseURLWriteAllowed(" not in writer:
            detail["w7"].append("customBaseURL writer does not route through isCustomBaseURLWriteAllowed(")

    # ---- W8 [deep-audit-1039 A-04] normalization is single-sourced ----
    if NORMALIZE_DECL not in pwr:
        detail["w8"].append("ProviderWriteRules lacks %r (normalization single source)" % NORMALIZE_DECL)

    face_code = {
        "PC": pc,
        "PM": pm,
    }
    for label, key in BASE_URL_FACES:
        code = face_code[key]
        if label.endswith("writer"):
            body = extract_function(code, "customBaseURL")
        elif label.endswith("resolveAddCustomBaseURL"):
            body = extract_function(code, "resolveAddCustomBaseURL")
        else:
            body = extract_function(code, label.split(".")[-1])
        if body is None:
            detail["fail_closed"].append("%s: face body not found — re-anchor W8" % label)
            continue
        if NORMALIZE_CALL not in body:
            detail["w8"].append("%s does not consume %s" % (label, NORMALIZE_CALL))
        for line_text, pat in inline_blank_on_base_url_lines(body):
            detail["w8"].append("%s spells blank handling inline on a customBaseURL line (%r): %s"
                                % (label, pat, line_text))

    # No other file in the main source set may carry a second normalization
    # helper: exactly one declaration tree-wide.
    norm_decls = count_decl(root, NORMALIZE_DECL)
    if sum(c for _, c in norm_decls) != 1:
        detail["w8"].append("normalizeCustomBaseURL declared %d times: %s"
                            % (sum(c for _, c in norm_decls), norm_decls))
    elif norm_decls[0][0] != PWR_REL:
        detail["w8"].append("normalizeCustomBaseURL declared in %s, expected %s"
                            % (norm_decls[0][0], PWR_REL))

    # ---- W9 [deep-audit-1039 A-04] the read side trims before use ----
    pcf = read_code(os.path.join(root, PCF_REL))
    if pcf is None:
        detail["fail_closed"].append("%s missing" % PCF_REL)
    else:
        getter = effective_base_getter(pcf)
        if getter is None:
            detail["fail_closed"].append("effectiveBaseURL getter not found in ProviderConfig.kt — re-anchor W9")
        elif ".trim()" not in getter:
            detail["w9"].append("effectiveBaseURL does not trim the stored value before use")
        elif "trimEnd('/')" not in getter:
            detail["w9"].append("effectiveBaseURL lost its trailing-slash collapse")

    bad = any(detail[k] for k in detail if k != "fail_closed") or detail["fail_closed"]
    return (1 if bad else 0), detail


def effective_base_getter(code):
    """Body of the `effectiveBaseURL` property getter — brace-matched, because
    a property getter is not a `fun` and extract_function cannot find it."""
    i = code.find("val effectiveBaseURL")
    if i < 0:
        return None
    j = code.find("get() {", i)
    if j < 0:
        return None
    k = code.index("{", j)
    depth = 0
    while k < len(code):
        if code[k] == "{":
            depth += 1
        elif code[k] == "}":
            depth -= 1
            if depth == 0:
                return code[i:k + 1]
        k += 1
    return None


def replace_in(rel, old, new):
    """Selftest fixture builder: replace `old` with `new` in file `rel`.
    fail-closed — a missing anchor aborts the whole selftest rather than
    silently producing a fixture that mutates nothing."""
    def fn(r, text):
        if r != rel:
            return text
        assert old in text, "fixture anchor missing: %r" % old[:60]
        return text.replace(old, new)
    return fn


def compose(*fns):
    def fn(r, text):
        for f in fns:
            text = f(r, text)
        return text
    return fn


def run_selftest():
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.abspath(os.path.join(here, "..", ".."))
    real = {}
    for rel in (PWR_REL, PC_REL, PM_REL, PCF_REL):
        code = read(os.path.join(repo, rel))
        if code is None:
            print("SELFTEST cannot load %s" % rel)
            return 1
        real[rel] = code

    results = []

    def run(name, mutate=None, expect=()):
        root = tempfile.mkdtemp(prefix="pwrselftest-")
        try:
            for rel, text in real.items():
                dest = os.path.join(root, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(mutate(rel, text) if mutate else text)
            rc, detail = check_tree(root)
            got = tuple(sorted(k for k in detail if detail[k]))
            exp = tuple(sorted(expect))
            ok = (rc == 1 if exp else rc == 0) and got == exp
            results.append((name, ok, rc, got, exp))
        finally:
            shutil.rmtree(root)

    run("S0-clean-tree-green")

    def dup_decl(rel, text):
        if rel == PC_REL:
            return text + ("\n\ninternal const val CUSTOM_BASE_URL_OPENROUTER_MESSAGE: String =\n"
                           '    "customBaseURL is not supported for providerType=openRouter"\n')
        return text

    run("S1-duplicate-constant-declaration-W2", dup_decl, ("w2",))

    def literal_copy(rel, text):
        if rel == PM_REL:
            return text.replace(
                "throw RPCException(-32602, CUSTOM_BASE_URL_OPENROUTER_MESSAGE)",
                'throw RPCException(-32602, "customBaseURL is not supported for providerType=openRouter")')
        return text

    run("S2-message-literal-copy-back-W4", literal_copy, ("w4", "w5"))

    def ua_literal_copy(rel, text):
        if rel == PM_REL:
            return text.replace(
                "throw RPCException(-32602, CUSTOM_USER_AGENT_OAUTH_MESSAGE)",
                'throw RPCException(-32602, "Custom User-Agent is only supported for third-party '
                'API-key providers. OAuth providers (Anthropic/Codex) use their own authentication '
                'UA and cannot be overridden.")')
        return text

    run("S3-ua-message-literal-copy-back-W4", ua_literal_copy, ("w4",))

    def writer_inline(rel, text):
        if rel == PC_REL:
            return text.replace(
                """                if (!isCustomBaseURLWriteAllowed(inst.providerType, newBase)) {""",
                """                if (!inst.providerType.supportsCustomBaseURL) {""")
        return text

    run("S4-writer-inline-predicate-W7", writer_inline, ("w7",))

    def models_add_revert(rel, text):
        if rel == PM_REL:
            return text.replace(
                """        val modelId = normalizeCustomModelId(params.optString("modelId", ""))
            ?: throw RPCException(-32602, "Missing 'modelId' param")""",
                """        val modelId = params.optString("modelId", "").ifEmpty {
            throw RPCException(-32602, "Missing 'modelId' param")
        }""").replace(
                "provider = canonicalModelProviderName(instance.providerType),",
                "provider = instance.providerType.displayName,")
        return text

    run("S5-modelsAdd-old-shape-W6", models_add_revert, ("w6",))

    def helper_removed(rel, text):
        if rel == PWR_REL:
            return text.replace("internal fun canonicalModelProviderName(", "internal fun unusedHelper(")
        return text

    run("S6-shared-rule-missing-W1", helper_removed, ("w1",))

    # ─── [deep-audit-1039 A-04] W8/W9 fixtures ─────────────────────────────
    # Every fixture is a "breaks exactly this criterion" mutation of the REAL
    # tree files, so each new criterion has a tooth of its own.

    def run_real(name, mutate_map, expect):
        root = tempfile.mkdtemp(prefix="pwrselftest-w8-")
        try:
            for rel, text in real.items():
                dest = os.path.join(root, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                mutated = mutate_map(rel, text) if callable(mutate_map) else (
                    mutate_map[rel] if rel in mutate_map else text)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(mutated)
            rc, detail = check_tree(root)
            got = tuple(sorted(k for k in detail if detail[k]))
            exp = tuple(sorted(expect))
            ok = (rc == 1 if exp else rc == 0) and got == exp
            results.append((name, ok, rc, got, exp))
        finally:
            shutil.rmtree(root)

    # S7: the shared helper disappears (declaration renamed) — W1 AND W8 red
    # (W1 pins the declaration list, W8 pins the same declaration by name).
    def helper_gone2(rel, text):
        if rel == PWR_REL:
            return text.replace(
                "internal fun normalizeCustomBaseURL(raw: String?): String?",
                "internal fun retiredNormalize(raw: String?): String?")
        return text

    # NOTE: W1 pins the four ORIGINAL shared-rule declarations and W8 pins the
    # new helper by name — both read the same file, but W1's list does not
    # include the helper, so removing it reds W8 only. That asymmetry is
    # deliberate: W1's list is the ARCH-01 baseline (frozen so an unrelated
    # rename cannot silently satisfy it), W8 owns the new criterion.
    run_real("S7-normalize-helper-missing-W8", helper_gone2, ("w8",))

    # S8: the collection writer reverts to its own inline blank handling — W8.
    run_real("S8-writer-inline-blank-W8", replace_in(
        PC_REL,
        "                val newBase = normalizeCustomBaseURL(s)",
        "                val newBase = s.ifEmpty { null }"), ("w8",))

    # S9: the add path reverts to `takeIf { it.isNotBlank() }` — W8.
    run_real("S9-add-path-inline-blank-W8", replace_in(
        PC_REL,
        "    val base = normalizeCustomBaseURL(raw) ?: return null",
        "    val base = raw?.takeIf { it.isNotBlank() } ?: return null"), ("w8",))

    # S10: the create face reverts to `ifEmpty { null }` — W8.
    run_real("S10-create-face-inline-blank-W8", replace_in(
        PM_REL,
        '        val customBaseURL = normalizeCustomBaseURL(params.optString("customBaseURL", ""))',
        '        val customBaseURL = params.optString("customBaseURL", "").ifEmpty { null }'), ("w8",))

    # S11: the update face reverts (guard + copy arm) — W8.
    def update_face_revert(rel, text):
        if rel != PM_REL:
            return text
        return compose(
            replace_in(
                PM_REL,
                '            val newBase = normalizeCustomBaseURL(params.optString("customBaseURL", ""))',
                '            val newBase = params.optString("customBaseURL", "").ifEmpty { null }'),
            replace_in(
                PM_REL,
                '                else normalizeCustomBaseURL(params.optString("customBaseURL", ""))',
                '                else params.optString("customBaseURL", "").ifEmpty { null }'),
        )(rel, text)

    run_real("S11-update-face-inline-blank-W8", update_face_revert, ("w8",))

    # S12: a SECOND normalization helper appears in another file — W8 (single decl).
    def second_helper(rel, text):
        if rel == PM_REL:
            return text + ("\n\ninternal fun normalizeCustomBaseURL(raw: String?): String? = raw\n")
        return text

    run_real("S12-second-normalize-helper-W8", second_helper, ("w8",))

    # S13: the read side stops trimming — W9.
    run_real("S13-getter-drops-trim-W9", replace_in(
        PCF_REL,
        "            val base = customBaseURL?.trim()?.trimEnd('/')?.takeIf { it.isNotEmpty() } ?: return null",
        "            val base = customBaseURL?.trimEnd('/') ?: return null"), ("w9",))

    # S14: the read side keeps the trim but drops the trailing-slash collapse
    # — W9 (a different tooth of the same criterion).
    run_real("S14-getter-drops-slash-collapse-W9", replace_in(
        PCF_REL,
        "            val base = customBaseURL?.trim()?.trimEnd('/')?.takeIf { it.isNotEmpty() } ?: return null",
        "            val base = customBaseURL?.trim()?.takeIf { it.isNotEmpty() } ?: return null"), ("w9",))

    # S15: contrast — a fixture where the helper is consumed on every face and
    # the getter keeps both guards must stay GREEN (no criterion fires on the
    # accepted shape).
    run_real("S15-fixed-shape-green", lambda rel, text: text, ())

    all_ok = True
    for name, ok, rc, got, exp in results:
        print("%s %s (rc=%d got=%s exp=%s)" % ("PASS" if ok else "FAIL", name, rc, got, exp))
        if not ok:
            all_ok = False
    if all_ok:
        print(SELFTEST_MARKER)
        return 0
    return 1


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        return run_selftest()
    if len(sys.argv) < 2:
        print("usage: provider_write_rules_check.py <repo-root> | --selftest")
        return 2
    root = sys.argv[1]
    rc, detail = check_tree(root)
    if rc == 0:
        print("provider_write_rules: ALL GREEN (W1 shared rules, W2/W3 single declaration, "
              "W4 no literal copies, W5 debug wiring, W6 modelsAdd, W7 writer wiring, "
              "W8 normalization single-sourced on every write face, W9 read side trims)")
        return 0
    print("provider_write_rules: FAIL")
    print(json.dumps(detail, indent=2))
    return 1


if __name__ == "__main__":
    sys.exit(main())

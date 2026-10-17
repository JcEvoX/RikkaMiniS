#!/usr/bin/env python3
"""
Gemini base-version guard — the version segment the request path depends on.

Bug class (deep-audit-1016 ISSUE-01, live ~27 days): `GeminiProvider` assembles
`<base>/models/<id>:generateContent`. `bd68c66c` (2026-09-19, FIX-1) made it strip
`/v1beta` and `/v1` from the configured base — mirrors the models-LIST helper, which
collapses the two forms relays paste — but never restored a segment. The endpoint has
no versionless form, so every Gemini instance (official base included) got 404 on both
chat paths, while the model list kept working: model list 200, chat 404. Measured
ground truth: `/models/<id>:generateContent` -> 404 with an empty body;
`/v1beta/models/<id>:generateContent` -> 400 "API key not valid" (path exists).

Criterion (structural — the behavioural half lives in `GeminiBasePathTest` /
`GeminiProviderTest`, which assert the built URL for every base shape):
  1. Both base normalizers exist and are found **per file**:
       * `GeminiProvider.kt`   -> `normalizedBasePath`
       * `GeminiModelsApi.kt`  -> `normalizeBase`
       * `GeminiBasePath.kt`   -> `normalize` (the extracted shared implementation)
     A missing function is a HARD FAIL, not a skip: a renamed or moved normalizer must
     fail loudly instead of leaving the gate silently green (audit3 FIX-D's lesson —
     a crude mutation dropped a host from the scanned set and the gate passed).
  2. Each normalizer either (a) delegates to `GeminiBasePath.normalize(...)`, or
     (b) itself strips `/v1beta`, strips `/v1`, and restores exactly one `/v1beta`.
     Files are checked individually: a union across files is what hid the split shape
     for a month (the list side restored the segment, the chat side did not).
  3. `GeminiProvider` routes **both** call sites (non-streaming and streaming) through
     the normalizer, and no call site concatenates the raw `basePath` — a single
     bypassing call site is the exact shape this defect can come back in.
  4. [deep-audit-1024 ISSUE-02] The three **other** sites that build a Gemini URL from
     the configured base must delegate too: `GeminiModelsApi.normalizeBase` (checked
     here as "delegates, no shadow copy"), `GeminiVoiceProvider.buildVoiceOutputRequest`
     and the probe-URL builder in `ProviderMutationMethods.kt`. Criteria 4 and 5 need a
     full check-out to say anything: the frozen `test_scan.py` fixtures build a
     gemini-only tree, so when either cross-file target is missing the gate prints
     `[PARTIAL]` and falls back to criteria 1-3 rather than failing on a partial tree.
  5. Any `ProviderType` arm assembling a `/models` probe URL must apply the same
     version-segment rule as its siblings: `anthropic` guards on `endsWith("/v1")`,
     `gemini` goes through [SHARED_DELEGATE]. Before the fix the two arms were the only
     two without the guard, which is what made the 404 reachable only for those types.

Usage: python3 scripts/scan/gemini_base_version_check.py [repo_root]
       python3 scripts/scan/gemini_base_version_check.py --selftest
Exit:  0 = all checks pass, 1 = a missing/incorrect normalizer or wiring
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile

GEMINI_DIR = os.path.join(
    "src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app", "provider", "gemini"
)

APP_DIR = os.path.join("src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app")
VOICE_REL = os.path.join(APP_DIR, "provider", "voice", "VoiceProviderVendors.kt")
MUTATION_REL = os.path.join(APP_DIR, "debug", "ProviderMutationMethods.kt")

# Both must exist before the cross-file criteria (4/5) can say anything: a tree holding
# only the gemini package cannot express a delegation graph across four files.
CROSS_FILE_RELS = (VOICE_REL, MUTATION_REL)
VOICE_CLASS = "GeminiVoiceProvider"
VOICE_FN = "buildVoiceOutputRequest"
PROBE_ARM_MARK = "/models"

# file -> normalizer function name
TARGETS = {
    "GeminiProvider.kt": "normalizedBasePath",
    "GeminiModelsApi.kt": "normalizeBase",
    "GeminiBasePath.kt": "normalize",
}

SHARED_DELEGATE = "GeminiBasePath.normalize"


def strip_comments(text):
    """Drop // and /* */ comments so prose cannot fake a structural match."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def function_body(text, name):
    """Return the body of `fun <name>(...)`, brace-balanced; None if not found.

    Skips string literals so `"${...}"` interpolation braces cannot unbalance the
    count, and so a quoted brace can never be mistaken for structure.
    """
    m = re.search(r"\bfun\s+" + re.escape(name) + r"\s*\(", text)
    if not m:
        return None
    i = text.find("(", m.end() - 1)
    depth = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    # expression body (`: Type = expr`) or block body (`{ ... }`) — whichever the
    # signature reaches first. These normalizers are one-liners when they delegate.
    tail = text[i + 1 :]
    eq = tail.find("=")
    brace = tail.find("{")
    if eq >= 0 and (brace < 0 or eq < brace):
        line_end = tail.find("\n", eq)
        return tail[eq + 1 : line_end if line_end >= 0 else len(tail)]
    j = i + 1 + brace  # brace was found in `tail`; the scan below indexes `text`
    if brace < 0:
        return None
    depth = 0
    k = j
    while k < len(text):
        c = text[k]
        if c == '"':
            k += 1
            while k < len(text) and text[k] != '"':
                k += 2 if text[k] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[j + 1 : k]
        k += 1
    return None


def restores_version(body):
    """True when the body puts `/v1beta` back, rather than only stripping it.

    Strip the strip-calls out first; a `/v1beta` that survives is a restore.
    """
    if SHARED_DELEGATE in body:
        return True
    residue = body.replace('.removeSuffix("/v1beta")', "").replace('.removeSuffix("/v1")', "")
    return "/v1beta" in residue


def analyse(body):
    """Which of the three structural pieces the normalizer body has."""
    return {
        "strips /v1beta": '.removeSuffix("/v1beta")' in body,
        "strips /v1": '.removeSuffix("/v1")' in body,
        "restores /v1beta": restores_version(body),
    }


def balanced_block(text, open_idx):
    """Index just past the closer matching text[open_idx]; -1 when unbalanced."""
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def when_blocks(text):
    """Body of every `when (...) { ... }` in [text], brace-balanced.

    Block-scoped on purpose: arms are split per block, so a `ProviderType.x ->`
    outside the block under test cannot bleed into an arm's text.
    """
    out = []
    for m in re.finditer(r"\bwhen\s*\(", text):
        brace = text.find("{", m.end())
        if brace < 0 or brace - m.end() > 200:
            continue
        end = balanced_block(text, brace)
        if end < 0:
            continue
        out.append(text[brace + 1 : end - 1])
    return out


def provider_arms(block):
    """(type name, arm text) for every `ProviderType.<x> -> ...` arm in [block]."""
    marks = list(re.finditer(r"ProviderType\.(\w+)\s*->", block))
    for idx, m in enumerate(marks):
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(block)
        yield m.group(1), block[m.end() : end]


def class_body(text, class_name):
    """Body of `class <class_name>` — brace-balanced, None when absent."""
    m = re.search(r"\bclass\s+" + re.escape(class_name) + r"\s*[(<]", text)
    if not m:
        return None
    brace = text.find("{", m.end())
    if brace < 0:
        return None
    end = balanced_block(text, brace)
    return None if end < 0 else text[brace + 1 : end - 1]


def read(rel, root):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return strip_comments(f.read())


def check_cross_file_sites(root, gemini_bodies):
    """Criteria 4/5 — the three sibling sites must delegate like the shared rule.

    Only called when both cross-file targets are present: a tree holding just the
    gemini package (the frozen `test_scan.py` fixtures) cannot express this graph.
    """
    failures = []

    sibling = gemini_bodies.get("GeminiModelsApi.kt")
    if sibling is None:
        failures.append("GeminiModelsApi.kt::normalizeBase not found — cannot confirm it delegates")
        print("  [ROT] GeminiModelsApi.kt::normalizeBase not found")
    elif SHARED_DELEGATE in sibling:
        print(f"  [OK] GeminiModelsApi.kt::normalizeBase delegates to {SHARED_DELEGATE}")
    else:
        failures.append(
            "GeminiModelsApi.kt::normalizeBase carries its own copy of the rule — a second "
            f"implementation is exactly ISSUE-02; it must call {SHARED_DELEGATE}(...)"
        )
        print("  [FAIL] GeminiModelsApi.kt::normalizeBase has a shadow copy")

    voice = read(VOICE_REL, root)
    body = class_body(voice, VOICE_CLASS) if voice else None
    fn = function_body(body, VOICE_FN) if body else None
    if fn is None:
        failures.append(f"{VOICE_REL}: `{VOICE_CLASS}.{VOICE_FN}` not found — keep this gate honest")
        print(f"  [ROT] {VOICE_CLASS}::{VOICE_FN} not found")
    elif SHARED_DELEGATE in fn:
        print(f"  [OK] {VOICE_CLASS}::{VOICE_FN} delegates to {SHARED_DELEGATE}")
    else:
        failures.append(
            f"{VOICE_CLASS}::{VOICE_FN} builds the Gemini base itself (contains-check on "
            f"/v1beta) — it must call {SHARED_DELEGATE}(effectiveBaseURL())"
        )
        print(f"  [FAIL] {VOICE_CLASS}::{VOICE_FN} does not delegate")

    mutation = read(MUTATION_REL, root)
    probe_blocks = [
        b for b in when_blocks(mutation or "") if "ProviderType." in b and PROBE_ARM_MARK in b
    ]
    if not probe_blocks:
        failures.append(
            f"{MUTATION_REL}: no `when` block assembles a `{PROBE_ARM_MARK}` probe URL — the "
            "probe URL moved; re-anchor this gate deliberately"
        )
        print(f"  [ROT] {MUTATION_REL}: probe URL block not found")
        return failures

    seen = {"gemini": 0, "anthropic": 0}
    for block in probe_blocks:
        for type_name, arm in provider_arms(block):
            if PROBE_ARM_MARK not in arm:
                continue
            if type_name == "gemini":
                seen["gemini"] += 1
                ok = SHARED_DELEGATE in arm
                print(f"  [{'OK' if ok else 'FAIL'}] probe arm ProviderType.gemini delegates")
                if not ok:
                    failures.append(
                        "ProviderMutationMethods.kt: the gemini probe arm concatenates a literal "
                        f"version segment instead of {SHARED_DELEGATE}(...) — a custom base that "
                        "already carries /v1beta then probes /v1beta/v1beta/models (404)"
                    )
            elif type_name == "anthropic":
                seen["anthropic"] += 1
                ok = 'endsWith("/v1")' in arm
                print(f"  [{'OK' if ok else 'FAIL'}] probe arm ProviderType.anthropic guards on /v1")
                if not ok:
                    failures.append(
                        "ProviderMutationMethods.kt: the anthropic probe arm appends /v1 with no "
                        "endsWith(\"/v1\") guard — effectiveBaseURL already ends in /v1 when "
                        "appendV1Suffix applied, so it probes /v1/v1/models (404). The sibling "
                        "openAI/xAI/kimiCode arms all guard; the asymmetry is the defect."
                    )
    for type_name, count in sorted(seen.items()):
        if count == 0:
            failures.append(
                f"{MUTATION_REL}: no ProviderType.{type_name} arm carries a `{PROBE_ARM_MARK}` "
                "probe URL — anchor moved, re-anchor this gate deliberately"
            )
            print(f"  [ROT] {MUTATION_REL}: no {type_name} probe arm found")

    return failures


def _fixture_voice(fn_body=None, decoy_body=None, fn_name="buildVoiceOutputRequest"):
    gemini = fn_body or (
        "        val base = GeminiBasePath.normalize(effectiveBaseURL())\n"
        '        return "$base/models/$model:generateContent?key="\n'
    )
    decoy = decoy_body or (
        "        return composedUrlString(voiceOutputEndpointPath())\n"
    )
    return (
        "package com.rikkaminis.app.provider.voice\n"
        "\n"
        "class OtherVoiceProvider(providerId: String, baseURL: String, apiKey: String?) {\n"
        "    override fun buildVoiceOutputRequest(request: String): String {\n"
        + decoy
        + "    }\n"
        "}\n"
        "\n"
        "class GeminiVoiceProvider(providerId: String, baseURL: String, apiKey: String?) {\n"
        f"    override fun {fn_name}(request: String): String {{\n"
        + gemini
        + "    }\n"
        "}\n"
    )


def _probe_body(arms=None):
    """The probe-URL builder. Default = the fixed shape; [arms] overrides arms."""
    if arms is None:
        arms = (
            '        ProviderType.anthropic -> if (baseURL.endsWith("/v1")) "$baseURL/models" '
            'else "$baseURL/v1/models"\n'
            '        ProviderType.gemini -> "${GeminiBasePath.normalize(baseURL)}/models?key=" '
            "+ (key ?: \"\")\n"
            '        ProviderType.openAI -> if (baseURL.endsWith("/v1")) "$baseURL/models" '
            'else "$baseURL/v1/models"\n'
        )
    return (
        "internal fun providerProbeUrl(type: ProviderType, baseURL: String, key: String?): String =\n"
        "    when (type) {\n"
        + arms
        + "    }\n"
    )


def _fixture_mutation(probe=None):
    """Mutation file: a decoy `when` (the per-type fallback, no /models literal)
    plus the probe builder. The decoy is what the block-scoped arm split exists
    for — its arms must not be mistaken for probe arms."""
    if probe is None:
        probe = _probe_body()
    return (
        "package com.rikkaminis.app.debug\n"
        "\n"
        "internal object ProviderMutationMethods {\n"
        "    private fun fallback(type: ProviderType): String = when (type) {\n"
        '        ProviderType.anthropic -> "https://api.anthropic.com"\n'
        '        ProviderType.gemini -> "https://generativelanguage.googleapis.com"\n'
        '        ProviderType.kimiCode -> "https://api.kimi.com/coding/v1"\n'
        "    }\n"
        "}\n"
        "\n"
        + probe
    )


def _legacy_sibling_body():
    """The pre-fix copy: strips each suffix once, so `.../v1beta/v1` → `/v1beta/v1beta`."""
    return (
        "    private fun normalizeBase(baseUrl: String?): String {\n"
        "        val raw = baseUrl?.trim()?.trimEnd('/')?.takeIf { it.isNotEmpty() } "
        "?: return DEFAULT_BASE\n"
        '        val stripped = raw.removeSuffix("/v1beta").removeSuffix("/v1")\n'
        '        return "$stripped/v1beta"\n'
        "    }\n"
    )


def write_fixture(root, partial=False, sibling_body=None, voice_fn_body=None,
                  decoy_body=None, probe=None, voice_fn_name="buildVoiceOutputRequest"):
    gemini_dir = os.path.join(root, GEMINI_DIR)
    os.makedirs(gemini_dir, exist_ok=True)
    files = {
        os.path.join(GEMINI_DIR, "GeminiProvider.kt"): (
            "package com.rikkaminis.app.provider.gemini\n"
            "class GeminiProvider(private val basePath: String = GeminiBasePath.DEFAULT_BASE) {\n"
            "    private fun normalizedBasePath(): String = GeminiBasePath.normalize(basePath)\n"
            "    fun sendMessage() {\n"
            '        val url = "${normalizedBasePath()}/models/gemini-2.5-flash:generateContent"\n'
            "    }\n"
            "    fun streamMessage() {\n"
            '        val url = "${normalizedBasePath()}/models/gemini-2.5-flash:streamGenerateContent"\n'
            "    }\n"
            "}\n"
        ),
        os.path.join(GEMINI_DIR, "GeminiBasePath.kt"): (
            "package com.rikkaminis.app.provider.gemini\n"
            "internal object GeminiBasePath {\n"
            '    const val DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"\n'
            "    fun normalize(raw: String?): String {\n"
            "        val base = raw?.trim()?.trimEnd('/')?.takeIf { it.isNotEmpty() } "
            "?: return DEFAULT_BASE\n"
            "        var stripped = base\n"
            "        while (true) {\n"
            '            val next = stripped.removeSuffix("/v1beta").removeSuffix("/v1")\n'
            "            if (next == stripped) break\n"
            "            stripped = next\n"
            "        }\n"
            '        return "$stripped/v1beta"\n'
            "    }\n"
            "}\n"
        ),
        os.path.join(GEMINI_DIR, "GeminiModelsApi.kt"): (
            "package com.rikkaminis.app.provider.gemini\n"
            "internal object GeminiModelsApi {\n"
            + (
                sibling_body
                if sibling_body is not None
                else "    private fun normalizeBase(baseUrl: String?): String = "
                "GeminiBasePath.normalize(baseUrl)\n"
            )
            + "}\n"
        ),
        VOICE_REL: _fixture_voice(voice_fn_body, decoy_body, voice_fn_name),
        MUTATION_REL: _fixture_mutation(probe),
    }
    if partial:
        for rel in CROSS_FILE_RELS:
            files.pop(rel, None)
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def _run_gate(root):
    """Run main() against [root] with its stdout captured."""
    old, buf = sys.argv, io.StringIO()
    sys.argv = ["gemini_base_version_check.py", str(root)]
    try:
        with contextlib.redirect_stdout(buf):
            code = main()
    finally:
        sys.argv = old
    return code, buf.getvalue()


def selftest():
    """Fixture trees — one dirty arm per criterion, every one must turn it red."""
    results = []

    def run(name, expect_red, note_in=None, **kwargs):
        root = tempfile.mkdtemp(prefix="geminibase_")
        try:
            write_fixture(root, **kwargs)
            code, out = _run_gate(root)
            ok = (code == 1) if expect_red else (code == 0)
            if ok and note_in is not None and note_in not in out:
                ok, note_in = False, f"missing {note_in!r} in output"
            results.append((name, ok, note_in or f"exit={code}"))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    run("clean full tree passes", False, "[FULL]")
    run("gemini-only tree falls back to criteria 1-3", False, "[PARTIAL]", partial=True)
    run(
        "GeminiModelsApi keeping its own single-pass copy is caught",
        True,
        "GeminiModelsApi.kt",
        sibling_body=_legacy_sibling_body(),
    )
    run(
        "GeminiVoiceProvider building the base itself is caught",
        True,
        "GeminiVoiceProvider",
        voice_fn_body=(
            "        var base = effectiveBaseURL()\n"
            '        if (!base.contains("/v1beta")) base = base.trimEnd(\'/\') + "/v1beta"\n'
            '        return "$base/models/$model:generateContent?key="\n'
        ),
    )
    run(
        "a decoy class with the same method name is not mistaken for Gemini's",
        False,
        "[FULL]",
        decoy_body=(
            "        var base = effectiveBaseURL()\n"
            '        if (!base.contains("/v1beta")) base = base.trimEnd(\'/\') + "/v1beta"\n'
            '        return "$base/models/$model:generateContent"\n'
        ),
    )
    run(
        "gemini probe arm with a literal /v1beta is caught",
        True,
        "ProviderType.gemini",
        probe=_probe_body(
            arms=(
                '        ProviderType.anthropic -> if (baseURL.endsWith("/v1")) "$baseURL/models" '
                'else "$baseURL/v1/models"\n'
                '        ProviderType.gemini -> "$baseURL/v1beta/models?key=" + (key ?: "")\n'
            )
        ),
    )
    run(
        "anthropic probe arm with no /v1 guard is caught",
        True,
        "ProviderType.anthropic",
        probe=_probe_body(
            arms=(
                '        ProviderType.anthropic -> "$baseURL/v1/models"\n'
                '        ProviderType.gemini -> "${GeminiBasePath.normalize(baseURL)}/models?key=" '
                "+ (key ?: \"\")\n"
            )
        ),
    )
    run(
        "a probe URL that moved out of a when block is a rot failure",
        True,
        "probe URL moved",
        probe=(
            "internal fun providerProbeUrl(type: ProviderType, baseURL: String, key: String?): "
            "String = lookup[type] ?: baseURL\n"
        ),
    )
    run(
        "a renamed voice target is a rot failure, not a skip",
        True,
        "not found",
        voice_fn_name="buildOutputRequest",
    )

    print("=== gemini_base_version_check --selftest ===")
    failed = 0
    for name, ok, note in results:
        if not ok:
            failed += 1
        print(f"[{'ok' if ok else 'FAIL'}] {name} -> {note}")
    if failed:
        print(f"SELFTEST: {failed} arm(s) misbehaved")
        return 1
    print(f"GEMINI_BASE_VERSION_SELFTEST=PASS ({len(results)} arms)")
    return 0


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    root = args[0] if args else "."
    print("━━━ gemini_base_version_check ━━━")
    failures = []
    seen = 0
    delegating = []
    results = {}
    bodies = {}

    for fname, fn_name in TARGETS.items():
        path = os.path.join(root, GEMINI_DIR, fname)
        if not os.path.exists(path):
            failures.append(f"{fname}: file missing (base normalization moved?)")
            print(f"  [ROT] {fname}: not found at {GEMINI_DIR}")
            continue
        with open(path, encoding="utf-8") as f:
            text = strip_comments(f.read())
        body = function_body(text, fn_name)
        if body is None:
            # Rot guard: a renamed/removed normalizer must not read as "clean".
            failures.append(f"{fname}: `{fn_name}` not found — keep this gate honest")
            print(f"  [ROT] {fname}: `{fn_name}` not found")
            continue
        seen += 1
        bodies[fname] = body
        if SHARED_DELEGATE in body:
            # Delegation is the shape we want: the rule lives once. The shared
            # implementation is a target of this same gate, so its correctness is
            # checked in the same run.
            delegating.append(fname)
            results[fname] = True
            print(f"  [OK] {fname}::{fn_name}  delegates to {SHARED_DELEGATE}")
            continue
        pieces = analyse(body)
        ok = all(pieces.values())
        results[fname] = ok
        tag = "[OK]" if ok else "[FAIL]"
        print(f"  {tag} {fname}::{fn_name}  " + ", ".join(f"{k}={v}" for k, v in pieces.items()))
        for piece, present in pieces.items():
            if not present:
                failures.append(f"{fname}::{fn_name} does not {piece}")

    if seen == 0:
        failures.append("no base normalizer found in any target file")
    if delegating and not results.get("GeminiBasePath.kt", False):
        failures.append("a normalizer delegates to GeminiBasePath.normalize, but GeminiBasePath.kt is missing or not OK")

    # Wiring: both GeminiProvider call sites must go through the normalizer.
    provider = os.path.join(root, GEMINI_DIR, "GeminiProvider.kt")
    if os.path.exists(provider):
        src = strip_comments(open(provider, encoding="utf-8").read())
        routed = len(re.findall(r"normalizedBasePath\(\)", src)) - 1  # minus the declaration
        print(f"  [{'OK' if routed >= 2 else 'FAIL'}] GeminiProvider routes {routed} call site(s) through the normalizer")
        if routed < 2:
            failures.append(f"GeminiProvider routes only {routed} call site(s) through the normalizer (need 2)")
        raw = re.findall(r'"\$\{basePath\}/models/', src)
        if raw:
            failures.append(f"GeminiProvider builds {len(raw)} URL(s) from the raw basePath")
            print(f"  [FAIL] {len(raw)} URL(s) built from the raw basePath")

    # Criteria 4/5 — the sibling sites. Gated on a full check-out: the frozen
    # test_scan.py fixtures build a gemini-only tree, so an absent cross-file
    # target means "partial tree", not "site removed".
    missing = [rel for rel in CROSS_FILE_RELS if not os.path.isfile(os.path.join(root, rel))]
    if missing:
        print(
            "  [PARTIAL] cross-file criteria skipped — tree lacks "
            + ", ".join(missing)
            + " (fixture-shaped tree: criteria 1-3 only)"
        )
    else:
        print("  [FULL] cross-file criteria 4/5:")
        failures += check_cross_file_sites(root, bodies)

    print("")
    if failures:
        print("❌ gemini_base_version_check findings:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✅ gemini base version guard: all normalizers restore /v1beta, both call sites routed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

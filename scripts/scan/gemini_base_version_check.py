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

Usage: python3 scripts/scan/gemini_base_version_check.py [repo_root]
Exit:  0 = all checks pass, 1 = a missing/incorrect normalizer or wiring
"""
import os
import re
import sys

GEMINI_DIR = os.path.join(
    "src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app", "provider", "gemini"
)

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


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    print("━━━ gemini_base_version_check ━━━")
    failures = []
    seen = 0
    delegating = []
    results = {}

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

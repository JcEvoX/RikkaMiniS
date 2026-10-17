#!/usr/bin/env python3
"""
Gate: FIX-I (deep-audit-1024 ISSUE-09) — `ModelUseOffloadHandler` must route
every relative path through [resolveMaybeRelative] (the single join
implementation defined in `ConfigOffloadHandler.kt`) instead of keeping private,
drifting copies and cwd-less call chains.

The bug (one function, contradicting itself):
  - `cmdRun`'s `--output` still refused any relative path with
    `invalid_output_path`, while the SAME `cmdRun` forwarded `request.cwd` for
    `--input` / `--system-file`;
  - `readLinuxPath` kept a hand-written inline join (`$cwd/$linuxPath`), which
    `BrowserUseOffloadHandler.kt`'s KDoc forbids ("single join implementation");
  - the `image_url` chain (`parseMessages` -> `parseMessageArray` ->
    `resolveImageUrl`) had no `cwd` parameter, so a relative image path inside
    `prompt.json` (`{"type":"image_url","image_url":{"url":"chart.png"}}`)
    threw `ImageInputError`.

Checks run on comment-stripped text, so prose in a KDoc cannot satisfy them:
  C1  `readLinuxPath(...)`      body calls `resolveMaybeRelative(`.
  C2  `cmdRun(...)`             body normalises `--output` through
                                `resolveMaybeRelative(`.
  C3  `resolveImageUrl(...)`    body calls `resolveMaybeRelative(`.
  C4  the file keeps NO inline cwd join (`"$cwd...` string template) — the
      single implementation may only live in `ConfigOffloadHandler.kt`.

Usage: python3 scripts/scan/offload_relative_path_unify_check.py [repo_root]
       python3 scripts/scan/offload_relative_path_unify_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import shutil
import sys
import tempfile

SRC = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload"
TARGET_REL = SRC + "/ModelUseOffloadHandler.kt"
CALL = "resolveMaybeRelative("
# A Kotlin string template that joins a cwd itself — the shape that must not
# reappear in the target file (the join belongs to resolveMaybeRelative).
INLINE_JOIN = '"$cwd'


def strip_comments(text):
    """Blank out comment content, keeping everything else, with a scanner that
    understands Kotlin string templates.

    Two traps this must survive in the target file:
      * `"file://"` — a naive `//` scan truncates the very line that carries the
        call we assert on;
      * `"…'${args.get("input")}'…"` — a quote NESTED inside a `${…}` template
        expression, which desyncs any scanner that does not recurse into it.
    """
    out = []
    stack = [["code", 0]]  # [state, brace_depth] — code inside a template expr pops on '}'
    i = 0
    n = len(text)
    while i < n:
        st = stack[-1]
        if st[0] == "code":
            if text.startswith('"""', i):
                stack.append(["raw", 0]); i += 3; continue
            c = text[i]
            if c == '"':
                stack.append(["str", 0]); out.append(c); i += 1; continue
            if text.startswith("//", i):
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text.startswith("/*", i):
                end = text.find("*/", i + 2)
                i = n if end < 0 else end + 2
                continue
            if c == "{":
                st[1] += 1
            elif c == "}":
                if st[1] == 0 and len(stack) > 1:
                    stack.pop(); i += 1; continue
                st[1] -= 1
            out.append(c); i += 1; continue
        if st[0] == "str":
            c = text[i]
            if c == "\\":
                out.append(c)
                if i + 1 < n:
                    out.append(text[i + 1])
                i += 2; continue
            if text.startswith("${", i):
                stack.append(["code", 0]); out.append("${"); i += 2; continue
            if c == '"':
                stack.pop(); out.append(c); i += 1; continue
            out.append(c); i += 1; continue
        # raw string
        if text.startswith('"""', i):
            stack.pop(); out.append('"""'); i += 3; continue
        out.append(text[i]); i += 1
    return "".join(out)


def declaration_slice(text, marker):
    """Return the `{ ... }` body of the declaration containing `marker`
    (template/string aware, so braces inside `"${…}"` do not unbalance it).
    Returns "" when the marker or its opening brace is absent."""
    idx = text.find(marker)
    if idx == -1:
        return ""
    start = text.find("{", idx)
    if start == -1:
        return ""
    depth = 0
    tdepth = 0
    tstack = []
    in_str = False
    in_raw = False
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if in_raw:
            if text.startswith('"""', i):
                in_raw = False; i += 3; continue
            i += 1; continue
        if in_str:
            if c == "\\":
                i += 2; continue
            if text.startswith("${", i):
                tstack.append(tdepth); tdepth = 0; in_str = False; i += 2; continue
            if c == '"':
                in_str = False; i += 1; continue
            i += 1; continue
        if text.startswith('"""', i):
            in_raw = True; i += 3; continue
        if c == '"':
            in_str = True; i += 1; continue
        if c == "{":
            if tstack:
                tdepth += 1
            else:
                depth += 1
        elif c == "}":
            if tstack:
                if tdepth == 0:
                    tdepth = tstack.pop(); in_str = True
                else:
                    tdepth -= 1
            else:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return text[start:]


def declaration_slice(text, marker):
    """Return the `{ ... }` body of the declaration containing `marker`.

    Brace matching skips braces inside string literals (so `"$cwd/$path"` and
    `"${x}"` templates do not unbalance the counter). Returns "" when the
    marker or its opening brace is absent.
    """
    idx = text.find(marker)
    if idx == -1:
        return ""
    start = text.find("{", idx)
    if start == -1:
        return ""
    depth = 0
    i = start
    n = len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return text[start:]


def check_source(raw):
    """Return a list of (criterion_id, message) for the given file text."""
    code = strip_comments(raw)
    problems = []

    def body(marker):
        return declaration_slice(code, marker)

    if CALL not in body("fun readLinuxPath("):
        problems.append(("C1", "readLinuxPath() does not call resolveMaybeRelative() "
                               "— it must delegate to the single join implementation"))
    if CALL not in body("fun cmdRun("):
        problems.append(("C2", "cmdRun() does not normalise --output through "
                               "resolveMaybeRelative() — a relative --output still "
                               "dies with invalid_output_path"))
    if CALL not in body("fun resolveImageUrl("):
        problems.append(("C3", "resolveImageUrl() does not call resolveMaybeRelative() "
                               "— a relative image_url has no cwd to resolve against"))
    if INLINE_JOIN in code:
        problems.append(("C4", "a hand-written inline cwd join ('\"$cwd...') is back — the "
                               "join must stay single-source in resolveMaybeRelative()"))

    # A marker with no body means the declaration vanished: fail closed rather
    # than silently reporting "clean" on an empty slice.
    for marker, cid in (("fun readLinuxPath(", "C1"), ("fun cmdRun(", "C2"),
                        ("fun resolveImageUrl(", "C3")):
        if not declaration_slice(code, marker):
            problems.append((cid, "declaration '%s' not found in the target file" % marker))
    return problems


def check_tree(root):
    path = os.path.join(root, TARGET_REL)
    if not os.path.isfile(path):
        return None, [("C0", "target file missing: %s" % TARGET_REL)]
    with open(path, "r", encoding="utf-8") as fh:
        return path, check_source(fh.read())


# ── selftest fixtures ────────────────────────────────────────────────────────

_CLEAN = '''package com.rikkaminis.app.sandbox.offload

class ModelUseOffloadHandler {
    private fun cmdRun(request: NativeOffloadRequest) {
        val outputPath = args.get("output")?.let { raw ->
            if (raw.startsWith("file://")) raw else resolveMaybeRelative(raw, request.cwd)
        }
        if (outputPath != null && !outputPath.startsWith("/")) {
            return NativeOffloadResult(2, "nope")
        }
    }

    private fun readLinuxPath(linuxPath: String, cwd: String?): String? {
        val resolved = resolveMaybeRelative(linuxPath, cwd)
        return resolved
    }

    private fun resolveImageUrl(url: String, cwd: String?): LLMMessage.ImagePart {
        val linuxPath = when {
            url.startsWith("file://") -> url.removePrefix("file://")
            url.startsWith("/") -> url
            else -> resolveMaybeRelative(url, cwd).takeIf { it.startsWith("/") }
                ?: throw ImageInputError("bad")
        }
        return linuxPath
    }
}
'''

_DIRTY_READLINUX = _CLEAN.replace(
    "        val resolved = resolveMaybeRelative(linuxPath, cwd)\n        return resolved",
    '        val resolved = if (!linuxPath.startsWith("/") && !cwd.isNullOrEmpty()) {\n'
    '            if (cwd.endsWith("/")) "$cwd$linuxPath" else "$cwd/$linuxPath"\n'
    "        } else linuxPath\n        return resolved",
)

_DIRTY_OUTPUT = _CLEAN.replace(
    '        val outputPath = args.get("output")?.let { raw ->\n'
    '            if (raw.startsWith("file://")) raw else resolveMaybeRelative(raw, request.cwd)\n'
    "        }",
    '        val outputPath = args.get("output")',
)

_DIRTY_IMAGE = _CLEAN.replace(
    "            else -> resolveMaybeRelative(url, cwd).takeIf { it.startsWith(\"/\") }\n"
    "                ?: throw ImageInputError(\"bad\")",
    "            else -> throw ImageInputError(\"bad\")",
)

# All three call sites clean, but a 4th helper keeps an inline join → only C4.
_DIRTY_INLINE_ONLY = _CLEAN.replace(
    "    private fun readLinuxPath(linuxPath: String, cwd: String?): String? {",
    '    private fun otherJoin(linuxPath: String, cwd: String?): String {\n'
    '        return if (cwd.endsWith("/")) "$cwd$linuxPath" else "$cwd/$linuxPath"\n'
    "    }\n\n"
    "    private fun readLinuxPath(linuxPath: String, cwd: String?): String? {",
)


def _fixture_root(text):
    root = tempfile.mkdtemp(prefix="offload-relpath-gate-selftest-")
    path = os.path.join(root, TARGET_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return root


def selftest():
    cases = [
        ("clean: all three sites delegate to resolveMaybeRelative", _CLEAN, set()),
        ("C1C4: readLinuxPath keeps an inline join", _DIRTY_READLINUX, {"C1", "C4"}),
        ("C2: --output not normalised", _DIRTY_OUTPUT, {"C2"}),
        ("C3: resolveImageUrl has no cwd join", _DIRTY_IMAGE, {"C3"}),
        ("C4 only: all sites clean but a stray inline join remains", _DIRTY_INLINE_ONLY, {"C4"}),
    ]
    failures = []
    for label, text, expected in cases:
        root = _fixture_root(text)
        try:
            _, problems = check_tree(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        got = {cid for cid, _ in problems}
        ok = got == expected
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  → got=%s expected=%s" % (sorted(got), sorted(expected))))
        if not ok:
            failures.append(label)
    if failures:
        print("❌ selftest failed: %s" % ", ".join(failures))
        return 1
    print("OFFLOAD_RELATIVE_PATH_UNIFY_SELFTEST=PASS (5 fixtures: 4 violation "
          "shapes caught, clean tree green)")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    root = os.path.abspath(args[0]) if args else os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".."))
    if "--selftest" in flags:
        return selftest()
    unknown = [f for f in flags if f != "--selftest"]
    if unknown:
        print("unknown option(s): %s" % " ".join(unknown))
        return 2
    path, problems = check_tree(root)
    if path is None or problems:
        print("offload_relative_path_unify_check: %d violation(s) in %s" %
              (len(problems), TARGET_REL))
        for cid, msg in problems:
            print("  [%s] %s" % (cid, msg))
        return 1
    print("offload_relative_path_unify_check: %s — OFFLOAD-RELATIVE-PATH-UNIFY GATE OK"
          % TARGET_REL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

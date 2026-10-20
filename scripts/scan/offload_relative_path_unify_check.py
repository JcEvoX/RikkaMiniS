#!/usr/bin/env python3
"""
Gate: FIX-I (deep-audit-1024 ISSUE-09) + FIX-C (deep-audit-1033 ISSUE-03) —
every path-taking offload handler must route a relative path through
[resolveMaybeRelative] (the single join implementation defined in
`ConfigOffloadHandler.kt`) instead of keeping private, drifting copies and
cwd-less call chains.

The 1024 bug (one function, contradicting itself):
  - `cmdRun`'s `--output` still refused any relative path with
    `invalid_output_path`, while the SAME `cmdRun` forwarded `request.cwd` for
    `--input` / `--system-file`;
  - `readLinuxPath` kept a hand-written inline join (`$cwd/$linuxPath`), which
    `BrowserUseOffloadHandler.kt`'s KDoc forbids ("single join implementation");
  - the `image_url` chain (`parseMessages` -> `parseMessageArray` ->
    `resolveImageUrl`) had no `cwd` parameter, so a relative image path inside
    `prompt.json` (`{"type":"image_url","image_url":{"url":"chart.png"}}`)
    threw `ImageInputError`.

The 1033 bug (the last handler that never adopted the contract):
  - `SpeechOffloadHandler.cmdTranscribe` was dispatched as
    `cmdTranscribe(args, request.sessionId)` — `request.cwd` dropped — and
    `resolveSourcePath` opened with
    `if (trimmed.isEmpty() || !trimmed.startsWith("/")) return null`, so
    `android-speech transcribe --source note.wav` from the shell's cwd always
    came back `file_exists:false` / `resolved_host_path:""`.

Checks run on comment-stripped text, so prose in a KDoc cannot satisfy them:
  ModelUseOffloadHandler.kt
    C1  `readLinuxPath(...)`      body calls `resolveMaybeRelative(`.
    C2  `cmdRun(...)`             body normalises `--output` through
                                  `resolveMaybeRelative(`.
    C3  `resolveImageUrl(...)`    body calls `resolveMaybeRelative(`.
    C4  the file keeps NO inline cwd join (`"$cwd...` string template) — the
        single implementation may only live in `ConfigOffloadHandler.kt`.
  SpeechOffloadHandler.kt
    S1  `resolveSourcePath(...)`  body calls `resolveMaybeRelative(`.
    S2  every `cmdTranscribe(...)` / `resolveSourcePath(...)` occurrence in the
        file carries `cwd` in its argument list — i.e. the cwd really enters
        the call chain. Judged on the decision point, not on argument order or
        on the expression spelling (`request.cwd` and a local `cwd` both count).
    S3  `resolveSourcePath` no longer decides relative-vs-absolute itself
        (no `startsWith("/")` in its body) and the file carries no hand-written
        `"$cwd` join.
    S4  privacy edge: inside `cmdTranscribe`, the `--source` classifier runs
        BEFORE the microphone permission gate, and the explicit non-mic branch
        returns a NON-zero result carrying an error payload — a `--source` the
        user typed must never be silently swapped for a live mic, and must
        never come back as a success.

Boundary (documented, not a bug): S4 pins the current literal shape. A refactor
that moves the error payload into a helper (e.g. `return notSupported(...)`)
reds the gate; that is fail-closed on purpose — the helper must then be named
in the criterion.

Usage: python3 scripts/scan/offload_relative_path_unify_check.py [repo_root]
       python3 scripts/scan/offload_relative_path_unify_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import re
import shutil
import sys
import tempfile

SRC = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload"
TARGET_REL = SRC + "/ModelUseOffloadHandler.kt"
SPEECH_REL = SRC + "/SpeechOffloadHandler.kt"
CALL = "resolveMaybeRelative("
# deep-audit-1034 A2 / ISSUE-03: the same single-source contract, third target —
# the Shizuku handler resolves the paths it hands to the privileged service.
SHIZUKU_REL = SRC + "/ShizukuOffloadHandler.kt"
# A Kotlin string template that joins a cwd itself — the shape that must not
# reappear in either target file (the join belongs to resolveMaybeRelative).
INLINE_JOIN = '"$cwd'
# Argument lists of the two Speech call-chain links (no nested parens in either).
CHAIN_RE = re.compile(r"\b(cmdTranscribe|resolveSourcePath)\s*\(([^()]*)\)")


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
                    # closes the `${...}` expression: back into the literal, but
                    # keep the brace so the text stays balanced for the scanners
                    # that run after this one
                    stack.pop(); out.append(c); i += 1; continue
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


def strip_strings(code):
    """Blank the *contents* of string literals in comment-free code.

    A criterion that scans the whole file for a call site must not be driven by
    a token that only appears inside a log message — `resolveSourcePath`'s own
    failure line reads `"resolveSourcePath('$trimmed') failed: ${e.message}"`,
    which a naive whole-file regex happily matches as a cwd-less call. Template
    expressions (`${…}`) resume as code, so an interpolated `args.get("input")`
    stays visible; raw strings are blanked whole.
    """
    out = []
    stack = [["code", 0]]
    i = 0
    n = len(code)
    while i < n:
        st = stack[-1]
        if st[0] == "code":
            if code.startswith('"""', i):
                stack.append(["raw", 0]); out.append("   "); i += 3; continue
            c = code[i]
            if c == '"':
                stack.append(["str", 0]); out.append(c); i += 1; continue
            if len(stack) > 1 and c == "{":
                st[1] += 1
            elif len(stack) > 1 and c == "}":
                if st[1] == 0:
                    # closes the `${…}` we are inside: back to the literal
                    stack.pop(); out.append(c); i += 1; continue
                st[1] -= 1
            out.append(c); i += 1; continue
        if st[0] == "str":
            c = code[i]
            if c == "\\":
                out.append("  ")
                i += 2
                continue
            if code.startswith("${", i):
                stack.append(["code", 0]); out.append("${"); i += 2; continue
            if c == '"':
                stack.pop(); out.append(c); i += 1; continue
            out.append(c if c == "\n" else " "); i += 1; continue
        if code.startswith('"""', i):
            stack.pop(); out.append('"""'); i += 3; continue
        out.append(code[i] if code[i] == "\n" else " "); i += 1
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


# NOTE: this second definition shadows the template-aware one above (the module
# keeps both; the LAST binding wins). Its `in_str` state does not recurse into
# `${…}`, so a quote nested inside a template expression ends the literal early
# — harmless for the criteria below (which match tokens, not braces) but do not
# rely on it for a brace-sensitive criterion without re-checking the slice.
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


def check_speech_source(raw):
    """SpeechOffloadHandler.kt half of the contract (deep-audit-1033 FIX-C)."""
    code = strip_comments(raw)
    problems = []

    resolver = declaration_slice(code, "fun resolveSourcePath(")
    cmd_body = declaration_slice(code, "fun cmdTranscribe(")

    # S1 — the resolver delegates the join.
    if not resolver:
        problems.append(("S1", "declaration 'fun resolveSourcePath(' not found in the target file"))
    elif CALL not in resolver:
        problems.append(("S1", "resolveSourcePath() does not call resolveMaybeRelative() — a "
                               "relative --source is still refused instead of joined onto cwd"))

    # S2 — the cwd really enters the call chain (decision point, not spelling).
    chains = CHAIN_RE.findall(strip_strings(code))
    if not any(name == "cmdTranscribe" for name, _ in chains):
        problems.append(("S2", "no cmdTranscribe(...) occurrence found — declaration vanished"))
    for name, args in chains:
        if "cwd" not in args:
            problems.append(("S2", "%s(...) drops cwd from its arguments: %s(%s)"
                             % (name, name, args.strip())))

    # S3 — relative-vs-absolute is the join's decision, and the join is single-source.
    if resolver and "startsWith(\"/\")" in resolver:
        problems.append(("S3", "resolveSourcePath() re-implements the relative-vs-absolute "
                               "decision (startsWith(\"/\") in its body) — it belongs to "
                               "resolveMaybeRelative()"))
    if INLINE_JOIN in code:
        problems.append(("S3", "a hand-written inline cwd join ('\"$cwd...') is back — the "
                               "join must stay single-source in resolveMaybeRelative()"))

    # S4 — privacy edge: classify --source before asking for the microphone, and
    # an explicit non-mic source must return an error, never a success.
    if not cmd_body:
        problems.append(("S4", "declaration 'fun cmdTranscribe(' not found in the target file"))
    else:
        classified = cmd_body.find("sourceIsMic(")
        mic_gate = cmd_body.find("ensureMicPermission(")
        if classified < 0:
            problems.append(("S4", "cmdTranscribe() never classifies --source (sourceIsMic)"))
        if mic_gate < 0:
            problems.append(("S4", "cmdTranscribe() never reaches the microphone permission "
                                   "gate (ensureMicPermission)"))
        if classified >= 0 and mic_gate >= 0:
            if mic_gate < classified:
                problems.append(("S4", "the microphone permission gate runs BEFORE the --source "
                                       "classifier — an explicit --source can fall through to a "
                                       "live mic"))
            else:
                branch = cmd_body[classified:mic_gate]
                if '"error"' not in branch:
                    problems.append(("S4", "the explicit non-mic source branch carries no error "
                                           "payload before the mic gate"))
                codes = re.findall(r"return NativeOffloadResult\(\s*(\d+)", branch)
                if not codes:
                    problems.append(("S4", "the explicit non-mic source branch never returns a "
                                           "result before the mic gate"))
                elif codes[-1] == "0":
                    problems.append(("S4", "the explicit non-mic source branch returns success "
                                           "(exit 0) — a missing file must not read as a "
                                           "transcription"))
    return problems


def check_shizuku_source(raw):
    """ShizukuOffloadHandler.kt half of the contract (deep-audit-1034 A2 / ISSUE-03).

    The Shizuku service runs OUTSIDE the sandbox: the paths this handler hands to
    `ls`/`rm`/`cp`/`pm install` must be guest→host translated first. The join is
    single-source ([resolveMaybeRelative]), the session is what makes the mapping
    possible at all, and a path that does not resolve must refuse.
    """
    code = strip_comments(raw)
    problems = []
    resolver = declaration_slice(code, "private fun shizukuHostPath(")

    # Z1 — the guest→host translation delegates the join to the single source.
    if not resolver:
        problems.append(("Z1", "declaration 'private fun shizukuHostPath(' not found in the target file"))
    elif CALL not in resolver:
        problems.append(("Z1", "shizukuHostPath() does not call resolveMaybeRelative() — a relative "
                               "path handed to the privileged service cannot be joined onto cwd"))

    # Z2 — the cwd really enters the chain (decision point, not spelling).
    if resolver and "request.cwd" not in resolver:
        problems.append(("Z2", "shizukuHostPath() never passes request.cwd — a relative path has "
                               "nothing to resolve against"))

    # Z3 — a guest path cannot be used by an out-of-sandbox service: map it into
    #      this session's host tree, or the operand is a silent no-op.
    if resolver and "resolvePathForSession(" not in resolver:
        problems.append(("Z3", "shizukuHostPath() does not resolve through "
                               "PRootKernel.resolvePathForSession — a guest path cannot reach the "
                               "Shizuku service's host filesystem"))

    # Z4 — relative-vs-absolute is the join's decision, and the join is single-source.
    if resolver and 'startsWith("/")' in resolver:
        problems.append(("Z4", "shizukuHostPath() re-implements the relative-vs-absolute decision "
                               "(startsWith(\"/\") in its body) — it belongs to resolveMaybeRelative()"))
    if INLINE_JOIN in code:
        problems.append(("Z4", "a hand-written inline cwd join ('\"$cwd...') is back — the join must "
                               "stay single-source in resolveMaybeRelative()"))
    return problems

def check_tree(root):
    problems = []
    main_path = os.path.join(root, TARGET_REL)
    if not os.path.isfile(main_path):
        problems.append(("C0", "target file missing: %s" % TARGET_REL))
    else:
        with open(main_path, "r", encoding="utf-8") as fh:
            problems.extend(check_source(fh.read()))
    speech_path = os.path.join(root, SPEECH_REL)
    if not os.path.isfile(speech_path):
        problems.append(("S0", "target file missing: %s" % SPEECH_REL))
    else:
        with open(speech_path, "r", encoding="utf-8") as fh:
            problems.extend(check_speech_source(fh.read()))
    shizuku_path = os.path.join(root, SHIZUKU_REL)
    if not os.path.isfile(shizuku_path):
        problems.append(("Z0", "target file missing: %s" % SHIZUKU_REL))
    else:
        with open(shizuku_path, "r", encoding="utf-8") as fh:
            problems.extend(check_shizuku_source(fh.read()))
    return main_path, problems


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

# Mirrors the post-fix SpeechOffloadHandler shape (cwd in the chain, join in the
# resolver, classifier ahead of the mic gate, error payload in between).
_SPEECH_CLEAN = '''package com.rikkaminis.app.sandbox.offload

class SpeechOffloadHandler(private val context: Context) : NativeOffloadHandler {
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(request.argv.drop(1))
        return when (args.positional.firstOrNull()) {
            "transcribe", "listen" -> cmdTranscribe(args, request.sessionId, request.cwd)
            else -> NativeOffloadResult(2, "usage")
        }
    }

    private fun cmdTranscribe(args: OffloadArgs, sessionId: String?, cwd: String?): NativeOffloadResult {
        val source = args.get("source")
        if (source != null && !sourceIsMic(source)) {
            val resolved = resolveSourcePath(source, sessionId, cwd)
            val body = JSONObject().put("error", "not_supported")
                .put("requested_path", source)
                .toString()
            return NativeOffloadResult(2, OffloadOutput.formatBody(body, args))
        }
        ensureMicPermission(args)?.let { return it }
        return NativeOffloadResult(0, "ok")
    }

    private fun resolveSourcePath(source: String, sessionId: String?, cwd: String?): File? {
        val trimmed = source.trim()
        if (trimmed.isEmpty()) return null
        val guestPath = resolveMaybeRelative(trimmed, cwd)
        return try {
            PRootKernel.resolvePathForSession(sessionId, guestPath, context)
        } catch (e: Throwable) {
            null
        }
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

# The Before shape of deep-audit-1033 ISSUE-03, verbatim: no join, hard reject.
_SPEECH_BEFORE = _SPEECH_CLEAN.replace(
    "        val guestPath = resolveMaybeRelative(trimmed, cwd)\n"
    "        return try {\n"
    "            PRootKernel.resolvePathForSession(sessionId, guestPath, context)",
    "        if (!trimmed.startsWith(\"/\")) return null\n"
    "        return try {\n"
    "            PRootKernel.resolvePathForSession(sessionId, trimmed, context)",
)

# cwd dropped from the dispatch (the one-line defect) — the join stays.
_SPEECH_NO_CWD = _SPEECH_CLEAN.replace(
    "cmdTranscribe(args, request.sessionId, request.cwd)",
    "cmdTranscribe(args, request.sessionId)",
)

# The microphone gate hoisted above the classifier: the privacy edge reopens.
_SPEECH_MIC_FIRST = _SPEECH_CLEAN.replace(
    "        if (source != null && !sourceIsMic(source)) {",
    "        ensureMicPermission(args)?.let { return it }\n"
    "        if (source != null && !sourceIsMic(source)) {",
).replace("\n        ensureMicPermission(args)?.let { return it }\n        return NativeOffloadResult(0, \"ok\")",
          "\n        return NativeOffloadResult(0, \"ok\")")

# Join present, but a stray hand-written join elsewhere → S3 only.
_SPEECH_STRAY_JOIN = _SPEECH_CLEAN.replace(
    "        ensureMicPermission(args)?.let { return it }",
    '        ensureMicPermission(args)?.let { return it }\n'
    '        val fallbackJoin = "$cwd/legacy.wav"',
)


_SHIZUKU_CLEAN = '''package com.rikkaminis.app.sandbox.offload

class ShizukuOffloadHandler(private val context: Context) : NativeOffloadHandler {
    private fun shizukuHostPath(raw: String, request: NativeOffloadRequest): String? {
        val guestPath = resolveMaybeRelative(raw.trim(), request.cwd)
        return PRootKernel.resolvePathForSession(request.sessionId, guestPath, context)?.absolutePath
    }

    private fun fileLs(path: String?, request: NativeOffloadRequest): NativeOffloadResult {
        val host = shizukuHostPath(path, request)
            ?: return errEnvelope("PATH_NOT_RESOLVED", "file ls: '$path' does not resolve", args)
        return ok(ShizukuManager.runProcess(arrayOf("ls", host)))
    }
}
'''
# The base shape: no shared join at all (a relative path dies) — and with the
# join gone the cwd is gone too, so both Z1 and Z2 fire (same shape as S1S3).
_SHIZUKU_NO_JOIN = _SHIZUKU_CLEAN.replace(
    "resolveMaybeRelative(raw.trim(), request.cwd)",
    "raw.trim()",
)
# The join is there but the cwd is not passed: a relative path has nothing to
# resolve against.
_SHIZUKU_NO_CWD = _SHIZUKU_CLEAN.replace(
    "resolveMaybeRelative(raw.trim(), request.cwd)",
    'resolveMaybeRelative(raw.trim(), "")',
)
# Resolved into the guest spelling: the out-of-sandbox service would get a path
# that does not exist on the host.
_SHIZUKU_GUEST_ONLY = _SHIZUKU_CLEAN.replace(
    "PRootKernel.resolvePathForSession(",
    "PRootKernel.resolveHostPath(",
)
# A second relative-vs-absolute decision grown inside the resolver.
_SHIZUKU_INLINE_DECISION = _SHIZUKU_CLEAN.replace(
    "val guestPath = resolveMaybeRelative(raw.trim(), request.cwd)",
    'val guestPath = if (raw.startsWith("/")) raw '
    "else resolveMaybeRelative(raw.trim(), request.cwd)",
)


def _fixture_root(model_text, speech_text=_SPEECH_CLEAN, shizuku_text=_SHIZUKU_CLEAN):
    root = tempfile.mkdtemp(prefix="offload-relpath-gate-selftest-")
    for rel, text in ((TARGET_REL, model_text), (SPEECH_REL, speech_text),
                      (SHIZUKU_REL, shizuku_text)):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


def selftest():
    cases = [
        ("clean: all three ModelUse sites delegate to resolveMaybeRelative", _CLEAN, set()),
        ("C1C4: readLinuxPath keeps an inline join", _DIRTY_READLINUX, {"C1", "C4"}),
        ("C2: --output not normalised", _DIRTY_OUTPUT, {"C2"}),
        ("C3: resolveImageUrl has no cwd join", _DIRTY_IMAGE, {"C3"}),
        ("C4 only: all sites clean but a stray inline join remains", _DIRTY_INLINE_ONLY, {"C4"}),
        ("S1S3: the Speech Before shape (hard relative reject, no join)",
         _CLEAN, {"S1", "S3"}, _SPEECH_BEFORE),
        ("S2: the Speech dispatch drops request.cwd", _CLEAN, {"S2"}, _SPEECH_NO_CWD),
        ("S4: the mic gate runs before the --source classifier",
         _CLEAN, {"S4"}, _SPEECH_MIC_FIRST),
        ("S3 only: the Speech join is present but a stray inline join is back",
         _CLEAN, {"S3"}, _SPEECH_STRAY_JOIN),
        # ── [da1034-A2 ISSUE-03] the Shizuku half ──────────────────────────
        ("Z1Z2: the Shizuku resolver drops the shared join (and the cwd with it)",
         _CLEAN, {"Z1", "Z2"}, _SPEECH_CLEAN, _SHIZUKU_NO_JOIN),
        ("Z2: the Shizuku resolver never passes request.cwd",
         _CLEAN, {"Z2"}, _SPEECH_CLEAN, _SHIZUKU_NO_CWD),
        ("Z3: the Shizuku resolver keeps the guest spelling",
         _CLEAN, {"Z3"}, _SPEECH_CLEAN, _SHIZUKU_GUEST_ONLY),
        ("Z4: a second relative-vs-absolute decision inside the resolver",
         _CLEAN, {"Z4"}, _SPEECH_CLEAN, _SHIZUKU_INLINE_DECISION),
    ]
    failures = []
    for case in cases:
        label, model_text, expected = case[0], case[1], case[2]
        speech_text = case[3] if len(case) > 3 else _SPEECH_CLEAN
        shizuku_text = case[4] if len(case) > 4 else _SHIZUKU_CLEAN
        root = _fixture_root(model_text, speech_text, shizuku_text)
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
    print("OFFLOAD_RELATIVE_PATH_UNIFY_SELFTEST=PASS (13 fixtures: 12 violation "
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
        print("offload_relative_path_unify_check: %d violation(s) in %s + %s" %
              (len(problems), TARGET_REL, os.path.basename(SPEECH_REL)))
        for cid, msg in problems:
            print("  [%s] %s" % (cid, msg))
        return 1
    print("offload_relative_path_unify_check: %s + %s + %s — "
          "OFFLOAD-RELATIVE-PATH-UNIFY GATE OK"
          % (TARGET_REL, os.path.basename(SPEECH_REL), os.path.basename(SHIZUKU_REL)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

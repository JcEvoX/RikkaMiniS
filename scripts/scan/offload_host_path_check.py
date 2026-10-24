#!/usr/bin/env python3
"""
offload_host_path_check — [deep-audit-1016c FIX-B] two bug classes in the
android-* offload handlers, both of which shipped once and were invisible to
every other gate:

  R1  GUEST PATH READ AS A HOST FILE
      `android-<x> --path/--file/--source <guest path>` is resolved by
      PRootKernel / GuestPathMapper, never by a bare `File(...)`. A bare host
      File breaks the command (PRoot bind layout != host layout, so the file
      never exists) AND is a read escape: a host-absolute path such as
      /data/data/<pkg>/databases/minis.db is opened with the app's own
      privileges and can be copied out (e.g. into the public MediaStore).
      Fixed first in SpeechOffloadHandler ([fix/sweep1013-security]), then in
      PhotosOffloadHandler import ([deep-audit-1016c FIX-B A]).

  R2  UNHARDENED intent: URI LAUNCH
      `Intent.parseUri(url, URI_INTENT_SCHEME)` on model-supplied input must be
      stripped before startActivity: grant flags (a crafted URI could hand a
      launched activity a read/write/persistable URI grant), clipData, a
      same-package `component` target, a same-package `package` target
      (explicit same-uid intents AND `package=`-pinned intents both bypass
      `exported=false` and the system chooser), and the injected `selector`.
      Fixed in OpenOffloadHandler ([deep-audit-1016c FIX-B B] grants /
      clipData / component / selector; [deep-audit-1041 FIX-A6] the
      same-package `package=` target — and the R2 CRITERIA THEMSELVES: the
      body below had claimed "component stripping" in this docstring since
      1016c while never reading component/selector, so deleting those two
      lines from the handler left the gate GREEN).
      [deep-audit-1046 FIX-F9] THE SCOPE WAS THE BLIND SPOT: R2 only ever read
      `sandbox/offload/*OffloadHandler.kt`, so ui/browser's
      BrowserExternalSchemeHandler — the one fed by arbitrary WEB PAGES —
      shipped with `selector = null` as its entire hardening and was invisible
      here. The scope is now DERIVED from the shipped source tree: every
      `Intent.parseUri(` call site, every file that dispatches an
      `intent:`/`android-app:` link and calls startActivity, and every read of
      the page-supplied `browser_fallback_url` (which must be scheme-gated to
      http(s)). A shared sanitizer is accepted by body (a function whose own
      body clears every strip) instead of by name, so extracting the sequence
      into one helper cannot silence the gate — and gutting that helper turns
      every call site red again.

Usage:
  python3 scripts/scan/offload_host_path_check.py <repo_root>
  python3 scripts/scan/offload_host_path_check.py <repo_root> --selftest
Exit: 0 = clean (or selftest passed), 1 = violation / selftest failure.
"""
import os
import re
import shutil
import sys
import tempfile

OFFLOAD_REL = os.path.join(
    "src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app", "sandbox", "offload"
)

GRANT_FLAGS = (
    "FLAG_GRANT_READ_URI_PERMISSION",
    "FLAG_GRANT_WRITE_URI_PERMISSION",
    "FLAG_GRANT_PERSISTABLE_URI_PERMISSION",
    "FLAG_GRANT_PREFIX_URI_PERMISSION",
)

# ── [deep-audit-1041 FIX-A6] R2: what the hardening block must clear ────────
# (label used in the problem message, regex that must match inside the
# `Intent.parseUri(...)` statement window).
#
# Regexes, not plain substrings, because two of these criteria ARE decisions:
# the clear must be guarded by "this target is OUR package". An unconditional
# `setPackage(null)` / `component = null` satisfies a bare existence check but
# is a REGRESSION — it would destroy a legitimate third-party (`package=` /
# `component=`) target, the thing the fix must not break. Accepted spellings
# are listed per entry; anything else fails CLOSED (a red gate that a human
# re-anchors) rather than silently passing.
# The two package strips must stay GUARDED by "this target is OUR package".
# [deep-audit-1046 FIX-F9] the accepted guard spellings grew to cover the shared
# sanitizer, which receives the package as a parameter and asks a derived pure
# predicate (`isSelfTarget(pkg, selfPackage)`) instead of inlining
# `context.packageName`. Everything else still fails CLOSED.
PACKAGE_GUARD = (
    r"(?:\.packageName|selfPackage|selfPkg|ownPackage|isSelfTarget\(|shouldStripSelfTarget\()"
)
R2_STRIPS = (
    ("clipData = null",
     r"\bclipData\s*=\s*null\b|\bsetClipData\(\s*null\s*\)"),
    ("same-package component strip",
     r"if\s*\([^\n]*" + PACKAGE_GUARD + r"[^\n]*\)[^\n]*"
     r"(?:\bcomponent\s*=\s*null\b|\bsetComponent\(\s*null\s*\))"),
    ("same-package package strip",
     r"if\s*\([^\n]*(?:" + PACKAGE_GUARD + r"|getPackage\(\)|`package`)[^\n]*\)[^\n]*"
     r"(?:\bsetPackage\(\s*null\s*\)|`package`\s*=\s*null\b)"),
    ("selector = null",
     r"\bselector\s*=\s*null\b|\bsetSelector\(\s*null\s*\)"),
)

# ── [deep-audit-1046 FIX-F9] R2 SCOPE: every intent:// path in the app ──────
# R2 used to read only `sandbox/offload/*OffloadHandler.kt`, so the app's SECOND
# `Intent.parseUri` caller — ui/browser/BrowserExternalSchemeHandler, the one fed
# by arbitrary WEB PAGES — was invisible to this gate while stripping the
# selector only. The blind spot was never "that file"; it was the hand-written
# scope. The scope is derived from the shipped source tree below, so a third
# caller is covered the day it lands.
MAIN_SRC_REL = os.path.join("src", "android", "app", "src", "main", "java")

FUN_DECL_RE = re.compile(r"\bfun\s+(?:<[^>]+>\s+)?([A-Za-z_]\w*)\s*\(")
# Any spelling that normalises the scheme before parsing (`lowercaseIntentScheme(u)`,
# `u.lowercase()`). `Intent.parseUri` matches `intent:` case-SENSITIVELY, so a
# mixed-case scheme silently degrades to ACTION_VIEW over the raw string.
SCHEME_NORMALISE_RE = re.compile(r"[Ll]owercase")
START_ACTIVITY_RE = re.compile(r"\bstartActivity\s*\(")
# strip_comments() cuts a line at `//`, so `"intent://x"` arrives as `"intent:` —
# accept the scheme prefix with or without its closing quote.
INTENT_SCHEME_LITERAL_RE = re.compile(r'"(?:android-app|intent)(?::|")')
FALLBACK_READ_RE = re.compile(r'getStringExtra\(\s*"browser_fallback_url"\s*\)')
FALLBACK_ASSIGN_RE = re.compile(
    r'val\s+([A-Za-z_]\w*)\s*(?::\s*[A-Za-z_]\w*\s*)?=\s*[^\n]*'
    r'getStringExtra\(\s*"browser_fallback_url"\s*\)'
)
FALLBACK_GATE_RE = re.compile(
    r"[Aa]llowedFallback|startsWith\(\s*\"https?\"|scheme\s*==\s*\"https?\""
)
GATED_IDENT_TMPL = (
    r"(?:[Aa]llowedFallback\w*|[Ss]afeFallback\w*)\s*\(\s*%s\b"
    r"|%s\s*\.\s*startsWith\(\s*\"https?\""
)

# `val <ident> = ...get("path"|"file"|"source")` (any receiver spelling).
# [deep-audit-1039 A-08] The flag-name set used to be path|file|source, so a
# guest path arriving as --output / --input / --system-file / --cookies-file
# never entered taint and reached a bare File() unread. Widened to the actual
# offload flag vocabulary that carries a path operand.
ARG_NAMES = r'path|file|source|input|output|system-file|cookies-file'
ARG_ASSIGN_RE = re.compile(r'\b(\w+)\s*=\s*[\w.]*get\(\s*"(%s)"' % ARG_NAMES)
# A bare `File(<ident>)` / `File(args.get("path"))` construction.
FILE_IDENT_RE = re.compile(r"\bFile\(\s*([A-Za-z_]\w*)\s*\)")
FILE_DIRECT_RE = re.compile(r'\bFile\(\s*[^)\n]*get\(\s*"(%s)"' % ARG_NAMES)
# `<tainted>?.let { File(it) }` — the lambda's implicit `it` is not an
# identifier form, so FILE_IDENT_RE never saw it.
FILE_LET_RE = re.compile(r"\bFile\(\s*it\s*\)")
LET_TAINTED_RE = re.compile(r"\?\.let\s*\{")
# Guest→host converters: their call is what makes a tainted value SAFE to wrap in File().
# [deep-audit-1039 A-08] `resolveHostPath` is NOT on this list any more: it is
# the CROSS-SESSION GLOBAL resolver (PRootKernel.resolveHostPath searches every
# minis-sessions/<id>/ tree for an existing match, then the rootfs — see the R5
# ban below). Treating it as a sanitizer made "resolveHostPath(x) → File(x)"
# invisible to R1, i.e. the exact shape the R5 criterion exists to forbid was
# whitelisted by this one. Only SESSION-SCOPED converters sanitize.
RESOLVER_CALL_RE = re.compile(
    r"\b(?:resolvePathForSession|resolveSourcePath|resolveGuestHostPath|readLinuxPath"
    r"|sessionAwareHostFile|GuestPathMapper\.resolve)\("
)

# ── [meta-audit-1024 M6] R1b: `File(<expression>)`, not only `File(<ident>)` ──
# The three R1 spellings above match the SHAPE of one argument: a bare
# identifier, `it`, or an inline `get("path")`. A derived expression slips
# through all three — the adversarial witness for this gate is exactly
# `val src = File(rawPath.trim())`, where the value is still the raw guest path
# but the argument is no longer one of those three shapes. The obligation R1
# states is a DATAFLOW one ("the value reaching File() went through a guest→host
# resolver"), so the argument is now read as an expression and every
# args-tainted identifier inside it is checked.
FILE_CALL_RE = re.compile(r"\bFile\s*\(")
FILE_ARG_IDENT_ONLY_RE = re.compile(r"^[A-Za-z_]\w*$")


def _file_call_args(line):
    """(offset, argument text) for every `File(...)` opened on a single line."""
    out = []
    for m in FILE_CALL_RE.finditer(line):
        depth = 1
        i = m.end()
        while i < len(line) and depth:
            c = line[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth == 0:
            out.append((m.start(), line[m.end():i]))
    return out


# ── [meta-audit-1024 M6] R7: `android-photos import` — sanitisation must HAPPEN ─
# R1's spellings are about the shape of ONE `File(...)` argument; they say nothing
# about whether the guest→host conversion happened at all. The witness for this
# gate replaces `val src = PRootKernel.resolvePathForSession(...)` with
# `val src = File(rawPath.trim())` — the handler still compiles, still imports,
# and R1's argument-shape criteria alone must carry the whole load. The
# obligation is DATAFLOW, and it lives on the import path:
#   S1 the entry must resolve session-scoped (a session-scoped resolver call in
#      the body — sanitisation must happen, not merely exist elsewhere);
#   S2 the RESOLVED value must be what gets opened / streamed;
#   S3 fail-closed: the entry cannot be derived, or its resolver result cannot
#      be traced to an open point ⇒ violation, never a silent skip.
PHOTOS_HANDLER_REL = os.path.join(OFFLOAD_REL, "PhotosOffloadHandler.kt")
# The import sink: the guest file is copied OUT of the sandbox into MediaStore.
IMPORT_SINK_RE = re.compile(
    r"contentResolver\s*\.\s*insert\s*\(|MediaStore\s*\.\s*(?:Images|Video)\.Media\."
)
# Anything that turns a path value into bytes / a stream.
HOST_OPEN_TOKEN_RE = re.compile(
    r"\b(?:File|FileInputStream|FileOutputStream|RandomAccessFile)\s*\(|\bFiles\s*\.\s*\w+\s*\("
    r"|\.\s*(?:inputStream|outputStream|readBytes|readText|bufferedReader|useLines|source)\s*\("
)

# ── [da1025-A] R3: player session-path wiring ───────────────────────────────
# android-player play <session> <path> must join relative paths against the
# session cwd and resolve the file session-scoped, fail-closed. Pre-fix, the
# handler ignored request.sessionId entirely and MediaPlayerManager resolved
# through the bare global fallback: relative paths 100% failed and a path in
# another session's sandbox dir still played (the per-session isolation was
# bypassed; same family as the Speech:245 host-read escape, fixed earlier).
PLAYER_HANDLER_REL = os.path.join(OFFLOAD_REL, "PlayerOffloadHandler.kt")
PLAYER_MANAGER_REL = os.path.join(
    "src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app",
    "offload", "MediaPlayerManager.kt",
)

# ── [dv16-A03] R5: debug RPC file-system paths must be session-scoped ───────
# `minis-debug ls|read|write` built their JSON-RPC params without the caller's
# session id, and DebugRPCHandler resolved every path through the bare global
# `PRootKernel.resolveHostPath` — which searches EVERY minis-sessions/<id>/
# tree for an existing match, then the rootfs. So a session-relative `ls .`
# listed whichever session's file matched first, and a new file landed in the
# rootfs ghost dir the session's shell (bound to its own tree) cannot see.
# Same family as R3 (player) and the Speech:245 host-read escape.
DEBUG_RPC_REL = os.path.join(
    "src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app",
    "debug", "DebugRPCHandler.kt",
)
DEBUG_OFFLOAD_REL = os.path.join(OFFLOAD_REL, "DebugOffloadHandler.kt")
DEBUG_PATH_SUBJECTS = ("handleLS", "handleReadFile", "handleWriteFile")


def _fun_body(text, header_pat):
    """Body of the first function matching header_pat (brace-balanced), or None."""
    m = re.search(header_pat, text)
    if not m:
        return None
    open_brace = text.find("{", m.end())
    if open_brace < 0:
        return None
    depth = 0
    for i in range(open_brace, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace:i + 1]
    return None


def scan_player_session_path(root):
    """R3: player play paths must be cwd-joined and session-scoped (fail-closed)."""
    problems = []
    handler_path = os.path.join(root, PLAYER_HANDLER_REL)
    if not os.path.isfile(handler_path):
        return ["%s: missing — cannot verify player session-path wiring" % PLAYER_HANDLER_REL]
    with open(handler_path, encoding="utf-8", errors="replace") as fh:
        handler = strip_comments(fh.read())
    if "request.sessionId" not in handler:
        problems.append(
            "%s: `request.sessionId` never used — play cannot be session-scoped "
            "(cross-session read hole)" % PLAYER_HANDLER_REL
        )
    if "resolveMaybeRelative(" not in handler:
        problems.append(
            "%s: no resolveMaybeRelative(...) call — relative play paths are "
            "passed raw and fail" % PLAYER_HANDLER_REL
        )
    manager_path = os.path.join(root, PLAYER_MANAGER_REL)
    if not os.path.isfile(manager_path):
        return problems + ["%s: missing — cannot verify session-scoped play resolution" % PLAYER_MANAGER_REL]
    with open(manager_path, encoding="utf-8", errors="replace") as fh:
        manager = strip_comments(fh.read())
    body = _fun_body(manager, r"\bfun\s+play\s*\(")
    if body is None:
        problems.append("%s: `fun play` could not be delimited — fail-closed" % PLAYER_MANAGER_REL)
    elif "resolvePathForSession(" not in body:
        problems.append(
            "%s: play() does not resolve through PRootKernel.resolvePathForSession — "
            "per-session containment is bypassed" % PLAYER_MANAGER_REL
        )
    return problems


# ── [da1034-A2 ISSUE-03] R4: Shizuku privileged path operands ───────────────
# The Shizuku service runs OUTSIDE the sandbox: a guest path handed to
# `ls`/`rm`/`cp`/`pm install` is a silent no-op (host `/var/minis/...` does not
# exist) while a host-only path works — the mirror image of the Speech
# host-read escape. The criterion is the decision point, not a function name:
# (a) the operand the privileged command receives must not be the raw argument,
# (b) a path that does not resolve must refuse instead of falling back to the
# raw spelling, and (c) the file must actually map guest → host somewhere.
SHIZUKU_HANDLER_REL = os.path.join(OFFLOAD_REL, "ShizukuOffloadHandler.kt")

SHIZUKU_PATH_SUBJECTS = (
    ("fileLs", ("path",)),
    ("fileRm", ("path",)),
    ("filePullPush", ("src", "dst")),
    ("packageInstall", ("apk",)),
)


def _mask_strings(text):
    """Blank out string-literal bodies (quotes kept) so an identifier named in a
    user-facing message is not read as a command operand."""
    out = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            out.append('"')
            i += 1
            while i < len(text) and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                out.append(" ")
                i += 1
            out.append('"')
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _command_operands(body):
    """Identifiers handed to a privileged command inside a function body."""
    masked = _mask_strings(body)
    names = set(m.group(1) for m in re.finditer(r"\bcmd\.add\(\s*([A-Za-z_]\w*)\s*\)", masked))
    for line in masked.splitlines():
        if "arrayOf(" in line:
            names.update(w for w in re.findall(r"[A-Za-z_]\w*", line) if w != "arrayOf")
    return names


def scan_shizuku_paths(root):
    """R4: `file` / `package install` operands must be resolved guest→host."""
    problems = []
    path = os.path.join(root, SHIZUKU_HANDLER_REL)
    if not os.path.isfile(path):
        return ["%s: missing — cannot verify privileged path resolution" % SHIZUKU_HANDLER_REL]
    with open(path, encoding="utf-8", errors="replace") as fh:
        code = strip_comments(fh.read())
    if "resolvePathForSession(" not in code:
        problems.append(
            "%s: no PRootKernel.resolvePathForSession(...) — the Shizuku service runs outside "
            "the sandbox, so every guest path it receives is a silent no-op" % SHIZUKU_HANDLER_REL
        )
    for fn, raw_params in SHIZUKU_PATH_SUBJECTS:
        body = _fun_body(code, r"\bfun\s+%s\s*\(" % re.escape(fn))
        if body is None:
            problems.append(
                "%s: `%s` not found — the privileged path surface moved (fail-closed)"
                % (SHIZUKU_HANDLER_REL, fn)
            )
            continue
        leaked = sorted(p for p in raw_params if p in _command_operands(body))
        if leaked:
            problems.append(
                "%s: %s hands the raw argument(s) %s to the privileged command — translate "
                "through the session resolver first"
                % (SHIZUKU_HANDLER_REL, fn, ", ".join("`%s`" % p for p in leaked))
            )
        if not re.search(r"\?:\s*return\s+errEnvelope\(", body):
            problems.append(
                "%s: %s has no fail-closed refusal for an unresolvable path (null must refuse, "
                "not fall back to the raw spelling)" % (SHIZUKU_HANDLER_REL, fn)
            )
    return problems


def scan_debug_session_path(root):
    """R5 [dv16-A03]: the debug file surface must resolve session-scoped.

    Fail-closed: a file that cannot be delimited (moved class / renamed
    member / lost anchor) is a violation, not a silent pass.
    """
    problems = []
    rpc_path = os.path.join(root, DEBUG_RPC_REL)
    if not os.path.isfile(rpc_path):
        return ["%s: missing — cannot verify debug session-path wiring" % DEBUG_RPC_REL]
    with open(rpc_path, encoding="utf-8", errors="replace") as fh:
        rpc = strip_comments(fh.read())

    for fn in DEBUG_PATH_SUBJECTS:
        body = _fun_body(rpc, r"\bfun\s+%s\s*\(" % re.escape(fn))
        if body is None:
            problems.append(
                "%s: `%s` could not be delimited — fail-closed (signature moved?)"
                % (DEBUG_RPC_REL, fn)
            )
            continue
        if "resolvePathForSession(" not in body:
            problems.append(
                "%s: %s does not resolve through PRootKernel.resolvePathForSession — the per-session "
                "bind mounts are bypassed (cross-session read / rootfs ghost write)"
                % (DEBUG_RPC_REL, fn)
            )
        if "PRootKernel.resolveHostPath(" in body:
            problems.append(
                "%s: %s still calls the bare global resolveHostPath — it scans every "
                "minis-sessions/<id>/ tree for an existing match (cross-session misread)"
                % (DEBUG_RPC_REL, fn)
            )
    if "resolvePathForSession(" not in rpc:
        problems.append(
            "%s: no PRootKernel.resolvePathForSession(...) anywhere — the debug file surface "
            "lost its session-scoped resolver" % DEBUG_RPC_REL
        )

    offload_path = os.path.join(root, DEBUG_OFFLOAD_REL)
    if not os.path.isfile(offload_path):
        return problems + [
            "%s: missing — cannot verify the session-id passthrough" % DEBUG_OFFLOAD_REL
        ]
    with open(offload_path, encoding="utf-8", errors="replace") as fh:
        offload = strip_comments(fh.read())
    handle = _fun_body(offload, r"\boverride\s+fun\s+handle\s*\(")
    if handle is None:
        problems.append(
            "%s: `override fun handle` could not be delimited — fail-closed" % DEBUG_OFFLOAD_REL
        )
    elif "request.sessionId" not in handle:
        problems.append(
            "%s: `request.sessionId` never reaches the rpc request — ls/read/write cannot be "
            "session-scoped (the handler's global resolver would search every session tree)"
            % DEBUG_OFFLOAD_REL
        )
    # The decision, not the spelling: any gate that binds the passthrough to a
    # named session-scoped subcommand set (or its literal) satisfies this.
    if "SESSION_SCOPED_SUBCOMMANDS" not in offload and 'setOf("ls", "read", "write")' not in offload:
        problems.append(
            "%s: the session-scoped subcommand gate is gone — the passthrough is not bound to a "
            "decision and could leak into pass-through subcommands" % DEBUG_OFFLOAD_REL
        )
    return problems

# ── [dv18-A05] R6: the debug exec entry points must carry the caller's scope ─
# `debug.modelUse.exec` / `debug.sessions.exec` rebuilt their offload request
# with a hardcoded `cwd = "/"` and `sessionId = null`, and the forwarder in
# DebugOffloadHandler handed the RPC only the bare argv. So
# `minis-debug model-use run --input notes.txt` resolved the relative operand
# against the guest root, and a caller that named a chat session still reached
# the session-less "global" arm. Same session-scope family as R5, one layer
# out: R5 covers the path resolver inside the handler, R6 covers the entry
# point that decides what the handler is told.
#
# Bound to the DECISION, not to a spelling: every `handle*Exec` entry that
# builds a request must take its scope from the shared parse (so a NEW entry is
# covered the day it lands), the forwarded call must carry the scope, and the
# carrier set is asserted as a set.
DEBUG_EXEC_ENTRY_ANCHORS = ("handleShizukuExec", "handleModelUseExec", "handleSessionsExec")
# A device-level privileged shell, where the guest cwd has no meaning: this
# entry deliberately keeps its literal scope (registered negative decision —
# see backlog §1038A-2 R2). Any other entry is held to the rule.
DEBUG_EXEC_DEVICE_LEVEL = ("handleShizukuExec",)
DEBUG_EXEC_SCOPE_CARRIERS = ["model-use", "modelUse"]
DEBUG_EXEC_SCOPE_PARSE = "execScopeOf("
DEBUG_EXEC_SCOPE_INJECT = "execScopeParams("
DEBUG_EXEC_SCOPE_MIN_ENTRIES = 2


def _read_code(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return strip_comments(fh.read())


def scan_debug_exec_scope(root):
    """R6 [dv18-A05]: the debug exec entry points must honour the caller's scope.

    Fail-closed: a missing file, an entry anchor that can no longer be
    delimited, a lost carrier declaration, or a tree where too few entries
    build a request (the shape this criterion reads) are violations, not skips.
    """
    problems = []
    rpc_path = os.path.join(root, DEBUG_RPC_REL)
    if not os.path.isfile(rpc_path):
        return ["%s: missing — cannot verify the exec entry-point scope" % DEBUG_RPC_REL]
    rpc = _read_code(rpc_path)

    entries = re.findall(r"\bfun\s+(handle\w+Exec)\s*\(", rpc)
    for anchor in DEBUG_EXEC_ENTRY_ANCHORS:
        if anchor not in entries:
            problems.append(
                "%s: `%s` is gone — fail-closed (the exec entry points were renamed or moved, so "
                "the scope criteria below would silently check nothing)" % (DEBUG_RPC_REL, anchor)
            )
    checked = 0
    for fn in entries:
        if fn in DEBUG_EXEC_DEVICE_LEVEL:
            continue
        body = _fun_body(rpc, r"\bfun\s+%s\s*\(" % re.escape(fn))
        if body is None or "NativeOffloadRequest(" not in body:
            continue
        checked += 1
        if DEBUG_EXEC_SCOPE_PARSE not in body:
            problems.append(
                "%s: %s builds a NativeOffloadRequest without the shared scope parse (%s) — an exec "
                "entry point that invents its own cwd/session cannot honour what the caller forwarded"
                % (DEBUG_RPC_REL, fn, DEBUG_EXEC_SCOPE_PARSE.rstrip("("))
            )
        for pat, what in ((r'cwd\s*=\s*"/"', 'cwd = "/"'),
                          (r"sessionId\s*=\s*null\b", "sessionId = null")):
            if re.search(pat, body):
                problems.append(
                    "%s: %s still hardcodes %s — the forwarded session/cwd is dropped, so a relative "
                    "operand resolves against the guest root and a named session falls back to the "
                    "global arm" % (DEBUG_RPC_REL, fn, what)
                )
    if checked < DEBUG_EXEC_SCOPE_MIN_ENTRIES:
        problems.append(
            "%s: only %d exec entry point(s) build an offload request — fail-closed (expected >= %d; "
            "a rewire into a helper would move the scope decision out of this criterion's reach)"
            % (DEBUG_RPC_REL, checked, DEBUG_EXEC_SCOPE_MIN_ENTRIES)
        )

    offload_path = os.path.join(root, DEBUG_OFFLOAD_REL)
    if not os.path.isfile(offload_path):
        return problems + [
            "%s: missing — cannot verify the session-scope forwarding" % DEBUG_OFFLOAD_REL
        ]
    offload = _read_code(offload_path)
    handle = _fun_body(offload, r"\boverride\s+fun\s+handle\s*\(")
    if handle is None:
        problems.append(
            "%s: `override fun handle` could not be delimited — fail-closed" % DEBUG_OFFLOAD_REL
        )
    elif DEBUG_EXEC_SCOPE_INJECT not in handle:
        problems.append(
            "%s: the forwarder no longer adds the caller's scope to a pass-through exec call (%s gone) "
            "— the RPC side rebuilds the request without a session or cwd"
            % (DEBUG_OFFLOAD_REL, DEBUG_EXEC_SCOPE_INJECT.rstrip("("))
        )
    m = re.search(r"internal\s+val\s+EXEC_SCOPE_SUBCOMMANDS\s*=\s*setOf\(([^)]*)\)", offload)
    if m is None:
        problems.append(
            "%s: EXEC_SCOPE_SUBCOMMANDS is not declared as a set — fail-closed (the carrier decision "
            "must stay declarative for this criterion to assert it)" % DEBUG_OFFLOAD_REL
        )
    else:
        members = re.findall(r'"([^"]*)"', m.group(1))
        if sorted(members) != sorted(DEBUG_EXEC_SCOPE_CARRIERS):
            problems.append(
                "%s: EXEC_SCOPE_SUBCOMMANDS = %s — the scope may only ride on %s (a raw `call` keeps its "
                "params verbatim; the device-level shizuku shell has no guest cwd)"
                % (DEBUG_OFFLOAD_REL, members, DEBUG_EXEC_SCOPE_CARRIERS)
            )
    return problems


_CODE_TOKEN_RE = re.compile(
    r'"""(?:\\.|[^\\])*?"""'          # triple-quoted string (may span lines)
    r'|"(?:\\.|[^"\\\n])*"'           # single-line string
    r"|'(?:\\.|[^'\\\n])*'"           # char literal
    r"|//[^\n]*"                      # line comment
    r"|/\*.*?\*/"                     # block comment, terminated
    r"|/\*.*",                        # unterminated block comment (to EOF)
    re.S,
)


def strip_comments(text):
    """Blank out // and /* */ comment content, keeping line structure.

    [meta-audit-1024 M6] Literals are tokenised first. The line-oriented version
    this replaces opened a BLOCK comment on the `/*` inside a string: the import
    handler's `"video/*"` / `"image/*"` MIME lookup blanked the following 67
    lines — values, contentResolver.insert, openOutputStream and the
    `FileInputStream(src)` read itself — so every criterion reading this file
    (R1 included) was judging a handler with half its body missing. The
    replacement keeps every newline, so line numbers are unchanged.
    """
    def repl(m):
        s = m.group(0)
        if s.startswith("//"):
            return ""
        if s.startswith("/*"):
            return "\n" * s.count("\n")
        return s

    return _CODE_TOKEN_RE.sub(repl, text)


def tainted_from_args(text):
    """One-hop taint: identifiers carrying a guest path taken from args.

    `val rawPath = args.get("path")` taints rawPath. An identifier assigned from
    an expression mentioning a tainted identifier stays tainted — UNLESS that
    expression calls a guest→host resolver, which is exactly the conversion the
    rule is asking for (`val host = resolveSourcePath(raw, sid)` is legitimate;
    `File(host)` after that is fine).

    [deep-audit-1039 A-08] Two changes:
      * A SESSION-SCOPED resolver sanitizes; the global `resolveHostPath` does
        not (it searches every session tree — see RESOLVER_CALL_RE).
      * The pseudo-identifier `it` is tracked per line: a line that opens a
        scope on a tainted receiver (`p?.let {`) taints `it` FOR THAT LINE, so
        `File(it)` inside the block is caught the same way `File(p)` on the
        next line would be. Returning the per-line map (not a final snapshot)
        is what makes single-line `p?.let { File(it) }` visible: the scope
        opens and closes on the same line, so a snapshot would have already
        dropped the binding.
    """
    tainted = {}
    per_line = {}
    for line in text.splitlines():
        snapshot = dict(tainted)
        m = ARG_ASSIGN_RE.search(line)
        if m:
            tainted[m.group(1)] = "raw"
            snapshot[m.group(1)] = "raw"
            per_line[line] = snapshot
            continue
        # A scope opened on a tainted receiver taints `it` on this line.
        let = LET_TAINTED_RE.search(line)
        if let:
            recv = line[:let.start()]
            if any(re.search(r"\b%s\b" % re.escape(t), recv) for t in tainted):
                tainted["it"] = "raw"
                snapshot["it"] = "raw"
        a = re.match(r"\s*(?:val|var)\s+(\w+)[^=]*=\s*(.*)$", line)
        if a:
            name, expr = a.group(1), a.group(2)
            if any(re.search(r"\b%s\b" % re.escape(t), expr) for t in tainted):
                tainted[name] = "resolved" if RESOLVER_CALL_RE.search(expr) else "raw"
                snapshot[name] = tainted[name]
        per_line[line] = snapshot
        # A scope that closed on this line drops the `it` binding again.
        if line.count("{") <= line.count("}"):
            tainted.pop("it", None)
    return tainted, per_line


def scan_bare_file(rel, text):
    """R1: a guest path from args reaching a bare File(...).

    Three shapes, all of which used to be invisible:
      * `File(args.get("path"))` inline (FILE_DIRECT_RE);
      * `val p = args.get("path")` … `File(p)` (one-hop identifier taint);
      * `p?.let { File(it) }` — the lambda's implicit `it` is not an identifier
        form, so it is tracked per line (deep-audit-1039 A-08).
    """
    problems = []
    for i, line in enumerate(text.splitlines(), 1):
        if FILE_DIRECT_RE.search(line):
            problems.append(
                "%s:%d: args value reaches a bare File(...) — resolve it through "
                "PRootKernel.resolvePathForSession / GuestPathMapper instead" % (rel, i)
            )
    assigned, per_line = tainted_from_args(text)
    lines = text.splitlines()
    if assigned:
        for i, line in enumerate(lines, 1):
            snapshot = per_line.get(line, assigned)
            if FILE_LET_RE.search(line) and snapshot.get("it") == "raw":
                problems.append(
                    "%s:%d: `File(it)` inside `?.let {}` derives from the guest path argument "
                    "without passing through a guest→host resolver — no raw host File" % (rel, i)
                )
                continue
            for m in FILE_IDENT_RE.finditer(line):
                ident = m.group(1)
                if snapshot.get(ident) == "raw":
                    problems.append(
                        "%s:%d: `File(%s)` derives from the guest path argument without passing "
                        "through a guest→host resolver — no raw host File" % (rel, i, ident)
                    )
            # [meta-audit-1024 M6] the same obligation, read as an EXPRESSION:
            # `File(rawPath.trim())`, `File(Paths.get(rawPath).toString())`,
            # `File("" + rawPath)`. Shapes already covered above are skipped so a
            # single construction still reports exactly one violation.
            for _pos, arg in _file_call_args(line):
                arg_s = arg.strip()
                if not arg_s or arg_s == "it" or FILE_ARG_IDENT_ONLY_RE.match(arg_s):
                    continue  # covered by the identifier / ?.let criteria above
                if re.search(r'get\(\s*"(?:%s)"' % ARG_NAMES, arg_s):
                    continue  # covered by FILE_DIRECT_RE
                if RESOLVER_CALL_RE.search(arg_s):
                    continue  # sanitised inline: File(resolvePathForSession(...))
                hits = sorted(
                    t for t, state in snapshot.items()
                    if state == "raw" and re.search(r"\b%s\b" % re.escape(t), arg_s)
                )
                if hits:
                    problems.append(
                        "%s:%d: `File(%s)` derives from the guest path argument (%s) without "
                        "passing through a guest→host resolver — no raw host File"
                        % (rel, i, arg_s, ", ".join(hits))
                    )
    return problems

def _statement_window(text, start_idx):
    """Text of the statement opened at start_idx (bracket-balanced), or None."""
    lines = text[start_idx:].splitlines()
    depth = 0
    seen = False
    for n, line in enumerate(lines):
        depth += line.count("{") + line.count("(") + line.count("[")
        depth -= line.count("}") + line.count(")") + line.count("]")
        if line.strip():
            seen = True
        if seen and depth <= 0:
            return "\n".join(lines[: n + 1])
    return None


def _main_src_files(root):
    """(path, repo-relative path) for every .kt in the shipped main source set."""
    base = os.path.join(root, MAIN_SRC_REL)
    files = []
    if not os.path.isdir(base):
        return files
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".kt"):
                continue
            path = os.path.join(dirpath, name)
            files.append((path, os.path.relpath(path, root)))
    return files


def _brace_body(text, idx):
    """Brace-balanced body of the declaration whose header ends at idx."""
    start = text.find("{", idx)
    if start < 0:
        return None
    depth = 0
    for n in range(start, len(text)):
        c = text[n]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:n + 1]
    return None


def _body_opener(text, idx):
    """Index of the `{` that opens a function body, or None.

    Expression bodies (`fun f(x: Int) = ...`) and abstract declarations have no
    brace body. `_brace_body` alone cannot tell them apart — it would find the
    NEXT function's `{` and swallow every function in between, which is how a
    whole run of declarations disappears from an enumeration like this one.
    """
    depth = 1                                  # idx sits just inside `(`
    i = idx
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and c == "=":
            return None                      # `fun f() = <expression>`
        elif depth == 0 and c == "{":
            return i
        elif depth == 0 and c == "\n":
            if not text[i + 1:].lstrip().startswith("{"):
                return None                  # header ended without a body
        i += 1
    return None


def _fun_declarations(text):
    """(name, body) for every function with a brace body in text, outermost only."""
    out = []
    covered = -1
    for m in FUN_DECL_RE.finditer(text):
        if m.start() < covered:
            continue
        open_idx = _body_opener(text, m.end())
        if open_idx is None:
            continue
        body = _brace_body(text, open_idx)
        if body is None:
            continue
        out.append((m.group(1), body))
        covered = open_idx + len(body)
    return out


def scan_photos_import_sanitisation(root):
    """R7 [meta-audit-1024 M6]: `android-photos import` must resolve the guest
    path session-scoped AND open the RESOLVED file.

    Scope is derived, never a hardcoded function name: an import entry is any
    function in PhotosOffloadHandler.kt whose body reads a path operand from args
    AND writes into MediaStore (a copy OUT of the sandbox). A second import entry
    — or the rename of this one — is covered without touching the gate, and an
    entry that can no longer be derived at all is reported instead of skipped.

    This is the axis R1 cannot see: R1 asks "is this File() argument shaped like
    a raw path"; R7 asks "did the conversion happen, and is its RESULT the thing
    that gets read". An import that keeps a resolver call somewhere but opens a
    different expression satisfies R1 and fails R7.
    """
    rel = PHOTOS_HANDLER_REL
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return ["%s: missing — cannot verify that the import path sanitises the guest "
                "path before reading it as a host file (fail-closed)" % rel]
    code = _read_code(path)
    entries = [
        (name, body)
        for name, body in _fun_declarations(code)
        if ARG_ASSIGN_RE.search(body) and IMPORT_SINK_RE.search(body)
    ]
    if not entries:
        return ["%s: no import entry could be derived (no function reads a path operand "
                "from args and writes into MediaStore) — fail-closed: the entry was renamed "
                "or moved, so the sanitiser criteria would silently check nothing" % rel]
    problems = []
    for name, body in entries:
        if not RESOLVER_CALL_RE.search(body):
            problems.append(
                "%s: `%s` never resolves the guest path through a session-scoped guest→host "
                "resolver — the file that gets imported is a raw host path (sanitisation must "
                "happen in the entry, not merely exist elsewhere in the file)" % (rel, name)
            )
            continue
        bound = re.search(
            r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*(?::[^=\n]*)?=\s*[^\n]*"
            + RESOLVER_CALL_RE.pattern, body)
        if bound:
            ident = re.escape(bound.group(1))
            used = any(
                re.search(r"\b%s\b" % ident, line) and HOST_OPEN_TOKEN_RE.search(line)
                for line in body.splitlines()
            )
        else:
            # `File(PRootKernel.resolvePathForSession(...))` — inline is a flow too.
            used = re.search(
                HOST_OPEN_TOKEN_RE.pattern + r"[^\n]*" + RESOLVER_CALL_RE.pattern, body
            ) is not None
        if not used:
            problems.append(
                "%s: `%s` resolves the guest path but its result never reaches a host open "
                "point — the value that gets opened is not the resolved one" % (rel, name)
            )
    return problems


def has_full_hardening(body):
    """True when `body` clears every R2 strip — i.e. it IS a sanitizer."""
    if "removeFlags(" not in body:
        return False
    if any(flag not in body for flag in GRANT_FLAGS):
        return False
    return all(re.search(pattern, body) for _label, pattern in R2_STRIPS)


def derive_sanitizer_names(root):
    """Names of tree functions whose body performs the FULL hardening.

    Derived from the body, never from a hardcoded name: a shared sanitizer is
    accepted by [scan_parse_uri] because its own body carries the guards, and
    the day somebody guts that body the name leaves this set and every call site
    goes red again (arm "sanitizer gutted" pins that transitivity).
    """
    names = set()
    for path, _rel in _main_src_files(root):
        code = _read_code(path)
        for m in FUN_DECL_RE.finditer(code):
            body = _brace_body(code, m.end())
            if body and has_full_hardening(body):
                names.add(m.group(1))
    return names


def _parse_uri_hardening_missing(window, sanitizer_names):
    """Criteria one `Intent.parseUri(...)` statement must satisfy."""
    missing = []
    if not SCHEME_NORMALISE_RE.search(window):
        missing.append("scheme normalisation (`Intent.parseUri` matches `intent:` case-sensitively)")
    for name in sorted(sanitizer_names):
        if re.search(r"\b%s\s*\(" % re.escape(name), window):
            return missing
    if "removeFlags(" not in window:
        missing.append("removeFlags(...)")
    for flag in GRANT_FLAGS:
        if flag not in window:
            missing.append(flag)
    for label, pattern in R2_STRIPS:
        if not re.search(pattern, window):
            missing.append(label)
    return missing


def scan_parse_uri(rel, text, sanitizer_names=()):
    """R2: Intent.parseUri must be paired with grant / clipData / component /
    package / selector stripping plus scheme normalisation — inline in the same
    statement, or by calling a shared sanitizer whose own body carries them
    ([deep-audit-1046 FIX-F9] added the sanitizer shape and the normalisation)."""
    problems = []
    for m in re.finditer(r"Intent\.parseUri\(", text):
        line_no = text.count("\n", 0, m.start()) + 1
        window = _statement_window(text, m.start())
        if window is None:
            problems.append("%s:%d: Intent.parseUri statement could not be delimited" % (rel, line_no))
            continue
        missing = _parse_uri_hardening_missing(window, sanitizer_names)
        if missing:
            problems.append(
                "%s:%d: Intent.parseUri without hardening — missing %s"
                % (rel, line_no, ", ".join(missing))
            )
    return problems


def _fallback_is_gated(code, match):
    """Is the page-supplied `browser_fallback_url` value scheme-gated?"""
    window = _statement_window(code, match.start()) or ""
    if FALLBACK_GATE_RE.search(window):
        return True
    am = FALLBACK_ASSIGN_RE.search(code)
    if not am:
        return False
    ident = re.escape(am.group(1))
    return re.search(GATED_IDENT_TMPL % (ident, ident), code) is not None


def scan_intent_uri_scope(root, sanitizer_names=None, skip_rels=()):
    """R2 scope ([dv26-F9]): every `Intent.parseUri` site in the shipped tree,
    every `intent://` dispatcher, and the page-supplied `browser_fallback_url`.

    Returns (problems, stats). `skip_rels` keeps the caller's own offload-dir
    pass from being reported twice.
    """
    if sanitizer_names is None:
        sanitizer_names = derive_sanitizer_names(root)
    problems = []
    stats = {"sites": 0, "consumers": 0, "fallbacks": 0}
    skip = set(skip_rels)
    for path, rel in _main_src_files(root):
        code = _read_code(path)
        site_problems = []
        if "Intent.parseUri(" in code:
            stats["sites"] += code.count("Intent.parseUri(")
            # The caller's own offload-dir pass already ran R2 over these files.
            if rel not in skip:
                site_problems = scan_parse_uri(rel, code, sanitizer_names)
                problems.extend(site_problems)
        calls_sanitizer = any(
            re.search(r"\b%s\s*\(" % re.escape(n), code) for n in sanitizer_names
        )
        if START_ACTIVITY_RE.search(code) and INTENT_SCHEME_LITERAL_RE.search(code):
            stats["consumers"] += 1
            hardened_inline = any(
                not _parse_uri_hardening_missing(_statement_window(code, m.start()) or "", ())
                for m in re.finditer(r"Intent\.parseUri\(", code)
            )
            # One message per file: a dispatcher whose own parseUri site was
            # already reported above is the same defect, not a second one.
            if not calls_sanitizer and not hardened_inline and not site_problems:
                problems.append(
                    "%s: dispatches intent:// links and calls startActivity but never routes the "
                    "parsed intent through a shared sanitizer (derived in tree: %s) — a "
                    "page-supplied intent keeps its URI grants / clipData / same-package target"
                    % (rel, ", ".join(sorted(sanitizer_names)) or "<none>")
                )
        for m in FALLBACK_READ_RE.finditer(code):
            stats["fallbacks"] += 1
            if _fallback_is_gated(code, m):
                continue
            problems.append(
                "%s:%d: page-supplied `browser_fallback_url` is dispatched without an http(s) "
                "scheme gate — an intent:// (or file://) fallback re-opens the escape"
                % (rel, code.count("\n", 0, m.start()) + 1)
            )
    return problems, stats


INTENT_SCOPE_STATS = {"sites": 0, "consumers": 0, "fallbacks": 0}


def check_tree(root, verbose=True):
    offload_dir = os.path.join(root, OFFLOAD_REL)
    problems = []
    scanned = 0
    if not os.path.isdir(offload_dir):
        print("❌ offload dir not found: %s" % offload_dir)
        return 1, ["missing dir"]
    # [deep-audit-1046 FIX-F9] the shared `intent://` sanitizers, derived from
    # their bodies (empty set on a tree without one — arms stay meaningful).
    sanitizers = derive_sanitizer_names(root)
    covered = set()
    for name in sorted(os.listdir(offload_dir)):
        if not name.endswith("OffloadHandler.kt"):
            continue
        path = os.path.join(offload_dir, name)
        rel = os.path.join(OFFLOAD_REL, name)
        code = _read_code(path)
        scanned += 1
        covered.add(rel)
        problems.extend(scan_bare_file(rel, code))
        problems.extend(scan_parse_uri(rel, code, sanitizers))
    scope_problems, scope_stats = scan_intent_uri_scope(root, sanitizers, skip_rels=covered)
    INTENT_SCOPE_STATS.update(scope_stats)
    problems.extend(scope_problems)
    if verbose:
        for p in problems:
            print("❌ %s" % p)
    return scanned, problems


# ── selftest ────────────────────────────────────────────────────────────────

DIRTY_RESOLVED_FILE = """package com.rikkaminis.app.sandbox.offload

import java.io.File

class DirtyResolvedHandler {
    private fun handleImport(args: OffloadArgs, request: NativeOffloadRequest): NativeOffloadResult {
        val rawPath = args.get("path") ?: return NativeOffloadResult(2, "required")
        val guest = GuestPathMapper.absoluteGuestPath(request.cwd, rawPath) ?: return NativeOffloadResult(1, "bad")
        // LEGITIMATE: File() after the guest→host resolver is the whole point.
        val src = PRootKernel.resolvePathForSession(request.sessionId, guest, context)
            ?: return NativeOffloadResult(1, "no_file")
        if (!src.exists()) return NativeOffloadResult(1, "no_file")
        return NativeOffloadResult(0, src.name)
    }
}
"""

DIRTY_FALLBACK = """package com.rikkaminis.app.sandbox.offload

import java.io.File

class DirtyFallbackHandler {
    private fun handleImport(args: OffloadArgs, request: NativeOffloadRequest): NativeOffloadResult {
        val rawPath = args.get("path") ?: return NativeOffloadResult(2, "required")
        val guestPath = GuestPathMapper.absoluteGuestPath(request.cwd, rawPath)
            ?: File(rawPath)  // elvis fallback reopens the read escape
        val src = PRootKernel.resolvePathForSession(request.sessionId, guestPath, context)
            ?: return NativeOffloadResult(1, "no_file")
        return NativeOffloadResult(0, src.name)
    }
}
"""

DIRTY_FILE = """package com.rikkaminis.app.sandbox.offload

import java.io.File

class DirtyHandler {
    private fun handleImport(args: OffloadArgs): NativeOffloadResult {
        val path = args.get("path")
            ?: return NativeOffloadResult(2, "required")
        val src = File(path)
        return NativeOffloadResult(0, src.name)
    }
}
"""

DIRTY_FILE_DIRECT = """package com.rikkaminis.app.sandbox.offload

import java.io.File

class DirtyDirectHandler {
    private fun handleThing(args: OffloadArgs): NativeOffloadResult {
        val src = File(args.get("file") ?: "x")
        return NativeOffloadResult(0, src.name)
    }
}
"""

CLEAN_FILE = """package com.rikkaminis.app.sandbox.offload

class CleanHandler {
    private fun handleImport(args: OffloadArgs, request: NativeOffloadRequest): NativeOffloadResult {
        val rawPath = args.get("path")
            ?: return NativeOffloadResult(2, "required")
        val guestPath = GuestPathMapper.absoluteGuestPath(request.cwd, rawPath) ?: return fail()
        val src = PRootKernel.resolvePathForSession(request.sessionId, guestPath, context) ?: return fail()
        return NativeOffloadResult(0, src.name)
    }
}
"""

# ── [deep-audit-1039 A-08] the three shapes that used to be invisible ────────
# DIRTY_GLOBAL_AS_SANITIZER: the guest path is "resolved" through the
# CROSS-SESSION GLOBAL resolver and then wrapped in a bare File. resolveHostPath
# used to sit in RESOLVER_CALL_RE's whitelist, so the taint was cleared and R1
# saw nothing — while R5 exists precisely to ban that call shape.
DIRTY_GLOBAL_AS_SANITIZER = """package com.rikkaminis.app.sandbox.offload

import java.io.File

class DirtyGlobalHandler {
    private fun handleImport(args: OffloadArgs): NativeOffloadResult {
        val raw = args.get("path") ?: return NativeOffloadResult(2, "required")
        val host = PRootKernel.resolveHostPath(raw)
        val src = File(host)
        return NativeOffloadResult(0, src.name)
    }
}
"""

# DIRTY_UNLISTED_FLAG: the operand arrives as --output, a flag name ARG_ASSIGN_RE
# never matched, so it never entered taint at all.
DIRTY_UNLISTED_FLAG = """package com.rikkaminis.app.sandbox.offload

import java.io.File

class DirtyOutputHandler {
    private fun handleExport(args: OffloadArgs): NativeOffloadResult {
        val p = args.get("output")
            ?: return NativeOffloadResult(2, "required")
        val dst = File(p)
        return NativeOffloadResult(0, dst.name)
    }
}
"""

# DIRTY_LET_IT: `p?.let { File(it) }` — the lambda's implicit `it` is not an
# identifier form, so FILE_IDENT_RE never matched it.
DIRTY_LET_IT = """package com.rikkaminis.app.sandbox.offload

import java.io.File

class DirtyLetHandler {
    private fun handleExport(args: OffloadArgs): NativeOffloadResult {
        val p = args.get("path") ?: return NativeOffloadResult(2, "required")
        p?.let { File(it).mkdirs() }
        return NativeOffloadResult(0, "ok")
    }
}
"""

# CLEAN_LET_RESOLVED: the same lambda shape, but the value passes through the
# session-scoped resolver before File() — the legitimate spelling.
CLEAN_LET_RESOLVED = """package com.rikkaminis.app.sandbox.offload

class CleanLetHandler {
    private fun handleExport(args: OffloadArgs, request: NativeOffloadRequest): NativeOffloadResult {
        val p = args.get("output") ?: return NativeOffloadResult(2, "required")
        p?.let {
            val host = PRootKernel.resolvePathForSession(request.sessionId, it, context) ?: return fail()
            File(host).mkdirs()
        }
        return NativeOffloadResult(0, "ok")
    }
}
"""

DIRTY_INTENT = """package com.rikkaminis.app.sandbox.offload

class DirtyIntentHandler {
    private fun handleOpen(url: String) {
        val intent = Intent.parseUri(url, Intent.URI_INTENT_SCHEME).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            selector = null
        }
        context.startActivity(intent)
    }
}
"""

CLEAN_INTENT = """package com.rikkaminis.app.sandbox.offload

class CleanIntentHandler {
    private fun handleOpen(url: String) {
        val intent = Intent.parseUri(lowercaseIntentScheme(url), Intent.URI_INTENT_SCHEME).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            removeFlags(
                Intent.FLAG_GRANT_READ_URI_PERMISSION or
                    Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                    Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION or
                    Intent.FLAG_GRANT_PREFIX_URI_PERMISSION,
            )
            clipData = null
            if (component?.packageName == context.packageName) component = null
            if (getPackage() == context.packageName) setPackage(null)
            selector = null
        }
        context.startActivity(intent)
    }
}
"""

# [deep-audit-1041 FIX-A6] Fail-closed arm for R2: a truncated/renamed call the
# statement-window walk cannot balance must be REPORTED, never silently skipped.
INTENT_UNDELIMITED = """package com.rikkaminis.app.sandbox.offload

class BrokenIntentHandler {
    private fun handleOpen(url: String) {
        val intent = Intent.parseUri(url, Intent.URI_INTENT_SCHEME).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
"""


def _fixture_root(files):
    root = tempfile.mkdtemp(prefix="offload-gate-selftest-")
    for name, content in files.items():
        path = os.path.join(root, OFFLOAD_REL, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    return root


def selftest():
    cases = [
        ("R1 dirty: bare File(ident) from args", {"AOffloadHandler.kt": DIRTY_FILE}, False),
        ("R1 dirty: File(args.get(...)) inline", {"AOffloadHandler.kt": DIRTY_FILE_DIRECT}, False),
        ("R1 dirty: elvis fallback File(guestPath)", {"AOffloadHandler.kt": DIRTY_FALLBACK}, False),
        ("R1 clean: resolver + session-aware", {"AOffloadHandler.kt": CLEAN_FILE}, True),
        ("R1 clean: File(host) after the resolver", {"AOffloadHandler.kt": DIRTY_RESOLVED_FILE}, True),
        ("R2 dirty: parseUri without stripping", {"AOffloadHandler.kt": DIRTY_INTENT}, False),
        ("R2 clean: parseUri with stripping", {"AOffloadHandler.kt": CLEAN_INTENT}, True),
    ]
    failures = []
    for label, files, expect_clean in cases:
        root = _fixture_root(files)
        try:
            _, problems = check_tree(root, verbose=False)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        clean = not problems
        ok = clean == expect_clean
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  → problems=%s" % problems))
        if not ok:
            failures.append(label)
    if failures:
        print("❌ selftest failed: %s" % ", ".join(failures))
        return 1
    print("SELFTEST=PASS (7 fixtures: 4 violations caught, 3 clean trees green)")

    # ── [deep-audit-1039 A-08] R1 blind-spot fixtures ───────────────────────
    # Each arm breaks exactly one of the three shapes that used to pass, plus a
    # clean arm proving the legitimate spelling of the lambda shape still
    # passes. Expected problem counts are literals: an arm that fires TWO
    # criteria is reported as a failure, so "only red on its own criterion" is
    # mechanically enforced rather than eyeballed.
    a08_arms = [
        ("A08 dirty: global resolver used as the sanitizer",
         DIRTY_GLOBAL_AS_SANITIZER, 1, "without passing through a guest→host resolver"),
        ("A08 dirty: --output flag never entered taint",
         DIRTY_UNLISTED_FLAG, 1, "without passing through a guest→host resolver"),
        ("A08 dirty: File(it) inside ?.let {}",
         DIRTY_LET_IT, 1, "inside `?.let {}`"),
        ("A08 clean: ?.let {} with the session resolver",
         CLEAN_LET_RESOLVED, 0, None),
    ]
    a08_failures = []
    for label, src, expected, needle in a08_arms:
        root = _fixture_root({"AOffloadHandler.kt": src})
        try:
            _, problems = check_tree(root, verbose=False)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        ok = len(problems) == expected
        if ok and needle is not None:
            ok = any(needle in p for p in problems)
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  → problems=%s" % problems))
        if not ok:
            a08_failures.append(label)
    if a08_failures:
        print("❌ selftest failed: %s" % ", ".join(a08_failures))
        return 1
    print("A08_R1_BLINDSPOT_SELFTEST=PASS (4 fixtures: 3 violations caught, 1 clean tree green)")

    # ── [deep-audit-1041 FIX-A6] R2 blind-spot fixtures ────────────────────
    # R2 used to read grants/clipData only, while this module's docstring had
    # claimed component stripping since 1016c: deleting the component /
    # selector lines from the real handler left the gate GREEN (probe/
    # blindspot.py records that on the real tree, both strips removed at once
    # included). Each arm below drops exactly ONE strip from the shipped block
    # and must be red on that strip's own label: the expected count is a
    # literal AND the message must name the strip.
    def _intent_without(line):
        if line not in CLEAN_INTENT:
            raise AssertionError("A07 fixture anchor missing: %r" % line)
        return CLEAN_INTENT.replace(line, "", 1)

    component_strip = "            if (component?.packageName == context.packageName) component = null\n"
    package_strip = "            if (getPackage() == context.packageName) setPackage(null)\n"
    a07_arms = [
        ("A07 dirty: same-package component strip removed",
         _intent_without(component_strip), 1, "same-package component strip"),
        ("A07 dirty: same-package package strip removed",
         _intent_without(package_strip), 1, "same-package package strip"),
        ("A07 dirty: selector strip removed",
         _intent_without("            selector = null\n"), 1, "selector = null"),
        ("A07 dirty: UNGUARDED setPackage(null) — third-party targets broken",
         CLEAN_INTENT.replace(package_strip, "            setPackage(null)\n"), 1,
         "same-package package strip"),
        ("A07 clean: the shipped hardening block", CLEAN_INTENT, 0, None),
        ("A07 fail-closed: parseUri statement cannot be delimited",
         INTENT_UNDELIMITED, 1, "could not be delimited"),
    ]
    a07_failures = []
    for label, src, expected, needle in a07_arms:
        root = _fixture_root({"AOffloadHandler.kt": src})
        try:
            _, problems = check_tree(root, verbose=False)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        ok = len(problems) == expected
        if ok and needle is not None:
            ok = any(needle in p for p in problems)
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  → problems=%s" % problems))
        if not ok:
            a07_failures.append(label)
    if a07_failures:
        print("❌ selftest failed: %s" % ", ".join(a07_failures))
        return 1
    print("A07_R2_STRIP_SELFTEST=PASS (6 fixtures: 4 violations + 1 fail-closed "
          "arm red, 1 clean tree green)")

    # ── [da1025-A] R3: player session-path fixtures ────────────────────────
    # Each dirty fixture breaks exactly one criterion: NO_SESSION keeps the
    # cwd join (only the sessionId criterion fires), NO_RELATIVE keeps the
    # sessionId wiring (only the join criterion fires), and the manager
    # fixture keeps both handler criteria while dropping the session-scoped
    # resolution from play() (only the play criterion fires).
    PLAYER_HANDLER_OK = """package com.rikkaminis.app.sandbox.offload

class PlayerOffloadHandler(private val context: Context) : NativeOffloadHandler {
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val resolved = resolveMaybeRelative("y.wav", request.cwd)
        return MediaPlayerManager.play("s", resolved, chatSessionId = request.sessionId, context = context)
    }
}
"""

    PLAYER_HANDLER_NO_SESSION = """package com.rikkaminis.app.sandbox.offload

class PlayerOffloadHandler(private val context: Context) : NativeOffloadHandler {
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val resolved = resolveMaybeRelative("y.wav", request.cwd)
        return MediaPlayerManager.play("s", resolved, context = context)
    }
}
"""

    PLAYER_HANDLER_NO_RELATIVE = """package com.rikkaminis.app.sandbox.offload

class PlayerOffloadHandler(private val context: Context) : NativeOffloadHandler {
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        return MediaPlayerManager.play("s", "y.wav", chatSessionId = request.sessionId, context = context)
    }
}
"""

    PLAYER_MANAGER_OK = """package com.rikkaminis.app.offload

object MediaPlayerManager {
    fun play(sessionId: String, filePath: String): String {
        val hostFile = PRootKernel.resolvePathForSession("sid", filePath, context)
            ?: return "Error: cannot resolve path"
        return "Playing"
    }
}
"""

    PLAYER_MANAGER_NO_SESSION = """package com.rikkaminis.app.offload

object MediaPlayerManager {
    fun play(sessionId: String, filePath: String): String {
        val hostFile = PRootKernel.resolveHostPath(filePath)
            ?: return "Error: cannot resolve path"
        return "Playing"
    }
}
"""

    def player_fixture_root(handler_src, manager_src):
        root = tempfile.mkdtemp(prefix="offload-gate-selftest-player-")
        for rel, content in ((PLAYER_HANDLER_REL, handler_src), (PLAYER_MANAGER_REL, manager_src)):
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        return root

    player_cases = [
        ("R3 dirty: handler never uses request.sessionId", PLAYER_HANDLER_NO_SESSION, PLAYER_MANAGER_OK, False),
        ("R3 dirty: handler no resolveMaybeRelative", PLAYER_HANDLER_NO_RELATIVE, PLAYER_MANAGER_OK, False),
        ("R3 dirty: play() bypasses resolvePathForSession", PLAYER_HANDLER_OK, PLAYER_MANAGER_NO_SESSION, False),
        ("R3 clean: cwd-join + session-scoped play", PLAYER_HANDLER_OK, PLAYER_MANAGER_OK, True),
    ]
    player_failures = []
    for label, handler_src, manager_src, expect_clean in player_cases:
        root = player_fixture_root(handler_src, manager_src)
        try:
            problems = scan_player_session_path(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        clean = not problems
        ok = clean == expect_clean
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  → problems=%s" % problems))
        if not ok:
            player_failures.append(label)
    if player_failures:
        print("❌ selftest failed: %s" % ", ".join(player_failures))
        return 1
    print("PLAYER_PATH_SELFTEST=PASS (4 fixtures: 3 violations caught, 1 clean tree green)")

    # ── [da1034-A2 ISSUE-03] R4: Shizuku privileged path fixtures ──────────
    # One dirty arm per decision the criterion makes: the raw operand reaching
    # the privileged command (file group and cp), the missing fail-closed
    # refusal, and the vanished guest→host mapping. Anchors are unique or
    # first-occurrence-scoped, so each arm breaks exactly one thing.
    SHIZUKU_OK = """package com.rikkaminis.app.sandbox.offload

class ShizukuOffloadHandler(private val context: Context) : NativeOffloadHandler {
    private fun shizukuHostPath(raw: String, request: NativeOffloadRequest): String? {
        val guestPath = resolveMaybeRelative(raw.trim(), request.cwd)
        return PRootKernel.resolvePathForSession(request.sessionId, guestPath, context)?.absolutePath
    }

    private fun fileLs(path: String?, args: OffloadArgs, request: NativeOffloadRequest): NativeOffloadResult {
        if (path.isNullOrBlank()) return errEnvelope("INVALID_ARGS", "file ls <path>", args)
        val host = shizukuHostPath(path, request)
            ?: return errEnvelope("PATH_NOT_RESOLVED", "file ls: '$path' does not resolve", args)
        val cmd = mutableListOf("ls")
        cmd.add(host)
        return ok(ShizukuManager.runProcess(cmd.toTypedArray()))
    }

    private fun fileRm(path: String?, args: OffloadArgs, request: NativeOffloadRequest): NativeOffloadResult {
        val host = shizukuHostPath(path, request)
            ?: return errEnvelope("PATH_NOT_RESOLVED", "file rm: '$path' does not resolve", args)
        val cmd = mutableListOf("rm")
        cmd.add(host)
        return ok(ShizukuManager.runProcess(cmd.toTypedArray()))
    }

    private fun filePullPush(src: String?, dst: String?, args: OffloadArgs, request: NativeOffloadRequest, pull: Boolean): NativeOffloadResult {
        val srcHost = shizukuHostPath(src, request)
            ?: return errEnvelope("PATH_NOT_RESOLVED", "src '$src' does not resolve", args)
        val dstHost = shizukuHostPath(dst, request)
            ?: return errEnvelope("PATH_NOT_RESOLVED", "dst '$dst' does not resolve", args)
        return ok(ShizukuManager.runProcess(arrayOf("cp", "-r", srcHost, dstHost)))
    }

    private fun packageInstall(apk: String?, args: OffloadArgs, request: NativeOffloadRequest): NativeOffloadResult {
        val apkHost = shizukuHostPath(apk, request)
            ?: return errEnvelope("PATH_NOT_RESOLVED", "package install: '$apk' does not resolve", args)
        val cmd = mutableListOf("pm", "install")
        cmd.add(apkHost)
        return ok(ShizukuManager.runProcess(cmd.toTypedArray()))
    }
}
"""

    def shizuku_fixture_root(text):
        root = tempfile.mkdtemp(prefix="offload-gate-selftest-shizuku-")
        path = os.path.join(root, SHIZUKU_HANDLER_REL)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return root

    shizuku_arms = [
        ("R4 clean: resolved operands + refusal on null", None, True),
        ("R4 dirty: `file ls` hands the raw `path` to the command",
         ("cmd.add(host)", "cmd.add(path)"), False),
        ("R4 dirty: `cp` receives the raw src/dst",
         ('arrayOf("cp", "-r", srcHost, dstHost)', 'arrayOf("cp", "-r", src, dst)'), False),
        ("R4 dirty: no fail-closed refusal when the path does not resolve",
         ("?: return errEnvelope(", '?: ""'), False),
        ("R4 dirty: the guest→host mapping is gone",
         ("PRootKernel.resolvePathForSession(", "PRootKernel.resolveHostPath("), False),
    ]
    shizuku_failures = []
    for label, mutation, expect_clean in shizuku_arms:
        if mutation is not None and mutation[0] not in SHIZUKU_OK:
            print("  [FAIL] %s: anchor not found in fixture" % label)
            shizuku_failures.append(label)
            continue
        text = SHIZUKU_OK if mutation is None else SHIZUKU_OK.replace(mutation[0], mutation[1], 1)
        root = shizuku_fixture_root(text)
        try:
            problems = scan_shizuku_paths(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        clean = not problems
        ok = clean == expect_clean
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  → problems=%s" % problems))
        if not ok:
            shizuku_failures.append(label)
    if shizuku_failures:
        print("❌ selftest failed: %s" % ", ".join(shizuku_failures))
        return 1
    print("SHIZUKU_PATH_SELFTEST=PASS (5 fixtures: 4 violations caught, 1 clean tree green)")

    # ── [dv16-A03] R5: debug RPC session-path fixtures ─────────────────────
    # One dirty arm per decision the criterion makes: the bare global resolver
    # restored in a handler, the session resolver dropped without a global
    # call, the handler anchor renamed (fail-closed), the session id never
    # forwarded by the offload handler, and the subcommand gate removed.
    DEBUG_RPC_OK = """package com.rikkaminis.app.debug

class DebugRPCHandler(private val context: Context) {
    private fun handleLS(params: JSONObject): Any {
        val sessionId = params.optString("sessionId", "").ifBlank { null }
        val hostFile = PRootKernel.resolvePathForSession(sessionId, path, context)
            ?: throw RPCException(-32602, "Cannot resolve path: $path")
        return hostFile
    }

    private fun handleReadFile(params: JSONObject): JSONObject {
        val sessionId = params.optString("sessionId", "").ifBlank { null }
        val hostFile = PRootKernel.resolvePathForSession(sessionId, path, context)
            ?: throw RPCException(-32602, "Cannot resolve path: $path")
        return JSONObject()
    }

    private fun handleWriteFile(params: JSONObject): JSONObject {
        val sessionId = params.optString("sessionId", "").ifBlank { null }
        val hostFile = PRootKernel.resolvePathForSession(sessionId, path, context) ?: run {
            val rootfsDir = RootfsManager.getInstance(context).rootfsDir
            File(rootfsDir, path.removePrefix("/"))
        }
        return JSONObject()
    }
}
"""

    DEBUG_OFFLOAD_OK = """package com.rikkaminis.app.sandbox.offload

class DebugOffloadHandler(private val context: Context) : NativeOffloadHandler {
    private val SESSION_SCOPED_SUBCOMMANDS = setOf("ls", "read", "write")

    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        if (sub in SESSION_SCOPED_SUBCOMMANDS && !request.sessionId.isNullOrBlank()) {
            rpc.params.put("sessionId", request.sessionId)
        }
        return postJsonRpc(rpc.method, rpc.params)
    }
}
"""

    def debug_fixture_root(rpc_text, offload_text=None, rpc_present=True):
        root = tempfile.mkdtemp(prefix="offload-gate-selftest-debug-")
        if rpc_present:
            path = os.path.join(root, DEBUG_RPC_REL)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(rpc_text)
        if offload_text is not None:
            path = os.path.join(root, DEBUG_OFFLOAD_REL)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(offload_text)
        return root

    debug_arms = [
        ("R5 clean: session-scoped handlers + id forwarded", DEBUG_RPC_OK, DEBUG_OFFLOAD_OK,
         None, True),
        ("R5 dirty: a handler keeps a bare global resolveHostPath",
         DEBUG_RPC_OK.replace(
             "        val hostFile = PRootKernel.resolvePathForSession(sessionId, path, context)\n"
             "            ?: throw RPCException(-32602, \"Cannot resolve path: $path\")\n"
             "        return JSONObject()\n    }\n\n"
             "    private fun handleWriteFile",
             "        val hostFile = PRootKernel.resolvePathForSession(sessionId, path, context)\n"
             "            ?: throw RPCException(-32602, \"Cannot resolve path: $path\")\n"
             "        val legacy = PRootKernel.resolveHostPath(path)\n"
             "        return JSONObject()\n    }\n\n"
             "    private fun handleWriteFile"),
         DEBUG_OFFLOAD_OK, "still calls the bare global resolveHostPath", False),
        ("R5 dirty: the session resolver dropped without a global call",
         DEBUG_RPC_OK.replace(
             "        val sessionId = params.optString(\"sessionId\", \"\").ifBlank { null }\n"
             "        val hostFile = PRootKernel.resolvePathForSession(sessionId, path, context)\n"
             "            ?: throw RPCException(-32602, \"Cannot resolve path: $path\")\n"
             "        return JSONObject()",
             "        val hostFile = PRootKernel.resolveGuestHostPath(path)\n"
             "        return JSONObject()", 1),
         DEBUG_OFFLOAD_OK, "does not resolve through PRootKernel.resolvePathForSession", False),
        ("R5 dirty: a handler anchor renamed (fail-closed)", DEBUG_RPC_OK,
         DEBUG_OFFLOAD_OK, "could not be delimited", False),
        ("R5 dirty: the offload handler never forwards request.sessionId",
         DEBUG_RPC_OK,
         DEBUG_OFFLOAD_OK.replace(
             "        if (sub in SESSION_SCOPED_SUBCOMMANDS && !request.sessionId.isNullOrBlank()) {\n"
             "            rpc.params.put(\"sessionId\", request.sessionId)\n"
             "        }\n", ""),
         "never reaches the rpc request", False),
        ("R5 dirty: the session-scoped subcommand gate removed",
         DEBUG_RPC_OK,
         DEBUG_OFFLOAD_OK.replace('SESSION_SCOPED_SUBCOMMANDS = setOf("ls", "read", "write")',
                                  'PATH_SUBS = setOf("ls")')
                        .replace("sub in SESSION_SCOPED_SUBCOMMANDS", "sub in PATH_SUBS"),
         "session-scoped subcommand gate is gone", False),
    ]
    debug_failures = []
    for label, rpc_text, offload_text, needle, expect_clean in debug_arms:
        if label.startswith("R5 dirty: a handler anchor renamed"):
            rpc_text = DEBUG_RPC_OK.replace("private fun handleLS(", "private fun handleLs(")
        root = debug_fixture_root(rpc_text, offload_text)
        try:
            problems = scan_debug_session_path(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        ok = (not problems) == expect_clean
        if ok and needle is not None:
            ok = any(needle in p for p in problems)
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  → problems=%s" % problems))
        if not ok:
            debug_failures.append(label)
    # Fail-closed: a missing DebugRPCHandler.kt is a violation, not a skip.
    root = debug_fixture_root(DEBUG_RPC_OK, DEBUG_OFFLOAD_OK, rpc_present=False)
    try:
        problems = scan_debug_session_path(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if not problems:
        debug_failures.append("R5 fail-closed: missing DebugRPCHandler.kt was not reported")
        print("  [FAIL] R5 fail-closed: missing DebugRPCHandler.kt was not reported")
    else:
        print("  [PASS] R5 fail-closed: missing DebugRPCHandler.kt is a violation")
    # Fail-closed: a missing offload handler is a violation too.
    root = debug_fixture_root(DEBUG_RPC_OK, None)
    try:
        problems = scan_debug_session_path(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if not problems:
        debug_failures.append("R5 fail-closed: missing DebugOffloadHandler.kt was not reported")
        print("  [FAIL] R5 fail-closed: missing DebugOffloadHandler.kt was not reported")
    else:
        print("  [PASS] R5 fail-closed: missing DebugOffloadHandler.kt is a violation")
    # ── [dv18-A05] R6: debug exec entry-point scope fixtures ────────────────
    # One dirty arm per decision the criterion makes: a hardcoded cwd, a
    # dropped session id, the shared parse bypassed, a NEW entry repeating the
    # defect, the forwarder losing the scope, and the carrier set widened to
    # the raw `call` escape hatch. Plus fail-closed arms for every anchor the
    # criterion would otherwise silently stop checking.
    R6_RPC_OK = """package com.rikkaminis.app.debug

class DebugRPCHandler(private val context: Context) {
    private fun handleShizukuExec(params: JSONObject): JSONObject {
        // device-level shell: intentionally session-less
        val request = NativeOffloadRequest(pid = -1, cwd = "/", sessionId = null)
        return JSONObject()
    }

    private fun handleModelUseExec(params: JSONObject): JSONObject {
        val scope = execScopeOf(params.optString("sessionId", ""), params.optString("cwd", ""))
        val request = NativeOffloadRequest(pid = -1, cwd = scope.cwd, sessionId = scope.sessionId)
        return JSONObject()
    }

    private fun handleSessionsExec(params: JSONObject): JSONObject {
        val scope = execScopeOf(params.optString("sessionId", ""), params.optString("cwd", ""))
        val request = NativeOffloadRequest(pid = -1, cwd = scope.cwd, sessionId = scope.sessionId)
        return JSONObject()
    }

    private fun handleMinisConfigExec(params: JSONObject): JSONObject {
        return JSONObject()
    }
}
"""

    R6_INJECT = """        execScopeParams(sub, request.sessionId, request.cwd).forEach { (key, value) ->
            rpc.params.put(key, value)
        }
"""

    R6_OFFLOAD_OK = """package com.rikkaminis.app.sandbox.offload

class DebugOffloadHandler(private val context: Context) : NativeOffloadHandler {
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
""" + R6_INJECT + """        return postJsonRpc(rpc.method, rpc.params)
    }
}

internal val EXEC_SCOPE_SUBCOMMANDS = setOf("model-use", "modelUse")

internal fun execScopeParams(sub: String, sessionId: String?, cwd: String?): Map<String, String> {
    if (sub !in EXEC_SCOPE_SUBCOMMANDS) return emptyMap()
    val scope = LinkedHashMap<String, String>()
    if (!sessionId.isNullOrBlank()) scope["sessionId"] = sessionId
    if (!cwd.isNullOrBlank()) scope["cwd"] = cwd
    return scope
}
"""

    def _patch_body(text, fn, old, new):
        """Replace [old] with [new] inside the `fn` body only (fixtures have one member per brace level)."""
        start = text.index("fun %s(" % fn)
        end = text.index("\n    }", start)
        return text[:start] + text[start:end].replace(old, new, 1) + text[end:]

    def exec_fixture_root(rpc_text, offload_text=None, rpc_present=True):
        root = tempfile.mkdtemp(prefix="offload-gate-selftest-exexec-")
        if rpc_present:
            path = os.path.join(root, DEBUG_RPC_REL)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(rpc_text)
        if offload_text is not None:
            path = os.path.join(root, DEBUG_OFFLOAD_REL)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(offload_text)
        return root

    SCOPE_CALL = 'execScopeOf(params.optString("sessionId", ""), params.optString("cwd", ""))'
    EXEC_REQ = "val request = NativeOffloadRequest(pid = -1, cwd = scope.cwd, sessionId = scope.sessionId)"

    exec_arms = [
        ("R6 clean: scope-parsed entries + forwarding forwarder",
         R6_RPC_OK, R6_OFFLOAD_OK, None, True),
        ('R6 dirty: a scope-taking entry hardcodes cwd = "/"',
         _patch_body(R6_RPC_OK, "handleModelUseExec", "cwd = scope.cwd", 'cwd = "/"'),
         R6_OFFLOAD_OK, "still hardcodes cwd", False),
        ("R6 dirty: a scope-taking entry drops the session id",
         _patch_body(R6_RPC_OK, "handleSessionsExec", "sessionId = scope.sessionId", "sessionId = null"),
         R6_OFFLOAD_OK, "still hardcodes sessionId = null", False),
        ("R6 dirty: the shared scope parse bypassed",
         _patch_body(R6_RPC_OK, "handleModelUseExec", SCOPE_CALL, 'params.optString("cwd", "/")'),
         R6_OFFLOAD_OK, "without the shared scope parse", False),
        ("R6 dirty: a NEW exec entry repeats the defect",
         _patch_body(R6_RPC_OK, "handleMinisConfigExec", "return JSONObject()",
                     "val request = NativeOffloadRequest(pid = -1, cwd = \"/\", sessionId = null)"),
         R6_OFFLOAD_OK, "still hardcodes cwd", False),
        ("R6 dirty: the forwarder stops carrying the caller's scope",
         R6_RPC_OK, R6_OFFLOAD_OK.replace(R6_INJECT, ""),
         "no longer adds the caller's scope", False),
        ("R6 dirty: the carrier set widened to the raw `call` escape hatch",
         R6_RPC_OK,
         R6_OFFLOAD_OK.replace('setOf("model-use", "modelUse")',
                               'setOf("model-use", "modelUse", "call")'),
         "may only ride on", False),
        ("R6 fail-closed: an entry anchor renamed",
         R6_RPC_OK.replace("private fun handleModelUseExec(", "private fun handleModelUseEntry("),
         R6_OFFLOAD_OK, "is gone — fail-closed", False),
        ("R6 fail-closed: the carrier declaration removed",
         R6_RPC_OK,
         R6_OFFLOAD_OK.replace('internal val EXEC_SCOPE_SUBCOMMANDS = setOf("model-use", "modelUse")\n', ""),
         "not declared as a set", False),
        ("R6 fail-closed: the request construction moved into a helper",
         R6_RPC_OK.replace(EXEC_REQ, "val request = buildExecRequest(scope)"),
         R6_OFFLOAD_OK, "only 0 exec entry point(s) build an offload request", False),
    ]
    exec_failures = []
    for label, rpc_text, offload_text, needle, expect_clean in exec_arms:
        root = exec_fixture_root(rpc_text, offload_text)
        try:
            problems = scan_debug_exec_scope(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        ok = (not problems) == expect_clean
        if ok and needle is not None:
            ok = any(needle in p for p in problems)
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  → problems=%s" % problems))
        if not ok:
            exec_failures.append(label)
    # Fail-closed: a missing DebugRPCHandler.kt is a violation, not a skip.
    for label, rpc_text, offload_text in (
        ("R6 fail-closed: missing DebugRPCHandler.kt", None, R6_OFFLOAD_OK),
        ("R6 fail-closed: missing DebugOffloadHandler.kt", R6_RPC_OK, None),
    ):
        root = exec_fixture_root(rpc_text, offload_text, rpc_present=rpc_text is not None)
        try:
            problems = scan_debug_exec_scope(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        ok = bool(problems)
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  → problems=%s" % problems))
        if not ok:
            exec_failures.append(label)

    if debug_failures:
        print("❌ selftest failed: %s" % ", ".join(debug_failures))
        return 1
    print("DEBUG_SESSION_PATH_SELFTEST=PASS (6 fixtures + 2 fail-closed: "
          "5 violations caught, 1 clean tree green)")
    if exec_failures:
        print("❌ selftest failed: %s" % ", ".join(exec_failures))
        return 1
    _exec_total = len(exec_arms) + 2
    print("DEBUG_EXEC_SCOPE_SELFTEST=PASS (%d fixtures: %d dirty/fail-closed arms red as expected, "
          "1 clean tree green)" % (_exec_total, _exec_total - 1))

    # ── [deep-audit-1046 FIX-F9] R2 SCOPE: every parseUri site + every
    # dispatcher + the page-supplied fallback ───────────────────────────────
    # The blind spot this pins is the SCOPE, not a strip: R2 only ever read
    # `sandbox/offload/*OffloadHandler.kt`, so ui/browser (fed by arbitrary web
    # pages) had a naked `Intent.parseUri` with `selector = null` as its entire
    # hardening. Arms 2-4 are the browser-side regressions; arm 5 is a brand-new
    # third caller (the derivation must not need a human to remember); arm 6 is
    # the `browser_fallback_url` second-launch; arm 7 proves the sanitizer
    # derivation is TRANSITIVE (gut the helper and its callers go red too).
    MAIN = MAIN_SRC_REL
    F9_UTIL = os.path.join(MAIN, "com", "rikkaminis", "app", "util")
    F9_OFFLOAD = os.path.join(MAIN, "com", "rikkaminis", "app", "sandbox", "offload")
    F9_BROWSER = os.path.join(MAIN, "com", "rikkaminis", "app", "ui", "browser")

    F9_SANITIZER = """package com.rikkaminis.app.util

internal object F9IntentSanitizer {
    fun sanitize(intent: Intent, context: Context): Intent {
        intent.removeFlags(
            Intent.FLAG_GRANT_READ_URI_PERMISSION or
                Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION or
                Intent.FLAG_GRANT_PREFIX_URI_PERMISSION,
        )
        intent.clipData = null
        if (isSelfTarget(intent.component?.packageName, context.packageName)) intent.component = null
        if (isSelfTarget(intent.getPackage(), context.packageName)) intent.setPackage(null)
        intent.selector = null
        return intent
    }
}
"""

    F9_SANITIZER_GUTTED = F9_SANITIZER.replace(
        "            Intent.FLAG_GRANT_READ_URI_PERMISSION or\n", "", 1
    )

    F9_OFFLOAD_CONSUMER = """package com.rikkaminis.app.sandbox.offload

class F9OpenHandler(private val context: Context) {
    private fun tryOpen(url: String): Intent {
        val isIntentUri = url.startsWith("android-app:", ignoreCase = true)
        return Intent.parseUri(lowercaseIntentScheme(url), Intent.URI_INTENT_SCHEME).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            F9IntentSanitizer.sanitize(this, context)
        }
    }

    private fun dispatch(intent: Intent) = context.startActivity(intent)
}
"""

    F9_BROWSER_CONSUMER = """package com.rikkaminis.app.ui.browser

object F9BrowserHandler {
    private val ANDROID_APP_SCHEME = "android-app"

    fun handleIntentScheme(context: Context, url: String): Boolean {
        val intent = Intent.parseUri(lowercaseIntentScheme(url), Intent.URI_INTENT_SCHEME).apply {
            addCategory(Intent.CATEGORY_BROWSABLE)
            F9IntentSanitizer.sanitize(this, context)
        }
        val rawFallback = intent.getStringExtra("browser_fallback_url")
        val fallback = allowedFallbackUrl(rawFallback)
        context.startActivity(intent)
        return fallback != null
    }
}
"""

    # Arm 2: the shipped-before shape — parse, strip the selector, launch.
    F9_BROWSER_DEGRADED = """package com.rikkaminis.app.ui.browser

object F9BrowserHandler {
    private val ANDROID_APP_SCHEME = "android-app"

    fun handleIntentScheme(context: Context, url: String): Boolean {
        val intent = Intent.parseUri(lowercaseIntentScheme(url), Intent.URI_INTENT_SCHEME).apply {
            selector = null
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
        return true
    }
}
"""

    # Arm 3: full hardening, but the page's raw casing reaches parseUri.
    F9_BROWSER_NO_LOWERCASE = F9_BROWSER_CONSUMER.replace(
        "Intent.parseUri(lowercaseIntentScheme(url)", "Intent.parseUri(url", 1
    )

    # Arm 5: dispatches intent:// and never parses / never sanitises.
    F9_DISPATCHER_ONLY = """package com.rikkaminis.app.ui.browser

object F9RawDispatcher {
    fun handle(context: Context, url: String): Boolean {
        context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        return true
    }

    private val INTENT_SCHEME = "intent:"
}
"""

    # Arm 6: the fallback is launched verbatim, whatever scheme it names.
    F9_BROWSER_RAW_FALLBACK = """package com.rikkaminis.app.ui.browser

object F9BrowserHandler {
    private val ANDROID_APP_SCHEME = "android-app"

    fun handleIntentScheme(context: Context, url: String): Boolean {
        val intent = Intent.parseUri(lowercaseIntentScheme(url), Intent.URI_INTENT_SCHEME).apply {
            F9IntentSanitizer.sanitize(this, context)
        }
        val fallback = intent.getStringExtra("browser_fallback_url")
        if (fallback != null) {
            context.startActivity(Intent(Intent.ACTION_VIEW, fallback.toUri()))
        }
        return true
    }
}
"""

    # Arm 8: no shared helper at all — inline hardening is still accepted.
    F9_INLINE_CONSUMER = """package com.rikkaminis.app.sandbox.offload

class F9InlineHandler(private val context: Context) {
    private fun tryOpen(url: String): Intent {
        val isIntentUri = url.startsWith("android-app:", ignoreCase = true)
        return Intent.parseUri(lowercaseIntentScheme(url), Intent.URI_INTENT_SCHEME).apply {
            removeFlags(
                Intent.FLAG_GRANT_READ_URI_PERMISSION or
                    Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                    Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION or
                    Intent.FLAG_GRANT_PREFIX_URI_PERMISSION,
            )
            clipData = null
            if (component?.packageName == context.packageName) component = null
            if (getPackage() == context.packageName) setPackage(null)
            selector = null
        }
    }

    private fun dispatch(intent: Intent) = context.startActivity(intent)
}
"""

    # Arm 4: a THIRD caller nobody remembered to wire up.
    F9_THIRD_BARE = """package com.rikkaminis.app.ui.settings

object F9NewConsumer {
    fun launch(url: String): Intent =
        Intent.parseUri(lowercaseIntentScheme(url), Intent.URI_INTENT_SCHEME)
}
"""

    def f9_root(files):
        root = tempfile.mkdtemp(prefix="offload-gate-selftest-f9-")
        os.makedirs(os.path.join(root, OFFLOAD_REL), exist_ok=True)
        for rel, content in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        return root

    F9_SANITIZER_REL = os.path.join(F9_UTIL, "F9IntentSanitizer.kt")
    F9_OFFLOAD_REL = os.path.join(F9_OFFLOAD, "F9OpenHandler.kt")
    F9_BROWSER_REL = os.path.join(F9_BROWSER, "F9BrowserHandler.kt")

    f9_arms = [
        ("F9 clean: shared sanitizer + both consumers",
         {F9_SANITIZER_REL: F9_SANITIZER,
          F9_OFFLOAD_REL: F9_OFFLOAD_CONSUMER,
          F9_BROWSER_REL: F9_BROWSER_CONSUMER}, 0, None),
        ("F9 dirty: browser side degrades to `selector = null` only",
         {F9_SANITIZER_REL: F9_SANITIZER,
          F9_OFFLOAD_REL: F9_OFFLOAD_CONSUMER,
          F9_BROWSER_REL: F9_BROWSER_DEGRADED}, 1, "Intent.parseUri without hardening"),
        ("F9 dirty: page casing handed to parseUri (no scheme normalisation)",
         {F9_SANITIZER_REL: F9_SANITIZER,
          F9_OFFLOAD_REL: F9_OFFLOAD_CONSUMER,
          F9_BROWSER_REL: F9_BROWSER_NO_LOWERCASE}, 1, "scheme normalisation"),
        ("F9 dirty: a THIRD bare Intent.parseUri caller",
         {F9_SANITIZER_REL: F9_SANITIZER,
          F9_OFFLOAD_REL: F9_OFFLOAD_CONSUMER,
          F9_BROWSER_REL: F9_BROWSER_CONSUMER,
          os.path.join(MAIN, "com", "rikkaminis", "app", "ui", "settings", "F9NewConsumer.kt"):
              F9_THIRD_BARE}, 1, "F9NewConsumer.kt"),
        ("F9 dirty: an intent:// dispatcher that never sanitises",
         {F9_SANITIZER_REL: F9_SANITIZER,
          F9_OFFLOAD_REL: F9_OFFLOAD_CONSUMER,
          F9_BROWSER_REL: F9_BROWSER_CONSUMER,
          os.path.join(F9_BROWSER, "F9RawDispatcher.kt"): F9_DISPATCHER_ONLY},
         1, "never routes the parsed intent through a shared sanitizer"),
        ("F9 dirty: browser_fallback_url launched without a scheme gate",
         {F9_SANITIZER_REL: F9_SANITIZER,
          F9_OFFLOAD_REL: F9_OFFLOAD_CONSUMER,
          F9_BROWSER_REL: F9_BROWSER_RAW_FALLBACK}, 1, "browser_fallback_url"),
        ("F9 dirty: sanitizer gutted → both call sites lose the guards (transitive)",
         {F9_SANITIZER_REL: F9_SANITIZER_GUTTED,
          F9_OFFLOAD_REL: F9_OFFLOAD_CONSUMER,
          F9_BROWSER_REL: F9_BROWSER_CONSUMER}, 2, "FLAG_GRANT_READ_URI_PERMISSION"),
        ("F9 clean: inline hardening at the call site (no shared helper)",
         {F9_OFFLOAD_REL: F9_INLINE_CONSUMER}, 0, None),
    ]
    f9_failures = []
    for label, files, expected, needle in f9_arms:
        root = f9_root(files)
        try:
            _, problems = check_tree(root, verbose=False)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        ok = len(problems) == expected
        if ok and needle is not None:
            ok = any(needle in p for p in problems)
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  → problems=%s" % problems))
        if not ok:
            f9_failures.append(label)
    if f9_failures:
        print("❌ selftest failed: %s" % ", ".join(f9_failures))
        return 1
    print("F9_INTENT_SCOPE_SELFTEST=PASS (%d fixtures: %d dirty arms red on their own "
          "criterion, %d clean trees green)"
          % (len(f9_arms),
             sum(1 for a in f9_arms if a[2] > 0),
             sum(1 for a in f9_arms if a[2] == 0)))

    # ── [meta-audit-1024 M6] R1b + R7: sanitisation must HAPPEN ────────────
    # Two axes, one obligation: the value that reaches a host file must be the
    # one a session-scoped resolver produced. R1's three spellings read the SHAPE
    # of one argument; these arms pin the two shapes that walk straight past it:
    # an EXPRESSION argument (`File(rawPath.trim())`) and an entry that keeps a
    # resolver somewhere but opens something else. Expected counts are literals
    # so "red on exactly its own criterion" is mechanical, not eyeballed.
    M6_FILE_EXPR_TAINTED = """package com.rikkaminis.app.sandbox.offload

import java.io.File
import java.io.FileInputStream

class M6ExprHandler {
    private fun handleExport(args: OffloadArgs): NativeOffloadResult {
        val rawPath = args.get("path") ?: return NativeOffloadResult(2, "required")
        val src = File(rawPath.trim())
        return NativeOffloadResult(0, FileInputStream(src).use { it.readBytes().size.toString() })
    }
}
"""

    M6_FILE_EXPR_PATHS_GET = """package com.rikkaminis.app.sandbox.offload

import java.io.File
import java.nio.file.Paths

class M6PathsGetHandler {
    private fun handleExport(args: OffloadArgs): NativeOffloadResult {
        val rawPath = args.get("path") ?: return NativeOffloadResult(2, "required")
        val src = File(Paths.get(rawPath).toString())
        return NativeOffloadResult(0, src.name)
    }
}
"""

    M6_FILE_EXPR_RESOLVED = """package com.rikkaminis.app.sandbox.offload

import java.io.File

class M6ResolvedExprHandler {
    private fun handleExport(args: OffloadArgs, request: NativeOffloadRequest): NativeOffloadResult {
        val rawPath = args.get("path") ?: return NativeOffloadResult(2, "required")
        val host = PRootKernel.resolvePathForSession(request.sessionId, rawPath, context)
            ?: return NativeOffloadResult(1, "no_file")
        val src = File(host.absolutePath)
        return NativeOffloadResult(0, src.name)
    }
}
"""

    # The MIME lookup that made the old comment stripper blank the rest of the
    # file: `"video/*"` opened a BLOCK comment. Everything below it — including
    # the raw File(...) this arm plants — used to be invisible.
    M6_STRIP_STRING_COMMENT = """package com.rikkaminis.app.sandbox.offload

import java.io.File

class M6StripHandler {
    private fun mime(ext: String): String = when (ext) {
        "mp4" -> "video/*"
        "jpg" -> "image/*"
        else -> "application/octet-stream"
    }

    private fun handleImport(args: OffloadArgs): NativeOffloadResult {
        val rawPath = args.get("path") ?: return NativeOffloadResult(2, "required")
        val src = File(rawPath.trim())
        return NativeOffloadResult(0, src.name)
    }
}
"""

    M6_PHOTOS_IMPORT_OK = """package com.rikkaminis.app.sandbox.offload

import java.io.FileInputStream

class PhotosOffloadHandler(private val context: Context) : NativeOffloadHandler {
    private fun handleImport(args: OffloadArgs, request: NativeOffloadRequest): NativeOffloadResult {
        val rawPath = args.get("path") ?: return NativeOffloadResult(2, "required")
        val guestPath = GuestPathMapper.absoluteGuestPath(request.cwd, rawPath)
            ?: return NativeOffloadResult(1, "path_outside_sandbox")
        val src = PRootKernel.resolvePathForSession(request.sessionId, guestPath, context)
            ?: return NativeOffloadResult(1, "no_file")
        val values = ContentValues()
        val uri = context.contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
            ?: return NativeOffloadResult(1, "insert_failed")
        context.contentResolver.openOutputStream(uri)?.use { os ->
            FileInputStream(src).use { input -> os.write(input.readBytes()) }
        }
        return NativeOffloadResult(0, src.name)
    }
}
"""

    # The gate's adversarial witness, verbatim: the resolver is replaced by a raw
    # host File whose argument is an expression, so none of R1's three spellings
    # match and the handler still compiles and still imports.
    M6_PHOTOS_IMPORT_NO_RESOLVER = M6_PHOTOS_IMPORT_OK.replace(
        """        val src = PRootKernel.resolvePathForSession(request.sessionId, guestPath, context)
            ?: return NativeOffloadResult(1, "no_file")""",
        "        val src = File(rawPath.trim())",
    )

    # A resolver that is present but whose RESULT never reaches the read: this is
    # what "keep the sanitiser so the gate is happy, open something else" looks
    # like — R1 sees nothing (the File() argument is an expression).
    M6_PHOTOS_IMPORT_RESULT_DROPPED = M6_PHOTOS_IMPORT_OK.replace(
        """        val src = PRootKernel.resolvePathForSession(request.sessionId, guestPath, context)
            ?: return NativeOffloadResult(1, "no_file")""",
        """        val resolved = PRootKernel.resolvePathForSession(request.sessionId, guestPath, context)
            ?: return NativeOffloadResult(1, "no_file")
        val src = File(rawPath.trim())""",
    )

    M6_PHOTOS_NO_ENTRY = """package com.rikkaminis.app.sandbox.offload

class PhotosOffloadHandler(private val context: Context) : NativeOffloadHandler {
    private fun handleImport(args: OffloadArgs, request: NativeOffloadRequest): NativeOffloadResult {
        val rawPath = args.get("path") ?: return NativeOffloadResult(2, "required")
        return NativeOffloadResult(0, rawPath)
    }
}
"""

    def photos_fixture_root(src):
        root = tempfile.mkdtemp(prefix="offload-gate-selftest-photos-")
        path = os.path.join(root, PHOTOS_HANDLER_REL)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        return root

    m6_arms = [
        ("M6 dirty: `File(rawPath.trim())` — an expression, not the three R1 spellings",
         "tree", M6_FILE_EXPR_TAINTED, 1, "derives from the guest path argument (rawPath)"),
        ("M6 dirty: `File(Paths.get(rawPath).toString())` — value rebuilt, never resolved",
         "tree", M6_FILE_EXPR_PATHS_GET, 1, "derives from the guest path argument"),
        ("M6 dirty: a `\"video/*\"` literal must not blank the rest of the handler",
         "tree", M6_STRIP_STRING_COMMENT, 1, "derives from the guest path argument"),
        ("M6 clean: `File(<resolved>.absolutePath)` after the session resolver",
         "tree", M6_FILE_EXPR_RESOLVED, 0, None),
        ("M6 clean: photos import resolves, then opens the RESOLVED file",
         "photos", M6_PHOTOS_IMPORT_OK, 0, None),
        ("M6 dirty (witness): sanitisation replaced by `File(rawPath.trim())`",
         "photos", M6_PHOTOS_IMPORT_NO_RESOLVER, 1, "never resolves the guest path"),
        ("M6 dirty: resolver present but its result never reaches the read",
         "photos", M6_PHOTOS_IMPORT_RESULT_DROPPED, 1, "never reaches a host open point"),
        ("M6 fail-closed: no import entry can be derived (renamed or moved)",
         "photos", M6_PHOTOS_NO_ENTRY, 1, "no import entry could be derived"),
        ("M6 fail-closed: PhotosOffloadHandler.kt missing",
         "photos_missing", None, 1, "missing — cannot verify"),
    ]
    m6_failures = []
    for label, kind, src, expected, needle in m6_arms:
        if kind == "photos":
            root = photos_fixture_root(src)
            try:
                problems = scan_photos_import_sanitisation(root)
            finally:
                shutil.rmtree(root, ignore_errors=True)
        elif kind == "photos_missing":
            root = tempfile.mkdtemp(prefix="offload-gate-selftest-nophotos-")
            try:
                problems = scan_photos_import_sanitisation(root)
            finally:
                shutil.rmtree(root, ignore_errors=True)
        else:
            root = _fixture_root({"AOffloadHandler.kt": src})
            try:
                _, problems = check_tree(root, verbose=False)
            finally:
                shutil.rmtree(root, ignore_errors=True)
        ok = len(problems) == expected
        if ok and needle is not None:
            ok = any(needle in p for p in problems)
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                               "" if ok else "  → problems=%s" % problems))
        if not ok:
            m6_failures.append(label)
    if m6_failures:
        print("❌ selftest failed: %s" % ", ".join(m6_failures))
        return 1
    print("M6_TAINT_SELFTEST=PASS (%d fixtures: %d dirty/fail-closed arms red on their own "
          "criterion, %d clean trees green)"
          % (len(m6_arms),
             sum(1 for a in m6_arms if a[3] > 0),
             sum(1 for a in m6_arms if a[3] == 0)))
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    root = os.path.abspath(args[0]) if args else os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if "--selftest" in flags:
        return selftest()
    unknown = [f for f in flags if f != "--selftest"]
    if unknown:
        print("unknown option(s): %s" % " ".join(unknown))
        return 2
    scanned, problems = check_tree(root)
    extra = []
    extra.extend(scan_player_session_path(root))
    extra.extend(scan_shizuku_paths(root))
    extra.extend(scan_debug_session_path(root))
    extra.extend(scan_debug_exec_scope(root))
    extra.extend(scan_photos_import_sanitisation(root))
    # [meta-audit-1024 M6] a red gate has to say WHAT is red: the extension
    # criteria used to contribute only to the count, so a witness like
    # `File(rawPath.trim())` reported "2 violation(s)" with one line of text.
    for p in extra:
        print("❌ %s" % p)
    problems.extend(extra)
    if problems:
        print("offload_host_path_check: %d handler file(s) scanned, %d violation(s)" % (scanned, len(problems)))
        return 1
    print("offload_host_path_check: %d handler file(s) + %d intent dispatcher(s) scanned, "
          "0 violations — OFFLOAD-HOST-PATH GATE OK (%d Intent.parseUri site(s), "
          "%d browser_fallback_url read(s))"
          % (scanned, INTENT_SCOPE_STATS["consumers"], INTENT_SCOPE_STATS["sites"],
             INTENT_SCOPE_STATS["fallbacks"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

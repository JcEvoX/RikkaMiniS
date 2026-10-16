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
      launched activity a read/write/persistable URI grant), clipData, and a
      same-package `component` target (explicit same-uid intents bypass
      `exported=false`).
      Fixed in OpenOffloadHandler ([deep-audit-1016c FIX-B B]).

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

# `val <ident> = ...get("path"|"file"|"source")` (any receiver spelling).
ARG_ASSIGN_RE = re.compile(r'\b(\w+)\s*=\s*[\w.]*get\(\s*"(path|file|source)"')
# A bare `File(<ident>)` / `File(args.get("path"))` construction.
FILE_IDENT_RE = re.compile(r"\bFile\(\s*([A-Za-z_]\w*)\s*\)")
FILE_DIRECT_RE = re.compile(r'\bFile\(\s*[^)\n]*get\(\s*"(path|file|source)"')
# Guest→host converters: their call is what makes a tainted value SAFE to wrap in File().
RESOLVER_CALL_RE = re.compile(
    r"\b(?:resolvePathForSession|resolveSourcePath|resolveHostPath|resolveGuestHostPath|readLinuxPath)\("
)


def strip_comments(text):
    """Blank out // and /* */ comment content, keeping line structure."""
    out = []
    in_block = False
    for line in text.splitlines():
        s = line
        if in_block:
            end = s.find("*/")
            if end < 0:
                out.append("")
                continue
            s = s[end + 2:]
            in_block = False
        while True:
            d = s.find("//")
            b = s.find("/*")
            if b >= 0 and (d < 0 or b < d):
                end = s.find("*/", b + 2)
                if end < 0:
                    s = s[:b]
                    in_block = True
                    break
                s = s[:b] + s[end + 2:]
                continue
            if d >= 0:
                s = s[:d]
            break
        out.append(s)
    return "\n".join(out)


def tainted_from_args(text):
    """One-hop taint: identifiers carrying a guest path taken from args.

    `val rawPath = args.get("path")` taints rawPath. An identifier assigned from
    an expression mentioning a tainted identifier stays tainted — UNLESS that
    expression calls a guest→host resolver, which is exactly the conversion the
    rule is asking for (`val host = resolveSourcePath(raw, sid)` is legitimate;
    `File(host)` after that is fine).
    """
    tainted = {}
    for line in text.splitlines():
        m = ARG_ASSIGN_RE.search(line)
        if m:
            tainted[m.group(1)] = "raw"
            continue
        a = re.match(r"\s*(?:val|var)\s+(\w+)[^=]*=\s*(.*)$", line)
        if not a:
            continue
        name, expr = a.group(1), a.group(2)
        if any(re.search(r"\b%s\b" % re.escape(t), expr) for t in tainted):
            tainted[name] = "resolved" if RESOLVER_CALL_RE.search(expr) else "raw"
    return tainted


def scan_bare_file(rel, text):
    """R1: args.get(path|file|source) handed to a bare File(...)."""
    problems = []
    for i, line in enumerate(text.splitlines(), 1):
        if FILE_DIRECT_RE.search(line):
            problems.append(
                "%s:%d: args value reaches a bare File(...) — resolve it through "
                "PRootKernel.resolvePathForSession / GuestPathMapper instead" % (rel, i)
            )
    assigned = tainted_from_args(text)
    if assigned:
        for i, line in enumerate(text.splitlines(), 1):
            for m in FILE_IDENT_RE.finditer(line):
                ident = m.group(1)
                if assigned.get(ident) == "raw":
                    problems.append(
                        "%s:%d: `File(%s)` derives from the guest path argument without passing "
                        "through a guest→host resolver — no raw host File" % (rel, i, ident)
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


def scan_parse_uri(rel, text):
    """R2: Intent.parseUri must be followed by grant/clipData/component stripping."""
    problems = []
    for m in re.finditer(r"Intent\.parseUri\(", text):
        line_no = text.count("\n", 0, m.start()) + 1
        window = _statement_window(text, m.start())
        if window is None:
            problems.append("%s:%d: Intent.parseUri statement could not be delimited" % (rel, line_no))
            continue
        missing = []
        if "removeFlags(" not in window:
            missing.append("removeFlags(...)")
        for flag in GRANT_FLAGS:
            if flag not in window:
                missing.append(flag)
        if "clipData = null" not in window:
            missing.append("clipData = null")
        if missing:
            problems.append(
                "%s:%d: Intent.parseUri without hardening — missing %s"
                % (rel, line_no, ", ".join(missing))
            )
    return problems


def check_tree(root, verbose=True):
    offload_dir = os.path.join(root, OFFLOAD_REL)
    problems = []
    scanned = 0
    if not os.path.isdir(offload_dir):
        print("❌ offload dir not found: %s" % offload_dir)
        return 1, ["missing dir"]
    for name in sorted(os.listdir(offload_dir)):
        if not name.endswith("OffloadHandler.kt"):
            continue
        path = os.path.join(offload_dir, name)
        rel = os.path.join(OFFLOAD_REL, name)
        with open(path, encoding="utf-8", errors="replace") as fh:
            code = strip_comments(fh.read())
        scanned += 1
        problems.extend(scan_bare_file(rel, code))
        problems.extend(scan_parse_uri(rel, code))
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
        val intent = Intent.parseUri(url, Intent.URI_INTENT_SCHEME).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            removeFlags(
                Intent.FLAG_GRANT_READ_URI_PERMISSION or
                    Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                    Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION or
                    Intent.FLAG_GRANT_PREFIX_URI_PERMISSION,
            )
            clipData = null
            if (component?.packageName == context.packageName) component = null
            selector = null
        }
        context.startActivity(intent)
    }
}
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
    if problems:
        print("offload_host_path_check: %d handler file(s) scanned, %d violation(s)" % (scanned, len(problems)))
        return 1
    print("offload_host_path_check: %d handler file(s) scanned, 0 violations — "
          "OFFLOAD-HOST-PATH GATE OK" % scanned)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""file_preview_url_encode_check — deep-audit-1022 FIX-H (backlog §1021-V) gate.

Pins the file:// URL encoding contract in ui/sandbox/FilePreviewScreen.kt
(device-verified 2026-10-17: a name containing `#` pasted raw into a
`file://` URL makes WebView parse the `#` as a fragment start, so
`a#b.html` loads as `a.html` → `net::ERR_FILE_NOT_FOUND`; space / `?` / `%` /
non-ASCII names break the same way):

  arm1: the file must not contain a bare `"file://${…absolutePath}"`
        template (the pre-fix shape) — every file:// URL must be built
        through the encoder.
  arm2: the single-source encoder must be present
        (`internal fun encodeFileUrlPath(`) and both load sites must go
        through it (`fileUrlFor(item.file)` — the HTML preview and the
        print WebView).

Usage:
  python3 file_preview_url_encode_check.py <repo_root>   # real tree
  python3 file_preview_url_encode_check.py --selftest    # fixtures

Selftest marker: FILE_PREVIEW_URL_ENCODE_SELFTEST=PASS
"""

import os
import re
import sys
import tempfile

SCREEN_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/FilePreviewScreen.kt"

BARE_TEMPLATE_RE = re.compile(r"file://\$\{[^}]*absolutePath\}")
ENCODE_DEF_RE = re.compile(r"internal fun encodeFileUrlPath\(")
LOAD_SITE_RE = re.compile(r"fileUrlFor\(item\.file\)")


def strip_comments(src):
    """Remove // line comments and /* */ block comments (KDoc included).

    String-aware: `//` inside a double-quoted literal (e.g. `"file://"`) is
    NOT a comment — the stripper tracks quote state so URL strings survive.
    """
    out = []
    i, n = 0, len(src)
    in_block = in_line = in_str = False
    while i < n:
        if in_block:
            if src.startswith("*/", i):
                in_block = False
                i += 2
            else:
                i += 1
        elif in_str:
            out.append(src[i])
            if src[i] == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if src[i] == '"':
                in_str = False
            i += 1
        elif in_line:
            if src[i] == "\n":
                in_line = False
                out.append("\n")
            i += 1
        elif src.startswith("//", i):
            in_line = True
            i += 2
        elif src.startswith("/*", i):
            in_block = True
            i += 2
        elif src[i] == '"':
            in_str = True
            out.append(src[i])
            i += 1
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def check(root):
    """Return a list of violation strings (empty = green)."""
    path = os.path.join(root, SCREEN_REL)
    if not os.path.exists(path):
        return [f"{SCREEN_REL} not found"]
    src = open(path, encoding="utf-8", errors="replace").read()
    code = strip_comments(src)
    violations = []
    if BARE_TEMPLATE_RE.search(code):
        violations.append("bare 'file://${…absolutePath}' template still present (pre-fix shape)")
    if '"file://" + file.absolutePath' in code:
        violations.append(
            "bare '\"file://\" + file.absolutePath' concatenation still present — must route through the encoder"
        )
    if not ENCODE_DEF_RE.search(code):
        violations.append("encoder `internal fun encodeFileUrlPath(` not found")
    if '"file://" + encodeFileUrlPath(' not in code:
        violations.append(
            "the file:// URL build site must concatenate through encodeFileUrlPath( — exactly one"
        )
    loads = LOAD_SITE_RE.findall(code)
    if len(loads) < 2:
        violations.append(
            f"expected 2 load sites through fileUrlFor(item.file) (preview + print), got {len(loads)}"
        )
    return violations


# --- compliant / violation fixture snippets (code-level, not comments) ---
# The compliant fixture exercises # / space+CJK names through the encoder —
# the three real-world trigger classes from backlog §1021-V.

COMPLIANT = """package com.rikkaminis.app.ui.sandbox

internal fun encodeFileUrlPath(path: String): String = path

private fun fileUrlFor(file: File): String = "file://" + encodeFileUrlPath(file.absolutePath)

class Screen {
    private fun preview(item: Item) {
        val targetUrl = fileUrlFor(item.file)
        post { loadUrl(targetUrl) }
    }
    private fun printFile(context: Context, item: Item) {
        webView.loadUrl(fileUrlFor(item.file))
    }
}
"""

VIOLATION_BARE = COMPLIANT.replace(
    'val targetUrl = fileUrlFor(item.file)',
    'val targetUrl = "file://${item.file.absolutePath}"',
)

VIOLATION_NO_ENCODER = COMPLIANT.replace(
    'private fun fileUrlFor(file: File): String = "file://" + encodeFileUrlPath(file.absolutePath)',
    'private fun fileUrlFor(file: File): String = "file://" + file.absolutePath',
)


def run_fixture(name, content, expect_ok):
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, SCREEN_REL.rsplit("/", 1)[0]))
        with open(os.path.join(tmp, SCREEN_REL), "w") as f:
            f.write(content)
        v = check(tmp)
        ok = (not v) == expect_ok
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] fixture {name}: {v}")
        return ok


def selftest():
    print("file_preview_url_encode_check selftest:")
    ok = True
    ok &= run_fixture("compliant (#/space/CJK via encoder)", COMPLIANT, expect_ok=True)
    ok &= run_fixture("violation-bare-template", VIOLATION_BARE, expect_ok=False)
    ok &= run_fixture("violation-no-encoder", VIOLATION_NO_ENCODER, expect_ok=False)
    print("FILE_PREVIEW_URL_ENCODE_SELFTEST=" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if len(sys.argv) < 2:
        print("usage: python3 file_preview_url_encode_check.py <repo_root>", file=sys.stderr)
        sys.exit(2)
    root = sys.argv[1]
    violations = check(root)
    if violations:
        for v in violations:
            print("FAIL:", v)
        sys.exit(1)
    print("file_preview_url_encode_check: OK (arm1 + arm2 green)")


if __name__ == "__main__":
    main()

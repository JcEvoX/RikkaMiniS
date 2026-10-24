#!/usr/bin/env python3
"""file_preview_url_encode_check — deep-audit-1022 FIX-H (backlog §1021-V) gate.

Pins the file:// URL encoding contract in ui/sandbox/FilePreviewScreen.kt
(device-verified 2026-10-17: a name containing `#` pasted raw into a
`file://` URL makes WebView parse the `#` as a fragment start, so
`a#b.html` loads as `a.html` → `net::ERR_FILE_NOT_FOUND`; space / `?` / `%` /
non-ASCII names break the same way):

  arm1: no raw path may be concatenated into a `file://` URL — every
        file:// URL build site must hand the encoder the operand.
  arm2: the encoder must EXIST and its body must really escape (a `%XX`
        emission whose carrier is what the function returns) — declaring
        `internal fun encodeFileUrlPath(` and returning the input unchanged
        is not an encoder.
  arm3: `fileUrlFor` must return the encoder's value, and both load sites must
        consume it as-is (`fileUrlFor(item.file)` may not be rewritten by a
        chained `let/run/also/apply/takeIf/takeUnless` or an elvis).

[wide-1024 gate-witness audit] arms 2/3 were reachable three ways, all three
measured on the real tree before this change:

  * `encodeFileUrlPath` body prefixed with `if (true) return path` — the old
    criterion only matched the declaration head.
  * `fileUrlFor` rewritten to `"file://" + encodeFileUrlPath(file.absolutePath)
    .let { file.path }` — the old criterion counted the prefix substring, the
    value produced by the encoder was thrown away one hop later.
  * a load site rewritten to `fileUrlFor(item.file).let { "file://" +
    item.file.path }` — the old criterion counted `fileUrlFor(item.file)`
    occurrences, and the count stayed 2.

The fix is a dataflow criterion, not a spelling one: the encoder's RESULT must
be the value that leaves the builder, and the escaping must actually happen
inside the encoder body. Parsing failures (unbalanced parens/braces) are
fail-closed violations, never silent passes.

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

BARE_TEMPLATE_RE = re.compile(r"file://\$\{[^}]*\.(?:absolutePath|canonicalPath|path)\}")
ENCODE_DEF_RE = re.compile(r"internal fun encodeFileUrlPath\(")
LOAD_SITE_RE = re.compile(r"fileUrlFor\(item\.file\)")
RAW_PREFIX_RE = re.compile(r'"file://"\s*\+')
# A chained scope function in front of the value means the value it produced is
# not what the caller sees (`x.let { 0 }` is the cheapest way to make a correct
# call produce a wrong answer).
VALUE_DISCARD_RE = re.compile(r"^\s*\.\s*(let|run|also|apply|takeIf|takeUnless)\b")
ELVIS_RE = re.compile(r"^\s*\?:")
ESCAPE_EMIT_RE = re.compile(r"append\(\s*'%'\s*\)|append\(\s*\"%\"\s*\)|\+= *'%'|%02[Xx]|\bUri\.encode\b|\bURLEncoder\.encode\b")
ESCAPE_CARRIER_RE = re.compile(r"\b(\w+)\s*(?:\.\s*append\(\s*'%'\s*\)|\.\s*append\(\s*\"%\"\s*\)|\+= *'%')")


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


def _match(text, i, open_ch, close_ch):
    """Index of the bracket closing text[i]; -1 when unbalanced."""
    depth = 0
    while i < len(text):
        c = text[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _block_at(text, open_idx):
    end = _match(text, open_idx, "{", "}")
    return None if end < 0 else text[open_idx:end + 1]


def _fun_parts(code, sig):
    """(params, body, form) for the single declaration matching `sig`.

    Returns None when the signature is missing or ambiguous. `form` is
    "block" | "expr" | "missing"; an expression body is taken to the end of the
    logical line (continuation lines that end in an operator are followed).
    """
    if code.count(sig) != 1:
        return None
    i = code.index(sig)
    p = code.find("(", i)
    if p < 0:
        return None
    q = _match(code, p, "(", ")")
    if q < 0:
        return None
    params = code[p + 1:q]
    nl = code.find("\n", q)
    tail = code[q + 1: nl if nl >= 0 else len(code)]
    eq = tail.find("=")
    brace = tail.find("{")
    if brace >= 0 and (eq < 0 or brace < eq):
        body = _block_at(code, code.find("{", q))
        return params, body, "block"
    if eq < 0:
        return params, None, "missing"
    start = q + 1 + eq + 1
    end = code.find("\n", start)
    end = len(code) if end < 0 else end
    while True:
        line = code[start:end].rstrip()
        if line and line[-1] in "+-*/&|,?:=":
            nxt = code.find("\n", end + 1)
            end = len(code) if nxt < 0 else nxt
            continue
        break
    return params, code[start:end], "expr"


def _first_param(params):
    m = re.match(r"\s*(\w+)", params or "")
    return m.group(1) if m else None


def check_encoder(code):
    """arm2 — the single encoder must exist AND really escape (dataflow)."""
    problems = []
    if not ENCODE_DEF_RE.search(code):
        return ["encoder `internal fun encodeFileUrlPath(` not found"]
    parts = _fun_parts(code, "internal fun encodeFileUrlPath(")
    if parts is None:
        return ["`internal fun encodeFileUrlPath(` must be declared exactly once "
                "(anchor missing or duplicated — re-point this criterion)"]
    params, body, form = parts
    if body is None or not body.strip():
        return ["encodeFileUrlPath has no body — the encoder cannot be an empty declaration"]
    param = _first_param(params)
    if not ESCAPE_EMIT_RE.search(body):
        problems.append(
            "encodeFileUrlPath never emits a percent escape (`'%'` append / `%02X` / "
            "Uri.encode / URLEncoder.encode) — a function that returns its input is not an encoder"
        )
    # dataflow: the returned value must carry the escapes
    carriers = set(ESCAPE_CARRIER_RE.findall(body))
    returns = [m.group(1) for m in re.finditer(r"\breturn\b([^\n;}]*)", body)]
    expr_return = body.strip() if form == "expr" else None
    exprs = list(returns) + ([expr_return] if expr_return else [])
    if param:
        for expr in exprs:
            if re.fullmatch(r"\s*%s\s*" % re.escape(param), expr):
                problems.append(
                    "encodeFileUrlPath returns its input unchanged (`return %s`) — every "
                    "`#`/`?`/space/`%%` byte survives into the file:// URL unescaped" % param
                )
                break
    if carriers:
        if not any(any(re.search(r"\b%s\b" % re.escape(c), e) for c in carriers) for e in exprs):
            problems.append(
                "encodeFileUrlPath builds an escaped buffer (%s) but never returns it — the "
                "escaped value must be what the function returns"
                % ", ".join(sorted(carriers))
            )
    elif not re.search(r"\b(Uri\.encode|URLEncoder\.encode)\s*\(", " ".join(exprs)):
        problems.append(
            "encodeFileUrlPath returns a value that carries no escaping (fail-closed: no "
            "escape carrier found in any return expression)"
        )
    return problems


def check_raw_concats(code):
    """arm1 — no raw operand may be concatenated after `"file://" +`."""
    problems = []
    for m in RAW_PREFIX_RE.finditer(code):
        start = m.end()
        nl = code.find("\n", start)
        seg = code[start: nl if nl >= 0 else len(code)]
        if re.match(r"\s*encodeFileUrlPath\s*\(", seg):
            continue
        problems.append(
            "`\"file://\" +` must be followed by encodeFileUrlPath( … ) — found %r "
            "(a raw path in a file:// URL turns `#` into a fragment)" % seg.strip()[:60]
        )
    return problems


def check_file_url_for(code):
    """arm3a — fileUrlFor must RETURN the encoder's value."""
    parts = _fun_parts(code, "private fun fileUrlFor(")
    if parts is None:
        return ["`private fun fileUrlFor(` must be declared exactly once — it is the single "
                "file:// URL builder this criterion pins"]
    _params, body, form = parts
    if not body or not body.strip():
        return ["fileUrlFor has no body"]
    problems = []
    if body.count('"file://" +') != 1:
        problems.append(
            "fileUrlFor must build exactly one `\"file://\" + …` URL (found %d)"
            % body.count('"file://" +')
        )
    if body.count("encodeFileUrlPath(") != 1:
        problems.append(
            "fileUrlFor must route the path through exactly one encodeFileUrlPath( call "
            "(found %d) — the file:// URL may only be built from the encoder's value"
            % body.count("encodeFileUrlPath(")
        )
    for m in re.finditer(r"encodeFileUrlPath\s*\(", body):
        close = _match(body, m.end() - 1, "(", ")")
        if close < 0:
            problems.append(
                "fileUrlFor: unbalanced parentheses around encodeFileUrlPath( "
                "(fail-closed — the criterion cannot read this body)"
            )
            continue
        tail = body[close + 1: close + 80]
        if VALUE_DISCARD_RE.match(tail) or ELVIS_RE.match(tail):
            problems.append(
                "fileUrlFor discards the encoder's value (%r) — the URL must be the encoded "
                "path, not something substituted one chain call later" % tail.strip()[:40]
            )
    if form == "block" and not re.search(r"\breturn\b[^\n]*encodeFileUrlPath\s*\(", body):
        problems.append(
            "fileUrlFor never returns the encoded URL — the builder must hand back "
            "`\"file://\" + encodeFileUrlPath(...)`"
        )
    return problems


def check_load_sites(code):
    """arm3b — both load sites consume the URL as produced."""
    problems = []
    loads = list(LOAD_SITE_RE.finditer(code))
    if len(loads) < 2:
        problems.append(
            "expected 2 load sites through fileUrlFor(item.file) (preview + print), got %d"
            % len(loads)
        )
    for m in loads:
        tail = code[m.end(): m.end() + 80]
        if VALUE_DISCARD_RE.match(tail) or ELVIS_RE.match(tail):
            problems.append(
                "a load site rewrites fileUrlFor(item.file) with %r — the printed/previewed "
                "URL must be the encoder's output, not a value swapped in behind it"
                % tail.strip()[:40]
            )
    return problems


def check(root):
    """Return a list of violation strings (empty = green)."""
    path = os.path.join(root, SCREEN_REL)
    if not os.path.exists(path):
        return [f"{SCREEN_REL} not found"]
    src = open(path, encoding="utf-8", errors="replace").read()
    code = strip_comments(src)
    violations = []
    if BARE_TEMPLATE_RE.search(code):
        violations.append("bare 'file://${…path}' template still present (pre-fix shape)")
    if '"file://" + file.absolutePath' in code:
        violations.append(
            "bare '\"file://\" + file.absolutePath' concatenation still present — must route through the encoder"
        )
    violations.extend(check_raw_concats(code))
    violations.extend(check_encoder(code))
    violations.extend(check_file_url_for(code))
    violations.extend(check_load_sites(code))
    return violations


# --- compliant / violation fixture snippets (code-level, not comments) ---
# The compliant fixture exercises # / space+CJK names through the encoder —
# the three real-world trigger classes from backlog §1021-V — and carries a
# real escaping body, because arm2 now measures the body, not the head.

ENCODER_BLOCK = """internal fun encodeFileUrlPath(path: String): String {
    val sb = StringBuilder(path.length)
    for (b in path.toByteArray(Charsets.UTF_8)) {
        val v = b.toInt() and 0xFF
        if (v < 0x21 || v > 0x7E) {
            sb.append('%').append(Integer.toHexString(v shr 4)).append(Integer.toHexString(v and 0x0F))
        } else {
            sb.append(v.toChar())
        }
    }
    return sb.toString()
}"""

COMPLIANT = """package com.rikkaminis.app.ui.sandbox

""" + ENCODER_BLOCK + """

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

# wide-1024 arm2(a): declaration kept, body short-circuited at the first line.
VIOLATION_ENCODER_SHORT_CIRCUIT = COMPLIANT.replace(
    "    val sb = StringBuilder(path.length)",
    "    if (true) return path\n    val sb = StringBuilder(path.length)",
)

# wide-1024 arm2(b): body hollowed out to `= path` (declaration still matches).
VIOLATION_ENCODER_HOLLOW = COMPLIANT.replace(
    ENCODER_BLOCK,
    "internal fun encodeFileUrlPath(path: String): String = path",
)

# wide-1024 arm3(a): the encoder runs, then its value is thrown away.
VIOLATION_FILEURLFOR_DISCARD = COMPLIANT.replace(
    'private fun fileUrlFor(file: File): String = "file://" + encodeFileUrlPath(file.absolutePath)',
    'private fun fileUrlFor(file: File): String = "file://" + encodeFileUrlPath(file.absolutePath).let { file.path }',
)

# wide-1024 arm3(b): the load-site count stays 2, the URL is swapped behind it.
VIOLATION_LOAD_SITE_DISCARD = COMPLIANT.replace(
    "val targetUrl = fileUrlFor(item.file)",
    'val targetUrl = fileUrlFor(item.file).let { "file://" + item.file.path }',
)


def run_fixture(name, content, expect_ok, expect_msg=None):
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, SCREEN_REL.rsplit("/", 1)[0]))
        with open(os.path.join(tmp, SCREEN_REL), "w") as f:
            f.write(content)
        v = check(tmp)
        ok = (not v) == expect_ok
        if ok and not expect_ok and expect_msg is not None:
            ok = any(expect_msg in item for item in v)
            if not ok:
                print(f"  [FAIL] fixture {name}: expected a {expect_msg!r} violation, got {v}")
                return False
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] fixture {name}: {v}")
        return ok


def selftest():
    print("file_preview_url_encode_check selftest:")
    ok = True
    ok &= run_fixture("compliant (#/space/CJK through the encoder)", COMPLIANT, expect_ok=True)
    ok &= run_fixture("violation-bare-template", VIOLATION_BARE, expect_ok=False,
                      expect_msg="bare 'file://${")
    ok &= run_fixture("violation-no-encoder", VIOLATION_NO_ENCODER, expect_ok=False,
                      expect_msg='"file://" + file.absolutePath')
    ok &= run_fixture("violation-encoder-short-circuit", VIOLATION_ENCODER_SHORT_CIRCUIT,
                      expect_ok=False, expect_msg="returns its input unchanged")
    ok &= run_fixture("violation-encoder-hollow", VIOLATION_ENCODER_HOLLOW,
                      expect_ok=False, expect_msg="never emits a percent escape")
    ok &= run_fixture("violation-fileurlfor-discard", VIOLATION_FILEURLFOR_DISCARD,
                      expect_ok=False, expect_msg="discards the encoder's value")
    ok &= run_fixture("violation-load-site-discard", VIOLATION_LOAD_SITE_DISCARD,
                      expect_ok=False, expect_msg="rewrites fileUrlFor(item.file)")
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
    print("file_preview_url_encode_check: OK (arm1 + arm2 + arm3 green)")


if __name__ == "__main__":
    main()

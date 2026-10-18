#!/usr/bin/env python3
"""deep-audit-1021 FIX-H (ISSUE-11, P2) — MinisOpenUrlBroker scheme gate.

Pins the contract fixed by branch fix/da1021-open-url-broker-scheme for
terminal/MinisOpenUrlBroker.kt: BOTH entry points must funnel through the
single `isSupportedScheme` whitelist before anything lands in `_pendingUrl`.

Bug context (audit-0917 half-fix): `offer(uri: Uri)` published whatever a
caller handed it — no scheme check — while the `[audit-0917]` comment sitting
on `offer(uriString: String)` claimed "Same scheme gate as offer(Uri)" and
described the Uri overload accepting file:// / javascript: payloads. The
guard described by the comment had been added to neither overload's call
path as written: the comment described a defect that did not exist on the
String overload (which always had its own gate) and the Uri overload itself
stayed bare.

The fix delegates offer(String) → offer(Uri) so the whitelist is consulted
exactly once. The gate enforces:

  U1  the `offer(uri: Uri)` body consults `isSupportedScheme(` (a bare
      `_pendingUrl.value = uri` revert is RED);
  U2  single gate: the String overload either still gates on
      `isSupportedScheme(parsed.scheme)` or delegates via `offer(parsed)`,
      AND the file contains exactly ONE non-null `_pendingUrl.value =`
      write point (a second inline copy is the next drift site);
  U3  ordering: inside the `offer(uri: Uri)` body the scheme check appears
      BEFORE the `_pendingUrl.value = uri` assignment (an order mutation
      that publishes first is RED);
  U4  the whitelist itself is not weakened: `isSupportedScheme` still
      lowercases, fail-closes on null, and still lists http/https/about/minis.

deep-audit-1027 FIX-G (ISSUE-07, P2) added the consumer half of the same
contract, which lives in a different file:

  U5  `ui/terminal/TerminalScreen.kt`, inside its `LaunchedEffect(pendingUrl)`
      block, must call `MinisOpenUrlBroker.consume()` ONLY inside the
      `if (MinisOpenUrlBroker.isWebScheme(...))` branch. The fullscreen
      terminal only *presents* web schemes; consuming unconditionally ate
      `minis://` deep links / local-file previews before they could sit in
      the broker for the underlying ChatScreen (whose observer deliberately
      defers while `terminalVisible` is true and re-runs when it flips back
      to false). Anchors are fail-closed: a missing/ambiguous block is RED.
      The criterion is skipped when TerminalScreen.kt is absent (mini-tree
      fixtures that only stage the broker file).

Usage: python3 open_url_broker_scheme_check.py [ROOT]   (ROOT = repo root)
       python3 open_url_broker_scheme_check.py --selftest
Exit:  0 = gate green, 1 = violations found (or selftest failures).
"""
import os
import re
import sys

BROKER_REL = "src/android/app/src/main/java/com/rikkaminis/app/terminal/MinisOpenUrlBroker.kt"
TERMINAL_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/terminal/TerminalScreen.kt"


def strip_comments(src):
    """Remove // line comments and /* */ block comments (KDoc included).

    String-literal aware: `//` inside a string (e.g. "file://") must NOT be
    treated as a comment — the whitelist literals live on code lines and
    would otherwise be truncated.
    """
    out = []
    in_block = False
    for line in src.splitlines():
        res = []
        i = 0
        n = len(line)
        quote = None
        while i < n:
            if in_block:
                j = line.find("*/", i)
                if j < 0:
                    i = n
                else:
                    in_block = False
                    i = j + 2
                continue
            c = line[i]
            if quote is not None:
                res.append(c)
                if c == "\\" and i + 1 < n:
                    res.append(line[i + 1])
                    i += 2
                    continue
                if c == quote:
                    quote = None
                i += 1
                continue
            if line.startswith("//", i):
                break
            if line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            if c in ('"', "'"):
                quote = c
            res.append(c)
            i += 1
        out.append("".join(res))
    return "\n".join(out)


def extract_fun(code, decl_re):
    """Extract the body (incl. braces) of the function matching decl_re.

    Fail-closed: returns None when the declaration is missing or ambiguous
    (multiple matches) — the caller reports a violation instead of guessing.
    Brace matching is string-aware via the comment-stripped code.
    """
    m = decl_re.search(code)
    if not m:
        return None
    if decl_re.search(code, m.end()):
        return None  # ambiguous
    # The declaration regex ends with `{` (the function's opening brace) —
    # start brace matching AT it, not at the next `{` (which would live
    # inside the body or a later function and truncate the extract).
    i = m.end() - 1
    if code[i] != "{":
        i = code.find("{", m.end())
        if i < 0:
            return None
    depth = 0
    quote = None
    n = len(code)
    while i < n:
        c = code[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return code[m.start():i + 1]
        i += 1
    return None


RE_OFFER_URI = re.compile(r"\bfun\s+offer\(\s*uri:\s*Uri\s*\)\s*\{")
RE_OFFER_STR = re.compile(r"\bfun\s+offer\(\s*uriString:\s*String\s*\)\s*\{")
RE_IS_SUP = re.compile(r"\bfun\s+isSupportedScheme\(\s*scheme:\s*String\?\s*\)\s*:\s*Boolean\s*\{")
RE_WRITE = re.compile(r"_pendingUrl\.value\s*=\s*(?=\S)(?!null\b)")


def analyse(code_raw):
    """Return [(criterion, ok, detail)]."""
    code = strip_comments(code_raw)
    results = []

    body_uri = extract_fun(code, RE_OFFER_URI)
    if body_uri is None:
        results.append(("U1 offer(uri: Uri) present and unambiguous", False,
                        "declaration missing or ambiguous — fail-closed"))
        return results

    # --- U1: the Uri overload consults the whitelist ---
    ok1 = "isSupportedScheme(" in body_uri
    results.append(("U1 offer(uri: Uri) consults isSupportedScheme", ok1,
                    "scheme gate missing from the Uri overload" if not ok1 else "gate present"))

    # --- U2: single gate, exactly one non-null publish point ---
    body_str = extract_fun(code, RE_OFFER_STR)
    if body_str is None:
        results.append(("U2a offer(uriString: String) present and unambiguous", False,
                        "declaration missing or ambiguous — fail-closed"))
        return results
    gated = "isSupportedScheme(parsed.scheme)" in body_str
    delegates = re.search(r"\boffer\(\s*parsed\s*\)", body_str) is not None
    results.append(("U2a offer(String) funnels through the same gate",
                    gated or delegates,
                    "must gate on isSupportedScheme(parsed.scheme) or delegate to offer(parsed)"))
    n_writes = len(RE_WRITE.findall(code))
    results.append(("U2b exactly one non-null _pendingUrl.value write point", n_writes == 1,
                    "found %d (single shared gate requires exactly 1)" % n_writes))

    # --- U3: publish must come after the check ---
    idx_gate = body_uri.find("isSupportedScheme(")
    m_write = RE_WRITE.search(body_uri)
    idx_write = m_write.start() if m_write else -1
    ok3 = idx_gate >= 0 and idx_write >= 0 and idx_gate < idx_write
    results.append(("U3 scheme check precedes the _pendingUrl publish", ok3,
                    "gate idx=%d write idx=%d" % (idx_gate, idx_write)))

    # --- U4: whitelist itself not weakened ---
    body_sup = extract_fun(code, RE_IS_SUP)
    if body_sup is None:
        results.append(("U4 isSupportedScheme present and unambiguous", False,
                        "whitelist missing or ambiguous — fail-closed"))
        return results
    ok_lower = ".lowercase()" in body_sup
    ok_null = re.search(r"\?\s*:\s*return\s+false", body_sup) is not None
    ok_list = all(s in body_sup for s in ('"http"', '"https"', '"about"', '"minis"'))
    ok4 = ok_lower and ok_null and ok_list
    results.append(("U4 whitelist not weakened (lowercase + null fail-close + 4 schemes)", ok4,
                    "lowercase=%s null-close=%s schemes=%s" % (ok_lower, ok_null, ok_list)))
    return results

# ------------------------------------------------------- TerminalScreen (U5) ---
RE_LAUNCHED_PENDING = re.compile(r"LaunchedEffect\(\s*pendingUrl\s*\)\s*\{")
RE_WEB_SCHEME_IF = re.compile(r"if\s*\(\s*MinisOpenUrlBroker\.isWebScheme\(")
CONSUME_CALL = "MinisOpenUrlBroker.consume()"

def match_brace(code, open_idx):
    """Index of the '}' matching the '{' at open_idx (string-aware), else -1."""
    depth = 0
    i = open_idx
    n = len(code)
    quote = None
    while i < n:
        c = code[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1

def analyse_terminal(code_raw):
    """U5 — the fullscreen terminal may only consume URLs it actually presents."""
    code = strip_comments(code_raw)

    m = RE_LAUNCHED_PENDING.search(code)
    if not m:
        return [("U5a LaunchedEffect(pendingUrl) block present", False,
                 "anchor block missing — fail-closed")]
    end_idx = match_brace(code, m.end() - 1)
    if end_idx < 0:
        return [("U5a LaunchedEffect(pendingUrl) block present", False,
                 "unbalanced block braces — fail-closed")]
    block = code[m.end() - 1:end_idx + 1]
    results = [("U5a LaunchedEffect(pendingUrl) block present", True, "block found")]

    mi = RE_WEB_SCHEME_IF.search(block)
    if not mi:
        results.append(("U5b isWebScheme branch inside the block", False,
                        "no `if (MinisOpenUrlBroker.isWebScheme(` anchor — fail-closed"))
        return results
    oi = block.find("{", mi.end())
    if oi < 0:
        results.append(("U5b isWebScheme branch inside the block", False,
                        "gate without a body — fail-closed"))
        return results
    ei = match_brace(block, oi)
    if ei < 0:
        results.append(("U5b isWebScheme branch inside the block", False,
                        "unbalanced gate braces — fail-closed"))
        return results
    results.append(("U5b isWebScheme branch inside the block", True, "gate body found"))

    sites = []
    start = 0
    while True:
        j = block.find(CONSUME_CALL, start)
        if j < 0:
            break
        sites.append(j)
        start = j + 1
    if not sites:
        results.append(("U5c consume() only inside the isWebScheme branch", False,
                        "no consume() call in the LaunchedEffect(pendingUrl) block"))
    else:
        outside = [j for j in sites if not (oi < j < ei)]
        results.append(("U5c consume() only inside the isWebScheme branch", not outside,
                        "consume() sites=%d outside-gate=%d" % (len(sites), len(outside))))
    return results


# ---------------------------------------------------------------- selftest ---
GOOD = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        if (!isSupportedScheme(uri.scheme)) return
        _pendingUrl.value = uri
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

BAD_NO_GATE = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        _pendingUrl.value = uri
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

BAD_ORDER = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        _pendingUrl.value = uri
        if (!isSupportedScheme(uri.scheme)) return
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

BAD_SECOND_GATE = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        if (!isSupportedScheme(uri.scheme)) return
        _pendingUrl.value = uri
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        // drift: a second inline gate + a second publish point
        if (!isSupportedScheme(parsed.scheme)) return
        _pendingUrl.value = parsed
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""


GOOD_TERMINAL = """package probe
@Composable
fun TerminalScreen(pendingUrl: Uri?) {
    var previewUrl by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(pendingUrl) {
        val uri = pendingUrl ?: return@LaunchedEffect
        if (MinisOpenUrlBroker.isWebScheme(uri.scheme)) {
            previewUrl = uri.toString()
            MinisOpenUrlBroker.consume()
        }
    }
}
"""

BAD_TERMINAL_OUTSIDE = """package probe
@Composable
fun TerminalScreen(pendingUrl: Uri?) {
    var previewUrl by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(pendingUrl) {
        val uri = pendingUrl ?: return@LaunchedEffect
        if (MinisOpenUrlBroker.isWebScheme(uri.scheme)) {
            previewUrl = uri.toString()
        }
        MinisOpenUrlBroker.consume()
    }
}
"""

BAD_TERMINAL_NO_CONSUME = """package probe
@Composable
fun TerminalScreen(pendingUrl: Uri?) {
    var previewUrl by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(pendingUrl) {
        val uri = pendingUrl ?: return@LaunchedEffect
        if (MinisOpenUrlBroker.isWebScheme(uri.scheme)) {
            previewUrl = uri.toString()
        }
    }
}
"""

BAD_TERMINAL_NO_ANCHOR = """package probe
@Composable
fun TerminalScreen(pendingUrl: Uri?) {
    val pendingUrl2 = pendingUrl
}
"""

GOOD_TERMINAL_COMMENT_ONLY = """package probe
// LaunchedEffect(pendingUrl) {
//     if (MinisOpenUrlBroker.isWebScheme(uri.scheme)) {
//         MinisOpenUrlBroker.consume()
//     }
// }
@Composable
fun TerminalScreen(pendingUrl: Uri?) {
    val pendingUrl2 = pendingUrl
}
"""

def selftest():
    failures = []

    def expect(cond, what):
        if not cond:
            failures.append(what)

    def as_dict(res):
        return dict((c, ok) for c, ok, _ in res)

    # good fixture: everything green
    good = as_dict(analyse(GOOD))
    expect(len(good) == 5, "good fixture should produce 5 criteria, got %d" % len(good))
    for c, ok in good.items():
        expect(ok, "good fixture must pass %s" % c)

    # bad1: gate removed from offer(Uri) (the original bug) — U1+U3 red, rest OK
    d = as_dict(analyse(BAD_NO_GATE))
    for c, ok in d.items():
        if c.startswith("U1") or c.startswith("U3"):
            expect(not ok, "BAD_NO_GATE %s must be RED" % c)
        else:
            expect(ok, "BAD_NO_GATE %s must stay OK" % c)

    # bad2: publish before the check (order mutation) — U3 red, U1/U2 stay OK
    d = as_dict(analyse(BAD_ORDER))
    expect(not d["U3 scheme check precedes the _pendingUrl publish"],
           "BAD_ORDER U3 must be RED")
    expect(d["U1 offer(uri: Uri) consults isSupportedScheme"],
           "BAD_ORDER U1 must stay OK (gate text still present)")
    expect(d["U2b exactly one non-null _pendingUrl.value write point"],
           "BAD_ORDER U2b must stay OK")

    # bad3: second inline gate + second publish point — U2b red (drift catcher)
    d = as_dict(analyse(BAD_SECOND_GATE))
    expect(not d["U2b exactly one non-null _pendingUrl.value write point"],
           "BAD_SECOND_GATE U2b must be RED (two publish points)")

    # fail-closed: missing file / garbage input must NOT read as green
    d = as_dict(analyse("class B { }"))
    expect(len(d) == 1 and not d["U1 offer(uri: Uri) present and unambiguous"],
           "garbage input must fail-closed on U1")

    # ---- U5 (TerminalScreen consumer placement) ----
    tgood = as_dict(analyse_terminal(GOOD_TERMINAL))
    expect(len(tgood) == 3, "U5 good fixture should produce 3 criteria, got %d" % len(tgood))
    for c, ok in tgood.items():
        expect(ok, "U5 good fixture must pass %s" % c)

    d = as_dict(analyse_terminal(BAD_TERMINAL_OUTSIDE))
    expect(not d["U5c consume() only inside the isWebScheme branch"],
           "BAD_TERMINAL_OUTSIDE U5c must be RED (consume outside the gate)")
    expect(d["U5a LaunchedEffect(pendingUrl) block present"],
           "BAD_TERMINAL_OUTSIDE U5a must stay OK")
    expect(d["U5b isWebScheme branch inside the block"],
           "BAD_TERMINAL_OUTSIDE U5b must stay OK")

    d = as_dict(analyse_terminal(BAD_TERMINAL_NO_CONSUME))
    expect(not d["U5c consume() only inside the isWebScheme branch"],
           "BAD_TERMINAL_NO_CONSUME U5c must be RED (nothing consumes)")

    d = as_dict(analyse_terminal(BAD_TERMINAL_NO_ANCHOR))
    expect(len(d) == 1 and not d["U5a LaunchedEffect(pendingUrl) block present"],
           "BAD_TERMINAL_NO_ANCHOR must fail-closed on U5a")

    # KDoc quoting the anchor must not satisfy the criterion (comments stripped)
    d = as_dict(analyse_terminal(GOOD_TERMINAL_COMMENT_ONLY))
    expect(not d["U5a LaunchedEffect(pendingUrl) block present"],
           "comment-only anchor must NOT read as green (comments must be stripped)")

    if failures:
        print("OPEN_URL_BROKER_SCHEME_SELFTEST=FAIL")
        for f in failures:
            print("  selftest: %s" % f)
        return 1
    print("OPEN_URL_BROKER_SCHEME_SELFTEST=PASS")
    return 0


def main():
    argv = [a for a in sys.argv[1:]]
    if "--selftest" in argv:
        sys.exit(selftest())
    root = argv[0] if argv else "."
    path = os.path.join(root, BROKER_REL)
    if not os.path.isfile(path):
        print("gate: broker file missing: %s" % path)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        code = f.read()
    results = analyse(code)
    tpath = os.path.join(root, TERMINAL_REL)
    if os.path.isfile(tpath):
        with open(tpath, "r", encoding="utf-8") as f:
            results = results + analyse_terminal(f.read())
    else:
        print("  [--] U5 skipped — %s not staged in this tree" % TERMINAL_REL)
    failed = [(c, d) for c, ok, d in results if not ok]
    for c, ok, d in results:
        print("  [%s] %s%s" % ("ok" if ok else "RED", c,
                               "" if ok else " — %s" % d))
    if failed:
        print("open_url_broker_scheme_check: %d violation(s)" % len(failed))
        sys.exit(1)
    print("open_url_broker_scheme_check: all %d criteria green" % len(results))
    sys.exit(0)


if __name__ == "__main__":
    main()

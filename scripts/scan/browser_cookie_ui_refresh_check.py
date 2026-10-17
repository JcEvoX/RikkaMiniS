#!/usr/bin/env python3
"""[fix/da1022-G] browser_cookie_ui_refresh_check

Issue-07 gate: BrowserSettingsSheet cookie section must
  1. never call the callback-less `removeAllCookies(null)` (no flush, no refresh);
  2. use the callback form `removeAllCookies { ... }` with a `flush()` inside;
  3. cover BOTH http and https variants in the per-domain delete;
  4. carry a refresh signal (cookieRefreshTick) wired into remember keys and
     bumped at the per-domain and clear-all actions.

Code anchors only — comments are stripped before judgment, so a KDoc that
mentions `removeAllCookies(null)` does not trip the gate.

Usage: python3 browser_cookie_ui_refresh_check.py <repo_root>
       python3 browser_cookie_ui_refresh_check.py --root <repo_root>
       python3 browser_cookie_ui_refresh_check.py --selftest
Exit 0 = pass, 1 = violation, 2 = usage error.
"""
import sys

TARGET = "src/android/app/src/main/java/com/rikkaminis/app/ui/browser/BrowserSettingsSheet.kt"
MARKER = "BROWSER_COOKIE_UI_REFRESH_SELFTEST=PASS"


def strip_line_comments(src: str) -> str:
    """Remove // line comments, honouring string literals (URLs like
    https://... must survive). Does not attempt /* */ handling: this file
    uses line comments only for the strings the gate anchors on."""
    out_lines = []
    for line in src.split("\n"):
        in_str = False
        i = 0
        cut = len(line)
        while i < len(line):
            c = line[i]
            if c == "\\" and in_str:
                i += 2
                continue
            if c == '"':
                in_str = not in_str
            elif c == "/" and not in_str and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
            i += 1
        out_lines.append(line[:cut])
    return "\n".join(out_lines)


def brace_slice(src: str, start_line_idx: int, open_line: str) -> str:
    """From the line containing the first `{` of open_line, take lines until
    the brace opened there is balanced (inclusive of the closing line)."""
    lines = src.split("\n")
    depth = 0
    started = False
    buf = []
    i = start_line_idx
    while i < len(lines):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
        buf.append(lines[i])
        if started and depth <= 0:
            break
        i += 1
    return "\n".join(buf)


def slice_from_marker(src: str, marker: str) -> str:
    lines = src.split("\n")
    for idx, line in enumerate(lines):
        if marker in line:
            return brace_slice(src, idx, line)
    return ""


def check(source: str) -> list:
    src = strip_line_comments(source)
    errs = []

    # 1. callback-less removeAllCookies(null) must be gone
    if "removeAllCookies(null)" in src:
        errs.append("callback-less removeAllCookies(null) still present")

    # 2. callback form with flush() inside
    cb = slice_from_marker(src, "removeAllCookies {")
    if not cb:
        errs.append("no callback form removeAllCookies { ... } found")
    elif "flush()" not in cb:
        errs.append("removeAllCookies callback does not flush()")

    # 3. per-domain delete covers both schemes
    cand = slice_from_marker(src, "candidateUrls")
    if not cand:
        errs.append("per-domain delete has no candidateUrls block")
    else:
        if '"https://$domain"' not in cand:
            errs.append("per-domain delete misses the https://$domain variant")
        if '"http://$domain"' not in cand:
            errs.append("per-domain delete misses the http://$domain variant")

    # 4. refresh signal wired
    if "cookieRefreshTick" not in src:
        errs.append("no cookieRefreshTick refresh signal")
    else:
        if "remember(cookieRefreshTick)" not in src:
            errs.append("cookieRefreshTick not used as a remember key")
        bumps = src.count("cookieRefreshTick++")
        if bumps < 2:
            errs.append(f"cookieRefreshTick++ bumped {bumps} times, expected >= 2 (per-domain + clear-all)")

    return errs


def selftest() -> int:
    compliant = """
            val hasCookies = remember(cookieRefreshTick) { cookieManager.hasCookies() }
            val cm = CookieManager.getInstance()
            cm.removeAllCookies {
                cm.flush()
                cookieRefreshTick++
            }
            showClearConfirm = false
            cookieRefreshTick++
    """
    good_delete = """
            val cm = CookieManager.getInstance()
            val candidateUrls = if (domain.startsWith("http")) {
                listOf(domain)
            } else {
                listOf("https://$domain", "http://$domain")
            }
            for (url in candidateUrls) { names.forEach { cm.setCookie(url, "$it=; Max-Age=0; Path=/") } }
            cm.flush()
            cookieRefreshTick++
    """
    bad_nocallback = "CookieManager.getInstance().removeAllCookies(null)"
    bad_one_variant = """
            val url = if (domain.startsWith("http")) domain else "https://$domain"
            val names = cm.getCookie(url).orEmpty().split(';')
            names.forEach { cm.setCookie(url, "$it=; Max-Age=0; Path=/") }
            cm.flush()
    """
    bad_noflush = """
            cm.removeAllCookies { cookieRefreshTick++ }
    """
    bad_notick = """
            cm.removeAllCookies { cm.flush() }
            cm.flush()
    """
    # the comment trap: a KDoc mentioning removeAllCookies(null) must NOT trip
    good_with_comment = """
            // [fix/da1022-G] removeAllCookies(null) left no flush and no refresh:
            // use the callback form to persist + bump the tick.
    """ + compliant + good_delete

    arms = [
        ("compliant", good_with_comment, []),
        ("V1 callback-less removeAllCookies(null)", bad_nocallback, 1),
        ("V2 single scheme variant", bad_one_variant, 1),
        ("V3 no flush in callback", bad_noflush, 1),
        ("V4 no refresh tick", bad_notick, 1),
    ]
    failures = 0
    for name, sample, expect in arms:
        errs = check(sample)
        if expect == [] and errs:
            print(f"  FAIL {name}: expected green, got {errs}")
            failures += 1
        elif expect != [] and not errs:
            print(f"  FAIL {name}: expected red, got green")
            failures += 1
        else:
            print(f"  OK {name}: {errs if errs else 'green'}")
    if failures:
        print(f"SELFTEST FAILED ({failures} arms)")
        return 1
    print(MARKER)
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if "--selftest" in args:
        return selftest()
    root = None
    if "--root" in args:
        i = args.index("--root")
        if i + 1 >= len(args):
            print("usage: --root <repo_root>", file=sys.stderr)
            return 2
        root = args[i + 1]
    elif len(args) == 1:
        root = args[0]
    else:
        print("usage: <repo_root> | --root <repo_root> | --selftest", file=sys.stderr)
        return 2
    import os
    path = os.path.join(root, TARGET)
    try:
        src = open(path).read()
    except OSError as e:
        print(f"cannot read {path}: {e}", file=sys.stderr)
        return 2
    errs = check(src)
    if errs:
        print(f"browser_cookie_ui_refresh_check RED on {TARGET}:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print(f"browser_cookie_ui_refresh_check OK ({TARGET})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

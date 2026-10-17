#!/usr/bin/env python3
"""deep-audit-1019 ISSUE-08 (P1) — browser teardown wake-up + data:-URL payload gate.

Two silent stalls / one silent corruption, all inside
browser/BrowserUseManager.kt:

1. `destroy()` completed neither in-flight waiter. It cleared the file-chooser
   pins only, then swapped the client, removed and destroyed the WebView. After
   that no `onPageFinished` and no `__minis__.resolve`/`.reject` callback can
   ever arrive again, so `navigate()` / `reloadAndWait()` and `executeJS()` /
   `fetch()` each sat out their full 30s / 60s timeout and then reported a
   network timeout / JS timeout for a tab that no longer exists.
2. `onRendererGone` had woken the navigation waiter for a long time but not the
   async-JS waiter sitting next to it — same dead-renderer stall, same 30s/60s.
3. `deliverBlobDownload` decoded a non-base64 `data:` payload with a bare
   `java.net.URLDecoder.decode`, i.e. FORM semantics ('+' -> ' '), corrupting
   every such download that contained a literal '+': `data:text/plain,1+1%3D2`
   landed on disk as `1 1=2`. The sibling decoders in WebDavClient and
   OAuthCallbackServer already pre-escape the '+'.

This gate pins, on the comment-stripped source:

  * `destroy()` wakes BOTH waiters (`navigationDeferred?.complete(` and
    `asyncJsSlot.takeCurrent()`);
  * `onRendererGone` wakes the async-JS waiter too;
  * `AsyncJsSlot.takeCurrent()` still exists (the teardown entry point);
  * EVERY `URLDecoder.decode(` call site under src/android/app/src/main/java
    carries the `.replace("+", "%2B")` escape inside a +/- window (no
    allowlist: the two pre-existing call sites already comply, so any new
    bare call site is a regression by construction).

Usage: python3 browser_destroy_wakeup_check.py [ROOT]   (ROOT = repo root, default ".")
       python3 browser_destroy_wakeup_check.py --selftest
Exit:  0 = gate green, 1 = violations found (or selftest failures).
"""
import os
import re
import sys

MANAGER = "src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserUseManager.kt"
SLOT = "src/android/app/src/main/java/com/rikkaminis/app/browser/AsyncJsSlot.kt"
SRC_ROOT = "src/android/app/src/main/java"

DESTROY_MUST = (
    ("destroy() does not wake the navigation waiter", "navigationDeferred?.complete("),
    ("destroy() does not wake the async-JS waiter", "asyncJsSlot.takeCurrent()"),
)
RENDERER_MUST = (
    ("onRendererGone does not wake the async-JS waiter", "asyncJsSlot.takeCurrent()"),
)
SLOT_MUST = "fun takeCurrent()"

CALL_RE = re.compile(r"URLDecoder\.decode\(")
ESCAPE = '.replace("+", "%2B")'
BACK = 2  # lines of context before a call site
FWD = 4   # lines after (multi-line call / chained .toByteArray())


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def function_body(text, name):
    """Body of `fun <name>(...)` — brace-balanced, string literals skipped."""
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
    j = text.find("{", i)
    if j < 0:
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
                return text[j:k + 1]
        k += 1
    return None


def analyse_manager(manager_src):
    """-> [(criterion, ok, detail)] for the two teardown bodies."""
    doc = strip_comments(manager_src)
    out = []
    destroy = function_body(doc, "destroy")
    for crit, needle in DESTROY_MUST:
        if destroy is None:
            out.append((crit, False, "destroy() body not found"))
        else:
            out.append((crit, needle in destroy, "destroy() body"))
    onboard = function_body(doc, "onRendererGone")
    for crit, needle in RENDERER_MUST:
        if onboard is None:
            out.append((crit, False, "onRendererGone() body not found"))
        else:
            out.append((crit, needle in onboard, "onRendererGone() body"))
    return out


def scan_lines(name, src):
    """-> [(name, lineno)] for every bare form-oriented decode call site."""
    bad = []
    lines = strip_comments(src).split("\n")
    for i, line in enumerate(lines):
        if not CALL_RE.search(line):
            continue
        window = "\n".join(lines[max(0, i - BACK):i + FWD + 1])
        if ESCAPE not in window:
            bad.append((name, i + 1))
    return bad


def url_decode_violations(root):
    """-> [(relpath, lineno)] for every bare form-oriented decode call site."""
    bad = []
    base = os.path.join(root, SRC_ROOT)
    for dirpath, _dirs, files in os.walk(base):
        for fname in sorted(files):
            if not fname.endswith(".kt"):
                continue
            path = os.path.join(dirpath, fname)
            bad += scan_lines(
                os.path.relpath(path, root), open(path, encoding="utf-8").read()
            )
    return bad


def analyse_slot(slot_src):
    doc = strip_comments(slot_src)
    return [(
        "AsyncJsSlot.takeCurrent() teardown entry point is gone",
        SLOT_MUST in doc,
        "AsyncJsSlot.kt",
    )]


# --- fixtures ----------------------------------------------------------------
GOOD_MANAGER = """
    fun destroy() {
        runCatching { pendingFileChooser?.onReceiveValue(null) }
        pendingFileChooser = null
        navigationDeferred?.complete(NavigationResult(NavigationOutcome.FAILED))
        navigationDeferred = null
        asyncJsSlot.takeCurrent()?.complete("{\\"error\\":\\"gone\\"}")
        webView.destroy()
    }
    private val x = object : SafeWebViewClient() {
        override fun onRendererGone(view: WebView?) {
            navigationDeferred?.complete(NavigationResult(NavigationOutcome.FAILED))
            asyncJsSlot.takeCurrent()?.complete("{\\"error\\":\\"killed\\"}")
        }
    }
"""

BAD_DESTROY_NAV = GOOD_MANAGER.replace("navigationDeferred?.complete(NavigationResult(NavigationOutcome.FAILED))\n        navigationDeferred = null\n        asyncJsSlot", "asyncJsSlot")
BAD_DESTROY_JS = GOOD_MANAGER.replace('asyncJsSlot.takeCurrent()?.complete("{\\"error\\":\\"gone\\"}")\n', "")
BAD_RENDERER_JS = GOOD_MANAGER.replace('asyncJsSlot.takeCurrent()?.complete("{\\"error\\":\\"killed\\"}")\n', "")


def selftest():
    fails = []

    def expect(label, got_ok, want_ok, crit_filter=None):
        if got_ok != want_ok:
            fails.append(label)
            print("  [FAIL] %s -> ok=%s (want %s)" % (label, got_ok, want_ok))
        else:
            print("  [OK] %s -> ok=%s" % (label, got_ok))

    def manager_ok(src, crit_substr):
        res = analyse_manager(src)
        hit = [ok for c, ok, _ in res if crit_substr in c]
        if len(hit) != 1:
            return None
        return hit[0]

    # 1-3: the three teardown wake-ups, each removed in turn.
    expect("clean fixture: destroy wakes navigation", manager_ok(GOOD_MANAGER, "navigation waiter"), True)
    expect("clean fixture: destroy wakes async-JS", manager_ok(GOOD_MANAGER, "destroy() does not wake the async-JS"), True)
    expect("clean fixture: onRendererGone wakes async-JS", manager_ok(GOOD_MANAGER, "onRendererGone does not"), True)
    expect("breaker: destroy without navigation wake", manager_ok(BAD_DESTROY_NAV, "navigation waiter"), False)
    expect("breaker: destroy without async-JS wake", manager_ok(BAD_DESTROY_JS, "destroy() does not wake the async-JS"), False)
    expect("breaker: onRendererGone without async-JS wake", manager_ok(BAD_RENDERER_JS, "onRendererGone does not"), False)

    # 4: the gate must not be satisfied by a comment that merely mentions it.
    commented = GOOD_MANAGER.replace("navigationDeferred?.complete(", "// navigationDeferred?.complete(")
    expect("breaker: navigation wake commented out", manager_ok(commented, "navigation waiter"), False)

    # 5: the escape check (real call into scan_lines, not a sanity string).
    good_escape = 'val s = URLDecoder.decode(p.replace("+", "%2B"), "UTF-8")'
    bad_escape = 'val s = URLDecoder.decode(p, "UTF-8")'
    multi_good = 'val s = URLDecoder.decode(\n    p.replace("+", "%2B"),\n    "UTF-8",\n)'
    multi_bad = 'val s = URLDecoder.decode(\n    p,\n    "UTF-8",\n)'
    expect("clean fixture: escaped call site", scan_lines("f.kt", good_escape) == [], True)
    expect("clean fixture: escape on a continuation line", scan_lines("f.kt", multi_good) == [], True)
    expect("breaker: bare call site", scan_lines("f.kt", bad_escape) == [], False)
    expect("breaker: bare call across lines", scan_lines("f.kt", multi_bad) == [], False)
    expect(
        "precision: a comment mentioning a bare call is not a call",
        scan_lines("f.kt", 'val x = 1 // URLDecoder.decode(p, "UTF-8")') == [],
        True,
    )
    expect(
        "precision: a bare call inside a KDoc block is not a call",
        scan_lines("f.kt", "/** old shape was URLDecoder.decode(p, \"UTF-8\") */\nval y = 2") == [],
        True,
    )

    # 7: slot entry point.
    expect("clean fixture: takeCurrent present", analyse_slot("fun takeCurrent(): T? { return null }")[0][1], True)
    expect("breaker: takeCurrent removed", analyse_slot("fun finish(): Boolean { return false }")[0][1], False)

    return fails


def main():
    if "--selftest" in sys.argv:
        fails = selftest()
        if fails:
            print("SELFTEST FAIL (%d)" % len(fails))
            return 1
        print("BROWSER_DESTROY_WAKEUP_SELFTEST=PASS")
        return 0

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = args[0] if args else "."
    manager_path = os.path.join(root, MANAGER)
    slot_path = os.path.join(root, SLOT)
    for p in (manager_path, slot_path):
        if not os.path.isfile(p):
            print("FAIL %s not found under root %s" % (p, root))
            return 1

    results = list(analyse_manager(open(manager_path, encoding="utf-8").read()))
    results += list(analyse_slot(open(slot_path, encoding="utf-8").read()))

    violations = url_decode_violations(root)
    results.append((
        "a URLDecoder.decode() call site lacks the '.replace(\"+\", \"%2B\")' escape",
        not violations,
        "src/android/app/src/main/java",
    ))

    failed = [(c, d) for c, ok, d in results if not ok]
    for crit, ok, detail in results:
        if ok:
            print("  [OK] %s" % crit)
        else:
            print("  [FAIL] %s (%s)" % (crit, detail))
    for rel, lineno in violations:
        print("  [FAIL] %s:%d bare URLDecoder.decode() — '+' would be decoded as a space" % (rel, lineno))

    print("Summary: %d passed, %d failed" % (len(results) - len(failed), len(failed)))
    if failed:
        print("browser teardown wake-up gate: RED — the ISSUE-08 contract is violated")
        return 1
    print("GATE=GREEN browser_destroy_wakeup")
    return 0


if __name__ == "__main__":
    sys.exit(main())

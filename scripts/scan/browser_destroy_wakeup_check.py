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
HOLDER = "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebViewHolder.kt"
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

# ── [deep-audit-1045 B-02] WebView lifecycle contract ────────────────────────
# Decision points, not spellings: a View-queue post on the manager's own
# WebView; the function that ARMS the one-shot blob bridge credential; any
# runtime UA writer that reloads a live page; both destroy() bodies.
VIEW_POST_RE = re.compile(r"(?<![\w.])\w*webView\.post(?:Delayed)?\s*[({]")
ARM_RE = re.compile(r"blobDownloadActiveToken\s*=\s*(?!null)\w")
UA_WRITE = "settings.userAgentString"
DETACH_TOKENS = ("removeView(", "detach(")
CLEAR_CALLBACKS = "removeCallbacks(null)"
TEARDOWN = "webView.destroy()"
LATCH = "destroyed"

CALL_RE = re.compile(r"URLDecoder\.decode\(")
ESCAPE = '.replace("+", "%2B")'
BACK = 2  # lines of context before a call site
FWD = 4   # lines after (multi-line call / chained .toByteArray())


def skip_string(text, i):
    """Index just past the string literal starting at `text[i]` (a quote).

    Handles Kotlin raw strings (three quotes ... three quotes), which may hold a
    single quote or two consecutive quotes. The pre-existing simple-string scan
    stopped at the first quote inside a raw string and therefore swallowed whole
    function bodies, so every derived criterion below read the wrong text.
    """
    if text.startswith('"""', i):
        j = text.find('"""', i + 3)
        return len(text) if j < 0 else j + 3
    q = text[i]
    i += 1
    while i < len(text) and text[i] != q:
        if text[i] == "\\":
            i += 2
        elif text.startswith("${", i):
            # string template: "${…}" may carry its own string literals
            # ("${if (x) \"a\" else \"b\"}"), which would otherwise close this
            # string early and swallow a brace.
            i = skip_template(text, i + 2)
        else:
            i += 1
    return i + 1


def skip_template(text, i):
    """Index just past the `}` matching an already-consumed `${`."""
    depth = 1
    while i < len(text):
        c = text[i]
        if c == '"' or c == "'":
            i = skip_string(text, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def strip_comments(src):
    """Remove line + block comments, leaving string literals intact."""
    out = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            j = skip_string(src, i)
            out.append(src[i:j])
            i = j
        elif src.startswith("//", i):
            # Blank the comment but KEEP its newline: every line number the
            # criteria report must be a real line in the original file.
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            end = n if j < 0 else j + 2
            out.append("\n" * src.count("\n", i, end))
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out)


def function_body(text, name):
    """Body of `fun <name>(...)` — brace-balanced, string literals skipped."""
    m = re.search(r"\bfun\s+" + re.escape(name) + r"\s*\(", text)
    if not m:
        return None
    i = text.find("(", m.end() - 1)
    depth = 0
    while i < len(text):
        c = text[i]
        if c == '"' or c == "'":
            i = skip_string(text, i)
            continue
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
    body, _end = brace_body(text, j)
    return body


def brace_body(text, start):
    """Balanced `{...}` starting at `start` (a '{'); string literals skipped.
    -> (body including braces, index just past the closing brace)."""
    depth = 0
    k = start
    while k < len(text):
        c = text[k]
        if c == '"' or c == "'":
            k = skip_string(text, k)
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:k + 1], k + 1
        k += 1
    return text[start:], len(text)


FUN_HEAD_RE = re.compile(r"\bfun\s+(?:<[^>\n]*>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\(")


def iter_functions(text):
    """-> [(name, body)] for every BLOCK-bodied `fun` in `text`.

    Derived, not listed: the criteria below never name the functions they
    police, so renaming (or adding) a UA writer / credential arming site is
    covered on the day it lands. Expression-bodied functions carry no block and
    are skipped — every site these criteria care about is block-bodied.
    """
    out = []
    pos = 0
    while True:
        m = FUN_HEAD_RE.search(text, pos)
        if not m:
            return out
        i = m.end() - 1
        depth = 0
        while i < len(text):
            c = text[i]
            if c == '"' or c == "'":
                i = skip_string(text, i)
                continue
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        j = i + 1
        while j < len(text) and text[j] not in "{=":
            j += 1
        if j >= len(text):
            return out
        if text[j] == "=":
            pos = j + 1
            continue
        body, end = brace_body(text, j)
        out.append((m.group(1), body))
        pos = end


def analyse_lifecycle(manager_src, holder_src):
    """[deep-audit-1045 B-02] -> [(criterion, ok, detail)] for the WebView
    lifecycle contract: injected-script dispatch, runtime UA writers, and the
    teardown callback drain in BOTH owners."""
    mdoc = strip_comments(manager_src)
    hdoc = strip_comments(holder_src)
    mfns = iter_functions(mdoc)
    hfns = iter_functions(hdoc)
    out = []

    # C1 — the blob: download injection. Arms a one-shot bridge credential and
    # then injects a script: both halves must be there, the latch must gate
    # them, and the dispatch must not be a View-queue post (the pre-fix shape;
    # a detached pool tab never drains that queue, so the FileReader never runs
    # and the download is silently lost).
    armed = [(n, b) for n, b in mfns if ARM_RE.search(b)]
    if not armed:
        out.append((
            "no function arms the one-shot blob bridge credential (download path gone?)",
            False,
            "BrowserUseManager.kt",
        ))
    for name, body in armed:
        out.append((
            "the blob-download injection (%s) is not gated by the destroyed latch" % name,
            LATCH in body,
            "BrowserUseManager.kt %s()" % name,
        ))
        out.append((
            "the blob-download injection (%s) does not dispatch the script" % name,
            "evaluateJavascript(" in body,
            "BrowserUseManager.kt %s()" % name,
        ))
        out.append((
            "the blob-download injection (%s) goes through the WebView's View post queue" % name,
            VIEW_POST_RE.search(body) is None,
            "BrowserUseManager.kt %s()" % name,
        ))

    # C2 — structural: the manager's own WebView must never be scheduled through
    # its View queue at all. `View.post` on a detached view parks the runnable in
    # HandlerActionQueue until a later attach; pool tabs never attach.
    hits = [mdoc[:m.start()].count("\n") + 1 for m in VIEW_POST_RE.finditer(mdoc)]
    out.append((
        "the manager schedules work on its own WebView through the View post queue",
        not hits,
        "BrowserUseManager.kt line(s) %s" % hits if hits else "no webView.post/postDelayed",
    ))

    # C3 — every RUNTIME UA switch (writes the UA *and* reloads a live page)
    # must consult the latch first. Construction-time configuration is exempt by
    # construction: it does not reload, so `configureWebView` stays untouched.
    writers = [
        (label, name, body)
        for label, fns in (("BrowserUseManager.kt", mfns), ("WebViewHolder.kt", hfns))
        for name, body in fns
        if UA_WRITE in body and "reload(" in body
    ]
    if not writers:
        out.append((
            "no runtime user-agent writer found (UA switch path gone?)",
            False,
            "BrowserUseManager.kt + WebViewHolder.kt",
        ))
    for label, name, body in writers:
        out.append((
            "the user-agent switch (%s) writes settings/reload without the destroyed latch" % name,
            LATCH in body,
            "%s %s()" % (label, name),
        ))

    # C4 — both destroy() bodies drop this view's queued callbacks BEFORE
    # destroy(), and only after the view is detached (a cleared attachInfo is
    # what keeps `removeCallbacks(null)` from draining the shared window
    # handler's null-token messages).
    for label, fns in (("BrowserUseManager.kt", mfns), ("WebViewHolder.kt", hfns)):
        destroys = [b for n, b in fns if n == "destroy"]
        if not destroys:
            out.append(("%s has no destroy() body" % label, False, label))
            continue
        for body in destroys:
            rb = body.find(CLEAR_CALLBACKS)
            td = body.find(TEARDOWN)
            detach = min([i for i in (body.find(t) for t in DETACH_TOKENS) if i >= 0] or [-1])
            out.append((
                "%s destroy() does not call webView.removeCallbacks(null) before webView.destroy()" % label,
                rb >= 0 and td >= 0 and rb < td,
                "%s destroy() removeCallbacks=%d destroy=%d" % (label, rb, td),
            ))
            out.append((
                "%s destroy() clears view callbacks before detaching the view" % label,
                rb >= 0 and detach >= 0 and detach < rb,
                "%s destroy() detach=%d removeCallbacks=%d" % (label, detach, rb),
            ))
    return out


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


# --- [deep-audit-1045 B-02] lifecycle fixtures -------------------------------
# Shapes mirror the real files: the blob injection arms a one-shot credential
# then dispatches; the UA switch writes and reloads; destroy detaches, stops,
# drops this view's queue, destroys.
GOOD_LIFECYCLE_MANAGER = """
    private fun fetchBlobDownload(url: String) {
        if (destroyed) {
            return
        }
        blobDownloadActiveToken = token
        runJsOnMain("blob-download") { webView.evaluateJavascript(js, null) }
    }

    fun setUserAgent(profile: UserAgentProfile, customUA: String? = null) {
        if (destroyed) {
            return
        }
        webView.settings.userAgentString = ua
        webView.reload()
    }

    fun destroy() {
        (webView.parent as? ViewGroup)?.removeView(webView)
        webView.stopLoading()
        webView.removeCallbacks(null)
        webView.destroy()
    }
"""

GOOD_LIFECYCLE_HOLDER = """
    fun toggleDesktopMode() {
        if (destroyed.get()) {
            return
        }
        webView.settings.userAgentString = DESKTOP_UA
        webView.reload()
    }

    fun destroy() {
        if (!destroyed.compareAndSet(false, true)) return
        detach()
        webView.stopLoading()
        webView.removeCallbacks(null)
        webView.destroy()
    }
"""


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

    # ── [deep-audit-1045 B-02] lifecycle contract ────────────────────────────
    # Every arm states its EXPECTED RED SET up front (hand-written from the
    # criterion table, never derived from a run): an arm that reddens a
    # neighbouring criterion fails the selftest, which is what keeps "one arm
    # per criterion" honest.
    def expect_red(label, mgr, holder, want):
        got = [c for c, ok, _ in analyse_lifecycle(mgr, holder) if not ok]
        for w in want:
            if not any(w in c for c in got):
                fails.append(label + " | missing red: " + w)
                print("  [FAIL] %s -> no red for %r" % (label, w))
        for c in got:
            if not any(w in c for w in want):
                fails.append(label + " | unexpected red: " + c)
                print("  [FAIL] %s -> unexpected red %r" % (label, c))
        if all(any(w in c for c in got) for w in want) and all(any(w in c for w in want) for c in got):
            print("  [OK] %s -> red set %d" % (label, len(got)))

    expect_red("clean fixture: lifecycle contract", GOOD_LIFECYCLE_MANAGER, GOOD_LIFECYCLE_HOLDER, [])

    viewpost = GOOD_LIFECYCLE_MANAGER.replace(
        'runJsOnMain("blob-download") { webView.evaluateJavascript(js, null) }',
        "webView.post { webView.evaluateJavascript(js, null) }",
    )
    expect_red(
        "breaker: the injection is a View-queue post again",
        viewpost,
        GOOD_LIFECYCLE_HOLDER,
        ["goes through the WebView's View post queue", "schedules work on its own WebView"],
    )

    no_arm = GOOD_LIFECYCLE_MANAGER.replace("        blobDownloadActiveToken = token\n", "")
    expect_red(
        "breaker: nothing arms the blob bridge credential",
        no_arm,
        GOOD_LIFECYCLE_HOLDER,
        ["no function arms the one-shot blob bridge credential"],
    )

    armed_unlatched = GOOD_LIFECYCLE_MANAGER.replace(
        "    private fun fetchBlobDownload(url: String) {\n        if (destroyed) {\n            return\n        }\n",
        "    private fun fetchBlobDownload(url: String) {\n",
    )
    expect_red(
        "breaker: the blob injection drops the destroyed latch",
        armed_unlatched,
        GOOD_LIFECYCLE_HOLDER,
        ["is not gated by the destroyed latch"],
    )

    commented_latch = GOOD_LIFECYCLE_MANAGER.replace(
        "        if (destroyed) {\n            return\n        }\n        blobDownloadActiveToken = token",
        "        // if (destroyed) { return }\n        blobDownloadActiveToken = token",
    )
    expect_red(
        "precision: a commented-out latch is not a latch",
        commented_latch,
        GOOD_LIFECYCLE_HOLDER,
        ["is not gated by the destroyed latch"],
    )

    ua_unlatched = GOOD_LIFECYCLE_MANAGER.replace(
        "    fun setUserAgent(profile: UserAgentProfile, customUA: String? = null) {\n        if (destroyed) {\n            return\n        }\n",
        "    fun setUserAgent(profile: UserAgentProfile, customUA: String? = null) {\n",
    )
    expect_red(
        "breaker: the manager UA switch drops the latch",
        ua_unlatched,
        GOOD_LIFECYCLE_HOLDER,
        ["user-agent switch (setUserAgent) writes"],
    )

    holder_unlatched = GOOD_LIFECYCLE_HOLDER.replace(
        "    fun toggleDesktopMode() {\n        if (destroyed.get()) {\n            return\n        }\n",
        "    fun toggleDesktopMode() {\n",
    )
    expect_red(
        "breaker: the holder UA switch drops the latch",
        GOOD_LIFECYCLE_MANAGER,
        holder_unlatched,
        ["user-agent switch (toggleDesktopMode) writes"],
    )

    construct_only = GOOD_LIFECYCLE_MANAGER.replace(
        "    fun setUserAgent(profile: UserAgentProfile, customUA: String? = null) {",
        "    fun configureWebView(webView: WebView) {\n"
        "        webView.settings.userAgentString = ua\n"
        "    }\n\n"
        "    fun setUserAgent(profile: UserAgentProfile, customUA: String? = null) {",
    )
    expect_red(
        "precision: a construction-time UA write needs no latch (it does not reload)",
        construct_only,
        GOOD_LIFECYCLE_HOLDER,
        [],
    )

    mgr_teardown = GOOD_LIFECYCLE_MANAGER.replace("        webView.removeCallbacks(null)\n", "")
    expect_red(
        "breaker: the manager destroy() drops the queued callbacks",
        mgr_teardown,
        GOOD_LIFECYCLE_HOLDER,
        [
            "BrowserUseManager.kt destroy() does not call webView.removeCallbacks(null)",
            "BrowserUseManager.kt destroy() clears view callbacks before detaching",
        ],
    )

    holder_teardown = GOOD_LIFECYCLE_HOLDER.replace("        webView.removeCallbacks(null)\n", "")
    expect_red(
        "breaker: the holder destroy() drops the queued callbacks",
        GOOD_LIFECYCLE_MANAGER,
        holder_teardown,
        [
            "WebViewHolder.kt destroy() does not call webView.removeCallbacks(null)",
            "WebViewHolder.kt destroy() clears view callbacks before detaching",
        ],
    )

    early_clear = GOOD_LIFECYCLE_MANAGER.replace(
        "        (webView.parent as? ViewGroup)?.removeView(webView)\n"
        "        webView.stopLoading()\n"
        "        webView.removeCallbacks(null)\n",
        "        webView.removeCallbacks(null)\n"
        "        (webView.parent as? ViewGroup)?.removeView(webView)\n"
        "        webView.stopLoading()\n",
    )
    expect_red(
        "breaker: callbacks cleared before the view is detached (shared handler drain)",
        early_clear,
        GOOD_LIFECYCLE_HOLDER,
        ["BrowserUseManager.kt destroy() clears view callbacks before detaching"],
    )

    # baseline shape: the pre-fix tree, criterion by criterion.
    baseline_mgr = """
    private fun fetchBlobDownload(url: String) {
        blobDownloadActiveToken = token
        webView.post { webView.evaluateJavascript(js, null) }
    }

    fun setUserAgent(profile: UserAgentProfile, customUA: String? = null) {
        webView.settings.userAgentString = ua
        webView.reload()
    }

    fun destroy() {
        (webView.parent as? ViewGroup)?.removeView(webView)
        webView.stopLoading()
        webView.destroy()
    }
"""
    baseline_holder = """
    fun toggleDesktopMode() {
        webView.settings.userAgentString = DESKTOP_UA
        webView.reload()
    }

    fun destroy() {
        detach()
        webView.stopLoading()
        webView.destroy()
    }
"""
    expect_red(
        "baseline shape: every lifecycle criterion red",
        baseline_mgr,
        baseline_holder,
        [
            "is not gated by the destroyed latch",
            "goes through the WebView's View post queue",
            "schedules work on its own WebView",
            "user-agent switch (setUserAgent) writes",
            "user-agent switch (toggleDesktopMode) writes",
            "BrowserUseManager.kt destroy() does not call webView.removeCallbacks(null)",
            "BrowserUseManager.kt destroy() clears view callbacks before detaching",
            "WebViewHolder.kt destroy() does not call webView.removeCallbacks(null)",
            "WebViewHolder.kt destroy() clears view callbacks before detaching",
        ],
    )

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
    holder_path = os.path.join(root, HOLDER)
    slot_path = os.path.join(root, SLOT)
    for p in (manager_path, holder_path, slot_path):
        if not os.path.isfile(p):
            print("FAIL %s not found under root %s" % (p, root))
            return 1

    manager_src = open(manager_path, encoding="utf-8").read()
    holder_src = open(holder_path, encoding="utf-8").read()
    results = list(analyse_manager(manager_src))
    results += list(analyse_lifecycle(manager_src, holder_src))
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

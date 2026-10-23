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

[deep-audit-1046 S-07] — the offscreen AndroidView hosts (`ui/markdown/KaTeXView.kt`,
`ui/sandbox/FilePreviewScreen.kt`) and the shared teardown policy
(`ui/preview/WebViewReleaseLatch.kt`). Their `onRelease` used
`removeCallbacks(null)` — a documented no-op (AOSP guards on `action != null`) —
with no latch, so a `postDelayed` capture could draw on a destroyed WebView and
cache an EMPTY bitmap under a formula's key (blank for the process lifetime).
Criteria, all derived from code shape: the release path latches the view before
it touches it; it consumes the shared `releasePlan(…)` order; it removes the
tracked runnables by identity; `stopLoading` still precedes `destroy`; every
deferred block opens with the latch guard; every named runnable is registered.

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
# [deep-audit-1046 S-07] the two offscreen AndroidView hosts + the shared policy
KATEX_VIEW = "src/android/app/src/main/java/com/rikkaminis/app/ui/markdown/KaTeXView.kt"
FILE_PREVIEW = "src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/FilePreviewScreen.kt"
RELEASE_POLICY = "src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebViewReleaseLatch.kt"
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


# ── [deep-audit-1046 S-07] offscreen WebView release contract ────────────────
# Two `AndroidView` hosts (KaTeXView.kt / FilePreviewScreen.kt) schedule work on
# a WebView through its View queue and destroy it in `onRelease`. Their teardown
# called `removeCallbacks(null)` — a documented no-op (AOSP guards the method on
# `action != null`) — and their deferred bodies had no latch, so a capture block
# could draw on a destroyed WebView and cache an EMPTY bitmap under that
# formula's key. The criteria below are derived: the latch call, the removal of
# the tracked runnables, the teardown plan and the guard positions are all read
# from the code, never from a member list.
ONRELEASE_RE = re.compile(r"\bonRelease\s*=\s*\{")
DEFERRED_RE = re.compile(r"(?<![\w.])(?:post|postDelayed)\s*(?:\(\s*)?\{")
NAMED_RUNNABLE_RE = re.compile(r"\bval\s+([A-Za-z_]\w*)\s*=\s*Runnable\s*\{")
NAMED_REMOVE_RE = re.compile(r"removeCallbacks\(\s*(?!null\b)([^)\s])")
NULL_CLEAR = "removeCallbacks(null)"
DETACH_TOKENS_ANY = ("removeView(", "detach(")
POLICY_LISTS_RE = re.compile(r"listOf\(([^)]*)\)")
STEP_RE = re.compile(r"ReleaseStep\.([A-Z_]+)")

def lambda_body(text, brace_at):
    """`{...}` body at `brace_at` (the index of a '{'), or None."""
    if brace_at < 0 or brace_at >= len(text) or text[brace_at] != "{":
        j = text.find("{", max(brace_at, 0))
        if j < 0:
            return None
        brace_at = j
    body, _end = brace_body(text, brace_at)
    return body

def deferred_body_guarded(body):
    """True iff the body's FIRST statement is `if (<latch>…) return…`.

    Fail-closed by construction: the guard has to open the block. A guard placed
    after the work it was meant to stop is exactly the pre-fix shape with extra
    text.
    """
    inner = body[1:]
    i = 0
    while i < len(inner) and inner[i] in " \t\r\n":
        i += 1
    if not inner.startswith("if", i):
        return False
    j = inner.find("(", i)
    if j < 0:
        return False
    depth = 0
    k = j
    while k < len(inner):
        c = inner[k]
        if c == '"' or c == "'":
            k = skip_string(inner, k)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    cond = inner[j:k + 1]
    if "canRunDeferred(" not in cond and "isReleased" not in cond:
        return False
    return re.match(r"\s*return\b", inner[k + 1:k + 120]) is not None

def analyse_release_latch(sources):
    """-> [(criterion, ok, detail)] for the offscreen WebView release contract.

    `sources` = [(label, source)] — one entry per AndroidView host.
    """
    out = []
    for label, src in sources:
        doc = strip_comments(src)
        bodies = []
        for m in ONRELEASE_RE.finditer(doc):
            body = lambda_body(doc, m.end() - 1)
            if body:
                bodies.append(body)
        if not bodies:
            out.append((
                "%s has no AndroidView onRelease teardown (the renderer leaks)" % label,
                False,
                label,
            ))
            continue
        for body in bodies:
            latch = body.find(".release()")
            plan = body.find("releasePlan(")
            drop = body.find("removeCallbacks(")
            stop = body.find("stopLoading()")
            destroy = body.find("destroy()")
            touches = [i for i in (plan, drop, stop, destroy) if i >= 0]
            out.append((
                "%s release path touches the WebView before latching it" % label,
                latch >= 0 and touches and latch < min(touches),
                "%s onRelease latch=%d first-touch=%s"
                % (label, latch, min(touches) if touches else "none"),
            ))
            out.append((
                "%s release path does not use the derived teardown plan" % label,
                plan >= 0,
                "%s onRelease" % label,
            ))
            out.append((
                "%s release path does not remove the deferred work by identity" % label,
                NAMED_REMOVE_RE.search(body) is not None,
                "%s onRelease" % label,
            ))
            out.append((
                "%s release path no longer stops and destroys the WebView" % label,
                stop >= 0 and destroy >= 0 and stop < destroy,
                "%s onRelease stop=%d destroy=%d" % (label, stop, destroy),
            ))
            # Defensive: a bulk null-clear may only run once the view is detached
            # (that is the only ordering under which it even has a local meaning;
            # as written it removes nothing at all, which is why it is gone).
            null_clear = body.find(NULL_CLEAR)
            if null_clear >= 0:
                detach = min([i for i in (body.find(t) for t in DETACH_TOKENS_ANY) if i >= 0] or [-1])
                out.append((
                    "%s release path clears view callbacks with removeCallbacks(null) "
                    "before detaching" % label,
                    detach >= 0 and detach < null_clear,
                    "%s onRelease detach=%d null-clear=%d" % (label, detach, null_clear),
                ))

        # Every deferred block — inline `post { … }` and named
        # `val x = Runnable { … }` alike — must open with the latch guard.
        blocks = [(m.start(), lambda_body(doc, m.end() - 1)) for m in DEFERRED_RE.finditer(doc)]
        blocks += [(m.start(), lambda_body(doc, m.end() - 1)) for m in NAMED_RUNNABLE_RE.finditer(doc)]
        if not blocks:
            out.append((
                "%s schedules no deferred work on its WebView at all (capture/load path gone?)"
                % label,
                False,
                label,
            ))
        for pos, body in blocks:
            if body is None:
                continue
            line = doc[:pos].count("\n") + 1
            out.append((
                "%s deferred block at line %d does not stand down on release" % (label, line),
                deferred_body_guarded(body),
                "%s:%d" % (label, line),
            ))

        # A named runnable that is never registered can never be removed by the
        # teardown — the registration is half of the contract.
        for m in NAMED_RUNNABLE_RE.finditer(doc):
            name = m.group(1)
            line = doc[:m.start()].count("\n") + 1
            out.append((
                "%s deferred runnable %s (line %d) is never registered with the release latch"
                % (label, name, line),
                re.search(r"\.track\(\s*" + re.escape(name) + r"\s*\)", doc) is not None,
                "%s:%d" % (label, line),
            ))
    return out

def analyse_release_policy(src):
    """-> [(criterion, ok, detail)] for WebViewReleaseLatch.kt: the plan ORDER
    is the contract, and the two hosts cannot re-derive it locally."""
    doc = strip_comments(src)
    plans = []
    for m in POLICY_LISTS_RE.finditer(doc):
        steps = STEP_RE.findall(m.group(1))
        if steps:
            plans.append(steps)
    out = []
    out.append((
        "the release plan is not expressed as two driven step lists (attached / already detached)",
        len(plans) == 2,
        "WebViewReleaseLatch.kt plans=%d" % len(plans),
    ))
    if len(plans) == 2:
        out.append((
            "the attached-view plan does not detach first and destroy last",
            plans[0] == ["DETACH", "DROP_DEFERRED", "STOP_LOADING", "DESTROY"],
            "attached=%s" % plans[0],
        ))
        out.append((
            "the already-detached plan does not drop the detach step (or reorders the rest)",
            plans[1] == ["DROP_DEFERRED", "STOP_LOADING", "DESTROY"],
            "detached=%s" % plans[1],
        ))
    out.append((
        "WebViewReleaseLatch no longer exposes the deferred-work guard the hosts call",
        "fun canRunDeferred(" in doc and "fun release(" in doc and "fun track(" in doc,
        "WebViewReleaseLatch.kt",
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


# --- [deep-audit-1046 S-07] release-latch fixtures ---------------------------
# Shapes mirror the real files: a latch per WebView, both deferred bodies guarded,
# a named capture runnable registered and removed by identity, teardown driven by
# the shared plan.
GOOD_KATEX = """
        key(cacheKey, rendererEpoch) {
            val releaseLatch = remember { WebViewReleaseLatch() }
            AndroidView(
                factory = { ctx ->
                    WebView(ctx).apply {
                        addJavascriptInterface(object {
                            fun onRendered(width: Int, height: Int, error: String, token: Long) {
                                if (!releaseLatch.canRunDeferred()) return
                                post {
                                    if (!releaseLatch.canRunDeferred()) return@post
                                    layoutParams = ViewGroup.LayoutParams(bitmapW, bitmapH)
                                    val capture = Runnable {
                                        if (!releaseLatch.canRunDeferred()) return@Runnable
                                        renderedBitmap = bitmap
                                    }
                                    releaseLatch.track(capture)
                                    postDelayed(capture, 100)
                                }
                            }
                        }, "AndroidBridge")
                    }
                },
                onRelease = { wv ->
                    val outstanding = releaseLatch.release()
                    for (step in releasePlan(attached = wv.parent != null)) {
                        when (step) {
                            ReleaseStep.DETACH -> (wv.parent as? ViewGroup)?.removeView(wv)
                            ReleaseStep.DROP_DEFERRED -> for (task in outstanding) wv.removeCallbacks(task)
                            ReleaseStep.STOP_LOADING -> wv.stopLoading()
                            ReleaseStep.DESTROY -> wv.destroy()
                        }
                    }
                },
            )
        }
"""

GOOD_PREVIEW = """
    key(item.file, rendererEpoch) {
    val releaseLatch = remember { WebViewReleaseLatch() }
    AndroidView(
        factory = { ctx ->
            WebView(ctx).apply {
                val initialLoad = Runnable {
                    if (!releaseLatch.canRunDeferred()) return@Runnable
                    loadUrl(targetUrl)
                }
                releaseLatch.track(initialLoad)
                post(initialLoad)
            }
        },
        onRelease = { wv ->
            val outstanding = releaseLatch.release()
            for (step in releasePlan(attached = wv.parent != null)) {
                when (step) {
                    ReleaseStep.DETACH -> (wv.parent as? ViewGroup)?.removeView(wv)
                    ReleaseStep.DROP_DEFERRED -> for (task in outstanding) wv.removeCallbacks(task)
                    ReleaseStep.STOP_LOADING -> wv.stopLoading()
                    ReleaseStep.DESTROY -> wv.destroy()
                }
            }
        },
    )
"""

GOOD_POLICY = """
internal fun releasePlan(attached: Boolean): List<ReleaseStep> =
    if (attached) {
        listOf(ReleaseStep.DETACH, ReleaseStep.DROP_DEFERRED, ReleaseStep.STOP_LOADING, ReleaseStep.DESTROY)
    } else {
        listOf(ReleaseStep.DROP_DEFERRED, ReleaseStep.STOP_LOADING, ReleaseStep.DESTROY)
    }

internal class WebViewReleaseLatch {
    fun canRunDeferred(): Boolean = !released.get()
    fun track(task: Runnable) { }
    fun release(): List<Runnable> = emptyList()
}
"""

# The pre-fix shape, verbatim in structure: unguarded nested `post { postDelayed }`,
# a null-clear teardown, no latch.
BASE_KATEX = """
        key(cacheKey, rendererEpoch) {
            AndroidView(
                factory = { ctx ->
                    WebView(ctx).apply {
                        addJavascriptInterface(object {
                            fun onRendered(width: Int, height: Int, error: String, token: Long) {
                                post {
                                    layoutParams = ViewGroup.LayoutParams(bitmapW, bitmapH)
                                    postDelayed({
                                        draw(canvas)
                                        KaTeXRendererCache.cache.put(cacheKey, entry)
                                    }, 100)
                                }
                            }
                        }, "AndroidBridge")
                    }
                },
                onRelease = { wv ->
                    wv.removeCallbacks(null)
                    wv.stopLoading()
                    wv.destroy()
                },
            )
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

    # ── [deep-audit-1046 S-07] offscreen release latch ───────────────────────
    # Same discipline as `expect_red`: the EXPECTED red set is written out by
    # hand from the criterion table, never derived from a run.
    def expect_latch(label, katex, preview, policy, want, n=None):
        res = analyse_release_latch([
            ("KaTeXView.kt", katex),
            ("FilePreviewScreen.kt", preview),
        ])
        res += analyse_release_policy(policy)
        got = [c for c, ok, _ in res if not ok]
        missing = [w for w in want if not any(w in c for c in got)]
        unexpected = [c for c in got if not any(w in c for w in want)]
        bad = bool(missing or unexpected) or (n is not None and len(got) != n)
        if bad:
            fails.append(label)
            print(
                "  [FAIL] %s -> red set %d (want %s), missing=%r unexpected=%r"
                % (label, len(got), n if n is not None else len(want), missing, unexpected)
            )
        else:
            print("  [OK] %s -> red set %d" % (label, len(got)))

    expect_latch("clean fixture: release latch contract", GOOD_KATEX, GOOD_PREVIEW, GOOD_POLICY, [], 0)

    # PRE arm — the pre-fix shape: no latch, no plan, null-clear teardown, both
    # deferred blocks unguarded.
    expect_latch(
        "baseline shape: every release-latch criterion red",
        BASE_KATEX,
        GOOD_PREVIEW,
        GOOD_POLICY,
        [
            "touches the WebView before latching it",
            "does not use the derived teardown plan",
            "does not remove the deferred work by identity",
            "clears view callbacks with removeCallbacks(null) before detaching",
            "KaTeXView.kt deferred block",
        ],
        # 4 teardown criteria + 2 unguarded blocks (`post {` + `postDelayed({`)
        6,
    )

    # one arm per criterion, each reddening exactly one
    no_capture_guard = GOOD_KATEX.replace(
        "                                        if (!releaseLatch.canRunDeferred()) return@Runnable\n",
        "",
    )
    expect_latch(
        "breaker: the capture block drops its latch guard",
        no_capture_guard,
        GOOD_PREVIEW,
        GOOD_POLICY,
        ["KaTeXView.kt deferred block"],
        1,
    )

    late_capture_guard = GOOD_KATEX.replace(
        "                                        if (!releaseLatch.canRunDeferred()) return@Runnable\n"
        "                                        renderedBitmap = bitmap\n",
        "                                        renderedBitmap = bitmap\n"
        "                                        if (!releaseLatch.canRunDeferred()) return@Runnable\n",
    )
    expect_latch(
        "breaker: the capture guard sits after the work it was meant to stop",
        late_capture_guard,
        GOOD_PREVIEW,
        GOOD_POLICY,
        ["KaTeXView.kt deferred block"],
        1,
    )

    commented_capture_guard = GOOD_KATEX.replace(
        "                                        if (!releaseLatch.canRunDeferred()) return@Runnable\n",
        "                                        // if (!releaseLatch.canRunDeferred()) return@Runnable\n",
    )
    expect_latch(
        "precision: a commented-out guard is not a guard",
        commented_capture_guard,
        GOOD_PREVIEW,
        GOOD_POLICY,
        ["KaTeXView.kt deferred block"],
        1,
    )

    untracked = GOOD_KATEX.replace("                                    releaseLatch.track(capture)\n", "")
    expect_latch(
        "breaker: the capture runnable is never registered for removal",
        untracked,
        GOOD_PREVIEW,
        GOOD_POLICY,
        ["is never registered with the release latch"],
        1,
    )

    unlatched = GOOD_KATEX.replace("                    val outstanding = releaseLatch.release()\n", "")
    expect_latch(
        "breaker: the release path never latches the view",
        unlatched,
        GOOD_PREVIEW,
        GOOD_POLICY,
        ["touches the WebView before latching it"],
        1,
    )

    inlined_plan = GOOD_KATEX.replace(
        "                    for (step in releasePlan(attached = wv.parent != null)) {\n"
        "                        when (step) {\n"
        "                            ReleaseStep.DETACH -> (wv.parent as? ViewGroup)?.removeView(wv)\n"
        "                            ReleaseStep.DROP_DEFERRED -> for (task in outstanding) wv.removeCallbacks(task)\n"
        "                            ReleaseStep.STOP_LOADING -> wv.stopLoading()\n"
        "                            ReleaseStep.DESTROY -> wv.destroy()\n"
        "                        }\n"
        "                    }\n",
        "                    (wv.parent as? ViewGroup)?.removeView(wv)\n"
        "                    for (task in outstanding) wv.removeCallbacks(task)\n"
        "                    wv.stopLoading()\n"
        "                    wv.destroy()\n",
    )
    expect_latch(
        "breaker: the release path hand-sequences the teardown instead of using the plan",
        inlined_plan,
        GOOD_PREVIEW,
        GOOD_POLICY,
        ["does not use the derived teardown plan"],
        1,
    )

    null_instead_of_named = GOOD_KATEX.replace(
        "ReleaseStep.DROP_DEFERRED -> for (task in outstanding) wv.removeCallbacks(task)",
        "ReleaseStep.DROP_DEFERRED -> for (task in outstanding) wv.removeCallbacks(null)",
    )
    expect_latch(
        "breaker: the tracked runnables are dropped with the null no-op again",
        null_instead_of_named,
        GOOD_PREVIEW,
        GOOD_POLICY,
        ["does not remove the deferred work by identity"],
        1,
    )

    no_destroy = GOOD_KATEX.replace("ReleaseStep.DESTROY -> wv.destroy()", "")
    expect_latch(
        "breaker: the release path no longer destroys the WebView",
        no_destroy,
        GOOD_PREVIEW,
        GOOD_POLICY,
        ["no longer stops and destroys the WebView"],
        1,
    )

    # the second host is covered by the same criteria, not by a copy of them
    preview_unguarded = GOOD_PREVIEW.replace(
        "                    if (!releaseLatch.canRunDeferred()) return@Runnable\n",
        "",
    )
    expect_latch(
        "breaker: the preview load drops its latch guard",
        GOOD_KATEX,
        preview_unguarded,
        GOOD_POLICY,
        ["FilePreviewScreen.kt deferred block"],
        1,
    )

    # policy arms — the plan order is the contract both hosts inherit
    plan_no_detach = GOOD_POLICY.replace(
        "listOf(ReleaseStep.DETACH, ReleaseStep.DROP_DEFERRED, ReleaseStep.STOP_LOADING, ReleaseStep.DESTROY)",
        "listOf(ReleaseStep.DROP_DEFERRED, ReleaseStep.STOP_LOADING, ReleaseStep.DESTROY)",
    )
    expect_latch(
        "breaker: the attached-view plan stops detaching first",
        GOOD_KATEX,
        GOOD_PREVIEW,
        plan_no_detach,
        ["the attached-view plan does not detach first"],
        1,
    )

    plan_single = GOOD_POLICY.replace(
        "    } else {\n"
        "        listOf(ReleaseStep.DROP_DEFERRED, ReleaseStep.STOP_LOADING, ReleaseStep.DESTROY)\n"
        "    }\n",
        "    } else {\n"
        "        ReleaseStep.DESTROY.let { java.util.Collections.singletonList(it) }\n"
        "    }\n",
    )
    expect_latch(
        "breaker: the two plans collapse into one (the detached branch lost its steps)",
        GOOD_KATEX,
        GOOD_PREVIEW,
        plan_single,
        ["not expressed as two driven step lists"],
        1,
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
    katex_path = os.path.join(root, KATEX_VIEW)
    preview_path = os.path.join(root, FILE_PREVIEW)
    policy_path = os.path.join(root, RELEASE_POLICY)
    for p in (manager_path, holder_path, slot_path, katex_path, preview_path, policy_path):
        if not os.path.isfile(p):
            print("FAIL %s not found under root %s" % (p, root))
            return 1

    manager_src = open(manager_path, encoding="utf-8").read()
    holder_src = open(holder_path, encoding="utf-8").read()
    results = list(analyse_manager(manager_src))
    results += list(analyse_lifecycle(manager_src, holder_src))
    results += list(analyse_slot(open(slot_path, encoding="utf-8").read()))
    results += list(analyse_release_latch([
        ("KaTeXView.kt", open(katex_path, encoding="utf-8").read()),
        ("FilePreviewScreen.kt", open(preview_path, encoding="utf-8").read()),
    ]))
    results += list(analyse_release_policy(open(policy_path, encoding="utf-8").read()))

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

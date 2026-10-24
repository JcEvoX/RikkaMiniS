#!/usr/bin/env python3
"""[deep-audit-1016c FIX-C] Gate: every WebView-driving execute call inside
`runAcquiredAction` must run under a per-tab mutex.

ISSUE-03A — only `executeSerialized` took `lockForTab`, so the implicit-tab
entry path (`runAcquiredAction(input, implicitTab = true)`) executed
`tab.manager.execute(input)` with NO lock. Two overlap windows against an
explicit `executeSerialized(T)`:

1. the explicit path holds `lockForTab(T)` before its own `acquireTab` flips
   `inUse = true` (a Main-dispatcher gap in which the fan-out still sees T
   free and walks straight in);
2. `acquireTab`'s explicit-existing-id branch returns T WITHOUT checking
   `inUse`, so an explicit action walks onto a tab a tab-less action is
   already driving.

Fix shape (the one this gate pins): inside `runAcquiredAction`, the execute
call sits inside `if (implicitTab) { … lockForTab(tab.id).withLock { … } }`,
with the `else` arm being the explicit path (its caller already holds the
mutex — Kotlin's Mutex is not reentrant, so re-taking it there would
self-deadlock). Rule: within `runAcquiredAction`, every `tab.manager.execute(`
must sit inside the lexical span of `lockForTab(…).withLock { … }`, or inside
the `else` arm of the `if (implicitTab)` conditional (an explicit
`SERIAL-LOCK: caller-held` comment opts nothing out — the else arm is the
only sanctioned unlocked shape). `executeSerialized` must still take its
mutex.

Exit 0 = PASS, 1 = FAIL (prints file:line for each violation).
`--selftest` builds a violating fixture and a compliant fixture and asserts
the checker reds the first and greens the second (the gate proves its own
teeth).

Interface: `python3 scripts/scan/browser_tab_exec_serial_check.py [REPO_ROOT]`
(defaults to cwd; also accepts `--root ROOT`). Registration into
scripts/scan/scan.sh is the main session's job (this branch is forbidden from
touching scan.sh / test_scan.py).
"""
import os
import re
import sys
import tempfile

REL = "src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserTabPool.kt"

EXEC_CALL = "tab.manager.execute("
IMPLICIT_COND = "if (implicitTab)"
LOCK_FN = "lockForTab"


def line_of(src: str, idx: int) -> int:
    return src.count("\n", 0, idx) + 1


def withlock_receivers(src: str):
    """Every `.withLock` in [src] reduced to its receiver.

    Returns (kind, name, expr) triples:
      ("call", callee, full_call)  — `lockForTab(tabId).withLock` / `Mutex().withLock`
      ("ident", name, "")          — `mutex.withLock`
      ("unknown", "", "")          — receiver could not be resolved (fail-closed)

    [fix/meta-audit-m2-binding] the falsifying witness kept `lockForTab(tabId)`
    in the function (bound to a dead `val mutex`) and locked on a freshly
    constructed `Mutex()` instead — the old "both substrings present" test read
    that as serialized. The receiver of `withLock` is the contract; not the
    co-occurrence of two words somewhere in the body.
    """
    out = []
    for m in re.finditer(r"\.withLock", src):
        i = m.start() - 1
        while i >= 0 and src[i] in " \t\r\n":
            i -= 1
        if i < 0:
            out.append(("unknown", "", ""))
            continue
        if src[i] == ")":
            depth = 0
            j = i
            while j >= 0:
                if src[j] == ")":
                    depth += 1
                elif src[j] == "(":
                    depth -= 1
                    if depth == 0:
                        break
                j -= 1
            if j < 0:
                out.append(("unknown", "", ""))
                continue
            k = j - 1
            while k >= 0 and (src[k].isalnum() or src[k] in "_."):
                k -= 1
            out.append(("call", src[k + 1:j], src[j:i + 1]))
            continue
        if src[i].isalnum() or src[i] == "_":
            k = i
            while k >= 0 and (src[k].isalnum() or src[k] in "_."):
                k -= 1
            out.append(("ident", src[k + 1:i + 1], ""))
            continue
        out.append(("unknown", "", ""))
    return out


def receiver_is_per_tab(kind: str, name: str, scope: str) -> bool:
    """True only when the receiver provably comes from lockForTab(…)."""
    if kind == "call":
        return name == LOCK_FN or name.endswith("." + LOCK_FN)
    if kind == "ident":
        return bool(re.search(
            r"\b%s\s*(?::\s*[^=]+)?=\s*%s\s*\(" % (re.escape(name), LOCK_FN), scope))
    return False


def balanced_block(src: str, open_idx: int):
    """Return (start, end_exclusive) of the brace block opened at open_idx
    (which must point at '{')."""
    assert src[open_idx] == "{"
    depth = 0
    i = open_idx
    in_str = None
    while i < len(src):
        c = src[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        elif c in ('"', "'"):
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return (open_idx, i + 1)
        i += 1
    return (open_idx, len(src))


def extract_fun(src: str, signature: str):
    """Body span of the function whose source contains [signature]."""
    at = src.find(signature)
    if at < 0:
        return None
    brace = src.find("{", at)
    if brace < 0:
        return None
    return balanced_block(src, brace)


def check_source(src: str, label: str) -> list:
    """Returns a list of 'label:line: message' violations (empty = PASS)."""
    bad = []
    body = extract_fun(src, "fun runAcquiredAction(")
    if body is None:
        bad.append("%s:1: runAcquiredAction(…) not found — the serialization "
                   "contract moved? re-pin this gate" % label)
        return bad
    _, body_end = body
    body_src = src[body[0]:body_end]

    exec_hits = [m.start() for m in re.finditer(re.escape(EXEC_CALL), body_src)]
    if not exec_hits:
        bad.append("%s:%d: runAcquiredAction no longer calls %s — the "
                   "WebView-driving call moved? re-pin this gate"
                   % (label, line_of(src, body[0]), EXEC_CALL))
        return bad

    # Locate the `if (implicitTab) { … } else { … }` conditional (if present).
    cond = body_src.find(IMPLICIT_COND)
    lock_span = None       # (start, end) of the sanctioned locked shape
    else_span = None       # (start, end) of the sanctioned caller-held arm
    if cond >= 0:
        cond_line = line_of(src, body[0] + cond)
        brace = body_src.find("{", cond + len(IMPLICIT_COND))
        if brace < 0:
            bad.append("%s:%d: if (implicitTab) has no block" % (label, cond_line))
        else:
            c_start, c_end = balanced_block(body_src, brace)
            arm = body_src[c_start:c_end]
            arm_recv = withlock_receivers(arm)
            if not arm_recv:
                bad.append("%s:%d: the implicit arm takes no lock — it must "
                           "acquire the acquired tab's per-tab mutex "
                           "(lockForTab(tab.id).withLock { … })"
                           % (label, cond_line))
            else:
                for kind, name, expr in arm_recv:
                    if receiver_is_per_tab(kind, name, arm):
                        continue
                    if kind == "unknown":
                        bad.append("%s:%d: the implicit arm locks on a "
                                   "receiver this gate cannot resolve — "
                                   "re-pin the gate rather than assume it is "
                                   "the per-tab mutex" % (label, cond_line))
                    elif kind == "call":
                        bad.append("%s:%d: the implicit arm locks on "
                                   "`%s.withLock` — not lockForTab(tab.id), so "
                                   "the fan-out still drives the WebView "
                                   "unserialized" % (label, cond_line, name))
                    else:
                        bad.append("%s:%d: the implicit arm locks on "
                                   "`%s.withLock` but `%s` is not bound to "
                                   "lockForTab(…) there" % (label, cond_line, name, name))
                wl = arm.rfind("withLock")
                wb = arm.find("{", wl)
                if wb >= 0:
                    ls, le = balanced_block(arm, wb)
                    lock_span = (c_start + ls, c_start + le)
            # else arm
            else_kw = body_src.find("else", c_end - 1)
            if else_kw < 0:
                bad.append("%s:%d: the if (implicitTab) conditional has no "
                           "else arm — the explicit path's execute must stay "
                           "here (its caller already holds the mutex)"
                           % (label, cond_line))
            else:
                e_brace = body_src.find("{", else_kw)
                if e_brace < 0:
                    bad.append("%s:%d: the else arm has no block" % (label, cond_line))
                else:
                    else_span = balanced_block(body_src, e_brace)

    for hit in exec_hits:
        line = line_of(src, body[0] + hit)
        in_locked = lock_span and lock_span[0] <= hit < lock_span[1]
        in_else = else_span and else_span[0] <= hit < else_span[1]
        if in_locked or in_else:
            continue
        bad.append(
            "%s:%d: tab.manager.execute( outside a per-tab mutex scope "
            "(ISSUE-03A: the implicit path must wrap it in "
            "lockForTab(tab.id).withLock { … }; the explicit path's caller "
            "already holds the mutex)" % (label, line)
        )

    # The serial entry must still exist and still take its mutex — on the
    # per-tab mutex ITSELF, not merely in its neighbourhood.
    #
    # [fix/meta-audit-m2-binding] dataflow axis: the falsifying witness left
    # `val mutex = lockForTab(tabId)` in place (now a dead local) and locked on
    # `Mutex()` — every `executeSerialized` call then serialized against a
    # throwaway instance while the tab's real mutex was never acquired, and the
    # old "lockForTab( and withLock both appear" test stayed green.
    ser = extract_fun(src, "fun executeSerialized(")
    if ser is None:
        bad.append("%s:1: executeSerialized(…) not found — the serial entry "
                   "moved? re-pin this gate" % label)
    else:
        s_src = src[ser[0]:ser[1]]
        # Scope for resolving `val x = lockForTab(...)`: the function body plus
        # any enclosing-class locals are out of reach here, so bindings must be
        # visible inside the function itself — that is where the witness lives.
        recv = withlock_receivers(s_src)
        if not recv:
            bad.append("%s:%d: executeSerialized takes no lock at all — it "
                       "must acquire lockForTab(tabId).withLock"
                       % (label, line_of(src, ser[0])))
        else:
            for kind, name, expr in recv:
                if receiver_is_per_tab(kind, name, s_src):
                    continue
                if kind == "unknown":
                    bad.append("%s:%d: executeSerialized locks on a receiver "
                               "this gate cannot resolve — re-pin the gate "
                               "rather than assume it is the per-tab mutex"
                               % (label, line_of(src, ser[0])))
                elif kind == "call":
                    bad.append("%s:%d: executeSerialized locks on `%s.withLock` "
                               "— a `%s(...)` result is not the per-tab mutex, "
                               "so two concurrent actions still drive the same "
                               "WebView (ISSUE-03A overlap window)"
                               % (label, line_of(src, ser[0]), name, name))
                else:
                    bad.append("%s:%d: executeSerialized locks on `%s.withLock` "
                               "but `%s` is not bound to lockForTab(…) inside "
                               "the function — the per-tab mutex is never "
                               "acquired (ISSUE-03A overlap window)"
                               % (label, line_of(src, ser[0]), name, name))
    return bad


def run(repo: str) -> int:
    path = os.path.join(repo, REL)
    if not os.path.exists(path):
        print("FAIL %s: not found (run from the repo root)" % path)
        return 1
    src = open(path, encoding="utf-8", errors="replace").read()
    bad = check_source(src, REL)
    if bad:
        for b in bad:
            print("FAIL " + b)
        return 1
    print("PASS browser_tab_exec_serial (runAcquiredAction's WebView-driving "
          "execute sits under a per-tab mutex)")
    return 0


GOOD = '''    private suspend fun runAcquiredAction(
        input: BrowserActionInput,
        implicitTab: Boolean,
        acquireTabId: Int? = null,
    ): BrowserActionResult {
        val tab = acquireTab(acquireTabId ?: input.tabId)
            ?: return BrowserActionResult.error("Failed to acquire browser tab")
        return try {
            val executed = if (implicitTab) {
                withTimeoutOrNull(TAB_SERIAL_WAIT_TIMEOUT_MS) {
                    lockForTab(tab.id).withLock { tab.manager.execute(input) }
                }
            } else {
                tab.manager.execute(input)
            }
            val result = executed ?: return BrowserActionResult.error("busy")
            result
        } finally {
            tab.inUse = false
        }
    }

    private suspend fun executeSerialized(tabId: Int, input: BrowserActionInput): BrowserActionResult {
        val mutex = lockForTab(tabId)
        val result = withTimeoutOrNull(TAB_SERIAL_WAIT_TIMEOUT_MS) {
            mutex.withLock {
                runAcquiredAction(input, implicitTab = false, acquireTabId = tabId)
            }
        }
        return result ?: BrowserActionResult.error("busy")
    }
'''

# Pre-fix shape: the implicit path executes with no lock at all.
BAD_NO_LOCK = '''    private suspend fun runAcquiredAction(
        input: BrowserActionInput,
        implicitTab: Boolean,
        acquireTabId: Int? = null,
    ): BrowserActionResult {
        val tab = acquireTab(acquireTabId ?: input.tabId)
            ?: return BrowserActionResult.error("Failed to acquire browser tab")
        return try {
            val result = tab.manager.execute(input)
            result
        } finally {
            tab.inUse = false
        }
    }

    private suspend fun executeSerialized(tabId: Int, input: BrowserActionInput): BrowserActionResult {
        val mutex = lockForTab(tabId)
        val result = withTimeoutOrNull(TAB_SERIAL_WAIT_TIMEOUT_MS) {
            mutex.withLock {
                runAcquiredAction(input, implicitTab = false, acquireTabId = tabId)
            }
        }
        return result ?: BrowserActionResult.error("busy")
    }
'''

# Lock exists but on the wrong thing: inside the implicit arm but not wrapping
# the execute call.
BAD_WRONG_SPAN = '''    private suspend fun runAcquiredAction(
        input: BrowserActionInput,
        implicitTab: Boolean,
        acquireTabId: Int? = null,
    ): BrowserActionResult {
        val tab = acquireTab(acquireTabId ?: input.tabId)
            ?: return BrowserActionResult.error("Failed to acquire browser tab")
        return try {
            if (implicitTab) {
                lockForTab(tab.id).withLock { tab.lastActivityDate = Date() }
                tab.manager.execute(input)
            } else {
                tab.manager.execute(input)
            }
        } finally {
            tab.inUse = false
        }
    }

    private suspend fun executeSerialized(tabId: Int, input: BrowserActionInput): BrowserActionResult {
        val mutex = lockForTab(tabId)
        return mutex.withLock { runAcquiredAction(input, implicitTab = false, acquireTabId = tabId) }
    }
'''


# [fix/meta-audit-m2-binding] the falsifying witness, verbatim: `lockForTab(tabId)`
# stays in the function (bound to a now-dead `val mutex`) while `withLock` moves
# onto a throwaway `Mutex()`. Both magic substrings remain, so the old
# co-occurrence test stayed green — this arm pins the RECEIVER identity.
BAD_MUTEX_RECEIVER = GOOD.replace(
    "        val result = withTimeoutOrNull(TAB_SERIAL_WAIT_TIMEOUT_MS) {\n"
    "            mutex.withLock {",
    "        val result = withTimeoutOrNull(TAB_SERIAL_WAIT_TIMEOUT_MS) {\n"
    "            Mutex().withLock {")

# Same class of defect on the implicit arm: the lock is taken on a fresh
# instance instead of the acquired tab's mutex.
BAD_IMPLICIT_RECEIVER = GOOD.replace(
    "                    lockForTab(tab.id).withLock { tab.manager.execute(input) }",
    "                    Mutex().withLock { tab.manager.execute(input) }")

# Direct-chain form (no local binding) must stay accepted: `lockForTab(id).withLock`.
GOOD_DIRECT_CHAIN = GOOD.replace(
    "        val mutex = lockForTab(tabId)\n"
    "        val result = withTimeoutOrNull(TAB_SERIAL_WAIT_TIMEOUT_MS) {\n"
    "            mutex.withLock {",
    "        val result = withTimeoutOrNull(TAB_SERIAL_WAIT_TIMEOUT_MS) {\n"
    "            lockForTab(tabId).withLock {")

# Lock taken on a non-per-tab factory — same shape as `Mutex()`, different name.
BAD_OTHER_FACTORY = GOOD.replace("            mutex.withLock {",
                                 "            newMutex().withLock {")


def selftest() -> int:
    failures = []
    good = check_source(GOOD, "good.kt")
    bad_nolock = check_source(BAD_NO_LOCK, "bad-nolock.kt")
    bad_span = check_source(BAD_WRONG_SPAN, "bad-span.kt")
    print("selftest good      fixture -> %d violation(s)" % len(good))
    print("selftest bad-nolock fixture -> %d violation(s)" % len(bad_nolock))
    print("selftest bad-span  fixture -> %d violation(s)" % len(bad_span))
    for v in bad_nolock + bad_span:
        print("   " + v)
    if good:
        failures.append("compliant fixture must be clean, got %s" % good)
    if not bad_nolock:
        failures.append("no-lock fixture must red")
    if not any("outside a per-tab mutex scope" in b for b in bad_nolock):
        failures.append("no-lock fixture did not trip the placement rule")
    if not bad_span:
        failures.append("wrong-span fixture must red")
    # executeSerialized stripped of its mutex must red too.
    stripped = GOOD.replace("val mutex = lockForTab(tabId)", "val mutex = kotlinx.coroutines.sync.Mutex()")
    if not check_source(stripped, "x.kt"):
        failures.append("stripped executeSerialized not detected")

    # ── [fix/meta-audit-m2-binding] receiver-identity arms ─────────────────
    arms = [
        # (name, source, expect_red, must_mention)
        ("witness: Mutex().withLock receiver", BAD_MUTEX_RECEIVER, True,
         "`Mutex.withLock`"),
        ("witness: implicit arm on Mutex()", BAD_IMPLICIT_RECEIVER, True,
         "`Mutex.withLock`"),
        ("direct chain lockForTab(id).withLock", GOOD_DIRECT_CHAIN, False, ""),
        ("non-per-tab factory receiver", BAD_OTHER_FACTORY, True,
         "`newMutex.withLock`"),
    ]
    for name, src, expect_red, needle in arms:
        v = check_source(src, name.replace(" ", "-") + ".kt")
        if expect_red:
            if not v:
                failures.append("%s must red" % name)
            elif needle and not any(needle in b for b in v):
                failures.append("%s red but not on the receiver rule: %s" % (name, v))
        else:
            if v:
                failures.append("%s must stay clean, got %s" % (name, v))
        print("selftest %-38s -> %d violation(s)" % (name, len(v)))
        for b in v:
            print("   " + b)

    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    # The closing marker is pinned verbatim in the frozen scripts/scan/test_scan.py
    # (DA1016_GATES); growing the arm count must not break that contract, so the
    # arm total is reported on its own line.
    print("SELFTEST=PASS (browser_tab_exec_serial: 3 fixtures)")
    print("SELFTEST_ARMS=PASS (browser_tab_exec_serial: +4 receiver-identity arms)")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if "--selftest" in args:
        return selftest()
    repo = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(repo)


if __name__ == "__main__":
    sys.exit(main())

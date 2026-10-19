#!/usr/bin/env python3
"""[fix/da1022-G] browser_cookie_ui_refresh_check

Issue-07 gate: BrowserSettingsSheet cookie section must
  1. never call the callback-less `removeAllCookies(null)` (no flush, no refresh);
  2. use the callback form `removeAllCookies { ... }` with a `flush()` inside;
  3. cover BOTH http and https variants in the per-domain delete;
  4. carry a refresh signal (cookieRefreshTick) wired into remember keys and
     bumped at the per-domain and clear-all actions.

[da1029-E] The same sheet also guards the viewport / session-override pair, so
the second criteria family below was added here rather than as a new gate (a new
gate would have to be registered in scan.sh, which is frozen during this batch;
the name stays `cookie_ui_refresh` because renaming it would break the
registration — this docstring is the compensating note). The invariant is that
the panel shows the viewport the pool actually resolves:

  V1  the sheet subscribes to BOTH viewport sources — the global pair
      (`customViewportWidth/Height`) and the session override
      (`sessionViewportWidth/Height`, what an agent `set_viewport` writes). The
      pre-fix sheet subscribed to the global pair only, so a session override
      rendered as "Using UA default <session size>" with empty fields and the
      Default radio selected.
  V2  the width/height text states must be keyed on a value DERIVED from the
      session source (`resolvedViewportSize()`'s priority is session > global >
      UA default). Keying them on the global pair alone is exactly the pre-fix
      defect; keying them on anything not derived from the session flow (e.g. a
      nonce) would satisfy "has a key" while still never reflecting it.
  V3  `showCustomViewportEditor` must be keyed too — un-keyed it froze at the
      first frame, so the editor stayed hidden/interactive against the pool's
      real state.
  V4  each of the four viewport actions must clear the session override
      (`resetSessionViewport()`) BEFORE writing the global value: a session
      override shadows the global one, so without the clear the action (and in
      particular "Default") was a visible no-op.
  V5  regression anchor: `cookieFilterText` must stay UN-keyed. It is a pure
      local search box; giving it a key (e.g. the refresh tick) would reset the
      user's filter text on every recomposition — this arm is what proves the
      family has discrimination rather than "any change is green".

Families 1-4 are judged by [check] (cookie) and 5-9 by [check_viewport], each
with its own fixtures; both run on the real file.

Code anchors only — comments are stripped before judgment, so a KDoc that
mentions `removeAllCookies(null)` does not trip the gate.

Usage: python3 browser_cookie_ui_refresh_check.py <repo_root>
       python3 browser_cookie_ui_refresh_check.py --root <repo_root>
       python3 browser_cookie_ui_refresh_check.py --selftest
Exit 0 = pass, 1 = violation, 2 = usage error.
"""
import re
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


def mask_code(src: str) -> str:
    """Comment + string masking for the viewport criteria family.

    The cookie family must keep string literals (`"https://$domain"`), so this
    is a separate pass: here a mention inside a comment OR a string can neither
    satisfy a criterion nor trip one."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
        elif c == '"':
            i += 1
            while i < n and src[i] != '"':
                if src[i] == "\\":
                    if out[i] != "\n":
                        out[i] = " "
                    i += 1
                    if i < n and out[i] != "\n":
                        out[i] = " "
                    i += 1
                    continue
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            i += 1
        else:
            i += 1
    return "".join(out)


SESSION_SOURCES = {
    "viewportWidthText": "sessionWidthFromPool",
    "viewportHeightText": "sessionHeightFromPool",
}
VIEWPORT_ACTIONS = ("onSelectDefault", "onSelectCustom", "onApply", "onPreset")
RESET_CALL = "resetSessionViewport()"
GLOBAL_WRITE = "setGlobalViewport("
FROZEN_FILTER = "cookieFilterText"


def _statement_window(code, ident):
    """The declaration statement of [ident]: its `val` line plus any following
    lines indented deeper than that line (a wrapped expression).

    Stopping at the first line whose indentation is <= the declaration's is what
    makes the V2 criterion mean something: a fixed "next N lines" window let an
    unrelated neighbouring `val` (the session subscription itself) satisfy
    "derived from the session flow" — which is a false negative, exactly the
    shape V2 exists to catch.
    """
    m = re.search(r"^([ \t]*)val\s+" + re.escape(ident) + r"\b", code, re.M)
    if not m:
        return None
    indent = len(m.group(1).replace("\t", "    "))
    lines = code[m.start():].split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        if not line.strip():
            break
        if len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line)
    return "\n".join(out)


def check_viewport(source: str) -> list:
    """[da1029-E] V1–V5 — the panel must show the viewport the pool resolves."""
    code = mask_code(source)
    errs = []

    for flow, other in (("sessionViewportWidth", "sessionViewportHeight"),
                        ("sessionViewportHeight", "sessionViewportWidth")):
        if not re.search(r"\.%s\s*\.\s*collectAsState\s*\(\s*\)" % flow, code):
            errs.append(
                "does not subscribe to the %s flow — a session override (what an "
                "agent set_viewport writes) stays invisible to this panel" % flow
            )

    for state, session_flow in SESSION_SOURCES.items():
        m = re.search(r"var\s+%s\s+by\s+remember\s*\(([^)]*)\)" % re.escape(state), code)
        if not m:
            errs.append(
                "`var %s by remember {` has no key list at all — it freezes on the "
                "values seen at the first composition" % state
            )
            continue
        keys = [k.strip() for k in m.group(1).split(",") if k.strip()]
        if not keys:
            errs.append("`%s` has an empty remember key list" % state)
            continue
        derived = False
        for key in keys:
            if not re.fullmatch(r"[A-Za-z_]\w*", key):
                continue
            window = _statement_window(code, key)
            if window and session_flow in window:
                derived = True
        if not derived:
            errs.append(
                "`%s` is keyed on %s, none of which is derived from `%s` — the "
                "state cannot follow a session-override change (V2)"
                % (state, keys, session_flow)
            )

    m = re.search(r"var\s+showCustomViewportEditor\s+by\s+remember\s*\(([^)]*)\)", code)
    if not m or not m.group(1).strip():
        errs.append(
            "`var showCustomViewportEditor by remember {` is un-keyed — the "
            "editor freezes at the first frame instead of following the pool"
        )

    for action in VIEWPORT_ACTIONS:
        block = slice_from_marker(code, action + " = {")
        if not block:
            errs.append(
                "no `%s = {` block — a viewport action the sheet pins is gone "
                "(ROT: update this gate)" % action
            )
            continue
        if RESET_CALL not in block:
            errs.append(
                "`%s` never calls `%s` — a session override shadows the global "
                "value it writes, so the action is a no-op for those tabs (V4)"
                % (action, RESET_CALL)
            )
            continue
        if GLOBAL_WRITE not in block:
            errs.append(
                "`%s` clears the session override but never writes a global "
                "value — the clear alone leaves every tab at the UA default"
                % action
            )
            continue
        if block.index(RESET_CALL) > block.index(GLOBAL_WRITE):
            errs.append(
                "`%s` writes the global viewport before clearing the session "
                "override — the stale override wins the resolve (V4)" % action
            )

    if re.search(r"var\s+%s\s+by\s+remember\s*\(" % FROZEN_FILTER, code):
        errs.append(
            "`%s` gained a remember key — it is a pure local search box, so a key "
            "(the refresh tick, the pool state) resets the user's typed filter "
            "text on recomposition (V5 regression anchor)" % FROZEN_FILTER
        )
    elif not re.search(r"var\s+%s\s+by\s+remember\s*\{" % FROZEN_FILTER, code):
        errs.append(
            "`%s` is gone or no longer a plain un-keyed remember — the V5 "
            "regression anchor lost its subject (ROT: re-point it)" % FROZEN_FILTER
        )

    return errs


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

    # ── [da1029-E] viewport / session-override fixtures ──────────────────────
    good_vp = """
    val customWidthFromPool by tabPool.customViewportWidth.collectAsState()
    val customHeightFromPool by tabPool.customViewportHeight.collectAsState()
    val sessionWidthFromPool by tabPool.sessionViewportWidth.collectAsState()
    val sessionHeightFromPool by tabPool.sessionViewportHeight.collectAsState()
    val sessionViewportActive = sessionWidthFromPool > 0 && sessionHeightFromPool > 0
    val effectiveWidthFromPool =
        if (sessionViewportActive) sessionWidthFromPool else customWidthFromPool
    val effectiveHeightFromPool =
        if (sessionViewportActive) sessionHeightFromPool else customHeightFromPool
    var viewportWidthText by remember(effectiveWidthFromPool) {
        mutableStateOf(if (effectiveWidthFromPool > 0) effectiveWidthFromPool.toString() else "")
    }
    var viewportHeightText by remember(effectiveHeightFromPool) {
        mutableStateOf(if (effectiveHeightFromPool > 0) effectiveHeightFromPool.toString() else "")
    }
    var viewportMode by remember(isCustomViewportActive) { mutableStateOf(ViewportMode.DEFAULT) }
    var showCustomViewportEditor by remember(isCustomViewportActive) {
        mutableStateOf(isCustomViewportActive)
    }
    var cookieFilterText by remember { mutableStateOf("") }
    ViewportSection(
        onSelectDefault = {
            coroutineScope.launch {
                tabPool.resetSessionViewport()
                tabPool.setGlobalViewport(0, 0)
            }
        },
        onSelectCustom = {
            coroutineScope.launch {
                tabPool.resetSessionViewport()
                tabPool.setGlobalViewport(1280, 800)
            }
        },
        onApply = {
            coroutineScope.launch {
                tabPool.resetSessionViewport()
                tabPool.setGlobalViewport(1440, 900)
            }
        },
        onPreset = { w, h ->
            coroutineScope.launch {
                tabPool.resetSessionViewport()
                tabPool.setGlobalViewport(w, h)
            }
        },
    )
    """

    good_vp_commented = """
    // the pool resolves session > global > UA default; subscribe to both so an
    // agent set_viewport is visible here (see sessionViewportWidth.collectAsState)
    // and clear the override with resetSessionViewport() before setGlobalViewport(
    """ + good_vp

    vp_arms = [
        ("V0 compliant viewport wiring", good_vp_commented, []),
        ("V1 session height not subscribed",
         good_vp.replace("    val sessionHeightFromPool by tabPool.sessionViewportHeight.collectAsState()\n", ""),
         ["does not subscribe"]),
        ("V2 width keyed on the global pair",
         good_vp.replace("remember(effectiveWidthFromPool)", "remember(customWidthFromPool)"),
         ["none of which is derived"]),
        ("V2 height keyed on a nonce",
         good_vp.replace("remember(effectiveHeightFromPool)", "remember(vpNonce)") + "\n    val vpNonce = 0\n",
         ["none of which is derived"]),
        ("V3 editor un-keyed",
         good_vp.replace("var showCustomViewportEditor by remember(isCustomViewportActive) {",
                         "var showCustomViewportEditor by remember {"),
         ["is un-keyed"]),
        ("V4 action without the reset",
         good_vp.replace("""        onApply = {
            coroutineScope.launch {
                tabPool.resetSessionViewport()
""", """        onApply = {
            coroutineScope.launch {
"""),
         ["never calls `resetSessionViewport()`"]),
        ("V4 reset after the global write",
         good_vp.replace("""            coroutineScope.launch {
                tabPool.resetSessionViewport()
                tabPool.setGlobalViewport(w, h)""",
                         """            coroutineScope.launch {
                tabPool.setGlobalViewport(w, h)
                tabPool.resetSessionViewport()"""),
         ["before clearing"]),
        ("V5 filter box keyed",
         good_vp.replace("var cookieFilterText by remember {",
                         "var cookieFilterText by remember(cookieRefreshTick) {"),
         ["gained a remember key"]),
    ]    # the comment trap: a KDoc mentioning removeAllCookies(null) must NOT trip
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

    for name, sample, needles in vp_arms:
        errs = check_viewport(sample)
        if not needles:
            if errs:
                print(f"  FAIL {name}: expected green, got {errs}")
                failures += 1
            else:
                print(f"  OK {name}: green")
            continue
        hit = [n for n in needles if not any(n in e for e in errs)]
        if hit or not errs:
            print(f"  FAIL {name}: expected {needles}, got {errs}")
            failures += 1
        else:
            print(f"  OK {name}: {errs}")
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
    vp_errs = check_viewport(src)
    if errs or vp_errs:
        print(f"browser_cookie_ui_refresh_check RED on {TARGET}:")
        for e in errs:
            print(f"  - {e}")
        for e in vp_errs:
            print(f"  - [viewport] {e}")
        return 1
    print(f"browser_cookie_ui_refresh_check OK ({TARGET}; cookie refresh + "
          f"viewport/session-override wiring)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

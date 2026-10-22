#!/usr/bin/env python3
"""[fix/da1022-G] browser_cookie_ui_refresh_check

Issue-07 gate: BrowserSettingsSheet cookie section must
  1. never call the callback-less `removeAllCookies(null)` (no flush, no refresh);
  2. use the callback form `removeAllCookies { ... }` with a `flush()` inside;
  3. cover BOTH http and https variants in the per-domain delete;
  4. carry a refresh signal (cookieRefreshTick) wired into remember keys and
     bumped at the per-domain and clear-all actions.

[deep-audit-1039 B-07] Family 5 (B1–B2) — the domain list's refresh signal must
be the STORE's, not the sheet's. The cookie domains are HISTORY domains
(`historyStore.uniqueDomains()`), so the list changes whenever the history
changes — including changes this panel causes indirectly: switching UA (or
Viewport) here reloads the active tab, `BrowserUseManager.onPageFinished`
records the visit and `historyStore.revision` advances. Keying the list on the
panel-local `cookieRefreshTick` alone left it on the pre-reload snapshot until
the sheet was closed and reopened (the [da1022-G] fix wired the tick for
cookie deletes, which is a different mutation surface). Same shape and same fix
as `BrowserHistorySheet` ([dv15-B3]): subscribe to `historyStore.revision` and
feed it into the `allDomains` remember keys. The criterion binds the DECISION
POINT (the memoized domain read is invalidated by the store's counter), not one
spelling of the key list.

  B1  the sheet subscribes to `historyStore.revision` via collectAsState.
  B2  the `allDomains` remember key list includes a value backed by that flow
      (derived-window check, same technique as V2/V6 — a key that is not fed
      by the store's flow satisfies "has a key" while never reflecting a
      record()).

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
  V6  [da1030-B] the UA radio selection (`selectedProfile`) must be seeded from
      the pool's `currentUserAgentProfile` flow and keyed on it. The pre-fix
      sheet snapshotted the `user_agent_profile` prefs key once (un-keyed
      remember), so an agent `set_user_agent` — which now routes through the
      pool — left the panel rendering the old radio while the tabs had moved on.
  V7  [da1030-B] the pool's `when (input.action)` must route
      `BrowserAction.SET_USER_AGENT` through an explicit branch, and that
      handler must replay the resolved viewport (`applyViewportToAllTabs()`).
      Pre-fix the action fell into the per-tab `else`: one tab got the new UA,
      `BrowserUseManager.setUserAgent` reset that tab's layout to the profile
      default, and `applyViewport()` (no-arg) replayed only the profile's own
      size — so a session/global custom viewport was clobbered and the other
      tabs kept the old profile.

Families 1-4 are judged by [check] (cookie); B1–B2 by [check_domain_refresh],
V1–V6 by [check_viewport] and V7 by [check_pool_ua] — the latter is the first
criterion here that reads a second file (`BrowserTabPool.kt`). Each family
carries its own fixtures; all run on the real tree, not on samples.

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
POOL_TARGET = "src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserTabPool.kt"
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
UA_POOL_FLOW = "currentUserAgentProfile"
REPLAY_CALL = "applyViewportToAllTabs()"


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

    # ── V6 [da1030-B] the UA radio must follow the pool, not a prefs snapshot ──
    if not re.search(r"\.%s\s*\.\s*collectAsState\s*\(\s*\)" % UA_POOL_FLOW, code):
        errs.append(
            "does not subscribe to `%s` — an agent `set_user_agent` (which now "
            "routes through the pool) leaves this panel on the old radio (V6)"
            % UA_POOL_FLOW
        )
    m = re.search(r"var\s+selectedProfile\s+by\s+remember\s*\(([^)]*)\)", code)
    if not m:
        errs.append(
            "`var selectedProfile by remember {` has no key list at all — it froze "
            "on the prefs snapshot read at the first composition, so the radio kept "
            "showing the profile the sheet was opened with (V6)"
        )
    else:
        keys = [k.strip() for k in m.group(1).split(",") if k.strip()]
        backed = False
        for key in keys:
            if not re.fullmatch(r"[A-Za-z_]\w*", key):
                continue
            window = _statement_window(code, key)
            if window and UA_POOL_FLOW in window:
                backed = True
        if not backed:
            errs.append(
                "`selectedProfile` is keyed on %s, none of which is fed by the "
                "pool's `%s` flow — a key must re-seed the selection from the pool "
                "(V6)" % (keys, UA_POOL_FLOW)
            )

    return errs


def strip_comments_only(src: str) -> str:
    """Comment-only masking: string literals are PRESERVED.

    `mask_code` blanks string literals, which is right for the viewport family
    (a mention inside a string must not count) but wrong for the pref-key
    criteria: `"custom_user_agent"` IS the decision point there. Comments are
    still blanked, so a KDoc mentioning a key cannot satisfy a criterion.
    """
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
            # keep the literal, but skip over it so a "//" inside a string is
            # not mistaken for a comment
            i += 1
            while i < n and src[i] != '"':
                if src[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 1
        else:
            i += 1
    return "".join(out)


def check_custom_ua_single_source(pool_src: str, sheet_src: str) -> list:
    """[dv23-B4 B-05] V8 — the stored custom UA string has ONE source.

    The defect: `setUserAgentFromUI(profile, customUA)` assigned
    `customUserAgentString = customUA` unconditionally, so tapping a
    non-CUSTOM preset (which passes `null`) CLEARED the string the user had
    typed; a later `set_user_agent --user-agent custom` then fell back to the
    platform UA. The sheet's own field is the real source (it persists
    `custom_user_agent` on every keystroke), so the pool must resolve through
    that key and must not treat "no opinion" as "clear".

    Anchored on the DECISION (a guarded write, and a resolution through the
    single source), not on spelling.
    """
    code = strip_comments_only(pool_src)
    errs = []

    setter = slice_from_marker(code, "fun setUserAgentFromUI(")
    if not setter:
        errs.append(
            "no `fun setUserAgentFromUI(` in %s — the sheet's apply path this "
            "criterion pins is gone (ROT: update this gate)" % POOL_TARGET
        )
        return errs

    guard = setter.find("if (customUA != null)")
    write = setter.find("customUserAgentString = customUA")
    if guard < 0:
        errs.append(
            "`setUserAgentFromUI` assigns the stored custom UA unconditionally — "
            "a preset tap (which carries no custom string) clears the string the "
            "user typed, and a later `set_user_agent --user-agent custom` falls "
            "back to the platform UA (V8)"
        )
    elif write < 0:
        errs.append(
            "`setUserAgentFromUI` no longer stores the custom UA at all (V8)"
        )
    elif write < guard:
        errs.append(
            "`setUserAgentFromUI` writes the stored custom UA BEFORE the "
            "`customUA != null` guard — the guard does not protect the write (V8)"
        )

    if "storedCustomUserAgent()" not in code:
        errs.append(
            "the pool has no single-source resolution for the stored custom UA — "
            "`customUserAgentString` is a private cache that can lag the sheet's "
            "last keystroke (V8)"
        )

    handler = slice_from_marker(code, "fun handleSetUserAgent(")
    if not handler:
        errs.append(
            "no `fun handleSetUserAgent(` in %s (V8)" % POOL_TARGET
        )
    elif "storedCustomUserAgent()" not in handler:
        errs.append(
            "`handleSetUserAgent` does not resolve CUSTOM through the single "
            "source — it reads the private cache, which may predate the sheet's "
            "last keystroke (V8)"
        )

    sheet = strip_comments_only(sheet_src)
    if "stageCustomUserAgent(" not in sheet:
        errs.append(
            "the settings sheet's UA field does not feed the pool's copy — the "
            "pool's cache stays stale until Apply, while the sheet persists the "
            "string on every keystroke (V8)"
        )
    return errs


def check_pool_ua(source: str) -> list:
    """[da1030-B] V7 — `set_user_agent` must be a pool-level action.

    Two halves, each with a fixture: the branch must exist explicitly (falling
    into the per-tab `else` is the defect) and its handler must replay the
    resolved viewport (`BrowserUseManager.setUserAgent` resets the tab layout to
    the new profile's default; without the replay a session/global custom
    viewport is silently clobbered).
    """
    code = mask_code(source)
    errs = []

    block = slice_from_marker(code, "when (input.action)")
    if not block:
        errs.append(
            "no `when (input.action)` block in %s — the pool dispatch this "
            "criterion pins is gone (ROT: update this gate)" % POOL_TARGET
        )
        return errs

    m = re.search(r"BrowserAction\.SET_USER_AGENT\s*->\s*([A-Za-z_]\w*)\s*\(", block)
    if not m:
        errs.append(
            "`when (input.action)` has no explicit `BrowserAction.SET_USER_AGENT ->` "
            "branch — the action falls into the per-tab `else`, so it hits ONE tab, "
            "skips the viewport replay and leaves the panel/other tabs on the old "
            "profile (V7)"
        )
        return errs

    handler = m.group(1)
    body = slice_from_marker(code, "fun %s(" % handler)
    if not body:
        errs.append(
            "`BrowserAction.SET_USER_AGENT ->` calls `%s(` but no such function body "
            "exists in %s (V7)" % (handler, POOL_TARGET)
        )
    elif REPLAY_CALL not in body:
        errs.append(
            "`%s` never calls `%s` — `setUserAgent` resets each tab to the new "
            "profile's default viewport, so a session/global custom override is "
            "clobbered (V7)" % (handler, REPLAY_CALL)
        )

    return errs


def _remember_key_list(code: str, decl_marker: str):
    """Ordered key tokens of the `remember(...)` call that follows `decl_marker`
    (e.g. "val hasCookies = remember"). Paren-matched, so a wrapped key list
    cannot truncate the way a first-`)` scan would. None when the declaration
    is absent (caller reports fail-closed)."""
    idx = code.find(decl_marker)
    if idx < 0:
        return None
    op = code.find("(", idx + len(decl_marker))
    if op < 0:
        return None
    depth = 0
    i = op
    while i < len(code):
        c = code[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        return None
    return [k.strip() for k in code[op + 1:i].split(",") if k.strip()]


def _revision_backed(code: str, key: str) -> bool:
    """True when `key` is an identifier whose declaration subscribes to the
    history store's revision flow (`val <key> by ...revision.collectAsState()`).

    Binds B2 to the DECISION POINT — the key must be fed by the store's counter,
    not merely exist. A key on a nonce or an unrelated state has no declaration
    carrying `.revision.collectAsState` and fails this probe."""
    if not re.fullmatch(r"[A-Za-z_]\w*", key):
        return False
    return re.search(
        r"val\s+%s\s+by\s+[\w.]*revision\s*\.\s*collectAsState" % re.escape(key), code
    ) is not None


def check_domain_refresh(source: str) -> list:
    """[deep-audit-1039 B-07] B1–B2 — the domain list must follow the store.

    Pre-fix shape (the reason this family exists): `allDomains` was keyed on
    `(hasCookies, cookieRefreshTick)` — a panel-local counter that never moves
    when the history store changes, so switching UA/Viewport in the sheet
    reloaded the tab, `record()` planted the new domain, and the list kept
    showing the pre-reload snapshot until the sheet was closed and reopened.
    """
    code = strip_line_comments(source)
    errs = []

    if not re.search(r"\.revision\s*\.\s*collectAsState\s*\(\s*\)", code):
        errs.append(
            "does not subscribe to `historyStore.revision` — the domain list is "
            "keyed on panel-local state only, so a reload-driven record() (UA / "
            "viewport switch) leaves it on the pre-reload snapshot (B1)"
        )
        return errs

    for decl_marker, label in (
        ("val hasCookies = remember", "hasCookies"),
        ("val allDomains = remember", "allDomains"),
    ):
        keys = _remember_key_list(code, decl_marker)
        if keys is None:
            errs.append(
                "`%s` remember declaration not found — the domain-refresh anchor "
                "lost its subject (ROT: re-point it)" % label
            )
            continue
        if "cookieRefreshTick" not in keys:
            errs.append(
                "`%s` remember keys %s dropped `cookieRefreshTick` — the "
                "panel-local signal still owns cookie deletes / clear-all and "
                "must stay wired (family 4 regression)" % (label, keys)
            )
            continue
        if not any(_revision_backed(code, k) for k in keys):
            errs.append(
                "`%s` is keyed on %s, none of which is fed by "
                "`historyStore.revision` — the read cannot follow a reload-driven "
                "record() (B2)" % (label, keys)
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
    #    [deep-audit-1039 B-07] re-anchored: the literal `remember(cookieRefreshTick)`
    #    pinned the PRE-fix key list, which the B-07 fix must extend with the
    #    store's revision value. The decision is "the panel-local tick is still a
    #    remember key of the cookie-section derivations", judged via the same
    #    paren-matched key lists family 5 uses; the store-side half is B1/B2.
    if "cookieRefreshTick" not in src:
        errs.append("no cookieRefreshTick refresh signal")
    else:
        for decl_marker, label in (
            ("val hasCookies = remember", "hasCookies"),
            ("val allDomains = remember", "allDomains"),
        ):
            keys = _remember_key_list(src, decl_marker)
            if keys is None:
                errs.append("`%s` remember declaration not found (family 4)" % label)
            elif "cookieRefreshTick" not in keys:
                errs.append(
                    "`%s` remember keys %s lack cookieRefreshTick" % (label, keys)
                )
        bumps = src.count("cookieRefreshTick++")
        if bumps < 2:
            errs.append(f"cookieRefreshTick++ bumped {bumps} times, expected >= 2 (per-domain + clear-all)")

    return errs


def selftest() -> int:
    # [deep-audit-1039 B-07] the compliant cookie fixture is the POST-fix shape:
    # the panel-local tick stays a key AND the derivations are keyed on a value
    # fed by historyStore.revision (subscription + key use).
    compliant = """
            val cookieManager = remember { CookieManager.getInstance() }
            val historyStore = remember { BrowserHistoryStore.getInstance(context) }
            val historyRevision by historyStore.revision.collectAsState()
            val hasCookies = remember(cookieRefreshTick, historyRevision) { cookieManager.hasCookies() }
            val allDomains = remember(hasCookies, cookieRefreshTick, historyRevision) {
                historyStore.uniqueDomains()
            }
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
    val poolProfile by tabPool.currentUserAgentProfile.collectAsState()
    var selectedProfile by remember(poolProfile) {
        mutableStateOf(poolProfile)
    }
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
        ("V6 UA radio un-keyed (prefs snapshot)",
         good_vp.replace("var selectedProfile by remember(poolProfile) {",
                         "var selectedProfile by remember {"),
         ["no key list at all"]),
        ("V6 UA radio keyed on a nonce",
         good_vp.replace("var selectedProfile by remember(poolProfile) {",
                         "var selectedProfile by remember(vpNonce) {"),
         ["none of which is fed by"]),
        ("V6 UA pool flow not subscribed",
         good_vp.replace("    val poolProfile by tabPool.currentUserAgentProfile.collectAsState()\n", ""),
         ["does not subscribe to `currentUserAgentProfile`"]),
    ]

    # ── [da1030-B] pool-side fixtures: set_user_agent must be pool-level (V7) ──
    good_pool = """
        val result = when (input.action) {
            BrowserAction.NEW_TAB -> newTab(input.url)
            BrowserAction.SET_VIEWPORT -> handleSetViewport(input)
            BrowserAction.SET_USER_AGENT -> handleSetUserAgent(input)
            else -> {
                val serialTabId = input.tabId
                if (serialTabId != null) executeSerialized(serialTabId, input) else execute(input)
            }
        }
    }

    private suspend fun handleSetUserAgent(input: BrowserActionInput): BrowserActionResult = withContext(Dispatchers.Main) {
        val newProfile = input.userAgent
            ?: return@withContext BrowserActionResult.error("set_user_agent requires --user-agent")
        val customUA = if (newProfile == UserAgentProfile.CUSTOM) customUserAgentString else null
        userAgentProfile = newProfile
        for (tab in _tabs.value) {
            tab.manager.setUserAgent(newProfile, customUA)
        }
        applyViewportToAllTabs()
        val (w, h) = resolvedViewportSize()
        BrowserActionResult(text = "Switched to ${newProfile.value} (${w}x$h)")
    }
    """
    pool_arms = [
        ("V7 compliant pool routing", good_pool, []),
        ("V7 branch missing (falls into the per-tab else)",
         good_pool.replace("            BrowserAction.SET_USER_AGENT -> handleSetUserAgent(input)\n", ""),
         ["no explicit `BrowserAction.SET_USER_AGENT ->` branch"]),
        ("V7 handler skips the viewport replay",
         good_pool.replace("        applyViewportToAllTabs()\n", ""),
         ["never calls `applyViewportToAllTabs()`"]),
        ("V7 dispatch block gone",
         good_pool.replace("when (input.action)", "when (input.mode)"),
         ["no `when (input.action)` block"]),
    ]

    # ── [dv23-B4 B-05] V8 arms: the stored custom UA has ONE source ───────
    # Each dirty fixture differs from the compliant shape by exactly one
    # decision, so a breaker cannot be satisfied by another criterion.
    good_ua_pool = """
        fun setUserAgentFromUI(profile: UserAgentProfile, customUA: String? = null) {
            userAgentProfile = profile
            if (customUA != null) {
                customUserAgentString = customUA.ifEmpty { null }
            }
            val effectiveCustomUA =
                if (profile == UserAgentProfile.CUSTOM) storedCustomUserAgent() else null
            for (tab in _tabs.value) {
                tab.manager.setUserAgent(profile, effectiveCustomUA)
            }
        }

        private fun storedCustomUserAgent(): String? =
            customUserAgentString?.ifEmpty { null }
                ?: context.getSharedPreferences("browser_prefs", Context.MODE_PRIVATE)
                    .getString("custom_user_agent", null)?.ifEmpty { null }

        private suspend fun handleSetUserAgent(input: BrowserActionInput): BrowserActionResult = withContext(Dispatchers.Main) {
            val newProfile = input.userAgent
                ?: return@withContext BrowserActionResult.error("set_user_agent requires --user-agent")
            val customUA = if (newProfile == UserAgentProfile.CUSTOM) storedCustomUserAgent() else null
            userAgentProfile = newProfile
        }
    """
    good_ua_sheet = """
            OutlinedTextField(
                value = customUA,
                onValueChange = {
                    customUA = it
                    prefs.edit().putString("custom_user_agent", it).apply()
                    tabPool.stageCustomUserAgent(it)
                },
            )
    """
    ua_arms = [
        ("V8 compliant single source", good_ua_pool, good_ua_sheet, []),
        ("V8 preset tap clears the stored UA",
         good_ua_pool.replace(
             "            if (customUA != null) {\n"
             "                customUserAgentString = customUA.ifEmpty { null }\n"
             "            }\n",
             "            customUserAgentString = customUA\n"),
         good_ua_sheet,
         ["assigns the stored custom UA unconditionally"]),
        ("V8 no single-source resolution",
         good_ua_pool.replace("storedCustomUserAgent()", "customUserAgentString"),
         good_ua_sheet,
         ["no single-source resolution"]),
        ("V8 handler reads the private cache",
         good_ua_pool.replace(
             "val customUA = if (newProfile == UserAgentProfile.CUSTOM) storedCustomUserAgent() else null",
             "val customUA = if (newProfile == UserAgentProfile.CUSTOM) customUserAgentString else null"),
         good_ua_sheet,
         ["does not resolve CUSTOM through the single source"]),
        ("V8 sheet does not feed the pool's copy",
         good_ua_pool,
         good_ua_sheet.replace("                    tabPool.stageCustomUserAgent(it)\n", ""),
         ["does not feed the pool's copy"]),
    ]
    # the comment trap: a KDoc mentioning removeAllCookies(null) must NOT trip
    good_with_comment = """
            // [fix/da1022-G] removeAllCookies(null) left no flush and no refresh:
            // use the callback form to persist + bump the tick.
    """ + compliant + good_delete

    # ── [deep-audit-1039 B-07] domain-refresh (B1–B2) fixtures ───────────────
    # Only-red-its-own-criterion: each dirty fixture below differs from the
    # compliant shape by exactly one decision.
    good_domain = """
            val cookieManager = remember { CookieManager.getInstance() }
            val historyStore = remember { BrowserHistoryStore.getInstance(context) }
            val historyRevision by historyStore.revision.collectAsState()
            val hasCookies = remember(cookieRefreshTick, historyRevision) { cookieManager.hasCookies() }
            val allDomains = remember(hasCookies, cookieRefreshTick, historyRevision) {
                historyStore.uniqueDomains()
            }
    """
    domain_arms = [
        ("B1+B2 compliant (post-fix shape)", good_domain, []),
        ("B1 no revision subscription (pre-fix shape)",
         good_domain.replace("            val historyRevision by historyStore.revision.collectAsState()\n", ""),
         ["does not subscribe to `historyStore.revision`"]),
        ("precision: the receiver spelling of the subscription is not pinned",
         good_domain.replace("historyStore.revision.collectAsState()",
                             "store.revision.collectAsState()"),
         []),
        ("B2 allDomains not keyed on the revision-backed value",
         good_domain.replace("remember(hasCookies, cookieRefreshTick, historyRevision)",
                             "remember(hasCookies, cookieRefreshTick)"),
         ["none of which is fed by"]),
        ("B2 allDomains keyed on a nonce instead",
         good_domain.replace("remember(hasCookies, cookieRefreshTick, historyRevision)",
                             "remember(hasCookies, cookieRefreshTick, domainNonce)"),
         ["none of which is fed by"]),
        ("B2 hasCookies keeps the stale key list",
         good_domain.replace("val hasCookies = remember(cookieRefreshTick, historyRevision)",
                             "val hasCookies = remember(cookieRefreshTick)"),
         ["none of which is fed by"]),
        ("family-4 precision: tick dropped from allDomains",
         good_domain.replace("remember(hasCookies, cookieRefreshTick, historyRevision)",
                             "remember(hasCookies, historyRevision)"),
         ["dropped `cookieRefreshTick`"]),
        ("B1 precision: a comment mentioning revision is not a subscription",
         good_domain.replace("            val historyRevision by historyStore.revision.collectAsState()\n",
                             "            // val historyRevision by historyStore.revision.collectAsState()\n"
                             "            val historyRevision = 0\n"),
         ["does not subscribe to `historyStore.revision`"]),
    ]

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
    for name, sample, needles in pool_arms:
        errs = check_pool_ua(sample)
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
    for name, pool_sample, sheet_sample, needles in ua_arms:
        errs = check_custom_ua_single_source(pool_sample, sheet_sample)
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
    for name, sample, needles in domain_arms:
        errs = check_domain_refresh(sample)
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

    def read_target(rel):
        path = os.path.join(root, rel)
        try:
            return open(path).read()
        except OSError as e:
            print(f"cannot read {path}: {e}", file=sys.stderr)
            return None

    src = read_target(TARGET)
    if src is None:
        return 2
    pool_src = read_target(POOL_TARGET)
    if pool_src is None:
        return 2
    errs = check(src)
    vp_errs = check_viewport(src)
    pool_errs = check_pool_ua(pool_src)
    domain_errs = check_domain_refresh(src)
    # [dv23-B4 B-05] V8 — the stored custom UA has one source (pool + sheet).
    ua_errs = check_custom_ua_single_source(pool_src, src)
    if errs or vp_errs or pool_errs or domain_errs or ua_errs:
        print("browser_cookie_ui_refresh_check RED on %s (+ %s):" % (TARGET, POOL_TARGET))
        for e in errs:
            print(f"  - {e}")
        for e in vp_errs:
            print(f"  - [viewport] {e}")
        for e in pool_errs:
            print(f"  - [pool] {e}")
        for e in domain_errs:
            print(f"  - [domain-refresh] {e}")
        for e in ua_errs:
            print(f"  - [custom-ua] {e}")
        return 1
    print(f"browser_cookie_ui_refresh_check OK ({TARGET}; cookie refresh + "
          f"domain-refresh/store-revision + viewport/session-override + UA-pool "
          f"wiring + custom-UA single source; {POOL_TARGET} checked too)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

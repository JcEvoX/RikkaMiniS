#!/usr/bin/env python3
"""[da1023-F] gate — draft-promotion session-id binding at the UI seam.

After a draft chat's first send promotes it to a real session row
(`ChatSessionEntryExt.ensureSession` → `ChatViewModelStore.rename`), the
nav-route arg stays on the `__new__<uuid>` draft key for the whole screen
lifetime. Any entry point that keeps consuming the route arg binds to the
orphan draft key while the real resources live under the promoted id.

This gate pins the six audited entry points (deep-audit-1023 ISSUE-06):

  ChatScreen.kt
    1. markdown image tap handler — `remember(messages, …)` keyed on the
       ACTIVE id and `resolveMdMediaFile(..., viewModel.currentSessionId)`.
    2. ChatHistoryDrawer select — `if (id != viewModel.currentSessionId)` /
       `if (id == viewModel.currentSessionId)` (matches the drawer's own
       `currentSessionId = viewModel.currentSessionId` at its call site).
    3. ChatInputArea — `sessionId = viewModel.currentSessionId` (feeds the
       composer's MoveToSessionSheet self-filter and attachment funnel).
    4. WebAppSource.ChatAttachment — `sessionId = viewModel.currentSessionId`
       (AddToHomeSheet copies bytes into minis-sessions/<sid>/attachments/).
  AppNavigation.kt
    5. onOpenTerminal / onOpenTerminalWithCommand / onBrowseChatFiles route
       through `ChatViewModelStore.resolveSessionId(sessionId)`.
  MoveToSessionSheet.kt
    6. `LaunchedEffect(currentSessionId)` — re-runs the self-filter when the
       promoted id changes instead of caching the draft-era list.

  ChatScreen.kt (deep-audit-1027 ISSUE-01) — criterion 8 below
    7. pinned-shortcut HTML preview — the `LaunchedEffect` consuming
       `consumePendingHtmlPreview()` must be keyed on `viewModel.currentSessionId`,
       resolve through `resolvePathForSession(activeSessionId, …)` (never the
       global `resolveHostPath`), and accept the promoted id
       (`pending.sessionId != activeSessionId`) — the route arg alone leaves a
       shortcut pinned after draft promotion permanently unconsumed.

Bare route-arg usage at these sites must not come back. `resolveKey` itself
stays private — `katex_ready_race_check.py` hard-pins its source form.

  MainActivity.kt (deep-audit-1029 ISSUE-01) — criterion 9 below
    9. New-Chat entries (onStart launch-mode-2 resume + handleDeepLink's
       NewChat/NewCameraChat branch) resolve the draft id through
       `ComposerDraftStore.nextDraftId(this)`, never a hand-built
       `"__new__<uuid>"`. A fresh uuid orphans the live draft slot, and
       `saveText`'s slot guard then drops the text.

  MainActivity.kt (deep-audit-1032 ISSUE-07, P2) — criteria 10/11 below
   10. The `currentBackStackEntryFlow.collect { … }` listener (keep-alive
       presence + `currentChatSessionId` + the crash-recovery prefs write) must
       feed the id it read from the nav entry through
       `ChatViewModelStore.resolveSessionId(…)` *before* consuming it, and must
       still assign `currentChatSessionId = sid`. The route arg stays on
       `__new__<uuid>` after draft promotion, so the raw read handed
       SessionActivityTracker an orphan draft session and made a crash restore
       synthesize `OpenSession("__new__…")` — the user came back to a synthetic
       draft instead of the conversation.
   11. `onSaveInstanceState` must put the *resolved* id into the bundle
       (`KEY_CURRENT_CHAT_SESSION_ID`) for the same reason: it is the value
       `restoredChatSessionId` replays as a synthesized deep link.
   Both criteria are fail-closed on a missing/renamed anchor and are scoped to
   the block they name — a legitimate bare `getString("sessionId")` route-arg
   read elsewhere in the file must not trip them.
"""
import argparse
import re
import sys
import tempfile
from pathlib import Path

MARKER = "CHAT_SESSION_ID_BINDING_SELFTEST=PASS"

REL_SCREEN = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatScreen.kt"
REL_NAV = "src/android/app/src/main/java/com/rikkaminis/app/ui/navigation/AppNavigation.kt"
REL_MOVE = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/MoveToSessionSheet.kt"
REL_STORE = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatViewModelStore.kt"
# [da1029-A] criterion 9 — optional (mini-tree fixtures predate it)
REL_ACTIVITY = "src/android/app/src/main/java/com/rikkaminis/app/MainActivity.kt"
# [deep-audit-1039 A-06] criterion 13 — optional, same stance as criterion 9:
# the mini-tree fixtures that predate it write only the four files above.
REL_RUNNER = "src/android/app/src/main/java/com/rikkaminis/app/debug/HeadlessChatRunner.kt"

# (start token, end token) pairs isolating a call block; the block must
# contain `required` and must NOT contain `forbidden`.
SCREEN_CALL_SPECS = [
    (
        "ChatInputArea(",
        ")",
        {"required": "sessionId = viewModel.currentSessionId", "label": "ChatInputArea sessionId"},
    ),
    (
        "WebAppSource.ChatAttachment(",
        ")",
        {"required": "sessionId = viewModel.currentSessionId", "label": "WebAppSource.ChatAttachment sessionId"},
    ),
]

errors: "list[str]" = []


def strip_comments(text: str) -> str:
    """String-literal-aware comment stripper.

    The naive `re.sub(r"/\\*.*?\\*/", "", text, flags=re.S)` is unusable here:
    a `/*` inside a string literal plus a later `*/` swallows the whole body of
    the file (measured: 319,632 B -> 51,494 B on ChatScreen.kt), which silently
    turns every criterion into a false RED. This scanner walks the text,
    skipping string/char literals verbatim, so only real comments vanish.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            if text.startswith('"""', i):
                j = text.find('"""', i + 3)
                j = n if j == -1 else j + 3
            else:
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == '"' or text[j] == "\n":
                        j += 1
                        break
                    j += 1
            out.append(text[i:j])
            i = j
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    j += 1
                    break
                if text[j] == "\n":
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def extract_call_block(code: str, start: str, end: str) -> str:
    idx = code.find(start)
    if idx == -1:
        return ""
    return code[idx : code.find(end, idx) + len(end)] if end else code[idx:]


def _skip_string_literal(code: str, i: int) -> int:
    """Index just past the string literal starting at `code[i]` (a quote).

    Brace matching must not be fooled by `{`/`}` inside literals — the pinned
    block legitimately contains `"${pending.resourcePath}"` and warning text
    with interpolations. Mirrors the literal handling in [strip_comments].
    """
    quote = code[i]
    if quote == '"' and code.startswith('"""', i):
        j = code.find('"""', i + 3)
        return len(code) if j == -1 else j + 3
    j = i + 1
    n = len(code)
    while j < n:
        if code[j] == "\\":
            j += 2
            continue
        if code[j] == quote or code[j] == "\n":
            return j + 1
        j += 1
    return n


def extract_braced_block(code: str, decl_re) -> "str | None":
    """Body of the `{ … }` block whose opening brace ends `decl_re`'s match.

    Fail-closed: `None` when the anchor is missing or the braces do not
    balance, so a renamed/relocated anchor surfaces as a violation instead of
    silently skipping the criterion.
    """
    m = decl_re.search(code)
    if not m:
        return None
    open_idx = m.end() - 1
    if code[open_idx] != "{":
        return None
    depth = 0
    i = open_idx
    n = len(code)
    while i < n:
        c = code[i]
        if c == '"' or c == "'":
            i = _skip_string_literal(code, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return code[open_idx:i + 1]
        i += 1
    return None


# [da1032-D] anchors for MainActivity criteria 10/11.
RE_NAV_FLOW_COLLECT = re.compile(r"currentBackStackEntryFlow\s*\.\s*collect\s*\{")
RE_SAVE_INSTANCE = re.compile(r"override\s+fun\s+onSaveInstanceState\s*\(\s*outState\s*:\s*Bundle\s*\)\s*\{")
# A resolved chat id: the alias hop itself, or the store's already-resolved
# "chat currently on screen" seam.
RE_RESOLVED_EXPR = re.compile(
    r"ChatViewModelStore\s*\.\s*(?:resolveSessionId\s*\(|activeSessionId)"
)
# Continuation heuristics for a multi-line binding (`val sid = a\n  ?.let { … }`).
_CONT_SUFFIX = ("=", "?.", ".", ",", "&&", "||", "?:", "+")
_CONT_PREFIX = ("?.", ".", "?:", "&&", "||", "->", "+")


def _joined_statement(lines, i):
    """`lines[i]` plus its continuation lines, joined."""
    out = [lines[i]]
    while i + 1 < len(lines):
        cur = out[-1].rstrip()
        nxt = lines[i + 1].strip()
        if not nxt:
            break
        if cur.endswith(_CONT_SUFFIX) or nxt.startswith(_CONT_PREFIX):
            out.append(nxt)
            i += 1
            continue
        break
    return " ".join(out)


def resolved_names(text):
    """Local vals bound from a resolved-session expression inside `text`."""
    names = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"\s*(?:val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and RE_RESOLVED_EXPR.search(_joined_statement(lines, i)):
            names.add(m.group(1))
    return names


def extract_effect_block(code: str, marker: str) -> "tuple[str, str] | None":
    """Isolate the `LaunchedEffect(<keys>) { <body> }` block containing [marker].

    Returns `(keys, body)` — the key-list text and the braced body — or `None`
    when the marker is absent, no enclosing `LaunchedEffect(` precedes it, or
    the braces do not balance. Callers must treat `None` as a violation
    (fail-closed): a renamed or relocated block must never silently pass.
    """
    idx = code.find(marker)
    if idx == -1:
        return None
    start = code.rfind("LaunchedEffect(", 0, idx)
    if start == -1:
        return None
    brace = code.find("{", start)
    if brace == -1 or brace > idx:
        return None
    depth = 0
    i = brace
    n = len(code)
    while i < n:
        c = code[i]
        if c == '"' or c == "'":
            i = _skip_string_literal(code, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return code[start + len("LaunchedEffect(") : brace], code[brace : i + 1]
        i += 1
    return None


RE_START_DEST = re.compile(r"\bval\s+startDestination\s*=")
RE_REMEMBER_CALL = re.compile(r"remember\s*\(([^)]*)\)\s*\{")
RE_IDENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)")


def _match_brace(code: str, open_idx: int) -> "int | None":
    """Index of the `}` closing the `{` at [open_idx] (string-literal aware)."""
    if open_idx < 0 or open_idx >= len(code) or code[open_idx] != "{":
        return None
    depth = 0
    i = open_idx
    n = len(code)
    while i < n:
        c = code[i]
        if c == '"' or c == "'":
            i = _skip_string_literal(code, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


# [deep-audit-1039 A-06] anchors for criterion 13 (the headless cancel path).
RE_RUNNER_CANCEL_DECL = re.compile(r"suspend\s+fun\s+cancel\s*\(")
RE_RUNNER_RESOLVER_DECL = re.compile(r"private\s+fun\s+viewModelForCancel\s*\(")


def _fn_body_after(code: str, decl_re) -> "str | None":
    """`{ … }` body of the first function whose declaration matches [decl_re].

    Scans past the parameter list, then brace-matches the first `{` after it, so
    BOTH writing styles are accepted — a block body, and an expression body whose
    trailing lambda IS the body (`… = withContext(Dispatchers.Main) { }`). The
    criterion binds the decision point, not one spelling. Fail-closed: `None`
    when the declaration is absent or the braces do not balance.
    """
    m = decl_re.search(code)
    if not m:
        return None
    i = code.find("(", m.end() - 1)
    if i == -1:
        return None
    depth = 0
    n = len(code)
    while i < n:
        c = code[i]
        if c == '"' or c == "'":
            i = _skip_string_literal(code, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    open_idx = code.find("{", i)
    if open_idx == -1:
        return None
    end = _match_brace(code, open_idx)
    return None if end is None else code[open_idx : end + 1]


def _remember_derived_body(code: str, expr_start: int) -> "str | None":
    """Braced body of the `remember(…) { … }` behind a binding's right-hand side.

    Two legal shapes are accepted: the remember call written inline
    (`val startDestination = remember(…) { … }`), or a name bound from one
    (`val plan = remember(…) { … }` → `val startDestination = plan.field`).
    `None` when neither resolves — callers must fail closed.
    """
    line_end = code.find("\n", expr_start)
    raw = code[expr_start:] if line_end == -1 else code[expr_start:line_end]
    rhs = raw.strip()
    m = RE_REMEMBER_CALL.match(rhs)
    if m:
        brace = code.find("{", expr_start + (len(raw) - len(raw.lstrip())))
        end = _match_brace(code, brace)
        return None if end is None else code[brace:end + 1]
    m2 = RE_IDENT.match(rhs)
    if not m2:
        return None
    dm = re.search(
        r"\bval\s+%s\s*=\s*remember\s*\([^)]*\)\s*\{" % re.escape(m2.group(1)), code
    )
    if not dm:
        return None
    end = _match_brace(code, dm.end() - 1)
    return None if end is None else code[dm.end() - 1 : end + 1]


def _holder_spans(code: str) -> "list[tuple[int, int]]":
    """Spans of every `remember(…) { … }` / `LaunchedEffect(…) { … }` block.

    Used to answer "does this call sit at the composition top level?" — the
    one evaluation shape that is NOT single-shot.
    """
    spans = []
    for anchor in ("remember(", "LaunchedEffect("):
        idx = 0
        while True:
            i = code.find(anchor, idx)
            if i == -1:
                break
            brace = code.find("{", i)
            if brace == -1:
                break
            end = _match_brace(code, brace)
            if end is None:
                break
            spans.append((i, end + 1))
            idx = end + 1
    return spans


def check(root: Path) -> int:
    del errors[:]
    screen_path = root / REL_SCREEN
    nav_path = root / REL_NAV
    move_path = root / REL_MOVE
    store_path = root / REL_STORE
    for p in (screen_path, nav_path, move_path, store_path):
        if not p.is_file():
            print(f"FAIL {p} not found (wrong root?)")
            return 1

    screen = strip_comments(screen_path.read_text(encoding="utf-8"))
    nav = strip_comments(nav_path.read_text(encoding="utf-8"))
    move = strip_comments(move_path.read_text(encoding="utf-8"))
    store = strip_comments(store_path.read_text(encoding="utf-8"))

    # --- 1. markdown image tap handler ---
    if "remember<(String, String) -> Unit>(messages, viewModel.currentSessionId)" not in screen:
        errors.append(
            "ChatScreen.kt: markdown image tap handler must be keyed "
            "`remember<(String, String) -> Unit>(messages, viewModel.currentSessionId)`"
        )
    if re.search(r"resolveMdMediaFile\(\s*context,\s*ref\.source,\s*sessionId\s*\)", screen):
        errors.append(
            "ChatScreen.kt: resolveMdMediaFile must not receive the bare route `sessionId`"
        )
    if not re.search(r"resolveMdMediaFile\(\s*context,\s*ref\.source,\s*viewModel\.currentSessionId\s*\)", screen):
        errors.append(
            "ChatScreen.kt: resolveMdMediaFile must resolve media via `viewModel.currentSessionId`"
        )

    # --- 2. ChatHistoryDrawer select comparisons ---
    if re.search(r"if \(id (!=|==) sessionId\)", screen):
        errors.append(
            "ChatScreen.kt: drawer select must compare against `viewModel.currentSessionId`, "
            "not the bare route `sessionId` (route stays on the draft key after promotion)"
        )
    for op in ("!=", "=="):
        if f"if (id {op} viewModel.currentSessionId)" not in screen:
            errors.append(f"ChatScreen.kt: expected drawer comparison `if (id {op} viewModel.currentSessionId)`")

    # --- 3/4. call-block arguments ---
    for start, end, spec in SCREEN_CALL_SPECS:
        block = extract_call_block(screen, start, end)
        if not block:
            errors.append(f"ChatScreen.kt: call site `{start}` not found")
            continue
        if spec["required"] not in block:
            errors.append(
                f"ChatScreen.kt: `{spec['label']}` must pass `sessionId = viewModel.currentSessionId`"
            )

    # --- 5. AppNavigation route builders resolve through the alias table ---
    for label, needle in (
        ("onOpenTerminal", "Routes.terminal(sessionId = ChatViewModelStore.resolveSessionId(sessionId))"),
        ("onOpenTerminalWithCommand", "sessionId = ChatViewModelStore.resolveSessionId(sessionId)"),
        ("onBrowseChatFiles", "Routes.chatFiles(ChatViewModelStore.resolveSessionId(sessionId))"),
    ):
        if needle not in nav:
            errors.append(
                f"AppNavigation.kt: {label} must resolve the nav-arg id via "
                "`ChatViewModelStore.resolveSessionId(sessionId)` before navigating"
            )
    for label, forbidden in (
        ("onOpenTerminal", "Routes.terminal(sessionId = sessionId)"),
        ("onBrowseChatFiles", "Routes.chatFiles(sessionId)"),
    ):
        if forbidden in nav:
            errors.append(f"AppNavigation.kt: {label} navigates with the bare route `sessionId`")

    # --- 6. MoveToSessionSheet effect key ---
    if "LaunchedEffect(currentSessionId)" not in move:
        errors.append(
            "MoveToSessionSheet.kt: session list effect must be keyed `LaunchedEffect(currentSessionId)`"
        )
    if re.search(r"LaunchedEffect\(Unit\)", move):
        errors.append("MoveToSessionSheet.kt: stale `LaunchedEffect(Unit)` still present")

    # --- 7. the public seam must delegate, resolveKey must stay private ---
    if "fun resolveSessionId(sessionId: String): String" not in store:
        errors.append("ChatViewModelStore.kt: public `resolveSessionId` seam missing")
    if "resolveSessionId(sessionId: String): String =\n        resolveKey(sessionId)" not in store.replace("\r", "") and \
       "resolveSessionId(sessionId: String): String = resolveKey(sessionId)" not in store:
        errors.append(
            "ChatViewModelStore.kt: resolveSessionId must delegate to resolveKey "
            "(single alias table — no second lookup implementation)"
        )
    if "private fun resolveKey(sessionId: String)" not in store:
        errors.append(
            "ChatViewModelStore.kt: `private fun resolveKey(sessionId: String)` form changed "
            "(katex_ready_race_check.py pins it)"
        )

    # --- 8. pinned-shortcut HTML preview effect (deep-audit-1027 ISSUE-01) ---
    # Anchored on the consume call so a renamed/moved block is RED, not skipped.
    effect = extract_effect_block(screen, "consumePendingHtmlPreview()")
    if effect is None:
        errors.append(
            "ChatScreen.kt: pinned-shortcut block anchored on "
            "`consumePendingHtmlPreview()` is not inside a `LaunchedEffect` "
            "(renamed or moved? re-anchor this criterion, do not drop it)"
        )
    else:
        effect_keys, effect_body = effect
        if "viewModel.currentSessionId" not in effect_keys:
            errors.append(
                "ChatScreen.kt: pinned-shortcut LaunchedEffect must be keyed on "
                "`viewModel.currentSessionId` so a promoted draft re-runs it"
            )
        if "pendingHtmlPreviewState" not in effect_keys:
            errors.append(
                "ChatScreen.kt: pinned-shortcut LaunchedEffect must observe the pending "
                "state (`pendingHtmlPreviewState` key + `collectAsState()`), not a "
                "one-shot `.value` read — a hot-path deep link reusing the live "
                "ChatScreen would otherwise never re-run the effect"
            )
        if "collectAsState()" not in screen:
            errors.append("ChatScreen.kt: pinned-shortcut pending state is not collected")
        if "resolvePathForSession(" not in effect_body:
            errors.append(
                "ChatScreen.kt: pinned-shortcut must resolve via "
                "`PRootKernel.resolvePathForSession(activeSessionId, …)`"
            )
        if "resolveHostPath(" in effect_body:
            errors.append(
                "ChatScreen.kt: pinned-shortcut must not resolve through the global "
                "`resolveHostPath` (scans every session tree; needs boot; cross-session reads)"
            )
        if (
            "viewModel.currentSessionId" not in effect_body
            and "ChatViewModelStore.resolveSessionId(" not in effect_body
        ):
            errors.append(
                "ChatScreen.kt: pinned-shortcut must bind the session through "
                "`viewModel.currentSessionId` (or `ChatViewModelStore.resolveSessionId(…)`)"
            )
        if "pending.sessionId != activeSessionId" not in effect_body:
            errors.append(
                "ChatScreen.kt: pinned-shortcut must accept the live session id "
                "(`pending.sessionId != activeSessionId`), not the route arg alone"
            )

    # --- 9. [da1029-A] MainActivity's New-Chat entries resolve the draft id
    #     through the single source (ComposerDraftStore.nextDraftId) ---
    # Both entries (onStart resume for launch-mode 2, and handleDeepLink's
    # NewChat/NewCameraChat branch) used to hand-build `"__new__<uuid>"`.
    # A fresh uuid orphans the live draft slot, and ComposerDraftStore.saveText
    # is a deliberate no-op for a slot that belongs to a different draft id —
    # so the typed-but-unsent text was silently thrown away. Same family as
    # criteria 1-4: an entry point bypassing the one source of truth.
    #
    # Optional file: the pre-da1029-0/A mini-tree fixtures write only the four
    # files above, so a missing activity source is a skip (drift-detector
    # stance), not a failure. Presence is required by the real repo tree, which
    # the branch CI always runs.
    act_path = root / REL_ACTIVITY
    if act_path.is_file():
        activity = strip_comments(act_path.read_text(encoding="utf-8"))
        bare = activity.count("__new__${")
        if bare:
            errors.append(
                f"MainActivity.kt: {bare} bare `\"__new__${{...}}\"` draft-id "
                "construction(s) — New-Chat entries must resolve through "
                "`ComposerDraftStore.nextDraftId(this)` (a fresh uuid orphans the "
                "live draft and its text is dropped by saveText's slot guard)"
            )
        resolved = activity.count("ComposerDraftStore.nextDraftId(")
        if resolved < 2:
            errors.append(
                "MainActivity.kt: expected >= 2 `ComposerDraftStore.nextDraftId(` "
                f"call sites (onStart resume + handleDeepLink quick actions), found {resolved}"
            )

        # --- 10. [da1032-D] the nav back-stack listener resolves the chat id
        #     before consuming it (keep-alive presence / currentChatSessionId /
        #     crash-recovery prefs all sit downstream of this one read). ---
        listener = extract_braced_block(activity, RE_NAV_FLOW_COLLECT)
        if listener is None:
            errors.append(
                "MainActivity.kt: `currentBackStackEntryFlow.collect { … }` block "
                "not found (renamed or moved? re-anchor criterion 10, do not drop it)"
            )
        else:
            if "getString(\"sessionId\")" not in listener:
                errors.append(
                    "MainActivity.kt: the nav listener no longer reads the route "
                    "`sessionId` — re-anchor criterion 10 rather than letting it pass"
                )
            names = resolved_names(listener)
            field_writes = re.findall(r"currentChatSessionId\s*=\s*([^\n]+)", listener)
            if not field_writes:
                errors.append(
                    "MainActivity.kt: the nav listener must keep assigning "
                    "`currentChatSessionId = sid` (everything downstream — including "
                    "onSaveInstanceState and the crash-recovery prefs — reads it)"
                )
            for rhs in field_writes:
                rhs = rhs.strip()
                ok = RE_RESOLVED_EXPR.search(rhs) is not None or any(
                    re.search(r"\b%s\b" % re.escape(n), rhs) for n in names
                )
                if not ok:
                    errors.append(
                        "MainActivity.kt: the nav listener assigns `currentChatSessionId` "
                        "from an unresolved value (%r) — the route arg stays on "
                        "`__new__<uuid>` after draft promotion, so keep-alive presence and "
                        "crash recovery would track an orphan draft. Resolve through "
                        "`ChatViewModelStore.resolveSessionId(…)` first." % rhs[:120]
                    )
            if "resolveSessionId(" not in listener:
                errors.append(
                    "MainActivity.kt: the nav listener must resolve the route `sessionId` "
                    "through `ChatViewModelStore.resolveSessionId(…)` (no alias hop in the "
                    "listener body at all)"
                )

        # --- 11. [da1032-D] the saved-state bundle carries the resolved id ---
        saver = extract_braced_block(activity, RE_SAVE_INSTANCE)
        if saver is None:
            errors.append(
                "MainActivity.kt: `override fun onSaveInstanceState(outState: Bundle) { … }` "
                "not found (renamed or moved? re-anchor criterion 11, do not drop it)"
            )
        else:
            if "KEY_CURRENT_CHAT_SESSION_ID" not in saver:
                errors.append(
                    "MainActivity.kt: onSaveInstanceState no longer persists "
                    "`KEY_CURRENT_CHAT_SESSION_ID` (crash/LMK restore relies on it)"
                )
            elif "resolveSessionId(" not in saver:
                errors.append(
                    "MainActivity.kt: onSaveInstanceState must persist the RESOLVED id "
                    "(`ChatViewModelStore.resolveSessionId(…)`) — the bundle value is "
                    "replayed as a synthesized OpenSession deep link, so a draft key "
                    "lands the user in a synthetic draft"
                )

    # --- 12. [deep-audit-1038 B-02] the cold-start route decision is
    #     SINGLE-SHOT: it must be derived from a `remember(…)` block, never
    #     evaluated bare at the composition top level. ---
    # `AppNavigation` recomposes on theme / font-size / system dark-mode
    # changes (it reads MaterialTheme.colorScheme.background for the NavHost
    # transition surface), so a bare `val startDestination = when { … }`
    # re-ran on every recomposition: the pending camera / HTML-preview action
    # was replayed into a live ChatScreen (reactively consumed since
    # da1028-C) and the draft id was re-minted once the slot was free —
    # `startDestination` drift (NavGraph rebuild) plus saveText's slot guard
    # silently dropping the draft text the user was typing.
    #
    # Criterion 9 covers the draft-id SINGLE SOURCE (which factory); this one
    # covers its SINGLE EVALUATION POINT (once per cold start). Both legal
    # shapes are accepted — the remember call written inline, or a val bound
    # from a remember-derived plan — so the criterion binds the decision
    # point, not one writing style. Fail-closed on a missing / duplicated
    # `val startDestination =` anchor.
    dest_hits = list(RE_START_DEST.finditer(nav))
    if len(dest_hits) != 1:
        errors.append(
            "AppNavigation.kt: expected exactly 1 `val startDestination =` cold-start "
            f"binding, found {len(dest_hits)} (renamed or restructured? re-anchor "
            "criterion 12, do not drop it)"
        )
    else:
        dest_body = _remember_derived_body(nav, dest_hits[0].end())
        if dest_body is None:
            errors.append(
                "AppNavigation.kt: the cold-start `startDestination` is not derived from "
                "a `remember(…)` block — a bare evaluation re-runs on every "
                "recomposition, re-playing the pending camera / preview action into a "
                "live ChatScreen and re-minting the draft id (`startDestination` drift; "
                "the typed draft is dropped by saveText's slot guard). Wrap the decision "
                "in `remember(initialDeepLink) { … }`."
            )
        else:
            missing = []
            if "nextDraftId" not in dest_body:
                missing.append("draft-id resolution (`nextDraftId`)")
            if "setPending" not in dest_body:
                missing.append("pending-state seeding (`setPending…`)")
            if missing:
                errors.append(
                    "AppNavigation.kt: the `remember(…)`-derived startDestination does "
                    "not carry " + " / ".join(missing) + " — the cold-start decision "
                    "moved out of the single-shot block (re-anchor criterion 12, do "
                    "not drop it)"
                )

    # 12b — no pending seed may run bare at the composition top level either:
    # a seed re-added outside the remember block has the same replay defect
    # even while `startDestination` itself stays inside it.
    holder_spans = _holder_spans(nav)
    for call in (
        "DeepLinkCoordinator.setPendingChatAction(",
        "DeepLinkCoordinator.setPendingHtmlPreview(",
    ):
        start = 0
        while True:
            j = nav.find(call, start)
            if j == -1:
                break
            if not any(s <= j < e for s, e in holder_spans):
                errors.append(
                    f"AppNavigation.kt: `{call[:-1]}…)` runs at the composition top "
                    "level — the cold-start pending seed must sit inside the single-shot "
                    "`remember(initialDeepLink)` block (or a keyed `LaunchedEffect`), "
                    "otherwise every recomposition re-plays it into a live ChatScreen"
                )
            start = j + 1

    # --- 13. [deep-audit-1039 A-06] `cancel` resolves the ViewModel through the
    #     process-wide store, not just the private provider cache. ---
    # The cache is written at `providerFor`, i.e. by the RPC path only. A session
    # whose ViewModel the UI created (the ordinary case — the user opens the chat,
    # then asks the agent over RPC to stop it) is invisible to that cache, so the
    # old `cancel` read `false`, stopped nothing, and the stream kept running
    # against a user who had just pressed stop. `deleteSession` calls cancel
    # before dropping the row, so the same hole silently dropped its cancel.
    #
    # Optional file: the four-file mini-tree fixtures predate it (drift-detector
    # stance, same as criteria 9-11). The real repo tree always carries it, and
    # the branch CI runs the real tree, so the criterion is live there.
    runner_path = root / REL_RUNNER
    if runner_path.is_file():
        runner = strip_comments(runner_path.read_text(encoding="utf-8"))
        cancel_body = _fn_body_after(runner, RE_RUNNER_CANCEL_DECL)
        if cancel_body is None:
            errors.append(
                "HeadlessChatRunner.kt: `suspend fun cancel(` block not found "
                "(renamed or moved? re-anchor criterion 13, do not drop it)"
            )
        else:
            if "providers[" in cancel_body:
                errors.append(
                    "HeadlessChatRunner.kt: `cancel` reads the private provider cache "
                    "directly — that cache is written by the RPC path only, so a "
                    "UI-created ViewModel is invisible, nothing is stopped, and cancel "
                    "reports false. Resolve through `viewModelForCancel(…)`."
                )
            if "viewModelForCancel(" not in cancel_body:
                errors.append(
                    "HeadlessChatRunner.kt: `cancel` must resolve the ViewModel through "
                    "`viewModelForCancel(…)` — cache fast path plus store fallback in ONE "
                    "place, so the resolution order is a property of cancel alone"
                )
            if "isStreaming.value" not in cancel_body or "cancelStream()" not in cancel_body:
                errors.append(
                    "HeadlessChatRunner.kt: `cancel` no longer reads `isStreaming.value` / "
                    "calls `cancelStream()` — the return contract (false when nothing was "
                    "streaming) is part of the RPC surface"
                )
        resolver_body = _fn_body_after(runner, RE_RUNNER_RESOLVER_DECL)
        if resolver_body is None:
            errors.append(
                "HeadlessChatRunner.kt: `private fun viewModelForCancel(` block not found "
                "(renamed or moved? re-anchor criterion 13, do not drop it)"
            )
        else:
            if "ChatViewModelStore.ownerFor(" not in resolver_body:
                errors.append(
                    "HeadlessChatRunner.kt: the cancel resolver must fall back to "
                    "`ChatViewModelStore.ownerFor(…)` — that is the store the UI binds to"
                )
            if "ChatViewModelStore.resolveSessionId(" not in resolver_body:
                errors.append(
                    "HeadlessChatRunner.kt: the resolver must build the ViewModel with the "
                    "RESOLVED id (`ChatViewModelStore.resolveSessionId(…)`) — the store's "
                    "live VM sits under the promoted id, so the raw `__new__<uuid>` draft "
                    "key would create a second ViewModel"
                )
            if "providers[" not in resolver_body:
                errors.append(
                    "HeadlessChatRunner.kt: the resolver dropped the private-cache fast "
                    "path — RPC-driven sessions would rebuild their ViewModel on every "
                    "cancel"
                )

    if errors:
        for e in errors:
            print(f"FAIL {e}")
        print(f"\ngate RED: {len(errors)} violation(s)")
        return 1
    print("chat_session_id_binding_check: all criteria satisfied")
    return 0


# ---------------- selftest fixtures (minimal synthetic repo) ----------------

FIXTURE_STORE = '''package com.rikkaminis.app.ui.chat

object ChatViewModelStore {
    private val aliases = mutableMapOf<String, String>()

    @Synchronized
    private fun resolveKey(sessionId: String): String =
        aliases[sessionId] ?: sessionId

    @Synchronized
    fun resolveSessionId(sessionId: String): String =
        resolveKey(sessionId)
}
'''

# [da1029-A] criterion 9 fixture: MainActivity's two New-Chat entry points.
# [da1032-D] criteria 10/11 fixture: the nav back-stack listener and the
# saved-state bundle.
FIXTURE_ACTIVITY = '''package com.rikkaminis.app

class MainActivity {
    private var currentChatSessionId: String? = null

    override fun onStart() {
        val newRoute = Routes.chat(com.rikkaminis.app.data.ComposerDraftStore.nextDraftId(this))
        navController.currentBackStackEntryFlow.collect { entry ->
            val isChatRoute = entry.destination.route == Routes.CHAT
            val sid = entry.arguments?.getString("sessionId")
                ?.takeIf { isChatRoute }
                ?.let { ChatViewModelStore.resolveSessionId(it) }
            if (sid != currentChatSessionId) {
                currentChatSessionId = sid
            }
        }
    }

    private fun handleDeepLink() {
        val newRoute = Routes.chat(com.rikkaminis.app.data.ComposerDraftStore.nextDraftId(this))
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        currentChatSessionId?.let {
            outState.putString(KEY_CURRENT_CHAT_SESSION_ID, ChatViewModelStore.resolveSessionId(it))
        }
    }
}
'''

# Precision arm: a legitimate bare `getString("sessionId")` route-arg read
# elsewhere in the file (the terminal destination reads its own nav arg) must
# not trip criteria 10/11 — they are scoped to the listener / save block.
FIXTURE_ACTIVITY_PRECISION = FIXTURE_ACTIVITY.replace(
    "    private fun handleDeepLink() {",
    "    private fun terminalArgs(backStackEntry: NavBackStackEntry) {\n"
    "        val sessionId = backStackEntry.arguments?.getString(\"sessionId\")\n"
    "    }\n\n"
    "    private fun handleDeepLink() {",
)

# [deep-audit-1038 B-02] criterion 12 fixtures: the cold-start route decision.
# Shape A — the remember call assigned inline (the real-tree form).
COLD_START_SHAPE_A = (
    "val startDestination = remember(initialDeepLink) {\n"
    "    coldStartStartDestination(\n"
    "        initialDeepLink = initialDeepLink,\n"
    "        nextDraftId = { com.rikkaminis.app.data.ComposerDraftStore.nextDraftId(context) },\n"
    "        chatRoute = { Routes.chat(it) },\n"
    "        seedCamera = {\n"
    "            DeepLinkCoordinator.setPendingChatAction(\n"
    "                DeepLinkCoordinator.ChatAction.OPEN_CAMERA,\n"
    "            )\n"
    "        },\n"
    "        seedHtmlPreview = { sessionId, resourcePath, title ->\n"
    "            DeepLinkCoordinator.setPendingHtmlPreview(sessionId, resourcePath, title)\n"
    "        },\n"
    "    )\n"
    "}\n"
)

# Shape B — a plan bound from remember, then a field projection. Criterion 12
# must accept it too (decision point, not one writing style).
COLD_START_SHAPE_B = (
    "val coldStartPlan = remember(initialDeepLink) {\n"
    "    coldStartStartDestination(\n"
    "        initialDeepLink = initialDeepLink,\n"
    "        nextDraftId = { com.rikkaminis.app.data.ComposerDraftStore.nextDraftId(context) },\n"
    "        chatRoute = { Routes.chat(it) },\n"
    "        seedCamera = { DeepLinkCoordinator.setPendingChatAction(DeepLinkCoordinator.ChatAction.OPEN_CAMERA) },\n"
    "        seedHtmlPreview = { s, p, t -> DeepLinkCoordinator.setPendingHtmlPreview(s, p, t) },\n"
    "    )\n"
    "}\n"
    "val startDestination = coldStartPlan\n"
)

# [deep-audit-1039 A-06] criterion 13 fixture: the headless cancel path. Faithful
# to the production shape (cache fast path first, then the store fallback), but
# free-standing — the fixture is never compiled.
FIXTURE_RUNNER = '''package com.rikkaminis.app.debug

class HeadlessChatRunner {
    private val providers = mutableMapOf<String, Any>()

    suspend fun cancel(context: Context, sessionId: String): Boolean = withContext(Dispatchers.Main) {
        val vm = viewModelForCancel(context, sessionId) ?: return@withContext false
        val wasRunning = vm.isStreaming.value
        if (wasRunning) vm.cancelStream()
        wasRunning
    }

    private fun viewModelForCancel(context: Context, sessionId: String): ChatViewModel? {
        providers[sessionId]?.let { cached ->
            runCatching { cached[ChatViewModel::class.java] }.getOrNull()?.let { return it }
        }
        val owner = runCatching { ChatViewModelStore.ownerFor(sessionId) }.getOrNull() ?: return null
        val app = app(context)
        return runCatching {
            ViewModelProvider(
                owner,
                ChatViewModel.factory(
                    sessionId = ChatViewModelStore.resolveSessionId(sessionId),
                ),
            )[ChatViewModel::class.java]
        }.getOrNull()
    }
}
'''

_R_CANCEL_CALL = ("        val vm = viewModelForCancel(context, sessionId) "
                  "?: return@withContext false")
_R_OWNER_LINE = ("        val owner = runCatching { "
                 "ChatViewModelStore.ownerFor(sessionId) }.getOrNull() ?: return null\n")
_R_RESOLVED_ARG = "sessionId = ChatViewModelStore.resolveSessionId(sessionId),"
_R_CACHE_BLOCK = ("        providers[sessionId]?.let { cached ->\n"
                  "            runCatching { cached[ChatViewModel::class.java] }"
                  ".getOrNull()?.let { return it }\n"
                  "        }\n")

# Dirty variants, one per decision point (built fail-closed: a variant that did
# not change the fixture is a broken mutation, not a passing one).
FIXTURE_RUNNER_CACHE_ONLY = FIXTURE_RUNNER.replace(
    _R_CANCEL_CALL,
    "        val vm = providers[sessionId] as? ChatViewModel "
    "?: return@withContext false",
)
FIXTURE_RUNNER_NO_STORE = FIXTURE_RUNNER.replace(_R_OWNER_LINE, "")
FIXTURE_RUNNER_RAW_KEY = FIXTURE_RUNNER.replace(_R_RESOLVED_ARG, "sessionId = sessionId,")
FIXTURE_RUNNER_NO_FASTPATH = FIXTURE_RUNNER.replace(_R_CACHE_BLOCK, "")
FIXTURE_RUNNER_RENAMED = FIXTURE_RUNNER.replace("suspend fun cancel(", "suspend fun abort(")

_RUNNER_MUTATIONS = (
    ("cancel back to the private cache only (MUT-R1)", FIXTURE_RUNNER_CACHE_ONLY, _R_CANCEL_CALL),
    ("resolver drops the store fallback (MUT-R2)", FIXTURE_RUNNER_NO_STORE, _R_OWNER_LINE),
    ("resolver builds the VM from the raw draft key (MUT-R3)",
     FIXTURE_RUNNER_RAW_KEY, _R_RESOLVED_ARG),
    ("resolver drops the cache fast path (MUT-R4)", FIXTURE_RUNNER_NO_FASTPATH, _R_CACHE_BLOCK),
    ("a renamed cancel anchor (MUT-R5)", FIXTURE_RUNNER_RENAMED, "suspend fun cancel("),
)

# Each mutation is (name, fixture-mutator) applied to the GREEN fixture set.
# The mutated tree MUST go red for the gate to have teeth.
MUTATIONS = [
    (
        "MUT-1 ChatInputArea back to bare route id",
        lambda screen, nav, move: (screen.replace(
            "sessionId = viewModel.currentSessionId", "sessionId = sessionId", 1), nav, move),
    ),
    (
        "MUT-2 drawer comparisons back to bare route id",
        lambda screen, nav, move: (re.sub(
            r"if \(id (!=|==) viewModel\.currentSessionId\)", r"if (id \1 sessionId)", screen), nav, move),
    ),
    (
        "MUT-3 AppNavigation onOpenTerminal back to bare route id",
        lambda screen, nav, move: (screen, nav.replace(
            "Routes.terminal(sessionId = ChatViewModelStore.resolveSessionId(sessionId))",
            "Routes.terminal(sessionId = sessionId)"), move),
    ),
    (
        "MUT-4 AppNavigation onBrowseChatFiles back to bare route id",
        lambda screen, nav, move: (screen, nav.replace(
            "Routes.chatFiles(ChatViewModelStore.resolveSessionId(sessionId))",
            "Routes.chatFiles(sessionId)"), move),
    ),
    (
        "MUT-5 MoveToSessionSheet effect key back to Unit",
        lambda screen, nav, move: (screen, nav, move.replace(
            "LaunchedEffect(currentSessionId)", "LaunchedEffect(Unit)")),
    ),
    (
        "MUT-6 resolveMdMediaFile back to bare route id",
        lambda screen, nav, move: (screen.replace(
            "resolveMdMediaFile(context, ref.source, viewModel.currentSessionId)",
            "resolveMdMediaFile(context, ref.source, sessionId)"), nav, move),
    ),
    (
        "MUT-7 ChatAttachment back to bare route id",
        lambda screen, nav, move: (screen.replace(
            "sessionId = viewModel.currentSessionId,\n    sessionTitle = null",
            "sessionId = sessionId,\n    sessionTitle = null"), nav, move),
    ),
    (
        "MUT-8 remember key back to bare route id",
        lambda screen, nav, move: (screen.replace(
            "remember<(String, String) -> Unit>(messages, viewModel.currentSessionId)",
            "remember<(String, String) -> Unit>(messages, sessionId)"), nav, move),
    ),
    (
        "MUT-11 pinned shortcut back to the global resolver",
        lambda screen, nav, move: (screen.replace(
            "PRootKernel.resolvePathForSession(\n        activeSessionId,",
            "PRootKernel.resolveHostPath(\n        activeSessionId,"), nav, move),
    ),
    (
        "MUT-12 pinned shortcut drops the live-session comparison",
        lambda screen, nav, move: (screen.replace(
            "if (pending.sessionId != sessionId && pending.sessionId != activeSessionId) {",
            "if (pending.sessionId != sessionId) {"), nav, move),
    ),
    (
        "MUT-13 pinned shortcut key drops viewModel.currentSessionId",
        lambda screen, nav, move: (screen.replace(
            "LaunchedEffect(sessionId, viewModel.currentSessionId, pendingHtmlPreviewState) {",
            "LaunchedEffect(sessionId, pendingHtmlPreviewState) {"), nav, move),
    ),
    (
        "MUT-14 pinned shortcut forgets the consume anchor",
        lambda screen, nav, move: (screen.replace(
            "consumePendingHtmlPreview()", "consumePreview()", 1), nav, move),
    ),
    (
        "MUT-15 pinned shortcut stops observing the pending state",
        lambda screen, nav, move: (screen.replace(
            "LaunchedEffect(sessionId, viewModel.currentSessionId, pendingHtmlPreviewState) {",
            "LaunchedEffect(sessionId, viewModel.currentSessionId) {"), nav, move),
    ),
    (
        # [deep-audit-1038 B-02] criterion 12: strip the remember wrapper —
        # the cold-start decision evaluates bare at composition top level.
        "MUT-22 cold-start startDestination back to a bare evaluation",
        lambda screen, nav, move: (screen, nav.replace(
            "val startDestination = remember(initialDeepLink) {",
            "val startDestination = run {", 1), move),
    ),
    (
        # [deep-audit-1038 B-02] criterion 12b: re-add a pending seed at the
        # composition top level — same replay defect, different route in.
        "MUT-23 a pending seed re-added at the composition top level",
        lambda screen, nav, move: (screen, nav + (
            "\nval replayedSeed = DeepLinkCoordinator.setPendingHtmlPreview(\"s\", \"/p\", \"t\")\n"),
         move),
    ),
]


def _green_fixtures() -> "tuple[str, str, str]":
    screen = (
        "val markdownImageTapHandler = remember<(String, String) -> Unit>(messages, viewModel.currentSessionId) {\n"
        "    val resolved = resolveMdMediaFile(context, ref.source, viewModel.currentSessionId)\n"
        "}\n"
        "if (id != viewModel.currentSessionId) { }\n"
        "if (id == viewModel.currentSessionId) { }\n"
        "ChatInputArea(\n"
        "    viewModel = viewModel,\n"
        "    sessionId = viewModel.currentSessionId,\n"
        ")\n"
        "source = com.rikkaminis.app.webapp.WebAppSource.ChatAttachment(\n"
        "    uri = target.uri,\n"
        "    fileName = target.fileName,\n"
        "    sessionId = viewModel.currentSessionId,\n"
        "    sessionTitle = null,\n"
        ")\n"
        # deep-audit-1027 ISSUE-01: pinned-shortcut effect (criterion 8).
        "val pendingHtmlPreviewState by com.rikkaminis.app.deeplink.DeepLinkCoordinator\n"
        "    .pendingHtmlPreview.collectAsState()\n"
        "LaunchedEffect(sessionId, viewModel.currentSessionId, pendingHtmlPreviewState) {\n"
        "    val pending = pendingHtmlPreviewState ?: return@LaunchedEffect\n"
        "    val activeSessionId = ChatViewModelStore.resolveSessionId(viewModel.currentSessionId)\n"
        "    if (pending.sessionId != sessionId && pending.sessionId != activeSessionId) {\n"
        "        return@LaunchedEffect\n"
        "    }\n"
        "    com.rikkaminis.app.deeplink.DeepLinkCoordinator.consumePendingHtmlPreview()\n"
        "    val file = com.rikkaminis.app.sandbox.PRootKernel.resolvePathForSession(\n"
        "        activeSessionId,\n"
        '        "/var/minis${pending.resourcePath}",\n'
        "        appCtx,\n"
        "    )\n"
        "}\n"
    )
    nav = (
        "onOpenTerminal = {\n"
        "    navController.safeNavigate(\n"
        "        Routes.terminal(sessionId = ChatViewModelStore.resolveSessionId(sessionId)),\n"
        "    )\n"
        "},\n"
        "onOpenTerminalWithCommand = { command ->\n"
        "    navController.safeNavigate(\n"
        "        Routes.terminal(\n"
        "            initCommand = command,\n"
        "            sessionId = ChatViewModelStore.resolveSessionId(sessionId),\n"
        "        ),\n"
        "    )\n"
        "},\n"
        "onBrowseChatFiles = {\n"
        "    navController.safeNavigate(\n"
        "        Routes.chatFiles(ChatViewModelStore.resolveSessionId(sessionId)),\n"
        "    )\n"
        "},\n"
        # [deep-audit-1038 B-02] criterion 12: the single-shot cold-start block.
    ) + COLD_START_SHAPE_A
    move = (
        "LaunchedEffect(currentSessionId) {\n"
        "    sessions = withContext(Dispatchers.IO) {\n"
        "        chatRepository.dao.listSessions().filter { it.id != currentSessionId }\n"
        "    }\n"
        "}\n"
    )
    return screen, nav, move


def selftest() -> int:
    import shutil

    failures = []

    def run_tree(screen: str, nav: str, move: str, store: str,
                 activity: "str | None" = None,
                 runner: "str | None" = None) -> "tuple[int, list[str]]":
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            for rel in (REL_SCREEN, REL_NAV, REL_MOVE, REL_STORE):
                f = tdp / rel
                f.parent.mkdir(parents=True, exist_ok=True)
            (tdp / REL_SCREEN).write_text(screen, encoding="utf-8")
            (tdp / REL_NAV).write_text(nav, encoding="utf-8")
            (tdp / REL_MOVE).write_text(move, encoding="utf-8")
            (tdp / REL_STORE).write_text(store, encoding="utf-8")
            if activity is not None:
                f = tdp / REL_ACTIVITY
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(activity, encoding="utf-8")
            if runner is not None:
                f = tdp / REL_RUNNER
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(runner, encoding="utf-8")
            rc = check(tdp)
            return rc, list(errors)

    screen, nav, move = _green_fixtures()

    # GREEN baseline
    rc, errs = run_tree(screen, nav, move, FIXTURE_STORE)
    if rc != 0:
        failures.append(f"GREEN fixture unexpectedly red: {errs}")
    else:
        print("selftest: GREEN fixture passes")

    # Mutations 1-8 mutate screen/nav/move
    for name, mut in MUTATIONS:
        s2, n2, m2 = mut(screen, nav, move)
        if name == "MUT-9 resolveKey widened to public":
            continue
        rc, errs = run_tree(s2, n2, m2, FIXTURE_STORE)
        if rc == 0:
            failures.append(f"{name}: gate stayed GREEN (no teeth)")
        else:
            print(f"selftest: {name} -> RED ({len(errs)} violation(s)) OK")

    # MUT-9: widened resolveKey must be caught by criterion 7
    bad_store = FIXTURE_STORE.replace(
        "private fun resolveKey", "fun resolveKey"
    )
    rc, errs = run_tree(screen, nav, move, bad_store)
    if rc == 0:
        failures.append("MUT-9 resolveKey widened to public: gate stayed GREEN (no teeth)")
    else:
        print(f"selftest: MUT-9 resolveKey widened to public -> RED ({len(errs)} violation(s)) OK")

    # MUT-10: resolveSessionId reimplemented without delegating (second lookup)
    dup_store = FIXTURE_STORE.replace(
        "fun resolveSessionId(sessionId: String): String =\n        resolveKey(sessionId)",
        "fun resolveSessionId(sessionId: String): String = aliases[sessionId] ?: sessionId",
    )
    rc, errs = run_tree(screen, nav, move, dup_store)
    if rc == 0:
        failures.append("MUT-10 resolveSessionId second implementation: gate stayed GREEN (no teeth)")
    else:
        print(f"selftest: MUT-10 non-delegating resolveSessionId -> RED ({len(errs)} violation(s)) OK")

    # --- criterion 9 [da1029-A]: MainActivity draft-id single source --------
    # Backward compat: the four-file trees above omit MainActivity.kt, and the
    # criterion must skip them (drift-detector stance) — already proven GREEN.
    print("selftest: criterion 9 skipped when MainActivity.kt absent (old fixtures) OK")

    rc, errs = run_tree(screen, nav, move, FIXTURE_STORE, FIXTURE_ACTIVITY)
    if rc != 0:
        failures.append(f"criterion 9 GREEN fixture unexpectedly red: {errs}")
    else:
        print("selftest: criterion 9 GREEN fixture passes")

    for name, act in (
        (
            "one bare __new__ uuid left (MUT-16)",
            FIXTURE_ACTIVITY.replace(
                "com.rikkaminis.app.data.ComposerDraftStore.nextDraftId(this)",
                '"__new__${java.util.UUID.randomUUID()}"', 1),
        ),
        (
            "both entries back to a bare uuid (MUT-17)",
            FIXTURE_ACTIVITY.replace(
                "com.rikkaminis.app.data.ComposerDraftStore.nextDraftId(this)",
                '"__new__${java.util.UUID.randomUUID()}"'),
        ),
    ):
        rc, errs = run_tree(screen, nav, move, FIXTURE_STORE, act)
        if rc == 0:
            failures.append(f"criterion 9 {name}: gate stayed GREEN (no teeth)")
        else:
            print(f"selftest: criterion 9 {name} -> RED ({len(errs)} violation(s)) OK")

    # --- criteria 10/11 [da1032-D]: resolved id at the nav listener / bundle ---
    rc, errs = run_tree(screen, nav, move, FIXTURE_STORE, FIXTURE_ACTIVITY_PRECISION)
    if rc != 0:
        failures.append(
            "criteria 10/11 precision fixture unexpectedly red (a bare route-arg read "
            f"elsewhere must stay legal): {errs}")
    else:
        print("selftest: criteria 10/11 precision fixture (unrelated bare route arg) passes")

    for name, act in (
        (
            "criterion 10 nav listener back to the bare route id (MUT-18)",
            FIXTURE_ACTIVITY.replace(
                "\n                ?.let { ChatViewModelStore.resolveSessionId(it) }", "", 1),
        ),
        (
            "criterion 10 listener assigns an unresolved value (MUT-20)",
            FIXTURE_ACTIVITY.replace(
                "                currentChatSessionId = sid",
                "                currentChatSessionId = entry.arguments?.getString(\"sessionId\")",
                1),
        ),
        (
            "criterion 11 saved-state back to the bare id (MUT-19)",
            FIXTURE_ACTIVITY.replace(
                "outState.putString(KEY_CURRENT_CHAT_SESSION_ID, "
                "ChatViewModelStore.resolveSessionId(it))",
                "outState.putString(KEY_CURRENT_CHAT_SESSION_ID, it)", 1),
        ),
        (
            "criterion 11 saved-state drops the bundle key (MUT-21)",
            FIXTURE_ACTIVITY.replace(
                "outState.putString(KEY_CURRENT_CHAT_SESSION_ID, "
                "ChatViewModelStore.resolveSessionId(it))",
                "outState.putString(KEY_OTHER, it)", 1),
        ),
    ):
        rc, errs = run_tree(screen, nav, move, FIXTURE_STORE, act)
        if rc == 0:
            failures.append(f"{name}: gate stayed GREEN (no teeth)")
        else:
            print(f"selftest: {name} -> RED ({len(errs)} violation(s)) OK")

    # fail-closed: a renamed anchor must NOT read as green
    rc, errs = run_tree(
        screen, nav, move, FIXTURE_STORE,
        FIXTURE_ACTIVITY.replace("currentBackStackEntryFlow", "currentBackStackEntry"))
    if rc == 0:
        failures.append("criterion 10 missing anchor: gate stayed GREEN (fail-closed violated)")
    else:
        print(f"selftest: criterion 10 missing anchor -> RED ({len(errs)} violation(s)) OK")

    # --- criterion 12 [deep-audit-1038 B-02]: single-shot cold start --------
    # Shape A is the GREEN fixture above (already exercised by every run above).
    # Shape B (plan bound from remember, then a projection) must also pass —
    # criterion 12 binds the decision point, not one writing style.
    rc, errs = run_tree(
        screen, nav.replace(COLD_START_SHAPE_A, COLD_START_SHAPE_B), move, FIXTURE_STORE)
    if rc != 0:
        failures.append(
            f"criterion 12 shape B (remember-derived plan) unexpectedly red: {errs}")
    else:
        print("selftest: criterion 12 shape B (remember-derived plan) passes")

    # fail-closed: a dropped `val startDestination =` anchor must NOT read green
    rc, errs = run_tree(
        screen, nav.replace("val startDestination =", "val dest ="), move, FIXTURE_STORE)
    if rc == 0:
        failures.append("criterion 12 missing anchor: gate stayed GREEN (fail-closed violated)")
    else:
        print(f"selftest: criterion 12 missing anchor -> RED ({len(errs)} violation(s)) OK")

    # --- criterion 13 [deep-audit-1039 A-06]: cancel resolves through the store ---
    # Backward compat: the four-file trees above omit HeadlessChatRunner.kt, and
    # the criterion must skip them (drift-detector stance) — already proven GREEN.
    print("selftest: criterion 13 skipped when HeadlessChatRunner.kt absent (old fixtures) OK")

    rc, errs = run_tree(screen, nav, move, FIXTURE_STORE, FIXTURE_ACTIVITY, FIXTURE_RUNNER)
    if rc != 0:
        failures.append(f"criterion 13 GREEN fixture unexpectedly red: {errs}")
    else:
        print("selftest: criterion 13 GREEN fixture passes")

    for name, run_fixture, anchor in _RUNNER_MUTATIONS:
        if run_fixture == FIXTURE_RUNNER:
            failures.append(f"criterion 13 {name}: mutation is a no-op (anchor not found)")
            continue
        rc, errs = run_tree(screen, nav, move, FIXTURE_STORE, FIXTURE_ACTIVITY, run_fixture)
        if rc == 0:
            failures.append(f"criterion 13 {name}: gate stayed GREEN (no teeth)")
        else:
            print(f"selftest: criterion 13 {name} -> RED ({len(errs)} violation(s)) OK")

    if failures:
        print("\nselftest FAILURES:")
        for f in failures:
            print(f"  - {f}")
        print("CHAT_SESSION_ID_BINDING_SELFTEST=FAIL")
        return 1
    print("CHAT_SESSION_ID_BINDING_SELFTEST=PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".", help="repo root")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return check(Path(args.root).resolve())


if __name__ == "__main__":
    sys.exit(main())

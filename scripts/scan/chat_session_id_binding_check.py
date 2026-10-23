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
# [deep-audit-1043 A-05] criterion 14 derives its entry set from EVERY file in
# the `debug` package, not just the runner: `ChatMutationMethods.uiPrompt`
# obtains the screen-owned ViewModel and dispatches into it too.
REL_DEBUG_DIR = "src/android/app/src/main/java/com/rikkaminis/app/debug"
REL_MUTATION_FILE = REL_DEBUG_DIR + "/ChatMutationMethods.kt"

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


# [deep-audit-1044 A-03] criterion 15 — a dropped headless send is an ERROR.
# `ChatViewModel.sendMessage` claims `_isStreaming` SYNCHRONOUSLY, and only
# after every pre-flight gate (Safe Mode / `currentProvider == null` /
# `_isCompacting` / context exhausted / blank input). `prompt()` reads the flag
# right after the call, so `started == false` means "this send was dropped and
# nothing will ever stream" — the `wait=true` path used to fall through an empty
# `if (!started) { }` block instead, and because nothing streams the wait
# returned at once (`withTimeoutOrNull` sees `isStreaming == false`, skips the
# `first { !it }` branch) and the drain read the PREVIOUS assistant turn out of
# the DB and reported it as `Completed`. The caller (chat.prompt RPC /
# `minis-sessions send`) then accepted a stale reply as this request's answer.
#
# The criterion binds the DECISION (an `!started` early return carrying a
# structured error, before the drain), not one spelling: the envelope may be
# written inline or delegated to a helper, and the status literal may live in
# that helper — one hop is followed, which is what `droppedSendResult()` uses.
# It also pins the pre-fix shape itself: an EMPTY `!started` block is red on its
# own, because "fall through to drain" is exactly the defect.
RE_RUNNER_PROMPT_DECL = re.compile(r"\bsuspend\s+fun\s+prompt\s*\(")
RE_IF_NOT_STARTED = re.compile(r"if\s*\(\s*!\s*started\s*\)")

def _decl_region(code: str, name: str, span: int = 600) -> "str | None":
    """`fun <name>(` plus the first [span] characters of its declaration.

    Used to follow ONE delegation hop for the dropped-send envelope. A text
    window rather than a parsed body on purpose: the helper is a single
    expression (`… : PromptResult = PromptResult(status = "Error", …)`) with no
    brace block of its own, so a brace-hunting walk would read the NEXT
    function's body and either pass or fail for the wrong reason.
    """
    m = re.search(r"\bfun\s+" + re.escape(name) + r"\s*\(", code)
    if m is None:
        return None
    return code[m.start(): m.start() + span]

def _yields_dropped_error(code: str, block: str) -> "bool":
    """Does this `if (!started) { … }` block RETURN the dropped-send envelope?

    Accepts both spellings of the same decision: the literal inline
    (`status = "Error"`, `responseText = "send_dropped_before_stream"`) and a
    call to a helper whose own declaration carries them. Both literals must be
    present, because together they ARE the contract the CLI reads (status
    decides the exit code, responseText is the machine reason).
    """
    text = strip_comments(block)
    if "return" not in text:
        return False
    hop = [text]
    for name in sorted(set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", text))):
        region = _decl_region(code, name)
        if region is not None:
            hop.append(region)
    blob = "\n".join(hop)
    return '"Error"' in blob and "send_dropped_before_stream" in blob

def _prompt_drop_violations(code: str) -> "list[str]":
    """Criterion 15 for one `debug/HeadlessChatRunner.kt` (comments stripped)."""
    body = _fn_body_after(code, RE_RUNNER_PROMPT_DECL)
    if body is None:
        return [
            "HeadlessChatRunner.kt: `suspend fun prompt(` block not found "
            "(renamed or moved? re-anchor criterion 15, do not drop it)"
        ]
    if "withTimeoutOrNull(" not in body or "loadMessages(" not in body:
        return [
            "HeadlessChatRunner.kt: prompt() lost its wait->drain arm "
            "(`withTimeoutOrNull(` around the `isStreaming` wait, then "
            "`loadMessages(` for the final text) — re-anchor criterion 15 "
            "instead of dropping it"
        ]
    drain = body.find("loadMessages(")
    guards = []
    for m in RE_IF_NOT_STARTED.finditer(body):
        open_idx = body.find("{", m.end())
        if open_idx == -1:
            continue
        end = _match_brace(body, open_idx)
        if end is None:
            continue
        guards.append((m.start(), body[open_idx:end + 1]))
    errs = []
    if not guards:
        errs.append(
            "HeadlessChatRunner.kt: prompt() no longer guards on `!started` — a "
            "send dropped by a pre-flight gate settles `Completed` with the "
            "PREVIOUS assistant text (nothing ever streams, so the wait returns "
            "at once and the drain reads the last turn)"
        )
        return errs
    if not [b for at, b in guards if at < drain]:
        errs.append(
            "HeadlessChatRunner.kt: prompt()'s `!started` guard sits AFTER the "
            "drain — it must decide before `loadMessages(` is reached"
        )
    elif not [b for at, b in guards if at < drain and _yields_dropped_error(code, b)]:
        errs.append(
            "HeadlessChatRunner.kt: prompt()'s `!started` guard does not return "
            "a structured error before the drain — it must settle "
            '`status = "Error"` with `responseText = "send_dropped_before_stream"` '
            '(inline or through the shared envelope helper), never a pseudo-success'
        )
    if [b for _, b in guards if not strip_comments(b[1:-1]).strip()]:
        errs.append(
            "HeadlessChatRunner.kt: prompt()'s `if (!started) { … }` block is "
            "EMPTY — falling through to the drain reports the last assistant "
            "turn as this request's answer"
        )
    return errs

# [deep-audit-1042 A-05] criterion 14 — the headless session-ready barrier.
# `ChatSessionLifecycle.loadSession` is a TWO-STEP publish: step 1 flips
# `_activeEntryId` (ChatSessionLifecycle:423) and the RPC layer's existing
# readiness wait keys on it; step 2 reads the messages on IO (:477) and only
# its `finally` flips `_sessionLoaded` (:786). Every headless entry that hands
# the ViewModel a message/block id and lets it mutate the in-memory turn state
# therefore has a window in which the id resolves to nothing:
# `retryFromMessage` returns on `index < 0`, `rerunFromToolBlock` returns false
# (or returns TRUE through its degenerate branch while `retryFromMessage`
# drops), the compactor sees an empty history, and `revertCompact` reads a
# `_cachedLatestMarker` that is still null. The runner used to report
# `Completed` for all of that — the wait loop exits immediately because no
# stream was ever claimed.
#
# The criterion binds the DECISION (await the latch before the mutation), not
# one spelling: the wait must (a) sit in the same member function, (b) precede
# the mutation call, (c) be wrapped in an off-Main hop — waiting on Main
# deadlocks the load that flips the latch, and (d) be bounded — an unbounded
# wait pins the RPC connection forever. The entry set is DERIVED by scanning
# function bodies for the mutation calls, so a new entry is covered the moment
# it is written and a renamed one does not silently escape (gate criterion 11
# of §8.7: never hardcode the member count).
#
# [deep-audit-1043 A-05] Two blind spots closed here, both of the same shape
# ("the derived set was spelled, not derived"):
#   (a) the mutator list was hardcoded as `vm.<method>(` — `selectEntry` was
#       missing, so `HeadlessChatRunner.selectModel` (the 6th `viewModel(…)`
#       call site) was never derived at all;
#   (b) the scan was pinned to ONE file — `debug/ChatMutationMethods.uiPrompt`
#       obtains the SAME ViewModel from the store and calls `sendMessage`, but
#       it was never read, and its call is written `it.sendMessage(text)`
#       (a `.also { … }` receiver), which the `vm.` prefix could not see either.
# The criterion now walks every `debug/*.kt`, derives the ChatViewModel
# receivers per member function, and polices every state-mutating call made on
# one of them. Receivers it cannot prove are a ChatViewModel are RED
# (fail-closed) unless they belong to a transport that delegates to another
# entry in this same package — those are policed on the callee's body, which
# this criterion also scans.
RE_MEMBER_FUN = re.compile(r"(?m)^    (?:private |internal )?(?:suspend )?fun (\w+)\s*\(")
RUNNER_STATE_MUTATORS = (
    "sendMessage",
    "retryFromMessage",
    "rerunFromToolBlock",
    "compactBefore",
    "revertCompact",
    "runCompactNow",
    "selectEntry",
)
RE_STATE_MUTATOR_CALL = re.compile(
    r"(?<![\w.])([A-Za-z_]\w*)\.(%s)\(" % "|".join(RUNNER_STATE_MUTATORS)
)
# ChatViewModel bindings, derived from the acquisition expression itself:
#   `val vm = viewModel(context, sessionId)`      (the runner's factory helper)
#   `val vm = viewModelForCancel(…)`              (the cancel resolver)
#   `val vm = … [ChatViewModel::class.java]`      (the store/ViewModelProvider shape)
#   `… [ChatViewModel::class.java].also { it.… }` (the pre-1043 uiPrompt shape)
RE_VM_FACTORY_BIND = re.compile(
    r"\bval\s+(\w+)\s*=\s*(?:[\w.]+\.)?(?:viewModel|viewModelForCancel)\s*\("
)
VM_TYPE_MARK = "ChatViewModel::class.java"
RE_LET_BIND = re.compile(r"\.(?:also|let)\s*\{\s*(\w+)\s*\.")
# Transports: their calls land on an object of THIS package, and the callee's
# own body is scanned by this criterion (and is where the barrier belongs).
# `ChatViewModel` is a type reference (companion / factory), not an instance.
DELEGATING_RECEIVERS = ("HeadlessChatRunner", "ChatMutationMethods", "ChatViewModel")
RUNNER_BARRIER_WAIT = "sessionLoaded.first"
# Bind the decision (an off-Main hop), not one spelling: the `debug` package
# mixes imported and fully-qualified dispatcher names, and a legal
# `kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Default)`
# must not read as "waits on Main".
RE_OFFMAIN_HOP = re.compile(
    r"withContext\s*\(\s*(?:[A-Za-z_]\w*\.)*Dispatchers\s*\.\s*Default\s*\)"
)
RUNNER_OFFMAIN_HOP = "withContext(Dispatchers.Default)"
RUNNER_BOUND_WAIT = "withTimeoutOrNull("


def _body_span_after(code: str, paren_hint: int) -> "tuple[int, int] | None":
    """Span `(start, end)` of the `{ … }` following the parameter list.

    Same walk as `_fn_body_after` (past the balanced parameter parentheses,
    then the first brace block), but keyed on a position instead of a regex,
    so it can be run over every member declaration of a file. Accepts both a
    block body and an expression body whose trailing lambda IS the body
    (`… = withContext(Dispatchers.Main) { … }`). Fail-closed: `None` when the
    parentheses or braces do not balance.
    """
    i = code.find("(", paren_hint)
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
    else:
        return None
    open_idx = code.find("{", i)
    if open_idx == -1:
        return None
    end = _match_brace(code, open_idx)
    return None if end is None else (open_idx, end + 1)


def _member_spans(code: str) -> "list[tuple[str, int, int]]":
    """(name, span_start, span_end) for every member function in [code].

    Spans are delimited by the NEXT member declaration, not by the first brace
    after the parameter list: an expression-bodied member that does not end in
    a trailing lambda (`private fun app(context: Context): MinisApp = …`) has
    no body brace at all, and a brace-hunting walk would hand that member the
    following function's body — attributing its mutator calls to the wrong
    entry and skipping the real owner.
    """
    decls = list(RE_MEMBER_FUN.finditer(code))
    spans = []
    for i, m in enumerate(decls):
        end = decls[i + 1].start() if i + 1 < len(decls) else len(code)
        spans.append((m.group(1), m.start(), end))
    return spans


def _derived_vm_receivers(body: str) -> "set[str]":
    """Names bound to a ChatViewModel in [body] (per function, not per file).

    Derived from the acquisition expression so a rename is still covered: the
    factory helpers by name, the store/`ViewModelProvider` shape by the
    `[ChatViewModel::class.java]` projection it terminates in, and the
    `.also { it… }` / `.let { it… }` lambda parameter. The projection arm binds
    the NEAREST preceding `val x =` only when nothing re-binds in between
    (`return` / another `val`), which is what keeps an unrelated
    `[ChatViewModel::class.java]` inside `runCatching { … }` from minting a
    bogus receiver name.
    """
    names = {m.group(1) for m in RE_VM_FACTORY_BIND.finditer(body)}
    idx = 0
    while True:
        i = body.find(VM_TYPE_MARK, idx)
        if i == -1:
            break
        idx = i + 1
        binds = list(re.finditer(r"\bval\s+(\w+)\s*=", body[:i]))
        if not binds:
            continue
        tail = body[binds[-1].end() : i]
        if "return" in tail or "val " in tail:
            continue
        names.add(binds[-1].group(1))
    for m in RE_LET_BIND.finditer(body):
        names.add(m.group(1))
    return names


def _offmain_bounded_blocks(prefix: str) -> "list[tuple[int, int]]":
    """Spans of every off-Main `withContext(Dispatchers.Default) { … }` in [prefix]."""
    blocks = []
    pos = 0
    while True:
        m = RE_OFFMAIN_HOP.search(prefix, pos)
        if m is None:
            break
        d = m.start()
        pos = m.end()
        brace = prefix.find("{", m.end())
        if brace == -1:
            continue
        end = _match_brace(prefix, brace)
        if end is None:
            continue
        blocks.append((brace, end + 1))
    return blocks


def _session_barrier_violations(rel: str, code: str) -> "list[str]":
    out: "list[str]" = []
    spans = _member_spans(code)
    for m in RE_STATE_MUTATOR_CALL.finditer(code):
        j = m.start()
        receiver = m.group(1)
        token = m.group(2) + "("
        owner = None
        for name, s, e in spans:
            if s <= j < e:
                owner = (name, s, e)
                break
        if owner is None:
            out.append(
                "%s: `%s.%s` no longer sits inside a member function body — "
                "criterion 14 cannot prove the caller awaits `sessionLoaded` "
                "before it (re-anchor, do not drop it)"
                % (rel, receiver, m.group(2))
            )
            continue
        name, body_start, body_end = owner
        if receiver in DELEGATING_RECEIVERS:
            # A transport handing the call to another entry of this package:
            # that entry's own body is scanned here too, and the barrier
            # belongs to it (waiting in the transport would wait for the VM the
            # callee resolves, not this one).
            continue
        if receiver not in _derived_vm_receivers(code[body_start:body_end]):
            out.append(
                "%s: cannot prove `%s` in `%s` is a ChatViewModel — criterion 14 "
                "only polices calls made on a receiver derived from a "
                "ChatViewModel acquisition (`val x = viewModel(…)` / "
                "`val x = …[ChatViewModel::class.java]` / `.also { it… }`). "
                "Either acquire it that way or add the delegating owner to "
                "DELEGATING_RECEIVERS — do not let the entry escape unproven"
                % (rel, receiver, name)
            )
            continue
        prefix = code[body_start:j]
        if RUNNER_BARRIER_WAIT not in prefix:
            out.append(
                "%s: `%s` mutates the ViewModel's in-memory turn state at `%s` "
                "without awaiting the `sessionLoaded` latch first — "
                "`_activeEntryId` flips at load step 1 and the latch only at "
                "step 2's finally, so in that window the id/message list/model "
                "is still the constructor default: the mutation drops (or is "
                "rolled back by the late apply) and the settle branch reports "
                "the pre-load value as if it were live"
                % (rel, name, token[:-1])
            )
            continue
        k = prefix.rfind(RUNNER_BARRIER_WAIT)
        covered = any(
            b < k < e and RUNNER_BOUND_WAIT in prefix[b:e]
            for b, e in _offmain_bounded_blocks(prefix)
        )
        if not covered:
            out.append(
                "%s: the `sessionLoaded` wait in `%s` is not a BOUNDED OFF-MAIN "
                "hop — it must sit inside "
                "`withContext(Dispatchers.Default) { withTimeoutOrNull(…) { … } }`. "
                "Waiting on Main deadlocks the load that flips the latch; waiting "
                "without a timeout pins the RPC connection forever when the load "
                "never completes" % (rel, name)
            )
    return out


def _session_barrier_violations_in(root: Path) -> "list[str]":
    """Criterion 14 over the whole `debug` package (derived file set)."""
    debug_dir = root / REL_DEBUG_DIR
    files = sorted(debug_dir.glob("*.kt")) if debug_dir.is_dir() else []
    if not files:
        # Backward compat: the four-file mini-tree fixtures predate the runner.
        # A fixture that carries it as a single file keeps working; a tree with
        # neither is vacuously green (criterion 13 in the same stance).
        single = root / REL_RUNNER
        files = [single] if single.is_file() else []
    out: "list[str]" = []
    for p in files:
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            rel = p.name
        out.extend(
            _session_barrier_violations(rel, strip_comments(p.read_text(encoding="utf-8")))
        )
    return out


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

        # --- 15. [deep-audit-1044 A-03] a dropped send is an ERROR, never the
        #     previous turn replayed as a "Completed". ---
        errors.extend(_prompt_drop_violations(runner))

    # --- 14. [deep-audit-1042 A-05 / 1043 A-05] every entry that mutates the
    #     ViewModel's in-memory turn state awaits the `sessionLoaded` latch
    #     BEFORE the mutation. Files, entries and receivers are all derived
    #     (every `debug/*.kt`, every state-mutating call on a
    #     ChatViewModel-typed receiver), so a new entry — or a new file — is
    #     covered without editing this gate. ---
    errors.extend(_session_barrier_violations_in(root))

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

# --- criterion 15 [deep-audit-1044 A-03]: the headless send path -------------
# Every runner fixture carries `prompt()`, because the criterion is fail-closed:
# a fixture without the function would red criterion 15 and make the 13/14 arms
# look like they have more teeth (or fewer) than they do.
_R_PROMPT_GUARD_INLINE = (
    "        if (!started) {\n"
    "            return@withContext PromptResult(\n"
    "                status = \"Error\",\n"
    "                responseText = \"send_dropped_before_stream\",\n"
    "                timedOut = false,\n"
    "            )\n"
    "        }\n"
)
_R_PROMPT_GUARD_DELEGATED = (
    "        if (!started) {\n"
    "            return@withContext droppedSendResult()\n"
    "        }\n"
)
# Top level, AFTER the class — exactly where the production helper lives, so the
# one-hop delegation path in `_yields_dropped_error` is exercised for real.
_R_PROMPT_HELPER_DECL = (
    "\n"
    "internal fun droppedSendResult(): PromptResult =\n"
    "    PromptResult(\n"
    "        status = \"Error\",\n"
    "        responseText = \"send_dropped_before_stream\",\n"
    "        timedOut = false,\n"
    "    )\n"
)
_R_PROMPT_DRAIN = (
    "        val finished = withTimeoutOrNull(timeoutMs) {\n"
    "            if (vm.isStreaming.value) {\n"
    "                vm.isStreaming.first { !it }\n"
    "            }\n"
    "            true\n"
    "        } ?: false\n"
    "        val msgs = app.chatRepository.loadMessages(sessionId)\n"
    "        val lastAssistant = msgs.lastOrNull { it.role == \"assistant\" }\n"
    "        PromptResult(\n"
    "            status = if (finished) \"Completed\" else \"Timeout\",\n"
    "            responseText = lastAssistant?.id,\n"
    "            timedOut = !finished,\n"
    "        )\n"
    "    }\n"
)

def _prompt_entry(guard: str) -> str:
    return (
        "\n"
        "    suspend fun prompt(\n"
        "        context: Context,\n"
        "        sessionId: String,\n"
        "        text: String,\n"
        "        wait: Boolean,\n"
        "        timeoutMs: Long,\n"
        "    ): PromptResult = withContext(Dispatchers.Main) {\n"
        "        val vm = viewModel(context, sessionId)\n"
        "        val started = vm.isStreaming.value\n"
        "        if (!wait) {\n"
        "            return@withContext if (started) {\n"
        "                PromptResult(status = \"Running\", responseText = null, timedOut = false)\n"
        "            } else {\n"
        "                PromptResult(status = \"Error\", "
        "responseText = \"send_dropped_before_stream\", timedOut = false)\n"
        "            }\n"
        "        }\n"
        + guard
        + _R_PROMPT_DRAIN
    )

_R_RUNNER_TAIL = "}\n"
assert FIXTURE_RUNNER.endswith(_R_RUNNER_TAIL), "runner fixture tail moved"
FIXTURE_RUNNER = (
    FIXTURE_RUNNER[: -len(_R_RUNNER_TAIL)] + _prompt_entry(_R_PROMPT_GUARD_INLINE) + _R_RUNNER_TAIL
)
# Green arm, delegated spelling: the envelope literals live in a helper one hop
# away, which the criterion must accept (it binds the decision, not the spelling).
FIXTURE_RUNNER_DROP_DELEGATED = (
    FIXTURE_RUNNER.replace(_R_PROMPT_GUARD_INLINE, _R_PROMPT_GUARD_DELEGATED)
    + _R_PROMPT_HELPER_DECL
)
# Dirty arms, one per decision point.
_R_PROMPT_GUARD_EMPTY = (
    "        if (!started) {\n"
    "            // dropped before the stream was claimed — fall through to drain\n"
    "        }\n"
)
_R_PROMPT_GUARD_COMPLETED = (
    "        if (!started) {\n"
    "            return@withContext PromptResult(\n"
    "                status = \"Completed\",\n"
    "                responseText = null,\n"
    "                timedOut = false,\n"
    "            )\n"
    "        }\n"
)
_R_PROMPT_DRAIN_CLOSE = "    }\n"
assert _R_PROMPT_DRAIN.endswith(_R_PROMPT_DRAIN_CLOSE), "prompt drain tail moved"
FIXTURE_RUNNER_DROP_EMPTY = FIXTURE_RUNNER.replace(_R_PROMPT_GUARD_INLINE, _R_PROMPT_GUARD_EMPTY)
FIXTURE_RUNNER_DROP_NONE = FIXTURE_RUNNER.replace(_R_PROMPT_GUARD_INLINE, "")
FIXTURE_RUNNER_DROP_COMPLETED = FIXTURE_RUNNER.replace(
    _R_PROMPT_GUARD_INLINE, _R_PROMPT_GUARD_COMPLETED)
FIXTURE_RUNNER_DROP_AFTER = FIXTURE_RUNNER.replace(_R_PROMPT_GUARD_INLINE, "").replace(
    _R_PROMPT_DRAIN,
    _R_PROMPT_DRAIN[: -len(_R_PROMPT_DRAIN_CLOSE)]
    + _R_PROMPT_GUARD_INLINE
    + _R_PROMPT_DRAIN_CLOSE,
)

_RUNNER_DROP_MUTATIONS = (
    ("an empty !started block (MUT-P1)", FIXTURE_RUNNER_DROP_EMPTY, "is EMPTY"),
    ("the guard deleted (MUT-P2)", FIXTURE_RUNNER_DROP_NONE, "no longer guards on `!started`"),
    ("the guard returns a Completed pseudo-success (MUT-P3)",
     FIXTURE_RUNNER_DROP_COMPLETED, "does not return a structured error"),
    ("the guard moved below the drain (MUT-P4)", FIXTURE_RUNNER_DROP_AFTER, "sits AFTER the drain"),
)

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

# --- criterion 14 [deep-audit-1042 A-05]: the session-ready barrier ----------
# Faithful to the production shape of the four headless mutation entries: the
# bounded off-Main wait on `sessionLoaded`, then the mutation, then the
# "did a run actually start" read. Never compiled — the gate reads text.
_R_BARRIER_WAIT_BLOCK = (
    "        val sessionReady = withContext(Dispatchers.Default) {\n"
    "            withTimeoutOrNull(5000L) { vm.sessionLoaded.first { it } }\n"
    "        }\n"
)
_R_BARRIER_ERROR_BLOCK = (
    "        if (sessionReady == null) {\n"
    "            return@withContext PromptResult(status = \"Error\", "
    "responseText = \"session_not_loaded_in_5s\")\n"
    "        }\n"
)
_R_BARRIER_ENTRY = (
    "\n"
    "    suspend fun retry(context: Context, sessionId: String): PromptResult "
    "= withContext(Dispatchers.Main) {\n"
    "        val vm = viewModel(context, sessionId)\n"
    + _R_BARRIER_WAIT_BLOCK
    + _R_BARRIER_ERROR_BLOCK
    + "        vm.retryFromMessage(messageId)\n"
    "        val started = vm.isStreaming.value\n"
    "        PromptResult(status = if (started) \"Completed\" else \"Error\")\n"
    "    }\n"
)
_R_RUNNER_CLASS_TAIL = "    }\n}\n"

# The green fixture = criterion-13's runner PLUS one barrier-bearing entry.
# Built fail-closed: a fixture whose tail moved would otherwise produce a
# silently unclosed class and a vacuous criterion.
assert FIXTURE_RUNNER.endswith(_R_RUNNER_CLASS_TAIL), "runner fixture tail moved"
FIXTURE_RUNNER_BARRIER = FIXTURE_RUNNER[: -len("}\n")] + _R_BARRIER_ENTRY + "}\n"

# (name, dirty variant, anchor that must have been replaced)
_RUNNER_BARRIER_MUTATIONS = (
    (
        "sessionLoaded barrier dropped entirely (MUT-B1)",
        FIXTURE_RUNNER_BARRIER.replace(
            _R_BARRIER_WAIT_BLOCK + _R_BARRIER_ERROR_BLOCK, ""),
        _R_BARRIER_WAIT_BLOCK,
    ),
    (
        "the wait runs on Main (MUT-B2)",
        FIXTURE_RUNNER_BARRIER.replace(
            _R_BARRIER_WAIT_BLOCK,
            "        val sessionReady = withContext(Dispatchers.Unconfined) {\n"
            "            withTimeoutOrNull(5000L) { vm.sessionLoaded.first { it } }\n"
            "        }\n"),
        _R_BARRIER_WAIT_BLOCK,
    ),
    (
        "the wait is unbounded (MUT-B3)",
        FIXTURE_RUNNER_BARRIER.replace(
            _R_BARRIER_WAIT_BLOCK,
            "        val sessionReady = withContext(Dispatchers.Default) {\n"
            "            vm.sessionLoaded.first { it }\n"
            "        }\n"),
        _R_BARRIER_WAIT_BLOCK,
    ),
    (
        "the barrier sits AFTER the mutation (MUT-B4)",
        FIXTURE_RUNNER_BARRIER.replace(
            "        val vm = viewModel(context, sessionId)\n"
            + _R_BARRIER_WAIT_BLOCK
            + _R_BARRIER_ERROR_BLOCK
            + "        vm.retryFromMessage(messageId)\n",
            "        val vm = viewModel(context, sessionId)\n"
            "        vm.retryFromMessage(messageId)\n"
            + _R_BARRIER_WAIT_BLOCK
            + _R_BARRIER_ERROR_BLOCK),
        "        vm.retryFromMessage(messageId)\n" + _R_BARRIER_WAIT_BLOCK,
    ),
    (
        "a second mutation entry without the barrier (MUT-B5, derived set)",
        FIXTURE_RUNNER_BARRIER[
            : FIXTURE_RUNNER_BARRIER.rfind("}\n")
        ] + (
            "\n    suspend fun compact(context: Context, sessionId: String) "
            "= withContext(Dispatchers.Main) {\n"
            "        val vm = viewModel(context, sessionId)\n"
            "        vm.runCompactNow()\n"
            "    }\n"
        ) + "}\n",
        _R_RUNNER_CLASS_TAIL,
    ),
)

# --- criterion 14 [deep-audit-1043 A-05]: the SECOND file ---------------------
# `debug/ChatMutationMethods.uiPrompt` obtains the SAME screen-owned ViewModel
# from the store and dispatches into it — an entry the pre-1043 criterion could
# not see (it read one file and matched a `vm.` prefix, while this call is
# written `it.sendMessage(text)`). The fixture is faithful to the post-fix
# production shape: fully-qualified coroutine calls, a projection-only
# acquisition, and an expression-bodied `app(…)` helper with NO body brace
# (a brace-hunting member walk would hand its body to the next function).
FIXTURE_MUTATION = '''package com.rikkaminis.app.debug

internal object ChatMutationMethods {
    private fun app(context: Context): MinisApp =
        context.applicationContext as? MinisApp ?: throw RPCException(-32000, "no app")

    suspend fun uiPrompt(context: Context, text: String): JSONObject {
        val app = app(context)
        val vm = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Main) {
            androidx.lifecycle.ViewModelProvider(
                ChatViewModelStore.ownerFor(sessionId),
                ChatViewModel.factory(sessionId = sessionId),
            )[com.rikkaminis.app.ui.chat.ChatViewModel::class.java]
        }
        val sessionReady = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Default) {
            kotlinx.coroutines.withTimeoutOrNull(5000L) { vm.sessionLoaded.first { it } }
        }
        if (sessionReady == null) {
            throw RPCException(-32000, "session_not_loaded_in_5s")
        }
        val started = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Main) {
            vm.sendMessage(text)
            vm.isStreaming.value
        }
        return JSONObject()
    }
}
'''

_R_MUT_BARRIER_BLOCK = (
    "        val sessionReady = kotlinx.coroutines.withContext("
    "kotlinx.coroutines.Dispatchers.Default) {\n"
    "            kotlinx.coroutines.withTimeoutOrNull(5000L) "
    "{ vm.sessionLoaded.first { it } }\n"
    "        }\n"
    "        if (sessionReady == null) {\n"
    "            throw RPCException(-32000, \"session_not_loaded_in_5s\")\n"
    "        }\n"
)

_R_MUT_DISPATCH_BLOCK = (
    "        val started = kotlinx.coroutines.withContext("
    "kotlinx.coroutines.Dispatchers.Main) {\n"
    "            vm.sendMessage(text)\n"
    "            vm.isStreaming.value\n"
    "        }\n"
)

_R_MUT_ACQUISITION = (
    "        val vm = kotlinx.coroutines.withContext("
    "kotlinx.coroutines.Dispatchers.Main) {\n"
    "            androidx.lifecycle.ViewModelProvider(\n"
    "                ChatViewModelStore.ownerFor(sessionId),\n"
    "                ChatViewModel.factory(sessionId = sessionId),\n"
    "            )[com.rikkaminis.app.ui.chat.ChatViewModel::class.java]\n"
    "        }\n"
)

assert _R_MUT_BARRIER_BLOCK in FIXTURE_MUTATION, "mutation fixture: barrier block moved"
assert _R_MUT_DISPATCH_BLOCK in FIXTURE_MUTATION, "mutation fixture: dispatch block moved"
assert _R_MUT_ACQUISITION in FIXTURE_MUTATION, "mutation fixture: acquisition moved"

# (name, mutation-file fixture, anchor that must have been replaced)
_MUTATION_FILE_MUTATIONS = (
    (
        "the second file's uiPrompt drops the barrier (MUT-B6)",
        FIXTURE_MUTATION.replace(_R_MUT_BARRIER_BLOCK, ""),
        _R_MUT_BARRIER_BLOCK,
    ),
    (
        "the pre-1043 uiPrompt shape (`.also { it.sendMessage }`, no barrier) "
        "is caught even though the receiver is not `vm` (MUT-B7)",
        FIXTURE_MUTATION.replace(_R_MUT_DISPATCH_BLOCK, "        vm.also { it.sendMessage(text) }\n")
        .replace(_R_MUT_BARRIER_BLOCK, ""),
        _R_MUT_DISPATCH_BLOCK,
    ),
    (
        "the ViewModel acquisition no longer derives a receiver — fail-closed, "
        "not silently skipped (MUT-B9)",
        FIXTURE_MUTATION.replace(_R_MUT_ACQUISITION, "        val vm = buildUiViewModel()\n"),
        _R_MUT_ACQUISITION,
    ),
)

# `selectEntry` was missing from the mutator list, so the 6th `viewModel(…)`
# call site (selectModel) was outside the derived set entirely.
_RUNNER_SELECT_MODEL_ENTRY = (
    "\n    suspend fun selectModel(context: Context, sessionId: String): Pair<String, String> "
    "= withContext(Dispatchers.Main) {\n"
    "        val vm = viewModel(context, sessionId)\n"
    "        vm.selectEntry(entryId)\n"
    "        vm.modelName.value to vm.thinkingLevel.value.name\n"
    "    }\n"
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
                 runner: "str | None" = None,
                 mutation: "str | None" = None) -> "tuple[int, list[str]]":
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
            if mutation is not None:
                f = tdp / REL_MUTATION_FILE
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(mutation, encoding="utf-8")
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

    # --- criterion 15 [deep-audit-1044 A-03]: a dropped send is an ERROR -----
    # The green arm above (FIXTURE_RUNNER, inline spelling) already proves the
    # happy path, because every runner fixture carries `prompt()`. This arm adds
    # the DELEGATED spelling — the envelope literals one hop away in a helper.
    rc, errs = run_tree(screen, nav, move, FIXTURE_STORE, FIXTURE_ACTIVITY,
                        FIXTURE_RUNNER_DROP_DELEGATED)
    if rc != 0:
        failures.append(f"criterion 15 delegated-envelope fixture unexpectedly red: {errs}")
    else:
        print("selftest: criterion 15 delegated envelope (helper carries the literals) passes")

    for name, run_fixture, expect in _RUNNER_DROP_MUTATIONS:
        if run_fixture == FIXTURE_RUNNER:
            failures.append(f"criterion 15 {name}: mutation is a no-op (anchor not found)")
            continue
        rc, errs = run_tree(screen, nav, move, FIXTURE_STORE, FIXTURE_ACTIVITY, run_fixture)
        if rc == 0 or not [e for e in errs if expect in e]:
            failures.append(
                f"criterion 15 {name}: expected a `{expect}` violation, got {errs}")
        else:
            print(f"selftest: criterion 15 {name} -> RED ({len(errs)} violation(s)) OK")

    # --- criterion 14 [deep-audit-1042 A-05]: the session-ready barrier ---
    # Backward compat: the fixture above (no mutator calls) proves the
    # criterion stays vacuously GREEN on trees without the entries.
    print("selftest: criterion 14 vacuous when no mutation entries exist (MUT-R fixtures) OK")

    rc, errs = run_tree(screen, nav, move, FIXTURE_STORE, FIXTURE_ACTIVITY, FIXTURE_RUNNER_BARRIER)
    if rc != 0:
        failures.append(f"criterion 14 GREEN fixture unexpectedly red: {errs}")
    else:
        print("selftest: criterion 14 GREEN fixture passes")

    for name, run_fixture, anchor in _RUNNER_BARRIER_MUTATIONS:
        if run_fixture == FIXTURE_RUNNER_BARRIER:
            failures.append(f"criterion 14 {name}: mutation is a no-op (anchor not found)")
            continue
        rc, errs = run_tree(screen, nav, move, FIXTURE_STORE, FIXTURE_ACTIVITY, run_fixture)
        if rc == 0:
            failures.append(f"criterion 14 {name}: gate stayed GREEN (no teeth)")
        else:
            print(f"selftest: criterion 14 {name} -> RED ({len(errs)} violation(s)) OK")

    # --- criterion 14 [deep-audit-1043 A-05]: the derived set spans the whole
    #     `debug` package and the receiver is derived, not spelled `vm.` ------
    rc, errs = run_tree(
        screen, nav, move, FIXTURE_STORE, FIXTURE_ACTIVITY,
        FIXTURE_RUNNER_BARRIER, FIXTURE_MUTATION)
    if rc != 0:
        failures.append(
            f"criterion 14 second-file (ChatMutationMethods) GREEN fixture unexpectedly red: {errs}")
    else:
        print("selftest: criterion 14 second-file (ChatMutationMethods) GREEN fixture passes")

    for name, mut_fixture, anchor in _MUTATION_FILE_MUTATIONS:
        if mut_fixture == FIXTURE_MUTATION:
            failures.append(f"criterion 14 {name}: mutation is a no-op (anchor not found)")
            continue
        rc, errs = run_tree(
            screen, nav, move, FIXTURE_STORE, FIXTURE_ACTIVITY,
            FIXTURE_RUNNER_BARRIER, mut_fixture)
        if rc == 0:
            failures.append(f"criterion 14 {name}: gate stayed GREEN (no teeth)")
        else:
            print(f"selftest: criterion 14 {name} -> RED ({len(errs)} violation(s)) OK")

    # selectEntry is the mutator the pre-1043 list was missing: an entry that
    # switches the session's model without the latch wait must be RED.
    select_model_fixture = (
        FIXTURE_RUNNER_BARRIER[: FIXTURE_RUNNER_BARRIER.rfind("}\n")]
        + _RUNNER_SELECT_MODEL_ENTRY + "}\n"
    )
    if select_model_fixture == FIXTURE_RUNNER_BARRIER:
        failures.append("criterion 14 selectEntry arm: mutation is a no-op (anchor not found)")
    else:
        rc, errs = run_tree(
            screen, nav, move, FIXTURE_STORE, FIXTURE_ACTIVITY, select_model_fixture)
        if rc == 0:
            failures.append(
                "criterion 14 a selectModel entry without the barrier (MUT-B8, "
                "selectEntry): gate stayed GREEN (no teeth)")
        else:
            print(
                "selftest: criterion 14 a selectModel entry without the barrier "
                f"(MUT-B8, selectEntry) -> RED ({len(errs)} violation(s)) OK")

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

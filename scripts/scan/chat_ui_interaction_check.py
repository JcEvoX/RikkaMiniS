#!/usr/bin/env python3
"""
Gate: FIX-4 (deep-audit-1037, dv16 wave) — chat UI interaction wiring. Three
defect families, one gate (plus the dv19-B-01 family, E):

B-01  SlashCommand.isMcp was written but never read: an MCP row tap fell into
      executeSlashCommand's action dispatch, whose unrecognized-id arm returns
      "" — WIPING the composer instead of filling it. ChatInputArea also gated
      the installed-row divider + IME restore on a bare isSkill test.
B-02  selectionTableActions was wired into MinisMarkdownTextToolbar only — the
      main-chat MinisSelectionToolbarHost (the long-press toolbar chat users
      actually see) never surfaced Copy Table / Copy Table Image. The table
      registry was keyed by the bare messageId: same-message tables overwrote
      each other and a table leaving composition dropped its sibling's entry.
B-06  The swipe-up release branch re-derived eligibility inside an already
      hasText-guarded block (a dead branch) and read the debounced VM string,
      so attachment-only drags went to the keyboard and a just-typed prefix
      could be lost on release. One shared predicate read at call time now
      drives all three decision sites, and the threshold reads through
      rememberUpdatedState so ChatTuning adjustments reach the gesture.

Checks run on comment-stripped, string-blanked text, so prose in a KDoc or a
log literal cannot satisfy them:

  ChatModels.kt
    A1  every boolean field of the SlashCommand data class has >= 1 read point
        (`.field` in a condition across ChatSlashTokenExt + ChatInputArea).
        Fail-closed: a written-but-never-read boolean = the B-01 class again.
  ChatSlashTokenExt.kt
    A2  executeSlashCommand's body contains an `if (cmd.isMcp)` branch routed
        through the shared fill helper.
  ChatInputArea.kt
    A3  isMcp is read in a condition (divider / IME restore), not just defined.
  MinisTextKitGesture.kt
    B1  the file calls selectionTableActions() (the main-chat wiring).
  MinisMarkdownTextToolbar.kt
    B2  the file still calls selectionTableActions() — that path is a
        legitimate consumer; deleting it is a regression, not a cleanup.
  MinisTextKitSelection.kt
    B3  the table registry carries a table-level key: rememberTableActions /
        forgetTableActions each take an Int table dimension beyond messageId,
        and the map declaration is two-level.
    B4  selectionTableActions still guards on singleMessageId() (cross-message
        selections expose no table actions).
  StreamingMarkdownText.kt
    B5  the publisher passes the table index (3-arg rememberTableActions call)
        and sources it from the shard allocator.
  ChatInputArea.kt (swipe block, delimited by the pointerInput lambda)
    C1  exactly one shared send-eligibility predicate (`: () -> Boolean = {`)
        whose body reads the field's own TextFieldValue (never the debounced
        VM string) + the attachment list.
    C2  the predicate is used >= 3 times (the three decision sites).
    C3  the nested dead-branch shape is banned: `if (X && Y)` followed within
        4 lines by `val _ = Y || ...` — an eligibility re-derivation inside a
        branch that already implies it. Detected on the SHAPE (variable names
        are captured from the enclosing condition), not on names.
    C4  the swipe threshold reads through rememberUpdatedState (a plain val
        capture freezes the first composition's px forever), and the gesture
        reads its .value.
    C5  performSendOrEnqueue() still reached from the gesture (streaming
        enqueue preserved).
    C6  the empty-input keyboard shortcut (!inputFocused) survived.

  ChatInputArea.kt (composer decisions, dv19 wave B-01 — the E family)
    E1  the content flag of `val <showStop> = isStreaming && !<flag>` must,
        transitively over `val <name> = …` (<= 4 hops), reach the field's live
        value (or the shared C1 predicate) and must never reach the debounced
        VM string.
    E2  the three sites that act on / leave the composer (slash dismiss, slash
        command, cross-session transfer) must pass the field's live value. All
        three must exist: a missing site is fail-closed, since a decision that
        moved out of the gate's view must not read as "clean".
    E3  each of those three sites must drop the pending IME-burst flush before
        it acts on the composer — inline, or through a shared `() -> Unit`
        helper whose body cancels imeBurstJob (both shapes are legal: the
        decision is "cancelled before the write", not the spelling).

Boundary (documented, not a bug): C4 pins the rememberUpdatedState decision —
a refactor that swaps it for a state-holder class must rename the criterion.
C3's 4-line window is the static approximation of "inside the guarded block";
a dead branch more than 4 lines below its guard needs the window widened.
E1 follows identifiers through `val <name> = …` initialisers, so it requires
that flag (and the values it reads) to stay one-line `val`s — a multi-line
initialiser reads as "not reached" (fail-closed, never a silent pass).
E-family scope: the four decisions named in deep-audit-1039 B-01. A NEW
decision that reads the debounced string needs its own site criterion here —
the family does not blanket-ban `inputText` (the debounce machinery and the
field mirroring legitimately read it).

Usage: python3 scripts/scan/chat_ui_interaction_check.py [repo_root]
       python3 scripts/scan/chat_ui_interaction_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import re
import shutil
import sys
import tempfile

CHAT = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat"
MODELS_REL = CHAT + "/ChatModels.kt"
TOKEN_REL = CHAT + "/ChatSlashTokenExt.kt"
INPUT_REL = CHAT + "/ChatInputArea.kt"
GESTURE_REL = CHAT + "/MinisTextKitGesture.kt"
SELECTION_REL = CHAT + "/MinisTextKitSelection.kt"
STREAM_REL = CHAT + "/StreamingMarkdownText.kt"
MDTOOLBAR_REL = CHAT + "/MinisMarkdownTextToolbar.kt"

FILL_HELPER = "fillComposerWithSlashTitle("
PRED_RE = re.compile(r"val (\w+):\s*\(\)\s*->\s*Boolean\s*=\s*\{")
IF_AND_RE = re.compile(r"if\s*\((\w+)\s*&&\s*(\w+)")


# ── scanners (same provenance as the other gates: comment/string aware) ──────

def strip_comments(text):
    """Blank out comment content, keeping everything else, with a scanner that
    understands Kotlin string templates (a `//` inside a URL literal or a
    quote nested inside a `${…}` desyncs any naive scanner)."""
    out = []
    stack = [["code", 0]]
    i = 0
    n = len(text)
    while i < n:
        st = stack[-1]
        if st[0] == "code":
            if text.startswith('"""', i):
                stack.append(["raw", 0]); i += 3; continue
            c = text[i]
            if c == '"':
                stack.append(["str", 0]); out.append(c); i += 1; continue
            if text.startswith("//", i):
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text.startswith("/*", i):
                end = text.find("*/", i + 2)
                i = n if end < 0 else end + 2
                continue
            if c == "{":
                st[1] += 1
            elif c == "}":
                if st[1] == 0 and len(stack) > 1:
                    stack.pop(); out.append(c); i += 1; continue
                st[1] -= 1
            out.append(c); i += 1; continue
        if st[0] == "str":
            c = text[i]
            if c == "\\":
                out.append(c)
                if i + 1 < n:
                    out.append(text[i + 1])
                i += 2; continue
            if text.startswith("${", i):
                stack.append(["code", 0]); out.append("${"); i += 2; continue
            if c == '"':
                stack.pop(); out.append(c); i += 1; continue
            out.append(c); i += 1; continue
        if text.startswith('"""', i):
            stack.pop(); out.append('"""'); i += 3; continue
        out.append(text[i]); i += 1
    return "".join(out)


def strip_strings(code):
    """Blank the contents of string literals in comment-free code, so a whole-
    file call-site scan is never driven by a log literal."""
    out = []
    stack = [["code", 0]]
    i = 0
    n = len(code)
    while i < n:
        st = stack[-1]
        if st[0] == "code":
            if code.startswith('"""', i):
                stack.append(["raw", 0]); out.append("   "); i += 3; continue
            c = code[i]
            if c == '"':
                stack.append(["str", 0]); out.append(c); i += 1; continue
            if len(stack) > 1 and c == "{":
                st[1] += 1
            elif len(stack) > 1 and c == "}":
                if st[1] == 0:
                    stack.pop(); out.append(c); i += 1; continue
                st[1] -= 1
            out.append(c); i += 1; continue
        if st[0] == "str":
            c = code[i]
            if c == "\\":
                out.append("  ")
                i += 2
                continue
            if code.startswith("${", i):
                stack.append(["code", 0]); out.append("${"); i += 2; continue
            if c == '"':
                stack.pop(); out.append(c); i += 1; continue
            out.append(c if c == "\n" else " "); i += 1; continue
        if code.startswith('"""', i):
            stack.pop(); out.append('"""'); i += 3; continue
        out.append(code[i] if code[i] == "\n" else " "); i += 1
    return "".join(out)


def declaration_slice(text, marker):
    """Return the `{ ... }` body of the declaration containing `marker`
    (template/string aware). Returns "" when absent."""
    idx = text.find(marker)
    if idx == -1:
        return ""
    start = text.find("{", idx)
    if start == -1:
        return ""
    depth = 0
    tdepth = 0
    tstack = []
    in_str = False
    in_raw = False
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if in_raw:
            if text.startswith('"""', i):
                in_raw = False; i += 3; continue
            i += 1; continue
        if in_str:
            if c == "\\":
                i += 2; continue
            if text.startswith("${", i):
                tstack.append(tdepth); tdepth = 0; in_str = False; i += 2; continue
            if c == '"':
                in_str = False; i += 1; continue
            i += 1; continue
        if text.startswith('"""', i):
            in_raw = True; i += 3; continue
        if c == '"':
            in_str = True; i += 1; continue
        if c == "{":
            if tstack:
                tdepth += 1
            else:
                depth += 1
        elif c == "}":
            if tstack:
                if tdepth == 0:
                    tdepth = tstack.pop(); in_str = True
                else:
                    tdepth -= 1
            else:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return text[start:]


def signature_args(text, marker):
    """Return the argument list of the `marker(...)` call/declaration nearest
    the marker (no nested parens in the targets)."""
    idx = text.find(marker)
    if idx == -1:
        return None
    op = text.find("(", idx)
    cl = text.find(")", op)
    if op == -1 or cl == -1:
        return None
    return text[op + 1:cl]


def read(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()

# ── D: slash-menu memo wiring (dv17-4 / deep-audit-1038 B-05) ─────────────────
SLASH_MEMO_DECL = "val filteredSlashCommands"
SLASH_MEMO_CALL = "viewModel.filteredSlashCommands()"
# A guard is a CONDITION on showSlashMenu — `if (…showSlashMenu…)`, a negated
# `!showSlashMenu`, or a conjunction `showSlashMenu && …`. A bare
# `val showSlashMenu by …collectAsState()` subscription is NOT a guard.
SLASH_GUARD_RE = re.compile(
    r"if\s*\(\s*!?\s*showSlashMenu\b|!\s*showSlashMenu\b|showSlashMenu\s*&&")

def _balanced_paren(text, op):
    """Index of the `)` matching the `(` at `op`; None when unbalanced. The memo
    keys contain nested calls (`collectAsState()`), so a first-`)` scan would
    truncate the key list."""
    depth = 0
    for i in range(op, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
    return None

def slash_memo_findings(inp_c):
    """The slash list is only ever RENDERED under showSlashMenu, so:

    D1  the memo must not key on `thinkingLevel` — that source has 0 reads in
        the lambda (the /thinking row left `availableSlashCommands`), so the
        key only buys a needless recompute + N+M `isEnabledForSession` query
        burst on every thinking-notch tap;
    D2  the memo must key on the live sources the body reads — the session
        scope (currentSessionId/activeSessionId) and the two repository lists
        the per-session filter walks (call shapes `.skills` / `.servers`);
    D3  every `viewModel.filteredSlashCommands()` call must sit in a
        showSlashMenu-gated region: the window from the memo declaration (and
        800 chars of preceding statement context, so an early-exit shape is
        legal too) up to the call must contain a guard. Binding the criterion
        to the CONDITION (not to a specific spelling) keeps both legal shapes
        open: `if (showSlashMenu) { remember… }` and `if (!showSlashMenu) …`
        ahead of an ungated-looking call.
    """
    problems = []
    memo = inp_c.find(SLASH_MEMO_DECL)
    if memo == -1:
        return [("D-memo", "%s not found — fail-closed (the slash list memo "
                 "shape drifted?)" % SLASH_MEMO_DECL)]
    rm = re.search(r"\bremember\s*\(", inp_c[memo:])
    if rm is None:
        return [("D-memo", "no remember( after %s — fail-closed" % SLASH_MEMO_DECL)]
    op = memo + rm.end() - 1
    close = _balanced_paren(inp_c, op)
    if close is None:
        return [("D-memo", "unbalanced remember key list — fail-closed")]
    keys = inp_c[op + 1:close]

    if re.search(r"\.thinkingLevel\b", keys):
        problems.append(("D1", "the slash-menu memo keys on thinkingLevel — "
                         "that source has 0 reads in the lambda (the /thinking row "
                         "left availableSlashCommands), so every thinking-notch tap "
                         "buys a needless recompute + N+M isEnabledForSession "
                         "queries (B-05)"))
    if not re.search(r"\b(currentSessionId|activeSessionId)\b", keys):
        problems.append(("D2", "the slash-menu memo keys carry no session scope "
                         "(currentSessionId/activeSessionId) — an open menu keeps "
                         "showing the previous session's per-session skill/MCP "
                         "enablement after ensureSession() lands (B-05)"))
    for live, label in ((r"\.skills\b", "skill"), (r"\.servers\b", "MCP server")):
        if not re.search(live, keys):
            problems.append(("D2", "the slash-menu memo keys carry no %s list "
                             "— an entry toggled for this session (or added from "
                             "disk) never refreshes an open menu (B-05)" % label))

    for m in re.finditer(re.escape(SLASH_MEMO_CALL), inp_c):
        window = inp_c[max(0, memo - 800):m.start()]
        if not SLASH_GUARD_RE.search(window):
            problems.append(("D3", "%s runs ungated — the list is only rendered "
                             "under showSlashMenu, so every composition pays the "
                             "N+M isEnabledForSession queries for a value the next "
                             "line drops (B-05)" % SLASH_MEMO_CALL))
    return problems

# ── E: composer decisions read the LIVE field value (dv19-B1) ────────────────
#
# B-01 (dv19 wave): the composer keeps two copies of the draft — the field's own
# TextFieldValue (`inputFieldValue`, written FIRST on every onValueChange, i.e.
# exactly what the user sees) and the ViewModel string, which a >8-char IME
# burst (dictation / large paste) lags by up to 150 ms (the debounce flush). The
# dv16-B fix routed the swipe gesture to a shared predicate over the live value
# but left four DECISIONS reading the debounced string:
#
#   the 3-state button's content flag  → inside a burst the button renders a red
#       STOP over a live stream (the tap calls cancelStream and kills the run the
#       user meant to enqueue) and the idle Send stays dimmed over a composer the
#       user can see is full;
#   the slash-menu dismiss argument     → dismissSlashMenu() echoes its argument
#       back as the composer value ⇒ the just-dictated burst is cleared;
#   the slash-command argument          → same seam (its value is currently
#       ignored in favour of savedInputBeforeSlash, but the call site is the
#       documented "current input" reader and the next reader would inherit it);
#   the cross-session transfer argument → the move stashes an empty string and
#       leaves the 150 ms job alive on ChatScreen's scope, so the snapshot lands
#       on the session the user just left (inputText is per-ViewModel).
#
# Criteria — every one bound to a DECISION POINT (a captured variable name or a
# captured argument expression), never to a whole-line spelling:
#   E1  the content flag of `val <showStop> = isStreaming && !<flag>` must,
#       transitively over `val <name> = …` (<= 4 hops), reach the field's live
#       value or the shared eligibility predicate — and must NOT reach the
#       debounced string.
#   E2  the three call arguments must BE the live field read. All three sites
#       must exist (fail-closed: a missing site = the decision moved).
#   E3  each of the three sites must drop the pending burst flush before it acts
#       on the composer — inline (`imeBurstJob?.cancel()`), or through a shared
#       `() -> Unit` helper whose body cancels the job. Both shapes are legal:
#       the decision is "cancelled before the write", not the spelling.
DEBOUNCED_RE = re.compile(r"\binputText\b")
VAL_DECL_RE = re.compile(r"\bval (\w+)\s*=\s*([^\n]*)")
PRED_DECL_RE = re.compile(r"\bval (\w+):\s*\(\)\s*->\s*Boolean\s*=\s*\{")
UNIT_LAMBDA_RE = re.compile(r"\bval (\w+):\s*\(\)\s*->\s*Unit\s*=\s*\{")
FLAG_USE_RE = re.compile(r"val (\w+)\s*=\s*isStreaming\s*&&\s*!(\w+)")
CANCEL_RE = re.compile(r"imeBurstJob\s*\?\.\s*cancel\(\)")
PENDING_RE = re.compile(r"(?<![\w])PendingTransfer\(")
DISMISS_RE = re.compile(r"dismissSlashMenu\(")
EXECUTE_RE = re.compile(r"executeSlashCommand\(")


def brace_block_at(text, idx):
    """The innermost `{ … }` slice containing [idx] — the block a statement sits
    in. Offsets come from the comment-stripped text, so a prose brace in a KDoc
    cannot land the slice on the wrong block."""
    depth = 0
    start = -1
    for i in range(idx):
        c = text[i]
        if c == "{":
            depth += 1
            start = i
        elif c == "}":
            depth -= 1
            if depth <= 0:
                start = -1
    if start == -1:
        return ""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def call_arg_spans(text, open_idx):
    """Top-level argument slices of the call whose `(` sits at [open_idx]."""
    close = _balanced_paren(text, open_idx)
    if close is None:
        return None
    inner = text[open_idx + 1:close]
    spans = []
    depth = 0
    cur = 0
    for i, c in enumerate(inner):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            spans.append(inner[cur:i])
            cur = i + 1
    spans.append(inner[cur:])
    return [s.strip() for s in spans]


def composer_live_source_findings(inp_c):
    problems = []

    # ── E1: the 3-state button's content decision ───────────────────────────
    decls = {m.group(1): m.group(2) for m in VAL_DECL_RE.finditer(inp_c)}
    pred_m = PRED_DECL_RE.search(inp_c)
    pred = pred_m.group(1) if pred_m else None
    flag_m = FLAG_USE_RE.search(inp_c)
    if flag_m is None:
        problems.append(("E1", "the 3-state button's content decision "
                         "(`val <showStop> = isStreaming && !<flag>`) not found — "
                         "fail-closed"))
    else:
        flag = flag_m.group(2)
        if flag not in decls:
            problems.append(("E1", "the button's content flag `%s` has no "
                             "`val %s = …` declaration — fail-closed (keep that flag a "
                             "one-line val so the decision stays readable)" % (flag, flag)))
        else:
            seen = set()
            frontier = [flag]
            hops = 0
            live = False
            debounced = False
            while frontier and hops <= 4:
                nxt = []
                for name in frontier:
                    if name in seen:
                        continue
                    seen.add(name)
                    init = decls.get(name)
                    if init is None:
                        continue
                    if "inputFieldValue" in init:
                        live = True
                    if DEBOUNCED_RE.search(init):
                        debounced = True
                    if pred and re.search(r"\b%s\s*\(" % re.escape(pred), init):
                        live = True
                    for ident in re.findall(r"[A-Za-z_]\w*", init):
                        if ident in decls and ident not in seen:
                            nxt.append(ident)
                frontier = nxt
                hops += 1
            if debounced:
                problems.append(("E1", "the button's content decision reads the "
                                 "DEBOUNCED ViewModel string (inputText) via %s — inside a "
                                 "dictation/paste burst the button renders a red STOP over "
                                 "a live stream (the tap cancels the run the user meant to "
                                 "enqueue) and the idle Send stays dimmed over a full "
                                 "composer" % sorted(seen)))
            if not live:
                problems.append(("E1", "the button's content decision (closure %s) never "
                                 "reaches the field's live value or the shared eligibility "
                                 "predicate — it must share that single source" % sorted(seen)))

    # ── E2/E3: the three sites that leave / act on the composer ─────────────
    sites = []
    for cid, label, rx in (("E2-dismiss", "dismiss", DISMISS_RE),
                           ("E2-execute", "execute", EXECUTE_RE),
                           ("E2-transfer", "transfer", PENDING_RE)):
        m = rx.search(inp_c)
        if m is None:
            problems.append((cid, "the %s site (%s…) is missing — fail-closed (these three "
                             "decisions must be visible to the gate)" % (label, rx.pattern)))
            continue
        sites.append((cid, label, m.end() - 1, call_arg_spans(inp_c, m.end() - 1)))

    for cid, label, open_idx, args in sites:
        if args is None:
            problems.append((cid, "unbalanced argument list — fail-closed"))
            continue
        if cid == "E2-execute":
            arg = args[-1] if len(args) >= 2 else None
        elif cid == "E2-transfer":
            arg = None
            for a in args:
                am = re.match(r"inputText\s*=\s*(.*)$", a, re.S)
                if am:
                    arg = am.group(1).strip()
            if arg is None:
                problems.append((cid, "no `inputText = …` named argument — fail-closed"))
                continue
        else:
            arg = args[0]
        if DEBOUNCED_RE.search(arg):
            problems.append((cid, "the %s site passes the DEBOUNCED ViewModel string "
                             "(inputText) to the composer — the 150 ms burst window makes "
                             "it stale (empty right after dictation); it must pass the "
                             "field's own TextFieldValue" % label))
        elif "inputFieldValue" not in arg:
            problems.append((cid, "the %s site passes `%s`, not the field's live value "
                             "(inputFieldValue.text)" % (label, arg[:60])))

    # ── E3: drop the pending burst flush before acting on the composer ──────
    helper = None
    for hm in UNIT_LAMBDA_RE.finditer(inp_c):
        if CANCEL_RE.search(brace_block_at(inp_c, hm.end())):
            helper = hm.group(1)
            break
    inline_any = False
    for cid, label, open_idx, _args in sites:
        block = brace_block_at(inp_c, open_idx)
        ok = bool(helper and re.search(r"\b%s\s*\(\)" % re.escape(helper), block))
        if not ok:
            inline_any = inline_any or bool(CANCEL_RE.search(block))
            ok = bool(CANCEL_RE.search(block))
        if not ok:
            problems.append(("E3", "the %s site does not drop the pending IME-burst flush "
                             "before it acts on the composer: cancel the job (or call the "
                             "shared cancel helper) so the 150 ms snapshot cannot commit "
                             "over the value this site just wrote — after a cross-session "
                             "transfer it would land on the session the user left" % label))
    if helper is None and not inline_any:
        problems.append(("E3", "no pending-burst cancel found anywhere — fail-closed "
                         "(either a `() -> Unit` helper whose body cancels imeBurstJob, or "
                         "an inline cancel at each site)"))
    return problems


# ── the gate ─────────────────────────────────────────────────────────────────

def check_tree(root):
    """Returns (None, [problems]) when a target file is missing, else
    (path-of-first-problem, problems)."""
    problems = []

    def miss(rel):
        problems.append(("missing:" + rel, "target file missing — fail-closed"))
        return None

    models = read(root, MODELS_REL)
    token = read(root, TOKEN_REL)
    inp = read(root, INPUT_REL)
    gesture = read(root, GESTURE_REL)
    selection = read(root, SELECTION_REL)
    stream = read(root, STREAM_REL)
    mdtoolbar = read(root, MDTOOLBAR_REL)
    for rel, src in ((MODELS_REL, models), (TOKEN_REL, token), (INPUT_REL, inp),
                     (GESTURE_REL, gesture), (SELECTION_REL, selection),
                     (STREAM_REL, stream), (MDTOOLBAR_REL, mdtoolbar)):
        if src is None:
            miss(rel)
    if problems:
        return None, problems

    models_c = strip_strings(strip_comments(models))
    token_c = strip_strings(strip_comments(token))
    inp_c = strip_strings(strip_comments(inp))
    gesture_c = strip_strings(strip_comments(gesture))
    selection_c = strip_strings(strip_comments(selection))
    stream_c = strip_strings(strip_comments(stream))
    mdtoolbar_c = strip_strings(strip_comments(mdtoolbar))

    # ── A1: every SlashCommand boolean field has >= 1 read point ────────────
    # SlashCommand is a primary-constructor-only data class (no body braces),
    # so the field list is captured from the parameter list (paren-matched) —
    # slicing from the first `{` would run into the NEXT declaration's body
    # and pick up its computed properties (isText lives on AssistantBlock).
    cmd_params = signature_args(models_c, "data class SlashCommand(")
    bool_fields = re.findall(r"val\s+(\w+)\s*:\s*Boolean", cmd_params or "")
    if not bool_fields:
        problems.append((MODELS_REL, "A1 SlashCommand declares no boolean fields — "
                         "fail-closed (descriptor shape drifted?)"))
    consumers = token_c + inp_c
    for field in bool_fields:
        reads = len(re.findall(r"\.%s\b" % re.escape(field), consumers))
        if reads == 0:
            problems.append((
                "A1:%s" % field,
                "A1 SlashCommand.%s is written but has 0 read points across the "
                "slash consumers (ChatSlashTokenExt + ChatInputArea) — the "
                "B-01 class: a written-but-never-read descriptor flag" % field,
            ))

    # ── A2: executeSlashCommand carries an isMcp branch ─────────────────────
    exec_slice = declaration_slice(token_c, "fun ChatViewModel.executeSlashCommand(")
    if not exec_slice:
        problems.append(("A2", "executeSlashCommand not found — fail-closed"))
    else:
        if "if (cmd.isMcp)" not in exec_slice:
            problems.append(("A2", "executeSlashCommand has no `if (cmd.isMcp)` "
                             "branch — MCP row taps fall into the action dispatch and "
                             "wipe the composer (B-01)"))
        if FILL_HELPER not in exec_slice:
            problems.append(("A2", "executeSlashCommand must route the "
                             "typing-aid fill through the shared helper %s (single "
                             "source for the prepend/caret semantics)" % FILL_HELPER))

    # ── A3: isMcp read in a ChatInputArea condition ─────────────────────────
    if len(re.findall(r"\.isMcp\b", inp_c)) == 0:
        problems.append(("A3", "ChatInputArea never reads isMcp — the "
                         "installed-row divider / IME restore stay skill-only and an "
                         "MCP tap drops the keyboard (B-01)"))

    # ── B1: main-chat toolbar wires the table actions ───────────────────────
    if "selectionTableActions()" not in gesture_c:
        problems.append(("B1", "MinisSelectionToolbarHost never calls "
                         "selectionTableActions() — the main-chat long-press toolbar "
                         "never surfaces Copy Table / Copy Table Image (B-02)"))

    # ── B2: the markdown-toolbar path stays a consumer ──────────────────────
    if "selectionTableActions()" not in mdtoolbar_c:
        problems.append(("B2", "MinisMarkdownTextToolbar no longer calls "
                         "selectionTableActions() — that path is a legitimate "
                         "consumer; deleting it is a regression"))

    # ── B3: table-level registry key ────────────────────────────────────────
    remember_args = signature_args(selection_c, "fun rememberTableActions(")
    forget_args = signature_args(selection_c, "fun forgetTableActions(")
    if remember_args is None or ": Int" not in remember_args or remember_args.count(",") < 2:
        problems.append(("B3", "rememberTableActions must take a table-"
                         "level key (an Int dimension beyond messageId) — a bare "
                         "messageId key lets same-message tables overwrite each "
                         "other and a dispose drop its sibling (B-02)"))
    if forget_args is None or ": Int" not in forget_args:
        problems.append(("B3", "forgetTableActions must take the table "
                         "key too — a bare messageId remove takes whatever is "
                         "stored, including a still-composed sibling table"))
    sel_slice = declaration_slice(selection_c, "fun selectionTableActions(")
    if sel_slice:
        if not re.search(r"(Map<Int|SnapshotStateMap<Int)", selection_c):
            problems.append(("B3", "the table registry declaration must be "
                             "two-level (an Int table dimension inside the per-message "
                             "map)"))
    else:
        problems.append(("B3", "selectionTableActions not found — fail-closed"))

    # ── B4: cross-message exclusion preserved ───────────────────────────────
    if sel_slice and "singleMessageId()" not in sel_slice:
        problems.append(("B4", "selectionTableActions must still guard on "
                         "singleMessageId() — a cross-message selection must not "
                         "expose single-table actions"))

    # ── B5: publisher passes the table index ────────────────────────────────
    pub_call = signature_args(stream_c, ".rememberTableActions(")
    if pub_call is None or pub_call.count(",") != 2:
        problems.append(("B5", "RenderTable must publish under the table key "
                         "(a 3-arg rememberTableActions call: messageId, tableIndex, "
                         "actions)"))
    if "LocalShardSubIndexAllocator.current" not in stream_c:
        problems.append(("B5", "RenderTable must source the table index from "
                         "the shard allocator (a stable per-slot identity within the "
                         "fragment)"))

    # ── H: the streaming block ledger (dv21-B4-B05) ─────────────────────────
    problems.extend(streaming_block_ledger_findings(stream_c))

    # ── swipe block: delimited by the pointerInput lambda ───────────────────
    g_idx = inp_c.find("pointerInput(Unit)")
    if g_idx == -1:
        problems.append(("swipe-block", "swipe gesture block not found (pointerInput(Unit)) "
                         "— fail-closed"))
        gesture_slice = ""
    else:
        gesture_slice = declaration_slice(inp_c, "pointerInput(Unit)")

    # ── C1/C2: one shared eligibility predicate, read at call time ──────────
    preds = PRED_RE.findall(inp_c)
    send_preds = [name for name in preds
                  if re.search(r"val %s:\s*\(\)\s*->\s*Boolean\s*=\s*\{([^\n]*)" % re.escape(name), inp_c)
                  and "inputFieldValue" in inp_c]
    # Narrow to the predicate whose body carries the field-value + attachments reads.
    body_match = re.search(r"val (\w+):\s*\(\)\s*->\s*Boolean\s*=\s*\{\s*inputFieldValue", inp_c)
    if body_match is None:
        problems.append(("C1", "no shared send-eligibility predicate found — the "
                         "three swipe decision sites must read one predicate "
                         "(`val <name>: () -> Boolean = { inputFieldValue.text.isNotBlank()"
                         " || attachments.isNotEmpty() }`), not re-derive it per site (B-06)"))
    else:
        name = body_match.group(1)
        # The body spans lines — capture from the opening brace to the matching
        # close (same-line capture would see only the newline).
        brace = inp_c.find("{", body_match.start())
        close = inp_c.find("}", brace)
        pred_body = inp_c[brace:close] if brace != -1 and close != -1 else ""
        if "inputText.isNotBlank()" in pred_body:
            problems.append((INPUT_REL, "C1 the swipe predicate reads the debounced VM "
                             "string (inputText) — a just-typed prefix is invisible for "
                             "the debounce window; it must read the field's own "
                             "TextFieldValue"))
        if "attachments" not in pred_body:
            problems.append(("C1", "C1 the swipe predicate ignores attachments — "
                             "attachment-only drags must count as sendable (B-06)"))
        calls = len(re.findall(r"\b%s\(\)" % re.escape(name), inp_c))
        if calls < 3:
            problems.append(("C2", "C2 the shared predicate is used %d time(s) — the "
                             "three decision sites (drag hint / release-send / empty-input "
                             "keyboard) must all read it, or a per-site re-derivation "
                             "drifts back" % calls))

    # ── C3: nested dead-branch shape (shape-detected, not name-bound) ───────
    lines = gesture_slice.split("\n")
    for i, line in enumerate(lines):
        m = IF_AND_RE.search(line)
        if not m:
            continue
        second = m.group(2)
        window = [l for l in lines[i + 1:i + 5] if l.strip()]
        for l in window:
            if re.search(r"val\s+\w+\s*=\s*%s\b\s*(\(\))?\s*\|\|" % re.escape(second), l):
                problems.append(("C3", "dead branch: an eligibility value is "
                                 "re-derived from `%s` inside a block already guarded by "
                                 "`&& %s` — the inner test is tautological and routes "
                                 "attachment-only drags to the keyboard (B-06)" % (second, second)))
                break

    # ── C4: threshold via rememberUpdatedState ──────────────────────────────
    thr = re.search(r"val (\w+)\s*=\s*rememberUpdatedState\(\s*with\(LocalDensity\.current\)\s*\{\s*sendSwipeThresholdDp\.dp\.toPx\(\)", inp_c)
    if thr is None:
        problems.append(("C4", "the swipe threshold must read through "
                         "rememberUpdatedState — a plain val capture freezes the first "
                         "composition's px and a ChatTuning adjustment never reaches the "
                         "gesture"))
    else:
        thr_name = thr.group(1)
        if "%s.value" % thr_name not in gesture_slice:
            problems.append(("C4", "the gesture must read the threshold state "
                             "(%s.value), or the state holder is dead weight" % thr_name))

    # ── C5/C6: preserved behaviors inside the gesture ───────────────────────
    if gesture_slice:
        if "performSendOrEnqueue()" not in gesture_slice:
            problems.append(("C5", "the release branch must still route through "
                             "performSendOrEnqueue() — streaming drag-to-send enqueues "
                             "instead of being dropped"))
        if "!inputFocused" not in gesture_slice:
            problems.append(("C6", "the empty-input keyboard shortcut "
                             "(!inputFocused) must survive — swiping up with nothing to "
                             "send and no focus pops the keyboard"))

    # ── D1-D3: slash-menu memo wiring (dv17-4 / B-05) ───────────────────────
    problems.extend(slash_memo_findings(inp_c))

    # ── E1-E3: composer decisions read the live field value (dv19-B1) ──────
    problems.extend(composer_live_source_findings(inp_c))
    problems.extend(composer_clear_funnel_findings(inp_c))

    return INPUT_REL, problems


# ── selftest fixtures ────────────────────────────────────────────────────────

MODELS_F = """package com.rikkaminis.app.ui.chat

@Immutable
data class SlashCommand(
    val id: String,
    val icon: androidx.compose.ui.graphics.vector.ImageVector,
    val title: String,
    val subtitle: String,
    val isSkill: Boolean = false,
    val isMcp: Boolean = false,
)
"""

TOKEN_F = """package com.rikkaminis.app.ui.chat

fun ChatViewModel.executeSlashCommand(cmd: SlashCommand, currentInput: String = ""): String {
    val saved = savedInputBeforeSlash
    if (cmd.isSkill) {
        resetSlashMenuState()
        return fillComposerWithSlashTitle(cmd, saved)
    }
    if (cmd.isMcp) {
        resetSlashMenuState()
        return fillComposerWithSlashTitle(cmd, saved)
    }
    when (cmd.id) {
        "memory" -> toggleMemoryEnabled()
        else -> Unit
    }
    return ""
}
"""

INPUT_F = """package com.rikkaminis.app.ui.chat

@Composable
fun ChatInputArea() {
    val swipeThresholdPxState = rememberUpdatedState(
        with(LocalDensity.current) { sendSwipeThresholdDp.dp.toPx() },
    )
    val showSlashMenu by viewModel.showSlashMenu.collectAsState()
    val filteredSlashCommands = if (showSlashMenu) {
        remember(
            viewModel.slashFilter.collectAsState().value,
            viewModel.memoryEnabled.collectAsState().value,
            viewModel.currentSessionId,
            viewModel.skillRepository?.skills?.collectAsState()?.value,
            viewModel.mcpRepository?.servers?.collectAsState()?.value,
        ) { viewModel.filteredSlashCommands() }
    } else {
        emptyList()
    }
    if (showSlashMenu && filteredSlashCommands.isNotEmpty()) {
        Text(filteredSlashCommands.size.toString())
        Popup(onDismissRequest = {
            cancelPendingImeBurst()
            viewModel.setInputText(viewModel.dismissSlashMenu(inputFieldValue.text))
        })
        Row {
            if (!isThinking) {
                it.clickable {
                    cancelPendingImeBurst()
                    viewModel.setInputText(viewModel.executeSlashCommand(cmd, inputFieldValue.text))
                }
            }
        }
    }
    val cancelPendingImeBurst: () -> Unit = {
        imeBurstJob?.cancel()
        imeBurstJob = null
        imeBurstBuffer = null
    }
    val clearComposerInput: () -> Unit = {
        cancelPendingImeBurst()
        viewModel.setInputText("")
        inputFieldValue = TextFieldValue("")
        lastTrueCaretEnd = 0
    }
    val performSendOrEnqueue: () -> Unit = handler@{
        val toSend = inputFieldValue.text
        if (viewModel.tryExecuteInputAsSlashCommand(toSend)) {
            clearComposerInput()
            return@handler
        }
        clearComposerInput()
        viewModel.sendMessage(toSend)
    }
    Row {
        Surface(modifier = Modifier.clickable {
            viewModel.cancelEdit()
            clearComposerInput()
        }) { }
    }
    val canSwipeSendNow: () -> Boolean = {
        inputFieldValue.text.isNotBlank() || attachments.isNotEmpty()
    }
    itemsIndexed(filteredSlashCommands, key = { _, c -> c.id }) { index, cmd ->
        val isInstalledRow = cmd.isSkill || cmd.isMcp
        val prevIsInstalledRow = index > 0 &&
            (filteredSlashCommands[index - 1].isSkill ||
                filteredSlashCommands[index - 1].isMcp)
        if (isInstalledRow && index > 0 && !prevIsInstalledRow) {
            HorizontalDivider()
        }
        if (cmd.isSkill || cmd.isMcp) {
            inputFocusRequester.requestFocus()
        }
    }
    Box(modifier = Modifier
        .fillMaxWidth()
        .pointerInput(Unit) {
            val slop = viewConfiguration.touchSlop
            awaitEachGesture {
                val down = awaitFirstDown(requireUnconsumed = false)
                var totalDy = 0f
                var claimed = false
                verticalDrag(down.id) { change ->
                    if (totalDy < 0) {
                        if (canSwipeSendNow()) {
                            val newProgress = (-totalDy / swipeThresholdPxState.value).coerceIn(0f, 1f)
                            sendSwipeProgress = newProgress
                        } else if (sendSwipeProgress != 0f) {
                            sendSwipeProgress = 0f
                        }
                    }
                }
                val swipedUp = claimed && totalDy < 0
                if (swipedUp && canSwipeSendNow()) {
                    if (sendSwipeProgress >= swipeArmFraction) {
                        performSendOrEnqueue()
                    }
                    sendSwipeProgress = 0f
                } else if (swipedUp && !canSwipeSendNow() && !inputFocused) {
                    inputFocusRequester.requestFocus()
                    sendSwipeProgress = 0f
                } else {
                    sendSwipeProgress = 0f
                }
            }
        },
    ) {
        Column {
        }
    }
    val hasContent = canSwipeSendNow()
    val showStop = isStreaming && !hasContent
    if (showMoveSheet) {
        MoveToSessionSheet(onSelect = { targetId ->
            cancelPendingImeBurst()
            ChatViewModelStore.stashPendingTransfer(
                ChatViewModelStore.PendingTransfer(
                    inputText = inputFieldValue.text,
                    attachments = viewModel.attachments.value,
                ),
            )
            clearComposerInput()
        })
    }
}
"""

GESTURE_F = """package com.rikkaminis.app.ui.chat

@Composable
fun MinisSelectionToolbarHost(controller: SelectionController) {
    Row {
        MinisToolbarButton(label = "Copy") { controller.clearSelection() }
        val tableActions = controller.selectionTableActions()
        if (tableActions != null) {
            MinisToolbarButton(label = "Copy Table") { tableActions.copyTableMarkdown() }
            MinisToolbarButton(label = "Copy Table Image") { tableActions.copyTableImage() }
        }
    }
}
"""

SELECTION_F = """package com.rikkaminis.app.ui.chat

class SelectionController {
    class TableActions(
        val copyTableMarkdown: () -> Unit,
        val copyTableImage: () -> Unit,
    )
    private val tableActionsByMessage =
        androidx.compose.runtime.mutableStateMapOf<
            String,
            androidx.compose.runtime.snapshots.SnapshotStateMap<Int, TableActions>
        >()
    fun rememberTableActions(messageId: String, tableIndex: Int, actions: TableActions) {
        val perMessage = tableActionsByMessage.getOrPut(messageId) {
            androidx.compose.runtime.mutableStateMapOf()
        }
        perMessage[tableIndex] = actions
    }
    fun forgetTableActions(messageId: String, tableIndex: Int) {
        val perMessage = tableActionsByMessage[messageId] ?: return
        perMessage.remove(tableIndex)
        if (perMessage.isEmpty()) tableActionsByMessage.remove(messageId)
    }
    fun selectionTableActions(): TableActions? {
        val msgId = singleMessageId() ?: return null
        val perMessage = tableActionsByMessage[msgId] ?: return null
        for (endpoint in listOf(selection.value.start, selection.value.end)) {
            val tableIndex = tableIndexFromShard(endpoint.shard.shardId) ?: continue
            perMessage[tableIndex]?.let { return it }
        }
        return null
    }
}
"""

STREAM_F = """package com.rikkaminis.app.ui.chat

private const val COLD_PARSE_OFFMAIN_THRESHOLD_CHARS = 2_000

internal object StreamingBlockLedger {
    data class Published(val blocks: List<MdBlock>, val source: String?)
    fun seed(blocks: List<MdBlock>?, raw: String): Published =
        Published(blocks.orEmpty(), if (blocks != null) raw else null)
    fun publish(computed: List<MdBlock>, raw: String): Published = Published(computed, raw)
    fun rendered(published: Published, seed: List<MdBlock>?): List<MdBlock> =
        if (seed != null && published.blocks !== seed) seed else published.blocks
    fun isConvergedWith(published: Published, raw: String, isStreaming: Boolean): Boolean =
        !isStreaming && published.source == raw
}

@Composable
private fun RenderTable(block: MdBlock.Table) {
    val selectionController = LocalMinisSelectionController.current
    val shardIdForTable = LocalShardId.current
    val messageId = shardIdForTable?.messageId
    val tableSlotAllocator = LocalShardSubIndexAllocator.current
    val tableIndex = remember { tableSlotAllocator?.next() ?: 0 }
    if (selectionController != null && messageId != null) {
        androidx.compose.runtime.DisposableEffect(selectionController, messageId, block.raw, tableIndex) {
            selectionController.rememberTableActions(messageId, tableIndex, actions)
            onDispose { selectionController.forgetTableActions(messageId, tableIndex) }
        }
    }
}

@Composable
private fun StreamingMarkdownTextBody(
    content: String,
    isStreaming: Boolean,
    modifier: Modifier = Modifier,
) {
    val displayContent = content
    val mdColors = currentMdColors()
    val warmSeed = remember(displayContent, isStreaming) {
        MarkdownParseCaches.cachedBlocks(displayContent)
    }
    var published by remember {
        mutableStateOf(StreamingBlockLedger.seed(warmSeed, displayContent))
    }
    val blocks = StreamingBlockLedger.rendered(published, warmSeed)
    LaunchedEffect(displayContent, isStreaming) {
        if (warmSeed != null) {
            published = StreamingBlockLedger.seed(warmSeed, displayContent)
            return@LaunchedEffect
        }
        if (StreamingBlockLedger.isConvergedWith(published, displayContent, isStreaming)) {
            if (displayContent.length >= COLD_PARSE_OFFMAIN_THRESHOLD_CHARS) {
                MarkdownParseCaches.putBlocks(displayContent, published.blocks)
            }
            return@LaunchedEffect
        }
        val computed = withContext(Dispatchers.Default) {
            parseMarkdownBlocks(displayContent).also {
                MarkdownParseCaches.prewarm(it, mdColors)
                if (!isStreaming && displayContent.length >= COLD_PARSE_OFFMAIN_THRESHOLD_CHARS) {
                    MarkdownParseCaches.putBlocks(displayContent, it)
                }
            }
        }
        published = StreamingBlockLedger.publish(computed, displayContent)
    }
    Column(modifier = modifier) {
        val lastIdx = blocks.size - 1
        blocks.forEachIndexed { idx, block -> RenderBlock(block) }
    }
}
"""

MDTOOLBAR_F = """package com.rikkaminis.app.ui.chat

@Composable
fun MinisMarkdownTextToolbarHost(toolbar: Toolbar) {
    Row {
        ToolbarButton(label = "Copy") { toolbar.hide() }
        val tableActions = toolbar.selectionController?.selectionTableActions()
        if (tableActions != null) {
            ToolbarButton(label = "Copy Table") { tableActions.copyTableMarkdown() }
        }
    }
}
"""


# ---------------------------------------------------------------------------
# F (dv20-B03): the composer clear is one funnel
# ---------------------------------------------------------------------------
CLEAR_WRITE_RE = re.compile(r"setInputText\s*\(\s*\"\"\s*\)")
FIELD_WRITE_RE = re.compile(r"\binputFieldValue\s*=")
CARET_RESET_RE = re.compile(r"\blastTrueCaretEnd\s*=\s*0\b")
BURST_DROP_RE = re.compile(
    r"cancelPendingImeBurst\s*\(\s*\)|imeBurstJob\s*\?\.\s*cancel\s*\(\)")
FUNNEL_LAMBDA_RE = re.compile(r"\bval\s+(\w+)\s*:\s*\(\)\s*->\s*Unit\s*=\s*\{")
FUNNEL_CONSUMERS = [
    (re.compile(r"\bviewModel\.sendMessage\s*\("), "send path"),
    (re.compile(r"\btryExecuteInputAsSlashCommand\s*\("), "slash-command branch"),
    (re.compile(r"\bstashPendingTransfer\s*\("), "cross-session move"),
    (re.compile(r"\bviewModel\.cancelEdit\s*\("), "exit-edit pill"),
]


def _innermost_brace_span(text, idx):
    """(open_idx, close_idx) of the innermost {...} containing [idx]."""
    depth = 0
    start = -1
    for i in range(idx):
        c = text[i]
        if c == "{":
            depth += 1
            start = i
        elif c == "}":
            depth -= 1
            if depth <= 0:
                start = -1
    if start == -1:
        return None
    d = 0
    for j in range(start, len(text)):
        if text[j] == "{":
            d += 1
        elif text[j] == "}":
            d -= 1
            if d == 0:
                return (start, j)
    return None


def composer_clear_funnel_findings(inp_c):
    """Every composer-clear routes through one shared funnel (dv20-B03).

    The clear entrances used to commit an empty string on their own. That write
    is suppressed by the ViewModel's StateFlow (equal values do not emit), so
    the inputText mirror never re-runs and the composer keeps the text the user
    just sent. The funnel commits the empty string AND resets inputFieldValue +
    lastTrueCaretEnd, and drops the pending IME burst so a still-alive 150ms
    flush cannot resurrect the sent text.
    """
    problems = []
    clears = list(CLEAR_WRITE_RE.finditer(inp_c))
    if not clears:
        problems.append(("F", "no composer-clear write found in ChatInputArea.kt "
                              "-- the external-clear anchor moved, re-point this guard"))
        return problems
    funnels = []
    for m in FUNNEL_LAMBDA_RE.finditer(inp_c):
        ob = inp_c.find("{", m.end() - 1)
        if ob < 0:
            continue
        # ob+1: count the lambda's own opening brace, so the span IS the lambda
        # body (passing ob would return the enclosing block instead)
        span = _innermost_brace_span(inp_c, ob + 1)
        if span is None:
            continue
        b0, b1 = span
        if any(b0 <= c.start() < b1 for c in clears):
            funnels.append((m.group(1), b0, b1))
    if not funnels:
        problems.append(("F", "%d composer-clear write(s) commit the empty string on "
                              "their own with no shared clear funnel -- the field reset "
                              "drifts out of the entrances" % len(clears)))
        return problems
    if len(funnels) > 1:
        problems.append(("F", "%d separate composer-clear funnels (%s) -- two clear "
                              "definitions will drift apart, keep one"
                              % (len(funnels), ", ".join(f[0] for f in funnels))))
        return problems
    fname, f0, f1 = funnels[0]
    body = inp_c[f0:f1 + 1]
    if not FIELD_WRITE_RE.search(body):
        problems.append(("F1", "the clear funnel commits the empty string but never "
                               "resets inputFieldValue -- the VM write is suppressed "
                               "when the string is already empty, so the field keeps "
                               "the text that was just sent"))
    if not CARET_RESET_RE.search(body):
        problems.append(("F1", "the clear funnel never resets lastTrueCaretEnd -- a "
                               "stale caret index survives the clear and lands in the "
                               "next draft"))
    if not BURST_DROP_RE.search(body):
        problems.append(("F1", "the clear funnel does not drop the pending IME burst "
                               "-- a still-alive 150ms flush commits the snapshot "
                               "after the clear and resurrects the sent text"))
    for c in clears:
        if not (f0 <= c.start() <= f1):
            line = inp_c.count("\n", 0, c.start()) + 1
            problems.append(("F2", "a composer-clear at line %d commits the empty "
                                   "string outside the clear funnel -- the field reset "
                                   "is bypassed for this entrance" % line))
    for m in re.finditer(r"\b%s\s*\(\s*\)" % re.escape(fname), inp_c):
        if m.start() < f0:
            line = inp_c.count("\n", 0, m.start()) + 1
            problems.append(("F3", "%s is called at line %d before its declaration -- "
                                   "a local val lambda cannot be referenced before its "
                                   "declaration" % (fname, line)))
    # Direction + same-path rules, measured on the tree this guard was written
    # against. Block containment alone was too coarse for the send path: the
    # enclosing lambda also holds the slash-command branch's funnel call, so a
    # send that skipped the funnel still sat "inside a block that routes through
    # it" (found by the FIX-B3 mutation run, m5). What the defect actually breaks
    # is the *sequence on one path*, so each anchor is checked in its measured
    # direction and must not cross a block boundary or a return.
    FUNNEL_RULES = {
        "send path": ("before", 200),
        "cross-session move": ("after", 400),
        "exit-edit pill": ("after", 200),
    }
    BOUNDARY_RE = re.compile(r"[{}]|\breturn@")
    call_re = re.compile(r"\b%s\s*\(\s*\)" % re.escape(fname))
    calls = [m.start() for m in call_re.finditer(inp_c)]
    for anchor, label in FUNNEL_CONSUMERS:
        hits = list(anchor.finditer(inp_c))
        if not hits:
            problems.append(("F4", "the %s anchor moved -- re-point this guard" % label))
            continue
        for h in hits:
            if label == "slash-command branch":
                # the funnel must be the FIRST statement of the if-body
                p = inp_c.find("(", h.end() - 1)
                close = -1
                if p >= 0:
                    depth = 0
                    for j in range(p, min(len(inp_c), p + 2000)):
                        if inp_c[j] == "(":
                            depth += 1
                        elif inp_c[j] == ")":
                            depth -= 1
                            if depth == 0:
                                close = j
                                break
                if close < 0:
                    problems.append(("F4", "cannot locate the %s condition -- re-point"
                                           % label))
                    continue
                # the call sits inside an `if (...)` condition, so the run of
                # closing parens that ends the enclosing guard sits between the
                # call's `)` and the body's `{` -- skip it (measured shape:
                # `if (viewModel.tryExecuteInputAsSlashCommand(x)) {`)
                j = close + 1
                while j < len(inp_c) and inp_c[j] in ") \t\r\n":
                    j += 1
                if inp_c[j:j + 1] != "{":
                    problems.append(("F4", "the %s has no brace body -- re-point"
                                           % label))
                    continue
                ok = call_re.match(inp_c[j + 1:].lstrip()) is not None
                if not ok:
                    line = inp_c.count("\n", 0, h.start()) + 1
                    problems.append(("F4", "the %s at line %d acts on the composer "
                                           "without routing through %s as the first "
                                           "statement of its body -- the field reset is "
                                           "bypassed" % (label, line, fname)))
                continue
            direction, reach = FUNNEL_RULES[label]
            if direction == "before":
                prev = [c for c in calls if c < h.start()]
                ok = (bool(prev) and h.start() - prev[-1] < reach
                      and not BOUNDARY_RE.search(inp_c[prev[-1]:h.start()]))
            else:
                nxt = [c for c in calls if c > h.end()]
                ok = (bool(nxt) and nxt[0] - h.end() < reach
                      and not BOUNDARY_RE.search(inp_c[h.end():nxt[0]]))
            if not ok:
                line = inp_c.count("\n", 0, h.start()) + 1
                problems.append(("F4", "the %s at line %d acts on the composer without "
                                       "routing through %s on the same path -- the field "
                                       "reset is bypassed"
                                       % (label, line, fname)))
    return problems


def _guard_before_brace(text, ob):
    """The `if (...)` condition whose body opens at [ob], or None when the brace
    opens a bare block / lambda."""
    j = ob - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j < 0 or text[j] != ")":
        return None
    depth = 0
    k = j
    while k >= 0:
        if text[k] == ")":
            depth += 1
        elif text[k] == "(":
            depth -= 1
            if depth == 0:
                break
        k -= 1
    if k < 0:
        return None
    if not text[max(0, k - 8):k].rstrip().endswith("if"):
        return None
    return text[k + 1:j]


def _enclosing_condition_chain(text, idx, floor):
    """Every `if (...)` condition on the path from [idx] out to [floor].

    A deposit nested inside two conditions (stream ended AND long enough) is
    governed by BOTH — reading only the innermost one would report a missing
    `isStreaming` guard on a correctly guarded deposit, and reading only the
    outermost would let a source check sit on a branch that cannot reach it.
    Anchoring on enclosing BLOCKS (not on a backward scan for the token `if`)
    keeps a nested lambda's own parentheses out of the reading."""
    conds = []
    cur = idx
    while cur > floor:
        span = _innermost_brace_span(text, cur)
        if span is None or span[0] <= floor:
            break
        cond = _guard_before_brace(text, span[0])
        if cond is not None:
            conds.append(cond)
        cur = span[0] - 1
    return conds


BLOCK_STATE_PAIR_RE = re.compile(
    r"mutableStateOf\s*\(\s*[\w.]+\(\s*[^()]*?\b(displayContent|content)\b\s*\)")
BARE_LIST_STATE_RE = re.compile(r"mutableStateOf\s*<\s*List\s*<\s*MdBlock\s*>")
CACHE_PEEK_RE = re.compile(r"\.cachedBlocks\s*\(")


def streaming_block_ledger_findings(stream_c):
    """[dv21-B4-B05] The streaming body's block state must be a (blocks, source)
    ledger, and the frame where a stream CONVERGES must both re-enter the
    publishing effect and deposit through a source-checked guard.

    Two field-visible defects came out of the bare `List<MdBlock>` in an unkeyed
    `remember`:

      (a) a body REUSED for another message drew the outgoing message's blocks
          for one frame, then re-parsed a list the cache already had (the
          identity check `blocks === warmSeed` compared against a list it had
          thrown away, so a HIT was recorded as FROZEN_MISS);
      (b) the convergence frame never deposited — the deposit is gated on
          `!isStreaming`, a coroutine already running keeps the `true` it
          captured, and the effect's only key (`displayContent`) had already
          reached its final value — so every long streamed answer stayed out of
          blocksLru and was re-parsed on each scroll-back.

    Four decisions, not four spellings:

      H1  the cache-seeded `remember` is keyed on the streaming flag AS WELL AS
          the content (a converged frame must re-read a seed it skipped while
          live);
      H2  the block state carries the text it was parsed from (no bare
          `List<MdBlock>` state; the initializer pairs blocks with the content);
      H3  the publishing `LaunchedEffect` is keyed on the streaming flag too, or
          the convergence frame never runs at all;
      H4  a deposit whose list was NOT freshly parsed must consult that list's
          ledger in its guard — caching a prefix that a cancelled parse left
          behind is a durable truncated render, worse than a cold cache.

    H4 is deliberately keyed on "the guard names the state that owns the
    deposited list": a refactor that renames the ledger must keep that property
    (the guard consults the source), but is free to rename the helper.
    """
    problems = []

    # Scope: the ACTIVE renderer only. `MarkdownDocument` / `MarkdownBlockBody`
    # keep their own bare `List<MdBlock>` state and are RUNTIME-DEAD while
    # AGGREGATE_MESSAGE_ITEMS is true (the audit's own 失真 #1: this gate must
    # not demand a change there — the barrier is deliberate).
    body = declaration_slice(stream_c, "fun StreamingMarkdownTextBody(")
    body_off = stream_c.find(body)
    if not body:
        problems.append(("H", "`StreamingMarkdownTextBody` not found — the streaming "
                              "renderer this family pins moved, re-point this guard "
                              "(fail-closed)"))
        return problems

    seed_keys = None
    for m in re.finditer(r"\bremember\s*\(", body):
        op = m.end() - 1
        cl = _balanced_paren(body, op)
        if cl is None:
            continue
        window = body[cl:cl + 500]
        if not CACHE_PEEK_RE.search(window):
            continue
        seed_keys = body[op + 1:cl]
        break
    if seed_keys is None:
        problems.append(("H1", "no `remember { … cachedBlocks(…) }` seed found — the "
                               "warm-cache seed anchor moved, re-point this guard"))
    else:
        if "isStreaming" not in seed_keys:
            problems.append(("H1", "the warm-cache seed is not keyed on the streaming "
                                   "flag (keys: %r) — a stream that converges keeps the "
                                   "`null` seed cached from its live frames, so the "
                                   "final frame misses a cache that should hit" % seed_keys.strip()))
        if not re.search(r"\b(displayContent|content)\b", seed_keys):
            problems.append(("H1", "the warm-cache seed is not keyed on the content — "
                                   "a reused body would keep the previous message's seed"))

    # ── H2: the block state carries its source text ─────────────────────────
    if BARE_LIST_STATE_RE.search(body):
        problems.append(("H2", "the block state is still a bare `List<MdBlock>` — "
                               "without the text it was parsed from, a convergence "
                               "deposit cannot tell its own content from a cancelled "
                               "prefix, and a reused body cannot tell its own message "
                               "from the previous one"))
    elif not BLOCK_STATE_PAIR_RE.search(body):
        problems.append(("H2", "no block state pairs its blocks with the content it "
                               "was parsed from (expected `<ledger>(<blocks>, "
                               "<content>)` in the state initializer) — re-point this "
                               "guard if the pairing moved"))

    # ── H3/H4: the publishing effect re-enters, and every deposit is guarded ─
    effect_body = None
    effect_keys = None
    for m in re.finditer(r"\bLaunchedEffect\s*\(", body):
        op = m.end() - 1
        cl = _balanced_paren(body, op)
        if cl is None:
            continue
        tail = body[cl:cl + 3000]
        if ".putBlocks(" not in tail:
            continue
        effect_keys = body[op + 1:cl]
        body_start = body.find("{", cl)
        span = _innermost_brace_span(body, body_start + 1) if body_start >= 0 else None
        effect_body = body[body_start:span[1] + 1] if span else None
        effect_body_off = body_start
        break
    if effect_body is None:
        problems.append(("H3", "no publishing `LaunchedEffect` that reaches "
                               "putBlocks( — the deposit anchor moved, re-point this "
                               "guard"))
        return problems
    if "isStreaming" not in (effect_keys or ""):
        problems.append(("H3", "the publishing effect is not keyed on the streaming "
                               "flag (keys: %r) — `displayContent` reaches its final "
                               "value one throttle tick before the stream ends, so the "
                               "convergence frame never re-enters and nothing is ever "
                               "deposited" % (effect_keys or "").strip()))
    if not re.search(r"\b(displayContent|content)\b", effect_keys or ""):
        problems.append(("H3", "the publishing effect is not keyed on the content — "
                               "a reused body would keep the previous message's blocks"))

    floor = effect_body.find("{")
    for m in re.finditer(r"\.putBlocks\s*\(", effect_body):
        conds = _enclosing_condition_chain(effect_body, m.start(), floor)
        cond = " && ".join(conds)
        line = body_off + body.count("\n", 0, effect_body_off + m.start()) + 1
        if not conds:
            problems.append(("H4", "the deposit at line %d has no enclosing condition — "
                                   "it caches whatever is in hand, so an empty or "
                                   "stale block list can become the content's cached "
                                   "parse" % line))
            continue
        if "isStreaming" not in cond:
            problems.append(("H4", "the deposit at line %d is not gated on the stream "
                                   "having ended — caching a half-parsed prefix under "
                                   "the final content's key renders a truncated message "
                                   "from then on" % line))
            continue
        if not re.search(r"\b(displayContent|content)\b", cond):
            problems.append(("H4", "the deposit at line %d does not name the content it "
                                   "is being cached under" % line))
            continue
        # The deposited list: `putBlocks(<content>, <list>)`.
        op = effect_body.find("(", m.start())
        args = call_arg_spans(effect_body, op) or []
        if len(args) < 2:
            problems.append(("H4", "the deposit at line %d does not pass a block list "
                                   "— fail-closed" % line))
            continue
        deposited = args[1]
        if deposited in ("it", "computed", "parsed", "blocks"):
            continue  # a list parsed from the content in hand — safe by construction
        owner = re.match(r"\s*(\w+)", deposited)
        if owner is None or owner.group(1) not in cond:
            problems.append(("H4", "the deposit at line %d caches `%s`, a list this "
                                   "frame did not parse, but its guard never consults "
                                   "that list's source — a parse cancelled by a newer "
                                   "chunk leaves a prefix behind, and caching it is a "
                                   "durable truncated render" % (line, deposited.strip()[:40])))
    return problems


def fixtures(**overrides):
    files = {
        MODELS_REL: MODELS_F,
        TOKEN_REL: TOKEN_F,
        INPUT_REL: INPUT_F,
        GESTURE_REL: GESTURE_F,
        SELECTION_REL: SELECTION_F,
        STREAM_REL: STREAM_F,
        MDTOOLBAR_REL: MDTOOLBAR_F,
    }
    files.update(overrides)
    return files


def selftest():
    failures = []

    def expect(name, files, want_violation_ids):
        root = tempfile.mkdtemp(prefix="chat-ui-gate-selftest-")
        try:
            for rel, content in files.items():
                path = os.path.join(root, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            _, problems = check_tree(root)
            got = {cid for cid, _ in problems}
            if want_violation_ids is None:
                if problems:
                    failures.append("%s: expected CLEAN, got %s" % (name, sorted(got)))
            else:
                missing = [v for v in want_violation_ids
                           if not any(v in cid for cid in got)]
                if missing:
                    failures.append("%s: expected %s, got %s" %
                                    (name, want_violation_ids, sorted(got)))
        finally:
            shutil.rmtree(root)

    # clean fixture → exit 0
    expect("clean", fixtures(), None)
    # A1: a written-but-never-read boolean flips red (the B-01 class, generalized)
    expect("A1 unread boolean", fixtures(**{
        MODELS_REL: MODELS_F.replace(
            "    val isMcp: Boolean = false,\n",
            "    val isMcp: Boolean = false,\n    val isZombie: Boolean = false,\n"),
    }), ["A1"])
    # A2: dropping the isMcp branch flips red
    expect("A2 no mcp branch", fixtures(**{
        TOKEN_REL: TOKEN_F.replace(
            "    if (cmd.isMcp) {\n        resetSlashMenuState()\n"
            "        return fillComposerWithSlashTitle(cmd, saved)\n    }\n", ""),
    }), ["A2"])
    # B1: dropping the main-chat wiring flips red
    expect("B1 gesture call dropped", fixtures(**{
        GESTURE_REL: GESTURE_F.replace(
            "        val tableActions = controller.selectionTableActions()\n"
            "        if (tableActions != null) {\n"
            "            MinisToolbarButton(label = \"Copy Table\") { tableActions.copyTableMarkdown() }\n"
            "            MinisToolbarButton(label = \"Copy Table Image\") { tableActions.copyTableImage() }\n"
            "        }\n", ""),
    }), ["B1"])
    # B2: deleting the markdown-toolbar consumer flips red
    expect("B2 mdtoolbar consumer dropped", fixtures(**{
        MDTOOLBAR_REL: MDTOOLBAR_F.replace(
            "        val tableActions = toolbar.selectionController?.selectionTableActions()\n"
            "        if (tableActions != null) {\n"
            "            ToolbarButton(label = \"Copy Table\") { tableActions.copyTableMarkdown() }\n"
            "        }\n", ""),
    }), ["B2"])
    # B3: reverting the key to a bare messageId flips red
    expect("B3 key reverted", fixtures(**{
        SELECTION_REL: SELECTION_F.replace(
            "    fun rememberTableActions(messageId: String, tableIndex: Int, actions: TableActions) {",
            "    fun rememberTableActions(messageId: String, actions: TableActions) {").replace(
            "    fun forgetTableActions(messageId: String, tableIndex: Int) {",
            "    fun forgetTableActions(messageId: String) {"),
    }), ["B3"])
    # B5: publishing without the table index flips red
    expect("B5 bare-messageId publish", fixtures(**{
        STREAM_REL: STREAM_F.replace(
            "selectionController.rememberTableActions(messageId, tableIndex, actions)",
            "selectionController.rememberTableActions(messageId, actions)"),
    }), ["B5"])
    # C3: restoring the dead-branch shape flips red
    expect("C3 dead branch", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "                if (swipedUp && canSwipeSendNow()) {\n"
            "                    if (sendSwipeProgress >= swipeArmFraction) {",
            "                if (swipedUp && canSwipeSendNow()) {\n"
            "                    val canSendNow = canSwipeSendNow() || attachments.isNotEmpty()\n"
            "                    if (sendSwipeProgress >= swipeArmFraction) {"),
    }), ["C3"])
    # C3 (baseline shape): the exact defect form — guard on a bare boolean whose
    # body re-derives eligibility from that boolean
    expect("C3 baseline dead branch", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "    val canSwipeSendNow: () -> Boolean = {\n"
            "        inputFieldValue.text.isNotBlank() || attachments.isNotEmpty()\n"
            "    }\n", "")
            .replace(
            "                        if (canSwipeSendNow()) {",
            "                        val hasText = inputText.isNotBlank()\n"
            "                        if (hasText) {")
            .replace(
            "                if (swipedUp && canSwipeSendNow()) {\n"
            "                    if (sendSwipeProgress >= swipeArmFraction) {",
            "                val hasText = inputText.isNotBlank()\n"
            "                if (swipedUp && hasText) {\n"
            "                    val canSendNow = hasText || attachmentsCount > 0\n"
            "                    if (sendSwipeProgress >= swipeArmFraction) {")
            .replace(
            "                } else if (swipedUp && !canSwipeSendNow() && !inputFocused) {",
            "                } else if (swipedUp && !hasText && !inputFocused) {"),
    }), ["C1", "C3"])
    # C4: plain val capture of the threshold flips red
    expect("C4 frozen threshold", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "    val swipeThresholdPxState = rememberUpdatedState(\n"
            "        with(LocalDensity.current) { sendSwipeThresholdDp.dp.toPx() },\n"
            "    )\n",
            "    val swipeThresholdPxState = with(LocalDensity.current) { sendSwipeThresholdDp.dp.toPx() }\n"),
    }), ["C4"])
    # C5: dropping the enqueue route flips red
    expect("C5 enqueue dropped", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "                        performSendOrEnqueue()\n", ""),
    }), ["C5"])
    # C6: dropping the keyboard shortcut flips red
    expect("C6 keyboard shortcut dropped", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "                } else if (swipedUp && !canSwipeSendNow() && !inputFocused) {",
            "                } else if (swipedUp && !canSwipeSendNow()) {"),
    }), ["C6"])

    # ── D1-D3 (dv17-4 / B-05) ───────────────────────────────────────────────
    # D1: the dead thinkingLevel key is back
    expect("D1 dead thinkingLevel key", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "            viewModel.memoryEnabled.collectAsState().value,\n",
            "            viewModel.memoryEnabled.collectAsState().value,\n"
            "            viewModel.thinkingLevel.collectAsState().value,\n"),
    }), ["D1"])
    # D2: session scope dropped from the keys
    expect("D2 session scope dropped", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "            viewModel.currentSessionId,\n", ""),
    }), ["D2"])
    # D2: the two live repository lists dropped from the keys
    expect("D2 repository lists dropped", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "            viewModel.skillRepository?.skills?.collectAsState()?.value,\n"
            "            viewModel.mcpRepository?.servers?.collectAsState()?.value,\n", ""),
    }), ["D2"])
    # D3: the pre-fix shape — the memo computed unconditionally, with the menu
    # gate only on the NEXT line (the rendered-value-drop baseline)
    expect("D3 ungated memo (baseline shape)", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "    val filteredSlashCommands = if (showSlashMenu) {\n"
            "        remember(\n"
            "            viewModel.slashFilter.collectAsState().value,\n"
            "            viewModel.memoryEnabled.collectAsState().value,\n"
            "            viewModel.currentSessionId,\n"
            "            viewModel.skillRepository?.skills?.collectAsState()?.value,\n"
            "            viewModel.mcpRepository?.servers?.collectAsState()?.value,\n"
            "        ) { viewModel.filteredSlashCommands() }\n"
            "    } else {\n"
            "        emptyList()\n"
            "    }\n",
            "    val filteredSlashCommands = remember(\n"
            "        showSlashMenu,\n"
            "        viewModel.slashFilter.collectAsState().value,\n"
            "        viewModel.memoryEnabled.collectAsState().value,\n"
            "        viewModel.currentSessionId,\n"
            "        viewModel.skillRepository?.skills?.collectAsState()?.value,\n"
            "        viewModel.mcpRepository?.servers?.collectAsState()?.value,\n"
            "    ) { viewModel.filteredSlashCommands() }\n"),
    }), ["D3"])
    # precision: an early-exit guard is a legal shape (D3 must stay green)
    expect("D3 precision: early-exit guard accepted", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "    val filteredSlashCommands = if (showSlashMenu) {\n",
            "    if (!showSlashMenu) {\n"
            "        return@ChatInputArea\n"
            "    }\n"
            "    val filteredSlashCommands = if (true) {\n"),
    }), None)

    # ── E1-E3 (dv19-B1): composer decisions read the LIVE field value ────────
    def expect_exact(name, files, want_violation_ids):
        """Stricter than expect(): the violation-id SET must match exactly, so a
        fixture cannot pass by tripping an unrelated criterion (the "only this
        one turns red" proof each new criterion owes)."""
        root = tempfile.mkdtemp(prefix="chat-ui-gate-selftest-")
        try:
            for rel, content in files.items():
                path = os.path.join(root, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            _, problems = check_tree(root)
            got = {cid for cid, _ in problems}
            want = set(want_violation_ids)
            if got != want:
                failures.append("%s: expected exactly %s, got %s"
                                % (name, sorted(want), sorted(got)))
        finally:
            shutil.rmtree(root)

    # E1: a content flag that reads the debounced VM string flips red
    expect_exact("E1 debounced button flag", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "    val hasContent = canSwipeSendNow()\n",
            "    val hasContent = inputText.isNotBlank() || attachments.isNotEmpty()\n"),
    }), ["E1"])
    # E1 precision: reading the field's own value is an equally legal shape
    expect("E1 precision: direct live read", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "    val hasContent = canSwipeSendNow()\n",
            "    val hasContent = inputFieldValue.text.isNotBlank() || attachments.isNotEmpty()\n"),
    }), None)
    # E2: each decision site must pass the live value -- one fixture per site
    expect_exact("E2 debounced dismiss arg", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "dismissSlashMenu(inputFieldValue.text)", "dismissSlashMenu(inputText)"),
    }), ["E2-dismiss"])
    expect_exact("E2 debounced execute arg", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "executeSlashCommand(cmd, inputFieldValue.text)", "executeSlashCommand(cmd, inputText)"),
    }), ["E2-execute"])
    expect_exact("E2 debounced transfer arg", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "inputText = inputFieldValue.text,", "inputText = inputText,"),
    }), ["E2-transfer"])
    # E2 fail-closed: a site that moved away is reported, never silently skipped
    expect_exact("E2 missing transfer site", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "ChatViewModelStore.PendingTransfer(", "ChatViewModelStore.MovedTransfer("),
    }), ["E2-transfer"])
    # E3: dropping the cancel at one site flips that site red
    expect_exact("E3 cancel dropped at the transfer site", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "        MoveToSessionSheet(onSelect = { targetId ->\n"
            "            cancelPendingImeBurst()\n",
            "        MoveToSessionSheet(onSelect = { targetId ->\n"),
    }), ["E3"])
    # E3 precision: inline cancels (no shared helper) are an equally legal shape
    expect_exact("E3 precision: inline cancels", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "    val cancelPendingImeBurst: () -> Unit = {\n"
            "        imeBurstJob?.cancel()\n"
            "        imeBurstJob = null\n"
            "        imeBurstBuffer = null\n"
            "    }\n", "").replace(
            "cancelPendingImeBurst()", "imeBurstJob?.cancel()"),
    }), [])
    # ── F1-F4: composer clear is one funnel (dv20-B03) ──────────────────────
    expect("F1 funnel loses the field reset", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            '        inputFieldValue = TextFieldValue("")\n', ""),
    }), ["F1"])
    expect("F1 funnel loses the caret reset", fixtures(**{
        INPUT_REL: INPUT_F.replace('        lastTrueCaretEnd = 0\n', ""),
    }), ["F1"])
    expect("F1 funnel loses the burst drop", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            '        cancelPendingImeBurst()\n        viewModel.setInputText("")',
            '        viewModel.setInputText("")'),
    }), ["F1"])
    expect("F2 clear outside the funnel", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            '        viewModel.sendMessage(toSend)',
            '        viewModel.setInputText("")\n        viewModel.sendMessage(toSend)'),
    }), ["F2"])
    expect("F3 funnel called before its declaration", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "package com.rikkaminis.app.ui.chat\n",
            "package com.rikkaminis.app.ui.chat\n\n"
            "val earlyCaller: () -> Unit = { clearComposerInput() }\n"),
    }), ["F3"])
    expect("F4 pill routes around the funnel", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            '            viewModel.cancelEdit()\n            clearComposerInput()',
            '            viewModel.cancelEdit()'),
    }), ["F4"])
    expect("F precision: comments mention the shapes", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            "    val clearComposerInput: () -> Unit = {",
            "    // NEVER: setInputText(\"\") alone -- the VM write is suppressed\n"
            "    // when the string is already empty, so the field keeps the sent text\n"
            "    val clearComposerInput: () -> Unit = {"),
    }), [])
    expect("F4 send path skips the funnel", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            '        clearComposerInput()\n        viewModel.sendMessage(toSend)',
            '        viewModel.sendMessage(toSend)'),
    }), ["F4"])
    expect("F4 slash branch skips the funnel", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            '            clearComposerInput()\n            return@handler',
            '            return@handler'),
    }), ["F4"])
    expect("F4 move-to-session skips the funnel", fixtures(**{
        INPUT_REL: INPUT_F.replace(
            '            clearComposerInput()\n        })',
            '        })'),
    }), ["F4"])

    # ── H family (B-05): the streaming body's ledger invariants ─────────────
    # H1: the seed forgets the streaming flag → the convergence frame keeps the
    #     `null` cached during the live frames.
    expect("H1 seed not keyed on isStreaming", fixtures(**{
        STREAM_REL: STREAM_F.replace(
            "    val warmSeed = remember(displayContent, isStreaming) {",
            "    val warmSeed = remember(displayContent) {"),
    }), ["H1"])
    # H2: the block state collapses back to a bare list.
    expect("H2 bare List<MdBlock> state", fixtures(**{
        STREAM_REL: STREAM_F.replace(
            "    var published by remember {\n"
            "        mutableStateOf(StreamingBlockLedger.seed(warmSeed, displayContent))\n"
            "    }\n"
            "    val blocks = StreamingBlockLedger.rendered(published, warmSeed)\n",
            "    var blocks by remember { mutableStateOf<List<MdBlock>>(warmSeed.orEmpty()) }\n"),
    }), ["H2"])
    # H3: the publishing effect drops the streaming flag from its keys → the
    #     convergence frame never re-enters.
    expect("H3 effect not keyed on isStreaming", fixtures(**{
        STREAM_REL: STREAM_F.replace(
            "    LaunchedEffect(displayContent, isStreaming) {",
            "    LaunchedEffect(displayContent) {"),
    }), ["H3"])
    # H4a: the convergence deposit loses its ledger guard.
    expect("H4 deposit guard never names the stream state", fixtures(**{
        STREAM_REL: STREAM_F.replace(
            "        if (StreamingBlockLedger.isConvergedWith(published, displayContent, isStreaming)) {",
            "        if (published.blocks.isNotEmpty()) {"),
    }), ["H4"])
    # H4b: the deposit caches a list this frame did not parse, without asking
    #      where that list came from.
    expect("H4 deposit of a foreign list", fixtures(**{
        STREAM_REL: STREAM_F.replace(
            "                MarkdownParseCaches.putBlocks(displayContent, published.blocks)",
            "                MarkdownParseCaches.putBlocks(displayContent, stalePrefix)"),
    }), ["H4"])
    # H precision: prose mentions the forbidden shapes.
    expect("H precision: comments mention the old shapes", fixtures(**{
        STREAM_REL: STREAM_F.replace(
            "internal object StreamingBlockLedger {",
            "// NEVER: var blocks by remember { mutableStateOf<List<MdBlock>>(emptyList()) }\n"
            "// NEVER: putBlocks(displayContent, published.blocks) outside isStreaming\n"
            "internal object StreamingBlockLedger {"),
    }), [])

    if failures:
        print("❌ selftest failed:")
        for f in failures:
            print("   " + f)
        return 1
    print("CHAT_UI_INTERACTION_SELFTEST=PASS (42 fixtures: clean + 41 violation/precision)")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    root = os.path.abspath(args[0]) if args else os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".."))
    if "--selftest" in flags:
        return selftest()
    unknown = [f for f in flags if f != "--selftest"]
    if unknown:
        print("unknown option(s): %s" % " ".join(unknown))
        return 2
    path, problems = check_tree(root)
    if path is None or problems:
        print("chat_ui_interaction_check: %d violation(s) in %s" % (len(problems), path))
        for cid, msg in problems:
            print("  [%s] %s" % (cid, msg))
        return 1
    print("chat_ui_interaction_check: slash wiring + table toolbar + swipe parity — "
          "CHAT-UI-INTERACTION GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

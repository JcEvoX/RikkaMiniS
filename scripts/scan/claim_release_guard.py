#!/usr/bin/env python3
"""Claim/release ownership guard — RikkaMinis CI scan gate.

## Why this gate exists

2026-10-12, for real (session b4c466fd): the user switched model while a slow
turn was still in flight. The predecessor's `finally` landed INSIDE the new
claim's window — after `_isStreaming = true` but before the new claim's marker
was published — read the OLD marker, passed both halves of the stale-job gate
and flipped `_isStreaming` off. The new turn kept streaming with no Stop
button. Fixed in 41f024e7 by minting an identity token BEFORE the flip
(`streamingClaimToken = Any()`), so a stale release can never pass
`claimToken === streamingClaimToken`.

That was the third generation of the same family:
  - 2026-09-06 17:31 (see ChatTurnPersistence KDoc): old finally cleared the
    new turn's flag — before any ownership gate existed;
  - retryFromMessage / retryLast got the guard next; switchModelAndRerun did
    not (the "fixed one side, missed the other" step);
  - 2026-10-12 14:40: the gate existed everywhere, but the marker it compares
    was published a few statements AFTER `_isStreaming = true` — the window
    the gate was supposed to cover was open from the flip;
  - 2026-10-23 (deep-audit-1044 A-02): TerminalSession.start()'s preemption
    bail-outs settled the `_state` / `termuxSession` slot of the attempt that
    superseded them — and the gate certified them, because it counted a counter
    INEQUALITY as an ownership gate. See C3 below (and the direction rule).

The class is "stale release of a shared run-state flag": a flag with multiple
claimants (send / retry / resume / switch-model) whose teardown paths must
prove ownership before releasing it. Timing cannot be seen statically, but the
two mechanical shapes every instance has exhibited CAN be:
  A. SIBLING-GATE INCONSISTENCY — within one flag's release-site family, some
     releases are guarded by an ownership comparison (epoch / token / job
     identity) and others are not. Ungated siblings are either a miss or a
     deliberate choice; both deserve a `release-ok:` marker. This is the
     "fixed one side, missed the other" detector.

  B. MARKER PUBLISHED AFTER THE FLIP — the claim publishes the very marker its
     family's release gates compare (an epoch stamp or a fresh identity token)
     only AFTER flipping the flag to true. A predecessor's teardown landing in
     that window reads the stale marker, passes the gate, and clears the live
     claim. Every claim must publish at least one gate-compared marker BEFORE
     the flip. (41f024e7's root cause; the pre-fix tree violates this at all 7
     claim sites, the post-fix tree at none — used as ground truth.)

  C. [1042-A-03, 2026-10-22] SELF-BOUND GATES & UNGUARDED SETTLEMENT — two
     blind spots the pre-1042 gate had, both found by deep-audit-1042 A-03 in
     sandbox/TerminalSession.kt:

     C2. SELF-BOUND OWNERSHIP GUARD — `X?.let { s -> … if (X === s) … }`.
         The let-parameter IS the receiver, so the identity check is a
         tautology: it always passes and the "guarded" release behind it is
         effectively ungated. The pre-fix start() failure handler self-bound
         exactly this way (`termuxSession?.let { s -> if (termuxSession === s)
         termuxSession = null }`) and unconditionally killed a newer attempt's
         PTY, unregistered its liveSessions entry and flipped STOPPED over it.
         Such a condition now never counts as a gate (Check A sees the write
         as ungated) and is reported on its own with an explicit message.

     C1. TERMINAL SETTLEMENT OWNERSHIP — inside every `fun onSessionFinished`
         body, the settlement decision points (the *Generation bump, the
         termuxSession slot clear, the _state.value publish, the
         liveSessions removal) must sit inside the brace block of an
         ownership guard (`if (termuxSession === changedSession) { … }`).
         The pre-fix callback bumped the generation, published STOPPED and
         unregistered liveSessions OUTSIDE the guard, so a stale callback
         landing after a newer start() aborted the fresh boot, rendered the
         live session dead on the screen and cut it off from broadcasts.
         The criterion anchors the DECISIONS, not their spellings (backlog
         §1036-A1 R3: criteria bound to literal call shapes rotted 4×); if
         the decisions themselves disappear the gate ROTs loudly instead of
         disarming silently.

     C3. [1044-A02, 2026-10-23] START PREEMPTION SETTLEMENT — inside every
         `fun start` body, a preemption bail-out (a branch whose condition
         proves "I am NOT the current owner" while containing a `return`)
         must not settle shared state it no longer owns:

           · it must not publish `_state.value` at all — the generation bump
             that invalidated the attempt was made by the party that now owns
             the state (`stop()` publishes STOPPED, a newer `start()` publishes
             BOOTING), so publishing here overwrites a newer attempt's state and
             pins the terminal to STOPPED while its PTY boots;
           · a `termuxSession = …` clear / `liveSessions.remove…` must sit
             behind an identity guard against the value captured at creation
             (`if (termuxSession === session) { … }`). Unguarded, a superseded
             attempt clears the PTY a newer `start()` just installed — an
             orphaned bash tree the UI can neither write to nor resize — and
             strands its liveSessions entry.

         Same doctrine as C1: anchors the decisions, and ROTs when a file
         declares a generation counter next to the terminal slot but no
         `fun start` (so a rename cannot disarm it silently).

## What counts as an ownership gate

The nearest enclosing `if` of a release write, if its condition, READ IN THE
POLARITY IT IS USED — direction matters ([1044-A02]; the pre-1044 gate ignored
it, which is how the defect below stayed invisible):

  · taken branch  (`if (c) { …write… }`)      → ownership when `c` is
    `==` / `===` between two non-literal operands;
  · skipped branch (`if (c) return` above the write) → ownership when `c` is
    `!=` / `!==` between two non-literal operands.

A counter inequality in a taken branch (`if (myGeneration !=
startGeneration.get()) { … }`) therefore means "I am stale" and is NOT a gate,
while `if (termuxSession === session) { … }` is. Plain business checks
(`result != null`, `code == 200`) never count — they release on a condition,
not on ownership.

## Family scoping (false-family suppression)

"Same key" alone over-merges: `process` in LogcatTailer vs PersistentShell are
two different fields, not one family. So a family is scoped by the key's
declaration site: if the key is declared in exactly one file, all writes of
that key across files form one family (ChatViewModel's `_isStreaming` is
written from 5 files and IS one family); if declared in several files, each
file's writes form their own family. Log strings and comments are stripped
before matching, so `"… _isStreaming=false SKIPPED"` log text never counts as
a write.

Escape hatches: `release-ok: <reason>` (Check A) and `claim-ok: <reason>`
(Check B), on the same or the previous line.

Modes: default = strict (exit 1 on findings; wired into scan.sh as gate 21).
`--report` prints the same findings but exits 0 — kept for triage when the
baseline changes. Historical ground truth: the pre-fix tree (41f024e7^) trips
Check B at all 7 claim sites; the current tree passes strict clean, and the
self-test asserts both.

Usage: python3 scripts/scan/claim_release_guard.py <repo_root> [--report]
       python3 scripts/scan/claim_release_guard.py --selftest
Exit:  0 = clean (or report mode), 1 = finding(s) in strict mode
"""
import os
import re
import sys

SRC_ROOTS = ["src/android/app/src/main/java"]

BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
LINE_COMMENT_RE = re.compile(r"//.*$")
RELEASE_ESCAPE_RE = re.compile(r"release-ok\s*:")
CLAIM_ESCAPE_RE = re.compile(r"claim-ok\s*:")

# Assignment write: receiver chain, optional `.value`, single `=` (not
# `==`/`!=`/`<=`/`>=`), value true|false|null.
WRITE_RE = re.compile(
    r"([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\s*(?:\.\s*value)?\s*"
    r"(?<![=!<>])=(?!=)\s*(true|false|null)\b"
)

# Mint-shaped plain assignment: `X = Any()`, or `X = otherIdent`. Declarations
# (preceded by val/var) are excluded at match time. Name must look like a
# generation stamp (epoch/token/claim/…).
MINT_SHAPE_RE = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*=\s*(Any\s*\(\s*\)|[A-Za-z_]\w*)\s*$"
)
MARKER_NAME_RE = re.compile(r"(?i)(epoch|token|claim|serial|owner)")

DECL_RE = re.compile(r"\b(?:val|var)\s+([A-Za-z_]\w*)\b")

# Ownership vocabulary. Substring form for distinctive stems (epochAtCancel,
# streamEpoch, claimToken...); word-bounded form for short generic stems.
OWNERSHIP_LONG = re.compile(r"(?i)(epoch|token|claim|generation|serial|owner|job)")
OWNERSHIP_SHORT = re.compile(r"(?i)\b(gen|seq|active|isactive)\b")
LITERAL_RE = re.compile(r"^(null|true|false|-?\d[\w.]*|\"[^\"]*\"|'[^']*')$")
CMP_OP_RE = re.compile(r"!==|===|!=|==")
SINGLE_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")

GATE_WINDOW = 8
GUARD_WINDOW_CHARS = 220
MINT_CLAIM_SEARCH = 60
FUNC_SEARCH_BACK = 60
FWD_REPORT_WINDOW = 60

# [1042-A-03] Self-bound fake gate: `X?.let { s -> … }` / `X?.let { … }`.
# Receiver must be a plain identifier chain (a call result has no name to
# self-compare against). The explicit-param form captures `s`, the implicit
# form means `it`.
SELF_LET_RE = re.compile(
    r"([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\?\.\s*let\s*\{"
    r"(?:\s*([A-Za-z_]\w*)\s*->)?"
)
SELF_LET_SCAN_LINES = 15

# [1042-A-03] C1 anchors: the settlement decisions of the terminal family.
ON_SESSION_FINISHED_RE = re.compile(r"\bfun\s+onSessionFinished\s*\(")
SETTLE_DECISIONS = (
    ("*Generation counter bump",
     re.compile(r"\b[A-Za-z_]\w*[Gg]eneration\s*(?:\.\s*(?:increment|add|"
                r"update|set)\w*\s*\(|=(?!=))")),
    ("termuxSession slot clear",
     re.compile(r"\btermuxSession\s*=(?!=)")),
    ("_state.value publish",
     re.compile(r"\b_state\s*\.\s*value\s*=(?!=)")),
    ("liveSessions removal",
     re.compile(r"\bliveSessions\s*\.\s*(?:remove\w*|clear)\b")),
)
TERM_SETTLE_KEYS = ("termuxSession", "startGeneration", "_state",
                    "liveSessions")

# [1044-A02] C3 anchors: `fun start` preemption bail-outs. Named aliases of the
# same decision shapes C1 uses, so a renamed decision ROTs in both criteria.
START_FUN_RE = re.compile(r"\bfun\s+start\s*\(")
RETURN_RE = re.compile(r"\breturn\b")
STATE_PUBLISH_RE = re.compile(r"\b_state\s*\.\s*value\s*=(?!=)")
SLOT_CLEAR_RE = re.compile(r"\btermuxSession\s*=(?!=)")
LIVE_REMOVE_RE = re.compile(r"\bliveSessions\s*\.\s*(?:remove\w*|clear)\b")

# Audited (file, key) pairs where the ungated release is demonstrably safe in
# the current architecture. Every entry states WHY — re-audit when the file's
# concurrency model changes. New ungated writes of a listed key are NOT
# separately flagged: re-audit this file when touching these paths. (B is
# alias-free: no allow-list applies to Check B.)
ALLOWED = {
    ("com/rikkaminis/app/browser/BrowserUseManager.kt", "_isLoading"):
        "UI-only spinner flag; every navigation runs serialized through the "
        "tab's per-tab Mutex (BrowserTabPool lockForTab), and the ungated "
        "clears are callback-path clears of the CURRENT slot.",
    ("com/rikkaminis/app/browser/BrowserUseManager.kt", "navigationDeferred"):
        "Single-slot deferred; callbacks (onPageFinished/onReceivedError/"
        "onRendererGone) clear the slot they own; the re-entrant paths "
        "(timeout runnable, reloadAndWait) already carry identity guards.",
    ("com/rikkaminis/app/sandbox/PersistentShell.kt", "stdinWriter"):
        "stop() is owner-initiated teardown (generation++ issues first); the "
        "reader-loop teardown already carries the identity guard.",
    ("com/rikkaminis/app/webapp/WebAppActivity.kt", "webViewRef"):
        "Activity-scoped single instance; clears happen on the main thread "
        "during init-rebuild and onDestroy only.",
}


def strip_line(line):
    """String literals first (URLs contain `//`), then line comments."""
    s = STRING_RE.sub('""', line)
    return LINE_COMMENT_RE.sub("", s)


def load(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    raw_lines = raw.split("\n")
    no_block = BLOCK_COMMENT_RE.sub(
        lambda m: "\n" * m.group(0).count("\n"), raw
    )
    clean_lines = [strip_line(l) for l in no_block.split("\n")]
    return raw_lines, clean_lines


def escaped(raw_lines, line_no, rx):
    for j in (line_no - 2, line_no - 1):
        if 0 <= j < len(raw_lines) and rx.search(raw_lines[j]):
            return True
    return False


def extract_condition(text, open_paren):
    depth = 0
    for j in range(open_paren, len(text)):
        ch = text[j]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:j]
    return None


def compare_operands(cond):
    """Yield (op, left, right) for each comparison in a condition, left to right.

    [1044-A02] Scanned with ONE alternation instead of one `finditer` per
    operator: `"!=="` contains `"!="`, and `"!="` contains `"="`, so the
    per-operator loop also reported a phantom `==` inside `!==` (left operand
    `myToken !`) and turned a staleness test into an ownership gate.
    """
    c = " ".join(cond.split())
    for m in CMP_OP_RE.finditer(c):
        left = re.split(r"[&|();{]", c[:m.start()])[-1].strip()
        right = re.split(r"[&|();{]", c[m.end():])[0].strip()
        if left and right:
            yield m.group(0), left, right


def is_ownership_cond(cond, taken=True):
    """Does the branch this condition selects mean "I am the current owner"?

    [1044-A02] DIRECTION MATTERS, and the pre-1044 gate ignored it. A release
    write is safe when the branch PROVES ownership; it is the defect when the
    branch proves the opposite and settles anyway. Both polarities of both
    operator families exist in the tree:

        if (termuxSession === session) { …settle… }    # taken  → I own it
        if (genAtStart != generation) { …settle… }     # taken  → I am STALE
        if (claimToken !== streamingClaimToken) return # not-taken → I own it

    So ownership = `==`/`===` when the branch IS taken, and `!=`/`!==` when the
    branch is SKIPPED (the early-return guard form). Treating every comparison
    as a gate — what the pre-1044 gate did — is exactly how 1044-A02 hid: the
    preemption branch in `TerminalSession.start()` read
    `myGeneration != startGeneration.get()`, matched the `generation` stem of
    the ownership vocabulary, and the gate therefore certified the two
    unguarded settlements behind it (unconditional slot clear + STOPPED publish
    over a newer attempt) as "gated".

    taken=True  → the caller is the `if` body (or a span enclosed by it).
    taken=False → the caller is the early-return form `if (cond) return`, i.e.
                  execution continues past the guard only when cond is false.
    """
    if not cond:
        return False
    for op, left, right in compare_operands(cond):
        if LITERAL_RE.match(left) or LITERAL_RE.match(right):
            continue
        if not re.search(r"[A-Za-z_]", left) or not re.search(r"[A-Za-z_]", right):
            continue
        same = op in ("==", "===")
        if same != taken:
            continue  # this comparison, read in this branch, says "not mine"
        if op in ("===", "!=="):
            return True  # identity compare of two non-literals = TOCTOU guard
        if (OWNERSHIP_LONG.search(left) or OWNERSHIP_LONG.search(right)
                or OWNERSHIP_SHORT.search(left) or OWNERSHIP_SHORT.search(right)):
            return True
    return False


def is_staleness_cond(cond):
    """[1044-A02] TRUE when `cond` proves "I am NOT the current owner".

    Exactly the inverse of an ownership gate, and the shape the pre-1044 gate
    certified as a gate: `if (myGeneration != startGeneration.get()) { …settle… }`.
    Settling shared state inside such a branch means a superseded attempt
    overwrites the state/slot of the attempt that superseded it.
    """
    return (bool(cond)
            and not is_ownership_cond(cond, taken=True)
            and is_ownership_cond(cond, taken=False))


def match_brace(text, open_idx):
    """Index of the `}` matching the `{` at [open_idx], or None (quote-aware)."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"' or ch == "'":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                if text[j] == "\\":
                    j += 1
                j += 1
            i = j + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def norm_cond(cond):
    return " ".join(cond.split())


def scan_fake_gates(clean_lines):
    """[1042-A-03 C2] Self-bound ownership guards in this file.

    `X?.let { s -> … if (X === s) … }`: the let-parameter binds to the
    receiver itself, so the identity comparison is a tautology — it always
    passes and the "guarded" write behind it is effectively ungated. Returns
    ({(line, norm_cond): (recv, param, op)}, [(line, recv, param, op, cond)]).
    """
    fakes = {}
    reports = []
    n = len(clean_lines)
    for i, line in enumerate(clean_lines):
        for m in SELF_LET_RE.finditer(line):
            recv = norm_cond(m.group(1))
            param = m.group(2) or "it"
            brace_off = m.start() + m.group(0).rfind("{")
            # Walk the lambda body (brace depth from the `let {`) to bound the scan.
            depth = 0
            j = i
            while j < n and j < i + SELF_LET_SCAN_LINES:
                text = clean_lines[j]
                seg_from = brace_off if j == i else 0
                closed = False
                for k in range(seg_from, len(text)):
                    if text[k] == "{":
                        depth += 1
                    elif text[k] == "}":
                        depth -= 1
                        if depth == 0:
                            closed = True
                            break
                if closed:
                    break
                j += 1
            for jj in range(i, min(j + 1, n)):
                for fm in re.finditer(r"\bif\s*\(", clean_lines[jj]):
                    cond = extract_condition(clean_lines[jj], fm.end() - 1)
                    if cond is None:
                        continue
                    for op, left, right in compare_operands(cond):
                        if op not in ("===", "!=="):
                            continue
                        if (norm_cond(left) == recv and norm_cond(right) == param) or \
                           (norm_cond(right) == recv and norm_cond(left) == param):
                            key = (jj, norm_cond(cond))
                            if key not in fakes:
                                fakes[key] = (recv, param, op)
                                reports.append((jj, recv, param, op, cond))
    return fakes, reports


def terminal_settlement_findings(rel, clean_lines, fake_gates):
    """[1042-A-03 C1] onSessionFinished settlement must be ownership-gated.

    Every settlement decision in the callback body — *Generation bump,
    termuxSession slot clear, _state.value publish, liveSessions removal —
    must sit inside the brace block (or the single brace-less statement) of
    an ownership guard. Anchors the DECISIONS, not their spellings; a body
    with no decisions at all ROTs loudly (audit-0917 requires the natural-exit
    path to mirror stop()'s bookkeeping).
    """
    joined = "\n".join(clean_lines)
    out = []
    for m in ON_SESSION_FINISHED_RE.finditer(joined):
        open_brace = joined.find("{", m.end())
        if open_brace < 0:
            continue
        close = match_brace(joined, open_brace)
        if close is None:
            close = len(joined) - 1
        body = joined[open_brace:close + 1]
        base_line = joined[:open_brace].count("\n")  # 0-based line of `{`
        decisions = []
        for label, rx in SETTLE_DECISIONS:
            for dm in rx.finditer(body):
                line = base_line + body[:dm.start()].count("\n") + 1
                decisions.append((dm.start(), label, line))
        if not decisions:
            mentions = [k for k in TERM_SETTLE_KEYS if k in body]
            if not mentions:
                out.append(("C1-ROT", rel, m.start() and
                            (joined[:m.start()].count("\n") + 1),
                            "onSessionFinished found but no settlement decisions "
                            "and no terminal-family keys — audit-0917's "
                            "bookkeeping is gone or the keys were renamed. "
                            "ROT — update this gate."))
            else:
                out.append(("C1-ROT", rel,
                            joined[:m.start()].count("\n") + 1,
                            "onSessionFinished found, family keys present "
                            f"({', '.join(mentions)}) but no decision points "
                            "matched — the gate's decision anchors drifted. "
                            "ROT — update this gate."))
            continue
        spans = ownership_spans(body, fake_gates, base_line)
        for d, label, line in decisions:
            if any(s < d <= e for s, e in spans):
                continue
            out.append(("C1", rel, line,
                        f"onSessionFinished: {label} sits OUTSIDE the "
                        "ownership guard — a stale callback landing after a "
                        "newer start() would settle a live session's state. "
                        "Wrap the whole settlement in "
                        "`if (termuxSession === changedSession) { … }`."))
    return out


def ownership_spans(text, fake_gates=(), line_base=0):
    """Offsets of the blocks/spans whose `if` PROVES ownership when taken.

    Shared by C1 (onSessionFinished settlement) and C3 (start preemption) so
    both anchor the same decision shape. `line_base` is the 0-based file line
    of `text`'s first line, needed because `fake_gates` is keyed by file line.
    """
    spans = []
    for fm in re.finditer(r"\bif\s*\(", text):
        p = fm.end() - 1
        cond = extract_condition(text, p)
        if cond is None:
            continue
        line = line_base + text[:fm.start()].count("\n")
        if (line, norm_cond(cond)) in fake_gates:
            continue  # self-bound tautology — not a gate (C2 reports it)
        if not is_ownership_cond(cond, taken=True):
            continue
        after = p + len(cond) + 2
        k = after
        while k < len(text) and text[k] in " \t":
            k += 1
        if k < len(text) and text[k] == "{":
            c = match_brace(text, k)
            if c is not None:
                spans.append((k, c))
        else:
            nl = text.find("\n", after)
            spans.append((after, nl if nl >= 0 else len(text)))
    return spans


def start_preemption_findings(rel, clean_lines, fake_gates):
    """[1044-A02] C3: `fun start` preemption bail-outs must not settle state
    they no longer own.

    Shape under test (TerminalSession.start): the boot coroutine compares the
    generation it captured against the live one and, having lost the race,
    reaps its PTY and returns. Two settlements behind such a bail-out are
    wrong, and both were unguarded in the pre-1044 tree:

      · `_state.value = …` — the bump that invalidated this attempt was made by
        the party that now owns the state (stop() publishes STOPPED, a newer
        start() publishes BOOTING), so publishing here overwrites a newer
        attempt's state: the terminal is pinned to STOPPED while a fresh PTY
        boots.
      · `termuxSession = …` / `liveSessions.remove…` — the slot is SHARED, so
        clearing it unconditionally drops whatever a newer attempt has already
        installed (orphaning a live PTY the UI can neither write to nor resize)
        and strands its registry entry. The clear must sit behind an identity
        guard against the value captured at creation
        (`if (termuxSession === session) { … }`).

    Anchors decisions, not spellings: a bail-out is recognised by the POLARITY
    of its condition (proves "not mine" when taken) plus a `return`, never by
    `generation`-named text — so the pre-1044 `myGeneration !=
    startGeneration.get()` and a token-compared spelling are both covered. A
    file that declares a generation counter next to the terminal slot but has
    no `fun start` ROTs instead of passing silently.
    """
    joined = "\n".join(clean_lines)
    out = []
    starts = list(START_FUN_RE.finditer(joined))
    if not starts:
        if (re.search(r"\b(?:val|var)\s+[A-Za-z_]*[Gg]eneration[A-Za-z_]*",
                      joined) and "termuxSession" in joined):
            out.append(("C3-ROT", rel, 1,
                        "terminal-family generation counter declared but no "
                        "`fun start(` found — the preemption checkpoints were "
                        "renamed or moved. ROT — update this gate."))
        return out
    for sm in starts:
        open_brace = joined.find("{", sm.end())
        if open_brace < 0:
            continue
        close = match_brace(joined, open_brace)
        if close is None:
            close = len(joined) - 1
        body = joined[open_brace:close + 1]
        base_line = joined[:open_brace].count("\n")
        for fm in re.finditer(r"\bif\s*\(", body):
            p = fm.end() - 1
            cond = extract_condition(body, p)
            if not is_staleness_cond(cond):
                continue
            after = p + len(cond) + 2
            k = after
            while k < len(body) and body[k] in " \t":
                k += 1
            if k < len(body) and body[k] == "{":
                end = match_brace(body, k)
                if end is None:
                    continue
                block_off, block = k, body[k:end + 1]
            else:
                nl = body.find("\n", after)
                end = nl if nl >= 0 else len(body)
                block_off, block = after, body[after:end]
            if not RETURN_RE.search(block):
                continue  # an inner staleness test, not a bail-out
            block_line_base = base_line + body[:block_off].count("\n")

            def line_at(off, _b=block, _o=block_off, _l=block_line_base):
                return _l + _b[:off].count("\n") + 1

            for dm in STATE_PUBLISH_RE.finditer(block):
                out.append(("C3", rel, line_at(dm.start()),
                            "start(): `_state.value` publish inside a "
                            "preemption bail-out — the bump that invalidated "
                            "this attempt was made by the party that now owns "
                            "the state (stop() → STOPPED, a newer start() → "
                            "BOOTING), so this write overwrites a newer "
                            "attempt's state. Write nothing here."))
            guards = ownership_spans(block, fake_gates, block_line_base)
            for label, rx in (("termuxSession slot clear", SLOT_CLEAR_RE),
                              ("liveSessions removal", LIVE_REMOVE_RE)):
                for dm in rx.finditer(block):
                    if any(s < dm.start() <= e for s, e in guards):
                        continue
                    out.append(("C3", rel, line_at(dm.start()),
                                f"start(): {label} inside a preemption "
                                "bail-out without an ownership guard — a "
                                "superseded attempt would clear the newer "
                                "attempt's PTY/registry entry. Guard it with "
                                "the value captured at creation: "
                                "`if (termuxSession === session) { … }`."))
    return out


def enclosing_gate(clean_lines, idx, col, fake_gates=()):
    """First ownership condition enclosing the write, or None.

    fake_gates: {(line_idx, norm_cond)} of self-bound tautologies — such a
    condition never counts as a gate ([1042-A-03 C2]).
    """
    lo = max(0, idx - GATE_WINDOW)
    window = "\n".join(clean_lines[lo:idx + 1])
    my_pos = sum(len(clean_lines[j]) + 1 for j in range(lo, idx)) + col
    for m in re.finditer(r"\bif\s*\(", window):
        p = m.end() - 1
        if p >= my_pos:
            continue
        cond = extract_condition(window, p)
        if cond is None:
            continue
        if (lo + window[:m.start()].count("\n"), norm_cond(cond)) in fake_gates:
            continue  # self-bound tautology — see scan_fake_gates
        after = p + len(cond) + 2
        k = after
        while k < len(window) and window[k] in " \t":
            k += 1
        enclosed = False
        if k < len(window) and window[k] == "{":
            depth = 0
            enclosed = True
            for t in range(k, my_pos):
                if window[t] == "{":
                    depth += 1
                elif window[t] == "}":
                    depth -= 1
                    if depth == 0:
                        enclosed = False
                        break
        else:  # brace-less if: enclosed until a closing brace or long gap
            seg = window[after:my_pos]
            if "}" not in seg and len(seg) < 120:
                enclosed = True
        if enclosed and is_ownership_cond(cond):
            return cond
    # early-return guard style: `if (ownership) return` just above the write
    tail_start = max(0, my_pos - GUARD_WINDOW_CHARS)
    tail = window[tail_start:my_pos]
    for m in re.finditer(r"\bif\s*\(", tail):
        cond = extract_condition(tail, m.end() - 1)
        if cond is None:
            continue
        if (lo + window[:tail_start + m.start()].count("\n"),
                norm_cond(cond)) in fake_gates:
            continue
        after = tail[m.end() + len(cond) + 1:]
        if re.match(r"\s*(return|continue)\b", after) and is_ownership_cond(
                cond, taken=False):
            return cond
    return None


def gate_marker_operands(cond):
    """Single-identifier operands in a gate condition (marker candidates)."""
    names = set()
    for _op, left, right in compare_operands(cond):
        for side in (left, right):
            if SINGLE_IDENT_RE.match(side):
                names.add(side)
    return names


def handoff_gate(clean_lines, idx, col, key):
    """Atomic hand-off shapes that make an ungated write safe.

      - take-or-bail:      `val X = slot ?: return/continue/break` in the
                           window — the writer holds the only reference.
      - same-slot snapshot:`val X = <key>` — takes the slot's value right
                           before clearing it (single-threaded hand-off).
      - conservative null:  enclosing `if (V == null)` whose body also writes
                           `V = null` — "clear only if nobody took the slot".

    These are the patterns a claimant uses when IT owns the slot; they are not
    the stale-teardown shape (a teardown reading a gate marker that may have
    moved on).
    """
    for j in range(max(0, idx - 4), idx):
        line = clean_lines[j]
        if re.search(r"\bval\s+\w+\s*=", line) and re.search(
            r"\?:\s*(return|continue|break)\b", line
        ):
            return "take-or-bail hand-off"
        if re.search(r"\bval\s+\w+\s*=\s*" + re.escape(key) + r"\s*$", line):
            return "same-slot snapshot"
    for j in range(max(0, idx - 5), idx + 1):
        m = re.search(r"\bif\s*\(\s*([A-Za-z_]\w*)\s*==\s*null\s*\)", clean_lines[j])
        if not m:
            continue
        var = m.group(1)
        body = "\n".join(clean_lines[j:min(len(clean_lines), idx + 5)])
        if re.search(r"(?<![\w.])" + re.escape(var) + r"\s*=\s*null", body):
            return f"conservative `{var} == null` gate"
    return None


def function_start(clean_lines, idx):
    for j in range(idx - 1, max(-1, idx - FUNC_SEARCH_BACK), -1):
        if re.search(r"\bfun\s+[A-Za-z_]\w*\s*[(<]", clean_lines[j]):
            return j
    return max(0, idx - FUNC_SEARCH_BACK)


def collect(root):
    """Returns (claims, releases, mints, decls, clean, rawD).

    mints: plain `X = <fresh stamp>` assignments where X is a marker-named var.
    """
    claims, releases, mints = [], [], []
    decls = {}
    clean, rawD = {}, {}
    for sr in SRC_ROOTS:
        base_dir = os.path.join(root, sr)
        for base, _dirs, files in os.walk(base_dir):
            for fn in sorted(files):
                if not fn.endswith(".kt"):
                    continue
                path = os.path.join(base, fn)
                rel = os.path.relpath(path, base_dir).replace(os.sep, "/")
                raw_lines, clean_lines = load(path)
                clean[rel], rawD[rel] = clean_lines, raw_lines
                for i, line in enumerate(clean_lines):
                    for m in DECL_RE.finditer(line):
                        decls.setdefault(m.group(1), set()).add(rel)
                    mm = MINT_SHAPE_RE.match(line)
                    if mm and MARKER_NAME_RE.search(mm.group(1)):
                        prefix = line[:mm.start(1)]
                        if not re.search(r"\b(val|var)\b", prefix):
                            mints.append(dict(file=rel, idx=i, line=i + 1,
                                              var=mm.group(1)))
                    for m in WRITE_RE.finditer(line):
                        prefix = line[:m.start()]
                        if re.search(r"\b(val|var)\b", prefix):
                            continue  # declaration, not an assignment
                        if prefix.count("(") > prefix.count(")"):
                            continue  # parameter default inside a signature
                        target = re.sub(r"\.\s*value$", "",
                                        m.group(1).strip())
                        if not target:
                            continue
                        rec = dict(file=rel, idx=i, line=i + 1, col=m.start(),
                                   target=target, key=target.split(".")[-1],
                                   value=m.group(2))
                        (claims if m.group(2) == "true" else releases).append(rec)
    return claims, releases, mints, decls, clean, rawD


def family_scope(site, decls):
    """Family anchor for a write: declaration file if unique, else own file."""
    files = decls.get(site["key"])
    if files and len(files) == 1:
        return next(iter(files))
    return site["file"]


def selftest():
    """[1042-A-03] Selftest: every new criterion has a breaker, and the
    pre-existing ground truths (2026-10-12 Check A/B) still bite."""
    import shutil
    import subprocess
    import tempfile

    base = "com/rikkaminis/app/sandbox/"
    chat = "com/rikkaminis/app/ui/chat/"
    prefix = "src/android/app/src/main/java/"

    post = prefix + base + "Post.kt"
    pre_onfin = prefix + base + "PreOnFin.kt"
    selfbound = prefix + base + "SelfBound.kt"
    pre_catch = prefix + base + "PreCatch.kt"
    rot = prefix + base + "Rot.kt"
    pre_start = prefix + base + "PreStart.kt"
    post_start = prefix + base + "PostStart.kt"
    rot_start = prefix + base + "RotStart.kt"
    polarity = prefix + chat + "Pol.kt"
    b_good = prefix + chat + "BG.kt"
    b_bad = prefix + chat + "BB.kt"
    sib_bad = prefix + chat + "Sib.kt"
    handoff = prefix + chat + "Hand.kt"

    POST = ("""package com.rikkaminis.app.sandbox
class Post {
    var termuxSession: Any? = null
    var startGeneration = 0
    val _state = St()
    val liveSessions = mutableListOf<Any>()
    inner class St { var value: Any? = null }
    fun start() {
        var createdSession: Any? = null
        try {
            val session = Any()
            createdSession = session
            termuxSession = session
        } catch (t: Throwable) {
            createdSession?.let { s ->
                runCatching { kill(s) }
            }
            if (termuxSession === createdSession) {
                termuxSession = null
                liveSessions.removeAll { it === this@Post }
                _state.value = null
            }
        }
    }
    inner class C {
        fun onSessionFinished(changedSession: Any) {
            if (termuxSession === changedSession) {
                startGeneration = startGeneration + 1
                termuxSession = null
                if (_state.value != null) _state.value = null
                liveSessions.removeAll { it === this@Post }
            } else {
                Log.i("stale callback ignored")
            }
        }
    }
    fun kill(s: Any?) {}
    object Log { fun i(m: String) {} }
}
""")

    # Pre-fix onSessionFinished (1042-A-03 shape): generation bump, state
    # publish and liveSessions removal OUTSIDE the ownership guard. The slot
    # clear itself was (brace-lessly) guarded — C1 must NOT flag it.
    PRE_ONFIN = ("""package com.rikkaminis.app.sandbox
class PreOnFin {
    var termuxSession: Any? = null
    var startGeneration = 0
    val _state = St()
    val liveSessions = mutableListOf<Any>()
    inner class St { var value: Any? = null }
    inner class C {
        fun onSessionFinished(changedSession: Any) {
            startGeneration.incrementAndGet()
            if (termuxSession === changedSession) termuxSession = null
            if (_state.value != STOPPED) _state.value = STOPPED
            liveSessions.removeAll { it === this@PreOnFin }
        }
    }
    object STOPPED
}
""")

    # Settlement "guarded" by a self-bound let: the guard is a tautology, so
    # C2 must report it AND C1 must see the decisions as unguarded.
    SELFBOUND = ("""package com.rikkaminis.app.sandbox
class SelfBound {
    var termuxSession: Any? = null
    var startGeneration = 0
    val _state = St()
    val liveSessions = mutableListOf<Any>()
    inner class St { var value: Any? = null }
    inner class C {
        fun onSessionFinished(changedSession: Any) {
            termuxSession?.let { s ->
                if (termuxSession === s) {
                    startGeneration.incrementAndGet()
                    termuxSession = null
                    _state.value = STOPPED
                    liveSessions.removeAll { it === this@SelfBound }
                }
            }
        }
    }
    object STOPPED
}
""")

    # Pre-fix start() catch handler (1042-A-03 shape): self-bound guard only.
    PRE_CATCH = ("""package com.rikkaminis.app.sandbox
class PreCatch {
    var termuxSession: Any? = null
    val _state = St()
    inner class St { var value: Any? = null }
    fun start() {
        try {
            termuxSession = Any()
        } catch (t: Throwable) {
            termuxSession?.let { s ->
                if (termuxSession === s) termuxSession = null
                runCatching { kill(s) }
            }
            _state.value = false
        }
    }
    fun kill(s: Any?) {}
}
""")

    ROT_SRC = ("""package com.rikkaminis.app.sandbox
class Rot {
    inner class C {
        fun onSessionFinished(changedSession: Any) {
            Log.i("finished")
        }
    }
    object Log { fun i(m: String) {} }
}
""")

    # [1044-A02] Pre-fix `fun start` preemption bail-outs: the superseded
    # attempt publishes STOPPED and clears the shared slot unguarded. C3 must
    # catch both, and Check A must see the unguarded slot clear too.
    PRE_START = ("""package com.rikkaminis.app.sandbox
class PreStart {
    var termuxSession: Any? = null
    var startGeneration = 0
    var claimToken: Any? = null
    val _state = St()
    val liveSessions = mutableListOf<Any>()
    inner class St { var value: Any? = null }
    fun start() {
        val myGeneration = startGeneration.incrementAndGet()
        val session = Any()
        if (myGeneration != startGeneration.get()) {
            kill(session)
            _state.value = STOPPED
            return
        }
        termuxSession = session
        if (myGeneration != startGeneration.get()) {
            kill(session)
            termuxSession = null
            _state.value = STOPPED
            return
        }
        val myToken = claimToken
        if (myToken !== claimToken) {
            kill(session)
            liveSessions.removeAll { it === this@PreStart }
            termuxSession = null
            return
        }
        _state.value = RUNNING
    }
    fun attach() {
        try {
            termuxSession = Any()
        } catch (t: Throwable) {
            if (termuxSession === null) {
                termuxSession = null
                liveSessions.removeAll { it === this@PreStart }
            }
        }
    }
    fun kill(s: Any?) {}
    object STOPPED
    object RUNNING
}
""")

    # Post-fix shape: the bail-out reaps the PTY and nothing else; the shared
    # slot/registry release sits behind an identity guard against the value
    # captured at creation.
    POST_START = ("""package com.rikkaminis.app.sandbox
class PostStart {
    var termuxSession: Any? = null
    var startGeneration = 0
    val _state = St()
    val liveSessions = mutableListOf<Any>()
    inner class St { var value: Any? = null }
    fun start() {
        val myGeneration = startGeneration.incrementAndGet()
        val session = Any()
        if (myGeneration != startGeneration.get()) {
            kill(session)
            return
        }
        termuxSession = session
        if (myGeneration != startGeneration.get()) {
            kill(session)
            if (termuxSession === session) {
                termuxSession = null
                liveSessions.removeAll { it === this@PostStart }
            }
            return
        }
        _state.value = RUNNING
    }
    fun kill(s: Any?) {}
    object RUNNING
    object STOPPED
}
""")

    # ROT: the bail-out was renamed away — the criterion must ROT loudly rather
    # than pass silently on a tree that no longer has the checkpoints.
    ROT_START = ("""package com.rikkaminis.app.sandbox
class RotStart {
    var termuxSession: Any? = null
    var startGeneration = 0
    val _state = St()
    inner class St { var value: Any? = null }
    fun begin() {
        val myGeneration = startGeneration.incrementAndGet()
        val session = Any()
        if (myGeneration != startGeneration.get()) {
            kill(session)
            _state.value = STOPPED
            return
        }
    }
    fun kill(s: Any?) {}
    object STOPPED
}
""")

    B_GOOD = ("""package com.rikkaminis.app.ui.chat
class BG {
    var streamingClaimToken: Any = Any()
    var streamJob: Job? = null
    fun send() {
        streamingClaimToken = Any()
        _isStreaming.value = true
        val claimToken = streamingClaimToken
        streamJob = launch {
            try { loop() } finally {
                if (claimToken === streamingClaimToken) { _isStreaming.value = false }
            }
        }
    }
    fun sendSecond() {
        streamingClaimToken = Any()
        _isStreaming.value = true
    }
}
""")

    B_BAD = ("""package com.rikkaminis.app.ui.chat
class BB {
    var streamingClaimToken: Any = Any()
    fun send() {
        _isStreaming.value = true
        streamingClaimToken = Any()
        val claimToken = streamingClaimToken
        launch {}
        if (claimToken === streamingClaimToken) { _isStreaming.value = false }
    }
}
""")

    SIB_BAD = ("""package com.rikkaminis.app.ui.chat
class Sib {
    var streamEpoch = 0
    var curEpoch = 0
    fun a() { if (streamEpoch === curEpoch) { _isStreaming.value = false } }
    fun b() { _isStreaming.value = false }
}
""")

    HANDOFF = ("""package com.rikkaminis.app.ui.chat
class Hand {
    var pendingContinuation: Any? = null
    var gateEpoch = 0
    var curGate = 0
    fun respond() {
        val cont = pendingContinuation ?: return
        pendingContinuation = null
        _pendingRequest.value = null
    }
    fun timedOut() { if (gateEpoch === curGate) { pendingContinuation = null } }
}
""")

    # [1044-A02] Direction proof for Check A: `a` releases behind an identity
    # guard (owner), `b` releases inside a counter INEQUALITY — "I am not the
    # current owner". The pre-1044 gate read `streamGeneration` as ownership
    # vocabulary and certified `b` as gated; the direction-aware gate must
    # report it. This is the mutation arm for the polarity rule itself.
    POLARITY = ("""package com.rikkaminis.app.ui.chat
class Pol {
    var streamJob: Any? = null
    var claimJob: Any? = null
    var streamGeneration = 0
    var myGeneration = 0
    fun a() { if (streamJob === claimJob) { _isStreaming.value = false } }
    fun b() { if (myGeneration != streamGeneration) { _isStreaming.value = false } }
}
""")

    def mutate(src, old, new):
        assert src.count(old) == 1, "selftest fixture mutation anchor not unique"
        return src.replace(old, new)

    # 1044-A02 mutations: each new C3 sub-criterion must flip on its own.
    MUT_NOGUARD = mutate(
        POST_START,
        """            if (termuxSession === session) {
                termuxSession = null
                liveSessions.removeAll { it === this@PostStart }
            }""",
        """            termuxSession = null
            liveSessions.removeAll { it === this@PostStart }""")
    MUT_PUBLISH = mutate(
        POST_START,
        """            kill(session)
            if (termuxSession === session) {""",
        """            kill(session)
            _state.value = STOPPED
            if (termuxSession === session) {""")
    # Guard on the wrong predicate: "the slot is empty" instead of "the slot is
    # still mine". Behaviourally indistinguishable in the JVM device when the
    # slot is occupied (both clear nothing), so the SHAPE criterion is the only
    # device that can see it — see the 1044-A02 arm below and report §4.
    MUT_WRONG_PREDICATE = mutate(
        POST_START,
        "if (termuxSession === session) {",
        "if (termuxSession == null) {")

    def mk_tree(files):
        root = tempfile.mkdtemp(prefix="claim-release-selftest-")
        for rel, text in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return root

    def run_gate(root):
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), root],
            capture_output=True, text=True, timeout=180)
        return proc.returncode, proc.stdout + proc.stderr

    print("── selftest: compliant fixture green, every breaker red, "
          "ground truths intact ──")
    arms = [
        ("post-fix terminal settlement is green (exit 0)", {post: POST}, 0, []),
        ("pre-fix onSessionFinished settlement caught (C1)",
         {pre_onfin: PRE_ONFIN}, 1,
         ["[C1] ", "*Generation counter bump", "_state.value publish",
          "liveSessions removal"]),
        ("pre-fix onSessionFinished: guarded slot clear NOT flagged (precision)",
         {pre_onfin: PRE_ONFIN}, 1, ["NOT termuxSession slot clear"]),
        ("self-bound settlement guard is a tautology (C2 + C1)",
         {selfbound: SELFBOUND}, 1, ["[C2] ", "[C1] "]),
        ("pre-fix self-bound catch handler caught (C2)",
         {pre_catch: PRE_CATCH}, 1, ["[C2] ", "tautology"]),
        ("onSessionFinished with no settlement ROTs",
         {rot: ROT_SRC}, 1, ["C1-ROT"]),
        ("ground truth: claim after mint passes", {b_good: B_GOOD}, 0, []),
        ("ground truth: pre-flip claim caught",
         {b_bad: B_BAD}, 1, ["AFTER the claim"]),
        ("ground truth: ungated sibling caught",
         {sib_bad: SIB_BAD}, 1, ["[A]"]),
        ("ground truth: atomic hand-off passes", {handoff: HANDOFF}, 0, []),
        # ── [1044-A02] start() preemption ownership ──
        ("1044-A02 pre-fix start() preemption settles unowned state (C3)",
         {pre_start: PRE_START}, 1,
         ["[C3] ", "`_state.value` publish inside a preemption bail-out",
          "termuxSession slot clear inside a preemption bail-out without an "
          "ownership guard"]),
        ("1044-A02 pre-fix token-compared bail-out ALSO caught (not "
         "generation-bound)", {pre_start: PRE_START}, 1,
         ["liveSessions removal inside a preemption bail-out"]),
        ("1044-A02 post-fix start() bail-out is green (precision)",
         {post_start: POST_START}, 0, ["NOT C3"]),
        ("1044-A02 start() preemption ROTs when the checkpoints vanish",
         {rot_start: ROT_START}, 1, ["C3-ROT"]),
        ("1044-A02 mutation: slot clear loses its identity guard",
         {post_start: MUT_NOGUARD}, 1,
         ["termuxSession slot clear inside a preemption bail-out",
          "NOT `_state.value` publish"]),
        ("1044-A02 mutation: bail-out publishes state again",
         {post_start: MUT_PUBLISH}, 1,
         ["`_state.value` publish inside a preemption bail-out",
          "NOT termuxSession slot clear"]),
        ("1044-A02 direction: counter INEQUALITY is not an ownership gate (A)",
         {polarity: POLARITY}, 1,
         ["[A] ", "`_isStreaming` — 1 ungated"]),
        ("1044-A02 mutation: guard on `== null` instead of identity is caught",
         {post_start: MUT_WRONG_PREDICATE}, 1,
         ["termuxSession slot clear inside a preemption bail-out without an "
          "ownership guard", "NOT `_state.value` publish"]),
    ]
    failures = []
    for name, files, want_rc, needles in arms:
        root = mk_tree(files)
        try:
            rc, out = run_gate(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        problems = []
        if rc != want_rc:
            problems.append(f"exit={rc} want={want_rc}")
        for needle in needles:
            if needle.startswith("NOT "):
                if needle[4:] in out:
                    problems.append(f"unexpected {needle[4:]!r}")
            elif needle not in out:
                problems.append(f"missing {needle!r}")
        if problems:
            failures.append(f"{name}: {'; '.join(problems)}\n--- output ---\n{out}")
            print(f"  [FAIL] {name}: {'; '.join(problems)}")
        else:
            print(f"  [OK] {name}")
    if failures:
        print(f"SELFTEST FAIL ({len(failures)} arm(s))")
        return 1
    print(f"SELFTEST=PASS ({len(arms)} fixtures: {sum(1 for a in arms if a[2] == 1)} "
          "breakers red, compliant/good arms green)")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    report_only = "--report" in sys.argv
    if "--selftest" in sys.argv:
        return selftest()
    root = os.path.abspath(args[0] if args else ".")

    claims, releases, mints, decls, clean, rawD = collect(root)

    # [1042-A-03 C2] self-bound fake gates, per file.
    fake_gates, c2_reports = {}, []
    for rel, lines in clean.items():
        fk, rep = scan_fake_gates(lines)
        fake_gates[rel] = fk
        c2_reports.extend((rel, ln, recv, param, op, cond)
                          for ln, recv, param, op, cond in rep)

    # ── Check A: sibling-gate inconsistency within one flag's family ──
    families = {}
    for r in releases:
        families.setdefault((r["key"], family_scope(r, decls)), []).append(r)

    marker_by_key = {}  # flag key -> set of gate-compared marker names
    findings_a = []
    for (key, _scope), sites in sorted(families.items()):
        gated, ungated = [], []
        for s in sites:
            cond = enclosing_gate(clean[s["file"]], s["idx"], s["col"],
                                  fake_gates.get(s["file"], ()))
            if cond:
                s["gate_cond"] = cond
                gated.append(s)
                marker_by_key.setdefault(key, set()).update(
                    gate_marker_operands(cond))
            else:
                s["handoff"] = handoff_gate(clean[s["file"]], s["idx"],
                                            s["col"], s["key"])
                ungated.append(s)
        if not gated or not ungated:
            continue
        if (sites[0]["file"], key) in ALLOWED:
            continue  # audited safe (see ALLOWED rationale)
        for s in ungated:
            if s.get("handoff"):
                continue  # atomic hand-off: the slot is the writer's own
            if escaped(rawD[s["file"]], s["line"], RELEASE_ESCAPE_RE):
                continue
            findings_a.append((s, gated[0]))

    # Only names that are actually re-minted somewhere count as markers: a
    # generation stamp compared by the gate AND reassigned as `X = <fresh>`.

    # ── Check C1: onSessionFinished settlement ownership ([1042-A-03]) ──
    findings_c1 = []
    for rel in sorted(clean):
        findings_c1.extend(
            terminal_settlement_findings(rel, clean[rel],
                                         fake_gates.get(rel, ())))

    # ── Check C3: start() preemption settlement ownership ([1044-A02]) ──
    findings_c3 = []
    for rel in sorted(clean):
        findings_c3.extend(
            start_preemption_findings(rel, clean[rel],
                                      fake_gates.get(rel, ())))

    minted_names = {m["var"] for m in mints}

    # ── Check B: claim flips before publishing any gate-compared marker ──
    findings_b = []
    for c in claims:
        markers = (marker_by_key.get(c["key"], set()) & minted_names)
        if not markers:
            continue  # no stamp-based ownership family for this claim
        lo = max(function_start(clean[c["file"]], c["idx"]),
                 c["idx"] - MINT_CLAIM_SEARCH)
        pre = [m for m in mints
               if m["file"] == c["file"] and m["var"] in markers
               and lo <= m["idx"] < c["idx"]]
        if pre:
            continue
        if escaped(rawD[c["file"]], c["line"], CLAIM_ESCAPE_RE):
            continue
        fwd = [m for m in mints
               if m["file"] == c["file"] and m["var"] in markers
               and c["idx"] < m["idx"] <= c["idx"] + FWD_REPORT_WINDOW]
        nearest = min(fwd, key=lambda m: m["idx"]) if fwd else None
        findings_b.append((c, sorted(markers), nearest))

    total = (len(findings_a) + len(findings_b) + len(findings_c1)
             + len(findings_c3) + len(c2_reports))
    if not total:
        print("✅ Claim/release guard: no ungated release, self-bound gate, "
              "unguarded settlement or unminted claim found")
        return 0

    tag = "⚠️  REPORT-ONLY" if report_only else "❌"
    print(f"{tag} Claim/release guard: {total} finding(s)"
          + ("  (report mode — not gating CI yet)" if report_only else ""))
    if findings_a:
        print("   [A] Ungated release whose siblings carry an ownership gate.")
        print("       Stale teardown can clear a newer claim (2026-09-06 / 10-12 family).")
        print("       Gate on ownership, or justify with `release-ok: <reason>`.")
        grouped = {}
        for s, g in findings_a:
            grouped.setdefault((s["file"], s["key"]), []).append((s, g))
        for (f, key), items in sorted(grouped.items()):
            lines = ", ".join(f":{s['line']}" for s, _ in items)
            g = items[0][1]
            print(f"     {f}  `{key}` — {len(items)} ungated: {lines}"
                  f"   | gated sibling :{g['line']} (`{g['gate_cond'][:56]}`)")
    if c2_reports:
        print("   [C2] Self-bound ownership guard — the let-parameter IS the")
        print("       receiver, so `if (X === s)` is a tautology that always")
        print("       passes. The write behind it is effectively ungated")
        print("       (1042-A-03). Compare the slot against a value captured")
        print("       elsewhere, e.g. the local this-attempt created reference.")
        for rel, ln, recv, param, op, cond in c2_reports:
            print(f"     {rel}:{ln + 1}  `{recv}?.let {{ {param} -> … "
                  f"{recv} {op} {param} … }}` — `{cond.strip()[:60]}`")
    if findings_c1:
        print("   [C1] Terminal onSessionFinished settlement outside the")
        print("       ownership guard (1042-A-03). A stale callback landing")
        print("       after a newer start() would bump the generation, publish")
        print("       STOPPED and unregister liveSessions over the live session.")
        for kind, rel, ln, msg in findings_c1:
            print(f"     [{kind}] {rel}:{ln}  {msg}")
    if findings_c3:
        print("   [C3] start() preemption bail-out settles state it no longer")
        print("       owns (1044-A02). A superseded attempt must reap its PTY")
        print("       and nothing else: publishing _state pins the terminal to")
        print("       STOPPED over a newer start(), clearing termuxSession")
        print("       orphans the newer attempt's live PTY.")
        for kind, rel, ln, msg in findings_c3:
            print(f"     [{kind}] {rel}:{ln}  {msg}")
    if findings_b:
        print("   [B] Claim flips the flag before publishing the release gate's marker.")
        print("       Publish (`X = Any()` / `X = epoch`) BEFORE the flip, or justify")
        print("       with `claim-ok: <reason>`. Root cause of 2026-10-12 14:40.")
        for c, markers, nearest in findings_b:
            extra = (f"; nearest mint of {'/'.join(markers)} at :{nearest['line']}"
                     f" is AFTER the claim") if nearest else ""
            print(f"     {c['file']}:{c['line']}  `{c['target']} = true` — no "
                  f"pre-flip mint of {'/'.join(markers)}{extra}")
    return 0 if report_only else 1


if __name__ == "__main__":
    sys.exit(main())

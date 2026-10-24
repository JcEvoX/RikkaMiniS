#!/usr/bin/env python3
"""[deep-audit-1016c FIX-A] Gate: persistence slices start at the run-scoped
UNPERSISTED block index, not at the per-turn start.

Two indices live side by side in `runAgentLoop` and must not be collapsed:

  * `turnStartBlockIndex` — captured at the top of EVERY iteration
    (`allToolBlocks.size` at that moment). It answers "what did THIS turn show?"
    and is what `rollbackTurnBlocksTo` needs (roll back only the failed
    attempt's partial blocks).

  * `loopState.unpersistedBlockStart` — run-scoped, advances ONLY when a persist
    actually landed. Five no-tool continuation branches (EOF-stub, length-wall
    prefill / reminder, verify nudge, residue refill) `continue` without
    persisting the text they already streamed; the next iteration re-captures
    `turnStartBlockIndex` at the new end of the append-only list, so any slice
    that starts there skips the continuation prefix FOREVER — the segment shows
    in the live bubble and vanishes on reload.

Rules enforced here:
  R1 every `buildTurnParts(loopState.allToolBlocks, X, toolInputMap)` call site
     passes `loopState.unpersistedBlockStart` (never `turnStartBlockIndex`);
  R2 rollback keeps the per-turn index (exactly two sites);
  R3 per-turn visibility keeps the per-turn index;
  R4 the no-tool terminal advances the unpersisted index inside its success
     guard (`if (persistedRowId != null) {`), so a failed DB write re-covers;
  R5 the tool-path terminal advances it inside its own dbId guard;
  R6 clearing `allToolBlocks` resets the unpersisted index to 0;
  R7 `AgentLoopState` declares the field (runtime state, not persisted).

Exit 0 = PASS, 1 = FAIL (prints file:line for each violation).
`--selftest` builds violating + compliant fixtures and asserts the checker reds
the first and greens the second (the gate proves its own teeth).

Registration into scripts/scan/scan.sh is the main session's job (this branch is
forbidden from touching scan.sh / test_scan.py).
"""
import os
import re
import sys

ENGINE_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/AgentLoopEngine.kt"
STATE_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/AgentLoopState.kt"

# A persistence slice call, with whatever expression is passed as the start.
SLICE_CALL = re.compile(r"buildTurnParts\(\s*loopState\.allToolBlocks\s*,\s*([A-Za-z0-9_.]+)\s*,")
ROLLBACK = re.compile(r"rollbackTurnBlocksTo\(\s*loopState\.allToolBlocks\s*,\s*([A-Za-z0-9_.]+)\s*\)")
VISIBILITY = re.compile(r"turnVisibleBlocks\s*=\s*loopState\.allToolBlocks\.drop\(([A-Za-z0-9_.]+)\)")
ADVANCE = "loopState.unpersistedBlockStart = loopState.allToolBlocks.size"
RESET = "loopState.unpersistedBlockStart = 0"
FIELD = "var unpersistedBlockStart: Int = 0"

UNPERSISTED = "loopState.unpersistedBlockStart"
PER_TURN = "turnStartBlockIndex"


def line_of(src: str, idx: int) -> int:
    return src.count("\n", 0, idx) + 1


def code_lines(src: str):
    """(line_no, text) for lines that are not pure comments."""
    out = []
    for i, line in enumerate(src.split("\n"), start=1):
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        out.append((i, line))
    return out


def block_after(src: str, anchor: str):
    """Verbatim brace-balanced block opening at/after `anchor`, plus its offset."""
    at = src.find(anchor)
    if at < 0:
        return None, -1
    open_at = src.find("{", at)
    if open_at < 0:
        return None, at
    depth = 0
    i = open_at
    while i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[open_at:i + 1], at
        i += 1
    return None, at


IF_RE = re.compile(r"(?<![\w.])if\s*\(")
BACKFILL = re.compile(r"copy\(\s*dbMessageId\s*=\s*([^)\n]+)\)")
IDX_GUARD = re.compile(r"^\s*[A-Za-z_]\w*\s*(?:>=\s*0|>\s*-1|!=\s*-1)\s*$")
ADVANCE_TO_SIZE = "loopState.unpersistedBlockStart = loopState.allToolBlocks.size"
ASSIGN_INDEX = re.compile(r"loopState\.unpersistedBlockStart\s*(\+=|-=|=)\s*([^\n]*)")
VAL_DECL = re.compile(r"(?m)^[ \t]*(?:val|var)\s+([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*([^\n]*)")


def mask(src):
    """Blank comment bodies and string literal contents, offsets preserved.

    Every criterion below is a dataflow statement about CODE; a `//` comment or
    a log string that happens to contain the same tokens must not satisfy it.
    Strings are scanned before comments, so `"video/*"` cannot open a block
    comment.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            if src.startswith('"""', i):
                j = i + 3
                while j < n and not src.startswith('"""', j):
                    if src[j] != "\n":
                        out[j] = " "
                    j += 1
                i = j + 3
                continue
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    out[j] = " "
                    if j + 1 < n and src[j + 1] != "\n":
                        out[j + 1] = " "
                    j += 2
                    continue
                if src[j] == '"':
                    break
                if src[j] != "\n":
                    out[j] = " "
                j += 1
            i = j + 1
            continue
        if c == "'":
            m = re.match(r"'(?:\\.|[^'\\\n])'", src[i:])
            if m:
                for k in range(i, i + m.end()):
                    out[k] = " "
                i += m.end()
                continue
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = i
            while j < n and src[j] != "\n":
                out[j] = " "
                j += 1
            i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = i
            while j < n and not (src[j] == "*" and j + 1 < n and src[j + 1] == "/"):
                if src[j] != "\n":
                    out[j] = " "
                j += 1
            for k in range(j, min(j + 2, n)):
                if src[k] != "\n":
                    out[k] = " "
            i = j + 2
            continue
        i += 1
    return "".join(out)


def brace_span(text, open_idx):
    depth = 0
    for k in range(open_idx, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return open_idx, k + 1
    return None


def guards(text):
    """[(cond, body_start, body_end)] for every `if (…)` with a bounded body."""
    out = []
    for m in IF_RE.finditer(text):
        i = text.find("(", m.end() - 1)
        if i < 0:
            continue
        depth = 0
        j = i
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        cond = text[i + 1:j]
        k = j + 1
        while k < len(text) and text[k] in " \t\r\n":
            k += 1
        if k < len(text) and text[k] == "{":
            sp = brace_span(text, k)
            if sp is None:
                continue
            out.append((cond, sp[0], sp[1]))
        else:
            depth2 = 0
            end = len(text)
            for k2 in range(k, len(text)):
                ch = text[k2]
                if ch in "([{":
                    depth2 += 1
                elif ch in ")]}":
                    if depth2 == 0:
                        end = k2
                        break
                    depth2 -= 1
                elif ch == "\n" and depth2 == 0:
                    end = k2
                    break
            out.append((cond, k, end))
    return out


def enclosing(gs, pos):
    return [g for g in gs if g[1] <= pos < g[2]]


def local_domains(code):
    """Integer domains implied by a local's initializer.

    `indexOfLast` / `indexOf` / `lastIndexOf` return -1 when absent, so the
    variable is >= -1; `.size` is >= 0. Used to prove a guard is REACHABLE
    instead of accepting any comparison against any literal.
    """
    d = {}
    for m in VAL_DECL.finditer(code):
        name, expr = m.group(1), m.group(2)
        if "indexOfLast" in expr or "indexOf" in expr or "lastIndexOf" in expr:
            d[name] = (-1, None)
        elif ".size" in expr:
            d[name] = (0, None)
    return d


def _max_lo(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _min_hi(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


FLIP = {"==": "==", "!=": "!=", "<": ">", ">": "<", "<=": ">=", ">=": "<="}


def feasible(cond, domains):
    """False only when the condition can never hold given the known domains."""
    c = cond.strip()
    if c in ("false", "!true"):
        return False
    m = re.fullmatch(r"(-?\d+)\s*(==|!=|<|>|<=|>=)\s*(-?\d+)", c)
    if m:
        a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
        return {"==": a == b, "!=": a != b, "<": a < b,
                ">": a > b, "<=": a <= b, ">=": a >= b}[op]
    m = re.fullmatch(r"([A-Za-z_]\w*)\s*(==|!=|<|>|<=|>=)\s*(-?\d+)", c)
    if m:
        name, op, num = m.group(1), m.group(2), int(m.group(3))
    else:
        m = re.fullmatch(r"(-?\d+)\s*(==|!=|<|>|<=|>=)\s*([A-Za-z_]\w*)", c)
        if not m:
            return True
        num, op, name = int(m.group(1)), FLIP[m.group(2)], m.group(3)
    if name not in domains:
        return True
    lo, hi = domains[name]
    if op == ">=":
        slo, shi = num, None
    elif op == ">":
        slo, shi = num + 1, None
    elif op == "<=":
        slo, shi = None, num
    elif op == "<":
        slo, shi = None, num - 1
    elif op == "==":
        slo, shi = num, num
    else:  # !=
        return not (lo is not None and hi is not None and lo == hi == num)
    nlo, nhi = _max_lo(lo, slo), _min_hi(hi, shi)
    return not (nlo is not None and nhi is not None and nlo > nhi)


def check_source(engine: str, state: str, label: str):
    bad = []

    # ── R1: every persistence slice reads the run-scoped index ────────────
    sites = []
    for line_no, text in code_lines(engine):
        for m in SLICE_CALL.finditer(text):
            sites.append((line_no, m.group(1)))
    if not sites:
        bad.append("%s:1: no buildTurnParts(loopState.allToolBlocks, …) persistence slice found — "
                   "did the call shape change?" % label)
    for line_no, start in sites:
        if start != UNPERSISTED:
            bad.append("%s:%d: persistence slice starts at '%s' — the per-turn start skips blocks a "
                       "continuation branch already streamed (they never reach the DB); it must be '%s'"
                       % (label, line_no, start, UNPERSISTED))

    # ── R2: rollback keeps the per-turn index ─────────────────────────────
    rollbacks = [(n, m.group(1)) for n, t in code_lines(engine) for m in ROLLBACK.finditer(t)]
    if len(rollbacks) != 2:
        bad.append("%s:1: expected exactly 2 rollbackTurnBlocksTo(loopState.allToolBlocks, …) sites, found %d"
                   % (label, len(rollbacks)))
    for line_no, start in rollbacks:
        if start != PER_TURN:
            bad.append("%s:%d: rollback uses '%s' — rollback must remove only the failed attempt's blocks "
                       "(%s), never an earlier turn's unpersisted prefix"
                       % (label, line_no, start, PER_TURN))

    # ── R3: per-turn visibility keeps the per-turn index ──────────────────
    vis = [(n, m.group(1)) for n, t in code_lines(engine) for m in VISIBILITY.finditer(t)]
    if not vis:
        bad.append("%s:1: turnVisibleBlocks = allToolBlocks.drop(…) not found — the per-turn visibility "
                   "slice disappeared" % label)
    for line_no, start in vis:
        if start != PER_TURN:
            bad.append("%s:%d: per-turn visibility uses '%s' — with the run-scoped index the 'turn showed "
                       "nothing' test can never fire again" % (label, line_no, start))

    # ── R4: no-tool terminal advances inside its success guard ────────────
    guard, at = block_after(engine, "if (persistedRowId != null) {")
    if guard is None:
        bad.append("%s:1: `if (persistedRowId != null) {` success guard not found at the no-tool terminal"
                   % label)
    elif ADVANCE not in guard:
        bad.append("%s:%d: the no-tool terminal does not advance the unpersisted index inside its success "
                   "guard — the next persist would re-send blocks this row already carries"
                   % (label, line_of(engine, at)))

    # ── R5: tool terminal advances inside its dbId guard ──────────────────
    tool_guard, tool_at = block_after(engine, "if (assistantDbId != null) {")
    if tool_guard is None:
        bad.append("%s:1: `if (assistantDbId != null) {` guard not found at the tool terminal" % label)
    elif ADVANCE not in tool_guard:
        bad.append("%s:%d: the tool-path terminal does not advance the unpersisted index inside its dbId "
                   "guard" % (label, line_of(engine, tool_at)))

    # ── R5b: the no-tool terminal backfills dbMessageId (ISSUE-01/B) ──────
    # Without it the assistant row reloads with a null DB anchor: compaction
    # falls back to a degraded walk and every dbId-dependent path degrades.
    if guard is not None and "copy(dbMessageId = persistedRowId)" not in guard:
        bad.append("%s:%d: the no-tool terminal does not backfill dbMessageId inside its success guard "
                   "(ISSUE-01/B) — the tool path does" % (label, line_of(engine, at)))

    # ── R6: clearing the block list resets the index ──────────────────────
    clear_at = -1
    for line_no, text in code_lines(engine):
        if "loopState.allToolBlocks.clear()" in text:
            clear_at = line_no
            break
    if clear_at < 0:
        bad.append("%s:1: loopState.allToolBlocks.clear() not found (queue-interrupt reset site)" % label)
    else:
        window = "\n".join(engine.split("\n")[clear_at - 1: clear_at + 5])
        if RESET not in window:
            bad.append("%s:%d: clear site does not reset the unpersisted index to 0 within 5 lines — the "
                       "two indices drift apart on a queued-message interrupt" % (label, clear_at))

    # ── R7: the state field exists ────────────────────────────────────────
    if FIELD not in state:
        bad.append("%s:1: AgentLoopState does not declare '%s'" % (state.split("/")[-1], FIELD))

    # ── R8: the run-scoped index may only be reset or advanced to the count ─
    # [wide-1024 W1] `loopState.unpersistedBlockStart = turnStartBlockIndex`
    # re-collapses the two indices this whole gate exists to keep apart: R1 only
    # inspected the argument text of buildTurnParts, R4 only matched the advance
    # shape, so a tainted assignment walked straight through both.
    code = mask(engine)
    for m in ASSIGN_INDEX.finditer(code):
        op, rhs = m.group(1), m.group(2).strip().rstrip(";").strip()
        line = line_of(engine, m.start())
        if op != "=":
            bad.append("%s:%d: the unpersisted index is adjusted with '%s' — it is set to 0 "
                       "(reset) or to loopState.allToolBlocks.size (advance), never "
                       "arithmetically" % (label, line, op))
        elif rhs not in ("0", "loopState.allToolBlocks.size"):
            bad.append("%s:%d: the unpersisted index is set to '%s' — only 0 (reset) or "
                       "loopState.allToolBlocks.size (advance) may feed it; anything derived "
                       "from the per-turn start re-collapses the two indices and cuts the blocks "
                       "a continuation branch already streamed out of the next persist"
                       % (label, line, rhs))

    # ── R9: every advance must sit inside a success (null-id) guard ───────
    # [wide-1024 W2] A second, unconditional advance hoisted ABOVE
    # `if (persistedRowId != null)` leaves the guarded copy intact and still
    # moves the index past blocks that never reached the DB.
    gs = guards(code)
    for m in re.finditer(re.escape(ADVANCE_TO_SIZE), code):
        line = line_of(engine, m.start())
        encl = enclosing(gs, m.start())
        if not any(re.fullmatch(r"^[A-Za-z_][\w.]*\s*!=\s*null$", c.strip())
                   for c, _a, _b in encl):
            bad.append("%s:%d: the unpersisted index advances OUTSIDE a success guard "
                       "(`if (persistedRowId != null)` / `if (assistantDbId != null)`) — when the "
                       "insert returns null the range is still marked durable and a retry can "
                       "never re-cover it" % (label, line))

    # ── R10: the dbId backfill must be REACHABLE ─────────────────────────
    # [wide-1024 W3] `if (lastAssistantIdx >= 0)` flipped to `< -1` is a
    # forever-false guard: indexOfLast returns >= -1, so the backfill never runs
    # while its text (`copy(dbMessageId = persistedRowId)`) stays in place.
    domains = local_domains(code)
    for m in BACKFILL.finditer(code):
        line = line_of(engine, m.start())
        val = m.group(1).strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", val) or val == "null":
            bad.append("%s:%d: dbMessageId is backfilled from '%s' — it must be the row id "
                       "returned by the persist call" % (label, line, val))
        encl = enclosing(gs, m.start())
        if not encl:
            bad.append("%s:%d: the dbMessageId backfill is unguarded — indexOfLast returns -1 "
                       "when no assistant row is pending" % (label, line))
            continue
        for cond, _a, _b in encl:
            if not feasible(cond, domains):
                bad.append("%s:%d: the dbMessageId backfill sits behind the unreachable guard "
                           "`if (%s)` — the guarded value's domain makes the condition "
                           "impossible, so the backfill can never run"
                           % (label, line, cond.strip()))
        if not any(IDX_GUARD.match(c) for c, _a, _b in encl):
            bad.append("%s:%d: the dbMessageId backfill is not guarded by an index-presence "
                       "check (`if (<index> >= 0)`) — a -1 index would rewrite the last message"
                       % (label, line))

    return bad


def run(repo: str) -> int:
    engine_path = os.path.join(repo, ENGINE_REL)
    state_path = os.path.join(repo, STATE_REL)
    for p in (engine_path, state_path):
        if not os.path.exists(p):
            print("FAIL %s: not found (run from the repo root)" % p)
            return 1
    engine = open(engine_path, encoding="utf-8", errors="replace").read()
    state = open(state_path, encoding="utf-8", errors="replace").read()
    bad = check_source(engine, state, ENGINE_REL)
    if bad:
        for b in bad:
            print("FAIL " + b)
        return 1
    print("PASS loop_persist_slice_check (persistence slices start at "
          "loopState.unpersistedBlockStart; rollback + per-turn visibility keep turnStartBlockIndex; "
          "advance only inside success guards; clear resets to 0)")
    return 0


# ── fixtures for --selftest ────────────────────────────────────────────────

GOOD_ENGINE = '''
                val turnParts = host.buildTurnParts(loopState.allToolBlocks, loopState.unpersistedBlockStart, toolInputMap)
                val persistedRowId = host.persistAssistantTurn(turnParts)
                if (persistedRowId != null) {
                    loopState.unpersistedBlockStart = loopState.allToolBlocks.size
                    val idx = host.agentHistory.indexOfLast { it.dbMessageId == null }
                    if (idx >= 0) host.agentHistory[idx] = host.agentHistory[idx].copy(dbMessageId = persistedRowId)
                }
            val livePreviewParts = host.buildTurnParts(loopState.allToolBlocks, loopState.unpersistedBlockStart, toolInputMap)
            val turnParts2 = host.buildTurnParts(loopState.allToolBlocks, loopState.unpersistedBlockStart, toolInputMap)
                if (assistantDbId != null) {
                    loopState.unpersistedBlockStart = loopState.allToolBlocks.size
                }
                    loopState.allToolBlocks.clear()
                    loopState.unpersistedBlockStart = 0
                rollbackTurnBlocksTo(loopState.allToolBlocks, turnStartBlockIndex)
                rollbackTurnBlocksTo(loopState.allToolBlocks, turnStartBlockIndex)
                val turnVisibleBlocks = loopState.allToolBlocks.drop(turnStartBlockIndex)
'''

BAD_PER_TURN_SLICE = GOOD_ENGINE.replace(
    "val turnParts = host.buildTurnParts(loopState.allToolBlocks, loopState.unpersistedBlockStart,",
    "val turnParts = host.buildTurnParts(loopState.allToolBlocks, turnStartBlockIndex,",
)

BAD_NO_ADVANCE = GOOD_ENGINE.replace(
    "                    loopState.unpersistedBlockStart = loopState.allToolBlocks.size\n",
    "",
)

BAD_ROLLBACK_SWITCHED = GOOD_ENGINE.replace(
    "rollbackTurnBlocksTo(loopState.allToolBlocks, turnStartBlockIndex)\n                rollbackTurnBlocksTo(loopState.allToolBlocks, turnStartBlockIndex)",
    "rollbackTurnBlocksTo(loopState.allToolBlocks, loopState.unpersistedBlockStart)\n"
    "                rollbackTurnBlocksTo(loopState.allToolBlocks, loopState.unpersistedBlockStart)",
)

BAD_CLEAR_NO_RESET = GOOD_ENGINE.replace(
    "                    loopState.allToolBlocks.clear()\n                    loopState.unpersistedBlockStart = 0\n",
    "                    loopState.allToolBlocks.clear()\n",
)

# R5b: no-tool terminal keeps the advance but loses the dbId backfill.
BAD_NO_BACKFILL = GOOD_ENGINE.replace(
    "                    if (idx >= 0) host.agentHistory[idx] = host.agentHistory[idx].copy(dbMessageId = persistedRowId)\n",
    "",
)

# ── wide-1024 witnesses (each one passed this gate unchanged) ───────────────

# W1 (dataflow): the run-scoped index re-tainted from the per-turn start.
BAD_TAINTED_ADVANCE = GOOD_ENGINE.replace(
    "                val turnParts = host.buildTurnParts(loopState.allToolBlocks, loopState.unpersistedBlockStart, toolInputMap)\n",
    "                loopState.unpersistedBlockStart = turnStartBlockIndex\n"
    "                val turnParts = host.buildTurnParts(loopState.allToolBlocks, loopState.unpersistedBlockStart, toolInputMap)\n",
)

# W2 (order): a second advance hoisted ABOVE the success guard.
BAD_HOISTED_ADVANCE = GOOD_ENGINE.replace(
    """                if (persistedRowId != null) {
                    loopState.unpersistedBlockStart = loopState.allToolBlocks.size""",
    """                loopState.unpersistedBlockStart = loopState.allToolBlocks.size
                if (persistedRowId != null) {
                    loopState.unpersistedBlockStart = loopState.allToolBlocks.size""",
)

# W3 (dataflow): the inner index guard flipped to a forever-false comparison.
BAD_UNREACHABLE_BACKFILL = GOOD_ENGINE.replace(
    "                    if (idx >= 0) host.agentHistory[idx]",
    "                    if (idx < -1) host.agentHistory[idx]",
)

GOOD_STATE = "    var unpersistedBlockStart: Int = 0\n"
BAD_STATE = "    var somethingElse: Int = 0\n"


def selftest() -> int:
    failures = []

    good = check_source(GOOD_ENGINE, GOOD_STATE, "good.kt")
    if good:
        failures.append("compliant fixture must be clean, got %s" % good)

    bad_slice = check_source(BAD_PER_TURN_SLICE, GOOD_STATE, "bad_slice.kt")
    if not any("persistence slice starts at 'turnStartBlockIndex'" in b for b in bad_slice):
        failures.append("per-turn-slice fixture did not trip R1 (got %s)" % bad_slice)

    bad_adv = check_source(BAD_NO_ADVANCE, GOOD_STATE, "bad_advance.kt")
    if not any("does not advance the unpersisted index" in b for b in bad_adv):
        failures.append("missing-advance fixture did not trip R4/R5 (got %s)" % bad_adv)

    bad_rb = check_source(BAD_ROLLBACK_SWITCHED, GOOD_STATE, "bad_rollback.kt")
    if len([b for b in bad_rb if "rollback uses" in b]) != 2:
        failures.append("over-broad rollback fixture did not trip R2 twice (got %s)" % bad_rb)

    bad_clear = check_source(BAD_CLEAR_NO_RESET, GOOD_STATE, "bad_clear.kt")
    if not any("clear site does not reset" in b for b in bad_clear):
        failures.append("clear-without-reset fixture did not trip R6 (got %s)" % bad_clear)

    bad_backfill = check_source(BAD_NO_BACKFILL, GOOD_STATE, "bad_backfill.kt")
    if not any("does not backfill dbMessageId" in b for b in bad_backfill):
        failures.append("missing-backfill fixture did not trip R5b (got %s)" % bad_backfill)

    bad_taint = check_source(BAD_TAINTED_ADVANCE, GOOD_STATE, "bad_taint.kt")
    if not any("is set to 'turnStartBlockIndex'" in b for b in bad_taint):
        failures.append("W1 tainted-advance fixture did not trip R8 (got %s)" % bad_taint)

    bad_hoist = check_source(BAD_HOISTED_ADVANCE, GOOD_STATE, "bad_hoist.kt")
    if not any("advances OUTSIDE a success guard" in b for b in bad_hoist):
        failures.append("W2 hoisted-advance fixture did not trip R9 (got %s)" % bad_hoist)

    bad_unreach = check_source(BAD_UNREACHABLE_BACKFILL, GOOD_STATE, "bad_unreach.kt")
    if not any("unreachable guard" in b for b in bad_unreach):
        failures.append("W3 unreachable-backfill fixture did not trip R10 (got %s)" % bad_unreach)

    bad_state = check_source(GOOD_ENGINE, BAD_STATE, "bad_state.kt")
    if not any("does not declare" in b for b in bad_state):
        failures.append("missing-field fixture did not trip R7 (got %s)" % bad_state)

    print("selftest good fixture            -> %d violation(s)" % len(good))
    print("selftest per-turn-slice fixture  -> %d violation(s)" % len(bad_slice))
    print("selftest missing-advance fixture -> %d violation(s)" % len(bad_adv))
    print("selftest over-broad-rollback     -> %d violation(s)" % len(bad_rb))
    print("selftest clear-without-reset     -> %d violation(s)" % len(bad_clear))
    print("selftest missing-backfill fixture-> %d violation(s)" % len(bad_backfill))
    print("selftest tainted-advance       -> %d violation(s)" % len(bad_taint))
    print("selftest hoisted-advance       -> %d violation(s)" % len(bad_hoist))
    print("selftest unreachable-backfill  -> %d violation(s)" % len(bad_unreach))
    print("selftest missing-field fixture   -> %d violation(s)" % len(bad_state))

    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    print("SELFTEST=PASS loop_persist_slice_check (6 violating fixtures red, compliant fixture green)")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        return selftest()
    repo = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(repo)


if __name__ == "__main__":
    sys.exit(main())

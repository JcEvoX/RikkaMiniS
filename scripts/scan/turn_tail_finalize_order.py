#!/usr/bin/env python3
"""[deep-audit-1017 FIX-B] Gate: the turn-tail row finalize must not be skippable.

Two shapes broke the agent loop tail and are cheap to catch mechanically:

1. ISSUE-02 — `updateAssistantMessage(id, accumulatedText, /*isStreaming=*/false, blocks)`
   (the "this turn's row is done" write) sat ABOVE the four `continue` branches of
   the `toolCalls.isEmpty()` tail (empty-turn recovery x2, EOF-stub continuation,
   EOF one-shot retry). Every one of those keeps streaming into the SAME assistant
   id (`loopState.assistantId` is minted once per run), and a finalized row
   rejects every later delta (`StaleStreamPublishPolicy.DROP_FINALIZED`), so the
   bubble froze for the rest of the run. Rule: exactly one finalize, and no
   `continue` may sit between it and the tail's `break`.

2. ISSUE-03 — the tail asked "did this turn show the user anything?" of the
   RUN-scoped accumulators (`loopState.accumulatedText` / `allToolBlocks`), which
   are append-only, so the answer stayed true from the first tool turn onward and
   the empty-turn recovery could never fire again. Rule: every `TurnVisibleContent.*`
   decision is fed per-TURN values — never the run-scoped accumulators, directly
   or through a local alias.

Wide-sweep hardening (meta-wide 06 / wide-1024, three gate-blind witnesses):
  W1 `if (turn < 10) { continue }` after the finalize ⇒ the old `continue` regex
     assumed the keyword owns its line (`\\n[ \\t]*continue[ \\t]*(\\n|//)`), so a
     brace-wrapped / `if (…) continue` spelling was invisible (spelling). The
     keyword is now searched as a TOKEN anywhere between finalize and break, in
     comments-and-strings-masked code, so every spelling counts.
  W2 `val fullText = loopState.accumulatedText` then
     `hasVisibleContent(fullText, …)` ⇒ argument-text matching saw no accumulator
     (dataflow). Local aliases of the run-scoped accumulators are now tainted
     one-hop (fixpoint) and taint is rejected at the call site.
  W3 `isEmptyTurn(loopState.accumulatedText, loopState.allToolBlocks, …)` ⇒ only
     `hasVisibleContent` was guarded, while `isEmptyTurn` is the branch that
     actually owns the recovery `continue` (dataflow/completeness). The rule is
     now applied to EVERY `TurnVisibleContent.<fn>(…)` call.

Exit 0 = PASS, 1 = FAIL (prints file:line for each violation).
`--selftest` builds violating fixtures and a compliant fixture and asserts the
checker reds the first and greens the second (the gate proves its own teeth).

Registration into scripts/scan/scan.sh is the main session's job (this branch is
forbidden from touching scan.sh / test_scan.py).
"""
import os
import re
import sys
import tempfile

REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/AgentLoopEngine.kt"

FINALIZE = re.compile(
    r"updateAssistantMessage\(\s*loopState\.assistantId,\s*loopState\.accumulatedText,\s*false,",
    re.S,
)
# Tokens, not line shapes: a `continue` inside `if (…) { … }` or a one-liner
# `if (…) continue` is the same control-flow escape as a bare one.
CONTINUE_TOKEN = re.compile(r"(?<![\w.$])continue\b")
BREAK_TOKEN = re.compile(r"(?<![\w.$])break\b")
# `val hasVisibleContent = …accumulatedText…` possibly continued on the next line.
VISIBLE_FROM_ACCUM = re.compile(
    r"val\s+hasVisibleContent\s*=[^\n]*(?:\n[^\n]*)?", re.M
)
PER_TURN_CALL = "TurnVisibleContent.hasVisibleContent("
ANY_TVC_CALL = re.compile(r"TurnVisibleContent\.([A-Za-z_]\w*)\s*\(")
VAL_DECL = re.compile(r"(?m)^[ \t]*(?:val|var)\s+([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*([^\n]*)")
PER_TURN_SLICE = r"^loopState\.allToolBlocks\.drop\(\s*turnStartBlockIndex\s*\)$"
ACCUMULATORS = ("loopState.accumulatedText", "loopState.allToolBlocks")


def line_of(src: str, idx: int) -> int:
    return src.count("\n", 0, idx) + 1


def mask(src):
    """Blank comment bodies and string/char literal CONTENTS, offsets preserved.

    Strings are scanned before comments so a `"video/*"` literal cannot open a
    block comment and blank the rest of the file.
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


def paren_inner(text, open_idx):
    j = text.find("(", open_idx)
    if j < 0:
        return None, None
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                return text[j + 1:k], k + 1
    return None, None


def split_args(inner):
    args, cur, depth = [], "", 0
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur)
    return [a.strip() for a in args]


def tainted_locals(code):
    """Locals whose value is the run-scoped accumulator (or an alias of one).

    A per-turn slice — `loopState.allToolBlocks.drop(turnStartBlockIndex)` — is
    explicitly NOT tainted: that is the correct expression the fix introduced.
    """
    tainted = set()
    changed = True
    while changed:
        changed = False
        for m in VAL_DECL.finditer(code):
            name, expr = m.group(1), m.group(2).strip()
            if name in tainted:
                continue
            if expr in ACCUMULATORS or expr in tainted:
                tainted.add(name)
                changed = True
    return tainted


def accumulator_arg(arg, tainted):
    e = arg.strip()
    if e in ACCUMULATORS or e in tainted:
        return True
    if re.match(PER_TURN_SLICE, e):
        return False
    if e.startswith("loopState.allToolBlocks.drop("):
        return True          # a slice at any OTHER index is not this turn
    if e.startswith("loopState.allToolBlocks.filter("):
        return True
    if e.startswith(("loopState.allToolBlocks", "loopState.accumulatedText")):
        return True
    return False


def check_source(src: str, label: str) -> list:
    """Returns a list of 'label:line: message' violations (empty = PASS)."""
    bad = []
    code = mask(src)

    hits = [m for m in FINALIZE.finditer(code)]
    if len(hits) != 1:
        bad.append("%s:%d: expected exactly 1 turn-tail finalize "
                   "(updateAssistantMessage(assistantId, accumulatedText, false, …)), found %d"
                   % (label, line_of(src, hits[0].start()) if hits else 1, len(hits)))
    if not hits:
        return bad

    at = hits[0].start()
    br = BREAK_TOKEN.search(code, at)
    if br is None:
        bad.append("%s:%d: no `break` statement after the turn-tail finalize — the tail must "
                   "end the loop right after finalizing" % (label, line_of(src, at)))
    else:
        cm = CONTINUE_TOKEN.search(code, at, br.start())
        if cm is not None:
            bad.append("%s:%d: a `continue` branch can bypass the turn-tail finalize "
                       "(the finalized row then rejects the continued turn's deltas)"
                       % (label, line_of(src, cm.start())))

    if PER_TURN_CALL not in code:
        bad.append("%s:1: the tail does not use TurnVisibleContent.hasVisibleContent(…) — the "
                   "per-turn visible-content predicate is missing" % label)
    for m in VISIBLE_FROM_ACCUM.finditer(code):
        if "loopState.accumulatedText" in m.group(0):
            bad.append("%s:%d: hasVisibleContent is derived from the run-scoped accumulator "
                       "loopState.accumulatedText (ISSUE-03)" % (label, line_of(src, m.start())))

    # ── every TurnVisibleContent decision must be fed THIS turn's values ───
    tainted = tainted_locals(code)
    for m in ANY_TVC_CALL.finditer(code):
        fn = m.group(1)
        inner, _ = paren_inner(code, m.end() - 1)
        if inner is None:
            bad.append("%s:%d: TurnVisibleContent.%s( … ) arguments could not be parsed "
                       "(fail-closed)" % (label, line_of(src, m.start()), fn))
            continue
        for arg in split_args(inner):
            if accumulator_arg(arg, tainted):
                bad.append("%s:%d: TurnVisibleContent.%s is fed the run-scoped accumulator "
                           "'%s' — the answer stays true from the first tool turn onward, so "
                           "the empty-turn recovery can never fire (ISSUE-03)"
                           % (label, line_of(src, m.start()), fn, arg))
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
    print("PASS turn_tail_finalize_order (finalize after every continue branch; per-turn predicate)")
    return 0


GOOD = '''            if (toolCalls.isEmpty()) {
                val turnVisibleBlocks = loopState.allToolBlocks.drop(turnStartBlockIndex)
                val hasVisibleContent = TurnVisibleContent.hasVisibleContent(turnText, turnVisibleBlocks)
                val finishedCleanly = TurnVisibleContent.finishedCleanly(turnFinishReason, turnTruncated)
                if (TurnVisibleContent.isEmptyTurn(turnText, turnVisibleBlocks, turnFinishReason, turnTruncated)) {
                    host.agentHistory.removeAt(host.agentHistory.size - 1)
                    continue
                }
                host.fireBanner()
                withContext(Dispatchers.Main) {
                    host.updateAssistantMessage(loopState.assistantId, loopState.accumulatedText, false, loopState.allToolBlocks)
                }
                break
            }
'''

BAD_SKIPPABLE = '''            if (toolCalls.isEmpty()) {
                val hasVisibleContent = loopState.accumulatedText.isNotBlank() ||
                    loopState.allToolBlocks.any { it.kind == "tool_use" }
                withContext(Dispatchers.Main) {
                    host.updateAssistantMessage(loopState.assistantId, loopState.accumulatedText, false, loopState.allToolBlocks)
                }
                if (!hasVisibleContent) {
                    host.fireBanner()
                    continue
                }
                break
            }
'''

# W1: one-line continue after the finalize — invisible to the old line-anchored regex.
BAD_SINGLE_LINE_CONTINUE = '''            if (toolCalls.isEmpty()) {
                val turnVisibleBlocks = loopState.allToolBlocks.drop(turnStartBlockIndex)
                val hasVisibleContent = TurnVisibleContent.hasVisibleContent(turnText, turnVisibleBlocks)
                host.fireBanner()
                withContext(Dispatchers.Main) {
                    host.updateAssistantMessage(loopState.assistantId, loopState.accumulatedText, false, loopState.allToolBlocks)
                }
                if (turn < 10) { continue }
                break
            }
'''

# W2: accumulator laundered through a local alias.
BAD_ALIASED_VISIBLE = '''            if (toolCalls.isEmpty()) {
                val turnVisibleBlocks = loopState.allToolBlocks.drop(turnStartBlockIndex)
                val fullText = loopState.accumulatedText
                val hasVisibleContent = TurnVisibleContent.hasVisibleContent(fullText, turnVisibleBlocks)
                host.fireBanner()
                withContext(Dispatchers.Main) {
                    host.updateAssistantMessage(loopState.assistantId, loopState.accumulatedText, false, loopState.allToolBlocks)
                }
                break
            }
'''

# W3: the branch that owns the recovery `continue` reads the accumulators directly.
BAD_ISEMPTYTURN_ACCUM = '''            if (toolCalls.isEmpty()) {
                val turnVisibleBlocks = loopState.allToolBlocks.drop(turnStartBlockIndex)
                val hasVisibleContent = TurnVisibleContent.hasVisibleContent(turnText, turnVisibleBlocks)
                if (TurnVisibleContent.isEmptyTurn(loopState.accumulatedText, loopState.allToolBlocks, turnFinishReason, turnTruncated)) {
                    host.agentHistory.removeAt(host.agentHistory.size - 1)
                    continue
                }
                host.fireBanner()
                withContext(Dispatchers.Main) {
                    host.updateAssistantMessage(loopState.assistantId, loopState.accumulatedText, false, loopState.allToolBlocks)
                }
                break
            }
'''


def selftest() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as d:
        good_path = os.path.join(d, "good.kt")
        bad_path = os.path.join(d, "bad.kt")
        open(good_path, "w").write(GOOD)
        open(bad_path, "w").write(BAD_SKIPPABLE)
        good = check_source(GOOD, "good.kt")
        bad = check_source(BAD_SKIPPABLE, "bad.kt")
        print("selftest good fixture -> %d violation(s)" % len(good))
        print("selftest bad  fixture -> %d violation(s)" % len(bad))
        for v in bad:
            print("   " + v)
        if good:
            failures.append("compliant fixture must be clean, got %s" % good)
        if not bad:
            failures.append("violating fixture must red")
        if not any("can bypass the turn-tail finalize" in b for b in bad):
            failures.append("violating fixture did not trip the placement rule")
        if not any("run-scoped accumulator" in b for b in bad):
            failures.append("violating fixture did not trip the ISSUE-03 rule")
        # A finalize REMOVED entirely must red too.
        if not check_source(GOOD.replace("updateAssistantMessage(loopState.assistantId,", "updateAssistantMessage(otherId,"), "x.kt"):
            failures.append("missing finalize not detected")

        # ── wide-1024 witnesses: each one used to pass this gate ───────────
        w1 = check_source(BAD_SINGLE_LINE_CONTINUE, "w1.kt")
        print("selftest W1 one-line continue -> %d violation(s)" % len(w1))
        if not any("can bypass the turn-tail finalize" in b for b in w1):
            failures.append("W1 (one-line `if (…) { continue }`) not detected")
        w2 = check_source(BAD_ALIASED_VISIBLE, "w2.kt")
        print("selftest W2 aliased accumulator -> %d violation(s)" % len(w2))
        if not any("fullText" in b for b in w2):
            failures.append("W2 (local alias of loopState.accumulatedText) not detected")
        w3 = check_source(BAD_ISEMPTYTURN_ACCUM, "w3.kt")
        print("selftest W3 isEmptyTurn accumulators -> %d violation(s)" % len(w3))
        if not any("isEmptyTurn" in b for b in w3):
            failures.append("W3 (accumulators fed to isEmptyTurn) not detected")
    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    print("SELFTEST PASS")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if "--selftest" in args:
        return selftest()
    repo = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(repo)


if __name__ == "__main__":
    sys.exit(main())

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
   the empty-turn recovery could never fire again. Rule: the visible-content
   decision comes from `TurnVisibleContent` over THIS turn's text + blocks, and no
   `hasVisibleContent` assignment may read the run-scoped accumulators.

Exit 0 = PASS, 1 = FAIL (prints file:line for each violation).
`--selftest` builds a violating fixture and a compliant fixture and asserts the
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
CONTINUE_STMT = re.compile(r"\n[ \t]*continue[ \t]*(\n|//)")
BREAK_STMT = re.compile(r"\n[ \t]*break[ \t]*(\n|//)")
# `val hasVisibleContent = …accumulatedText…` possibly continued on the next line.
VISIBLE_FROM_ACCUM = re.compile(
    r"val\s+hasVisibleContent\s*=[^\n]*(?:\n[^\n]*)?", re.M
)
PER_TURN_CALL = "TurnVisibleContent.hasVisibleContent("


def line_of(src: str, idx: int) -> int:
    return src.count("\n", 0, idx) + 1


def check_source(src: str, label: str) -> list:
    """Returns a list of 'label:line: message' violations (empty = PASS)."""
    bad = []

    hits = [m for m in FINALIZE.finditer(src)]
    if len(hits) != 1:
        bad.append("%s:%d: expected exactly 1 turn-tail finalize "
                   "(updateAssistantMessage(assistantId, accumulatedText, false, …)), found %d"
                   % (label, line_of(src, hits[0].start()) if hits else 1, len(hits)))
    if not hits:
        return bad

    at = hits[0].start()
    br = BREAK_STMT.search(src, at)
    if br is None:
        bad.append("%s:%d: no `break` statement after the turn-tail finalize — the tail must "
                   "end the loop right after finalizing" % (label, line_of(src, at)))
    else:
        tail = src[at:br.start()]
        cm = CONTINUE_STMT.search(tail)
        if cm is not None:
            bad.append("%s:%d: a `continue` branch can bypass the turn-tail finalize "
                       "(the finalized row then rejects the continued turn's deltas)"
                       % (label, line_of(src, at + cm.start())))

    if PER_TURN_CALL not in src:
        bad.append("%s:1: the tail does not use TurnVisibleContent.hasVisibleContent(…) — the "
                   "per-turn visible-content predicate is missing" % label)
    for m in VISIBLE_FROM_ACCUM.finditer(src):
        if "loopState.accumulatedText" in m.group(0):
            bad.append("%s:%d: hasVisibleContent is derived from the run-scoped accumulator "
                       "loopState.accumulatedText (ISSUE-03)" % (label, line_of(src, m.start())))
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
                if (!hasVisibleContent && finishedCleanly) {
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

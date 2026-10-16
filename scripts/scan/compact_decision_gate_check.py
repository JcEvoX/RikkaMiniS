#!/usr/bin/env python3
"""
Compact-governance decision gate (deep-audit-1016c ISSUE-04 / ISSUE-05).

Three coupled properties of the auto-compact entry, each of which failed
silently in production before [fix/compact-pregate-exhausted-1016] /
[fix/compact-escalation-tier-contract-1016] / [fix/compact-lastfailure-pin-1016]
/ [fix/compact-silent-abort-cards-1016]:

  1. ENTRY ADMITS EVERY VERDICT `decide()` ADJUDICATES.
     `maybeAutoCompactInLoop` used to enter `decide()` only on NEEDS_COMPACT
     (plus the one-shot offload escalation). `ContextPolicy.check` answers
     EXHAUSTED the moment tokens >= window and NEEDS_COMPACT only strictly
     below it, so the single state `decide()` turns into RESCUE / EXHAUSTED
     could not reach it — the once-per-user-action rescue was behind a door
     that was shut exactly when it was needed, and an over-ceiling context fell
     straight through to the verbatim hard trim.

  2. ESCALATION DOES NOT FABRICATE A COMPACT LINE.
     `escalatedFromOffload` exists to bypass the *band* check on tiers that
     have a compact line. On an `exhaustedOnly` tier (`compactThreshold == 0`,
     contract: "too small for auto-compact; only surface .exhausted") the
     bypass used to auto-compact anyway — measured on the real classes:
     window 50 000 / tokens 45 000 / escalated -> AUTO_COMPACT while the same
     coordinate's `check()` is EXHAUSTED.

  3. THE CHAIN THROWS ITS CAPACITY EVIDENCE.
     The outer catch decides "halve and retry" purely on
     `isContextTooLargeError(e)`. When a 400 context-length failure was
     followed by an ordinary one (timeout / 5xx / unavailable), the chain threw
     the ordinary one and the halving retry never fired ([Compact] Splitting:
     0 hits in three days of device logs).

  plus the silent-mode card contract in `compactAll` (criterion 4): a card that
  can be re-emitted on every eligible attempt must be `!silent`-gated, while a
  provably-unreachable one-time invariant break is kept with an explicit
  written criterion instead of being silenced for symmetry.

Mechanical criteria:
  1. inside `maybeAutoCompactInLoop`: a single `val verdict = policy.check(...)`
     feeds `ContextCompactor.loopGateOpens(verdict, escalatedFromOffload)`, and
     the pre-fix gate shape is gone;
  2. `ContextCompactor.loopGateOpens` admits EXHAUSTED, and `decide` carries
     `tierHasNoCompactLine` in the escalation guard;
  3. the chain routes failures through `ContextCompactor.chainFailureAfter(...)`
     with `isTooLarge = ::isContextTooLargeError`, and
     `chainTerminalFailure(...)` is applied BEFORE `throw lastFailure`;
  4. each `appendSystemInfo` card in `compactAll` is either preceded by
     `if (!silent)` (between it and the previous card) or carries the explicit
     "Deliberately NOT" criterion.

Usage: python3 scripts/scan/compact_decision_gate_check.py [repo_root]
       python3 scripts/scan/compact_decision_gate_check.py --selftest
Exit:  0 = guarded, 1 = violation (file:line printed)
"""
import os
import re
import sys

MAIN = "src/android/app/src/main/java/com/rikkaminis/app"
LOOP_FILE = MAIN + "/ui/chat/ChatContextWindowExt.kt"
COMPACTOR_FILE = MAIN + "/conversation/ContextCompactor.kt"
ORCH_FILE = MAIN + "/conversation/CompactOrchestration.kt"

LOOP_BEGIN = "internal suspend fun ChatViewModel.maybeAutoCompactInLoop("
LOOP_END = "internal suspend fun ChatViewModel.awaitAutoCompactIfNeeded()"

GATE_CALL = "ContextCompactor.loopGateOpens(verdict, escalatedFromOffload)"
VERDICT_DECL = "val verdict = policy.check(lastContextTokens, contextWindow)"
OLD_GATE = "if (!inCompactBand && !escalatedFromOffload)"
TIER_GUARD = "tierHasNoCompactLine"
TIER_DECL = "val tierHasNoCompactLine = policy.compactThreshold <= 0 || policy.exhaustedOnly"
# the guard EXPRESSION, not just the identifier: dropping the predicate from the
# condition while leaving the declaration behind must still flip this red.
TIER_GUARD_EXPR = "if (!bandReached && (!escalatedFromOffload || tierHasNoCompactLine))"
RESOURCE_RE = re.compile(r"R\.string\.([A-Za-z0-9_]+)")
CHAIN_PREDICATE = "isTooLarge = ::isContextTooLargeError"
CHAIN_STEP = "ContextCompactor.chainFailureAfter("
CHAIN_TERMINAL = "ContextCompactor.chainTerminalFailure("
SILENT_GATE = "if (!silent)"
KEPT_CRITERION = "Deliberately NOT"

# resource ids of the four compactAll cards the silent contract is judged on
GATED_CARDS = [
    "sysmsg_compact_empty_summary",
    "sysmsg_compact_anchor_failed",
    "sysmsg_compact_failed",
]
KEPT_CARD = "sysmsg_compact_anchor_id_missing"

SELFTEST_MARK = "COMPACT_DECISION_GATE_SELFTEST=PASS"


def line_of(text, needle):
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else 0


def segments_between_calls(text):
    """(start, end, resource_id) for every appendSystemInfo card call site."""
    calls = []
    cursor = 0
    while True:
        idx = text.find("appendSystemInfo(", cursor)
        if idx < 0:
            break
        calls.append(idx)
        cursor = idx + 1
    out = []
    prev_end = 0
    for idx in calls:
        match = RESOURCE_RE.search(text[idx:idx + 400])
        out.append((prev_end, idx, match.group(1) if match else ""))
        prev_end = idx
    return out


def check(loop, compactor, orch):
    """Return [(criterion, message)] — empty list means guarded."""
    problems = []

    def need(text, needle, where, criterion, why):
        if needle not in text:
            problems.append((criterion, "%s: missing %r (%s)" % (where, needle, why)))

    def forbid(text, needle, where, criterion, why):
        if needle in text:
            problems.append((
                criterion,
                "%s:%d forbidden %r remains (%s)" % (where, line_of(text, needle), needle, why),
            ))

    # ── criterion 1: the loop entry admits EXHAUSTED ────────────────────
    if LOOP_BEGIN in loop and LOOP_END in loop:
        region = loop.split(LOOP_BEGIN, 1)[1].split(LOOP_END, 1)[0]
        need(region, VERDICT_DECL, LOOP_FILE, "entry-admits-exhausted",
             "the pressure verdict must be computed once and fed to the gate")
        need(region, GATE_CALL, LOOP_FILE, "entry-admits-exhausted",
             "the pre-gate must be the single-source predicate")
        forbid(region, OLD_GATE, LOOP_FILE, "entry-admits-exhausted",
               "the pre-fix gate refused EXHAUSTED, which is what decide() adjudicates")
    else:
        problems.append(("entry-admits-exhausted",
                         "%s: could not isolate %s" % (LOOP_FILE, LOOP_BEGIN)))

    # ── criterion 2: the gate itself + the escalation tier contract ─────
    if "fun loopGateOpens(" in compactor:
        gate_body = compactor.split("fun loopGateOpens(", 1)[1][:400]
        need(gate_body, "ContextPolicy.CheckResult.EXHAUSTED", COMPACTOR_FILE,
             "gate-admits-exhausted", "the gate must admit the ceiling verdict")
    else:
        problems.append(("gate-admits-exhausted",
                         "%s: fun loopGateOpens( missing" % COMPACTOR_FILE))
    need(compactor, TIER_DECL, COMPACTOR_FILE, "escalation-tier-contract",
         "the tier contract needs a single-source predicate")
    need(compactor, TIER_GUARD_EXPR, COMPACTOR_FILE, "escalation-tier-contract",
         "the escalation shortcut must consult the tier predicate")

    # ── criterion 3: the chain throws its capacity evidence ─────────────
    need(orch, CHAIN_STEP, ORCH_FILE, "chain-pins-too-large",
         "the reduction must be single-sourced in the pure function")
    need(orch, CHAIN_PREDICATE, ORCH_FILE, "chain-pins-too-large",
         "the reduction must be driven by the real classifier")
    if CHAIN_TERMINAL in orch:
        terminal_at = orch.find(CHAIN_TERMINAL)
        throw_at = orch.rfind("throw lastFailure")
        if throw_at < 0 or terminal_at > throw_at:
            problems.append((
                "chain-pins-too-large",
                "%s: %s must be applied BEFORE `throw lastFailure`" % (ORCH_FILE, CHAIN_TERMINAL),
            ))
    else:
        problems.append(("chain-pins-too-large",
                         "%s: %s missing" % (ORCH_FILE, CHAIN_TERMINAL)))

    # ── criterion 4: the silent-mode card contract ──────────────────────
    cards = segments_between_calls(orch)
    seen = {resource: (start, idx) for start, idx, resource in cards if resource}
    for resource in GATED_CARDS:
        if resource not in seen:
            problems.append(("silent-card-contract",
                             "%s: card %s not found" % (ORCH_FILE, resource)))
            continue
        start, idx = seen[resource]
        if SILENT_GATE not in orch[start:idx]:
            problems.append((
                "silent-card-contract",
                "%s:%d card %s is reachable in silent mode (no `if (!silent)` between it and the previous card)"
                % (ORCH_FILE, line_of(orch, "R.string." + resource), resource),
            ))
    if KEPT_CARD not in seen:
        problems.append(("silent-card-contract",
                         "%s: kept card %s not found" % (ORCH_FILE, KEPT_CARD)))
    else:
        start, idx = seen[KEPT_CARD]
        span = orch[start:idx]
        if SILENT_GATE in span:
            problems.append((
                "silent-card-contract",
                "%s:%d %s is now silent-gated: the written keep-criterion must be revisited, not copied"
                % (ORCH_FILE, line_of(orch, "R.string." + KEPT_CARD), KEPT_CARD),
            ))
        if KEPT_CRITERION not in span:
            problems.append((
                "silent-card-contract",
                "%s:%d %s is not silent-gated but carries no explicit keep-criterion"
                % (ORCH_FILE, line_of(orch, "R.string." + KEPT_CARD), KEPT_CARD),
            ))

    return problems


def load(root):
    out = []
    for rel in (LOOP_FILE, COMPACTOR_FILE, ORCH_FILE):
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            print("❌ compact-decision gate: %s not found (moved?)" % rel)
            return None
        with open(path, encoding="utf-8") as fh:
            out.append(fh.read())
    return out


def selftest(root):
    files = load(root)
    if files is None:
        return 1
    loop, compactor, orch = files

    def on_loop(fn):
        return lambda l, c, o: (fn(l), c, o)

    def on_compactor(fn):
        return lambda l, c, o: (l, fn(c), o)

    def on_orch(fn):
        return lambda l, c, o: (l, c, fn(o))

    arms = [
        # (name, criterion that must fire, mutator)
        (
            "pre-fix entry gate restored",
            "entry-admits-exhausted",
            on_loop(lambda s: s.replace(GATE_CALL, OLD_GATE, 1)),
        ),
        (
            "gate no longer admits EXHAUSTED",
            "gate-admits-exhausted",
            on_compactor(lambda s: s.replace(
                "verdict == ContextPolicy.CheckResult.EXHAUSTED ||\n            escalatedFromOffload",
                "escalatedFromOffload",
                1,
            )),
        ),
        (
            "escalation tier guard dropped",
            "escalation-tier-contract",
            on_compactor(lambda s: s.replace(
                TIER_GUARD_EXPR, "if (!bandReached && !escalatedFromOffload)", 1,
            )),
        ),
        (
            "chain terminal rule removed",
            "chain-pins-too-large",
            on_orch(lambda s: s.replace(
                "    lastFailure = ContextCompactor.chainTerminalFailure(lastFailure, tooLargeFailure)\n",
                "",
                1,
            )),
        ),
        (
            "silent gate removed from a repeatable card",
            "silent-card-contract",
            on_orch(lambda s: s.replace(
                "                if (!silent) {\n                    withContext(Dispatchers.Main) {\n"
                "                        appendSystemInfo(context.getString(R.string.sysmsg_compact_empty_summary), \"compact\")",
                "                if (true) {\n                    withContext(Dispatchers.Main) {\n"
                "                        appendSystemInfo(context.getString(R.string.sysmsg_compact_empty_summary), \"compact\")",
                1,
            )),
        ),
        (
            "keep-criterion deleted from the un-gated card",
            "silent-card-contract",
            on_orch(lambda s: s.replace(KEPT_CRITERION, "Silenced", 1)),
        ),
    ]

    # compliant fixture first: the real tree must be clean, otherwise every arm below is meaningless.
    baseline = check(loop, compactor, orch)
    if baseline:
        print("❌ selftest baseline is not clean: %s" % baseline)
        return 1

    failed = []
    for name, expected, mutate in arms:
        mutated = mutate(loop, compactor, orch)
        if mutated == (loop, compactor, orch):
            failed.append("%s (mutation did not apply — the anchor moved)" % name)
            continue
        criteria = [c for c, _ in check(*mutated)]
        if expected not in criteria:
            failed.append("%s (expected %r, got %s)" % (name, expected, criteria or "nothing"))

    if failed:
        print("❌ compact-decision gate --selftest:")
        for line in failed:
            print("   - %s" % line)
        return 1
    print("✅ compact-decision gate --selftest: %d arms flip exactly their criterion" % len(arms))
    print(SELFTEST_MARK)
    return 0


def main():
    if "--selftest" in sys.argv:
        root = next((a for a in sys.argv[1:] if not a.startswith("-")), ".")
        return selftest(root)
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    files = load(root)
    if files is None:
        return 1
    problems = check(*files)
    if problems:
        print("❌ compact-decision gate findings:")
        for criterion, msg in problems:
            print("   - [%s] %s" % (criterion, msg))
        return 1
    print("✅ compact-decision gate: loop entry admits EXHAUSTED, escalation keeps the tier "
          "contract, the chain throws its 400 evidence, and every compactAll card is either "
          "silent-gated or carries a written keep-criterion")
    return 0


if __name__ == "__main__":
    sys.exit(main())

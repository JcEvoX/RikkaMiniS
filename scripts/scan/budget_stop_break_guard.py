#!/usr/bin/env python3
"""
Guard: a `break` that ends a RUN must break to the run's loop, not to the
inner retry loop.

The regression this exists for (audit3 F-02, fixed 2026-10-15): the agent
loop's two execution-budget gates live INSIDE the turn body's retry loop

    turnLoop@ for (turn in 0 until maxTurnsThisRun) {
        …
        while (!collectDone) {
            if (budget.isExpired()) { … ; break }          # bare `break`
            if (!consumeProviderAttempt()) { … ; break }   # bare `break`

Those `break`s only left the retry `while`; the turn then fell through to the
"the turn's stream completed without error" tail, which
  * recorded RouteOutcome.Success for the member whose attempt budget had just
    refused it (clearing that member's cooldown / circuit state), and
  * appended an empty ASSISTANT entry to agentHistory,
while the remaining turns burned against a reducer already parked in
FINALIZING (field log 2026-09-07: "3 empty turns burned in 4ms against a
refused provider-attempt gate").

Everything compiled and every existing gate passed: none of them compares
WHICH loop a `break` exits with what the branch claims to be doing.

Criterion (mechanical — brace spans + loop nesting, no regex judgement calls):

  1. Candidate = a statement assigning `t7BudgetStopReason = "<string>"`, the
     engine's "this run is over" marker. (`= null` is the reset at run end and
     is not a candidate.)
  2. Look FORWARD inside the same brace block for the branch's first `break`.
     No `break` in that block (e.g. the branch returns an error string for the
     model to handle instead) = out of remit; such a branch does not claim to
     end the run.
  3. Count the loops enclosing that `break` (the outer turn loop counts).
     * < 2  → a bare `break` is correct: it already leaves the outermost loop.
              This is the shape of the turn-entry deadline / turn_limit gates,
              which sit at for-body depth.
     * >= 2 → the `break` MUST carry the OUTERMOST enclosing loop's label
              (e.g. `break@turnLoop`); anything else leaves the run running.
  4. If the outermost enclosing loop has no label at all, that is a violation
     too — there is nothing to break to.

Usage: python3 scripts/scan/budget_stop_break_guard.py <repo_root>
Exit:  0 = clean, 1 = violation(s)
"""
import os
import re
import sys

KOTLIN_APP = "src/android/app/src/main/java"

# `t7BudgetStopReason = "deadline_reached"`. Matched on the RAW line (string
# literals survive there), plus a guard that the code — not a comment — mentions
# the marker, so `= null` and doc comments are both excluded. Reading the raw
# line keeps unrelated lines that merely *use* the marker (e.g.
# `reason = traceObserver.t7BudgetStopReason ?: "unknown"`) out of the remit.
ASSIGN_RE = re.compile(r"t7BudgetStopReason\s*=\s*\"")
ASSIGN_MARKER = "t7BudgetStopReason"
# `while (…) {` / `for (…) {` / `turnLoop@ for (…) {` — capture the label.
LOOP_RE = re.compile(r"^\s*(?:(\w+)@\s*)?(?:while|for)\s*\(.*\)\s*\{\s*$")
BREAK_RE = re.compile(r"^\s*(break|break@\w+)\s*$")


def strip_code(line):
    """Drop string literals, char literals and trailing // comments."""
    out = []
    i = 0
    in_str = in_char = False
    while i < len(line):
        c = line[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if in_char:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_char = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "'":
            in_char = True
            i += 1
            continue
        if line.startswith("//", i):
            break
        out.append(c)
        i += 1
    return "".join(out)


def clean_lines(src):
    """Strip block comments (stateful across lines) then per-line code."""
    out = []
    in_block = False
    for raw in src:
        code = raw
        if in_block:
            if "*/" in code:
                code = code.split("*/", 1)[1]
                in_block = False
            else:
                code = ""
        while "/*" in code:
            before, rest = code.split("/*", 1)
            if "*/" in rest:
                code = before + rest.split("*/", 1)[1]
            else:
                code = before
                in_block = True
                break
        out.append(strip_code(code))
    return out


def analyse(path):
    """Return (violations, stats) for one Kotlin file."""
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read().split("\n")
    except OSError as e:
        return [], {"error": str(e)}

    clean = clean_lines(src)

    # Depth at line START, and depth produced by the line.
    start_depth = []
    delta = []
    d = 0
    for c in clean:
        start_depth.append(d)
        delta.append(c.count("{") - c.count("}"))
        d += delta[-1]
    after = [start_depth[i] + delta[i] for i in range(len(clean))]

    # Loop spans: header line -> first line whose AFTER depth returns to the
    # header's start depth. (label_line, header_line, end_line, label)
    loop_spans = []
    for i, c in enumerate(clean):
        m = LOOP_RE.match(c)
        if not m:
            continue
        end = None
        for k in range(i + 1, len(clean)):
            if after[k] == start_depth[i]:
                end = k + 1
                break
        loop_spans.append((i + 1, end if end is not None else len(clean), m.group(1)))

    violations = []
    candidates = 0
    for i, c in enumerate(clean):
        if ASSIGN_MARKER not in c or not ASSIGN_RE.search(src[i]):
            continue
        candidates += 1
        assign_line = i + 1
        # Forward inside the same brace block only: stop when the block closes.
        window_end = len(clean)
        for k in range(i + 1, len(clean)):
            if start_depth[k] < start_depth[i]:
                window_end = k
                break
        break_line = None
        break_text = None
        for k in range(i + 1, window_end):
            m = BREAK_RE.match(clean[k])
            if m:
                break_line = k + 1
                break_text = m.group(1)
                break
        if break_line is None:
            continue  # branch does not claim to end the run (e.g. returns)

        enclosing = [s for s in loop_spans if s[0] <= break_line <= s[1]]
        outermost = enclosing[0] if enclosing else None  # smallest header line

        def violation(reason):
            violations.append(
                {
                    "line": break_line,
                    "break": break_text,
                    "assign_line": assign_line,
                    "loop_count": len(enclosing),
                    "outermost": outermost,
                    "reason": reason,
                }
            )

        if len(enclosing) < 2:
            continue  # a bare break already leaves the outermost loop
        if outermost is None or outermost[2] is None:
            violation("the outermost enclosing loop carries no label")
            continue
        if break_text != "break@" + outermost[2]:
            violation(
                "the `break` must be `break@%s`, not `%s`"
                % (outermost[2], break_text)
            )

    return violations, {"candidates": candidates, "loops": len(loop_spans)}


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    app_dir = os.path.join(root, KOTLIN_APP)
    if not os.path.isdir(app_dir):
        print(f"❌ kotlin source root not found: {app_dir}")
        return 1

    files = []
    for dirpath, _, fns in os.walk(app_dir):
        for fn in fns:
            if fn.endswith(".kt"):
                files.append(os.path.join(dirpath, fn))
    files.sort()

    total_violations = 0
    total_candidates = 0
    for path in files:
        violations, stats = analyse(path)
        total_candidates += stats.get("candidates", 0)
        for v in violations:
            total_violations += 1
            rel = os.path.relpath(path, root)
            print(f"❌ {rel}:{v['line']}")
            print(
                f"   `{v['break']}` ends the branch opened by the budget stop at "
                f"line {v['assign_line']}, but it is nested in "
                f"{v['loop_count']} loops — it leaves only the innermost one."
            )
            print(f"   {v['reason']}.")
            if v["outermost"]:
                print(
                    f"   outermost enclosing loop: line {v['outermost'][0]}"
                    + (f", label `{v['outermost'][2]}`" if v["outermost"][2] else ", NO label")
                    + f" (span ends line {v['outermost'][1]})"
                )
            print("   consequence if left as-is: the turn body runs on after the")
            print("   budget stop — the success tail records a route Success for the")
            print("   refused member and appends an empty assistant entry, while the")
            print("   remaining turns burn against a reducer already in FINALIZING.")
            print("")
            print(f"   fix: `break@{v['outermost'][2] if v['outermost'] and v['outermost'][2] else 'turnLoop'}`"
                  " (and label the enclosing loop if it has none).")
            print("")

    if total_violations:
        print(f"❌ budget stop break guard: {total_violations} violation(s)")
        return 1
    print(
        f"✅ budget stop break guard: {total_candidates} budget-stop assignment(s), "
        "every run-ending `break` targets the outermost loop"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

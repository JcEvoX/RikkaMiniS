#!/usr/bin/env python3
"""
Guard: a counter reset that CLAIMS tool-call semantics must sit inside a
tool-call guard.

The regression this exists for (audit2 BUG-01, fixed 2026-10-15): the three
continuation counters of the agent loop

    loopState.lengthWallContinues
    loopState.deterministicEmptyStreak
    loopState.eofStubContinues

were reset to 0 ~640 lines ABOVE the `if (toolCalls.isEmpty()) {` branch, at a
spot every turn passes through. Their comments said "A tool-call turn clears
…", but the statements executed unconditionally — so each counter was pinned to
0 before it could reach its limit and the length-wall / deterministic-empty /
EOF-stub guards were all dead. Only `maxTurns` (default 256) still bounded the
loop. Nothing caught it: the code compiled, the comments read correctly, and
`scan.sh` had no gate that compares a statement's EXECUTION position with the
semantics its comment claims.

Criterion (mechanical, brace-depth based — no regex judgement calls):

  1. Candidate = a statement `loopState.<name> = <number>` whose immediately
     preceding contiguous comment block (blank lines and sibling numeric
     `loopState.* = <n>` resets skipped) matches `tool[- ]call` — i.e. the
     comment claims "this happens on a tool-call turn".
  2. Such a statement MUST fall strictly inside the brace span of a positive
     `toolCalls` guard: `if (toolCalls.isNotEmpty())` / `if (!toolCalls.isEmpty())`
     / `if (toolCalls.any())` / `if (toolCalls.size > 0)`, as computed from the
     header line and its closing brace.
  3. Anything else is a violation, reported with file:line and the depth of the
     nearest `if (toolCalls.isEmpty())` header (depth equality is exactly the
     old bug shape).

Usage: python3 scripts/scan/loop_counter_reset_guard.py <repo_root>
Exit:  0 = clean, 1 = violation(s)
"""
import os
import re
import sys

KOTLIN_APP = "src/android/app/src/main/java"

RESET_RE = re.compile(r"loopState\.(\w+)\s*=\s*(\d+)\s*(?://.*)?$")
GUARD_RE = re.compile(r"^\s*if\s*\((.*)\)\s*\{\s*(?://.*)?$")
EMPTY_GUARD_RE = re.compile(r"^\s*if\s*\(\s*toolCalls\.isEmpty\(\)\s*\)\s*\{")
SIBLING_RESET_RE = re.compile(r"^\s*loopState\.\w+\s*=\s*\d+\s*$")
TOOLCALL_CLAIM_RE = re.compile(r"tool[-\s]?call", re.IGNORECASE)


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


def is_positive_toolcalls_guard(cond):
    if "toolCalls" not in cond:
        return False
    if "isNotEmpty" in cond:
        return True
    if "!" in cond and "isEmpty()" in cond:
        return True
    if ".any(" in cond or "size > 0" in cond or "size >= 1" in cond:
        return True
    return False


def analyse(path):
    """Return (violations, stats) for one Kotlin file."""
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read().split("\n")
    except OSError as e:
        return [], {"error": str(e)}

    clean = clean_lines(src)
    delim = [c + " " for c in clean]  # keep index alignment with src

    # Depth at line START, and depth delta produced by the line.
    start_depth = []
    delta = []
    d = 0
    for c in clean:
        start_depth.append(d)
        delta.append(c.count("{") - c.count("}"))
        d += delta[-1]
    after = [start_depth[i] + delta[i] for i in range(len(clean))]

    # Positive tool-call guard spans: header line -> first line whose AFTER
    # depth returns to the header's start depth.
    guard_spans = []
    empty_guard_depths = []  # line_no -> depth
    for i, c in enumerate(clean):
        m = GUARD_RE.match(c)
        if not m:
            continue
        line_no = i + 1
        if EMPTY_GUARD_RE.match(c):
            empty_guard_depths.append((line_no, start_depth[i]))
            continue
        if not is_positive_toolcalls_guard(m.group(1)):
            continue
        close = None
        for k in range(i + 1, len(clean)):
            if after[k] == start_depth[i]:
                close = k + 1
                break
        guard_spans.append((line_no, close if close is not None else len(clean)))

    violations = []
    candidates = 0
    for i, c in enumerate(clean):
        m = RESET_RE.search(c.strip())
        if not m:
            continue
        # Collect the contiguous comment block above, skipping blank lines and
        # sibling numeric resets (they share one comment).
        j = i - 1
        comments = []
        while j >= 0:
            s = src[j].strip()
            if s == "":
                j -= 1
                continue
            if s.startswith("//") or s.startswith("/*") or s.startswith("*"):
                comments.append(s)
                j -= 1
                continue
            if SIBLING_RESET_RE.match(src[j]):
                j -= 1
                continue
            break
        if not comments:
            continue
        if not TOOLCALL_CLAIM_RE.search(" ".join(comments)):
            continue
        candidates += 1
        line_no = i + 1
        inside = any(h <= line_no <= c2 for (h, c2) in guard_spans)
        if inside:
            continue
        violations.append(
            {
                "line": line_no,
                "statement": src[i].strip(),
                "counter": m.group(1),
                "depth": start_depth[i],
                "empty_guard": [
                    (ln, dp) for (ln, dp) in empty_guard_depths if dp == start_depth[i]
                ],
                "guards": guard_spans,
            }
        )

    return violations, {"candidates": candidates, "guards": guard_spans}


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
            print(f"   statement: {v['statement']}")
            print(
                "   its comment block claims tool-call semantics, but the "
                f"statement is not inside a `toolCalls` positive guard "
                f"(brace depth {v['depth']})."
            )
            for ln, dp in v["empty_guard"]:
                print(
                    f"   depth {dp} is the same depth as `if (toolCalls.isEmpty())` "
                    f"at line {ln} — i.e. the statement runs EVERY turn, "
                    "including the turns the comment says it should skip."
                )
            if not v["guards"]:
                print(
                    "   no `if (toolCalls.isNotEmpty())` / `if (!toolCalls.isEmpty())` "
                    "guard exists in this file at all."
                )
            print("   fix: move the reset inside the tool-call branch, or wrap it in")
            print("        `if (toolCalls.isNotEmpty()) { … }`.")
            print("")

    if total_violations:
        print(f"❌ loop counter reset guard: {total_violations} violation(s)")
        return 1
    print(
        f"✅ loop counter reset guard: {total_candidates} tool-call-claimed reset(s), "
        "all inside a tool-call guard"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

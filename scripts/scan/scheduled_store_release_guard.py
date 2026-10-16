#!/usr/bin/env python3
"""
Guard: a persisted-task-set removal must release the runner's in-process
single-flight state — from OUTSIDE the store lock.

The regression this exists for (deep-audit-1016 B-7c, fixed 2026-10-16):
`ScheduledTasksStore.deleteTask` and `importDocument` cleared the persisted
`running` marker but never touched `ScheduledTaskRunner`'s in-process lock
(`runningTaskId` + `dispatched`). Deleting the task that was running right now
therefore left its id holding the single-flight gate until
`reapAbandonedDispatch`'s deadline+grace elapsed (~30 min for a default task),
during which every other due task degraded to `overlap` and never fired.

Two things have to hold, and both are silent when broken:

  1. PRESENCE — every function in `ScheduledTasksStore.kt` that drops tasks from
     the document must notify the runner. Deciding "drops tasks" textually:
     the body contains `filterNot { it.id ==` (drops by id) or
     `copy(running = null)` (the restore path).
  2. PLACEMENT — the notification must be a *top-level statement of that
     function*, never nested inside the store's `lock.withLock { … }` /
     `mutate(…) { … }` block. The notification takes the runner's `checkMutex`,
     which `checkAndFireToday` holds across a whole fire pass — and a fire pass
     calls back into the store (`markRunning`). Nesting the two locks inverts
     the order and deadlocks: the delete would wait for checkMutex while the
     fire pass waits for the store mutex.

Criterion (brace-depth based, no regex judgement calls): for each function
detected by rule 1, require exactly one `ScheduledTaskRunner.onTasksRemoved(`
call, at depth 1 of that function's body. Plus a file-wide rule: no
`ScheduledTaskRunner.` reference may appear while a `lock.withLock {` block is
open.

Usage: python3 scripts/scan/scheduled_store_release_guard.py <repo_root>
Exit:  0 = clean, 1 = violation(s) / ROT
"""
import os
import re
import sys

STORE_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/scheduled/ScheduledTasksStore.kt"
)

RELEASE_CALL = "ScheduledTaskRunner.onTasksRemoved("
FUN_RE = re.compile(r"^    (?:[a-z]+ )*fun (\w+) *\(", re.MULTILINE)
REMOVAL_RES = (
    re.compile(r"filterNot *\{ *it\.id *=="),
    re.compile(r"copy\(running *= *null\)"),
)
LOCK_RE = re.compile(r"\block\.withLock *\{")


def strip_code(src):
    """Blank out comments and string literals, preserving offsets and newlines."""
    out = list(src)
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            for _ in range(2):
                if i < n and src[i] != "\n":
                    out[i] = " "
                i += 1
        elif c == '"':
            if src[i:i + 3] == '"""':
                for k in range(3):
                    out[i + k] = " "
                i += 3
                while i < n and src[i:i + 3] != '"""':
                    if src[i] != "\n":
                        out[i] = " "
                    i += 1
                for _ in range(3):
                    if i < n and src[i] != "\n":
                        out[i] = " "
                    i += 1
            else:
                out[i] = " "
                i += 1
                while i < n and src[i] != '"':
                    if src[i] == "\\":
                        out[i] = " "
                        i += 1
                    if i < n and src[i] != "\n":
                        out[i] = " "
                    i += 1
                if i < n:
                    out[i] = " "
                    i += 1
        else:
            i += 1
    return "".join(out)


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def function_spans(scan):
    """[(name, body_start_index, body_end_index_exclusive, start_line)] for each fun."""
    spans = []
    for m in FUN_RE.finditer(scan):
        # The body opens at the first '{' after the signature.
        open_idx = scan.find("{", m.end())
        if open_idx < 0:
            continue
        depth = 0
        close_idx = None
        for j in range(open_idx, len(scan)):
            ch = scan[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    close_idx = j
                    break
        if close_idx is None:
            continue
        spans.append((m.group(1), open_idx, close_idx + 1, line_of(scan, m.start())))
    return spans


def depth_at(scan, start, idx):
    """Brace depth inside the function body at index `idx` (body brace = depth 1)."""
    return scan.count("{", start + 1, idx) - scan.count("}", start + 1, idx) + 1


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    path = os.path.join(root, STORE_REL)
    findings = []
    rot = []

    if not os.path.isfile(path):
        print("ROT: %s not found — the gate cannot see the shape it guards." % STORE_REL)
        print("     If the store moved, teach this gate the new path.")
        return 1

    src = open(path, encoding="utf-8").read()
    scan = strip_code(src)
    spans = function_spans(scan)

    removal_fns = []
    for name, start, end, line in spans:
        body = scan[start:end]
        if any(r.search(body) for r in REMOVAL_RES):
            removal_fns.append((name, start, end, line))

    if not spans:
        rot.append("no `fun` at object-member indentation was parsed")
    if not removal_fns:
        rot.append(
            "no task-removal function was detected (expected deleteTask / "
            "importDocument shapes: `filterNot { it.id ==` or `copy(running = null)`)"
        )

    for name, start, end, line in removal_fns:
        body = scan[start:end]
        hits = [start + m.start() for m in re.finditer(re.escape(RELEASE_CALL), body)]
        if not hits:
            findings.append(
                "%s:%d: [%s] drops tasks from the persisted document but never calls "
                "%s — the runner's in-process single-flight lock survives the removal "
                "(B-7c)" % (STORE_REL, line, name, RELEASE_CALL.rstrip("("))
            )
            continue
        if len(hits) > 1:
            findings.append(
                "%s:%d: [%s] calls %s %d times — exactly one release per removal"
                % (STORE_REL, line, name, RELEASE_CALL.rstrip("("), len(hits))
            )
        for hit in hits:
            d = depth_at(scan, start, hit)
            if d != 1:
                findings.append(
                    "%s:%d: [%s] %s sits at brace depth %d (nested inside the store "
                    "lock) — LOCK ORDER: the release takes the runner's checkMutex, "
                    "which a fire pass holds while calling back into this store "
                    "(`markRunning`); nesting deadlocks. Call it after the lock."
                    % (STORE_REL, line_of(scan, hit), name, RELEASE_CALL.rstrip("("), d)
                )

    # File-wide: no runner reference while a store lock block is open.
    depth = 0
    lock_body_depth = None
    for lineno, raw in enumerate(scan.split("\n"), start=1):
        if lock_body_depth is None and LOCK_RE.search(raw):
            lock_body_depth = depth + 1
        if "ScheduledTaskRunner." in raw and lock_body_depth is not None:
            findings.append(
                "%s:%d: ScheduledTaskRunner referenced inside a `lock.withLock` block "
                "(opened at body depth %d) — LOCK ORDER (see "
                "ScheduledTaskRunner.onTasksRemoved)"
                % (STORE_REL, lineno, lock_body_depth)
            )
        depth += raw.count("{") - raw.count("}")
        if lock_body_depth is not None and depth < lock_body_depth:
            lock_body_depth = None

    if rot:
        print("ROT: %s" % STORE_REL)
        for r in rot:
            print("  - %s" % r)
        print("  The gate fails loudly instead of passing vacuously. If the store was")
        print("  refactored, update the detection in this script.")
        return 1

    if findings:
        for f in findings:
            print(f)
        print("\n%d violation(s)." % len(findings))
        return 1

    print(
        "OK: %d removal function(s) release the runner (%s) at top level."
        % (len(removal_fns), ", ".join(n for n, _, _, _ in removal_fns))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

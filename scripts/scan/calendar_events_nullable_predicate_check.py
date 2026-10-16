#!/usr/bin/env python3
"""[da1017-D1] Gate: never filter CalendarContract.Events on a NULL-able column.

Mechanism the gate protects (measured on-device 2026-10-16, Android 15):
the Calendar provider stores recurring events as RRULE + DURATION and leaves
`Events.DTEND` NULL — it rejects both together (CalendarProvider2.validateEventData:
"Cannot have both DTEND and DURATION in an event"). A provider query whose
selection compares DTEND (`dtend < ?`, `dtend > ?`, `dtend = ?`) is therefore
never true for those rows, and the caller silently reports the *opposite* of the
truth (freebusy answered "free" for a window holding a weekly occurrence).

Two rules:
  A. No SQL predicate (a string literal that couples an Events.DTEND reference
     with a comparison operator and a bind placeholder) may exist anywhere in
     src/android/app/src/main/java, unless the same literal guards NULL
     (`IS NULL`). Predicates must be built on CalendarContract.Instances
     (BEGIN/END), which the provider expands for recurring rows.
  B. The `freebusy` code path of the calendar offload handler must read the
     instances window (Instances.CONTENT_URI + the shared selection helper) and
     must not fall back to Events.

Run from the repo root: `python3 scripts/scan/calendar_events_nullable_predicate_check.py`
exit 0 = PASS, exit 1 = FAIL (violations printed as path:line).

`--selftest` builds violating + compliant fixtures and asserts the gate's own
teeth (FIX-1015 lesson: a gate that cannot fail is worse than no gate).
"""
import argparse
import os
import re
import shutil
import sys
import tempfile

MAIN_ROOT = os.path.join("src", "android", "app", "src", "main", "java")
HANDLER_REL = os.path.join(
    MAIN_ROOT, "com", "rikkaminis", "app", "sandbox", "offload", "CalendarOffloadHandler.kt"
)

# String literals: single-line "..." (no newline) and """...""" blocks.
LITERAL_RE = re.compile(r'"""(?:.|\n)*?"""|"(?:[^"\\\n]|\\.)*"')
COMPARISON_RE = re.compile(r"[<>=!]+\s*[?$]")
NULL_GUARD_RE = re.compile(r"IS\s+NULL", re.IGNORECASE)

RULE_A_CODE = 'predicate-on-nullable-column'


def iter_kotlin_files(root):
    for dirpath, _dirnames, filenames in os.walk(os.path.join(root, MAIN_ROOT)):
        for name in sorted(filenames):
            if name.endswith(".kt"):
                yield os.path.join(dirpath, name)


def line_of(src, index):
    return src.count("\n", 0, index) + 1


def rule_a(src):
    """Predicates shaped on Events.DTEND (outside a NULL guard)."""
    hits = []
    for m in LITERAL_RE.finditer(src):
        lit = m.group(0)
        if "CalendarContract.Events.DTEND" not in lit:
            continue
        if NULL_GUARD_RE.search(lit):
            continue
        if not COMPARISON_RE.search(lit):
            continue
        hits.append((line_of(src, m.start()), lit.strip()[:120]))
    return hits


def member_slice(src, marker):
    """Lines of the member declared at [marker], up to the next sibling member."""
    lines = src.split("\n")
    start = next((i for i, l in enumerate(lines) if marker in l), None)
    if start is None:
        return None
    for i in range(start + 1, len(lines)):
        line = lines[i]
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if (
            0 < indent <= 4
            and stripped
            and not stripped.startswith("//")
            and not stripped.startswith("*")
            and re.match(r"^(private|internal|public|protected|fun|val|var|@)", stripped)
        ):
            return "\n".join(lines[start:i])
    return "\n".join(lines[start:])


def rule_b(src):
    """The freebusy path must read instances, never events."""
    body = member_slice(src, "private fun doFreebusy(")
    if body is None:
        return []
    problems = []
    if "CalendarContract.Instances.CONTENT_URI" not in body:
        problems.append("freebusy does not query CalendarContract.Instances.CONTENT_URI")
    if "instancesWindowSelection(" not in body:
        problems.append("freebusy does not use the shared instances window selection")
    if "CalendarContract.Events.CONTENT_URI" in body:
        problems.append("freebusy queries CalendarContract.Events.CONTENT_URI")
    if "CalendarContract.Events.DTEND" in body:
        problems.append("freebusy filters on CalendarContract.Events.DTEND (NULL for recurring events)")
    return problems


def run(root, verbose=True):
    failures = []
    for path in iter_kotlin_files(root):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        rel = os.path.relpath(path, root)
        for lineno, snippet in rule_a(src):
            failures.append(f"{rel}:{lineno}: [{RULE_A_CODE}] Events.DTEND compared without a NULL guard: {snippet}")
        if os.path.normpath(rel) == os.path.normpath(HANDLER_REL):
            for problem in rule_b(src):
                failures.append(f"{rel}: [{RULE_A_CODE}] freebusy path: {problem}")
    if verbose:
        for line in failures:
            print(line)
    return failures


# ── selftest ────────────────────────────────────────────────────────────────

VIOLATING_HANDLER = '''
package x

class CalendarOffloadHandler {
    private fun doFreebusy() {
        val projection = arrayOf(CalendarContract.Events.TITLE, CalendarContract.Events.DTSTART)
        val selection = "${CalendarContract.Events.DTSTART} < ? AND ${CalendarContract.Events.DTEND} > ?"
        context.contentResolver.query(CalendarContract.Events.CONTENT_URI, projection, selection, null, null)
    }
}
'''

COMPLIANT_HANDLER = '''
package x

class CalendarOffloadHandler {
    private fun doFreebusy() {
        val projection = freebusyInstanceProjection()
        val (selection, selArgs) = instancesWindowSelection(startMs, endMs)
        context.contentResolver.query(CalendarContract.Instances.CONTENT_URI, projection, selection, selArgs, sort)
    }
}
'''

NULL_GUARDED_HANDLER = '''
package x

class OtherHandler {
    private fun doFreebusy() {
        val selection = "${CalendarContract.Events.DTEND} IS NULL OR ${CalendarContract.Events.DTEND} > ?"
        val write = arrayOf(CalendarContract.Events.DTEND)
        val values = ContentValues()
        values.put(CalendarContract.Events.DTEND, 1L)
    }
}
'''

OTHER_SLUG_HANDLER = '''
package x

class OtherHandler {
    private fun doFreebusy() {
        val selection = "${CalendarContract.Events.DTEND} > ?"
    }
}
'''

COMPLIANT_WRITER = '''
package x

class OtherHandler {
    fun update() {
        val values = ContentValues()
        values.put(CalendarContract.Events.DTEND, 1L)
        val projection = arrayOf(CalendarContract.Events.DTEND)
    }
}
'''


def write_fixture(root, name, body, handler_rel=True):
    rel = HANDLER_REL if handler_rel else os.path.join(MAIN_ROOT, "com", "rikkaminis", "app", "other", name)
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def selftest():
    cases = [
        ("violating handler (events predicate + events query)", VIOLATING_HANDLER, True, 1),
        ("compliant handler (instances)", COMPLIANT_HANDLER, True, 0),
        ("null-guarded predicate + write-only DTEND", NULL_GUARDED_HANDLER, False, 0),
        ("events predicate in another file", OTHER_SLUG_HANDLER, False, 1),
        ("write-only DTEND in another file", COMPLIANT_WRITER, False, 0),
    ]
    ok = True
    for label, body, handler_rel, expect_failures in cases:
        root = tempfile.mkdtemp(prefix="calgate-")
        try:
            write_fixture(root, "CalendarOffloadHandler.kt", body, handler_rel=handler_rel)
            failures = run(root, verbose=False)
            passed = (len(failures) > 0) == (expect_failures > 0)
            print(f"[{'PASS' if passed else 'FAIL'}] selftest: {label} "
                  f"(expected {'red' if expect_failures else 'green'}, got {len(failures)} violation(s))")
            for f in failures:
                print(f"        {f}")
            ok = ok and passed
        finally:
            shutil.rmtree(root, ignore_errors=True)
    print("selftest: " + ("ALL ARMS MATCH" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    failures = run(args.root)
    if failures:
        print(f"calendar_events_nullable_predicate_check: FAIL ({len(failures)} violation(s))")
        return 1
    print("calendar_events_nullable_predicate_check: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

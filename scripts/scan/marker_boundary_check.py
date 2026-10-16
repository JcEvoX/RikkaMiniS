#!/usr/bin/env python3
"""
Marker-boundary completion guard — `internalScanMarker` may only report a HIT
after the full `__MINIS_DONE_<marker>_EXIT_<n>__` has been parsed, and the
pending-marker window it withholds must be explicitly bounded.

Bug class ([deep-audit-1017] ISSUE-09, live since [fix/marker-chunk-boundary]):
the scan hit on the bare `__MINIS_DONE_<m>_EXIT_` PREFIX and took the exit code
from a regex that requires the closing `__`. When a 4096-char read boundary fell
between `_EXIT_` and its digits, the command completed with `exitCode = -1` —
the "shell died" code, on which ExecutionCoordinator rebuilds the shell and
RE-RUNS the command (non-idempotent side effects twice) — and every byte of the
marker line still in flight was dropped (`pendingCallback = null; tail = ""`).
The previous fix in the same file only taught the loop to carry a split PREFIX
(`[fix/marker-chunk-boundary]`), so the exit-code half of the same marker stayed
exposed. A pure-function regression here is silent: nothing crashes, the command
just reports the wrong result.

Criterion (mechanical):
  1. inside `internalScanMarker`, an `if (exitCode >= 0)` guard must precede the
     first `hit = true` — a hit without a parsed exit code is the bug;
  2. a bounded window: `internal const val MAX_PENDING_MARKER_CHARS = <n>` must
     exist with 32 <= n <= 256 (the prefix is 27-33 chars for an 8-char marker;
     anything larger is not a cap) and must be referenced by the function.

Usage: python3 scripts/scan/marker_boundary_check.py [repo_root]
       python3 scripts/scan/marker_boundary_check.py --selftest
Exit:  0 = guarded, 1 = violation (file:line printed)
"""
import os
import re
import sys

TARGET = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/PersistentShell.kt"
FUNC = "internalScanMarker"
CAP_CONST = "MAX_PENDING_MARKER_CHARS"
CAP_DECL = re.compile(r"internal const val %s\s*=\s*(\d+)" % CAP_CONST)
GUARD = re.compile(r"if\s*\(\s*exitCode\s*>=\s*0\s*\)")
HIT = re.compile(r"hit\s*=\s*true")
MIN_CAP = 32
MAX_CAP = 256


def function_body(text, name):
    """Source of the top-level `internal fun <name>(` up to its col-0 closing brace."""
    m = re.search(r"^internal fun %s\(" % re.escape(name), text, re.M)
    if m is None:
        return None, 0
    start = m.start()
    line_no = text[:start].count("\n") + 1
    tail = text[start:]
    for i, line in enumerate(tail.split("\n")):
        if i > 0 and line == "}":
            return "\n".join(tail.split("\n")[: i + 1]), line_no
    return None, line_no


def check_source(text):
    """Return [(line_or_None, message)] for one source text."""
    findings = []
    body, body_line = function_body(text, FUNC)
    if body is None:
        return [(None, "`%s` not found — keep this gate honest (renamed or removed?)" % FUNC)]

    guard = GUARD.search(body)
    hits = [m for m in HIT.finditer(body)]
    if guard is None:
        findings.append((body_line, "%s has no `if (exitCode >= 0)` guard — a bare marker "
                         "prefix would report completion with exitCode -1 (\"shell died\")" % FUNC))
    if hits and (guard is None or guard.start() > hits[0].start()):
        findings.append((body_line + body[: hits[0].start()].count("\n"),
                         "%s reports `hit = true` before any exit-code check" % FUNC))

    decl = CAP_DECL.search(text)
    span = tuple(body.split("\n")[0:1])
    if decl is None:
        findings.append((None, "no `internal const val %s = <n>` — the pending marker "
                         "window may grow without bound" % CAP_CONST))
    else:
        value = int(decl.group(1))
        if not (MIN_CAP <= value <= MAX_CAP):
            findings.append((None, "%s = %d is outside %d..%d — a cap that large does not "
                             "bound anything" % (CAP_CONST, value, MIN_CAP, MAX_CAP)))
    del span
    if CAP_CONST not in body:
        findings.append((body_line, "%s does not use %s — the pending window is unbounded "
                         "in the hit path" % (FUNC, CAP_CONST)))
    return findings


OK_FIXTURE = """
package p
internal const val MAX_PENDING_MARKER_CHARS = 64
internal fun internalScanMarker(tail: String, text: String, marker: String): MarkerScanResult {
    val idx = combined.indexOf(markerPattern)
    if (idx >= 0) {
        val exitCode = internalParseMinisExitCode(combined.substring(idx), marker)
        if (exitCode >= 0) {
            return MarkerScanResult(
                hit = true,
                beforeMarker = "",
                exitCode = exitCode,
                keptTail = "",
            )
        }
        val pending = combined.substring(idx)
        if (pending.length <= MAX_PENDING_MARKER_CHARS) {
            return MarkerScanResult(hit = false, beforeMarker = "", exitCode = -1, keptTail = pending)
        }
    }
    return MarkerScanResult(hit = false, beforeMarker = combined, exitCode = -1, keptTail = "")
}
"""

PRE_FIX_FIXTURE = OK_FIXTURE.replace(
    """        val exitCode = internalParseMinisExitCode(combined.substring(idx), marker)
        if (exitCode >= 0) {
""",
    "",
).replace(
    """            return MarkerScanResult(
                hit = true,
                beforeMarker = "",
                exitCode = exitCode,
                keptTail = "",
            )
        }
""",
    "        return MarkerScanResult(hit = true, beforeMarker = \"\", exitCode = -1, keptTail = \"\")\n",
).replace("        if (pending.length <= MAX_PENDING_MARKER_CHARS) {\n", "").replace(
    "internal const val MAX_PENDING_MARKER_CHARS = 64\n", ""
)

NO_CAP_FIXTURE = OK_FIXTURE.replace("internal const val MAX_PENDING_MARKER_CHARS = 64\n", "")
CAP_UNUSED_FIXTURE = OK_FIXTURE.replace(
    "if (pending.length <= MAX_PENDING_MARKER_CHARS) {", "if (pending.length <= 64) {"
)
CAP_TOO_BIG_FIXTURE = OK_FIXTURE.replace(
    "MAX_PENDING_MARKER_CHARS = 64", "MAX_PENDING_MARKER_CHARS = 100000"
)

SELFTEST = [
    ("compliant source is green", OK_FIXTURE, 0, []),
    ("pre-fix shape is red", PRE_FIX_FIXTURE, 4,
     ["guard", "before any exit-code", "no `internal const val", "does not use"]),
    ("missing cap declaration is red", NO_CAP_FIXTURE, 1, ["no `internal const val"]),
    ("unused cap constant is red", CAP_UNUSED_FIXTURE, 1, ["does not use"]),
    ("oversized cap is red", CAP_TOO_BIG_FIXTURE, 1, ["outside 32..256"]),
]


def selftest():
    bad = 0
    for name, text, want_count, needles in SELFTEST:
        findings = check_source(text)
        messages = "\n".join(m for _, m in findings)
        ok = len(findings) == want_count and all(n in messages for n in needles)
        if not ok:
            bad += 1
            print("  [FAIL] %s: got %d findings, wanted %d" % (name, len(findings), want_count))
            for line, msg in findings:
                print("         %s" % msg)
        else:
            print("  [OK] %s (%d findings)" % (name, len(findings)))
    # A fixture must never be green for the wrong reason.
    if not check_source(OK_FIXTURE):
        print("  [OK] unaffected fixture stays green")
    else:
        bad += 1
        print("  [FAIL] compliant fixture reported findings")
    print("SELFTEST=%s" % ("PASS" if bad == 0 else "FAIL"))
    return 1 if bad else 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    path = os.path.join(root, TARGET)
    if not os.path.exists(path):
        print("❌ marker-boundary guard: %s not found (moved?)" % TARGET)
        return 1
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    findings = check_source(text)
    if findings:
        print("❌ marker-boundary guard findings:")
        for line, msg in findings:
            print("   - %s%s" % ("%s:%d " % (TARGET, line) if line else "", msg))
        return 1
    print("✅ marker-boundary guard: `%s` requires a parsed exit code before a hit, "
          "pending window capped by %s" % (FUNC, CAP_CONST))
    return 0


if __name__ == "__main__":
    sys.exit(main())

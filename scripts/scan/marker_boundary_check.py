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
  0. every `MarkerScanResult(…)` whose hit argument is `true` — named
     `hit = true` OR the FIRST POSITIONAL argument — must sit inside an
     `if (<v> >= 0)` guard, `<v>` must be declared as
     `val <v> = internalParseMinisExitCode(…)`, and the exitCode handed to the
     result must be that same `<v>`. Otherwise the hit is backed by a constant /
     a -1 / an unrelated variable and a split marker still reports completion.
  1. inside `internalScanMarker`, an `if (exitCode >= 0)` guard must precede the
     first `hit = true` — a hit without a parsed exit code is the bug;
  2. a bounded window: `internal const val MAX_PENDING_MARKER_CHARS = <n>` must
     exist with 32 <= n <= 256 (the prefix is 27-33 chars for an 8-char marker;
     anything larger is not a cap), must be referenced by the function as a real
     BOUND, and that bound must not be weakened by a top-level `||` (a
     `if (len <= CAP || true)` reads as bounded and is not).

Wide-sweep hardening (meta-wide 06 / wide-1024, three gate-blind witnesses):
  W1 `val exitCode = 0` + the parse moved into the result ⇒ the guard tested a
     constant (dataflow). Criterion 0 binds guard ⇄ parse ⇄ result.
  W2 `if (pending.length <= MAX_PENDING_MARKER_CHARS || true)` ⇒ substring
     presence was the whole check (dataflow). Criterion 2 rejects a weakened
     bound.
  W3 `if (true) return MarkerScanResult(true, …)` after the guard block ⇒
     positional construction dodged the `hit = true` regex (anchor).
     Criterion 0 also accepts positional arguments, so there is no second
     spelling to hide in.

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
GUARD = re.compile(r"if\s*\(\s*exitCode\s*>=\s*0\s*\)")  # legacy literal spelling
HIT = re.compile(r"hit\s*=\s*true")
PARSE_HELPER = "internalParseMinisExitCode("
MIN_CAP = 32
MAX_CAP = 256

IF_RE = re.compile(r"(?<![\w.])if\s*\(")
CONST_TRUE_GUARD = re.compile(r"^\s*(?:true|1\s*==\s*1)\s*$")


# ── minimal Kotlin lexer ──────────────────────────────────────────────────────
def mask(src):
    """Blank out comment bodies and string/char literal CONTENTS in place.

    Newlines and offsets are preserved so line numbers stay exact and every
    structural scan sees code only. Strings are scanned BEFORE comments, so a
    `"video/*"` literal can never open a block comment (the failure mode that
    once hid 67 lines of a real handler from a gate).
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


def brace_block(text, open_idx):
    """(body_text, end_idx) for the `{…}` starting at/after `open_idx`."""
    j = text.find("{", open_idx)
    if j < 0:
        return None, None
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[j:k + 1], k + 1
    return None, None


def paren_inner(text, open_idx):
    """(inner_text, end_idx) for the `(…)` starting at/after `open_idx`."""
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


def guards(text):
    """[(cond, body_start, body_end)] for every `if (…)` with a bounded body."""
    out = []
    for m in IF_RE.finditer(text):
        cond, after = paren_inner(text, m.end() - 1)
        if cond is None:
            continue
        k = after
        while k < len(text) and text[k] in " \t\r\n":
            k += 1
        if k < len(text) and text[k] == "{":
            blk, end = brace_block(text, k)
            if blk is None:
                continue
            out.append((cond, k, end))
        else:
            # brace-less: the guarded statement is the rest of this statement
            depth = 0
            end = len(text)
            for k2 in range(k, len(text)):
                ch = text[k2]
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    if depth == 0:
                        end = k2
                        break
                    depth -= 1
                elif ch == "\n" and depth == 0:
                    end = k2
                    break
            out.append((cond, k, end))
    return out


def enclosing(gs, pos):
    return [g for g in gs if g[1] <= pos < g[2]]


def function_span(text, name):
    """(start, end) offsets of the top-level `internal fun <name>(` body."""
    m = re.search(r"(?m)^internal fun %s\(" % re.escape(name), text)
    if m is None:
        return None, None, 0
    line_no = text[:m.start()].count("\n") + 1
    nl = text.find("\n}", m.start())
    if nl < 0:
        return None, None, line_no
    return m.start(), nl + 2, line_no


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


NAMED = re.compile(r"^([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", re.S)


def marker_sites(body):
    """[(pos, hit_expr, exit_expr)] for every `MarkerScanResult(…)` construction.

    Named and positional arguments are both resolved, so a positional
    `MarkerScanResult(true, …, -1, …)` cannot dodge the hit/hit-code pairing.
    """
    sites = []
    for m in re.finditer(r"MarkerScanResult\s*\(", body):
        inner, _ = paren_inner(body, m.end() - 1)
        if inner is None:
            continue
        args = split_args(inner)
        named, positional = {}, []
        for a in args:
            nm = NAMED.match(a)
            if nm:
                named[nm.group(1)] = nm.group(2).strip()
            else:
                positional.append(a)
        hit = named.get("hit", positional[0] if len(positional) > 0 else "")
        exit_expr = named.get("exitCode", positional[2] if len(positional) > 2 else "")
        sites.append((m.start(), hit.strip(), exit_expr.strip()))
    return sites


def check_source(text):
    """Return [(line_or_None, message)] for one source text."""
    findings = []
    code = mask(text)
    start, end, body_line = function_span(code, FUNC)
    body = code[start:end] if start is not None else None
    if body is None:
        return [(None, "`%s` not found — keep this gate honest (renamed or removed?)" % FUNC)]

    # ── 1. legacy ordering rule: an exit-code guard must precede the first hit
    guard = GUARD.search(body)
    hits = [m for m in HIT.finditer(body)]
    if guard is None:
        findings.append((body_line, "%s has no `if (exitCode >= 0)` guard — a bare marker "
                         "prefix would report completion with exitCode -1 (\"shell died\")" % FUNC))
    if hits and (guard is None or guard.start() > hits[0].start()):
        findings.append((body_line + body[: hits[0].start()].count("\n"),
                         "%s reports `hit = true` before any exit-code check" % FUNC))

    # ── 0. every hit must be backed by the PARSED code, in every spelling
    gs = guards(body)
    for pos, hit_expr, exit_expr in marker_sites(body):
        if hit_expr != "true":
            continue
        line = body_line + body[:pos].count("\n")
        inner = enclosing(gs, pos)
        g = None
        for cond, b0, b1 in reversed(inner):
            if re.match(r"^\s*([A-Za-z_]\w*)\s*>=\s*0\s*$", cond):
                g = (cond, b0, b1)
                break
        if g is None:
            findings.append((line, "%s reports a hit (hit = true / first positional `true`) "
                             "outside any `if (<parsedExitCode> >= 0)` guard — a split marker "
                             "then completes the command with exitCode -1 (\"shell died\")" % FUNC))
            continue
        var = re.match(r"^\s*([A-Za-z_]\w*)\s*>=", g[0]).group(1)
        decl = re.search(r"(?m)^[ \t]*(?:val|var)\s+%s\s*=\s*%s" % (re.escape(var),
                                                                    re.escape(PARSE_HELPER)), body)
        if decl is None or decl.start() > pos:
            findings.append((line, "the exit-code guard tests '%s', but no `val %s = %s…)` "
                             "precedes the hit — the guard is on a constant / unrelated value, "
                             "not on the parsed marker exit code" % (var, var, PARSE_HELPER)))
        if exit_expr != var:
            findings.append((line, "the hit hands exitCode = '%s' to MarkerScanResult while the "
                             "guard validated '%s' — the value that decides completion must be the "
                             "value that is reported" % (exit_expr or "<missing>", var)))

    # ── 2. the cap must be a real bound at its use site
    decl = CAP_DECL.search(code)
    if decl is None:
        findings.append((None, "no `internal const val %s = <n>` — the pending marker "
                         "window may grow without bound" % CAP_CONST))
    else:
        value = int(decl.group(1))
        if not (MIN_CAP <= value <= MAX_CAP):
            findings.append((None, "%s = %d is outside %d..%d — a cap that large does not "
                             "bound anything" % (CAP_CONST, value, MIN_CAP, MAX_CAP)))
    if CAP_CONST not in body:
        findings.append((body_line, "%s does not use %s — the pending window is unbounded "
                         "in the hit path" % (FUNC, CAP_CONST)))
    else:
        used = False
        for cond, b0, b1 in gs:
            if CAP_CONST not in cond:
                continue
            if "||" in cond:
                findings.append((body_line + body[:b0].count("\n"),
                                 "the %s bound is weakened by a disjunction (`||`) — "
                                 "`if (… <= %s || true)` can never withhold anything, so a "
                                 "split-marker prefix accumulates without limit"
                                 % (CAP_CONST, CAP_CONST)))
                continue
            if CONST_TRUE_GUARD.match(cond):
                continue
            if re.search(r"%s\s*(<=|>=|<|>)" % re.escape(CAP_CONST), cond) or \
               re.search(r"(<=|>=|<|>)\s*%s" % re.escape(CAP_CONST), cond):
                used = True
        if not used:
            findings.append((body_line, "%s does not bound anything at its use site — the "
                             "pending window is still unbounded in the hit path" % CAP_CONST))
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

# ── wide-1024 witnesses (each one used to pass this gate unchanged) ───────────

# W1: the guard tests a constant, the parse is smuggled into the result.
W1_CONST_GUARD_FIXTURE = OK_FIXTURE.replace(
    """        val exitCode = internalParseMinisExitCode(combined.substring(idx), marker)
        if (exitCode >= 0) {
            return MarkerScanResult(
                hit = true,
                beforeMarker = "",
                exitCode = exitCode,
                keptTail = "",
            )
        }""",
    """        val exitCode = 0
        if (exitCode >= 0) {
            return MarkerScanResult(
                hit = true,
                beforeMarker = "",
                exitCode = internalParseMinisExitCode(combined.substring(idx), marker),
                keptTail = "",
            )
        }""",
)

# W2: the cap comparison is short-circuited into a tautology.
W2_WEAKENED_CAP_FIXTURE = OK_FIXTURE.replace(
    "if (pending.length <= MAX_PENDING_MARKER_CHARS) {",
    "if (pending.length <= MAX_PENDING_MARKER_CHARS || true) {",
)

# W3: a positional hit after the guard block — no `hit = true` to grep for.
W3_POSITIONAL_HIT_FIXTURE = OK_FIXTURE.replace(
    """        val pending = combined.substring(idx)
        if (pending.length <= MAX_PENDING_MARKER_CHARS) {""",
    """        if (true) return MarkerScanResult(true, combined.substring(0, idx), -1, "")
        val pending = combined.substring(idx)
        if (pending.length <= MAX_PENDING_MARKER_CHARS) {""",
)

SELFTEST = [
    ("compliant source is green", OK_FIXTURE, 0, []),
    ("pre-fix shape is red", PRE_FIX_FIXTURE, 6,
     ["no `if (exitCode >= 0)` guard", "before any exit-code", "no `internal const val",
      "does not use", "guard tests 'idx'"]),
    ("missing cap declaration is red", NO_CAP_FIXTURE, 1, ["no `internal const val"]),
    ("unused cap constant is red", CAP_UNUSED_FIXTURE, 1, ["does not use"]),
    ("oversized cap is red", CAP_TOO_BIG_FIXTURE, 1, ["outside 32..256"]),
    ("W1 constant guard is red", W1_CONST_GUARD_FIXTURE, 2,
     ["no `val exitCode = internalParseMinisExitCode", "hands exitCode"]),
    ("W2 weakened cap is red", W2_WEAKENED_CAP_FIXTURE, 2,
     ["weakened by a disjunction", "does not bound anything"]),
    ("W3 positional hit is red", W3_POSITIONAL_HIT_FIXTURE, 2,
     ["guard tests 'idx'", "hands exitCode = '-1'"]),
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

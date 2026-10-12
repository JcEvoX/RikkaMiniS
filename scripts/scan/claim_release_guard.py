#!/usr/bin/env python3
"""Claim/release ownership guard — RikkaMinis CI scan gate.

## Why this gate exists

2026-10-12, for real (session b4c466fd): the user switched model while a slow
turn was still in flight. The predecessor's `finally` landed INSIDE the new
claim's window — after `_isStreaming = true` but before the new claim's marker
was published — read the OLD marker, passed both halves of the stale-job gate
and flipped `_isStreaming` off. The new turn kept streaming with no Stop
button. Fixed in 41f024e7 by minting an identity token BEFORE the flip
(`streamingClaimToken = Any()`), so a stale release can never pass
`claimToken === streamingClaimToken`.

That was the third generation of the same family:
  - 2026-09-06 17:31 (see ChatTurnPersistence KDoc): old finally cleared the
    new turn's flag — before any ownership gate existed;
  - retryFromMessage / retryLast got the guard next; switchModelAndRerun did
    not (the "fixed one side, missed the other" step);
  - 2026-10-12 14:40: the gate existed everywhere, but the marker it compares
    was published a few statements AFTER `_isStreaming = true` — the window
    the gate was supposed to cover was open from the flip.

The class is "stale release of a shared run-state flag": a flag with multiple
claimants (send / retry / resume / switch-model) whose teardown paths must
prove ownership before releasing it. Timing cannot be seen statically, but the
two mechanical shapes every instance has exhibited CAN be:

  A. SIBLING-GATE INCONSISTENCY — within one flag's release-site family, some
     releases are guarded by an ownership comparison (epoch / token / job
     identity) and others are not. Ungated siblings are either a miss or a
     deliberate choice; both deserve a `release-ok:` marker. This is the
     "fixed one side, missed the other" detector.

  B. MARKER PUBLISHED AFTER THE FLIP — the claim publishes the very marker its
     family's release gates compare (an epoch stamp or a fresh identity token)
     only AFTER flipping the flag to true. A predecessor's teardown landing in
     that window reads the stale marker, passes the gate, and clears the live
     claim. Every claim must publish at least one gate-compared marker BEFORE
     the flip. (41f024e7's root cause; the pre-fix tree violates this at all 7
     claim sites, the post-fix tree at none — used as ground truth.)

## What counts as an ownership gate

The nearest enclosing `if` of a release write, if its condition compares
identity (`===` / `!==` between two non-literal operands), or compares
epoch/token/claim/job-named operands with `==` / `!=`. Plain business checks
(`result != null`, `code == 200`) do NOT count — they release on a condition,
not on ownership.

## Family scoping (false-family suppression)

"Same key" alone over-merges: `process` in LogcatTailer vs PersistentShell are
two different fields, not one family. So a family is scoped by the key's
declaration site: if the key is declared in exactly one file, all writes of
that key across files form one family (ChatViewModel's `_isStreaming` is
written from 5 files and IS one family); if declared in several files, each
file's writes form their own family. Log strings and comments are stripped
before matching, so `"… _isStreaming=false SKIPPED"` log text never counts as
a write.

Escape hatches: `release-ok: <reason>` (Check A) and `claim-ok: <reason>`
(Check B), on the same or the previous line.

Modes: default = strict (exit 1 on findings; wired into scan.sh as gate 21).
`--report` prints the same findings but exits 0 — kept for triage when the
baseline changes. Historical ground truth: the pre-fix tree (41f024e7^) trips
Check B at all 7 claim sites; the current tree passes strict clean, and the
self-test asserts both.

Usage: python3 scripts/scan/claim_release_guard.py <repo_root> [--report]
Exit:  0 = clean (or report mode), 1 = finding(s) in strict mode
"""
import os
import re
import sys

SRC_ROOTS = ["src/android/app/src/main/java"]

BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
LINE_COMMENT_RE = re.compile(r"//.*$")
RELEASE_ESCAPE_RE = re.compile(r"release-ok\s*:")
CLAIM_ESCAPE_RE = re.compile(r"claim-ok\s*:")

# Assignment write: receiver chain, optional `.value`, single `=` (not
# `==`/`!=`/`<=`/`>=`), value true|false|null.
WRITE_RE = re.compile(
    r"([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\s*(?:\.\s*value)?\s*"
    r"(?<![=!<>])=(?!=)\s*(true|false|null)\b"
)

# Mint-shaped plain assignment: `X = Any()`, or `X = otherIdent`. Declarations
# (preceded by val/var) are excluded at match time. Name must look like a
# generation stamp (epoch/token/claim/…).
MINT_SHAPE_RE = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*=\s*(Any\s*\(\s*\)|[A-Za-z_]\w*)\s*$"
)
MARKER_NAME_RE = re.compile(r"(?i)(epoch|token|claim|serial|owner)")

DECL_RE = re.compile(r"\b(?:val|var)\s+([A-Za-z_]\w*)\b")

# Ownership vocabulary. Substring form for distinctive stems (epochAtCancel,
# streamEpoch, claimToken...); word-bounded form for short generic stems.
OWNERSHIP_LONG = re.compile(r"(?i)(epoch|token|claim|generation|serial|owner|job)")
OWNERSHIP_SHORT = re.compile(r"(?i)\b(gen|seq|active|isactive)\b")
LITERAL_RE = re.compile(r"^(null|true|false|-?\d[\w.]*|\"[^\"]*\"|'[^']*')$")
SINGLE_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")

GATE_WINDOW = 8
GUARD_WINDOW_CHARS = 220
MINT_CLAIM_SEARCH = 60
FUNC_SEARCH_BACK = 60
FWD_REPORT_WINDOW = 60

# Audited (file, key) pairs where the ungated release is demonstrably safe in
# the current architecture. Every entry states WHY — re-audit when the file's
# concurrency model changes. New ungated writes of a listed key are NOT
# separately flagged: re-audit this file when touching these paths. (B is
# alias-free: no allow-list applies to Check B.)
ALLOWED = {
    ("com/rikkaminis/app/browser/BrowserUseManager.kt", "_isLoading"):
        "UI-only spinner flag; every navigation runs serialized through the "
        "tab's per-tab Mutex (BrowserTabPool lockForTab), and the ungated "
        "clears are callback-path clears of the CURRENT slot.",
    ("com/rikkaminis/app/browser/BrowserUseManager.kt", "navigationDeferred"):
        "Single-slot deferred; callbacks (onPageFinished/onReceivedError/"
        "onRendererGone) clear the slot they own; the re-entrant paths "
        "(timeout runnable, reloadAndWait) already carry identity guards.",
    ("com/rikkaminis/app/sandbox/PersistentShell.kt", "stdinWriter"):
        "stop() is owner-initiated teardown (generation++ issues first); the "
        "reader-loop teardown already carries the identity guard.",
    ("com/rikkaminis/app/webapp/WebAppActivity.kt", "webViewRef"):
        "Activity-scoped single instance; clears happen on the main thread "
        "during init-rebuild and onDestroy only.",
}


def strip_line(line):
    """String literals first (URLs contain `//`), then line comments."""
    s = STRING_RE.sub('""', line)
    return LINE_COMMENT_RE.sub("", s)


def load(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    raw_lines = raw.split("\n")
    no_block = BLOCK_COMMENT_RE.sub(
        lambda m: "\n" * m.group(0).count("\n"), raw
    )
    clean_lines = [strip_line(l) for l in no_block.split("\n")]
    return raw_lines, clean_lines


def escaped(raw_lines, line_no, rx):
    for j in (line_no - 2, line_no - 1):
        if 0 <= j < len(raw_lines) and rx.search(raw_lines[j]):
            return True
    return False


def extract_condition(text, open_paren):
    depth = 0
    for j in range(open_paren, len(text)):
        ch = text[j]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:j]
    return None


def compare_operands(cond):
    """Yield (left, right) operand strings for ==/!=/===/!== in a condition."""
    c = " ".join(cond.split())
    for op in ("!==", "===", "!=", "=="):
        for m in re.finditer(re.escape(op), c):
            left = re.split(r"[&|();{]", c[:m.start()])[-1].strip()
            right = re.split(r"[&|();{]", c[m.end():])[0].strip()
            if left and right:
                yield op, left, right


def is_ownership_cond(cond):
    if not cond:
        return False
    for op, left, right in compare_operands(cond):
        if LITERAL_RE.match(left) or LITERAL_RE.match(right):
            continue
        if not re.search(r"[A-Za-z_]", left) or not re.search(r"[A-Za-z_]", right):
            continue
        if op in ("===", "!=="):
            return True  # identity compare of two non-literals = TOCTOU guard
        if (OWNERSHIP_LONG.search(left) or OWNERSHIP_LONG.search(right)
                or OWNERSHIP_SHORT.search(left) or OWNERSHIP_SHORT.search(right)):
            return True
    return False


def enclosing_gate(clean_lines, idx, col):
    """First ownership condition enclosing the write, or None."""
    lo = max(0, idx - GATE_WINDOW)
    window = "\n".join(clean_lines[lo:idx + 1])
    my_pos = sum(len(clean_lines[j]) + 1 for j in range(lo, idx)) + col
    for m in re.finditer(r"\bif\s*\(", window):
        p = m.end() - 1
        if p >= my_pos:
            continue
        cond = extract_condition(window, p)
        if cond is None:
            continue
        after = p + len(cond) + 2
        k = after
        while k < len(window) and window[k] in " \t":
            k += 1
        enclosed = False
        if k < len(window) and window[k] == "{":
            depth = 0
            enclosed = True
            for t in range(k, my_pos):
                if window[t] == "{":
                    depth += 1
                elif window[t] == "}":
                    depth -= 1
                    if depth == 0:
                        enclosed = False
                        break
        else:  # brace-less if: enclosed until a closing brace or long gap
            seg = window[after:my_pos]
            if "}" not in seg and len(seg) < 120:
                enclosed = True
        if enclosed and is_ownership_cond(cond):
            return cond
    # early-return guard style: `if (ownership) return` just above the write
    tail = window[max(0, my_pos - GUARD_WINDOW_CHARS):my_pos]
    for m in re.finditer(r"\bif\s*\(", tail):
        cond = extract_condition(tail, m.end() - 1)
        if cond is None:
            continue
        after = tail[m.end() + len(cond) + 1:]
        if re.match(r"\s*(return|continue)\b", after) and is_ownership_cond(cond):
            return cond
    return None


def gate_marker_operands(cond):
    """Single-identifier operands in a gate condition (marker candidates)."""
    names = set()
    for _op, left, right in compare_operands(cond):
        for side in (left, right):
            if SINGLE_IDENT_RE.match(side):
                names.add(side)
    return names


def handoff_gate(clean_lines, idx, col, key):
    """Atomic hand-off shapes that make an ungated write safe.

      - take-or-bail:      `val X = slot ?: return/continue/break` in the
                           window — the writer holds the only reference.
      - same-slot snapshot:`val X = <key>` — takes the slot's value right
                           before clearing it (single-threaded hand-off).
      - conservative null:  enclosing `if (V == null)` whose body also writes
                           `V = null` — "clear only if nobody took the slot".

    These are the patterns a claimant uses when IT owns the slot; they are not
    the stale-teardown shape (a teardown reading a gate marker that may have
    moved on).
    """
    for j in range(max(0, idx - 4), idx):
        line = clean_lines[j]
        if re.search(r"\bval\s+\w+\s*=", line) and re.search(
            r"\?:\s*(return|continue|break)\b", line
        ):
            return "take-or-bail hand-off"
        if re.search(r"\bval\s+\w+\s*=\s*" + re.escape(key) + r"\s*$", line):
            return "same-slot snapshot"
    for j in range(max(0, idx - 5), idx + 1):
        m = re.search(r"\bif\s*\(\s*([A-Za-z_]\w*)\s*==\s*null\s*\)", clean_lines[j])
        if not m:
            continue
        var = m.group(1)
        body = "\n".join(clean_lines[j:min(len(clean_lines), idx + 5)])
        if re.search(r"(?<![\w.])" + re.escape(var) + r"\s*=\s*null", body):
            return f"conservative `{var} == null` gate"
    return None


def function_start(clean_lines, idx):
    for j in range(idx - 1, max(-1, idx - FUNC_SEARCH_BACK), -1):
        if re.search(r"\bfun\s+[A-Za-z_]\w*\s*[(<]", clean_lines[j]):
            return j
    return max(0, idx - FUNC_SEARCH_BACK)


def collect(root):
    """Returns (claims, releases, mints, decls, clean, rawD).

    mints: plain `X = <fresh stamp>` assignments where X is a marker-named var.
    """
    claims, releases, mints = [], [], []
    decls = {}
    clean, rawD = {}, {}
    for sr in SRC_ROOTS:
        base_dir = os.path.join(root, sr)
        for base, _dirs, files in os.walk(base_dir):
            for fn in sorted(files):
                if not fn.endswith(".kt"):
                    continue
                path = os.path.join(base, fn)
                rel = os.path.relpath(path, base_dir).replace(os.sep, "/")
                raw_lines, clean_lines = load(path)
                clean[rel], rawD[rel] = clean_lines, raw_lines
                for i, line in enumerate(clean_lines):
                    for m in DECL_RE.finditer(line):
                        decls.setdefault(m.group(1), set()).add(rel)
                    mm = MINT_SHAPE_RE.match(line)
                    if mm and MARKER_NAME_RE.search(mm.group(1)):
                        prefix = line[:mm.start(1)]
                        if not re.search(r"\b(val|var)\b", prefix):
                            mints.append(dict(file=rel, idx=i, line=i + 1,
                                              var=mm.group(1)))
                    for m in WRITE_RE.finditer(line):
                        prefix = line[:m.start()]
                        if re.search(r"\b(val|var)\b", prefix):
                            continue  # declaration, not an assignment
                        if prefix.count("(") > prefix.count(")"):
                            continue  # parameter default inside a signature
                        target = re.sub(r"\.\s*value$", "",
                                        m.group(1).strip())
                        if not target:
                            continue
                        rec = dict(file=rel, idx=i, line=i + 1, col=m.start(),
                                   target=target, key=target.split(".")[-1],
                                   value=m.group(2))
                        (claims if m.group(2) == "true" else releases).append(rec)
    return claims, releases, mints, decls, clean, rawD


def family_scope(site, decls):
    """Family anchor for a write: declaration file if unique, else own file."""
    files = decls.get(site["key"])
    if files and len(files) == 1:
        return next(iter(files))
    return site["file"]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    report_only = "--report" in sys.argv
    root = os.path.abspath(args[0] if args else ".")

    claims, releases, mints, decls, clean, rawD = collect(root)

    # ── Check A: sibling-gate inconsistency within one flag's family ──
    families = {}
    for r in releases:
        families.setdefault((r["key"], family_scope(r, decls)), []).append(r)

    marker_by_key = {}  # flag key -> set of gate-compared marker names
    findings_a = []
    for (key, _scope), sites in sorted(families.items()):
        gated, ungated = [], []
        for s in sites:
            cond = enclosing_gate(clean[s["file"]], s["idx"], s["col"])
            if cond:
                s["gate_cond"] = cond
                gated.append(s)
                marker_by_key.setdefault(key, set()).update(
                    gate_marker_operands(cond))
            else:
                s["handoff"] = handoff_gate(clean[s["file"]], s["idx"],
                                            s["col"], s["key"])
                ungated.append(s)
        if not gated or not ungated:
            continue
        if (sites[0]["file"], key) in ALLOWED:
            continue  # audited safe (see ALLOWED rationale)
        for s in ungated:
            if s.get("handoff"):
                continue  # atomic hand-off: the slot is the writer's own
            if escaped(rawD[s["file"]], s["line"], RELEASE_ESCAPE_RE):
                continue
            findings_a.append((s, gated[0]))

    # Only names that are actually re-minted somewhere count as markers: a
    # generation stamp compared by the gate AND reassigned as `X = <fresh>`.
    minted_names = {m["var"] for m in mints}

    # ── Check B: claim flips before publishing any gate-compared marker ──
    findings_b = []
    for c in claims:
        markers = (marker_by_key.get(c["key"], set()) & minted_names)
        if not markers:
            continue  # no stamp-based ownership family for this claim
        lo = max(function_start(clean[c["file"]], c["idx"]),
                 c["idx"] - MINT_CLAIM_SEARCH)
        pre = [m for m in mints
               if m["file"] == c["file"] and m["var"] in markers
               and lo <= m["idx"] < c["idx"]]
        if pre:
            continue
        if escaped(rawD[c["file"]], c["line"], CLAIM_ESCAPE_RE):
            continue
        fwd = [m for m in mints
               if m["file"] == c["file"] and m["var"] in markers
               and c["idx"] < m["idx"] <= c["idx"] + FWD_REPORT_WINDOW]
        nearest = min(fwd, key=lambda m: m["idx"]) if fwd else None
        findings_b.append((c, sorted(markers), nearest))

    total = len(findings_a) + len(findings_b)
    if not total:
        print("✅ Claim/release guard: no ungated release or unminted claim found")
        return 0

    tag = "⚠️  REPORT-ONLY" if report_only else "❌"
    print(f"{tag} Claim/release guard: {total} finding(s)"
          + ("  (report mode — not gating CI yet)" if report_only else ""))
    if findings_a:
        print("   [A] Ungated release whose siblings carry an ownership gate.")
        print("       Stale teardown can clear a newer claim (2026-09-06 / 10-12 family).")
        print("       Gate on ownership, or justify with `release-ok: <reason>`.")
        grouped = {}
        for s, g in findings_a:
            grouped.setdefault((s["file"], s["key"]), []).append((s, g))
        for (f, key), items in sorted(grouped.items()):
            lines = ", ".join(f":{s['line']}" for s, _ in items)
            g = items[0][1]
            print(f"     {f}  `{key}` — {len(items)} ungated: {lines}"
                  f"   | gated sibling :{g['line']} (`{g['gate_cond'][:56]}`)")
    if findings_b:
        print("   [B] Claim flips the flag before publishing the release gate's marker.")
        print("       Publish (`X = Any()` / `X = epoch`) BEFORE the flip, or justify")
        print("       with `claim-ok: <reason>`. Root cause of 2026-10-12 14:40.")
        for c, markers, nearest in findings_b:
            extra = (f"; nearest mint of {'/'.join(markers)} at :{nearest['line']}"
                     f" is AFTER the claim") if nearest else ""
            print(f"     {c['file']}:{c['line']}  `{c['target']} = true` — no "
                  f"pre-flip mint of {'/'.join(markers)}{extra}")
    return 0 if report_only else 1


if __name__ == "__main__":
    sys.exit(main())

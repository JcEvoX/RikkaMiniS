#!/usr/bin/env python3
"""deep-audit-1016c ISSUE-11 (P2) — RSS probe lock-scope gate.

Pins the contract introduced by branch fix/da1016-rssprobe-lock-scope for
sandbox/OffloadRssProbe.kt: the four field increments, the LEAK-SUSPECT
winner-flag write, the governance threshold decision and the cumulative
reset must form ONE critical section, and summary() must take a snapshot
under the same lock.

Before the fix, `synchronized(st)` guarded only the increments; the
threshold checks, `st.leaked = true` and `st.totalDeltaKb = 0L` all ran
outside it, so two offload workers (Semaphore(2), NativeOffload.kt:116/160)
could lose an increment between check and reset, double-fire the
LEAK-SUSPECT WARN, and concurrently execute the high-cost governanceHook
twice.

Scope: OffloadRssProbe.kt is enforced (FAIL). The same family exists in
browser/BrowserRssProbe.kt and provider/ProviderRssProbe.kt (pre-fix
shapes still there — 1016c dispatch constraint: this branch may only fix
OffloadRssProbe.kt); the gate lists their violations as OBSERVATION lines
without failing, so the follow-up branch has a machine list to work from.

Usage: python3 rss_probe_lock_scope_check.py [ROOT]   (ROOT = repo root)
       python3 rss_probe_lock_scope_check.py --selftest
Exit:  0 = gate green, 1 = violations found (or selftest failures).
"""
import os
import re
import sys

FAMILY = (
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/OffloadRssProbe.kt",
    "src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserRssProbe.kt",
    "src/android/app/src/main/java/com/rikkaminis/app/provider/ProviderRssProbe.kt",
)
# [da1019 FIX-I] the whole family is now enforced: both remaining probes were
# fixed in fix/da1019-rssprobe-lock-scope. The governance-reset criterion is
# scoped to GOVERNANCE_OWNER — Browser/Provider probes deliberately have NO
# governance semantics (no cumulative reset), so requiring the reset there
# would force a dead/fake write and change their behaviour.
ENFORCED = FAMILY
GOVERNANCE_OWNER = FAMILY[0]

RECORD = "record"
DECISION = "applyAndDecideLocked"
SUMMARY = "summary"

OUTSIDE_RECORD_BAD = (
    "reset outside record's critical section",
    "leak winner flag write outside record's critical section",
    "LEAK_SUSPECT threshold check outside record's critical section",
    "CUMULATIVE_GOVERN threshold check outside record's critical section",
    "PEAK_GOVERN threshold check outside record's critical section",
)
OUTSIDE_RECORD_NEEDLES = (
    "totalDeltaKb = 0L",
    "st.leaked = true",
    ">= LEAK_SUSPECT_KB",
    ">= CUMULATIVE_GOVERN_KB",
    ">= PEAK_GOVERN_KB",
)
DECISION_MUST_CONTAIN = (
    ("governance reset moved out of the locked decision function", "totalDeltaKb = 0L"),
    ("leak winner flag moved out of the locked decision function", "st.leaked = true"),
)


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def function_body(text, name):
    """Body of `fun <name>(...)` — brace-balanced, string literals skipped."""
    m = re.search(r"\bfun\s+" + re.escape(name) + r"\s*\(", text)
    if not m:
        return None
    i = text.find("(", m.end() - 1)
    depth = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    j = text.find("{", i + 1)
    if j < 0:
        return None
    depth = 0
    k = j
    while k < len(text):
        c = text[k]
        if c == '"':
            k += 1
            while k < len(text) and text[k] != '"':
                k += 2 if text[k] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[j + 1 : k]
        k += 1
    return None


def line_of(src, needle):
    i = src.find(needle)
    return src.count("\n", 0, i) + 1 if i >= 0 else 0


def analyse(src, require_govern_reset=True):
    """Returns list of (criterion, ok, detail, line). ok=True means contract held."""
    res = []
    rec = function_body(src, RECORD)
    if rec is None:
        res.append(("record function found", False, "missing", 0))
    else:
        for idx, needle in enumerate(OUTSIDE_RECORD_NEEDLES):
            hits = rec.count(needle)
            res.append((
                OUTSIDE_RECORD_BAD[idx],
                hits == 0,
                "%d occurrence(s) outside the lock" % hits,
                line_of(src, needle),
            ))
        sync_n = rec.count("synchronized(")
        res.append((
            "record has exactly one synchronized section",
            sync_n == 1,
            "found %d" % sync_n,
            line_of(src, "synchronized("),
        ))
    dec = function_body(src, DECISION)
    if dec is None:
        res.append(("locked decision function %s found" % DECISION, False, "missing", 0))
    else:
        for crit, needle in DECISION_MUST_CONTAIN:
            # Only the governance owner carries the cumulative reset; the
            # other family members have no governance section at all.
            if needle == "totalDeltaKb = 0L" and not require_govern_reset:
                continue
            res.append((
                crit,
                needle in dec,
                needle,
                line_of(src, needle),
            ))
    summ = function_body(src, SUMMARY)
    if summ is None:
        res.append(("summary function found", False, "missing", 0))
    else:
        res.append((
            "summary takes a snapshot under the lock",
            "synchronized(" in summ,
            "synchronized(",
            line_of(src, "fun summary("),
        ))
    return res


COMPLIANT = '''package com.rikkaminis.app.sandbox

object Probe {
    private const val LEAK_SUSPECT_KB = 1_048_576L
    private const val CUMULATIVE_GOVERN_KB = 262_144L
    private const val PEAK_GOVERN_KB = 65_536L

    internal data class Stats(var count: Int = 0, var totalDeltaKb: Long = 0L, var leaked: Boolean = false)
    private val byHandler = java.util.concurrent.ConcurrentHashMap<String, Stats>()
    var governanceHook: () -> Unit = {}

    fun record(name: String, deltaKb: Long) {
        val st = byHandler.computeIfAbsent(name) { Stats() }
        val d = synchronized(st) { applyAndDecideLocked(st, deltaKb) }
        if (d.govern) {
            runCatching { governanceHook() }
        }
    }

    internal fun applyAndDecideLocked(st: Stats, deltaKb: Long): Long {
        st.count += 1
        st.totalDeltaKb += deltaKb
        val totalAfterKb = st.totalDeltaKb
        val warnLeak = !st.leaked && totalAfterKb >= LEAK_SUSPECT_KB
        if (warnLeak) st.leaked = true
        val govern = totalAfterKb >= CUMULATIVE_GOVERN_KB || deltaKb >= PEAK_GOVERN_KB
        if (govern) st.totalDeltaKb = 0L
        return totalAfterKb
    }

    fun summary(): String {
        val snapshot = byHandler.entries.map { (name, st) -> name to synchronized(st) { st.copy() } }
        return "offload-rss summary:"
    }
}
'''

BREAKERS = {}
# B1: reset moved back outside the record critical section
BREAKERS[1] = (
    [("val d = synchronized(st) { applyAndDecideLocked(st, deltaKb) }",
      "val d = applyAndDecideLocked(st, deltaKb)\n        if (st.totalDeltaKb >= CUMULATIVE_GOVERN_KB) st.totalDeltaKb = 0L")],
    OUTSIDE_RECORD_BAD[0],
)
# B2: leak winner flag write moved back outside the lock (pre-fix shape #2):
# the decision function loses the write, record gains it outside the lock
BREAKERS[2] = (
    [("if (warnLeak) st.leaked = true", ""),
     ("if (d.govern) {", "if (!st.leaked) st.leaked = true\n        if (d.govern) {")],
    OUTSIDE_RECORD_BAD[1],
)
# B3: record loses its critical section entirely
BREAKERS[3] = (
    [("val d = synchronized(st) { applyAndDecideLocked(st, deltaKb) }",
      "val d = applyAndDecideLocked(st, deltaKb)")],
    "record has exactly one synchronized section",
)
# B4: summary reads without a snapshot lock
BREAKERS[4] = (
    [("name to synchronized(st) { st.copy() }", "name to st")],
    "summary takes a snapshot under the lock",
)


def selftest():
    print("── selftest: compliant fixture must be green, each breaker must go red ──")
    base = analyse(strip_comments(COMPLIANT))
    failures = []
    reds0 = [c for c, ok, _, _ in base if not ok]
    if reds0:
        failures.append("compliant fixture is not green: %s" % reds0)
        print("  [FAIL] compliant fixture red on: %s" % reds0)
    else:
        print("  [OK] compliant fixture green on all %d criteria" % len(base))

    for idx, (pairs, target) in BREAKERS.items():
        mutated = COMPLIANT
        missing = [old for old, _ in pairs if old not in mutated]
        if missing:
            failures.append("breaker %d cannot apply (fixture text moved): %s" % (idx, missing))
            print("  [FAIL] breaker %d: anchor not found in fixture" % idx)
            continue
        for old, new in pairs:
            mutated = mutated.replace(old, new, 1)
        res = analyse(strip_comments(mutated))
        red = [c for c, ok, _, _ in res if not ok]
        if target not in red:
            failures.append("breaker %d did not turn %r red (red=%s)" % (idx, target, red))
            print("  [FAIL] breaker %d -> %r stayed green (red=%s)" % (idx, target, red))
        else:
            print("  [OK] breaker %d -> red on %r%s" % (idx, target, "" if len(red) == 1 else " (+%d side)" % (len(red) - 1)))
    return failures


def main():
    if "--selftest" in sys.argv:
        fails = selftest()
        if fails:
            print("SELFTEST FAIL (%d)" % len(fails))
            return 1
        print("SELFTEST=PASS (%d fixtures)" % (1 + len(BREAKERS)))
        return 0

    root = None
    args = [a for a in sys.argv[1:]]
    if "--root" in args:
        root = args[args.index("--root") + 1]
    elif args:
        root = args[0]
    root = root or "."
    for rel in ENFORCED:
        if not os.path.isfile(os.path.join(root, rel)):
            print("FAIL %s not found under root %s" % (rel, root))
            return 1

    results = []
    for rel in ENFORCED:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            print("FAIL %s not found under root %s" % (rel, root))
            return 1
        src = open(path, encoding="utf-8").read()
        print("== enforced: %s" % rel)
        for crit, ok, detail, line in analyse(
            strip_comments(src), require_govern_reset=(rel == GOVERNANCE_OWNER)
        ):
            results.append((crit, ok, detail, line, rel))

    failed = [(c, d, l, r) for c, ok, d, l, r in results if not ok]
    for c, ok, d, l, r in results:
        if ok:
            print("  [OK] %s (%s)" % (c, r))
        else:
            print("  [FAIL] %s:%s %s — %s" % (os.path.basename(r), l, c, d))

    total = len(results)
    print("Summary: %d passed, %d failed" % (total - len(failed), len(failed)))
    print("Observation (same family, not fixed in this branch): 0")
    if failed:
        print("rss-probe lock scope gate: RED — the fix contract is violated")
        return 1
    print("GATE=GREEN rss_probe_lock_scope")
    return 0


if __name__ == "__main__":
    sys.exit(main())

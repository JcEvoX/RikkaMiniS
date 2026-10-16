#!/usr/bin/env python3
"""deep-audit-1018 ISSUE-10 (P1) — router health lock + slot-promotion resume gate.

Pins the two contracts introduced by branch fix/da1018-router-locks:

1. GroupRouter.health — the map lives on a process-wide singleton
   (ChatViewModel companion holds the only instance; every session, every
   sub-agent dispatch shares it). Pre-fix it was a bare LinkedHashMap touched
   from Main (clearHealth / isUsable / select) AND IO coroutines
   (AgentLoopEngine.recordResult on stream failure) with zero locking —
   concurrent structural modification loses updates or throws CME, and the
   lost/stale health state then drives cooldown/circuit-break decisions.
   Contract: EVERY `health` reference (reads, writes, iteration) must sit
   inside a `synchronized(healthLock)` critical section. FIFO eviction of the
   LinkedHashMap is why the fix is a lock rather than a ConcurrentHashMap.

2. SessionConcurrencyManager — "resume outside the lock" must be real.
   Pre-fix, promote() was called from @Synchronized releaseSlot() /
   releasePromotedAfterCancel(): promote's own inner block exited, but the
   outer monitor was still held (re-entrant), so the `toResume?.resume(Unit)`
   ran on a lock-holding thread — with a Dispatchers.Unconfined waiter the
   whole continuation body ran before the releaser could let go.
   Contract: zero `@Synchronized` methods in this file (all critical sections
   are explicit `synchronized(this)` spans), and every `.resume(` call sits
   OUTSIDE every such span.

Deliberate strictness: `@Synchronized` anywhere in the file is a violation —
the file is the concurrency core; keeping every critical section a visible
`synchronized(this)` span is part of the audited design, not an accident.

Usage: python3 router_lock_check.py [ROOT]   (ROOT = repo root, default .)
       python3 router_lock_check.py --selftest
Exit:  0 = gate green, 1 = violations found (or selftest failures).
"""
import os
import re
import sys

ROUTER = "src/android/app/src/main/java/com/rikkaminis/app/data/routing/GroupRouter.kt"
SCM = "src/android/app/src/main/java/com/rikkaminis/app/service/SessionConcurrencyManager.kt"

HEALTH_REF = re.compile(r"\bhealth\b")
RESUME_CALL = re.compile(r"\.resume\(")
MIN_HEALTH_REFS = 6
MIN_SCM_SPANS = 2
MIN_RESUME_SITES = 2


def code_only(src):
    """Comment/string-stripped view with ALL offsets preserved: contents are
    replaced by spaces (newlines kept), so span/offset arithmetic below uses
    the same coordinates as the raw file."""
    out = list(src)
    n = len(src)
    i = 0
    while i < n:
        if src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j
            for k in range(i, min(j + 3, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 3
        elif src.startswith("//", i):
            j = i
            while j < n and src[j] != "\n":
                out[j] = " "
                j += 1
            i = j
        elif src.startswith("/*", i):
            j = i + 2
            while j + 1 < n and not (src[j] == "*" and src[j + 1] == "/"):
                if out[j] != "\n":
                    out[j] = " "
                j += 1
            for k in range(i, min(j + 2, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 2
        elif src[i] == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def blank_health_decl(code):
    """Blank the `private val health = mutableMapOf<…>()` field declaration —
    a declaration is not an access; everything else must live in a span."""
    m = re.search(r"\bprivate\s+val\s+health\s*=[^\n]*", code)
    if not m:
        return code
    e = code.find("\n", m.end())
    e = len(code) if e < 0 else e
    return code[: m.start()] + " " * (e - m.start()) + code[e:]


def synchronized_spans(code):
    """[(lock_expr, open_off, close_off)] for every `synchronized (…) { … }`."""
    spans = []
    for m in re.finditer(r"\bsynchronized\s*\(", code):
        i = m.end() - 1
        depth = 0
        while i < len(code):
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            continue
        lock_expr = code[m.end(): i].strip()
        j = code.find("{", i + 1)
        if j < 0 or j - i > 200:
            continue
        depth = 0
        k = j
        while k < len(code):
            if code[k] == "{":
                depth += 1
            elif code[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if depth == 0:
            spans.append((lock_expr, j, k))
    return spans


def _line(code, off):
    return code.count("\n", 0, off) + 1


def analyse_router(code):
    res = []
    declared = re.search(r"\bval\s+healthLock\b", code)
    res.append((
        "healthLock declared",
        declared is not None,
        "private val healthLock = Any()" if declared else "no `val healthLock` in code view",
        _line(code, declared.start()) if declared else 0,
    ))
    spans = synchronized_spans(code)
    health_spans = [(a, b) for (expr, a, b) in spans if "healthLock" in expr]
    refs = list(HEALTH_REF.finditer(blank_health_decl(code)))
    res.append((
        "health access surface present (>= %d points)" % MIN_HEALTH_REFS,
        len(refs) >= MIN_HEALTH_REFS,
        "%d health reference(s) in code view" % len(refs),
        0,
    ))
    outside = [m.start() for m in refs if not any(a <= m.start() <= b for (a, b) in health_spans)]
    res.append((
        "every health reference inside a synchronized(healthLock) span",
        not outside,
        ("all %d reference(s) protected" % len(refs)) if not outside
        else "%d unprotected at line(s) %s" % (len(outside), ", ".join(str(_line(code, o)) for o in outside[:8])),
        _line(code, outside[0]) if outside else 0,
    ))
    res.append((
        "synchronized(healthLock) spans exist (>= 1)",
        len(health_spans) >= 1,
        "%d span(s)" % len(health_spans),
        0,
    ))
    return res


def analyse_scm(code):
    res = []
    synch = [m.start() for m in re.finditer(r"@Synchronized\b", code)]
    res.append((
        "no @Synchronized methods in SessionConcurrencyManager",
        not synch,
        "0 occurrences" if not synch
        else "%d at line(s) %s" % (len(synch), ", ".join(str(_line(code, o)) for o in synch)),
        _line(code, synch[0]) if synch else 0,
    ))
    spans = synchronized_spans(code)
    res.append((
        "explicit critical sections exist (>= %d spans)" % MIN_SCM_SPANS,
        len(spans) >= MIN_SCM_SPANS,
        "%d span(s)" % len(spans),
        0,
    ))
    resumes = list(RESUME_CALL.finditer(code))
    res.append((
        "waiter wake-up sites exist (>= %d)" % MIN_RESUME_SITES,
        len(resumes) >= MIN_RESUME_SITES,
        "%d resume site(s)" % len(resumes),
        0,
    ))
    inside = [m.start() for m in resumes if any(a <= m.start() <= b for (_, a, b) in spans)]
    res.append((
        "resume happens outside every critical section",
        not inside,
        ("all %d resume site(s) outside the lock" % len(resumes)) if not inside
        else "%d inside a span at line(s) %s" % (len(inside), ", ".join(str(_line(code, o)) for o in inside)),
        _line(code, inside[0]) if inside else 0,
    ))
    return res


COMPLIANT_ROUTER = '''package com.rikkaminis.app.data.routing

class CompliantRouter {
    private val healthLock = Any()
    private val health = mutableMapOf<String, Int>()

    fun record(entryId: String, failed: Boolean) {
        synchronized(healthLock) {
            val current = health[entryId]
            health[entryId] = (current ?: 0) + 1
            if (health.size > 3) {
                val eldest = health.keys.firstOrNull() ?: return
                health.remove(eldest)
            }
        }
    }

    fun clearHealth() = synchronized(healthLock) { health.clear() }
    fun isUsable(entryId: String): Boolean = synchronized(healthLock) { health[entryId] != null }
    fun healthOf(entryId: String): Int? = synchronized(healthLock) { health[entryId] }
}
'''

VACUOUS_ROUTER = '''package com.rikkaminis.app.data.routing

class VacuousRouter {
    private val healthLock = Any()
}
'''

ROUTER_BREAKERS = {
    1: (
        [("fun isUsable(entryId: String): Boolean = synchronized(healthLock) { health[entryId] != null }",
          "fun peek(entryId: String): Int? = health[entryId]\n    fun isUsable(entryId: String): Boolean = synchronized(healthLock) { health[entryId] != null }")],
        "every health reference inside a synchronized(healthLock) span",
    ),
    2: (
        [("fun clearHealth() = synchronized(healthLock) { health.clear() }",
          "fun clearHealth() = synchronized(otherLock) { health.clear() }")],
        "every health reference inside a synchronized(healthLock) span",
    ),
    3: (
        [("synchronized(healthLock) {\n            val current = health[entryId]\n            health[entryId] = (current ?: 0) + 1\n            if (health.size > 3) {\n                val eldest = health.keys.firstOrNull() ?: return\n                health.remove(eldest)\n            }\n        }",
          "{\n            val current = health[entryId]\n            health[entryId] = (current ?: 0) + 1\n            if (health.size > 3) {\n                val eldest = health.keys.firstOrNull() ?: return\n                health.remove(eldest)\n            }\n        }")],
        "every health reference inside a synchronized(healthLock) span",
    ),
    4: (
        [("fun clearHealth() = synchronized(healthLock) { health.clear() }",
          "fun clearHealth() = synchronized(healthLock) { health.clear() }\n    fun snapshot(): Map<String, Int> = health")],
        "every health reference inside a synchronized(healthLock) span",
    ),
}

COMPLIANT_SCM = '''package com.rikkaminis.app.service

import kotlin.coroutines.resume
import kotlinx.coroutines.CancellableContinuation

class CompliantManager {
    private class Promotion(val happened: Boolean, val toResume: CancellableContinuation<Unit>?)

    fun releaseSlot(runId: String) {
        val promotion = synchronized(this) { promoteLocked(runId) }
        finishPromotion(promotion)
    }

    fun releasePromotedAfterCancel(runId: String) {
        val promotion = synchronized(this) { promoteLocked(runId) }
        finishPromotion(promotion)
    }

    private fun promoteLocked(runId: String): Promotion = Promotion(true, null)

    private fun finishPromotion(promotion: Promotion) {
        promotion.toResume?.resume(Unit)
    }

    suspend fun acquireSlot(runId: String) {
        val resumeNow = synchronized(this) { null as CancellableContinuation<Unit>? }
        resumeNow?.resume(Unit)
    }
}
'''

SCM_BREAKERS = {
    1: (
        [("fun releaseSlot(runId: String) {\n        val promotion = synchronized(this) { promoteLocked(runId) }\n        finishPromotion(promotion)\n    }",
          "fun releaseSlot(runId: String) {\n        synchronized(this) {\n            val promotion = promoteLocked(runId)\n            promotion.toResume?.resume(Unit)\n        }\n    }")],
        "resume happens outside every critical section",
    ),
    2: (
        [("fun releaseSlot(runId: String) {", "@Synchronized\n    fun releaseSlot(runId: String) {")],
        "no @Synchronized methods in SessionConcurrencyManager",
    ),
    3: (
        [("promotion.toResume?.resume(Unit)", "promotion.toResume"),
         ("resumeNow?.resume(Unit)", "checkNotNull(resumeNow)")],
        "waiter wake-up sites exist (>= 2)",
    ),
    4: (
        [("val promotion = synchronized(this) { promoteLocked(runId) }", "val promotion = promoteLocked(runId)"),
         ("val promotion = synchronized(this) { promoteLocked(runId) }", "val promotion = promoteLocked(runId)"),
         ("val resumeNow = synchronized(this) { null as CancellableContinuation<Unit>? }", "val resumeNow = null as CancellableContinuation<Unit>?")],
        "explicit critical sections exist (>= 2 spans)",
    ),
}


def selftest():
    print("-- selftest: compliant fixtures green; every breaker red on its target --")
    failures = []
    fixtures = 0

    base_r = analyse_router(code_only(COMPLIANT_ROUTER))
    reds = [c for c, ok, _, _ in base_r if not ok]
    fixtures += 1
    if reds:
        failures.append("compliant router fixture red on: %s" % reds)
        print("  [FAIL] compliant router fixture red on: %s" % reds)
    else:
        print("  [OK] compliant router fixture green on all %d criteria" % len(base_r))

    base_s = analyse_scm(code_only(COMPLIANT_SCM))
    reds = [c for c, ok, _, _ in base_s if not ok]
    fixtures += 1
    if reds:
        failures.append("compliant scm fixture red on: %s" % reds)
        print("  [FAIL] compliant scm fixture red on: %s" % reds)
    else:
        print("  [OK] compliant scm fixture green on all %d criteria" % len(base_s))

    vac = analyse_router(code_only(VACUOUS_ROUTER))
    reds = [c for c, ok, _, _ in vac if not ok]
    fixtures += 1
    if "health access surface present (>= %d points)" % MIN_HEALTH_REFS not in reds:
        failures.append("vacuous router fixture did not trip the anti-vacuous sanity")
        print("  [FAIL] vacuous router fixture stayed green (red=%s)" % reds)
    else:
        print("  [OK] vacuous router fixture -> red on %r (+%d side)" % (
            "health access surface present (>= %d points)" % MIN_HEALTH_REFS, len(reds) - 1))

    for idx, (pairs, target) in ROUTER_BREAKERS.items():
        mutated = COMPLIANT_ROUTER
        missing = [old for old, _ in pairs if old not in mutated]
        fixtures += 1
        if missing:
            failures.append("router breaker %d cannot apply (fixture anchor moved)" % idx)
            print("  [FAIL] router breaker %d: anchor not found in fixture" % idx)
            continue
        for old, new in pairs:
            mutated = mutated.replace(old, new, 1)
        reds = [c for c, ok, _, _ in analyse_router(code_only(mutated)) if not ok]
        if target not in reds:
            failures.append("router breaker %d did not turn %r red (red=%s)" % (idx, target, reds))
            print("  [FAIL] router breaker %d -> %r stayed green (red=%s)" % (idx, target, reds))
        else:
            print("  [OK] router breaker %d -> red on %r (+%d side)" % (idx, target, len(reds) - 1))

    for idx, (pairs, target) in SCM_BREAKERS.items():
        mutated = COMPLIANT_SCM
        missing = [old for old, _ in pairs if old not in mutated]
        fixtures += 1
        if missing:
            failures.append("scm breaker %d cannot apply (fixture anchor moved)" % idx)
            print("  [FAIL] scm breaker %d: anchor not found in fixture" % idx)
            continue
        for old, new in pairs:
            mutated = mutated.replace(old, new, 1)
        reds = [c for c, ok, _, _ in analyse_scm(code_only(mutated)) if not ok]
        if target not in reds:
            failures.append("scm breaker %d did not turn %r red (red=%s)" % (idx, target, reds))
            print("  [FAIL] scm breaker %d -> %r stayed green (red=%s)" % (idx, target, reds))
        else:
            print("  [OK] scm breaker %d -> red on %r (+%d side)" % (idx, target, len(reds) - 1))

    return failures, fixtures


def main():
    if "--selftest" in sys.argv:
        fails, fixtures = selftest()
        if fails:
            print("SELFTEST FAIL (%d)" % len(fails))
            return 1
        print("ROUTER_LOCK_SELFTEST=PASS (%d fixtures)" % fixtures)
        return 0

    root = "."
    args = sys.argv[1:]
    if "--root" in args:
        root = args[args.index("--root") + 1]
    elif args:
        root = args[0]

    results = []
    for rel, analyser in ((ROUTER, analyse_router), (SCM, analyse_scm)):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            print("FAIL %s not found under root %s" % (rel, root))
            return 1
        code = code_only(open(path, encoding="utf-8").read())
        print("== enforced: %s" % rel)
        for crit, ok, detail, line in analyser(code):
            results.append((crit, ok, detail, line, rel))

    failed = [(c, d, l, r) for c, ok, d, l, r in results if not ok]
    for c, ok, d, l, r in results:
        if ok:
            print("  [OK] %s" % c)
        else:
            print("  [FAIL] %s:%s %s — %s" % (os.path.basename(r), l, c, d))

    print("Summary: %d passed, %d failed" % (len(results) - len(failed), len(failed)))
    if failed:
        print("router-lock gate: RED — the fix contract is violated")
        return 1
    print("GATE=GREEN router_lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())

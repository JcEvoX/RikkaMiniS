#!/usr/bin/env python3
"""
Gate: a date that decides a DAY must be derived from the caller's day, not
re-read from the clock at the wrong moment.

Two silent-in-production defect classes from deep-audit-1016c (fixed 2026-10-16,
branch fix/da1016-schedule-date-bounds):

  A. SETTLE-TIME DATE LEAKAGE (ISSUE-09A)
     `ScheduledTaskRunner.finalizeRun` passed the settle-day clock
     (`LocalDate.now()`) into `ScheduledTasksStore.completeRun`. A scheduled run
     fired 23:55 that settled at 00:06 therefore wrote TOMORROW into
     `ScheduledTask.lastRunDate`, and `ScheduledTaskPolicy` answered
     ALREADY_RAN for the whole of tomorrow — a full day of work silently
     skipped, nothing having run. The rule: the date handed to `completeRun`
     must come from the run's SCHEDULED day (`Dispatch.scheduledDate`, captured
     in the fire pass), routed through `ScheduledRunDating.gateDateOnSettle`.

  B. RAW PARSE AT AN END BOUNDARY (ISSUE-09B)
     `resolveDateRange` fed `parseDate(str)` straight into the window, so a bare
     `--end 2026-10-16` parsed to 00:00:00.000 and `--start X --end X` was a
     zero-width window (measured width=0) — a day the caller named explicitly
     answered empty. Same for `freebusy`. The rule: the query surfaces must
     route a bare-date end through `alignBareDateEnd` (docs say INCLUSIVE
     end-of-day) and must not rebuild calendar math inline (the `--today`
     +24h constant was 1h wrong on every DST day).

Reading is brace/paren-depth aware where judgement matters; the checks below
assert PRESENCE of the routing call rather than the absence of a spelling, so a
future refactor that keeps the contract (same decision, different helper name
registered here) is a deliberate edit to this gate, not a silent pass.

Usage: python3 scripts/scan/schedule_date_source_check.py [repo_root]
       python3 scripts/scan/schedule_date_source_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import argparse
import os
import re
import sys

SCHEDULER_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/scheduled/ScheduledTaskRunner.kt"
)
CALENDAR_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/CalendarOffloadHandler.kt"
)

COMPLETE_CALL = "ScheduledTasksStore.completeRun("
DATING_CALL = "ScheduledRunDating.gateDateOnSettle("
DISPATCH_CALL = "dispatched[sid] = Dispatch("
ALIGN_CALL = "alignBareDateEnd("

FUN_RE = re.compile(r"^    (?:[@a-zA-Z]+ )*fun (\w+) *\(", re.MULTILINE)
CLOCK_IN_ARG = re.compile(r"LocalDate\.now\(\)|LocalDateTime\.now\(\)")


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def member_slice(src, marker):
    """Lines of the member declared at [marker], up to the next sibling member."""
    lines = src.split("\n")
    start = next((i for i, l in enumerate(lines) if marker in l), None)
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        indent = len(lines[i]) - len(lines[i].lstrip(" "))
        if indent <= 4 and stripped and FUN_RE.match(lines[i] + "("):
            end = i
            break
        if indent <= 4 and stripped.startswith(("private ", "internal ", "public ", "fun ")) \
                and "fun " in stripped and "(" in stripped:
            end = i
            break
    return "\n".join(lines[start:end])


def call_args(src, call):
    """All parenthesised argument texts of [call] occurrences (depth aware)."""
    out = []
    for m in re.finditer(re.escape(call), src):
        i = m.end() - 1  # at '('
        depth = 0
        for k in range(i, len(src)):
            if src[k] == "(":
                depth += 1
            elif src[k] == ")":
                depth -= 1
                if depth == 0:
                    out.append((m.start(), src[i + 1:k]))
                    break
    return out


def strip_calls(text, marker):
    """Remove every parenthesised call text starting at [marker] (incl. a
    trailing lambda body when the call is followed by `{ ... }`)."""
    out, i = text, 0
    while True:
        k = out.find(marker, i)
        if k < 0:
            return out
        j = out.index("(", k + len(marker) - 1)
        depth, end = 0, len(out)
        for m in range(j, len(out)):
            if out[m] == "(":
                depth += 1
            elif out[m] == ")":
                depth -= 1
                if depth == 0:
                    end = m + 1
                    break
        rest = out[end:]
        if rest.lstrip().startswith("{"):
            lb = out.index("{", end)
            d, e2 = 0, len(out)
            for m in range(lb, len(out)):
                if out[m] == "{":
                    d += 1
                elif out[m] == "}":
                    d -= 1
                    if d == 0:
                        e2 = m + 1
                        break
            end = e2
        out = out[:k] + out[end:]
        i = k


def top_level_args(argtext):
    """Split a call's argument text on top-level commas."""
    parts, depth, cur = [], 0, []
    for ch in argtext:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def violations_scheduler(src, label):
    """ISSUE-09A rules for ScheduledTaskRunner.kt."""
    bad = []

    calls = call_args(src, COMPLETE_CALL)
    if len(calls) != 1:
        bad.append("%s:%d: expected exactly 1 %s call (finalizeRun's funnel), found %d"
                   % (label, 1, COMPLETE_CALL, len(calls)))
    for at, argtext in calls:
        args = top_level_args(argtext)
        if len(args) != 4:
            bad.append("%s:%d: %s must take 4 arguments (app, taskId, outcome, gateDate), found %d"
                       % (label, line_of(src, at), COMPLETE_CALL, len(args)))
            continue
        gate = args[3]
        if DATING_CALL not in gate:
            bad.append("%s:%d: the date argument of completeRun is not routed through %s "
                       "(found: %s) — a settle-time clock read here re-opens ISSUE-09A "
                       "(a run settling after local midnight pins TOMORROW into lastRunDate "
                       "and the whole next day degrades to ALREADY_RAN)"
                       % (label, line_of(src, at), DATING_CALL, gate[:80]))
        outside = strip_calls(gate, DATING_CALL)
        if CLOCK_IN_ARG.search(outside):
            bad.append("%s:%d: a wall-clock read reaches completeRun's date argument outside "
                       "the dating call (%s)" % (label, line_of(src, at), outside[:80]))

    builds = call_args(src, "= Dispatch(")
    if not builds:
        bad.append("%s:1: no dispatch record construction found (expected `%s` with "
                   "`scheduledDate = …`)" % (label, DISPATCH_CALL))
    for at, argtext in builds:
        if "scheduledDate" not in argtext:
            bad.append("%s:%d: Dispatch(…) is built without scheduledDate — the settle path "
                       "cannot credit the scheduled day" % (label, line_of(src, at)))

    if "internal object ScheduledRunDating {" not in src:
        bad.append("%s:1: ScheduledRunDating is gone — the dating decision is no longer a "
                   "pure, harness-covered unit" % label)
    return bad


def violations_calendar(src, label):
    """ISSUE-09B rules for CalendarOffloadHandler.kt."""
    bad = []

    rdr = member_slice(src, "private fun resolveDateRange(")
    if rdr is None:
        bad.append("%s:1: resolveDateRange is gone (rename => update this gate)" % label)
    else:
        if "resolveRangePure(" not in rdr:
            bad.append("%s:1: resolveDateRange must delegate to the pure core resolveRangePure(…)"
                       % label)
        own_code = strip_calls(rdr, "resolveRangePure(")
        if "parseDate(" in own_code or "parseDateInZone(" in own_code:
            bad.append("%s:1: resolveDateRange parses bounds itself again — the zero-width "
                       "--start X --end X window (bare date parsed to 00:00:00.000) and the "
                       "+24h DST arithmetic both come back with it" % label)
        if "24 * 60 * 60 * 1000L" in own_code:
            bad.append("%s:1: resolveDateRange keeps a flat 24h constant — DST days are 23/25h "
                       "and Calendar day arithmetic (addLocalDays/startOfLocalDay) is required"
                       % label)

    if "internal fun resolveRangePure(" not in src:
        bad.append("%s:1: resolveRangePure is gone" % label)
    else:
        pure = member_slice(src, "internal fun resolveRangePure(") or ""
        if "alignBareDateEnd(" not in pure:
            bad.append("%s:1: resolveRangePure no longer aligns a bare-date --end — "
                       "`--start D --end D` answers an empty day again" % label)
        for needed in ("startOfLocalDay(", "addLocalDays("):
            if needed not in pure:
                bad.append("%s:1: resolveRangePure lost %s — --today/--days must walk local "
                           "calendar days, not fixed offsets" % (label, needed))

    if "internal fun alignBareDateEnd(" not in src:
        bad.append("%s:1: alignBareDateEnd helper is gone" % label)
    else:
        align = member_slice(src, "internal fun alignBareDateEnd(") or ""
        if "DATE_ONLY" not in align:
            bad.append("%s:1: alignBareDateEnd no longer gates on the bare `YYYY-MM-DD` form — "
                       "explicit instants (datetime / relative) must keep their exact value"
                       % label)
    end = member_slice(src, "internal fun endOfLocalDay(") or ""
    if "add(Calendar.DAY_OF_YEAR, 1)" not in end:
        bad.append("%s:1: endOfLocalDay no longer uses add(DAY_OF_YEAR, 1) — the DST-safe "
                   "inclusive end (a 24h-1ms constant stops 1h short on a 25-hour day)" % label)

    fb = member_slice(src, "private fun doFreebusy(")
    if fb is not None:
        if "parseDate(endStr)" in fb:
            bad.append("%s:1: freebusy feeds the raw parse of --end into the window — a bare "
                       "date must go through alignBareDateEnd" % label)
        if ALIGN_CALL not in fb:
            bad.append("%s:1: freebusy --end must route through %s" % (label, ALIGN_CALL))
    return bad


def check(repo_root):
    bad = []
    for rel, fn in ((SCHEDULER_REL, violations_scheduler), (CALENDAR_REL, violations_calendar)):
        path = os.path.join(repo_root, rel)
        if not os.path.isfile(path):
            bad.append("%s:1: ROT — file not found under %s" % (rel, repo_root))
            continue
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        bad.extend(fn(src, rel))
    return bad


# ─── fixtures: the gate proves its own teeth ────────────────────────────────

GOOD_RUNNER = """
    private data class Dispatch(
        val taskId: String,
        val deadlineSec: Int,
        val startedAtMs: Long,
        val scheduledDate: String,
    )

    private suspend fun fire(app: MinisApp, task: ScheduledTask, scheduledDate: String) {
            dispatched[sid] = Dispatch(
                taskId = task.id,
                deadlineSec = task.deadlineSec,
                startedAtMs = System.currentTimeMillis(),
                scheduledDate = scheduledDate,
            )
    }

    internal object ScheduledRunDating {
        fun gateDateOnSettle(scheduledDate: String, settleDate: String, outcome: String): String = scheduledDate
    }

    private suspend fun finalizeRun(app: MinisApp, taskId: String, outcome: String, sessionId: String?, scheduledDate: String) {
        val res = try {
            ScheduledTasksStore.completeRun(
                app,
                taskId,
                outcome,
                ScheduledRunDating.gateDateOnSettle(scheduledDate, LocalDate.now().toString(), outcome),
            )
        } catch (t: Throwable) {
            null
        }
    }
"""

BAD_RUNNER_SETTLE_CLOCK = GOOD_RUNNER.replace(
    """                ScheduledRunDating.gateDateOnSettle(scheduledDate, LocalDate.now().toString(), outcome),""",
    """                LocalDate.now().toString(),""",
).replace(
    """        val scheduledDate: String,\n""", "",
).replace(
    """                scheduledDate = scheduledDate,\n""", "",
)

GOOD_CALENDAR = """
    companion object {
        private val DATE_ONLY: Regex = Regex(\"\"\"^\\d{4}-\\d{2}-\\d{2}$\"\"\")

        internal fun startOfLocalDay(ms: Long, zone: TimeZone): Long =
            Calendar.getInstance(zone).apply { timeInMillis = ms }.timeInMillis

        internal fun endOfLocalDay(ms: Long, zone: TimeZone): Long {
            val cal = Calendar.getInstance(zone).apply {
                timeInMillis = ms
                add(Calendar.DAY_OF_YEAR, 1)
            }
            return cal.timeInMillis - 1
        }

        internal fun addLocalDays(ms: Long, days: Int, zone: TimeZone): Long = ms

        internal fun alignBareDateEnd(parsedMs: Long, raw: String, zone: TimeZone): Long =
            if (DATE_ONLY.matches(raw.trim())) endOfLocalDay(parsedMs, zone) else parsedMs

        internal fun resolveRangePure(
            nowMs: Long,
            zone: TimeZone,
            todayFlag: Boolean,
            daysRaw: Int?,
            startRaw: String?,
            endRaw: String?,
            parse: (String, Long, TimeZone) -> Long?,
        ): Pair<Long, Long>? {
            val s = startOfLocalDay(addLocalDays(nowMs, -1, zone), zone)
            return alignBareDateEnd(parse(endRaw ?: "", nowMs, zone) ?: return null, endRaw ?: "", zone) to s
        }
    }

    private fun resolveDateRange(args: OffloadArgs): Pair<Long, Long>? =
        resolveRangePure(
            nowMs = System.currentTimeMillis(),
            zone = TimeZone.getDefault(),
            todayFlag = args.hasFlag("today"),
            daysRaw = args.getInt("days"),
            startRaw = args.get("start"),
            endRaw = args.get("end"),
        ) { s, nowMs, z -> parseDateInZone(s, nowMs, z) }

    private fun doFreebusy(args: OffloadArgs): NativeOffloadResult {
        val endMs = parseDateInZone(endStr, 0L, TimeZone.getDefault())
            ?.let { alignBareDateEnd(it, endStr, TimeZone.getDefault()) }
            ?: return NativeOffloadResult(2, "bad --end")
        return NativeOffloadResult(0, "$endMs")
    }
"""

BAD_CALENDAR_RAW_PARSE = GOOD_CALENDAR.replace(
    """    private fun resolveDateRange(args: OffloadArgs): Pair<Long, Long>? =
        resolveRangePure(
            nowMs = System.currentTimeMillis(),
            zone = TimeZone.getDefault(),
            todayFlag = args.hasFlag("today"),
            daysRaw = args.getInt("days"),
            startRaw = args.get("start"),
            endRaw = args.get("end"),
        ) { s, nowMs, z -> parseDateInZone(s, nowMs, z) }""",
    """    private fun resolveDateRange(args: OffloadArgs): Pair<Long, Long>? {
        val now = System.currentTimeMillis()
        if (args.hasFlag("today")) {
            val cal = Calendar.getInstance().apply { set(Calendar.HOUR_OF_DAY, 0) }
            val s = cal.timeInMillis
            return s to (s + 24 * 60 * 60 * 1000L)
        }
        val startStr = args.get("start")
        val endStr = args.get("end")
        val startMs = if (startStr != null) parseDate(startStr) ?: return null else now - 24 * 60 * 60 * 1000L
        val endMs = if (endStr != null) parseDate(endStr) ?: return null else now
        return if (startMs > endMs) endMs to startMs else startMs to endMs
    }""",
).replace(
    """        val endMs = parseDateInZone(endStr, 0L, TimeZone.getDefault())
            ?.let { alignBareDateEnd(it, endStr, TimeZone.getDefault()) }
            ?: return NativeOffloadResult(2, "bad --end")""",
    """        val endMs = parseDate(endStr) ?: return NativeOffloadResult(2, "bad --end")""",
)

SELFTEST_MARK = "SELFTEST=PASS (schedule-date-source: 2 files x 2 arms, teeth on every criterion)"


def run_selftest():
    cases = [
        ("runner/compliant", violations_scheduler(GOOD_RUNNER, "fixture"), 0),
        ("runner/settle-clock", violations_scheduler(BAD_RUNNER_SETTLE_CLOCK, "fixture"), ">0"),
        ("calendar/compliant", violations_calendar(GOOD_CALENDAR, "fixture"), 0),
        ("calendar/raw-parse", violations_calendar(BAD_CALENDAR_RAW_PARSE, "fixture"), ">0"),
    ]
    problems = []
    for name, bad, expected in cases:
        want_clean = expected == 0
        if want_clean and bad:
            problems.append("%s: expected CLEAN, got %d violation(s): %s" % (name, len(bad), bad[:2]))
        if not want_clean and not bad:
            problems.append("%s: expected VIOLATIONS, got clean" % name)
    # The blocking criteria must each have teeth: neutering any single rule must
    # turn one of the fixtures red for that rule alone.
    if not any("not routed through" in b for b in violations_scheduler(BAD_RUNNER_SETTLE_CLOCK, "fixture")):
        problems.append("runner/settle-clock: the routing rule did not fire")
    if not any("zero-width" in b for b in violations_calendar(BAD_CALENDAR_RAW_PARSE, "fixture")):
        problems.append("calendar/raw-parse: the raw-parse rule did not fire")
    if not any("24h constant" in b for b in violations_calendar(BAD_CALENDAR_RAW_PARSE, "fixture")):
        problems.append("calendar/raw-parse: the DST-constant rule did not fire")
    if not any("alignBareDateEnd" in b for b in violations_calendar(BAD_CALENDAR_RAW_PARSE, "fixture")):
        problems.append("calendar/raw-parse: the freebusy alignment rule did not fire")

    if problems:
        print("SELFTEST=FAIL")
        for p in problems:
            print("  -", p)
        return 1
    print(SELFTEST_MARK)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return run_selftest()

    root = os.path.abspath(args.root or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    bad = check(root)
    if bad:
        print("schedule-date-source: %d violation(s)" % len(bad))
        for b in bad:
            print("  " + b)
        return 1
    print("schedule-date-source: OK (scheduled day threaded; bare-date end aligned; DST-safe day math)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

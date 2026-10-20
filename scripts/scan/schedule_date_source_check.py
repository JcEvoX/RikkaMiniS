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
PHOTOS_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/PhotosOffloadHandler.kt"
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

    crt = member_slice(src, "private fun doCreate(")
    if crt is None:
        bad.append("%s:1: doCreate is gone (rename => update this gate)" % label)
    else:
        if "createEndMs(" not in crt:
            bad.append("%s:1: doCreate does not route --end through createEndMs(…) — an "
                       "explicit --end that fails to parse silently falls back to the 1h "
                       "default: create answers an event in a window the caller never asked "
                       "for, and nothing says the value was rejected" % label)
        if "invalid --end" not in crt:
            bad.append("%s:1: doCreate no longer refuses an unparseable --end — the caller is "
                       "never told the value was rejected" % label)
        if re.search(r"\?: \(\s*startMs\s*\+", crt):
            bad.append("%s:1: doCreate keeps the elvis-default swallow (present-but-unusable "
                       "--end silently becomes start+1h) — the rule is bound at the decision, "
                       "not at the spelling of the default" % label)
    if "internal fun createEndMs(" not in src:
        bad.append("%s:1: createEndMs helper is gone" % label)
    else:
        cem = member_slice(src, "internal fun createEndMs(") or ""
        if "isNullOrBlank" not in cem:
            bad.append("%s:1: createEndMs no longer separates ABSENT (→ the 1h default) from "
                       "PRESENT-BUT-UNPARSEABLE (→ null → refusal): that decision point is the "
                       "whole fix — an invalid --end slips back into the default branch" % label)
        if "60L * 60 * 1000" not in cem:
            bad.append("%s:1: createEndMs lost the 1h default for an absent --end" % label)
    return bad


def violations_photos(src, label):
    """ISSUE-04(B) rules for PhotosOffloadHandler.kt: a --start/--end that decides a
    day must REFUSE a value the caller spelled wrong, never silently widen the window
    to the whole library."""
    bad = []

    rdr = member_slice(src, "private fun resolveDateRange(")
    if rdr is None:
        bad.append("%s:1: resolveDateRange is gone (rename => update this gate)" % label)
    else:
        if "resolvePhotoRange(" not in rdr:
            bad.append("%s:1: resolveDateRange must delegate to the pure core "
                       "resolvePhotoRange(…)" % label)
        own_code = strip_calls(rdr, "resolvePhotoRange(")
        if "parseDate(" in own_code:
            bad.append("%s:1: resolveDateRange parses --start/--end itself again — an "
                       "unparseable value goes back to being silently dropped and the "
                       "command answers the entire library" % label)
        if "daysAgoStartOfDayMs(" in own_code or "alignDateOnlyEndMs(" in own_code:
            bad.append("%s:1: resolveDateRange rebuilds the day math inline instead of the "
                       "shared helpers" % label)

    if "internal sealed class PhotoRange" not in src:
        bad.append("%s:1: PhotoRange is gone — the refusal outcome has no carrier, so an "
                   "invalid bound degrades to null == 'no filter' again" % label)
    if "internal fun resolvePhotoRange(" not in src:
        bad.append("%s:1: resolvePhotoRange is gone" % label)
    else:
        pure = member_slice(src, "internal fun resolvePhotoRange(") or ""
        if "PhotoRange.Invalid(" not in pure:
            bad.append("%s:1: resolvePhotoRange no longer returns an Invalid outcome for a bad "
                       "--start/--end — ISSUE-04(B) re-opens (the window silently widens)" % label)
        if "endMs < startMs" not in pure:
            bad.append("%s:1: resolvePhotoRange lost the inverted-range refusal (--start after "
                       "--end must not be served as a back-to-front window)" % label)
        for needed in ("daysAgoStartOfDayMs(", "alignDateOnlyEndMs("):
            if needed not in pure:
                bad.append("%s:1: resolvePhotoRange lost %s (the shared day-math helper)"
                           % (label, needed))

    hl = member_slice(src, "private fun handleList(")
    if hl is None:
        bad.append("%s:1: handleList is gone" % label)
    else:
        if "PhotoRange.Invalid" not in hl:
            bad.append("%s:1: handleList no longer branches on PhotoRange.Invalid — the refusal "
                       "never reaches the caller" % label)
        if "android-photos list: invalid" not in hl:
            bad.append("%s:1: handleList no longer reports which bound was invalid" % label)

    alb = member_slice(src, "private fun handleAlbum(")
    if alb is None:
        bad.append("%s:1: handleAlbum is gone" % label)
    else:
        if ("MediaStore.Images.Media.WIDTH" not in alb
                or "MediaStore.Images.Media.HEIGHT" not in alb):
            bad.append("%s:1: handleAlbum's projection no longer asks for WIDTH/HEIGHT — every "
                       "album row answers 0x0 and the caller cannot pick a size" % label)
        if 'put("width"' not in alb or 'put("height"' not in alb:
            bad.append("%s:1: handleAlbum no longer emits width/height in its JSON" % label)
    return bad


# ── [dv16-A A-01] strict date parsing ───────────────────────────────────────
# A date that decides a DAY must not come back as a DIFFERENT day: in the
# default lenient mode SimpleDateFormat silently ROLLS an out-of-range field
# over (`2026-02-30` → March 2, `2026-13-01` → next January), so a mistyped
# `--start` / `--end` / `--at` answered for another day instead of being
# refused. SessionsOffloadHandler.kt:561 is the one parser that got this right;
# these are its four siblings.
NOTIFICATION_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/NotificationOffloadHandler.kt"
)
ALARM_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/AlarmOffloadHandler.kt"
)

# (file, member marker, what the parsed value decides)
STRICT_DATE_PARSERS = (
    (PHOTOS_REL, "private fun parseDate(", "--start/--end"),
    (CALENDAR_REL, "internal fun parseDateInZone(", "--start/--end"),
    (NOTIFICATION_REL, "private fun parseIso(", "--at"),
    (ALARM_REL, "private fun parseTimeArg(", "the alarm time"),
)

STRICT_PARSE_FLAG = "isLenient = false"


def member_body(src, marker):
    """Brace-balanced text of the member declared at [marker]; None if it is not
    there. Indentation-based slicing is not enough here — the Calendar parser is
    followed by a `companion object` block, which the sibling scan would swallow
    (the flag could then live anywhere in the rest of the file)."""
    start = src.find(marker)
    if start < 0:
        return None
    line_start = src.rfind("\n", 0, start) + 1
    open_brace = src.find("{", start)
    if open_brace < 0:
        return None
    depth = 0
    i = open_brace
    while i < len(src):
        c = src[i]
        if c == '"':
            if src.startswith('"""', i):
                k = src.find('"""', i + 3)
                i = len(src) if k < 0 else k + 3
                continue
            i += 1
            while i < len(src) and src[i] != '"':
                i += 2 if src[i] == "\\" else 1
        elif c == "'":
            i += 1
            while i < len(src) and src[i] != "'":
                i += 2 if src[i] == "\\" else 1
        elif c == "/" and src.startswith("//", i):
            nxt = src.find("\n", i)
            if nxt < 0:
                break
            i = nxt
        elif c == "/" and src.startswith("/*", i):
            k = src.find("*/", i + 2)
            if k < 0:
                break
            i = k + 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[line_start:i + 1]
        i += 1
    return None


def violations_strict_date_parser(src, label, marker, what):
    """The parser must not run in the default (lenient) mode.

    Bound to the DECISION, not to a spelling: the flag may sit anywhere inside
    the parser (after the pattern list, inside the `apply {}`, before the parse
    call). Fail-closed in both directions — a parser that cannot be sliced out
    is a finding, and so is one that no longer builds a SimpleDateFormat at all
    (that is a rewrite of the decision this gate pins)."""
    bad = []
    body = member_body(src, marker)
    if body is None:
        bad.append("%s:1: %s is gone — the strict-date decision this gate pins cannot be "
                   "read; re-point the marker" % (label, marker.strip()))
        return bad
    if "SimpleDateFormat" not in body or ".parse(" not in body:
        bad.append("%s:1: %s no longer parses with a SimpleDateFormat — re-read this rule "
                   "before trusting it" % (label, marker.strip()))
        return bad
    if STRICT_PARSE_FLAG not in body:
        bad.append("%s:1: %s parses %s in the DEFAULT LENIENT mode — an out-of-range field is "
                   "rolled over (2026-02-30 → March 2) and the command answers for a DIFFERENT "
                   "day instead of refusing" % (label, marker.strip(), what))
    return bad


def check(repo_root):
    bad = []
    for rel, fn in ((SCHEDULER_REL, violations_scheduler), (CALENDAR_REL, violations_calendar),
                    (PHOTOS_REL, violations_photos)):
        path = os.path.join(repo_root, rel)
        if not os.path.isfile(path):
            bad.append("%s:1: ROT — file not found under %s" % (rel, repo_root))
            continue
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        bad.extend(fn(src, rel))
    # [dv16-A A-01] the strictness of every offload date parser, on the same
    # is-it-really-there footing: the function body is sliced out and the flag
    # must be inside it.
    for rel, marker, what in STRICT_DATE_PARSERS:
        path = os.path.join(repo_root, rel)
        if not os.path.isfile(path):
            bad.append("%s:1: ROT — file not found under %s" % (rel, repo_root))
            continue
        with open(path, encoding="utf-8") as fh:
            bad.extend(violations_strict_date_parser(fh.read(), rel, marker, what))
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

        internal fun createEndMs(rawEnd: String?, startMs: Long, parse: (String) -> Long?): Long? {
            if (rawEnd.isNullOrBlank()) return startMs + 60L * 60 * 1000
            return parse(rawEnd)
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

    private fun doCreate(args: OffloadArgs): NativeOffloadResult {
        val startMs = parseDate(startStr) ?: return NativeOffloadResult(2, "bad --start")
        val rawEnd = args.get("end")
        val endMs = createEndMs(rawEnd, startMs) { if (isAllDay) parseDateUtc(it) else parseDate(it) }
            ?: return NativeOffloadResult(2, "android-calendar create: invalid --end '$rawEnd'\\n")
        return NativeOffloadResult(0, "$startMs..$endMs")
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

# [da1032-C] ISSUE-04(A) negatives: the old shape (present-but-invalid --end falls
# back to the 1h default) must trip a rule of its own.
BAD_CALENDAR_SWALLOW_END = GOOD_CALENDAR.replace(
    "        val endMs = createEndMs(rawEnd, startMs) { if (isAllDay) parseDateUtc(it) else parseDate(it) }",
    '        val endMs = args.get("end")?.let { parseDate(it) } ?: (startMs + 60 * 60 * 1000L)',
)

BAD_CALENDAR_NO_REFUSAL = GOOD_CALENDAR.replace(
    '"android-calendar create: invalid --end',
    '"android-calendar create: bad value for',
)

BAD_CALENDAR_NULL_CHECK = GOOD_CALENDAR.replace(
    "if (rawEnd.isNullOrBlank()) return startMs + 60L * 60 * 1000",
    "if (rawEnd == null) return startMs + 60L * 60 * 1000",
)

BAD_CALENDAR_NO_DEFAULT = GOOD_CALENDAR.replace(
    "if (rawEnd.isNullOrBlank()) return startMs + 60L * 60 * 1000",
    "if (rawEnd.isNullOrBlank()) return startMs",
)

# [da1032-C] ISSUE-04(B) fixtures: a --start/--end the caller spelled wrong must be
# REFUSED, never silently widened to the whole library.
GOOD_PHOTOS = """
    private fun resolveDateRange(args: OffloadArgs): PhotoRange =
        resolvePhotoRange(
            nowMs = System.currentTimeMillis(),
            zone = TimeZone.getDefault(),
            daysRaw = args.getInt("days"),
            startRaw = args.get("start"),
            endRaw = args.get("end"),
        ) { parseDate(it) }

    private fun handleList(args: OffloadArgs): NativeOffloadResult {
        val range = when (val r = resolveDateRange(args)) {
            is PhotoRange.Window -> r
            is PhotoRange.Invalid ->
                return NativeOffloadResult(2, "android-photos list: invalid ${r.flag} '${r.raw}'")
        }
        return NativeOffloadResult(0, "${range.startMs}..${range.endMs}")
    }

    private fun handleAlbum(args: OffloadArgs): NativeOffloadResult {
        val proj = arrayOf(
            MediaStore.Images.Media._ID,
            MediaStore.Images.Media.DISPLAY_NAME,
            MediaStore.Images.Media.DATE_TAKEN,
            MediaStore.Images.Media.WIDTH,
            MediaStore.Images.Media.HEIGHT,
            MediaStore.Images.Media.SIZE,
            MediaStore.Images.Media.MIME_TYPE,
            MediaStore.Images.Media.BUCKET_ID,
            MediaStore.Images.Media.BUCKET_DISPLAY_NAME,
        )
        val out = JSONArray()
        while (c.moveToNext()) {
            out.put(JSONObject().put("id", c.getLong(0))
                .put("size", c.getLong(5))
                .put("width", c.getInt(3))
                .put("height", c.getInt(4))
                .put("mime", c.getString(6)))
        }
        return NativeOffloadResult(0, out.toString())
    }

internal sealed class PhotoRange {
    data class Window(val startMs: Long?, val endMs: Long?) : PhotoRange()
    data class Invalid(val flag: String, val raw: String) : PhotoRange()
}

internal fun resolvePhotoRange(
    nowMs: Long,
    zone: TimeZone,
    daysRaw: Int?,
    startRaw: String?,
    endRaw: String?,
    parse: (String) -> Long?,
): PhotoRange {
    if (daysRaw != null) {
        val n = if (daysRaw <= 0) 7 else daysRaw
        return PhotoRange.Window(daysAgoStartOfDayMs(nowMs, n, zone), nowMs)
    }
    val startMs = if (startRaw.isNullOrBlank()) null else parse(startRaw) ?: return PhotoRange.Invalid("--start", startRaw)
    val endMs = if (endRaw.isNullOrBlank()) {
        null
    } else {
        val ms = parse(endRaw) ?: return PhotoRange.Invalid("--end", endRaw)
        if (isDateOnly(endRaw)) alignDateOnlyEndMs(ms, zone) else ms
    }
    if (startMs != null && endMs != null && endMs < startMs) {
        return PhotoRange.Invalid("--end", endRaw.orEmpty())
    }
    return PhotoRange.Window(startMs, endMs)
}
"""

BAD_PHOTOS_OWN_PARSE = GOOD_PHOTOS.replace(
    ") { parseDate(it) }",
    ') { parseDate(it) }.also { parseDate("x") }',
)

BAD_PHOTOS_NO_REFUSAL = GOOD_PHOTOS.replace(
    'parse(startRaw) ?: return PhotoRange.Invalid("--start", startRaw)',
    'parse(startRaw) ?: null',
).replace(
    'val ms = parse(endRaw) ?: return PhotoRange.Invalid("--end", endRaw)',
    'val ms = parse(endRaw) ?: 0L',
).replace(
    'return PhotoRange.Invalid("--end", endRaw.orEmpty())',
    'return PhotoRange.Window(null, null)',
)

BAD_PHOTOS_NO_INVERT = GOOD_PHOTOS.replace("endMs < startMs", "false")

BAD_PHOTOS_ALBUM_NO_SIZE = GOOD_PHOTOS.replace(
    "            MediaStore.Images.Media.WIDTH,\n"
    "            MediaStore.Images.Media.HEIGHT,\n",
    "",
)

BAD_PHOTOS_LIST_NO_BRANCH = GOOD_PHOTOS.replace(
    """        val range = when (val r = resolveDateRange(args)) {
            is PhotoRange.Window -> r
            is PhotoRange.Invalid ->
                return NativeOffloadResult(2, "android-photos list: invalid ${r.flag} '${r.raw}'")
        }""",
    "        val range = resolveDateRange(args) as PhotoRange.Window",
)

SELFTEST_MARK = "SELFTEST=PASS (schedule-date-source: 2 files x 2 arms, teeth on every criterion)"

# ── [dv16-A A-01] fixtures for the strict-parser criterion ─────────────────
GOOD_STRICT_PARSER = """
class FakeHandler {
    private fun parseDate(s: String): Long? {
        for (p in patterns) {
            try {
                val sdf = SimpleDateFormat(p, Locale.US).apply {
                    timeZone = if (p.endsWith("'Z'")) TimeZone.getTimeZone("UTC") else TimeZone.getDefault()
                    isLenient = false
                }
                return sdf.parse(s)?.time
            } catch (e: Exception) {
                continue
            }
        }
        return null
    }
}
"""

BAD_LENIENT_PARSER = GOOD_STRICT_PARSER.replace("                    isLenient = false\n", "")

BAD_PARSER_REWRITTEN = GOOD_STRICT_PARSER.replace("SimpleDateFormat", "MyFormats")

BAD_PARSER_RENAMED = GOOD_STRICT_PARSER.replace("private fun parseDate(", "private fun parseDay(")

STRICT_PARSERS_MARK = "STRICT_DATE_PARSERS_SELFTEST=PASS (4 parsers x 4 arms)"


def run_selftest():
    cases = [
        ("runner/compliant", violations_scheduler(GOOD_RUNNER, "fixture"), 0),
        ("runner/settle-clock", violations_scheduler(BAD_RUNNER_SETTLE_CLOCK, "fixture"), ">0"),
        ("calendar/compliant", violations_calendar(GOOD_CALENDAR, "fixture"), 0),
        ("calendar/raw-parse", violations_calendar(BAD_CALENDAR_RAW_PARSE, "fixture"), ">0"),
        ("calendar/create-swallows-end", violations_calendar(BAD_CALENDAR_SWALLOW_END, "fixture"), ">0"),
        ("calendar/no-refusal", violations_calendar(BAD_CALENDAR_NO_REFUSAL, "fixture"), ">0"),
        ("calendar/absent-vs-invalid", violations_calendar(BAD_CALENDAR_NULL_CHECK, "fixture"), ">0"),
        ("calendar/no-1h-default", violations_calendar(BAD_CALENDAR_NO_DEFAULT, "fixture"), ">0"),
        ("photos/compliant", violations_photos(GOOD_PHOTOS, "fixture"), 0),
        ("photos/own-parse", violations_photos(BAD_PHOTOS_OWN_PARSE, "fixture"), ">0"),
        ("photos/no-refusal", violations_photos(BAD_PHOTOS_NO_REFUSAL, "fixture"), ">0"),
        ("photos/no-inversion-refusal", violations_photos(BAD_PHOTOS_NO_INVERT, "fixture"), ">0"),
        ("photos/album-drops-size", violations_photos(BAD_PHOTOS_ALBUM_NO_SIZE, "fixture"), ">0"),
        ("photos/list-loses-branch", violations_photos(BAD_PHOTOS_LIST_NO_BRANCH, "fixture"), ">0"),
        # [dv16-A A-01] the strict-date-parser criterion: compliant fixture is
        # clean, and a lenient / rewritten / renamed parser each trip it.
        ("parsers/strict", violations_strict_date_parser(
            GOOD_STRICT_PARSER, "fixture", "private fun parseDate(", "--start/--end"), 0),
        ("parsers/lenient", violations_strict_date_parser(
            BAD_LENIENT_PARSER, "fixture", "private fun parseDate(", "--start/--end"), ">0"),
        ("parsers/rewritten", violations_strict_date_parser(
            BAD_PARSER_REWRITTEN, "fixture", "private fun parseDate(", "--start/--end"), ">0"),
        ("parsers/renamed", violations_strict_date_parser(
            BAD_PARSER_RENAMED, "fixture", "private fun parseDate(", "--start/--end"), ">0"),
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
    # [da1032-C] ISSUE-04(A): each new Calendar rule proves itself, and the
    # compliant fixture's legit "absent --end -> 1h default" path stays clean.
    if not any("does not route --end through createEndMs" in b
               for b in violations_calendar(BAD_CALENDAR_SWALLOW_END, "fixture")):
        problems.append("calendar/create-swallows-end: the routing rule did not fire")
    if not any("keeps the elvis-default swallow" in b
               for b in violations_calendar(BAD_CALENDAR_SWALLOW_END, "fixture")):
        problems.append("calendar/create-swallows-end: the elvis-swallow rule did not fire")
    if not any("no longer refuses an unparseable --end" in b
               for b in violations_calendar(BAD_CALENDAR_NO_REFUSAL, "fixture")):
        problems.append("calendar/no-refusal: the refusal rule did not fire")
    if not any("separates ABSENT" in b
               for b in violations_calendar(BAD_CALENDAR_NULL_CHECK, "fixture")):
        problems.append("calendar/absent-vs-invalid: the decision-point rule did not fire")
    if not any("lost the 1h default" in b
               for b in violations_calendar(BAD_CALENDAR_NO_DEFAULT, "fixture")):
        problems.append("calendar/no-1h-default: the default rule did not fire")
    # [da1032-C] ISSUE-04(B): each Photos rule proves itself; the two precision
    # controls pin that a delegating/emitting fixture is NOT flagged.
    if not any("parses --start/--end itself again" in b
               for b in violations_photos(BAD_PHOTOS_OWN_PARSE, "fixture")):
        problems.append("photos/own-parse: the inline-parse rule did not fire")
    if any("must delegate to the pure core" in b
           for b in violations_photos(BAD_PHOTOS_OWN_PARSE, "fixture")):
        problems.append("photos/own-parse: the delegate rule fired on a delegating fixture")
    if not any("no longer returns an Invalid outcome" in b
               for b in violations_photos(BAD_PHOTOS_NO_REFUSAL, "fixture")):
        problems.append("photos/no-refusal: the Invalid-outcome rule did not fire")
    if any("inverted-range refusal" in b
           for b in violations_photos(BAD_PHOTOS_NO_REFUSAL, "fixture")):
        problems.append("photos/no-refusal: the inversion rule fired on a fixture that kept it")
    if not any("inverted-range refusal" in b
               for b in violations_photos(BAD_PHOTOS_NO_INVERT, "fixture")):
        problems.append("photos/no-inversion-refusal: the inversion rule did not fire")
    if not any("projection no longer asks for WIDTH/HEIGHT" in b
               for b in violations_photos(BAD_PHOTOS_ALBUM_NO_SIZE, "fixture")):
        problems.append("photos/album-drops-size: the projection rule did not fire")
    if any("no longer emits width/height" in b
           for b in violations_photos(BAD_PHOTOS_ALBUM_NO_SIZE, "fixture")):
        problems.append("photos/album-drops-size: the emit rule fired on a fixture that kept it")
    if not any("branches on PhotoRange.Invalid" in b
               for b in violations_photos(BAD_PHOTOS_LIST_NO_BRANCH, "fixture")):
        problems.append("photos/list-loses-branch: the branch rule did not fire")
    if not any("reports which bound was invalid" in b
               for b in violations_photos(BAD_PHOTOS_LIST_NO_BRANCH, "fixture")):
        problems.append("photos/list-loses-branch: the refusal-message rule did not fire")

    # [dv16-A A-01] each branch of the strict-parser criterion proves itself,
    # and precise failure modes are distinguished (lenient vs rewritten vs
    # renamed) so a neutered rule cannot pass as "some finding fired".
    if not any("DEFAULT LENIENT mode" in b
               for b in violations_strict_date_parser(BAD_LENIENT_PARSER, "fixture",
                                                      "private fun parseDate(", "--start/--end")):
        problems.append("parsers/lenient: the lenient-mode rule did not fire")
    if any("DEFAULT LENIENT mode" in b
           for b in violations_strict_date_parser(GOOD_STRICT_PARSER, "fixture",
                                                  "private fun parseDate(", "--start/--end")):
        problems.append("parsers/strict: the lenient rule fired on the compliant fixture")
    if not any("no longer parses with a SimpleDateFormat" in b
               for b in violations_strict_date_parser(BAD_PARSER_REWRITTEN, "fixture",
                                                      "private fun parseDate(", "--start/--end")):
        problems.append("parsers/rewritten: the decision-rewrite rule did not fire")
    if not any("is gone" in b
               for b in violations_strict_date_parser(BAD_PARSER_RENAMED, "fixture",
                                                      "private fun parseDate(", "--start/--end")):
        problems.append("parsers/renamed: the fail-closed marker rule did not fire")
    # …and the flag may sit anywhere inside the parser, not only in the apply{}:
    if violations_strict_date_parser(
            GOOD_STRICT_PARSER.replace(
                "                    isLenient = false\n", "").replace(
                "                return sdf.parse(s)?.time",
                "                sdf.isLenient = false\n                return sdf.parse(s)?.time"),
            "fixture", "private fun parseDate(", "--start/--end"):
        problems.append("parsers/anywhere: a flag outside the apply{} was reported as missing")

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

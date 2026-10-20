#!/usr/bin/env python3
"""[da1017-D1] Gate: never filter CalendarContract.Events on a NULL-able column.

Mechanism the gate protects (measured on-device 2026-10-16, Android 15):
the Calendar provider stores recurring events as RRULE + DURATION and leaves
`Events.DTEND` NULL — it rejects both together (CalendarProvider2.validateEventData:
"Cannot have both DTEND and DURATION in an event"). A provider query whose
selection compares DTEND (`dtend < ?`, `dtend > ?`, `dtend = ?`) is therefore
never true for those rows, and the caller silently reports the *opposite* of the
truth (freebusy answered "free" for a window holding a weekly occurrence).

Rules (all four live in this one script and share its discovery helpers; they
are all "a calendar/photos decision must not silently answer something the
caller did not ask for"):
  A. No SQL predicate (a string literal that couples an Events.DTEND reference
     with a comparison operator and a bind placeholder) may exist anywhere in
     src/android/app/src/main/java, unless the same literal guards NULL
     (`IS NULL`). Predicates must be built on CalendarContract.Instances
     (BEGIN/END), which the provider expands for recurring rows.
  B. The `freebusy` code path of the calendar offload handler must read the
     instances window (Instances.CONTENT_URI + the shared selection helper) and
     must not fall back to Events.

[dv14-A3] additions — two contracts the offload date/permission surface kept
breaking, on the files this batch owns:
  D. `day-window-refusal` — the pure day-window resolver (discovered by SHAPE:
     returns `PhotoRange` / `Pair<Long, Long>` and takes an injected parse
     callback) must carry the raw `--days` string and REFUSE a supplied-but-
     unreadable value (`toIntOrNull() == null`). `getInt("days")` answers null
     both for "flag absent" and for "`--days 7d`": Photos then answered the
     ENTIRE library and Calendar silently narrowed to the last 24 hours. Judged
     on PhotosOffloadHandler.kt and CalendarOffloadHandler.kt only.
  E. `write-permission-parity` — a subcommand entry point (takes
     `args: OffloadArgs`) that writes the calendar provider must gate on BOTH
     `hasRead()` and `hasWrite()` and must request both permissions (every
     calendar write reads the provider first, so WRITE alone asks the user for
     one permission and then fails on another); read-only entry points must not
     ask for WRITE at all.

Same discipline as rules A/B: every criterion is fail-closed (remove the
machinery and the rule goes red rather than silent) and every selftest arm
asserts its OWN rule code, so an arm that trips a neighbour cannot look green.

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
PHOTOS_REL = os.path.join(
    MAIN_ROOT, "com", "rikkaminis", "app", "sandbox", "offload", "PhotosOffloadHandler.kt"
)

# String literals: single-line "..." (no newline) and """...""" blocks.
LITERAL_RE = re.compile(r'"""(?:.|\n)*?"""|"(?:[^"\\\n]|\\.)*"')
COMPARISON_RE = re.compile(r"[<>=!]+\s*[?$]")
NULL_GUARD_RE = re.compile(r"IS\s+NULL", re.IGNORECASE)
# Comments are stripped before the rule-C read-path check: the gate must judge
# the decision point, not prose that merely names it (the handler legitimately
# explains IN a comment why the read path does NOT refuse).
COMMENT_RE = re.compile(r"//[^\n]*|/\*(?:.|\n)*?\*/")

def strip_comments(src):
    return COMMENT_RE.sub("", src)

RULE_A_CODE = 'predicate-on-nullable-column'
RULE_C_CODE = 'explicit-calendar-selector'
RULE_D_CODE = 'day-window-refusal'
RULE_E_CODE = 'write-permission-parity'

# [dv14-A3] Shape-based discovery of `fun` declarations, so the new rules can be
# written against a *decision* instead of a helper's spelling: rename the pure
# core and the rule still finds it.
FUN_RE = re.compile(
    r"^(?P<indent>[ ]*)(?:private |internal |public |protected |override |final |inline |suspend )*"
    r"fun\s+(?P<name>\w+)\s*\("
)
# The two window types the offload date cores return (Photos / Calendar).
# `PhotoRange` carries a refusal case, `Pair<Long, Long>?` uses null for it.
DAYS_WINDOW_RET_RE = re.compile(r"^(?:PhotoRange|Pair<Long, Long>)\??$")
# A function-type parameter (`parse: (String) -> Long?`) — the injected parser of
# a *pure* date core. It is what tells the decision core apart from the thin
# member that merely forwards the parsed args.
INJECTED_PARSER_RE = re.compile(r"->")
DAY_PARAM_RE = re.compile(r"\bdays\w*\s*:\s*String\?")
DAY_RAW_READ_RE = re.compile(r"\bdays\w*\s*=\s*args\.get\(\s*\"days\"\s*\)")
UNREADABLE_DAYS_GUARD_RE = re.compile(r"toIntOrNull\(\)\s*==\s*null")
REFUSAL_RE = re.compile(r"return null|Invalid\(")
ARGS_PARAM_RE = re.compile(r"\bargs\s*:\s*OffloadArgs\b")
WRITE_CALL_RE = re.compile(r"contentResolver\.(?:insert|update|delete)\(")
QUERY_CALL_RE = re.compile(r"contentResolver\.query\(")
# Ends a member's slice: the next declaration at the same or a smaller indent.
# (A plain `fun` test is not enough — a TOP-LEVEL function is followed by an
# indented class declaration, and a slice that ignores it swallows the file.)
DECL_BREAK_RE = re.compile(
    r"^(?:/\*\*|@|(?:private|internal|public|protected|override|final|inline|suspend|abstract|open|sealed|data|enum|annotation) )*"
    r"(?:fun|class|object|interface|val|var|companion)\b"
)


def fun_decls(src):
    """Every `fun` in [src] as a dict: name / indent / sig / ret / body / line.

    The body runs to the next declaration at the same or a smaller indent, so a
    member's slice never swallows its siblings. Discovery only — no name lists.
    """
    lines = src.split("\n")
    decls = []
    i = 0
    while i < len(lines):
        m = FUN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group("indent"))
        sig = lines[i]
        depth = sig.count("(") - sig.count(")")
        j = i
        while depth > 0 and j + 1 < len(lines):
            j += 1
            sig += "\n" + lines[j]
            depth += lines[j].count("(") - lines[j].count(")")
        ret_m = re.search(r"\)\s*:\s*([^={\n]+)", sig)
        body_lines = []
        k = j + 1
        while k < len(lines):
            line = lines[k]
            stripped = line.strip()
            if stripped and (len(line) - len(line.lstrip(" "))) <= indent and DECL_BREAK_RE.match(stripped):
                break
            body_lines.append(line)
            k += 1
        decls.append({
            "name": m.group("name"),
            "indent": indent,
            "sig": sig,
            "ret": (ret_m.group(1).strip() if ret_m else ""),
            "body": "\n".join(body_lines),
            "line": i + 1,
        })
        i = k
    return decls


def rule_d(src):
    """[dv14-A3] ISSUE-05 — a supplied-but-unreadable `--days` must be REFUSED,
    never read as absent.

    `args.getInt("days")` answers null both for "the flag was not given" and for
    "`--days 7d`" — the second case used to fall through to the no-filter window
    (Photos: the ENTIRE library, exit 0) or to the 24h default (Calendar: six of
    the seven days the caller named, silently dropped).

    The *day-window resolver* is discovered by shape: it returns one of the two
    window types AND takes an injected parse callback (the pure core, as opposed
    to the `args`-forwarding member). Every criterion is fail-closed — remove the
    machinery and the rule goes red rather than silent.
    """
    problems = []
    # Scope: a file that never names the `--days` flag has no day-window surface
    # to protect (the minimal rule-A/B fixtures are deliberately free of it).
    # Once the flag is handled, every criterion below is fail-closed.
    if '"days"' not in src:
        return []
    resolvers = [d for d in fun_decls(src) if DAYS_WINDOW_RET_RE.match(d["ret"]) and INJECTED_PARSER_RE.search(d["sig"])]
    if not resolvers:
        return ["no pure day-window resolver found (a function returning PhotoRange / Pair<Long, Long> with an "
                "injected parse callback) — the --days decision has no core to hold the refusal"]
    if not DAY_RAW_READ_RE.search(src):
        problems.append("the handler never reads the RAW --days string (args.get(\"days\")) — getInt() alone "
                        "cannot tell 'flag absent' from 'supplied but unreadable'")
    # Overloads share a name (Photos keeps a positional back-compat shape next to
    # the daysText core), so the criterion is per NAME: at least one same-named
    # resolver must carry the raw parameter and the refusal. A pass-through
    # overload cannot hold a guard, and dropping the guard from the real core
    # leaves the name without one — red either way.
    groups = {}
    for d in resolvers:
        groups.setdefault(d["name"], []).append(d)
    for name in sorted(groups):
        members = groups[name]
        with_param = [d for d in members if DAY_PARAM_RE.search(d["sig"])]
        line = members[0]["line"]
        if not with_param:
            problems.append(f"{name} (line {line}) takes no raw `days…: String?` parameter — an unreadable "
                            "--days stays indistinguishable from an absent one")
            continue
        if not any(UNREADABLE_DAYS_GUARD_RE.search(d["body"]) for d in with_param):
            problems.append(f"{name} (line {line}) has no `toIntOrNull() == null` guard — a non-integer "
                            "--days is not refused")
        elif not any(REFUSAL_RE.search(d["body"]) for d in with_param):
            problems.append(f"{name} (line {line}) detects an unreadable --days but never returns a "
                            "refusal (null / Invalid)")
    return problems


def rule_e(src):
    """[dv14-A3] ISSUE-06 — the write paths need READ as well as WRITE, and the
    permission they REQUEST must be the permission they DECIDE on.

    Every calendar write first reads the provider (listWritableCalendars queries
    Calendars.CONTENT_URI, findEventRow queries Events.CONTENT_URI); with only
    WRITE_CALENDAR granted those reads throw SecurityException which the read
    helpers swallow, so the write reported `calendar_no_account` / `not_found`
    while `satisfied = { hasWrite() }` claimed the permission was fine.

    Discovery is shape-based: "a subcommand entry point" is a function taking
    `args: OffloadArgs`; "a write path" is one that inserts/updates/deletes on
    the provider (the reminder helpers take no args and are gated by their
    caller, so they are not entry points). Read-only entry points must NOT ask
    for WRITE — that is the minimum-permission half of the same rule.
    """
    problems = []
    for d in fun_decls(src):
        if not ARGS_PARAM_RE.search(d["sig"]):
            continue
        body = strip_comments(d["body"])
        if WRITE_CALL_RE.search(body):
            if not re.search(r"satisfied\s*=\s*\{[^}]*hasRead\(\)", body) or \
               not re.search(r"satisfied\s*=\s*\{[^}]*hasWrite\(\)", body):
                problems.append(f"{d['name']} (line {d['line']}) writes the calendar provider but its "
                                "`satisfied` predicate does not check BOTH hasRead() and hasWrite()")
            if "READ_CALENDAR" not in body or "WRITE_CALENDAR" not in body:
                problems.append(f"{d['name']} (line {d['line']}) requests a permission set that does not match "
                                "what it decides on (both READ_CALENDAR and WRITE_CALENDAR must be requested)")
        elif QUERY_CALL_RE.search(body):
            if "WRITE_CALENDAR" in body or "needsWrite = true" in body:
                problems.append(f"{d['name']} (line {d['line']}) is a read-only subcommand but asks for / gates on "
                                "WRITE — the read paths must stay minimum-permission")
    return problems


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


def rule_c(src):
    """[da1034-A4] ISSUE-07 — a WRITE that NAMES a calendar must refuse, not
    silently write somewhere else.

    `--calendar <name>` / `--calendar-id <id>` used to fall through to the
    auto-pick heuristic when it matched no writable calendar, so `create` wrote
    into a calendar the caller never named and `update` MOVED an existing event
    into the auto-picked one. Scoped to the handler file (like rule_b) and gated
    on the write surface being present, so the older minimal freebusy fixtures
    stay unaffected.

    Every criterion is anchored on a decision point rather than a spelling, and
    each is fail-closed: rename the machinery away and the rule goes red instead
    of going silent.
    """
    if "fun doCreate(" not in src and "fun doUpdate(" not in src:
        return []
    problems = []
    if "resolveExplicitCalendar(" not in src:
        problems.append("resolveExplicitCalendar() is gone — the explicit-selector decision has no single source")
    if not re.search(r"CalendarSelection\.Unmatched\s*->", src):
        problems.append("no branch consumes CalendarSelection.Unmatched — a selector miss is not decided anywhere")
    if "CalendarTarget.Refused(" not in src:
        problems.append("CalendarTarget.Refused( is never constructed — a selector miss does not become a refusal")
    if "calendar_not_found" not in src:
        problems.append("no calendar_not_found payload — the refusal is not structured")
    if "pickWritableCalendar()" in src:
        problems.append("pickWritableCalendar() is called with no calendars — the old silent auto-pick fallback")
    if "falling back to auto-pick" in src:
        problems.append("the 'falling back to auto-pick' log line is back")
    for marker, resolver in (("private fun doCreate(", "requireCalendarTarget("),
                             ("private fun doUpdate(", "optionalCalendarTarget(")):
        body = member_slice(src, marker)
        if body is None:
            problems.append(f"{marker[:-1]} not found — the write surface moved")
        elif resolver not in body:
            problems.append(f"{marker[:-1]} does not route through {resolver}")
    listing = member_slice(src, "private fun doList(")
    if listing is not None:
        listing = strip_comments(listing)
        if ("calendar_not_found" in listing or "CalendarTarget.Refused(" in listing
                or "CalendarSelection.Unmatched" in listing):
            problems.append("doList refuses an unmatched --calendar — on the read path a miss is a"
                            " legitimate empty answer, not a typo to refuse")
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
            for problem in rule_c(src):
                failures.append(f"{rel}: [{RULE_C_CODE}] {problem}")
            for problem in rule_d(src):
                failures.append(f"{rel}: [{RULE_D_CODE}] {problem}")
            for problem in rule_e(src):
                failures.append(f"{rel}: [{RULE_E_CODE}] {problem}")
        elif os.path.normpath(rel) == os.path.normpath(PHOTOS_REL):
            # [dv14-A3] rule D is the same contract on the photos side; rule E
            # is calendar-only (permissions parity), rule A/B/C never applied here.
            for problem in rule_d(src):
                failures.append(f"{rel}: [{RULE_D_CODE}] {problem}")
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

# [da1034-A4] ISSUE-07 fixtures — a write that names a calendar must refuse
# instead of falling through to the auto-pick heuristic.
GREEN_SELECTOR_HANDLER = '''
package x

internal sealed class CalendarSelection {
    data class Matched(val id: Long) : CalendarSelection()
    object Unmatched : CalendarSelection()
}

internal fun resolveExplicitCalendar(cals: List<CalendarRef>, calendarId: Long?, calendarName: String?): CalendarSelection? {
    if (calendarId == null && calendarName == null) return null
    if (calendarId != null) {
        return if (cals.any { it.id == calendarId }) CalendarSelection.Matched(calendarId) else CalendarSelection.Unmatched
    }
    return CalendarSelection.Unmatched
}

class CalendarOffloadHandler {
    private fun doCreate(args: OffloadArgs): NativeOffloadResult {
        val calendarId = when (val target = requireCalendarTarget(args)) {
            is CalendarTarget.Id -> target.value
            is CalendarTarget.Refused -> return target.result
        }
        return NativeOffloadResult(0, calendarId.toString())
    }

    private fun doUpdate(args: OffloadArgs): NativeOffloadResult {
        when (val target = optionalCalendarTarget(args)) {
            null -> Unit
            is CalendarTarget.Id -> values.put(CalendarContract.Events.CALENDAR_ID, target.value)
            is CalendarTarget.Refused -> return target.result
        }
        return NativeOffloadResult(0, "ok")
    }

    private fun doList(args: OffloadArgs): NativeOffloadResult {
        val calFilter = args.get("calendar")
        return NativeOffloadResult(0, calFilter ?: "")
    }

    private fun requireCalendarTarget(args: OffloadArgs): CalendarTarget {
        when (val sel = resolveExplicitCalendar(refs, args.getLong("calendar-id"), args.get("calendar"))) {
            is CalendarSelection.Matched -> return CalendarTarget.Id(sel.id)
            is CalendarSelection.Unmatched -> return CalendarTarget.Refused(calendarNotFoundError(args))
            null -> Unit
        }
        return CalendarTarget.Id(pickWritableCalendar(cals) ?: 0L)
    }

    private fun calendarNotFoundError(args: OffloadArgs): NativeOffloadResult =
        NativeOffloadResult(1, OffloadOutput.formatBody(JSONObject().put("error", "calendar_not_found").toString(), args))

    private fun pickWritableCalendar(cals: JSONArray): Long? = null

    private sealed class CalendarTarget {
        data class Id(val value: Long) : CalendarTarget()
        data class Refused(val result: NativeOffloadResult) : CalendarTarget()
    }
}
'''

SELECTOR_FALLBACK_HANDLER = GREEN_SELECTOR_HANDLER.replace(
    '''        val calendarId = when (val target = requireCalendarTarget(args)) {
            is CalendarTarget.Id -> target.value
            is CalendarTarget.Refused -> return target.result
        }''',
    '        val calendarId = pickWritableCalendar()')

SELECTOR_UNHANDLED_HANDLER = GREEN_SELECTOR_HANDLER.replace(
    "            is CalendarSelection.Unmatched -> return CalendarTarget.Refused(calendarNotFoundError(args))",
    "            is CalendarSelection.Unmatched -> Unit")

SELECTOR_FILTER_REFUSES_HANDLER = GREEN_SELECTOR_HANDLER.replace(
    '        val calFilter = args.get("calendar")',
    '        val calFilter = args.get("calendar") ?: "calendar_not_found"')

SELECTOR_RENAMED_PAYLOAD_HANDLER = GREEN_SELECTOR_HANDLER.replace(
    '"calendar_not_found"', '"not_found"')

SELECTOR_RENAMED_DECISION_HANDLER = GREEN_SELECTOR_HANDLER.replace(
    "resolveExplicitCalendar(", "resolveCalendarSelector(")


# [dv14-A3] rules D + E fixtures. A synthetic handler carrying: a pure
# day-window core with the raw-string guard, its `args`-forwarding member, one
# write entry point that gates on read+write, and one read-only entry point.
GREEN_DAYS_PERM_HANDLER = '''
package x

internal fun resolveRangeFixture(
    nowMs: Long,
    daysRaw: Int?,
    daysText: String? = null,
    parse: (String, Long, TimeZone) -> Long?,
): Pair<Long, Long>? {
    if (daysText != null && daysText.isNotBlank() && daysText.toIntOrNull() == null) {
        return null
    }
    return null
}

class CalendarOffloadHandler {
    private fun resolveDateRange(args: OffloadArgs): Pair<Long, Long>? =
        resolveRangeFixture(
            nowMs = 0L,
            daysRaw = args.getInt("days"),
            daysText = args.get("days"),
        ) { s, now, z -> null }

    private fun doCreate(args: OffloadArgs): NativeOffloadResult {
        ensurePermission(
            permissions = listOf(Manifest.permission.WRITE_CALENDAR, Manifest.permission.READ_CALENDAR),
            satisfied = { calendarSatisfied(hasRead(), hasWrite(), needsWrite = true) },
            args = args,
        )?.let { return it }
        val uri = context.contentResolver.insert(CalendarContract.Events.CONTENT_URI, values)
        return NativeOffloadResult(0, uri.toString())
    }

    private fun doList(args: OffloadArgs): NativeOffloadResult {
        ensurePermission(
            permissions = listOf(Manifest.permission.READ_CALENDAR),
            satisfied = { hasRead() },
            args = args,
        )?.let { return it }
        context.contentResolver.query(CalendarContract.Instances.CONTENT_URI, projection, null, null, null)
        return NativeOffloadResult(0, "ok")
    }
}
'''

DAYS_GUARD_REMOVED = GREEN_DAYS_PERM_HANDLER.replace(
    '''    if (daysText != null && daysText.isNotBlank() && daysText.toIntOrNull() == null) {
        return null
    }
''', "")

DAYS_PARAM_REMOVED = GREEN_DAYS_PERM_HANDLER.replace(
    "    daysText: String? = null,\n", "")

DAYS_WIRING_REMOVED = GREEN_DAYS_PERM_HANDLER.replace(
    '            daysText = args.get("days"),\n', "")

DAYS_NO_CORE = GREEN_DAYS_PERM_HANDLER.replace(
    '''internal fun resolveRangeFixture(
    nowMs: Long,
    daysRaw: Int?,
    daysText: String? = null,
    parse: (String, Long, TimeZone) -> Long?,
): Pair<Long, Long>? {
    if (daysText != null && daysText.isNotBlank() && daysText.toIntOrNull() == null) {
        return null
    }
    return null
}

''', "")

# The pre-fix write shape: both permissions requested, only WRITE decided on.
PERM_WRITE_ONLY = GREEN_DAYS_PERM_HANDLER.replace(
    "satisfied = { calendarSatisfied(hasRead(), hasWrite(), needsWrite = true) }",
    "satisfied = { hasWrite() }")

# Decision and request drift apart: gated on both, but READ is never requested.
PERM_REQUEST_DRIFT = GREEN_DAYS_PERM_HANDLER.replace(
    "permissions = listOf(Manifest.permission.WRITE_CALENDAR, Manifest.permission.READ_CALENDAR),",
    "permissions = listOf(Manifest.permission.WRITE_CALENDAR),")

PERM_NO_GATE = GREEN_DAYS_PERM_HANDLER.replace(
    '''        ensurePermission(
            permissions = listOf(Manifest.permission.WRITE_CALENDAR, Manifest.permission.READ_CALENDAR),
            satisfied = { calendarSatisfied(hasRead(), hasWrite(), needsWrite = true) },
            args = args,
        )?.let { return it }
''', "")

PERM_READ_PATH_ASKS_WRITE = GREEN_DAYS_PERM_HANDLER.replace(
    '''            permissions = listOf(Manifest.permission.READ_CALENDAR),
            satisfied = { hasRead() },''',
    '''            permissions = listOf(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR),
            satisfied = { hasRead() },''')

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
    # [da1034-A4] rule C — one control plus one arm per criterion. Each arm
    # asserts rule C specifically (not merely "something went red"): an arm that
    # trips rule A/B instead would leave rule C toothless and still look green.
    selector_cases = [
        ("selector machinery intact (rule C control)", GREEN_SELECTOR_HANDLER, False),
        ("a write falls back to the auto-pick", SELECTOR_FALLBACK_HANDLER, True),
        ("the selector miss stops being handled", SELECTOR_UNHANDLED_HANDLER, True),
        ("the read path (list filter) starts refusing", SELECTOR_FILTER_REFUSES_HANDLER, True),
        ("the refusal payload loses its name", SELECTOR_RENAMED_PAYLOAD_HANDLER, True),
        ("the selector decision loses its single source", SELECTOR_RENAMED_DECISION_HANDLER, True),
    ]
    for label, body, expect_red in selector_cases:
        root = tempfile.mkdtemp(prefix="calgate-c-")
        try:
            write_fixture(root, "CalendarOffloadHandler.kt", body, handler_rel=True)
            failures = run(root, verbose=False)
            hit = [f for f in failures if RULE_C_CODE in f]
            passed = (len(hit) > 0) == expect_red
            print(f"[{'PASS' if passed else 'FAIL'}] selftest: {label} "
                  f"(rule C expected {'red' if expect_red else 'green'}, got {len(hit)} rule-C violation(s))")
            for f in hit:
                print(f"        {f}")
            ok = ok and passed
        finally:
            shutil.rmtree(root, ignore_errors=True)
    ok = ok and passed
    # [dv14-A3] rules D + E — one control plus one arm per criterion, each arm
    # asserting its OWN rule code (an arm that trips another rule instead would
    # leave D/E toothless and still look green).
    for label, body, rule, expect_red in (
        ("days+permissions intact (rule D/E control)", GREEN_DAYS_PERM_HANDLER, None, False),
        ("the unreadable-days guard is gone", DAYS_GUARD_REMOVED, RULE_D_CODE, True),
        ("the raw days parameter is gone", DAYS_PARAM_REMOVED, RULE_D_CODE, True),
        ("the raw days string is never read", DAYS_WIRING_REMOVED, RULE_D_CODE, True),
        ("the day-window core is gone", DAYS_NO_CORE, RULE_D_CODE, True),
        ("a write gates on hasWrite() alone", PERM_WRITE_ONLY, RULE_E_CODE, True),
        ("a write requests a permission set it does not decide on", PERM_REQUEST_DRIFT, RULE_E_CODE, True),
        ("a write entry point has no permission gate", PERM_NO_GATE, RULE_E_CODE, True),
        ("a read-only subcommand asks for WRITE", PERM_READ_PATH_ASKS_WRITE, RULE_E_CODE, True),
    ):
        root = tempfile.mkdtemp(prefix="calgate-de-")
        try:
            write_fixture(root, "CalendarOffloadHandler.kt", body, handler_rel=True)
            failures = run(root, verbose=False)
            if rule is None:
                hit = [f for f in failures if RULE_D_CODE in f or RULE_E_CODE in f]
                passed = not expect_red and not hit
            else:
                hit = [f for f in failures if rule in f]
                passed = (len(hit) > 0) == expect_red
            print(f"[{'PASS' if passed else 'FAIL'}] selftest: {label} "
                  f"({'rule D/E' if rule is None else rule} expected {'red' if expect_red else 'green'}, "
                  f"got {len(hit)} violation(s))")
            for f in hit:
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

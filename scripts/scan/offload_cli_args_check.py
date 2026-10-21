#!/usr/bin/env python3
"""[da1019-J / ISSUE-10] Offload CLI argv + relative-path gate.

Pins the two shapes this branch fixed, so neither can silently regress:

  1. ContactsOffloadHandler must dispatch on the PARSED positional list
     (`parseContactsCommand`), never on the raw argv: no `rawArgs[0]`, no
     `rawArgs.drop(1)` subcommand body args, no `args.firstOrNull()` id
     lookup, and the raw-argv `--max` scan is gone (replaced by
     `listMax(parsedArgs)`).
  2. ConfigOffloadHandler's `--file` resolver must take the guest cwd, do the
     relative join in the pure `resolveMaybeRelative` helper, still route the
     joined path through `PRootKernel.resolvePathForSession` (fail-closed),
     and ALL THREE call sites must pass `request.cwd`.

[da1020-C] additions (relative paths + tar prefix robustness):

  3. BrowserUseOffloadHandler must take the guest cwd and do the relative
     join for `--cookies-file` and `--paths` BEFORE the session-aware
     resolver sees the path (`resolveMaybeRelative` / `resolveMaybeRelativeAll`
     first, then `resolvePathForSession`): a relative token must never reach
     the resolver raw.
  4. FastioOffloadHandler's tar verdict (`isTarCommand`) and the expander
     (`normalizeTarShorts`) must both call the SAME `firstSubcommandToken`
     helper — the tar flag set and the short-option expansion can never
     drift apart again (they used to be two copies of `firstOrNull() ==
     "tar"`, which broke whenever a global output flag preceded the
     subcommand).

[da1026-B] additions (ISSUE-02 — the global boolean-flag set, and the
handler that forgot to declare its own):

  5. `OffloadOutput.OUTPUT_FLAGS` must recognize `help` / `h`. `OffloadArgs`
     merges that set into every handler's `booleanFlags`, so ONLY a name
     listed here (or declared by the handler itself) is exempt from the
     greedy `--key value` branch. `--help <sub>` is the same shape as
     `--compact list`: with `help` missing from the set, `--help list`
     parsed as `--help = "list"`, `hasFlag("h", "help")` stayed false and the
     handler fell through to its DEFAULT subcommand and ran it for real
     (calendar/clipboard/location/notification/device …). Fail-closed: no
     `val OUTPUT_FLAGS … = setOf(…)` declaration found → red.
  6. `SpeakOffloadHandler` must declare `--stop` / `--status` as boolean
     flags: both are read with `hasFlag`, and an undeclared flag is eaten as
     a `--key value` pair (`android-speak --stop now` stored "now" as the
     value of `--stop`, so the stop branch was never reached).

[da1033-D] addition (ISSUE-04 — the payload that reaches `formatBody` is JSON):

  7. `PhotosOffloadHandler` / `ContactsOffloadHandler` must never hand
     `OffloadOutput.formatBody` a *bare* string literal, and must never
     `return` one from a payload helper. `formatBody` only re-shapes JSON —
     prose passes straight through, so `android-photos near` (empty radius)
     and `android-contacts search|get|list` (empty result / no cursor) used to
     exit 0 with an English sentence and break every downstream `jq` /
     `json.loads`; `near` additionally returned a bare JSONArray when it was
     NOT empty, so even the top-level type flipped with the result. Accepted
     shapes: variables, expressions (`JSONObject()…toString()`,
     `arr.toString(2)`, a helper call) and literals that already carry `{` /
     `[`. Judged on code-only text, so a prose payload quoted in a comment
     cannot satisfy OR trip the rule. Two spellings are pinned because the
     Photos payload is *returned* by a String-returning helper and therefore
     arrives at the call site as a variable: N1 (first argument) and N2
     (`return <literal>`).

[da1034-A4] addition (ISSUE-08 — the same payload contract, in the rest of
   the offload surface; and N2 is now scoped to the *helper*):

  8. The payload-file list grows from two handlers to eight: `near`'s defect
     was not unique to Photos. Location's `geocode` printed an English
     sentence on an empty geocode (exit 0) and bare address lines otherwise —
     a top-level type that flipped with the result; its uncaught handler, the
     notification/clipboard success messages, Open's `Opened: <url>` and
     Fastio's `usage()` all handed prose to `formatBody`. Every one of them
     must now carry JSON.
  9. N2 judges the *helper*, not the whole file: a `return <literal>` only
     counts when the function is the one whose result reaches `formatBody`
     (`formatBody(nearPhotos(…))`). The whole-file reading demanded ten
     rewrites of `FastioOffloadHandler`'s reason-code pairs
     (`return "not_found" to "…"`), which are not payloads — a rule that
     judges spelling instead of the defect. N1 stays whole-file.

Comment-aware: judgements run on code-only text, so a comment quoting a
forbidden pattern cannot satisfy the gate (and deleting a pinned line cannot
be masked by prose).

Usage:
  python3 scripts/scan/offload_cli_args_check.py <repo_root>   # check the tree
  python3 scripts/scan/offload_cli_args_check.py <repo_root> --selftest
Exit 0 = pass, 1 = violation (or selftest failure).
Success marker on selftest: OFFLOAD_CLI_ARGS_SELFTEST=PASS
"""
import re
import sys
from pathlib import Path

CONTACTS = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ContactsOffloadHandler.kt"
CONFIG = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ConfigOffloadHandler.kt"
BROWSERUSE = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/BrowserUseOffloadHandler.kt"
FASTIO = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/FastioOffloadHandler.kt"
OUTPUT = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/OffloadOutput.kt"
SPEAK = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/SpeakOffloadHandler.kt"
PHOTOS = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/PhotosOffloadHandler.kt"
NOTIFICATION = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/NotificationOffloadHandler.kt"
# [dv15-A1 A-01/A-02] the shared parser every handler goes through.
ARGS = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/OffloadArgs.kt"

# [da1034-A4] ISSUE-08: every handler that ships a `formatBody` payload. Kept
# explicit rather than a glob over the offload dir: a glob would also drag in
# the exit-2 `"…\n$HELP"` strings (Shizuku, ModelUse …) that deliberately never
# touch formatBody, and `payload_findings` would flag them.
PAYLOAD_FILES = [
    PHOTOS,
    CONTACTS,
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/CalendarOffloadHandler.kt",
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/LocationOffloadHandler.kt",
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/NotificationOffloadHandler.kt",
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ClipboardOffloadHandler.kt",
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/OpenOffloadHandler.kt",
    FASTIO,
]

# (id, kind, needle) — kind: "req" exact substring / "forbid" exact substring /
# "req_re" / "forbid_re" required-or-forbidden regex / "count_re" (needle = (re, n))
CONTACTS_RULES = [
    ("C1", "req", "val cmd = parseContactsCommand(rawArgs)"),
    ("C2", "req", 'val needsWrite = cmd.sub == "delete"'),
    ("C3", "req", "when (val sub = cmd.sub)"),
    ("C4", "req", '"get" -> doGet(cmd.id, parsedArgs)'),
    ("C5", "req", '"delete" -> doDelete(cmd.id, parsedArgs)'),
    ("C6", "req", "sub = args.positional.getOrNull(0)"),
    ("C7", "req", "id = args.positional.getOrNull(1)?.toLongOrNull()"),
    ("C8", "req", 'booleanFlags = setOf("help", "h")'),
    ("C9", "req", "internal fun listMax(parsedArgs: OffloadArgs, default: Int = DEFAULT_LIST_MAX): Int"),
    ("C10", "forbid", "rawArgs[0]"),
    ("C11", "forbid", "rawArgs.drop(1)"),
    ("C12", "forbid", "args.firstOrNull()?.toLongOrNull()"),
    ("C13", "forbid", 'it == "--max" || it == "-n"'),
]

CONFIG_RULES = [
    ("K1", "req", "private fun readLinuxPath(linuxPath: String, sessionId: String?, cwd: String?): String?"),
    ("K2", "req", "internal fun resolveMaybeRelative(path: String, cwd: String?): String ="),
    ("K3", "req", 'if (!path.startsWith("/") && !cwd.isNullOrEmpty() && cwd.startsWith("/"))'),
    ("K4", "req", 'if (cwd.endsWith("/")) "$cwd$path" else "$cwd/$path"'),
    ("K5", "req", "PRootKernel.resolvePathForSession(sessionId, resolved, context)"),
    ("K6", "req", "val resolved = resolveMaybeRelative(linuxPath, cwd)"),
    ("K7", "count_re", (r"readLinuxPath\(fileArg, request\.sessionId, request\.cwd\)", 3)),
    ("K8", "forbid_re", r"readLinuxPath\(fileArg, request\.sessionId\)(?!,)"),
    # [dv15-A1 A-02] the short options this handler reads as values must be
    # DECLARED at its parser call, or `-f pinned` silently becomes flag + stray
    # positional (the filter is dropped and the unfiltered list is returned).
    ("K9", "req", 'valueShortOptions = setOf("f", "p", "s")'),
]

# [dv15-A1 A-01/A-02] the shared parser's two value-binding decisions.
#   A-01: a token starting with `-` was never a value, so `--lat -33.86` left
#         `lat` UNSET and the number became a stray flag — the query then ran
#         unconstrained (the caller's `getDouble("lat")` returned null).
#   A-02: a single-dash option was ALWAYS a flag, so `-f pinned` dropped
#         `pinned` into the positionals and the command behaved as if the
#         option had not been given.
# The needles pin the DECISION (the negative-number predicate, the declared
# branch that writes into `values`), not a spelling: a rewrite that keeps the
# behaviour passes, the old coarse branches fail closed.
ARGS_RULES = [
    ("A1", "req", "private fun valueTokenOrNull(token: String?): String? ="),
    ("A2", "req", 'token?.takeIf { !it.startsWith("-") || isNegativeNumberToken(it) }'),
    ("A3", "req", "private fun isNegativeNumberToken(token: String): Boolean ="),
    ("A4", "req", "token.length > 1 && token[0] == '-' && token[1].isDigit()"),
    ("A5", "req", "private val valueShortOptions: Set<String> = emptySet(),"),
    ("A6", "req", "if (name in valueShortOptions && name !in effectiveBooleanFlags && value != null) {"),
    ("A7", "req", "vs[name] = value"),
    ("A8", "forbid_re", r'a\.startsWith\("-"\) && a\.length > 1 -> fs\.add\('),
]

# [da1026-B] OffloadOutput.OUTPUT_FLAGS must cover the universal help flags;
# judged against code_only text, so a comment listing help/h cannot satisfy it.
# Fail-closed: the declaration itself must exist.
OUTPUT_FLAGS_RULES = [
    ("O1", "req", "val OUTPUT_FLAGS: Set<String> = setOf("),
    ("O2", "req_re", r'val OUTPUT_FLAGS: Set<String> = setOf\([^)]*"help"[^)]*\)'),
    ("O3", "req_re", r'val OUTPUT_FLAGS: Set<String> = setOf\([^)]*"h"[^)]*\)'),
]

# [da1026-B] SpeakOffloadHandler reads --stop/--status via hasFlag, so the
# OffloadArgs call must declare them boolean (the handler previously used the
# no-arg default `booleanFlags = emptySet()`).
SPEAK_RULES = [
    ("S1", "req_re", r'OffloadArgs\(request\.argv\.drop\(1\),\s*booleanFlags\s*=\s*setOf\([^)]*"stop"[^)]*\)'),
    ("S2", "req_re", r'booleanFlags\s*=\s*setOf\([^)]*"status"[^)]*\)'),
]

# [da1020-C] BrowserUse relative-path join: the guest cwd must reach
# buildInputJson, and BOTH path-carrying options must resolve through the
# pure join helpers BEFORE the session-aware resolver sees them.
BROWSERUSE_RULES = [
    ("B1", "req", "private fun buildInputJson(args: OffloadArgs, sessionId: String?, cwd: String?): JSONObject"),
    ("B2", "req", "internal fun resolveMaybeRelativeAll(paths: List<String>, cwd: String?): List<String>"),
    ("B3", "req", "val tokens = resolveMaybeRelativeAll("),
    ("B4", "req", 'obj.put("paths", JSONArray(tokens))'),
    ("B5", "req", "val joined = resolveMaybeRelative(cookiesFile, cwd)"),
    ("B6", "req", "PRootKernel.resolvePathForSession(sessionId, joined, app)"),
    ("B7", "forbid_re", r"resolvePathForSession\(sessionId, cookiesFile, app\)"),
    ("B8", "req", "buildInputJson(args, request.sessionId, request.cwd)"),
]

# [da1020-C] Fastio tar-prefix robustness: the tar verdict and the short-option
# expander must share ONE subcommand-token helper (never two copies of
# `firstOrNull() == "tar"`), and the flag set must stay scoped to `tar`.
FASTIO_RULES = [
    ("F1", "req", "internal fun firstSubcommandToken(rawArgv: List<String>): String?"),
    ("F2", "req", 'val isTarCommand = firstSubcommandToken(rawArgv) == "tar"'),
    ("F3", "req", 'if (firstSubcommandToken(argv) != "tar") return argv'),
    ("F4", "forbid_re", r'firstOrNull\(\) (!=|==) "tar"'),
    ("F5", "req", "internal fun fastioBooleanFlags(isTarCommand: Boolean): Set<String>"),
    ("F6", "req", "booleanFlags = fastioBooleanFlags(isTarCommand)"),
    ("F7", "req", "internal fun normalizeTarShorts(argv: List<String>): List<String>"),
    ("F8", "req", "internal fun normalizeLongOptionSpelling(argv: List<String>): List<String>"),
]


# [da1028-A] ISSUE-08 — DebugOffloadHandler's pass-through subcommands.
# The handler used to parse argv ONCE, greedily (`OffloadArgs` treats every
# `-`-prefixed token anywhere as one of ITS flags) and then hand the leftovers
# to `exec` / `shizuku` / `model-use`: `minis-debug exec ls -l` lost the `-l`
# because the top level had already eaten it. Contract: a second structural
# view (`splitArgv` → globals / subcommand / verbatim tail) feeds the
# pass-through branches, while output shaping for those branches reads the
# GLOBAL slice only, so a child's own `-q` cannot strip this command's
# envelope. The rules pin the wiring; the anchor-drift case (a missing needle)
# is itself a finding.
DEBUG_HANDLER = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/DebugOffloadHandler.kt"
DEBUG_RULES = [
    ("D1", "req", "internal fun splitArgv(tokens: List<String>): DebugArgvSplit {"),
    ("D2", "req", "internal fun forwardedArgv("),
    ("D3", "req", "): List<String> = if (sub in PASSTHROUGH_SUBCOMMANDS) rawRest else args.positional.drop(1)"),
    ("D4", "req", 'private val PASSTHROUGH_SUBCOMMANDS = setOf("exec", "shizuku", "model-use", "modelUse")'),
    ("D5", "req", "val argv = splitArgv(raw)"),
    ("D6", "req", "val sub = argv.subcommand"),
    ("D7", "req", "buildRpcCall(sub, args, argv.rawRest)"),
    ("D8", "req", "private fun buildRpcCall(sub: String, args: OffloadArgs, rawRest: List<String>): RpcCall {"),
    ("D9", "req", "val rest = forwardedArgv(sub, args, rawRest)"),
    ("D10", "req", 'RpcCall("debug.shizuku.exec", buildExecForwarder(rest, args))'),
    ("D11", "req", 'RpcCall("debug.modelUse.exec", buildExecForwarder(rest, args))'),
    ("D12", "req", "execCommandLine(rest)"),
    ("D13", "req", "if (sub in PASSTHROUGH_SUBCOMMANDS) OffloadArgs(argv.globals, BOOLEAN_FLAGS) else args"),
    ("D14", "forbid_re", r"buildExecForwarder\(args\.positional"),
    ("D15", "forbid", "buildRpcCall(sub, args)"),
    ("D16", "forbid", "forwardedArgv(sub, args)"),
    ("D17", "forbid_re", r"buildExecForwarder\(OffloadArgs\("),
]

# [da1032-C] ISSUE-08 — SessionsOffloadHandler's date bounds.
# The three date-carrying subcommands (list / search / messages) each parsed
# `parseDate(args.get("start"))` on their own and answered `null` on a value
# the caller DID spell but the parser could not read — and `null` means "no
# filter", so a typo returned the WHOLE library. The fix routes every bound
# through ONE funnel (resolveDateBounds → sessionsDateBound → parseSessionsDate)
# that throws [InvalidDateArg] for a supplied-but-unreadable value while an
# ABSENT flag still resolves to null (no filter — legal). The unknown-command
# message is generated from SUBCOMMANDS so it can no longer drift out of sync
# with the dispatch table (`send` was missing from the hand-written list).
SESSIONS = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/SessionsOffloadHandler.kt"
SESSIONS_RULES = [
    ("J1", "req", "private fun resolveDateBounds(args: OffloadArgs): Pair<Long?, Long?>"),
    ("J2", "req", 'val startMs = sessionsDateBound("--start", args.get("start"))'),
    ("J3", "req", 'val endMs = sessionsDateBound("--end", args.get("end"))'),
    ("J4", "count_re", (r"val \(startMs, endMs\) = resolveDateBounds\(args\)", 3)),
    ("J5", "req", "internal fun sessionsDateBound(flag: String, raw: String?, parse: (String) -> Long?): Long?"),
    ("J6", "req", "internal fun parseSessionsDate(raw: String?): Long?"),
    ("J7", "req", "internal class InvalidDateArg(flag: String, raw: String)"),
    ("J8", "req", "catch (e: InvalidDateArg)"),
    ("J9", "req", 'private val SUBCOMMANDS = listOf("list", "search", "messages", "send")'),
    ("J10", "req", 'Valid: ${SUBCOMMANDS.joinToString(", ")}'),
    ("J11", "forbid", "Valid: list, search, messages."),
    ("J12", "forbid_re", r"parseDate\(args\.get\(\"start\"\)\)"),
    ("J13", "forbid_re", r"parseEndDate\(args\.get\(\"end\"\)\)"),
    ("J14", "req", '"send" -> cmdSend(args)'),
    ("J15", "count_re", (r"throw InvalidDateArg\(flag, raw\)", 1)),
    # [dv15-A1 A-08] both bounds readable but inside out: the query
    # (`lastActive >= start AND lastActive <= end`) can match nothing, and the
    # command used to answer exit 0 with {"count":0,"sessions":[]} — read by the
    # caller as "no history in that period". J17 pins the STRICT comparison
    # (`--start X --end X` stays legal: parseEndDate aligns the end to that
    # day's 23:59:59.999), J18 fails closed on the non-strict variant, J19 pins
    # the delegation (both raws travel with the bounds for the message) and J20
    # the refusal itself.
    ("J16", "req", "internal fun sessionsDateWindow("),
    ("J17", "count_re", (r"endMs < startMs", 1)),
    ("J18", "forbid_re", r"endMs <="),
    ("J19", "count_re", (r'return sessionsDateWindow\(startMs, endMs, args\.get\("start"\), args\.get\("end"\)\)', 1)),
    ("J20", "req", "throw InvertedDateWindow("),
]


def code_only(src: str) -> str:
    """Strip block + line comments so prose quoting a pattern cannot trip a rule."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


def check_text(contacts_src: str, config_src: str, browseruse_src: str = "", fastio_src: str = "") -> list:
    """Return the list of violated rule ids (empty = compliant)."""
    findings = []
    contacts = code_only(contacts_src)
    config = code_only(config_src)
    browseruse = code_only(browseruse_src)
    fastio = code_only(fastio_src)
    for rid, kind, needle in CONTACTS_RULES:
        if kind in ("req", "forbid"):
            hit = needle in contacts
        if kind == "req" and not hit:
            findings.append((rid, "missing", needle))
        if kind == "forbid" and hit:
            findings.append((rid, "present", needle))
    for rid, kind, needle in CONFIG_RULES:
        if kind in ("req", "forbid"):
            hit = needle in config
            if kind == "req" and not hit:
                findings.append((rid, "missing", needle))
            if kind == "forbid" and hit:
                findings.append((rid, "present", needle))
        elif kind == "count_re":
            pattern, want = needle
            got = len(re.findall(pattern, config))
            if got != want:
                findings.append((rid, f"count {got} != {want}", pattern))
        elif kind == "forbid_re":
            if re.search(needle, config):
                findings.append((rid, "present", needle))
    for rid, kind, needle in BROWSERUSE_RULES:
        if kind in ("req", "forbid"):
            hit = needle in browseruse
            if kind == "req" and not hit:
                findings.append((rid, "missing", needle))
            if kind == "forbid" and hit:
                findings.append((rid, "present", needle))
        elif kind == "forbid_re":
            if re.search(needle, browseruse):
                findings.append((rid, "present", needle))
    for rid, kind, needle in FASTIO_RULES:
        if kind in ("req", "forbid"):
            hit = needle in fastio
            if kind == "req" and not hit:
                findings.append((rid, "missing", needle))
            if kind == "forbid" and hit:
                findings.append((rid, "present", needle))
        elif kind == "forbid_re":
            if re.search(needle, fastio):
                findings.append((rid, "present", needle))
    return findings


def check_flag_set(output_src: str, speak_src: str) -> list:
    """[da1026-B] Global boolean-flag set + the handler that forgot to declare.

    Judged on code-only text (a comment quoting the wanted set cannot pass).
    Fail-closed: a missing `val OUTPUT_FLAGS ... = setOf(...)` declaration is a
    finding, never a silent skip.
    """
    findings = []
    output = code_only(output_src)
    speak = code_only(speak_src)
    declared_literal = re.search(r"val OUTPUT_FLAGS: Set<String> = setOf\(([^)]*)\)", output)
    declared = set(re.findall(r'"([^"]*)"', declared_literal.group(1))) if declared_literal else None
    for rid, kind, needle in OUTPUT_FLAGS_RULES:
        if kind == "req":
            if needle not in output:
                findings.append((rid, "missing", needle))
        elif kind == "req_re":
            if declared is None:
                # O1 already reports the missing declaration; do not add noise.
                continue
            want = "help" if rid == "O2" else "h"
            if want not in declared:
                findings.append((rid, "missing", f'"{want}" in OUTPUT_FLAGS'))
    for rid, kind, needle in SPEAK_RULES:
        if not re.search(needle, speak):
            findings.append((rid, "missing", needle))
    return findings


def check_debug(debug_src: str) -> list:
    """[da1028-A] ISSUE-08 rules, judged on comment-stripped text.

    Fail-closed by construction: every needle is a `req` (missing → finding) or
    a `forbid` (present → finding), so a renamed helper, a rebuilt greedy parse
    at a pass-through branch, or a whole-file rewrite all land as findings
    rather than as a silently empty rule set.
    """
    findings = []
    debug = code_only(debug_src)
    for rid, kind, needle in DEBUG_RULES:
        if kind == "req":
            if needle not in debug:
                findings.append((rid, "missing", needle))
        elif kind == "forbid":
            if needle in debug:
                findings.append((rid, "present", needle))
        elif kind == "forbid_re":
            if re.search(needle, debug):
                findings.append((rid, "present", needle))
    return findings


def check_args(args_src: str) -> list:
    """[dv15-A1 A-01/A-02] rules for the shared OffloadArgs parser, on
    comment-stripped text.

    Fail-closed by construction: every needle is a `req` (missing → finding), a
    `forbid` (present → finding), a `count_re`, or a `forbid_re`. A parser
    rewrite that drops the value-binding decisions lands as findings rather
    than as a silently empty rule set.
    """
    findings = []
    args = code_only(args_src)
    for rid, kind, needle in ARGS_RULES:
        if kind in ("req", "forbid"):
            hit = needle in args
            if kind == "req" and not hit:
                findings.append((rid, "missing", needle))
            if kind == "forbid" and hit:
                findings.append((rid, "present", needle))
        elif kind == "count_re":
            pattern, want = needle
            got = len(re.findall(pattern, args))
            if got != want:
                findings.append((rid, f"count {got} != {want}", pattern))
        elif kind == "forbid_re":
            if re.search(needle, args):
                findings.append((rid, "present", needle))
    findings += violations_dash_branch(args)
    return findings


# ── [dv16-A A-02] the top-level single-dash branch ──────────────────────────
# A8 above forbids ONE spelling of the unguarded branch (`… > 1 -> fs.add(`).
# It cannot see the guard being *removed* from the block-bodied branch this
# parser actually uses: a negative number is then read as a short option named
# after its digits (`-33.86` → flag `33.86`), the positionals shift by one, and
# `android-photos near -33.86 151.21` answers about another place (or refuses
# with "missing argument"). So the decision point itself is pinned: the branch
# condition must carry a negative-number exception AND the declared-flag escape
# that keeps a digit-spelled switch legal where a handler declares it
# (`android-shizuku-cli package list -3`, declared in NO_VALUE_FLAGS).
DASH_BRANCH_MARKER = 'a.startsWith("-") && a.length > 1'
DASH_BRANCH_NEGATIVE = "isNegativeNumberToken"
DASH_BRANCH_DECLARED = "effectiveBooleanFlags"


def dash_branch_condition(args_src):
    """Condition text of the top-level single-dash branch: from the marker up to
    the `->` that opens the branch body. Depth-aware, so a `->` inside a nested
    lambda cannot end it early, and blind to string literals, so the `"-"`
    argument cannot either. None when the branch is not there at all
    (fail-closed — a rename must fail this gate, not skip it)."""
    i = args_src.find(DASH_BRANCH_MARKER)
    if i < 0:
        return None
    depth = 0
    j = i
    while j < len(args_src):
        c = args_src[j]
        if c == '"':
            k = args_src.find('"', j + 1)
            j = len(args_src) if k < 0 else k + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "-" and args_src.startswith("->", j) and depth == 0:
            return args_src[i:j]
        j += 1
    return None


def violations_dash_branch(args_src):
    """The branch must decide by the DECLARED vocabulary, not by the token's
    shape — that is what keeps `-33.86` a value everywhere while `-3` stays a
    switch where a handler declared it."""
    findings = []
    cond = dash_branch_condition(args_src)
    if cond is None:
        findings.append(("A9", "single-dash branch missing", DASH_BRANCH_MARKER))
        return findings
    if DASH_BRANCH_NEGATIVE not in cond:
        findings.append(("A9", "no negative-number exception in the branch condition",
                         DASH_BRANCH_MARKER))
    if DASH_BRANCH_DECLARED not in cond:
        findings.append(("A9", "no declared-flag escape in the branch condition",
                         DASH_BRANCH_DECLARED))
    return findings


def check_sessions(sessions_src: str) -> list:
    """[da1032-C] ISSUE-08 rules for SessionsOffloadHandler, on comment-stripped text.

    Fail-closed by construction: every needle is a `req` (missing → finding), a
    `forbid` (present → finding), a `count_re`, or a `forbid_re`. A renamed
    funnel, a rebuilt per-command parse, or a whole-file rewrite all land as
    findings rather than as a silently empty rule set.
    """
    findings = []
    sessions = code_only(sessions_src)
    for rid, kind, needle in SESSIONS_RULES:
        if kind in ("req", "forbid"):
            hit = needle in sessions
            if kind == "req" and not hit:
                findings.append((rid, "missing", needle))
            if kind == "forbid" and hit:
                findings.append((rid, "present", needle))
        elif kind == "count_re":
            pattern, want = needle
            got = len(re.findall(pattern, sessions))
            if got != want:
                findings.append((rid, f"count {got} != {want}", pattern))
        elif kind == "forbid_re":
            if re.search(needle, sessions):
                findings.append((rid, "present", needle))
    return findings


def describe(findings) -> str:
    return "\n".join(f"  ✗ {rid}: {kind} → {needle}" for rid, kind, needle in findings)


# ── [da1033-D] ISSUE-04: the payload handed to OffloadOutput.formatBody is JSON ──
#
# `OffloadOutput.formatBody` only *re-shapes* JSON — a plain sentence passes
# through untouched (OffloadOutput.kt:61-80, both the no-flag fast path and the
# parse-failure fallback). So `android-photos near` and `android-contacts
# search|get|list` used to exit 0 with an English sentence whenever the result
# was empty (the most common outcome), breaking every `jq` / `json.loads`
# downstream — and `near` returned a bare JSONArray when it was NOT empty, so
# even the top-level type flipped with the result.
#
# Decision point (deliberately NOT a call form): a *bare string literal* must
# never be the payload. Accepted, in any spelling: a variable, any expression
# (`JSONObject()…toString()`, `arr.toString(2)`, `photos.toString(2)`, a helper
# call), or a literal that already carries JSON delimiters (`{` / `[`, e.g. the
# `"""{"error":…}"""` form, or a template with an interpolated object).
#
# Two sites, because the defect has two spellings:
#   N1 — the first argument of every `formatBody(` call in the payload files.
#   N2 — every `return <literal>` inside a *payload helper*: a function whose
#        result is the first argument of a `formatBody(` call (the Photos
#        payload is produced by the String-returning `nearPhotos`, so at the
#        call site it is just a variable — N1 alone would be blind to it; this
#        gate exists because the *review* proposed N1 only).
#
# [da1034-A4] ISSUE-08 widened the file list to every handler that ships a
# formatBody payload, and that is what forced N2's scope to be the *helper*
# rather than the whole file: `FastioOffloadHandler` returns bare literals from
# ten non-payload helpers (`return "not_found" to "…"` — a reason-code Pair that
# later becomes the JSON envelope, not a payload), so the whole-file reading
# demanded ten rewrites of working code. N2 now judges the defect (a literal
# that reaches formatBody) instead of the spelling (any literal return).
_STR_LIT_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_RAW_LIT_RE = re.compile(r'"""(?:[^"]|"(?!""))*"""')


def _literal_at(src: str, i: int):
    """Source text of the Kotlin string literal starting at `i`, else None."""
    if src.startswith('"""', i):
        m = _RAW_LIT_RE.match(src, i)
        return m.group(0) if m else None
    if src[i] == '"':
        m = _STR_LIT_RE.match(src, i)
        return m.group(0) if m else None
    return None


def _mask_templates(lit: str) -> str:
    """Drop Kotlin `${…}` interpolations, leaving the literal's own text.

    Load-bearing: `${radiusKm}` contains a `{`, so a naive delimiter test would
    call `"No photos found within ${radiusKm}km."` JSON — the *Before* shape of
    this very defect. The delimiter has to come from the literal text.
    """
    out = []
    i = 0
    while i < len(lit):
        if lit[i] == "$" and i + 1 < len(lit) and lit[i + 1] == "{":
            depth = 0
            j = i + 1
            while j < len(lit):
                if lit[j] == "{":
                    depth += 1
                elif lit[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            i = j + 1  # skip the whole interpolation
            continue
        out.append(lit[i])
        i += 1
    return "".join(out)


def _is_json_literal(lit: str) -> bool:
    """A literal is JSON-shaped when its OWN text carries `{` / `[`.

    `"{\\"error\\":\\"$code\\"}"` → yes. `"No photos found within ${km}km."` →
    no (the brace is interpolation, not structure). A literal that is nothing
    but interpolation (`"${obj}"`) counts as a variable in a template coat and
    is accepted, same as passing `obj` directly.
    """
    core = _mask_templates(lit).strip().strip('"').strip()
    if core == "":
        return True  # pure interpolation — equivalent to passing the variable
    return "{" in core or "[" in core


def _payload_helper_bodies(code: str, name: str):
    """Body text of `fun <name>(…)`, or None (declared elsewhere / expression
    body — an expression body has no `return` statement to judge)."""
    m = re.search(r"\bfun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?" + re.escape(name) + r"\s*\(", code)
    if not m:
        return None
    i, depth = m.end() - 1, 0
    while i < len(code):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    j = code.find("{", i)
    if j < 0:
        return None
    # An expression body (`fun f(...): T = expr`) has no `return` statement to
    # judge — and the next `{` after its parameter list belongs to a LATER
    # function, so without this the rule would read the neighbour's body and
    # mislabel its findings as this function's.
    eq = code.find("=", i)
    if 0 <= eq < j:
        return None
    depth, k = 0, j
    while k < len(code):
        if code[k] == "{":
            depth += 1
        elif code[k] == "}":
            depth -= 1
            if depth == 0:
                return code[j:k + 1]
        k += 1
    return None


def _payload_helper_names(code: str) -> list:
    """Names of the functions whose result is fed to `formatBody(` — the N2
    scope. Sorted so fixture output is stable."""
    names = {m.group(1) for m in re.finditer(r"formatBody\(\s*([A-Za-z_]\w*)\s*\(", code)}
    return sorted(names)


def payload_findings(label: str, src: str) -> list:
    """N1 / N2 for one payload file (code-only text, comments stripped)."""
    findings = []
    code = code_only(src)
    for m in re.finditer(r"formatBody\(", code):
        j = m.end()
        while j < len(code) and code[j] in " \t\r\n":
            j += 1
        if j >= len(code) or code[j] != '"':
            continue  # variable / expression / helper call → accepted
        lit = _literal_at(code, j)
        if lit is None:
            findings.append(("N1", "unreadable literal", label))
        elif not _is_json_literal(lit):
            findings.append(("N1", "bare non-JSON payload literal", label + ": " + lit[:70]))
    for name in _payload_helper_names(code):
        body = _payload_helper_bodies(code, name)
        if body is None:
            continue
        for m in re.finditer(r"\breturn\s+(?=\")", body):
            lit = _literal_at(body, m.end())
            if lit is None:
                findings.append(("N2", "unreadable literal", f"{label}: {name}()"))
            elif not _is_json_literal(lit):
                findings.append(
                    ("N2", "bare non-JSON payload return", f"{label}: {name}() " + lit[:70]))
    return findings


def check_offload_json(*files) -> list:
    """[da1033-D] ISSUE-04 + [da1034-A4] ISSUE-08 rules for the payload files.

    [files] is (label, source) pairs; every one of them gets N1, and N2 where a
    payload helper exists.
    """
    out = []
    for label, src in files:
        out += payload_findings(label, src)
    return out

# ── [dv14-A1] deep-audit-v14 ISSUE-01 / ISSUE-02 / ISSUE-04: alias bindings ──
#
# One process-wide fact ties the three together: OffloadArgs binds NO value to a
# single-dash token (`-n` becomes a bare flag and its value lands in
# `positional`), and a `--key` is exempt from the greedy `--key value` branch
# ONLY when its name is in the declared boolean set. Every defect below is a
# name the code accepts with none of the value machinery behind it — so the
# call succeeds with wrong output (exit 0, `jq`-visible) instead of being
# refused. Each rule pins the DEFINITION (the function that owns the decision),
# never a call spelling:
#
#  V1 ContactsOffloadHandler — `doSearch` must resolve its page size through
#     `listMax` (the same resolver `doList` uses; `getInt("max", "n")` is the
#     dead alias spelling and must not come back), and the legacy `-n <N>` form
#     must be bound by `bindLegacyShortMax` BEFORE the parse, or its value takes
#     the query slot (`search -n 10 Alice` searched for the name "10").
#  V2 NotificationOffloadHandler — `handleSend` must decide
#     deferred-vs-immediate from the RAW `--after` string via `sendDisposition`:
#     `getInt("after")` collapses "spelled but unreadable" (`--after 5m`) into
#     "not spelled" and fires the notification immediately with exit 0, and
#     `scheduleDeferred`'s "--after or --at is required" refusal (unreachable
#     while the branch was gated on the parsed value) must stay live.
#  V3 FastioOffloadHandler — `firstSubcommandToken` must consult
#     `fastioBooleanFlags` (the same set the shared parse uses) while keeping
#     OUTPUT_FLAGS: knowing only OUTPUT_FLAGS, a leading `--force` / `--verbose`
#     / `--create` swallowed the `tar` subcommand as its value, so
#     `isTarCommand` came out false and the `-cf`/`-xf` expansion never ran.
#
# Fail-closed by construction: every arm reports its own id when its anchor is
# absent, so a renamed / restructured / deleted owner function lands as a
# finding instead of shrinking the rule set silently. Every needle is judged on
# comment-stripped text (a comment quoting the wanted shape cannot pass) and the
# bodies come from the same brace matcher the N2 arm uses — an expression body
# has no owner body to judge and is itself a finding.
def check_offload_cli_bindings(contacts_src: str, notification_src: str,
                               fastio_src: str) -> list:
    """[dv14-A1] the alias-binding rules (V1 contacts, V2 notification, V3 fastio)."""
    findings = []
    contacts = code_only(contacts_src)
    notif = code_only(notification_src)
    fastio = code_only(fastio_src)

    # V1 — the dead alias read + the resolver wiring.
    if 'getInt("max", "n")' in contacts:
        findings.append(
            ("V1a", "present", 'getInt("max", "n") — OffloadArgs never fills values["n"]'))
    do_search = _payload_helper_bodies(contacts, "doSearch")
    if do_search is None:
        findings.append(("V1b", "missing", "private fun doSearch(parsedArgs: OffloadArgs)"))
    elif "listMax(" not in do_search:
        findings.append(("V1b", "missing", "doSearch must resolve its page size through listMax("))
    parse_contacts = _payload_helper_bodies(contacts, "parseContactsCommand")
    if parse_contacts is None:
        findings.append(("V1c", "missing", "internal fun parseContactsCommand(argv: List<String>)"))
    elif "bindLegacyShortMax(argv)" not in parse_contacts:
        findings.append(
            ("V1c", "missing",
             "parseContactsCommand must bind the legacy -n alias (bindLegacyShortMax(argv))"))
    if _payload_helper_bodies(contacts, "bindLegacyShortMax") is None:
        findings.append(
            ("V1d", "missing", "internal fun bindLegacyShortMax(argv: List<String>): List<String>"))

    # V2 — the deferred decision, its owner function, and its refusal guard.
    if 'getInt("after")' in notif:
        findings.append(
            ("V2a", "present",
             'getInt("after") erases "spelled but unreadable" into "not spelled"'))
    handle_send = _payload_helper_bodies(notif, "handleSend")
    if handle_send is None:
        findings.append(("V2b", "missing", "private fun handleSend(sub: String, args: OffloadArgs)"))
    elif "sendDisposition(" not in handle_send:
        findings.append(("V2b", "missing", "handleSend must decide through sendDisposition("))
    if _payload_helper_bodies(notif, "sendDisposition") is None:
        findings.append(
            ("V2c", "missing",
             "internal fun sendDisposition(sub: String, rawAfter: String?, rawAt: String?): SendDisposition"))
    if "android-notification: --after or --at is required\\n" not in notif:
        findings.append(
            ("V2d", "missing", "scheduleDeferred's '--after or --at is required' refusal"))

    # V3 — the raw-argv scan must know the same boolean set the parse uses.
    scan = _payload_helper_bodies(fastio, "firstSubcommandToken")
    if scan is None:
        findings.append(("V3c", "missing", "internal fun firstSubcommandToken(rawArgv: List<String>)"))
    else:
        if "fastioBooleanFlags(" not in scan:
            findings.append(
                ("V3a", "missing", "firstSubcommandToken must consult fastioBooleanFlags("))
        if "OUTPUT_FLAGS" not in scan:
            findings.append(
                ("V3b", "missing",
                 "firstSubcommandToken must keep excluding OffloadOutput.OUTPUT_FLAGS"))
        # [dv18-A A-01b] the pre-scan must consume value-taking short options
        # from a declared set that includes `-C` (tar's --directory): a scan
        # that skips `-C /tmp` mis-reads `/tmp` as the subcommand, so the tar
        # verbs never get declared and `--directory` never expands.
        shorts_decl = re.search(r"SHORT_VALUE_OPTIONS\s*=\s*setOf\(([^)]*)\)", fastio)
        if shorts_decl is None or "-C" not in shorts_decl.group(1):
            findings.append(
                ("V3d", "missing",
                 "SHORT_VALUE_OPTIONS must be declared and include -C (tar --directory)"))
        if "SHORT_VALUE_OPTIONS" not in scan:
            findings.append(
                ("V3e", "missing", "firstSubcommandToken must consult SHORT_VALUE_OPTIONS"))
        if not re.search(r"i \+= if \(consumesValue\) 2 else 1", scan):
            findings.append(
                ("V3f", "missing",
                 "firstSubcommandToken must consume the value token (i += 2)"))
    return findings


def check_photos_album(photos_src: str) -> list:
    """[dv18-A A-03] the --album decision is ONE classification (Absent / ById /
    ByName / Unreadable): a non-numeric --id refuses (exit 2) instead of
    silently downgrading to the name branch, and a blank --name refuses
    instead of running a `LIKE '%%'` full-library scan. handleAlbums (the
    no-arg listing) stays untouched."""
    photos = code_only(photos_src)
    findings = []
    if "internal fun resolveAlbumSelector(" not in photos:
        findings.append(("P1", "missing", "the --album decision lost its single classifier"))
    if not re.search(r"AlbumSelector\.Unreadable\s*->\s*return NativeOffloadResult\(2,\s*albumSelectorRefusal\(",
                     photos):
        findings.append(("P2", "missing",
                         "a non-numeric --id must reach a refusal (albumSelectorRefusal), not the name branch"))
    resolver = _payload_helper_bodies(photos, "resolveAlbumSelector")
    if resolver is not None and ("toLongOrNull()" not in resolver or "isNotBlank()" not in resolver):
        findings.append(("P3", "missing",
                         "the classifier must nullable-parse --id and blank-check --name"))
    if "fun handleAlbums(" not in photos:
        findings.append(("P4", "missing", "handleAlbums (the no-arg listing) is gone"))
    return findings


# ── selftest: every rule must have a tooth, and the green arm must pass ──────

GREEN_CONTACTS = """
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val rawArgs = request.argv.drop(1)
        val cmd = parseContactsCommand(rawArgs)
        val parsedArgs = cmd.args
        val needsWrite = cmd.sub == "delete"
        when (val sub = cmd.sub) {
            "list" -> doList(parsedArgs)
            "get" -> doGet(cmd.id, parsedArgs)
            "delete" -> doDelete(cmd.id, parsedArgs)
        }
    }
    companion object {
        internal fun parseContactsCommand(argv: List<String>): ContactsCommand {
            val args = OffloadArgs(bindLegacyShortMax(argv), booleanFlags = setOf("help", "h"))
            return ContactsCommand(
                sub = args.positional.getOrNull(0),
                id = args.positional.getOrNull(1)?.toLongOrNull(),
                args = args,
            )
        }
        internal fun bindLegacyShortMax(argv: List<String>): List<String> {
            if ("-n" !in argv) return argv
            val sub = OffloadArgs(argv, booleanFlags = setOf("help", "h")).positional.getOrNull(0)
            if (sub != "list" && sub != "search") return argv
            val out = ArrayList<String>(argv.size)
            out.add("--max")
            return out
        }
        internal fun doSearch(parsedArgs: OffloadArgs): NativeOffloadResult {
            val query = searchQueryOf(parsedArgs.positional)
                ?: return NativeOffloadResult(2, "android-contacts search: missing <query>\n")
            val max = listMax(parsedArgs, 20)
            return NativeOffloadResult(0, OffloadOutput.formatBody(arr.toString(2), parsedArgs) + "\n")
        }
        internal fun listMax(parsedArgs: OffloadArgs, default: Int = DEFAULT_LIST_MAX): Int {
            parsedArgs.getInt("max")?.let { return it }
        }
    }
"""

GREEN_CONFIG = """
    private fun cmdSet(args: OffloadArgs, request: NativeOffloadRequest): NativeOffloadResult {
        val fileArg = args.get("file")
        readLinuxPath(fileArg, request.sessionId, request.cwd) ?: return errorResult(
            args, EXIT_INVALID_ARGS, "INVALID_ARGS", "--file: could not read value")
        readLinuxPath(fileArg, request.sessionId, request.cwd) ?: return errorResult(
            args, EXIT_INVALID_ARGS, "INVALID_ARGS", "--file: could not read value")
        readLinuxPath(fileArg, request.sessionId, request.cwd) ?: return errorResult(
            args, EXIT_INVALID_ARGS, "INVALID_ARGS", "--file: could not read value")
    }
    private fun parseConfigArgs(argv: List<String>): OffloadArgs =
        OffloadArgs(argv, valueShortOptions = setOf("f", "p", "s"))
    private fun readLinuxPath(linuxPath: String, sessionId: String?, cwd: String?): String? {
        val resolved = resolveMaybeRelative(linuxPath, cwd)
        val hostFile: File = PRootKernel.resolvePathForSession(sessionId, resolved, context) ?: return null
        return try { hostFile.readText() } catch (_: Throwable) { null }
    }
    companion object {
        internal fun resolveMaybeRelative(path: String, cwd: String?): String =
            if (!path.startsWith("/") && !cwd.isNullOrEmpty() && cwd.startsWith("/")) {
                if (cwd.endsWith("/")) "$cwd$path" else "$cwd/$path"
            } else path
    }
"""

# [da1020-C] green fixtures for the two new arms — minimal but every pinned
# rule must be satisfiable from code (never from prose).
GREEN_BROWSERUSE = """
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val inputJson = buildInputJson(args, request.sessionId, request.cwd)
    }
    private fun buildInputJson(args: OffloadArgs, sessionId: String?, cwd: String?): JSONObject {
        args.get("paths")?.let { raw ->
            val tokens = resolveMaybeRelativeAll(rawTokens, cwd)
            if (tokens.isNotEmpty()) obj.put("paths", JSONArray(tokens))
        }
        val cookiesFile = args.get("cookies-file", "cookies_file")
        val cookiesRaw: String? = if (cookiesFile != null) {
            val joined = resolveMaybeRelative(cookiesFile, cwd)
            val host = PRootKernel.resolvePathForSession(sessionId, joined, app)
                ?: throw IllegalArgumentException("--cookies-file: cannot resolve path '$cookiesFile'")
            runCatching { host.readText() }.getOrNull()
                ?: throw IllegalArgumentException("--cookies-file: could not read '$cookiesFile'")
        } else {
            args.get("cookies")
        }
    }
    internal fun resolveMaybeRelativeAll(paths: List<String>, cwd: String?): List<String> =
        paths.map { resolveMaybeRelative(it, cwd) }
"""

GREEN_FASTIO = """
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val rawArgv = request.argv.drop(1)
        val isTarCommand = firstSubcommandToken(rawArgv) == "tar"
        val args = OffloadArgs(
            normalizeLongOptionSpelling(normalizeTarShorts(rawArgv)),
            booleanFlags = fastioBooleanFlags(isTarCommand),
        )
    }
    companion object {
        internal fun firstSubcommandToken(rawArgv: List<String>): String? {
            val noValue = OffloadOutput.OUTPUT_FLAGS + fastioBooleanFlags(isTarCommand = true)
            var i = 0
            while (i < rawArgv.size) {
                val token = rawArgv[i]
                if (!token.startsWith("-")) return token
                val consumesValue = when {
                    token in SHORT_VALUE_OPTIONS -> true
                    token.substring(2) !in noValue -> true
                    else -> false
                }
                i += if (consumesValue) 2 else 1
            }
            return null
        }
        private val SHORT_VALUE_OPTIONS = setOf("-C")
        internal fun fastioBooleanFlags(isTarCommand: Boolean): Set<String> =
            if (isTarCommand) {
                setOf("recursive", "force", "ignore-case", "create", "extract", "gzip", "verbose")
            } else {
                setOf("recursive", "force", "ignore-case")
            }
        internal fun normalizeTarShorts(argv: List<String>): List<String> {
            if (firstSubcommandToken(argv) != "tar") return argv
        }
        internal fun normalizeLongOptionSpelling(argv: List<String>): List<String> =
            argv.map { if (it in SHORT_LONG_OPTIONS) "-$it" else it }
    }
"""


# [da1026-B] green fixtures for the OUTPUT_FLAGS / speak arms. The declared set
# is written out literally (never `OffloadOutput.OUTPUT_FLAGS + ...`), so the
# fixture states the wanted contract instead of restating the code.
GREEN_ARGS = """
internal class OffloadArgs(
    argv: List<String>,
    booleanFlags: Set<String> = emptySet(),
    private val valueShortOptions: Set<String> = emptySet(),
) {
    init {
        val effectiveBooleanFlags = booleanFlags + OffloadOutput.OUTPUT_FLAGS
        val vs = mutableMapOf<String, String>()
        val value = valueTokenOrNull(argv.getOrNull(i + 1))
        if (name in valueShortOptions && name !in effectiveBooleanFlags && value != null) {
            vs[name] = value
        }
        when {
            a.startsWith("-") && a.length > 1 &&
                (!isNegativeNumberToken(a) || a.substring(1) in effectiveBooleanFlags) -> {
                fs.add(a.substring(1))
            }
            else -> pos.add(a)
        }
    }

    private fun valueTokenOrNull(token: String?): String? =
        token?.takeIf { !it.startsWith("-") || isNegativeNumberToken(it) }

    private fun isNegativeNumberToken(token: String): Boolean =
        token.length > 1 && token[0] == '-' && token[1].isDigit()
}
"""

GREEN_OUTPUT = """
internal object OffloadOutput {
    val OUTPUT_FLAGS: Set<String> = setOf("compact", "quiet", "q", "help", "h")
}
"""

GREEN_SPEAK = """
class SpeakOffloadHandler {
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(request.argv.drop(1), booleanFlags = setOf("stop", "status"))
        if (args.hasFlag("h", "help")) return NativeOffloadResult(0, HELP)
        if (args.hasFlag("stop")) return cmdStop(args)
        if (args.hasFlag("status")) return buildStatus(args)
    }
}
"""


GREEN_DEBUG = """
class DebugOffloadHandler {
    private val PASSTHROUGH_SUBCOMMANDS = setOf("exec", "shizuku", "model-use", "modelUse")

    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(raw)
        val argv = splitArgv(raw)
        val sub = argv.subcommand
            ?: return NativeOffloadResult(2, HELP)
        val rpc: RpcCall = try {
            buildRpcCall(sub, args, argv.rawRest)
        } catch (e: BadArgsException) {
            return NativeOffloadResult(2, "minis-debug ${'$'}sub: ${'$'}{e.message}")
        }
        val outArgs =
            if (sub in PASSTHROUGH_SUBCOMMANDS) OffloadArgs(argv.globals, BOOLEAN_FLAGS) else args
        return NativeOffloadResult(0, OffloadOutput.formatBody("{}", outArgs))
    }

    private fun buildRpcCall(sub: String, args: OffloadArgs, rawRest: List<String>): RpcCall {
        val rest = forwardedArgv(sub, args, rawRest)
        return when (sub) {
            "exec" -> {
                if (rest.isEmpty()) throw BadArgsException("missing <command...>")
                RpcCall("debug.shellExecute", JSONObject().put("command", execCommandLine(rest)))
            }
            "shizuku" -> RpcCall("debug.shizuku.exec", buildExecForwarder(rest, args))
            "model-use", "modelUse" -> RpcCall("debug.modelUse.exec", buildExecForwarder(rest, args))
            else -> throw BadArgsException("unknown subcommand '${'$'}sub'")
        }
    }

    private fun buildExecForwarder(rest: List<String>, args: OffloadArgs): JSONObject = JSONObject()

    internal fun splitArgv(tokens: List<String>): DebugArgvSplit {
        val globals = mutableListOf<String>()
        var i = 0
        while (i < tokens.size) {
            val t = tokens[i]
            if (!t.startsWith("-")) break
            globals.add(t)
            if (t == "--args") {
                i += 1
                if (i < tokens.size) globals.add(tokens[i])
            }
            i += 1
        }
        if (i >= tokens.size) return DebugArgvSplit(globals, null, emptyList())
        val tail = tokens.subList(i + 1, tokens.size).toList()
        val rest = if (tail.firstOrNull() == "--") tail.drop(1) else tail
        return DebugArgvSplit(globals, tokens[i], rest)
    }

    internal fun forwardedArgv(
        sub: String,
        args: OffloadArgs,
        rawRest: List<String>,
    ): List<String> = if (sub in PASSTHROUGH_SUBCOMMANDS) rawRest else args.positional.drop(1)

    internal fun execCommandLine(argv: List<String>): String = argv.joinToString(" ")
}
"""


# [da1032-C] green fixture for the Sessions date-bound arms: ONE funnel
# (resolveDateBounds → sessionsDateBound), three call sites, a throwing guard
# for a supplied-but-unreadable bound, and an unknown-command message generated
# from the SUBCOMMANDS list (so it cannot drift from the dispatch table).
GREEN_SESSIONS = """
class SessionsOffloadHandler {
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(request.argv.drop(1))
        return try {
            when (val sub = args.positional.getOrNull(0)) {
                "list" -> cmdList(args)
                "search" -> cmdSearch(args)
                "messages" -> cmdMessages(args)
                "send" -> cmdSend(args)
                else -> NativeOffloadResult(EXIT_INVALID_ARGS, "Unknown command '$sub'. Valid: ${SUBCOMMANDS.joinToString(", ")}. ")
            }
        } catch (e: InvalidDateArg) {
            NativeOffloadResult(EXIT_INVALID_ARGS, e.message)
        }
    }

    private fun cmdList(args: OffloadArgs): NativeOffloadResult {
        val (startMs, endMs) = resolveDateBounds(args)
        return NativeOffloadResult(0, "${startMs}..${endMs}")
    }

    private fun cmdSearch(args: OffloadArgs): NativeOffloadResult {
        val (startMs, endMs) = resolveDateBounds(args)
        return NativeOffloadResult(0, "${startMs}..${endMs}")
    }

    private fun cmdMessages(args: OffloadArgs): NativeOffloadResult {
        val (startMs, endMs) = resolveDateBounds(args)
        return NativeOffloadResult(0, "${startMs}..${endMs}")
    }

    private fun cmdSend(args: OffloadArgs): NativeOffloadResult = NativeOffloadResult(0, "")

    private fun resolveDateBounds(args: OffloadArgs): Pair<Long?, Long?> {
        val startMs = sessionsDateBound("--start", args.get("start")) { parseSessionsDate(it) }
        val endMs = sessionsDateBound("--end", args.get("end")) { parseEndDate(it) }
        return sessionsDateWindow(startMs, endMs, args.get("start"), args.get("end"))
    }

    private fun parseEndDate(raw: String?): Long? {
        val ms = parseSessionsDate(raw) ?: return null
        return ms
    }

    companion object {
        private val SUBCOMMANDS = listOf("list", "search", "messages", "send")
        private const val EXIT_INVALID_ARGS = 2
    }
}

internal class InvalidDateArg(flag: String, raw: String) :
    IllegalArgumentException("sessions: invalid $flag '$raw'")

internal fun parseSessionsDate(raw: String?): Long? {
    if (raw.isNullOrBlank()) return null
    return null
}

internal class InvertedDateWindow(startRaw: String, endRaw: String) :
    IllegalArgumentException("--end '$endRaw' is before --start '$startRaw'")

internal fun sessionsDateWindow(
    startMs: Long?,
    endMs: Long?,
    startRaw: String?,
    endRaw: String?,
): Pair<Long?, Long?> {
    if (startMs != null && endMs != null && endMs < startMs) {
        throw InvertedDateWindow(startRaw.orEmpty(), endRaw.orEmpty())
    }
    return startMs to endMs
}

internal fun sessionsDateBound(flag: String, raw: String?, parse: (String) -> Long?): Long? {
    if (raw.isNullOrBlank()) return null
    return parse(raw) ?: throw InvalidDateArg(flag, raw)
}
"""


# [da1033-D] ISSUE-04 green fixtures for the payload-JSON arm. Synthetic but
# shape-faithful: every accepted spelling appears once (variable, JSON builder,
# helper call, raw JSON literal, expression-into-arrayOf, a prose literal that
# lives in a COMMENT) plus the two N2 spellings (`return data.toString(2)`).
# [dv14-A1] green fixture for the notification arm: the deferred decision is
# taken from the RAW `--after` string, and the "--after or --at is required"
# refusal stays reachable behind it.
GREEN_NOTIFICATION_HANDLE = r"""
    private fun handleSend(sub: String, args: OffloadArgs): NativeOffloadResult {
        val title = args.get("title") ?: return NativeOffloadResult(2, "android-notification: missing --title\n")
        val body = args.get("body") ?: ""
        when (val plan = sendDisposition(sub, args.get("after"), args.get("at"))) {
            is SendDisposition.Invalid -> return NativeOffloadResult(2, plan.message)
            is SendDisposition.Deferred -> return scheduleDeferred(title, body, plan.secs, plan.at, args)
            SendDisposition.Immediate -> Unit
        }
        return NativeOffloadResult(0, JSONObject().put("scheduled", "immediate").toString(2) + "\n")
    }

    private fun scheduleDeferred(
        title: String,
        body: String,
        afterSec: Int?,
        atStr: String?,
        args: OffloadArgs,
    ): NativeOffloadResult {
        return when {
            afterSec != null && afterSec <= 0 ->
                NativeOffloadResult(2, "android-notification: --after must be positive\n")
            atStr != null -> unreachable()
            else -> NativeOffloadResult(2, "android-notification: --after or --at is required\n")
        }
    }

    companion object {
        internal sealed class SendDisposition {
            class Deferred(val secs: Int?, val at: String?) : SendDisposition()
            class Invalid(val message: String) : SendDisposition()
            object Immediate : SendDisposition()
        }

        internal fun sendDisposition(sub: String, rawAfter: String?, rawAt: String?): SendDisposition {
            val secs = when (rawAfter) {
                null -> null
                else -> rawAfter.toIntOrNull() ?: return SendDisposition.Invalid("bad --after\n")
            }
            if (secs != null || rawAt != null) return SendDisposition.Deferred(secs, rawAt)
            if (sub == "schedule") return SendDisposition.Deferred(null, null)
            return SendDisposition.Immediate
        }
    }
"""

GREEN_PAYLOAD_PHOTOS = r"""
    private fun nearPhotos(lat: Double, lon: Double, radiusKm: Double, max: Int): String {
        val photos = JSONArray()
        // A prose payload quoted in a comment must not trip the gate:
        // formatBody("No photos found within 1km of (1.0, 2.0).", args)
        val data = JSONObject()
            .put("media", photos)
            .put("count", photos.length())
            .put("lat", lat)
            .put("lon", lon)
            .put("radius_km", radiusKm)
        if (photos.length() == 0) {
            val hint = if (scanned == scanLimit) "scanned the newest 500 photos; no matches in that window" else ""
            if (hint.isNotEmpty()) data.put("hint", hint)
        }
        return data.toString(2)
    }

    private fun handleNear(args: OffloadArgs): NativeOffloadResult {
        return NativeOffloadResult(0, OffloadOutput.formatBody(nearPhotos(1.0, 2.0, 1.0, 20), args) + "\n")
    }
"""

GREEN_PAYLOAD_CONTACTS = r"""
    private fun queryFailedBody(message: String): String =
        JSONObject().put("error", "query_failed").put("message", message).toString()


    private fun doList(parsedArgs: OffloadArgs): NativeOffloadResult {
        val cursor = context.contentResolver.query(uri, projection, null, null, sort)
            ?: return NativeOffloadResult(
                1,
                OffloadOutput.formatBody(
                    queryFailedBody("Failed to query contacts."),
                    parsedArgs,
                ) + "\n",
            )
        val listArr = JSONArray()
        return NativeOffloadResult(0, OffloadOutput.formatBody(listArr.toString(2), parsedArgs) + "\n")
    }

    private fun doSearch(parsedArgs: OffloadArgs): NativeOffloadResult {
        val searchArr = JSONArray()
        return NativeOffloadResult(0, OffloadOutput.formatBody(searchArr.toString(2), parsedArgs) + "\n")
    }

    private fun doGet(id: Long, parsedArgs: OffloadArgs): NativeOffloadResult {
        cursor.use {
            if (!it.moveToFirst()) {
                val body = JSONObject().put("error", "not_found").put("id", id)
                    .put("message", "No contact with id=$id.")
                    .toString()
                return NativeOffloadResult(1, OffloadOutput.formatBody(body, parsedArgs) + "\n")
            }
        }
        // Precision arm: an expression-built payload (the header/staging shape)
        // is not the literal this rule judges.
        return NativeOffloadResult(
            0,
            OffloadOutput.formatBody(arrayOf("All contacts (N):").joinToString("\n"), parsedArgs) + "\n",
        )
    }

    private fun doStrict(parsedArgs: OffloadArgs): NativeOffloadResult {
        return NativeOffloadResult(0, OffloadOutput.formatBody(@Q3@{"media": [], "count": 0}@Q3@, parsedArgs) + "\n")
    }

    private fun doFromConstant(parsedArgs: OffloadArgs): NativeOffloadResult {
        return NativeOffloadResult(0, OffloadOutput.formatBody(HELP_TEXT, parsedArgs) + "\n")
    }

    private fun doTemplate(parsedArgs: OffloadArgs): NativeOffloadResult {
        val code = "x"
        val payload = JSONObject().put("a", 1)
        return NativeOffloadResult(
            1,
            OffloadOutput.formatBody("{\"error\":\"${code}\"}", parsedArgs) + "\n" +
                OffloadOutput.formatBody("${payload}", parsedArgs) + "\n",
        )
    }
""".replace("@Q3@", '"' * 3)

# [da1034-A4] ISSUE-08 — the six files the rule set grew to cover. One green
# fixture per shape family so every new arm has a clean control next to it.
GREEN_PAYLOAD_LOCATION = r"""
    private fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        return try {
            when (val sub = request.argv.getOrNull(1)) {
                "current" -> current(args)
                else -> NativeOffloadResult(2, OffloadOutput.formatBody(
                    JSONObject().put("error", "usage").toString(), args) + "\n")
            }
        } catch (e: Throwable) {
            // A prose payload quoted in a comment must not trip the gate:
            // formatBody("android-location: ${e.message ?: "unknown error"}", args)
            val body = JSONObject()
                .put("error", "uncaught")
                .put("message", e.message ?: "unknown error")
                .toString()
            NativeOffloadResult(1, OffloadOutput.formatBody(body, args) + "\n")
        }
    }

    private fun reverseGeocodeJson(lat: Double, lon: Double, lines: List<String>): JSONObject {
        val arr = JSONArray()
        lines.forEach { arr.put(it) }
        return JSONObject()
            .put("latitude", lat)
            .put("longitude", lon)
            .put("address", lines.joinToString(", "))
            .put("count", lines.size)
    }

    private fun geocode(lat: Double, lon: Double, args: OffloadArgs): NativeOffloadResult {
        val lines = listOf<String>()
        return NativeOffloadResult(
            0,
            OffloadOutput.formatBody(reverseGeocodeJson(lat, lon, lines).toString(2), args) + "\n",
        )
    }
"""

GREEN_PAYLOAD_SIDE_EFFECTS = r"""
    private fun doSet(cm: ClipboardManager, args: OffloadArgs): NativeOffloadResult {
        val label = args.get("label") ?: "RikkaMinis"
        val setData = JSONObject()
            .put("copied", true)
            .put("characters", 5)
            .put("label", label)
        return NativeOffloadResult(0, OffloadOutput.formatBody(setData.toString(2), args) + "\n")
    }

    private fun doClear(cm: ClipboardManager, args: OffloadArgs): NativeOffloadResult {
        // formatBody("Clipboard cleared.", args) — a comment, not a payload
        val clearData = JSONObject().put("cleared", true)
        return NativeOffloadResult(0, OffloadOutput.formatBody(clearData.toString(2), args) + "\n")
    }

    private fun openUrl(url: String, args: OffloadArgs): NativeOffloadResult {
        val openData = JSONObject().put("opened", true).put("url", url)
        return NativeOffloadResult(0, OffloadOutput.formatBody(openData.toString(2), args) + "\n")
    }

    private fun clearAll(nm: NotificationManager, args: OffloadArgs): NativeOffloadResult {
        val notifyData = JSONObject().put("cleared", "all")
        return NativeOffloadResult(0, OffloadOutput.formatBody(notifyData.toString(2), args) + "\n")
    }
"""

GREEN_PAYLOAD_CALENDAR = r"""
    private fun doList(cals: List<CalendarRef>, args: OffloadArgs): NativeOffloadResult {
        val events = JSONArray()
        val data = JSONObject()
            .put("events", events)
            .put("count", events.length())
        // A prose payload quoted in a comment must not trip the gate:
        // formatBody("android-calendar: no writable calendar", args)
        return NativeOffloadResult(0, OffloadOutput.formatBody(data.toString(2), args) + "\n")
    }
"""

GREEN_PAYLOAD_FASTIO_USAGE = r"""
    private fun usage(reason: String, args: OffloadArgs): String =
        OffloadOutput.formatBody(usageText(reason), args) + "\n"

    private fun usageText(reason: String): String = JSONObject()
        .put("error", "usage")
        .put("message", reason)
        .put("help", HELP)
        .toString()

    private fun banner(reason: String): String {
        // Not a formatBody argument ⇒ N2 must NOT judge it (precision control
        // for the scope narrowing).
        return "minis-fastio: " + reason
    }
"""

# [dv18-A A-03] green fixture for the photos --album classification arms. The
# refusal reachability is written as the real when-branch shape, so a mutation
# that restores the silent downgrade trips P2.
GREEN_ALBUM_PHOTOS = """
    private fun handleAlbum(args: OffloadArgs): NativeOffloadResult {
        when (val selector = resolveAlbumSelector(args.get("id"), args.get("name"))) {
            is AlbumSelector.ById -> return ok(listOf(selector.id))
            is AlbumSelector.ByName -> return ok(listOf(selector.name))
            is AlbumSelector.Unreadable -> return NativeOffloadResult(2, albumSelectorRefusal(selector))
            AlbumSelector.Absent -> return NativeOffloadResult(
                2,
                "android-photos album: --id <bucket_id> or --name <bucket_name> is required\\n",
            )
        }
    }

    private fun handleAlbums(args: OffloadArgs): NativeOffloadResult {
        return ok(emptyList())
    }

internal sealed class AlbumSelector {
    object Absent : AlbumSelector()
    data class ById(val id: Long) : AlbumSelector()
    data class ByName(val name: String) : AlbumSelector()
    data class Unreadable(val flag: String, val raw: String) : AlbumSelector()
}

internal fun resolveAlbumSelector(rawId: String?, rawName: String?): AlbumSelector {
    if (rawId != null) {
        val id = rawId.toLongOrNull()
        return if (id != null) AlbumSelector.ById(id) else AlbumSelector.Unreadable("--id", rawId)
    }
    if (rawName != null) {
        return if (rawName.isNotBlank()) AlbumSelector.ByName(rawName)
        else AlbumSelector.Unreadable("--name", rawName)
    }
    return AlbumSelector.Absent
}

internal fun albumSelectorRefusal(selector: AlbumSelector.Unreadable): String =
    "android-photos album: " + selector.flag + " '" + selector.raw + "' is not usable\\n"
"""

def selftest() -> int:
    # Each case mutates ONE arm and names the rules it must trip. The expected
    # sets are written by hand from the rule table (never derived from a run:
    # a derived expectation would follow the checker into being wrong).
    red_cases = [
        ("raw argv slot read",
         GREEN_CONTACTS.replace('val needsWrite = cmd.sub == "delete"',
                                'val needsWrite = rawArgs[0] == "delete"'),
         GREEN_CONFIG, {"C2", "C10"}),
        ("raw argv dispatch",
         GREEN_CONTACTS.replace("when (val sub = cmd.sub)", "when (val sub = rawArgs[0])"),
         GREEN_CONFIG, {"C3", "C10"}),
        ("raw tail passed to a body",
         GREEN_CONTACTS.replace('"list" -> doList(parsedArgs)',
                                '"list" -> doList(rawArgs.drop(1), parsedArgs)'),
         GREEN_CONFIG, {"C11"}),
        ("id off the raw list",
         GREEN_CONTACTS.replace("id = args.positional.getOrNull(1)?.toLongOrNull()",
                                "id = args.firstOrNull()?.toLongOrNull()"),
         GREEN_CONFIG, {"C7", "C12"}),
        ("help not declared boolean",
         GREEN_CONTACTS.replace('booleanFlags = setOf("help", "h")', "booleanFlags = emptySet()"),
         GREEN_CONFIG, {"C8"}),
        ("max resolver loses the parsed shape",
         GREEN_CONTACTS.replace(
             "internal fun listMax(parsedArgs: OffloadArgs, default: Int = DEFAULT_LIST_MAX): Int {",
             "internal fun listMax(args: List<String>): Int {"),
         GREEN_CONFIG, {"C9"}),
        ("sub off the raw list",
         GREEN_CONTACTS.replace("sub = args.positional.getOrNull(0)", "sub = rawArgs[0]"),
         GREEN_CONFIG, {"C6", "C10"}),
        ("a comment cannot mask a raw argv read",
         GREEN_CONTACTS.replace("val cmd = parseContactsCommand(rawArgs)",
                                "val cmd = parseContactsCommand(rawArgs)\n        val x = rawArgs[0]"),
         GREEN_CONFIG, {"C10"}),
        ("cwd-less --file call",
         GREEN_CONTACTS,
         GREEN_CONFIG.replace("readLinuxPath(fileArg, request.sessionId, request.cwd) ?: return errorResult(",
                              "readLinuxPath(fileArg, request.sessionId) ?: return errorResult(", 1),
         {"K7", "K8"}),
        ("resolver drops the cwd",
         GREEN_CONTACTS,
         GREEN_CONFIG.replace("private fun readLinuxPath(linuxPath: String, sessionId: String?, cwd: String?): String?",
                              "private fun readLinuxPath(linuxPath: String, sessionId: String?): String?", 1),
         {"K1"}),
        ("join helper renamed",
         GREEN_CONTACTS,
         GREEN_CONFIG.replace("internal fun resolveMaybeRelative(path: String, cwd: String?): String =",
                              "internal fun unusedHelper(): Int = 0"),
         {"K2"}),
        ("absolute-cwd guard dropped",
         GREEN_CONTACTS,
         GREEN_CONFIG.replace('if (!path.startsWith("/") && !cwd.isNullOrEmpty() && cwd.startsWith("/"))',
                              'if (!path.startsWith("/"))'),
         {"K3"}),
        ("session resolver bypassed",
         GREEN_CONTACTS,
         GREEN_CONFIG.replace("PRootKernel.resolvePathForSession(sessionId, resolved, context)",
                              "File(resolved)"),
         {"K5"}),
        # [da1020-C] new arms
        ("relative --cookies-file join dropped",
         GREEN_CONTACTS,
         GREEN_CONFIG,
         GREEN_BROWSERUSE.replace(
             "val joined = resolveMaybeRelative(cookiesFile, cwd)\n"
             "            val host = PRootKernel.resolvePathForSession(sessionId, joined, app)",
             "val host = PRootKernel.resolvePathForSession(sessionId, cookiesFile, app)"),
         GREEN_FASTIO, {"B5", "B6", "B7"}),
        ("--paths tokens reach the resolver unjoined",
         GREEN_CONTACTS, GREEN_CONFIG,
         GREEN_BROWSERUSE.replace("val tokens = resolveMaybeRelativeAll(rawTokens, cwd)",
                                  "val tokens = rawTokens"),
         GREEN_FASTIO, {"B3"}),
        ("buildInputJson loses the cwd parameter",
         GREEN_CONTACTS, GREEN_CONFIG,
         GREEN_BROWSERUSE.replace(
             "private fun buildInputJson(args: OffloadArgs, sessionId: String?, cwd: String?): JSONObject",
             "private fun buildInputJson(args: OffloadArgs, sessionId: String?): JSONObject").replace(
             "buildInputJson(args, request.sessionId, request.cwd)",
             "buildInputJson(args, request.sessionId)"),
         GREEN_FASTIO, {"B1", "B8"}),
        ("cwd-less call site only",
         GREEN_CONTACTS, GREEN_CONFIG,
         GREEN_BROWSERUSE.replace("buildInputJson(args, request.sessionId, request.cwd)",
                                  "buildInputJson(args, request.sessionId)"),
         GREEN_FASTIO, {"B8"}),
        ("a comment cannot mask a raw tar verdict",
         GREEN_CONTACTS, GREEN_CONFIG, GREEN_BROWSERUSE,
         GREEN_FASTIO.replace("val isTarCommand = firstSubcommandToken(rawArgv) == \"tar\"",
                              "val isTarCommand = firstSubcommandToken(rawArgv) == \"tar\"\n"
                              "        val x = rawArgv.firstOrNull() == \"tar\""),
         {"F4"}),
        ("tar verdict reverted to the first raw token",
         GREEN_CONTACTS, GREEN_CONFIG, GREEN_BROWSERUSE,
         GREEN_FASTIO.replace("val isTarCommand = firstSubcommandToken(rawArgv) == \"tar\"",
                              "val isTarCommand = rawArgv.firstOrNull() == \"tar\""),
         {"F2", "F4"}),
        ("expander guard reverted to the first raw token",
         GREEN_CONTACTS, GREEN_CONFIG, GREEN_BROWSERUSE,
         GREEN_FASTIO.replace("if (firstSubcommandToken(argv) != \"tar\") return argv",
                              "if (argv.firstOrNull() != \"tar\") return argv"),
         {"F3", "F4"}),
        ("boolean-flag sets inlined back into the head",
         GREEN_CONTACTS, GREEN_CONFIG, GREEN_BROWSERUSE,
         GREEN_FASTIO.replace("booleanFlags = fastioBooleanFlags(isTarCommand)",
                              'booleanFlags = if (isTarCommand) setOf("recursive", "force", "ignore-case") '
                              'else setOf("recursive", "force", "ignore-case")'),
         {"F6"}),
        ("flag-set resolver renamed",
         GREEN_CONTACTS, GREEN_CONFIG, GREEN_BROWSERUSE,
         GREEN_FASTIO.replace("internal fun fastioBooleanFlags(isTarCommand: Boolean): Set<String>",
                              "internal fun unusedFlags(): Int = 0"),
         {"F5"}),
        ("expander made private again",
         GREEN_CONTACTS, GREEN_CONFIG, GREEN_BROWSERUSE,
         GREEN_FASTIO.replace("internal fun normalizeTarShorts(argv: List<String>): List<String>",
                              "private fun normalizeTarShorts(argv: List<String>): List<String>"),
         {"F7"}),
    ]
    # [da1026-B] the flag-set arm: (label, output_src, speak_src, want).
    red_cases_flags = [
        ("OUTPUT_FLAGS reverted to the three output-shaping names",
         GREEN_OUTPUT.replace('setOf("compact", "quiet", "q", "help", "h")',
                              'setOf("compact", "quiet", "q")'),
         GREEN_SPEAK, {"O2", "O3"}),
        ("OUTPUT_FLAGS loses only help",
         GREEN_OUTPUT.replace('"q", "help", "h"', '"q", "h"'),
         GREEN_SPEAK, {"O2"}),
        ("OUTPUT_FLAGS loses only h",
         GREEN_OUTPUT.replace('"q", "help", "h"', '"q", "help"'),
         GREEN_SPEAK, {"O3"}),
        ("OUTPUT_FLAGS declaration removed entirely",
         GREEN_OUTPUT.replace("    val OUTPUT_FLAGS: Set<String> = setOf(\"compact\", \"quiet\", \"q\", \"help\", \"h\")\n", ""),
         GREEN_SPEAK, {"O1"}),
        ("a comment naming help/h cannot satisfy the declaration",
         GREEN_OUTPUT.replace(
             '    val OUTPUT_FLAGS: Set<String> = setOf("compact", "quiet", "q", "help", "h")',
             '    // setOf("compact", "quiet", "q", "help", "h")\n'
             '    val OUTPUT_FLAGS: Set<String> = setOf("compact", "quiet", "q")'),
         GREEN_SPEAK, {"O2", "O3"}),
        ("speak handler parses with the no-arg default again",
         GREEN_OUTPUT,
         GREEN_SPEAK.replace(', booleanFlags = setOf("stop", "status")', ""),
         {"S1", "S2"}),
        ("speak declares stop but not status",
         GREEN_OUTPUT,
         GREEN_SPEAK.replace('setOf("stop", "status")', 'setOf("stop")'),
         {"S2"}),
    ]
    failures = 0
    for label, output, speak, want in red_cases_flags:
        if output == GREEN_OUTPUT and speak == GREEN_SPEAK:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_flag_set(output, speak)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    for label, contacts, config, *extra in red_cases:
        if len(extra) == 1:
            browseruse, fastio, want = GREEN_BROWSERUSE, GREEN_FASTIO, extra[0]
        else:
            browseruse, fastio, want = extra
        if (contacts == GREEN_CONTACTS and config == GREEN_CONFIG
                and browseruse == GREEN_BROWSERUSE and fastio == GREEN_FASTIO):
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_text(contacts, config, browseruse, fastio)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # [da1028-A] ISSUE-08 arms. Expected rule-id sets are written by hand from
    # DEBUG_RULES — never derived from a run.
    red_cases_debug = [
        ("forwardedArgv falls back to the greedy leftovers everywhere",
         GREEN_DEBUG.replace(
             "): List<String> = if (sub in PASSTHROUGH_SUBCOMMANDS) rawRest else args.positional.drop(1)",
             "): List<String> = args.positional.drop(1)"),
         {"D3"}),
        ("the rest slice is rebuilt from the greedy parse at the call site",
         GREEN_DEBUG.replace("val rest = forwardedArgv(sub, args, rawRest)",
                             "val rest = args.positional.drop(1)"),
         {"D9"}),
        ("the shizuku branch stops forwarding the verbatim tail",
         GREEN_DEBUG.replace('RpcCall("debug.shizuku.exec", buildExecForwarder(rest, args))',
                             'RpcCall("debug.shizuku.exec", buildExecForwarder(args.positional, args))'),
         {"D10", "D14"}),
        ("the model-use branch stops forwarding the verbatim tail",
         GREEN_DEBUG.replace('RpcCall("debug.modelUse.exec", buildExecForwarder(rest, args))',
                             'RpcCall("debug.modelUse.exec", buildExecForwarder(args.positional, args))'),
         {"D11", "D14"}),
        ("the rawRest never reaches buildRpcCall",
         GREEN_DEBUG.replace("buildRpcCall(sub, args, argv.rawRest)", "buildRpcCall(sub, args)"),
         {"D7", "D15"}),
        ("forwardedArgv is called without the verbatim tail",
         GREEN_DEBUG.replace("val rest = forwardedArgv(sub, args, rawRest)",
                             "val rest = forwardedArgv(sub, args)"),
         {"D9", "D16"}),
        ("the exec branch forwards the greedy leftovers",
         GREEN_DEBUG.replace("execCommandLine(rest)", "execCommandLine(args.positional)"),
         {"D12"}),
        ("output shaping reverts to the whole-argv view",
         GREEN_DEBUG.replace(
             "if (sub in PASSTHROUGH_SUBCOMMANDS) OffloadArgs(argv.globals, BOOLEAN_FLAGS) else args",
             "args"),
         {"D13"}),
        ("the structural argv view is dropped from the head",
         GREEN_DEBUG.replace("val argv = splitArgv(raw)",
                             "val argv = DebugArgvSplit(emptyList(), null, emptyList())"),
         {"D5"}),
        ("a fresh greedy parse is handed to the forwarder",
         GREEN_DEBUG.replace('RpcCall("debug.modelUse.exec", buildExecForwarder(rest, args))',
                             'RpcCall("debug.modelUse.exec", buildExecForwarder(OffloadArgs(raw).positional, args))'),
         {"D11", "D17"}),
    ]
    for label, debug_src, want in red_cases_debug:
        if debug_src == GREEN_DEBUG:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_debug(debug_src)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # [da1032-C] each Sessions rule must be winnable by exactly the mutation it
    # pins — the funnel rename, the per-command parse coming back, the guard
    # losing its throw, the hand-written "Valid:" list, the SUBCOMMANDS drift.
    red_cases_sessions = [
        ("the funnel is renamed",
         GREEN_SESSIONS.replace(
             "private fun resolveDateBounds(args: OffloadArgs): Pair<Long?, Long?>",
             "private fun resolveBounds(args: OffloadArgs): Pair<Long?, Long?>"),
         {"J1"}),
        ("a subcommand skips the funnel",
         GREEN_SESSIONS.replace("val (startMs, endMs) = resolveDateBounds(args)",
                                "val (startMs, endMs) = resolveBounds(args)", 1),
         {"J4"}),
        ("--start is parsed outside the funnel",
         GREEN_SESSIONS.replace(
             'val startMs = sessionsDateBound("--start", args.get("start")) { parseSessionsDate(it) }',
             'val startMs = parseDate(args.get("start"))'),
         {"J2", "J12"}),
        ("--end is parsed outside the funnel",
         GREEN_SESSIONS.replace(
             'val endMs = sessionsDateBound("--end", args.get("end")) { parseEndDate(it) }',
             'val endMs = parseEndDate(args.get("end"))'),
         {"J3", "J13"}),
        ("sessionsDateBound is renamed",
         GREEN_SESSIONS.replace(
             "internal fun sessionsDateBound(flag: String, raw: String?, parse: (String) -> Long?): Long?",
             "internal fun bound(flag: String, raw: String?, parse: (String) -> Long?): Long?"),
         {"J5"}),
        ("parseSessionsDate is renamed",
         GREEN_SESSIONS.replace("internal fun parseSessionsDate(raw: String?): Long?",
                                "internal fun parseDate(raw: String?): Long?"),
         {"J6"}),
        ("the guard stops refusing",
         GREEN_SESSIONS.replace("return parse(raw) ?: throw InvalidDateArg(flag, raw)",
                                "return parse(raw)"),
         {"J15"}),
        ("the refusal carrier is renamed",
         GREEN_SESSIONS.replace("internal class InvalidDateArg(flag: String, raw: String) :",
                                "internal class BadDateArg(flag: String, raw: String) :"),
         {"J7"}),
        ("handle stops catching the refusal",
         GREEN_SESSIONS.replace("} catch (e: InvalidDateArg) {", "} catch (e: Exception) {"),
         {"J8"}),
        ("the declared list drops send",
         GREEN_SESSIONS.replace(
             'private val SUBCOMMANDS = listOf("list", "search", "messages", "send")',
             'private val SUBCOMMANDS = listOf("list", "search", "messages")'),
         {"J9"}),
        ("the Valid: list is hand-written again",
         GREEN_SESSIONS.replace('Valid: ${SUBCOMMANDS.joinToString(", ")}',
                                "Valid: list, search, messages."),
         {"J10", "J11"}),
        ("the dispatch table loses send",
         GREEN_SESSIONS.replace('"send" -> cmdSend(args)', '"send" -> doNothing(args)'),
         {"J14"}),
    ]
    for label, sessions_src, want in red_cases_sessions:
        if sessions_src == GREEN_SESSIONS:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_sessions(sessions_src)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # [dv15-A1 A-01] K9 tooth — the config parser must be BUILT with the
    # value-taking short options; expectation written by hand from CONFIG_RULES.
    for label, config_src, want in [
        ("config value short options dropped",
         GREEN_CONFIG.replace('valueShortOptions = setOf("f", "p", "s")',
                              "valueShortOptions = emptySet()"),
         {"K9"}),
    ]:
        if config_src == GREEN_CONFIG:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_text(
            GREEN_CONTACTS, config_src, GREEN_BROWSERUSE, GREEN_FASTIO)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # [dv15-A1 A-08] J16–J20 teeth — the inverted window must exist, be the ONLY
    # comparison (an `<=` would call an equal window inverted), be reached from
    # the shared bounds helper, and refuse through its own exception type.
    for label, sessions_src, want in [
        ("date window helper renamed",
         GREEN_SESSIONS.replace("internal fun sessionsDateWindow(",
                                "internal fun unusedWindow("),
         {"J16"}),
        ("inverted window compared with <=",
         GREEN_SESSIONS.replace("endMs < startMs", "endMs <= startMs"),
         {"J17", "J18"}),
        ("window refused with a foreign exception",
         GREEN_SESSIONS.replace(
             "throw InvertedDateWindow(startRaw.orEmpty(), endRaw.orEmpty())",
             'throw IllegalArgumentException("bad window")'),
         {"J20"}),
        ("bounds bypass the window",
         GREEN_SESSIONS.replace(
             'return sessionsDateWindow(startMs, endMs, args.get("start"), args.get("end"))',
             "return startMs to endMs"),
         {"J19"}),
    ]:
        if sessions_src == GREEN_SESSIONS:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_sessions(sessions_src)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # [dv15-A1 A-01/A-02] ARGS teeth — every value-binding rule has one arm.
    # Expectations are written by hand from ARGS_RULES, never derived from a run.
    for label, args_src, want in [
        ("value token helper gone",
         GREEN_ARGS.replace("private fun valueTokenOrNull(token: String?): String? =",
                            "private fun unusedTokenHelper(token: String?): String? ="),
         {"A1"}),
        ("a flag is accepted as its own value",
         GREEN_ARGS.replace(
             'token?.takeIf { !it.startsWith("-") || isNegativeNumberToken(it) }', "token"),
         {"A2"}),
        ("negative-number guard gone",
         GREEN_ARGS.replace("private fun isNegativeNumberToken(token: String): Boolean =",
                            "private fun unusedNegativeGuard(token: String): Boolean ="),
         {"A3"}),
        ("digit look-ahead dropped",
         GREEN_ARGS.replace("token.length > 1 && token[0] == '-' && token[1].isDigit()",
                            "token.length > 1 && token[0] == '-'"),
         {"A4"}),
        ("value short options not declared",
         GREEN_ARGS.replace("private val valueShortOptions: Set<String> = emptySet(),",
                            "private val otherShortOptions: Set<String> = emptySet(),"),
         {"A5"}),
        ("boolean flags win over the value declaration",
         GREEN_ARGS.replace(
             "if (name in valueShortOptions && name !in effectiveBooleanFlags && value != null) {",
             "if (name in valueShortOptions && value != null) {"),
         {"A6"}),
        ("value lookup never recorded",
         GREEN_ARGS.replace("vs[name] = value", "vs.put(name, value)"),
         {"A7"}),
        ("dashed token joins the positionals",
         GREEN_ARGS + '\n    val probe = a.startsWith("-") && a.length > 1 -> fs.add(a)',
         {"A8"}),
        ("single-dash branch loses its negative-number exception",
         GREEN_ARGS.replace(
             'a.startsWith("-") && a.length > 1 &&\n'
             '                (!isNegativeNumberToken(a) || a.substring(1) in effectiveBooleanFlags) -> {',
             'a.startsWith("-") && a.length > 1 -> {'),
         {"A9"}),
        ("single-dash branch renamed away",
         GREEN_ARGS.replace('a.startsWith("-") && a.length > 1', 'a.startsWith("-") && a.size > 1'),
         {"A9"}),
    ]:
        if args_src == GREEN_ARGS:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_args(args_src)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # [da1033-D] ISSUE-04 — each payload spelling has a tooth: the prose return
    # (N2), the prose first argument in a one-line / multi-line / raw-string
    # call (N1), and the unterminated literal (fail-closed, not a silent skip).
    red_cases_payload = [
        ("photos payload reverts to the English sentence",
         GREEN_PAYLOAD_PHOTOS.replace('return data.toString(2)',
                                      'return "No photos found within ${radiusKm}km of ($lat, $lon).$hint"'),
         GREEN_PAYLOAD_CONTACTS, {"N2"}),
        ("prose behind an interpolation is still prose",
         GREEN_PAYLOAD_PHOTOS,
         GREEN_PAYLOAD_CONTACTS.replace("formatBody(searchArr.toString(2), parsedArgs)",
                                        'formatBody("Failed to query contacts for ${name}.", parsedArgs)'),
         {"N1"}),
        ("the near call site inlines a prose literal",
         GREEN_PAYLOAD_PHOTOS.replace("formatBody(nearPhotos(1.0, 2.0, 1.0, 20), args)",
                                      'formatBody("No photos found within 1km of (1.0, 2.0).", args)'),
         GREEN_PAYLOAD_CONTACTS, {"N1"}),
        ("the raw-string prose spelling is caught too",
         GREEN_PAYLOAD_PHOTOS.replace("formatBody(nearPhotos(1.0, 2.0, 1.0, 20), args)",
                                      "formatBody(" + '"' * 3 + "No photos found within 1km." + '"' * 3 + ", args)"),
         GREEN_PAYLOAD_CONTACTS, {"N1"}),
        ("an unterminated literal fails closed",
         GREEN_PAYLOAD_PHOTOS.replace("formatBody(nearPhotos(1.0, 2.0, 1.0, 20), args)",
                                      'formatBody("No photos found within 1km.\n, args)'),
         GREEN_PAYLOAD_CONTACTS, {"N1"}),
        ("contacts no-match reverts to prose",
         GREEN_PAYLOAD_PHOTOS,
         GREEN_PAYLOAD_CONTACTS.replace("formatBody(searchArr.toString(2), parsedArgs)",
                                        'formatBody("No contacts found matching \'q\'.", parsedArgs)'),
         {"N1"}),
        ("contacts not-found reverts to the bare id sentence",
         GREEN_PAYLOAD_PHOTOS,
         GREEN_PAYLOAD_CONTACTS.replace("formatBody(body, parsedArgs)",
                                        'formatBody("android-contacts: no contact with id=$id", parsedArgs)'),
         {"N1"}),
        ("the multi-line query-failure call inlines the sentence",
         GREEN_PAYLOAD_PHOTOS,
         GREEN_PAYLOAD_CONTACTS.replace('queryFailedBody("Failed to query contacts.")',
                                        '"Failed to query contacts."'),
         {"N1"}),
    ]
    for label, photos_src, contacts_src, want in red_cases_payload:
        if photos_src == GREEN_PAYLOAD_PHOTOS and contacts_src == GREEN_PAYLOAD_CONTACTS:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_offload_json(
            ("PhotosOffloadHandler.kt", photos_src),
            ("ContactsOffloadHandler.kt", contacts_src))}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # [da1034-A4] ISSUE-08 — the rule set grew from 2 files to 8, so the arms
    # below are the teeth for the six newly covered ones. One mutation per
    # spelling the real tree shipped (the bare sentence at the call site, the
    # sentence behind an interpolation, the prose return of a payload helper)
    # and one per file, each against its own green control above.
    red_cases_payload_a4 = [
        ("the location uncaught branch reverts to the sentence",
         GREEN_PAYLOAD_LOCATION.replace(
             "OffloadOutput.formatBody(body, args)",
             'OffloadOutput.formatBody("android-location: unknown error", args)'),
         {"N1"}),
        ("the reverse geocode reverts to the English sentence",
         GREEN_PAYLOAD_LOCATION.replace(
             "OffloadOutput.formatBody(reverseGeocodeJson(lat, lon, lines).toString(2), args)",
             'OffloadOutput.formatBody("No address found for ($lat, $lon).", args)'),
         {"N1"}),
        ("the reverse geocode helper returns prose",
         GREEN_PAYLOAD_LOCATION.replace('return JSONObject()\n            .put("latitude", lat)',
                                        'return "No address found for ($lat, $lon)."\n        // .put("latitude", lat)'),
         {"N2"}),
        ("the expression-built address lines are left alone (rule boundary)",
         GREEN_PAYLOAD_LOCATION.replace(
             "OffloadOutput.formatBody(reverseGeocodeJson(lat, lon, lines).toString(2), args)",
             'OffloadOutput.formatBody(lines.joinToString("\\n"), args)'),
         set()),
        ("the calendar list payload reverts to the prose sentence",
         GREEN_PAYLOAD_CALENDAR.replace(
             "OffloadOutput.formatBody(data.toString(2), args)",
             'OffloadOutput.formatBody("android-calendar: no writable calendar", args)'),
         {"N1"}),
        ("the clipboard set reverts to the prose sentence",
         GREEN_PAYLOAD_SIDE_EFFECTS.replace(
             "OffloadOutput.formatBody(setData.toString(2), args)",
             'OffloadOutput.formatBody("Copied 5 characters to clipboard.", args)'),
         {"N1"}),
        ("the clipboard clear reverts to the prose sentence",
         GREEN_PAYLOAD_SIDE_EFFECTS.replace(
             "OffloadOutput.formatBody(clearData.toString(2), args)",
             'OffloadOutput.formatBody("Clipboard cleared.", args)'),
         {"N1"}),
        ("the open success reverts to the Opened: sentence",
         GREEN_PAYLOAD_SIDE_EFFECTS.replace(
             "OffloadOutput.formatBody(openData.toString(2), args)",
             'OffloadOutput.formatBody("Opened: $url", args)'),
         {"N1"}),
        ("the notification clear reverts to the prose sentence",
         GREEN_PAYLOAD_SIDE_EFFECTS.replace(
             "OffloadOutput.formatBody(notifyData.toString(2), args)",
             'OffloadOutput.formatBody("All notifications cleared.", args)'),
         {"N1"}),
        ("a non-payload helper is handed to formatBody and its prose return is caught",
         GREEN_PAYLOAD_SIDE_EFFECTS.replace(
             "OffloadOutput.formatBody(openData.toString(2), args)",
             "OffloadOutput.formatBody(localSummary(), args)").replace(
             "    private fun openUrl",
             '    private fun localSummary(): String {\n'
             '        return "clipboard: ok"\n'
             '    }\n\n'
             "    private fun openUrl"),
         {"N2"}),
        ("the fastio usage reverts to the inline prose payload",
         GREEN_PAYLOAD_FASTIO_USAGE.replace(
             "OffloadOutput.formatBody(usageText(reason), args)",
             'OffloadOutput.formatBody("minis-fastio: " + reason + "\\n" + HELP, args)'),
         {"N1"}),
        ("the usage payload helper reverts to a prose return",
         GREEN_PAYLOAD_FASTIO_USAGE.replace(
             "    private fun usageText(reason: String): String = JSONObject()\n",
             '    private fun usageText(reason: String): String {\n'
             '        return "minis-fastio: " + reason\n'
             '    }\n\n'
             '    private fun unusedText(reason: String): String = JSONObject()\n'),
         {"N2"}),
    ]
    for label, src, want in red_cases_payload_a4:
        if src in (GREEN_PAYLOAD_LOCATION, GREEN_PAYLOAD_SIDE_EFFECTS,
                   GREEN_PAYLOAD_FASTIO_USAGE, GREEN_PAYLOAD_CALENDAR):
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_offload_json(("fixture.kt", src))}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # [dv14-A1] one arm per spelling the three defects shipped (ISSUE-01/02/04),
    # each against its own green control: the dead single-dash alias read, the
    # resolver bypassed, the alias never bound, the parsed `--after` read, the
    # owner function renamed away, the refusal guard deleted, and the raw-argv
    # scan knowing too small a boolean set (both halves of it).
    red_cases_bindings = [
        ("contacts: doSearch reverts to the dead alias read",
         GREEN_CONTACTS.replace("val max = listMax(parsedArgs, 20)",
                                'val max = parsedArgs.getInt("max", "n") ?: 20'),
         GREEN_NOTIFICATION_HANDLE, GREEN_FASTIO, {"V1a", "V1b"}),
        ("contacts: doSearch stops reusing the resolver",
         GREEN_CONTACTS.replace("val max = listMax(parsedArgs, 20)", "val max = 20"),
         GREEN_NOTIFICATION_HANDLE, GREEN_FASTIO, {"V1b"}),
        ("contacts: the -n alias stops being bound before the parse",
         GREEN_CONTACTS.replace("OffloadArgs(bindLegacyShortMax(argv), booleanFlags",
                                "OffloadArgs(argv, booleanFlags"),
         GREEN_NOTIFICATION_HANDLE, GREEN_FASTIO, {"V1c"}),
        ("contacts: the bind helper is renamed",
         GREEN_CONTACTS.replace(
             "internal fun bindLegacyShortMax(argv: List<String>): List<String> {",
             "internal fun unusedBinder(argv: List<String>): List<String> {"),
         GREEN_NOTIFICATION_HANDLE, GREEN_FASTIO, {"V1d"}),
        ("notification: handleSend goes back to the parsed read",
         GREEN_CONTACTS,
         GREEN_NOTIFICATION_HANDLE.replace(
             "val plan = sendDisposition(sub, args.get(\"after\"), args.get(\"at\"))",
             'val plan = args.getInt("after")'),
         GREEN_FASTIO, {"V2a", "V2b"}),
        ("notification: handleSend stops consulting the disposition",
         GREEN_CONTACTS,
         GREEN_NOTIFICATION_HANDLE.replace(
             "val plan = sendDisposition(sub, args.get(\"after\"), args.get(\"at\"))",
             "val plan = SendDisposition.Immediate"),
         GREEN_FASTIO, {"V2b"}),
        ("notification: the disposition helper is renamed",
         GREEN_CONTACTS,
         GREEN_NOTIFICATION_HANDLE.replace("internal fun sendDisposition(sub: String,",
                                           "internal fun unusedDisposition(sub: String,"),
         GREEN_FASTIO, {"V2c"}),
        ("notification: the required-argument refusal is deleted",
         GREEN_CONTACTS,
         GREEN_NOTIFICATION_HANDLE.replace(
             "android-notification: --after or --at is required\\n",
             "android-notification: nothing to schedule\\n"),
         GREEN_FASTIO, {"V2d"}),
        ("fastio: the scan forgets the boolean flag set",
         GREEN_CONTACTS, GREEN_NOTIFICATION_HANDLE,
         GREEN_FASTIO.replace(
             "val noValue = OffloadOutput.OUTPUT_FLAGS + fastioBooleanFlags(isTarCommand = true)",
             "val noValue = OffloadOutput.OUTPUT_FLAGS"),
         {"V3a"}),
        ("fastio: the scan forgets the global output flags",
         GREEN_CONTACTS, GREEN_NOTIFICATION_HANDLE,
         GREEN_FASTIO.replace(
             "val noValue = OffloadOutput.OUTPUT_FLAGS + fastioBooleanFlags(isTarCommand = true)",
             "val noValue = fastioBooleanFlags(isTarCommand = true)"),
         {"V3b"}),
        ("fastio: the scan becomes an expression body (fail-closed)",
         GREEN_CONTACTS, GREEN_NOTIFICATION_HANDLE,
         GREEN_FASTIO.replace(
             "internal fun firstSubcommandToken(rawArgv: List<String>): String? {",
             "internal fun firstSubcommandToken(rawArgv: List<String>): String? = rawArgv.firstOrNull {"),
         {"V3c"}),
        ("a comment cannot mask the boolean-flag reference",
         GREEN_CONTACTS, GREEN_NOTIFICATION_HANDLE,
         GREEN_FASTIO.replace(
             "val noValue = OffloadOutput.OUTPUT_FLAGS + fastioBooleanFlags(isTarCommand = true)",
             "// val noValue = Output.OUTPUT_FLAGS + fastioBooleanFlags(true)"),
         {"V3a", "V3b"}),
        # [dv18-A A-01b] the value-short consumption arms — a pre-scan that
        # drops the declared set (or stops consuming values) mis-reads
        # `-C /tmp` and loses the tar verbs + `--directory` expansion.
        ("fastio: the value-short set is undeclared",
         GREEN_CONTACTS, GREEN_NOTIFICATION_HANDLE,
         GREEN_FASTIO.replace('private val SHORT_VALUE_OPTIONS = setOf("-C")',
                              'private val UNUSED_SHORTS = setOf("-C")'),
         {"V3d"}),
        ("fastio: the scan stops consulting the value-short set",
         GREEN_CONTACTS, GREEN_NOTIFICATION_HANDLE,
         GREEN_FASTIO.replace("token in SHORT_VALUE_OPTIONS -> true", "false -> true"),
         {"V3e"}),
        ("fastio: the scan stops consuming values",
         GREEN_CONTACTS, GREEN_NOTIFICATION_HANDLE,
         GREEN_FASTIO.replace("i += if (consumesValue) 2 else 1", "i += 1"),
         {"V3f"}),
    ]
    for label, contacts_src, notif_src, fastio_src, want in red_cases_bindings:
        if contacts_src == GREEN_CONTACTS and notif_src == GREEN_NOTIFICATION_HANDLE \
                and fastio_src == GREEN_FASTIO:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_offload_cli_bindings(contacts_src, notif_src, fastio_src)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # [dv18-A A-03] the photos --album classification arms. Expected sets are
    # hand-written from the P-table (never derived from a run).
    red_cases_album = [
        ("photos: the album classifier is gone",
         GREEN_ALBUM_PHOTOS.replace("internal fun resolveAlbumSelector(",
                                    "internal fun unusedSelector("),
         {"P1"}),
        ("photos: the refusal is dropped",
         GREEN_ALBUM_PHOTOS.replace(
             "return NativeOffloadResult(2, albumSelectorRefusal(selector))",
             "return ok(emptyList())"),
         {"P2"}),
        ("photos: the classifier stops classifying",
         GREEN_ALBUM_PHOTOS.replace("val id = rawId.toLongOrNull()", "val id = rawId.toLong()"),
         {"P3"}),
        ("photos: handleAlbums is gone",
         GREEN_ALBUM_PHOTOS.replace("fun handleAlbums(", "fun unusedAlbums("),
         {"P4"}),
    ]
    for label, photos_src, want in red_cases_album:
        if photos_src == GREEN_ALBUM_PHOTOS:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_photos_album(photos_src)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # the green arms must be fully clean, or every rule is vacuous
    clean = check_text(GREEN_CONTACTS, GREEN_CONFIG, GREEN_BROWSERUSE, GREEN_FASTIO)
    clean_flags = check_flag_set(GREEN_OUTPUT, GREEN_SPEAK)
    clean_debug = check_debug(GREEN_DEBUG)
    clean_sessions = check_sessions(GREEN_SESSIONS)
    clean_args = check_args(GREEN_ARGS)
    clean_album = check_photos_album(GREEN_ALBUM_PHOTOS)
    clean_bindings = check_offload_cli_bindings(
        GREEN_CONTACTS, GREEN_NOTIFICATION_HANDLE, GREEN_FASTIO)
    clean_payload = check_offload_json(
        ("PhotosOffloadHandler.kt", GREEN_PAYLOAD_PHOTOS),
        ("ContactsOffloadHandler.kt", GREEN_PAYLOAD_CONTACTS),
        ("CalendarOffloadHandler.kt", GREEN_PAYLOAD_CALENDAR),
        ("LocationOffloadHandler.kt", GREEN_PAYLOAD_LOCATION),
        ("ClipboardOffloadHandler.kt", GREEN_PAYLOAD_SIDE_EFFECTS),
        ("NotificationOffloadHandler.kt", GREEN_PAYLOAD_SIDE_EFFECTS),
        ("OpenOffloadHandler.kt", GREEN_PAYLOAD_SIDE_EFFECTS),
        ("FastioOffloadHandler.kt", GREEN_PAYLOAD_FASTIO_USAGE),
    )
    if clean or clean_flags or clean_debug or clean_sessions or clean_bindings \
            or clean_payload or clean_album:
        green_findings = (clean + clean_flags + clean_debug + clean_sessions
                          + clean_bindings + clean_payload + clean_album)
        print(f"SELFTEST-FAIL: green fixtures must produce no findings, "
              f"got {describe(green_findings)}")
        failures += 1
    if failures:
        print(f"SELFTEST: {failures} failing case(s)")
        return 1
    print("OFFLOAD_CLI_ARGS_SELFTEST=PASS")
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    # Order-independent: gate_run.sh calls `python3 <gate> --selftest <repo>`,
    # test_scan.py calls `python3 <gate> --selftest`, humans call `... <repo>`.
    if "--selftest" in argv:
        return selftest()
    positional = [a for a in argv if not a.startswith("--")]
    if not positional:
        print(__doc__)
        return 2
    root = Path(positional[0])
    contacts_path = root / CONTACTS
    config_path = root / CONFIG
    browseruse_path = root / BROWSERUSE
    fastio_path = root / FASTIO
    output_path = root / OUTPUT
    speak_path = root / SPEAK
    debug_path = root / DEBUG_HANDLER
    sessions_path = root / SESSIONS
    notification_path = root / NOTIFICATION
    args_path = root / ARGS
    if not contacts_path.is_file() or not config_path.is_file() \
            or not browseruse_path.is_file() or not fastio_path.is_file() \
            or not output_path.is_file() or not speak_path.is_file() \
            or not debug_path.is_file() or not sessions_path.is_file() \
            or not notification_path.is_file() or not args_path.is_file():
        print(f"GATE-FAIL: handler source not found under {root}")
        return 1
    # fail-closed: a payload file that moves (or is renamed) must be a red
    # gate, not a silently narrower rule set.
    payload_files = []
    for rel in PAYLOAD_FILES:
        if not (root / rel).is_file():
            print(f"GATE-FAIL: payload source not found: {rel} under {root}")
            return 1
        payload_files.append((rel, (root / rel).read_text(encoding="utf-8")))
    findings = check_text(contacts_path.read_text(encoding="utf-8"),
                          config_path.read_text(encoding="utf-8"),
                          browseruse_path.read_text(encoding="utf-8"),
                          fastio_path.read_text(encoding="utf-8"))
    findings += check_flag_set(output_path.read_text(encoding="utf-8"),
                               speak_path.read_text(encoding="utf-8"))
    findings += check_debug(debug_path.read_text(encoding="utf-8"))
    findings += check_sessions(sessions_path.read_text(encoding="utf-8"))
    findings += check_args(args_path.read_text(encoding="utf-8"))
    findings += check_photos_album((root / PHOTOS).read_text(encoding="utf-8"))
    findings += check_offload_cli_bindings(contacts_path.read_text(encoding="utf-8"),
                                          notification_path.read_text(encoding="utf-8"),
                                          fastio_path.read_text(encoding="utf-8"))
    findings += check_offload_json(*payload_files)
    if findings:
        print("offload_cli_args_check: FAIL")
        print(describe(findings))
        return 1
    print("offload_cli_args_check: PASS (contacts dispatch parsed, config --file cwd-aware, "
          "browseruse paths joined, fastio tar verdict shared, "
          "OUTPUT_FLAGS covers help/h, speak declares stop/status, "
          "sessions date bounds funneled + refuse invalid, "
          "debug pass-through argv verbatim + global-only output shaping, "
          "8/8 payload files hand formatBody JSON (N1 whole-file, N2 payload helpers), "
          "contacts search -n bound to --max through the one resolver, notification --after "
          "refuses an unreadable value, fastio subcommand scan knows its boolean flags "
          "and consumes -C, photos --album decision classified (non-numeric --id refuses))")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

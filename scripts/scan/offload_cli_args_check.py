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
[dv27-A5 A-07] addition (the exit code of a help-guarded early exit):

 13. Five handlers (`android-model-use`, `android-alarm`, `android-photos`,
     `android-speech`, `android-a11y`) guarded `--help` AND a bare call with
     one condition (`hasFlag("h", "help") || positional.isEmpty()`) and then
     decided the exit code on POSITIONAL emptiness. `OffloadArgs` folds
     `--help` / `-h` into flags, so `tool --help` leaves positional empty
     exactly like a bare `tool`: the `else 0` arm was dead, every `tool --help`
     exited 2 and `set -e` / `cmd --help && …` probe chains broke (the agent
     reads a help request as a failure). H1 pins that a help probe reaches
     exit 0 (a positional test cannot separate the two invocations); H2 keeps
     the bare-invocation contract at 2 wherever the exit code varies; H3 is
     fail-closed on the derivation itself (>= 15 sites, one decidable early
     exit per help-probing file). The accepted spellings are derived, not
     listed: a literal 0, a `const val … = 0` name, and `if (<token count>)
     2 else 0` (DebugOffloadHandler's shape) all pass.

Success marker on selftest: OFFLOAD_CLI_ARGS_SELFTEST=PASS

[dv19-A1] additions (the same defect family one layer up: a decision read from
   the WRONG text, or a guard that watches the wrong moment):

 10. `ContactsOffloadHandler.bindLegacyShortMax` (V1e/V1f/V1g) — the subcommand
     probe runs BEFORE the alias is bound, so `-n 10` is still an unbound
     single-dash token there. Probing the raw argv put "10" in `positional[0]`,
     `sub` came out "10" (neither list nor search), the helper returned the
     un-rewritten argv and the caller refused with `unknown subcommand '10'`
     (exit 2) — while the same alias AFTER the subcommand worked. Pinned as a
     decision: the list handed to the probe is not the caller's argv, the
     `--max` rewrite is built no later than the probe, and the probed list is
     the one the function returns.
 11. `SessionsOffloadHandler` (J21/J22) — `--wait` was missing from the declared
     boolean set although the KDoc documents it as `send`'s default mode, so
     `minis-sessions-cli --wait send --prompt "hi"` bound "send" as the VALUE of
     `--wait`, left `positional` empty and ran the default `list` subcommand at
     exit 0. J21 reads the declared SET (membership, not spelling: `wait` must
     be in it and the value option `--wait-timeout` must not), J22 requires the
     flag to have its own HELP_TEXT line.
 12. `FastioSearch.grepTree` (S1/S2/S3a/S3b) — the caller spends ONE match
     budget across several roots (`remaining -= outcome.matches.size`), so a
     later root is walked with `limit == 0`. The cap used to be watched only
     inside `grepFile`, i.e. at the first MATCHING line: a root of non-matching
     files was read end to end (up to 8 MiB per file) to list nothing, and the
     directories it walked through were still charged as visited. Pinned on the
     `grepTree` body: S1 the entry answers a non-positive budget as capped with
     NO walk, S2 `preVisitDirectory` consults the cap BEFORE charging, S3a/S3b
     `visitFile` decides after `grepFile` returned instead of continuing.
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
# [dv19-A1 A-03] the grep/find walk engine lives beside the handler; the
# per-root match budget is enforced there, so the gate has to read it.
FASTIO_SEARCH = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/FastioSearch.kt"
# [dv22-A6 A-06] clipboard clear — see check_clipboard_clear.
CLIPBOARD = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ClipboardOffloadHandler.kt"
# [dv25-A4 A-07] the player CLI: its payload used to be the manager's English
# sentence, so N1 accepted a variable and N2 (same-file helpers) could not see
# the cross-file call. Registered here so both rules judge it, and Z1-Z4 below
# police the provenance itself.
PLAYER = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/PlayerOffloadHandler.kt"

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
    CLIPBOARD,
    "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/OpenOffloadHandler.kt",
    FASTIO,
    PLAYER,
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
    # [dv19-A1 A-02] `--wait` is documented (cmdSend's KDoc) as the DEFAULT mode
    # of `send`, so it is a spelling callers legitimately write. While `wait` was
    # missing from the declared boolean set, `--wait send --prompt "hi"` bound
    # "send" as the VALUE of `--wait`, `positional` came out empty and the
    # command ran the default `list` subcommand at exit 0 — a wrong answer that
    # looks like a successful call. J21 reads the declared SET (membership, so
    # reordering/reformatting cannot trip it) and stays fail-closed on the value
    # option: declaring `--wait-timeout` boolean would eat its `<sec>`. J22 makes
    # the flag discoverable — `--wait-timeout`'s line does not satisfy it.
    ("J21", "decl_re", (r"booleanFlags = setOf\(([^)]*)\)", ['"wait"'], ['"wait-timeout"'])),
    ("J22", "count_re", (r"(?m)^\s*--wait\s+\S", 1)),
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
        elif kind == "decl_re":
            # [dv19-A1 A-02] judge a declared SET by membership, not spelling:
            # `needle = (re, names that must be in group(1), names that must not)`.
            pattern, must, must_not = needle
            m = re.search(pattern, sessions)
            if m is None:
                findings.append((rid, "missing", pattern))
            else:
                declared = m.group(1)
                for name in must:
                    if name not in declared:
                        findings.append((rid, f"undeclared {name}", pattern))
                for name in must_not:
                    if name in declared:
                        findings.append((rid, f"must not declare {name}", pattern))
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

    # V1e/V1f/V1g — [dv19-A1 A-01] the subcommand probe must read the REWRITTEN
    # list. The probe runs before the alias is bound, so `-n 10` is still an
    # unbound single-dash token there: judging the raw argv put "10" in
    # positional[0], `sub` came out "10" (neither list nor search), the helper
    # returned the un-rewritten argv and the caller refused with
    # `unknown subcommand '10'` (exit 2) — while the same alias AFTER the
    # subcommand worked. Pinned as the decision, not a spelling: the list handed
    # to the probe is not the caller's argv, the `--max` rewrite is built no
    # later than the probe, and the probed list is the one the function returns.
    bind = _payload_helper_bodies(contacts, "bindLegacyShortMax")
    if bind is not None:
        probe = re.search(r"OffloadArgs\(\s*([A-Za-z_]\w*)\s*[,)]", bind)
        if probe is None:
            findings.append(
                ("V1e", "missing",
                 "bindLegacyShortMax must probe the subcommand through OffloadArgs("))
        else:
            probed = probe.group(1)
            if probed == "argv":
                findings.append(
                    ("V1e", "present",
                     "the subcommand probe reads the raw (pre-rewrite) argv"))
            elif not re.search(r"\breturn\s+" + re.escape(probed) + r"\b", bind):
                findings.append(
                    ("V1g", "missing",
                     f"the probed list ({probed}) must be the list the helper returns"))
        max_at = bind.find('"--max"')
        if max_at < 0:
            findings.append(
                ("V1f", "missing", 'the `--max` rewrite of the `-n <N>` alias'))
        elif probe is not None and max_at > probe.start():
            findings.append(
                ("V1f", "present", "the `--max` rewrite runs AFTER the subcommand probe"))

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


# ── [dv19-A1 A-03] the match budget is spent per ROOT, not per tree ─────────
#
# `grep` (FastioOffloadHandler) carries ONE budget across several roots —
# `remaining -= outcome.matches.size` — and hands a later root `limit = 0`. The
# cap used to be watched in exactly one place (`grepFile`, i.e. at the first
# MATCHING line), so such a root was walked end to end: every non-matching file
# read (up to MAX_GREP_FILE_BYTES = 8 MiB each) to list nothing, and the
# directories it descended were still charged as visited (turning a
# matches_capped result into `truncated` too). Each rule pins one of the three
# moments a walk can spend that budget, on the `grepTree` body:
#   S1  the entry: a non-positive budget is answered as capped, with NO walk
#   S2  preVisitDirectory consults the cap BEFORE charging the directory
#   S3a/S3b visitFile decides after grepFile returned (the file that filled the
#       budget ends the walk) instead of continuing unconditionally
def _brace_block(text: str, marker: str):
    """Verbatim `{…}` block whose opening brace is the first one after `marker`."""
    i = text.find(marker)
    if i < 0:
        return None
    j = text.find("{", i)
    if j < 0:
        return None
    depth = 0
    k = j
    while k < len(text):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[j:k + 1]
        k += 1
    return None


def check_fastio_search(fastio_search_src: str) -> list:
    findings = []
    src = code_only(fastio_search_src)
    tree = _payload_helper_bodies(src, "grepTree")
    if tree is None:
        findings.append(
            ("S1", "missing", "internal fun grepTree(…, limit: Int, out: GrepOutcome)"))
        return findings
    walk_at = tree.find("Files.walkFileTree(")
    if walk_at < 0:
        findings.append(("S1", "missing", "grepTree must walk with Files.walkFileTree("))
        return findings
    head = tree[:walk_at]
    if not re.search(r"limit\s*<=\s*0", head):
        findings.append(
            ("S1", "missing",
             "grepTree must short-circuit a non-positive budget (limit <= 0) before the walk"))
    elif "matchesCapped = true" not in head:
        findings.append(
            ("S1", "missing",
             "the non-positive-budget branch must answer out.matchesCapped = true"))

    pre = _brace_block(tree, "override fun preVisitDirectory")
    if pre is None:
        findings.append(("S2", "missing", "grepTree's preVisitDirectory override"))
    else:
        cap_at = pre.find("matchesCapped")
        charge_at = pre.find("charge(")
        if cap_at < 0:
            findings.append(
                ("S2", "missing", "preVisitDirectory must consult out.matchesCapped"))
        elif charge_at < 0 or cap_at > charge_at:
            findings.append(
                ("S2", "present",
                 "preVisitDirectory charges the directory before consulting the cap"))

    visit = _brace_block(tree, "override fun visitFile")
    if visit is None:
        findings.append(("S3a", "missing", "grepTree's visitFile override"))
    else:
        grep_at = visit.find("grepFile(")
        if grep_at < 0:
            findings.append(("S3a", "missing", "visitFile must walk files through grepFile("))
        else:
            tail = visit[grep_at:]
            if re.search(r"matchesCapped[\s\S]{0,160}?\bTERMINATE\b", tail) is None:
                findings.append(
                    ("S3a", "missing",
                     "visitFile must terminate once grepFile filled the budget"))
            if re.search(r"grepFile\([\s\S]{0,240}?return FileVisitResult\.CONTINUE", visit):
                findings.append(
                    ("S3b", "present",
                     "visitFile returns CONTINUE unconditionally after grepFile"))
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

# ── [dv22-A6 A-06] android-clipboard clear: an empty string is not a clear ──
#
# `doClear` used to write `setPrimaryClip(newPlainText("", ""))` and call it
# done: the clipboard still holds an item (itemCount 1, text/plain
# description), so `status` kept answering has_content = true for a clipboard
# `clear` had just reported as cleared = true. Its background guard was also
# the only one of the four (get/set/clear/status) missing `&& SDK_INT >= Q`,
# refusing pre-Q background clears that Android 8–9 actually permit.
# Four judgements, all derived from the tree:
#   L1  doClear keeps its owner body (a renamed / deleted owner is a red
#       gate, not a silently narrower rule set)
#   L2  the platform clear `clearPrimaryClip(` is reached from doClear
#   L3  every `!isAppForeground()` site keeps the `&& SDK_INT >= Q` half —
#       per-site derived, so a fifth subcommand is covered the day it lands
#       (no hand-written member list to go stale)
#   L4  doClear keeps BOTH write arms and they sit in different branches:
#       the empty-string arm is what keeps API 26–28 working (clearPrimaryClip
#       is not reliable there), and an unconditional clear-then-set would put
#       the empty item right back even on Q+
FOREGROUND_Q_RE = r"!isAppForeground\(\)\s*&&\s*Build\.VERSION\.SDK_INT\s*>=\s*Build\.VERSION_CODES\.Q"
BARE_FOREGROUND_RE = r"!isAppForeground\(\)(?!\s*&&\s*Build\.VERSION\.SDK_INT\s*>=\s*Build\.VERSION_CODES\.Q)"

def check_clipboard_clear(clipboard_src: str) -> list:
    """[dv22-A6 A-06] the L1–L4 rules above."""
    code = code_only(clipboard_src)
    findings = []
    body = _payload_helper_bodies(code, "doClear")
    if body is None:
        findings.append(("L1", "missing",
                         "doClear lost its owner body (renamed / inlined / deleted?)"))
        return findings
    if "clearPrimaryClip(" not in body:
        findings.append(("L2", "missing",
                         "clear must reach the platform clearPrimaryClip() — an empty "
                         "string write leaves itemCount = 1 and status answers has_content = true"))
    if re.search(BARE_FOREGROUND_RE, code):
        findings.append(("L3", "regressed",
                         "an isAppForeground() guard dropped its `&& SDK_INT >= Q` half "
                         "(pre-Q background clipboard access is legal; refusing it breaks clear)"))
    if not re.search(FOREGROUND_Q_RE, body):
        findings.append(("L3", "missing",
                         "doClear's background guard must be the exact get/set/status shape"))
    if 'setPrimaryClip(ClipData.newPlainText("", ""))' not in body:
        findings.append(("L4", "missing",
                         "the API 26–28 empty-string fallback is gone — clearPrimaryClip() "
                         "is not reliable there"))
    else:
        platform_at = body.find("clearPrimaryClip(")
        if platform_at >= 0:
            fallback_at = body.find('ClipData.newPlainText("", "")')
            between = body[platform_at:fallback_at] if fallback_at > platform_at \
                else body[fallback_at:platform_at]
            if "else" not in between and "->" not in between:
                findings.append(("L4", "unbranched",
                                 "the two clear writes must sit in different branches — "
                                 "unconditional clear-then-set puts the empty item back on Q+"))
    return findings


# ── [deep-audit-1044 A-04] the `send` subcommand's status → exit-code contract ──
#
# `ChatMutationMethods.prompt` does NOT throw when the send is dropped — it
# answers with a structured `status` ("Error" + the reason in `responseText`),
# and with "Timeout" when the wait elapsed. `cmdSend` used to catch exceptions
# only and then `emit(...)` unconditionally, and `emit()` hard-codes `ok: true`
# with exit code 0 — so every refusal (no provider, compacting in flight,
# context exhausted, wait timeout) left the CLI as a SUCCESS whose
# `response_text` carried a machine reason in the place an assistant reply
# belongs. A shell caller could not tell a delivered prompt from one that
# never left the device.
#
# The rule binds the DECISION POINTS, not one spelling:
#   S1 — the status must be READ from the RPC result (`optString("status")`).
#   S2 — the success set must be consulted (`sendSucceeded(`): only
#        "Completed" / "Running" are successes; every other status (including
#        an unknown or empty one) is a refusal.
#   S3 — a refusal must go through the file's existing `errorEnvelope` funnel
#        (no second envelope shape).
#   S4 — a refusal must pick its exit code through `sendExitFor(` (nonzero).
#   S5 — the refusal branch must be REACHED BEFORE `emit("send"…)`: the success
#        payload may only be emitted on the success path.
#   S6 — the success set must be exactly {"Completed", "Running"} (two
#        comparisons against the status) — a set that also accepts "" or an
#        unknown status re-opens the defect.
#   S7 — the exit code must be the file's failure code (1), not 0.
#
# Judged on comment-stripped text, so a status literal quoted in a comment
# cannot satisfy OR trip the rule.
_SEND_STATUS_RE = re.compile(r'optString\(\s*"status"\s*\)')


def _send_helper_body(code: str, name: str):
    """Body of `fun <name>(…)` in [code], or None (expression body / absent)."""
    m = re.search(r"\bfun\s+" + re.escape(name) + r"\s*\(", code)
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


GREEN_SESSIONS_SEND = """
class SessionsOffloadHandler {
    private fun cmdSend(args: OffloadArgs): NativeOffloadResult {
        val result = runBlocking {
            com.rikkaminis.app.debug.ChatMutationMethods.prompt(context, params)
        }
        val status = result.optString("status")
        val data = JSONObject()
            .put("session_id", result.optString("sessionId"))
            .put("status", status)
        if (result.has("responseText")) data.put("response_text", result.optString("responseText"))
        if (!sendSucceeded(status)) {
            val detail = JSONObject()
                .put("session_id", data.optString("session_id"))
                .put("status", status)
            val err = errorEnvelope(
                "send",
                sendErrorCode(status),
                sendErrorReason(status, data.optString("response_text")),
                detail,
            )
            return NativeOffloadResult(
                sendExitFor(status),
                OffloadOutput.formatBody(err.toString(2), args) + "\\n",
            )
        }
        emit("send", data, args)
    }

    private fun errorEnvelope(
        action: String,
        code: String,
        msg: String,
        detail: JSONObject? = null,
    ): JSONObject {
        val data = JSONObject().put("error", code).put("message", msg)
        if (detail != null) data.put("detail", detail)
        return JSONObject().put("ok", false).put("action", action).put("data", data)
    }

    private fun emit(action: String, data: JSONObject, args: OffloadArgs): NativeOffloadResult {
        val envelope = JSONObject().put("ok", true).put("action", action).put("data", data)
        return NativeOffloadResult(0, OffloadOutput.formatBody(envelope.toString(2), args) + "\\n")
    }

    companion object {
        private const val EXIT_FAILURE = 1

        internal fun sendSucceeded(status: String): Boolean =
            status == "Completed" || status == "Running"

        internal fun sendExitFor(status: String): Int =
            if (sendSucceeded(status)) 0 else EXIT_FAILURE

        internal fun sendErrorCode(status: String): String =
            if (status == "Timeout") "SEND_TIMEOUT" else "SEND_FAILED"

        internal fun sendErrorReason(status: String, responseText: String?): String {
            val reason = responseText?.trim()
            if (!reason.isNullOrEmpty()) return reason
            return if (status == "Timeout") "timeout" else status.lowercase()
        }
    }
}
"""


def _cut_at_next_decl(region: str) -> str:
    """Trim a declaration window at the NEXT `fun`/`class`/`companion` keyword.

    A text window that runs past the helper's own declaration would swallow the
    NEXT helper's comparisons — `sendSucceeded` would then look like it
    compares three statuses. Cut at the next declaration so each window
    describes exactly one decision.
    """
    m = re.search(r"\b(fun|class|companion\s+object)\b", region[1:])
    return region[: m.start() + 1] if m else region


def _send_helper_region(code: str, name: str, span: int = 400) -> "str | None":
    """The declaration of `fun <name>(…)` plus [span] characters after it.

    A text window rather than a brace body on purpose: the decision helpers are
    EXPRESSION-bodied (`… : Boolean = status == "Completed" || …`) and have no
    braces of their own — a brace-hunting walk would read the NEXT function's
    body and pass or fail for the wrong reason.
    """
    m = re.search(r"\bfun\s+" + re.escape(name) + r"\s*\(", code)
    if m is None:
        return None
    return code[m.start(): m.start() + span]


def check_sessions_send_status(sessions_src: str) -> list:
    """[deep-audit-1044 A-04] — the `send` status → exit-code contract."""
    code = code_only(sessions_src)
    findings = []
    body = _send_helper_body(code, "cmdSend")
    if body is None:
        findings.append(("S1", "missing",
                         "cmdSend lost its owner body (renamed / inlined / deleted?)"))
        return findings
    if not _SEND_STATUS_RE.search(body):
        findings.append(("S1", "missing",
                         "cmdSend must read the RPC status (`optString(\"status\")`) — "
                         "a dropped send is reported in the payload, not by an exception"))
    if "sendSucceeded(" not in body:
        findings.append(("S2", "missing",
                         "cmdSend must consult the success set (`sendSucceeded(…)`) — "
                         "only Completed/Running are deliveries"))
    if "errorEnvelope(" not in body:
        findings.append(("S3", "missing",
                         "a refusal must go through the existing `errorEnvelope(` funnel — "
                         "the file's single error shape"))
    else:
        # The FIRST `errorEnvelope(` in cmdSend belongs to the earlier ARGUMENT
        # guards (SUBAGENT_DISABLED / INVALID_ARGS) — the refusal's own call is
        # the one AFTER the success-set consultation. Requiring that one keeps
        # the rule from being satisfied by an unrelated guard.
        gate_at = body.find("sendSucceeded(")
        if gate_at >= 0 and "errorEnvelope(" not in body[gate_at:]:
            findings.append(("S3", "missing",
                             "the refusal branch must call `errorEnvelope(` AFTER the "
                             "success-set consultation — the earlier argument guards "
                             "do not count"))
    if "sendExitFor(" not in body:
        findings.append(("S4", "missing",
                         "a refusal must pick its exit code through `sendExitFor(…)` — "
                         "the CLI contract (nonzero = not delivered)"))
    emit_at = body.find('emit("send"')
    if emit_at < 0:
        findings.append(("S5", "missing",
                         "cmdSend no longer emits its success payload (`emit(\"send\"…`)"))
    else:
        # Ordering, anchored on the success-set consultation rather than on the
        # envelope helper's name: whatever the refusal is spelled, it has to be
        # decided before the payload leaves.
        gate_at = body.find("sendSucceeded(")
        if gate_at < 0 or gate_at > emit_at:
            findings.append(("S5", "present",
                             "the success decision must be reached BEFORE `emit(\"send\"…)` — "
                             "the success payload may only be emitted on the success path"))
    succ = _send_helper_region(code, "sendSucceeded")
    if succ is None:
        findings.append(("S6", "missing",
                         "the success set helper `sendSucceeded(…)` is gone — the "
                         "status → success decision must live in ONE place"))
    else:
        succ = _cut_at_next_decl(succ)
        if '"Completed"' not in succ or '"Running"' not in succ:
            findings.append(("S6", "missing",
                             "the success set must contain Completed and Running"))
        compares = len(re.findall(r"status\s*==", succ))
        if compares != 2:
            findings.append(("S6", f"count {compares} != 2",
                             "the success set must compare the status against exactly "
                             "two values — an extra accepted status re-opens the defect"))
    exit_helper = _send_helper_region(code, "sendExitFor")
    if exit_helper is None:
        findings.append(("S7", "missing",
                         "the exit-code helper `sendExitFor(…)` is gone"))
    else:
        exit_helper = _cut_at_next_decl(exit_helper)
        if "EXIT_FAILURE" not in exit_helper:
            findings.append(("S7", "missing",
                             "the exit code must come from the file's failure constant"))
    if not re.search(r"EXIT_FAILURE\s*=\s*1\b", code):
        findings.append(("S7", "missing",
                         "the failure exit code must be 1 (the file's other error "
                         "branches use 1)"))
    # S8 — the reason must NOT ride along in the failure payload under the
    # `response_text` key: that key is where an assistant's answer belongs, and
    # a caller reading it would still see a machine code in the answer slot.
    # The reason belongs in the envelope's `message`; the diagnostic fields
    # (session id / status / timeout flag) may stay in `detail`.
    detail_at = body.find("val detail")
    # The FIRST `errorEnvelope(` in cmdSend belongs to the earlier argument
    # guards (SUBAGENT_DISABLED / INVALID_ARGS) — the refusal's own call is the
    # one AFTER the detail payload is built.
    err_at = body.find("errorEnvelope(", detail_at) if detail_at >= 0 else -1
    if detail_at >= 0 and err_at > detail_at:
        detail_region = body[detail_at:err_at]
        if "response_text" in detail_region:
            findings.append(("S8", "present",
                             "the failure payload must not carry the reason under "
                             "`response_text` — that key is the assistant's answer "
                             "slot; use the envelope `message` instead"))
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
            val rewritten = ArrayList<String>(argv.size)
            rewritten.add("--max")
            val sub = OffloadArgs(rewritten, booleanFlags = setOf("help", "h")).positional.getOrNull(0)
            if (sub != "list" && sub != "search") return argv
            return rewritten
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
        val args = OffloadArgs(
            request.argv.drop(1),
            booleanFlags = setOf("full", "async", "wait", "help", "h"),
        )
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
        private const val HELP_TEXT = \"\"\"
Usage: minis-sessions-cli <command> [options]

Commands:
  list                  List recent sessions
  send                  Send a prompt to a session

Options:
  --async               (send only) Return immediately with the session id
  --wait                (send only) Block until the reply completes (default; --async wins)
  --wait-timeout <sec>  (send only) Give up waiting after <sec> seconds
\"\"\"
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


# [dv19-A1 A-03] green fixture for the grep walk engine: shape-faithful skeleton
# of `grepTree` after the fix — the entry answers a non-positive budget as capped
# without walking, `preVisitDirectory` consults the cap before charging, and
# `visitFile` decides on the cap after `grepFile` returned.
GREEN_FASTIO_SEARCH = """
internal fun grepTree(
    root: Path,
    rootGuestPath: String,
    regex: Regex,
    limit: Int,
    maxEntries: Long,
    out: GrepOutcome,
) {
    if (limit <= 0) {
        out.matchesCapped = true
        return
    }
    try {
        Files.walkFileTree(
            root,
            object : SimpleFileVisitor<Path>() {
                override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (out.matchesCapped) return FileVisitResult.TERMINATE
                    if (charge(out, maxEntries)) return FileVisitResult.TERMINATE
                    return FileVisitResult.CONTINUE
                }

                override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (charge(out, maxEntries)) return FileVisitResult.TERMINATE
                    if (out.matchesCapped) return FileVisitResult.TERMINATE
                    grepFile(file, guestPathFor(root, rootGuestPath, file), regex, limit, out)
                    return if (out.matchesCapped) {
                        FileVisitResult.TERMINATE
                    } else {
                        FileVisitResult.CONTINUE
                    }
                }
            },
        )
    } catch (_: IOException) {
        out.failures++
    }
}

internal fun findTree(
    root: Path,
    regex: Regex,
    limit: Int,
    out: FindOutcome,
) {
    out.matched++
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

# [dv22-A6 A-06] the fixed clipboard clear shape, all four subcommand guards
# and both write arms. The red arms below are mutations of THIS text, so the
# expectations are hand-written from the L-table and never from a run.
GREEN_CLIPBOARD_CLEAR = """
    private fun doGet(cm: ClipboardManager, args: OffloadArgs): NativeOffloadResult {
        if (!isAppForeground() && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            return backgroundError(args)
        }
    }

    private fun doSet(cm: ClipboardManager, args: OffloadArgs): NativeOffloadResult {
        if (!isAppForeground() && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            return backgroundError(args)
        }
    }

    private fun doClear(cm: ClipboardManager, args: OffloadArgs): NativeOffloadResult {
        if (!isAppForeground() && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            return backgroundError(args)
        }
        when (clearAction(Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)) {
            ClipboardClearAction.PlatformClear -> cm.clearPrimaryClip()
            ClipboardClearAction.EmptyStringFallback -> cm.setPrimaryClip(ClipData.newPlainText("", ""))
        }
        val data = JSONObject().put("cleared", true)
        return NativeOffloadResult(0, OffloadOutput.formatBody(data.toString(2), args) + "\\n")
    }

    private fun doStatus(cm: ClipboardManager, args: OffloadArgs): NativeOffloadResult {
        if (!isAppForeground() && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            return backgroundError(args)
        }
    }
"""

OFFLOAD_HANDLER_DIR = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload"
OFFLOAD_SOURCE_DIRS = (
    OFFLOAD_HANDLER_DIR,
    "src/android/app/src/main/java/com/rikkaminis/app/offload",
)

# Handler basename → why it still ships a prose payload from another file.
PAYLOAD_DELEGATE_EXEMPT = {
    "WeatherOffloadHandler.kt": (
        "registered P2 (backlog 1045-A07): WeatherManager.fetchWeather() renders a "
        "human weather report; the structured sibling fetchWeatherJson() already "
        "exists, so this is a straightforward follow-up — deliberately not fixed in "
        "the android-player branch."
    ),
}

_PCS_FUN_RE = re.compile(r"\bfun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(\w+)\s*\(")
_PCS_STR_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"')


def _pcs_strip(src: str) -> str:
    """Comments out, string literals kept verbatim."""
    out = []
    i, n = 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "//":
            while i < n and src[i] != "\n":
                i += 1
        elif two == "/*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
        elif src[i] == '"':
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    break
                j += 1
            out.append(src[i:j + 1])
            i = j + 1
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def _pcs_match(code: str, i: int, opener: str, closer: str) -> int:
    depth = 0
    while i < len(code):
        c = code[i]
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
        elif c == '"':
            j = i + 1
            while j < len(code):
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == '"':
                    break
                j += 1
            i = j
        i += 1
    return len(code) - 1


def _pcs_decl_table(sources: dict) -> dict:
    """callee name → {(basename, declared return type)}."""
    table = {}
    for base, src in sources.items():
        code = _pcs_strip(src)
        for m in _PCS_FUN_RE.finditer(code):
            end = _pcs_match(code, m.end() - 1, "(", ")")
            tail = code[end + 1:end + 1 + 240]
            mm = re.match(r"\s*:\s*([A-Za-z_][\w.<>?, ]*)", tail)
            rt = mm.group(1).strip() if mm else ""
            table.setdefault(m.group(1), set()).add((base, rt))
    return table


def _pcs_first_arg(code: str, open_idx: int) -> str:
    i, depth, start = open_idx + 1, 0, open_idx + 1
    while i < len(code):
        c = code[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return code[start:i]
            depth -= 1
        elif c == "," and depth == 0:
            return code[start:i]
        elif c == '"':
            j = i + 1
            while j < len(code):
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == '"':
                    break
                j += 1
            i = j
        i += 1
    return code[start:]


def _pcs_init(code: str, after_eq: int) -> str:
    """Initializer text: balanced to the end of the statement."""
    i, depth = after_eq, 0
    while i < len(code):
        c = code[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return code[after_eq:i]
            depth -= 1
        elif c == '"':
            j = i + 1
            while j < len(code):
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == '"':
                    break
                j += 1
            i = j
        elif c == "\n" and depth == 0:
            return code[after_eq:i]
        i += 1
    return code[after_eq:]


def _pcs_bindings(code: str) -> dict:
    out = {}
    for m in re.finditer(r"\b(?:val|var)\s+(\w+)\s*(?::[^=\n]+)?=", code):
        out.setdefault(m.group(1), _pcs_init(code, m.end()))
    return out


def _pcs_calls(text: str) -> set:
    # raw strings first (prose in a `"""…"""` HELP block spells words like
    # "text [" and would otherwise read as calls), then ordinary literals
    stripped = re.sub(r'"""(?:[^"]|"(?!""))*"""', '""', text)
    stripped = _PCS_STR_RE.sub('""', stripped)
    return set(re.findall(r"\b(\w+)\s*\(", stripped))


def _pcs_value_text(text: str) -> str:
    """Value positions only: a nested `val x = …` initializer is an
    intermediate (its result is not the payload), and a `catch`/`finally` block
    is the failure path — neither may be attributed to the payload. Without
    this, `val result = try { … } catch { … }` charged every call in the catch
    branch (including `formatBody` itself) to the payload."""
    out = []
    i = 0
    while i < len(text):
        m = re.compile(r"\b(?:val|var)\s+\w+\s*(?::[^=\n]+)?=").search(text, i)
        k = re.compile(r"\b(?:catch|finally)\b").search(text, i)
        nxt = min([x.start() for x in (m, k) if x] or [len(text)])
        out.append(text[i:nxt])
        if nxt >= len(text):
            break
        head = text[nxt:]
        if m and m.start() == nxt:
            init = _pcs_init(text, m.end())
            i = m.end() + len(init)
        else:
            brace = text.find("{", nxt)
            i = len(text) if brace < 0 else _pcs_match(text, brace, "{", "}") + 1
    return "".join(out)


def _pcs_delegates(base: str, code: str, table: dict) -> list:
    """[(callee, return_type, declaring_file)] for the payload of every
    `formatBody(` call in this file, resolved one binding level down."""
    bind = _pcs_bindings(code)
    hits = []
    for m in re.finditer(r"formatBody\(", code):
        expr = _pcs_first_arg(code, m.end() - 1).strip()
        # the payload is <ident> or <ident>.<method>(…) — follow the receiver
        src_text = expr
        mm = re.fullmatch(r"([A-Za-z_]\w*)(?:\s*\.\s*\w+\s*\([^()]*\))?", expr)
        if mm and mm.group(1) in bind:
            src_text = bind[mm.group(1)]
        for callee in _pcs_calls(_pcs_value_text(src_text)):
            decls = table.get(callee, ())
            if not decls:
                continue
            if any(d == base for (d, _) in decls):
                continue  # a same-file declaration of that name wins
            for (d, rt) in decls:
                hits.append((callee, rt, d))
    return sorted(set(hits))


def check_payload_delegates(sources: dict, payload_files, exemptions=None) -> list:
    """Z1–Z4. `sources` maps basename → file text (both offload dirs)."""
    exemptions = PAYLOAD_DELEGATE_EXEMPT if exemptions is None else exemptions
    table = _pcs_decl_table(sources)
    derived = {}
    for base, src in sources.items():
        code = _pcs_strip(src)
        if "formatBody(" not in code:
            continue
        hits = _pcs_delegates(base, code, table)
        if hits:
            derived[base] = hits
    findings = []
    registered = {Path(p).name for p in payload_files}
    # Z4 (first: an empty derivation would make Z1/Z2 vacuous)
    if not derived:
        findings.append(("Z4", "payload delegates: the derivation found no file", "derived set is empty"))
    # Z1 — unregistered delegating handler
    for base in sorted(derived):
        if base in registered or base in exemptions:
            continue
        why = ", ".join("%s()→%s in %s" % h for h in derived[base])
        findings.append(("Z1", "formatBody payload delegated across files", "%s: %s" % (base, why)))
    # Z2 — a registered payload file must not take a String from another file
    for base in sorted(derived):
        if base not in registered:
            continue
        for (callee, rt, d) in derived[base]:
            if rt == "String":
                findings.append((
                    "Z2",
                    "payload file takes its payload from a String delegate",
                    "%s: %s()→String in %s (a sentence from another file)" % (base, callee, d),
                ))
    # Z3 — exemptions must still be needed
    for base, reason in exemptions.items():
        if not str(reason).strip():
            findings.append(("Z3", "exemption without a reason", base))
        if base not in sources:
            findings.append(("Z3", "exemption names a missing file", base))
        elif base not in derived:
            findings.append(("Z3", "exemption no longer needed", base))
    return findings

# [dv25-A4 A-07] payload provenance: a handler that assembles its body from a
# structured envelope (green) vs one that hands over another file's sentence.
GREEN_PLAYER_HANDLER = r"""
class PlayerOffloadHandler {
    fun handle(sub: String, args: OffloadArgs): NativeOffloadResult {
        val outcome = try {
            when (sub) {
                "play" -> MediaPlayerManager.play(args.positional[1])
                else -> PlayerOutcome.failure("unknown_subcommand", sub)
            }
        } catch (e: Throwable) {
            PlayerOutcome.failure(PlayerOutcome.ERROR_PLAYER_FAILED, e.message ?: "unknown")
        }
        val envelope = playerEnvelope(sub, outcome)
        return NativeOffloadResult(
            if (outcome.ok) 0 else 1,
            OffloadOutput.formatBody(envelope.toString(2), args) + "\n",
        )
    }
}
"""

PROSE_PLAYER_HANDLER = r"""
class PlayerOffloadHandler {
    fun handle(sub: String, args: OffloadArgs): NativeOffloadResult {
        val result = try {
            when (sub) {
                "play" -> MediaPlayerManager.play(args.positional[1])
                else -> "Error: unknown subcommand"
            }
        } catch (e: Throwable) {
            "Error: ${e.message}"
        }
        return NativeOffloadResult(0, OffloadOutput.formatBody(result.trimEnd('\n'), args) + "\n")
    }
}
"""

GREEN_PLAYER_MANAGER = r"""
object MediaPlayerManager {
    fun play(id: String): PlayerOutcome = PlayerOutcome.ok(session = id)
}
"""

PROSE_PLAYER_MANAGER = r"""
object MediaPlayerManager {
    fun play(id: String): String = "Playing '$id'"
}
"""

GREEN_PLAYER_OUTCOME = r"""
internal fun playerEnvelope(action: String, outcome: PlayerOutcome): JSONObject {
    return JSONObject().put("ok", outcome.ok).put("action", action)
}
"""

GREEN_WEATHER_HANDLER = r"""
class WeatherOffloadHandler {
    fun handle(args: OffloadArgs): NativeOffloadResult {
        val text = WeatherManager.fetchWeather(1.0, 2.0)
        return NativeOffloadResult(0, OffloadOutput.formatBody(text.trimEnd('\n'), args) + "\n")
    }
}
"""

GREEN_WEATHER_MANAGER = r"""
object WeatherManager {
    fun fetchWeather(latitude: Double, longitude: Double): String = "weather"
}
"""

# ── [dv27-A5 A-07] a help-guarded early exit must answer 0 for `--help` ──────
#
# `OffloadArgs` folds `--help` / `-h` into `flags` (both are in
# `OffloadOutput.OUTPUT_FLAGS`), so `tool --help` leaves `positional` EMPTY —
# byte-for-byte the state a bare `tool` is in. A guard that merges the two
# invocations (`hasFlag("h", "help") || positional.isEmpty()`) and then decides
# the exit code on *positional emptiness* therefore answers 2 for `--help` as
# well: its `else 0` arm is dead code. Five handlers shipped that shape, so
# every `tool --help` exited 2 and `set -e` / `cmd --help && …` probe chains
# broke (the agent reads `--help` as a failure).
#
# Rules, derived from the decision point (never from a spelling or a file list):
#   H1 — a help probe must be able to reach exit 0. For every `if (<guard>)`
#        whose condition probes the help flag, the guarded early exit's code
#        expression must evaluate to 0 while the flag is present:
#          • a literal 0 (or a `const val … = 0` name)                     → ok
#          • `if (C) A else B` where C probes the help flag: A (the help
#            side) must be 0;
#          • `if (C) A else B` where C does NOT probe it: C must then be a
#            TOKEN-COUNT separator (`argv.size` / `rawArgs.isEmpty`). A
#            POSITIONAL test cannot separate the two invocations — `--help` is
#            a flag, so positional stays empty — which is exactly the defect.
#        Anything else is fail-closed red (an exit code nobody can decide is
#        not evidence that `--help` works).
#   H2 — the bare-invocation contract stays 2 wherever it is expressible: when
#        the exit code varies and the guard can also be reached without help,
#        the branch serving the bare call must be 2 (the pinned contract).
#        A merged guard answering a literal 0 is left alone: `android-contacts`
#        / `android-browser-use` deliberately print help at exit 0 for a bare
#        call, and because their separator is a token count the help case is
#        still reachable — they are not this family.
#   H3 — fail-closed on the derivation itself: the family must still be found
#        (>= 15 sites) and a file that probes help must yield a decidable early
#        exit, so a rename or an extracted helper cannot narrow the rule to
#        nothing and turn the gate into a false green.
#
# Judged on comment-stripped text, so a KDoc quoting the old shape cannot trip
# the rule (1046-F9) and a comment cannot satisfy it either.
_HELP_PROBE_RE = re.compile(r'hasFlag\(([^)]*)\)')
_HELP_NAME_RE = re.compile(r'"([^"]*)"')
_NUM_LIT_RE = re.compile(r'^\d+$')
_BARE_SEPARATOR_RE = re.compile(
    r'\b(?:argv|rawArgs)\s*\.\s*(?:size|isEmpty|count|length|isNotEmpty)\b')
_POSITIONAL_TEST_RE = re.compile(
    r'\bpositional\s*\.\s*(?:isEmpty|isNotEmpty|count|size)\b')
_CONST_INT_RE = re.compile(r'\bconst\s+val\s+([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(\d+)\b')


def _statement_ifs(code: str):
    r"""Indices of the `(` of every `if (` in STATEMENT position.

    A naive `\bif\s*\(` scan also matches the `if (…) 0 else 2` nested INSIDE
    a call argument (`NativeOffloadResult(if (help) 0 else 2, HELP)`), whose
    "guarded region" is then the tail of that same expression — the gate would
    judge the fix as if it were the guard. Only an `if` whose enclosing bracket
    is a block (or nothing) can guard a statement, so the scan tracks the
    bracket stack and skips string literals.
    """
    stack = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c == '"':
            j = i + 1
            while j < n:
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == '"':
                    break
                j += 1
            i = j + 1
            continue
        if c in "([{":
            stack.append(c)
            i += 1
            continue
        if c in ")]}":
            if stack:
                stack.pop()
            i += 1
            continue
        if code.startswith("if", i) and (i == 0 or not (code[i - 1].isalnum() or code[i - 1] in "_.")):
            if not stack or stack[-1] == "{":
                j = i + 2
                while j < n and code[j].isspace():
                    j += 1
                if j < n and code[j] == "(":
                    yield j
            i += 2
            continue
        i += 1


def _help_probe_span(cond: str):
    """(start, end) of the first `hasFlag(...)` call naming the help flag."""
    for m in _HELP_PROBE_RE.finditer(cond):
        if "help" in _HELP_NAME_RE.findall(m.group(1)):
            return (m.start(), m.end())
    return None


def _exit_code_value(expr: str, consts: dict):
    """Int value of a numeric literal or a `const val … = <int>` name, else None."""
    e = expr.strip()
    if _NUM_LIT_RE.match(e):
        return int(e)
    return consts.get(e)


def _guarded_region(text: str, j: int) -> str:
    """The statement / block guarded by an `if (…)` whose `)` ends at [j]."""
    n = len(text)
    k = j
    while k < n and text[k].isspace():
        k += 1
    if k >= n:
        return ""
    if text[k] == "{":
        end = _pcs_match(text, k, "{", "}")
        return text[k:end + 1] if end >= 0 else text[k:]
    # single-statement form (`if (…) return NativeOffloadResult(…)`) — the call
    # may sit on the next line, so walk to the first complete call.
    idx = text.find("NativeOffloadResult(", k, min(n, k + 1200))
    if idx < 0:
        return ""
    close = _pcs_match(text, idx + len("NativeOffloadResult"), "(", ")")
    if close < 0:
        return ""
    eol = text.find("\n", close)
    return text[k:(eol if eol >= 0 else n)]


def _first_call_arg(text: str, paren_at: int):
    """Text of the first top-level argument of the call whose `(` is [paren_at]."""
    depth = 0
    j = paren_at
    while j < len(text):
        c = text[j]
        if c == '"':                      # a literal's brackets and commas do not count
            j += 1
            while j < len(text):
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            j += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return text[paren_at + 1:j]
        elif c == "," and depth == 1:
            return text[paren_at + 1:j]
        j += 1
    return ""


def _split_top_level_else(rest: str):
    """Split `A else B` at its top-level `else`; (A, B) or (None, None)."""
    depth = 0
    i = 0
    while i < len(rest):
        c = rest[i]
        if c == '"':
            j = i + 1
            while j < len(rest):
                if rest[j] == "\\":
                    j += 2
                    continue
                if rest[j] == '"':
                    break
                j += 1
            i = j + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and rest.startswith("else", i) \
                and (i == 0 or rest[i - 1].isspace()) \
                and (i + 4 >= len(rest) or rest[i + 4].isspace()):
            return rest[:i].strip(), rest[i + 4:].strip()
        i += 1
    return None, None


def _squash(text: str, limit: int = 90) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _help_exit_findings(file: str, guard: str, expr: str, consts: dict) -> list:
    """H1/H2 verdict for one help-guarded early exit."""
    findings = []
    e = expr.strip()
    value = _exit_code_value(e, consts)
    if value is not None:
        if value != 0:
            findings.append(("H1", f"{file}: --help exits {value}",
                             _squash(e)))
        return findings
    if not e.startswith("if") or "(" not in e:
        findings.append(("H1", f"{file}: exit code is not decidable (fail-closed)",
                         _squash(e)))
        return findings
    op = e.index("(")
    cl = _pcs_match(e, op, "(", ")")
    cond = e[op + 1:cl]
    if_side, else_side = _split_top_level_else(e[cl + 1:])
    if if_side is None:
        findings.append(("H1", f"{file}: exit-code conditional has no else arm",
                         _squash(e)))
        return findings
    probe = _help_probe_span(cond)
    if probe is not None:
        # `!hasFlag(…)` flips which arm serves the help call.
        negated = cond.strip().startswith("!")
        help_side, bare_side = (else_side, if_side) if negated else (if_side, else_side)
    elif _BARE_SEPARATOR_RE.search(cond) and not _POSITIONAL_TEST_RE.search(cond):
        # A token count separates a bare call (true) from a help call (false).
        help_side, bare_side = else_side, if_side
    else:
        findings.append((
            "H1",
            f"{file}: exit code decided on positional emptiness — `--help` is a flag, "
            f"so positional is empty for it too and the other arm is dead",
            _squash(cond)))
        return findings
    help_value = _exit_code_value(help_side, consts)
    if help_value != 0:
        findings.append(("H1", f"{file}: the help arm of the exit code must be 0",
                         _squash(help_side)))
    outer_probe = _help_probe_span(guard)
    guard_rest = (guard[:outer_probe[0]] + guard[outer_probe[1]:]) if outer_probe else ""
    if "||" in guard_rest:
        if _exit_code_value(bare_side, consts) != 2:
            findings.append(("H2", f"{file}: a bare invocation must still exit 2",
                             _squash(bare_side)))
    return findings


HELP_EXIT_MIN_SITES = 15


def check_help_exit(sources: dict, min_sites: int = HELP_EXIT_MIN_SITES) -> list:
    """[dv27-A5 A-07] H1–H3 above, over every offload source file.

    [min_sites] is a parameter so the selftest can exercise the shape arms on a
    hand-written fixture (which naturally has fewer sites) while the tree run
    keeps the fail-closed floor.
    """
    findings = []
    consts = {}
    for src in sources.values():
        for m in _CONST_INT_RE.finditer(code_only(src)):
            consts[m.group(1)] = int(m.group(2))
    sites = 0
    for name in sorted(sources):
        code = code_only(sources[name])
        for op in _statement_ifs(code):
            cl = _pcs_match(code, op, "(", ")")
            cond = code[op + 1:cl]
            if _help_probe_span(cond) is None:
                continue
            region = _guarded_region(code, cl + 1)
            call = region.find("NativeOffloadResult(")
            if call < 0:
                findings.append((
                    "H3", f"{name}: help guard with no exit-code site (fail-closed)",
                    _squash(cond)))
                continue
            arg = _first_call_arg(region, region.index("(", call))
            sites += 1
            findings += _help_exit_findings(name, cond, arg, consts)
    if sites < min_sites:
        findings.append((
            "H3", f"the help/bare derivation found only {sites} early exits "
                  f"(< {min_sites}, fail-closed)",
            "the handlers were renamed or the guard shape changed — re-derive, "
            "do not delete the rule"))
    return findings


# [dv27-A5 A-07] Every ACCEPTED spelling of the help / bare-invocation exit
# code, one class each — the mutation arms above are diffs against this text.
GREEN_HELP_SHAPES = """
class ConditionalHelp {
    fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(request.argv.drop(1))
        // `--help` is a flag, so positional is empty for it too.
        if (args.hasFlag("h", "help") || args.positional.isEmpty()) {
            return NativeOffloadResult(if (args.hasFlag("h", "help")) 0 else 2, HELP)
        }
        return NativeOffloadResult(0, args.positional[0])
    }
}

class TokenSeparatorHelp {
    fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(request.argv.drop(1))
        if (request.argv.size <= 1 || args.hasFlag("h", "help") && args.positional.isEmpty()) {
            return NativeOffloadResult(if (request.argv.size <= 1) 2 else 0, HELP)
        }
        return NativeOffloadResult(0, "list")
    }
}

class ConstantHelp {
    fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(request.argv.drop(1))
        if (args.hasFlag("h", "help")) return NativeOffloadResult(EXIT_OK, HELP)
        return NativeOffloadResult(EXIT_FAILURE, "nope")
    }
}

class LiteralHelp {
    fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(request.argv.drop(1))
        if (rawArgs.isEmpty() || args.hasFlag("h", "help")) {
            return NativeOffloadResult(0, HELP)
        }
        return NativeOffloadResult(1, "nope")
    }
}

private const val EXIT_OK = 0
private const val EXIT_FAILURE = 1
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
        # [dv19-A1 A-02] `--wait` teeth: the declaration must carry it (and must
        # not swallow the value option), and the flag must be documented.
        ("--wait dropped from the boolean declaration",
         GREEN_SESSIONS.replace('setOf("full", "async", "wait", "help", "h")',
                                'setOf("full", "async", "help", "h")'),
         {"J21"}),
        ("the value option --wait-timeout declared boolean",
         GREEN_SESSIONS.replace('setOf("full", "async", "wait", "help", "h")',
                                'setOf("full", "async", "wait", "wait-timeout", "help", "h")'),
         {"J21"}),
        ("the --wait help line is dropped",
         GREEN_SESSIONS.replace(
             "  --wait                (send only) Block until the reply completes "
             "(default; --async wins)\n", ""),
         {"J22"}),
    ]:
        if sessions_src == GREEN_SESSIONS:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_sessions(sessions_src)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # [deep-audit-1044 A-04] S1–S7 teeth — the send status → exit-code contract.
    # Expectations are written by hand from the S rule table, never derived from
    # a run. The pre-fix shape is the first arm: `cmdSend` catches exceptions and
    # then emits, so the status is never consulted (S2) and nothing returns early
    # with a nonzero code (S4); S1/S3 stay green because the payload builder does
    # read the status and the catch branch does call `errorEnvelope`.
    for label, send_src, want in [
        ("the status read is dropped",
         GREEN_SESSIONS_SEND.replace('val status = result.optString("status")',
                                    'val status = ""'),
         {"S1"}),
        # Consulting the set and placing it BEFORE the payload are one decision:
        # an `if (false)` stub keeps the payload unconditional, so both rules fire.
        ("the success set is never consulted",
         GREEN_SESSIONS_SEND.replace("if (!sendSucceeded(status)) {", "if (false) {"),
         {"S2", "S5"}),
        ("the refusal skips the shared error funnel",
         GREEN_SESSIONS_SEND.replace('val err = errorEnvelope(', 'val err = errorShape('),
         {"S3"}),
        ("the refusal still exits 0",
         GREEN_SESSIONS_SEND.replace("sendExitFor(status),", "0,"),
         {"S4"}),
        ("emit runs before the refusal branch",
         GREEN_SESSIONS_SEND.replace("        if (!sendSucceeded(status)) {",
                                     '        emit("send", data, args)\n'
                                     "        if (!sendSucceeded(status)) {"),
         {"S5"}),
        ("an unknown status counts as a delivery",
         GREEN_SESSIONS_SEND.replace(
             'status == "Completed" || status == "Running"',
             'status == "Completed" || status == "Running" || status == ""'),
         {"S6"}),
        ("Completed is no longer a delivery",
         GREEN_SESSIONS_SEND.replace(
             'status == "Completed" || status == "Running"', 'status == "Running"'),
         {"S6"}),
        ("the refusal hard-codes its exit code",
         GREEN_SESSIONS_SEND.replace("sendExitFor(status),", "1,"),
         {"S4"}),
        ("the exit helper is deleted",
         GREEN_SESSIONS_SEND.replace(
             "        internal fun sendExitFor(status: String): Int =\n"
             "            if (sendSucceeded(status)) 0 else EXIT_FAILURE\n", ""),
         {"S7"}),
        ("the reason rides along under response_text",
         GREEN_SESSIONS_SEND.replace(
             '            val detail = JSONObject()\n'
             '                .put("session_id", data.optString("session_id"))\n'
             '                .put("status", status)\n',
             '            val detail = JSONObject()\n'
             '                .put("session_id", data.optString("session_id"))\n'
             '                .put("response_text", data.optString("response_text"))\n'
             '                .put("status", status)\n'),
         {"S8"}),
        ("the failure exit code becomes 0",
         GREEN_SESSIONS_SEND.replace("private const val EXIT_FAILURE = 1",
                                     "private const val EXIT_FAILURE = 0"),
         {"S7"}),
    ]:
        if send_src == GREEN_SESSIONS_SEND:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_sessions_send_status(send_src)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1

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
        # [dv19-A1 A-01] the subcommand probe must read the REWRITTEN list: on the
        # raw argv `-n 10` is still unbound, so "10" took positional[0] and
        # `-n 10 list` refused with `unknown subcommand '10'` (exit 2).
        ("contacts: the subcommand probe reads the raw argv again",
         GREEN_CONTACTS.replace("OffloadArgs(rewritten, booleanFlags",
                                "OffloadArgs(argv, booleanFlags"),
         GREEN_NOTIFICATION_HANDLE, GREEN_FASTIO, {"V1e"}),
        ("contacts: the --max rewrite moves behind the subcommand probe",
         GREEN_CONTACTS.replace(
             'val rewritten = ArrayList<String>(argv.size)\n            rewritten.add("--max")',
             'val rewritten = ArrayList<String>(argv.size)').replace(
             '            if (sub != "list" && sub != "search") return argv\n            return rewritten',
             '            if (sub != "list" && sub != "search") return argv\n'
             '            rewritten.add("--max")\n            return rewritten'),
         GREEN_NOTIFICATION_HANDLE, GREEN_FASTIO, {"V1f"}),
        ("contacts: the probed list is built but not the one returned",
         GREEN_CONTACTS.replace("            return rewritten\n", "            return argv\n"),
         GREEN_NOTIFICATION_HANDLE, GREEN_FASTIO, {"V1g"}),
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
    # [dv22-A6 A-06] the clipboard clear arms. Expectations hand-written from
    # the L-table: one arm per way the fix can be undone, plus the baseline
    # (pre-fix) shape and the fail-closed arm for a lost owner function.
    CLEAR_WHEN_BLOCK = (
        "        when (clearAction(Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)) {\n"
        "            ClipboardClearAction.PlatformClear -> cm.clearPrimaryClip()\n"
        "            ClipboardClearAction.EmptyStringFallback -> cm.setPrimaryClip(ClipData.newPlainText(\"\", \"\"))\n"
        "        }\n"
    )
    DO_CLEAR_GUARD = (
        "    private fun doClear(cm: ClipboardManager, args: OffloadArgs): NativeOffloadResult {\n"
        "        if (!isAppForeground() && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {\n"
    )
    red_cases_clipboard = [
        ("clipboard: clear reverts to the empty-string write (baseline defect)",
         GREEN_CLIPBOARD_CLEAR.replace(CLEAR_WHEN_BLOCK,
                                       '        cm.setPrimaryClip(ClipData.newPlainText("", ""))\n'),
         {"L2"}),
        ("clipboard: doClear's guard drops the `&& SDK_INT >= Q` half (baseline defect)",
         GREEN_CLIPBOARD_CLEAR.replace(DO_CLEAR_GUARD,
                                       "    private fun doClear(cm: ClipboardManager, args: OffloadArgs): NativeOffloadResult {\n"
                                       "        if (!isAppForeground()) {\n"),
         {"L3"}),
        ("clipboard: always clearPrimaryClip — the pre-Q fallback is deleted",
         GREEN_CLIPBOARD_CLEAR.replace(CLEAR_WHEN_BLOCK, "        cm.clearPrimaryClip()\n"),
         {"L4"}),
        ("clipboard: both writes run unconditionally (clear-then-set puts the item back)",
         GREEN_CLIPBOARD_CLEAR.replace(
             CLEAR_WHEN_BLOCK,
             '        cm.clearPrimaryClip()\n'
             '        cm.setPrimaryClip(ClipData.newPlainText("", ""))\n'),
         {"L4"}),
        ("clipboard: the whole pre-fix shape",
         GREEN_CLIPBOARD_CLEAR
         .replace(CLEAR_WHEN_BLOCK, '        cm.setPrimaryClip(ClipData.newPlainText("", ""))\n')
         .replace(DO_CLEAR_GUARD,
                  "    private fun doClear(cm: ClipboardManager, args: OffloadArgs): NativeOffloadResult {\n"
                  "        if (!isAppForeground()) {\n"),
         {"L2", "L3"}),
        ("fail-closed: doClear lost its owner body",
         GREEN_CLIPBOARD_CLEAR.replace("private fun doClear(", "private fun unusedClear("),
         {"L1"}),
    ]
    for label, clipboard_src, want in red_cases_clipboard:
        if clipboard_src == GREEN_CLIPBOARD_CLEAR:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_clipboard_clear(clipboard_src)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # [dv19-A1 A-03] the grep budget teeth. Expectations hand-written from
    # check_fastio_search's S-table: one arm per moment a walk can spend the
    # caller's single cross-root budget, plus a fail-closed arm proving the rules
    # are not vacuous on a file that merely lost the walk.
    for label, search_src, want in [
        ("grep entry: a spent budget walks anyway",
         GREEN_FASTIO_SEARCH.replace(
             "    if (limit <= 0) {\n        out.matchesCapped = true\n        return\n    }\n", ""),
         {"S1"}),
        ("grep entry: the short circuit forgets to call it capped",
         GREEN_FASTIO_SEARCH.replace(
             "        out.matchesCapped = true\n        return\n", "        return\n"),
         {"S1"}),
        ("grep walk: a directory is entered after the budget is spent",
         GREEN_FASTIO_SEARCH.replace(
             "                    if (out.matchesCapped) return FileVisitResult.TERMINATE\n"
             "                    if (charge(out, maxEntries)) return FileVisitResult.TERMINATE",
             "                    if (charge(out, maxEntries)) return FileVisitResult.TERMINATE"),
         {"S2"}),
        ("grep walk: the file that filled the budget keeps walking",
         GREEN_FASTIO_SEARCH.replace(
             "                    return if (out.matchesCapped) {\n"
             "                        FileVisitResult.TERMINATE\n"
             "                    } else {\n"
             "                        FileVisitResult.CONTINUE\n"
             "                    }",
             "                    return FileVisitResult.CONTINUE"),
         {"S3a", "S3b"}),
        ("fail-closed: the grep engine lost its walk",
         GREEN_FASTIO_SEARCH.replace("Files.walkFileTree(", "unusedWalk("),
         {"S1"}),
    ]:
        if search_src == GREEN_FASTIO_SEARCH:
            print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
            failures += 1
            continue
        got = {rid for rid, _, _ in check_fastio_search(search_src)}
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    # the green arms must be fully clean, or every rule is vacuous
    clean = check_text(GREEN_CONTACTS, GREEN_CONFIG, GREEN_BROWSERUSE, GREEN_FASTIO)
    clean_flags = check_flag_set(GREEN_OUTPUT, GREEN_SPEAK)
    clean_debug = check_debug(GREEN_DEBUG)
    clean_sessions = check_sessions(GREEN_SESSIONS)
    clean_send_status = check_sessions_send_status(GREEN_SESSIONS_SEND)
    clean_args = check_args(GREEN_ARGS)
    clean_album = check_photos_album(GREEN_ALBUM_PHOTOS)
    clean_clipboard = check_clipboard_clear(GREEN_CLIPBOARD_CLEAR)
    clean_bindings = check_offload_cli_bindings(
        GREEN_CONTACTS, GREEN_NOTIFICATION_HANDLE, GREEN_FASTIO)
    clean_search = check_fastio_search(GREEN_FASTIO_SEARCH)
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
    # [dv25-A4 A-07] payload provenance across files (Z1-Z4). Expected sets are
    # written by hand from the rule table, never derived from the run.
    _prov_sources = {
        "PlayerOffloadHandler.kt": GREEN_PLAYER_HANDLER,
        "MediaPlayerManager.kt": GREEN_PLAYER_MANAGER,
        "PlayerOutcome.kt": GREEN_PLAYER_OUTCOME,
        "WeatherOffloadHandler.kt": GREEN_WEATHER_HANDLER,
        "WeatherManager.kt": GREEN_WEATHER_MANAGER,
    }
    _prov_prose = dict(_prov_sources)
    _prov_prose["PlayerOffloadHandler.kt"] = PROSE_PLAYER_HANDLER
    _prov_prose["MediaPlayerManager.kt"] = PROSE_PLAYER_MANAGER
    _prov_exempt = {"WeatherOffloadHandler.kt": "P2 registered, out of this branch"}
    clean_provenance = check_payload_delegates(_prov_sources, [PLAYER], _prov_exempt)
    if clean_provenance:
        print(f"SELFTEST-FAIL: provenance green arm is not clean: {describe(clean_provenance)}")
        failures += 1
    for label, _src, _payload, _exempt, _want in [
        ("provenance: a payload file keeps the manager's sentence",
         _prov_prose, [PLAYER], _prov_exempt, {"Z2"}),
        ("provenance: a delegating handler is missing from the whitelist",
         _prov_sources, [], _prov_exempt, {"Z1"}),
        ("provenance: the exemption list lost an entry",
         _prov_sources, [PLAYER], {}, {"Z1"}),
        ("provenance: an exemption outlived its defect",
         _prov_sources, [PLAYER],
         dict(_prov_exempt, **{"PhotosOffloadHandler.kt": "stale"}), {"Z3"}),
        ("provenance: an exemption carries no reason",
         _prov_sources, [PLAYER], {"WeatherOffloadHandler.kt": "   "}, {"Z3"}),
        ("provenance: the derivation lost the tree",
         {"Other.kt": "class Other { fun f(): Int = 1 }"}, [PLAYER], {}, {"Z4"}),
    ]:
        _got = {rid for rid, _, _ in check_payload_delegates(_src, _payload, _exempt)}
        if _got != _want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(_got)}, want {sorted(_want)}")
            failures += 1

    # [dv27-A5 A-07] help / bare-invocation exit-code arms. Expected rule-id
    # sets are written by hand from the H1/H2/H3 table, never derived from a
    # run; the fixture carries every accepted spelling at once (conditional on
    # the flag, literal 0, a `const val` name, and the token-count separator).
    def _help_ids(src, min_sites=1):
        return {rid for rid, _, _ in check_help_exit({"H.kt": src}, min_sites=min_sites)}

    for label, mutate, want in [
        ("help exit: the accepted spellings are clean", None, set()),
        ("help exit: decided on positional emptiness again (the defect)",
         lambda x: x.replace('if (args.hasFlag("h", "help")) 0 else 2',
                             'if (args.positional.isEmpty()) 2 else 0'), {"H1"}),
        ("help exit: the help arm answers a non-zero literal",
         lambda x: x.replace("NativeOffloadResult(0, HELP)",
                             "NativeOffloadResult(2, HELP)"), {"H1"}),
        ("help exit: the bare arm degraded to 0",
         lambda x: x.replace("0 else 2", "0 else 0"), {"H2"}),
        ("help exit: the bare arm degraded to 1",
         lambda x: x.replace("0 else 2", "0 else 1"), {"H2"}),
        ("help exit: the exit constant behind the help arm stops being 0",
         lambda x: x.replace("const val EXIT_OK = 0", "const val EXIT_OK = 2"), {"H1"}),
        ("help exit: an exit code nobody can decide (fail-closed)",
         lambda x: x.replace('if (args.hasFlag("h", "help")) 0 else 2',
                             "helpExitFor(args)"), {"H1"}),
        ("help exit: a help guard with no exit site",
         lambda x: x.replace('return NativeOffloadResult(0, HELP)',
                             "println(HELP)"), {"H3"}),
        ("help exit: the derivation finds fewer sites than the floor",
         None, {"H3"}),
    ]:
        if label.endswith("floor"):
            got = _help_ids(GREEN_HELP_SHAPES, min_sites=HELP_EXIT_MIN_SITES)
        else:
            mutated = GREEN_HELP_SHAPES if mutate is None else mutate(GREEN_HELP_SHAPES)
            if mutate is not None and mutated == GREEN_HELP_SHAPES:
                print(f"SELFTEST-FAIL: {label}: mutation did not apply to any arm")
                failures += 1
                continue
            got = _help_ids(mutated)
        if got != want:
            print(f"SELFTEST-FAIL: {label}: got {sorted(got)}, want {sorted(want)}")
            failures += 1
    if clean_help := check_help_exit({"H.kt": GREEN_HELP_SHAPES}, min_sites=1):
        print(f"SELFTEST-FAIL: help-exit green fixture is not clean: "
              f"{describe(clean_help)}")
        failures += 1

    if clean or clean_flags or clean_debug or clean_sessions or clean_bindings \
            or clean_payload or clean_album or clean_search or clean_clipboard \
            or clean_send_status or clean_provenance or clean_help:
        green_findings = (clean + clean_flags + clean_debug + clean_sessions
                          + clean_bindings + clean_payload + clean_album + clean_search
                          + clean_clipboard + clean_send_status)
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
    search_path = root / FASTIO_SEARCH
    if not contacts_path.is_file() or not config_path.is_file() \
            or not browseruse_path.is_file() or not fastio_path.is_file() \
            or not output_path.is_file() or not speak_path.is_file() \
            or not debug_path.is_file() or not sessions_path.is_file() \
            or not notification_path.is_file() or not args_path.is_file() \
            or not search_path.is_file():
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
    findings += check_sessions_send_status(sessions_path.read_text(encoding="utf-8"))
    findings += check_args(args_path.read_text(encoding="utf-8"))
    findings += check_photos_album((root / PHOTOS).read_text(encoding="utf-8"))
    findings += check_offload_cli_bindings(contacts_path.read_text(encoding="utf-8"),
                                          notification_path.read_text(encoding="utf-8"),
                                          fastio_path.read_text(encoding="utf-8"))
    findings += check_offload_json(*payload_files)
    findings += check_clipboard_clear((root / CLIPBOARD).read_text(encoding="utf-8"))
    findings += check_fastio_search(search_path.read_text(encoding="utf-8"))
    # [dv25-A4 A-07] payload provenance ACROSS files (see check_payload_delegates)
    sources = {}
    for _dir in OFFLOAD_SOURCE_DIRS:
        _p = root / _dir
        if not _p.is_dir():
            print(f"GATE-FAIL: offload source dir not found: {_dir} under {root}")
            return 1
        for _f in sorted(_p.glob("*.kt")):
            sources[_f.name] = _f.read_text(encoding="utf-8")
    findings += check_payload_delegates(sources, PAYLOAD_FILES)
    # [dv27-A5 A-07] the help / bare-invocation exit code, derived over the family
    findings += check_help_exit(sources)
    if findings:
        print("offload_cli_args_check: FAIL")
        print(describe(findings))
        return 1
    print("offload_cli_args_check: PASS (contacts dispatch parsed, config --file cwd-aware, "
          "browseruse paths joined, fastio tar verdict shared, "
          "OUTPUT_FLAGS covers help/h, speak declares stop/status, "
          "sessions date bounds funneled + refuse invalid, "
          "debug pass-through argv verbatim + global-only output shaping, "
          "9/9 payload files hand formatBody JSON (N1 whole-file, N2 payload helpers), "
          "cross-file payload delegates registered (Z1 derived members, Z2 no String "
          "delegate in a payload file, Z3 exemptions still needed, Z4 derivation alive), "
          "contacts search -n bound to --max through the one resolver, notification --after "
          "refuses an unreadable value, fastio subcommand scan knows its boolean flags "
          "and consumes -C, photos --album decision classified (non-numeric --id refuses), "
          "contacts subcommand probe reads the rewritten alias list, sessions declares "
          "async/wait/help/h and documents --wait, grep budget stops a spent root before "
          "any file is opened, clipboard clear reaches clearPrimaryClip behind the shared "
          "four-guard shape and keeps the pre-Q empty-string fallback, "
          "every help-guarded early exit answers 0 for --help while a bare invocation "
          "still answers 2 (H1/H2 derived over the family, H3 fail-closed on the "
          "derivation)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

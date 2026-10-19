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
            val args = OffloadArgs(argv, booleanFlags = setOf("help", "h"))
            return ContactsCommand(
                sub = args.positional.getOrNull(0),
                id = args.positional.getOrNull(1)?.toLongOrNull(),
                args = args,
            )
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
            var i = 0
            while (i < rawArgv.size) {
                if (!rawArgv[i].startsWith("-")) return rawArgv[i]
                i += 1
            }
            return null
        }
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
        return startMs to endMs
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

internal fun sessionsDateBound(flag: String, raw: String?, parse: (String) -> Long?): Long? {
    if (raw.isNullOrBlank()) return null
    return parse(raw) ?: throw InvalidDateArg(flag, raw)
}
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
    # the green arms must be fully clean, or every rule is vacuous
    clean = check_text(GREEN_CONTACTS, GREEN_CONFIG, GREEN_BROWSERUSE, GREEN_FASTIO)
    clean_flags = check_flag_set(GREEN_OUTPUT, GREEN_SPEAK)
    clean_debug = check_debug(GREEN_DEBUG)
    clean_sessions = check_sessions(GREEN_SESSIONS)
    if clean or clean_flags or clean_debug or clean_sessions:
        print(f"SELFTEST-FAIL: green fixtures must produce no findings, "
              f"got {describe(clean + clean_flags + clean_debug + clean_sessions)}")
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
    if not contacts_path.is_file() or not config_path.is_file() \
            or not browseruse_path.is_file() or not fastio_path.is_file() \
            or not output_path.is_file() or not speak_path.is_file() \
            or not debug_path.is_file() or not sessions_path.is_file():
        print(f"GATE-FAIL: handler source not found under {root}")
        return 1
    findings = check_text(contacts_path.read_text(encoding="utf-8"),
                          config_path.read_text(encoding="utf-8"),
                          browseruse_path.read_text(encoding="utf-8"),
                          fastio_path.read_text(encoding="utf-8"))
    findings += check_flag_set(output_path.read_text(encoding="utf-8"),
                               speak_path.read_text(encoding="utf-8"))
    findings += check_debug(debug_path.read_text(encoding="utf-8"))
    findings += check_sessions(sessions_path.read_text(encoding="utf-8"))
    if findings:
        print("offload_cli_args_check: FAIL")
        print(describe(findings))
        return 1
    print("offload_cli_args_check: PASS (contacts dispatch parsed, config --file cwd-aware, "
          "browseruse paths joined, fastio tar verdict shared, "
          "OUTPUT_FLAGS covers help/h, speak declares stop/status, "
          "sessions date bounds funneled + refuse invalid, "
          "debug pass-through argv verbatim + global-only output shaping)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

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
    failures = 0
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
    # the green arms must be fully clean, or every rule is vacuous
    clean = check_text(GREEN_CONTACTS, GREEN_CONFIG, GREEN_BROWSERUSE, GREEN_FASTIO)
    if clean:
        print(f"SELFTEST-FAIL: green fixtures must produce no findings, got {describe(clean)}")
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
    root = Path(argv[0])
    if "--selftest" in argv[1:]:
        return selftest()
    contacts_path = root / CONTACTS
    config_path = root / CONFIG
    browseruse_path = root / BROWSERUSE
    fastio_path = root / FASTIO
    if not contacts_path.is_file() or not config_path.is_file() \
            or not browseruse_path.is_file() or not fastio_path.is_file():
        print(f"GATE-FAIL: handler source not found under {root}")
        return 1
    findings = check_text(contacts_path.read_text(encoding="utf-8"),
                          config_path.read_text(encoding="utf-8"),
                          browseruse_path.read_text(encoding="utf-8"),
                          fastio_path.read_text(encoding="utf-8"))
    if findings:
        print("offload_cli_args_check: FAIL")
        print(describe(findings))
        return 1
    print("offload_cli_args_check: PASS (contacts dispatch parsed, config --file cwd-aware, "
          "browseruse paths joined, fastio tar verdict shared)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""
android-shizuku-cli argument-handling guard (deep-audit-1017 ISSUE-08).

Three defects live in `ShizukuOffloadHandler.kt`, and only some of them are
visible to a JVM test (`ShizukuCliArgsTest`):

  * what the helpers *produce* (encodeInputText / buildPackageClearCommand /
    clearScope) — behavioural, pinned by that test;
  * how the handler *calls* them and where it reads its arguments from — that
    wiring lives in a class that imports `android.content.Context`, so no unit
    test can reach it. It is pinned here instead, because a helper that is
    correct but unplugged is exactly the "wrapper is alive, path does not run
    through it" failure (deep-audit-1016 FIX-D G3b).

Criteria (all structural; the file must be the real handler):
  1. the handler builds its parser with the declared switch set
     (`OffloadArgs(rest, booleanFlags = NO_VALUE_FLAGS)`);
  2. `NO_VALUE_FLAGS` exists, covers `block`/`unblock`, and excludes
     `importance` (a value flag — declaring it would orphan its value);
  3. `encodeInputText` emits the platform's `%s` escape, not a bare `%`;
  4. `buildPackageClearCommand` asks `pm clear` for the package (per-package),
     and the handler contains no global `trim-caches` stand-in;
  5. `handleNotification` hands `notificationChannel` the **parsed** positional
     list (`args.positional.drop(1)`), not the raw argv tail;
  6. `notificationChannel` reads its package/channel from those positionals
     (`pos.getOrNull(1)`) and never from raw `rest`;
  7. `packageClear` reports the scope it actually cleared (`clearScope(...)`);
  8. every long option read through `hasFlag` is declared value-less;
  9. the group comes from the parsed positional (`splitShizukuArgv`), not
     `argv[0]`;
 10. group sub-commands read parsed positionals, not the raw `rest` tail;
 11. `exec` passthrough still joins the raw tail verbatim;
 12. the one two-meaning option (`--data`) is opted out per group;
 13. [deep-audit-1029 ISSUE-07] `exec`'s payload is a shell command, not an
     option list: the parser is fed only the CLI's own tokens
     (`split.execOptionTokens`) and the one option `exec` owns
     (`--timeout-ms N`) is lifted out of the payload by
     `stripExecTimeoutTokens`. Either half missing restores a shipped defect —
     the payload reaching the output shaper (`exec grep -q foo /p` did, via
     `-q` → `--quiet`) makes it drop the whole `data` object on a non-zero exit,
     and the un-lifted option is executed by the shell.
     [wide-1024] …and the lift must ACTUALLY happen: the helper's body is
     judged (loop + option literal + two accumulators + no early return), not
     its existence. `if (true) return tokens to tokens` used to stay green.

 14. [wide-1024] the netpolicy direction is judged inside the `arrayOf(...)`
     that is an ARGUMENT of the call. The old anchor took the first textual
     mention of the list name, so a dead `val x = arrayOf(…)` one line above
     the real `runProcess(...)` absorbed it.

 15. [wide-1024] "no fallback to root" is judged as a SPELLING CLASS: every
     `?:` whose fallback evaluates to "0" (`?: "0"`, `?: (0).toString()`,
     `?: 0.toString()`, `?: Integer.toString(0)`, `?: "$0"`). Pinning the one
     literal let a re-spelled root fallback through.

A renamed function is a HARD FAIL, never a skip (the gate must not go quiet
because the code moved).

Usage: python3 scripts/scan/shizuku_args_check.py [repo_root] [--selftest]
Exit:  0 = all criteria hold, 1 = at least one violation
"""
import os
import re
import sys
import tempfile

REL = os.path.join(
    "src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app",
    "sandbox", "offload", "ShizukuOffloadHandler.kt",
)


def strip_comments(text):
    """Drop comment text but keep every newline, so line numbers still point at
    the real file (a stripped block comment used to shift every later anchor)."""
    out = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "/" and i + 1 < len(text) and text[i + 1] == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < len(text) and text[i + 1] == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def function_body(text, name):
    """Body of `fun <name>(...)`.

    Handles both block bodies (`{ ... }`, brace-balanced, string literals
    skipped) and expression bodies (`= expr`, delimited by the blank line that
    follows). Without the expression-body branch the scan would walk into the
    *next* declaration's braces and silently check the wrong function
    (deep-audit-1016 FIX-D G3b).
    """
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
    k = i + 1
    while k < len(text) and text[k] in " \t\r\n":
        k += 1
    if k < len(text) and text[k] == ":":  # return type
        while k < len(text) and text[k] not in "={":
            k += 1
    if k >= len(text):
        return None
    if text[k] == "=":  # expression body — ends at the following blank line
        end = text.find("\n\n", k)
        return text[k + 1 : end if end >= 0 else len(text)].rstrip()
    j = text.find("{", k)
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
    """1-based line of the first occurrence of `needle`, 0 when absent."""
    i = src.find(needle)
    return src.count("\n", 0, i) + 1 if i >= 0 else 0


def _array_span(src, i):
    """(start, end) of the `arrayOf(...)` enclosing index [i], or None."""
    j = src.rfind("arrayOf(", 0, i)
    if j < 0:
        return None
    depth, k = 0, j
    while k < len(src):
        if src[k] == "(":
            depth += 1
        elif src[k] == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    if k >= len(src) or not (j < i < k):
        return None
    return j, k + 1


def _call_literals(src, list_name):
    """Every `arrayOf(...)` that names [list_name] AND is handed to a call.

    Decision-point binding, by CALL FRAME rather than by first textual hit
    ([wide-1024 Criterion 14]). The old anchor took `src.find('"<list>"')` —
    the first mention of the list name in the file — and walked back to the
    nearest `arrayOf(`. A dead `val _unused = arrayOf(… "list" …)` placed one
    line above the real call therefore absorbed the anchor, and the direction
    actually executed by `runProcess(` was never read (a shipped inversion
    stayed green). Here the subject is the array that is an ARGUMENT of a call
    (`run(` / `ShizukuManager.runProcess(`); an array bound to a `val` is not
    executed by this handler and is not this criterion's subject.

    Returns [(text, is_call_arg)] so the caller can require at least one live
    site (fail-closed) and judge every live one.
    """
    out = []
    seen = set()
    for m in re.finditer(r'"%s"' % re.escape(list_name), src):
        span = _array_span(src, m.start())
        if span is None:
            continue
        if span in seen:
            continue
        seen.add(span)
        j, k = span
        prefix = src[max(0, j - 60):j]
        is_arg = re.search(r"[A-Za-z_][\w.]*\s*\(\s*$", prefix) is not None
        out.append((src[j:k], is_arg))
    return out


def _option_spellings(src, option):
    """Every spelling that identifies [option]: the literal, plus each constant
    bound to it in this file (`private const val EXEC_TIMEOUT_OPT =
    "--timeout-ms"`). A named constant is the same decision written once, not a
    second spelling — the real handler uses it, so a literal-only anchor would
    red the clean tree."""
    out = {'"%s"' % option}
    for m in re.finditer(
            r"\b(?:const\s+)?val\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*String\s*)?=\s*\"%s\""
            % re.escape(option), src):
        out.add(m.group(1))
    return out


def _strips_option_out(body, spellings):
    """True when [body] really lifts [option] out of its input list.

    [wide-1024 Criterion 13] "the helper exists and the splitter calls it" is
    not "the helper strips". A first line `if (true) return tokens to tokens`
    (or any early `return <in> to <in>`) satisfies both anchors while the
    payload keeps `--timeout-ms`, and the shell then executes the flag as the
    command name. So the body must enumerate its input, compare against the
    option literal, fill two distinct accumulators, and only return after the
    loop — the returned pair must be those two accumulators.
    """
    if body is None:
        return False, "helper MISSING"
    if not any(sp in body for sp in spellings):
        return False, "the body never names the option (no %s)" % " / ".join(sorted(spellings))
    loop = re.search(r"\b(?:while|for)\s*\(", body)
    if loop is None:
        return False, "no loop over the tokens"
    ret = body.find("return")
    if ret >= 0 and ret < loop.start():
        return False, "returns before the loop (an early exit leaves the option in the payload)"
    adds = set(re.findall(r"\b([A-Za-z_]\w*)\.add(?:All)?\(", body))
    tail = re.search(r"return\s+([A-Za-z_]\w*)\s+to\s+([A-Za-z_]\w*)", body)
    if tail is None:
        return False, "no `return <payload> to <options>`"
    if tail.group(1) not in adds or tail.group(2) not in adds:
        return False, "the returned pair is not the two accumulators the loop fills"
    if tail.group(1) == tail.group(2):
        return False, "the pair returns the same list twice"
    return True, "ok"


# ── [wide-1024 Criterion 15] spellings that all evaluate to the string "0" ──
# The shipped defect was `?: "0"` (an unresolvable uid becoming uid 0 = root).
# Pinning that one literal missed every equivalent: `?: (0).toString()`,
# `?: 0.toString()`, `?: Integer.toString(0)`, `?: "$0"`. The criterion now
# normalises the elvis fallback expression and compares it against the set of
# zero-yielding spellings, so the shape is judged, not the typing.
_ZERO_SPELLINGS = {
    '"0"', '0.tostring()', '(0).tostring()', '0l.tostring()', '(0l).tostring()',
    'integer.tostring(0)', 'long.tostring(0)', 'string.valueof(0)',
    '"0".tostring()', '"$0"', '"${0}"', '0.toint().tostring()',
}


def _expr_at(text, i):
    """The expression starting at/after [i], up to a depth-0 delimiter."""
    depth = 0
    out = []
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and c in "\n,;&|=+":
            break
        out.append(c)
        i += 1
    return "".join(out)


def _zero_fallback(text):
    """(True, spelling) when some `?:` fallback evaluates to the string "0"."""
    for m in re.finditer(r"\?:", text):
        expr = _expr_at(text, m.end())
        norm = re.sub(r"\s+", "", expr).lower()
        if norm in _ZERO_SPELLINGS:
            return True, expr.strip()
    return False, ""


def analyse(src):
    """-> list of (criterion, ok, line, note).

    `line` is the 1-based anchor line in the handler (0 = the anchor is gone,
    which is reported as a FAIL rather than a skip; COMMON §4 wants violations
    cited as file:line).
    """
    results = []
    flags = re.search(r"val\s+NO_VALUE_FLAGS\s*=\s*setOf\(([^)]*)\)", src)
    names = set(re.findall(r'"([^"]+)"', flags.group(1))) if flags else set()

    # [dv15-A1 A-02] `-a/-d/-p/-c` take values, so the parser must be told: the
    # criterion pins that DECISION at BOTH sites (the declared switches and the
    # declared value-taking shorts travel with the call) rather than one
    # spellable line, so a reformat that keeps the behaviour stays green.
    # [dv18-A A-01a] re-anchored to EVERY OffloadArgs( construction site: the
    # two handle sites carry the declared set AND the pre-scan probe must pass
    # the SAME set (a pre-scan that drops it mis-splits `-p com.x package info`
    # into a bogus group). Count + decision-point, not one spellable line.
    switches = "booleanFlags = noValueFlagsFor(group)"
    shorts = "valueShortOptions = SHIZUKU_VALUE_SHORT_OPTIONS"
    naked = [
        src[:m.start()].count("\n") + 1
        for m in re.finditer(r"OffloadArgs\(", src)
        if "valueShortOptions =" not in src[m.start():m.start() + 200]
    ]
    pre_scan = re.search(
        r"firstPositionalIndex\(\s*argv[^)]*SHIZUKU_VALUE_SHORT_OPTIONS", src)
    results.append((
        "1 parser receives declared switches (per group); every OffloadArgs threads value shorts",
        src.count(switches) == 2 and src.count(shorts) == 2
        and not naked and pre_scan is not None
        and "valueShortOptions = valueShortOptions" in src,
        line_of(src, "split.withoutGroup"),
        "expected 2x `%s` and 2x `%s`; naked constructions=%s; pre-scan passes the set=%s"
        % (switches, shorts, naked or "none", "yes" if pre_scan else "NO"),
    ))
    results.append((
        "2 NO_VALUE_FLAGS covers switches, excludes value flags",
        flags is not None and {"block", "unblock"} <= names and "importance" not in names,
        line_of(src, "val NO_VALUE_FLAGS"),
        "declared=%s" % (sorted(names) if flags else "MISSING"),
    ))
    body = function_body(src, "encodeInputText")
    results.append((
        "3 input text uses the platform %s escape",
        body is not None and '"%s"' in body and "replace(' ', '%')" not in body,
        line_of(src, "internal fun encodeInputText("),
        "helper " + ("missing" if body is None else "present, must emit \"%s\" and not a bare '%'"),
    ))
    cmd = function_body(src, "buildPackageClearCommand")
    results.append((
        "4 package clear is per-package (no global trim-caches stand-in)",
        cmd is not None and '--cache-only' in cmd and '"pm", "clear"' in cmd and "trim-caches" not in src,
        line_of(src, 'val cmd = mutableListOf("pm", "clear")'), 
        "helper " + ("missing" if cmd is None else "present") + ", trim-caches hits=%d" % src.count("trim-caches"),
    ))
    notify = function_body(src, "handleNotification") or ""
    results.append((
        "5 channel sub-command wired to parsed positionals",
        "notificationChannel(args.positional.drop(1), args)" in notify,
        line_of(src, "notificationChannel("),
        "handleNotification body" if notify else "handleNotification MISSING",
    ))
    chan = function_body(src, "notificationChannel")
    results.append((
        "6 notificationChannel reads positionals, not raw argv",
        chan is not None and "pos.getOrNull(1)" in chan and "rest.getOrNull(" not in chan,
        line_of(src, "private fun notificationChannel("),
        "helper " + ("missing" if chan is None else "present"),
    ))
    clear = function_body(src, "packageClear")
    results.append((
        "7 package clear payload reports the scope it actually cleared",
        clear is not None and "clearScope(cacheOnly)" in clear
        and "buildPackageClearCommand(" in clear,
        line_of(src, "private fun packageClear("),
        "helper " + ("missing" if clear is None else "present"),
    ))

    # ── deep-audit-1020 ISSUE-02: declaration coverage + argv shaping ──────
    # 8. every long option read through hasFlag must be declared value-less
    used_names = set()
    for m in re.finditer(r'hasFlag\(((?:"[^"]*"\s*,?\s*)+)\)', src):
        for nm in re.findall(r'"([^"]+)"', m.group(1)):
            if len(nm) > 1:
                used_names.add(nm)
    undeclared = used_names - names - {"help"}
    results.append((
        "8 every hasFlag long name is declared value-less",
        flags is not None and not undeclared,
        line_of(src, "val NO_VALUE_FLAGS"),
        ("undeclared=%s (a switch that eats the next token)" % sorted(undeclared))
        if undeclared else "covered=%d names" % len(used_names),
    ))

    # 9. the group comes from the parsed positional, never from argv[0] — both
    #    at the call site AND inside the splitter's implementation (a mutation
    #    that guts firstPositionalIndex must not slip past the call-site pin)
    handle_body = function_body(src, "handle") or ""
    results.append((
        "9 group taken by position (splitShizukuArgv), not argv[0]",
        "splitShizukuArgv(argv)" in handle_body and "split.group" in handle_body
        and "val rest = if (group == \"exec\") split.trailing else split.withoutGroup" in handle_body
        and "firstPositionalIndex(argv" in src and "group = argv[index]" in src
        and "group = argv[0]" not in src,
        line_of(src, "splitShizukuArgv(argv)"),
        "handle body + splitter impl" + ("" if handle_body else " MISSING"),
    ))

    # 10. none of the group handlers may read the raw rest tail for its sub
    raw_sub = line_of(src, "val sub = rest.firstOrNull()")
    results.append((
        "10 group sub-commands read parsed positionals, not raw rest",
        raw_sub == 0,
        raw_sub if raw_sub else line_of(src, "private fun handlePackage("),
        "raw `val sub = rest.firstOrNull()` hits=%d" % src.count("val sub = rest.firstOrNull()"),
    ))

    # 11. exec passthrough still joins the raw tail
    exec_body = function_body(src, "handleExec")
    results.append((
        "11 exec passthrough joins the raw tail verbatim",
        exec_body is not None and 'rest.joinToString(" ")' in exec_body,
        line_of(src, "private fun handleExec("),
        "handleExec " + ("missing" if exec_body is None else "present, must be `rest.joinToString(\" \")`"),
    ))

    # 12. the one two-meaning option must be opted out explicitly
    data_flag = 'hasFlag("data")' in src
    data_value = re.search(r'args\.get\(\s*"data"', src) is not None
    opt_out = 'NO_VALUE_FLAGS - "data"' in src
    results.append((
        "12 --data read both ways is opted out per group",
        not (data_flag and data_value) or opt_out,
        line_of(src, 'NO_VALUE_FLAGS - "data"'),
        "hasFlag(data)=%s get(data)=%s opt-out=%s" % (data_flag, data_value, opt_out),
    ))

    # 13. [deep-audit-1029 ISSUE-07] `exec`'s payload is a shell command, not an
    #     option list: the parser must be fed the CLI's own tokens only, and the
    #     one option `exec` owns (`--timeout-ms N`) must be lifted out of the
    #     payload by the splitter. Either half missing restores a shipped
    #     defect — the payload reaching the output shaper drops the whole `data`
    #     object on a non-zero exit, and the un-lifted option is executed by the
    #     shell (`sh -c "… --timeout-ms 5000"`).
    strip_body = function_body(src, "stripExecTimeoutTokens")
    splitter_body = function_body(src, "splitShizukuArgv") or ""
    strips_in_splitter = "stripExecTimeoutTokens(" in splitter_body
    parser_uses_exec_tokens = "split.execOptionTokens" in handle_body
    strips_out, strip_note = _strips_option_out(
        strip_body, _option_spellings(src, "--timeout-ms"))
    results.append((
        "13 exec trims its own options out of the shell payload",
        strips_out and strips_in_splitter and parser_uses_exec_tokens
        and "execOptionTokens = " in src,
        line_of(src, "internal fun stripExecTimeoutTokens("),
        "helper-strips=%s (%s) splitter-calls-it=%s handle-feeds-execOptionTokens=%s slot=%s"
        % (
            strips_out, strip_note,
            strips_in_splitter,
            parser_uses_exec_tokens,
            "execOptionTokens = " in src,
        ),
    ))
    # ── deep-audit-1034 ISSUE-04: netpolicy direction + uid resolution ─────
    # 14. `restrict-background-whitelist` EXEMPTS a uid from the background
    #     restriction, so `restrict` must REMOVE from that list and `allow` ADD
    #     to it; `restrict-background-blacklist` runs the other way. The shipped
    #     code had the wifi pair inverted, so `network restrict --wifi` kept the
    #     package online (and `network allow --wifi` cut it off).
    wl_sites = _call_literals(src, "restrict-background-whitelist")
    bl_sites = _call_literals(src, "restrict-background-blacklist")
    wl_live = [t for t, is_arg in wl_sites if is_arg]
    bl_live = [t for t, is_arg in bl_sites if is_arg]
    wl_bad = [t for t in wl_live if 'if (restrict) "remove" else "add"' not in t]
    bl_bad = [t for t in bl_live if 'if (restrict) "add" else "remove"' not in t]
    results.append((
        "14 netpolicy list direction follows the restrict flag",
        bool(wl_live) and bool(bl_live) and not wl_bad and not bl_bad,
        line_of(src, "restrict-background-whitelist"),
        "live array arguments: whitelist=%d (%d with the wrong direction), "
        "blacklist=%d (%d); direction must be chosen inside the array that is "
        "passed to the call, dead `val x = arrayOf(...)` copies are not call sites"
        % (len(wl_live), len(wl_bad), len(bl_live), len(bl_bad)),
    ))

    # 15. An unresolvable uid must refuse, never fall back to uid 0 (root).
    #     `pkgUid` used to end in `?: "0"`, so `network restrict <typo>` ran
    #     `cmd netpolicy … 0` and reported success while changing root's policy,
    #     and `network stats <typo>` printed root's traffic as the package's.
    #     Scope: the two callers + the resolver — `?: -1` elsewhere in the file
    #     (screen metrics, package-list importance) is legitimate.
    net_restrict = function_body(src, "networkRestrict") or ""
    net_stats = function_body(src, "networkStats") or ""
    restrict_refuses = re.search(
        r"pkgUid\(pkg\)\s*\?:\s*return\s+errEnvelope\(", net_restrict) is not None
    stats_refuses = re.search(
        r"pkgUid\(pkg\)\?\.toIntOrNull\(\)\s*\?:\s*return\s+errEnvelope\(", net_stats) is not None
    resolver_nullable = re.search(
        r"internal fun extractUidFromDumpsys\([^)]*\)\s*:\s*String\?\s*=", src) is not None
    resolver_body = function_body(src, "extractUidFromDumpsys") or ""
    zero_hit, zero_note = "", ""
    for scoped in (resolver_body, net_restrict, net_stats):
        hit, spelling = _zero_fallback(scoped)
        if hit:
            zero_hit, zero_note = scoped, spelling
            break
    no_zero_fallback = not zero_hit
    no_neg_one = "?: -1" not in net_stats
    results.append((
        "15 unresolvable uid refuses instead of aiming at root",
        restrict_refuses and stats_refuses and resolver_nullable and no_zero_fallback and no_neg_one,
        line_of(src, "val uid = pkgUid(pkg)"),
        "restrict-refuses=%s stats-refuses=%s resolver-String?=%s no-zero-fallback=%s%s no-minus-one=%s"
        % (
            restrict_refuses, stats_refuses, resolver_nullable, no_zero_fallback,
            "" if no_zero_fallback else " (found `?: %s`)" % zero_note,
            no_neg_one,
        ),
    ))
    # [dv15-A1 A-02] the value-taking short options are declared ONCE and that
    # declaration is what both call sites pass. Fail-closed: the set must be
    # exactly {a, d, p, c} — the four letters `activity start` forwards to
    # `am start`. A missing/renamed declaration makes `-p com.x` a flag plus a
    # stray positional, i.e. the launch silently does nothing.
    short_set = re.search(r'internal val SHIZUKU_VALUE_SHORT_OPTIONS = setOf\(([^)]*)\)', src)
    short_names = set(re.findall(r'"([^"]+)"', short_set.group(1))) if short_set else set()
    results.append((
        "16 short options that take a value are declared once",
        short_names == {"a", "d", "p", "c"},
        line_of(src, "SHIZUKU_VALUE_SHORT_OPTIONS"),
        "declared=%s" % (sorted(short_names) if short_set else "MISSING"),
    ))
    return results


COMPLIANT = '''package com.rikkaminis.app.sandbox.offload

internal val SHIZUKU_VALUE_SHORT_OPTIONS = setOf("a", "d", "p", "c")

class ShizukuOffloadHandler {
    private fun handle(request: Any) {
        val argv = listOf<String>()
        val split = splitShizukuArgv(argv)
        val group = split.group ?: return
        val args = if (group == "exec") {
            OffloadArgs(
                split.execOptionTokens,
                booleanFlags = noValueFlagsFor(group),
                valueShortOptions = SHIZUKU_VALUE_SHORT_OPTIONS,
            )
        } else {
            OffloadArgs(
                split.withoutGroup,
                booleanFlags = noValueFlagsFor(group),
                valueShortOptions = SHIZUKU_VALUE_SHORT_OPTIONS,
            )
        }
        val rest = if (group == "exec") split.trailing else split.withoutGroup
        val sub = args.positional.firstOrNull()
    }

    private fun handleExec(rest: List<String>, args: OffloadArgs): Int {
        val cmd = rest.joinToString(" ")
        return run(cmd)
    }

    private fun inputText(text: String, args: OffloadArgs): Int = run(encodeInputText(text))

    private fun packageClear(pkg: String?, args: OffloadArgs): Int {
        val cacheOnly = args.hasFlag("cache-only")
        val cmd = buildPackageClearCommand(pkg ?: "", cacheOnly, args.get("user"))
        return ok(cmd, clearScope(cacheOnly))
    }

    private fun netData(args: OffloadArgs): Int {
        if (args.hasFlag("data")) return run(args.get("user") ?: "")
        return 0
    }

    private fun actData(args: OffloadArgs): Int {
        return run(args.get("data", "d") ?: "")
    }

    private fun networkRestrict(pkg: String?, args: OffloadArgs, restrict: Boolean): NativeOffloadResult {
        if (pkg.isNullOrBlank()) return errEnvelope("INVALID_ARGS", "network restrict <pkg>", args)
        val uid = pkgUid(pkg)
            ?: return errEnvelope("OPERATION_FAILED", "network restrict: no uid for '$pkg'", args)
        if (args.hasFlag("wifi")) {
            val r = ShizukuManager.runProcess(arrayOf("cmd", "netpolicy",
                if (restrict) "remove" else "add", "restrict-background-whitelist", uid))
            return ok(r)
        }
        return run(arrayOf("cmd", "netpolicy",
            if (restrict) "add" else "remove", "restrict-background-blacklist", uid))
    }

    private fun networkStats(pkg: String?, args: OffloadArgs): NativeOffloadResult {
        val uid = pkgUid(pkg)?.toIntOrNull()
            ?: return errEnvelope("OPERATION_FAILED", "network stats: no uid for '$pkg'", args)
        val (rx, tx) = parseNetstats("", uid)
        return ok(listOf(rx, tx))
    }

    private fun pkgUid(pkg: String): String? {
        val r = ShizukuManager.runProcess(arrayOf("dumpsys", "package", pkg))
        return extractUidFromDumpsys(r.stdout)
    }

    private fun handleNotification(rest: List<String>, args: OffloadArgs): Int {
        return notificationChannel(args.positional.drop(1), args)
    }

    private fun notificationChannel(pos: List<String>, args: OffloadArgs): Int {
        val sub = pos.firstOrNull()
        val pkg = pos.getOrNull(1)
        return 0
    }

    companion object {
        internal val NO_VALUE_FLAGS = setOf("block", "unblock", "cache-only", "data", "all", "background", "wifi")
    }
}

internal fun noValueFlagsFor(group: String?): Set<String> =
    if (group == "activity") ShizukuOffloadHandler.NO_VALUE_FLAGS - "data"
    else ShizukuOffloadHandler.NO_VALUE_FLAGS

internal fun splitShizukuArgv(argv: List<String>): ShizukuArgv {
    val index = firstPositionalIndex(argv, ShizukuOffloadHandler.NO_VALUE_FLAGS, SHIZUKU_VALUE_SHORT_OPTIONS)
    if (index < 0) return ShizukuArgv(null, argv, emptyList(), execOptionTokens = argv)
    val group = argv[index]
    val leading = argv.take(index)
    val tail = argv.drop(index + 1)
    val (payload, execOptions) =
        if (group == EXEC_GROUP) stripExecTimeoutTokens(tail) else tail to emptyList()
    return ShizukuArgv(
        group = group,
        withoutGroup = argv.filterIndexed { i, _ -> i != index },
        trailing = payload,
        execOptionTokens = leading + execOptions,
    )
}

private const val EXEC_GROUP = "exec"

internal fun stripExecTimeoutTokens(tokens: List<String>): Pair<List<String>, List<String>> {
    val payload = mutableListOf<String>()
    val options = mutableListOf<String>()
    var i = 0
    while (i < tokens.size) {
        val token = tokens[i]
        val next = tokens.getOrNull(i + 1)
        when {
            token == "--timeout-ms" && next != null && isExecTimeoutValue(next) -> {
                options.add(token)
                options.add(next)
                i += 2
            }
            else -> {
                payload.add(token)
                i += 1
            }
        }
    }
    return payload to options
}

private fun isExecTimeoutValue(value: String): Boolean =
    value.isNotEmpty() && value.all { it in '0'..'9' }

internal fun firstPositionalIndex(
    argv: List<String>,
    booleanFlags: Set<String>,
    valueShortOptions: Set<String> = emptySet(),
): Int {
    val probe = OffloadArgs(argv, booleanFlags = booleanFlags, valueShortOptions = valueShortOptions)
    return if (probe.positional.isNotEmpty()) 0 else -1
}

internal fun extractUidFromDumpsys(dumpsysOutput: String): String? =
    Regex("""userId=(\\d+)""").find(dumpsysOutput)?.groupValues?.get(1)

internal fun encodeInputText(text: String): String =
    text.replace(" ", "%s").replace("'", "")

internal fun buildPackageClearCommand(pkg: String, cacheOnly: Boolean, user: String?): List<String> {
    val cmd = mutableListOf("pm", "clear")
    if (cacheOnly) cmd.add("--cache-only")
    if (user != null) cmd.addAll(listOf("--user", user))
    cmd.add(pkg)
    return cmd
}

internal fun clearScope(cacheOnly: Boolean): String = if (cacheOnly) "cache-only" else "data+cache"
'''

# criterion index (1-based) -> (mutation, expected broken criteria)
BREAKERS = {
    1: ("                split.withoutGroup,\n                booleanFlags = noValueFlagsFor(group),\n",
        "                split.withoutGroup,\n"),
    2: ('setOf("block", "unblock", "cache-only", "data", "all", "background", "wifi")',
        'setOf("block", "unblock", "cache-only", "data", "all", "background", "wifi", "importance")'),
    3: ('text.replace(" ", "%s")', "text.replace(' ', '%')"),
    4: ('cmd.add("--cache-only")', 'cmd.add("--cache-only") // kept'),  # replaced below
    5: ("notificationChannel(args.positional.drop(1), args)", "notificationChannel(rest.drop(1), args)"),
    6: ("pos.getOrNull(1)", "rest.getOrNull(1)"),
    7: ("clearScope(cacheOnly)", "clearScope(false)"),
    8: ('setOf("block", "unblock", "cache-only", "data", "all", "background", "wifi")',
        'setOf("block", "unblock", "data", "all", "background", "wifi")'),
    9: ("val group = split.group ?: return", "val group = argv[0]"),
    10: ("val sub = args.positional.firstOrNull()", "val sub = rest.firstOrNull()"),
    11: ('rest.joinToString(" ")', 'args.positional.joinToString(" ")'),
    12: ('NO_VALUE_FLAGS - "data"', 'NO_VALUE_FLAGS'),
    13: ("if (group == EXEC_GROUP) stripExecTimeoutTokens(tail) else tail to emptyList()",
         "if (group == EXEC_GROUP) tail to emptyList() else tail to emptyList()"),
    14: ('if (restrict) "remove" else "add", "restrict-background-whitelist", uid',
         'if (restrict) "add" else "remove", "restrict-background-whitelist", uid'),
    15: ("val uid = pkgUid(pkg)\n            ?: return errEnvelope",
         'val uid = pkgUid(pkg) ?: "0"\n            ?: return errEnvelope'),
}

# Criterion 13 bundles three anchors (helper present / splitter runs it / the
# parser is fed the isolated set). The main table can only flip one criterion
# per entry, so the other two anchors get their own arms here — otherwise a
# sub-condition could be dead text that no fixture proves live.
EXTRA_BREAKERS = [
    ("13b handle feeds the parser the whole argv again",
     "                split.execOptionTokens,\n",
     "                split.withoutGroup,\n", "13 "),
    ("13c the helper is gone (renamed)",
     "internal fun stripExecTimeoutTokens(tokens: List<String>): Pair<List<String>, List<String>> {",
     "internal fun stripExecTimeoutTokensRenamed(tokens: List<String>): "
     "Pair<List<String>, List<String>> {", "13 "),
    # Criterion 15 fuses the two callers and the resolver; each half gets its own
    # arm so no sub-condition can rot into dead text.
    ("15b the stats caller falls back instead of refusing",
     "val uid = pkgUid(pkg)?.toIntOrNull()\n            ?: return errEnvelope",
     'val uid = pkgUid(pkg)?.toIntOrNull() ?: -1\n            ?: return errEnvelope', "15 "),
    ("15c the resolver falls back to root's uid",
     r'Regex("""userId=(\d+)""").find(dumpsysOutput)?.groupValues?.get(1)',
     r'Regex("""userId=(\d+)""").find(dumpsysOutput)?.groupValues?.get(1) ?: "0"', "15 "),
    # [dv18-A A-01a] criterion 1 was re-anchored to every OffloadArgs(
    # construction; the pre-scan half gets its own arms — a pre-scan that drops
    # the set (or a probe that stops threading it) mis-splits
    # `-p com.x package info` while the handle sites stay correct.
    ("1b the pre-scan call drops the value-short set",
     "firstPositionalIndex(argv, ShizukuOffloadHandler.NO_VALUE_FLAGS, SHIZUKU_VALUE_SHORT_OPTIONS)",
     "firstPositionalIndex(argv, ShizukuOffloadHandler.NO_VALUE_FLAGS)", "1 "),
    ("1c the pre-scan probe stops threading the value shorts",
     "val probe = OffloadArgs(argv, booleanFlags = booleanFlags, valueShortOptions = valueShortOptions)",
     "val probe = OffloadArgs(argv, booleanFlags = booleanFlags)", "1 "),
    # ── [wide-1024] the three witnesses this branch was handed ────────────
    # Criterion 14 (anchor axis): a DEAD arrayOf above the real call used to
    # absorb the first-mention anchor, so the direction actually executed by
    # `runProcess(` was never read. The live call below is inverted.
    ("14b a dead arrayOf absorbs the anchor and the live call is inverted",
     '            val r = ShizukuManager.runProcess(arrayOf("cmd", "netpolicy",\n'
     '                if (restrict) "remove" else "add", "restrict-background-whitelist", uid))',
     '            val _unused = arrayOf("cmd", "netpolicy", if (restrict) "remove" else "add", '
     '"restrict-background-whitelist", uid)\n'
     '            val r = ShizukuManager.runProcess(arrayOf("cmd", "netpolicy", '
     'if (restrict) "add" else "remove", "restrict-background-whitelist", uid))', "14 "),
    # Criterion 13 (dataflow axis): the helper exists and the splitter calls it,
    # but it short-circuits — the option stays in the shell payload.
    ("13d the helper short-circuits and hands the payload back unchanged",
     "internal fun stripExecTimeoutTokens(tokens: List<String>): "
     "Pair<List<String>, List<String>> {\n",
     "internal fun stripExecTimeoutTokens(tokens: List<String>): "
     "Pair<List<String>, List<String>> {\n    if (true) return tokens to tokens\n", "13 "),
    # Criterion 15 (spelling axis): the same root fallback, spelled as an
    # expression instead of the literal `"0"`.
    ("15d the resolver falls back to a re-spelled root uid",
     r'Regex("""userId=(\d+)""").find(dumpsysOutput)?.groupValues?.get(1)',
     r'Regex("""userId=(\d+)""").find(dumpsysOutput)?.groupValues?.get(1) ?: (0).toString()', "15 "),
]
# criterion 4 has two halves; break the global-trim half explicitly
BREAKERS[4] = (
    'internal fun buildPackageClearCommand(pkg: String, cacheOnly: Boolean, user: String?): List<String> {\n'
    '    val cmd = mutableListOf("pm", "clear")',
    'internal fun buildPackageClearCommand(pkg: String, cacheOnly: Boolean, user: String?): List<String> {\n'
    '    val cmd = mutableListOf("pm", "trim-caches", "1G")',
)


# criterion 16: the declaration itself (a dropped letter = a dead short option)
BREAKERS[16] = ('internal val SHIZUKU_VALUE_SHORT_OPTIONS = setOf("a", "d", "p", "c")',
                'internal val SHIZUKU_VALUE_SHORT_OPTIONS = setOf("a", "d", "p")')

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

    names = [c for c, _, _, _ in base]
    for idx, (old, new) in BREAKERS.items():
        if idx > len(names):
            failures.append("criterion %d is gone from analyse()" % idx)
            print("  [FAIL] criterion %d: not evaluated (only %d criteria)" % (idx, len(names)))
            continue
        if old not in COMPLIANT:
            failures.append("breaker %d cannot apply (fixture text moved)" % idx)
            print("  [FAIL] breaker %d: anchor not found in fixture" % idx)
            continue
        mutated = strip_comments(COMPLIANT.replace(old, new, 1))
        res = analyse(mutated)
        red = [c for c, ok, _, _ in res if not ok]
        target = names[idx - 1]
        if target not in red:
            failures.append("breaker %d did not turn %r red (red=%s)" % (idx, target, red))
            print("  [FAIL] breaker %d -> %r stayed green (red=%s)" % (idx, target, red))
        else:
            print("  [OK] breaker %d -> red on %r%s" % (idx, target, "" if len(red) == 1 else " (+%d side)" % (len(red) - 1)))

    # Each arm names the criterion family it must flip (2nd element = prefix);
    # the table can only flip one criterion per entry, so sub-conditions of a
    # bundled criterion get their own arms here — otherwise they could rot into
    # dead text that no fixture proves live.
    for label, old, new, prefix in EXTRA_BREAKERS:
        target = next((c for c in names if c.startswith(prefix)), None)
        if target is None:
            failures.append("%s: criterion %r is gone from analyse()" % (label, prefix))
            print("  [FAIL] %s: criterion %r not evaluated" % (label, prefix))
            continue
        if old not in COMPLIANT:
            failures.append("%s: anchor not found in fixture" % label)
            print("  [FAIL] %s: anchor not found in fixture" % label)
            continue
        res = analyse(strip_comments(COMPLIANT.replace(old, new, 1)))
        red = [c for c, ok, _, _ in res if not ok]
        if target in red:
            print("  [OK] breaker %s -> red on %r%s" % (label, target, "" if len(red) == 1 else " (+%d side)" % (len(red) - 1)))
        else:
            failures.append("breaker %s did not turn %r red (red=%s)" % (label, target, red))
            print("  [FAIL] breaker %s -> %r stayed green (red=%s)" % (label, target, red))
    if not failures:
        print("SHIZUKU_ARGS_SELFTEST=PASS criteria=%d breakers=%d extra=%d"
              % (len(base), len(BREAKERS), len(EXTRA_BREAKERS)))
    return failures


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = args[0] if args else "."
    if "--selftest" in sys.argv:
        fails = selftest()
        if fails:
            print("❌ selftest: the gate has no teeth (%d)" % len(fails))
            return 1
        print("✅ selftest: gate flips red on every criterion")
        return 0

    path = os.path.join(root, REL)
    print("━━━ shizuku_args_check ━━━")
    if not os.path.exists(path):
        print("  [ROT] %s not found — the handler moved; keep this gate honest" % REL)
        return 1
    with open(path, encoding="utf-8") as f:
        results = analyse(strip_comments(f.read()))
    bad = []
    for crit, ok, line, note in results:
        where = "%s:%d" % (REL.replace(os.sep, "/"), line) if line else "%s:<anchor missing>" % REL
        print("  [%s] %s  (%s) @ %s" % ("OK" if ok else "FAIL", crit, note, where))
        if not ok:
            bad.append((crit, where))
    print("")
    if bad:
        print("❌ shizuku_args_check findings (%d):" % len(bad))
        for crit, where in bad:
            print("   - %s:%s" % (where, crit))
        return 1
    print("✅ android-shizuku-cli argument handling: argv parsing + call sites verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())

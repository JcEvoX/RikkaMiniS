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
  7. `packageClear` reports the scope it actually cleared (`clearScope(...)`).

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


def analyse(src):
    """-> list of (criterion, ok, detail). One entry per criterion, always."""
    results = []
    flags = re.search(r"val\s+NO_VALUE_FLAGS\s*=\s*setOf\(([^)]*)\)", src)
    names = set(re.findall(r'"([^"]+)"', flags.group(1))) if flags else set()

    results.append((
        "1 parser receives declared switches",
        re.search(r"OffloadArgs\(\s*rest\s*,\s*booleanFlags\s*=\s*NO_VALUE_FLAGS\s*\)", src) is not None,
        "OffloadArgs(rest, booleanFlags = NO_VALUE_FLAGS)",
    ))
    results.append((
        "2 NO_VALUE_FLAGS covers switches, excludes value flags",
        flags is not None and {"block", "unblock"} <= names and "importance" not in names,
        "declared=%s" % (sorted(names) if flags else "MISSING"),
    ))
    body = function_body(src, "encodeInputText")
    results.append((
        "3 input text uses the platform %s escape",
        body is not None and '"%s"' in body and "replace(' ', '%')" not in body,
        "helper " + ("missing" if body is None else "found"),
    ))
    cmd = function_body(src, "buildPackageClearCommand")
    results.append((
        "4 package clear is per-package (no global trim-caches stand-in)",
        cmd is not None and '--cache-only' in cmd and '"pm", "clear"' in cmd and "trim-caches" not in src,
        "helper " + ("missing" if cmd is None else "found"),
    ))
    notify = function_body(src, "handleNotification") or ""
    results.append((
        "5 channel sub-command wired to parsed positionals",
        "notificationChannel(args.positional.drop(1), args)" in notify,
        "handleNotification body" if notify else "handleNotification MISSING",
    ))
    chan = function_body(src, "notificationChannel")
    results.append((
        "6 notificationChannel reads positionals, not raw argv",
        chan is not None and "pos.getOrNull(1)" in chan and "rest.getOrNull(" not in chan,
        "helper " + ("missing" if chan is None else "found"),
    ))
    clear = function_body(src, "packageClear")
    results.append((
        "7 package clear payload reports the scope it actually cleared",
        clear is not None and "clearScope(cacheOnly)" in clear
        and "buildPackageClearCommand(" in clear,
        "helper " + ("missing" if clear is None else "found"),
    ))
    return results


COMPLIANT = '''package com.rikkaminis.app.sandbox.offload

class ShizukuOffloadHandler {
    private fun handle(request: Any) {
        val rest = listOf<String>()
        val args = OffloadArgs(rest, booleanFlags = NO_VALUE_FLAGS)
    }

    private fun inputText(text: String, args: OffloadArgs): Int = run(encodeInputText(text))

    private fun packageClear(pkg: String?, args: OffloadArgs): Int {
        val cacheOnly = args.hasFlag("cache-only")
        val cmd = buildPackageClearCommand(pkg ?: "", cacheOnly, args.get("user"))
        return ok(cmd, clearScope(cacheOnly))
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
        internal val NO_VALUE_FLAGS = setOf("block", "unblock")
    }
}

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
    1: ("OffloadArgs(rest, booleanFlags = NO_VALUE_FLAGS)", "OffloadArgs(rest)"),
    2: ('setOf("block", "unblock")', 'setOf("block", "unblock", "importance")'),
    3: ('text.replace(" ", "%s")', "text.replace(' ', '%')"),
    4: ('cmd.add("--cache-only")', 'cmd.add("--cache-only") // kept'),  # replaced below
    5: ("notificationChannel(args.positional.drop(1), args)", "notificationChannel(rest.drop(1), args)"),
    6: ("pos.getOrNull(1)", "rest.getOrNull(1)"),
    7: ("clearScope(cacheOnly)", "clearScope(false)"),
}
# criterion 4 has two halves; break the global-trim half explicitly
BREAKERS[4] = (
    'internal fun buildPackageClearCommand(pkg: String, cacheOnly: Boolean, user: String?): List<String> {\n'
    '    val cmd = mutableListOf("pm", "clear")',
    'internal fun buildPackageClearCommand(pkg: String, cacheOnly: Boolean, user: String?): List<String> {\n'
    '    val cmd = mutableListOf("pm", "trim-caches", "1G")',
)


def selftest():
    print("── selftest: compliant fixture must be green, each breaker must go red ──")
    base = analyse(strip_comments(COMPLIANT))
    failures = []
    greens = [c for c, ok, _ in base if ok]
    if len(greens) != len(base):
        failures.append("compliant fixture is not green: %s" % [c for c, ok, _ in base if not ok])
        print("  [FAIL] compliant fixture red on: %s" % [c for c, ok, _ in base if not ok])
    else:
        print("  [OK] compliant fixture green on all %d criteria" % len(base))

    names = [c for c, _, _ in base]
    for idx, (old, new) in BREAKERS.items():
        if old not in COMPLIANT:
            failures.append("breaker %d cannot apply (fixture text moved)" % idx)
            print("  [FAIL] breaker %d: anchor not found in fixture" % idx)
            continue
        mutated = strip_comments(COMPLIANT.replace(old, new, 1))
        res = analyse(mutated)
        red = [c for c, ok, _ in res if not ok]
        target = names[idx - 1]
        if target not in red:
            failures.append("breaker %d did not turn %r red (red=%s)" % (idx, target, red))
            print("  [FAIL] breaker %d -> %r stayed green (red=%s)" % (idx, target, red))
        else:
            print("  [OK] breaker %d -> red on %r%s" % (idx, target, "" if len(red) == 1 else " (+%d side)" % (len(red) - 1)))
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
    for crit, ok, detail in results:
        print("  [%s] %s  (%s)" % ("OK" if ok else "FAIL", crit, detail))
        if not ok:
            bad.append(crit)
    print("")
    if bad:
        print("❌ shizuku_args_check findings (%d):" % len(bad))
        for b in bad:
            print("   - %s" % b)
        return 1
    print("✅ android-shizuku-cli argument handling: argv parsing + call sites verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())

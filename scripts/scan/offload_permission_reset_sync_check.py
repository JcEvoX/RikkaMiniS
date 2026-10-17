#!/usr/bin/env python3
"""[fix/da1024-H] offload_permission_reset_sync_check

Issue-08 gate: a permission-level change must reach BOTH the persisted level and
the in-memory session caches, and the settings screen must actually re-read the
level after such a change.

  1. OffloadPermissionManager.resetAll() drops every session's grants AND
     denials (inline, or via a helper the body calls) — "Reset All" that leaves
     a cached verdict behind silently allows/refuses the next call in the same
     session;
  2. OffloadPermissionManager.setLevel() drops ONLY that tool's cached verdict
     (a per-tool removal, never a clear-all: grants for untouched tools are
     still valid verdicts), and must not leave the session caches alone either;
  3. both paths bump the `_revision` refresh signal;
  4. OffloadPermissionScreen.kt keys its row level state on that revision — no
     keyless `remember { mutableStateOf(OffloadPermissionManager.getLevel(...)) }`
     (which froze the first-composition value, so "Reset All" left the whole
     page showing the old levels), and both rows subscribe to the signal.

Bodies are followed through the helpers they call (depth-capped), so an
implementation that extracts `clearAllSessionGrants()` / `clearToolSessionState()`
/ `bumpRevision()` is judged on what those helpers actually do — an empty
helper is a red, not a pass.

Code anchors only: comments are stripped first, so a KDoc *mentioning*
`sessionGrants.clear()` cannot satisfy criterion 1.

Usage: python3 offload_permission_reset_sync_check.py <repo_root>
       python3 offload_permission_reset_sync_check.py --root <repo_root>
       python3 offload_permission_reset_sync_check.py --selftest
Exit 0 = pass, 1 = violation, 2 = usage error.
"""
import os
import re
import sys

MANAGER = "src/android/app/src/main/java/com/rikkaminis/app/offload/OffloadPermissionManager.kt"
SCREEN = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/OffloadPermissionScreen.kt"
MARKER = "OFFLOAD_PERMISSION_RESET_SYNC_SELFTEST=PASS"

FUN_RE = re.compile(r"\bfun\s+([A-Za-z_][A-Za-z0-9_]*)\s*[<(]")
KEYLESS_RE = re.compile(
    r"remember\s*\{\s*mutableStateOf\(\s*OffloadPermissionManager\.getLevel"
)
REVISION_KEY_RE = re.compile(r"remember\s*\([^)\n]*\brevision\b[^)\n]*\)")


def strip_comments(src: str) -> str:
    """Remove // and /* */ comments, honouring string and raw-string literals.

    KDoc in these files quotes the very anchors the gate looks for, so comment
    text must not be able to satisfy a criterion.
    """
    out = []
    state = "code"
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        two = src[i:i + 2]
        if state == "code":
            if two == "//":
                state = "line"
                i += 2
            elif two == "/*":
                state = "block"
                i += 2
            elif src[i:i + 3] == '"""':
                state = "raw"
                i += 3
            elif c == '"':
                state = "str"
                out.append(c)
                i += 1
            else:
                out.append(c)
                i += 1
        elif state == "line":
            if c == "\n":
                state = "code"
                out.append(c)
            i += 1
        elif state == "block":
            if two == "*/":
                state = "code"
                i += 2
            else:
                if c == "\n":
                    out.append(c)
                i += 1
        elif state == "str":
            if c == "\\":
                out.append(src[i:i + 2])
                i += 2
                continue
            if c == "\n":
                state = "code"
            elif c == '"':
                state = "code"
            out.append(c)
            i += 1
        else:  # raw string
            if src[i:i + 3] == '"""':
                state = "code"
                i += 3
            else:
                i += 1
    return "".join(out)


def body_of(src: str, header: str) -> str:
    """Braces-balanced body of the first function whose declaration contains
    `header` (which must end just before the `{`, e.g. 'fun resetAll('). ''

    when absent — absent means red, never a silent pass."""
    i = src.find(header)
    if i < 0:
        return ""
    j = src.find("{", i)
    if j < 0:
        return ""
    depth = 0
    k = j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
        k += 1
    return ""


def expand(src: str, body: str, defined, depth: int = 3) -> str:
    """`body` plus the bodies of any file-defined function it calls (recursively,
    depth-capped) so an extracted helper is judged by its own contents."""
    if depth <= 0 or not body:
        return body
    out = body
    for name in defined:
        if name + "(" in body:
            sub = body_of(src, "fun " + name + "(")
            if sub and sub != body:
                out += "\n" + expand(src, sub, defined, depth - 1)
    return out


def bumps_revision(text: str) -> bool:
    """Any of the accepted ways to move the counter forward."""
    return (
        "_revision.value++" in text
        or "_revision.update" in text
        or "_revision.value =" in text
        or "_revision.value=" in text
    )


def check(manager_raw: str, screen_raw: str) -> list:
    mgr = strip_comments(manager_raw)
    screen = strip_comments(screen_raw)
    defined = set(FUN_RE.findall(mgr))
    errs = []

    # ── the refresh signal itself ────────────────────────────────────────────
    if "_revision" not in mgr or not re.search(r"\bval\s+revision\b", mgr):
        errs.append(
            "OffloadPermissionManager has no `revision` refresh signal "
            "(the settings rows cannot observe a level change without one)"
        )
    elif "asStateFlow()" not in mgr:
        errs.append("`revision` is not exposed as a read-only StateFlow (asStateFlow())")

    # ── 1. resetAll clears every session's grants AND denials ────────────────
    reset = expand(mgr, body_of(mgr, "fun resetAll("), defined)
    if not reset.strip():
        errs.append("resetAll() not found")
    else:
        if "sessionGrants.clear()" not in reset or "sessionDenials.clear()" not in reset:
            errs.append(
                "resetAll() does not clear the in-memory session caches "
                "(needs sessionGrants.clear() AND sessionDenials.clear(), inline or in a helper it calls)"
            )
        if not bumps_revision(reset):
            errs.append("resetAll() does not bump _revision (the UI never refreshes after Reset All)")

    # ── 2. setLevel drops that tool's cached verdict, and only that tool's ───
    setl = expand(mgr, body_of(mgr, "fun setLevel("), defined)
    if not setl.strip():
        errs.append("setLevel() not found")
    else:
        per_tool = re.search(r"remove\s*\(\s*toolName\s*\)", setl)
        if "sessionGrants" not in setl or "sessionDenials" not in setl:
            errs.append(
                "setLevel() does not touch the in-memory session caches "
                "(a cached verdict outlives the level change)"
            )
        elif not per_tool:
            errs.append(
                "setLevel() does not remove the tool's own cached verdict "
                "(expected a per-tool remove(toolName) over the session caches)"
            )
        if "sessionGrants.clear()" in setl or "sessionDenials.clear()" in setl:
            errs.append(
                "setLevel() clears EVERY tool's session cache — a level change must not "
                "invalidate grants for tools the user did not touch"
            )
        if not bumps_revision(setl):
            errs.append("setLevel() does not bump _revision (the row's own re-read is not driven by it)")

    # ── 3. the settings rows re-read the level off that signal ───────────────
    keyless = KEYLESS_RE.search(screen)
    if keyless:
        line = screen[:keyless.start()].count("\n") + 1
        errs.append(
            f"{SCREEN}:{line} has a keyless `remember {{ mutableStateOf(OffloadPermissionManager.getLevel(...)) }}` "
            "— it freezes the first-composition level"
        )
    if "OffloadPermissionManager.revision" not in screen:
        errs.append(f"{SCREEN} never subscribes to OffloadPermissionManager.revision")
    else:
        keys = REVISION_KEY_RE.findall(screen)
        if len(keys) < 2:
            errs.append(
                f"{SCREEN} keys only {len(keys)} remember(...) on revision, expected >= 2 "
                "(AgentPolicyRow and PermissionRow each own their level state)"
            )
    return errs


# ── selftest ────────────────────────────────────────────────────────────────

_HELPERS = """
    private val sessionGrants = java.util.concurrent.ConcurrentHashMap<String, MutableSet<String>>()
    private val sessionDenials = java.util.concurrent.ConcurrentHashMap<String, MutableSet<String>>()
    private val _revision = MutableStateFlow(0)
    val revision: StateFlow<Int> = _revision.asStateFlow()

    internal fun sessionsContaining(sessions: Map<String, Set<String>>, toolName: String): Set<String> =
        sessions.entries.filter { toolName in it.value }.map { it.key }.toSet()

    internal fun clearToolFrom(sessions: MutableMap<String, MutableSet<String>>, toolName: String) {
        for (id in sessionsContaining(sessions, toolName)) {
            sessions[id]?.remove(toolName)
        }
    }

    internal fun clearToolSessionState(toolName: String) {
        clearToolFrom(sessionGrants, toolName)
        clearToolFrom(sessionDenials, toolName)
    }

    internal fun clearAllSessionGrants() {
        sessionGrants.clear()
        sessionDenials.clear()
    }

    private fun bumpRevision() {
        _revision.update { it + 1 }
    }
"""

_GOOD_RESET = """
    fun resetAll() {
        val editor = prefs.edit()
        for (tool in toolRegistry) {
            editor.remove("level_${tool.toolName}")
        }
        editor.apply()
        clearAllSessionGrants()
        bumpRevision()
    }
"""

_GOOD_SET = """
    fun setLevel(toolName: String, level: PermissionLevel) {
        prefs.edit().putString("level_$toolName", level.name).apply()
        clearToolSessionState(toolName)
        bumpRevision()
    }
"""

_GOOD_SCREEN = """
@Composable
private fun AgentPolicyRow(toolName: String, showDivider: Boolean) {
    val revision by OffloadPermissionManager.revision.collectAsState()
    var currentLevel by remember(toolName, revision) { mutableStateOf(OffloadPermissionManager.getLevel(toolName)) }
    var expanded by remember { mutableStateOf(false) }
}

@Composable
private fun PermissionRow(tool: ToolPermissionInfo, showDivider: Boolean) {
    val revision by OffloadPermissionManager.revision.collectAsState()
    var currentLevel by remember(tool.toolName, revision) { mutableStateOf(OffloadPermissionManager.getLevel(tool.toolName)) }
    var expanded by remember { mutableStateOf(false) }
}
"""


def _manager(reset=_GOOD_RESET, set_level=_GOOD_SET, helpers=_HELPERS) -> str:
    return "object OffloadPermissionManager {\n" + helpers + set_level + reset + "}\n"


def selftest() -> int:
    arms = []

    # compliant (incl. the comment trap: KDoc quoting the anchors must not help)
    comment_trap = (
        "/**\n"
        " * [fix/da1024-H] resetAll() must call sessionGrants.clear() and\n"
        " * sessionDenials.clear(); setLevel(toolName, level) removes remove(toolName).\n"
        " */\n"
    )
    arms.append(("compliant (+ KDoc quoting the anchors)", comment_trap + _manager(), _GOOD_SCREEN, 0))

    # 1: resetAll leaves the session caches alone
    bad_reset_no_clear = """
    fun resetAll() {
        val editor = prefs.edit()
        for (tool in toolRegistry) {
            editor.remove("level_${tool.toolName}")
        }
        editor.apply()
        bumpRevision()
    }
"""
    arms.append((
        "V1 resetAll does not clear the session caches",
        _manager(reset=bad_reset_no_clear),
        _GOOD_SCREEN,
        1,
    ))

    # 1b: resetAll calls the helper, but the helper is empty (the extracted-helper hole)
    empty_helper = _HELPERS.replace(
        "    internal fun clearAllSessionGrants() {\n"
        "        sessionGrants.clear()\n"
        "        sessionDenials.clear()\n"
        "    }\n",
        "    internal fun clearAllSessionGrants() {\n"
        "    }\n",
    )
    arms.append((
        "V2 resetAll calls an EMPTY clearAllSessionGrants()",
        _manager(helpers=empty_helper),
        _GOOD_SCREEN,
        1,
    ))

    # 1c: no revision bump on reset
    no_bump_reset = _GOOD_RESET.replace("        bumpRevision()\n", "")
    arms.append((
        "V3 resetAll does not bump _revision",
        _manager(reset=no_bump_reset),
        _GOOD_SCREEN,
        1,
    ))

    # 2: setLevel ignores the session caches
    bad_set_ignores = """
    fun setLevel(toolName: String, level: PermissionLevel) {
        prefs.edit().putString("level_$toolName", level.name).apply()
        bumpRevision()
    }
"""
    arms.append((
        "V4 setLevel leaves the session caches alone",
        _manager(set_level=bad_set_ignores),
        _GOOD_SCREEN,
        1,
    ))

    # 2b: setLevel over-clears (every tool, every session) on top of the right per-tool removal
    over_clear = """
    fun setLevel(toolName: String, level: PermissionLevel) {
        prefs.edit().putString("level_$toolName", level.name).apply()
        clearToolSessionState(toolName)
        sessionGrants.clear()
        sessionDenials.clear()
        bumpRevision()
    }
"""
    arms.append((
        "V5 setLevel clears EVERY tool's cache (over-clear)",
        _manager(set_level=over_clear),
        _GOOD_SCREEN,
        1,
    ))

    # 2c: setLevel removes the tool but does not bump
    no_bump_set = _GOOD_SET.replace("        bumpRevision()\n", "")
    arms.append((
        "V6 setLevel does not bump _revision",
        _manager(set_level=no_bump_set),
        _GOOD_SCREEN,
        1,
    ))

    # 3: keyless remember reappears in one row (the other two stay keyed)
    keyless_screen = _GOOD_SCREEN + """
@Composable
private fun RegressionRow(toolName: String) {
    var currentLevel by remember { mutableStateOf(OffloadPermissionManager.getLevel(toolName)) }
}
"""
    arms.append(("V7 screen keeps a keyless remember for the level", _manager(), keyless_screen, 1))

    # 3b: screen never subscribes at all (rows deleted / signal dropped)
    unsubscribed = _GOOD_SCREEN.replace("OffloadPermissionManager.revision.collectAsState()", "null")
    arms.append(("V8 screen never subscribes to revision", _manager(), unsubscribed, 1))

    # 3c: one row keyed, the other not
    one_row = _GOOD_SCREEN.replace("remember(tool.toolName, revision)", "remember(tool.toolName)")
    arms.append(("V9 only one row is keyed on revision", _manager(), one_row, 1))

    failures = 0
    for name, mgr, screen, expect in arms:
        errs = check(mgr, screen)
        if len(errs) != expect:
            print(f"  FAIL {name}: expected {expect} error(s), got {len(errs)}: {errs}")
            failures += 1
        else:
            print(f"  OK {name}: {errs if errs else 'green'}")
    if failures:
        print(f"SELFTEST FAILED ({failures} arms)")
        return 1
    print(MARKER)
    return 0


def main() -> int:
    args = list(sys.argv[1:])
    if "--selftest" in args:
        return selftest()
    root = None
    if "--root" in args:
        i = args.index("--root")
        if i + 1 >= len(args):
            print("usage: --root <repo_root>", file=sys.stderr)
            return 2
        root = args[i + 1]
    elif len(args) == 1:
        root = args[0]
    else:
        print("usage: <repo_root> | --root <repo_root> | --selftest", file=sys.stderr)
        return 2
    try:
        manager = open(os.path.join(root, MANAGER)).read()
        screen = open(os.path.join(root, SCREEN)).read()
    except OSError as e:
        print(f"cannot read sources under {root}: {e}", file=sys.stderr)
        return 2
    errs = check(manager, screen)
    if errs:
        print("offload_permission_reset_sync_check RED:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print(f"offload_permission_reset_sync_check OK ({MANAGER} + {SCREEN})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

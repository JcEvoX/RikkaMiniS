#!/usr/bin/env python3
"""
Gate: FIX-E (deep-audit-1023 ISSUE-05, P1) — the Skills sheet PASTE import must
not run the repository write on the Main dispatcher.

The URL tab already wraps its repository call in withContext(Dispatchers.IO);
the paste branch carried the [audit-0917] comment demanding the same wrap while
the code never had it — importFromContent (parse + SQLite write + SKILL.md
write) ran on Main, the ANR the URL branch avoids.

Fix contract asserted here:
  arm1  the paste branch's importFromContent(pasteContent) call is lexically
        inside a `withContext(Dispatchers.IO) { ... }` block (brace-depth scan,
        comments stripped, string templates masked)
  arm2  there is exactly one importFromContent(pasteContent) call site, so the
        wrap cannot be satisfied by an orphan copy elsewhere in the file

deep-audit-1032 ISSUE-06 (P1) arms added to the same gate (E owns only this
gate; no new gate, no new SELFTEST marker):
  arm3  every `reloadFromDisk()` call in the Skills screen is OFF Main — either
        lexically inside a `withContext(Dispatchers.IO)` block *in that file*,
        or routed through a repository method that wraps internally. The
        criterion binds the DECISION POINT (the thread context the call runs
        on), not one spelling of it: moving the wrap into the repository is an
        equivalent fix and must stay green (1031-D lesson).
  arm4  same for the MCP screen (`mcpRepository.reloadFromDisk()`).
        Both screens are asserted — fixing only one is the shape this arm
        exists to catch.
  arm5  the skill editor's remember keys: `editName` must carry keys (an
        un-keyed draft deadlocks the name dialog), `editContent` keys must
        include `updatedAt` (otherwise initialContent re-derives while
        editContent keeps the pre-edit snapshot forever), and the shape anchor
        `val initialContent = remember` must still be present in the file
        (skill_frontmatter_roundtrip_check.py pins that literal).

Limits (deliberate, documented so they are not mistaken for coverage):
  * the repository-side alternative resolves the method body plus ONE
    delegation hop (`fun reloadFromDisk() = load()` / `{ loadAll() }`), not a
    whole call-graph fixpoint. An unresolvable chain counts as "not wrapped"
    (fail-closed) — keep the call-site wrap in that case.
  * arms 3/4 only look at the two Settings screens. The chat/backup callers of
    reloadFromDisk run on non-suspend paths (buildSystemPrompt) and are out of
    this gate's scope; see fix-reports/FIX-E.md §7.

Usage: python3 scripts/scan/skill_import_offmain_check.py [repo_root]
       python3 scripts/scan/skill_import_offmain_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import re
import sys

SM_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/SkillsManagementScreen.kt"
CALL = "skillRepository.importFromContent(pasteContent)"
OPENER = "withContext(Dispatchers.IO)"

# [deep-audit-1032 ISSUE-06] The two Settings screens re-scan the repositories
# on entry; both used to do it inline on the Main dispatcher.
MCP_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/MCPIntegrationsScreen.kt"
SKILL_REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/SkillRepository.kt"
MCP_REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/MCPRepository.kt"

# (screen file, call expression, repository file, repository entry point)
RELOAD_SITES = (
    (SM_REL, "skillRepository.reloadFromDisk()", SKILL_REPO_REL, "reloadFromDisk"),
    (MCP_REL, "mcpRepository.reloadFromDisk()", MCP_REPO_REL, "reloadFromDisk"),
)

# Editor remember keys (arm5) live in the same screen file.
EDITOR_REL = SM_REL
EDITOR_KEYS = (
    ("editName", None),
    ("editContent", "updatedAt"),
)
INITIAL_LITERAL = "val initialContent = remember"

SELFTEST_MARK = "SKILL_IMPORT_OFFMAIN_SELFTEST=PASS"


def strip_comments(text):
    """Remove // and /* */ comments; strings are kept (length-preserving)."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            if text.startswith('"""', i):
                j = text.find('"""', i + 3)
                j = n if j < 0 else j + 3
            else:
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] in ('"', "\n"):
                        j += 1
                        break
                    j += 1
            out.append(text[i:j])
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def mask_strings(text):
    """Replace string-literal and char-literal CONTENTS with 'x'
    (length-preserving) so brace matching survives `${...}` templates."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            if text.startswith('"""', i):
                j = text.find('"""', i + 3)
                j = n if j < 0 else j + 3
            else:
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] in ('"', "\n"):
                        j += 1
                        break
                    j += 1
            out.append('x' * (j - i))
            i = j
        elif c == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] in ("'", "\n"):
                    j += 1
                    break
                j += 1
            out.append('x' * (j - i))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def inside_io_wrap(text_nc, text_masked):
    """True when the paste call site sits inside a withContext(Dispatchers.IO)
    block. Brace-depth stack over the masked text; each open brace remembers
    the code text immediately preceding it."""
    call_idx = text_nc.find(CALL)
    if call_idx < 0:
        return False, "call site not found"
    if text_nc.count(CALL) != 1:
        return False, "expected exactly 1 call site, found %d" % text_nc.count(CALL)
    stack = []
    for i, ch in enumerate(text_masked[:call_idx]):
        if ch == "{":
            head = text_nc[:i].rstrip()
            head = head[:-1].rstrip() if head.endswith("{") else head
            tail = head[-len("withContext(Dispatchers.IO)"):] if len(head) >= len(OPENER) else ""
            stack.append(tail == OPENER)
        elif ch == "}":
            if stack:
                stack.pop()
    if any(stack):
        return True, ""
    return False, "call site is not lexically inside %s { ... }" % OPENER


def _io_wrapped_at(text_nc, text_masked, idx):
    """True when code position `idx` sits inside an OPENER { ... } block.

    Brace-depth stack over the masked text; each open brace remembers the
    code text immediately preceding it (same trick as inside_io_wrap).
    """
    stack = []
    for i, ch in enumerate(text_masked[:idx]):
        if ch == "{":
            head = text_nc[:i].rstrip()
            head = head[:-1].rstrip() if head.endswith("{") else head
            tail = head[-len(OPENER):] if len(head) >= len(OPENER) else ""
            stack.append(tail == OPENER)
        elif ch == "}":
            if stack:
                stack.pop()
    return any(stack)


def all_calls_offline(text_nc, text_masked, call):
    """True when EVERY occurrence of `call` sits inside an OPENER block."""
    idxs = []
    start = 0
    while True:
        j = text_nc.find(call, start)
        if j < 0:
            break
        idxs.append(j)
        start = j + 1
    if not idxs:
        return False, "no call site found (%s)" % call
    bad = [
        text_nc[:i].count("\n") + 1
        for i in idxs
        if not _io_wrapped_at(text_nc, text_masked, i)
    ]
    if bad:
        return False, "bare (Main-thread) call site(s) at line(s) %s" % bad
    return True, ""


def _match_brace(masked, j):
    """Index just past the `}` matching the `{` at `j` (masked text)."""
    depth = 0
    i = j
    while i < len(masked):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(masked)


def repo_method_wraps_io(text, name, hops=1):
    """True when `fun <name>(...)` wraps its work inside OPENER.

    Handles a block body (brace-matched, comments stripped, strings masked)
    and ONE expression-body delegation hop (`fun reloadFromDisk() = load()`).
    Not a call-graph fixpoint — fail-closed when the chain does not resolve.
    """
    nc = strip_comments(text)
    masked = mask_strings(nc)
    m = re.search(r"\bfun\s+%s\s*\(" % re.escape(name), nc)
    if not m:
        return False, "no `fun %s(` declaration" % name
    i = m.end()  # first char after '('
    depth = 1
    while i < len(masked) and depth:
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            depth -= 1
        i += 1
    j = i
    while j < len(masked) and masked[j] in " \t\r\n":
        j += 1
    if j < len(masked) and masked[j] == "{":
        body = nc[j:_match_brace(masked, j)]
        if OPENER in body:
            return True, ""
        if hops > 0:
            callees = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", body)
            for callee in callees:
                if callee == name:
                    continue
                ok, _ = repo_method_wraps_io(text, callee, hops - 1)
                if ok:
                    return True, ""
        return False, "`%s` body is not inside %s" % (name, OPENER)
    if j < len(masked) and masked[j] == "=" and masked[j + 1:j + 2] != "=":
        line = nc[j:].split("\n", 1)[0]
        call = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if call and hops > 0:
            return repo_method_wraps_io(text, call.group(1), hops - 1)
        return False, "`%s` expression body is not inside %s" % (name, OPENER)
    return False, "`%s`: body form not recognised" % name


def check_paste(text):
    """arms 1-2: the paste import's repository write must be off Main."""
    nc = strip_comments(text)
    masked = mask_strings(nc)
    ok, detail = inside_io_wrap(nc, masked)
    return [] if ok else ["paste import not on IO — %s" % detail]


def check_reload(files):
    """arms 3-4: every reloadFromDisk() call in the two screens is OFF Main.

    Offline = the call site is lexically inside an OPENER block in its own
    file, OR the repository entry point wraps internally (decision point =
    the thread the call runs on, not the spelling).
    """
    failures = []
    for screen_rel, call, repo_rel, repo_fn in RELOAD_SITES:
        text = files.get(screen_rel)
        if text is None:
            failures.append("missing file %s" % screen_rel)
            continue
        nc = strip_comments(text)
        masked = mask_strings(nc)
        ok, detail = all_calls_offline(nc, masked, call)
        if ok:
            continue
        repo_text = files.get(repo_rel)
        alt = False
        if repo_text is not None:
            alt, _ = repo_method_wraps_io(repo_text, repo_fn)
        if not alt:
            failures.append("%s: reload not off Main — %s" % (screen_rel, detail))
    return failures


def check_editor_keys(files):
    """arm5: the skill editor's remember keys + shape anchor literal."""
    text = files.get(EDITOR_REL)
    if text is None:
        return ["missing file %s" % EDITOR_REL]
    nc = strip_comments(text)
    failures = []
    for var, require in EDITOR_KEYS:
        decl = re.search(r"\bvar\s+%s\s+by\s+remember" % var, nc)
        if not decl:
            failures.append(
                "%s: `var %s by remember` declaration not found" % (EDITOR_REL, var))
            continue
        keys = re.match(r"\s*\(([^)]*)\)", nc[decl.end():])
        if not keys:
            failures.append("%s: `%s` remember has no key" % (EDITOR_REL, var))
            continue
        k = keys.group(1).strip()
        if k in ("", "Unit"):
            failures.append(
                "%s: `%s` remember key list is empty/Unit" % (EDITOR_REL, var))
        elif require and require not in k:
            failures.append(
                "%s: `%s` remember keys lack `%s`" % (EDITOR_REL, var, require))
    if INITIAL_LITERAL not in nc:
        failures.append(
            "%s: shape anchor `%s` missing" % (EDITOR_REL, INITIAL_LITERAL))
    return failures


def _read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return fh.read()


def run_checks_map(files):
    """All arms over a {rel_path: text} map (real tree or fixtures)."""
    failures = []
    sm = files.get(SM_REL)
    if sm is None:
        failures.append("missing file %s" % SM_REL)
    else:
        failures.extend(check_paste(sm))
    failures.extend(check_reload(files))
    failures.extend(check_editor_keys(files))
    return failures


def run_checks(repo_root):
    files = {}
    for rel in (SM_REL, MCP_REL, SKILL_REPO_REL, MCP_REPO_REL):
        try:
            files[rel] = _read(repo_root, rel)
        except OSError:
            pass
    return run_checks_map(files)


# ── selftest fixtures ────────────────────────────────────────────────────

GOOD_SM = '''package com.rikkaminis.app.ui.settings

fun sample(pasteContent: String, scope: CoroutineScope, onDismiss: () -> Unit) {
    scope.launch {
        try {
            isLoading = true
            val result = withContext(Dispatchers.IO) {
                skillRepository.importFromContent(pasteContent)
            }
            if (result != null) onDismiss()
        } catch (e: Exception) {
            errorText = "Error: ${e.message}"
        } finally {
            isLoading = false
        }
    }
}
'''

BAD_MAIN_THREAD = GOOD_SM.replace(
    """            val result = withContext(Dispatchers.IO) {
                skillRepository.importFromContent(pasteContent)
            }""",
    "            val result = skillRepository.importFromContent(pasteContent)")

BAD_DOUBLE_SITE = GOOD_SM + '''
fun sample2(pasteContent: String) {
    val result = skillRepository.importFromContent(pasteContent)
}
'''


def run_checks_text(text):
    """arms 1-2 only — kept for the two original paste fixtures."""
    return check_paste(text)


# ── deep-audit-1032 ISSUE-06 fixtures (arms 3-5) ─────────────────────────

def _sm_text(
    paste_io=True,
    reload_io=True,
    edit_name_keyed=True,
    edit_content_keys="skillId, relativePath, skill.updatedAt",
    initial_literal=True,
    extra="",
):
    paste = (
        "            val r = withContext(Dispatchers.IO) {\n"
        "                skillRepository.importFromContent(pasteContent)\n"
        "            }"
    ) if paste_io else "            val r = skillRepository.importFromContent(pasteContent)"
    reload_ = (
        "    LaunchedEffect(Unit) {\n"
        "        withContext(Dispatchers.IO) {\n"
        "            skillRepository.reloadFromDisk()\n"
        "        }\n"
        "    }"
    ) if reload_io else (
        "    LaunchedEffect(Unit) {\n"
        "        skillRepository.reloadFromDisk()\n"
        "    }"
    )
    edit_name = (
        "    var editName by remember(skill.id, skill.name) { mutableStateOf(skill.name) }"
    ) if edit_name_keyed else "    var editName by remember { mutableStateOf(skill.name) }"
    initial = (
        '    val initialContent = remember(skillId, relativePath, skill.updatedAt) { "" }'
    ) if initial_literal else (
        '    val contentInit = remember(skillId, relativePath) { "" }'
    )
    return (
        "package com.rikkaminis.app.ui.settings\n\n"
        "fun sample(pasteContent: String) {\n"
        "    scope.launch {\n" + paste + "\n    }\n"
        + reload_ + "\n"
        + edit_name + "\n"
        + initial + "\n"
        "    var editContent by remember(" + edit_content_keys + ") { mutableStateOf(initialContent) }\n"
        "    var unrelatedDialog by remember { mutableStateOf(false) }\n"
        + extra + "}\n"
    )


def _mcp_text(reload_io=True):
    body = (
        "        withContext(Dispatchers.IO) {\n"
        "            mcpRepository.reloadFromDisk()\n"
        "        }\n"
    ) if reload_io else "        mcpRepository.reloadFromDisk()\n"
    return (
        "package com.rikkaminis.app.ui.settings\n\n"
        "fun sample() {\n    LaunchedEffect(Unit) {\n" + body + "    }\n}\n"
    )


REPO_BARE = '''package com.rikkaminis.app.data.repository

private fun loadAll() {
    db.rawQuery("SELECT * FROM skills", null)
    filesDir.listFiles()
}

fun reloadFromDisk() {
    loadAll()
}
'''

REPO_WRAPS = '''package com.rikkaminis.app.data.repository

private fun loadAll() {
    withContext(Dispatchers.IO) {
        db.rawQuery("SELECT * FROM skills", null)
    }
}

fun reloadFromDisk() {
    loadAll()
}
'''

MCP_REPO_BARE = '''package com.rikkaminis.app.data.repository

fun load() {
    val parsed = readServersFile()
    _servers.value = parsed
}

fun reloadFromDisk() = load()
'''

MCP_REPO_WRAPS = '''package com.rikkaminis.app.data.repository

fun load() {
    withContext(Dispatchers.IO) {
        val parsed = readServersFile()
    }
}

fun reloadFromDisk() = load()
'''

def _files(sm=None, mcp=None, skill_repo=None, mcp_repo=None):
    return {
        SM_REL: _sm_text() if sm is None else sm,
        MCP_REL: _mcp_text() if mcp is None else mcp,
        SKILL_REPO_REL: REPO_BARE if skill_repo is None else skill_repo,
        MCP_REPO_REL: MCP_REPO_BARE if mcp_repo is None else mcp_repo,
    }


ISSUE06_FIXTURES = [
    # (label, files, expect_red, needle)
    ("clean: both screens wrapped (branch A)",
     _files(), False, None),
    ("precision: paste IO block does not cover reload",
     _files(sm=_sm_text(paste_io=True, reload_io=False)), True, "bare (Main-thread)"),
    ("MCP screen bare on Main",
     _files(mcp=_mcp_text(reload_io=False)), True, "MCPIntegrationsScreen"),
    ("alt branch: repo method wraps (call site bare)",
     _files(sm=_sm_text(reload_io=False), skill_repo=REPO_WRAPS), False, None),
    ("alt branch: MCP repo wraps via 1 delegation hop",
     _files(mcp=_mcp_text(reload_io=False), mcp_repo=MCP_REPO_WRAPS), False, None),
    ("commented-out bare reload must not count",
     _files(sm=_sm_text(extra="    // skillRepository.reloadFromDisk()\n")), False, None),
    ("paste arm not satisfied by the reload IO block",
     _files(sm=_sm_text(paste_io=False, reload_io=True)), True, "paste import not on IO"),
    ("editName un-keyed (dialog snapshot)",
     _files(sm=_sm_text(edit_name_keyed=False)), True, "`editName` remember has no key"),
    ("editName keyed with Unit (no usable key)",
     _files(sm=_sm_text(edit_name_keyed=False).replace(
         "    var editName by remember {",
         "    var editName by remember(Unit) {")), True, "empty/Unit"),
    ("editContent keys lack updatedAt",
     _files(sm=_sm_text(edit_content_keys="skillId, relativePath")),
     True, "lack `updatedAt`"),
    ("initialContent shape anchor removed",
     _files(sm=_sm_text(initial_literal=False)), True, "shape anchor"),
]


def selftest():
    ok = True
    f = run_checks_text(GOOD_SM)
    print("selftest clean tree: %s" % ("PASS" if not f else "FAIL %s" % f))
    ok &= not f

    for label, text in [
        ("bare Main-thread call", BAD_MAIN_THREAD),
        ("second call site outside IO", BAD_DOUBLE_SITE),
    ]:
        f = run_checks_text(text)
        red = len(f) > 0
        print("selftest dirty %-30s: %s %s" % (label, "RED" if red else "NOT-RED", f[:1]))
        ok &= red

    for label, files, expect_red, needle in ISSUE06_FIXTURES:
        f = run_checks_map(files)
        red = len(f) > 0
        good = (red == expect_red)
        if good and expect_red and needle:
            good = any(needle in x for x in f)
        print("selftest 1032-06 %-46s: %s%s" % (
            label, "RED" if red else "CLEAN",
            "" if good else "  UNEXPECTED %s" % f[:2]))
        ok &= good

    print(SELFTEST_MARK if ok else "SKILL_IMPORT_OFFMAIN_SELFTEST=FAIL")
    return 0 if ok else 1


def main():
    # Argv conventions in this tree: test_scan.py passes `[script, ROOT]` (real
    # tree) or `[script, --selftest]`; gate_run.sh passes
    # `[script, --selftest, ROOT]`. Scan for the flag instead of indexing argv
    # (indexing silently treated "--selftest" as the repo root → red gate).
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        sys.exit(selftest())
    repo = next((a for a in args if not a.startswith("-")), ".")
    failures = run_checks(repo)
    if failures:
        # Each finding names its own file — the check now covers two screens,
        # so a single fixed header path would point at the wrong file.
        print("FAIL skill_import_offmain_check: %d finding(s)" % len(failures))
        for f in failures:
            print("  - %s" % f)
        sys.exit(1)
    print("OK %s (7 arms: paste x2, reload-off-main x2, editor-keys x3)" % SM_REL)
    sys.exit(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""session_sheet_id_binding_check.py — deep-audit-1022 FIX-B (ISSUE-02).

Guards that ChatScreen's session-scoped subpanels receive the ACTIVE session
id (viewModel.currentSessionId — real id after draft promotion, __new__ route
arg before), not the bare `sessionId` route arg which stays on `__new__…`
after the first send and orphans every write against it.

Judged by CODE anchors (comments stripped first — never anchor on comments):

  ChatScreen.kt
    * SessionSkillsSheet(    block: `sessionId = viewModel.currentSessionId` present,
                                   bare `sessionId = sessionId` forbidden
    * SessionMcpsSheet(      block: same
    * WebPreviewBottomSheet( block: `pinSessionId = viewModel.currentSessionId` present,
                                   bare `pinSessionId = sessionId` forbidden
    * ChatHistoryDrawer(     block: `currentSessionId = viewModel.currentSessionId` present,
                                   bare `currentSessionId = sessionId` forbidden
    * `LocalMarkdownSessionId provides viewModel.currentSessionId` present;
      bare `provides sessionId` forbidden

  SessionSkillsSheet.kt
    * `remember(skills, sessionId)` present (seed must re-derive on id change);
      `remember(skills) {` (keyless on id) forbidden

  SessionMcpsSheet.kt
    * `remember(sessionId)` (fresh map per session id) present;
      keyless `overrides = remember {` forbidden
    * `LaunchedEffect(servers, sessionId)` present (seeding effect re-runs on
      id change)

Other bare `sessionId = sessionId` sites in ChatScreen (routing params of
unrelated UI children) are OUT OF SCOPE — the guard pins to the named call
blocks only, never the whole file.

Usage:
    python3 session_sheet_id_binding_check.py <repo_root>
    python3 session_sheet_id_binding_check.py --root <repo_root>
    python3 session_sheet_id_binding_check.py --selftest
"""

import os
import re
import shutil
import sys
import tempfile

SELFTEST_MARKER = "SESSION_SHEET_ID_BINDING_SELFTEST=PASS"

# ---------------------------------------------------------------------------
# Tree layout

CHAT_DIR = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat"
CHATSCREEN = os.path.join(CHAT_DIR, "ChatScreen.kt")
SKILLS_SHEET = os.path.join(CHAT_DIR, "SessionSkillsSheet.kt")
MCPS_SHEET = os.path.join(CHAT_DIR, "SessionMcpsSheet.kt")

LINE_COMMENT_RE = re.compile(r"//.*$")
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
KDOC_FENCE_RE = re.compile(r"^\s*/\*\*", re.M)


def strip_comments(text):
    """Remove block comments (incl. KDoc) and //-comments, keeping structure.

    String-aware state machine: `//` inside string literals (e.g. URLs) is
    NOT a comment and must survive — a naive per-line `//.*$` strip breaks
    string literals and unbalances the call-block paren extraction.
    """
    out = []
    i = 0
    n = len(text)
    in_string = None  # quote char or None
    while i < n:
        c = text[i]
        if in_string is not None:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == in_string:
                in_string = None
            i += 1
            continue
        if c in ('"', "'"):
            in_string = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            # line comment: keep line structure, drop content
            j = text.find("\n", i)
            if j == -1:
                j = n
            out.append("//")
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            end = n if j == -1 else j + 2
            # keep newlines inside block comments so line structure survives
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:end]))
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out)


def extract_call_block(code, call_name):
    """Return the text of `callName(` call block (balanced parens), or None."""
    m = re.search(r"\b" + re.escape(call_name) + r"\s*\(", code)
    if not m:
        return None
    i = code.index("(", m.start())
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "(":
            depth += 1
        elif code[j] == ")":
            depth -= 1
            if depth == 0:
                return code[i : j + 1]
    return None


# ---------------------------------------------------------------------------
# Anchor specs

# (call_name, required_pattern, forbidden_pattern, label)
BLOCK_SPECS = [
    ("SessionSkillsSheet", r"sessionId\s*=\s*viewModel\.currentSessionId", r"sessionId\s*=\s*sessionId\b", "skills sheet"),
    ("SessionMcpsSheet", r"sessionId\s*=\s*viewModel\.currentSessionId", r"sessionId\s*=\s*sessionId\b", "mcps sheet"),
    ("WebPreviewBottomSheet", r"pinSessionId\s*=\s*viewModel\.currentSessionId", r"pinSessionId\s*=\s*sessionId\b", "web preview pin"),
    ("ChatHistoryDrawer", r"currentSessionId\s*=\s*viewModel\.currentSessionId", r"currentSessionId\s*=\s*sessionId\b", "history drawer"),
]


def check_chatscreen(code, findings):
    code = strip_comments(code)
    for call_name, required, forbidden, label in BLOCK_SPECS:
        block = extract_call_block(code, call_name)
        if block is None:
            findings.append(f"ChatScreen.kt: call site `{call_name}(` not found")
            continue
        if not re.search(required, block):
            findings.append(
                f"ChatScreen.kt: `{call_name}(` ({label}) must pass the ACTIVE session id "
                f"(expected pattern: {required})"
            )
        if re.search(forbidden, block):
            findings.append(
                f"ChatScreen.kt: `{call_name}(` ({label}) still binds the bare route arg "
                f"`{forbidden}` — orphan key after draft promotion"
            )
    # LocalMarkdownSessionId composition local
    m = re.search(r"LocalMarkdownSessionId\s+provides\s+([A-Za-z_.]+)", code)
    if m is None:
        findings.append("ChatScreen.kt: `LocalMarkdownSessionId provides` not found")
    elif m.group(1) == "sessionId":
        findings.append(
            "ChatScreen.kt: LocalMarkdownSessionId binds the bare route arg `sessionId` — "
            "markdown media lookups resolve against the stale __new__ session dir"
        )
    elif "viewModel.currentSessionId" not in m.group(1):
        findings.append(
            f"ChatScreen.kt: LocalMarkdownSessionId provides `{m.group(1)}` — expected viewModel.currentSessionId"
        )
    return findings


def check_skills_sheet(code, findings):
    code = strip_comments(code)
    if not re.search(r"overrides\s*=\s*remember\s*\(\s*skills\s*,\s*sessionId\s*\)", code):
        findings.append(
            "SessionSkillsSheet.kt: overrides `remember` must be keyed on sessionId "
            "(expected `overrides = remember(skills, sessionId)`) — otherwise the map keeps the "
            "pre-promotion __new__ seeds"
        )
    if re.search(r"overrides\s*=\s*remember\s*\(\s*skills\s*\)\s*\{", code):
        findings.append(
            "SessionSkillsSheet.kt: `overrides = remember(skills) {` still present — keyless on sessionId"
        )
    return findings


def check_mcps_sheet(code, findings):
    code = strip_comments(code)
    if not re.search(r"overrides\s*=\s*remember\s*\(\s*sessionId\s*\)", code):
        findings.append(
            "SessionMcpsSheet.kt: overrides map must be keyed on sessionId "
            "(expected `remember(sessionId)`) — keyless map keeps __new__ seeds and "
            "the merge guard skips re-seeding same-id servers"
        )
    if re.search(r"overrides\s*=\s*remember\s*\{", code):
        findings.append("SessionMcpsSheet.kt: keyless `overrides = remember {` still present")
    if not re.search(r"LaunchedEffect\s*\(\s*servers\s*,\s*sessionId\s*\)", code):
        findings.append(
            "SessionMcpsSheet.kt: seeding effect must re-run on sessionId change "
            "(expected `LaunchedEffect(servers, sessionId)`)"
        )
    return findings


def check(root):
    findings = []
    paths = {
        CHATSCREEN: check_chatscreen,
        SKILLS_SHEET: check_skills_sheet,
        MCPS_SHEET: check_mcps_sheet,
    }
    for rel, fn in paths.items():
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            findings.append(f"missing file: {rel}")
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            fn(f.read(), findings)
    return findings


# ---------------------------------------------------------------------------
# Selftest fixtures

FIX_CHATSCEEN_OK = """\
fun ChatScreen() {
    LocalMarkdownSessionId provides viewModel.currentSessionId,
    val historyDrawerState = rememberDrawerState()
    ModalNavigationDrawer(drawerContent = {
        ChatHistoryDrawer(
            chatRepository = chatRepository,
            currentSessionId = viewModel.currentSessionId,
            draft = draftSnapshot,
        )
    })
    if (showSkillsSheet && skillRepository != null) {
        SessionSkillsSheet(
            skillRepository = skillRepository,
            sessionId = viewModel.currentSessionId,
            onDismiss = { showSkillsSheet = false },
        )
    }
    if (showMcpsSheet && mcpRepository != null) {
        SessionMcpsSheet(
            mcpRepository = mcpRepository,
            sessionId = viewModel.currentSessionId,
            onDismiss = { showMcpsSheet = false },
        )
    }
    WebPreviewBottomSheet(
        holder = holder,
        pinSessionId = viewModel.currentSessionId,
        onDismiss = onFullDismiss,
    )
    // unrelated bare pass-through that stays legal
    SomeOtherChild(sessionId = sessionId)
}
"""

FIX_CHATSCEEN_DIRTY = """\
fun ChatScreen() {
    LocalMarkdownSessionId provides sessionId,
    ModalNavigationDrawer(drawerContent = {
        ChatHistoryDrawer(
            chatRepository = chatRepository,
            currentSessionId = sessionId,
            draft = draftSnapshot,
        )
    })
    if (showSkillsSheet && skillRepository != null) {
        SessionSkillsSheet(
            skillRepository = skillRepository,
            sessionId = sessionId,
            onDismiss = { showSkillsSheet = false },
        )
    }
    if (showMcpsSheet && mcpRepository != null) {
        SessionMcpsSheet(
            mcpRepository = mcpRepository,
            sessionId = sessionId,
            onDismiss = { showMcpsSheet = false },
        )
    }
    WebPreviewBottomSheet(
        holder = holder,
        pinSessionId = sessionId,
        onDismiss = onFullDismiss,
    )
}
"""

FIX_SKILLS_OK = """\
fun SessionSkillsSheet(sessionId: String, repo: SkillRepository) {
    val skills by repo.skills.collectAsState()
    val overrides = remember(skills, sessionId) {
        mutableStateMapOf<String, Boolean>().apply {
            for (skill in skills) put(skill.id, repo.isEnabledForSession(skill.id, sessionId))
        }
    }
    // a different remember keyed only on skills stays LEGAL (search filter)
    val filteredSkills by remember(skills) {
        derivedStateOf { skills }
    }
}
"""

FIX_SKILLS_DIRTY = """\
fun SessionSkillsSheet(sessionId: String, repo: SkillRepository) {
    val skills by repo.skills.collectAsState()
    // remember(skills) { ... } — keyless on sessionId (comment trap: the
    // scanner must judge code, not this comment)
    val overrides = remember(skills) {
        mutableStateMapOf<String, Boolean>().apply {
            for (skill in skills) put(skill.id, repo.isEnabledForSession(skill.id, sessionId))
        }
    }
}
"""

FIX_MCPS_OK = """\
fun SessionMcpsSheet(sessionId: String, mcpRepository: MCPRepository) {
    val servers by mcpRepository.servers.collectAsState()
    // remember(servers) { mutableStateMapOf(...) } was the OLD form (comment trap)
    val overrides = remember(sessionId) {
        mutableStateMapOf<String, Boolean>()
    }
    LaunchedEffect(servers, sessionId) {
        for (server in servers) {
            if (server.id !in overrides) {
                overrides[server.id] = mcpRepository.isEnabledForSession(server.id, sessionId)
            }
        }
    }
}
"""

FIX_MCPS_DIRTY = """\
fun SessionMcpsSheet(sessionId: String, mcpRepository: MCPRepository) {
    val servers by mcpRepository.servers.collectAsState()
    val overrides = remember {
        mutableStateMapOf<String, Boolean>()
    }
    LaunchedEffect(servers) {
        for (server in servers) {
            if (server.id !in overrides) {
                overrides[server.id] = mcpRepository.isEnabledForSession(server.id, sessionId)
            }
        }
    }
}
"""


def write_fixture(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def make_tree(chatscreen, skills, mcps):
    root = tempfile.mkdtemp(prefix="ssib-selftest-")
    base = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat"
    write_fixture(root, os.path.join(base, "ChatScreen.kt"), chatscreen)
    write_fixture(root, os.path.join(base, "SessionSkillsSheet.kt"), skills)
    write_fixture(root, os.path.join(base, "SessionMcpsSheet.kt"), mcps)
    return root


def selftest():
    cases = [
        # (name, chatscreen, skills, mcps, expect_clean)
        ("compliant tree", FIX_CHATSCEEN_OK, FIX_SKILLS_OK, FIX_MCPS_OK, True),
        ("dirty chatscreen (bare sessionId everywhere)", FIX_CHATSCEEN_DIRTY, FIX_SKILLS_OK, FIX_MCPS_OK, False),
        ("dirty skills sheet (remember(skills))", FIX_CHATSCEEN_OK, FIX_SKILLS_DIRTY, FIX_MCPS_OK, False),
        ("dirty mcps sheet (keyless remember + effect key)", FIX_CHATSCEEN_OK, FIX_SKILLS_OK, FIX_MCPS_DIRTY, False),
        # per-violation precision: exactly one finding each
        ("only markdown site dirty", FIX_CHATSCEEN_DIRTY, FIX_SKILLS_OK, FIX_MCPS_OK, False),
    ]
    ok = True
    for name, cs, sk, mp, expect_clean in cases:
        root = make_tree(cs, sk, mp)
        try:
            findings = check(root)
            clean = not findings
            if clean != expect_clean:
                ok = False
                print(f"[selftest FAIL] {name}: expected {'clean' if expect_clean else 'findings'}, got: {findings}")
            else:
                print(f"[selftest ok] {name}: {'clean' if clean else f'{len(findings)} finding(s)'}")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # precision: dirty tree findings must name the right anchors
    root = make_tree(FIX_CHATSCEEN_DIRTY, FIX_SKILLS_OK, FIX_MCPS_OK)
    try:
        findings = check(root)
        joined = " | ".join(findings)
        required_hits = [
            "SessionSkillsSheet",
            "SessionMcpsSheet",
            "WebPreviewBottomSheet",
            "ChatHistoryDrawer",
            "LocalMarkdownSessionId",
        ]
        for hit in required_hits:
            if hit not in joined:
                ok = False
                print(f"[selftest FAIL] precision: missing anchor `{hit}` in findings")
        print(f"[selftest ok] precision: all 5 anchors flagged ({len(findings)} findings)")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(SELFTEST_MARKER if ok else "SESSION_SHEET_ID_BINDING_SELFTEST=FAIL")
    return 0 if ok else 1


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    root = None
    if "--root" in args:
        i = args.index("--root")
        if i + 1 >= len(args):
            print("usage: --root <repo_root>", file=sys.stderr)
            return 2
        root = args[i + 1]
    elif args:
        root = args[0]
    else:
        root = os.getcwd()
    if not os.path.isdir(root):
        print(f"repo root not found: {root}", file=sys.stderr)
        return 2
    findings = check(root)
    if findings:
        print(f"session_sheet_id_binding_check: FAIL ({len(findings)} finding(s))")
        for f in findings:
            print(f"  - {f}")
        return 1
    print("session_sheet_id_binding_check: OK (5 call sites active-bound, both sheets id-keyed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    * the seed must MERGE (mirrors SessionMcpsSheet):
      `remember(sessionId)` (fresh map per session id) present +
      `LaunchedEffect(skills, sessionId)` present (re-runs on id change);
      the rebuild shape `overrides = remember(skills…` (whole map re-derived
      from the live list — one SQLite read per skill, per emission, inside
      composition) and the keyless `overrides = remember {` are both forbidden

  [da1034-b3] Until this revision the SessionSkillsSheet criterion was the
  REBUILD shape (`remember(skills, sessionId) { … isEnabledForSession … }`) —
  the gate itself pinned the defect in place and made the two sheets
  contradictory (the Mcps criterion already demanded the incremental merge).
  Both sheets are now judged by the same incremental-merge shape.

  SessionMcpsSheet.kt
    * `remember(sessionId)` (fresh map per session id) present;
      keyless `overrides = remember {` forbidden
    * `LaunchedEffect(servers, sessionId)` present (seeding effect re-runs on
      id change)

  [da1032-A] ISSUE-02 — `SessionEditSheet` is host-agnostic: its Regenerate
  button and its live row are both defaulted parameters, so a host that omits
  them still compiles and the button silently no-ops. Two more blocks:

  ChatScreen.kt
    * SessionEditSheet(  block: `onRegenerate =` present AND `liveSession =`
                                present (never rely on the defaults)

  SessionsShared.kt (inside the `internal fun SessionEditSheet(` body only)
    * `var title by remember(<keys containing session.id>)` present,
      keyless `var title by remember {` forbidden
    * `var selectedCategory by remember(<keys containing session.id>)` present,
      keyless `var selectedCategory by remember {` forbidden
    * `LaunchedEffect(<keys containing liveSession.id>)` present (the seeding
      effect must re-run when the sheet is re-pointed at another row)

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
SESSIONS_DIR = "src/android/app/src/main/java/com/rikkaminis/app/ui/sessions"
SESSIONS_SHARED = os.path.join(SESSIONS_DIR, "SessionsShared.kt")

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


def _match_forward(code, start, opener, closer):
    """Index of the token that closes the group opened at `start`, or -1."""
    depth = 0
    for j in range(start, len(code)):
        if code[j] == opener:
            depth += 1
        elif code[j] == closer:
            depth -= 1
            if depth == 0:
                return j
    return -1


def extract_block_fun_body(code, signature_fragment):
    """Body of a BLOCK-body function, scoped so the file's other composables
    cannot satisfy (or break) its anchors.

    Parameters are paren-matched first and only then the body `{` is taken —
    a bare brace-pairing search would trip over `onRegenerate: () -> Unit = {}`
    inside the parameter list. Expressions bodies (`fun f(...) = ...`) are NOT
    supported: returning None makes the caller fail closed rather than judge
    the wrong region.
    """
    m = re.search(re.escape(signature_fragment), code)
    if not m:
        return None
    open_paren = code.find("(", m.start())
    if open_paren < 0:
        return None
    close_paren = _match_forward(code, open_paren, "(", ")")
    if close_paren < 0:
        return None
    open_brace = code.find("{", close_paren)
    if open_brace < 0:
        return None
    # `{` must be the body opener, not a stray brace in a return type/annotation
    if "fun " in code[close_paren:open_brace]:
        return None
    close_brace = _match_forward(code, open_brace, "{", "}")
    if close_brace < 0:
        return None
    return code[open_brace : close_brace + 1]


# ---------------------------------------------------------------------------
# Anchor specs

# (call_name, required_pattern, forbidden_pattern, label)
BLOCK_SPECS = [
    ("SessionSkillsSheet", r"sessionId\s*=\s*viewModel\.currentSessionId", r"sessionId\s*=\s*sessionId\b", "skills sheet"),
    ("SessionMcpsSheet", r"sessionId\s*=\s*viewModel\.currentSessionId", r"sessionId\s*=\s*sessionId\b", "mcps sheet"),
    ("WebPreviewBottomSheet", r"pinSessionId\s*=\s*viewModel\.currentSessionId", r"pinSessionId\s*=\s*sessionId\b", "web preview pin"),
    ("ChatHistoryDrawer", r"currentSessionId\s*=\s*viewModel\.currentSessionId", r"currentSessionId\s*=\s*sessionId\b", "history drawer"),
]

# [da1032-A] ISSUE-02: `SessionEditSheet` is host-agnostic — `onRegenerate`
# defaults to `{}` and `liveSession` defaults to the (stale) `session`
# snapshot, so omitting either compiles clean and disables the feature
# silently. Pin both arguments at the call site.
EDIT_SHEET_CALL = "SessionEditSheet"
EDIT_SHEET_SPECS = [
    (EDIT_SHEET_CALL, [r"onRegenerate\s*=", r"liveSession\s*="], "session edit sheet"),
]

EDIT_SHEET_FUN_SIG = "internal fun SessionEditSheet"

# Keyed-remember key lists: the id must appear SOMEWHERE among the keys (the
# decision point is "re-seeds when the sheet is re-pointed"), so extra keys
# stay legal.
TITLE_KEYED_RE = r"\bvar\s+title\s+by\s+remember\s*\([^)]*\bsession\.id\b"
TITLE_KEYLESS_RE = r"\bvar\s+title\s+by\s+remember\s*\{"
CATEGORY_KEYED_RE = r"\bvar\s+selectedCategory\s+by\s+remember\s*\([^)]*\bsession\.id\b"
CATEGORY_KEYLESS_RE = r"\bvar\s+selectedCategory\s+by\s+remember\s*\{"
LIVE_SEED_EFFECT_RE = r"LaunchedEffect\s*\([^)]*\bliveSession\.id\b"


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
    # [da1032-A] SessionEditSheet call block
    for call_name, required_patterns, label in EDIT_SHEET_SPECS:
        block = extract_call_block(code, call_name)
        if block is None:
            findings.append(f"ChatScreen.kt: call site `{call_name}(` not found")
            continue
        for required in required_patterns:
            if not re.search(required, block):
                arg = required.replace(r"\s*=", "").replace("\\s*=", "")
                findings.append(
                    f"ChatScreen.kt: `{call_name}(` ({label}) must pass `{arg}` explicitly — "
                    f"it is a defaulted parameter, so omitting it compiles clean and silently "
                    f"disables the control it feeds"
                )
    return findings


def check_sessions_shared(code, findings):
    """[da1032-A] The edit-sheet seed state must be keyed on the row id.

    Scoped to the `internal fun SessionEditSheet` body: other composables in
    this file legitimately hold keyless `var … by remember` state and must not
    be judged.
    """
    code = strip_comments(code)
    body = extract_block_fun_body(code, EDIT_SHEET_FUN_SIG)
    if body is None:
        findings.append(
            "SessionsShared.kt: `internal fun SessionEditSheet(` body not found — "
            "cannot verify the edit-state keys"
        )
        return findings
    if not re.search(TITLE_KEYED_RE, body):
        findings.append(
            "SessionsShared.kt: `title` must be seeded by "
            "`var title by remember(<keys with session.id>)` — otherwise the field keeps "
            "the previous session's text when the sheet is re-pointed"
        )
    if re.search(TITLE_KEYLESS_RE, body):
        findings.append("SessionsShared.kt: keyless `var title by remember {` still present")
    if not re.search(CATEGORY_KEYED_RE, body):
        findings.append(
            "SessionsShared.kt: `selectedCategory` must be seeded by "
            "`var selectedCategory by remember(<keys with session.id>)`"
        )
    if re.search(CATEGORY_KEYLESS_RE, body):
        findings.append(
            "SessionsShared.kt: keyless `var selectedCategory by remember {` still present"
        )
    if not re.search(LIVE_SEED_EFFECT_RE, body):
        findings.append(
            "SessionsShared.kt: the live-row seeding effect must key on `liveSession.id` "
            "(expected `LaunchedEffect(liveSession.id, …)`) — otherwise a re-pointed sheet "
            "never re-seeds from the new row"
        )
    return findings


def check_skills_sheet(code, findings):
    """[da1034-b3] Same incremental-merge criterion as [check_mcps_sheet].

    The previous criterion required the REBUILD shape
    (`overrides = remember(skills, sessionId) { … }`), so this gate held the
    defect in place and directly contradicted the Mcps criterion below: the
    rebuild re-derived the whole map from the live list on every emission —
    one `isEnabledForSession` SQLite read per skill, inside composition — and
    threw away any toggle the user had already flipped in the open sheet. The
    two sheets are checked by the same shape now.
    """
    code = strip_comments(code)
    if not re.search(r"overrides\s*=\s*remember\s*\(\s*sessionId\s*\)", code):
        findings.append(
            "SessionSkillsSheet.kt: overrides map must be keyed on sessionId "
            "(expected `overrides = remember(sessionId)`) — a map rebuilt from the live skills "
            "list keeps the pre-promotion __new__ seeds and re-runs the seed per emission"
        )
    if re.search(r"overrides\s*=\s*remember\s*\(\s*skills", code):
        findings.append(
            "SessionSkillsSheet.kt: `overrides = remember(skills…)` full rebuild still present — "
            "every skills StateFlow emission re-derives the map and runs one isEnabledForSession "
            "SQLite read per skill inside composition; seed by merging into a sessionId-keyed map "
            "instead (mirrors SessionMcpsSheet)"
        )
    if re.search(r"overrides\s*=\s*remember\s*\{", code):
        findings.append("SessionSkillsSheet.kt: keyless `overrides = remember {` still present")
    if not re.search(r"LaunchedEffect\s*\(\s*skills\s*,\s*sessionId\s*\)", code):
        findings.append(
            "SessionSkillsSheet.kt: seeding effect must re-run on sessionId change "
            "(expected `LaunchedEffect(skills, sessionId)`) — otherwise a promoted draft keeps "
            "the __new__ seeds and never re-derives from the real id"
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
        SESSIONS_SHARED: check_sessions_shared,
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
    // [da1032-A] the edit sheet host block: both defaulted args passed
    // explicitly, and deliberately NOT in declaration order (the guard is on
    // the decision point, not the argument order).
    editingSession?.let { session ->
        com.rikkaminis.app.ui.sessions.SessionEditSheet(
            session = session,
            liveSession = liveSessions.firstOrNull { it.id == session.id } ?: session,
            onDismiss = { editingSession = null },
            onSave = { t, c -> viewModel.updateTitleAndCategory(t, c) },
            onRegenerate = { viewModel.regenerateSessionTitle() },
        )
    }
    // unrelated bare pass-through that stays legal
    SomeOtherChild(sessionId = sessionId)
}
"""

# [da1032-A] the pre-fix call site: compiles clean, button dead.
FIX_CHATSCEEN_EDITSHEET_MISSING = FIX_CHATSCEEN_OK.replace(
    """            liveSession = liveSessions.firstOrNull { it.id == session.id } ?: session,
            onDismiss = { editingSession = null },
            onSave = { t, c -> viewModel.updateTitleAndCategory(t, c) },
            onRegenerate = { viewModel.regenerateSessionTitle() },
""",
    """            onDismiss = { editingSession = null },
            onSave = { t, c -> viewModel.updateTitleAndCategory(t, c) },
""",
)

# half-wired: the button works, the field never refreshes.
FIX_CHATSCEEN_EDITSHEET_NO_LIVE = FIX_CHATSCEEN_OK.replace(
    "            liveSession = liveSessions.firstOrNull { it.id == session.id } ?: session,\n",
    "",
)

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

# [da1034-b3] OK = incremental merge into a sessionId-keyed map (mirrors
# FIX_MCPS_OK). The comment names the old shape on purpose: the scanner must
# judge code, never comments.
FIX_SKILLS_OK = """\
fun SessionSkillsSheet(sessionId: String, repo: SkillRepository) {
    val skills by repo.skills.collectAsState()
    // `remember(skills, sessionId) { … }` was the OLD rebuild form (comment trap)
    val overrides = remember(sessionId) {
        mutableStateMapOf<String, Boolean>()
    }
    LaunchedEffect(skills, sessionId) {
        mergeSessionOverrides(
            liveIds = skills.map { it.id },
            overrides = overrides,
            lookup = { repo.isEnabledForSession(it, sessionId) },
        )
    }
    // a different remember keyed only on skills stays LEGAL (search filter)
    val filteredSkills by remember(skills) {
        derivedStateOf { skills }
    }
}
"""

# [da1034-b3] the pre-fix shape — this was the gate's OK fixture until the
# criterion was corrected.
FIX_SKILLS_DIRTY = """\
fun SessionSkillsSheet(sessionId: String, repo: SkillRepository) {
    val skills by repo.skills.collectAsState()
    val overrides = remember(skills, sessionId) {
        mutableStateMapOf<String, Boolean>().apply {
            for (skill in skills) put(skill.id, repo.isEnabledForSession(skill.id, sessionId))
        }
    }
}
"""

FIX_SKILLS_KEYLESS = """\
fun SessionSkillsSheet(sessionId: String, repo: SkillRepository) {
    val skills by repo.skills.collectAsState()
    val overrides = remember {
        mutableStateMapOf<String, Boolean>()
    }
    LaunchedEffect(skills, sessionId) {
        for (skill in skills) {
            if (skill.id !in overrides) {
                overrides[skill.id] = repo.isEnabledForSession(skill.id, sessionId)
            }
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


FIX_SESSIONS_OK = """\
@Composable
internal fun SessionEditSheet(
    session: ChatSessionEntity,
    onDismiss: () -> Unit,
    onSave: (title: String, category: String?) -> Unit,
    liveSession: ChatSessionEntity = session,
    isRegenerating: Boolean = false,
    onRegenerate: () -> Unit = {},
) {
    // extra keys are legal: the guard asks whether the key list carries the id,
    // not whether the list is exactly (session.id)
    var title by remember(session.id, session.title) { mutableStateOf(session.title ?: "") }
    var selectedCategory by remember(session.id) { mutableStateOf(session.category) }
    val defaultSessionTitle = stringResource(R.string.session_new_chat_default)

    LaunchedEffect(liveSession.id, liveSession.title, liveSession.category) {
        liveSession.title?.let { title = it }
        selectedCategory = liveSession.category
    }

    OutlinedButton(onClick = onRegenerate, enabled = !isRegenerating) {
        Text(defaultSessionTitle)
    }
}

// PRECISION: a different composable in the SAME file holding keyless seed
// state with the same names — legal, and the guard is scoped to the
// SessionEditSheet body so it must not be judged.
@Composable
internal fun SessionListRow(session: ChatSessionEntity) {
    var title by remember { mutableStateOf(session.title ?: "") }
    var selectedCategory by remember { mutableStateOf(session.category) }
    Text(title ?: "", selectedCategory)
}
"""

# [da1032-A] the pre-fix shape: unkeyed seeds + an effect that cannot re-seed
# when the sheet is re-pointed at another row.
FIX_SESSIONS_DIRTY = """\
@Composable
internal fun SessionEditSheet(
    session: ChatSessionEntity,
    liveSession: ChatSessionEntity = session,
    onRegenerate: () -> Unit = {},
) {
    var title by remember { mutableStateOf(session.title ?: "") }
    var selectedCategory by remember { mutableStateOf(session.category) }

    LaunchedEffect(liveSession.title, liveSession.category) {
        liveSession.title?.let { title = it }
        selectedCategory = liveSession.category
    }

    OutlinedButton(onClick = onRegenerate) { Text(title) }
}
"""


def make_tree(chatscreen, skills, mcps, sessions=FIX_SESSIONS_OK):
    root = tempfile.mkdtemp(prefix="ssib-selftest-")
    base = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat"
    write_fixture(root, os.path.join(base, "ChatScreen.kt"), chatscreen)
    write_fixture(root, os.path.join(base, "SessionSkillsSheet.kt"), skills)
    write_fixture(root, os.path.join(base, "SessionMcpsSheet.kt"), mcps)
    write_fixture(root, os.path.join(SESSIONS_DIR, "SessionsShared.kt"), sessions)
    return root


def selftest():
    cases = [
        # (name, chatscreen, skills, mcps, expect_clean)
        ("compliant tree", FIX_CHATSCEEN_OK, FIX_SKILLS_OK, FIX_MCPS_OK, True),
        ("dirty chatscreen (bare sessionId everywhere)", FIX_CHATSCEEN_DIRTY, FIX_SKILLS_OK, FIX_MCPS_OK, False),
        # [da1034-b3] the skills arms flipped direction: the rebuild shape is
        # now the dirty one, the merge shape the clean one.
        ("dirty skills sheet (rebuild on skills+sessionId)", FIX_CHATSCEEN_OK, FIX_SKILLS_DIRTY, FIX_MCPS_OK, False),
        ("dirty skills sheet (keyless remember)", FIX_CHATSCEEN_OK, FIX_SKILLS_KEYLESS, FIX_MCPS_OK, False),
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

    # [da1032-A] the new condition, three shapes + one precision arm
    edit_cases = [
        ("edit sheet call missing BOTH defaulted args",
         FIX_CHATSCEEN_EDITSHEET_MISSING, FIX_SESSIONS_OK, False),
        ("edit sheet call missing liveSession only",
         FIX_CHATSCEEN_EDITSHEET_NO_LIVE, FIX_SESSIONS_OK, False),
        ("sessions shared unkeyed seeds + effect without liveSession.id",
         FIX_CHATSCEEN_OK, FIX_SESSIONS_DIRTY, False),
        ("sessions shared keyed (precision: extra keys stay legal)",
         FIX_CHATSCEEN_OK, FIX_SESSIONS_OK, True),
    ]
    for name, cs, ss, expect_clean in edit_cases:
        root = make_tree(cs, FIX_SKILLS_OK, FIX_MCPS_OK, ss)
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

    # precision: the two new shapes must name their own anchor, and only theirs.
    for name, cs, ss, must_mention, must_not_mention, want_count in (
        ("edit sheet both args missing", FIX_CHATSCEEN_EDITSHEET_MISSING, FIX_SESSIONS_OK,
         ["SessionEditSheet", "onRegenerate", "liveSession"], [], 2),
        ("edit sheet liveSession missing", FIX_CHATSCEEN_EDITSHEET_NO_LIVE, FIX_SESSIONS_OK,
         ["SessionEditSheet", "liveSession"], ["onRegenerate"], 1),
        ("sessions shared unkeyed", FIX_CHATSCEEN_OK, FIX_SESSIONS_DIRTY,
         ["liveSession.id", "var title by remember", "var selectedCategory by remember"], [], 5),
    ):
        root = make_tree(cs, FIX_SKILLS_OK, FIX_MCPS_OK, ss)
        try:
            findings = check(root)
            joined = " | ".join(findings)
            for hit in must_mention:
                if hit not in joined:
                    ok = False
                    print(f"[selftest FAIL] precision {name}: missing `{hit}` in findings")
            for hit in must_not_mention:
                if hit in joined:
                    ok = False
                    print(f"[selftest FAIL] precision {name}: unexpected `{hit}` in findings")
            if len(findings) != want_count:
                ok = False
                print(f"[selftest FAIL] precision {name}: expected {want_count} finding(s), got {len(findings)}: {findings}")
            else:
                print(f"[selftest ok] precision {name}: exactly {want_count} finding(s), anchors named")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # [da1034-b3] the skills arms must name their own anchors and only theirs;
    # the OK fixture's comment names the old shape (comment trap) and must stay clean.
    for name, sk, must_mention, must_not_mention, want_count in (
        ("skills rebuild shape", FIX_SKILLS_DIRTY,
         ["SessionSkillsSheet", "remember(skills", "LaunchedEffect(skills, sessionId)"],
         ["keyless"], 3),
        ("skills keyless shape", FIX_SKILLS_KEYLESS,
         ["SessionSkillsSheet", "keyless"],
         ["remember(skills", "LaunchedEffect(skills, sessionId)"], 2),
    ):
        root = make_tree(FIX_CHATSCEEN_OK, sk, FIX_MCPS_OK)
        try:
            findings = check(root)
            joined = " | ".join(findings)
            for hit in must_mention:
                if hit not in joined:
                    ok = False
                    print(f"[selftest FAIL] precision {name}: missing `{hit}` in findings")
            for hit in must_not_mention:
                if hit in joined:
                    ok = False
                    print(f"[selftest FAIL] precision {name}: unexpected `{hit}` in findings")
            if len(findings) != want_count:
                ok = False
                print(f"[selftest FAIL] precision {name}: expected {want_count} finding(s), got {len(findings)}: {findings}")
            else:
                print(f"[selftest ok] precision {name}: exactly {want_count} finding(s), anchors named")
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
    print("session_sheet_id_binding_check: OK (4 session-scoped call blocks + LocalMarkdownSessionId "
          "active-bound, SessionEditSheet call args wired, both sheets id-keyed with incremental "
          "seeding, edit-sheet keys id-keyed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

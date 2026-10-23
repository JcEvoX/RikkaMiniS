#!/usr/bin/env python3
"""
Gate: FIX-A (deep-audit-1021 ISSUE-01 + ISSUE-10) — skill restore id threading
+ cross-db session cleanup closure.

ISSUE-01 (SkillRepository.kt)
  `add()` used to recompute `slugify(name)` for its landing id even when the
  caller had ALREADY resolved the id through SkillRestorePolicy.resolveImportId
  (importFromContent's preferredId parameter, installBundledSkill's assets
  dirName). A renamed skill (pure-CJK name -> blank slug -> add returned null)
  was silently lost, or landed under a fresh slug while its backup payload and
  session_skill_overrides still pointed at the old id (dir split). The rule:
  `add()`'s first assignment resolves the id via
  SkillRestorePolicy.resolveImportId(preferredId, slugify(name)), and both
  production callers pass their resolved id through.

ISSUE-10 (ChatRepository.kt + MinisApp.kt)
  SkillRepository.clearSessionOverrides / MCPRepository.clearSessionOverrides
  existed with ZERO production callers — a deleted session's "Allow in this
  session" skill/mcp toggles leaked into every later session that reused the
  id. The rule: ChatRepository exposes an onSessionDeleted hook that EVERY
  function removing `sessions` rows reaches AFTER the row is gone (user delete,
  row-only delete and — since dv21-a2, deep-audit-1041 A-02 — the startup
  empty-session sweep `deleteEmptySessions`), and MinisApp injects a callback
  that reaps both repositories (with skill/mcp constructed BEFORE
  chatRepository so the callback references the real instances). The entry
  point set is DERIVED from the tree: the sweep was the entry point that a
  hard-coded two-entry list made invisible.

Checks assert PRESENCE of the routing rather than the absence of a spelling,
so a refactor that keeps the contract is a deliberate edit to this gate rather
than a silent pass.

I04 (SkillRepository.kt, deep-audit-1029)
  `recordSkillUse` computed its mapped list under `synchronized(addLock)` but
  published `_skills.value = next` AFTER the release — while the KDoc directly
  above claimed the update happened under the same lock as `add`. Two
  concurrent calls (parallel tool executions on Dispatchers.IO) both read the
  pre-publish snapshot, so memory drifted one behind the atomic SQL increment;
  a concurrent `add()` could be erased outright by the stale publish. The rule:
  every `_skills.value =` inside `recordSkillUse` sits inside the
  `synchronized(addLock)` block (criterion on lock ownership, not on the shape
  of the right-hand side).

I01/I02 (deep-audit-1030, FIX-A)
  `ensureSession()`'s `if (isDraft)` block already re-points the skill / mcp
  overrides and the BrowserTabPool onto the real session id, but had no hook
  for the pinned WebApp shortcuts: their `scope_context` / `source_session_id`
  were frozen at pin time (`__new__<uuid>`), and `migrateDraftResources` deletes
  the draft directory the resolver reads through — the launcher icon reports
  "Source Missing" forever. Symmetrically, MinisApp's `onSessionDeleted` hook
  reaps the skill / mcp overrides but left the session-list badge queue behind,
  so a deleted session's PAUSED badge re-surfaced on whatever session reused
  the id. Rules: the draft block calls `renameSessionShortcuts(`, and the
  `onSessionDeleted` lambda body calls `SessionBadgeStore.clear(`.

Usage: python3 scripts/scan/skill_restore_session_cleanup_check.py [repo_root]
       python3 scripts/scan/skill_restore_session_cleanup_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import os
import re
import sys

SKILL_REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/SkillRepository.kt"
CHAT_REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/ChatRepository.kt"
MINIS_APP_REL = "src/android/app/src/main/java/com/rikkaminis/app/MinisApp.kt"
CHAT_ENTRY_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatSessionEntryExt.kt"
DRAWER_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatHistoryDrawer.kt"

# [dv24-b3 / deep-audit-1044 B-06] Draft promotion must persist the BODY, not
# just a 50-char title prefix, and the drawer must treat a preview as content.
PROMOTE_DECL = "fun ChatViewModel.promoteDraftIfNeeded("
ENSURE_CALL = "ensureSession("
# The two single-source writers that associate text with a session durably.
# Both live in ChatRepository and both refresh `last_message` + `updated_at`.
DURABLE_WRITES = ("appendMessage(", "updateSessionPreview(")
# `_inputText.value = "<literal>` — assigning a CONSTANT to the composer. In
# this function the only such constant is the blank, i.e. "destroy the in-memory
# copy"; a variable (`= text`) keeps it. Anchored on that decision, not on the
# spelling of the blank.
COMPOSER_LITERAL_RE = re.compile(r"_inputText\.value\s*=\s*[\"']")
DRAWER_VISIBLE_DECL = "fun isDrawerRowVisible("
DRAWER_VISIBLE_CALL = "isDrawerRowVisible("
DRAWER_FILTER_RE = re.compile(r"sessions\s*\.\s*filter\s*\{[^}]*isDrawerRowVisible\(")
# Blankness test on the preview column (any of these is the "no content" arm).
PREVIEW_BLANK_CHECKS = ("isNullOrBlank", "isNotBlank", "isNullOrEmpty", "isNotEmpty")
TOP_LEVEL_BOUNDS = ("\nfun ", "\ninternal fun ", "\nprivate fun ", "\n@Composable")

ADD_DECL = "fun add("
ADD_SIG_END = "): Skill? {"
RESOLVE_CALL = "SkillRestorePolicy.resolveImportId("
RETURN_ADD = "return add("
PREFERRED_ID = "preferredId ="
BUNDLED_DECL = "private fun installBundledSkill(dirName: String) {"
BUNDLED_NEXT = "private fun isAdoptableBundledRow("
# [dv16-A05] sparse-clear arm
OVERRIDE_DECL = "fun setSessionOverride(sessionId: String, skillId: String, enabled: Boolean)"
OVERRIDE_NEXT = "fun clearSessionOverrides("
DELETE_OVERRIDE = "DELETE FROM session_skill_overrides WHERE session_id=? AND skill_id=?"
SHARED_DECISION = "shouldDeleteSessionSkillOverride("
GLOBAL_READ = "_skills.value.find { it.id == skillId }?.isEnabled"
DELETE_SESSION = "suspend fun deleteSession(id: String): SessionFileStore.DeleteResult? {"
DELETE_ROW_ONLY = "suspend fun deleteSessionRowOnly(id: String) {"
# [dv21-a2] Third entry point: the startup empty-session sweep.
DELETE_EMPTY = "suspend fun deleteEmptySessions("
INVOKE_HOOK = "onSessionDeleted?.invoke("
# [dv21-a2] A function whose body drops `sessions` rows IS a delete entry point.
# The gate derives the set from the tree instead of trusting this list, so a
# fourth entry point that skips the cleanup goes red.
SESSION_ROW_DELETES = ("dao.deleteSession(", "dao.deleteEmptySessions(")
FN_DECL_RE = re.compile(r"\bfun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
CHATREPO_NEW = "chatRepository = ChatRepository("
SKILL_NEW = "skillRepository = SkillRepository("
MCP_NEW = "mcpRepository = MCPRepository("


RECORD_DECL = "fun recordSkillUse("
RECORD_LOCK = "synchronized(addLock)"
SKILLS_ASSIGN = "_skills.value ="

# [da1030-A ISSUE-01]
ENSURE_DECL = "internal suspend fun ChatViewModel.ensureSession("
DRAFT_IF = "if (isDraft) {"
RENAME_SHORTCUTS = "renameSessionShortcuts("
ON_SESSION_DELETED = "onSessionDeleted = { sid ->"
BADGE_CLEAR = "SessionBadgeStore.clear("


def lines_of(src, needle):
    return [i + 1 for i, line in enumerate(src.split("\n")) if needle in line]


def strip_literals(src):
    """Empty out string bodies + drop comments so brace matching can't be fooled.

    Needed because the KDoc for `recordSkillUse` quotes `synchronized(addLock)`
    and `_skills.value` verbatim, and because SQL literals could carry braces.
    Returns None when a literal/comment never terminates (caller treats that as
    ROT rather than silently matching garbage).
    """
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith('"""', i):
            end = src.find('"""', i + 3)
            if end < 0:
                return None
            out.append("~")
            i = end + 3
            continue
        c = src[i]
        if c == '"':
            i += 1
            while i < n and src[i] != '"':
                i += 2 if src[i] == "\\" else 1
            if i >= n:
                return None
            out.append("~")
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                return None
            i = j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def brace_block_end(code, open_idx):
    """Index just past the '}' matching the '{' at `open_idx`, or -1."""
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def check_record_skill_use_lock(src):
    """Arm 4 [da1029-C / I04]: recordSkillUse's StateFlow write-back must be
    INSIDE the `synchronized(addLock)` block, and nowhere outside it.

    The pre-fix shape computed `mapped` under the lock but published it after
    the release; the comment above the function claimed the opposite. Two
    concurrent calls then both read the pre-publish snapshot (one increment
    lost, memory drifts behind the DB) and a concurrent `add()` could be
    overwritten by the stale publish.

    The criterion is on **lock ownership**, deliberately not on the shape of
    the right-hand side: `_skills.value = <any expression>` inside the block is
    accepted (mapping inline, or via an intermediate `val`).
    """
    v = []
    code = strip_literals(src)
    if code is None:
        v.append("SkillRepository.kt: unterminated string/comment literal — cannot brace-match "
                 "recordSkillUse (ROT — update this gate)")
        return v
    start = code.find(RECORD_DECL)
    if start < 0:
        v.append("SkillRepository.kt: '%s' not found (ROT — update this gate)" % RECORD_DECL)
        return v
    open_brace = code.find("{", start)
    if open_brace < 0:
        v.append("SkillRepository.kt: recordSkillUse has no body brace (ROT — update this gate)")
        return v
    end = brace_block_end(code, open_brace)
    if end < 0:
        v.append("SkillRepository.kt: recordSkillUse body braces do not balance (ROT — update this gate)")
        return v
    body = code[open_brace:end + 1]

    lock_at = body.find(RECORD_LOCK)
    if lock_at < 0:
        v.append("SkillRepository.kt: recordSkillUse has no '%s' — the read-modify-write "
                 "over _skills.value is back outside the add lock" % RECORD_LOCK)
        return v
    lock_open = body.find("{", lock_at)
    lock_end = brace_block_end(body, lock_open) if lock_open >= 0 else -1
    if lock_end < 0:
        v.append("SkillRepository.kt: recordSkillUse's synchronized block braces do not balance "
                 "(ROT — update this gate)")
        return v

    assigns = [m.start() for m in re.finditer(re.escape(SKILLS_ASSIGN), body)]
    if not assigns:
        v.append("SkillRepository.kt: recordSkillUse never publishes '%s' — the use_count bump "
                 "would never reach the in-memory mirror" % SKILLS_ASSIGN)
        return v
    inside = [p for p in assigns if lock_open < p < lock_end]
    outside = [p for p in assigns if not (lock_open < p < lock_end)]
    if not inside:
        v.append("SkillRepository.kt: recordSkillUse computes under the lock but publishes "
                 "'%s' only OUTSIDE it — a concurrent recordSkillUse loses one increment and a "
                 "concurrent add() can be overwritten by the stale publish" % SKILLS_ASSIGN)
    for p in outside:
        v.append("SkillRepository.kt: '%s' at body offset %d is out of the '%s' block — "
                 "write-back must share the lock with add()" % (SKILLS_ASSIGN, p, RECORD_LOCK))
    return v


def slice_between(src, start_marker, end_marker, label, violations):
    start = src.find(start_marker)
    if start < 0:
        violations.append("%s: '%s' not found (ROT — update this gate)" % (label, start_marker))
        return None
    end = src.find(end_marker, start + len(start_marker))
    if end < 0:
        violations.append("%s: '%s' not found after %s (ROT — update this gate)" % (label, end_marker, start_marker))
        return None
    return src[start:end]


def check_skill_repo(src):
    v = []

    # Arm 4 [da1029-C / I04]: recordSkillUse's write-back shares add()'s lock.
    v.extend(check_record_skill_use_lock(src))

    # Arm 8 [dv16-A05]: setSessionOverride sparse-clears when the requested
    # value equals the CURRENT global default; the decision lives in the
    # shared pure function shouldDeleteSessionSkillOverride (the MCP twin's
    # shape). A pinned row keeps the session stuck at the old value forever.
    override_slice = slice_between(src, OVERRIDE_DECL, OVERRIDE_NEXT, "SkillRepository.kt", v)
    if override_slice is not None:
        if DELETE_OVERRIDE not in override_slice:
            v.append(
                "SkillRepository.kt: setSessionOverride does not DELETE the override row when the "
                "value equals the global default — a pinned row keeps the session stuck at the old "
                "value after the user toggles back (sparse-clear is the MCP twin's shape)"
            )
        if SHARED_DECISION not in override_slice:
            v.append(
                "SkillRepository.kt: setSessionOverride does not consult the shared decision "
                "shouldDeleteSessionSkillOverride — a second decision copy is back"
            )
        if GLOBAL_READ not in override_slice:
            v.append(
                "SkillRepository.kt: setSessionOverride does not read the CURRENT global default "
                "before the branch — the row would be pinned even when it matches"
            )

    # Arm 1: add()'s first assignment must resolve the id through the policy.
    add_slice = slice_between(src, ADD_DECL, ADD_SIG_END, "SkillRepository.kt", v)
    if add_slice is not None:
        body = src[src.find(ADD_SIG_END, src.find(ADD_DECL)):]
        first_assign = body.find("val id =")
        if first_assign < 0:
            v.append("SkillRepository.kt: add() has no 'val id =' assignment (ROT — update this gate)")
        else:
            line_start = body.rfind("\n", 0, first_assign) + 1
            line_end = body.find("\n", first_assign)
            stmt = body[line_start:line_end]
            if RESOLVE_CALL not in stmt:
                v.append(
                    "SkillRepository.kt: add() first id assignment does not resolve "
                    "via '%s' (raw slugify recompute is back)" % RESOLVE_CALL
                )
            if "preferredId" not in stmt:
                v.append("SkillRepository.kt: add() id assignment does not consult preferredId")

    # Arm 2: importFromContent's `return add(` block must pass preferredId.
    ifc = slice_between(src, "fun importFromContent(", "fun skillMdPath", "SkillRepository.kt", v)
    if ifc is not None:
        ra = ifc.find(RETURN_ADD)
        if ra < 0:
            v.append("SkillRepository.kt: importFromContent has no '%s' (ROT — update this gate)" % RETURN_ADD)
        else:
            block = ifc[ra:ifc.find(")\n", ra) if ifc.find(")\n", ra) > 0 else len(ifc)]
            if PREFERRED_ID not in block:
                v.append(
                    "SkillRepository.kt: importFromContent's return add() does not pass "
                    "'%s' — the resolved id is dropped on the new-install branch" % PREFERRED_ID
                )

    # Arm 3: installBundledSkill's add() must pass preferredId = dirName.
    bundled = slice_between(src, BUNDLED_DECL, BUNDLED_NEXT, "SkillRepository.kt", v)
    if bundled is not None:
        a = bundled.find("add(")
        if a < 0:
            v.append("SkillRepository.kt: installBundledSkill has no add() call (ROT — update this gate)")
        else:
            block = bundled[a:bundled.find(")\n", a) if bundled.find(")\n", a) > 0 else len(bundled)]
            if PREFERRED_ID not in block:
                v.append(
                    "SkillRepository.kt: installBundledSkill's add() does not pass "
                    "'%s' — a bundled skill may land under a slug other than its "
                    "assets dirName (payload lives in <dirName>/)" % PREFERRED_ID
                )
    return v


def balanced_index(text, open_idx, opener, closer):
    """Index of the delimiter matching [opener] at `open_idx`, or -1."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


def block_body(code, decl_idx):
    """The `{...}` body of a block-bodied declaration at `decl_idx`, or None.

    Bounded on purpose: the opening brace must sit on the SAME LINE as the
    parameter list's closing paren (this codebase's style). That keeps an
    expression-bodied sibling (`fun foo(): Int = 0`) from swallowing the next
    declaration's brace.
    """
    op = code.find("(", decl_idx)
    if op < 0:
        return None
    cp = balanced_index(code, op, "(", ")")
    if cp < 0:
        return None
    ob = code.find("{", cp)
    if ob < 0 or "\n" in code[cp + 1:ob]:
        return None
    oe = brace_block_end(code, ob)
    if oe < 0:
        return None
    return code[ob:oe + 1]


def block_functions(code):
    """[(name, body)] for every block-bodied declaration in the file."""
    out = []
    for m in FN_DECL_RE.finditer(code):
        body = block_body(code, m.start())
        if body is not None:
            out.append((m.group(1), body))
    return out


def check_chat_repo(src):
    """Arms 1–3 [fix/da1021-A, widened by dv21-a2]: EVERY `sessions`-row delete
    entry point runs the cross-db cleanup.

    The hook was originally wired into two of the three entry points; the
    startup empty-session sweep (`deleteEmptySessions`) skipped it, so a session
    the user opened, flipped a session-scoped toggle on and then abandoned kept
    its skills.db / mcp.db override rows, its badge and its offload grants for
    whatever session later reused the id. Criterion: every function that removes
    `sessions` rows must reach `onSessionDeleted` — inline, or through a helper
    it calls — and the entry-point SET is derived from the tree, so a fourth
    entry point that skips the cleanup goes red instead of passing a gate whose
    list nobody updated.
    """
    v = []
    code = strip_literals(src)
    if code is None:
        v.append("ChatRepository.kt: unterminated string/comment (ROT — update this gate)")
        return v
    funcs = block_functions(code)
    bodies = {}
    for name, body in funcs:
        bodies.setdefault(name, body)
    # The decision this gate is about: which functions drop `sessions` rows.
    row_deleters = sorted({name for name, body in funcs
                           if any(call in body for call in SESSION_ROW_DELETES)})
    registered = []
    for decl, label in ((DELETE_SESSION, "deleteSession"),
                        (DELETE_ROW_ONLY, "deleteSessionRowOnly"),
                        (DELETE_EMPTY, "deleteEmptySessions")):
        m = re.search(r"\bfun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", decl)
        registered.append((decl, label, m.group(1) if m else label))
    names = {name for _, _, name in registered}
    # Arm 1: the registered declarations are still there (a rename is ROT, not
    # a silent pass).
    entry_bodies = {}
    for decl, label, name in registered:
        off = code.find(decl)
        if off < 0:
            v.append("ChatRepository.kt: '%s' not found (ROT — update this gate)" % decl)
            continue
        entry_bodies[label] = block_body(code, off)
    # Arm 2: nothing removes `sessions` rows outside the registered set.
    for name in row_deleters:
        if name not in names:
            v.append(
                "ChatRepository.kt: %s removes `sessions` rows but is not a registered "
                "delete entry point — it must reach '%s' too (ROT — update this gate)"
                % (name, INVOKE_HOOK)
            )
    # Arm 3: every entry point reaches the hook (directly or via a helper whose
    # own body holds it), judged on the body, not on a whole-line spelling.
    for _, label, _ in registered:
        body = entry_bodies.get(label)
        if body is None:
            continue
        if INVOKE_HOOK in body:
            continue
        if any(INVOKE_HOOK in bodies.get(c, "") for c in CALL_RE.findall(body)):
            continue
        v.append(
            "ChatRepository.kt: %s does not invoke '%s' — cross-db "
            "session cleanup is unwired" % (label, INVOKE_HOOK)
        )
    if INVOKE_HOOK not in src:
        v.append("ChatRepository.kt: onSessionDeleted hook declaration missing")
    return v


def code_only(src):
    """Drop comment-only lines so a KDoc/backticked mention of an anchor
    string can't satisfy (or shadow) a real code anchor."""
    return "\n".join(l for l in src.split("\n") if not l.strip().startswith("//"))


def check_minis_app(src):
    src = code_only(src)
    v = []
    if CHATREPO_NEW not in src:
        v.append("MinisApp.kt: '%s' not found (ROT — update this gate)" % CHATREPO_NEW)
        return v
    chat_line = src.find(CHATREPO_NEW)
    skill_line = src.find(SKILL_NEW)
    mcp_line = src.find(MCP_NEW)
    if skill_line < 0:
        v.append("MinisApp.kt: '%s' assignment not found" % SKILL_NEW)
    if mcp_line < 0:
        v.append("MinisApp.kt: '%s' assignment not found" % MCP_NEW)
    # Arm 5: skill/mcp must be constructed BEFORE chatRepository (the callback
    # must reference the real singletons, not a second copy built too late).
    if skill_line >= 0 and skill_line > chat_line:
        v.append("MinisApp.kt: skillRepository is constructed AFTER chatRepository — the onSessionDeleted callback would reference an unwired instance")
    if mcp_line >= 0 and mcp_line > chat_line:
        v.append("MinisApp.kt: mcpRepository is constructed AFTER chatRepository — the onSessionDeleted callback would reference an unwired instance")
    if "clearSessionOverrides(sid)" not in src:
        v.append("MinisApp.kt: onSessionDeleted callback does not call clearSessionOverrides")
    return v

def check_chat_entry(src):
    """Arm 6 [da1030-A / I01]: `ensureSession`'s draft-promotion block must
    re-point the pinned WebApp shortcuts onto the real session id.

    A shortcut pinned from a draft chat stores `__new__<uuid>` in both scope
    columns, and `migrateDraftResources` (called a few lines above) moves the
    session tree to the real id and deletes the draft directory. Every reader
    resolves `htmlPath` through `minis-sessions/<scope_context>/attachments`,
    so without this hop the pinned launcher icon points at a directory that no
    longer exists ("Source Missing") and nothing else ever rewrites the row.

    The criterion is scoped to the `if (isDraft)` block on purpose: a call
    placed outside it (e.g. before the guard) would fire for real sessions too
    and, on the draft path, would run after the promotion window.
    """
    v = []
    code = strip_literals(src)
    if code is None:
        v.append("ChatSessionEntryExt.kt: unterminated string/comment literal — cannot "
                 "brace-match ensureSession (ROT — update this gate)")
        return v
    start = code.find(ENSURE_DECL)
    if start < 0:
        v.append("ChatSessionEntryExt.kt: '%s' not found (ROT — update this gate)" % ENSURE_DECL)
        return v
    open_brace = code.find("{", start)
    if open_brace < 0:
        v.append("ChatSessionEntryExt.kt: ensureSession has no body brace (ROT — update this gate)")
        return v
    end = brace_block_end(code, open_brace)
    if end < 0:
        v.append("ChatSessionEntryExt.kt: ensureSession body braces do not balance "
                 "(ROT — update this gate)")
        return v
    body = code[open_brace:end + 1]

    draft_at = body.find(DRAFT_IF)
    if draft_at < 0:
        v.append("ChatSessionEntryExt.kt: ensureSession has no '%s' block (ROT — update this gate)"
                 % DRAFT_IF)
        return v
    draft_open = body.find("{", draft_at)
    draft_end = brace_block_end(body, draft_open) if draft_open >= 0 else -1
    if draft_end < 0:
        v.append("ChatSessionEntryExt.kt: ensureSession's '%s' braces do not balance "
                 "(ROT — update this gate)" % DRAFT_IF)
        return v
    draft_body = body[draft_open:draft_end + 1]

    if RENAME_SHORTCUTS not in draft_body:
        v.append("ChatSessionEntryExt.kt: ensureSession's '%s' block does not call '%s' — a "
                 "shortcut pinned on the draft keeps scope_context = __new__* and resolves "
                 "under the draft directory migrateDraftResources just deleted "
                 "(launcher icon: \"Source Missing\")" % (DRAFT_IF, RENAME_SHORTCUTS))
    return v

def check_minis_app_badge(src):
    """Arm 7 [da1030-A / I01 裂缝B]: the `onSessionDeleted` lambda must drop the
    deleted session's badge queue as well.

    `SessionBadgeStore.clear` removes the id from the in-memory StateFlow AND
    from SharedPreferences; without the hook a deleted session's PAUSED badge
    survived the process and re-surfaced on whatever session later reused the
    id. The criterion is scoped to the lambda body: a stray call elsewhere in
    `onCreate` would not run on the delete path.
    """
    v = []
    code = strip_literals(src)
    if code is None:
        v.append("MinisApp.kt: unterminated string/comment literal — cannot brace-match "
                 "onSessionDeleted (ROT — update this gate)")
        return v
    at = code.find(ON_SESSION_DELETED)
    if at < 0:
        v.append("MinisApp.kt: '%s' not found (ROT — update this gate)" % ON_SESSION_DELETED)
        return v
    open_brace = code.find("{", at)
    if open_brace < 0:
        v.append("MinisApp.kt: onSessionDeleted lambda has no brace (ROT — update this gate)")
        return v
    end = brace_block_end(code, open_brace)
    if end < 0:
        v.append("MinisApp.kt: onSessionDeleted lambda braces do not balance "
                 "(ROT — update this gate)")
        return v
    body = code[open_brace:end + 1]
    if BADGE_CLEAR not in body:
        v.append("MinisApp.kt: onSessionDeleted does not call '%s' — a deleted session's "
                 "badge queue (memory + SharedPreferences) survives the delete and "
                 "re-surfaces on whatever session reuses the id" % BADGE_CLEAR)
    return v


A05_SWEEP_CALL = "deleteEmptySessions("
A05_LIVE_SET = "activeSessionIds"
A05_IO = "Dispatchers.IO"


def _a05_balanced(src, open_idx, opener, closer):
    depth = 0
    k = open_idx
    while k < len(src):
        c = src[k]
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return -1


def _a05_io_regions(src):
    """Brace-balanced regions opened by an IO dispatch.

    The `)` + `.name`-hop + `{` walk is deliberate: `CoroutineScope(SupervisorJob()
    + Dispatchers.IO)` is a *property* initialiser with no block, and treating its
    far-away next `{` as a region would let an on-main sweep hide inside it.
    """
    regions = []
    token = "Dispatchers.IO"
    k = src.find(token)
    while k >= 0:
        tail = src[k + len(token):]
        j = 0
        while j < len(tail) and tail[j] in " \t\r\n":
            j += 1
        if j < len(tail) and tail[j] == ")":
            j += 1
            while True:
                while j < len(tail) and tail[j] in " \t\r\n":
                    j += 1
                if j < len(tail) and tail[j] == ".":
                    j += 1
                    while j < len(tail) and (tail[j].isalnum() or tail[j] == "_"):
                        j += 1
                    continue
                break
            if j < len(tail) and tail[j] == "{":
                brace = k + len(token) + j
                close = _a05_balanced(src, brace, "{", "}")
                if close > brace:
                    regions.append((brace, close))
        k = src.find(token, k + 1)
    return regions


def check_minis_app_sweep(src):
    """Arm 7 [dv20-A4-A05]: the startup empty-session sweep must be *wired*.

    `ChatRepository.deleteEmptySessions` and `ChatViewModelStore.activeSessionIds`
    both shipped with zero production callers, so the backstop that clears a
    session the user opened and closed without typing never ran — the empty rows
    accumulated in the drawer forever. Three decisions, in the order they can go
    wrong:

      1. the call exists at all (a helper with no caller is indistinguishable
         from a feature nobody built);
      2. it carries the live-session set — sweeping without it deletes the very
         session the user is in when they reopen the app;
      3. it runs off the main thread — it is a Room DELETE at cold start.
    """
    src = code_only(src)
    v = []
    off = src.find(A05_SWEEP_CALL)
    if off < 0:
        v.append("MinisApp.kt: no call to '%s' — the startup empty-session "
                 "backstop never runs" % A05_SWEEP_CALL)
        return v
    paren = src.find("(", off)
    end = _a05_balanced(src, paren, "(", ")") if paren >= 0 else -1
    args = src[paren:end + 1] if end > paren else src[paren:]
    if A05_LIVE_SET not in args:
        v.append("MinisApp.kt: '%s' is called without '%s()' — the sweep would "
                 "delete the sessions that are still live"
                 % (A05_SWEEP_CALL, A05_LIVE_SET))
    if not any(lo <= off <= hi for lo, hi in _a05_io_regions(src)):
        v.append("MinisApp.kt: '%s' is not inside a '%s' region — it is a Room "
                 "DELETE at cold start and must not run on the caller's thread"
                 % (A05_SWEEP_CALL, A05_IO))
    return v


def mask_keep_offsets(src):
    """Mask string/comment BODIES with spaces, keeping every offset intact.

    The sibling `strip_literals` collapses bodies (offsets shift) and
    `code_only` drops whole comment lines — neither can be used when the
    criterion is *ordering* ("the composer blanking must come after the durable
    write"). Quotes are kept so `= ""` stays visible as an assignment of a
    literal; only the bodies are blanked, newlines preserved.

    Returns None when a literal/comment never terminates (caller treats that as
    ROT rather than matching shifted garbage).
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        if src.startswith('"""', i):
            end = src.find('"""', i + 3)
            if end < 0:
                return None
            for k in range(i + 3, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end + 3
            continue
        c = src[i]
        if c == '"' or c == "'":
            i += 1
            while i < n and src[i] != c:
                if src[i] == "\\":
                    if out[i] != "\n":
                        out[i] = " "
                    i += 1
                    if i < n and out[i] != "\n":
                        out[i] = " "
                    i += 1
                    continue
                if out[i] != "\n":
                    out[i] = " "
                i += 1
            if i >= n:
                return None
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                return None
            for k in range(i, j + 2):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 2
            continue
        i += 1
    return "".join(out)


def decl_slice(masked, decl):
    """`masked[decl:next-top-level-declaration]` — for BOTH body shapes.

    `block_body` only understands block bodies and returns None for an
    expression-bodied declaration (`): Boolean = ...`), which is the shape the
    drawer helper is written in. Slicing to the next top-level declaration keeps
    the anchor honest without dictating the shape.
    """
    start = masked.find(decl)
    if start < 0:
        return None
    end = len(masked)
    for pat in TOP_LEVEL_BOUNDS:
        k = masked.find(pat, start + len(decl))
        if k >= 0:
            end = min(end, k)
    return masked[start:end]


def check_promote_draft(src):
    """Arm 9 [dv24-b3 / deep-audit-1044 B-06]: promoting a draft on "New Chat"
    must hand the typed body to a DURABLE home owned by the new session.

    The pre-fix function wrote `sessions.title = text.take(50).trim()` and
    nothing else, then blanked both in-memory copies. Consequences, in the order
    they bite: the list row has no `messages`, so `ChatHistoryDrawer`'s
    `pinnedAt / messageCounts` filter hides it; `cleanupIfEmptyOnExit` deletes it
    (and releases the VM) the moment the user opens and leaves that session;
    the startup sweep deletes it after 60 s. The text is gone 100% of the time.

    Two decisions, both anchored on the outcome rather than on a spelling:
      C1 — the captured text reaches `appendMessage(` / `updateSessionPreview(`
           AFTER `ensureSession()` (persisting under a sid that does not exist
           yet is a no-op);
      C2 — IF the composer copy is blanked, that happens AFTER the durable write.
           Ordering is the cancellation safety net: clearing first destroys the
           only copy if the coroutine never resumes.
    """
    v = []
    masked = mask_keep_offsets(src)
    if masked is None:
        v.append("ChatSessionEntryExt.kt: unterminated string/comment literal — cannot mask "
                 "offsets for the promotion ordering check (ROT — update this gate)")
        return v
    start = masked.find(PROMOTE_DECL)
    if start < 0:
        v.append("ChatSessionEntryExt.kt: '%s' not found (ROT — update this gate)"
                 % PROMOTE_DECL)
        return v
    open_brace = masked.find("{", start)
    end = brace_block_end(masked, open_brace) if open_brace >= 0 else -1
    if end < 0:
        v.append("ChatSessionEntryExt.kt: promoteDraftIfNeeded body braces do not balance "
                 "(ROT — update this gate)")
        return v
    body = masked[open_brace:end + 1]

    ensure_at = body.find(ENSURE_CALL)
    wired = []
    for token in DURABLE_WRITES:
        k = body.find(token)
        while k >= 0:
            paren = body.find("(", k + len(token) - 1)
            close = balanced_index(body, paren, "(", ")") if paren >= 0 else -1
            args = body[paren:close + 1] if close > paren else ""
            # The persisted payload must carry the captured text. `sid`-only
            # writes (a title, a binding) are exactly the pre-fix shape.
            if "text" in args:
                wired.append(k)
            k = body.find(token, k + 1)
    if not wired:
        v.append(
            "ChatSessionEntryExt.kt: promoteDraftIfNeeded never persists the captured draft text "
            "into the promoted session (no 'appendMessage(' / 'updateSessionPreview(' carrying "
            "`text`) — only a 50-char title would land, and a message-less row is hidden by the "
            "drawer, deleted by cleanupIfEmptyOnExit and swept at the next cold start"
        )
    elif ensure_at < 0:
        v.append("ChatSessionEntryExt.kt: promoteDraftIfNeeded does not call '%s' — the promoted "
                 "id it persists against cannot exist" % ENSURE_CALL)
    elif not any(k > ensure_at for k in wired):
        v.append("ChatSessionEntryExt.kt: promoteDraftIfNeeded persists the body BEFORE '%s' — "
                 "the write lands against a session id that does not exist yet and is "
                 "silently dropped" % ENSURE_CALL)

    blanks = [m.start() for m in COMPOSER_LITERAL_RE.finditer(body)]
    if blanks and wired:
        first_durable = min(wired)
        for b in blanks:
            if b < first_durable:
                v.append(
                    "ChatSessionEntryExt.kt: promoteDraftIfNeeded blanks the composer "
                    "(_inputText.value = \"\") BEFORE the durable body write — a cancellation "
                    "or DB failure in between leaves NO copy of the draft text"
                )
    return v


def check_drawer_visibility(src):
    """Arm 10 [dv24-b3 / deep-audit-1044 B-06]: the drawer's visibility
    decision must treat a preview as content, without resurrecting ghost rows.

    A promoted draft's body lives in `last_message` while `messages` stays empty,
    so the old `pinnedAt != null || messageCounts > 0` filter hid exactly the
    sessions this fix rescues. The inverse hazard is the `P0-1-drawer-title-
    visibility` contract: `/memory` and `/thinking` toggle rows are message-less
    AND preview-less, so they must stay hidden.

    Criterion shape: the composable must ROUTE the filter through the extracted
    pure predicate (so the four cases are JVM-pinnable, see
    PromoteDraftVisibilityTest), and that predicate must consult all three
    inputs — dropping any one of them is a different bug (drop the pin term and
    pinned empties vanish; drop the count term and ordinary chats vanish; drop
    the preview term and this fix evaporates).
    """
    v = []
    masked = mask_keep_offsets(src)
    if masked is None:
        v.append("ChatHistoryDrawer.kt: unterminated string/comment literal — cannot inspect the "
                 "visibility filter (ROT — update this gate)")
        return v
    if not DRAWER_FILTER_RE.search(masked):
        v.append("ChatHistoryDrawer.kt: the session list filter does not route through '%s' — the "
                 "visibility decision is inline again (a promoted draft with a preview but no "
                 "messages row stays invisible)" % DRAWER_VISIBLE_CALL)
    helper = decl_slice(masked, DRAWER_VISIBLE_DECL)
    if helper is None:
        v.append("ChatHistoryDrawer.kt: '%s' not found (ROT — update this gate)"
                 % DRAWER_VISIBLE_DECL)
        return v
    # Look at the DECISION, not the parameter list: the parameters are named
    # after the three inputs, so a term dropped from the predicate body would
    # otherwise still be "mentioned" by its own parameter.
    paren = helper.find("(")
    close = balanced_index(helper, paren, "(", ")") if paren >= 0 else -1
    decision = helper[close + 1:] if close > paren else helper
    for term, label in (("pinnedAt", "the pinned reason"),
                        ("messageCount", "the has-messages reason"),
                        ("lastMessage", "the has-a-preview reason")):
        if term not in decision:
            v.append("ChatHistoryDrawer.kt: isDrawerRowVisible does not consult '%s' (%s) — "
                     "that row class silently disappears from the drawer" % (term, label))
    if not any(token in decision for token in PREVIEW_BLANK_CHECKS):
        v.append("ChatHistoryDrawer.kt: isDrawerRowVisible accepts any non-null preview — a "
                 "blank `last_message` would resurrect the ghost rows "
                 "(P0-1-drawer-title-visibility): test blankness (%s)"
                 % " / ".join(PREVIEW_BLANK_CHECKS))
    return v



GOOD_APP_SWEEP = """
class MinisApp : Application() {
    override fun onCreate() {
        kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {
            runCatching {
                val swept = chatRepository.deleteEmptySessions(
                    com.rikkaminis.app.ui.chat.ChatViewModelStore.activeSessionIds(),
                )
            }
        }
    }
}
"""

BAD_APP_SWEEP_UNWIRED = GOOD_APP_SWEEP.replace(
    "chatRepository.deleteEmptySessions(", "chatRepository.countEmptySessions(")

BAD_APP_SWEEP_NO_LIVE_SET = GOOD_APP_SWEEP.replace(
    "                    com.rikkaminis.app.ui.chat.ChatViewModelStore.activeSessionIds(),\n", "")

BAD_APP_SWEEP_ON_MAIN = GOOD_APP_SWEEP.replace(
    "kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {",
    "kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Main).launch {")


def read(root, rel):
    path = os.path.join(root, rel)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def run(root):
    if not os.path.isdir(root):
        print("skill_restore_session_cleanup_check: not a directory: %s" % root)
        return 2
    try:
        skill = read(root, SKILL_REPO_REL)
        chat = read(root, CHAT_REPO_REL)
        app = read(root, MINIS_APP_REL)
        entry = read(root, CHAT_ENTRY_REL)
        drawer = read(root, DRAWER_REL)
    except OSError as exc:
        print("skill_restore_session_cleanup_check: cannot read sources: %s" % exc)
        return 2
    violations = (check_skill_repo(skill) + check_chat_repo(chat) + check_minis_app(app)
                  + check_chat_entry(entry) + check_minis_app_badge(app)
                  + check_minis_app_sweep(app)
                  + check_promote_draft(entry) + check_drawer_visibility(drawer))
    if violations:
        print("FAIL skill_restore_session_cleanup_check")
        for v in violations:
            print("  " + v)
        return 1
    print("PASS skill_restore_session_cleanup_check (id threaded, cleanup closed)")
    return 0


# --------------------------------------------------------------------------
# selftest: compliant fixtures must be clean, violating fixtures must trip
# --------------------------------------------------------------------------

GOOD_SKILL = """
internal class SkillRepository(private val context: Context) {
    fun add(
        name: String,
        description: String,
        body: String,
        frontmatter: String = "",
        preferredId: String? = null,
    ): Skill? {
        val id = SkillRestorePolicy.resolveImportId(preferredId, slugify(name))
        if (id.isBlank()) return null
        return synchronized(addLock) { _skills.value.firstOrNull { it.id == id } }
    }

    fun importFromContent(
        content: String,
        preferredId: String? = null,
    ): Skill? {
        val parsed = parseSkillMd(content) ?: return null
        val id = SkillRestorePolicy.resolveImportId(preferredId, slugify(parsed.name))
        if (id.isNotBlank() && _skills.value.any { it.id == id }) {
            return replace(id, parsed)
        }
        return add(
            name = parsed.name,
            description = parsed.description,
            body = parsed.body,
            frontmatter = SubagentSkill.extractFrontmatterBlock(content),
            preferredId = id,
        )
    }

    fun skillMdPath(id: String): String = "/var/minis/skills/$id/SKILL.md"

    private fun installBundledSkill(dirName: String) {
        val parsed = parseSkillMd(readAsset(dirName)) ?: return
        if (existing != null) {
            update(existing)
        } else {
            add(
                name = parsed.name,
                body = parsed.body,
                frontmatter = "",
                preferredId = dirName,
            )
        }
    }

    private fun isAdoptableBundledRow(existing: Skill): Boolean = false

    // [dv16-A05] the compliant shape: sparse-clear when the value equals the
    // CURRENT global default, decided by the shared pure function.
    fun setSessionOverride(sessionId: String, skillId: String, enabled: Boolean) {
        val globalEnabled = _skills.value.find { it.id == skillId }?.isEnabled
        if (shouldDeleteSessionSkillOverride(globalEnabled, enabled)) {
            db.execSQL(
                "DELETE FROM session_skill_overrides WHERE session_id=? AND skill_id=?",
                arrayOf(sessionId, skillId)
            )
        } else {
            db.execSQL(
                "INSERT OR REPLACE INTO session_skill_overrides (session_id, skill_id, is_enabled) VALUES (?, ?, ?)",
                arrayOf<Any>(sessionId, skillId, if (enabled) 1 else 0)
            )
        }
    }

    fun clearSessionOverrides(sessionId: String) {
        db.execSQL("DELETE FROM session_skill_overrides WHERE session_id=?", arrayOf(sessionId))
    }

    fun recordSkillUse(skillId: String) {
        // The mirror must move under the same lock as add().
        synchronized(addLock) {
            val current = _skills.value.find { it.id == skillId } ?: return
            val bumped = current.copy(useCount = current.useCount + 1.0)
            db.execSQL(
                "UPDATE skills SET use_count = use_count + 1 WHERE id=?",
                arrayOf<Any>(skillId)
            )
            _skills.value = _skills.value.map { if (it.id == skillId) bumped else it }
        }
    }
}
"""

BAD_SKILL_RAW_SLUG = GOOD_SKILL.replace(
    "val id = SkillRestorePolicy.resolveImportId(preferredId, slugify(name))\n        if (id.isBlank()) return null",
    "val id = slugify(name)\n        if (id.isBlank()) return null",
)

BAD_SKILL_DROP_ID = GOOD_SKILL.replace(
    "            frontmatter = SubagentSkill.extractFrontmatterBlock(content),\n            preferredId = id,\n        )",
    "            frontmatter = SubagentSkill.extractFrontmatterBlock(content),\n        )",
)

BAD_SKILL_BUNDLED = GOOD_SKILL.replace("preferredId = dirName,", "")

# [da1029-C / I04] the pre-fix shape: `mapped` computed under the lock, then
# published after the release — exactly what the KDoc above it used to claim
# was already impossible.
BAD_SKILL_PUBLISH_OUTSIDE_LOCK = GOOD_SKILL.replace(
    """        synchronized(addLock) {
            val current = _skills.value.find { it.id == skillId } ?: return""",
    """        val next = synchronized(addLock) {
            val current = _skills.value.find { it.id == skillId } ?: return""",
).replace(
    """            _skills.value = _skills.value.map { if (it.id == skillId) bumped else it }
        }
    }
}""",
    """            _skills.value.map { if (it.id == skillId) bumped else it }
        }
        _skills.value = next
    }
}""",
)

# No lock at all around the read-modify-write.
BAD_SKILL_NO_LOCK = GOOD_SKILL.replace(
    """        synchronized(addLock) {
            val current = _skills.value.find { it.id == skillId } ?: return""",
    """        run {
            val current = _skills.value.find { it.id == skillId } ?: return""",
)

# Anchor itself gone -> fail-closed, not a silent skip.
BAD_SKILL_DROP_FUNCTION = GOOD_SKILL.replace(
    "fun recordSkillUse(", "fun recordSkillUseRenamed(")

# [dv16-A05] the pre-fix shape: INSERT OR REPLACE always — a toggled-back
# session keeps its pinned row forever (the MCP twin sparse-clears; this one
# didn't).
BAD_SKILL_PIN_OVERRIDE = GOOD_SKILL.replace(
    """        val globalEnabled = _skills.value.find { it.id == skillId }?.isEnabled
        if (shouldDeleteSessionSkillOverride(globalEnabled, enabled)) {
            db.execSQL(
                "DELETE FROM session_skill_overrides WHERE session_id=? AND skill_id=?",
                arrayOf(sessionId, skillId)
            )
        } else {
            db.execSQL(""",
    """        db.execSQL(""",
)

GOOD_CHAT = """
class ChatRepository(
    internal val dao: ChatDao,
    private val sessionFiles: SessionFileStore? = null,
    val onSessionDeleted: ((String) -> Unit)? = null,
) {
    suspend fun deleteSession(id: String): SessionFileStore.DeleteResult? {
        dao.deleteMessages(id)
        dao.deleteSession(id)
        runCatching { onSessionDeleted?.invoke(id) }.onFailure { warn(it) }
        return sessionFiles?.deleteSessionFiles(id)
    }

    suspend fun deleteSessionRowOnly(id: String) {
        dao.deleteMessages(id)
        dao.deleteSession(id)
        runCatching { onSessionDeleted?.invoke(id) }.onFailure { warn(it) }
    }

    suspend fun deleteEmptySessions(
        activeIds: List<String> = emptyList(),
        graceMillis: Long = 60_000L,
    ): Int {
        val guarded = guardActiveIds(activeIds)
        val staleBefore = System.currentTimeMillis() - graceMillis
        val candidates = dao.selectEmptySessionIds(guarded, staleBefore)
        val deleted = dao.deleteEmptySessions(guarded, staleBefore)
        if (candidates.isEmpty()) return deleted
        val survivors = dao.existingSessionIds(candidates).toSet()
        for (id in candidates) {
            if (id in survivors) continue
            runCatching { onSessionDeleted?.invoke(id) }.onFailure { warn(it) }
        }
        return deleted
    }
}
"""

# One line of the hook, exactly as the wired fixture spells it. Removing every
# occurrence models "the cleanup was never wired anywhere".
HOOK_LINE = "runCatching { onSessionDeleted?.invoke(id) }.onFailure { warn(it) }\n"
# The sweep's copy sits one indent deeper (inside the per-id loop), so this key
# removes ONLY the sweep's notification.
SWEEP_HOOK_LINE = "            " + HOOK_LINE

BAD_CHAT_NO_HOOK = GOOD_CHAT.replace(HOOK_LINE, "")

# The dv21-a2 regression itself: a two-entry-point gate was blind to the sweep.
BAD_CHAT_SWEEP_NO_HOOK = GOOD_CHAT.replace(SWEEP_HOOK_LINE, "")

# A FOURTH entry point that drops `sessions` rows without the cleanup: the gate
# must derive the entry set from the tree, not from a list someone forgot.
BAD_CHAT_UNREGISTERED_ENTRY = GOOD_CHAT.replace(
    "        return deleted\n    }\n}",
    "        return deleted\n    }\n\n"
    "    suspend fun deleteExpiredSessions(ids: List<String>) {\n"
    "        dao.deleteSession(ids.first())\n"
    "    }\n}",
)

# Delegation is legal: the entry points call one helper that owns the hook.
GOOD_CHAT_DELEGATED = GOOD_CHAT.replace(
    "            " + HOOK_LINE, "            notifySessionDeleted(id)\n",
).replace(
    "        " + HOOK_LINE, "        notifySessionDeleted(id)\n",
).replace(
    "    }\n}",
    "    }\n\n    private fun notifySessionDeleted(id: String) {\n"
    "        runCatching { onSessionDeleted?.invoke(id) }.onFailure { warn(it) }\n"
    "    }\n}",
)


def _drop_helper_hook(src):
    """Keep the delegated shape but empty the helper — the delegation branch
    must not launder an unhooked helper."""
    return src.replace(
        "    private fun notifySessionDeleted(id: String) {\n"
        "        runCatching { onSessionDeleted?.invoke(id) }.onFailure { warn(it) }\n"
        "    }\n",
        "    private fun notifySessionDeleted(id: String) {\n"
        "        log(id)\n"
        "    }\n",
    )


BAD_CHAT_HELPER_NO_HOOK = _drop_helper_hook(GOOD_CHAT_DELEGATED)

GOOD_APP = """
class MinisApp : Application() {
    lateinit var chatRepository: ChatRepository
    lateinit var skillRepository: SkillRepository
    lateinit var mcpRepository: MCPRepository

    override fun onCreate() {
        database = AppDatabase.getInstance(this)
        skillRepository = SkillRepository(this)
        mcpRepository = MCPRepository(this)
        chatRepository = ChatRepository(
            database.chatDao(),
            SessionFileStore(this),
            onSessionDeleted = { sid ->
                runCatching { skillRepository.clearSessionOverrides(sid) }
                runCatching { mcpRepository.clearSessionOverrides(sid) }
                runCatching { SessionBadgeStore.clear(sid) }
            },
        )
    }
}
"""

# [da1030-A / I01 裂缝B] the badge queue left behind by the pre-fix hook.
BAD_APP_NO_BADGE = GOOD_APP.replace(
    "                runCatching { SessionBadgeStore.clear(sid) }\n", "")

# Precision control: a call that exists, but NOT on the delete path — the
# criterion must be scoped to the lambda body, not to the file.
BAD_APP_BADGE_ELSEWHERE = GOOD_APP.replace(
    "                runCatching { SessionBadgeStore.clear(sid) }\n", "").replace(
    "    override fun onCreate() {\n",
    "    override fun onCreate() {\n        SessionBadgeStore.clear(\"unrelated\")\n")

# [da1030-A / I01] draft-promotion fixture. The `if (isDraft)` block carries
# the full hop set the production file has.
GOOD_ENTRY = """
internal suspend fun ChatViewModel.ensureSession(): String {
    if (realSessionId.isNotEmpty()) return realSessionId
    val session = chatRepository.createSession(modelId = modelId)
    realSessionId = session.id
    if (isDraft) {
        ChatViewModelStore.rename(sessionId, session.id)
        migrateDraftResources(fromDraft = sessionId, toReal = session.id)
        skillRepository?.renameSessionOverrides(fromDraft = sessionId, toReal = session.id)
        mcpRepository?.renameSessionOverrides(fromDraft = sessionId, toReal = session.id)
        runCatching {
            (context.applicationContext as? MinisApp)
                ?.webAppShortcutRepository
                ?.renameSessionShortcuts(fromDraft = sessionId, toReal = session.id)
        }
        _browserTabPoolRef?.setSession(session.id)
    }
    return realSessionId
}
"""

BAD_ENTRY_DROP_SHORTCUTS = GOOD_ENTRY.replace(
    """        runCatching {
            (context.applicationContext as? MinisApp)
                ?.webAppShortcutRepository
                ?.renameSessionShortcuts(fromDraft = sessionId, toReal = session.id)
        }
""", "")

# Precision control: the call exists but sits OUTSIDE the `if (isDraft)` guard
# (it would then also fire for already-real sessions, in the wrong window).
BAD_ENTRY_OUTSIDE_DRAFT = GOOD_ENTRY.replace(
    """        runCatching {
            (context.applicationContext as? MinisApp)
                ?.webAppShortcutRepository
                ?.renameSessionShortcuts(fromDraft = sessionId, toReal = session.id)
        }
""", "").replace(
    "    return realSessionId\n}",
    "    webAppShortcutRepository?.renameSessionShortcuts(sessionId, session.id)\n"
    "    return realSessionId\n}")

# [dv24-b3 / deep-audit-1044 B-06] draft-promotion fixtures. The compliant
# shape persists the body AFTER ensureSession and blanks the composer only
# after that write landed.
GOOD_PROMOTE = '''
fun ChatViewModel.promoteDraftIfNeeded(): Boolean {
    if (!isDraft || realSessionId.isNotEmpty()) return false
    val text = _inputText.value
    if (text.isBlank()) return false
    com.rikkaminis.app.data.ComposerDraftStore.clearDraft(context, sessionId)
    viewModelScope.launch {
        val sid = ensureSession()
        if (sid.isEmpty()) return@launch
        val title = text.take(50).trim()
        chatRepository.updateSessionTitleAndCategory(sid, title, null)
        _sessionTitle.value = title
        val promoted = chatRepository.appendMessage(sid, "user", buildUserPartsJson(text, emptyList()))
        _messages.value = _messages.value + ChatMessage(id = promoted.id, role = "user", content = text)
        _inputText.value = ""
    }
    return true
}
'''

PERSIST_TWO_LINES = (
    "        val promoted = chatRepository.appendMessage(sid, \"user\", buildUserPartsJson(text, emptyList()))\n"
    "        _messages.value = _messages.value + ChatMessage(id = promoted.id, role = \"user\", content = text)\n"
)

# The pre-fix shape: a 50-char title and nothing else.
BAD_PROMOTE_TITLE_ONLY = GOOD_PROMOTE.replace(PERSIST_TWO_LINES, "")

# Ordering hazard: the composer is blanked BEFORE the durable write, so a
# cancellation in between destroys the only copy.
BAD_PROMOTE_BLANK_FIRST = GOOD_PROMOTE.replace(
    "        _inputText.value = \"\"\n", "").replace(
    "    com.rikkaminis.app.data.ComposerDraftStore.clearDraft(context, sessionId)\n",
    "    com.rikkaminis.app.data.ComposerDraftStore.clearDraft(context, sessionId)\n"
    "    _inputText.value = \"\"\n")

# Persisting before the id exists is a no-op against an empty sid.
BAD_PROMOTE_PERSIST_BEFORE_ENSURE = GOOD_PROMOTE.replace(
    "    viewModelScope.launch {\n        val sid = ensureSession()",
    "    viewModelScope.launch {\n"
    "        chatRepository.appendMessage(realSessionId, \"user\", buildUserPartsJson(text, emptyList()))\n"
    "        val sid = ensureSession()").replace(
    PERSIST_TWO_LINES, "")

GOOD_DRAWER = '''
@Composable
fun ChatHistoryDrawer(chatRepository: ChatRepository) {
    val messageCounts by chatRepository.observeMessageCountsPerSession()
        .collectAsState(initial = emptyMap())
    val visibleSessions = remember(sessions, messageCounts) {
        sessions.filter { session ->
            isDrawerRowVisible(
                pinnedAt = session.pinnedAt,
                messageCount = messageCounts[session.id] ?: 0,
                lastMessage = session.lastMessage,
            )
        }
    }
}

internal fun isDrawerRowVisible(
    pinnedAt: Long?,
    messageCount: Int,
    lastMessage: String?,
): Boolean = pinnedAt != null || messageCount > 0 || !lastMessage.isNullOrBlank()
'''

# The pre-fix inline filter: no preview term at all, and no shared predicate.
BAD_DRAWER_INLINE_FILTER = GOOD_DRAWER.replace(
    """        sessions.filter { session ->
            isDrawerRowVisible(
                pinnedAt = session.pinnedAt,
                messageCount = messageCounts[session.id] ?: 0,
                lastMessage = session.lastMessage,
            )
        }""",
    "        sessions.filter { it.pinnedAt != null || (messageCounts[it.id] ?: 0) > 0 }",
)

# Dropping the has-messages term makes every ordinary chat vanish.
BAD_DRAWER_NO_COUNT = GOOD_DRAWER.replace(
    "): Boolean = pinnedAt != null || messageCount > 0 || !lastMessage.isNullOrBlank()",
    "): Boolean = pinnedAt != null || !lastMessage.isNullOrBlank()",
)

# Accepting any non-null preview resurrects the ghost rows.
BAD_DRAWER_ANY_NON_NULL = GOOD_DRAWER.replace(
    "!lastMessage.isNullOrBlank()",
    "lastMessage != null",
)

BAD_APP_ORDER = """
class MinisApp : Application() {
    lateinit var chatRepository: ChatRepository
    lateinit var skillRepository: SkillRepository
    lateinit var mcpRepository: MCPRepository

    override fun onCreate() {
        database = AppDatabase.getInstance(this)
        chatRepository = ChatRepository(
            database.chatDao(),
            SessionFileStore(this),
        )
        skillRepository = SkillRepository(this)
        mcpRepository = MCPRepository(this)
    }
}
"""


def expect_trip(name, violations, needle, failures):
    if not violations:
        failures.append("%s: no violation reported (gate is blind)" % name)
    elif not any(needle in v for v in violations):
        failures.append("%s: violations %r do not mention expected needle %r" % (name, violations, needle))


def selftest():
    failures = []

    good_skill = check_skill_repo(GOOD_SKILL)
    bad_slug = check_skill_repo(BAD_SKILL_RAW_SLUG)
    bad_drop = check_skill_repo(BAD_SKILL_DROP_ID)
    bad_bundled = check_skill_repo(BAD_SKILL_BUNDLED)
    bad_publish = check_skill_repo(BAD_SKILL_PUBLISH_OUTSIDE_LOCK)
    bad_nolock = check_skill_repo(BAD_SKILL_NO_LOCK)
    bad_nofunc = check_skill_repo(BAD_SKILL_DROP_FUNCTION)
    bad_pin = check_skill_repo(BAD_SKILL_PIN_OVERRIDE)

    # Precision control: a compliant fixture must be clean, or "everything is
    # red" would masquerade as teeth.
    if good_skill:
        failures.append("compliant skill fixture is not clean: %r" % good_skill)

    expect_trip("raw-slugify fixture (ISSUE-01)", bad_slug, "does not resolve", failures)
    expect_trip("raw-slugify fixture (ISSUE-01)", bad_slug, "does not consult preferredId", failures)
    expect_trip("dropped-id fixture (ISSUE-01)", bad_drop, "does not pass 'preferredId ='", failures)
    expect_trip("bundled-dirName fixture (ISSUE-01)", bad_bundled, "other than its", failures)
    # [da1029-C / I04]
    expect_trip("publish-outside-lock fixture (I04)", bad_publish, "publishes", failures)
    expect_trip("publish-outside-lock fixture (I04)", bad_publish, "out of the", failures)
    expect_trip("no-lock fixture (I04)", bad_nolock, "has no 'synchronized(addLock)'", failures)
    expect_trip("dropped-function fixture (I04)", bad_nofunc, "not found (ROT", failures)
    # [dv16-A05] the pinned-override pre-fix shape fires ONLY the new arm.
    expect_trip("pinned-override fixture (dv16-A05)", bad_pin, "does not DELETE the override row", failures)
    expect_trip("pinned-override fixture (dv16-A05)", bad_pin, "shouldDeleteSessionSkillOverride", failures)
    if any("does not resolve" in v or "publishes" in v or "has no 'synchronized" in v for v in bad_pin):
        failures.append("pinned-override fixture (dv16-A05) tripped an UNRELATED arm: %r" % bad_pin)

    good_chat = check_chat_repo(GOOD_CHAT)
    bad_chat = check_chat_repo(BAD_CHAT_NO_HOOK)
    expect_trip("unwired-hook fixture (ISSUE-10)", bad_chat, "unwired", failures)

    # [dv21-a2 / deep-audit-1041 A-02] the startup sweep as a THIRD entry point.
    # Precision control first: the wired fixture (now carrying the sweep) must be
    # clean, or "everything is red" would masquerade as teeth.
    if good_chat:
        failures.append("compliant chat fixture is not clean: %r" % good_chat)
    bad_sweep = check_chat_repo(BAD_CHAT_SWEEP_NO_HOOK)
    expect_trip("unhooked-sweep fixture (dv21-a2)", bad_sweep, "deleteEmptySessions", failures)
    expect_trip("unhooked-sweep fixture (dv21-a2)", bad_sweep, "unwired", failures)
    # The sweep fixture must fire ONLY the sweep arm — the other two entries are
    # still wired, so any extra violation means the gate is matching the wrong
    # body (e.g. back to the first-`}` slice).
    if len(bad_sweep) != 1:
        failures.append("unhooked-sweep fixture tripped %d arms, expected exactly 1: %r"
                        % (len(bad_sweep), bad_sweep))

    # A fourth `sessions`-row delete entry point that nobody registered.
    bad_unregistered = check_chat_repo(BAD_CHAT_UNREGISTERED_ENTRY)
    expect_trip("unregistered-entry fixture (dv21-a2)", bad_unregistered, "not a registered",
                failures)

    # Delegation to one shared helper is a legitimate shape and must stay clean;
    # an EMPTY helper must not launder the missing cleanup.
    good_delegated = check_chat_repo(GOOD_CHAT_DELEGATED)
    if good_delegated:
        failures.append("delegating chat fixture is not clean: %r" % good_delegated)
    bad_helper = check_chat_repo(BAD_CHAT_HELPER_NO_HOOK)
    expect_trip("empty-helper fixture (dv21-a2)", bad_helper, "unwired", failures)

    good_app = check_minis_app(GOOD_APP)
    bad_app = check_minis_app(BAD_APP_ORDER)
    expect_trip("late-wiring fixture (ISSUE-10)", bad_app, "AFTER chatRepository", failures)

    # [dv20-A4-A05] the startup empty-session sweep. Precision control first: the
    # wired fixture must be clean, or "everything is red" would masquerade as teeth.
    good_sweep = check_minis_app_sweep(GOOD_APP_SWEEP)
    bad_sweep_unwired = check_minis_app_sweep(BAD_APP_SWEEP_UNWIRED)
    bad_sweep_no_live = check_minis_app_sweep(BAD_APP_SWEEP_NO_LIVE_SET)
    bad_sweep_on_main = check_minis_app_sweep(BAD_APP_SWEEP_ON_MAIN)
    if good_sweep:
        failures.append("compliant startup-sweep fixture is not clean: %r" % good_sweep)
    expect_trip("unwired startup sweep (dv20-A4-A05)", bad_sweep_unwired, "never runs", failures)
    expect_trip("sweep without the live-session set (dv20-A4-A05)", bad_sweep_no_live,
                "still live", failures)
    expect_trip("sweep off the IO region (dv20-A4-A05)", bad_sweep_on_main,
                "Room DELETE at cold start", failures)
    # The live-set arm must fire ONLY its own decision: the other two arms would
    # otherwise mask a regression that left the exclusion set unwired.
    if len(bad_sweep_no_live) != 1:
        failures.append("live-set fixture tripped %d arms, expected exactly 1: %r"
                        % (len(bad_sweep_no_live), bad_sweep_no_live))

    # [da1030-A / I01 裂缝B]
    bad_badge = check_minis_app_badge(BAD_APP_NO_BADGE)
    bad_badge_elsewhere = check_minis_app_badge(BAD_APP_BADGE_ELSEWHERE)
    good_badge = check_minis_app_badge(GOOD_APP)
    if good_badge:
        failures.append("compliant MinisApp badge fixture is not clean: %r" % good_badge)
    expect_trip("no-badge-clear fixture (I01 裂缝B)", bad_badge, "does not call", failures)
    expect_trip("no-badge-clear fixture (I01 裂缝B)", bad_badge, "SessionBadgeStore.clear(",
                failures)
    expect_trip("badge-clear-outside-lambda fixture (I01 裂缝B)", bad_badge_elsewhere,
                "does not call", failures)

    # [da1030-A / I01] draft promotion re-points the WebApp shortcuts
    good_entry = check_chat_entry(GOOD_ENTRY)
    if good_entry:
        failures.append("compliant ChatSessionEntryExt fixture is not clean: %r" % good_entry)
    bad_entry = check_chat_entry(BAD_ENTRY_DROP_SHORTCUTS)
    bad_entry_outside = check_chat_entry(BAD_ENTRY_OUTSIDE_DRAFT)
    expect_trip("dropped-renameSessionShortcuts fixture (I01)", bad_entry,
                "renameSessionShortcuts(", failures)
    expect_trip("dropped-renameSessionShortcuts fixture (I01)", bad_entry,
                "Source Missing", failures)
    expect_trip("renameSessionShortcuts-outside-isDraft fixture (I01)", bad_entry_outside,
                "does not call", failures)

    print("selftest compliant skill repo    -> %d violation(s)" % len(good_skill))
    print("selftest raw-slugify skill repo  -> %d violation(s)" % len(bad_slug))
    print("selftest dropped-id skill repo   -> %d violation(s)" % len(bad_drop))
    print("selftest bundled-dirName repo    -> %d violation(s)" % len(bad_bundled))
    print("selftest publish-outside-lock    -> %d violation(s)" % len(bad_publish))
    print("selftest no-lock recordSkillUse  -> %d violation(s)" % len(bad_nolock))
    print("selftest recordSkillUse removed  -> %d violation(s)" % len(bad_nofunc))
    print("selftest pinned override         -> %d violation(s)" % len(bad_pin))
    print("selftest compliant chat repo     -> %d violation(s)" % len(good_chat))
    print("selftest unwired-hook chat repo  -> %d violation(s)" % len(bad_chat))
    print("selftest unhooked sweep          -> %d violation(s)" % len(bad_sweep))
    print("selftest unregistered entry      -> %d violation(s)" % len(bad_unregistered))
    print("selftest delegating chat repo    -> %d violation(s)" % len(good_delegated))
    print("selftest empty helper            -> %d violation(s)" % len(bad_helper))
    print("selftest compliant MinisApp      -> %d violation(s)" % len(good_app))
    print("selftest late-wiring MinisApp    -> %d violation(s)" % len(bad_app))
    print("selftest MinisApp badge kept     -> %d violation(s)" % len(bad_badge))
    print("selftest MinisApp badge elsewhere-> %d violation(s)" % len(bad_badge_elsewhere))
    print("selftest compliant entry ext     -> %d violation(s)" % len(good_entry))
    print("selftest dropped webapp re-point -> %d violation(s)" % len(bad_entry))
    print("selftest re-point outside draft  -> %d violation(s)" % len(bad_entry_outside))
    print("selftest compliant sweep wiring    -> %d violation(s)" % len(good_sweep))
    print("selftest unwired startup sweep     -> %d violation(s)" % len(bad_sweep_unwired))
    print("selftest sweep without live set    -> %d violation(s)" % len(bad_sweep_no_live))
    print("selftest sweep off the IO region   -> %d violation(s)" % len(bad_sweep_on_main))

    # [dv24-b3 / deep-audit-1044 B-06] draft promotion persists the BODY.
    good_promote = check_promote_draft(GOOD_PROMOTE)
    if good_promote:
        failures.append("compliant promoteDraftIfNeeded fixture is not clean: %r" % good_promote)
    bad_title_only = check_promote_draft(BAD_PROMOTE_TITLE_ONLY)
    expect_trip("title-only promotion (B-06)", bad_title_only, "never persists the captured draft text",
                failures)
    # The pre-fix shape must fire ONLY C1: with no durable write there is no
    # ordering to judge, so C2 stays quiet and the arms stay attributable.
    if len(bad_title_only) != 1:
        failures.append("title-only promotion fixture tripped %d arms, expected exactly 1: %r"
                        % (len(bad_title_only), bad_title_only))
    bad_blank_first = check_promote_draft(BAD_PROMOTE_BLANK_FIRST)
    expect_trip("blank-before-persist promotion (B-06)", bad_blank_first, "BEFORE the durable body write",
                failures)
    if len(bad_blank_first) != 1:
        failures.append("blank-before-persist fixture tripped %d arms, expected exactly 1: %r"
                        % (len(bad_blank_first), bad_blank_first))
    bad_persist_early = check_promote_draft(BAD_PROMOTE_PERSIST_BEFORE_ENSURE)
    expect_trip("persist-before-ensure promotion (B-06)", bad_persist_early, "BEFORE 'ensureSession('",
                failures)

    # [dv24-b3] drawer visibility treats a preview as content.
    good_drawer = check_drawer_visibility(GOOD_DRAWER)
    if good_drawer:
        failures.append("compliant drawer visibility fixture is not clean: %r" % good_drawer)
    bad_inline = check_drawer_visibility(BAD_DRAWER_INLINE_FILTER)
    expect_trip("inline drawer filter (B-06)", bad_inline, "does not route through", failures)
    bad_no_count = check_drawer_visibility(BAD_DRAWER_NO_COUNT)
    expect_trip("drawer predicate without messageCount (B-06)", bad_no_count, "does not consult 'messageCount'",
                failures)
    if len(bad_no_count) != 1:
        failures.append("no-count drawer fixture tripped %d arms, expected exactly 1: %r"
                        % (len(bad_no_count), bad_no_count))
    bad_any_non_null = check_drawer_visibility(BAD_DRAWER_ANY_NON_NULL)
    expect_trip("drawer predicate accepting any non-null preview (B-06)", bad_any_non_null,
                "accepts any non-null preview", failures)
    if len(bad_any_non_null) != 1:
        failures.append("any-non-null drawer fixture tripped %d arms, expected exactly 1: %r"
                        % (len(bad_any_non_null), bad_any_non_null))

    print("selftest compliant promote draft  -> %d violation(s)" % len(good_promote))
    print("selftest title-only promotion     -> %d violation(s)" % len(bad_title_only))
    print("selftest blank-before-persist     -> %d violation(s)" % len(bad_blank_first))
    print("selftest persist-before-ensure    -> %d violation(s)" % len(bad_persist_early))
    print("selftest compliant drawer filter  -> %d violation(s)" % len(good_drawer))
    print("selftest inline drawer filter      -> %d violation(s)" % len(bad_inline))
    print("selftest drawer without count      -> %d violation(s)" % len(bad_no_count))
    print("selftest drawer any-non-null       -> %d violation(s)" % len(bad_any_non_null))

    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    print("SKILL_RESTORE_SESSION_CLEANUP_SELFTEST=PASS")
    return 0


def main():
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        return selftest()
    root = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(root)


if __name__ == "__main__":
    sys.exit(main())

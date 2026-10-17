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
  id. The rule: ChatRepository exposes an onSessionDeleted hook invoked AFTER
  the session row is gone from BOTH delete entry points, and MinisApp injects
  a callback that reaps both repositories (with skill/mcp constructed BEFORE
  chatRepository so the callback references the real instances).

Checks assert PRESENCE of the routing rather than the absence of a spelling,
so a refactor that keeps the contract is a deliberate edit to this gate rather
than a silent pass.

Usage: python3 scripts/scan/skill_restore_session_cleanup_check.py [repo_root]
       python3 scripts/scan/skill_restore_session_cleanup_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import os
import sys

SKILL_REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/SkillRepository.kt"
CHAT_REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/ChatRepository.kt"
MINIS_APP_REL = "src/android/app/src/main/java/com/rikkaminis/app/MinisApp.kt"

ADD_DECL = "fun add("
ADD_SIG_END = "): Skill? {"
RESOLVE_CALL = "SkillRestorePolicy.resolveImportId("
RETURN_ADD = "return add("
PREFERRED_ID = "preferredId ="
BUNDLED_DECL = "private fun installBundledSkill(dirName: String) {"
BUNDLED_NEXT = "private fun isAdoptableBundledRow("
DELETE_SESSION = "suspend fun deleteSession(id: String): SessionFileStore.DeleteResult? {"
DELETE_ROW_ONLY = "suspend fun deleteSessionRowOnly(id: String) {"
INVOKE_HOOK = "onSessionDeleted?.invoke("
CHATREPO_NEW = "chatRepository = ChatRepository("
SKILL_NEW = "skillRepository = SkillRepository("
MCP_NEW = "mcpRepository = MCPRepository("


def lines_of(src, needle):
    return [i + 1 for i, line in enumerate(src.split("\n")) if needle in line]


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


def check_chat_repo(src):
    v = []
    for decl, label in ((DELETE_SESSION, "deleteSession"), (DELETE_ROW_ONLY, "deleteSessionRowOnly")):
        s = slice_between(src, decl, "}", label, v)  # first '}' ends the decl's opening block marker only in fixtures
        if s is None:
            continue
        if INVOKE_HOOK not in s:
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
    except OSError as exc:
        print("skill_restore_session_cleanup_check: cannot read sources: %s" % exc)
        return 2
    violations = check_skill_repo(skill) + check_chat_repo(chat) + check_minis_app(app)
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
}
"""

BAD_CHAT_NO_HOOK = GOOD_CHAT.replace(
    "runCatching { onSessionDeleted?.invoke(id) }.onFailure { warn(it) }\n        return sessionFiles",
    "return sessionFiles",
).replace(
    "runCatching { onSessionDeleted?.invoke(id) }.onFailure { warn(it) }\n    }\n}",
    "    }\n}",
)

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
            },
        )
    }
}
"""

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

    expect_trip("raw-slugify fixture (ISSUE-01)", bad_slug, "does not resolve", failures)
    expect_trip("raw-slugify fixture (ISSUE-01)", bad_slug, "does not consult preferredId", failures)
    expect_trip("dropped-id fixture (ISSUE-01)", bad_drop, "does not pass 'preferredId ='", failures)
    expect_trip("bundled-dirName fixture (ISSUE-01)", bad_bundled, "other than its", failures)

    good_chat = check_chat_repo(GOOD_CHAT)
    bad_chat = check_chat_repo(BAD_CHAT_NO_HOOK)
    expect_trip("unwired-hook fixture (ISSUE-10)", bad_chat, "unwired", failures)

    good_app = check_minis_app(GOOD_APP)
    bad_app = check_minis_app(BAD_APP_ORDER)
    expect_trip("late-wiring fixture (ISSUE-10)", bad_app, "AFTER chatRepository", failures)

    print("selftest compliant skill repo    -> %d violation(s)" % len(good_skill))
    print("selftest raw-slugify skill repo  -> %d violation(s)" % len(bad_slug))
    print("selftest dropped-id skill repo   -> %d violation(s)" % len(bad_drop))
    print("selftest bundled-dirName repo    -> %d violation(s)" % len(bad_bundled))
    print("selftest compliant chat repo     -> %d violation(s)" % len(good_chat))
    print("selftest unwired-hook chat repo  -> %d violation(s)" % len(bad_chat))
    print("selftest compliant MinisApp      -> %d violation(s)" % len(good_app))
    print("selftest late-wiring MinisApp    -> %d violation(s)" % len(bad_app))

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

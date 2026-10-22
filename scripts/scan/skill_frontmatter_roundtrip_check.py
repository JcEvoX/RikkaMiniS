#!/usr/bin/env python3
"""
Gate: FIX-E (deep-audit-1023 ISSUE-05, P1) — SKILL.md frontmatter integrity on
the write path: memory-first merge base, update chain carries the block, and
the merge exit emits a parsable (single-delimiter) document.

ISSUE-05: writeSkillMd seeded its merge with `existing ?: skill.frontmatter`
(disk shadows the incoming block), update() had no frontmatter parameter (the
URL / file update chain could not hand the remote block over), and the merge
walk duplicated the opening `---` (an unparsable document).

Fix contract asserted here (code anchors, comments stripped before matching):

  arm1  resolveMergeBase is defined, its FIRST parameter is the fresh block
        (polarity lives in the parameter order)
  arm2  writeSkillMd calls resolveMergeBase(skill.frontmatter, existing) and
        the pre-fix expression `existing ?: skill.frontmatter` is gone
  arm3  update() gained `frontmatter: String? = null` and re-seeds its copy
        from the on-disk SKILL.md when the caller passes none
  arm4  updateFromURL passes `frontmatter = parsed.frontmatter...`
  arm5  ParsedSkill carries the block and parseSkillMd populates it
  arm6  importFromContent consumes parsed.frontmatter (replace + add branch)
  arm7  mergeFrontmatter's walk starts at line 1 — no doubled opening `---`
        (scoped to mergeFrontmatter; renameFrontmatterName's offset loop is a
        different, correct `0 until`)
  arm8  the SKILL.md viewer renders through the repository's single merge exit
        instead of hand-splicing a 3-key block
  arm9  the skills-BROWSER refresh (re-download of a catalogue SKILL.md) hands
        the parsed block to update() as well — same contract as arm4/arm8's
        file path; without it the on-disk block shadowed the incoming one
        (deep-audit-1030 ISSUE-02)
  arm10 [dv22-B6 §1042-B-07] the skills-BROWSER import path judges duplication
        BEFORE the full-fat import: it resolves the update target
        (resolveUpdateTargetId) first, hands that id to importFromGitHub as
        preferredId (else importFromContent's landing id is slugify(name), so
        a renamed / custom-id skill is duplicated instead of refreshed), and
        picks the HUD string on the dedup result — pre-fix importFromGitHub
        always returned non-null (its replace branch), so `if (skill != null)`
        was always true, every refresh claimed "imported", and the resolved-id
        refresh was unreachable dead code

Usage: python3 scripts/scan/skill_frontmatter_roundtrip_check.py [repo_root]
       python3 scripts/scan/skill_frontmatter_roundtrip_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import re
import sys

REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app"
SR_REL = REPO_REL + "/data/repository/SkillRepository.kt"
SM_REL = REPO_REL + "/ui/settings/SkillsManagementScreen.kt"
SB_REL = REPO_REL + "/ui/settings/MinisSkillsBrowserScreen.kt"

SELFTEST_MARK = "SKILL_FRONTMATTER_ROUNDTRIP_SELFTEST=PASS"

# ── anchors ──────────────────────────────────────────────────────────────
RESOLVE_DEF = "internal fun resolveMergeBase("
RESOLVE_FIRST_PARAM = "newFrontmatter: String?,"
WRITE_CALL = "resolveMergeBase(skill.frontmatter, existing)"
OLD_POLARITY = "existing ?: skill.frontmatter"
UPDATE_PARAM = "frontmatter: String? = null"
UPDATE_RESEED = "?: readSkillMdFrontmatter(id).ifBlank { current.frontmatter }"
URL_PASSES = "frontmatter = parsed.frontmatter.ifBlank { null }"
PARSED_FIELD = "val frontmatter: String = \"\""
PARSE_RETURNS = "frontmatter = SubagentSkill.extractFrontmatterBlock(trimmed)"
IMPORT_REPLACE = "frontmatter = parsed.frontmatter.ifBlank { current.frontmatter }"
IMPORT_ADD = "frontmatter = parsed.frontmatter,"
MERGE_WALK_FIXED = "for (i in 1 until frontmatterEnd)"
MERGE_WALK_DOUBLED = "for (i in 0 until frontmatterEnd)"
VIEWER_EXIT = "SkillRepository.mergeFrontmatter("
VIEWER_SPLICE = 'appendLine("name: '
DETAIL_PASSES = "frontmatter = parsed.frontmatter.ifBlank { null }"
# [da1030-A ISSUE-02] the browser refresh call site
SB_UPDATE_CALL = "skillRepository.update("
SB_UPDATE_DISCRIMINATOR = "id = targetId"
# [dv22-B6 §1042-B-07] the browser pre-import dedup chain. Decision points,
# not whole-line spellings: the resolution must happen before the full-fat
# import, the import call must carry the resolved id, and the success HUD must
# branch on the dedup result. The dedup VARIABLE NAME is derived from the
# `val x = resolveUpdateTargetId(` statement, so renaming it cannot silently
# satisfy (or break) the criteria — only removing the decision can.
SB_RESOLVE_STMT_RE = re.compile(
    r"val\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*resolveUpdateTargetId\(")
SB_IMPORT_CALL = "skillRepository.importFromGitHub("
SB_NULL_GUARD = "if (skill != null)"
SB_MSG_UPDATED = "R.string.skills_browser_success_updated"
SB_MSG_IMPORTED = "R.string.skills_browser_success_imported"


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
    """Replace string-literal CONTENTS with 'x' (length-preserving) so brace
    matching survives `${...}` templates."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            if text.startswith('"""', i):
                j = text.find('"""', i + 3)
                j = n if j < 0 else j + 3
                out.append('x' * (j - i))
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


def function_body(text_nc, text_masked, signature):
    """The {...} block of the function whose signature matches `signature`
    (first occurrence), via brace matching on the string-masked text."""
    idx = text_nc.find(signature)
    if idx < 0:
        return None
    open_idx = text_masked.find("{", idx)
    if open_idx < 0:
        return None
    depth = 0
    for i in range(open_idx, len(text_masked)):
        if text_masked[i] == "{":
            depth += 1
        elif text_masked[i] == "}":
            depth -= 1
            if depth == 0:
                return text_nc[idx:i + 1]
    return None


def call_arg_block(text, start):
    """The substring of `text` from `start` (the callee name) through the ')'
    matching the call's opening '(' — paren-depth walk, braces ignored (a
    trailing lambda is a separate concern). Returns None when unbalanced."""
    i = text.find("(", start)
    if i < 0:
        return None
    depth = 0
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None

def run_checks_text(sr_text, sm_text, sb_text=None):
    sr_nc = strip_comments(sr_text)
    sm_nc = strip_comments(sm_text)
    sr_masked = mask_strings(sr_nc)
    sm_masked = mask_strings(sm_nc)

    failures = []

    def check(name, cond, detail=""):
        if not cond:
            failures.append("%s%s" % (name, (" — " + detail) if detail else ""))

    # arm1+2: polarity, single exit, call-site ordering
    check("arm1 resolveMergeBase defined", RESOLVE_DEF in sr_nc)
    check("arm1 first param is the fresh block",
          RESOLVE_FIRST_PARAM in sr_nc,
          "polarity lives in the parameter order (memory-first)")
    check("arm2 writeSkillMd uses the single exit (caller block first)",
          WRITE_CALL in sr_nc)
    check("arm2 pre-fix polarity expression gone",
          OLD_POLARITY not in sr_nc,
          "`existing ?: skill.frontmatter` still present (code, not comment)")

    # arm3: update() carries + re-seeds
    update_body = function_body(sr_nc, sr_masked, "fun update(")
    check("arm3 update() signature has frontmatter param",
          update_body is not None and UPDATE_PARAM in update_body.split("): Boolean")[0])
    check("arm3 update() re-seeds from disk when no block passed",
          update_body is not None and UPDATE_RESEED in update_body)

    # arm4: updateFromURL passes the remote block
    url_body = function_body(sr_nc, sr_masked, "suspend fun updateFromURL(")
    if url_body is None:
        url_body = function_body(sr_nc, sr_masked, "fun updateFromURL(")
    check("arm4 updateFromURL passes parsed.frontmatter",
          url_body is not None and "frontmatter = parsed.frontmatter" in url_body)

    # arm5: ParsedSkill carries the block; parseSkillMd populates it
    check("arm5 ParsedSkill has the frontmatter field", PARSED_FIELD in sr_nc)
    parse_body = function_body(sr_nc, sr_masked, "private fun parseSkillMd(")
    check("arm5 parseSkillMd returns the block",
          parse_body is not None and PARSE_RETURNS in parse_body)

    # arm6: importFromContent consumes parsed.frontmatter (both branches)
    import_body = function_body(sr_nc, sr_masked, "fun importFromContent(")
    check("arm6 importFromContent replace branch uses parsed.frontmatter",
          import_body is not None and IMPORT_REPLACE in import_body)
    check("arm6 importFromContent add branch uses parsed.frontmatter",
          import_body is not None and IMPORT_ADD in import_body)
    check("arm6 no per-site re-extraction left in importFromContent",
          import_body is not None and "extractFrontmatterBlock(content)" not in import_body)

    # arm7: the merge walk must not duplicate the opening delimiter
    merge_body = function_body(sr_nc, sr_masked, "internal fun mergeFrontmatter(")
    check("arm7 mergeFrontmatter walk starts at line 1",
          merge_body is not None and MERGE_WALK_FIXED in merge_body)
    check("arm7 doubled walk gone from mergeFrontmatter",
          merge_body is not None and MERGE_WALK_DOUBLED not in merge_body,
          "renameFrontmatterName's `0 until` offset loop is a different, correct site")

    # arm8: the viewer renders through the single merge exit
    viewer_idx = sm_nc.find("val initialContent = remember")
    viewer_seg = sm_nc[viewer_idx:viewer_idx + 4000] if viewer_idx >= 0 else ""
    check("arm8 viewer renders through the repository merge exit",
          VIEWER_EXIT in viewer_seg)
    check("arm8 hand-spliced 3-key block gone from the viewer",
          VIEWER_SPLICE not in viewer_seg)
    # the detail "update from file" path passes the block too
    check("arm8 update-from-file passes parsed.frontmatter",
          DETAIL_PASSES in sm_nc)

    # arm9 [da1030-A ISSUE-02]: the BROWSE-tab refresh path (re-parse of a
    # downloaded SKILL.md) must hand the parsed block over as well. Scoped to
    # the refresh call site — identified by its `id = targetId` argument — so
    # the unrelated pure-rename call `update(skill.id, name = trimmed)` in the
    # settings list cannot satisfy (or shadow) the criterion, and so a second
    # update() call added later in the file cannot silently stand in for it.
    sb_nc = strip_comments(sb_text if sb_text is not None else "")
    call_at = sb_nc.find(SB_UPDATE_CALL)
    call_block = call_arg_block(sb_nc, call_at) if call_at >= 0 else None
    if call_block is None:
        check("arm9 browser refresh call site found", False,
              "'%s' not found in MinisSkillsBrowserScreen.kt (ROT — update this gate)"
              % SB_UPDATE_CALL)
    else:
        check("arm9 browser refresh call site is the resolved-id one",
              SB_UPDATE_DISCRIMINATOR in call_block,
              "expected '%s' in the update() arguments (ROT — update this gate)"
              % SB_UPDATE_DISCRIMINATOR)
        check("arm9 browser refresh passes parsed.frontmatter",
              DETAIL_PASSES in call_block,
              "update() re-seeds from the on-disk SKILL.md when no block is "
              "passed, so the incoming subagent / max_turns / allowed_tools are "
              "shadowed by the disk copy")

    # arm10 [dv22-B6 §1042-B-07]: the browser import judges duplication BEFORE
    # the full-fat import. The dedup variable name is derived from the
    # `val x = resolveUpdateTargetId(` statement so a rename cannot satisfy
    # the criteria — only the decision chain can.
    import_at = sb_nc.find(SB_IMPORT_CALL)
    if import_at < 0:
        check("arm10 browser import call site found", False,
              "'%s' not found in MinisSkillsBrowserScreen.kt (ROT — update this gate)"
              % SB_IMPORT_CALL)
    else:
        m = SB_RESOLVE_STMT_RE.search(sb_nc)
        check("arm10 browser resolves the update target before importing",
              m is not None and m.start() < import_at,
              "expected a `val <x> = resolveUpdateTargetId(` statement before "
              "the importFromGitHub call — without it the import always "
              "duplicated a renamed / custom-id skill and always claimed "
              "'imported' (ROT — update this gate)")
        if m is None:
            check("arm10 browser import carries the resolved id", False,
                  "no resolveUpdateTargetId statement to carry (ROT — update this gate)")
            check("arm10 HUD message branches on the dedup result", False,
                  "no resolveUpdateTargetId statement to branch on (ROT — update this gate)")
        else:
            dedup_var = m.group(1)
            import_block = call_arg_block(sb_nc, import_at)
            check("arm10 browser import carries the resolved id",
                  import_block is not None and
                  ("preferredId = %s" % dedup_var) in import_block,
                  "expected 'preferredId = %s' in the importFromGitHub "
                  "arguments — without the passthrough importFromContent lands "
                  "under slugify(name) and a local record under a different id "
                  "is duplicated instead of refreshed (ROT — update this gate)"
                  % dedup_var)
            # scope the message check to the success branch: from the import
            # call to the next resolveUpdateTargetId( (the fallback re-resolve)
            # — the fallback's own 'updated' string must not satisfy this.
            dedup2 = sb_nc.find("resolveUpdateTargetId(", import_at)
            seg_end = min(dedup2 if dedup2 >= 0 else len(sb_nc), import_at + 3000)
            msg_seg = sb_nc[import_at:seg_end]
            guard_at = msg_seg.find(SB_NULL_GUARD)
            check("arm10 HUD message branches on the dedup result",
                  SB_NULL_GUARD in msg_seg and
                  SB_MSG_UPDATED in msg_seg and SB_MSG_IMPORTED in msg_seg and
                  ("%s != null" % dedup_var) in msg_seg,
                  "expected the success HUD to pick %s / %s on '%s != null' "
                  "inside the if (skill != null) branch (ROT — update this gate)"
                  % (SB_MSG_UPDATED, SB_MSG_IMPORTED, dedup_var))

    return failures


def run_checks(repo_root):
    sr = open(os.path.join(repo_root, SR_REL), encoding="utf-8").read()
    sm = open(os.path.join(repo_root, SM_REL), encoding="utf-8").read()
    sb = open(os.path.join(repo_root, SB_REL), encoding="utf-8").read()
    return run_checks_text(sr, sm, sb)


# ── selftest fixtures ────────────────────────────────────────────────────

GOOD_SR = '''package com.rikkaminis.app.data.repository

class SkillRepository {
    companion object {
        internal fun mergeFrontmatter(
            existing: String?,
            name: String,
            description: String,
            version: String,
            body: String,
        ): String {
            val raw = existing?.trimStart().orEmpty()
            val src = raw.lines()
            var frontmatterEnd = -1
            if (raw.startsWith("---") && src.isNotEmpty()) {
                for (i in 1 until src.size) {
                    if (src[i].trim() == "---") { frontmatterEnd = i; break }
                }
            }
            if (frontmatterEnd < 0) {
                return buildString {
                    appendLine("---")
                    appendLine("name: " + name)
                    appendLine("---")
                    append(body)
                }
            }
            val fm = mutableListOf<String>()
            for (i in 1 until frontmatterEnd) {
                fm.add(src[i])
            }
            return buildString {
                appendLine("---")
                for (line in fm) appendLine(line)
                appendLine("---")
                append(body)
            }
        }

        internal fun resolveMergeBase(
            newFrontmatter: String?,
            diskContent: String?,
        ): String? = newFrontmatter?.ifBlank { null } ?: diskContent
    }

    private fun writeSkillMd(skill: Skill) {
        val dir = java.io.File("/tmp/skills", skill.id)
        dir.mkdirs()
        val file = java.io.File(dir, "SKILL.md")
        val existing = runCatching { file.readText() }.getOrNull()
        val mergeBase = resolveMergeBase(skill.frontmatter, existing)
        val content = mergeFrontmatter(
            existing = mergeBase,
            name = skill.name,
            description = skill.description,
            version = skill.version,
            body = skill.body,
        )
        file.writeText(content)
    }

    suspend fun updateFromURL(skillId: String, content: String): Boolean {
        val parsed = parseSkillMd(content) ?: return false
        val ok = update(
            id = skillId,
            name = parsed.name,
            description = parsed.description,
            body = parsed.body,
            version = parsed.version,
            frontmatter = parsed.frontmatter.ifBlank { null },
        )
        return ok
    }

    private fun readSkillMdFrontmatter(id: String): String = "---"

    fun update(
        id: String,
        name: String? = null,
        description: String? = null,
        body: String? = null,
        version: String? = null,
        frontmatter: String? = null,
    ): Boolean {
        val current = _skills.value.find { it.id == id } ?: return false
        val updated = current.copy(
            name = name ?: current.name,
            frontmatter = frontmatter?.ifBlank { null }
                ?: readSkillMdFrontmatter(id).ifBlank { current.frontmatter },
        )
        return true
    }

    data class ParsedSkill(
        val name: String,
        val description: String,
        val version: String = "1.0.0",
        val body: String,
        val frontmatter: String = "",
    )

    private fun parseSkillMd(content: String): ParsedSkill? {
        val trimmed = content.trimStart()
        if (!trimmed.startsWith("---")) return null
        return ParsedSkill(
            "n", "d", "1.0.0", "b",
            frontmatter = SubagentSkill.extractFrontmatterBlock(trimmed),
        )
    }

    fun importFromContent(content: String): Skill? {
        val parsed = parseSkillMd(content) ?: return null
        val id = "x"
        if (id.isNotBlank() && _skills.value.any { it.id == id }) {
            val current = _skills.value.first { it.id == id }
            val replaced = current.copy(
                frontmatter = parsed.frontmatter.ifBlank { current.frontmatter },
            )
            return replaced
        }
        return add(
            name = parsed.name,
            frontmatter = parsed.frontmatter,
        )
    }
}
'''

GOOD_SM = '''package com.rikkaminis.app.ui.settings

val initialContent = remember(skillId, relativePath, skill.updatedAt) {
    if (isSkillMd) {
        SkillRepository.mergeFrontmatter(
            existing = skill.frontmatter.ifBlank { null },
            name = skill.name,
            description = skill.description,
            version = skill.version ?: "1.0.0",
            body = skill.body,
        )
    } else {
        skillRepository.readSkillFile(skillId, relativePath) ?: ""
    }
}

// viewer save path passes the block too:
val ok = skillRepository.update(
    id = skill.id,
    name = parsed.name,
    version = parsed.version,
    frontmatter = parsed.frontmatter.ifBlank { null },
)
'''

# dirty variants — each flips exactly one arm
DIRTY_SR_POLARITY = GOOD_SR.replace(
    "resolveMergeBase(skill.frontmatter, existing)",
    "existing ?: skill.frontmatter.ifBlank { null }").replace(
    "): String? = newFrontmatter?.ifBlank { null } ?: diskContent",
    "): String? = diskContent ?: newFrontmatter?.ifBlank { null }")
DIRTY_SR_WALK = GOOD_SR.replace("for (i in 1 until frontmatterEnd) {\n                fm.add(src[i])",
                                "for (i in 0 until frontmatterEnd) {\n                fm.add(src[i])")
DIRTY_SR_UPDATE = GOOD_SR.replace("        frontmatter: String? = null,\n    ): Boolean {",
                                  "    ): Boolean {")
DIRTY_SR_PARSE = GOOD_SR.replace(
    "            frontmatter = SubagentSkill.extractFrontmatterBlock(trimmed),\n        )\n    }\n\n    fun importFromContent",
    "        )\n    }\n\n    fun importFromContent")
DIRTY_SM_SPLICE = GOOD_SM.replace(
    """        SkillRepository.mergeFrontmatter(
            existing = skill.frontmatter.ifBlank { null },
            name = skill.name,
            description = skill.description,
            version = skill.version ?: "1.0.0",
            body = skill.body,
        )""",
    """        buildString {
            appendLine("---")
            appendLine("name: " + skill.name)
            appendLine("---")
            append(skill.body)
        }""")

# [da1030-A ISSUE-02] browser-refresh fixture: the call site the gate scopes to
# is the resolved-id one (`id = targetId`). [dv22-B6] extended with the
# pre-import dedup chain arm10 pins: resolve before import, carry the id, and
# branch the HUD string on the dedup result.
GOOD_SB = '''package com.rikkaminis.app.ui.settings

internal suspend fun refreshFromCatalogue(skillRepository: SkillRepository) {
    val parsed = skillRepository.parseSkillMdPublic(content)
    val matchedId = resolveUpdateTargetId(rows, slug = slugify(parsed.name), incomingName = parsed.name)
    val skill = skillRepository.importFromGitHub(url, preferredId = matchedId)
    if (skill != null) {
        val messageRes =
            if (matchedId != null) R.string.skills_browser_success_updated
            else R.string.skills_browser_success_imported
        onStateChange(HudState.SUCCESS, context.getString(messageRes, skill.name))
        return
    }
    val targetId = resolveUpdateTargetId(rows, slugify(parsed.name), parsed.name) ?: slugify(parsed.name)
    val updated = withContext(Dispatchers.IO) {
        skillRepository.update(
            id = targetId,
            name = parsed.name,
            description = parsed.description,
            version = parsed.version,
            body = parsed.body,
            frontmatter = parsed.frontmatter.ifBlank { null },
        )
    }
}
'''

# The pre-fix shape: the refresh kept its own block and let update() re-seed
# from the on-disk SKILL.md.
DIRTY_SB_DROP = GOOD_SB.replace(
    "            frontmatter = parsed.frontmatter.ifBlank { null },\n", "")

# Precision control: the block is present in the file, but on a DIFFERENT
# update() call — the criterion is scoped to the refresh site, so this is still
# a violation (a file-wide `"frontmatter = parsed.frontmatter" in sb_nc` check
# would have been satisfied here and gone green on the real defect).
DIRTY_SB_BLOCK_ELSEWHERE = DIRTY_SB_DROP.replace(
    "    val updated = withContext(Dispatchers.IO) {\n",
    "    skillRepository.update(id = otherId, frontmatter = parsed.frontmatter.ifBlank { null })\n"
    "    val updated = withContext(Dispatchers.IO) {\n")

# [dv22-B6 §1042-B-07] dirty variants — each flips exactly the arm10 decision
# it names, keeping arm9 green (the update() call site is untouched).

# The pre-fix shape: import before resolve (no dedup at all).
DIRTY_SB_NO_DEDUP = GOOD_SB.replace(
    """    val matchedId = resolveUpdateTargetId(rows, slug = slugify(parsed.name), incomingName = parsed.name)
    val skill = skillRepository.importFromGitHub(url, preferredId = matchedId)
""",
    """    val skill = skillRepository.importFromGitHub(url)
""")

# Resolve happens but the id never reaches the import call — the landing id
# falls back to slugify(name) and a custom-id record is duplicated.
DIRTY_SB_NO_PASSTHROUGH = GOOD_SB.replace(
    "skillRepository.importFromGitHub(url, preferredId = matchedId)",
    "skillRepository.importFromGitHub(url)")

# The id is carried but the HUD always claims "imported" — the message lies
# about the in-place refresh (the original symptom).
DIRTY_SB_ALWAYS_IMPORTED = GOOD_SB.replace(
    """        val messageRes =
            if (matchedId != null) R.string.skills_browser_success_updated
            else R.string.skills_browser_success_imported
""",
    """        val messageRes = R.string.skills_browser_success_imported
""")


def selftest():
    # tempfile.mkdtemp resolves through the guest-tmp translation (host-side
    # path the sandbox cannot reopen) — run the checks on in-memory text
    # instead; the repo-root wrapper above is the only filesystem path.
    ok = True

    def run(sr_text, sm_text, sb_text=GOOD_SB):
        return run_checks_text(sr_text, sm_text, sb_text)

    f = run(GOOD_SR, GOOD_SM)
    print("selftest clean tree: %s" % ("PASS" if not f else "FAIL %s" % f))
    ok &= not f

    for label, sr_t, sm_t, sb_t in [
        ("polarity reverted", DIRTY_SR_POLARITY, GOOD_SM, GOOD_SB),
        ("delimiter walk reverted", DIRTY_SR_WALK, GOOD_SM, GOOD_SB),
        ("update() loses frontmatter param", DIRTY_SR_UPDATE, GOOD_SM, GOOD_SB),
        ("parseSkillMd stops returning the block", DIRTY_SR_PARSE, GOOD_SM, GOOD_SB),
        ("viewer hand-splice returns", GOOD_SR, DIRTY_SM_SPLICE, GOOD_SB),
        ("browser refresh drops the block", GOOD_SR, GOOD_SM, DIRTY_SB_DROP),
        ("browser block on the wrong call", GOOD_SR, GOOD_SM, DIRTY_SB_BLOCK_ELSEWHERE),
        ("browser import without dedup", GOOD_SR, GOOD_SM, DIRTY_SB_NO_DEDUP),
        ("browser dedup id not carried", GOOD_SR, GOOD_SM, DIRTY_SB_NO_PASSTHROUGH),
        ("browser HUD always says imported", GOOD_SR, GOOD_SM, DIRTY_SB_ALWAYS_IMPORTED),
    ]:
        f = run(sr_t, sm_t, sb_t)
        red = len(f) > 0
        print("selftest dirty %-38s: %s %s" % (label, "RED" if red else "NOT-RED", f[:1]))
        ok &= red

    print(SELFTEST_MARK if ok else "SKILL_FRONTMATTER_ROUNDTRIP_SELFTEST=FAIL")
    return 0 if ok else 1


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    failures = run_checks(repo)
    if failures:
        print("FAIL %s" % SR_REL)
        for f in failures:
            print("  - %s" % f)
        sys.exit(1)
    print("OK %s + %s + %s (17 arms)" % (SR_REL, SM_REL, SB_REL))
    sys.exit(0)


if __name__ == "__main__":
    main()

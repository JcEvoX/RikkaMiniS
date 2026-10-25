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

def function_spans(text_nc, text_masked, signature):
    """(start, end) of the declaration at `signature` — end just past the '}'
    that closes its body. Offsets are valid in BOTH texts (masking is
    length-preserving), so a structural walk on `text_masked` can be read back
    as source on `text_nc`."""
    idx = text_nc.find(signature)
    if idx < 0:
        return None
    ob = text_masked.find("{", idx)
    if ob < 0:
        return None
    depth = 0
    for i in range(ob, len(text_masked)):
        if text_masked[i] == "{":
            depth += 1
        elif text_masked[i] == "}":
            depth -= 1
            if depth == 0:
                return (idx, i + 1)
    return None


def stmt_end(text, start):
    """Index of the newline that ENDS the statement/expression at `start`.

    Bracket-depth aware, and a line ending in an operator (or a next line that
    starts with `?:` / `.`) continues the statement — Kotlin's expression-bodied
    helpers wrap their body across lines and a naive `find("\\n")` would slice
    the polarity check down to its first line."""
    depth = 0
    i, n = start, len(text)
    while i < n:
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "\n" and depth <= 0:
            seg = text[start:i].rstrip()
            j = i + 1
            while j < n and text[j] in " \t":
                j += 1
            if seg.endswith(("?:", "=", "&&", "||", "+", ".", ",", "(")) or \
                    text[j:j + 3].startswith(("?:", "?.")) or text[j:j + 1] == ".":
                i += 1
                continue
            return i
        i += 1
    return n


def split_top_level(text, sep=","):
    """Split on `sep` at bracket depth 0 (strings already masked)."""
    out, buf, depth = [], [], 0
    for c in text:
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        if c == sep and depth <= 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(c)
    out.append("".join(buf))
    return out


def top_level_index(text, token):
    """Index of `token` at bracket depth 0, or -1."""
    depth = 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth <= 0 and text.startswith(token, i):
            return i
        i += 1
    return -1


def param_names(text_masked, sig_idx):
    """Parameter names of the declaration at `sig_idx` (annotation/val prefixes
    and types dropped), or [] when the parameter list cannot be parsed."""
    op = text_masked.find("(", sig_idx)
    if op < 0:
        return []
    depth = 0
    cp = -1
    for i in range(op, len(text_masked)):
        if text_masked[i] == "(":
            depth += 1
        elif text_masked[i] == ")":
            depth -= 1
            if depth == 0:
                cp = i
                break
    if cp < 0:
        return []
    inner = text_masked[op + 1:cp]
    names = []
    for part in split_top_level(inner):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(?:@\w+\s+)*(?:(?:val|var|noinline|crossinline)\s+)*([A-Za-z_]\w*)\s*:", part)
        names.append(m.group(1) if m else None)
    return names


def check_resolve_polarity(sr_nc, sr_masked):
    """arm1 (dataflow): resolveMergeBase's BODY must read the fresh block first.

    The pre-fix criterion only checked the parameter ORDER, on the assumption
    that "polarity lives in the signature". It does not: a body that swaps the
    two operands (`diskContent?.ifBlank { null } ?: newFrontmatter`) keeps the
    signature intact, silently returns the on-disk block for every skill that
    already exists on disk, and the incoming subagent / max_turns /
    allowed_tools block is dropped — ISSUE-05's headline defect, verbatim.

    The criterion: the FIRST declared parameter is the guarded (left) operand
    of the body's top-level elvis, the SECOND is the fallback. Anything we
    cannot derive fail-closes (a `when`-shaped rewrite must re-point this arm)."""
    idx = sr_nc.find(RESOLVE_DEF)
    if idx < 0:
        return []  # the presence arm already reports the missing declaration
    op = sr_masked.find("(", idx)
    if op < 0:
        return ["arm1 resolveMergeBase: no parameter list (fail-closed)"]
    depth = 0
    cp = -1
    for i in range(op, len(sr_masked)):
        if sr_masked[i] == "(":
            depth += 1
        elif sr_masked[i] == ")":
            depth -= 1
            if depth == 0:
                cp = i
                break
    if cp < 0:
        return ["arm1 resolveMergeBase: unbalanced parameter list (fail-closed)"]
    head_masked = sr_masked[op:cp + 1]
    names = param_names(head_masked, 0)
    if len(names) < 2 or not names[0] or not names[1]:
        return ["arm1 resolveMergeBase: cannot derive its parameter names (fail-closed — "
                "re-point this arm at the new signature)"]
    # The expression body starts at the first `=` after the parameter list —
    # and a `{` before it means a block body, which this arm cannot judge.
    k, eq = cp + 1, -1
    while k < len(sr_masked):
        c = sr_masked[k]
        if c == "=" and sr_masked[k + 1:k + 2] != "=" and sr_masked[k - 1:k] not in ("!", "<", ">"):
            eq = k
            break
        if c in "{;" or c == "\n":
            break
        k += 1
    if eq < 0:
        return ["arm1 resolveMergeBase: no expression body to inspect (block body or a "
                "signature split across lines — fail-closed: re-point this arm)"]
    expr_start = eq + 1
    expr_end = stmt_end(sr_masked, expr_start)
    expr_masked = sr_masked[expr_start:expr_end]
    expr = sr_nc[expr_start:expr_end]
    elvis = top_level_index(expr_masked, "?:")
    if elvis < 0:
        return ["arm1 resolveMergeBase: the merge base is no longer a memory-first elvis "
                "(`new block ?: disk`) — cannot prove the fresh block wins (fail-closed: "
                "re-point this arm at the new shape)"]
    left = expr[:elvis]
    right = expr[elvis + 2:]
    out = []
    if names[0] not in left:
        out.append(
            "arm1 resolveMergeBase does not GUARD the fresh block — `%s` (the first "
            "parameter) does not appear before the top-level `?:`, so the on-disk copy "
            "shadows the incoming subagent / max_turns / allowed_tools block "
            "(body reads: %s)" % (names[0], " ".join(expr.split())[:120]))
    if names[1] not in right:
        out.append(
            "arm1 resolveMergeBase: the fallback side of `?:` does not read `%s` (the "
            "disk parameter) — polarity is inverted or a third source was introduced "
            "(body reads: %s)" % (names[1], " ".join(expr.split())[:120]))
    return out


MERGE_WALK_RE = re.compile(
    r"for\s*\(\s*i\s+in\s+1\s+until\s+frontmatterEnd\s*\)\s*\{")


def _block_at(text, open_idx):
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx:i + 1]
    return None


def check_merge_seed(sr_nc, sr_masked):
    """arm7 (dataflow): the collected-lines list must START EMPTY.

    The pre-fix criterion pinned the walk's lower bound (`1 until
    frontmatterEnd`), i.e. it proved only that the loop skips the source's
    opening delimiter. Seeding the list instead — `mutableListOf("---")` — puts
    a second `---` in the emitted document, which then no longer parses as a
    frontmatter block: every save of a skill silently corrupts its SKILL.md.

    Criterion: derive the accumulator from the walk, then require its
    initializer (and anything added before the walk) to carry no `---`."""
    span = function_spans(sr_nc, sr_masked, "internal fun mergeFrontmatter(")
    if span is None:
        return []  # presence is reported by the walk arm
    start, end = span
    body_masked = sr_masked[start:end]
    body = sr_nc[start:end]
    m = MERGE_WALK_RE.search(body_masked)
    if m is None:
        return []  # the walk arm owns the missing/broken walk
    loop = _block_at(body_masked, m.end() - 1) or ""
    fm_add = re.search(r"(\w+)\s*\.\s*add\(", loop)
    if fm_add is None:
        return ["arm7 mergeFrontmatter: cannot derive the list the walk fills (no "
                "`<list>.add(` inside the `1 until frontmatterEnd` loop) — fail-closed: "
                "re-point this arm"]
    var = fm_add.group(1)
    d = re.search(r"\bval\s+%s\s*=\s*" % re.escape(var), body_masked)
    if d is None:
        return ["arm7 mergeFrontmatter: cannot derive the initializer of `%s` (the list the "
                "walk fills) — fail-closed: re-point this arm" % var]
    init_end = stmt_end(body_masked, d.end())
    init = body[d.end():init_end]
    out = []
    if "---" in init:
        out.append(
            "arm7 mergeFrontmatter seeds `%s` with a delimiter literal (%s) — the emitted "
            "document opens with a doubled `---`, so the written SKILL.md no longer parses "
            "as frontmatter" % (var, " ".join(init.split())[:80]))
    pre = body[:m.start()]
    if re.search(r"\b%s\s*\+=\s*[^\n]*---" % re.escape(var), pre) or \
            re.search(r"\b%s\s*\.\s*add(?:All)?\s*\([^)]*---" % re.escape(var), pre):
        out.append(
            "arm7 mergeFrontmatter adds a delimiter literal to `%s` before the walk — the "
            "opening `---` would be emitted twice" % var)
    if '---' not in body:
        out.append("arm7 mergeFrontmatter never emits an opening `---` — the merged document "
                   "has no frontmatter delimiter at all")
    return out


RESOLVE_TARGET_CALL = "resolveUpdateTargetId("
EMPTY_COLL_RE = re.compile(
    r"^(?:empty(?:List|Set|Sequence|Array|Map)|listOf|mutableListOf|setOf|mutableSetOf|"
    r"sequenceOf|mapOf|mutableMapOf|arrayListOf)\s*(?:<[^()]*>)?\s*\(\s*\)$")
LITERAL_COLL_RE = re.compile(
    r"^(?:listOf|mutableListOf|setOf|mutableSetOf|arrayListOf|sequenceOf)\s*(?:<[^()]*>)?\s*\(")


def resolve_target_first_arg_problems(sb_nc):
    """arm10 (dataflow): the update-target resolution must be fed the LIVE
    candidate set.

    The pre-fix criterion proved only that a `val x = resolveUpdateTargetId(`
    statement sits before the import and that its result is carried. Passing
    `emptyList()` satisfies all of that: the resolver can never match, so every
    update of an already-installed skill duplicates it (or lands a new copy
    under slugify(name)) — the exact defect arm10 exists for.

    Criterion: every call site (except the declaration) is checked at the
    ARGUMENT level: the candidate set must not be an empty / literal constant.
    An unparseable call fail-closes."""
    out = []
    for m in re.finditer(re.escape(RESOLVE_TARGET_CALL), sb_nc):
        prefix = sb_nc[max(0, m.start() - 24):m.start()]
        if re.search(r"\bfun\s+$", prefix):
            continue  # the declaration itself
        block = call_arg_block(sb_nc, m.start())
        if block is None:
            out.append("arm10 resolveUpdateTargetId call at offset %d: arguments do not "
                       "balance (fail-closed)" % m.start())
            continue
        inner = block[block.find("(") + 1:-1]
        args = split_top_level(inner)
        first = args[0] if args else ""
        flat = re.sub(r"\s+", " ", first).strip()
        if not flat:
            out.append("arm10 resolveUpdateTargetId is called with no candidate set — the "
                       "dedup can never resolve an existing skill, so every update "
                       "duplicates it")
        elif EMPTY_COLL_RE.match(flat) or flat == "null":
            out.append(
                "arm10 resolveUpdateTargetId is fed a CONSTANT candidate set (`%s`) — the "
                "resolver can never match an installed skill, so the update path is dead "
                "code and every refresh duplicates the skill" % flat)
        elif LITERAL_COLL_RE.match(flat) and "." not in flat:
            out.append(
                "arm10 resolveUpdateTargetId is fed a literal list (`%s`) instead of the "
                "live skill set — the dedup decision is detached from the repository "
                "(%s)" % (flat[:60], "fail-closed" if "." not in flat else ""))
    return out


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
    # arm1 (dataflow) — the signature alone does not prove polarity: the body
    # must GUARD the fresh block. Derived from the expression body, so an
    # operand swap (or a third source) goes red instead of passing on the
    # parameter order.
    failures.extend(check_resolve_polarity(sr_nc, sr_masked))

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
    # arm7 (dataflow) — the walk's lower bound proves only that the SOURCE's
    # opening delimiter is skipped; seeding the accumulator puts a second `---`
    # into the emitted document instead.
    failures.extend(check_merge_seed(sr_nc, sr_masked))

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

    # arm10 (dataflow): the resolution must be fed the LIVE candidate set — a
    # `emptyList()` satisfies every structural arm above while making the
    # dedup decision permanently false.
    failures.extend(resolve_target_first_arg_problems(sb_nc))

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

# ── wide-1024 witnesses (each mechanically reproduced on the real tree:
# pre-fix rc=0, post-fix rc=1) ────────────────────────────────────────────

# W1 (dataflow): the merge base's OPERANDS swap while the signature keeps the
# fresh block first — "polarity lives in the parameter order" is not true.
DIRTY_SR_POLARITY_BODY = GOOD_SR.replace(
    "): String? = newFrontmatter?.ifBlank { null } ?: diskContent",
    "): String? = diskContent?.ifBlank { null } ?: newFrontmatter")

# W2 (dataflow): the accumulator is SEEDED with a delimiter, so the walk's
# `1 until` lower bound is satisfied while the document gets a doubled `---`.
DIRTY_SR_SEED_DELIM = GOOD_SR.replace(
    "            val fm = mutableListOf<String>()",
    '            val fm = mutableListOf("---")')

# W3 (dataflow): the dedup resolver is fed a constant candidate set — every
# structural arm10 criterion above still holds.
DIRTY_SB_EMPTY_TARGETS = GOOD_SB.replace(
    "resolveUpdateTargetId(rows, slug = slugify(parsed.name), incomingName = parsed.name)",
    "resolveUpdateTargetId(emptyList(), slug = slugify(parsed.name), incomingName = parsed.name)")


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

    for entry in [
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
        # wide-1024 witnesses — the needle pins WHICH criterion fired, so a
        # fixture cannot go red on an unrelated arm and count as evidence.
        ("merge base operands swapped", DIRTY_SR_POLARITY_BODY, GOOD_SM, GOOD_SB,
         "does not GUARD the fresh block"),
        ("merge list seeded with a delimiter", DIRTY_SR_SEED_DELIM, GOOD_SM, GOOD_SB,
         "seeds `fm` with a delimiter literal"),
        ("dedup fed a constant candidate set", GOOD_SR, GOOD_SM, DIRTY_SB_EMPTY_TARGETS,
         "CONSTANT candidate set"),
    ]:
        label, sr_t, sm_t, sb_t = entry[:4]
        needle = entry[4] if len(entry) > 4 else None
        f = run(sr_t, sm_t, sb_t)
        red = len(f) > 0
        hit = needle is None or any(needle in x for x in f)
        print("selftest dirty %-38s: %s %s"
              % (label, "RED" if (red and hit) else ("WRONG-ARM" if red else "NOT-RED"), f[:1]))
        ok &= red and hit

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

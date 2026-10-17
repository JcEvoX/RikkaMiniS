#!/usr/bin/env python3
"""
Gate: FIX-E (deep-audit-1021 ISSUE-06, P1) — SkillFileViewerScreen must
consume the save result, never silently pop the screen.

ISSUE-06: the Save callback of `SkillFileViewerScreen` dropped the return
values of `SkillRepository.importFromContent` / `writeSkillFile` and then
unconditionally executed `isEditing = false; onBack()` — a broken SKILL.md
frontmatter made importFromContent return null without writing anything, so
the user's whole edit vanished with no error shown. The save also ran
synchronously on the main thread (SQLite + file I/O).

Fix contract asserted here (presence of the corrected wiring, not absence of
a spelling — a refactor that keeps the contract is a deliberate gate edit):

  arm1  the `importFromContent(` call in the screen is consumed (`) != null`)
  arm2  the save runs inside `val ok = withContext(Dispatchers.IO) { ... }`
        and both importFromContent and writeSkillFile live in that block
  arm3  `if (ok)` guards the exit: its block contains `isEditing = false`
        and `onBack()`, and `isEditing = false` occurs exactly once in the
        screen (no unconditional pop sequence outside the guard)
  arm4  `withContext(Dispatchers.IO)` is present in the screen
  arm5  the `preferredId = skillId` pin survives (audit-1015-b4-2 contract)

Usage: python3 scripts/scan/skill_file_save_result_check.py [repo_root]
       python3 scripts/scan/skill_file_save_result_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import sys

TARGET_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/SkillsManagementScreen.kt"
SCREEN_ANCHOR = "fun SkillFileViewerScreen("

CONSUME_IMPORT = "importFromContent("
IO_CTX = "withContext(Dispatchers.IO)"
OK_BINDING = "val ok ="
OK_GUARD = "if (ok)"
PREFERRED_ID = "preferredId = skillId"
EXIT_ASSIGN = "isEditing = false"
EXIT_CALL = "onBack()"


def strip_comments(text):
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


def find_matching_brace(text, open_idx):
    """Index of the '}' matching the '{' at open_idx (no strings expected in
    the block bodies this gate extracts — string templates with braces do not
    occur inside the withContext / if(ok) blocks)."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def skip_ws(text, idx):
    while idx < len(text) and text[idx] in " \t\r\n":
        idx += 1
    return idx


def screen_segment(repo_root):
    path = os.path.join(repo_root, TARGET_REL)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    idx = src.find(SCREEN_ANCHOR)
    if idx < 0:
        raise SystemExit("FATAL: %s not found in %s" % (SCREEN_ANCHOR, TARGET_REL))
    # The screen is the last top-level function in the file.
    seg = strip_comments(src[idx:])
    return seg


def balanced_block(text, after_idx, opener="{"):
    """Return the {...} block whose '{' is the first non-ws char at/after
    after_idx, or None."""
    i = skip_ws(text, after_idx)
    if i >= len(text) or text[i] != opener:
        return None
    close = find_matching_brace(text, i)
    if close < 0:
        return None
    return text[i : close + 1]


def run_checks(seg):
    failures = []

    # arm1: importFromContent consumed with `) != null`
    arm1 = False
    pos = seg.find(CONSUME_IMPORT)
    if pos >= 0:
        close = seg.find(")", seg.find("(", pos))
        if close >= 0:
            nxt = skip_ws(seg, close + 1)
            arm1 = seg.startswith("!=", nxt)
    if not arm1:
        failures.append("arm1: importFromContent( call is not consumed with ') != null'")

    # arm2: save is bound via `val ok = withContext(...) { ... }` and both
    # importFromContent and writeSkillFile live in that block. (The IO
    # dispatcher itself is arm4's contract, so a dispatcher-only mutation
    # flips exactly arm4.)
    arm2 = False
    ctx = seg.find("withContext(")
    if ctx >= 0:
        pre = seg[max(0, ctx - 40) : ctx]
        open_paren = seg.find("(", ctx)
        close_paren = seg.find(")", open_paren)
        blk = balanced_block(seg, close_paren + 1)
        arm2 = OK_BINDING in pre and blk is not None \
            and CONSUME_IMPORT in blk and "writeSkillFile(" in blk
    if not arm2:
        failures.append(
            "arm2: save must run inside 'val ok = withContext(Dispatchers.IO) { ... }' "
            "with both importFromContent and writeSkillFile in the block"
        )

    # arm3: if (ok) guards the exit; no unconditional pop
    arm3 = False
    guard = seg.find(OK_GUARD)
    if guard >= 0:
        blk = balanced_block(seg, guard + len(OK_GUARD))
        exit_assigns = seg.count(EXIT_ASSIGN)
        arm3 = (
            blk is not None
            and EXIT_CALL in blk
            and EXIT_ASSIGN in blk
            and exit_assigns == 1
        )
    if not arm3:
        failures.append(
            "arm3: 'if (ok)' must guard isEditing=false + onBack(), and "
            "'isEditing = false' must occur exactly once in the screen"
        )

    # arm4: Dispatchers.IO present in the screen
    arm4 = IO_CTX in seg
    if not arm4:
        failures.append("arm4: withContext(Dispatchers.IO) missing from the screen")

    # arm5: preferredId pin survives
    arm5 = PREFERRED_ID in seg
    if not arm5:
        failures.append("arm5: preferredId = skillId pin missing (audit-1015-b4-2 contract)")

    return failures


# -- selftest fixtures (trimmed mirror of the fixed screen) --

COMPLIANT = """fun SkillFileViewerScreen(skillId: String, relativePath: String, onBack: () -> Unit) {
    var isEditing by remember { mutableStateOf(false) }
    var isSaving by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val snackbarHostState = remember { SnackbarHostState() }
    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) },
        topBar = {
            actions = {
                if (isEditing) {
                    MinisTextButton(onClick = {
                        if (isSaving) return@MinisTextButton
                        isSaving = true
                        val contentToSave = editContent
                        scope.launch {
                            val ok = withContext(Dispatchers.IO) {
                                if (isSkillMd) {
                                    skillRepository.importFromContent(
                                        contentToSave,
                                        skill.importSource,
                                        preferredId = skillId,
                                    ) != null
                                } else {
                                    skillRepository.writeSkillFile(skillId, relativePath, contentToSave)
                                }
                            }
                            isSaving = false
                            if (ok) {
                                isEditing = false
                                onBack()
                            } else {
                                snackbarHostState.showSnackbar(invalidFormatMsg)
                            }
                        }
                    }) { Text(stringResource(R.string.skill_file_save)) }
                } else {
                    MinisTextButton(onClick = {
                        editContent = initialContent
                        isEditing = true
                    }) { Text(stringResource(R.string.skill_file_edit)) }
                }
            }
        },
    ) { padding -> if (isEditing) { DialogTextField() } else { LazyColumn() } }
}
"""

# V1: bare importFromContent call (result dropped) -> arm1 red
V1 = COMPLIANT.replace(
    """skillRepository.importFromContent(
                                        contentToSave,
                                        skill.importSource,
                                        preferredId = skillId,
                                    ) != null""",
    """skillRepository.importFromContent(
                                        contentToSave,
                                        skill.importSource,
                                        preferredId = skillId,
                                    )""",
)

# V2: writeSkillFile hoisted out of the withContext block (synchronous save
# outside) -> arm2 red only
V2 = COMPLIANT.replace(
    """                                } else {
                                    skillRepository.writeSkillFile(skillId, relativePath, contentToSave)
                                }""",
    """                                } else {
                                    false
                                }""",
).replace(
    "val contentToSave = editContent",
    """val contentToSave = editContent
                        skillRepository.writeSkillFile(skillId, relativePath, contentToSave)""",
)

# V3: unconditional pop (no if (ok) guard) -> arm3 red
V3 = COMPLIANT.replace(
    """if (ok) {
                                isEditing = false
                                onBack()
                            } else {""",
    """if (ok) {
                                snackbarHostState.showSnackbar(invalidFormatMsg)
                            }
                            isEditing = false
                            onBack()
                            if (ok) {""",
)

# V4: synchronous main-thread save (no Dispatchers.IO) -> arm4 red only
V4 = COMPLIANT.replace("withContext(Dispatchers.IO)", "withContext(Dispatchers.Default)")

# V5: preferredId pin dropped -> arm5 red only
V5 = COMPLIANT.replace("preferredId = skillId,", 'preferredId = "other",')


def selftest():
    failures = []
    # compliant fixture: all arms green
    f = run_checks(strip_comments(COMPLIANT))
    if f:
        failures.append("compliant fixture unexpectedly red: %s" % f)
    # each violation fixture flips exactly its own arm red
    expectations = [("V1", V1, "arm1"), ("V2", V2, "arm2"), ("V3", V3, "arm3"),
                    ("V4", V4, "arm4"), ("V5", V5, "arm5")]
    all_arms = ["arm1", "arm2", "arm3", "arm4", "arm5"]
    for name, fixture, arm in expectations:
        got = run_checks(strip_comments(fixture))
        red = [a for a in all_arms if any(a in x for x in got)]
        if red != [arm]:
            failures.append("%s: expected exactly [%s] red, got %s" % (name, arm, red))
    if failures:
        for x in failures:
            print("SELFTEST FAIL: " + x)
        print("SKILL_FILE_SAVE_RESULT_SELFTEST=FAIL")
        return 1
    print("selftest OK: compliant green + 5 violation fixtures each flip exactly their own arm")
    print("SKILL_FILE_SAVE_RESULT_SELFTEST=PASS")
    return 0


def main():
    if "--selftest" in sys.argv[1:]:
        return selftest()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = args[0] if args else "."
    seg = screen_segment(root)
    failures = run_checks(seg)
    if failures:
        print("skill_file_save_result_check: %d violation(s) in %s" % (len(failures), TARGET_REL))
        for x in failures:
            print("  - " + x)
        return 1
    print("skill_file_save_result_check: OK (5/5 arms) on %s" % TARGET_REL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

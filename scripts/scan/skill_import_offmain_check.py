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

Usage: python3 scripts/scan/skill_import_offmain_check.py [repo_root]
       python3 scripts/scan/skill_import_offmain_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import sys

SM_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/SkillsManagementScreen.kt"
CALL = "skillRepository.importFromContent(pasteContent)"
OPENER = "withContext(Dispatchers.IO)"

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


def run_checks(repo_root):
    path = os.path.join(repo_root, SM_REL)
    text = open(path, encoding="utf-8").read()
    nc = strip_comments(text)
    masked = mask_strings(nc)
    ok, detail = inside_io_wrap(nc, masked)
    failures = []
    if not ok:
        failures.append("paste import not on IO — %s" % detail)
    return failures


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

    print(SELFTEST_MARK if ok else "SKILL_IMPORT_OFFMAIN_SELFTEST=FAIL")
    return 0 if ok else 1


def run_checks_text(text):
    nc = strip_comments(text)
    masked = mask_strings(nc)
    ok, detail = inside_io_wrap(nc, masked)
    failures = []
    if not ok:
        failures.append("paste import not on IO — %s" % detail)
    return failures


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    failures = run_checks(repo)
    if failures:
        print("FAIL %s" % SM_REL)
        for f in failures:
            print("  - %s" % f)
        sys.exit(1)
    print("OK %s (2 arms)" % SM_REL)
    sys.exit(0)


if __name__ == "__main__":
    main()

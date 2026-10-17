#!/usr/bin/env python3
"""[fix/da1023-G] memory_ui_wiring_check

deep-audit-1023 ISSUE-07 gate. Two files, three defect surfaces:

  A. MemoryManagementScreen.kt — `MemoryFileRow` received a nullable
     `onDelete` callback from its caller (built for every non-global,
     non-SOUL file) and the screen carries a full delete-confirmation
     dialog, but the row body NEVER consumed the callback: the daily-log
     delete feature was 100% unreachable dead code. The gate requires the
     row body to actually invoke `onDelete` from an IconButton click
     (code anchors only; the KDoc that explains the wiring must not
     satisfy it).

  B. MemoryManagementScreen.kt — the confirm path ran blocking
     `deleteFile()` + full `listAllFiles()` on the main thread (MemoryRepository
     has no suspend surface). Every `memoryRepository.deleteFile(` /
     `memoryRepository.listAllFiles(` site in the file must now sit inside
     a `withContext(Dispatchers.IO)` region, and the confirm block must go
     through `scope.launch` (dialog state cleared first, result published
     back on the composition thread).

  C. SessionMemorySheet.kt — `autoItems` was `remember(memoryRepository,
     context) { buildAutoInjectedItems(...) }`: both keys stable for the
     life of the sheet, so the cached snapshot (full file contents!) went
     stale after every in-sheet edit. The gate requires:
       - a refresh-tick remember key on the very remember that builds the
         auto items;
       - the tick bumped on ALL THREE success paths that rewrite the
         backing files (AutoFile save / Write-record save / Write revoke).

Comments are stripped before every judgment (the fix ships with prose
comments that name each criterion; they must not carry the gate).

Usage: python3 memory_ui_wiring_check.py <repo_root>
       python3 memory_ui_wiring_check.py --root <repo_root>
       python3 memory_ui_wiring_check.py --selftest
Exit 0 = pass, 1 = violation, 2 = usage error.
"""
import re
import sys

MEM_SCREEN = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/MemoryManagementScreen.kt"
MEM_SHEET = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/SessionMemorySheet.kt"
MARKER = "MEMORY_UI_WIRING_SELFTEST=PASS"


def strip_comments(src: str) -> str:
    """Remove // and /* */ comments, honouring string literals."""
    out = []
    i = 0
    n = len(src)
    in_str = False
    while i < n:
        c = src[i]
        if in_str:
            if c == "\\":
                out.append(src[i:i + 2])
                i += 2
                continue
            if c == '"':
                in_str = False
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def brace_slice(src: str, sig: str, from_pos: int = 0) -> str:
    """Char-offset slice from `sig` through the brace it opens (balanced).

    `from_pos` matters when the signature appears several times: each
    `withContext(Dispatchers.IO) {` match must slice ITS OWN block, not the
    first one in the file (the selftest caught the always-slice-first bug:
    the second IO region's deleteFile site looked unguarded)."""
    i = src.find(sig, from_pos)
    if i < 0:
        return ""
    depth = 0
    started = False
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
            started = True
        elif c == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
    return src[i:] if started else ""


def check(screen_src: str, sheet_src: str) -> list:
    errs = []
    scr = strip_comments(screen_src)
    sht = strip_comments(sheet_src)

    # ---- A. MemoryFileRow must consume onDelete in its body ----
    row = brace_slice(scr, "private fun MemoryFileRow(")
    if not row:
        errs.append("MemoryFileRow not found in " + MEM_SCREEN + " — gate ROT")
    else:
        refs = len(re.findall(r"\bonDelete\b", row))
        if refs < 2:
            errs.append(
                f"MemoryFileRow body references onDelete {refs} time(s) "
                "(param only) — the row consumes the delete callback nowhere; "
                "the daily-log delete stays unreachable")
        if not re.search(r"IconButton\s*\(\s*onClick\s*=\s*onDelete\b", row) \
                and not re.search(r"IconButton\s*\(\s*onClick\s*=\s*\{\s*onDelete\?\.\s*invoke\(\)\s*\}", row):
            errs.append(
                "MemoryFileRow has no IconButton(onClick = onDelete) — no "
                "rendered handle can reach the confirm dialog")

    # ---- B. every blocking repository call site is inside Dispatchers.IO ----
    io_slices = []
    for m in re.finditer(r"withContext\s*\(\s*Dispatchers\.IO\s*\)\s*\{", scr):
        sl = brace_slice(scr, m.group(0), from_pos=m.start())
        if sl:
            io_slices.append((m.start(), m.start() + len(sl)))
    for call in ("memoryRepository.deleteFile(", "memoryRepository.listAllFiles("):
        for m in re.finditer(re.escape(call), scr):
            pos = m.start()
            if not any(a <= pos < b for a, b in io_slices):
                errs.append(
                    f"'{call}' at offset {pos} runs outside any "
                    "withContext(Dispatchers.IO) region — main-thread file IO")
    confirm = brace_slice(scr, "if (deleteFileName != null) {")
    if not confirm:
        errs.append("delete-confirmation block not found — gate ROT")
    else:
        if "scope.launch" not in confirm:
            errs.append(
                "delete confirm is synchronous — expected scope.launch + "
                "withContext(Dispatchers.IO) (state must clear before launch)")
        if "withContext(Dispatchers.IO)" not in confirm:
            errs.append("delete confirm has no withContext(Dispatchers.IO)")

    # ---- C. autoItems snapshot must rebuild on every in-sheet mutation ----
    tick_decl = re.search(r"var\s+autoItemsRefreshTick\s+by\s+remember", sht)
    if not tick_decl:
        errs.append("no 'var autoItemsRefreshTick by remember' refresh signal")
    if not re.search(
            r"remember\s*\([^)]*autoItemsRefreshTick[^)]*\)\s*\{[^{}]*buildAutoInjectedItems",
            sht):
        errs.append(
            "autoItems remember does not key on autoItemsRefreshTick — "
            "edits made in this sheet leave the list (and the cached file "
            "contents the editor re-opens) permanently stale")
    bumps = len(re.findall(r"autoItemsRefreshTick\+\+", sht))
    if bumps < 3:
        errs.append(
            f"autoItemsRefreshTick bumped {bumps} time(s), expected >= 3 "
            "(AutoFile save + Write save + Write revoke)")

    return errs


def selftest() -> int:
    good_screen = """
val scope = rememberCoroutineScope()
LaunchedEffect(Unit) {
    files = withContext(Dispatchers.IO) { memoryRepository.listAllFiles() }
}
if (deleteFileName != null) {
    AlertDialog(
        confirmButton = {
            MinisTextButton(onClick = {
                val name = deleteFileName
                deleteFileName = null
                if (name != null) {
                    scope.launch {
                        val refreshed = withContext(Dispatchers.IO) {
                            memoryRepository.deleteFile(name)
                            memoryRepository.listAllFiles()
                        }
                        files = refreshed
                    }
                }
            }) { Text("x") }
        },
    )
}
@Composable
private fun MemoryFileRow(
    file: MemoryRepository.MemoryFileInfo,
    onClick: () -> Unit,
    onDelete: (() -> Unit)?,
) {
    Row {
        Icon(Icons.Filled.Delete, null)
        if (onDelete != null) {
            IconButton(onClick = onDelete) { Icon(Icons.Filled.Delete, null) }
        }
    }
}
"""
    good_sheet = """
var autoItemsRefreshTick by remember { mutableStateOf(0) }
val autoItems = remember(memoryRepository, context, autoItemsRefreshTick) {
    buildAutoInjectedItems(context, memoryRepository)
}
autoItemsRefreshTick++
autoItemsRefreshTick++
autoItemsRefreshTick++
"""

    v1_screen = good_screen.replace(
        "        if (onDelete != null) {\n"
        "            IconButton(onClick = onDelete) { Icon(Icons.Filled.Delete, null) }\n"
        "        }\n", "")
    v2_screen = good_screen.replace(
        "                        val refreshed = withContext(Dispatchers.IO) {\n"
        "                            memoryRepository.deleteFile(name)\n"
        "                            memoryRepository.listAllFiles()\n"
        "                        }\n"
        "                        files = refreshed\n",
        "                        files = memoryRepository.listAllFiles()\n"
        "                        memoryRepository.deleteFile(name)\n").replace(
        "files = withContext(Dispatchers.IO) { memoryRepository.listAllFiles() }",
        "files = memoryRepository.listAllFiles()")
    v3_sheet = good_sheet.replace(
        "remember(memoryRepository, context, autoItemsRefreshTick)",
        "remember(memoryRepository, context)")
    v4_sheet = good_sheet.replace("autoItemsRefreshTick++", "", 1)
    # comment trap: prose naming every criterion must NOT make a stripped tree pass
    v1_comment_screen = """
// [fix] IconButton(onClick = onDelete) wires the row; the confirm path uses
// scope.launch and withContext(Dispatchers.IO).
""" + v1_screen
    v5_sheet = good_sheet.replace(
        "var autoItemsRefreshTick by remember { mutableStateOf(0) }",
        "// autoItemsRefreshTick\nval tick = remember { mutableStateOf(0) }")

    arms = [
        ("compliant pair", good_screen, good_sheet, 0),
        ("V1 row never consumes onDelete", v1_screen, good_sheet, 1),
        ("V1b comment-only wiring (comments stripped)", v1_comment_screen, good_sheet, 1),
        ("V2 blocking repository calls outside Dispatchers.IO", v2_screen, good_sheet, 1),
        ("V3 autoItems remember has no refresh key", good_screen, v3_sheet, 1),
        ("V4 only two tick bumps", good_screen, v4_sheet, 1),
        ("V5 tick is prose-only / not a state var", good_screen, v5_sheet, 1),
    ]
    failures = 0
    for name, s1, s2, expect_red in arms:
        errs = check(s1, s2)
        if expect_red and not errs:
            print(f"  FAIL {name}: expected red, got green")
            failures += 1
        elif not expect_red and errs:
            print(f"  FAIL {name}: expected green, got {errs}")
            failures += 1
        else:
            print(f"  OK {name}: {'red: ' + errs[0] if errs else 'green'}")
    if failures:
        print(f"SELFTEST FAILED ({failures} arms)")
        return 1
    print(MARKER)
    return 0


def main() -> int:
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
    elif len(args) == 1:
        root = args[0]
    else:
        print("usage: <repo_root> | --root <repo_root> | --selftest", file=sys.stderr)
        return 2
    import os
    p1 = os.path.join(root, MEM_SCREEN)
    p2 = os.path.join(root, MEM_SHEET)
    try:
        s1 = open(p1).read()
        s2 = open(p2).read()
    except OSError as e:
        print(f"cannot read target: {e}", file=sys.stderr)
        return 2
    errs = check(s1, s2)
    if errs:
        print(f"memory_ui_wiring_check RED on {MEM_SCREEN} / {MEM_SHEET}:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print(f"memory_ui_wiring_check OK ({MEM_SCREEN}, {MEM_SHEET})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

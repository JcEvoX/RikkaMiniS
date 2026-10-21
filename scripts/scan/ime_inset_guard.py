#!/usr/bin/env python3
"""IME-inset guard — RikkaMinis CI scan gate.

## Why this gate exists

2026-09-20, user-reported: in the skill-file editor and the memory-file editor,
tapping the text field let the soft keyboard cover the content being edited.

Root cause (library sources, compose-bom 2025.09.00 → material3 1.3.2 /
foundation 1.9.1): the app is edge-to-edge (`MainActivity` enableEdgeToEdge +
manifest `adjustResize`). Under edge-to-edge `adjustResize` no longer resizes
the window for the IME, so Compose must consume `WindowInsets.ime` itself.

Host containers differ in whether they consume it:

  SettingsScaffold    → .imePadding() + verticalScroll   (SettingsComponents.kt)
  ModalBottomSheet    → contentWindowInsets = safeDrawing.only(Bottom),
                        and safeDrawing ⊇ ime             (SheetDefaults.kt:316)
  StandardChatSheet   → wraps ModalBottomSheet
  bare Scaffold       → contentWindowInsets = systemBars ONLY (Scaffold.kt:293)
  AlertDialog/Dialog  → independent Window, platform decides

So a text field hosted by a bare `Scaffold` has NOTHING shrinking it above the
keyboard. `BasicTextField` does scroll its own caret into view
(`CoreTextField.bringSelectionEndIntoView`), but its bounds extend behind the
IME, so the caret lands behind the keyboard. Silent: no crash, no failing test,
no log line — just a field you cannot see while typing.

This is not a one-off. The same class was fixed piecemeal at least four times
before anyone noticed the pattern: `BrowserSettingsSheet`, `MCPIntegrationsScreen`
(GH#44), `EnvironmentVariablesScreen`, `SettingsComponents` (T183) — and then
again in 2026-09-20 for the two user-reported editors plus four more hosts.
Per the change-ladder rule "same family recurring ≥2× must be fixed at the
shared layer", a mechanical gate is the fix for the recurrence; fixing the
sixth call site by hand is not.

## What this gate flags

For every text-input call site in production sources, find the innermost
enclosing composable. If that composable's body contains a bare `Scaffold(`,
does NOT contain a protecting host (`SettingsScaffold` / `ModalBottomSheet` /
`StandardChatSheet`), and has no `.imePadding()` anywhere in the body, flag it.

Escape hatch — TWO forms, both inline and therefore immune to line-number rot:

  `ime-ok: <reason>`     the site is intentionally unprotected (e.g. the host is
                         an AlertDialog with its own Window; the platform
                         pans/resizes to keep the focused field visible).
                         Put it in the composable, same or previous line.

  `ime-unreachable: <reason>`  the bare Scaffold and the text field are in
                         mutually exclusive branches (e.g. a `Scaffold` for one
                         state, a sheet for another), so the heuristic's
                         "same composable" grouping is a false positive.

Do NOT silence this with a line-numbered allow-list — those rot on the first
refactor. The escape reasons are greppable, reviewable and survive edits.

## Known ceiling (read this before trusting a green run)

The judge is a STATIC HEURISTIC over "same composable", not a dataflow proof.
It cannot see:
  - new host containers (a future `BasicAlertDialog`, a hand-rolled `Window`,
    a `Popup`) — add them to PROTECTING only after verifying they consume IME;
  - runtime-only facts (a host that conditionally consumes IME).
It only prevents the SAME static shape from coming back. Real-device
verification stays mandatory for any new editor surface — the 2026-09-20 round
was found by the user, not by any scanner.

Usage: python3 scripts/scan/ime_inset_guard.py <repo_root>
Exit:  0 = clean, 1 = violation(s) found
"""
import os
import re
import sys

SRC_ROOTS = [
    "src/android/app/src/main/java",
]

# 文本输入构件：直接的 Compose 输入框 + 本仓的共享包装
FIELD_RE = re.compile(
    r"(?<![\w.])("
    r"BasicTextField|OutlinedTextField|TextField|"
    r"DialogTextField|SectionTextField|MemoryFileEditorContent"
    r")\s*\("
)

# 裸 Scaffold —— 两种调用形态都要覆盖：`Scaffold(...) { }` 与尾随 lambda
# `Scaffold { }`（后者无括号；漏掉它会让 ApiKeyStep / ModelSelectionStep 这类
# 站点整体消失，判据假阴性 —— 实测踩过）。
BARE_SCAFFOLD_RE = re.compile(r"(?<![\w.])Scaffold\s*(?:\(|\{)")

# 已知会消费 IME 的宿主（每一条都要有库源码或本仓包装的依据，见模块 KDoc）
# 同样覆盖括号与尾随 lambda 两种形态。
PROTECTING_RE = re.compile(
    r"(?<![\w.])(SettingsScaffold|ModalBottomSheet|StandardChatSheet)\s*(?:\(|\{)"
)

IME_PADDING_RE = re.compile(r"\.imePadding\(\)")

FUN_RE = re.compile(
    r"^(\s*)(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:private\s+|internal\s+|public\s+|override\s+)?"
    r"fun\s+(?:<[^>]*>\s*)?([A-Za-z0-9_]+)\s*\("
)

BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT_RE = re.compile(r"//.*$")
ESCAPE_RE = re.compile(r"ime-(?:ok|unreachable)\s*:")


def _blank_block(m):
    # 保留内部换行数，其余字符抹成空格
    return "\n" * m.group(0).count("\n")


def strip_comments(text):
    """去掉注释，但保留含逃生口的行（否则标记自己会被剥掉）。

    两条硬约束（都是实测踩出来的）：

    1. **行数必须严格不变** —— 否则报告的行号会偏移、函数跨度会错位。
       所以块注释用「等长替换、保留换行」的方式抹掉，而不是整体删除。

    2. **块注释只能在同一行内闭合** —— 不能用 `/*.*?*/` 跨行匹配。
       Kotlin 里 `"*/*"`（如 `fileLauncher.launch("*/*")`）会被裸正则当成
       块注释起点，然后一路吞到几百行外的下一个 `*/`，把中间所有函数签名
       抹平（实测：SkillsManagementScreen.kt:526 的 `"*/*"` 吞掉了 963 行
       前的全部代码，导致 SkillDetailScreen 整段消失、判据假阴性）。
       这里改成按行处理：只剥「行内成对」的块注释。
    """
    out = []
    for line in text.split("\n"):
        if line.lstrip().startswith("//"):
            out.append(line if ESCAPE_RE.search(line) else "//")
            continue
        # 行内块注释：同一行内 /* ... */ 成对
        line = re.sub(r"/\*.*?\*/", "", line)
        out.append(LINE_COMMENT_RE.sub("", line))
    return out


def function_spans(lines):
    """返回 [(name, start, end)] —— 用「下一个函数起点」当上一个的终点。

    刻意用这个粗糙口径：本仓 composable 极少嵌套定义（嵌套会让内层被外层
    吞掉，但那种情况下外层通常也含同样的宿主，判据仍成立）。KDoc 里记了
    这个天花板。
    """
    funcs = []
    for i, line in enumerate(lines):
        m = FUN_RE.match(line)
        if m:
            funcs.append([m.group(2), i, len(m.group(1))])
    for k in range(len(funcs)):
        funcs[k].append(funcs[k + 1][1] if k + 1 < len(funcs) else len(lines))
    return funcs


def scan_file(path, rel):
    lines = strip_comments(open(path, encoding="utf-8", errors="replace").read())
    funcs = function_spans(lines)
    findings = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("import", "*")):
            continue
        if not FIELD_RE.search(line):
            continue
        # 最内层（跨度最小）的封闭函数 = 该输入点所属的 composable
        encl = [f for f in funcs if f[1] <= i < f[3]]
        if not encl:
            continue
        name, start, _indent, end = min(encl, key=lambda f: f[3] - f[1])
        body_lines = lines[start:end]
        body = "\n".join(body_lines)
        if not BARE_SCAFFOLD_RE.search(body):
            continue          # 宿主不是裸 Scaffold ⇒ 不在本门判据内
        if PROTECTING_RE.search(body):
            continue          # 同时有保护宿主 ⇒ 启发式判不准，放行
        if IME_PADDING_RE.search(body):
            continue          # 已消费 IME ⇒ 安全
        # 逃生口：向上连续回看注释/空行（多行理由注释很常见），
        # 以及函数签名头两行。
        window = list(body_lines[:2])
        j = i - 1
        while j >= 0 and (not lines[j].strip() or lines[j].lstrip().startswith("//")):
            window.append(lines[j])
            j -= 1
        window.append(lines[i])
        if any(ESCAPE_RE.search(l) for l in window):
            continue
        findings.append((i + 1, name))
    return findings


# ─── [deep-audit-1040 B-01] SettingsScaffold must render a back affordance ───
#
# `SettingsScaffold` is the shared host this gate already owns (it is in the host
# table in the module KDoc above). Its `navigationSlot` used to honour ONLY the
# `navigation` parameter, so the ten call sites that passed just `onBack`
# (provider detail / connection, storage session, runtime limits, Shizuku and
# offload permissions, provider add, custom model add, chat tuning) rendered an
# EMPTY navigation slot — no back arrow at all, even though the parameter KDoc
# had always promised "the slot falls back to the back arrow iff onBack is set".
# The system back gesture still worked, which is exactly why it survived review:
# a user who does not know the gesture had no visible way out of the screen.
#
# Same family as the IME class above — a shared host whose contract and whose
# rendering disagreed, silently — so it is graded at the shared layer instead of
# one screen at a time.
#
# Graded:
#   B01-SLOT   inside the slot body: BOTH arms exist (`navigation != null` AND
#              `onBack != null`) and the onBack arm renders an IconButton wired
#              to `onBack`.
#   B01-CALLS  the number of production `SettingsScaffold(...)` call sites
#              passing a non-null `onBack` stays at or above the recorded floor.
#              Fail-closed in both directions: an anchor that stopped matching
#              (regex rot) and a screen that lost its back handler both surface
#              as a drop.
#
# Deliberately NOT graded: a top-level screen passing NEITHER parameter keeps an
# empty slot. That is the documented design (rely on the system back gesture /
# bottom navigation); flagging it would force a back arrow onto screens that are
# not drill-downs.
SCAFFOLD_REL = "com/rikkaminis/app/ui/settings/SettingsComponents.kt"
NAV_SLOT_DECL_RE = re.compile(r"\bval\s+navigationSlot\s*:")
NAV_ARM_RE = re.compile(r"\bnavigation\s*!=\s*null\s*->")
ONBACK_ARM_RE = re.compile(r"\bonBack\s*!=\s*null\s*->")
ONBACK_RENDER_RE = re.compile(r"IconButton\s*\([\s\S]{0,160}?onClick\s*=\s*onBack\b")
SCAFFOLD_CALL_RE = re.compile(r"(?<![\w.])SettingsScaffold\s*\(")
# Capture the VALUE rather than asserting a negative lookahead: `\s*=(?!null)`
# can backtrack the whitespace and match `onBack = null` anyway (measured: the
# lookahead form counted 26 of 27 call sites, i.e. essentially every one).
ONBACK_ARG_RE = re.compile(r"\bonBack\s*=\s*([^,\n)]+)")
# 10 call sites in 9 files pass `onBack` on 2026-10-21; the floor is one below
# that so a legitimate merge of two screens does not false-alarm while regex rot
# (which would collapse the count) still does.
MIN_ONBACK_CALLSITES = 9


def balanced_args(text, open_idx):
    """Text inside the parenthesis at `open_idx`, or None when unbalanced.

    Comments are stripped before this runs, so a stray paren inside a string
    literal is the only way it can drift — the same ceiling `strip_comments`
    documents for the IME half of this gate.
    """
    depth = 0
    for i in range(open_idx, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
    return None


def braced_block(text, open_idx):
    """Body of the brace block starting at `open_idx` (the `{`), exclusive."""
    depth = 0
    for i in range(open_idx, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
    return None


def check_scaffold_navigation(text):
    """B01-* — see the block comment above. Returns [(criterion, ok, detail)]."""
    results = []
    m = NAV_SLOT_DECL_RE.search(text)
    if m is None:
        results.append(("B01-SLOT", False,
                        "no `val navigationSlot` in SettingsComponents.kt (fail-closed: the host "
                        "was reworked — re-derive this criterion)"))
        return results
    brace = text.find("{", m.end())
    slot = braced_block(text, brace) if brace >= 0 else None
    if slot is None:
        results.append(("B01-SLOT", False,
                        "`navigationSlot` has no brace body (fail-closed: re-derive)"))
        return results
    problems = []
    if not NAV_ARM_RE.search(slot):
        problems.append("the slot no longer honours the custom `navigation` slot")
    if not ONBACK_ARM_RE.search(slot):
        problems.append("the slot has no `onBack != null` arm — every screen that passes only "
                        "`onBack` renders an empty navigation slot again")
    elif not ONBACK_RENDER_RE.search(slot):
        problems.append("the `onBack` arm does not render an `IconButton(onClick = onBack)`")
    results.append(("B01-SLOT", not problems,
                    "the navigation slot honours the custom slot, and falls back to a back arrow "
                    "when only `onBack` is set" if not problems else "; ".join(problems)))
    return results


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    violations = []
    scaffold_violations = []
    scaffold_seen = False
    onback_calls = 0
    for sr in SRC_ROOTS:
        for base, _dirs, files in os.walk(os.path.join(root, sr)):
            for fn in sorted(files):
                if not fn.endswith(".kt"):
                    continue
                path = os.path.join(base, fn)
                rel = os.path.relpath(path, os.path.join(root, sr)).replace(os.sep, "/")
                for line_no, fname in scan_file(path, rel):
                    violations.append(f"{rel}:{line_no}  in {fname}()")
                # [deep-audit-1040 B-01] the shared settings host's navigation
                # affordance (see the block comment above the criteria).
                text = "\n".join(strip_comments(
                    open(path, encoding="utf-8", errors="replace").read()))
                if rel == SCAFFOLD_REL:
                    scaffold_seen = True
                    for cid, ok, detail in check_scaffold_navigation(text):
                        if not ok:
                            scaffold_violations.append("%s  %s" % (cid, detail))
                for cm in SCAFFOLD_CALL_RE.finditer(text):
                    args = balanced_args(text, cm.end() - 1)
                    if args is None:
                        continue
                    vm = ONBACK_ARG_RE.search(args)
                    if vm is not None and vm.group(1).strip() != "null":
                        onback_calls += 1

    # The call-site floor only applies when the host file is actually in the tree:
    # test_scan.py builds single-file fixture trees (no SettingsComponents.kt) and
    # a missing host is not this gate's business — an unconditional floor would
    # red every one of those exit-0 cases.
    if scaffold_seen and onback_calls < MIN_ONBACK_CALLSITES:
        scaffold_violations.append(
            "B01-CALLS  only %d production `SettingsScaffold(...)` call site(s) pass a non-null "
            "`onBack` (floor %d) — the anchor stopped matching (re-derive this criterion), or "
            "screens lost their back handler" % (onback_calls, MIN_ONBACK_CALLSITES))

    if scaffold_violations:
        print(f"❌ SettingsScaffold navigation guard: {len(scaffold_violations)} violation(s)")
        print("   The slot used to honour only the custom `navigation` parameter, so every")
        print("   screen passing just `onBack` rendered an empty navigation slot: no back")
        print("   arrow, and a user who does not know the system back gesture has no way out.")
        print("   Fix: keep both arms of the `when` — `navigation != null -> navigation()`")
        print("        and `onBack != null -> IconButton(onClick = onBack)`.")
        for v in scaffold_violations:
            print("     " + v)
        return 1

    if violations:
        print(f"❌ IME-inset guard: {len(violations)} violation(s)")
        print("   A text field is hosted by a bare Scaffold (contentWindowInsets =")
        print("   systemBars only) with no `.imePadding()` anywhere in the")
        print("   composable. Under edge-to-edge the keyboard covers the field and")
        print("   the caret scrolls behind it — silent, no crash, no test failure.")
        print("   Fix: add `.imePadding()` AFTER `.padding(padding)` on the host")
        print("        (host layer, not the leaf — leaves inside ModalBottomSheet")
        print("        would double up).")
        print("   Or, if intentional, mark it inline:")
        print("        `// ime-ok: <reason>`            (unprotected on purpose)")
        print("        `// ime-unreachable: <reason>`   (branches are exclusive)")
        for v in violations:
            print("     " + v)
        return 1
    print("✅ IME-inset guard: no unprotected text field in a bare-Scaffold host")
    if scaffold_seen:
        print(f"✅ SettingsScaffold navigation guard: {onback_calls} call site(s) pass `onBack`, "
              "and the slot falls back to a back arrow")
    return 0


if __name__ == "__main__":
    sys.exit(main())

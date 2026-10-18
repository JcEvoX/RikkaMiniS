#!/usr/bin/env python3
"""[fix/da1022-G] tool_title_error_branch_check — extended [fix/da1025-G]

Original gate: the five file/memory tools must resolve `tool_title` BEFORE
the try block (FileWriteTool pattern) and every error branch must report the
resolved title — no `toolTitle = NAME` hardcode in catch arms, no silently
dropped third parameter.

[fix/da1025-G] extension (ISSUE-07): the same contract now covers the
browser_use executor and the conversation_history executor:
  ChatToolExecutors.kt : resolveToolTitle BEFORE the parse early-return;
                         both error shapes (`Invalid browser_use input`
                         early return + generic catch) carry toolTitle;
                         the try-scoped inline copy is gone
  ConversationHistoryTool.kt : resolveToolTitle with a TOOL-NAME fallback —
                         `getOrDefault("")` and the single-arg
                         `optString("tool_title")` are both forbidden
  FileWriteTool.kt     : mode tightened inline_or_helper -> helper
                         (single-sourced onto resolveToolTitle)

Code anchors only — comments are stripped before judgment. Anchor matching
is "contains" (not startswith) so `?: return ToolExecutionResult("Error: …")`
early-return lines are checked too.

Usage: python3 tool_title_error_branch_check.py <repo_root>
       python3 tool_title_error_branch_check.py --root <repo_root>
       python3 tool_title_error_branch_check.py --selftest
Exit 0 = pass, 1 = violation, 2 = usage error.
"""
import os
import sys

TOOL_DIR = "src/android/app/src/main/java/com/rikkaminis/app/tools"
UI_CHAT_DIR = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat"
MARKER = "TOOL_TITLE_ERROR_BRANCH_SELFTEST=PASS"

# file -> (dir, error-line anchor (None = no anchor loop), resolution kind)
SPEC = {
    "FileWriteTool.kt": (TOOL_DIR, 'ToolExecutionResult("Error writing file:', "helper"),
    "FileReadTool.kt": (TOOL_DIR, 'ToolExecutionResult("Error reading file:', "helper"),
    "FileEditTool.kt": (TOOL_DIR, 'ToolExecutionResult("Error editing file:', "helper"),
    "ReadImageTool.kt": (TOOL_DIR, 'ToolExecutionResult("Error reading image:', "helper"),
    "MemoryTools.kt": (TOOL_DIR, 'ToolResult("Error:', "helper"),
    # [fix/da1025-G] browser_use executor — both error shapes must carry title
    "ChatToolExecutors.kt": (UI_CHAT_DIR, 'ToolExecutionResult("Error:', "helper"),
    # [fix/da1025-G] conversation_history — fallback must be the tool name
    "ConversationHistoryTool.kt": (TOOL_DIR, None, "helper_fallback"),
}


def strip_line_comments(src: str) -> str:
    out_lines = []
    for line in src.split("\n"):
        in_str = False
        i = 0
        cut = len(line)
        while i < len(line):
            c = line[i]
            if c == "\\" and in_str:
                i += 2
                continue
            if c == '"':
                in_str = not in_str
            elif c == "/" and not in_str and i + 1 < len(line) and line[i + 1] == "//":
                cut = i
                break
            i += 1
        out_lines.append(line[:cut])
    return "\n".join(out_lines)


def check_file(source: str, fname: str) -> list:
    src = strip_line_comments(source)
    _, anchor, kind = SPEC[fname]
    errs = []

    if kind == "helper":
        # resolution must exist before the try (code anchors only)
        if "resolveToolTitle(" not in src:
            errs.append("no resolveToolTitle( call found")
        if 'optString("tool_title"' in src:
            errs.append('try-scoped optString("tool_title") still present')
    elif kind == "helper_fallback":
        # [fix/da1025-G] the fallback must be the tool NAME, never ""
        if "resolveToolTitle(" not in src:
            errs.append("no resolveToolTitle( call found")
        if 'optString("tool_title")' in src:
            errs.append('single-arg optString("tool_title") (implicit "" fallback) still present')
        if '.getOrDefault("")' in src:
            errs.append('empty-string fallback .getOrDefault("") still present')
    # (no other kinds; FileWriteTool's inline_or_helper was tightened to helper)

    # every error branch carrying `anchor` must pass the resolved title
    if anchor is not None:
        for line in src.split("\n"):
            t = line.strip()
            if anchor not in t:
                continue
            if "toolTitle = toolTitle" not in t and ", toolTitle)" not in t:
                errs.append(f"error branch does not pass the resolved title: {t}")

    # forbidden hardcoded/empty title in error branches
    if "toolTitle = NAME" in src:
        errs.append("hardcoded toolTitle = NAME still present in an error branch")
    if 'toolTitle = ""' in src:
        errs.append('empty toolTitle still present in an error branch')

    return errs


def check(root: str) -> list:
    errs = []
    for fname, (fdir, _, _) in SPEC.items():
        path = os.path.join(root, fdir, fname)
        try:
            src = open(path).read()
        except OSError as e:
            errs.append(f"cannot read {fname}: {e}")
            continue
        for e in check_file(src, fname):
            errs.append(f"{fname}: {e}")
    # MemoryTools: both functions must resolve the title
    path = os.path.join(root, TOOL_DIR, "MemoryTools.kt")
    try:
        src = strip_line_comments(open(path).read())
        n = src.count("resolveToolTitle(")
        if n < 2:
            errs.append(f"MemoryTools.kt: resolveToolTitle( found {n} times, expected 2 (write + get)")
    except OSError:
        pass
    return errs


def selftest() -> int:
    good_exec = """
        fun execute(argsJson: String, sessionId: String, context: Context): ToolExecutionResult {
            val toolTitle = resolveToolTitle(argsJson, NAME)
            return try {
                val args = JSONObject(argsJson)
                ToolExecutionResult("ok", true, toolTitle = toolTitle)
            } catch (e: Exception) {
                ToolExecutionResult("Error reading file: ${e.message}", false, toolTitle = toolTitle)
            }
        }
    """
    bad_hardcode = good_exec.replace(
        'ToolExecutionResult("Error reading file: ${e.message}", false, toolTitle = toolTitle)',
        'ToolExecutionResult("Error reading file: ${e.message}", false, toolTitle = NAME)',
    )
    bad_dropped = good_exec.replace(
        'ToolExecutionResult("Error reading file: ${e.message}", false, toolTitle = toolTitle)',
        'ToolExecutionResult("Error reading image: ${e.message}", false)',
    )
    bad_tryscoped = """
        fun execute(argsJson: String, sessionId: String, context: Context): ToolExecutionResult {
            return try {
                val args = JSONObject(argsJson)
                val toolTitle = args.optString("tool_title", NAME)
                ToolExecutionResult("ok", true, toolTitle = toolTitle)
            } catch (e: Exception) {
                ToolExecutionResult("Error editing file: ${e.message}", false, toolTitle = NAME)
            }
        }
    """
    good_memory = """
        fun executeMemoryWrite(inputJson: String, repository: MemoryRepository): ToolResult {
            val toolTitle = resolveToolTitle(inputJson, "memory_write")
            return try {
                ToolResult("ok", true, toolTitle)
            } catch (e: Exception) {
                ToolResult("Error: ${e.message}", false, toolTitle)
            }
        }
        fun executeMemoryGet(inputJson: String, repository: MemoryRepository): ToolResult {
            val toolTitle = resolveToolTitle(inputJson, "memory_get")
            return try {
                ToolResult("ok", true, toolTitle)
            } catch (e: Exception) {
                ToolResult("Error: ${e.message}", false, toolTitle)
            }
        }
    """
    bad_memory = good_memory.replace(
        'ToolResult("Error: ${e.message}", false, toolTitle)',
        'ToolResult("Error: ${e.message}", false)',
    )

    # ── [fix/da1025-G] browser_use executor (ChatToolExecutors.kt) ──────
    good_browser = """
        val toolTitle = resolveToolTitle(argsJson, "browser_use")
        val parsed = BrowserActionInput.parse(argsJson)
            ?: return ToolExecutionResult("Error: Invalid browser_use input", false, toolTitle = toolTitle)
        return try {
            val result = tabPool.execute(input)
            ToolExecutionResult(output = result.text, toolTitle = toolTitle)
        } catch (e: Exception) {
            ToolExecutionResult("Error: ${e.message}", false, toolTitle = toolTitle)
        }
    """
    # V5: early-return loses the title — exactly the error-branch criterion fires
    bad_browser_early = good_browser.replace(
        'ToolExecutionResult("Error: Invalid browser_use input", false, toolTitle = toolTitle)',
        'ToolExecutionResult("Error: Invalid browser_use input", false)',
    )
    # V6: catch arm loses the title
    bad_browser_catch = good_browser.replace(
        'ToolExecutionResult("Error: ${e.message}", false, toolTitle = toolTitle)',
        'ToolExecutionResult("Error: ${e.message}", false)',
    )
    # V7: try-scoped inline copy (the pre-fix shape) — resolution + optString both fire
    bad_browser_inline = """
        val parsed = BrowserActionInput.parse(argsJson)
            ?: return ToolExecutionResult("Error: Invalid browser_use input", false)
        return try {
            val result = tabPool.execute(input)
            val toolTitle = try {
                JSONObject(argsJson).optString("tool_title", "browser_use")
            } catch (_: Exception) { "browser_use" }
            ToolExecutionResult(output = result.text, toolTitle = toolTitle)
        } catch (e: Exception) {
            ToolExecutionResult("Error: ${e.message}", false)
        }
    """

    # ── [fix/da1025-G] conversation_history (ConversationHistoryTool.kt) ─
    good_convhist = """
        val toolTitle = resolveToolTitle(argsJson, ConversationHistoryContract.NAME)
        return when (val request = parseConversationHistoryRequest(argsJson)) {
            is ConversationHistoryRequest.Invalid -> ToolExecutionResult(
                output = "Error: " + request.message,
                success = false,
                toolTitle = toolTitle,
            )
            else -> ToolExecutionResult(output = "ok", success = true, toolTitle = toolTitle)
        }
    """
    # V8: empty-string fallback via getOrDefault("") — exactly that criterion
    bad_convhist_empty = """
        val toolTitle = resolveToolTitle(argsJson, ConversationHistoryContract.NAME)
        val legacy = runCatching { JSONObject(argsJson).optString("tool_title", "conversation_history") }.getOrDefault("")
        return when (val request = parseConversationHistoryRequest(argsJson)) {
            is ConversationHistoryRequest.Invalid -> ToolExecutionResult(
                output = "Error: " + request.message,
                success = false,
                toolTitle = legacy,
            )
            else -> ToolExecutionResult(output = "ok", success = true, toolTitle = legacy)
        }
    """
    # V9: single-arg optString (implicit "" fallback) — exactly that criterion
    bad_convhist_singlearm = """
        val toolTitle = resolveToolTitle(argsJson, ConversationHistoryContract.NAME)
        val legacy = args.optString("tool_title")
        return ToolExecutionResult(output = "ok", success = true, toolTitle = legacy)
    """
    # V10: the full pre-fix shape — no resolveToolTitle, single-arg + empty fallback
    bad_convhist_prefix = """
        val toolTitle = runCatching { JSONObject(argsJson).optString("tool_title") }.getOrDefault("")
        return ToolExecutionResult(output = "ok", success = true, toolTitle = toolTitle)
    """

    # ── [fix/da1025-G] FileWriteTool tightened to helper (single-source) ─
    good_filewrite = """
        fun execute(argsJson: String, sessionId: String, context: Context): ToolExecutionResult {
            val toolTitle = resolveToolTitle(argsJson, NAME)
            return try {
                val args = JSONObject(argsJson)
                ToolExecutionResult("ok", true, toolTitle = toolTitle)
            } catch (e: Exception) {
                ToolExecutionResult("Error writing file: ${e.message}", false, toolTitle = toolTitle)
            }
        }
    """
    # V11: the inline runCatching copy (non-single-source) — red under helper mode
    bad_filewrite_inline = good_filewrite.replace(
        "val toolTitle = resolveToolTitle(argsJson, NAME)",
        'val toolTitle = runCatching { JSONObject(argsJson).optString("tool_title", NAME) }\n'
        "            .getOrDefault(NAME)",
    )

    arms = [
        ("compliant exec", "FileReadTool.kt", good_exec, 0),
        ("V1 catch hardcode NAME", "FileReadTool.kt", bad_hardcode, 2),
        ("V2 error branch drops toolTitle", "ReadImageTool.kt", bad_dropped, 1),
        ("V3 try-scoped val + hardcode", "FileEditTool.kt", bad_tryscoped, 4),
        ("compliant memory", "MemoryTools.kt", good_memory, 0),
        ("V4 memory catch drops toolTitle", "MemoryTools.kt", bad_memory, 2),
        ("compliant browser_use", "ChatToolExecutors.kt", good_browser, 0),
        ("V5 browser early-return drops title", "ChatToolExecutors.kt", bad_browser_early, 1),
        ("V6 browser catch drops title", "ChatToolExecutors.kt", bad_browser_catch, 1),
        ("V7 browser try-scoped inline copy", "ChatToolExecutors.kt", bad_browser_inline, 4),
        ("compliant conversation_history", "ConversationHistoryTool.kt", good_convhist, 0),
        ("V8 convhist empty getOrDefault", "ConversationHistoryTool.kt", bad_convhist_empty, 1),
        ("V9 convhist single-arg optString", "ConversationHistoryTool.kt", bad_convhist_singlearm, 1),
        ("V10 convhist full pre-fix shape", "ConversationHistoryTool.kt", bad_convhist_prefix, 3),
        ("compliant file_write (helper)", "FileWriteTool.kt", good_filewrite, 0),
        ("V11 file_write inline copy (non-single-source)", "FileWriteTool.kt", bad_filewrite_inline, 2),
    ]
    failures = 0
    for name, fname, sample, expect in arms:
        errs = [f"{fname}: {e}" for e in check_file(sample, fname)]
        if expect == 0 and errs:
            print(f"  FAIL {name}: expected green, got {errs}")
            failures += 1
        elif expect != 0 and len(errs) != expect:
            print(f"  FAIL {name}: expected {expect} error(s), got {len(errs)}: {errs}")
            failures += 1
        else:
            print(f"  OK {name}: {errs if errs else 'green'}")
    if failures:
        print(f"SELFTEST FAILED ({failures} arms)")
        return 1
    print(MARKER)
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:]]
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
    errs = check(root)
    if errs:
        print("tool_title_error_branch_check RED:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("tool_title_error_branch_check OK (7 files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

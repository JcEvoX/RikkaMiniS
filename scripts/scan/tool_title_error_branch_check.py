#!/usr/bin/env python3
"""[fix/da1022-G] tool_title_error_branch_check

Issue-09 gate: the five file/memory tools must resolve `tool_title` BEFORE
the try block (FileWriteTool pattern) and every error branch must report the
resolved title — no `toolTitle = NAME` hardcode in catch arms, no silently
dropped third parameter.

Code anchors only — comments are stripped before judgment.

Checked (per file, tools package):
  FileWriteTool.kt : inline runCatching pattern OR resolveToolTitle helper;
                     error branch `Error writing file:` carries toolTitle
  FileReadTool.kt  : resolveToolTitle before try; try-scoped optString gone;
                     error branch `Error reading file:` carries toolTitle
  FileEditTool.kt  : same family, `Error editing file:`
  ReadImageTool.kt : same family, `Error reading image:` (must not drop the
                     named toolTitle parameter)
  MemoryTools.kt   : resolveToolTitle for both functions; catch-arm
                     ToolResult("Error: ...", false) must pass toolTitle

Usage: python3 tool_title_error_branch_check.py <repo_root>
       python3 tool_title_error_branch_check.py --root <repo_root>
       python3 tool_title_error_branch_check.py --selftest
Exit 0 = pass, 1 = violation, 2 = usage error.
"""
import os
import sys

PKG = "src/android/app/src/main/java/com/rikkaminis/app/tools"
MARKER = "TOOL_TITLE_ERROR_BRANCH_SELFTEST=PASS"

# file -> (error-line anchor, resolution anchor requirement)
SPEC = {
    "FileWriteTool.kt": ("ToolExecutionResult(\"Error writing file:", "inline_or_helper"),
    "FileReadTool.kt": ("ToolExecutionResult(\"Error reading file:", "helper"),
    "FileEditTool.kt": ("ToolExecutionResult(\"Error editing file:", "helper"),
    "ReadImageTool.kt": ("ToolExecutionResult(\"Error reading image:", "helper"),
    "MemoryTools.kt": ("ToolResult(\"Error:", "helper"),
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
            elif c == "/" and not in_str and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
            i += 1
        out_lines.append(line[:cut])
    return "\n".join(out_lines)


def check_file(source: str, fname: str) -> list:
    src = strip_line_comments(source)
    anchor, kind = SPEC[fname]
    errs = []

    # resolution must exist before the try (code anchors only)
    if kind == "helper":
        if "resolveToolTitle(" not in src:
            errs.append("no resolveToolTitle( call found")
        if "optString(\"tool_title\"" in src:
            errs.append("try-scoped optString(\"tool_title\") still present")
    else:  # FileWriteTool: inline runCatching or helper
        has_inline = "runCatching { JSONObject(argsJson).optString(\"tool_title\"" in src
        if not has_inline and "resolveToolTitle(" not in src:
            errs.append("neither inline runCatching pattern nor resolveToolTitle( found")

    # every error branch carrying `anchor` must pass the resolved title
    for line in src.split("\n"):
        t = line.strip()
        if not t.startswith(anchor):
            continue
        if "toolTitle = toolTitle" not in t and ", toolTitle)" not in t:
            errs.append(f"error branch does not pass the resolved title: {t}")

    # forbidden hardcoded/empty title in error branches
    if "toolTitle = NAME" in src:
        errs.append("hardcoded toolTitle = NAME still present in an error branch")
    if "toolTitle = \"\"" in src:
        errs.append("empty toolTitle still present in an error branch")

    return errs


def check(root: str) -> list:
    errs = []
    for fname, _ in SPEC.items():
        path = os.path.join(root, PKG, fname)
        try:
            src = open(path).read()
        except OSError as e:
            errs.append(f"cannot read {fname}: {e}")
            continue
        for e in check_file(src, fname):
            errs.append(f"{fname}: {e}")
    # MemoryTools: both functions must resolve the title
    path = os.path.join(root, PKG, "MemoryTools.kt")
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

    arms = [
        ("compliant exec", "FileReadTool.kt", good_exec, []),
        ("V1 catch hardcode NAME", "FileReadTool.kt", bad_hardcode, 1),
        ("V2 error branch drops toolTitle", "ReadImageTool.kt", bad_dropped, 1),
        ("V3 try-scoped val + hardcode", "FileEditTool.kt", bad_tryscoped, 1),
        ("compliant memory", "MemoryTools.kt", good_memory, []),
        ("V4 memory catch drops toolTitle", "MemoryTools.kt", bad_memory, 1),
    ]
    failures = 0
    for name, fname, sample, expect in arms:
        errs = [f"{fname}: {e}" for e in check_file(sample, fname)]
        if expect == [] and errs:
            print(f"  FAIL {name}: expected green, got {errs}")
            failures += 1
        elif expect != [] and not errs:
            print(f"  FAIL {name}: expected red, got green")
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
    print("tool_title_error_branch_check OK (5 files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

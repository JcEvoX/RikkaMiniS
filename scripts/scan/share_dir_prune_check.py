#!/usr/bin/env python3
"""share_dir_prune_check — deep-audit-1022 FIX-H (ISSUE-10, P2) gate.

Pins the cache/share housekeeping contract:

  arm1: every production .kt file that constructs `File(context.cacheDir,
        "share")` must also call `pruneShareDir(` — share/ blobs were written
        once and never deleted, accumulating for the life of the install.
        The pruner's definition file (FullscreenImageViewer.kt) is exempt.
  arm2: in WebAppActivity.kt's stagedShareUri, the share-dir construction
        (and its `mkdirs()`) must live INSIDE the `withContext(Dispatchers.IO)`
        block, and the call order must be
        withContext(Dispatchers.IO) → File(cacheDir, "share") → pruneShareDir
        → copyTo. Before the fix, `mkdirs()` ran on the main thread and
        `pruneShareDir` was never called.

  Prune-then-copy is safe by construction: the 24h TTL cutoff always
  postdates the fresh copy written after the prune, so the prune can never
  delete the file the current call is about to stage.

Usage:
  python3 share_dir_prune_check.py <repo_root>   # real tree
  python3 share_dir_prune_check.py --selftest    # fixtures

Selftest marker: SHARE_DIR_PRUNE_SELFTEST=PASS
"""

import os
import re
import sys
import tempfile

DEFINER_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/components/FullscreenImageViewer.kt"
ACTIVITY_REL = "src/android/app/src/main/java/com/rikkaminis/app/webapp/WebAppActivity.kt"
MAIN_ROOT = "src/android/app/src/main"

SHARE_DIR_PAT = re.compile(r"File\(\s*context\.cacheDir\s*,\s*\"share\"\s*\)")

# Files that satisfy the prune contract with their own name-scoped housekeeping
# instead of pruneShareDir. Each entry maps filename -> marker that must be
# present in the (comment-stripped) code — if the marker disappears, the file
# falls back to violating, so the exemption cannot rot silently.
#   SkillRepository.kt: sweepStaleExports() deletes `skill-export-*` dirs older
#   than EXPORT_TTL_MS and is called before each new export (its own contract).
EXEMPT_FILES = {
    "SkillRepository.kt": "sweepStaleExports()",
    "FullscreenImageViewer.kt": "internal fun pruneShareDir(",
}


def strip_comments(src):
    """Remove // line comments and /* */ block comments (KDoc included)."""
    out = []
    i, n = 0, len(src)
    in_block = in_line = False
    while i < n:
        if in_block:
            if src.startswith("*/", i):
                in_block = False
                i += 2
            else:
                i += 1
        elif in_line:
            if src[i] == "\n":
                in_line = False
                out.append("\n")
            i += 1
        elif src.startswith("//", i):
            in_line = True
            i += 2
        elif src.startswith("/*", i):
            in_block = True
            i += 2
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def check_arm1(root):
    """Every producer of cacheDir/share must call pruneShareDir (definer exempt)."""
    violations = []
    base = os.path.join(root, MAIN_ROOT)
    for dirpath, _dirnames, filenames in os.walk(base):
        for fn in filenames:
            if not fn.endswith(".kt"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                src = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            code = strip_comments(src)
            if not SHARE_DIR_PAT.search(code):
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if rel == DEFINER_REL:
                continue
            if "pruneShareDir(" in code:
                continue
            exempt_marker = EXEMPT_FILES.get(fn)
            if exempt_marker and exempt_marker in code:
                continue
            violations.append(rel)
    return violations


def extract_function(code, marker):
    """Extract the region from `marker` to the next top-level `    }` + blank line
    that closes the function (heuristic: next line matching r'^    @|^    private|^    fun|^}$')."""
    start = code.find(marker)
    if start < 0:
        return None
    rest = code[start:]
    lines = rest.split("\n")
    body = [lines[0]]
    depth = lines[0].count("{") - lines[0].count("}")
    if depth <= 0:
        # signature line without braces — keep scanning
        pass
    for line in lines[1:]:
        body.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            break
    return "\n".join(body)


def check_arm2(root):
    """stagedShareUri: mkdirs inside IO context, order IO → dir → prune → copy."""
    path = os.path.join(root, ACTIVITY_REL)
    src = open(path, encoding="utf-8", errors="replace").read()
    code = strip_comments(src)
    fn = extract_function(code, "private suspend fun stagedShareUri")
    if fn is None:
        return "stagedShareUri function not found in WebAppActivity.kt (stripped)"
    io_idx = fn.find("withContext(Dispatchers.IO)")
    dir_idx = fn.find('File(context.cacheDir, "share")')
    prune_idx = fn.find("pruneShareDir(")
    copy_idx = fn.find(".copyTo(")
    if io_idx < 0:
        return "stagedShareUri lost its withContext(Dispatchers.IO) block"
    if dir_idx < 0:
        return "stagedShareUri lost the File(context.cacheDir, \"share\") construction"
    if prune_idx < 0:
        return "stagedShareUri must call pruneShareDir( before staging"
    if copy_idx < 0:
        return "stagedShareUri lost its file.copyTo( staging call"
    if not (io_idx < dir_idx < prune_idx < copy_idx):
        return (
            "stagedShareUri ordering broken: expected "
            "withContext(Dispatchers.IO) < File(cacheDir, \"share\") < pruneShareDir < copyTo, got "
            f"io={io_idx} dir={dir_idx} prune={prune_idx} copy={copy_idx}"
        )
    # mkdirs must ride the same IO block: it appears on the share-dir line.
    if "mkdirs()" not in fn[dir_idx:prune_idx]:
        return "share-dir mkdirs() must stay inside the IO block (with pruneShareDir)"
    return None


# --- compliant / violation fixture snippets (code-level, not comments) ---

COMPLIANT = """package com.rikkaminis.app.webapp

import com.rikkaminis.app.ui.components.pruneShareDir

class WebAppActivity {
    private suspend fun stagedShareUri(context: android.content.Context, file: File): Uri? {
        return try {
            val stem = file.nameWithoutExtension.ifBlank { "webapp" }
            val staged = withContext(Dispatchers.IO) {
                val shareDir = File(context.cacheDir, "share").apply { mkdirs() }
                pruneShareDir(shareDir)
                val s = File(shareDir, "$stem.html")
                file.copyTo(s, overwrite = true)
                s
            }
            FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", staged)
        } catch (t: Throwable) {
            null
        }
    }
}
"""

VIOLATION_NO_PRUNE = COMPLIANT.replace("                pruneShareDir(shareDir)\n", "")

VIOLATION_MAIN_THREAD = """package com.rikkaminis.app.webapp

import com.rikkaminis.app.ui.components.pruneShareDir

class WebAppActivity {
    private suspend fun stagedShareUri(context: android.content.Context, file: File): Uri? {
        return try {
            val stem = file.nameWithoutExtension.ifBlank { "webapp" }
            val shareDir = File(context.cacheDir, "share").apply { mkdirs() }
            pruneShareDir(shareDir)
            val staged = withContext(Dispatchers.IO) {
                val s = File(shareDir, "$stem.html")
                file.copyTo(s, overwrite = true)
                s
            }
            FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", staged)
        } catch (t: Throwable) {
            null
        }
    }
}
"""


def run_fixture(name, content, expect_ok):
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, ACTIVITY_REL.rsplit("/", 1)[0]))
        with open(os.path.join(tmp, ACTIVITY_REL), "w") as f:
            f.write(content)
        a1 = check_arm1(tmp)
        a2 = check_arm2(tmp)
        ok = not a1 and a2 is None
        status = "PASS" if ok == expect_ok else "FAIL"
        print(f"  [{status}] fixture {name}: arm1={a1!r} arm2={a2!r}")
        return status == "PASS"


def selftest():
    print("share_dir_prune_check selftest:")
    ok = True
    # arm1 fixtures: a producer without prune must be caught; the exemption
    # must hold only while its housekeeping marker is present.
    with tempfile.TemporaryDirectory() as tmp:
        rel_dir = os.path.join(MAIN_ROOT, "com/rikkaminis/app/other")
        os.makedirs(os.path.join(tmp, rel_dir))
        with open(os.path.join(tmp, rel_dir, "Writer.kt"), "w") as f:
            f.write(
                'package com.rikkaminis.app.other\n'
                'class Writer {\n'
                '    val shareDir = File(context.cacheDir, "share").apply { mkdirs() }\n'
                '}\n'
            )
        v = check_arm1(tmp)
        ok &= v == ["src/android/app/src/main/com/rikkaminis/app/other/Writer.kt"]
        print(f"  [{'PASS' if v else 'FAIL'}] arm1 catches a producer without pruneShareDir: {v}")
    with tempfile.TemporaryDirectory() as tmp:
        rel_dir = os.path.join(MAIN_ROOT, "com/rikkaminis/app/other")
        os.makedirs(os.path.join(tmp, rel_dir))
        # exemption with marker present -> green
        with open(os.path.join(tmp, rel_dir, "SkillRepository.kt"), "w") as f:
            f.write(
                'package com.rikkaminis.app.other\n'
                'class SkillRepository {\n'
                '    val root = File(context.cacheDir, "share")\n'
                '    init { sweepStaleExports() }\n'
                '    private fun sweepStaleExports() {}\n'
                '}\n'
            )
        v = check_arm1(tmp)
        ok &= v == []
        print(f"  [{'PASS' if not v else 'FAIL'}] arm1 exemption holds while housekeeping marker present: {v}")
        # exemption marker stripped -> red again (tooth)
        with open(os.path.join(tmp, rel_dir, "SkillRepository.kt"), "w") as f:
            f.write(
                'package com.rikkaminis.app.other\n'
                'class SkillRepository {\n'
                '    val root = File(context.cacheDir, "share")\n'
                '}\n'
            )
        v = check_arm1(tmp)
        ok &= v == ["src/android/app/src/main/com/rikkaminis/app/other/SkillRepository.kt"]
        print(f"  [{'PASS' if v else 'FAIL'}] arm1 exemption falls back when marker gone: {v}")
    ok &= run_fixture("compliant", COMPLIANT, expect_ok=True)
    ok &= run_fixture("violation-no-prune", VIOLATION_NO_PRUNE, expect_ok=False)
    ok &= run_fixture("violation-main-thread", VIOLATION_MAIN_THREAD, expect_ok=False)
    print("SHARE_DIR_PRUNE_SELFTEST=" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if len(sys.argv) < 2:
        print("usage: python3 share_dir_prune_check.py <repo_root>", file=sys.stderr)
        sys.exit(2)
    root = sys.argv[1]
    failures = []
    a1 = check_arm1(root)
    if a1:
        failures.append(f"arm1 (share-dir producers missing pruneShareDir): {a1}")
    a2 = check_arm2(root)
    if a2:
        failures.append(f"arm2 (stagedShareUri IO/prune contract): {a2}")
    if failures:
        for f in failures:
            print("FAIL:", f)
        sys.exit(1)
    print("share_dir_prune_check: OK (arm1 + arm2 green)")


if __name__ == "__main__":
    main()

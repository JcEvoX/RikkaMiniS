#!/usr/bin/env python3
"""[fix/da1025-D] browser_tabs_lifecycle_check

ISSUE-04 gate: the browser tab-pool saved-state files
(filesDir/browser_tabs/<sid>.json — a session's full visited-URL history)
must live and die with the session, same as the bind-mounted session dir and
its media. Before this gate the file was outside both cleanup paths:
  - deleteSessionFiles never deleted it → deleted session's URL history
    stayed on disk forever;
  - reclaimOrphans never scanned it → invisible to the storage page's
    orphan sweep.

Checked in SessionFileStore.kt (code anchors only — line AND block comments
are stripped before judgment, so a KDoc that merely *mentions* browser_tabs
cannot satisfy any criterion):
  C1  `browser_tabs` root + `fun browserTabFile(` definition exist
      (fail-closed: missing anchor = red, never a silent skip);
  C2  `deleteSessionFiles` function body references `browserTabFile(`
      (the delete path actually removes the tab state file);
  C3  `reclaimOrphans` function body calls the orphan cleanup
      (`deleteOrphanBrowserTabFiles(` or equivalent anchor), and the
      orphan browser-tabs cleanup itself is session-shape guarded
      (`looksLikeSessionId` OR `__new__` prefix branch present).

Usage: python3 browser_tabs_lifecycle_check.py <repo_root>
       python3 browser_tabs_lifecycle_check.py --root <repo_root>
       python3 browser_tabs_lifecycle_check.py --selftest
Exit 0 = pass, 1 = violation, 2 = usage error.
"""
import os
import sys

PKG = "src/android/app/src/main/java/com/rikkaminis/app/data/storage"
TARGET = "SessionFileStore.kt"
MARKER = "BROWSER_TABS_SELFTEST=PASS"


def strip_comments(src: str) -> str:
    """Remove // line comments AND /* */ block comments, string-aware."""
    out = []
    in_str = False
    in_line = False
    in_block = False
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
                out.append(c)
            i += 1
            continue
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            if c == "\n":
                out.append(c)
            i += 1
            continue
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
        if c == "/" and nxt == "/":
            in_line = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def slice_body(src: str, header: str) -> str:
    """Return the function body for `header` (e.g. 'fun deleteSessionFiles').

    Fail-closed: returns '' when the header is not found or braces never
    balance — callers treat '' as a violation, never a silent skip.
    """
    idx = src.find(header)
    if idx < 0:
        return ""
    open_idx = src.find("{", idx)
    if open_idx < 0:
        return ""
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == '"':
            # skip string literal (escape-aware) so braces inside don't count
            j = i + 1
            while j < len(src):
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    break
                j += 1
            i = j
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx:i + 1]
    return ""


def check_source(source: str) -> list:
    src = strip_comments(source)
    errs = []

    # C1: root + definition (fail-closed)
    if "browser_tabs" not in src:
        errs.append("C1: no browser_tabs root in SessionFileStore")
    if "fun browserTabFile(" not in src:
        errs.append("C1: no fun browserTabFile( definition found")

    # C2: delete path removes the tab state file
    del_body = slice_body(src, "fun deleteSessionFiles")
    if not del_body:
        errs.append("C2: deleteSessionFiles body not extractable (fail-closed)")
    elif "browserTabFile(" not in del_body:
        errs.append("C2: deleteSessionFiles does not delete the browser tab state file")

    # C3: orphan reclaim path covers browser_tabs
    reclaim_body = slice_body(src, "fun reclaimOrphans")
    if not reclaim_body:
        errs.append("C3: reclaimOrphans body not extractable (fail-closed)")
    elif "deleteOrphanBrowserTabFiles(" not in reclaim_body:
        errs.append("C3: reclaimOrphans does not reclaim orphan browser_tabs files")
    orphan_body = slice_body(src, "fun deleteOrphanBrowserTabFiles")
    if not orphan_body:
        errs.append("C3: deleteOrphanBrowserTabFiles not found (fail-closed)")
    else:
        if "looksLikeSessionId(" not in orphan_body:
            errs.append("C3: orphan browser_tabs cleanup not session-shape guarded")
        if "__new__" not in orphan_body:
            errs.append("C3: orphan browser_tabs cleanup misses the __new__ draft branch")

    return errs


def selftest() -> int:
    compliant = """package com.rikkaminis.app.data.storage

/**
 * Singular owner of the on-disk footprint attached to a chat session.
 * The doc mentions browser_tabs/<sid>.json and browserTabFile( freely —
 * block comments are stripped, so none of this satisfies any criterion.
 */
class SessionFileStore internal constructor(
    private val filesDirForTestingOrProd: File,
) {
    val sessionsRoot: File = File(filesDirForTestingOrProd, "minis-sessions")
    private val mediaRoot: File = File(filesDirForTestingOrProd, "media")
    private val browserTabsRoot: File = File(filesDirForTestingOrProd, "browser_tabs")

    fun sessionDir(sessionId: String): File = File(sessionsRoot, sessionId)
    fun browserTabFile(sessionId: String): File = File(browserTabsRoot, "$sessionId.json")

    // delete the tab file alongside the session dir (line comment mentions
    // browserTabFile( — stripped, must not satisfy C2 by itself)
    fun deleteSessionFiles(sessionId: String): DeleteResult {
        val sessionDeleted = deleteRecursivelyChecked(sessionDir(sessionId)) &&
            deleteRecursivelyChecked(browserTabFile(sessionId))
        val mediaDeleted = mediaSessionDirs(sessionId).fold(true) { acc, dir ->
            deleteRecursivelyChecked(dir) && acc
        }
        return DeleteResult(sessionDeleted, mediaDeleted)
    }

    fun reclaimOrphans(liveSessionIds: Set<String>): ReclaimReport {
        val report = scanOrphans(liveSessionIds)
        deleteOrphanMediaLeaves(liveSessionIds)
        deleteOrphanBrowserTabFiles(liveSessionIds)
        return report
    }

    private fun deleteOrphanBrowserTabFiles(liveSessionIds: Set<String>) {
        if (!browserTabsRoot.exists()) return
        browserTabsRoot.listFiles()?.forEach { file ->
            val stem = file.name.removeSuffix(".json")
            val isSessionShaped = looksLikeSessionId(stem) || stem.startsWith("__new__")
            if (isSessionShaped && stem !in liveSessionIds) file.delete()
        }
    }
}
"""
    # V1: delete path no longer removes the tab state file
    v1 = compliant.replace(
        "        val sessionDeleted = deleteRecursivelyChecked(sessionDir(sessionId)) &&\n"
        "            deleteRecursivelyChecked(browserTabFile(sessionId))",
        "        val sessionDeleted = deleteRecursivelyChecked(sessionDir(sessionId))",
    )
    # V2: orphan reclaim loses the browser_tabs half
    v2 = compliant.replace("        deleteOrphanBrowserTabFiles(liveSessionIds)\n", "")
    # V3: no browser_tabs at all (regression to the defect status quo)
    v3 = """package com.rikkaminis.app.data.storage

class SessionFileStore internal constructor(
    private val filesDirForTestingOrProd: File,
) {
    val sessionsRoot: File = File(filesDirForTestingOrProd, "minis-sessions")
    private val mediaRoot: File = File(filesDirForTestingOrProd, "media")

    fun sessionDir(sessionId: String): File = File(sessionsRoot, sessionId)

    fun deleteSessionFiles(sessionId: String): DeleteResult {
        val sessionDeleted = deleteRecursivelyChecked(sessionDir(sessionId))
        val mediaDeleted = mediaSessionDirs(sessionId).fold(true) { acc, dir ->
            deleteRecursivelyChecked(dir) && acc
        }
        return DeleteResult(sessionDeleted, mediaDeleted)
    }

    fun reclaimOrphans(liveSessionIds: Set<String>): ReclaimReport {
        val report = scanOrphans(liveSessionIds)
        deleteOrphanMediaLeaves(liveSessionIds)
        return report
    }
}
"""
    # V4: KDoc mentions browser_tabs + browserTabFile( but no code (comment trap)
    v4 = v3.replace(
        "class SessionFileStore",
        "/**\n"
        " * KDoc claims we handle browser_tabs/<sid>.json and call browserTabFile(\n"
        " * and deleteOrphanBrowserTabFiles( — pure documentation, no code.\n"
        " */\n"
        "class SessionFileStore",
    )
    # V5: orphan cleanup loses the __new__ draft branch
    v5 = compliant.replace(
        "            val isSessionShaped = looksLikeSessionId(stem) || stem.startsWith(\"__new__\")",
        "            val isSessionShaped = looksLikeSessionId(stem)",
    )

    arms = [
        ("compliant", compliant, []),
        ("V1 delete-path missing tab file", v1, ["C2"]),
        ("V2 orphan reclaim missing browser_tabs", v2, ["C3"]),
        ("V3 regression to defect status quo", v3, ["C1"]),
        ("V4 KDoc-only mention (comment trap)", v4, ["C1", "C2", "C3"]),
        ("V5 orphan cleanup misses __new__ branch", v5, ["C3"]),
    ]
    failures = 0
    for name, sample, expect in arms:
        errs = check_source(sample)
        if expect == [] and errs:
            print(f"  FAIL {name}: expected green, got {errs}")
            failures += 1
        elif expect != [] and not errs:
            print(f"  FAIL {name}: expected red, got green")
            failures += 1
        elif expect != [] and not any(err.startswith(c) for c in expect for err in errs):
            print(f"  FAIL {name}: expected {expect}, got {errs}")
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
    path = os.path.join(root, PKG, TARGET)
    try:
        src = open(path).read()
    except OSError as e:
        print(f"browser_tabs_lifecycle_check RED: cannot read {TARGET}: {e}")
        return 1
    errs = check_source(src)
    if errs:
        print("browser_tabs_lifecycle_check RED:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("browser_tabs_lifecycle_check OK (3 criteria)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""share_dir_prune_check — deep-audit-1022 FIX-H (ISSUE-10, P2) gate.

Pins the cache/share housekeeping contract:

  arm1: every production .kt file that constructs `File(context.cacheDir,
        "share")` — or `"shared"` — must also call `pruneShareDir(` — share/
        blobs were written once and never deleted, accumulating for the life of
        the install. The pruner's definition file (FullscreenImageViewer.kt) is
        exempt.
        [da1024-G] "shared" was this gate's own N+1 hole: SHARE_DIR_PAT required
        a `)` right after the `"share"` literal, so the three
        `File(context.cacheDir, "shared")` writers were invisible while the
        singular directory was covered.
  arm2: in WebAppActivity.kt's stagedShareUri, the share-dir construction
        (and its `mkdirs()`) must live INSIDE the `withContext(Dispatchers.IO)`
        block, and the call order must be
        withContext(Dispatchers.IO) → File(cacheDir, "share") → pruneShareDir
        → copyTo. Before the fix, `mkdirs()` ran on the main thread and
        `pruneShareDir` was never called.
  arm3: [da1024-G] the cacheDir/shared writers must do their disk work off the
        main thread. `exportProviderInstance` / `shareMcpServerJson` must stay
        `private suspend fun` with their mkdirs/prune/writeText inside
        `withContext(Dispatchers.IO)`, and ChatExporter.exportToZip must build
        the shared/ dir and prune before opening the zip stream.
  arm4: [da1025-B] CrashFrequencyDetector.packageZip is the single write point
        for cacheDir/share (all three dispatch* paths call it). It must stay a
        `private suspend fun` with the mkdir/prune/zip work inside
        `withContext(Dispatchers.IO)` (zip packaging ran on the calling UI
        thread before the fix — bounded ANR risk on every export), must call
        the shared `pruneShareDir(` pruner before opening the zip stream (the
        inline [audit-0917] prune only drops all but the newest
        minis-logs-*.zip and is blind to other blobs), and the prune must
        precede ZipOutputStream.

  Prune-then-copy is safe by construction: the 24h TTL cutoff always
  postdates the fresh copy written after the prune, so the prune can never
  delete the file the current call is about to stage.

Usage:
  python3 share_dir_prune_check.py <repo_root>   # real tree
  python3 share_dir_prune_check.py --selftest    # fixtures

Selftest marker: SHARE_DIR_PRUNE_SELFTEST=PASS (+ CRASH_SHARE_SELFTEST=PASS
for the [da1025-B] arm4 fixture block)
"""

import os
import re
import sys
import tempfile

DEFINER_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/components/FullscreenImageViewer.kt"
ACTIVITY_REL = "src/android/app/src/main/java/com/rikkaminis/app/webapp/WebAppActivity.kt"
MAIN_ROOT = "src/android/app/src/main"
SETTINGS_DIR = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings"
PROVIDER_REL = SETTINGS_DIR + "/ProviderDetailScreen.kt"
MCP_REL = SETTINGS_DIR + "/MCPIntegrationsScreen.kt"
EXPORTER_REL = "src/android/app/src/main/java/com/rikkaminis/app/share/ChatExporter.kt"
CRASH_REL = "src/android/app/src/main/java/com/rikkaminis/app/crash/CrashFrequencyDetector.kt"

# Covers cacheDir/share (plural writers) and cacheDir/shared, for ANY receiver
# (`context.cacheDir` / `ctx.cacheDir` / ...). The literal run is bounded by the
# closing quote so "share_inbound" / "shared-foo" cannot match by accident.
# [da1025-B] the receiver was widened from `context.cacheDir` to
# `(?:\w+\.)?cacheDir`: CrashFrequencyDetector constructs `File(ctx.cacheDir,
# "share")` and the strict pattern was blind to it (this gate's own N+1 hole,
# same shape as the da1024-G "shared" hole).
SHARE_DIR_PAT = re.compile(r"File\(\s*(?:\w+\.)?cacheDir\s*,\s*\"share(?:d)?\"\s*\)")

# arm3: (file, suspend-signature marker, what it stages)
UI_WRITERS = (
    (PROVIDER_REL, "private suspend fun exportProviderInstance"),
    (MCP_REL, "private suspend fun shareMcpServerJson"),
)

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


def extract_braced_block(code, marker):
    """Return the text from `marker` through the matching closing brace.

    Unlike extract_function (which assumes the signature line carries the
    opening brace), this tolerates multi-line parameter lists — both
    exportProviderInstance and shareMcpServerJson brace on their own line.
    """
    start = code.find(marker)
    if start < 0:
        return None
    brace = code.find("{", start)
    if brace < 0:
        return None
    depth = 0
    for i in range(brace, len(code)):
        ch = code[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
    return None


def _read_stripped(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return strip_comments(f.read())


def check_arm3(root):
    """[da1024-G] cacheDir/shared writers: suspend + IO context + prune first."""
    problems = []
    for rel, marker in UI_WRITERS:
        code = _read_stripped(root, rel)
        if code is None:
            problems.append(f"{rel}: file missing (was it renamed/moved?)")
            continue
        fn = extract_braced_block(code, marker)
        if fn is None:
            problems.append(
                f"{rel}: `{marker}` not found — it must stay a "
                "`private suspend fun` (the body runs disk I/O)"
            )
            continue
        if "withContext(Dispatchers.IO)" not in fn:
            problems.append(
                f"{rel}: `{marker}` must run its mkdirs/prune/write inside "
                "withContext(Dispatchers.IO)"
            )
            continue
        prune = fn.find("pruneShareDir(")
        write = fn.find(".writeText(")
        if prune < 0:
            problems.append(f"{rel}: `{marker}` must call pruneShareDir( before staging")
        elif write < 0:
            problems.append(f"{rel}: `{marker}` lost its staging .writeText( call")
        elif prune > write:
            problems.append(
                f"{rel}: pruneShareDir( must precede the staging write "
                f"(prune={prune} write={write})"
            )

    # ChatExporter.exportToZip already runs inside withContext(Dispatchers.IO);
    # pin that the prune sits between the dir construction and the zip stream.
    code = _read_stripped(root, EXPORTER_REL)
    if code is None:
        problems.append(f"{EXPORTER_REL}: file missing (was it renamed/moved?)")
        return problems
    fn = extract_braced_block(code, "suspend fun exportToZip")
    if fn is None:
        problems.append(f"{EXPORTER_REL}: `suspend fun exportToZip` not found")
        return problems
    io_idx = fn.find("withContext(Dispatchers.IO)")
    dir_idx = fn.find('File(context.cacheDir, "shared")')
    prune_idx = fn.find("pruneShareDir(")
    zip_idx = fn.find("ZipOutputStream(")
    if io_idx < 0:
        problems.append(f"{EXPORTER_REL}: exportToZip lost its withContext(Dispatchers.IO) block")
    elif dir_idx < 0:
        problems.append(f'{EXPORTER_REL}: exportToZip lost the File(context.cacheDir, "shared") construction')
    elif prune_idx < 0:
        problems.append(f"{EXPORTER_REL}: exportToZip must call pruneShareDir( before staging the zip")
    elif zip_idx < 0:
        problems.append(f"{EXPORTER_REL}: exportToZip lost its ZipOutputStream( staging call")
    elif not (io_idx < dir_idx < prune_idx < zip_idx):
        problems.append(
            f"{EXPORTER_REL}: exportToZip ordering broken: expected withContext(Dispatchers.IO) "
            f'< File(cacheDir, "shared") < pruneShareDir < ZipOutputStream, got '
            f"io={io_idx} dir={dir_idx} prune={prune_idx} zip={zip_idx}"
        )
    return problems


CRASH_OK = """package com.rikkaminis.app.crash

import com.rikkaminis.app.ui.components.pruneShareDir

object CrashFrequencyDetector {
    private suspend fun packageZip(ctx: Context, files: List<File>): File? =
        withContext(Dispatchers.IO) {
        val readable = files.filter { it.exists() && it.length() > 0 }
        if (readable.isEmpty()) return@withContext null
        val shareDir = File(ctx.cacheDir, "share").apply { mkdirs() }
        pruneShareDir(shareDir)
        val zipFile = File(shareDir, "minis-logs-stamp.zip")
        ZipOutputStream(FileOutputStream(zipFile).buffered()).use { zout ->
            zout.write(byteArrayOf(1))
        }
        return@withContext zipFile
    }
}
"""


def check_arm4(root):
    """[da1025-B] CrashFrequencyDetector.packageZip: suspend + IO + prune-first."""
    code = _read_stripped(root, CRASH_REL)
    if code is None:
        return [f"{CRASH_REL}: file missing (was it renamed/moved?)"]
    fn = extract_braced_block(code, "private suspend fun packageZip")
    if fn is None:
        return [
            f"{CRASH_REL}: `packageZip` must stay a `private suspend fun` — "
            "its mkdir/prune/zip work runs disk I/O off the calling (UI) thread"
        ]
    io_idx = fn.find("withContext(Dispatchers.IO)")
    dir_idx = fn.find('File(ctx.cacheDir, "share")')
    prune_idx = fn.find("pruneShareDir(")
    zip_idx = fn.find("ZipOutputStream(")
    if io_idx < 0:
        return [
            f"{CRASH_REL}: packageZip lost its withContext(Dispatchers.IO) block "
            "— zip packaging ran back on the calling thread"
        ]
    if dir_idx < 0:
        return [f'{CRASH_REL}: packageZip lost the File(ctx.cacheDir, "share") construction']
    if prune_idx < 0:
        return [f"{CRASH_REL}: packageZip must call the shared pruneShareDir( pruner before writing"]
    if zip_idx < 0:
        return [f"{CRASH_REL}: packageZip lost its ZipOutputStream( staging call"]
    if not (io_idx < dir_idx < prune_idx < zip_idx):
        return [
            f"{CRASH_REL}: packageZip ordering broken: expected withContext(Dispatchers.IO) < "
            f'File(ctx.cacheDir, "share") < pruneShareDir < ZipOutputStream, got '
            f"io={io_idx} dir={dir_idx} prune={prune_idx} zip={zip_idx}"
        ]
    # `withContext`'s block parameter is crossinline => a bare non-local `return`
    # inside it does not compile ("'return' is prohibited here"). CI run
    # 39087482248 caught exactly this at :979 after the [da1025-B] conversion;
    # ban the shape here so the next edit fails locally instead of on CI.
    if re.search(r"(?<![\w.@])return\s+[^\s@]", fn):
        return [
            f"{CRASH_REL}: packageZip contains a bare `return` inside the "
            "withContext(Dispatchers.IO) block — withContext's block is crossinline, "
            "so only `return@withContext` compiles ('return' is prohibited here)"
        ]
    return []


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


# --- [da1024-G] arm3 fixtures: the three cacheDir/shared writers ------------

PROVIDER_OK = """package com.rikkaminis.app.ui.settings

private suspend fun exportProviderInstance(
    context: android.content.Context,
    providerRepository: ProviderRepository,
) {
    val file = withContext(Dispatchers.IO) {
        val json = providerRepository.exportInstanceJSON(id) ?: return@withContext null
        val dir = java.io.File(context.cacheDir, "shared").apply { mkdirs() }
        pruneShareDir(dir)
        val f = java.io.File(dir, "label.json")
        runCatching { f.writeText(json) }.map { f }.getOrNull()
    } ?: return
    context.startActivity(android.content.Intent())
}
"""

MCP_OK = """package com.rikkaminis.app.ui.settings

private suspend fun shareMcpServerJson(context: android.content.Context, serverId: String, json: String) {
    val file = withContext(Dispatchers.IO) {
        val dir = java.io.File(context.cacheDir, "shared").apply { mkdirs() }
        pruneShareDir(dir)
        val f = java.io.File(dir, "server-mcp.json")
        runCatching { f.writeText(json) }.map { f }.getOrNull()
    } ?: return
    context.startActivity(android.content.Intent())
}
"""

EXPORTER_OK = """package com.rikkaminis.app.share

class ChatExporter {
    suspend fun exportToZip(context: Context): Pair<Uri, Summary> = withContext(Dispatchers.IO) {
        val sharedDir = File(context.cacheDir, "shared").apply { mkdirs() }
        pruneShareDir(sharedDir)
        val zipFile = File(sharedDir, "t.zip")
        ZipOutputStream(FileOutputStream(zipFile).buffered()).use { it.flush() }
        uri to summary
    }
}
"""

# The gate's own N+1 tooth: `"shared"` producers were invisible to the old
# SHARE_DIR_PAT (which demanded `)` right after the `"share"` literal).
SHARED_PRODUCER_NO_PRUNE = """package com.rikkaminis.app.other

class SharedWriter {
    val dir = java.io.File(context.cacheDir, "shared").apply { mkdirs() }
    fun write() { java.io.File(dir, "a.json").writeText("{}") }
}
"""


def run_crash_fixture(name, crash_content, expect_ok):
    """Write the five contracted files (CrashFrequencyDetector overridden) and
    grade arm1+arm2+arm3+arm4."""
    files = {
        ACTIVITY_REL: COMPLIANT,
        PROVIDER_REL: PROVIDER_OK,
        MCP_REL: MCP_OK,
        EXPORTER_REL: EXPORTER_OK,
        CRASH_REL: crash_content,
    }
    with tempfile.TemporaryDirectory() as tmp:
        for r, body in files.items():
            path = os.path.join(tmp, r)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(body)
        a1 = check_arm1(tmp)
        a2 = check_arm2(tmp)
        a3 = check_arm3(tmp)
        a4 = check_arm4(tmp)
        ok = not a1 and a2 is None and not a3 and not a4
        status = "PASS" if ok == expect_ok else "FAIL"
        print(f"  [{status}] fixture {name}: arm1={a1!r} arm2={a2!r} arm3={a3!r} arm4={a4!r}")
        return status == "PASS"


def run_writer_fixture(name, rel, content, expect_ok):
    """Write the four contracted files (one overridden) and grade arm1+arm2+arm3."""
    files = {
        ACTIVITY_REL: COMPLIANT,
        PROVIDER_REL: PROVIDER_OK,
        MCP_REL: MCP_OK,
        EXPORTER_REL: EXPORTER_OK,
        rel: content,
    }
    with tempfile.TemporaryDirectory() as tmp:
        for r, body in files.items():
            path = os.path.join(tmp, r)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(body)
        a1 = check_arm1(tmp)
        a2 = check_arm2(tmp)
        a3 = check_arm3(tmp)
        ok = not a1 and a2 is None and not a3
        status = "PASS" if ok == expect_ok else "FAIL"
        print(f"  [{status}] fixture {name}: arm1={a1!r} arm2={a2!r} arm3={a3!r}")
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
    # arm1 tooth for the "shared" pattern (the N+1 this batch closed): a
    # `cacheDir/shared` producer with no pruner must be caught, while the
    # pattern must stay blind to it under the OLD regex — printed below.
    ok &= run_writer_fixture(
        "shared-producer-without-prune",
        "src/android/app/src/main/java/com/rikkaminis/app/other/SharedWriter.kt",
        SHARED_PRODUCER_NO_PRUNE,
        expect_ok=False,
    )
    old_pat = re.compile(r"File\(\s*context\.cacheDir\s*,\s*\"share\"\s*\)")
    blind = old_pat.search(SHARED_PRODUCER_NO_PRUNE) is None
    ok &= blind
    print(f"  [{'PASS' if blind else 'FAIL'}] fixture shared-producer is invisible to the old pattern (regression tooth)")
    # arm3: compliant baseline is green...
    ok &= run_writer_fixture("writers-compliant", EXPORTER_REL, EXPORTER_OK, expect_ok=True)
    # ...and each criterion has exactly one fixture that breaks it.
    ok &= run_writer_fixture(
        "provider-not-suspend",
        PROVIDER_REL,
        PROVIDER_OK.replace("private suspend fun exportProviderInstance(", "private fun exportProviderInstance("),
        expect_ok=False,
    )
    ok &= run_writer_fixture(
        "provider-without-io-context",
        PROVIDER_REL,
        PROVIDER_OK.replace("withContext(Dispatchers.IO) {", "run {"),
        expect_ok=False,
    )
    ok &= run_writer_fixture(
        "provider-without-prune",
        PROVIDER_REL,
        PROVIDER_OK.replace("        pruneShareDir(dir)\n", ""),
        expect_ok=False,
    )
    ok &= run_writer_fixture(
        "mcp-without-io-context",
        MCP_REL,
        MCP_OK.replace("withContext(Dispatchers.IO) {", "run {"),
        expect_ok=False,
    )
    ok &= run_writer_fixture(
        "exporter-without-prune",
        EXPORTER_REL,
        EXPORTER_OK.replace("        pruneShareDir(sharedDir)\n", ""),
        expect_ok=False,
    )
    # --- [da1025-B] arm4 fixtures: CrashFrequencyDetector.packageZip ---------
    print("  [da1025-B] arm4 (crash export chain):")
    ok &= run_crash_fixture("crash-compliant", CRASH_OK, expect_ok=True)
    ok &= run_crash_fixture(
        "crash-without-prune",
        CRASH_OK.replace("        pruneShareDir(shareDir)\n", ""),
        expect_ok=False,
    )
    ok &= run_crash_fixture(
        "crash-prune-after-write",
        CRASH_OK.replace(
            "        pruneShareDir(shareDir)\n"
            '        val zipFile = File(shareDir, "minis-logs-stamp.zip")\n'
            "        ZipOutputStream(FileOutputStream(zipFile).buffered()).use { zout ->\n"
            "            zout.write(byteArrayOf(1))\n"
            "        }\n",
            '        val zipFile = File(shareDir, "minis-logs-stamp.zip")\n'
            "        ZipOutputStream(FileOutputStream(zipFile).buffered()).use { zout ->\n"
            "            zout.write(byteArrayOf(1))\n"
            "        }\n"
            "        pruneShareDir(shareDir)\n",
        ),
        expect_ok=False,
    )
    ok &= run_crash_fixture(
        "crash-zip-on-main-thread",
        CRASH_OK.replace("        withContext(Dispatchers.IO) {", "        run {"),
        expect_ok=False,
    )
    # regression tooth: the OLD strict regex (context.cacheDir only) was blind
    # to the `File(ctx.cacheDir, "share")` form this batch uncovered.
    ok &= run_crash_fixture(
        "crash-bare-return",
        CRASH_OK.replace("return@withContext zipFile", "return zipFile"),
        expect_ok=False,
    )
    blind2 = re.compile(r"File\(\s*context\.cacheDir\s*,\s*\"share\"\s*\)").search(CRASH_OK) is None
    ok &= blind2 and 'File(ctx.cacheDir, "share")' in CRASH_OK
    print(f"  [{'PASS' if blind2 else 'FAIL'}] fixture crash file's ctx.cacheDir form is invisible to the old strict pattern (regression tooth)")
    print("CRASH_SHARE_SELFTEST=" + ("PASS" if ok else "FAIL"))
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
    a3 = check_arm3(root)
    if a3:
        failures.append(f"arm3 (cacheDir/shared writers must be suspend + IO + prune): {a3}")
    a4 = check_arm4(root)
    if a4:
        failures.append(f"arm4 ([da1025-B] CrashFrequencyDetector.packageZip suspend + IO + prune): {a4}")
    if failures:
        for f in failures:
            print("FAIL:", f)
        sys.exit(1)
    print("share_dir_prune_check: OK (arm1 + arm2 + arm3 + arm4 green)")


if __name__ == "__main__":
    main()

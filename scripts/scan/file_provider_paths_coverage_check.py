#!/usr/bin/env python3
"""FileProvider path coverage gate (deep-audit-1023 ISSUE-01, FIX-A).

事故 (真机可复现，不是 latent):
  `res/xml/file_provider_paths.xml` declares the directories
  `androidx.core.content.FileProvider` is allowed to serve. Three dirs were
  written and handed to `FileProvider.getUriForFile` but never declared —
  `cacheDir/large-messages/` (LargeContentGuard oversized-message export),
  `filesDir/media/` (MediaStore-staged chat attachments) and
  `filesDir/photos-export/` (android-photos offload output). Undeclared root
  ⇒ `getUriForFile` throws `IllegalArgumentException` ⇒ tapping the export
  button always toasted "export failed" while the file stayed behind as a
  permanent orphan (nothing deleted `large-messages/`), and external-open /
  share of a chat attachment always fell through to a `file://` URI that the
  receiving app cannot read.

判据 (all code/XML anchors — never comment text):
  C1  every writer dir is declared under the matching FileProvider tag
  C2  the writer sites named by the registry still exist (a renamed or
      removed dir means the registry silently stopped covering anything)
  C3  oversized-message export runs off the main thread: `exportLargeContent-
      ToFile` is `suspend`, its body switches with `withContext(Dispatchers.IO)`,
      and the call site is inside a coroutine `launch` whose scope comes from
      `rememberCoroutineScope()`
  C4  the export prunes expired orphans before writing, wrapped so a prune
      failure cannot block the export (`runCatching { prune... }`), and the
      prune is defined with a literal TTL constant
  C5  `getUriForFile` runs AFTER `writeText` inside the export (no
      "URI handed out for a file that is not on disk yet" race)
  C7  no full-coverage provider root (`<external-path>`, `<root-path>`,
      `path="."`/`path=""`) — widening the provider to a whole volume is a
      separate, explicitly approved decision, not something a coverage fix
      may slip in.

用法:
  python3 file_provider_paths_coverage_check.py [repo_root]
  python3 file_provider_paths_coverage_check.py --root <dir>
  python3 file_provider_paths_coverage_check.py --selftest

夹具:
  --selftest builds temp dirs — one CLEAN fixture plus a dirty variant per
  criterion — and requires every dirty fixture to turn red.
"""
import re
import sys
import tempfile
from pathlib import Path

XML_REL = "src/android/app/src/main/res/xml/file_provider_paths.xml"
KT_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/LargeContentGuard.kt"
MEDIASTORE_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/storage/MediaStore.kt"
PHOTOS_REL = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/PhotosOffloadHandler.kt"

# (registry id, writer file, regex proving the dir is written, provider tag,
#  declared path). This list IS the coverage contract: a new dir handed to
#  getUriForFile must be added here AND declared in the XML.
WRITER_DIRS = [
    ("large-messages", KT_REL,
     r'File\(\s*context\.cacheDir,\s*"large-messages"\s*\)',
     "cache-path", "large-messages/"),
    ("media", MEDIASTORE_REL,
     r'File\(\s*context\.filesDir,\s*"media"\s*\)',
     "files-path", "media/"),
    ("photos-export", PHOTOS_REL,
     r'File\(\s*context\.filesDir,\s*"photos-export"\s*\)',
     "files-path", "photos-export/"),
]

TTL_EXPECTED = "internal const val LARGE_MESSAGE_EXPORT_TTL_MS = 24L * 60L * 60L * 1000L"

DECL_RE = re.compile(
    r'<\s*(files-path|cache-path|external-path|external-files-path|external-cache-path|root-path)'
    r'\s+name\s*=\s*"([^"]*)"\s+path\s*=\s*"([^"]*)"\s*/?>',
    re.M,
)


def strip_comments(text: str) -> str:
    """Remove line and block comments without touching string literals.

    Anchors must be code, never comment text — the audit trail for this bug
    is a KDoc that described the flow, not a check that enforced it.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            if text.startswith('"""', i):
                j = text.find('"""', i + 3)
                j = n if j < 0 else j + 3
                out.append(text[i:j])
                i = j
                continue
            out.append(c)
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    out.append(text[i:i + 2])
                    i += 2
                else:
                    out.append(text[i])
                    i += 1
            if i < n:
                out.append('"')
                i += 1
            continue
        if c == "'":  # char literal, possibly escaped
            out.append(c)
            i += 1
            while i < n and text[i] != "'":
                if text[i] == "\\":
                    out.append(text[i:i + 2])
                    i += 2
                else:
                    out.append(text[i])
                    i += 1
            if i < n:
                out.append("'")
                i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def declared_roots(xml_text: str):
    """Return (list of (tag, name, path), violations)."""
    decls = DECL_RE.findall(xml_text)
    violations = []
    names = [d[1] for d in decls]
    dupes = sorted({x for x in names if names.count(x) > 1})
    if dupes:
        violations.append(f"C6 DUPLICATE provider root name(s): {dupes} — FileProvider throws "
                          "IllegalStateException at first use when two roots share a name")
    for tag, name, path in decls:
        if tag in ("external-path", "root-path") or path in (".", "", "./"):
            violations.append(f"C7 FULL-COVERAGE provider root <{tag} name=\"{name}\" path=\"{path}\"> — "
                              "a whole volume / the whole app dir must not be registered by a "
                              "coverage fix; that widening is an explicitly approved decision "
                              "(deep-audit-1023 FIX-A 报告 §6 超界申请)")
    return decls, violations


def fn_body(code: str, signature_re: str):
    """Brace-matched body of the function whose signature matches, or None."""
    m = re.search(signature_re, code)
    if not m:
        return None, None
    start = code.index("{", m.end() - 1) if "{" in code[m.end():] else -1
    if start < 0:
        return None, None
    depth = 0
    for i in range(start, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[start:i + 1], start
    return None, None


def check(root: Path) -> list:
    violations = []

    xml_path = root / XML_REL
    kt_path = root / KT_REL
    if not xml_path.exists():
        return [f"{XML_REL} MISSING"]
    if not kt_path.exists():
        return [f"{KT_REL} MISSING"]

    xml_text = xml_path.read_text()
    code = strip_comments(kt_path.read_text())

    decls, xml_violations = declared_roots(xml_text)
    violations += xml_violations
    declared_set = {(tag, path) for tag, _n, path in decls}

    # ── C1 / C2: every writer dir is declared, and the writer still exists ──
    for rid, rel, writer_re, tag, path in WRITER_DIRS:
        wpath = root / rel
        if not wpath.exists():
            violations.append(f"C2 [{rid}] writer file MISSING: {rel}")
            continue
        wcode = strip_comments(wpath.read_text())
        if not re.search(writer_re, wcode):
            violations.append(
                f"C2 [{rid}] writer site no longer matches `{writer_re}` in {rel} — the "
                "coverage registry points at a dir that is not written any more; update "
                "WRITER_DIRS together with the XML")
        if (tag, path) not in declared_set:
            violations.append(
                f"C1 [{rid}] dir `{path}` is written ({rel}) but not declared as "
                f"<{tag} path=\"{path}\"> in {XML_REL} — getUriForFile throws "
                "IllegalArgumentException for every file under it")

    # ── C3: export must leave the main thread ───────────────────────────────
    if not re.search(r'private\s+suspend\s+fun\s+exportLargeContentToFile\s*\(', code):
        violations.append("C3 exportLargeContentToFile is not `private suspend fun` — the export "
                          "was a blocking disk write on the UI thread (deep-audit-1023 ISSUE-01)")
    body, _off = fn_body(code, r'private\s+suspend\s+fun\s+exportLargeContentToFile\s*\(')
    if body is None:
        violations.append("C3 exportLargeContentToFile body not found — cannot verify IO dispatch")
    else:
        if "withContext(Dispatchers.IO)" not in body:
            violations.append("C3 exportLargeContentToFile body has no `withContext(Dispatchers.IO)` "
                              "— mkdirs / listFiles / writeText must run off the main thread")
        # ── C4: prune wired before the write, failure non-blocking ─────────
        if "runCatching { pruneExpiredLargeMessageFiles(" not in body:
            violations.append("C4 exportLargeContentToFile does not wrap the orphan prune in "
                              "`runCatching { pruneExpiredLargeMessageFiles(...) }` — exports must "
                              "stop leaving permanent files behind, and a prune failure must not "
                              "block the export the user asked for")
        prune_at = body.find("pruneExpiredLargeMessageFiles(")
        write_at = body.find("writeText(")
        if prune_at >= 0 and write_at >= 0 and prune_at > write_at:
            violations.append("C4 prune runs after writeText — the fresh export would be inside "
                              "the swept set; prune must precede the write")
        # ── C5: URI handed out only after the bytes are on disk ────────────
        uri_at = body.find("FileProvider.getUriForFile(")
        if write_at >= 0 and uri_at >= 0 and uri_at < write_at:
            violations.append("C5 getUriForFile precedes writeText in the export — the chooser may "
                              "get a URI for a file that is not written yet")

    if not re.search(r'\binternal\s+fun\s+pruneExpiredLargeMessageFiles\s*\(', code):
        violations.append("C4 pruneExpiredLargeMessageFiles is not defined in LargeContentGuard.kt")
    if TTL_EXPECTED not in code:
        violations.append(f"C4 TTL literal changed — expected `{TTL_EXPECTED}` (recalibrating it "
                          "is a deliberate act: update this gate in the same commit)")

    # call site: suspend function must be invoked from a coroutine launched on a
    # composition scope, not from a plain onClick on the UI thread
    if "rememberCoroutineScope()" not in code:
        violations.append("C3 LargeContentGuard.kt has no `rememberCoroutineScope()` — the export "
                          "button needs a composition-scoped coroutine to leave the main thread")
    call_at = code.find("exportLargeContentToFile(context")
    if call_at < 0:
        violations.append("C3 export call site not found — LargeContentBadge must invoke "
                          "exportLargeContentToFile")
    else:
        window = code[max(0, call_at - 200):call_at]
        if ".launch" not in window:
            violations.append("C3 export call site is not inside a coroutine `launch` — a suspend "
                              "export called straight from onClick would not compile off-thread, "
                              "and a non-suspend one blocks the UI thread")

    return violations


# ── fixtures ────────────────────────────────────────────────────────────────

CLEAN_XML = """<?xml version="1.0" encoding="utf-8"?>
<paths xmlns:android="http://schemas.android.com/apk/res/android">
    <cache-path name="shared" path="shared/" />
    <cache-path name="share" path="share/" />
    <cache-path name="large-messages" path="large-messages/" />
    <files-path name="media" path="media/" />
    <files-path name="photos-export" path="photos-export/" />
    <files-path name="logs" path="logs/" />
</paths>
"""

CLEAN_KT = '''package com.rikkaminis.app.ui.chat

import androidx.core.content.FileProvider
import java.io.File

internal const val LARGE_MESSAGE_EXPORT_TTL_MS = 24L * 60L * 60L * 1000L

internal fun pruneExpiredLargeMessageFiles(
    exportDir: File,
    nowMs: Long,
    maxAgeMs: Long = LARGE_MESSAGE_EXPORT_TTL_MS,
): Int {
    val children = exportDir.listFiles() ?: return 0
    val cutoff = nowMs - maxAgeMs
    var deleted = 0
    for (f in children) {
        if (f.isFile && f.lastModified() < cutoff && runCatching { f.delete() }.getOrDefault(false)) {
            deleted++
        }
    }
    return deleted
}

@Composable
internal fun LargeContentBadge(content: String, stableKey: String) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    TextButton(
        onClick = {
            scope.launch {
                val ok = exportLargeContentToFile(context, content, stableKey)
                Toast.makeText(context, if (ok) "ok" else "fail").show()
            }
        },
    ) { }
}

private suspend fun exportLargeContentToFile(
    context: android.content.Context,
    content: String,
    stableKey: String,
): Boolean = try {
    val file = withContext(Dispatchers.IO) {
        val dir = File(context.cacheDir, "large-messages").apply { mkdirs() }
        runCatching { pruneExpiredLargeMessageFiles(dir, System.currentTimeMillis()) }
            .onFailure { AppLogger.warning("LargeContentGuard", "prune failed") }
        val safeKey = stableKey.replace(Regex("[^A-Za-z0-9_.-]"), "_").take(64)
        val f = File(dir, "msg-$safeKey.txt")
        f.writeText(content)
        f
    }
    val uri = FileProvider.getUriForFile(context, context.packageName + ".fileprovider", file)
    context.startActivity(chooserFor(uri))
    true
} catch (t: Throwable) {
    false
}
'''

CLEAN_MEDIASTORE = '''package com.rikkaminis.app.data.storage

import android.content.Context
import java.io.File

class MediaStore(private val context: Context) {
    val mediaBaseDir: File = File(context.filesDir, "media")
}
'''

CLEAN_PHOTOS = '''package com.rikkaminis.app.sandbox.offload

import android.content.Context
import java.io.File

class PhotosOffloadHandler(private val context: Context) {
    fun outDir(): File = File(context.filesDir, "photos-export").also { it.mkdirs() }
}
'''


def write_fixture(base: Path, xml=None, kt=None, media=None, photos=None):
    def put(rel, text):
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    put(XML_REL, xml if xml is not None else CLEAN_XML)
    put(KT_REL, kt if kt is not None else CLEAN_KT)
    put(MEDIASTORE_REL, media if media is not None else CLEAN_MEDIASTORE)
    put(PHOTOS_REL, photos if photos is not None else CLEAN_PHOTOS)


def selftest() -> int:
    results = []

    def run(name, expect_red, **kwargs):
        with tempfile.TemporaryDirectory(prefix="fpcheck_") as td:
            base = Path(td)
            write_fixture(base, **kwargs)
            v = check(base)
            if expect_red:
                hits = sorted({x.split("]")[0] + "]" for x in v})
                ok = bool(v)
                note = f"red on {hits}" if ok else "NOT RED"
            else:
                ok = not v
                note = "clean" if ok else f"UNEXPECTED: {v}"
            results.append((name, ok, note))

    # The clean fixture must pass: that is the contract for the real tree.
    run("CLEAN", expect_red=False)

    run(
        "XML missing large-messages",
        expect_red=True,
        xml=CLEAN_XML.replace('    <cache-path name="large-messages" path="large-messages/" />\n', ""),
    )
    run(
        "XML missing media",
        expect_red=True,
        xml=CLEAN_XML.replace('    <files-path name="media" path="media/" />\n', ""),
    )
    run(
        "XML missing photos-export",
        expect_red=True,
        xml=CLEAN_XML.replace('    <files-path name="photos-export" path="photos-export/" />\n', ""),
    )
    # A whole-volume root must not sneak in with a coverage fix.
    run(
        "XML wide external-path root",
        expect_red=True,
        xml=CLEAN_XML.replace("</paths>", '    <external-path name="everything" path="." />\n</paths>'),
    )
    run(
        "XML duplicate root name",
        expect_red=True,
        xml=CLEAN_XML.replace('    <files-path name="logs" path="logs/" />',
                              '    <files-path name="media" path="logs/" />'),
    )
    # Writer site renamed ⇒ the registry stopped covering anything real.
    run(
        "writer dir renamed",
        expect_red=True,
        media=CLEAN_MEDIASTORE.replace('File(context.filesDir, "media")',
                                       'File(context.filesDir, "media2")'),
    )
    # Export back on the main thread (non-suspend, no withContext).
    run(
        "export not suspend",
        expect_red=True,
        kt=CLEAN_KT.replace("private suspend fun exportLargeContentToFile(",
                            "private fun exportLargeContentToFile("),
    )
    run(
        "export missing Dispatchers.IO",
        expect_red=True,
        kt=CLEAN_KT.replace("val file = withContext(Dispatchers.IO) {", "val file = run {")
          .replace("    val uri = FileProvider", "    val uri = FileProvider"),
    )
    run(
        "prune call removed",
        expect_red=True,
        kt=CLEAN_KT.replace(
            '        runCatching { pruneExpiredLargeMessageFiles(dir, System.currentTimeMillis()) }\n'
            '            .onFailure { AppLogger.warning("LargeContentGuard", "prune failed") }\n', ""),
    )
    run(
        "prune after write",
        expect_red=True,
        kt=CLEAN_KT.replace(
            '        runCatching { pruneExpiredLargeMessageFiles(dir, System.currentTimeMillis()) }\n'
            '            .onFailure { AppLogger.warning("LargeContentGuard", "prune failed") }\n'
            '        val safeKey',
            '        val safeKey').replace(
            '        f.writeText(content)\n',
            '        f.writeText(content)\n'
            '        runCatching { pruneExpiredLargeMessageFiles(dir, System.currentTimeMillis()) }\n'),
    )
    run(
        "uri before write",
        expect_red=True,
        kt=CLEAN_KT.replace(
            "        val f = File(dir, \"msg-$safeKey.txt\")\n",
            "        val f = File(dir, \"msg-$safeKey.txt\")\n"
            "        val early = FileProvider.getUriForFile(context, \"a\", f)\n"
            "        if (early != null) println(early)\n"),
    )
    run(
        "TTL literal recalibrated silently",
        expect_red=True,
        kt=CLEAN_KT.replace("LARGE_MESSAGE_EXPORT_TTL_MS = 24L * 60L * 60L * 1000L",
                            "LARGE_MESSAGE_EXPORT_TTL_MS = 7L * 24L * 60L * 60L * 1000L"),
    )
    run(
        "call site outside launch",
        expect_red=True,
        kt=CLEAN_KT.replace("            scope.launch {\n", "            run {\n"),
    )

    print("=== file_provider_paths_coverage --selftest ===")
    failed = 0
    for name, ok, note in results:
        if not ok:
            failed += 1
        print(f"[{'ok' if ok else 'FAIL'}] {name} -> {note}")
    if failed:
        print(f"SELFTEST: {failed} arm(s) misbehaved")
        return 1
    print(f"FILE_PROVIDER_PATHS_SELFTEST=PASS ({len(results)} arms)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if args and args[0] == "--root":
        if len(args) < 2:
            print("usage: file_provider_paths_coverage_check.py --root <dir>")
            return 2
        root = Path(args[1])
    elif args:
        root = Path(args[0])
    else:
        root = Path(__file__).resolve().parents[2]
    violations = check(root)
    for v in violations:
        print("VIOLATION:", v)
    print(f"Summary: {len(violations)} violation(s) found")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())

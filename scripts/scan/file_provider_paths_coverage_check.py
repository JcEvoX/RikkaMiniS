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
  C8  [fix/da1024-A] the `cacheDir/share_inbound/` writer copies on
      `Dispatchers.IO` (`addAttachmentFromStagedShare` is `suspend`, its body
      switches) AND prunes its own orphans there under a literal TTL constant —
      nothing used to delete those staged copies (deep-audit-1024 ISSUE-01).
  C9  [fix/da1024-A] the ChatScreen share drain keeps its disk work on IO: the
      `LaunchedEffect(shareBufferVersion)` body switches with
      `withContext(Dispatchers.IO)` and the `cleanSharedFiles(` directory sweep
      sits inside one of those regions.

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
# [fix/da1024-A] deep-audit-1024 ISSUE-01: the share_inbound staging dir lives in
# ChatViewModel.kt and is drained from ChatScreen.kt.
CHATVM_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatViewModel.kt"
SCREEN_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatScreen.kt"

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
    ("share-inbound", CHATVM_REL,
     r'File\(\s*context\.cacheDir,\s*"share_inbound"\s*\)',
     "cache-path", "share_inbound/"),
]

TTL_EXPECTED = "internal const val LARGE_MESSAGE_EXPORT_TTL_MS = 24L * 60L * 60L * 1000L"
SHARE_TTL_EXPECTED = "internal const val SHARE_INBOUND_TTL_MS = 24L * 60L * 60L * 1000L"

IO_OPEN_RE = re.compile(r'withContext\(\s*Dispatchers\.IO\s*\)\s*\{')

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


def brace_body(code: str, open_idx: int):
    """Brace-matched body starting at the `{` at [open_idx], or None."""
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[open_idx:i + 1]
    return None


def io_regions(code: str):
    """Brace-matched bodies of every `withContext(Dispatchers.IO) { ... }` —
    the only anchor that proves a disk call is actually inside the IO switch
    rather than merely sitting next to it."""
    out = []
    for m in IO_OPEN_RE.finditer(code):
        body = brace_body(code, code.index("{", m.start()))
        if body is not None:
            out.append(body)
    return out


def check(root: Path) -> list:
    violations = []

    xml_path = root / XML_REL
    kt_path = root / KT_REL
    if not xml_path.exists():
        return [f"{XML_REL} MISSING"]
    if not kt_path.exists():
        return [f"{KT_REL} MISSING"]
    for rel in (CHATVM_REL, SCREEN_REL):
        if not (root / rel).exists():
            return [f"{rel} MISSING"]

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

    # ── C8 [fix/da1024-A]: the staged-share copy runs on IO and prunes ──────
    #     its own orphans (deep-audit-1024 ISSUE-01 face 2 + 3). The dir is
    #     written, never deleted, and the copy is up to 100 MB per file
    #     (ShareReceiverActivity cap) — all of it used to run on the UI thread.
    vm_code = strip_comments((root / CHATVM_REL).read_text())
    if not re.search(r'internal\s+fun\s+pruneExpiredShareInboundFiles\s*\(', vm_code):
        violations.append("C8 pruneExpiredShareInboundFiles is not defined as a top-level "
                          "`internal fun` in ChatViewModel.kt — nothing would ever delete the "
                          "share_inbound staging copies")
    if SHARE_TTL_EXPECTED not in vm_code:
        violations.append(f"C8 TTL literal changed — expected `{SHARE_TTL_EXPECTED}` "
                          "(recalibrating it is a deliberate act: update this gate in the same "
                          "commit)")
    if not re.search(r'suspend\s+fun\s+addAttachmentFromStagedShare\s*\(', vm_code):
        violations.append("C8 addAttachmentFromStagedShare is not `suspend` — a non-suspending "
                          "signature means the 100 MB stream copy cannot leave the UI thread")
    vm_body, _vm_off = fn_body(vm_code, r'fun\s+addAttachmentFromStagedShare\s*\(')
    if vm_body is None:
        violations.append("C8 addAttachmentFromStagedShare body not found — cannot verify the "
                          "share_inbound copy left the main thread")
    else:
        regions = io_regions(vm_body)
        if not regions:
            violations.append("C8 addAttachmentFromStagedShare body has no "
                              "`withContext(Dispatchers.IO)` — mkdirs / the TTL prune / the "
                              "stream copy would run on the UI thread again "
                              "(deep-audit-1024 ISSUE-01)")
        elif not any("pruneExpiredShareInboundFiles(" in r for r in regions):
            violations.append("C8 the share_inbound prune is not inside "
                              "`withContext(Dispatchers.IO)` in addAttachmentFromStagedShare — a "
                              "directory walk on the UI thread right before a 100 MB copy")

    # ── C9 [fix/da1024-A]: the ChatScreen share drain keeps its disk work on
    #     IO. Anchor is the `LaunchedEffect(shareBufferVersion)` body itself, so
    #     a `withContext(Dispatchers.IO)` that drifted to an unrelated call site
    #     does not satisfy the criterion.
    scr_code = strip_comments((root / SCREEN_REL).read_text())
    scr_body, _scr_off = fn_body(scr_code, r'LaunchedEffect\s*\(\s*shareBufferVersion\s*\)')
    if scr_body is None:
        violations.append("C9 ChatScreen share-drain ingestion point not found — expected a "
                          "`LaunchedEffect(shareBufferVersion) {` block in ChatScreen.kt")
    else:
        if "addAttachmentFromStagedShare(" not in scr_body:
            violations.append("C9 ChatScreen share drain no longer calls "
                              "addAttachmentFromStagedShare — a staged share would stop being "
                              "attached to the composer")
        regions = io_regions(scr_body)
        if not regions:
            violations.append("C9 the ChatScreen share drain has no "
                              "`withContext(Dispatchers.IO)` — consumeBuffer / the attachment "
                              "copy / the staging-dir sweep all run on the UI thread again "
                              "(deep-audit-1024 ISSUE-01)")
        elif not any("cleanSharedFiles(" in r for r in regions):
            violations.append("C9 SharedShareStore.cleanSharedFiles is called outside "
                              "`withContext(Dispatchers.IO)` in the ChatScreen share drain — "
                              "listFiles() + per-file delete() on the UI thread")

    return violations


# ── fixtures ────────────────────────────────────────────────────────────────

CLEAN_XML = """<?xml version="1.0" encoding="utf-8"?>
<paths xmlns:android="http://schemas.android.com/apk/res/android">
    <cache-path name="shared" path="shared/" />
    <cache-path name="share" path="share/" />
    <cache-path name="large-messages" path="large-messages/" />
    <cache-path name="share-inbound" path="share_inbound/" />
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

# [fix/da1024-A] minimal stand-in for ui/chat/ChatViewModel.kt: the
# share_inbound writer, its suspend IO switch and the orphan prune.
CLEAN_CHATVM = '''package com.rikkaminis.app.ui.chat

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

class ChatViewModel(private val context: android.content.Context) {

    suspend fun addAttachmentFromStagedShare(file: File): Any? {
        if (!file.exists()) return null
        val durable = withContext(Dispatchers.IO) {
            val durableDir = File(context.cacheDir, "share_inbound").apply { mkdirs() }
            runCatching { pruneExpiredShareInboundFiles(durableDir, System.currentTimeMillis()) }
                .onFailure { Log.w(TAG, "share_inbound prune failed: ${it.message}") }
            val f = File(durableDir, "${java.util.UUID.randomUUID()}-${file.name}")
            try {
                file.inputStream().use { input ->
                    f.outputStream().use { output -> input.copyTo(output) }
                }
                f
            } catch (e: Exception) {
                Log.w(TAG, "failed to copy staged share file ${file.name}: ${e.message}")
                null
            }
        } ?: return null
        addAttachment(durable)
        return durable
    }
}

internal const val SHARE_INBOUND_TTL_MS = 24L * 60L * 60L * 1000L

internal fun pruneExpiredShareInboundFiles(
    inboundDir: File,
    nowMs: Long,
    maxAgeMs: Long = SHARE_INBOUND_TTL_MS,
): Int {
    val children = inboundDir.listFiles() ?: return 0
    val cutoff = nowMs - maxAgeMs
    var deleted = 0
    for (f in children) {
        if (f.isFile && f.lastModified() < cutoff && runCatching { f.delete() }.getOrDefault(false)) {
            deleted++
        }
    }
    return deleted
}
'''

# [fix/da1024-A] minimal stand-in for ui/chat/ChatScreen.kt: the share drain.
CLEAN_SCREEN = '''package com.rikkaminis.app.ui.chat

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

@Composable
fun ChatScreen(sessionId: String, viewModel: ChatViewModel) {
    val shareBufferVersion by ShareCoordinator.bufferVersion.collectAsState()
    LaunchedEffect(shareBufferVersion) {
        if (shareBufferVersion == 0) return@LaunchedEffect
        val pending = withContext(Dispatchers.IO) {
            ShareCoordinator.consumeBuffer(context)
        } ?: return@LaunchedEffect
        val sharedDir = withContext(Dispatchers.IO) {
            SharedShareStore.sharedFileDirectory(context)
        }
        for (item in pending.items) {
            when (item.kind) {
                PendingShare.Item.Kind.INLINE_TEXT -> {
                    viewModel.setInputText(item.value)
                }
                PendingShare.Item.Kind.ATTACHMENT -> {
                    viewModel.addAttachmentFromStagedShare(File(sharedDir, item.value))
                }
            }
        }
        viewModel.markShareInjected()
        withContext(Dispatchers.IO) {
            SharedShareStore.cleanSharedFiles(context)
        }
    }
}
'''


def write_fixture(base: Path, xml=None, kt=None, media=None, photos=None, chatvm=None, screen=None):
    def put(rel, text):
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    put(XML_REL, xml if xml is not None else CLEAN_XML)
    put(KT_REL, kt if kt is not None else CLEAN_KT)
    put(MEDIASTORE_REL, media if media is not None else CLEAN_MEDIASTORE)
    put(PHOTOS_REL, photos if photos is not None else CLEAN_PHOTOS)
    put(CHATVM_REL, chatvm if chatvm is not None else CLEAN_CHATVM)
    put(SCREEN_REL, screen if screen is not None else CLEAN_SCREEN)


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

    # ── [fix/da1024-A] deep-audit-1024 ISSUE-01: share_inbound wiring ───────
    run(
        "XML missing share-inbound",
        expect_red=True,
        xml=CLEAN_XML.replace('    <cache-path name="share-inbound" path="share_inbound/" />\n', ""),
    )
    run(
        "share_inbound writer dir renamed",
        expect_red=True,
        chatvm=CLEAN_CHATVM.replace('File(context.cacheDir, "share_inbound")',
                                    'File(context.cacheDir, "share_inbound2")'),
    )
    run(
        "share_inbound prune call removed",
        expect_red=True,
        chatvm=CLEAN_CHATVM.replace(
            '            runCatching { pruneExpiredShareInboundFiles(durableDir, System.currentTimeMillis()) }\n'
            '                .onFailure { Log.w(TAG, "share_inbound prune failed: ${it.message}") }\n', ""),
    )
    run(
        "share_inbound prune outside the IO switch",
        expect_red=True,
        chatvm=(CLEAN_CHATVM
                .replace('            runCatching { pruneExpiredShareInboundFiles(durableDir, System.currentTimeMillis()) }\n'
                         '                .onFailure { Log.w(TAG, "share_inbound prune failed: ${it.message}") }\n', "")
                .replace('        val durable = withContext(Dispatchers.IO) {\n',
                         '        pruneExpiredShareInboundFiles(File(context.cacheDir, "share_inbound"), 0L)\n'
                         '        val durable = withContext(Dispatchers.IO) {\n')),
    )
    run(
        "share_inbound copy not on IO",
        expect_red=True,
        chatvm=CLEAN_CHATVM.replace("val durable = withContext(Dispatchers.IO) {",
                                    "val durable = run {"),
    )
    run(
        "share_inbound copy no longer suspend",
        expect_red=True,
        chatvm=CLEAN_CHATVM.replace("suspend fun addAttachmentFromStagedShare(",
                                    "fun addAttachmentFromStagedShare("),
    )
    run(
        "share_inbound prune not internal top-level",
        expect_red=True,
        chatvm=CLEAN_CHATVM.replace("\ninternal fun pruneExpiredShareInboundFiles(",
                                    "\nprivate fun pruneExpiredShareInboundFiles("),
    )
    run(
        "share_inbound TTL literal recalibrated silently",
        expect_red=True,
        chatvm=CLEAN_CHATVM.replace("SHARE_INBOUND_TTL_MS = 24L * 60L * 60L * 1000L",
                                    "SHARE_INBOUND_TTL_MS = 7L * 24L * 60L * 60L * 1000L"),
    )
    run(
        "ChatScreen sweep outside the IO switch",
        expect_red=True,
        screen=CLEAN_SCREEN.replace(
            '        withContext(Dispatchers.IO) {\n            SharedShareStore.cleanSharedFiles(context)\n        }\n',
            '        SharedShareStore.cleanSharedFiles(context)\n'),
    )
    run(
        "ChatScreen drain has no IO switch at all",
        expect_red=True,
        screen=CLEAN_SCREEN.replace("withContext(Dispatchers.IO) {", "run {"),
    )
    run(
        "ChatScreen drain stops attaching staged files",
        expect_red=True,
        screen=CLEAN_SCREEN.replace("viewModel.addAttachmentFromStagedShare(File(sharedDir, item.value))",
                                    "Unit"),
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

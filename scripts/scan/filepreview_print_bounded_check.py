#!/usr/bin/env python3
"""
Gate: FIX-H (deep-audit-1020 ISSUE-08, P2) — the print path of
FilePreviewScreen.kt must not read the whole file into the heap.

ISSUE-08: `buildPrintHtml` used to call `item.file.readBytes()` — the one
remaining unbounded read in the file (the other three preview surfaces had
already been fixed to `readCapped` under [rv1014-E1]). Printing a multi-GB
.log/.txt pulled the whole file into the heap BEFORE decodeUtf8Capped applied
its 512_000-byte cap. The fix routes the read through the same `readCapped`
helper, which returns at most MAX+1 bytes and still yields the truncated
verdict in a single pass; the printed bytes are unchanged by design.

[da1026-F] extended — the `.readBytes()` ban was measured, but measured on the
wrong axis: a *spelling*, not the property. `CsvPreview` read the file with
`bufferedReader(...).readLine()` and a 200-ROW cap, which reads as "bounded"
while `readLine()` still materialises the whole line — a CSV with one
multi-hundred-MB line grew a single String to the file size and died with
`OutOfMemoryError`, an `Error` the caller's `catch (e: Exception)` never saw.
So the file is now checked for bare `bufferedReader(` / `.readText()` /
`.readLines()` as well — deliberately with NO whitelist inside readCapped,
because that body IS the bound and letting it spell a raw read would
reintroduce the very OOM. Same round also pins the second defect:
`TextPreview` and `MarkdownPreview` held their preview state in unkeyed
`remember { }`, so a stale `error` from the previous file survived the switch
and masked the new one — both bodies must key their state on `item.file`.

Checks assert PRESENCE of the corrected routing rather than the absence of a
spelling, so a refactor that keeps the contract is a deliberate edit to this
gate rather than a silent pass. Bodies are located by brace-balanced anchor
and FAIL CLOSED when the anchor is missing or duplicated.

Usage: python3 scripts/scan/filepreview_print_bounded_check.py [repo_root]
       python3 scripts/scan/filepreview_print_bounded_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import re
import shutil
import sys
import tempfile

TARGET_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/FilePreviewScreen.kt"

# buildPrintHtml must read through the bounded helper and keep the decode
# boundary handling wired in.
BOUNDED_READ = "readCapped(item.file, MAX_TEXT_PREVIEW_BYTES)"
DECODE_WIRED = "decodeUtf8Capped("
BUILD_ANCHOR = "private fun buildPrintHtml(item: FileItem): String {"

# The whole file (comments stripped) must contain zero unbounded reads —
# :1275 was the last one. The prose in KDoc/line comments mentions
# `readBytes()` on purpose (history notes), so comments are stripped first.
UNBOUNDED_CALL = ".readBytes()"

# [da1026-F] Every other read spelling a preview could reach for. Deliberately
# NOT whitelisted inside readCapped: that body IS the bound, and letting the
# bound itself spell `readBytes()` / `bufferedReader().readLine()` would
# reintroduce the very OOM this gate exists to stop. One rule for the whole
# file, same as the `.readBytes()` criterion above (see BAD_READCAP_BARE, the
# fixture that proves the criterion bites inside readCapped too).
BARE_READS = ("bufferedReader(", ".readText()", ".readLines()")

# [da1026-F] Preview composables whose state must be keyed on the file. An
# unkeyed `remember { }` is reused across a file switch (Compose keys by slot
# position), which is how a failed read's error banner ended up masking the
# next file — the success path never clears `error`.
KEYED_REMEMBER = "by remember(item.file) {"
BARE_REMEMBER = "remember {"
REMEMBER_ANCHORS = (
    "private fun TextPreview(item: FileItem) {",
    "private fun MarkdownPreview(item: FileItem) {",
)



def strip_comments(text):
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


def grab_body(text, sig):
    """Verbatim body of a brace-balanced declaration, or None."""
    n = text.count(sig)
    if n != 1:
        return None
    i = text.index(sig)
    depth = 0
    k = text.index("{", i)
    while k < len(text):
        c = text[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i:k + 1]
        k += 1
    return None


def check(src):
    v = []
    body = grab_body(src, BUILD_ANCHOR)
    if body is None:
        v.append("buildPrintHtml must exist exactly once (anchor missing or duplicated)")
        return v
    if BOUNDED_READ not in body:
        v.append("buildPrintHtml must read through readCapped(item.file, MAX_TEXT_PREVIEW_BYTES)")
    if DECODE_WIRED not in body:
        v.append("buildPrintHtml must keep decodeUtf8Capped wired in (decode-boundary handling)")
    if UNBOUNDED_CALL in body:
        v.append("buildPrintHtml body must not contain readBytes()")
    code = strip_comments(src)
    hits = code.count(UNBOUNDED_CALL)
    if hits != 0:
        v.append("FilePreviewScreen.kt has %d unbounded .readBytes() call(s) outside comments "
                 "(no whitelist — every call must go through readCapped)" % hits)
    # [da1026-F] Bare reader spellings. Computed on comment-stripped code, so
    # the KDoc history notes about the old `bufferedReader(...)` call can
    # neither satisfy nor trip it. No whitelist — see BARE_READS.
    for needle in BARE_READS:
        n = code.count(needle)
        if n:
            v.append("FilePreviewScreen.kt has %d bare %s read(s) "
                     "(every preview read must go through readCapped/decodeUtf8Capped)"
                     % (n, needle))
    # [da1026-F] Preview state must be keyed on the file.
    for sig in REMEMBER_ANCHORS:
        fbody = grab_body(code, sig)
        if fbody is None:
            v.append("%s must exist exactly once (anchor missing or duplicated)" % sig)
            continue
        if BARE_REMEMBER in fbody:
            v.append("%s holds unkeyed remember { } state — it must be keyed on item.file "
                     "(a stale error banner otherwise survives the file switch)" % sig)
        if KEYED_REMEMBER not in fbody:
            v.append("%s must key its preview state on item.file "
                     "(no 'by remember(item.file) {{' found)" % sig)
    for needle, why in (
        ("private fun readCapped(file: File, maxBytes: Int): ByteArray {",
         "the readCapped helper must stay defined"),
        ("private fun decodeUtf8Capped(bytes: ByteArray, cap: Int = MAX_TEXT_PREVIEW_BYTES): String {",
         "the decodeUtf8Capped helper must stay defined"),
        ("private const val MAX_TEXT_PREVIEW_BYTES = 512_000",
         "the preview cap constant must stay defined"),
    ):
        if needle not in code:
            v.append("%s (required token missing)" % why)
    return v


# ── [deep-audit-1041 B-07] the log-share path ──────────────────────────────
#
# Same defect family, second file. `LogManagementScreen.shareLogFile`'s
# FileProvider failure arm ran `file.readText().take(100_000)` — the whole file
# into one `String`, THEN trimmed — and the whole path ran inside the
# composable's `onClick` (a main-thread `File.exists()` too). The KDoc a screen
# up in that very file records what a 33 MB day of agent traffic did to the same
# pattern in the log viewer, so this is not a hypothetical.
#
# The subject is the DECISION, not the spelling: (a) no whole-file read may
# exist in the file, (b) a bounded reader must exist whose body reads through a
# stream and honours a cap, (c) the chooser is built inside a
# `withContext(Dispatchers.IO)` block, (d) `startActivity` is NOT inside one —
# it has to run on the caller's (Main) thread.
LOG_SCREEN_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/LogManagementScreen.kt"
IO_BLOCK_RE = re.compile(r"withContext\s*\(\s*Dispatchers\.IO\s*\)\s*\{")
CAP_TOKEN = re.compile(r"\bmaxChars\b|\bLOG_SHARE_MAX_CHARS\b")
FUN_DECL_RE = re.compile(r"\bfun\s+\w+\s*\(")


def _log_body_at(text, open_idx):
    """Index just past the `}` closing text[open_idx]; -1 when unbalanced."""
    depth, i = 0, open_idx
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _log_functions(text):
    """[(signature, body)] for every `fun` in `text`.

    Both halves start at the DECLARATION's line, so `private suspend fun …` keeps
    its modifiers and an expression body (`= withContext(Dispatchers.IO) { … }`)
    keeps the call that opens it — measuring from the `fun` keyword alone drops
    the very token the criteria look for.
    """
    out = []
    for m in FUN_DECL_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        brace = text.find("{", m.end())
        if brace < 0:
            continue
        end = _log_body_at(text, brace)
        if end < 0:
            continue
        out.append((text[line_start:brace], text[line_start:end]))
    return out


def _io_regions(text):
    """[(start, end)] covering every `withContext(Dispatchers.IO) { … }` block."""
    regions = []
    for m in IO_BLOCK_RE.finditer(text):
        open_idx = text.find("{", m.start(), m.end())
        if open_idx < 0:
            continue
        end = _log_body_at(text, open_idx)
        if end < 0:
            continue
        regions.append((open_idx, end))
    return regions


def check_log_share(root):
    """(violations, rot, sites, notes) — criterion B-07."""
    violations, rot, notes = [], [], []
    path = os.path.join(root, LOG_SCREEN_REL)
    pkg = os.path.dirname(path)
    if not os.path.isfile(path):
        if os.path.isdir(pkg) and os.listdir(pkg):
            rot.append(
                "%s lost its anchor (the ui/settings package is there, the file is not)"
                % LOG_SCREEN_REL
            )
        else:
            notes.append(
                "[PARTIAL] the log-share anchor — this tree never shipped %s" % LOG_SCREEN_REL
            )
        return violations, rot, 0, notes

    with open(path, encoding="utf-8", errors="replace") as fh:
        code = strip_comments(fh.read())
    sites = 0

    for needle in BARE_READS + (UNBOUNDED_CALL,):
        n = code.count(needle)
        sites += 1
        if n:
            violations.append(
                "%s: %d whole-file read(s) (%s) — the share fallback is the one place a "
                "log file is read as text, and a 33 MB day must not become a single "
                "String on the heap" % (LOG_SCREEN_REL, n, needle)
            )

    bodies = [b for _sig, b in _log_functions(code)]
    sites += 1
    if not any("FileInputStream(" in b and CAP_TOKEN.search(b) for b in bodies):
        violations.append(
            "%s: no bounded reader found — the share fallback needs one that streams "
            "through FileInputStream and stops at a cap" % LOG_SCREEN_REL
        )

    prep = None
    launch = None
    for sig, body in _log_functions(code):
        if "FileProvider.getUriForFile(" in body:
            prep = (sig, body)
        if "startActivity(" in body:
            launch = (sig, body)
    if prep is None:
        rot.append(
            "%s: no function builds the FileProvider share any more — the off-main "
            "criterion would pass by looking at nothing" % LOG_SCREEN_REL
        )
    else:
        sites += 1
        body = prep[1]
        if not IO_BLOCK_RE.search(body):
            violations.append(
                "%s: the share prep (FileProvider lookup + the fallback read) runs on the "
                "caller's thread — wrap it in withContext(Dispatchers.IO)" % LOG_SCREEN_REL
            )
        if "startActivity(" in body:
            violations.append(
                "%s: startActivity is inside the share prep — it must run on the caller's "
                "(Main) thread, so the prep has to return the chooser" % LOG_SCREEN_REL
            )
    if launch is None:
        rot.append(
            "%s: nothing starts the share chooser any more — the criterion lost its "
            "subject" % LOG_SCREEN_REL
        )
    else:
        sites += 1
        sig, body = launch
        if "suspend" not in sig:
            violations.append(
                "%s: the share entry point is not a `suspend fun` — it cannot hop off the "
                "click's thread to prepare the chooser" % LOG_SCREEN_REL
            )
        offset = code.find(body)
        starts = offset if offset >= 0 else 0
        act = body.find("startActivity(")
        if act >= 0 and any(s <= starts + act < e for s, e in _io_regions(code)):
            violations.append(
                "%s: startActivity sits inside a Dispatchers.IO block — the chooser must be "
                "started on the Main thread" % LOG_SCREEN_REL
            )
    return violations, rot, sites, notes


# ── [deep-audit-1041 B-07] fixture: the log-share decision, written out ─────

def _log_share_screen(shape="fixed"):
    """The B-07 subject. [shape]: fixed / readtext / on_main / start_in_io /
    no_bounded_reader / no_reader_at_all."""
    header = (
        "package com.rikkaminis.app.ui.settings\n"
        "\n"
        "import kotlinx.coroutines.Dispatchers\n"
        "import kotlinx.coroutines.withContext\n"
        "\n"
        "internal const val LOG_SHARE_MAX_CHARS = 100_000\n"
        "\n"
    )
    fixed_reader = (
        "internal fun readLogShareText(file: File, maxChars: Int = LOG_SHARE_MAX_CHARS): String? =\n"
        "    try {\n"
        "        FileInputStream(file).use { input ->\n"
        "            val reader = InputStreamReader(input, Charsets.UTF_8)\n"
        "            val builder = StringBuilder(minOf(maxChars, 64 * 1024))\n"
        "            val buffer = CharArray(8 * 1024)\n"
        "            while (builder.length < maxChars) {\n"
        "                val read = reader.read(buffer, 0, minOf(buffer.size, maxChars - builder.length))\n"
        "                if (read <= 0) break\n"
        "                builder.append(buffer, 0, read)\n"
        "            }\n"
        "            builder.toString()\n"
        "        }\n"
        "    } catch (_: Exception) { null }\n"
        "\n"
    )
    if shape == "no_bounded_reader":
        reader = (
            "internal fun readLogShareText(file: File): String? =\n"
            "    try { file.bufferedReader(Charsets.UTF_8).readLine() } catch (_: Exception) { null }\n"
            "\n"
        )
    elif shape == "no_reader_at_all":
        reader = ""
    else:
        reader = fixed_reader

    if shape == "readtext":
        fallback = ('            val text = try { file.readText().take(100_000) } '
                    'catch (_: Exception) { return }\n')
    else:
        fallback = "            val text = readLogShareText(file) ?: return\n"

    inner = (
        "        if (!file.exists()) return null\n"
        "        try {\n"
        "            val uri = FileProvider.getUriForFile(context, \"authority\", file)\n"
        "            Intent.createChooser(Intent(Intent.ACTION_SEND), \"share\")\n"
        "        } catch (e: Exception) {\n"
        + fallback +
        "            Intent.createChooser(Intent(Intent.ACTION_SEND), \"share\")\n"
        "        }\n"
    )

    if shape == "on_main":
        prep = (
            "private fun shareLogFile(context: Context, file: File) {\n"
            + inner +
            "        context.startActivity(Intent(Intent.ACTION_SEND))\n"
            "}\n"
        )
    elif shape == "start_in_io":
        prep = (
            "private suspend fun buildLogShareChooser(context: Context, file: File): Intent? =\n"
            "    withContext(Dispatchers.IO) {\n"
            + inner +
            "    }\n"
            "\n"
            "private suspend fun shareLogFile(context: Context, file: File) {\n"
            "    withContext(Dispatchers.IO) {\n"
            "        context.startActivity(buildLogShareChooser(context, file))\n"
            "    }\n"
            "}\n"
        )
    else:
        prep = (
            "private suspend fun buildLogShareChooser(context: Context, file: File): Intent? =\n"
            "    withContext(Dispatchers.IO) {\n"
            + inner +
            "    }\n"
            "\n"
            "private suspend fun shareLogFile(context: Context, file: File) {\n"
            "    val chooser = buildLogShareChooser(context, file) ?: return\n"
            "    context.startActivity(chooser)\n"
            "}\n"
        )
    return header + reader + prep


def _write(root, rel, text):
    path = os.path.join(root, rel)
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _log_share_fixture(shape="fixed", drop=False, keep_pkg=True, drop_preview=False):
    root = tempfile.mkdtemp(prefix="logshare_")
    if not drop_preview:
        _write(root, TARGET_REL, GOOD_SRC)
    if not drop:
        _write(root, LOG_SCREEN_REL, _log_share_screen(shape))
    elif keep_pkg:
        _write(root, "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/OtherScreen.kt",
               "package com.rikkaminis.app.ui.settings\n\nclass OtherScreen\n")
    return root


def selftest_log_share():
    """One dirty arm per B-07 criterion; every one must turn the gate red."""
    failures = []

    def arm(name, expect_red, needle=None, **kwargs):
        root = _log_share_fixture(**kwargs)
        try:
            v, rot, sites, _notes = check_log_share(root)
            red = bool(v or rot)
            ok = red == expect_red
            if ok and needle is not None:
                hay = " ".join(v + rot)
                if needle not in hay:
                    ok = False
            failures.append((name, ok, needle or ("violations=%s rot=%s" % (v, rot))))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    arm("the bounded, off-main share path passes", False)
    arm("a whole-file readText() in the share fallback is caught", True,
        "whole-file read(s)", shape="readtext")
    arm("the share prep on the caller's thread is caught", True,
        "runs on the caller's thread", shape="on_main")
    arm("startActivity inside the IO block is caught", True,
        "inside a Dispatchers.IO block", shape="start_in_io")
    arm("a fallback reader with no cap is caught", True,
        "no bounded reader", shape="no_bounded_reader")
    arm("a file that lost its share anchor is a rot failure", True,
        "lost its anchor", drop=True)
    arm("a tree that never shipped ui/settings is skipped, not failed", False,
        drop=True, keep_pkg=False)

    for name, ok, note in failures:
        print("[%s] B-07 %s -> %s" % ("ok" if ok else "FAIL", name, note))
    return len([1 for _n, ok, _x in failures if not ok])


GOOD_SRC = """package probe
// a KDoc mentioning readBytes() on purpose must NOT trip the whole-file count:
// [rv1014-E1] was item.file.readBytes()
private const val MAX_TEXT_PREVIEW_BYTES = 512_000

private fun buildPrintHtml(item: FileItem): String {
    val raw = decodeUtf8Capped(readCapped(item.file, MAX_TEXT_PREVIEW_BYTES))
    return buildString { append(raw) }
}

private fun decodeUtf8Capped(bytes: ByteArray, cap: Int = MAX_TEXT_PREVIEW_BYTES): String {
    val truncated = bytes.size > cap
    return String(bytes.copyOf(cap), Charsets.UTF_8)
}

private fun readCapped(file: File, maxBytes: Int): ByteArray {
    val out = java.io.ByteArrayOutputStream(minOf(maxBytes + 1, 64 * 1024))
    val buf = ByteArray(64 * 1024)
    file.inputStream().use { input ->
        var remaining = maxBytes + 1
        while (remaining > 0) {
            val n = input.read(buf, 0, minOf(buf.size, remaining))
            if (n < 0) break
            out.write(buf, 0, n)
            remaining -= n
        }
    }
    return out.toByteArray()
}

@Composable
private fun TextPreview(item: FileItem) {
    var content by remember(item.file) { mutableStateOf<String?>(null) }
    var truncated by remember(item.file) { mutableStateOf(false) }
    var error by remember(item.file) { mutableStateOf<String?>(null) }
}

@Composable
private fun MarkdownPreview(item: FileItem) {
    var content by remember(item.file) { mutableStateOf<String?>(null) }
    var error by remember(item.file) { mutableStateOf<String?>(null) }
}
"""

BAD_UNBOUNDED = GOOD_SRC.replace(
    "val raw = decodeUtf8Capped(readCapped(item.file, MAX_TEXT_PREVIEW_BYTES))",
    "val raw = decodeUtf8Capped(item.file.readBytes())",
)

BAD_DECODE_DROPPED = GOOD_SRC.replace(
    "val raw = decodeUtf8Capped(readCapped(item.file, MAX_TEXT_PREVIEW_BYTES))",
    'val raw = String(readCapped(item.file, MAX_TEXT_PREVIEW_BYTES), Charsets.UTF_8)',
)

BAD_STRAY = GOOD_SRC.replace(
    "return buildString { append(raw) }",
    "return buildString { append(raw) }; val extra = File(\"x\").readBytes()",
)

# [da1026-F] The OOM shape the old gate measured past: no .readBytes(), yet an
# unbounded reader (`readLine()` grows a String to the file size).
BAD_BARE_BUFFERED = GOOD_SRC.replace(
    "return buildString { append(raw) }",
    "return buildString { append(raw) }; val s = File(\"x\").bufferedReader(Charsets.UTF_8).readLine()",
)

# [da1026-F] Other bare-read spellings.
BAD_READ_TEXT = GOOD_SRC.replace(
    "return buildString { append(raw) }",
    'return buildString { append(raw) }; val t = File("x").readText()',
)

BAD_READ_LINES = GOOD_SRC.replace(
    "return buildString { append(raw) }",
    'return buildString { append(raw) }; val ls = File("x").readLines()',
)

# [da1026-F] readCapped is deliberately NOT whitelisted: if the bound itself
# reached for a bare reader, it would reintroduce the very OOM this gate
# guards. This fixture proves the criterion bites inside readCapped too.
BAD_READCAP_BARE = GOOD_SRC.replace(
    "val n = input.read(buf, 0, minOf(buf.size, remaining))",
    "val n = buf.size; val all = file.readText()",
)

# [da1026-F] Unkeyed remember in TextPreview (slot reuse across file switch).
BAD_TEXT_UNKEYED = GOOD_SRC.replace(
    "private fun TextPreview(item: FileItem) {\n"
    "    var content by remember(item.file) { mutableStateOf<String?>(null) }\n"
    "    var truncated by remember(item.file) { mutableStateOf(false) }\n"
    "    var error by remember(item.file) { mutableStateOf<String?>(null) }\n"
    "}",
    "private fun TextPreview(item: FileItem) {\n"
    "    var content by remember { mutableStateOf<String?>(null) }\n"
    "    var truncated by remember(item.file) { mutableStateOf(false) }\n"
    "    var error by remember(item.file) { mutableStateOf<String?>(null) }\n"
    "}",
)

# [da1026-F] Unkeyed remember in MarkdownPreview.
BAD_MD_UNKEYED = GOOD_SRC.replace(
    "private fun MarkdownPreview(item: FileItem) {\n"
    "    var content by remember(item.file) { mutableStateOf<String?>(null) }\n"
    "    var error by remember(item.file) { mutableStateOf<String?>(null) }\n"
    "}",
    "private fun MarkdownPreview(item: FileItem) {\n"
    "    var content by remember { mutableStateOf<String?>(null) }\n"
    "    var error by remember(item.file) { mutableStateOf<String?>(null) }\n"
    "}",
)

# [da1026-F] The keys dropped ENTIRELY from a preview body (no
# remember(item.file) at all) must fail closed, not pass on the absence of
# the bare spelling alone.
BAD_TEXT_KEYS_GONE = GOOD_SRC.replace(
    "private fun TextPreview(item: FileItem) {\n"
    "    var content by remember(item.file) { mutableStateOf<String?>(null) }\n"
    "    var truncated by remember(item.file) { mutableStateOf(false) }\n"
    "    var error by remember(item.file) { mutableStateOf<String?>(null) }\n"
    "}",
    "private fun TextPreview(item: FileItem) {\n"
    "    val content = produceState<String?>(null) {}\n"
    "}",
)


def selftest():
    failures = []

    def expect_clean(name, v):
        if v:
            failures.append("%s must be clean, got %s" % (name, v))

    def expect_trip(name, v, needle):
        if not any(needle in item for item in v):
            failures.append("%s did not trip %r (got %s)" % (name, needle, v))

    good = check(GOOD_SRC)
    expect_clean("compliant fixture", good)

    bad_unbounded = check(BAD_UNBOUNDED)
    expect_trip("reverted-to-readBytes fixture", bad_unbounded, "readCapped(item.file")
    expect_trip("reverted-to-readBytes fixture", bad_unbounded, "must not contain readBytes()")
    expect_trip("reverted-to-readBytes fixture", bad_unbounded, "1 unbounded .readBytes()")

    bad_decode = check(BAD_DECODE_DROPPED)
    expect_trip("decode-dropped fixture", bad_decode, "decodeUtf8Capped wired in")

    bad_stray = check(BAD_STRAY)
    expect_trip("stray-readBytes fixture", bad_stray, "1 unbounded .readBytes()")

    # [da1026-F] fixtures — each trips exactly the criterion built for it.
    bad_buffered = check(BAD_BARE_BUFFERED)
    expect_trip("bare-bufferedReader fixture", bad_buffered, "bare bufferedReader(")

    bad_readtext = check(BAD_READ_TEXT)
    expect_trip("bare-readText fixture", bad_readtext, "bare .readText()")

    bad_readlines = check(BAD_READ_LINES)
    expect_trip("bare-readLines fixture", bad_readlines, "bare .readLines()")

    bad_outside = check(BAD_READCAP_BARE)
    expect_trip("bare-read-inside-readCapped fixture", bad_outside, "bare .readText()")

    bad_text_unkeyed = check(BAD_TEXT_UNKEYED)
    expect_trip("text unkeyed-remember fixture", bad_text_unkeyed, "TextPreview")
    expect_trip("text unkeyed-remember fixture", bad_text_unkeyed, "unkeyed remember { }")

    bad_md_unkeyed = check(BAD_MD_UNKEYED)
    expect_trip("markdown unkeyed-remember fixture", bad_md_unkeyed, "MarkdownPreview")

    bad_keys_gone = check(BAD_TEXT_KEYS_GONE)
    expect_trip("text keys-gone fixture", bad_keys_gone, "key its preview state on item.file")

    # [da1026-F] fail-closed: a body that lost its anchor must be red, not skipped.
    missing_anchor = check(GOOD_SRC.replace(REMEMBER_ANCHORS[0], "private fun TextPreviewX(item: FileItem) {"))
    expect_trip("missing TextPreview anchor (fail-closed)", missing_anchor, "anchor missing or duplicated")
    dup_anchor = check(
        GOOD_SRC.replace("private fun MarkdownPreview(item: FileItem) {", REMEMBER_ANCHORS[0])
    )
    expect_trip("duplicated TextPreview anchor (fail-closed)", dup_anchor, "anchor missing or duplicated")
    missing_readcap = check(GOOD_SRC.replace("private fun readCapped(file: File, maxBytes: Int): ByteArray {", "private fun readCappedX(file: File, maxBytes: Int): ByteArray {"))
    expect_trip("missing readCapped token (fail-closed)", missing_readcap, "the readCapped helper must stay defined")

    print("selftest compliant            -> %d violation(s)" % len(good))
    print("selftest reverted-readBytes   -> %d violation(s)" % len(bad_unbounded))
    print("selftest decode-dropped       -> %d violation(s)" % len(bad_decode))
    print("selftest stray-readBytes      -> %d violation(s)" % len(bad_stray))
    print("selftest bare-bufferedReader  -> %d violation(s)" % len(bad_buffered))
    print("selftest bare-readText        -> %d violation(s)" % len(bad_readtext))
    print("selftest bare-readLines       -> %d violation(s)" % len(bad_readlines))
    print("selftest bare-inside-readCapped -> %d violation(s)" % len(bad_outside))
    print("selftest text-unkeyed         -> %d violation(s)" % len(bad_text_unkeyed))
    print("selftest markdown-unkeyed     -> %d violation(s)" % len(bad_md_unkeyed))
    print("selftest text-keys-gone       -> %d violation(s)" % len(bad_keys_gone))
    print("selftest anchors-fail-closed  -> 3 arms exercised")

    # [deep-audit-1041 B-07] the second target's arms.
    failures.append("B-07 arms misbehaved" if selftest_log_share() else None)
    failures = [f for f in failures if f]

    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    print("FILEPREVIEW_PRINT_BOUNDED_SELFTEST=PASS")
    return 0


def run(root):
    path = os.path.join(root, TARGET_REL)
    if not os.path.isfile(path):
        print("FAIL missing target: %s" % path)
        return 1
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    v = check(src)
    rc = 0
    if v:
        for item in v:
            print("FAIL %s: %s" % (TARGET_REL, item))
        rc = 1
    else:
        print("PASS %s: buildPrintHtml bounded via readCapped, decode wired, 0 unbounded readBytes" % TARGET_REL)
    # [deep-audit-1041 B-07] second target, same family.
    lv, lrot, lsites, lnotes = check_log_share(root)
    for n in lnotes:
        print("  %s" % n)
    if lrot:
        for r in lrot:
            print("ROT %s" % r)
        rc = 1
    if lv:
        for item in lv:
            print("FAIL %s" % item)
        rc = 1
    if not (lv or lrot):
        print("PASS %s: log share bounded and prepared off the caller's thread (%d decision(s) checked)"
              % (LOG_SCREEN_REL, lsites))
    return rc


def main():
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        return selftest()
    root = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(root)


if __name__ == "__main__":
    sys.exit(main())

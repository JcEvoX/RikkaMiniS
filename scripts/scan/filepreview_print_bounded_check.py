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
import sys

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
    if v:
        for item in v:
            print("FAIL %s: %s" % (TARGET_REL, item))
        return 1
    print("PASS %s: buildPrintHtml bounded via readCapped, decode wired, 0 unbounded readBytes" % TARGET_REL)
    return 0


def main():
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        return selftest()
    root = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(root)


if __name__ == "__main__":
    sys.exit(main())

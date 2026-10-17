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

Checks assert PRESENCE of the corrected routing rather than the absence of a
spelling, so a refactor that keeps the contract is a deliberate edit to this
gate rather than a silent pass.

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
    return file.readBytesCompat(maxBytes)
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

    print("selftest compliant          -> %d violation(s)" % len(good))
    print("selftest reverted-readBytes -> %d violation(s)" % len(bad_unbounded))
    print("selftest decode-dropped     -> %d violation(s)" % len(bad_decode))
    print("selftest stray-readBytes    -> %d violation(s)" % len(bad_stray))

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

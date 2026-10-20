#!/usr/bin/env python3
"""
Gate: FIX-C (deep-audit-1019 ISSUE-03) — ustar name/prefix ceilings must match
what putText can actually store, and a denied tar path must exit non-zero.

ISSUE-03a (FastioTar.kt)
  `putText` reserves the final byte of every fixed-width string field for the
  NUL terminator (`count = minOf(bytes.size, length - 1)`), so the name field
  stores 99 bytes and the prefix field 154. `splitUstarName` used to ask
  "does it fit" with the RAW field sizes (`<= NAME_LENGTH` / `<=
  PREFIX_LENGTH`), so a 100-byte name / 155-byte prefix / 100-byte rest was
  declared to fit and then silently truncated by one byte — `.py` became `.p`,
  a multi-byte UTF-8 character got cut in half, and nothing on the write path
  reported it. The rule: the fit test uses the STORED sizes
  (`NAME_LENGTH - 1` / `PREFIX_LENGTH - 1`), the same way the linkname guard in
  `writeHeader` always did (`> LINKNAME_LENGTH - 1`). Anything one byte larger
  then takes the GNU 'L' long-name record, which has no such ceiling.

ISSUE-03b (FastioOffloadHandler.kt)
  `finishTar(entry, failed, …)` maps `failed` to the exit code (`if (failed) 1
  else 0`). A denied archive path means NOTHING was written, so the call must
  pass `true` — the old `false` returned 0 with an error object in the body,
  which `set -e` and `&&` chains read as success. The rule: every
  `finishTar(deniedEntry(…), <flag>, …)` passes `true`, same as the
  destination-denied branch of tarExtractFrom.

Checks assert PRESENCE of the corrected routing rather than the absence of a
spelling, so a refactor that keeps the contract is a deliberate edit to this
gate rather than a silent pass.

Usage: python3 scripts/scan/fastio_tar_bounds_check.py [repo_root]
       python3 scripts/scan/fastio_tar_bounds_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import re
import sys

TAR_REL = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/FastioTar.kt"
HANDLER_REL = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/FastioOffloadHandler.kt"

# ── ISSUE-03a: the fit test must use the stored sizes ────────────────────────
SPLIT_NAME_OK = 'if (utf8Length(name) <= NAME_LENGTH - 1) return "" to name'
SPLIT_PAIR_OK = (
    "if (utf8Length(prefix) <= PREFIX_LENGTH - 1 && utf8Length(rest) <= NAME_LENGTH - 1) best = i"
)
# A `<= <FIELD>_LENGTH` that is NOT followed by ` - 1` is the stale raw ceiling.
STALE_CEILING_RE = re.compile(
    r"utf8Length\((?:name|prefix|rest)\) <= (?:NAME_LENGTH|PREFIX_LENGTH)(?!\s*-\s*1)"
)
# The NUL-terminator reserve in putText — the reason the ceilings are -1.
PUTTEXT_RESERVE = "val count = minOf(bytes.size, length - 1)"

# ── ISSUE-03b: a denied path is a failure ───────────────────────────────────
DENIED_CALL_RE = re.compile(
    r"finishTar\(deniedEntry\(.*?\)\s*,\s*(true|false)\s*,",
    re.DOTALL,
)
MIN_DENIED_SITES = 2  # archive path (tarCreate) + dest path (tarExtract)


def check_tar(src):
    v = []
    for needle, why in (
        (SPLIT_NAME_OK, "splitUstarName must gate the short-name path on NAME_LENGTH - 1"),
        (SPLIT_PAIR_OK, "splitUstarName must gate the prefix split on FIELD - 1 (both halves)"),
        (PUTTEXT_RESERVE, "putText must keep the final field byte for the NUL terminator"),
    ):
        if needle not in src:
            v.append("%s (required token missing)" % why)
    for i, line in enumerate(src.split("\n")):
        if STALE_CEILING_RE.search(line):
            v.append("FastioTar.kt:%d stale raw-field ceiling: %r" % (i + 1, line.strip()))
    return v


def check_handler(src):
    v = []
    sites = list(DENIED_CALL_RE.finditer(src))
    if len(sites) < MIN_DENIED_SITES:
        v.append(
            "expected >= %d finishTar(deniedEntry(...)) sites (archive + dest), found %d"
            % (MIN_DENIED_SITES, len(sites))
        )
    for m in sites:
        if m.group(1) != "true":
            line = src.count("\n", 0, m.start()) + 1
            v.append(
                "FastioOffloadHandler.kt:%d denied path must finishTar with failed=true (found %s)"
                % (line, m.group(1))
            )
    return v


# ── [dv14-A2] writeRegular: open-before-header + short-payload refill ───────
#
# The header declares `size` bytes of payload. Two consequences:
#   1. If the entry cannot be OPENED, no header may be written — writeLink's
#      order (fail the read, then write the header). Writing the header first
#      declares a payload the stream never receives, and every later member
#      and the end blocks are swallowed as this entry's data.
#   2. If the copy dies MID-WAY, the short payload must be zero-filled back to
#      the declared `size` and the 512-byte boundary restored (GNU tar's
#      "file shrank, padding with zeros" contract), and the failure must still
#      land in out.errors — a truncated archive must not exit 0.
# The refill is guarded (runCatching): a sink that is itself failing (disk
# full) must not mask the read error that is the actual finding.
#
# Scope note: these criteria read ONLY FastioTar.kt. The gate's other view
# reads FastioOffloadHandler.kt, which is another work package's file — do not
# extend these checks onto it. The refill token matches `writeZeros(` or an
# inlined `sink.write(`; a refactor that renames the helper is a deliberate
# edit to this gate, not a silent pass.
def tar_fun_body(src, name):
    start = src.find("fun %s(" % name)
    if start < 0:
        return None
    end = src.find("\nprivate fun ", start + 1)
    if end < 0:
        end = src.find("\ninternal fun ", start + 1)
    return src[start:end] if end > start else src[start:]


def check_write_regular(src):
    v = []
    body = tar_fun_body(src, "writeRegular")
    if body is None:
        return ["writeRegular must exist in FastioTar.kt (required function missing)"]
    open_at = body.find("Files.newInputStream")
    header_at = body.find("writeHeader(")
    if open_at < 0:
        v.append("writeRegular must open the entry's input stream (Files.newInputStream missing)")
    if header_at < 0:
        v.append("writeRegular must write a ustar header")
    if open_at >= 0 and header_at >= 0 and open_at > header_at:
        v.append(
            "writeRegular writes the header BEFORE opening the input stream - an entry "
            "that cannot be read leaves a declared-but-unwritten payload in the stream"
        )
    copy_at = body.find("copy(")
    if copy_at < 0:
        v.append("writeRegular must stream the payload through copy(")
        return v
    catch_at = body.find("catch (", copy_at)
    if catch_at < 0:
        v.append("writeRegular must guard the payload copy with a catch")
        return v
    ret_at = body.find("return", catch_at)
    catch_seg = body[catch_at : ret_at + 1 if ret_at >= 0 else catch_at + 800]
    if "out.errors.add" not in catch_seg:
        v.append("the copy-failure catch must still record out.errors (truncation must not exit 0)")
    has_refill = "writeZeros(" in catch_seg or "sink.write(" in catch_seg
    if not has_refill:
        v.append("the copy-failure catch must zero-fill the short payload back to the declared size")
    elif "runCatching" not in catch_seg:
        v.append("the copy-failure refill must be guarded so a failing sink cannot mask the read error")
    return v


def run(root):
    violations = []
    tar_path = os.path.join(root, TAR_REL)
    handler_path = os.path.join(root, HANDLER_REL)
    if not os.path.isfile(tar_path):
        return 2
    with open(tar_path, encoding="utf-8") as f:
        tar_src = f.read()
    violations += check_tar(tar_src)
    violations += check_write_regular(tar_src)
    if not os.path.isfile(handler_path):
        return 2
    with open(handler_path, encoding="utf-8") as f:
        violations += check_handler(f.read())
    if violations:
        for v in violations:
            print("VIOLATION " + v)
        print("GATE=RED fastio_tar_bounds")
        return 1
    print("GATE=GREEN fastio_tar_bounds")
    return 0


# ── selftest fixtures ────────────────────────────────────────────────────────

GOOD_TAR = """
internal fun splitUstarName(name: String): Pair<String, String>? {
    if (utf8Length(name) <= NAME_LENGTH - 1) return "" to name
    var best = -1
    for (i in name.indices) {
        if (name[i] != '/') continue
        val prefix = name.substring(0, i)
        val rest = name.substring(i + 1)
        if (rest.isEmpty()) continue
        if (utf8Length(prefix) <= PREFIX_LENGTH - 1 && utf8Length(rest) <= NAME_LENGTH - 1) best = i
    }
    return null
}

private fun putText(block: ByteArray, offset: Int, length: Int, value: String) {
    val bytes = value.toByteArray(StandardCharsets.UTF_8)
    val count = minOf(bytes.size, length - 1)
    System.arraycopy(bytes, 0, block, offset, count)
}
"""

# pre-fix shape: raw field sizes (100 / 155) — one byte is silently lost.
BAD_TAR_STALE = GOOD_TAR.replace(
    "if (utf8Length(name) <= NAME_LENGTH - 1) return \"\" to name",
    "if (utf8Length(name) <= NAME_LENGTH) return \"\" to name",
)

# half-fix shape: name gate fixed, prefix gate forgotten.
BAD_TAR_HALF_PREFIX = GOOD_TAR.replace(
    "utf8Length(prefix) <= PREFIX_LENGTH - 1 &&", "utf8Length(prefix) <= PREFIX_LENGTH &&"
)
BAD_TAR_HALF_REST = GOOD_TAR.replace(
    "&& utf8Length(rest) <= NAME_LENGTH - 1)", "&& utf8Length(rest) <= NAME_LENGTH)"
)

GOOD_HANDLER = """
private fun handleTar(request: NativeOffloadRequest): NativeOffloadResult {
    val archiveResolution = GuestPathMapper.resolve(archiveRaw, request.cwd, bindings)
    if (archiveResolution is GuestPathMapper.Resolution.Denied) {
        return finishTar(deniedEntry(archiveRaw, archiveResolution), true, startedNs, args)
    }
    return if (create) {
        tarCreateInto(entry, archivePath, rest, bindings, request, gzip, force, startedNs, args)
    } else {
        tarExtractFrom(entry, archivePath, args, bindings, request, gzip, force, startedNs)
    }
}

private fun tarExtractFrom(entry: JSONObject, archivePath: Path): NativeOffloadResult {
    val destResolution = GuestPathMapper.resolve(destRaw, request.cwd, bindings)
    if (destResolution is GuestPathMapper.Resolution.Denied) {
        return finishTar(deniedEntry(destRaw, destResolution), true, startedNs, args)
    }
    return finishTar(entry, outcome.errors.isNotEmpty(), startedNs, args)
}
"""

# pre-fix shape: the archive-denied branch lies with exit 0.
BAD_HANDLER_FALSE = GOOD_HANDLER.replace(
    "return finishTar(deniedEntry(archiveRaw, archiveResolution), true, startedNs, args)",
    "return finishTar(deniedEntry(archiveRaw, archiveResolution), false, startedNs, args)",
)

# ── [dv14-A2] writeRegular fixtures ──────────────────────────────────────────

# compliant shape: open BEFORE the header, and the copy-failure catch both
# records the error and zero-fills the short payload under a guard.
GOOD_REGULAR = """
internal fun writeRegular(
    sink: OutputStream,
    name: String,
    file: Path,
    out: TarCreateOutcome,
    input: InputStream? = null,
) {
    val size = try {
        Files.size(file)
    } catch (e: Exception) {
        out.errors.add("$name: ${e.message}")
        return
    }
    val stream = input ?: try {
        Files.newInputStream(file)
    } catch (e: Exception) {
        out.errors.add("$name: ${e.message}")
        return
    }
    stream.use {
        writeHeader(sink, header(name, '0', size, modeOf(file), mtimeOf(file), ""))
        out.visited++
        var written = 0L
        val counted = object : OutputStream() {
            override fun write(b: Int) { sink.write(b); written += 1 }
            override fun write(b: ByteArray, off: Int, len: Int) { sink.write(b, off, len); written += len }
        }
        try {
            copy(stream, counted, size)
        } catch (e: Exception) {
            out.errors.add("$name: ${e.message}")
            runCatching {
                writeZeros(sink, size - written)
                writeZeros(sink, boundaryPad(size).toLong())
            }
            return
        }
        writeZeros(sink, boundaryPad(size).toLong())
        out.files++
        out.bytes += size
    }
}
"""

# pre-fix shape: the header goes out before the stream is opened — an entry
# that cannot be read leaves a declared-but-unwritten payload in the stream.
BAD_REGULAR_OLD_ORDER = """
internal fun writeRegular(sink: OutputStream, name: String, file: Path, out: TarCreateOutcome) {
    val size = try {
        Files.size(file)
    } catch (e: Exception) {
        out.errors.add("$name: ${e.message}")
        return
    }
    writeHeader(sink, header(name, '0', size, modeOf(file), mtimeOf(file), ""))
    out.visited++
    try {
        Files.newInputStream(file).use { s -> copy(s, sink, size) }
    } catch (e: Exception) {
        out.errors.add("$name: ${e.message}")
        return
    }
    val pad = ((BLOCK_SIZE - (size % BLOCK_SIZE)) % BLOCK_SIZE).toInt()
    if (pad > 0) sink.write(ByteArray(pad))
    out.files++
    out.bytes += size
}
"""

# half-fix: open-before-header restored, but the copy-failure catch swallows
# without refilling the declared payload — the archive after this member is
# still garbage to a real reader.
BAD_REGULAR_NO_REFILL = GOOD_REGULAR.replace(
    """            runCatching {
                writeZeros(sink, size - written)
                writeZeros(sink, boundaryPad(size).toLong())
            }
            return""",
    "            return",
)

# unguarded refill: the short payload is filled, but a sink that is itself
# failing (disk full) would mask the read error with its own IOException.
BAD_REGULAR_UNGUARDED = GOOD_REGULAR.replace(
    """            runCatching {
                writeZeros(sink, size - written)
                writeZeros(sink, boundaryPad(size).toLong())
            }
            return""",
    """            writeZeros(sink, size - written)
            writeZeros(sink, boundaryPad(size).toLong())
            return""",
)

# silent truncation: the payload is refilled but the read error never lands
# in out.errors, so the handler reports exit 0 for a mangled archive.
BAD_REGULAR_SILENT = GOOD_REGULAR.replace(
    '            out.errors.add("$name: ${e.message}")\n            runCatching {',
    "            runCatching {",
)


def selftest():
    failures = []

    def expect_clean(name, v):
        if v:
            failures.append("%s must be clean, got %s" % (name, v))

    def expect_trip(name, v, needle):
        if not any(needle in item for item in v):
            failures.append("%s did not trip %r (got %s)" % (name, needle, v))

    good_tar = check_tar(GOOD_TAR)
    expect_clean("compliant tar fixture", good_tar)
    good_handler = check_handler(GOOD_HANDLER)
    expect_clean("compliant handler fixture", good_handler)

    bad_stale = check_tar(BAD_TAR_STALE)
    expect_trip("stale ceiling fixture (ISSUE-03a)", bad_stale, "stale raw-field ceiling")
    expect_trip("stale ceiling fixture (ISSUE-03a)", bad_stale, "NAME_LENGTH - 1")

    bad_half_prefix = check_tar(BAD_TAR_HALF_PREFIX)
    expect_trip("half-fix prefix fixture", bad_half_prefix, "stale raw-field ceiling")
    bad_half_rest = check_tar(BAD_TAR_HALF_REST)
    expect_trip("half-fix rest fixture", bad_half_rest, "stale raw-field ceiling")

    bad_nul = check_tar(GOOD_TAR.replace("length - 1)", "length)"))
    expect_trip("no-NUL-reserve fixture", bad_nul, "putText must keep")

    bad_false = check_handler(BAD_HANDLER_FALSE)
    expect_trip("denied-with-false fixture (ISSUE-03b)", bad_false, "failed=true")
    bad_single = check_handler(
        GOOD_HANDLER.replace(
            "        return finishTar(deniedEntry(destRaw, destResolution), true, startedNs, args)\n", ""
        )
    )
    expect_trip("single-site fixture", bad_single, "expected >= 2 finishTar")

    # ── [dv14-A2] writeRegular ──────────────────────────────────────────────
    good_regular = check_write_regular(GOOD_REGULAR)
    expect_clean("compliant writeRegular fixture", good_regular)
    bad_regular_order = check_write_regular(BAD_REGULAR_OLD_ORDER)
    expect_trip(
        "header-before-open fixture (dv14-A2)",
        bad_regular_order,
        "BEFORE opening the input stream",
    )
    bad_regular_norefill = check_write_regular(BAD_REGULAR_NO_REFILL)
    expect_trip(
        "copy-failure-without-refill fixture (dv14-A2)",
        bad_regular_norefill,
        "zero-fill the short payload",
    )
    bad_regular_unguarded = check_write_regular(BAD_REGULAR_UNGUARDED)
    expect_trip(
        "unguarded-refill fixture (dv14-A2)",
        bad_regular_unguarded,
        "must be guarded",
    )
    bad_regular_silent = check_write_regular(BAD_REGULAR_SILENT)
    expect_trip(
        "silent-truncation fixture (dv14-A2)",
        bad_regular_silent,
        "must still record out.errors",
    )
    bad_regular_missing = check_write_regular("object FastioTar { }")
    expect_trip(
        "missing-writeRegular fixture (dv14-A2)",
        bad_regular_missing,
        "required function missing",
    )

    print("selftest compliant tar          -> %d violation(s)" % len(good_tar))
    print("selftest compliant handler      -> %d violation(s)" % len(good_handler))
    print("selftest stale ceiling         -> %d violation(s)" % len(bad_stale))
    print("selftest half-fix prefix/rest   -> %d / %d violation(s)"
          % (len(bad_half_prefix), len(bad_half_rest)))
    print("selftest no NUL reserve        -> %d violation(s)" % len(bad_nul))
    print("selftest denied-with-false      -> %d violation(s)" % len(bad_false))
    print("selftest single-site handler    -> %d violation(s)" % len(bad_single))
    print("selftest compliant writeRegular -> %d violation(s)" % len(good_regular))
    print("selftest writeRegular shapes    -> %d / %d / %d / %d / %d violation(s)"
          % (len(bad_regular_order), len(bad_regular_norefill), len(bad_regular_unguarded),
             len(bad_regular_silent), len(bad_regular_missing)))

    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    print("FASTIO_TAR_BOUNDS_SELFTEST=PASS")
    return 0


def main():
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        return selftest()
    root = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(root)


if __name__ == "__main__":
    sys.exit(main())

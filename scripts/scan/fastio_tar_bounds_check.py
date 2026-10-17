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


def run(root):
    violations = []
    tar_path = os.path.join(root, TAR_REL)
    handler_path = os.path.join(root, HANDLER_REL)
    if not os.path.isfile(tar_path):
        return 2
    with open(tar_path, encoding="utf-8") as f:
        violations += check_tar(f.read())
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

    print("selftest compliant tar          -> %d violation(s)" % len(good_tar))
    print("selftest compliant handler      -> %d violation(s)" % len(good_handler))
    print("selftest stale ceiling         -> %d violation(s)" % len(bad_stale))
    print("selftest half-fix prefix/rest   -> %d / %d violation(s)"
          % (len(bad_half_prefix), len(bad_half_rest)))
    print("selftest no NUL reserve        -> %d violation(s)" % len(bad_nul))
    print("selftest denied-with-false      -> %d violation(s)" % len(bad_false))
    print("selftest single-site handler    -> %d violation(s)" % len(bad_single))

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

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


# ── [dv15-A2] A-04: the archive FILE itself must not be followed ─────────────
#
# GuestPathMapper leaves the LAST segment of a guest path unresolved on purpose
# (that is what keeps `rm <link>` working), so the archive path is a host path
# whose final component may be a symbolic link to ANY host file. Two opens make
# that reachable: Files.newOutputStream(archivePath) in tarCreateInto (write
# through the link — `--force` is what let it through) and the input stream the
# extract side reads with (read through the link). Both cross the guest/host
# boundary this handler exists to enforce.
#
# The decision point is "before the archive file is opened", and TWO shapes
# satisfy it: an explicit Files.isSymbolicLink(...) refusal, or an open that asks
# for LinkOption.NOFOLLOW_LINKS. Both are accepted (the check binds the decision,
# not a spelling) and it is required on BOTH paths — a guard on one half only is
# the pre-fix escape, half-closed. The explicit-refusal shape must also carry a
# structured payload, otherwise the link is noticed and then followed anyway.
#
# member_slice note: tar_fun_body() above cuts at "\nprivate fun ", which a class
# member (indented) never matches, so it returns the rest of the file. Slicing on
# the declaration's own indent keeps the scope tight — and scope is the point
# here, because "a guard exists somewhere in the file" is not the property under
# test.
ARCHIVE_LINK_GUARD = "Files.isSymbolicLink("
NOFOLLOW_OPEN_RE = re.compile(r"new(?:Input|Output)Stream\([^)\n]*NOFOLLOW_LINKS")
ARCHIVE_GUARD_PAYLOAD = "unsafe_archive_path"
DECL_KEYWORDS = ("private ", "internal ", "public ", "protected ", "override ", "fun ",
                 "class ", "object ", "}")


def handler_member(src, name):
    start = src.find("fun %s(" % name)
    if start < 0:
        return None
    line_start = src.rfind("\n", 0, start) + 1
    indent = len(src[line_start:start]) - len(src[line_start:start].lstrip(" "))
    lines = src[line_start:].split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        stripped = line.strip()
        if stripped:
            ind = len(line) - len(line.lstrip(" "))
            if ind <= indent and stripped.startswith(DECL_KEYWORDS):
                break
        out.append(line)
    return "\n".join(out)


def archive_open_guard_shape(src, name, extra_opener=None):
    """'refuse' / 'nofollow' / None for the member that opens the archive.

    extra_opener lets the read side put its NOFOLLOW open in the helper that
    actually calls newInputStream (openArchive) instead of in the member — that
    is the same decision made one call deeper, so it counts.
    """
    body = handler_member(src, name)
    if body is None:
        return "missing"
    if ARCHIVE_LINK_GUARD in body:
        return "refuse"
    if NOFOLLOW_OPEN_RE.search(body):
        return "nofollow"
    if extra_opener:
        opener = handler_member(src, extra_opener)
        if opener is not None and NOFOLLOW_OPEN_RE.search(opener):
            return "nofollow"
    return None


def check_archive_open_guard(src):
    v = []
    shapes = {}
    for name, extra in (("tarCreateInto", None), ("tarExtractFrom", "openArchive")):
        shape = archive_open_guard_shape(src, name, extra_opener=extra)
        if shape == "missing":
            v.append("%s must exist in FastioOffloadHandler.kt (required function missing)" % name)
            continue
        shapes[name] = shape
        if shape is None:
            v.append(
                "%s opens the archive path without a NOFOLLOW-level guard — a symbolic link at the"
                " archive path would be followed (write-through on create, read-through on extract)"
                % name
            )
    if "refuse" in shapes.values() and ARCHIVE_GUARD_PAYLOAD not in src:
        v.append(
            "a symlink at the archive path is detected but not refused — no %s payload"
            % ARCHIVE_GUARD_PAYLOAD
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


# ── [dv16-A FLAKE] the create traversal must emit members in a decided order ─
#
# `FastioTarTest > a relative argument produces relative member names…` went red
# in EIGHT separate CI runs on nothing but member ORDER: `Files.walkFileTree`
# hands the entries over in the directory stream's own order (ext4/f2fs hash
# order), so the same tree produced different archive bytes between runs. The
# decided fix (user, 2026-10-20) is production-side: list each level, sort it,
# then visit in order — one level at a time, so the streaming contract survives
# (header before its members; no whole-tree buffer against
# DEFAULT_MAX_ENTRIES = 2,000,000).
#
# Two decision points are pinned, both bound to the DECISION and not a spelling:
#   (a) the create side does not walk with Files.walkFileTree;
#   (b) the traversal orders the entries of each level before visiting them
#       (`sortedBy` / `sorted()` / `sortedWith` / `sortedByDescending` … all
#       count — what must exist is "a sort applied to the listing").
# Scoped to the create members only (tarCreate + writeTree): FastioTar.kt also
# has an extract side, and extract order is the archive's own, not ours.
CREATE_WALK = "Files.walkFileTree("
LISTING_RE = re.compile(r"newDirectoryStream\(|Files\.list\(")
# Bound to the DECISION (a sort is applied to the listing), not to a spelling:
# `sortedBy { … }` (trailing lambda) and `sorted()` / `sortedWith(cmp)` all count.
SORT_RE = re.compile(r"\.sorted[A-Za-z]*\s*[({]")


def check_create_traversal(src):
    v = []
    scope_parts = []
    for name in ("tarCreate", "writeTree"):
        body = tar_fun_body(src, name)
        if body is None:
            v.append("the create traversal cannot be sliced (%s is gone) — re-point this rule "
                     "before trusting it" % name)
            return v
        scope_parts.append(body)
    if CREATE_WALK in "\n".join(scope_parts):
        v.append("the create traversal walks with Files.walkFileTree — member order is then "
                  "whatever the directory stream hands over, so the same tree archives differently "
                  "between runs (the FastioTarTest order flake)")
    tree = tar_fun_body(src, "writeTree")
    if not LISTING_RE.search(tree):
        v.append("writeTree no longer lists the directory itself — re-read this rule before "
                  "trusting the order criterion")
        return v
    if not SORT_RE.search(tree):
        v.append("writeTree never orders the entries of a level — the archive member order is "
                  "undecided again")
    return v


# ── [dv22-A2] §1042-A-02: the top-level root loop is a walk like any other ───
#
# `writeTree` has always charged the entry budget, but the loop around it did
# not, in three ways that only bite when a ROOT is not a directory:
#   1. no ceiling — `writeRegular` / `writeLink` on a top-level root bumped
#      `visited` past `maxEntries` and nothing ever set `truncated`;
#   2. `writeTree`'s `false` ("budget exhausted, stop the WHOLE traversal")
#      was discarded, so roots after an exhausted tree were still archived;
#   3. a top-level FIFO/socket went straight into `writeRegular`, whose
#      `Files.newInputStream` blocks forever on a pipe with no writer — the
#      worker thread is dead for the rest of the request.
#
# Three decision points are pinned, each against the DECISION:
#   (a) a budget comparison on `out.visited` exists in the loop and fires
#       BEFORE that iteration's first write call, setting `truncated` and
#       `break`ing;
#   (b) the `writeTree` return value is consumed with its polarity
#       (`if (!writeTree(…)`) — a bare statement is the pre-fix shape;
#   (c) the top-level non-directory write is gated on `Files.isRegularFile`
#       and the non-regular residue is counted (`skippedSpecial++`), matching
#       the `writeTree` child branch and tarCreate's own KDoc.
#
# The gate must NOT charge: `writeRegular` / `writeLink` / `writeTree` each do
# their own `visited++`, so a `charge(` here would count one entry twice and
# stop the walk at half the budget. `charge(` inside tarCreate is therefore a
# violation, not an accepted spelling — that asymmetry (charge here, charge
# there) is exactly what produces an off-by-N budget nobody can reason about.
TOPLEVEL_GATE_BLOCK_RE = re.compile(
    r"if\s*\([^)\n]*\.visited\s*>=\s*maxEntries\s*\)\s*\{(.*?)\}", re.DOTALL
)
TOPLEVEL_WRITE_CALLS = ("writeLink(", "writeRegular(", "writeTree(")
TOPLEVEL_TREE_CONSUMED_RE = re.compile(r"!\s*writeTree\(|writeTree\([^;\n]*?\)\s*==\s*false")
TOPLEVEL_REGULAR_GATE = "Files.isRegularFile("
TOPLEVEL_SKIP_COUNT = "skippedSpecial++"

def strip_kotlin_comments(text):
    """Blank out // and /* */ comments, char-for-char (newlines kept), so a
    criterion cannot be tripped — or hidden — by prose. Applied by the
    top-level check only: its pre-fix shape (§1042-A-02) was first reported by
    a comment that SAID `charge()` while the real call lived in writeTree."""
    out = list(text)
    i, n, in_block = 0, len(text), False
    while i < n:
        if in_block:
            if text[i : i + 2] == "*/":
                out[i] = out[i + 1] = " "
                i += 2
                in_block = False
            else:
                if text[i] != "\n":
                    out[i] = " "
                i += 1
        elif text[i : i + 2] == "/*":
            in_block = True
            out[i] = out[i + 1] = " "
            i += 2
        elif text[i : i + 2] == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)

def check_toplevel_roots(src):
    v = []
    raw = tar_fun_body(src, "tarCreate")
    if raw is None:
        return ["tarCreate must exist in FastioTar.kt (required function missing)"]
    body = strip_kotlin_comments(raw)

    # (a) the budget gate: exists, stops the walk, and precedes the writes.
    gate = TOPLEVEL_GATE_BLOCK_RE.search(body)
    if gate is None:
        v.append(
            "tarCreate's root loop has no entry-budget gate — a top-level root (file or "
            "symlink) is written past maxEntries and truncated is never set"
        )
    else:
        block = gate.group(1)
        if "truncated = true" not in block:
            v.append("the root-loop budget gate must set out.truncated (a silent over-budget archive)")
        if not re.search(r"\bbreak\b", block):
            v.append(
                "the root-loop budget gate must break the loop — continuing keeps writing the "
                "remaining roots after the budget is gone"
            )
        first_write = min((body.find(w) for w in TOPLEVEL_WRITE_CALLS if body.find(w) >= 0),
                          default=-1)
        if 0 <= first_write < gate.start():
            v.append(
                "the root-loop budget gate sits AFTER the first write call — the root that "
                "exhausts the budget is still archived"
            )
    # The gate compares; the writers charge. Charging both double-counts.
    if re.search(r"\bcharge\(", body):
        v.append(
            "tarCreate charges the budget itself — writeRegular/writeLink/writeTree already "
            "do `visited++`, so the walk stops at half maxEntries (and the gate must only compare)"
        )

    # (b) writeTree's false means "stop the whole traversal".
    tree_calls = [m for m in re.finditer(r"writeTree\(", body)]
    if not tree_calls:
        v.append("tarCreate no longer calls writeTree — re-point this rule before trusting it")
    for m in tree_calls:
        pre = body[max(0, m.start() - 8):m.start()]
        consumed = re.search(r"!\s*$", pre) is not None
        if not consumed:
            consumed = TOPLEVEL_TREE_CONSUMED_RE.search(body, m.start() - 1) is not None
        if not consumed:
            v.append(
                "tarCreate discards writeTree's return value — `false` means the budget ran out "
                "and the remaining roots must NOT be archived"
            )
            break

    # (c) a top-level non-directory is only read when it IS a regular file.
    regular_at = body.find(TOPLEVEL_REGULAR_GATE)
    write_regular_at = body.find("writeRegular(")
    if regular_at < 0:
        v.append(
            "tarCreate writes a top-level non-directory without checking Files.isRegularFile — "
            "a FIFO has no writer, so newInputStream blocks the worker thread forever"
        )
    elif write_regular_at >= 0 and regular_at > write_regular_at:
        v.append("the isRegularFile gate sits AFTER writeRegular — it cannot prevent the open")
    if TOPLEVEL_SKIP_COUNT not in body:
        v.append(
            "a top-level special file (FIFO/socket/device) is not counted in skippedSpecial — "
            "the archive silently lacks it, against tarCreate's own KDoc"
        )
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
    violations += check_create_traversal(tar_src)
    violations += check_toplevel_roots(tar_src)
    if not os.path.isfile(handler_path):
        return 2
    with open(handler_path, encoding="utf-8") as f:
        handler_src = f.read()
    violations += check_handler(handler_src)
    violations += check_archive_open_guard(handler_src)
    search_path = os.path.join(root, SEARCH_REL)
    copy_path = os.path.join(root, COPY_REL)
    if not (os.path.isfile(search_path) and os.path.isfile(copy_path)):
        return 2
    with open(search_path, encoding="utf-8") as f:
        search_src = f.read()
    with open(copy_path, encoding="utf-8") as f:
        copy_src = f.read()
    violations += check_special_file_guards(search_src, copy_src)
    if violations:
        for v in violations:
            print("VIOLATION " + v)
        print("GATE=RED fastio_tar_bounds")
        return 1
    print("GATE=GREEN fastio_tar_bounds")
    return 0


# ── [dv23-A6] §1043-A-07: the engines that OPEN an entry must ask first ──────
#
# `FastioTar`'s create walk learned this in §1042-A-02 (a top-level FIFO went
# straight into writeRegular and blocked a worker thread forever). The two
# sibling engines were never gated, and `find` mislabelled the same entries:
#   (a) grepTree.visitFile opens the entry (grepFile → Files.newInputStream)
#       without asking whether it is a regular file;
#   (b) copyTree.visitFile copies it (Files.copy) the same way;
#   (c) findTree.visitFile types every entry that is not a link as `f`, while
#       the help text says `f` means "regular file" — so `find -type f` listed
#       pipes and sockets the guest cannot open.
# Measured on this JVM (probe, 2026-10-22): `Files.newInputStream` and
# `Files.copy` on a WRITER-LESS FIFO never return — open(2) blocks with no
# timeout — while a unix socket raises ENXIO. Two hits exhaust the 2-thread
# offload pool and every other tool call stalls.
#
# Each criterion binds the DECISION ("is this entry a regular file?") and not a
# spelling: `when { attrs.isRegularFile -> 'f' }`, `if (!attrs.isRegularFile)`,
# `if (attrs.isOther)`, `Files.isRegularFile(path, NOFOLLOW_LINKS)` all count.
# The decision must sit BEFORE the open/copy/consider call in the same
# visitFile block — a guard after it cannot prevent the open.
#
# Scope: each engine is sliced on its own. A guard in `grepTree` must not cover
# for a missing one in `copyTree` or `findTree` (that symmetry failure is the
# whole finding). Comments are stripped before slicing for the same reason
# §1042-A-02 strips them: the fix's own KDoc names isRegularFile, and a gate
# that reads prose would stay green after the guard was deleted.
SEARCH_REL = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/FastioSearch.kt"
COPY_REL = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/FastioCopy.kt"

SPECIAL_GUARD_TOKENS = ("isRegularFile", "isOther")
# What the guard has to precede, per engine (the call that touches the entry).
SPECIAL_OPEN_CALLS = {
    "grepTree": ("grepFile(", "Files.newInputStream"),
    "findTree": ("consider(",),
    "copyTree": ("Files.copy(",),
}
# Where the answer has to sit, relative to that call:
#   "before"      - its own statement ahead of an open/copy (grep, cp);
#   "in-argument" - the TYPE handed to `consider(`, because find never opens
#                   anything: its whole defect is the label it passes on.
SPECIAL_GUARD_POSITION = {"grepTree": "before", "findTree": "in-argument", "copyTree": "before"}


def kotlin_top_member(src, name):
    """Slice a COLUMN-0 declaration (`fun name(`) up to the next column-0 line.

    The repo keeps every top-level declaration at indent 0 and every body line
    indented, so indentation is what delimits the member — the same shape the
    handler's `handler_member` relies on, without its indent-vs-keyword dance.
    """
    start = src.find("fun %s(" % name)
    if start < 0:
        return None
    line_start = src.rfind("\n", 0, start) + 1
    lines = src[line_start:].split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        # A multi-line signature closes with `) {` at column 0 — that is still
        # part of the member, not a new declaration.
        if line and not line[0].isspace() and not line.startswith(")"):
            break
        out.append(line)
    return "\n".join(out)


def visit_file_block(src, engine):
    """The `override fun visitFile(` block of [engine], or None if unsliceable."""
    body = kotlin_top_member(src, engine)
    if body is None:
        return None
    start = body.find("override fun visitFile(")
    if start < 0:
        return None
    nxt = body.find("override fun ", start + len("override fun visitFile("))
    return body[start:] if nxt < 0 else body[start:nxt]


def check_special_file_guards(search_src, copy_src):
    v = []
    engines = (
        ("FastioSearch.kt", search_src, "grepTree",
         "grepTree opens every non-link entry"),
        ("FastioSearch.kt", search_src, "findTree",
         "findTree types every non-link entry as a regular file"),
        ("FastioCopy.kt", copy_src, "copyTree",
         "copyTree copies every non-link entry"),
    )
    for fname, raw, engine, why in engines:
        block = visit_file_block(strip_kotlin_comments(raw), engine)
        if block is None:
            v.append("%s: %s's visitFile cannot be sliced - re-point this rule before "
                     "trusting it" % (fname, engine))
            continue
        calls = SPECIAL_OPEN_CALLS[engine]
        open_at = min((block.find(t) for t in calls if block.find(t) >= 0), default=-1)
        if open_at < 0:
            v.append("%s: %s no longer calls %s - re-point this rule before trusting it"
                     % (fname, engine, " / ".join(calls)))
            continue
        mode = SPECIAL_GUARD_POSITION[engine]
        anywhere = min((block.find(t) for t in SPECIAL_GUARD_TOKENS if block.find(t) >= 0),
                       default=-1)
        scope = block[:open_at] if mode == "before" else block[open_at:]
        guard_at = min((scope.find(t) for t in SPECIAL_GUARD_TOKENS if scope.find(t) >= 0),
                       default=-1)
        if guard_at < 0 and mode == "before" and anywhere >= 0:
            v.append("%s: %s: the regular-file test sits AFTER the call that touches the entry - "
                     "it cannot prevent the open" % (fname, engine))
        elif guard_at < 0:
            tail = (
                "WITHOUT asking whether it is a regular file - and the help text defines `f` as "
                "a regular file, so `-type f` lists pipes and sockets the guest cannot open"
                if mode == "in-argument" else
                "WITHOUT asking whether it is a regular file - a FIFO with no writer blocks "
                "open(2) forever and kills an offload worker thread"
            )
            v.append("%s: %s: %s %s" % (fname, engine, why, tail))
    return v


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

private fun tarCreateInto(entry: JSONObject, archivePath: Path): NativeOffloadResult {
    if (Files.isSymbolicLink(archivePath)) {
        return refuseLinkedArchive(entry, "write", startedNs, args)
    }
    val raw = Files.newOutputStream(archivePath)
    return finishTar(entry, outcome.errors.isNotEmpty(), startedNs, args)
}

private fun tarExtractFrom(entry: JSONObject, archivePath: Path): NativeOffloadResult {
    val destResolution = GuestPathMapper.resolve(destRaw, request.cwd, bindings)
    if (destResolution is GuestPathMapper.Resolution.Denied) {
        return finishTar(deniedEntry(destRaw, destResolution), true, startedNs, args)
    }
    if (Files.isSymbolicLink(archivePath)) {
        return refuseLinkedArchive(entry, "read", startedNs, args)
    }
    return finishTar(entry, outcome.errors.isNotEmpty(), startedNs, args)
}

private fun refuseLinkedArchive(entry: JSONObject, verb: String, startedNs: Long, args: OffloadArgs): NativeOffloadResult =
    finishTarWith(entry.put("error", "unsafe_archive_path"), 2, startedNs, args)
"""

# [dv15-A2] A-04, the OTHER accepted shape: no explicit isSymbolicLink() check —
# the opens themselves ask for NOFOLLOW_LINKS, and the read side's open lives one
# call deeper (openArchive). If this fixture went red, the criterion would be
# pinning a spelling instead of the decision.
GOOD_HANDLER_NOFOLLOW = """
private fun tarCreateInto(entry: JSONObject, archivePath: Path): NativeOffloadResult {
    val raw = Files.newOutputStream(archivePath, LinkOption.NOFOLLOW_LINKS)
    return finishTar(entry, outcome.errors.isNotEmpty(), startedNs, args)
}

private fun tarExtractFrom(entry: JSONObject, archivePath: Path): NativeOffloadResult {
    return openArchive(archivePath, gzip).use { tarExtract(it, destPath, force, budget, outcome) }
}

private fun openArchive(path: Path, gzip: Boolean): java.io.InputStream {
    val raw = Files.newInputStream(path, LinkOption.NOFOLLOW_LINKS)
    return if (gzip) GZIPInputStream(raw) else raw
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


# ── [dv16-A FLAKE] create-side ordering fixtures ────────────────────────────
GOOD_TREE = """
internal fun tarCreate(
    sink: OutputStream,
    roots: List<Pair<Path, String>>,
    maxEntries: Long,
    out: TarCreateOutcome,
) {
    for ((path, memberPrefix) in roots) {
        writeTree(sink, path, tarMemberName(memberPrefix), path, maxEntries, out)
    }
}

private fun writeTree(
    sink: OutputStream,
    dir: Path,
    rootName: String,
    root: Path,
    maxEntries: Long,
    out: TarCreateOutcome,
): Boolean {
    val children = Files.newDirectoryStream(dir).use { stream ->
        stream.toList().sortedBy { it.fileName.toString() }
    }
    for (child in children) {
        if (!writeTree(sink, child, rootName, root, maxEntries, out)) return false
    }
    return true
}
"""

# the pre-fix shape: the walker decides the order (directory hash order).
BAD_TREE_WALK = GOOD_TREE.replace(
    "    val children = Files.newDirectoryStream(dir).use { stream ->\n"
    "        stream.toList().sortedBy { it.fileName.toString() }\n"
    "    }",
    "    val children = ArrayList<Path>()\n"
    "    Files.walkFileTree(dir, object : SimpleFileVisitor<Path>() {})",
)

# listed, but never ordered: the members come out in stream order again.
BAD_TREE_UNSORTED = GOOD_TREE.replace(
    "        stream.toList().sortedBy { it.fileName.toString() }",
    "        stream.toList()",
)

# marker gone: the rule must fail closed, not silently pass.
BAD_TREE_RENAMED = GOOD_TREE.replace("private fun writeTree(", "private fun walkDir(")

# ── [dv22-A2] §1042-A-02: top-level root loop fixtures ──────────────────────
GATE_BLOCK = """        if (out.visited >= maxEntries) {
            out.truncated = true
            break
        }
"""

GOOD_TOPLEVEL = """
internal fun tarCreate(
    sink: OutputStream,
    roots: List<Pair<Path, String>>,
    maxEntries: Long,
    out: TarCreateOutcome,
) {
    for ((path, memberPrefix) in roots) {
        val name = tarMemberName(memberPrefix)
        if (out.visited >= maxEntries) {
            out.truncated = true
            break
        }
        if (!Files.exists(path, LinkOption.NOFOLLOW_LINKS)) {
            out.errors.add("$memberPrefix: not found")
            continue
        }
        if (Files.isSymbolicLink(path)) {
            writeLink(sink, name, path, out)
            continue
        }
        if (!Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS)) {
            if (!Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)) {
                out.skippedSpecial++
                if (out.skippedNames.size < MAX_REPORTED_SKIPS) out.skippedNames.add(name)
                continue
            }
            writeRegular(sink, name, path, out)
            continue
        }
        try {
            if (!writeTree(sink, path, name, path, maxEntries, out)) break
        } catch (e: Exception) {
            out.errors.add("$memberPrefix: ${e.message}")
        }
    }
    sink.write(ByteArray(BLOCK_SIZE * 2))
    sink.flush()
}

private fun writeTree(sink: OutputStream, dir: Path): Boolean = true
"""

# pre-fix shape (§1042-A-02): no gate at all — roots are written past the budget.
BAD_TOPLEVEL_NO_GATE = GOOD_TOPLEVEL.replace(GATE_BLOCK, "")

# half-fix: the gate exists but reports nothing.
BAD_TOPLEVEL_NO_TRUNCATED = GOOD_TOPLEVEL.replace(
    "            out.truncated = true\n            break", "            break"
)

# half-fix: the gate keeps SCANNING instead of stopping — later roots still write.
BAD_TOPLEVEL_CONTINUE = GOOD_TOPLEVEL.replace(
    "            out.truncated = true\n            break", "            out.truncated = true\n            continue"
)

# half-fix: the gate lives after the first writer, so the budget-exhausting
# root itself is still archived.
BAD_TOPLEVEL_GATE_AFTER_WRITE = GOOD_TOPLEVEL.replace(GATE_BLOCK, "").replace(
    "            writeLink(sink, name, path, out)\n            continue",
    "            writeLink(sink, name, path, out)\n            continue\n"
    "        if (out.visited >= maxEntries) {\n"
    "            out.truncated = true\n"
    "            break\n"
    "        }",
)

# double-charge shape: the gate charges, the writer charges again.
BAD_TOPLEVEL_CHARGES = GOOD_TOPLEVEL.replace(
    GATE_BLOCK, "        if (charge(out, maxEntries)) break\n"
)

# pre-fix shape: writeTree's budget-exhausted `false` is discarded.
BAD_TOPLEVEL_IGNORED_RETURN = GOOD_TOPLEVEL.replace(
    "            if (!writeTree(sink, path, name, path, maxEntries, out)) break",
    "            writeTree(sink, path, name, path, maxEntries, out)",
)

# pre-fix shape: a top-level FIFO goes straight into writeRegular.
BAD_TOPLEVEL_UNCHECKED_SPECIAL = GOOD_TOPLEVEL.replace(
    """            if (!Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)) {
                out.skippedSpecial++
                if (out.skippedNames.size < MAX_REPORTED_SKIPS) out.skippedNames.add(name)
                continue
            }
""", "")


# ── [dv23-A6] §1043-A-07 fixtures ───────────────────────────────────────────
GOOD_SEARCH_FIX = """
internal fun findTree(
    root: Path,
    out: FindOutcome,
) {
    try {
        Files.walkFileTree(
            root,
            object : SimpleFileVisitor<Path>() {
                override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                    consider(dir, 'd', 0L)
                    return FileVisitResult.CONTINUE
                }

                override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                    out.files++
                    consider(
                        file,
                        when {
                            attrs.isSymbolicLink -> 'l'
                            attrs.isRegularFile -> 'f'
                            else -> 'o'
                        },
                        attrs.size(),
                    )
                    return FileVisitResult.CONTINUE
                }

                override fun visitFileFailed(file: Path, exc: IOException): FileVisitResult {
                    return FileVisitResult.CONTINUE
                }
            },
        )
    } catch (e: Exception) {
        out.errors.add("$root: ${e.message}")
    }
}

internal fun grepTree(
    root: Path,
    out: GrepOutcome,
) {
    try {
        Files.walkFileTree(
            root,
            object : SimpleFileVisitor<Path>() {
                override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (attrs.isSymbolicLink) {
                        out.filesSkippedLink++
                        return FileVisitResult.CONTINUE
                    }
                    if (!attrs.isRegularFile) {
                        out.filesSkippedSpecial++
                        return FileVisitResult.CONTINUE
                    }
                    grepFile(file, guestPathFor(root, "/ws", file), regex, limit, out)
                    return FileVisitResult.CONTINUE
                }

                override fun visitFileFailed(file: Path, exc: IOException): FileVisitResult {
                    return FileVisitResult.CONTINUE
                }
            },
        )
    } catch (e: Exception) {
        out.errors.add("$root: ${e.message}")
    }
}
"""

GOOD_COPY_FIX = """
internal fun copyTree(src: Path, dst: Path, force: Boolean, out: CopyOutcome) {
    try {
        Files.walkFileTree(
            src,
            object : SimpleFileVisitor<Path>() {
                override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                    val target = targetFor(file)
                    try {
                        if (!attrs.isRegularFile && !attrs.isSymbolicLink) {
                            out.errors.add("$file: special file, not copied")
                            return FileVisitResult.CONTINUE
                        }
                        if (attrs.isSymbolicLink) {
                            Files.createSymbolicLink(target, Files.readSymbolicLink(file))
                        } else {
                            Files.copy(file, target)
                        }
                        out.files++
                    } catch (e: Exception) {
                        out.errors.add("$target: ${e.message}")
                    }
                    return FileVisitResult.CONTINUE
                }

                override fun visitFileFailed(file: Path, exc: IOException): FileVisitResult {
                    out.errors.add("$file: ${exc.message}")
                    return FileVisitResult.CONTINUE
                }
            },
        )
    } catch (e: Exception) {
        out.errors.add("$src: ${e.message}")
    }
}
"""

GREP_GUARD = """                    if (!attrs.isRegularFile) {
                        out.filesSkippedSpecial++
                        return FileVisitResult.CONTINUE
                    }
"""
COPY_GUARD = """                        if (!attrs.isRegularFile && !attrs.isSymbolicLink) {
                            out.errors.add("$file: special file, not copied")
                            return FileVisitResult.CONTINUE
                        }
"""
FIND_WHEN = """                        when {
                            attrs.isSymbolicLink -> 'l'
                            attrs.isRegularFile -> 'f'
                            else -> 'o'
                        },
"""

# (a) grep: the guard is gone — the engine opens whatever the walk hands it.
BAD_SEARCH_NO_GREP_GUARD = GOOD_SEARCH_FIX.replace(GREP_GUARD, "")
# (b) find: back to `if (isSymbolicLink) 'l' else 'f'`.
BAD_SEARCH_FIND_LABEL = GOOD_SEARCH_FIX.replace(
    FIND_WHEN, "                        if (attrs.isSymbolicLink) 'l' else 'f',\n"
)
# (c) cp: the guard is gone — Files.copy opens the pipe.
BAD_COPY_NO_GUARD = GOOD_COPY_FIX.replace(COPY_GUARD, "")
# (d) the guard exists but AFTER the open: it cannot prevent anything.
BAD_SEARCH_GUARD_AFTER_OPEN = GOOD_SEARCH_FIX.replace(
    GREP_GUARD + "                    grepFile(",
    "                    grepFile(",
).replace(
    "grepFile(file, guestPathFor(root, \"/ws\", file), regex, limit, out)\n",
    "grepFile(file, guestPathFor(root, \"/ws\", file), regex, limit, out)\n" + GREP_GUARD,
)
# (e) prose only: the guard is replaced by a COMMENT that names it (the reason
#     the criterion strips comments — §1042-A-02's own scar).
BAD_SEARCH_COMMENT_ONLY = GOOD_SEARCH_FIX.replace(
    GREP_GUARD,
    "                    // a special file is not a regular file (attrs.isRegularFile)\n",
)
# (f) marker gone: the rule must fail closed, not silently pass.
BAD_SEARCH_RENAMED = GOOD_SEARCH_FIX.replace("fun grepTree(", "fun walkGrep(")

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

    # ── [dv15-A2] A-04: the archive opens must not follow a link ────────────
    good_guard = check_archive_open_guard(GOOD_HANDLER)
    expect_clean("compliant archive-open guard fixture", good_guard)

    # the OTHER accepted shape must stay clean: no explicit check, the opens ask
    # for NOFOLLOW (and the read side's open sits one call deeper). If this went
    # red the criterion would be pinning a spelling, not the decision.
    expect_clean("NOFOLLOW-open shape fixture (dv15-A2)",
                 check_archive_open_guard(GOOD_HANDLER_NOFOLLOW))

    bad_guard_create = check_archive_open_guard(
        GOOD_HANDLER.replace(
            """    if (Files.isSymbolicLink(archivePath)) {
        return refuseLinkedArchive(entry, "write", startedNs, args)
    }
""", "")
    )
    expect_trip("create-side guard removed (dv15-A2)", bad_guard_create,
                "tarCreateInto opens the archive path")

    bad_guard_extract = check_archive_open_guard(
        GOOD_HANDLER.replace(
            """    if (Files.isSymbolicLink(archivePath)) {
        return refuseLinkedArchive(entry, "read", startedNs, args)
    }
""", "")
    )
    expect_trip("extract-side guard removed (dv15-A2)", bad_guard_extract,
                "tarExtractFrom opens the archive path")

    bad_guard_payload = check_archive_open_guard(
        GOOD_HANDLER.replace('"unsafe_archive_path"', '"unsafe_path"')
    )
    expect_trip("guard-without-payload fixture (dv15-A2)", bad_guard_payload,
                "detected but not refused")

    bad_guard_missing = check_archive_open_guard("object FastioOffloadHandler { }")
    expect_trip("missing archive-open members (dv15-A2)", bad_guard_missing,
                "required function missing")

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

    # ── [dv16-A FLAKE] the create traversal must decide the member order ─────
    good_tree = check_create_traversal(GOOD_TREE)
    expect_clean("compliant create traversal", good_tree)
    bad_tree_walk = check_create_traversal(BAD_TREE_WALK)
    expect_trip("walker-order fixture (FLAKE)", bad_tree_walk, "walks with Files.walkFileTree")
    bad_tree_unsorted = check_create_traversal(BAD_TREE_UNSORTED)
    expect_trip("unsorted fixture (FLAKE)", bad_tree_unsorted, "never orders the entries")
    bad_tree_renamed = check_create_traversal(BAD_TREE_RENAMED)
    expect_trip("renamed-traversal fixture (FLAKE)", bad_tree_renamed, "cannot be sliced")
    # the order criterion must not fire on a file that merely MENTIONS the walker
    # (FastioTar.kt documents the replacement in a KDoc right above writeTree):
    expect_clean("walker mentioned in a doc comment",
                 check_create_traversal(GOOD_TREE.replace(
                     "private fun writeTree(",
                     "* Replaces `Files.walkFileTree`, which yields the entries in\n"
                     " * whatever order the directory stream hands them over.\n"
                     " */\nprivate fun writeTree(")))

    # ── [dv22-A2] §1042-A-02: the top-level root loop ───────────────────────
    good_top = check_toplevel_roots(GOOD_TOPLEVEL)
    expect_clean("compliant top-level fixture (dv22-A2)", good_top)
    bad_top_no_gate = check_toplevel_roots(BAD_TOPLEVEL_NO_GATE)
    expect_trip("no-gate fixture (dv22-A2)", bad_top_no_gate, "no entry-budget gate")
    bad_top_no_trunc = check_toplevel_roots(BAD_TOPLEVEL_NO_TRUNCATED)
    expect_trip("silent gate fixture (dv22-A2)", bad_top_no_trunc, "must set out.truncated")
    bad_top_continue = check_toplevel_roots(BAD_TOPLEVEL_CONTINUE)
    expect_trip("scan-instead-of-stop fixture (dv22-A2)", bad_top_continue, "must break the loop")
    bad_top_after_write = check_toplevel_roots(BAD_TOPLEVEL_GATE_AFTER_WRITE)
    expect_trip("gate-after-write fixture (dv22-A2)", bad_top_after_write, "AFTER the first write")
    bad_top_charges = check_toplevel_roots(BAD_TOPLEVEL_CHARGES)
    expect_trip("double-charge fixture (dv22-A2)", bad_top_charges, "charges the budget itself")
    bad_top_ignored = check_toplevel_roots(BAD_TOPLEVEL_IGNORED_RETURN)
    expect_trip("discarded-return fixture (dv22-A2)", bad_top_ignored, "discards writeTree's return")
    bad_top_unchecked = check_toplevel_roots(BAD_TOPLEVEL_UNCHECKED_SPECIAL)
    expect_trip("unchecked-special fixture (dv22-A2)", bad_top_unchecked,
                "without checking Files.isRegularFile")
    bad_top_missing = check_toplevel_roots("object FastioTar { }")
    expect_trip("missing-tarCreate fixture (dv22-A2)", bad_top_missing, "required function missing")

    # ── [dv23-A6] §1043-A-07: the read/open engines ask first ──────────────
    def expect_only(name, v, needle):
        """Exactly one violation, and it is [needle]: the criterion has a tooth
        and the OTHER engines stay clean (a guard in one engine must not cover
        for a missing one in another)."""
        if len(v) != 1 or needle not in v[0]:
            failures.append("%s must trip exactly %r, got %s" % (name, needle, v))

    def expect_all_clean(name, v):
        if v:
            failures.append("%s must be clean, got %s" % (name, v))

    good_engines = check_special_file_guards(GOOD_SEARCH_FIX, GOOD_COPY_FIX)
    expect_all_clean("compliant special-file guards (dv23-A6)", good_engines)
    bad_grep = check_special_file_guards(BAD_SEARCH_NO_GREP_GUARD, GOOD_COPY_FIX)
    expect_only("grep guard removed (dv23-A6)", bad_grep, "grepTree opens every non-link entry")
    bad_find = check_special_file_guards(BAD_SEARCH_FIND_LABEL, GOOD_COPY_FIX)
    expect_only("find label regression (dv23-A6)", bad_find,
                "findTree types every non-link entry")
    bad_cp = check_special_file_guards(GOOD_SEARCH_FIX, BAD_COPY_NO_GUARD)
    expect_only("copy guard removed (dv23-A6)", bad_cp, "copyTree copies every non-link entry")
    bad_late = check_special_file_guards(BAD_SEARCH_GUARD_AFTER_OPEN, GOOD_COPY_FIX)
    expect_only("guard-after-open (dv23-A6)", bad_late, "sits AFTER the call")
    bad_prose = check_special_file_guards(BAD_SEARCH_COMMENT_ONLY, GOOD_COPY_FIX)
    expect_only("comment-only guard (dv23-A6)", bad_prose, "grepTree opens every non-link entry")
    bad_renamed = check_special_file_guards(BAD_SEARCH_RENAMED, GOOD_COPY_FIX)
    expect_only("renamed engine (dv23-A6)", bad_renamed, "cannot be sliced")
    # the OTHER accepted shape (`isOther`) must stay clean, or the criterion
    # would be pinning `isRegularFile` the SPELLING rather than the decision.
    expect_all_clean(
        "isOther-shaped guard (dv23-A6)",
        check_special_file_guards(
            GOOD_SEARCH_FIX.replace("if (!attrs.isRegularFile) {", "if (attrs.isOther) {"),
            GOOD_COPY_FIX,
        ),
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
    print("selftest compliant top-level    -> %d violation(s)" % len(good_top))
    print("selftest special-file guards    -> %d / %d / %d / %d / %d / %d violation(s)"
          % (len(bad_grep), len(bad_find), len(bad_cp), len(bad_late), len(bad_prose),
             len(bad_renamed)))
    print("selftest top-level shapes       -> %d / %d / %d / %d / %d / %d / %d / %d violation(s)"
          % (len(bad_top_no_gate), len(bad_top_no_trunc), len(bad_top_continue),
             len(bad_top_after_write), len(bad_top_charges), len(bad_top_ignored),
             len(bad_top_unchecked), len(bad_top_missing)))

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

#!/usr/bin/env python3
"""
Read-only-mount write guard — every host-file write whose target comes from a
caller-supplied path parameter must consult `isLinuxPathUnderReadOnlyMount`.

Bug class ([deep-audit-1016] FIX-C 2b, live since T219): the guard exists and
`tools/FileWriteTool` / `tools/FileEditTool` call it, but `minis-model-use
--output` wrote through `shapeRawPassthrough` / `writeRunOutput` with no
read-only check at all — so a `--output` pointing into a user's read-only
mounted folder wrote the file anyway, while the same path refused via
`file_write`. Two write sites, one contract, and nothing mechanical looked at
both. (The audit report also claimed `FastioOffloadHandler` already checked;
it does not — it refuses *any* external-mount write, a superset of the
read-only rule, which is why it is exempt below WITH evidence rather than
silently passed.)

Criterion 1 (read-only mounts): for every function that (a) declares a path
parameter from PATH_PARAMS and (b) writes a host file, the function body must
call `isLinuxPathUnderReadOnlyMount(` — unless the file is in EXEMPT with a
reason and an evidence pattern that must still be present (an exemption that
rots is a failure, not a pass).

Criterion 2 (blocking I/O off the caller's thread, [deep-audit-1025 ISSUE-08]):
same "one contract, two write sites, nothing mechanical looked at both" shape,
one layer down. `MountedFoldersStore.probeWritable()` does a synchronous
create+delete probe write and was called straight from the suspend `add()` /
`refreshWritability()` bodies — both reached from Main (`rememberCoroutineScope()`
in MountedFoldersScreen, ON_RESUME, the storage-permission result) — while
`AppLogger.clearLogs()` (writer close + directory scan + unlink of every log
file) was called straight from the delete-all button's onClick. Every call site
of either must now sit inside a `withContext(Dispatchers.IO) { … }` block; see
BLOCKING_IO_CALLS for the table and `check_blocking_io` for what "inside" means.
[deep-audit-1027 ISSUE-05] added the third subject: `RootfsManager.verifyIntegrity()`
(manifest read + seven stats) was called twice from
`RootfsManagementViewModel.repairRootfs`, reached straight from the screen's
`onClick` — once inline, once inside a `viewModelScope.launch` whose
`Main.immediate` body ran it on the click thread anyway.

Usage: python3 scripts/scan/readonly_mount_write_guard.py [repo_root]
       python3 scripts/scan/readonly_mount_write_guard.py --selftest
Exit:  0 = every site guarded (or exempt with evidence), 1 = unguarded site
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile

GUARD = "isLinuxPathUnderReadOnlyMount("

PATH_PARAMS = re.compile(
    r"^(outputPath|linuxPath|guestPath|destPath|dstPath|dest|dst|hostPath|targetPath|archivePath|outPath)$"
)

# A call that puts bytes on the host filesystem.
WRITE_CALL = re.compile(
    r"\.(writeBytes|writeText|appendText|write)\(|newOutputStream\(|createNewFile\("
    r"|FileOutputStream\(|Files\.(copy|write|createDirectories)\("
)

FUN_DECL = re.compile(
    r"^\s*(?:(?:internal|private|public|protected|override|suspend|inline|operator|"
    r"tailrec|open|abstract|final|external)\s+)*fun\s+(\w+)\s*[(<]"
)

# rel path -> (reason, [(evidence file rel or None for "this file", regex, min hits)])
#
# Each entry must earn its keep TWICE: the reason is the argument, and the
# evidence pattern must still be present in the named file — an exemption whose
# justification rotted (the refusal it leans on was deleted) is reported as a
# violation, not silently tolerated. `None` means "this file".
EXEMPT = {
    "sandbox/PRootKernel.kt": (
        "defines the guard itself (`isLinuxPathUnderReadOnlyMount`)",
        [(None, r"fun isLinuxPathUnderReadOnlyMount\(", 1)],
    ),
    "sandbox/offload/FastioOffloadHandler.kt": (
        "refuses any external-mount write (`isExternalMount`) — a superset of the "
        "read-only rule, so the read-only guard is unreachable by construction",
        [(None, r"isExternalMount", 3)],
    ),
    "sandbox/offload/FastioCopy.kt": (
        "tree-copy engine: every caller reaches it through FastioOffloadHandler, which "
        "refuses any external-mount destination before the copy starts",
        [("sandbox/offload/FastioOffloadHandler.kt", r"isExternalMount", 3)],
    ),
    "sandbox/offload/FastioTar.kt": (
        "tar engine: the extract entry point runs after FastioOffloadHandler's "
        "external-mount refusal on the destination",
        [("sandbox/offload/FastioOffloadHandler.kt", r"isExternalMount", 3)],
    ),
    "data/MountedFoldersStore.kt": (
        "`probeWritable` discovers OS-level writability by *attempting* a write; "
        "refusing it would make every mount report non-writable",
        [(None, r"fun probeWritable\(", 1)],
    ),
    "sandbox/offload/PhotosOffloadHandler.kt": (
        "its destinations are app-private (`filesDir/photos-export`), never a "
        "caller-supplied guest path",
        [(None, r"photos-export", 1)],
    ),
}


APP_REL = os.path.join("src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app")

# ── Criterion 2: blocking host I/O that must not run on the caller's thread ──
#
# (anchor file rel, call regex, definition regex, label)
#
# The anchor file is where the blocking work lives / is reached from: if it is
# gone while its package directory survives, the criterion has lost its subject
# and says so instead of quietly checking nothing. The *call* regexes are then
# applied to every Kotlin file under the app package — the blocking call is what
# matters, not which file happens to hold it today.
BLOCKING_IO_CALLS = (
    (
        "data/MountedFoldersStore.kt",
        r"probeWritable\s*\(",
        r"\bfun\s+probeWritable\s*\(",
        "probeWritability (creates + deletes a probe file on the mount)",
    ),
    (
        "ui/settings/LogManagementScreen.kt",
        r"AppLogger\.clearLogs\s*\(",
        r"\bfun\s+clearLogs\s*\(",
        "log purge (closes the writers + unlinks every log file)",
    ),
    (
        "share/ShareReceiverActivity.kt",
        r"importProviderJson\s*\(",
        r"\bfun\s+importProviderJson\s*\(",
        "provider import (importInstanceJSON: ensureConfigLoaded runBlocking + "
        "EncryptedSharedPreferences commit + Room write)",
    ),
    (
        "share/ShareReceiverActivity.kt",
        r"discardStagedAttachments\s*\(",
        r"\bfun\s+discardStagedAttachments\s*\(",
        "staging discard (length() + delete() per staged attachment)",
    ),
    (
        "ui/sandbox/RootfsManagementViewModel.kt",
        r"manager\.verifyIntegrity\s*\(",
        r"\bfun\s+verifyIntegrity\s*\(",
        "rootfs integrity verification (reads .integrity_manifest + stats 7 critical files)",
    ),
)

# Guarded = the call sits inside a brace-balanced `withContext(Dispatchers.IO) {`
# block that encloses it. The non-blocking `withContext(IO) expr` form is
# deliberately NOT recognised: a wrong-shaped wrap must go red rather than pass
# on a technicality (fail-closed). An unbalanced block is a ROT failure, never a
# silent skip — an unparseable file must not read as clean.
IO_BLOCK_RE = re.compile(r"withContext\s*\(\s*Dispatchers\.IO\s*\)\s*\{")


def strip_comments(text, collapse_literals=True):
    """Drop comments, keep every newline (so a finding can cite the real line).

    A character scanner, not a regex. The regex version this replaces ate `//`
    inside string literals (`"https://…"`), which left the quote unterminated and
    then corrupted every brace/quote scan downstream — measured on
    `sandbox/RootfsManager.kt`, whose stripped text lost quote parity and made a
    perfectly balanced `withContext(Dispatchers.IO) { … }` look unbalanced.

    [collapse_literals] replaces a string/char literal with `""` / `''` (and a raw
    string with a newline-preserving placeholder) so nothing inside one counts as
    structure: a brace in a message must not unbalance a block, and a guard call
    quoted in prose must not satisfy criterion 1. `False` keeps literal bodies
    verbatim — that is the view the EXEMPT evidence patterns are matched against,
    where `"photos-export"` must still be found.
    Kotlin nests block comments, so `/*` counts depth.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            if text.startswith('"""', i):
                end = text.find('"""', i + 3)
                end = n if end < 0 else end + 3
                out.append('""' + "\n" * text.count("\n", i, end)
                           if collapse_literals else text[i:end])
                i = end
                continue
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "\n" or text[j] == '"':
                    break
                j += 1
            closed = j < n and text[j] == '"'
            if collapse_literals:
                out.append('""' if closed else " ")
            else:
                out.append(text[i:j + 1] if closed else text[i:])
            i = j + 1 if j < n else n
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "\n" or text[j] == "'":
                    break
                j += 1
            closed = j < n and text[j] == "'"
            if collapse_literals:
                out.append("''" if closed else " ")
            else:
                out.append(text[i:j + 1] if closed else text[i:])
            i = j + 1 if j < n else n
            continue
        if text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("/*", i):
            depth = 0
            while i < n:
                if text.startswith("/*", i):
                    depth += 1
                    i += 2
                    continue
                if text.startswith("*/", i):
                    depth -= 1
                    i += 2
                    if depth == 0:
                        break
                    continue
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def strip_strings(line):
    out, i, quote = [], 0, None
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
        out.append(c)
        i += 1
    return "".join(out)


def param_close(sig_text):
    """Index just past the `)` that closes the first `(`; -1 when unbalanced.

    Needed instead of a non-greedy regex because a parameter list can nest
    parentheses (`(onDone: () -> Unit)`) and span lines.
    """
    start = sig_text.find("(")
    if start < 0:
        return -1
    depth, i = 0, start
    while i < len(sig_text):
        c = sig_text[i]
        if c in "\"'":
            quote = c
            i += 1
            while i < len(sig_text) and sig_text[i] != quote:
                i += 2 if sig_text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def depth_of(text):
    """Net ( and { opened by a chunk of stripped source."""
    return text.count("(") - text.count(")") + text.count("{") - text.count("}")


def functions(text):
    """[(name, params, body)] for every `fun` declared in `text`."""
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        m = FUN_DECL.match(lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1)
        # Signature runs until the parameter list closes.
        paren, j = 0, i
        sig = []
        seen_open = False
        while j < len(lines):
            s = strip_strings(lines[j])
            paren += s.count("(") - s.count(")")
            if "(" in s:
                seen_open = True
            sig.append(lines[j])
            if seen_open and paren <= 0:
                break
            j += 1
        sig_text = "\n".join(sig)
        close = param_close(sig_text)
        params = sig_text[sig_text.find("(") + 1:close - 1] if close > 0 else ""
        # Expression body (`: T = expr`, no `{` before the `=`): the signature
        # scan already followed the expression's parentheses, so the body is the
        # tail of the signature rather than the next `{` block. Without this the
        # scan runs on into the *following* declaration — crediting this function
        # with a write that is not its own and skipping that declaration
        # entirely (measured on `PRootKernel.resolveHostPath`, a pure resolver
        # that was credited with `installHandlerStubs`' `stub.writeText(…)`).
        rest = strip_strings(sig_text[close:]) if close > 0 else ""
        eq, brace = rest.find("="), rest.find("{")
        if eq >= 0 and (brace < 0 or eq < brace):
            body = [sig_text[close:]]
            depth = depth_of(strip_strings(body[0]))
            k = j + 1
            while depth > 0 and k < len(lines):
                s = strip_strings(lines[k])
                body.append(lines[k])
                depth += depth_of(s)
                k += 1
            out.append((name, params, "\n".join(body)))
            i = k
            continue
        # Body: brace-balanced from the signature's last line.
        bal, k, seen_brace, body = 0, j, False, []
        while k < len(lines):
            s = strip_strings(lines[k])
            bal += s.count("{") - s.count("}")
            if "{" in s:
                seen_brace = True
            body.append(lines[k])
            if seen_brace and bal <= 0:
                break
            k += 1
        out.append((name, params, "\n".join(body)))
        i = max(k, j) + 1
    return out


def read(root, rel):
    full = os.path.join(root, "src/android/app/src/main/java/com/rikkaminis/app", rel)
    try:
        return open(full, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def path_param_names(params):
    names = []
    for part in params.split(","):
        nm = part.strip().split(":")[0].strip()
        if PATH_PARAMS.match(nm):
            names.append(nm)
    return names


def scan(root):
    """Returns (violations, sites_checked, files_scanned, exempt_used)."""
    app = os.path.join(root, "src/android/app/src/main/java/com/rikkaminis/app")
    violations, checked, files = [], 0, 0
    exempt_used = {}
    evidence_cache = {}

    def evidence_text(rel_key):
        """Comment-free text with literal bodies kept — the EXEMPT view.

        Evidence patterns name things that live inside string literals
        (`"photos-export"`), so they must be matched against a view that did not
        collapse literals away. Structure, by contrast, is scanned on the
        collapsed view (`GUARD in body`), where a guard call quoted in a message
        cannot satisfy the criterion.
        """
        if rel_key not in evidence_cache:
            evidence_cache[rel_key] = strip_comments(read(root, rel_key), collapse_literals=False)
        return evidence_cache[rel_key]

    for dirpath, _dirs, filenames in os.walk(app):
        for fn in sorted(filenames):
            if not fn.endswith(".kt"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, app).replace(os.sep, "/")
            raw = open(full, encoding="utf-8", errors="replace").read()
            text = strip_comments(raw)
            files += 1
            for name, params, body in functions(text):
                if not path_param_names(params):
                    continue
                if not WRITE_CALL.search(body):
                    continue
                checked += 1
                if GUARD in body:
                    continue
                if rel in EXEMPT:
                    reason, evidence = EXEMPT[rel]
                    rotted = []
                    for ev_rel, pattern, need in evidence:
                        target = evidence_text(ev_rel or rel)
                        hits = len(re.findall(pattern, target))
                        if hits < need:
                            rotted.append(f"{ev_rel or rel}: /{pattern}/ {hits} < {need}")
                    if rotted:
                        violations.append(
                            f"{rel}:{name} — exempt ('{reason}') but its evidence rotted: "
                            + "; ".join(rotted)
                        )
                    else:
                        exempt_used[rel] = reason
                    continue
                violations.append(
                    f"{rel}:{name}({params.strip()[:60]}) — writes a host file from a path "
                    f"parameter without calling {GUARD}"
                )
    return violations, checked, files, exempt_used


def balanced_block(text, open_idx):
    """Index just past the closer matching text[open_idx]; -1 when unbalanced.

    String literals are skipped so a quoted brace (or a `${…}` interpolation
    inside one) can never be mistaken for structure.
    """
    depth, i = 0, open_idx
    while i < len(text):
        c = text[i]
        if c in "\"'":
            quote = c
            i += 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def io_block_regions(text):
    """[(start, end)] of every `withContext(Dispatchers.IO) { … }` block; None = unbalanced."""
    regions = []
    for m in IO_BLOCK_RE.finditer(text):
        open_idx = text.find("{", m.start(), m.end())
        if open_idx < 0:
            return None
        end = balanced_block(text, open_idx)
        if end < 0:
            return None
        regions.append((open_idx, end))
    return regions


def call_offsets(text, call_re, decl_re):
    """Offsets of every call, skipping the declaration it is named after.

    A definition (`fun probeWritable(`) is not a call site: counting it would
    report the declaration line as an unguarded caller — the false positive this
    criterion would otherwise open with.
    """
    decl_spans = [m.span() for m in decl_re.finditer(text)] if decl_re else []
    out = []
    for m in call_re.finditer(text):
        if any(s <= m.start() < e for s, e in decl_spans):
            continue
        out.append(m.start())
    return out


def app_kt_files(root):
    app = os.path.join(root, APP_REL)
    found = []
    for dirpath, _dirs, filenames in os.walk(app):
        for fn in sorted(filenames):
            if fn.endswith(".kt"):
                found.append(os.path.join(dirpath, fn))
    return found


def check_blocking_io(root):
    """Blocking host I/O reached off a `Dispatchers.IO` block.

    Returns (violations, rot, sites, notes). A target whose anchor file is gone
    while its package directory survives is ROT (the tree moved, not the bug);
    a tree that simply does not ship that package (the frozen `test_scan.py`
    fixtures build a `sandbox/`-only tree) is noted as `[PARTIAL]` and skipped.
    """
    app = os.path.join(root, APP_REL)
    violations, rot, notes = [], [], []
    sites = 0

    active = []
    for rel, call_pat, decl_pat, label in BLOCKING_IO_CALLS:
        path = os.path.join(app, *rel.split("/"))
        if os.path.isfile(path):
            active.append((rel, call_pat, decl_pat, label))
            continue
        pkg = os.path.dirname(path)
        if os.path.isdir(pkg):
            rot.append(
                f"{rel}: missing while `{os.path.basename(pkg)}/` still exists — "
                f"the {label} criterion lost its anchor"
            )
        else:
            notes.append(f"[PARTIAL] {label} criterion skipped ({rel} absent)")

    if not active:
        return violations, rot, sites, notes

    # One pass over the app package, holding on to nothing that is not a hit.
    # (An earlier revision stripped all ~600 sources into a dict first; that is
    # ~20 MB of live Python strings and got the whole sandbox SIGKILLed mid-run
    # at gate 26 of scan.sh. Keep it streaming.)
    rules = [(rel, re.compile(call_pat), re.compile(decl_pat), label)
             for rel, call_pat, decl_pat, label in active]
    decl_hits = {label: 0 for _rel, _cp, _dp, label in rules}
    sites_here = {rel: 0 for rel, _cp, _dp, _label in rules}
    kept, hits, total = {}, [], {label: 0 for _rel, _cp, _dp, label in rules}

    for full in app_kt_files(root):
        fps = os.path.relpath(full, app).replace(os.sep, "/")
        try:
            raw = open(full, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        text = strip_comments(raw)
        regions = None
        for rel, call_re, decl_re, label in rules:
            decl_hits[label] += len(decl_re.findall(text))
            offsets = call_offsets(text, call_re, decl_re)
            if not offsets:
                continue
            if regions is None:
                regions = io_block_regions(text)
                if regions is None:
                    rot.append(
                        f"{fps}: unbalanced braces around a withContext(Dispatchers.IO) block"
                    )
                    regions = []
            kept[fps] = (text, regions)
            total[label] += len(offsets)
            if fps == rel:
                sites_here[rel] += len(offsets)
            for off in offsets:
                hits.append((fps, off, label))
        del text

    for rel, _call_re, _decl_re, label in rules:
        if decl_hits[label] == 0:
            rot.append(
                f"{label}: no definition matching its declaration pattern anywhere — renamed "
                f"or removed, so the criterion would pass by looking at nothing"
            )
            continue
        if total[label] == 0:
            rot.append(
                f"{rel}: nothing calls this any more — the {label} criterion would be vacuous"
            )
            continue
        if sites_here[rel] == 0:
            # The call moved out of its home file. Still enforced (the scan above
            # is app-wide), but the anchor is worth naming: it is the place a
            # reader looks first when this criterion changes.
            notes.append(f"[PARTIAL] {label}: no call site left in {rel} (enforced app-wide)")

    if rot:
        # Nothing below can be trusted to have parsed: report and let main decide.
        return violations, rot, sites, notes

    for fps, off, label in hits:
        sites += 1
        text, regions = kept[fps]
        if not any(s <= off < e for s, e in regions):
            line = text.count("\n", 0, off) + 1
            violations.append(
                f"{fps}:{line} — {label} runs on the caller's thread; wrap it in "
                f"withContext(Dispatchers.IO) {{ … }}"
            )
    return violations, rot, sites, notes


# ── Criterion 3: the share-import UI hand-off must stay on a Looper thread ──
#
# The ISSUE-07 fix moved the provider import onto Dispatchers.IO, which made the
# `toast()` helper functionally dependent on hopping back to Main: `Toast.show()`
# on a worker thread throws "Can't toast on a thread that has not called
# Looper.prepare()". No criterion-2 rule can see that dependency (showing a
# Toast is not a blocking-I/O call), so a future cleanup that removes the hop
# would read as a pure simplification and pass every gate — then crash at
# runtime on the very first shared-provider import. This criterion pins the hop
# in the one file that owns it, and reports ROT when its premise disappears.
SHARE_TOAST_HOP_RE = re.compile(r"runOnUiThread\s*[({]|Handler\s*\(")

def check_share_toast_hop(root):
    """The share `toast()` must hop to a Looper thread.

    Returns (violations, rot, sites, notes) — the same contract as
    check_blocking_io, wired into main() the same way. Skipped ([PARTIAL]) on
    trees that do not ship the share package, e.g. the frozen `test_scan.py`
    fixtures, which build a `sandbox/`-only tree.
    """
    rel = "share/ShareReceiverActivity.kt"
    path = os.path.join(root, APP_REL, *rel.split("/"))
    violations, rot, notes = [], [], []
    sites = 0
    if not os.path.isfile(path):
        if os.path.isdir(os.path.dirname(path)):
            rot.append(
                f"{rel}: missing while `share/` still exists — the toast-hop "
                f"criterion lost its anchor"
            )
        else:
            notes.append("[PARTIAL] share toast hop criterion skipped (share/ absent)")
        return violations, rot, sites, notes

    text = strip_comments(open(path, encoding="utf-8", errors="replace").read())
    bodies = [body for name, _params, body in functions(text) if name == "toast"]
    if not bodies:
        rot.append(
            f"{rel}: no `fun toast(` left to check — the criterion would pass "
            f"by looking at nothing"
        )
        return violations, rot, sites, notes

    # Premise: the import really does run on a worker thread in this file. If a
    # future refactor pulls it back onto Main, the hop stops being required —
    # say so and let a human re-justify or retire the criterion instead of
    # enforcing it forever for no reason (the same rot-not-skip idiom as
    # criterion 2).
    regions = io_block_regions(text)
    if regions is None:
        rot.append(f"{rel}: unbalanced braces around a withContext(Dispatchers.IO) block")
        return violations, rot, sites, notes
    import_offsets = call_offsets(
        text, re.compile(r"importProviderJson\s*\("), re.compile(r"\bfun\s+importProviderJson\s*\(")
    )
    if not any(s <= o < e for s, e in regions for o in import_offsets):
        rot.append(
            f"{rel}: the toast-hop criterion's premise is gone — no "
            f"importProviderJson call inside a withContext(Dispatchers.IO) block"
        )
        return violations, rot, sites, notes

    head = text.find("fun toast(")
    for body in bodies:
        sites += 1
        if not SHARE_TOAST_HOP_RE.search(body):
            line = text.count("\n", 0, head) + 1 if head >= 0 else 0
            violations.append(
                f"{rel}:{line} — toast() shows the Toast on its calling thread; the provider "
                f"import now runs on Dispatchers.IO, where Toast.show() throws without a "
                f"Looper — hop back with runOnUiThread {{ … }}"
            )
    return violations, rot, sites, notes


# ── criterion 5: guard-call SHAPE at the model-carried write sites ──────────
#
# [da1031-C] ISSUE-04. The whitelist above is keyed on the PARAMETER NAME, so it
# cannot see a write that arrives through a model — `deleteItem(item: FileItem)`
# and `handleWriteFile(params: JSONObject)`. Widening that whitelist was
# rejected on evidence: measured on this tree it newly flags three legitimate
# sites (`parseAttachments` / `handleModelUseExec` / `postJsonRpc`), and one of
# them shares its FILE with a site this criterion exists to protect — a
# file-level exemption would therefore silently disable the check it was added
# for. Anchor on the FUNCTION NAME plus the write call instead, and require the
# guard to PRECEDE the write: a `return@launch` after the delete is not a guard,
# it is a post-mortem.
GUARD_SITES = (
    (
        "ui/sandbox/FileBrowserViewModel.kt",
        "deleteItem",
        re.compile(r"\.deleteRecursively\s*\(|\.delete\s*\("),
        "deleting a listed item's host file",
    ),
    (
        "debug/DebugRPCHandler.kt",
        "handleWriteFile",
        re.compile(r"\.writeBytes\s*\("),
        "overwriting a host file behind resolveHostPath",
    ),
)

# [da1031-C] The route that opens a mounted folder must hand the browser the
# guest-side prefix. Without it the browser works on a host path, so the guard
# above never sees a mount path at all — it is unreachable from that entry
# (and "Copy Absolute Path" copies the host path).
MOUNT_ROUTE = (
    "ui/navigation/AppNavigation.kt",
    "Routes.fileBrowser(",
    'linuxRoot = "/var/minis/mounts/',
)


def check_guard_call_shape(root):
    """Returns (violations, rot, sites, notes).

    [PARTIAL] convention, same as [check_blocking_io]: a tree that carries none
    of these anchors is a synthetic fixture tree (the frozen `test_scan.py`
    shape), not a regression. As soon as one anchor is present, a missing one is
    ROT — the criterion lost its subject and says so instead of checking nothing.
    """
    anchors = [(rel, fn, rx, label) for rel, fn, rx, label in GUARD_SITES]
    route_rel, route_anchor, route_needle = MOUNT_ROUTE
    present = [a for a in anchors if read(root, a[0])]
    if read(root, route_rel):
        present.append(MOUNT_ROUTE)
    if not present:
        return [], [], 0, [
            "[PARTIAL] the read-only-mount guard anchors (FileBrowserViewModel / "
            "DebugRPCHandler / AppNavigation) are absent from this tree — skipped"
        ]

    violations, rot, sites = [], [], 0
    for rel, fn, rx, label in anchors:
        src = read(root, rel)
        if not src:
            rot.append(f"{rel}: lost its anchor for {fn}() — criterion 5 cannot run")
            continue
        bodies = [body for name, _params, body in functions(strip_comments(src)) if name == fn]
        if not bodies:
            rot.append(f"{rel}: no definition of {fn}() — renamed? criterion 5 lost its subject")
            continue
        sites += 1
        body = bodies[0]
        guard_at = body.find(GUARD)
        write = rx.search(body)
        if write is None:
            rot.append(f"{rel}:{fn}() — the write call is gone; criterion 5 lost its subject")
            continue
        if guard_at < 0:
            violations.append(f"{rel}:{fn}() — {label} without calling {GUARD})")
        elif guard_at > write.start():
            violations.append(
                f"{rel}:{fn}() — {GUARD}) runs AFTER the write it is meant to refuse; "
                f"move it in front of it"
            )

    route_src = read(root, route_rel)
    if not route_src:
        rot.append(f"{route_rel}: lost its anchor — criterion 5 cannot run")
        return violations, rot, sites, []
    # Literal-preserving view: the needle lives inside a string literal, which
    # the default (collapsing) view replaces with `""`.
    route_text = strip_comments(route_src, collapse_literals=False)
    if route_anchor not in route_text:
        rot.append(f"{route_rel}: no {route_anchor} call — criterion 5 lost its subject")
        return violations, rot, sites, []
    sites += 1
    if route_needle not in route_text:
        violations.append(
            f"{route_rel} — the mounted-folder browse route opens the file browser "
            f"without a guest-side prefix ({route_needle!r} missing): the read-only "
            f"guard and Copy Absolute Path never see /var/minis/mounts/<name>/…"
        )
    return violations, rot, sites, []


# ── selftest ────────────────────────────────────────────────────────────────

FILLER_GUARD = "isLinuxPathUnderReadOnlyMount("


def _filler(tag):
    """A guarded path-parameter write site — filler for the criterion-1 rot guards."""
    return (
        "package com.rikkaminis.app.filler\n"
        "\n"
        f"class Filler{tag} {{\n"
        f"    fun write{tag}(outputPath: String, data: String) {{\n"
        f"        if ({FILLER_GUARD}outputPath)) return\n"
        "        java.io.File(outputPath).writeText(data)\n"
        "    }\n"
        "}\n"
    )


MOUNT_READERS = {
    # the shape criterion 2 exists to keep: the probe hops to IO
    "io": (
        "    suspend fun refreshWritability() {\n"
        "        val probed = entry.resolvedHostPath?.let { p ->\n"
        "            withContext(Dispatchers.IO) { @@FN@@(p) }\n"
        "        } ?: false\n"
        "    }\n"
    ),
    # the pre-fix shape (live until deep-audit-1025)
    "direct": (
        "    suspend fun refreshWritability() {\n"
        "        val probed = entry.resolvedHostPath?.let { p -> @@FN@@(p) } ?: false\n"
        "    }\n"
    ),
    # `withContext(IO) expr` — correct at runtime, but not a block: fail-closed
    "inline_io": (
        "    suspend fun refreshWritability() {\n"
        "        val probed = entry.resolvedHostPath?.let { p ->\n"
        "            withContext(Dispatchers.IO) @@FN@@(p)\n"
        "        } ?: false\n"
        "    }\n"
    ),
}


def _mount_store(shape="io", fn_name="probeWritable"):
    reader = MOUNT_READERS[shape].replace("@@FN@@", fn_name)
    return (
        "package com.rikkaminis.app.data\n"
        "\n"
        "import kotlinx.coroutines.Dispatchers\n"
        "import kotlinx.coroutines.withContext\n"
        "\n"
        "class MountedFoldersStore {\n"
        "    private val entry = Entry()\n"
        "\n"
        f"    fun {fn_name}(hostPath: String): Boolean {{\n"
        '        val probe = java.io.File(hostPath, ".minis-probe")\n'
        "        return runCatching { probe.outputStream().use { it.write(0) }; true }\n"
        "            .getOrDefault(false)\n"
        "            .also { runCatching { probe.delete() } }\n"
        "    }\n"
        "\n"
        + reader +
        "}\n"
    )


LOG_BODIES = {
    "io": (
        "            MinisTextButton(onClick = {\n"
        "                scope.launch {\n"
        "                    withContext(Dispatchers.IO) { AppLogger.clearLogs() }\n"
        "                    refresh()\n"
        "                }\n"
        "            })\n"
    ),
    # the pre-fix shape: purge on whatever thread the click arrived on
    "direct": (
        "            MinisTextButton(onClick = {\n"
        "                AppLogger.clearLogs()\n"
        "                refresh()\n"
        "            })\n"
    ),
    # IO used for a *different* call — the wrap must actually cover the purge
    "wrong_call": (
        "            MinisTextButton(onClick = {\n"
        "                withContext(Dispatchers.IO) { somethingElse() }\n"
        "                AppLogger.clearLogs()\n"
        "            })\n"
    ),
}


def _log_screen(shape="io"):
    return (
        "package com.rikkaminis.app.ui.settings\n"
        "\n"
        "import kotlinx.coroutines.Dispatchers\n"
        "import kotlinx.coroutines.withContext\n"
        "\n"
        "@Composable\n"
        "fun LogManagementScreen() {\n"
        + LOG_BODIES[shape] +
        "}\n"
    )


def _app_logger():
    return (
        "package com.rikkaminis.app.logging\n"
        "\n"
        "object AppLogger {\n"
        "    fun clearLogs() {\n"
        "    }\n"
        "}\n"
    )


# ── criterion-2 subject added by [deep-audit-1027 ISSUE-05] ──────────────────
# `RootfsManager.verifyIntegrity()` reads `.integrity_manifest` and stats seven
# critical files; the pre-fix ViewModel called it twice from a method reached
# straight from the screen's onClick.
ROOTFS_BODIES = {
    # the shape FIX-D ships: both reads inside `withContext(Dispatchers.IO) { … }`
    "io": (
        "    fun repairRootfs(ctx: Ctx) {\n"
        "        val manager = RootfsManager.getInstance(ctx)\n"
        "        viewModelScope.launch {\n"
        "            val initial = withContext(Dispatchers.IO) {\n"
        "                manager.verifyIntegrity()\n"
        "            }\n"
        "            if (initial.healthy) return@launch\n"
        "            manager.autoRepair()\n"
        "            val after = withContext(Dispatchers.IO) { manager.verifyIntegrity() }\n"
        "        }\n"
        "    }\n"
    ),
    # pre-fix shape (live until deep-audit-1027): the first read on the click thread
    "direct": (
        "    fun repairRootfs(ctx: Ctx) {\n"
        "        val manager = RootfsManager.getInstance(ctx)\n"
        "        val initial = manager.verifyIntegrity()\n"
        "        if (initial.healthy) return\n"
        "        viewModelScope.launch {\n"
        "            val after = withContext(Dispatchers.IO) { manager.verifyIntegrity() }\n"
        "        }\n"
        "    }\n"
    ),
    # the second read left inside `viewModelScope.launch`: Main.immediate is not an
    # IO hop, so the read still runs on the click thread
    "launch_only": (
        "    fun repairRootfs(ctx: Ctx) {\n"
        "        val manager = RootfsManager.getInstance(ctx)\n"
        "        withContext(Dispatchers.IO) { manager.verifyIntegrity() }\n"
        "        viewModelScope.launch {\n"
        "            val after = manager.verifyIntegrity()\n"
        "        }\n"
        "    }\n"
    ),
    # correct at runtime, wrong shape (`withContext(IO) expr`) — fail-closed
    "inline_io": (
        "    fun repairRootfs(ctx: Ctx) {\n"
        "        val manager = RootfsManager.getInstance(ctx)\n"
        "        val initial = withContext(Dispatchers.IO) manager.verifyIntegrity()\n"
        "    }\n"
    ),
}


def _rootfs_vm(shape="io"):
    return (
        "package com.rikkaminis.app.ui.sandbox\n"
        "\n"
        "import kotlinx.coroutines.Dispatchers\n"
        "import kotlinx.coroutines.launch\n"
        "import kotlinx.coroutines.withContext\n"
        "\n"
        "class RootfsManagementViewModel {\n"
        + ROOTFS_BODIES[shape] +
        "}\n"
    )


def _rootfs_manager():
    """The declaration the criterion's fail-closed anchor points at."""
    return (
        "package com.rikkaminis.app.sandbox\n"
        "\n"
        "class RootfsManager {\n"
        "    fun verifyIntegrity(): Boolean = true\n"
        "\n"
        "    suspend fun autoRepair(): Boolean = true\n"
        "}\n"
    )


SHARE_BODIES = {
    # the shape the fix ships (deep-audit-1026 ISSUE-07): both disk-bound calls
    # inside the withContext(Dispatchers.IO) block
    "io": (
        "    fun onPositive(json: String, items: List<String>) {\n"
        "        lifecycleScope.launch {\n"
        "            withContext(Dispatchers.IO) {\n"
        "                if (importProviderJson(json)) discardStagedAttachments(items)\n"
        "            }\n"
        "        }\n"
        "    }\n"
    ),
    # pre-fix shape for the import: the staged-file delete is wrapped, the
    # import (ensureConfigLoaded + prefs commit + Room write) is not
    "direct_import": (
        "    fun onPositive(json: String, items: List<String>) {\n"
        "        if (importProviderJson(json)) {\n"
        "            withContext(Dispatchers.IO) { discardStagedAttachments(items) }\n"
        "        }\n"
        "    }\n"
    ),
    # the delete left on the dialog callback's thread
    "direct_discard": (
        "    fun onPositive(json: String, items: List<String>) {\n"
        "        withContext(Dispatchers.IO) { importProviderJson(json) }\n"
        "        discardStagedAttachments(items)\n"
        "    }\n"
    ),
    # `lifecycleScope.launch` alone is NOT an IO wrap — the body still runs on
    # Main, so both calls must stay red (fail-closed on a plausible mistake)
    "launch_only": (
        "    fun onPositive(json: String, items: List<String>) {\n"
        "        lifecycleScope.launch {\n"
        "            if (importProviderJson(json)) discardStagedAttachments(items)\n"
        "        }\n"
        "    }\n"
    ),
}

# Criterion-3 fixtures: what the `toast()` helper looks like. `hop` is the shape
# ISSUE-07 ships; `direct` is the crash-on-first-import shape; `missing` is the
# rot shape (the helper was deleted outright, so the criterion must not read
# the tree as clean by checking nothing).
SHARE_TOAST_BODIES = {
    "hop": (
        "    private fun toast(msg: String) {\n"
        "        runOnUiThread {\n"
        "            android.widget.Toast.makeText(this, msg, android.widget.Toast.LENGTH_SHORT).show()\n"
        "        }\n"
        "    }\n"
    ),
    "direct": (
        "    private fun toast(msg: String) {\n"
        "        android.widget.Toast.makeText(this, msg, android.widget.Toast.LENGTH_SHORT).show()\n"
        "    }\n"
    ),
    "missing": "",
}


def _share_receiver(shape="io", toast_hop="hop"):
    return (
        "package com.rikkaminis.app.share\n"
        "\n"
        "import kotlinx.coroutines.Dispatchers\n"
        "import kotlinx.coroutines.launch\n"
        "import kotlinx.coroutines.withContext\n"
        "\n"
        "class ShareReceiverActivity {\n"
        "    fun importProviderJson(json: String): Boolean = json.isNotEmpty()\n"
        "\n"
        "    fun discardStagedAttachments(items: List<String>) {\n"
        "        items.forEach { java.io.File(it).length() }\n"
        "    }\n"
        "\n"
        + SHARE_BODIES[shape] +
        "\n"
        + SHARE_TOAST_BODIES[toast_hop] +
        "}\n"
    )


BROWSER_GUARD_BODY = (
    "        linuxPathOf(item)?.let { lp ->\n"
    "            if (PRootKernel.isLinuxPathUnderReadOnlyMount(lp)) {\n"
    "                return@launch\n"
    "            }\n"
    "        }\n"
)


def _file_browser_vm(shape="guard"):
    """Criterion-5 anchor: the delete path of a listed item.

    [shape] guard (default) / direct (no guard) / late (guard AFTER the delete —
    the post-mortem shape) / renamed (function renamed away → ROT).
    """
    guard = "" if shape == "direct" else BROWSER_GUARD_BODY
    head, tail = ("", guard) if shape == "late" else (guard, "")
    fn = "deleteEntry" if shape == "renamed" else "deleteItem"
    return (
        "package com.rikkaminis.app.ui.sandbox\n"
        "\n"
        "class FileBrowserViewModel {\n"
        "    private fun linuxPathOf(item: FileItem): String? = null\n"
        "\n"
        f"    fun {fn}(item: FileItem) {{\n"
        "        viewModelScope.launch(Dispatchers.IO) {\n"
        + head +
        "            try {\n"
        "                val deleted = if (item.isDirectory) "
        "item.file.deleteRecursively() else item.file.delete()\n"
        "            } catch (e: Exception) {\n"
        "            }\n"
        + tail +
        "        }\n"
        "    }\n"
        "}\n"
    )


RPC_GUARD_BODY = (
    "        if (PRootKernel.isLinuxPathUnderReadOnlyMount(path)) {\n"
    "            throw RPCException(-32000, \"Read-only mount: '$path' is not writable\")\n"
    "        }\n"
)


def _debug_rpc(shape="guard"):
    """Criterion-5 anchor: the RPC write path (`params` is not a whitelisted
    parameter name, which is exactly why this criterion is keyed on the
    function name)."""
    guard = "" if shape == "direct" else RPC_GUARD_BODY
    fn = "handleWrite" if shape == "renamed" else "handleWriteFile"
    return (
        "package com.rikkaminis.app.debug\n"
        "\n"
        "class DebugRPCHandler {\n"
        f"    private fun {fn}(params: JSONObject) {{\n"
        + guard +
        "        val hostFile = PRootKernel.resolveHostPath(path) ?: File(path)\n"
        "        hostFile.writeBytes(content.toByteArray())\n"
        "    }\n"
        "}\n"
    )


def _app_nav(shape="guard"):
    """Criterion-5 anchor: the mounted-folder browse route's guest prefix.

    [shape] guard (route carries the prefix) / no_prefix (host path only — the
    guard above then never sees a mount path) / no_route (no fileBrowser call at
    all → ROT).
    """
    if shape == "no_route":
        call = ""
    elif shape == "no_prefix":
        call = (
            "        navController.safeNavigate(\n"
            "            Routes.fileBrowser(\n"
            "                rootPath = hostPath,\n"
            "                rootLabel = entry.name,\n"
            "            ),\n"
            "        )\n"
        )
    else:
        call = (
            "        navController.safeNavigate(\n"
            "            Routes.fileBrowser(\n"
            "                rootPath = hostPath,\n"
            "                rootLabel = entry.name,\n"
            '                linuxRoot = "/var/minis/mounts/${entry.name}",\n'
            '                displayPrefix = "/var/minis/mounts/${entry.name}",\n'
            "            ),\n"
            "        )\n"
        )
    return (
        "package com.rikkaminis.app.ui.navigation\n"
        "\n"
        "class AppNavigation {\n"
        "    private fun mountedFolderDetail(hostPath: String?) {\n"
        + call +
        "    }\n"
        "}\n"
    )


def write_io_fixture(root, store_shape="io", log_shape="io", store_fn="probeWritable",
                     drop_store=False, drop_log=False, data_pkg=True, ui_pkg=True,
                     keep_pkgs_when_dropped=True, share_shape="io", drop_share=False,
                     toast_hop="hop", rootfs_shape="io", drop_rootfs=False,
                     browser_shape="guard", rpc_shape="guard", nav_shape="guard",
                     drop_guard_anchors=False):
    """A `com/rikkaminis/app` tree carrying both criterion-2 targets.

    [keep_pkgs_when_dropped] leaves an unrelated file in the package whose anchor
    was dropped, which is what a *moved* file looks like (ROT). With it off, the
    package is gone too, which is what a synthetic fixture tree looks like
    ([PARTIAL] — the frozen `test_scan.py` shape). [drop_guard_anchors] removes
    criterion 5's three anchors outright, which is the same [PARTIAL] shape for
    that criterion.
    """
    app = os.path.join(root, APP_REL)
    files = {}
    for i in range(22):
        files[f"filler/F{i}.kt"] = _filler(i)
    files["logging/AppLogger.kt"] = _app_logger()
    if not drop_store:
        files["data/MountedFoldersStore.kt"] = _mount_store(store_shape, store_fn)
    elif keep_pkgs_when_dropped:
        files["data/MountRegistry.kt"] = "package com.rikkaminis.app.data\n\nclass MountRegistry\n"
    if not drop_log:
        files["ui/settings/LogManagementScreen.kt"] = _log_screen(log_shape)
    elif keep_pkgs_when_dropped:
        files["ui/settings/OtherScreen.kt"] = (
            "package com.rikkaminis.app.ui.settings\n\nclass OtherScreen\n"
        )
    if not drop_share:
        files["share/ShareReceiverActivity.kt"] = _share_receiver(share_shape, toast_hop)
    elif keep_pkgs_when_dropped:
        files["share/ShareHost.kt"] = "package com.rikkaminis.app.share\n\nclass ShareHost\n"
    if not drop_rootfs:
        files["sandbox/RootfsManager.kt"] = _rootfs_manager()
        files["ui/sandbox/RootfsManagementViewModel.kt"] = _rootfs_vm(rootfs_shape)
    elif keep_pkgs_when_dropped:
        files["ui/sandbox/OtherScreen.kt"] = (
            "package com.rikkaminis.app.ui.sandbox\n\nclass OtherScreen\n"
        )
    if not data_pkg:
        files.pop("data/MountRegistry.kt", None)
    if not ui_pkg:
        files.pop("ui/settings/OtherScreen.kt", None)
    # Criterion 5's anchors belong to the same "did this tree ever ship the
    # package?" question: in the synthetic shape (keep_pkgs_when_dropped=False)
    # writing FileBrowserViewModel.kt would make `ui/sandbox/` exist again and
    # trip criterion 4's moved-file rot guard.
    if not drop_guard_anchors and keep_pkgs_when_dropped:
        files["ui/sandbox/FileBrowserViewModel.kt"] = _file_browser_vm(browser_shape)
        files["debug/DebugRPCHandler.kt"] = _debug_rpc(rpc_shape)
        files["ui/navigation/AppNavigation.kt"] = _app_nav(nav_shape)
    for rel, text in files.items():
        path = os.path.join(app, *rel.split("/"))
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def _run_gate(root):
    old = sys.argv
    sys.argv = ["readonly_mount_write_guard.py", str(root)]
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main()
        return code, buf.getvalue()
    finally:
        sys.argv = old


def selftest():
    """Fixture trees — one dirty arm per criterion, every one must turn it red."""
    results = []

    def run(name, expect_red, note_in=None, **kwargs):
        root = tempfile.mkdtemp(prefix="romount_")
        try:
            write_io_fixture(root, **kwargs)
            code, out = _run_gate(root)
            ok = (code == 1) if expect_red else (code == 0)
            if ok and note_in is not None and note_in not in out:
                ok, note_in = False, f"missing {note_in!r} in output"
            results.append((name, ok, note_in or f"exit={code}"))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    run("both call sites wrapped in Dispatchers.IO pass", False,
        "blocking-IO call site(s) inside Dispatchers.IO")
    run("the probe called straight from the suspend reader is caught", True,
        "data/MountedFoldersStore.kt", store_shape="direct")
    run("the log purge on the click's thread is caught", True,
        "ui/settings/LogManagementScreen.kt", log_shape="direct")
    run("a wrap around a different call does not cover the purge", True,
        "ui/settings/LogManagementScreen.kt", log_shape="wrong_call")
    run("the non-blocking `withContext(IO) expr` form is not accepted (fail-closed)", True,
        "data/MountedFoldersStore.kt", store_shape="inline_io")
    run("a renamed probe is a rot failure, not a silent skip", True,
        "no definition matching", store_fn="probe")
    run("a package that lost its anchor file is a rot failure", True,
        "lost its anchor", drop_store=True, drop_log=True)
    run("a tree that never shipped the package is skipped, not failed", False,
        "[PARTIAL]", drop_store=True, drop_log=True, keep_pkgs_when_dropped=False)
    run("the provider import wrapped in Dispatchers.IO passes", False)
    run("the provider import outside the IO block is caught", True,
        "share/ShareReceiverActivity.kt", share_shape="direct_import")
    run("the staged-file discard outside the IO block is caught", True,
        "share/ShareReceiverActivity.kt", share_shape="direct_discard")
    run("`lifecycleScope.launch` alone does not wrap the calls in IO", True,
        "share/ShareReceiverActivity.kt", share_shape="launch_only")
    run("a share anchor that lost its file is a rot failure", True,
        "lost its anchor", drop_share=True)
    run("the share toast hop after the import moved to IO passes", False,
        "share UI hand-off verified on a Looper thread")
    run("a Toast shown straight from the dialog thread is caught", True,
        "share/ShareReceiverActivity.kt", toast_hop="direct")
    run("a share tree whose toast() was removed is a rot failure, not a silent skip", True,
        "no `fun toast(", toast_hop="missing")
    run("the toast-hop criterion reports its premise gone when the import is not on IO", True,
        "premise is gone", share_shape="launch_only")
    run("both rootfs integrity reads inside Dispatchers.IO pass", False)
    run("the rootfs integrity read on the click thread is caught", True,
        "rootfs integrity verification (reads .integrity_manifest", rootfs_shape="direct")
    run("`viewModelScope.launch` alone does not wrap the rootfs read in IO", True,
        "ui/sandbox/RootfsManagementViewModel.kt", rootfs_shape="launch_only")
    run("the non-blocking `withContext(IO) expr` rootfs wrap is not accepted", True,
        "ui/sandbox/RootfsManagementViewModel.kt", rootfs_shape="inline_io")
    run("a rootfs ViewModel anchor that lost its file is a rot failure", True,
        "lost its anchor", drop_rootfs=True)
    run("a tree that never shipped the rootfs ViewModel package is skipped, not failed", False,
        "[PARTIAL]", drop_rootfs=True, keep_pkgs_when_dropped=False)

    # ── criterion 5 (da1031-C) ──────────────────────────────────────────────
    run("the mounted-folder browser keeps the read-only guard in front of the delete", False,
        "model-carried write/route site(s) carry the guard")
    run("a delete that skips the read-only guard is caught", True,
        "FileBrowserViewModel.kt:deleteItem(", browser_shape="direct")
    run("a guard placed AFTER the delete is caught (post-mortem is not a guard)", True,
        "runs AFTER the write", browser_shape="late")
    run("a renamed deleteItem is a rot failure, not a silent skip", True,
        "no definition of deleteItem(", browser_shape="renamed")
    run("an RPC write that skips the read-only guard is caught", True,
        "DebugRPCHandler.kt:handleWriteFile(", rpc_shape="direct")
    run("a renamed handleWriteFile is a rot failure, not a silent skip", True,
        "no definition of handleWriteFile(", rpc_shape="renamed")
    run("a mounted-folder route that drops the guest prefix is caught", True,
        "without a guest-side prefix", nav_shape="no_prefix")
    run("a route whose fileBrowser call was removed is a rot failure", True,
        "no Routes.fileBrowser( call", nav_shape="no_route")
    run("a tree that never shipped the criterion-5 anchors is skipped, not failed", False,
        "[PARTIAL] the read-only-mount guard anchors", drop_guard_anchors=True)

    print("=== readonly_mount_write_guard --selftest ===")
    failed = 0
    for name, ok, note in results:
        if not ok:
            failed += 1
        print(f"[{'ok' if ok else 'FAIL'}] {name} -> {note}")
    if failed:
        print(f"SELFTEST: {failed} arm(s) misbehaved")
        return 1
    print(f"MOUNT_PROBE_IO_SELFTEST=PASS ({len(results)} arms)")
    return 0


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    root = os.path.abspath(args[0] if args else os.path.join(os.path.dirname(__file__), "..", ".."))
    violations, checked, files, exempt_used = scan(root)
    io_violations, io_rot, io_sites, io_notes = check_blocking_io(root)
    hop_violations, hop_rot, hop_sites, hop_notes = check_share_toast_hop(root)
    shape_violations, shape_rot, shape_sites, shape_notes = check_guard_call_shape(root)

    rot = []
    if files < 20:
        rot.append(f"only {files} Kotlin files under com/rikkaminis/app — the source tree moved?")
    if checked < 3:
        rot.append(f"only {checked} path-parameter write sites found — the detector stopped matching?")
    rot += io_rot
    rot += hop_rot
    rot += shape_rot
    if rot:
        print("ROT — the guard can no longer see what it is supposed to check:")
        for r in rot:
            print(f"  ✗ {r}")
        return 1
    for n in io_notes + hop_notes + shape_notes:
        print(f"  {n}")
    if violations or io_violations:
        print("Unguarded host-file writes:")
        for v in violations + io_violations:
            print(f"  ✗ {v}")
    if hop_violations:
        print("UI calls left on the wrong thread:")
        for v in hop_violations:
            print(f"  ✗ {v}")
    if shape_violations:
        print("Read-only-mount guard not wired at the model-carried write site:")
        for v in shape_violations:
            print(f"  ✗ {v}")
    if violations or io_violations or hop_violations or shape_violations:
        return 1
    print(f"OK: {checked} path-parameter write site(s) guarded ({files} files scanned)")
    print(f"    {io_sites} blocking-IO call site(s) inside Dispatchers.IO")
    if hop_sites:
        print(f"    {hop_sites} share UI hand-off verified on a Looper thread")
    if shape_sites:
        print(
            f"    {shape_sites} model-carried write/route site(s) carry the guard "
            f"in front of the write"
        )
    for rel, reason in sorted(exempt_used.items()):
        print(f"  · exempt {rel}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

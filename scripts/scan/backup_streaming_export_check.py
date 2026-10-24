#!/usr/bin/env python3
"""backup_streaming_export_check — deep-audit-1023 FIX-I (ISSUE-10, P1) gate.

Pins the automatic-backup streaming + fetch-hardening contract:

  arm1 (streaming export): AutoBackupManager.runLocked must stream the
        document into the local file via `ConfigBackup.exportToWriter(` and
        must NOT assemble the whole document as a String via
        `ConfigBackup.export(` — the auto payload (skills + memory + shared/
        artifacts + knowledge graph) reaches tens of MB on a heavy install
        and the String path materializes it ×2 in UTF-16 before the first
        byte hits disk. runLocked must also rotate through the shared
        `rotateLocalAutoBackups(` helper instead of an inline drop loop.
  arm2 (WebDavSync surface): WebDavSync must expose a `backupAuto` File
        overload (callers that already have the document on disk must not
        rebuild a String to upload it) and the pure
        `sanitizeAutoBackupStamp(` token reducer.
  arm3 (screen fetch hardening): BackupSettingsScreen's fetch-to-local path
        must sanitize the remote display name through
        `WebDavSync.sanitizeAutoBackupStamp(` (the name arrives from the
        WebDAV server and used to be concatenated straight into a local
        path) and must join the fetched copy into the shared local rotation
        via `rotateLocalAutoBackups(`.
  arm4 (no main-thread dir listing): every `listLocal(` call line in
        BackupSettingsScreen must ride a `withContext(Dispatchers.IO)` on the
        SAME line. The threading contract is deliberately style-pinned
        (single-line wrapping) so the gate stays a dumb, greppable text check
        instead of a brace-tracking heuristic; the entry load, the post-run
        refresh and the post-fetch refresh are the only three call sites.
  arm5 (secret-warning routing flag, deep-audit-1027 ISSUE-03): the
        `webDavUploadPending` marker is read once, inside the shared secret
        confirmation (`runExport`), to decide whether a confirmed export goes
        to WebDAV or through the local SAF picker. Any path that can leave a
        stale `true` behind silently re-routes a later LOCAL export to a
        remote upload (API keys included). The flag must therefore be cleared
        on >= 3 sites: the local-export entry, the `runExport` consumption and
        the dialog's `onDismissRequest`. Anchor is fail-closed: if the flag
        declaration disappears (renamed/moved), the arm goes RED instead of
        silently counting zero and passing — re-point the arm at the new name.
        The count contract is deliberately dumb/greppable (same style as arm4)
        rather than brace-tracking.
  arm6 (fetch-to-local busy gate + tail liveness, deep-audit-1040 B-08): the
        `onFetch` handler pulls a full auto-backup payload into memory and then
        writes + rotates `filesDir/backup-autos` — the same directory the
        nightly run, the SAF export and every restore touch — but it used to
        raise only the dialog-local `autoRemoteLoading`, so dismissing the
        dialog mid-flight left "Run Now" / "Restore" / "Export" live and two
        full payloads could land on that directory at once. It also ran its
        post-fetch refresh after the composition (and with it the coroutine
        scope) was gone. Three decisions are pinned on the handler REGION
        (located by brace matching, not by a line pattern): the fetch path
        claims the shared `operationBusy` gate, releases it inside the
        `finally` of the same region, and only refreshes the list while the
        coroutine is still alive. Fail-closed: a handler region that cannot be
        located — or that no longer carries the refresh call — is RED, not a
        silent zero-count pass; re-point the arm, do not delete it.

Usage:
  python3 backup_streaming_export_check.py <repo_root>   # real tree
  python3 backup_streaming_export_check.py --selftest    # fixtures

Selftest marker: BACKUP_STREAMING_EXPORT_SELFTEST=PASS
"""

import os
import re
import sys
import tempfile

AUTO_MGR_REL = "src/android/app/src/main/java/com/rikkaminis/app/backup/AutoBackupManager.kt"
SYNC_REL = "src/android/app/src/main/java/com/rikkaminis/app/backup/WebDavSync.kt"
SCREEN_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/BackupSettingsScreen.kt"

FILE_OVERLOAD_PAT = re.compile(r"fun backupAuto\(\s*config: WebDavConfig,\s*file: File")


def strip_comments(src):
    """Remove // line comments and /* */ block comments (KDoc included),
    STRING-AWARE: `"` and `'` literals (regular + triple-quoted) are consumed
    as strings so a `/*` or `//` inside a string literal (BackupSettingsScreen
    has one at its path-pattern site) cannot open a phantom block comment and
    silently eat half the file."""
    out = []
    i, n = 0, len(src)
    state = "code"  # code | line | block | str | rawstr | char
    while i < n:
        if state == "line":
            if src[i] == "\n":
                state = "code"
                out.append("\n")
            i += 1
        elif state == "block":
            if src.startswith("*/", i):
                state = "code"
                i += 2
            else:
                i += 1
        elif state == "rawstr":
            if src.startswith('"""', i):
                state = "code"
                i += 3
            else:
                out.append(src[i])
                i += 1
        elif state == "str":
            c = src[i]
            if c == "\\":
                i += 2
            elif c == '"':
                state = "code"
                i += 1
            elif c == "\n":
                state = "code"  # unterminated single-line string — bail to code
                out.append("\n")
                i += 1
            else:
                out.append(c)
                i += 1
        elif state == "char":
            c = src[i]
            if c == "\\":
                i += 2
            elif c == "'":
                state = "code"
                i += 1
            else:
                i += 1
        else:  # code
            if src.startswith("//", i):
                state = "line"
                i += 2
            elif src.startswith("/*", i):
                state = "block"
                i += 2
            elif src.startswith('"""', i):
                state = "rawstr"
                i += 3
            elif src[i] == '"':
                state = "str"
                i += 1
            elif src[i] == "'":
                state = "char"
                i += 1
            else:
                out.append(src[i])
                i += 1
    return "".join(out)


def read_stripped(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    src = open(path, encoding="utf-8", errors="replace").read()
    return strip_comments(src)


DEAD_COND_PAT = re.compile(
    r"\bif\s*\(\s*(?:false\b|!\s*true\b|0\s*!=\s*0\b|1\s*==\s*0\b|true\s*==\s*false\b)")


def brace_block_span(text, pos):
    """(start, end) of the SMALLEST `{ … }` block containing `pos`."""
    depth = 0
    i = pos - 1
    while i >= 0:
        c = text[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                d = 0
                for j in range(i, len(text)):
                    if text[j] == "{":
                        d += 1
                    elif text[j] == "}":
                        d -= 1
                        if d == 0:
                            return i, j + 1
                return i, len(text)
            depth -= 1
        i -= 1
    return 0, len(text)


def fun_span(code, name):
    """(start, end) of `fun <name>( … ) { … }`, or None."""
    m = re.search(r"\bfun\s+%s\s*\(" % re.escape(name), code)
    if m is None:
        return None
    brace = code.find("{", m.end())
    if brace < 0:
        return None
    start, end = brace_block_span(code, brace + 1)
    return start, end


def dead_guarded(code, pos):
    """True when `pos` can never execute: its own statement is gated on a
    constant that is false, or an enclosing block is (`if (false) { … }`).

    `if (false) rotateLocalAutoBackups(app)` keeps the call, the helper and
    every other anchor of arm 1 — the rotation simply never runs."""
    line_start = code.rfind("\n", 0, pos) + 1
    if DEAD_COND_PAT.search(code[line_start:pos]):
        return True
    # A guard on the previous line / on a wrapped statement: `if (false)\n call()`.
    j = pos - 1
    while j >= 0 and code[j] in " \t\r\n":
        j -= 1
    if j >= 0 and code[j] == ")":
        depth = 0
        i = j
        while i >= 0:
            if code[i] == ")":
                depth += 1
            elif code[i] == "(":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        if re.search(r"\bif\s*$", code[max(0, i - 40):i]) and DEAD_COND_PAT.search(code[i:j + 1]):
            return True
    # Enclosing blocks: walk out through every `{ … }` that contains `pos`.
    probe = pos
    while True:
        start, end = brace_block_span(code, probe)
        if start == 0 and end == len(code):
            break
        head = code[max(0, start - 90):start]
        if DEAD_COND_PAT.search(head):
            return True
        if start <= 0:
            break
        probe = start
    return False


def paren_block(code, open_idx):
    """The balanced `( … )` starting at `open_idx`, or None."""
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return code[open_idx:i + 1]
    return None


def assign_target(code, pos):
    """The variable `val X =` / `X =` whose value starts at `pos`, or None."""
    head = code[max(0, pos - 240):pos]
    m = re.search(r"(?:val\s+|var\s+)?([A-Za-z_]\w*)\s*(?::\s*[\w<>?,. ]+)?=\s*$", head)
    return m.group(1) if m else None


def check_arm1(auto_code):
    """AutoBackupManager: streaming export + shared rotation, and the rotation
    must be REACHABLE.

    wide-1024: the old criterion was a whole-file substring match, so
    `if (false) rotateLocalAutoBackups(app)` read exactly like the real call —
    `filesDir/backup-autos` then grows without bound until the device is full,
    which is the defect [deep-audit-1027 B-04] closed. The check is now scoped
    to runLocked's body and the call site is checked for a dead guard.
    """
    if auto_code is None:
        return "AutoBackupManager.kt not found"
    span = fun_span(auto_code, "runLocked")
    if span is None:
        return ("AutoBackupManager.runLocked not found (anchor drift) — re-point arm 1 "
                "at the renamed writer; do not delete it")
    region = auto_code[span[0]:span[1]]
    if "ConfigBackup.exportToWriter(" not in region:
        return "runLocked must stream via ConfigBackup.exportToWriter( (no full-document String)"
    if "ConfigBackup.export(" in region:
        return "runLocked must not assemble the whole document via ConfigBackup.export("
    pos = region.find("rotateLocalAutoBackups(app)")
    if pos < 0:
        return "runLocked must rotate through the shared rotateLocalAutoBackups(app) helper"
    if dead_guarded(region, pos):
        return ("runLocked's shared rotation is DEAD-GUARDED (a constant condition can "
                "never hold) — the call is there, the rotation never runs, and "
                "filesDir/backup-autos grows without bound until the device is full")
    return None


def check_arm2(sync_code):
    """WebDavSync: File overload for backupAuto + the stamp sanitizer."""
    if sync_code is None:
        return "WebDavSync.kt not found"
    if not FILE_OVERLOAD_PAT.search(sync_code):
        return "WebDavSync.backupAuto needs a File overload (streaming callers must not rebuild a String)"
    if "fun sanitizeAutoBackupStamp(" not in sync_code:
        return "WebDavSync must define sanitizeAutoBackupStamp( (pure filename-token reducer)"
    return None


SANITIZE_CALL = "WebDavSync.sanitizeAutoBackupStamp"


def check_arm3(screen_code):
    """Fetch-to-local path: sanitize, and USE what the sanitizer returned.

    wide-1024: the old criterion proved only that the call appears somewhere in
    the screen. Deleting the null-guard and building the destination from the
    RAW remote display name keeps the call — while writing a
    remote-controlled path (`../../../…`, absolute `/data/…`) into
    `filesDir/backup-autos`, i.e. the path-traversal / overwrite defect
    [deep-audit-1031 A-07] re-opened with one line.

    Criterion (per call site, on its enclosing block):
      1. the result is BOUND to a variable (not discarded);
      2. the sanitizer's null result is REJECTED (guard or `?:`) — the whole
         point of the reducer is that it can say "no";
      3. the destination `File(…)` is built from the sanitized value (or a
         variable assigned from it), never from the raw token alone.
    An unparseable call site fail-closes.
    """
    if screen_code is None:
        return "BackupSettingsScreen.kt not found"
    if SANITIZE_CALL + "(" not in screen_code:
        return "fetch-to-local must sanitize the remote display name via WebDavSync.sanitizeAutoBackupStamp("
    if "rotateLocalAutoBackups(context)" not in screen_code:
        return "fetch-to-local must join the shared local rotation via rotateLocalAutoBackups(context)"
    for m in re.finditer(re.escape(SANITIZE_CALL), screen_code):
        if re.search(r"fun\s+$", screen_code[max(0, m.start() - 40):m.start()]):
            continue  # the declaration itself
        op = screen_code.find("(", m.end() - 1)
        block = paren_block(screen_code, op) if op >= 0 else None
        if block is None:
            return ("WebDavSync.sanitizeAutoBackupStamp( … ) does not balance at offset %d "
                    "(fail-closed)" % m.start())
        call_end = op + len(block)
        var = assign_target(screen_code, m.start())
        if not var:
            return ("the sanitized remote name is DISCARDED at offset %d (result not bound "
                    "to a variable) — the file name is still derived from the raw, "
                    "remote-controlled display name (path traversal / overwrite into "
                    "filesDir/backup-autos)" % m.start())
        start, end = brace_block_span(screen_code, m.start())
        after = screen_code[call_end:end]
        guarded = (re.match(r"\s*\?:", after) is not None
                   or re.search(r"\b%s\s*==\s*null\b" % re.escape(var), after) is not None
                   or re.search(r"\bnull\s*==\s*%s\b" % re.escape(var), after) is not None
                   or re.search(r"requireNotNull\s*\(\s*%s\b" % re.escape(var), after) is not None)
        if not guarded:
            return ("the sanitizer's rejection is IGNORED (`%s` is never null-checked) — a "
                    "remote display name that the reducer refuses is then written as-is "
                    "into filesDir/backup-autos" % var)
        uses = [x for x in re.finditer(r"\b%s\b" % re.escape(var), after)]
        if not uses:
            return ("`%s` (the sanitized result) is never read — the destination path is "
                    "built from something else, so sanitising the name changed nothing" % var)
        aliases = {var}
        for a in re.finditer(r"\b([A-Za-z_]\w*)\s*=\s*%s\b" % re.escape(var), after):
            aliases.add(a.group(1))
        region = screen_code[start:end]
        dests = [paren_block(region, d.end() - 1)
                 for d in re.finditer(r"\bFile\s*\(", region)]
        dests = [d for d in dests if d]
        if dests and not any(any(re.search(r"\b%s\b" % re.escape(a), d) for a in aliases)
                             for d in dests):
            return ("the destination `File(…)` is not built from the sanitized value "
                    "(none of %s appears in it) — the guard protects a value the writer "
                    "never uses" % ", ".join(sorted("`%s`" % a for a in aliases)))
    return None


def check_arm4(screen_code):
    """Every listLocal( call line in the screen must ride withContext(Dispatchers.IO)."""
    if screen_code is None:
        return "BackupSettingsScreen.kt not found"
    violations = []
    for lineno, line in enumerate(screen_code.split("\n"), 1):
        if "listLocal(" in line and "Dispatchers.IO" not in line:
            violations.append(lineno)
    if violations:
        return (
            "main-thread local auto-backup listing: listLocal( lines without "
            "withContext(Dispatchers.IO) on the same line (style-pinned contract): "
            f"{violations}"
        )
    return None


def check_arm5(screen_code):
    """Secret-warning routing flag must be cleared on every escaping path.

    A leaked `webDavUploadPending = true` (dialog dismissed via back press /
    tap outside) re-routes the NEXT local export into a WebDAV upload —
    silently shipping a secrets-included payload to the remote server.
    """
    if screen_code is None:
        # Drift-probe / mini-tree fixture without the screen: nothing to pin.
        return None
    if not re.search(r"\bvar webDavUploadPending\b", screen_code):
        return (
            "BackupSettingsScreen must declare `var webDavUploadPending` — the "
            "secret-warning routing flag vanished (anchor drift). Re-point this "
            "arm at the renamed flag; do not delete the arm."
        )
    clears = screen_code.count("webDavUploadPending = false")
    if clears < 3:
        return (
            "webDavUploadPending must be cleared on >= 3 sites (local-export "
            "entry / runExport consumption / dialog onDismissRequest) — a stale "
            "`true` redirects a later local export to a remote WebDAV upload "
            f"(secrets included): found {clears}"
        )
    return None


def _braced_region(src, start):
    """Text from `start` through the brace that closes the first `{` at or after
    it, or None when the braces never balance."""
    open_i = src.find("{", start)
    if open_i < 0:
        return None
    depth = 0
    for i in range(open_i, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    return None


def fetch_handler_region(screen_code):
    """The `onFetch` handler body, located by brace matching rather than by a
    whole-line pattern (any of `onFetch = {`, `fun onFetch(...) {` matches).

    Every `onFetch` token whose block opens is considered; the candidate that
    carries the post-fetch list refresh wins. Returns None when no such region
    exists — arm6 reports that as anchor drift instead of passing on a zero
    count.
    """
    for m in re.finditer(r"\bonFetch\b", screen_code):
        region = _braced_region(screen_code, m.start())
        if region and "openAutoRemoteList(" in region:
            return region
    return None


LIVENESS_PAT = re.compile(r"\b(?:isActive|isCancelled)\b|\bensureActive\(")


def check_arm6(screen_code):
    """Fetch-to-local rides the shared busy gate; its tail refresh is liveness-guarded.

    deep-audit-1040 B-08 (P1). Three decisions, each pinned on the handler
    region (brace-located) instead of on a single line:

      1. the fetch path CLAIMS the shared `operationBusy` gate (dismissing the
         dialog must not leave "Run Now" / "Restore" / "Export" clickable while
         a full payload is being written into `filesDir/backup-autos`);
      2. it RELEASES the gate inside the `finally` of the same region, so the
         release sits on the exit path rather than on the happy path only;
      3. the tail `openAutoRemoteList()` runs only while the coroutine is still
         alive (`isActive` / `ensureActive()`), otherwise a torn-down screen
         re-arms its spinner from a dead scope.

    Fail-closed: an unlocatable handler region, or one that lost the refresh
    call, is RED — re-point the arm rather than deleting it.
    """
    if screen_code is None:
        return "BackupSettingsScreen.kt not found"
    if not re.search(r"\bonFetch\b", screen_code):
        return (
            "the onFetch fetch-to-local handler vanished (anchor drift) — "
            "re-point this arm at the renamed handler; do not delete it"
        )
    region = fetch_handler_region(screen_code)
    if region is None:
        return (
            "could not locate the onFetch handler region (no block carrying "
            "openAutoRemoteList() — anchor drift). Re-point this arm; a zero "
            "count must not read as a pass."
        )
    if "operationBusy = true" not in region:
        return (
            "fetch-to-local must claim the shared operationBusy gate (the "
            "payload is written + rotated into filesDir/backup-autos while "
            "Run Now / Restore / Export stay clickable otherwise)"
        )
    finally_i = region.find("finally")
    if finally_i < 0:
        return (
            "fetch-to-local must release the shared operationBusy gate on its "
            "exit path (no `finally` in the handler region)"
        )
    finally_region = _braced_region(region, finally_i) or ""
    # wide-1024 (order axis): a release that is followed by another claim is
    # not a release — the LAST write in the exit path is the one that survives.
    writes = re.findall(r"operationBusy\s*=\s*(true|false)", finally_region)
    if not writes:
        return (
            "fetch-to-local must release the shared operationBusy gate inside "
            "its `finally` (a release on the happy path only leaks the gate)"
        )
    if writes[-1] != "false":
        return (
            "fetch-to-local's `finally` ends with `operationBusy = %s` — the last write "
            "on the exit path wins, so the shared gate stays CLAIMED after the fetch "
            "finishes and Run Now / Restore / Export are dead until the screen is "
            "recreated (a release followed by a re-claim is not a release)" % writes[-1]
        )
    refresh_i = region.find("openAutoRemoteList(")
    window = region[max(0, refresh_i - 200) : refresh_i]
    if not LIVENESS_PAT.search(window):
        return (
            "the post-fetch openAutoRemoteList() must be liveness-guarded "
            "(isActive / ensureActive()) — a refresh fired from an already "
            "cancelled scope re-arms the dialog spinner on a dead composition"
        )
    return None


def check_all(root):
    auto = read_stripped(root, AUTO_MGR_REL)
    sync = read_stripped(root, SYNC_REL)
    screen = read_stripped(root, SCREEN_REL)
    failures = []
    for name, err in (
        ("arm1", check_arm1(auto)),
        ("arm2", check_arm2(sync)),
        ("arm3", check_arm3(screen)),
        ("arm4", check_arm4(screen)),
        ("arm5", check_arm5(screen)),
        ("arm6", check_arm6(screen)),
    ):
        if err:
            failures.append(f"{name}: {err}")
    return failures


# --- compliant / violation fixture snippets (code-level, not comments) ---

COMPLIANT_AUTO = """package com.rikkaminis.app.backup

object AutoBackupManager {
    private suspend fun runLocked(app: MinisApp) {
        val local = File(app.filesDir, "x").apply { mkdirs() }
        val unreadable = local.bufferedWriter().use { writer ->
            ConfigBackup.exportToWriter(
                providerRepo = app.providerRepository,
                includeSecrets = false,
                writer = writer,
            )
        }
        rotateLocalAutoBackups(app)
        WebDavSync.backupAuto(cfg, local)
    }
}
"""

COMPLIANT_SYNC = """package com.rikkaminis.app.backup

import java.io.File

object WebDavSync {
    fun backupAuto(config: WebDavConfig, payload: String, client: OkHttpClient = d()): String =
        putAutoBackup(config, payload.toByteArray(Charsets.UTF_8), client)

    fun backupAuto(
        config: WebDavConfig,
        file: File,
        client: OkHttpClient = d(),
    ): String = putAutoBackup(config, file.inputStream().use { it.readBytes() }, client)

    private fun putAutoBackup(config: WebDavConfig, bytes: ByteArray, client: OkHttpClient): String = "x"

    fun sanitizeAutoBackupStamp(raw: String): String? = raw
}
"""

COMPLIANT_SCREEN = """package com.rikkaminis.app.ui.settings

@Composable
fun BackupSettingsScreen(context: Context) {
    var showSecretWarning by remember { mutableStateOf(false) }
    var webDavUploadPending by remember { mutableStateOf(false) }
    var autoBackupFiles by remember { mutableStateOf<List<File>>(emptyList()) }
    LaunchedEffect(Unit) {
        autoBackupFiles = withContext(Dispatchers.IO) { AutoBackupManager.listLocal(context) }
    }
    SettingsRow(
        onClick = if (operationBusy) null else ({ webDavUploadPending = false; showSecretWarning = true }),
    )
    SettingsRow(
        onClick = if (webDavConfig != null && !operationBusy) {
            { webDavUploadPending = true; showSecretWarning = true }
        } else {
            null
        },
    )
    if (showSecretWarning) {
        val runExport: (Boolean) -> Unit = runExport@{ withSecrets ->
            operationBusy = true
            showSecretWarning = false
            val toWebDav = webDavUploadPending
            webDavUploadPending = false
            if (toWebDav) uploadRemote() else exportWithSecrets = withSecrets
        }
        AlertDialog(
            onDismissRequest = {
                showSecretWarning = false
                webDavUploadPending = false
            },
        )
    }
    fun onFetch(entry: WebDavSync.AutoBackupEntry) {
        if (!operationBusy) {
            operationBusy = true
            autoRemoteLoading = true
            try {
                val stamp = WebDavSync.sanitizeAutoBackupStamp(entry.item.displayName) ?: return
                val local = File(dir, AutoBackupManager.LOCAL_FILE_PREFIX + stamp + ".json")
                withContext(Dispatchers.IO) { local.writeText(json) }
                withContext(Dispatchers.IO) { AutoBackupManager.rotateLocalAutoBackups(context) }
                autoBackupFiles = withContext(Dispatchers.IO) { AutoBackupManager.listLocal(context) }
            } finally {
                autoRemoteLoading = false
                operationBusy = false
            }
            if (scope.isActive) openAutoRemoteList()
        }
    }
}
"""

VIOLATION_EXPORT_STRING = COMPLIANT_AUTO.replace(
    "            ConfigBackup.exportToWriter(\n"
    "                providerRepo = app.providerRepository,\n"
    "                includeSecrets = false,\n"
    "                writer = writer,\n"
    "            )",
    "            ConfigBackup.export(\n"
    "                providerRepo = app.providerRepository,\n"
    "                includeSecrets = false,\n"
    "            )",
)

VIOLATION_INLINE_ROTATION = COMPLIANT_AUTO.replace(
    "        rotateLocalAutoBackups(app)",
    "        listLocal(app).drop(3).forEach { runCatching { it.delete() } }",
)

VIOLATION_NO_FILE_OVERLOAD = COMPLIANT_SYNC.replace(
    "    fun backupAuto(\n"
    "        config: WebDavConfig,\n"
    "        file: File,\n"
    "        client: OkHttpClient = d(),\n"
    "    ): String = putAutoBackup(config, file.inputStream().use { it.readBytes() }, client)\n",
    "",
)

VIOLATION_NO_SANITIZER = COMPLIANT_SYNC.replace(
    "    fun sanitizeAutoBackupStamp(raw: String): String? = raw",
    "",
)

VIOLATION_FETCH_NO_SANITIZE = COMPLIANT_SCREEN.replace(
    "val stamp = WebDavSync.sanitizeAutoBackupStamp(entry.item.displayName) ?: return",
    "val stamp = entry.item.displayName.removePrefix(\"rikkaminis-backup-auto-\")",
)

VIOLATION_FETCH_NO_ROTATION = COMPLIANT_SCREEN.replace(
    "                withContext(Dispatchers.IO) { AutoBackupManager.rotateLocalAutoBackups(context) }\n",
    "",
)

VIOLATION_MAIN_THREAD_REMEMBER = COMPLIANT_SCREEN.replace(
    "    var autoBackupFiles by remember { mutableStateOf<List<File>>(emptyList()) }\n"
    "    LaunchedEffect(Unit) {\n"
    "        autoBackupFiles = withContext(Dispatchers.IO) { AutoBackupManager.listLocal(context) }\n"
    "    }",
    "    var autoBackupFiles by remember { mutableStateOf(AutoBackupManager.listLocal(context)) }",
)

VIOLATION_MAIN_THREAD_REFRESH = COMPLIANT_SCREEN.replace(
    "autoBackupFiles = withContext(Dispatchers.IO) { AutoBackupManager.listLocal(context) }",
    "autoBackupFiles = withContext(Dispatchers.Main) { AutoBackupManager.listLocal(context) }",
)

# arm5 (deep-audit-1027 ISSUE-03) — the secret-warning routing flag.
VIOLATION_NO_ENTRY_CLEAR = COMPLIANT_SCREEN.replace(
    "({ webDavUploadPending = false; showSecretWarning = true })",
    "({ showSecretWarning = true })",
)

VIOLATION_NO_DISMISS_CLEAR = COMPLIANT_SCREEN.replace(
    "            onDismissRequest = {\n"
    "                showSecretWarning = false\n"
    "                webDavUploadPending = false\n"
    "            },",
    "            onDismissRequest = { showSecretWarning = false },",
)

VIOLATION_FLAG_RENAMED = COMPLIANT_SCREEN.replace(
    "webDavUploadPending",
    "webDavUploadQueued",
)

# arm6 (deep-audit-1040 B-08) — fetch-to-local gate + tail liveness.
VIOLATION_FETCH_NO_BUSY_CLAIM = COMPLIANT_SCREEN.replace(
    "            operationBusy = true\n            autoRemoteLoading = true\n",
    "            autoRemoteLoading = true\n",
)

VIOLATION_FETCH_NO_BUSY_RELEASE = COMPLIANT_SCREEN.replace(
    "                autoRemoteLoading = false\n                operationBusy = false\n",
    "                autoRemoteLoading = false\n",
)

VIOLATION_FETCH_UNGUARDED_REFRESH = COMPLIANT_SCREEN.replace(
    "            if (scope.isActive) openAutoRemoteList()",
    "            openAutoRemoteList()",
)

VIOLATION_FETCH_HANDLER_RENAMED = COMPLIANT_SCREEN.replace(
    "fun onFetch(entry: WebDavSync.AutoBackupEntry) {",
    "fun pullRemote(entry: WebDavSync.AutoBackupEntry) {",
)

# ── wide-1024 witnesses (mechanically reproduced on the real tree: pre-fix
# rc=0, post-fix rc=1) ────────────────────────────────────────────────────

# W1 (dataflow): the call and the helper both survive — the call can never run,
# so the rotation is dead and filesDir/backup-autos grows unbounded.
VIOLATION_ROTATION_DEAD_GUARD = COMPLIANT_AUTO.replace(
    "        rotateLocalAutoBackups(app)",
    "        if (false) rotateLocalAutoBackups(app)",
)

# W2 (dataflow, spelling→effect): the sanitizer's rejection is dropped and the
# path is built from the raw, remote-controlled display name.
VIOLATION_FETCH_SANITIZER_DEAD = COMPLIANT_SCREEN.replace(
    '                val stamp = WebDavSync.sanitizeAutoBackupStamp(entry.item.displayName) ?: return\n'
    '                val local = File(dir, AutoBackupManager.LOCAL_FILE_PREFIX + stamp + ".json")',
    '                val stamp = WebDavSync.sanitizeAutoBackupStamp(entry.item.displayName)\n'
    '                val local = File(dir, AutoBackupManager.LOCAL_FILE_PREFIX'
    ' + entry.item.displayName + ".json")',
)

# W2b: the guard is kept, the writer ignores the guarded value.
VIOLATION_FETCH_RAW_PATH = COMPLIANT_SCREEN.replace(
    'AutoBackupManager.LOCAL_FILE_PREFIX + stamp + ".json"',
    'AutoBackupManager.LOCAL_FILE_PREFIX + entry.item.displayName + ".json"',
)

# W3 (order): the release is followed by a re-claim — last write wins, the gate
# is left claimed.
VIOLATION_FETCH_GATE_RECLAIM = COMPLIANT_SCREEN.replace(
    '                autoRemoteLoading = false\n                operationBusy = false\n',
    '                autoRemoteLoading = false\n                operationBusy = false\n'
    '                operationBusy = true\n',
)


def write_tree(tmp, auto=None, sync=None, screen=None):
    for rel, content in (
        (AUTO_MGR_REL, auto),
        (SYNC_REL, sync),
        (SCREEN_REL, screen),
    ):
        if content is None:
            continue
        path = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def run_fixture(name, expect_ok, auto=None, sync=None, screen=None, contains=None):
    """`contains` pins WHICH criterion fired — a dirty fixture that goes red on
    an unrelated arm must not count as evidence for the arm under test."""
    with tempfile.TemporaryDirectory() as tmp:
        write_tree(
            tmp,
            auto=auto if auto is not None else COMPLIANT_AUTO,
            sync=sync if sync is not None else COMPLIANT_SYNC,
            screen=screen if screen is not None else COMPLIANT_SCREEN,
        )
        failures = check_all(tmp)
        ok = (not failures) == expect_ok
        if ok and contains is not None:
            ok = any(contains in f for f in failures)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] fixture {name}: failures={failures}")
        return ok


def selftest():
    print("backup_streaming_export_check selftest:")
    ok = True
    ok &= run_fixture("compliant", expect_ok=True)
    ok &= run_fixture("violation-export-string", expect_ok=False, auto=VIOLATION_EXPORT_STRING)
    ok &= run_fixture("violation-inline-rotation", expect_ok=False, auto=VIOLATION_INLINE_ROTATION)
    ok &= run_fixture("violation-no-file-overload", expect_ok=False, sync=VIOLATION_NO_FILE_OVERLOAD)
    ok &= run_fixture("violation-no-sanitizer", expect_ok=False, sync=VIOLATION_NO_SANITIZER)
    ok &= run_fixture("violation-fetch-no-sanitize", expect_ok=False, screen=VIOLATION_FETCH_NO_SANITIZE)
    ok &= run_fixture("violation-fetch-no-rotation", expect_ok=False, screen=VIOLATION_FETCH_NO_ROTATION)
    ok &= run_fixture("violation-main-thread-remember", expect_ok=False, screen=VIOLATION_MAIN_THREAD_REMEMBER)
    ok &= run_fixture("violation-main-thread-refresh", expect_ok=False, screen=VIOLATION_MAIN_THREAD_REFRESH)
    ok &= run_fixture("violation-entry-no-flag-clear", expect_ok=False, screen=VIOLATION_NO_ENTRY_CLEAR)
    ok &= run_fixture("violation-dismiss-no-flag-clear", expect_ok=False, screen=VIOLATION_NO_DISMISS_CLEAR)
    ok &= run_fixture("violation-flag-renamed-anchor-drift", expect_ok=False, screen=VIOLATION_FLAG_RENAMED)
    ok &= run_fixture("violation-fetch-no-busy-claim", expect_ok=False, screen=VIOLATION_FETCH_NO_BUSY_CLAIM)
    ok &= run_fixture("violation-fetch-no-busy-release", expect_ok=False, screen=VIOLATION_FETCH_NO_BUSY_RELEASE)
    ok &= run_fixture("violation-fetch-unguarded-refresh", expect_ok=False, screen=VIOLATION_FETCH_UNGUARDED_REFRESH)
    ok &= run_fixture("violation-fetch-handler-renamed", expect_ok=False, screen=VIOLATION_FETCH_HANDLER_RENAMED)
    # wide-1024: each witness must trip the arm it was written against.
    ok &= run_fixture("violation-rotation-dead-guard", expect_ok=False,
                      auto=VIOLATION_ROTATION_DEAD_GUARD, contains="DEAD-GUARDED")
    ok &= run_fixture("violation-fetch-sanitizer-dead", expect_ok=False,
                      screen=VIOLATION_FETCH_SANITIZER_DEAD, contains="rejection is IGNORED")
    ok &= run_fixture("violation-fetch-raw-path", expect_ok=False,
                      screen=VIOLATION_FETCH_RAW_PATH, contains="never read")
    ok &= run_fixture("violation-fetch-gate-reclaim", expect_ok=False,
                      screen=VIOLATION_FETCH_GATE_RECLAIM, contains="last write")
    print("BACKUP_STREAMING_EXPORT_SELFTEST=" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if len(sys.argv) < 2:
        print("usage: python3 backup_streaming_export_check.py <repo_root>", file=sys.stderr)
        sys.exit(2)
    root = sys.argv[1]
    failures = check_all(root)
    if failures:
        for f in failures:
            print("FAIL:", f)
        sys.exit(1)
    print("backup_streaming_export_check: OK (arm1 + arm2 + arm3 + arm4 + arm5 + arm6 green)")


if __name__ == "__main__":
    main()

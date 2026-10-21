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


def check_arm1(auto_code):
    """AutoBackupManager: streaming export + shared rotation."""
    if auto_code is None:
        return "AutoBackupManager.kt not found"
    if "ConfigBackup.exportToWriter(" not in auto_code:
        return "runLocked must stream via ConfigBackup.exportToWriter( (no full-document String)"
    if "ConfigBackup.export(" in auto_code:
        return "runLocked must not assemble the whole document via ConfigBackup.export("
    if "rotateLocalAutoBackups(app)" not in auto_code:
        return "runLocked must rotate through the shared rotateLocalAutoBackups(app) helper"
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


def check_arm3(screen_code):
    """Fetch-to-local path: sanitize + shared rotation."""
    if screen_code is None:
        return "BackupSettingsScreen.kt not found"
    if "WebDavSync.sanitizeAutoBackupStamp(" not in screen_code:
        return "fetch-to-local must sanitize the remote display name via WebDavSync.sanitizeAutoBackupStamp("
    if "rotateLocalAutoBackups(context)" not in screen_code:
        return "fetch-to-local must join the shared local rotation via rotateLocalAutoBackups(context)"
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
    if "operationBusy = false" not in finally_region:
        return (
            "fetch-to-local must release the shared operationBusy gate inside "
            "its `finally` (a release on the happy path only leaks the gate)"
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


def run_fixture(name, expect_ok, auto=None, sync=None, screen=None):
    with tempfile.TemporaryDirectory() as tmp:
        write_tree(
            tmp,
            auto=auto if auto is not None else COMPLIANT_AUTO,
            sync=sync if sync is not None else COMPLIANT_SYNC,
            screen=screen if screen is not None else COMPLIANT_SCREEN,
        )
        failures = check_all(tmp)
        ok = not failures
        status = "PASS" if ok == expect_ok else "FAIL"
        print(f"  [{status}] fixture {name}: failures={failures}")
        return status == "PASS"


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

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
    var autoBackupFiles by remember { mutableStateOf<List<File>>(emptyList()) }
    LaunchedEffect(Unit) {
        autoBackupFiles = withContext(Dispatchers.IO) { AutoBackupManager.listLocal(context) }
    }
    fun onFetch(entry: WebDavSync.AutoBackupEntry) {
        val stamp = WebDavSync.sanitizeAutoBackupStamp(entry.item.displayName) ?: return
        val local = File(dir, AutoBackupManager.LOCAL_FILE_PREFIX + stamp + ".json")
        withContext(Dispatchers.IO) { local.writeText(json) }
        withContext(Dispatchers.IO) { AutoBackupManager.rotateLocalAutoBackups(context) }
        autoBackupFiles = withContext(Dispatchers.IO) { AutoBackupManager.listLocal(context) }
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
    "        withContext(Dispatchers.IO) { AutoBackupManager.rotateLocalAutoBackups(context) }\n",
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
    print("backup_streaming_export_check: OK (arm1 + arm2 + arm3 + arm4 green)")


if __name__ == "__main__":
    main()

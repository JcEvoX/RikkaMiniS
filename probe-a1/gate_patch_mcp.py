#!/usr/bin/env python3
"""[dv20-a1 A-01] Extend mcp_oauth_lifecycle_check.py with the artifact-face
criteria for a credentials-free export.

Three criteria, reported as three groups (pack wiring / single rule +
fail-closed / archiver drop) so each fixture can prove it reds exactly one.

Idempotent: re-running is a no-op once the marker is present. Every insertion
asserts an exact anchor count of 1 and refuses to write on a miss.
"""
import io
import os
import sys

GATE = "/var/minis/shared/work/deep-audit-1040/a1/scripts/scan/mcp_oauth_lifecycle_check.py"
MARKER = "check_artifact_redaction"

CONSTS_ANCHOR = 'OAUTH_SKIP_REPORT = "oauth bridge entries"\n'
CONSTS_ADD = '''
# [dv20-a1 A-01] The artifact (output-asset) face of the same credential
# invariant: a credentials-free export must not archive servers.json verbatim.
ARTIFACT_ARCHIVER_REL = "src/android/app/src/main/java/com/rikkaminis/app/backup/ArtifactArchiver.kt"
ARTIFACT_WRITE_CALL = "ArtifactArchiver.writeEntries("
MCP_ARTIFACT_REDACTOR = "redactMcpArtifactEntry"
MCP_SECRET_STRIP = "stripExportedMcpSecrets"
'''

CHECK_FN = '''
def argument_text(code, call_start):
    """Parenthesised argument list of the call starting at [call_start]."""
    lp = code.find("(", call_start)
    if lp < 0:
        return ""
    depth = 0
    for j in range(lp, len(code)):
        if code[j] == "(":
            depth += 1
        elif code[j] == ")":
            depth -= 1
            if depth == 0:
                return code[lp:j + 1]
    return code[lp:]


def check_artifact_redaction(scoped, masked, archiver_masked):
    """[dv20-a1 A-01] The artifact face of a credentials-free backup.

    Returns three violation groups, one per decision:
      r1 the pack loop reaches the redactor `redactMcpArtifactEntry`, gated on
         `!includeSecrets` so an explicit with-credentials export is untouched;
      r2 the redactor DELEGATES to Stage 5's `stripExportedMcpSecrets` (one rule
         for one credential shape) and returns null when the bytes cannot be
         parsed instead of writing them through;
      r3 `writeEntries` drops — never writes — an entry whose transform
         returned null, and reports the drop.

    Anchored on the decision (who redacts, which bytes are dropped), not on a
    call's spelling: the redactor may be passed inline or bound to a local
    first. [scoped] keeps string literals; [masked]/[archiver_masked] blank
    them for the structural work.
    """
    r1, r2, r3 = [], [], []

    # -- R1: the pack face reaches the redactor, gated on credentials-free.
    m_root = re.search(r"val\\s+isMcpRoot\\s*=", masked)
    if not m_root:
        r1.append(
            "ConfigBackup.kt: no `val isMcpRoot =` in the artifact pack loop — the "
            "mcp artifact root is no longer distinguished; re-anchor this gate"
        )
        return r1, r2, r3
    call = masked.find(ARTIFACT_WRITE_CALL, m_root.end())
    if call < 0:
        r1.append(
            "ConfigBackup.kt: the artifact pack loop never calls `"
            + ARTIFACT_WRITE_CALL + "` — re-anchor this gate"
        )
        return r1, r2, r3
    args = argument_text(masked, call)
    window = masked[m_root.end():call]
    reachable = MCP_ARTIFACT_REDACTOR in args
    if not reachable:
        for ident in set(re.findall(r"\\b([A-Za-z_]\\w*)\\b", args)):
            if re.search(
                r"val\\s+" + re.escape(ident) + r"\\s*=[\\s\\S]{0,300}?"
                + re.escape(MCP_ARTIFACT_REDACTOR),
                window,
            ):
                reachable = True
                break
    if not reachable:
        r1.append(
            "ConfigBackup.kt: `" + ARTIFACT_WRITE_CALL + "` is handed no `"
            + MCP_ARTIFACT_REDACTOR + "(` — a credentials-free export archives "
            "mcp-servers/servers.json verbatim, so headers/env/--token= args/url "
            "query keys ride into the zip (and into every WebDAV copy of it)"
        )
    if not re.search(r"!\\s*includeSecrets", window):
        r1.append(
            "ConfigBackup.kt: the artifact redactor is not gated on `!includeSecrets` "
            "— an explicit with-credentials export (includeSecrets=true) would be "
            "silently redacted too"
        )

    # -- R2: one rule, and it must fail closed.
    body = body_of(masked, MCP_ARTIFACT_REDACTOR)
    body_scoped = body_of(scoped, MCP_ARTIFACT_REDACTOR)
    if body is None:
        r2.append(
            "ConfigBackup.kt: no `fun " + MCP_ARTIFACT_REDACTOR + "(` — the pack face "
            "has no redactor to hand the archiver; re-anchor this gate"
        )
    else:
        if MCP_SECRET_STRIP + "(" not in body:
            r2.append(
                "ConfigBackup.kt: `" + MCP_ARTIFACT_REDACTOR + "` does not call `"
                + MCP_SECRET_STRIP + "(` — a second, divergent secret rule now sits "
                "next to Stage 5's, which is how the two faces drift apart"
            )
        if body_scoped is not None and ('"headers"' in body_scoped or '"env"' in body_scoped):
            r2.append(
                "ConfigBackup.kt: `" + MCP_ARTIFACT_REDACTOR + "` blanks secret values "
                "itself (it names `headers`/`env`) instead of delegating to "
                + MCP_SECRET_STRIP + " — the two faces will drift"
            )
        if not re.search(r"catch\\s*\\([^)]*\\)\\s*\\{[^}]*\\bnull\\b", body):
            r2.append(
                "ConfigBackup.kt: `" + MCP_ARTIFACT_REDACTOR + "` has no parse-failure "
                "path returning null — unredactable bytes are written into a "
                "credentials-free archive verbatim; it must fail closed"
            )

    # -- R3: the archiver drops a rejected entry and says so.
    wb = body_of(archiver_masked, "writeEntries")
    if wb is None:
        r3.append(
            "ArtifactArchiver.kt: no `fun writeEntries(` — the archive face moved; "
            "re-anchor this gate"
        )
        return r1, r2, r3
    if "transform(" not in wb:
        r3.append(
            "ArtifactArchiver.kt: `writeEntries` never applies its transform — a caller "
            "that hands one is ignored and every entry is archived verbatim"
        )
        return r1, r2, r3
    m_res = re.search(r"val\\s+(\\w+)\\s*=\\s*transform\\(\\s*[^)]*\\)", wb)
    if not m_res:
        r3.append(
            "ArtifactArchiver.kt: the transform result is not bound to a local "
            "(`val x = transform(...)`) — nothing can tell a rejected entry from an "
            "accepted one; bind it (fail-closed, re-anchor if intentionally reshaped)"
        )
        return r1, r2, r3
    ident = m_res.group(1)
    if not re.search(r"\\b" + re.escape(ident) + r"\\s*==\\s*null", wb):
        r3.append(
            "ArtifactArchiver.kt: the transform result is never compared to null — a "
            "rejected entry is not dropped"
        )
        return r1, r2, r3
    null_pos = wb.find(ident + " == null")
    next_continue = wb.find("continue", null_pos)
    next_write = wb.find("putNextEntry", null_pos)
    if next_continue < 0 or (0 <= next_write < next_continue):
        r3.append(
            "ArtifactArchiver.kt: a null transform result does not `continue` before "
            "`putNextEntry` — the rejected entry is archived anyway; it must fail closed"
        )
    if "droppedSink" not in wb:
        r3.append(
            "ArtifactArchiver.kt: a dropped entry is not reported to `droppedSink` — the "
            "caller's file/skip accounting silently overcounts the archive"
        )
    return r1, r2, r3

'''

RUN_READ_FROM = '''        scope_raw = read(root, SCOPE_REL)
        backup_raw = read(root, CONFIG_BACKUP_REL)
'''
RUN_READ_TO = '''        scope_raw = read(root, SCOPE_REL)
        backup_raw = read(root, CONFIG_BACKUP_REL)
        archiver_raw = read(root, ARTIFACT_ARCHIVER_REL)
'''

RUN_CALL_FROM = '''        violations += check_backup_scope(mask_comments(scope_raw))
        violations += check_backup_import(mask_comments(backup_raw), mask(backup_raw))
'''
RUN_CALL_TO = '''        violations += check_backup_scope(mask_comments(scope_raw))
        violations += check_backup_import(mask_comments(backup_raw), mask(backup_raw))
        v_pack, v_rule, v_arch = check_artifact_redaction(
            mask_comments(backup_raw), mask(backup_raw), mask(archiver_raw))
        violations += v_pack + v_rule + v_arch
'''

PASS_FROM = '''        "oauth bridge never archived and never restored%s)"
'''
PASS_TO = '''        "oauth bridge never archived and never restored; mcp servers.json redacted "
        "on a credentials-free export%s)"
'''

FIXTURES = '''
# -- [dv20-a1 A-01] artifact-face fixtures -----------------------------------

GOOD_PACK = """
object ConfigBackup {
    suspend fun buildSections(artifactRoots: List<java.io.File>?, includeSecrets: Boolean) {
        if (artifactRoots != null && artifactRoots.isNotEmpty()) {
            for (root in artifactRoots) {
                val isMcpRoot = root.name == "mcp-servers"
                val prefix = if (isMcpRoot) "mcp" else "shared"
                val transform =
                    if (isMcpRoot && !includeSecrets) ::redactMcpArtifactEntry else null
                val dropped = ArrayList<String>()
                totalBytes += ArtifactArchiver.writeEntries(
                    zos, root, sel.files, prefix + "/", transform, dropped,
                )
                fileCount += sel.files.size - dropped.size
                artifactSkipped += dropped.size
            }
        }
    }

    internal fun redactMcpArtifactEntry(relPath: String, bytes: ByteArray): ByteArray? {
        if (relPath != "servers.json") return bytes
        return try {
            val obj = org.json.JSONObject(String(bytes))
            stripExportedMcpSecrets(obj)
            obj.toString(2).toByteArray()
        } catch (t: Throwable) {
            null
        }
    }

    internal fun stripExportedMcpSecrets(obj: org.json.JSONObject) { }
}
"""

BAD_PACK_NO_REDACTOR = GOOD_PACK.replace(
    "if (isMcpRoot && !includeSecrets) ::redactMcpArtifactEntry else null",
    "null")
BAD_PACK_UNGATED = GOOD_PACK.replace(
    "if (isMcpRoot && !includeSecrets) ::redactMcpArtifactEntry else null",
    "if (isMcpRoot) ::redactMcpArtifactEntry else null")
BAD_PACK_NO_STRIP = GOOD_PACK.replace("            stripExportedMcpSecrets(obj)\\n", "")
BAD_PACK_INLINE_RULE = GOOD_PACK.replace(
    "            stripExportedMcpSecrets(obj)\\n",
    '            val section = obj.optJSONObject("headers")\\n'
    '            if (section != null) for (k in section.keys()) section.put(k, "")\\n')
BAD_PACK_NO_FAILCLOSED = GOOD_PACK.replace(
    "        } catch (t: Throwable) {\\n            null\\n        }",
    "        } catch (t: Throwable) {\\n            bytes\\n        }")
BAD_PACK_NO_ROOT = GOOD_PACK.replace('val isMcpRoot = root.name == "mcp-servers"', 'val isMcp = 1')

GOOD_ARCHIVER = """
object ArtifactArchiver {
    fun writeEntries(
        zos: java.util.zip.ZipOutputStream,
        root: java.io.File,
        files: List<ArtifactBackupScope.SelectedFile>,
        entryPrefix: String = "",
        transform: ((String, ByteArray) -> ByteArray?)? = null,
        droppedSink: MutableList<String>? = null,
    ): Long {
        var totalBytes = 0L
        for (f in files) {
            val src = java.io.File(root, f.relPath)
            if (!src.isFile) continue
            var bytes = src.readBytes()
            if (transform != null) {
                val transformed = transform(f.relPath, bytes)
                if (transformed == null) {
                    droppedSink?.add(f.relPath)
                    continue
                }
                bytes = transformed
            }
            zos.putNextEntry(java.util.zip.ZipEntry(entryPrefix + f.relPath))
            zos.write(bytes)
            zos.closeEntry()
            totalBytes += bytes.size
        }
        return totalBytes
    }
}
"""

ARCHIVER_DROP = """                if (transformed == null) {
                    droppedSink?.add(f.relPath)
                    continue
                }
"""
BAD_ARCHIVER_NO_NULL_CHECK = GOOD_ARCHIVER.replace(ARCHIVER_DROP, "")
BAD_ARCHIVER_NO_SINK = GOOD_ARCHIVER.replace("                    droppedSink?.add(f.relPath)\\n", "")
BAD_ARCHIVER_NO_TRANSFORM = GOOD_ARCHIVER.replace(
    """            if (transform != null) {
                val transformed = transform(f.relPath, bytes)
                if (transformed == null) {
                    droppedSink?.add(f.relPath)
                    continue
                }
                bytes = transformed
            }
""", "")
BAD_ARCHIVER_NO_FUN = GOOD_ARCHIVER.replace("fun writeEntries(", "fun writeEntriesX(")

'''

ARMS = '''
    # [dv20-a1 A-01] artifact-face arms: each fixture must red exactly one group
    def expect_only(name, groups, gi, needle):
        if not any(needle in x for x in groups[gi]):
            failures.append(
                "%s must trip group %d of 3 with '%s', got %s"
                % (name, gi + 1, needle, [len(g) for g in groups]))
            return
        for j, g in enumerate(groups):
            if j != gi and g:
                failures.append("%s left sibling group %d dirty: %s" % (name, j + 1, g))

    def art(fixture=None, archiver=None):
        p = fixture if fixture is not None else GOOD_PACK
        a = archiver if archiver is not None else GOOD_ARCHIVER
        return check_artifact_redaction(mask_comments(p), mask(p), mask(a))

    p_good, r_good, a_good = art()
    expect_clean("compliant credential-free pack fixture", p_good)
    expect_clean("compliant redactor fixture", r_good)
    expect_clean("compliant archiver fixture", a_good)

    expect_only("pack without a redactor fixture", art(BAD_PACK_NO_REDACTOR), 0, "is handed no")
    expect_only("ungated redactor fixture", art(BAD_PACK_UNGATED), 0, "not gated on")
    expect_only("pack anchor drift fixture", art(BAD_PACK_NO_ROOT), 0, "re-anchor this gate")
    expect_only("redactor without the Stage 5 strip fixture", art(BAD_PACK_NO_STRIP), 1,
                "does not call")
    expect_only("redactor with a second rule fixture", art(BAD_PACK_INLINE_RULE), 1,
                "blanks secret values itself")
    expect_only("redactor that cannot fail closed fixture", art(BAD_PACK_NO_FAILCLOSED), 1,
                "parse-failure path")
    expect_only("archiver that never drops fixture", art(archiver=BAD_ARCHIVER_NO_NULL_CHECK), 2,
                "never compared to null")
    expect_only("archiver that does not report a drop fixture", art(archiver=BAD_ARCHIVER_NO_SINK), 2,
                "droppedSink")
    expect_only("archiver that ignores its transform fixture",
                art(archiver=BAD_ARCHIVER_NO_TRANSFORM), 2, "never applies its transform")
    expect_only("archiver anchor drift fixture", art(archiver=BAD_ARCHIVER_NO_FUN), 2,
                "re-anchor this gate")

    print("selftest dv20-a1 artifact arms (no-redactor/ungated/pack-rot/no-strip/second-rule/"
          "no-fail-closed/drop-removed/no-sink/ignores-transform/archiver-rot) -> "
          "%d/%d/%d/%d/%d/%d/%d/%d/%d/%d violation(s)"
          % (len(art(BAD_PACK_NO_REDACTOR)[0]), len(art(BAD_PACK_UNGATED)[0]),
             len(art(BAD_PACK_NO_ROOT)[0]), len(art(BAD_PACK_NO_STRIP)[1]),
             len(art(BAD_PACK_INLINE_RULE)[1]), len(art(BAD_PACK_NO_FAILCLOSED)[1]),
             len(art(archiver=BAD_ARCHIVER_NO_NULL_CHECK)[2]),
             len(art(archiver=BAD_ARCHIVER_NO_SINK)[2]),
             len(art(archiver=BAD_ARCHIVER_NO_TRANSFORM)[2]),
             len(art(archiver=BAD_ARCHIVER_NO_FUN)[2])))

'''

TAIL_ANCHOR = '''    if failures:
        for item in failures:
            print("SELFTEST FAIL " + item)
        return 1
    print(SELFTEST_MARK)
'''


def apply(src, edits):
    for old, new in edits:
        n = src.count(old)
        if n != 1:
            raise SystemExit("anchor count %d (expected 1) for: %r" % (n, old[:70]))
        src = src.replace(old, new)
    return src


def main():
    with io.open(GATE, "r", encoding="utf-8") as fh:
        src = fh.read()
    if MARKER in src:
        print("already patched (marker present) — no-op")
        return 0
    src = apply(src, [
        (CONSTS_ANCHOR, CONSTS_ANCHOR + CONSTS_ADD),
        ("def read(root, rel):", CHECK_FN.lstrip("\n") + "\ndef read(root, rel):"),
        (RUN_READ_FROM, RUN_READ_TO),
        (RUN_CALL_FROM, RUN_CALL_TO),
        (PASS_FROM, PASS_TO),
        ("def selftest():\n    failures = []", FIXTURES.lstrip("\n") + "\ndef selftest():\n    failures = []"),
        (TAIL_ANCHOR, ARMS.lstrip("\n") + "\n" + TAIL_ANCHOR),
    ])
    with io.open(GATE, "w", encoding="utf-8") as fh:
        fh.write(src)
    print("patched %s (%d bytes)" % (os.path.basename(GATE), len(src)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

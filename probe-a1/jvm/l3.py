#!/usr/bin/env python3
"""[dv20-a1] L3 probe: compile and RUN real extracted production code.

Arms
  post  the branch tree as committed
  pre   the same test file against baseline semantics (the predicate falls back
        to persisted-entries-only, the artifact redactor is a pass-through and
        the archiver ignores the hook) — proves the tests WOULD have caught the
        defects
  m1    predicate: the System clause removed
  m2    redactor: the parse failure returns the bytes instead of null
  m3    archiver: a null transform result is written instead of dropped
  m4    redactor: the servers.json guard removed (over-redaction)

Everything is extracted from the real sources by brace matching; nothing is a
re-implementation. The Kotlin test file (probe.L3Probe) is shared by every arm.
"""
import os
import re
import shutil
import subprocess
import sys

JVM = "/var/minis/shared/work/deep-audit-1040/a1/probe-a1/jvm"
REPO = "/var/minis/shared/work/deep-audit-1040/a1"
MAIN = os.path.join(REPO, "src/android/app/src/main/java/com/rikkaminis/app")
LIBS = ":".join([
    "/opt/jvm-libs/junit-4.13.2.jar",
    "/opt/jvm-libs/hamcrest-core-1.3.jar",
    "/opt/jvm-libs/json-20240303.jar",
    "/opt/kotlinc/lib/kotlin-stdlib.jar",
])
KOTLINC = "/opt/kotlinc/bin/kotlinc"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def brace_body(src, needle, expect):
    """Return the whole declaration starting at `needle` through its closing
    brace (naive brace matching; the caller asserts `expect` is inside)."""
    i = src.index(needle)
    start = src.rfind("\n", 0, i) + 1
    j = src.index("{", i)
    depth, k = 0, j
    while True:
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    out = src[start:k + 1]
    assert expect in out, "extraction of %r lost %r" % (needle, expect)
    return out


def expr_body(src, needle, expect):
    """Return an expression-bodied declaration (signature + continuation)."""
    i = src.index(needle)
    start = src.rfind("\n", 0, i) + 1
    out, depth = "", 0
    for line in src[start:].split("\n"):
        out += line + "\n"
        depth += line.count("(") - line.count(")")
        if depth == 0 and not line.rstrip().endswith(("=", "||", "&&", "+")):
            break
    out = out.rstrip("\n")
    assert expect in out, "extraction of %r lost %r" % (needle, expect)
    return out


VOICE_IDS = re.search(r'const val BUILTIN_PROVIDER_ID = "([^"]+)"',
                      read(os.path.join(MAIN, "data/model/VoiceModality.kt"))).group(1)

ARCHIVER_SRC = read(os.path.join(MAIN, "backup/ArtifactArchiver.kt"))
VOICE_SRC = read(os.path.join(MAIN, "data/model/SystemVoiceEntries.kt"))
BACKUP_SRC = read(os.path.join(MAIN, "backup/ConfigBackup.kt"))

HOOK_BLOCK = """            var bytes = src.readBytes() // bounded by MAX_FILE_BYTES per file
            if (transform != null) {
                val transformed = transform(f.relPath, bytes)
                if (transformed == null) {
                    droppedSink?.add(f.relPath)
                    continue
                }
                bytes = transformed
            }
"""
assert ARCHIVER_SRC.count(HOOK_BLOCK) == 1, "archiver hook block anchor"

IS_SYSTEM = expr_body(VOICE_SRC, "fun isSystemEntryId(id: String)", "startsWith")
IS_KNOWN = expr_body(VOICE_SRC, "fun isKnownGroupMemberId(", "isSystemEntryId(memberId)")
STRIP_EXPORTED = brace_body(BACKUP_SRC, "internal fun stripExportedMcpSecrets(", "mcpServers")
STRIP_VALUES = brace_body(BACKUP_SRC, "internal fun stripMcpSecretValues(", "headers")
REDACT = brace_body(BACKUP_SRC, "internal fun redactMcpArtifactEntry(", "servers.json")


def decl_body(src, needle, expect):
    """Return a multi-line `val` declaration through its closing bracket."""
    i = src.index(needle)
    start = src.rfind("\n", 0, i) + 1
    out, depth = "", 0
    for line in src[start:].split("\n"):
        out += line + "\n"
        depth += (line.count("(") - line.count(")")) + (line.count("[") - line.count("]"))
        if depth == 0 and line.rstrip().endswith((")", "}", "]")):
            break
    out = out.rstrip("\n")
    assert expect in out, "extraction of %r lost %r" % (needle, expect)
    return out


# the strip helpers lean on these three members of ConfigBackup — extract them
# rather than re-typing regexes/keys in the harness
SECRET_ARG_RE = decl_body(BACKUP_SRC, "private val SECRET_ARG_RE", "Regex")
QUERY_KEYS = decl_body(BACKUP_SRC, "private val SENSITIVE_QUERY_KEYS", "setOf")
URL_QUERY_FN = brace_body(BACKUP_SRC, "private fun redactUrlQuery(", "SENSITIVE_QUERY_KEYS")

STUBS = '''package probe

object AppLogger {
    fun warning(category: String, message: String) {
        println("[WARN] " + category + ": " + message)
    }
}

object SystemVoiceIds {
    const val BUILTIN_PROVIDER_ID = "%s"
}

object ArtifactBackupScope {
    data class SelectedFile(val relPath: String)
}
''' % VOICE_IDS

VOICE_SLICE = '''package probe

object VoiceSlice {
%s

%s
}
''' % (IS_SYSTEM, IS_KNOWN)

REDACTOR_SLICE = '''package probe

import org.json.JSONObject

object RedactorSlice {
    private const val TAG = "ConfigBackup"

%s

%s

%s

%s

%s
}
''' % (STRIP_EXPORTED, STRIP_VALUES, REDACT, SECRET_ARG_RE + "\n\n" + QUERY_KEYS, URL_QUERY_FN)

BASELINE_REDACT = '''    internal fun redactMcpArtifactEntry(relPath: String, bytes: ByteArray): ByteArray? {
        return bytes
    }'''

ARMS = {
    "post": {},
    "pre": {
        "voice": (" || isSystemEntryId(memberId)", ""),
        "redactor": (REDACT, BASELINE_REDACT),
        "archiver": (HOOK_BLOCK, "            val bytes = src.readBytes() // bounded by MAX_FILE_BYTES per file\n"),
    },
    "m1": {"voice": (" || isSystemEntryId(memberId)", "")},
    "m2": {"redactor": ('            AppLogger.warning(TAG, "mcp artifact redaction failed for $relPath: ${t.message}")\n            null',
                        '            AppLogger.warning(TAG, "mcp artifact redaction failed for $relPath: ${t.message}")\n            bytes')},
    "m3": {"archiver": ("                if (transformed == null) {\n                    droppedSink?.add(f.relPath)\n                    continue\n                }\n                bytes = transformed",
                        "                bytes = transformed ?: bytes")},
    "m4": {"redactor": ('        if (relPath.substringAfterLast(\'/\') != "servers.json") return bytes\n', "")},
}

EXPECT_RED = {
    "post": set(),
    "pre": {"t1SentinelIdsAreKnownGroupMembers", "t2CredentialsNeverReachTheArchive",
            "t3UnparseableServerConfigDropsTheEntry"},
    "m1": {"t1SentinelIdsAreKnownGroupMembers"},
    "m2": {"t3UnparseableServerConfigDropsTheEntry"},
    "m3": {"t3UnparseableServerConfigDropsTheEntry"},
    "m4": {"t4NonServerArtifactsPassVerbatim"},
}


def emit(arm, spec):
    root = os.path.join(JVM, "gen", arm)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "src"))
    os.makedirs(os.path.join(root, "out"))

    archiver, voice, redactor = ARCHIVER_SRC, VOICE_SLICE, REDACTOR_SLICE
    for kind, (old, new) in spec.items():
        target = {"voice": "voice", "redactor": "redactor", "archiver": "archiver"}[kind]
        text = {"voice": voice, "redactor": redactor, "archiver": archiver}[target]
        assert text.count(old) == 1, "%s/%s anchor count %d" % (arm, kind, text.count(old))
        text = text.replace(old, new, 1)
        archiver, voice, redactor = (text if target == "archiver" else archiver,
                                     text if target == "voice" else voice,
                                     text if target == "redactor" else redactor)

    files = {
        "Stubs.kt": STUBS,
        "ArtifactArchiver.kt": archiver.replace("package com.rikkaminis.app.backup", "package probe", 1),
        "VoiceSlice.kt": voice,
        "RedactorSlice.kt": redactor,
        "L3Probe.kt": read(os.path.join(JVM, "L3Probe.kt")),
    }
    for name, text in files.items():
        with open(os.path.join(root, "src", name), "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


def run_arm(arm, root):
    log = os.path.join(JVM, "logs")
    os.makedirs(log, exist_ok=True)
    cp = os.path.join(root, "out")
    comp = subprocess.run([KOTLINC, "-nowarn", "-cp", LIBS, "-d", cp]
                          + sorted(os.path.join(root, "src", f)
                                   for f in os.listdir(os.path.join(root, "src"))),
                          capture_output=True, text=True)
    with open(os.path.join(log, "%s-compile.log" % arm), "w") as fh:
        fh.write(comp.stdout + comp.stderr)
    if comp.returncode != 0:
        print("[%s] COMPILE FAILED — see logs/%s-compile.log" % (arm, arm))
        print("\n".join((comp.stdout + comp.stderr).split("\n")[:12]))
        return None
    proc = subprocess.run(["java", "-cp", cp + ":" + LIBS, "org.junit.runner.JUnitCore",
                           "probe.L3Probe"], capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    with open(os.path.join(log, "%s-run.log" % arm), "w") as fh:
        fh.write(out)
    if "OK (5 tests)" in out:
        return set()
    red = set(re.findall(r"^\d+\) (\w+)\(probe\.L3Probe\)", out, re.M))
    if not red:
        print("[%s] UNPARSEABLE RUN OUTPUT:\n%s" % (arm, out[:800]))
        return None
    return red


def main():
    bad = []
    for arm, spec in ARMS.items():
        root = emit(arm, spec)
        red = run_arm(arm, root)
        if red is None:
            bad.append(arm)
            continue
        want = EXPECT_RED[arm]
        mark = "OK " if red == want else "MISMATCH"
        if red != want:
            bad.append(arm)
        print("[%s] %s red=%s want=%s" % (arm, mark, sorted(red) or "-", sorted(want) or "-"))
    print()
    if bad:
        print("L3 JUDGE: FAIL (%s)" % ", ".join(bad))
        return 1
    print("L3 JUDGE: ALL ARMS MATCH (post green, pre precisely red, 4 mutations precise)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

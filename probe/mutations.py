#!/usr/bin/env python3
"""[da1020-C] Mutation negative controls — LAYERED driver: python ONLY does
byte edits + backup + state json. The gate and JVM runs are orchestrated by
the shell (probe/run-mutation.sh), because an in-python subprocess of kotlinc
crashed with PRoot waitpid Errno 38 (S28 variant) leaving the tree in a
mutated state — this driver dies BEFORE any subprocess is spawned, so the
tree can only be left mutated by an explicit restore omission, which the
shell wrapper always performs (restore is idempotent and runs even on gate
failure)."""
import hashlib
import json
import sys
from pathlib import Path

REPO = Path("/var/minis/shared/work/deep-audit-1020/repo-c/repo")
BU = REPO / "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/BrowserUseOffloadHandler.kt"
FS = REPO / "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/FastioOffloadHandler.kt"
BKDIR = REPO / "probe/backup"
STATE = REPO / "probe/mutation-state.json"

MUTATIONS = {
    "M1": (BU, [
        ("            val joined = resolveMaybeRelative(cookiesFile, cwd)\n"
         "            val host = PRootKernel.resolvePathForSession(sessionId, joined, app)",
         "            val host = PRootKernel.resolvePathForSession(sessionId, cookiesFile, app)"),
    ]),
    "M2": (FS, [
        ('        val isTarCommand = firstSubcommandToken(rawArgv) == "tar"',
         '        val isTarCommand = rawArgv.firstOrNull() == "tar"'),
        ('            if (firstSubcommandToken(argv) != "tar") return argv',
         '            if (argv.firstOrNull() != "tar") return argv'),
    ]),
    "M3": (FS, [
        ('            } else {\n'
         '                setOf("recursive", "force", "ignore-case")\n'
         '            }',
         '            } else {\n'
         '                setOf("recursive", "force", "ignore-case", "create", "extract", "gzip", "verbose")\n'
         '            }'),
    ]),
    "M4": (BU, [
        ("            val tokens = resolveMaybeRelativeAll(",
         '            val tokens: List<String> = raw.split(Regex("[,\\\\s]+")).map { it.trim() }'
         '.filter { it.isNotEmpty() }.let {'),
        ('            if (tokens.isNotEmpty()) obj.put("paths", JSONArray(tokens))',
         '            }\n            if (tokens.isNotEmpty()) obj.put("paths", JSONArray(tokens))'),
    ]),
}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_state() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text())
    return {}


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=1))


def do_backup(mid: str) -> int:
    path, _ = MUTATIONS[mid]
    BKDIR.mkdir(parents=True, exist_ok=True)
    bak = BKDIR / f"{path.name}.{mid}.bak"
    bak.write_bytes(path.read_bytes())
    print(f"BACKUP-OK {mid} -> {bak.name} sha={sha(path)}")
    return 0


def do_apply(mid: str) -> int:
    path, edits = MUTATIONS[mid]
    pre = sha(path)
    s = path.read_text(encoding="utf-8")
    for old, new in edits:
        if s.count(old) != 1:
            print(f"APPLY-FAIL: anchor not unique for {mid}: {old[:60]!r}")
            return 2
        s = s.replace(old, new)
    path.write_text(s, encoding="utf-8")
    st = load_state()
    st[mid] = {"applied_sha": sha(path), "pre_sha": pre}
    save_state(st)
    print(f"APPLY-OK {mid} sha={st[mid]['applied_sha']}")
    return 0


def do_restore(mid: str) -> int:
    path, _ = MUTATIONS[mid]
    bak = BKDIR / f"{path.name}.{mid}.bak"
    if not bak.is_file():
        print(f"RESTORE-FAIL: no backup for {mid}")
        return 2
    path.write_bytes(bak.read_bytes())
    st = load_state()
    ok = sha(path) == st.get(mid, {}).get("pre_sha")
    st[mid]["restored_exact"] = ok
    save_state(st)
    print(f"RESTORE-{'OK' if ok else 'MISMATCH'} {mid}")
    return 0 if ok else 2


def do_status() -> int:
    st = load_state()
    print(json.dumps(st, indent=1))
    return 0


def main() -> int:
    cmd = sys.argv[1]
    if cmd == "backup":
        return do_backup(sys.argv[2])
    if cmd == "apply":
        return do_apply(sys.argv[2])
    if cmd == "restore":
        return do_restore(sys.argv[2])
    if cmd == "status":
        return do_status()
    print(f"usage: {sys.argv[0]} backup|apply|restore <mid> | status")
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""FIX-A mutation negative controls (deep-audit-1020).

Byte-level mutation driver: NO git for restore — the pristine bytes are held
(base64) in probe/mut-state.json together with their sha256 and written back
VERBATIM on restore, so an interrupted run can never leave the tree in a
mutated state silently.

Subcommands:
  backup   store the pristine bytes + sha256 of every mutable file
  mut1     drop the forwarded Context at the production call site
  mut2     make the OpenRouter adapter pass null instead of the context
  restore  write the pristine bytes back (self-healing entry point)
  status   verify the working tree matches the pristine sha256 (clean check)
"""
import base64
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "mut-state.json")

FILES = {
    "repo": "src/android/app/src/main/java/com/rikkaminis/app/data/repository/ProviderRepository.kt",
    "adapters": "src/android/app/src/main/java/com/rikkaminis/app/provider/ModelListProviderAdapters.kt",
}

# (file, needle, replacement, description, expect_count)
MUT1 = ("repo",
        "forceRefresh, context)",
        "forceRefresh)",
        "production call site drops the forwarded Context (pre-fix shape)",
        1)
MUT2 = ("adapters",
        "return OpenRouterModelsApi.fetchModels(\n            apiKey,\n            context = context,",
        "return OpenRouterModelsApi.fetchModels(\n            apiKey,\n            context = null,",
        "OpenRouter adapter silently passes null instead of the context",
        1)


def repo_root():
    return os.path.dirname(HERE)


def read(rel):
    with open(os.path.join(repo_root(), rel), "rb") as fh:
        return fh.read()


def write(rel, data):
    with open(os.path.join(repo_root(), rel), "wb") as fh:
        fh.write(data)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def cmd_backup():
    state = {}
    for key, rel in FILES.items():
        data = read(rel)
        state[key] = {"rel": rel, "sha256": sha(data), "bytes": base64.b64encode(data).decode("ascii")}
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=1)
    print("BACKUP_OK %d files" % len(state))
    return 0


def load_state():
    with open(STATE) as fh:
        return json.load(fh)


def cmd_mut(spec):
    state = load_state()
    key, needle, repl, desc, expect = spec
    data = read(FILES[key]).decode("utf-8")
    n = data.count(needle)
    if n != expect:
        print("MUT_FAIL: needle found %d times (expected %d): %s" % (n, expect, desc))
        return 2
    mutated = data.replace(needle, repl, 1)
    if mutated == data:
        print("MUT_FAIL: replacement produced identical bytes")
        return 2
    write(FILES[key], mutated.encode("utf-8"))
    print("MUT_OK %s" % desc)
    return 0


def cmd_restore():
    state = load_state()
    for key, rec in state.items():
        data = base64.b64decode(rec["bytes"])
        if sha(data) != rec["sha256"]:
            print("RESTORE_FAIL: stored bytes for %s do not match the stored sha256" % key)
            return 1
        write(rec["rel"], data)
        if sha(read(rec["rel"])) != rec["sha256"]:
            print("RESTORE_FAIL: written tree for %s does not match the pristine sha256" % key)
            return 1
    print("RESTORED_OK %d files (sha256 verified)" % len(state))
    return 0


def cmd_status():
    state = load_state()
    ok = True
    for key, rec in state.items():
        data = read(rec["rel"])
        match = sha(data) == rec["sha256"]
        print("%s %s sha256=%s" % ("CLEAN" if match else "MUTATED", key, rec["sha256"][:12]))
        ok = ok and match
    return 0 if ok else 1


if __name__ == "__main__":
    cmds = {
        "backup": cmd_backup,
        "mut1": lambda: cmd_mut(MUT1),
        "mut2": lambda: cmd_mut(MUT2),
        "restore": cmd_restore,
        "status": cmd_status,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        print(__doc__)
        sys.exit(2)
    sys.exit(cmds[sys.argv[1]]())

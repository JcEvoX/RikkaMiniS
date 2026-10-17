#!/usr/bin/env python3
"""
Gate: model-entry composite-key guard + canonical id return
(deep-audit-1023 ISSUE-03, branch fix/da1023-entry-key-provider-parity).

Two config entries with the same (providerInstanceId, baseModel.id) collapse
to ONE composite id at the persistence boundary: ProviderConfigMapping writes
`idMap[entry.uuid] = compositeEntryKey(instanceId, modelId)` and the load side
restores `uuid = row.id`. Two such rows therefore share a primary key —
LazyColumn `key = entry.id` throws "Key was already used", and one of the pair
silently vanishes on the next canonicalize round-trip.

The pre-fix code guarded only API-reported entries (`if (!entry.isCustom)`);
custom entries (AddCustomModelScreen / provider.models.add) could mint the
collision. This gate pins the fixed shape:

  Arm 1  addEntry carries the unified guard (decideEntryAdd) and its
         `!entry.isCustom` branch is GONE (the guard is not type-conditional).
  Arm 2  the pure decision helpers exist (same file, JVM-testable).
  Arm 3  ModelsCollection.add returns the canonical persisted id.
  Arm 4  provider.models.add pre-checks duplicates and reports the persisted
         entry (no pre-persist uuid).
  Arm 5  provider.models.update reports the persisted entry (fail-closed on a
         rejected rename instead of claiming success).

Usage: python3 scripts/scan/entry_composite_key_guard_check.py [root]
       python3 scripts/scan/entry_composite_key_guard_check.py --root <root>
       python3 scripts/scan/entry_composite_key_guard_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import argparse
import os
import sys
import tempfile

APP = "src/android/app/src/main/java/com/rikkaminis/app"
PR_REL = APP + "/data/repository/ProviderRepository.kt"
MC_REL = APP + "/config/collections/ModelsCollection.kt"
PM_REL = APP + "/debug/ProviderMutationMethods.kt"

# Arm 1 — unified guard wiring inside addEntry.
ARM1_ANCHORS = (
    "decideEntryAdd(",
    "decision.persistedId",
    "?: compositeEntryKey(",
)
# The type-conditional guard is the pre-fix regression, in its textual form.
ARM1_FORBIDDEN = ("!entry.isCustom",)

# Arm 2 — pure decision helpers must exist (top-level, same file).
ARM2_ANCHORS = (
    "internal fun decideEntryAdd(",
    "internal fun decideCompactPinMigration(",
    "internal fun decideCompactPinCleanup(",
)

# Arm 3 — ModelsCollection.add must return the persisted id, keeping the
# agent-facing AlreadyExists pre-check (the typed error surface).
ARM3_ANCHORS = (
    "return repo.addEntry(",
    "ConfigError.AlreadyExists",
)

# Arm 4 — modelsAdd RPC: duplicate pre-check + persisted-entry response.
ARM4_ANCHORS = (
    "already exists",
    "persistedId",
    "RPCException",
)

# Arm 5 — modelsUpdate RPC: canonical id, fail-closed on rejection.
ARM5_ANCHORS = (
    "repo.updateEntry(updated)",
    "persistedId",
    "?: throw RPCException",
)


def code_only(text):
    """Text with line comments (//…) removed — presence checks must not be
    satisfiable from prose in a comment."""
    return "\n".join(line.split("//")[0] for line in text.split("\n"))


def _balanced(src, open_idx, opener="(", closer=")"):
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == opener:
            depth += 1
        elif src[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def function_text(src, header):
    i = src.find(header)
    if i < 0:
        return None
    brace = src.find("{", i)
    if brace < 0:
        return None
    end = _balanced(src, brace, "{", "}")
    return None if end < 0 else src[i:end]


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def read(rel, root):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def check_anchors(src, rel, header, arm, anchors, what):
    if header is None:
        code = code_only(src)
        base = 0
    else:
        body = function_text(src, header)
        if body is None:
            return [(rel, 0, arm, header + " not found — gate ROT")]
        code = code_only(body)
        base = src.find(header)
    out = []
    for anchor in anchors:
        if anchor not in code:
            out.append((rel, line_of(src, base) if base >= 0 else 0, arm,
                        "%s is missing required code anchor `%s`" % (what, anchor)))
    return out


def check_absent(src, rel, header, arm, forbidden, what):
    body = function_text(src, header)
    if body is None:
        return [(rel, 0, arm, header + " not found — gate ROT")]
    code = code_only(body)
    base = src.find(header)
    out = []
    for token in forbidden:
        if token in code:
            out.append((rel, line_of(src, base), arm,
                        "%s must not be type-conditional: found `%s`" % (what, token)))
    return out


def run_checks(root, rels=None):
    rels = rels or {"pr": PR_REL, "mc": MC_REL, "pm": PM_REL}
    srcs = {k: read(rel, root) for k, rel in rels.items()}
    missing = [(rel, 0, "?", "file not found — gate ROT")
               for k, rel in rels.items() if srcs[k] is None]
    if missing:
        return missing
    checks = []
    checks += check_anchors(srcs["pr"], rels["pr"], "fun addEntry(", "1",
                            ARM1_ANCHORS, "addEntry")
    checks += check_absent(srcs["pr"], rels["pr"], "fun addEntry(", "1",
                           ARM1_FORBIDDEN, "addEntry guard")
    checks += check_anchors(srcs["pr"], rels["pr"], None, "2",
                            ARM2_ANCHORS, "ProviderRepository helpers")
    checks += check_anchors(srcs["mc"], rels["mc"], "override fun add(", "3",
                            ARM3_ANCHORS, "ModelsCollection.add")
    checks += check_anchors(srcs["pm"], rels["pm"], "fun modelsAdd(", "4",
                            ARM4_ANCHORS, "modelsAdd RPC")
    checks += check_anchors(srcs["pm"], rels["pm"], "fun modelsUpdate(", "5",
                            ARM5_ANCHORS, "modelsUpdate RPC")
    return checks


# ----------------------------------------------------------------------
# Selftest fixtures — minimal Kotlin-shaped files exercising every arm.
# ----------------------------------------------------------------------
FIX_PR_OK = """fun addEntry(entry: ModelEntry): String = synchronized(configLock) {
    ensureConfigLoaded()
    val config = mutationSnapshot(_config.value)
    val decision = decideEntryAdd(entry.providerInstanceId, entry.baseModel.id, existing)
    if (!decision.allowed) {
        return@synchronized decision.persistedId
    }
    config.modelEntries.add(entry)
    saveConfig(config)
    _config.value.modelEntries.firstOrNull { it.id == entry.id }?.id
        ?: compositeEntryKey(entry.providerInstanceId, entry.baseModel.id)
}

internal fun decideEntryAdd(a: String, b: String, c: List<Pair<String, String>>): EntryAddDecision = TODO()

internal fun decideCompactPinMigration(p: String, o: String, n: String): String? = TODO()

internal fun decideCompactPinCleanup(p: String, r: Collection<String>): Boolean = TODO()
"""

FIX_PR_BAD_TYPECONDITIONAL = """fun addEntry(entry: ModelEntry) = synchronized(configLock) {
    ensureConfigLoaded()
    val config = mutationSnapshot(_config.value)
    if (!entry.isCustom) {
        val exists = config.modelEntries.any { it.baseModel.id == entry.baseModel.id }
        if (exists) return@synchronized
    }
    config.modelEntries.add(entry)
    saveConfig(config)
}

internal fun decideEntryAdd(a: String, b: String, c: List<Pair<String, String>>): EntryAddDecision = TODO()

internal fun decideCompactPinMigration(p: String, o: String, n: String): String? = TODO()

internal fun decideCompactPinCleanup(p: String, r: Collection<String>): Boolean = TODO()
"""

FIX_MC_OK = """override fun add(payload: ConfigValue): String {
    val duplicate = repo.config.value.modelEntries.firstOrNull { it.baseModel.id == modelIdStr }
    if (duplicate != null) {
        throw ConfigError.AlreadyExists("Model already exists")
    }
    val entry = ModelEntry(baseModel = model)
    return repo.addEntry(entry)
}
"""

FIX_MC_BAD_TEMPUUID = """override fun add(payload: ConfigValue): String {
    val entry = ModelEntry(baseModel = model)
    repo.addEntry(entry)
    return entry.id
}
"""

FIX_PM_OK = """fun modelsAdd(context: Context, params: JSONObject): JSONObject {
    val entry = ModelEntry(baseModel = model)
    if (repo.config.value.modelEntries.any { it.baseModel.id == modelId }) {
        throw RPCException(-32602, "model already exists")
    }
    val persistedId = repo.addEntry(entry)
    val persisted = repo.config.value.modelEntries.firstOrNull { it.id == persistedId } ?: entry
    return entryToJson(persisted)
}

fun modelsUpdate(context: Context, params: JSONObject): JSONObject {
    val updated = current.copy(baseModel = newModel)
    val persistedId = repo.updateEntry(updated)
        ?: throw RPCException(-32602, "Entry update rejected")
    val persisted = repo.config.value.modelEntries.firstOrNull { it.id == persistedId } ?: updated
    return entryToJson(persisted)
}
"""

FIX_PM_BAD_STALE = """fun modelsAdd(context: Context, params: JSONObject): JSONObject {
    val entry = ModelEntry(baseModel = model)
    repo.addEntry(entry)
    return entryToJson(entry)
}

fun modelsUpdate(context: Context, params: JSONObject): JSONObject {
    val updated = current.copy(baseModel = newModel)
    repo.updateEntry(updated)
    return entryToJson(updated)
}
"""


def _report(checks, fixture_name, label):
    if not checks:
        print("  [OK]   %s (%s)" % (fixture_name, label))
        return True
    for rel, line, arm, msg in checks:
        print("  [FAIL] %s arm %s line %s: %s" % (fixture_name, arm, line, msg))
    return False


def selftest():
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        rels = {"pr": "pr.kt", "mc": "mc.kt", "pm": "pm.kt"}

        def write(name, text):
            with open(os.path.join(tmp, rels[name]), "w", encoding="utf-8") as f:
                f.write(text)

        write("pr", FIX_PR_OK)
        write("mc", FIX_MC_OK)
        write("pm", FIX_PM_OK)
        ok &= _report(run_checks(tmp, rels), "fixed", "all arms clean")

        # Dirty 1: type-conditional guard (the pre-fix shape).
        write("pr", FIX_PR_BAD_TYPECONDITIONAL)
        checks = run_checks(tmp, rels)
        got = [c for c in checks if c[2] == "1"]
        if not any("type-conditional" in c[3] for c in got):
            print("  [FAIL] typec-conditional fixture did not trip the forbidden check")
            ok = False
        else:
            print("  [OK]   typec-conditional fixture trips arm 1 (%d findings)" % len(got))
        write("pr", FIX_PR_OK)

        # Dirty 2: ModelsCollection returns the pre-persist uuid.
        write("mc", FIX_MC_BAD_TEMPUUID)
        checks = run_checks(tmp, rels)
        got = [c for c in checks if c[2] == "3"]
        if not got:
            print("  [FAIL] temp-uuid fixture did not trip arm 3")
            ok = False
        else:
            print("  [OK]   temp-uuid fixture trips arm 3 (%d findings)" % len(got))
        write("mc", FIX_MC_OK)

        # Dirty 3: RPC surfaces report the pre-persist copy.
        write("pm", FIX_PM_BAD_STALE)
        checks = run_checks(tmp, rels)
        got = [c for c in checks if c[2] in ("4", "5")]
        if not got:
            print("  [FAIL] stale-response fixture did not trip arms 4/5")
            ok = False
        else:
            print("  [OK]   stale-response fixture trips arms 4/5 (%d findings)" % len(got))
        write("pm", FIX_PM_OK)

        # ROT control: a repo without the target files must fail loudly.
        with tempfile.TemporaryDirectory() as empty:
            rot = run_checks(empty, rels)
            if not rot:
                print("  [FAIL] ROT control: missing files produced no finding")
                ok = False
            else:
                print("  [OK]   ROT control: missing files reported (%d findings)" % len(rot))

    print("ENTRY_COMPOSITE_KEY_SELFTEST=%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("root", nargs="?", default=None)
    ap.add_argument("--root", dest="root_opt", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    root = args.root_opt or args.root
    if not root:
        ap.print_usage(sys.stderr)
        return 2
    if not os.path.isdir(root):
        print("root not found: %s" % root, file=sys.stderr)
        return 2

    checks = run_checks(root)
    if not checks:
        print("entry_composite_key_guard_check: OK (5 arms clean)")
        return 0
    for rel, line, arm, msg in checks:
        print("%s:%s [arm %s] %s" % (rel, line, arm, msg))
    print("entry_composite_key_guard_check: %d violation(s)" % len(checks))
    return 1


if __name__ == "__main__":
    sys.exit(main())

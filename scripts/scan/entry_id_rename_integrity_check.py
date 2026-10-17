#!/usr/bin/env python3
"""
Gate: custom model-id rename integrity (deep-audit-1022 ISSUE-05,
branch fix/da1022-entry-id-integrity).

Renaming a custom model's id changes the composite entry key
("{instanceId}/{modelId}") — the persisted row primary key and the foreign
key every downstream consumer (lastUsed prefs / session bindings /
compact pin / group members) compares by string equality. The report's
original claim ("groups / AgentLoop entries silently drop after restart")
was wrong — toSnapshot's idMap translation + the load-side uuid=row.id
keep config-internal references self-consistent. The real cracks are:

  1. updateEntry did NO id normalization: the renamed entry kept the old
     composite uuid while the persisted row moved to the new key — and
     KEY_LAST_USED_ENTRY (prefs) still held the OLD key, so
     lastUsedVisibleEntry() stopped resolving "the model I was just using".
  2. NO blank-id interception anywhere on the rename path
     (ModelEntryDetailScreen saved `modelId` verbatim; the modelsUpdate RPC
     copied an untrimmed param) — a whitespace id mints the dangling key
     "instanceId/" and orphans every downstream lookup.
  3. A rename colliding with another entry of the same instance would make
     syncAllIncremental's upsertEntries REPLACE the existing row (same
     primary key) — the duplicate entry silently vanishes on next load.

Usage: python3 scripts/scan/entry_id_rename_integrity_check.py [root]
       python3 scripts/scan/entry_id_rename_integrity_check.py --root <root>
       python3 scripts/scan/entry_id_rename_integrity_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import argparse
import os
import sys
import tempfile

APP = "src/android/app/src/main/java/com/rikkaminis/app"
PR_REL = APP + "/data/repository/ProviderRepository.kt"
PU_REL = APP + "/data/repository/ProviderRepositoryUtils.kt"
ME_REL = APP + "/ui/settings/ModelEntryDetailScreen.kt"
PM_REL = APP + "/debug/ProviderMutationMethods.kt"

# Arm 1 — updateEntry wiring. Code anchors only (comments are stripped):
# the decideEntryRename call, the fail-closed bail, the two reference-sync
# rewrites and the KEY_LAST_USED_ENTRY rewrite must all be live code.
ARM1_ANCHORS = (
    "decideEntryRename(",
    "return@synchronized",
    "memberEntryIds.replaceAll",
    "agentLoopModelEntryIds.replaceAll",
    "lastUsedEntryId = newUuid",
)

# Arm 2 — decision core must reject blank ids and colliding renames, and
# carry the old→new remap on an allowed rename.
ARM2_ANCHORS = (
    "updatedModelId.isBlank()",
    "others.any",
    "oldKey = existingUuid",
    "newKey = newKey",
)

# Arm 3 — the detail screen must trim before persisting and gate blank ids.
ARM3_ANCHORS = (
    "baseModel.copy(id = modelId.trim())",
    "enabled = !entry.isCustom || modelId.isNotBlank()",
)

# Arm 4 — the modelsUpdate RPC must trim and reject blank modelId params.
ARM4_ANCHORS = (
    'params.optString("modelId", current.baseModel.id).trim()',
    '"modelId must not be blank"',
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


def run_checks(root, rels=None):
    rels = rels or {"pr": PR_REL, "pu": PU_REL, "me": ME_REL, "pm": PM_REL}
    srcs = {k: read(rel, root) for k, rel in rels.items()}
    missing = [(rel, 0, "?", "file not found — gate ROT")
               for k, rel in rels.items() if srcs[k] is None]
    if missing:
        return missing
    checks = []
    checks += check_anchors(srcs["pr"], rels["pr"], "fun updateEntry(", "1",
                            ARM1_ANCHORS, "updateEntry")
    checks += check_anchors(srcs["pu"], rels["pu"], "fun decideEntryRename(", "2",
                            ARM2_ANCHORS, "decideEntryRename")
    checks += check_anchors(srcs["me"], rels["me"], None, "3",
                            ARM3_ANCHORS, "ModelEntryDetailScreen")
    checks += check_anchors(srcs["pm"], rels["pm"], "fun modelsUpdate(", "4",
                            ARM4_ANCHORS, "modelsUpdate RPC")
    return checks


# ----------------------------------------------------------------------
# Selftest fixtures — minimal Kotlin-shaped files exercising every arm.
# ----------------------------------------------------------------------
FIX_PR_OK = """fun updateEntry(entry: ModelEntry) = synchronized(configLock) {
    ensureConfigLoaded()
    val config = mutationSnapshot(_config.value)
    val idx = config.modelEntries.indexOfFirst { it.id == entry.id }
    if (idx >= 0) {
        val existing = config.modelEntries[idx]
        val decision = decideEntryRename(
            existingUuid = existing.id,
            updatedInstanceId = entry.providerInstanceId,
            updatedModelId = entry.baseModel.id,
            others = config.modelEntries.map { it.providerInstanceId to it.baseModel.id },
        )
        if (!decision.allowed) {
            return@synchronized
        }
        val renamed = decision.newKey != null
        val newUuid = decision.newKey ?: existing.id
        config.modelEntries[idx] = entry.copy(uuid = newUuid, userModifiedAt = 1L)
        if (renamed) {
            val oldKey = decision.oldKey
            config.modelGroups.forEach { group ->
                group.memberEntryIds.replaceAll { id -> if (id == oldKey) newUuid else id }
            }
            config.agentLoopModelEntryIds.replaceAll { id -> if (id == oldKey) newUuid else id }
            if (lastUsedEntryId == oldKey) lastUsedEntryId = newUuid
        }
        saveConfig(config)
    }
}
"""

FIX_PR_BAD_NOSYNC = """fun updateEntry(entry: ModelEntry) = synchronized(configLock) {
    ensureConfigLoaded()
    val config = mutationSnapshot(_config.value)
    val idx = config.modelEntries.indexOfFirst { it.id == entry.id }
    if (idx >= 0) {
        val decision = decideEntryRename(existing.id, entry.providerInstanceId, entry.baseModel.id, others)
        if (!decision.allowed) {
            return@synchronized
        }
        config.modelEntries[idx] = entry.copy(userModifiedAt = 1L)
        saveConfig(config)
    }
}
"""

FIX_PR_BAD_NOGATE = """fun updateEntry(entry: ModelEntry) = synchronized(configLock) {
    ensureConfigLoaded()
    val config = mutationSnapshot(_config.value)
    val idx = config.modelEntries.indexOfFirst { it.id == entry.id }
    if (idx >= 0) {
        val decision = decideEntryRename(existing.id, entry.providerInstanceId, entry.baseModel.id, others)
        val newUuid = decision.newKey ?: existing.id
        config.modelEntries[idx] = entry.copy(uuid = newUuid, userModifiedAt = 1L)
        if (decision.newKey != null) {
            config.modelGroups.forEach { group ->
                group.memberEntryIds.replaceAll { id -> if (id == decision.oldKey) newUuid else id }
            }
            config.agentLoopModelEntryIds.replaceAll { id -> if (id == decision.oldKey) newUuid else id }
            if (lastUsedEntryId == decision.oldKey) lastUsedEntryId = newUuid
        }
        saveConfig(config)
    }
}
"""

FIX_PU_OK = """internal fun decideEntryRename(
    existingUuid: String,
    updatedInstanceId: String,
    updatedModelId: String,
    others: List<Pair<String, String>>,
): EntryRenameDecision {
    if (updatedModelId.isBlank()) {
        return EntryRenameDecision(allowed = false, rejectionReason = "blank model id")
    }
    val newKey = "$updatedInstanceId/$updatedModelId"
    if (newKey == existingUuid) return EntryRenameDecision(allowed = true)
    val collides = others.any { it.first == updatedInstanceId && it.second == updatedModelId }
    if (collides) {
        return EntryRenameDecision(allowed = false, rejectionReason = "collision")
    }
    return EntryRenameDecision(allowed = true, oldKey = existingUuid, newKey = newKey)
}
"""

FIX_PU_BAD_NOBLANK = """internal fun decideEntryRename(
    existingUuid: String,
    updatedInstanceId: String,
    updatedModelId: String,
    others: List<Pair<String, String>>,
): EntryRenameDecision {
    val newKey = "$updatedInstanceId/$updatedModelId"
    if (newKey == existingUuid) return EntryRenameDecision(allowed = true)
    val collides = others.any { it.first == updatedInstanceId && it.second == updatedModelId }
    if (collides) {
        return EntryRenameDecision(allowed = false, rejectionReason = "collision")
    }
    return EntryRenameDecision(allowed = true, oldKey = existingUuid, newKey = newKey)
}
"""

FIX_PU_BAD_NOCOLLIDE = """internal fun decideEntryRename(
    existingUuid: String,
    updatedInstanceId: String,
    updatedModelId: String,
    others: List<Pair<String, String>>,
): EntryRenameDecision {
    if (updatedModelId.isBlank()) {
        return EntryRenameDecision(allowed = false, rejectionReason = "blank model id")
    }
    val newKey = "$updatedInstanceId/$updatedModelId"
    if (newKey == existingUuid) return EntryRenameDecision(allowed = true)
    return EntryRenameDecision(allowed = true, oldKey = existingUuid, newKey = newKey)
}
"""

FIX_ME_OK = """@Composable
fun ModelEntryDetailScreen(instanceId: String, entryId: String, providerRepository: ProviderRepository, onBack: () -> Unit) {
    val updated = if (entry.isCustom) {
        entry.copy(baseModel = baseModel.copy(id = modelId.trim()), overrides = newOverrides)
    } else {
        entry.copy(overrides = newOverrides)
    }
    providerRepository.updateEntry(updated)
    onBack()
}

// actions block
MinisButton(
    onClick = { save() },
    enabled = !entry.isCustom || modelId.isNotBlank(),
) { Text("save") }
"""

FIX_ME_BAD_NOTRIM = """@Composable
fun ModelEntryDetailScreen(instanceId: String, entryId: String, providerRepository: ProviderRepository, onBack: () -> Unit) {
    val updated = if (entry.isCustom) {
        entry.copy(baseModel = baseModel.copy(id = modelId), overrides = newOverrides)
    } else {
        entry.copy(overrides = newOverrides)
    }
    providerRepository.updateEntry(updated)
    onBack()
}

MinisButton(
    onClick = { save() },
    enabled = !entry.isCustom || modelId.isNotBlank(),
) { Text("save") }
"""

FIX_ME_BAD_NOGATE = """@Composable
fun ModelEntryDetailScreen(instanceId: String, entryId: String, providerRepository: ProviderRepository, onBack: () -> Unit) {
    val updated = if (entry.isCustom) {
        entry.copy(baseModel = baseModel.copy(id = modelId.trim()), overrides = newOverrides)
    } else {
        entry.copy(overrides = newOverrides)
    }
    providerRepository.updateEntry(updated)
    onBack()
}

MinisButton(onClick = { save() }) { Text("save") }
"""

FIX_PM_OK = """fun modelsUpdate(context: Context, params: JSONObject): JSONObject {
    val newModel = if (params.has("modelId")) {
        val mid = params.optString("modelId", current.baseModel.id).trim()
        if (mid.isEmpty()) {
            throw RPCException(-32602, "modelId must not be blank")
        }
        current.baseModel.copy(id = mid)
    } else current.baseModel
    return JSONObject()
}
"""

FIX_PM_BAD_NOTRIM = """fun modelsUpdate(context: Context, params: JSONObject): JSONObject {
    val newModel = if (params.has("modelId")) {
        val mid = params.optString("modelId", current.baseModel.id)
        current.baseModel.copy(id = mid)
    } else current.baseModel
    return JSONObject()
}
"""

# (name, {rel-key: fixture}, rel-map, expected-arm)
SELFTEST_CASES = [
    ("pr-ok", {"pr": FIX_PR_OK, "pu": FIX_PU_OK, "me": FIX_ME_OK, "pm": FIX_PM_OK}, None),
    ("pr-nosync", {"pr": FIX_PR_BAD_NOSYNC, "pu": FIX_PU_OK, "me": FIX_ME_OK, "pm": FIX_PM_OK}, "1"),
    ("pr-nogate", {"pr": FIX_PR_BAD_NOGATE, "pu": FIX_PU_OK, "me": FIX_ME_OK, "pm": FIX_PM_OK}, "1"),
    ("pu-noblank", {"pr": FIX_PR_OK, "pu": FIX_PU_BAD_NOBLANK, "me": FIX_ME_OK, "pm": FIX_PM_OK}, "2"),
    ("pu-nocollide", {"pr": FIX_PR_OK, "pu": FIX_PU_BAD_NOCOLLIDE, "me": FIX_ME_OK, "pm": FIX_PM_OK}, "2"),
    ("me-notrim", {"pr": FIX_PR_OK, "pu": FIX_PU_OK, "me": FIX_ME_BAD_NOTRIM, "pm": FIX_PM_OK}, "3"),
    ("me-nogate", {"pr": FIX_PR_OK, "pu": FIX_PU_OK, "me": FIX_ME_BAD_NOGATE, "pm": FIX_PM_OK}, "3"),
    ("pm-notrim", {"pr": FIX_PR_OK, "pu": FIX_PU_OK, "me": FIX_ME_OK, "pm": FIX_PM_BAD_NOTRIM}, "4"),
]


def selftest():
    rels = {"pr": "ProviderRepository.kt", "pu": "ProviderRepositoryUtils.kt",
            "me": "ModelEntryDetailScreen.kt", "pm": "ProviderMutationMethods.kt"}
    ok = True
    for name, fixtures, expect_arm in SELFTEST_CASES:
        with tempfile.TemporaryDirectory() as tmp:
            for k, rel in rels.items():
                path = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(fixtures[k])
            violations = run_checks(tmp, rels=rels)
            arms = {v[2] for v in violations}
            if expect_arm is None:
                if violations:
                    ok = False
                    print("  selftest %-14s FAIL: expected clean, got arms %s" % (name, sorted(arms)))
                else:
                    print("  selftest %-14s PASS (clean)" % name)
            else:
                if arms == {expect_arm} and violations:
                    print("  selftest %-14s PASS (red on arm %s)" % (name, expect_arm))
                else:
                    ok = False
                    print("  selftest %-14s FAIL: expected red on arm %s, got arms %s" % (name, expect_arm, sorted(arms)))
    if ok:
        print("ENTRY_ID_RENAME_INTEGRITY_SELFTEST=PASS")
    else:
        print("ENTRY_ID_RENAME_INTEGRITY_SELFTEST=FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root_pos", nargs="?", help="repo root (positional)")
    ap.add_argument("--root", dest="root", help="repo root")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    root = args.root or args.root_pos
    if not root:
        print("usage: entry_id_rename_integrity_check.py [root] | --root <root> | --selftest")
        return 2
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print("not a directory: %s" % root)
        return 2
    violations = run_checks(root)
    if violations:
        for rel, ln, arm, msg in violations:
            print("VIOLATION [%s] %s:%d: %s" % (arm, rel, ln, msg))
        print("Summary: %d violation(s)" % len(violations))
        return 1
    print("entry_id_rename_integrity_check: OK (4 arms, 0 violations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

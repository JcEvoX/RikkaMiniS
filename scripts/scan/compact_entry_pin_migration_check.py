#!/usr/bin/env python3
"""deep-audit-1023 ISSUE-03 (P1) — compact-pin secondary-reference gate.

`AgentRuntimeLimitsPrefs.compactModelEntryId` is a *secondary reference* to a
model entry id, held in SharedPreferences rather than in ProviderConfig. Every
per-config cascade that heals the in-config references (modelGroups
.memberEntryIds, agentLoopModelEntryIds, prefs KEY_LAST_USED_ENTRY) is blind to
it, because `saveConfig` only translates ids on its way through
ProviderConfigMapping:

    idMap[entry.uuid] = compositeEntryKey(instanceId, modelId)   (write side)
    uuid = row.id                                                (load side)

A pin that is not rewritten dangles, and `CompactOrchestration` then resolves
it to nothing and silently follows the session chain (INFO-level only) — the
user's explicit "compact with THIS model" choice evaporates with no error.

Pre-fix, ProviderRepository.kt had ZERO references to AgentRuntimeLimitsPrefs:
the in-config cascades existed (lastUsedEntryId was migrated on rename) and the
prefs-level one simply was not part of the family. This gate pins the fixed
shape, in CODE (comments are stripped first):

  Arm 1  `ProviderRepository.updateEntry` — inside the rename branch
         (`if (renamed)`), the pin is migrated through the pure decision
         helper and written back.
  Arm 2  `ProviderRepository.removeEntry` — the deleted id cascades into the
         pin (reset to "" = explicit follow-session), after the agent-loop
         cascade that it mirrors.
  Arm 3  `ProviderRepository.removeInstance` — its own entry cascade (it does
         NOT route through removeEntry) also cleans the pin for the whole
         removed set.
  Arm 4  the pure decision helpers exist as top-level `internal fun`s in the
         repository file (JVM-testable, no Android dependency in the slice).

Usage: python3 scripts/scan/compact_entry_pin_migration_check.py [ROOT]
       python3 scripts/scan/compact_entry_pin_migration_check.py --root ROOT
       python3 scripts/scan/compact_entry_pin_migration_check.py --selftest
Exit:  0 = green, 1 = violation(s) (file:line printed) / selftest failure.
"""
import os
import sys

REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/ProviderRepository.kt"

PIN_READ = "AgentRuntimeLimitsPrefs.compactModelEntryId()"
PIN_SAVE = "AgentRuntimeLimitsPrefs.save(context, compactModelEntryId ="
PIN_RESET = 'compactModelEntryId = ""'

UPDATE_ARM = (
    "decideCompactPinMigration(",
    PIN_READ,
    "compactModelEntryId = migrated",
)
REMOVE_ARM = (
    "decideCompactPinCleanup(",
    PIN_READ,
    PIN_RESET,
)
REMOVE_INSTANCE_ARM = (
    "decideCompactPinCleanup(",
    PIN_RESET,
)
HELPER_ARM = (
    "internal fun decideCompactPinMigration(",
    "internal fun decideCompactPinCleanup(",
)

# Code anchors that must appear BEFORE the pin handling inside the same
# function, i.e. the pin edit rides along with the cascade it mirrors.
UPDATE_AFTER = "if (renamed)"
REMOVE_AFTER = "config.agentLoopModelEntryIds.removeAll { it == entryId }"


def code_only(text):
    """Strip // line comments so no criterion can be satisfied from prose."""
    return "\n".join(line.split("//")[0] for line in text.split("\n"))


def _balanced(src, open_idx, opener="{", closer="}"):
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
    end = _balanced(src, brace)
    return None if end < 0 else src[i:end]


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def check_body(src, rel, header, arm, anchors, what):
    body = function_text(src, header)
    if body is None:
        return [(rel, 0, arm, header + " not found — gate ROT")]
    code = code_only(body)
    base = src.find(header)
    out = []
    for a in anchors:
        if a not in code:
            out.append((rel, line_of(src, base), arm,
                        "%s is missing code anchor `%s`" % (what, a)))
    return out


def check_order(src, rel, header, arm, first, second, what):
    """`first` must appear before `second` inside the function (code only)."""
    body = function_text(src, header)
    if body is None:
        return [(rel, 0, arm, header + " not found — gate ROT")]
    code = code_only(body)
    base = src.find(header)
    i, j = code.find(first), code.find(second)
    if i < 0:
        return [(rel, line_of(src, base), arm,
                 "%s is missing code anchor `%s`" % (what, first))]
    if j >= 0 and j < i:
        return [(rel, line_of(src, base), arm,
                 "%s: `%s` must come AFTER `%s`" % (what, second, first))]
    return []


def check_inside(src, rel, header, arm, guard, needle, what):
    """`needle` must appear INSIDE the `guard { ... }` block of `header`.

    An index-order check is too weak here: it passes whenever the needle
    merely follows the guard opening, which is also true when the needle sits
    after the guard's closing brace (the migration would then run on every
    update, not just renames).
    """
    body = function_text(src, header)
    if body is None:
        return [(rel, 0, arm, header + " not found — gate ROT")]
    base = src.find(header)
    g = body.find(guard)
    if g < 0:
        return [(rel, line_of(src, base), arm,
                 "%s is missing guard `%s`" % (what, guard))]
    brace = body.find("{", g)
    end = _balanced(body, brace) if brace >= 0 else -1
    if brace < 0 or end < 0:
        return [(rel, line_of(src, base), arm,
                 "%s guard `%s` is not a braced block — gate ROT" % (what, guard))]
    if needle not in code_only(body[brace:end]):
        return [(rel, line_of(src, base), arm,
                 "%s: `%s` must sit INSIDE `%s`" % (what, needle, guard))]
    return []


def run_checks(root, rel=REPO_REL):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return [(rel, 0, "?", "file not found — gate ROT")]
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()

    out = []
    out += check_body(src, rel, "fun updateEntry(", "1", UPDATE_ARM, "updateEntry rename branch")
    out += check_inside(src, rel, "fun updateEntry(", "1", UPDATE_AFTER,
                        "decideCompactPinMigration(", "updateEntry rename branch")
    out += check_body(src, rel, "fun removeEntry(", "2", REMOVE_ARM, "removeEntry")
    out += check_order(src, rel, "fun removeEntry(", "2", REMOVE_AFTER,
                       "decideCompactPinCleanup(", "removeEntry")
    out += check_body(src, rel, "fun removeInstance(", "3", REMOVE_INSTANCE_ARM, "removeInstance")
    body_all = code_only(src)
    for a in HELPER_ARM:
        if a not in body_all:
            out.append((rel, 0, "4", "helper missing: `%s`" % a))
    return out


# ----------------------------------------------------------------------
# Selftest fixtures — Kotlin-shaped snippets exercising every arm.
# ----------------------------------------------------------------------
PR_OK = """class ProviderRepository(private val context: Context) {
    fun updateEntry(entry: ModelEntry): String? = synchronized(configLock) {
        val renamed = decision.newKey != null
        val newUuid = decision.newKey ?: existing.id
        if (renamed) {
            val oldKey = decision.oldKey
            if (lastUsedEntryId == oldKey) lastUsedEntryId = newUuid
            decideCompactPinMigration(
                pinEntryId = AgentRuntimeLimitsPrefs.compactModelEntryId(),
                oldKey = oldKey,
                newKey = newUuid,
            )?.let { migrated ->
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = migrated)
            }
        }
        saveConfig(config)
        return@synchronized newUuid
    }

    fun removeEntry(entryId: String) = synchronized(configLock) {
        config.modelEntries.removeAll { it.id == entryId }
        config.agentLoopModelEntryIds.removeAll { it == entryId }
        if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), setOf(entryId))) {
            AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
        }
        saveConfig(config)
    }

    fun removeInstance(instanceId: String) = synchronized(configLock) {
        if (removedEntryIds.isNotEmpty()) {
            config.agentLoopModelEntryIds.removeAll { it in removedEntryIds }
            if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), removedEntryIds)) {
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
            }
        }
        saveConfig(config)
    }
}

internal fun decideCompactPinMigration(pinEntryId: String, oldKey: String, newKey: String): String? = TODO()

internal fun decideCompactPinCleanup(pinEntryId: String, removedEntryIds: Collection<String>): Boolean = TODO()
"""

# Regression shape A: the pre-fix world — pin untouched on rename.
PR_BAD_NO_MIGRATION = """class ProviderRepository(private val context: Context) {
    fun updateEntry(entry: ModelEntry): String? = synchronized(configLock) {
        val renamed = decision.newKey != null
        val newUuid = decision.newKey ?: existing.id
        if (renamed) {
            val oldKey = decision.oldKey
            if (lastUsedEntryId == oldKey) lastUsedEntryId = newUuid
        }
        saveConfig(config)
        return@synchronized newUuid
    }

    fun removeEntry(entryId: String) = synchronized(configLock) {
        config.agentLoopModelEntryIds.removeAll { it == entryId }
        if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), setOf(entryId))) {
            AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
        }
        saveConfig(config)
    }

    fun removeInstance(instanceId: String) = synchronized(configLock) {
        if (removedEntryIds.isNotEmpty()) {
            config.agentLoopModelEntryIds.removeAll { it in removedEntryIds }
            if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), removedEntryIds)) {
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
            }
        }
        saveConfig(config)
    }
}

internal fun decideCompactPinMigration(pinEntryId: String, oldKey: String, newKey: String): String? = TODO()

internal fun decideCompactPinCleanup(pinEntryId: String, removedEntryIds: Collection<String>): Boolean = TODO()
"""

# Regression shape B: pin migrated but on the WRONG side of the rename guard
# (i.e. after the branch closes) — must be rejected as ordering violation.
PR_BAD_ORDER = """class ProviderRepository(private val context: Context) {
    fun updateEntry(entry: ModelEntry): String? = synchronized(configLock) {
        val renamed = decision.newKey != null
        val newUuid = decision.newKey ?: existing.id
        if (renamed) {
            val oldKey = decision.oldKey
            if (lastUsedEntryId == oldKey) lastUsedEntryId = newUuid
        }
        decideCompactPinMigration(
            pinEntryId = AgentRuntimeLimitsPrefs.compactModelEntryId(),
            oldKey = oldKey,
            newKey = newUuid,
        )?.let { migrated ->
            AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = migrated)
        }
        saveConfig(config)
        return@synchronized newUuid
    }

    fun removeEntry(entryId: String) = synchronized(configLock) {
        config.agentLoopModelEntryIds.removeAll { it == entryId }
        if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), setOf(entryId))) {
            AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
        }
        saveConfig(config)
    }

    fun removeInstance(instanceId: String) = synchronized(configLock) {
        if (removedEntryIds.isNotEmpty()) {
            config.agentLoopModelEntryIds.removeAll { it in removedEntryIds }
            if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), removedEntryIds)) {
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
            }
        }
        saveConfig(config)
    }
}

internal fun decideCompactPinMigration(pinEntryId: String, oldKey: String, newKey: String): String? = TODO()

internal fun decideCompactPinCleanup(pinEntryId: String, removedEntryIds: Collection<String>): Boolean = TODO()
"""

# Regression shape C: removeEntry/removeInstance cascades still missing the pin.
PR_BAD_NO_CLEANUP = """class ProviderRepository(private val context: Context) {
    fun updateEntry(entry: ModelEntry): String? = synchronized(configLock) {
        val renamed = decision.newKey != null
        val newUuid = decision.newKey ?: existing.id
        if (renamed) {
            val oldKey = decision.oldKey
            if (lastUsedEntryId == oldKey) lastUsedEntryId = newUuid
            decideCompactPinMigration(
                pinEntryId = AgentRuntimeLimitsPrefs.compactModelEntryId(),
                oldKey = oldKey,
                newKey = newUuid,
            )?.let { migrated ->
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = migrated)
            }
        }
        saveConfig(config)
        return@synchronized newUuid
    }

    fun removeEntry(entryId: String) = synchronized(configLock) {
        config.agentLoopModelEntryIds.removeAll { it == entryId }
        saveConfig(config)
    }

    fun removeInstance(instanceId: String) = synchronized(configLock) {
        if (removedEntryIds.isNotEmpty()) {
            config.agentLoopModelEntryIds.removeAll { it in removedEntryIds }
        }
        saveConfig(config)
    }
}

internal fun decideCompactPinMigration(pinEntryId: String, oldKey: String, newKey: String): String? = TODO()

internal fun decideCompactPinCleanup(pinEntryId: String, removedEntryIds: Collection<String>): Boolean = TODO()
"""

# Regression shape D: helper removed (inlined at call sites) — JVM slice gone.
PR_BAD_NO_HELPER = PR_OK.replace("internal fun decideCompactPinCleanup", "private fun legacyCleanup")


def _report(name, label, checks, want_arms, contains=None):
    hit = [c for c in checks
           if c[2] in want_arms and (contains is None or contains in c[3])]
    if not hit:
        print("  [FAIL] %s (%s): no finding on arm(s) %s (want text %r)"
              % (name, label, want_arms, contains))
        return False
    print("  [OK]   %s (%s): %d finding(s) on arm(s) %s" % (name, label, len(hit), want_arms))
    return True


def selftest():
    ok = True
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, REPO_REL)
        os.makedirs(os.path.dirname(target), exist_ok=True)

        def write(text):
            with open(target, "w", encoding="utf-8") as f:
                f.write(text)

        # 1) fixed shape → all arms clean.
        write(PR_OK)
        c = run_checks(tmp)
        if c:
            print("  [FAIL] fixed fixture not clean: %s" % (c[0],))
            ok = False
        else:
            print("  [OK]   fixed fixture: all 4 arms clean")

        # 2) pre-fix shape → arm 1 must fire on the missing migration.
        write(PR_BAD_NO_MIGRATION)
        ok &= _report("no-migration", "arm 1", run_checks(tmp), ("1",),
                      contains="decideCompactPinMigration(")

        # 3) migration placed OUTSIDE `if (renamed)` → containment violation.
        write(PR_BAD_ORDER)
        ok &= _report("wrong-order", "arm 1", run_checks(tmp), ("1",),
                      contains="must sit INSIDE")

        # 4) delete paths untouched → arms 2+3 fire.
        write(PR_BAD_NO_CLEANUP)
        ok &= _report("no-cleanup", "arms 2/3", run_checks(tmp), ("2", "3"))

        # 5) pure slice removed → arm 4 fires.
        write(PR_BAD_NO_HELPER)
        ok &= _report("no-helper", "arm 4", run_checks(tmp), ("4",))

        # 6) ROT control: missing file must be reported, never silently green.
        import shutil
        shutil.rmtree(tmp)
        os.makedirs(tmp)
        c = run_checks(tmp)
        if not c:
            print("  [FAIL] ROT control: missing file produced no finding")
            ok = False
        else:
            print("  [OK]   ROT control: missing file reported")

    print("COMPACT_ENTRY_PIN_MIGRATION_SELFTEST=%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    root = None
    if "--root" in args:
        i = args.index("--root")
        root = args[i + 1] if i + 1 < len(args) else None
    else:
        for a in args:
            if not a.startswith("-"):
                root = a
                break
    if not root:
        print("usage: compact_entry_pin_migration_check.py [ROOT] | --selftest", file=sys.stderr)
        return 2
    if not os.path.isdir(root):
        print("root not found: %s" % root, file=sys.stderr)
        return 2
    checks = run_checks(root)
    if not checks:
        print("compact_entry_pin_migration_check: OK (4 arms green)")
        return 0
    for rel, line, arm, msg in checks:
        print("%s:%s [arm %s] %s" % (rel, line, arm, msg))
    print("compact_entry_pin_migration_check: %d violation(s)" % len(checks))
    return 1


if __name__ == "__main__":
    sys.exit(main())

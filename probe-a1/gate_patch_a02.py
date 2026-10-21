#!/usr/bin/env python3
"""[dv20-a1 A-02] Add the System-voice member criteria to the two remaining
gates of this branch:

  envvar_config_group_check.py     C12 (Stage 2 member remap keeps the
                                   sentinels) + C13 (the `groups` summary
                                   resolves them);
  provider_rpc_symmetry_check.py   C16 (every group member face judges through
                                   the ONE shared predicate) + C17 (no face
                                   re-spells the sentinel literal).

Idempotent, and every insertion asserts an exact anchor count of 1.
The arms use self-contained synthetic sources so the pre-existing shared
fixtures keep judging exactly what they judged before.
"""
import io
import os
import sys

SCAN = "/var/minis/shared/work/deep-audit-1040/a1/scripts/scan"
ENV_GATE = os.path.join(SCAN, "envvar_config_group_check.py")
RPC_GATE = os.path.join(SCAN, "provider_rpc_symmetry_check.py")

# ─────────────────────────── envvar gate ────────────────────────────

ENV_FN = '''
def check_sentinel_remap(backup_src: str) -> str | None:
    """Criterion 12 [dv20-a1 A-02]: the Stage 2 group-member remap keeps the
    virtual System voice ids instead of counting them as dropped members."""
    text = strip_comments(backup_src)
    drops = [m.start() for m in re.finditer(r"else\\s+droppedMembers\\+\\+", text)]
    if not drops:
        return (
            "C12 EXTRACT_FAIL: ConfigBackup group-member remap "
            "(`else droppedMembers++`) not found (fail-closed)"
        )
    for pos in drops:
        if "SystemVoiceEntries.isSystemEntryId" not in text[max(0, pos - 400):pos]:
            return (
                "C12 RED: the member remap drops a member whenever the entry map has "
                "no mapping — the virtual System voice ids are never in that map, so "
                "every voice-group member counts as a dropped member; the remap must "
                "consult SystemVoiceEntries.isSystemEntryId (single source)"
            )
    if "__builtin_system_speech__" in backup_src:
        return (
            "C12 RED: ConfigBackup spells the System sentinel id literal instead of "
            "going through SystemVoiceEntries — the id must have one home"
        )
    return None

def check_group_member_resolution(builtins_src: str) -> str | None:
    """Criterion 13 [dv20-a1 A-02]: the `groups` config summary resolves the
    virtual System voice entries instead of emitting a "missing" stub."""
    text = strip_comments(builtins_src)
    if "SystemVoiceEntries.resolve(" not in text:
        return (
            "C13 RED: the `groups` config summary never calls "
            "SystemVoiceEntries.resolve — a virtual System voice member is rendered "
            "as a missing model row even though chat resolves it fine"
        )
    if "__builtin_system_speech__" in builtins_src:
        return (
            "C13 RED: ConfigBuiltins spells the System sentinel id literal instead of "
            "going through SystemVoiceEntries — the id must have one home"
        )
    return None

'''

ENV_REG_FROM = '''        for fn in (
            lambda: check_backup_scope_envvars(bk),
            lambda: check_overlay_registration(cb),
            lambda: check_overlay_single_source(cb),
            lambda: check_reverse_anchors(bk, cb),
        ):
'''
ENV_REG_TO = '''        for fn in (
            lambda: check_backup_scope_envvars(bk),
            lambda: check_overlay_registration(cb),
            lambda: check_overlay_single_source(cb),
            lambda: check_reverse_anchors(bk, cb),
            lambda: check_sentinel_remap(bk),
            lambda: check_group_member_resolution(cb),
        ):
'''

ENV_MSG_FROM = '''    print("envvar_config_group_check: OK (11 criteria green)")
'''
ENV_MSG_TO = '''    print("envvar_config_group_check: OK (13 criteria green)")
'''

ENV_ARMS = '''    # ── [dv20-a1 A-02] sentinel-preservation arms ─────────────────────────
    SENTINEL_BACKUP_OK = \'\'\'
package fake.config

import com.rikkaminis.app.data.model.SystemVoiceEntries

object ConfigBackup {
    private fun remapMembers(entryIdMap: Map<String, String>, srcMembers: List<String>): Int {
        var droppedMembers = 0
        for (old in srcMembers) {
            val mapped = entryIdMap[old]
                ?: old.takeIf { SystemVoiceEntries.isSystemEntryId(it) }
            if (mapped != null) members.add(mapped) else droppedMembers++
        }
        return droppedMembers
    }
}
\'\'\'

    SENTINEL_BUILTINS_OK = \'\'\'
package fake.config

import com.rikkaminis.app.data.model.SystemVoiceEntries

object ConfigBuiltins {
    fun groupsSummary(entriesById: Map<String, Any>): List<Any> {
        val entryIds = listOf("a")
        return entryIds.map { entryId ->
            val e = entriesById[entryId] ?: SystemVoiceEntries.resolve(entryId)
            if (e == null) listOf("entry_id", entryId, "missing") else listOf("entry_id", entryId)
        }
    }
}
\'\'\'

    def sentinel_arm(label: str, source: str, fn, want) -> bool:
        r = fn(source)
        got = [] if r is None else [r]
        if want is None:
            if got:
                print(f"[SELFTEST] {label}: expected GREEN, got {got}")
                return False
            print(f"[SELFTEST] {label}: GREEN as expected")
            return True
        if len(got) != 1 or not got[0].startswith(want):
            print(f"[SELFTEST] {label}: expected exactly one {want!r}, got {got}")
            return False
        print(f"[SELFTEST] {label}: red as expected ({got[0]})")
        return True

    ok &= sentinel_arm("FIX-19 sentinel-preserving remap", SENTINEL_BACKUP_OK,
                       check_sentinel_remap, None)
    ok &= sentinel_arm(
        "FIX-20 remap drops the sentinels",
        SENTINEL_BACKUP_OK.replace(
            "                ?: old.takeIf { SystemVoiceEntries.isSystemEntryId(it) }\\n", ""),
        check_sentinel_remap, "C12 RED:")
    ok &= sentinel_arm("FIX-21 remap anchor absent",
                       "package fake\\n\\nobject ConfigBackup {\\n}\\n",
                       check_sentinel_remap, "C12 EXTRACT_FAIL:")
    ok &= sentinel_arm("FIX-22 summary resolves the members", SENTINEL_BUILTINS_OK,
                       check_group_member_resolution, None)
    ok &= sentinel_arm(
        "FIX-23 summary emits the missing stub",
        SENTINEL_BUILTINS_OK.replace(" ?: SystemVoiceEntries.resolve(entryId)", ""),
        check_group_member_resolution, "C13 RED:")
    ok &= sentinel_arm(
        "FIX-24 sentinel literal re-spelled",
        SENTINEL_BUILTINS_OK.replace(
            "        val entryIds = listOf(\\"a\\")",
            "        val pinned = \\"__builtin_system_speech__\\"\\n"
            "        val entryIds = listOf(\\"a\\")"),
        check_group_member_resolution, "C13 RED:")

'''

ENV_ARM_ANCHOR = '''    for dd in fds:
        shutil.rmtree(dd, ignore_errors=True)

    return ok
'''

# ──────────────────────── provider_rpc gate ─────────────────────────

RPC_FN = '''
def check_group_member_faces(mut_code, debug_code, groups_code):
    """[dv20-a1 A-02] Two decisions on the group member faces.

    C16 — member-id validity is judged through the ONE shared predicate
    `SystemVoiceEntries.isKnownGroupMemberId` on every write face
    (GroupsCollection.requireKnownEntryIds, groupsCreate, groupsUpdate), and the
    read face (groupsList member rows) resolves the virtual System entries
    through `SystemVoiceEntries.resolve` instead of dropping them. The
    sentinels are never persisted, so a face that re-derives "known" from
    `modelEntries` alone calls every voice-group member unknown.

    C17 — none of those faces re-spells the sentinel id literal; the id has one
    home, or the five readers drift apart again.

    Returns (v16, v17, fail_closed).
    """
    v16, v17, vfc = [], [], []
    helper = extract_function(groups_code, ENTRY_ID_HELPER)
    if helper is None:
        vfc.append("%s: %s not found (fail-closed) — the single implementation moved"
                   % (GROUPS_COLLECTION_FILE, ENTRY_ID_HELPER))
    elif "SystemVoiceEntries.isKnownGroupMemberId" not in helper:
        v16.append(
            "%s: %s judges member ids from the persisted entry list alone — the "
            "virtual System voice ids are never persisted, so a voice group written "
            "by the harness is refused with '%s'; consult "
            "SystemVoiceEntries.isKnownGroupMemberId (single source)"
            % (GROUPS_COLLECTION_FILE, ENTRY_ID_HELPER, ENTRY_ID_MSG))
    for fn in (GROUP_CREATE_FN, GROUP_UPDATE_FN):
        body = extract_function(mut_code, fn)
        if body is None:
            vfc.append("%s: %s not found (fail-closed)" % (MUTATION_FILE, fn))
        elif "SystemVoiceEntries.isKnownGroupMemberId" not in body:
            v16.append(
                "%s: %s filters member ids by the persisted entry list alone — a System "
                "voice member is silently dropped from the group it was just handed; "
                "consult SystemVoiceEntries.isKnownGroupMemberId"
                % (MUTATION_FILE, fn))
    members = extract_function(debug_code, GROUP_LIST_FN)
    if members is None:
        vfc.append("%s: %s not found (fail-closed)" % (DEBUG_FILE, GROUP_LIST_FN))
    elif "SystemVoiceEntries.resolve(" not in members:
        v16.append(
            "%s: %s never resolves a virtual System voice member — the member row is "
            "silently missing from the group the harness just read back; resolve it "
            "through SystemVoiceEntries.resolve" % (DEBUG_FILE, GROUP_LIST_FN))
    for label, code in ((GROUPS_COLLECTION_FILE, groups_code),
                        (MUTATION_FILE, mut_code),
                        (DEBUG_FILE, debug_code)):
        if "__builtin_system_speech__" in code:
            v17.append(
                "%s: spells the System sentinel id literal — it must come from "
                "SystemVoiceEntries, or one of the five readers drifts the moment the "
                "id changes" % label)
    return v16, v17, vfc

'''

RPC_DETAIL_FROM = '''    detail = {"c1": [], "c2": [], "c3": [], "c4": [], "c5": [], "c6": [], "c7": [],
              "c8": [], "c9": [], "c10": [], "c11": [], "c12": [], "c13": [],
              "c14": [], "c15": [], "fail_closed": []}
'''
RPC_DETAIL_TO = '''    detail = {"c1": [], "c2": [], "c3": [], "c4": [], "c5": [], "c6": [], "c7": [],
              "c8": [], "c9": [], "c10": [], "c11": [], "c12": [], "c13": [],
              "c14": [], "c15": [], "c16": [], "c17": [], "fail_closed": []}
'''

RPC_CALL_FROM = '''    # ---- C14 ---- [fix/dv16-a A-07] the two instance write faces must accept
'''
RPC_CALL_TO = '''    # ---- C16/C17 ---- [dv20-a1 A-02] the group member faces must judge through
    # the ONE shared predicate, and must not re-spell the sentinel id.
    v16, v17, vfc = check_group_member_faces(mut_code, debug_code, groups_code)
    detail["c16"].extend(v16)
    detail["c17"].extend(v17)
    detail["fail_closed"].extend(vfc)

    # ---- C14 ---- [fix/dv16-a A-07] the two instance write faces must accept
'''

RPC_KEYS = '''    for k in ("c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8", "c9", "c10", "c11",
              "c12", "c13", "c14", "c15", "fail_closed"):'''
RPC_KEYS_NEW = '''    for k in ("c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8", "c9", "c10", "c11",
              "c12", "c13", "c14", "c15", "c16", "c17", "fail_closed"):'''
# the same key list appears twice (rc computation vs printing) — anchor each
# with its next line so both edits stay exact.
RPC_MAIN_FROM = RPC_KEYS + "\n        if detail[k]:"
RPC_MAIN_TO = RPC_KEYS_NEW + "\n        if detail[k]:"
RPC_PRINT_FROM = RPC_KEYS + "\n        for line in detail[k]:"
RPC_PRINT_TO = RPC_KEYS_NEW + "\n        for line in detail[k]:"

RPC_MSG_FROM = '''              "C14 instance key parity, C15 collection image keys)")
'''
RPC_MSG_TO = '''              "C14 instance key parity, C15 collection image keys, "
              "C16 group member predicate single-source, C17 no sentinel literal)")
'''

RPC_FIXTURES = '''
# -- [dv20-a1 A-02] group member-face fixtures -------------------------------
# Self-contained on purpose: the shared GOOD_* fixtures above keep judging
# exactly what they judged before this batch.

GOOD_MEMBER_FACES_COLL = \'\'\'
package fake.collections

import com.rikkaminis.app.data.model.SystemVoiceEntries

class GroupsCollection {
    private fun requireKnownEntryIds(ids: List<String>) {
        val valid = repo.config.value.modelEntries.map { it.id }.toSet()
        for (eid in ids) {
            if (!SystemVoiceEntries.isKnownGroupMemberId(eid, valid)) {
                throw ConfigError.InvalidValue("Unknown model entry uuid: $eid")
            }
        }
    }
}
\'\'\'

GOOD_MEMBER_FACES_MUT = \'\'\'
package fake.debug

import com.rikkaminis.app.data.model.SystemVoiceEntries

object ProviderMutationMethods {
    fun groupsCreate(repo: Any, params: Any): Any {
        val knownEntryIds = repo.config.value.modelEntries.map { it.id }.toSet()
        if (id.isNotEmpty() && SystemVoiceEntries.isKnownGroupMemberId(id, knownEntryIds)) {
            members.add(id)
        }
        return members
    }

    fun groupsUpdate(repo: Any, params: Any): Any {
        val knownIds = repo.config.value.modelEntries.map { it.id }.toSet()
        if (mid.isNotEmpty() && SystemVoiceEntries.isKnownGroupMemberId(mid, knownIds)) {
            members.add(mid)
        }
        return members
    }
}
\'\'\'

GOOD_MEMBER_FACES_DBG = \'\'\'
package fake.debug

import com.rikkaminis.app.data.model.SystemVoiceEntries

object ProviderDebugMethods {
    fun groupsList(cfg: Any): Any {
        val members = JSONArray()
        for (memberId in group.memberEntryIds) {
            val persisted = cfg.modelEntries.find { it.id == memberId }
            val entry = persisted ?: SystemVoiceEntries.resolve(memberId) ?: continue
            members.put(JSONObject().apply { put("entryId", entry.id) })
        }
        return members
    }
}
\'\'\'

'''

RPC_ARMS = '''    # --- [dv20-a1 A-02] C16/C17: the group member faces judge through the one
    # shared predicate, and none of them re-spells the sentinel id.
    def member_face_arm(label, key, mut, dbg, grp):
        v16, v17, vfc = check_group_member_faces(mut, dbg, grp)
        got = {"c16": v16, "c17": v17, "fail_closed": vfc}
        if key is None:
            clean = not any(got.values())
            results.append((label, clean, {} if clean else got))
            return
        ok = bool(got[key]) and not any(got[k] for k in got if k != key)
        results.append((label, ok, got))

    member_face_arm("F40-member-faces-good-green", None,
                    GOOD_MEMBER_FACES_MUT, GOOD_MEMBER_FACES_DBG, GOOD_MEMBER_FACES_COLL)
    member_face_arm(
        "F41-collection-refuses-sentinels-C16", "c16",
        GOOD_MEMBER_FACES_MUT, GOOD_MEMBER_FACES_DBG,
        GOOD_MEMBER_FACES_COLL.replace(
            "!SystemVoiceEntries.isKnownGroupMemberId(eid, valid)", "eid !in valid"))
    member_face_arm(
        "F42-create-drops-sentinels-C16", "c16",
        GOOD_MEMBER_FACES_MUT.replace(
            "SystemVoiceEntries.isKnownGroupMemberId(id, knownEntryIds)", "id in knownEntryIds"),
        GOOD_MEMBER_FACES_DBG, GOOD_MEMBER_FACES_COLL)
    member_face_arm(
        "F43-list-drops-sentinels-C16", "c16",
        GOOD_MEMBER_FACES_MUT,
        GOOD_MEMBER_FACES_DBG.replace(" ?: SystemVoiceEntries.resolve(memberId)", ""),
        GOOD_MEMBER_FACES_COLL)
    member_face_arm(
        "F44-sentinel-literal-respelled-C17", "c17",
        GOOD_MEMBER_FACES_MUT.replace(
            "        val knownIds = repo.config.value.modelEntries.map { it.id }.toSet()",
            "        val pinned = \\"__builtin_system_speech__\\"\\n"
            "        val knownIds = repo.config.value.modelEntries.map { it.id }.toSet()"),
        GOOD_MEMBER_FACES_DBG, GOOD_MEMBER_FACES_COLL)
    member_face_arm(
        "F45-member-face-anchor-absent-fail-closed", "fail_closed",
        GOOD_MEMBER_FACES_MUT, GOOD_MEMBER_FACES_DBG,
        "package fake\\n\\nclass GroupsCollection {\\n}\\n")

'''

RPC_FIX_ANCHOR = "def write_fixture(debug_text, mutation_text, collection_text=None,"
RPC_ARM_ANCHOR = '''    all_ok = True
    for name, ok, detail in results:
'''


def apply(path, edits, marker):
    with io.open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    if marker in src:
        print("%s: already patched — no-op" % os.path.basename(path))
        return
    for old, new in edits:
        n = src.count(old)
        if n != 1:
            raise SystemExit("%s: anchor count %d (expected 1) for %r"
                             % (os.path.basename(path), n, old[:70]))
        src = src.replace(old, new)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    print("%s: patched (%d bytes)" % (os.path.basename(path), len(src)))


def main():
    apply(ENV_GATE, [
        ("def check(envvars_path: str, builtins_path: str, groups_path: str,",
         ENV_FN.lstrip("\n") + "\ndef check(envvars_path: str, builtins_path: str, groups_path: str,"),
        (ENV_REG_FROM, ENV_REG_TO),
        (ENV_MSG_FROM, ENV_MSG_TO),
        (ENV_ARM_ANCHOR, ENV_ARMS + ENV_ARM_ANCHOR),
    ], "check_sentinel_remap")

    apply(RPC_GATE, [
        ("def check_tree(root):", RPC_FN.lstrip("\n") + "\ndef check_tree(root):"),
        (RPC_DETAIL_FROM, RPC_DETAIL_TO),
        (RPC_CALL_FROM, RPC_CALL_TO),
        (RPC_MAIN_FROM, RPC_MAIN_TO),
        (RPC_PRINT_FROM, RPC_PRINT_TO),
        (RPC_MSG_FROM, RPC_MSG_TO),
        (RPC_FIX_ANCHOR, RPC_FIXTURES.lstrip("\n") + "\n" + RPC_FIX_ANCHOR),
        (RPC_ARM_ANCHOR, RPC_ARMS + RPC_ARM_ANCHOR),
    ], "check_group_member_faces")
    return 0


if __name__ == "__main__":
    sys.exit(main())

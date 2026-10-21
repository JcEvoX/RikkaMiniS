#!/usr/bin/env python3
"""[dv20-a1 A-02] Re-anchor the SHARED fixtures of the two gates to the
post-fix shape.

The new criteria (envvar C12/C13, rpc C16/C17) judge the group-member decision
points. A gate fixture is supposed to mirror the current correct code, so the
"good" fixtures have to carry the sentinel-aware calls — otherwise every
pre-existing arm would report the new criteria as red on the old shape.

Only the good fixtures change; every mutation arm still anchors on the text it
anchored on before. Idempotent, exact anchor counts.
"""
import io
import os
import sys

SCAN = "/var/minis/shared/work/deep-audit-1040/a1/scripts/scan"
ENV_GATE = os.path.join(SCAN, "envvar_config_group_check.py")
RPC_GATE = os.path.join(SCAN, "provider_rpc_symmetry_check.py")

# ── provider_rpc: give the good fixtures the single-source calls ──────────

E1_OLD = '''    fun groupsList(context: android.content.Context, params: JSONObject): JSONObject {
        val arr = JSONArray()
'''
E1_NEW = '''    fun groupsList(context: android.content.Context, params: JSONObject): JSONObject {
        val arr = JSONArray()
        val resolvedMember = com.rikkaminis.app.data.model.SystemVoiceEntries.resolve("m")
'''

E2_OLD = '''    fun groupsCreate(context: android.content.Context, params: JSONObject): JSONObject {
        val thinking = params.optString("defaultThinkingLevel", "")
'''
E2_NEW = '''    fun groupsCreate(context: android.content.Context, params: JSONObject): JSONObject {
        val thinking = params.optString("defaultThinkingLevel", "")
        val membersOk = SystemVoiceEntries.isKnownGroupMemberId("m", emptySet())
'''

E3_OLD = '''    fun groupsUpdate(context: android.content.Context, params: JSONObject): JSONObject {
        var limitValue = 0
'''
E3_NEW = '''    fun groupsUpdate(context: android.content.Context, params: JSONObject): JSONObject {
        var limitValue = 0
        val membersOk = SystemVoiceEntries.isKnownGroupMemberId("m", emptySet())
'''

E4_OLD = '''            if (eid !in valid) throw ConfigError.InvalidValue("Unknown model entry uuid: $eid")
'''
E4_NEW = '''            if (!SystemVoiceEntries.isKnownGroupMemberId(eid, valid)) {
                throw ConfigError.InvalidValue("Unknown model entry uuid: $eid")
            }
'''

# ── envvar: the backup remap keeps sentinels, the summary resolves them ───

E5_OLD = '''                                    "entries" to ConfigValue.Arr(g.memberEntryIds.map { ConfigValue.Str(it) }),
'''
E5_NEW = '''                                    "entries" to ConfigValue.Arr(g.memberEntryIds.map { memberId ->
                                        ConfigValue.Str(SystemVoiceEntries.resolve(memberId)?.id ?: memberId)
                                    }),
'''

E6_OLD = "FIXTURE_BACKUP_OK = '''package fake\n"
E6_NEW = """FIXTURE_BACKUP_OK = '''package fake

private fun remapGroupMembers(
    entryIdMap: Map<String, String>,
    srcMembers: List<String>,
    out: MutableList<String>,
): Int {
    var droppedMembers = 0
    for (old in srcMembers) {
        val mapped = entryIdMap[old] ?: old.takeIf { SystemVoiceEntries.isSystemEntryId(it) }
        if (mapped != null) out.add(mapped) else droppedMembers++
    }
    return droppedMembers
}
"""

MARKER = "[dv20-a1 A-02] fixture re-anchor"


def apply(path, edits):
    with io.open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    if MARKER in src:
        print("%s: already re-anchored — no-op" % os.path.basename(path))
        return
    for tag, old, new in edits:
        n = src.count(old)
        if n != 1:
            raise SystemExit("%s: %s anchor count %d (expected 1) for %r"
                             % (os.path.basename(path), tag, n, old[:60]))
        src = src.replace(old, new, 1)
    # leave a trace so a re-run is a no-op and a reader can see why
    src = src.replace("import sys\n", "import sys  # " + MARKER + "\n", 1)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    print("%s: re-anchored (%d bytes)" % (os.path.basename(path), len(src)))


def main():
    apply(RPC_GATE, [("E1", E1_OLD, E1_NEW), ("E2", E2_OLD, E2_NEW),
                     ("E3", E3_OLD, E3_NEW), ("E4", E4_OLD, E4_NEW)])
    apply(ENV_GATE, [("E5", E5_OLD, E5_NEW), ("E6", E6_OLD, E6_NEW)])
    return 0


if __name__ == "__main__":
    sys.exit(main())

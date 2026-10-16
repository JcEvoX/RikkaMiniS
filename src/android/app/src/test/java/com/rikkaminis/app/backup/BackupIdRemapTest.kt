package com.rikkaminis.app.backup

import com.rikkaminis.app.config.ConfigValue
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

/**
 * [FIX-E / ISSUE-09] Pins the id remapping a restore must perform.
 *
 * A backup taken on install A carries that install's group/entry uuids. Import
 * mints fresh uuids (Stages 1-2 build [groupIdMap] / [entryIdMap]) and every
 * persisted reference must be rewritten onto them. `remapDefaultsIds` used to
 * miss `runtime.compactModelEntryId` (the pinned compaction model — its
 * consumer silently follows the session chain when the pin cannot be resolved,
 * so the loss was invisible), and session `modelBinding` rows were written
 * through verbatim, so every restored session failed restoreFromBinding and
 * degraded to its stale modelId.
 *
 * Expectations are dead literals — never derived from the code under test.
 */
class BackupIdRemapTest {

    // ── remapDefaultsIds ───────────────────────────────────────────────────

    @Test
    fun remapDefaultsIds_rewritesCompactModelEntryId() {
        val out = remapDefaultsIds(
            path = "runtime.compactModelEntryId",
            value = ConfigValue.Str("11111111-1111-1111-1111-111111111111"),
            groupIdMap = emptyMap(),
            entryIdMap = mapOf("11111111-1111-1111-1111-111111111111" to "22222222-2222-2222-2222-222222222222"),
        )
        assertEquals(ConfigValue.Str("22222222-2222-2222-2222-222222222222"), out)
    }

    @Test
    fun remapDefaultsIds_keepsIdMissingFromMap() {
        val out = remapDefaultsIds(
            path = "runtime.compactModelEntryId",
            value = ConfigValue.Str("00000000-0000-0000-0000-00000000000a"),
            groupIdMap = emptyMap(),
            entryIdMap = mapOf("ffffffff-ffff-ffff-ffff-ffffffffffff" to "22222222-2222-2222-2222-222222222222"),
        )
        assertEquals(ConfigValue.Str("00000000-0000-0000-0000-00000000000a"), out)
    }

    @Test
    fun remapDefaultsIds_emptyMapsReturnInputUntouched() {
        // Same-install restore (no maps minted) must not rewrite anything —
        // assertSame proves the value object itself came back, byte-identical.
        for (path in listOf(
            "defaults.primaryGroup",
            "defaults.subGroup",
            "defaults.agentLoopGroups",
            "defaults.agentLoopEntries",
            "runtime.compactModelEntryId",
        )) {
            val str = ConfigValue.Str("11111111-1111-1111-1111-111111111111")
            assertSame(str, remapDefaultsIds(path, str, emptyMap(), emptyMap()))
            val arr = ConfigValue.Arr(
                listOf(
                    ConfigValue.Str("11111111-1111-1111-1111-111111111111"),
                    ConfigValue.Bool(true),
                )
            )
            assertSame(arr, remapDefaultsIds(path, arr, emptyMap(), emptyMap()))
        }
    }

    @Test
    fun remapDefaultsIds_arrElementUnmappedStays() {
        val out = remapDefaultsIds(
            path = "defaults.agentLoopEntries",
            value = ConfigValue.Arr(
                listOf(
                    ConfigValue.Str("11111111-1111-1111-1111-111111111111"),
                    ConfigValue.Str("99999999-9999-9999-9999-999999999999"),
                    ConfigValue.Bool(false),
                )
            ),
            groupIdMap = emptyMap(),
            entryIdMap = mapOf("11111111-1111-1111-1111-111111111111" to "22222222-2222-2222-2222-222222222222"),
        )
        assertEquals(
            ConfigValue.Arr(
                listOf(
                    ConfigValue.Str("22222222-2222-2222-2222-222222222222"),
                    ConfigValue.Str("99999999-9999-9999-9999-999999999999"),
                    ConfigValue.Bool(false),
                )
            ),
            out,
        )
    }

    @Test
    fun remapDefaultsIds_unknownPathReturnsInput() {
        val v = ConfigValue.Str("whatever")
        assertSame(v, remapDefaultsIds("runtime.memoryInjectLines", v, mapOf("x" to "y"), mapOf("x" to "y")))
    }

    // ── remapSessionBindingJson ────────────────────────────────────────────

    @Test
    fun remapSessionBindingJson_rewritesGroupBinding() {
        val out = remapSessionBindingJson(
            bindingJson = """{"type":"group","groupId":"g-old","lastEntryId":"e-old"}""",
            groupIdMap = mapOf("g-old" to "g-new"),
            entryIdMap = mapOf("e-old" to "e-new"),
        )
        val obj = JSONObject(out!!)
        assertEquals("group", obj.optString("type"))
        assertEquals("g-new", obj.optString("groupId"))
        assertEquals("e-new", obj.optString("lastEntryId"))
        assertEquals(3, obj.length())
    }

    @Test
    fun remapSessionBindingJson_rewritesEntryBinding() {
        val out = remapSessionBindingJson(
            bindingJson = """{"type":"entry","entryId":"e-old"}""",
            groupIdMap = emptyMap(),
            entryIdMap = mapOf("e-old" to "e-new"),
        )
        val obj = JSONObject(out!!)
        assertEquals("entry", obj.optString("type"))
        assertEquals("e-new", obj.optString("entryId"))
        assertEquals(2, obj.length())
    }

    @Test
    fun remapSessionBindingJson_unknownIdsKeepOriginalValues() {
        val out = remapSessionBindingJson(
            bindingJson = """{"type":"group","groupId":"g-keep","lastEntryId":"e-keep"}""",
            groupIdMap = mapOf("g-other" to "g-new"),
            entryIdMap = emptyMap(),
        )
        val obj = JSONObject(out!!)
        assertEquals("g-keep", obj.optString("groupId"))
        assertEquals("e-keep", obj.optString("lastEntryId"))
    }

    @Test
    fun remapSessionBindingJson_emptyMapsReturnIdenticalString() {
        // Same-install restore: byte-identical passthrough, not a re-serialize.
        val group = """{"type":"group","groupId":"g-old","lastEntryId":"e-old"}"""
        assertEquals(group, remapSessionBindingJson(group, emptyMap(), emptyMap()))
        assertNull(remapSessionBindingJson(null, mapOf("a" to "b"), mapOf("a" to "b")))
        assertEquals("", remapSessionBindingJson("", mapOf("a" to "b"), mapOf("a" to "b")))
    }

    @Test
    fun remapSessionBindingJson_malformedPayloadReturnedUnchanged() {
        val maps = mapOf("e-old" to "e-new")
        for (payload in listOf(
            "not json at all",
            """[1,2,3]""",
            """{"type":"future","entryId":"e-old"}""",
            """{"entryId":"e-old"}""",
        )) {
            assertEquals(payload, remapSessionBindingJson(payload, maps, maps))
        }
    }

    @Test
    fun remapSessionBindingJson_groupWithoutLastEntryIdStaysKeyless() {
        // ChatSessionEntryExt can persist {"type":"group","groupId":…} with no
        // lastEntryId — remapping must not conjure the key.
        val out = remapSessionBindingJson(
            bindingJson = """{"type":"group","groupId":"g-old"}""",
            groupIdMap = mapOf("g-old" to "g-new"),
            entryIdMap = emptyMap(),
        )
        val obj = JSONObject(out!!)
        assertEquals("g-new", obj.optString("groupId"))
        assertEquals(false, obj.has("lastEntryId"))
    }
}

package com.rikkaminis.app.backup

import com.rikkaminis.app.data.model.FallbackStrategy
import com.rikkaminis.app.data.model.ModelGroup
import com.rikkaminis.app.data.model.RoutingStrategy
import com.rikkaminis.app.data.model.ThinkingLevel
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/dv18-4 A-07] A backup's `groups[]` entry carries five run-config
 * fields (strategy / fallbackStrategy / defaultThinkingLevel /
 * contextLimitTokens / lastContextLimitTokens — see buildSections), but the
 * import's same-name merge branch used to decode NONE of them, so restoring
 * onto an install that already had the group silently reset the user's
 * routing / thinking / context-limit choices. ConfigBackup.import needs
 * Android repositories, so what is testable from the JVM is the seam the fix
 * lives in: readGroupRunConfig (the ONE decode) + applyGroupRunConfig (the
 * merge-path applier, which honours per-key presence so a legacy backup is
 * a no-op on run-config instead of a reset). All expectations are written as
 * literals — never derived from the code under test.
 */
class ModelGroupRunConfigRestoreTest {

    /** A fully-configured `groups[]` entry exactly as buildSections emits it
     * (keys and enum `.name` spell the export payload contract). */
    private fun fullPayload(): JSONObject = JSONObject(
        """
        {
          "id": "src-group-id",
          "name": "work",
          "memberEntryIds": ["src-entry-1", "src-entry-2"],
          "strategy": "loadBalance",
          "fallbackStrategy": "always",
          "defaultThinkingLevel": "MEDIUM",
          "contextLimitTokens": 128000,
          "lastContextLimitTokens": 96000
        }
        """.trimIndent()
    )

    /** The local (pre-restore) group: same name, deliberately different
     * run-config on every field, so a silent drop is indistinguishable from
     * "kept local" only if the test fails to write. */
    private fun localGroup(): ModelGroup = ModelGroup(
        id = "local-group-id",
        name = "work",
        memberEntryIds = mutableListOf("local-entry-1"),
        strategy = RoutingStrategy.cheapestFirst,
        fallbackStrategy = FallbackStrategy.default,
        defaultThinkingLevel = ThinkingLevel.HIGH,
        contextLimitTokens = 64000,
        lastContextLimitTokens = 32000,
    )

    private fun decode(obj: JSONObject, warnings: MutableList<String>): GroupRunConfigSnapshot =
        readGroupRunConfig(obj) { warnings.add(it) }

    @Test
    fun sameNameMergeRestoresAllFiveRunConfigFieldsFromBackup() {
        val warnings = mutableListOf<String>()
        val local = localGroup()
        val snapshot = decode(fullPayload(), warnings)
        val merged = applyGroupRunConfig(local, snapshot)

        // The backup wins on every run-config field.
        assertEquals(RoutingStrategy.loadBalance, merged.strategy)
        assertEquals(FallbackStrategy.always, merged.fallbackStrategy)
        assertEquals(ThinkingLevel.MEDIUM, merged.defaultThinkingLevel)
        assertEquals(128000, merged.contextLimitTokens)
        assertEquals(96000, merged.lastContextLimitTokens)
        // The merge match key and the member list are the caller's decisions:
        // the applier must not touch them.
        assertEquals("local-group-id", merged.id)
        assertEquals("work", merged.name)
        assertEquals(listOf("local-entry-1"), merged.memberEntryIds)
        // A well-formed payload produces no warnings.
        assertTrue(warnings.isEmpty())
        // Copy-on-write: the live local object is unchanged.
        assertEquals(RoutingStrategy.cheapestFirst, local.strategy)
        assertEquals(FallbackStrategy.default, local.fallbackStrategy)
        assertEquals(ThinkingLevel.HIGH, local.defaultThinkingLevel)
        assertEquals(64000, local.contextLimitTokens)
        assertEquals(32000, local.lastContextLimitTokens)
    }

    @Test
    fun legacyBackupWithoutRunConfigKeysKeepsLocalRunConfig() {
        val legacy = JSONObject(
            """
            {
              "id": "src-group-id",
              "name": "work",
              "memberEntryIds": ["src-entry-1"]
            }
            """.trimIndent()
        )
        val snapshot = decode(legacy, mutableListOf())
        val merged = applyGroupRunConfig(localGroup(), snapshot)

        // Absent keys must NOT overwrite the local values with null/defaults.
        assertEquals(RoutingStrategy.cheapestFirst, merged.strategy)
        assertEquals(FallbackStrategy.default, merged.fallbackStrategy)
        assertEquals(ThinkingLevel.HIGH, merged.defaultThinkingLevel)
        assertEquals(64000, merged.contextLimitTokens)
        assertEquals(32000, merged.lastContextLimitTokens)
        // ...but the same decode must still feed the create path with the
        // pre-fix inline defaults (pinned below as literals).
        assertEquals(RoutingStrategy.fallback, snapshot.strategy.value)
        assertEquals(FallbackStrategy.default, snapshot.fallbackStrategy.value)
        assertNull(snapshot.defaultThinkingLevel.value)
        assertNull(snapshot.contextLimitTokens.value)
        assertNull(snapshot.lastContextLimitTokens.value)
    }

    @Test
    fun unknownThinkingLevelDecodesToNoOverrideAndWarns() {
        val payload = fullPayload().put("defaultThinkingLevel", "ULTRA_PLUS")
        val warnings = mutableListOf<String>()
        val snapshot = decode(payload, warnings)
        val merged = applyGroupRunConfig(localGroup(), snapshot)

        // Result is unchanged from the pre-fix silent-null behaviour…
        assertNull(snapshot.defaultThinkingLevel.value)
        assertNull(merged.defaultThinkingLevel)
        // …but the drop is no longer silent.
        assertEquals(1, warnings.size)
        assertTrue(
            "warning should name the unknown token, was: " + warnings,
            warnings[0].contains("ULTRA_PLUS"),
        )
        // The other four fields still restore.
        assertEquals(RoutingStrategy.loadBalance, merged.strategy)
        assertEquals(96000, merged.lastContextLimitTokens)
    }

    @Test
    fun presentButNonPositiveTokenLimitClearsTheOverride() {
        // A present key IS the backup speaking (export omits nulls), so a
        // present-but-non-positive limit means "no limit" — the same shape the
        // create path has always decoded — and must overwrite the local value.
        val payload = fullPayload().put("contextLimitTokens", 0)
        payload.remove("lastContextLimitTokens")
        val merged = applyGroupRunConfig(
            localGroup(),
            decode(payload, mutableListOf()),
        )
        assertEquals(null, merged.contextLimitTokens)
        // A key the payload does not carry keeps the local value.
        assertEquals(32000, merged.lastContextLimitTokens)
    }

    @Test
    fun unknownStrategyTokenDecodesToModelDefault() {
        // Forward-compat with a newer build's backup: an unknown enum token
        // degrades to the model default instead of sinking the import.
        val payload = fullPayload().put("strategy", "roundRobin")
        val snapshot = decode(payload, mutableListOf())
        assertEquals(RoutingStrategy.fallback, snapshot.strategy.value)
        assertTrue(snapshot.strategy.present)
        // And on the merge path it is applied (present ⇒ backup wins).
        val merged = applyGroupRunConfig(localGroup(), snapshot)
        assertEquals(RoutingStrategy.fallback, merged.strategy)
    }

    @Test
    fun snapshotFieldsCarryPresenceForEveryKey() {
        val snapshot = decode(fullPayload(), mutableListOf())
        assertTrue(snapshot.strategy.present)
        assertTrue(snapshot.fallbackStrategy.present)
        assertTrue(snapshot.defaultThinkingLevel.present)
        assertTrue(snapshot.contextLimitTokens.present)
        assertTrue(snapshot.lastContextLimitTokens.present)

        val legacy = JSONObject().put("name", "work")
        val bare = decode(legacy, mutableListOf())
        assertFalse(bare.strategy.present)
        assertFalse(bare.fallbackStrategy.present)
        assertFalse(bare.defaultThinkingLevel.present)
        assertFalse(bare.contextLimitTokens.present)
        assertFalse(bare.lastContextLimitTokens.present)
    }

    @Test
    fun exportShapeRoundTripsThroughTheSeam() {
        // The export writes enum `.name`s and bare ints; the decode must read
        // exactly that shape back (payload contract, spelled as literals).
        val src = ModelGroup(
            id = "g1",
            name = "work",
            memberEntryIds = mutableListOf("e1"),
            strategy = RoutingStrategy.cheapestFirst,
            fallbackStrategy = FallbackStrategy.always,
            defaultThinkingLevel = ThinkingLevel.XHIGH,
            contextLimitTokens = 131072,
            lastContextLimitTokens = 65536,
        )
        // What buildSections emits for this group (hand-built to the export
        // contract; building it for real needs Android repositories).
        val exported = JSONObject(
            """
            {
              "id": "g1",
              "name": "work",
              "memberEntryIds": ["e1"],
              "strategy": "cheapestFirst",
              "fallbackStrategy": "always",
              "defaultThinkingLevel": "XHIGH",
              "contextLimitTokens": 131072,
              "lastContextLimitTokens": 65536
            }
            """.trimIndent()
        )
        val restored = applyGroupRunConfig(
            ModelGroup(id = "g2", name = "other", memberEntryIds = mutableListOf("z9")),
            decode(exported, mutableListOf()),
        )
        assertEquals(RoutingStrategy.cheapestFirst, restored.strategy)
        assertEquals(FallbackStrategy.always, restored.fallbackStrategy)
        assertEquals(ThinkingLevel.XHIGH, restored.defaultThinkingLevel)
        assertEquals(131072, restored.contextLimitTokens)
        assertEquals(65536, restored.lastContextLimitTokens)
        // The applier never touches identity or members.
        assertEquals("g2", restored.id)
        assertEquals(listOf("z9"), restored.memberEntryIds)
        // Sanity: src and restored agree on all five run-config fields.
        assertEquals(src.strategy, restored.strategy)
        assertEquals(src.fallbackStrategy, restored.fallbackStrategy)
        assertEquals(src.defaultThinkingLevel, restored.defaultThinkingLevel)
        assertEquals(src.contextLimitTokens, restored.contextLimitTokens)
        assertEquals(src.lastContextLimitTokens, restored.lastContextLimitTokens)
    }
}

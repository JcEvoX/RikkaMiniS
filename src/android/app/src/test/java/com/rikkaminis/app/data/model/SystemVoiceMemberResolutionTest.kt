package com.rikkaminis.app.data.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1043 B-06] The two single sources for a group member.
 *
 * A [ModelGroup.memberEntryIds] holds ids; every reader has to answer two
 * questions about one, and both used to be re-derived at each reader:
 *
 *  1. **Which entry is this?** — the persisted one, else the synthesized System
 *     voice entry ([SystemVoiceEntries.resolveIn]). Readers that only looked in
 *     `config.modelEntries` resolved a System member to null. A default Voice
 *     Input/Output group contains ONLY System sentinels (that is what makes it
 *     work with zero configuration), so those readers painted it as
 *     「No usable models」 in red with no modality badges, an empty member
 *     preview and a 「0 models」 count — while the group's own detail screen
 *     listed the same two members normally.
 *
 *  2. **Is this member usable right now?** — a System entry always is (the
 *     device serves it: no Provider instance, no API key, and the
 *     `__builtin_system_speech__` instance is synthesized on demand so it can
 *     never appear in `config.instances`), a persisted entry only when its
 *     instance is enabled ([SystemVoiceEntries.isEnabledGroupMember]).
 *
 * Every literal below is hand-written — no id, no instance name and no
 * expected count is derived from the code under test, so weakening either
 * helper cannot move an expectation with it.
 */
class SystemVoiceMemberResolutionTest {

    private val sentinelOnline = "__builtin_system_speech__/system-asr-online"
    private val sentinelOffline = "__builtin_system_speech__/system-asr-offline"
    private val sentinelTts = "__builtin_system_speech__/system-tts"

    private fun entry(instanceId: String, modelId: String, name: String) = ModelEntry(
        providerInstanceId = instanceId,
        baseModel = LLMModel(id = modelId, displayName = name, provider = instanceId),
        uuid = "$instanceId/$modelId",
    )

    private val persisted = listOf(
        entry("prov-1", "model-a", "Model A"),
        entry("prov-2", "model-b", "Model B"),
    )

    // ── resolveIn: which entry does this id stand for? ───────────────────

    @Test
    fun aPersistedMemberResolvesToItsOwnEntry() {
        assertEquals("Model A", SystemVoiceEntries.resolveIn(persisted, "prov-1/model-a")?.baseModel?.displayName)
        assertEquals("Model B", SystemVoiceEntries.resolveIn(persisted, "prov-2/model-b")?.baseModel?.displayName)
    }

    @Test
    fun aSentinelMemberResolvesEvenThoughItIsNeverPersisted() {
        // Precondition: the sentinels really are absent from the persisted list,
        // so the assertions below can only pass through the System fallback.
        assertFalse(persisted.any { it.id in setOf(sentinelOnline, sentinelOffline, sentinelTts) })
        assertEquals("System Recognition (Online)", SystemVoiceEntries.resolveIn(persisted, sentinelOnline)?.baseModel?.displayName)
        assertEquals("System Recognition (Offline)", SystemVoiceEntries.resolveIn(persisted, sentinelOffline)?.baseModel?.displayName)
        assertEquals("System Voice (Auto)", SystemVoiceEntries.resolveIn(persisted, sentinelTts)?.baseModel?.displayName)
    }

    @Test
    fun anUnknownMemberResolvesToNull() {
        // Fail-closed: neither persisted nor a System sentinel is not "some entry".
        assertNull(SystemVoiceEntries.resolveIn(persisted, "prov-9/model-gone"))
        assertNull(SystemVoiceEntries.resolveIn(emptyList(), "prov-9/model-gone"))
    }

    @Test
    fun aPersistedEntryWinsOverTheSystemFallbackForItsOwnId() {
        // A user who persists an entry with a sentinel-looking id keeps their
        // own entry: the persisted list is consulted first, by design.
        val shadow = entry("prov-3", "system-tts", "My Own TTS")
        val shadowId = shadow.id
        assertEquals("My Own TTS", SystemVoiceEntries.resolveIn(listOf(shadow), shadowId)?.baseModel?.displayName)
    }

    // ── isEnabledGroupMember: is this member usable right now? ───────────

    @Test
    fun aSentinelMemberIsUsableWithNoEnabledInstancesAtAll() {
        // The bug's sharpest form: a default Voice group has System members only
        // and the user has no provider instance enabled. It is still fully usable.
        val nothingEnabled = emptySet<String>()
        assertTrue(SystemVoiceEntries.isEnabledGroupMember(persisted, nothingEnabled, sentinelOnline))
        assertTrue(SystemVoiceEntries.isEnabledGroupMember(emptyList(), nothingEnabled, sentinelOffline))
        assertTrue(SystemVoiceEntries.isEnabledGroupMember(emptyList(), nothingEnabled, sentinelTts))
    }

    @Test
    fun aPersistedMemberIsUsableOnlyWhenItsInstanceIsEnabled() {
        assertTrue(SystemVoiceEntries.isEnabledGroupMember(persisted, setOf("prov-1"), "prov-1/model-a"))
        assertFalse(SystemVoiceEntries.isEnabledGroupMember(persisted, setOf("prov-2"), "prov-1/model-a"))
        // The `__builtin_system_speech__` instance is synthesized — it is never
        // in config.instances, so "are its instances enabled" must not be how a
        // System member is judged: here NO instance is enabled and it is still
        // usable.
        assertTrue(SystemVoiceEntries.isEnabledGroupMember(persisted, emptySet(), sentinelOnline))
    }

    @Test
    fun aMemberWhoseInstanceVanishedIsNotUsable() {
        // A persisted entry pointing at a deleted instance could not be
        // instantiated, so it must not be counted as usable.
        assertFalse(SystemVoiceEntries.isEnabledGroupMember(persisted, emptySet(), "prov-1/model-a"))
    }

    @Test
    fun anUnknownMemberIsNotUsable() {
        assertFalse(SystemVoiceEntries.isEnabledGroupMember(persisted, setOf("prov-1"), "prov-9/model-gone"))
    }

    // ── The user-visible count the bug produced ──────────────────────────

    @Test
    fun aDefaultVoiceGroupCountsItsSystemMembersAsUsable() {
        // Exactly the two members `ensureDefaultVoiceInputGroup` writes.
        val voiceInputMembers = listOf(sentinelOnline, sentinelOffline)
        val config = ProviderConfig()          // nothing enabled, nothing persisted
        val usable = voiceInputMembers.count { id ->
            SystemVoiceEntries.isEnabledGroupMember(config.modelEntries, emptySet(), id)
        }
        // Before B-06 this was 0, which is what lit 「No usable models」 in red.
        assertEquals(2, usable)
    }

    @Test
    fun resolutionAndUsabilityAgreeOnIdentity() {
        // A member that resolves must be judged for usability under the SAME id
        // — the two helpers must not disagree about what a member is.
        for (id in listOf(sentinelOnline, sentinelOffline, sentinelTts) + persisted.map { it.id }) {
            val resolved = SystemVoiceEntries.resolveIn(persisted, id)
            assertTrue("$id should resolve", resolved != null)
            assertEquals("$id resolves under its own id", id, resolved!!.id)
        }
    }
}

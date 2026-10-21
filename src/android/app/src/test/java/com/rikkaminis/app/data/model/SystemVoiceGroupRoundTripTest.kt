package com.rikkaminis.app.data.model

import com.rikkaminis.app.config.ConfigError
import com.rikkaminis.app.config.ConfigValue
import com.rikkaminis.app.config.collections.parseMemberEntryIds
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv20-a1 A-02] The three virtual System voice entries are legitimate MODEL
 * GROUP members, but they are never written to `config.modelEntries` — they
 * are synthesized on demand. Before this batch every config/backup/debug
 * reader of `memberEntryIds` re-derived "known" from `modelEntries` alone, so
 * all five treated the sentinels as unknown models: the backup Stage 2 remap
 * counted them as dropped members, `GroupsCollection.requireKnownEntryIds`
 * refused the write outright, the `groups` config summary emitted a "missing"
 * stub, and the two debug group RPCs silently filtered them out of the member
 * list they wrote and read back.
 *
 * The assertions use hand-written literal ids and labels — none is derived
 * from the code under test, so weakening the predicate cannot move them.
 */
class SystemVoiceGroupRoundTripTest {

    /** Ids that ARE persisted, in the composite `<instance>/<model>` shape the
     *  app writes for ordinary entries. */
    private val persisted = setOf("prov-1/model-a", "prov-2/model-b")

    private val sentinelOnline = "__builtin_system_speech__/system-asr-online"
    private val sentinelOffline = "__builtin_system_speech__/system-asr-offline"
    private val sentinelTts = "__builtin_system_speech__/system-tts"

    // ── The shared membership judgement ──────────────────────────────────

    @Test
    fun theThreeSentinelMembersAreKnownWithoutBeingPersisted() {
        assertFalse("precondition: sentinels are NOT persisted", persisted.any { it.startsWith("__builtin_") })
        assertTrue(SystemVoiceEntries.isKnownGroupMemberId(sentinelOnline, persisted))
        assertTrue(SystemVoiceEntries.isKnownGroupMemberId(sentinelOffline, persisted))
        assertTrue(SystemVoiceEntries.isKnownGroupMemberId(sentinelTts, persisted))
    }

    @Test
    fun theSentinelMembersAreKnownEvenWithNoPersistedEntriesAtAll() {
        assertTrue(SystemVoiceEntries.isKnownGroupMemberId(sentinelOnline, emptySet()))
    }

    @Test
    fun persistedEntryIdsStillPassTheSamePredicate() {
        assertTrue(SystemVoiceEntries.isKnownGroupMemberId("prov-1/model-a", persisted))
        assertTrue(SystemVoiceEntries.isKnownGroupMemberId("prov-2/model-b", persisted))
    }

    @Test
    fun anIdThatIsNeitherPersistedNorSystemIsStillUnknown() {
        // Fail closed — this is the branch that must keep refusing.
        assertFalse(SystemVoiceEntries.isKnownGroupMemberId("ghost/model-z", persisted))
        assertFalse(SystemVoiceEntries.isKnownGroupMemberId("", persisted))
    }

    @Test
    fun anUnrelatedProviderWhoseIdMerelyContainsTheSentinelIsNotSystem() {
        // The prefix test is anchored at the START of the id.
        assertFalse(
            SystemVoiceEntries.isKnownGroupMemberId("prov__builtin_system_speech__/m", persisted),
        )
    }

    // ── The synthesized entries ──────────────────────────────────────────

    @Test
    fun theSentinelMemberIdsAreTheCompositeShapeVoiceGroupsWrite() {
        assertEquals("__builtin_system_speech__/system-asr-online", SystemVoiceEntries.asrOnline.id)
        assertEquals("__builtin_system_speech__/system-asr-offline", SystemVoiceEntries.asrOffline.id)
        assertEquals("__builtin_system_speech__/system-tts", SystemVoiceEntries.tts.id)
    }

    @Test
    fun everySentinelMemberIdResolvesToARealEntry() {
        for (id in listOf(sentinelOnline, sentinelOffline, sentinelTts)) {
            val e = SystemVoiceEntries.resolve(id)
            assertNotNull("$id must resolve", e)
            assertEquals(id, e!!.id)
            assertEquals("__builtin_system_speech__", e.providerInstanceId)
            assertTrue("system entries are hidden from the plain model list", e.isHidden)
        }
    }

    @Test
    fun resolveIsExactWhileMembershipIsPrefixBased() {
        // Documented asymmetry: an unknown model under the sentinel provider is
        // still a System id (so no reader may call it an unknown model), but
        // there is no entry to render — resolve() answers null.
        val ghost = "__builtin_system_speech__/nope"
        assertTrue(SystemVoiceEntries.isKnownGroupMemberId(ghost, persisted))
        assertNull(SystemVoiceEntries.resolve(ghost))
    }

    @Test
    fun displayNamesAndModalitiesAreStable() {
        assertEquals("System Recognition (Online)", SystemVoiceEntries.asrOnline.model.displayName)
        assertEquals("System Recognition (Offline)", SystemVoiceEntries.asrOffline.model.displayName)
        assertEquals("System Voice (Auto)", SystemVoiceEntries.tts.model.displayName)

        assertEquals("system-asr-online", SystemVoiceEntries.asrOnline.baseModel.id)
        assertEquals("system-asr-offline", SystemVoiceEntries.asrOffline.baseModel.id)
        assertEquals("system-tts", SystemVoiceEntries.tts.baseModel.id)

        assertEquals(listOf("audio"), SystemVoiceEntries.asrOnline.model.inputModalities)
        assertEquals(listOf("audio"), SystemVoiceEntries.tts.model.outputModalities)
        assertEquals("system", SystemVoiceEntries.tts.model.provider)
    }

    @Test
    fun syntheticInstanceIsOnDemandAndLabeled() {
        val inst = SystemVoiceEntries.syntheticInstance()
        assertEquals("__builtin_system_speech__", inst.id)
        assertEquals("System", inst.label)
        // Never persisted: calling it twice must not accumulate state.
        assertEquals(inst.id, SystemVoiceEntries.syntheticInstance().id)
    }

    // ── The write face ───────────────────────────────────────────────────

    @Test
    fun groupMemberListsCarrySentinelsThroughTheSharedWriteParser() {
        val ids = parseMemberEntryIds(
            listOf(
                ConfigValue.Str(sentinelOnline),
                ConfigValue.Str("prov-1/model-a"),
                ConfigValue.Str(sentinelTts),
            ),
        )
        assertEquals(listOf(sentinelOnline, "prov-1/model-a", sentinelTts), ids)
        // Every one of them is a member the shared predicate accepts.
        for (id in ids) {
            assertTrue(id, SystemVoiceEntries.isKnownGroupMemberId(id, persisted))
        }
    }

    @Test
    fun theWriteParserStillRefusesEmptyIdsAndDeduplicates() {
        // Fail-closed contract for genuinely bad input is unchanged.
        assertThrows(ConfigError.InvalidValue::class.java) {
            parseMemberEntryIds(listOf(ConfigValue.Str("")))
        }
        assertEquals(
            listOf(sentinelOnline, "prov-1/model-a"),
            parseMemberEntryIds(
                listOf(
                    ConfigValue.Str(sentinelOnline),
                    ConfigValue.Str("prov-1/model-a"),
                    ConfigValue.Str(sentinelOnline),
                ),
            ),
        )
    }
}

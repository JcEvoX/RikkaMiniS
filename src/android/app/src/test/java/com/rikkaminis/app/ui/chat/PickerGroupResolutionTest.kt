package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv25/B-04] JVM tests for the picker's group-resolution decisions.
 *
 * The entry type is a TYPE PARAMETER in [pickerHeaderEntry] /
 * [pickerDisplayMembers], so these tests need neither Compose nor the Android
 * SDK: the persisted-vs-System single source is injected as the `resolve`
 * lambda, and the tests model it with plain ids.
 *
 * The case that matters is the SELECTED group whose active entry is a System
 * sentinel: a default Voice Input/Output group is built from sentinels alone,
 * so the persisted-only lookup resolved it to null and the header dropped to
 * 「N models」 / 「No linked models」 while the member rows right below it
 * listed the very same entry.
 */
class PickerGroupResolutionTest {

    /** The single source under test: persisted ids first, System sentinels second. */
    private fun resolve(persisted: List<String>, system: List<String>): (String) -> String? =
        { id -> if (id in persisted) id else if (id in system) id else null }

    private val voiceSentinel = "__builtin_system_speech__/system-asr-online"
    private val persistedModel = "gpt-4o"

    // ── pickerHeaderEntry ───────────────────────────────────────────────────

    @Test fun `selected group with a sentinel active entry resolves the sentinel`() {
        val r = resolve(persisted = emptyList(), system = listOf(voiceSentinel))
        val entry = pickerHeaderEntry(
            isSelected = true,
            activeEntryId = voiceSentinel,
            memberEntryIds = listOf(voiceSentinel),
            resolve = r,
        )
        assertEquals(voiceSentinel, entry)
    }

    @Test fun `selected group with a persisted active entry resolves the persisted entry`() {
        val r = resolve(persisted = listOf(persistedModel), system = listOf(voiceSentinel))
        val entry = pickerHeaderEntry(
            isSelected = true,
            activeEntryId = persistedModel,
            memberEntryIds = listOf(persistedModel, voiceSentinel),
            resolve = r,
        )
        assertEquals(persistedModel, entry)
    }

    @Test fun `selected group with an unresolvable active entry falls back to the first member`() {
        // The pre-fix spelling had NO fallback on this branch: the elvis bound
        // to the `else` branch alone, so the selected group rendered null.
        val r = resolve(persisted = listOf(persistedModel), system = listOf(voiceSentinel))
        val entry = pickerHeaderEntry(
            isSelected = true,
            activeEntryId = "stale-id-that-no-longer-exists",
            memberEntryIds = listOf(persistedModel, voiceSentinel),
            resolve = r,
        )
        assertEquals(persistedModel, entry)
    }

    @Test fun `selected group whose members are all sentinels falls back to a sentinel`() {
        val r = resolve(persisted = emptyList(), system = listOf(voiceSentinel))
        val entry = pickerHeaderEntry(
            isSelected = true,
            activeEntryId = "stale-id",
            memberEntryIds = listOf(voiceSentinel),
            resolve = r,
        )
        assertEquals(voiceSentinel, entry)
    }

    @Test fun `unselected group previews its first resolvable member`() {
        val r = resolve(persisted = listOf(persistedModel), system = listOf(voiceSentinel))
        val entry = pickerHeaderEntry(
            isSelected = false,
            activeEntryId = persistedModel,
            memberEntryIds = listOf(voiceSentinel, persistedModel),
            resolve = r,
        )
        assertEquals(voiceSentinel, entry)
    }

    @Test fun `null active entry never resolves`() {
        val r = resolve(persisted = listOf(persistedModel), system = listOf(voiceSentinel))
        val entry = pickerHeaderEntry(
            isSelected = true,
            activeEntryId = null,
            memberEntryIds = listOf(persistedModel),
            resolve = r,
        )
        assertEquals(persistedModel, entry)
    }

    @Test fun `a group with no resolvable member resolves to null`() {
        val r = resolve(persisted = emptyList(), system = emptyList())
        val entry = pickerHeaderEntry(
            isSelected = true,
            activeEntryId = "stale",
            memberEntryIds = listOf("stale-too"),
            resolve = r,
        )
        assertNull(entry)
    }

    // ── pickerDisplayMembers ────────────────────────────────────────────────

    @Test fun `members are shown as resolved`() {
        val r = resolve(persisted = listOf(persistedModel), system = listOf(voiceSentinel))
        val members = pickerDisplayMembers(
            resolvedMembers = listOf(persistedModel, voiceSentinel),
            isSelected = true,
            activeEntryId = persistedModel,
            resolve = r,
        )
        assertEquals(listOf(persistedModel, voiceSentinel), members)
    }

    @Test fun `an empty member list falls back to the sentinel active entry`() {
        // The pre-fix fallback was `modelEntries`-only, so a sentinel active
        // entry resolved to null and the rows rendered 「No linked models」.
        val r = resolve(persisted = emptyList(), system = listOf(voiceSentinel))
        val members = pickerDisplayMembers(
            resolvedMembers = emptyList(),
            isSelected = true,
            activeEntryId = voiceSentinel,
            resolve = r,
        )
        assertEquals(listOf(voiceSentinel), members)
    }

    @Test fun `an empty member list on an unselected group stays empty`() {
        val r = resolve(persisted = emptyList(), system = listOf(voiceSentinel))
        val members = pickerDisplayMembers(
            resolvedMembers = emptyList(),
            isSelected = false,
            activeEntryId = voiceSentinel,
            resolve = r,
        )
        assertTrue(members.isEmpty())
    }
}

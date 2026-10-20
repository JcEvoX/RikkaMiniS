package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1034 FIX-B3 ISSUE-06] JVM tests for
 * [mergeSessionOverrides] — the seeding merge behind `SessionSkillsSheet`'s
 * per-session override map.
 *
 * The regression: the map was built inside `remember(skills, sessionId)` with
 * an unconditional `put` per skill, so (a) every skills StateFlow emission
 * rebuilt the whole map and re-ran one `isEnabledForSession` SQLite read per
 * skill *inside composition*, and (b) a toggle the user had just flipped in
 * the still-open sheet was thrown away — the row snapped back to the stored
 * value until the write landed. Both halves are pinned below: the merge must
 * never overwrite a key it already has, and it must not consult storage for
 * keys it already has.
 *
 * Expected values are literals, never derived from the production constants —
 * a mutated `!in` guard or a dropped `retainAll` has to make a test fail.
 */
class SessionSkillOverrideMergeTest {

    private fun stored(vararg pairs: Pair<String, Boolean>): (String) -> Boolean {
        val table = pairs.toMap()
        return { id -> table.getValue(id) }
    }

    @Test fun `seeds every missing id from storage`() {
        val overrides = mutableMapOf<String, Boolean>()
        var calls = 0
        mergeSessionOverrides(
            liveIds = listOf("alpha", "beta"),
            overrides = overrides,
            lookup = { calls++; stored("alpha" to true, "beta" to false)(it) },
        )
        assertEquals(mapOf("alpha" to true, "beta" to false), overrides)
        assertEquals(2, calls)
    }

    @Test fun `never overwrites an already-seeded id`() {
        // The user just flipped alpha ON in the open sheet; storage still says
        // OFF because the write has not landed yet. A rebuild would revert it.
        val overrides = mutableMapOf("alpha" to true)
        mergeSessionOverrides(
            liveIds = listOf("alpha", "beta"),
            overrides = overrides,
            lookup = stored("alpha" to false, "beta" to false),
        )
        assertEquals(mapOf("alpha" to true, "beta" to false), overrides)
    }

    @Test fun `does not consult storage for ids it already has`() {
        val overrides = mutableMapOf("alpha" to true)
        val consulted = mutableListOf<String>()
        mergeSessionOverrides(
            liveIds = listOf("alpha"),
            overrides = overrides,
            lookup = { consulted.add(it); false },
        )
        assertTrue("lookup must not be called for a present key: $consulted", consulted.isEmpty())
    }

    @Test fun `drops ids that vanished from the live list`() {
        val overrides = mutableMapOf("alpha" to true, "beta" to true)
        mergeSessionOverrides(
            liveIds = listOf("alpha"),
            overrides = overrides,
            lookup = stored("alpha" to false),
        )
        assertEquals(mapOf("alpha" to true), overrides)
    }

    @Test fun `re-added id is re-seeded from storage, not from the stale toggle`() {
        val overrides = mutableMapOf<String, Boolean>()
        mergeSessionOverrides(listOf("alpha"), overrides, stored("alpha" to true))
        assertEquals(mapOf("alpha" to true), overrides)
        // alpha disappears (uninstalled skill) …
        mergeSessionOverrides(listOf("beta"), overrides, stored("beta" to false))
        assertEquals(mapOf("beta" to false), overrides)
        // … then comes back with storage now saying OFF.
        mergeSessionOverrides(listOf("alpha", "beta"), overrides, stored("alpha" to false, "beta" to false))
        assertEquals(mapOf("alpha" to false, "beta" to false), overrides)
    }

    @Test fun `empty live list clears the map`() {
        val overrides = mutableMapOf("alpha" to true)
        mergeSessionOverrides(emptyList(), overrides, lookup = { error("must not be consulted") })
        assertTrue(overrides.isEmpty())
    }

    @Test fun `lookup ids arrive in live-list order`() {
        val overrides = mutableMapOf<String, Boolean>()
        val consulted = mutableListOf<String>()
        mergeSessionOverrides(
            liveIds = listOf("gamma", "alpha", "beta"),
            overrides = overrides,
            lookup = { consulted.add(it); true },
        )
        assertEquals(listOf("gamma", "alpha", "beta"), consulted)
        assertEquals(listOf("gamma", "alpha", "beta"), overrides.keys.toList())
        assertEquals(3, overrides.size)
        assertFalse(overrides.values.any { !it })
    }
}

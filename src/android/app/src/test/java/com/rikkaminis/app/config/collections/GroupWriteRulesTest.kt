package com.rikkaminis.app.config.collections

import com.rikkaminis.app.config.ConfigError
import com.rikkaminis.app.config.ConfigValue
import com.rikkaminis.app.data.model.FallbackStrategy
import com.rikkaminis.app.data.model.ModelGroup
import com.rikkaminis.app.data.model.RoutingStrategy
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertSame
import org.junit.Assert.fail
import org.junit.Test

/**
 * [fix/da1023-D / ISSUE-04] Drives the extracted pure write rules of
 * GroupsCollection (GroupWriteRules.kt). ProviderRepository is Context-bound
 * and unconstructible on the JVM, so the collection delegates its
 * copy-on-mutate core, its member-entry-id decode and its context-limit
 * write semantics to top-level internal functions, tested here with
 * hard-coded expectations. The mutate → helper → repo.updateGroup wiring is
 * pinned by scripts/scan/collection_copy_on_mutate_check.py.
 */
class GroupWriteRulesTest {

    private fun group() = ModelGroup(
        name = "G",
        memberEntryIds = mutableListOf("e-uuid-1", "e-uuid-2"),
        strategy = RoutingStrategy.fallback,
        fallbackStrategy = FallbackStrategy.default,
    )

    // ① copy-on-mutate: the edit lands on a detached copy; the live object
    // the caller fetched is untouched (before/after snapshot).
    @Test
    fun `mutatedGroupCopy applies to a detached copy and leaves the original untouched`() {
        val original = group()
        val membersBefore = original.memberEntryIds.toList()
        var observed: ModelGroup? = null
        val working = mutatedGroupCopy(original) { g ->
            observed = g
            g.memberEntryIds.clear()
            g.memberEntryIds.add("e-uuid-9")
            g.contextLimitTokens = 32000
            g.lastContextLimitTokens = 32000
        }
        // 对拍前后快照 — the LIVE object is untouched
        assertEquals(listOf("e-uuid-1", "e-uuid-2"), original.memberEntryIds)
        assertEquals(membersBefore, original.memberEntryIds)
        assertEquals(null as Int?, original.contextLimitTokens)
        // the edit landed on the copy
        assertEquals(listOf("e-uuid-9"), working.memberEntryIds)
        assertEquals(Integer.valueOf(32000), working.contextLimitTokens)
        // the persisted object IS the working copy
        assertSame(working, observed)
        assertNotSame(original.memberEntryIds, working.memberEntryIds)
    }

    // ② decode: duplicates collapse to their first occurrence.
    @Test
    fun `parseMemberEntryIds keeps first occurrence of duplicates`() {
        val ids = parseMemberEntryIds(
            listOf(
                ConfigValue.Str("e-a"),
                ConfigValue.Str("e-b"),
                ConfigValue.Str("e-a"),
            )
        )
        assertEquals(listOf("e-a", "e-b"), ids)
    }

    // ② decode: a non-string element is malformed input, not "absent".
    @Test
    fun `parseMemberEntryIds refuses non-string elements instead of dropping them`() {
        try {
            parseMemberEntryIds(
                listOf(
                    ConfigValue.Str("e-a"),
                    ConfigValue.Int(1),
                )
            )
            fail("expected ConfigError.TypeMismatch")
        } catch (expected: ConfigError.TypeMismatch) {
            // expected — the old inline mapNotNull silently dropped it
        }
    }

    // ② decode: an empty id is refused at the single decode point.
    @Test
    fun `parseMemberEntryIds refuses empty ids`() {
        try {
            parseMemberEntryIds(listOf(ConfigValue.Str("")))
            fail("expected ConfigError.InvalidValue")
        } catch (expected: ConfigError.InvalidValue) {
            // expected
        }
    }

    @Test
    fun `parseMemberEntryIds preserves order for plain input`() {
        val ids = parseMemberEntryIds(
            listOf(
                ConfigValue.Str("e-x"),
                ConfigValue.Str("e-y"),
            )
        )
        assertEquals(listOf("e-x", "e-y"), ids)
    }

    // ③ context-limit write: a positive value also refreshes the
    // "last selected" memory the UI restores on an OFF→ON toggle.
    @Test
    fun `applyContextLimitWrite syncs lastContextLimitTokens on positive value`() {
        val g = group()
        applyContextLimitWrite(g, 32000)
        assertEquals(Integer.valueOf(32000), g.contextLimitTokens)
        assertEquals(Integer.valueOf(32000), g.lastContextLimitTokens)
    }

    // ③ context-limit write: clearing (0) keeps the memory.
    @Test
    fun `applyContextLimitWrite clear keeps lastContextLimitTokens for UI restore`() {
        val g = group()
        g.contextLimitTokens = 64000
        g.lastContextLimitTokens = 64000
        applyContextLimitWrite(g, 0)
        assertEquals(null as Int?, g.contextLimitTokens)
        assertEquals(Integer.valueOf(64000), g.lastContextLimitTokens)
    }

    // [fix/dv16-a A-06] The RPC groups.update face routes its context-limit
    // clear through the same composition the collection uses (mutatedGroupCopy
    // + applyContextLimitWrite) — pin the composition: clearing on the working
    // copy keeps the memory, and the live object the caller fetched is
    // untouched. Before the fix the update face nulled lastContextLimitTokens,
    // so a CLI write + an OFF→ON toggle round-trip snapped back to the 128K
    // default instead of the last value the user set.
    @Test
    fun `clear via mutatedGroupCopy keeps the memory the update face restores`() {
        val g = group()
        g.contextLimitTokens = 64000
        g.lastContextLimitTokens = 64000
        val working = mutatedGroupCopy(g) { applyContextLimitWrite(it, 0) }
        assertEquals(null as Int?, working.contextLimitTokens)
        assertEquals(Integer.valueOf(64000), working.lastContextLimitTokens)
        // the live object is untouched (copy-on-mutate contract)
        assertEquals(Integer.valueOf(64000), g.contextLimitTokens)
        assertEquals(Integer.valueOf(64000), g.lastContextLimitTokens)
    }
}

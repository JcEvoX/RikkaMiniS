package com.rikkaminis.app.ui.settings

import com.rikkaminis.app.data.model.LLMModel
import com.rikkaminis.app.data.model.ModelEntry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1039-B3 B-08] Regression tests for the Quick Test target resolution.
 *
 * Both screens that host the shared `modelEntryPickerItems` (AddModelsToGroupScreen
 * and the agent-loop add sheet) used to capture the tapped `ModelEntry` in a
 * `remember { mutableStateOf<ModelEntry?>(null) }`. The sheet stays open across
 * external writes (minis-config RPC, Debug RPC, backup restore, delete), so
 * "Run again" kept hitting the config the row had at tap time, and deleting the
 * model left the sheet testing a row that no longer exists.
 *
 * [liveQuickTestEntry] resolves the id against the live list on every
 * composition; [quickTestTargetVanished] is the separate close decision so the
 * effect keys on "the target is gone" rather than "the lookup is null" (which is
 * also true when the sheet was never opened). Expectations are hand-written
 * literals, never derived from the helpers under test.
 */
class QuickTestTargetResolutionTest {

    private fun entry(uuid: String, displayName: String = "model", hidden: Boolean = false) = ModelEntry(
        providerInstanceId = "inst-1",
        baseModel = LLMModel(id = "m-$uuid", displayName = displayName, provider = "TestProvider"),
        isHidden = hidden,
        uuid = uuid,
    )

    @Test
    fun `no pending id resolves to nothing`() {
        assertNull(liveQuickTestEntry(null, listOf(entry("a"))))
    }

    @Test
    fun `the pending id resolves to the live object`() {
        val live = entry("b", "Beta")
        assertSame(live, liveQuickTestEntry("b", listOf(entry("a"), live)))
    }

    @Test
    fun `resolution follows an external rewrite instead of the tapped copy`() {
        val tapped = entry("a", "Old name")
        val rewritten = entry("a", "New name")

        assertSame(tapped, liveQuickTestEntry("a", listOf(tapped)))
        val resolvedAfterExternalWrite = liveQuickTestEntry("a", listOf(rewritten))

        assertSame(rewritten, resolvedAfterExternalWrite)
        // The pre-fix snapshot kept handing `tapped` back here, so "Run again"
        // tested the config the row had when the lightning bolt was tapped.
        assertNotSame(tapped, resolvedAfterExternalWrite)
        assertEquals("New name", resolvedAfterExternalWrite?.baseModel?.displayName)
    }

    @Test
    fun `a hidden entry is still resolvable - Quick Test is a user action`() {
        val hidden = entry("c", hidden = true)
        assertSame(hidden, liveQuickTestEntry("c", listOf(hidden)))
    }

    @Test
    fun `a deleted id stops resolving`() {
        assertNull(liveQuickTestEntry("gone", listOf(entry("a"), entry("b"))))
        assertNull(liveQuickTestEntry("gone", emptyList()))
    }

    @Test
    fun `an unopened sheet is never treated as vanished`() {
        assertFalse(quickTestTargetVanished(null, listOf(entry("a"))))
        assertFalse(quickTestTargetVanished(null, emptyList()))
    }

    @Test
    fun `a resolving id keeps the sheet open`() {
        assertFalse(quickTestTargetVanished("a", listOf(entry("a"))))
    }

    @Test
    fun `an id that no longer resolves closes the sheet`() {
        assertTrue(quickTestTargetVanished("a", emptyList()))
        assertTrue(quickTestTargetVanished("a", listOf(entry("b"))))
    }

    @Test
    fun `vanished and resolving are complementary only while an id is pending`() {
        val entries = listOf(entry("a"), entry("b"))
        for (id in listOf("a", "gone")) {
            val resolved = liveQuickTestEntry(id, entries)
            assertEquals(resolved == null, quickTestTargetVanished(id, entries))
        }
    }
}

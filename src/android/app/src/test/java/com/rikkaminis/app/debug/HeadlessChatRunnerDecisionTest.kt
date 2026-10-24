package com.rikkaminis.app.debug

import com.rikkaminis.app.data.model.LLMModel
import com.rikkaminis.app.data.model.ModelEntry
import com.rikkaminis.app.data.model.ProviderCredential
import com.rikkaminis.app.data.model.ProviderInstance
import com.rikkaminis.app.data.model.ProviderType
import com.rikkaminis.app.ui.chat.SessionModelResolution
import com.rikkaminis.app.ui.chat.UnusableModelReason
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [dv29-A2] The decisions behind the debug/IPC model surfaces, pinned as
 * literals.
 *
 * Three callers received a *result they could not distinguish from success*
 * because the decision they needed was made somewhere else:
 *
 *  1. `applyModelOverride` — the binding write accepted an entry whose provider
 *     was disabled or had no API key (it checked `isEnabled` only) and accepted
 *     any group at all; `restoreFromBinding` then refused the very same binding
 *     and the resident VM fell back to the default model, while the RPC returned
 *     the refused model's display name.
 *  2. `selectModel` — `ChatViewModel.selectEntry` REFUSES silently (a UI
 *     contract), so returning `vm.modelName.value` after the call answered with
 *     the PREVIOUS model next to the caller's rejected id.
 *  3. `compact` — `isCompacting` flipping `true → false` only means "the job
 *     finished": a provider exception, an empty summary and a stale anchor all
 *     end there with no marker written, and all three used to report
 *     `Completed`.
 *
 * The first two now share `decideSessionModelResolution`, so what is pinned here
 * is the *translation* of that decision into an RPC answer — [bindingRejectReason]
 * and [HeadlessChatRunner.firstUsableGroupEntry]. The third is pinned as the pure
 * [HeadlessChatRunner.settleCompactOutcome]. `HeadlessChatRunner` itself needs a
 * Context and a ViewModel, so the wiring is pinned by
 * `Dv29HeadlessWiringShapeTest`.
 */
class HeadlessChatRunnerDecisionTest {

    private fun model(id: String) = LLMModel(id = id, displayName = id, provider = "openai")

    private fun entry(uuid: String, instanceId: String) =
        ModelEntry(providerInstanceId = instanceId, baseModel = model("m-$uuid"), uuid = uuid)

    private fun instance(id: String, enabled: Boolean) = ProviderInstance(
        id = id,
        label = id,
        providerType = ProviderType.openAI,
        credentialType = ProviderCredential.apiKey,
        isEnabled = enabled,
    )

    // ── 1. binding refuse reasons ────────────────────────────────────────────

    @Test
    fun `an unusable decision yields its reason token`() {
        assertEquals(
            "PROVIDER_DISABLED",
            HeadlessChatRunner.bindingRejectReason(
                SessionModelResolution.Unusable(UnusableModelReason.PROVIDER_DISABLED),
            ),
        )
        assertEquals(
            "MISSING_API_KEY",
            HeadlessChatRunner.bindingRejectReason(
                SessionModelResolution.Unusable(UnusableModelReason.MISSING_API_KEY),
            ),
        )
        assertEquals(
            "MISSING_INSTANCE",
            HeadlessChatRunner.bindingRejectReason(
                SessionModelResolution.Unusable(UnusableModelReason.MISSING_INSTANCE),
            ),
        )
    }

    @Test
    fun `a usable decision yields no reject reason`() {
        val e = entry("e1", "p1")
        val resolution = SessionModelResolution.Use(e, instance("p1", true), "sk-x")
        assertNull(HeadlessChatRunner.bindingRejectReason(resolution))
    }

    // ── 2. group member selection ───────────────────────────────────────────

    @Test
    fun `group selection skips a disabled instance and a key-less one`() {
        val disabled = entry("e-disabled", "p-disabled")
        val keyless = entry("e-keyless", "p-keyless")
        val usable = entry("e-usable", "p-usable")
        val instances = mapOf(
            "p-disabled" to instance("p-disabled", false),
            "p-keyless" to instance("p-keyless", true),
            "p-usable" to instance("p-usable", true),
        )
        val keys = mapOf("p-disabled" to "sk-d", "p-keyless" to null, "p-usable" to "sk-u")

        val picked = HeadlessChatRunner.firstUsableGroupEntry(
            members = listOf(disabled, keyless, usable),
            instanceOf = { instances[it] },
            apiKeyOf = { keys[it] },
        )

        // Literal identity, not "the last one": the ORDER is what the defect got
        // wrong (`memberEntryIds.first()` = the head member whatever it is).
        assertEquals("e-usable", picked?.uuid)
        assertEquals("m-e-usable", picked?.baseModel?.id)
    }

    @Test
    fun `a group whose every member is unusable selects nothing`() {
        val a = entry("e-a", "p-off")
        val b = entry("e-b", "p-nokey")
        val instances = mapOf(
            "p-off" to instance("p-off", false),
            "p-nokey" to instance("p-nokey", true),
        )
        val picked = HeadlessChatRunner.firstUsableGroupEntry(
            members = listOf(a, b),
            instanceOf = { instances[it] },
            apiKeyOf = { if (it == "p-nokey") null else "sk" },
        )
        assertNull(picked)
    }

    @Test
    fun `an empty group selects nothing`() {
        assertNull(
            HeadlessChatRunner.firstUsableGroupEntry(
                members = emptyList(),
                instanceOf = { null },
                apiKeyOf = { null },
            ),
        )
    }

    // ── 3. compact settlement ───────────────────────────────────────────────

    @Test
    fun `compact settles Completed only on evidence`() {
        val byMarker = HeadlessChatRunner.settleCompactOutcome(
            finished = true, markerLanded = true, summaryChanged = false,
        )
        assertEquals("Completed", byMarker.status)
        assertNull(byMarker.error)

        val bySummary = HeadlessChatRunner.settleCompactOutcome(
            finished = true, markerLanded = false, summaryChanged = true,
        )
        assertEquals("Completed", bySummary.status)
        assertNull(bySummary.error)
    }

    @Test
    fun `compact with neither marker nor summary change is an error, not Completed`() {
        val settled = HeadlessChatRunner.settleCompactOutcome(
            finished = true, markerLanded = false, summaryChanged = false,
        )
        assertEquals("Error", settled.status)
        assertEquals("compact_failed:nothing_folded", settled.error)
    }

    @Test
    fun `an unfinished compact stays Timeout and never claims success`() {
        val settled = HeadlessChatRunner.settleCompactOutcome(
            finished = false, markerLanded = true, summaryChanged = true,
        )
        assertEquals("Timeout", settled.status)
        assertNull(settled.error)
    }
}

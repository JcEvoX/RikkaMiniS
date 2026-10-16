package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.data.model.LLMModel
import com.rikkaminis.app.data.model.ModelEntry
import com.rikkaminis.app.data.model.ProviderCredential
import com.rikkaminis.app.data.model.ProviderInstance
import com.rikkaminis.app.data.model.ProviderType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1019-FIX-E] Locks the contract of [decideSessionModelResolution] — the
 * single decision point for the three session-model resolution sites
 * (restoreFromBinding "entry" branch / loadSession priority 2 /
 * applyNewChatDefaultModel):
 *
 *  1. a provider the user disabled is NEVER usable, no matter what else is
 *     present (this is the gate the three sites used to miss);
 *  2. a missing credential makes the whole triple unusable — callers must not
 *     half-apply (write the model name / activeEntryId but leave
 *     currentProvider null);
 *  3. the check order is instance → enabled → credential, so a disabled
 *     provider with ALSO a missing key reports PROVIDER_DISABLED.
 *
 * Expected values are hardcoded literals — never derived from the enum under
 * test (self-referential-expectation trap).
 */
class SessionModelResolutionTest {

    private fun entry() = ModelEntry(
        providerInstanceId = "inst-1",
        baseModel = LLMModel(id = "gpt-x", displayName = "GPT X", provider = "openai"),
    )

    private fun instance(enabled: Boolean) = ProviderInstance(
        id = "inst-1",
        label = "Relay",
        providerType = ProviderType.openAI,
        credentialType = ProviderCredential.apiKey,
        isEnabled = enabled,
    )

    private fun reasonOf(decision: SessionModelResolution): UnusableModelReason =
        (decision as SessionModelResolution.Unusable).reason

    @Test
    fun disabledProviderIsUnusableEvenWithApiKey() {
        val decision = decideSessionModelResolution(entry(), instance(enabled = false), "key")
        assertTrue(decision is SessionModelResolution.Unusable)
        assertEquals(UnusableModelReason.PROVIDER_DISABLED, reasonOf(decision))
    }

    @Test
    fun missingInstanceIsUnusableEvenWithApiKey() {
        val decision = decideSessionModelResolution(entry(), null, "key")
        assertTrue(decision is SessionModelResolution.Unusable)
        assertEquals(UnusableModelReason.MISSING_INSTANCE, reasonOf(decision))
    }

    @Test
    fun enabledProviderWithoutApiKeyIsUnusable() {
        val decision = decideSessionModelResolution(entry(), instance(enabled = true), null)
        assertTrue(decision is SessionModelResolution.Unusable)
        assertEquals(UnusableModelReason.MISSING_API_KEY, reasonOf(decision))
    }

    @Test
    fun disabledBeatsMissingApiKeyInDecisionOrder() {
        // Order pin: the enabled check must run BEFORE the credential check,
        // otherwise the log line / telemetry blames a stale credential for what
        // is really the user's disable switch.
        val decision = decideSessionModelResolution(entry(), instance(enabled = false), null)
        assertEquals(UnusableModelReason.PROVIDER_DISABLED, reasonOf(decision))
    }

    @Test
    fun fullyUsableTripleIsUseAndCarriesTheSameParts() {
        val entry = entry()
        val instance = instance(enabled = true)
        val decision = decideSessionModelResolution(entry, instance, "key")
        assertTrue(decision is SessionModelResolution.Use)
        decision as SessionModelResolution.Use
        assertSame(entry, decision.entry)
        assertSame(instance, decision.instance)
        assertSame("key", decision.apiKey)
    }
}

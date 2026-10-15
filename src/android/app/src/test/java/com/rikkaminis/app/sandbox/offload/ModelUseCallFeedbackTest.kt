package com.rikkaminis.app.sandbox.offload

import com.rikkaminis.app.data.model.ProviderType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/da1015-modeluse-proc] The two pure answers `:app` needs to run the CLI
 * without building a provider: which provider shape an instance type maps to,
 * and what happens to the warnings the caller side collected.
 *
 * ## Why these live here (and why they are worth a test)
 *
 * `minis-model-use run` used to build a provider in `:app` purely to type-test
 * it (`provider is OpenAIProvider`, twice: the `input_audio` gate and the
 * passthrough gate). Building it was not free — `ProviderFactory.create` draws
 * a key from `KeyRoulette` (LRU state + a non-atomic rewrite of
 * `key_roulette.json`) and fires a `ConnectionWarmer` TLS/DNS warm-up — and the
 * request itself is sent by `:modelservice`, which builds its own provider. So
 * every run drew two keys and warmed the origin twice, the second draw being
 * the one that actually reaches the wire; the rotation was being driven by
 * draws nobody used.
 *
 * The replacement is a pure predicate over `ProviderType`, which duplicates
 * knowledge that lives in `ProviderFactory.create`'s `when` — the classic
 * mirror-drift shape this file's module exists to prevent. Two guards keep them
 * together: the predicate is EXHAUSTIVE (a new `ProviderType` is a compile
 * error), and the expectations below are literals that pin the whole domain
 * (a new constant or a flipped arm fails here). Every expectation is a literal,
 * not derived from the implementation.
 */
class ModelUseCallFeedbackTest {

    // ── caller-side warnings must survive the remote result ──────────────

    @Test
    fun `caller warnings survive a result that has warnings of its own`() {
        // Pre-fix this pairing returned ["c"] — the remote branch assigned
        // `callWarnings = remoteWarnings`, discarding the caller's list.
        val merged = ModelUseProtocol.mergeCallWarnings(listOf("a", "b"), listOf("c"))
        assertEquals(listOf("a", "b", "c"), merged)
    }

    @Test
    fun `caller warnings survive a result with no warnings at all`() {
        // Guards the "only merges when both sides are non-empty" mis-fix.
        val merged = ModelUseProtocol.mergeCallWarnings(listOf("a"), emptyList())
        assertEquals(listOf("a"), merged)
    }

    @Test
    fun `a warning both processes produce is reported once`() {
        // Both processes run ModelUseProtocol.parseChatExtraBody, so an
        // `extra_body` type mismatch arrives with identical text twice. The
        // rule is "de-duplicate on first occurrence", matching what
        // attachCallFeedback does to the JSON body.
        val merged = ModelUseProtocol.mergeCallWarnings(listOf("a", "b"), listOf("b", "c"))
        assertEquals(listOf("a", "b", "c"), merged)
    }

    @Test
    fun `no warnings on either side yields no warnings`() {
        assertTrue(ModelUseProtocol.mergeCallWarnings(emptyList(), emptyList()).isEmpty())
    }

    @Test
    fun `remote-only warnings still pass through`() {
        val merged = ModelUseProtocol.mergeCallWarnings(emptyList(), listOf("c"))
        assertEquals(listOf("c"), merged)
    }

    // ── the OpenAI-compatible gate, over its whole domain ────────────────

    @Test
    fun `openai-compatible types are exactly the four the factory builds as OpenAIProvider`() {
        // Literals, read off ProviderFactory.create's type→class mapping:
        //   anthropic -> AnthropicProvider   gemini -> GeminiProvider
        //   openAI / openRouter / xAI / kimiCode -> OpenAIProvider
        // If this fails, either an arm was flipped (fix the predicate) or a
        // ProviderType was added (check which class the factory now builds for
        // it, then update BOTH the predicate and this list).
        assertTrue(ModelUseProtocol.isOpenAiCompatibleProviderType(ProviderType.openAI))
        assertTrue(ModelUseProtocol.isOpenAiCompatibleProviderType(ProviderType.openRouter))
        assertTrue(ModelUseProtocol.isOpenAiCompatibleProviderType(ProviderType.xAI))
        assertTrue(ModelUseProtocol.isOpenAiCompatibleProviderType(ProviderType.kimiCode))
        assertFalse(ModelUseProtocol.isOpenAiCompatibleProviderType(ProviderType.anthropic))
        assertFalse(ModelUseProtocol.isOpenAiCompatibleProviderType(ProviderType.gemini))
    }

    @Test
    fun `the compatible set covers every ProviderType and matches it exactly`() {
        val actual = ProviderType.entries
            .filter { ModelUseProtocol.isOpenAiCompatibleProviderType(it) }
        assertEquals(
            "the OpenAI-compatible set changed — check ProviderFactory.create's mapping for " +
                "the affected type before updating this expectation",
            listOf(ProviderType.openAI, ProviderType.openRouter, ProviderType.xAI, ProviderType.kimiCode),
            actual,
        )
        // Pinned so a NEW ProviderType cannot slip through unconsidered: the
        // predicate would still answer (its `when` has no `else`, so adding a
        // constant is a compile error too), but the class the factory picks for
        // it is a decision someone has to make.
        assertEquals(
            "ProviderType gained or lost a constant — update the predicate and this expectation",
            6,
            ProviderType.entries.size,
        )
    }
}

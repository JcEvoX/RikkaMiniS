package com.rikkaminis.app.config.collections

import com.rikkaminis.app.config.ConfigError
import com.rikkaminis.app.data.model.ProviderCredential
import com.rikkaminis.app.data.model.ProviderInstance
import com.rikkaminis.app.data.model.ProviderType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * [deep-audit-1039 A-04] JVM tests for customBaseURL normalization (write side)
 * and trimming (read side).
 *
 * Every expected value is written as a LITERAL — never derived from the code
 * under test — so a semantics mutation cannot masquerade as PASS by
 * re-deriving its own expectation (the JVM-device self-reference trap).
 */
class CustomBaseURLNormalizationTest {

    private fun inst(
        type: ProviderType = ProviderType.openAI,
        base: String? = null,
    ): ProviderInstance = ProviderInstance(
        id = "inst-1",
        label = "Test Provider",
        providerType = type,
        credentialType = ProviderCredential.apiKey,
        customBaseURL = base,
    )

    // ---- isCustomBaseURLWriteAllowed: the guard's direction is unchanged ----

    @Test
    fun guard_blankStaysAllowedForOpenRouter() {
        // Blank is a clear, not a write — the guard must keep allowing it.
        assertTrue(isCustomBaseURLWriteAllowed(ProviderType.openRouter, null))
        assertTrue(isCustomBaseURLWriteAllowed(ProviderType.openRouter, ""))
        assertTrue(isCustomBaseURLWriteAllowed(ProviderType.openRouter, "   "))
    }

    @Test
    fun guard_paddedValueStillRefusedForOpenRouter() {
        assertEquals(false, isCustomBaseURLWriteAllowed(ProviderType.openRouter, "  https://x  "))
    }

    // ---- resolveAddCustomBaseURL: the add-payload write face ----

    @Test
    fun resolveAdd_paddedURLIsTrimmed() {
        assertEquals(
            "https://api.example.com",
            resolveAddCustomBaseURL("  https://api.example.com  ", ProviderType.openAI),
        )
    }

    @Test
    fun resolveAdd_blankBecomesNullForEveryType() {
        assertNull(resolveAddCustomBaseURL(null, ProviderType.openAI))
        assertNull(resolveAddCustomBaseURL("", ProviderType.openAI))
        assertNull(resolveAddCustomBaseURL("   ", ProviderType.openAI))
        // openRouter keeps working for a bare create (blank ⇒ absent).
        assertNull(resolveAddCustomBaseURL("   ", ProviderType.openRouter))
    }

    @Test
    fun resolveAdd_paddedValueOnOpenRouterThrows() {
        try {
            resolveAddCustomBaseURL("  https://openrouter-ish.example  ", ProviderType.openRouter)
            fail("expected ConfigError.InvalidValue")
        } catch (expected: ConfigError.InvalidValue) {
            assertEquals(
                "invalid_value: customBaseURL is not supported for providerType=openRouter",
                expected.message,
            )
        }
    }

    // ---- effectiveBaseURL: the read side trims before appending ----

    @Test
    fun effective_blankBecomesNull() {
        assertNull(inst(base = "   ").effectiveBaseURL)
        assertNull(inst(base = "").effectiveBaseURL)
        assertNull(inst(base = null).effectiveBaseURL)
    }

    @Test
    fun effective_paddedBaseIsTrimmedBeforeV1Append() {
        // THE read-side defect: `"   "` used to become `"   /v1"`.
        assertEquals("https://api.openai.com/v1", inst(base = " https://api.openai.com/v1/ ").effectiveBaseURL)
    }

    @Test
    fun effective_trailingSlashStillCollapses() {
        assertEquals("https://api.openai.com/v1", inst(base = "https://api.openai.com/").effectiveBaseURL)
    }

    @Test
    fun effective_appendV1SuffixOffKeepsBareBase() {
        val i = inst(base = "  https://api.openai.com/v1beta  ").copy(appendV1Suffix = false)
        assertEquals("https://api.openai.com/v1beta", i.effectiveBaseURL)
    }

    @Test
    fun effective_cleanValueRoundTripsUnchanged() {
        assertEquals("https://api.example.com/v1", inst(base = "https://api.example.com/v1").effectiveBaseURL)
    }

    @Test
    fun effective_paddedBareHostGetsV1Appended() {
        // Trim must happen BEFORE the append: the literal below is what the
        // request path must see on the wire.
        assertEquals("https://api.example.com/v1", inst(base = "  https://api.example.com  ").effectiveBaseURL)
    }
    // ---- normalizeCustomBaseURL: the shared helper every write face routes ----

    @Test
    fun normalize_nullEmptyAndWhitespaceAllBecomeNull() {
        assertNull(normalizeCustomBaseURL(null))
        assertNull(normalizeCustomBaseURL(""))
        assertNull(normalizeCustomBaseURL("   "))
        assertNull(normalizeCustomBaseURL(" \t\n "))
    }

    @Test
    fun normalize_paddedValueIsTrimmed() {
        assertEquals("https://api.example.com", normalizeCustomBaseURL("  https://api.example.com  "))
        assertEquals("https://api.example.com", normalizeCustomBaseURL("\thttps://api.example.com\n"))
    }

    @Test
    fun normalize_cleanValueIsUntouched() {
        assertEquals("https://api.example.com/v1", normalizeCustomBaseURL("https://api.example.com/v1"))
    }
}

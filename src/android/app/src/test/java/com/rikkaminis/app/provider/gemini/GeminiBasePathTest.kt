package com.rikkaminis.app.provider.gemini

import com.rikkaminis.app.data.model.ProviderType
import com.rikkaminis.app.debug.providerProbeUrl
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [deep-audit-1016 ISSUE-01] URL-shape contract for the Gemini base.
 *
 * Every expectation below is a **literal**, written from the measured endpoint behaviour
 * (404 = path missing, 400 "API key not valid" = path exists) recorded in
 * `deep-audit-1016/fix-a/probe/url-shape-probe.txt` — none of them is derived from a
 * constant under test.
 */
class GeminiBasePathTest {

    @Test
    fun `official base keeps its single version segment`() {
        assertEquals(
            "https://generativelanguage.googleapis.com/v1beta",
            GeminiBasePath.normalize("https://generativelanguage.googleapis.com/v1beta"),
        )
    }

    @Test
    fun `openai-shaped v1 base is rewritten to v1beta`() {
        assertEquals(
            "https://generativelanguage.googleapis.com/v1beta",
            GeminiBasePath.normalize("https://generativelanguage.googleapis.com/v1"),
        )
    }

    @Test
    fun `trailing slash is dropped`() {
        assertEquals(
            "https://generativelanguage.googleapis.com/v1beta",
            GeminiBasePath.normalize("https://generativelanguage.googleapis.com/v1beta/"),
        )
    }

    @Test
    fun `bare host gets exactly one version segment`() {
        assertEquals(
            "https://generativelanguage.googleapis.com/v1beta",
            GeminiBasePath.normalize("https://generativelanguage.googleapis.com"),
        )
    }

    @Test
    fun `doubled v1 then v1beta collapses to one`() {
        assertEquals(
            "https://generativelanguage.googleapis.com/v1beta",
            GeminiBasePath.normalize("https://generativelanguage.googleapis.com/v1/v1beta"),
        )
    }

    @Test
    fun `relay base keeps its path prefix`() {
        assertEquals(
            "https://relay.example.com/gemini/v1beta",
            GeminiBasePath.normalize("https://relay.example.com/gemini/v1beta"),
        )
    }

    // appendV1Suffix (default true on ProviderInstance, and the default for backup/JSON
    // imports) appends "/v1" to a custom base, so a Gemini instance whose base is
    // ".../v1beta" arrives here as ".../v1beta/v1". Measured: ".../v1beta/models/..." -> 400
    // (works), ".../v1beta/v1beta/models/..." -> 404. A single strip pass per suffix would
    // produce the latter, i.e. this fix would have broken a shape that works today.
    @Test
    fun `appendV1Suffix shape is collapsed, not doubled`() {
        assertEquals(
            "https://generativelanguage.googleapis.com/v1beta",
            GeminiBasePath.normalize("https://generativelanguage.googleapis.com/v1beta/v1"),
        )
    }

    @Test
    fun `relay plus appendV1Suffix shape is collapsed, not doubled`() {
        assertEquals(
            "https://relay.example.com/gemini/v1beta",
            GeminiBasePath.normalize("https://relay.example.com/gemini/v1beta/v1"),
        )
    }

    @Test
    fun `repeated slashes after the base are dropped`() {
        assertEquals(
            "https://generativelanguage.googleapis.com/v1beta",
            GeminiBasePath.normalize("https://generativelanguage.googleapis.com/v1beta//"),
        )
    }

    @Test
    fun `surrounding whitespace is tolerated`() {
        assertEquals(
            "https://relay.example.com/gemini/v1beta",
            GeminiBasePath.normalize("  https://relay.example.com/gemini/v1beta  "),
        )
    }

    @Test
    fun `blank or missing base falls back to the public endpoint`() {
        assertEquals("https://generativelanguage.googleapis.com/v1beta", GeminiBasePath.normalize(null))
        assertEquals("https://generativelanguage.googleapis.com/v1beta", GeminiBasePath.normalize(""))
        assertEquals("https://generativelanguage.googleapis.com/v1beta", GeminiBasePath.normalize("   "))
    }

    @Test
    fun `a version segment with no host stays relative (degenerate input fails loudly)`() {
        // "/v1beta" on its own is not a base: there is no scheme and no host. Stripping the
        // only segment leaves nothing to prepend, so the result is a relative URL — OkHttp
        // rejects it outright, which surfaces the bad configuration instead of quietly
        // redirecting the user's API key to the public endpoint. GeminiModelsApi.normalizeBase
        // returns the same "/v1beta" for this input; this is the one degenerate shape where
        // falling back to DEFAULT_BASE would be a *behaviour* difference, not a fix.
        assertEquals("/v1beta", GeminiBasePath.normalize("/v1beta"))
    }

    @Test
    fun `a path that merely starts with v1 is left alone`() {
        // "v1beta-2" is not a version segment — only a whole trailing "/v1beta" or "/v1" is.
        assertEquals(
            "https://example.com/api/v1beta-2/v1beta",
            GeminiBasePath.normalize("https://example.com/api/v1beta-2"),
        )
    }

    /**
     * [deep-audit-1024 ISSUE-02] The shapes the **other three** call sites feed in — the
     * model list (`GeminiModelsApi.normalizeBase` ← `instance.effectiveBaseURL`), the
     * connectivity probe and Gemini TTS. They all delegate here now, so one table covers
     * all three; before the fix each of them carried a weaker copy of the rule.
     *
     * The `.../v1beta/v1` row is the negative control: a **single** strip pass — the
     * removed `GeminiModelsApi` copy — yields `.../v1beta/v1beta`, the measured 404 shape,
     * while the looped collapse yields the working `.../v1beta`. Rows are literal
     * expectations, none derived from the implementation.
     */
    @Test
    fun `every base shape the list, probe and voice sites feed collapses to one version segment`() {
        val shapes = listOf(
            // custom/relay base that `appendV1Suffix` (default true) extended: single pass
            // would leave ".../v1beta/v1beta" = 404.
            "https://relay.example.com/gemini/v1beta/v1" to "https://relay.example.com/gemini/v1beta",
            // OpenAI-shaped base a user pasted for a Gemini instance.
            "https://relay.example.com/gemini/v1" to "https://relay.example.com/gemini/v1beta",
            // already-correct base stays put.
            "https://relay.example.com/gemini/v1beta" to "https://relay.example.com/gemini/v1beta",
            // bare host + trailing slash (the shape the old voice `contains()` check
            // handled by accident).
            "https://relay.example.com/gemini/" to "https://relay.example.com/gemini/v1beta",
            // doubled segment: single pass also 404s here (".../v1beta/v1beta").
            "https://relay.example.com/gemini/v1beta/v1beta" to "https://relay.example.com/gemini/v1beta",
            // no custom base at all — the probe's per-type fallback endpoint (no version).
            "https://generativelanguage.googleapis.com" to "https://generativelanguage.googleapis.com/v1beta",
            // blank/null keep falling back to the public endpoint.
            "" to "https://generativelanguage.googleapis.com/v1beta",
            null to "https://generativelanguage.googleapis.com/v1beta",
        )
        for ((input, expected) in shapes) {
            assertEquals("normalize($input)", expected, GeminiBasePath.normalize(input))
        }
    }

    /**
     * [deep-audit-1024 ISSUE-02] `provider.instances.test` probe URLs. The Gemini and
     * Anthropic arms used to append their version segment unconditionally, so a custom
     * base that already carried it probed `.../v1beta/v1beta/models` / `.../v1/v1/models`
     * and reported a healthy provider as unreachable. Expectations are literals.
     */
    @Test
    fun `gemini probe URL carries exactly one version segment for every base shape`() {
        assertEquals(
            "https://relay.example.com/gemini/v1beta/models?key=K",
            providerProbeUrl(ProviderType.gemini, "https://relay.example.com/gemini/v1beta/v1", "K"),
        )
        assertEquals(
            "https://relay.example.com/gemini/v1beta/models?key=",
            providerProbeUrl(ProviderType.gemini, "https://relay.example.com/gemini/v1", null),
        )
        assertEquals(
            "https://generativelanguage.googleapis.com/v1beta/models?key=K",
            providerProbeUrl(ProviderType.gemini, "https://generativelanguage.googleapis.com", "K"),
        )
    }

    @Test
    fun `anthropic probe URL does not double the v1 segment`() {
        // Custom base: `effectiveBaseURL` already appended /v1 (appendV1Suffix default true).
        assertEquals(
            "https://relay.example.com/anthropic/v1/models",
            providerProbeUrl(ProviderType.anthropic, "https://relay.example.com/anthropic/v1", "K"),
        )
        // Public endpoint: no version segment yet, so one is appended.
        assertEquals(
            "https://api.anthropic.com/v1/models",
            providerProbeUrl(ProviderType.anthropic, "https://api.anthropic.com", "K"),
        )
    }

    @Test
    fun `the openai-shaped probe arms keep their own v1 guard`() {
        for (type in listOf(ProviderType.openAI, ProviderType.xAI, ProviderType.kimiCode)) {
            assertEquals(
                "$type with a /v1 base",
                "https://relay.example.com/v1/models",
                providerProbeUrl(type, "https://relay.example.com/v1", "K"),
            )
            assertEquals(
                "$type with a bare base",
                "https://relay.example.com/v1/models",
                providerProbeUrl(type, "https://relay.example.com", "K"),
            )
        }
        // openRouter's base is already versionless by construction.
        assertEquals(
            "https://openrouter.ai/api/v1/models",
            providerProbeUrl(ProviderType.openRouter, "https://openrouter.ai/api/v1", "K"),
        )
    }
}

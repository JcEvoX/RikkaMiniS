package com.rikkaminis.app.provider.gemini

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
        assertEquals("https://generativelanguage.googleapis.com/v1beta", GeminiBasePath.normalize("/v1beta"))
    }

    @Test
    fun `a path that merely starts with v1 is left alone`() {
        // "v1beta-2" is not a version segment — only a whole trailing "/v1beta" or "/v1" is.
        assertEquals(
            "https://example.com/api/v1beta-2/v1beta",
            GeminiBasePath.normalize("https://example.com/api/v1beta-2"),
        )
    }
}

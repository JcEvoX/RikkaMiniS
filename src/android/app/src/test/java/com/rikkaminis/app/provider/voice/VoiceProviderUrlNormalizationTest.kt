package com.rikkaminis.app.provider.voice

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [deep-audit-1039 A-05] URL-shape contract for the two voice base
 * normalizations that used to be inline and asymmetric copies.
 *
 * Every expectation below is a **literal**, written from the shape of the
 * endpoint each provider posts to — never derived from the functions under
 * test, so a mutation cannot silently satisfy an assertion.
 *
 * The two defects these pin:
 *
 *  * **Azure double segment.** `VoiceProviderFactory.build` stripped
 *    `/cognitiveservices` *before* trimming a trailing slash, so a custom
 *    base pasted as `https://…speech.microsoft.com/` never matched, the
 *    segment survived, and `AzureTTSVoiceProvider`'s endpoint path
 *    (`/cognitiveservices/v1`) appended a second one:
 *    `…/cognitiveservices/cognitiveservices/v1` → 404 on every request.
 *
 *  * **ElevenLabs / Deepgram missing segment.** Both used
 *    `if (!base.contains("/v1")) base += "/v1"`. `contains` asks "does this
 *    path mention /v1 anywhere", but concatenation needs it as the **suffix**
 *    to end up with exactly one copy (the intent
 *    `VoiceProvider.composedUrlString`'s KDoc states). A relay base like
 *    `https://relay.example.com/v1/elevenlabs` therefore kept its request on
 *    `…/v1/elevenlabs/text-to-speech/…` — no version segment at all.
 */
class VoiceProviderUrlNormalizationTest {

    // ── Azure: normalizeAzureBase reduces to the bare host ─────────────────
    //
    // The endpoint path is always `/cognitiveservices/v1`, so the normalized
    // base must be the host: the segment is restored by the path, exactly once.

    @Test
    fun `azure default host stays the default host`() {
        assertEquals(
            "https://eastasia.tts.speech.microsoft.com",
            VoiceBaseNormalizer.normalizeAzureBase("https://eastasia.tts.speech.microsoft.com"),
        )
    }

    @Test
    fun `azure trailing slash no longer shields the segment`() {
        // The reported defect: the old order checked endsWith before any
        // trim, so this shape appended a second /cognitiveservices.
        assertEquals(
            "https://eastasia.tts.speech.microsoft.com",
            VoiceBaseNormalizer.normalizeAzureBase("https://eastasia.tts.speech.microsoft.com/"),
        )
        assertEquals(
            "https://eastasia.tts.speech.microsoft.com",
            VoiceBaseNormalizer.normalizeAzureBase("https://eastasia.tts.speech.microsoft.com///"),
        )
    }

    @Test
    fun `azure pasted segment is stripped with and without a trailing slash`() {
        assertEquals(
            "https://eastasia.tts.speech.microsoft.com",
            VoiceBaseNormalizer.normalizeAzureBase(
                "https://eastasia.tts.speech.microsoft.com/cognitiveservices",
            ),
        )
        assertEquals(
            "https://eastasia.tts.speech.microsoft.com",
            VoiceBaseNormalizer.normalizeAzureBase(
                "https://eastasia.tts.speech.microsoft.com/cognitiveservices/",
            ),
        )
    }

    @Test
    fun `azure fully pasted endpoint collapses to the host`() {
        // A user pasting the documented full endpoint
        // (…/cognitiveservices/v1) contributed to the double/v1 shapes.
        assertEquals(
            "https://eastasia.tts.speech.microsoft.com",
            VoiceBaseNormalizer.normalizeAzureBase(
                "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1",
            ),
        )
        assertEquals(
            "https://eastasia.tts.speech.microsoft.com",
            VoiceBaseNormalizer.normalizeAzureBase(
                "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1/",
            ),
        )
    }

    @Test
    fun `azure repeated segments all collapse`() {
        // The tail the defect produced, fed back in, must not survive either.
        assertEquals(
            "https://eastasia.tts.speech.microsoft.com",
            VoiceBaseNormalizer.normalizeAzureBase(
                "https://eastasia.tts.speech.microsoft.com/cognitiveservices/cognitiveservices/v1",
            ),
        )
    }

    @Test
    fun `azure custom region host is preserved`() {
        assertEquals(
            "https://westeurope.tts.speech.microsoft.com",
            VoiceBaseNormalizer.normalizeAzureBase("https://westeurope.tts.speech.microsoft.com/"),
        )
    }

    @Test
    fun `azure blank custom base is empty so the factory falls back to its default`() {
        // The factory decides the default (`?:` branch) — the normalizer only
        // reports "nothing usable", so a null custom base is indistinguishable
        // from a blank one at this layer.
        assertEquals("", VoiceBaseNormalizer.normalizeAzureBase(null))
        assertEquals("", VoiceBaseNormalizer.normalizeAzureBase("   "))
        assertEquals("", VoiceBaseNormalizer.normalizeAzureBase("///"))
    }

    // ── version segment: normalizeVersionSegment keeps exactly one ─────────

    @Test
    fun `elevenlabs default base gains its single version segment`() {
        assertEquals(
            "https://api.elevenlabs.io/v1",
            VoiceBaseNormalizer.normalizeVersionSegment(
                "https://api.elevenlabs.io",
                defaultBase = "https://api.elevenlabs.io",
            ),
        )
    }

    @Test
    fun `deepgram default base gains its single version segment`() {
        assertEquals(
            "https://api.deepgram.com/v1",
            VoiceBaseNormalizer.normalizeVersionSegment(
                "https://api.deepgram.com",
                defaultBase = "https://api.deepgram.com",
            ),
        )
    }

    @Test
    fun `base already ending in the version segment is left alone`() {
        // The old `contains` check also passed this shape — it must stay green.
        assertEquals(
            "https://api.elevenlabs.io/v1",
            VoiceBaseNormalizer.normalizeVersionSegment(
                "https://api.elevenlabs.io/v1",
                defaultBase = "https://api.elevenlabs.io",
            ),
        )
        assertEquals(
            "https://api.elevenlabs.io/v1",
            VoiceBaseNormalizer.normalizeVersionSegment(
                "https://api.elevenlabs.io/v1/",
                defaultBase = "https://api.elevenlabs.io",
            ),
        )
    }

    @Test
    fun `relay base with the version segment in the middle now gains the suffix`() {
        // The defect half: `contains("/v1")` saw the mid-path segment, skipped
        // the append, and the endpoint went out versionless.
        assertEquals(
            "https://relay.example.com/v1/elevenlabs/v1",
            VoiceBaseNormalizer.normalizeVersionSegment(
                "https://relay.example.com/v1/elevenlabs",
                defaultBase = "https://api.elevenlabs.io",
            ),
        )
    }

    @Test
    fun `version segment repeated on the tail collapses to one`() {
        assertEquals(
            "https://api.elevenlabs.io/v1",
            VoiceBaseNormalizer.normalizeVersionSegment(
                "https://api.elevenlabs.io/v1/v1",
                defaultBase = "https://api.elevenlabs.io",
            ),
        )
    }

    @Test
    fun `trailing slashes do not create an empty path segment`() {
        assertEquals(
            "https://api.elevenlabs.io/v1",
            VoiceBaseNormalizer.normalizeVersionSegment(
                "https://api.elevenlabs.io///",
                defaultBase = "https://api.elevenlabs.io",
            ),
        )
        assertEquals(
            "https://api.elevenlabs.io/v1",
            VoiceBaseNormalizer.normalizeVersionSegment(
                "https://api.elevenlabs.io/v1///",
                defaultBase = "https://api.elevenlabs.io",
            ),
        )
    }

    @Test
    fun `blank custom base falls back to the vendor default`() {
        assertEquals(
            "https://api.deepgram.com/v1",
            VoiceBaseNormalizer.normalizeVersionSegment(
                null,
                defaultBase = "https://api.deepgram.com",
            ),
        )
        assertEquals(
            "https://api.deepgram.com/v1",
            VoiceBaseNormalizer.normalizeVersionSegment(
                "  ",
                defaultBase = "https://api.deepgram.com",
            ),
        )
    }

    @Test
    fun `a base whose only version mention is a different version is untouched`() {
        // /v2 is not /v1: the loop must not strip or dedupe an unrelated
        // segment — the endpoint's own path is what names the version.
        assertEquals(
            "https://relay.example.com/v2/tts/v1",
            VoiceBaseNormalizer.normalizeVersionSegment(
                "https://relay.example.com/v2/tts",
                defaultBase = "https://api.elevenlabs.io",
            ),
        )
    }
}

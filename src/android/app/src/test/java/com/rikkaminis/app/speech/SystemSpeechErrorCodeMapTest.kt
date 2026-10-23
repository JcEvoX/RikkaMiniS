package com.rikkaminis.app.speech

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1044 A-06] The `SpeechRecognizer` error table used to be shifted by
 * one: it labelled 11/12 as the API-33 language pair, so
 *
 *  - `11` (`ERROR_SERVER_DISCONNECTED`, API 31) was reported as "language not
 *    supported by the recognizer", and
 *  - `13` (`ERROR_LANGUAGE_UNAVAILABLE`, API 33) fell through to `UNKNOWN`.
 *
 * The second half silently disabled a recovery path: `onError` retries once on
 * the default (cloud) recognizer when `mapError(code) == LANGUAGE_UNSUPPORTED`
 * and the session was on-device — a device with no on-device pack for the
 * locale therefore hit a dead end reading "Unknown error (13)" instead of
 * falling back.
 *
 * Expectations are literals on purpose: deriving them from the production
 * constants would let a shifted table move the expectation with it.
 *
 * `SpeechRecognizer` cannot be constructed on the JVM, but the table itself is
 * pure integer logic (the constants are compile-time `static final int`s, which
 * Kotlin inlines), so `mapError` / `errorMessage` are exercised directly — the
 * class-level Android fields are instance state and are never touched.
 */
class SystemSpeechErrorCodeMapTest {

    private fun mapped(code: Int) = SystemSpeechRecognitionEngine.mapError(code)

    private fun message(code: Int) = SystemSpeechRecognitionEngine.errorMessage(code)

    // ── the codes that were wrong ────────────────────────────────────────

    @Test
    fun `13 language pack unavailable maps to language unsupported`() {
        assertEquals(RecognitionError.LANGUAGE_UNSUPPORTED, mapped(13))
    }

    @Test
    fun `12 language not supported maps to language unsupported`() {
        assertEquals(RecognitionError.LANGUAGE_UNSUPPORTED, mapped(12))
    }

    @Test
    fun `11 server disconnected is a network failure, not a language failure`() {
        assertEquals(RecognitionError.NETWORK, mapped(11))
        assertNotEquals(RecognitionError.LANGUAGE_UNSUPPORTED, mapped(11))
    }

    @Test
    fun `10 too many requests is a busy recognizer, not a language failure`() {
        assertEquals(RecognitionError.RECOGNIZER_BUSY, mapped(10))
        assertNotEquals(RecognitionError.UNKNOWN, mapped(10))
    }

    // ── the recovery path those two codes feed ───────────────────────────

    @Test
    fun `the on-device retry gate now fires for a missing language pack`() {
        // Mirrors the production condition in onError:19x verbatim — a missing
        // on-device pack must re-enter startInternal with the default recognizer.
        val usingOnDevice = true
        val retryLocale: String? = "en-US"
        val err = mapped(13)
        assertTrue(
            "ERROR_LANGUAGE_UNAVAILABLE must still satisfy the on-device→cloud retry gate",
            err == RecognitionError.LANGUAGE_UNSUPPORTED && usingOnDevice && retryLocale != null,
        )
    }

    @Test
    fun `a disconnected service does not trigger the language retry`() {
        val usingOnDevice = true
        val err = mapped(11)
        assertTrue(
            "a dropped recognizer service must not be retried as a language problem",
            !(err == RecognitionError.LANGUAGE_UNSUPPORTED && usingOnDevice),
        )
    }

    // ── the errors that poison the engine for the process stay untouched ──

    @Test
    fun `codes 10 and 11 are not among the engine-poisoning errors`() {
        // onError calls markDegraded() for OEM_NO_SERVICE / PERMISSION_DENIED /
        // AUDIO_ERROR only. A single dropped session or a busy moment must not
        // permanently hide the mic button.
        val poisoning = setOf(
            RecognitionError.OEM_NO_SERVICE,
            RecognitionError.PERMISSION_DENIED,
            RecognitionError.AUDIO_ERROR,
        )
        assertTrue(mapped(10) !in poisoning)
        assertTrue(mapped(11) !in poisoning)
    }

    // ── the user-visible half ────────────────────────────────────────────

    @Test
    fun `every documented code gets a specific message, never the unknown fallback`() {
        for (code in listOf(10, 11, 12, 13)) {
            val text = message(code)
            assertTrue("code $code still reported as Unknown: $text", !text.startsWith("Unknown error"))
        }
    }

    @Test
    fun `the two language failures stay distinguishable for the user`() {
        // 12 = the recognizer does not support the language at all;
        // 13 = the pack is merely not downloaded yet (the user can fix it).
        assertNotEquals(message(12), message(13))
        assertEquals("Language not supported by the recognizer", message(12))
        assertTrue(
            "the unavailable-pack message must point at the fixable cause",
            message(13).contains("pack"),
        )
    }

    @Test
    fun `an unrecognised code still falls back to the literal echo`() {
        assertEquals(RecognitionError.UNKNOWN, mapped(99))
        assertEquals("Unknown error (99)", message(99))
    }
}

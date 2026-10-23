package com.rikkaminis.app.provider

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1047 A-03] The distribution of the legacy (non-contentParts)
 * media carriers over the three serializers.
 *
 * Expected values are hand-written literals — never derived from the code under
 * test (evidence-discipline): the request-level image list attaches to the last
 * user turn only, and a per-message image part stays on its own turn.
 */
class LegacyMediaPlanTest {

    @Test
    fun `request images attach to the last user turn only`() {
        assertEquals(
            2,
            LegacyMediaPlan.imageCount(
                attachRequestImages = true,
                requestImageCount = 2,
                messageImageCount = 0,
            ))
        assertEquals(
            0,
            LegacyMediaPlan.imageCount(
                attachRequestImages = false,
                requestImageCount = 2,
                messageImageCount = 0,
            ),
        )
    }

    @Test
    fun `per-message image parts stay on their own turn`() {
        assertEquals(
            3,
            LegacyMediaPlan.imageCount(
                attachRequestImages = false,
                requestImageCount = 5,
                messageImageCount = 3,
            ))
        assertEquals(
            3,
            LegacyMediaPlan.imageCount(
                attachRequestImages = true,
                requestImageCount = 0,
                messageImageCount = 3,
            ))
    }

    @Test
    fun `images and audio are one decision, never two branches`() {
        // The 88ef9b27 Responses shape: images and audio in mutually exclusive
        // `else if` arms. A turn carrying BOTH lost its audio silently.
        assertTrue(
            "a turn with images AND audio is a media turn",
            LegacyMediaPlan.hasMedia(imageCount = 2, messageAudioCount = 1))
        assertTrue(
            "an image-only turn is a media turn",
            LegacyMediaPlan.hasMedia(imageCount = 1, messageAudioCount = 0))
        assertTrue(
            "an audio-only turn is a media turn",
            LegacyMediaPlan.hasMedia(imageCount = 0, messageAudioCount = 1))
        assertFalse(
            "a turn with no media is not a media turn",
            LegacyMediaPlan.hasMedia(imageCount = 0, messageAudioCount = 0))
    }

    @Test
    fun `negative counts are floored, never subtracted`() {
        assertEquals(
            0,
            LegacyMediaPlan.imageCount(
                attachRequestImages = true,
                requestImageCount = -1,
                messageImageCount = 0,
            ))
        assertFalse(
            LegacyMediaPlan.hasMedia(imageCount = 0, messageAudioCount = -1))
    }
}

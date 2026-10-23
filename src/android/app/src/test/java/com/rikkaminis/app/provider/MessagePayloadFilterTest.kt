package com.rikkaminis.app.provider

import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1045 A-03] Boundary table for the shared "does this message
 * still carry anything worth putting on the wire?" predicate.
 *
 * The defect this pins: the three provider serializers each dropped messages
 * the orphan-tool-pairing sanitizer had emptied, using the predicate
 * `contentParts.isNotEmpty() || content.isNotEmpty()` — which answers "is
 * there TEXT?". A `minis-model-use` turn carrying only an image block or
 * only an audio block has no text, so the filter deleted the whole user turn
 * and the model never saw the bytes.
 *
 * Expected values are hand-written literals (never derived from the code
 * under test — see evidence-discipline).
 */
class MessagePayloadFilterTest {

    private fun user(
        content: String = "",
        contentParts: List<AgentContentPart> = emptyList(),
        audioParts: List<LLMMessage.AudioPart> = emptyList(),
        imageParts: List<LLMMessage.ImagePart> = emptyList(),
    ) = LLMMessage(
        role = LLMMessage.Role.USER,
        content = content,
        imageParts = imageParts,
        audioParts = audioParts,
        contentParts = contentParts,
    )

    @Test
    fun `a message with text is kept`() {
        assertTrue(user(content = "hello").hasWirePayload(false, 0))
    }

    @Test
    fun `a message with structured content parts is kept`() {
        val parts = listOf(AgentContentPart.Text("hi"))
        assertTrue(user(contentParts = parts).hasWirePayload(false, 0))
    }

    @Test
    fun `an audio-only message is kept`() {
        val audio = listOf(LLMMessage.AudioPart(format = "wav", base64Data = "AAA"))
        assertTrue(user(audioParts = audio).hasWirePayload(false, 0))
    }

    @Test
    fun `a message with per-message image parts is kept`() {
        val images = listOf(LLMMessage.ImagePart(data = byteArrayOf(1, 2), mimeType = "image/png"))
        assertTrue(user(imageParts = images).hasWirePayload(false, 0))
    }

    @Test
    fun `the last user turn is kept when the request carries turn images`() {
        // The providers attach the request-level image list to the last user
        // turn, so that turn must survive the filter for the bytes to land.
        assertTrue(user().hasWirePayload(isLastUserTurn = true, turnImageCount = 1))
    }

    @Test
    fun `a non-last user turn is dropped even when the request carries images`() {
        assertFalse(user().hasWirePayload(isLastUserTurn = false, turnImageCount = 1))
    }

    @Test
    fun `a truly empty message is dropped`() {
        assertFalse(user().hasWirePayload(false, 0))
    }
}

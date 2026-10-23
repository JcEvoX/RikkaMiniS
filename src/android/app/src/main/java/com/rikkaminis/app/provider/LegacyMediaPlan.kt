package com.rikkaminis.app.provider

/**
 * [deep-audit-1047 A-03] One answer to "which images belong to this turn, and
 * does this turn carry any media at all?".
 *
 * The three provider serializers each carried their own copy of that question,
 * and the copies disagreed:
 *  • OpenAI Chat Completions read the request-level list and the message's own
 *    image parts from DIFFERENT places — the request-level list only on the last
 *    user turn, the per-message parts nowhere at all;
 *  • OpenAI Responses had the request-level images and the message's audio in
 *    two MUTUALLY EXCLUSIVE `else if` arms, so a turn carrying both lost its
 *    audio silently;
 *  • Anthropic and Gemini read the request-level list only — a per-message
 *    image part (a carrier `hasWirePayload` already weighs) was dropped.
 *
 * The per-message image carrier has no producer in production today, so every
 * live path is byte-identical to before this change; the point is to make the
 * carrier SET explicit and shared, so the next carrier cannot silently become
 * a fourth variant.
 *
 * Pure + top-level so the distribution is directly unit-testable
 * (see LegacyMediaPlanTest) instead of being triplicated inline.
 */
internal object LegacyMediaPlan {

    /**
     * How many image parts this turn contributes.
     *
     * [attachRequestImages] is the caller's decision about the REQUEST-level
     * list (the serializers attach it to the last user turn only — unchanged
     * contract); [messageImageCount] is the message's own carrier, which always
     * stays on its own turn.
     */
    fun imageCount(
        attachRequestImages: Boolean,
        requestImageCount: Int,
        messageImageCount: Int,
    ): Int =
        (if (attachRequestImages) requestImageCount.coerceAtLeast(0) else 0) +
            messageImageCount.coerceAtLeast(0)

    /**
     * Does this turn carry any media at all?
     *
     * Images and audio are ONE decision, never two branches: that is exactly
     * the shape the Responses serializer got wrong (mutually exclusive `else if`
     * arms), and the reason a turn with both lost one of them.
     */
    fun hasMedia(imageCount: Int, messageAudioCount: Int): Boolean =
        imageCount > 0 || messageAudioCount.coerceAtLeast(0) > 0
}

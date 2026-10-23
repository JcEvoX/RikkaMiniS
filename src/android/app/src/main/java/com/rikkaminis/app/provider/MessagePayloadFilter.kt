package com.rikkaminis.app.provider

import com.rikkaminis.app.data.model.LLMMessage

/**
 * [deep-audit-1045 A-03] One shared answer to "does this message still carry
 * anything worth putting on the wire?".
 *
 * The three provider serializers (OpenAI chat/completions, OpenAI responses,
 * Anthropic) each dropped messages that the orphan-tool-pairing sanitizer had
 * emptied, and each wrote the SAME predicate:
 * `contentParts.isNotEmpty() || content.isNotEmpty()`. That predicate answers
 * "is there TEXT?", which is not the question being asked. A `minis-model-use`
 * turn that carries only an image block or only an audio block has no text,
 * so the filter deleted the whole user turn and the model never saw the
 * bytes — the request went out valid and semantically empty.
 *
 * The predicate therefore also accepts:
 *  - [LLMMessage.audioParts] — inline audio attachments (GH#67);
 *  - [LLMMessage.imageParts] — per-message image parts;
 *  - a turn that the REQUEST-level image list attaches to, i.e. the last user
 *    turn when the caller passed top-level images ([turnImageCount] > 0).
 *    The providers attach those bytes to the last user turn only, so that turn
 *    must survive the filter for the bytes to have a home.
 *
 * Pure + top-level so the boundary table is directly unit-testable
 * (see MessagePayloadFilterTest) instead of being triplicated inline.
 */
internal fun LLMMessage.hasWirePayload(
    isLastUserTurn: Boolean,
    turnImageCount: Int,
): Boolean =
    contentParts.isNotEmpty() ||
        content.isNotEmpty() ||
        audioParts.isNotEmpty() ||
        imageParts.isNotEmpty() ||
        (isLastUserTurn && turnImageCount > 0)

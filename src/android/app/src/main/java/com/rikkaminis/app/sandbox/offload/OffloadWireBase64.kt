package com.rikkaminis.app.sandbox.offload

/**
 * Single decode entry for every Base64 payload that travels the cross-process
 * model-exec wire (worker → `result.json` / JSONL chunks → main process, and
 * the `media_files` / `image_parts` / `contentParts` fields inside them).
 *
 * Two properties, both of which the call sites used to get wrong one at a
 * time:
 *
 *  1. **Tolerant alphabet (RFC 2045 / MIME).** The producer side is not
 *     uniform — `android.util.Base64.encodeToString(..., NO_WRAP)` writes one
 *     long line, but `Base64.DEFAULT` (and any outer relay, e.g. a payload that
 *     passed through a MIME-aware transport) wraps at 76 chars with line
 *     terminators. `java.util.Base64.getDecoder()` is strict RFC 4648 and
 *     throws on the wrapped form, so half the decode sites rejected payloads
 *     the other half accepted — for the SAME field.
 *  2. **Never throws.** A single malformed attachment must not take down the
 *     whole run: the callers either skip the attachment (typed-failure path) or
 *     degrade to an empty blob. An `IllegalArgumentException` escaping from
 *     inside a `mapNotNull` discarded an already-generated text reply along
 *     with every remaining attachment.
 *
 * Mirrors what `ProviderExecutionGateway` already did inline
 * (`runCatching { getMimeDecoder().decode(b64) }.getOrNull()`); the other four
 * consumers each hand-rolled a different variant of it.
 *
 * No Android dependencies (JVM-testable) — this is the single source of truth
 * for "how bytes arrive over the offload wire".
 */
object OffloadWireBase64 {

    /**
     * Decode [b64], returning `null` for an empty string or a payload that
     * cannot be decoded. Never throws.
     *
     * Callers decide what "no bytes" means for their field:
     *  - a media attachment → skip it (`return@mapNotNull null`);
     *  - an image part / chunk payload → an empty [ByteArray];
     *  - an input the user explicitly asked for (`data:` URL, `input_audio`)
     *    → a typed error naming the field (see the handler's ImageInputError
     *    and AudioInputError paths, which call this and then refuse).
     */
    fun decode(b64: String): ByteArray? =
        if (b64.isEmpty()) null
        else runCatching { java.util.Base64.getMimeDecoder().decode(b64) }.getOrNull()
}

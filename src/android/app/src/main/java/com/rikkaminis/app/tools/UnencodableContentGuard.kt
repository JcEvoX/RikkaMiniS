package com.rikkaminis.app.tools

import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction

/**
 * [deep-audit-1047 A-08] Shared UTF-8 validity check for the write-shaped
 * tools: `FileWriteTool` has always run it, `FileEditTool` never did.
 *
 * `String.toByteArray(UTF_8)` and `File.writeText` do NOT reject text that
 * cannot round-trip — the JVM encoder SUBSTITUTES `?` (`U+FFFD`) for unpaired
 * surrogates and returns a byte array anyway. So the older try/catch around
 * `content.toByteArray(UTF_8)` in FileWriteTool was an unreachable guard: it
 * could never fire, yet read like a validation. Configuring the encoder with
 * [CodingErrorAction.REPORT] is what actually rejects them.
 *
 * Without this check the failure is silent and destructive either way:
 * `AtomicFileWrite.write` computes its own expected size from the *encoded*
 * bytes while the tool reports the char count, so the mismatch is logged as a
 * warning after the file has already been overwritten with mojibake.
 *
 * Both write paths must hold this to the same standard, so the check lives in
 * one place. Top level (not a private member) so a plain JVM test can pin it.
 */
internal fun isUnpairedSurrogateFailure(content: String): Boolean =
    try {
        Charsets.UTF_8.newEncoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .encode(java.nio.CharBuffer.wrap(content))
        false
    } catch (e: CharacterCodingException) {
        true
    }

/** Shared rejection wording — every write tool reports the same failure. */
internal const val UNENCODABLE_CONTENT_MESSAGE =
    "Error: Content contains unpaired surrogates and cannot be encoded as UTF-8"

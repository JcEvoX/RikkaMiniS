package com.rikkaminis.app.tools

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1047 / A-08] Regression tests for the write tools' UTF-8 guard.
 *
 * The defect: `FileWriteTool` guarded unpaired surrogates (after an earlier
 * repair), `FileEditTool` did not — so `file_edit` wrote text the JVM encoder
 * silently substitutes with `?`. The file landed corrupted, and
 * `AtomicFileWrite`'s own expected-size check then warned about a byte count
 * nobody could reconcile.
 *
 * The guard itself used to be a try/catch around `content.toByteArray(UTF_8)`,
 * which NEVER throws — the encoder substitutes rather than failing, so the
 * original guard was unreachable while reading like a validation. The shared
 * helper configures the encoder with `CodingErrorAction.REPORT`, which does
 * reject unpaired surrogates.
 *
 * The expectations below are HAND-WRITTEN literals, never derived from the
 * implementation under test.
 */
class UnencodableContentGuardTest {

    @Test
    fun plainAsciiIsEncodable() {
        assertFalse(isUnpairedSurrogateFailure("hello world"))
    }

    @Test
    fun cjkIsEncodable() {
        assertFalse(isUnpairedSurrogateFailure("你好，世界"))
    }

    @Test
    fun emojiIsEncodable() {
        // A surrogate PAIR is well-formed and must survive.
        assertFalse(isUnpairedSurrogateFailure("ok \uD83D\uDE00 done"))
    }

    @Test
    fun emptyStringIsEncodable() {
        assertFalse(isUnpairedSurrogateFailure(""))
    }

    @Test
    fun loneHighSurrogateIsRejected() {
        assertTrue(isUnpairedSurrogateFailure("\uD800"))
    }

    @Test
    fun loneLowSurrogateIsRejected() {
        assertTrue(isUnpairedSurrogateFailure("\uDC00"))
    }

    @Test
    fun unpairedSurrogateAmongTextIsRejected() {
        assertTrue(isUnpairedSurrogateFailure("before\uD800after"))
    }

    @Test
    fun trailingHighSurrogateIsRejected() {
        assertTrue(isUnpairedSurrogateFailure("text ends with \uD83D"))
    }

    @Test
    fun bothWriteToolsShareTheSameMessage() {
        // The two tools must report the SAME wording — a divergent message
        // would make the tool's failure shape unrecognizable to the model.
        assertTrue(UNENCODABLE_CONTENT_MESSAGE.contains("unpaired surrogates"))
        assertTrue(UNENCODABLE_CONTENT_MESSAGE.contains("UTF-8"))
    }
}

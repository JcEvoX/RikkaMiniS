package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * JVM-pure tests for the URI path-semantics percent decoder (sweep1014-B6).
 * The production file UriPathDecode.kt + ChatLinkMissingReason.kt +
 * DeepLinkPathGuard.kt are compiled verbatim — all JVM-pure by design.
 *
 * Bug context: [java.net.URLDecoder.decode] (the previous implementation at
 * all five decode sites) follows form-encoding semantics — it turns literal
 * '+' into space and throws IllegalArgumentException on malformed escapes.
 * The latter was reachable during Compose composition via a chat message
 * containing a `file:///…/100%.txt` link = app crash.
 */
class UriPathDecodeTest {

    @Test fun plusIsPreserved() {
        // The form-encoding bug: c++_primer.md decoded to `c  _primer.md`.
        assertEquals("c++_primer.md", decodeUriPath("c++_primer.md"))
        assertEquals("a+b.png", decodeUriPath("a+b.png"))
    }

    @Test fun barePercentDoesNotThrowAndFallsBack() {
        // The crash path: `100%.txt` used to throw IllegalArgumentException.
        assertEquals("100%.txt", decodeUriPath("100%.txt"))
        assertEquals("50%off.png", decodeUriPath("50%off.png"))
    }

    @Test fun percentAtEndOfInputIsVerbatim() {
        assertEquals("file%", decodeUriPath("file%"))
        assertEquals("x%2", decodeUriPath("x%2"))
    }

    @Test fun chineseFilenameIsDecoded() {
        assertEquals("中文.md", decodeUriPath("%E4%B8%AD%E6%96%87.md"))
        // Mixed: encoded space between two encoded characters.
        assertEquals("中文 name.md", decodeUriPath("%E4%B8%AD%E6%96%87%20name.md"))
    }

    @Test fun asciiEscapesAreDecoded() {
        assertEquals("a b.png", decodeUriPath("a%20b.png"))
        assertEquals("x#y.md", decodeUriPath("x%23y.md"))
    }

    @Test fun plusIsPreservedAlongsideEscapes() {
        // Pins '+' preservation inside the decode loop (strings WITH escapes
        // don't hit the no-% fast path) — URLDecoder semantics would produce
        // "a b c.png".
        assertEquals("a b+c.png", decodeUriPath("a%20b+c.png"))
    }

    @Test fun adjacentSequencesDecodeAsOneUtf8Unit() {
        // Consecutive %XX bytes must form a single UTF-8 sequence, not
        // decode per-byte into replacement characters.
        assertEquals("中文", decodeUriPath("%E4%B8%AD%E6%96%87"))
    }

    @Test fun noPercentIsFastPathVerbatim() {
        assertEquals("plain name.md", decodeUriPath("plain name.md"))
        assertEquals("", decodeUriPath(""))
    }

    @Test fun missingReasonWithPlusInPathIsFileMissing() {
        // '+' must survive decode in the missing-reason classification too —
        // the segment guard sees the same filename the resolver resolves.
        assertEquals(
            ChatLinkMissingReason.FILE_MISSING,
            chatLinkMissingReason("minis://workspace/c++_primer.md"),
        )
    }

    @Test fun missingReasonPercentEncodedDotDotStillIllegal() {
        // Decode-before-guard behavior is unchanged for the security case.
        assertEquals(
            ChatLinkMissingReason.ILLEGAL_PATH,
            chatLinkMissingReason("minis://shared/%2e%2e/mounts/x.md"),
        )
    }
}

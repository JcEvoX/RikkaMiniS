package com.rikkaminis.app.browser

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test
import java.net.URLDecoder
import java.util.concurrent.atomic.AtomicInteger

/**
 * [deep-audit-1019 ISSUE-08] `data:` URL payload semantics of
 * [decodeDataUrlPayload].
 *
 * RFC 2397 payloads are percent-encoded, where a literal `+` is a `+` —
 * [java.net.URLDecoder] follows form encoding (`+` → space) and silently
 * corrupted every non-base64 download containing one. Expectations are
 * literals; the injected `base64Decoder` stands in for `android.util.Base64`
 * (unavailable on the JVM), so only the ROUTING of the base64 arm is asserted
 * here, never its decoding.
 */
class DataUrlPayloadDecodeTest {

    private fun s(bytes: ByteArray): String = String(bytes, Charsets.UTF_8)

    /** The exact corruption the fix targets: `data:text/plain,1+1%3D2`. */
    @Test
    fun `a literal plus survives non-base64 decoding`() {
        assertEquals("1+1=2", s(decodeDataUrlPayload("text/plain", "1+1%3D2")))
    }

    /**
     * Differential arm, pinning the defect: the PRE-FIX path (bare
     * URLDecoder, form semantics) decodes the very same payload to `1 1=2`.
     * If this ever flips, the guard was "fixed" by changing the test instead
     * of the code.
     */
    @Test
    fun `bare URLDecoder form semantics corrupt the same payload - differential arm`() {
        assertEquals(
            "the pre-fix path decoded '+' as a space",
            "1 1=2",
            URLDecoder.decode("1+1%3D2", "UTF-8"),
        )
    }

    /** Precision control: `%20` keeps meaning space — the fix did not simply
     *  switch encoders. */
    @Test
    fun `percent-encoded space still decodes to a space`() {
        assertEquals("a b", s(decodeDataUrlPayload("text/plain", "a%20b")))
    }

    @Test
    fun `an already-encoded plus decodes to a literal plus`() {
        assertEquals("1+1", s(decodeDataUrlPayload("text/plain", "1%2B1")))
    }

    @Test
    fun `utf-8 percent-escapes decode correctly`() {
        assertEquals("中+", s(decodeDataUrlPayload("text/plain;charset=utf-8", "%E4%B8%AD%2B")))
    }

    @Test
    fun `base64 headers route to the base64 decoder`() {
        val routed = AtomicInteger(0)
        val bytes = decodeDataUrlPayload("text/plain;base64", "aGk=") { _ ->
            routed.incrementAndGet()
            byteArrayOf(7, 7) // literal stand-in: only the ROUTING is asserted here
        }
        assertEquals("the base64 arm must be taken exactly once", 1, routed.get())
        assertArrayEquals(byteArrayOf(7, 7), bytes)
    }

    @Test
    fun `non-base64 headers never touch the base64 decoder`() {
        val routed = AtomicInteger(0)
        val bytes = decodeDataUrlPayload("text/plain", "ok") { _ ->
            routed.incrementAndGet()
            byteArrayOf(0)
        }
        assertEquals(0, routed.get())
        assertEquals("ok", s(bytes))
    }
}

package com.rikkaminis.app.debug

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * [da1028-A] Bounded segment read for `debug.readFile` (readFileSegment).
 *
 * The defect this pins is an API-level one that CANNOT be reproduced on the
 * host JVM, and the report says so instead of implying coverage:
 * `InputStream.readNBytes(int)` is API 33+ while minSdk is 26, so on Android
 * 8-12 the old shape threw `NoSuchMethodError` (an `Error`, outside the
 * `catch (e: Exception)` wrappers) — the JDK has had the method since 11, so
 * the host cannot see it. What the host CAN pin is the read contract the
 * replacement must satisfy: exact offset, exact length, EOF short reads without
 * padding, empty reads past EOF, and byte transparency for non-text payloads.
 * The API ban itself is enforced by `scripts/scan/debug_leak_guard.py`.
 *
 * Expectations are literals taken from the file contents written below, never
 * derived from the production code, and case [matchesAPlainSliceOfTheFile]
 * cross-checks against an independent implementation path (whole-file read +
 * explicit range) so a bug in the clamp arithmetic cannot hide behind a
 * matching expectation.
 */
class DebugReadFileCompatTest {

    private val content = "0123456789"

    private fun tempFile(bytes: ByteArray): File {
        val file = Files.createTempFile("debugread", ".bin").toFile()
        file.deleteOnExit()
        file.writeBytes(bytes)
        return file
    }

    private fun read(text: String, offset: Long, limit: Int): String =
        String(readFileSegment(tempFile(text.toByteArray()), offset, limit), Charsets.UTF_8)

    /** A limit past EOF must return the bytes that exist — never padding. */
    @Test
    fun limitPastEofReturnsExactlyTheRemainingBytes() {
        assertEquals("0123456789", read(content, 0, 1 shl 20))
        assertEquals(10, readFileSegment(tempFile(content.toByteArray()), 0, 1 shl 20).size)
    }

    @Test
    fun limitPastEofAfterAnOffsetReturnsExactlyTheRemainingBytes() {
        assertEquals("89", read(content, 8, 4))
        assertEquals("789", read(content, 7, 100))
    }

    @Test
    fun offsetAndLimitSelectTheExactWindow() {
        assertEquals("01234", read(content, 0, 5))
        assertEquals("3456", read(content, 3, 4))
        assertEquals("56", read(content, 5, 2))
    }

    @Test
    fun offsetAtOrPastEofReturnsEmpty() {
        assertEquals("", read(content, 10, 5))
        assertEquals("", read(content, 99, 5))
        assertEquals(0, readFileSegment(tempFile(content.toByteArray()), 10, 5).size)
    }

    @Test
    fun negativeOffsetReadsFromTheStartAndZeroLimitReadsNothing() {
        assertEquals("012", read(content, -5, 3))
        assertEquals("", read(content, 0, 0))
    }

    /** The base64 branch re-encodes raw bytes: they must survive unchanged. */
    @Test
    fun bytesOutsideAsciiSurviveUnchanged() {
        val raw = byteArrayOf(0x00, 0x41, 0x7F, 0x80.toByte(), 0xFF.toByte())
        val file = tempFile(raw)
        assertArrayEquals(raw.copyOfRange(1, 5), readFileSegment(file, 1, 4))
        assertArrayEquals(byteArrayOf(), readFileSegment(file, 5, 4))
    }

    /** Independent path: the segment must equal a plain slice of the file. */
    @Test
    fun matchesAPlainSliceOfTheFile() {
        val whole = content.toByteArray()
        val windows = listOf(
            0L to 1, 0L to 10, 0L to 11, 1L to 3, 3L to 4, 9L to 1, 9L to 9, 10L to 9, 4L to 0,
        )
        for ((offset, limit) in windows) {
            val file = tempFile(whole)
            val expected = whole.copyOfRange(offset.toInt(), minOf(offset.toInt() + limit, whole.size))
            assertArrayEquals(
                "offset=$offset limit=$limit must equal the file's own bytes in that window",
                expected, readFileSegment(file, offset, limit),
            )
        }
    }
}

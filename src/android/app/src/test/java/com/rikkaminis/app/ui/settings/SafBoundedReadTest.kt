package com.rikkaminis.app.ui.settings

import java.io.ByteArrayInputStream
import java.io.InputStream
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1045 B-07] The bounded SAF read.
 *
 * Bug context: `ProviderListScreen`'s non-zip import branch ran
 * `openInputStream(uri)?.bufferedReader()?.use { it.readText() }` and
 * `SkillsManagementScreen.readUriBytes` ran a bare `use { it.readBytes() }` —
 * both materialised the whole user-picked document before anything looked at
 * it, so a multi-GB `.json` / `.zip` reached OutOfMemoryError. The downstream
 * 32 MiB guard (`SkillRepository.readZipEntries`) only bounds EXPANDED entries.
 *
 * These tests pin the decision, not the spelling: how many bytes may enter the
 * heap, and what the boundary means. Expectations are HAND-WRITTEN literals —
 * never derived from the constants under test (a constant changed together with
 * its expectation would hide the mutation behind a PASS).
 */
class SafBoundedReadTest {

    private fun payload(size: Int): ByteArray = ByteArray(size) { (it % 251).toByte() }

    /** A stream that will keep producing [limit] bytes and remembers how many were taken. */
    private class CountingStream(private val limit: Int, private val closeCallback: () -> Unit = {}) :
        InputStream() {
        var consumed = 0
            private set
        var closed = false
            private set

        override fun read(): Int {
            if (consumed >= limit) return -1
            consumed++
            return 0x78
        }

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (len == 0) return 0
            if (consumed >= limit) return -1
            val n = minOf(len, limit - consumed)
            for (i in 0 until n) b[off + i] = 0x78
            consumed += n
            return n
        }

        override fun close() {
            closed = true
            closeCallback()
        }
    }

    // ── the caps themselves ────────────────────────────────────────────────

    @Test
    fun `the two import caps are the literal 16 and 32 MiB`() {
        // Deliberately literal: 16 MiB mirrors ProviderImportZip's MAX_TOTAL_BYTES,
        // 32 MiB mirrors SkillRepository.readZipEntries' maxExpanded.
        assertEquals(16_777_216L, MAX_PROVIDER_JSON_BYTES)
        assertEquals(33_554_432L, MAX_SKILL_IMPORT_BYTES)
    }

    // ── readAtMost: the boundary ───────────────────────────────────────────

    @Test
    fun `a stream shorter than the cap is returned whole`() {
        val body = payload(700)

        val read = readAtMost(ByteArrayInputStream(body), 1_000L)

        assertArrayEquals(body, read!!)
    }

    @Test
    fun `a stream exactly the cap long is accepted whole, never truncated`() {
        val body = payload(1_000)

        val read = readAtMost(ByteArrayInputStream(body), 1_000L)

        assertArrayEquals("a full-cap file is a valid import, not an overflow", body, read!!)
        assertEquals(1_000, read.size)
    }

    @Test
    fun `one byte past the cap is refused`() {
        val read = readAtMost(ByteArrayInputStream(payload(1_001)), 1_000L)

        assertNull("cap + 1 must be refused", read)
    }

    @Test
    fun `an empty stream yields empty bytes, not null`() {
        val read = readAtMost(ByteArrayInputStream(ByteArray(0)), 1_000L)

        assertEquals(0, read!!.size)
    }

    @Test
    fun `a cap of zero refuses any non-empty stream`() {
        assertNull(readAtMost(ByteArrayInputStream(payload(1)), 0L))
        assertEquals(0, readAtMost(ByteArrayInputStream(ByteArray(0)), 0L)!!.size)
    }

    @Test
    fun `a huge stream is abandoned after at most cap plus one bytes`() {
        // 100_000 available, cap 1_000: the probe is what makes the refusal
        // certain, and it must not cost more than a single extra byte.
        val stream = CountingStream(100_000)

        val read = readAtMost(stream, 1_000L)

        assertNull(read)
        assertEquals("must stop at the probe byte", 1_001, stream.consumed)
    }

    @Test
    fun `readAtMost never closes the caller's stream`() {
        val stream = CountingStream(10)

        readAtMost(stream, 1_000L)

        assertFalse("the caller owns the stream (readSafBytes wraps it in use { })", stream.closed)
    }

    // ── declaredSizeExceeds: the cheap early reject ────────────────────────

    @Test
    fun `a declared size equal to the cap is not an overflow`() {
        assertFalse(declaredSizeExceeds(1_024L, 1_024L))
    }

    @Test
    fun `a declared size one byte above the cap is refused`() {
        assertTrue(declaredSizeExceeds(1_025L, 1_024L))
    }

    @Test
    fun `an unreported declared size is never a rejection reason`() {
        // Providers are allowed to report nothing; the stream read is the authority.
        assertFalse(declaredSizeExceeds(null, 1_024L))
    }

    @Test
    fun `a declared zero is not an overflow even under a zero cap`() {
        assertFalse(declaredSizeExceeds(0L, 0L))
    }

    // ── the user-visible wording ───────────────────────────────────────────

    @Test
    fun `the too-large wording names the limit in whole megabytes`() {
        // Dead literal: 33_554_432 B must read as "32 MB", and the text must not
        // look like the read-failure wording (that is the whole point).
        assertEquals(
            "File is too large to import (limit 32 MB)",
            safTooLargeText(33_554_432L),
        )
        assertEquals(
            "File is too large to import (limit 16 MB)",
            safTooLargeText(16_777_216L),
        )
    }
}

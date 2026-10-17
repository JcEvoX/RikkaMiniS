package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path

/**
 * Field-boundary evidence for the ustar name/prefix ceilings
 * (deep-audit-1019 ISSUE-03).
 *
 * `putText` reserves the last byte of every fixed-width string field for the
 * NUL terminator (`count = minOf(bytes.size, length - 1)`), so a name/prefix
 * that the SPLITTER thinks fits (100/155) actually gets written 99/154 — one
 * byte silently lost, with nothing on the write path to report it. These tests
 * pin the real ceilings byte for byte:
 *
 *  - 99-byte names and 154-byte prefixes still take the short-name path;
 *  - names/prefixes one byte larger fall back to the GNU `'L'` long-name
 *    record and round-trip exactly;
 *  - a multi-byte character at the boundary is not cut in half;
 *  - the header FIELD itself is inspected independently of the engine's own
 *    reader (the 99-byte name must sit in bytes 0..98 with a NUL at 99).
 *
 * Every expected value is a hard literal. The fixtures below deliberately
 * reach the boundary through `tarCreate`'s member-name override (the second
 * element of each root pair), so the byte counts are exact without needing
 * the filesystem to host 200-byte path segments.
 */
class FastioTarFieldBoundsTest {

    private val longLinkMarker = "././@LongLink"

    // ── the exact ceilings as pure functions ───────────────────────────────

    @Test
    fun `a 99-byte name takes the short-name path, a 100-byte name does not`() {
        val name99 = "a".repeat(96) + ".py"          // 96 + 3 = 99 bytes
        val short = splitUstarName(name99)!!
        assertEquals("", short.first)
        assertEquals(name99, short.second)

        val name100 = "a".repeat(97) + ".py"        // 97 + 3 = 100 bytes
        assertNull(
            "a 100-byte name cannot fit the 99-byte store; it must go to the GNU record",
            splitUstarName(name100),
        )
    }

    @Test
    fun `a 154-byte prefix is the last split that fits`() {
        // 74 + '/' + 79 = 154-byte prefix, 60-byte rest.
        val prefix154 = "p".repeat(74) + "/" + "q".repeat(79)
        val rest60 = "r".repeat(60)
        val split154 = splitUstarName("$prefix154/$rest60")!!
        assertEquals(prefix154, split154.first)
        assertEquals(rest60, split154.second)

        // One byte more (155) must NOT be accepted: putText would write 154.
        val prefix155 = "p".repeat(74) + "/" + "q".repeat(80)
        assertNull(
            "a 155-byte prefix cannot fit the 154-byte store; the GNU record is required",
            splitUstarName("$prefix155/$rest60"),
        )
    }

    @Test
    fun `a 100-byte rest after a slash no longer fits the name field`() {
        // The split point that would leave rest = 100 must be rejected.
        val name = "dir" + "/" + "f".repeat(100)
        assertNull(splitUstarName(name))
    }

    // ── round-trips through the real engine ────────────────────────────────

    @Test
    fun `a 99-byte member name round-trips byte for byte without a long-name record`() {
        val name99 = "a".repeat(96) + ".py"          // 99 bytes
        val (bytes, root) = archiveOf(name99)
        try {
            assertFalse(
                "a 99-byte name fits the field; no GNU record may be emitted",
                String(bytes, Charsets.ISO_8859_1).contains(longLinkMarker),
            )
            // The field itself, read independently of the engine's reader:
            // bytes 0..98 are the name, byte 99 is the NUL terminator.
            val field = String(bytes, 0, 99, Charsets.ISO_8859_1)
            assertEquals(name99, field)
            assertEquals(0.toByte(), bytes[99])

            assertArrayEquals(name99.toByteArray(StandardCharsets.UTF_8), readLastName(bytes))
            val extracted = extractTo(bytes)
            assertTrue(Files.exists(extracted.resolve(name99)))
        } finally {
            deleteRecursively(root)
        }
    }

    @Test
    fun `a 100-byte member name falls back to the GNU record and round-trips exactly`() {
        val name100 = "a".repeat(97) + ".py"        // 100 bytes: one past the field
        val (bytes, root) = archiveOf(name100)
        try {
            assertTrue(
                "a 100-byte name must be carried by a GNU @LongLink record",
                String(bytes, Charsets.ISO_8859_1).contains(longLinkMarker),
            )
            assertArrayEquals(name100.toByteArray(StandardCharsets.UTF_8), readLastName(bytes))
            val extracted = extractTo(bytes)
            assertTrue(Files.exists(extracted.resolve(name100)))
        } finally {
            deleteRecursively(root)
        }
    }

    @Test
    fun `a multi-byte character at the boundary is not cut in half`() {
        // 97 ASCII bytes + one 3-byte CJK character = 100 bytes. The old code
        // stored 99 of them — the last character would lose its first byte and
        // decode as U+FFFD. The fixed code must carry it whole.
        val name100 = "b".repeat(97) + "中"
        assertEquals(100, name100.toByteArray(StandardCharsets.UTF_8).size)
        val (bytes, root) = archiveOf(name100)
        try {
            assertTrue(String(bytes, Charsets.ISO_8859_1).contains(longLinkMarker))
            assertArrayEquals(name100.toByteArray(StandardCharsets.UTF_8), readLastName(bytes))
        } finally {
            deleteRecursively(root)
        }
    }

    @Test
    fun `a 155-byte prefix falls back to the GNU record instead of losing a byte`() {
        // prefix 74 + '/' + 80 = 155 bytes, rest 60: the old code accepted this
        // split (155 <= 155) and putText wrote 154 — the name read back one
        // byte short. The fix must reject it and use the GNU record.
        val name = "p".repeat(74) + "/" + "q".repeat(80) + "/" + "r".repeat(60)
        val (bytes, root) = archiveOf(name)
        try {
            assertTrue(String(bytes, Charsets.ISO_8859_1).contains(longLinkMarker))
            assertArrayEquals(name.toByteArray(StandardCharsets.UTF_8), readLastName(bytes))
            val extracted = extractTo(bytes)
            assertTrue(Files.exists(extracted.resolve(name)))
        } finally {
            deleteRecursively(root)
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /** One temp file packed under the exact member name [member]. */
    private fun archiveOf(member: String): Pair<ByteArray, Path> {
        val root = Files.createTempDirectory("fastio-tar-bounds")
        val file = root.resolve("payload.txt")
        Files.write(file, "x".toByteArray())
        val sink = ByteArrayOutputStream()
        val outcome = TarCreateOutcome()
        // Member names come from the second pair element, so the on-disk
        // fixture stays short while the HEADER name hits the exact byte count.
        tarCreate(sink, listOf(file to member), 1000L, outcome)
        assertTrue("create must report no errors: ${outcome.errors}", outcome.errors.isEmpty())
        return sink.toByteArray() to root
    }

    /** Extract [bytes] into a fresh temp directory and return it. */
    private fun extractTo(bytes: ByteArray): Path {
        val dest = Files.createTempDirectory("fastio-tar-bounds-out")
        val out = TarExtractOutcome()
        tarExtract(ByteArrayInputStream(bytes), dest, false, 1000L, out)
        assertTrue("extract must report no errors: ${out.errors}", out.errors.isEmpty())
        return dest
    }

    /** The last member's name, read with the engine's own reader. */
    private fun readLastName(bytes: ByteArray): ByteArray {
        val reader = TarReader(ByteArrayInputStream(bytes))
        var name: String? = null
        while (true) {
            val member = reader.next() ?: break
            reader.skipData()
            name = member.name
        }
        return name!!.toByteArray(StandardCharsets.UTF_8)
    }

    private fun deleteRecursively(dir: Path) {
        Files.walk(dir).sorted(Comparator.reverseOrder()).forEach { Files.deleteIfExists(it) }
    }
}

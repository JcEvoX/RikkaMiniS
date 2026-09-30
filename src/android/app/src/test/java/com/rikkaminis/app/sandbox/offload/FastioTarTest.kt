package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.util.zip.GZIPInputStream
import java.util.zip.GZIPOutputStream

/**
 * `minis-fastio tar` engine ([T-minis-fastio] phase 3).
 *
 * These tests own the FORMAT half of the evidence: ustar field layout, name
 * splitting, long-name fallback, checksum, pax/GNU record handling, and the
 * tar-slip rule. The other half — that the guest's real `busybox tar 1.37`
 * accepts what this writes and vice versa — cannot live here (it needs the
 * sandbox's tar) and is covered by the interop probe
 * (`shared/work/fastio-p3/jvm/run_probe3.sh`), which drives the same engine
 * through the real handler with `tar` as the counterparty. A reader/writer pair
 * that only agrees with itself proves nothing about a container format, which
 * is why both halves exist.
 *
 * Expected values are hard literals, and the hostile archives are built by a
 * SECOND, independent implementation ([craftHeader]) rather than by the writer
 * under test — an archive the writer mangles would otherwise still be checked
 * against the writer's own idea of the format.
 */
class FastioTarTest {

    // ── round trip ──────────────────────────────────────────────────────────

    @Test
    fun `create and extract round-trips content, symlinks and counts`() {
        val root = Files.createTempDirectory("fastio-tar")
        val tree = Files.createDirectory(root.resolve("tree"))
        Files.write(tree.resolve("a.txt"), "12345".toByteArray())
        val sub = Files.createDirectory(tree.resolve("sub"))
        Files.write(sub.resolve("c.log"), "cc".toByteArray())
        Files.createDirectory(tree.resolve("empty"))
        Files.createSymbolicLink(tree.resolve("link.txt"), Paths.get("a.txt"))
        val dest = root.resolve("out")

        val archive = ByteArrayOutputStream()
        val created = TarCreateOutcome()
        tarCreate(archive, listOf(tree to tree.toString()), 1000L, created)

        assertEquals(2L, created.files)
        assertEquals(3L, created.dirs)
        assertEquals(1L, created.symlinks)
        assertEquals(7L, created.bytes)
        assertEquals(0L, created.skippedSpecial)
        assertTrue(created.errors.isEmpty())

        val bytes = archive.toByteArray()
        val validation = TarExtractOutcome()
        val safe = tarValidate(ByteArrayInputStream(bytes), dest, 1000L, validation)
        assertTrue("engine's own archive must validate: ${validation.errors}", safe)

        // The stored name is the guest path minus its leading '/', so the tree
        // lands under dest/<that path>, not under dest/<basename>.
        val extractedAt = dest.resolve(tree.toString().trimStart('/'))
        val extracted = TarExtractOutcome()
        tarExtract(ByteArrayInputStream(bytes), dest, false, 1000L, extracted)

        assertEquals(2L, extracted.files)
        assertEquals(3L, extracted.dirs)
        assertEquals(1L, extracted.symlinks)
        assertEquals(7L, extracted.bytes)
        assertTrue(extracted.errors.isEmpty())

        val treeOut = extractedAt
        assertEquals("12345", Files.readAllBytes(treeOut.resolve("a.txt")).decodeToString())
        assertEquals("cc", Files.readAllBytes(treeOut.resolve("sub/c.log")).decodeToString())
        assertTrue(Files.isSymbolicLink(treeOut.resolve("link.txt")))
        assertEquals("a.txt", Files.readSymbolicLink(treeOut.resolve("link.txt")).toString())
        assertTrue(Files.isDirectory(treeOut.resolve("empty")))
    }

    @Test
    fun `gzip round-trips through the same engine`() {
        val root = Files.createTempDirectory("fastio-tar-gz")
        val tree = Files.createDirectory(root.resolve("tree"))
        Files.write(tree.resolve("a.txt"), "hello".toByteArray())
        val dest = root.resolve("out")

        val archive = ByteArrayOutputStream()
        GZIPOutputStream(archive).use { sink ->
            tarCreate(sink, listOf(tree to tree.toString()), 1000L, TarCreateOutcome())
        }
        val bytes = archive.toByteArray()

        // The stored name is the guest path minus its leading '/', so the tree
        // lands under dest/<that path>, not under dest/<basename>.
        val extractedAt = dest.resolve(tree.toString().trimStart('/'))
        val extracted = TarExtractOutcome()
        GZIPInputStream(ByteArrayInputStream(bytes)).use {
            tarExtract(it, dest, false, 1000L, extracted)
        }
        assertEquals(1L, extracted.files)
        assertEquals("hello", Files.readAllBytes(dest.resolve("$tree/a.txt")).decodeToString())
    }

    // ── name fields ─────────────────────────────────────────────────────────

    @Test
    fun `a name that fits the 100-byte field is stored verbatim`() {
        val unpacked = splitUstarName("tmp/fix/a.txt")
        assertEquals("", unpacked!!.first)
        assertEquals("tmp/fix/a.txt", unpacked.second)
    }

    @Test
    fun `a long name is split across name and prefix, and survives a read-back`() {
        val segment = "d".repeat(60)
        val name = "tmp/$segment/$segment/a.txt"      // 136 bytes: no plain fit
        val unpacked = splitUstarName(name)!!
        assertTrue("prefix must be non-empty", unpacked.first.isNotEmpty())
        assertTrue(unpacked.first.toByteArray().size <= 155)
        assertTrue(unpacked.second.toByteArray().size <= 100)

        val root = Files.createTempDirectory("fastio-tar-long")
        val deep = root.resolve(segment).resolve(segment)
        Files.createDirectories(deep)
        Files.write(deep.resolve("a.txt"), "x".toByteArray())
        val archive = ByteArrayOutputStream()
        tarCreate(archive, listOf(root to root.toString()), 1000L, TarCreateOutcome())

        val guest = root.toString().trimStart('/')
        val members = readMembers(archive.toByteArray())
        assertEquals("$guest/$segment/$segment/a.txt", members.last().name)
        assertEquals('0', members.last().type)
    }

    @Test
    fun `a name too long for the prefix split falls back to a GNU long-name record`() {
        // 200 bytes in ONE segment: too long for the 100-byte name field, and
        // there is no '/' available to split on once the path prefix is used up,
        // so the writer must fall back to a GNU long-name record. (The
        // filesystem's own 255-byte segment limit is why this is 200 and not
        // larger — a longer fixture cannot even be created.)
        val long = "e".repeat(200)
        assertNull(splitUstarName(long))

        val root = Files.createTempDirectory("fastio-tar-gnu")
        val dir = Files.createDirectory(root.resolve(long))
        Files.write(dir.resolve("a.txt"), "y".toByteArray())
        val archive = ByteArrayOutputStream()
        tarCreate(archive, listOf(root to root.toString()), 1000L, TarCreateOutcome())

        val bytes = archive.toByteArray()
        assertTrue(
            "the archive must contain a GNU @LongLink record",
            String(bytes, Charsets.ISO_8859_1).contains("././@LongLink"),
        )
        val guest = root.toString().trimStart('/')
        val members = readMembers(bytes)
        assertEquals("$guest/$long/a.txt", members.last().name)

        // and the engine reads its own long form back
        val dest = root.resolve("out")
        val out = TarExtractOutcome()
        tarExtract(ByteArrayInputStream(bytes), dest, false, 1000L, out)
        assertEquals("y", Files.readAllBytes(dest.resolve("$guest/$long/a.txt")).decodeToString())
    }

    @Test
    fun `splitUstarName gives up on a single segment longer than the name field`() {
        // The ustar ceiling is 155 (prefix) + '/' + 100 (name); a single segment
        // can never be split, so the writer must switch to a GNU record rather
        // than silently truncating the name.
        assertNull(splitUstarName("f".repeat(101)))
        assertNull(splitUstarName("f".repeat(4000)))
        assertEquals("f".repeat(100), splitUstarName("f".repeat(100))!!.second)
    }

    @Test
    fun `a pax path override wins over the header name`() {
        val pax = paxRecord("path", "real.txt")
        val archive = ByteArrayOutputStream()
        archive.write(craftHeader("PaxHeader", 'x', pax.toByteArray().size.toLong(), 420, ""))
        archive.write(pax.toByteArray())
        archive.write(ByteArray(pad(pax.toByteArray().size.toLong())))
        archive.write(craftHeader("placeholder", '0', 5, 420, ""))
        archive.write("hello".toByteArray())
        archive.write(ByteArray(pad(5)))
        archive.write(ByteArray(1024))

        val dest = Files.createTempDirectory("fastio-tar-pax").resolve("out")
        val out = TarExtractOutcome()
        tarExtract(ByteArrayInputStream(archive.toByteArray()), dest, false, 100L, out)

        assertEquals(1L, out.files)
        assertEquals(1L, out.paxHeaders)
        assertEquals("hello", Files.readAllBytes(dest.resolve("real.txt")).decodeToString())
        assertFalse(Files.exists(dest.resolve("placeholder")))
    }

    // ── special types ───────────────────────────────────────────────────────

    @Test
    fun `device nodes and fifos are skipped and counted, never invented`() {
        val archive = ByteArrayOutputStream()
        archive.write(craftHeader("dev/null", '3', 0, 420, ""))
        archive.write(craftHeader("pipe", '6', 0, 420, ""))
        archive.write(craftHeader("ok.txt", '0', 2, 420, ""))
        archive.write("hi".toByteArray())
        archive.write(ByteArray(pad(2)))
        archive.write(ByteArray(1024))

        val dest = Files.createTempDirectory("fastio-tar-special").resolve("out")
        val out = TarExtractOutcome()
        tarExtract(ByteArrayInputStream(archive.toByteArray()), dest, false, 100L, out)

        assertEquals(2L, out.skippedSpecial)
        assertEquals(1L, out.files)
        assertEquals(listOf("dev/null", "pipe"), out.skippedNames)
        assertFalse(Files.exists(dest.resolve("pipe")))
        assertEquals("hi", Files.readAllBytes(dest.resolve("ok.txt")).decodeToString())
    }

    @Test
    fun `hard links are reported instead of materialised`() {
        val archive = ByteArrayOutputStream()
        archive.write(craftHeader("a.txt", '0', 2, 420, ""))
        archive.write("hi".toByteArray())
        archive.write(ByteArray(pad(2)))
        archive.write(craftHeader("b.txt", '1', 0, 420, "a.txt"))
        archive.write(ByteArray(1024))

        val dest = Files.createTempDirectory("fastio-tar-hard").resolve("out")
        val out = TarExtractOutcome()
        tarExtract(ByteArrayInputStream(archive.toByteArray()), dest, false, 100L, out)

        assertEquals(1L, out.files)
        assertEquals(1L, out.skippedHardlink)
        assertEquals(listOf("b.txt"), out.skippedNames)
        assertEquals("hi", Files.readAllBytes(dest.resolve("a.txt")).decodeToString())
    }

    // ── tar slip ────────────────────────────────────────────────────────────

    @Test
    fun `the slip rule refuses absolute, climbing and empty names but keeps the legal ones`() {
        val dest = Files.createTempDirectory("fastio-tar-slip")
        fun member(name: String) = TarMember(name, '0', 0, 420, 0, "")

        assertTrue(tarEntryTarget(dest, member("../evil.txt")) is TarRefusal.Slip)
        assertTrue(tarEntryTarget(dest, member("/etc/passwd")) is TarRefusal.Slip)
        assertTrue(tarEntryTarget(dest, member("a/../../evil.txt")) is TarRefusal.Slip)
        assertTrue(tarEntryTarget(dest, member("..")) is TarRefusal.Slip)
        assertTrue(tarEntryTarget(dest, member(".")) is TarRefusal.Slip)
        assertNull(tarEntryTarget(dest, member("a/../b.txt")))
        assertNull(tarEntryTarget(dest, member("dir/file.txt")))
        assertNull(tarEntryTarget(dest, member("./dir/file.txt")))
    }

    @Test
    fun `extraction refuses a name that writes through a symlink the archive itself declares`() {
        val archive = ByteArrayOutputStream()
        archive.write(craftHeader("lnk", '2', 0, 511, "/tmp/elsewhere"))
        archive.write(craftHeader("lnk/pwned.txt", '0', 2, 420, ""))
        archive.write("hi".toByteArray())
        archive.write(ByteArray(pad(2)))
        archive.write(ByteArray(1024))

        val root = Files.createTempDirectory("fastio-tar-chain")
        val dest = root.resolve("out")
        val validation = TarExtractOutcome()
        val safe = tarValidate(ByteArrayInputStream(archive.toByteArray()), dest, 100L, validation)

        assertFalse(safe)
        assertTrue(validation.errors.toString(), validation.errors[0].contains("tar_slip"))
        assertTrue(validation.errors[0].contains("declared by this archive"))
        assertFalse("validation must not have created anything", Files.exists(dest))
    }

    @Test
    fun `extraction refuses a name whose parent chain crosses a symlink already on disk`() {
        val root = Files.createTempDirectory("fastio-tar-ondisk")
        val dest = Files.createDirectory(root.resolve("out"))
        val outside = Files.createDirectory(root.resolve("outside"))
        Files.createSymbolicLink(dest.resolve("lnk"), outside)

        val archive = ByteArrayOutputStream()
        archive.write(craftHeader("lnk/pwned.txt", '0', 2, 420, ""))
        archive.write("hi".toByteArray())
        archive.write(ByteArray(pad(2)))
        archive.write(ByteArray(1024))

        val validation = TarExtractOutcome()
        val safe = tarValidate(ByteArrayInputStream(archive.toByteArray()), dest, 100L, validation)

        assertFalse(safe)
        assertTrue(validation.errors.toString(), validation.errors[0].contains("tar_slip"))
        assertFalse(Files.exists(outside.resolve("pwned.txt")))
    }

    @Test
    fun `a hostile entry anywhere leaves the destination untouched - the benign control proves the rule is not blanket`() {
        val good = ByteArrayOutputStream()
        good.write(craftHeader("dir/", '5', 0, 493, ""))
        good.write(craftHeader("dir/hello.txt", '0', 2, 420, ""))
        good.write("hi".toByteArray())
        good.write(ByteArray(pad(2)))
        good.write(ByteArray(1024))

        val bad = ByteArrayOutputStream()
        bad.write(craftHeader("dir/", '5', 0, 493, ""))
        bad.write(craftHeader("dir/hello.txt", '0', 2, 420, ""))
        bad.write("hi".toByteArray())
        bad.write(ByteArray(pad(2)))
        bad.write(craftHeader("../escape.txt", '0', 2, 420, ""))
        bad.write("hi".toByteArray())
        bad.write(ByteArray(pad(2)))
        bad.write(ByteArray(1024))

        val root = Files.createTempDirectory("fastio-tar-atomic")
        val goodDest = root.resolve("good")
        val goodOutcome = TarExtractOutcome()
        assertTrue(tarValidate(ByteArrayInputStream(good.toByteArray()), goodDest, 100L, goodOutcome))
        assertEquals("the benign archive has dir/ and dir/hello.txt", 2, countMembers(good.toByteArray()))

        val badDest = root.resolve("bad")
        val badOutcome = TarExtractOutcome()
        assertFalse(tarValidate(ByteArrayInputStream(bad.toByteArray()), badDest, 100L, badOutcome))
        assertFalse(
            "a refusal must leave nothing behind, not even the good prefix",
            Files.exists(badDest),
        )
    }

    @Test
    fun `a corrupt header checksum is refused`() {
        val block = craftHeader("ok.txt", '0', 2, 420, "")
        block[148] = '9'.code.toByte()
        val archive = ByteArrayOutputStream()
        archive.write(block)
        archive.write("hi".toByteArray())
        archive.write(ByteArray(pad(2)))
        archive.write(ByteArray(1024))

        val dest = Files.createTempDirectory("fastio-tar-sum").resolve("out")
        val out = TarExtractOutcome()
        val safe = tarValidate(ByteArrayInputStream(archive.toByteArray()), dest, 100L, out)
        assertFalse(safe)
        assertTrue(out.errors.toString(), out.errors[0].contains("checksum"))
        assertFalse(Files.exists(dest))
    }

    @Test
    fun `a truncated archive is refused rather than half-extracted`() {
        val full = ByteArrayOutputStream()
        full.write(craftHeader("a.txt", '0', 100, 420, ""))
        full.write(ByteArray(512))
        val truncated = full.toByteArray().copyOf(600)   // data cut mid-entry

        val dest = Files.createTempDirectory("fastio-tar-cut").resolve("out")
        val out = TarExtractOutcome()
        val safe = tarValidate(ByteArrayInputStream(truncated), dest, 100L, out)
        assertFalse(safe)
        assertTrue(out.errors.toString(), out.errors[0].contains("truncated"))
    }

    // ── overwrite policy ────────────────────────────────────────────────────

    @Test
    fun `extraction reuses an existing directory but refuses to replace an existing file without force`() {
        val archive = ByteArrayOutputStream()
        archive.write(craftHeader("dir/", '5', 0, 493, ""))
        archive.write(craftHeader("dir/hello.txt", '0', 5, 420, ""))
        archive.write("fresh".toByteArray())
        archive.write(ByteArray(pad(5)))
        archive.write(ByteArray(1024))
        val bytes = archive.toByteArray()

        val root = Files.createTempDirectory("fastio-tar-overwrite")
        val dest = Files.createDirectory(root.resolve("out"))
        Files.createDirectory(dest.resolve("dir"))
        Files.write(dest.resolve("dir/hello.txt"), "old".toByteArray())

        val refused = TarExtractOutcome()
        tarExtract(ByteArrayInputStream(bytes), dest, false, 100L, refused)
        assertEquals(0L, refused.files)
        assertEquals(1L, refused.dirs)
        assertTrue(refused.errors[0].contains("already_exists"))
        assertEquals("old", Files.readAllBytes(dest.resolve("dir/hello.txt")).decodeToString())

        val forced = TarExtractOutcome()
        tarExtract(ByteArrayInputStream(bytes), dest, true, 100L, forced)
        assertEquals(1L, forced.files)
        assertTrue(forced.errors.isEmpty())
        assertEquals("fresh", Files.readAllBytes(dest.resolve("dir/hello.txt")).decodeToString())
    }

    @Test
    fun `the entry budget stops the walk and says so`() {
        val root = Files.createTempDirectory("fastio-tar-budget")
        val tree = Files.createDirectory(root.resolve("tree"))
        for (i in 0 until 5) Files.write(tree.resolve("f$i.txt"), "x".toByteArray())

        val out = TarCreateOutcome()
        tarCreate(ByteArrayOutputStream(), listOf(tree to tree.toString()), 3L, out)

        assertTrue(out.truncated)
        assertEquals(3L, out.visited)
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /** Read every member of [bytes] with the engine's own reader (names/types only). */
    private fun readMembers(bytes: ByteArray): List<TarMember> {
        val reader = TarReader(ByteArrayInputStream(bytes))
        val members = ArrayList<TarMember>()
        while (true) {
            val member = reader.next() ?: break
            reader.skipData()
            members.add(member)
        }
        return members
    }

    private fun countMembers(bytes: ByteArray): Int = readMembers(bytes).size

    private fun pad(size: Long): Int = ((512 - (size % 512)) % 512).toInt()

    /** pax record: `"<length> key=value\n"`, where length counts the whole record. */
    private fun paxRecord(key: String, value: String): String {
        val body = "$key=$value\n"
        var length = body.length + 1
        while (true) {
            val candidate = "$length $body"
            if (candidate.length == length) return candidate
            length = candidate.length
        }
    }

    /**
     * A SECOND ustar writer, deliberately independent of [tarCreate]: hostile
     * archives must not be produced by the code under test, or a format bug
     * would be baked into both sides of the assertion.
     */
    private fun craftHeader(name: String, type: Char, size: Long, mode: Int, link: String): ByteArray {
        val block = ByteArray(512)
        val nameBytes = name.toByteArray()
        assertTrue("craft header only writes short names", nameBytes.size <= 100)
        System.arraycopy(nameBytes, 0, block, 0, nameBytes.size)
        putOctal(block, 100, 8, mode.toLong())
        putOctal(block, 108, 8, 0L)
        putOctal(block, 116, 8, 0L)
        putOctal(block, 124, 12, size)
        putOctal(block, 136, 12, 0L)
        for (i in 148 until 156) block[i] = ' '.code.toByte()
        block[156] = type.code.toByte()
        val linkBytes = link.toByteArray()
        System.arraycopy(linkBytes, 0, block, 157, linkBytes.size)
        block[257] = 'u'.code.toByte()
        block[258] = 's'.code.toByte()
        block[259] = 't'.code.toByte()
        block[260] = 'a'.code.toByte()
        block[261] = 'r'.code.toByte()
        block[262] = 0
        block[263] = '0'.code.toByte()
        block[264] = '0'.code.toByte()
        var sum = 0L
        for (b in block) sum += (b.toLong() and 0xFF)
        putOctal(block, 148, 7, sum)
        block[155] = ' '.code.toByte()
        return block
    }

    private fun putOctal(block: ByteArray, offset: Int, length: Int, value: Long) {
        val text = java.lang.Long.toOctalString(value)
        val width = length - 1
        val padded = if (text.length >= width) text.substring(text.length - width) else "0".repeat(width - text.length) + text
        for (i in padded.indices) block[offset + i] = padded[i].code.toByte()
        block[offset + width] = 0
    }
}

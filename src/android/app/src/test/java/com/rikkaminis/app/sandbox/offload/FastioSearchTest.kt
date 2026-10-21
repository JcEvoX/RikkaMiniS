package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.RandomAccessFile
import java.nio.file.FileSystems
import java.nio.file.Files
import java.nio.file.Path

/**
 * `minis-fastio find` / `grep` walk engines ([T-minis-fastio] phase 2).
 *
 * Expectations are hard literals derived from the fixtures (sizes read back
 * from the fixture itself where the platform decides them), never from the
 * constants under test — a changed default must turn these red rather than
 * silently follow along.
 */
class FastioSearchTest {

    /** root/{a.txt, b.log, sub/{c.log, d.txt}} — 4 files, 2 dirs. */
    private fun findFixture(): Path {
        val root = Files.createTempDirectory("fastio-find")
        Files.write(root.resolve("a.txt"), "alpha\n".toByteArray())
        Files.write(root.resolve("b.log"), "beta\n".toByteArray())
        val sub = Files.createDirectory(root.resolve("sub"))
        Files.write(sub.resolve("c.log"), "gamma\n".toByteArray())
        Files.write(sub.resolve("d.txt"), "delta\n".toByteArray())
        return root
    }

    @Test
    fun `find counts the whole tree and reconstructs guest paths`() {
        val root = findFixture()
        val out = FindOutcome()

        findTree(root, "/ws", matcher = null, typeFilter = null, limit = 100, maxEntries = 1_000, out = out)

        assertEquals(4L, out.files)
        assertEquals(2L, out.dirs)
        assertEquals(6L, out.matched)
        assertEquals(
            listOf("/ws", "/ws/a.txt", "/ws/b.log", "/ws/sub", "/ws/sub/c.log", "/ws/sub/d.txt"),
            out.matches.map { it.guestPath }.sorted(),
        )
        assertFalse(out.matchesCapped)
        assertFalse(out.truncated)
        assertTrue(out.errors.isEmpty())
    }

    @Test
    fun `find -name matches the entry name only`() {
        val root = findFixture()
        val matcher = FileSystems.getDefault().getPathMatcher("glob:*.log")
        val out = FindOutcome()

        findTree(root, "/ws", matcher, null, limit = 100, maxEntries = 1_000, out = out)

        assertEquals(listOf("/ws/b.log", "/ws/sub/c.log"), out.matches.map { it.guestPath }.sorted())
        assertEquals(2L, out.matched)
    }

    @Test
    fun `find -type filters by entry type`() {
        val root = findFixture()
        val out = FindOutcome()

        findTree(root, "/ws", null, typeFilter = 'd', limit = 100, maxEntries = 1_000, out = out)

        assertEquals(listOf("/ws", "/ws/sub"), out.matches.map { it.guestPath }.sorted())
        assertEquals(2L, out.matched)
    }

    @Test
    fun `find caps the listing but keeps the totals over the whole tree`() {
        val root = findFixture()
        val out = FindOutcome()

        findTree(root, "/ws", null, null, limit = 2, maxEntries = 1_000, out = out)

        assertEquals(2, out.matches.size)
        assertTrue(out.matchesCapped)
        assertEquals(6L, out.matched)   // authoritative even though only 2 were listed
    }

    @Test
    fun `find reports the traversal budget as truncation`() {
        val root = findFixture()
        val out = FindOutcome()

        findTree(root, "/ws", null, null, limit = 100, maxEntries = 1, out = out)

        assertTrue(out.truncated)
        assertEquals(1L, out.dirs)   // the root is charged before the budget stops the walk
        assertEquals(0L, out.files)
    }

    @Test
    fun `find reports a symlink as type l and never follows it`() {
        val root = Files.createTempDirectory("fastio-find-link")
        val outside = Files.createTempDirectory("fastio-find-outside")
        Files.write(outside.resolve("victim.txt"), "v".toByteArray())
        Files.createSymbolicLink(root.resolve("link"), outside)
        val out = FindOutcome()

        findTree(root, "/ws", null, typeFilter = 'l', limit = 100, maxEntries = 1_000, out = out)

        assertEquals(listOf("/ws/link"), out.matches.map { it.guestPath }.sorted())
        assertEquals(1L, out.files)   // counters are filter-independent: the link is a file entry
    }

    // ── grep ────────────────────────────────────────────────────────────────

    /** root/{a.txt, b.txt, bin.dat, link.txt, sub/c.txt} */
    private fun grepFixture(): Path {
        val root = Files.createTempDirectory("fastio-grep")
        Files.write(root.resolve("a.txt"), "one\ntwo\nthree\n".toByteArray())
        Files.write(root.resolve("b.txt"), "nothing here\n".toByteArray())
        val sub = Files.createDirectory(root.resolve("sub"))
        Files.write(sub.resolve("c.txt"), "two again\n".toByteArray())
        Files.write(root.resolve("bin.dat"), byteArrayOf(0, 1, 2, 65, 66))
        Files.createSymbolicLink(root.resolve("link.txt"), root.resolve("a.txt"))
        return root
    }

    @Test
    fun `grep reports file, line number and text and counts the skips`() {
        val root = grepFixture()
        val out = GrepOutcome()

        grepTree(root, "/ws", Regex("two"), limit = 100, maxEntries = 1_000, out = out)

        assertEquals(
            listOf("/ws/a.txt:2", "/ws/sub/c.txt:1"),
            out.matches.map { "${it.guestPath}:${it.lineNumber}" }.sorted(),
        )
        assertEquals("two", out.matches.first { it.guestPath == "/ws/a.txt" }.text)
        assertEquals(3L, out.filesScanned)          // a.txt, b.txt, sub/c.txt
        assertEquals(1L, out.filesSkippedBinary)    // bin.dat (NUL in the first block)
        assertEquals(1L, out.filesSkippedLink)      // link.txt
        assertEquals(0L, out.filesSkippedLarge)
        assertFalse(out.matchesCapped)
        assertTrue(out.errors.isEmpty())
    }

    @Test
    fun `grep honours a case-insensitive pattern from the caller`() {
        val root = grepFixture()
        val out = GrepOutcome()

        grepTree(root, "/ws", Regex("TWO", RegexOption.IGNORE_CASE), limit = 100, maxEntries = 1_000, out = out)

        assertEquals(2, out.matches.size)
        assertEquals("two", out.matches.first { it.guestPath == "/ws/a.txt" }.text)
    }

    @Test
    fun `grep stops at the match budget and flags it`() {
        val root = Files.createTempDirectory("fastio-grep-cap")
        Files.write(root.resolve("many.txt"), "hit\nhit\nhit\nhit\nhit\n".toByteArray())
        val out = GrepOutcome()

        grepTree(root, "/ws", Regex("hit"), limit = 2, maxEntries = 1_000, out = out)

        assertEquals(2, out.matches.size)
        assertEquals(listOf(1, 2), out.matches.map { it.lineNumber })
        assertTrue(out.matchesCapped)
    }

    @Test
    fun `grep clips a long matching line to 200 chars plus the ellipsis`() {
        val root = Files.createTempDirectory("fastio-grep-long")
        Files.write(root.resolve("long.txt"), ("x".repeat(500) + "NEEDLE\n").toByteArray())
        val out = GrepOutcome()

        grepTree(root, "/ws", Regex("NEEDLE"), limit = 100, maxEntries = 1_000, out = out)

        assertEquals(1, out.matches.size)
        assertEquals(201, out.matches[0].text.length)
        assertTrue(out.matches[0].text.endsWith("…"))
    }

    @Test
    fun `grep skips files above the size ceiling without reading them`() {
        val root = Files.createTempDirectory("fastio-grep-big")
        val big = root.resolve("big.txt")
        RandomAccessFile(big.toFile(), "rw").use { it.setLength(8L * 1024 * 1024 + 1) }
        Files.write(root.resolve("small.txt"), "needle\n".toByteArray())
        val out = GrepOutcome()

        grepTree(root, "/ws", Regex("needle"), limit = 100, maxEntries = 1_000, out = out)

        assertEquals(1L, out.filesSkippedLarge)
        assertEquals(1L, out.filesScanned)
        assertEquals(listOf("/ws/small.txt"), out.matches.map { it.guestPath })
    }

    // ── [dv19-A1 A-03] the budget is spent per ROOT, not per tree ────────────
    //
    // `grep` (the handler) carries ONE match budget across several roots:
    // `remaining -= outcome.matches.size`, and the next root is walked with
    // whatever is left. A root that opens with `limit == 0` used to be walked
    // end to end — the walk only learned the budget was gone at the first
    // MATCHING line (grepFile:220), so a root of non-matching files was read in
    // full (up to 8 MiB per file) to find nothing. The invariant these cases
    // pin: once the budget is spent, no file is opened and no directory is
    // entered.

    @Test
    fun `a root reached with a spent budget is never scanned`() {
        val first = Files.createTempDirectory("fastio-grep-first")
        // Three hits for a budget of two: the third is what sets matchesCapped
        // ("exactly limit matches" is deliberately NOT capped — see the KDoc).
        Files.write(first.resolve("hits.txt"), "hit\nhit\nhit\n".toByteArray())
        val second = Files.createTempDirectory("fastio-grep-second")
        Files.write(second.resolve("a.txt"), "alpha\n".toByteArray())
        val sub = Files.createDirectory(second.resolve("sub"))
        Files.write(sub.resolve("b.txt"), "beta\n".toByteArray())

        val firstOut = GrepOutcome()
        grepTree(first, "/one", Regex("hit"), limit = 2, maxEntries = 1_000, out = firstOut)
        assertEquals(2, firstOut.matches.size)
        assertTrue(firstOut.matchesCapped)

        // Exactly what FastioOffloadHandler.grep does with `remaining`.
        val remaining = 2 - firstOut.matches.size
        assertEquals(0, remaining)
        val secondOut = GrepOutcome()
        grepTree(second, "/two", Regex("hit"), limit = remaining, maxEntries = 1_000, out = secondOut)

        assertTrue(secondOut.matches.isEmpty())
        assertTrue(secondOut.matchesCapped)
        assertEquals(0L, secondOut.filesScanned)
        assertEquals(0L, secondOut.bytesScanned)
        assertEquals(0L, secondOut.visited)
    }

    @Test
    fun `a zero budget lists nothing and opens no file`() {
        val root = grepFixture()
        val out = GrepOutcome()

        grepTree(root, "/ws", Regex("two"), limit = 0, maxEntries = 1_000, out = out)

        assertTrue(out.matches.isEmpty())
        assertTrue(out.matchesCapped)
        assertEquals(0L, out.filesScanned)
        assertEquals(0L, out.bytesScanned)
        assertEquals(0L, out.visited)
        assertFalse(out.truncated)
    }

    @Test
    fun `the file that fills the budget is the last entry charged`() {
        val root = Files.createTempDirectory("fastio-grep-stop")
        Files.write(root.resolve("a.txt"), "hit\nhit\nhit\n".toByteArray())
        Files.write(root.resolve("b.txt"), "hit\nhit\nhit\n".toByteArray())
        val sub = Files.createDirectory(root.resolve("sub"))
        Files.write(sub.resolve("c.txt"), "nothing\n".toByteArray())
        val out = GrepOutcome()

        grepTree(root, "/ws", Regex("hit"), limit = 2, maxEntries = 1_000, out = out)

        // Whichever file the walk reaches first fills the budget; whatever came
        // next — a sibling file or a subdirectory — is never entered, so the
        // only charged entries are the root and that one file.
        assertEquals(2, out.matches.size)
        assertTrue(out.matchesCapped)
        assertEquals(1L, out.filesScanned)
        assertEquals(2L, out.visited)
        assertFalse(out.truncated)
    }

    @Test
    fun `an already capped outcome never enters a directory`() {
        val root = grepFixture()
        val out = GrepOutcome()
        out.matchesCapped = true // as set by the file that filled the budget

        grepTree(root, "/ws", Regex("two"), limit = 100, maxEntries = 1_000, out = out)

        assertEquals(0L, out.visited)
        assertEquals(0L, out.filesScanned)
        assertFalse(out.truncated)
    }
}

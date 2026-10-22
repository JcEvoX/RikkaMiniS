package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.nio.file.Files
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * [dv22-A2 §1042-A-02] `tarCreate`'s TOP-LEVEL root loop. `writeTree` always
 * charged the budget, but the loop around it did not: `writeRegular` /
 * `writeLink` on a top-level root incremented `visited` with no ceiling, the
 * budget-exhausted `false` from `writeTree` was discarded (later roots kept
 * writing), and a top-level FIFO went straight into `writeRegular`, whose
 * `Files.newInputStream` blocks forever on a pipe with no writer.
 *
 * The literals below are hand-written, never derived from the code under test.
 */
class FastioTarTopLevelTest {

    @Test
    fun `top-level files stop at the budget without double counting`() {
        val root = Files.createTempDirectory("fastio-tar-top-budget")
        for (i in 0 until 3) Files.write(root.resolve("f$i.txt"), "x".toByteArray())

        val out = TarCreateOutcome()
        val sink = ByteArrayOutputStream()
        tarCreate(sink, listOf(
            root.resolve("f0.txt") to "f0.txt",
            root.resolve("f1.txt") to "f1.txt",
            root.resolve("f2.txt") to "f2.txt",
        ), 2L, out)

        // Two of the three roots fit; the third is refused by the gate, which
        // must not charge anything itself (writeRegular's own `visited++` is
        // the single count per entry).
        assertTrue(out.truncated)
        assertEquals(2L, out.visited)
        assertEquals(2L, out.files)
        assertTrue(out.errors.isEmpty())
    }

    @Test
    fun `a top-level file inside the budget is archived`() {
        val root = Files.createTempDirectory("fastio-tar-top-ok")
        Files.write(root.resolve("a.txt"), "payload".toByteArray())

        val out = TarCreateOutcome()
        val sink = ByteArrayOutputStream()
        tarCreate(sink, listOf(root.resolve("a.txt") to "a.txt"), 10L, out)

        assertFalse(out.truncated)
        assertEquals(1L, out.visited)
        assertEquals(1L, out.files)
        assertTrue(out.errors.isEmpty())
    }

    @Test
    fun `a tree that exhausts the budget stops the whole root list`() {
        val root = Files.createTempDirectory("fastio-tar-top-tree")
        val tree = Files.createDirectory(root.resolve("tree"))
        Files.write(tree.resolve("one.txt"), "x".toByteArray())
        Files.write(tree.resolve("two.txt"), "x".toByteArray())
        val afterTree = root.resolve("after.txt")
        Files.write(afterTree, "x".toByteArray())

        val out = TarCreateOutcome()
        // Real budget semantics (frozen by `the entry budget stops the walk`):
        // the dir header is charged once (visited=1); a child file is charged
        // once AND writeRegular counts its own write (visited=2, then 3). The
        // budget runs out exactly at two.txt's charge, so the walk stops with
        // one file archived — and the second ROOT must not be written either.
        tarCreate(ByteArrayOutputStream(), listOf(
            tree to "tree",
            afterTree to "after.txt",
        ), 3L, out)

        // writeTree returns false at the third charge; the loop must consume
        // it and NOT write the second root (the old code archived after.txt
        // past the budget: files was 3 and the budget was already gone).
        assertTrue(out.truncated)
        assertEquals(3L, out.visited)
        assertEquals(1L, out.files)
        assertEquals(1L, out.dirs)
    }

    @Test
    fun `a top-level fifo is counted and never read`() {
        val root = Files.createTempDirectory("fastio-tar-top-fifo")
        val fifo = root.resolve("pipe")
        val made = ProcessBuilder("mkfifo", fifo.toString()).start().waitFor()
        assertEquals("mkfifo must succeed (CI runs on ubuntu)", 0, made)

        val out = TarCreateOutcome()
        val sink = ByteArrayOutputStream()
        val done = CountDownLatch(1)
        val worker = Thread {
            tarCreate(sink, listOf(fifo to "pipe"), 10L, out)
            done.countDown()
        }
        worker.isDaemon = true
        worker.start()
        // The pre-fix code opened the FIFO for reading (no writer: blocks
        // forever, worker thread dead). The fix must SKIP it instead.
        assertTrue(
            "tarCreate hung reading a top-level FIFO",
            done.await(30, TimeUnit.SECONDS),
        )

        assertEquals(1L, out.skippedSpecial)
        assertEquals(1, out.skippedNames.size)
        assertEquals(0L, out.visited)
        assertEquals(0L, out.files)
        assertFalse(out.truncated)
        assertTrue(out.errors.isEmpty())
        // Only the two end-of-archive blocks — no member was written.
        assertEquals(1024, sink.size())
    }

    @Test
    fun `a top-level symlink gets the budget gate too`() {
        val root = Files.createTempDirectory("fastio-tar-top-link")
        val target = root.resolve("target")
        Files.write(target, "x".toByteArray())
        val link = root.resolve("link")
        Files.createSymbolicLink(link, target)

        val refused = TarCreateOutcome()
        tarCreate(ByteArrayOutputStream(), listOf(link to "link"), 0L, refused)
        // Budget 0: the gate must fire before writeLink can even count the link
        // (the old code wrote it, leaving visited past maxEntries).
        assertTrue(refused.truncated)
        assertEquals(0L, refused.visited)
        assertEquals(0L, refused.symlinks)
        assertTrue(refused.errors.isEmpty())

        val written = TarCreateOutcome()
        tarCreate(ByteArrayOutputStream(), listOf(link to "link"), 5L, written)
        assertFalse(written.truncated)
        assertEquals(1L, written.symlinks)
        assertEquals(1L, written.visited)
    }
}

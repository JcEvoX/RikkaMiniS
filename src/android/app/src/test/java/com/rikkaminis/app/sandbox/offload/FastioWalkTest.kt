package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.attribute.BasicFileAttributes

/**
 * Byte-accounting semantics for the fastio walk engine.
 *
 * P1 fix 2026-09-30: directory st_size must NOT be counted — the guest's
 * PATH `du -sb` resolves to GNU coreutils 9.5 in this rootfs (file entries
 * only). On this platform every directory reports st_size 3452, so counting
 * dirs inflates bytes by dirs × 3452 (in-sandbox fixture: GNU du -sb = 5,
 * busybox du -sb = 6909 on a 5-byte file + 2 dirs). Expectations below are
 * hard literals derived from the fixture, not from the code under test
 * (symlink sizes are read from the fixture itself — ground truth, not the
 * walk's output).
 */
class FastioWalkTest {

    @Test
    fun `du counts file entries only - directories contribute zero bytes`() {
        val root = Files.createTempDirectory("fastio-du")
        Files.write(root.resolve("f5.bin"), "12345".toByteArray())          // 5 bytes
        val sub = Files.createDirectory(root.resolve("sub"))
        Files.write(sub.resolve("f3.bin"), "abc".toByteArray())             // 3 bytes

        val totals = WalkTotals()
        walkForSize(root, totals, 1_000)

        assertEquals(2L, totals.files)
        assertEquals(2L, totals.dirs)
        assertEquals(8L, totals.bytes)     // 5 + 3 — NOT +2×3452 (dir st_size excluded)
        assertTrue(totals.errors.isEmpty())
        assertFalse(totals.truncated)
    }

    @Test
    fun `du does not follow symlinked directories`() {
        val root = Files.createTempDirectory("fastio-du-link")
        Files.write(root.resolve("f5.bin"), "12345".toByteArray())          // 5 bytes
        val outside = Files.createTempDirectory("fastio-du-outside")
        Files.write(outside.resolve("victim.bin"), "V".repeat(10).toByteArray())
        val link = Files.createSymbolicLink(root.resolve("dlink"), outside)
        val linkSize = Files.readAttributes(
            link, BasicFileAttributes::class.java, LinkOption.NOFOLLOW_LINKS,
        ).size()

        val totals = WalkTotals()
        walkForSize(root, totals, 1_000)

        assertEquals(2L, totals.files)     // f5.bin + the link itself (as a file entry)
        assertEquals(1L, totals.dirs)
        assertEquals(5L + linkSize, totals.bytes)   // victim's 10 bytes excluded
        assertTrue(Files.exists(outside.resolve("victim.bin")))
    }

    @Test
    fun `rm walk frees file bytes only and never follows symlinks out of the tree`() {
        val root = Files.createTempDirectory("fastio-rm")
        val outside = Files.createTempDirectory("fastio-rm-outside")
        Files.write(outside.resolve("victim.bin"), "V".repeat(10).toByteArray())
        val sub = Files.createDirectory(root.resolve("sub"))
        Files.write(sub.resolve("f3.bin"), "abc".toByteArray())             // 3 bytes
        Files.write(root.resolve("f5.bin"), "12345".toByteArray())          // 5 bytes
        val link = Files.createSymbolicLink(root.resolve("dlink"), outside)
        val linkSize = Files.readAttributes(
            link, BasicFileAttributes::class.java, LinkOption.NOFOLLOW_LINKS,
        ).size()

        val totals = WalkTotals()
        deleteTree(root, totals)

        assertEquals(3L, totals.files)     // f5.bin + f3.bin + the link (unlinked)
        assertEquals(2L, totals.dirs)      // sub + root
        assertEquals(8L + linkSize, totals.bytes)   // freed_bytes: no dir st_size (2×3452)
        assertTrue(!Files.exists(root))
        assertTrue(Files.exists(outside.resolve("victim.bin")))   // never followed
    }

    @Test
    fun `du truncates at the entry budget after counting the root dir`() {
        val root = Files.createTempDirectory("fastio-du-trunc")
        Files.write(root.resolve("a.bin"), "1".toByteArray())
        Files.write(root.resolve("b.bin"), "22".toByteArray())
        Files.write(root.resolve("c.bin"), "333".toByteArray())

        val totals = WalkTotals()
        walkForSize(root, totals, 1)       // budget 1: the root consumes it

        assertEquals(1L, totals.dirs)      // root visited before the budget hit
        assertEquals(0L, totals.files)     // first file charge terminates the walk
        assertEquals(0L, totals.bytes)
        assertTrue(totals.truncated)
    }
}

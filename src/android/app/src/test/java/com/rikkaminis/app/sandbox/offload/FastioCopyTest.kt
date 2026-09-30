package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.attribute.BasicFileAttributes

/**
 * `minis-fastio cp` / `mv` engines ([T-minis-fastio] phase 2).
 *
 * The overwrite policy itself lives in the handler (it needs the guest-path
 * context for its message); these tests pin the engine behaviour the policy
 * relies on — replace vs leave-alone — plus the symlink rule and the
 * copy-into-itself guard. Expectations are hard literals.
 */
class FastioCopyTest {

    @Test
    fun `copy copies a file and reports its bytes`() {
        val dir = Files.createTempDirectory("fastio-cp")
        val src = Files.write(dir.resolve("a.txt"), "12345".toByteArray())
        val out = CopyOutcome()

        copyTree(src, dir.resolve("b.txt"), force = false, out = out)

        assertEquals(1L, out.files)
        assertEquals(0L, out.dirs)
        assertEquals(5L, out.bytes)
        assertEquals(0L, out.overwritten)
        assertEquals("12345", Files.readString(dir.resolve("b.txt")))
        assertTrue(out.errors.isEmpty())
    }

    @Test
    fun `copy copies a tree recursively and reproduces a symlink instead of following it`() {
        val dir = Files.createTempDirectory("fastio-cp-tree")
        val outside = Files.createTempDirectory("fastio-cp-outside")
        Files.write(outside.resolve("victim.txt"), "V".toByteArray())
        val src = Files.createDirectory(dir.resolve("src"))
        Files.write(src.resolve("a.txt"), "abc".toByteArray())
        val sub = Files.createDirectory(src.resolve("sub"))
        Files.write(sub.resolve("b.txt"), "de".toByteArray())
        val link = Files.createSymbolicLink(src.resolve("dlink"), outside)
        val linkSize = Files.readAttributes(
            link,
            BasicFileAttributes::class.java,
            LinkOption.NOFOLLOW_LINKS,
        ).size()
        val out = CopyOutcome()

        copyTree(src, dir.resolve("dst"), force = false, out = out)

        assertEquals(3L, out.files)   // a.txt + b.txt + the link itself
        assertEquals(2L, out.dirs)
        assertEquals(5L + linkSize, out.bytes)
        assertTrue(Files.isSymbolicLink(dir.resolve("dst/dlink")))
        assertEquals(outside, Files.readSymbolicLink(dir.resolve("dst/dlink")))
        assertTrue(Files.exists(outside.resolve("victim.txt")))
        assertTrue(out.errors.isEmpty())
    }

    @Test
    fun `copy without force records an existing target and leaves it untouched`() {
        val dir = Files.createTempDirectory("fastio-cp-noforce")
        val src = Files.write(dir.resolve("a.txt"), "new".toByteArray())
        val dst = Files.write(dir.resolve("b.txt"), "old".toByteArray())
        val out = CopyOutcome()

        copyTree(src, dst, force = false, out = out)

        assertEquals(1, out.errors.size)
        assertEquals(0L, out.files)
        assertEquals(0L, out.overwritten)
        assertEquals("old", Files.readString(dst))
    }

    @Test
    fun `copy with force replaces the target and counts the overwrite`() {
        val dir = Files.createTempDirectory("fastio-cp-force")
        val src = Files.write(dir.resolve("a.txt"), "new".toByteArray())
        val dst = Files.write(dir.resolve("b.txt"), "old".toByteArray())
        val out = CopyOutcome()

        copyTree(src, dst, force = true, out = out)

        assertEquals(1L, out.overwritten)
        assertEquals(1L, out.files)
        assertEquals(3L, out.bytes)
        assertEquals("new", Files.readString(dst))
        assertTrue(out.errors.isEmpty())
    }

    @Test
    fun `copy with force merges into an existing directory`() {
        val dir = Files.createTempDirectory("fastio-cp-merge")
        val src = Files.createDirectory(dir.resolve("src"))
        Files.write(src.resolve("a.txt"), "a".toByteArray())
        val dst = Files.createDirectory(dir.resolve("dst"))
        Files.write(dst.resolve("kept.txt"), "k".toByteArray())
        val out = CopyOutcome()

        copyTree(src, dst, force = true, out = out)

        assertEquals(1L, out.files)     // only a.txt is new; kept.txt is not touched
        assertEquals(0L, out.dirs)      // dst already existed
        assertEquals("a", Files.readString(dst.resolve("a.txt")))
        assertEquals("k", Files.readString(dst.resolve("kept.txt")))
        assertTrue(out.errors.isEmpty())
    }

    @Test
    fun `move renames within one filesystem and reports the method`() {
        val dir = Files.createTempDirectory("fastio-mv")
        val src = Files.write(dir.resolve("a.txt"), "12345".toByteArray())
        val out = MoveOutcome()

        moveTree(src, dir.resolve("b.txt"), out)

        assertEquals("rename", out.method)
        assertFalse(out.counted)   // a rename costs no walk, so nothing was counted
        assertFalse(Files.exists(src))
        assertEquals("12345", Files.readString(dir.resolve("b.txt")))
        assertTrue(out.errors.isEmpty())
    }

    @Test
    fun `move renames a directory tree in one step`() {
        val dir = Files.createTempDirectory("fastio-mv-tree")
        val src = Files.createDirectory(dir.resolve("src"))
        Files.write(src.resolve("a.txt"), "a".toByteArray())
        val sub = Files.createDirectory(src.resolve("sub"))
        Files.write(sub.resolve("b.txt"), "bb".toByteArray())
        val out = MoveOutcome()

        moveTree(src, dir.resolve("dst"), out)

        assertEquals("rename", out.method)
        assertFalse(Files.exists(src))
        assertEquals("a", Files.readString(dir.resolve("dst/a.txt")))
        assertEquals("bb", Files.readString(dir.resolve("dst/sub/b.txt")))
        assertTrue(out.errors.isEmpty())
    }

    @Test
    fun `a target inside its own source is detected, including through a symlink alias`() {
        val dir = Files.createTempDirectory("fastio-inside")
        val src = Files.createDirectory(dir.resolve("a"))

        assertTrue(targetInsideSource(src, src.resolve("b")))    // cp -r a a/b
        assertTrue(targetInsideSource(src, src))                 // cp -r a a
        assertFalse(targetInsideSource(src, dir.resolve("ab")))  // sibling sharing a name prefix

        val alias = Files.createSymbolicLink(dir.resolve("link"), src)
        assertTrue(targetInsideSource(src, alias.resolve("b")))  // dir/link/b IS dir/a/b
    }

    @Test
    fun `targetInsideSource does not treat a sibling prefix as containment`() {
        val dir = Files.createTempDirectory("fastio-inside-2")
        val src = Files.createDirectory(dir.resolve("work"))
        assertFalse(targetInsideSource(src, dir.resolve("workspace")))
    }
}

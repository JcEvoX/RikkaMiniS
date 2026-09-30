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
        assertEquals("12345", Files.readAllBytes(dir.resolve("b.txt")).decodeToString())
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
        assertEquals("old", Files.readAllBytes(dst).decodeToString())
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
        assertEquals("new", Files.readAllBytes(dst).decodeToString())
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
        assertEquals("a", Files.readAllBytes(dst.resolve("a.txt")).decodeToString())
        assertEquals("k", Files.readAllBytes(dst.resolve("kept.txt")).decodeToString())
        assertTrue(out.errors.isEmpty())
    }

    @Test
    fun `move renames within one filesystem and reports the method`() {
        val dir = Files.createTempDirectory("fastio-mv")
        val src = Files.write(dir.resolve("a.txt"), "12345".toByteArray())
        val out = MoveOutcome()

        moveTree(src, dir.resolve("b.txt"), force = false, out = out)

        assertEquals("rename", out.method)
        assertFalse(out.counted)   // a rename costs no walk, so nothing was counted
        assertFalse(Files.exists(src))
        assertEquals("12345", Files.readAllBytes(dir.resolve("b.txt")).decodeToString())
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

        moveTree(src, dir.resolve("dst"), force = false, out = out)

        assertEquals("rename", out.method)
        assertFalse(Files.exists(src))
        assertEquals("a", Files.readAllBytes(dir.resolve("dst/a.txt")).decodeToString())
        assertEquals("bb", Files.readAllBytes(dir.resolve("dst/sub/b.txt")).decodeToString())
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

    // ── mv --force (the handler's overwrite contract) ───────────────────────
    //
    // The three below are what a bare `Files.move` gets wrong: the handler has
    // already decided the overwrite is wanted, so the ENGINE is the only place
    // that can lose it. Deleting REPLACE_EXISTING from `moveTree` must turn the
    // first one red — that is the negative control for this pair.

    @Test
    fun `move with force replaces an existing target`() {
        val dir = Files.createTempDirectory("fastio-mv-force")
        val src = Files.write(dir.resolve("a.txt"), "new".toByteArray())
        val dst = Files.write(dir.resolve("b.txt"), "old".toByteArray())
        val out = MoveOutcome()

        moveTree(src, dst, force = true, out = out)

        assertEquals("rename", out.method)
        assertTrue(out.errors.isEmpty())
        assertFalse(Files.exists(src))
        assertEquals("new", Files.readAllBytes(dst).decodeToString())
    }

    @Test
    fun `move without force leaves the target alone and says why`() {
        val dir = Files.createTempDirectory("fastio-mv-noforce")
        val src = Files.write(dir.resolve("a.txt"), "new".toByteArray())
        val dst = Files.write(dir.resolve("b.txt"), "old".toByteArray())
        val out = MoveOutcome()

        moveTree(src, dst, force = false, out = out)

        assertEquals(1, out.errors.size)
        assertTrue(
            "the reason must be words, not the bare Java message (which is the path again)",
            out.errors[0].endsWith("already exists; pass --force to overwrite"),
        )
        assertEquals("old", Files.readAllBytes(dst).decodeToString())
        assertEquals("new", Files.readAllBytes(src).decodeToString())
    }

    @Test
    fun `move with force onto a non-empty directory reports a readable reason`() {
        val dir = Files.createTempDirectory("fastio-mv-nonempty")
        val src = Files.write(dir.resolve("a.txt"), "x".toByteArray())
        // What the handler resolves `mv --force a.txt dir` to: dir/a.txt.
        val target = Files.createDirectory(dir.resolve("dir"))
        val occupied = Files.createDirectory(target.resolve("a.txt"))
        Files.write(occupied.resolve("inner.txt"), "i".toByteArray())
        val out = MoveOutcome()

        moveTree(src, occupied, force = true, out = out)

        assertEquals(1, out.errors.size)
        assertTrue(
            "a rename cannot replace a non-empty directory; the reason must say so",
            out.errors[0].contains("non-empty directory"),
        )
        assertTrue(Files.exists(src))
    }
}

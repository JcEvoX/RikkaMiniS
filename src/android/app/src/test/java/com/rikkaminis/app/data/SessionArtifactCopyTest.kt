package com.rikkaminis.app.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.nio.file.Files

/**
 * JVM tests for the [sweep1014-B10] artifact-copy helpers:
 *  - [isCopyableArtifactEntry] — the runtime-file exclusion matrix (sockets /
 *    FIFOs / symlinks fall under "non-regular, non-directory"; Chromium
 *    `Singleton*` profile locks; `*.lock` / `*.sock`).
 *  - [copySessionDirTree] — recursive copy that preserves relative structure,
 *    skips excluded entries (including whole excluded subtrees), and reports
 *    the copied count literally.
 *
 * These are top-level internal functions in SessionForkManager.kt — kept out
 * of the class so they compile under the JVM without Android/Room classes.
 */
class SessionArtifactCopyTest {

    @get:Rule
    val tmp = TemporaryFolder()

    // ── isCopyableArtifactEntry matrix ──

    @Test
    fun regularFilesAndDirectoriesAreCopyable() {
        assertTrue(isCopyableArtifactEntry("a.txt", isRegularFile = true, isDirectory = false))
        assertTrue(isCopyableArtifactEntry("notes.md", isRegularFile = true, isDirectory = false))
        assertTrue(isCopyableArtifactEntry("Default", isRegularFile = false, isDirectory = true))
    }

    @Test
    fun nonRegularNonDirectoryEntriesAreExcluded() {
        // Unix sockets, FIFOs, devices and symlinks all land here from
        // BasicFileAttributes (isRegularFile=false, isDirectory=false).
        assertFalse(isCopyableArtifactEntry("offload.sock", isRegularFile = false, isDirectory = false))
        assertFalse(isCopyableArtifactEntry("fifo", isRegularFile = false, isDirectory = false))
    }

    @Test
    fun runtimeLockAndSocketNamesAreExcluded() {
        assertFalse(isCopyableArtifactEntry("x.lock", isRegularFile = true, isDirectory = false))
        assertFalse(isCopyableArtifactEntry("x.sock", isRegularFile = true, isDirectory = false))
    }

    @Test
    fun chromiumSingletonProfileEntriesAreExcluded() {
        assertFalse(isCopyableArtifactEntry("SingletonLock", isRegularFile = true, isDirectory = false))
        assertFalse(isCopyableArtifactEntry("SingletonSocket", isRegularFile = false, isDirectory = false))
        assertFalse(isCopyableArtifactEntry("SingletonCookie", isRegularFile = true, isDirectory = false))
        // A directory named SingletonLock (profile dir recreated) is excluded too.
        assertFalse(isCopyableArtifactEntry("SingletonLock", isRegularFile = false, isDirectory = true))
        // Ordinary names are untouched.
        assertTrue(isCopyableArtifactEntry("singletonfile", isRegularFile = true, isDirectory = false))
        assertTrue(isCopyableArtifactEntry("blocker.locker", isRegularFile = true, isDirectory = false))
    }

    // ── copySessionDirTree end-to-end ──

    @Test
    fun copiesPreservingRelativeStructure() {
        val src = tmp.newFolder("workspace")
        File(src, "a.txt").writeText("a")
        File(src, "sub").mkdir()
        File(src, "sub/b.txt").writeText("b")
        val dst = File(tmp.newFolder("out"), "workspace")

        val (copied, failures) = copySessionDirTree(src, dst)

        assertEquals(2, copied)
        assertTrue(failures.isEmpty())
        assertEquals("a", File(dst, "a.txt").readText())
        assertEquals("b", File(dst, "sub/b.txt").readText())
    }

    @Test
    fun excludesRuntimeFilesAndWholeSingletonSubtrees() {
        val src = tmp.newFolder("browser")
        File(src, "Cookies").writeText("keep")
        File(src, "singleton_lock").writeText("keep") // lowercase — not excluded
        File(src, "SingletonLock").writeText("drop") // runtime lock file
        val singletonDir = File(src, "SingletonSocket")
        singletonDir.mkdir()
        File(singletonDir, "inner.txt").writeText("drop") // excluded subtree child
        val lockDir = File(src, "locks")
        lockDir.mkdir()
        File(lockDir, "profile.lock").writeText("drop") // .lock inside a kept dir
        val dst = File(tmp.newFolder("out"), "browser")

        val (copied, failures) = copySessionDirTree(src, dst)

        // "Cookies" + "singleton_lock" + the kept "locks" directory = 2 files.
        assertEquals(2, copied)
        assertTrue(failures.isEmpty())
        assertTrue(File(dst, "Cookies").exists())
        assertTrue(File(dst, "singleton_lock").exists())
        assertFalse(File(dst, "SingletonLock").exists())
        assertFalse(File(dst, "SingletonSocket").exists())
        assertFalse(File(dst, "SingletonSocket/inner.txt").exists())
        // locks/ dir itself is copied, its .lock child is not.
        assertTrue(File(dst, "locks").isDirectory)
        assertFalse(File(dst, "locks/profile.lock").exists())
    }

    @Test
    fun missingSourceDirYieldsEmptyResult() {
        val src = File(tmp.newFolder("nope"), "missing")
        val dst = File(tmp.newFolder("out"), "workspace")

        val (copied, failures) = copySessionDirTree(src, dst)

        assertEquals(0, copied)
        assertTrue(failures.isEmpty())
        assertFalse(dst.exists())
    }

    @Test
    fun emptySourceDirCreatesEmptyDestination() {
        val src = tmp.newFolder("workspace")
        val dst = File(tmp.newFolder("out"), "workspace")

        val (copied, failures) = copySessionDirTree(src, dst)

        assertEquals(0, copied)
        assertTrue(failures.isEmpty())
        assertTrue(dst.isDirectory)
    }

    @Test
    fun reCopyOverwritesExistingFiles() {
        val src = tmp.newFolder("workspace")
        File(src, "a.txt").writeText("new")
        val dst = File(tmp.newFolder("out"), "workspace")
        dst.mkdirs()
        File(dst, "a.txt").writeText("old")

        val (copied, failures) = copySessionDirTree(src, dst)

        assertEquals(1, copied)
        assertTrue(failures.isEmpty())
        assertEquals("new", File(dst, "a.txt").readText())
    }

    @Test
    fun artifactSubdirsMatchSandboxBindList() {
        // The four subdirs must stay in sync with what the sandbox binds
        // per session (ExecutionCoordinator / TerminalSession buildTermuxArgs).
        assertEquals(
            listOf("attachments", "offloads", "workspace", "browser"),
            SESSION_ARTIFACT_SUBDIRS,
        )
        // And the class-level const must be the same list (no drift).
        assertEquals(SessionForkManager.ARTIFACT_SUBDIRS, SESSION_ARTIFACT_SUBDIRS)
    }

    @Test
    fun symlinkedFileInsideTreeIsNotFollowed() {
        val src = tmp.newFolder("workspace")
        val target = tmp.newFile("outside.txt")
        target.writeText("secret")
        Files.createSymbolicLink(File(src, "link.txt").toPath(), target.toPath())
        val dst = File(tmp.newFolder("out"), "workspace")

        val (copied, failures) = copySessionDirTree(src, dst)

        assertEquals(0, copied)
        assertTrue(failures.isEmpty())
        assertFalse(File(dst, "link.txt").exists())
    }
}

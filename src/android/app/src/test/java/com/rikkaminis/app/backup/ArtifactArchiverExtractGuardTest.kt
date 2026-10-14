package com.rikkaminis.app.backup

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * [fix-R12 D8] Canonical prefix-boundary guard on extractZipByPrefix: the old
 * `target.canonicalPath.startsWith(destDir.canonicalPath)` (no File.separator)
 * let a sibling directory whose name shares the prefix — "shared_evil" against
 * a "shared" check, reachable through a symlink named `sub` — write through
 * the guard.
 */
class ArtifactArchiverExtractGuardTest {

    @Test
    fun isUnderBase_boundaryTable() {
        assertTrue(ArtifactArchiver.isUnderBase("/a/shared", "/a/shared"))
        assertTrue(ArtifactArchiver.isUnderBase("/a/shared", "/a/shared/x/y.txt"))
        assertFalse(ArtifactArchiver.isUnderBase("/a/shared", "/a/shared_evil/x"))
        assertFalse(ArtifactArchiver.isUnderBase("/a/shared", "/a/sharedX"))
        assertFalse(ArtifactArchiver.isUnderBase("/a/shared", "/b/shared/x"))
    }

    @Test
    fun extract_rejectsSymlinkEscapingToPrefixSibling() {
        val tmp = File(System.getProperty("java.io.tmpdir"), "archguard-${System.nanoTime()}")
        tmp.mkdirs()
        try {
            val root = File(tmp, "root").apply { mkdirs() }
            val shared = File(root, "shared").apply { mkdirs() }
            val evil = File(root, "shared_evil").apply { mkdirs() }
            File(evil, "pwned.txt").writeText("x")

            val zip = File(tmp, "in.zip")
            ZipOutputStream(FileOutputStream(zip)).use { zos ->
                val ok = File(shared, "ok.txt")
                ok.writeText("ok")
                zos.putNextEntry(ZipEntry("shared/ok.txt"))
                zos.write(ok.readBytes())
                zos.closeEntry()
                // Symlink inside shared/ pointing at the prefix-sibling dir.
                val sub = File(shared, "sub")
                try {
                    Files.createSymbolicLink(sub.toPath(), evil.absoluteFile.toPath())
                } catch (e: UnsupportedOperationException) {
                    assumeTrue("filesystem without symlink support", false)
                    return
                }
                zos.putNextEntry(ZipEntry("shared/sub/pwned.txt"))
                zos.write("pwn".toByteArray())
                zos.closeEntry()
            }

            val out = File(tmp, "out").apply { mkdirs() }
            val count = ArtifactArchiver.extractZipByPrefix(zip, mapOf("shared" to shared))
            // Only the legit entry is extracted; the symlink escape is rejected.
            assertEquals(1, count)
            assertEquals("x", File(evil, "pwned.txt").readText())
            assertTrue(File(shared, "ok.txt").exists())
            assertFalse(File(out, "pwned.txt").exists())
        } finally {
            tmp.deleteRecursively()
        }
    }
}

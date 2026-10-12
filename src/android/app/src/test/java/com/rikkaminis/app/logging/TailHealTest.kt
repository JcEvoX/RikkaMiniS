package com.rikkaminis.app.logging

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [T-log-tail-heal-1012] Regression anchors for torn-tail healing.
 *
 * Measured 2026-10-12 (main @ 8637ea61): a kill inside a buffered write
 * leaves a trailing fragment without a newline; the next process's first
 * append glued onto it — 26 torn lines in minis-2026-10-12.log / 78 in the
 * worker file, every second segment being the next process's
 * "Logging session started" line.
 */
class TailHealTest {

    @Test fun `torn tail gets a newline and a marker`() {
        val dir = createTempDir("healtest")
        try {
            val f = File(dir, "minis-2026-10-12.log").apply { writeText("[00:07:22.446] [STDOUT] tick=570 sinceH") }
            assertTrue(healTornTail(f, "MARKER"))
            val text = f.readText()
            assertTrue(text.endsWith("sinceH\nMARKER\n"))
            // The torn fragment itself must survive (append, not truncate).
            assertTrue(text.contains("tick=570 sinceH\n"))
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test fun `clean tail ending with newline is untouched`() {
        val dir = createTempDir("healtest")
        try {
            val f = File(dir, "minis-2026-10-12.log").apply { writeText("line one\nline two\n") }
            assertFalse(healTornTail(f, "MARKER"))
            assertEquals("line one\nline two\n", f.readText())
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test fun `empty or missing file is a no-op`() {
        val dir = createTempDir("healtest")
        try {
            val empty = File(dir, "empty.log").apply { writeText("") }
            assertFalse(healTornTail(empty, "MARKER"))
            assertFalse(healTornTail(File(dir, "missing.log"), "MARKER"))
            assertEquals("", empty.readText())
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test fun `healed file accepts further appends on their own lines`() {
        val dir = createTempDir("healtest")
        try {
            val f = File(dir, "minis-2026-10-12.log").apply { writeText("torn-fragment") }
            healTornTail(f, "MARKER")
            f.appendText("[00:13:43.890] [INFO] session started\n")
            val lines = f.readText().lines().filter { it.isNotBlank() }
            assertEquals(listOf("torn-fragment", "MARKER", "[00:13:43.890] [INFO] session started"), lines)
        } finally {
            dir.deleteRecursively()
        }
    }
}

package com.rikkaminis.app.logging

import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File

/**
 * [T-log-rotation-1012] Regression anchors for the debug-roll fix.
 *
 * Measured on 2026-10-12 (main @ 8637ea61): the debug file's byte counter was
 * per-process state while the FILE was shared across ~720 short-lived
 * `:modelservice` worker processes — no single process ever reached
 * DEBUG_MAX_BYTES, so the file grew to 202 MB before the first rotation, and
 * the fixed `.1.log` roll name made the NEXT roll delete the PREVIOUS roll
 * (a 211 MB evidence file survived ~7 minutes).
 */
class DebugRollNamingTest {

    private val base = "debug-2026-10-12.modelservice.log"

    // ---- debugRollFileName -------------------------------------------------

    @Test fun `first roll appends the index`() {
        assertEquals("debug-2026-10-12.modelservice.1.log", debugRollFileName(base, 1))
    }

    @Test fun `second roll SHIFTS the index instead of reusing dot-one`() {
        val prev = debugRollFileName(base, 1)
        // The old scheme produced the same `.1.log` name for every roll —
        // this is the assertion that fails on the pre-fix naming.
        assertEquals("debug-2026-10-12.modelservice.2.log", debugRollFileName(prev, 2))
    }

    @Test fun `third roll shifts again`() {
        val prev = debugRollFileName(debugRollFileName(base, 1), 2)
        assertEquals("debug-2026-10-12.modelservice.3.log", debugRollFileName(prev, 3))
    }

    @Test fun `main-process name (no process suffix) rolls the same way`() {
        val mainBase = "debug-2026-10-12.log"
        assertEquals("debug-2026-10-12.1.log", debugRollFileName(mainBase, 1))
        assertEquals("debug-2026-10-12.2.log", debugRollFileName("debug-2026-10-12.1.log", 2))
    }

    // ---- highestRollIndex --------------------------------------------------

    @Test fun `highest roll index finds existing rolls among siblings`() {
        val names = listOf(
            "debug-2026-10-12.modelservice.log",
            "debug-2026-10-12.modelservice.1.log",
            "debug-2026-10-12.modelservice.2.log",
            "minis-2026-10-12.modelservice.log", // other channel, ignored
            "debug-2026-10-11.modelservice.5.log", // other day, ignored
            "error-snapshot-2026-10-12-101010-123.log", // evidence, ignored
        )
        assertEquals(2, highestRollIndex(names, base))
    }

    @Test fun `highest roll index is zero when no rolls exist`() {
        val names = listOf(base, "minis-2026-10-12.modelservice.log")
        assertEquals(0, highestRollIndex(names, base))
    }

    @Test fun `non-integer middle segments never match`() {
        // A file like `debug-<date>.modelservice.old.log` must not be picked
        // up as roll "old" (toIntOrNull guards the sweep).
        val names = listOf(base, "debug-2026-10-12.modelservice.old.log")
        assertEquals(0, highestRollIndex(names, base))
    }

    // ---- rotation semantics against real files (tmp dir) --------------------

    @Test fun `rotation preserves previous roll bytes`() {
        val dir = createTempDir("rolltest")
        try {
            val live = File(dir, base).apply { writeText("LIVE-OLD-BYTES") }
            val priorRoll = File(dir, debugRollFileName(base, 1)).apply { writeText("PRIOR-ROLL-BYTES") }

            // Simulate the post-fix rotate: shift to index 2, keep index 1.
            val highest = highestRollIndex(dir.list()?.toList() ?: emptyList(), base)
            live.renameTo(File(dir, debugRollFileName(base, highest + 1)))

            assertEquals("PRIOR-ROLL-BYTES", priorRoll.readText())
            assertEquals("PRIOR-ROLL-BYTES", File(dir, debugRollFileName(base, 1)).readText())
            assertEquals("LIVE-OLD-BYTES", File(dir, debugRollFileName(base, 2)).readText())
        } finally {
            dir.deleteRecursively()
        }
    }
}

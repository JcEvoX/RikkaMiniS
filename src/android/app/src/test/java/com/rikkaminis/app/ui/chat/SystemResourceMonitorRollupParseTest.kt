package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [T-android-resource-hud-offmain] Ported from upstream's
 * MainThreadHotspotsTest (perf source): the three pure-parse tests for
 * [SystemResourceMonitor.parseTotalPssKb] — the smaps_rollup reader that
 * replaced the full-smaps `Debug.getMemoryInfo()` walk on the main thread.
 *
 * Expectations are hard-coded literals (not derived from the code under
 * test) so a mutation of the parser can't disguise itself as a pass.
 */
class SystemResourceMonitorRollupParseTest {

    private val rollup = """
        00400000-7fffffff ---p 00000000 00:00 0                          [rollup]
        Rss:              412340 kB
        Pss:              301224 kB
        Pss_Anon:         210000 kB
        Pss_File:          80000 kB
        Pss_Shmem:         11224 kB
        Shared_Clean:      90000 kB
        Private_Dirty:    200000 kB
        Swap:              50000 kB
        SwapPss:           40100 kB
        Locked:                0 kB
    """.trimIndent()

    @Test
    fun `total PSS is Pss plus SwapPss, like Debug MemoryInfo getTotalPss`() {
        assertEquals(341324L, SystemResourceMonitor.parseTotalPssKb(rollup))
    }

    @Test
    fun `sub-fields like Pss_Anon are not mistaken for the total`() {
        val onlySubfields = "Pss_Anon: 5 kB\nPss_File: 6 kB\n"
        assertNull(SystemResourceMonitor.parseTotalPssKb(onlySubfields))
    }

    @Test
    fun `no swap line means no swap`() {
        assertEquals(1000L, SystemResourceMonitor.parseTotalPssKb("Rss: 2000 kB\nPss: 1000 kB\n"))
    }
}

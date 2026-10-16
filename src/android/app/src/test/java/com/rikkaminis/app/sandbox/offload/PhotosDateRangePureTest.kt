package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.util.TimeZone

/**
 * [deep-audit-1016c FIX-B C] Pure date-alignment helpers for
 * `android-photos` date ranges — compiled from the VERBATIM top-level source
 * of PhotosOffloadHandler.kt via probe/jvm/extract_pure.py, so the code under
 * test IS the shipped code, not a hand copy that can drift.
 *
 * Every expected value below is a written literal computed from an
 * independent implementation (python zoneinfo ground truth, see
 * probe/pre-p3-expected.txt), never derived from the functions under test.
 *
 * The two bugs this pins:
 *  1. date-only `--end` parsed to 00:00:00.000 ⇒ zero-width ranges;
 *  2. `--days N` computed as `now - N * 24h` ⇒ hour of drift across a DST
 *     transition can truncate onto the wrong local day.
 */
class PhotosDateRangePureTest {

    private val ny = TimeZone.getTimeZone("America/New_York")
    private val utc = TimeZone.getTimeZone("UTC")

    // ── isDateOnly ────────────────────────────────────────────────────────

    @Test
    fun `date-only shape is recognized`() {
        assertTrue(isDateOnly("2026-10-16"))
        assertTrue(isDateOnly("  2026-10-16  ")) // surrounding whitespace tolerated
    }

    @Test
    fun `anything with a time part or garbage is not date-only`() {
        assertFalse(isDateOnly("2026-10-16T23:59:59"))
        assertFalse(isDateOnly("2026-10-16T10:00"))
        assertFalse(isDateOnly("2026-10-16 10:00:00"))
        assertFalse(isDateOnly("2026-10-1"))
        assertFalse(isDateOnly("x2026-10-16"))
        assertFalse(isDateOnly(""))
    }

    // ── alignDateOnlyEndMs ────────────────────────────────────────────────

    @Test
    fun `normal day in New York aligns to 23_59_59_999`() {
        // ground truth (python zoneinfo): 2026-10-16T00:00:00-04:00 == 1792123200000
        val midnight = 1792123200000L
        // ground truth: 2026-10-16T23:59:59.999-04:00 == 1792209599999
        assertEquals(1792209599999L, alignDateOnlyEndMs(midnight, ny))
    }

    @Test
    fun `utc day aligns across the same instant ladder`() {
        // ground truth: 2026-10-16T00:00:00Z == 1792108800000; end == 1792195199999
        assertEquals(1792195199999L, alignDateOnlyEndMs(1792108800000L, utc))
    }

    @Test
    fun `spring-forward day keeps its full 25h wall clock`() {
        // 2026-03-08 is the 2026 spring-forward day in America/New_York:
        // midnight is EST (-05:00), 23:59:59.999 is EDT (-04:00).
        // ground truth: midnight == 1772946000000; end == 1773028799999
        assertEquals(1773028799999L, alignDateOnlyEndMs(1772946000000L, ny))
        // The naive `+24h` alignment would have produced 1773032399999 —
        // 00:59:59.999 of the NEXT local day (the day is only 23h long) —
        // the literal above is exactly 3600000 lower.
        assertEquals(3600000L, 1773032399999L - alignDateOnlyEndMs(1772946000000L, ny))
    }

    @Test
    fun `fall-back day keeps its full 23h wall clock`() {
        // 2026-11-01 is the 2026 fall-back day in America/New_York.
        // ground truth: midnight (EDT -04:00) == 1793505600000; end (EST -05:00) == 1793595599999
        assertEquals(1793595599999L, alignDateOnlyEndMs(1793505600000L, ny))
    }

    @Test
    fun `midday instant lands on its own day's end`() {
        // 2026-10-16T12:00:00-04:00 == 1792166400000 → same day's 23:59:59.999
        assertEquals(1792209599999L, alignDateOnlyEndMs(1792166400000L, ny))
    }

    // ── daysAgoStartOfDayMs ───────────────────────────────────────────────

    @Test
    fun `one day back crosses the DST boundary by wall clock not arithmetic`() {
        // now = 2026-03-09T00:15:00-04:00 == 1773029700000 (the day AFTER
        // spring forward). The PREVIOUS LOCAL DAY's midnight is
        // 2026-03-08T00:00:00-05:00 == 1772946000000.
        assertEquals(1772946000000L, daysAgoStartOfDayMs(1773029700000L, 1, ny))
        // The buggy form (now - 24h = 2026-03-07T23:15 EST, truncated to
        // 2026-03-07T00:00 EST == 1772859600000) would have skipped a day.
    }

    @Test
    fun `seven days back in stable fall time`() {
        // now = 2026-10-16T12:00:00-04:00 == 1792166400000;
        // 7 local days earlier, local midnight: 2026-10-09T00:00:00-04:00 == 1791518400000
        assertEquals(1791518400000L, daysAgoStartOfDayMs(1792166400000L, 7, ny))
    }

    @Test
    fun `zero days back is today's local midnight`() {
        // 2026-10-16T12:00:00-04:00 → 2026-10-16T00:00:00-04:00 == 1792123200000
        assertEquals(1792123200000L, daysAgoStartOfDayMs(1792166400000L, 0, ny))
    }

    @Test
    fun `utc never drifts`() {
        // 2026-10-16T12:00:00Z == 1792152000000 minus 3 days → 2026-10-13T00:00:00Z == 1791849600000
        assertEquals(1791849600000L, daysAgoStartOfDayMs(1792152000000L, 3, utc))
    }

    companion object {
        // Silence "unused" for the fixture tree anchor used by the runner script.
        @JvmStatic fun probeSource(): File = File(".")
    }
}

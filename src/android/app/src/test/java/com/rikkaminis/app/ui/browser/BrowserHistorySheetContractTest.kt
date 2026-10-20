package com.rikkaminis.app.ui.browser

import com.rikkaminis.app.browser.BrowserHistoryStore
import com.rikkaminis.app.browser.browserHistoryDayLabel
import com.rikkaminis.app.browser.historySnapshotKey
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test
import java.util.Calendar

/**
 * [deep-audit-v14 ISSUE-08] JVM contract test for the browser-history sheet.
 *
 * The two decisions under test are pure (`java.util.Calendar` + arithmetic, no
 * Context, no Compose), so unlike the sheet's Compose wiring they can be run
 * for real here instead of asserted on source text. Both expectations are
 * written as literals on purpose — deriving them from the production constants
 * would let a mutation masquerade as a pass.
 *
 * 1. the day-bucket labels come from the caller (they used to be the English
 *    literals "Today" / "Yesterday" baked into the store, which bypassed all 7
 *    shipped locales) — sentinel labels prove the value is returned verbatim;
 * 2. the snapshot key the sheet remembers on must tell an equal-count
 *    replacement apart, because a `record()` that also prunes one entry out of
 *    the 7-day window leaves `size` unchanged.
 *
 * The Compose-side wiring (the sheet reads the store once per refresh tick and
 * passes `stringResource(...)` labels) is graded by
 * `scripts/scan/browser_history_snapshot_check.py` arm B1/B2, which runs on the
 * frozen CI gate set.
 */
class BrowserHistorySheetContractTest {

    /** Local-time wall clock, so the day comparison is timezone-independent. */
    private fun at(year: Int, month: Int, day: Int, hour: Int = 12, minute: Int = 0): Long =
        Calendar.getInstance().apply {
            set(year, month - 1, day, hour, minute, 0)
            set(Calendar.MILLISECOND, 0)
        }.timeInMillis

    private fun entry(id: String, timestamp: Long) = BrowserHistoryStore.Entry(
        id = id,
        url = "https://example.com/$id",
        title = id,
        timestamp = timestamp,
    )

    // ── day buckets ──────────────────────────────────────────────────────────

    @Test
    fun `buckets map today yesterday and older to the caller's labels`() {
        val now = at(2026, 3, 15, 10, 0)

        assertEquals("Heute", browserHistoryDayLabel(at(2026, 3, 15, 0, 1), now, "Heute", "Gestern"))
        assertEquals("Gestern", browserHistoryDayLabel(at(2026, 3, 14, 23, 59), now, "Heute", "Gestern"))
        assertEquals("3/13", browserHistoryDayLabel(at(2026, 3, 13, 12, 0), now, "Heute", "Gestern"))
    }

    @Test
    fun `a night-time entry belongs to the calendar day it happened on`() {
        val justAfterMidnight = at(2026, 3, 15, 0, 5)

        assertEquals("T", browserHistoryDayLabel(at(2026, 3, 15, 23, 55), justAfterMidnight, "T", "Y"))
        assertEquals("Y", browserHistoryDayLabel(at(2026, 3, 14, 23, 59), justAfterMidnight, "T", "Y"))
    }

    @Test
    fun `year and month boundaries roll over correctly`() {
        val newYear = at(2026, 1, 1, 0, 30)

        assertEquals("Y", browserHistoryDayLabel(at(2025, 12, 31, 23, 0), newYear, "T", "Y"))
        assertEquals("12/30", browserHistoryDayLabel(at(2025, 12, 30, 12, 0), newYear, "T", "Y"))
    }

    @Test
    fun `labels are the caller's strings, not English defaults`() {
        val now = at(2026, 6, 1, 8, 0)

        assertEquals("今天", browserHistoryDayLabel(now, now, "今天", "昨天"))
        assertEquals("昨天", browserHistoryDayLabel(at(2026, 5, 31, 8, 0), now, "今天", "昨天"))
        assertNotEquals("Today", browserHistoryDayLabel(now, now, "今天", "昨天"))
    }

    // ── snapshot key ─────────────────────────────────────────────────────────

    @Test
    fun `an equal-count replacement changes the snapshot key`() {
        val older = at(2026, 3, 14, 9, 0)
        val newer = at(2026, 3, 15, 9, 0)

        val before = listOf(entry("a", newer), entry("b", older))
        val after = listOf(entry("c", newer), entry("d", older))

        assertEquals(2, before.size)
        assertEquals(2, after.size)
        assertNotEquals(historySnapshotKey(before), historySnapshotKey(after))
    }

    @Test
    fun `the snapshot key is stable while the content is`() {
        val stamp = at(2026, 3, 15, 9, 0)

        assertEquals(historySnapshotKey(listOf(entry("a", stamp))), historySnapshotKey(listOf(entry("a", stamp))))
        assertEquals(1 to "a", historySnapshotKey(listOf(entry("a", stamp))))
        assertEquals(0 to null, historySnapshotKey(emptyList()))
    }

    @Test
    fun `appending a newer entry changes the snapshot key`() {
        val stamp = at(2026, 3, 15, 9, 0)
        val two = listOf(entry("b", stamp), entry("a", stamp - 60_000))
        val three = listOf(entry("c", stamp + 60_000)) + two

        assertNotEquals(historySnapshotKey(two), historySnapshotKey(three))
    }
}

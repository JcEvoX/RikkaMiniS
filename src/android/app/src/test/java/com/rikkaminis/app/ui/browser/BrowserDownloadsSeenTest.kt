package com.rikkaminis.app.ui.browser

import com.rikkaminis.app.browser.BrowserTabPool
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1036 ISSUE-07 / dv15-B3] The downloads panel's "mark as read"
 * decision, run for real.
 *
 * Defect shape being pinned: `LaunchedEffect(Unit) { tabPool.markDownloadsSeen() }`
 * made exactly one pass, at the instant the sheet entered composition, and
 * never again. A download that finished while the panel was open kept
 * `seen = false`, so the toolbar badge lit up behind the panel the user was
 * already looking at, and stayed lit after closing it.
 *
 * The fix keys the effect on the observed list instead
 * (`LaunchedEffect(needsSeenPass)`), so the pass re-runs when — and only when —
 * a terminal entry is still unseen. That decision is `pendingDownloadSeenPass`,
 * a pure function, which is what this class exercises; the gate
 * `scripts/scan/downloads_atomic_update_check.py` is the structural half.
 *
 * The entries are the real [BrowserTabPool.DownloadEntry] values the store
 * publishes, so a change to the model's terminal states shows up here.
 */
class BrowserDownloadsSeenTest {

    private fun entry(
        id: Long,
        state: BrowserTabPool.DownloadState,
        seen: Boolean,
    ) = BrowserTabPool.DownloadEntry(
        id = id,
        filename = "f$id.bin",
        destination = null,
        bytesDone = 0L,
        totalBytes = 0L,
        state = state,
        startedAt = 0L,
        seen = seen,
    )

    /**
     * Open time: the entries the panel is about to show are terminal and
     * unseen, so one pass is owed.
     */
    @Test
    fun `opening the panel owes a pass for the terminal entries`() {
        val rows = listOf(
            entry(1, BrowserTabPool.DownloadState.COMPLETED, seen = false),
            entry(2, BrowserTabPool.DownloadState.FAILED, seen = false),
        )

        assertTrue(pendingDownloadSeenPass(rows))
    }

    /**
     * The regression itself: the panel had already marked everything it saw at
     * open time, then a download settles while it is still open. That new
     * entry re-arms the pass — this is the case the one-shot effect missed.
     */
    @Test
    fun `a completion arriving while the panel is open owes a new pass`() {
        val rows = listOf(
            entry(1, BrowserTabPool.DownloadState.COMPLETED, seen = true),
            entry(2, BrowserTabPool.DownloadState.COMPLETED, seen = false),
        )

        assertTrue(pendingDownloadSeenPass(rows))
    }

    /** A failure list is terminal too — the badge counts it the same way. */
    @Test
    fun `a failure arriving while the panel is open owes a new pass`() {
        val rows = listOf(
            entry(1, BrowserTabPool.DownloadState.COMPLETED, seen = true),
            entry(2, BrowserTabPool.DownloadState.FAILED, seen = false),
        )

        assertTrue(pendingDownloadSeenPass(rows))
    }

    /** Nothing terminal is unseen ⇒ no pass, so the effect settles instead of looping. */
    @Test
    fun `nothing is owed once every terminal entry is seen`() {
        val rows = listOf(
            entry(1, BrowserTabPool.DownloadState.COMPLETED, seen = true),
            entry(2, BrowserTabPool.DownloadState.FAILED, seen = true),
        )

        assertFalse(pendingDownloadSeenPass(rows))
    }

    /**
     * In-flight entries are not terminal: `markDownloadsSeen()` cannot change
     * them, so a progress tick must not re-arm the pass (that would re-run it
     * on every byte).
     */
    @Test
    fun `in-flight progress alone owes nothing`() {
        assertFalse(pendingDownloadSeenPass(listOf(
            entry(1, BrowserTabPool.DownloadState.DOWNLOADING, seen = false),
        )))
        assertFalse(pendingDownloadSeenPass(listOf(
            entry(1, BrowserTabPool.DownloadState.DOWNLOADING, seen = false),
            entry(2, BrowserTabPool.DownloadState.DOWNLOADING, seen = false),
        )))
    }

    @Test
    fun `an empty list owes nothing`() {
        assertFalse(pendingDownloadSeenPass(emptyList()))
    }

    /**
     * Mixed panel — the in-flight row must not mask the settled one behind it.
     */
    @Test
    fun `an in-flight row does not hide an unseen terminal row`() {
        val rows = listOf(
            entry(1, BrowserTabPool.DownloadState.DOWNLOADING, seen = false),
            entry(2, BrowserTabPool.DownloadState.COMPLETED, seen = true),
            entry(3, BrowserTabPool.DownloadState.COMPLETED, seen = false),
        )

        assertTrue(pendingDownloadSeenPass(rows))
    }
}

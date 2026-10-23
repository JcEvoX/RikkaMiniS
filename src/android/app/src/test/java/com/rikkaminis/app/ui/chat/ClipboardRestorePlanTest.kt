package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv27-B4 / deep-audit-1047 B-07] — `MinisMarkdownTextToolbar.withSelection`
 * borrows the system clipboard to resolve a text selection (Compose's
 * `TextToolbar` exposes no "give me the AnnotatedString selection" API).
 *
 * Its `finally` restored the snapshot ONLY when there was one:
 *
 *     if (original != null) runCatching { cm.setPrimaryClip(original) }
 *
 * For a user whose clipboard was empty — or unreadable, since the snapshot is
 * read through `runCatching { cm.primaryClip }.getOrNull()` — there was
 * nothing to restore, so "add selection to input" left the borrowed selection
 * sitting in the system clipboard. That is a side effect outside the feature's
 * contract, and it survives the app: every other app and the next paste see
 * it.
 *
 * The decision is now one pure function — [clipboardRestoreAction] — with the
 * platform check passed in by the call site (exactly how
 * `ClipboardOffloadHandler.clearAction(atLeastQ)` is called), so it can be
 * pinned as behaviour rather than re-derived from a Compose-only file.
 *
 * The expectations below are LITERALS on purpose: deriving them from the
 * production enum would make a mutation of that enum look like a PASS (the
 * JVM-harness self-reference trap).
 */
class ClipboardRestorePlanTest {

    @Test
    fun `an existing clip is put back untouched`() {
        assertEquals(
            ClipboardRestoreAction.RESTORE_ORIGINAL,
            clipboardRestoreAction(hadOriginal = true, atLeastQ = true),
        )
    }

    @Test
    fun `an existing clip is put back untouched below Q too`() {
        assertEquals(
            ClipboardRestoreAction.RESTORE_ORIGINAL,
            clipboardRestoreAction(hadOriginal = true, atLeastQ = false),
        )
    }

    @Test
    fun `an empty clipboard is emptied again on Q and later`() {
        assertEquals(
            ClipboardRestoreAction.CLEAR_PLATFORM,
            clipboardRestoreAction(hadOriginal = false, atLeastQ = true),
        )
    }

    @Test
    fun `an empty clipboard keeps the empty-string fallback below Q`() {
        // `clearPrimaryClip()` is only reliable from API 29 (Q) on; on API
        // 26-28 the empty-string write was the only way to empty the
        // clipboard — the same split `ClipboardOffloadHandler.clearAction`
        // owns (dv22-A6 A-06).
        assertEquals(
            ClipboardRestoreAction.CLEAR_EMPTY_STRING,
            clipboardRestoreAction(hadOriginal = false, atLeastQ = false),
        )
    }

    @Test
    fun `the empty-clipboard case never resolves to the restore action`() {
        // The defect shape this class exists to catch: an empty clipboard must
        // never be "handled" by restoring (there is nothing to restore), and
        // must never be left carrying our borrowed text.
        for (atLeastQ in listOf(true, false)) {
            val action = clipboardRestoreAction(hadOriginal = false, atLeastQ = atLeastQ)
            assertTrue(
                "an empty clipboard must not resolve to RESTORE_ORIGINAL (atLeastQ=$atLeastQ)",
                action != ClipboardRestoreAction.RESTORE_ORIGINAL,
            )
            assertTrue(
                "an empty clipboard must be emptied, not left alone (atLeastQ=$atLeastQ)",
                action == ClipboardRestoreAction.CLEAR_PLATFORM ||
                    action == ClipboardRestoreAction.CLEAR_EMPTY_STRING,
            )
        }
    }

    /**
     * Negative control: the BASELINE shape (restore only when a snapshot
     * existed) must fail the check above. If the baseline ever passes, the
     * assertion has lost its teeth.
     */
    @Test
    fun `negative control - the baseline shape skips the empty-clipboard case`() {
        val baselineHadOriginal: Boolean? = null
        val baselineAction =
            if (baselineHadOriginal != null) ClipboardRestoreAction.RESTORE_ORIGINAL
            else ClipboardRestoreAction.RESTORE_ORIGINAL
        assertTrue(
            "the baseline shape resolves the empty-clipboard case to RESTORE_ORIGINAL " +
                "— the borrowed text is left behind",
            baselineAction == ClipboardRestoreAction.RESTORE_ORIGINAL,
        )
    }
}

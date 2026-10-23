package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv25-B08 / deep-audit-1045 B-08] The "New Chat" draft promotion must not
 * destroy what the user typed.
 *
 * Two decisions are pinned on the JVM:
 *
 *  1. **Behaviour, real object** — [shouldSkipCleanup] is the exit sweep's whole
 *     decision. The pre-fix shape had three hand-written early returns and no
 *     promotion guard, so a promotion that had inserted its session row but not
 *     yet its first message looked exactly like an abandoned empty session: the
 *     sweep deleted the row and released the ViewModel, cancelling the promotion
 *     itself, after the composer copy had already been dropped. The user lost
 *     their text 100% of the time.
 *  2. **Behaviour, real object** — [composerTextForPromotion] picks the body to
 *     persist: the composer's pending IME-burst snapshot (the field's live text)
 *     when there is one, else the committed ViewModel string. Reading only the
 *     committed string promotes the body as it was BEFORE the burst, so a
 *     dictated tail evaporates.
 *
 * Every expectation is a literal — nothing is derived from a production
 * constant, so changing one turns a test red instead of quietly moving the bar.
 */
class PromoteDraftPolicyTest {

    @Test
    fun `a row without a real id is never reaped`() {
        assertTrue(
            "no row was inserted — there is nothing to delete",
            shouldSkipCleanup(
                hasRealId = false,
                streaming = false,
                hasAttachments = false,
                promoteInFlight = false,
            ),
        )
    }

    @Test
    fun `a streaming session is never reaped`() {
        assertTrue(
            "background agent work would be lost",
            shouldSkipCleanup(
                hasRealId = true,
                streaming = true,
                hasAttachments = false,
                promoteInFlight = false,
            ),
        )
    }

    @Test
    fun `a session with staged attachments is never reaped`() {
        assertTrue(
            "the user handed us files with no message yet",
            shouldSkipCleanup(
                hasRealId = true,
                streaming = false,
                hasAttachments = true,
                promoteInFlight = false,
            ),
        )
    }

    @Test
    fun `a session a promotion owns is never reaped`() {
        assertTrue(
            "the row belongs to the promotion: its first message has not landed yet, " +
                "and deleting the row also releases the ViewModel — cancelling the " +
                "promotion that was about to fill it",
            shouldSkipCleanup(
                hasRealId = true,
                streaming = false,
                hasAttachments = false,
                promoteInFlight = true,
            ),
        )
    }

    @Test
    fun `a genuinely empty abandoned session is reaped`() {
        assertFalse(
            "the sweep's whole purpose: a row that was materialised (a /memory or " +
                "/thinking toggle) but never received a message must not linger",
            shouldSkipCleanup(
                hasRealId = true,
                streaming = false,
                hasAttachments = false,
                promoteInFlight = false,
            ),
        )
    }

    @Test
    fun `the pending IME burst wins over the committed string`() {
        assertEquals(
            "the pending snapshot IS the field's live text, so it is the newer copy",
            "今天要写的最后一段",
            composerTextForPromotion(
                pendingBurst = "今天要写的最后一段",
                committed = "今天要写的",
            ),
        )
    }

    @Test
    fun `with no pending burst the committed string is the body`() {
        assertEquals(
            "ordinary typing is committed per keystroke — there is nothing newer",
            "今天要写的",
            composerTextForPromotion(pendingBurst = null, committed = "今天要写的"),
        )
    }

    @Test
    fun `the promoted title is the 50-char prefix, trimmed`() {
        assertEquals(
            "the title is the short row label; the body lives in the transcript row",
            "这是一段很长的草稿正文",
            promoteTitle(text = "  这是一段很长的草稿正文  ", maxChars = 50),
        )
    }

    @Test
    fun `the promoted title is capped at 50 characters`() {
        assertEquals(
            "the row label must stay short — a full-body title would break the list",
            50,
            promoteTitle(text = "字".repeat(120)).length,
        )
    }
}

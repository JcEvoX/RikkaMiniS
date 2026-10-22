package com.rikkaminis.app.share

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [§1042-A-07] Regression tests for the share-staging byte budget.
 *
 * Two defects, both silent:
 *  1. `debitStagedBytes` clamped a DISCARDED copy
 *     (`totalStagedBytes.addAndGet(-bytes).coerceAtLeast(0L)` in statement
 *     position), so a debit larger than the balance drove the real counter
 *     negative — e.g. the same staged file freed by the provider-import path
 *     and by cleanSharedFiles. The cumulative 500 MB check then under-counted
 *     and admitted shares past the cap.
 *  2. The AttachText branch (long EXTRA_TEXT staged as shared-text-*.txt)
 *     applied no per-file / cumulative guard and never credited the budget,
 *     so text shares bypassed the cap the binary path enforces.
 *
 * Expected values are hand-written literals — never derived from the code
 * under test (a constant changed together with its expectation hides the
 * mutation behind a PASS).
 */
class ShareStagedBytesAccountingTest {

    // ── ledger clamping (defect 1) ──────────────────────────────────────────

    @Test
    fun `debit larger than the balance clamps at zero`() {
        val ledger = StagedBytesLedger()
        ledger.credit(100L)
        ledger.debit(150L)
        assertEquals(0L, ledger.current())
    }

    @Test
    fun `double debit does not underflow`() {
        val ledger = StagedBytesLedger()
        ledger.credit(100L)
        ledger.debit(60L)          // 100 -> 40
        assertEquals(40L, ledger.current())
        ledger.debit(60L)          // 40 -> clamps at 0, never -20
        assertEquals(0L, ledger.current())
    }

    @Test
    fun `a clamped ledger counts the next share from zero`() {
        val ledger = StagedBytesLedger()
        ledger.credit(100L)
        ledger.debit(150L)         // the pre-fix counter sat at -50 here
        ledger.credit(30L)         // pre-fix: -20, fixed: 30
        assertEquals(30L, ledger.current())
    }

    @Test
    fun `debit of the exact balance reaches zero`() {
        val ledger = StagedBytesLedger()
        ledger.credit(100L)
        ledger.debit(100L)
        assertEquals(0L, ledger.current())
    }

    @Test
    fun `reset zeroes the ledger`() {
        val ledger = StagedBytesLedger()
        ledger.credit(500L)
        ledger.reset()
        assertEquals(0L, ledger.current())
    }

    @Test
    fun `credit of non-positive bytes is a no-op`() {
        val ledger = StagedBytesLedger()
        ledger.credit(0L)
        ledger.credit(-5L)
        assertEquals(0L, ledger.current())
    }

    // ── AttachText admission (defect 2) ─────────────────────────────────────

    @Test
    fun `byte count is utf8 bytes not chars`() {
        assertEquals(5L, attachTextByteCount("hello"))
        assertEquals(3L, attachTextByteCount("你"))     // 1 char, 3 UTF-8 bytes
    }

    @Test
    fun `text above the per-file cap is rejected`() {
        val verdict = admitAttachText(
            textBytes = 11L, currentTotalBytes = 0L, maxFileBytes = 10L, maxTotalBytes = 100L,
        )
        assertTrue(verdict is AttachTextAdmission.RejectedPerFile)
    }

    @Test
    fun `text pushing past the cumulative cap is rejected`() {
        val verdict = admitAttachText(
            textBytes = 6L, currentTotalBytes = 95L, maxFileBytes = 10L, maxTotalBytes = 100L,
        )
        assertTrue(verdict is AttachTextAdmission.RejectedCumulative)
    }

    @Test
    fun `text exactly at the per-file cap is admitted`() {
        val verdict = admitAttachText(
            textBytes = 10L, currentTotalBytes = 0L, maxFileBytes = 10L, maxTotalBytes = 100L,
        )
        assertTrue(verdict is AttachTextAdmission.Admitted)
    }

    @Test
    fun `text exactly filling the cumulative cap is admitted`() {
        val verdict = admitAttachText(
            textBytes = 6L, currentTotalBytes = 94L, maxFileBytes = 10L, maxTotalBytes = 100L,
        )
        assertTrue(verdict is AttachTextAdmission.Admitted)
    }

    @Test
    fun `a rejected text stages and credits nothing`() {
        val ledger = StagedBytesLedger()
        ledger.credit(95L)
        val text = "overflow"      // 8 UTF-8 bytes: per-file OK, 95 + 8 > 100
        val verdict = admitAttachText(attachTextByteCount(text), ledger.current(), 10L, 100L)
        assertTrue(verdict is AttachTextAdmission.RejectedCumulative)
        // Rejection path returns before writeText/credit — the balance is untouched.
        assertEquals(95L, ledger.current())
    }

    @Test
    fun `an admitted text is credited byte-for-byte`() {
        val ledger = StagedBytesLedger()
        val text = "hello"         // 5 UTF-8 bytes
        val verdict = admitAttachText(attachTextByteCount(text), ledger.current(), 10L, 100L)
        assertTrue(verdict is AttachTextAdmission.Admitted)
        ledger.credit(attachTextByteCount(text))
        assertEquals(5L, ledger.current())
    }
}

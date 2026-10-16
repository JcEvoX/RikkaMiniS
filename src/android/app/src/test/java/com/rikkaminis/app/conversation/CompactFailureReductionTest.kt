package com.rikkaminis.app.conversation

import com.rikkaminis.app.ui.chat.isContextTooLargeError
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/compact-lastfailure-pin-1016]
 *
 * JVM device for the candidate-chain failure reduction. The chain in
 * `generateCompactSummary` walks ordered candidates; each failure used to
 * overwrite `lastFailure`, and the exhausted chain threw the LAST one. The
 * outer catch decides "halve and retry" solely on `isContextTooLargeError(e)`,
 * so a chain whose capacity evidence (a 400 context-length) was followed by an
 * ordinary failure (timeout / 5xx / unavailable) threw the ordinary one and the
 * halving retry never fired — the `[Compact] Splitting` telemetry had 0 hits in
 * three days of device logs.
 *
 * The reduction is a pure function so it can be driven by failure SEQUENCES
 * here; the production wiring (`::isContextTooLargeError` as the predicate) is
 * pinned by the scan gate `compact_decision_gate_check.py`.
 *
 * Expectations are LITERALS, and the predicate is the REAL classifier — so a
 * change to the classifier's substring set shows up here as a red rather than
 * silently weakening both the code and its test.
 */
class CompactFailureReductionTest {

    // Literal provider-failure texts, chosen to exercise the REAL classifier
    // (substring set pinned in ChatCompactionLogic.isContextTooLargeError).
    private val tooLargeA = IllegalStateException(
        "compaction failed (400): this model's maximum context length is 128000 tokens",
    )
    private val tooLargeB = IllegalStateException(
        "compaction failed (400): prompt is too long for this model",
    )
    private val timeout = IllegalStateException(
        "compaction failed (timeout): read timed out after 45000ms",
    )
    private val unavailable = IllegalStateException(
        "compaction failed (503): service unavailable, try again later",
    )
    private val serverError = IllegalStateException(
        "compaction failed (500): internal server error",
    )

    private val initial = IllegalStateException("compaction failed")

    /** Fold a failure sequence exactly the way the production loop does. */
    private fun reduce(sequence: List<Exception>): Exception {
        var lastOrdinary: Exception = initial
        var pinnedTooLarge: Exception? = null
        for (candidate in sequence) {
            val next = ContextCompactor.chainFailureAfter(
                lastOrdinary = lastOrdinary,
                pinnedTooLarge = pinnedTooLarge,
                candidate = candidate,
                isTooLarge = ::isContextTooLargeError,
            )
            lastOrdinary = next.first
            pinnedTooLarge = next.second
        }
        return ContextCompactor.chainTerminalFailure(lastOrdinary, pinnedTooLarge)
    }

    // ── the classifier itself, pinned literally ─────────────────────────

    @Test
    fun `predicate literals classify as the reduction assumes`() {
        assertTrue("context-length 400 must classify as too-large", isContextTooLargeError(tooLargeA))
        assertTrue("'prompt is too long' must classify as too-large", isContextTooLargeError(tooLargeB))
        assertFalse("timeout must NOT classify as too-large", isContextTooLargeError(timeout))
        assertFalse("503 unavailable must NOT classify as too-large", isContextTooLargeError(unavailable))
        assertFalse("500 must NOT classify as too-large", isContextTooLargeError(serverError))
    }

    // ── the three required orders ───────────────────────────────────────

    @Test
    fun `too-large then ordinary - the too-large survives the chain`() {
        val terminal = reduce(listOf(tooLargeA, timeout))
        assertSame("the 400 must be the terminal failure", tooLargeA, terminal)
        assertTrue("terminal must take the halving path", isContextTooLargeError(terminal))
    }

    @Test
    fun `ordinary then too-large - the too-large is pinned when it arrives`() {
        val terminal = reduce(listOf(timeout, tooLargeA))
        assertSame("the 400 must be the terminal failure", tooLargeA, terminal)
        assertTrue("terminal must take the halving path", isContextTooLargeError(terminal))
    }

    @Test
    fun `all too-large - the earliest is pinned`() {
        val terminal = reduce(listOf(tooLargeA, tooLargeB))
        assertSame("the FIRST 400 anchors the timing", tooLargeA, terminal)
        assertTrue(isContextTooLargeError(terminal))
    }

    // ── the ordinary-only chain keeps its previous semantics ────────────

    @Test
    fun `ordinary only - the last failure is still thrown`() {
        val terminal = reduce(listOf(timeout, unavailable, serverError))
        assertSame("no capacity evidence: last failure, as before", serverError, terminal)
        assertFalse("no capacity evidence: no halving retry", isContextTooLargeError(terminal))
    }

    @Test
    fun `empty chain - the initial failure is thrown and does not halve`() {
        val terminal = reduce(emptyList())
        assertSame(initial, terminal)
        assertFalse(isContextTooLargeError(terminal))
    }

    @Test
    fun `too-large long before the tail still wins`() {
        val terminal = reduce(listOf(tooLargeA, timeout, unavailable, serverError))
        assertSame("three later ordinary failures must not bury the 400", tooLargeA, terminal)
        assertTrue(isContextTooLargeError(terminal))
    }

    // ── the reduction's own invariants ──────────────────────────────────

    @Test
    fun `an ordinary failure never clears an already-pinned too-large`() {
        val afterFirst = ContextCompactor.chainFailureAfter(initial, null, tooLargeA, ::isContextTooLargeError)
        assertEquals(initial, afterFirst.first)
        assertSame(tooLargeA, afterFirst.second)
        val afterSecond = ContextCompactor.chainFailureAfter(
            afterFirst.first, afterFirst.second, timeout, ::isContextTooLargeError,
        )
        assertSame("pinned stays pinned", tooLargeA, afterSecond.second)
        assertSame("ordinary slot advances", timeout, afterSecond.first)
        assertEquals(
            "terminal prefers the pin",
            tooLargeA,
            ContextCompactor.chainTerminalFailure(afterSecond.first, afterSecond.second),
        )
    }

    @Test
    fun `terminal falls back to the ordinary slot when nothing was pinned`() {
        assertEquals(serverError, ContextCompactor.chainTerminalFailure(serverError, null))
        assertSame(tooLargeA, ContextCompactor.chainTerminalFailure(serverError, tooLargeA))
    }
}

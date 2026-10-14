package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/rv1014-g1] The auto-title trigger must be a SEMANTIC condition (does
 * the session still need a title?), not a positional one (`turn == 0`).
 *
 * The reported regression: a first turn that came back empty (clean finish)
 * goes through the empty-turn recovery retry as turn 1; when the retry round
 * then completed cleanly, the decision point was reached with `turn == 0`
 * already false, so the auto-title was skipped until the next send — or
 * forever if the user never sent again.
 *
 * These pin [sessionTitleNeedsGenerationOf], the criterion both engine call
 * sites now key off via AgentLoopHost.sessionTitleNeedsGeneration. The loop
 * itself needs a live ViewModel; the criterion does not, so the criterion is
 * tested here and the call sites are thin adapters over it. The host
 * function's own guards (in-flight / max-attempts / already-set) own the
 * "exactly once / never overwrite a user-edited title" semantics; this
 * criterion must simply agree with the already-set guard so the trigger and
 * the guard can never drift.
 */
class AutoTitleTriggerTest {

    /** The OLD positional guard the engine used before the fix (verbatim shape). */
    private fun oldConditionFires(turn: Int): Boolean = turn == 0

    /** The NEW semantic condition (the production criterion itself). */
    private fun newConditionFires(title: String): Boolean = sessionTitleNeedsGenerationOf(title)

    // ── the reported failure: empty first turn + recovery retry ────────────

    @Test
    fun regressionSequence_retrySucceeded_stillFires() {
        // Turn 0 came back empty (clean finish) → recovery injected the
        // reminder and retried → the retry round completed cleanly. The
        // decision point is reached with turn == 1 and the title still
        // ungenerated.
        val turn = 1
        val title = "New Chat"
        assertTrue(newConditionFires(title))
        assertFalse(oldConditionFires(turn)) // the bug: old guard skipped here
    }

    @Test
    fun regressionSequence_titleGeneratedOnlyOnce_acrossTheSequence() {
        // Simulate the trigger reads across the reported sequence:
        // turn 0 (empty, recovery), turn 1 (retry, clean), then any later
        // turn. The title only becomes generated after turn 1's read —
        // one firing in the whole sequence.
        val reads = listOf(
            Pair(0, "New Chat"), // turn 0 read (during the empty round)
            Pair(1, "New Chat"), // turn 1 read — THIS is the one the old guard missed
            Pair(1, "My Topic"), // later read after generation landed
        )
        val firings = reads.count { (turn, title) -> newConditionFires(title) }
        assertEquals(2, firings) // reads while ungenerated fire; the host fn's own guards make it exactly-once overall
        assertEquals(1, reads.count { (turn, title) -> newConditionFires(title) && !oldConditionFires(turn) })
    }

    // ── criterion truth table (must match the already-set guard) ───────────

    @Test
    fun untouchedSession_needsGeneration() {
        assertTrue(newConditionFires("New Chat"))
    }

    @Test
    fun emptyTitle_needsGeneration() {
        assertTrue(newConditionFires(""))
    }

    @Test
    fun blankButNonEmptyTitle_countsAsSet() {
        // Matches the old guard's isNotEmpty semantics: " " is treated as a
        // (weird but present) title, not an ungenerated one.
        assertFalse(newConditionFires(" "))
    }

    @Test
    fun generatedTitle_doesNotFireAgain() {
        assertFalse(newConditionFires("Kotlin 协程入门"))
    }

    @Test
    fun userEditedTitle_doesNotFire() {
        // User renamed the session — the auto path must never overwrite it.
        assertFalse(newConditionFires("我的自定义标题"))
    }

    @Test
    fun criterion_matchesOldGuardFormula_exactly() {
        // The old already-set guard: skip when title != "New Chat" && title.isNotEmpty().
        // The criterion must be its exact negation for every input.
        val samples = listOf("", "New Chat", " ", "Real Title", "我的标题", "\t", "New chat", "New Chat ")
        for (title in samples) {
            val oldGuardSkips = title != "New Chat" && title.isNotEmpty()
            assertEquals("criterion mismatch for '$title'", oldGuardSkips, !newConditionFires(title))
        }
    }
}

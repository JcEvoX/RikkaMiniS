package com.rikkaminis.app.debug

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1048 A-07] — the headless drain must report THIS turn's answer.
 *
 * `HeadlessChatRunner` settles a turn when `isStreaming` flips back to false and
 * then reads the answer out of the DB. That flag only says "no stream is
 * running": a turn that ends without writing an assistant row (provider error /
 * cancel / an empty completion that `persistAssistantTurn` skips) used to be
 * settled by scanning for the LAST `assistant` row in the transcript — the
 * PREVIOUS turn — and reported as `status = "Completed"`.
 *
 * The layer split is the same as the rest of `debug/`: the runner itself needs
 * Android + `ChatViewModel`, so the DECISION lives in [TurnAnswerPick.kt]
 * (Android-free, two columns per row) and is asserted here as behaviour with
 * literal expectations. The mechanical twin is criterion 17 of
 * `scripts/scan/chat_session_id_binding_check.py`.
 */
class TurnAnswerPickTest {

    private fun row(id: String, role: String) = TurnAnswerRow(id, role)

    private fun rows(vararg pairs: Pair<String, String>) =
        pairs.map { row(it.first, it.second) }

    // ── turnAnswerIndex: the identity, not the role ─────────────────────────

    @Test
    fun `a turn that wrote no assistant row is not the previous turn's answer`() {
        // The exact failure shape: previous answer a1, this turn only appended u2.
        val prior = assistantIdsOf(rows("u1" to "user", "a1" to "assistant"))
        val after = rows("u1" to "user", "a1" to "assistant", "u2" to "user")
        assertEquals(-1, turnAnswerIndex(after, prior))
    }

    @Test
    fun `a turn that wrote an assistant row picks that row`() {
        val prior = assistantIdsOf(rows("u1" to "user", "a1" to "assistant"))
        val after = rows("u1" to "user", "a1" to "assistant", "u2" to "user", "a2" to "assistant")
        val idx = turnAnswerIndex(after, prior)
        assertEquals("a2", after[idx].id)
    }

    @Test
    fun `a fresh session accepts its first answer`() {
        val prior = assistantIdsOf(emptyList())
        val after = rows("u1" to "user", "a1" to "assistant")
        val idx = turnAnswerIndex(after, prior)
        assertEquals("a1", after[idx].id)
    }

    @Test
    fun `a truncated transcript cannot resurrect an older answer`() {
        // retry() deletes everything after the target user message. a2 (the
        // previous answer) is GONE, so "the last assistant row" is a1 — an even
        // older turn. A boundary of "not the previous answer's id" would accept
        // it; set membership of row identities cannot.
        val prior = assistantIdsOf(
            rows("u1" to "user", "a1" to "assistant", "u2" to "user", "a2" to "assistant"),
        )
        val after = rows("u1" to "user", "a1" to "assistant", "u2" to "user")
        assertTrue(
            "a2 must be gone from the truncated transcript",
            after.none { it.id == "a2" },
        )
        assertEquals(-1, turnAnswerIndex(after, prior))
    }

    @Test
    fun `a multi-round turn reports its last row, not its first`() {
        // A tool-running turn persists several assistant rows; the answer is the
        // final one (the one after the last tool result).
        val prior = assistantIdsOf(rows("u1" to "user", "a1" to "assistant"))
        val after = rows(
            "u1" to "user", "a1" to "assistant",
            "u2" to "user", "a2" to "assistant", "tr1" to "user", "a3" to "assistant",
        )
        val idx = turnAnswerIndex(after, prior)
        assertEquals("a3", after[idx].id)
    }

    @Test
    fun `assistantIdsOf keeps only assistant rows`() {
        val ids = assistantIdsOf(
            rows("u1" to "user", "a1" to "assistant", "tr1" to "user", "a2" to "assistant"),
        )
        assertEquals(setOf("a1", "a2"), ids)
        assertEquals(emptySet<String>(), assistantIdsOf(emptyList()))
        assertEquals(emptySet<String>(), assistantIdsOf(rows("u1" to "user")))
    }

    // ── settleTurn: the status contract ─────────────────────────────────────

    @Test
    fun `a settled turn with an answer is Completed`() {
        val s = settleTurn(finished = true, answered = true)
        assertEquals("Completed", s.status)
        assertNull("Completed must not carry a reason override", s.reason)
    }

    @Test
    fun `a settled turn with no answer is a structured error`() {
        val s = settleTurn(finished = true, answered = false)
        assertEquals("Error", s.status)
        assertEquals("no_answer_this_turn", s.reason)
        // Status strings are a contract: anything != "Running" settles as
        // RESULT_ERROR in ScheduledTaskRunner, and the offload/CLI layer prints
        // responseText as the machine reason.
        assertEquals(NO_TURN_ANSWER_REASON, s.reason)
    }

    @Test
    fun `a timeout stays a timeout whether or not a partial answer exists`() {
        assertEquals("Timeout", settleTurn(finished = false, answered = false).status)
        assertEquals("Timeout", settleTurn(finished = false, answered = true).status)
        assertNull(settleTurn(finished = false, answered = false).reason)
        assertNull(settleTurn(finished = false, answered = true).reason)
    }
}

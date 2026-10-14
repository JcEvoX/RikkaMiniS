package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage

/**
 * [port-v114 S4 / T-android-empty-turn-reminder-any-tail] Decides how the agent loop recovers
 * from an EMPTY model turn — one where the model produced no text and no tool
 * call and simply stopped.
 *
 * Background: `runAgentLoop` classifies a turn as empty via
 * `turnText.isEmpty() && toolCalls.isEmpty()`. An empty turn is high-
 * probability whenever the conversation ends on a STATEMENT the model owes no
 * reply to (a finished-task notice, an injected reminder, etc.). A tool_result
 * tail (the case the loop already handled) is just one shape of the same
 * problem; a user TEXT tail — a statement the model owes no reply to — falls
 * through to the error hint with no retry.
 *
 * The recovery is: remove the empty assistant turn, append a one-shot
 * `<system-reminder>` nudging the model to continue, and retry ONE round. This
 * object decides WHERE the reminder goes; the loop performs the mutation and
 * owns the once-per-run guard. Keeping the decision pure lets it be tested
 * without a live ViewModel (mirrors FallbackPulseGate).
 */
internal object EmptyTurnRecovery {

    /** The reminder text appended to the history tail before the retry. */
    const val REMINDER: String =
        "\n\n<system-reminder>The previous response was empty. You MUST continue: " +
            "if the last message was a completed result or a notice, acknowledge it " +
            "and take the next step or give the user a final answer; if more work is needed, " +
            "respond with the next tool call(s). Do not return an empty response.</system-reminder>"

    sealed class Plan {
        /**
         * Append [REMINDER] onto the content-part at [tailIndex]/[partIndex] of
         * the tail user turn (a tool_result part, or a text part).
         */
        data class NudgeExistingPart(val tailIndex: Int, val partIndex: Int, val isToolResult: Boolean) : Plan()

        /** Append a fresh standalone user turn carrying [REMINDER]. */
        object AppendStandalone : Plan()

        /** No user turn to nudge (e.g. tail is an assistant row) — fall through to the error hint. */
        object GiveUp : Plan()
    }

    /**
     * @param history the FULL agentHistory, whose LAST entry is the empty
     *   assistant turn the loop just appended. The tail being replied to is at
     *   `history[size - 2]`.
     * @param alreadyFired the once-per-run guard: when true, always [GiveUp].
     */
    fun plan(history: List<LLMMessage>, alreadyFired: Boolean): Plan {
        if (alreadyFired) return Plan.GiveUp
        if (history.size < 2) return Plan.GiveUp
        val tailIdx = history.size - 2
        val tail = history[tailIdx]
        if (tail.role != LLMMessage.Role.USER) return Plan.GiveUp

        // Prefer a trailing tool_result (original behaviour, byte-identical for
        // that case), else a trailing text part (the statement / notice tail).
        val trPartIdx = tail.contentParts.indexOfLast { it is AgentContentPart.ToolResult }
        if (trPartIdx >= 0) return Plan.NudgeExistingPart(tailIdx, trPartIdx, isToolResult = true)
        val txtPartIdx = tail.contentParts.indexOfLast { it is AgentContentPart.Text }
        if (txtPartIdx >= 0) return Plan.NudgeExistingPart(tailIdx, txtPartIdx, isToolResult = false)

        // User tail with no appendable part (image-only, etc.).
        return Plan.AppendStandalone
    }

    /**
     * [fix/rv1014-g2] Performs the [Plan.AppendStandalone] mutation: drop the
     * empty assistant turn the loop just appended, enforce role alternation
     * (the plan pre-condition is a USER tail — a blind USER append yields two
     * consecutive user roles, and Gemini maps messages 1:1 on the request side
     * with no consecutive-role merge), then append the standalone reminder
     * turn. Same bridge family every other append point uses
     * ([ensureRoleAlternationBeforeUserAppend]).
     *
     * Pure + JVM-testable (no ViewModel dependencies).
     */
    fun appendStandaloneReminderTurn(history: MutableList<LLMMessage>, reminder: String) {
        history.removeAt(history.size - 1)
        ensureRoleAlternationBeforeUserAppend(history)
        history.add(
            LLMMessage(
                role = LLMMessage.Role.USER,
                content = reminder,
                contentParts = listOf(AgentContentPart.Text(reminder)),
            ),
        )
    }
}

package com.rikkaminis.app.agent

/**
 * Which "the agent loop stopped early" shape a session's tail matches.
 *
 * Extracted from [com.rikkaminis.app.ui.chat.ChatViewModel.loadSession]
 * (lives in ChatSessionLifecycle.kt) and from
 * [com.rikkaminis.app.data.repository.ChatRepository.isInterruptedTail] so the
 * rule has ONE copy instead of two hand-kept duplicates. It decides whether
 * the user gets a Resume affordance at all, and a wrong answer is invisible in
 * code review — either the banner never appears (GH#262/#263: a reply-less user
 * tail used to be un-recoverable) or it appears over a turn that is simply
 * still waiting.
 */
enum class InterruptedTailShape {
    /** Case A — tools completed, but the follow-up model call never fired. */
    TOOL_RESULT_TAIL,

    /** Case B — the model asked for tools that never executed. */
    ASSISTANT_TOOL_USE,

    /** Case C — the synthetic "user stopped the previous response" reminder was
     *  committed, but `resume()` never re-entered the agent loop. */
    CONTINUE_REMINDER,

    /**
     * Case D — a plain-text user turn with NO reply after it at all (GH#262/#263).
     *
     * `send()` persists the user row BEFORE the reply lands, and
     * `persistAssistantTurn()` drops an assistant row that has no parts — so a
     * process death in between (or a first-turn network failure, where
     * `setInlineError` has no assistant row to attach to) leaves a tail that
     * looks finished but never got an answer.
     */
    UNANSWERED_USER_TURN,

    /** Not interrupted. */
    NONE,
}

/**
 * A persisted content part reduced to the three facts the classifier needs.
 *
 * Kept primitive on purpose: the two callers persist history in different
 * shapes (ChatSessionLifecycle reads typed
 * [com.rikkaminis.app.data.model.AgentContentPart] lists, ChatRepository reads
 * the raw `parts_json` column), and neither mapping needs the other's types.
 * Keeping this file free of Android / org.json imports is what lets it be
 * unit-tested directly instead of only through a mocked repository.
 */
enum class InterruptedTailPartKind { TEXT, TOOL_USE, TOOL_RESULT, OTHER }

/** Minimal view of the final `agentHistory` entry, see [InterruptedTailPartKind]. */
data class InterruptedTailSnapshot(
    /** Lowercased role: "user" / "assistant". Case is normalised inside
     * [InterruptedTailDetector.classify], so a caller may pass either. */
    val role: String,
    /** One entry per persisted content part, in order. */
    val partKinds: List<InterruptedTailPartKind>,
    /** Text of the FIRST part when it is text, else null. */
    val firstText: String?,
)

object InterruptedTailDetector {

    /** Marker text of the synthetic continue reminder (see `ChatViewModel.resume()`). */
    const val CONTINUE_REMINDER_MARKER = "The user stopped the previous response"

    /**
     * Classify [snapshot] — the final entry of `agentHistory`.
     *
     * Callers MUST additionally gate on "nothing is currently streaming"
     * (`!isStreaming && !SessionActivityTracker.isActive(sid)`). This function
     * deliberately knows nothing about liveness: it answers "what shape is
     * this tail", not "is it safe to offer Resume", and conflating the two is
     * how a still-waiting turn would get a Resume banner.
     */
    fun classify(snapshot: InterruptedTailSnapshot?): InterruptedTailShape {
        if (snapshot == null) return InterruptedTailShape.NONE
        return when (snapshot.role.lowercase()) {
            "user" -> {
                val kinds = snapshot.partKinds
                val allToolResults = kinds.isNotEmpty() &&
                    kinds.all { it == InterruptedTailPartKind.TOOL_RESULT }
                val isContinueReminder = kinds.size == 1 &&
                    kinds.first() == InterruptedTailPartKind.TEXT &&
                    snapshot.firstText?.contains(CONTINUE_REMINDER_MARKER) == true
                when {
                    // Order matters: the first two describe a turn that was
                    // mid-flight; the third describes one that never started.
                    allToolResults -> InterruptedTailShape.TOOL_RESULT_TAIL
                    isContinueReminder -> InterruptedTailShape.CONTINUE_REMINDER
                    // An EMPTY user turn is not a recoverable shape — there is
                    // nothing to answer, and re-sending it would post a
                    // content-less message the API rejects.
                    kinds.isEmpty() -> InterruptedTailShape.NONE
                    else -> InterruptedTailShape.UNANSWERED_USER_TURN
                }
            }
            "assistant" ->
                if (snapshot.partKinds.any { it == InterruptedTailPartKind.TOOL_USE }) {
                    InterruptedTailShape.ASSISTANT_TOOL_USE
                } else {
                    // A plain assistant reply IS the completed turn.
                    InterruptedTailShape.NONE
                }
            else -> InterruptedTailShape.NONE
        }
    }

    /** True when the tail is any recoverable shape. */
    fun isInterrupted(snapshot: InterruptedTailSnapshot?): Boolean =
        classify(snapshot) != InterruptedTailShape.NONE
}

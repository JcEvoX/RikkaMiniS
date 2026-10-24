package com.rikkaminis.app.debug

/**
 * [deep-audit-1048 A-07] Which assistant row counts as THIS turn's answer.
 *
 * The headless runner settles a turn when `isStreaming` flips back to false and
 * then reads the answer out of the DB. `isStreaming == false` only says "no
 * stream is running" — it does NOT say "this turn produced an assistant
 * message". A turn that ends without one (provider error / cancel / an empty
 * completion that `persistAssistantTurn` skips because `parts.isEmpty()`) used
 * to be settled by scanning the transcript for the LAST `assistant` row, which
 * is the PREVIOUS turn's row: the RPC caller and `minis-sessions send` accepted
 * a stale reply as `status = "Completed"`.
 *
 * The fix is an identity, not a role scan: snapshot the ids of the assistant
 * rows that exist BEFORE the turn is dispatched, and accept only a row that
 * appeared after that snapshot.
 *
 * Why a SET of ids and not a position (`turnStartIndex` / `msgs.size`):
 * `retry` and `rerunFromToolBlock` TRUNCATE the transcript before re-running
 * (`deletedCount = all.size - indexOf(target) - 1`), so a positional boundary
 * shifts under the turn itself — and the "last assistant id" variant is worse
 * still: the truncation DELETES the previous answer, so "id != previous" would
 * accept an even older turn. Only set membership of row identities survives
 * both appends and deletes; `messages.id` is a fresh UUID per append
 * (`ChatRepository.appendMessage`), so "not in the snapshot" == "written during
 * this turn".
 *
 * Android-free on purpose (two columns, no Room/Context imports) so the
 * decision is unit-testable off-device — the runner itself is not.
 */

/** The two columns of a persisted message row this decision needs. */
internal data class TurnAnswerRow(val id: String, val role: String)

/** Ids of the assistant rows already on disk — the identity of "before this turn". */
internal fun assistantIdsOf(rows: List<TurnAnswerRow>): Set<String> {
    val out = HashSet<String>()
    for (row in rows) if (row.role == "assistant") out.add(row.id)
    return out
}

/**
 * Index (into [rows]) of the LAST assistant row that appeared after
 * [priorAssistantIds] was snapshotted, or `-1` when this turn produced no
 * assistant row of its own.
 *
 * `-1` is a real outcome, not an error: the caller must report "no answer this
 * turn" instead of replaying an earlier turn's text.
 */
internal fun turnAnswerIndex(rows: List<TurnAnswerRow>, priorAssistantIds: Set<String>): Int {
    for (i in rows.indices.reversed()) {
        val row = rows[i]
        if (row.role == "assistant" && row.id !in priorAssistantIds) return i
    }
    return -1
}

/**
 * How a settled turn reports itself.
 *
 * Status strings are a contract: `ScheduledTaskRunner` settles any status !=
 * "Running" as RESULT_ERROR, and the offload/CLI layer prints `responseText` as
 * the machine-readable reason (`SessionsOffloadHandler.sendErrorReason`). So a
 * settled turn without an answer reuses the existing `"Error"` value plus a
 * reason, exactly like `droppedSendResult()` — no new status word.
 */
internal data class TurnSettlement(val status: String, val reason: String?)

/** Machine reason for "the turn settled but wrote no assistant row". */
internal const val NO_TURN_ANSWER_REASON = "no_answer_this_turn"

/**
 * Status/reason for a settled turn. [finished] is the wait outcome (false =
 * the timeout elapsed), [answered] says whether this turn's own assistant row
 * was found. A timeout stays a timeout; a settled turn with no answer is an
 * error, never a pseudo-success.
 */
internal fun settleTurn(finished: Boolean, answered: Boolean): TurnSettlement = when {
    !finished -> TurnSettlement("Timeout", null)
    answered -> TurnSettlement("Completed", null)
    else -> TurnSettlement("Error", NO_TURN_ANSWER_REASON)
}

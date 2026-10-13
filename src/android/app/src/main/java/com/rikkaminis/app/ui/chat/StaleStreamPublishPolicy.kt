package com.rikkaminis.app.ui.chat

/**
 * [fix/cancel-stale-publish] Admission policy for a streaming side-channel
 * publish ([ChatViewModel.publishStreamingDelta]).
 *
 * ## The bug this closes
 *
 * A user interrupt during tool execution is handled by
 * `sendMessage` → `enqueuePrompt` + `cancelStream()`, which (a) cancels the
 * agent-loop job, (b) drains the side channel into the canonical message and
 * (c) flips the in-flight tool blocks to `CANCELLED` so the tool capsules stay
 * on screen with a terminal status.
 *
 * `streamJob.cancel()` is cooperative, and it only covers the loop job. Several
 * writers survive it and fire *after* the cleanup has finalized the row:
 *
 *  - `onBlockUpdate`'s per-line `viewModelScope.launch(Dispatchers.Main)`
 *    publishes (one per streamed shell line — a chatty tool queues hundreds);
 *  - engine `withContext(Dispatchers.Main)` publishes that were already past
 *    their suspension point when the cancel landed;
 *  - the throttle trailing job (viewModelScope, explicitly NOT cancelled by
 *    `streamJob.cancel()`).
 *
 * Each of those calls `updateAssistantMessage(..., isStreaming = true, blocks)`,
 * which writes a fresh `_streamingById` entry — and `mergeStreamingOverlay`
 * REPLACES the canonical row's `toolBlocks` with that entry wholesale. So the
 * last writer wins, and the loser is the CANCELLED capsule state the user is
 * looking at: the capsules flip back to a ghost `RUNNING`, or — when the
 * interrupt landed in the window where the loop had already cleared
 * `allToolBlocks` (the queued-prompt interrupt checkpoint) — the publish
 * carries an EMPTY block list and the capsules vanish outright.
 *
 * ## The rule
 *
 * A message row is streamed into by exactly one live turn, and `runAgentLoop`
 * always mints a NEW assistant id for a new turn (`addAssistantPlaceholder` /
 * the queued-prompt bridge) — nothing ever re-streams into a finalized row.
 * Therefore: an `isStreaming = true` publish for a row that exists, is no
 * longer streaming, and has no live side-channel entry is stale by
 * construction and must be dropped instead of re-minting an overlay entry.
 *
 * The `hasSideChannelEntry` clause is a safety valve, not a second rule: while
 * a turn is live the canonical row carries `isStreaming = true`, so the only
 * way the clause can matter is a write ordering where the row's flags were
 * already cleared but the entry survives — in that case the entry is the
 * fresher state and the publish is honored (fail-open, i.e. today's
 * behaviour).
 *
 * A publish for an id with NO canonical row is admitted: the overlay maps over
 * the message list, so a delta for an unknown id is inert, and rejecting it
 * would change the placeholder-gap behaviour for no benefit.
 *
 * Pure function — no Android/AppLogger dependency, fully JVM-testable.
 */
internal object StaleStreamPublishPolicy {

    /** Outcome of the admission check; the caller logs the dropped case. */
    enum class Verdict {
        /** Write the side-channel entry (normal live-turn publish). */
        PUBLISH,

        /**
         * The target row was finalized (cancel cleanup / queued-prompt seal /
         * natural stream end) and no live side-channel entry backs it — the
         * publish is a late writer racing the teardown. Drop it.
         */
        DROP_FINALIZED,
    }

    /**
     * @param rowExists           a canonical `_messages` row carries this id
     * @param rowIsStreaming      that row's `isStreaming` flag
     * @param hasSideChannelEntry `_streamingById` already holds a delta for
     *                            this id
     */
    fun verdict(
        rowExists: Boolean,
        rowIsStreaming: Boolean,
        hasSideChannelEntry: Boolean,
    ): Verdict = if (rowExists && !rowIsStreaming && !hasSideChannelEntry) {
        Verdict.DROP_FINALIZED
    } else {
        Verdict.PUBLISH
    }

    /** Convenience for call sites that only branch. */
    fun shouldDrop(
        rowExists: Boolean,
        rowIsStreaming: Boolean,
        hasSideChannelEntry: Boolean,
    ): Boolean = verdict(rowExists, rowIsStreaming, hasSideChannelEntry) == Verdict.DROP_FINALIZED
}

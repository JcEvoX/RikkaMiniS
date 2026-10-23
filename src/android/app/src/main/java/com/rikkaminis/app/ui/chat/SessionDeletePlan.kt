package com.rikkaminis.app.ui.chat

/**
 * [dv27-B4 / deep-audit-1047 B-06] The one ordered list of steps a
 * UI-initiated session deletion runs, in the order it must run them.
 *
 * The order used to be implicit (and wrong): `deleteSessionAndCleanup` deleted
 * the row plus the recursive session directory FIRST and only then released the
 * resident [ChatViewModel] from `ChatViewModelStore`. Two failure modes came out
 * of that window:
 *
 *   1. The live view model kept its `streamJob` and its `BrowserTabPool`
 *      (`isDisposed` still false), so a write that landed during the delete
 *      recreated `minis-sessions/<id>/…` / `browser_tabs/<id>.json` in the tree
 *      that had just been removed — the deleted session came back as an empty
 *      ghost directory.
 *   2. `release` cancels the stream's `viewModelScope`; the cancelled job's
 *      `finally` then publishes an overlay excerpt / sets the session inactive
 *      on IO *after* `SessionBadgeStore.clear(id)` had already run, so the
 *      deleted id lit its completion badge / floating excerpt back up.
 *
 * Stopping the producer before removing what it produces into closes both.
 * `ChatMutationMethods.delete` (the headless entry) already cancels first for
 * the same reason; this plan gives the UI path a single named source for the
 * sequence so a JVM test can pin it instead of re-deriving it from a
 * Compose-only file.
 *
 * Kept free of Android imports on purpose: the sandbox JVM harness compiles
 * this exact file (`kotlinc` + JUnitCore), so the assertion runs against the
 * production text rather than a replica.
 */
internal enum class SessionDeleteStep {
    /** Cancel the resident view model + its tab pool. Must hop to Main. */
    RELEASE_VIEW_MODEL,

    /** Remove the row, its messages and its session directory. */
    DELETE_ROW,

    /** Drop any queued badge so the deleted id cannot light up again. */
    CLEAR_BADGE,
}

/**
 * The order is deliberate and is the ONLY place it is written down:
 * release → delete → clear. Never delete before release (the ghost-directory
 * and badge-resurrection windows above), never clear the badge before the
 * delete (a failed delete would leave a live session unbadged).
 */
internal fun sessionDeletePlan(): List<SessionDeleteStep> = listOf(
    SessionDeleteStep.RELEASE_VIEW_MODEL,
    SessionDeleteStep.DELETE_ROW,
    SessionDeleteStep.CLEAR_BADGE,
)

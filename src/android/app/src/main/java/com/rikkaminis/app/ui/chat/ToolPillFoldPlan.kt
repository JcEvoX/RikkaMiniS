package com.rikkaminis.app.ui.chat

/**
 * [T-android-tool-pill-fold] Render bound for the tool pills of ONE frozen
 * assistant message.
 *
 * Background (2026-09-27 perf audit, session 9068927b): a single agent turn
 * accumulates every tool call of the turn into one aggregate row
 * ([FlatChatItem.AssistantMessageItem] → `AssistantMessageView`). Measured on
 * device: one such row placed at `992x34976` px ≈ 15 screens, and composing it
 * on re-entry was the user-visible jank. The arithmetic behind that number:
 * ~277 one-line tool pills × ~93 px ≈ 25,800 px plus ~5,700 chars of assistant
 * prose ≈ 9,000 px — the pills are ~3/4 of the wall, and `LargeContentGuard`
 * never saw them because it only guards *text* block length.
 *
 * Fold policy (deliberately narrow):
 *  - **Frozen only.** While a turn is streaming the newest pill is the live
 *    status surface, and a mid-stream fold would hide the pill the user is
 *    actually watching. Same rationale [LargeContentGuard] documents for its
 *    own streaming bypass.
 *  - **Head-anchored.** The first [TOOL_PILL_FOLD_LIMIT] pills stay inline in
 *    stream order; everything after folds behind one summary row that sits
 *    exactly where the folded pills begin. Prose blocks keep rendering in
 *    place, so the message still reads top-to-bottom.
 *  - **Nothing is dropped.** The folded pills are data, not rendering: one tap
 *    on the summary row composes them all, tap again folds back.
 *  - **Trigger ≠ limit.** [TOOL_PILL_FOLD_TRIGGER] (12) is where folding starts
 *    being worth a tap; [TOOL_PILL_FOLD_LIMIT] (8) is how many stay inline. A
 *    9-pill turn keeps rendering all 9 — the fold must not tax the common case
 *    to fix the tail.
 *  - Expansion state is per message and resets when the session is reopened
 *    ([androidx.compose.runtime.saveable.rememberSaveable]), mirroring the
 *    guard's "explicit opt-in per session view" stance.
 *
 * Pure on purpose (no Compose / Android imports) so the decision is covered by
 * a plain JVM unit test — the call site can only supply the wrong count, not
 * the wrong rule.
 */

/**
 * Fold only when a message carries more pills than this. Below it the pill run
 * already fits roughly half a screen (12 × ~93 px ≈ 1,100 px), so folding would
 * cost a tap for almost nothing.
 */
internal const val TOOL_PILL_FOLD_TRIGGER = 12

/** How many pills stay inline once folding is active. ~8 × 93 px ≈ 740 px. */
internal const val TOOL_PILL_FOLD_LIMIT = 8

/**
 * @param foldable  this message is a fold candidate at all (frozen + over
 *                  [TOOL_PILL_FOLD_TRIGGER]).
 * @param applied   the fold is currently collapsing the pills. False while
 *                  expanded, and false whenever [foldable] is false — in both
 *                  cases every pill renders.
 * @param limit     pills `[0, limit)` stay inline while [applied].
 * @param totalPills tool_use blocks in the message.
 */
internal data class ToolPillFoldPlan(
    val foldable: Boolean,
    val applied: Boolean,
    val limit: Int,
    val totalPills: Int,
) {
    /** Pills hidden behind the summary row. 0 when not [foldable]. */
    val hiddenCount: Int
        get() = if (foldable) (totalPills - limit).coerceAtLeast(0) else 0

    /**
     * Whether the pill at [pillIndex] (0-based, in stream order) renders
     * inline. Every pill renders unless the fold is [applied].
     */
    fun isPillVisible(pillIndex: Int): Boolean = !applied || pillIndex < limit
}

/**
 * Decide the fold for one message. [expanded] is the user's per-message tap
 * state; it only matters while [ToolPillFoldPlan.foldable] holds.
 */
internal fun planToolPillFold(
    isStreaming: Boolean,
    totalPills: Int,
    expanded: Boolean,
    trigger: Int = TOOL_PILL_FOLD_TRIGGER,
    limit: Int = TOOL_PILL_FOLD_LIMIT,
): ToolPillFoldPlan {
    val foldable = !isStreaming && totalPills > trigger
    return ToolPillFoldPlan(
        foldable = foldable,
        applied = foldable && !expanded,
        limit = limit,
        totalPills = totalPills,
    )
}

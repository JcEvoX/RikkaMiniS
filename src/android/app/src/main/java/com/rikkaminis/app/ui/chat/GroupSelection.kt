package com.rikkaminis.app.ui.chat

/**
 * [deep-audit-1047 B-04] The decision behind a group (or group-member)
 * selection, as a pure value.
 *
 * Both `selectGroup` and `selectGroupEntry` used to write their display state
 * FIRST (`_selectedGroupId` / `_selectedGroupName`) and only then ask
 * `resolveProviderFromGroup` whether the group could actually serve. When the
 * answer was no, the top bar and the picker highlight showed group B while
 * `currentProvider` / `currentModel` / `_activeEntryId` and the persisted
 * session binding stayed on group A — the app claimed a switch it never made.
 *
 * A member pick has a second half of the same problem: the member the user
 * tapped is not necessarily the member that ends up serving, because
 * resolution falls back off a member it cannot use (disabled instance, or no
 * credential). Writing the raw `entryId` into `pendingEntryOverride` and into
 * the persisted `lastEntryId` bound the next rotation and the next session
 * reload to a member that had just been skipped.
 *
 * Android-free and dependency-free on purpose: this is the one part of the
 * selection that is decidable without a repository, so it is unit-testable on
 * the JVM (see `GroupSelectionPlanTest`).
 *
 * @property applyUi whether the caller may publish the new group as the
 *   selected one — false keeps the previous selection untouched.
 * @property effectiveEntryId the entry that will actually serve the next turn;
 *   null exactly when [applyUi] is false.
 */
data class GroupSelectPlan(
    val applyUi: Boolean,
    val effectiveEntryId: String?,
)

/**
 * Plan one group / group-member selection.
 *
 * Order of the arms is the contract:
 *  1. a group that did not resolve publishes NOTHING (the previous selection
 *     survives intact — that is the whole point of the fix);
 *  2. once resolved, the entry that is actually active is the only id any
 *     consumer may write, because resolution is allowed to fall back off the
 *     member the user asked for;
 *  3. the requested id is the last resort, for a resolution that published no
 *     active entry of its own.
 *
 * @param resolved whether the group resolved to a member that can serve.
 * @param resolvedId the entry id that ended up active after resolution.
 * @param requestedId the entry the caller asked for.
 */
fun planGroupSelect(
    resolved: Boolean,
    resolvedId: String?,
    requestedId: String,
): GroupSelectPlan = when {
    !resolved -> GroupSelectPlan(applyUi = false, effectiveEntryId = null)
    resolvedId != null -> GroupSelectPlan(applyUi = true, effectiveEntryId = resolvedId)
    else -> GroupSelectPlan(applyUi = true, effectiveEntryId = requestedId)
}

package com.rikkaminis.app.ui.chat

/**
 * [dv25/B-04] The model picker's two 「which entry stands for this group」
 * decisions, extracted from [ModelPickerSheet].
 *
 * Both were spelled inline and both were wrong in one of two ways:
 *
 *  1. `config.modelEntries.find { it.id == activeEntryId }` — the persisted-only
 *     half of the single source. A default Voice Input/Output group is built
 *     from System sentinels alone, so selecting one resolved its own ACTIVE
 *     entry to null while the member list below it resolved fine
 *     (`SystemVoiceEntries.resolveIn`).
 *  2. `if (…) { … } else null ?: group.memberEntryIds…` — Kotlin binds `?:`
 *     tighter than the `if` branches, so the elvis attached to the `else`
 *     branch ALONE. The `if` branch — the selected-group case, the one that
 *     matters — had no fallback at all: an unresolvable active entry dropped
 *     the header to null even though the group had resolvable members.
 *
 * Extracted rather than re-spelled in place for two reasons: the decision is
 * the only part of the header that can be pinned on the JVM (see
 * `PickerGroupResolutionTest`), and one helper stops the third spelling from
 * being re-derived (dv24-B02 fixed five readers of the same decision; this is
 * the sixth and seventh site on this surface).
 *
 * No Compose and no Android dependency on purpose, and the entry type is a
 * TYPE PARAMETER so the file compiles on a bare JVM: the caller's single-source
 * lookup — `SystemVoiceEntries.resolveIn(config.modelEntries, …)` — is injected
 * rather than imported, so this file cannot grow a second spelling of
 * 「persisted entry first, synthesized System entry second」.
 */
internal fun <T> pickerHeaderEntry(
    isSelected: Boolean,
    activeEntryId: String?,
    memberEntryIds: List<String>,
    resolve: (String) -> T?,
): T? {
    // Selected group: the entry actually in use wins, and the group's first
    // resolvable member is the fallback when it cannot be resolved (a stale id
    // left by another group, or a member that was removed). Unselected group:
    // the header is a preview of the first member.
    val fromActive = if (isSelected && activeEntryId != null) resolve(activeEntryId) else null
    return fromActive ?: memberEntryIds.firstNotNullOfOrNull(resolve)
}

/**
 * [dv25/B-04] The expanded group's member rows.
 *
 * [resolvedMembers] is passed in rather than re-derived so the caller keeps the
 * ONE `mapNotNull` that feeds the rows. The fallback matters in the opposite
 * direction from its sibling: it is used only when the group resolves to no
 * member at all (every member id stale), and then showing the ACTIVE entry is
 * what keeps the list honest — the header above already says that entry is in
 * use. That lookup goes through the same [resolve] single source; the pre-fix
 * `modelEntries`-only spelling returned null for a System sentinel and the list
 * rendered 「No linked models」 under a header that knew better.
 */
internal fun <T> pickerDisplayMembers(
    resolvedMembers: List<T>,
    isSelected: Boolean,
    activeEntryId: String?,
    resolve: (String) -> T?,
): List<T> = resolvedMembers.ifEmpty {
    if (isSelected && activeEntryId != null) listOfNotNull(resolve(activeEntryId)) else emptyList()
}

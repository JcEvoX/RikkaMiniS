package com.rikkaminis.app.ui.settings

/**
 * [dv27-B3] The three per-member PRESENTATION decisions of the group detail
 * screen, lifted out of the Compose tree so they can be pinned by a plain JVM
 * test (and by `provider_remember_key_check` criteria C-SV1 / C-SV2).
 *
 * Why they exist at all: a default `Voice Input` / `Voice Output` group is made
 * ENTIRELY of System voice sentinels (`SystemVoiceEntries`), and those are
 * virtual — they live in `SystemVoiceIds.BUILTIN_PROVIDER_ID`, never in
 * `config.modelEntries`, and have no `ProviderInstance` (the device is the
 * provider). Three separate places on this screen therefore answered a question
 * about 「a member that has no cloud provider behind it」 by accident:
 *
 *   • the row subtitle read `instance?.label` and fell back to `""`, so every
 *     row of every default Voice group had a blank secondary line;
 *   • the cost-tier menu offered a tier write that `updateEntry` silently
 *     drops (`indexOfFirst == -1`), so the tick never moved;
 *   • the remove dialog looked the member up in `config.modelEntries` only, so
 *     it degraded to the generic 「remove this model」 copy.
 *
 * Deliberately Android-free and dependency-free (String/Boolean only): the
 * decisions are the part that can be wrong, and they must be checkable without
 * a device. Callers pass in everything that needs a `stringResource` / a lookup,
 * so localization stays at the call site and zero new language keys are needed.
 */
internal object SystemVoiceMemberPresentation {

    /**
     * The secondary line under a member row.
     *
     * [instanceLabel] is the persisted provider's label, or null when the entry
     * has no instance behind it — which is ALWAYS the case for a System voice
     * entry, and is also the shape of a stale persisted entry whose provider was
     * deleted. [systemLabel] is the localized built-in provider name (the same
     * `R.string.voice_provider_system` the pickers use for their synthesized
     * System section), and is only reachable through [isSystemEntry]: a System
     * member's provider IS the device, so it must name it instead of going
     * blank, while a stale persisted entry correctly shows nothing rather than
     * claiming to be served by System.
     */
    fun memberSubtitle(
        instanceLabel: String?,
        isSystemEntry: Boolean,
        systemLabel: String,
    ): String = instanceLabel ?: if (isSystemEntry) systemLabel else ""

    /**
     * May the cost-tier menu be offered for this member?
     *
     * No for a System voice entry: its tier is not a persisted field, and the
     * write path (`ProviderRepository.updateEntry`) resolves the entry by id
     * inside `config.modelEntries` — a virtual entry is simply not there, so the
     * tap would close the menu and change nothing. Hide the rows instead of
     * showing a control that cannot work; 「从组中移除」 stays visible on
     * purpose, because taking a System member out of a custom group is a real
     * user action.
     */
    fun canEditCostTier(isSystemEntry: Boolean): Boolean = !isSystemEntry

    /**
     * The model name to put in the 「remove X from this group?」 confirmation.
     *
     * [entryName] is `entry.model.displayName` for whatever the member id
     * resolved to — persisted entry or System virtual entry, both resolved
     * through `SystemVoiceEntries.resolveIn` at the call site. [fallback] is
     * shown only for a genuinely stale id (a member whose model no longer exists
     * anywhere), which is a different situation from a System member: before
     * dv27-B3 the two were conflated, so removing a System voice entry from a
     * group asked 「remove this model?」 with no name.
     */
    fun removalDisplayName(entryName: String?, fallback: String): String =
        entryName ?: fallback
}

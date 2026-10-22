package com.rikkaminis.app.data.model

/**
 * [T-android-provider-voice] Virtual System voice entries — Android port of
 * iOS UnifiedModelPicker's ModelEntry.systemASROnline / systemASROffline /
 * systemTTS extensions. These are NOT persisted in config.modelEntries: they
 * are synthesized on demand so voice group members like
 * "__builtin_system_speech__/system-asr-online" resolve to a real-looking
 * entry in pickers and group detail instead of rendering as a stale member.
 *
 * uuid is the composite "<sentinel>/<model-id>" — exactly the member id shape
 * ensureDefaultVoiceInputGroup/-OutputGroup write (and the shape iOS
 * ModelEntry.id produces), so `entry.id == memberId` holds everywhere.
 */
object SystemVoiceEntries {

    /** System ASR — Online (server-based). Higher accuracy, needs network. */
    val asrOnline = ModelEntry(
        providerInstanceId = SystemVoiceIds.BUILTIN_PROVIDER_ID,
        baseModel = LLMModel(
            id = SystemVoiceIds.SYSTEM_ASR_ONLINE,
            displayName = "System Recognition (Online)",
            provider = "system",
            inputModalities = listOf("audio"),
        ),
        isHidden = true,
        uuid = "${SystemVoiceIds.BUILTIN_PROVIDER_ID}/${SystemVoiceIds.SYSTEM_ASR_ONLINE}",
    )

    /** System ASR — Offline (on-device). No rate limits; audio stays local. */
    val asrOffline = ModelEntry(
        providerInstanceId = SystemVoiceIds.BUILTIN_PROVIDER_ID,
        baseModel = LLMModel(
            id = SystemVoiceIds.SYSTEM_ASR_OFFLINE,
            displayName = "System Recognition (Offline)",
            provider = "system",
            inputModalities = listOf("audio"),
        ),
        isHidden = true,
        uuid = "${SystemVoiceIds.BUILTIN_PROVIDER_ID}/${SystemVoiceIds.SYSTEM_ASR_OFFLINE}",
    )

    /** System TTS — best installed voice per reply language. */
    val tts = ModelEntry(
        providerInstanceId = SystemVoiceIds.BUILTIN_PROVIDER_ID,
        baseModel = LLMModel(
            id = SystemVoiceIds.SYSTEM_TTS,
            displayName = "System Voice (Auto)",
            provider = "system",
            outputModalities = listOf("audio"),
        ),
        isHidden = true,
        uuid = "${SystemVoiceIds.BUILTIN_PROVIDER_ID}/${SystemVoiceIds.SYSTEM_TTS}",
    )

    val all: List<ModelEntry> = listOf(asrOnline, asrOffline, tts)

    /** Resolve a group member id to a virtual system entry, or null. */
    fun resolve(memberId: String): ModelEntry? = all.find { it.id == memberId }

    /**
     * True when [id] lives in the System voice namespace — the PREFIX test, so
     * it also covers the bare provider id and any `<sentinel>/<model>` member
     * iOS may write beyond the three entries above.
     */
    fun isSystemEntryId(id: String): Boolean =
        id.startsWith(SystemVoiceIds.BUILTIN_PROVIDER_ID)

    /**
     * [dv20-a1 A-02] Single membership judgement for MODEL GROUP member ids:
     * an id is known when an entry with that id is persisted in
     * `config.modelEntries`, OR when it is a synthesized System voice entry
     * ([isSystemEntryId]).
     *
     * Every reader of `memberEntryIds` defers to this one predicate — the
     * ConfigBackup Stage 2 remap, `GroupsCollection.requireKnownEntryIds`, the
     * `groups` config summary and the two debug group RPCs. Before it each
     * re-derived "known" from `modelEntries` alone, so the System sentinels
     * were "unknown" on all five faces (members silently dropped, or an
     * `Unknown model entry uuid` refusal) even though chat resolves them fine.
     *
     * Deliberately the prefix test rather than the three-entry enumeration:
     * the sentinels are never persisted, so a future System entry would
     * otherwise need the same five-site edit again. Fail-closed — an id that
     * is neither persisted nor System is still unknown.
     */
    fun isKnownGroupMemberId(memberId: String, persistedEntryIds: Set<String>): Boolean =
        memberId in persistedEntryIds || isSystemEntryId(memberId)

    /**
     * [deep-audit-1043 B-06] Resolve a group member id to the ModelEntry it
     * stands for: the persisted entry first, the synthesized System entry
     * second. This is the RESOLUTION sibling of [isKnownGroupMemberId] — the
     * same 「persisted-or-System」 decision, answered with the entry instead of a
     * boolean.
     *
     * The `entries.find { it.id == … } ?: resolve(…)` pair had been re-derived
     * at every reader of `ModelGroup.memberEntryIds`; the ones that forgot the
     * `?:` half silently resolved a System member to null, i.e. a default
     * Voice Input/Output group counted 0 usable members while its detail screen
     * listed them normally. Five such readers are fixed in the same commit —
     * this helper is what stops a sixth from re-deriving it differently.
     */
    fun resolveIn(persistedEntries: List<ModelEntry>, memberId: String): ModelEntry? =
        persistedEntries.find { it.id == memberId } ?: resolve(memberId)

    /**
     * [deep-audit-1043 B-06] Is this group member USABLE right now — the single
     * predicate behind 「No usable models」 and the display-side member counts.
     *
     * A System entry is always usable: it is served by the device (System
     * SpeechRecognitionEngine / TTS), needs no Provider instance and no API key,
     * so it must not be judged by [enabledInstanceIds] — the
     * `__builtin_system_speech__` instance is synthesized on demand and never
     * appears in `config.instances`. A persisted entry is usable when its
     * provider instance is enabled (an entry whose instance vanished is NOT —
     * it could not be instantiated).
     *
     * NOT the same question as `ProviderRepository.enabledMemberEntries`, which
     * answers 「which members can serve a CHAT turn」 and therefore excludes
     * System entries on purpose (they can never produce a ProviderInstance or
     * an API key). Use this one for counts/badges/labels; use that one for
     * routing. Conflating them is a regression in one direction or the other.
     */
    fun isEnabledGroupMember(
        persistedEntries: List<ModelEntry>,
        enabledInstanceIds: Set<String>,
        memberId: String,
    ): Boolean =
        if (isSystemEntryId(memberId)) true
        else resolveIn(persistedEntries, memberId)?.providerInstanceId in enabledInstanceIds

    /**
     * Synthetic instance for pickers that group entries by provider — gives
     * the virtual entries a "System" section header. Never persisted.
     */
    // Label resolved by the caller (Composable) via stringResource so the
    // built-in provider name localizes like iOS's String(localized: "System").
    fun syntheticInstance(label: String = "System") = ProviderInstance(
        id = SystemVoiceIds.BUILTIN_PROVIDER_ID,
        label = label,
        providerType = ProviderType.openAI,
        credentialType = ProviderCredential.apiKey,
        isEnabled = true,
    )
}

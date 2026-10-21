package probe

object VoiceSlice {
    fun isSystemEntryId(id: String): Boolean =
        id.startsWith(SystemVoiceIds.BUILTIN_PROVIDER_ID)

    fun isKnownGroupMemberId(memberId: String, persistedEntryIds: Set<String>): Boolean =
        memberId in persistedEntryIds
}

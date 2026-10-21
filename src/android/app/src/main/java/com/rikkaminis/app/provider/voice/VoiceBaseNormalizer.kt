package com.rikkaminis.app.provider.voice

/**
 * [deep-audit-1039 A-05] One place for the two base-URL normalizations the
 * voice family had as inline, asymmetric copies.
 *
 * The shape this replaces, and why each copy was wrong:
 *
 *  * **Azure** (`VoiceProviderFactory.build`): the strip ran *before* any
 *    trailing-slash trim, and in the order `endsWith("/cognitiveservices")`
 *    then `removeSuffix(...)`. A custom base pasted with a trailing slash
 *    therefore never matched, the segment survived, and the provider's
 *    endpoint path (`/cognitiveservices/v1`) appended a second one:
 *    `https://…tts.speech.microsoft.com/cognitiveservices/cognitiveservices/v1`
 *    — 100% request failure, no error anywhere naming the cause.
 *
 *  * **ElevenLabs / Deepgram** (`VoiceProviderVendors`): `if (!base.contains("/v1")) base += "/v1"`.
 *    `contains` answers "does this path mention /v1 anywhere", which is the
 *    wrong question — the segment has to be the **suffix** to make
 *    concatenation produce exactly one copy (the same intent
 *    [VoiceProvider.composedUrlString]'s KDoc states). A relay base like
 *    `https://relay.example.com/v1/elevenlabs` mentions `/v1` mid-path, so
 *    nothing was appended and the request landed on
 *    `https://relay.example.com/v1/elevenlabs/text-to-speech/…` (no version
 *    segment on the endpoint itself).
 *
 * Both fixes are pure string work with no Android/OkHttp dependency, so the
 * JVM unit tests exercise the real functions (not a re-implementation).
 *
 * ponytail: two functions, no shared "segment stripper" abstraction — the
 * loops differ by one suffix each and a parameterised stripper would cost
 * more to read than the duplication. | 天花板: a third vendor needing a
 * different version segment (/v2/v3 as standalone base suffixes) | 升级触发:
 * `grep -rn 'contains("/v' provider/voice/` returns a hit outside this file.
 */
internal object VoiceBaseNormalizer {

    /**
     * Collapse every trailing [version] occurrence in [raw] and restore exactly one.
     *
     * Mirrors `GeminiBasePath.normalize` (deep-audit-1024 ISSUE-02, same bug
     * family on the chat side): the loop rather than one strip pass, because a
     * single pass is not idempotent for shapes reachable in production — a
     * base the user pasted as `.../v1/v1` (or a relay that appended twice)
     * would otherwise keep one copy. Blank input falls back to [defaultBase],
     * the same rule the providers' `?:` defaults use.
     */
    fun normalizeVersionSegment(raw: String?, version: String = "/v1", defaultBase: String): String {
        val base = raw?.trim()?.trimEnd('/')?.takeIf { it.isNotEmpty() }
            ?: defaultBase.trimEnd('/')
        var stripped = base
        while (true) {
            val next = stripped.removeSuffix(version)
            if (next == stripped) break
            stripped = next
        }
        return "$stripped$version"
    }

    /**
     * Normalize a custom Azure TTS base to a bare host: trailing slashes do
     * not shield a pasted `/cognitiveservices` segment, and a base that
     * already carries the version segment (the user pasted the full endpoint)
     * is reduced to the host too, because
     * [com.rikkaminis.app.provider.voice.AzureTTSVoiceProvider]'s path is
     * always `/cognitiveservices/v1`.
     */
    fun normalizeAzureBase(raw: String?): String {
        val base = raw?.trim()?.trimEnd('/')?.takeIf { it.isNotEmpty() } ?: return ""
        var stripped = base
        while (true) {
            val next = stripped
                .removeSuffix("/cognitiveservices")
                .removeSuffix("/v1")
                .trimEnd('/')
            if (next == stripped) break
            stripped = next
        }
        return stripped
    }
}

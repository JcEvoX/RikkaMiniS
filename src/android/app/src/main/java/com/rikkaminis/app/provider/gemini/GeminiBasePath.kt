package com.rikkaminis.app.provider.gemini

/**
 * [deep-audit-1016 ISSUE-01] The Gemini request URL is assembled as
 * `<base>/models/<id>:generateContent`, i.e. **the base must carry the API version
 * segment** — the endpoint has no versionless form.
 *
 * Ground truth (probed from a non-blocked egress 2026-10-16, bogus key `x`):
 * | path | HTTP |
 * |---|---|
 * | `/models/gemini-2.0-flash:generateContent`       | 404 (empty body — path missing) |
 * | `/v1beta/models/gemini-2.0-flash:generateContent`| 400 "API key not valid" (path exists) |
 * | `/v1/models/gemini-2.0-flash:generateContent`    | 400 (path exists) |
 * | `/v1beta/v1/models/gemini-2.0-flash:generateContent` | 404 |
 * | `/v1beta/v1beta/models/gemini-2.0-flash:generateContent` | 404 |
 *
 * History: `bd68c66c` (2026-09-19, FIX-1) added the provider-side stripping for dev-history
 * **F-194** ("a base that already carries `/v1beta` produced `/v1beta/v1/models` = 404"), but
 * stripped the version segment *without restoring one* — so every Gemini instance, official or
 * relay, 404'd on both chat paths for ~27 days while the model list (which does restore it)
 * kept working. Root cause of the latency was **two implementations of one rule**: this file is
 * the extracted single implementation; [GeminiModelsApi.normalizeBase] still carries its own
 * copy and should delegate here next (see the FIX-A report, §超界申请).
 */
internal object GeminiBasePath {

    /**
     * The public endpoint *with* its version segment. Duplicated as a literal in
     * `ProviderFactory` / `AddProviderScreen` (out of this change's file set).
     */
    const val DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"

    /**
     * Collapses every trailing API-version segment in [raw] and restores exactly one `/v1beta`.
     *
     * The collapse **loops** rather than stripping once per suffix, because a single pass is not
     * idempotent for shapes that are reachable in production:
     * `appendV1Suffix` (default `true` on [com.rikkaminis.app.data.model.ProviderInstance], and the
     * default for backup/JSON imports) appends `/v1` to a custom base, so a Gemini instance whose
     * base is `.../v1beta` reaches this function as `.../v1beta/v1`. One pass leaves
     * `.../v1beta/v1beta` (→ 404, measured); the loop yields the working `.../v1beta`.
     *
     * Blank input falls back to [DEFAULT_BASE], matching the sibling implementation.
     */
    fun normalize(raw: String?): String {
        val base = raw?.trim()?.trimEnd('/')?.takeIf { it.isNotEmpty() } ?: return DEFAULT_BASE
        var stripped = base
        while (true) {
            val next = stripped.removeSuffix("/v1beta").removeSuffix("/v1")
            if (next == stripped) break
            stripped = next
        }
        return "$stripped/v1beta"
    }
}

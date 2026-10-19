package com.rikkaminis.app.data.usage

import org.json.JSONObject

/**
 * Pure-JVM aggregation over raw usage rows. No Android/Compose dependency —
 * unit-testable in the sandbox (org.json is available both on Android and via
 * the Maven artifact used by the JVM test classpath).
 */
data class UsageRow(
    val modelId: String,
    val tokenUsageJson: String,
    val createdAtMs: Long,
    val sessionId: String,
)

data class UsageFilter(val sinceMs: Long? = null, val untilMs: Long? = null)

data class AggregatedModelStats(
    val modelId: String,
    val inputTokens: Long,
    val outputTokens: Long,
    val cacheCreationTokens: Long,
    val cacheReadTokens: Long,
    val distinctDays: Set<String>,
    val distinctSessions: Set<String>,
) {
    val totalInput: Long get() = inputTokens + cacheReadTokens + cacheCreationTokens
}

/**
 * [da1033-I06] One (modelId, displayName, provider) triple as some layer reports it.
 *
 * [UsageAggregator.modelDisplayLookup] takes these instead of importing
 * `LLMModel` / `ModelEntry`, which keeps this file free of repo dependencies so the
 * sandbox unit tests can compile it on its own (same reason the class is pure JVM).
 */
data class ModelIdentity(
    val modelId: String,
    val displayName: String,
    val provider: String,
)

/** Provider group for usage rows whose model id no layer recognises. */
const val UNKNOWN_PROVIDER: String = "Unknown"

/**
 * Display order of the provider groups the usage screen knows about.
 *
 * [UNKNOWN_PROVIDER] is listed for documentation only — [providerGroupSortKey] pins it
 * to the END regardless of its index here, because it sorts malformed/unattributed rows
 * and must never push a real provider down the list.
 */
val KNOWN_PROVIDER_ORDER: List<String> = listOf(
    "OpenAI",
    "Anthropic",
    "Google Gemini",
    "Google",
    "Antigravity",
    UNKNOWN_PROVIDER,
)

object UsageAggregator {
    /**
     * Aggregate [rows] filtered by [filter]. Malformed JSON rows are skipped
     * (same tolerance as the current UI code). Time filtering is applied
     * before aggregation (filter-then-aggregate). Day bucketing uses the
     * injected [dayFormat]; production passes a device-local-timezone
     * formatter while tests pass a UTC one for determinism.
     */
    fun aggregate(
        rows: List<UsageRow>,
        filter: UsageFilter = UsageFilter(),
        dayFormat: (Long) -> String = { it.toString() }, // injectable for tests
    ): Map<String, AggregatedModelStats> {
        val since = filter.sinceMs
        val until = filter.untilMs

        val byModel = LinkedHashMap<String, MutableList<UsageRow>>()
        for (row in rows) {
            // [sinceMs <= createdAt < untilMs) — half-open window.
            if (since != null && row.createdAtMs < since) continue
            if (until != null && row.createdAtMs >= until) continue
            byModel.getOrPut(row.modelId) { mutableListOf() }.add(row)
        }

        val result = LinkedHashMap<String, AggregatedModelStats>()
        for ((modelId, modelRows) in byModel) {
            var input = 0L
            var output = 0L
            var cacheCr = 0L
            var cacheRd = 0L
            val days = LinkedHashSet<String>()
            val sessions = LinkedHashSet<String>()
            for (row in modelRows) {
                val usage = try { JSONObject(row.tokenUsageJson) } catch (_: Exception) { continue }
                input += usage.optLong("inputTokens", 0)
                output += usage.optLong("outputTokens", 0)
                // Legacy key fallbacks kept identical to the previous inline
                // UI parsing (Anthropic-style *InputTokens naming).
                cacheCr += usage.optLong("cacheCreationTokens", usage.optLong("cacheCreationInputTokens", 0))
                cacheRd += usage.optLong("cacheReadTokens", usage.optLong("cacheReadInputTokens", 0))
                days.add(dayFormat(row.createdAtMs))
                sessions.add(row.sessionId)
            }
            // [da1033-I06] Only emit a model that had at least ONE parseable row in the
            // window. The SQL fast path filters `json_valid(m.token_usage)` in its WHERE
            // clause before GROUP BY (ChatDao.usageStatsAggregated), so it never produced
            // an all-zero row for a model whose window is entirely malformed JSON — the
            // two paths must agree, and this one used to emit one.
            if (days.isEmpty()) continue
            result[modelId] = AggregatedModelStats(
                modelId = modelId,
                inputTokens = input,
                outputTokens = output,
                cacheCreationTokens = cacheCr,
                cacheReadTokens = cacheRd,
                distinctDays = days,
                distinctSessions = sessions,
            )
        }
        return result
    }

    /**
     * [da1033-I06] modelId → (displayName, provider) for the usage screen.
     *
     * [configured] (the LIVE provider config: user renames and the instance's own
     * provider display name) wins over [builtins] (the static catalog). Previously the
     * builtin catalog was written first and the config only filled the gaps, so:
     *   - renaming a catalog model never showed up in the stats screen, and
     *   - a catalog id kept the catalog's hardcoded provider ("Google" for the Gemini
     *     list, "Kimi" for the Kimi list) while the same instance's other models used
     *     `instance.providerType.displayName` ("Google Gemini" / "Kimi Code") — one
     *     instance rendered as two separate group cards.
     *
     * [builtins] is still consulted, but only for model ids the config does not know
     * (historical usage whose instance is gone). First match wins, so a model id held by
     * several instances resolves deterministically to the first one in config order.
     */
    fun modelDisplayLookup(
        configured: List<ModelIdentity>,
        builtins: List<ModelIdentity>,
    ): Map<String, Pair<String, String>> {
        val out = LinkedHashMap<String, Pair<String, String>>()
        for (m in configured) {
            // First match wins — see KDoc.
            if (!out.containsKey(m.modelId)) out[m.modelId] = m.displayName to m.provider
        }
        for (m in builtins) {
            if (!out.containsKey(m.modelId)) out[m.modelId] = m.displayName to m.provider
        }
        return out
    }

    /**
     * [da1033-I06] Sort key for a provider group name.
     *
     * Known providers keep [KNOWN_PROVIDER_ORDER]'s relative order; any other *real*
     * provider (xAI / OpenRouter / Kimi Code / …) sorts right after them; [UNKNOWN_PROVIDER]
     * is always LAST. The previous key gave every provider missing from the order the same
     * value as the end of the list (`providerOrder.size`) while `Unknown` sat at index 5,
     * so any legitimate provider outside the top five rendered BELOW the bucket that only
     * holds unattributed rows.
     */
    fun providerGroupSortKey(provider: String): Int {
        if (provider == UNKNOWN_PROVIDER) return Int.MAX_VALUE
        val idx = KNOWN_PROVIDER_ORDER.indexOf(provider)
        return if (idx >= 0) idx else KNOWN_PROVIDER_ORDER.size
    }
}
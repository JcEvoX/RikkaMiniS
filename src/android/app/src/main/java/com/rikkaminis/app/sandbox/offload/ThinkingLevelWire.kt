package com.rikkaminis.app.sandbox.offload

import com.rikkaminis.app.data.model.ThinkingLevel

/**
 * [dv27-A02] The ONE place that turns the wire value of `thinking_level`
 * into a [ThinkingLevel].
 *
 * Why this exists: `ModelExecutionDispatcher.buildRequestJson` writes
 * `thinking_level` for every request whose level is not OFF, but the worker
 * read it in only ONE of its two execution paths. The streaming path parsed
 * it; the non-streaming path (`executeRun`) never did, so the value fell back
 * to the `LLMProvider.sendMessage` default (`ThinkingLevel.OFF`) — every
 * non-streamed offloaded request ran with reasoning explicitly switched off,
 * on the DeepSeek / Qwen3 / Claude 3.7+ families where the level decides
 * whether the model reasons at all.
 *
 * The two paths used to carry their own opinions (one an inline `let`, the
 * other nothing). Two copies of the same decision drift — that is exactly
 * what happened. This function is the single source, and BOTH paths call it.
 *
 * Contract (unchanged from the streaming path it replaces):
 *  • ABSENT / empty string ⇒ [ThinkingLevel.OFF]. The dispatcher deliberately
 *    omits the key when the level is OFF ("don't serialize defaults"), and
 *    `optString` yields "" for a missing key. The strict enum parse
 *    ([T-model-exec-strict-enum]) treats "" as an UNKNOWN value and throws,
 *    so an empty string must never reach it — a thinking-OFF turn has to
 *    reach HTTP, not die as `unknown ThinkingLevel value: `.
 *  • A present-but-unparseable value (a newer enum case from a main-process
 *    only build) still throws [UnknownEnumValueException] — the strict
 *    cross-version contract is preserved, so the client respawns a worker
 *    that knows the new case.
 */
internal fun thinkingLevelFromWire(raw: String): ThinkingLevel {
    if (raw.isEmpty()) return ThinkingLevel.OFF
    return try {
        ThinkingLevel.valueOf(raw)
    } catch (_: IllegalArgumentException) {
        throw UnknownEnumValueException(ThinkingLevel::class.java.simpleName, raw)
    }
}

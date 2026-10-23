package com.rikkaminis.app.provider.thinking

import com.rikkaminis.app.data.model.ThinkingLevel
import org.json.JSONObject
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1047 A-01] The thinking-budget side of the `max_tokens` pair
 * contract, on the resolver's own terms.
 *
 * `thinking_budget` is defined RELATIVE to `max_tokens` — a budget that is not
 * strictly below the wire value is a deterministic 400. Two ways the pair broke,
 * both invisible to a test of either number alone:
 *
 *  1. the guard `maxTokens < 2 → drop the budget` sat inside
 *     `budget > 0 && maxTokens > 0`, so a non-positive value skipped the clamp
 *     altogether and the RAW budget was emitted;
 *  2. the provider wrote the CLAMPED `max_tokens` while handing the RAW request
 *     value to the injector, so the two readers disagreed exactly when the
 *     clamp fired (the provider-side half is pinned by
 *     `anthropic_thinking_budget_check.py`, criterion C-OAI-1).
 *
 * The expectations are hand-written literals (evidence-discipline): a budget is
 * legal when it is strictly below the max it is paired with, and a value with
 * no room carries no budget at all.
 */
class ThinkingBudgetPairTest {

    private fun budgetFor(maxTokens: Int): Int? {
        val body = JSONObject()
        ThinkingRuleResolver.apply(
            body,
            ThinkingResolveContext(
                modelId = "qwen3-32b",
                instanceId = null,
                supportsReasoning = true,
                declaredEffortValues = null,
                declaresNoEffortTiers = false,
                level = ThinkingLevel.HIGH,
                maxTokens = maxTokens,
                isOpenRouter = false,
                usesUnifiedReasoningEffort = false,
                isMistral = false,
                isDashScope = true, // QwenDual — the only built-in `thinking_budget` emitter
                isXAI = false,
                isOfficialDeepSeek = false,
                offEffort = null,
            ),
        )
        return if (body.has("thinking_budget")) body.getInt("thinking_budget") else null
    }

    @Test
    fun `a budget never reaches the max tokens it is paired with`() {
        val offenders = listOf(4096, 8192, 32768, 65536, 128_000).mapNotNull { maxTokens ->
            val budget = budgetFor(maxTokens)
            if (budget != null && budget >= maxTokens) "maxTokens=$maxTokens budget=$budget" else null
        }
        assertTrue(
            "thinking_budget must be strictly below max_tokens; offenders: $offenders",
            offenders.isEmpty(),
        )
    }

    @Test
    fun `a non-positive max tokens carries no budget at all`() {
        val offenders = (-8..0).mapNotNull { maxTokens ->
            val budget = budgetFor(maxTokens)
            if (budget != null && budget > 0) "maxTokens=$maxTokens budget=$budget" else null
        }
        assertTrue(
            "a non-positive max_tokens means 'no room', not 'skip the clamp'; offenders: $offenders",
            offenders.isEmpty(),
        )
    }

    @Test
    fun `a value with room still carries a budget`() {
        // Sanity: the guard is not deleting the field everywhere.
        val budget = budgetFor(65536)
        assertTrue(
            "a value with room must still carry a budget — got $budget",
            budget != null && budget > 0,
        )
    }

    @Test
    fun `the off level emits no budget`() {
        assertFalse(
            "OFF must not carry a thinking budget",
            budgetFor(65536, ThinkingLevel.OFF) != null,
        )
    }

    private fun budgetFor(maxTokens: Int, level: ThinkingLevel): Int? {
        val body = JSONObject()
        ThinkingRuleResolver.apply(
            body,
            ThinkingResolveContext(
                modelId = "qwen3-32b",
                instanceId = null,
                supportsReasoning = true,
                declaredEffortValues = null,
                declaresNoEffortTiers = false,
                level = level,
                maxTokens = maxTokens,
                isOpenRouter = false,
                usesUnifiedReasoningEffort = false,
                isMistral = false,
                isDashScope = true,
                isXAI = false,
                isOfficialDeepSeek = false,
                offEffort = null,
            ),
        )
        return if (body.has("thinking_budget")) body.getInt("thinking_budget") else null
    }
}
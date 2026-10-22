package com.rikkaminis.app.provider

import com.rikkaminis.app.data.model.LLMModel
import com.rikkaminis.app.data.model.ModelEntry
import com.rikkaminis.app.data.model.ModelOverrides
import com.rikkaminis.app.data.model.ThinkingLevel
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test

/**
 * [dv23-A04] The thinking-level CEILING must be resolved from the ENRICHED
 * model, not from the raw `entry.model`.
 *
 * WHY THIS EXISTS
 * ---------------
 * `ModelEntry.effectiveMaxThinkingLevel` used to read
 * `overrides.maxThinkingLevel ?: model.catalogMaxThinkingLevel`, where
 * `ModelEntry.model` is only `baseModel.withOverrides(overrides)` — a chain
 * that never consults the models.dev registry. Two consequences, both silent:
 *
 *  1. Every built-in row carries `reasoningEffortValues == null` and
 *     `supportsReasoning == null`, so the data-driven tier of
 *     `catalogMaxThinkingLevel` (`selectableThinkingLevels.lastOrNull()`)
 *     never fired for them, and the ceiling fell back to the id-heuristic.
 *  2. A custom/imported model whose models.dev row says `reasoning = true`
 *     resolved to `ThinkingLevel.OFF` — the chat picker's LOW/MEDIUM/HIGH/
 *     XHIGH options were all hidden while the settings badge (which DOES go
 *     through `ModelsDevApi.effectiveModel`) said "supports reasoning".
 *
 * The fix routes the ceiling through the single existing outlet
 * `ModelsDevApi.effectiveModel(this)`. These tests pin that DECISION.
 *
 * THE REGISTRY IS INJECTED, NOT MOCKED
 * ------------------------------------
 * `ModelsDevApi` keeps its registry in a private `cachedRegistry` field; the
 * test writes a synthetic registry into it by reflection and restores the
 * previous value afterwards. That exercises the REAL production resolution
 * (`enrichModel` → `applyDevData` → `effectiveEntryModel` → `withOverrides`)
 * instead of re-implementing it in the test.
 */
class ThinkingCeilingEnrichmentTest {

    private val registryField =
        ModelsDevApi::class.java.getDeclaredField("cachedRegistry").apply { isAccessible = true }
    private val timestampField =
        ModelsDevApi::class.java.getDeclaredField("cacheTimestamp").apply { isAccessible = true }

    private var previousRegistry: Any? = null
    private var previousTimestamp: Long = 0L

    @Before
    fun setUp() {
        previousRegistry = registryField.get(ModelsDevApi)
        previousTimestamp = timestampField.getLong(ModelsDevApi)
    }

    @After
    fun tearDown() {
        registryField.set(ModelsDevApi, previousRegistry)
        timestampField.setLong(ModelsDevApi, previousTimestamp)
    }

    /**
     * Install a synthetic models.dev registry.
     *
     * `providerId` is the registry key; `modelId` the row id. The row's
     * `reasoningEffortValues` / `reasoning` are what enrichment must pick up.
     */
    private fun installRegistry(
        providerId: String = "zhipuai",
        modelId: String = "glm-5.2",
        reasoning: Boolean? = true,
        effortValues: List<String>? = listOf("high", "max"),
        declaresNoEffortTiers: Boolean = false,
    ) {
        val entry = ModelsDevApi.ProviderEntry(
            id = providerId,
            name = providerId,
            api = "https://api.zhipuai.cn/v1",
            models = mapOf(
                modelId to ModelsDevApi.ModelDevEntry(
                    id = modelId,
                    name = modelId,
                    family = null,
                    contextWindow = 200_000,
                    maxOutputTokens = 32_000,
                    reasoning = reasoning,
                    interleavedField = null,
                    inputModalities = listOf("text"),
                    outputModalities = listOf("text"),
                    reasoningEffortValues = effortValues,
                    declaresNoEffortTiers = declaresNoEffortTiers,
                ),
            ),
        )
        registryField.set(ModelsDevApi, mapOf(providerId to entry))
        // Fresh timestamp so the in-memory cache is considered fresh.
        timestampField.setLong(ModelsDevApi, System.currentTimeMillis())
    }

    private fun entry(
        id: String = "glm-5.2",
        provider: String = "Zhipu AI",
        supportsReasoning: Boolean? = null,
        reasoningEffortValues: List<String>? = null,
        overrides: ModelOverrides = ModelOverrides(),
    ) = ModelEntry(
        providerInstanceId = "inst-1",
        baseModel = LLMModel(
            id = id,
            displayName = id,
            provider = provider,
            supportsReasoning = supportsReasoning,
            reasoningEffortValues = reasoningEffortValues,
        ),
        overrides = overrides,
    )

    // ------------------------------------------------------------------ cases

    /**
     * The headline case: the registry row declares the effort ladder
     * `["high","max"]`, so the ceiling is MAX — but the raw entry carries
     * nothing (no `reasoningEffortValues`, no `supportsReasoning`). Before
     * the fix this resolved to XHIGH (the id-heuristic default); the
     * data-driven tier was dead code for every built-in row.
     */
    @Test
    fun `ceiling follows the registry declared effort ladder`() {
        installRegistry(modelId = "glm-5.2", effortValues = listOf("high", "max"))
        val e = entry(id = "glm-5.2")
        assertEquals(
            "the registry ladder [high,max] must raise the ceiling to MAX",
            ThinkingLevel.MAX,
            e.effectiveMaxThinkingLevel,
        )
    }

    /**
     * A ladder that tops out BELOW the id-heuristic default must also be
     * honoured: the registry is a stronger statement than an id substring
     * rule, so `["low","medium"]` means HIGH, not XHIGH.
     */
    @Test
    fun `ceiling is capped by a registry ladder that tops out low`() {
        installRegistry(modelId = "some-model", effortValues = listOf("low", "medium"))
        val e = entry(id = "some-model")
        assertEquals(
            "a registry ladder of [low,medium] means MEDIUM (the top DECLARED tier)",
            ThinkingLevel.MEDIUM,
            e.effectiveMaxThinkingLevel,
        )
    }

    /**
     * The user's explicit override still wins over the registry.
     */
    @Test
    fun `explicit user override still wins over the registry`() {
        installRegistry(modelId = "glm-5.2", effortValues = listOf("high", "max"))
        val e = entry(
            id = "glm-5.2",
            overrides = ModelOverrides(maxThinkingLevel = ThinkingLevel.LOW),
        )
        assertEquals(
            "an explicit override is the highest-priority source",
            ThinkingLevel.LOW,
            e.effectiveMaxThinkingLevel,
        )
    }

    /**
     * An explicit `supportsReasoning = false` override must still switch the
     * ceiling OFF even though the registry says the model reasons. This is
     * the ordering contract of `effectiveEntryModel` (enrich the BASE model
     * first, apply the overrides second) — a fix that enriched the
     * already-overridden model would break it.
     */
    @Test
    fun `explicit supportsReasoning false still forces OFF`() {
        installRegistry(modelId = "glm-5.2", reasoning = true, effortValues = listOf("high", "max"))
        val e = entry(
            id = "glm-5.2",
            overrides = ModelOverrides(supportsReasoning = false),
        )
        assertEquals(
            "an explicit supportsReasoning=false must beat the registry's true",
            ThinkingLevel.OFF,
            e.effectiveMaxThinkingLevel,
        )
    }

    /**
     * A registry row that affirmatively declares NO effort tiers (the model
     * reasons but takes no `reasoning_effort` parameter) does NOT change the
     * CEILING — the ceiling is a picker bound, and the "no effort tiers"
     * declaration is enforced on the WIRE side (OpenAIProvider), not here.
     *
     * This test pins that boundary on purpose: the work order's mechanism
     * section claimed the ceiling returns OFF for such rows, but the real
     * tree has NO `declaresNoEffortTiers` branch in `catalogMaxThinkingLevel`
     * at all (verified on the baseline blob), and adding one would change
     * the picker for every model whose registry row lacks a `values` array —
     * a behaviour change beyond this ticket's radius.
     */
    @Test
    fun `registry row declaring no effort tiers does not change the ceiling`() {
        installRegistry(
            modelId = "no-effort-model",
            reasoning = true,
            effortValues = null,
            declaresNoEffortTiers = true,
        )
        val e = entry(id = "no-effort-model")
        assertEquals(
            "the ceiling is unaffected by the no-effort-tiers declaration",
            ThinkingLevel.XHIGH,
            e.effectiveMaxThinkingLevel,
        )
    }

    /**
     * A custom model that the registry does not know keeps the legacy
     * behaviour (no enrichment, id heuristic / default) — the fix must not
     * change the answer for models the registry has no row for.
     */
    @Test
    fun `model absent from the registry keeps the legacy resolution`() {
        installRegistry(modelId = "glm-5.2")
        val e = entry(id = "totally-unknown-model", supportsReasoning = true)
        assertEquals(
            "an unknown model keeps the XHIGH default",
            ThinkingLevel.XHIGH,
            e.effectiveMaxThinkingLevel,
        )
    }

    /**
     * NEGATIVE CONTROL — the defect shape itself.
     *
     * Reading the raw `model.catalogMaxThinkingLevel` (the pre-fix chain)
     * must NOT reproduce the enriched answer: with a registry ladder of
     * `["high","max"]` the raw read gives XHIGH while the enriched read
     * gives MAX. If this assertion ever passes, the ceiling stopped reading
     * the registry and the whole test class is measuring nothing.
     */
    @Test
    fun `the raw un-enriched read does NOT produce the enriched answer`() {
        installRegistry(modelId = "glm-5.2", effortValues = listOf("high", "max"))
        val e = entry(id = "glm-5.2")
        val raw = e.model.catalogMaxThinkingLevel
        assertEquals(
            "the raw entry.model read is the pre-fix chain and must differ",
            ThinkingLevel.XHIGH,
            raw,
        )
        assertEquals(
            "while the enriched ceiling is MAX",
            ThinkingLevel.MAX,
            e.effectiveMaxThinkingLevel,
        )
    }
}

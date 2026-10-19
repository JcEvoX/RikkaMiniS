package com.rikkaminis.app.ui.settings

import com.rikkaminis.app.data.model.ThinkingLevel
import com.rikkaminis.app.provider.thinking.AnthropicThinkingStyle
import com.rikkaminis.app.provider.thinking.ReasoningEchoPolicy
import com.rikkaminis.app.provider.thinking.ThinkingResolveContext
import com.rikkaminis.app.provider.thinking.ThinkingRule
import com.rikkaminis.app.provider.thinking.ThinkingRuleResolver
import com.rikkaminis.app.provider.thinking.ThinkingWireFormat
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1024-F] Regression tests for the pure save path behind
 * ThinkingRuleEditorDialog's Save button.
 *
 * The inline version had two defects (both reproduced against the pre-fix tree in
 * `probe/pre_proof.txt`):
 *   1. `buildRule()` used the four-argument positional `ThinkingRule(...)` constructor, so
 *      `reasoningEcho` (the vendor's history-echo contract — mistral NEVER,
 *      deepseek-v4-relay AFTER_TOOL_USE_ONLY) and `id` were dropped on EVERY save;
 *   2. the saved format came from the dropdown ALONE, so a rule whose format the dropdown
 *      cannot offer (DeepSeekRelay / QwenRootOnly / null) was rewritten to
 *      `ReasoningEffort` by a save that changed nothing — and that difference is not
 *      cosmetic: the OFF branch sends `thinking:{type:"disabled"}` (relay) or the portable
 *      `enable_thinking:false` (Qwen on a non-DashScope host) instead of omitting, and
 *      omitting an explicit disable leaves both families reasoning by default.
 *
 * Expected values are literal on purpose — deriving them from the production constants
 * would let a mutation masquerade as PASS.
 */
class ThinkingRuleEditorPreserveTest {

    @After
    fun tearDown() {
        // Never leak resolver cache state into other tests in the same JVM.
        ThinkingRuleResolver.setAllCustomRules(emptyMap())
    }

    // ── A. buildEditedThinkingRule: unexposed fields survive Save ──

    /** The shape [ThinkingRulesSection] seeds from a tapped built-in ("Copy of …"). */
    private fun seedFromBuiltIn(
        label: String = "deepseek-v4-relay",
        wireFormat: ThinkingWireFormat? = ThinkingWireFormat.DeepSeekRelay,
        echo: ReasoningEchoPolicy? = ReasoningEchoPolicy("reasoning_content", ReasoningEchoPolicy.Timing.AFTER_TOOL_USE_ONLY),
        id: String = "",
    ) = ThinkingRule(
        kind = ThinkingRule.Kind.CUSTOM,
        scope = ThinkingRule.Scope.ModelPattern("*deepseek-v4*"),
        wireFormat = wireFormat,
        reasoningEcho = echo,
        label = "Copy of $label",
        id = id,
    )

    @Test
    fun save_keepsReasoningEchoAndId() {
        val seed = seedFromBuiltIn(id = "abc")
        val saved = buildEditedThinkingRule(
            seed = seed,
            allModels = false,
            pattern = "*deepseek-v4*",
            wireFormat = ThinkingWireFormat.DeepSeekRelay,
            label = "  Copy of deepseek-v4-relay  ",
        )
        assertEquals(
            ReasoningEchoPolicy("reasoning_content", ReasoningEchoPolicy.Timing.AFTER_TOOL_USE_ONLY),
            saved.reasoningEcho,
        )
        assertEquals("abc", saved.id)
        assertEquals("Copy of deepseek-v4-relay", saved.label)
        assertEquals(ThinkingRule.Kind.CUSTOM, saved.kind)
    }

    @Test
    fun save_keepsNeverEchoPolicy() {
        val seed = seedFromBuiltIn(
            label = "mistral-official",
            wireFormat = ThinkingWireFormat.OmitEverything,
            echo = ReasoningEchoPolicy("reasoning_content", ReasoningEchoPolicy.Timing.NEVER),
        )
        val saved = buildEditedThinkingRule(seed, true, "", ThinkingWireFormat.OmitEverything, "mine")
        assertEquals(
            ReasoningEchoPolicy("reasoning_content", ReasoningEchoPolicy.Timing.NEVER),
            saved.reasoningEcho,
        )
        assertEquals("mine", saved.label)
    }

    @Test
    fun save_doesNotFabricateAnEchoPolicy() {
        // A rule with no echo opinion must stay opinion-free — inventing one would change
        // the vendor contract the rule was written to express.
        val seed = seedFromBuiltIn(echo = null)
        assertNull(buildEditedThinkingRule(seed, true, "", ThinkingWireFormat.ReasoningEffort(null), "x").reasoningEcho)
    }

    @Test
    fun save_movesTheScopeBetweenAllModelsAndPattern() {
        val seed = seedFromBuiltIn()
        val all = buildEditedThinkingRule(seed, true, "ignored", ThinkingWireFormat.DeepSeekRelay, "x")
        assertEquals(ThinkingRule.Scope.AllModels, all.scope)
        val patterned = buildEditedThinkingRule(seed, false, "  my-relay-*  ", ThinkingWireFormat.DeepSeekRelay, "x")
        assertEquals(ThinkingRule.Scope.ModelPattern("my-relay-*"), patterned.scope)
    }

    @Test
    fun save_passesTheChosenWireFormatThrough_verbatim() {
        val seed = seedFromBuiltIn()
        assertEquals(
            ThinkingWireFormat.OmitEverything,
            buildEditedThinkingRule(seed, true, "", ThinkingWireFormat.OmitEverything, "x").wireFormat,
        )
        // null is a real value ("no opinion"), not "leave the seed's format in place".
        assertNull(buildEditedThinkingRule(seed, true, "", null, "x").wireFormat)
    }

    // ── B. chooseEditedWireFormat: the dropdown may not rewrite what it cannot offer ──

    @Test
    fun untouchedSave_keepsRelayFormat() {
        val chosen = chooseEditedWireFormat(
            seedWire = ThinkingWireFormat.DeepSeekRelay,
            formatChanged = false,
            edited = ThinkingWireFormat.ReasoningEffort(null),
        )
        assertEquals(ThinkingWireFormat.DeepSeekRelay, chosen)
    }

    @Test
    fun untouchedSave_keepsQwenRootOnlyFormat() {
        val chosen = chooseEditedWireFormat(
            seedWire = ThinkingWireFormat.QwenRootOnly,
            formatChanged = false,
            edited = ThinkingWireFormat.ReasoningEffort(null),
        )
        assertEquals(ThinkingWireFormat.QwenRootOnly, chosen)
    }

    @Test
    fun untouchedSave_keepsNoOpinionAsNoOpinion() {
        // null means "fall through to the next resolution layer"; the dropdown has no way
        // to express that, so Save must not convert it into an effort opinion.
        assertNull(chooseEditedWireFormat(null, false, ThinkingWireFormat.ReasoningEffort(null)))
    }

    @Test
    fun changedDropdown_winsOverAnUnrepresentableSeed() {
        assertEquals(
            ThinkingWireFormat.ReasoningEffort("none"),
            chooseEditedWireFormat(ThinkingWireFormat.DeepSeekRelay, true, ThinkingWireFormat.ReasoningEffort("none")),
        )
    }

    @Test
    fun tweakingAPerFormatField_countsAsChangingTheFormat() {
        // The parameter fields belong to the dropdown's choice, not to a preserved seed:
        // honouring them (rather than keeping the seed) is what stops this fix from
        // swapping one silent discard for another.
        assertEquals(
            ThinkingWireFormat.ReasoningEffort("none"),
            chooseEditedWireFormat(ThinkingWireFormat.QwenRootOnly, true, ThinkingWireFormat.ReasoningEffort("none")),
        )
    }

    @Test
    fun representableSeed_usesWhatTheFieldsBuild() {
        // For a format the dropdown owns, the built value is authoritative — an untouched
        // save still round-trips, and an edited field is honoured.
        assertEquals(
            ThinkingWireFormat.ReasoningEffort(null),
            chooseEditedWireFormat(ThinkingWireFormat.ReasoningEffort(null), false, ThinkingWireFormat.ReasoningEffort(null)),
        )
        assertEquals(
            ThinkingWireFormat.ReasoningEffort("none"),
            chooseEditedWireFormat(ThinkingWireFormat.ReasoningEffort(null), false, ThinkingWireFormat.ReasoningEffort("none")),
        )
        assertEquals(
            ThinkingWireFormat.CustomPath("thinking.reasoning", mapOf(ThinkingLevel.HIGH to "max"), null),
            chooseEditedWireFormat(
                ThinkingWireFormat.CustomPath("thinking.reasoning", mapOf(ThinkingLevel.HIGH to "max"), null),
                false,
                ThinkingWireFormat.CustomPath("thinking.reasoning", mapOf(ThinkingLevel.HIGH to "max"), null),
            ),
        )
    }

    @Test
    fun representableSet_isExactlyTheDropdownsFormats() {
        val representable = listOf(
            ThinkingWireFormat.OmitEverything,
            ThinkingWireFormat.ReasoningEffort(null),
            ThinkingWireFormat.ReasoningEffortNested("low"),
            ThinkingWireFormat.BooleanToggle("thinking"),
            ThinkingWireFormat.ExtraBodyToggle("thinking.enabled"),
            ThinkingWireFormat.DeepSeekSibling,
            ThinkingWireFormat.QwenDual,
            ThinkingWireFormat.CustomPath("extra.thinking", emptyMap(), null),
        )
        for (fmt in representable) assertTrue("$fmt should be representable", isDropdownRepresentableWireFormat(fmt))

        val notRepresentable = listOf<ThinkingWireFormat?>(
            null,
            ThinkingWireFormat.DeepSeekRelay,
            ThinkingWireFormat.QwenRootOnly,
            ThinkingWireFormat.AnthropicThinking(AnthropicThinkingStyle.ADAPTIVE),
            ThinkingWireFormat.GeminiBudget(128, true),
            ThinkingWireFormat.GeminiThinkingLevel,
        )
        for (fmt in notRepresentable) {
            assertFalse("$fmt must not be treated as representable", isDropdownRepresentableWireFormat(fmt))
        }
    }

    // ── C. End-to-end: what the rule SAVED this way does on the wire ──

    private fun ctx(modelId: String, instanceId: String?, level: ThinkingLevel) = ThinkingResolveContext(
        modelId = modelId,
        instanceId = instanceId,
        supportsReasoning = true,
        declaredEffortValues = null,
        level = level,
        maxTokens = 8192,
        isOpenRouter = false,
        usesUnifiedReasoningEffort = false,
        isMistral = false,
        isDashScope = false,
        offEffort = null,
    )

    /** Save a tapped built-in the way the dialog does, then apply the saved rule. */
    private fun saveAndApply(
        seed: ThinkingRule,
        formatChanged: Boolean,
        edited: ThinkingWireFormat,
        level: ThinkingLevel,
    ): JSONObject {
        val saved = buildEditedThinkingRule(
            seed = seed,
            allModels = false,
            pattern = (seed.scope as ThinkingRule.Scope.ModelPattern).pattern,
            wireFormat = chooseEditedWireFormat(seed.wireFormat, formatChanged, edited),
            label = seed.label,
        )
        ThinkingRuleResolver.setCustomRules("inst-A", listOf(saved))
        val body = JSONObject()
        ThinkingRuleResolver.apply(body, ctx("deepseek-v4-flash", "inst-A", level))
        return body
    }

    @Test
    fun untouchedCopyOfRelayBuiltin_off_stillSendsTheExplicitDisable() {
        val body = saveAndApply(
            seed = seedFromBuiltIn(),
            formatChanged = false,
            edited = ThinkingWireFormat.ReasoningEffort(null),
            level = ThinkingLevel.OFF,
        )
        assertEquals("disabled", body.getJSONObject("thinking").optString("type"))
        assertFalse(body.has("reasoning_effort"))
    }

    @Test
    fun counterfactual_preFixRewrite_losesTheDisableOnOff() {
        // The pre-fix save shape, frozen as damage evidence: the dropdown value won, so a
        // rule that shipped an explicit disable no longer disables anything (deepseek-v4
        // thinks by default → the user's "Off" silently means "vendor default").
        val preFixSave = buildEditedThinkingRule(
            seed = seedFromBuiltIn(),
            allModels = false,
            pattern = "*deepseek-v4*",
            wireFormat = ThinkingWireFormat.ReasoningEffort(null), // ← what the old buildRule() produced
            label = "Copy of deepseek-v4-relay",
        )
        ThinkingRuleResolver.setCustomRules("inst-A", listOf(preFixSave))
        val body = JSONObject()
        ThinkingRuleResolver.apply(body, ctx("deepseek-v4-flash", "inst-A", ThinkingLevel.OFF))
        assertFalse(body.has("thinking"))
        assertFalse(body.has("reasoning_effort"))
    }

    @Test
    fun untouchedCopyOfQwenRootOnly_off_stillSendsThePortableToggle() {
        val seed = seedFromBuiltIn(
            label = "qwen-root-only",
            wireFormat = ThinkingWireFormat.QwenRootOnly,
            echo = null,
        ).copy(scope = ThinkingRule.Scope.ModelPattern("*qwen*"))
        val saved = buildEditedThinkingRule(
            seed = seed,
            allModels = false,
            pattern = "*qwen*",
            wireFormat = chooseEditedWireFormat(seed.wireFormat, false, ThinkingWireFormat.ReasoningEffort(null)),
            label = seed.label,
        )
        ThinkingRuleResolver.setCustomRules("inst-A", listOf(saved))
        val body = JSONObject()
        ThinkingRuleResolver.apply(body, ctx("qwen3.8-max", "inst-A", ThinkingLevel.OFF))
        assertEquals(false, body.opt("enable_thinking"))
    }

    @Test
    fun keptEchoPolicy_survivesAnEditOfAnUnrelatedField() {
        // The user narrowed the scope of a copied relay rule and renamed it; the echo
        // contract must still be readable for a model the built-in pattern does not cover.
        val saved = buildEditedThinkingRule(
            seed = seedFromBuiltIn(id = "abc"),
            allModels = false,
            pattern = "*my-relay*",
            wireFormat = chooseEditedWireFormat(ThinkingWireFormat.DeepSeekRelay, false, ThinkingWireFormat.ReasoningEffort(null)),
            label = "my relay",
        )
        ThinkingRuleResolver.setCustomRules("inst-A", listOf(saved))
        assertEquals(
            ReasoningEchoPolicy("reasoning_content", ReasoningEchoPolicy.Timing.AFTER_TOOL_USE_ONLY),
            ThinkingRuleResolver.echoPolicyFor(ctx("my-relay-v2", "inst-A", ThinkingLevel.AUTO)),
        )
    }

    @Test
    fun counterfactual_droppedEchoPolicy_isUnreachableAgain() {
        // Same edit, but with the echo the pre-fix constructor threw away.
        val saved = buildEditedThinkingRule(
            seed = seedFromBuiltIn().copy(reasoningEcho = null),
            allModels = false,
            pattern = "*my-relay*",
            wireFormat = chooseEditedWireFormat(ThinkingWireFormat.DeepSeekRelay, false, ThinkingWireFormat.ReasoningEffort(null)),
            label = "my relay",
        )
        ThinkingRuleResolver.setCustomRules("inst-A", listOf(saved))
        assertNull(ThinkingRuleResolver.echoPolicyFor(ctx("my-relay-v2", "inst-A", ThinkingLevel.AUTO)))
    }

    @Test
    fun changingTheFormatInTheDropdown_stillReachesTheWire() {
        val body = saveAndApply(
            seed = seedFromBuiltIn(),
            formatChanged = true,
            edited = ThinkingWireFormat.OmitEverything,
            level = ThinkingLevel.HIGH,
        )
        assertFalse(body.has("thinking"))
        assertFalse(body.has("reasoning_effort"))
    }

    // ── C. [da1033-I07] the dialog is seeded from the LIVE rule ──
    //
    // Source-shape assertions. The Compose screens cannot be compiled here (no Android
    // SDK), so the decision these two files encode — "Save must not write back the copy
    // the dialog opened with" — is pinned textually. The form-flexible half of the same
    // decision is graded by `provider_remember_key_check.py` C14 / C15; these literals
    // are the anchors, so they are expected values written out, not derived.

    /** The nine form states `provider_remember_key_check.py` C14 grades. */
    private val seedKeyedFormStates = listOf(
        "label", "allModels", "pattern", "choice", "formatChanged",
        "sendOffValue", "offValue", "path", "customValues",
    )

    @Test
    fun editorFormStates_keyOnTheSeedTheyRead() {
        val code = readRepoFile(EDITOR_REL)
        for (field in seedKeyedFormStates) {
            assertTrue(
                "`var $field` is seeded from `seed`; without the key an external write " +
                    "(minis-config / Debug RPC / backup restore) leaves the dialog on the " +
                    "copy it opened with, and Save writes that copy back over it",
                code.contains("var $field by remember(seed)"),
            )
        }
    }

    @Test
    fun editorTransientUiFlag_staysKeyless() {
        // Precision control: the criterion grades seed-derived form state, not
        // "every remember must carry a key".
        assertTrue(
            "the dropdown-open flag derives from nothing and must stay keyless",
            readRepoFile(EDITOR_REL).contains("var formatMenuOpen by remember {"),
        )
    }

    @Test
    fun editorDialog_isHandedTheLiveRule() {
        val code = readRepoFile(SECTION_REL)
        for (anchor in listOf(
            "val editorLiveIndex = editorRequest?.existingId?.let { customIds.indexOf(it) } ?: -1",
            "val liveSeed = if (req.existingId != null && !req.isNew && editorLiveIndex >= 0) {",
            "customRules[editorLiveIndex]",
            "request = req.copy(seed = liveSeed),",
            "if (req?.existingId != null && editorLiveIndex < 0) editorRequest = null",
        )) {
            assertTrue("call-site anchor missing: $anchor", code.contains(anchor))
        }
    }

    @Test
    fun editorNewRulePaths_stillSeedThemselves() {
        // "Add rule" and "Copy of ..." carry no existingId: the `!req.isNew` guard in the
        // resolution is what keeps them on their own seed instead of a lookup.
        val code = readRepoFile(SECTION_REL)
        assertTrue(code.contains("!req.isNew"))
        assertTrue(
            "both new-rule entry points must keep existingId = null",
            code.split("existingId = null").size - 1 >= 2,
        )
    }

    /**
     * Walk up from the test working directory to the repo root containing [relative].
     * Gradle sets user.dir to the module dir; a sandbox JVM run can start anywhere
     * under the repo — no fixed depth works.
     */
    private fun readRepoFile(relative: String): String {
        val cwd = System.getProperty("user.dir") ?: "."
        var dir: java.io.File? = java.io.File(cwd)
        while (dir != null) {
            val f = java.io.File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw java.io.FileNotFoundException("$relative not found from user.dir=$cwd")
    }

    private companion object {
        const val EDITOR_REL =
            "app/src/main/java/com/rikkaminis/app/ui/settings/ThinkingRuleEditor.kt"
        const val SECTION_REL =
            "app/src/main/java/com/rikkaminis/app/ui/settings/ThinkingRulesSection.kt"
    }
}

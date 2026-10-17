package com.rikkaminis.app.ui.settings

import com.rikkaminis.app.data.model.LLMModel
import com.rikkaminis.app.data.model.ModelOverrides
import com.rikkaminis.app.data.model.normalizeModalityName

/**
 * [da1023-B] Pure derivation of [ModelOverrides] for [ModelEntryDetailScreen]'s
 * Save action. Extracted out of the composable so the four defects the audit
 * found in the inline version are JVM-testable and cannot silently regress:
 *
 *  1. the screen has switches for image / pdf / audio / video input and
 *     image / audio output but NOT for `text` (both sides) or `video` output —
 *     the inline version rebuilt the lists from the switches alone, so
 *     `"text"` was dropped from every save and the comparison against the
 *     un-normalized base list reported "differs" even when the user touched
 *     nothing. A written override is non-null, and [com.rikkaminis.app.data.model.ModelEntry.model]
 *     replaces the base list wholesale when it is non-null — so a text-out
 *     model lost `"text"` out of `outputModalities`, flipping
 *     [LLMModel.isTextOutput] to false (dropped from the new-chat default
 *     candidate list, ProviderRepository) and [com.rikkaminis.app.sandbox.offload.ModelUseProtocol.isPureImageGenerator]
 *     to true (pinned to the Images endpoint).
 *  2. the comparison ran in a different space than the switch state:
 *     [ModelEntryDetailScreen] derives its switches from `normalizeModalityName()`-d
 *     lists, but the inline save compared against the RAW base list — a base
 *     holding `"image_input"` / `"text_output"` (OpenRouter / provider APIs)
 *     therefore always "differed" and forced a write on a no-op save.
 *  3. `ModelOverrides(...)` was constructed positionally with the six
 *     screen-bound fields, so the seventh field
 *     ([ModelOverrides.maxThinkingLevel], written by OpenMinis backup import /
 *     config restore / config RPC) defaulted to null — one Save wiped it.
 *     [ModelOverrides.copy] keeps every field this screen does not own.
 *  4. `null` means "inherit baseModel" (see [ModelOverrides]). A save that the
 *     user did not touch must keep both modality fields null so the entry keeps
 *     tracking later provider metadata updates.
 *
 * Semantics kept from the inline version (not a behaviour change): displayName /
 * maxOutputTokens / contextWindow / supportsReasoning are persisted only when
 * they diverge from the base model.
 */
internal fun buildModelEntryOverrides(
    baseModel: LLMModel,
    previous: ModelOverrides,
    displayName: String,
    maxOutputTokensText: String,
    contextWindowText: String,
    thinkingEnabled: Boolean,
    imageInput: Boolean,
    pdfInput: Boolean,
    audioInput: Boolean,
    videoInput: Boolean,
    imageOutput: Boolean,
    audioOutput: Boolean,
): ModelOverrides {
    val baseInputs = normalizeModalityList(baseModel.inputModalities)
    val baseOutputs = normalizeModalityList(baseModel.outputModalities)
    val newInputs = deriveModalityList(
        base = baseInputs,
        toggles = listOf(
            "image" to imageInput,
            "pdf" to pdfInput,
            "audio" to audioInput,
            "video" to videoInput,
        ),
    )
    val newOutputs = deriveModalityList(
        base = baseOutputs,
        toggles = listOf(
            "image" to imageOutput,
            "audio" to audioOutput,
        ),
    )
    return previous.copy(
        displayName = displayName.trim().takeIf { it.isNotEmpty() && it != baseModel.displayName },
        maxOutputTokens = maxOutputTokensText.trim().toIntOrNull()?.takeIf { it > 0 },
        contextWindow = contextWindowText.trim().toIntOrNull()?.takeIf { it > 0 },
        // supportsReasoning: persist only when user diverged from base.
        supportsReasoning = thinkingEnabled.takeIf { it != (baseModel.supportsReasoning ?: false) },
        // Modality lists: persist only when the user-edited set differs from
        // baseModel's set (both sides normalized — same space as the switches);
        // otherwise leave null so the entry tracks future provider updates.
        inputModalities = if (newInputs.toSet() != baseInputs.toSet()) newInputs else null,
        outputModalities = if (newOutputs.toSet() != baseOutputs.toSet()) newOutputs else null,
    )
}

/**
 * Rebuild one modality list from its switches plus base passthrough.
 *
 * The switches only own the dimensions the screen exposes as toggles
 * (`toggles`); every other dimension in `base` — `"text"` on both sides,
 * `"video"` on the output side, and anything a future catalog adds — is
 * carried over untouched. That makes "user flipped nothing" a no-op: the
 * derived set equals the (normalized) base set, so the caller writes `null`
 * and the entry keeps inheriting.
 *
 * `base` must already be normalized ([normalizeModalityList]) — both the
 * toggle init in the screen and the comparison here work in the bare-name
 * space (`"image"`, not `"image_input"`).
 *
 * ponytail: toggle order is fixed (image/pdf/audio/video in, image/audio out)
 * and passthrough follows base order — no sorting pass. | 天花板: if a future
 * surface needs canonical `modalityNamesInOrder` output (the `minis-config`
 * list surface re-orders on read anyway) this list order becomes visible.
 * | 升级触发: a consumer compares the persisted list order instead of the set.
 */
internal fun deriveModalityList(
    base: List<String>,
    toggles: List<Pair<String, Boolean>>,
): List<String> {
    val toggleNames = toggles.map { it.first }.toSet()
    val out = LinkedHashSet<String>()
    for ((name, on) in toggles) if (on) out.add(name)
    for (raw in base) {
        val name = raw.normalizeModalityName()
        if (name.isNotEmpty() && name !in toggleNames) out.add(name)
    }
    return out.toList()
}

/**
 * Bare-name, de-duplicated, junk-free form of a base modality list
 * (`["text_input", "image", "image"]` → `["text", "image"]`). Mirrors the
 * screen's own `effectiveInput` / `effectiveOutput` derivation so the toggles
 * and the "did the user change anything?" comparison share one space.
 */
internal fun normalizeModalityList(list: List<String>?): List<String> =
    (list ?: emptyList())
        .map { it.normalizeModalityName() }
        .filter { it.isNotEmpty() }
        .distinct()

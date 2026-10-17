package com.rikkaminis.app.ui.settings

import com.rikkaminis.app.provider.thinking.ThinkingRule
import com.rikkaminis.app.provider.thinking.ThinkingWireFormat

/**
 * [da1024-F] Pure save-path derivations for [ThinkingRuleEditorDialog]. Extracted out
 * of the composable so the two defects the 1024 audit found are JVM-testable and
 * cannot silently regress — the dialog itself needs Compose + `R`, which the sandbox
 * harness has no stubs for.
 *
 * Same family as [buildModelEntryOverrides] (da1023 ISSUE-02): a settings editor that
 * **rebuilds its subject from scratch** silently drops every field it does not
 * surface, and silently coerces every representation it does not model.
 *
 *  1. The inline `buildRule()` used the four-argument positional constructor
 *     `ThinkingRule(kind, scope, wireFormat, label)`, so `seed.reasoningEcho`
 *     ([ThinkingRule.reasoningEcho] — the vendor's history-echo contract, consumed by
 *     `ThinkingRuleResolver.echoPolicyFor` / `ReasoningEchoDecider`) and `seed.id` both
 *     defaulted away on EVERY save. A rule seeded from a built-in lost its echo
 *     declaration the moment the user renamed it.
 *  2. The dialog offered a dropdown of the Chat-Completions family and derived the saved
 *     format from the dropdown ALONE, through `FormatChoice.from`, whose `else` branch
 *     is `REASONING_EFFORT`. A rule whose format is not in the dropdown —
 *     [ThinkingWireFormat.DeepSeekRelay] / [ThinkingWireFormat.QwenRootOnly], the two
 *     shapes `ThinkingRuleResolver` generates for the deepseek-v4 relay and for Qwen on a
 *     non-DashScope relay, plus `null` ("no opinion") — was therefore rewritten into
 *     `reasoning_effort` by a save that changed nothing else. That is not a cosmetic
 *     relabelling: the OFF branch is what differs (`thinking:{type:"disabled"}` and the
 *     portable `enable_thinking:false`, versus "omit"), and omitting an explicit disable
 *     leaves both model families reasoning by default.
 *
 * The edit path keeps ONE contract: fields the dialog does not expose survive through
 * [ThinkingRule.copy], and a representation the dialog cannot offer survives unless the
 * user actively replaced it.
 */

/**
 * The rule the dialog's Save button writes, derived from the seed it opened with.
 *
 * `seed.copy(...)` is load-bearing, not stylistic: it carries every field this dialog
 * does not surface — [ThinkingRule.reasoningEcho] and [ThinkingRule.id] today — the same
 * way [buildModelEntryOverrides] carries `maxThinkingLevel`. Naming every argument for the
 * sake of explicitness would re-open the exact class of bug this function exists to close.
 */
internal fun buildEditedThinkingRule(
    seed: ThinkingRule,
    allModels: Boolean,
    pattern: String,
    wireFormat: ThinkingWireFormat?,
    label: String,
): ThinkingRule = seed.copy(
    kind = ThinkingRule.Kind.CUSTOM,
    scope = if (allModels) ThinkingRule.Scope.AllModels else ThinkingRule.Scope.ModelPattern(pattern.trim()),
    wireFormat = wireFormat,
    label = label.trim(),
)

/**
 * True when [fmt] round-trips through the editor's format dropdown: `FormatChoice.from`
 * lands on a concrete branch (not its `else` display fallback) and the per-format fields
 * are initialised from it, so rebuilding from the dropdown reproduces the same shape.
 *
 * `null` is deliberately NOT representable: "this rule expresses no opinion on the wire
 * shape" is a state the resolver treats as "fall through to the next layer"
 * ([ThinkingRule.wireFormat] KDoc), and the dialog has no way to express it — so an
 * untouched save must not turn it into an opinion.
 *
 * Kept in step with `FormatChoice.from` by hand; the regression gate
 * (`scripts/scan/thinking_rule_editor_preserve_check.py`) reads both as text and fails if
 * `from`'s concrete branches stop being a subset of this list — that drift is the root
 * cause of defect 2, so leaving it to review would let it come back.
 */
internal fun isDropdownRepresentableWireFormat(fmt: ThinkingWireFormat?): Boolean = when (fmt) {
    is ThinkingWireFormat.OmitEverything,
    is ThinkingWireFormat.ReasoningEffort,
    is ThinkingWireFormat.ReasoningEffortNested,
    is ThinkingWireFormat.BooleanToggle,
    is ThinkingWireFormat.ExtraBodyToggle,
    is ThinkingWireFormat.DeepSeekSibling,
    is ThinkingWireFormat.QwenDual,
    is ThinkingWireFormat.CustomPath -> true
    else -> false
}

/**
 * The wire format Save should persist: what the dropdown+fields currently build, or the
 * seed's own format when the dialog cannot represent it and the user has not touched the
 * format controls.
 *
 * [formatChanged] is "the user actively changed the format" — the dropdown selection OR
 * any of its per-format parameter fields (path / off value / per-tier values). Using that
 * flag in both places is what keeps this from trading one silent rewrite for another: for
 * a seed the dropdown cannot represent, editing its parameter fields would otherwise be
 * discarded on save (those fields belong to the dropdown's choice, not to the seed).
 */
internal fun chooseEditedWireFormat(
    seedWire: ThinkingWireFormat?,
    formatChanged: Boolean,
    edited: ThinkingWireFormat,
): ThinkingWireFormat? =
    if (formatChanged || isDropdownRepresentableWireFormat(seedWire)) edited else seedWire

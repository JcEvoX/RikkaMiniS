package com.rikkaminis.app.config.collections

import com.rikkaminis.app.data.model.ProviderType
// Pure predicate read (`this != openRouter`) — no debug entry point, no
// BuildConfig.DEBUG branch, no log line; R8 folds it into the call site, so the
// debug JSON-RPC surface is not pulled into the release APK. Same audited shape as
// ProvidersCollection.kt's exemption; the allow-list is untouched (shared gate).
// debug-ok: pure predicate read, no debug-only behaviour (see above)
import com.rikkaminis.app.debug.supportsCustomBaseURL

/**
 * Single-source validation and construction rules shared across
 * [ProvidersCollection], [ModelsCollection], and
 * [com.rikkaminis.app.debug.ProviderMutationMethods].
 *
 * Keeping these predicates pure and Android-free lets JVM unit tests verify
 * the write surfaces (UI, Debug RPC, minis-config) in one place.
 * [ARCH-01] (arch refactoring blueprint): this file is the home of the
 * wording + gate predicates that used to be copied per write surface —
 * the historical result was the 9-round "provider field parity" family
 * (GH#68 → pinned → 1019 … → 1026), where each new field shipped with a
 * hand-written copy per face and any missed copy became the next audit's
 * P1 ticket.
 *
 * Scope note: [ModelsCollection.add] keeps its inline model_id gate —
 * `models_provider_field_check` pins the `.trim()`-inside-the-statement
 * shape there and is outside this work order's gate set, so the shared
 * helper ([normalizeCustomModelId]) is wired into the Debug RPC face only.
 */

// ─── [ARCH-01] Shared customBaseURL type rule ──────────────────────────────

/**
 * [da1023-C] Custom base URL is honored by every provider arm in
 * ProviderFactory except openRouter, whose request builder pins
 * https://openrouter.ai/api/v1. The predicate itself is **single-sourced** in
 * `ProviderMutationMethods.supportsCustomBaseURL` (debug face) — this file
 * imports it rather than restating `this != openRouter` a second time.
 *
 * `CUSTOM_BASE_URL_OPENROUTER_MESSAGE` is the wording single source for BOTH
 * faces (mirroring `CUSTOM_USER_AGENT_OAUTH_MESSAGE`): the collection face
 * (`providers.<id>.customBaseURL` write field + the `add` payload path via
 * [resolveAddCustomBaseURL]) and the debug face
 * (`provider.instances.create/update` guards) all consult it instead of
 * keeping a copy each. The `provider_rpc_symmetry_check` gate pins that they
 * still do (C2/C5) — so the two faces cannot drift to different user-visible
 * errors silently.
 */
internal const val CUSTOM_BASE_URL_OPENROUTER_MESSAGE: String =
    "customBaseURL is not supported for providerType=openRouter"

/**
 * Returns `true` iff [customBaseURL] (already trimmed/non-blank) is allowed
 * to be written onto a provider of [providerType]. Null/empty clear writes
 * are always permitted so a stale value can be cleared — the collection
 * writer previously rejected even the clear, which made a phantom
 * openRouter `customBaseURL` (ignored at runtime, invisible in the UI)
 * permanently un-clearable via `minis-config`; the debug
 * `provider.instances.update` face already allowed the clear.
 */
internal fun isCustomBaseURLWriteAllowed(
    providerType: ProviderType,
    customBaseURL: String?,
): Boolean = customBaseURL.isNullOrBlank() || providerType.supportsCustomBaseURL

/**
 * [deep-audit-1039 A-04] Single-source normalization for the raw
 * `customBaseURL` input of EVERY write face: the collection field writer, the
 * `add` payload path ([resolveAddCustomBaseURL]), and the debug
 * `provider.instances.create/update` faces. Trims surrounding whitespace and
 * collapses the blank result to `null` (= clear).
 *
 * Before this helper the write sites spelled the normalization differently —
 * `ifEmpty { null }` (blank-but-not-empty survives) and
 * `takeIf { it.isNotBlank() }` (no trim at all) — so `"   "` passed the
 * [isCustomBaseURLWriteAllowed] guard on the openRouter arm (blank is a clear,
 * not a write) and was persisted verbatim, and `"  https://api.x  "` kept its
 * padding in the stored value. The read side
 * (`ProviderInstance.effectiveBaseURL`) appended `/v1` to the untrimmed
 * string, so a padded value produced a request URL like `"   /v1"` — every
 * request on that instance failed with no config-side error anywhere.
 *
 * Semantics: `null` / `""` / `"   "` all normalize to `null`; a padded URL
 * comes back trimmed. This does NOT change the guard's direction — blank
 * stays an allowed clear.
 */
internal fun normalizeCustomBaseURL(raw: String?): String? =
    raw?.trim()?.takeIf { it.isNotEmpty() }

// ─── [ARCH-01] Shared model-id / provider-name rules ──────────────────────

/**
 * Normalizes a raw `modelId` / `model_id` input by trimming surrounding
 * whitespace. Returns `null` when the resulting id is empty so callers can
 * surface their surface-appropriate error ([ConfigError.InvalidValue] or
 * [com.rikkaminis.app.debug.RPCException]). Shared by the minis-config face
 * ([ModelsCollection]) and the Debug RPC face
 * ([com.rikkaminis.app.debug.ProviderMutationMethods.modelsAdd]) so a
 * whitespace-only id cannot mint a blank/padded composite-key row on either
 * surface.
 */
internal fun normalizeCustomModelId(raw: String?): String? =
    raw?.trim()?.takeIf { it.isNotEmpty() }

/**
 * Resolves the canonical `LLMModel.provider` display label for a model owned
 * by an instance of [providerType]. Always uses [ProviderType.displayName] so
 * UI, Debug RPC, backup restore, and `minis-config` produce identical strings
 * for [com.rikkaminis.app.provider.ModelsDevApi] enrichment lookups — the
 * raw instance UUID or a short catalog name misses `providerKeyMap` and drops
 * the row into the order-dependent all-providers scan (the F-212 failure).
 */
internal fun canonicalModelProviderName(providerType: ProviderType): String =
    providerType.displayName

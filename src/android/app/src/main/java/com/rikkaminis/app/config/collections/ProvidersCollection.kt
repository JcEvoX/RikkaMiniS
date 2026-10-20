package com.rikkaminis.app.config.collections

import com.rikkaminis.app.config.ConfigCollection
import com.rikkaminis.app.config.ConfigError
import com.rikkaminis.app.config.ConfigField
import com.rikkaminis.app.config.ConfigRisk
import com.rikkaminis.app.config.ConfigSchema
import com.rikkaminis.app.config.ConfigValue
import com.rikkaminis.app.config.fields.ClosureField
import com.rikkaminis.app.config.fields.HiddenField
import com.rikkaminis.app.config.fields.ReadOnlyField
import com.rikkaminis.app.data.model.ProviderCredential
import com.rikkaminis.app.data.model.ImageEndpointMode
import com.rikkaminis.app.data.model.ProviderInstance
import com.rikkaminis.app.data.model.ProviderType
import com.rikkaminis.app.data.repository.EnvVarRepository
import com.rikkaminis.app.data.repository.ProviderRepository
// ISSUE-05 single-source predicate read (supportsCustomBaseURL): a pure internal
// extension property (`this != openRouter`) — no debug entry point, no
// BuildConfig.DEBUG branch, no log line. R8 folds it into the call site, so it
// does not pull the debug JSON-RPC surface into the release APK.
// The debug-leak allow-list was NOT touched (shared gate, COMMON §3): for a
// one-site cross-package read the in-line exemption is the audited form, and
// provider_rpc_symmetry_check C1 pins that this stays the ONLY definition.
// debug-ok: pure predicate read, no debug-only behaviour (see above)
import com.rikkaminis.app.debug.supportsCustomBaseURL

/**
 * Exposes `ProviderInstance` fields under `providers.<id>.…`. Mirrors
 * iOS `ProvidersCollection`.
 *
 * [T-minis-config-provider-add] Add is OPEN (per user decision: writes
 * are agent-permitted, reads of credentials remain guarded). Remove
 * stays denied — yanking a provider may break tool calls already in
 * flight, and the user's revertable choice is to disable it instead
 * (`providers.<id>.isEnabled = false`).
 *
 * Credential safety contract:
 *   - apiKey is a write-only ClosureField — writes accept either a
 *     literal value or a `$$ENV_VAR` reference (resolved at write time
 *     via [envVars]); reads throw permission_denied (forwarded by the
 *     bridge as `error: permission_denied`, NOT `read_failed`).
 *   - oauthToken stays HiddenField on every axis — OAuth tokens come
 *     from a multi-step browser flow that minis-config cannot drive.
 */
class ProvidersCollection(
    private val repo: ProviderRepository,
    private val envVars: EnvVarRepository,
) : ConfigCollection {
    override val basePath: String get() = "providers"
    override val displayName: String get() = "LLM provider instances"
    override val description: String get() =
        "Configured provider accounts (Anthropic, OpenAI, …). API keys may be written (literal or $\$ENV_VAR), but never read back."
    override val addable: Boolean get() = true
    override val removable: Boolean get() = false
    override val risk: ConfigRisk get() = ConfigRisk.SENSITIVE
    override val addPayloadSchema: ConfigSchema get() = ConfigSchema.Json

    override fun childIds(): List<String> = repo.instances.map { it.id }

    override fun fields(forId: String): List<ConfigField> {
        if (repo.instance(forId) == null) return emptyList()
        return listOf(
            providerType(forId),
            credentialType(forId),
            label(forId),
            enabled(forId),
            customBaseURL(forId),
            appendV1Suffix(forId),
            useResponsesAPI(forId),
            azureMode(forId),
            imageEndpointMode(forId),
            imageEndpointResolved(forId),
            customUserAgent(forId),
            apiKeyField(forId),
            // OAuth token stays fully hidden — the flow is browser-driven
            // and can't be expressed as a single value.
            HiddenField(
                path = "providers.$forId.oauthToken",
                displayName = "OAuth Token",
                description = "Hidden — completed via the in-app OAuth flow.",
                reason = "OAuth tokens cannot be set via minis-config — use Settings UI",
            ),
        )
    }

    /**
     * Create a new provider instance. JSON payload shape (mirrors iOS
     * Provider export envelope's `config` object so a single agent
     * surface covers both create-from-scratch and create-from-export):
     *
     *   {
     *     "providerType":     "openAI" | "anthropic" | "gemini" | ...,  // required
     *     "label":            "My Provider",                            // required, must be unique
     *     "credentialType":   "apiKey" | "oauth",                       // default apiKey
     *     "customBaseURL":    "https://api.x.ai",                       // optional
     *     "appendV1Suffix":   true,                                     // optional, default true
     *     "useResponsesAPI":  false,                                    // optional, default false
     *     "azureMode":        false,                                    // optional, default false (OpenAI api-key only)
     *     "customUserAgent":  "MyAgent/1.0",                            // optional, apiKey providers only
     *     "apiKey":           "sk-…" | "$\$DEEPSEEK_KEY",                 // optional; literal OR env ref
     *   }
     *
     * `apiKey` accepts either a literal string (stored verbatim in the
     * encrypted credential store) or a `$\$ENV_NAME` reference. The
     * reference is RESOLVED AT WRITE TIME (looked up against the
     * EnvVarRepository now); missing keys fail loudly with
     * invalid_value rather than waiting for a 401 mid-stream. Future
     * env-var changes do NOT re-update the stored apiKey — the env var
     * acts as a one-shot value source, not a live binding.
     */
    override fun add(payload: ConfigValue): String {
        val obj = (payload as? ConfigValue.Obj)?.value
            ?: throw ConfigError.InvalidValue("Expected JSON object")

        val providerTypeRaw = (obj["providerType"] as? ConfigValue.Str)?.value
            ?: throw ConfigError.InvalidValue("`providerType` required (one of: ${ProviderType.values().joinToString { it.name }})")
        val providerType = runCatching { ProviderType.valueOf(providerTypeRaw) }.getOrNull()
            ?: throw ConfigError.InvalidValue("Unknown providerType '$providerTypeRaw'")

        val label = (obj["label"] as? ConfigValue.Str)?.value?.trim().orEmpty()
        if (label.isEmpty()) throw ConfigError.InvalidValue("`label` required (non-empty)")
        if (repo.instances.any { it.label.equals(label, ignoreCase = true) }) {
            throw ConfigError.InvalidValue("A provider with label '$label' already exists")
        }

        val credentialType = (obj["credentialType"] as? ConfigValue.Str)?.value?.let { raw ->
            runCatching { ProviderCredential.valueOf(raw) }.getOrNull()
                ?: throw ConfigError.InvalidValue("Unknown credentialType '$raw' (allowed: apiKey, oauth)")
        } ?: ProviderCredential.apiKey

        // [deep-audit-1026 ISSUE-05] Same openRouter rule as the per-instance
        // write field and the debug `provider.add` — shared via
        // [resolveAddCustomBaseURL], not a second copy. Absent/blank ⇒ null
        // (no override), so a bare create keeps working for every type.
        val customBaseURL = resolveAddCustomBaseURL(
            (obj["customBaseURL"] as? ConfigValue.Str)?.value,
            providerType,
        )
        val appendV1Suffix = (obj["appendV1Suffix"] as? ConfigValue.Bool)?.value ?: true
        val useResponsesAPI = (obj["useResponsesAPI"] as? ConfigValue.Bool)?.value ?: false
        val azureMode = (obj["azureMode"] as? ConfigValue.Bool)?.value ?: false

        // [fix/dv16-a A-07] Optional image-endpoint fields — the same key set
        // the debug `provider.instances.create` accepts (gate C14 pins the two
        // write faces to the same keys). Absent = auto / no cached probe (the
        // current defaults). Same wire tokens as the per-instance field below;
        // imageEndpointResolved stays at its null default (a fresh instance
        // has no cached probe result).
        val imageEndpointMode = (obj["imageEndpointMode"] as? ConfigValue.Str)?.value?.let { raw ->
            imageEndpointModeFromToken(raw)
                ?: throw ConfigError.InvalidValue(
                    "imageEndpointMode must be auto | images_generations | chat_completions (got '$raw')"
                )
        } ?: ImageEndpointMode.auto

        // [fix/da1020-memory-gate-provider-fields] Same credential rule as the
        // per-instance write field — shared via [customUserAgentWritable], not
        // a second copy. A supplied-but-empty value is treated as absent
        // (matches the debug `provider.add` create path).
        val customUserAgent = resolveAddCustomUserAgent(
            (obj["customUserAgent"] as? ConfigValue.Str)?.value,
            credentialType,
        )

        // Resolve apiKey BEFORE addInstance, so a bad $$ENV ref aborts
        // the whole operation without leaving a half-created instance.
        val apiKeyValue = (obj["apiKey"] as? ConfigValue.Str)?.value?.takeIf { it.isNotEmpty() }
            ?.let { resolveCredential(it, "apiKey") }

        val instance = ProviderInstance(
            id = java.util.UUID.randomUUID().toString(),
            label = label,
            providerType = providerType,
            credentialType = credentialType,
            customBaseURL = customBaseURL,
            appendV1Suffix = appendV1Suffix,
            useResponsesAPI = useResponsesAPI,
            azureMode = azureMode,
            imageEndpointMode = imageEndpointMode,
            customUserAgent = customUserAgent,
        )
        repo.addInstance(instance)
        if (apiKeyValue != null) repo.saveApiKey(instance.id, apiKeyValue)
        return instance.id
    }

    override fun remove(id: String) {
        throw ConfigError.PermissionDenied(
            "Removing a provider may break later tool calls — disable it instead (`providers.$id.isEnabled = false`) or use the Settings UI"
        )
    }

    // -- Credential resolution ----------------------------------------
    //
    // `$$ENV_NAME` references are resolved at WRITE TIME against the
    // env var store. We deliberately don't intercept reads at provider
    // call time: doing so would mean rewriting 18+ `loadApiKey()` call
    // sites and lose the "credential was already validated when set"
    // invariant. A missing env var here surfaces immediately as an
    // invalid_value error, rather than as a 401 deep inside a stream.
    private val envRefRegex = Regex("^\\$\\$([A-Za-z_][A-Za-z0-9_]*)$")

    private fun resolveCredential(raw: String, fieldName: String): String {
        val m = envRefRegex.matchEntire(raw) ?: return raw
        val key = m.groupValues[1]
        val resolved = envVars.getValue(key)
            ?: throw ConfigError.InvalidValue(
                "$fieldName references env var \$\$$key, but no such env var exists. " +
                    "Create it first via [Set $key](minis://settings/environments?create_key=$key&create_value=)."
            )
        if (resolved.isEmpty()) {
            throw ConfigError.InvalidValue("$fieldName references env var \$\$$key but its value is empty.")
        }
        return resolved
    }

    // -- Field factories --

    private fun mutate(id: String, apply: (ProviderInstance) -> ProviderInstance) {
        val inst = repo.instance(id) ?: throw ConfigError.UnknownPath("providers.$id")
        repo.updateInstance(apply(inst))
    }

    private fun providerType(id: String): ConfigField =
        ReadOnlyField(
            path = "providers.$id.providerType",
            displayName = "Provider type",
            description = "Anthropic / OpenAI / Gemini / OpenRouter.",
            valueSchema = ConfigSchema.Str(),
            reader = {
                val inst = repo.instance(id) ?: return@ReadOnlyField ConfigValue.Null
                ConfigValue.Str(inst.providerType.name)
            },
        )

    private fun credentialType(id: String): ConfigField =
        ReadOnlyField(
            path = "providers.$id.credentialType",
            displayName = "Credential type",
            description = "API key vs OAuth.",
            valueSchema = ConfigSchema.Str(),
            reader = {
                val inst = repo.instance(id) ?: return@ReadOnlyField ConfigValue.Null
                ConfigValue.Str(inst.credentialType.name)
            },
        )

    private fun label(id: String): ConfigField =
        ClosureField(
            path = "providers.$id.label",
            displayName = "Label",
            description = "User-facing nickname for this instance.",
            valueSchema = ConfigSchema.Str(maxLength = 200),
            risk = ConfigRisk.NORMAL,
            revertable = true,
            reader = {
                val inst = repo.instance(id) ?: return@ClosureField ConfigValue.Null
                ConfigValue.Str(inst.label)
            },
            writer = { v ->
                val s = (v as? ConfigValue.Str)?.value ?: throw ConfigError.TypeMismatch("string")
                mutate(id) { it.copy(label = s) }
            },
        )

    private fun enabled(id: String): ConfigField =
        ClosureField(
            path = "providers.$id.isEnabled",
            displayName = "Enabled",
            description = "When false, the instance is excluded from agent calls.",
            valueSchema = ConfigSchema.Bool,
            risk = ConfigRisk.SENSITIVE,
            revertable = true,
            reader = {
                val inst = repo.instance(id) ?: return@ClosureField ConfigValue.Null
                ConfigValue.Bool(inst.isEnabled)
            },
            writer = { v ->
                val b = (v as? ConfigValue.Bool)?.value ?: throw ConfigError.TypeMismatch("bool")
                mutate(id) { it.copy(isEnabled = b) }
            },
        )

    private fun customBaseURL(id: String): ConfigField =
        ClosureField(
            path = "providers.$id.customBaseURL",
            displayName = "Custom base URL",
            description = "Override the default API endpoint. Empty string = use default.",
            valueSchema = ConfigSchema.Str(maxLength = 1000),
            risk = ConfigRisk.SENSITIVE,
            revertable = true,
            reader = {
                val inst = repo.instance(id) ?: return@ClosureField ConfigValue.Null
                ConfigValue.Str(inst.customBaseURL ?: "")
            },
            writer = { v ->
                val s = (v as? ConfigValue.Str)?.value ?: throw ConfigError.TypeMismatch("string")
                // [deep-audit-1026 ISSUE-05] openRouter's request builder pins
                // https://openrouter.ai/api/v1 and ignores this field entirely,
                // and the settings UI does not render it for openRouter — so a
                // write here used to be a *phantom* config: reported success,
                // took effect in the collection, changed nothing at runtime, and
                // could not be cleared from the UI. Same rule + wording as the
                // debug `provider.instances.create/update` guards.
                // [ARCH-01] The decision lives in [isCustomBaseURLWriteAllowed]
                // (ProviderWriteRules) so this face, the debug
                // `provider.instances.create/update` guards, and the `add`
                // payload path share one predicate. Blank is a clear, not a
                // write, so it stays allowed.
                val inst = repo.instance(id) ?: throw ConfigError.InvalidValue("Provider not found")
                val newBase = s.ifEmpty { null }
                if (!isCustomBaseURLWriteAllowed(inst.providerType, newBase)) {
                    throw ConfigError.InvalidValue(CUSTOM_BASE_URL_OPENROUTER_MESSAGE)
                }
                mutate(id) { it.copy(customBaseURL = newBase) }
            },
        )

    private fun appendV1Suffix(id: String): ConfigField =
        ClosureField(
            path = "providers.$id.appendV1Suffix",
            displayName = "Append /v1 suffix",
            description = "When false, the custom base URL is treated as already-versioned.",
            valueSchema = ConfigSchema.Bool,
            risk = ConfigRisk.NORMAL,
            revertable = true,
            reader = {
                val inst = repo.instance(id) ?: return@ClosureField ConfigValue.Null
                ConfigValue.Bool(inst.appendV1Suffix)
            },
            writer = { v ->
                val b = (v as? ConfigValue.Bool)?.value ?: throw ConfigError.TypeMismatch("bool")
                mutate(id) { it.copy(appendV1Suffix = b) }
            },
        )

    private fun useResponsesAPI(id: String): ConfigField =
        ClosureField(
            path = "providers.$id.useResponsesAPI",
            displayName = "Use OpenAI Responses API",
            description = "OpenAI-only: when true, traffic goes through /v1/responses instead of /v1/chat/completions.",
            valueSchema = ConfigSchema.Bool,
            risk = ConfigRisk.NORMAL,
            revertable = true,
            reader = {
                val inst = repo.instance(id) ?: return@ClosureField ConfigValue.Null
                ConfigValue.Bool(inst.useResponsesAPI)
            },
            writer = { v ->
                val b = (v as? ConfigValue.Bool)?.value ?: throw ConfigError.TypeMismatch("bool")
                mutate(id) { it.copy(useResponsesAPI = b) }
            },
        )

    // [T-android-azure-openai] Azure OpenAI mode. Mirrors useResponsesAPI above.
    private fun azureMode(id: String): ConfigField =
        ClosureField(
            path = "providers.$id.azureMode",
            displayName = "Azure OpenAI mode",
            description = "OpenAI API-key only: when true, auth uses the api-key header and the custom base URL is treated as an Azure endpoint ({endpoint}/openai/deployments/{model}/…?api-version=…).",
            valueSchema = ConfigSchema.Bool,
            risk = ConfigRisk.NORMAL,
            revertable = true,
            reader = {
                val inst = repo.instance(id) ?: return@ClosureField ConfigValue.Null
                ConfigValue.Bool(inst.azureMode)
            },
            writer = { v ->
                val b = (v as? ConfigValue.Bool)?.value ?: throw ConfigError.TypeMismatch("bool")
                mutate(id) { it.copy(azureMode = b) }
            },
        )

    // [fix/dv16-a A-07] Image endpoint mode. Mirrors azureMode above; the
    // write face accepts the same wire tokens as the debug
    // provider.instances.create/update (gate C14 pins the two write faces to
    // the same key set) and forcing a non-auto mode clears the cached probe
    // result — the same decision the update face and the picker UI make, so a
    // stale auto-probe cannot shadow the user's choice.
    private fun imageEndpointMode(id: String): ConfigField =
        ClosureField(
            path = "providers.$id.imageEndpointMode",
            displayName = "Image endpoint mode",
            description = "How image-generation requests are sent: auto (probe) / images_generations / chat_completions.",
            valueSchema = ConfigSchema.StrEnum(listOf("auto", "images_generations", "chat_completions")),
            risk = ConfigRisk.NORMAL,
            revertable = true,
            reader = {
                val inst = repo.instance(id) ?: return@ClosureField ConfigValue.Null
                ConfigValue.Str(imageEndpointModeToToken(inst.imageEndpointMode))
            },
            writer = { v ->
                val s = (v as? ConfigValue.Str)?.value ?: throw ConfigError.TypeMismatch("string")
                mutate(id) {
                    val mode = imageEndpointModeFromToken(s)
                        ?: throw ConfigError.InvalidValue(
                            "imageEndpointMode must be auto | images_generations | chat_completions (got '$s')"
                        )
                    // Forcing a non-auto mode clears the cached probe result
                    // (mirrors the update face / picker UI); auto keeps
                    // whatever is cached so the probe result survives.
                    it.copy(
                        imageEndpointMode = mode,
                        imageEndpointResolved = if (mode != ImageEndpointMode.auto) {
                            null
                        } else {
                            it.imageEndpointResolved
                        },
                    )
                }
            },
        )

    // [fix/dv16-a A-07] The cached image-endpoint probe result. Derived +
    // read-only: written by the auto-probe at request time and cleared when
    // the user forces a non-auto mode — never directly writable (same axis
    // as the other identity fields).
    private fun imageEndpointResolved(id: String): ConfigField =
        ReadOnlyField(
            path = "providers.$id.imageEndpointResolved",
            displayName = "Image endpoint (resolved)",
            description = "Cached probe result for auto mode; null until the first image request probes the endpoint.",
            valueSchema = ConfigSchema.Str(),
            reader = {
                val inst = repo.instance(id) ?: return@ReadOnlyField ConfigValue.Null
                inst.imageEndpointResolved
                    ?.let { ConfigValue.Str(imageEndpointModeToToken(it)) }
                    ?: ConfigValue.Null
            },
        )

    // [fix/dv16-a A-07] Wire tokens (`images_generations` / `chat_completions`)
    // match the iOS SerialName shape + the debug help text; the reader
    // round-trips identical strings so a read-back write is accepted.
    private fun imageEndpointModeToToken(mode: ImageEndpointMode): String = when (mode) {
        ImageEndpointMode.auto -> "auto"
        ImageEndpointMode.imagesGenerations -> "images_generations"
        ImageEndpointMode.chatCompletions -> "chat_completions"
    }

    private fun imageEndpointModeFromToken(token: String): ImageEndpointMode? = when (token) {
        "auto" -> ImageEndpointMode.auto
        "images_generations" -> ImageEndpointMode.imagesGenerations
        "chat_completions" -> ImageEndpointMode.chatCompletions
        else -> null
    }

    /**
     * [T-android-minisconfig-custom-useragent] Per-provider User-Agent
     * override. Empty string clears the override and falls back to the
     * branded default (Minis/<version> (Android <release>; <model>) — see
     * MinisUserAgent.DEFAULT) at request build time. Cloned from
     * [customBaseURL] in shape; the underlying field on ProviderInstance
     * has been wired through ProviderFactory + applyUserAgentOverride
     * since #802, so a write here takes effect on the very next outbound
     * call without any restart.
     */
    private fun customUserAgent(id: String): ConfigField =
        ClosureField(
            path = "providers.$id.customUserAgent",
            displayName = "Custom User-Agent",
            description = "Override the outbound User-Agent header. Empty string = use default.",
            valueSchema = ConfigSchema.Str(maxLength = 256),
            risk = ConfigRisk.NORMAL,
            revertable = true,
            reader = {
                val inst = repo.instance(id) ?: return@ClosureField ConfigValue.Null
                ConfigValue.Str(inst.customUserAgent ?: "")
            },
            writer = { v ->
                val s = (v as? ConfigValue.Str)?.value ?: throw ConfigError.TypeMismatch("string")
                // [T-ua-block-oauth-provider] Custom UA only applies to
                // third-party API-key providers. OAuth providers (Anthropic /
                // Codex) ride their own authentication request chain with a
                // fixed client UA — overriding it can break the handshake or be
                // rejected upstream. Block ALL writes (including clearing to "")
                // for non-apiKey credentials; reads stay unblocked.
                // [fix/da1020-memory-gate-provider-fields] The rule + wording
                // live in [customUserAgentWritable] /
                // [CUSTOM_USER_AGENT_OAUTH_MESSAGE] so `add` reuses them.
                val inst = repo.instance(id) ?: throw ConfigError.InvalidValue("Provider not found")
                if (!customUserAgentWritable(inst.credentialType)) {
                    throw ConfigError.InvalidValue(CUSTOM_USER_AGENT_OAUTH_MESSAGE)
                }
                mutate(id) { it.copy(customUserAgent = s.ifEmpty { null }) }
            },
        )

    /**
     * Write-only API key. Read always throws permission_denied (the
     * bridge surfaces this as `error: permission_denied`, not
     * `read_failed`, thanks to the explicit catch added in T-minis-
     * config-provider-add). Write accepts either a literal value or a
     * `$$ENV_NAME` reference resolved at write time against the env
     * var store; failed lookups throw invalid_value.
     */
    private fun apiKeyField(id: String): ConfigField =
        ClosureField(
            path = "providers.$id.apiKey",
            displayName = "API Key",
            description = "Write-only. Pass a literal value (sk-…) or `\$\$ENV_NAME` to copy from an env var. Reads are blocked.",
            valueSchema = ConfigSchema.Str(maxLength = 10_000),
            risk = ConfigRisk.DESTRUCTIVE,
            revertable = false,
            reader = {
                throw ConfigError.PermissionDenied(
                    "API keys are never read back via minis-config. To change one, write a new value."
                )
            },
            writer = { v ->
                val s = (v as? ConfigValue.Str)?.value ?: throw ConfigError.TypeMismatch("string")
                if (repo.instance(id) == null) throw ConfigError.UnknownPath("providers.$id")
                if (s.isEmpty()) {
                    // Empty string clears the stored credential. Useful for
                    // testing the "no key" failure path, or for transitioning
                    // an instance to OAuth.
                    repo.deleteApiKey(id)
                } else {
                    repo.saveApiKey(id, resolveCredential(s, "apiKey"))
                }
            },
        )
}

// ─── [fix/da1020-memory-gate-provider-fields] Shared UA credential rule ──

/**
 * [T-ua-block-oauth-provider] Custom User-Agent only applies to third-party
 * API-key providers. OAuth providers (Anthropic / Codex) ride their own
 * authentication request chain with a fixed client UA — overriding it can
 * break the handshake or be rejected upstream.
 *
 * Single source for the rule and the wording: both the
 * `providers.<id>.customUserAgent` write field and the `add` payload path
 * ([resolveAddCustomUserAgent]) consult this instead of keeping a copy each.
 * [ARCH-01] The debug `provider.add` / `provider.instances.update`
 * (ProviderMutationMethods) now delegate here too, so the minis-config and
 * Debug RPC faces cannot drift.
 */
internal const val CUSTOM_USER_AGENT_OAUTH_MESSAGE: String =
    "Custom User-Agent is only supported for third-party API-key providers. OAuth providers (Anthropic/Codex) use their own authentication UA and cannot be overridden."

internal fun customUserAgentWritable(credentialType: ProviderCredential): Boolean =
    credentialType == ProviderCredential.apiKey

/**
 * Pure slice of [ProvidersCollection.add]: resolve the optional
 * `customUserAgent` payload entry into the value stored on the new
 * [com.rikkaminis.app.data.model.ProviderInstance], applying the same
 * credential rule as the `providers.<id>.customUserAgent` write field.
 * Absent/blank ⇒ null (no override). A supplied UA on a non-apiKey
 * credential throws [ConfigError.InvalidValue] — mirrors the debug
 * `provider.add` gating, so a bare OAuth create without a UA keeps working.
 */
internal fun resolveAddCustomUserAgent(
    raw: String?,
    credentialType: ProviderCredential,
): String? {
    val ua = raw?.takeIf { it.isNotBlank() } ?: return null
    if (!customUserAgentWritable(credentialType)) {
        throw ConfigError.InvalidValue(CUSTOM_USER_AGENT_OAUTH_MESSAGE)
    }
    return ua
}

// ─── [deep-audit-1026 ISSUE-05] Shared customBaseURL add-path rule ────────

/**
 * Pure slice of [ProvidersCollection.add]: resolve the optional
 * `customBaseURL` payload entry into the value stored on the new
 * [com.rikkaminis.app.data.model.ProviderInstance], applying the same
 * openRouter rule as the `providers.<id>.customBaseURL` write field.
 * Absent/blank ⇒ null (no override, and no throw — a blank value is a read of
 * "no custom base", exactly as the debug `provider.instances.update` treats an
 * empty/JSON-null patch). A supplied base URL on a type that ignores it throws
 * [ConfigError.InvalidValue] — mirrors the debug `provider.add` gating, so a
 * bare openRouter create without a base URL keeps working.
 *
 * The predicate (`isCustomBaseURLWriteAllowed`) and the wording
 * (`CUSTOM_BASE_URL_OPENROUTER_MESSAGE`) are single-sourced in
 * `ProviderWriteRules.kt` [ARCH-01]; this file consults them instead of
 * keeping a copy.
 */
internal fun resolveAddCustomBaseURL(
    raw: String?,
    providerType: ProviderType,
): String? {
    val base = raw?.takeIf { it.isNotBlank() } ?: return null
    if (!providerType.supportsCustomBaseURL) {
        throw ConfigError.InvalidValue(CUSTOM_BASE_URL_OPENROUTER_MESSAGE)
    }
    return base
}

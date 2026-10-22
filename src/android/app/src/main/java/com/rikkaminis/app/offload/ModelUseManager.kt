package com.rikkaminis.app.offload

import com.rikkaminis.app.data.model.ModelEntry
import com.rikkaminis.app.data.model.ProviderInstance
import com.rikkaminis.app.data.model.directionalModalities
import org.json.JSONArray
import org.json.JSONObject

/**
 * Provides model listing and search capabilities for the model_use tool.
 * Only surfaces the Agent-Loop-visible subset (mirrors iOS
 * ProviderConfigStore.resolvedAgentLoopEntries). Models not exposed in
 * Settings > Model Groups > "Available Models in Agent Loop" are hidden.
 *
 * Output shape mirrors iOS `ModelUseOffloadBridge.entryDict` so the agent
 * sees the same fields on both platforms — most importantly `modalities`,
 * which it needs to pick an image-generation / TTS model.
 */
object ModelUseManager {

    private const val NO_MODELS_HINT =
        "No models available. Go to Settings > Model Groups > Available Models in Agent Loop to expose models to the agent."

    /**
     * List the agent-loop-visible models, optionally filtered by a free-text
     * query that matches against model id, display name, or provider.
     */
    fun listModels(
        entries: List<ModelEntry>,
        instances: Map<String, ProviderInstance> = emptyMap(),
        filter: String? = null,
    ): String {
        val filtered = if (filter.isNullOrBlank()) entries else {
            val q = filter.lowercase()
            entries.filter {
                it.model.id.lowercase().contains(q) ||
                    it.model.displayName.lowercase().contains(q) ||
                    it.model.provider.lowercase().contains(q)
            }
        }

        if (filtered.isEmpty()) {
            return if (filter != null) "No models matching '$filter'."
            else NO_MODELS_HINT
        }

        val result = JSONArray()
        for (entry in filtered) {
            result.put(entryDict(entry, instances[entry.providerInstanceId]))
        }
        return result.toString(2)
    }

    /**
     * Emit the per-entry JSON. Field names match iOS exactly:
     * entry_id, model_id, display_name, provider, instance_label,
     * provider_type, modalities, context_window.
     */
    fun entryDict(entry: ModelEntry, instance: ProviderInstance?): JSONObject {
        // Enrich at read time — entries persisted before the modalities field
        // existed won't have inputModalities/outputModalities populated, so
        // look them up from the bundled models.dev registry on the fly.
        //
        // [dv21-A05] …but the registry is a DEFAULT source, not a user input.
        // `effectiveModel` enriches the BASE model first and applies the user's
        // overrides second; enriching the merged model instead re-imposed the
        // registry's modalities / context window on top of an explicit override.
        val effectiveModel = com.rikkaminis.app.provider.ModelsDevApi.effectiveModel(entry)
        val modalities = JSONArray()
        // One canonical expansion, shared with the offload modality filter and
        // the config collection (LLMModel.directionalModalities) — the fallback
        // for an absent list is per direction, so a model with
        // outputModalities=["image"] never gains a "text_output".
        for (m in effectiveModel.directionalModalities) modalities.put(m)

        return JSONObject().apply {
            put("entry_id", entry.id)
            put("model_id", entry.model.id)
            put("display_name", entry.model.displayName)
            put("provider", entry.model.provider)
            put("instance_label", instance?.label ?: "unknown")
            put("provider_type", instance?.providerType?.displayName ?: "unknown")
            put("modalities", modalities)
            effectiveModel.contextWindow?.let { put("context_window", it) }
        }
    }
}

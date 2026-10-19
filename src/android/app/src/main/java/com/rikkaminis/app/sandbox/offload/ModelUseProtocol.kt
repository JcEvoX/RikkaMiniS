package com.rikkaminis.app.sandbox.offload

import com.rikkaminis.app.data.model.ImageEndpointMode
import com.rikkaminis.app.data.model.ProviderCredential
import com.rikkaminis.app.data.model.ProviderType
import com.rikkaminis.app.provider.safeOptString
import org.json.JSONArray
import org.json.JSONObject

/**
 * [fix/audit3-modeluse-1015] Canonical `minis-model-use` protocol helpers,
 * shared by BOTH processes that implement `run`:
 *
 *   - `:app`          [ModelUseOffloadHandler]  — CLI parsing, result shaping, file IO
 *   - `:modelservice` [ModelExecutionService]   — provider construction + the network call
 *
 * ## Why this file exists
 *
 * The two processes used to carry *mirrored copies* of the same parsers
 * (`parseChatExtraBody`, `parseImageGenConfig`, the passthrough envelope, the
 * image-route decision). The copies had already drifted, and the drift is
 * invisible to review: audit3 F-04 found `:modelservice` rebuilding a
 * `JSONArray` element-by-element from an integer loop index (so
 * `extra_body:{"stop":["a","b"]}` went out as `[0,1]`) and dropping nested
 * `JSONObject`s — while `:app` had always forwarded the raw org.json node.
 * A mirrored dialect cannot be kept in sync by discipline; it needs one
 * implementation and two callers.
 *
 * Everything in this file is **pure JVM**: no Android types, no Context, no
 * process state, no logging. [ModelUseProtocolTest] drives it directly, which
 * is also what makes the two-callers-one-implementation claim mechanical: the
 * same function object serves both processes, so there is no second dialect to
 * compare against.
 */
internal object ModelUseProtocol {

    /** Human-readable JSON type name for warning messages. Mirrors iOS jsonTypeName. */
    fun jsonTypeName(v: Any?): String = when (v) {
        null, JSONObject.NULL -> "null"
        is String -> "string"
        is Boolean -> "bool"
        is Number -> "number"
        is JSONArray -> "array"
        is JSONObject -> "object"
        else -> v.javaClass.simpleName
    }

    /**
     * Standard-mode chat `extra_body`: the EXPLICIT envelope only (implicit
     * unknown top-level keys stay image-path-only, per iOS). Values are kept as
     * raw org.json values so the provider's `body.put(k, v)` re-serializes them
     * faithfully — including nested arrays/objects, which is what
     * `extra_body` exists for (provider-specific knobs our schema does not
     * model, e.g. `{"stop":["x"],"response_format":{"type":"json_object"}}`).
     *
     * [fix/audit3-1015 F-04] The worker copy of this parser used to run the
     * array branch through `(0 until v.length()).mapNotNull { e -> e.let { if
     * (it is Int || …) it else null } }` — the lambda parameter was the loop
     * *index*, not the element, so the `is Int` test was unconditionally true
     * and every array was rewritten as `[0, 1, … N-1]`. Nested objects were not
     * handled at all and fell into the `else` warning branch. Both are gone:
     * this is the in-process dialect, verbatim.
     */
    fun parseChatExtraBody(inputJson: String, warnings: MutableList<String>): Map<String, Any?> {
        val obj = try {
            val t = inputJson.trim()
            if (t.startsWith("{")) JSONObject(t) else null
        } catch (_: Throwable) { null } ?: return emptyMap()
        val raw = obj.opt("extra_body")?.takeIf { it != JSONObject.NULL } ?: return emptyMap()
        val extra = raw as? JSONObject
        if (extra == null) {
            // [T-model-use-passthrough-warnings] type-mismatch feedback
            warnings.add(
                "extra_body is not a JSON object (actual type: ${jsonTypeName(raw)}) — ignored. " +
                    "Wrap your fields in an object: \"extra_body\":{...}.",
            )
            return emptyMap()
        }
        val out = LinkedHashMap<String, Any?>()
        for (key in extra.keys()) {
            val v = extra.opt(key)
            when {
                // Scalars, arrays and objects all travel VERBATIM as org.json
                // nodes; JSONObject.put(index, value) does the serialization.
                v is Number || v is Boolean || v is String -> out[key] = v
                v is JSONArray || v is JSONObject -> out[key] = v
                v === JSONObject.NULL -> out[key] = JSONObject.NULL
                // Unreachable for any value org.json can produce — kept as a
                // warning rather than a silent drop so a future JSON impl that
                // hands us something else is visible instead of lossy.
                else -> warnings.add("extra_body.$key: unsupported type (${jsonTypeName(v)}), dropped")
            }
        }
        return out
    }

    /**
     * Standard-mode `extra_headers` (a string→string map added to the request).
     *
     * [da1019 ISSUE-01] The last two envelope parsers that still had a hand-
     * written copy in EACH process. They had already drifted, in both
     * directions:
     *
     *  - `:modelservice` read with `obj.optJSONObject("extra_headers")`, so a
     *    present-but-not-an-object value (e.g. `["Authorization: ..."]`) was
     *    dropped with **zero** warning; `:app` warned. Its KDoc nevertheless
     *    claimed "both routes now drop + warn identically".
     *  - `:modelservice` read only `endpoint` (never `image_endpoint`) and
     *    never checked the `/` prefix, so `{"endpoint":"chat"}` — a SYMBOLIC
     *    endpoint-mode value that [parseImageGenConfig] consumes as a mode —
     *    was written verbatim into `provider.absoluteEndpointOverride` and
     *    echoed back in `applied_extras.custom_endpoint`.
     *
     * The strict (and correct) dialect is the in-process one, which is what
     * this function and [parseCustomEndpointPath] now are: one implementation,
     * two callers. Same rule as [parseChatExtraBody].
     *
     * Values must be JSON strings: `optString` would stringify a number or
     * boolean, so a typo'd header (`{"x-retry": 3}`) would look like it had
     * been sent. Non-string values are dropped **with** a warning instead.
     */
    fun parseExtraHeaders(inputJson: String, warnings: MutableList<String>): Map<String, String> {
        val obj = try {
            val t = inputJson.trim()
            if (t.startsWith("{")) JSONObject(t) else null
        } catch (_: Throwable) { null } ?: return emptyMap()
        val raw = obj.opt("extra_headers")?.takeIf { it != JSONObject.NULL } ?: return emptyMap()
        val hdrs = raw as? JSONObject
        if (hdrs == null) {
            warnings.add(
                "extra_headers is not a JSON object (actual type: ${jsonTypeName(raw)}) — " +
                    "all custom headers were ignored.",
            )
            return emptyMap()
        }
        val out = LinkedHashMap<String, String>()
        for (key in hdrs.keys()) {
            val v = hdrs.opt(key)
            if (v is String) {
                out[key] = v
            } else {
                warnings.add(
                    "extra_headers.$key value is not a string (actual type: ${jsonTypeName(v)}) — " +
                        "this header was ignored. Header values must be JSON strings.",
                )
            }
        }
        return out
    }

    /**
     * Parse a CUSTOM absolute endpoint path from top-level `image_endpoint` /
     * `endpoint` values (the former wins). A value starting with "/" is an
     * absolute path replacing the entire URL path after scheme+host; anything
     * else — symbolic values (`auto` / `chat` / `images-gen`, which are
     * endpoint MODES consumed by [parseImageGenConfig], not paths) and JSON
     * null — returns null.
     *
     * [da1019 ISSUE-01] `:modelservice` used to accept any non-empty
     * `endpoint` string here, so `{"endpoint":"chat"}` reached
     * `provider.absoluteEndpointOverride` and the reply's
     * `applied_extras.custom_endpoint`, while an `image_endpoint` path was
     * dropped entirely (the `:app` log line saying otherwise was the only
     * side that had resolved it).
     *
     * Note: on Android these arrive inside the input JSON (not as translated
     * argv), so unlike iOS there is no rootfs-dataPath prefix to strip — the
     * value is used verbatim.
     */
    fun parseCustomEndpointPath(inputJson: String): String? {
        val obj = try {
            val t = inputJson.trim()
            if (t.startsWith("{")) JSONObject(t) else null
        } catch (_: Throwable) { null } ?: return null
        val raw = obj.safeOptString("image_endpoint", "").ifEmpty { obj.safeOptString("endpoint", "") }
            .trim()
        return if (raw.startsWith("/")) raw else null
    }

    // ── provider shape (which implementation a type gets) ────────────────────
    //
    // [fix/da1015-modeluse-proc] `:app` used to answer "is this an
    // OpenAI-compatible instance?" by BUILDING a provider and type-testing it
    // (`provider is OpenAIProvider`), which cost a KeyRoulette draw plus a
    // TLS warm-up per CLI call — side effects this process has no use for,
    // because the request is sent by `:modelservice`, which builds its own
    // provider. The predicate below answers the same question from the one
    // input ProviderFactory uses to pick the class, with no construction.
    //
    // It is deliberately EXHAUSTIVE (no `else`): adding a ProviderType is then
    // a compile error here, forcing the new type's provider class to be
    // considered instead of silently inheriting an answer.
    //
    // `kimiCode` is included: ProviderFactory builds an OpenAIProvider for it
    // (api.kimi.com/coding/v1 is an OpenAI-compatible upstream), so
    // `input_audio`, `extra_body` and the raw passthrough mode all behave the
    // way they do on the other OpenAI-family types.

    /**
     * Does the provider implementation built for [type] speak the OpenAI wire
     * protocol (`OpenAIProvider`)? Mirrors `ProviderFactory.create`'s
     * type→class mapping exactly; the six arms are pinned by
     * [ModelUseProtocolTest] and cross-checked against the factory's source by
     * the FIX-E probe (`probe/check_factory_mapping.py`), so a new mapping
     * cannot drift away from this predicate unnoticed.
     */
    fun isOpenAiCompatibleProviderType(type: ProviderType): Boolean = when (type) {
        ProviderType.openAI, ProviderType.openRouter, ProviderType.xAI, ProviderType.kimiCode -> true
        ProviderType.anthropic, ProviderType.gemini -> false
    }

    // ── call feedback ───────────────────────────────────────────────────────

    /**
     * Warnings that ride on the final result: the caller-side envelope warnings
     * collected in `:app` PLUS the worker's own (it re-parses `input_json` with
     * these same parsers, so the two lists largely overlap).
     *
     * [fix/da1015-modeluse-proc] The caller-side list used to be DISCARDED: the
     * remote branch assigned `callWarnings = remoteWarnings`, so a warning only
     * this process can produce — e.g. `extra_headers` present but not a JSON
     * object, which `:modelservice`'s copy of that parser silently ignores —
     * never reached the caller in either the JSON body or stderr.
     *
     * Local first, then remote, de-duplicated on first occurrence: the caller's
     * own envelope problems read first, and a warning both processes produce
     * (identical strings, same parser) collapses to one line. This is the same
     * rule `attachCallFeedback` applies to the body, so the value handed to the
     * writer is already final.
     */
    fun mergeCallWarnings(local: List<String>, remote: List<String>): MutableList<String> {
        val merged = LinkedHashSet<String>(local.size + remote.size)
        merged.addAll(local)
        merged.addAll(remote)
        return merged.toMutableList()
    }

    // ── image generation route ──────────────────────────────────────────────
    //
    // [fix/audit3-1015 F-10] The image route used to be implemented TWICE, in
    // the two processes, and only one copy was live:
    //
    //   - `:app`'s `tryImageGenerationRoute` (≈150 lines) had the complete
    //     routing logic (pure-image pinning, input-image handling, endpoint
    //     resolution) but was reachable only from the fallback path taken when
    //     a dispatch FAILED — and that path called `generateImage` in-process,
    //     which `ProviderBoundary.enforce` rejects (F-03's violation).
    //   - `:modelservice`'s branch was the live one, but it keyed the route on
    //     `outputExt in ("png","jpg",…)` alone: a pure image generator invoked
    //     without `-o` was sent to the chat endpoint (which cannot serve it),
    //     input images were silently dropped into a text-to-image call, and
    //     the pure-image endpoint pin was missing.
    //
    // The decision now lives here, once, as a pure function of its inputs.

    /** Image-gen knobs the `run` input JSON may carry. */
    data class ImageGenConfig(
        val prompt: String? = null,
        val n: Int = 1,
        val size: String? = null,
        val quality: String? = null,
        val endpointOverride: ImageEndpointMode? = null,
    )

    /**
     * `prompt`/`n`/`size`/`quality` from either the top level or
     * `generation_config` (top level wins), plus an endpoint override via
     * `image_endpoint` / `endpoint` / `generation_config.image_endpoint`.
     */
    fun parseImageGenConfig(inputJson: String): ImageGenConfig {
        val obj = try {
            val t = inputJson.trim()
            if (t.startsWith("{")) JSONObject(t) else null
        } catch (_: Throwable) { null } ?: return ImageGenConfig()

        var prompt: String? = null
        var n = 1
        var size: String? = null
        var quality: String? = null

        // generation_config first (lower priority).
        obj.optJSONObject("generation_config")?.let { gc ->
            val gn = if (gc.has("n")) gc.optInt("n", 1)
            else if (gc.has("number_of_images")) gc.optInt("number_of_images", 1) else 1
            n = maxOf(1, gn)
            size = gc.safeOptString("size", "").ifEmpty { gc.safeOptString("image_size", "").ifEmpty { null } }
            quality = gc.safeOptString("quality", "").ifEmpty { null }
            prompt = gc.safeOptString("prompt", "").ifEmpty { null }
        }
        // Top-level overrides.
        obj.safeOptString("prompt", "").takeIf { it.isNotEmpty() }?.let { prompt = it }
        if (obj.has("n")) n = maxOf(1, obj.optInt("n", n))
        obj.safeOptString("size", "").takeIf { it.isNotEmpty() }?.let { size = it }
        obj.safeOptString("quality", "").takeIf { it.isNotEmpty() }?.let { quality = it }

        // Endpoint override (image_endpoint / endpoint / generation_config.image_endpoint).
        val rawEndpoint = obj.safeOptString("image_endpoint", "").ifEmpty {
            obj.safeOptString("endpoint", "").ifEmpty {
                obj.optJSONObject("generation_config")?.safeOptString("image_endpoint", "").orEmpty()
            }
        }
        val endpoint = when (rawEndpoint.lowercase()) {
            "auto" -> ImageEndpointMode.auto
            "images_generations", "images-generations", "images-gen" ->
                ImageEndpointMode.imagesGenerations
            "chat_completions", "chat-completions", "chat" ->
                ImageEndpointMode.chatCompletions
            else -> null
        }
        return ImageGenConfig(prompt, n, size, quality, endpoint)
    }

    /** Extensions that mean "the caller wants the answer written as an image". */
    private val IMAGE_OUTPUT_EXTS = setOf("png", "jpg", "jpeg", "webp", "gif")

    fun isImageOutputExt(ext: String?): Boolean = ext?.lowercase() in IMAGE_OUTPUT_EXTS

    /**
     * `image_output` without `text_output`: such a model has exactly ONE usable
     * endpoint (the Images API). A stale cached `chatCompletions` pin would
     * send it somewhere it cannot answer (401 "Missing scopes"), so the route
     * pins it to `imagesGenerations` regardless of the cache.
     */
    fun isPureImageGenerator(outputModalities: List<String>): Boolean =
        outputModalities.any { it.equals("image", ignoreCase = true) } &&
            outputModalities.none { it.equals("text", ignoreCase = true) }

    sealed class ImageRoute {
        /** No image route: use the normal chat-completions sendMessage path. */
        object ChatPath : ImageRoute()

        /** Route through an image endpoint in the given mode. */
        data class Generate(val mode: ImageEndpointMode) : ImageRoute()

        /** Terminal refusal — the caller must surface this, not fall through. */
        data class Reject(val error: String, val message: String) : ImageRoute()
    }

    /**
     * [da1031-D ISSUE-08] Can an instance of this provider/credential pair be
     * served by an OpenAI Images API endpoint at all?
     *
     * These are the three provider/credential legs of [decideImageRoute],
     * extracted so the judge has exactly one body. `QuickTestSheet` used to
     * re-decide eligibility with its own copy of the rule (`providerType ==
     * openAI`), so an OpenRouter / xAI image model was refused on the card
     * while the worker would have routed it fine.
     *
     * OAuth credentials are excluded: the Images API is authenticated with an
     * API key, not the OAuth bearer the chat path may carry.
     */
    fun supportsImageApiRoute(
        providerType: ProviderType?,
        credentialType: ProviderCredential?,
        isOpenAiCompatibleProvider: Boolean,
    ): Boolean {
        if (!isOpenAiCompatibleProvider) return false
        if (providerType != ProviderType.openAI &&
            providerType != ProviderType.openRouter &&
            providerType != ProviderType.xAI
        ) {
            return false
        }
        return credentialType != ProviderCredential.oauth
    }

    /**
     * Where should an image-output model send this `run` call?
     *
     * Ported verbatim from `:app`'s `tryImageGenerationRoute`, with the
     * service's live `-o <media ext>` trigger kept as the mixed-model gate:
     * `-o out.png` is the caller-facing way to say "I want an image".
     */
    fun decideImageRoute(
        displayName: String,
        outputModalities: List<String>,
        outputExt: String?,
        hasInputImages: Boolean,
        providerType: ProviderType?,
        credentialType: ProviderCredential?,
        isOpenAiCompatibleProvider: Boolean,
        imageEndpointMode: ImageEndpointMode,
        imageEndpointResolved: ImageEndpointMode?,
        endpointOverride: ImageEndpointMode?,
    ): ImageRoute {
        // Only OpenAI-compatible API-key instances have an Images API. The
        // provider/credential legs live in [supportsImageApiRoute] (single
        // source — the QuickTest card calls the same function).
        if (!supportsImageApiRoute(providerType, credentialType, isOpenAiCompatibleProvider)) {
            return ImageRoute.ChatPath
        }
        if (outputModalities.none { it.equals("image", ignoreCase = true) }) return ImageRoute.ChatPath

        val pure = isPureImageGenerator(outputModalities)
        // [F-10 gap 1] A pure image generator WITHOUT `-o <media ext>` used to
        // be sent to the chat path, where an image-only model cannot answer.
        // It has exactly one route; take it. (Mixed text+image models keep the
        // `-o` trigger: a caller asking for text gets text.)
        if (!isImageOutputExt(outputExt) && !pure) return ImageRoute.ChatPath

        // [F-10 gap 2] Input images used to be silently dropped into a
        // text-to-image call, so the reference image was ignored and the user
        // got the wrong picture with no explanation.
        if (hasInputImages) {
            if (pure) {
                return ImageRoute.Reject(
                    error = "image_edit_not_supported",
                    message = "Image-to-image editing (input image + $displayName) is not " +
                        "supported by minis-model-use on Android yet — only text-to-image " +
                        "generation. Omit the input image, or use a model that also supports " +
                        "text output so the image can be passed via chat.",
                )
            }
            // Mixed text+image model: the chat path forwards imageParts, so the
            // reference image is at least seen.
            return ImageRoute.ChatPath
        }

        // [F-10 gap 3] Endpoint-mode resolution, including the pure-image pin:
        // a pure image generator can NEVER be pinned to chatCompletions (a
        // stale probe cache would send it to an endpoint that cannot serve it).
        val effectiveMode = when {
            endpointOverride != null -> endpointOverride
            pure -> ImageEndpointMode.imagesGenerations
            imageEndpointMode == ImageEndpointMode.auto && imageEndpointResolved != null -> imageEndpointResolved
            else -> imageEndpointMode
        }
        if (effectiveMode == ImageEndpointMode.chatCompletions) return ImageRoute.ChatPath
        return ImageRoute.Generate(effectiveMode)
    }

    /**
     * Does this provider error mean "that endpoint is not there" (→ the auto
     * probe may cache chatCompletions and fall through) rather than a real
     * failure (auth / moderation / network), which must surface?
     */
    fun looksLikeEndpointMissing(message: String): Boolean {
        val m = message.lowercase()
        return m.contains("404") ||
            m.contains("not found") ||
            m.contains("no such endpoint") ||
            m.contains("unknown endpoint") ||
            m.contains("invalid url") ||
            m.contains("method not allowed") ||
            m.contains("unsupported route") ||
            m.contains("405")
    }

    /** Provider-specific image-param guidance appended to a terminal failure. */
    fun imageParamHint(
        displayName: String,
        providerType: ProviderType?,
        outputModalities: List<String>,
    ): String {
        if (outputModalities.none { it.equals("image", ignoreCase = true) }) return ""
        return when (providerType) {
            ProviderType.gemini -> """
                Hint — $displayName is a Gemini image model. Pass image params under `generation_config` in the input JSON:
                  aspect_ratio       "1:1" | "16:9" | "9:16" | "4:3" | "3:4"
                  image_size         "512px" | "1K" | "2K" | "4K"
                  number_of_images   1-4
                  person_generation  "DONT_ALLOW" | "ALLOW_ADULT"
                Example:
                  {"messages":[{"role":"user","content":"<prompt>"}],
                   "generation_config":{"aspect_ratio":"16:9","image_size":"2K"}}
            """.trimIndent()
            ProviderType.openAI, ProviderType.openRouter -> """
                Hint — $displayName is an OpenAI-compatible image model. Pass image params at the top level of the input JSON (matches /v1/images/generations):
                  n         integer, number of images (default 1)
                  size      "1024x1024" | "1792x1024" | "1024x1792" | etc.
                  quality   "standard" | "hd"
                  prompt    string (overrides last user message)
                Example:
                  {"prompt":"<prompt>","size":"1792x1024","quality":"hd","n":1}

                Passthrough (topics: image-to-image, extra_body, extra_headers, endpoint_path) — any field our schema doesn't model is forwarded VERBATIM into the /images/generations JSON body, so you can drive provider-specific params (e.g. Volcengine Seedream image-to-image via an `image` field, `watermark`, `tools`). Two ways, both optional & additive:
                  • implicit: any unknown TOP-LEVEL key → request body
                  • explicit: `extra_body` (object, merged into body), `extra_headers` (string map, added to request headers), `endpoint_path` (string, overrides "/images/generations" for non-standard endpoints)
                Your keys win over our defaults; `model` is always forced to the resolved id. If you set `response_format`, the b64_json auto-probe is skipped.
                Image-to-image example (Seedream — image goes in the BODY, not messages):
                  {"prompt":"make the eyes blue","image":"data:image/png;base64,<...>","size":"2K","watermark":false}
                Explicit-envelope example:
                  {"messages":[{"role":"user","content":"<prompt>"}],"extra_body":{"image":"<url-or-data-uri>","seed":42},"extra_headers":{"X-Custom":"1"},"endpoint_path":"/api/v3/images/generations"}
            """.trimIndent()
            // xAI (Grok) / Kimi Coding have no image-output models in the
            // current catalog — fall through to empty hint like Anthropic.
            ProviderType.anthropic, ProviderType.xAI, ProviderType.kimiCode, null -> ""
        }
    }

    // ── passthrough envelope [T-android-model-use-passthrough-mode] ──────
    //
    // [fix/audit3-1015 F-03] `--passthrough` used to run its network call in the
    // APP process (`performRawPassthrough` → `openAI.rawPassthroughRequest`),
    // where `ProviderBoundary.enforce` rejects the process; the catch turned
    // that into `{"error":"passthrough_failed"}`, so the mode could never
    // succeed. The split is explicit now: this module owns the pure JSON half
    // (envelope parsing + body assembly, so the bytes come from ONE
    // implementation), the app keeps CLI shaping and file IO, and the WORKER
    // owns the HTTP call.

    /**
     * Keys the handler already interprets itself — never treated as implicit
     * passthrough body fields, so existing callers' structure/behavior is
     * unchanged. (Explicit `extra_body` can still re-introduce any of these.)
     * Mirrors iOS ModelUseOffloadBridge.imageReservedKeys.
     */
    private val imageReservedKeys: Set<String> = setOf(
        "messages", "model", "chat_model", "prompt", "n", "number_of_images",
        "size", "image_size", "quality", "generation_config", "endpoint",
        "image_endpoint", "endpoint_path", "extra_body", "extra_headers",
        "stream", "temperature", "max_tokens",
    )

    /**
     * Explicit passthrough envelope: verbatim body/headers/endpoint, raw
     * (unparsed) response output. Mirrors iOS ModelUseOffloadBridge.PassthroughSpec.
     */
    data class PassthroughSpec(
        val active: Boolean = false,
        val endpoint: String? = null,        // "/abs/path?q" (absolute) or "rel/segment"
        val method: String = "POST",
        val headers: Map<String, String> = emptyMap(),
        val body: Map<String, Any?> = emptyMap(),
        val bodyMode: String = "merge",      // "merge" | "replace"
        /** [T-model-use-passthrough-warnings] Ignored/downgraded-field feedback. */
        val warnings: List<String> = emptyList(),
    )

    /**
     * Parse the top-level `passthrough` object into a [PassthroughSpec].
     * Mirrors iOS ModelUseOffloadBridge.parsePassthroughEnvelope.
     */
    fun parsePassthroughEnvelope(inputJson: String): PassthroughSpec {
        val obj = try {
            val t = inputJson.trim()
            if (t.startsWith("{")) JSONObject(t) else null
        } catch (_: Throwable) { null } ?: return PassthroughSpec()

        val warnings = mutableListOf<String>()
        val envRaw: Any? = obj.opt("passthrough")
        val env = envRaw as? JSONObject
        if (env == null) {
            // [T-model-use-passthrough-warnings] Exact key absent or wrong
            // type — surface the two silent failure shapes. Mirrors iOS.
            if (envRaw != null && envRaw != JSONObject.NULL) {
                warnings.add(
                    "Top-level 'passthrough' is not a JSON object (actual type: ${jsonTypeName(envRaw)}) — " +
                        "it was ignored and this call ran in STANDARD mode. " +
                        "Expected shape: {\"passthrough\":{\"endpoint\":...,\"body\":{...}}}.",
                )
                return PassthroughSpec(warnings = warnings)
            }
            val envelopeKeys = setOf("endpoint", "body", "body_mode", "headers", "method")
            for (key in obj.keys()) {
                if (key == "passthrough" || key in imageReservedKeys) continue
                val v = obj.opt(key) as? JSONObject ?: continue
                val normalized = key.lowercase().replace("_", "").replace("-", "")
                val looksLikeName = normalized in setOf("passthrough", "passthru", "passthough", "pathrough")
                val looksLikeShape = v.keys().asSequence().any { it in envelopeKeys }
                if (looksLikeName || looksLikeShape) {
                    warnings.add(
                        "Top-level key '$key' looks like a passthrough envelope, but the exact key " +
                            "must be lowercase 'passthrough' — it was ignored and this call ran in STANDARD mode.",
                    )
                }
            }
            return PassthroughSpec(warnings = warnings)
        }

        var endpoint: String? = null
        env.opt("endpoint")?.takeIf { it != JSONObject.NULL }?.let { epRaw ->
            val ep = (epRaw as? String)?.trim()
            if (!ep.isNullOrEmpty()) {
                endpoint = ep
            } else {
                warnings.add(
                    "passthrough.endpoint is not a non-empty string (actual type: ${jsonTypeName(epRaw)}) — " +
                        "ignored; the default chat/completions path was used.",
                )
            }
        }
        var method = "POST"
        env.opt("method")?.takeIf { it != JSONObject.NULL }?.let { mRaw ->
            // PATCH included — provider-native update APIs use it. Mirrors iOS.
            val allowed = setOf("GET", "POST", "PUT", "DELETE", "PATCH")
            val m = (mRaw as? String)?.uppercase()
            if (m != null && m in allowed) {
                method = m
            } else {
                val desc = (mRaw as? String) ?: jsonTypeName(mRaw)
                warnings.add("method value '$desc' is not supported (supported: GET/POST/PUT/DELETE/PATCH) — sent as POST.")
            }
        }
        val headers = LinkedHashMap<String, String>()
        env.opt("headers")?.takeIf { it != JSONObject.NULL }?.let { hRaw ->
            if (hRaw is JSONObject) {
                for (key in hRaw.keys()) {
                    val v = hRaw.opt(key)
                    if (v is String) {
                        headers[key] = v
                    } else {
                        warnings.add(
                            "passthrough.headers.$key value is not a string (actual type: ${jsonTypeName(v)}) — " +
                                "this header was ignored. Header values must be JSON strings.",
                        )
                    }
                }
            } else {
                warnings.add(
                    "passthrough.headers is not a JSON object (actual type: ${jsonTypeName(hRaw)}) — " +
                        "all custom headers were ignored.",
                )
            }
        }
        val body = LinkedHashMap<String, Any?>()
        env.opt("body")?.takeIf { it != JSONObject.NULL }?.let { bRaw ->
            if (bRaw is JSONObject) {
                for (key in bRaw.keys()) body[key] = bRaw.opt(key)
            } else {
                warnings.add(
                    "passthrough.body is not a JSON object (actual type: ${jsonTypeName(bRaw)}) — " +
                        "ignored. Wrap your body fields in an object: \"body\":{...}.",
                )
            }
        }
        var bodyMode = "merge"
        env.opt("body_mode")?.takeIf { it != JSONObject.NULL }?.let { bmRaw ->
            val bm = (bmRaw as? String)?.lowercase()
            if (bm != null && bm in setOf("merge", "replace")) {
                bodyMode = bm
            } else {
                val desc = (bmRaw as? String) ?: jsonTypeName(bmRaw)
                warnings.add("body_mode value '$desc' is invalid (only merge/replace are supported) — treated as merge.")
            }
        }
        return PassthroughSpec(true, endpoint, method, headers, bodyMode = bodyMode, body = body, warnings = warnings)
    }

    /**
     * Assemble the request body for a passthrough call.
     *
     * merge mode keeps the standard OpenAI shape (messages + system +
     * max_tokens, `model` LOCKED to the resolved id) so callers add ONLY their
     * extras; replace mode is the caller's object verbatim, with `model`
     * unlocked — they own it. A replace-mode call with no body is legitimate
     * for GET-style calls and a common nesting mistake otherwise, so it is
     * reported through [warnings] either way rather than silently sent.
     *
     * [fix/audit3-1015 F-03] Deciding these bytes is JSON work, not I/O, so it
     * belongs here where both processes can be shown to agree on it.
     */
    fun buildRawPassthroughBody(
        spec: PassthroughSpec,
        modelId: String,
        systemPrompt: String?,
        messages: List<Pair<String, String>>,
        maxTokens: Int,
        warnings: MutableList<String>,
    ): JSONObject? {
        if (spec.bodyMode == "replace") {
            if (spec.body.isEmpty()) {
                warnings.add(
                    "body_mode=replace but body is empty or missing \u2014 this request was sent with NO body. " +
                        "If that is not what you intended, check that your fields are nested inside " +
                        "passthrough.body (not at the top level).",
                )
                return null
            }
            val out = JSONObject()
            for ((k, v) in spec.body) out.put(k, v ?: JSONObject.NULL)
            return out
        }
        val baseline = JSONObject().put("model", modelId)
        if (messages.isNotEmpty()) {
            val arr = JSONArray()
            if (!systemPrompt.isNullOrEmpty()) {
                arr.put(JSONObject().put("role", "system").put("content", systemPrompt))
            }
            for ((role, content) in messages) {
                arr.put(JSONObject().put("role", role).put("content", content))
            }
            baseline.put("messages", arr)
            baseline.put("max_tokens", maxTokens)
            baseline.put("stream", false)
        }
        for ((k, v) in spec.body) baseline.put(k, v ?: JSONObject.NULL)
        baseline.put("model", modelId)   // merge mode locks model
        return baseline
    }

    /**
     * The app→worker wire envelope for a passthrough call.
     *
     * [fix/audit3-1015 F-03] The assembled request travels as JSON through
     * ModelExecutionDispatcher into :modelservice, which reads exactly the keys
     * below (`endpoint` / `method` / `headers` / `body`). Building it here, next
     * to the parser it mirrors, keeps the two ends of the boundary checkable
     * against each other instead of by eye; absent values travel as explicit
     * JSON null rather than being dropped.
     */
    fun buildPassthroughWireRequest(spec: PassthroughSpec, body: JSONObject?): JSONObject =
        JSONObject()
            .put("endpoint", spec.endpoint ?: JSONObject.NULL)
            .put("method", spec.method)
            .put("headers", JSONObject(spec.headers as Map<*, *>))
            .put("body", body ?: JSONObject.NULL)
            .put("body_mode", spec.bodyMode)
}

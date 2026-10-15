package com.rikkaminis.app.sandbox.offload

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
}

package com.rikkaminis.app.config

import android.content.Context
import com.rikkaminis.app.data.repository.ChatRepository
import com.rikkaminis.app.data.repository.EnvVarRepository
import com.rikkaminis.app.data.repository.ProviderRepository
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Single source of truth for every configurable setting in the app.
 *
 * Add a new setting in three steps:
 *   1. Pick a dot-path id (`appearance.theme`, `browser.uaProfile`, …).
 *   2. Construct a [ConfigField] (or a [ConfigCollection] for dynamic
 *      children) and register it in `ConfigBuiltins`.
 *   3. Done — the offload bridge, confirmation gate, audit log, revert
 *      flow, and topic-help output all derive from this registry.
 *
 * Mirrors iOS `ConfigRegistry`. The Android side is plain mutable maps
 * (no actor isolation needed — handler threads are the writers and the
 * registry is initialized once at boot before any reads).
 */
class ConfigRegistry private constructor() {
    private val fields = LinkedHashMap<String, ConfigField>()
    private val collections = LinkedHashMap<String, ConfigCollection>()
    private val initialized = AtomicBoolean(false)

    fun register(field: ConfigField) {
        fields[field.path] = field
    }

    fun register(collection: ConfigCollection) {
        collections[collection.basePath] = collection
    }

    /**
     * Look up a field by path. Handles both flat fields and collection
     * children (`<base>.<id>.<sub>`). Returns null for unknown paths.
     *
     * The grammar itself lives in [resolveRegistryField] — a pure function
     * (maps in, field out), so the path rules are JVM-testable without an
     * Android context and there is exactly one place that decides where a
     * child id ends.
     */
    fun resolveField(path: String): ConfigField? =
        resolveRegistryField(path, fields, collections)

    fun collection(basePath: String): ConfigCollection? = collections[basePath]

    /** All registered top-level field paths (excluding hidden), sorted. */
    fun allVisibleFieldPaths(): List<String> =
        fields.values
            .filter { it.access != ConfigAccess.HIDDEN }
            .map { it.path }
            .sorted()

    /** Topic names = unique first segments of every visible path / collection base. Sorted. */
    fun topics(): List<String> {
        val set = LinkedHashSet<String>()
        for (f in fields.values) {
            if (f.access == ConfigAccess.HIDDEN) continue
            val head = f.path.substringBefore('.', missingDelimiterValue = "")
            if (head.isNotEmpty()) set.add(head)
        }
        for (c in collections.values) set.add(c.basePath)
        return set.sorted()
    }

    /**
     * All visible fields whose path equals `<topic>` (the bare topic
     * name — e.g. an aggregate `providers` summary) or starts with
     * `<topic>.`. When [topic] matches a registered collection, a
     * representative child's fields (using the first child id) are
     * also included so `topic-help <collection>` surfaces the per-
     * child schema instead of an empty list. Mirrors iOS.
     */
    fun fields(topic: String): List<ConfigField> {
        val out = ArrayList<ConfigField>()
        for (f in fields.values) {
            if (f.access == ConfigAccess.HIDDEN) continue
            if (f.path == topic || f.path.startsWith("$topic.")) out.add(f)
        }
        val coll = collections[topic]
        if (coll != null) {
            val firstId = coll.childIds().firstOrNull()
            if (firstId != null) {
                out.addAll(coll.fields(forId = firstId).filter { it.access != ConfigAccess.HIDDEN })
            }
        }
        return out.sortedBy { it.path }
    }

    companion object {
        /**
         * Process-wide singleton. The first caller to invoke [init] wins;
         * subsequent calls are no-ops so idempotent registration is safe.
         */
        @Volatile private var INSTANCE: ConfigRegistry? = null

        fun get(): ConfigRegistry =
            INSTANCE ?: error("ConfigRegistry not initialized; call init() from Application.onCreate")

        fun init(
            context: Context,
            providerRepository: ProviderRepository,
            envVarRepository: EnvVarRepository,
            chatRepository: ChatRepository,
            sessionIdProvider: () -> String?,
        ): ConfigRegistry {
            INSTANCE?.let { return it }
            synchronized(this) {
                INSTANCE?.let { return it }
                val r = ConfigRegistry()
                if (r.initialized.compareAndSet(false, true)) {
                    ConfigBuiltins.registerInto(
                        r, context, providerRepository, envVarRepository, chatRepository,
                        sessionIdProvider,
                    )
                }
                INSTANCE = r
                return r
            }
        }
    }
}

/**
 * Pure core of [ConfigRegistry.resolveField] — resolve [path] against the
 * flat [fields] map and the [collections] table. Returns null for unknown
 * paths.
 *
 * A collection child path (`<base>.<id>.<sub>`) cannot be split by counting
 * dots: the child id itself carries dots and so may the leaf. A previous
 * implementation did exactly that and silently truncated every dotted id —
 * `models.<instanceId>/gpt-5.5.isHidden` was cut into
 * `fields(forId = "<instanceId>/gpt-5")`, so `minis-config get/set` answered
 * `unknown_path` for every child of a dotted id (16 of the 39 built-in
 * models have a dot in their id, and user-added model ids like
 * `deepseek-v4.1-flash` are just as common; env-var keys may carry dots too).
 *
 * The collection's own id table is the only authority for where the id
 * ends: take the LONGEST registered id that prefixes the remainder, then
 * confirm the collection exposes a field whose full path equals the request
 * (a near match is a miss — refuse instead of guessing).
 */
internal fun resolveRegistryField(
    path: String,
    fields: Map<String, ConfigField>,
    collections: Map<String, ConfigCollection>,
): ConfigField? {
    fields[path]?.let { return it }
    val coll = collections[path.substringBefore('.', missingDelimiterValue = "")] ?: return null
    val prefix = coll.basePath + "."
    if (!path.startsWith(prefix)) return null
    val rest = path.substring(prefix.length)
    val childId = coll.childIds()
        .filter { rest.startsWith("$it.") }
        .maxByOrNull { it.length }
        ?: return null
    return coll.fields(forId = childId).firstOrNull { it.path == path }
}

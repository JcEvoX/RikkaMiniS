package com.rikkaminis.app.config

import com.rikkaminis.app.config.fields.ClosureField
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

/**
 * [fix/dv21-a1 A-01] Drives the pure path grammar behind
 * `ConfigRegistry.resolveField` ([resolveRegistryField]).
 *
 * The registry itself is Context-bound (its singleton is built from
 * ProviderRepository / EnvVarRepository / ChatRepository, all of which are
 * unconstructible on the JVM), so the grammar was extracted into a top-level
 * internal function taking the two plain maps. That extraction is what makes
 * the defect reachable from a unit test: the pre-fix implementation split the
 * path with `path.split('.', limit = 3)` and therefore cut every child id
 * that contains a dot.
 *
 * Expectations are written as hard-coded literals on purpose — deriving them
 * from the id table under test would let a mutation of that table flip both
 * the input and the expectation, i.e. pass while the rule is wrong.
 *
 * The pinning of the *decision* (id boundary comes from `childIds()`, not
 * from counting dots; near match = miss) lives in
 * `scripts/scan/entry_composite_key_guard_check.py` arms 6–10, not here —
 * a test cannot notice a regression in code it re-implements.
 */

/** Top-level (not a member): [FakeCollection] is a nested class and cannot
 *  reach the enclosing test instance. */
private fun fieldOf(path: String): ConfigField = ClosureField(
    path = path,
    displayName = path,
    description = path,
    valueSchema = ConfigSchema.Str(),
    reader = { ConfigValue.Null },
    writer = { },
)

class ConfigRegistryResolveFieldTest {

    // -- fixtures -------------------------------------------------------

    /**
     * Minimal collection: [childIds] is an explicit table (never derived) and
     * [fields] mints one field per leaf for whichever id is asked for — the
     * same contract the four production collections honour.
     */
    private class FakeCollection(
        override val basePath: String,
        private val ids: List<String>,
    ) : ConfigCollection {
        override val displayName: String get() = basePath
        override val description: String get() = "fake $basePath"
        override fun childIds(): List<String> = ids
        override fun fields(forId: String): List<ConfigField> =
            if (forId !in ids) emptyList()
            else LEAVES.map { fieldOf("$basePath.$forId.$it") }
        override fun add(payload: ConfigValue): String = error("not used")
        override fun remove(id: String) {}

        private companion object {
            val LEAVES = listOf(
                "displayName", "contextWindowOverride", "isHidden", "isEnabled",
                "note", "group",
            )
        }
    }

    // Composite ids exactly as ProviderConfigMapping.compositeEntryKey mints
    // them: "<instanceId>/<modelId>", where the model id carries the dots.
    private val instanceA = "6f97e418-7498-4426-9cb2-a2784dc97240"
    private val instanceB = "d5e7d749-e541-4971-9e38-55f6d0e0725b"

    private val models = FakeCollection(
        basePath = "models",
        ids = listOf(
            "$instanceA/gpt-5.5",
            "$instanceA/gemini-2.5-pro",
            "$instanceB/deepseek-v4.1-flash",
            // Deliberately a prefix-extension of the first id: the resolver
            // must pick the LONGEST matching id, not the first one found.
            "$instanceA/gpt-5.5.1",
        ),
    )
    private val providers = FakeCollection("providers", listOf("p-uuid-1", "p-uuid-2"))
    private val groups = FakeCollection("groups", listOf("g-1"))
    private val envvars = FakeCollection("envvars", listOf("MY.KEY", "PLAIN"))

    private val collections: Map<String, ConfigCollection> = linkedMapOf(
        "models" to models,
        "providers" to providers,
        "groups" to groups,
        "envvars" to envvars,
    )

    private val flat: Map<String, ConfigField> = linkedMapOf(
        "debug.minisConfig.exec" to fieldOf("debug.minisConfig.exec"),
        "appearance.theme" to fieldOf("appearance.theme"),
        "models.aggregate.summary" to fieldOf("models.aggregate.summary"),
    )

    private fun resolve(path: String): ConfigField? =
        resolveRegistryField(path, flat, collections)

    /** Resolve and assert the *whole* path round-trips (no truncated id). */
    private fun assertResolves(path: String) {
        val field = resolve(path)
        assertEquals("$path did not resolve", path, field?.path)
    }

    // -- ① the defect: dotted child ids --------------------------------

    @Test
    fun `dotted model id resolves its leaf field`() {
        assertResolves("models.$instanceA/gpt-5.5.displayName")
    }

    @Test
    fun `dotted model id resolves a second leaf`() {
        assertResolves("models.$instanceA/gpt-5.5.contextWindowOverride")
    }

    @Test
    fun `dashed and dotted model id resolves`() {
        assertResolves("models.$instanceA/gemini-2.5-pro.isHidden")
        assertResolves("models.$instanceB/deepseek-v4.1-flash.displayName")
    }

    @Test
    fun `dotted env var key resolves`() {
        assertResolves("envvars.MY.KEY.note")
        assertResolves("envvars.MY.KEY.group")
    }

    @Test
    fun `the truncated id is not what resolves`() {
        // Pre-fix, "<instanceId>/gpt-5" was the id handed to fields(forId=…),
        // which returned an empty list. Guard the shape explicitly: the
        // resolved field's path carries the FULL id.
        val field = resolve("models.$instanceA/gpt-5.5.isHidden")
        assertEquals("models.$instanceA/gpt-5.5.isHidden", field?.path)
        assertNull(resolve("models.$instanceA/gpt-5.isHidden"))
    }

    // -- ② dot-free ids keep working -----------------------------------

    @Test
    fun `dot free child id still resolves`() {
        assertResolves("providers.p-uuid-1.isEnabled")
        assertResolves("groups.g-1.displayName")
        assertResolves("envvars.PLAIN.note")
    }

    // -- ③ unknown paths are null, never a near match ------------------

    @Test
    fun `unknown leaf returns null`() {
        assertNull(resolve("models.$instanceA/gpt-5.5.bogusField"))
    }

    @Test
    fun `unknown child id returns null`() {
        assertNull(resolve("models.$instanceA/gpt-5.0.displayName"))
        assertNull(resolve("providers.p-uuid-9.isEnabled"))
    }

    @Test
    fun `unknown collection returns null`() {
        assertNull(resolve("nothing.abc.def"))
        assertNull(resolve(""))
        assertNull(resolve(".leading.dot"))
    }

    @Test
    fun `bare collection base is not a field`() {
        assertNull(resolve("models"))
        assertNull(resolve("providers"))
    }

    @Test
    fun `a near match on the leaf is a miss`() {
        assertNull(resolve("models.$instanceA/gpt-5.5.displayNameX"))
        assertNull(resolve("models.$instanceA/gpt-5.5extra.displayName"))
    }

    // -- ④ flat fields keep O(1) priority ------------------------------

    @Test
    fun `flat field is found by direct lookup`() {
        assertSame(flat["appearance.theme"], resolve("appearance.theme"))
        assertSame(flat["debug.minisConfig.exec"], resolve("debug.minisConfig.exec"))
    }

    @Test
    fun `flat field wins over a collection reading of the same path`() {
        // "models.aggregate.summary" is a registered flat field; models has no
        // child called "aggregate", so both routes must not fight: the flat
        // map is consulted first and answers.
        assertSame(flat["models.aggregate.summary"], resolve("models.aggregate.summary"))
    }

    // -- ⑤ longest prefix, not first match ----------------------------

    @Test
    fun `longest child id wins`() {
        assertResolves("models.$instanceA/gpt-5.5.1.displayName")
        // …while the shorter sibling is still reachable.
        assertResolves("models.$instanceA/gpt-5.5.displayName")
    }

    // -- ⑥ fail-closed wiring ------------------------------------------

    @Test
    fun `a collection registered under a foreign base path never resolves`() {
        val mismatched = mapOf<String, ConfigCollection>("models" to FakeCollection("groups", listOf("g-1")))
        assertNull(resolveRegistryField("models.g-1.name", flat, mismatched))
    }

    @Test
    fun `an empty child table resolves nothing`() {
        val empty = mapOf<String, ConfigCollection>("models" to FakeCollection("models", emptyList()))
        assertNull(resolveRegistryField("models.$instanceA/gpt-5.5.displayName", flat, empty))
    }
}

package com.rikkaminis.app.data.repository

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1023-C] JVM tests for the model-entry composite-key guard and the
 * compact-pin secondary-reference handling (deep-audit-1023 ISSUE-03 + 09).
 *
 * ProviderRepository itself needs a Room-backed Context and is not
 * JVM-constructible, so the three decisions under test were extracted into
 * top-level pure functions in ProviderRepository.kt (same package, so
 * `internal` is reachable from the test source set). Their *application* —
 * which function calls them, inside which branch — is pinned by source-shape
 * assertions at the bottom of this class (house pattern: the wiring is
 * asserted on real text, the logic on real values).
 *
 * Expected values are hardcoded literals, never derived from the tested
 * constants (evidence-discipline: a mutation must not move the expectation).
 */
class ProviderEntryKeyAndPinTest {

    private val inst1 = "inst-1"
    private val inst2 = "inst-2"

    /** (entryId, compositeKey) pairs as addEntry builds them from live rows. */
    private val live = listOf(
        "uuid-a" to "$inst1/gpt-4o",
        "uuid-b" to "$inst1/claude-3",
        "uuid-c" to "$inst2/gpt-4o",
    )

    // ── decideEntryAdd: composite-key uniqueness ───────────────────────────

    @Test
    fun `new instance-model pair is allowed and persists the composite key`() {
        val d = decideEntryAdd(inst1, "o3-mini", live)
        assertTrue(d.allowed)
        assertEquals("inst-1/o3-mini", d.persistedId)
    }

    @Test
    fun `same model id on a different instance is allowed`() {
        // The key is the PAIR: gpt-4o exists on inst-1 and inst-2 in `live`,
        // and a third instance must not be treated as a duplicate.
        val d = decideEntryAdd("inst-3", "gpt-4o", live)
        assertTrue(d.allowed)
        assertEquals("inst-3/gpt-4o", d.persistedId)
    }

    @Test
    fun `duplicate pair is rejected and yields the surviving entry id`() {
        val d = decideEntryAdd(inst1, "gpt-4o", live)
        assertFalse(d.allowed)
        // Not the composite string: callers surface this as "already exists
        // as <id>", i.e. the uuid the live row actually carries.
        assertEquals("uuid-a", d.persistedId)
    }

    @Test
    fun `duplicate detection is instance-scoped not model-scoped`() {
        // Same modelId on inst-2 must resolve to inst-2's row, never inst-1's.
        val d = decideEntryAdd(inst2, "gpt-4o", live)
        assertFalse(d.allowed)
        assertEquals("uuid-c", d.persistedId)
    }

    @Test
    fun `empty live set always allows`() {
        val d = decideEntryAdd(inst1, "gpt-4o", emptyList())
        assertTrue(d.allowed)
        assertEquals("inst-1/gpt-4o", d.persistedId)
    }

    // ── decideCompactPinMigration: rename rewrite ──────────────────────────

    @Test
    fun `pin equal to the renamed-old key migrates to the new key`() {
        assertEquals(
            "inst-1/new-model",
            decideCompactPinMigration("inst-1/old-model", "inst-1/old-model", "inst-1/new-model"),
        )
    }

    @Test
    fun `pin pointing elsewhere is left alone`() {
        assertNull(
            decideCompactPinMigration("inst-2/gpt-4o", "inst-1/old-model", "inst-1/new-model"),
        )
    }

    @Test
    fun `empty pin is never treated as a match`() {
        // "" means "follow the session chain" — rewriting it on any rename
        // would silently pin the user to an unrelated model.
        assertNull(decideCompactPinMigration("", "inst-1/old-model", "inst-1/new-model"))
        assertNull(decideCompactPinMigration("", "", "inst-1/new-model"))
    }

    @Test
    fun `rename without an actual key change does not rewrite`() {
        assertNull(
            decideCompactPinMigration("inst-1/same", "inst-1/same", "inst-1/same"),
        )
    }

    // ── decideCompactPinCleanup: removal reset ─────────────────────────────

    @Test
    fun `pin inside the removed set is reset`() {
        assertTrue(decideCompactPinCleanup("inst-1/gone", setOf("inst-1/gone")))
        assertTrue(decideCompactPinCleanup("inst-1/gone", setOf("other", "inst-1/gone")))
    }

    @Test
    fun `pin outside the removed set is untouched`() {
        assertFalse(decideCompactPinCleanup("inst-1/keep", setOf("inst-1/gone")))
    }

    @Test
    fun `empty pin is never reset`() {
        assertFalse(decideCompactPinCleanup("", setOf("inst-1/gone", "")))
    }

    // ── source-shape: the wiring that these decisions plug into ────────────
    // Mutation ① (drop the addEntry guard) and the pin call sites are not
    // reachable from a JVM value test — ProviderRepository needs a real
    // Context — so they are pinned on the real source text.

    private fun repositorySource(): String =
        readRepoFile("src/android/app/src/main/java/com/rikkaminis/app/data/repository/ProviderRepository.kt")

    private fun bodyOf(src: String, header: String): String {
        val i = src.indexOf(header)
        require(i >= 0) { "header not found: $header" }
        var depth = 0
        val open = src.indexOf('{', i)
        var j = open
        while (j < src.length) {
            if (src[j] == '{') depth++
            if (src[j] == '}') {
                depth--
                if (depth == 0) return src.substring(i, j + 1)
            }
            j++
        }
        error("unbalanced braces after: $header")
    }

    @Test
    fun `addEntry guard is unconditional, not gated on isCustom`() {
        val body = bodyOf(repositorySource(), "fun addEntry(")
        assertTrue("addEntry must call the composite-key decision", body.contains("decideEntryAdd("))
        assertFalse(
            "the duplicate guard must not sit inside an isCustom-only branch",
            body.contains("!entry.isCustom"),
        )
    }

    @Test
    fun `addEntry returns the canonical persisted id`() {
        val body = bodyOf(repositorySource(), "fun addEntry(")
        // The re-query is what makes the return value the id actually stored
        // (saveConfig canonicalizes the uuid); a bare `entry.id` return would
        // hand back the pre-persist temp uuid.
        assertTrue(body.contains("_config.value.modelEntries.firstOrNull"))
        assertFalse(body.trimEnd().endsWith("return@synchronized entry.id\n    }"))
    }

    @Test
    fun `updateEntry migrates the compact pin inside the rename branch`() {
        val body = bodyOf(repositorySource(), "fun updateEntry(")
        assertTrue(body.contains("decideCompactPinMigration("))
        assertTrue(body.contains("AgentRuntimeLimitsPrefs.compactModelEntryId()"))
        // Must live next to the lastUsedEntryId rewrite, i.e. inside
        // `if (renamed)`, not after the block closes.
        val renameIdx = body.indexOf("if (renamed)")
        val migrateIdx = body.indexOf("decideCompactPinMigration(")
        assertTrue(renameIdx in 0 until migrateIdx)
    }

    @Test
    fun `removeEntry clears the compact pin it just orphaned`() {
        val body = bodyOf(repositorySource(), "fun removeEntry(")
        assertTrue(body.contains("decideCompactPinCleanup("))
        assertTrue(body.contains("compactModelEntryId = \"\""))
    }

    @Test
    fun `removeInstance clears the compact pin for the whole removed set`() {
        // removeInstance cascades on its own and never routes through
        // removeEntry, so it needs its own cleanup.
        val body = bodyOf(repositorySource(), "fun removeInstance(")
        assertTrue(body.contains("decideCompactPinCleanup("))
        assertTrue(body.contains("compactModelEntryId = \"\""))
    }

    /**
     * Walk up from the test working directory to the repo root containing
     * [relative]. Gradle sets user.dir to the module dir; a sandbox JVM run
     * can start anywhere under the repo — no fixed depth works.
     */
    private fun readRepoFile(relative: String): String {
        var dir: java.io.File? = java.io.File(System.getProperty("user.dir"))
        while (dir != null) {
            val f = java.io.File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw java.io.FileNotFoundException(
            "$relative not found from user.dir=${System.getProperty("user.dir")}",
        )
    }
}

package com.rikkaminis.app.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [audit-1015 BUG-07] Regression tests for the env-broadcast staging slot
 * ([PendingEnvironmentUpdate]) and the shared script builder
 * ([internalBuildEnvironmentScript]).
 *
 * Field bug: a TZ/proxy broadcast landing while a command was mid-flight
 * wrote top-level `export` lines into the PTY input stream beneath the
 * running command — the foreground child read them as its own stdin and the
 * PTY echoed them into the captured output.
 *
 * Expected values are literal ground truth.
 */
class PendingEnvironmentUpdateTest {

    // ── staging: busy broadcasts are staged, not lost ────────────────────

    @Test
    fun `staged broadcast drains with its keys`() {
        val q = PendingEnvironmentUpdate()
        assertTrue(q.isEmpty())
        q.merge(mapOf("TZ" to "Asia/Singapore"), previousKeys = emptySet())
        assertFalse(q.isEmpty())
        assertEquals(linkedMapOf("TZ" to "Asia/Singapore" as String?), q.drain())
        assertTrue(q.isEmpty())
    }

    @Test
    fun `drain clears the slot`() {
        val q = PendingEnvironmentUpdate()
        q.merge(mapOf("TZ" to "a"), emptySet())
        q.drain()
        assertEquals(emptyMap<String, String?>(), q.drain())
    }

    @Test
    fun `later broadcast overrides the same key`() {
        val q = PendingEnvironmentUpdate()
        q.merge(mapOf("TZ" to "a"), emptySet())
        q.merge(mapOf("TZ" to "b"), emptySet())
        assertEquals(linkedMapOf("TZ" to "b" as String?), q.drain())
    }

    @Test
    fun `unsets are staged as null entries`() {
        val q = PendingEnvironmentUpdate()
        q.merge(mapOf("TZ" to "a"), emptySet())
        q.merge(mapOf("PROXY" to "p"), previousKeys = setOf("TZ", "OLDPROXY"))
        // Sequential-application semantics: stage 2's unset list (previousKeys
        // absent from its envVars) UNSETS "TZ" and "OLDPROXY" after stage 1
        // set TZ — so the merged entry records TZ=null (per-key last-write).
        // "OLDPROXY" is in previousKeys but absent from envVars → unset.
        assertEquals(
            linkedMapOf("TZ" to null as String?, "OLDPROXY" to null, "PROXY" to "p"),
            q.drain(),
        )
    }

    @Test
    fun `per-key last-write-wins matches sequential application`() {
        // Sequential: apply set TZ=a, then apply unset TZ → TZ unset.
        // Merged staging must produce the same final state.
        val q = PendingEnvironmentUpdate()
        q.merge(mapOf("TZ" to "a"), emptySet())
        q.merge(emptyMap(), previousKeys = setOf("TZ"))
        assertEquals(linkedMapOf("TZ" to null as String?), q.drain())
    }

    // ── script builder: byte-identical to the legacy format ─────────────

    @Test
    fun `script format is byte-identical to the legacy two-loop format`() {
        // The direct broadcast path used to write exactly:
        //   for (key in previousKeys - envVars.keys) writer.write("unset $key\n")
        //   for ((key, value) in envVars) writer.write("export $key='$escaped'\n")
        val envVars = linkedMapOf("A" to "1", "B" to "x y")
        val previousKeys = setOf("A", "OLD", "C")
        val entries = LinkedHashMap<String, String?>()
        for (key in previousKeys - envVars.keys) entries[key] = null
        for ((k, v) in envVars) entries[k] = v
        val script = internalBuildEnvironmentScript(entries)
        assertEquals("unset OLD\nunset C\nexport A='1'\nexport B='x y'\n", script)
    }

    @Test
    fun `single quotes in values are escaped`() {
        val script = internalBuildEnvironmentScript(linkedMapOf("K" to "it's"))
        assertEquals("export K='it'\\''s'\n", script)
    }

    @Test
    fun `null value means unset`() {
        assertEquals("unset TZ\n", internalBuildEnvironmentScript(linkedMapOf("TZ" to null)))
    }

    @Test
    fun `empty entries produce an empty script`() {
        assertEquals("", internalBuildEnvironmentScript(emptyMap()))
    }

    @Test
    fun `drained staging renders as the same script as a direct apply`() {
        // A staged flush must be indistinguishable from an immediate apply.
        val q = PendingEnvironmentUpdate()
        val envVars = mapOf("TZ" to "Asia/Singapore")
        q.merge(envVars, emptySet())
        val staged = internalBuildEnvironmentScript(q.drain())
        val entries = LinkedHashMap<String, String?>()
        for (key in emptySet<String>() - envVars.keys) entries[key] = null
        for ((k, v) in envVars) entries[k] = v
        assertEquals(internalBuildEnvironmentScript(entries), staged)
        assertEquals("export TZ='Asia/Singapore'\n", staged)
    }
}

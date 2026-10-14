package com.rikkaminis.app.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class KeyRouletteTest {

    @Test
    fun `single key passes through verbatim`() {
        val k = "sk-test-123"
        assertEquals(k, KeyRoulette.next(k, "p1"))
        assertEquals(k, KeyRoulette.next(k, "p1"))
    }

    @Test
    fun `duplicated or padded keys collapse to one cleaned token`() {
        // T-provider-key-roulette: previously "k1, k1" was handed back raw and
        // would have been sent as a Bearer value ("k1, k1") → auth failure.
        assertEquals("k1", KeyRoulette.next("k1, k1", "p-dup"))
        assertEquals("q", KeyRoulette.next("q, q  q", "p-dup2"))
        // Stray whitespace around a single key is stripped.
        assertEquals("sk-1", KeyRoulette.next(" sk-1 ", "p-pad"))
        // Blank input still falls through verbatim (upper layers own the error).
        assertEquals("   ", KeyRoulette.next("   ", "p-blank"))
    }

    @Test
    fun `multi key rotates round robin`() {
        val keys = "k1, k2, k3"
        val seen = mutableSetOf<String>()
        repeat(6) {
            val next = KeyRoulette.next(keys, "p-rot")
            assertTrue("picked key must come from the list", next in listOf("k1", "k2", "k3"))
            seen.add(next)
        }
        assertTrue("all keys must be picked across 6 draws", seen.size == 3)
    }

    @Test
    fun `multi key rotation is LRU balanced`() {
        val keys = "a b"
        val picks = List(10) { KeyRoulette.next(keys, "p-lru") }
        assertEquals(5, picks.count { it == "a" })
        assertEquals(5, picks.count { it == "b" })
    }

    @Test
    fun `whitespace and comma delimiters both split`() {
        val comma = "x1,x2"
        val space = "y1 y2"
        assertEquals("x1", KeyRoulette.next(comma, "p-c1"))
        assertEquals("x2", KeyRoulette.next(comma, "p-c1"))
        assertEquals("y1", KeyRoulette.next(space, "p-c2"))
        assertEquals("y2", KeyRoulette.next(space, "p-c2"))
    }

    @Test
    fun `rotation state persists across reinit`() {
        val dir = File.createTempFile("keyroulette", "test").let {
            it.delete()
            File(it.absolutePath + ".d")
        }.apply { mkdirs() }
        val keys = "p1, p2"
        val first = KeyRoulette.next(keys, "p-persist")
        KeyRoulette.init(dir)
        val second = KeyRoulette.next(keys, "p-persist")
        assertTrue(first != second)
        dir.deleteRecursively()
    }

    @Test
    fun `candidates splits deduplicates and cleans`() {
        assertEquals(listOf("k1", "k2", "k3"), KeyRoulette.candidates(" k1 , k2  k3 "))
        assertEquals(listOf("k1"), KeyRoulette.candidates("k1, k1"))
        assertTrue(KeyRoulette.candidates("   ").isEmpty())
    }

    @Test
    fun `consecutive draws from one key string are distinct until exhausted`() {
        // The model-list probe loop leans on this: N draws from the same
        // multi-key string must walk all N keys before repeating any —
        // otherwise a dead key would be retried while a live one stays untried.
        val draws = List(3) { KeyRoulette.next("k1 k2 k3", "p-distinct") }
        assertEquals(listOf("k1", "k2", "k3"), draws)
    }

    @Test
    fun `candidates is draw free and does not disturb the rotation`() {
        // VoiceProviderFactory.supports() classifies on a cleaned candidate
        // while merely listing options; that lookup must not consume a draw,
        // or browsing the UI would re-order which key the next request picks.
        val keys = "a b"
        assertEquals("a", KeyRoulette.next(keys, "p-nodraw")) // a used → b is now LRU
        KeyRoulette.candidates(keys)
        assertEquals("b", KeyRoulette.next(keys, "p-nodraw")) // still b
    }

    @Test
    fun `per provider isolation`() {
        val k1 = KeyRoulette.next("a1 a2", "prov-A")
        val k2 = KeyRoulette.next("b1 b2", "prov-B")
        assertTrue(k1 in listOf("a1", "a2"))
        assertTrue(k2 in listOf("b1", "b2"))
    }

    // ── [rv1014-D2] legacy plaintext migration ──

    /** Test-local reimplementation of the (private) production hashKey. */
    private fun sha256Hex(k: String): String =
        java.security.MessageDigest.getInstance("SHA-256").digest(k.toByteArray(Charsets.UTF_8))
            .take(16).joinToString("") { "%02x".format(it) }

    @Test
    fun `sanitize migrates legacy plaintext keys to hashes preserving stamps`() {
        val obj = org.json.JSONObject(
            """{"p-legacy":{"sk-abc-plaintext":7,"k2":3,"0123456789abcdef0123456789abcdef":11}}""",
        )

        val sanitized = KeyRoulette.sanitizeLegacyRouletteEntries(obj)
            ?: org.json.JSONObject()

        val slice = sanitized.getJSONObject("p-legacy")
        assertFalse("plaintext must be gone", slice.has("sk-abc-plaintext"))
        assertFalse(slice.has("k2"))
        // The 32-hex entry is hash-shaped — kept verbatim.
        assertEquals(11, slice.getLong("0123456789abcdef0123456789abcdef"))
        // Migrated plaintext keys keep their stamps under their hashes.
        assertEquals(7, slice.getLong(sha256Hex("sk-abc-plaintext")))
        assertEquals(3, slice.getLong(sha256Hex("k2")))
    }

    @Test
    fun `sanitize is a no-op on an already hash-only file`() {
        val obj = org.json.JSONObject(
            """{"p-hash":{"0123456789abcdef0123456789abcdef":9,"abcdef0123456789abcdef0123456789":1}}""",
        )
        assertEquals(null, KeyRoulette.sanitizeLegacyRouletteEntries(obj))
    }

    @Test
    fun `sanitize drops blank keys and records them`() {
        val obj = org.json.JSONObject("""{"p-blank":{"":5,"   ":6,"sk-real":2}}""")
        val dropped = mutableListOf<String>()

        val sanitized = KeyRoulette.sanitizeLegacyRouletteEntries(obj, dropped)!!

        val slice = sanitized.getJSONObject("p-blank")
        assertFalse(slice.has(""))
        assertFalse(slice.has("   "))
        assertEquals(2, slice.getLong(sha256Hex("sk-real")))
        assertTrue(dropped.contains(""))
        assertTrue(dropped.contains("   "))
    }

    @Test
    fun `sanitize preserves non-object provider entries verbatim`() {
        val obj = org.json.JSONObject("""{"p-scalar":42,"p-legacy":{"sk-x":4}}""")
        val sanitized = KeyRoulette.sanitizeLegacyRouletteEntries(obj)!!
        assertEquals(42, sanitized.getInt("p-scalar"))
        assertFalse(sanitized.getJSONObject("p-legacy").has("sk-x"))
    }

    @Test
    fun `init migrates a legacy file on disk`() {
        val dir = File.createTempFile("keyroulette", "mig").let {
            it.delete()
            File(it.absolutePath + ".d")
        }.apply { mkdirs() }
        val f = File(dir, "key_roulette.json")
        val legacyStamps = mapOf("sk-old-a" to 100L, "sk-old-b" to 50L)
        f.writeText(
            """{"prov-mig-e2e":{"sk-old-a":100,"sk-old-b":50,"k1 k2":7}}""",
        )

        KeyRoulette.init(dir)

        val after = f.readText()
        assertFalse("plaintext key must not survive on disk", after.contains("sk-old-a"))
        assertFalse(after.contains("sk-old-b"))
        assertFalse("temp file must be cleaned up", File(dir, "key_roulette.json.tmp").exists())
        val slice = org.json.JSONObject(after).getJSONObject("prov-mig-e2e")
        // Stamps preserved under hashes; "k1 k2" contains whitespace so it was
        // never a valid single slice key in the legacy writer — migrated as a
        // single (odd) key with its stamp intact.
        assertEquals(100L, slice.getLong(sha256Hex("sk-old-a")))
        assertEquals(50L, slice.getLong(sha256Hex("sk-old-b")))
        assertEquals(7L, slice.getLong(sha256Hex("k1 k2")))
        assertEquals(3, slice.length())

        // Idempotent: second init on the migrated file changes nothing.
        val before2 = f.readText()
        KeyRoulette.init(dir)
        assertEquals(before2, f.readText())
        dir.deleteRecursively()
    }
}

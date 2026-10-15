package com.rikkaminis.app.sandbox.offload

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [fix/audit3-1015 F-04] The `extra_body` dialect, and the structural guard
 * that keeps it single-sourced.
 *
 * The bug: `:modelservice`'s copy of `parseChatExtraBody` mapped a `JSONArray`
 * through `(0 until v.length()).mapNotNull { e -> e.let { if (it is Int || …)
 * it else null } }`. The lambda parameter was the loop *index*, so `is Int` was
 * unconditionally true and `{"stop":["a","b"]}` was sent as `[0,1]`; nested
 * objects fell into `else` and were dropped with a warning. The `:app` copy had
 * always forwarded raw org.json values, so the SAME input produced two
 * different request bodies depending on which process executed the run — and
 * nothing observed the difference (the JVM path never reaches the worker).
 *
 * Every expectation below is a literal, taken from the pre-fix `:app`
 * behaviour (which is the definition of "correct" here — it is the dialect the
 * iOS bridge and the help text describe), not derived from the implementation.
 */
class ModelUseProtocolTest {

    private fun warn() = mutableListOf<String>()

    // ── F-04 regression: array elements must survive as elements ─────────

    @Test
    fun `extra_body array keeps its elements`() {
        val w = warn()
        val out = ModelUseProtocol.parseChatExtraBody("""{"extra_body":{"stop":["a","b"]}}""", w)
        val arr = out["stop"] as JSONArray
        assertEquals(2, arr.length())
        assertEquals("a", arr.getString(0))
        assertEquals("b", arr.getString(1))
        assertTrue("no warnings expected for a well-formed envelope: $w", w.isEmpty())
    }

    @Test
    fun `extra_body array serializes back to the original JSON`() {
        val w = warn()
        val out = ModelUseProtocol.parseChatExtraBody(
            """{"extra_body":{"stop":["a","b"],"logit_bias":{"50256":-100},"n":2,"stream":false}}""",
            w,
        )
        // The provider does `body.put(key, value)` with these values; that is
        // the only contract that matters. Pre-fix the worker dialect printed
        // `[0,1]` for stop and dropped logit_bias.
        val serialized = JSONObject().apply { for ((k, v) in out) put(k, v) }
        assertEquals("""["a","b"]""", serialized.get("stop").toString())
        assertEquals("""{"50256":-100}""", serialized.get("logit_bias").toString())
        assertEquals(2, serialized.getInt("n"))
        assertEquals(false, serialized.getBoolean("stream"))
        assertTrue("no warnings expected: $w", w.isEmpty())
    }

    @Test
    fun `nested object is preserved as an object, not dropped`() {
        val w = warn()
        val out = ModelUseProtocol.parseChatExtraBody(
            """{"extra_body":{"response_format":{"type":"json_object"}}}""",
            w,
        )
        val nested = out["response_format"]
        assertTrue("expected a JSONObject, got ${nested?.javaClass}", nested is JSONObject)
        assertEquals("json_object", (nested as JSONObject).getString("type"))
        assertTrue("a nested object must not warn-and-drop: $w", w.isEmpty())
    }

    @Test
    fun `array of objects round-trips structurally`() {
        val w = warn()
        val out = ModelUseProtocol.parseChatExtraBody(
            """{"extra_body":{"tools":[{"type":"function","function":{"name":"f"}}]}}""",
            w,
        )
        val arr = out["tools"] as JSONArray
        assertEquals(1, arr.length())
        // org.json's toString() does not promise key order — compare structure.
        val only = JSONObject(arr.get(0).toString())
        assertEquals("function", only.getString("type"))
        assertEquals("f", only.getJSONObject("function").getString("name"))
    }

    // ── scalars / null / absent / malformed ─────────────────────────────

    @Test
    fun `json null is preserved as a key rather than dropped`() {
        val w = warn()
        val out = ModelUseProtocol.parseChatExtraBody("""{"extra_body":{"x":null,"y":1}}""", w)
        assertTrue("the key must survive", out.containsKey("x"))
        assertEquals(JSONObject.NULL, out["x"])
        assertEquals(1, out["y"])
        assertTrue("no warnings expected: $w", w.isEmpty())
    }

    @Test
    fun `unicode and escapes survive verbatim`() {
        val w = warn()
        val out = ModelUseProtocol.parseChatExtraBody("""{"extra_body":{"t":"a\nb«é»"}}""", w)
        assertEquals("a\nb«é»", out["t"])
        assertTrue(w.isEmpty())
    }

    @Test
    fun `absent extra_body yields no body and no warning`() {
        val w = warn()
        assertTrue(ModelUseProtocol.parseChatExtraBody("""{"messages":[]}""", w).isEmpty())
        assertTrue("an absent envelope is not a mistake: $w", w.isEmpty())
    }

    @Test
    fun `empty extra_body object yields no body and no warning`() {
        val w = warn()
        assertTrue(ModelUseProtocol.parseChatExtraBody("""{"extra_body":{}}""", w).isEmpty())
        assertTrue(w.isEmpty())
    }

    @Test
    fun `non-object extra_body is ignored with a type-mismatch warning`() {
        val w = warn()
        val out = ModelUseProtocol.parseChatExtraBody("""{"extra_body":"nope"}""", w)
        assertTrue(out.isEmpty())
        assertEquals(1, w.size)
        assertTrue(w[0].contains("extra_body is not a JSON object"))
        assertTrue(w[0].contains("string"))
    }

    @Test
    fun `malformed input json yields no body and no warning`() {
        val w = warn()
        assertTrue(ModelUseProtocol.parseChatExtraBody("not json at all", w).isEmpty())
        assertTrue(w.isEmpty())
    }

    // ── structural: one implementation, two callers ─────────────────────

    private fun readRepoFile(relative: String): String? {
        var dir: File? = File(System.getProperty("user.dir"))
        while (dir != null) {
            val f = File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        return null
    }

    @Test
    fun `both processes delegate extra_body parsing to this one dialect`() {
        val handler = readRepoFile(
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ModelUseOffloadHandler.kt",
        ) ?: return // tree not on disk → skip (see class kdoc)
        val service = readRepoFile(
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ModelExecutionService.kt",
        ) ?: return
        for ((name, text) in listOf("ModelUseOffloadHandler.kt" to handler, "ModelExecutionService.kt" to service)) {
            assertTrue(
                "$name must delegate extra_body parsing to ModelUseProtocol",
                text.contains("ModelUseProtocol.parseChatExtraBody("),
            )
            assertFalse(
                "$name carries a second extra_body dialect again — index-based array rebuild",
                text.contains("(0 until v.length())"),
            )
        }
    }
}

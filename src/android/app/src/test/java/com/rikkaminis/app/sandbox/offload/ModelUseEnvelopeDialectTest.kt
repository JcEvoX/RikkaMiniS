package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [da1019 ISSUE-01] The LAST two envelope parsers that still had a hand-written
 * copy in EACH process: `parseExtraHeaders` and `parseCustomEndpointPath`. The
 * copies had already drifted — the worker dropped a present-but-not-an-object
 * `extra_headers` with ZERO warning (its KDoc claimed "both routes now drop +
 * warn identically"), accepted any non-empty `endpoint` as a path (symbolic
 * values like `{"endpoint":"chat"}` were written into
 * `provider.absoluteEndpointOverride`), and never read `image_endpoint` at all.
 *
 * The dialect now lives ONCE, in [ModelUseProtocol]. Every expectation below is
 * a LITERAL taken from the strict in-process behaviour (the definition of
 * "correct" — it is what the iOS bridge and the help text describe), never
 * derived from the implementation.
 */
class ModelUseEnvelopeDialectTest {

    private fun warn() = mutableListOf<String>()

    // ── parseExtraHeaders: envelope handling ─────────────────────────────

    @Test
    fun `extra_headers absent empty or json null yields empty map with no warning`() {
        for (json in listOf("""{"messages":[]}""", """{"extra_headers":{}}""", """{"extra_headers":null}""")) {
            val w = warn()
            assertTrue(ModelUseProtocol.parseExtraHeaders(json, w).isEmpty())
            assertTrue("no warning expected for <$json>: $w", w.isEmpty())
        }
    }

    @Test
    fun `extra_headers present but not an object yields empty map with exactly one warning`() {
        for (json in listOf(
            """{"extra_headers":["Authorization: Bearer x"]}""",
            """{"extra_headers":"x-a: 1"}""",
            """{"extra_headers":7}""",
        )) {
            val w = warn()
            assertTrue(ModelUseProtocol.parseExtraHeaders(json, w).isEmpty())
            assertEquals("exactly one warning expected for <$json>: $w", 1, w.size)
            assertTrue(
                "warning must name the actual type: $w",
                w[0].startsWith("extra_headers is not a JSON object (actual type: "),
            )
        }
    }

    @Test
    fun `extra_headers string values survive`() {
        val w = warn()
        val out = ModelUseProtocol.parseExtraHeaders("""{"extra_headers":{"x-a":"1","x-b":"2"}}""", w)
        assertEquals(mapOf("x-a" to "1", "x-b" to "2"), out)
        assertTrue("no warnings expected: $w", w.isEmpty())
    }

    @Test
    fun `extra_headers non-string value is dropped WITH a warning`() {
        val w = warn()
        val out = ModelUseProtocol.parseExtraHeaders("""{"extra_headers":{"x-a":"1","x-retry":3}}""", w)
        assertEquals(mapOf("x-a" to "1"), out)
        assertEquals(1, w.size)
        assertTrue(
            "warning must name the key and the actual type: $w",
            w[0].startsWith("extra_headers.x-retry value is not a string (actual type: number)"),
        )
    }

    @Test
    fun `malformed or non-json input yields empty map with no warning`() {
        for (json in listOf("""{"extra_headers":{""", "just a prompt")) {
            val w = warn()
            assertTrue(ModelUseProtocol.parseExtraHeaders(json, w).isEmpty())
            assertTrue("no warning expected for <$json>: $w", w.isEmpty())
        }
    }

    // ── parseCustomEndpointPath ──────────────────────────────────────────

    @Test
    fun `absolute endpoint path is returned verbatim`() {
        assertEquals("/v2/chat", ModelUseProtocol.parseCustomEndpointPath("""{"endpoint":"/v2/chat"}"""))
        assertEquals(
            "/v3/messages",
            ModelUseProtocol.parseCustomEndpointPath("""{"endpoint":"  /v3/messages  "}"""),
        )
    }

    @Test
    fun `image_endpoint wins over endpoint`() {
        assertEquals(
            "/img/gen",
            ModelUseProtocol.parseCustomEndpointPath("""{"image_endpoint":"/img/gen","endpoint":"/v2/chat"}"""),
        )
        assertEquals("/img/gen", ModelUseProtocol.parseCustomEndpointPath("""{"image_endpoint":"/img/gen"}"""))
    }

    @Test
    fun `symbolic endpoint modes are NOT paths`() {
        for (mode in listOf("auto", "chat", "images-gen")) {
            assertNull(
                "symbolic endpoint mode <$mode> must return null",
                ModelUseProtocol.parseCustomEndpointPath("""{"endpoint":"$mode"}"""),
            )
        }
        // A symbolic image_endpoint is still selected (non-empty wins) and is
        // not a path — it shadows the valid endpoint behind it. This is the
        // pre-existing in-process dialect, preserved verbatim.
        assertNull(
            ModelUseProtocol.parseCustomEndpointPath("""{"image_endpoint":"auto","endpoint":"/x"}"""),
        )
    }

    @Test
    fun `null empty number or absent endpoint yields null`() {
        assertNull(ModelUseProtocol.parseCustomEndpointPath("""{"endpoint":null}"""))
        assertNull(ModelUseProtocol.parseCustomEndpointPath("""{"endpoint":""}"""))
        assertNull(ModelUseProtocol.parseCustomEndpointPath("""{"endpoint":123}"""))
        assertNull(ModelUseProtocol.parseCustomEndpointPath("""{"messages":[]}"""))
        assertNull(ModelUseProtocol.parseCustomEndpointPath("not json at all"))
    }

    // ── structural: one implementation, two delegating call sites ────────

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
    fun `both processes delegate the envelope dialect to ModelUseProtocol`() {
        val handler = readRepoFile(
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ModelUseOffloadHandler.kt",
        ) ?: return // tree not on disk → skip
        val service = readRepoFile(
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ModelExecutionService.kt",
        ) ?: return
        for ((name, text) in listOf("ModelUseOffloadHandler.kt" to handler, "ModelExecutionService.kt" to service)) {
            assertTrue("$name must delegate parseExtraHeaders to ModelUseProtocol", text.contains("ModelUseProtocol.parseExtraHeaders("))
            assertTrue("$name must delegate parseCustomEndpointPath to ModelUseProtocol", text.contains("ModelUseProtocol.parseCustomEndpointPath("))
            assertFalse(
                "$name carries a second extra_headers dialect again — optJSONObject silent drop",
                text.contains("""optJSONObject("extra_headers")"""),
            )
            assertFalse(
                "$name carries a private jsonTypeName copy again",
                text.contains("private fun jsonTypeName"),
            )
        }
    }
}

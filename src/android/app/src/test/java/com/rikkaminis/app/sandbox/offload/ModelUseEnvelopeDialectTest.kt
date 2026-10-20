package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
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

    // ── [dv14-A4] ISSUE-08: the IMAGE passthrough dialect, one source ────
    //
    // Same defect one layer over: implicit top-level keys (minus the reserved
    // set) + the explicit `extra_body` / `extra_headers` / `endpoint_path`
    // envelope was written THREE times, each copy with its own 17-key
    // `imageReservedKeys` set, and two of the three copies were already
    // unreachable (one dead, one feeding a reader nobody called).
    // `ModelUseProtocol.parseImagePassthrough` is the single source now.

    @Test
    fun `implicit top-level keys fold into the body and reserved keys never do`() {
        val spec = ModelUseProtocol.parseImagePassthrough(
            """{"prompt":"a cat","image":"data:image/png;base64,AAA","size":"1024x1024","n":1}""",
        )
        // `prompt`, `size` and `n` are schema keys the image call already
        // consumes — they must NOT be smuggled into the passthrough body.
        assertEquals(mapOf("image" to "data:image/png;base64,AAA"), spec.body)
        assertTrue("no warning expected: ${spec.warnings}", spec.warnings.isEmpty())
        assertTrue(spec.headers.isEmpty())
        assertNull(spec.endpointPath)
    }

    @Test
    fun `an explicit extra_body beats an implicit top-level key of the same name`() {
        val spec = ModelUseProtocol.parseImagePassthrough(
            """{"image":"implicit","extra_body":{"image":"explicit","seed":7}}""",
        )
        assertEquals("explicit", spec.body["image"])
        assertEquals(7, (spec.body["seed"] as Number).toInt())
        assertEquals(2, spec.body.size)
        assertTrue("no warning expected: ${spec.warnings}", spec.warnings.isEmpty())
    }

    @Test
    fun `a nested extra_body value travels verbatim, not stringified`() {
        val spec = ModelUseProtocol.parseImagePassthrough(
            """{"extra_body":{"plugins":[{"id":"web"}],"watermark":false}}""",
        )
        assertEquals("""[{"id":"web"}]""", spec.body["plugins"].toString())
        assertEquals(false, spec.body["watermark"])
    }

    @Test
    fun `a non-object extra_body is reported instead of silently dropped`() {
        // Before: the worker read `optJSONObject("extra_body")`, so an array
        // (or a string) was dropped with ZERO feedback — the exact silent
        // shape da1019 fixed for extra_headers.
        for (json in listOf("""{"image":"x","extra_body":["a"]}""", """{"image":"x","extra_body":"y"}""")) {
            val spec = ModelUseProtocol.parseImagePassthrough(json)
            assertEquals(mapOf("image" to "x"), spec.body)
            assertEquals("exactly one warning for <$json>", 1, spec.warnings.size)
            assertTrue(
                "the warning must name the actual type: ${spec.warnings}",
                spec.warnings[0].startsWith("extra_body is not a JSON object (actual type: "),
            )
        }
    }

    @Test
    fun `extra_headers and endpoint_path are part of the same parse`() {
        val spec = ModelUseProtocol.parseImagePassthrough(
            """{"extra_headers":{"x-a":"1"},"endpoint_path":" /v2/images "}""",
        )
        assertEquals(mapOf("x-a" to "1"), spec.headers)
        assertEquals("/v2/images", spec.endpointPath)
        assertTrue(spec.body.isEmpty())
        assertTrue("no warning expected: ${spec.warnings}", spec.warnings.isEmpty())
    }

    @Test
    fun `endpoint_path is reserved so it never leaks into the body`() {
        val spec = ModelUseProtocol.parseImagePassthrough(
            """{"endpoint_path":"/v2/images","image_endpoint":"/v1/i","endpoint":"chat"}""",
        )
        assertTrue("every key here is reserved: ${spec.body}", spec.body.isEmpty())
        assertEquals("/v2/images", spec.endpointPath)
        assertNull(ModelUseProtocol.parseImagePassthrough("""{"endpoint_path":"   "}""").endpointPath)
    }

    @Test
    fun `non-json input yields the empty spec`() {
        val spec = ModelUseProtocol.parseImagePassthrough("just a prompt")
        assertTrue(spec.body.isEmpty())
        assertTrue(spec.headers.isEmpty())
        assertNull(spec.endpointPath)
        assertTrue(spec.warnings.isEmpty())
    }

    @Test
    fun `both processes delegate the image dialect to ModelUseProtocol`() {
        // Fail-closed on purpose: a structural check that silently skips when
        // it cannot find the tree is not evidence (the sibling envelope check
        // still returns early — registered, not changed here).
        val handlerRel =
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ModelUseOffloadHandler.kt"
        val serviceRel =
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ModelExecutionService.kt"
        val handler = readRepoFile(handlerRel) ?: run {
            fail("cannot locate $handlerRel from user.dir=${System.getProperty("user.dir")}")
            return
        }
        val service = readRepoFile(serviceRel) ?: run {
            fail("cannot locate $serviceRel from user.dir=${System.getProperty("user.dir")}")
            return
        }
        // The worker owns the image path (`decideImageRoute` is called there),
        // so it must go through the single parser rather than its own copy.
        assertTrue(
            "ModelExecutionService.kt must parse the image dialect via ModelUseProtocol",
            service.contains("ModelUseProtocol.parseImagePassthrough("),
        )
        val keyDef = Regex("""\bval\s+(imageReservedKeys|IMAGE_PASSTHROUGH_RESERVED_KEYS)\b""")
        for ((name, text) in listOf("ModelUseOffloadHandler.kt" to handler, "ModelExecutionService.kt" to service)) {
            assertFalse(
                "$name defines a second reserved-key set — it belongs to ModelUseProtocol alone",
                keyDef.containsMatchIn(text),
            )
            assertFalse(
                "$name re-reads extra_body silently again (optJSONObject drop)",
                text.contains("""optJSONObject("extra_body")"""),
            )
        }
    }

    // ── structural: one implementation, two delegating call sites ────────

    private fun readRepoFile(relative: String): String? {
        // [dv14-A4] ISSUE-08: -Dprobe.repo first, so the structural checks can
        // be driven deterministically outside Gradle (the sandbox arms point it
        // at a tree; Gradle leaves it unset and the walk below resolves).
        System.getProperty("probe.repo")?.let { probe ->
            val f = File(probe, relative)
            if (f.exists()) return f.readText()
        }
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

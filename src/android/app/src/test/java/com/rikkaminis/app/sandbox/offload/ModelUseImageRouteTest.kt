package com.rikkaminis.app.sandbox.offload

import com.rikkaminis.app.data.model.ImageEndpointMode
import com.rikkaminis.app.data.model.ProviderCredential
import com.rikkaminis.app.data.model.ProviderType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/audit3-1015 F-10] The image-route decision, as a table.
 *
 * Ground truth is the routing the CLI side (`:app`'s deleted
 * `tryImageGenerationRoute`) always implemented: pure-image pinning, endpoint
 * mode resolution, input-image handling. The worker's copy only had an
 * `outputExt in ("png", …)` test and fell back to chat on ANY failure — the
 * three gaps this fixes. Every expectation is a literal, not derived from the
 * implementation.
 */
class ModelUseImageRouteTest {

    private fun decide(
        modalities: List<String> = listOf("image", "text"),
        ext: String? = null,
        inputImages: Boolean = false,
        type: ProviderType? = ProviderType.openAI,
        cred: ProviderCredential = ProviderCredential.apiKey,
        compat: Boolean = true,
        mode: ImageEndpointMode = ImageEndpointMode.auto,
        resolved: ImageEndpointMode? = null,
        override: ImageEndpointMode? = null,
    ) = ModelUseProtocol.decideImageRoute(
        displayName = "test-model",
        outputModalities = modalities,
        outputExt = ext,
        hasInputImages = inputImages,
        providerType = type,
        credentialType = cred,
        isOpenAiCompatibleProvider = compat,
        imageEndpointMode = mode,
        imageEndpointResolved = resolved,
        endpointOverride = override,
    )

    private fun gen(mode: ImageEndpointMode) = ModelUseProtocol.ImageRoute.Generate(mode)
    private val chatPath = ModelUseProtocol.ImageRoute.ChatPath

    // ── who may route at all ────────────────────────────────────────────

    @Test
    fun `non openai-compatible providers never route to images`() {
        assertEquals(chatPath, decide(type = ProviderType.gemini, ext = "png"))
        assertEquals(chatPath, decide(type = ProviderType.gemini, modalities = listOf("image")))
        assertEquals(chatPath, decide(type = ProviderType.anthropic, ext = "png"))
        assertEquals(chatPath, decide(type = ProviderType.kimiCode, ext = "png"))
        assertEquals(chatPath, decide(compat = false, ext = "png"))
    }

    @Test
    fun `a model without image output never routes`() {
        assertEquals(chatPath, decide(modalities = listOf("text"), ext = "png"))
        assertEquals(chatPath, decide(modalities = emptyList(), ext = null))
    }

    @Test
    fun `openrouter and xai are in the allow-list, oauth is not`() {
        assertEquals(gen(ImageEndpointMode.auto), decide(type = ProviderType.openRouter, ext = "png"))
        assertEquals(gen(ImageEndpointMode.auto), decide(type = ProviderType.xAI, ext = "png"))
        assertEquals(chatPath, decide(cred = ProviderCredential.oauth, ext = "png"))
    }

    // ── [F-10 gap 1] pure image generators route WITHOUT -o ─────────────

    @Test
    fun `pure image generator routes without a media output ext`() {
        // Pre-fix: ChatPath (sent to the chat endpoint, which cannot serve it).
        assertEquals(gen(ImageEndpointMode.imagesGenerations), decide(modalities = listOf("image"), ext = null))
        assertEquals(gen(ImageEndpointMode.imagesGenerations), decide(modalities = listOf("image"), ext = "png"))
    }

    @Test
    fun `mixed models keep the -o trigger`() {
        assertEquals(chatPath, decide(modalities = listOf("image", "text"), ext = null))
        assertEquals(gen(ImageEndpointMode.auto), decide(modalities = listOf("image", "text"), ext = "png"))
        assertEquals(gen(ImageEndpointMode.auto), decide(modalities = listOf("image", "text"), ext = "JPG"))
    }

    // ── [F-10 gap 2] input images must not be silently dropped ──────────

    @Test
    fun `input image on a pure image generator is a terminal reject`() {
        val d = decide(modalities = listOf("image"), ext = null, inputImages = true)
        assertTrue("expected Reject, got $d", d is ModelUseProtocol.ImageRoute.Reject)
        assertEquals("image_edit_not_supported", (d as ModelUseProtocol.ImageRoute.Reject).error)
        assertTrue(d.message.contains("not supported"))
    }

    @Test
    fun `input image on a mixed model goes to chat (images forwarded)`() {
        assertEquals(
            chatPath,
            decide(modalities = listOf("image", "text"), ext = "png", inputImages = true),
        )
    }

    // ── [F-10 gap 3] endpoint mode resolution ───────────────────────────

    @Test
    fun `the pure-image pin beats a stale chat_completions cache`() {
        // A pure image generator pinned to chatCompletions by a stale probe
        // cache would be sent to an endpoint that cannot serve it.
        assertEquals(
            gen(ImageEndpointMode.imagesGenerations),
            decide(
                modalities = listOf("image"),
                ext = null,
                mode = ImageEndpointMode.auto,
                resolved = ImageEndpointMode.chatCompletions,
            ),
        )
    }

    @Test
    fun `a resolved cache answer is honored for mixed models`() {
        assertEquals(
            chatPath,
            decide(
                modalities = listOf("image", "text"),
                ext = "png",
                mode = ImageEndpointMode.auto,
                resolved = ImageEndpointMode.chatCompletions,
            ),
        )
        assertEquals(
            gen(ImageEndpointMode.imagesGenerations),
            decide(
                modalities = listOf("image", "text"),
                ext = "png",
                mode = ImageEndpointMode.auto,
                resolved = ImageEndpointMode.imagesGenerations,
            ),
        )
    }

    @Test
    fun `auto with no cached answer routes as auto`() {
        assertEquals(
            gen(ImageEndpointMode.auto),
            decide(modalities = listOf("image", "text"), ext = "png", mode = ImageEndpointMode.auto, resolved = null),
        )
    }

    @Test
    fun `a configured images_generations endpoint routes as images_generations`() {
        assertEquals(
            gen(ImageEndpointMode.imagesGenerations),
            decide(mode = ImageEndpointMode.imagesGenerations, ext = "png"),
        )
    }

    @Test
    fun `an explicit endpoint override picks the endpoint once the route is triggered`() {
        // Override wins over the pure-image pin (the caller explicitly said
        // "this model is not an images model").
        assertEquals(
            chatPath,
            decide(
                modalities = listOf("image"),
                ext = null,
                mode = ImageEndpointMode.imagesGenerations,
                override = ImageEndpointMode.chatCompletions,
            ),
        )
        // ...and over the cached answer, in both directions.
        assertEquals(
            gen(ImageEndpointMode.imagesGenerations),
            decide(modalities = listOf("image", "text"), ext = "png", override = ImageEndpointMode.imagesGenerations),
        )
        assertEquals(
            chatPath,
            decide(modalities = listOf("image", "text"), ext = "png", override = ImageEndpointMode.chatCompletions),
        )
    }

    @Test
    fun `an override does not manufacture a route the call did not ask for`() {
        // A mixed model with no `-o <media ext>` is a text call: there is no
        // output file to write an image to, so the override has nothing to
        // pick between. (The baseline app-side route had no ext gate of its
        // own — it lived at the call site — and the service's ext gate is the
        // caller-facing contract: `-o out.png` means "I want an image".)
        assertEquals(
            chatPath,
            decide(modalities = listOf("image", "text"), ext = null, override = ImageEndpointMode.imagesGenerations),
        )
        assertEquals(
            chatPath,
            decide(
                modalities = listOf("image", "text"),
                ext = null,
                mode = ImageEndpointMode.imagesGenerations,
                override = ImageEndpointMode.imagesGenerations,
            ),
        )
    }

    // ── parseImageGenConfig ─────────────────────────────────────────────

    @Test
    fun `top-level image params win over generation_config`() {
        val c = ModelUseProtocol.parseImageGenConfig(
            """{"prompt":"top","size":"1792x1024","quality":"hd","n":2,
                "generation_config":{"prompt":"gc","image_size":"2K","quality":"low","number_of_images":4}}""",
        )
        assertEquals("top", c.prompt)
        assertEquals("1792x1024", c.size)
        assertEquals("hd", c.quality)
        assertEquals(2, c.n)
    }

    @Test
    fun `generation_config knobs are read when no top-level key exists`() {
        val c = ModelUseProtocol.parseImageGenConfig(
            """{"generation_config":{"prompt":"gc prompt","image_size":"2K","number_of_images":3}}""",
        )
        assertEquals("gc prompt", c.prompt)
        assertEquals("2K", c.size)
        assertEquals(3, c.n)
    }

    @Test
    fun `image_endpoint symbolic names parse`() {
        fun ep(json: String) = ModelUseProtocol.parseImageGenConfig(json).endpointOverride
        assertEquals(ImageEndpointMode.imagesGenerations, ep("""{"image_endpoint":"images_generations"}"""))
        assertEquals(ImageEndpointMode.imagesGenerations, ep("""{"endpoint":"images-gen"}"""))
        assertEquals(ImageEndpointMode.imagesGenerations, ep("""{"endpoint":"images-generations"}"""))
        assertEquals(ImageEndpointMode.chatCompletions, ep("""{"image_endpoint":"chat_completions"}"""))
        assertEquals(ImageEndpointMode.chatCompletions, ep("""{"endpoint":"chat"}"""))
        assertEquals(ImageEndpointMode.auto, ep("""{"image_endpoint":"auto"}"""))
        assertEquals(null, ep("""{"endpoint":"nonsense"}"""))
        assertEquals(
            ImageEndpointMode.imagesGenerations,
            ep("""{"generation_config":{"image_endpoint":"images_generations"}}"""),
        )
    }

    @Test
    fun `malformed or empty input yields defaults`() {
        val c = ModelUseProtocol.parseImageGenConfig("not json")
        assertEquals(null, c.prompt)
        assertEquals(1, c.n)
        assertEquals(null, c.size)
        assertEquals(null, c.quality)
        assertEquals(null, c.endpointOverride)
        assertEquals(1, ModelUseProtocol.parseImageGenConfig("{}").n)
    }

    // ── helpers ─────────────────────────────────────────────────────────

    @Test
    fun `isPureImageGenerator is case-insensitive and needs no text`() {
        assertTrue(ModelUseProtocol.isPureImageGenerator(listOf("image")))
        assertTrue(ModelUseProtocol.isPureImageGenerator(listOf("IMAGE")))
        assertTrue(ModelUseProtocol.isPureImageGenerator(listOf("image", "Image")))
        assertFalse(ModelUseProtocol.isPureImageGenerator(listOf("image", "text")))
        assertFalse(ModelUseProtocol.isPureImageGenerator(listOf("text")))
        assertFalse(ModelUseProtocol.isPureImageGenerator(emptyList()))
    }

    @Test
    fun `isImageOutputExt covers the media extensions`() {
        for (ext in listOf("png", "jpg", "jpeg", "webp", "gif", "PNG", "WebP")) {
            assertTrue(ext, ModelUseProtocol.isImageOutputExt(ext))
        }
        assertFalse(ModelUseProtocol.isImageOutputExt(null))
        assertFalse(ModelUseProtocol.isImageOutputExt(""))
        assertFalse(ModelUseProtocol.isImageOutputExt("txt"))
        assertFalse(ModelUseProtocol.isImageOutputExt("mp3"))
    }

    @Test
    fun `looksLikeEndpointMissing matches route-shaped failures only`() {
        for (m in listOf("HTTP 404", "endpoint not found", "no such endpoint", "method not allowed")) {
            assertTrue(m, ModelUseProtocol.looksLikeEndpointMissing(m))
        }
        for (m in listOf("Missing scopes", "invalid api key", "content moderation", "timeout", "")) {
            assertFalse(m, ModelUseProtocol.looksLikeEndpointMissing(m))
        }
    }

    @Test
    fun `imageParamHint speaks the right dialect per provider`() {
        val image = listOf("image", "text")
        assertTrue(ModelUseProtocol.imageParamHint("m", ProviderType.gemini, image).contains("generation_config"))
        assertTrue(ModelUseProtocol.imageParamHint("m", ProviderType.openAI, image).contains("images/generations"))
        assertTrue(ModelUseProtocol.imageParamHint("m", ProviderType.openRouter, image).contains("images/generations"))
        assertEquals("", ModelUseProtocol.imageParamHint("m", ProviderType.anthropic, image))
        assertEquals("", ModelUseProtocol.imageParamHint("m", ProviderType.openAI, listOf("text")))
        assertEquals("", ModelUseProtocol.imageParamHint("m", null, image))
    }
}

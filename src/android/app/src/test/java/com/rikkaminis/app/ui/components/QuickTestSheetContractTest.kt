package com.rikkaminis.app.ui.components

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [deep-audit-1031 ISSUE-08] Source-shape probe for the Quick Test sheet.
 *
 * All three defects of this issue are *shape* defects inside one Compose file,
 * and the sandbox cannot compile a Compose screen, so — exactly like
 * `A11yRecoverySlotShapeTest` (1027-E) — the properties are asserted on the
 * production text, with every expectation written as a literal:
 *
 * 1. the `IMAGE_GEN` branch no longer re-decides provider eligibility with its
 *    own copy of the rule (`providerType == openAI`), it calls
 *    `ModelUseProtocol.supportsImageApiRoute` — the predicate `decideImageRoute`
 *    itself applies, so the card cannot disagree with the worker again;
 * 2. the same branch persists the worker's `image_endpoint_resolved` answer
 *    (it used to be dropped, so every image test re-probed);
 * 3. `AudioReplyContent` keys the clip's deletion on the file, not on `Unit`
 *    (the Unit-keyed effect only ever saw the LAST clip, so each re-test leaked
 *    its predecessor in cacheDir).
 *
 * [violations] returns one reason per arm, so each arm asserts its own failure
 * reason; the pre-fix fixture below must violate all of them, which is what
 * stops a probe that has quietly stopped looking from passing.
 */
class QuickTestSheetContractTest {

    private val sheetRel =
        "src/main/java/com/rikkaminis/app/ui/components/QuickTestSheet.kt"

    /** The pre-fix shapes, quoted from the audit (kept verbatim on purpose). */
    private val preFix = """
        QuickTestKind.IMAGE_GEN -> {
            if (instance.providerType != com.rikkaminis.app.data.model.ProviderType.openAI) {
                return@withContext failure(context.getString(R.string.quicktest_image_unsupported))
            }
            runCatching {
                when (val r = com.rikkaminis.app.sandbox.offload.ProviderExecutionGateway.generateImage(
                    context = context,
                )) {
                    is com.rikkaminis.app.sandbox.offload.ProviderExecutionGateway.SendResult.Success -> {
                        val img = r.response.mediaAttachments.firstOrNull {
                            it.type == LLMMediaAttachment.MediaType.IMAGE
                        } ?: return@runCatching failure(context.getString(R.string.quicktest_no_image))
                        QuickTestState.ImageReply(img.data)
                    }
                }
            }
        }

        androidx.compose.runtime.DisposableEffect(Unit) {
            onDispose {
                runCatching { player.release() }
                runCatching { file.delete() }
            }
        }
    """.trimIndent()

    // ── probe ────────────────────────────────────────────────────────────────

    private fun sourceText(rel: String): String {
        val viaAndroid = "src/android/app/$rel"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val here = dir
            val direct = File(here, rel)
            if (direct.isFile) return direct.readText()
            val module = File(here, viaAndroid)
            if (module.isFile) return module.readText()
            dir = here.parentFile
        }
        throw AssertionError("QuickTestSheet.kt not found from ${File(".").absolutePath}")
    }

    /** The IMAGE_GEN branch: from its marker to the SPEECH_OUT terminal branch. */
    private fun imageGenBranch(src: String): String {
        val start = src.indexOf("QuickTestKind.IMAGE_GEN ->")
        // Search forward from the branch: SPEECH_OUT also appears earlier in
        // the file (the kind list and the can-run gate).
        val end = src.indexOf("QuickTestKind.SPEECH_OUT", start)
        if (start < 0 || end <= start) throw AssertionError("IMAGE_GEN branch not found (probe is blind)")
        return src.substring(start, end)
    }

    /** The whole `AudioReplyContent` composable body (up to the next function). */
    private fun audioReply(src: String): String {
        val start = src.indexOf("private fun AudioReplyContent")
        val end = src.indexOf("private fun runAll(", start)
        if (start < 0 || end <= start) throw AssertionError("AudioReplyContent not found (probe is blind)")
        return src.substring(start, end)
    }

    /** Every way this sheet can regress — one reason string per arm. */
    private fun violations(imageGen: String, audio: String): List<String> = buildList {
        if (imageGen.contains("ProviderType.openAI")) {
            add("IMAGE_GEN re-decides eligibility with its own hardcoded provider type")
        }
        if (!imageGen.contains("supportsImageApiRoute(")) {
            add("IMAGE_GEN does not call ModelUseProtocol.supportsImageApiRoute")
        }
        if (!imageGen.contains("\"image_endpoint_resolved\"")) {
            add("IMAGE_GEN drops the worker's image_endpoint_resolved answer")
        }
        if (!imageGen.contains("setImageEndpointResolved(")) {
            add("IMAGE_GEN never persists the resolved image endpoint")
        }
        if (!audio.contains("DisposableEffect(file)")) {
            add("clip deletion is not keyed by the file")
        }
        if (audio.substringAfter("DisposableEffect(Unit)").take(160).contains("file.delete()")) {
            add("clip deletion is keyed by Unit (leaks the clip on every re-test)")
        }
        if (!audio.contains("LaunchedEffect(file)")) {
            add("the clip write/auto-play path is gone")
        }
    }

    // ── arms ─────────────────────────────────────────────────────────────────

    @Test
    fun `the live sheet violates no arm`() {
        val src = sourceText(sheetRel)
        val found = violations(imageGenBranch(src), audioReply(src))
        assertEquals("shape violations: $found", emptyList<String>(), found)
    }

    @Test
    fun `the pre-fix shape violates every arm`() {
        // Teeth: if the probe stopped looking, the fixture would come back clean.
        val src = sourceText(sheetRel)
        val injected = violations(
            imageGen = preFix.substring(preFix.indexOf("QuickTestKind.IMAGE_GEN ->")),
            audio = preFix,
        )
        assertEquals("the pre-fix fixture must trip every arm", 7, injected.size)
        assertTrue(injected.any { it.contains("hardcoded provider type") })
        assertTrue(injected.any { it.contains("keyed by Unit") })
    }

    @Test
    fun `regression anchor - the TEXT branch send call is untouched`() {
        val src = sourceText(sheetRel)
        val start = src.indexOf("QuickTestKind.TEXT ->")
        val end = src.indexOf("QuickTestKind.IMAGE_GEN ->")
        assertTrue("TEXT branch not found (probe is blind)", start >= 0 && end > start)
        val textBranch = src.substring(start, end)
        assertTrue(textBranch.contains("com.rikkaminis.app.sandbox.offload.ProviderExecutionGateway.send("))
        assertTrue(textBranch.contains("maxTokens = 128,"))
        assertTrue(textBranch.contains("temperature = null,"))
        assertTrue(textBranch.contains("Say hello back in one short, friendly sentence."))
    }
}

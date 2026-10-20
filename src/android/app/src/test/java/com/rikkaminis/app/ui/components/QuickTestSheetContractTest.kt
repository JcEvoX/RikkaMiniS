package com.rikkaminis.app.ui.components

import com.rikkaminis.app.data.model.ProviderConfig
import com.rikkaminis.app.data.model.ProviderCredential
import com.rikkaminis.app.data.model.ProviderInstance
import com.rikkaminis.app.data.model.ProviderType
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

    // ── [deep-audit-v14 ISSUE-05] shared collapsed-provider state ────────────
    //
    // Both screens that render `modelEntryPickerItems` used to keep their own
    // `val collapsedInstanceIds = remember(config) { mutableStateOf(…config.instances…) }`
    // copy. `ProviderConfig` is a data class whose `revision` is bumped by
    // `ProviderRepository.saveConfig` on EVERY mutation (see the T273 note on
    // the field), so the config object is a different value after any write —
    // a Quick Test persisting `imageEndpointResolved`, `refreshModels` landing,
    // a DTO → StateFlow re-emit — and the collapse choice of a user who was
    // mid-pick was wiped. The state now lives in one shared source; the two
    // screen shapes are asserted here, the state's key decision is asserted by
    // calling the pure derivation below.

    private val addModelsRel =
        "src/main/java/com/rikkaminis/app/ui/settings/AddModelsToGroupScreen.kt"
    private val agentLoopSheetsRel =
        "src/main/java/com/rikkaminis/app/ui/settings/AgentLoopAddSheets.kt"

    /** The pre-fix shapes, quoted from the audit (kept verbatim on purpose). */
    private val collapsedPreFix = """
        val collapsedInstanceIds = remember(config) {
            mutableStateOf(
                if (modalityFilter != null) emptySet()
                else config.instances.map { it.id }.toSet(),
            )
        }
    """.trimIndent()

    /** Every way the collapse-state wiring can regress — one reason per arm. */
    private fun collapsedViolations(source: String, expectsVoiceFlag: Boolean): List<String> =
        buildList {
            if (!source.contains("rememberCollapsedInstanceIds(")) {
                add("the screen does not seed the collapse state through the shared single source")
            }
            if (source.contains("collapsedInstanceIds = remember(")) {
                add("the screen still keeps a private collapse-state remember")
            }
            if (expectsVoiceFlag && !source.contains("defaultExpanded = modalityFilter != null")) {
                add("the voice scenario no longer starts fully expanded ([T-android-provider-voice])")
            }
        }

    private fun instance(id: String, enabled: Boolean = true) = ProviderInstance(
        id = id,
        label = id.uppercase(),
        providerType = ProviderType.openAI,
        credentialType = ProviderCredential.apiKey,
        isEnabled = enabled,
    )

    @Test
    fun `the picker screens delegate the collapse state instead of copying it`() {
        val addModels = sourceText(addModelsRel)
        val agentLoop = sourceText(agentLoopSheetsRel)

        val viaAdd = collapsedViolations(addModels, expectsVoiceFlag = true)
        assertEquals("AddModelsToGroupScreen violations: $viaAdd", emptyList<String>(), viaAdd)
        val viaAgentLoop = collapsedViolations(agentLoop, expectsVoiceFlag = false)
        assertEquals("AgentLoopAddSheets violations: $viaAgentLoop", emptyList<String>(), viaAgentLoop)
    }

    @Test
    fun `the pre-fix collapse shape violates every arm`() {
        // Teeth: if the probe stopped looking, this fixture would come back clean.
        val injected = collapsedViolations(collapsedPreFix, expectsVoiceFlag = true)
        assertEquals("the pre-fix fixture must trip every arm", 3, injected.size)
        assertTrue(injected.any { it.contains("private collapse-state") })
        assertTrue(injected.any { it.contains("single source") })
        assertTrue(injected.any { it.contains("voice scenario") })
    }

    @Test
    fun `the collapse state key ignores writes that are not instance changes`() {
        val before = ProviderConfig(instances = mutableListOf(instance("a"), instance("b")))

        assertEquals(setOf("a", "b"), providerInstanceIds(before))

        // saveConfig bumps `revision` on every mutation: a different config
        // value, but not a different instance set — the state must survive.
        val afterUnrelatedWrite = before.copy(revision = before.revision + 1)
        assertEquals(providerInstanceIds(before), providerInstanceIds(afterUnrelatedWrite))

        // toggling a provider is not an instance add/remove either
        val toggled = before.copy(instances = mutableListOf(instance("a", enabled = false), instance("b")))
        assertEquals(providerInstanceIds(before), providerInstanceIds(toggled))

        // an actual add re-seeds (the newcomer starts collapsed), as before
        val added = before.copy(
            instances = mutableListOf(instance("a"), instance("b"), instance("c")))
        assertEquals(setOf("a", "b", "c"), providerInstanceIds(added))
    }

    @Test
    fun `the collapse seed starts fully expanded only in a voice scenario`() {
        // [T-android-provider-voice]: don't fold a 39-voice Azure provider
        // behind a disclosure when the user opened the picker to pick a voice.
        assertEquals(emptySet<String>(), collapsedInstanceIdsSeed(setOf("a", "b"), defaultExpanded = true))
        assertEquals(setOf("a", "b"), collapsedInstanceIdsSeed(setOf("a", "b"), defaultExpanded = false))
        assertEquals(emptySet<String>(), collapsedInstanceIdsSeed(emptySet(), defaultExpanded = false))
    }
}

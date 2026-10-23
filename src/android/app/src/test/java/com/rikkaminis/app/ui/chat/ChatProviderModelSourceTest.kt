package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [deep-audit-1044 B-03/B-04] Every `ProviderFactory.create(…)` in the chat
 * layer must hand over an ENRICHED model, and the new-chat fallback must
 * publish the model it resolved.
 *
 * Why a source probe instead of a behavioural test: the request path ends in
 * `:modelservice`, a separate process that never initialises
 * `ModelsDevApi` — it only ever sees the serialised `LLMModel`. The provider
 * then re-decides vision (`"image" in model.inputModalities`), the thinking
 * tier (`clampThinkingLevel` / `injectThinkingParams`) and the output cap FROM
 * that object, so an un-enriched model silently downgrades attachments to
 * `[attached image: …]` and clamps a MAX pick back to XHIGH. The defect is
 * "which model object was handed over", which is exactly what the source
 * says. (The ordering half of the contract — enrich the base first, fold the
 * overrides second — is executable and pinned by `ThinkingCeilingEnrichmentTest`.)
 *
 * Negative controls below keep each probe honest: a synthetic pre-fix source
 * must be REJECTED by the same reader the real tree is judged with.
 */
class ChatProviderModelSourceTest {

    private val lifecycle = "src/main/java/com/rikkaminis/app/ui/chat/ChatSessionLifecycle.kt"
    private val routing = "src/main/java/com/rikkaminis/app/ui/chat/ChatModelRouting.kt"
    private val promptTools = "src/main/java/com/rikkaminis/app/ui/chat/ChatPromptAndTools.kt"
    private val viewModel = "src/main/java/com/rikkaminis/app/ui/chat/ChatViewModel.kt"

    private val createFiles = listOf(lifecycle, routing, promptTools, viewModel)

    /** `entry.model` = `baseModel.withOverrides(overrides)`: no models.dev data. */
    private val rawArgs = setOf("entry.model", "targetEntry.model", "cachedProvider.model")

    private val resolvers = listOf(
        "providerModelFor(",
        "liveEffectiveModel(",
        "ModelsDevApi.effectiveModel(",
    )

    private data class CallSite(val start: Int, val modelArg: String)

    @Test
    fun everyProviderConstructionSitePassesAnEnrichedModel() {
        var sites = 0
        for (rel in createFiles) {
            val code = stripComments(source(rel))
            for (call in createCallArgs(code)) {
                sites++
                val arg = call.modelArg
                assertFalse(
                    "$rel hands the raw `$arg` to ProviderFactory.create — that model carries no " +
                        "models.dev capabilities and `:modelservice` cannot fill them in " +
                        "(images degrade to a text placeholder, MAX thinking clamps to XHIGH, " +
                        "33 of the 39 built-ins lose their output cap)",
                    arg in rawArgs,
                )
                assertTrue(
                    "$rel: ProviderFactory.create model argument `$arg` is not resolved through " +
                        "$resolvers — route it through the one enriched exit",
                    resolvers.any { arg.contains(it) } || resolvedFromLocalVal(code, call.start, arg),
                )
            }
        }
        // Fail-closed: the four files carried 9 construction sites when this
        // probe was written (8 resolutions + the in-place route rebuild).
        assertTrue(
            "expected at least 8 ProviderFactory.create sites in the chat layer, found $sites — " +
                "detection lost (ROT)",
            sites >= 8,
        )
    }

    @Test
    fun theNewChatDefaultModelSitePublishesTheModelItResolved() {
        val code = stripComments(source(lifecycle))
        val body = functionBody(code, "internal fun ChatViewModel.applyNewChatDefaultModel(): Boolean {")
        val write = body.indexOf("currentModel =")
        assertTrue(
            "applyNewChatDefaultModel resolves a provider but never publishes `currentModel` — " +
                "with no default group a new chat then persists model_id \"unknown\", the " +
                "Token Usage Thinking block disappears and dynamicMaxTokens skips the " +
                "remaining-context clamp",
            write >= 0,
        )
        val decide = body.indexOf("decideSessionModelResolution(")
        assertTrue(
            "the model must be published only AFTER the usability decision " +
                "(decideSessionModelResolution)",
            decide in 0..write,
        )
    }

    @Test
    fun theLiveExitIsVisibleToTheExtensionReaders() {
        val code = stripComments(source(viewModel))
        assertTrue(
            "`liveEffectiveModel()` must be `internal`, not `private`: ChatErrorHandling." +
                "dynamicMaxTokens and ChatSlashTokenExt.thinkingInfo are extension functions in " +
                "other files — a private exit forces them to re-derive the lookup from the raw " +
                "`currentModel` snapshot",
            Regex("(?m)^[ \\t]*internal fun liveEffectiveModel\\(\\)\\s*:\\s*LLMModel\\?\\s*=")
                .containsMatchIn(code),
        )
    }

    // ── negative controls: the readers reject the pre-fix shapes ─────────────

    @Test
    fun theCreateSiteReaderRejectsARawModel() {
        val preFix = """
            internal fun ChatViewModel.rot() {
                currentProvider = ProviderFactory.create(instance, apiKey, entry.model, context)
            }
        """.trimIndent()
        val sites = createCallArgs(stripComments(preFix))
        assertEquals(1, sites.size)
        assertEquals("entry.model", sites[0].modelArg)
        assertTrue(sites[0].modelArg in rawArgs)
    }

    @Test
    fun theCreateSiteReaderAcceptsTheSharedResolver() {
        val fixed = """
            internal fun ChatViewModel.ok() {
                val effectiveModel = providerModelFor(entry)
                currentProvider = ProviderFactory.create(instance, apiKey, effectiveModel, context)
            }
        """.trimIndent()
        val code = stripComments(fixed)
        val site = createCallArgs(code).single()
        assertEquals("effectiveModel", site.modelArg)
        assertTrue(resolvers.none { site.modelArg.contains(it) })
        assertTrue(resolvedFromLocalVal(code, site.start, site.modelArg))
    }

    @Test
    fun thePublishReaderRejectsASilentSite() {
        val preFix = """
            internal fun ChatViewModel.applyNewChatDefaultModel(): Boolean {
                val decision = decideSessionModelResolution(entry, instance, apiKey)
                currentProvider = ProviderFactory.create(
                    decision.instance, decision.apiKey, providerModelFor(entry), context,
                )
                return true
            }
        """.trimIndent()
        val body = functionBody(stripComments(preFix),
            "internal fun ChatViewModel.applyNewChatDefaultModel(): Boolean {")
        assertFalse(body.contains("currentModel ="))
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /** Every `ProviderFactory.create(` call site with its third (model) argument. */
    private fun createCallArgs(code: String): List<CallSite> {
        val out = mutableListOf<CallSite>()
        val needle = "ProviderFactory.create("
        var from = 0
        while (true) {
            val i = code.indexOf(needle, from)
            if (i < 0) return out
            from = i + needle.length
            val args = splitArgs(code, i + needle.length - 1) ?: continue
            if (args.size >= 3) out.add(CallSite(i, args[2].trim()))
        }
    }

    /** Arguments of the call whose `(` sits at [openIdx]; null when unbalanced. */
    private fun splitArgs(code: String, openIdx: Int): List<String>? {
        val args = mutableListOf<String>()
        val cur = StringBuilder()
        var depth = 0
        var i = openIdx
        while (i < code.length) {
            val c = code[i]
            if (c == '(' || c == '[' || c == '{') {
                depth++
                if (depth == 1) {
                    i++
                    continue
                }
            } else if (c == ')' || c == ']' || c == '}') {
                depth--
                if (depth == 0) {
                    args.add(cur.toString())
                    return args
                }
            } else if (c == ',' && depth == 1) {
                args.add(cur.toString())
                cur.setLength(0)
                i++
                continue
            }
            cur.append(c)
            i++
        }
        return null
    }

    /** Is [arg] an identifier assigned from a resolver earlier in its function? */
    private fun resolvedFromLocalVal(code: String, callStart: Int, arg: String): Boolean {
        if (!Regex("[A-Za-z_][A-Za-z0-9_]*").matches(arg)) return false
        val window = code.substring(declStartBefore(code, callStart), callStart)
        val assign = Regex("\\b(?:val|var)\\s+" + Regex.escape(arg) + "\\b[^=\\n]*=")
        for (m in assign.findAll(window)) {
            val rest = window.substring(m.range.last + 1).split("\n")
            val rhs = (rest.getOrNull(0) ?: "") + "\n" + (rest.getOrNull(1) ?: "")
            if (resolvers.any { rhs.contains(it) }) return true
        }
        return false
    }

    private fun declStartBefore(code: String, pos: Int): Int {
        val decl = Regex("(?m)^[ \\t]*(?:internal |private |public |override |suspend )*fun ")
        var last = 0
        for (m in decl.findAll(code.substring(0, pos))) last = m.range.first
        return last
    }

    private fun functionBody(code: String, signature: String): String {
        val start = code.indexOf(signature)
        assertTrue("`$signature` not found — probe ROT", start >= 0)
        val end = code.indexOf("\n}", start)
        assertTrue("closing brace for `$signature` not found — probe ROT", end > start)
        return code.substring(start, end)
    }

    /** Comments out (KDoc is prose: it must neither satisfy nor trip a probe). */
    private fun stripComments(src: String): String {
        val noBlock = Regex("/\\*.*?\\*/", RegexOption.DOT_MATCHES_ALL).replace(src) { m ->
            "\n".repeat(m.value.count { it == '\n' })
        }
        return noBlock.split("\n").joinToString("\n") { it.substringBefore("//") }
    }

    private fun source(rel: String): String = sourceFile(rel).readText()

    private fun sourceFile(rel: String): File {
        val viaAndroid = "src/android/app/$rel"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel)
            if (File(d, viaAndroid).isFile) return File(d, viaAndroid)
            dir = d.parentFile
        }
        error("'$rel' not found from ${File(".").absoluteFile}")
    }
}

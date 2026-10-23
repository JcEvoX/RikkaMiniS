package com.rikkaminis.app.data.repository

import com.rikkaminis.app.data.model.LLMModel
import com.rikkaminis.app.data.model.ModelEntry
import com.rikkaminis.app.data.model.ModelOverrides
import com.rikkaminis.app.data.model.effectiveEntryModel
import com.rikkaminis.app.data.model.hasAudioInput
import com.rikkaminis.app.data.model.hasAudioOutput
import com.rikkaminis.app.data.model.hasVoiceModality
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1044 A-01] One entry, one capability answer — the *voice* half.
 *
 * A `ModelEntry` carries two models:
 *
 *  - [ModelEntry.model] = `baseModel.withOverrides(overrides)` — no models.dev
 *    lookup at all;
 *  - the effective one = `ModelsDevApi.effectiveModel(entry)` = enrich the base
 *    model FIRST, apply the user's overrides SECOND.
 *
 * Every one of the 39 built-in models leaves `inputModalities` /
 * `outputModalities` null by construction (the "absent means text in / text
 * out" convention, see `LLMModel.isTextOutput`), so the raw property answers
 * "no audio, yes text" for **every** model the registry knows to be
 * multimodal. `ProviderRepository` asked the raw one in eight places — the
 * Voice Input candidates dropped an audio-capable model (recording then failed
 * with「No provider voice-input model configured」), the Voice Services shadow
 * list reported "0 models", and a pure image generator could be chosen as the
 * new-chat default. `ProviderSpeechRecognitionEngine` handed the raw model to
 * the vendor adapters, which read capability off it.
 *
 * Two layers, house pattern ([com.rikkaminis.app.debug.DebugModelsEnrichmentTest],
 * `ProviderThinkingRulePublishOrderTest`):
 *
 *  1. *Behaviour, really executed*: the decision core — `effectiveEntryModel`
 *     plus the real `hasAudioInput` / `hasAudioOutput` / `hasVoiceModality` /
 *     `isTextOutput` predicates, driven with models.dev rows modelled on the
 *     registry's published values. The pre-fix read is pinned first, so the
 *     difference is the assertion, not a comment.
 *  2. *Wiring, as source shape*: [ProviderRepository] / the ASR engine need an
 *     Android Context and cannot be driven on the JVM, so the shipped source is
 *     read and probed — and every probe carries an in-test negative control
 *     ([probeHasTeeth]) that reverts one call site and requires the probe to
 *     reject it.
 *
 * Every expected value is a hand-written literal, never derived from the code
 * under test (a mutation must not be able to move an expectation with it).
 */
class VoiceModalityEffectiveModelTest {

    // ── fixtures ────────────────────────────────────────────────────────────

    /**
     * Stands in for the models.dev row: it fills the modality lists
     * unconditionally (`applyDevData` does), which is exactly why enrichment
     * has to happen BEFORE the overrides.
     */
    private val devRows: Map<String, LLMModel> = mapOf(
        // Multimodal chat model: the registry knows it takes audio in.
        "gemini-3-pro-preview" to LLMModel(
            id = "gemini-3-pro-preview",
            displayName = "Gemini 3 Pro (Preview)",
            provider = "Google",
            contextWindow = 1_000_000,
            inputModalities = listOf("text", "image", "audio", "video", "pdf"),
            outputModalities = listOf("text"),
        ),
        // Pure image generator: the registry says it produces images only.
        "imagen-4" to LLMModel(
            id = "imagen-4",
            displayName = "Imagen 4",
            provider = "Google",
            outputModalities = listOf("image"),
        ),
        // Dedicated TTS model.
        "gpt-4o-mini-tts" to LLMModel(
            id = "gpt-4o-mini-tts",
            displayName = "GPT-4o mini TTS",
            provider = "OpenAI",
            inputModalities = listOf("text"),
            outputModalities = listOf("audio"),
        ),
    )

    /** Mirrors `ModelsDevApi.applyDevData`: the registry fills, base keeps what dev omits. */
    private fun devEnrich(model: LLMModel): LLMModel {
        val dev = devRows[model.id] ?: return model
        return model.copy(
            contextWindow = dev.contextWindow ?: model.contextWindow,
            inputModalities = dev.inputModalities ?: model.inputModalities,
            outputModalities = dev.outputModalities ?: model.outputModalities,
        )
    }

    private fun entry(model: LLMModel, overrides: ModelOverrides = ModelOverrides()) =
        ModelEntry(providerInstanceId = "inst-1", baseModel = model, overrides = overrides)

    private fun catalogModel(id: String): LLMModel = LLMModel.allModels.first { it.id == id }

    // ── layer 1: the decision core, executed ────────────────────────────────

    @Test
    fun `a built-in multimodal model looks like TEXT-ONLY through the raw entry`() {
        // The pre-fix answer, pinned: the catalog declares no modalities, so
        // every raw capability question about this model answers "no".
        val base = catalogModel("gemini-3-pro-preview")
        assertNull(base.inputModalities)
        assertNull(base.outputModalities)
        val e = entry(base)
        assertFalse(e.model.hasAudioInput)
        assertFalse(e.model.hasVoiceModality)
        // And the registry-augmented answer is the opposite one.
        assertTrue(effectiveEntryModel(e, ::devEnrich).hasAudioInput)
        assertTrue(effectiveEntryModel(e, ::devEnrich).hasVoiceModality)
    }

    @Test
    fun `a model the registry calls audio-capable is only selectable through the funnel`() {
        // `hasVoiceModels` / the voice pickers filter on exactly this predicate.
        val e = entry(catalogModel("gemini-3-pro-preview"))
        val rawCandidates = listOf(e).filter { it.model.hasAudioInput }
        val effectiveCandidates = listOf(e).filter { effectiveEntryModel(it, ::devEnrich).hasAudioInput }
        assertEquals(0, rawCandidates.size)
        assertEquals(1, effectiveCandidates.size)
    }

    @Test
    fun `a pure image generator is not a text-output model once the registry is consulted`() {
        // New-chat default selection reads isTextOutput. A custom row that
        // declares nothing is "text out" by convention, and the registry row
        // that says otherwise never reached the raw read.
        val e = entry(LLMModel(id = "imagen-4", displayName = "Imagen 4", provider = "Google"))
        assertTrue(e.model.isTextOutput)
        assertFalse(effectiveEntryModel(e, ::devEnrich).isTextOutput)
    }

    @Test
    fun `a dedicated TTS model produces audio only through the funnel`() {
        val e = entry(LLMModel(id = "gpt-4o-mini-tts", displayName = "GPT-4o mini TTS", provider = "OpenAI"))
        assertFalse(e.model.hasAudioOutput)
        assertTrue(effectiveEntryModel(e, ::devEnrich).hasAudioOutput)
        // …and it is not mistaken for an ASR model.
        assertFalse(effectiveEntryModel(e, ::devEnrich).hasAudioInput)
    }

    @Test
    fun `an explicit modality override still wins over the registry`() {
        // The ordering contract. A user who narrowed this entry to text must
        // not get audio back from the registry row.
        val e = entry(
            catalogModel("gemini-3-pro-preview"),
            ModelOverrides(inputModalities = listOf("text"), outputModalities = listOf("text")),
        )
        assertFalse(effectiveEntryModel(e, ::devEnrich).hasAudioInput)
        assertFalse(effectiveEntryModel(e, ::devEnrich).hasVoiceModality)
    }

    @Test
    fun `the other order is what the wrong fix would have produced`() {
        // Teeth for the test above: enrich(entry.model) — the shape a fix must
        // NOT take — re-adds the registry's audio over the user's choice.
        val e = entry(
            catalogModel("gemini-3-pro-preview"),
            ModelOverrides(inputModalities = listOf("text"), outputModalities = listOf("text")),
        )
        assertTrue(devEnrich(e.model).hasAudioInput)
    }

    // ── layer 2: the wiring, as shipped source shape ────────────────────────

    private val prPath = "data/repository/ProviderRepository.kt"
    private val asrPath = "speech/ProviderSpeechRecognitionEngine.kt"

    /** Capability decisions that must be taken on the effective model. */
    private val decisionFunctions = listOf(
        "newestProviderNewestTextEntry",
        "resolveVoiceInputChoice",
        "resolveVoiceInputCandidates",
        "resolveVoiceOutputChoice",
        "hasVoiceModels",
        "shadowVoiceProviders",
    )

    private val rawModalityRead = Regex(
        """\.model\s*\.\s*(hasAudioInput|hasAudioOutput|hasVoiceModality|isTextOutput)\b""",
    )

    private fun readShipped(relativeUnderApp: String): String {
        var dir: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
        while (dir != null) {
            for (prefix in listOf(
                "src/main/java/com/rikkaminis/app/",
                "src/android/app/src/main/java/com/rikkaminis/app/",
                "android/app/src/main/java/com/rikkaminis/app/",
            )) {
                val f = File(dir, prefix + relativeUnderApp)
                if (f.isFile) return f.readText()
            }
            dir = dir.parentFile
        }
        throw AssertionError(
            "shipped source not found: $relativeUnderApp from " + System.getProperty("user.dir"),
        )
    }

    /** Drop line and block comments — prose about a removed shape is legal. */
    private fun codeOnly(src: String): String {
        val sb = StringBuilder()
        var i = 0
        var quote: Char? = null
        while (i < src.length) {
            val c = src[i]
            if (quote != null) {
                sb.append(c)
                if (c == '\\' && i + 1 < src.length) {
                    sb.append(src[i + 1])
                    i += 2
                    continue
                }
                if (c == quote) quote = null
                i++
                continue
            }
            if (c == '"' || c == '\'') {
                quote = c
                sb.append(c)
                i++
                continue
            }
            if (c == '/' && i + 1 < src.length && src[i + 1] == '/') {
                while (i < src.length && src[i] != '\n') i++
                continue
            }
            if (c == '/' && i + 1 < src.length && src[i + 1] == '*') {
                i += 2
                while (i < src.length && !(src[i] == '*' && i + 1 < src.length && src[i + 1] == '/')) i++
                i += 2
                continue
            }
            sb.append(c)
            i++
        }
        return sb.toString()
    }

    /**
     * A declaration's text: the braced body when there is one, otherwise the
     * expression body up to the next declaration (an expression body sliced on
     * braces would swallow the following function).
     */
    private fun declarationText(code: String, header: String): String? {
        val at = code.indexOf(header)
        if (at < 0) return null
        var i = code.indexOf('(', at + header.length - 1)
        if (i < 0) return null
        var depth = 0
        while (i < code.length) {
            if (code[i] == '(') depth++
            if (code[i] == ')') {
                depth--
                if (depth == 0) break
            }
            i++
        }
        var k = i + 1
        while (k < code.length && code[k] != '{' && code[k] != '=') k++
        if (k >= code.length) return null
        if (code[k] == '=') {
            val nextDecl = Regex("""(?m)^[ \t]*(?:(?:private|internal|public|override|suspend|inline|operator)\s+)*fun\s""")
            val end = nextDecl.find(code, k + 1)?.range?.first ?: code.length
            return code.substring(at, end)
        }
        var d = 0
        var e = k
        while (e < code.length) {
            if (code[e] == '{') d++
            if (code[e] == '}') {
                d--
                if (d == 0) break
            }
            e++
        }
        return code.substring(at, e + 1)
    }

    /**
     * The decision, not the spelling: inside the named functions a capability
     * predicate must not be read off `ModelEntry.model`, and the function must
     * route through the effective-model funnel at all (a dropped gate is as bad
     * as a wrong one).
     */
    private fun wiringViolations(source: String): List<String> {
        val code = codeOnly(source)
        val out = mutableListOf<String>()
        for (fn in decisionFunctions) {
            val body = declarationText(code, "fun $fn(")
            if (body == null) {
                out += "declaration not found: $fn"
                continue
            }
            if (rawModalityRead.containsMatchIn(body)) {
                out += "$fn reads a modality off the raw entry model"
            }
            if (!body.contains("effective")) {
                out += "$fn does not consult the effective model"
            }
        }
        return out
    }

    private fun funnelViolations(source: String): List<String> {
        val code = codeOnly(source)
        val out = mutableListOf<String>()
        val funnel = declarationText(code, "private fun effectiveEntryModel(")
        if (funnel == null) {
            out += "the effectiveEntryModel( funnel is gone"
        } else {
            if (!funnel.contains("ModelsDevApi.effectiveModel(")) {
                out += "the funnel does not route through ModelsDevApi.effectiveModel("
            }
            if (Regex("""=\s*entry\.model\b""").containsMatchIn(funnel)) {
                out += "the funnel re-derives the raw entry model"
            }
        }
        return out
    }

    private fun asrViolations(source: String): List<String> {
        val code = codeOnly(source)
        val out = mutableListOf<String>()
        if (Regex("""resolvedModel\s*=\s*entry\.model""").containsMatchIn(code)) {
            out += "the transcription request hands over the raw entry model"
        }
        if (!Regex("""resolvedModel\s*=\s*ModelsDevApi\.effectiveModel\(""").containsMatchIn(code)) {
            out += "resolvedModel is not resolved through ModelsDevApi.effectiveModel("
        }
        return out
    }

    /** The one sanctioned raw read: template seeds are not ModelEntry values. */
    private fun seedCarveOutViolations(source: String): List<String> {
        val code = codeOnly(source)
        val out = mutableListOf<String>()
        val body = declarationText(code, "fun replaceEntries(")
        if (body == null) {
            out += "replaceEntries( not found"
            return out
        }
        if (!body.contains("?.mockModels")) out += "the template-seed anchor is gone"
        if (!body.contains("hasVoiceModality")) out += "the seed filter lost its modality read"
        if (body.contains("effective")) out += "the seed read was routed through the funnel"
        return out
    }

    @Test
    fun `every ProviderRepository capability decision goes through the effective model`() {
        assertEquals(
            emptyList<String>(),
            wiringViolations(readShipped(prPath)),
        )
    }

    @Test
    fun `the ProviderRepository funnel is the single source, not a re-derivation`() {
        assertEquals(
            emptyList<String>(),
            funnelViolations(readShipped(prPath)),
        )
    }

    @Test
    fun `the voice template seeds keep their raw modality read`() {
        assertEquals(
            emptyList<String>(),
            seedCarveOutViolations(readShipped(prPath)),
        )
    }

    @Test
    fun `the transcription request carries the effective model`() {
        assertEquals(
            emptyList<String>(),
            asrViolations(readShipped(asrPath)),
        )
    }

    // ── layer 3: the probes have teeth ──────────────────────────────────────

    /** Revert exactly one call site of the shipped file and require the probe to reject it. */
    private fun probeHasTeeth(
        source: String,
        revert: String,
        to: String,
        probe: (String) -> List<String>,
        what: String,
    ) {
        assertTrue("$what: the shipped source does not contain `$revert`", source.contains(revert))
        assertTrue("$what: the shipped source is clean to begin with", probe(source).isEmpty())
        val mutated = source.replaceFirst(revert, to)
        assertFalse("$what: a reverted call site passed the probe", probe(mutated).isEmpty())
    }

    @Test
    fun `the wiring probes reject the pre-fix call sites`() {
        val shipped = readShipped(prPath)
        probeHasTeeth(
            shipped,
            "|| !effectiveEntryModel(entry).hasAudioInput) return null",
            "|| !entry.model.hasAudioInput) return null",
            ::wiringViolations,
            "audio-input gate",
        )
        probeHasTeeth(
            shipped,
            "effectiveEntryModel(it).hasVoiceModality",
            "it.model.hasVoiceModality",
            ::wiringViolations,
            "voice-model predicate",
        )
        probeHasTeeth(
            shipped,
            "!it.isHidden && effectiveEntryModel(it).isTextOutput",
            "!it.isHidden && it.model.isTextOutput",
            ::wiringViolations,
            "text-output default",
        )
    }

    @Test
    fun `the funnel and ASR probes have teeth`() {
        val prShipped = readShipped(prPath)
        probeHasTeeth(
            prShipped,
            "= ModelsDevApi.effectiveModel(entry)",
            "= entry.model",
            ::funnelViolations,
            "funnel",
        )
        probeHasTeeth(
            prShipped,
            "?.filter { it.hasVoiceModality }",
            "?.filter { effectiveEntryModel(seedEntry).hasVoiceModality }",
            ::seedCarveOutViolations,
            "seed carve-out",
        )
        probeHasTeeth(
            readShipped(asrPath),
            "resolvedModel = ModelsDevApi.effectiveModel(entry)",
            "resolvedModel = entry.model",
            ::asrViolations,
            "transcription model",
        )
    }
}

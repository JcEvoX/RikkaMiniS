package com.rikkaminis.app.ui.modelcapability

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [deep-audit-1042 dv22-B3] Source-shape probe for the capability single source.
 *
 * A built-in [com.rikkaminis.app.data.model.ModelEntry] stores what the user
 * configured — and that is, for every registry model, an entry with **no**
 * modality declaration. The actionable capabilities are the models.dev values
 * under the entry, with the user's overrides on top:
 * `ModelsDevApi.effectiveModel(entry)`. Six UI surfaces used to read the raw
 * `entry.model` instead, so a vision model rendered as "text only" in the
 * pickers, the quick-test sheet, the entry editor, the provider detail rows and
 * the chat's non-text warning, while the request itself carried images.
 *
 * The properties are shape properties of Compose screens, which this sandbox
 * cannot compile, so — like `QuickTestSheetContractTest` — they are asserted on
 * the production text, one arm per surface. Every expectation is a literal; the
 * pre-fix fixture below must violate them, which is what keeps a probe that has
 * quietly stopped looking from passing.
 */
class UiModelCapabilitySourceTest {

    /**
     * Resolve a source file from whatever cwd gradle picked: the module dir
     * (`src/main/java/…`) or the repo root (`src/android/app/src/main/java/…`).
     * Same walk-up shape as `QuickTestSheetContractTest#sourceText` — a test
     * that only works from one of the two cwds fails for the wrong reason.
     */
    private fun source(rel: String): String {
        val viaModule = "src/main/java/$rel"
        val viaRepo = "src/android/app/$viaModule"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val here = dir
            for (candidate in listOf(viaModule, viaRepo)) {
                val f = File(here, candidate)
                if (f.isFile) return f.readText()
            }
            dir = here.parentFile
        }
        throw AssertionError("source not found from ${File(".").absolutePath}: $rel")
    }

    private val surfaces = mapOf(
        "the model entry picker" to "com/rikkaminis/app/ui/components/ModelEntryPicker.kt",
        "the chat model picker sheet" to "com/rikkaminis/app/ui/chat/ChatModelPickerSheet.kt",
        "the quick test sheet" to "com/rikkaminis/app/ui/components/QuickTestSheet.kt",
        "the chat screen non-text label" to "com/rikkaminis/app/ui/chat/ChatScreen.kt",
        "the model entry editor" to "com/rikkaminis/app/ui/settings/ModelEntryDetailScreen.kt",
        "the provider detail rows" to "com/rikkaminis/app/ui/settings/ProviderDetailScreen.kt",
        // [deep-audit-1043 B-06] `groupTopModalities` was the seventh reader of
        // the raw `entry.model` and the one C33's original six-file table
        // missed — so a group card lost every modality badge while its own
        // detail screen showed them.
        "the model groups screen" to "com/rikkaminis/app/ui/settings/ModelGroupsScreen.kt",
    )

    /** One reason string per way a surface can go back to the raw model. */
    private fun violations(code: String): List<String> = buildList {
        for (pattern in listOf(
            Regex("""\.model\s*\.\s*(inputModalities|outputModalities)\b"""),
            Regex("""\.model\s*\?\.\s*(inputModalities|outputModalities)\b"""),
            Regex("""modalityBadges\s*\(\s*[\w.]+\s*\.model\s*\)"""),
            Regex("""modalityFilter\s*\.\s*matches\s*\(\s*[\w.]+\s*\.model\s*\)"""),
            Regex("""\.model\s*\?\.\s*let\s*\(\s*::\s*nonTextLabelFor"""),
        )) {
            if (pattern.containsMatchIn(code)) add("capability read straight off `.model`: ${pattern.pattern}")
        }
        for (m in Regex("""\bval\s+(\w+)\s*=\s*[\w.]+\s*\.model\b""").findAll(code)) {
            val local = Regex.escape(m.groupValues[1])
            if (Regex("""\b$local\s*\.\s*(inputModalities|outputModalities)\b""").containsMatchIn(code) ||
                Regex("""modalityBadges\s*\(\s*$local\s*\)""").containsMatchIn(code) ||
                Regex("""nonTextLabelFor\s*\(\s*$local\s*\)""").containsMatchIn(code)
            ) {
                add("capability read off the local `${m.groupValues[1]}` captured from `.model`")
            }
        }
        val reads = Regex("""\b(inputModalities|outputModalities|modalityBadges|nonTextLabelFor)\b""")
            .findAll(code).count()
        if (reads > 0 && !Regex("""\b(effectiveModel|enrichModel)\s*\(""").containsMatchIn(code)) {
            add("reads capability $reads time(s) but never calls effectiveModel/enrichModel")
        }
    }

    /** The pre-fix shapes, quoted from the audit (kept verbatim on purpose). */
    private val preFix = """
        fun row(entry: ModelEntry) {
            val mods = entry.model.inputModalities.orEmpty()
            modalityBadges(entry.model).forEach { badge -> render(badge) }
            val label = entry?.model?.let(::nonTextLabelFor)
            if (modalityFilter.matches(entry.model)) show()
        }
    """.trimIndent()

    @Test
    fun `the pre-fix shape violates every arm`() {
        // Teeth: if the probe stopped looking, the fixture would come back clean.
        val found = violations(preFix)
        assertTrue("the pre-fix fixture must trip the raw-model arm", found.isNotEmpty())
        assertTrue(found.any { it.contains("inputModalities") || it.contains("outputModalities") })
        assertTrue(found.any { it.contains("modalityBadges") || it.contains("modalityFilter") })
        assertTrue(found.any { it.contains("never calls effectiveModel") })
    }

    @Test
    fun `every capability-reading surface routes through the effective model`() {
        for ((what, rel) in surfaces) {
            val src = source(rel)
            assertEquals("$what ($rel) shape violations", emptyList<String>(), violations(src))
        }
    }

    @Test
    fun `the model entry editor enriches the raw base for its capability base`() {
        // The editor must not hand the RAW base to the save-time diff either:
        // the switches were seeded from the enriched modalities, so comparing
        // against the raw ones would freeze today's registry value as a user
        // override on a save that changed nothing.
        val src = source("com/rikkaminis/app/ui/settings/ModelEntryDetailScreen.kt")
        assertTrue("editor must build a capability base", src.contains("val capabilityBase = enrichCapabilityBase(baseModel)"))
        assertTrue("the save-time diff must use the capability base", src.contains("baseModel = capabilityBase"))
        assertTrue("the switch derivation must use the capability base", src.contains("overrides.inputModalities ?: capabilityBase.inputModalities"))
        assertTrue("the enrichment helper must exist", src.contains("private fun enrichCapabilityBase(baseModel: LLMModel): LLMModel"))
    }

    @Test
    fun `the raw model still reaches the persisted entry`() {
        // Radius guard: only the *derivation* moves to the enriched base. What
        // gets written back stays the user's raw base — enrichment must never be
        // persisted as if the user had picked it.
        val src = source("com/rikkaminis/app/ui/settings/ModelEntryDetailScreen.kt")
        assertTrue(src.contains("entry.copy(baseModel = baseModel.copy(id = modelId.trim())"))
    }
}

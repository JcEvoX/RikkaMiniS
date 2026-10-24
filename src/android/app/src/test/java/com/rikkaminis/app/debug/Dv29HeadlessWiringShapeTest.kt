package com.rikkaminis.app.debug

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv29-A2] Source shape of the debug/IPC surfaces this batch repaired.
 *
 * Every arm is paired with an in-test negative control on a *mutated copy* of
 * the same production text: the predicate must flip when the wiring is put back
 * the way it was. Without that, a typo inside a predicate reads as green.
 *
 * Comment lines are stripped before any predicate runs — the first draft of
 * this file asserted "no `else -> Completed`" and the comment *explaining* that
 * arm matched it, and the positional-pick ban matched the comment that documents
 * the ban. A gate that reads prose as code fails on its own rationale.
 *
 * The behaviour lives in `HeadlessChatRunnerDecisionTest`; this file only pins
 * that the production call sites still route through those decisions.
 */
class Dv29HeadlessWiringShapeTest {

    private fun readRepoFile(relative: String): String {
        var dir: java.io.File? = java.io.File(System.getProperty("user.dir") ?: ".")
        while (dir != null) {
            val f = java.io.File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw java.io.FileNotFoundException(
            "$relative not found from user.dir=${System.getProperty("user.dir")}",
        )
    }

    /** Drop whole-line comments so prose cannot satisfy (or trip) a predicate. */
    private fun stripCommentLines(code: String): String =
        code.lineSequence()
            .filterNot { line ->
                val t = line.trimStart()
                t.startsWith("//") || t.startsWith("*") || t.startsWith("/*")
            }
            .joinToString("\n")

    private val runner: String by lazy {
        stripCommentLines(
            readRepoFile(
                "src/android/app/src/main/java/com/rikkaminis/app/debug/HeadlessChatRunner.kt",
            ),
        )
    }
    private val mutations: String by lazy {
        stripCommentLines(
            readRepoFile(
                "src/android/app/src/main/java/com/rikkaminis/app/debug/ChatMutationMethods.kt",
            ),
        )
    }

    /** Body of `fun [name](`, from the `{` after the parameter list. */
    private fun funBody(text: String, name: String): String {
        val at = text.indexOf("fun $name(")
        assertTrue("`fun $name(` not found", at >= 0)
        val open = text.indexOf('{', at)
        assertTrue("no body for `$name`", open > at)
        var depth = 0
        var i = open
        while (i < text.length) {
            when (text[i]) {
                '{' -> depth += 1
                '}' -> {
                    depth -= 1
                    if (depth == 0) return text.substring(open, i + 1)
                }
            }
            i += 1
        }
        throw IllegalStateException("unbalanced body for `$name`")
    }

    private fun mutate(code: String, before: String, after: String): String {
        val mutated = code.replaceFirst(before, after)
        assertNotEquals("mutation anchor not found: $before", code, mutated)
        return mutated
    }

    // ── A-02: one decision for the binding write ────────────────────────────

    private fun bindingWriteUsesSingleDecision(code: String): Boolean {
        val body = funBody(code, "applyModelOverride")
        return body.contains("decideSessionModelResolution(") &&
            body.contains("firstUsableGroupEntry(") &&
            !Regex("memberEntryIds\\s*\\.\\s*(first|firstOrNull)\\s*\\(").containsMatchIn(body)
    }

    @Test
    fun `the binding write decides through the shared resolution`() {
        assertTrue(
            "applyModelOverride must route both branches through " +
                "decideSessionModelResolution / firstUsableGroupEntry",
            bindingWriteUsesSingleDecision(runner),
        )
        assertTrue(
            "firstUsableGroupEntry must apply decideSessionModelResolution",
            funBody(runner, "firstUsableGroupEntry").contains("decideSessionModelResolution("),
        )
    }

    @Test
    fun `negative control - dropping the shared decision is detected`() {
        val preFix = mutate(
            runner,
            "                val decision = decideSessionModelResolution(",
            "                val decision = unusedDecision(",
        )
        assertFalse(
            "an entry that does not call the shared decision must fail this predicate",
            bindingWriteUsesSingleDecision(preFix),
        )
    }

    @Test
    fun `negative control - a positional group pick is detected`() {
        val preFix = mutate(
            runner,
            "                val usableMember = firstUsableGroupEntry(",
            "                val usableMember = group.memberEntryIds.firstOrNull() ?: firstUsableGroupEntry(",
        )
        assertFalse(
            "a bare `members.firstOrNull()` pick must fail this predicate",
            bindingWriteUsesSingleDecision(preFix),
        )
    }

    // ── A-03: selectModel answers with the truth ────────────────────────────

    private fun selectModelVerifiesSwitch(code: String): Boolean {
        val body = funBody(code, "selectModel")
        val decisionAt = body.indexOf("decideSessionModelResolution(")
        val selectAt = body.indexOf("vm.selectEntry(")
        val verifyAt = body.indexOf("vm.activeEntryId.value")
        val answerAt = body.indexOf("vm.modelName.value")
        return decisionAt in 0 until selectAt &&
            verifyAt in selectAt until answerAt &&
            body.contains("model_switch_not_applied")
    }

    @Test
    fun `selectModel refuses an unusable target and verifies the switch landed`() {
        assertTrue(
            "selectModel must pre-validate with the shared decision, then read " +
                "activeEntryId before answering",
            selectModelVerifiesSwitch(runner),
        )
    }

    @Test
    fun `negative control - answering without the post-check is detected`() {
        val preFix = mutate(
            runner,
            "        val active = vm.activeEntryId.value",
            "        val active = entryId",
        )
        assertFalse(
            "a selectModel that trusts `selectEntry` without reading the " +
                "observable state must fail this predicate",
            selectModelVerifiesSwitch(preFix),
        )
    }

    @Test
    fun `negative control - skipping the pre-validation is detected`() {
        val preFix = mutate(
            runner,
            "        val decision = decideSessionModelResolution(\n" +
                "            entry,\n" +
                "            instance,\n" +
                "            instance?.let { app.providerRepository.loadApiKey(it.id) },\n" +
                "        )\n" +
                "        if (decision !is SessionModelResolution.Use) {\n" +
                "            val reason = bindingRejectReason(decision) ?: \"UNUSABLE\"\n" +
                "            throw RPCException(-32602, \"entry:\$entryId is not usable — \$reason\")\n" +
                "        }\n" +
                "        vm.selectEntry(entryId)",
            "        vm.selectEntry(entryId)",
        )
        assertFalse(
            "a selectModel that calls selectEntry with no usability check must fail",
            selectModelVerifiesSwitch(preFix),
        )
    }

    // ── A-04: compact settles on evidence ───────────────────────────────────

    private fun compactSettlesOnEvidence(code: String): Boolean {
        val body = funBody(code, "compact")
        val markers = Regex("latestCompactMarker\\(").findAll(body).count()
        return markers >= 2 &&
            body.contains("settleCompactOutcome(") &&
            body.contains("markerLanded")
    }

    @Test
    fun `compact settles on the marker or the summary, not on the flag`() {
        assertTrue(
            "compact must snapshot the marker before and after and settle " +
                "through settleCompactOutcome",
            compactSettlesOnEvidence(runner),
        )
    }

    @Test
    fun `negative control - settling on the flag transition is detected`() {
        val preFix = mutate(
            runner,
            "        val settled = settleCompactOutcome(finished, markerLanded, summaryChanged)",
            "        val settled = CompactSettlement(\"Completed\", null)",
        )
        assertFalse(
            "the old unconditional `Completed` must fail this predicate",
            compactSettlesOnEvidence(preFix),
        )
        // Without the BEFORE snapshot there is nothing to compare the marker
        // against, so the evidence test itself is gone.
        val noBefore = mutate(
            runner,
            "        val priorMarkerId = withContext(Dispatchers.IO) {\n" +
                "            app.chatRepository.dao.latestCompactMarker(sessionId)?.id\n" +
                "        }",
            "        val priorMarkerId: String? = null",
        )
        assertFalse(
            "dropping the before-snapshot must fail this predicate",
            compactSettlesOnEvidence(noBefore),
        )
    }

    // ── V-1: the existence gate is wired into every send/bind entry ─────────

    private fun gateBeforeVm(code: String, name: String): Boolean {
        val body = funBody(code, name)
        val gateAt = body.indexOf("requireSendableSession(")
        val vmAt = body.indexOf("viewModel(context, sessionId)")
        return gateAt >= 0 && (vmAt < 0 || gateAt < vmAt)
    }

    @Test
    fun `every send entry checks the session exists before materialising a VM`() {
        for (name in listOf(
            "prompt",
            "retry",
            "rerunFromToolBlock",
            "selectModel",
            "applyModelOverride",
        )) {
            assertTrue("`$name` must gate on the session row", gateBeforeVm(runner, name))
        }
        assertTrue(
            "the screen-side prompt must gate too",
            mutations.contains("HeadlessChatRunner.requireSendableSession("),
        )
        val guard = funBody(runner, "requireSendableSession")
        assertTrue("the gate reads the row", guard.contains("getSession("))
        assertTrue("the gate tolerates a resident draft VM", guard.contains("peekViewModel("))
        assertTrue("the gate answers with an RPC error", guard.contains("throw RPCException("))
    }

    @Test
    fun `negative control - an entry that builds the VM first is detected`() {
        val preFix = mutate(
            runner,
            "        val app = app(context)\n" +
                "        requireSendableSession(app, sessionId)\n" +
                "        val vm = viewModel(context, sessionId)",
            "        val app = app(context)\n" +
                "        val vm = viewModel(context, sessionId)",
        )
        assertFalse(
            "an entry that materialises the VM before checking the row must fail",
            gateBeforeVm(preFix, "prompt"),
        )
    }

    @Test
    fun `negative control - a guard without the row read is detected`() {
        val preFix = mutate(
            runner,
            "            app.chatRepository.dao.getSession(sessionId)",
            "            true",
        )
        val guard = funBody(preFix, "requireSendableSession")
        assertEquals(
            "the derived guard shape needs all three facts",
            true,
            guard.contains("peekViewModel(") && !guard.contains("getSession("),
        )
    }
}

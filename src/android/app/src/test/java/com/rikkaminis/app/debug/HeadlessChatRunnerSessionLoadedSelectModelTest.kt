package com.rikkaminis.app.debug

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1043 A-05] The two headless entries the 1042 barrier missed:
 * `HeadlessChatRunner.selectModel` and `ChatMutationMethods.uiPrompt`.
 *
 * Same defect family as [HeadlessChatRunnerSessionReadyBarrierTest] —
 * `ChatSessionLifecycle.loadSession` publishes in two steps (`_activeEntryId`
 * at :423, then the message list on IO at :477 and `_sessionLoaded` in that
 * block's `finally` at :786) — but two consequences the 1042 entries do not
 * have:
 *
 *  - `selectModel` READS state back (`vm.thinkingLevel.value.name`) and
 *    returns it to the caller. Inside the window the level is still the
 *    constructor default `OFF` (ChatViewModel:444), so the RPC echoes `"OFF"`
 *    for a session whose stored level is not OFF, and the model switch it just
 *    made is then overwritten by load step 2.
 *  - `uiPrompt` drives the SCREEN-OWNED ViewModel (the one ChatScreen
 *    collects), so it cannot reuse `HeadlessChatRunner.prompt`'s barrier; it
 *    needs its own, and it needs to report whether a run actually started
 *    instead of a bare `ok: true`.
 *
 * `ChatViewModel` has no Android-free seam (ViewModelProvider / Context /
 * repositories), so — same stance as the two sibling tests — the contract is
 * asserted on the production TEXT with literal expected strings, and every arm
 * has a negative control that mutates a copy of the real source and asserts
 * the very same check fails. The mechanical twin is criterion 14 of
 * `scripts/scan/chat_session_id_binding_check.py`; this file additionally pins
 * the SELECTED-STATE read-back and the error envelope, which a text gate
 * cannot.
 */
class HeadlessChatRunnerSessionLoadedSelectModelTest {

    private fun sourceFile(rel: String): File {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel)
            dir = d.parentFile
        }
        error("'$rel' not found from ${File(".").absoluteFile}")
    }

    private fun source(rel: String): String = sourceFile(rel).readText()

    private val runnerFile =
        "src/android/app/src/main/java/com/rikkaminis/app/debug/HeadlessChatRunner.kt"
    private val mutationFile =
        "src/android/app/src/main/java/com/rikkaminis/app/debug/ChatMutationMethods.kt"

    /** Index of the `}` closing the `{` at [openIdx], or -1. */
    private fun matchingBrace(text: String, openIdx: Int): Int {
        var depth = 0
        var i = openIdx
        while (i < text.length) {
            when (text[i]) {
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) return i
                }
            }
            i++
        }
        return -1
    }

    /**
     * Body of the member function declared [decl] — delimited by the NEXT
     * member declaration, because an expression-bodied helper (`private fun
     * app(context: Context): MinisApp = …`) has no body brace of its own and a
     * brace-hunting walk would hand the following function's body to it.
     */
    private fun fnBody(code: String, decl: String): String {
        val decls = Regex("(?m)^    (?:private |internal )?(?:suspend )?fun (\\w+)\\s*\\(")
            .findAll(code).toList()
        // `decl` omits the 4-space member indent, so anchor on the declaration
        // text with the indent trimmed — matching the raw match text would
        // never hit and the arm would fail as "anchor missing".
        val idx = decls.indexOfFirst { it.value.trimStart().startsWith(decl) }
        assertTrue("$decl must exist (anchor missing — re-anchor)", idx >= 0)
        val start = decls[idx].range.first
        val end = if (idx + 1 < decls.size) decls[idx + 1].range.first else code.length
        return code.substring(start, end)
    }

    // ── the barrier probe (mirrors gate criterion 14; receiver-agnostic) ────

    /**
     * Decision points, not spelling: the entry must (a) await `sessionLoaded`
     * BEFORE the mutation call, (b) inside an off-Main
     * `withContext(...Dispatchers.Default)` hop — waiting on Main deadlocks the
     * load that flips the latch, and (c) inside a `withTimeoutOrNull` — an
     * unbounded wait pins the RPC connection. Written as a regex on the hop so
     * a fully-qualified `kotlinx.coroutines.Dispatchers.Default` (the
     * `ChatMutationMethods` style) reads as the same decision as the imported
     * one (the `HeadlessChatRunner` style).
     */
    private fun barrierViolations(entry: String, mutatorCall: String): List<String> {
        val v = mutableListOf<String>()
        val callIdx = entry.indexOf(mutatorCall)
        if (callIdx < 0) {
            v.add("the entry must contain `$mutatorCall` (anchor missing — re-anchor)")
            return v
        }
        val prefix = entry.substring(0, callIdx)
        val waitIdx = prefix.indexOf("sessionLoaded.first")
        if (waitIdx < 0) {
            v.add(
                "the entry mutates the VM's in-memory state at `$mutatorCall` " +
                    "without awaiting the `sessionLoaded` latch first — " +
                    "`_activeEntryId` flips at load step 1, the latch only at " +
                    "step 2's finally, so in that window the level/model/messages " +
                    "are still the constructor defaults"
            )
            return v
        }
        val hops = Regex(
            "withContext\\s*\\(\\s*(?:[A-Za-z_]\\w*\\.)*Dispatchers\\s*\\.\\s*Default\\s*\\)"
        ).findAll(prefix).toList()
        var covered = false
        for (h in hops) {
            val brace = prefix.indexOf('{', h.range.last)
            if (brace < 0 || brace > waitIdx) continue
            val end = matchingBrace(prefix, brace)
            if (end < waitIdx) continue
            val block = prefix.substring(brace, end + 1)
            if (block.contains("sessionLoaded.first") && block.contains("withTimeoutOrNull(")) {
                covered = true
                break
            }
        }
        if (!covered) {
            v.add(
                "the `sessionLoaded` wait must be a BOUNDED OFF-MAIN hop — inside " +
                    "`withContext(Dispatchers.Default) { withTimeoutOrNull(…) { … } }`. " +
                    "Waiting on Main deadlocks the load; waiting unbounded hangs the " +
                    "RPC connection forever"
            )
        }
        return v
    }

    private fun assertBarrier(entry: String, mutatorCall: String, label: String) {
        val v = barrierViolations(entry, mutatorCall)
        assertTrue("$label: ${v.joinToString("; ")}", v.isEmpty())
    }

    // ── selectModel ────────────────────────────────────────────────────────

    @Test
    fun `selectModel awaits the sessionLoaded latch before switching the model`() {
        val select = fnBody(source(runnerFile), "suspend fun selectModel(")
        assertBarrier(select, "vm.selectEntry(", "selectModel")
    }

    @Test
    fun `selectModel reads the level back only after the barrier`() {
        // The litmus: the level is READ as part of the return value, so waiting
        // is not enough — the read must come after the wait too. Pre-fix the
        // return read `vm.thinkingLevel.value.name` straight after selectEntry
        // with no latch, i.e. it echoed the constructor default OFF.
        val select = fnBody(source(runnerFile), "suspend fun selectModel(")
        val waited = select.indexOf("vm.sessionLoaded.first")
        val read = select.indexOf("vm.thinkingLevel.value.name")
        assertTrue("selectModel must read the level back", read >= 0)
        assertTrue(
            "selectModel must read the level AFTER awaiting the latch — inside " +
                "the window `_thinkingLevel` is still the constructor default OFF, " +
                "so the RPC echoes OFF for a session whose stored level is not OFF",
            waited >= 0 && waited < read,
        )
    }

    @Test
    fun `a cold selectModel answers with the structured latch error, not a level guess`() {
        // 1042's `prompt` keeps a WARN-ONLY barrier on purpose (pre-fix behavior
        // is the floor for dispatch). A NEW entry has no such excuse: guessing a
        // thinking level is worse than failing, because the caller acts on it.
        val select = fnBody(source(runnerFile), "suspend fun selectModel(")
        val err = select.indexOf("session_not_loaded_in_5s")
        val call = select.indexOf("vm.selectEntry(")
        assertTrue("selectModel must name the latch timeout error", err >= 0)
        assertTrue(
            "the latch error must be raised BEFORE the model switch — a switch " +
                "that lands inside the window is about to be rolled back by the " +
                "late load, so proceeding would report a state that never holds",
            err < call,
        )
    }

    // ── uiPrompt ───────────────────────────────────────────────────────────

    @Test
    fun `uiPrompt awaits the sessionLoaded latch before dispatching`() {
        val prompt = fnBody(source(mutationFile), "suspend fun uiPrompt(")
        assertBarrier(prompt, "vm.sendMessage(", "uiPrompt")
    }

    @Test
    fun `uiPrompt reports whether a run started instead of a bare ok`() {
        // Pre-fix: `isStreaming` was read from the ViewModel AFTER the whole
        // `withContext(Main) { … }` returned, and `ok` was hard-coded true — a
        // dispatch dropped by sendMessage's own gates still answered ok:true.
        // The flag is claimed SYNCHRONOUSLY by sendMessage, so it must be read
        // on the same dispatcher, straight after the call.
        val prompt = fnBody(source(mutationFile), "suspend fun uiPrompt(")
        val call = prompt.indexOf("vm.sendMessage(")
        val read = prompt.indexOf("vm.isStreaming.value")
        assertTrue("uiPrompt must dispatch", call >= 0)
        assertTrue("uiPrompt must read isStreaming back", read >= 0)
        assertTrue(
            "uiPrompt must read `isStreaming` AFTER `sendMessage` (the flag is " +
                "claimed synchronously — reading it before the call answers a " +
                "previous turn, not this one)",
            read > call,
        )
        assertTrue(
            "uiPrompt must put the MEASURED flag into the response, not a hard-coded true",
            prompt.substring(read).contains("put(\"isStreaming\""),
        )
    }

    @Test
    fun `uiPrompt raises the structured latch error before dispatching`() {
        val prompt = fnBody(source(mutationFile), "suspend fun uiPrompt(")
        val err = prompt.indexOf("session_not_loaded_in_5s")
        val call = prompt.indexOf("vm.sendMessage(")
        assertTrue("uiPrompt must name the latch timeout error", err >= 0)
        assertTrue(
            "the latch error must precede the dispatch — sending into the window " +
                "lets load step 2's `_messages.value = loaded` overwrite the " +
                "just-appended user message",
            err < call,
        )
    }

    // ── negative controls — each mutates a copy of the real source and
    //    asserts the very same check fails ──────────────────────────────────

    private val selectBarrier = (
        "        val sessionReady = withContext(Dispatchers.Default) {\n" +
            "            withTimeoutOrNull(5000L) { vm.sessionLoaded.first { it } }\n" +
            "        }\n"
        )

    @Test
    fun `negative control - the barrier removed from selectModel is caught`() {
        val select = fnBody(source(runnerFile), "suspend fun selectModel(")
        val mutated = select.replace(selectBarrier, "")
        assertTrue("the barrier must exist to be mutated", mutated != select)
        assertTrue(
            "a selectModel without the latch wait must fail the barrier probe",
            barrierViolations(mutated, "vm.selectEntry(").isNotEmpty(),
        )
    }

    @Test
    fun `negative control - the selectModel wait moved onto Main is caught`() {
        val select = fnBody(source(runnerFile), "suspend fun selectModel(")
        val mutated = select.replace(
            "withContext(Dispatchers.Default)",
            "withContext(Dispatchers.Unconfined)",
        )
        assertTrue("the Default hop must exist to be mutated", mutated != select)
        assertTrue(
            "a wait on the caller's (Main) dispatcher deadlocks the load it waits " +
                "for — must fail the barrier probe",
            barrierViolations(mutated, "vm.selectEntry(").isNotEmpty(),
        )
    }

    @Test
    fun `negative control - an unbounded selectModel wait is caught`() {
        val select = fnBody(source(runnerFile), "suspend fun selectModel(")
        val mutated = select.replace(
            "withTimeoutOrNull(5000L) { vm.sessionLoaded.first { it } }",
            "vm.sessionLoaded.first { it }",
        )
        assertTrue("the bounded wait must exist to be mutated", mutated != select)
        assertTrue(
            "a wait without a timeout pins the RPC connection — must fail the " +
                "barrier probe",
            barrierViolations(mutated, "vm.selectEntry(").isNotEmpty(),
        )
    }

    @Test
    fun `negative control - the pre-1043 uiPrompt shape is caught`() {
        // Literal pre-fix shape: the VM is acquired and dispatched inside ONE
        // `withContext(Main)` via `.also { it.sendMessage(text) }`, no latch at
        // all, and `isStreaming` read outside the dispatch block.
        val pre = (
            "    suspend fun uiPrompt(context: Context, params: JSONObject): JSONObject {\n"
            + "        val vm = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Main) {\n"
            + "            androidx.lifecycle.ViewModelProvider(\n"
            + "                store.ownerFor(sessionId),\n"
            + "                ChatViewModel.factory(),\n"
            + "            )[com.rikkaminis.app.ui.chat.ChatViewModel::class.java]"
            + ".also { it.sendMessage(text) }\n"
            + "        }\n"
            + "        return JSONObject().apply { put(\"ok\", true) }\n"
            + "    }\n"
        )
        assertTrue(
            "the pre-1043 shape (dispatch inside the acquisition, receiver `it`, " +
                "no latch) must fail the barrier probe",
            barrierViolations(pre, "it.sendMessage(").isNotEmpty(),
        )
        // …and the post-fix source must NOT trip that same probe.
        assertTrue(
            "the post-fix uiPrompt must pass the probe the pre-fix shape fails",
            barrierViolations(
                fnBody(source(mutationFile), "suspend fun uiPrompt("), "vm.sendMessage("
            ).isEmpty(),
        )
    }

    @Test
    fun `negative control - reading isStreaming before the dispatch is caught`() {
        val prompt = fnBody(source(mutationFile), "suspend fun uiPrompt(")
        val callLine = "            vm.sendMessage(text)\n"
        val readLine = "            vm.isStreaming.value\n"
        assertTrue(
            "the dispatch block must exist to be mutated",
            callLine in prompt && readLine in prompt,
        )
        // A KDoc comment sits between the two lines, so swap them one at a time
        // rather than replacing the adjacent pair.
        assertTrue(
            "the dispatch block must exist to be mutated",
            prompt.indexOf(callLine) in 0 until prompt.indexOf(readLine),
        )
        val mutated = prompt
            .replace(callLine, "@@CALL@@\n")
            .replace(readLine, callLine)
            .replace("@@CALL@@\n", readLine)
        assertTrue("the mutation must reorder the source", mutated != prompt)
        assertTrue(
            "a flag read that precedes the dispatch answers the PREVIOUS turn — " +
                "it must be caught",
            mutated.indexOf("vm.isStreaming.value") < mutated.indexOf("vm.sendMessage("),
        )
    }
}

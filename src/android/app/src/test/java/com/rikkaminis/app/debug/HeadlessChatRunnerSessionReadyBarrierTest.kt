package com.rikkaminis.app.debug

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1042 A-05] The four headless mutation entries — retry /
 * rerunFromToolBlock / compact / revertCompact — must await the ViewModel's
 * `sessionLoaded` latch BEFORE handing the VM a message/block id.
 *
 * The defect: `ChatSessionLifecycle.loadSession` publishes in two steps —
 * `_activeEntryId` flips at step 1 (ChatSessionLifecycle:423) while the
 * message list is only read at step 2 (:477) and `_sessionLoaded` flips in
 * that block's `finally` (:786). The runner's existing readiness wait keys on
 * `_activeEntryId`, so every mutation entry had a window in which the VM's
 * in-memory list was still empty: `retryFromMessage` returns on `index < 0`
 * (ChatViewModel:3199), `rerunFromToolBlock` returns false — or TRUE through
 * its degenerate branch while the delegated `retryFromMessage` drops — the
 * compactor walked an empty history, and `revertCompact` read a
 * `_cachedLatestMarker` that is "null before the first load completes".
 * The `wait=true` branches then saw `isStreaming == false` and settled
 * `Completed` with the PREVIOUS assistant text: a fake success for an
 * operation that never happened.
 *
 * `ChatViewModel` has no Android-free seam (ViewModelProvider / Context /
 * repositories), so — same stance as [HeadlessChatCancelStoreWiringTest] —
 * the contract is asserted on the production text with literal expected
 * strings, and every arm has a negative control that mutates a copy of the
 * real source and asserts the very same check fails. The mechanical twin of
 * these checks is criterion 14 of
 * `scripts/scan/chat_session_id_binding_check.py` (derived entry set); this
 * test additionally pins the error ENVELOPES, which a text gate cannot.
 */
class HeadlessChatRunnerSessionReadyBarrierTest {

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

    /** The `{ … }` body of the first function whose declaration starts with [decl]. */
    private fun fnBody(runner: String, decl: String): String {
        val start = runner.indexOf(decl)
        assertTrue("$decl must exist in HeadlessChatRunner.kt", start >= 0)
        var depth = 0
        var i = runner.indexOf('{', start)
        assertTrue("$decl must have a body", i >= 0)
        val from = i
        while (i < runner.length) {
            when (runner[i]) {
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) return runner.substring(from, i + 1)
                }
            }
            i++
        }
        error("unbalanced braces after $decl")
    }

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

    private val barrierBlock =
        "        val sessionReady = withContext(Dispatchers.Default) {\n" +
            "            withTimeoutOrNull(5000L) { vm.sessionLoaded.first { it } }\n" +
            "        }\n"

    // ── the shared barrier probe (mirrors gate criterion 14) ───────────────

    /**
     * Decision points, not spelling: the entry must (a) await `sessionLoaded`
     * BEFORE the mutation call, (b) inside a `withContext(Dispatchers.Default)`
     * hop — waiting on Main deadlocks the load that flips the latch, and
     * (c) inside a `withTimeoutOrNull` — an unbounded wait pins the RPC
     * connection when the load never completes.
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
                "the entry mutates the VM's in-memory turn state at `$mutatorCall` " +
                    "without awaiting the `sessionLoaded` latch first — " +
                    "`_activeEntryId` flips at load step 1, the latch only at " +
                    "step 2's finally, so in that window the id resolves to nothing"
            )
            return v
        }
        var covered = false
        var hop = prefix.indexOf("withContext(Dispatchers.Default)")
        while (hop >= 0 && !covered) {
            val brace = prefix.indexOf('{', hop)
            if (brace >= 0) {
                val end = matchingBrace(prefix, brace)
                if (end >= 0) {
                    val block = prefix.substring(brace, end + 1)
                    covered = brace < waitIdx && waitIdx <= end &&
                        block.contains("sessionLoaded.first") &&
                        block.contains("withTimeoutOrNull(")
                }
            }
            hop = prefix.indexOf("withContext(Dispatchers.Default)", hop + 1)
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

    // ── the error-envelope probe (this file's own teeth) ──────────────────

    /**
     * A cold session must settle as a structured error, never as a fake
     * success: (1) the latch timeout has an explicit `session_not_loaded_in_5s`
     * branch BEFORE the mutation, and (2) a mutation that never started a
     * stream (the sync `_isStreaming` claim never happened) has its own
     * `*_dropped_before_stream` error BEFORE the `Completed` settle — the
     * pre-fix code fell through to `Completed` + the stale assistant text.
     */
    private fun envelopeViolations(
        entry: String,
        mutatorCall: String,
        droppedEnvelope: String,
    ): List<String> {
        val v = mutableListOf<String>()
        val callIdx = entry.indexOf(mutatorCall)
        if (callIdx < 0) {
            v.add("the entry must contain `$mutatorCall` (anchor missing — re-anchor)")
            return v
        }
        if (!entry.substring(0, callIdx).contains("session_not_loaded_in_5s")) {
            v.add(
                "a session whose latch never flips must be answered with the " +
                    "`session_not_loaded_in_5s` error envelope BEFORE mutating"
            )
        }
        val completedIdx = entry.indexOf("\"Completed\"")
        val droppedIdx = entry.indexOf(droppedEnvelope)
        if (droppedIdx < 0 || (completedIdx >= 0 && droppedIdx > completedIdx)) {
            v.add(
                "a mutation that never started a stream must be answered with " +
                    "`$droppedEnvelope` before the `Completed` settle — otherwise " +
                    "a dropped run reports Completed with the previous assistant text"
            )
        }
        return v
    }

    // ── the barrier contract ────────────────────────────────────────────────

    @Test
    fun `every mutating entry awaits the sessionLoaded latch off Main before mutating`() {
        val runner = source(runnerFile)
        assertBarrier(fnBody(runner, "suspend fun retry("), "vm.retryFromMessage(", "retry")
        assertBarrier(
            fnBody(runner, "suspend fun rerunFromToolBlock("),
            "vm.rerunFromToolBlock(", "rerunFromToolBlock",
        )
        assertBarrier(fnBody(runner, "suspend fun compact("), "vm.compactBefore(", "compact (anchor path)")
        assertBarrier(fnBody(runner, "suspend fun compact("), "vm.runCompactNow(", "compact (whole-history path)")
        assertBarrier(fnBody(runner, "suspend fun revertCompact("), "vm.revertCompact()", "revertCompact")
        // prompt() owns the SAME barrier since [fix/send-dispatch-race] — its
        // presence is an anchor the fix must not disturb.
        assertBarrier(fnBody(runner, "suspend fun prompt("), "vm.sendMessage(", "prompt")
    }

    @Test
    fun `a cold session settles as a structured error, never as a fake success`() {
        val runner = source(runnerFile)
        val retry = fnBody(runner, "suspend fun retry(")
        val rerun = fnBody(runner, "suspend fun rerunFromToolBlock(")
        val compact = fnBody(runner, "suspend fun compact(")
        val revert = fnBody(runner, "suspend fun revertCompact(")
        assertTrue(
            "retry: ${envelopeViolations(retry, "vm.retryFromMessage(", "retry_dropped_before_stream").joinToString("; ")}",
            envelopeViolations(retry, "vm.retryFromMessage(", "retry_dropped_before_stream").isEmpty(),
        )
        assertTrue(
            "rerunFromToolBlock: " +
                envelopeViolations(rerun, "vm.rerunFromToolBlock(", "rerun_dropped_before_stream").joinToString("; "),
            envelopeViolations(rerun, "vm.rerunFromToolBlock(", "rerun_dropped_before_stream").isEmpty(),
        )
        // compact has no `Completed`-vs-drop envelope (its `NoOp` detection
        // already tells a no-op from a real run) — but the latch timeout must
        // still answer with the error envelope BEFORE the mutation.
        assertTrue(
            "compact must answer a latch timeout with `session_not_loaded_in_5s` before compacting",
            compact.indexOf("session_not_loaded_in_5s").let { it >= 0 } &&
                compact.indexOf("session_not_loaded_in_5s") < compact.indexOf("vm.compactBefore("),
        )
        assertTrue(
            "revertCompact must answer a latch timeout with the " +
                "`session_not_loaded_in_5s` RPCException before mutating",
            revert.indexOf("session_not_loaded_in_5s") >= 0 &&
                revert.indexOf("session_not_loaded_in_5s") < revert.indexOf("vm.revertCompact()"),
        )
    }

    @Test
    fun `prompt keeps its warning-only five second dispatch barrier`() {
        // prompt()'s barrier is deliberately NON-fatal (pre-fix behavior is
        // the floor for dispatch, per [fix/send-dispatch-race]); the four
        // mutation entries are the opposite (structured failure). This arm
        // pins that distinction so neither half drifts into the other.
        val prompt = fnBody(source(runnerFile), "suspend fun prompt(")
        assertTrue(
            "prompt's barrier must keep its warning-only stance " +
                "(proceed on timeout, never a hard error)",
            prompt.contains("sessionLoaded not observed in 5s"),
        )
        assertTrue(
            "prompt's barrier must precede the dispatch",
            prompt.indexOf("vm.sessionLoaded.first") < prompt.indexOf("vm.sendMessage("),
        )
    }

    // ── negative controls — each mutates a copy of the real source and
    //    asserts the very same check fails ──────────────────────────────────

    private fun retryBodyOf(runner: String): String = fnBody(runner, "suspend fun retry(")

    @Test
    fun `negative control - the barrier removed from retry is caught`() {
        val retry = retryBodyOf(source(runnerFile))
        val mutated = retry.replace(barrierBlock, "")
        assertTrue("the barrier block must exist to be mutated", mutated != retry)
        assertTrue(
            "a retry without the latch wait must fail the barrier probe",
            barrierViolations(mutated, "vm.retryFromMessage(").isNotEmpty(),
        )
    }

    @Test
    fun `negative control - the wait moved onto Main is caught`() {
        val retry = retryBodyOf(source(runnerFile))
        val mutated = retry.replace(
            "withContext(Dispatchers.Default)",
            "withContext(Dispatchers.Unconfined)",
        )
        assertTrue("the Default hop must exist to be mutated", mutated != retry)
        assertTrue(
            "a wait that runs on the caller's (Main) dispatcher deadlocks the load " +
                "it waits for — must fail the barrier probe",
            barrierViolations(mutated, "vm.retryFromMessage(").isNotEmpty(),
        )
    }

    @Test
    fun `negative control - an unbounded wait is caught`() {
        val retry = retryBodyOf(source(runnerFile))
        val mutated = retry.replace(
            "withTimeoutOrNull(5000L) { vm.sessionLoaded.first { it } }",
            "vm.sessionLoaded.first { it }",
        )
        assertTrue("the bounded wait must exist to be mutated", mutated != retry)
        assertTrue(
            "a wait without a timeout pins the RPC connection when the load never " +
                "completes — must fail the barrier probe",
            barrierViolations(mutated, "vm.retryFromMessage(").isNotEmpty(),
        )
    }

    @Test
    fun `negative control - the barrier moved after the mutation is caught`() {
        val retry = retryBodyOf(source(runnerFile))
        val callLine = "        vm.retryFromMessage(targetMsgId)\n"
        val mutated = retry
            .replace(barrierBlock, "")
            .replace(callLine, callLine + barrierBlock)
        assertTrue("the mutation must reorder the source", mutated != retry)
        assertTrue(
            "a barrier that runs AFTER the mutation watches a window that already " +
                "closed — must fail the barrier probe",
            barrierViolations(mutated, "vm.retryFromMessage(").isNotEmpty(),
        )
    }

    @Test
    fun `negative control - a bare member function that mutates without the latch is caught`() {
        // Derived-set tooth: a NEW entry (here: compact's whole-history path)
        // written without the barrier must be caught even though the other
        // entries all carry it.
        val runner = source(runnerFile)
        val compact = fnBody(runner, "suspend fun compact(")
        val stripped = compact.replace(barrierBlock, "")
        assertTrue("the barrier block must exist to be mutated", stripped != compact)
        assertTrue(
            "a mutation path without the latch wait must fail the barrier probe",
            barrierViolations(stripped, "vm.runCompactNow(").isNotEmpty(),
        )
    }

    @Test
    fun `negative control - the dropped-run envelope removed from retry is caught`() {
        val retry = retryBodyOf(source(runnerFile))
        val mutated = retry.replace(
            "responseText = \"retry_dropped_before_stream\",",
            "responseText = null,",
        )
        assertTrue("the dropped-run envelope must exist to be mutated", mutated != retry)
        assertTrue(
            "a retry that settles a never-started run must fail the envelope probe",
            envelopeViolations(mutated, "vm.retryFromMessage(", "retry_dropped_before_stream").isNotEmpty(),
        )
    }

    @Test
    fun `negative control - prompt losing its dispatch barrier is caught`() {
        val runner = source(runnerFile)
        val prompt = fnBody(runner, "suspend fun prompt(")
        val callIdx = prompt.indexOf("vm.sendMessage(")
        assertTrue("prompt must dispatch to be mutated", callIdx >= 0)
        val mutated = prompt.replaceRange(
            0,
            callIdx,
            prompt.substring(0, callIdx).replace("vm.sessionLoaded.first { it }", "true"),
        )
        assertTrue("the dispatch barrier must exist to be mutated", mutated != prompt)
        assertTrue(
            "prompt must keep awaiting the latch before appending the dispatch",
            barrierViolations(mutated, "vm.sendMessage(").isNotEmpty(),
        )
    }
}

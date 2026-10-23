package com.rikkaminis.app.debug

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1044 A-03] — a headless send dropped by a pre-flight gate used to
 * be reported as `Completed`, carrying the PREVIOUS assistant turn.
 *
 * `ChatViewModel.sendMessage` claims `_isStreaming` SYNCHRONOUSLY and only
 * after every pre-flight gate (Safe Mode / `currentProvider == null` /
 * `_isCompacting` / context exhausted / blank input). [HeadlessChatRunner.prompt]
 * reads the flag right after the call, so `started == false` means "nothing will
 * ever stream". The `wait=true` path used to fall through an EMPTY
 * `if (!started) { }` block: because nothing streams, the wait returned at once
 * (`withTimeoutOrNull` sees `isStreaming == false`, skips `first { !it }`) and
 * the drain read the last assistant message out of the DB and called it
 * `Completed` — the RPC caller and `minis-sessions send` accepted a stale reply
 * as this request's answer.
 *
 * Layer split: [HeadlessChatRunner] has no Android-free seam (Context /
 * ViewModelProvider / repositories), so — same stance as
 * [HeadlessChatRunnerSessionReadyBarrierTest] — the wiring is asserted on the
 * production text with literal expectations plus a negative control per arm,
 * and the envelope itself is asserted as BEHAVIOUR on the shared helper (which
 * is top level so it can be called here at all).
 *
 * The mechanical twin of the source-shape checks is criterion 15 of
 * `scripts/scan/chat_session_id_binding_check.py`.
 */
class HeadlessPromptDropContractTest {

    private val runnerFile =
        "src/android/app/src/main/java/com/rikkaminis/app/debug/HeadlessChatRunner.kt"

    // ── the envelope, as behaviour (literal expectations) ───────────────────

    @Test
    fun `the dropped-send envelope is a structured error`() {
        val r = droppedSendResult()
        assertEquals("Error", r.status)
        assertEquals("send_dropped_before_stream", r.responseText)
        assertFalse("a dropped send is not a timeout", r.timedOut)
        assertEquals(0, r.deletedMessageCount)
    }

    @Test
    fun `the envelope is a fresh value each time`() {
        // Nothing about it is stateful: two calls must not share or mutate.
        val a = droppedSendResult()
        val b = droppedSendResult()
        assertEquals(a, b)
        assertNotNull(b.responseText)
    }

    // ── the source shape: prompt() decides before it drains ─────────────────

    @Test
    fun `prompt returns a structured error when the send was dropped`() {
        val body = fnBody(codeOnly(readRunner()), "suspend fun prompt(")
        assertTrue(
            "prompt() must still wait through `withTimeoutOrNull(` and drain " +
                "through `loadMessages(`",
            body.contains("withTimeoutOrNull(") && body.contains("loadMessages("),
        )
        assertTrue(
            "an `!started` guard must decide before the drain reads the DB — " +
                "otherwise the previous assistant turn is reported as this " +
                "request's answer",
            droppedGuardDecides(body),
        )
    }

    @Test
    fun `negative control - an empty guard block fails the same check`() {
        val mutated = readRunner().replace(
            GUARD_BLOCK,
            "        if (!started) {\n" +
                "            // dropped — fall through to drain\n" +
                "        }\n",
        )
        assertFalse("the mutation must apply", mutated == readRunner())
        val body = fnBody(codeOnly(mutated), "suspend fun prompt(")
        assertFalse(
            "an empty `if (!started) { }` must be caught",
            droppedGuardDecides(body),
        )
    }

    @Test
    fun `negative control - a Completed pseudo-success fails the same check`() {
        val mutated = readRunner().replace(
            GUARD_BLOCK,
            "        if (!started) {\n" +
                "            return@withContext PromptResult(\n" +
                "                status = \"Completed\",\n" +
                "                responseText = null,\n" +
                "                timedOut = false,\n" +
                "            )\n" +
                "        }\n",
        )
        assertFalse("the mutation must apply", mutated == readRunner())
        val body = fnBody(codeOnly(mutated), "suspend fun prompt(")
        assertFalse(
            "a guard that reports Completed for a dropped send must be caught",
            droppedGuardDecides(body),
        )
    }

    @Test
    fun `negative control - deleting the guard fails the same check`() {
        val mutated = readRunner().replace(GUARD_BLOCK, "")
        assertFalse("the mutation must apply", mutated == readRunner())
        val body = fnBody(codeOnly(mutated), "suspend fun prompt(")
        assertFalse(
            "the pre-fix fall-through shape must be caught",
            droppedGuardDecides(body),
        )
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /** The exact production guard, so the negative controls mutate a real anchor. */
    private val GUARD_BLOCK =
        "        if (!started) {\n" +
            "            return@withContext droppedSendResult()\n" +
            "        }\n"

    /**
     * Does `prompt()`'s first `!started` block return the dropped-send envelope
     * BEFORE `loadMessages(`?
     *
     * Accepts the delegated spelling (a call to the shared helper) because that
     * is what the production code uses, and the literal spelling, because the
     * helper may be inlined later — the DECISION is what is pinned.
     */
    private fun droppedGuardDecides(body: String): Boolean {
        val drain = body.indexOf("loadMessages(")
        if (drain < 0) return false
        val at = Regex("""if\s*\(\s*!\s*started\s*\)""").find(body)?.range?.first ?: return false
        if (at > drain) return false
        val open = body.indexOf('{', at)
        if (open < 0) return false
        val block = braceSlice(body, open)
        if (!block.contains("return")) return false
        val delegates = block.contains("droppedSendResult(")
        val inline = block.contains("\"Error\"") &&
            block.contains("send_dropped_before_stream")
        return delegates || inline
    }

    /** The `{ … }` body of the first function whose declaration starts with [decl]. */
    private fun fnBody(src: String, decl: String): String {
        val start = src.indexOf(decl)
        assertTrue("$decl must exist in HeadlessChatRunner.kt", start >= 0)
        val open = src.indexOf('{', start)
        assertTrue("$decl must have a body", open >= 0)
        return braceSlice(src, open)
    }

    private fun braceSlice(src: String, open: Int): String {
        var depth = 0
        var k = open
        while (k < src.length) {
            when (src[k]) {
                '{' -> depth += 1
                '}' -> {
                    depth -= 1
                    if (depth == 0) return src.substring(open, k + 1)
                }
            }
            k += 1
        }
        assertTrue("unbalanced braces at $open", false)
        return ""
    }

    private fun readRunner(): String = sourceFile(runnerFile).readText()

    private fun sourceFile(rel: String): File {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel)
            dir = d.parentFile
        }
        error("'$rel' not found from ${File(".").absoluteFile}")
    }

    /**
     * Comments stripped — a comment must not be able to satisfy (or trip) the
     * criterion, and the production KDoc QUOTES the pre-fix shape verbatim.
     */
    private fun codeOnly(src: String): String {
        val out = StringBuilder(src.length)
        var i = 0
        while (i < src.length) {
            val c = src[i]
            if (c == '/' && i + 1 < src.length && src[i + 1] == '/') {
                while (i < src.length && src[i] != '\n') i++
            } else if (c == '/' && i + 1 < src.length && src[i + 1] == '*') {
                i += 2
                while (i + 1 < src.length && !(src[i] == '*' && src[i + 1] == '/')) i++
                i = if (i + 2 > src.length) src.length else i + 2
            } else {
                out.append(c)
                i += 1
            }
        }
        return out.toString()
    }
}

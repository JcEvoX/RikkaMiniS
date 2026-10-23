package com.rikkaminis.app.sandbox.offload

import java.io.File
import java.io.FileNotFoundException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1044 A-04] — `minis-sessions send` reported a dropped prompt as a
 * delivered one.
 *
 * `ChatMutationMethods.prompt` does NOT throw when the send never leaves the
 * device: it answers with a structured `status` — `"Error"` with the machine
 * reason in `responseText` (`send_dropped_before_stream` /
 * `no_provider_resolved_in_5s` / `session_not_loaded_in_5s`), `"Timeout"` when
 * the wait elapsed, `"Completed"` / `"Running"` when it really ran. `cmdSend`
 * only caught exceptions and then `emit(…)`, whose envelope is hard-wired to
 * `ok: true` + exit code 0 — so a refusal came back as a success whose
 * `response_text` held a machine code in the place an assistant reply belongs.
 *
 * Layer split (why this file is not an end-to-end handler run): the handler
 * needs a `Context` + a ChatRepository, i.e. Room, which the JVM runtime does
 * not carry. What IS reachable is the decision core — `sendSucceeded` /
 * `sendExitFor` / `sendErrorCode` / `sendErrorReason` live in the companion for
 * exactly this reason — plus the SOURCE shape of the surface that consumes it.
 * Same split as [SessionsOffloadHandlerValidationTest] and
 * [ClipboardClearPlanTest].
 *
 * Expectations below are literals, never derived from the code under test.
 */
class SessionsSendExitCodeTest {

    private val handler =
        "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/SessionsOffloadHandler.kt"

    // ── the decision, as behaviour ──────────────────────────────────────────

    @Test
    fun `only Completed and Running are deliveries`() {
        assertTrue(SessionsOffloadHandler.sendSucceeded("Completed"))
        assertTrue(SessionsOffloadHandler.sendSucceeded("Running"))
        assertFalse(SessionsOffloadHandler.sendSucceeded("Error"))
        assertFalse(SessionsOffloadHandler.sendSucceeded("Timeout"))
        // An unknown / empty status is a refusal: reading it as success is
        // exactly how the defect stayed invisible.
        assertFalse(SessionsOffloadHandler.sendSucceeded(""))
        assertFalse(SessionsOffloadHandler.sendSucceeded("Retrying"))
    }

    @Test
    fun `a refusal exits non-zero`() {
        assertEquals(0, SessionsOffloadHandler.sendExitFor("Completed"))
        assertEquals(0, SessionsOffloadHandler.sendExitFor("Running"))
        assertEquals(1, SessionsOffloadHandler.sendExitFor("Error"))
        assertEquals(1, SessionsOffloadHandler.sendExitFor("Timeout"))
        assertEquals(1, SessionsOffloadHandler.sendExitFor(""))
    }

    @Test
    fun `timeout and error carry different envelope codes`() {
        assertEquals("SEND_TIMEOUT", SessionsOffloadHandler.sendErrorCode("Timeout"))
        assertEquals("SEND_FAILED", SessionsOffloadHandler.sendErrorCode("Error"))
        assertEquals("SEND_FAILED", SessionsOffloadHandler.sendErrorCode(""))
    }

    @Test
    fun `the message is the machine reason the RPC layer produced`() {
        assertEquals(
            "send_dropped_before_stream",
            SessionsOffloadHandler.sendErrorReason("Error", "send_dropped_before_stream"),
        )
        assertEquals(
            "no_provider_resolved_in_5s",
            SessionsOffloadHandler.sendErrorReason("Error", "  no_provider_resolved_in_5s "),
        )
        // A timed-out wait has no responseText at all.
        assertEquals("timeout", SessionsOffloadHandler.sendErrorReason("Timeout", null))
        assertEquals("timeout", SessionsOffloadHandler.sendErrorReason("Timeout", ""))
        // An unknown refusal still names itself instead of reporting "ok".
        assertEquals("error", SessionsOffloadHandler.sendErrorReason("Error", null))
    }

    // ── the source shape: cmdSend routes through the decision ────────────────

    @Test
    fun `cmdSend consults the status before it emits`() {
        val body = cmdSendBody(readHandler())
        assertTrue(
            "cmdSend must read the RPC status field",
            body.contains("""optString("status")"""),
        )
        assertTrue(
            "the refusal must go through the file's one error funnel",
            body.contains("errorEnvelope("),
        )
        assertTrue(
            "the refusal must pick a nonzero exit through the mapping",
            body.contains("sendExitFor("),
        )
        val gate = body.indexOf("sendSucceeded(")
        val emit = body.indexOf("emit(\"send\"")
        assertTrue("cmdSend must still emit its success payload", emit >= 0)
        assertTrue(
            "the success decision must be reached BEFORE the payload leaves " +
                "(gate=$gate emit=$emit)",
            gate in 0 until emit,
        )
        // The refusal must not hand back exit 0: the region before the emit is
        // the refusal branch, and `NativeOffloadResult(0` there would be the
        // pre-fix shape wearing a branch.
        val refusal = body.substring(body.indexOf("errorEnvelope("), emit)
        assertFalse(
            "the refusal branch must not return exit code 0",
            Regex("""NativeOffloadResult\(\s*0\b""").containsMatchIn(refusal),
        )
    }

    @Test
    fun `the refusal payload does not keep the reason in the answer slot`() {
        val body = cmdSendBody(readHandler())
        val detailAt = body.indexOf("val detail")
        // The first `errorEnvelope(` in cmdSend belongs to the EARLIER argument
        // guards (SUBAGENT_DISABLED / INVALID_ARGS) — the refusal's own call is
        // the one AFTER the detail payload is built.
        val errAt = body.indexOf("errorEnvelope(", detailAt)
        assertTrue("the refusal must build its own detail payload", detailAt >= 0)
        assertTrue("the refusal must go through the error funnel", errAt > detailAt)
        val detail = body.substring(detailAt, errAt)
        assertFalse(
            "the reason must not ride along under `response_text` — that key is " +
                "where an assistant's answer belongs (the reason goes in the " +
                "envelope's `message`)",
            detail.contains("response_text"),
        )
    }

    @Test
    fun `negative control - an unconditional emit fails the same check`() {
        val mutated = readHandler().replace(
            """        if (!sendSucceeded(status)) {""",
            "        if (false) {",
        )
        assertFalse("the mutation must apply", mutated == readHandler())
        val body = cmdSendBody(mutated)
        val gate = body.indexOf("sendSucceeded(")
        val emit = body.indexOf("emit(\"send\"")
        assertFalse(
            "an always-false gate must be caught: the payload would leave for every status",
            gate in 0 until emit,
        )
    }

    @Test
    fun `negative control - a zero exit on the refusal path fails the same check`() {
        val mutated = readHandler().replace("sendExitFor(status),", "0,")
        assertFalse("the mutation must apply", mutated == readHandler())
        val body = cmdSendBody(mutated)
        val emit = body.indexOf("emit(\"send\"")
        val refusal = body.substring(body.indexOf("errorEnvelope("), emit)
        assertTrue(
            "the zero exit must be caught by the same rule",
            Regex("""NativeOffloadResult\(\s*0\b""").containsMatchIn(refusal),
        )
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /** Comments stripped first: the KDoc quotes both the old and new spellings. */
    private fun cmdSendBody(src: String): String =
        braceSlice(codeOnly(src), "private fun cmdSend(")

    private fun readHandler(): String {
        val f = File(repoRoot(), handler)
        if (!f.exists()) throw FileNotFoundException(f.path)
        return f.readText()
    }

    private fun repoRoot(): File {
        System.getProperty("probe.repo")?.let { return File(it) }
        var dir: File? = File(System.getProperty("user.dir") ?: ".")
        while (dir != null) {
            if (File(dir, handler).exists()) return dir
            dir = dir.parentFile
        }
        throw FileNotFoundException(
            "repo root not found from user.dir=${System.getProperty("user.dir")}",
        )
    }

    /** `src` from the first `{` after [marker], balanced. */
    private fun braceSlice(src: String, marker: String): String {
        val at = src.indexOf(marker)
        assertTrue("marker not found: $marker", at >= 0)
        val open = src.indexOf('{', at)
        assertTrue("no block after: $marker", open >= 0)
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
        assertTrue("unbalanced braces after: $marker", false)
        return ""
    }

    /**
     * Comments stripped — hand-written linear scan (the regex form blew the JVM
     * stack on this file family; see [OffloadPayloadJsonShapeTest]).
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

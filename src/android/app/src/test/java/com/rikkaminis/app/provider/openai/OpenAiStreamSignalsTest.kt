package com.rikkaminis.app.provider.openai

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv28 A-01 / A-08] Boundary tables for the two decisions that were missing
 * from the OpenAI SSE loop.
 *
 * A-01: the EOF truncation guard's tool-call arm is fed by a turn-level
 * counter. Before this, only the Chat Completions shape fed it, so a
 * tool-call-only Responses turn cut by EOF never produced
 * `Finished(truncated = true)` and a half-streamed argument object reached the
 * executor.
 *
 * A-08: a mid-stream error's code token is symbolic on most relays
 * (`"rate_limit_exceeded"`), and the old `optInt("code", 0)` coerced it to 0 —
 * which `mapHttpError` turns into a non-retryable `ProviderError`.
 *
 * Expected values are hand-written literals (never derived from the code under
 * test — see evidence-discipline).
 */
class OpenAiStreamSignalsTest {

    // ------------------------------------------------------------------
    // A-01 — tool-call progress weights
    // ------------------------------------------------------------------

    @Test
    fun `chat completions counts every announced call`() {
        assertEquals(1, chatCompletionsToolCallEventWeight(1))
        assertEquals(3, chatCompletionsToolCallEventWeight(3))
    }

    @Test
    fun `chat completions reports no progress for an empty delta`() {
        assertEquals(0, chatCompletionsToolCallEventWeight(0))
        assertEquals(0, chatCompletionsToolCallEventWeight(-1))
    }

    @Test
    fun `a responses function call announcement counts as progress`() {
        assertEquals(
            1,
            responsesToolCallEventWeight("response.output_item.added", "function_call", 0),
        )
    }

    @Test
    fun `a responses function call completion counts as progress`() {
        assertEquals(
            1,
            responsesToolCallEventWeight("response.output_item.done", "function_call", 0),
        )
    }

    @Test
    fun `a responses argument delta counts as progress only when it carries text`() {
        assertEquals(
            1,
            responsesToolCallEventWeight("response.function_call_arguments.delta", null, 42),
        )
        assertEquals(
            0,
            responsesToolCallEventWeight("response.function_call_arguments.delta", null, 0),
        )
    }

    @Test
    fun `a non tool-call responses item is not progress`() {
        assertEquals(
            0,
            responsesToolCallEventWeight("response.output_item.added", "message", 0),
        )
        assertEquals(
            0,
            responsesToolCallEventWeight("response.output_text.delta", null, 100),
        )
        assertEquals(0, responsesToolCallEventWeight("response.completed", null, 0))
    }

    // ------------------------------------------------------------------
    // A-08 — mid-stream error code -> HTTP status
    // ------------------------------------------------------------------

    @Test
    fun `a numeric code is kept verbatim`() {
        assertEquals(429, openAiStreamErrorStatus("429", ""))
        assertEquals(500, openAiStreamErrorStatus("500", ""))
        assertEquals(404, openAiStreamErrorStatus("404", ""))
    }

    @Test
    fun `a symbolic rate limit code maps to 429`() {
        assertEquals(429, openAiStreamErrorStatus("rate_limit_exceeded", ""))
        assertEquals(429, openAiStreamErrorStatus("rate_limit_error", ""))
        assertEquals(429, openAiStreamErrorStatus("", "rate_limit_exceeded"))
    }

    @Test
    fun `an exhausted quota maps to 429 so it can fall back`() {
        assertEquals(429, openAiStreamErrorStatus("insufficient_quota", ""))
    }

    @Test
    fun `a server error maps to 500 so it stays transient`() {
        assertEquals(500, openAiStreamErrorStatus("server_error", ""))
        assertEquals(500, openAiStreamErrorStatus("api_error", ""))
    }

    @Test
    fun `an authentication failure maps to 401`() {
        assertEquals(401, openAiStreamErrorStatus("authentication_error", ""))
        assertEquals(401, openAiStreamErrorStatus("invalid_api_key", ""))
    }

    @Test
    fun `a permission failure maps to 403`() {
        assertEquals(403, openAiStreamErrorStatus("permission_error", ""))
    }

    @Test
    fun `a missing model maps to 404`() {
        assertEquals(404, openAiStreamErrorStatus("model_not_found", ""))
        assertEquals(404, openAiStreamErrorStatus("not_found_error", ""))
    }

    @Test
    fun `an invalid request maps to 400`() {
        assertEquals(400, openAiStreamErrorStatus("invalid_request_error", ""))
        assertEquals(400, openAiStreamErrorStatus("context_length_exceeded", ""))
    }

    @Test
    fun `an unrecognised token yields no status so the old provider error stands`() {
        assertEquals(0, openAiStreamErrorStatus("something_new", ""))
        assertEquals(0, openAiStreamErrorStatus("", ""))
        assertEquals(0, openAiStreamErrorStatus("", ""))
    }

    @Test
    fun `the type token is consulted only when the code carries no family`() {
        // The code wins even though the type names another family.
        assertEquals(429, openAiStreamErrorStatus("rate_limit_exceeded", "server_error"))
        // The type is used when the code is absent.
        assertEquals(429, openAiStreamErrorStatus("", "rate_limit_error"))
    }

    // ------------------------------------------------------------------
    // A-01 — the turn-level verdict: a tool-call-only Responses turn cut by
    // EOF must reach Finished(truncated = true).
    //
    // The guard itself is pinned by truncated_call_guard_check.py (C3), so it
    // is NOT extracted into a helper — that would give the same fact two
    // homes. Instead this case executes the guard's own disjunction with the
    // counters a real turn would have, and asserts the produced expression is
    // still literally the one in the provider file.
    // ------------------------------------------------------------------

    @Test
    fun `a tool-call-only responses turn cut by eof is judged truncated`() {
        var contentLen = 0
        var reasoningLen = 0
        var toolCallEventCount = 0

        // What the Responses flavour actually feeds (order = wire order):
        toolCallEventCount += responsesToolCallEventWeight(
            "response.output_item.added", "function_call", 0,
        )
        toolCallEventCount += responsesToolCallEventWeight(
            "response.function_call_arguments.delta", null, 12,
        )
        val finishedSent = false   // EOF without [DONE]

        assertEquals(0, contentLen)
        assertEquals(0, reasoningLen)
        // The guard's disjunction, verbatim:
        val truncated = !finishedSent &&
            (contentLen > 0 || reasoningLen > 0 || toolCallEventCount > 0)
        assertEquals(true, truncated)

        // ...and the file still holds exactly that disjunction, so this case
        // cannot drift away from production.
        assertTrue(readRepoFile(OPENAI).contains(GUARD))
    }

    @Test
    fun `an empty responses turn cut by eof is still not judged truncated`() {
        var contentLen = 0
        var reasoningLen = 0
        var toolCallEventCount = 0
        toolCallEventCount += responsesToolCallEventWeight(
            "response.output_text.delta", null, 0,
        )
        toolCallEventCount += responsesToolCallEventWeight("response.completed", null, 0)
        val finishedSent = false
        assertEquals(
            false,
            !finishedSent && (contentLen > 0 || reasoningLen > 0 || toolCallEventCount > 0),
        )
    }

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

    companion object {
        private const val OPENAI =
            "src/android/app/src/main/java/com/rikkaminis/app/provider/openai/OpenAIProvider.kt"
        private const val GUARD =
            "if (!finishedSent && (contentLen > 0 || reasoningLen > 0 || toolCallEventCount > 0)) {"
    }
}

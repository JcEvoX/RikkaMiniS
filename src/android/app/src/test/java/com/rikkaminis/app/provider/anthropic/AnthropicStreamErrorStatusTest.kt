package com.rikkaminis.app.provider.anthropic

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [deep-audit-1045 A-04] Boundary table for the HTTP status derived from an
 * Anthropic mid-stream `error` event.
 *
 * The defect this pins: the stream branch sent everything except
 * `overloaded_error` as 400. Anthropic reports a rate limit as
 * `type: "rate_limit_error"`, and 400 falls through `mapHttpError` to
 * ProviderError (NOT retryable) while the same condition reported over HTTP
 * as 429 becomes RateLimited (retryable). A retryable upstream condition was
 * therefore turned into a hard failure.
 *
 * Expected values are hand-written literals (never derived from the code
 * under test — see evidence-discipline).
 */
class AnthropicStreamErrorStatusTest {

    @Test
    fun `overloaded_error stays 529`() {
        // Pre-existing behaviour that must be preserved verbatim: 529 is in
        // mapHttpError's transient set, so it stays a retryable TransientError.
        assertEquals(529, anthropicStreamErrorStatus("overloaded_error"))
    }

    @Test
    fun `rate_limit_error maps to 429 so it becomes RateLimited`() {
        assertEquals(429, anthropicStreamErrorStatus("rate_limit_error"))
    }

    @Test
    fun `api_error maps to 500 so it stays transient`() {
        assertEquals(500, anthropicStreamErrorStatus("api_error"))
    }

    @Test
    fun `authentication_error maps to 401`() {
        assertEquals(401, anthropicStreamErrorStatus("authentication_error"))
    }

    @Test
    fun `permission_error maps to 403`() {
        assertEquals(403, anthropicStreamErrorStatus("permission_error"))
    }

    @Test
    fun `not_found_error maps to 404`() {
        assertEquals(404, anthropicStreamErrorStatus("not_found_error"))
    }

    @Test
    fun `request_too_large maps to 413`() {
        assertEquals(413, anthropicStreamErrorStatus("request_too_large"))
    }

    @Test
    fun `invalid_request_error maps to 400`() {
        assertEquals(400, anthropicStreamErrorStatus("invalid_request_error"))
    }

    @Test
    fun `an unknown type keeps the previous 400`() {
        assertEquals(400, anthropicStreamErrorStatus("some_new_error"))
    }

    @Test
    fun `a blank or absent type keeps the previous 400`() {
        assertEquals(400, anthropicStreamErrorStatus(""))
        assertEquals(400, anthropicStreamErrorStatus(null))
    }
}

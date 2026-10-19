package com.rikkaminis.app.provider.openai

import okhttp3.Request

/**
 * [da1031-D ISSUE-07] The one body that decides the OpenAI-family auth header
 * pair: Azure authenticates with the `api-key` header, every other key-based
 * provider with `Authorization: Bearer`. Returned as a plain pair (not tied to
 * okhttp) so a caller that is not building a `Request.Builder` — the debug RPC
 * connectivity probe, whose `probeAuthHeader` used to carry its own copy of the
 * ternary — can delegate here too. Two copies of this fork is how the debug
 * probe and the request path drift apart.
 */
internal fun openAIKeyAuthHeader(azure: Boolean, token: String): Pair<String, String> =
    if (azure) "api-key" to token else "Authorization" to "Bearer $token"

/**
 * OpenAI-compatible key auth, single source for every outbound builder.
 * [OpenAIProvider.applyKeyAuth] delegates here so the two can't drift — and so
 * does the shared pair builder above.
 */
internal fun Request.Builder.applyOpenAIKeyAuth(
    azure: Boolean,
    token: String,
): Request.Builder =
    openAIKeyAuthHeader(azure, token).let { (name, value) -> header(name, value) }

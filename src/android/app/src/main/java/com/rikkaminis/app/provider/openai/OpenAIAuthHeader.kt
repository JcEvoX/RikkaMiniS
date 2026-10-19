package com.rikkaminis.app.provider.openai

import okhttp3.Request

/**
 * OpenAI-compatible key auth, single source for every outbound builder.
 * Azure uses `api-key`; everything else `Authorization: Bearer`.
 * [OpenAIProvider.applyKeyAuth] delegates here so the two can't drift.
 */
internal fun Request.Builder.applyOpenAIKeyAuth(
    azure: Boolean,
    token: String,
): Request.Builder =
    if (azure) header("api-key", token) else header("Authorization", "Bearer $token")

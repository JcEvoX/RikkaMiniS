package com.rikkaminis.app.provider.openai

import com.rikkaminis.app.provider.openai.OpenAIModelsApi.resolveProviderName
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * [dv18-A-08] The `provider` field of the rows [OpenAIModelsApi.fetchModels]
 * returns.
 *
 * The defect: an adapter that reuses this fetcher for a vendor family (Kimi
 * Code) passed its own base URL, which `isOfficialOpenAI` cannot recognise —
 * every row came back labelled "Custom". `ModelsDevApi.providerKeyMap` has no
 * "Custom" key, so enrichment fell through to the order-dependent
 * all-providers scan (the F-212 failure).
 *
 * The fix adds an OPTIONAL `providerName`, so the contract has two halves and
 * both are locked here:
 *
 *  - a caller that names its family gets that name on every row, and
 *  - a caller that does not (`OpenAIModelListAdapter`, the generic
 *    OpenAI-compatible one) keeps the legacy "Custom"/"OpenAI" decision
 *    byte-for-byte — the regression guard that keeps this fix from changing
 *    behaviour for third-party endpoints.
 *
 * Expectations are literals on purpose: deriving them from
 * [OpenAIModelsApi.resolveProviderName] (or from `ProviderType.displayName`)
 * would make a mutation of the decision invisible.
 */
class OpenAIModelsApiProviderNameTest {

    private lateinit var server: MockWebServer

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    // -- The decision itself: pure, no Android runtime, no network ----------

    @Test
    fun `a named caller relabels third-party rows`() {
        assertEquals(
            "Kimi Code",
            resolveProviderName(isCustomBase = true, providerName = "Kimi Code"),
        )
    }

    @Test
    fun `an unnamed third-party row keeps the legacy Custom label`() {
        assertEquals("Custom", resolveProviderName(isCustomBase = true, providerName = null))
    }

    @Test
    fun `an unnamed official row keeps the OpenAI label`() {
        assertEquals("OpenAI", resolveProviderName(isCustomBase = false, providerName = null))
    }

    @Test
    fun `the supplied name wins even for a base URL that looks official`() {
        // A vendor adapter is the authority on which family its rows belong to;
        // a gateway proxied through an openai.com-looking host must still be
        // filed under the vendor the user configured.
        assertEquals(
            "Kimi Code",
            resolveProviderName(isCustomBase = false, providerName = "Kimi Code"),
        )
    }

    // -- End to end through the live fetch path -----------------------------

    private val modelsBody = """{"data":[
        {"id":"kimi-k3","object":"model"},
        {"id":"kimi-k2","object":"model"}
    ]}"""

    @Test
    fun `fetching with a providerName stamps every row`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setBody(modelsBody)
                .setHeader("Content-Type", "application/json")
        )

        val models = OpenAIModelsApi.fetchModels(
            apiKey = "sk-test",
            baseURL = server.url("/v1").toString(),
            context = null,        // production: adapters pass a Context, tests do not
            forceRefresh = true,
            providerName = "Kimi Code",
        )

        assertEquals(2, models.size)
        assertEquals(listOf("Kimi Code", "Kimi Code"), models.map { it.provider })
        assertTrue(
            "no row may keep the placeholder label",
            models.none { it.provider == "Custom" },
        )
    }

    @Test
    fun `fetching without a providerName keeps the legacy Custom label`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setBody(modelsBody)
                .setHeader("Content-Type", "application/json")
        )

        val models = OpenAIModelsApi.fetchModels(
            apiKey = "sk-test",
            baseURL = server.url("/v1").toString(),
            context = null,
            forceRefresh = true,
        )

        assertEquals(2, models.size)
        assertEquals(listOf("Custom", "Custom"), models.map { it.provider })
    }
}

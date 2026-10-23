package com.rikkaminis.app.mcp.oauth

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv25-A1] deep-audit-1045 A-01 — the OAuth block's on-disk key contract.
 *
 * The in-guest CLI (`minis-mcp-cli add --oauth-*`, see main.py) writes
 * camelCase keys, this app writes snake_case. [MCPOAuthConfig.fromJson] reads
 * the snake_case spelling first and the CLI's spelling as a fallback (the same
 * primary-field-then-alias priority `MCPRepository.parseStartupTimeout` uses).
 *
 * Before that fallback, a CLI-written block parsed blank ⇒ [MCPOAuthConfig
 * .isConfigured] false ⇒ `MCPRepository.save()` — it runs on the load of any
 * entry without `createdAt`, which the CLI never writes — skipped the `oauth`
 * block entirely, so the user's OAuth configuration evaporated from
 * servers.json on the next reload-from-disk / backup import.
 *
 * The gate `scripts/scan/mcp_oauth_lifecycle_check.py` pins the SHAPE (key
 * tables ordered primary-then-alias, wired into `fromJson`, `toJson` still
 * snake_case, null path kept); this file pins the BEHAVIOUR. Neither alone is
 * enough: a rename keeps behaviour but can unhook the tables, and a wired table
 * with the order flipped keeps the shape but reads the wrong value.
 */
class MCPOAuthConfigTest {

    /** Exactly the `oauth` object `minis-mcp-cli add --oauth-*` writes. */
    private fun cliPayload(): JSONObject = JSONObject(
        """
        {
          "mode": "static",
          "clientId": "cli-client",
          "authorizationEndpoint": "https://auth.example.com/authorize",
          "tokenEndpoint": "https://auth.example.com/token",
          "scopes": "openid calendar",
          "redirectURI": "http://127.0.0.1:8765/callback"
        }
        """.trimIndent()
    )

    /** What this app has always written (snake_case). */
    private fun appPayload(): JSONObject = JSONObject(
        """
        {
          "mode": "static",
          "client_id": "app-client",
          "authorization_endpoint": "https://auth.example.com/authorize",
          "token_endpoint": "https://auth.example.com/token",
          "scopes": "openid email",
          "redirect_uri": "rikkaminis://oauth/callback"
        }
        """.trimIndent()
    )

    @Test
    fun cliWrittenCamelCaseBlockParsesCompletely() {
        val config = MCPOAuthConfig.fromJson(cliPayload())
        assertNotNull("a CLI-written oauth block must not parse as null", config)
        config!!
        assertEquals("static", config.mode)
        assertEquals("cli-client", config.clientId)
        assertEquals("https://auth.example.com/authorize", config.authorizationEndpoint)
        assertEquals("https://auth.example.com/token", config.tokenEndpoint)
        assertEquals("openid calendar", config.scopes)
        assertEquals("http://127.0.0.1:8765/callback", config.redirectUri)
    }

    /**
     * The regression itself: `MCPRepository.entryToJson` keeps the block only
     * when `isConfigured` is true, so a blank parse is a silent delete of the
     * user's OAuth configuration on the next save.
     */
    @Test
    fun cliWrittenBlockStaysConfigured() {
        val config = MCPOAuthConfig.fromJson(cliPayload())
        assertNotNull(config)
        assertTrue("a fully populated CLI block is a configured server", config!!.isConfigured)
        // …and it survives the exact rule MCPRepository.entryToJson applies.
        val entry = JSONObject()
        config.takeIf { it.isConfigured }?.let { entry.put("oauth", it.toJson()) }
        assertTrue("the oauth block must survive the save path", entry.has("oauth"))
        assertEquals(
            "cli-client",
            entry.getJSONObject("oauth").optString("client_id", ""),
        )
    }

    @Test
    fun snakeCaseConfigRoundTripsUnchanged() {
        val original = MCPOAuthConfig.fromJson(appPayload())
        assertNotNull(original)
        val rewritten = MCPOAuthConfig.fromJson(original!!.toJson())
        assertEquals(original, rewritten)
    }

    @Test
    fun primaryWinsOverAliasWhenBothArePresent() {
        val mixed = JSONObject()
            .put("mode", "static")
            .put("client_id", "app-client")
            .put("clientId", "cli-client")
            .put("authorization_endpoint", "https://app.example.com/authorize")
            .put("authorizationEndpoint", "https://cli.example.com/authorize")
            .put("token_endpoint", "https://app.example.com/token")
            .put("tokenEndpoint", "https://cli.example.com/token")
            .put("redirect_uri", "rikkaminis://oauth/callback")
            .put("redirectURI", "http://127.0.0.1:8765/callback")

        val config = MCPOAuthConfig.fromJson(mixed)!!
        assertEquals("app-client", config.clientId)
        assertEquals("https://app.example.com/authorize", config.authorizationEndpoint)
        assertEquals("https://app.example.com/token", config.tokenEndpoint)
        assertEquals("rikkaminis://oauth/callback", config.redirectUri)
    }

    @Test
    fun blankPrimaryFallsBackToAlias() {
        // A writer that emits the alias but an empty string for the primary
        // (a template, a half-migrated file) must still be read, not dropped.
        val mixed = JSONObject()
            .put("client_id", "")
            .put("clientId", "cli-client")
            .put("authorizationEndpoint", "https://auth.example.com/authorize")
            .put("tokenEndpoint", "https://auth.example.com/token")

        val config = MCPOAuthConfig.fromJson(mixed)
        assertNotNull(config)
        assertEquals("cli-client", config!!.clientId)
        assertTrue(config.isConfigured)
    }

    @Test
    fun toJsonWritesOnlySnakeCaseKeys() {
        val written = MCPOAuthConfig.fromJson(appPayload())!!.toJson()
        val keys = written.keySet().sorted()
        assertEquals(
            listOf(
                "authorization_endpoint",
                "client_id",
                "mode",
                "redirect_uri",
                "scopes",
                "token_endpoint",
            ),
            keys,
        )
        for (key in keys) {
            assertEquals("the disk format stays snake_case: $key", key.lowercase(), key)
        }
    }

    @Test
    fun blockWithoutEndpointsIsStillNull() {
        val noise = JSONObject()
            .put("mode", "static")
            .put("scopes", "openid")
            .put("redirectURI", "http://127.0.0.1:8765/callback")
        assertNull("an endpoint-less oauth block is noise, not configuration", MCPOAuthConfig.fromJson(noise))
    }

    @Test
    fun absentBlockIsNull() {
        assertNull(MCPOAuthConfig.fromJson(null))
    }

    @Test
    fun partialCliBlockKeepsWhatItHas() {
        // Only clientId + authorization are present: still not "configured"
        // (a token swap would fail), but the values that ARE there survive so
        // the edit form can show them.
        val partial = JSONObject()
            .put("clientId", "cli-client")
            .put("authorizationEndpoint", "https://auth.example.com/authorize")
        val config = MCPOAuthConfig.fromJson(partial)
        assertNotNull(config)
        assertEquals("cli-client", config!!.clientId)
        assertEquals("https://auth.example.com/authorize", config.authorizationEndpoint)
        assertEquals("", config.tokenEndpoint)
        assertFalse(config.isConfigured)
    }
}

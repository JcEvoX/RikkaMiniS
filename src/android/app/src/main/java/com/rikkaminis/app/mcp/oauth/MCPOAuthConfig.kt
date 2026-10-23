package com.rikkaminis.app.mcp.oauth

import org.json.JSONObject

/**
 * [T-android-mcp-oauth] Non-secret OAuth configuration for an MCP server
 * (Static mode: user-supplied client credentials, PKCE Authorization Code
 * flow). Round-tripped inside the server entry in servers.json. The client
 * SECRET and issued tokens never live here — those go to [MCPOAuthStore]
 * (EncryptedSharedPreferences). Mirrors iOS `MCPOAuthConfig`.
 */
data class MCPOAuthConfig(
    /** "static" = user-supplied client credentials (this implementation).
     *  "dynamic" reserved for discovery + DCR (design-only for now). */
    val mode: String = "static",
    val clientId: String = "",
    val authorizationEndpoint: String = "",
    val tokenEndpoint: String = "",
    /** Space-separated scopes, e.g. "openid email https://.../calendar". */
    val scopes: String? = null,
    /** Custom redirect URI; defaults to the loopback callback when null. */
    val redirectUri: String? = null,
) {
    val isConfigured: Boolean
        get() = clientId.isNotBlank() &&
            authorizationEndpoint.isNotBlank() &&
            tokenEndpoint.isNotBlank()

    fun toJson(): JSONObject = JSONObject().apply {
        put("mode", mode)
        put("client_id", clientId)
        put("authorization_endpoint", authorizationEndpoint)
        put("token_endpoint", tokenEndpoint)
        scopes?.takeIf { it.isNotBlank() }?.let { put("scopes", it) }
        redirectUri?.takeIf { it.isNotBlank() }?.let { put("redirect_uri", it) }
    }

    companion object {
        // [T-mcp-oauth-cli-alias] Key tables: the snake_case spelling this app
        // writes (see [toJson]) first, the camelCase spelling the in-guest CLI
        // writes (`minis-mcp-cli add --oauth-*`, main.py) as the alias. Order
        // IS the contract — primary wins, the CLI's spelling is the fallback,
        // the same primary-field-then-alias priority
        // `MCPRepository.parseStartupTimeout` uses for migrated configs.
        //
        // Reading snake_case only made every field blank for a CLI-written
        // entry ⇒ [isConfigured] false ⇒ `MCPRepository.save()` — which runs
        // on the load of any entry without `createdAt`, and the CLI writes
        // none — skipped the `oauth` block outright, so the config evaporated
        // from servers.json on the next reload / backup import.
        private val CLIENT_ID_KEYS = listOf("client_id", "clientId")
        private val AUTH_ENDPOINT_KEYS = listOf("authorization_endpoint", "authorizationEndpoint")
        private val TOKEN_ENDPOINT_KEYS = listOf("token_endpoint", "tokenEndpoint")
        private val REDIRECT_URI_KEYS = listOf("redirect_uri", "redirectURI")

        /** First non-blank value among [keys], or "" — the alias walk. */
        private fun JSONObject.firstNonBlank(keys: List<String>): String =
            keys.firstNotNullOfOrNull { key -> optString(key, "").ifBlank { null } } ?: ""

        /** Parse from the `oauth` object inside a server entry. Returns null
         *  when the object is absent or lacks the required endpoints. */
        fun fromJson(obj: JSONObject?): MCPOAuthConfig? {
            if (obj == null) return null
            val config = MCPOAuthConfig(
                mode = obj.optString("mode", "static").ifBlank { "static" },
                clientId = obj.firstNonBlank(CLIENT_ID_KEYS),
                authorizationEndpoint = obj.firstNonBlank(AUTH_ENDPOINT_KEYS),
                tokenEndpoint = obj.firstNonBlank(TOKEN_ENDPOINT_KEYS),
                scopes = obj.optString("scopes", "").ifBlank { null },
                redirectUri = obj.firstNonBlank(REDIRECT_URI_KEYS).ifBlank { null },
            )
            // An oauth block with no endpoints is noise, not configuration.
            return if (config.clientId.isBlank() &&
                config.authorizationEndpoint.isBlank() &&
                config.tokenEndpoint.isBlank()
            ) {
                null
            } else {
                config
            }
        }
    }
}

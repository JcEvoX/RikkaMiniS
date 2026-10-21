package probe

import org.json.JSONObject

object RedactorSlice {
    private const val TAG = "ConfigBackup"

    internal fun stripExportedMcpSecrets(obj: JSONObject) {
        val inner = obj.optJSONObject("mcpServers")
        if (inner == null) {
            stripMcpSecretValues(obj)
            return
        }
        for (id in inner.keys().asSequence().toList()) {
            inner.optJSONObject(id)?.let { stripMcpSecretValues(it) }
        }
    }

    internal fun stripMcpSecretValues(obj: JSONObject) {
        for (secretKey in listOf("headers", "env")) {
            val section = obj.optJSONObject(secretKey) ?: continue
            for (k in section.keys().asSequence().toList()) {
                section.put(k, "")
            }
        }
        // [review D-3] Credential carriers beyond headers/env:
        // 1. `args` entries of the `--token=…` / `--api-key=…` shape (stdio
        //    servers that take the credential as a CLI flag).
        // 2. `url` query values for sensitive keys (?key=/token=/api_key=…),
        //    mirroring LLMRequestLog.SENSITIVE_QUERY_KEYS.
        // Both are blanked only when they actually carry a credential shape;
        // benign values survive so the restored entry stays wired to its
        // command/host (only the secret needs refilling).
        val args = obj.optJSONArray("args")
        if (args != null) {
            for (i in 0 until args.length()) {
                val v = args.optString(i, "")
                if (SECRET_ARG_RE.matches(v)) args.put(i, v.substringBefore('=') + "=")
            }
        }
        val url = obj.optString("url", "")
        if (url.contains("?")) {
            obj.put("url", redactUrlQuery(url))
        }
    }

    internal fun redactMcpArtifactEntry(relPath: String, bytes: ByteArray): ByteArray? {
        return try {
            val obj = JSONObject(String(bytes, Charsets.UTF_8))
            stripExportedMcpSecrets(obj)
            obj.toString(2).toByteArray(Charsets.UTF_8)
        } catch (t: Throwable) {
            AppLogger.warning(TAG, "mcp artifact redaction failed for $relPath: ${t.message}")
            null
        }
    }

    private val SECRET_ARG_RE = Regex(
        """(?i)^--(token|api[_-]?key|secret|password|access[_-]?token)\s*=\S.*""",
    )

    private val SENSITIVE_QUERY_KEYS = setOf(
        "key", "api_key", "apikey", "api-key", "token", "access_token",
        "secret", "password", "signature", "sig", "auth", "authorization",
    )

    private fun redactUrlQuery(url: String): String {
        val qIdx = url.indexOf('?')
        val query = url.substring(qIdx + 1)
        val fragIdx = query.indexOf('#')
        val (qBody, frag) = if (fragIdx >= 0) query.substring(0, fragIdx) to query.substring(fragIdx) else query to ""
        val redacted = qBody.split('&').joinToString("&") { pair ->
            val eq = pair.indexOf('=')
            if (eq > 0) {
                val k = pair.substring(0, eq)
                val v = pair.substring(eq + 1)
                if (k.lowercase() in SENSITIVE_QUERY_KEYS) "$k=[redacted:${v.length}]" else pair
            } else pair
        }
        return url.substring(0, qIdx + 1) + redacted + frag
    }
}

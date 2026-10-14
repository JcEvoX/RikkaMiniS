package com.rikkaminis.app.debug

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import java.io.InputStream
import java.io.PrintWriter
import java.net.ServerSocket
import java.net.Socket

/**
 * Debug-only JSON-RPC 2.0 server on port 5321.
 * Listens on all interfaces (0.0.0.0) so it's reachable from the local network for debugging.
 * Mirrors the iOS DebugServer for parity with the debug-server CLI skill.
 *
 * Auth model: EVERY client (loopback AND LAN) must present X-Minis-Token /
 * Authorization: Bearer. Loopback is no longer exempt — on-device browser
 * pages can fetch `http://127.0.0.1:5321/` from any origin, so a token-free
 * loopback would be a drive-by RPC surface. adb / curl pass the token
 * explicitly and keep working. CORS is stripped (no Access-Control-Allow-Origin),
 * so a browser page cannot read the response body even with the token.
 *
 * IMPORTANT: Only start this in debug builds. Never start in release.
 */
class DebugServer(
    private val context: Context,
    private val port: Int = 5321,
) {
    companion object {
        private const val TAG = "DebugServer"

        /**
         * [fix/sweep1013-security R4] Cap on the declared request body size.
         * `Content-Length` arrives from the client before any validation of
         * its value, and the reader used to allocate `CharArray(contentLength)`
         * straight from that declaration — a single header like
         * `Content-Length: 2147483647` from a token-holding caller (agent
         * toolchain included) allocated ~4GB and OOM'd the main process.
         * 8 MiB is far above any legitimate RPC payload (the biggest real
         * bodies are JSON tool results; debug.readFile clamps to 4 MiB on the
         * response side) and small enough that a hostile header can't hurt.
         * Requests declaring more are rejected with 413 before allocation.
         */
        private const val MAX_BODY_BYTES = 8 * 1024 * 1024

        /**
         * [T-android-debugserver-auth] Auth decision, kept pure for unit
         * testing. EVERY client — loopback AND LAN — must present the device
         * token. The previous model waived loopback because "adb forward is
         * the developer's own machine", but a debug build is reachable from
         * any web page on the device (even loopback: a page can
         * `fetch('http://127.0.0.1:5321/')`), so unauthenticated loopback
         * grants a drive-by RPC surface (shellExecute / writeFile / provider
         * import / LLM header exfiltration). With the token gate on all paths
         * plus CORS removed (no Access-Control-Allow-Origin), a browser page
         * can no longer obtain a valid token *or* read a response. adb/curl
         * are unaffected — they send the token explicitly. The iOS
         * protocol-v1 encrypted envelope (358e3ded) is deliberately NOT
         * ported: with no distribution surface the token gate is the
         * proportionate hardening (evaluated in the A8 batch report).
         * @param isLoopback retained for signature stability / audit log
         *   labelling; it does NOT skip authentication.
         */
        fun isAuthorized(isLoopback: Boolean, providedToken: String?, expectedToken: String): Boolean {
            if (expectedToken.isEmpty()) return false
            val provided = providedToken ?: return false
            if (provided.length != expectedToken.length) return false
            // Constant-time compare — don't leak the token via timing.
            var diff = 0
            for (i in expectedToken.indices) {
                diff = diff or (provided[i].code xor expectedToken[i].code)
            }
            return diff == 0
        }

        /**
         * [fix/sweep1014-debug-backup B1] Decode a request body that was read
         * as raw UTF-8 **bytes**. Returns null when the connection delivered
         * fewer bytes than declared (peer hung up mid-body) — replying 400
         * closes the HTTP exchange instead of leaving it half-open. Decoding
         * is UTF-8 because Content-Length counts BYTES, not chars, so
         * multi-byte content (CJK = 3 bytes/char, emoji = 4) round-trips
         * exactly. Pure for unit testing.
         */
        fun decodeBody(buffer: ByteArray, totalRead: Int, declaredLength: Int): String? {
            if (totalRead != declaredLength) return null
            return String(buffer, 0, totalRead, Charsets.UTF_8)
        }
    }

    private var serverSocket: ServerSocket? = null
    private var acceptJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO)
    private val rpcHandler = DebugRPCHandler(context)

    /**
     * [T-android-debugserver-auth] Per-install token required from ALL
     * clients (loopback AND LAN). Generated once, persisted in filesDir so the
     * developer can read it with:
     *   adb shell run-as com.rikkaminis.app cat files/debug_server_token
     * Also logged at startup (logcat is adb-only — not readable remotely).
     */
    private val authToken: String by lazy {
        val f = java.io.File(context.filesDir, "debug_server_token")
        if (f.exists()) {
            f.readText().trim().ifEmpty { generateToken(f) }
        } else {
            generateToken(f)
        }
    }

    private fun generateToken(f: java.io.File): String {
        val bytes = ByteArray(24)
        java.security.SecureRandom().nextBytes(bytes)
        val token = bytes.joinToString("") { "%02x".format(it) }
        runCatching { f.writeText(token) }
        return token
    }

    @Volatile
    private var stopped = false

    fun start() {
        if (serverSocket != null) return
        stopped = false

        acceptJob = scope.launch {
            try {
                // Listen on all interfaces (0.0.0.0) for network access
                val ss = ServerSocket(port, 10)
                serverSocket = ss
                Log.i(TAG, "Debug server listening on port $port (all interfaces)")
                // [audit-cs0913] NEVER log the token value here. This TAG is not
                // `Minis.*`, so AppLogger's logcat tailer persists it verbatim into
                // files/logs/ — a file that is one tap from the share sheet
                // (Settings → Logs). A leaked token is full RPC access, including
                // debug.shellExecute. Retrieve it out-of-band instead
                // (`adb shell run-as com.rikkaminis.app cat files/debug_server_token`);
                // the 401 response repeats that hint for CLI clients.
                Log.i(TAG, "Clients must present X-Minis-Token (see files/debug_server_token)")

                while (!stopped) {
                    try {
                        val client = ss.accept()
                        launch { handleConnection(client) }
                    } catch (e: Exception) {
                        if (!stopped) Log.w(TAG, "Accept error: ${e.message}")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to start server on port $port: ${e.message}")
            }
        }
    }

    fun stop() {
        stopped = true
        try { serverSocket?.close() } catch (_: Exception) {}
        serverSocket = null
        acceptJob?.cancel()
        acceptJob = null
        Log.i(TAG, "Server stopped")
    }

    private fun handleConnection(socket: Socket) {
        socket.use { s ->
            try {
                s.soTimeout = 30_000
                // [fix/sweep1014-debug-backup B1] Byte-level line reader over the
                // raw stream: request line / headers are ASCII lines, and the
                // body that follows must be read as raw bytes from the same
                // stream. A BufferedReader would buffer body bytes ahead of the
                // body read (consuming them as UTF-16 chars), losing multi-byte
                // content.
                val input = s.getInputStream()
                val reader = ByteLineReader(input)
                val writer = PrintWriter(s.getOutputStream(), true)

                // Read HTTP request line
                val requestLine = reader.readLine() ?: return
                val parts = requestLine.split(" ", limit = 3)
                if (parts.size < 2) {
                    sendResponse(writer, 400, rpcHandler.errorJSON(-32700, "Parse error"))
                    return
                }
                val method = parts[0]
                // [T-android-debugserver-skill] Path (query stripped) so the
                // unauthenticated GET skill routes can be dispatched.
                val path = parts.getOrNull(1)?.substringBefore('?') ?: "/"

                // Handle CORS preflight
                if (method == "OPTIONS") {
                    sendCorsPreflightResponse(writer)
                    return
                }

                // Read headers
                var contentLength = 0
                var providedToken: String? = null
                var accept = ""
                while (true) {
                    val headerLine = reader.readLine() ?: break
                    if (headerLine.isEmpty()) break
                    val lower = headerLine.lowercase()
                    if (lower.startsWith("content-length:")) {
                        contentLength = headerLine.substringAfter(":").trim().toIntOrNull() ?: 0
                    }
                    if (lower.startsWith("accept:")) {
                        accept = lower.substringAfter(":").trim()
                    }
                    // [T-android-debugserver-auth] Token via X-Minis-Token or
                    // Authorization: Bearer — either spelling accepted.
                    if (lower.startsWith("x-minis-token:")) {
                        providedToken = headerLine.substringAfter(":").trim()
                    }
                    if (lower.startsWith("authorization:")) {
                        val v = headerLine.substringAfter(":").trim()
                        if (v.lowercase().startsWith("bearer ")) {
                            providedToken = v.substring(7).trim()
                        }
                    }
                }

                // [T-android-debugserver-auth] Gate BEFORE any RPC dispatch.
                // EVERY client (loopback AND LAN) must present the token:
                // on-device browser pages reach 127.0.0.1 from any origin, so a
                // loopback exemption would leave an unauthenticated RPC surface.
                val isLoopback = s.inetAddress?.isLoopbackAddress == true
                if (!isAuthorized(isLoopback, providedToken, authToken)) {
                    Log.w(TAG, "401 unauthorized ${if (isLoopback) "loopback" else s.inetAddress?.hostAddress ?: "?"} (missing/wrong token)")
                    sendResponse(writer, 401, rpcHandler.errorJSON(-32000, "Unauthorized — send X-Minis-Token (see `adb shell run-as com.rikkaminis.app cat files/debug_server_token`)"))
                    return
                }

                // [T-android-debugserver-skill] Self-serve bootstrap routes,
                // mirroring iOS: a client that only has curl can pull the manual
                // and a working reference client straight off the device.
                //
                // Deliberately placed AFTER the auth gate above: unlike iOS
                // (whose /skill is unauthenticated because every RPC is still
                // sealed by the v1 envelope), Android's RPCs are plaintext, so
                // the token is the only barrier for EVERY client (loopback AND
                // LAN) and must not be bypassed. adb tooling reads the token via
                // `run-as ... cat files/debug_server_token` and sends it explicitly.
                if (method == "GET") {
                    val wantsHuman = accept.contains("text/html") ||
                        accept.contains("text/markdown") ||
                        accept.contains("text/plain")
                    when (path) {
                        "/schema" -> {
                            sendResponse(writer, 200, schemaJSON())
                            return
                        }
                        "/", "/skill", "/skill/" -> {
                            // A bare `GET /` from a tool (no human Accept) keeps
                            // returning the machine schema, same contract as iOS.
                            if (path == "/" && !wantsHuman) {
                                sendResponse(writer, 200, schemaJSON())
                            } else {
                                sendSkill(writer, wantsHuman)
                            }
                            return
                        }
                        "/skill/examples/python", "/skill/examples/minis_rpc_android.py" -> {
                            sendSkillAsset(writer, "examples/minis_rpc_android.py", "text/x-python; charset=utf-8")
                            return
                        }
                        "/skill/examples/curl", "/skill/examples/curl.md" -> {
                            sendSkillAsset(writer, "examples/curl.md", "text/markdown; charset=utf-8")
                            return
                        }
                    }
                }

                if (method != "POST") {
                    sendResponse(writer, 405, rpcHandler.errorJSON(-32600, "Only POST accepted"))
                    return
                }

                if (contentLength <= 0) {
                    sendResponse(writer, 400, rpcHandler.errorJSON(-32700, "Empty body"))
                    return
                }
                if (contentLength > MAX_BODY_BYTES) {
                    sendResponse(
                        writer, 413,
                        rpcHandler.errorJSON(-32700, "Body too large: $contentLength bytes declared (limit $MAX_BODY_BYTES)"),
                    )
                    return
                }

                // [fix/sweep1014-debug-backup B1] Read the body as raw UTF-8
                // bytes — Content-Length counts BYTES, not chars. The old
                // CharArray path counted UTF-16 chars, so any multi-byte body
                // (CJK 3 bytes/char, emoji 4) "finished" early and the read
                // kept blocking on EOF until the 30s soTimeout killed a
                // perfectly valid request.
                val body = ByteArray(contentLength)
                var totalRead = 0
                while (totalRead < contentLength) {
                    val n = input.read(body, totalRead, contentLength - totalRead)
                    if (n < 0) break
                    totalRead += n
                }
                val jsonBody = decodeBody(body, totalRead, contentLength)
                if (jsonBody == null) {
                    sendResponse(writer, 400, rpcHandler.errorJSON(-32700, "Incomplete body: read $totalRead of $contentLength bytes"))
                    return
                }

                val responseJSON = runBlocking {
                    rpcHandler.handle(jsonBody)
                }

                sendResponse(writer, 200, responseJSON)
            } catch (e: Exception) {
                Log.w(TAG, "Connection error: ${e.message}")
            }
        }
    }

    /**
     * [fix/sweep1014-debug-backup B1] Reads HTTP header lines byte-by-byte
     * from the raw socket stream. No read-ahead buffering: the bytes it
     * consumes are exactly the line + CRLF, so the body that follows can be
     * read byte-exact from the same stream. Header lines are ASCII by HTTP
     * spec; the body is NOT read through this class.
     */
    private class ByteLineReader(private val input: InputStream) {
        private val line = java.io.ByteArrayOutputStream()

        fun readLine(): String? {
            line.reset()
            while (true) {
                val b = input.read()
                if (b < 0) {
                    return if (line.size() == 0) null else String(line.toByteArray(), Charsets.US_ASCII)
                }
                if (b == '\n'.code) {
                    var bytes = line.toByteArray()
                    if (bytes.isNotEmpty() && bytes.last() == '\r'.code.toByte()) {
                        bytes = bytes.copyOf(bytes.size - 1)
                    }
                    return String(bytes, Charsets.US_ASCII)
                }
                line.write(b)
            }
        }
    }

    /// [T-android-debugserver-skill] Capability descriptor. Mirrors the iOS
    /// shape so a shared client can detect the platform: `auth` reports the
    /// Android scheme ("token-lan" — plaintext payloads, token only for
    /// non-loopback) rather than iOS's "v1" envelope, and there is no pair_path.
    private fun schemaJSON(): String {
        val version = try {
            context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "?"
        } catch (_: Exception) {
            "?"
        }
        return """{"app":"MinisApp","platform":"android","version":"$version",""" +
            """"rpc":"jsonrpc-2.0","auth":"token-lan","transport":"plaintext",""" +
            """"rpc_path":"/","skill_path":"/skill"}"""
    }

    /// GET / and /skill. Human clients get raw markdown; tools get JSON with the
    /// manual plus every reference client inlined, so one request bootstraps.
    private fun sendSkill(writer: PrintWriter, wantsHuman: Boolean) {
        val skill = readSkillAsset("SKILL.md")
        if (skill == null) {
            sendResponse(writer, 503, """{"error":"skill assets not bundled in this build"}""")
            return
        }
        if (wantsHuman) {
            sendResponse(writer, 200, skill, "text/markdown; charset=utf-8")
            return
        }
        val py = readSkillAsset("examples/minis_rpc_android.py") ?: ""
        val curl = readSkillAsset("examples/curl.md") ?: ""
        val payload = org.json.JSONObject().apply {
            put("skill", skill)
            put("clients", org.json.JSONObject().apply {
                put("python", py)
                put("curl", curl)
            })
            put("client_paths", org.json.JSONObject().apply {
                put("python", "/skill/examples/python")
                put("curl", "/skill/examples/curl")
            })
        }
        sendResponse(writer, 200, payload.toString())
    }

    private fun sendSkillAsset(writer: PrintWriter, assetPath: String, contentType: String) {
        val body = readSkillAsset(assetPath)
        if (body == null) {
            sendResponse(writer, 503, """{"error":"asset not bundled: $assetPath"}""")
            return
        }
        sendResponse(writer, 200, body, contentType)
    }

    /// Reads from the DEBUG-only asset source set staged by
    /// scripts/gen_debug_skill_android.sh. Absent in release (where this whole
    /// server never starts) and absent if the generator didn't run.
    private fun readSkillAsset(relPath: String): String? = try {
        context.assets.open("debug-skill/$relPath").bufferedReader().use { it.readText() }
    } catch (_: Exception) {
        null
    }

    private fun sendResponse(
        writer: PrintWriter,
        statusCode: Int,
        body: String,
        contentType: String = "application/json",
    ) {
        val statusText = when (statusCode) {
            200 -> "OK"
            400 -> "Bad Request"
            401 -> "Unauthorized"
            405 -> "Method Not Allowed"
            503 -> "Service Unavailable"
            else -> "Error"
        }
        val bytes = body.toByteArray(Charsets.UTF_8)
        writer.print("HTTP/1.1 $statusCode $statusText\r\n")
        writer.print("Content-Type: $contentType\r\n")
        writer.print("Content-Length: ${bytes.size}\r\n")
        writer.print("Connection: close\r\n")
        writer.print("\r\n")
        writer.print(body)
        writer.flush()
    }

    // [T-android-debugserver-auth] No `Access-Control-Allow-Origin` header is
    // sent on any response (including this preflight). Without it a browser
    // cross-origin request is rejected by CORS on the client side, so an
    // on-device web page cannot read the response body even if it obtains a
    // token. adb / curl do not enforce CORS and are unaffected.
    private fun sendCorsPreflightResponse(writer: PrintWriter) {
        writer.print("HTTP/1.1 204 No Content\r\n")
        writer.print("Connection: close\r\n")
        writer.print("\r\n")
        writer.flush()
    }
}

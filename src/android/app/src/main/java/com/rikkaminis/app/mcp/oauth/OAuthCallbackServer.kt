package com.rikkaminis.app.mcp.oauth

import android.util.Log
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.ServerSocket
import java.net.URI

class OAuthCallbackServer(
    private val port: Int,
    private val fallbackPorts: List<Int> = emptyList(),
    private val onCode: (code: String, state: String?) -> Unit,
) {
    companion object {
        private const val TAG = "OAuthCallbackServer"

        // [verify-1015 BUG-12a] Per-connection read budget. The accept loop is
        // single-threaded and readLine() blocks with NO timeout, so one socket
        // that connects and sends nothing (a browser preconnect / speculative
        // connection, which Chrome opens for the authorization page before any
        // redirect) parked the listener; the real callback then queued behind
        // it and OAuth died on its 5-minute timeout. 5 s is ~1000x a real
        // request (a single TCP segment, milliseconds) yet short enough that a
        // stalled socket is dropped long before the user gives up.
        private const val IDLE_CONNECT_TIMEOUT_MS = 5_000
    }

    private var serverSocket: ServerSocket? = null
    @Volatile private var running = false

    /** The port actually bound (may differ from [port] if fallback was used). */
    var boundPort: Int = port
        private set

    /**
     * Invoked by [stop] when the server is shut down by an external
     * caller (e.g. the user dismisses Chrome Custom Tab and the auth
     * manager wants to abort the in-flight wait without waiting for the
     * 5-minute timeout). The callback is _only_ fired when stop() is
     * called by something other than the success path —
     * [onCode] callers set this to null before calling stop() so they
     * don't fire a cancel after a successful redirect.
     *
     * [T-xai-oauth-stop-resume, port iOS d1dbdd5d]
     */
    @Volatile var onExternalCancel: (() -> Unit)? = null

    fun start() {
        running = true
        Thread {
            try {
                val portsToTry = listOf(port) + fallbackPorts
                var bound = false
                for (p in portsToTry) {
                    try {
                        serverSocket = ServerSocket(p, 0, InetAddress.getLoopbackAddress())
                        boundPort = p
                        bound = true
                        break
                    } catch (e: java.net.BindException) {
                        Log.w(TAG, "Port $p in use, trying next...")
                    }
                }
                if (!bound) {
                    Log.e(TAG, "All ports unavailable: $portsToTry")
                    // [sweep1014-D5a] Every port failed to bind: shut the server
                    // down (running=false + socket cleanup) and fire
                    // onExternalCancel so the caller's in-flight wait resumes
                    // immediately instead of hanging until the 5-minute OAuth
                    // timeout. stop() consumes the callback once, so a later
                    // stop() never double-fires.
                    stop()
                    return@Thread
                }
                Log.d(TAG, "Listening on port $boundPort")
                while (running) {
                    val socket = serverSocket?.accept() ?: break
                    try {
                        // [verify-1015 BUG-12a] Bound the per-connection read
                        // (see IDLE_CONNECT_TIMEOUT_MS): a connection that
                        // sends nothing must not park the single-threaded
                        // listener ahead of the real callback. On timeout
                        // readLine() throws SocketTimeoutException, which the
                        // catch below already logs and closes.
                        socket.soTimeout = IDLE_CONNECT_TIMEOUT_MS
                        val reader = BufferedReader(InputStreamReader(socket.getInputStream()))
                        val requestLine = reader.readLine() ?: continue
                        Log.d(TAG, "Request: $requestLine")

                        // CORS preflight for providers (e.g. xAI) that
                        // OPTIONS /callback from their authorization page
                        // before redirecting the browser. Without this we
                        // return a 404 to the preflight and the browser
                        // never follows the redirect — OAuth stalls.
                        // Read the rest of the headers to find Origin and
                        // only echo back permissive CORS for known xAI hosts.
                        if (requestLine.startsWith("OPTIONS")) {
                            var origin: String? = null
                            while (true) {
                                val h = reader.readLine() ?: break
                                if (h.isEmpty()) break
                                val lower = h.lowercase()
                                if (lower.startsWith("origin:")) {
                                    origin = h.substringAfter(":").trim()
                                }
                            }
                            val trustedHosts = listOf("auth.x.ai", "accounts.x.ai")
                            val allowOrigin = if (origin != null && trustedHosts.any { host -> origin == "https://$host" }) {
                                origin
                            } else {
                                "null"
                            }
                            val pre = "HTTP/1.1 204 No Content\r\n" +
                                "Access-Control-Allow-Origin: $allowOrigin\r\n" +
                                "Access-Control-Allow-Methods: GET, OPTIONS\r\n" +
                                "Access-Control-Allow-Headers: *\r\n" +
                                "Access-Control-Max-Age: 600\r\n" +
                                "Connection: close\r\n\r\n"
                            socket.getOutputStream().write(pre.toByteArray())
                            socket.close()
                            continue
                        }

                        // Parse GET /callback?code=xxx&state=yyy HTTP/1.1
                        val parts = requestLine.split(" ")
                        if (parts.size >= 2) {
                            val uri = URI("http://localhost${ parts[1] }")
                            val params = uri.query?.split("&")?.associate {
                                val kv = it.split("=", limit = 2)
                                kv[0] to (if (kv.size > 1) java.net.URLDecoder.decode(kv[1], "UTF-8") else "")
                            } ?: emptyMap()

                            val code = params["code"]
                            val state = params["state"]

                            // Send response
                            val html = "<html><body><h1>Authorization complete</h1><p>You can close this tab.</p><script>window.close()</script></body></html>"
                            val response = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ${html.length}\r\nConnection: close\r\n\r\n$html"
                            socket.getOutputStream().write(response.toByteArray())
                            socket.close()

                            if (code != null) {
                                // [sweep1014-D5b] Consume onExternalCancel before
                                // stopping: stop() here is called by the server
                                // itself (the success path), so the onCode caller
                                // never gets a chance to null it out — without
                                // this a successful redirect also fires a phantom
                                // cancel. External stop() calls are unaffected
                                // and still fire onExternalCancel.
                                onExternalCancel = null
                                onCode(code, state)
                                stop()
                                return@Thread
                            }
                        }
                        socket.close()
                    } catch (e: Exception) {
                        Log.w(TAG, "Error handling connection", e)
                        try { socket.close() } catch (_: Exception) {}
                    }
                }
            } catch (e: Exception) {
                if (running) Log.e(TAG, "Server error", e)
            }
        }.start()
    }

    fun stop() {
        val wasRunning = running
        running = false
        try { serverSocket?.close() } catch (_: Exception) {}
        serverSocket = null
        // Notify external callers (e.g. XAIOAuthManager) so they can
        // cancel an in-flight suspendCancellableCoroutine instead of
        // hanging until the next inbound connection / the 5-min
        // OAuth wait timeout fires. Consume the callback (set to
        // null before invoking) so re-entrant stop() calls don't
        // double-fire (T-xai-oauth-stop-resume).
        if (wasRunning) {
            val cancel = onExternalCancel
            onExternalCancel = null
            try { cancel?.invoke() } catch (e: Exception) {
                Log.w(TAG, "onExternalCancel threw: ${e.message}")
            }
        }
    }
}

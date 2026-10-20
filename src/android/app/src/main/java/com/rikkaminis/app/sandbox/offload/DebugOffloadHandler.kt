package com.rikkaminis.app.sandbox.offload

import android.content.Context
import android.util.Log
import com.rikkaminis.app.sandbox.NativeOffloadHandler
import com.rikkaminis.app.sandbox.NativeOffloadRequest
import com.rikkaminis.app.sandbox.NativeOffloadResult
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException

/**
 * minis-debug — DEBUG-ONLY CLI wrapper for the local DebugServer JSON-RPC
 * endpoint at 127.0.0.1:5321.
 *
 * Lets a user (or agent) in the PRoot shell drive the in-app debug RPC
 * without hand-rolling curl + JSON. Each subcommand maps to one RPC
 * method registered in DebugMethodRegistry; the response envelope from
 * the server is forwarded verbatim through OffloadOutput so the standard
 * `--compact` / `-q` / `--quiet` flags work the same way as every other
 * `android-*` / `minis-*` offload.
 *
 * Registration is guarded by `BuildConfig.DEBUG` in MinisApp.kt so the
 * Release APK contains no trace of this command.
 */
class DebugOffloadHandler(private val context: Context) : NativeOffloadHandler {

    /**
     * [fix/sweep1013-security R3] Same source as [com.rikkaminis.app.debug.DebugServer]'s
     * authToken: the per-install token persisted at `files/debug_server_token`.
     * RC12 made DebugServer require that token from ALL clients (loopback
     * included) but this in-repo client never followed — every `minis-debug`
     * subcommand got 401 in debug builds (auth seam broken — fail-closed
     * functionality wiped out).
     * Read the same file and attach the header; when the file doesn't exist
     * yet (server never started) send nothing and let the server's 401
     * message guide the caller.
     */
    private val authToken: String? by lazy {
        try {
            val f = java.io.File(context.filesDir, "debug_server_token")
            if (f.exists()) f.readText().trim().ifEmpty { null } else null
        } catch (_: Throwable) {
            null
        }
    }


    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val raw = request.argv.drop(1)
        val args = OffloadArgs(raw, booleanFlags = BOOLEAN_FLAGS)
        if (request.argv.size <= 1 || args.hasFlag("h", "help") && args.positional.isEmpty()) {
            return NativeOffloadResult(if (request.argv.size <= 1) 2 else 0, HELP)
        }

        // Per-subcommand --help: `minis-debug ls --help` prints the same
        // top-level help so the user discovers the full surface either
        // way. Cheaper than per-subcommand help strings; the help text
        // already enumerates every subcommand's flags.
        if (args.hasFlag("h", "help")) {
            return NativeOffloadResult(0, HELP)
        }

        // [da1028-A] A second, structural view of the same argv: globals /
        // subcommand / the subcommand's own verbatim tail. `args` above is a
        // GREEDY parse (every `-`-prefixed token anywhere is one of ITS flags),
        // which is right for this command's own options and wrong for the
        // pass-through subcommands — see [splitArgv].
        val argv = splitArgv(raw)

        val sub = argv.subcommand
            ?: return NativeOffloadResult(2, HELP)

        val rpc: RpcCall = try {
            buildRpcCall(sub, args, argv.rawRest)
        } catch (e: BadArgsException) {
            return NativeOffloadResult(2, "minis-debug $sub: ${e.message}\n")
        }

        // [da1031-C] ISSUE-04 B2: ls/read/write used to forward the raw
        // positional as `path`, so a relative token reached the RPC handler and
        // was joined onto the ROOTFS ROOT (`File(rootfsDir, path)`), not the
        // session cwd — `minis-debug write notes.txt` landed at `/notes.txt`.
        // Every sibling handler (Config / BrowserUse / ModelUse / Player) runs
        // the same pure join before its resolver; keep the same instance-wide
        // reading here. Done as a post-pass on the built RpcCall (buildRpcCall's
        // signature and call site are pinned by offload_cli_args_check D7/D8 —
        // do not inline this into it).
        normalizeDebugRpcPaths(sub, rpc.params, request.cwd)

        // [dv16-A03] ls/read/write are session-scoped in the PRoot layout:
        // workspace/attachments/offloads/browser are PER-SESSION bind mounts,
        // and the RPC handler's global resolveHostPath searches EVERY session
        // tree then the rootfs placeholder — a session-relative
        // `minis-debug ls .` (joined onto the caller's cwd above) would
        // list/read/write another session's file, or a new file would vanish
        // into the rootfs ghost dir the session shell cannot see. Forward the
        // caller's session id so DebugRPCHandler resolves through
        // resolvePathForSession (session hit ⇒ never falls back globally).
        // Only the file-system subcommands carry it: `call` keeps its raw
        // params verbatim, and a null session (interactive terminal) keeps
        // the historical global behaviour in the handler's sessionId==null arm.
        if (sub in SESSION_SCOPED_SUBCOMMANDS && !request.sessionId.isNullOrBlank()) {
            rpc.params.put("sessionId", request.sessionId)
        }

        // [da1028-A] Output shaping for the pass-through subcommands reads the
        // GLOBAL slice only (tokens before the subcommand): everything after
        // `exec` / `shizuku` / `model-use` belongs to the child process, so a
        // `-q` meant for the child (`minis-debug exec grep -q x`) must not strip
        // this command's envelope. Every other subcommand keeps the historical
        // whole-argv view (`minis-debug ls /sdcard --compact`).
        val outArgs =
            if (sub in PASSTHROUGH_SUBCOMMANDS) OffloadArgs(argv.globals, BOOLEAN_FLAGS) else args

        return try {
            val response = postJsonRpc(rpc.method, rpc.params)
            // DebugServer returns a JSON-RPC 2.0 envelope: {jsonrpc, id, result|error}.
            // We pass it through formatBody so --compact / -q work; -q strips to
            // the iOS-style {ok, data, error} envelope when present, otherwise
            // returns the raw object. Exit code reflects RPC error.
            val obj = try { JSONObject(response) } catch (_: Exception) { null }
            val isError = obj?.has("error") == true
            NativeOffloadResult(
                if (isError) 1 else 0,
                OffloadOutput.formatBody(response, outArgs) + "\n",
            )
        } catch (e: ConnectionRefused) {
            val body = JSONObject()
                .put("error", "debug_server_unreachable")
                .put("message", "Debug server not running on 127.0.0.1:5321. Is this a debug build?")
                .toString()
            NativeOffloadResult(1, OffloadOutput.formatBody(body, outArgs) + "\n")
        } catch (e: SocketTimeoutException) {
            val body = JSONObject()
                .put("error", "debug_server_timeout")
                .put("message", "Debug server did not respond within ${TIMEOUT_MS / 1000}s.")
                .toString()
            NativeOffloadResult(1, OffloadOutput.formatBody(body, outArgs) + "\n")
        } catch (e: Throwable) {
            Log.w(TAG, "uncaught: ${e.message}", e)
            val body = JSONObject()
                .put("error", "internal")
                .put("message", e.message ?: e.javaClass.simpleName)
                .toString()
            NativeOffloadResult(1, OffloadOutput.formatBody(body, outArgs) + "\n")
        }
    }

    // ── Subcommand → JSON-RPC mapping ────────────────────────────────────────

    private data class RpcCall(val method: String, val params: JSONObject)
    private class BadArgsException(msg: String) : RuntimeException(msg)

    // [dv16-A03] The subcommands whose `path` operand resolves through the
    // per-session bind mounts (workspace/attachments/offloads/browser). Pass-
    // through subcommands (`exec`/`shizuku`/`model-use`) and the raw `call`
    // escape hatch are not path-scoped to the caller's session.
    private val SESSION_SCOPED_SUBCOMMANDS = setOf("ls", "read", "write")

    private fun buildRpcCall(sub: String, args: OffloadArgs, rawRest: List<String>): RpcCall {
        // [da1028-A] `rest` is the argv slice this subcommand owns — for the
        // pass-through subcommands that is the VERBATIM tail (see
        // [forwardedArgv]), not the leftovers of the greedy parse.
        val rest = forwardedArgv(sub, args, rawRest)
        return when (sub) {
            "discover" -> RpcCall("rpc.discover", JSONObject())

            "appInfo" -> RpcCall("debug.appInfo", JSONObject())

            "screenshot" -> {
                val p = JSONObject()
                args.getDouble("scale")?.let { p.put("scale", it) }
                RpcCall("debug.screenshot", p)
            }

            "ls" -> {
                val p = JSONObject()
                rest.firstOrNull()?.let { p.put("path", it) }
                if (args.hasFlag("recursive")) p.put("recursive", true)
                args.getInt("maxDepth")?.let { p.put("maxDepth", it) }
                RpcCall("debug.ls", p)
            }

            "read" -> {
                val path = rest.firstOrNull()
                    ?: throw BadArgsException("missing <path>")
                val p = JSONObject().put("path", path)
                args.getInt("offset")?.let { p.put("offset", it) }
                args.getInt("limit")?.let { p.put("limit", it) }
                if (args.hasFlag("base64")) p.put("base64", true)
                RpcCall("debug.readFile", p)
            }

            "write" -> {
                val path = rest.firstOrNull()
                    ?: throw BadArgsException("missing <path>")
                val content = args.get("content")
                    ?: throw BadArgsException("missing --content <text>")
                val p = JSONObject().put("path", path).put("content", content)
                args.get("encoding")?.let { p.put("encoding", it) }
                RpcCall("debug.writeFile", p)
            }

            "exec" -> {
                if (rest.isEmpty()) throw BadArgsException("missing <command...>")
                RpcCall("debug.shellExecute", JSONObject().put("command", execCommandLine(rest)))
            }

            // Android-only debug-build methods, exposed because they live
            // in DEBUG_ONLY_METHODS of DebugMethodRegistry and are useful
            // from a shell context. Both take an `args` JSON array; we
            // accept either a positional command line or `--args '["a","b"]'`.
            "shizuku" -> RpcCall("debug.shizuku.exec", buildExecForwarder(rest, args))
            "model-use", "modelUse" -> RpcCall("debug.modelUse.exec", buildExecForwarder(rest, args))

            // Generic escape hatch — call any registered method directly.
            // `minis-debug call <method> [--params '<json>']`
            "call" -> {
                val method = rest.firstOrNull()
                    ?: throw BadArgsException("missing <method>")
                val raw = args.get("params")
                val params = if (raw.isNullOrBlank()) JSONObject() else try {
                    JSONObject(raw)
                } catch (_: Exception) {
                    throw BadArgsException("--params must be a JSON object")
                }
                RpcCall(method, params)
            }

            else -> throw BadArgsException("unknown subcommand '$sub' (try `minis-debug --help`)")
        }
    }

    /**
     * Build the `{args: [...]}` envelope expected by `debug.shizuku.exec` /
     * `debug.modelUse.exec`. If the user passes `--args '["foo","bar"]'`
     * we take that verbatim; otherwise we use the remaining positional
     * arguments as the argv slice.
     */
    private fun buildExecForwarder(rest: List<String>, args: OffloadArgs): JSONObject {
        val explicit = args.get("args")
        val argvArr = if (explicit != null) {
            try { JSONArray(explicit) }
            catch (_: Exception) { throw BadArgsException("--args must be a JSON array of strings") }
        } else {
            JSONArray().apply { rest.forEach { put(it) } }
        }
        return JSONObject().put("args", argvArr)
    }

    // ── Raw HTTP POST over Socket ────────────────────────────────────────────

    private class ConnectionRefused(cause: Throwable) : IOException(cause)

    /**
     * Hand-rolled HTTP/1.1 POST so we don't pull in OkHttp or HttpURLConnection's
     * connection-pool quirks for what is a single localhost round-trip.
     * DebugServer reads exactly Content-Length bytes off the wire and writes
     * an HTTP/1.1 response with its own Content-Length header — see
     * DebugServer.handleConnection.
     */
    private fun postJsonRpc(method: String, params: JSONObject): String {
        val body = JSONObject()
            .put("jsonrpc", "2.0")
            .put("id", 1)
            .put("method", method)
            .put("params", params)
            .toString()
        val bodyBytes = body.toByteArray(Charsets.UTF_8)

        val socket = Socket()
        try {
            try {
                socket.connect(InetSocketAddress("127.0.0.1", DEBUG_SERVER_PORT), TIMEOUT_MS)
            } catch (e: java.net.ConnectException) {
                throw ConnectionRefused(e)
            }
            socket.soTimeout = TIMEOUT_MS

            val out: OutputStream = socket.getOutputStream()
            val req = buildString {
                append("POST / HTTP/1.1\r\n")
                append("Host: 127.0.0.1:$DEBUG_SERVER_PORT\r\n")
                append("Content-Type: application/json\r\n")
                append("Content-Length: ${bodyBytes.size}\r\n")
                // [fix/sweep1013-security R3] DebugServer requires the per-install
                // token from ALL clients (loopback included) since RC12.
                if (authToken != null) append("X-Minis-Token: $authToken\r\n")
                append("Connection: close\r\n")
                append("\r\n")
            }.toByteArray(Charsets.UTF_8)
            out.write(req)
            out.write(bodyBytes)
            out.flush()

            return readHttpResponse(BufferedInputStream(socket.getInputStream()))
        } finally {
            try { socket.close() } catch (_: Throwable) {}
        }
    }

    /**
     * Minimal HTTP/1.1 response parser: status line, headers, then body
     * sized by Content-Length (DebugServer always sends one). We don't
     * support chunked transfer because DebugServer doesn't emit it.
     */
    private fun readHttpResponse(input: BufferedInputStream): String {
        // Read header section: bytes up to and including "\r\n\r\n".
        val headerBuf = ByteArrayOutputStream()
        var prev = -1
        var prev2 = -1
        var prev3 = -1
        while (true) {
            val b = input.read()
            if (b == -1) throw IOException("debug server closed connection before headers complete")
            headerBuf.write(b)
            if (prev3 == 0x0D && prev2 == 0x0A && prev == 0x0D && b == 0x0A) break
            prev3 = prev2
            prev2 = prev
            prev = b
        }
        val headerText = headerBuf.toString("UTF-8")
        var contentLength = -1
        for (line in headerText.split("\r\n")) {
            val idx = line.indexOf(':')
            if (idx <= 0) continue
            if (line.substring(0, idx).equals("Content-Length", ignoreCase = true)) {
                contentLength = line.substring(idx + 1).trim().toIntOrNull() ?: -1
            }
        }
        if (contentLength < 0) {
            // Fall back to read-to-EOF — DebugServer normally sets the
            // header, but we'd rather degrade than fail outright.
            val rest = ByteArrayOutputStream()
            val buf = ByteArray(4096)
            while (true) {
                val n = input.read(buf)
                if (n <= 0) break
                rest.write(buf, 0, n)
            }
            return rest.toString("UTF-8")
        }

        val bodyBuf = ByteArray(contentLength)
        var read = 0
        while (read < contentLength) {
            val n = input.read(bodyBuf, read, contentLength - read)
            if (n < 0) throw IOException("debug server closed connection at $read / $contentLength bytes")
            read += n
        }
        return String(bodyBuf, Charsets.UTF_8)
    }

    companion object {
        private const val TAG = "DebugOffload"
        private const val DEBUG_SERVER_PORT = 5321
        private const val TIMEOUT_MS = 30_000

        /**
         * Declared boolean flags so OffloadArgs doesn't greedily consume the
         * following positional token as a value. e.g. `--recursive /sdcard`
         * must parse as flag + positional, not `--recursive=/sdcard`.
         */
        private val BOOLEAN_FLAGS = setOf("recursive", "base64")

        /**
         * [da1028-A] Subcommands whose own argv is forwarded verbatim to a
         * child process (or another CLI). For these, everything after the
         * subcommand token belongs to the child — see [splitArgv] /
         * [forwardedArgv].
         */
        private val PASSTHROUGH_SUBCOMMANDS = setOf("exec", "shizuku", "model-use", "modelUse")

        /** [da1028-A] See [splitArgv]. */
        internal data class DebugArgvSplit(
            val globals: List<String>,
            val subcommand: String?,
            val rawRest: List<String>,
        )

        /**
         * [da1028-A] Structural split of `request.argv.drop(1)`:
         * `[globals…] <subcommand> [tail…]`.
         *
         * The greedy [OffloadArgs] view treats every `-`-prefixed token
         * ANYWHERE as one of this command's own flags, and pairs `--key value`
         * across the whole invocation. That is right for options this command
         * owns (`--offset`, `--limit`, …) and wrong for the pass-through
         * subcommands, where the tail is another program's argv:
         * `minis-debug exec ls -l` lost the `-l` (it became this command's
         * boolean flag "l") and the child ran a bare `ls`, and
         * `minis-debug model-use run --model x` lost the whole `--model x`
         * pair (it became this command's `--model` option). Neither failure is
         * visible in the reply — the child just runs something slightly
         * different from what was typed.
         *
         * `globals` (tokens before the subcommand) is what the output-shaping
         * flags are read from for those subcommands, so a `-q` that belongs to
         * the child (`exec grep -q x`) cannot strip this command's envelope.
         * The subcommand is the first token that is neither an option this
         * command owns nor the VALUE of one — the prefix is walked with the
         * same `--key value` pairing [OffloadArgs] uses, so the historical
         * `minis-debug --args '["pm","list"]' shizuku` ordering still finds
         * `shizuku` instead of mistaking the JSON for a subcommand. A leading
         * `--` is dropped as the end-of-options marker, and a `--` opening the
         * tail is dropped too (`exec -- grep -v x` runs `grep -v x`); neither
         * is ever the subcommand.
         */
        internal fun splitArgv(tokens: List<String>): DebugArgvSplit {
            val globals = mutableListOf<String>()
            var i = 0
            while (i < tokens.size) {
                val t = tokens[i]
                if (t == "--") { i++; break }
                if (!t.startsWith("-")) break
                globals.add(t)
                val isLong = t.startsWith("--")
                val key = if (isLong) t.substring(2).substringBefore('=') else null
                val carriesInlineValue = isLong && t.contains('=')
                // Same rule as [OffloadArgs]: an undeclared `--key` takes the
                // next token as its value unless that token is itself an
                // option. The value is global, not the subcommand.
                val takesNextValue = isLong && !carriesInlineValue &&
                    key !in BOOLEAN_FLAGS && key !in OffloadOutput.OUTPUT_FLAGS &&
                    i + 1 < tokens.size && !tokens[i + 1].startsWith("-")
                if (takesNextValue) {
                    globals.add(tokens[i + 1])
                    i++
                }
                i++
            }
            if (i >= tokens.size) return DebugArgvSplit(globals, null, emptyList())
            val tail = tokens.subList(i + 1, tokens.size).toList()
            val rest = if (tail.firstOrNull() == "--") tail.drop(1) else tail
            return DebugArgvSplit(globals, tokens[i], rest)
        }

        /**
         * [da1028-A] The argv slice a subcommand may build its RPC from: the
         * verbatim tail for the pass-through set, this command's parsed
         * positionals (minus the subcommand token) for everything else.
         */
        internal fun forwardedArgv(
            sub: String,
            args: OffloadArgs,
            rawRest: List<String>,
        ): List<String> = if (sub in PASSTHROUGH_SUBCOMMANDS) rawRest else args.positional.drop(1)

        /**
         * [da1028-A] `exec`'s command line: the verbatim tail joined with single
         * spaces (lang-exec semantics — the child's own shell splits it again).
         */
        internal fun execCommandLine(argv: List<String>): String = argv.joinToString(" ")

        private const val HELP = """minis-debug — CLI for the in-app DebugServer JSON-RPC (debug builds only)

Usage:
  minis-debug <subcommand> [options]

SUBCOMMANDS:
  discover                           List all registered RPC methods (rpc.discover)
  appInfo                            Show platform / SDK / build info (debug.appInfo)
  screenshot [--scale N]             Capture foreground Activity (debug.screenshot)

  ls [path] [--recursive] [--maxDepth N]
                                     List a path inside the PRoot rootfs (debug.ls)
  read <path> [--offset N] [--limit N] [--base64]
                                     Read a file from PRoot rootfs (debug.readFile)
  write <path> --content <text> [--encoding utf8|base64]
                                     Write a file inside PRoot rootfs (debug.writeFile)
  exec <command...>                  Run a shell command via debug.shellExecute

Android-only (DEBUG_ONLY_METHODS in DebugMethodRegistry):
  shizuku <argv...>                  Invoke android-shizuku-cli (debug.shizuku.exec)
  model-use <argv...>                Invoke minis-model-use (debug.modelUse.exec)

Escape hatch (for any method not listed above):
  call <method> [--params '<json>']  Invoke an arbitrary registered method

GLOBAL OPTIONS:
  --help, -h                         Show this help
  --compact                          Minimize JSON output
  -q, --quiet                        Output only the data/error field of the
                                     iOS-style envelope (when present)

EXAMPLES:
  minis-debug discover
  minis-debug appInfo
  minis-debug ls /sdcard --recursive --maxDepth 2
  minis-debug read /etc/passwd --offset 0 --limit 100
  minis-debug exec uname -a
  minis-debug call debug.logs.list --params '{}'

PASS-THROUGH ARGUMENTS:
  Everything after `exec` / `shizuku` / `model-use` is forwarded VERBATIM to the
  child: `minis-debug exec grep -q x` runs `grep -q x`, and `-q` is the child's
  flag, not this command's. Put global options BEFORE the subcommand
  (`minis-debug --compact exec ls`). A leading `--` is dropped as the
  end-of-options marker (`minis-debug exec -- grep -v x` runs `grep -v x`).

The handler talks to 127.0.0.1:$DEBUG_SERVER_PORT (DebugServer). If the server
isn't running you'll see `debug_server_unreachable` — Release builds don't
ship this command at all.

AUTH: the server requires the per-install token from every client (loopback
included). The handler reads it from the same file the server uses
(`files/debug_server_token`) and sends `X-Minis-Token` automatically — no
configuration needed.
"""
    }
}

/**
 * [da1031-C] Subcommands whose single positional IS a file path — the only ones
 * `normalizeDebugRpcPaths` may rewrite (see there for why).
 */
internal val DEBUG_PATH_SUBCOMMANDS = setOf("ls", "read", "write")

/**
 * [da1031-C] Relative → absolute join for a `ls` / `read` / `write` positional:
 * `null` when this subcommand takes no path positional, or the caller omitted
 * it (such a call keeps the RPC default — resolve against the resolved cwd /
 * rootfs — which is what `minis-debug ls` with no argument relies on). Pure
 * String→String? so the subcommand scoping and the pass-through cases are
 * JVM-testable without an RPC round trip.
 */
internal fun debugPathFor(sub: String, rawPath: String, cwd: String): String? =
    if (sub in DEBUG_PATH_SUBCOMMANDS && rawPath.isNotEmpty()) {
        resolveMaybeRelative(rawPath, cwd)
    } else null

/**
 * [da1031-C] ISSUE-04 B2 — post-pass over the built RPC params: `ls` / `read` /
 * `write` take a path positional, and a RELATIVE one used to reach
 * DebugRPCHandler verbatim, where it was joined onto the rootfs ROOT instead of
 * the session cwd. Normalizing here keeps DebugRPCHandler's own contract
 * (absolute paths only) intact while making the CLI behave like every sibling
 * handler (Config / BrowserUse / ModelUse / Player) — they all route file args
 * through [resolveMaybeRelative], so the same relative token reads the same
 * file from the same instance.
 */
internal fun normalizeDebugRpcPaths(sub: String, params: JSONObject, cwd: String) {
    val normalized = debugPathFor(sub, params.optString("path"), cwd) ?: return
    params.put("path", normalized)
}

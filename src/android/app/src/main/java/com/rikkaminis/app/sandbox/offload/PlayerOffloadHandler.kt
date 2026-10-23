package com.rikkaminis.app.sandbox.offload

import android.content.Context
import com.rikkaminis.app.offload.MediaPlayerManager
import com.rikkaminis.app.offload.PlayerOutcome
import com.rikkaminis.app.offload.playerEnvelope
import com.rikkaminis.app.sandbox.NativeOffloadHandler
import com.rikkaminis.app.sandbox.NativeOffloadRequest
import com.rikkaminis.app.sandbox.NativeOffloadResult

/**
 * android-player — control audio playback.
 *
 * Usage:
 *   android-player play <session> <path>
 *   android-player pause <session>
 *   android-player resume <session>
 *   android-player seek <session> <position_ms>
 *   android-player stop <session>
 *   android-player status <session>
 *   android-player list
 *
 * [dv25-A4 A-07] The payload is JSON on EVERY path, success and refusal
 * alike. Before, the only `formatBody` input was the manager's English
 * sentence (`"Playing '…'"`, `"Error: no session '…'"`, `"No active media
 * sessions."`), and `OffloadOutput.formatBody` is a pass-through for prose —
 * so `-c` / `-q` were no-ops and `jq` / `json.loads` broke on success while
 * the uncaught-exception branch answered `{"error":"player_failed"}`: one
 * command, two formats. [MediaPlayerManager] now returns fields
 * ([PlayerOutcome]) and [playerEnvelope] — the sibling handlers' `{ok, tool,
 * action, data}` shape — assembles the body; no prose is parsed and no
 * `startsWith("Error")` sniff decides the exit code.
 *
 * Exit codes are unchanged: 0 = command ran (a live session that cannot
 * report a position still exits 0 and carries `player_error`), 1 = refusal,
 * 2 = bad arguments. `--help` and the exit-2 usage strings deliberately
 * never reach `formatBody`: they are human text, not a payload.
 */
class PlayerOffloadHandler(private val context: Context) : NativeOffloadHandler {
    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(request.argv.drop(1))
        // [T-offload-defaults-batch-android] Help only on explicit
        // --help/-h; no subcommand defaults to `list` (NOT `status`, which
        // requires a <session> id and would exit 2 with zero args).
        if (args.hasFlag("h", "help")) {
            return NativeOffloadResult(0, HELP)
        }

        val sub = args.positional.firstOrNull() ?: "list"
        val id = args.positional.getOrNull(1)
        val outcome = try {
            when (sub) {
                "play" -> {
                    val path = args.positional.getOrNull(2)
                    if (id == null || path == null) {
                        return NativeOffloadResult(2, "android-player play: need <session> <path>\n")
                    }
                    // [da1025-A] Relative play paths (`./y.wav`, `Music/x.mp3`)
                    // are joined against the session cwd first — previously
                    // they were passed raw and 100% failed against the
                    // global resolver. The chat session id rides along so
                    // MediaPlayerManager resolves session-scoped (fail-closed)
                    // instead of ignoring the per-session sandbox isolation.
                    val resolved = resolveMaybeRelative(path, request.cwd)
                    MediaPlayerManager.play(id, resolved, chatSessionId = request.sessionId, context = context)
                }
                "pause" -> {
                    if (id == null) return NativeOffloadResult(2, "android-player pause: need <session>\n")
                    MediaPlayerManager.pause(id)
                }
                "resume" -> {
                    if (id == null) return NativeOffloadResult(2, "android-player resume: need <session>\n")
                    MediaPlayerManager.resume(id)
                }
                "seek" -> {
                    val pos = args.positional.getOrNull(2)?.toIntOrNull()
                    if (id == null || pos == null) {
                        return NativeOffloadResult(2, "android-player seek: need <session> <position_ms>\n")
                    }
                    MediaPlayerManager.seek(id, pos)
                }
                "stop" -> {
                    if (id == null) return NativeOffloadResult(2, "android-player stop: need <session>\n")
                    MediaPlayerManager.stop(id)
                }
                "status" -> {
                    if (id == null) return NativeOffloadResult(2, "android-player status: need <session>\n")
                    MediaPlayerManager.status(id)
                }
                "list" -> MediaPlayerManager.listSessions()
                else -> return NativeOffloadResult(2, "android-player: unknown subcommand '$sub'\n$HELP")
            }
        } catch (e: Throwable) {
            // Same code the pre-fix catch branch emitted, now carried through
            // the one envelope so an uncaught failure and a refusal share a
            // shape.
            PlayerOutcome.failure(PlayerOutcome.ERROR_PLAYER_FAILED, e.message ?: "unknown")
        }
        val envelope = playerEnvelope(sub, outcome)
        return NativeOffloadResult(
            if (outcome.ok) 0 else 1,
            OffloadOutput.formatBody(envelope.toString(2), args) + "\n",
        )
    }

    companion object {
        private const val HELP = """android-player — control audio playback sessions

Usage:
  android-player                    Same as `list` (default subcommand)
  android-player play <session> <path>
  android-player pause <session>
  android-player resume <session>
  android-player seek <session> <position_ms>
  android-player stop <session>
  android-player status <session>
  android-player list
"""
    }
}

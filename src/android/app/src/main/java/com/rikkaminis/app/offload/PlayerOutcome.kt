package com.rikkaminis.app.offload

import org.json.JSONArray
import org.json.JSONObject

/**
 * [dv25-A4 A-07] The structured result of one `android-player` command.
 *
 * Why this type exists (defect it replaces): [MediaPlayerManager] used to
 * answer every command with an ENGLISH SENTENCE (`"Playing 'x.wav'
 * (session=s, type=audio, duration=0:01)"`, `"Error: no session 's'"`,
 * `"Active media sessions (2):\n  - …"`). `PlayerOffloadHandler` handed
 * those strings straight to `OffloadOutput.formatBody`, which only re-shapes
 * JSON — prose passes through untouched — so `-c` / `-q` were no-ops and the
 * single JSON path in that command was the uncaught-exception branch. A `jq`
 * / `json.loads` reader broke on success while the failure path answered
 * JSON: one command, two formats.
 *
 * The manager now hands over FIELDS (this type) and
 * [playerEnvelope] assembles the envelope, so the payload shape cannot drift
 * from the data: there is no prose to parse and no string prefix to sniff
 * (`result.startsWith("Error")` used to be the only error signal).
 *
 * Android-free on purpose (no `MediaPlayer`, no `Context`, no `Log`): the
 * envelope decision is unit-testable on the JVM, which is where the shape is
 * pinned. `MediaPlayerManager` stays the owner of the player state machine
 * and of every lock; this file owns only "what does the CLI print".
 *
 * Absent fields are OMITTED rather than emitted as JSON null: a stopped
 * session has no `position_ms` (the native player cannot answer
 * `currentPosition`), and a fabricated 0 would read as "at the start".
 */
data class PlayerOutcome(
    val ok: Boolean,
    val session: String? = null,
    val state: String? = null,
    val file: String? = null,
    val mediaType: String? = null,
    val positionMs: Int? = null,
    val durationMs: Int? = null,
    val sessions: List<PlayerSessionSnapshot>? = null,
    val playerError: String? = null,
    val error: String? = null,
    val message: String? = null,
) {
    companion object {
        /**
         * Catch-all code, kept identical to the one the pre-existing
         * uncaught-exception branch already emitted (`{"error":"player_failed"}`)
         * so a consumer keying on that value keeps working.
         */
        const val ERROR_PLAYER_FAILED = "player_failed"

        /** A command that produced a result. */
        fun ok(
            session: String? = null,
            state: String? = null,
            file: String? = null,
            mediaType: String? = null,
            positionMs: Int? = null,
            durationMs: Int? = null,
            sessions: List<PlayerSessionSnapshot>? = null,
            playerError: String? = null,
        ): PlayerOutcome = PlayerOutcome(
            ok = true,
            session = session,
            state = state,
            file = file,
            mediaType = mediaType,
            positionMs = positionMs,
            durationMs = durationMs,
            sessions = sessions,
            playerError = playerError,
        )

        /**
         * A refusal. [code] is machine-readable (`no_session`, `not_playing`,
         * `file_not_found`, …) so a caller never has to match prose, and
         * [message] keeps the human detail the old sentence carried (state,
         * resolved path, platform exception text).
         */
        fun failure(code: String, message: String): PlayerOutcome =
            PlayerOutcome(ok = false, error = code, message = message)
    }
}

/** One row of `android-player list` — a live session's metadata. */
data class PlayerSessionSnapshot(
    val session: String,
    val state: String,
    val file: String,
    val mediaType: String,
)

/** Tool name used by the envelope; mirrors the CLI name the agent invokes. */
internal const val PLAYER_TOOL_NAME = "android-player"

/**
 * The CLI envelope for one [PlayerOutcome]: `{ok, tool, action, data}` on
 * success, `{ok:false, tool, action, error, message}` on refusal.
 *
 * Same shape the sibling handlers already answer with
 * (`AccessibilityOffloadHandler.ok/err`, `BrowserUseOffloadHandler.envelope`,
 * `SessionsOffloadHandler.emit/errorEnvelope`) — deliberately NOT a new
 * dialect: `OffloadOutput`'s `-q` mode is documented against the
 * `{ok, data, error}` envelope and extracts `data` (success) / `error`
 * (failure) from exactly this layout.
 *
 * `error` is a machine code string (not the prose sentence, and not a
 * nested object): `-q` on a refusal then yields the code itself, which is
 * what a script branches on. `message` rides alongside for humans.
 */
internal fun playerEnvelope(action: String, outcome: PlayerOutcome): JSONObject {
    val envelope = JSONObject()
        .put("ok", outcome.ok)
        .put("tool", PLAYER_TOOL_NAME)
        .put("action", action)
    if (!outcome.ok) {
        return envelope
            .put("error", outcome.error ?: PlayerOutcome.ERROR_PLAYER_FAILED)
            .put("message", outcome.message ?: "")
    }
    return envelope.put("data", playerData(outcome))
}

private fun playerData(outcome: PlayerOutcome): JSONObject {
    val data = JSONObject()
    outcome.session?.let { data.put("session", it) }
    outcome.state?.let { data.put("state", it) }
    outcome.file?.let { data.put("file", it) }
    outcome.mediaType?.let { data.put("type", it) }
    outcome.positionMs?.let { data.put("position_ms", it) }
    outcome.durationMs?.let { data.put("duration_ms", it) }
    // A player that errored is still a live session: report the failure as a
    // field instead of collapsing the command into an error (the pre-fix
    // prose did the same and still exited 0).
    outcome.playerError?.let { data.put("player_error", it) }
    outcome.sessions?.let { rows ->
        // `count` + an ALWAYS-present array: an empty list and a populated one
        // must answer the same top-level type (the `-c`/`jq` lesson from
        // PhotosOffloadHandler's empty-vs-array flip).
        val array = JSONArray()
        for (row in rows) {
            array.put(
                JSONObject()
                    .put("session", row.session)
                    .put("state", row.state)
                    .put("file", row.file)
                    .put("type", row.mediaType),
            )
        }
        data.put("count", rows.size)
        data.put("sessions", array)
    }
    return data
}

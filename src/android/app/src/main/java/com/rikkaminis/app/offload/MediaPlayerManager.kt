package com.rikkaminis.app.offload

import android.media.MediaPlayer
import android.util.Log
import android.content.Context
import com.rikkaminis.app.sandbox.PRootKernel
import java.io.File
import java.util.concurrent.ConcurrentHashMap

/**
 * Manages multiple concurrent MediaPlayer sessions keyed by session ID.
 * Supports audio playback with play/pause/resume/seek/stop/status operations.
 * Paths are resolved through PRootKernel.resolveHostPath().
 */
object MediaPlayerManager {

    private const val TAG = "MediaPlayerManager"

    private data class Session(
        val player: MediaPlayer,
        val filePath: String,
        val mediaType: String, // "audio" or "video"
        // [da1022-C] Callbacks (onCompletion/onError) write state on the
        // player's binder/playback threads while play()/pause()/resume()/
        // stop() write it on the caller thread. The map is a
        // ConcurrentHashMap, but that only orders the map itself — it gives
        // no happens-before edge for this field, so a reader could keep
        // observing a stale PLAYING/PAUSED after a terminalize and hand a
        // released player to stop()/pause(). @Volatile is the minimal means:
        // the transitions are single-writer, so visibility (not atomicity) is
        // the missing half. Chosen over "one lock for every read/write"
        // because that would put player binder calls (stop/pause/seek) under
        // the lock for no additional guarantee.
        @Volatile var state: PlayerState = PlayerState.IDLE,
    )

    enum class PlayerState { IDLE, PLAYING, PAUSED, STOPPED }

    /**
     * [T-android-media-sessions-thread-safe] Concurrent map so concurrent
     * sessions (agent operating media across two sessions at once, plus the
     * player's onCompletion/onError callbacks firing on non-main threads) don't
     * race on the map itself. Player state transitions remain best-effort (see
     * Session.state), but the container is now safe for concurrent access.
     */
    private val sessions = ConcurrentHashMap<String, Session>()

    // [sweep1014-D11] Player callbacks (onCompletion/onError) fire on the
    // player's binder/playback threads while play()/stop() mutate the map on
    // the caller thread — all state writes and the check-then-act
    // registration window take this lock.
    private val lock = Any()

    private val audioExtensions = setOf(
        "mp3", "wav", "ogg", "flac", "aac", "m4a", "wma", "opus", "amr", "mid", "midi",
    )
    private val videoExtensions = setOf(
        "mp4", "mkv", "avi", "mov", "webm", "3gp", "flv", "wmv", "ts", "m4v",
    )

    /**
     * Start playback of a file in a new or existing session.
     * If the session already exists, the previous player is released first.
     *
     * [da1025-A] `chatSessionId` + `context` opt into the session-aware
     * resolution chain: the (handler-joined) path is resolved through
     * [PRootKernel.resolvePathForSession] so per-session sandbox dirs are
     * honoured and a `..`/symlink escape out of the session tree fails
     * closed; paths outside `/var/minis/` (user mounts, rootfs) still
     * resolve globally via that resolver's internal fallbacks. Callers
     * without a `context` (none today besides PlayerOffloadHandler) keep
     * the pre-fix global [PRootKernel.resolveHostPath] behaviour as the
     * safe default.
     */
    fun play(
        sessionId: String,
        filePath: String,
        chatSessionId: String? = null,
        context: Context? = null,
    ): PlayerOutcome {
        // [da1022-C] Releasing the previous player for this id happens UNDER
        // the lock the terminalize callbacks take: unlocked, the
        // onCompletion/onError handler could be mid-releaseSession() on the
        // same instance while this path released it a second time (same shape
        // as the check-then-act backstop below, which already routes the
        // replaced-instance release through releaseSession outside the lock —
        // that one stays outside because put() already removed it from the
        // map, so no second owner can reach it).
        synchronized(lock) {
            sessions[sessionId]?.let { existing ->
                releaseSession(existing)
            }
        }

        // [da1025-A] Session-aware resolution: the pure decision core keeps
        // the fail-closed arm testable off-device. A null from the session
        // resolver means "rejected or absent" and is returned as a play
        // error — never re-guessed through the global resolver, whose elvis
        // fallback would reopen the cross-session read hole
        // resolvePathForSession documents.
        val sessionResolver: ((String?, String) -> File?)? = context?.let { ctx ->
            { sid: String?, path: String -> PRootKernel.resolvePathForSession(sid, path, ctx) }
        }
        val hostFile = playerPathResolve(
            chatSessionId = chatSessionId,
            resolvedPath = filePath,
            resolveSession = sessionResolver,
            resolveGlobal = { PRootKernel.resolveHostPath(it) },
        ) ?: return PlayerOutcome.failure(
            "path_unresolved",
            "cannot resolve path '$filePath' (session=${chatSessionId ?: "none"})",
        )

        if (!hostFile.exists()) {
            return PlayerOutcome.failure(
                "file_not_found",
                "file not found at '$filePath' (resolved: ${hostFile.absolutePath})",
            )
        }

        val ext = hostFile.extension.lowercase()
        val mediaType = when {
            audioExtensions.contains(ext) -> "audio"
            videoExtensions.contains(ext) -> "video"
            else -> "audio" // default to audio for unknown extensions
        }

        if (mediaType == "video") {
            return PlayerOutcome.failure(
                "video_unsupported",
                "video playback requires a SurfaceView and is not supported in headless mode. " +
                    "Only audio files can be played.",
            )
        }

        // [audit-0909 T3-M3] Construct the player OUTSIDE the try so the
        // catch can release it: `apply { setDataSource/prepare/start }`
        // throws on a corrupt or unsupported file, and the old code dropped
        // the reference without release() — every failed play leaked a
        // native MediaPlayer (codec + buffers).
        val player = MediaPlayer()
        return try {
            player.apply {
                setDataSource(hostFile.absolutePath)
                prepare()
                start()
            }

            val session = Session(
                player = player,
                filePath = filePath,
                mediaType = mediaType,
                state = PlayerState.PLAYING,
            )

            player.setOnCompletionListener {
                Log.d(TAG, "Playback completed: session=$sessionId")
                // [sweep1014-D11] Release the native player here — after
                // completion it is terminal and would otherwise keep its
                // AudioTrack/codec handles alive until GC. The entry itself is
                // kept as lightweight metadata (filePath/mediaType/state) so
                // status/list still report it and stop() still removes it;
                // the state guards in pause/resume/seek keep callers off the
                // released player. Runs on the callback thread — lock guards
                // against concurrent state/map writes from play()/stop().
                synchronized(lock) { releaseSession(session) }
            }

            player.setOnErrorListener { _, what, extra ->
                Log.e(TAG, "MediaPlayer error: session=$sessionId what=$what extra=$extra")
                synchronized(lock) { releaseSession(session) }
                true
            }

            // [sweep1014-D11] check-then-act backstop: two concurrent play()
            // calls with the same sessionId can both observe no prior session,
            // each build a player, and the later put() would overwrite the
            // earlier one — the overwritten session loses its last reference
            // with a live native codec and is never released. put() returns
            // the replaced instance; release it under the same lock that
            // guards callback terminalize.
            val replaced = synchronized(lock) { sessions.put(sessionId, session) }
            replaced?.let { releaseSession(it) }

            val durationMs = player.duration
            PlayerOutcome.ok(
                session = sessionId,
                state = PlayerState.PLAYING.name,
                file = filePath,
                mediaType = mediaType,
                durationMs = durationMs,
            )
        } catch (e: Exception) {
            Log.e(TAG, "Failed to play $filePath", e)
            // [audit-0909 T3-M3] release the native codec on the failure path
            runCatching { player.release() }
            PlayerOutcome.failure("play_failed", "failed to play '$filePath': ${e.message}")
        }
    }

    /**
     * [dv25-A4 A-07] Every command answers a [PlayerOutcome] instead of an
     * English sentence: the CLI's payload has to be JSON (see
     * playerEnvelope()), and a `startsWith("Error")` sniff was the only
     * error signal the prose form offered. Refusals carry a machine code plus
     * the human detail the old sentence held; positions/durations are raw
     * milliseconds (a `m:ss` string cannot be summed by a caller).
     */
    fun pause(sessionId: String): PlayerOutcome {
        val session = sessions[sessionId]
            ?: return PlayerOutcome.failure("no_session", "no session '$sessionId'")

        if (session.state != PlayerState.PLAYING) {
            return PlayerOutcome.failure(
                "not_playing",
                "session '$sessionId' is not playing (state=${session.state})",
            )
        }

        return try {
            session.player.pause()
            session.state = PlayerState.PAUSED
            PlayerOutcome.ok(
                session = sessionId,
                state = PlayerState.PAUSED.name,
                positionMs = session.player.currentPosition,
            )
        } catch (e: Exception) {
            PlayerOutcome.failure(PlayerOutcome.ERROR_PLAYER_FAILED, e.message ?: "unknown")
        }
    }

    fun resume(sessionId: String): PlayerOutcome {
        val session = sessions[sessionId]
            ?: return PlayerOutcome.failure("no_session", "no session '$sessionId'")

        if (session.state != PlayerState.PAUSED) {
            return PlayerOutcome.failure(
                "not_paused",
                "session '$sessionId' is not paused (state=${session.state})",
            )
        }

        return try {
            session.player.start()
            session.state = PlayerState.PLAYING
            PlayerOutcome.ok(
                session = sessionId,
                state = PlayerState.PLAYING.name,
                positionMs = session.player.currentPosition,
            )
        } catch (e: Exception) {
            PlayerOutcome.failure(PlayerOutcome.ERROR_PLAYER_FAILED, e.message ?: "unknown")
        }
    }

    fun seek(sessionId: String, positionMs: Int): PlayerOutcome {
        val session = sessions[sessionId]
            ?: return PlayerOutcome.failure("no_session", "no session '$sessionId'")

        if (session.state == PlayerState.STOPPED || session.state == PlayerState.IDLE) {
            return PlayerOutcome.failure(
                "cannot_seek",
                "session '$sessionId' is ${session.state} – cannot seek",
            )
        }

        return try {
            session.player.seekTo(positionMs)
            PlayerOutcome.ok(
                session = sessionId,
                state = session.state.name,
                positionMs = positionMs,
            )
        } catch (e: Exception) {
            PlayerOutcome.failure(PlayerOutcome.ERROR_PLAYER_FAILED, e.message ?: "unknown")
        }
    }

    fun stop(sessionId: String): PlayerOutcome {
        // [da1022-C] remove() must be atomic with the release: unlocked
        // "release then remove" races a concurrent play() that re-registers
        // this id — the remove() would then drop the fresh session from the
        // map while its player is still live and unreferenced (leak + the
        // caller sees "no session" for a player that is still playing).
        val removed = synchronized(lock) { sessions.remove(sessionId) }
            ?: return PlayerOutcome.failure("no_session", "no session '$sessionId'")

        releaseSession(removed)
        return PlayerOutcome.ok(session = sessionId, state = PlayerState.STOPPED.name)
    }

    fun status(sessionId: String): PlayerOutcome {
        val session = sessions[sessionId]
            ?: return PlayerOutcome.failure("no_session", "no session '$sessionId'")

        // A terminal session has no readable position/duration (the native
        // player is already released); the fields are omitted rather than
        // reported as 0, which would read as "at the start".
        val live = session.state != PlayerState.STOPPED && session.state != PlayerState.IDLE
        return try {
            PlayerOutcome.ok(
                session = sessionId,
                state = session.state.name,
                file = session.filePath,
                mediaType = session.mediaType,
                positionMs = if (live) session.player.currentPosition else null,
                durationMs = if (live) session.player.duration else null,
            )
        } catch (e: Exception) {
            PlayerOutcome.ok(
                session = sessionId,
                state = session.state.name,
                file = session.filePath,
                mediaType = session.mediaType,
                playerError = e.message ?: "unknown",
            )
        }
    }

    /**
     * Sorted by session id: `ConcurrentHashMap` iteration order is
     * unspecified, so the pre-fix prose listed sessions in a different order
     * on every call — a caller diffing two `list` answers saw phantom churn.
     */
    fun listSessions(): PlayerOutcome = PlayerOutcome.ok(
        sessions = sessions.map { (id, session) ->
            PlayerSessionSnapshot(
                session = id,
                state = session.state.name,
                file = session.filePath,
                mediaType = session.mediaType,
            )
        }.sortedBy { it.session },
    )

    // ── Internal ────────────────────────────────────────────────────────

    /**
     * [da1025-A] Pure decision core of the play() path resolution: no
     * [Context], no [PRootKernel] state, so the JVM test source set can pin
     * the three arms — session resolver present → its result verbatim; a
     * null from it is "rejected or absent" and is returned as null (callers
     * turn that into a play error, never a global re-guess — the elvis
     * fallback would reopen the cross-session read hole
     * [PRootKernel.resolvePathForSession] documents); context-less callers
     * fall back to the pre-fix global resolver.
     */
    internal fun <T> playerPathResolve(
        chatSessionId: String?,
        resolvedPath: String,
        resolveSession: ((String?, String) -> T?)?,
        resolveGlobal: (String) -> T?,
    ): T? = if (resolveSession != null) {
        resolveSession(chatSessionId, resolvedPath)
    } else {
        resolveGlobal(resolvedPath)
    }

    private fun releaseSession(session: Session) {
        // [da1022-C] stop() and release() are TWO try blocks on purpose — same
        // shape as InlineMediaPlayer.onDispose. stop() throws
        // IllegalStateException whenever the player has already left the
        // playable states (the onError terminalize is exactly that case: the
        // player is in Error, our state may still read PLAYING/PAUSED). With
        // one shared try the throw skipped release() and the native
        // codec/AudioTrack handles stayed alive until GC; release() must run
        // on EVERY path.
        try {
            if (session.state == PlayerState.PLAYING || session.state == PlayerState.PAUSED) {
                session.player.stop()
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error stopping player before release: ${e.message}")
        }
        try {
            session.player.release()
            session.state = PlayerState.STOPPED
        } catch (e: Exception) {
            Log.w(TAG, "Error releasing player: ${e.message}")
        }
    }
}

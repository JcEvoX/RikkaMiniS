package com.rikkaminis.app.offload

import android.media.MediaPlayer
import java.lang.reflect.Modifier
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1022 ISSUE-03] releaseSession must release the native player
 * on EVERY path.
 *
 * Defect shape being pinned: stop() and release() shared one try block.
 * MediaPlayer.stop() throws IllegalStateException once the player has left
 * the playable states — which is exactly the state the onError terminalize
 * runs in — so the throw skipped release() and every error path leaked the
 * native codec + AudioTrack handles until GC. Same-family fix shape already
 * existed in InlineMediaPlayer.onDispose (two separate try blocks).
 *
 * The Session class is private and Android-bound (its player field is a real
 * MediaPlayer), so these tests reach it via reflection: a fake player
 * subclass records stop/release calls. Expected counts below are hard-coded
 * literals (never derived from the code under test); each mutation note
 * names the edit that must flip the test red.
 */
class MediaPlayerReleaseGuardTest {

    /**
     * Fake player mirroring the two calls the release path makes.
     * `stopThrows` reproduces the Error-state player: a real MediaPlayer
     * throws IllegalStateException from stop() there, release() still works.
     */
    private class FakePlayer : MediaPlayer() {
        var stopCalls = 0
        var releaseCalls = 0
        var stopThrows = false

        override fun stop() {
            stopCalls++
            if (stopThrows) throw IllegalStateException("player already left playable states")
        }

        override fun release() {
            releaseCalls++
        }
    }

    private val manager: MediaPlayerManager = MediaPlayerManager

    // ── reflection plumbing (Session is a private data class) ─────────────

    private fun sessionClass(): Class<*> =
        Class.forName("com.rikkaminis.app.offload.MediaPlayerManager\$Session")

    private fun newSession(player: MediaPlayer, state: MediaPlayerManager.PlayerState): Any {
        val ctor = sessionClass().getDeclaredConstructor(
            MediaPlayer::class.java,
            String::class.java,
            String::class.java,
            MediaPlayerManager.PlayerState::class.java,
        )
        ctor.isAccessible = true
        return ctor.newInstance(player, "probe.wav", "audio", state)
    }

    private fun invokeReleaseSession(session: Any) {
        val m = MediaPlayerManager::class.java
            .getDeclaredMethod("releaseSession", sessionClass())
        m.isAccessible = true
        m.invoke(manager, session)
    }

    private fun sessionState(session: Any): Any {
        val f = sessionClass().getDeclaredField("state")
        f.isAccessible = true
        return f.get(session)
    }

    // ── tests ─────────────────────────────────────────────────────────────

    /**
     * The judged arm of this fix. Mutation: merging stop() and release()
     * back into one try block must flip this red with releaseCalls == 0.
     */
    @Test
    fun releaseRunsWhenStopThrows() {
        val player = FakePlayer().apply { stopThrows = true }
        val session = newSession(player, MediaPlayerManager.PlayerState.PLAYING)

        invokeReleaseSession(session)

        assertEquals(1, player.stopCalls)
        // Without the two-try split this is 0 — release() was skipped and the
        // native codec leaked. The literal 1 is the whole point of the fix.
        assertEquals(1, player.releaseCalls)
    }

    /**
     * Normal path unchanged: stop then release, state parked at STOPPED.
     * Mutation: dropping the state write or reordering release before the
     * guard flips the state assertion.
     */
    @Test
    fun normalReleaseStopsThenReleasesOnce() {
        val player = FakePlayer()
        val session = newSession(player, MediaPlayerManager.PlayerState.PLAYING)

        invokeReleaseSession(session)

        assertEquals(1, player.stopCalls)
        assertEquals(1, player.releaseCalls)
        assertEquals(MediaPlayerManager.PlayerState.STOPPED, sessionState(session))
    }

    /**
     * Terminalized-then-stopped double release (the post-onCompletion stop()
     * path): stop() must NOT be called for a session already parked in
     * STOPPED, but release() must still run — the entry stays in the map as
     * lightweight metadata until stop()/listSessions prunes it.
     */
    @Test
    fun alreadyStoppedSessionIsStillReleased() {
        val player = FakePlayer()
        val session = newSession(player, MediaPlayerManager.PlayerState.STOPPED)

        invokeReleaseSession(session)

        assertEquals(0, player.stopCalls)
        assertEquals(1, player.releaseCalls)
        assertEquals(MediaPlayerManager.PlayerState.STOPPED, sessionState(session))
    }

    /**
     * Session.state is written by the binder/playback callback threads and
     * read by caller threads; ConcurrentHashMap only orders the map itself.
     * Mutation: removing @Volatile flips this red (isVolatile == false).
     */
    @Test
    fun sessionStateFieldIsVolatile() {
        val f = sessionClass().getDeclaredField("state")
        assertTrue(
            "Session.state must be @Volatile: callback threads write it while " +
                "caller threads read it, and neither goes through a common lock",
            Modifier.isVolatile(f.modifiers),
        )
    }
}

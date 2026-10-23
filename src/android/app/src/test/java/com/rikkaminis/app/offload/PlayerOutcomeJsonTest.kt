package com.rikkaminis.app.offload

import com.rikkaminis.app.sandbox.offload.OffloadArgs
import com.rikkaminis.app.sandbox.offload.OffloadOutput
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv25-A4 A-07] `android-player` answers JSON on EVERY path.
 *
 * The defect this pins: the handler used to hand `OffloadOutput.formatBody` the
 * manager's English sentence (`"Playing 'x.wav' (session=s, …)"`,
 * `"Error: no session 's'"`, `"No active media sessions."`). `formatBody` only
 * re-shapes JSON, so prose passed through, `-q` / `--compact` were no-ops, and a
 * `jq` / `json.loads` reader broke on success while the uncaught-exception
 * branch answered `{"error":"player_failed"}` — one command, two formats.
 * Error-ness was also decided by `result.startsWith("Error")`.
 *
 * Judged here: the [PlayerOutcome] → [playerEnvelope] contract (fields, types,
 * the refusal shape) and the `-q` extraction against the REAL
 * [OffloadOutput.formatBody]. Not judged: the player state machine — that stays
 * in `MediaPlayerManager` and is covered by `MediaPlayerReleaseGuardTest`.
 */
class PlayerOutcomeJsonTest {

    private fun envelope(action: String, outcome: PlayerOutcome): JSONObject =
        playerEnvelope(action, outcome)

    @Test
    fun playSuccessCarriesFieldsInsteadOfASentence() {
        val env = envelope(
            "play",
            PlayerOutcome.ok(
                session = "s1",
                state = "PLAYING",
                file = "Music/y.wav",
                mediaType = "audio",
                durationMs = 12_340,
            ),
        )

        assertTrue(env.getBoolean("ok"))
        assertEquals("android-player", env.getString("tool"))
        assertEquals("play", env.getString("action"))
        val data = env.getJSONObject("data")
        assertEquals("s1", data.getString("session"))
        assertEquals("PLAYING", data.getString("state"))
        assertEquals("Music/y.wav", data.getString("file"))
        assertEquals("audio", data.getString("type"))
        assertEquals(12_340, data.getInt("duration_ms"))
        // play has no position to report: the key must be ABSENT, not 0, which
        // would read as "at the start".
        assertFalse(data.has("position_ms"))
    }

    @Test
    fun refusalIsAnErrorEnvelopeWithAMachineCode() {
        val env = envelope("pause", PlayerOutcome.failure("no_session", "no session 's9'"))

        assertFalse(env.getBoolean("ok"))
        assertEquals("android-player", env.getString("tool"))
        assertEquals("pause", env.getString("action"))
        assertEquals("no_session", env.getString("error"))
        assertEquals("no session 's9'", env.getString("message"))
        assertFalse("a refusal carries no data payload", env.has("data"))
    }

    @Test
    fun failureWithoutACodeFallsBackToThePreExistingCode() {
        val env = envelope("status", PlayerOutcome(ok = false))

        assertFalse(env.getBoolean("ok"))
        // The value the old uncaught-exception branch already emitted, kept so a
        // consumer keying on "player_failed" keeps working.
        assertEquals("player_failed", env.getString("error"))
        assertEquals("", env.getString("message"))
    }

    /**
     * The whole point: `-q` now has something to extract. Before the fix this
     * call returned the sentence unchanged (it is not JSON, so `formatBody`
     * fell through to the pass-through branch).
     */
    @Test
    fun quietExtractsTheDataObjectThroughTheRealFormatter() {
        val env = envelope(
            "status",
            PlayerOutcome.ok(session = "s1", state = "PAUSED", positionMs = 5_000),
        )
        val quiet = OffloadArgs(listOf("-q"))

        val formatted = OffloadOutput.formatBody(env.toString(2), quiet)
        val parsed = JSONObject(formatted)

        assertEquals("s1", parsed.getString("session"))
        assertEquals("PAUSED", parsed.getString("state"))
        assertEquals(5_000, parsed.getInt("position_ms"))
        assertFalse("the envelope itself must be stripped", parsed.has("ok"))
    }

    @Test
    fun quietOnARefusalExtractsTheCode() {
        val env = envelope("seek", PlayerOutcome.failure("cannot_seek", "session 's1' is STOPPED"))

        val formatted = OffloadOutput.formatBody(env.toString(2), OffloadArgs(listOf("-q")))

        assertEquals("cannot_seek", formatted)
    }

    @Test
    fun compactCollapsesTheEnvelopeToALine() {
        val env = envelope("stop", PlayerOutcome.ok(session = "s1", state = "STOPPED"))

        val formatted = OffloadOutput.formatBody(env.toString(2), OffloadArgs(listOf("--compact")))

        assertFalse("a compact body has no newlines", formatted.contains("\n"))
        assertEquals("s1", JSONObject(formatted).getJSONObject("data").getString("session"))
    }

    /**
     * An empty list and a populated one must answer the SAME top-level type —
     * the empty-vs-array flip that broke `jq` for `android-photos near`.
     */
    @Test
    fun emptySessionListKeepsCountAndArray() {
        val env = envelope("list", PlayerOutcome.ok(sessions = emptyList()))

        val data = env.getJSONObject("data")
        assertEquals(0, data.getInt("count"))
        assertTrue(data.get("sessions") is JSONArray)
        assertEquals(0, data.getJSONArray("sessions").length())
    }

    @Test
    fun sessionRowsCarryTheirMetadata() {
        val env = envelope(
            "list",
            PlayerOutcome.ok(
                sessions = listOf(
                    PlayerSessionSnapshot("a", "PLAYING", "a.wav", "audio"),
                    PlayerSessionSnapshot("b", "PAUSED", "b.mp3", "audio"),
                ),
            ),
        )

        val data = env.getJSONObject("data")
        assertEquals(2, data.getInt("count"))
        val rows = data.getJSONArray("sessions")
        assertEquals("a", rows.getJSONObject(0).getString("session"))
        assertEquals("PLAYING", rows.getJSONObject(0).getString("state"))
        assertEquals("b.mp3", rows.getJSONObject(1).getString("file"))
        assertEquals("audio", rows.getJSONObject(1).getString("type"))
    }

    @Test
    fun aLiveSessionThatCannotReportAPositionKeepsOkTrue() {
        val env = envelope(
            "status",
            PlayerOutcome.ok(
                session = "s1",
                state = "STOPPED",
                file = "a.wav",
                mediaType = "audio",
                playerError = "IllegalStateException",
            ),
        )

        assertTrue("a readable session is not a failed command", env.getBoolean("ok"))
        val data = env.getJSONObject("data")
        assertEquals("IllegalStateException", data.getString("player_error"))
        assertFalse("a terminal session has no readable position", data.has("position_ms"))
        assertFalse(data.has("duration_ms"))
    }
}

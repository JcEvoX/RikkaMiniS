package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [deep-audit-1044 A-07] `android-speak`'s TTS manager is a process-wide
 * singleton, so its `speechRate` / `speechPitch` / `speechVolume` survive
 * between invocations. The handler used to apply each flag only when it was
 * present (`args.getDouble("x")?.let { tts.speechX = it }`), which made an
 * absent flag mean "keep whatever the previous call left behind" — one
 * `--volume 0` silenced the rest of the process, and `--rate 0.5` leaked into
 * every later call.
 *
 * [TtsSpeakTuning] resolves the documented per-call defaults (1.0 for all
 * three) once, so the decision is testable without an engine or a Context.
 * Expectations are literals on purpose: deriving them from the production
 * default constant would let a changed default move the expectation with it.
 */
class TtsSpeakDefaultsTest {

    @Test
    fun `absent flags resolve to the documented defaults`() {
        val tuning = TtsSpeakTuning.from(rate = null, pitch = null, volume = null)
        assertEquals(1.0f, tuning.rate, 0.0f)
        assertEquals(1.0f, tuning.pitch, 0.0f)
        assertEquals(1.0f, tuning.volume, 0.0f)
    }

    @Test
    fun `a call after a silent one is loud again`() {
        // The regression: `--volume 0` used to stick for the rest of the process.
        val silent = TtsSpeakTuning.from(rate = null, pitch = null, volume = 0.0)
        assertEquals(0.0f, silent.volume, 0.0f)

        val afterwards = TtsSpeakTuning.from(rate = null, pitch = null, volume = null)
        assertEquals(1.0f, afterwards.volume, 0.0f)
    }

    @Test
    fun `a call after a slow one is back to normal speed`() {
        val slow = TtsSpeakTuning.from(rate = 0.5, pitch = null, volume = null)
        assertEquals(0.5f, slow.rate, 0.0f)
        assertEquals(1.0f, slow.pitch, 0.0f)

        val afterwards = TtsSpeakTuning.from(rate = null, pitch = null, volume = null)
        assertEquals(1.0f, afterwards.rate, 0.0f)
    }

    @Test
    fun `zero volume is a real value, not an absent flag`() {
        // `0.0` must not be treated as falsy / as "no opinion" — it is the only
        // way to ask for silence for a single call.
        val tuning = TtsSpeakTuning.from(rate = 0.0, pitch = null, volume = 0.0)
        assertEquals(0.0f, tuning.rate, 0.0f)
        assertEquals(0.0f, tuning.volume, 0.0f)
    }

    @Test
    fun `each flag is resolved independently`() {
        val tuning = TtsSpeakTuning.from(rate = 1.5, pitch = 0.75, volume = null)
        assertEquals(1.5f, tuning.rate, 0.0f)
        assertEquals(0.75f, tuning.pitch, 0.0f)
        assertEquals(1.0f, tuning.volume, 0.0f)
    }

    @Test
    fun `out-of-range values pass through unclamped`() {
        // The manager's property setters own the coerceIn floors/ceilings
        // (0.1-3.0 / 0.5-2.0 / 0.0-1.0). Duplicating them here would mean two
        // clamps that can disagree; this pins that the resolver stays raw.
        val tuning = TtsSpeakTuning.from(rate = 9.0, pitch = 0.1, volume = 4.0)
        assertEquals(9.0f, tuning.rate, 0.0f)
        assertEquals(0.1f, tuning.pitch, 0.0f)
        assertEquals(4.0f, tuning.volume, 0.0f)
    }
}

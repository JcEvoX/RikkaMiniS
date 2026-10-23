package com.rikkaminis.app.sandbox.offload

import com.rikkaminis.app.data.model.ThinkingLevel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

/**
 * [dv27-A02] Pins the wire contract of `thinking_level` at the ONE place that
 * decodes it.
 *
 * The defect this guards: the dispatcher writes `thinking_level` for every
 * non-OFF turn, but only the streaming path ever read it — `executeRun` fell
 * back to `LLMProvider.sendMessage`'s `ThinkingLevel.OFF` default, so every
 * non-streamed offloaded request switched reasoning OFF on the families where
 * the level decides whether the model reasons at all.
 *
 * Two contracts, both of which the old inline copy in the streaming path got
 * right and which must survive any refactor:
 *  • an ABSENT key (`optString` → "") means OFF — the dispatcher deliberately
 *    omits the key when the level is OFF, so a thinking-off turn must reach the
 *    provider, not die as `unknown ThinkingLevel value: `;
 *  • a present-but-unknown value still throws, so a newer main-process build's
 *    enum case respawns a worker that knows it instead of silently degrading.
 */
class ThinkingLevelWireTest {

    @Test
    fun absentKeyDecodesAsOff() {
        assertEquals(ThinkingLevel.OFF, thinkingLevelFromWire(""))
    }

    @Test
    fun everyKnownLevelRoundTrips() {
        for (level in ThinkingLevel.entries) {
            assertEquals(level, thinkingLevelFromWire(level.name))
        }
    }

    @Test
    fun appendedAutoCaseRoundTrips() {
        assertEquals(ThinkingLevel.AUTO, thinkingLevelFromWire("AUTO"))
    }

    @Test
    fun unknownValueThrowsInsteadOfSilentlyDegrading() {
        val ex = assertThrows(UnknownEnumValueException::class.java) {
            thinkingLevelFromWire("NOPE_FROM_A_NEWER_BUILD")
        }
        assertEquals("ThinkingLevel", ex.enumClass)
        assertEquals("NOPE_FROM_A_NEWER_BUILD", ex.unknownValue)
    }

    @Test
    fun lowercaseIsNotSilentlyAccepted() {
        // Enum names are exact: "high" is an unknown value, not a lenient match.
        assertThrows(UnknownEnumValueException::class.java) {
            thinkingLevelFromWire("high")
        }
    }
}

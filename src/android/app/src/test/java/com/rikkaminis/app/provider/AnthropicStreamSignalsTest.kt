package com.rikkaminis.app.provider

import com.rikkaminis.app.provider.anthropic.toolInputDeltaActivityChars
import com.rikkaminis.app.provider.anthropic.toolUseStartActivityChars
import com.rikkaminis.app.provider.anthropic.toolUseStopActivityChars
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv29 A-01] Decision table for Anthropic's three tool-lifecycle activity
 * weights — the functions that feed `AnthropicProvider`'s `toolChars`, which
 * the SSE EOF guard compares against zero.
 *
 * The guard itself lives inside the SSE loop and cannot run without the
 * Android runtime, so these are the only JVM-level evidence for the mapping.
 * Every expectation is a hand-written literal (never derived from the constant
 * under test): a mutated floor would otherwise drag the expectation along and
 * masquerade as a pass.
 *
 * The three cut points the guard must survive:
 *  1. `content_block_start(tool_use)` then EOF, before any argument delta —
 *     the block announcement alone must already be positive.
 *  2. zero-argument tool completed (`content_block_stop`) then EOF, before
 *     `message_delta` — the completion alone must be positive.
 *  3. mid-`input_json_delta` EOF (the pre-existing, already working case) —
 *     must not regress.
 */
class AnthropicStreamSignalsTest {

    // ---------------------------------------------------------------
    // 1. block announcement — first proof the turn produced a tool call
    // ---------------------------------------------------------------

    @Test
    fun `announcement of a named call weighs the name`() {
        assertEquals(13, toolUseStartActivityChars("toolu_01", "shell_execute"))
    }

    @Test
    fun `announcement of an argument-less, unnamed call still weighs one`() {
        // The floor is the whole point: a zero-argument tool carries no payload,
        // yet "announced then cut" must still read as tool-call progress.
        assertEquals(1, toolUseStartActivityChars("", ""))
    }

    @Test
    fun `announcement with an id longer than the name weighs the id`() {
        assertEquals(10, toolUseStartActivityChars("toolu_abcd", "ls"))
    }

    // ---------------------------------------------------------------
    // 2. argument deltas — zero for keep-alive empties, length otherwise
    // ---------------------------------------------------------------

    @Test
    fun `empty argument delta carries no progress`() {
        assertEquals(0, toolInputDeltaActivityChars(""))
    }

    @Test
    fun `argument delta weighs its JSON text length`() {
        assertEquals(7, toolInputDeltaActivityChars("{\"a\":1}"))
    }

    @Test
    fun `argument delta of a truncated object still weighs its prefix`() {
        assertEquals(6, toolInputDeltaActivityChars("{\"path"))
    }

    // ---------------------------------------------------------------
    // 3. block completion — defence in depth for delta-less zero-arg calls
    // ---------------------------------------------------------------

    @Test
    fun `completion of a named call weighs the name`() {
        assertEquals(13, toolUseStopActivityChars("toolu_01", "shell_execute"))
    }

    @Test
    fun `completion of an argument-less, unnamed call still weighs one`() {
        assertEquals(1, toolUseStopActivityChars("", ""))
    }

    // ---------------------------------------------------------------
    // 4. the three cut points, accumulated the way the SSE loop does it
    // ---------------------------------------------------------------

    @Test
    fun `cut right after the announcement leaves the counter positive`() {
        // start event, then EOF: no delta, no stop, no message_delta.
        var toolChars = 0
        toolChars += toolUseStartActivityChars("toolu_1", "read_file")
        assertEquals(9, toolChars)
        assertTrue(toolChars > 0)
    }

    @Test
    fun `cut after a completed zero-argument call leaves the counter positive`() {
        var toolChars = 0
        toolChars += toolUseStartActivityChars("", "")
        toolChars += toolUseStopActivityChars("", "")
        assertEquals(2, toolChars)
        assertTrue(toolChars > 0)
    }

    @Test
    fun `cut mid argument stream keeps the pre-existing count`() {
        var toolChars = 0
        toolChars += toolUseStartActivityChars("toolu_7", "shell_execute")
        toolChars += toolInputDeltaActivityChars("")
        toolChars += toolInputDeltaActivityChars("{\"a\":1}")
        assertEquals(20, toolChars)
    }

    @Test
    fun `a turn with no tool event at all stays at zero`() {
        // Fully empty EOF belongs to failOnSilentEmptyCompletion: the guard must
        // not claim it, so nothing here may contribute a positive weight.
        var toolChars = 0
        toolChars += toolInputDeltaActivityChars("")
        assertEquals(0, toolChars)
    }
}

package com.rikkaminis.app.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1015 BUG-09] The host-side truncation pass must not cut a SECOND
 * time what the shell side already clipped.
 *
 * Order of operations on the production path (ExecutionCoordinator):
 *  1. PersistentShell clips the capture to `shellOutputKb() * 1024` BYTES
 *     (silently — the clip sets `truncated = true`, it adds no marker);
 *  2. executeWithShellRetry appends its diagnostics to that clipped text
 *     ([internalMemoryRecycleNotice], later the "(exit code: N)" trailer);
 *  3. execute() sanitizes, then truncates at … the SAME ceiling.
 *
 * Step 3 therefore re-cut a capture that was already at the ceiling, so any
 * command landing within a suffix-length of the knob lost another slice of real
 * output and grew a second "[... N bytes omitted ...]" marker.
 *
 * Expectations are literals (64 KiB ceiling, 1024 margin) — the shell-side
 * ceiling is an `AgentRuntimeLimitsPrefs` value the JVM cannot read, and a
 * margin expectation derived from the constant itself would stay green through
 * a mutation of that constant.
 */
class ShellOutputTruncationMarginTest {

    private val limitBytes = 64 * 1024

    @Test
    fun `the margin constant is pinned and still covers the longest suffix`() {
        assertEquals(1024, SHELL_OUTPUT_TRUNCATION_SUFFIX_MARGIN_BYTES)

        // Contract guard: whoever extends the diagnostics text past the margin
        // turns this red instead of silently re-introducing the second cut.
        val longestNotice = internalMemoryRecycleNotice(
            "the PRoot command's RSS 512MB crossed the 256MB ceiling",
        )
        val trailer = "(exit code: -1)"
        assertTrue(
            "margin must cover the longest suffix this path appends " +
                "(notice=${longestNotice.length} + trailer=${trailer.length} bytes)",
            longestNotice.length + trailer.length <= SHELL_OUTPUT_TRUNCATION_SUFFIX_MARGIN_BYTES,
        )
    }

    @Test
    fun `shell-clipped output plus diagnostics survives the host pass untouched`() {
        val shellClipped = "a".repeat(limitBytes)
        val notice = internalMemoryRecycleNotice(
            "the app's native heap 210MB crossed the 200MB ceiling",
        )
        val payload = shellClipped + notice

        val afterHostPass = TerminalSanitizer.truncateIfNeeded(
            payload,
            limitBytes + SHELL_OUTPUT_TRUNCATION_SUFFIX_MARGIN_BYTES,
        )

        assertEquals("the host pass must not touch it", payload, afterHostPass)
        assertEquals("no second omission marker", 0, occurrences(afterHostPass, "[..."))
        assertTrue("the diagnostics stay at the tail", afterHostPass.endsWith(notice))
    }

    @Test
    fun `the old threshold is what produced the second cut`() {
        val shellClipped = "a".repeat(limitBytes)
        val notice = internalMemoryRecycleNotice(
            "the app's native heap 210MB crossed the 200MB ceiling",
        )
        val payload = shellClipped + notice

        val oldThresholdResult = TerminalSanitizer.truncateIfNeeded(payload, limitBytes)

        assertEquals(
            "the same-ceiling threshold cut again",
            1,
            occurrences(oldThresholdResult, "[..."),
        )
        assertTrue(
            "…and it dropped a slice of real output that was already within budget",
            oldThresholdResult.length < payload.length,
        )
    }

    @Test
    fun `a comfortable margin is not a licence to keep everything`() {
        // The margin is a suffix allowance, NOT a raised ceiling: an output far
        // past the knob is still truncated, exactly once.
        val oversized = "b".repeat(limitBytes + 64 * 1024)
        val result = TerminalSanitizer.truncateIfNeeded(
            oversized,
            limitBytes + SHELL_OUTPUT_TRUNCATION_SUFFIX_MARGIN_BYTES,
        )
        assertFalse("still capped", result.length > oversized.length)
        assertEquals(1, occurrences(result, "[..."))
    }

    private fun occurrences(haystack: String, needle: String): Int {
        var count = 0
        var idx = haystack.indexOf(needle)
        while (idx >= 0) {
            count++
            idx = haystack.indexOf(needle, idx + needle.length)
        }
        return count
    }
}

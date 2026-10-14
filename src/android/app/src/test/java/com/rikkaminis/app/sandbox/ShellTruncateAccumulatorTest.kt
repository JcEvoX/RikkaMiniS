package com.rikkaminis.app.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.charset.StandardCharsets

/**
 * [audit-1015 PERF-01] The truncation path used to recompute
 * [TerminalSanitizer.utf8Length] over the WHOLE accumulated output on every
 * 4 KB read chunk (O(N²) over a run's total output). Now the caller maintains
 * an incremental byte accumulator and each chunk measures only the incoming
 * text.
 *
 * These tests A/B the incremental accumulator against BOTH full recompute and
 * the JDK's own encoder (`String.toByteArray(UTF_8).size` — independent ground
 * truth), on multi-chunk sequences including 3-byte CJK and 4-byte astral
 * emoji. Expected values are literals.
 */
class ShellTruncateAccumulatorTest {

    /** Drives the PRODUCTION accumulator ([IncrementalUtf8Budget]). */
    private class AccumulatingBuffer(val maxBytes: Int) {
        val sb = StringBuilder()
        val budget = IncrementalUtf8Budget { maxBytes }

        fun append(text: String) {
            budget.appendTo(sb, text)
        }

        val acc: Int get() = budget.byteLength
        val truncated: Boolean get() = budget.truncated

        /** A/B: the accumulator must equal the FULL recompute at every step. */
        fun invariantHolds(): Boolean =
            acc == TerminalSanitizer.utf8Length(sb) &&
                acc == sb.toString().toByteArray(StandardCharsets.UTF_8).size
    }

    // ── equivalence: incremental == full recompute == JDK encoder ────────

    @Test
    fun `incremental accumulator equals full recompute across chunks`() {
        val chunks = listOf(
            "hello world\n",                    // ASCII
            "你好世界，这是终端输出。\n",          // 3-byte CJK
            "🔥🔥 emoji astral plane 🔥\n",      // 4-byte surrogate pairs
            "mixed ASCII + 中文 + 🎉 again\n",
            "tail",
        )
        val buf = AccumulatingBuffer(maxBytes = 4096)
        for (c in chunks) {
            buf.append(c)
            assertTrue(
                "accumulator drifted after chunk: $c",
                buf.invariantHolds(),
            )
        }
        val full = chunks.joinToString("")
        assertEquals(full, buf.sb.toString())
        assertEquals(TerminalSanitizer.utf8Length(full), buf.acc)
    }

    @Test
    fun `incremental equals full recompute under truncation`() {
        // Cap below the total: the accumulator must stay exact through the
        // clip and through subsequent skipped chunks.
        val buf = AccumulatingBuffer(maxBytes = 20)
        buf.append("abcdefghijklmn")             // 14 ASCII bytes
        assertTrue(buf.invariantHolds())
        buf.append("你好世界")                    // 12 bytes → clips at 20
        assertTrue("truncation expected", buf.truncated)
        assertTrue(buf.invariantHolds())
        buf.append("more overflow")              // remaining <= 0 → skipped
        assertTrue(buf.invariantHolds())
        // Ground truth: the buffer holds exactly 20 UTF-8 bytes.
        assertEquals(20, buf.sb.toString().toByteArray(StandardCharsets.UTF_8).size)
        assertEquals(20, buf.acc)
    }

    @Test
    fun `clip never ends on a lone surrogate`() {
        // 🔥 is 4 UTF-8 bytes / 2 UTF-16 chars. Cap at 7: the prefix "a🔥b"
        // is 6 bytes — the next 🔥 (4 bytes) does not fit in the remaining 1,
        // and byteSafePrefixLength never splits a code point, so the clip
        // lands at 6 bytes (NOT exactly 7).
        val buf = AccumulatingBuffer(maxBytes = 7)
        buf.append("a🔥b🔥c")                     // 1+4+1+4+1 = 11 bytes
        assertTrue(buf.truncated)
        assertTrue(buf.invariantHolds())
        val text = buf.sb.toString()
        assertEquals(6, text.toByteArray(StandardCharsets.UTF_8).size)
        assertEquals("a🔥b", text)
        // Decoding the buffer round-trips — a lone surrogate would be
        // replaced by U+FFFD on re-encode, so byte equality proves none.
        assertEquals(text, String(text.toByteArray(StandardCharsets.UTF_8), StandardCharsets.UTF_8))
    }

    @Test
    fun `legacy three-arg form stays consistent with the accumulator path`() {
        // The 3-arg legacy form recomputes internally; results must agree
        // with the detailed form driven by an exact accumulator.
        val sb1 = StringBuilder()
        val legacyTruncated = internalTruncateOutput(sb1, "你好世界", 7)
        val sb2 = StringBuilder()
        val detailed = internalTruncateOutputDetailed(sb2, "你好世界", 7, 0)
        assertEquals(legacyTruncated, detailed.truncated)
        assertEquals(sb1.toString(), sb2.toString())
        assertTrue(legacyTruncated)
    }

    @Test
    fun `empty append and over-cap append keep the accumulator exact`() {
        val buf = AccumulatingBuffer(maxBytes = 5)
        buf.append("")
        assertTrue(buf.invariantHolds())
        assertFalse(buf.truncated)
        buf.append("12345")                      // fits exactly
        assertTrue(buf.invariantHolds())
        assertFalse(buf.truncated)
        buf.append("6")                          // 6 > 5 → skipped
        assertTrue(buf.truncated)
        assertTrue(buf.invariantHolds())
        assertEquals("12345", buf.sb.toString())
    }

    // ── utf8Length(CharSequence) signature change ────────────────────────

    @Test
    fun `utf8Length accepts CharSequence without materialising a copy`() {
        val sb = StringBuilder("a🔥b你好")
        // Literal ground truth: 1 + 4 + 1 + 3 + 3 = 12 UTF-8 bytes.
        assertEquals(12, TerminalSanitizer.utf8Length(sb as CharSequence))
        assertEquals(12, TerminalSanitizer.utf8Length(sb.toString()))
        assertEquals(12, sb.toString().toByteArray(StandardCharsets.UTF_8).size)
    }
}

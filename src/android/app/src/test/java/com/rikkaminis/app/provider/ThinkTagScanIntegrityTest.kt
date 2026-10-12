package com.rikkaminis.app.provider

import com.rikkaminis.app.provider.openai.THINK_TAG_FORMATS
import com.rikkaminis.app.provider.openai.ThinkTagDef
import com.rikkaminis.app.provider.openai.scanThinkTags
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Regression tests for think-tag scanner integrity:
 *
 *  - [unicode-index-shift] (fixed 2026-10-12): searching a `buffer.lowercase()`
 *    copy and slicing the SOURCE buffer with those indices corrupts text after
 *    any char whose lowercase() expands (canonical: U+0130 'İ' -> 2 code
 *    points). All 5 chunkings of the same logical stream must converge.
 *
 *  - [state-carry] (fixed 2026-10-12): the loop can exit with an ACTIVE region
 *    when the open tag is consumed exactly at the buffer end (relay sends
 *    "<thinking>" as its own delta). The result must carry that state so the
 *    next chunk continues the thinking region.
 *
 * Chunk-invariance is the core property: how a logical stream is split into
 * scanThinkTags calls must never change the final (visible, thinking) pair.
 */
class ThinkTagScanIntegrityTest {

    /** Streaming accumulator: feeds chunks through scanThinkTags and tracks region state. */
    private class ScanAccumulator {
        var buffer = ""
        var inside = false
        var fmt: ThinkTagDef? = null
        val visible = StringBuilder()
        val thinking = StringBuilder()

        fun append(text: String) {
            val r = scanThinkTags(buffer + text, inside, fmt, THINK_TAG_FORMATS)
            buffer = r.remainingBuffer
            inside = r.insideTag
            fmt = r.currentFormat
            visible.append(r.visible)
            thinking.append(r.thinking)
        }

        fun flush(): Pair<String, String> {
            if (inside) thinking.append(buffer) else visible.append(buffer)
            return visible.toString() to thinking.toString()
        }
    }

    private fun stream(chunks: List<String>): Pair<String, String> {
        val acc = ScanAccumulator()
        chunks.forEach { acc.append(it) }
        return acc.flush()
    }

    // ── [unicode-index-shift] chunk invariance with İ in the stream ──

    @Test
    fun `chunk invariance - İ stream 5 chunkings all converge to İdone + abc`() {
        val corpus = "İ<thinking>abc</thinking>done"
        val chunkings = mapOf(
            "whole" to listOf(corpus),
            "split-İ-first" to listOf("İ", "<thinking>abc</thinking>done"),
            "split-inside-tag" to listOf("İ<th", "inking>abc</thinking>done"),
            "split-before-tail" to listOf("İ<thinking>abc</thinking>", "done"),
            "per-char" to corpus.map { it.toString() },
        )
        for ((name, chunks) in chunkings) {
            val (v, t) = stream(chunks)
            assertEquals("visible mismatch for chunking $name", "İdone", v)
            assertEquals("thinking mismatch for chunking $name", "abc", t)
        }
    }

    @Test
    fun `chunk invariance - pure ASCII stream 4 chunkings all converge to done + abc`() {
        val corpus = "<thinking>abc</thinking>done"
        val chunkings = mapOf(
            "whole" to listOf(corpus),
            "tag-own-chunk" to listOf("<thinking>", "abc</thinking>done"),
            "tag-completes-at-end" to listOf("<thin", "king>", "abc</thinking>done"),
            "per-char" to corpus.map { it.toString() },
        )
        for ((name, chunks) in chunkings) {
            val (v, t) = stream(chunks)
            assertEquals("visible mismatch for chunking $name", "done", v)
            assertEquals("thinking mismatch for chunking $name", "abc", t)
        }
    }

    // ── exact values: İ shapes (fixed by [unicode-index-shift]) ──

    @Test
    fun `İ before complete pair - thinking extracted visible keeps İ`() {
        val (v, t) = stream(listOf("İ<thinking>abc</thinking>done"))
        assertEquals("İdone", v)
        assertEquals("abc", t)
    }

    @Test
    fun `İ before orphan close - closer dropped body intact`() {
        val (v, t) = stream(listOf("İ</thinking>x"))
        assertEquals("İx", v)
        assertEquals("", t)
    }

    @Test
    fun `İ inside thinking region - stays in thinking`() {
        val (v, t) = stream(listOf("<thinking>İ</thinking>ok"))
        assertEquals("ok", v)
        assertEquals("İ", t)
    }

    @Test
    fun `Turkish text with inline region - body and thinking both intact`() {
        val (v, t) = stream(listOf("İstanbul'a gidiyoruz<think>plan</think>"))
        assertEquals("İstanbul'a gidiyoruz", v)
        assertEquals("plan", t)
    }
    @Test
    fun `İ after leading text - no eating around İ`() {
        val (v, t) = stream(listOf("done İ <thinking>abc</thinking>end"))
        assertEquals("done İ end", v)
        assertEquals("abc", t)
    }

    @Test
    fun `İ split across chunks - chunk invariance holds`() {
        val (v, t) = stream(listOf("İ<thi", "nk>plan</think>done"))
        assertEquals("İdone", v)
        assertEquals("plan", t)
    }

    // ── exact values: cross-chunk boundary shapes (fixed by [state-carry]) ──

    @Test
    fun `boundary A - open tag alone in chunk keeps region active`() {
        val (v, t) = stream(listOf("A <thin", "king>", "abc</thinking>done"))
        assertEquals("A done", v)
        assertEquals("abc", t)
    }

    @Test
    fun `boundary B - realistic relay with leading empty chunk`() {
        val (v, t) = stream(listOf("", "<think>", "Let me think about this.</think>The answer is 42."))
        assertEquals("The answer is 42.", v)
        assertEquals("Let me think about this.", t)
    }

    @Test
    fun `boundary D - open tag at exact end of split stream`() {
        val (v, t) = stream(listOf("x", "<think>", "y</think>z"))
        assertEquals("xz", v)
        assertEquals("y", t)
    }

    // ── no phantom swallowing of normal prose ──

    @Test
    fun `Turkish prose without tags passes through untouched`() {
        val (v, t) = stream(listOf("İstanbul'a gidiyoruz, bugün hava çok güzel"))
        assertEquals("İstanbul'a gidiyoruz, bugün hava çok güzel", v)
        assertEquals("", t)
    }

    @Test
    fun `İstanbul word before tag region - region still extracted correctly`() {
        val (v, t) = stream(listOf("İstanbul <thinking>plan</thinking> done"))
        assertEquals("İstanbul  done", v)
        assertEquals("plan", t)
    }

    @Test
    fun `non-tag element like thinker passes through`() {
        val (v, t) = stream(listOf("A <thinker> is text"))
        assertEquals("A <thinker> is text", v)
        assertEquals("", t)
    }

    @Test
    fun `plain text without any tag passes through`() {
        val (v, t) = stream(listOf("Just regular text with no tags"))
        assertEquals("Just regular text with no tags", v)
        assertEquals("", t)
    }

    // ── scanner state returned directly (state-carry contract) ──

    @Test
    fun `single scan with open tag at buffer end reports insideTag true with format`() {
        val r = scanThinkTags("<thinking>", false, null, THINK_TAG_FORMATS)
        assertTrue("insideTag must be carried out", r.insideTag)
        assertEquals(THINK_TAG_FORMATS[0], r.currentFormat)
        assertEquals("", r.visible)
    }

    @Test
    fun `single scan without any tag reports insideTag false`() {
        val r = scanThinkTags("plain text", false, null, THINK_TAG_FORMATS)
        assertEquals(false, r.insideTag)
        assertEquals(null, r.currentFormat)
    }
}

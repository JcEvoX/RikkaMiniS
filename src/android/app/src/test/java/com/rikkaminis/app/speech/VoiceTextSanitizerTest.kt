package com.rikkaminis.app.speech

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [T-android-voice-text-sanitizer] Pure-JVM tests for the read-aloud text
 * cleanup. Uses the default English link phrases so no Context is needed.
 */
class VoiceTextSanitizerTest {

    private fun clean(s: String) = VoiceTextSanitizer.sanitize(s)

    // -- Emphasis --

    @Test
    fun `strips bold italic and strikethrough but keeps the words`() {
        assertEquals("important", clean("**important**"))
        assertEquals("important", clean("__important__"))
        assertEquals("important", clean("*important*"))
        assertEquals("important", clean("_important_"))
        assertEquals("gone", clean("~~gone~~"))
    }

    @Test
    fun `snake_case survives underscore emphasis stripping`() {
        // The (?<!\w)_..._(?!\w) guards exist precisely for this.
        assertEquals("call read_image_file now", clean("call read_image_file now"))
    }

    // -- Code --

    @Test
    fun `fenced code blocks are dropped entirely`() {
        val out = clean("Before\n```kotlin\nval x = 1\n```\nAfter")
        assertFalse("code leaked: $out", out.contains("val x"))
        assertTrue(out.contains("Before"))
        assertTrue(out.contains("After"))
    }

    @Test
    fun `inline code keeps its inner text`() {
        assertEquals("run ls now", clean("run `ls` now"))
    }

    // -- Links --

    @Test
    fun `markdown link keeps its description`() {
        assertEquals("the docs", clean("[the docs](https://example.com/a/b)"))
    }

    @Test
    fun `markdown link without description reads as a spoken phrase`() {
        assertEquals("link to example.com", clean("[](https://example.com/a/b)"))
    }

    @Test
    fun `bare url is replaced by a spoken phrase not read out verbatim`() {
        val out = clean("See https://www.example.com/very/long/path?x=1 for details")
        assertEquals("See link to example.com for details", out)
        assertFalse(out.contains("very/long"))
    }

    @Test
    fun `image markdown does not leak the url`() {
        val out = clean("![](https://example.com/chart.png)")
        assertFalse("url leaked: $out", out.contains("http"))
    }

    // -- Block markers --

    @Test
    fun `headings blockquotes and bullets lose their markers`() {
        assertEquals("Title", clean("## Title"))
        assertEquals("quoted", clean("> quoted"))
        assertEquals("item", clean("- item"))
        assertEquals("item", clean("1. item"))
    }

    @Test
    fun `table pipes and separator rows do not become noise`() {
        val out = clean("| a | b |\n| --- | --- |\n| 1 | 2 |")
        assertFalse("pipe leaked: $out", out.contains("|"))
        assertFalse("separator leaked: $out", out.contains("---"))
        assertTrue(out.contains("a"))
    }

    @Test
    fun `horizontal rule is removed`() {
        val out = clean("above\n---\nbelow")
        assertTrue(out.contains("above"))
        assertTrue(out.contains("below"))
        assertFalse(out.contains("---"))
    }

    // -- Non-speakable glyphs --

    @Test
    fun `emoji are removed`() {
        assertEquals("done", clean("done 🔥"))
        assertEquals("ok", clean("✅ ok"))
    }

    @Test
    fun `emoji between words does not fuse the words together`() {
        // Replaced with a space, not deleted.
        assertEquals("alpha beta", clean("alpha🔥beta"))
    }

    @Test
    fun `cjk and normal punctuation are kept`() {
        assertEquals("你好,世界。", clean("你好,世界。"))
    }

    // -- Whitespace / empties --

    @Test
    fun `whitespace left by removals is collapsed`() {
        assertEquals("a b", clean("a    b"))
    }

    @Test
    fun `a message that is only a code block sanitizes to empty`() {
        // Callers must treat empty as "nothing to speak".
        assertEquals("", clean("```\nval x = 1\n```"))
    }

    @Test
    fun `empty and blank input stay empty`() {
        assertEquals("", clean(""))
        assertEquals("", clean("   \n  "))
    }

    // -- host parsing --

    @Test
    fun `hostOf strips scheme www userinfo and port`() {
        assertEquals("example.com", VoiceTextSanitizer.hostOf("https://www.example.com/a"))
        assertEquals("example.com", VoiceTextSanitizer.hostOf("http://example.com:8080/a"))
        assertEquals("example.com", VoiceTextSanitizer.hostOf("https://user@example.com/a"))
        assertEquals("example.com", VoiceTextSanitizer.hostOf("https://example.com"))
    }

    // -- realistic composite --

    @Test
    fun `a realistic assistant reply is fully cleaned`() {
        val reply = """
            ## Summary ✅

            The **key** point is in `config.kt`. See [the guide](https://docs.example.com/x).

            - first item
            - second item

            ```kotlin
            fun main() {}
            ```
        """.trimIndent()
        val out = clean(reply)
        assertFalse("markdown leaked: $out", out.contains("**"))
        assertFalse("heading leaked: $out", out.contains("##"))
        assertFalse("code leaked: $out", out.contains("fun main"))
        assertFalse("emoji leaked: $out", out.contains("✅"))
        assertFalse("url leaked: $out", out.contains("http"))
        assertTrue(out.contains("Summary"))
        assertTrue(out.contains("key"))
        assertTrue(out.contains("the guide"))
        assertTrue(out.contains("first item"))
    }

    // -- localized phrases (the R.string path TtsSpokenPhrases wires) --

    @Test
    fun `localized link phrases are honoured for bare urls`() {
        // [fix/da1016-tts-voice] The phrases come from strings.xml via
        // TtsSpokenPhrases; this pins that a non-English set is honoured,
        // including the bare-link branch (URL with no parseable host).
        val zh = VoiceTextSanitizer.LinkPhrases(linkToHost = "%1\$s 的链接", bareLink = "链接")
        assertEquals(
            "见 example.com 的链接",
            VoiceTextSanitizer.sanitize("见 https://www.example.com/a", zh),
        )
        assertEquals("见 链接", VoiceTextSanitizer.sanitize("见 https:///path", zh))
    }

    @Test
    fun `localized link phrases are honoured for descriptionless markdown links`() {
        val zh = VoiceTextSanitizer.LinkPhrases(linkToHost = "%1\$s 的链接", bareLink = "链接")
        assertEquals(
            "看 example.com 的链接",
            VoiceTextSanitizer.sanitize("看 [](https://www.example.com/a)", zh),
        )
    }

    // -- exact output for multi-line tables (was only contains-asserted) --

    @Test
    fun `cross line table keeps its cells in reading order`() {
        assertEquals("a b\n\n1 2", clean("| a | b |\n| --- | --- |\n| 1 | 2 |"))
    }

    @Test
    fun `cross line table with cjk cells keeps the han`() {
        assertEquals(
            "名称 数量\n\n苹果 3",
            clean("| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 3 |"),
        )
    }

    // -- cjk text keeps the han through markdown cleanup --

    @Test
    fun `cjk survives emphasis heading and emoji cleanup`() {
        // The sanitized text is what language auto-detection sees now, so a
        // Han-bearing message must not come out Han-free.
        assertEquals("重要", clean("**重要**"))
        assertEquals("标题", clean("## 标题"))
        assertEquals("重要", clean("重要🔥"))
        assertTrue(
            "han must survive so auto-detection still picks zh-CN",
            VoiceTextSanitizer.sanitize("见 example.com 的链接", VoiceTextSanitizer.LinkPhrases("%1\$s 的链接", "链接"))
                .contains("的链接"),
        )
    }
}

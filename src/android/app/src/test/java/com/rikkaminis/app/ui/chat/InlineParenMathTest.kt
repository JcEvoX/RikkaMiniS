package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [crash-1016] `\\(` … `\\)` inline-math extraction.
 *
 * [inlineParenMathSpan] is the single decision shared by the two inline
 * scanners and [collectInlineMathLatex]. An EMPTY payload must resolve to null
 * (literal text) instead of an empty latex string: the payload becomes the
 * accessibility alternate text of `appendInlineContent`, and androidx requires
 * it non-empty — an adjacent `\\(` `\\)` crashed the whole app on 2026-10-16
 * (`IllegalArgumentException: alternateText can't be an empty string`).
 * [collectInlineMathLatex] must mirror that decision, otherwise it would
 * pre-register a KaTeX slot for a span the inline pass renders literally.
 */
class InlineParenMathTest {

    @Test fun `payload resolves with the index just past the closer`() {
        val text = "a \\(x + y\\) b"
        val span = inlineParenMathSpan(text, text.indexOf("\\("))
        assertEquals("x + y", span?.latex)
        assertEquals(text.indexOf("\\)") + 2, span?.next)
        assertEquals(" b", text.substring(span!!.next))
    }

    @Test fun `empty payload resolves to null`() {
        val text = "before \\(\\) after"
        assertNull(inlineParenMathSpan(text, text.indexOf("\\(")))
    }

    @Test fun `unclosed span resolves to null`() {
        val text = "before \\(x + y after"
        assertNull(inlineParenMathSpan(text, text.indexOf("\\(")))
    }

    @Test fun `index that is not an opener resolves to null`() {
        assertNull(inlineParenMathSpan("plain prose", 0))
        // `\\[` is display math, handled at block level — not an inline opener.
        assertNull(inlineParenMathSpan("\\[x\\]", 0))
    }

    @Test fun `collect skips an empty payload instead of registering a blank slot`() {
        assertEquals(emptyList<String>(), collectInlineMathLatex("a \\(\\) b"))
        assertFalse(collectInlineMathLatex("a \\(\\) b").any { it.isEmpty() })
    }

    @Test fun `collect keeps real spans around an empty one`() {
        assertEquals(listOf("x+y"), collectInlineMathLatex("\\(\\) then \\(x+y\\)"))
        assertEquals(listOf("x+y"), collectInlineMathLatex("\\(x+y\\) then \\(\\)"))
    }

    @Test fun `collect never yields an empty payload for the crashing shape`() {
        // The exact shape that crashed: `f\(\)` inside a python regex body.
        val text = "m = re.search(r\"fun f\\(\\): String\", text)"
        assertFalse(collectInlineMathLatex(text).any { it.isEmpty() })
    }
}

package com.rikkaminis.app.ui.chat

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [crash-1016] Regression coverage for indented fenced code blocks under list
 * items.
 *
 * Before the fix the list loops in [parseMarkdownBlocks] swallowed ANY indented
 * line into the previous item's text, so a fence and its body leaked into
 * [MdBlock.UnorderedList] item text. Item text is rendered through
 * `MarkdownParseCaches.inline` (StreamingMarkdownText), and a python regex
 * containing an adjacent `\(` `\)` inside such a body reached
 * `appendInlineContent(tag, "")` — androidx requires a non-empty alternate text
 * (`InlineTextContent.kt`), so the app crashed while opening a markdown report
 * (2026-10-16, AUDIT-REPORT.md:116). The twin parser
 * `ui/markdown/MarkdownParser.kt` had the same hole in its bullet-continuation
 * loop and is pinned here through the chat parser only, as the two are kept in
 * sync by hand.
 *
 * Runs under a timeout like [MarkdownBlockModelNumberedListTest]: the parser
 * checks `coroutineContext.ensureActive()` periodically, so a predicate drift
 * fails fast instead of hanging CI.
 */
class MarkdownBlockModelListFenceTest {

    private fun parse(text: String): List<MdBlock> = runBlocking {
        withTimeout(5_000) {
            withContext(Dispatchers.Default) { parseMarkdownBlocks(text) }
        }
    }

    private fun List<MdBlock>.itemTexts(): List<String> =
        filterIsInstance<MdBlock.UnorderedList>().flatMap { it.items.map { item -> item.text } } +
            filterIsInstance<MdBlock.OrderedList>().flatMap { it.items.map { item -> item.text } }

    @Test fun `indented fence under a bullet becomes a code block`() {
        val blocks = parse("- **标题**：说明\n  ```python\n  x = 1\n  ```\n- 下一条")
        val code = blocks.filterIsInstance<MdBlock.CodeBlock>()
        assertEquals("got ${blocks.map { it::class.simpleName }}", 1, code.size)
        assertEquals("python", code[0].language)
        assertEquals("  x = 1", code[0].code)
        assertEquals(listOf("**标题**：说明", "下一条"), blocks.itemTexts())
        assertFalse(blocks.itemTexts().any { it.contains("```") })
    }

    @Test fun `crash-1016 - empty inline math inside an indented fence never reaches item text`() {
        // Mirror of AUDIT-REPORT.md:112-118 (the file that crashed the app): a
        // python regex with an adjacent `\(` `\)` inside a fence introduced by a
        // bullet. The empty payload must stay inside the code block — reaching
        // inline parsing is what made appendInlineContent throw.
        val text = "- **单测与验证命令**：\n" +
            "  ```python\n" +
            "  m = re.search(r\"fun f\\(\\): String\", text, re.S)\n" +
            "  ```\n" +
            "- 下一条"
        val blocks = parse(text)
        val code = blocks.filterIsInstance<MdBlock.CodeBlock>()
        assertEquals(1, code.size)
        assertTrue("payload lost: ${code[0].code}", code[0].code.contains("f\\(\\)"))
        assertFalse(
            "empty math reached inline text: ${blocks.itemTexts()}",
            blocks.itemTexts().any { it.contains("\\(") },
        )
        assertEquals(listOf("**单测与验证命令**：", "下一条"), blocks.itemTexts())
    }

    @Test fun `indented fence under an ordered list becomes a code block`() {
        val blocks = parse("1. 第一步\n   ```bash\n   make test\n   ```\n2. 第二步")
        val code = blocks.filterIsInstance<MdBlock.CodeBlock>()
        assertEquals(1, code.size)
        assertEquals("bash", code[0].language)
        assertEquals("   make test", code[0].code)
        val list = blocks.filterIsInstance<MdBlock.OrderedList>().first()
        assertEquals(listOf("第一步"), list.items.map { it.text })
    }

    @Test fun `indented prose is still a continuation`() {
        val blocks = parse("- 第一行\n  续行内容\n- 第二条")
        assertEquals(listOf("第一行\n续行内容", "第二条"), blocks.itemTexts())
        assertTrue(blocks.none { it is MdBlock.CodeBlock })
    }

    @Test fun `column zero fence under a bullet still becomes a code block`() {
        // Control: this shape already worked before the fix (the loops broke on
        // `indent <= baseIndent`); pin it so the new guard cannot regress it.
        val blocks = parse("- 引入:\n```python\nx = 1\n```\n- 下一条")
        assertEquals(1, blocks.filterIsInstance<MdBlock.CodeBlock>().size)
        assertEquals(listOf("引入:", "下一条"), blocks.itemTexts())
    }

    @Test fun `fence body after the interruption is captured completely`() {
        // The break must hand the fence to the fenced-code branch, not drop lines.
        val blocks = parse("- 头\n  ```\n  line one\n  line two\n  ```\n尾")
        val code = blocks.filterIsInstance<MdBlock.CodeBlock>().single()
        assertEquals("  line one\n  line two", code.code)
        assertTrue(blocks.last() is MdBlock.Paragraph)
    }
}

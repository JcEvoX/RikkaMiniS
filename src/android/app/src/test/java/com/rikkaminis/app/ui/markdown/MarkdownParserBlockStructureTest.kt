package com.rikkaminis.app.ui.markdown

import com.rikkaminis.app.ui.markdown.MarkdownParser.Block
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Block-structure probe test for [MarkdownParser.parse] / [parseWithMath] —
 * 2026-10-12 session T5. Corpus matrix + structural invariants over the whole
 * block grammar (headings / fenced code / blockquote / lists / thematic breaks
 * / tables / media / math promotion / boundaries). Assertion literals were
 * captured from real runs of the parser (probe driver), not derived from the
 * source.
 *
 * Pins two fixes (same session):
 *  - heading trailing-# handling: "## title ##" used to render "title #"
 *    (residue) while "## C#" used to render "C" (content eaten);
 *  - setext-vs-table ambiguity: "Title\n---" used to parse as an empty
 *    Table(header=["Title"]) because the pipe-less "---" matched
 *    isTableSeparator as a single-column separator row.
 */
class MarkdownParserBlockStructureTest {

    private fun parse(md: String): List<Block> = MarkdownParser.parse(md)

    // ── headings ──────────────────────────────────────────────────────────

    @Test
    fun `headings levels 1 to 6`() {
        val b = parse("# a\n## b\n### c\n#### d\n##### e\n###### f")
        val levels = b.map { (it as Block.Heading).level }
        assertEquals(listOf(1, 2, 3, 4, 5, 6), levels)
        assertEquals(listOf("a", "b", "c", "d", "e", "f"), b.map { (it as Block.Heading).content })
    }

    @Test
    fun `heading with space-preceded closing hashes is stripped clean`() {
        val b = parse("## title ##")
        assertEquals("title", (b[0] as Block.Heading).content)
    }

    @Test
    fun `heading with multiple trailing hashes is stripped clean`() {
        assertEquals("a", (parse("## a ###")[0] as Block.Heading).content)
        assertEquals("a", (parse("# a #")[0] as Block.Heading).content)
    }

    @Test
    fun `hash inside heading content is preserved`() {
        // "C#" is real content — a closing sequence must be preceded by space.
        assertEquals("C#", (parse("## C#")[0] as Block.Heading).content)
        assertEquals("a#", (parse("## a#")[0] as Block.Heading).content)
    }

    @Test
    fun `mid-line hashes stay in content`() {
        assertEquals("t ## x", (parse("## t ## x")[0] as Block.Heading).content)
    }

    @Test
    fun `hash without space and 7 hashes are paragraphs`() {
        assertTrue(parse("#nospace")[0] is Block.Paragraph)
        assertTrue(parse("####### seven")[0] is Block.Paragraph)
    }

    // ── fenced code ───────────────────────────────────────────────────────

    @Test
    fun `fenced code with language`() {
        val b = parse("```kotlin\nval x = 1\n```\nafter")
        val code = b[0] as Block.CodeBlock
        assertEquals("kotlin", code.language)
        assertEquals("val x = 1", code.code)
        assertTrue(b[1] is Block.Paragraph)
    }

    @Test
    fun `fenced code without language`() {
        val code = parse("```\nplain\n```\n")[0] as Block.CodeBlock
        assertEquals("", code.language)
        assertEquals("plain", code.code)
    }

    @Test
    fun `unclosed fence consumes to end of input`() {
        val code = parse("```py\nprint(1)\n")[0] as Block.CodeBlock
        assertEquals("py", code.language)
        assertEquals("print(1)\n", code.code)
    }

    @Test
    fun `indented fence still closes`() {
        val code = parse("   ```\nind\n   ```\n")[0] as Block.CodeBlock
        assertEquals("ind", code.code)
    }

    @Test
    fun `inline fence mention inside code is kept verbatim`() {
        val code = parse("```\ninner ``` not close\n```\n")[0] as Block.CodeBlock
        assertEquals("inner ``` not close", code.code)
    }

    @Test
    fun `tilde fence is not a code block in this parser - design boundary`() {
        // GFM ~~~ fences are unsupported by parse(); extractMath skips them
        // verbatim but the block parser sees three paragraphs. Registered
        // design boundary, not fixed (no known instance).
        val b = parse("~~~\ncode\n~~~\n")
        assertEquals(1, b.size)
        assertTrue(b[0] is Block.Paragraph)
    }

    @Test
    fun `4-space and tab indented lines stay paragraphs - design boundary`() {
        assertEquals("    indented code line", (parse("    indented code line\n")[0] as Block.Paragraph).content)
        assertEquals("\ttabbed line", (parse("\ttabbed line\n")[0] as Block.Paragraph).content)
    }

    // ── blockquote ────────────────────────────────────────────────────────

    @Test
    fun `simple blockquote strips marker`() {
        val q = parse("> hello\n")[0] as Block.Blockquote
        assertEquals(listOf<Block>(Block.Paragraph("hello")), q.blocks)
    }

    @Test
    fun `bare blockquote marker is empty block list`() {
        val q = parse(">\n")[0] as Block.Blockquote
        assertEquals(emptyList<Block>(), q.blocks)
    }

    @Test
    fun `nested blockquote keeps structure`() {
        val q = parse("> a\n> > b\n")[0] as Block.Blockquote
        assertEquals(2, q.blocks.size)
        assertTrue(q.blocks[0] is Block.Paragraph)
        val inner = q.blocks[1] as Block.Blockquote
        assertEquals(listOf<Block>(Block.Paragraph("b")), inner.blocks)
    }

    @Test
    fun `multi paragraph blockquote`() {
        val q = parse("> a\n>\n> b\n")[0] as Block.Blockquote
        assertEquals(2, q.blocks.size)
        assertEquals("a", (q.blocks[0] as Block.Paragraph).content)
        assertEquals("b", (q.blocks[1] as Block.Paragraph).content)
    }

    @Test
    fun `blockquote containing a list`() {
        val q = parse("> - x\n> - y\n")[0] as Block.Blockquote
        assertEquals(1, q.blocks.size)
        val list = q.blocks[0] as Block.BulletList
        assertEquals(listOf("x", "y"), list.items.map { it.content })
    }

    // ── bullet lists ──────────────────────────────────────────────────────

    @Test
    fun `bullet markers dash star plus`() {
        val list = parse("- a\n* b\n+ c\n")[0] as Block.BulletList
        assertEquals(listOf("a", "b", "c"), list.items.map { it.content })
    }

    @Test
    fun `indented continuation joins the bullet content`() {
        val list = parse("- a\n  cont line\n- b\n")[0] as Block.BulletList
        assertEquals(listOf("a\ncont line", "b"), list.items.map { it.content })
    }

    @Test
    fun `bullet nesting is flattened - design boundary`() {
        // No nested-list support: "  - b" becomes a second flat item.
        val list = parse("- a\n  - b\n")[0] as Block.BulletList
        assertEquals(listOf("a", "b"), list.items.map { it.content })
    }

    @Test
    fun `loose bullet list splits into two lists - design boundary`() {
        // Blank line terminates the list; CommonMark would keep one loose list.
        val b = parse("- a\n\n- b\n")
        assertEquals(2, b.size)
        assertTrue(b[0] is Block.BulletList && b[1] is Block.BulletList)
    }

    @Test
    fun `task list markers parsed`() {
        val list = parse("- [x] done\n- [ ] todo\n- [X] capital\n")[0] as Block.BulletList
        assertEquals(listOf(true, false, true), list.items.map { it.checked })
        assertEquals(listOf("done", "todo", "capital"), list.items.map { it.content })
    }

    // ── numbered lists ────────────────────────────────────────────────────

    @Test
    fun `numbered list start number preserved`() {
        val list = parse("5. 五\n6. 六\n")[0] as Block.NumberedList
        assertEquals(5, list.startNumber)
        assertEquals(listOf("五", "六"), list.items.map { it.content })
    }

    @Test
    fun `indented numbered item up to 3 spaces`() {
        assertEquals(2, (parse("  2. x\n")[0] as Block.NumberedList).startNumber)
    }

    @Test
    fun `4-space indented numbered line stays paragraph`() {
        assertTrue(parse("    3. deep\n")[0] is Block.Paragraph)
    }

    // ── thematic breaks & setext boundary ─────────────────────────────────

    @Test
    fun `thematic break variants`() {
        for (md in listOf("---", "***", "___", "- - -", "----")) {
            assertEquals(md, Block.ThematicBreak, parse(md + "\n")[0])
        }
    }

    @Test
    fun `setext dash after paragraph is not a table`() {
        // "Title\n---" is a setext h2 in CommonMark; this parser does not
        // support setext, so it must fall back to Paragraph + ThematicBreak —
        // never an empty Table(header=["Title"]).
        val b = parse("Title\n---\n")
        assertEquals(2, b.size)
        assertEquals("Title", (b[0] as Block.Paragraph).content)
        assertEquals(Block.ThematicBreak, b[1])
    }

    @Test
    fun `setext equals after paragraph stays a paragraph`() {
        val b = parse("Title\n===\n")
        assertEquals(1, b.size)
        assertEquals("Title\n===", (b[0] as Block.Paragraph).content)
    }

    // ── tables ────────────────────────────────────────────────────────────

    @Test
    fun `basic table parses headers and rows`() {
        val t = parse("| a | b |\n|---|---|\n| 1 | 2 |\n")[0] as Block.Table
        assertEquals(listOf("a", "b"), t.headers)
        assertEquals(listOf(listOf("1", "2")), t.rows)
    }

    @Test
    fun `table alignment variants`() {
        val t = parse("l|c|r\n:---|:---:|---:\nx|y|z\n")[0] as Block.Table
        assertEquals(listOf(
            MarkdownParser.Alignment.LEFT,
            MarkdownParser.Alignment.CENTER,
            MarkdownParser.Alignment.RIGHT,
        ), t.alignments)
        assertEquals(listOf(listOf("x", "y", "z")), t.rows)
    }

    @Test
    fun `ragged table keeps row cells as written - design intent`() {
        // Rows are NOT padded/truncated to header length; renderer pads.
        val more = parse("| a | b |\n|---|---|\n| 1 | 2 | 3 |\n")[0] as Block.Table
        assertEquals(listOf(listOf("1", "2", "3")), more.rows)
        val less = parse("| a | b |\n|---|---|\n| 1 |\n")[0] as Block.Table
        assertEquals(listOf(listOf("1")), less.rows)
    }

    @Test
    fun `escaped pipe in cell is split as-is - registered design boundary`() {
        // GFM would treat \| as a literal pipe inside the cell; this parser
        // splits it (cell "x \" + "y"). Registered, not fixed.
        val t = parse("| a | b |\n|---|---|\n| x \\| y | 2 |\n")[0] as Block.Table
        assertEquals(listOf("x \\", "y", "2"), t.rows[0])
    }

    @Test
    fun `pipe inside link url survives the cell split`() {
        val t = parse("| a | b |\n|---|---|\n| [f](u\\|v.png) | 2 |\n")[0] as Block.Table
        assertEquals("[f](u\\|v.png)", t.rows[0][0])
        assertEquals("2", t.rows[0][1])
    }

    // ── media ─────────────────────────────────────────────────────────────

    @Test
    fun `standalone image with png`() {
        val img = parse("![alt](https://x/y.png)\n")[0] as Block.Image
        assertEquals("alt", img.alt)
        assertEquals("https://x/y.png", img.url)
    }

    @Test
    fun `standalone video routed by mp4 extension`() {
        assertTrue(parse("![clip](minis://workspace/clip.mp4)\n")[0] is Block.Video)
    }

    @Test
    fun `standalone audio routed by mp3 extension`() {
        assertTrue(parse("![song](minis://workspace/s.mp3)\n")[0] is Block.Audio)
    }

    @Test
    fun `percent-encoded hash still routes by real extension`() {
        assertTrue(parse("![v](https://x/foo%23China.mp4)\n")[0] is Block.Video)
    }

    @Test
    fun `query is stripped before the extension is read`() {
        // [dv22/B-06] `clip.mp4?t=1` used to fall through to Image: the extension
        // read produced "mp4?t=1", which is in neither extension set. The query
        // must be cut first, in the one shared classifier.
        assertTrue(parse("![clip](minis://workspace/clip.mp4?t=1)\n")[0] is Block.Video)
        assertTrue(parse("![clip](minis://workspace/clip.mp4?raw=1)\n")[0] is Block.Video)
        assertTrue(parse("![clip](https://x/clip.mp4?t=1)\n")[0] is Block.Video)
    }

    @Test
    fun `hash is a filename character for local media`() {
        // A local file may legally be named `foo#China.mp4`; cutting `#` on a
        // minis:// path would mangle it. Only http(s) treats `#` as a fragment.
        assertTrue(parse("![v](minis://workspace/foo#China.mp4)\n")[0] is Block.Video)
    }

    @Test
    fun `absolute guest path media routes by extension`() {
        assertTrue(parse("![v](/var/minis/workspace/clip.m4v)\n")[0] is Block.Video)
        assertTrue(parse("![s](/var/minis/workspace/song.wav)\n")[0] is Block.Audio)
    }

    @Test
    fun `non-ascii media filename keeps its url`() {
        val img = parse("![图](minis://workspace/图片.png)\n")[0] as Block.Image
        assertEquals("minis://workspace/图片.png", img.url)
    }

    @Test
    fun `image inline with text stays a paragraph`() {
        val p = parse("text ![a](u.png) more\n")[0] as Block.Paragraph
        assertEquals("text ![a](u.png) more", p.content)
    }

    // ── boundaries ────────────────────────────────────────────────────────

    @Test
    fun `empty input returns empty list`() {
        assertEquals(emptyList<Block>(), parse(""))
        assertEquals(emptyList<Block>(), parse("\n\n\n"))
    }

    @Test
    fun `no trailing newline still parses`() {
        val b = parse("# h\ntext")
        assertEquals("h", (b[0] as Block.Heading).content)
        assertEquals("text", (b[1] as Block.Paragraph).content)
    }

    @Test
    fun `crlf line endings handled`() {
        val b = parse("# h\r\n\r\ntext\r\n")
        assertEquals("h", (b[0] as Block.Heading).content)
        val p = b[1] as Block.Paragraph
        assertEquals("text", p.content)
        assertTrue("no stray CR in content: ${p.content}", !p.content.contains('\r'))
    }

    @Test
    fun `consecutive blank lines collapse`() {
        val b = parse("a\n\n\n\nb\n")
        assertEquals(2, b.size)
        assertEquals("a", (b[0] as Block.Paragraph).content)
        assertEquals("b", (b[1] as Block.Paragraph).content)
    }

    // ── math (parseWithMath) ──────────────────────────────────────────────

    @Test
    fun `display math dollar dollar becomes MathBlock`() {
        val r = MarkdownParser.parseWithMath("\$\$E=mc^2\$\$")
        assertEquals(listOf<Block>(Block.MathBlock("E=mc^2")), r.blocks)
    }

    @Test
    fun `display math bracket form becomes MathBlock`() {
        val r = MarkdownParser.parseWithMath("\\[\nx+1\n\\]")
        assertEquals(listOf<Block>(Block.MathBlock("\nx+1\n")), r.blocks)
    }

    @Test
    fun `inline math paren stays embedded in paragraph`() {
        val r = MarkdownParser.parseWithMath("value \\(x+1\\) here")
        assertEquals(1, r.blocks.size)
        val p = r.blocks[0] as Block.Paragraph
        assertTrue("placeholder kept inline", p.content.contains(MarkdownParser.MathSpan::class.qualifiedName ?: "") || p.content.contains("MATH"))
        assertEquals("x+1", r.mathSpans[0].latex)
        assertTrue(!r.mathSpans[0].isBlock)
    }

    @Test
    fun `inline math dollar stays embedded in paragraph`() {
        val r = MarkdownParser.parseWithMath("value \$x+1\$ here")
        assertEquals(1, r.blocks.size)
        assertEquals("x+1", r.mathSpans[0].latex)
        assertTrue(!r.mathSpans[0].isBlock)
    }

    @Test
    fun `mhchem ce block preserved`() {
        val r = MarkdownParser.parseWithMath("\$\$\\ce{H2O}\$\$")
        assertEquals(listOf<Block>(Block.MathBlock("\\ce{H2O}")), r.blocks)
    }

    @Test
    fun `paragraph containing only display math is promoted`() {
        val r = MarkdownParser.parseWithMath("text\n\n\$\$E=mc^2\$\$\n\ntail")
        assertEquals(listOf<Block>(
            Block.Paragraph("text"),
            Block.MathBlock("E=mc^2"),
            Block.Paragraph("tail"),
        ), r.blocks)
    }

    @Test
    fun `paragraph with only inline math stays a paragraph`() {
        val r = MarkdownParser.parseWithMath("\$x$\n")
        assertEquals(1, r.blocks.size)
        assertTrue(r.blocks[0] is Block.Paragraph)
    }

    @Test
    fun `currency amounts are not math`() {
        val r = MarkdownParser.parseWithMath("it costs \$5 and \$10 total")
        assertEquals(1, r.blocks.size)
        assertTrue(r.mathSpans.isEmpty())
        assertEquals("it costs \$5 and \$10 total", (r.blocks[0] as Block.Paragraph).content)
    }

    @Test
    fun `math inside code fence survives verbatim`() {
        val r = MarkdownParser.parseWithMath("```\n\$x\$\n```")
        assertTrue(r.mathSpans.isEmpty())
        assertEquals("\$x\$", (r.blocks[0] as Block.CodeBlock).code)
    }

    @Test
    fun `escaped dollar is preserved`() {
        val r = MarkdownParser.parseWithMath("\\\$5 is money")
        assertTrue(r.mathSpans.isEmpty())
        assertEquals("\\\$5 is money", (r.blocks[0] as Block.Paragraph).content)
    }

    @Test
    fun `two display maths in one paragraph split into blocks`() {
        val r = MarkdownParser.parseWithMath("a \$\$E_1\$\$ b \$\$E_2\$\$ c")
        assertTrue(r.blocks[0] is Block.Paragraph)
        assertEquals("E_1", (r.blocks[1] as Block.MathBlock).latex)
        assertTrue(r.blocks[2] is Block.Paragraph)
        assertEquals("E_2", (r.blocks[3] as Block.MathBlock).latex)
        assertTrue(r.blocks[4] is Block.Paragraph)
    }

    // ── structural invariants ─────────────────────────────────────────────

    @Test
    fun `parse is idempotent on the same input`() {
        val samples = listOf(
            "# h\n- a\n  cont\n> q\n| a | b |\n|---|---|\n| 1 | 2 |\n```kotlin\nx\n```\nend",
            "Title\n---\n",
            "## title ##\n## C#\n",
        )
        for (md in samples) {
            assertEquals(md, parse(md), parse(md))
        }
    }

    @Test
    fun `blockquote inner content never keeps the marker`() {
        val q = parse("> one\n> two\n")[0] as Block.Blockquote
        val p = q.blocks[0] as Block.Paragraph
        assertTrue("no '> ' residue: $p", !p.content.contains("> "))
    }

    @Test
    fun `code block content is preserved verbatim`() {
        val code = parse("```\n  leading spaces\n\ttab\n```")[0] as Block.CodeBlock
        assertEquals("  leading spaces\n\ttab", code.code)
    }

    @Test
    fun `heading content never keeps leading hashes`() {
        for (md in listOf("# x", "## y", "###### z")) {
            val h = parse(md)[0] as Block.Heading
            assertTrue(!h.content.startsWith("#"))
        }
    }

    // ── [dv15/B-07] glued block starts behind a paragraph ─────────────────
    // A block start with NO blank line before it. The paragraph loop's break
    // set used to be narrower than the block rules themselves (`# ` only, no
    // table probe), so the start line was collected as paragraph text and the
    // block never materialised. Expectations below are literal values captured
    // from a real run of the fixed parser (probe-b4/jvm/post/driver-post.log),
    // not derived from the source or from the chat-side twin.

    @Test
    fun `level 2 heading glued to a paragraph splits it`() {
        val b = parse("正文段落。\n## 二级标题")
        assertEquals(2, b.size)
        assertEquals("正文段落。", (b[0] as Block.Paragraph).content)
        assertEquals(Block.Heading(2, "二级标题"), b[1])
    }

    @Test
    fun `level 6 heading glued to a paragraph splits it`() {
        val b = parse("正文段落。\n###### 六级标题")
        assertEquals(2, b.size)
        assertEquals("正文段落。", (b[0] as Block.Paragraph).content)
        assertEquals(Block.Heading(6, "六级标题"), b[1])
    }

    @Test
    fun `level 1 heading glued to a paragraph still splits - parity anchor`() {
        // Passed before the fix too: this is the anchor proving the new break
        // widens the old one rather than replacing it with something else.
        val b = parse("正文段落。\n# 一级标题")
        assertEquals(2, b.size)
        assertEquals("正文段落。", (b[0] as Block.Paragraph).content)
        assertEquals(Block.Heading(1, "一级标题"), b[1])
    }

    @Test
    fun `hash without space and seven hashes stay glued prose`() {
        // The break uses the same predicate as the heading rule, so lines the
        // rule refuses stay prose — this file's contrast anchor.
        assertEquals(
            "正文段落。\n#nospace 标签",
            (parse("正文段落。\n#nospace 标签")[0] as Block.Paragraph).content,
        )
        assertEquals(
            "正文段落。\n####### seven",
            (parse("正文段落。\n####### seven")[0] as Block.Paragraph).content,
        )
    }

    @Test
    fun `indented heading line splits off as its own paragraph`() {
        // Break tests the TRIMMED line (the pre-fix `trimStart().startsWith`
        // shape, and the twin's `isAtxHeading(t)`), while the heading RULE reads
        // the raw column-0 line — an indented heading therefore splits the
        // paragraph and renders as a paragraph, exactly as level 1 always did.
        val b = parse("text\n  # indented heading")
        assertEquals(2, b.size)
        assertEquals("text", (b[0] as Block.Paragraph).content)
        assertEquals("  # indented heading", (b[1] as Block.Paragraph).content)
    }

    @Test
    fun `table glued to a paragraph becomes a table`() {
        val b = parse("说明文字\n| a | b |\n|---|---|\n| 1 | 2 |")
        assertEquals(2, b.size)
        assertEquals("说明文字", (b[0] as Block.Paragraph).content)
        val t = b[1] as Block.Table
        assertEquals(listOf("a", "b"), t.headers)
        assertEquals(listOf(MarkdownParser.Alignment.LEFT, MarkdownParser.Alignment.LEFT), t.alignments)
        assertEquals(listOf(listOf("1", "2")), t.rows)
    }

    @Test
    fun `aligned table glued to a paragraph keeps its alignment`() {
        val b = parse("说明文字\nl|c|r\n:---|:---:|---:\nx|y|z")
        assertEquals(2, b.size)
        assertEquals("说明文字", (b[0] as Block.Paragraph).content)
        val t = b[1] as Block.Table
        assertEquals(listOf("l", "c", "r"), t.headers)
        assertEquals(
            listOf(
                MarkdownParser.Alignment.LEFT,
                MarkdownParser.Alignment.CENTER,
                MarkdownParser.Alignment.RIGHT,
            ),
            t.alignments,
        )
        assertEquals(listOf(listOf("x", "y", "z")), t.rows)
    }

    @Test
    fun `pipe-less header glued pair is a single column table`() {
        // The break reuses the Table rule's own predicate (`|` in header OR
        // separator), so this pair parses the same way it does behind a blank
        // line: single-column table, no rows.
        val b = parse("说明文字\nabc\n|---|")
        assertEquals(2, b.size)
        assertEquals("说明文字", (b[0] as Block.Paragraph).content)
        val t = b[1] as Block.Table
        assertEquals(listOf("abc"), t.headers)
        assertEquals(emptyList<List<String>>(), t.rows)
    }

    @Test
    fun `tab after marker quote glued to a paragraph splits it`() {
        // `>` + TAB is a quote (the twin's `isBlockquoteLine` width); the old
        // rule only accepted `"> "` or a bare `">"`, so this line was swallowed
        // as paragraph text. The marker is stripped, the tab is content.
        val b = parse("正文段落。\n>\t制表符引用")
        assertEquals(2, b.size)
        assertEquals("正文段落。", (b[0] as Block.Paragraph).content)
        val q = b[1] as Block.Blockquote
        assertEquals(listOf<Block>(Block.Paragraph("\t制表符引用")), q.blocks)
    }

    @Test
    fun `comparison-initial glued line is still prose`() {
        // ">= 5" is not a quote: the predicate needs space/TAB/EOL after `>`.
        assertEquals(
            "正文段落。\n>= 5 比较运算",
            (parse("正文段落。\n>= 5 比较运算")[0] as Block.Paragraph).content,
        )
    }

    @Test
    fun `bare marker glued to a paragraph starts the quote`() {
        val b = parse("正文段落。\n>\n> 引用内容")
        assertEquals(2, b.size)
        assertEquals("正文段落。", (b[0] as Block.Paragraph).content)
        assertEquals(listOf<Block>(Block.Paragraph("引用内容")), (b[1] as Block.Blockquote).blocks)
    }
}

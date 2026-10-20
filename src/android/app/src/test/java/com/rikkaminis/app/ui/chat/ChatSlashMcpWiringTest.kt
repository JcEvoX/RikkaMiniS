package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertTrue
import org.junit.Assume
import org.junit.Test
import java.io.File

/**
 * Wiring probes for [T-mcp-integration-android] + FIX-4 B-01 (deep-audit-1037).
 *
 * `SlashCommand.isMcp` was written but never read: an MCP row tap fell into
 * `executeSlashCommand`'s action dispatch, whose unrecognized-id arm returns
 * `""` — WIPING the composer instead of filling it with the server name, the
 * way the skill rows (which read `isSkill`) do. The slash menu is Compose and
 * the ViewModel needs a full Application, so the wiring is asserted against
 * the sources themselves — the same probe shape as `FastioWiringTest`.
 *
 * Every expected string is a literal. Each probe has a negative control that
 * mutates a copy of the real source and asserts the very same check fails —
 * without one, a deleted branch would show up as a green test checking
 * nothing.
 */
class ChatSlashMcpWiringTest {

    private val tokenFile = "src/main/java/com/rikkaminis/app/ui/chat/ChatSlashTokenExt.kt"
    private val inputFile = "src/main/java/com/rikkaminis/app/ui/chat/ChatInputArea.kt"

    // ── the probes, as functions so a mutated copy can be run through them ──

    /**
     * An MCP row must fill the composer (the skill-row contract), never reach
     * the action dispatch — whose `else ->` arm returns "" and clears the box.
     */
    private fun assertMcpFillsComposer(token: String) {
        val exec = bodyOf(token, "fun ChatViewModel.executeSlashCommand(")
        assertTrue("executeSlashCommand must exist", exec.isNotEmpty())
        assertTrue(
            "the isMcp branch must exist — without it an MCP row tap wipes the composer",
            exec.contains("if (cmd.isMcp)"),
        )
        assertTrue(
            "the isMcp branch must return the shared fill helper (prepend /<server> " +
                "to the saved original, or just fill on typed \"/\") — checking the " +
                "MCP BRANCH, not the file: the sibling skill branch also routes " +
                "through the helper, so a file-wide test would stay green while the " +
                "MCP row still wiped the composer",
            mcpBranchText(token).contains("return fillComposerWithSlashTitle(cmd, saved)"),
        )
        val mcpIdx = exec.indexOf("if (cmd.isMcp)")
        val dispatchIdx = exec.indexOf("when (cmd.id)")
        assertTrue(
            "the isMcp branch must run BEFORE the action dispatch, or the MCP row " +
                "still falls into the unrecognized-id arm",
            dispatchIdx == -1 || mcpIdx < dispatchIdx,
        )
    }

    /** The typing-aid rows must keep the IME up and stay visually grouped. */
    private fun assertMenuRowsTreatMcpAsTypingAid(input: String) {
        assertTrue(
            "ChatInputArea must read isMcp in a condition — the installed-row divider " +
                "and the IME restore used to be gated on a bare isSkill test",
            input.contains(".isMcp"),
        )
        assertTrue(
            "the IME restore must cover both typing-aid rows",
            input.contains("if (cmd.isSkill || cmd.isMcp)"),
        )
    }

    // ── tests ────────────────────────────────────────────────────────────────

    @Test
    fun `an MCP row tap fills the composer instead of wiping it`() {
        assertMcpFillsComposer(source(tokenFile))
    }

    @Test
    fun `the slash menu treats MCP rows as typing aids`() {
        assertMenuRowsTreatMcpAsTypingAid(source(inputFile))
    }

    @Test
    fun `negative control - removing the isMcp branch fails the composer probe`() {
        val original = source(tokenFile)
        val mutated = removeMcpBranch(original)
        Assume.assumeTrue("the isMcp branch must exist to be removable", mutated != original)
        assertTrue("the isMcp branch must be gone after removal",
            Regex("""if \(cmd\.isMcp\)""").findAll(mutated).count() == 0)
        var failed = false
        try {
            assertMcpFillsComposer(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the composer probe must fail once the isMcp branch is gone", failed)
    }

    @Test
    fun `negative control - a branch that returns blank fails the composer probe`() {
        val original = source(tokenFile)
        // The exact pre-fix shape: the MCP row reaches the dispatch's else arm,
        // whose blank result is what wiped the composer.
        val mutated = mcpBranchReturnsBlank(original)
        Assume.assumeTrue("the MCP branch must exist to be rewritten", mutated != original)
        assertTrue("the MCP branch must now return blank", mutated.contains("return \"\""))
        var failed = false
        try {
            assertMcpFillsComposer(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the composer probe must fail on a blank-returning MCP branch", failed)
    }

    @Test
    fun `negative control - a skill-only IME restore fails the typing-aid probe`() {
        val original = source(inputFile)
        val mutated = original.replace(
            "if (cmd.isSkill || cmd.isMcp) {",
            "if (cmd.isSkill) {",
        )
        Assume.assumeTrue("the shared typing-aid gate must exist to be narrowed", mutated != original)
        var failed = false
        try {
            assertMenuRowsTreatMcpAsTypingAid(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the typing-aid probe must fail once MCP rows are skill-only", failed)
    }

    // ── source location (same technique as DatabaseVersionGuardTest) ─────────
    private fun source(rel: String): String = sourceFile(rel).readText()

    /**
     * [start, end] line indices of the `if (cmd.isMcp) { … }` block.
     * Line-anchored on purpose: the branch body carries a `${…}` template, so a
     * brace-counting walker would stop at the first `}` INSIDE the string literal.
     */
    private fun mcpBranchRange(src: String): Pair<Int, Int>? {
        val lines = src.split("\n")
        val start = lines.indexOfFirst { it.trim() == "if (cmd.isMcp) {" }
        if (start == -1) return null
        val end = (start + 1 until lines.size).firstOrNull { lines[it].trim() == "}" } ?: return null
        return start to end
    }

    private fun mcpBranchText(src: String): String {
        val range = mcpBranchRange(src) ?: return ""
        return src.split("\n").subList(range.first, range.second + 1).joinToString("\n")
    }

    private fun removeMcpBranch(src: String): String {
        val range = mcpBranchRange(src) ?: return src
        return src.split("\n")
            .filterIndexed { i, _ -> i < range.first || i > range.second }
            .joinToString("\n")
    }

    private fun mcpBranchReturnsBlank(src: String): String {
        val range = mcpBranchRange(src) ?: return src
        val lines = src.split("\n").toMutableList()
        val ret = (range.first + 1..range.second)
            .firstOrNull { lines[it].trim().startsWith("return ") } ?: return src
        lines[ret] = "        return \"\""
        return lines.joinToString("\n")
    }

    /**
     * Comment- and string-blanked copy of [src] with LENGTH AND OFFSETS
     * PRESERVED — a `${…}` template or a `//` inside a URL would otherwise
     * desync brace counting (the B-01 branch carries a `${cmd.id}` literal).
     * Because blanking keeps indices aligned, slices can be taken from the
     * original text using offsets found in the skeleton.
     */
    private fun skeleton(src: String): String {
        val out = StringBuilder(src.length)
        val stack = mutableListOf('c' to 0)
        var i = 0
        while (i < src.length) {
            val frame = stack.last()
            if (frame.first == 'c') {
                if (src.startsWith("\"\"\"", i)) {
                    out.append("   "); i += 3
                    while (i < src.length && !src.startsWith("\"\"\"", i)) {
                        out.append(if (src[i] == '\n') '\n' else ' '); i++
                    }
                    if (i < src.length) { out.append("   "); i += 3 }
                    continue
                }
                val c = src[i]
                if (c == '"') { stack.add('s' to 0); out.append('"'); i++; continue }
                if (src.startsWith("//", i)) {
                    while (i < src.length && src[i] != '\n') { out.append(' '); i++ }
                    continue
                }
                if (src.startsWith("/*", i)) {
                    out.append("  "); i += 2
                    while (i < src.length && !src.startsWith("*/", i)) {
                        out.append(if (src[i] == '\n') '\n' else ' '); i++
                    }
                    if (i < src.length) { out.append("  "); i += 2 }
                    continue
                }
                if (c == '{') { stack[stack.lastIndex] = 'c' to (frame.second + 1); out.append(c); i++; continue }
                if (c == '}') {
                    if (frame.second == 0 && stack.size > 1) { stack.removeAt(stack.lastIndex); out.append(c); i++; continue }
                    stack[stack.lastIndex] = 'c' to (frame.second - 1); out.append(c); i++; continue
                }
                out.append(c); i++
                continue
            }
            val c = src[i]
            if (c == '\\') { out.append("  "); i += 2; continue }
            if (src.startsWith("${'$'}{", i)) { stack.add('c' to 0); out.append("${'$'}{"); i += 2; continue }
            if (c == '"') { stack.removeAt(stack.lastIndex); out.append('"'); i++; continue }
            out.append(if (c == '\n') '\n' else ' '); i++
        }
        return out.toString()
    }

    /**
     * Code-only `{ … }` body of the declaration containing [marker]. Comments
     * and string contents are blanked (offsets preserved), so NEITHER prose (a
     * KDoc naming the very call under test) NOR a log literal can satisfy an
     * assertion. The parameter list is skipped — a default lambda in the
     * signature (`clampToViewportBounds: () -> Rect? = { null }`) would
     * otherwise win the "first brace" race.
     */
    private fun bodyOf(text: String, marker: String): String {
        val skel = skeleton(text)
        val idx = skel.indexOf(marker)
        if (idx == -1) return ""
        var i = skel.indexOf('(', idx)
        if (i == -1) return ""
        var depth = 0
        while (i < skel.length) {
            when (skel[i]) {
                '(' -> depth++
                ')' -> {
                    depth--
                    if (depth == 0) break
                }
            }
            i++
        }
        return braceSlice(skel, skel.indexOf('{', i + 1))
    }

    private fun braceSlice(skel: String, start: Int): String {
        if (start == -1) return ""
        var depth = 0
        var i = start
        while (i < skel.length) {
            when (skel[i]) {
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) return skel.substring(start, i + 1)
                }
            }
            i++
        }
        return skel.substring(start)
    }

    private fun sourceFile(rel: String): File {
        val viaAndroid = "src/android/app/$rel"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel)
            if (File(d, viaAndroid).isFile) return File(d, viaAndroid)
            dir = d.parentFile
        }
        error("'$rel' not found from ${File(".").absoluteFile}")
    }
}

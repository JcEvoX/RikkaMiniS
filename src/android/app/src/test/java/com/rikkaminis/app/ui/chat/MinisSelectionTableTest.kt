package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertTrue
import org.junit.Assume
import org.junit.Test
import java.io.File

/**
 * Wiring probes for [T-android-markdown-table-copy-actions] + FIX-4 B-02
 * (deep-audit-1037).
 *
 * A table publishes Copy Table / Copy Table Image to the [SelectionController]
 * so ONE selection toolbar appends them after Copy / Copy Markdown / etc. The
 * wiring was half-done: `selectionTableActions()` was called from
 * `MinisMarkdownTextToolbar` only — the main-chat
 * `MinisSelectionToolbarHost` (the long-press toolbar chat users actually see)
 * never surfaced the actions. On top of that the registry was keyed by the
 * bare messageId, so with two tables in one message the later overwrote the
 * earlier and a table leaving composition dropped its sibling's entry.
 *
 * Compose screens and a live SelectionController cannot be exercised on the
 * JVM (no Robolectric), so the wiring is asserted against the sources — same
 * probe shape as `FastioWiringTest`, with a negative control per probe.
 */
class MinisSelectionTableTest {

    private val gestureFile = "src/main/java/com/rikkaminis/app/ui/chat/MinisTextKitGesture.kt"
    private val selectionFile = "src/main/java/com/rikkaminis/app/ui/chat/MinisTextKitSelection.kt"
    private val streamFile = "src/main/java/com/rikkaminis/app/ui/chat/StreamingMarkdownText.kt"
    private val mdToolbarFile = "src/main/java/com/rikkaminis/app/ui/chat/MinisMarkdownTextToolbar.kt"

    // ── the probes ───────────────────────────────────────────────────────────

    /** The main-chat long-press toolbar must surface the table actions. */
    private fun assertMainChatToolbarWired(gesture: String) {
        val host = bodyOf(gesture, "fun MinisSelectionToolbarHost(")
        assertTrue("MinisSelectionToolbarHost must exist", host.isNotEmpty())
        assertTrue(
            "the main-chat toolbar must resolve the table actions from the controller — " +
                "without this the long-press toolbar never shows Copy Table",
            host.contains("selectionTableActions()"),
        )
        assertTrue(
            "both table buttons must call the published actions",
            host.contains("copyTableMarkdown()") && host.contains("copyTableImage()"),
        )
    }

    /** The markdown-toolbar path is a legitimate consumer and must stay. */
    private fun assertMarkdownToolbarStillConsumes(mdToolbar: String) {
        assertTrue(
            "MinisMarkdownTextToolbar must keep consuming selectionTableActions() — " +
                "removing it is a regression, not a cleanup",
            mdToolbar.contains("selectionTableActions()"),
        )
    }

    /** The registry key must carry the table dimension, and the publisher must pass it. */
    private fun assertTableLevelKey(selection: String, stream: String) {
        assertTrue(
            "rememberTableActions must take a table-level key beyond messageId",
            Regex("""fun rememberTableActions\(messageId: String,\s*tableIndex: Int,""")
                .containsMatchIn(selection),
        )
        assertTrue(
            "forgetTableActions must take the table key too — a bare messageId remove " +
                "takes a still-composed sibling table's entry",
            Regex("""fun forgetTableActions\(messageId: String,\s*tableIndex: Int\)""")
                .containsMatchIn(selection),
        )
        assertTrue(
            "the registry declaration must be two-level (an Int table dimension inside " +
                "the per-message map)",
            Regex("""SnapshotStateMap<Int,\s*TableActions>""").containsMatchIn(selection),
        )
        assertTrue(
            "RenderTable must publish under the table key",
            Regex("""rememberTableActions\(messageId,\s*tableIndex,\s*actions\)""")
                .containsMatchIn(stream),
        )
        assertTrue(
            "RenderTable must source the table index from the shard allocator",
            stream.contains("LocalShardSubIndexAllocator.current"),
        )
    }

    /** Cross-message selections expose no single-table actions. */
    private fun assertCrossMessageExcluded(selection: String) {
        val fn = bodyOf(selection, "fun selectionTableActions(")
        assertTrue("selectionTableActions must exist", fn.isNotEmpty())
        assertTrue(
            "a selection spanning messages must not expose table actions — the guard " +
                "on singleMessageId() is the contract",
            fn.contains("singleMessageId()"),
        )
        assertTrue(
            "the table under the selection must be resolved from the selection's shard",
            fn.contains("tableIndexFromShard("),
        )
    }

    // ── tests ────────────────────────────────────────────────────────────────

    @Test
    fun `main-chat toolbar surfaces the table copy actions`() {
        assertMainChatToolbarWired(source(gestureFile))
    }

    @Test
    fun `markdown toolbar keeps consuming the table actions`() {
        assertMarkdownToolbarStillConsumes(source(mdToolbarFile))
    }

    @Test
    fun `the table registry is keyed per table and the publisher passes the index`() {
        assertTableLevelKey(source(selectionFile), source(streamFile))
    }

    @Test
    fun `cross-message selections expose no table actions`() {
        assertCrossMessageExcluded(source(selectionFile))
    }

    @Test
    fun `negative control - unwiring the main-chat toolbar fails the wiring probe`() {
        val original = source(gestureFile)
        val mutated = original.replace(
            "val tableActions = controller.selectionTableActions()",
            "val tableActions: SelectionController.TableActions? = null",
        )
        Assume.assumeTrue("the wiring must exist to be removable", mutated != original)
        var failed = false
        try {
            assertMainChatToolbarWired(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the wiring probe must fail once the toolbar stops resolving actions", failed)
    }

    @Test
    fun `negative control - reverting the registry key to bare messageId fails the key probe`() {
        val original = source(selectionFile)
        val mutated = original
            .replace("fun rememberTableActions(messageId: String, tableIndex: Int,", "fun rememberTableActions(messageId: String,")
            .replace("fun forgetTableActions(messageId: String, tableIndex: Int)", "fun forgetTableActions(messageId: String)")
            .replace("SnapshotStateMap<Int, TableActions>", "TableActions")
        Assume.assumeTrue("the table key must exist to be reverted", mutated != original)
        var failed = false
        try {
            assertTableLevelKey(mutated, source(streamFile))
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the key probe must fail on a bare-messageId registry", failed)
    }

    @Test
    fun `negative control - a cross-message-blind resolver fails the exclusion probe`() {
        val original = source(selectionFile)
        // Mutate the ORIGINAL text (bodyOf returns a comment/string-blanked
        // slice, which is not a substring of the original). The guard literal
        // also appears in selectionMessageMarkdown(); mutating both is fine —
        // the probe reads the selectionTableActions body only.
        val mutated = original
            .replace("val msgId = singleMessageId() ?: return null", "val msgId = \"any\"")
            .replace("tableIndexFromShard(", "unusedTableIndexFromShard(")
        Assume.assumeTrue("the guard must exist to be removed", mutated != original)
        var failed = false
        try {
            assertCrossMessageExcluded(mutated)
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the exclusion probe must fail once the single-message guard is gone", failed)
    }

    // ── source helpers ───────────────────────────────────────────────────────
    private fun source(rel: String): String = sourceFile(rel).readText()

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

package com.rikkaminis.app.ui.chat

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv24-b3 / deep-audit-1044 B-06] "New Chat" with an unsent draft must not
 * destroy it.
 *
 * Two decisions are pinned here, both on the JVM:
 *
 *  1. **Behaviour, real object** — [isDrawerRowVisible] is the drawer's whole
 *     visibility decision, extracted from `ChatHistoryDrawer` precisely so this
 *     class can drive the production function instead of a mirror. Four row
 *     classes: a promoted draft (preview, no messages) must show; an ordinary
 *     chat must show; a pinned empty row must show; the `/memory` / `/thinking`
 *     ghost (no pin, no messages, no preview — or a whitespace-only preview)
 *     must stay hidden.
 *  2. **Source shape, real file** — `promoteDraftIfNeeded` cannot be invoked
 *     without a `ChatViewModel`, a Room database and a coroutine scope, so the
 *     decision it must make ("hand the captured text to a durable writer owned
 *     by the promoted session, and only then drop the composer copy") is read
 *     off the compiled sources. [promoteViolations] is the criterion; the
 *     negative controls below run it against mutated copies of the real text,
 *     so a criterion that can no longer see the pre-fix shape fails here rather
 *     than passing silently.
 *
 * Every expectation is a literal — nothing is derived from a production
 * constant, so changing one turns a test red instead of quietly moving the bar.
 */
class PromoteDraftVisibilityTest {

    private companion object {
        const val ENTRY_REL = "src/main/java/com/rikkaminis/app/ui/chat/ChatSessionEntryExt.kt"
        const val DRAWER_REL = "src/main/java/com/rikkaminis/app/ui/chat/ChatHistoryDrawer.kt"
    }

    // ── 1. the drawer's visibility decision (real production function) ─────

    @Test
    fun `a promoted draft with a preview and no messages is visible`() {
        assertTrue(
            "a promoted draft's body lives in last_message until its first send — hiding the row " +
                "is exactly the bug (the text is then unreachable)",
            isDrawerRowVisible(pinnedAt = null, messageCount = 0, lastMessage = "两百字的草稿正文"),
        )
    }

    @Test
    fun `an ordinary chat is visible through its message count`() {
        assertTrue(
            isDrawerRowVisible(pinnedAt = null, messageCount = 3, lastMessage = null),
        )
    }

    @Test
    fun `a pinned empty row stays visible`() {
        assertTrue(
            isDrawerRowVisible(pinnedAt = 1_700_000_000_000L, messageCount = 0, lastMessage = null),
        )
    }

    @Test
    fun `a ghost row with no pin, no message and no preview is hidden`() {
        assertFalse(
            "an empty chat materialised by a /memory or /thinking toggle must not appear in the " +
                "drawer (P0-1-drawer-title-visibility)",
            isDrawerRowVisible(pinnedAt = null, messageCount = 0, lastMessage = null),
        )
    }

    @Test
    fun `a whitespace-only preview is not content`() {
        assertFalse(
            "blankness, not null-ness, decides: a whitespace preview carries nothing",
            isDrawerRowVisible(pinnedAt = null, messageCount = 0, lastMessage = "   \n"),
        )
    }

    @Test
    fun `an empty-string preview is not content`() {
        assertFalse(
            isDrawerRowVisible(pinnedAt = null, messageCount = 0, lastMessage = ""),
        )
    }

    // ── 2. the promotion's persistence decision (real source, real file) ────

    @Test
    fun `the real promoteDraftIfNeeded persists the body after ensureSession`() {
        val shape = promoteShape(source(ENTRY_REL))
        assertTrue(
            "the promoted text must reach a durable writer (appendMessage / updateSessionPreview) " +
                "that carries it — a 50-char title alone leaves the body nowhere",
            shape.durableAt >= 0,
        )
        assertTrue(
            "the durable write must come AFTER ensureSession(): persisting against a session id " +
                "that does not exist yet is silently dropped",
            shape.durableAt > shape.ensureAt && shape.ensureAt >= 0,
        )
        assertTrue(
            "every other criterion in the gate assumes this body exists",
            shape.body.isNotBlank(),
        )
    }

    @Test
    fun `the composer copy is dropped only after the durable write`() {
        val shape = promoteShape(source(ENTRY_REL))
        assertTrue(
            "clear the composer only once the transcript row exists, so a cancellation in between " +
                "leaves the text somewhere",
            shape.composerLiteralAt < 0 || shape.composerLiteralAt > shape.durableAt,
        )
    }

    @Test
    fun `the pre-fix title-only shape is caught by the criterion`() {
        // Negative control #1 — the exact regression this branch fixes: a title
        // write, both in-memory copies dropped, nothing durable.
        val preFix = stripBlock(
            source(ENTRY_REL),
            "chatRepository.appendMessage(",
        ).replace(
            "        _messages.value = _messages.value + ChatMessage(\n" +
                "            id = promoted.id,\n" +
                "            role = \"user\",\n" +
                "            content = text,\n" +
                "        )\n",
            "",
        )
        val violations = promoteViolations(preFix)
        assertTrue(
            "the criterion must still recognise the shape this branch was written for: $violations",
            violations.any { it.contains("never persists") },
        )
    }

    @Test
    fun `blanking the composer before the durable write is caught`() {
        // Negative control #2 — the ordering hazard: clear first, persist later.
        val reordered = stripBlock(source(ENTRY_REL), "chatRepository.appendMessage(")
            .replace(
                "    com.rikkaminis.app.data.ComposerDraftStore.clearDraft(context, sessionId)\n",
                "    com.rikkaminis.app.data.ComposerDraftStore.clearDraft(context, sessionId)\n" +
                    "    _inputText.value = \"\"\n",
            )
        val violations = promoteViolations(reordered)
        assertTrue(
            "a blanked composer with no durable copy must be reported: $violations",
            violations.isNotEmpty(),
        )
    }

    @Test
    fun `the drawer filter routes through the shared predicate`() {
        val drawer = source(DRAWER_REL)
        assertTrue(
            "the composable must call isDrawerRowVisible(...) inside its sessions.filter — an " +
                "inline copy is what hid promoted drafts in the first place",
            Regex("sessions\\s*\\.\\s*filter\\s*\\{[^}]*isDrawerRowVisible\\(").containsMatchIn(drawer),
        )
    }

    @Test
    fun `the baseline predicate really did hide a preview-only row`() {
        // The PRE-fix decision, transcribed: this is what the drawer computed
        // before the fix, and it is why the promoted row was invisible.
        fun baseline(pinnedAt: Long?, messageCount: Int): Boolean =
            pinnedAt != null || messageCount > 0

        assertFalse(
            "the promoted row has no messages and no pin, so the old filter dropped it",
            baseline(pinnedAt = null, messageCount = 0),
        )
        assertTrue(
            "and the fixed decision keeps it",
            isDrawerRowVisible(pinnedAt = null, messageCount = 0, lastMessage = "草稿"),
        )
    }

    // ── promotion shape probe ───────────────────────────────────────────────

    /** Where the promotion's three decision points sit inside the function body. */
    private data class PromoteShape(
        val body: String,
        val ensureAt: Int,
        val durableAt: Int,
        val composerLiteralAt: Int,
    )

    private fun promoteShape(src: String): PromoteShape {
        val body = functionBody(src, "fun ChatViewModel.promoteDraftIfNeeded(")
        return PromoteShape(
            body = body,
            ensureAt = body.indexOf("ensureSession("),
            durableAt = durableWriteAt(body),
            composerLiteralAt = composerLiteralAt(body),
        )
    }

    private fun promoteViolations(src: String): List<String> {
        val shape = promoteShape(src)
        val out = mutableListOf<String>()
        if (shape.durableAt < 0) {
            out += "never persists the captured draft text into the promoted session"
        } else if (shape.ensureAt < 0 || shape.durableAt < shape.ensureAt) {
            out += "persists the body before ensureSession()"
        }
        if (shape.composerLiteralAt >= 0 && shape.durableAt >= 0 &&
            shape.composerLiteralAt < shape.durableAt
        ) {
            out += "blanks the composer before the durable body write"
        }
        return out
    }

    /**
     * Offset of a durable writer whose ARGUMENT LIST carries the captured text
     * (`text`), or -1. Judged on the payload, not on the call name alone: a
     * title-only `updateSessionTitleAndCategory(sid, title, null)` must not
     * count as "the body is persisted".
     */
    private fun durableWriteAt(body: String): Int {
        for (token in listOf("appendMessage(", "updateSessionPreview(")) {
            var from = body.indexOf(token)
            while (from >= 0) {
                val open = body.indexOf("(", from + token.length - 1)
                val close = matchingParen(body, open)
                if (close > open && body.substring(open, close + 1).contains("text")) return from
                from = body.indexOf(token, from + 1)
            }
        }
        return -1
    }

    /** Offset of `_inputText.value = "<literal>` (a constant write), or -1. */
    private fun composerLiteralAt(body: String): Int {
        val match = Regex("_inputText\\.value\\s*=\\s*\"").find(body) ?: return -1
        return match.range.first
    }

    private fun matchingParen(text: String, open: Int): Int {
        if (open < 0) return -1
        var depth = 0
        for (i in open until text.length) {
            when (text[i]) {
                '(' -> depth++
                ')' -> {
                    depth--
                    if (depth == 0) return i
                }
            }
        }
        return -1
    }

    private fun functionBody(src: String, decl: String): String {
        val start = src.indexOf(decl)
        if (start < 0) return ""
        val open = src.indexOf("{", start)
        if (open < 0) return ""
        var depth = 0
        for (i in open until src.length) {
            when (src[i]) {
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) return src.substring(open, i + 1)
                }
            }
        }
        return ""
    }

    /** Drop one call plus its continuation lines (used to model the pre-fix shape). */
    private fun stripBlock(src: String, token: String): String {
        val at = src.indexOf(token)
        if (at < 0) return src
        val open = src.indexOf("(", at)
        val close = matchingParen(src, open)
        if (close < 0) return src
        val lineStart = src.lastIndexOf('\n', at) + 1
        var end = src.indexOf('\n', close)
        end = if (end < 0) src.length else end + 1
        // `lineStart` already sits on the `val promoted = ` prefix, so a call
        // written across several lines is removed whole.
        return src.removeRange(lineStart, end)
    }

    // ── source location (same technique as FastioWiringTest) ────────────────

    private fun source(rel: String): String = sourceFile(rel).readText()

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

package com.rikkaminis.app.config

import java.io.File
import java.io.FileNotFoundException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv24-A05] — `minis-config set session.thinkingLevel` / `session.primaryModel`
 * wrote the Room row and NOTHING else, while the UI kept reading a
 * `ChatViewModel` that reads both fields exactly once (inside `loadSession()`)
 * and is cached for the whole process by `ChatViewModelStore`. The agent's
 * write returned `ok` and the chat kept using the old model / level; the next
 * in-app picker tap then wrote that stale memory back over the row.
 *
 * Two halves are pinned here, both on the real source (the behaviour lives in
 * Android types — `ConfigRegistry`, `ChatViewModel` — so it is not compilable
 * as a plain JVM object graph; the sibling `SessionConfigTargetTest` pins the
 * one pure decision that IS compilable):
 *
 *   1. each `session.*` writer keeps BOTH halves of the write — the DAO call
 *      (cross-restart store) and a resident-VM entry from the refresh family;
 *   2. the resident lookup cannot create a VM (`peekViewModel` must not
 *      `getOrPut` / go through `ViewModelProvider`), because refreshing a
 *      session that is not on screen must stay a no-op.
 *
 * Every negative control mutates a *copy* of the real source text and asserts
 * that the very same check which passes on the tree goes red on the mutation.
 */
class SessionConfigWriteThroughTest {

    private val builtins =
        "src/android/app/src/main/java/com/rikkaminis/app/config/ConfigBuiltins.kt"
    private val store =
        "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatViewModelStore.kt"

    private val thinkingPath = """path = "session.thinkingLevel""""
    private val bindingPath = """path = "session.primaryModel""""

    /**
     * The VM-side entry points that legitimately apply a session configuration
     * to the live chat: the thinking picker's own entry, the two priorities of
     * `loadSession`'s model chain, and the explicit model pickers. Anchored on
     * the family (a rename inside it must not turn this into a spelling test).
     */
    private val vmRefreshTokens = listOf(
        "setThinkingLevel(",
        "restoreFromBinding(",
        "applyDefaultModelFallback(",
        "applyNewChatDefaultModel(",
        "selectEntry(",
        "selectGroup(",
        "selectGroupEntry(",
    )

    // ── thinking level writer ───────────────────────────────────────────────

    @Test
    fun `the thinking writer persists the level AND refreshes the live chat`() {
        val block = writerBlock(codeOnly(read(builtins)), thinkingPath)
        assertTrue(
            "the Room row is the only cross-restart store — the DAO write must stay",
            block.contains("updateThinkingOverride("),
        )
        assertTrue(
            "the write must also reach the resident ChatViewModel, else the open " +
                "chat keeps using the old level (and later writes it back): $block",
            vmRefreshTokens.any { block.contains(it) },
        )
        assertTrue(
            "the refusal contract must survive the refactor",
            block.contains("No active session"),
        )
    }

    @Test
    fun `negative control - a DAO-only thinking writer must fail the same check`() {
        val src = codeOnly(read(builtins))
        val block = writerBlock(src, thinkingPath)
        // Baseline shape: one `runBlocking { dao.updateThinkingOverride(…) }`.
        val baseline = block.replace(
            Regex("""writeSessionConfig\(\s*sessionId = sid,[\s\S]*?\n\s*\)"""),
            "runBlocking { chatRepo.dao.updateThinkingOverride(sid, level.name) }",
        )
        assertFalse("the mutation must actually apply", baseline == block)
        assertTrue("the DAO half survives the mutation", baseline.contains("updateThinkingOverride("))
        assertFalse(
            "the baseline DAO-only writer must be caught",
            vmRefreshTokens.any { baseline.contains(it) },
        )
    }

    // ── primary model writer (two branches) ─────────────────────────────────

    @Test
    fun `both branches of the binding writer reach the live chat`() {
        val block = writerBlock(codeOnly(read(builtins)), bindingPath)
        val halves = block.split("return@ClosureField")
        assertEquals(
            "the writer must keep its clear/echo split (one early return)",
            2,
            halves.size,
        )
        val clear = halves[0]
        val set = halves[1]
        assertTrue(
            "the clear branch must still null the row",
            clear.contains("updateSessionBinding("),
        )
        assertTrue(
            "the clear branch must run the same default-model fallback the next " +
                "loadSession() would, else the chat keeps showing the cleared pin",
            vmRefreshTokens.any { clear.contains(it) },
        )
        assertTrue(
            "the set branch must still write the binding row",
            set.contains("updateSessionBinding("),
        )
        assertTrue(
            "the set branch must push the new binding into the live chat: $set",
            vmRefreshTokens.any { set.contains(it) },
        )
    }

    @Test
    fun `negative control - dropping one branch's refresh must fail the same check`() {
        val block = writerBlock(codeOnly(read(builtins)), bindingPath)
        val halves = block.split("return@ClosureField")
        val mutatedSet = halves[1]
            .replace(Regex("""refresh = \{ vm ->[\s\S]*?\n\s*\},\n"""), "")
        assertFalse("the mutation must actually apply", mutatedSet == halves[1])
        assertTrue(
            "the set branch keeps its DAO write",
            mutatedSet.contains("updateSessionBinding("),
        )
        assertFalse(
            "a set branch that only writes the row must be caught",
            vmRefreshTokens.any { mutatedSet.contains(it) },
        )
        // The clear branch is untouched — that is what makes this "one branch".
        assertTrue(vmRefreshTokens.any { halves[0].contains(it) })
    }

    // ── the resident lookup itself ─────────────────────────────────────────

    @Test
    fun `the resident lookup reads and never creates`() {
        val body = memberBody(codeOnly(read(store)), "fun peekViewModel(")
        assertTrue(
            "peek must read the existing stores (anchor: `stores[`)",
            body.contains("stores["),
        )
        for (creation in listOf("getOrPut(", "ViewModelProvider", ".put(")) {
            assertFalse(
                "peekViewModel must not create a ViewModel/Store ($creation): refreshing " +
                    "a session that is not on screen has to stay a no-op",
                body.contains(creation),
            )
        }
        // And the writers must actually go through it.
        assertTrue(
            "ConfigBuiltins must route the refresh through the non-creating lookup",
            codeOnly(read(builtins)).contains("peekViewModel("),
        )
    }

    @Test
    fun `negative control - a peeking getOrPut must fail the same check`() {
        val body = memberBody(codeOnly(read(store)), "fun peekViewModel(")
        val mutated = body.replace("stores[", "stores.getOrPut(")
        assertFalse("the mutation must actually apply", mutated == body)
        assertTrue(
            "a creating lookup must be caught",
            mutated.contains("getOrPut("),
        )
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private fun read(rel: String): String {
        val f = File(repoRoot(), rel)
        if (!f.exists()) throw FileNotFoundException(f.path)
        return f.readText()
    }

    private fun repoRoot(): File {
        System.getProperty("probe.repo")?.let { return File(it) }
        var dir: File? = File(System.getProperty("user.dir") ?: ".")
        while (dir != null) {
            if (File(dir, builtins).exists()) return dir
            dir = dir.parentFile
        }
        throw FileNotFoundException(
            "repo root not found from user.dir=${System.getProperty("user.dir")}",
        )
    }

    /** The `writer = { … }` block of the field whose `path = "…"` is [pathMarker]. */
    private fun writerBlock(code: String, pathMarker: String): String {
        val at = code.indexOf(pathMarker)
        assertTrue("path marker not found: $pathMarker", at >= 0)
        val w = code.indexOf("writer = {", at)
        assertTrue("no writer= block after $pathMarker", w >= 0)
        return braceSliceAt(code, w + "writer = ".length)
    }

    /** The `fun <marker>…{ … }` block (declaration form, e.g. `fun peekViewModel(`). */
    private fun memberBody(code: String, marker: String): String {
        val at = code.indexOf(marker)
        assertTrue("member not found: $marker", at >= 0)
        val open = code.indexOf('{', at)
        assertTrue("no body after $marker", open >= 0)
        return braceSliceAt(code, open)
    }

    /** `src` from index [from] (must be a `{`) to its balanced closing brace. */
    private fun braceSliceAt(src: String, from: Int): String {
        assertEquals("slice must start at a brace", '{', src[from])
        var depth = 0
        var i = from
        while (i < src.length) {
            when (src[i]) {
                '{' -> depth += 1
                '}' -> {
                    depth -= 1
                    if (depth == 0) return src.substring(from, i + 1)
                }
            }
            i += 1
        }
        assertTrue("unbalanced braces at $from", false)
        return ""
    }

    /**
     * Comments stripped — hand-written linear scan on purpose (the production
     * KDocs quote both the old and the new spelling, so a comment must be able
     * to neither satisfy nor break these checks).
     */
    private fun codeOnly(src: String): String {
        val out = StringBuilder(src.length)
        var i = 0
        while (i < src.length) {
            val c = src[i]
            if (c == '/' && i + 1 < src.length && src[i + 1] == '/') {
                while (i < src.length && src[i] != '\n') i++
            } else if (c == '/' && i + 1 < src.length && src[i + 1] == '*') {
                i += 2
                while (i + 1 < src.length && !(src[i] == '*' && src[i + 1] == '/')) i++
                i = if (i + 2 > src.length) src.length else i + 2
            } else {
                out.append(c)
                i += 1
            }
        }
        return out.toString()
    }
}

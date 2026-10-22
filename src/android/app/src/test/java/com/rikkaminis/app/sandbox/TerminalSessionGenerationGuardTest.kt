package com.rikkaminis.app.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [deep-audit-1042 A-03, P1] Source-shape probe for the terminal session's
 * ownership settlement.
 *
 * The defect is a *stale-release* defect and, like the ordering defect
 * `TerminalSessionInitOrderTest` pins, its trigger needs a live PRoot PTY
 * (Context, Termux JNI, `PRootKernel.boot`) plus two racing `start()` calls —
 * none of which a JVM unit test can stage. What IS mechanical is where the
 * settlement lives, so this probe asserts that:
 *
 * 1. `onSessionFinished` — the generation bump, the `termuxSession` slot
 *    clear, the `_state.value` publish and the `liveSessions` removal must all
 *    sit inside the ownership guard `if (termuxSession === changedSession)`.
 *    Pre-fix only the slot clear was guarded, so a stale callback landing
 *    after a newer `start()` bumped the generation (aborting the fresh boot at
 *    its next `myGeneration != startGeneration.get()` checkpoint), published
 *    STOPPED over the live session, and unregistered it from `liveSessions`
 *    (no more timezone/proxy broadcasts).
 *
 * 2. `start()`'s failure handler — the catch must compare the slot against a
 *    `createdSession` local captured at creation time and reaped through that
 *    local. The pre-fix form self-bound the comparison
 *    (`termuxSession?.let { s -> if (termuxSession === s) … }`): `s` IS
 *    `termuxSession`, so the "ownership" test was a tautology and the handler
 *    killed a newer attempt's PTY, cleared its slot and flipped STOPPED over
 *    it.
 *
 * Every arm also runs the same analyser over a pre-fix fixture, so a probe
 * that has quietly stopped looking fails loudly instead of passing. Comments
 * are blanked (offsets preserved) before analysis — the KDoc in both functions
 * quotes the very shapes being located.
 */
class TerminalSessionGenerationGuardTest {

    private val sessionRel =
        "src/android/app/src/main/java/com/rikkaminis/app/sandbox/TerminalSession.kt"

    // ── source access ────────────────────────────────────────────────────────

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

    /** Comments blanked, every offset preserved (string-literal aware). */
    private fun codeOnly(src: String): String {
        val out = StringBuilder(src.length)
        var i = 0
        val n = src.length
        while (i < n) {
            val c = src[i]
            if (c == '"' || c == '\'') {
                val end = skipLiteral(src, i)
                out.append(src, i, end)
                i = end
                continue
            }
            if (src.startsWith("//", i)) {
                var j = i
                while (j < n && src[j] != '\n') {
                    out.append(' ')
                    j++
                }
                i = j
                continue
            }
            if (src.startsWith("/*", i)) {
                val close = src.indexOf("*/", i + 2)
                val j = if (close < 0) n else close + 2
                for (k in i until j) out.append(if (src[k] == '\n') '\n' else ' ')
                i = j
                continue
            }
            out.append(c)
            i++
        }
        return out.toString()
    }

    private fun skipLiteral(code: String, i: Int): Int {
        val q = code[i]
        if (q == '"' && code.startsWith("\"\"\"", i)) {
            val j = code.indexOf("\"\"\"", i + 3)
            return if (j < 0) code.length else j + 3
        }
        var j = i + 1
        while (j < code.length) {
            if (code[j] == '\\') {
                j += 2
                continue
            }
            if (code[j] == q || code[j] == '\n') return j + 1
            j++
        }
        return code.length
    }

    /** The single `{ … }` block whose declaration matches [decl], else null. */
    private fun blockAt(code: String, decl: Regex): String? {
        val matches = decl.findAll(code).toList()
        if (matches.size != 1) return null
        val open = code.indexOf('{', matches[0].range.first)
        if (open < 0) return null
        val close = matchBrace(code, open) ?: return null
        return code.substring(open, close + 1)
    }

    /** Index of the brace closing the one at [openIdx], quote-aware. */
    private fun matchBrace(text: String, openIdx: Int): Int? =
        matchDelim(text, openIdx, '{', '}')

    private fun matchParen(text: String, openIdx: Int): Int? =
        matchDelim(text, openIdx, '(', ')')

    private fun matchDelim(text: String, openIdx: Int, open: Char, close: Char): Int? {
        var depth = 0
        var i = openIdx
        val n = text.length
        while (i < n) {
            val c = text[i]
            if (c == '"' || c == '\'') {
                i = skipLiteral(text, i)
                continue
            }
            when (c) {
                open -> depth++
                close -> {
                    depth--
                    if (depth == 0) return i
                }
            }
            i++
        }
        return null
    }

    private fun lineOf(code: String, idx: Int): Int =
        code.take(idx).count { it == '\n' } + 1

    // ── analyser: onSessionFinished settlement ──────────────────────────────

    /** Spans covered by `if (termuxSession === changedSession)` (block or
     *  single brace-less statement). */
    private fun ownershipRanges(body: String): List<IntRange> {
        val out = mutableListOf<IntRange>()
        val re = Regex("if\\s*\\(\\s*termuxSession\\s*===\\s*changedSession\\s*\\)")
        for (m in re.findAll(body)) {
            var k = m.range.last + 1
            while (k < body.length && (body[k] == ' ' || body[k] == '\t')) k++
            if (k < body.length && body[k] == '{') {
                val close = matchBrace(body, k) ?: (body.length - 1)
                out.add((k + 1) until close)
            } else {
                val nl = body.indexOf('\n', m.range.last)
                out.add(k until (if (nl < 0) body.length else nl))
            }
        }
        return out
    }

    private fun onFinishedViolations(code: String): List<String> {
        val body = blockAt(code, Regex("\\bfun\\s+onSessionFinished\\s*\\("))
            ?: return listOf("onSessionFinished declaration missing or ambiguous")
        val out = mutableListOf<String>()
        val ranges = ownershipRanges(body)
        if (ranges.isEmpty()) {
            out += "onSessionFinished: no `if (termuxSession === changedSession)` " +
                "ownership guard — the whole settlement is unowned"
        }
        val decisions = listOf(
            "startGeneration bump" to
                Regex("\\bstartGeneration\\s*\\.\\s*increment\\w*\\s*\\("),
            "termuxSession slot clear" to Regex("\\btermuxSession\\s*=(?!=)"),
            "_state.value publish" to Regex("\\b_state\\.value\\s*=(?!=)"),
            "liveSessions removal" to Regex("\\bliveSessions\\s*\\.\\s*remove\\w*"),
        )
        for ((label, rx) in decisions) {
            val hits = rx.findAll(body).toList()
            if (hits.isEmpty()) {
                out += "onSessionFinished: $label is gone — [audit-0917] requires " +
                    "the natural-exit path to mirror stop()'s bookkeeping"
            }
            for (m in hits) {
                if (ranges.none { m.range.first in it }) {
                    out += "onSessionFinished: $label at line " +
                        "${lineOf(body, m.range.first)} sits OUTSIDE the ownership " +
                        "guard — a stale callback would settle a newer session's state"
                }
            }
        }
        return out
    }

    // ── analyser: start() failure handler ───────────────────────────────────

    /** Blocks whose `if`/`else if` condition consults slot / supersession /
     *  attempt-identity state. */
    private fun conditionalBlocks(body: String, nameRe: Regex): List<IntRange> {
        val out = mutableListOf<IntRange>()
        for (m in Regex("\\bif\\s*\\(").findAll(body)) {
            val open = body.indexOf('(', m.range.first)
            val close = matchParen(body, open) ?: continue
            val cond = body.substring(open + 1, close)
            if (!nameRe.containsMatchIn(cond)) continue
            var k = close + 1
            while (k < body.length && (body[k] == ' ' || body[k] == '\t')) k++
            if (k < body.length && body[k] == '{') {
                val c = matchBrace(body, k) ?: (body.length - 1)
                out.add((k + 1) until c)
            } else {
                val nl = body.indexOf('\n', close)
                out.add(k until (if (nl < 0) body.length else nl))
            }
        }
        return out
    }

    private fun catchViolations(code: String): List<String> {
        val start = blockAt(code, Regex("\\bfun\\s+start\\s*\\("))
            ?: return listOf("start() declaration missing or ambiguous")
        val out = mutableListOf<String>()
        val tryIdx = start.indexOf("try {")
        val declIdx = start.indexOf("var createdSession:")
        if (declIdx < 0) {
            out += "start(): the `createdSession` local is gone — the catch cannot " +
                "prove which PTY THIS attempt created"
        } else if (tryIdx >= 0 && declIdx > tryIdx) {
            out += "start(): `createdSession` is declared INSIDE the try — the " +
                "catch handler cannot see it"
        }
        if (!start.contains("createdSession = session")) {
            out += "start(): `createdSession = session` capture at creation is gone"
        }
        val catchM = Regex("catch\\s*\\(\\s*t\\s*:\\s*Throwable\\s*\\)\\s*\\{").find(start)
            ?: return out + listOf("start(): the start-failure catch block is gone")
        val catchOpen = catchM.range.last
        val catchClose = matchBrace(start, catchOpen) ?: (start.length - 1)
        val catchBody = start.substring(catchOpen, catchClose + 1)

        if (Regex("termuxSession\\s*\\?\\.\\s*let").containsMatchIn(catchBody)) {
            out += "start(): catch still self-binds `termuxSession?.let { s -> … " +
                "termuxSession === s … }` — `s` IS `termuxSession`, so the guard " +
                "is a tautology (1042-A-03)"
        }
        if (!catchBody.contains("termuxSession === createdSession")) {
            out += "start(): catch has no `termuxSession === createdSession` " +
                "ownership guard"
        }
        if (!Regex("createdSession\\s*\\?\\.\\s*let").containsMatchIn(catchBody)) {
            out += "start(): catch does not reap through `createdSession?.let { … }` " +
                "— it must kill THIS attempt's PTY, not what the slot points at"
        }
        if (Regex("killTermuxProcessTree\\s*\\(\\s*termuxSession\\s*\\)")
                .containsMatchIn(catchBody)
        ) {
            out += "start(): catch kills via the slot " +
                "(`killTermuxProcessTree(termuxSession)`) — a newer attempt's live " +
                "PTY would be killed"
        }
        val allowed = conditionalBlocks(
            catchBody, Regex("termuxSession|startGeneration|createdSession"))
        if (allowed.isEmpty()) {
            out += "start(): no conditional in the catch consults ownership state — " +
                "the settlement runs unconditionally"
        }
        val settlement = listOf(
            "termuxSession slot clear" to Regex("\\btermuxSession\\s*=(?!=)"),
            "_state.value publish" to Regex("\\b_state\\.value\\s*=(?!=)"),
            "liveSessions removal" to Regex("\\bliveSessions\\s*\\.\\s*remove\\w*"),
        )
        for ((label, rx) in settlement) {
            for (m in rx.findAll(catchBody)) {
                if (allowed.none { m.range.first in it }) {
                    out += "start(): catch $label at line " +
                        "${lineOf(catchBody, m.range.first)} is NOT gated on ownership"
                }
            }
        }
        return out
    }

    // ── live tree ────────────────────────────────────────────────────────────

    /** The live source with comments blanked — the production KDoc quotes the
     *  very pre-fix shapes this probe hunts, so analysis must never see
     *  comment text (caught by the first L3 run: the catch's self-binding
     *  check matched its own explanatory comment). */
    private fun liveCode(): String = codeOnly(sourceFile(sessionRel).readText())

    @Test
    fun `stale session-finished callback cannot settle state without owning the slot`() {
        val violations = onFinishedViolations(liveCode())
        assertEquals("onSessionFinished ownership violations: $violations",
            emptyList<String>(), violations)
    }

    @Test
    fun `start failure handler reaps only the session it created`() {
        val violations = catchViolations(liveCode())
        assertEquals("start() catch ownership violations: $violations",
            emptyList<String>(), violations)
    }

    // ── teeth: the pre-fix shapes must be rejected ───────────────────────────

    /** Pre-fix `onSessionFinished`, shape-quoted from the baseline tree. */
    private val preFixOnFinished = """
        class C {
            override fun onSessionFinished(changedSession: T) {
                startGeneration.incrementAndGet()
                if (termuxSession === changedSession) termuxSession = null
                if (_state.value != State.STOPPED) _state.value = State.STOPPED
                liveSessions.removeAll { it.get() === this@TerminalSession }
            }
        }
    """.trimIndent()

    /** Pre-fix `start()` failure handler, shape-quoted from the baseline tree. */
    private val preFixStartCatch = """
        class T {
            fun start() {
                scope.launch {
                    try {
                        termuxSession = session
                    } catch (t: Throwable) {
                        termuxSession?.let { s ->
                            if (termuxSession === s) termuxSession = null
                            runCatching { killTermuxProcessTree(s) }
                        }
                        liveSessions.removeAll { it.get() === this@TerminalSession }
                        _state.value = State.STOPPED
                    }
                }
            }
        }
    """.trimIndent()

    @Test
    fun `the pre-fix onSessionFinished settlement is rejected`() {
        val violations = onFinishedViolations(codeOnly(preFixOnFinished))
        assertTrue("expected violations, got $violations", violations.isNotEmpty())
        for (needle in listOf("startGeneration bump", "_state.value publish",
                "liveSessions removal")) {
            assertTrue("expected '$needle' to be flagged: $violations",
                violations.any { it.contains(needle) })
        }
        // Precision: the slot clear WAS guarded (brace-less if) pre-fix.
        assertTrue("the guarded slot clear must not be flagged: $violations",
            violations.none { it.contains("termuxSession slot clear") })
    }

    @Test
    fun `the pre-fix self-bound catch handler is rejected`() {
        val violations = catchViolations(codeOnly(preFixStartCatch))
        assertTrue("expected violations, got $violations", violations.isNotEmpty())
        assertTrue("expected the self-bound tautology to be named: $violations",
            violations.any { it.contains("self-binds") })
        assertTrue("expected the missing createdSession guard to be named: $violations",
            violations.any { it.contains("no `termuxSession === createdSession`") })
    }

    // ── teeth: mutations of the live tree must be caught ────────────────────

    @Test
    fun `hoisting one settlement decision out of the guard is caught`() {
        val live = liveCode()
        val guard = "if (termuxSession === changedSession) {"
        assertTrue("live tree no longer contains the ownership guard",
            live.contains(guard))
        val mutated = live.replaceFirst(
            guard, "startGeneration.incrementAndGet()\n            $guard")
        val violations = onFinishedViolations(mutated)
        assertEquals("expected exactly the hoisted decision to be flagged: $violations",
            1, violations.size) // one decision point, one mutation
        assertTrue(violations[0].contains("startGeneration bump"))
        assertTrue(violations[0].contains("OUTSIDE"))
    }

    @Test
    fun `dropping the createdSession capture is caught`() {
        val live = liveCode()
        assertTrue("live tree no longer captures createdSession",
            live.contains("createdSession = session"))
        val mutated = live.replace("createdSession = session", "")
        val violations = catchViolations(mutated)
        assertTrue("expected the missing capture to be flagged: $violations",
            violations.any { it.contains("capture at creation is gone") })
    }
}

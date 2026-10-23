package com.rikkaminis.app.config

import java.io.File
import java.io.FileNotFoundException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv25-A05 / dv25-A06] — the WIRING of the two defects, pinned on the real
 * source text.
 *
 * The behaviour itself lives in Android types (`ConfigRegistry`,
 * `ChatViewModel`, `ChatRepository`), so it cannot be compiled as a plain JVM
 * object graph; `SessionBindingTargetTest` pins the pure decisions this file's
 * checks depend on:
 *
 *  - every `session.*` READER reaches the resident `ChatViewModel` through the
 *    non-creating lookup (A-06: row-only reads answered a different value than
 *    the write had just accepted) — while still keeping the row as its
 *    no-VM fallback;
 *  - `applyModelOverride` consults the session's persisted binding BEFORE the
 *    default group (A-05: the default group is a new-session default, not an
 *    override) and refreshes the resident VM after the row write.
 *
 * Every negative control mutates a *copy* of the real source text and asserts
 * that the same check which passes on the tree goes red on the mutation.
 */
class SessionConfigReadThroughTest {

    private val builtins =
        "src/android/app/src/main/java/com/rikkaminis/app/config/ConfigBuiltins.kt"
    private val runner =
        "src/android/app/src/main/java/com/rikkaminis/app/debug/HeadlessChatRunner.kt"

    private val thinkingPath = """path = "session.thinkingLevel""""
    private val bindingPath = """path = "session.primaryModel""""

    /** The VM-side entries that apply a session binding to the live chat. */
    private val vmRefreshTokens = listOf(
        "restoreFromBinding(",
        "applyDefaultModelFallback(",
        "selectEntry(",
        "selectGroup(",
        "applyNewChatDefaultModel(",
    )

    // ── A-06: the readers ───────────────────────────────────────────────────

    @Test
    fun `the thinking reader answers from the resident vm and still falls back to the row`() {
        val code = codeOnly(read(builtins))
        val block = readerBlock(code, thinkingPath)
        assertTrue(
            "the thinking reader must reach the resident ChatViewModel through the " +
                "non-creating lookup — a row-only read reports a different value than " +
                "the write just reported as accepted: $block",
            reaches(code, block, "peekViewModel("),
        )
        assertTrue(
            "the row must stay the no-VM fallback (a session with no live chat has " +
                "only the row)",
            reaches(code, block, "getSession("),
        )
        assertTrue(
            "the resident branch must produce a value FROM the VM, not a constant — " +
                "peeking and then answering a literal is the same defect with extra " +
                "steps: ${residentArm(block)}",
            residentBranchProducesValue(block),
        )
    }

    @Test
    fun `the model binding reader answers from the resident vm for a draft`() {
        val code = codeOnly(read(builtins))
        val block = readerBlock(code, bindingPath)
        assertTrue(
            "the binding reader must reach the resident ChatViewModel — a draft's write " +
                "lands in the VM only, so the row cannot answer it: $block",
            reaches(code, block, "peekViewModel("),
        )
        assertTrue(
            "the row must stay the answer whenever it exists (it holds the explicit " +
                "pin, the VM holds the model in use)",
            reaches(code, block, "getSession("),
        )
        assertTrue(
            "the resident branch must produce a value FROM the VM, not a constant: " +
                residentArm(block),
            residentBranchProducesValue(block),
        )
    }

    @Test
    fun `negative control - a row-only thinking reader fails the same check`() {
        val code = codeOnly(read(builtins))
        val block = readerBlock(code, thinkingPath)
        // Baseline shape: one `runBlocking { dao.getSession(sid) }`, no VM read.
        val baseline = Regex("""val vm = residentViewModel\(sid\)\n""")
            .replace(block, "")
        assertFalse("the mutation must actually apply", baseline == block)
        assertTrue("the row half survives the mutation", baseline.contains("getSession("))
        assertFalse(
            "the baseline row-only reader must be caught",
            reaches(code, baseline, "peekViewModel("),
        )
    }

    @Test
    fun `negative control - a vm-only reader that drops the row falls back nowhere`() {
        val code = codeOnly(read(builtins))
        val block = readerBlock(code, bindingPath)
        val mutated = Regex("""val session = runBlocking \{ chatRepo\.dao\.getSession\(sid\) \}\n""")
            .replace(block, "")
        assertFalse("the mutation must actually apply", mutated == block)
        assertTrue("the VM half survives the mutation", mutated.contains("residentViewModel("))
        assertFalse(
            "a reader that cannot answer without a live chat must be caught",
            reaches(code, mutated, "getSession("),
        )
    }

    // ── A-05: the RPC model override ────────────────────────────────────────

    @Test
    fun `applyModelOverride consults the persisted binding through the shared decision`() {
        val code = codeOnly(read(runner))
        val body = memberBody(code, "suspend fun applyModelOverride(")
        assertTrue(
            "applyModelOverride must read the session's persisted binding: the caller " +
                "cannot tell 'session created by the previous RPC call' from 'session the " +
                "user pinned last week', so the new-session default group must not be " +
                "applied unconditionally: $body",
            body.contains("modelBinding"),
        )
        assertTrue(
            "the new-session-vs-pinned decision must stay delegated to the pure decision " +
                "(its six outcomes are pinned by SessionBindingTargetTest) instead of being " +
                "re-derived at the call site: $body",
            body.contains("resolveBindingTarget("),
        )
        val args = callArgs(body, "resolveBindingTarget(")
        assertTrue("the decision call must be locatable", args != null)
        assertTrue(
            "the ROW value has to be the argument the decision reads — a binding that is " +
                "read and then replaced by null/false is indistinguishable from no guard at " +
                "all: $args",
            args!!.contains("modelBinding"),
        )
        assertTrue(
            "the default group must stay reachable — the new-session path still binds it",
            body.contains("defaultPrimaryGroupId"),
        )
    }

    @Test
    fun `negative control - the unconditional default group fails the same check`() {
        val code = codeOnly(read(runner))
        val body = memberBody(code, "suspend fun applyModelOverride(")
        // Baseline shape: the fallback ran with no knowledge of the session's row.
        val baseline = body.replace(
            "existingBinding = app.chatRepository.dao.getSession(sessionId)?.modelBinding,",
            "existingBinding = null,",
        )
        assertFalse("the mutation must actually apply", baseline == body)
        assertTrue(
            "the default group is still referenced (this is the defect, not a delete)",
            baseline.contains("defaultPrimaryGroupId"),
        )
        val args = callArgs(baseline, "resolveBindingTarget(")
        assertFalse(
            "a decision fed with no binding (or a hard-coded null) must be caught",
            args != null && args.contains("modelBinding"),
        )
    }

    @Test
    fun `the binding write refreshes the resident vm after the row write`() {
        val code = codeOnly(read(runner))
        val body = memberBody(code, "private fun persistBinding(")
        assertTrue(
            "the Room row is the only cross-restart store — the DAO write must stay",
            body.contains("updateSessionBinding("),
        )
        assertTrue(
            "the resident lookup must be the non-creating peek (refreshing a session " +
                "that is not on screen must stay a no-op): $body",
            reaches(code, body, "peekViewModel("),
        )
        assertTrue(
            "the row write must be followed by a VM-side apply, or the screen keeps the " +
                "old model while the row says the new one",
            vmRefreshTokens.any { reaches(code, body, it) },
        )
    }

    @Test
    fun `negative control - dropping the refresh fails the same check`() {
        val code = codeOnly(read(runner))
        val body = memberBody(code, "private fun persistBinding(")
        val mutated = Regex("""\n\s*refresh = \{ resident ->[\s\S]*?\n\s*\},?""")
            .replace(body, "\n")
        assertFalse("the mutation must actually apply", mutated == body)
        assertTrue("the row half survives the mutation", mutated.contains("updateSessionBinding("))
        assertFalse(
            "a row-only binding write must be caught",
            vmRefreshTokens.any { reaches(code, mutated, it) },
        )
    }

    // ── A-06: the writer's availability gate ────────────────────────────────

    @Test
    fun `the primaryModel writer rejects unusable providers on both branches`() {
        val code = codeOnly(read(builtins))
        val block = writerBlock(code, bindingPath)
        val entryBranch = block.substringAfter("""s.startsWith("entry:")""")
            .substringBefore("""s.startsWith("group:")""")
        val groupBranch = block.substringAfter("""s.startsWith("group:")""")
        assertTrue(
            "the entry branch must gate on the SAME decision the runtime restore uses, " +
                "else the write succeeds and the chat refuses the binding: $entryBranch",
            reaches(code, entryBranch, "decideSessionModelResolution("),
        )
        assertTrue(
            "the group branch must require at least one usable member — " +
                "resolveProviderFromGroup returns false when every member is disabled / " +
                "keyless: $groupBranch",
            reaches(code, groupBranch, "decideSessionModelResolution("),
        )
    }

    @Test
    fun `negative control - a writer without the availability gate fails the same check`() {
        val code = codeOnly(read(builtins))
        val block = writerBlock(code, bindingPath)
        val entryGateCall = "requireUsableEntry(providerRepo, entry, \"entry:" + '$' + "uuid\")"
        val withoutEntry = block.replace(entryGateCall, "")
        assertFalse("the mutation must actually apply", withoutEntry == block)
        val entryBranch = withoutEntry.substringAfter("""s.startsWith("entry:")""")
            .substringBefore("""s.startsWith("group:")""")
        assertFalse(
            "an entry branch that only checks existence must be caught",
            reaches(code, entryBranch, "decideSessionModelResolution("),
        )
        val withoutGroup = block.replace("""val firstEntry = firstUsableGroupMember(providerRepo, group)""", "")
        assertFalse("the mutation must actually apply", withoutGroup == block)
        val groupBranch = withoutGroup.substringAfter("""s.startsWith("group:")""")
        assertFalse(
            "a group branch that accepts a group with no usable member must be caught",
            reaches(code, groupBranch, "decideSessionModelResolution("),
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

    /** The `reader = { … }` block of the field whose `path = "…"` is [pathMarker]. */
    private fun readerBlock(code: String, pathMarker: String): String {
        val at = code.indexOf(pathMarker)
        assertTrue("path marker not found: $pathMarker", at >= 0)
        val r = code.indexOf("reader = {", at)
        assertTrue("no reader= block after $pathMarker", r >= 0)
        val slice = braceSliceAt(code, r + "reader = ".length)
        assertTrue("unbalanced reader block for $pathMarker", slice != null)
        return slice!!
    }

    /** The `writer = { … }` block of the field whose `path = "…"` is [pathMarker]. */
    private fun writerBlock(code: String, pathMarker: String): String {
        val at = code.indexOf(pathMarker)
        assertTrue("path marker not found: $pathMarker", at >= 0)
        val w = code.indexOf("writer = {", at)
        assertTrue("no writer= block after $pathMarker", w >= 0)
        val slice = braceSliceAt(code, w + "writer = ".length)
        assertTrue("unbalanced writer block for $pathMarker", slice != null)
        return slice!!
    }

    /** The body of the member whose declaration starts with [marker]. */
    private fun memberBody(code: String, marker: String): String {
        val at = code.indexOf(marker)
        assertTrue("member not found: $marker", at >= 0)
        val open = code.indexOf('{', at)
        assertTrue("no body after $marker", open >= 0)
        val slice = braceSliceAt(code, open)
        assertTrue("unbalanced body for $marker", slice != null)
        return slice!!
    }

    /**
     * Can [token] be reached from [block] — directly, or through one same-file
     * helper the block calls? The refresh/lookup halves legitimately live in a
     * helper (`persistBinding` → `deliver`, `residentViewModel` → `peekViewModel`),
     * so the check follows ONE level of indirection instead of pinning the
     * helper's name.
     */
    private fun reaches(code: String, block: String, token: String): Boolean {
        if (block.contains(token)) return true
        val callee = token.removeSuffix("(")
        for (m in Regex("""\b([A-Za-z_][A-Za-z0-9_]*)\s*\(""").findAll(block)) {
            val name = m.groupValues[1]
            if (name == callee) continue
            val body = functionBody(code, name) ?: continue
            if (body.contains(token)) return true
        }
        return false
    }

    /**
     * The text of the `SessionConfigReadSource.ResidentViewModel -> …` arm of a
     * reader's `when` (up to the next arm), i.e. what the reader answers when a
     * live VM exists.
     */
    private fun residentArm(block: String): String {
        val marker = "SessionConfigReadSource.ResidentViewModel ->"
        val at = block.indexOf(marker)
        assertTrue("no resident arm in the reader: $block", at >= 0)
        val rest = block.substring(at + marker.length)
        val next = rest.indexOf("SessionConfigReadSource.")
        return if (next < 0) rest else rest.substring(0, next)
    }

    /**
     * True when the resident arm derives its value (a VM member read or a call
     * that ends in one) instead of answering a constant — `peek` followed by
     * `""` / `null` is the original defect with extra steps.
     */
    private fun residentBranchProducesValue(block: String): Boolean {
        val arm = residentArm(block)
        return arm.contains(".value") || arm.contains("!!.") || arm.contains('(')
    }

    /**
     * The parenthesised argument list that follows the call named [call]
     * (e.g. `"resolveBindingTarget("`), or null when it is not balanced.
     */
    private fun callArgs(code: String, call: String): String? {
        val at = code.indexOf(call)
        if (at < 0) return null
        return braceSliceAt(code, at + call.length - 1, '(', ')')
    }

    private fun functionBody(code: String, name: String): String? {        val at = code.indexOf("fun $name(")
        if (at < 0) return null
        val open = code.indexOf('{', at)
        if (open < 0) return null
        return braceSliceAt(code, open)
    }

    /** `src` from index [from] (must be a `{`) to its balanced closing brace. */
    private fun braceSliceAt(src: String, from: Int): String? =
        braceSliceAt(src, from, '{', '}')

    /** `src` from index [from] (must be [open]) to its balanced [close]. */
    private fun braceSliceAt(src: String, from: Int, open: Char, close: Char): String? {
        assertEquals("slice must start at $open", open, src[from])
        var depth = 0
        var i = from
        while (i < src.length) {
            when (src[i]) {
                open -> depth += 1
                close -> {
                    depth -= 1
                    if (depth == 0) return src.substring(from, i + 1)
                }
            }
            i += 1
        }
        return null
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

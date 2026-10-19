package com.rikkaminis.app.sandbox.offload

import java.io.File
import java.io.FileNotFoundException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * deep-audit-1033 ISSUE-03 (P1) — branch `fix/da1033-c-speech-cwd`.
 *
 * `android-speech transcribe --source <path>` used to drop `request.cwd` on the
 * floor and then refuse every relative path (`resolveSourcePath` began with
 * `if (trimmed.isEmpty() || !trimmed.startsWith("/")) return null`), so
 * `--source note.wav` from the shell's cwd *always* reported
 * `file_exists:false` / `resolved_host_path:""`. Speech was the last of the 7
 * path-taking offload handlers that had not adopted the shared relative-path
 * contract (the other 6 route through `resolveMaybeRelative` /
 * `GuestPathMapper.absoluteGuestPath`).
 *
 * ## Device split (why this file looks the way it does)
 *
 * The production wiring (`cmdTranscribe` / `resolveSourcePath`) is an instance
 * member of `SpeechOffloadHandler`, which needs an Android `Context` plus the
 * `PRootKernel` object — it cannot be *executed* on the JVM source set. So this
 * test is split, exactly like [com.rikkaminis.app.sandbox.SessionPathResolutionTest]:
 *
 *  1. **behaviour** — the two Android-free pieces the fix depends on are
 *     top-level `internal` functions in this package and are driven for real:
 *     [resolveMaybeRelative] (the single join implementation, living in
 *     `ConfigOffloadHandler.kt`) and [sourceIsMic] (hoisted to top-level in
 *     `SpeechOffloadHandler.kt` by this branch for exactly this reason).
 *  2. **wiring** — source-shape assertions that the handler really calls them,
 *     in the right order, with the joined value.
 *
 * Expectations are literals; nothing is derived from a production constant, so
 * a changed constant fails here instead of silently moving the expectation.
 *
 * The order arm is the one that carries the defect class: joining a *mic*
 * spelling onto the cwd produces `/var/minis/workspace/mic` (a file name), so
 * `sourceIsMic` MUST run before the path resolver. The privacy arm pins the
 * stronger half of the same edge: an explicit non-mic `--source` must return an
 * error payload *before* the microphone permission gate is reachable — a
 * `--source` the user typed must never be silently swapped for a live mic.
 */
class OffloadRelativePathTest {

    private companion object {
        private const val SPEECH =
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/SpeechOffloadHandler.kt"
        private const val CONFIG =
            "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ConfigOffloadHandler.kt"
    }

    // ── arm 1: the join itself (real single-source implementation) ──────────

    @Test
    fun `a relative source joins onto the guest cwd`() {
        assertEquals(
            "/var/minis/workspace/note.wav",
            resolveMaybeRelative("note.wav", "/var/minis/workspace"),
        )
        // Trailing separator on the cwd must not double up.
        assertEquals(
            "/var/minis/workspace/note.wav",
            resolveMaybeRelative("note.wav", "/var/minis/workspace/"),
        )
        // The `./` prefix an agent naturally writes keeps working (the guest
        // resolver normalises dot segments downstream).
        assertEquals(
            "/var/minis/workspace/./note.wav",
            resolveMaybeRelative("./note.wav", "/var/minis/workspace"),
        )
        // The joining implementation itself is the single source of the rule.
        assertTrue(
            "the join must stay single-source in ConfigOffloadHandler.kt",
            readRepoFile(CONFIG)
                .contains("internal fun resolveMaybeRelative(path: String, cwd: String?): String ="),
        )
    }

    @Test
    fun `an absolute source is passed through untouched`() {
        // Regression anchor: the fix must not re-target absolute paths.
        assertEquals(
            "/var/minis/workspace/note.wav",
            resolveMaybeRelative("/var/minis/workspace/note.wav", "/var/minis/workspace"),
        )
        assertEquals("/etc/passwd", resolveMaybeRelative("/etc/passwd", "/var/minis/workspace"))
        // A path carrying `..` stays as written here; the session resolver (not
        // this join) is what confines it — see the wiring arm.
        assertEquals(
            "/var/minis/../../etc/passwd",
            resolveMaybeRelative("/var/minis/../../etc/passwd", "/root"),
        )
    }

    @Test
    fun `a relative path is never guessed when there is no absolute cwd`() {
        // Fail-closed: no cwd (or a cwd that is itself relative) ⇒ the raw
        // token survives, so it cannot be silently joined onto something else.
        assertEquals("note.wav", resolveMaybeRelative("note.wav", null))
        assertEquals("note.wav", resolveMaybeRelative("note.wav", ""))
        assertEquals("note.wav", resolveMaybeRelative("note.wav", "relative/cwd"))
    }

    // ── arm 2: the mic classifier (real implementation, hoisted by this fix) ─

    @Test
    fun `every mic spelling is classified as the microphone`() {
        assertTrue(sourceIsMic(""))
        assertTrue(sourceIsMic("   "))
        assertTrue(sourceIsMic("mic"))
        assertTrue(sourceIsMic("MIC"))
        assertTrue(sourceIsMic("system-mic"))
        assertTrue(sourceIsMic("system_mic"))
        assertTrue(sourceIsMic("microphone"))
    }

    @Test
    fun `file sources are never classified as the microphone`() {
        assertFalse(sourceIsMic("note.wav"))
        assertFalse(sourceIsMic("./note.wav"))
        assertFalse(sourceIsMic("/var/minis/workspace/note.wav"))
        assertFalse(sourceIsMic("meeting.m4a"))
        // A file merely *named* mic.wav is still a file.
        assertFalse(sourceIsMic("mic.wav"))
    }

    @Test
    fun `the classifier must run before the join or a mic spelling becomes a file`() {
        // Documents why the ordering probe below is load-bearing: without the
        // classifier first, "mic" would be treated as a relative file name.
        assertEquals("/var/minis/workspace/mic", resolveMaybeRelative("mic", "/var/minis/workspace"))
    }

    // ── arm 3: production wiring (source shape) ────────────────────────────

    @Test
    fun `handle forwards request cwd into cmdTranscribe`() {
        assertDispatchCarriesCwd(codeOnly(readRepoFile(SPEECH)))
    }

    @Test
    fun `resolveSourcePath joins through resolveMaybeRelative and hands over the joined path`() {
        assertJoinWiring(declaration(codeOnly(readRepoFile(SPEECH)), "private fun resolveSourcePath("))
    }

    @Test
    fun `resolveSourcePath no longer re-implements the relative-vs-absolute decision`() {
        assertNoHardReject(declaration(codeOnly(readRepoFile(SPEECH)), "private fun resolveSourcePath("))
    }

    @Test
    fun `no bare host File fallback is reintroduced`() {
        val body = declaration(codeOnly(readRepoFile(SPEECH)), "private fun resolveSourcePath(")
        assertFalse("`File(trimmed)` host fallback is back:\n$body", body.contains("File(trimmed)"))
        assertFalse("an elvis `?: File(` fallback is back:\n$body", body.contains("?: File("))
    }

    @Test
    fun `only one resolvePathForSession call site exists in the speech handler`() {
        // The anchor owned by SessionPathResolutionTest:224 — this fix may only
        // change the argument, never add a second resolver call site (a second
        // one would also be a second, unguarded path into the session tree).
        val count = Regex("""resolvePathForSession\s*\(""")
            .findAll(codeOnly(readRepoFile(SPEECH))).count()
        assertEquals("resolvePathForSession( occurrences in $SPEECH", 1, count)
    }

    // ── arm 3b: the privacy edge ───────────────────────────────────────────

    @Test
    fun `an explicit non-mic source returns an error before the mic gate`() {
        assertSourceBranchPrecedesMicGate(declaration(codeOnly(readRepoFile(SPEECH)), "private fun cmdTranscribe("))
    }

    @Test
    fun `the mic path is still reachable when --source is omitted or mic`() {
        // Precision guard: the ordering probe must not be satisfiable by
        // deleting the mic path altogether.
        val code = codeOnly(readRepoFile(SPEECH))
        assertTrue(
            "the microphone path must still be wired after the source branch",
            Regex("""ensureMicPermission\(args\)\?\.let \{ return it \}""").containsMatchIn(code),
        )
        assertTrue(
            "the mic recognizer call must still exist",
            code.contains("recognize(lang, maxResults"),
        )
    }

    // ── arm 4: the probes have teeth (mutate the real source, expect red) ───

    @Test
    fun `negative control - dropping cwd from the dispatch reds the wiring probe`() {
        assertThrowsProbe {
            assertDispatchCarriesCwd(
                codeOnly(mutate("cmdTranscribe(args, request.sessionId, request.cwd)",
                    "cmdTranscribe(args, request.sessionId, null)")),
            )
        }
    }

    @Test
    fun `negative control - removing the join reds the join probe`() {
        assertThrowsProbe {
            assertJoinWiring(
                declaration(
                    codeOnly(mutate("val guestPath = resolveMaybeRelative(trimmed, cwd)", "val guestPath = trimmed")),
                    "private fun resolveSourcePath(",
                ),
            )
        }
    }

    @Test
    fun `negative control - restoring the hard relative reject reds the shape probe`() {
        assertThrowsProbe {
            assertNoHardReject(
                declaration(
                    codeOnly(mutate("if (trimmed.isEmpty()) return null",
                        "if (trimmed.isEmpty() || !trimmed.startsWith(\"/\")) return null")),
                    "private fun resolveSourcePath(",
                ),
            )
        }
    }

    @Test
    fun `negative control - moving the mic gate above the source branch reds the ordering probe`() {
        assertThrowsProbe {
            assertSourceBranchPrecedesMicGate(
                declaration(codeOnly(mutateOrdering()), "private fun cmdTranscribe("),
            )
        }
    }

    // ── arm 5: the extractor fails closed ─────────────────────────────────

    @Test
    fun `the body extractor refuses a missing declaration`() {
        assertThrowsProbe { declaration(codeOnly(readRepoFile(SPEECH)), "private fun noSuchFunction(") }
        assertNull(declarationOrNull(codeOnly("class X { fun a() {} }"), "private fun nope("))
    }

    // ── the probes (shared by the positive arms and their negative controls) ─

    private fun assertDispatchCarriesCwd(code: String) {
        val calls = Regex("""cmdTranscribe\s*\(([^()]*)\)""").findAll(code).toList()
        assertTrue("no cmdTranscribe occurrence found", calls.isNotEmpty())
        for (call in calls) {
            assertTrue(
                "cmdTranscribe(...) without a cwd in its arguments: ${call.value}",
                call.groupValues[1].contains("cwd"),
            )
        }
        assertTrue(
            "the dispatch must pass request.cwd, not a re-derived value",
            code.contains("cmdTranscribe(args, request.sessionId, request.cwd)"),
        )
    }

    private fun assertJoinWiring(body: String) {
        assertTrue("resolveSourcePath must declare a cwd parameter:\n$body", body.contains("cwd: String?"))
        assertTrue(
            "resolveSourcePath must delegate the join to resolveMaybeRelative (single implementation):\n$body",
            body.contains("resolveMaybeRelative("),
        )
        assertTrue(
            "the joined value must be what reaches the session resolver:\n$body",
            body.contains("resolvePathForSession(sessionId, guestPath, context)"),
        )
        assertFalse(
            "the raw trimmed argument must not reach the session resolver:\n$body",
            body.contains("resolvePathForSession(sessionId, trimmed, context)"),
        )
    }

    private fun assertNoHardReject(body: String) {
        assertFalse(
            "the hard relative-path reject is back (Before shape `!trimmed.startsWith(\"/\")`):\n$body",
            body.contains("startsWith(\"/\")"),
        )
        assertFalse(
            "a hand-written cwd join is back — the join is single-source:\n$body",
            body.contains("\"\$cwd"),
        )
        assertTrue("a blank source must stay rejected", body.contains("if (trimmed.isEmpty()) return null"))
    }

    private fun assertSourceBranchPrecedesMicGate(body: String) {
        val classified = body.indexOf("sourceIsMic(")
        val micGate = body.indexOf("ensureMicPermission(")
        assertTrue("cmdTranscribe never classifies --source:\n$body", classified >= 0)
        assertTrue("cmdTranscribe never reaches the mic permission gate:\n$body", micGate > classified)

        val branch = body.substring(classified, micGate)
        assertTrue(
            "the explicit-source branch must carry an error payload:\n$branch",
            branch.contains("\"error\""),
        )
        val returns = Regex("""return NativeOffloadResult\(\s*(\d+)""").findAll(branch).toList()
        assertNotNull(
            "the explicit-source branch must return a result before the mic gate:\n$branch",
            returns.lastOrNull(),
        )
        assertTrue(
            "an explicit --source must not fall through to the mic path as a success:\n$branch",
            returns.last().groupValues[1] != "0",
        )
    }

    // ── helpers ────────────────────────────────────────────────────────────

    /** [readRepoFile], with [from] replaced by [to]; fails closed if absent. */
    private fun mutate(from: String, to: String): String {
        val original = readRepoFile(SPEECH)
        val mutated = original.replace(from, to)
        if (mutated == original) error("mutation source not found: $from")
        return mutated
    }

    /**
     * Simulates a refactor that asks for microphone permission first and only
     * then classifies `--source`: the privacy edge reopens.
     */
    private fun mutateOrdering(): String {
        val original = readRepoFile(SPEECH)
        val mutated = original
            .replace(
                "        if (source != null && !sourceIsMic(source)) {",
                "        if (source != null && source.length > 900) {",
            )
            .replace(
                "        ensureMicPermission(args)?.let { return it }",
                "        ensureMicPermission(args)?.let { return it }\n" +
                    "        if (source != null && !sourceIsMic(source)) {",
            )
        if (mutated == original) error("ordering mutation did not apply")
        return mutated
    }

    /**
     * Full declaration (header + body) whose header contains [marker], brace-balanced
     * with string literals skipped (so `"${…}"` templates cannot unbalance the
     * counter). Returns null when the marker is absent — callers that must not
     * pass on an empty slice use [declaration].
     */
    private fun declarationOrNull(src: String, marker: String): String? {
        val idx = src.indexOf(marker)
        if (idx < 0) return null
        val open = src.indexOf("{", idx)
        if (open < 0) return null
        var depth = 0
        var inString = false
        var inRaw = false
        var i = open
        while (i < src.length) {
            val c = src[i]
            if (inRaw) {
                if (src.startsWith("\"\"\"", i)) { inRaw = false; i += 3; continue }
                i++
                continue
            }
            if (inString) {
                if (c == '\\') { i += 2; continue }
                if (c == '"') { inString = false }
                i++
                continue
            }
            if (src.startsWith("\"\"\"", i)) { inRaw = true; i += 3; continue }
            when (c) {
                '"' -> inString = true
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) return src.substring(idx, i + 1)
                }
            }
            i++
        }
        return null
    }

    /** [declarationOrNull], failing closed when the declaration is missing. */
    private fun declaration(src: String, marker: String): String =
        declarationOrNull(src, marker)
            ?: error("declaration '$marker' not found — fail-closed (the probe would pass on an empty slice)")

    /**
     * Template-aware comment stripper (same shape as `OffloadCliArgsTest`'s
     * `codeOnlyStrings`, which exists because `"file://"` — a `//` inside a
     * string — desyncs any naive line scanner). Nested `${…}` expressions
     * recurse back into code, so a quote inside a template expression cannot
     * end the literal early.
     */
    private fun codeOnly(src: String): String {
        val out = StringBuilder()
        val states = ArrayDeque<Int>()
        val depths = ArrayDeque<Int>()
        states.addLast(0); depths.addLast(0)
        var i = 0
        val n = src.length
        while (i < n) {
            when (states.last()) {
                0 -> {
                    if (src.startsWith("\"\"\"", i)) {
                        states.addLast(2); depths.addLast(0); out.append("\"\"\""); i += 3
                    } else {
                        val c = src[i]
                        when {
                            c == '"' -> { states.addLast(1); depths.addLast(0); out.append(c); i++ }
                            src.startsWith("//", i) -> { while (i < n && src[i] != '\n') i++ }
                            src.startsWith("/*", i) -> {
                                val end = src.indexOf("*/", i + 2)
                                i = if (end < 0) n else end + 2
                            }
                            c == '{' -> { depths.addLast(depths.removeLast() + 1); out.append(c); i++ }
                            c == '}' && depths.last() == 0 && states.size > 1 -> {
                                states.removeLast(); depths.removeLast(); out.append(c); i++
                            }
                            c == '}' -> { depths.addLast(depths.removeLast() - 1); out.append(c); i++ }
                            else -> { out.append(c); i++ }
                        }
                    }
                }
                1 -> {
                    val c = src[i]
                    when {
                        c == '\\' -> { out.append(c); if (i + 1 < n) out.append(src[i + 1]); i += 2 }
                        src.startsWith("\${", i) -> {
                            states.addLast(0); depths.addLast(0); out.append("\${"); i += 2
                        }
                        c == '"' || c == '\n' -> { states.removeLast(); depths.removeLast(); out.append(c); i++ }
                        else -> { out.append(c); i++ }
                    }
                }
                else -> {
                    if (src.startsWith("\"\"\"", i)) {
                        states.removeLast(); depths.removeLast(); out.append("\"\"\""); i += 3
                    } else { out.append(src[i]); i++ }
                }
            }
        }
        return out.toString()
    }

    /** Assert that [probe] throws — the negative controls' own tooth check. */
    private fun assertThrowsProbe(probe: () -> Unit) {
        var threw = false
        try {
            probe()
        } catch (_: Throwable) {
            threw = true
        }
        assertTrue("the probe was expected to fail but passed", threw)
    }

    private fun readRepoFile(relative: String): String {
        // Gradle (CI) sets user.dir to the module dir; a sandbox JVM run may
        // start anywhere under the repo — walk up instead of assuming a depth.
        var dir: File? = File(System.getProperty("user.dir") ?: ".")
        while (dir != null) {
            val f = File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw FileNotFoundException("$relative not found from user.dir=${System.getProperty("user.dir")}")
    }
}

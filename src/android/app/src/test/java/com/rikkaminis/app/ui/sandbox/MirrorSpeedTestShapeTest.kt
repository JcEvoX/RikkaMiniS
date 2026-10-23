package com.rikkaminis.app.ui.sandbox

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [dv15-B2 / deep-audit-1036 B-02] Source-shape probe for the single-category
 * speed test.
 *
 * Three defects share one root: the single-category path was a truncated copy of
 * [MirrorSettingsScreen]'s `runAllTests`, so
 *  1. it awaited every probe before the progress loop (`deferred.awaitAll()`),
 *     leaving the bar frozen at 0 until the last latency — on a dead mirror that
 *     is the whole timeout, which is what made the tap look ignored;
 *  2. it never ran the auto-select / persist / apply tail, so the mirror the
 *     user had just measured never reached the rootfs config (the unused
 *     `context` parameter is what made the truncation invisible);
 *  3. the "use custom mirror" switch was `enabled = selected != null`, so no
 *     mirror had to be picked before the switch could be touched at all.
 *
 * Comments are blanked out first: the fix's own comments quote the old
 * expressions, and prose must not satisfy a shape probe. One arm per property so
 * the mutation table can show exactly one arm flipping per mutation.
 */
class MirrorSpeedTestShapeTest {

    private val mirrorRel = "src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/MirrorSettingsScreen.kt"

    private fun sourceFileOrNull(rel: String): File? {
        val viaAndroid = "src/android/app/$rel"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel)
            if (File(d, viaAndroid).isFile) return File(d, viaAndroid)
            dir = d.parentFile
        }
        return null
    }

    private fun codeOnly(body: String): String = body.lines().joinToString("\n") { line ->
        val t = line.trimStart()
        if (t.startsWith("//") || t.startsWith("*") || t.startsWith("/*")) {
            " ".repeat(line.length)
        } else {
            line
        }
    }

    private fun code() = sourceFileOrNull(mirrorRel)?.let { codeOnly(it.readText()) } ?: ""

    /** Text of `header` through its brace-balanced end. */
    private fun bodyOf(src: String, header: String): String {
        val i = src.indexOf(header)
        if (i < 0) return ""
        val open = src.indexOf('{', i)
        if (open < 0) return ""
        var depth = 0
        var j = open
        while (j < src.length) {
            when (src[j]) {
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) return src.substring(i, j + 1)
                }
            }
            j++
        }
        return src.substring(i)
    }

    /** Text of `header` through its paren-balanced end (argument lists). */
    private fun argsOf(src: String, header: String): String {
        val i = src.indexOf(header)
        if (i < 0) return ""
        val open = src.indexOf('(', i)
        if (open < 0) return ""
        var depth = 0
        var j = open
        while (j < src.length) {
            when (src[j]) {
                '(' -> depth++
                ')' -> {
                    depth--
                    if (depth == 0) return src.substring(i, j + 1)
                }
            }
            j++
        }
        return src.substring(i)
    }

    // ── arms (empty list = the shape holds) ──────────────────────────────────

    /** Arm E1: progress advances per completion, not after all of them. */
    private fun progressViolations(): List<String> {
        val src = code()
        if (src.isEmpty()) return listOf("$mirrorRel is missing")
        val runTest = bodyOf(src, "fun runTest(context: Context, category: MirrorCategory)")
        if (runTest.isEmpty()) return listOf("runTest is gone")
        val out = mutableListOf<String>()
        if (!runTest.contains("testProgress =")) {
            out += "runTest never writes testProgress"
        }
        if (runTest.contains("awaitAll(")) {
            out += "runTest still awaits every probe before advancing progress"
        }
        if (!runTest.contains(".await()")) {
            out += "runTest does not collect completions one by one"
        }
        return out
    }

    /** Arm E2: the single-category test reaches the config, like runAllTests. */
    private fun flowViolations(): List<String> {
        val src = code()
        if (src.isEmpty()) return listOf("$mirrorRel is missing")
        val runTest = bodyOf(src, "fun runTest(context: Context, category: MirrorCategory)")
        if (runTest.isEmpty()) return listOf("runTest is gone")
        val out = mutableListOf<String>()
        if (!runTest.contains("autoSelectFastest(")) {
            out += "runTest does not reuse the autoSelectFastest single source"
        }
        if (!runTest.contains("persist(context, cat)")) {
            out += "runTest never persists the auto-selected mirror"
        }
        if (!runTest.contains("applyMirror(context, cat)")) {
            out += "runTest never applies the mirror to the rootfs config"
        }
        return out
    }

    /** Arm E3: the use-custom switch is not gated on a prior selection. */
    private fun switchViolations(): List<String> {
        val src = code()
        if (src.isEmpty()) return listOf("$mirrorRel is missing")
        val out = mutableListOf<String>()
        if (src.contains("useCustomEnabled")) {
            out += "the switch is gated on a prior selection again (useCustomEnabled)"
        }
        val switchArgs = argsOf(src, "Switch(")
        if (switchArgs.isEmpty()) return out + "the use-custom Switch is gone"
        if (!switchArgs.contains("enabled = true")) {
            out += "the use-custom switch is not unconditionally enabled"
        }
        return out
    }

    /** Arm E4 (preserved invariant): the reference flow is untouched. */
    private fun runAllTestsViolations(): List<String> {
        val src = code()
        if (src.isEmpty()) return listOf("$mirrorRel is missing")
        val all = bodyOf(src, "fun runAllTests(context: Context)")
        if (all.isEmpty()) return listOf("runAllTests is gone")
        val out = mutableListOf<String>()
        // [dv25-B1] The test body moved to the private `startAllTests` — the
        // public entry point may not call it through `runAllTests`, because
        // runAllTests is what cancels an in-flight auto-detection (and that is
        // the job the boot path would be running in). So the tail is asserted on
        // the shared body, and the entry point must reach it.
        val shared = bodyOf(src, "private fun startAllTests(context: Context)")
        if (shared.isNotEmpty() && !all.contains("startAllTests(context)")) {
            out += "runAllTests no longer starts the shared speed-test body"
        }
        val tail = all + shared
        if (!tail.contains("autoSelectFastest(")) {
            out += "runAllTests lost its auto-select tail"
        }
        if (!tail.contains("NonCancellable")) {
            out += "runAllTests lost the T10-L8 NonCancellable reset"
        }
        return out
    }

    /** Arm E5: first-boot detection owns a job the user entry points cancel. */
    private fun autoDetectOwnershipViolations(): List<String> {
        val src = code()
        if (src.isEmpty()) return listOf("$mirrorRel is missing")
        val detect = bodyOf(src, "fun autoDetectOnceIfNeeded(context: Context)")
        if (detect.isEmpty()) return listOf("autoDetectOnceIfNeeded is gone")
        val out = mutableListOf<String>()
        val slot = launchOwnershipSlot(detect)
        if (slot == null) {
            return out + "autoDetectOnceIfNeeded launches with no ownership slot (bare scope.launch)"
        }
        if (!Regex("""@Volatile\s+private\s+var\s+$slot\b""").containsMatchIn(src)) {
            out += "the auto-detect job slot `$slot` is not a @Volatile field"
        }
        for (entry in listOf(
            "fun runAllTests(context: Context)",
            "fun runTest(context: Context, category: MirrorCategory)",
        )) {
            val body = bodyOf(src, entry)
            if (body.isEmpty()) {
                out += "$entry is gone"
                continue
            }
            if (!body.contains("$slot?.cancel()")) {
                out += "$entry does not cancel the auto-detect job (`$slot?.cancel()`)"
            }
        }
        if (!detect.contains("$slot !== self")) {
            out += "the auto-detect settlement is not ownership-checked (`$slot !== self`)"
        }
        return out
    }

    /**
     * The field a launch's Job is published to, or null.
     *
     * Both spellings are accepted, as in the production file:
     *   `slot = scope.launch { … }`  and  `val job = scope.launch { … }; slot = job`
     * A local `val` alone is *not* an ownership slot — nothing else can cancel
     * it, which is exactly the shipped defect.
     */
    private fun launchOwnershipSlot(detect: String): String? {
        for (m in Regex("""(\w+)\s*=\s*scope\.launch""").findAll(detect)) {
            val lineStart = detect.lastIndexOf('\n', m.range.first) + 1
            val prefix = detect.substring(lineStart, m.range.first).trimEnd()
            val lhs = m.groupValues[1]
            if (prefix.endsWith("val") || prefix.endsWith("var")) {
                Regex("""(?m)^\s*(\w+)\s*=\s*""" + Regex.escape(lhs) + """\b""")
                    .find(detect, m.range.last + 1)
                    ?.let { return it.groupValues[1] }
                continue
            }
            return lhs
        }
        return null
    }

    /** Arm E6: the settlement does not walk every category unguarded. */
    private fun autoDetectGuardViolations(): List<String> {
        val src = code()
        if (src.isEmpty()) return listOf("$mirrorRel is missing")
        val detect = bodyOf(src, "fun autoDetectOnceIfNeeded(context: Context)")
        if (detect.isEmpty()) return listOf("autoDetectOnceIfNeeded is gone")
        val out = mutableListOf<String>()
        if (detect.contains("for (cat in MirrorCategory.entries)")) {
            out += "the settlement walks every category unconditionally again"
        }
        if (!detect.contains("decideAutoApply(")) {
            out += "the settlement does not use the user-selection-aware decision"
        }
        if (!detect.contains("selectedMirrorId.keys")) {
            out += "the settlement does not derive the already-selected set from selectedMirrorId"
        }
        return out
    }

    private fun assertHolds(label: String, violations: List<String>) {
        assertTrue("[$label] the shape must hold, got: $violations", violations.isEmpty())
    }

    // ── the live source ──────────────────────────────────────────────────────

    @Test
    fun `arm E1 - progress advances as each probe settles`() {
        assertHolds("E1", progressViolations())
    }

    @Test
    fun `arm E2 - the single-category test selects, persists and applies`() {
        assertHolds("E2", flowViolations())
    }

    @Test
    fun `arm E3 - the use-custom switch works before a mirror is picked`() {
        assertHolds("E3", switchViolations())
    }

    @Test
    fun `arm E4 - runAllTests keeps its auto-select tail and cancellation reset`() {
        assertHolds("E4", runAllTestsViolations())
    }

    // ── [dv25-B1 / deep-audit-1045 B-01] first-boot detection ownership ──────

    @Test
    fun `arm E5 - auto detection owns a job the user entry points cancel`() {
        assertHolds("E5", autoDetectOwnershipViolations())
    }

    @Test
    fun `arm E6 - auto detection settlement keeps the user-selection guard`() {
        assertHolds("E6", autoDetectGuardViolations())
    }
}

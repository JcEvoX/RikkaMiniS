package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [dv14-A1] ISSUE-02 (P1) — `android-notification send|schedule --after <值>`
 * with a value the parser cannot read (`5m`, `30s`, a typo) used to be delivered
 * IMMEDIATELY, with exit 0 and `{"scheduled":"immediate"}`.
 *
 * The cause was a single argument read: the branch was gated on
 * `args.getInt("after")`, and [OffloadArgs.getInt] returns `null` both when the
 * option is ABSENT and when it is present but unparseable. `--after 5m` was
 * therefore erased into "no trigger" and fell through to the immediate path,
 * while `--at tomorrow` (read raw, then parsed with a `?: return exit 2` guard)
 * was refused correctly. The `.s/m/h` suffix habit is what an agent carries
 * over from its sibling `android-alarm timer --duration 5m` ([AlarmOffloadHandler]
 * parses a duration WITH a unit, so the same spelling looks legal one tool over).
 *
 * [NotificationOffloadHandler.sendDisposition] is the fix's decision point: it
 * reads the RAW string first, so "spelled but unreadable" is refused instead of
 * guessed, and it routes an explicit `schedule` with neither flag into
 * [schedulingPath], which turns `scheduleDeferred`'s own
 * "--after or --at is required" refusal from dead code into a live guard.
 *
 * Every expected value below is a literal; each probe has a negative control
 * that mutates a copy of the real source and asserts the very same check fails.
 */
class NotificationOffloadHandlerTest {

    private val handlerFile =
        "src/main/java/com/rikkaminis/app/sandbox/offload/NotificationOffloadHandler.kt"

    // ── the decision, as behaviour ──────────────────────────────────────────

    private fun plan(sub: String, after: String? = null, at: String? = null) =
        NotificationOffloadHandler.sendDisposition(sub, after, at)

    /** `send` with no trigger keeps the documented immediate delivery. */
    @Test
    fun `plain send stays immediate`() {
        assertEquals(
            SendDisposition.Immediate,
            plan("send"),
        )
    }

    /** A readable `--after` defers — the ordinary case must not regress. */
    @Test
    fun `a numeric --after defers`() {
        val d = plan("send", after = "30") as SendDisposition.Deferred
        assertEquals(30, d.secs)
        assertNull(d.at)

        val scheduled = plan("schedule", after = "600") as SendDisposition.Deferred
        assertEquals(600, scheduled.secs)
    }

    /** `--at` alone defers with the raw string handed on to the ISO parser. */
    @Test
    fun `an --at value defers with the raw string`() {
        val d = plan("send", at = "2026-01-01T09:00:00Z") as SendDisposition.Deferred
        assertNull(d.secs)
        assertEquals("2026-01-01T09:00:00Z", d.at)
    }

    /**
     * The regression itself: a unit-suffixed or otherwise unreadable `--after`
     * must be REFUSED, never silently delivered immediately.
     */
    @Test
    fun `an unreadable --after is refused instead of delivered immediately`() {
        for (raw in listOf("5m", "30s", "1h", "10 minutes", "abc", "30.5", "1,5")) {
            val disposition = plan("send", after = raw)
            assertTrue(
                "'--after $raw' must not fall through to the immediate path",
                disposition is SendDisposition.Invalid,
            )
            val message = (disposition as SendDisposition.Invalid).message
            assertTrue("the refusal must name the offending value: $message", message.contains("'$raw'"))
        }
    }

    /** An unreadable `--after` outranks a valid `--at` (refuse, do not guess). */
    @Test
    fun `an unreadable --after is refused even next to a valid --at`() {
        assertTrue(plan("send", after = "5m", at = "2026-01-01T09:00:00Z")
            is SendDisposition.Invalid)
    }

    /** `--after 0` / `--after=-5` reach scheduleDeferred, which refuses them. */
    @Test
    fun `zero and negative values still defer so the positive-only guard runs`() {
        assertEquals(
            0,
            (plan("send", after = "0") as SendDisposition.Deferred).secs,
        )
        assertEquals(
            -5,
            (plan("send", after = "-5") as SendDisposition.Deferred).secs,
        )
    }

    /**
     * `schedule` with neither flag must reach the deferred path, where the
     * "--after or --at is required" refusal lives (it was unreachable before:
     * the branch was gated on the parsed value it was supposed to guard).
     */
    @Test
    fun `an explicit schedule without a trigger defers so the guard can refuse it`() {
        val d = plan("schedule") as SendDisposition.Deferred
        assertNull(d.secs)
        assertNull(d.at)
    }

    // ── the wiring, as source shape ─────────────────────────────────────────

    private fun assertDeferDecisionUsesTheRawString(src: String) {
        val code = codeOnly(src)
        assertTrue(
            "handleSend must decide through the pure disposition helper",
            code.contains("sendDisposition("),
        )
        assertTrue(
            "the raw --after string must be read, not the parsed Int",
            code.contains("args.get(\"after\")"),
        )
        assertFalse(
            "the parsed read erases \"spelled but unreadable\" into \"not spelled\"",
            code.contains("getInt(\"after\")"),
        )
        assertTrue(
            "the decision helper must exist (pure, so it stays testable off-device)",
            code.contains(
                "internal fun sendDisposition(sub: String, rawAfter: String?, rawAt: String?): SendDisposition",
            ),
        )
        assertTrue(
            "the deferred path must still be a post-condition, not a silent drop",
            code.contains("is SendDisposition.Invalid -> return NativeOffloadResult(2, plan.message)"),
        )
        assertTrue(
            "the refusal must keep the exit-2 contract for an unusable delay",
            code.contains("android-notification: --after or --at is required\\n"),
        )
        assertTrue(
            "the immediate path must be reached only through the Immediate branch",
            code.contains("SendDisposition.Immediate -> Unit"),
        )
    }

    @Test
    fun `the deferred decision reads the raw --after string`() {
        assertDeferDecisionUsesTheRawString(source(handlerFile))
        // the dispatch really hands the subcommand in, so `schedule` is
        // distinguishable from `send` inside handleSend
        assertTrue(source(handlerFile).contains("\"send\", \"schedule\" -> handleSend(sub, args)"))
    }

    @Test
    fun `negative control - the parsed --after read comes back and the probe fails`() {
        val original = source(handlerFile)
        val mutated = original.replace(
            "val plan = sendDisposition(",
            "val plan = @Suppress(\"unused\") dummy(args.getInt(\"after\"),",
        )
        assertTrue("the disposition call must exist to be mutated", mutated != original)
        assertProbeFails { assertDeferDecisionUsesTheRawString(mutated) }
    }

    @Test
    fun `negative control - removing the refusal guard fails the probe`() {
        val original = source(handlerFile)
        val mutated = original.replace(" --after or --at is required\\n", " nothing to schedule\\n")
        assertTrue("the required-argument guard must exist to be removed", mutated != original)
        assertProbeFails { assertDeferDecisionUsesTheRawString(mutated) }
    }

    private fun assertProbeFails(probe: () -> Unit) {
        var failed = false
        try {
            probe()
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the probe must fail once the source is mutated", failed)
    }

    // ── helpers (same shape as OffloadCliArgsTest) ──────────────────────────

    private fun codeOnly(src: String): String = src
        .replace(Regex("(?s)/\\*.*?\\*/"), "")
        .replace(Regex("(?m)//.*$"), "")

    private fun source(rel: String): String {
        val viaAndroid = "src/android/app/$rel"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel).readText()
            if (File(d, viaAndroid).isFile) return File(d, viaAndroid).readText()
            dir = d.parentFile
        }
        error("'$rel' not found from ${File(".").absoluteFile}")
    }
}

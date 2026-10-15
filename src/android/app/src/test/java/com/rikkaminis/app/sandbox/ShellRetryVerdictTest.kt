package com.rikkaminis.app.sandbox

import com.rikkaminis.app.agent.runtime.RetrySafety
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1015 ARCH-01] JVM tests for the side-effect-aware shell retry
 * decision ([internalDecideShellRetryVerdict]) and its wiring contract.
 *
 * The decision under test answers BOTH questions at once:
 *  - may this command be re-run on a rebuilt shell? ([ShellRetryVerdict.retry])
 *  - may its result be believed? ([ShellRetryVerdict.outcomeKnown])
 *
 * Arms (per the fix task):
 *  1. UNKNOWN + dispatched + exitCode == -1  → NO replay, outcome unknown;
 *  2. READ_ONLY + same shape                → replay (the control arm, so the
 *     fix cannot degenerate into "never retry anything");
 *  3. pre-flight death (never dispatched)   → replay, outcome known;
 *  4. differential closure: legacy vs new decision over the same input grid,
 *     with the difference set pinned as literals.
 *
 * All expectations are literal — none is derived from the production constants
 * (a self-referential expectation would stay green through a mutation).
 */
class ShellRetryVerdictTest {

    // ── arm 1: unknown safety, dispatched, result lost → no replay ────────

    @Test
    fun `unknown safety dispatched shell death is not replayed`() {
        val v = verdict(exitCode = -1, alive = false, attempt = 1)
        assertFalse("must not replay an UNKNOWN command whose result was lost", v.retry)
        assertFalse("result must be reported as unknown", v.outcomeKnown)
    }

    @Test
    fun `unknown safety dispatched shell death with live shell is not replayed`() {
        // exitCode == -1 with a live shell: the readLoop saw the process exit
        // (generation recycle) — still a dispatched command with a lost result.
        val v = verdict(exitCode = -1, alive = true, attempt = 1)
        assertFalse(v.retry)
        assertFalse(v.outcomeKnown)
    }

    @Test
    fun `unknown safety lost result with non-zero exit is not replayed`() {
        // exitCode 130 (SIGINT) with a dead shell: the command ran, the shell
        // died before returning — RESULT_LOST.
        val v = verdict(exitCode = 130, alive = false, attempt = 1)
        assertFalse(v.retry)
        assertFalse(v.outcomeKnown)
    }

    // ── arm 2: read-only control — replay still allowed ───────────────────

    @Test
    fun `read-only dispatched shell death is still replayed`() {
        val v = verdict(exitCode = -1, alive = false, attempt = 1, safety = RetrySafety.READ_ONLY)
        assertTrue("a read-only command must keep its replay ability", v.retry)
        assertTrue(v.outcomeKnown)
    }

    @Test
    fun `read-only control respects the retry budget`() {
        val v = verdict(exitCode = -1, alive = false, attempt = 2, safety = RetrySafety.READ_ONLY)
        assertFalse("budget exhausted → no replay", v.retry)
        assertTrue("a read-only command can be reported as a known failure", v.outcomeKnown)
    }

    // ── arm 3: pre-flight death → replay is a FIRST execution ─────────────

    @Test
    fun `pre-flight death is replayed and the outcome stays known`() {
        // The shell was already unusable before the write: executeCommand bails
        // out with "[Shell not running]" and nothing ran. Re-issuing the command
        // is its first execution, so the zero-side-effect case must keep the
        // legacy rebuild-and-run ability.
        val v = verdict(exitCode = -1, alive = false, attempt = 1, commandDispatched = false)
        assertTrue("a never-dispatched command must keep its replay ability", v.retry)
        assertTrue(v.outcomeKnown)
    }

    @Test
    fun `pre-flight death respects the retry budget too`() {
        val v = verdict(exitCode = -1, alive = false, attempt = 2, commandDispatched = false)
        assertFalse(v.retry)
        assertTrue(v.outcomeKnown)
    }

    // ── arm 4: differential closure — legacy vs new on the same grid ──────

    /**
     * Same-inputs judgment table. Every row is evaluated by BOTH the legacy
     * [internalShouldRetryCommand] and the new verdict; the rows where they
     * differ are pinned below as literals.
     *
     * The difference set is exactly "dispatched, first attempt, shell died or
     * result lost" — the blind-replay shapes this fix removes. There is no
     * reverse difference (the policy never became MORE eager to replay).
     */
    @Test
    fun `differential closure legacy vs new differ exactly on dispatched lost results`() {
        val exitCodes = listOf(-1, 0, 1, 124, 125, 130)
        val aliveOptions = listOf(true, false)
        val attempts = listOf(1, 2)

        val divergences = mutableListOf<String>()
        val reverse = mutableListOf<String>()
        var rows = 0
        for (exitCode in exitCodes) {
            for (alive in aliveOptions) {
                for (attempt in attempts) {
                    rows++
                    val legacy = internalShouldRetryCommand(exitCode, alive, attempt)
                    val now = verdict(exitCode = exitCode, alive = alive, attempt = attempt).retry
                    if (legacy == now) continue
                    val row = "${exitCode}/${alive}/${attempt}"
                    if (now) reverse.add(row) else divergences.add(row)
                }
            }
        }

        assertEquals("grid size", 24, rows)
        assertEquals(
            "the new policy may only REMOVE replays, never add one",
            emptyList<String>(),
            reverse,
        )
        assertEquals(
            "legacy would blind-replay these; the new policy withholds the replay",
            listOf(
                "-1/true/1",   // readLoop saw the exit, shell possibly alive
                "-1/false/1",  // the classic silent_kill shape
                "0/false/1",   // exit 0 but the shell died before returning
                "1/false/1",   // non-zero exit, shell gone — result lost
                "130/false/1", // SIGINT exit, shell gone — result lost
            ),
            divergences,
        )
    }

    @Test
    fun `differential closure pre-flight rows keep the legacy verdict`() {
        // Red line (1) of the fix task: the "shell died BEFORE the command was
        // written" case must not lose its replay ability. Mechanically: with
        // commandDispatched = false the new verdict agrees with the legacy
        // predicate on every point of the grid that can carry a pre-flight
        // death (exitCode -1 / dead shell), so no ability was traded away.
        val rows = mutableListOf<String>()
        for (attempt in listOf(1, 2, 3)) {
            val legacy = internalShouldRetryCommand(-1, shellAlive = false, attempt = attempt)
            val now = verdict(exitCode = -1, alive = false, attempt = attempt, commandDispatched = false).retry
            assertEquals("pre-flight attempt=$attempt", legacy, now)
            rows.add("$attempt=$legacy")
        }
        assertEquals(listOf("1=true", "2=false", "3=false"), rows)
    }

    // ── topologies that must not change behaviour ─────────────────────────

    @Test
    fun `timeout is never replayed and reports an unknown outcome`() {
        for (alive in listOf(true, false)) {
            val v = verdict(exitCode = 124, alive = alive, attempt = 1)
            assertFalse("124 is never replayed", v.retry)
            assertFalse("a timed-out command may have partially run", v.outcomeKnown)
        }
    }

    @Test
    fun `stall kill is never replayed`() {
        assertFalse(verdict(exitCode = 125, alive = false, attempt = 1).retry)
        assertTrue(
            "a stall-killed command that produced its own diagnostic",
            verdict(exitCode = 125, alive = true, attempt = 1).outcomeKnown,
        )
    }

    @Test
    fun `non-zero exit with a live shell is a known failure`() {
        val v = verdict(exitCode = 1, alive = true, attempt = 1)
        assertFalse(v.retry)
        assertTrue("the command ran and failed — the result is known", v.outcomeKnown)
    }

    @Test
    fun `successful command needs no retry and its outcome is known`() {
        val v = verdict(exitCode = 0, alive = true, attempt = 1)
        assertFalse(v.retry)
        assertTrue(v.outcomeKnown)
    }

    @Test
    fun `truncated output of a dispatched command is not trusted`() {
        val v = verdict(exitCode = 0, alive = true, attempt = 1, truncated = true)
        assertFalse("output was cut — never replayed", v.retry)
        assertFalse("partial output must not pass as complete", v.outcomeKnown)
    }

    @Test
    fun `memory-recycled shell is never replayed and its outcome is unknown`() {
        // F-06 path: our own memory guard killed the shell mid-command.
        val v = verdict(exitCode = -1, alive = false, attempt = 1, memoryRecycled = true)
        assertFalse(v.retry)
        assertFalse(v.outcomeKnown)
    }

    @Test
    fun `memory recycle wins over a pre-flight claim`() {
        // Defensive: the in-flight monitor can only fire once a command is
        // dispatched, so this combination is unreachable in production — the
        // precedence is pinned so a future reordering cannot silently turn a
        // memory kill into a replay.
        val v = verdict(
            exitCode = -1,
            alive = false,
            attempt = 1,
            memoryRecycled = true,
            commandDispatched = false,
        )
        assertFalse(v.retry)
    }

    // ── the registry constant the wiring reads ────────────────────────────

    @Test
    fun `shell_execute is registered as unknown safety`() {
        assertEquals("shell_execute", SHELL_EXECUTE_TOOL_NAME)
        assertEquals(
            "the wiring reads this registry entry; UNKNOWN is what makes a " +
                "dispatched command unreplayable",
            RetrySafety.UNKNOWN,
            com.rikkaminis.app.agent.runtime.ToolRetrySafetyRegistry
                .defaultSafetyFor(SHELL_EXECUTE_TOOL_NAME),
        )
    }

    private fun verdict(
        exitCode: Int,
        alive: Boolean,
        attempt: Int,
        truncated: Boolean = false,
        safety: RetrySafety = RetrySafety.UNKNOWN,
        memoryRecycled: Boolean = false,
        commandDispatched: Boolean = true,
    ): ShellRetryVerdict = internalDecideShellRetryVerdict(
        exitCode = exitCode,
        shellAlive = alive,
        truncated = truncated,
        attempt = attempt,
        safety = safety,
        maxRetries = 2,
        memoryRecycled = memoryRecycled,
        commandDispatched = commandDispatched,
    )
}

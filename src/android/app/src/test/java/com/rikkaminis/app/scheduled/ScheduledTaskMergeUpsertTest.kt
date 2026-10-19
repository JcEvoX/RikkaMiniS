package com.rikkaminis.app.scheduled

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1030 ISSUE-08] JVM tests for [ScheduledTaskTransitions.mergeUpsertTask]
 * — what an editor save is allowed to change.
 *
 * Bug shape being pinned: the editor handed its WHOLE record to
 * `ScheduledTasksStore.upsertTask`, which wrote it over the stored one. Run state
 * (`lastRunDate` / `lastResult` / `failStreak` / `suspendedByFuse`) and `enabled`
 * ride in the same record as the spec the user was editing, and the editor's copy
 * is a snapshot from when the screen opened — so any run that settled during the
 * edit window (up to the full 60 s repair-prompt budget, plus the time the user
 * spends typing) was silently erased by the save.
 *
 * Editorial intent is "change the spec"; run state is owned by the runner and the
 * store. Every expectation below is a literal, and the same-day-gate test carries
 * its own negative control (the pre-fix whole-record write) so the arm cannot pass
 * by accident.
 */
class ScheduledTaskMergeUpsertTest {

    // ─── fixtures ────────────────────────────────────────────────────────────

    /** The record as the store holds it: run state from earlier today. */
    private fun stored() = ScheduledTask(
        id = "t-1",
        title = "old title",
        prompt = "old prompt",
        windowStart = "07:00",
        windowEnd = "09:00",
        enabled = true,
        maxTurns = 5,
        deadlineSec = 60,
        maxEstimatedTokens = 1_000L,
        lastRunDate = "2026-10-19",
        lastResult = ScheduledTask.RESULT_ERROR,
        failStreak = 2,
        suspendedByFuse = false,
    )

    /** What the editor submits: the form's spec, run state at its defaults. */
    private fun formFor(id: String = "t-1") = ScheduledTask(
        id = id,
        title = "new title",
        prompt = "new prompt",
        windowStart = "08:00",
        windowEnd = "10:00",
        enabled = false,
        maxTurns = 9,
        deadlineSec = 120,
        maxEstimatedTokens = 9_000L,
        lastRunDate = null,
        lastResult = null,
        failStreak = 0,
        suspendedByFuse = false,
    )

    private fun fileWith(vararg tasks: ScheduledTask) = ScheduledTasksFile(tasks = tasks.toList())

    // ─── the merge ───────────────────────────────────────────────────────────

    @Test
    fun `a save applies the form's spec and nothing else`() {
        val merged = ScheduledTaskTransitions
            .mergeUpsertTask(fileWith(stored()), formFor())
            .tasks.single()

        assertEquals("new title", merged.title)
        assertEquals("new prompt", merged.prompt)
        assertEquals("08:00", merged.windowStart)
        assertEquals("10:00", merged.windowEnd)
        assertEquals(9, merged.maxTurns)
        assertEquals(120, merged.deadlineSec)
        assertEquals(9_000L, merged.maxEstimatedTokens)

        // Run state + enablement stay exactly as the store held them.
        assertEquals("2026-10-19", merged.lastRunDate)
        assertEquals(ScheduledTask.RESULT_ERROR, merged.lastResult)
        assertEquals(2, merged.failStreak)
        assertFalse(merged.suspendedByFuse)
        assertTrue(merged.enabled)
    }

    @Test
    fun `a save cannot clear a fuse that tripped while the editor was open`() {
        val tripped = stored().copy(enabled = false, suspendedByFuse = true, failStreak = 3)
        // The editor's snapshot predates the trip, so it still says "enabled".
        val staleForm = formFor().copy(enabled = true)

        val merged = ScheduledTaskTransitions
            .mergeUpsertTask(fileWith(tripped), staleForm)
            .tasks.single()

        assertTrue("the trip must survive the save", merged.suspendedByFuse)
        assertFalse("a save must not silently re-enable a fused task", merged.enabled)
        assertEquals(3, merged.failStreak)
    }

    @Test
    fun `a save cannot clear the enablement a run just settled`() {
        val disabled = stored().copy(enabled = false)
        val merged = ScheduledTaskTransitions
            .mergeUpsertTask(fileWith(disabled), formFor())
            .tasks.single()

        assertFalse(merged.enabled)
    }

    @Test
    fun `a save cannot re-arm the same-day gate`() {
        val merged = ScheduledTaskTransitions
            .mergeUpsertTask(fileWith(stored()), formFor())
            .tasks.single()

        assertEquals("2026-10-19", merged.lastRunDate)
        // The observable consequence: still "already ran today" at 08:00, i.e.
        // inside the window the user just saved.
        assertEquals(
            listOf(ScheduledTaskPolicy.PassVerdict.Skip("t-1", ScheduledTaskPolicy.SkipReason.ALREADY_RAN)),
            ScheduledTaskPolicy.evaluate(
                nowDate = "2026-10-19",
                nowMinuteOfDay = 8 * 60,
                tasks = listOf(merged),
                runningTaskId = null,
            ),
        )
    }

    @Test
    fun `the negative control - a whole-record write re-arms the same-day gate`() {
        // The editor's snapshot = what the store used to write verbatim: when the
        // screen opened the task was enabled and had not run yet today.
        val staleForm = formFor().copy(enabled = true)
        assertNotEquals(stored(), staleForm)
        assertEquals(
            listOf(ScheduledTaskPolicy.PassVerdict.Fire("t-1")),
            ScheduledTaskPolicy.evaluate(
                nowDate = "2026-10-19",
                nowMinuteOfDay = 8 * 60,
                tasks = listOf(staleForm),
                runningTaskId = null,
            ),
        )
        // The merge keeps both facts the run settled while the editor was open.
        val merged = ScheduledTaskTransitions
            .mergeUpsertTask(fileWith(stored()), staleForm)
            .tasks.single()
        assertEquals(
            listOf(ScheduledTaskPolicy.PassVerdict.Skip("t-1", ScheduledTaskPolicy.SkipReason.ALREADY_RAN)),
            ScheduledTaskPolicy.evaluate(
                nowDate = "2026-10-19",
                nowMinuteOfDay = 8 * 60,
                tasks = listOf(merged),
                runningTaskId = null,
            ),
        )
    }

    // ─── new tasks and neighbours ────────────────────────────────────────────

    @Test
    fun `an unknown id is appended as the form gave it`() {
        val next = ScheduledTaskTransitions.mergeUpsertTask(fileWith(stored()), formFor(id = "t-2"))

        assertEquals(listOf("t-1", "t-2"), next.tasks.map { it.id })
        val added = next.tasks[1]
        assertEquals("new prompt", added.prompt)
        assertFalse("brand-new tasks enter disabled (brief §5.4)", added.enabled)
        assertEquals(null, added.lastRunDate)
        assertEquals(0, added.failStreak)
    }

    @Test
    fun `other tasks are left untouched and in order`() {
        val other = stored().copy(id = "t-0", title = "untouched", lastRunDate = "2026-01-01")
        val next = ScheduledTaskTransitions.mergeUpsertTask(fileWith(other, stored()), formFor())

        assertEquals(listOf("t-0", "t-1"), next.tasks.map { it.id })
        assertEquals(other, next.tasks[0])
        assertEquals("new title", next.tasks[1].title)
        assertEquals("2026-10-19", next.tasks[1].lastRunDate)
    }

    @Test
    fun `an empty file gains the task`() {
        val next = ScheduledTaskTransitions.mergeUpsertTask(ScheduledTasksFile(), formFor())
        assertEquals(1, next.tasks.size)
        assertEquals("t-1", next.tasks.single().id)
    }

    @Test
    fun `the run marker is not disturbed by a save`() {
        val marker = ScheduledRunMarker(taskId = "t-1", sessionId = "s-1", startedAt = 42L)
        val next = ScheduledTaskTransitions.mergeUpsertTask(
            ScheduledTasksFile(running = marker, tasks = listOf(stored())),
            formFor(),
        )
        assertEquals(marker, next.running)
    }
}

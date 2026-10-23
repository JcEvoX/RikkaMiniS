package com.rikkaminis.app.scheduled

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1046 S-11] JVM tests for the store's change ticker and the two
 * decisions behind it — all android-free, so they run on the plain JVM.
 *
 * Bug shape being pinned: `ScheduledTasksStore` is read-once (`load` only), so
 * a screen that rendered once kept rendering that document. The list never saw
 * a background run settle or the fuse trip; the editor never saw its target
 * disappear, and Save then appended the vanished record straight back
 * (`mergeUpsertTask`: `idx < 0 → append`) with the run state the form had
 * snapshotted.
 *
 * Every expectation below is a literal (never derived from the code under test),
 * so the mutation arms flip these assertions instead of passing silently.
 */
class ScheduledTasksStoreRevisionTest {

    // ─── helpers ─────────────────────────────────────────────────────────────

    private fun task(
        id: String,
        title: String = "title-$id",
        prompt: String = "prompt-$id",
        windowStart: String = "07:00",
        windowEnd: String = "23:00",
        enabled: Boolean = true,
        lastRunDate: String? = null,
        lastResult: String? = null,
        failStreak: Int = 0,
        suspendedByFuse: Boolean = false,
    ) = ScheduledTask(
        id = id,
        title = title,
        prompt = prompt,
        windowStart = windowStart,
        windowEnd = windowEnd,
        enabled = enabled,
        lastRunDate = lastRunDate,
        lastResult = lastResult,
        failStreak = failStreak,
        suspendedByFuse = suspendedByFuse,
    )

    private fun doc(vararg tasks: ScheduledTask) = ScheduledTasksFile(tasks = tasks.toList())

    // ─── the ticker ──────────────────────────────────────────────────────────

    @Test
    fun `a landed write bumps the revision by exactly one`() {
        assertEquals(5L, ScheduledTaskRevisionPolicy.nextRevision(4L, writeSucceeded = true))
    }

    @Test
    fun `a failed write leaves the revision untouched`() {
        // The atomic write threw: the file is exactly as it was, so telling
        // every open screen to re-read would re-render stale data for nothing.
        assertEquals(4L, ScheduledTaskRevisionPolicy.nextRevision(4L, writeSucceeded = false))
    }

    // ─── "my target is gone" ─────────────────────────────────────────────────

    @Test
    fun `a change after the initial load with the target gone marks it missing`() {
        assertTrue(ScheduledTaskRevisionPolicy.shouldMarkMissing(loadedOnce = true, stillExists = false))
    }

    @Test
    fun `a change arriving before the initial load decides nothing`() {
        // A revision that moved while this screen was still composing must not
        // pre-empt the load effect with an empty form.
        assertFalse(ScheduledTaskRevisionPolicy.shouldMarkMissing(loadedOnce = false, stillExists = false))
    }

    @Test
    fun `an existing target is never marked missing`() {
        assertFalse(ScheduledTaskRevisionPolicy.shouldMarkMissing(loadedOnce = true, stillExists = true))
        assertFalse(ScheduledTaskRevisionPolicy.shouldMarkMissing(loadedOnce = false, stillExists = true))
    }

    // ─── the save: insert is an explicit decision ────────────────────────────

    @Test
    fun `an edit whose target vanished is refused and the document is untouched`() {
        val deleted = doc(task("t-2"))
        val refused = ScheduledTaskTransitions.mergeUpsertTask(
            deleted,
            task("t-1", enabled = true, lastRunDate = "2026-10-22", lastResult = "ok"),
            allowInsert = false,
        )
        // Same instance: `ScheduledTasksStore.mutate` writes only when
        // `next !== current`, so the refused save cannot even bump the revision.
        assertSame(deleted, refused)
        assertTrue(refused.tasks.none { it.id == "t-1" })
        assertEquals(listOf("t-2"), refused.tasks.map { it.id })
    }

    @Test
    fun `a brand-new task still inserts through the default`() {
        val next = ScheduledTaskTransitions.mergeUpsertTask(doc(), task("t-9"))
        assertEquals(listOf("t-9"), next.tasks.map { it.id })
    }

    @Test
    fun `an explicit allow-insert still inserts`() {
        val next = ScheduledTaskTransitions.mergeUpsertTask(doc(), task("t-9"), allowInsert = true)
        assertEquals(listOf("t-9"), next.tasks.map { it.id })
    }

    @Test
    fun `an existing record still merges the spec and keeps its run state`() {
        // The 1030-08 contract this parameter sits next to: the spec follows the
        // form, the run half stays with the store.
        val stored = task("t-1", enabled = false, lastRunDate = "2026-10-20", lastResult = "error", failStreak = 2)
        val form = task("t-1", title = "new title", prompt = "new prompt", enabled = true, lastRunDate = null)
        val merged = ScheduledTaskTransitions.mergeUpsertTask(doc(stored), form, allowInsert = false)
            .tasks.single { it.id == "t-1" }
        assertEquals("new title", merged.title)
        assertEquals("new prompt", merged.prompt)
        assertFalse(merged.enabled)
        assertEquals("2026-10-20", merged.lastRunDate)
        assertEquals(2, merged.failStreak)
    }

    // ─── the change refresh: run half only ───────────────────────────────────

    @Test
    fun `a change refresh moves the run state and nothing else`() {
        val original = task("t-1", title = "typing…", prompt = "p", windowStart = "08:00", windowEnd = "21:00")
        val fresh = original.copy(
            enabled = false,
            lastRunDate = "2026-10-23",
            lastResult = ScheduledTask.RESULT_ERROR,
            failStreak = 3,
            suspendedByFuse = true,
            title = "SERVER TITLE",
            prompt = "SERVER PROMPT",
            windowStart = "09:00",
            windowEnd = "20:00",
        )
        val refreshed = ScheduledTaskTransitions.refreshRunState(original, fresh)
        // Run half: follows the store.
        assertFalse(refreshed.enabled)
        assertEquals("2026-10-23", refreshed.lastRunDate)
        assertEquals(ScheduledTask.RESULT_ERROR, refreshed.lastResult)
        assertEquals(3, refreshed.failStreak)
        assertTrue(refreshed.suspendedByFuse)
        // Spec half: stays whatever the editor holds — never the server copy.
        assertEquals("typing…", refreshed.title)
        assertEquals("p", refreshed.prompt)
        assertEquals("08:00", refreshed.windowStart)
        assertEquals("21:00", refreshed.windowEnd)
    }

    @Test
    fun `a change refresh of an unchanged record is a no-op`() {
        val original = task("t-1")
        assertEquals(original, ScheduledTaskTransitions.refreshRunState(original, original))
    }
}

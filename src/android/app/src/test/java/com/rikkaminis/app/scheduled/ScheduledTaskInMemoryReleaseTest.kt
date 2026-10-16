package com.rikkaminis.app.scheduled

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1016 B-7c] JVM tests for [InMemoryRunBookkeeping] — the pure decision core
 * behind `ScheduledTaskRunner.onTasksRemoved`, the release that runs when a
 * task is deleted or a backup restore replaces the whole task document.
 *
 * Bug shape being pinned: `ScheduledTasksStore.deleteTask` / `importDocument`
 * cleared the PERSISTED `running` marker but not the runner's in-process
 * single-flight state, so a deleted task kept the lock until
 * reapAbandonedDispatch's deadline+grace (~30 min) and every other task
 * degraded to `overlap`.
 *
 * Every expectation below is a literal (no derivation from the code under
 * test), so the negative controls flip these arms instead of passing silently.
 */
class ScheduledTaskInMemoryReleaseTest {

    // ─── helpers ─────────────────────────────────────────────────────────────

    private fun state(
        runningTaskId: String?,
        vararg dispatches: Pair<String, String>,
    ) = InMemoryRunBookkeeping.State(
        runningTaskId = runningTaskId,
        dispatchedTaskIdBySession = dispatches.toMap(),
    )

    // ─── deleting the running task ───────────────────────────────────────────

    @Test
    fun `deleting the running task drops its session and frees the single-flight lock`() {
        val plan = InMemoryRunBookkeeping.planRemoval(
            state("t-1", "sess-1" to "t-1"),
            removedTaskIds = setOf("t-1"),
        )
        assertEquals(listOf("sess-1"), plan.dropSessions)
        assertTrue(plan.clearRunningTaskId)
        assertFalse(plan.isEmpty)
    }

    @Test
    fun `the lock is still released when the dispatch record is already gone`() {
        // e.g. a reap already evicted the dispatch entry but the lock survived.
        val plan = InMemoryRunBookkeeping.planRemoval(
            state("t-1"),
            removedTaskIds = setOf("t-1"),
        )
        assertEquals(emptyList<String>(), plan.dropSessions)
        assertTrue(plan.clearRunningTaskId)
    }

    // ─── deleting an unrelated / unknown task ────────────────────────────────

    @Test
    fun `deleting a task that is not the running one leaves the live run alone`() {
        val plan = InMemoryRunBookkeeping.planRemoval(
            state("t-1", "sess-1" to "t-1"),
            removedTaskIds = setOf("t-2"),
        )
        assertEquals(emptyList<String>(), plan.dropSessions)
        assertFalse(plan.clearRunningTaskId)
        assertTrue(plan.isEmpty)
    }

    @Test
    fun `deleting an unknown id is a no-op while a run is in flight`() {
        val plan = InMemoryRunBookkeeping.planRemoval(
            state("t-7", "sess-a" to "t-7"),
            removedTaskIds = setOf("does-not-exist"),
        )
        assertTrue(plan.isEmpty)
    }

    @Test
    fun `task ids are compared by equality, not by prefix`() {
        // A `startsWith` regression would drop the live t-10 run when t-1 goes.
        val plan = InMemoryRunBookkeeping.planRemoval(
            state("t-10", "sess-10" to "t-10"),
            removedTaskIds = setOf("t-1"),
        )
        assertTrue(plan.isEmpty)
    }

    // ─── several sessions for one task ───────────────────────────────────────

    @Test
    fun `every session of the removed task is dropped`() {
        val plan = InMemoryRunBookkeeping.planRemoval(
            state(
                "t-1",
                "sess-1" to "t-1",
                "sess-2" to "t-1",
                "sess-other" to "t-2",
            ),
            removedTaskIds = setOf("t-1"),
        )
        assertEquals(listOf("sess-1", "sess-2"), plan.dropSessions)
        assertTrue(plan.clearRunningTaskId)
    }

    @Test
    fun `dropped sessions are sorted so logs are stable`() {
        // ConcurrentHashMap iteration order is not a contract; the plan's is.
        val plan = InMemoryRunBookkeeping.planRemoval(
            state(null, "s-z" to "t-1", "s-a" to "t-1", "s-m" to "t-1"),
            removedTaskIds = setOf("t-1"),
        )
        assertEquals(listOf("s-a", "s-m", "s-z"), plan.dropSessions)
    }

    // ─── backup restore (whole document replaced) ────────────────────────────

    @Test
    fun `a restore drops every session and clears the lock`() {
        val plan = InMemoryRunBookkeeping.planRemoval(
            state("t-1", "sess-1" to "t-1", "sess-2" to "t-9"),
            removedTaskIds = null,
        )
        assertEquals(listOf("sess-1", "sess-2"), plan.dropSessions)
        assertTrue(plan.clearRunningTaskId)
    }

    @Test
    fun `a restore of an idle process is a no-op`() {
        val plan = InMemoryRunBookkeeping.planRemoval(
            state(null),
            removedTaskIds = null,
        )
        assertTrue(plan.isEmpty)
    }

    @Test
    fun `a restore clears a lock whose dispatch record is already gone`() {
        val plan = InMemoryRunBookkeeping.planRemoval(
            state("t-3"),
            removedTaskIds = null,
        )
        assertEquals(emptyList<String>(), plan.dropSessions)
        assertTrue(plan.clearRunningTaskId)
    }

    // ─── the empty set ───────────────────────────────────────────────────────

    @Test
    fun `an empty removal set never touches live state`() {
        val plan = InMemoryRunBookkeeping.planRemoval(
            state("t-1", "sess-1" to "t-1"),
            removedTaskIds = emptySet(),
        )
        assertTrue(plan.isEmpty)
    }
}

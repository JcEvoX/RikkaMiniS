package com.rikkaminis.app.browser

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CountDownLatch

/**
 * [deep-audit-1016c ISSUE-03C] The four `_downloads` write sites
 * (`registerDownload` prepend :472, `updateDownloadEntry` map :478,
 * `markDownloadsSeen` map :527, `clearFinishedDownloads` filter :535) were
 * non-atomic read-modify-writes on a `MutableStateFlow` whose writers span
 * three threads (URL-download listener on Main, blob bridge on the
 * JavaBridge thread, download coroutines on IO). They now use
 * [MutableStateFlow.update] (CAS loop).
 *
 * This device pins the two halves of that claim:
 *
 *  1. **conservation** — N concurrent writers × M `update {}` appends land
 *     exactly N×M entries (linearizable; deterministic expectation);
 *  2. **the old shape loses data** — an explicitly interleaved read-modify-
 *     write (read snapshot → concurrent write → write stale snapshot back)
 *     deterministically drops the concurrent write.
 *
 * The scan gate `scripts/scan/downloads_atomic_update_check.py` is the
 * structural teeth against a revert inside BrowserTabPool.kt; this class is
 * the semantic half.
 */
class DownloadsAtomicUpdateTest {

    // ── arm 1: update {} conserves every write ──────────────────────────

    @Test
    fun `concurrent registration appends via update land exactly once each`() {
        val threads = 4
        val perThread = 500
        val store = MutableStateFlow<List<String>>(emptyList())
        val start = CountDownLatch(1)
        val workers = (0 until threads).map { t ->
            Thread {
                start.await()
                repeat(perThread) { r ->
                    // registerDownload's exact shape: newest-first prepend.
                    store.update { current -> listOf("d$t-$r") + current }
                }
            }
        }
        workers.forEach { it.start() }
        start.countDown()
        workers.forEach { it.join(30_000) }

        assertEquals(
            "every concurrent prepend must survive (no lost update)",
            threads * perThread,
            store.value.size,
        )
        // Ordering is not guaranteed across threads, membership is.
        assertEquals(
            "every entry must be present exactly once",
            (0 until threads).flatMap { t -> (0 until perThread).map { r -> "d$t-$r" } }.toSet(),
            store.value.toSet(),
        )
    }

    @Test
    fun `progress transforms via update all land even when writers interleave`() {
        val threads = 4
        val perThread = 500
        val store = MutableStateFlow(mapOf<String, Int>())
        val start = CountDownLatch(1)
        val workers = (0 until threads).map { t ->
            Thread {
                start.await()
                repeat(perThread) {
                    // updateDownloadEntry's exact shape: map the matching id.
                    store.update { current -> current + ("k$t" to (current["k$t"] ?: 0) + 1) }
                }
            }
        }
        workers.forEach { it.start() }
        start.countDown()
        workers.forEach { it.join(30_000) }

        assertEquals(
            "each writer's M transforms must each apply exactly once",
            mapOf("k0" to perThread, "k1" to perThread, "k2" to perThread, "k3" to perThread),
            store.value,
        )
    }

    // ── arm 2: the read-modify-write shape it replaced loses a write ────

    @Test
    fun `the replaced read-modify-write shape deterministically loses a concurrent write`() {
        val store = MutableStateFlow(listOf("seed"))

        // Writer A (the pattern the fix replaced): read …
        val staleRead = store.value
        // … writer B lands its entry in the gap …
        store.value = listOf("concurrent") + store.value
        // … writer A writes its stale snapshot back.
        store.value = listOf("mine") + staleRead

        assertEquals(
            "the concurrent writer's entry vanished — exactly the loss the fix removes",
            listOf("mine", "seed"),
            store.value,
        )
        assertTrue("the concurrent entry is gone from the list", "concurrent" !in store.value)
    }
}

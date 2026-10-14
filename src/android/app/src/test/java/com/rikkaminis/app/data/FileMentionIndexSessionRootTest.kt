package com.rikkaminis.app.data

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * [fix/sweep1014-b8] Session-root regression tests for the @-mention index.
 *
 * The per-session trees live under `filesDir/minis-sessions/<sid>/<subdir>`
 * on the host (PRootKernel.resolveSessionHostPath) and mount at
 * `/var/minis/<subdir>` inside PRoot — NO `<sid>` suffix in the Linux path.
 * The old Layer-1 roots (`minis-global/workspace/<sid>` +
 * `"/var/minis/<label>/<sid>"`) never existed, so session-local files were
 * 100% un-mentionable via `@`. These tests pin the fixed roots and the
 * Linux-visible paths.
 */
class FileMentionIndexSessionRootTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        // FileMentionIndex.publish switches to Dispatchers.Main — provide a
        // test Main (same pattern as ConfigConfirmationGateNotifyTest).
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun newIndex(root: File): FileMentionIndex = FileMentionIndex(
        filesDir = root,
        mountsProvider = { emptyList() },
        scope = CoroutineScope(SupervisorJob() + Dispatchers.Unconfined),
    )

    private fun awaitEntry(index: FileMentionIndex, predicate: (FileMentionIndex.Entry) -> Boolean): FileMentionIndex.Entry? {
        val deadline = System.currentTimeMillis() + 5000
        while (System.currentTimeMillis() < deadline) {
            dispatcher.scheduler.advanceUntilIdle()
            val hit = index.entries.value.firstOrNull { !it.isDirectory && predicate(it) }
            if (hit != null) return hit
            Thread.sleep(20)
        }
        return null
    }

    @Test
    fun `session workspace file indexed with linux path without sid suffix`() {
        val root = Files.createTempDirectory("fmi-ws").toFile()
        val ws = File(root, "minis-sessions/sess-1/workspace").apply { mkdirs() }
        File(ws, "a.txt").writeText("hello")
        val index = newIndex(root)
        index.refreshIfNeeded("sess-1")
        val hit = awaitEntry(index) { it.linuxPath == "/var/minis/workspace/a.txt" }
        assertTrue("expected /var/minis/workspace/a.txt, got: " +
            index.entries.value.joinToString { it.linuxPath }, hit != null)
        org.junit.Assert.assertEquals(FileMentionIndex.Scope.WORKSPACE, hit!!.scope)
        assertFalse(hit.linuxPath.contains("sess-1"))
    }

    @Test
    fun `session attachments file indexed`() {
        val root = Files.createTempDirectory("fmi-att").toFile()
        val att = File(root, "minis-sessions/sess-1/attachments").apply { mkdirs() }
        File(att, "p.png").writeBytes(byteArrayOf(1))
        val index = newIndex(root)
        index.refreshIfNeeded("sess-1")
        val hit = awaitEntry(index) { it.linuxPath == "/var/minis/attachments/p.png" }
        assertTrue("expected /var/minis/attachments/p.png, got: " +
            index.entries.value.joinToString { it.linuxPath }, hit != null)
        org.junit.Assert.assertEquals(FileMentionIndex.Scope.ATTACHMENTS, hit!!.scope)
    }

    @Test
    fun `global shared file indexed via minis-global root`() {
        val root = Files.createTempDirectory("fmi-shared").toFile()
        val shared = File(root, "minis-global/shared").apply { mkdirs() }
        File(shared, "x.md").writeText("note")
        val index = newIndex(root)
        index.refreshIfNeeded("sess-1")
        val hit = awaitEntry(index) { it.linuxPath == "/var/minis/shared/x.md" }
        assertTrue("expected /var/minis/shared/x.md, got: " +
            index.entries.value.joinToString { it.linuxPath }, hit != null)
        org.junit.Assert.assertEquals(FileMentionIndex.Scope.SHARED, hit!!.scope)
    }

    @Test
    fun `session switch rescans the new session tree`() {
        val root = Files.createTempDirectory("fmi-switch").toFile()
        val index = newIndex(root)
        index.refreshIfNeeded("sess-1")
        val ws2 = File(root, "minis-sessions/sess-2/workspace").apply { mkdirs() }
        File(ws2, "b.txt").writeText("second")
        index.refreshIfNeeded("sess-2")
        val hit = awaitEntry(index) { it.linuxPath == "/var/minis/workspace/b.txt" }
        assertTrue("expected /var/minis/workspace/b.txt after session switch, got: " +
            index.entries.value.joinToString { it.linuxPath }, hit != null)
    }
}

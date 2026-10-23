package com.rikkaminis.app.share

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.util.UUID

/**
 * [§1044-A08] Regression tests for the shared-stream staging copy.
 *
 * The defect: `ShareReceiverActivity.copyUriToStaging` read
 * `contentResolver.openInputStream(uri)?.use { … }` and then returned the staged
 * filename unconditionally. `openInputStream` is allowed to return null WITHOUT
 * throwing (a remote provider that refuses the read, or whose process was
 * reaped), so the `use` block never ran, no file was created, and the caller
 * still built a `PendingShare.Item` for a file that did not exist — a ghost
 * attachment that reached the composer and skipped the "Could not read shared
 * file" toast.
 *
 * The copy now lives in [copySharedStream]; these tests pin the decisions the
 * inline version got wrong or could lose, with HAND-WRITTEN literal
 * expectations (never derived from the code under test — a constant changed
 * together with its expectation would hide the mutation behind a PASS):
 *  * a null stream stages NOTHING (the defect itself);
 *  * a null stream leaves no 0-byte file in the staging directory;
 *  * bytes are written and credited exactly once, with the exact count;
 *  * the per-file / cumulative caps reject, delete the partial file and credit
 *    nothing, and admit a share that exactly fills either cap;
 *  * the IO deadline aborts a stalled provider and deletes the partial file;
 *  * a read failure deletes the partial file and reports the exception.
 */
class SharedStreamCopyTest {

    private val createdDirs = mutableListOf<File>()

    @After
    fun removeTempDirs() {
        for (dir in createdDirs) dir.deleteRecursively()
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private fun tempDir(): File {
        val dir = File(System.getProperty("java.io.tmpdir"), "share-stream-${UUID.randomUUID()}")
        assertTrue("temp dir must be creatable: $dir", dir.mkdirs())
        createdDirs += dir
        return dir
    }

    private fun destIn(dir: File): File = File(dir, "shared-image-abcd1234.png")

    private fun runCopy(
        dir: File,
        source: InputStream?,
        ledger: StagedBytesLedger,
        maxFileBytes: Long = 100L,
        maxTotalBytes: Long = 1000L,
        ioTimeoutMs: Long = 30_000L,
        now: () -> Long = { 0L },
    ): SharedStreamCopy = copySharedStream(
        openStream = { source },
        dest = destIn(dir),
        stagedBytesNow = { ledger.current() },
        credit = { ledger.credit(it) },
        maxFileBytes = maxFileBytes,
        maxTotalBytes = maxTotalBytes,
        ioTimeoutMs = ioTimeoutMs,
        now = now,
    )

    private fun stagedEntries(dir: File): Int = dir.listFiles()?.size ?: 0

    // ── the defect ──────────────────────────────────────────────────────────

    @Test
    fun `a null stream fails instead of reporting a staged file`() {
        val dir = tempDir()
        val ledger = StagedBytesLedger()

        val outcome = runCopy(dir, source = null, ledger = ledger)

        assertTrue("a null stream must NOT report success: $outcome", outcome is SharedStreamCopy.Failed)
        assertEquals(SharedStreamFailure.OpenStreamNull, (outcome as SharedStreamCopy.Failed).reason)
        assertEquals(0L, ledger.current())
    }

    @Test
    fun `a null stream leaves no ghost file in the staging directory`() {
        val dir = tempDir()
        val ledger = StagedBytesLedger()

        val outcome = runCopy(dir, source = null, ledger = ledger)

        assertTrue(outcome is SharedStreamCopy.Failed)
        assertFalse("no destination file may be created: ${destIn(dir)}", destIn(dir).exists())
        assertEquals("the staging dir must stay empty", 0, stagedEntries(dir))
    }

    // ── happy path ──────────────────────────────────────────────────────────

    @Test
    fun `bytes are written and credited once with the exact count`() {
        val dir = tempDir()
        val ledger = StagedBytesLedger()

        val outcome = runCopy(dir, source = ByteArrayInputStream("hello".toByteArray()), ledger = ledger)

        assertTrue(outcome is SharedStreamCopy.Staged)
        assertEquals(5L, (outcome as SharedStreamCopy.Staged).bytes)
        assertEquals("hello", destIn(dir).readText())
        assertEquals(5L, ledger.current())
    }

    @Test
    fun `an empty stream stages zero bytes and credits nothing`() {
        val dir = tempDir()
        val ledger = StagedBytesLedger()
        ledger.credit(7L)   // unrelated balance must survive the copy

        val outcome = runCopy(dir, source = ByteArrayInputStream(ByteArray(0)), ledger = ledger)

        assertTrue(outcome is SharedStreamCopy.Staged)
        assertEquals(0L, (outcome as SharedStreamCopy.Staged).bytes)
        assertEquals(7L, ledger.current())
    }

    // ── caps ────────────────────────────────────────────────────────────────

    @Test
    fun `the per file cap rejects and deletes the partial file`() {
        val dir = tempDir()
        val ledger = StagedBytesLedger()

        val outcome = runCopy(
            dir, source = ByteArrayInputStream("abcdef".toByteArray()), ledger = ledger, maxFileBytes = 5L,
        )

        assertTrue(outcome is SharedStreamCopy.Failed)
        assertEquals(SharedStreamFailure.ExceededPerFile, (outcome as SharedStreamCopy.Failed).reason)
        assertFalse("the partial file must be removed", destIn(dir).exists())
        assertEquals(0L, ledger.current())
    }

    @Test
    fun `a share exactly at the per file cap is admitted`() {
        val dir = tempDir()
        val ledger = StagedBytesLedger()

        val outcome = runCopy(
            dir, source = ByteArrayInputStream("abcde".toByteArray()), ledger = ledger, maxFileBytes = 5L,
        )

        assertTrue(outcome is SharedStreamCopy.Staged)
        assertEquals(5L, (outcome as SharedStreamCopy.Staged).bytes)
        assertEquals(5L, ledger.current())
    }

    @Test
    fun `the cumulative cap rejects and deletes the partial file`() {
        val dir = tempDir()
        val ledger = StagedBytesLedger()
        ledger.credit(96L)

        val outcome = runCopy(
            dir, source = ByteArrayInputStream("abcde".toByteArray()), ledger = ledger, maxTotalBytes = 100L,
        )

        assertTrue(outcome is SharedStreamCopy.Failed)
        assertEquals(SharedStreamFailure.ExceededCumulative, (outcome as SharedStreamCopy.Failed).reason)
        assertFalse("the partial file must be removed", destIn(dir).exists())
        assertEquals("a rejected share must not be credited", 96L, ledger.current())
    }

    @Test
    fun `a share exactly filling the cumulative cap is admitted`() {
        val dir = tempDir()
        val ledger = StagedBytesLedger()
        ledger.credit(95L)

        val outcome = runCopy(
            dir, source = ByteArrayInputStream("abcde".toByteArray()), ledger = ledger, maxTotalBytes = 100L,
        )

        assertTrue(outcome is SharedStreamCopy.Staged)
        assertEquals(100L, ledger.current())
    }

    // ── abort paths ─────────────────────────────────────────────────────────

    @Test
    fun `the io deadline aborts a stalled provider and deletes the partial file`() {
        val dir = tempDir()
        val ledger = StagedBytesLedger()
        var clock = 0L

        // Six 1-byte reads available, but the clock advances 500 ms per call and
        // the deadline is 1000 ms — the copy must give up after two bytes.
        val outcome = runCopy(
            dir,
            source = DripStream("abcdef".toByteArray()),
            ledger = ledger,
            ioTimeoutMs = 1000L,
            now = { clock.also { clock += 500L } },
        )

        assertTrue(outcome is SharedStreamCopy.Failed)
        assertEquals(SharedStreamFailure.TimedOut, (outcome as SharedStreamCopy.Failed).reason)
        assertFalse("the partial file must be removed", destIn(dir).exists())
        assertEquals(0L, ledger.current())
    }

    @Test
    fun `a read failure deletes the partial file and reports the exception`() {
        val dir = tempDir()
        val ledger = StagedBytesLedger()

        val outcome = runCopy(dir, source = FailingStream(), ledger = ledger)

        assertTrue(outcome is SharedStreamCopy.Failed)
        val failed = outcome as SharedStreamCopy.Failed
        assertEquals(SharedStreamFailure.ReadFailed, failed.reason)
        assertEquals("boom", failed.detail)
        assertFalse("the partial file must be removed", destIn(dir).exists())
        assertEquals(0L, ledger.current())
    }

    // ── fixtures ────────────────────────────────────────────────────────────

    /** Yields one byte per read, never EOF early — lets the clock drive the abort. */
    private class DripStream(private val payload: ByteArray) : InputStream() {
        private var index = 0
        override fun read(): Int =
            if (index < payload.size) payload[index++].toInt() and 0xFF else -1

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (index >= payload.size) return -1
            if (len <= 0) return 0
            b[off] = payload[index++]
            return 1
        }
    }

    private class FailingStream : InputStream() {
        override fun read(): Int = throw IOException("boom")
    }

    /** Documents that the ledger is the only credit sink the core talks to. */
    @Test
    fun `credit is never called for a failed copy`() {
        val dir = tempDir()
        var credited: Long? = null

        val outcome = copySharedStream(
            openStream = { null },
            dest = destIn(dir),
            stagedBytesNow = { 0L },
            credit = { credited = it },
            maxFileBytes = 100L,
            maxTotalBytes = 1000L,
            ioTimeoutMs = 30_000L,
            now = { 0L },
        )

        assertTrue(outcome is SharedStreamCopy.Failed)
        assertNull("no credit may be issued for an aborted copy", credited)
    }
}

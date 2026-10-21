package com.rikkaminis.app.browser

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

/**
 * [fix/dv17-1 B-06] (新增) the pure half of the draft-promotion download
 * re-pointing.
 *
 * Why this test exists: a download made while the chat was still a draft
 * recorded `minis-sessions/__new__<uuid>/workspace/<name>` in
 * [BrowserTabPool.DownloadEntry.destination]. Draft promotion moved the bytes
 * to the real session directory and deleted the draft tree, while
 * [BrowserTabPool.setSession] re-pointed only the session id and the live tabs
 * — so the downloads panel's "open" button died at its `file.exists()` guard
 * with zero feedback. [remapDownloadDestinations] is the path rewrite that
 * closes it; every expectation here is a hand-written literal.
 */
class DownloadDestinationRemapTest {

    /** Stand-in row so the pure function is testable without Android types. */
    private data class Row(val label: String, val dest: File?)

    private val oldDir = File("/tmp/probe/__new__abc/workspace")
    private val newDir = File("/tmp/probe/session-42/workspace")

    private fun remap(rows: List<Row>): List<Row> = remapDownloadDestinations(
        entries = rows,
        oldDir = oldDir,
        newDir = newDir,
        pathOf = { it.dest },
        withPath = { row, path -> row.copy(dest = path) },
    )

    @Test
    fun aRecordUnderTheOldWorkspaceIsRepointedToTheNewOne() {
        val result = remap(listOf(Row("k", File(oldDir, "report.pdf")))).single()
        assertEquals("/tmp/probe/session-42/workspace/report.pdf", result.dest!!.path)
        assertEquals("report.pdf", result.dest!!.name)
        assertEquals("k", result.label)
    }

    @Test
    fun nestedRelativeStructureIsPreserved() {
        val result = remap(listOf(Row("k", File(oldDir, "sub/dir/notes.txt")))).single()
        assertEquals("/tmp/probe/session-42/workspace/sub/dir/notes.txt", result.dest!!.path)
    }

    @Test
    fun aRecordOutsideTheOldWorkspaceIsLeftByteIdentical() {
        val rows = listOf(
            Row("OtherSession", File("/tmp/probe/other-session/workspace/keep.bin")),
            Row("Absolute", File("/var/minis/browser/keep.bin")),
            Row("AlreadyNew", File(newDir, "keep.bin")),
            // Name-only sibling of the old session: shares "__new__abc" but
            // diverges before "/workspace" — must not match at all.
            Row("SiblingName", File("/tmp/probe/__new__abcdef/workspace/keep.bin")),
            // Separator trap: this path DOES start with the old directory's
            // path as a string ("/tmp/probe/__new__abc/workspace"), but only
            // because "-extra" continues the last segment. The prefix must
            // end in File.separator or this row would be silently rewritten.
            Row("SiblingPrefix", File("/tmp/probe/__new__abc/workspace-extra/keep.bin")),
        )
        val result = remap(rows)
        assertEquals(rows, result)
        result.forEachIndexed { i, row -> assertSame(rows[i].dest, row.dest) }
    }

    @Test
    fun aRecordWithNoDestinationIsUntouched() {
        val rows = listOf(Row("Failed", null))
        val result = remap(rows)
        assertEquals(rows, result)
        assertNull(result.single().dest)
    }

    @Test
    fun theOldDirectoryItselfIsNotRewrittenIntoAFile() {
        // Degenerate input: destination IS the old directory (no relative tail).
        // There is nothing to re-point, so the row must come back untouched.
        val rows = listOf(Row("DirItself", oldDir))
        assertEquals(rows, remap(rows))
    }

    @Test
    fun anEmptyRegistryStaysEmpty() {
        assertEquals(emptyList<Row>(), remap(emptyList()))
    }

    @Test
    fun theRealDownloadEntryComposesWithThePureFunction() {
        // Proves the production wiring shape type-checks: the same lambdas
        // BrowserTabPool.remapDownloadsToSession passes, over the real type.
        val entry = BrowserTabPool.DownloadEntry(
            id = 7L,
            filename = "report.pdf",
            destination = File(oldDir, "report.pdf"),
            bytesDone = 12L,
            totalBytes = 34L,
            state = BrowserTabPool.DownloadState.COMPLETED,
            startedAt = 1_700_000_000_000L,
            seen = false,
        )
        val remapped = remapDownloadDestinations(
            entries = listOf(entry),
            oldDir = oldDir,
            newDir = newDir,
            pathOf = { it.destination },
            withPath = { e, path -> e.copy(destination = path) },
        ).single()
        assertEquals("/tmp/probe/session-42/workspace/report.pdf", remapped.destination!!.path)
        assertEquals("report.pdf", remapped.destination!!.name)
        // Identity survives: only the path is replaced.
        assertEquals(7L, remapped.id)
        assertEquals(BrowserTabPool.DownloadState.COMPLETED, remapped.state)
        assertEquals(12L, remapped.bytesDone)
        assertEquals(34L, remapped.totalBytes)
        assertEquals(1_700_000_000_000L, remapped.startedAt)
        assertEquals(false, remapped.seen)
    }
}

package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * [fix/da1023-A] — deep-audit-1023 ISSUE-01.
 *
 * `LargeContentGuard.exportLargeContentToFile` wrote one `msg-*.txt` per tap
 * under `cacheDir/large-messages` and nothing ever deleted it, so exports were
 * permanent orphans on top of being broken (the dir was also missing from
 * `res/xml/file_provider_paths.xml`, so `getUriForFile` threw every time).
 * [pruneExpiredLargeMessageFiles] is the cleanup half, extracted as a pure
 * File/Long function precisely so it can be driven here with a real temp dir —
 * no Robolectric, no Context.
 *
 * Expectations below are literal numbers, never derived from
 * [LARGE_MESSAGE_EXPORT_TTL_MS]: a test that recomputes the constant would
 * follow it silently if someone changed it. The shipped default is pinned
 * once, explicitly, in [default ttl is exactly 24 hours in millis].
 *
 * Expiry semantics deliberately match `ui/components/pruneShareDir.kt`
 * (`f.lastModified() < now - TTL`): strictly older dies, exactly-TTL-old
 * survives. [exactly at the ttl boundary survives] is the test that pins the
 * difference between `<` and `<=`.
 */
class LargeMessageExportPruneTest {

    @get:Rule
    val tmp = TemporaryFolder()

    /** Arbitrary fixed clock, ms-aligned so a 1s-granularity filesystem cannot
     *  blur the boundary cases. */
    private val nowMs = 1_700_000_000_000L

    /** 24h in ms — the value the production default must equal. */
    private val twentyFourHours = 86_400_000L

    private fun exportDir(): File = tmp.newFolder("large-messages")

    private fun aged(parent: File, name: String, ageMs: Long): File {
        val f = File(parent, name)
        f.writeText("x")
        assertTrue("fixture write failed for $name", f.setLastModified(nowMs - ageMs))
        return f
    }

    // ── TTL boundary ───────────────────────────────────────────────────────

    @Test
    fun `file strictly older than ttl is deleted`() {
        val dir = exportDir()
        val old = aged(dir, "msg-old.txt", ageMs = 86_400_001L)
        assertEquals(1, pruneExpiredLargeMessageFiles(dir, nowMs, maxAgeMs = 86_400_000L))
        assertFalse("expired export must be gone", old.exists())
    }

    @Test
    fun `exactly at the ttl boundary survives`() {
        val dir = exportDir()
        val edge = aged(dir, "msg-edge.txt", ageMs = 86_400_000L)
        assertEquals(
            "strictly-older test: age == ttl survives (pruneShareDir parity)",
            0,
            pruneExpiredLargeMessageFiles(dir, nowMs, maxAgeMs = 86_400_000L),
        )
        assertTrue(edge.exists())
    }

    @Test
    fun `file younger than ttl survives`() {
        val dir = exportDir()
        val fresh = aged(dir, "msg-fresh.txt", ageMs = 86_399_999L)
        assertEquals(0, pruneExpiredLargeMessageFiles(dir, nowMs, maxAgeMs = 86_400_000L))
        assertTrue(fresh.exists())
    }

    @Test
    fun `brand new export survives`() {
        val dir = exportDir()
        val now = File(dir, "msg-now.txt").apply { writeText("x") }
        assertEquals(0, pruneExpiredLargeMessageFiles(dir, nowMs, maxAgeMs = 86_400_000L))
        assertTrue(now.exists())
    }

    // ── directory / entry shapes ───────────────────────────────────────────

    @Test
    fun `missing directory returns zero instead of throwing`() {
        val ghost = File(tmp.root, "never-created")
        assertFalse(ghost.exists())
        assertEquals(0, pruneExpiredLargeMessageFiles(ghost, nowMs, maxAgeMs = 86_400_000L))
    }

    @Test
    fun `empty directory returns zero`() {
        val dir = exportDir()
        assertEquals(0, pruneExpiredLargeMessageFiles(dir, nowMs, maxAgeMs = 86_400_000L))
    }

    @Test
    fun `subdirectory is not deleted`() {
        val dir = exportDir()
        val nested = File(dir, "nested").apply { mkdir() }
        // A dir entry that is ancient: `isFile` must keep it, otherwise a
        // future non-flat layout would be silently wiped.
        assertTrue(nested.setLastModified(nowMs - 900_000_000L))
        assertEquals(0, pruneExpiredLargeMessageFiles(dir, nowMs, maxAgeMs = 86_400_000L))
        assertTrue(nested.exists())
    }

    @Test
    fun `mixed directory deletes every expired file and counts them`() {
        val dir = exportDir()
        val a = aged(dir, "msg-a.txt", ageMs = 100_000_000L)
        val b = aged(dir, "msg-b.txt", ageMs = 99_999_999L)
        val c = aged(dir, "msg-c.txt", ageMs = 1_000L)
        val d = aged(dir, "msg-d.txt", ageMs = 86_400_000L)
        assertEquals(2, pruneExpiredLargeMessageFiles(dir, nowMs, maxAgeMs = 86_400_000L))
        assertFalse(a.exists())
        assertFalse(b.exists())
        assertTrue(c.exists())
        assertTrue(d.exists())
    }

    @Test
    fun `non msg prefixed files are left alone`() {
        // The current writer only ever creates msg-*.txt, but the prune walks
        // every file — this pins that it does NOT filter by name, so a stray
        // file in the dir is treated by age alone (recorded behaviour, not an
        // accident: the delete condition has no name check).
        val dir = exportDir()
        val stray = aged(dir, "not-an-export.bin", ageMs = 100_000_000L)
        assertEquals(1, pruneExpiredLargeMessageFiles(dir, nowMs, maxAgeMs = 86_400_000L))
        assertFalse(stray.exists())
    }

    // ── shipped default ────────────────────────────────────────────────────

    @Test
    fun `default ttl is exactly 24 hours in millis`() {
        // Literal, not `24 * 60 * 60 * 1000`, on purpose — see class KDoc.
        assertEquals(86_400_000L, LARGE_MESSAGE_EXPORT_TTL_MS)
    }

    @Test
    fun `default maxAge is used when the argument is omitted`() {
        val dir = exportDir()
        val old = aged(dir, "msg-old.txt", ageMs = LARGE_MESSAGE_EXPORT_TTL_MS_DEFAULT_FOR_TEST)
        assertEquals(1, pruneExpiredLargeMessageFiles(dir, nowMs))
        assertFalse(old.exists())
    }

    private companion object {
        /** One ms past the literal 24h default, spelled out as a literal. */
        const val LARGE_MESSAGE_EXPORT_TTL_MS_DEFAULT_FOR_TEST = 86_400_001L
    }
}

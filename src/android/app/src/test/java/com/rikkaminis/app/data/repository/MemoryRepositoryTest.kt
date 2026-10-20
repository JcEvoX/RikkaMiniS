package com.rikkaminis.app.data.repository

import com.rikkaminis.app.agent.SoulStore
import com.rikkaminis.app.workspace.MemoryRollupEngine
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * JVM tests for [MemoryRepository]'s file listing / search filters.
 *
 * Guards the rollup-file exclusion: MEMORY-ROLLUP.md is an auto-generated
 * distilled index owned by memory_rollup, not a user-editable daily log. It
 * must never surface in listAllFiles() (so the user can't delete/edit it and
 * break the rollup idempotency anchor) nor in getMemory()'s daily-log scan
 * (so its distilled entries don't duplicate the original logs and waste the
 * search budget).
 */
class MemoryRepositoryTest {

    private val tempDir = File(System.getProperty("java.io.tmpdir"), "mem-repo-${UUID.randomUUID().toString().take(8)}")
    private val memoryDir = File(tempDir, "minis-global/memory")

    @After
    fun tearDown() {
        tempDir.deleteRecursively()
    }

    @Test
    fun listAllFiles_excludesRollupFile() {
        memoryDir.mkdirs()
        File(memoryDir, "GLOBAL.md").writeText("## global\npersistent prefs\n")
        File(memoryDir, "2026-08-25.md").writeText("<!-- 2026-08-25 10:00:00 -->\n## entry\ncontent\n")
        File(memoryDir, MemoryRollupEngine.ROLLUP_FILE).writeText("## Rollup 2026-08-24\n### 经验与知识点\n- 蒸馏条目\n")

        val repo = MemoryRepository(memoryDir)
        val names = repo.listAllFiles().map { it.name }

        assertTrue("GLOBAL.md must always be listed", names.contains("GLOBAL.md"))
        assertTrue("daily log must be listed", names.contains("2026-08-25.md"))
        assertFalse(
            "MEMORY-ROLLUP.md must not appear in the file list (user could delete/edit it)",
            names.contains(MemoryRollupEngine.ROLLUP_FILE),
        )
    }

    @Test
    fun getMemory_excludesRollupFile_fromSearch() {
        memoryDir.mkdirs()
        // Original daily log carries a distinctive keyword.
        File(memoryDir, "2026-08-25.md").writeText("<!-- 2026-08-25 10:00:00 -->\n## 经验\n根因特别关键词甲\n")
        // The rollup file also contains that same distilled keyword.
        File(memoryDir, MemoryRollupEngine.ROLLUP_FILE).writeText(
            "## Rollup 2026-08-24\n### 经验与知识点\n- **经验**：根因特别关键词甲\n",
        )

        val repo = MemoryRepository(memoryDir)
        val result = repo.getMemory("特别关键词甲", "daily")

        // The keyword is present via the daily log; the rollup file must not
        // be surfaced as a separate "file" in the output.
        assertTrue("search must find the daily log match", result.contains("2026-08-25.md"))
        assertFalse(
            "MEMORY-ROLLUP.md must not be searched as a daily log",
            result.contains(MemoryRollupEngine.ROLLUP_FILE),
        )
    }

    @Test
    fun getMemory_fullDump_excludesRollupFile() {
        memoryDir.mkdirs()
        File(memoryDir, "2026-08-25.md").writeText("## 日志正文\n一行内容\n")
        File(memoryDir, MemoryRollupEngine.ROLLUP_FILE).writeText("## Rollup 2026-08-24\n- 一条蒸馏\n")

        val repo = MemoryRepository(memoryDir)
        val result = repo.getMemory("", "daily")

        assertTrue(result.contains("2026-08-25.md"))
        assertFalse(result.contains(MemoryRollupEngine.ROLLUP_FILE))
    }

    @Test
    fun listAllFiles_sortsGlobalFirst_thenDailyLogsDescending() {
        memoryDir.mkdirs()
        File(memoryDir, "2026-08-24.md").writeText("## older\n")
        File(memoryDir, "2026-08-25.md").writeText("## newer\n")
        File(memoryDir, "GLOBAL.md").writeText("## global\n")

        val names = MemoryRepository(memoryDir).listAllFiles().map { it.name }
        assertEquals(listOf("GLOBAL.md", "2026-08-25.md", "2026-08-24.md"), names)
    }

    // ── [fix/send-prompt-bloat] loadRollupFragment byte-cap behaviour ──────

    @Test
    fun loadRollupFragment_missingFile_returnsNull() {
        val repo = MemoryRepository(memoryDir)
        assertNull(repo.loadRollupFragment())
    }

    @Test
    fun loadRollupFragment_emptyFile_returnsNull() {
        memoryDir.mkdirs()
        File(memoryDir, MemoryRollupEngine.ROLLUP_FILE).writeText("")
        val repo = MemoryRepository(memoryDir)
        assertNull(repo.loadRollupFragment())
    }

    @Test
    fun loadRollupFragment_smallFile_passesThroughVerbatim() {
        memoryDir.mkdirs()
        val small = "## Rollup 2026-08-12\n- rule A\n"
        File(memoryDir, MemoryRollupEngine.ROLLUP_FILE).writeText(small)
        val repo = MemoryRepository(memoryDir)
        assertEquals(small, repo.loadRollupFragment())
    }

    @Test
    fun loadRollupFragment_oversize_keepsTailAndMarksTruncation() {
        memoryDir.mkdirs()
        val big = StringBuilder("## Rollup OLD\n")
            .append("x".repeat(20_000))
            .append("\n## Rollup NEW\n- newest rule Y\n")
        File(memoryDir, MemoryRollupEngine.ROLLUP_FILE).writeText(big.toString())
        val repo = MemoryRepository(memoryDir)
        val capped = repo.loadRollupFragment()
        assertNotNull(capped)
        assertTrue(capped!!.length < big.length)
        assertTrue(capped.contains("older rollup rules truncated"))
        assertTrue("tail (newest rules) must be kept", capped.contains("newest rule Y"))
        assertFalse("oldest head should be dropped", capped.contains("## Rollup OLD"))
    }

    @Test
    fun loadRollupFragment_oversizeCjk_noReplacementChar() {
        memoryDir.mkdirs()
        // "房" is 3 bytes; a trailing ASCII byte forces the cut point to land
        // mid-code-point, which a naive byte-array slice would decode as U+FFFD.
        val misaligned = ("房".repeat(20_000)) + "X"
        File(memoryDir, MemoryRollupEngine.ROLLUP_FILE).writeText(misaligned)
        val repo = MemoryRepository(memoryDir)
        val capped = repo.loadRollupFragment()
        assertNotNull(capped)
        assertFalse("UTF-8 boundary must not produce U+FFFD", capped!!.contains('\uFFFD'))
    }

    // -- [FIX-6 / F-224] persona-file (SOUL.md) guards -------------------
    //
    // SOUL.md lives in this same directory (SoulStore.fileLocation →
    // <filesDir>/minis-global/memory/SOUL.md) and holds the user's persona.
    // Before the fix it was listed as a daily log, so the settings screen
    // offered a working Delete button next to it; deleting it made
    // SoulStore.ensureExists re-seed DEFAULT_CONTENT on the next launch — a
    // silent persona reset with no prompt, no backup and no undo.
    //
    // These tests reference the real SoulStore.FILE_NAME (the single source of
    // truth), so they stay honest if the filename ever changes.

    @Test
    fun listAllFiles_excludesSoulFile() {
        memoryDir.mkdirs()
        File(memoryDir, "GLOBAL.md").writeText("## global\npersistent prefs\n")
        File(memoryDir, "2026-08-25.md").writeText("<!-- 2026-08-25 10:00:00 -->\n## entry\ncontent\n")
        File(memoryDir, SoulStore.FILE_NAME).writeText("---\nname: RikkaMinis\n---\npersona body\n")

        val repo = MemoryRepository(memoryDir)
        val names = repo.listAllFiles().map { it.name }

        assertFalse(
            "SOUL.md must not appear in the daily-log list (it would offer a Delete button)",
            names.contains(SoulStore.FILE_NAME),
        )
        // Control arm: the fix must not over-filter — real logs still show up.
        assertTrue("daily log must still be listed", names.contains("2026-08-25.md"))
        assertTrue("GLOBAL.md must still be listed", names.contains("GLOBAL.md"))
    }

    @Test
    fun getMemory_excludesSoulFile_fromSearch() {
        memoryDir.mkdirs()
        File(memoryDir, "2026-08-25.md").writeText("<!-- 2026-08-25 10:00:00 -->\n## entry\nunique-log-marker\n")
        File(memoryDir, SoulStore.FILE_NAME).writeText("## persona\nunique-soul-marker\n")

        val repo = MemoryRepository(memoryDir)
        val result = repo.getMemory("", "daily")

        assertFalse(
            "SOUL.md must not be searched as a daily log",
            result.contains("unique-soul-marker"),
        )
        assertTrue("real daily logs must still be searched", result.contains("unique-log-marker"))
    }

    @Test
    fun getMemory_fullDump_excludesSoulFile() {
        memoryDir.mkdirs()
        File(memoryDir, SoulStore.FILE_NAME).writeText("## persona\nunique-soul-marker\n")

        val repo = MemoryRepository(memoryDir)
        val result = repo.getMemory("", "daily")

        assertFalse(result.contains("unique-soul-marker"))
    }

    @Test
    fun deleteFile_refusesSoulFile() {
        memoryDir.mkdirs()
        val soul = File(memoryDir, SoulStore.FILE_NAME)
        soul.writeText("---\nname: RikkaMinis\n---\npersona body\n")

        val repo = MemoryRepository(memoryDir)
        val deleted = repo.deleteFile(SoulStore.FILE_NAME)

        assertFalse("deleteFile must refuse the persona file", deleted)
        assertTrue("SOUL.md must survive the delete attempt", soul.exists())
    }

    @Test
    fun deleteFile_stillDeletesDailyLog() {
        // Control arm for the guard above: it must refuse SOUL.md specifically,
        // not silently break deletion of ordinary logs.
        memoryDir.mkdirs()
        val log = File(memoryDir, "2026-08-25.md")
        log.writeText("<!-- 2026-08-25 10:00:00 -->\n## entry\ncontent\n")

        val repo = MemoryRepository(memoryDir)
        val deleted = repo.deleteFile("2026-08-25.md")

        assertTrue("ordinary daily logs must still be deletable", deleted)
        assertFalse("the daily log file must be gone", log.exists())
    }

    // -- [deep-audit-1029 ISSUE-03] saveFile/readFile call contract --------
    // Settings → Memory's file editor writes SOUL.md through the raw
    // MemoryRepository.saveFile path (it does not go through SoulStore.save,
    // which is why the UI refreshes SoulStore's cache by hand afterwards).
    // Pin the two assumptions that path makes: the bytes land verbatim, and
    // the persona name really is the agreed literal (no drift away from
    // SoulStore.FILE_NAME). Expectations are written as literals on purpose —
    // deriving them from the constant under test would let a rename pass.
    @Test
    fun saveFile_writesSoulPersonaByteExact() {
        memoryDir.mkdirs()
        val repo = MemoryRepository(memoryDir)
        val content = "---\nname: Rikka\n---\n\nbody with unicode \u2713 中文\n"

        repo.saveFile(SoulStore.FILE_NAME, content)

        val written = File(memoryDir, SoulStore.FILE_NAME)
        assertTrue("SOUL.md must exist on disk after saveFile", written.exists())
        assertEquals("saveFile must write the exact bytes", content, written.readText())
        assertEquals(
            "the persona file name is the agreed literal",
            "SOUL.md",
            SoulStore.FILE_NAME,
        )
    }

    @Test
    fun readFile_soulRoundTrip_survivesLargeBody() {
        // The editor screen exists for 200KB+ files; the round trip must be
        // exact at that size, not just on a one-line sample.
        memoryDir.mkdirs()
        val repo = MemoryRepository(memoryDir)
        val content = "A".repeat(200_000) + "\nTAIL-CHUNK\n"

        repo.saveFile(SoulStore.FILE_NAME, content)

        assertEquals(
            "readFile must return exactly what saveFile wrote",
            content,
            repo.readFile(SoulStore.FILE_NAME),
        )
    }

    // ── F-9: dailyLogSizeSummary / largestDailyLogBytes on empty input ──
    // Pre-fix, dailyLogSizes() returned null only when the DIR was missing;
    // an existing-but-filtered-empty dir (all files excluded, e.g. only
    // SOUL.md) produced an empty list and `.first()` threw
    // NoSuchElementException. The summary is called from the memory_rollup
    // prompt path, so a fresh install (dir exists, no daily logs yet) hit
    // this on first use.

    @Test
    fun dailyLogSizeSummary_emptyDirReturnsNullNotThrow() {
        // Dir exists but has no daily logs — the exact F-9 trigger shape.
        memoryDir.mkdirs()
        File(memoryDir, SoulStore.FILE_NAME).writeText("persona only\n")

        val repo = MemoryRepository(memoryDir)

        assertEquals(
            "empty (all-excluded) dir must yield null, not NoSuchElementException",
            null, repo.dailyLogSizeSummary(),
        )
        assertEquals(
            "largestDailyLogBytes on empty dir must be 0",
            0L, repo.largestDailyLogBytes(),
        )
    }

    @Test
    fun dailyLogSizeSummary_missingDirReturnsNullNotThrow() {
        // Control arm: dir entirely absent — the pre-existing null path.
        val repo = MemoryRepository(memoryDir)  // not created

        assertNull(repo.dailyLogSizeSummary())
        assertEquals(0L, repo.largestDailyLogBytes())
    }

    @Test
    fun dailyLogSizeSummary_namesLargestBySize() {
        memoryDir.mkdirs()
        File(memoryDir, "2026-10-12.md").writeText("x".repeat(500))
        File(memoryDir, "2026-10-13.md").writeText("y".repeat(5000))
        File(memoryDir, SoulStore.FILE_NAME).writeText("persona".repeat(99)) // biggest, but excluded

        val repo = MemoryRepository(memoryDir)
        val summary = repo.dailyLogSizeSummary()

        assertNotNull(summary)
        assertTrue(
            "the largest DAILY LOG must be named, not the excluded persona file",
            summary!!.contains("2026-10-13.md"),
        )
        assertFalse(
            "SOUL.md must never be named as a daily log",
            summary.contains(SoulStore.FILE_NAME),
        )
    }

    // ── [dv15-A4] candidate口径 must match the rollup executor ──────────────
    // Pre-fix the filter only dropped three literal file names, so a log that
    // already had a MEMORY-ROLLUP.md section stayed the "largest daily log"
    // forever: the system prompt re-suggested memory_rollup for it on every
    // turn, and the agent burned a call to be told SKIPPED_ALREADY. The
    // candidate set now comes from the same predicate the executor uses.
    // (deep-audit-1036 A-07)

    /** Today as the repository computes it — same formatter, so timezone-stable. */
    private fun todayStamp(): String =
        SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())

    @Test
    fun dailyLogSizes_excludesAlreadyDistilledLog() {
        memoryDir.mkdirs()
        File(memoryDir, "2026-10-12.md").writeText("x".repeat(500))
        File(memoryDir, "2026-10-13.md").writeText("y".repeat(5000))
        File(memoryDir, MemoryRollupEngine.ROLLUP_FILE).writeText(
            "## Rollup 2026-10-13\n### 经验与知识点\n- 已蒸馏\n",
        )

        val repo = MemoryRepository(memoryDir)
        val summary = repo.dailyLogSizeSummary()

        assertNotNull(summary)
        assertTrue(
            "the still-pending log must be the one named",
            summary!!.contains("2026-10-12.md"),
        )
        assertFalse(
            "2026-10-13.md is already distilled — it is not pending",
            summary.contains("2026-10-13.md"),
        )
        assertEquals(
            "largest PENDING log is the 500-byte file, not the distilled 5000-byte one",
            500L,
            repo.largestDailyLogBytes(),
        )
    }

    @Test
    fun dailyLogSizes_excludesNonDateMarkdown() {
        memoryDir.mkdirs()
        File(memoryDir, "2026-10-12.md").writeText("x".repeat(500))
        File(memoryDir, "notes.md").writeText("n".repeat(9000)) // biggest, but not a daily log

        val repo = MemoryRepository(memoryDir)
        val summary = repo.dailyLogSizeSummary()

        assertNotNull(summary)
        assertTrue(summary!!.contains("2026-10-12.md"))
        assertFalse(
            "a non-date markdown file is not a daily log",
            summary.contains("notes.md"),
        )
        assertEquals(500L, repo.largestDailyLogBytes())
    }

    @Test
    fun dailyLogSizes_excludesToday() {
        memoryDir.mkdirs()
        File(memoryDir, "2026-10-12.md").writeText("x".repeat(500))
        File(memoryDir, "${todayStamp()}.md").writeText("t".repeat(9000)) // today: still being written

        val repo = MemoryRepository(memoryDir)
        val summary = repo.dailyLogSizeSummary()

        assertNotNull(summary)
        assertTrue(summary!!.contains("2026-10-12.md"))
        assertFalse(
            "today's log is still being written — never the largest pending one",
            summary.contains(todayStamp()),
        )
        assertEquals(500L, repo.largestDailyLogBytes())
    }

    @Test
    fun dailyLogSizes_allDistilled_returnsNullNotThrow() {
        memoryDir.mkdirs()
        File(memoryDir, "2026-10-12.md").writeText("x".repeat(9000))
        File(memoryDir, MemoryRollupEngine.ROLLUP_FILE).writeText(
            "## Rollup 2026-10-12\n- 已蒸馏\n",
        )

        val repo = MemoryRepository(memoryDir)

        assertNull(
            "every dated log is distilled — nothing pending",
            repo.dailyLogSizeSummary(),
        )
        assertEquals("no pending log → 0L", 0L, repo.largestDailyLogBytes())
    }

    @Test
    fun dailyLogSizes_isReadOnly() {
        memoryDir.mkdirs()
        val log = File(memoryDir, "2026-10-12.md")
        log.writeText("x".repeat(500))
        val rollup = File(memoryDir, MemoryRollupEngine.ROLLUP_FILE)
        rollup.writeText("## Rollup 2026-10-11\n- 已蒸馏\n")
        val logBefore = log.readText()
        val rollupBefore = rollup.readText()

        val repo = MemoryRepository(memoryDir)
        repo.dailyLogSizeSummary()
        repo.largestDailyLogBytes()

        assertEquals("the statistics path must not rewrite the log", logBefore, log.readText())
        assertEquals("nor the rollup file it reads", rollupBefore, rollup.readText())
    }
}

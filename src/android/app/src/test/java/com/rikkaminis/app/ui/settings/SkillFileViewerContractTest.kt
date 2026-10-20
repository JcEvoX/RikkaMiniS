package com.rikkaminis.app.ui.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.FileNotFoundException

/**
 * [dv14-B5] deep-audit-1035 ISSUE-06 P1 — the sibling-file disk read of
 * [SkillFileViewerScreen] must ride IO off the composition thread (it used to
 * run inside the `remember {}` lambda, i.e. synchronously on the main thread
 * of every recomposition), and the Edit button must be gated until the read
 * lands (a tap before the read would open the editor against a blank base and
 * a Save could overwrite the real file with "").
 *
 * Two layers:
 *  1. the pure isSkillMd × loaded matrix ([skillFileViewerContent]) — real
 *     JVM execution, expected values hardcoded literals (never derived from
 *     the code under test, so a mutation cannot masquerade as PASS);
 *  2. the wiring shape — source-shape assertions independently reimplemented
 *     (not shared with the scan gate's helpers), the same approach
 *     SkillFileViewerSaveShapeTest uses for the save path.
 *
 * Regression anchors (must keep passing): the save contract
 * `val ok = withContext(Dispatchers.IO)` + `if (ok)` guard + the
 * `preferredId = skillId` pin.
 */
class SkillFileViewerContractTest {

    // -- ① pure matrix: what the viewer shows / what the Edit button seeds --

    @Test
    fun skillMd_alwaysRendersTheFrontmatterMerge() {
        // SKILL.md: the in-memory merge is the source of truth; the disk base
        // is irrelevant (never read).
        assertEquals("merged-body", skillFileViewerContent(true, "merged-body", null))
        assertEquals("merged-body", skillFileViewerContent(true, "merged-body", "disk-text"))
        assertEquals("", skillFileViewerContent(true, "", "disk-text"))
    }

    @Test
    fun siblingFile_rendersTheLoadedDiskBase() {
        // Sibling files: the asynchronously loaded base is the source of
        // truth; blank until it lands (and a stale merge is never shown).
        assertEquals("disk-text", skillFileViewerContent(false, "", "disk-text"))
        assertEquals("disk-text", skillFileViewerContent(false, "merged", "disk-text"))
        assertEquals("", skillFileViewerContent(false, "", null))
        assertEquals("", skillFileViewerContent(false, "merged", null))
    }

    // -- ② wiring shape: async read + Edit gate (source-shape assertions) --

    private val screen: String by lazy {
        readRepoFile("app/src/main/java/com/rikkaminis/app/ui/settings/SkillsManagementScreen.kt")
    }

    private val segment: String by lazy {
        screen.substringAfter("fun SkillFileViewerScreen(")
    }

    @Test
    fun siblingRead_ridesWithContextIO() {
        // The read must sit inside a `withContext(Dispatchers.IO) { ... }`
        // block — not inside the remember lambda (main-thread composition).
        val readIdx = segment.indexOf("skillRepository.readSkillFile(")
        assertTrue("screen must read the sibling file via readSkillFile(", readIdx >= 0)
        val ioIdx = segment.lastIndexOf("withContext(Dispatchers.IO)", readIdx)
        assertTrue("readSkillFile( must follow a withContext(Dispatchers.IO)", ioIdx >= 0)
        val open = segment.indexOf('{', segment.indexOf(')', ioIdx))
        assertTrue(open > ioIdx)
        var depth = 0
        var close = -1
        for (i in open until segment.length) {
            when (segment[i]) {
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) {
                        close = i
                        break
                    }
                }
            }
        }
        assertTrue("withContext(Dispatchers.IO) block must be balanced", close > open)
        assertTrue(
            "readSkillFile( must sit inside the withContext(Dispatchers.IO) block",
            readIdx in open..close,
        )
    }

    @Test
    fun siblingRead_isLaunchedFromALaunchedEffect() {
        // The read must be triggered from a LaunchedEffect (async load), not
        // a synchronous composition-phase call.
        val readIdx = segment.indexOf("skillRepository.readSkillFile(")
        assertTrue(readIdx >= 0)
        val leIdx = segment.lastIndexOf("LaunchedEffect(", readIdx)
        assertTrue("readSkillFile( must follow a LaunchedEffect(", leIdx >= 0)
        val block = segment.substring(leIdx, readIdx)
        assertTrue(
            "readSkillFile( must sit in the LaunchedEffect's body (no closing brace between)",
            block.indexOf('{') >= 0 && block.indexOf('}') < 0,
        )
    }

    @Test
    fun editButton_isGatedOnTheAsyncLoad() {
        // The Edit action must be disabled until the read lands.
        val editIdx = segment.indexOf("skill_file_edit")
        assertTrue("screen must host the Edit action", editIdx >= 0)
        val button = segment.substring(
            maxOf(0, editIdx - 600),
            editIdx,
        )
        assertTrue(
            "Edit MinisTextButton must carry enabled = isLoaded",
            button.contains("enabled = isLoaded"),
        )
    }

    // -- ③ regression anchors: the save contract is untouched --

    @Test
    fun saveContract_untouched() {
        assertTrue(
            "save must stay bound via 'val ok = withContext(Dispatchers.IO)'",
            segment.contains("val ok = withContext(Dispatchers.IO)"),
        )
        assertTrue(
            "save exit must stay behind the 'if (ok)' guard",
            segment.contains("if (ok)"),
        )
        assertTrue(
            "preferredId = skillId pin must survive",
            segment.contains("preferredId = skillId"),
        )
    }

    private fun readRepoFile(relative: String): String {
        var dir: File? = File(System.getProperty("user.dir"))
        while (dir != null) {
            val f = File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw FileNotFoundException(
            "$relative not found from user.dir=${System.getProperty("user.dir")}",
        )
    }
}

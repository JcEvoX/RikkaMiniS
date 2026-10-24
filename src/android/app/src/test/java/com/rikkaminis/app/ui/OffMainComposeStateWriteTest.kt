package com.rikkaminis.app.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [deep-audit-1049 B-02] Source-shape probe for the three background writes to
 * Compose `remember { mutableStateOf(…) }` state this branch moves back to Main.
 *
 * All three are *thread* defects in composable code: `ImageGalleryViewer` and
 * `BackupSettingsScreen` run their work on `MinisApp.applicationScope`
 * (`SupervisorJob + Dispatchers.IO`, no Looper), and `ChatScreen`'s cold-open
 * prewarm runs on `Dispatchers.Default`. Writing snapshot state from those
 * threads races recomposition and can throw
 * `IllegalStateException: Reading a state that was created after the snapshot
 * was taken` — not observable from a JVM unit test without a live Compose host,
 * which is why the properties that make each repair safe are asserted on the
 * production text, like the neighbouring `*ShapeTest`s.
 *
 * What each arm pins (and what a mutation of it would mean):
 *
 *  * the *reset* of the gallery's `sharing` / `saving` flags is wrapped in a
 *    Main hop and still sits in the `finally` — dropping either is the baseline
 *    bug (`ImageGalleryViewer.kt:324` / `:360` before the fix);
 *  * the backup screen resolves `webDavConfig` AND reports
 *    `webdav_server_not_configured` inside one Main hop — the pre-fix code read
 *    and wrote both from `Dispatchers.IO` while the comment claimed Main;
 *  * the cold-prewarm *value* is still measured off-Main (moving the whole
 *    prewarm back to Main would put the parse on the frame path) and only the
 *    publish hops;
 *  * the work stays where it was: the gallery still uses the application scope
 *    (not `rememberCoroutineScope`, whose cancellation is what
 *    `fix/audit0917-b8` removed) and the prewarm is still
 *    `launch(Dispatchers.Default)`.
 *
 * Each arm was checked against a tree with all four write sites reverted: the
 * hop arms go red there, while the "still off-main" / "still app scoped" arms
 * stay green — so neither half of a pair can be satisfied by moving everything
 * to Main.
 */
class OffMainComposeStateWriteTest {

    private val pkg = "src/android/app/src/main/java/com/rikkaminis/app"
    private val galleryRel = "$pkg/ui/components/ImageGalleryViewer.kt"
    private val backupRel = "$pkg/ui/settings/BackupSettingsScreen.kt"
    private val chatScreenRel = "$pkg/ui/chat/ChatScreen.kt"

    /** Gradle runs with `user.dir` = the module dir, a local JVM run at the repo
     *  root — walk up until the path resolves (same idiom as
     *  `B3RenderInputShapeTest.repoFile`). */
    private fun repoFile(rel: String): File {
        var dir: File? = File(System.getProperty("user.dir"))
        while (dir != null) {
            val f = File(dir, rel)
            if (f.exists()) return f
            dir = dir.parentFile
        }
        throw AssertionError("$rel not found from user.dir=${System.getProperty("user.dir")}")
    }

    private fun read(rel: String): String = repoFile(rel).readText()

    /** Drops `//` comments, `/* */` blocks and the *contents* of string / char
     *  literals — a KDoc sentence quoting the old shape must not satisfy (or
     *  break) a count, and a `//` inside a URL literal must not swallow the rest
     *  of the line. */
    private fun codeOnly(text: String): String {
        val sb = StringBuilder(text.length)
        var i = 0
        var inLine = false
        var inBlock = false
        var inStr = false
        var inChr = false
        while (i < text.length) {
            val c = text[i]
            val n = if (i + 1 < text.length) text[i + 1] else '\u0000'
            when {
                inLine -> {
                    if (c == '\n') {
                        inLine = false
                        sb.append(c)
                    } else {
                        sb.append(' ')
                    }
                }
                inBlock -> {
                    if (c == '*' && n == '/') {
                        inBlock = false
                        sb.append("  ")
                        i++
                    } else {
                        sb.append(if (c == '\n') c else ' ')
                    }
                }
                inStr -> when {
                    c == '\\' -> {
                        sb.append("  ")
                        i++
                    }
                    c == '"' -> {
                        inStr = false
                        sb.append(c)
                    }
                    else -> sb.append(if (c == '\n') c else ' ')
                }
                inChr -> when {
                    c == '\\' -> {
                        sb.append("  ")
                        i++
                    }
                    c == '\'' -> {
                        inChr = false
                        sb.append(c)
                    }
                    else -> sb.append(if (c == '\n') c else ' ')
                }
                else -> when {
                    c == '/' && n == '/' -> {
                        inLine = true
                        sb.append("  ")
                        i++
                    }
                    c == '/' && n == '*' -> {
                        inBlock = true
                        sb.append("  ")
                        i++
                    }
                    c == '"' -> {
                        inStr = true
                        sb.append(c)
                    }
                    c == '\'' -> {
                        inChr = true
                        sb.append(c)
                    }
                    else -> sb.append(c)
                }
            }
            i++
        }
        return sb.toString()
    }

    private fun count(haystack: String, needle: String): Int {
        var n = 0
        var i = haystack.indexOf(needle)
        while (i >= 0) {
            n++
            i = haystack.indexOf(needle, i + 1)
        }
        return n
    }

    /** Start offsets of every [anchor] whose next [span] chars also hold [near]. */
    private fun sites(text: String, anchor: String, near: String, span: Int = 160): List<Int> {
        val out = ArrayList<Int>()
        var i = text.indexOf(anchor)
        while (i >= 0) {
            val tail = text.substring(i, minOf(text.length, i + span))
            if (near.isEmpty() || tail.contains(near)) out.add(i)
            i = text.indexOf(anchor, i + 1)
        }
        return out
    }

    /** The [lines] source lines immediately BEFORE offset [at]. */
    private fun linesBefore(text: String, at: Int, lines: Int): String {
        val parts = text.substring(0, at).split("\n")
        return parts.drop(maxOf(0, parts.size - 1 - lines)).joinToString("\n")
    }

    private fun assertHopBefore(text: String, at: Int, lines: Int = 2) {
        val before = linesBefore(text, at, lines)
        assertTrue(
            "the write at offset $at must land on Main; found before it:\n$before",
            before.contains("withContext(") && before.contains("Dispatchers.Main"),
        )
    }

    @Test
    fun galleryShareResetHopsToMain() {
        val code = codeOnly(read(galleryRel))
        assertEquals("gallery resets `sharing` exactly once", 1, count(code, "sharing = false"))
        val at = code.indexOf("sharing = false")
        assertHopBefore(code, at)
        // The reset survives a failed share: it lives in the finally.
        assertTrue(
            "the reset stays in a `finally` block",
            linesBefore(code, at, 12).contains("finally"),
        )
    }

    @Test
    fun gallerySaveResetHopsToMain() {
        val code = codeOnly(read(galleryRel))
        assertEquals("gallery resets `saving` exactly once", 1, count(code, "saving = false"))
        val at = code.indexOf("saving = false")
        assertHopBefore(code, at)
        assertTrue(
            "the save reset stays in a `finally` block",
            linesBefore(code, at, 12).contains("finally"),
        )
    }

    @Test
    fun galleryWorkStaysAppScoped() {
        val code = codeOnly(read(galleryRel))
        // The share/save work keeps running on the application scope: binding it
        // to rememberCoroutineScope is what dropped half-written MediaStore rows
        // when the dialog was dismissed mid-save (fix/audit0917-b8).
        assertTrue(
            "the gallery still resolves MinisApp.applicationScope",
            code.contains("?.applicationScope"),
        )
        assertTrue(
            "the reset is non-cancelling so a cancelled job still clears the flag",
            code.contains("NonCancellable + Dispatchers.Main.immediate"),
        )
    }

    @Test
    fun backupNotConfiguredReportHopsToMain() {
        val code = codeOnly(read(backupRel))
        val report = sites(code, "errorMessage = context.getString(", "webdav_server_not_configured")
        assertEquals("exactly one not-configured inline report", 1, report.size)
        assertHopBefore(code, report[0], lines = 3)
        // Both Compose-state touches — read `webDavConfig`, write `errorMessage` —
        // live in that same hop.
        assertTrue(
            "the config is resolved inside a Main hop",
            code.contains("val cfg = withContext(Dispatchers.Main) {"),
        )
        assertTrue("the config read moved into the hop", code.contains("val current = webDavConfig"))
    }

    @Test
    fun backupFailureStillNotifies() {
        val text = read(backupRel)
        // The tray notification is part of the bail arm's contract, not a
        // casualty of moving the write.
        assertTrue(
            "the not-configured arm still posts the tray failure",
            text.contains("tag = \"webdav-upload\""),
        )
        assertTrue(
            "the failure title is unchanged",
            text.contains("R.string.webdav_notify_title_failed"),
        )
    }

    @Test
    fun chatPrewarmPublishHopsToMainButTheWorkDoesNot() {
        val code = codeOnly(read(chatScreenRel))
        assertEquals(
            "the prewarm timing is published once",
            1,
            count(code, "lastColdPrewarmMs = prewarmMs"),
        )
        assertHopBefore(code, code.indexOf("lastColdPrewarmMs = prewarmMs"), lines = 1)
        // Measurement and parse stay off-Main: moving them back would re-add the
        // whole prewarm to the first-frame path.
        assertTrue(
            "the prewarm still runs on Dispatchers.Default",
            code.contains("launch(Dispatchers.Default) {"),
        )
        val measure = "val prewarmMs = (System.nanoTime() - tPrewarmNs) / 1_000_000"
        val measureAt = code.indexOf(measure)
        assertTrue("the timing measurement is unchanged", measureAt >= 0)
        assertTrue(
            "the measured value is what gets published",
            measureAt < code.indexOf("lastColdPrewarmMs = prewarmMs"),
        )
    }
}

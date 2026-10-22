package com.rikkaminis.app.ui.sandbox

import java.io.File
import java.io.FileNotFoundException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1041 B-02] The failure terminal of the three rootfs lifecycle
 * entry points.
 *
 * Bug context: `install` / `resetRootfs` / `repairRootfs` each called
 * `refresh(ctx)` on the SUCCESS path only. `reset()` deletes the rootfs
 * directory BEFORE re-installing, and `autoRepair()`'s last stage is exactly
 * that reset — so a throw mid-extract left `isInstalled` at its pre-operation
 * `true`: the screen kept rendering the green "Installed" badge with the
 * "Install" button hidden over a directory that no longer existed. `refresh()`
 * additionally read `rootfsSize` only for an INSTALLED rootfs, so the previous
 * figure survived a wipe and sat next to "Not installed".
 *
 * The state transition is exercised on the real function; "does every failure
 * arm reach it" cannot run on a JVM (Compose + `ViewModel` + `Context`), so it
 * is pinned as source shape — the house pattern of `MediaHostSourceKeyTest`.
 * Expected counts are dead literals, NOT derived from the production file.
 */
class RootfsManagementViewModelTest {

    // ── 1. the failure terminal itself (real function, no Android) ───────

    @Test
    fun `the failure terminal clears the in-flight and success flags`() {
        val before = RootfsManagementUiState(
            isInstalled = true,
            isProcessing = true,
            statusMessage = "Resetting…",
            resultMessage = null,
            lastOperationSuccess = true,
            rootfsSize = 111L,
            rootfsPath = "/x",
            installProgress = 0.42f,
        )

        val after = rootfsFailureState(before, "Reset failed")

        assertEquals("Reset failed", after.resultMessage)
        assertFalse("a failed operation must not stay in flight", after.isProcessing)
        assertFalse("a failed operation must not claim success", after.lastOperationSuccess)
        assertEquals("the progress bar must be cleared", null, after.installProgress)
    }

    @Test
    fun `the failure terminal does not invent an install state`() {
        // Which rootfs is really on disk is `refresh()`'s verdict (it re-reads
        // the manager). The pure transition must not guess — the defect was
        // precisely a guessed snapshot surviving the operation.
        val before = RootfsManagementUiState(
            isInstalled = true,
            rootfsSize = 111L,
            rootfsPath = "/x",
            statusMessage = "Repairing…",
        )

        val after = rootfsFailureState(before, "msg")

        assertTrue(after.isInstalled)
        assertEquals(111L, after.rootfsSize)
        assertEquals("/x", after.rootfsPath)
        assertEquals("Repairing…", after.statusMessage)
    }

    // ── 2. the wiring: every failure arm settles through the terminal ────

    @Test
    fun `every lifecycle failure arm reaches the shared terminal`() {
        val code = stripComments(readRepoFile(VIEW_MODEL))
        for (fn in LIFECYCLE) {
            val body = bodyOf(code, "fun $fn(")
            val arms = Regex("""catch\s*\([^)]*\)\s*\{""").findAll(body).toList()
            assertTrue("$fn must keep a failure arm", arms.isNotEmpty())
            for (m in arms) {
                val arm = braceBlockAt(body, m.range.last)
                assertTrue(
                    "$fn's catch arm must settle through the shared failure terminal " +
                        "(it must reach refresh()), got: ${arm.take(160)}",
                    arm.contains("settleRootfsFailure("),
                )
            }
        }
    }

    @Test
    fun `each lifecycle entry point subscribes to install progress`() {
        // `install` and `resetRootfs` always did; `repairRootfs` published
        // `installProgress = 0f` and then left the bar frozen at 0% for the
        // whole minutes-long Stage 3 re-extract because nobody collected
        // `manager.installState`.
        val code = stripComments(readRepoFile(VIEW_MODEL))
        for (fn in LIFECYCLE) {
            assertTrue(
                "$fn must subscribe to install progress",
                bodyOf(code, "fun $fn(").contains("observeInstallProgress("),
            )
        }
    }

    @Test
    fun `refresh zeroes the size of a missing rootfs before the guarded size read`() {
        val body = bodyOf(stripComments(readRepoFile(VIEW_MODEL)), "fun refresh(")
        val decision = Regex("""rootfsSize\s*=\s*if\s*\([^)]*isInstalled[^)]*\)[^,\n]*\belse\s+0L?""")
        val match = decision.find(body)
        assertTrue(
            "refresh must zero rootfsSize when the rootfs is not installed (the old " +
                "figure otherwise outlives a wipe and is rendered beside \"Not installed\")",
            match != null,
        )
        val guardedReadAt = body.indexOf("if (manager.isInstalled) {")
        assertTrue("the guarded size read must stay in refresh", guardedReadAt > 0)
        assertTrue(
            "the size decision must sit outside the installed-only branch — clearing it " +
                "inside that branch would flash 0 on every refresh of a healthy rootfs",
            match!!.range.first < guardedReadAt,
        )
    }

    // ── source helpers (same idiom as the other shape tests) ─────────────

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

    /** Body of `<decl>` — brace-balanced, quoted spans excluded. */
    private fun bodyOf(src: String, decl: String): String {
        val at = src.indexOf(decl)
        assertTrue("declaration not found: $decl", at >= 0)
        val open = src.indexOf("{", at)
        return braceBlockAt(src, open)
    }

    /** The `{ … }` block whose opening brace is at [open]. */
    private fun braceBlockAt(src: String, open: Int): String {
        var depth = 0
        var i = open
        while (i < src.length) {
            val c = src[i]
            if (c == '"' || c == '\'') {
                val quote = c
                i++
                while (i < src.length && src[i] != quote) {
                    i += if (src[i] == '\\') 2 else 1
                }
                i++
                continue
            }
            if (c == '{') depth++
            if (c == '}') {
                depth--
                if (depth == 0) return src.substring(open, i + 1)
            }
            i++
        }
        throw AssertionError("unbalanced braces at offset $open")
    }

    /** Removes // and block comments (nesting-aware) while keeping string literals. */
    private fun stripComments(src: String): String {
        val sb = StringBuilder()
        var i = 0
        var quote: Char? = null
        while (i < src.length) {
            val c = src[i]
            if (quote != null) {
                sb.append(c)
                if (c == '\\' && i + 1 < src.length) {
                    sb.append(src[i + 1])
                    i += 2
                    continue
                }
                if (c == quote) quote = null
                i++
                continue
            }
            if (c == '"' || c == '\'') {
                quote = c
                sb.append(c)
                i++
                continue
            }
            if (c == '/' && i + 1 < src.length && src[i + 1] == '/') {
                while (i < src.length && src[i] != '\n') i++
                continue
            }
            if (c == '/' && i + 1 < src.length && src[i + 1] == '*') {
                var nest = 1
                i += 2
                while (i < src.length && nest > 0) {
                    if (src[i] == '*' && i + 1 < src.length && src[i + 1] == '/') {
                        nest--
                        i += 2
                    } else if (src[i] == '/' && i + 1 < src.length && src[i + 1] == '*') {
                        nest++
                        i += 2
                    } else {
                        i++
                    }
                }
                continue
            }
            sb.append(c)
            i++
        }
        return sb.toString()
    }

    private companion object {
        private const val VIEW_MODEL =
            "src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/RootfsManagementViewModel.kt"
        private val LIFECYCLE = listOf("install", "resetRootfs", "repairRootfs")
    }
}

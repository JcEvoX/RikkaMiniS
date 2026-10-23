package com.rikkaminis.app.webapp

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv26-E8] S-08 — the `mount`-scope cold-start decision, exercised for real.
 *
 * The defect is a *race* (shortcut Activity vs. `applyMountedFoldersSnapshot`
 * on `applicationScope` + IO), so there is no pure input→output function in
 * the pre-fix code to test. The fix moves the decision out of the Android
 * layer into [resolveMountWithRetry], which takes its I/O as lambdas — these
 * tests drive that function with a scripted probe/refresh pair, so the
 * assertions are about behaviour (which attempt was accepted, how many times
 * the snapshot was refreshed) and not about source spelling.
 *
 * Layer: real JVM (no Android, no device) + a small source-shape net that the
 * call site keeps the IO contract.
 */
class MountResolvePolicyTest {

    // ── mountRootOf ─────────────────────────────────────────────────────

    @Test
    fun `mount root is derived from the path, not from scopeContext`() {
        assertEquals(
            "/var/minis/mounts/notes",
            mountRootOf("/var/minis/mounts/notes/site/index.html"),
        )
    }

    @Test
    fun `non-mount paths have no mount root`() {
        assertNull(mountRootOf("/var/minis/shared/site/index.html"))
        assertNull(mountRootOf("/var/minis/mounts/"))
        assertNull(mountRootOf("/var/minis/mounts/../shared/index.html"))
        assertNull(mountRootOf(""))
    }

    // ── resolveMountWithRetry ───────────────────────────────────────────

    private class ScriptedProbe(
        /** What `probe` answers, keyed by attempt number (1-based). */
        val answers: List<String?>,
        private val usablePaths: Set<String> = answers.filterNotNull().toSet(),
        private val refreshThrows: Boolean = false,
    ) {
        var probes = 0
            private set
        var refreshes = 0
            private set

        fun probe(path: String): String? = answers.getOrNull(probes++)

        fun usable(path: String): Boolean = path in usablePaths

        fun refresh() {
            refreshes++
            if (refreshThrows) throw IllegalStateException("rootfs not ready")
        }
    }

    private fun resolve(
        path: String,
        script: ScriptedProbe,
        mountKnown: (String) -> Boolean = { false },
    ): String? = resolveMountWithRetry(
        htmlPath = path,
        mountKnown = mountKnown,
        probe = script::probe,
        usable = script::usable,
        refresh = script::refresh,
    )

    @Test
    fun `a usable file is accepted on the first probe, no snapshot refresh`() {
        val script = ScriptedProbe(answers = listOf("/host/notes/site/index.html"))
        val got = resolve("/var/minis/mounts/notes/site/index.html", script)
        assertEquals("/host/notes/site/index.html", got)
        assertEquals("exactly one probe", 1, script.probes)
        assertEquals("no refresh when the first probe already landed", 0, script.refreshes)
    }

    @Test
    fun `cold start refreshes the snapshot once and resolves the retry`() {
        // Attempt 1 = the rootfs placeholder (not usable); attempt 2 = the real
        // host file that only exists after the snapshot was applied.
        val script = ScriptedProbe(
            answers = listOf("/rootfs/var/minis/mounts/notes/site/index.html", "/host/notes/site/index.html"),
            usablePaths = setOf("/host/notes/site/index.html"),
        )
        val got = resolve("/var/minis/mounts/notes/site/index.html", script)
        assertEquals("/host/notes/site/index.html", got)
        assertEquals("the retry must re-probe, not re-read the old File", 2, script.probes)
        assertEquals("exactly one snapshot refresh", 1, script.refreshes)
    }

    @Test
    fun `a registered mount with a missing file does not pay for a refresh`() {
        val script = ScriptedProbe(answers = listOf("/host/notes/gone.html"), usablePaths = emptySet())
        val got = resolve(
            "/var/minis/mounts/notes/gone.html",
            script,
            mountKnown = { it == "/var/minis/mounts/notes" },
        )
        assertNull(got)
        assertEquals(1, script.probes)
        assertEquals("a real deletion is not a snapshot race", 0, script.refreshes)
    }

    @Test
    fun `the retry is bounded - one refresh, two probes, even when nothing resolves`() {
        // The rootfs placeholder is a DIRECTORY: `probe` keeps answering a path
        // that `usable` rejects, which is the shape of the real cold start.
        val script = ScriptedProbe(answers = listOf("/rootfs/var/minis/mounts/notes"), usablePaths = emptySet())
        assertNull(resolve("/var/minis/mounts/notes/index.html", script))
        assertEquals("no unbounded retry loop", MOUNT_RESOLVE_MAX_ATTEMPTS, script.probes)
        assertEquals("at most one refresh", 1, script.refreshes)
    }

    @Test
    fun `a throwing snapshot still ends in source-missing, not a crash`() {
        val script = ScriptedProbe(answers = listOf(null), usablePaths = emptySet(), refreshThrows = true)
        assertNull(resolve("/var/minis/mounts/notes/index.html", script))
        assertEquals(1, script.refreshes)
    }

    @Test
    fun `a non-mount path never refreshes the snapshot`() {
        val script = ScriptedProbe(answers = listOf(null), usablePaths = emptySet())
        assertNull(resolve("/var/minis/shared/site/index.html", script))
        assertEquals(0, script.refreshes)
    }
}

/**
 * Shape net for the wiring the behaviour tests cannot see: the resolver's
 * `mount` arm must go through the policy, and the Activity must keep the
 * resolve call on `Dispatchers.IO` (the policy does blocking host I/O).
 *
 * Expectations are hard-coded literals, not derived from the sources under
 * test — a mutation that reverts the wiring turns these red.
 */
class WebAppMountColdstartWiringTest {

    private fun readRepoFile(relative: String): String {
        var dir: File? = File(System.getProperty("user.dir") ?: ".")
        while (dir != null) {
            val f = File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw java.io.FileNotFoundException("$relative not found from user.dir=" + System.getProperty("user.dir"))
    }

    private val resolver: String = readRepoFile(RESOLVER_REL)
    private val activity: String = readRepoFile(ACTIVITY_REL)

    @Test
    fun `the mount arm resolves through the snapshot-retrying policy`() {
        assertTrue(
            "SCOPE_MOUNT must not go straight to resolveHostPath any more",
            resolver.contains("WebAppShortcutRepository.SCOPE_MOUNT -> resolveMount("),
        )
        assertTrue(
            "the mount arm must funnel through the bounded retry policy",
            resolver.contains("resolveMountWithRetry("),
        )
        assertTrue(
            "the mount arm must be able to re-apply the mount snapshot",
            resolver.contains("applyMountedFoldersSnapshot(context)"),
        )
        assertTrue(
            "a retry without re-probing would re-read the placeholder",
            resolver.contains("PRootKernel.resolveHostPath(path)"),
        )
    }

    @Test
    fun `shared scope keeps its direct resolve`() {
        assertTrue(
            "SCOPE_SHARED must stay untouched (global binds are registered synchronously)",
            resolver.contains("WebAppShortcutRepository.SCOPE_SHARED -> PRootKernel.resolveHostPath(shortcut.htmlPath)"),
        )
    }

    @Test
    fun `the activity still resolves off the main thread`() {
        val resolveAt = activity.indexOf("WebAppPathResolver.resolve(")
        assertTrue("the Activity must still resolve the shortcut", resolveAt > 0)
        val ioAt = activity.lastIndexOf("withContext(Dispatchers.IO)", resolveAt)
        assertTrue(
            "resolve() must stay inside withContext(Dispatchers.IO)",
            ioAt in 0 until resolveAt,
        )
        assertTrue(
            "the null verdict must still be the single source-missing exit",
            activity.indexOf("if (file == null)") in resolveAt until activity.indexOf(
                "renderSourceMissing(sourceSessionId = shortcut.sourceSessionId)",
            ),
        )
        assertFalse(
            "the snapshot must not be applied from the Activity (main thread)",
            activity.contains("applyMountedFoldersSnapshot("),
        )
    }

    private companion object {
        const val RESOLVER_REL =
            "src/android/app/src/main/java/com/rikkaminis/app/webapp/WebAppPathResolver.kt"
        const val ACTIVITY_REL =
            "src/android/app/src/main/java/com/rikkaminis/app/webapp/WebAppActivity.kt"
    }
}

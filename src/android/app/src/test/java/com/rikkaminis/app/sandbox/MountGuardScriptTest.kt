package com.rikkaminis.app.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Regression tests for sweep-1014 FIX-3 (B5a / B5b).
 *
 * B5b: [PRootKernel.isLinuxPathUnderReadOnlyMount] used to match the mount
 * prefix on the RAW string, so `/var/minis/./mounts/...` slipped past the
 * guard while resolveHostPath still resolved the write INTO the read-only
 * mount. The guard now normalizes first ([normalizeLinuxPath]); these tests
 * pin the normalization + prefix composition directly.
 *
 * B5a: the write-guard wrappers used to prefix-match EVERY positional
 * argument, so `cp <read-only> /tmp/` (reading OUT) was refused, while
 * `dd of=<read-only>` wrote straight past the guard. The argument selection
 * is now command-specific ([PRootKernel.mountGuardScript]); the guard
 * semantics are unchanged. The live-run half of the evidence (wrapper
 * execution in a real shell against a simulated read-only mount) cannot
 * live here — it needs a shell — and is covered by the sandbox harness
 * (`shared/work/sweep1014-fix/f3/jvm/liverun.sh`).
 */
class MountGuardScriptTest {

    private fun underReadOnly(p: String): Boolean {
        val normalized = normalizeLinuxPath(p)
        return normalized != null && normalized.startsWith("/var/minis/mounts/")
    }

    // ── B5b: normalization closes the `.` / `//` bypass ─────────────────────

    @Test
    fun `plain read-only path is matched`() {
        assertTrue(underReadOnly("/var/minis/mounts/vault/f.md"))
    }

    @Test
    fun `dot-segment spelling is not a bypass`() {
        assertTrue(underReadOnly("/var/minis/./mounts/vault/f.md"))
    }

    @Test
    fun `double-slash spelling is not a bypass`() {
        assertTrue(underReadOnly("//var/minis/mounts/vault/f.md"))
    }

    @Test
    fun `dotdot segments inside the mount are not a bypass`() {
        assertTrue(underReadOnly("/var/minis/mounts/../mounts/vault/f.md"))
    }

    @Test
    fun `dotdot crossing into the tree is not a bypass`() {
        assertTrue(underReadOnly("/var/../var/minis/mounts/vault/f.md"))
    }

    @Test
    fun `sibling and outside paths are still writable`() {
        assertTrue(!underReadOnly("/var/minis/mounts-x/f.md"))
        assertTrue(!underReadOnly("/var/minis/workspace/f.md"))
        assertTrue(!underReadOnly("/var/minis/mounts"))
    }

    @Test
    fun `normalization is POSIX lexical`() {
        assertEquals("/var/minis/mounts/vault",
            normalizeLinuxPath("/var//minis/./mounts/../mounts/vault"))
        // POSIX: /.. = /
        assertEquals("/", normalizeLinuxPath("/var/.."))
        assertEquals("/var", normalizeLinuxPath("/var/../var"))
        // a relative path climbing above its root has no mount check
        assertNull(normalizeLinuxPath("../escape"))
    }

    // ── B5a: wrapper scripts select write targets per command ───────────────

    @Test
    fun `cp mv ln check only the LAST positional argument`() {
        assertEquals(setOf("cp", "mv", "ln"), PRootKernel.MOUNT_GUARD_LAST_ARG_CMDS)
        val cp = PRootKernel.mountGuardScript("cp")
        // the selection block collects the last non-flag argument
        assertTrue(cp.contains("dest=\"\""))
        assertTrue(cp.contains("check_prefix \"\$dest\" || exit 1"))
        // marker + config path + exec line preserved
        assertTrue(cp.contains(PRootKernel.GUARD_MARKER))
        assertTrue(cp.contains("/var/minis/.mount-readonly-prefixes"))
        assertTrue(cp.endsWith("exec /bin/busybox cp \"\$@\"\n"))
    }

    @Test
    fun `dd checks the value of of= only`() {
        val dd = PRootKernel.mountGuardScript("dd")
        // of= must be split off the argument before the prefix match
        assertTrue(dd.contains("of=*) check_prefix \"\${arg#of=}\" || exit 1;;"))
        // oflag=/ofs= must not be confused with of=
        assertTrue(dd.contains("oflag=*|ofs=*) ;;"))
        assertTrue(!dd.contains("dest=\"\""))
    }

    @Test
    fun `every-argument commands check each positional argument`() {
        val touch = PRootKernel.mountGuardScript("touch")
        assertTrue(touch.contains("check_prefix \"\$arg\" || exit 1"))
        assertTrue(!touch.contains("dest=\"\""))
        assertTrue(!touch.contains("of=*)"))
        // every guarded command keeps the exec line so nothing is shadowed
        for (name in listOf("touch", "tee", "cp", "mv", "mkdir", "rm", "rmdir", "ln", "dd")) {
            val s = PRootKernel.mountGuardScript(name)
            assertTrue(s.endsWith("exec /bin/busybox $name \"\$@\"\n"))
        }
    }
}

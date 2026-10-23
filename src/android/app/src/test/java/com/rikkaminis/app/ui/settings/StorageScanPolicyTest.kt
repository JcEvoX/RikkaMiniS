package com.rikkaminis.app.ui.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [fix/dv25-b3] — deep-audit-1045 B-03 (P1).
 *
 * The Storage page recomputes a process-lifetime snapshot (a ~125k-entry rootfs
 * lstat walk, the DB size, a recursive sizing of every session dir, the orphan
 * scan) and caches it so a re-entry does not repeat the storm. Two shapes were
 * wrong, and neither is reachable from a single-threaded unit test:
 *
 *  1. six Compose state variables on the list screen (plus three on the
 *     per-session detail screen) were assigned INSIDE
 *     `withContext(Dispatchers.IO)`. Compose state has no cross-thread
 *     happens-before with the frame rendering it, and two overlapping reloads
 *     could interleave their assignments — so the rows, the totals and the
 *     cached snapshot could each belong to a different scan;
 *  2. nothing invalidated that cache when a rootfs install / repair / reset
 *     rewrote the whole shell tree, so `shellSize` was pinned for the life of
 *     the process (the rescan path reuses the cached totals instead of
 *     re-walking the rootfs).
 *
 * What is pinned here: the freshness decision ([decideScan], extracted so the
 * 3-minute contract is testable at all), the "no state written off Main" shape
 * (with a negative control that feeds the detector the pre-fix shape), and the
 * single invalidation entry plus its ordering against each mutating
 * `RootfsManager` operation. Expected values are literals, never derived from
 * the code under test, so a mutation cannot masquerade as a pass.
 *
 * NOT asserted: the ~125k-entry walk itself, the `Semaphore(8)` fan-out, and
 * anything that needs a real Android filesystem — those stay with the CI gate
 * (`scripts/scan/sysinfo_state_thread_check.py`, criteria F + G) and the device.
 */
class StorageScanPolicyTest {

    private val screen: String by lazy {
        readRepoFile("app/src/main/java/com/rikkaminis/app/ui/settings/StorageManagementScreen.kt")
    }
    private val rootfsVm: String by lazy {
        readRepoFile("app/src/main/java/com/rikkaminis/app/ui/sandbox/RootfsManagementViewModel.kt")
    }

    // ── ① the scan decision ────────────────────────────────────────────────

    @Test
    fun aSnapshotInsideTheWindowIsReused() {
        assertEquals(
            "a 3-minute window means 179_999 ms old is still fresh",
            StorageScanDecision.Reuse,
            decideScan(cachedAt = 1_000L, force = false, now = 180_999L, staleMs = 180_000L),
        )
    }

    @Test
    fun theWindowIsInclusiveOnTheStaleSide() {
        assertEquals(
            "exactly staleMs old must rescan — the pre-fix test was `now - at < staleMs`",
            StorageScanDecision.RescanDeferred,
            decideScan(cachedAt = 1_000L, force = false, now = 181_000L, staleMs = 180_000L),
        )
    }

    @Test
    fun aForcedReloadNeverReusesTheSnapshot() {
        assertEquals(
            "reclaim / clear wait on a result, so they must not inherit the stale render " +
                "even when the snapshot is one millisecond old",
            StorageScanDecision.RescanNow,
            decideScan(cachedAt = 1_000L, force = true, now = 1_001L, staleMs = 180_000L),
        )
    }

    @Test
    fun noSnapshotAtAllScansImmediately() {
        assertEquals(
            StorageScanDecision.RescanNow,
            decideScan(cachedAt = null, force = false, now = 5L, staleMs = 180_000L),
        )
    }

    @Test
    fun aBackwardsClockReadsAsFresh() {
        assertEquals(
            "a clock that jumped back must not turn the cache into a rescan storm",
            StorageScanDecision.Reuse,
            decideScan(cachedAt = 2_000L, force = false, now = 1_000L, staleMs = 180_000L),
        )
    }

    // ── ② no Compose state is written off Main ─────────────────────────────

    @Test
    fun noComposeStateIsAssignedInsideABackgroundBlock() {
        assertEquals(
            "every background block must only COMPUTE; the writes belong after it, on " +
                "the composition dispatcher",
            emptyList<String>(),
            stateWritesInsideBackgroundBlocks(screen),
        )
    }

    @Test
    fun theDetectorStillFlagsThePreFixShape() {
        // The pre-fix shape, quoted: the two writes and the multi-line named
        // argument that is NOT a write.
        val preFix = """
            fun StorageManagementScreen() {
                var shellSize by remember { mutableLongStateOf(0L) }
                var dbSize by remember { mutableLongStateOf(0L) }
                scope.launch {
                    withContext(Dispatchers.IO) {
                        shellSize = report.totalBytes
                        dbSize = databaseSize(context)
                        StorageSnapshot(
                            shellSize = shellSize,
                        )
                    }
                }
            }
        """.trimIndent()
        assertEquals(
            "the detector must flag the two assignments and skip the call argument",
            listOf("6:shellSize", "7:dbSize"),
            stateWritesInsideBackgroundBlocks(preFix),
        )
    }

    @Test
    fun aWriteInsideALambdaArgumentIsStillFlagged() {
        // The clear-confirm callback is a lambda inside `AlertDialog(…)`, i.e.
        // nested in an argument list AND in statement position. Paren depth
        // alone reads those writes as named arguments and goes blind on them —
        // the first version of the detector did exactly that, on two real sites.
        val nested = """
            fun StorageManagementScreen() {
                var minisSize by remember { mutableLongStateOf(0L) }
                AlertDialog(
                    confirmButton = {
                        scope.launch {
                            withContext(Dispatchers.IO) {
                                minisSize = sizeOf(dir)
                            }
                        }
                    },
                )
            }
        """.trimIndent()
        assertEquals(
            "a block nested in an argument list is still statement position",
            listOf("7:minisSize"),
            stateWritesInsideBackgroundBlocks(nested),
        )
    }

    @Test
    fun theScanResultIsPublishedAfterTheBackgroundBlock() {
        val code = codeOnly(screen)
        val block = code.indexOf("val scanned = withContext(Dispatchers.IO) {")
        val publish = code.indexOf("shellSize = scanned.shellSize")
        assertTrue("the scan is no longer captured as a value", block > 0)
        assertTrue("the scan result is no longer published", publish > 0)
        assertTrue("publish must follow the background block", block < publish)
    }

    // ── ③ one invalidation entry, called before every mutation ─────────────

    @Test
    fun theCacheHasExactlyOneClearingSite() {
        val sites = codeOnly(screen).lines().count {
            it.trim() == "StorageSnapshotCache.snapshot = null"
        }
        assertEquals(
            "a second clearing site is how the next mutation path comes to miss it",
            1,
            sites,
        )
    }

    @Test
    fun theClearingSiteIsReachableFromOutsideTheFile() {
        assertTrue(
            "the clearing site must be a non-private file-level function so " +
                "RootfsManagementViewModel can call it",
            codeOnly(screen).contains("internal fun invalidateStorageSnapshot()"),
        )
    }

    @Test
    fun installingInvalidatesBeforeItMutates() {
        assertInvalidatesBefore("install", "manager.installIfNeeded()")
    }

    @Test
    fun resettingInvalidatesBeforeItMutates() {
        assertInvalidatesBefore("resetRootfs", "manager.reset()")
    }

    @Test
    fun repairingInvalidatesBeforeItMutates() {
        assertInvalidatesBefore("repairRootfs", "manager.autoRepair()")
    }

    @Test
    fun aRepairThatFindsNothingToDoKeepsTheCache() {
        val code = codeOnly(rootfsVm)
        val start = code.indexOf("fun repairRootfs(")
        assertTrue("repairRootfs missing", start > 0)
        val healthy = code.indexOf("if (initial.healthy) {", start)
        val invalidation = code.indexOf("invalidateStorageSnapshot()", start)
        assertTrue("the healthy early-return is gone", healthy > start)
        assertTrue("repairRootfs no longer invalidates the cache", invalidation > start)
        assertTrue(
            "a repair that changes nothing must not throw the cache away — that cache is " +
                "what keeps the ~125k-entry walk off the nav transition",
            healthy < invalidation,
        )
    }

    // ── helpers ────────────────────────────────────────────────────────────

    private fun assertInvalidatesBefore(funName: String, mutation: String) {
        val code = codeOnly(rootfsVm)
        val start = code.indexOf("fun $funName(")
        assertTrue("$funName missing from the ViewModel", start > 0)
        val invalidation = code.indexOf("invalidateStorageSnapshot()", start)
        val mutate = code.indexOf(mutation, start)
        assertTrue("$funName no longer invalidates the cached storage scan", invalidation > start)
        assertTrue("$funName no longer performs $mutation", mutate > start)
        assertTrue(
            "$funName must invalidate BEFORE $mutation: an interruption between the two " +
                "would leave the stale shell total pinned",
            invalidation < mutate,
        )
    }

    /**
     * Mirror of the CI gate's criterion F, kept here so the shape is also
     * enforced by the unit-test step (the two fail independently).
     * Returns `"<line>:<stateName>"` for every state assignment inside a
     * `withContext(Dispatchers.IO|Default) { … }` block.
     */
    private fun stateWritesInsideBackgroundBlocks(src: String): List<String> {
        val code = codeOnly(src)
        val stateNames = Regex(
            """^\s*var\s+(\w+)\s*(?::\s*[^=\n]+)?\s+by\s+remember\b""",
            RegexOption.MULTILINE,
        ).findAll(code).map { it.groupValues[1] }.toSet()
        val lines = code.lines()
        val brackets = innermostBrackets(code)
        val out = mutableListOf<String>()
        for (block in backgroundBlockSpans(code)) {
            val shadowed = mutableSetOf<String>()
            for (ln in block.first..block.second) {
                val text = lines[ln - 1].trim()
                val decl = Regex("""^(?:val|var)\s+(\w+)\b""").find(text)
                if (decl != null) {
                    shadowed.add(decl.groupValues[1])
                    continue
                }
                // `(` = in a call's argument list, so `name = value` is a named
                // argument; `{` = inside a block (body / lambda), i.e. statements.
                if (brackets[ln - 1] == "(") continue
                for (name in stateNames) {
                    if (name in shadowed) continue
                    if (Regex("""^${Regex.escape(name)}\s*(?:\+=|-=|=)(?!=)""").containsMatchIn(text)) {
                        out.add("$ln:$name")
                    }
                }
            }
        }
        return out
    }

    private fun backgroundBlockSpans(code: String): List<Pair<Int, Int>> =
        Regex("""withContext\(\s*Dispatchers\.(?:IO|Default)\s*\)\s*\{""").findAll(code)
            .mapNotNull { m -> braceSpan(code, m.range.last) }
            .toList()

    /** Innermost still-open bracket at the start of each line ("", "(", "{" …). */
    private fun innermostBrackets(code: String): List<String> {
        val out = mutableListOf<String>()
        val stack = ArrayDeque<Char>()
        for (line in code.lines()) {
            out.add(if (stack.isEmpty()) "" else stack.last().toString())
            for (ch in line) {
                when (ch) {
                    '(', '{', '[' -> stack.addLast(ch)
                    ')', '}', ']' -> if (stack.isNotEmpty()) stack.removeLast()
                }
            }
        }
        return out
    }

    /** 1-based (first, last) line of the `{`-block opening at [openIdx]. */
    private fun braceSpan(code: String, openIdx: Int): Pair<Int, Int>? {
        var depth = 0
        for (i in openIdx until code.length) {
            when (code[i]) {
                '{' -> depth++
                '}' -> {
                    depth--
                    if (depth == 0) {
                        val lo = code.take(openIdx).count { it == '\n' } + 1
                        val hi = code.take(i).count { it == '\n' } + 1
                        return lo to hi
                    }
                }
            }
        }
        return null
    }

    /** Comments blanked out line by line (KDoc blocks and trailing `//`). */
    private fun codeOnly(src: String): String {
        val out = StringBuilder()
        var inBlock = false
        for (line in src.lines()) {
            val trimmed = line.trimStart()
            if (inBlock) {
                if (trimmed.contains("*/")) inBlock = false
                out.append('\n')
                continue
            }
            if (trimmed.startsWith("/*")) {
                if (!trimmed.contains("*/")) inBlock = true
                out.append('\n')
                continue
            }
            val noTrailing = if (line.contains("//")) line.substringBefore("//") else line
            out.append(noTrailing).append('\n')
        }
        return out.toString()
    }

    private fun readRepoFile(relative: String): String {
        var dir: java.io.File? = java.io.File(System.getProperty("user.dir") ?: ".")
        while (dir != null) {
            val f = java.io.File(dir, relative)
            if (f.exists()) return f.readText()
            dir = dir.parentFile
        }
        throw java.io.FileNotFoundException(
            "$relative not found from user.dir=${System.getProperty("user.dir")}",
        )
    }
}

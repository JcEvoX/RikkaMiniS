package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Source-shape probe for `finishTar`'s exit code on a refused path
 * (deep-audit-1019 ISSUE-03, second half).
 *
 * `finishTar(entry, failed, …)` turns `failed` into the process exit code
 * (`if (failed) 1 else 0`). A denied archive path means NOTHING was written, so
 * it must exit non-zero — the old code passed `false` there and returned 0 with
 * an error object in the body, which `set -e` and `&&` chains read as success.
 *
 * This edge cannot be driven on the JVM: `handleTar` needs a `Context`, the
 * path mapper and the offload gate. The wiring is therefore asserted against
 * the compiled source, the same probe shape `FastioWiringTest` uses. The probe
 * has a negative control that flips the argument in a copy of the real source
 * and asserts the very same check fails — without one, a deleted call would
 * show up as a green test that checks nothing.
 */
class FastioTarExitCodeShapeTest {

    private val handlerFile = "src/main/java/com/rikkaminis/app/sandbox/offload/FastioOffloadHandler.kt"

    /** `finishTar(deniedEntry(…), <flag>, …)` — the flag is the exit code. */
    private val deniedCall = Regex(
        "finishTar\\(deniedEntry\\(.*?\\)\\s*,\\s*(true|false)\\s*,",
        RegexOption.DOT_MATCHES_ALL,
    )

    /** Returns the number of sites checked; throws when one passes `false`. */
    private fun assertDeniedCallsFail(handler: String): Int {
        val hits = deniedCall.findAll(handler).toList()
        assertTrue(
            "expected both denied-path finishTar sites (archive + dest); found ${hits.size}",
            hits.size >= 2,
        )
        for (hit in hits) {
            assertEquals(
                "a denied path writes nothing, so finishTar must be told it failed " +
                    "(found '${hit.groupValues[1]}' at char ${hit.range.first})",
                "true",
                hit.groupValues[1],
            )
        }
        return hits.size
    }

    // ── probes ──────────────────────────────────────────────────────────────

    @Test
    fun `every denied archive or dest path reports failure through finishTar`() {
        assertEquals(2, assertDeniedCallsFail(source(handlerFile)))
    }

    @Test
    fun `negative control - flipping the flag to false must be caught`() {
        val handler = source(handlerFile)
        val hit = deniedCall.find(handler)!!
        val flag = hit.groups[1]!!
        val mutated = handler.substring(0, flag.range.first) + "false" + handler.substring(flag.range.last + 1)
        assertTrue("the negative control must actually change the source", mutated != handler)
        assertThrows(AssertionError::class.java) { assertDeniedCallsFail(mutated) }
    }

    @Test
    fun `negative control - the check itself fails when the site is deleted`() {
        val handler = source(handlerFile)
        val withoutOne = handler.replaceFirst("finishTar(deniedEntry(", "finishTar(entryFrom(")
        assertTrue(withoutOne != handler)
        assertThrows(AssertionError::class.java) { assertDeniedCallsFail(withoutOne) }
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private fun source(rel: String): String = sourceFile(rel).readText()

    private fun sourceFile(rel: String): File {
        val viaAndroid = "src/android/app/$rel"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            if (File(dir, rel).isFile) return File(dir, rel)
            if (File(dir, viaAndroid).isFile) return File(dir, viaAndroid)
            dir = dir.parentFile
        }
        error("'$rel' not found from ${File(".").absoluteFile}")
    }
}

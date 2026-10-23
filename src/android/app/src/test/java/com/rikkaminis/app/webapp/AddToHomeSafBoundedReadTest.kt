package com.rikkaminis.app.webapp

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [deep-audit-1047 B-02] The picked launcher icon must be read with a STREAM
 * bound, not just a bounded decode.
 *
 * Bug context: `loadBitmapFromUri` ran
 * `openInputStream(uri)?.use { it.readBytes() }` and handed the array to
 * [ImageDecoding].decodeBytesBounded. That bound is on PIXELS — it samples the
 * decode — but the bytes it is handed were already materialised, so a
 * several-hundred-MB pick allocated its full size in the heap before any bound
 * applied. The gate's `declared-size precheck` classification is what hid it:
 * a provider that reports no SIZE (or lies) has nothing to precheck.
 *
 * This test pins the two things that can rot silently:
 *  1. the site routes through the repo's single bounded stream reader
 *     (`readAtMost`) with a `MAX_*_BYTES` cap — asserted on the shipped text,
 *     with the pre-fix `readBytes()` restored as a negative control;
 *  2. `readAtMost` itself still stops one byte past the cap and reports the
 *     overflow as null instead of truncating (behavioural, the real function).
 *
 * Expected sizes are dead literals, never derived from the constants under test.
 */
class AddToHomeSafBoundedReadTest {

    private companion object {
        const val SHEET = "app/src/main/java/com/rikkaminis/app/webapp/AddToHomeSheet.kt"
        const val READER = "app/src/main/java/com/rikkaminis/app/ui/settings/SafImportRead.kt"
    }

    @Test
    fun `loadBitmapFromUri reads the picked image with a stream cap`() {
        val src = readRepoFile(SHEET)
        assertSheetShape(src)

        // Negative controls: two pre-fix shapes.
        assertProbeRejects(
            src.replace(
                "    val bytes = context.contentResolver.openInputStream(uri)?.use { input ->\n" +
                    "        readAtMost(input, MAX_ICON_SOURCE_BYTES)\n" +
                    "    } ?: return@runCatching null\n",
                "    val bytes = context.contentResolver.openInputStream(uri)?.use { it.readBytes() }\n",
            ),
            ::assertSheetShape,
        )
        assertProbeRejects(
            src.replace("readAtMost(input, MAX_ICON_SOURCE_BYTES)", "input.readBytes()"),
            ::assertSheetShape,
        )
    }

    @Test
    fun `the icon cap is a named MAX_BYTES constant, not a literal`() {
        val src = readRepoFile(SHEET)
        assertTrue(
            "the cap must be a named MAX_*_BYTES constant (the gate derives its cap set from that)",
            src.contains("internal const val MAX_ICON_SOURCE_BYTES = "),
        )
        assertTrue(
            "the cap must actually be passed to the reader",
            src.contains("readAtMost(input, MAX_ICON_SOURCE_BYTES)"),
        )
    }

    @Test
    fun `the bounded reader stops one byte past the cap`() {
        // The behaviour the site now depends on: 8 bytes with a 4-byte cap is
        // an overflow (null), 4 bytes with a 4-byte cap is accepted whole. A
        // `while (total < cap)` loop would silently truncate instead.
        val reader = readRepoFile(READER)
        assertTrue(
            "readAtMost must compare the running total against the cap",
            reader.contains("if (total > cap) return null"),
        )
        assertTrue(
            "readAtMost must report an overflow as null, not as a truncated array",
            reader.contains("internal fun readAtMost(input: InputStream, cap: Long): ByteArray?"),
        )
    }

    private fun assertSheetShape(src: String) {
        assertTrue(
            "loadBitmapFromUri must read through the bounded stream reader",
            src.contains("readAtMost(input, MAX_ICON_SOURCE_BYTES)"),
        )
        // The bound is on the bytes; the decode bound stays in place too.
        assertTrue(
            "the decode must stay bounded",
            src.contains("ImageDecoding.decodeBytesBounded(bytes, maxEdge)?.bitmap"),
        )
        assertEquals(
            "the pre-fix whole-file read must not come back",
            0,
            countOccurrences(src, "it.readBytes()"),
        )
    }

    private fun assertProbeRejects(mutated: String, probe: (String) -> Unit) {
        var rejected = false
        try {
            probe(mutated)
        } catch (_: AssertionError) {
            rejected = true
        }
        assertTrue("negative control: the probe still accepts the pre-fix shape", rejected)
    }

    private fun countOccurrences(haystack: String, needle: String): Int {
        var count = 0
        var i = haystack.indexOf(needle)
        while (i >= 0) {
            count++
            i = haystack.indexOf(needle, i + needle.length)
        }
        return count
    }

    private fun readRepoFile(relative: String): String {
        var dir: java.io.File? = java.io.File(System.getProperty("user.dir"))
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

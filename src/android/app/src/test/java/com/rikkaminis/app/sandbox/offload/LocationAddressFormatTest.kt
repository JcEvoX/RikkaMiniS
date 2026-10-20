package com.rikkaminis.app.sandbox.offload

import java.io.File
import java.io.FileNotFoundException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv14-A4] ISSUE-07 — the address-line assembly is ONE function again.
 *
 * The handler spelled it out three times and the copies had drifted:
 * `handleForward` kept `getAddressLine`'s nulls (a sparse index printed the
 * literal `null` inside `address`), `geocode` used `.orEmpty()` (the empty
 * string survives into `joinToString(", ")`, giving a `", "` gap), and
 * `buildLocationJson` was a third, differently-shaped copy of the structured
 * field order. Only the middle one was fixed by the previous batch, and the
 * third was never touched.
 *
 * Two layers on purpose:
 *  - behaviour — [normalizedAddressLines] driven directly, every expectation a
 *    literal (`listOf("A", null, "", "B")` → `["A", "B"]`), never derived from
 *    the implementation;
 *  - structure — the three call sites really delegate, read from the source,
 *    because behaviour alone cannot see a FOURTH copy appear tomorrow.
 *
 * The assembler is deliberately pure JVM (it takes `List<String?>` rather than
 * `android.location.Address`) so this suite needs no Robolectric and no
 * device, and the normalisation cannot drift back one call site at a time.
 */
class LocationAddressFormatTest {

    private val offload = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload"

    // ── behaviour: the indexed address lines ──────────────────────────────

    @Test
    fun `null and blank indexed lines are dropped and the survivors trimmed`() {
        assertEquals(
            listOf("A", "B"),
            normalizedAddressLines(listOf("A", null, "", "B"), emptyList()),
        )
        assertEquals(
            listOf("A", "B"),
            normalizedAddressLines(listOf("  A  ", " ", "\n", "B"), emptyList()),
        )
    }

    @Test
    fun `a sparse index can no longer render the literal null`() {
        // Before: the `handleForward` copy mapped to List<String?> and
        // `joinToString(", ")` over listOf("A", null, "B") produced "A, null, B".
        val lines = normalizedAddressLines(listOf("A", null, "B"), emptyList())
        assertEquals(listOf("A", "B"), lines)
        assertFalse("no element may be the string \"null\": $lines", lines.contains("null"))
        assertEquals("A, B", lines.joinToString(", "))
    }

    @Test
    fun `an empty indexed line no longer leaves a gap in the text`() {
        // Before: `geocode`'s `.orEmpty()` kept the "", so the joined text was
        // "A, , B" — an empty segment between two separators.
        val lines = normalizedAddressLines(listOf("A", "", "B"), emptyList())
        assertEquals(listOf("A", "B"), lines)
        assertEquals("A, B", lines.joinToString(", "))
    }

    // ── behaviour: the structured fallback ────────────────────────────────

    @Test
    fun `all-blank indexed lines fall back to the structured fields in display order`() {
        assertEquals(
            listOf("Baker Street", "Marylebone", "London", "England", "United Kingdom"),
            normalizedAddressLines(
                listOf(null, "", "   "),
                listOf("Baker Street", "Marylebone", "London", "England", "United Kingdom"),
            ),
        )
    }

    @Test
    fun `a maxAddressLineIndex of -1 yields the fallback`() {
        // `(0..-1)` is an empty range, so the indexed side is empty — this is
        // the sparse-placemark case the fallback exists for.
        val maxAddressLineIndex = -1
        val indexed: List<String?> = (0..maxAddressLineIndex).map { null }
        assertEquals(
            listOf("Baker Street", "London"),
            normalizedAddressLines(indexed, listOf("Baker Street", null, " ", "London", "")),
        )
    }

    @Test
    fun `both sides blank yields no lines at all`() {
        assertEquals(
            emptyList<String>(),
            normalizedAddressLines(listOf(null, "", " "), listOf(null, "", "\t")),
        )
        // The [da1034-A4] payload contract: `addr == null` must stay
        // lines [] / count 0 / address "" — the fallback must not invent one.
        assertEquals(emptyList<String>(), normalizedAddressLines(emptyList(), emptyList()))
    }

    @Test
    fun `indexed lines win over the structured fallback`() {
        assertEquals(
            listOf("A"),
            normalizedAddressLines(listOf("A"), listOf("Baker Street", "London")),
        )
    }

    // ── structure: the copies stay gone ───────────────────────────────────

    @Test
    fun `all three address sites delegate to the shared assembler`() {
        val source = codeOnly(read("LocationOffloadHandler.kt"))
        for (marker in listOf(
            "private fun buildLocationJson(",
            "private fun geocode(",
            "private fun handleForward(",
        )) {
            val body = memberSlice(source, marker)
            assertTrue(
                "$marker must assemble through normalizedAddressLines()",
                body.contains("normalizedAddressLines("),
            )
        }
    }

    @Test
    fun `no fourth address assembler can grow back unnoticed`() {
        val source = codeOnly(read("LocationOffloadHandler.kt"))
        fun occurrences(needle: String) = source.split(needle).size - 1
        assertEquals(
            "exactly two getAddressLine call sites (geocode + forward) — a third is the defect",
            2,
            occurrences("getAddressLine("),
        )
        assertEquals(
            "one declaration + three delegations",
            4,
            occurrences("normalizedAddressLines("),
        )
        assertEquals(
            "the structured field order is listed once and used three times",
            4,
            occurrences("structuredAddressFields("),
        )
    }

    // ── helpers ───────────────────────────────────────────────────────────

    /**
     * Comments out, so an assertion never judges the explanation that names
     * the very shapes being forbidden. Hand-written linear scan, same reason
     * as the sibling payload-shape suite: the regex form recurses per
     * character of a block comment and blows the test JVM's stack on the
     * larger handlers.
     */
    private fun codeOnly(src: String): String {
        val out = StringBuilder(src.length)
        var i = 0
        while (i < src.length) {
            val c = src[i]
            if (c == '/' && i + 1 < src.length && src[i + 1] == '/') {
                while (i < src.length && src[i] != '\n') i++
            } else if (c == '/' && i + 1 < src.length && src[i + 1] == '*') {
                i += 2
                while (i + 1 < src.length && !(src[i] == '*' && src[i + 1] == '/')) i++
                i = if (i + 2 > src.length) src.length else i + 2
            } else {
                out.append(c)
                i++
            }
        }
        return out.toString()
    }

    /** Source lines of the class member at [marker], up to the next member. */
    private fun memberSlice(src: String, marker: String): String {
        val lines = src.lines()
        val start = lines.indexOfFirst { it.contains(marker) }
        assertTrue("marker not found: $marker", start >= 0)
        val end = (start + 1 until lines.size).firstOrNull { idx ->
            val t = lines[idx]
            val indent = t.length - t.trimStart(' ').length
            indent in 1..4 && t.trim().isNotEmpty() && !t.trim().startsWith("//") &&
                !t.trim().startsWith("*") &&
                Regex("""^(private|internal|public|protected|fun|val|var|@)""").containsMatchIn(t.trim())
        } ?: lines.size
        return lines.subList(start, end).joinToString("\n")
    }

    private fun read(name: String): String {
        val f = File(repoRoot(), "$offload/$name")
        if (!f.exists()) throw FileNotFoundException(f.path)
        return f.readText()
    }

    private fun repoRoot(): File {
        System.getProperty("probe.repo")?.let { return File(it) }
        var dir: File? = File(System.getProperty("user.dir"))
        while (dir != null) {
            if (File(dir, "$offload/LocationOffloadHandler.kt").exists()) return dir
            dir = dir.parentFile
        }
        throw FileNotFoundException("repo root not found from user.dir=${System.getProperty("user.dir")}")
    }
}

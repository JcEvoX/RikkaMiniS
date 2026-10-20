package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [dv15-A1] Two shapes the shared `OffloadArgs` parser used to get wrong, and
 * the window the sessions command used to answer wrongly:
 *
 *  • A-01 — `--lat -33.86`: a negative number was read as the NEXT FLAG (any
 *    token starting with `-`), so the value never landed and the caller's
 *    `getDouble("lat")` returned null: the query ran unconstrained. It is a
 *    VALUE.
 *  • A-02 — `-f pinned` / `-a <intent>`: a single-dash option was always a
 *    flag, so a declared value-taking short option dropped its value into a
 *    stray positional and the command behaved as if it had not been given.
 *  • A-08 — `--start 2026-10-20 --end 2026-10-13`: both bounds readable but
 *    inside out; the query cannot match, yet the command answered exit 0 with
 *    an empty list, which the caller reads as "no history in that period".
 *
 * Expected values are literals on purpose (an expectation derived from the code
 * under test moves with it). The declaration sites live in files that need an
 * Android Application, so they are pinned as source-shape probes with negative
 * controls, in the OffloadCliArgsTest / FastioWiringTest shape.
 */
class OffloadArgsValueBindingTest {

    // ── A-01: a negative number is a value ──

    @Test
    fun `a negative double after a long option is the value`() {
        val args = OffloadArgs(listOf("--lat", "-33.86"))
        assertEquals("-33.86", args.get("lat"))
        assertEquals(-33.86, args.getDouble("lat")!!, 1e-9)
        assertFalse(args.hasFlag("lat"))
        assertTrue(args.positional.isEmpty())
    }

    @Test
    fun `a negative epoch after a long option is the value`() {
        val args = OffloadArgs(listOf("--since", "-1739773001", "list"))
        assertEquals(-1739773001L, args.getLong("since")!!)
        assertEquals(listOf("list"), args.positional)
    }

    @Test
    fun `a dash word is still the next flag, not a value`() {
        val long = OffloadArgs(listOf("--lat", "--verbose"))
        assertTrue(long.hasFlag("lat"))
        assertTrue(long.hasFlag("verbose"))

        val short = OffloadArgs(listOf("--lat", "-v"))
        assertTrue(short.hasFlag("lat"))
        assertTrue(short.hasFlag("v"))
    }

    // ── [dv16-A A-02] a negative number is a POSITIONAL, not a short option ──

    @Test
    fun `decimal coordinates stay in the positional list`() {
        // `android-photos near -33.86 -122.41` / `android-weather -33.86 -122.41`:
        // the single-dash branch read `-33.86` as the short option `33.86`, so
        // the coordinate vanished from the positional list and the command
        // either refused ("missing argument") or — once a later token filled
        // the slot — answered for ANOTHER place.
        val args = OffloadArgs(listOf("near", "-33.86", "-122.41"))
        assertEquals(listOf("near", "-33.86", "-122.41"), args.positional)
        assertFalse(args.hasFlag("33.86"))
        assertFalse(args.hasFlag("122.41"))
    }

    @Test
    fun `an undeclared digit switch is a negative number, a declared one is a switch`() {
        // `android-shizuku-cli package list -3` (third-party) reads
        // hasFlag("third-party", "3") through this same branch, and the shizuku
        // handler DECLARES "3" — a declared name is the only thing that makes a
        // digit-spelled short flag legal (the declared vocabulary decides, not
        // the token's shape). Without the declaration the same token is the
        // caller's value.
        val declared = OffloadArgs(listOf("package", "list", "-3"), booleanFlags = setOf("3"))
        assertTrue(declared.hasFlag("3"))
        assertEquals(listOf("package", "list"), declared.positional)

        val undeclared = OffloadArgs(listOf("package", "list", "-3"))
        assertFalse(undeclared.hasFlag("3"))
        assertEquals(listOf("package", "list", "-3"), undeclared.positional)
    }

    @Test
    fun `the value short options keep the dv15-A1 contract`() {
        val f = OffloadArgs(listOf("-f", "pinned"), valueShortOptions = setOf("f"))
        assertEquals("pinned", f.get("f"))
        assertFalse(f.hasFlag("f"))
        assertTrue(f.positional.isEmpty())

        // A declared BOOLEAN wins over a value declaration: `-q` stays --quiet.
        val q = OffloadArgs(listOf("-q"), booleanFlags = setOf("q"), valueShortOptions = setOf("q"))
        assertTrue(q.hasFlag("q"))
        assertNull(q.get("q"))
    }

    @Test
    fun `a trailing double dash keeps its reading`() {
        // `--` is not a separator in this parser (unchanged by this fix): a
        // trailing one is an empty-key flag, and the tokens before it keep
        // their positions.
        val args = OffloadArgs(listOf("list", "--"))
        assertEquals(listOf("list"), args.positional)
    }

    @Test
    fun `the equals spelling is untouched`() {
        val args = OffloadArgs(listOf("--lat=-33.86"))
        assertEquals("-33.86", args.get("lat"))
        assertTrue(args.positional.isEmpty())
    }

    // ── A-02: a declared short option takes its value ──

    @Test
    fun `a declared short value option takes the next token`() {
        val args = OffloadArgs(listOf("-f", "pinned", "list"), valueShortOptions = setOf("f", "p", "s"))
        assertEquals("pinned", args.get("filter", "f"))
        assertFalse(args.hasFlag("f"))
        assertEquals(listOf("list"), args.positional)
    }

    @Test
    fun `an undeclared short option is still a flag`() {
        val args = OffloadArgs(listOf("-f", "pinned"), valueShortOptions = setOf("p"))
        assertTrue(args.hasFlag("f"))
        assertNull(args.get("filter", "f"))
        assertEquals(listOf("pinned"), args.positional)
    }

    @Test
    fun `the shizuku short options carry the am start extras`() {
        val args = OffloadArgs(
            listOf("-a", "android.intent.action.VIEW", "-d", "https://example.invalid/x", "-p", "com.foo"),
            valueShortOptions = SHIZUKU_VALUE_SHORT_OPTIONS,
        )
        assertEquals("android.intent.action.VIEW", args.get("action", "a"))
        assertEquals("https://example.invalid/x", args.get("data", "d"))
        assertEquals("com.foo", args.get("package", "p"))
        assertTrue(args.positional.isEmpty())
    }

    @Test
    fun `an output flag still cannot be stolen by a value declaration`() {
        val long = OffloadArgs(listOf("--compact", "list"), valueShortOptions = setOf("compact"))
        assertTrue(long.hasFlag("compact"))
        assertEquals(listOf("list"), long.positional)

        val short = OffloadArgs(listOf("-q", "list"), valueShortOptions = setOf("q"))
        assertTrue(short.hasFlag("q"))
        assertEquals(listOf("list"), short.positional)
    }

    // ── A-08: an inside-out window is refused, a single day is not ──

    @Test
    fun `an inside out window is refused with the real reason`() {
        val thrown = try {
            sessionsDateWindow(2_000L, 1_000L, "2026-10-20", "2026-10-13")
            null
        } catch (e: InvertedDateWindow) {
            e
        }
        assertTrue("an inside out window must be refused", thrown != null)
        assertTrue(
            "the message must name the ordering, not claim a malformed date: ${thrown!!.message}",
            thrown.message!!.contains("--end '2026-10-13' is before --start '2026-10-20'"),
        )
    }

    @Test
    fun `equal instants and a forward window are not refused`() {
        assertEquals(1_000L to 1_000L, sessionsDateWindow(1_000L, 1_000L, "a", "b"))
        assertEquals(1_000L to 2_000L, sessionsDateWindow(1_000L, 2_000L, "a", "b"))
        assertNull(sessionsDateWindow(1_000L, null, "a", null).second)
        assertNull(sessionsDateWindow(null, 1_000L, null, "b").first)
    }

    @Test
    fun `an unreadable bound still refuses exactly as before`() {
        val thrown = try {
            sessionsDateBound("--start", "2026-13-40") { parseSessionsDate(it) }
            null
        } catch (e: InvalidDateArg) {
            e
        }
        assertTrue(thrown != null)
        assertTrue(thrown!!.message!!.contains("2026-13-40"))
        assertNull(sessionsDateBound("--start", null) { parseSessionsDate(it) })
    }

    // ── wiring: the declaration sites are pinned as source shape ──

    @Test
    fun `config declares the short options it reads as values`() {
        val code = code(handler("ConfigOffloadHandler.kt"))
        assertTrue(
            "Config reads filter/f, page/p, page-size/s — all three must be declared",
            code.contains("valueShortOptions = setOf(\"f\", \"p\", \"s\")"),
        )
    }

    @Test
    fun `shizuku declares the short options and passes them to the parser`() {
        val code = code(handler("ShizukuOffloadHandler.kt"))
        assertTrue(code.contains("internal val SHIZUKU_VALUE_SHORT_OPTIONS = setOf(\"a\", \"d\", \"p\", \"c\")"))
        assertEquals(
            "both parser call sites must carry the declaration",
            2,
            Regex(Regex.escape("valueShortOptions = SHIZUKU_VALUE_SHORT_OPTIONS")).findAll(code).count(),
        )
    }

    @Test
    fun `sessions refuses an inside out window through the shared funnel`() {
        val code = code(handler("SessionsOffloadHandler.kt"))
        assertTrue(code.contains("endMs < startMs"))
        assertFalse("a non-strict check would reject the legal single day window", code.contains("endMs <= startMs"))
        assertTrue(code.contains("return sessionsDateWindow(startMs, endMs, args.get(\"start\"), args.get(\"end\"))"))
    }

    // ── negative controls: the probes must fail when the wiring goes away ──

    @Test
    fun `negative control - dropping the short option declaration fails the probe`() {
        val mutated = code(handler("ConfigOffloadHandler.kt"))
            .replace("valueShortOptions = setOf(\"f\", \"p\", \"s\")", "")
        assertTrue(assertProbeFails { assertTrue(mutated.contains("valueShortOptions = setOf(\"f\", \"p\", \"s\")")) })
    }

    @Test
    fun `negative control - a non-strict window check fails the probe`() {
        val mutated = code(handler("SessionsOffloadHandler.kt")).replace("endMs < startMs", "endMs <= startMs")
        assertTrue(assertProbeFails { assertFalse(mutated.contains("endMs <= startMs")) })
    }

    private fun assertProbeFails(probe: () -> Unit): Boolean = try {
        probe()
        false
    } catch (e: AssertionError) {
        true
    }

    private fun handler(name: String): File {
        val rel = "src/main/java/com/rikkaminis/app/sandbox/offload/$name"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val direct = File(dir, rel)
            if (direct.isFile) return direct
            val viaAndroid = File(dir, "src/android/app/$rel")
            if (viaAndroid.isFile) return viaAndroid
            dir = dir.parentFile
        }
        error("'$rel' not found from ${File(".").absoluteFile}")
    }

    /** Strip comments so prose quoting a pattern cannot satisfy a probe. */
    private fun code(file: File): String = code(file.readText())

    private fun code(src: String): String {
        val withoutBlocks = Regex("/\\*.*?\\*/", RegexOption.DOT_MATCHES_ALL).replace(src, "")
        return withoutBlocks.lines().joinToString("\n") { it.substringBefore("//") }
    }
}

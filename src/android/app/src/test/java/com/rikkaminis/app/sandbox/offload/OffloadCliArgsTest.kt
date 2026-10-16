package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * ISSUE-10 (deep-audit-1019, P2, branch `fix/da1019-offload-cli-args`).
 *
 * Two 100%-reproducible agent CLI combos failed before this branch:
 *
 *  A. `android-contacts get --compact 42` and `android-contacts --compact list`
 *     — [ContactsOffloadHandler] dispatched on the RAW argv (`rawArgs[0]`), so
 *     any option before the subcommand took the subcommand slot ("unknown
 *     subcommand '--compact'", exit 2) and `get`/`delete` read the option as
 *     the `<id>`. `[sweep1014-B34]` had fixed only the search query — the same
 *     family hole survived in dispatch, `needsWrite` and get/delete.
 *  B. `minis-config set soul.body --file ./payload.json` — [ConfigOffloadHandler]
 *     resolved the path without the guest cwd, while its KDoc claimed the same
 *     "helper shape" as [ModelUseOffloadHandler], whose `[B14-fix-r14]` join
 *     has carried the cwd since da1015. Every relative `--file` failed.
 *
 * The pure halves live in the two handlers' companion objects
 * (`parseContactsCommand` / `listMax` / `resolveMaybeRelative`) so the
 * behaviour is pinned off-device; the wiring half (dispatch really goes
 * through the parsed args, all three `--file` call sites really pass the cwd)
 * is asserted against the compiled sources, the `FastioWiringTest` shape.
 * Every expected value is a literal, and each probe has a negative control
 * that mutates a copy of the real source and asserts the same check fails.
 */
class OffloadCliArgsTest {

    private val contactsFile = "src/main/java/com/rikkaminis/app/sandbox/offload/ContactsOffloadHandler.kt"
    private val configFile = "src/main/java/com/rikkaminis/app/sandbox/offload/ConfigOffloadHandler.kt"

    // ── A. `android-contacts` subcommand / id resolution ─────────────────────

    private fun parse(vararg argv: String) = ContactsOffloadHandler.parseContactsCommand(argv.toList())

    /** `get --compact 42` used to read "--compact" as the id and exit 2. */
    @Test
    fun `get with an output flag before the id resolves both slots`() {
        val c = parse("get", "--compact", "42")
        assertEquals("get", c.sub)
        assertEquals(42L, c.id!!)
        assertTrue("--compact is an output flag, not a subcommand", c.args.hasFlag("compact"))
    }

    /** `--compact list` used to die with "unknown subcommand '--compact'". */
    @Test
    fun `output flag before the subcommand keeps the subcommand`() {
        val c = parse("--compact", "list")
        assertEquals("list", c.sub)
        assertNull(c.id)
    }

    @Test
    fun `quiet short flag before the id resolves both slots`() {
        val c = parse("delete", "-q", "7")
        assertEquals("delete", c.sub)
        assertEquals(7L, c.id!!)
        assertTrue(c.args.hasFlag("q"))
    }

    @Test
    fun `option between the subcommand and the id still resolves`() {
        val c = parse("delete", "--compact", "42")
        assertEquals("delete", c.sub)
        assertEquals(42L, c.id!!)
    }

    @Test
    fun `plain invocations are unchanged`() {
        assertEquals("list", parse("list").sub)
        assertEquals("search", parse("search", "Alice").sub)
        assertEquals(42L, parse("get", "42").id!!)
        assertEquals(9L, parse("delete", "9").id!!)
    }

    /** `get` with no id stays the exit-2 "missing <id>" path (id == null). */
    @Test
    fun `missing id stays null`() {
        val c = parse("get")
        assertEquals("get", c.sub)
        assertNull(c.id)
    }

    @Test
    fun `a non-numeric id is not silently accepted`() {
        assertNull(parse("delete", "abc").id)
    }

    /** [sweep1014-B34] regression pin: the query is positional[1], not "--max"'s value. */
    @Test
    fun `search query survives a leading value option`() {
        val c = parse("search", "--max", "10", "Alice")
        assertEquals("search", c.sub)
        assertEquals("Alice", c.args.positional.getOrNull(1))
        assertEquals(10, c.args.getInt("max")!!)
    }

    /** Options only, no subcommand: sub stays null (dispatch says so, not "null"). */
    @Test
    fun `option-only argv has no subcommand`() {
        assertNull(parse("--compact").sub)
        assertNull(parse("--max", "10").sub)
        assertNull(parse().sub)
    }

    /** [sweep1014-B26a] `--help list` must not swallow "list" as --help's value. */
    @Test
    fun `help is a boolean flag so it cannot eat the subcommand`() {
        val c = parse("--help", "list")
        assertTrue(c.args.hasFlag("h", "help"))
        assertEquals("list", c.sub)
        assertTrue(parse("-h").args.hasFlag("h", "help"))
    }

    // ── A'. `list --max` resolution (was the raw-argv scan `parseMax`) ────────

    private fun maxOf(vararg argv: String) =
        ContactsOffloadHandler.listMax(OffloadArgs(argv.toList(), booleanFlags = setOf("help", "h")))

    @Test
    fun `max defaults to 50 when absent`() {
        assertEquals(50, maxOf("list"))
    }

    @Test
    fun `max reads the named value after the subcommand`() {
        assertEquals(10, maxOf("list", "--max", "10"))
    }

    /** The `=` spelling was invisible to the old raw-argv scan. */
    @Test
    fun `max reads the equals spelling`() {
        assertEquals(10, maxOf("list", "--max=10"))
    }

    /** The legacy undocumented `-n N` form must not regress. */
    @Test
    fun `max keeps the legacy short form`() {
        assertEquals(10, maxOf("list", "-n", "10"))
    }

    @Test
    fun `unparseable or absent values fall back to the default`() {
        assertEquals(50, maxOf("list", "--max", "abc"))
        assertEquals(50, maxOf("list", "-n", "abc"))
        assertEquals(50, maxOf("list", "-n"))
        assertEquals(50, maxOf("list", "10"))
    }

    // ── B. `minis-config --file` relative path resolution ───────────────────

    @Test
    fun `relative path joins onto an absolute cwd`() {
        assertEquals("/root/ws/val.json", ConfigOffloadHandler.resolveMaybeRelative("val.json", "/root/ws"))
    }

    @Test
    fun `trailing slash cwd does not double the separator`() {
        assertEquals("/root/ws/val.json", ConfigOffloadHandler.resolveMaybeRelative("val.json", "/root/ws/"))
    }

    @Test
    fun `absolute path ignores the cwd`() {
        assertEquals("/abs/x", ConfigOffloadHandler.resolveMaybeRelative("/abs/x", "/root/ws"))
    }

    /** No cwd → unchanged, so the session-aware resolver fails closed. */
    @Test
    fun `missing cwd leaves the path untouched`() {
        assertEquals("val.json", ConfigOffloadHandler.resolveMaybeRelative("val.json", null))
        assertEquals("val.json", ConfigOffloadHandler.resolveMaybeRelative("val.json", ""))
    }

    /** A relative cwd must not be guessed onto — the downstream gate decides. */
    @Test
    fun `relative cwd is not joined`() {
        assertEquals("val.json", ConfigOffloadHandler.resolveMaybeRelative("val.json", "relative-cwd"))
    }

    @Test
    fun `nested relative path keeps its own segments`() {
        assertEquals("/root/ws/sub/val.json", ConfigOffloadHandler.resolveMaybeRelative("sub/val.json", "/root/ws"))
    }

    // ── C. wiring probes (source shape) ─────────────────────────────────────

    private fun assertContactsDispatchParsed(src: String) {
        val code = codeOnly(src)
        assertTrue(
            "handle must parse once and dispatch off the parsed command",
            code.contains("val cmd = parseContactsCommand(rawArgs)"),
        )
        assertTrue(
            "needsWrite must read the parsed subcommand, not the raw argv",
            code.contains("val needsWrite = cmd.sub == \"delete\""),
        )
        assertTrue(
            "dispatch must switch on the parsed subcommand",
            code.contains("when (val sub = cmd.sub)"),
        )
        assertTrue(
            "get must take the id resolved from the parsed positional list",
            code.contains("\"get\" -> doGet(cmd.id, parsedArgs)"),
        )
        assertTrue(
            "delete must take the id resolved from the parsed positional list",
            code.contains("\"delete\" -> doDelete(cmd.id, parsedArgs)"),
        )
        assertFalse(
            "no site may index the raw argv any more (OffloadArgs owns the parsing)",
            code.contains("rawArgs[0]"),
        )
        assertFalse(
            "the raw argv tail must not reach a subcommand body",
            code.contains("rawArgs.drop(1)"),
        )
        assertFalse(
            "the id must come from parsedArgs.positional, never `args.firstOrNull()`",
            code.contains("args.firstOrNull()?.toLongOrNull()"),
        )
        assertFalse(
            "the old raw-argv max scan must be gone",
            code.contains("it == \"--max\" || it == \"-n\""),
        )
        assertTrue(
            "sub must come from positional[0]",
            code.contains("sub = args.positional.getOrNull(0)"),
        )
        assertTrue(
            "id must come from positional[1]",
            code.contains("id = args.positional.getOrNull(1)?.toLongOrNull()"),
        )
        assertTrue(
            "help/h must be declared boolean flags or `--help list` swallows the subcommand",
            code.contains("booleanFlags = setOf(\"help\", \"h\")"),
        )
    }

    private fun assertConfigFilePathResolution(src: String) {
        val code = codeOnly(src)
        assertTrue(
            "the --file resolver must accept the guest cwd",
            code.contains("private fun readLinuxPath(linuxPath: String, sessionId: String?, cwd: String?): String?"),
        )
        assertTrue(
            "the relative join must be its own pure helper",
            code.contains("internal fun resolveMaybeRelative(path: String, cwd: String?): String ="),
        )
        assertTrue(
            "only an absolute cwd joins, and only for a relative path",
            code.contains("if (!path.startsWith(\"/\") && !cwd.isNullOrEmpty() && cwd.startsWith(\"/\"))"),
        )
        assertTrue(
            "the join must produce a single separator",
            code.contains("if (cwd.endsWith(\"/\")) \"\$cwd\$path\" else \"\$cwd/\$path\""),
        )
        assertTrue(
            "the joined path must still go through the session-aware guard (fail-closed)",
            code.contains("PRootKernel.resolvePathForSession(sessionId, resolved, context)"),
        )
        assertTrue(
            "the resolver must use the joined path, not the raw argument",
            code.contains("val resolved = resolveMaybeRelative(linuxPath, cwd)"),
        )
        // All three --file call sites (set / add / set-batch) must forward the cwd.
        assertEquals(
            "every --file call site must pass request.cwd",
            3,
            Regex("readLinuxPath\\(fileArg, request\\.sessionId, request\\.cwd\\)").findAll(code).count(),
        )
        assertFalse(
            "no --file call site may keep the cwd-less call",
            Regex("readLinuxPath\\(fileArg, request\\.sessionId\\)(?!,)").containsMatchIn(code),
        )
    }

    @Test
    fun `contacts dispatch is wired to the parsed args`() {
        assertContactsDispatchParsed(source(contactsFile))
    }

    @Test
    fun `config file resolution is wired to the guest cwd`() {
        assertConfigFilePathResolution(source(configFile))
    }

    // ── C'. negative controls: a mutation must red the very same probe ───────

    @Test
    fun `negative control - raw argv dispatch comes back and the probe fails`() {
        val original = source(contactsFile)
        val mutated = original.replace("when (val sub = cmd.sub)", "when (val sub = rawArgs[0])")
        assertTrue("the parsed dispatch line must exist to be removable", mutated != original)
        assertProbeFails { assertContactsDispatchParsed(mutated) }
    }

    @Test
    fun `negative control - needsWrite off the raw argv and the probe fails`() {
        val original = source(contactsFile)
        val mutated = original.replace(
            "val needsWrite = cmd.sub == \"delete\"",
            "val needsWrite = rawArgs[0] == \"delete\"",
        )
        assertTrue("the parsed needsWrite line must exist to be removable", mutated != original)
        assertProbeFails { assertContactsDispatchParsed(mutated) }
    }

    @Test
    fun `negative control - dropping the cwd from one --file site fails the probe`() {
        val original = source(configFile)
        val mutated = original.replaceFirst(
            "readLinuxPath(fileArg, request.sessionId, request.cwd)",
            "readLinuxPath(fileArg, request.sessionId)",
        )
        assertTrue("an --file call site must exist to be mutated", mutated != original)
        assertProbeFails { assertConfigFilePathResolution(mutated) }
    }

    @Test
    fun `negative control - flat path resolution fails the probe`() {
        val original = source(configFile)
        val mutated = original.replace(
            "if (!path.startsWith(\"/\") && !cwd.isNullOrEmpty() && cwd.startsWith(\"/\"))",
            "if (false)",
        )
        assertTrue("the join guard must exist to be removable", mutated != original)
        assertProbeFails { assertConfigFilePathResolution(mutated) }
    }

    private fun assertProbeFails(probe: () -> Unit) {
        var failed = false
        try {
            probe()
        } catch (_: AssertionError) {
            failed = true
        }
        assertTrue("the probe must fail once the source is mutated", failed)
    }

    // ── source location (same technique as DatabaseVersionGuardTest) ─────────

    /** Strip line + block comments so a comment quoting a bad pattern cannot trip a probe. */
    private fun codeOnly(src: String): String =
        src.replace(Regex("/\\*.*?\\*/", RegexOption.DOT_MATCHES_ALL), "")
            .lines()
            .joinToString("\n") { it.substringBefore("//") }

    private fun source(rel: String): String = sourceFile(rel).readText()

    private fun sourceFile(rel: String): File {
        val viaAndroid = "src/android/app/$rel"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel)
            if (File(d, viaAndroid).isFile) return File(d, viaAndroid)
            dir = d.parentFile
        }
        error("'$rel' not found from ${File(".").absoluteFile}")
    }
}

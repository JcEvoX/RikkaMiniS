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

    // ── A''. the legacy `-n` alias really binds a value (dv14-A1, ISSUE-01) ──
    //
    // `-n 10` used to leave `10` in the POSITIONAL list (OffloadArgs binds no
    // value to a single-dash token), so `getInt("max", "n")` could never see it
    // and `search -n 10 Alice` searched for the name "10" while dropping
    // "Alice". [ContactsOffloadHandler.bindLegacyShortMax] rewrites the alias to
    // `--max` before the parse, for the two subcommands that have a page size.

    private fun searchMaxOf(vararg argv: String): Int =
        ContactsOffloadHandler.listMax(parse(*argv).args, 20)

    @Test
    fun `legacy -n before the query binds the page size and keeps the query`() {
        val c = parse("search", "-n", "10", "Alice")
        assertEquals("search", c.sub)
        assertEquals("Alice", c.args.positional.getOrNull(1))
        assertEquals(10, searchMaxOf("search", "-n", "10", "Alice"))
    }

    @Test
    fun `legacy -n after the query binds the page size too`() {
        val c = parse("search", "Alice", "-n", "10")
        assertEquals("Alice", c.args.positional.getOrNull(1))
        assertEquals(10, searchMaxOf("search", "Alice", "-n", "10"))
    }

    @Test
    fun `legacy -n reaches the list resolver through the parse`() {
        assertEquals(10, ContactsOffloadHandler.listMax(parse("list", "-n", "10").args))
    }

    @Test
    fun `the alias only rewrites a numeric value`() {
        val c = parse("search", "-n", "Alice")
        assertEquals("Alice", c.args.positional.getOrNull(1))
        assertEquals(20, searchMaxOf("search", "-n", "Alice"))
        assertEquals(
            listOf("search", "-n", "Alice"),
            ContactsOffloadHandler.bindLegacyShortMax(listOf("search", "-n", "Alice")),
        )
    }

    @Test
    fun `the alias is only bound for the subcommands that have a page size`() {
        assertEquals(10L, parse("get", "-n", "10").id!!)
        assertEquals(
            listOf("get", "-n", "10"),
            ContactsOffloadHandler.bindLegacyShortMax(listOf("get", "-n", "10")),
        )
    }

    @Test
    fun `the alias is rewritten behind a leading output flag`() {
        assertEquals(
            listOf("--compact", "search", "--max", "10", "Alice"),
            ContactsOffloadHandler.bindLegacyShortMax(listOf("--compact", "search", "-n", "10", "Alice")),
        )
    }

    // ── B. `minis-config --file` relative path resolution ───────────────────

    @Test
    fun `relative path joins onto an absolute cwd`() {
        assertEquals("/root/ws/val.json", resolveMaybeRelative("val.json", "/root/ws"))
    }

    @Test
    fun `trailing slash cwd does not double the separator`() {
        assertEquals("/root/ws/val.json", resolveMaybeRelative("val.json", "/root/ws/"))
    }

    @Test
    fun `absolute path ignores the cwd`() {
        assertEquals("/abs/x", resolveMaybeRelative("/abs/x", "/root/ws"))
    }

    /** No cwd → unchanged, so the session-aware resolver fails closed. */
    @Test
    fun `missing cwd leaves the path untouched`() {
        assertEquals("val.json", resolveMaybeRelative("val.json", null))
        assertEquals("val.json", resolveMaybeRelative("val.json", ""))
    }

    /** A relative cwd must not be guessed onto — the downstream gate decides. */
    @Test
    fun `relative cwd is not joined`() {
        assertEquals("val.json", resolveMaybeRelative("val.json", "relative-cwd"))
    }

    @Test
    fun `nested relative path keeps its own segments`() {
        assertEquals("/root/ws/sub/val.json", resolveMaybeRelative("sub/val.json", "/root/ws"))
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
        // [dv14-A1] ISSUE-01: the page size has ONE resolver, and the legacy
        // `-n` alias must be bound before the parse or its value takes the
        // query slot.
        assertTrue(
            "doSearch must resolve the page size through listMax (search's documented default)",
            code.contains("val max = listMax(parsedArgs, 20)"),
        )
        assertTrue(
            "the legacy -n alias must be bound before the parse",
            code.contains("OffloadArgs(bindLegacyShortMax(argv), booleanFlags"),
        )
        assertTrue(
            "the alias binder must exist as its own pure helper",
            code.contains("internal fun bindLegacyShortMax(argv: List<String>): List<String> {"),
        )
        assertFalse(
            "the dead alias read must not come back (OffloadArgs never fills values[\"n\"])",
            code.contains("getInt(\"max\", \"n\")"),
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
    fun `negative control - the dead -n alias read comes back and the probe fails`() {
        val original = source(contactsFile)
        val mutated = original.replace(
            "val max = listMax(parsedArgs, 20)",
            "val max = parsedArgs.getInt(\"max\", \"n\") ?: 20",
        )
        assertTrue("the resolver call must exist to be removable", mutated != original)
        assertProbeFails { assertContactsDispatchParsed(mutated) }
    }

    @Test
    fun `negative control - unbinding the -n alias fails the probe`() {
        val original = source(contactsFile)
        val mutated = original.replace(
            "OffloadArgs(bindLegacyShortMax(argv), booleanFlags",
            "OffloadArgs(argv, booleanFlags",
        )
        assertTrue("the alias binding must exist to be removable", mutated != original)
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

    // ── D. ModelUseOffloadHandler relative-path wiring (ISSUE-09) ────────────
    //
    // The pure join semantics are covered in section B above (`resolveMaybeRelative`
    // is shared by ConfigOffloadHandler and ModelUseOffloadHandler — same function,
    // same literal expectations). What ISSUE-09 added is the *wiring*: three
    // ModelUse sites must now delegate to it — `--output` normalisation,
    // `readLinuxPath` (was a private inline copy), and the image_url chain
    // (`parseMessages` -> `parseMessageArray` -> `resolveImageUrl`, which had no
    // cwd at all) — and the inline copy must be gone for good. Asserted against
    // the compiled source, FastioWiringTest shape.

    private val modelUseFile =
        "src/main/java/com/rikkaminis/app/sandbox/offload/ModelUseOffloadHandler.kt"

    private fun assertModelUseRelativePathWiring(src: String) {
        val code = codeOnlyStrings(src)
        assertTrue(
            "--output must be normalised through the shared join before the fail-closed guard",
            code.contains("if (raw.startsWith(\"file://\")) raw else resolveMaybeRelative(raw, request.cwd)"),
        )
        assertTrue(
            "readLinuxPath must delegate to the shared join, not keep an inline copy",
            code.contains("val resolved = resolveMaybeRelative(linuxPath, cwd)"),
        )
        assertTrue(
            "resolveImageUrl must accept the guest cwd",
            code.contains(
                "private fun resolveImageUrl(url: String, sessionId: String?, cwd: String? = null): LLMMessage.ImagePart",
            ),
        )
        assertTrue(
            "resolveImageUrl must resolve a relative url against the cwd",
            code.contains("else -> resolveMaybeRelative(url, cwd).takeIf { it.startsWith(\"/\") }"),
        )
        assertTrue(
            "the image_url chain must forward request.cwd from cmdRun",
            code.contains("parseMessages(inputText, request.sessionId, request.cwd)"),
        )
        assertTrue(
            "parseMessageArray must forward the cwd into resolveImageUrl",
            code.contains("imgs.add(resolveImageUrl(url, sessionId, cwd))"),
        )
        assertFalse(
            "no hand-written cwd join may survive (resolveMaybeRelative is the single implementation)",
            code.contains("\"\$cwd"),
        )
    }

    @Test
    fun `model-use relative paths are wired through the shared join`() {
        assertModelUseRelativePathWiring(source(modelUseFile))
    }

    @Test
    fun `negative control - a re-inlined --output guard fails the probe`() {
        val original = source(modelUseFile)
        val mutated = original.replace(
            "if (raw.startsWith(\"file://\")) raw else resolveMaybeRelative(raw, request.cwd)",
            "val normalizedOutput = resolveMaybeRelative(raw, request.cwd)",
        )
        assertTrue("the --output normalisation line must exist to be mutated", mutated != original)
        assertProbeFails { assertModelUseRelativePathWiring(mutated) }
    }

    @Test
    fun `negative control - re-inlining readLinuxPath fails the probe`() {
        val original = source(modelUseFile)
        val mutated = original.replace(
            "val resolved = resolveMaybeRelative(linuxPath, cwd)",
            "val resolved = if (cwd.endsWith(\"/\")) \"\$cwd\$linuxPath\" else \"\$cwd/\$linuxPath\"",
        )
        assertTrue("the delegated join must exist to be mutated", mutated != original)
        assertProbeFails { assertModelUseRelativePathWiring(mutated) }
    }

    @Test
    fun `negative control - dropping the cwd from the image chain fails the probe`() {
        val original = source(modelUseFile)
        val mutated = original.replace(
            "imgs.add(resolveImageUrl(url, sessionId, cwd))",
            "imgs.add(resolveImageUrl(url, sessionId))",
        )
        assertTrue("the image_url call site must exist to be mutated", mutated != original)
        assertProbeFails { assertModelUseRelativePathWiring(mutated) }
    }

    /**
     * Template-aware comment stripper for the ModelUse probes. Two traps:
     * the shared [codeOnly] truncates a line at the FIRST `//` (which would
     * delete the call in `if (raw.startsWith("file://")) raw else
     * resolveMaybeRelative(...)`), and a quote NESTED inside a template
     * expression (`"…'${args.get("input")}'…"`) desyncs any non-recursive
     * scanner. States: 0 = code, 1 = string, 2 = raw string.
     */
    private fun codeOnlyStrings(src: String): String {
        val out = StringBuilder()
        val states = ArrayDeque<Int>()
        val depths = ArrayDeque<Int>()
        states.addLast(0); depths.addLast(0)
        var i = 0
        val n = src.length
        while (i < n) {
            when (states.last()) {
                0 -> {
                    if (src.startsWith("\"\"\"", i)) {
                        states.addLast(2); depths.addLast(0); out.append("\"\"\""); i += 3
                    } else {
                        val c = src[i]
                        when {
                            c == '"' -> { states.addLast(1); depths.addLast(0); out.append(c); i++ }
                            src.startsWith("//", i) -> { while (i < n && src[i] != '\n') i++ }
                            src.startsWith("/*", i) -> {
                                val end = src.indexOf("*/", i + 2)
                                i = if (end < 0) n else end + 2
                            }
                            c == '{' -> { depths.addLast(depths.removeLast() + 1); out.append(c); i++ }
                            c == '}' && depths.last() == 0 && states.size > 1 -> {
                                states.removeLast(); depths.removeLast(); out.append(c); i++
                            }
                            c == '}' -> { depths.addLast(depths.removeLast() - 1); out.append(c); i++ }
                            else -> { out.append(c); i++ }
                        }
                    }
                }
                1 -> {
                    val c = src[i]
                    when {
                        c == '\\' -> { out.append(c); if (i + 1 < n) out.append(src[i + 1]); i += 2 }
                        src.startsWith("\${", i) -> {
                            states.addLast(0); depths.addLast(0); out.append("\${"); i += 2
                        }
                        c == '"' || c == '\n' -> { states.removeLast(); depths.removeLast(); out.append(c); i++ }
                        else -> { out.append(c); i++ }
                    }
                }
                else -> {
                    if (src.startsWith("\"\"\"", i)) {
                        states.removeLast(); depths.removeLast(); out.append("\"\"\""); i += 3
                    } else { out.append(src[i]); i++ }
                }
            }
        }
        return out.toString()
    }

    // ── E. Offload payloads are JSON objects/arrays, never prose (ISSUE-04) ───
    //
    // [da1033-D] `OffloadOutput.formatBody` only *re-shapes* JSON: plain text
    // passes through untouched on the no-flag fast path AND on the parse-failure
    // fallback (OffloadOutput.kt:61-80). So an empty result used to leave
    // `android-photos near` and `android-contacts search|get` printing an
    // English sentence on stdout with exit 0 — every downstream `jq` /
    // `json.loads` broke on the *most common* branch — and `near` handed back a
    // bare JSONArray when it was NOT empty, so even the top-level type flipped
    // with the result. Both handlers are Android-bound (Context /
    // ContentResolver / MediaStore), so the payload contract is pinned here as
    // a shape assertion over the compiled sources with literal expectations;
    // `scripts/scan/offload_cli_args_check.py` (arms N1 / N2) enforces the same
    // rule mechanically, and the branch's probe log parses these literals with
    // a real JSON parser.

    private val photosFile = "src/main/java/com/rikkaminis/app/sandbox/offload/PhotosOffloadHandler.kt"

    /**
     * The two places a payload can be spelled out as a literal: the first
     * argument of `formatBody(` and the operand of a `return`. Comment-aware
     * ([codeOnly]), so prose quoted in a comment neither trips nor satisfies
     * the check.
     */
    private fun payloadLiterals(src: String): List<String> {
        val code = codeOnly(src)
        val out = mutableListOf<String>()
        for (m in Regex("formatBody\\(").findAll(code)) {
            var j = m.range.last + 1
            while (j < code.length && code[j].isWhitespace()) j++
            if (j < code.length && code[j] == '"') literalAt(code, j)?.let { out.add(it) }
        }
        for (m in Regex("\\breturn\\s+(?=\")").findAll(code)) {
            literalAt(code, m.range.last + 1)?.let { out.add(it) }
        }
        return out
    }

    /** The Kotlin string literal (plain or raw) starting at [start], else null. */
    private fun literalAt(code: String, start: Int): String? {
        if (code.startsWith("\"\"\"", start)) {
            val end = code.indexOf("\"\"\"", start + 3)
            return if (end < 0) null else code.substring(start, end + 3)
        }
        var i = start + 1
        while (i < code.length) {
            when (code[i]) {
                '\\' -> i += 2
                '"' -> return code.substring(start, i + 1)
                '\n' -> return null
                else -> i++
            }
        }
        return null
    }

    /** `${…}` interpolations out — the *Before* sentence
     *  `"No photos found within ${radiusKm}km of ($lat, $lon)."` would otherwise
     *  look JSON-shaped because of the brace in `${radiusKm}`. */
    private fun maskTemplates(lit: String): String {
        val out = StringBuilder()
        var i = 0
        while (i < lit.length) {
            if (lit[i] == '$' && i + 1 < lit.length && lit[i + 1] == '{') {
                var depth = 0
                var j = i + 1
                while (j < lit.length) {
                    if (lit[j] == '{') depth++
                    if (lit[j] == '}') {
                        depth--
                        if (depth == 0) break
                    }
                    j++
                }
                i = j + 1
            } else {
                out.append(lit[i])
                i++
            }
        }
        return out.toString()
    }

    /** A literal is JSON when its OWN text carries `{` / `[`. A literal that is
     *  nothing but interpolation is the variable in a template coat. */
    private fun isJsonPayload(lit: String): Boolean {
        val own = maskTemplates(lit).trim('"', ' ', '\n', '\t')
        if (own.isEmpty()) return true
        return own.contains('{') || own.contains('[')
    }

    private fun assertOffloadPayloads(src: String) {
        val code = codeOnly(src)
        for (lit in payloadLiterals(src)) {
            assertTrue("a payload literal must be JSON, got $lit", isJsonPayload(lit))
        }
        assertFalse("the empty-radius sentence must not come back", code.contains("\"No photos found within"))
        assertFalse("no-match must not be a sentence", code.contains("\"No contacts found matching"))
        assertFalse("a null cursor must not be a sentence", code.contains("\"android-contacts: failed to query"))
        assertFalse("a missing id must not be a sentence", code.contains("\"android-contacts: no contact with id="))
    }

    private fun assertNearEnvelope(src: String) {
        val code = codeOnly(src)
        for (key in listOf("media", "count", "lat", "lon", "radius_km")) {
            assertTrue("near must publish \"$key\" in its envelope", code.contains(".put(\"$key\""))
        }
        assertTrue(
            "the hint must ride on the empty branch as a field",
            code.contains("if (hint.isNotEmpty()) data.put(\"hint\", hint)"),
        )
        assertTrue("near must return the envelope", code.contains("return data.toString(2)"))
        assertFalse("near must not return the bare array any more", code.contains("return photos.toString(2)"))
    }

    private fun assertContactsFailureBranches(src: String) {
        val code = codeOnly(src)
        assertTrue(
            "an empty search must return the same [] shape doList returns",
            code.contains("OffloadOutput.formatBody(arr.toString(2), parsedArgs)"),
        )
        assertTrue(
            "a missing id must be an error object carrying the id",
            code.contains(".put(\"error\", \"not_found\")") && code.contains(".put(\"id\", id)"),
        )
        assertEquals("query_failed must have exactly one implementation", 1, Regex("\"query_failed\"").findAll(code).count())
        assertEquals(
            "all three null-cursor sites keep exit 1 with the shared body",
            3,
            Regex("NativeOffloadResult\\(\\s*1,\\s*OffloadOutput\\.formatBody\\(queryFailedBody\\(").findAll(code).count(),
        )
    }

    @Test
    fun `offload payloads are JSON, not prose`() {
        assertOffloadPayloads(source(photosFile))
        assertOffloadPayloads(source(contactsFile))
    }

    @Test
    fun `android-photos near publishes one envelope for empty and non-empty`() {
        assertNearEnvelope(source(photosFile))
    }

    @Test
    fun `android-contacts failure branches stay JSON with exit 1`() {
        assertContactsFailureBranches(source(contactsFile))
    }

    @Test
    fun `negative control - the near sentence fails the probe`() {
        val original = source(photosFile)
        val mutated = original.replace(
            "return data.toString(2)",
            "return \"No photos found within \${radiusKm}km of (\$lat, \$lon).\$hint\"",
        )
        assertTrue("the near payload must exist to be mutated", mutated != original)
        assertProbeFails { assertOffloadPayloads(mutated) }
    }

    @Test
    fun `negative control - a prose search branch fails the probe`() {
        val original = source(contactsFile)
        val mutated = original.replace(
            "OffloadOutput.formatBody(arr.toString(2), parsedArgs)",
            "OffloadOutput.formatBody(\"No contacts found matching 'q'.\", parsedArgs)",
        )
        assertTrue("the empty-search payload must exist to be mutated", mutated != original)
        assertProbeFails { assertOffloadPayloads(mutated) }
    }

    @Test
    fun `negative control - a prose null-cursor branch fails the probe`() {
        val original = source(contactsFile)
        val mutated = original.replace(
            "OffloadOutput.formatBody(queryFailedBody(\"Failed to query contacts.\"), parsedArgs)",
            "OffloadOutput.formatBody(\"android-contacts: failed to query contacts\", parsedArgs)",
        )
        assertTrue("the shared null-cursor body must exist to be mutated", mutated != original)
        assertProbeFails { assertOffloadPayloads(mutated) }
        assertProbeFails { assertContactsFailureBranches(mutated) }
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

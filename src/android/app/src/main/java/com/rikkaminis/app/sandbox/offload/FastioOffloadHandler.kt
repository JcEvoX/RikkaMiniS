package com.rikkaminis.app.sandbox.offload

import android.content.Context
import com.rikkaminis.app.sandbox.GuestPathMapper
import com.rikkaminis.app.sandbox.NativeOffloadHandler
import com.rikkaminis.app.sandbox.NativeOffloadRequest
import com.rikkaminis.app.sandbox.NativeOffloadResult
import com.rikkaminis.app.sandbox.PRootKernel
import com.rikkaminis.app.sandbox.RootfsManager
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.nio.file.FileSystems
import java.nio.file.FileVisitResult
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.SimpleFileVisitor
import java.nio.file.attribute.BasicFileAttributes
import java.util.regex.PatternSyntaxException

/**
 * `minis-fastio` — file-intensive primitives executed by the HOST process on
 * the real filesystem instead of through PRoot's ptrace boundary
 * ([T-minis-fastio]).
 *
 * ## Why
 *
 * PRoot charges ~100-200µs per path-touching syscall (stop/resume round trip
 * plus a POKEDATA rewrite of the translated path, which grows with path
 * length). A directory walk or a recursive delete over N files therefore costs
 * N × tax inside the guest — measured at 206µs per create+write+close and
 * 167µs per unlink. This handler does the same work from the app process with
 * ordinary JVM file calls (~1µs/op), so the guest pays one offload round trip
 * instead of N ptrace stops. The heavier and more file-dense the operation,
 * the larger the win.
 *
 * ## Scope
 *
 * Six primitives, all defined by the same translation + guard:
 *
 *   minis-fastio du <path>...              read-only: entries + apparent bytes per tree
 *   minis-fastio rm [-r] <path>...         destructive: recursive delete
 *   minis-fastio find <path> [opts]        read-only: walk + filter (capped listing)
 *   minis-fastio grep <pattern> <path>...  read-only: line search (capped matches)
 *   minis-fastio cp [-r] <src> <dst>       write: copy file or tree
 *   minis-fastio mv <src> <dst>            write: rename (copy+delete across filesystems)
 *
 * The read-only primitives (`du` / `find` / `grep`) are NOT gated — they grant
 * nothing the agent's `file_read` does not already have. Every WRITE primitive
 * is gated by [OffloadGate] under its own permission row (ASK_ONCE by default):
 * `fastio_rm`, `fastio_cp`, `fastio_mv` — one row per write tool, so a user who
 * trusts deletion does not thereby authorise overwriting.
 *
 * ## Guards on the destructive / write paths
 *
 * Beyond the path guard in [GuestPathMapper] (refuse-instead-of-guess:
 * `..` above `/`, host /dev //proc //sys, symlink escapes), the write
 * primitives refuse:
 *   - user-mounted external folders (`/var/minis/mounts/<name>`) as a WRITE
 *     target — the shell's own read-only-mount wrappers are the authority
 *     there, and this handler cannot see the mount's writability (`mv` refuses
 *     an external SOURCE too, because it deletes it);
 *   - bind-mount roots (`/`, `/var/minis/workspace`, …) as the thing being
 *     replaced or removed (copying INTO such a directory is fine — the target
 *     is then `<root>/<name>`, which the handler resolves first);
 *   - an existing target unless `--force` is passed (refuse-instead-of-guess:
 *     an accidental overwrite has no undo);
 *   - a target inside its own source (`cp -r /a /a/b` never terminates).
 */
class FastioOffloadHandler(private val context: Context) : NativeOffloadHandler {

    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(
            // `find`-style single-dash long options (`-name`, `-type`, `-limit`)
            // are rewritten to their `--` spelling first: OffloadArgs reads any
            // `-word` as a short FLAG, so `find /usr -name "*.log"` would
            // otherwise parse `-name` as a flag and `*.log` as a positional
            // path. Rewriting here leaves the parser shared by all ~46 handlers
            // untouched.
            normalizeLongOptionSpelling(request.argv.drop(1)),
            // `--recursive` / `--force` / `--ignore-case` are booleans, not
            // `--key value` pairs. Without this declaration OffloadArgs consumes
            // the NEXT token as the option's value, so `rm --recursive /tmp/x`
            // lost the path and reported "missing <path>" instead of deleting
            // anything.
            booleanFlags = setOf("recursive", "force", "ignore-case"),
        )
        if (args.hasFlag("h", "help")) return NativeOffloadResult(0, HELP)

        val sub = args.positional.firstOrNull()
        val paths = args.positional.drop(1)
        return when (sub) {
            null -> NativeOffloadResult(2, usage("missing subcommand", args))
            "du" -> du(paths, args, request)
            "rm" -> rm(paths, args, request)
            "find" -> find(paths, args, request)
            "grep" -> grep(paths, args, request)
            "cp" -> cp(paths, args, request)
            "mv" -> mv(paths, args, request)
            else -> NativeOffloadResult(2, usage("unknown subcommand '$sub'", args))
        }
    }

    // ── du ──────────────────────────────────────────────────────────────────

    private fun du(
        paths: List<String>,
        args: OffloadArgs,
        request: NativeOffloadRequest,
    ): NativeOffloadResult {
        if (paths.isEmpty()) return NativeOffloadResult(2, usage("du: missing <path>", args))

        val bindings = bindingsFor(request)
        val maxEntries = (args.getLong("max-entries") ?: DEFAULT_MAX_ENTRIES)
            .coerceIn(1L, HARD_MAX_ENTRIES)
        val startedNs = System.nanoTime()

        val results = JSONArray()
        var totalFiles = 0L
        var totalDirs = 0L
        var totalBytes = 0L
        var truncated = false
        var failures = 0

        for (raw in paths) {
            val entry = JSONObject()
            when (val resolution = GuestPathMapper.resolve(raw, request.cwd, bindings)) {
                is GuestPathMapper.Resolution.Denied -> {
                    failures++
                    results.put(deniedEntry(raw, resolution))
                }
                is GuestPathMapper.Resolution.Ok -> {
                    entry.put("path", resolution.guestPath).put("host", resolution.hostPath)
                    val path = Paths.get(resolution.hostPath)
                    if (!Files.exists(path, LinkOption.NOFOLLOW_LINKS)) {
                        failures++
                        entry.put("error", "not_found")
                    } else {
                        val totals = WalkTotals()
                        walkForSize(path, totals, maxEntries)
                        entry.put("files", totals.files)
                            .put("dirs", totals.dirs)
                            .put("bytes", totals.bytes)
                        if (totals.truncated) entry.put("truncated", true)
                        if (totals.errors.isNotEmpty()) {
                            entry.put("errors", JSONArray(totals.errors))
                            failures++
                        }
                        totalFiles += totals.files
                        totalDirs += totals.dirs
                        totalBytes += totals.bytes
                        truncated = truncated || totals.truncated
                    }
                    results.put(entry)
                }
            }
        }

        val body = JSONObject()
            .put("results", results)
            .put("totals", JSONObject().put("files", totalFiles).put("dirs", totalDirs).put("bytes", totalBytes))
            .put("elapsed_ms", (System.nanoTime() - startedNs) / 1_000_000)
        if (truncated) body.put("truncated", true)
        return NativeOffloadResult(if (failures > 0) 1 else 0, OffloadOutput.formatBody(body.toString(2), args) + "\n")
    }

    // ── rm ──────────────────────────────────────────────────────────────────

    private fun rm(
        paths: List<String>,
        args: OffloadArgs,
        request: NativeOffloadRequest,
    ): NativeOffloadResult {
        if (paths.isEmpty()) return NativeOffloadResult(2, usage("rm: missing <path>", args))

        // Destructive: gate before touching anything. Denied → PERMISSION_DENIED
        // envelope pointing at Settings → Permissions (OffloadGate builds it).
        OffloadGate.enforce(TOOL_NAME, DISPLAY_NAME, args, request)?.let { return it }

        // OffloadArgs does not split combined short options (`-rf` lands as one
        // flag "rf"), and teaching the shared parser to split them would change
        // argv handling for all ~46 handlers. Accept the combined spellings an
        // agent actually types instead — `rm -rf` failing with "is a directory;
        // pass -r" was the single most likely way to call this tool wrong.
        val recursive = args.hasFlag("r", "recursive", "rf", "fr", "R")
        val bindings = bindingsFor(request)
        val startedNs = System.nanoTime()

        val results = JSONArray()
        var deletedFiles = 0L
        var deletedDirs = 0L
        var freedBytes = 0L
        var failures = 0

        for (raw in paths) {
            val entry = JSONObject()
            when (val resolution = GuestPathMapper.resolve(raw, request.cwd, bindings)) {
                is GuestPathMapper.Resolution.Denied -> {
                    failures++
                    results.put(deniedEntry(raw, resolution))
                }
                is GuestPathMapper.Resolution.Ok -> {
                    entry.put("path", resolution.guestPath).put("host", resolution.hostPath)
                    val path = Paths.get(resolution.hostPath)
                    val refusal = rmRefusal(resolution, path, recursive)
                    if (refusal != null) {
                        failures++
                        entry.put("error", refusal.first).put("detail", refusal.second)
                    } else {
                        val totals = WalkTotals()
                        deleteTree(path, totals)
                        entry.put("deleted_files", totals.files)
                            .put("deleted_dirs", totals.dirs)
                            .put("freed_bytes", totals.bytes)
                        if (totals.errors.isNotEmpty()) {
                            entry.put("errors", JSONArray(totals.errors))
                            failures++
                        }
                        deletedFiles += totals.files
                        deletedDirs += totals.dirs
                        freedBytes += totals.bytes
                    }
                    results.put(entry)
                }
            }
        }

        val body = JSONObject()
            .put("results", results)
            .put(
                "totals",
                JSONObject()
                    .put("deleted_files", deletedFiles)
                    .put("deleted_dirs", deletedDirs)
                    .put("freed_bytes", freedBytes),
            )
            .put("elapsed_ms", (System.nanoTime() - startedNs) / 1_000_000)
        return NativeOffloadResult(if (failures > 0) 1 else 0, OffloadOutput.formatBody(body.toString(2), args) + "\n")
    }

    /**
     * The destructive-path policy, applied AFTER translation. Returns
     * `(errorCode, detail)` when the target must not be deleted, or null when
     * it may be. Split out so the JVM wiring test can assert the order of the
     * checks against the source text.
     */
    private fun rmRefusal(
        resolution: GuestPathMapper.Resolution.Ok,
        path: Path,
        recursive: Boolean,
    ): Pair<String, String>? {
        if (resolution.binding.isExternalMount) {
            return "external_mount" to
                "'${resolution.guestPath}' is a user-mounted external folder; " +
                "delete it through the shell so the read-only-mount guard applies"
        }
        if (resolution.isBindingRoot) {
            return "bind_mount_root" to
                "'${resolution.guestPath}' is a bind-mount root (${resolution.binding.hostBase}); refusing"
        }
        if (!Files.exists(path, LinkOption.NOFOLLOW_LINKS)) {
            return "not_found" to "'${resolution.guestPath}' does not exist"
        }
        if (Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS) && !recursive) {
            return "is_directory" to
                "'${resolution.guestPath}' is a directory; pass -r to delete it recursively"
        }
        return null
    }

    // ── find ────────────────────────────────────────────────────────────────

    private fun find(
        paths: List<String>,
        args: OffloadArgs,
        request: NativeOffloadRequest,
    ): NativeOffloadResult {
        if (paths.isEmpty()) return NativeOffloadResult(2, usage("find: missing <path>", args))
        unknownOption(args, FIND_OPTIONS)?.let { return NativeOffloadResult(2, usage("find: $it", args)) }
        missingValue(args, "name", "type", "limit", "max-entries")?.let {
            return NativeOffloadResult(2, usage("find: -$it needs a value", args))
        }

        val matcher = args.get("name")?.let { pattern ->
            try {
                FileSystems.getDefault().getPathMatcher("glob:$pattern")
            } catch (e: Exception) {
                return NativeOffloadResult(2, usage("find: bad -name glob '$pattern': ${e.message}", args))
            }
        }
        val typeFilter: Char? = args.get("type")?.let { raw ->
            when (raw.lowercase()) {
                "f" -> 'f'
                "d" -> 'd'
                "l" -> 'l'
                else -> return NativeOffloadResult(2, usage("find: -type must be f, d or l (got '$raw')", args))
            }
        }
        val limit = (args.getInt("limit") ?: DEFAULT_FIND_LIMIT).coerceIn(1, HARD_MATCH_LIMIT)
        val maxEntries = (args.getLong("max-entries") ?: DEFAULT_MAX_ENTRIES)
            .coerceIn(1L, HARD_MAX_ENTRIES)
        val bindings = bindingsFor(request)
        val startedNs = System.nanoTime()

        val results = JSONArray()
        var totalFiles = 0L
        var totalDirs = 0L
        var totalMatched = 0L
        var truncated = false
        var capped = false
        var failures = 0
        var remaining = limit

        for (raw in paths) {
            when (val resolution = GuestPathMapper.resolve(raw, request.cwd, bindings)) {
                is GuestPathMapper.Resolution.Denied -> {
                    failures++
                    results.put(deniedEntry(raw, resolution))
                }
                is GuestPathMapper.Resolution.Ok -> {
                    val entry = JSONObject().put("path", resolution.guestPath).put("host", resolution.hostPath)
                    val path = Paths.get(resolution.hostPath)
                    if (!Files.exists(path, LinkOption.NOFOLLOW_LINKS)) {
                        failures++
                        entry.put("error", "not_found")
                    } else {
                        val outcome = FindOutcome()
                        findTree(path, resolution.guestPath, matcher, typeFilter, remaining, maxEntries, outcome)
                        val matches = JSONArray()
                        for (match in outcome.matches) {
                            matches.put(
                                JSONObject()
                                    .put("path", match.guestPath)
                                    .put("type", match.type.toString())
                                    .put("size", match.size),
                            )
                        }
                        entry.put("matches", matches)
                            .put("files", outcome.files)
                            .put("dirs", outcome.dirs)
                            .put("matched", outcome.matched)
                        if (outcome.truncated) entry.put("truncated", true)
                        if (outcome.matchesCapped) entry.put("matches_capped", true)
                        if (outcome.errors.isNotEmpty()) {
                            entry.put("errors", JSONArray(outcome.errors))
                            failures++
                        }
                        totalFiles += outcome.files
                        totalDirs += outcome.dirs
                        totalMatched += outcome.matched
                        truncated = truncated || outcome.truncated
                        capped = capped || outcome.matchesCapped
                        remaining -= outcome.matches.size
                    }
                    results.put(entry)
                }
            }
        }

        val body = JSONObject()
            .put("results", results)
            .put(
                "totals",
                JSONObject().put("files", totalFiles).put("dirs", totalDirs).put("matched", totalMatched),
            )
            .put("limit", limit)
            .put("elapsed_ms", (System.nanoTime() - startedNs) / 1_000_000)
        if (truncated || capped) body.put("truncated", true)
        if (capped) body.put("matches_capped", true)
        // Compact, not indented: the match list IS the payload here, and
        // pretty-printing 500 records doubles the bytes /bin/cat pulls back
        // through proot — the transfer, not the walk, would become the cost.
        return NativeOffloadResult(if (failures > 0) 1 else 0, OffloadOutput.formatBody(body.toString(), args) + "\n")
    }

    // ── grep ────────────────────────────────────────────────────────────────

    private fun grep(
        paths: List<String>,
        args: OffloadArgs,
        request: NativeOffloadRequest,
    ): NativeOffloadResult {
        if (paths.size < 2) return NativeOffloadResult(2, usage("grep: expected <pattern> <path>...", args))
        unknownOption(args, GREP_OPTIONS)?.let { return NativeOffloadResult(2, usage("grep: $it", args)) }
        missingValue(args, "limit", "max-entries")?.let {
            return NativeOffloadResult(2, usage("grep: -$it needs a value", args))
        }

        val pattern = paths.first()
        val roots = paths.drop(1)
        val regex = try {
            Regex(pattern, if (args.hasFlag("i", "ignore-case")) setOf(RegexOption.IGNORE_CASE) else emptySet())
        } catch (e: PatternSyntaxException) {
            // Dialect stated in the error as well as in the help text: a user
            // coming from ripgrep/PCRE must be able to see WHY a pattern that
            // works there was rejected (variable-length lookbehind and PCRE
            // verbs are not supported).
            return NativeOffloadResult(
                2,
                usage("grep: invalid pattern (java.util.regex, not PCRE): ${e.description}", args),
            )
        }
        val limit = (args.getInt("limit") ?: DEFAULT_GREP_LIMIT).coerceIn(1, HARD_MATCH_LIMIT)
        val maxEntries = (args.getLong("max-entries") ?: DEFAULT_MAX_ENTRIES)
            .coerceIn(1L, HARD_MAX_ENTRIES)
        val bindings = bindingsFor(request)
        val startedNs = System.nanoTime()

        val results = JSONArray()
        var totalMatches = 0L
        var totalScanned = 0L
        var totalBinary = 0L
        var totalLarge = 0L
        var totalLinks = 0L
        var totalBytes = 0L
        var truncated = false
        var capped = false
        var failures = 0
        var remaining = limit

        for (raw in roots) {
            when (val resolution = GuestPathMapper.resolve(raw, request.cwd, bindings)) {
                is GuestPathMapper.Resolution.Denied -> {
                    failures++
                    results.put(deniedEntry(raw, resolution))
                }
                is GuestPathMapper.Resolution.Ok -> {
                    val entry = JSONObject().put("path", resolution.guestPath).put("host", resolution.hostPath)
                    val path = Paths.get(resolution.hostPath)
                    if (!Files.exists(path, LinkOption.NOFOLLOW_LINKS)) {
                        failures++
                        entry.put("error", "not_found")
                    } else {
                        val outcome = GrepOutcome()
                        grepTree(path, resolution.guestPath, regex, remaining, maxEntries, outcome)
                        val matches = JSONArray()
                        for (match in outcome.matches) {
                            matches.put(
                                JSONObject()
                                    .put("path", match.guestPath)
                                    .put("line", match.lineNumber)
                                    .put("text", match.text),
                            )
                        }
                        entry.put("matches", matches)
                            .put("files_scanned", outcome.filesScanned)
                            .put("files_skipped_binary", outcome.filesSkippedBinary)
                            .put("files_skipped_large", outcome.filesSkippedLarge)
                            .put("files_skipped_link", outcome.filesSkippedLink)
                            .put("bytes_scanned", outcome.bytesScanned)
                        if (outcome.truncated) entry.put("truncated", true)
                        if (outcome.matchesCapped) entry.put("matches_capped", true)
                        if (outcome.errors.isNotEmpty()) {
                            entry.put("errors", JSONArray(outcome.errors))
                            failures++
                        }
                        totalMatches += outcome.matches.size
                        totalScanned += outcome.filesScanned
                        totalBinary += outcome.filesSkippedBinary
                        totalLarge += outcome.filesSkippedLarge
                        totalLinks += outcome.filesSkippedLink
                        totalBytes += outcome.bytesScanned
                        truncated = truncated || outcome.truncated
                        capped = capped || outcome.matchesCapped
                        remaining -= outcome.matches.size
                    }
                    results.put(entry)
                }
            }
        }

        val body = JSONObject()
            .put("results", results)
            .put(
                "totals",
                JSONObject()
                    .put("matches", totalMatches)
                    .put("files_scanned", totalScanned)
                    .put("files_skipped_binary", totalBinary)
                    .put("files_skipped_large", totalLarge)
                    .put("files_skipped_link", totalLinks)
                    .put("bytes_scanned", totalBytes),
            )
            .put("engine", "java.util.regex")
            .put("limit", limit)
            .put("elapsed_ms", (System.nanoTime() - startedNs) / 1_000_000)
        if (truncated || capped) body.put("truncated", true)
        if (capped) body.put("matches_capped", true)
        return NativeOffloadResult(if (failures > 0) 1 else 0, OffloadOutput.formatBody(body.toString(), args) + "\n")
    }

    // ── cp / mv ─────────────────────────────────────────────────────────────

    private fun cp(
        paths: List<String>,
        args: OffloadArgs,
        request: NativeOffloadRequest,
    ): NativeOffloadResult {
        OffloadGate.enforce(CP_TOOL_NAME, CP_DISPLAY_NAME, args, request)?.let { return it }
        return transfer(paths, args, request, copy = true)
    }

    private fun mv(
        paths: List<String>,
        args: OffloadArgs,
        request: NativeOffloadRequest,
    ): NativeOffloadResult {
        OffloadGate.enforce(MV_TOOL_NAME, MV_DISPLAY_NAME, args, request)?.let { return it }
        return transfer(paths, args, request, copy = false)
    }

    /**
     * Shared body of `cp` / `mv`: translate, refuse, then hand two host paths to
     * [copyTree] / [moveTree]. The gate has already run by the time this is
     * called — every decision below happens behind it.
     */
    private fun transfer(
        paths: List<String>,
        args: OffloadArgs,
        request: NativeOffloadRequest,
        copy: Boolean,
    ): NativeOffloadResult {
        val verb = if (copy) "cp" else "mv"
        if (paths.size != 2) return NativeOffloadResult(2, usage("$verb: expected <src> <dst>", args))
        unknownOption(args, TRANSFER_OPTIONS)?.let { return NativeOffloadResult(2, usage("$verb: $it", args)) }

        val recursive = args.hasFlag("r", "recursive", "rf", "fr", "R")
        val force = args.hasFlag("force", "f", "rf", "fr")
        val bindings = bindingsFor(request)
        val startedNs = System.nanoTime()

        val srcResolution = GuestPathMapper.resolve(paths[0], request.cwd, bindings)
        val dstResolution = GuestPathMapper.resolve(paths[1], request.cwd, bindings)
        val src = srcResolution as? GuestPathMapper.Resolution.Ok
        val dst = dstResolution as? GuestPathMapper.Resolution.Ok
        val entry = JSONObject()
        if (src == null || dst == null) {
            val denied = (srcResolution as? GuestPathMapper.Resolution.Denied)
                ?: (dstResolution as GuestPathMapper.Resolution.Denied)
            entry.put("error", denied.reason.name.lowercase()).put("detail", denied.detail)
            return finishTransfer(entry, failed = true, startedNs = startedNs, args = args)
        }

        entry.put("src", src.guestPath).put("host", src.hostPath).put("dst", dst.guestPath)
        val srcPath = Paths.get(src.hostPath)
        val dstPath = Paths.get(dst.hostPath)

        // `cp -r a dir` puts a INSIDE dir (shell semantics), so the thing that
        // must not already exist / must not be a mount root is the RESOLVED
        // target, not the dst the caller typed.
        val intoDirectory = Files.isDirectory(dstPath, LinkOption.NOFOLLOW_LINKS)
        val target = if (intoDirectory) dstPath.resolve(srcPath.fileName) else dstPath
        val targetGuest = if (intoDirectory) "${dst.guestPath.trimEnd('/')}/${srcPath.fileName}" else dst.guestPath
        entry.put("target", targetGuest).put("target_host", target.toString())

        val refusal = transferRefusal(src, dst, srcPath, intoDirectory, recursive, copy)
        if (refusal != null) {
            entry.put("error", refusal.first).put("detail", refusal.second)
            return finishTransfer(entry, failed = true, startedNs = startedNs, args = args)
        }
        if (Files.exists(target, LinkOption.NOFOLLOW_LINKS) && !force) {
            entry.put("error", "already_exists")
                .put("detail", "'$targetGuest' already exists; pass --force to overwrite")
            return finishTransfer(entry, failed = true, startedNs = startedNs, args = args)
        }
        if (targetInsideSource(srcPath, target)) {
            entry.put("error", "target_inside_source")
                .put(
                    "detail",
                    "'$targetGuest' is inside '${src.guestPath}'; refusing " +
                        "(a copy into itself never terminates)",
                )
            return finishTransfer(entry, failed = true, startedNs = startedNs, args = args)
        }

        var failed = false
        if (copy) {
            val outcome = CopyOutcome()
            copyTree(srcPath, target, force, outcome)
            entry.put("copied_files", outcome.files)
                .put("copied_dirs", outcome.dirs)
                .put("bytes", outcome.bytes)
            if (outcome.overwritten > 0) entry.put("overwritten", outcome.overwritten)
            if (outcome.errors.isNotEmpty()) {
                entry.put("errors", JSONArray(outcome.errors))
                failed = true
            }
        } else {
            val outcome = MoveOutcome()
            moveTree(srcPath, target, outcome)
            entry.put("method", outcome.method)
            if (outcome.counted) {
                entry.put("copied_files", outcome.files)
                    .put("copied_dirs", outcome.dirs)
                    .put("bytes", outcome.bytes)
            }
            if (outcome.errors.isNotEmpty()) {
                entry.put("errors", JSONArray(outcome.errors))
                failed = true
            }
        }
        return finishTransfer(entry, failed, startedNs, args)
    }

    /**
     * Write-path policy, applied AFTER translation and BEFORE the target is
     * resolved away. Returns `(errorCode, detail)` when the transfer must not
     * happen, or null when it may. Split out so the JVM wiring test can assert
     * the order of the checks against the source text.
     */
    private fun transferRefusal(
        src: GuestPathMapper.Resolution.Ok,
        dst: GuestPathMapper.Resolution.Ok,
        srcPath: Path,
        intoDirectory: Boolean,
        recursive: Boolean,
        copy: Boolean,
    ): Pair<String, String>? {
        if (!Files.exists(srcPath, LinkOption.NOFOLLOW_LINKS)) {
            return "not_found" to "'${src.guestPath}' does not exist"
        }
        if (dst.binding.isExternalMount) {
            return "external_mount" to
                "'${dst.guestPath}' is a user-mounted external folder; write to it through the shell " +
                "so the read-only-mount guard applies"
        }
        if (!copy && src.binding.isExternalMount) {
            return "external_mount" to
                "'${src.guestPath}' is a user-mounted external folder; move it through the shell " +
                "so the read-only-mount guard applies"
        }
        // Only when the mount root ITSELF would be replaced or removed: copying
        // INTO `/var/minis/workspace` resolves to `<root>/<name>`, which is a
        // normal write the caller plainly meant.
        if (dst.isBindingRoot && !intoDirectory) {
            return "bind_mount_root" to "'${dst.guestPath}' is a bind-mount root (${dst.binding.hostBase}); refusing"
        }
        if (!copy && src.isBindingRoot) {
            return "bind_mount_root" to "'${src.guestPath}' is a bind-mount root (${src.binding.hostBase}); refusing"
        }
        if (copy && !recursive && Files.isDirectory(srcPath, LinkOption.NOFOLLOW_LINKS)) {
            return "is_directory" to "'${src.guestPath}' is a directory; pass -r to copy it recursively"
        }
        return null
    }

    private fun finishTransfer(
        entry: JSONObject,
        failed: Boolean,
        startedNs: Long,
        args: OffloadArgs,
    ): NativeOffloadResult {
        val body = JSONObject()
            .put("results", JSONArray().put(entry))
            .put("elapsed_ms", (System.nanoTime() - startedNs) / 1_000_000)
        return NativeOffloadResult(if (failed) 1 else 0, OffloadOutput.formatBody(body.toString(2), args) + "\n")
    }

    // ── shared plumbing ─────────────────────────────────────────────────────

    /**
     * The `-b` table for THIS session, rebuilt from the same inputs proot was
     * launched with: the live global map plus the calling session's own subdirs.
     * `request.sessionId` is the chat session the shell belongs to
     * (`MINIS_CHAT_SESSION_ID`); without it the session-scoped subdirs fall back
     * to the rootfs placeholders rather than guessing another session's tree.
     */
    private fun bindingsFor(request: NativeOffloadRequest): List<GuestPathMapper.Binding> =
        GuestPathMapper.sessionBindings(
            filesDir = context.filesDir.absolutePath,
            sessionId = request.sessionId,
            globalBindings = PRootKernel.bindMounts.toMap(),
            rootfsDir = RootfsManager.getInstance(context).rootfsDir.absolutePath,
        )

    private fun deniedEntry(raw: String, denied: GuestPathMapper.Resolution.Denied): JSONObject =
        JSONObject()
            .put("path", raw)
            .put("error", denied.reason.name.lowercase())
            .put("detail", denied.detail)

    private fun usage(reason: String, args: OffloadArgs): String =
        OffloadOutput.formatBody("minis-fastio: $reason\n$HELP", args) + "\n"

    /**
     * Refuse an option this subcommand does not implement. Without this a typo
     * (`find /x -nmae "*.log"`) parses as an unknown flag, is ignored, and
     * returns every entry — a silently wrong answer, which is worse than an
     * error. Returns the message, or null when every option is known.
     */
    private fun unknownOption(args: OffloadArgs, known: Set<String>): String? {
        val unknown = args.unknownFlags(known)
        return if (unknown.isEmpty()) null else "unknown option --${unknown.first()}; see --help"
    }

    /** An option that needs a value but was written bare (`find /x --name`). */
    private fun missingValue(args: OffloadArgs, vararg names: String): String? =
        names.firstOrNull { args.hasFlag(it) }

    companion object {
        private const val TOOL_NAME = "fastio_rm"
        private const val DISPLAY_NAME = "minis-fastio (delete)"

        // One permission row per WRITE primitive: trusting deletion must not
        // silently authorise overwriting a different file.
        private const val CP_TOOL_NAME = "fastio_cp"
        private const val CP_DISPLAY_NAME = "minis-fastio (copy)"
        private const val MV_TOOL_NAME = "fastio_mv"
        private const val MV_DISPLAY_NAME = "minis-fastio (move)"

        // ponytail: one entry budget for the whole command, not per path — a
        // walk that would run for minutes is a stuck offload worker (the pool
        // is 2 threads and a saturated pool stalls every other tool call).
        // 天花板: trees with more than this many entries report
        // `truncated: true` and under-count; 升级触发: a real task hits the cap.
        private const val DEFAULT_MAX_ENTRIES = 2_000_000L
        private const val HARD_MAX_ENTRIES = 50_000_000L

        /** Match budget for `find` / `grep` listings — see HARD_MATCH_LIMIT. */
        private const val DEFAULT_FIND_LIMIT = 500
        private const val DEFAULT_GREP_LIMIT = 200

        // ponytail: a listing is read back by /bin/cat INSIDE proot, so an
        // unbounded one turns a fast walk into a slow transfer; the cap is what
        // keeps the win. 天花板: a task genuinely needs >50k hits in one call
        // (it can page with -limit and a narrower root).
        // 升级触发: a user hits the cap and asks for more.
        private const val HARD_MATCH_LIMIT = 50_000

        // Options each subcommand understands, for the typo guard. Value-taking
        // options are listed too, so a bare `--name` is reported as "needs a
        // value" rather than as an unknown option.
        private val FIND_OPTIONS = setOf("name", "type", "limit", "max-entries")
        private val GREP_OPTIONS = setOf("i", "ignore-case", "limit", "max-entries")
        private val TRANSFER_OPTIONS = setOf("r", "recursive", "rf", "fr", "R", "force", "f")

        /**
         * Single-dash long options an agent types out of `find`/`grep` habit,
         * rewritten to the `--` spelling OffloadArgs reads as `key value`.
         * Exact matches only, so a path that merely contains a dash is safe.
         */
        private val SHORT_LONG_OPTIONS = setOf(
            "-name", "-type", "-limit", "-max-entries", "-ignore-case",
        )

        private fun normalizeLongOptionSpelling(argv: List<String>): List<String> =
            argv.map { if (it in SHORT_LONG_OPTIONS) "-$it" else it }

        private val HELP = """minis-fastio — file-intensive primitives run by the host process on the real filesystem, bypassing the PRoot syscall tax (~100-200µs/op → ~1µs/op).

Usage:
  minis-fastio du <path>...            Read-only: file/dir counts + apparent bytes per tree (JSON).
                                       Use instead of `du -sb` / `find | wc -l` / `find | xargs stat`
                                       when walking a large tree.
  minis-fastio find <path> [opts]      Read-only: walk a tree and list matches (compact JSON, capped).
                                       Use instead of `find <path> -type f | wc -l` / `find ... -name`
                                       / any `find` that stats every entry on a large tree: it pays
                                       the tax once per entry. A bare `find | wc -l` does no stat and
                                       is already cheap, so keep that one. Totals cover the WHOLE tree
                                       even when the listing is capped.
  minis-fastio grep <pattern> <path>...  Read-only: search file contents line by line.
                                       Pattern is java.util.regex (NOT PCRE / ripgrep): lookahead
                                       and bounded lookbehind work, variable-length lookbehind and
                                       PCRE verbs (\K, (*SKIP)) do not.
  minis-fastio rm [-r] <path>...       Destructive: recursive delete (-r required for directories).
  minis-fastio cp [-r] <src> <dst>     Write: copy a file, or a tree with -r.
  minis-fastio mv <src> <dst>          Write: rename (copy+delete only across filesystems).
  Every write primitive asks for permission first (Settings → Permissions), each under its
  own row: minis-fastio (delete) / (copy) / (move).

Options:
  -r, --recursive    (rm/cp) descend into directories. `-rf` / `-fr` work too; for `rm`, `-f`
                     alone is accepted and ignored (a missing path is still reported as an error
                     — POSIX `rm -f` would stay silent). For `cp`/`mv`, `-f` / `--force` means
                     overwrite.
  --force, -f        (cp/mv) allow replacing an existing target. Without it an existing target is
                     refused with "already_exists" — an accidental overwrite has no undo.
  --name GLOB        (find) match the entry NAME (shell-style glob, e.g. '*.log'). `-name` works.
  --type f|d|l       (find) only regular files / directories / symlinks. `-type` works.
  -i, --ignore-case  (grep) case-insensitive match.
  --limit N          (find/grep) max hits to LIST (find default $DEFAULT_FIND_LIMIT, grep default $DEFAULT_GREP_LIMIT).
                     Totals are still counted over the whole tree.
  --max-entries N    Entry budget for the traversal (default $DEFAULT_MAX_ENTRIES); over budget → "truncated": true
  --compact, -q      Standard output shaping (see other minis-* tools)

Notes:
  - Paths are guest paths (/tmp, /var/minis/workspace, /var/minis/mounts/<name>, …) and are
    translated to the real host paths before use.
  - `du` bytes are the apparent sizes of FILE entries only — directories are counted but contribute
    no bytes. This matches the guest's PATH `du -sb`, which in this rootfs resolves to GNU coreutils
    9.5 (file entries only). busybox `du -sb` DOES count directory st_size and reads larger by
    dirs × directory-size — do not "fix" it back. `rm`'s freed_bytes uses the same file-only
    accounting, so a fully deleted tree reports freed_bytes == its `du` bytes.
  - `find`/`grep` emit COMPACT json (the hit list is the payload); `du`/`rm`/`cp`/`mv` emit indented
    json. "truncated": true means the output is incomplete — either the hit budget was reached
    ("matches_capped": true, the scan/list stops there) or the traversal budget ran out.
  - `grep` skips symlinks, binary files (NUL in the first 8 KiB) and files larger than 8 MiB; the
    per-file skip counts are reported so an empty result is diagnosable.
  - `cp`/`mv` copy a symlink as a LINK (never followed), refuse to overwrite without --force,
    refuse a target inside its own source, and treat an existing directory `dst` as "copy into it"
    (`cp -r a dir` → `dir/a`), like the shell.
  - Write primitives refuse: host /dev //proc //sys, user-mounted external folders as a write
    target (`mv` also an external source, since it deletes it), bind-mount roots as the thing
    being replaced, and `cp` without -r on a directory. Nothing outside the app sandbox is reachable.
  - All subcommands report JSON on stdout; exit code 1 when any path failed, 2 for a usage error.
"""
    }
}

// ── walk engine (top-level for JVM testability; pure, no instance state) ────

/**
 * Entry budget shared by every fastio walk (`du` / `find` / `grep`). One
 * implementation of "have we looked at enough entries" instead of one per
 * primitive — the three copies would be the drift this project keeps paying
 * for.
 */
internal interface EntryBudget {
    var visited: Long
    var truncated: Boolean
}

/** Returns true once the entry budget is exhausted (and flags truncation). */
internal fun charge(budget: EntryBudget, maxEntries: Long): Boolean {
    if (budget.visited >= maxEntries) {
        budget.truncated = true
        return true
    }
    budget.visited++
    return false
}

internal class WalkTotals : EntryBudget {
    var files = 0L
    var dirs = 0L
    var bytes = 0L
    override var visited = 0L
    override var truncated = false
    val errors = ArrayList<String>()
}

/**
 * Sum apparent sizes of FILE entries over the tree without following symlinks
 * (matching `du`'s default). Directories are counted but contribute NO bytes:
 * the guest's PATH `du -sb` resolves to GNU coreutils 9.5 in this rootfs, which
 * reports file entries only. Do not "align" this to busybox — busybox `du -sb`
 * DOES count directory st_size (in-sandbox fixture 2026-09-30: 5-byte file +
 * 2 dirs → GNU du -sb = 5, busybox du -sb = 6909; every directory here reports
 * st_size 3452). File-only accounting is also what keeps `rm`'s freed_bytes
 * cross-checkable against `du`'s bytes for the same tree.
 */
internal fun walkForSize(root: Path, totals: WalkTotals, maxEntries: Long) {
    try {
        Files.walkFileTree(root, object : SimpleFileVisitor<Path>() {
            override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                if (charge(totals, maxEntries)) return FileVisitResult.TERMINATE
                totals.dirs++
                return FileVisitResult.CONTINUE
            }

            override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                if (charge(totals, maxEntries)) return FileVisitResult.TERMINATE
                totals.files++
                totals.bytes += attrs.size()
                return FileVisitResult.CONTINUE
            }

            override fun visitFileFailed(file: Path, exc: IOException): FileVisitResult {
                if (charge(totals, maxEntries)) return FileVisitResult.TERMINATE
                totals.errors.add("$file: ${exc.message}")
                return FileVisitResult.CONTINUE
            }
        })
    } catch (e: Exception) {
        totals.errors.add("$root: ${e.message}")
    }
}

/**
 * Post-order delete: children first, then the directory itself. Errors are
 * collected per entry and the walk continues — a single permission failure
 * deep in a tree must not abort the rest. freed_bytes counts FILE entries
 * only (directories are freed but contribute no bytes) — same accounting
 * as `du`, so a fully deleted tree reports freed_bytes == its `du` bytes.
 */
internal fun deleteTree(root: Path, totals: WalkTotals) {
    try {
        Files.walkFileTree(root, object : SimpleFileVisitor<Path>() {
            override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                try {
                    Files.delete(file)
                    totals.files++
                    totals.bytes += attrs.size()
                } catch (e: Exception) {
                    totals.errors.add("$file: ${e.message}")
                }
                return FileVisitResult.CONTINUE
            }

            override fun postVisitDirectory(dir: Path, exc: IOException?): FileVisitResult {
                if (exc != null) {
                    totals.errors.add("$dir: ${exc.message}")
                    return FileVisitResult.CONTINUE
                }
                try {
                    Files.delete(dir)
                    totals.dirs++
                } catch (e: Exception) {
                    totals.errors.add("$dir: ${e.message}")
                }
                return FileVisitResult.CONTINUE
            }

            override fun visitFileFailed(file: Path, exc: IOException): FileVisitResult {
                totals.errors.add("$file: ${exc.message}")
                return FileVisitResult.CONTINUE
            }
        })
    } catch (e: Exception) {
        totals.errors.add("$root: ${e.message}")
    }
}

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
import java.nio.file.FileVisitResult
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.SimpleFileVisitor
import java.nio.file.attribute.BasicFileAttributes

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
 * ## Scope (deliberately small)
 *
 * Two primitives, both defined by the same translation + guard:
 *
 *   minis-fastio du <path>...      read-only: entries + apparent bytes per tree
 *   minis-fastio rm [-r] <path>... destructive: recursive delete
 *
 * Read-only `du` is NOT gated — it grants nothing the agent's `file_read`
 * does not already have. `rm` IS gated ([OffloadGate], ASK_ONCE by default),
 * because it deletes real files with no undo.
 *
 * ## Guards on the destructive path
 *
 * Beyond the path guard in [GuestPathMapper] (refuse-instead-of-guess:
 * `..` above `/`, host /dev //proc //sys, symlink escapes), `rm` refuses:
 *   - user-mounted external folders (`/var/minis/mounts/<name>`) — the shell's
 *     own read-only-mount wrappers are the authority there, and this handler
 *     cannot see the mount's writability;
 *   - bind-mount roots (`/`, `/var/minis/workspace`, …) — deleting the mount
 *     root is never what the caller meant;
 *   - directories without `-r`, and anything that does not exist.
 */
class FastioOffloadHandler(private val context: Context) : NativeOffloadHandler {

    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val args = OffloadArgs(
            request.argv.drop(1),
            // `--recursive` / `--force` are booleans, not `--key value` pairs.
            // Without this declaration OffloadArgs consumes the NEXT token as
            // the option's value, so `rm --recursive /tmp/x` lost the path and
            // reported "missing <path>" instead of deleting anything.
            booleanFlags = setOf("recursive", "force"),
        )
        if (args.hasFlag("h", "help")) return NativeOffloadResult(0, HELP)

        val sub = args.positional.firstOrNull()
        val paths = args.positional.drop(1)
        return when (sub) {
            null -> NativeOffloadResult(2, usage("missing subcommand", args))
            "du" -> du(paths, args, request)
            "rm" -> rm(paths, args, request)
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

    /**
     * Sum apparent sizes over the tree without following symlinks (matching
     * `du`'s default). Directory entries contribute their own `size()` too —
     * that is what the guest's busybox `du -sb` reports (verified against it
     * on tmpfs and f2fs: busybox counts directory st_size, GNU coreutils'
     * `--apparent-size` mode does not, so a host-side GNU `du -sb` reads
     * smaller by dirs × directory-size).
     */
    private fun walkForSize(root: Path, totals: WalkTotals, maxEntries: Long) {
        try {
            Files.walkFileTree(root, object : SimpleFileVisitor<Path>() {
                override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (charge(totals, maxEntries)) return FileVisitResult.TERMINATE
                    totals.dirs++
                    totals.bytes += attrs.size()
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

    /** Returns true once the entry budget is exhausted (and flags truncation). */
    private fun charge(totals: WalkTotals, maxEntries: Long): Boolean {
        if (totals.visited >= maxEntries) {
            totals.truncated = true
            return true
        }
        totals.visited++
        return false
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

    /**
     * Post-order delete: children first, then the directory itself. Errors are
     * collected per entry and the walk continues — a single permission failure
     * deep in a tree must not abort the rest.
     */
    private fun deleteTree(root: Path, totals: WalkTotals) {
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
                        val size = Files.readAttributes(dir, BasicFileAttributes::class.java).size()
                        Files.delete(dir)
                        totals.dirs++
                        totals.bytes += size
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

    private class WalkTotals {
        var files = 0L
        var dirs = 0L
        var bytes = 0L
        var visited = 0L
        var truncated = false
        val errors = ArrayList<String>()
    }

    companion object {
        private const val TOOL_NAME = "fastio_rm"
        private const val DISPLAY_NAME = "minis-fastio (delete)"

        // ponytail: one entry budget for the whole command, not per path — a
        // walk that would run for minutes is a stuck offload worker (the pool
        // is 2 threads and a saturated pool stalls every other tool call).
        // 天花板: trees with more than this many entries report
        // `truncated: true` and under-count; 升级触发: a real task hits the cap.
        private const val DEFAULT_MAX_ENTRIES = 2_000_000L
        private const val HARD_MAX_ENTRIES = 50_000_000L

        private val HELP = """minis-fastio — file-intensive primitives run by the host process on the real filesystem, bypassing the PRoot syscall tax (~100-200µs/op → ~1µs/op).

Usage:
  minis-fastio du <path>...            Read-only: file/dir counts + apparent bytes per tree (JSON).
                                       Use instead of `du -sb` / `find | wc -l` / `find | xargs stat`
                                       when walking a large tree.
  minis-fastio rm [-r] <path>...       Recursive delete (-r required for directories).
                                       Asks for permission first (Settings → Permissions).

Options:
  -r, --recursive    (rm) delete a directory tree. `-rf` / `-fr` work too; `-f`
                     alone is accepted and ignored (a missing path is still
                     reported as an error — POSIX `rm -f` would stay silent).
  --max-entries N    Entry budget for `du` (default $DEFAULT_MAX_ENTRIES); over budget → "truncated": true
  --compact, -q      Standard output shaping (see other minis-* tools)

Notes:
  - Paths are guest paths (/tmp, /var/minis/workspace, /var/minis/mounts/<name>, …) and are
    translated to the real host paths before use.
  - `du` bytes are the apparent sizes of every entry INCLUDING directories — byte-for-byte what the
    guest's busybox `du -sb` reports. (GNU du's --apparent-size mode omits directory st_size, so a
    GNU `du -sb` reads smaller by dirs × directory-size.)
  - `rm` refuses: host /dev //proc //sys, user-mounted external folders, bind-mount roots,
    and directories without -r. Nothing outside the app sandbox can be reached.
  - Both subcommands report JSON on stdout; exit code 1 when any path failed.
"""
    }
}

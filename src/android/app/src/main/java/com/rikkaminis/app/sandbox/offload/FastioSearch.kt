package com.rikkaminis.app.sandbox.offload

import java.io.BufferedInputStream
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader
import java.nio.charset.StandardCharsets
import java.nio.file.FileVisitResult
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.PathMatcher
import java.nio.file.SimpleFileVisitor
import java.nio.file.attribute.BasicFileAttributes

/**
 * Pure walk engines behind `minis-fastio find` and `minis-fastio grep`
 * ([T-minis-fastio] phase 2).
 *
 * Top-level and free of Android types (no `Context`, no `org.json`) so both
 * engines are exercised by plain JVM unit tests — the same reason
 * [walkForSize] / [deleteTree] live outside the handler class. The handler
 * owns path translation, the entry budget defaults and the JSON shaping; the
 * engines only walk and count.
 *
 * Both engines take the walk root as a HOST path plus the root's GUEST path,
 * and reconstruct each hit's guest path from the relative walk position. The
 * guest path is what the agent can feed back into a follow-up command; the
 * host path is an implementation detail it must never need.
 */

// ── find ────────────────────────────────────────────────────────────────────

/** One emitted hit: guest path, entry type (`f` file / `d` dir / `l` symlink), apparent size. */
internal data class FindMatch(val guestPath: String, val type: Char, val size: Long)

internal class FindOutcome : EntryBudget {
    var files = 0L
    var dirs = 0L

    /** Entries that satisfied the filters — counted even past [matches]' cap. */
    var matched = 0L

    override var visited = 0L
    override var truncated = false

    /** The match budget was reached: [matches] holds `limit` entries, more may exist. */
    var matchesCapped = false

    val errors = ArrayList<String>()
    val matches = ArrayList<FindMatch>()
}

/**
 * Walk [root] without following symlinks (matching `find`'s default) and record
 * up to [limit] hits that pass [matcher] (tested against the entry NAME, like
 * `find -name`) and [typeFilter] (`f` / `d` / `l`, null = every type).
 *
 * Totals are counted over the whole tree even when the match budget stops
 * collection — `matched` stays authoritative, `matches` is the capped sample.
 * The traversal itself is bounded by [maxEntries] via [charge] (shared with
 * `du`), so a runaway tree degrades to `truncated: true` instead of pinning an
 * offload worker thread.
 */
internal fun findTree(
    root: Path,
    rootGuestPath: String,
    matcher: PathMatcher?,
    typeFilter: Char?,
    limit: Int,
    maxEntries: Long,
    out: FindOutcome,
) {
    try {
        Files.walkFileTree(
            root,
            object : SimpleFileVisitor<Path>() {
                override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (charge(out, maxEntries)) return FileVisitResult.TERMINATE
                    out.dirs++
                    consider(dir, 'd', 0L)
                    return FileVisitResult.CONTINUE
                }

                override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (charge(out, maxEntries)) return FileVisitResult.TERMINATE
                    out.files++
                    consider(file, if (attrs.isSymbolicLink) 'l' else 'f', attrs.size())
                    return FileVisitResult.CONTINUE
                }

                override fun visitFileFailed(file: Path, exc: IOException): FileVisitResult {
                    if (charge(out, maxEntries)) return FileVisitResult.TERMINATE
                    out.errors.add("$file: ${exc.message}")
                    return FileVisitResult.CONTINUE
                }

                private fun consider(path: Path, type: Char, size: Long) {
                    if (typeFilter != null && typeFilter != type) return
                    if (matcher != null) {
                        val name = path.fileName ?: return
                        if (!matcher.matches(name)) return
                    }
                    out.matched++
                    if (out.matches.size >= limit) {
                        out.matchesCapped = true
                        return
                    }
                    out.matches.add(FindMatch(guestPathFor(root, rootGuestPath, path), type, size))
                }
            },
        )
    } catch (e: Exception) {
        out.errors.add("$root: ${e.message}")
    }
}

// ── grep ────────────────────────────────────────────────────────────────────

/** One matching line: guest path, 1-based line number, clipped text. */
internal data class GrepMatch(val guestPath: String, val lineNumber: Int, val text: String)

internal class GrepOutcome : EntryBudget {
    val matches = ArrayList<GrepMatch>()
    var filesScanned = 0L
    var filesSkippedBinary = 0L
    var filesSkippedLarge = 0L
    var filesSkippedLink = 0L
    var bytesScanned = 0L

    override var visited = 0L
    override var truncated = false

    /** The match budget was reached and the scan stopped early. */
    var matchesCapped = false

    val errors = ArrayList<String>()
}

/**
 * Scan every regular file under [root] (symlinks skipped, never followed) for
 * lines matching [regex], collecting at most [limit] hits.
 *
 * [regex] is a caller-built `java.util.regex` pattern — the handler owns that
 * choice and states it in its help text; this engine stays dialect-agnostic.
 * Binary files are skipped after an 8 KiB NUL sniff, and files above
 * [MAX_GREP_FILE_BYTES] are skipped outright (see the constant for why).
 *
 * On reaching the match budget the scan STOPS (that early exit is most of the
 * win on a big tree) and sets [GrepOutcome.matchesCapped], which the caller
 * reports as `truncated`. It can therefore over-report when a tree holds
 * exactly `limit` matches — the safe direction.
 */
internal fun grepTree(
    root: Path,
    rootGuestPath: String,
    regex: Regex,
    limit: Int,
    maxEntries: Long,
    out: GrepOutcome,
) {
    try {
        Files.walkFileTree(
            root,
            object : SimpleFileVisitor<Path>() {
                override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (charge(out, maxEntries)) return FileVisitResult.TERMINATE
                    return FileVisitResult.CONTINUE
                }

                override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (charge(out, maxEntries)) return FileVisitResult.TERMINATE
                    if (out.matchesCapped) return FileVisitResult.TERMINATE
                    if (attrs.isSymbolicLink) {
                        out.filesSkippedLink++
                        return FileVisitResult.CONTINUE
                    }
                    if (attrs.size() > MAX_GREP_FILE_BYTES) {
                        out.filesSkippedLarge++
                        return FileVisitResult.CONTINUE
                    }
                    out.bytesScanned += attrs.size()
                    grepFile(file, guestPathFor(root, rootGuestPath, file), regex, limit, out)
                    return FileVisitResult.CONTINUE
                }

                override fun visitFileFailed(file: Path, exc: IOException): FileVisitResult {
                    if (charge(out, maxEntries)) return FileVisitResult.TERMINATE
                    out.errors.add("$file: ${exc.message}")
                    return FileVisitResult.CONTINUE
                }
            },
        )
    } catch (e: Exception) {
        out.errors.add("$root: ${e.message}")
    }
}

private fun grepFile(file: Path, guestPath: String, regex: Regex, limit: Int, out: GrepOutcome) {
    try {
        BufferedInputStream(Files.newInputStream(file), BINARY_SNIFF_BYTES * 2).use { input ->
            // NUL in the first block ⇒ treat as binary and skip: a match inside
            // a binary blob is noise the agent cannot act on, and decoding it
            // would flood the output with replacement characters.
            input.mark(BINARY_SNIFF_BYTES)
            val head = ByteArray(BINARY_SNIFF_BYTES)
            val read = input.read(head)
            if (read > 0 && hasNulByte(head, read)) {
                out.filesSkippedBinary++
                return
            }
            input.reset()
            out.filesScanned++

            val reader = BufferedReader(InputStreamReader(input, StandardCharsets.UTF_8))
            var lineNumber = 0
            while (true) {
                val line = reader.readLine() ?: break
                lineNumber++
                if (!regex.containsMatchIn(line)) continue
                if (out.matches.size >= limit) {
                    out.matchesCapped = true
                    return
                }
                out.matches.add(GrepMatch(guestPath, lineNumber, clipLine(line)))
            }
        }
    } catch (e: Exception) {
        out.errors.add("$guestPath: ${e.message}")
    }
}

private fun hasNulByte(bytes: ByteArray, length: Int): Boolean {
    for (i in 0 until length) {
        if (bytes[i] == 0.toByte()) return true
    }
    return false
}

/**
 * Clip a matching line to [MAX_GREP_LINE_CHARS] (with an ellipsis marking the
 * cut). A minified bundle or a one-line JSON log would otherwise put megabytes
 * into a single match record — the output cap only bounds the NUMBER of
 * matches, not their size.
 */
internal fun clipLine(line: String): String =
    if (line.length <= MAX_GREP_LINE_CHARS) line else line.take(MAX_GREP_LINE_CHARS) + "…"

// ── shared ──────────────────────────────────────────────────────────────────

/** Guest path of [path] given the walk root and the root's own guest path. */
private fun guestPathFor(root: Path, rootGuestPath: String, path: Path): String {
    val relative = root.relativize(path).toString().replace('\\', '/')
    if (relative.isEmpty()) return rootGuestPath
    return if (rootGuestPath.endsWith("/")) rootGuestPath + relative else "$rootGuestPath/$relative"
}

private const val BINARY_SNIFF_BYTES = 8 * 1024

/**
 * ponytail: files above 8 MiB are skipped by `grep` — one multi-gigabyte log
 * would dominate the wall clock of a whole-tree search while the match budget
 * (default 200) means the agent reads a tiny fraction of any file anyway.
 * 天花板: a real search needs to match inside a >8 MiB text file (it will be
 * reported as `files_skipped_large` rather than silently missed).
 * 升级触发: a user greps a known large file and gets nothing back.
 */
private const val MAX_GREP_FILE_BYTES = 8L * 1024 * 1024

private const val MAX_GREP_LINE_CHARS = 200

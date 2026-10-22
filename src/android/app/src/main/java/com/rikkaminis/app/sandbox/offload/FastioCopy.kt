package com.rikkaminis.app.sandbox.offload

import java.io.IOException
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.DirectoryNotEmptyException
import java.nio.file.FileAlreadyExistsException
import java.nio.file.FileVisitResult
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.SimpleFileVisitor
import java.nio.file.StandardCopyOption
import java.nio.file.attribute.BasicFileAttributes

/**
 * Pure copy/move engines behind `minis-fastio cp` and `minis-fastio mv`
 * ([T-minis-fastio] phase 2).
 *
 * Free of Android types and of `org.json` so the JVM tests can drive them
 * directly against temp trees. Policy (external mounts, bind-mount roots,
 * overwrite refusal) stays in the handler, where the guest-path context for a
 * useful error message lives; these engines do the filesystem work and report
 * per-entry failures without aborting the tree.
 *
 * ## Symlinks are copied as links, never followed
 *
 * Same rule as [deleteTree] and [walkForSize]: a link inside the source tree
 * is reproduced as a link (`Files.createSymbolicLink` with the ORIGINAL target
 * string). Following it would (a) read a file the guest never asked for and
 * (b) write the target's CONTENT into the copy — the exact inversion of what
 * `cp -r` does. A dangling link therefore copies fine and stays dangling.
 *
 * ## Special files are refused, not opened
 *
 * A named pipe, socket or device node cannot be reproduced (`mkfifo` is not
 * exposed by this JVM) and must never be opened: `Files.copy` on a writer-less
 * FIFO blocks in `open(2)` forever. Such an entry is recorded in
 * [CopyOutcome.errors] — an ERROR, because the cross-device `mv` fallback
 * deletes the source only when the copy reported none (a silent skip would
 * destroy the entry).
 */

// ── cp ──────────────────────────────────────────────────────────────────────

internal class CopyOutcome {
    var files = 0L
    var dirs = 0L
    var bytes = 0L

    /** Entries that replaced an existing target (`--force`). */
    var overwritten = 0L

    val errors = ArrayList<String>()
}

/**
 * Copy [src] (file, symlink or directory tree) to [dst], which the caller has
 * already checked for the overwrite policy. With [force] an existing target is
 * replaced (and an existing directory is merged into); without it an existing
 * target is recorded as an error and left untouched — the top-level refusal
 * lives in the handler, so this is the defensive second line, not the gate.
 *
 * Filesystem work only: the caller owns every policy decision.
 */
internal fun copyTree(src: Path, dst: Path, force: Boolean, out: CopyOutcome) {
    try {
        Files.walkFileTree(
            src,
            object : SimpleFileVisitor<Path>() {
                override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                    val target = targetFor(dir)
                    try {
                        if (Files.exists(target, LinkOption.NOFOLLOW_LINKS)) {
                            if (!Files.isDirectory(target, LinkOption.NOFOLLOW_LINKS)) {
                                out.errors.add("$target: exists and is not a directory")
                                return FileVisitResult.SKIP_SUBTREE
                            }
                            // Merging into an existing directory is what `cp -r`
                            // does with --force; per-entry existence is handled
                            // in visitFile.
                            return FileVisitResult.CONTINUE
                        }
                        Files.createDirectories(target)
                        out.dirs++
                    } catch (e: Exception) {
                        out.errors.add("$target: ${e.message}")
                    }
                    return FileVisitResult.CONTINUE
                }

                override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                    val target = targetFor(file)
                    try {
                        // [dv23-A6 §1043-A-07] A named pipe / socket / device
                        // node is neither copyable (this JVM exposes no
                        // `mkfifo`, so the entry cannot be reproduced) nor
                        // OPENABLE: `Files.copy` on a writer-less FIFO blocks
                        // in `open(2)` with no timeout (measured: no return in
                        // 4 s) and the offload pool is 2 threads.
                        //
                        // Refused as an ERROR rather than skipped silently, on
                        // purpose: `copyDeleteFallback` (cross-device `mv`)
                        // deletes the source only when the copy reported no
                        // errors, so a silent skip would make `mv` drop the
                        // special file for good. An error keeps both trees.
                        //
                        // Checked BEFORE the existing-target handling below:
                        // nothing is written, so nothing may be deleted either.
                        if (!attrs.isRegularFile && !attrs.isSymbolicLink) {
                            out.errors.add(
                                "$file: special file (not a regular file, directory or symlink), not copied",
                            )
                            return FileVisitResult.CONTINUE
                        }
                        if (Files.exists(target, LinkOption.NOFOLLOW_LINKS)) {
                            if (!force) {
                                out.errors.add("$target: already exists")
                                return FileVisitResult.CONTINUE
                            }
                            Files.delete(target)
                            out.overwritten++
                        }
                        if (attrs.isSymbolicLink) {
                            Files.createSymbolicLink(target, Files.readSymbolicLink(file))
                        } else {
                            // Regular file: the guard above refused everything else.
                            Files.copy(file, target)
                        }
                        out.files++
                        out.bytes += attrs.size()
                    } catch (e: Exception) {
                        out.errors.add("$target: ${e.message}")
                    }
                    return FileVisitResult.CONTINUE
                }

                override fun visitFileFailed(file: Path, exc: IOException): FileVisitResult {
                    out.errors.add("$file: ${exc.message}")
                    return FileVisitResult.CONTINUE
                }

                private fun targetFor(path: Path): Path {
                    val relative = src.relativize(path)
                    return if (relative.toString().isEmpty()) dst else dst.resolve(relative)
                }
            },
        )
    } catch (e: Exception) {
        out.errors.add("$src: ${e.message}")
    }
}

// ── mv ──────────────────────────────────────────────────────────────────────

internal class MoveOutcome {
    /** `rename` (same filesystem, the normal path) or `copy+delete` (cross-device). */
    var method = "rename"

    /** Counted only on the `copy+delete` path — a rename costs no walk. */
    var files = 0L
    var dirs = 0L
    var bytes = 0L
    var counted = false

    val errors = ArrayList<String>()
}

/**
 * Move [src] to [dst] (target policy already settled by the caller).
 *
 * The normal path is one host `rename` — no walk, no per-entry syscall, which
 * is why `mv` is the cheapest primitive in the set. [force] is what makes the
 * handler's `--force` contract real: the caller has already decided that
 * replacing the target is what the user asked for, so the rename must carry
 * REPLACE_EXISTING. Without it a bare move fails with
 * FileAlreadyExistsException and `mv --force` reports a failure for the one
 * thing it promises to do.
 *
 * The move is attempted with ATOMIC_MOVE first (B16): a cross-device rename
 * fails with EXDEV and surfaces as [AtomicMoveNotSupportedException], which
 * falls back to [copyDeleteFallback] — the real move. Without ATOMIC_MOVE the
 * JVM's own non-atomic fallback ran first and got the cross-device non-empty
 * directory case wrong (it created the dst directory, then failed rmdir on the
 * non-empty source with a DirectoryNotEmptyException pointing at SRC), which
 * left a half-empty dst and made the fallback unreachable.
 *
 * Two same-device semantics shift with ATOMIC_MOVE (both now POSIX-aligned,
 * nothing pinned the old behaviour): a file no longer replaces an EMPTY
 * directory target (POSIX rename cannot), and a directory no longer replaces
 * a file target. The readable non-empty-directory reason is preserved via the
 * pre-check above.
 */
internal fun moveTree(src: Path, dst: Path, force: Boolean, out: MoveOutcome) {
    if (!force && Files.exists(dst, LinkOption.NOFOLLOW_LINKS)) {
        // Defensive second line for the no-overwrite contract. Needed because
        // the ATOMIC_MOVE rename below would silently replace an existing EMPTY
        // directory target (POSIX rename does); same message the catch below
        // carries for the race window.
        out.errors.add("$dst: already exists; pass --force to overwrite")
        return
    }
    if (force && Files.isDirectory(dst, LinkOption.NOFOLLOW_LINKS) && !isDirEmpty(dst)) {
        // B16 companion: a non-empty directory target can never be replaced by
        // rename. With ATOMIC_MOVE the JDK surfaces that as a bare
        // FileSystemException ("Is a directory" / "Directory not empty"),
        // losing the readable reason the old non-atomic REPLACE_EXISTING path
        // produced (DirectoryNotEmptyException) — so spell it out up front.
        // The catch below stays for the race window.
        out.errors.add("$dst: already exists and is a non-empty directory; refusing to replace it")
        return
    }
    try {
        if (force) {
            Files.move(src, dst, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        } else {
            Files.move(src, dst, StandardCopyOption.ATOMIC_MOVE)
        }
        return
    } catch (e: AtomicMoveNotSupportedException) {
        // Cross-device rename (EXDEV) or a filesystem that rejects the atomic
        // flag — either way the copy+delete fallback below is the real move.
        // Fall through deliberately.
    } catch (e: DirectoryNotEmptyException) {
        // Only reachable with force: the target is a directory no rename can
        // replace. Spelled out instead of handing over the bare Java message,
        // which is just the path again and tells the caller nothing.
        out.errors.add("$dst: already exists and is a non-empty directory; refusing to replace it")
        return
    } catch (e: FileAlreadyExistsException) {
        out.errors.add("$dst: already exists; pass --force to overwrite")
        return
    } catch (e: Exception) {
        if (!isCrossDevice(e)) {
            out.errors.add("$src → $dst: ${e.message}")
            return
        }
    }

    copyDeleteFallback(src, dst, force, out)
}

/**
 * The copy-then-delete move for cross-device (EXDEV) targets. Always merges
 * with force: the caller has already settled the overwrite policy, and a
 * cross-device move cannot be a rename anyway.
 */
internal fun copyDeleteFallback(src: Path, dst: Path, force: Boolean, out: MoveOutcome) {
    out.method = "copy+delete"
    val dstPreExisted = Files.exists(dst, LinkOption.NOFOLLOW_LINKS)
    val copy = CopyOutcome()
    copyTree(src, dst, force = true, out = copy)
    if (copy.errors.isNotEmpty()) {
        // Half-copied residue: when WE created dst (it did not exist before
        // the move), remove the partial tree so a failed `mv` never leaves a
        // half-empty directory behind. A pre-existing --force merge target
        // keeps its original content — deleting it would destroy user data.
        if (!dstPreExisted) {
            val residue = WalkTotals()
            deleteTree(dst, residue)
        }
        out.errors.addAll(copy.errors)
        return
    }
    val deleted = WalkTotals()
    deleteTree(src, deleted)
    out.errors.addAll(deleted.errors)
    out.files = copy.files
    out.dirs = copy.dirs
    out.bytes = copy.bytes
    out.counted = true
}

/**
 * Is this the "rename across filesystems" failure? Matched on the message
 * (EXDEV / "Invalid cross-device link") because the JVM exposes no error code
 * — a heuristic, but the only alternative is treating every move failure as
 * cross-device and silently copying over a real error.
 */
private fun isCrossDevice(e: Exception): Boolean {
    val message = e.message?.lowercase() ?: return false
    return message.contains("cross-device") || message.contains("exdev")
}

/** Unlistable counts as non-empty: an unreadable directory is refused, not replaced. */
private fun isDirEmpty(dir: Path): Boolean =
    try {
        Files.newDirectoryStream(dir).use { s -> !s.iterator().hasNext() }
    } catch (_: Exception) {
        false
    }

// ── path containment ────────────────────────────────────────────────────────

/**
 * Would copying/moving [src] to [target] copy a tree into itself (an infinite
 * recursion: the walk keeps finding the directories it just created)?
 *
 * Checked on the CANONICAL path of the target's PARENT plus the target's own
 * name, which is what catches the aliased form the plain lexical check misses:
 * with `/tmp/link -> /tmp/a`, `cp -r /tmp/a /tmp/link/b` resolves the parent to
 * `/tmp/a` and is refused. The target itself usually does not exist yet, so it
 * cannot be canonicalized — hence resolving the parent.
 */
internal fun targetInsideSource(src: Path, target: Path): Boolean {
    val srcCanonical = canonicalOrNull(src) ?: src.toAbsolutePath().normalize()
    val parent = target.parent ?: return false
    val name = target.fileName ?: return false
    val parentCanonical = canonicalOrNull(parent) ?: parent.toAbsolutePath().normalize()
    val effective = parentCanonical.resolve(name)
    return effective == srcCanonical || effective.startsWith(srcCanonical)
}

private fun canonicalOrNull(path: Path): Path? =
    try {
        path.toRealPath()
    } catch (_: Exception) {
        null
    }

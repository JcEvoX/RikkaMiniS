package com.rikkaminis.app.tools

import com.rikkaminis.app.logging.AppLogger
import java.io.File
import java.io.IOException
import java.nio.file.Files

/**
 * Atomic whole-file replacement for the write/edit tools.
 *
 * `File.writeText` truncates in place: a SIGKILL that lands inside the write
 * window leaves a half-written file on disk, and SIGKILL cannot be caught, so
 * no `finally` can repair it. The app really does get killed that way (23
 * silent kills in one evening, 2026-09-15) while an agent loop is writing
 * SKILL.md / SOUL.md / config JSON / user scripts.
 *
 * This follows the repo's existing atomic-write idiom (`SoulStore.save`,
 * `ProviderModelsCache.save`, `MCPRepository`): write a sibling temp, rename it
 * over the target, and fall back to a direct write where rename is unavailable.
 * Two details differ from those private-file call sites, because these tools
 * write arbitrary user paths:
 *
 *  - **An existing symlink is followed.** `writeText` writes *through* a link to
 *    its destination; renaming over the link path would replace the link with a
 *    regular file and silently change what the path means (the sandbox runs
 *    PRoot with `--link2symlink`, so guest paths can be links).
 *  - **The target's mode is carried over.** A rename installs the temp file's
 *    mode, so an executable script rewritten through `file_write` would come
 *    back un-runnable.
 *
 * Append is deliberately NOT routed here: appending has no atomic form (it must
 * extend the existing bytes), and pretending otherwise would be worse than an
 * honest non-atomic append.
 */
internal object AtomicFileWrite {

    /**
     * Replace [target]'s content with [content] in a way that leaves either the
     * old bytes or the new bytes on disk — never a truncated mixture. Falls back
     * to the plain truncate-and-write when the filesystem refuses sibling temp
     * files, so a working write never turns into a failure.
     */
    fun write(target: File, content: String) {
        val dest = followLinks(target)
        val dir = dest.parentFile
        if (dir == null || (!dir.exists() && !dir.mkdirs())) {
            target.writeText(content)
            return
        }
        val tmp = try {
            File.createTempFile(".atomic-", ".tmp", dir)
        } catch (e: IOException) {
            // Some FUSE mounts refuse temp siblings; keep the old behaviour.
            target.writeText(content)
            return
        }
        try {
            tmp.writeText(content)
            if (dest.exists()) copyMode(dest, tmp)
            if (tmp.renameTo(dest)) {
                val expected = content.toByteArray(Charsets.UTF_8).size.toLong()
                if (dest.length() != expected) {
                    AppLogger.warning(
                        "FileWrite",
                        "atomic write to ${dest.path} landed with ${dest.length()} bytes, expected $expected",
                    )
                }
            } else {
                // rename can fail across a bind-mount boundary (seen on the
                // mounts dir); fall back so the write still lands.
                tmp.delete()
                target.writeText(content)
            }
        } catch (t: Throwable) {
            tmp.delete()
            throw t
        }
    }

    /**
     * Resolve an existing symlink chain so the rename replaces the FILE the link
     * points at, not the link itself.
     */
    private fun followLinks(target: File): File =
        try {
            target.canonicalFile
        } catch (e: IOException) {
            target
        }

    /**
     * Carry [source]'s mode over to [dest]: a rename installs the temp file's
     * mode, and an executable script must stay executable.
     */
    private fun copyMode(source: File, dest: File) {
        try {
            Files.setPosixFilePermissions(dest.toPath(), Files.getPosixFilePermissions(source.toPath()))
        } catch (e: Exception) {
            // No POSIX view for this path — carry the bit the sandbox actually
            // depends on and leave the rest at the default.
            if (source.canExecute()) dest.setExecutable(true, false)
        }
    }
}

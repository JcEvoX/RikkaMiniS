package com.rikkaminis.app.browser

import java.io.File

/**
 * [fix/dv17-1 B-06] Re-points download records written under one session's
 * workspace directory onto the same relative path in another session's
 * workspace.
 *
 * Defect this closes: a download made while a chat was still a draft landed in
 * `minis-sessions/__new__<uuid>/workspace/` and its [path] was frozen into the
 * entry. Draft promotion ([com.rikkaminis.app.ui.chat.ChatViewModel.ensureSession]
 * → migrateDraftResources) moved the bytes to the real session directory and
 * deleted the draft tree, but `BrowserTabPool.setSession` re-pointed only the
 * session id and the live tabs — every record kept pointing at the deleted
 * path, so the downloads panel's "open" button died at its `file.exists()`
 * guard with zero feedback while the file sat intact in the new directory.
 *
 * Pure, Android-free (java.io.File only) so it is directly JVM-testable; the
 * pool wires it through `_downloads.update {}` (the atomic CAS — a plain
 * assignment races the blob-bridge/URL-download writers).
 *
 * Semantics:
 *  - a record whose [path] is null, or whose absolute path is **not** under
 *    [oldDir], is returned untouched (bytes outside the old workspace were
 *    never moved by the draft migration — no rewrite, no guessing);
 *  - a path under [oldDir] maps to `File(newDir, relativePath)`, preserving
 *    the relative structure (top-level downloads only today, but the
 *    migration moves whole subtrees, so nested paths stay correct);
 *  - the entry's identity (id, state, seen, sizes) is preserved — only the
 *    path is replaced.
 */
internal fun <T> remapDownloadDestinations(
    entries: List<T>,
    oldDir: File,
    newDir: File,
    pathOf: (T) -> File?,
    withPath: (T, File) -> T,
): List<T> {
    val oldPrefix = oldDir.absolutePath.trimEnd(File.separatorChar) + File.separator
    return entries.map { entry ->
        val path = pathOf(entry)
        when {
            path == null -> entry
            !path.absolutePath.startsWith(oldPrefix) -> entry
            else -> {
                val relative = path.absolutePath.removePrefix(oldPrefix)
                if (relative.isEmpty()) entry else withPath(entry, File(newDir, relative))
            }
        }
    }
}

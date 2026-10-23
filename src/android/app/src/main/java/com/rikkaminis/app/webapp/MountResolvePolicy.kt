package com.rikkaminis.app.webapp

/**
 * [dv26-E8] S-08 — cold-start decision for a `mount`-scoped WebApp shortcut.
 *
 * The defect this file exists for: [WebAppPathResolver]'s `SCOPE_MOUNT` arm
 * read `PRootKernel.bindMounts` exactly once. On a cold start the launcher
 * icon starts [WebAppActivity] directly, and
 * [com.rikkaminis.app.sandbox.PRootKernel.applyMountedFoldersSnapshot] is fired
 * from `MinisApp.onCreate` on `applicationScope` + `Dispatchers.IO`
 * ([dv20-A4-A06] moved it off the first frame on purpose). Losing that race
 * leaves `/var/minis/mounts/<name>` absent from the bind set, so
 * `resolveHostPath` falls back to the rootfs *placeholder directory*, the file
 * "does not exist", and the Activity paints "WebApp source missing" for a
 * shortcut that is perfectly fine.
 *
 * [PRootKernel.boot] solves the same problem by awaiting the snapshot before
 * it spawns a shell; the WebApp fast path had no equivalent — there is no
 * kernel boot in front of it at all.
 *
 * Android-free on purpose (no `Context`, no `File`, no `PRootKernel`): the
 * resolver owns the I/O and injects it as lambdas, so the decision can be
 * exercised by a plain JVM test instead of a device.
 */

/** Linux prefix every user-mounted external folder lives under. */
internal const val MOUNT_SCOPE_PREFIX = "/var/minis/mounts/"

/**
 * Hard bound on `MOUNT`-scope resolve attempts. One retry is the whole fix:
 * the snapshot is idempotent and cheap-ish but it does SAF readdir + rootfs
 * mkdirs, so a loop that keeps retrying on a genuinely-deleted file would pay
 * that cost on every cold start for nothing.
 */
internal const val MOUNT_RESOLVE_MAX_ATTEMPTS = 2

/**
 * The `/var/minis/mounts/<name>` root [htmlPath] belongs to, or null when the
 * path is not under the mount scope at all (legacy/corrupt row, traversal).
 *
 * Deriving the root from the path — instead of trusting
 * [com.rikkaminis.app.data.db.WebAppShortcutEntity.scopeContext] — keeps a row
 * whose `scopeContext` disagrees with its `htmlPath` from "answering" for a
 * mount it does not belong to.
 */
internal fun mountRootOf(htmlPath: String): String? {
    if (!htmlPath.startsWith(MOUNT_SCOPE_PREFIX)) return null
    val name = htmlPath.removePrefix(MOUNT_SCOPE_PREFIX).substringBefore('/')
    if (name.isEmpty() || name == "." || name == "..") return null
    return MOUNT_SCOPE_PREFIX + name
}

/**
 * Resolve a `mount`-scoped [htmlPath] with at most one snapshot refresh.
 *
 * Loop shape (this is the decision; the caller supplies the I/O):
 *  1. `probe` the path. If it lands on something [usable] — accept it.
 *  2. Not usable and the bind snapshot has not been refreshed yet **and** the
 *     mount root is not in `mountKnown` — the snapshot simply is not applied
 *     yet (cold-start race) → refresh and go to 1 exactly once.
 *  3. Not usable and a refresh would be redundant — the mount is registered
 *     and there is genuinely no such file, or we already refreshed → give up.
 *     The caller surfaces "source missing" from that answer.
 *
 * The retry is a *complete unit* — refresh then `probe` again — never a
 * re-read of the previously returned `File`: the old object is exactly the
 * one that pointed into the rootfs placeholder.
 *
 * @param mountKnown whether the bind set already knows [mountRootOf] for this
 *        path (the caller reads `PRootKernel.bindMounts`).
 * @param probe the resolve step (`PRootKernel.resolveHostPath`): guest path →
 *        host path, or null when nothing matched.
 * @param usable whether a probed host path is an existing regular file (the
 *        caller's `exists() && isFile`; a directory is NOT usable — the rootfs
 *        placeholder dir is a directory).
 * @param refresh the snapshot step (`PRootKernel.applyMountedFoldersSnapshot`).
 * @return the host path to render, or null → the caller renders "source
 *         missing".
 */
internal fun resolveMountWithRetry(
    htmlPath: String,
    mountKnown: (String) -> Boolean,
    probe: (String) -> String?,
    usable: (String) -> Boolean,
    refresh: () -> Unit,
): String? {
    // Stale on the first attempt only when the bind set cannot answer for this
    // path — that is the race signature. A registered mount with a missing
    // file is a real deletion and must not pay for a SAF readdir.
    var stale = mountRootOf(htmlPath)?.let { !mountKnown(it) } ?: false
    var attempt = 0
    while (attempt < MOUNT_RESOLVE_MAX_ATTEMPTS) {
        attempt++
        val host = probe(htmlPath)
        if (host != null && usable(host)) return host
        if (!stale) return null
        // Idempotent and allowed to fail: a throwing snapshot must still end in
        // "source missing", never in a crash out of the Activity.
        runCatching { refresh() }
        stale = false
    }
    return null
}

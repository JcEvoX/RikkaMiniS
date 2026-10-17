package com.rikkaminis.app.sandbox.offload

/**
 * [da1019-J] Pure relative→absolute join for guest paths: a relative
 * [path] with an absolute [cwd] resolves against it (the same
 * [B14-fix-r14] shape as ModelUseOffloadHandler.readLinuxPath).
 * Everything else — an absolute path, or a missing / relative cwd —
 * is returned unchanged, so the session-aware resolver stays the only
 * gate and a path it rejects fails closed instead of being guessed
 * onto the rootfs. Pure so both arms stay testable off-device. Top-level
 * (not a companion member): the handler's companion is private, so a
 * companion member would be unreferenceable from the unit test.
 */
internal fun resolveMaybeRelative(path: String, cwd: String?): String =
    if (!path.startsWith("/") && !cwd.isNullOrEmpty() && cwd.startsWith("/")) {
        if (cwd.endsWith("/")) "$cwd$path" else "$cwd/$path"
    } else path

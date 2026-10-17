package com.rikkaminis.app.sandbox.offload

/**
 * [da1020-C] Relative→absolute join for `--paths` tokens: a relative [path]
 * with an absolute [cwd] resolves against it, same helper shape as
 * [resolveMaybeRelative] (ConfigOffloadHandler's `--file` join). Everything
 * else — an absolute path, or a missing / relative cwd — is returned
 * unchanged, so the session-aware resolver downstream stays the only gate
 * and a path it rejects fails closed instead of being guessed onto the
 * rootfs. Pure so both arms stay testable off-device. Top-level (same
 * package): [resolveMaybeRelative] is the single join implementation —
 * reusing it here does not create a third copy.
 */
internal fun resolveMaybeRelativeAll(paths: List<String>, cwd: String?): List<String> =
    paths.map { resolveMaybeRelative(it, cwd) }

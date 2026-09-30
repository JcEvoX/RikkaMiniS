package com.rikkaminis.app.sandbox

import java.io.File

/**
 * Guest → host path translation for native_offload handlers that operate on
 * the REAL filesystem instead of going through PRoot's ptrace boundary
 * ([T-minis-fastio]).
 *
 * ## Why this exists
 *
 * PRoot pays ~100-200µs per path-touching syscall (ptrace stop/resume round
 * trip + POKEDATA rewrite of the translated path). A handler that runs in the
 * app process operates on the same files through ordinary JVM `java.io` /
 * `java.nio` calls, i.e. ~1µs per op — but only if it can turn the guest path
 * the shell passed it into the real host path. That mapping is the whole
 * reason this object exists; everything else in `minis-fastio` is a walk.
 *
 * ## One table, two consumers
 *
 * The authoritative bind table is the one PRoot itself was launched with:
 * `-r <rootfs>` (catch-all) plus every `-b <host>:<guest>` that
 * [PRootKernel.buildProotCommand] emitted for this session. [sessionBindings]
 * rebuilds exactly that list from the same inputs the command line is built
 * from — the live [PRootKernel.bindMounts] map (globals + user mounts) and
 * [PER_SESSION_SUBDIRS] (which [PRootKernel] also uses) — so there is one
 * source of truth, not a second copy that drifts.
 *
 * ## The guard (never traded away)
 *
 * Translation is refuse-instead-of-guess. A guest path is accepted only when
 * its canonical host form is provably contained in the host base of the
 * binding it matched. `..` above `/`, the host pseudo-filesystems, and
 * symlinks pointing out of a binding all come back as [Resolution.Denied]
 * rather than as a best guess. Callers that mutate the filesystem are
 * expected to layer their own policy on top (see [Resolution.Ok.isBindingRoot]
 * and [Binding.isExternalMount]).
 */
object GuestPathMapper {

    /** Guest prefix of a user-mounted external folder (T219). */
    const val EXTERNAL_MOUNT_PREFIX = "/var/minis/mounts/"

    /**
     * Subdirs that live under `minis-sessions/<sessionId>/` rather than in the
     * global pool. Single source of truth — [PRootKernel] builds its
     * session-scoped resolution from this same list.
     */
    val PER_SESSION_SUBDIRS: List<String> = listOf("attachments", "offloads", "workspace", "browser")

    /**
     * Guest prefixes that PRoot binds straight onto the HOST kernel surfaces
     * (`-b /dev`, `-b /proc`, `-b /sys` in [PRootKernel.buildProotCommand]).
     * They exist in the guest for compatibility, but a translated file
     * operation there would hit the real device surfaces — `rm -r /proc/self`
     * is not a sandbox operation. Refused outright rather than mapped.
     */
    val PSEUDO_FS_PREFIXES: List<String> = listOf("/dev", "/proc", "/sys")

    /**
     * One `-b <hostBase>:<guestPrefix>` binding, as proot was launched with.
     * The rootfs is represented as the catch-all `guestPrefix == "/"`.
     */
    data class Binding(val guestPrefix: String, val hostBase: String) {
        val isCatchAll: Boolean get() = guestPrefix == "/"

        /** User-mounted external folder — outside the app's own sandbox. */
        val isExternalMount: Boolean get() = guestPrefix.startsWith(EXTERNAL_MOUNT_PREFIX)
    }

    /** Why a guest path was not translated. Never used for "not found" — that
     *  is a normal [Resolution.Ok] whose host path simply does not exist yet. */
    enum class Refusal {
        /** `..` segments climbed above `/`. */
        CLIMBS_ABOVE_ROOT,

        /** Path is under [PSEUDO_FS_PREFIXES] (host /dev, /proc, /sys). */
        PSEUDO_FS,

        /** No binding matched — only reachable if the rootfs catch-all is absent. */
        NO_BINDING,

        /** The canonical host path left the binding's host base (`..` or a
         *  symlink pointing out of the tree). */
        HOST_ESCAPE,
    }

    sealed class Resolution {
        abstract val guestPath: String

        data class Ok(
            override val guestPath: String,
            val hostPath: String,
            val binding: Binding,
            /** Path under [Binding.hostBase]; empty when the path IS the base. */
            val relative: String,
        ) : Resolution() {
            /**
             * The guest path is a bind-mount root (`/`, `/var/minis/workspace`,
             * a user mount, …). Deleting it would take the whole mount with it,
             * so mutating callers must refuse.
             */
            val isBindingRoot: Boolean get() = relative.isEmpty()
        }

        data class Denied(
            override val guestPath: String,
            val reason: Refusal,
            val detail: String,
        ) : Resolution()
    }

    /**
     * Rebuild the `-b` table for one session, longest guest prefix first.
     *
     * [globalBindings] is a snapshot of [PRootKernel.bindMounts] (global
     * `/var/minis/{memory,skills,shared,mcp-servers,logs}` plus the
     * `/var/minis/mounts/<name>` user mounts). Session-scoped subdirs are
     * derived from [filesDir] + [sessionId] and deliberately override any
     * global entry with the same guest prefix — a session's own view wins,
     * mirroring `ExecutionCoordinator.buildSessionBindMounts`.
     *
     * [sessionId] is rejected (session subdirs skipped) when it is blank or
     * contains a path separator: it arrives from the guest's environment and
     * must not be able to move the session base out of `minis-sessions/`.
     */
    fun sessionBindings(
        filesDir: String,
        sessionId: String?,
        globalBindings: Map<String, String>,
        rootfsDir: String,
    ): List<Binding> {
        val merged = LinkedHashMap<String, String>()
        for ((guest, host) in globalBindings) {
            if (guest.isNotEmpty() && host.isNotEmpty()) merged[guest] = host
        }
        if (!sessionId.isNullOrBlank() && '/' !in sessionId && sessionId != ".." && sessionId != ".") {
            val sessionBase = File(File(filesDir, "minis-sessions"), sessionId)
            for (sub in PER_SESSION_SUBDIRS) {
                merged["/var/minis/$sub"] = File(sessionBase, sub).path
            }
        }
        merged["/"] = rootfsDir
        return merged.entries
            .map { Binding(it.key, it.value) }
            .sortedByDescending { it.guestPrefix.length }
    }

    /**
     * Normalize [path] against [cwd] into an absolute guest path, resolving
     * `.` / `..` via [normalizeDotSegments]. Returns null when `..` climbs
     * above `/` — the one case that must not be silently clamped, because the
     * caller asked for something outside the guest tree.
     */
    fun absoluteGuestPath(cwd: String, path: String): String? {
        val base = if (path.startsWith("/")) {
            path
        } else {
            val dir = if (cwd.startsWith("/")) cwd else "/"
            if (dir.endsWith("/")) dir + path else "$dir/$path"
        }
        val segments = normalizeDotSegments(base) ?: return null
        return "/" + segments.joinToString("/")
    }

    /**
     * Translate one guest path. [bindings] must come from [sessionBindings]
     * (or an equivalent table) — the longest matching guest prefix wins, so a
     * user mount nested under `/var/minis/` resolves to the mount rather than
     * to the rootfs placeholder.
     */
    fun resolve(path: String, cwd: String, bindings: List<Binding>): Resolution {
        val guest = absoluteGuestPath(cwd, path)
            ?: return Resolution.Denied(
                path,
                Refusal.CLIMBS_ABOVE_ROOT,
                "'$path' (cwd '$cwd') climbs above '/' — refusing to translate",
            )

        for (pseudo in PSEUDO_FS_PREFIXES) {
            if (guest == pseudo || guest.startsWith("$pseudo/")) {
                return Resolution.Denied(
                    guest,
                    Refusal.PSEUDO_FS,
                    "'$guest' is the host $pseudo surface (bound by proot -b), not a guest file",
                )
            }
        }

        val binding = bindings
            .filter { matches(it.guestPrefix, guest) }
            .maxByOrNull { it.guestPrefix.length }
            ?: return Resolution.Denied(
                guest,
                Refusal.NO_BINDING,
                "'$guest' matched no proot bind mount",
            )

        val relative = if (binding.isCatchAll) {
            guest.removePrefix("/")
        } else {
            guest.removePrefix(binding.guestPrefix).removePrefix("/")
        }

        val host = containedHostPath(binding.hostBase, relative)
            ?: return Resolution.Denied(
                guest,
                Refusal.HOST_ESCAPE,
                "'$guest' resolves outside its bind base '${binding.hostBase}' " +
                    "(dot-segment or symlink escape)",
            )

        return Resolution.Ok(guest, host, binding, relative)
    }

    /** Does [guest] sit at or under [guestPrefix]? The rootfs catch-all matches everything. */
    fun matches(guestPrefix: String, guest: String): Boolean {
        if (guestPrefix == "/") return true
        return guest == guestPrefix || guest.startsWith("$guestPrefix/")
    }

    /**
     * Join [relative] under [hostBase] and prove containment on the CANONICAL
     * paths — canonicalizing both sides is what makes symlink escapes visible
     * (`<base>/link -> /etc` then `<base>/link/passwd` leaves the base) and
     * keeps the comparison honest on Android's aliased storage paths
     * (`/storage/self/primary` vs `/storage/emulated/0`).
     *
     * Returns null when the result escapes [hostBase] or cannot be
     * canonicalized. A [relative] with a leading `/` is refused outright: the
     * JVM's `File(base, "/x")` silently re-parents it to `<base>/x` (Java
     * strips the separator) rather than honouring the absolute path, so
     * accepting it would hide a caller passing a host path where a tail was
     * expected behind a plausible-looking nonsense result.
     */
    fun containedHostPath(hostBase: String, relative: String): String? {
        if (relative.startsWith("/")) return null
        val base = try { File(hostBase).canonicalPath } catch (_: Exception) { return null }
        if (relative.isEmpty()) return base
        val target = try { File(base, relative).canonicalPath } catch (_: Exception) { return null }
        if (target == base) return base
        if (!target.startsWith(base + File.separator)) return null
        return target
    }
}

/**
 * RFC-3986-style dot-segment normalization of a path tail.
 * Splits on '/', drops empty and `.` segments, pops the stack on `..`.
 * Returns null when a `..` would climb above the root (i.e. the tail resolves
 * outside its base), otherwise the normalized segment list.
 *
 * Top-level (not a member) so it can be unit-tested with zero Android deps.
 * Lives in this file rather than [PRootKernel] because [PRootKernel] is an
 * Android-coupled object that a JVM test cannot compile — and because
 * [GuestPathMapper] is the second consumer of the same rule; two copies of
 * "what does `..` mean" is exactly the drift this project keeps paying for.
 * Call sites are unchanged (same package, same name).
 */
internal fun normalizeDotSegments(tail: String): List<String>? {
    val stack = mutableListOf<String>()
    var climbsAboveBase = false
    for (segment in tail.split('/')) {
        when (segment) {
            "", "." -> Unit
            ".." -> {
                if (stack.isEmpty()) climbsAboveBase = true else stack.removeAt(stack.size - 1)
            }
            else -> stack.add(segment)
        }
    }
    return if (climbsAboveBase) null else stack
}

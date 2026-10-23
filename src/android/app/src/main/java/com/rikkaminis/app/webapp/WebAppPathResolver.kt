package com.rikkaminis.app.webapp

import android.content.Context
import com.rikkaminis.app.data.db.WebAppShortcutEntity
import com.rikkaminis.app.data.repository.WebAppShortcutRepository
import com.rikkaminis.app.data.storage.SessionFileStore
import com.rikkaminis.app.sandbox.PRootKernel
import java.io.File

/**
 * T-pwa-1 (renamed Pwa → WebApp): resolve the stored
 * ([WebAppShortcutEntity.pathScope], [WebAppShortcutEntity.scopeContext],
 * [WebAppShortcutEntity.htmlPath]) triple back to a host File. Returns
 * null if the file no longer exists — caller should surface a "source
 * missing" UI instead of crashing.
 *
 *   - session_attachment → `<filesDir>/minis-sessions/<sessionId>/attachments/<htmlPath>`
 *     (htmlPath relative to the session's attachments dir; absolute paths
 *      under /var/minis/<sub>/ are also accepted via resolveSessionHostPath)
 *   - shared             → resolved via [PRootKernel.resolveHostPath]
 *   - mount              → resolved via [PRootKernel.resolveHostPath]
 *                          (longest-prefix match against bindMounts), with one
 *                          bounded snapshot refresh on a cold start
 *                          ([resolveMount]; [dv26-E8] S-08)
 */
object WebAppPathResolver {

    fun resolve(context: Context, shortcut: WebAppShortcutEntity): File? {
        val file = when (shortcut.pathScope) {
            WebAppShortcutRepository.SCOPE_SESSION_ATTACHMENT -> resolveSession(context, shortcut)
            // Global binds (/var/minis/{memory,skills,shared}) are registered
            // synchronously in MinisApp.onCreate via registerGlobalBindMounts,
            // so `shared` has no equivalent cold-start window to lose.
            WebAppShortcutRepository.SCOPE_SHARED -> PRootKernel.resolveHostPath(shortcut.htmlPath)
            WebAppShortcutRepository.SCOPE_MOUNT -> resolveMount(context, shortcut.htmlPath)
            else -> null
        }
        return file?.takeIf { it.exists() && it.isFile }
    }

    /**
     * [dv26-E8] S-08 — `mount` scope, cold-start safe.
     *
     * A home-screen shortcut starts this Activity directly, so nothing awaits
     * [PRootKernel.applyMountedFoldersSnapshot] first (unlike
     * [PRootKernel.boot], which does). One missing `/var/minis/mounts/<name>`
     * bind is enough for [PRootKernel.resolveHostPath] to fall back to the
     * rootfs *placeholder directory* and for the shortcut to report "source
     * missing" on a file that is right there.
     *
     * The decision itself lives in [resolveMountWithRetry] (Android-free, JVM
     * tested); this function only injects the platform I/O. Blocking host I/O
     * is fine here: the one caller ([com.rikkaminis.app.webapp.WebAppActivity])
     * already runs [resolve] inside `withContext(Dispatchers.IO)`, and that is
     * a contract this file's callers keep — do not hop back to Main.
     */
    private fun resolveMount(context: Context, htmlPath: String): File? {
        val hostPath = resolveMountWithRetry(
            htmlPath = htmlPath,
            mountKnown = { root -> PRootKernel.bindMounts[root] != null },
            probe = { path -> PRootKernel.resolveHostPath(path)?.absolutePath },
            // A directory is NOT usable: the placeholders
            // materializeMountTargets creates inside the rootfs are directories.
            usable = { path -> File(path).let { it.exists() && it.isFile } },
            refresh = { PRootKernel.applyMountedFoldersSnapshot(context) },
        )
        return hostPath?.let { File(it) }
    }

    /**
     * T-pwa-3: reverse-resolve a host file path to a `(pathScope,
     * scopeContext, linuxPath)` triple suitable for
     * [WebAppShortcutRepository.create]. Walks the
     * [PRootKernel.bindMounts] map looking for an entry whose host
     * directory is a prefix of [hostFile]; returns null if none matches
     * (caller should hide the "Add to Home Screen" menu item).
     *
     * Mapping rules:
     *  - `/var/minis/shared` bind  → `pathScope = "shared"`,  `scopeContext = null`
     *  - `/var/minis/mounts/<n>`   → `pathScope = "mount"`,   `scopeContext = "<n>"`
     *  - everything else (incl. memory/skills, per-session subdirs, rootfs) → null
     */
    fun inferScope(hostFile: File): Triple<String, String?, String>? {
        val hostAbs = hostFile.absolutePath
        // [fix/audit-0917-b9] Sort by LINUX-key length, not host length:
        // resolveHostPath matches on the longest linux prefix, and a short
        // linux key can be bound to a long host mount — sorting by host
        // length let that pair win and produced a too-short linuxPath for a
        // file that also lives under a longer linux key. Same ordering both
        // directions keeps this mirror of resolveHostPath faithful.
        val sorted = com.rikkaminis.app.sandbox.PRootKernel
            .bindMounts.entries.sortedByDescending { it.key.length }
        for ((linuxPrefix, hostBase) in sorted) {
            val baseNorm = hostBase.trimEnd('/')
            if (hostAbs == baseNorm || hostAbs.startsWith("$baseNorm/")) {
                val tail = hostAbs.removePrefix(baseNorm).removePrefix("/")
                val linuxPath = if (tail.isEmpty()) linuxPrefix else "$linuxPrefix/$tail"
                return when {
                    linuxPrefix == "/var/minis/shared" ->
                        Triple(WebAppShortcutRepository.SCOPE_SHARED, null, linuxPath)
                    linuxPrefix.startsWith("/var/minis/mounts/") -> {
                        val mountName = linuxPrefix.removePrefix("/var/minis/mounts/")
                            .substringBefore('/')
                        Triple(WebAppShortcutRepository.SCOPE_MOUNT, mountName, linuxPath)
                    }
                    else -> null  // memory/skills/etc — no WebApp support
                }
            }
        }
        return null
    }

    private fun resolveSession(context: Context, shortcut: WebAppShortcutEntity): File? {
        val sessionId = shortcut.scopeContext ?: return null
        // Absolute /var/minis/<perSession>/... — go through PRootKernel which
        // knows how to map per-session subdirs to <filesDir>/minis-sessions/<id>/<sub>.
        if (shortcut.htmlPath.startsWith("/var/minis/")) {
            return PRootKernel.resolveSessionHostPath(sessionId, shortcut.htmlPath, context)
        }
        // Relative path — under the session's attachments dir.
        // [sweep1014-b7a] Same root as AddToHomeSheet's write side
        // (SessionFileStore.sessionsRoot = filesDir/minis-sessions), so
        // shortcut bytes live inside the orphan-scan / deleteSession
        // cleanup face instead of a detached filesDir/sessions/.
        // Canonicalize + prefix-guard: the stored htmlPath is user-influenced,
        // so reject `/`, `..`, and symlink escapes out of the attachments dir.
        val attachmentsDir = File(SessionFileStore(context).sessionsRoot, "$sessionId/attachments")
        PRootKernel.safeResolveWithin(attachmentsDir, shortcut.htmlPath)?.let { return it }
        // Legacy fallback (read-only): pre-minis-sessions shortcuts stored
        // bytes under filesDir/sessions/. Keep them resolving; those dirs
        // are outside SessionFileStore's cleanup face (registered, never
        // written again).
        return PRootKernel.safeResolveWithin(
            File(context.filesDir, "sessions/$sessionId/attachments"),
            shortcut.htmlPath,
        )
    }
}

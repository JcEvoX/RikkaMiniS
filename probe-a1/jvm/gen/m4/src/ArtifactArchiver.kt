package probe

import java.io.File
import java.io.FileOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * [T-auto-backup-assets] Build a ZIP archive of artifact files WITHOUT loading
 * the whole payload into memory. The automatic backup runs on the app's
 * foreground path (Dispatchers.Default), so a multi-MB artifact set must stream
 * file-by-file into a ZipOutputStream rather than readBytes() the world.
 *
 * Pure JVM (java.util.zip only) so the framing is unit-testable.
 */
object ArtifactArchiver {

    /**
     * Stream [files] (paths relative to [root]) into a NEW zip at [out],
     * each entry prefixed with [entryPrefix] (e.g. "shared/") when non-empty.
     * Source files are read under [root] WITHOUT the prefix; the prefix only
     * decorates the archive entry name so a multi-root archive can route
     * entries back to their own root on restore. Each file is read fully into
     * memory one at a time — bounded by [ArtifactBackupScope.MAX_FILE_BYTES],
     * so the transient peak is ~4MB.
     */
    fun archiveToZip(
        root: File,
        files: List<ArtifactBackupScope.SelectedFile>,
        out: File,
        entryPrefix: String = "",
    ): Long {
        out.parentFile?.mkdirs()
        var totalBytes = 0L
        FileOutputStream(out).use { fos ->
            ZipOutputStream(fos.buffered()).use { zos ->
                totalBytes += writeEntries(zos, root, files, entryPrefix)
            }
        }
        return totalBytes
    }

    /**
     * Append [files] as entries to an already-open [ZipOutputStream] — lets a
     * caller pack several roots into ONE archive ([ConfigBackup] packs
     * shared/ + mcp/ that way). [entryPrefix] decorates entry names only.
     *
     * [transform] is an optional per-entry hook applied to the bytes read from
     * disk BEFORE they are written; its result becomes the archived content.
     * Returning **null DROPS the entry** (fail closed — never a verbatim
     * fallback) and appends its relPath to [droppedSink] when one is supplied,
     * so the caller can keep its file/skip accounting honest. A null
     * [transform] writes every entry verbatim (the pre-[dv20-a1] behaviour).
     *
     * Entry names/prefixes are never touched by the hook — a redacted entry
     * keeps its name, so restore routing and on-disk layout are unchanged.
     */
    fun writeEntries(
        zos: ZipOutputStream,
        root: File,
        files: List<ArtifactBackupScope.SelectedFile>,
        entryPrefix: String = "",
        transform: ((String, ByteArray) -> ByteArray?)? = null,
        droppedSink: MutableList<String>? = null,
    ): Long {
        var totalBytes = 0L
        for (f in files) {
            val src = File(root, f.relPath.replace('/', File.separatorChar))
            if (!src.isFile) continue
            var bytes = src.readBytes() // bounded by MAX_FILE_BYTES per file
            if (transform != null) {
                val transformed = transform(f.relPath, bytes)
                if (transformed == null) {
                    droppedSink?.add(f.relPath)
                    continue
                }
                bytes = transformed
            }
            zos.putNextEntry(ZipEntry(entryPrefix + f.relPath))
            zos.write(bytes)
            zos.closeEntry()
            totalBytes += bytes.size
        }
        return totalBytes
    }

    /**
     * Inflate a ZIP from [zipFile] into per-prefix destination directories.
     * Entries are routed by their first path segment: `shared/...` lands under
     * [destByPrefix]["shared"], `mcp/...` under ["mcp"]. Entries with an
     * unknown prefix fall back to [fallbackDir] when non-null (single-root
     * callers), otherwise they are rejected (defence in depth: artifact names
     * come from an untrusted file). Returns the number of entries written.
     */
    fun extractZipByPrefix(
        zipFile: File,
        destByPrefix: Map<String, File>,
        fallbackDir: File? = null,
    ): Int {
        var count = 0
        java.util.zip.ZipFile(zipFile).use { zf ->
            val entries = zf.entries()
            while (entries.hasMoreElements()) {
                val entry = entries.nextElement()
                if (entry.isDirectory) continue
                val name = entry.name
                if (name.contains("..") || name.startsWith("/") ||
                    name.contains('\\') || name.contains(':')
                ) {
                    continue
                }
                val slash = name.indexOf('/')
                val prefix = if (slash > 0) name.substring(0, slash) else ""
                val destDir = destByPrefix[prefix] ?: fallbackDir ?: continue
                val rel = if (slash > 0) name.substring(slash + 1) else name
                if (rel.isEmpty() || rel.contains("..")) continue
                val target = File(destDir, rel)
                // [fix-R12 D8] The old `startsWith(destDir.canonicalPath)` let a
                // sibling like "shared_evil/x" pass a "shared" check. Require the
                // canonical separator boundary (same shape as SkillRepository).
                if (!isUnderBase(destDir.canonicalPath, target.canonicalPath)) continue
                target.parentFile?.mkdirs()
                zf.getInputStream(entry).use { input ->
                    target.outputStream().use { output -> input.copyTo(output) }
                }
                count++
            }
        }
        return count
    }

    /** Legacy single-destination extraction (used by tests / callers with one root). */
    fun extractZip(zipFile: File, destDir: File): Int =
        extractZipByPrefix(zipFile, emptyMap(), destDir)

    /**
     * [fix-R12 D8] Canonical prefix-boundary guard: target must equal base or
     * live under base + File.separator ("shared_evil" must not pass a "shared" check).
     */
    internal fun isUnderBase(baseCanonical: String, targetCanonical: String): Boolean =
        targetCanonical == baseCanonical ||
            targetCanonical.startsWith(baseCanonical + File.separator)
}

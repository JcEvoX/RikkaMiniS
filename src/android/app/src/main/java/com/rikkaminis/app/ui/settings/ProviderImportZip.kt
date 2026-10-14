package com.rikkaminis.app.ui.settings

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Log
import java.io.File
import java.io.IOException
import java.util.UUID
import java.util.zip.ZipException
import java.util.zip.ZipInputStream

/**
 * Helpers for the zip-format import flow used by [ProviderListScreen].
 *
 * Streams a user-picked .zip through [ZipInputStream], extracts into a unique
 * subdirectory of [Context.getCacheDir], then feeds every supported leaf file
 * (.json / .txt) through the existing single-file import handler.
 *
 * Hardens against the "zip slip" path-traversal class by resolving every
 * entry to a canonical path and rejecting anything that escapes the staging
 * root. Skips `__MACOSX/` noise, dot-files, and `.DS_Store`. Cleans up the
 * staging directory on every exit path.
 */
internal object ProviderImportZip {

    private const val TAG = "ProviderImportZip"
    private val SUPPORTED_EXTS = setOf("json", "txt")

    // [sweep1014-P9] Decompression bomb guard: a provider-config import zip is
    // never anywhere near this big. Without the caps, zin.copyTo extracted an
    // entry of unbounded size straight to disk and file.readText() slurped it
    // into memory.
    private const val MAX_ENTRY_BYTES = 2L * 1024 * 1024  // 2 MB per zip entry
    private const val MAX_TOTAL_BYTES = 16L * 1024 * 1024 // 16 MB across all entries

    /**
     * Read the display name reported by the [Uri]'s content provider, if any.
     * Returns null on failure — callers should fall back to MIME-type sniffing.
     */
    fun queryDisplayName(context: Context, uri: Uri): String? {
        return try {
            context.contentResolver.query(
                uri,
                arrayOf(OpenableColumns.DISPLAY_NAME),
                null,
                null,
                null,
            )?.use { cursor ->
                if (cursor.moveToFirst()) cursor.getString(0) else null
            }
        } catch (e: Exception) {
            null
        }
    }

    /**
     * Extracts the zip behind [uri], runs every .json/.txt entry through
     * [onImportSingle], and dispatches a summary / failure toast via the
     * provided callbacks. Always tears down the staging dir before returning.
     *
     * @param onImportSingle Should return a non-null label on success, null on
     *   "invalid file" — matches the existing provider import contract.
     */
    fun importFromZip(
        context: Context,
        uri: Uri,
        onImportSingle: (String) -> String?,
        onExtractFailed: () -> Unit,
        onNoSupported: () -> Unit,
        onSummary: (ok: Int, total: Int) -> Unit,
    ) {
        val stagingRoot = File(context.cacheDir, "import-staging")
        if (!stagingRoot.exists()) stagingRoot.mkdirs()
        val stagingDir = File(stagingRoot, UUID.randomUUID().toString())
        if (!stagingDir.mkdirs() && !stagingDir.isDirectory) {
            onExtractFailed()
            return
        }
        val rootCanonical = stagingDir.canonicalPath
        val rootPrefix = rootCanonical + File.separator

        try {
            val input = context.contentResolver.openInputStream(uri)
            if (input == null) {
                onExtractFailed()
                return
            }
            try {
                var totalBytes = 0L
                ZipInputStream(input.buffered()).use { zin ->
                    while (true) {
                        val entry = zin.nextEntry ?: break
                        try {
                            if (entry.isDirectory) continue
                            val rawName = entry.name
                            if (shouldSkipEntry(rawName)) continue

                            val target = File(stagingDir, rawName)
                            val canonical = target.canonicalPath
                            // ── zip-slip guard ───────────────────────────────
                            if (canonical != rootCanonical && !canonical.startsWith(rootPrefix)) {
                                Log.w(TAG, "rejected zip-slip entry: $rawName")
                                continue
                            }
                            val entryBytes = copyEntryCapped(zin, target, totalBytes)
                            if (entryBytes < 0) {
                                Log.w(
                                    TAG,
                                    "zip exceeds size limit at entry: $rawName " +
                                        "(caps: entry=$MAX_ENTRY_BYTES, total=$MAX_TOTAL_BYTES)",
                                )
                                onExtractFailed()
                                return
                            }
                            totalBytes += entryBytes
                        } finally {
                            zin.closeEntry()
                        }
                    }
                }
            } catch (e: ZipException) {
                Log.w(TAG, "zip extract failed", e)
                onExtractFailed()
                return
            } catch (e: IOException) {
                Log.w(TAG, "io while extracting zip", e)
                onExtractFailed()
                return
            }

            val candidates = mutableListOf<File>()
            collectSupportedFiles(stagingDir, candidates)
            if (candidates.isEmpty()) {
                onNoSupported()
                return
            }

            var ok = 0
            for (file in candidates) {
                try {
                    val content = file.readText()
                    val label = onImportSingle(content)
                    if (label != null) ok += 1
                } catch (e: Exception) {
                    Log.w(TAG, "skip import for ${file.name}: ${e.message}")
                }
            }
            onSummary(ok, candidates.size)
        } finally {
            runCatching { stagingDir.deleteRecursively() }
        }
    }

    /**
     * [sweep1014-P9] Copies the current zip entry into [target], capped at
     * [MAX_ENTRY_BYTES] per entry and [MAX_TOTAL_BYTES] across the whole
     * archive (counted against [totalSoFar]). Returns the copied byte count,
     * or -1 when a limit is exceeded (the truncated target doesn't matter —
     * the staging directory is torn down on every exit path).
     */
    private fun copyEntryCapped(zin: ZipInputStream, target: File, totalSoFar: Long): Long {
        target.parentFile?.mkdirs()
        var entryBytes = 0L
        target.outputStream().use { out ->
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = zin.read(buf)
                if (n < 0) break
                entryBytes += n
                if (entryBytes > MAX_ENTRY_BYTES || totalSoFar + entryBytes > MAX_TOTAL_BYTES) {
                    return -1
                }
                out.write(buf, 0, n)
            }
        }
        return entryBytes
    }

    private fun shouldSkipEntry(name: String): Boolean {
        if (name.startsWith("__MACOSX/") || name.contains("/__MACOSX/")) return true
        val leaf = name.substringAfterLast('/')
        if (leaf.isEmpty()) return true
        if (leaf == ".DS_Store") return true
        if (leaf.startsWith(".")) return true
        return false
    }

    private fun collectSupportedFiles(dir: File, out: MutableList<File>) {
        val children = dir.listFiles() ?: return
        for (child in children) {
            if (child.isDirectory) {
                collectSupportedFiles(child, out)
            } else {
                val ext = child.extension.lowercase()
                if (ext in SUPPORTED_EXTS) out += child
            }
        }
    }
}

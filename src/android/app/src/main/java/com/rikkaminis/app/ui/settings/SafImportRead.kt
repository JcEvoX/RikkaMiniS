package com.rikkaminis.app.ui.settings

import android.content.ContentResolver
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Log
import java.io.ByteArrayOutputStream
import java.io.InputStream

/**
 * [deep-audit-1045 B-07] Bounded reads for user-picked SAF documents.
 *
 * Both file-import screens read the picked document straight into the heap
 * before looking at it:
 *
 *  * `ProviderListScreen`'s non-zip branch ran
 *    `openInputStream(uri)?.bufferedReader()?.use { it.readText() }` — no upper
 *    bound at all, while the `.zip` branch next door is capped by
 *    [ProviderImportZip].
 *  * `SkillsManagementScreen.readUriBytes` ran a bare `use { it.readBytes() }`,
 *    and the only real bound downstream (`SkillRepository.readZipEntries`,
 *    `maxExpanded = 32 MiB`) applies to the EXPANDED entries — it cannot help
 *    the raw array that was already materialised upstream.
 *
 * A user-pickable 2 GB `.json` / `.zip` therefore reached `OutOfMemoryError`
 * before any validation ran. Everything SAF-shaped in the two screens now goes
 * through [readSafBytes], so there is one cap per import kind and one place to
 * audit.
 *
 * The bound is enforced twice on purpose:
 *  1. `OpenableColumns.SIZE` is queried FIRST purely as a cheap early reject —
 *     providers may report a wrong (or absent) size, so it is never trusted;
 *  2. the stream read itself stops one byte past the cap ([readAtMost]), which
 *     is the authority. A lying provider still cannot get past it.
 *
 */

// ponytail: the archive path still materialises the picked bundle once (now capped at 32 MiB) instead of streaming the zip entries through the extractor | 天花板: a legitimately larger bundle (or a format that needs >32 MiB raw) is refused outright | 升级触发: a user report of a real bundle above the cap, or `readZipEntries` raising its own expanded limit.

private const val TAG = "SafBoundedRead"

/**
 * Cap for a single provider JSON import, aligned with
 * [ProviderImportZip]'s `MAX_TOTAL_BYTES` (16 MiB): both branches of the SAME
 * import entry point must not disagree about what "too big" means, and one
 * provider instance is a few KB of JSON in practice.
 */
internal const val MAX_PROVIDER_JSON_BYTES = 16L * 1024 * 1024

/**
 * Cap for a picked skill bundle, aligned with `SkillRepository.readZipEntries`'
 * `maxExpanded` (32 MiB of EXPANDED entries): a zip's expanded size is never
 * smaller than its raw size, so anything above this is already unimportable
 * downstream and only burns heap on the way there. A plain `SKILL.md` is far
 * smaller.
 */
internal const val MAX_SKILL_IMPORT_BYTES = 32L * 1024 * 1024

/**
 * Outcome of a bounded SAF read. [TooLarge] is deliberately distinct from
 * [Failed]: an oversized pick is a user-visible "this file is too big" and must
 * not be reported as a read error (nor swallowed — every caller branches on it).
 */
internal sealed class SafRead {
    class Ok(val bytes: ByteArray) : SafRead()
    object TooLarge : SafRead()
    object Failed : SafRead()
}

/** Pure decision — a declared size above the cap is rejected before any read. JVM-covered. */
internal fun declaredSizeExceeds(declaredBytes: Long?, cap: Long): Boolean =
    declaredBytes != null && declaredBytes > cap

/**
 * Read at most [cap] bytes from [input] and close nothing (the caller owns the
 * stream). Returns null when the stream holds MORE than [cap] bytes.
 *
 * The overflow is detected by probing a single byte past the cap, so a stream
 * that is exactly [cap] bytes long is accepted whole — a `while (total < cap)`
 * loop would silently truncate it instead. Pure `java.io`, no Android types:
 * this is the decision the JVM arms exercise.
 */
internal fun readAtMost(input: InputStream, cap: Long): ByteArray? {
    require(cap >= 0L) { "cap must not be negative" }
    val out = ByteArrayOutputStream(minOf(cap, 64L * 1024L).toInt())
    val buffer = ByteArray(8 * 1024)
    var total = 0L
    while (true) {
        // Room left plus the one-byte probe. Never ask for more than the buffer.
        val room = cap - total + 1L
        if (room <= 0L) return null
        val want = minOf(room, buffer.size.toLong()).toInt()
        val read = input.read(buffer, 0, want)
        if (read <= 0) return out.toByteArray()
        total += read.toLong()
        if (total > cap) return null
        out.write(buffer, 0, read)
    }
}

/** Declared size from the URI's provider, or null when it does not report one. */
internal fun querySafSize(resolver: ContentResolver, uri: Uri): Long? =
    try {
        resolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { cursor ->
            val index = cursor.getColumnIndex(OpenableColumns.SIZE)
            if (cursor.moveToFirst() && index >= 0 && !cursor.isNull(index)) {
                cursor.getLong(index)
            } else {
                null
            }
        }
    } catch (_: Exception) {
        null
    }

/**
 * Read the picked document, refusing anything above [cap]. The single SAF read
 * entry point for the settings screens — do not call `openInputStream` directly
 * anywhere else (the scan gate derives its subject set from that).
 */
internal fun readSafBytes(resolver: ContentResolver, uri: Uri, cap: Long): SafRead {
    val declared = querySafSize(resolver, uri)
    if (declaredSizeExceeds(declared, cap)) {
        Log.w(TAG, "refusing $uri: provider declares $declared bytes, cap is $cap")
        return SafRead.TooLarge
    }
    return try {
        val stream = resolver.openInputStream(uri)
        if (stream == null) {
            Log.w(TAG, "refusing $uri: openInputStream returned null (revoked grant or dead provider)")
            SafRead.Failed
        } else {
            stream.use { input ->
                val bytes = readAtMost(input, cap)
                if (bytes == null) {
                    Log.w(TAG, "refusing $uri: stream exceeded the $cap-byte cap (declared=$declared)")
                    SafRead.TooLarge
                } else {
                    SafRead.Ok(bytes)
                }
            }
        }
    } catch (e: Exception) {
        Log.w(TAG, "read failed for $uri: ${e.message}")
        SafRead.Failed
    }
}

/**
 * Shared wording for a refused pick. Both screens show it on the same path that
 * already surfaces read failures, so an oversized file never looks like a
 * corrupt one. Kept as a literal (not a `strings.xml` entry) because this batch
 * must not touch the 8 language files — see the FIX-11 residual for the i18n
 * follow-up.
 */
internal fun safTooLargeText(capBytes: Long): String =
    "File is too large to import (limit ${capBytes / (1024L * 1024L)} MB)"

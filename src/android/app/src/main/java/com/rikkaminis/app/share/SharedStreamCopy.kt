package com.rikkaminis.app.share

import java.io.File
import java.io.InputStream

/** Bytes per read in [copySharedStream]. 8 KiB matches the pre-§1044-A08 inline
 *  copy and the rest of the repo's stream loops; large enough that a multi-MB
 *  share is a few hundred syscalls, small enough that an aborted copy never
 *  holds more than one buffer in memory. */
internal const val COPY_BUFFER_BYTES = 8192

/** [§1044-A08] Why a shared stream never reached the staging directory. */
internal enum class SharedStreamFailure {
    /** The resolver returned null — a remote provider refused or died. */
    OpenStreamNull,
    /** The per-file IO deadline elapsed before the source ran dry. */
    TimedOut,
    /** The single-file cap was crossed. */
    ExceededPerFile,
    /** The cumulative staging cap was crossed. */
    ExceededCumulative,
    /** Reading or writing threw; [SharedStreamCopy.Failed.detail] carries it. */
    ReadFailed,
}

/** [§1044-A08] Outcome of one staging copy. */
internal sealed class SharedStreamCopy {
    /** [bytes] were written to the destination and credited to the ledger. */
    data class Staged(val bytes: Long) : SharedStreamCopy()

    /** Nothing usable was staged; any destination file was removed. */
    data class Failed(
        val reason: SharedStreamFailure,
        val detail: String? = null,
    ) : SharedStreamCopy()
}

/**
 * [§1044-A08] Stream-copy core behind [ShareReceiverActivity.copyUriToStaging],
 * extracted so the "a null stream stages NOTHING" decision is pinned by JVM
 * tests instead of living only inside an Activity that cannot run off-device.
 *
 * The defect this closes: the previous inline copy read
 * `contentResolver.openInputStream(uri)?.use { … }` and then returned the staged
 * filename unconditionally. `openInputStream` may return null WITHOUT throwing
 * (a remote provider that refuses the read, or whose process was reaped), so the
 * `use` block never ran, no file was created, and the caller still produced a
 * `PendingShare.Item` pointing at a nonexistent file — a ghost attachment that
 * reached the composer, skipped the "Could not read shared file" toast, and only
 * surfaced later as a silently dropped attachment.
 *
 * Guards that MUST survive any refactor here (all preserved from the inline
 * version, and each one pinned by a test):
 *  * the source is null-checked BEFORE `dest.outputStream()` — creating the
 *    destination first is what turned "no stream" into a 0-byte ghost file;
 *  * [ioTimeoutMs] aborts a stalled provider and deletes the partial file;
 *  * [maxFileBytes] / [maxTotalBytes] reject oversized shares and delete the
 *    partial file they already wrote;
 *  * the ledger is credited ONLY after the copy finished ([credit] with the
 *    exact byte count), so a rejected share never counts against the cap;
 *  * any exception deletes the partial file and reports [SharedStreamFailure.ReadFailed].
 *
 * All effects are injected ([openStream] / [stagedBytesNow] / [credit] / [now])
 * so the caller keeps ownership of the ContentResolver and the process-lifetime
 * ledger; this function touches no Android API.
 */
internal fun copySharedStream(
    openStream: () -> InputStream?,
    dest: File,
    stagedBytesNow: () -> Long,
    credit: (Long) -> Unit,
    maxFileBytes: Long,
    maxTotalBytes: Long,
    ioTimeoutMs: Long,
    now: () -> Long,
): SharedStreamCopy {
    var total = 0L
    fun abort(reason: SharedStreamFailure, detail: String? = null): SharedStreamCopy {
        // The destination may already exist (partial write, or a name reuse) —
        // every failure path leaves the staging dir exactly as it found it.
        runCatching { dest.delete() }
        return SharedStreamCopy.Failed(reason, detail)
    }
    return try {
        val input = openStream()
            ?: return abort(SharedStreamFailure.OpenStreamNull)
        input.use { source ->
            dest.outputStream().use { output ->
                val buf = ByteArray(COPY_BUFFER_BYTES)
                val deadline = now() + ioTimeoutMs
                while (true) {
                    if (now() > deadline) return abort(SharedStreamFailure.TimedOut)
                    val n = source.read(buf)
                    if (n < 0) break
                    total += n
                    if (total > maxFileBytes) return abort(SharedStreamFailure.ExceededPerFile)
                    if (stagedBytesNow() + total > maxTotalBytes) {
                        return abort(SharedStreamFailure.ExceededCumulative)
                    }
                    output.write(buf, 0, n)
                }
            }
        }
        credit(total)
        SharedStreamCopy.Staged(total)
    } catch (e: Exception) {
        abort(SharedStreamFailure.ReadFailed, e.message)
    }
}

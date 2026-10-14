package com.rikkaminis.app.data

import android.content.Context
import com.rikkaminis.app.data.repository.ChatRepository
import com.rikkaminis.app.data.repository.SkillRepository
import com.rikkaminis.app.logging.AppLogger
import java.io.File
import java.io.IOException
import java.nio.file.FileVisitResult
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.SimpleFileVisitor
import java.nio.file.StandardCopyOption
import java.nio.file.attribute.BasicFileAttributes
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Clone a session (or its skill/memory artifacts) into a fresh local session.
 * Mirrors iOS `SessionForkManager`. Android MVP only implements the **local**
 * branch of the iOS API — `forkSession` (from iCloud-mirrored remote devices)
 * is deferred until we ship a cross-device sync layer; see spec §7.8.
 *
 * The returned session carries "(Copy)" suffix and entirely new message ids,
 * so the duplicate and its source are independent. Compact markers ARE cloned
 * (T-session-duplicate-compact-marker-android, iOS parity e8ac8b82) with their
 * message-id references remapped onto the duplicated messages, so the compact
 * divider/shadow is preserved on the copy.
 *
 * [sweep1014-B10] duplicateSession also copies the source session's on-disk
 * artifacts — `minis-sessions/<sid>/{workspace,attachments,offloads,browser}`
 * and `media/<yyyy/MM/dd>/<sid>` — onto the duplicate (same relative layout,
 * runtime files like sockets/locks skipped), so attachment and
 * `minis://workspace` references in the copied messages resolve in the new
 * session instead of dangling as "File not found". The copy is best-effort:
 * it runs on IO after the DB copy is committed, and a partial failure is
 * logged without aborting the clone.
 */
class SessionForkManager(
    private val chatRepository: ChatRepository,
    private val skillRepository: SkillRepository? = null,
    private val filesDir: File,
) {
    companion object {
        private const val TAG = "SessionForkManager"

        /** Per-session artifact subdirs (same set the sandbox binds per session). */
        internal val ARTIFACT_SUBDIRS = listOf("attachments", "offloads", "workspace", "browser")
    }

    /**
     * Deep-copy a local session: creates a new session with the same model id
     * and copies every message's payload. Returns the new session's id or
     * null if [sessionId] doesn't exist.
     *
     * Carries over user-meaningful session metadata so the duplicate looks
     * and behaves like the source:
     *   - `category` — drives the session-list icon + colour swatch (see
     *     SessionListScreen.categoryStyle). The bug that motivated this fix:
     *     a "Coding" session duplicated as a generic uncategorised chat with
     *     a default icon. iOS duplicateSession copies this too.
     *   - `modelBinding` — user-pinned model override; if the source session
     *     was pinned to a specific model, the copy should keep it pinned.
     *   - `memoryEnabled` — user-toggled per-session; expectation is that
     *     the copy inherits the source's memory setting rather than
     *     defaulting to on.
     *
     * Deliberately NOT copied (the duplicate is a new session at creation
     * time, not a clone of the source's lifecycle):
     *   - `pinnedAt` (the copy starts unpinned)
     *   - `source` (origin tag like "shortcut" — the copy was made via the
     *     duplicate button, not via the original entry point)
     *   - `editCount` (starts at 0)
     *   - `lastMessage` (createSession seeds it; refreshed by appendMessage)
     *   - `id` / `createdAt` / `updatedAt` (new identity, current timestamps)
     */
    suspend fun duplicateSession(sessionId: String): String? {
        val source = chatRepository.getSession(sessionId) ?: run {
            AppLogger.warning(TAG, "duplicateSession: $sessionId not found")
            return null
        }
        val messages = chatRepository.loadMessages(sessionId)
        val dupTitle = "${source.title ?: "Chat"} (Copy)"
        val new = chatRepository.createSession(
            modelId = source.modelId,
            title = dupTitle,
        )
        // Mirror iOS: carry over the source's category onto the copy so the
        // session-list icon + colour stay consistent with the original.
        if (source.category != null) {
            chatRepository.updateSessionTitleAndCategory(new.id, dupTitle, source.category)
        }
        // Carry over the user's per-session model binding (the "pinned model"
        // override) so duplicating a coding session pinned to GPT-5 stays
        // pinned to GPT-5 in the copy.
        if (source.modelBinding != null) {
            chatRepository.updateSessionBinding(new.id, source.modelBinding, source.modelId)
        }
        // Carry over the per-session memory toggle. Sessions default
        // memoryEnabled=1, so we only need to push the disabled state through;
        // the matching state is already in place from createSession.
        if (source.memoryEnabled == 0) {
            chatRepository.dao.updateMemoryEnabled(new.id, 0)
        }
        // T239: carry over the per-session thinking-mode override so a
        // duplicate of a "tuned to MEDIUM" session opens at MEDIUM rather
        // than reverting to the default (parity with memoryEnabled).
        source.thinkingOverride?.let { override ->
            chatRepository.dao.updateThinkingOverride(new.id, override)
        }
        // [T-session-duplicate-compact-marker-android] Track old→new message id
        // and old→new sort_order so copied compact markers (which reference DB
        // message ids + legacy sort_orders) can be remapped onto the freshly
        // minted duplicate messages. appendMessage returns the new MessageEntity
        // (id + sortOrder), so no re-query is needed. Aligns iOS e8ac8b82.
        val oldToNewId = HashMap<String, String>()
        val oldToNewSort = HashMap<String, Int>()
        // [audit-0917] Delete the half-built copy if any append fails. The
        // session row was already created above, so an exception mid-loop (DB
        // full, constraint violation, cancellation) left a "(Copy)" session
        // holding a truncated prefix of the original — indistinguishable from
        // a real duplicate, and nothing else ever cleans it up.
        try {
            for (msg in messages) {
                val newMsg = chatRepository.appendMessage(
                    sessionId = new.id,
                    role = msg.role,
                    partsJson = msg.partsJson,
                    tokenUsage = msg.tokenUsage,
                    reasoningContent = msg.reasoningContent,
                    // [F-233] appendMessage accepts 7 business fields; this copy
                    // used to pass only 4, silently dropping the usage
                    // attribution and the terminal error sticker. Consequence:
                    // the branch session's usage records fell back to the
                    // session's *current* model (wrong attribution), and
                    // ChatTranscriptRebuild's error sticker vanished in the copy.
                    usageModelId = msg.usageModelId,
                    usageEntryId = msg.usageEntryId,
                    errorInfo = msg.errorInfo,
                )
                oldToNewId[msg.id] = newMsg.id
                oldToNewSort[msg.id] = newMsg.sortOrder
            }
        } catch (t: Throwable) {
            AppLogger.warning(TAG, "duplicateSession: copy failed, removing ${new.id}: ${t.message}")
            runCatching { chatRepository.deleteSession(new.id) }
                .onFailure { AppLogger.warning(TAG, "duplicateSession: cleanup failed: ${it.message}") }
            throw t
        }

        // [T-session-duplicate-compact-marker-android] Copy compact markers
        // AFTER all messages are appended (so the new sort_orders exist). A
        // session with zero markers makes this a clean no-op — the normal
        // duplicate path is untouched. Each marker is remapped independently so
        // multi-compact sessions copy all dividers, in created_at order
        // (listCompactMarkers returns ASC by created_at).
        val markers = chatRepository.dao.listCompactMarkers(sessionId)
        if (markers.isNotEmpty()) {
            // Remap a referenced message id through the copy map. null → null.
            fun remapId(id: String?): String? = id?.let { oldToNewId[it] }
            // Resolve a legacy sort_order fallback to the NEW message's
            // sort_order via the referenced id; keep the original when the id
            // is null or can't be resolved (legacy markers without ids).
            fun remapSort(messageId: String?, original: Int): Int =
                messageId?.let { oldToNewSort[it] } ?: original
            var copied = 0
            for (marker in markers) {
                // All source messages are copied, so every non-null id field
                // MUST resolve. If one doesn't, the source marker is already
                // dangling — skip it rather than write a dangling copy.
                val firstKeptNew = remapId(marker.firstKeptMessageId)
                val lastCompactedNew = remapId(marker.lastCompactedMessageId)
                val boundaryNew = remapId(marker.boundaryMessageId)
                if ((marker.firstKeptMessageId != null && firstKeptNew == null) ||
                    (marker.lastCompactedMessageId != null && lastCompactedNew == null) ||
                    (marker.boundaryMessageId != null && boundaryNew == null)
                ) {
                    AppLogger.warning(
                        TAG,
                        "duplicateSession: skipping compact marker ${marker.id} — a referenced message id is not in the copy map (dangling source marker)",
                    )
                    continue
                }
                val copy = marker.copy(
                    id = java.util.UUID.randomUUID().toString(),
                    sessionId = new.id,
                    firstKeptSortOrder = remapSort(marker.firstKeptMessageId, marker.firstKeptSortOrder),
                    uiBoundarySortOrder = marker.uiBoundarySortOrder?.let {
                        remapSort(marker.boundaryMessageId, it)
                    },
                    boundaryMessageId = boundaryNew,
                    firstKeptMessageId = firstKeptNew,
                    lastCompactedMessageId = lastCompactedNew,
                    // summary / compactedCount / createdAt / version verbatim.
                )
                chatRepository.dao.insertCompactMarker(copy)
                copied++
            }
            AppLogger.info(TAG, "duplicateSession: copied $copied/${markers.size} compact marker(s) to ${new.id}")
        }

        // [sweep1014-B10] Copy the source session's on-disk artifacts onto the
        // duplicate: minis-sessions/<sid>/{workspace,attachments,offloads,browser}
        // and media/<yyyy/MM/dd>/<sid>. Without this, every attachment /
        // minis://workspace reference / offload file in the copied messages
        // dangles (File not found) in the new session. The DB copy is already
        // committed at this point, so the artifact copy is best-effort — a
        // partial failure is logged but never aborts the clone.
        copySessionArtifacts(sessionId, new.id)

        AppLogger.info(
            TAG,
            "duplicated session $sessionId → ${new.id} (${messages.size} msgs, " +
                "category=${source.category}, modelBinding=${source.modelBinding}, " +
                "memoryEnabled=${source.memoryEnabled})",
        )
        return new.id
    }

    /**
     * [sweep1014-B10] Copy the source session's on-disk artifacts to the
     * duplicate session:
     *   1. `minis-sessions/<src>/{attachments,offloads,workspace,browser}` →
     *      `minis-sessions/<new>/<subdir>` (session-relative layout preserved);
     *   2. `media/<yyyy>/<MM>/<dd>/<src>` → `media/<yyyy>/<MM>/<dd>/<new>`
     *      (the MediaStore dated-path layout, so copied MediaRef.relativePath
     *      entries resolve with the session id swapped).
     *
     * Runtime files (Unix sockets, FIFOs, symlinks, Chromium `Singleton*`
     * locks, `*.lock` / `*.sock`) are skipped — see [isCopyableArtifactEntry].
     * Never throws: per-entry I/O failures are collected and logged as a
     * summary warning (max 10 shown), so a partial copy can never abort the
     * clone; the caller decides what the clone is worth.
     */
    private suspend fun copySessionArtifacts(sourceSessionId: String, newSessionId: String) {
        val (totalFiles, failures) = withContext(Dispatchers.IO) {
            var copied = 0
            val failures = mutableListOf<String>()

            // 1. Session dir tree: minis-sessions/<sid>/{subdir}.
            val sourceBase = File(filesDir, "minis-sessions/$sourceSessionId")
            val targetBase = File(filesDir, "minis-sessions/$newSessionId")
            for (subdir in ARTIFACT_SUBDIRS) {
                val src = File(sourceBase, subdir)
                if (!src.isDirectory) continue
                runCatching { copySessionDirTree(src, File(targetBase, subdir)) }
                    .onSuccess { (n, failed) ->
                        copied += n
                        failures += failed
                    }
                    .onFailure { failures.add("$subdir: ${it.message}") }
            }

            // 2. Media tree: media/<yyyy>/<MM>/<dd>/<sid> — the MediaStore
            //    dated-path layout (see MediaStore.saveMedia). Copy each date
            //    bucket to the same structure with the new session id.
            val mediaBase = File(filesDir, "media")
            for (year in mediaBase.listDirsSafe()) {
                for (month in year.listDirsSafe()) {
                    for (day in month.listDirsSafe()) {
                        val src = File(day, sourceSessionId)
                        if (!src.isDirectory) continue
                        val dst = File(File(File(mediaBase, year.name), month.name), day.name)
                            .let { File(it, newSessionId) }
                        runCatching { copySessionDirTree(src, dst) }
                            .onSuccess { (n, failed) ->
                                copied += n
                                failures += failed
                            }
                            .onFailure {
                                failures.add("media/${year.name}/${month.name}/${day.name}: ${it.message}")
                            }
                    }
                }
            }
            copied to failures
        }
        if (failures.isEmpty()) {
            AppLogger.info(TAG, "duplicateSession: copied $totalFiles artifact file(s) to $newSessionId")
        } else {
            AppLogger.warning(
                TAG,
                "duplicateSession: artifact copy to $newSessionId PARTIAL — $totalFiles file(s) copied, " +
                    "${failures.size} failure(s): " + failures.take(10).joinToString("; "),
            )
        }
    }

    private fun File.listDirsSafe(): List<File> =
        listFiles { f -> f.isDirectory }?.toList() ?: emptyList()

    /**
     * Clone a skill into the local library. When [skillRepository] is wired,
     * reuses `importFromContent` so the skill lands in DB + `SKILL.md` just
     * like a fresh import. Returns whether the import succeeded.
     */
    fun copySkill(content: String, source: SkillRepository.ImportSource = SkillRepository.ImportSource.FILE): Boolean {
        val repo = skillRepository ?: run {
            AppLogger.warning(TAG, "copySkill: SkillRepository not injected")
            return false
        }
        return repo.importFromContent(content, source) != null
    }

    /**
     * Persist a memory note (plain Markdown / text) under
     * `<filesDir>/minis-global/memory/<fileName>`. Mirrors iOS
     * `SessionForkManager.copyRemoteMemory` which writes under
     * `minisMemoryPersistentDir`. Overwrites if the file already exists.
     */
    fun copyMemory(fileName: String, content: String): Boolean {
        if (fileName.contains("/") || fileName.contains("..")) {
            AppLogger.warning(TAG, "copyMemory: rejecting unsafe fileName '$fileName'")
            return false
        }
        val dir = File(filesDir, "minis-global/memory").apply { mkdirs() }
        return try {
            File(dir, fileName).writeText(content)
            true
        } catch (e: Exception) {
            AppLogger.warning(TAG, "copyMemory: write failed: ${e.message}")
            false
        }
    }
}

/** Convenience: default-wire against application context. */
fun SessionForkManager(
    context: Context,
    chatRepository: ChatRepository,
    skillRepository: SkillRepository? = null,
): SessionForkManager = SessionForkManager(
    chatRepository = chatRepository,
    skillRepository = skillRepository,
    filesDir = context.filesDir,
)

/** [sweep1014-B10] Per-session artifact subdirs mirrored from the sandbox bind list. */
internal val SESSION_ARTIFACT_SUBDIRS: List<String> =
    com.rikkaminis.app.data.SessionForkManager.ARTIFACT_SUBDIRS

/**
 * [sweep1014-B10] Pure copy-plan decision for one entry in a session artifact
 * tree. Excluded:
 *  - non-regular / non-directory entries — Unix sockets, FIFOs, devices and
 *    symlinks (the runtime offload socket, Chromium profile symlinks);
 *  - `Singleton*` — Chromium profile runtime locks/sockets (SingletonLock /
 *    SingletonSocket / SingletonCookie) that only make sense in the live
 *    browser instance;
 *  - `*.lock` / `*.sock` — runtime lock/socket files.
 *
 * Top-level (not a method on [SessionForkManager]) so it can be JVM-tested
 * without Android classes.
 */
internal fun isCopyableArtifactEntry(name: String, isRegularFile: Boolean, isDirectory: Boolean): Boolean {
    if (!isRegularFile && !isDirectory) return false
    if (name.startsWith("Singleton")) return false
    if (name.endsWith(".lock") || name.endsWith(".sock")) return false
    return true
}

/**
 * [sweep1014-B10] Recursively copy [srcDir] → [dstDir], skipping entries that
 * fail [isCopyableArtifactEntry]. Returns (files copied, failures as
 * `"entry: reason"` strings). A missing source dir yields an empty result.
 * Per-entry I/O failures are collected into the failure list (copy continues);
 * the only thrown error is a failing `mkdirs` of the destination root.
 *
 * Top-level so it can be JVM-tested without Android classes.
 */
internal fun copySessionDirTree(srcDir: File, dstDir: File): Pair<Int, List<String>> {
    var copied = 0
    val failures = mutableListOf<String>()
    if (!srcDir.isDirectory) return copied to failures
    val srcPath = srcDir.toPath()
    val dstPath = dstDir.toPath()
    Files.createDirectories(dstPath)
    Files.walkFileTree(
        srcPath,
        object : SimpleFileVisitor<Path>() {
            override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                // The root itself is always descended into (its name may be a
                // legit subdir name like "offloads" — the exclusion list only
                // applies to entries inside the tree).
                if (dir != srcPath &&
                    !isCopyableArtifactEntry(dir.fileName.toString(), attrs.isRegularFile, attrs.isDirectory)
                ) {
                    return FileVisitResult.SKIP_SUBTREE
                }
                Files.createDirectories(dstPath.resolve(srcPath.relativize(dir)))
                return FileVisitResult.CONTINUE
            }

            override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                if (!isCopyableArtifactEntry(file.fileName.toString(), attrs.isRegularFile, attrs.isDirectory)) {
                    return FileVisitResult.CONTINUE
                }
                return try {
                    Files.copy(
                        file,
                        dstPath.resolve(srcPath.relativize(file)),
                        StandardCopyOption.REPLACE_EXISTING,
                    )
                    copied++
                    FileVisitResult.CONTINUE
                } catch (t: Throwable) {
                    failures.add("${file.fileName}: ${t.message}")
                    FileVisitResult.CONTINUE
                }
            }

            override fun visitFileFailed(file: Path, t: IOException): FileVisitResult {
                failures.add("${file.fileName}: ${t.message}")
                return FileVisitResult.CONTINUE
            }
        },
    )
    return copied to failures
}

package com.rikkaminis.app.data.repository

import com.rikkaminis.app.data.db.WebAppShortcutDao
import com.rikkaminis.app.data.db.WebAppShortcutEntity
import java.util.UUID

/**
 * T-pwa-1 (renamed Pwa → WebApp): thin wrapper around
 * [WebAppShortcutDao]. Constructed once in [com.rikkaminis.app.MinisApp]
 * and shared across UI surfaces (chat attachment chip in T-pwa-2, file
 * browser row in T-pwa-3).
 */
class WebAppShortcutRepository(private val dao: WebAppShortcutDao) {

    suspend fun create(
        htmlPath: String,
        pathScope: String,
        scopeContext: String?,
        title: String,
        iconRef: String,
        iconCachePath: String?,
        sourceSessionId: String?,
    ): WebAppShortcutEntity {
        val entity = WebAppShortcutEntity(
            id = UUID.randomUUID().toString(),
            htmlPath = htmlPath,
            pathScope = pathScope,
            scopeContext = scopeContext,
            title = title,
            iconRef = iconRef,
            iconCachePath = iconCachePath,
            createdAt = System.currentTimeMillis(),
            sourceSessionId = sourceSessionId,
        )
        dao.insert(entity)
        return entity
    }

    suspend fun get(id: String): WebAppShortcutEntity? = dao.getById(id)

    suspend fun list(): List<WebAppShortcutEntity> = dao.getAll()

    suspend fun delete(id: String) = dao.deleteById(id)

    suspend fun update(entity: WebAppShortcutEntity) = dao.update(entity)

    /**
     * [da1030-A ISSUE-01] Re-point every shortcut pinned against the draft id
     * onto the real session id.
     *
     * Called from `ChatViewModel.ensureSession()`'s `if (isDraft)` block, right
     * after `migrateDraftResources()` has moved the session's disk tree to the
     * real id and deleted the draft directory. A shortcut's `scopeContext` /
     * `sourceSessionId` were frozen at pin time (`__new__<uuid>`, see
     * AddToHomeSheet), and [com.rikkaminis.app.webapp.WebAppPathResolver]'s
     * relative-path branch resolves `htmlPath` under
     * `minis-sessions/<scopeContext>/attachments` — which the migration just
     * moved away. Without this hop the pinned icon keeps pointing at the dead
     * draft key and reports "Source Missing" forever; nothing else re-points
     * it (the row is written once, at pin time).
     *
     * No-op without touching the DAO when either id is blank or when they are
     * equal, so callers can invoke it unconditionally.
     */
    suspend fun renameSessionShortcuts(fromDraft: String, toReal: String) {
        if (fromDraft.isBlank() || toReal.isBlank() || fromDraft == toReal) return
        dao.renameScopeContext(fromDraft, toReal)
        dao.renameSourceSessionId(fromDraft, toReal)
    }

    companion object {
        const val SCOPE_SESSION_ATTACHMENT = "session_attachment"
        const val SCOPE_SHARED = "shared"
        const val SCOPE_MOUNT = "mount"
    }
}

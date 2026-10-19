package com.rikkaminis.app.data.db

import androidx.room.Dao
import androidx.room.Delete
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Update

@Dao
interface WebAppShortcutDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(entity: WebAppShortcutEntity)

    @Update
    suspend fun update(entity: WebAppShortcutEntity)

    @Delete
    suspend fun delete(entity: WebAppShortcutEntity)

    @Query("DELETE FROM webapp_shortcuts WHERE id = :id")
    suspend fun deleteById(id: String)

    @Query("SELECT * FROM webapp_shortcuts WHERE id = :id LIMIT 1")
    suspend fun getById(id: String): WebAppShortcutEntity?

    @Query("SELECT * FROM webapp_shortcuts ORDER BY created_at DESC")
    suspend fun getAll(): List<WebAppShortcutEntity>

    // [da1030-A ISSUE-01] Draft-promotion re-point. A shortcut pinned while the
    // chat was still a draft stores `__new__<uuid>` in BOTH scope columns
    // (AddToHomeSheet writes `source.sessionId`), and `migrateDraftResources`
    // moves the bytes to the real id + deletes the draft directory. Every
    // reader (WebAppPathResolver.resolveSession, relative-path branch) then
    // resolves under a directory that no longer exists, so the launcher icon
    // reports "Source Missing". Two single-column UPDATEs (not a read-modify-
    // write) so a concurrent pin/delete cannot be clobbered. Both columns must
    // move together: leaving `source_session_id` behind strands the
    // "which chat did this come from" link on the dead draft key.
    @Query("UPDATE webapp_shortcuts SET scope_context = :toSessionId WHERE scope_context = :fromSessionId")
    suspend fun renameScopeContext(fromSessionId: String, toSessionId: String)

    @Query("UPDATE webapp_shortcuts SET source_session_id = :toSessionId WHERE source_session_id = :fromSessionId")
    suspend fun renameSourceSessionId(fromSessionId: String, toSessionId: String)
}

package com.rikkaminis.app.webapp

import com.rikkaminis.app.data.db.WebAppShortcutDao
import com.rikkaminis.app.data.db.WebAppShortcutEntity
import com.rikkaminis.app.data.repository.WebAppShortcutRepository
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * deep-audit-1030 ISSUE-01 — a WebApp shortcut pinned while the chat was still
 * a draft stores the draft key (`__new__<uuid>`) in its scope columns, and
 * `ChatViewModel.ensureSession()`'s draft-promotion block moves the session's
 * disk tree to the real id and DELETES the draft directory. Every reader
 * resolves `htmlPath` under `minis-sessions/<scope_context>/attachments`, so
 * without the re-point the pinned launcher icon reports "Source Missing"
 * forever — the row is written once, at pin time, and nothing else rewrites it.
 *
 * The repository hop is the unit under test; the DAO is faked in memory with
 * the two `@Query` statements implemented by their COLUMN semantics
 * (`scope_context` / `source_session_id`), not by transcribing Room's
 * generated code. Expectations are hard-coded literals.
 *
 * A Fake that implemented only the members the production call touches would
 * compile here and fail on CI's real SDK — every member of the DAO interface is
 * implemented below on purpose.
 */
class WebAppDraftSessionPromotionTest {

    private class FakeDao : WebAppShortcutDao {
        val rows = LinkedHashMap<String, WebAppShortcutEntity>()
        var scopeRenameCalls = 0
        var sourceRenameCalls = 0

        override suspend fun insert(entity: WebAppShortcutEntity) {
            rows[entity.id] = entity
        }

        override suspend fun update(entity: WebAppShortcutEntity) {
            rows[entity.id] = entity
        }

        override suspend fun delete(entity: WebAppShortcutEntity) {
            rows.remove(entity.id)
        }

        override suspend fun deleteById(id: String) {
            rows.remove(id)
        }

        override suspend fun getById(id: String): WebAppShortcutEntity? = rows[id]

        override suspend fun getAll(): List<WebAppShortcutEntity> = rows.values.toList()

        /** `UPDATE webapp_shortcuts SET scope_context = :to WHERE scope_context = :from` */
        override suspend fun renameScopeContext(fromSessionId: String, toSessionId: String) {
            scopeRenameCalls++
            for ((id, row) in rows.toList()) {
                if (row.scopeContext == fromSessionId) {
                    rows[id] = row.copy(scopeContext = toSessionId)
                }
            }
        }

        /** `UPDATE webapp_shortcuts SET source_session_id = :to WHERE source_session_id = :from` */
        override suspend fun renameSourceSessionId(fromSessionId: String, toSessionId: String) {
            sourceRenameCalls++
            for ((id, row) in rows.toList()) {
                if (row.sourceSessionId == fromSessionId) {
                    rows[id] = row.copy(sourceSessionId = toSessionId)
                }
            }
        }
    }

    private fun row(
        id: String,
        scopeContext: String?,
        sourceSessionId: String?,
        title: String = "Pinned app",
    ) = WebAppShortcutEntity(
        id = id,
        htmlPath = "$id/index.html",
        pathScope = WebAppShortcutRepository.SCOPE_SESSION_ATTACHMENT,
        scopeContext = scopeContext,
        title = title,
        iconRef = "html",
        iconCachePath = null,
        createdAt = 1_700_000_000_000L,
        sourceSessionId = sourceSessionId,
    )

    @Test
    fun `draft promotion re-points both scope columns of the draft row only`() = runBlocking {
        val dao = FakeDao()
        val repo = WebAppShortcutRepository(dao)
        dao.rows["draft-pin"] = row("draft-pin", "__new__draft1", "__new__draft1")
        dao.rows["other-session-pin"] = row("other-session-pin", "sess-other", "sess-other")

        repo.renameSessionShortcuts(fromDraft = "__new__draft1", toReal = "real-123")

        val moved = dao.rows.getValue("draft-pin")
        assertEquals("real-123", moved.scopeContext)
        assertEquals("real-123", moved.sourceSessionId)
        // Every other column rides through untouched — the row keeps its
        // identity, icon and html path.
        assertEquals("draft-pin/index.html", moved.htmlPath)
        assertEquals("Pinned app", moved.title)
        assertEquals(1_700_000_000_000L, moved.createdAt)

        val untouched = dao.rows.getValue("other-session-pin")
        assertEquals("sess-other", untouched.scopeContext)
        assertEquals("sess-other", untouched.sourceSessionId)

        assertEquals(1, dao.scopeRenameCalls)
        assertEquals(1, dao.sourceRenameCalls)
    }

    @Test
    fun `rows with a null scope are left alone`() = runBlocking {
        val dao = FakeDao()
        val repo = WebAppShortcutRepository(dao)
        // A HostFile pin carries scopeContext = null / sourceSessionId = null —
        // SQL `WHERE col = :from` never matches NULL, so the update must skip it.
        dao.rows["host-pin"] = row("host-pin", null, null)

        repo.renameSessionShortcuts(fromDraft = "__new__draft1", toReal = "real-123")

        val row = dao.rows.getValue("host-pin")
        assertNull(row.scopeContext)
        assertNull(row.sourceSessionId)
    }

    @Test
    fun `a row whose only scope column matches moves that column alone`() = runBlocking {
        val dao = FakeDao()
        val repo = WebAppShortcutRepository(dao)
        // Defensive: the two columns are independent in the schema, so a row
        // that matches on one must still not be given a value on the other.
        dao.rows["half"] = row("half", "__new__draft1", null)

        repo.renameSessionShortcuts(fromDraft = "__new__draft1", toReal = "real-123")

        val row = dao.rows.getValue("half")
        assertEquals("real-123", row.scopeContext)
        assertNull(row.sourceSessionId)
    }

    @Test
    fun `identical ids are a no-op and never touch the dao`() = runBlocking {
        val dao = FakeDao()
        val repo = WebAppShortcutRepository(dao)
        dao.rows["pin"] = row("pin", "real-123", "real-123")

        repo.renameSessionShortcuts(fromDraft = "real-123", toReal = "real-123")

        assertEquals(0, dao.scopeRenameCalls)
        assertEquals(0, dao.sourceRenameCalls)
        assertEquals("real-123", dao.rows.getValue("pin").scopeContext)
    }

    @Test
    fun `blank ids are a no-op and never touch the dao`() = runBlocking {
        val dao = FakeDao()
        val repo = WebAppShortcutRepository(dao)
        dao.rows["pin"] = row("pin", "", "   ")

        repo.renameSessionShortcuts(fromDraft = "", toReal = "real-123")
        repo.renameSessionShortcuts(fromDraft = "   ", toReal = "real-123")
        repo.renameSessionShortcuts(fromDraft = "__new__draft1", toReal = "")
        repo.renameSessionShortcuts(fromDraft = "__new__draft1", toReal = "  ")

        assertEquals(0, dao.scopeRenameCalls)
        assertEquals(0, dao.sourceRenameCalls)
        assertEquals("", dao.rows.getValue("pin").scopeContext)
    }
}

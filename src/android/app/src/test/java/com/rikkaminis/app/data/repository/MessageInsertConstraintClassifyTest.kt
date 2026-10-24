package com.rikkaminis.app.data.repository

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [dv29-A2] The constraint discriminator behind `appendMessage`'s retry gate,
 * pinned as literals.
 *
 * `appendMessage` used to assume every `SQLiteConstraintException` was the
 * `unique(session_id, sort_order)` race and replayed the insert. For the
 * foreign key on `messages.session_id → chat_sessions.id` (code 787) that
 * replay is pointless by construction — the parent is still missing — and the
 * second throw escaped the catch and killed the process (2026-10-24 12:57 ×2).
 *
 * ## Why the classifier is a pure function over the message
 *
 * The message is the only discriminator SQLite hands back, and this file is the
 * only place where that belief is spelled. Keeping it pure means the mapping is
 * verifiable without Room, without a device, and without the android.jar
 * question (a unit-test android.jar may drop an exception's message; that
 * possibility is why `appendMessage` below treats an UNKNOWN classification as
 * a single bounded replay rather than as the race).
 *
 * `sort_order` is part of the race test on purpose: `messages` also has a unique
 * PRIMARY KEY, and `UNIQUE constraint failed: messages.id` must NOT be retried
 * with the same colliding id.
 */
class MessageInsertConstraintClassifyTest {

    @Test
    fun `a sort order unique violation is the replayable race`() {
        assertEquals(
            MessageInsertConflict.SORT_ORDER,
            classifyMessageInsertConstraint(
                "UNIQUE constraint failed: messages.session_id, messages.sort_order " +
                    "(code 2067 SQLITE_CONSTRAINT_UNIQUE)",
            ),
        )
    }

    @Test
    fun `a foreign key violation is a missing session row`() {
        assertEquals(
            MessageInsertConflict.SESSION_ROW_MISSING,
            classifyMessageInsertConstraint(
                "FOREIGN KEY constraint failed (code 787 SQLITE_CONSTRAINT_FOREIGNKEY)",
            ),
        )
    }

    @Test
    fun `a primary key collision is not the sort order race`() {
        assertEquals(
            MessageInsertConflict.OTHER,
            classifyMessageInsertConstraint(
                "UNIQUE constraint failed: messages.id (code 1555 SQLITE_CONSTRAINT_PRIMARYKEY)",
            ),
        )
    }

    @Test
    fun `a named but unrelated constraint is other`() {
        assertEquals(
            MessageInsertConflict.OTHER,
            classifyMessageInsertConstraint("NOT NULL constraint failed: messages.role"),
        )
    }

    @Test
    fun `an absent message is unknown, never the race`() {
        assertEquals(MessageInsertConflict.UNKNOWN, classifyMessageInsertConstraint(null))
        assertEquals(MessageInsertConflict.UNKNOWN, classifyMessageInsertConstraint("   "))
    }
}

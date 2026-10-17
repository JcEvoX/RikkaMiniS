package com.rikkaminis.app.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [da1023-F] Draft-promotion session-id binding.
 *
 * `ChatViewModelStore.resolveSessionId` is the public alias hop for callers
 * that only hold the nav-arg session id (AppNavigation route builders). A
 * draft chat's route arg stays on `__new__<uuid>` for the whole screen even
 * after the first send promotes it to a real session row — those callers must
 * resolve through the alias table or navigate into the orphan draft key.
 *
 * Expected values are written as literal strings, never derived from the
 * code under test (mutation must not silently satisfy the assertion).
 */
class ChatSessionIdResolveTest {

    @Test
    fun `draft key resolves to real id after rename`() {
        val draft = "__new__577217c7-1111-2222-3333-444444444444"
        val real = "19550430-aaaa-bbbb-cccc-dddddddddddd"
        ChatViewModelStore.rename(draft, real)

        assertEquals(
            "19550430-aaaa-bbbb-cccc-dddddddddddd",
            ChatViewModelStore.resolveSessionId(draft),
        )
    }

    @Test
    fun `unknown id passes through unchanged`() {
        assertEquals(
            "19550430-aaaa-bbbb-cccc-dddddddddddd",
            ChatViewModelStore.resolveSessionId("19550430-aaaa-bbbb-cccc-dddddddddddd"),
        )
        assertEquals(
            "plain-non-draft-id",
            ChatViewModelStore.resolveSessionId("plain-non-draft-id"),
        )
    }

    @Test
    fun `released alias falls back to the input id`() {
        val draft = "__new__deadbeef-0000-0000-0000-000000000000"
        val real = "99999999-8888-7777-6666-555555555555"
        ChatViewModelStore.rename(draft, real)
        ChatViewModelStore.release(real)

        assertEquals(
            "__new__deadbeef-0000-0000-0000-000000000000",
            ChatViewModelStore.resolveSessionId(draft),
        )
    }

    @Test
    fun `two drafts promoted to distinct real ids keep separate mappings`() {
        val d1 = "__new__11111111-0000-0000-0000-000000000000"
        val r1 = "aaaaaaaa-0000-0000-0000-000000000001"
        val d2 = "__new__22222222-0000-0000-0000-000000000000"
        val r2 = "bbbbbbbb-0000-0000-0000-000000000002"
        ChatViewModelStore.rename(d1, r1)
        ChatViewModelStore.rename(d2, r2)

        assertEquals(
            "aaaaaaaa-0000-0000-0000-000000000001",
            ChatViewModelStore.resolveSessionId(d1),
        )
        assertEquals(
            "bbbbbbbb-0000-0000-0000-000000000002",
            ChatViewModelStore.resolveSessionId(d2),
        )
    }

    @Test
    fun `promoted draft and active session slot agree`() {
        val draft = "__new__cafebabe-0000-0000-0000-000000000000"
        val real = "12345678-1234-1234-1234-123456789012"
        ChatViewModelStore.rename(draft, real)
        ChatViewModelStore.setActiveSession(draft)

        assertEquals(
            "12345678-1234-1234-1234-123456789012",
            ChatViewModelStore.activeSessionId,
        )
        assertEquals(
            "12345678-1234-1234-1234-123456789012",
            ChatViewModelStore.resolveSessionId(draft),
        )
        ChatViewModelStore.clearActiveSession(draft)
    }

    @Test
    fun `resolveKey stays private and resolveSessionId is the public seam`() {
        // Structure assertion: katex_ready_race_check.py hard-pins the source
        // text `private fun resolveKey(sessionId: String)` — widening it would
        // break that gate. `getMethods()` only returns public members, so a
        // private `resolveKey` must be absent here while the new public
        // delegate is present.
        val publicMethods = ChatViewModelStore::class.java.methods.map { it.name }.toSet()
        assertTrue(
            "resolveSessionId must be a public method",
            publicMethods.contains("resolveSessionId"),
        )
        assertFalse(
            "resolveKey must stay private",
            publicMethods.contains("resolveKey"),
        )
    }

    @Test
    fun `resolveSessionId delegates to the same locked lookup as resolveKey`() {
        // Alias added AFTER a first resolve must be visible on the next call —
        // guards against a cached/snapshot-based implementation.
        val draft = "__new__feedface-0000-0000-0000-000000000000"
        val real = "eeeeeeee-0000-0000-0000-00000000000e"
        assertEquals(
            "__new__feedface-0000-0000-0000-000000000000",
            ChatViewModelStore.resolveSessionId(draft),
        )
        ChatViewModelStore.rename(draft, real)
        assertEquals(
            "eeeeeeee-0000-0000-0000-00000000000e",
            ChatViewModelStore.resolveSessionId(draft),
        )
    }
}

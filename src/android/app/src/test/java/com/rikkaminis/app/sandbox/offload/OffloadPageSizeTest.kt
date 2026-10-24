package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [dv29-A3 A-05 / A-07] The shared page-size contract, plus the market-link
 * package parse.
 *
 * Every expected value below is a LITERAL from the CLI contract (the documented
 * default / range and the refusal wording) — never derived from the production
 * constants or from the surfaces themselves, so changing a constant cannot move
 * the expectation with it and turn a mutation into a pass.
 */
class OffloadPageSizeTest {

    private fun args(vararg argv: String) = OffloadArgs(argv.toList())

    private fun ok(
        surface: PageSizeSurface,
        vararg argv: String,
        default: Int? = null,
    ): Int {
        val outcome = if (default == null) {
            PageSizeOutcome.resolve(args(*argv), surface)
        } else {
            PageSizeOutcome.resolve(args(*argv), surface, default)
        }
        return when (outcome) {
            is PageSizeOutcome.Ok -> outcome.value
            is PageSizeOutcome.Invalid ->
                throw AssertionError("expected a usable page size, got a refusal: ${outcome.message}")
        }
    }

    private fun refusal(
        surface: PageSizeSurface,
        vararg argv: String,
        default: Int? = null,
    ): String {
        val outcome = if (default == null) {
            PageSizeOutcome.resolve(args(*argv), surface)
        } else {
            PageSizeOutcome.resolve(args(*argv), surface, default)
        }
        return when (outcome) {
            is PageSizeOutcome.Ok ->
                throw AssertionError("expected a refusal, got the page size ${outcome.value}")
            is PageSizeOutcome.Invalid -> outcome.message
        }
    }

    // ── contacts: the 1048-A06 contract, now served by the shared resolver ──

    @Test
    fun `contacts answers 50 when nothing is spelled`() {
        assertEquals(50, ok(OffloadPageSize.CONTACTS))
        assertEquals(20, ok(OffloadPageSize.CONTACTS, default = 20))
    }

    @Test
    fun `contacts keeps the same refusal wording as before the move`() {
        assertEquals(
            "--max expects an integer between 1 and 500, got 'abc'",
            refusal(OffloadPageSize.CONTACTS, "--max", "abc"),
        )
        assertEquals(
            "--max expects an integer between 1 and 500, got 0",
            refusal(OffloadPageSize.CONTACTS, "--max", "0"),
        )
        assertEquals(
            "--max expects an integer between 1 and 500",
            refusal(OffloadPageSize.CONTACTS, "--max"),
        )
        assertEquals(
            "-n expects an integer between 1 and 500 (short form of --max)",
            refusal(OffloadPageSize.CONTACTS, "-n"),
        )
        assertEquals(
            "--max expects an integer between 1 and 500, got -5",
            refusal(OffloadPageSize.CONTACTS, "--max", "-5"),
        )
    }

    @Test
    fun `contacts upper bound stays inclusive at 500`() {
        assertEquals(500, ok(OffloadPageSize.CONTACTS, "--max", "500"))
        assertEquals(
            "--max expects an integer between 1 and 500, got 501",
            refusal(OffloadPageSize.CONTACTS, "--max", "501"),
        )
        // 500 is both the value the resolver refuses above and the number the
        // handler's MAX_PAGE_SIZE name answers — that name is DERIVED from this
        // surface now (`requireNotNull(OffloadPageSize.CONTACTS.max)`), and
        // OffloadCliArgsTest pins the name itself at 500.
        assertEquals(500, OffloadPageSize.CONTACTS.max)
    }

    // ── calendar: --limit with --max alias, no upper bound ─────────────────

    @Test
    fun `calendar refuses a negative or non-numeric limit instead of warning`() {
        // Before: exit 0, "events": [], and "_warning": "Returned -5 of 0 …".
        assertEquals(
            "--limit expects an integer >= 1, got -5",
            refusal(OffloadPageSize.CALENDAR, "--limit", "-5"),
        )
        assertEquals(
            "--limit expects an integer >= 1, got 0",
            refusal(OffloadPageSize.CALENDAR, "--limit", "0"),
        )
        assertEquals(
            "--limit expects an integer >= 1, got 'abc'",
            refusal(OffloadPageSize.CALENDAR, "--limit", "abc"),
        )
        assertEquals(
            "--limit expects an integer >= 1",
            refusal(OffloadPageSize.CALENDAR, "--limit"),
        )
    }

    @Test
    fun `calendar keeps --limit then --max precedence and the 50 default`() {
        assertEquals(50, ok(OffloadPageSize.CALENDAR))
        assertEquals(10, ok(OffloadPageSize.CALENDAR, "--limit", "10"))
        assertEquals(10, ok(OffloadPageSize.CALENDAR, "--limit=10"))
        assertEquals(10, ok(OffloadPageSize.CALENDAR, "--max", "10"))
        // --limit wins, but a misspelled alias is never silently discarded.
        assertEquals(10, ok(OffloadPageSize.CALENDAR, "--limit", "10", "--max", "20"))
        assertEquals(
            "--max expects an integer >= 1, got 'abc'",
            refusal(OffloadPageSize.CALENDAR, "--limit", "10", "--max", "abc"),
        )
        // No natural cap on a window-bounded listing.
        assertEquals(5000, ok(OffloadPageSize.CALENDAR, "--limit", "5000"))
    }

    // ── photos: 20/100 for list + near, 50/200 for album ───────────────────

    @Test
    fun `photos list and near refuse a clamped page`() {
        assertEquals(20, ok(OffloadPageSize.PHOTOS))
        assertEquals(
            "--limit expects an integer between 1 and 100, got 0",
            refusal(OffloadPageSize.PHOTOS, "--limit", "0"),
        )
        assertEquals(
            "--limit expects an integer between 1 and 100, got -1",
            refusal(OffloadPageSize.PHOTOS, "--limit", "-1"),
        )
        assertEquals(
            "--limit expects an integer between 1 and 100, got 'abc'",
            refusal(OffloadPageSize.PHOTOS, "--limit", "abc"),
        )
        assertEquals(
            "--limit expects an integer between 1 and 100, got 100000",
            refusal(OffloadPageSize.PHOTOS, "--limit", "100000"),
        )
        assertEquals(100, ok(OffloadPageSize.PHOTOS, "--limit", "100"))
    }

    @Test
    fun `photos album keeps its own documented range`() {
        assertEquals(50, ok(OffloadPageSize.PHOTOS_ALBUM))
        assertEquals(200, ok(OffloadPageSize.PHOTOS_ALBUM, "--limit", "200"))
        assertEquals(
            "--limit expects an integer between 1 and 200, got 201",
            refusal(OffloadPageSize.PHOTOS_ALBUM, "--limit", "201"),
        )
        assertEquals(200, ok(OffloadPageSize.PHOTOS_ALBUM, "--max", "200"))
    }

    // ── notification: --max, no upper bound ────────────────────────────────

    @Test
    fun `notification refuses a zero or unreadable max`() {
        assertEquals(50, ok(OffloadPageSize.NOTIFICATION))
        assertEquals(
            "--max expects an integer >= 1, got 0",
            refusal(OffloadPageSize.NOTIFICATION, "--max", "0"),
        )
        assertEquals(
            "--max expects an integer >= 1, got 'abc'",
            refusal(OffloadPageSize.NOTIFICATION, "--max", "abc"),
        )
        assertEquals(
            "--max expects an integer >= 1",
            refusal(OffloadPageSize.NOTIFICATION, "--max"),
        )
        assertEquals(1000, ok(OffloadPageSize.NOTIFICATION, "--max", "1000"))
    }

    // ── sessions: documented "default: 50, max: 100" ───────────────────────

    @Test
    fun `sessions refuses what it used to clamp to 1, 50 or 100`() {
        assertEquals(50, ok(OffloadPageSize.SESSIONS))
        assertEquals(
            "--limit expects an integer between 1 and 100, got 0",
            refusal(OffloadPageSize.SESSIONS, "--limit", "0"),
        )
        assertEquals(
            "--limit expects an integer between 1 and 100, got -1",
            refusal(OffloadPageSize.SESSIONS, "--limit", "-1"),
        )
        assertEquals(
            "--limit expects an integer between 1 and 100, got 1000",
            refusal(OffloadPageSize.SESSIONS, "--limit", "1000"),
        )
        assertEquals(
            "--limit expects an integer between 1 and 100, got 'abc'",
            refusal(OffloadPageSize.SESSIONS, "--limit", "abc"),
        )
        assertEquals(100, ok(OffloadPageSize.SESSIONS, "--limit", "100"))
        // `--max` is NOT a sessions spelling: it stays unread, as before.
        assertEquals(50, ok(OffloadPageSize.SESSIONS, "--max", "10"))
    }

    // ── the registry itself ───────────────────────────────────────────────

    @Test
    fun `every declared surface is registered`() {
        // The gate derives its scope from SURFACES; a declared-but-unregistered
        // surface would silently escape every rule.
        assertEquals(6, OffloadPageSize.SURFACES.size)
        assertTrue(OffloadPageSize.SURFACES.contains(OffloadPageSize.CONTACTS))
        assertTrue(OffloadPageSize.SURFACES.contains(OffloadPageSize.CALENDAR))
        assertTrue(OffloadPageSize.SURFACES.contains(OffloadPageSize.PHOTOS))
        assertTrue(OffloadPageSize.SURFACES.contains(OffloadPageSize.PHOTOS_ALBUM))
        assertTrue(OffloadPageSize.SURFACES.contains(OffloadPageSize.NOTIFICATION))
        assertTrue(OffloadPageSize.SURFACES.contains(OffloadPageSize.SESSIONS))
        for (surface in OffloadPageSize.SURFACES) {
            assertEquals("OffloadHandler.kt", surface.handlerFile.takeLast("OffloadHandler.kt".length))
            assertTrue(surface.names.isNotEmpty())
            assertTrue(surface.default >= 1)
            assertNotNull(surface.range)
        }
    }
}

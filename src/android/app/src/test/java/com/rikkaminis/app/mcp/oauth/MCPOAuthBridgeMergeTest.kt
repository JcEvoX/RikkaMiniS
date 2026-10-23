package com.rikkaminis.app.mcp.oauth

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [dv26-A2] deep-audit-1046 S-02 — the guest → host token promotion rule.
 *
 * [promoteTokensFromBridge] is Android-free on purpose, so the whole matrix
 * (fresher / older / equal / rotated / truncated / absent) is driven with
 * literals here and the store keeps only the file read + the mapping.
 *
 * The boundary cases are the point: an EQUAL expiry second with a different
 * access token is the same-second refresh (a `>` instead of `>=` silently stops
 * promoting it), and an OLDER bridge must never win (that is the shape of "the
 * app just authorized while the bridge write failed" — promoting it would roll
 * the host back to the token it just replaced).
 */
class MCPOAuthBridgeMergeTest {

    private fun stored(
        access: String = "at-host",
        refresh: String? = "rt-host",
        expiresAtMs: Long = 1_767_000_000_000L,
    ) = StoredTokenSnapshot(accessToken = access, refreshToken = refresh, expiresAtMs = expiresAtMs)

    /** Host expiry above is 1_767_000_000 s once converted — keep the two in step. */
    private val hostSeconds = 1_767_000_000L

    private fun bridge(
        access: String = "at-guest",
        refresh: String? = "rt-host",
        expiresAtSeconds: Long = hostSeconds + 3_600L,
    ) = BridgeTokenSnapshot(
        accessToken = access,
        refreshToken = refresh,
        expiresAtSeconds = expiresAtSeconds,
    )

    @Test
    fun `identical copies promote nothing`() {
        assertNull(
            promoteTokensFromBridge(
                stored(access = "at-same"),
                bridge(access = "at-same", expiresAtSeconds = hostSeconds),
            ),
        )
    }

    @Test
    fun `a missing bridge promotes nothing and never clears the host copy`() {
        assertNull(promoteTokensFromBridge(stored(), null))
    }

    @Test
    fun `a blank access token is a truncated file, not a refresh`() {
        assertNull(promoteTokensFromBridge(stored(), bridge(access = "   ")))
    }

    @Test
    fun `an older bridge never rolls the host back`() {
        // The host authorized after the guest last refreshed (or the bridge
        // write failed): the bridge is the stale copy even though its token
        // differs, so nothing may be promoted.
        assertNull(
            promoteTokensFromBridge(
                stored(access = "at-new"),
                bridge(access = "at-old", refresh = "rt-old", expiresAtSeconds = hostSeconds - 60L),
            ),
        )
    }

    @Test
    fun `a later expiry with a different token is promoted, seconds to ms`() {
        val promoted = promoteTokensFromBridge(
            stored(access = "at-old"),
            bridge(access = "at-new", expiresAtSeconds = hostSeconds + 3_600L),
        )
        assertEquals("at-new", promoted?.accessToken)
        assertEquals((hostSeconds + 3_600L) * 1000L, promoted?.expiresAtMs)
    }

    @Test
    fun `a same-second refresh is still promoted`() {
        // Equal expiry with a different token = two grants inside one second.
        // This is the case a `>` (strictly later) comparison silently drops.
        val promoted = promoteTokensFromBridge(
            stored(access = "at-old"),
            bridge(access = "at-new", expiresAtSeconds = hostSeconds),
        )
        assertEquals("at-new", promoted?.accessToken)
        assertEquals(hostSeconds * 1000L, promoted?.expiresAtMs)
    }

    @Test
    fun `a rotated refresh token is promoted even when the access token repeats`() {
        // Provider rotates on every grant: the dangerous half. Keeping the
        // host's superseded refresh token is what produced invalid_grant.
        val promoted = promoteTokensFromBridge(
            stored(access = "at-same", refresh = "rt-old"),
            bridge(access = "at-same", refresh = "rt-new", expiresAtSeconds = hostSeconds),
        )
        assertEquals("at-same", promoted?.accessToken)
        assertEquals("rt-new", promoted?.refreshToken)
    }

    @Test
    fun `a bridge without a refresh token keeps the host one`() {
        val promoted = promoteTokensFromBridge(
            stored(refresh = "rt-host"),
            bridge(access = "at-new", refresh = null, expiresAtSeconds = hostSeconds + 60L),
        )
        assertEquals("at-new", promoted?.accessToken)
        assertEquals("rt-host", promoted?.refreshToken)
    }

    @Test
    fun `a bridge with no expiry cannot override a host copy that has one`() {
        // expires_at absent (0) != expired: it is UNKNOWN, so the ordering
        // cannot be established and the host copy wins.
        assertNull(
            promoteTokensFromBridge(
                stored(access = "at-host"),
                bridge(access = "at-guest", expiresAtSeconds = 0L),
            ),
        )
    }

    @Test
    fun `two unknown expiries fall back to the token comparison`() {
        // Both sides carry no expiry (a provider that returns no expires_in).
        // Only the guest rewrites the bridge on its own, so a differing token
        // there is the newer one.
        val promoted = promoteTokensFromBridge(
            stored(access = "at-old", expiresAtMs = 0L),
            bridge(access = "at-new", expiresAtSeconds = 0L),
        )
        assertEquals("at-new", promoted?.accessToken)
        assertEquals(0L, promoted?.expiresAtMs)
    }

    @Test
    fun `a padded token from the bridge is trimmed`() {
        val promoted = promoteTokensFromBridge(
            stored(access = "at-old"),
            bridge(
                access = "  at-new\n",
                refresh = "rt-new \n",
                expiresAtSeconds = hostSeconds + 60L,
            ),
        )
        assertEquals("at-new", promoted?.accessToken)
        assertEquals("rt-new", promoted?.refreshToken)
    }
}

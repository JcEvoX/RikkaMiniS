package com.rikkaminis.app.mcp.oauth

/**
 * [dv26-A2] deep-audit-1046 S-02 — the one-way promotion rule that carries a
 * token the in-guest CLI refreshed back into the host store.
 *
 * The bridge file `<mcp-servers>/oauth/<server>.json` and
 * [MCPOAuthStore]'s `tokens::<server>` entry are two copies of ONE credential:
 *
 *   host → guest   `materializeBridgeFile` (authorization, and every config
 *                  save that reaches [MCPOAuthStore.reconcileForServer])
 *   guest → host   NOTHING — until now.
 *
 * The in-guest `minis-mcp-cli` refreshes on its own schedule
 * (`transport/http.py::_oauth_refresh` → `_save_oauth_tokens`) and rewrites the
 * bridge with a new `access_token`, a new `expires_at` and — when the provider
 * rotates refresh tokens — a new `refresh_token`. Because only the first
 * direction existed, the next save of any other field of that server
 * (timeout / header / description / client secret) ran `REFRESH_BRIDGE` with
 * the PRE-refresh snapshot, i.e. it wrote the rotated-away refresh token back
 * into the guest. The next refresh then failed with `invalid_grant` and the
 * authorization was dead for good (a permission prompt the user has to walk
 * through again), while the common case just paid one wasted refresh
 * round-trip.
 *
 * The merge is deliberately NARROW — it is a promotion, not a three-way merge
 * of the whole file:
 *
 *   - only the token triple (`access_token` / `refresh_token` / `expires_at`)
 *     can travel guest → host. `client_id`, `token_endpoint`, `client_secret`
 *     and `resource` stay authoritative on the host side (the saved `cfg` plus
 *     [MCPOAuthStore.clientSecret]); the reader in the store does not even
 *     parse them, so a stale or hand-edited bridge cannot redirect a refresh
 *     to another endpoint or another client (gate C3 pins that).
 *   - "fresher" is decided by the expiry instant, in seconds on both sides
 *     (the bridge stores SECONDS, [StoredTokenSnapshot] keeps ms). A bridge
 *     whose expiry is EARLIER than the stored one is never promoted: that is
 *     the shape of "the app just authorized and the guest-bridge write failed",
 *     and promoting it would roll the host back.
 *   - the same-second refresh is the case the timestamp cannot order (a
 *     provider that issues short-lived tokens fast enough for two grants to
 *     land in one second), so an equal expiry still promotes when the token
 *     itself differs.
 *   - a missing / unreadable / blank / older bridge promotes NOTHING. A damaged
 *     bridge file must never clear valid host tokens — that would be the
 *     inverse bug, and a worse one (sign the user out vs. one wasted refresh).
 *
 * Android-free on purpose: the decision is driven with literals in
 * `MCPOAuthBridgeMergeTest`, and the store only reads the file and maps the
 * two snapshots.
 */
internal data class BridgeTokenSnapshot(
    val accessToken: String,
    val refreshToken: String?,
    val expiresAtSeconds: Long,
)

/** The host copy of the same triple, in the units [MCPOAuthStore] persists. */
internal data class StoredTokenSnapshot(
    val accessToken: String,
    val refreshToken: String?,
    val expiresAtMs: Long,
)

/** What the host must persist (and materialize) when the bridge is fresher. */
internal data class TokenPromotion(
    val accessToken: String,
    val refreshToken: String?,
    val expiresAtMs: Long,
)

/**
 * Pure: the promoted triple, or null when the bridge carries nothing newer.
 *
 * Never throws and never inspects anything but the three token fields, so a
 * caller can run it on a bridge file it does not trust.
 */
internal fun promoteTokensFromBridge(
    stored: StoredTokenSnapshot,
    bridge: BridgeTokenSnapshot?,
): TokenPromotion? {
    if (bridge == null) return null
    val bridgeAccess = bridge.accessToken.trim()
    // A blank access token is not a refresh — it is a truncated / half-written
    // file. Promote nothing instead of signing the host out.
    if (bridgeAccess.isEmpty()) return null
    // Opaque tokens are base64url; surrounding whitespace is an editing
    // artifact, and carrying it into the `Authorization` header breaks the call.
    val bridgeRefresh = bridge.refreshToken?.trim()?.takeIf { it.isNotBlank() }
    val bridgeSeconds = if (bridge.expiresAtSeconds > 0) bridge.expiresAtSeconds else 0L
    val storedSeconds = if (stored.expiresAtMs > 0) stored.expiresAtMs / 1000 else 0L
    // Ordering gate for the WHOLE promotion. `expires_at` absent is 0, i.e.
    // "unknown" — and unknown on the bridge side against a known host expiry
    // means the guest file is not the newer copy (the CLI keeps the expiry the
    // host wrote, so a guest file without one was never refreshed by the CLI).
    // An older bridge is rejected ENTIRELY, rotation included: its refresh
    // token is the superseded one, and promoting that is what produced
    // invalid_grant.
    if (bridgeSeconds < storedSeconds) return null
    val replaced = bridgeAccess != stored.accessToken
    // Refresh-token rotation is the dangerous half: whatever the expiry says,
    // a DIFFERENT non-blank refresh token on the bridge means a grant happened
    // on the guest side, and keeping the host's old one is what produced
    // `invalid_grant`.
    val rotated = bridgeRefresh != null && bridgeRefresh != stored.refreshToken
    if (!replaced && !rotated) return null
    return TokenPromotion(
        accessToken = bridgeAccess,
        // Absent on the bridge = the provider did not rotate; keep the host's.
        refreshToken = bridgeRefresh ?: stored.refreshToken,
        expiresAtMs = bridgeSeconds * 1000,
    )
}

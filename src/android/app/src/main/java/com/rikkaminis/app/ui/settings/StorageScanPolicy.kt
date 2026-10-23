package com.rikkaminis.app.ui.settings

/**
 * [deep-audit-1045 B-03] What a re-entry into the Storage page must do with the
 * snapshot this process already holds.
 *
 * The three answers differ in *timing*, not in a boolean: a stale snapshot is
 * still rendered immediately and only then replaced (after waiting out the nav
 * transition), a missing one has nothing to render, and a forced refresh
 * (`force = true`, the reclaim / clear flows) must not inherit the stale
 * render. A `shouldRescan(...): Boolean` would have collapsed those into one
 * arm and pushed the timing decision back into the composable, which is exactly
 * where it cannot be tested.
 *
 * Kept in its own file, free of Compose and Android types, so the freshness
 * contract runs on a plain JVM (see `StorageScanPolicyTest`). The constants
 * themselves stay with the screen — `SNAPSHOT_STALE_MS` carries its jank
 * reasoning next to `RESCAN_DELAY_MS` and the viewport budget.
 */
internal enum class StorageScanDecision {
    /** The cached snapshot is inside the freshness window: render it and stop. */
    Reuse,

    /** The cached snapshot is stale: render it, wait out the transition, rescan. */
    RescanDeferred,

    /** No snapshot for this process, or the caller forced a refresh: rescan now. */
    RescanNow,
}

/**
 * The freshness gate. Same truth table the screen used to spell inline at the
 * top of `reload()`:
 *
 *  - `force` (reclaim / session clear / explicit refresh) always rescans, and
 *    never waits — those callers are waiting on a result, not on a transition;
 *  - a process with no snapshot scans immediately (nothing to render first);
 *  - otherwise the snapshot is reused while it is younger than [staleMs] and
 *    replaced in the background once it is not. The boundary is inclusive on
 *    the stale side (`>=`), so a snapshot exactly [staleMs] old is rescanned —
 *    the old inline test was `now - scannedAt < staleMs` and returned early,
 *    which is the same cut.
 *
 * A backwards clock (`now < cachedAt`) reads as fresh, matching the inline
 * test it replaces.
 */
internal fun decideScan(
    cachedAt: Long?,
    force: Boolean,
    now: Long,
    staleMs: Long,
): StorageScanDecision = when {
    force || cachedAt == null -> StorageScanDecision.RescanNow
    now - cachedAt >= staleMs -> StorageScanDecision.RescanDeferred
    else -> StorageScanDecision.Reuse
}

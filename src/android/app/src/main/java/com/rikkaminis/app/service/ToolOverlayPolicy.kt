package com.rikkaminis.app.service

/**
 * [T-android-overlay-intent-latch] Pure overlay decisions, extracted so the
 * race-free latch logic can be exercised on a plain JVM.
 *
 * The bug this file exists for: `ToolOverlayController.show()` publishes the
 * capsule from a **posted** Runnable, so `isShown` (which only flips once the
 * Runnable actually reaches `attach()`) is false for at least one main-thread
 * queue hop. Everything that decides visibility lives outside that hop, which
 * is why the decision inputs are plain booleans instead of controller state.
 */

/**
 * Should the capsule be on screen?
 *
 * @param wantShown a turn is in flight (assistant streaming or a tool
 *        running) — the [ToolOverlayController.show] "busy" arm.
 * @param permission SYSTEM_ALERT_WINDOW is granted
 *        ([ToolOverlayController.hasOverlayPermission]).
 * @param foreground Minis itself is in the foreground — the capsule must
 *        never draw over our own chat UI.
 * @param camera another app is using the camera — never draw over a preview.
 * @param hasPending a completion is pending user acknowledgement (the
 *        edge-scoped linger from [AgentForegroundService]).
 *
 * Note the user's master toggle is *not* an input: the service ANDs it
 * outside (`state.enabled && …`) because it also gates the permission nudge.
 */
internal fun resolveOverlayIntent(
    wantShown: Boolean,
    permission: Boolean,
    foreground: Boolean,
    camera: Boolean,
    hasPending: Boolean,
): Boolean = permission && !foreground && !camera && (wantShown || hasPending)

/**
 * Should a **queued** show Runnable still run? The latch is sampled inside
 * the Runnable, not when the Runnable is posted, so a hide that lands while
 * the attach is in flight wins the race instead of being overwritten by it.
 */
internal fun shouldRunShowPost(desired: Boolean): Boolean = desired

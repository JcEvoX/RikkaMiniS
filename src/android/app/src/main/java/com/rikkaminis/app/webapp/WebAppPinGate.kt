package com.rikkaminis.app.webapp

/**
 * [deep-audit-1048 B-06] Decision points for the "Add to Home Screen" sheet's
 * primary action, extracted so they are JVM-testable.
 *
 * The sheet used to have NO in-flight flag: `enabled = copiedFile != null &&
 * titleText.isNotBlank()` stayed true for the whole `create(...)` + pin round
 * trip, so a second tap during the (IPC-bound) pin started a second
 * `webAppShortcutRepository.create(...)` + `ShortcutPinner.pin(...)` for the
 * same shortcut id, and the first attempt's `cleanupCopiedAttachment()` then
 * deleted the copy out from under the second one. It also had no "the sheet is
 * gone" latch: the launch keeps running after the sheet is removed from
 * composition (Cancel, `onDismissRequest`, or an external `webAppSheetTarget =
 * null`), so a pin the user had already cancelled could still insert a DB row
 * and add a launcher icon.
 *
 * Both decisions are four booleans — no Android, no Compose — so the test
 * fixture can drive every combination without a device.
 */

/**
 * Whether the primary action must be tappable.
 *
 * [hasTitle] is the caller's `titleText.isNotBlank()`; [inFlight] is the
 * sheet's own pinning state; [closed] is the latch set by every teardown hook.
 * `closed` is included on purpose: a sheet that is leaving composition must not
 * present an actionable button in its last frame.
 */
internal fun pinActionEnabled(
    hasTarget: Boolean,
    hasTitle: Boolean,
    inFlight: Boolean,
    closed: Boolean,
): Boolean = hasTarget && hasTitle && !inFlight && !closed

/**
 * Whether the coroutine that already staged the icon may go on to insert the
 * shortcut row and ask the launcher to pin it.
 *
 * [inFlight] is required as well as `!closed`: if the latch was cleared because
 * a *different* attempt took over (or the state was reset), the stale coroutine
 * must not commit. Evaluated on the Main dispatcher, right after the staging
 * `withContext(Dispatchers.IO)` returns and before `create(...)`.
 */
internal fun pinShouldCommit(closed: Boolean, inFlight: Boolean): Boolean = !closed && inFlight

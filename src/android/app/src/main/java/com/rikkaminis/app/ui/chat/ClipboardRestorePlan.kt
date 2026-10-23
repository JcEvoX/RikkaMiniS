package com.rikkaminis.app.ui.chat

/**
 * [dv27-B4 / deep-audit-1047 B-07] What the selection round-trip must do to the
 * system clipboard once it has read the borrowed text back.
 *
 * `MinisMarkdownTextToolbar.withSelection` resolves the selection by writing it
 * into the clipboard (Compose's [androidx.compose.ui.platform.TextToolbar]
 * exposes no "give me the AnnotatedString selection" API). Its `finally`
 * restored the snapshot only when there WAS one:
 *
 *     if (original != null) runCatching { cm.setPrimaryClip(original) }
 *
 * For a user whose clipboard was empty (or unreadable — the read is
 * `runCatching { cm.primaryClip }.getOrNull()`) there is nothing to restore, so
 * "add selection to input" silently left the selected text sitting in the
 * system clipboard — a side effect outside the feature's contract, and one that
 * survives the app: it is visible to every other app and to the next paste.
 *
 * The decision is therefore "put back exactly what was there, and if there was
 * nothing, make the clipboard empty again" — two mutually exclusive outcomes,
 * never "leave our borrowed text behind".
 *
 * The Q-and-below split (platform `clearPrimaryClip()` vs. the empty-string
 * write) is the SAME decision the offload handler already owns — see
 * `ClipboardOffloadHandler.clearAction(atLeastQ)` /
 * `ClipboardClearAction` (dv22-A6 A-06): `clearPrimaryClip()` is only
 * available from API 29 (Q) on, and on API 26–28 the empty-string write was the
 * only way to empty the clipboard. This file does not invent a third variant;
 * it mirrors that contract so the UI path can be unit-tested without Android.
 *
 * Android-free on purpose: the sandbox JVM harness compiles this exact file,
 * so the assertion runs against the production text rather than a replica.
 */
internal enum class ClipboardRestoreAction {
    /** The user had a clip before we borrowed it — put that exact clip back. */
    RESTORE_ORIGINAL,

    /** Nothing was there and we run on API 29+ — empty the clipboard properly. */
    CLEAR_PLATFORM,

    /** Nothing was there and we run below API 29 — the empty-string write. */
    CLEAR_EMPTY_STRING,

    /**
     * Nothing to do: the caller cannot reach a clipboard manager at all.
     * Never reached in the toolbar path (the manager is resolved before the
     * borrow), but modelled so a future caller cannot silently fall back to
     * "leave the borrowed text behind".
     */
    NOOP,
}

/**
 * `hadOriginal` is the caller's `snapshot != null` (the snapshot is whatever
 * `primaryClip` returned before `onCopyRequested` ran).
 *
 * `atLeastQ` is passed in by the call site (the platform check lives at the
 * call site, exactly as `ClipboardOffloadHandler.clearAction` is called) so
 * this function stays a pure, JVM-testable decision.
 */
internal fun clipboardRestoreAction(
    hadOriginal: Boolean,
    atLeastQ: Boolean,
): ClipboardRestoreAction = when {
    hadOriginal -> ClipboardRestoreAction.RESTORE_ORIGINAL
    atLeastQ -> ClipboardRestoreAction.CLEAR_PLATFORM
    else -> ClipboardRestoreAction.CLEAR_EMPTY_STRING
}

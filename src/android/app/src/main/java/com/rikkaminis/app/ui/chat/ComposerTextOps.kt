package com.rikkaminis.app.ui.chat

/**
 * [dv24/B-05] Pure composition arithmetic for the composer's two text intents
 * (`ComposerCommand.AppendText` / `ComposerCommand.ReplaceText`).
 *
 * Kept in its own file, with no Compose and no Android dependency, because the
 * arithmetic is the ONLY part of the handoff that can be pinned on the JVM: the
 * intent channel, the field and the 150 ms IME-burst job all need a composition.
 * The three append callers disagree on their conventions on purpose —
 *
 *  - share injection:  `separator = "\n"`, keep the tail as-is, add the trailing
 *    space only for a URL (so the user can keep typing after the link);
 *  - cross-session Move-to transfer: `separator = "\n"`, no trailing space;
 *  - selection "add to input": `separator = " "`, trailing space, and the
 *    existing tail is trimmed so `"foo "` never becomes `"foo  bar "`.
 *
 * — so the conventions stay at the call sites and only this arithmetic is
 * shared.
 */
internal fun composeAppend(
    live: String,
    text: String,
    separator: String = " ",
    trimExistingEnd: Boolean = true,
    trailingSpace: Boolean = true,
): String {
    val base = if (trimExistingEnd) live.trimEnd() else live
    val head = if (base.isEmpty()) text else base + separator + text
    return if (trailingSpace) "$head " else head
}

/**
 * [dv24/B-05] Caret for `ComposerCommand.ReplaceText`: the caller's override
 * when it supplied one, the end of the new body otherwise.
 *
 * Clamped to the new body's bounds — `TextFieldValue` throws when a selection
 * points past its text, so a stale override computed against a longer draft
 * (a shorter prefilled message, a truncated context-exhausted stash) must not
 * escape as an out-of-range index.
 */
internal fun composerCaretFor(text: String, caretOverride: Int?): Int =
    (caretOverride ?: text.length).coerceIn(0, text.length)

package com.rikkaminis.app.ui.chat

/**
 * [dv25-B08 / deep-audit-1045 B-08] The two decisions of the "New Chat" draft
 * promotion that can be pinned on the JVM, extracted from the ViewModel for the
 * same reason `isDrawerRowVisible` (dv24-b3) was: the surrounding code needs a
 * Room database, a Compose host and a coroutine scope, but the DECISION is
 * three booleans and one string fallback.
 *
 * ### Why the cleanup guard exists
 *
 * `promoteDraftIfNeeded()` frees the draft slot synchronously and then promotes
 * asynchronously: `ensureSession()` inserts the session row (so `realSessionId`
 * becomes non-empty) and only several suspends later does `appendMessage()`
 * land the first `messages` row. Leaving the chat screen in that window fires
 * `ChatScreen`'s `onDispose` → `cleanupIfEmptyOnExit()`, whose IO coroutine read
 * `messageCount(sid) == 0`, deleted the brand-new row and called
 * `ChatViewModelStore.release(sid)` — which cancels `viewModelScope`, i.e. the
 * very coroutine doing the promotion. The composer had already been cleared, so
 * the user's typed body (and its attachments) were gone 100% of the time.
 *
 * The guard is a session-ownership bit, not a time window: while a promotion is
 * in flight the row belongs to that promotion, so the exit sweep has no claim on
 * it. A `delay`/grace period would only move the race (and would make the sweep
 * skip genuinely-empty rows the user actually left).
 *
 * ### Why the text fallback exists
 *
 * The composer commits ordinary typing immediately (`setInputText`) but debounces
 * large IME bursts — voice dictation, pinyin — for 150 ms, holding the newest
 * text in the composer's local buffer. Reading only the ViewModel string during
 * that window promotes the text as it was BEFORE the burst, silently dropping
 * the tail the user just dictated. The composer now publishes that pending
 * snapshot to the ViewModel ([ChatViewModel.publishPendingComposerText]); the
 * pending value is newer by construction (it is the field's live text), so it
 * wins, and it is nulled the moment the field commits past it.
 */
internal fun shouldSkipCleanup(
    hasRealId: Boolean,
    streaming: Boolean,
    hasAttachments: Boolean,
    promoteInFlight: Boolean,
): Boolean = !hasRealId || streaming || hasAttachments || promoteInFlight

/**
 * The body a promotion must persist: the composer's uncommitted IME burst when
 * one is pending (it is the field's live text and therefore newer), else the
 * committed ViewModel string.
 */
internal fun composerTextForPromotion(pendingBurst: String?, committed: String): String =
    pendingBurst ?: committed

/**
 * Promoted session title: the same 50-char prefix the pre-fix code wrote as the
 * title (the row label), trimmed. The BODY is the transcript row; this stays the
 * short label only.
 */
internal fun promoteTitle(text: String, maxChars: Int = 50): String =
    text.take(maxChars).trim()

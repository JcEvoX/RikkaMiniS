package com.rikkaminis.app.ui.navigation

import com.rikkaminis.app.deeplink.DeepLinkAction

/**
 * [deep-audit-1038 B-02] Cold-start route decision, computed ONCE per cold
 * start.
 *
 * `AppNavigation` recomposes on theme / font-size / system dark-mode changes
 * (it reads `MaterialTheme.colorScheme.background` for the NavHost transition
 * surface). The cold-start seeding used to live in two bare `val … = when …`
 * bindings at the top of the composable body, so every recomposition re-ran
 * them:
 *
 *  - `DeepLinkCoordinator.setPendingChatAction(OPEN_CAMERA)` /
 *    `setPendingHtmlPreview(...)` replayed into a live `ChatScreen` (the
 *    pending state is consumed reactively since da1028-C) — the system
 *    camera re-opened / the preview dialog re-popped;
 *  - `ComposerDraftStore.nextDraftId(context)` re-minted a fresh id once the
 *    draft slot was free, which changed `NavHost.startDestination`
 *    mid-flight (NavGraph rebuild) and orphaned the route the user was
 *    typing into — `saveText`'s slot guard then silently dropped the draft.
 *
 * This seam is deliberately Android-free so the per-cold-start contract is
 * exercised by plain JVM tests: every side effect is injected as a plain
 * function and COUNTED by the test. The caller wraps the call in
 * `remember(initialDeepLink) { … }` — `initialDeepLink` is an immutable
 * `MainActivity.onCreate` parameter, so the key never changes within one
 * composition lifetime.
 *
 * Branch order mirrors the original two-step resolver exactly:
 *  1. a pinned HTML-preview shortcut wins (seed the preview + land on its
 *     named chat; NO draft id is minted — the shortcut names a real session);
 *  2. `NewCameraChat` seeds `OPEN_CAMERA` and lands on a fresh draft;
 *  3. `NewChat` lands on a fresh draft;
 *  4. anything else (incl. null) lands on a fresh draft — the conversation
 *     drawer is the only session switcher, so cold start heads straight into
 *     a fresh draft chat (mirrors the New Chat launch mode, [remove-session-list]).
 */
internal fun coldStartStartDestination(
    initialDeepLink: DeepLinkAction?,
    nextDraftId: () -> String,
    chatRoute: (String) -> String,
    seedCamera: () -> Unit,
    seedHtmlPreview: (sessionId: String, resourcePath: String, title: String) -> Unit,
): String {
    val htmlShortcut = initialDeepLink as? DeepLinkAction.OpenHtmlPreview
    return when {
        htmlShortcut != null -> {
            // Seed coordinator before NavHost composition so ChatScreen sees
            // the pending state on its very first LaunchedEffect tick.
            seedHtmlPreview(htmlShortcut.sessionId, htmlShortcut.resourcePath, htmlShortcut.title)
            chatRoute(htmlShortcut.sessionId)
        }
        initialDeepLink is DeepLinkAction.NewCameraChat -> {
            seedCamera()
            chatRoute(nextDraftId())
        }
        initialDeepLink is DeepLinkAction.NewChat -> chatRoute(nextDraftId())
        else -> chatRoute(nextDraftId())
    }
}

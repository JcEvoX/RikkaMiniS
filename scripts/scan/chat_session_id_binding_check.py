#!/usr/bin/env python3
"""[da1023-F] gate — draft-promotion session-id binding at the UI seam.

After a draft chat's first send promotes it to a real session row
(`ChatSessionEntryExt.ensureSession` → `ChatViewModelStore.rename`), the
nav-route arg stays on the `__new__<uuid>` draft key for the whole screen
lifetime. Any entry point that keeps consuming the route arg binds to the
orphan draft key while the real resources live under the promoted id.

This gate pins the six audited entry points (deep-audit-1023 ISSUE-06):

  ChatScreen.kt
    1. markdown image tap handler — `remember(messages, …)` keyed on the
       ACTIVE id and `resolveMdMediaFile(..., viewModel.currentSessionId)`.
    2. ChatHistoryDrawer select — `if (id != viewModel.currentSessionId)` /
       `if (id == viewModel.currentSessionId)` (matches the drawer's own
       `currentSessionId = viewModel.currentSessionId` at its call site).
    3. ChatInputArea — `sessionId = viewModel.currentSessionId` (feeds the
       composer's MoveToSessionSheet self-filter and attachment funnel).
    4. WebAppSource.ChatAttachment — `sessionId = viewModel.currentSessionId`
       (AddToHomeSheet copies bytes into minis-sessions/<sid>/attachments/).
  AppNavigation.kt
    5. onOpenTerminal / onOpenTerminalWithCommand / onBrowseChatFiles route
       through `ChatViewModelStore.resolveSessionId(sessionId)`.
  MoveToSessionSheet.kt
    6. `LaunchedEffect(currentSessionId)` — re-runs the self-filter when the
       promoted id changes instead of caching the draft-era list.

  ChatScreen.kt (deep-audit-1027 ISSUE-01) — criterion 8 below
    7. pinned-shortcut HTML preview — the `LaunchedEffect` consuming
       `consumePendingHtmlPreview()` must be keyed on `viewModel.currentSessionId`,
       resolve through `resolvePathForSession(activeSessionId, …)` (never the
       global `resolveHostPath`), and accept the promoted id
       (`pending.sessionId != activeSessionId`) — the route arg alone leaves a
       shortcut pinned after draft promotion permanently unconsumed.

Bare route-arg usage at these sites must not come back. `resolveKey` itself
stays private — `katex_ready_race_check.py` hard-pins its source form.
"""
import argparse
import re
import sys
import tempfile
from pathlib import Path

MARKER = "CHAT_SESSION_ID_BINDING_SELFTEST=PASS"

REL_SCREEN = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatScreen.kt"
REL_NAV = "src/android/app/src/main/java/com/rikkaminis/app/ui/navigation/AppNavigation.kt"
REL_MOVE = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/MoveToSessionSheet.kt"
REL_STORE = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatViewModelStore.kt"

# (start token, end token) pairs isolating a call block; the block must
# contain `required` and must NOT contain `forbidden`.
SCREEN_CALL_SPECS = [
    (
        "ChatInputArea(",
        ")",
        {"required": "sessionId = viewModel.currentSessionId", "label": "ChatInputArea sessionId"},
    ),
    (
        "WebAppSource.ChatAttachment(",
        ")",
        {"required": "sessionId = viewModel.currentSessionId", "label": "WebAppSource.ChatAttachment sessionId"},
    ),
]

errors: "list[str]" = []


def strip_comments(text: str) -> str:
    """String-literal-aware comment stripper.

    The naive `re.sub(r"/\\*.*?\\*/", "", text, flags=re.S)` is unusable here:
    a `/*` inside a string literal plus a later `*/` swallows the whole body of
    the file (measured: 319,632 B -> 51,494 B on ChatScreen.kt), which silently
    turns every criterion into a false RED. This scanner walks the text,
    skipping string/char literals verbatim, so only real comments vanish.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            if text.startswith('"""', i):
                j = text.find('"""', i + 3)
                j = n if j == -1 else j + 3
            else:
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == '"' or text[j] == "\n":
                        j += 1
                        break
                    j += 1
            out.append(text[i:j])
            i = j
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    j += 1
                    break
                if text[j] == "\n":
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def extract_call_block(code: str, start: str, end: str) -> str:
    idx = code.find(start)
    if idx == -1:
        return ""
    return code[idx : code.find(end, idx) + len(end)] if end else code[idx:]


def _skip_string_literal(code: str, i: int) -> int:
    """Index just past the string literal starting at `code[i]` (a quote).

    Brace matching must not be fooled by `{`/`}` inside literals — the pinned
    block legitimately contains `"${pending.resourcePath}"` and warning text
    with interpolations. Mirrors the literal handling in [strip_comments].
    """
    quote = code[i]
    if quote == '"' and code.startswith('"""', i):
        j = code.find('"""', i + 3)
        return len(code) if j == -1 else j + 3
    j = i + 1
    n = len(code)
    while j < n:
        if code[j] == "\\":
            j += 2
            continue
        if code[j] == quote or code[j] == "\n":
            return j + 1
        j += 1
    return n


def extract_effect_block(code: str, marker: str) -> "tuple[str, str] | None":
    """Isolate the `LaunchedEffect(<keys>) { <body> }` block containing [marker].

    Returns `(keys, body)` — the key-list text and the braced body — or `None`
    when the marker is absent, no enclosing `LaunchedEffect(` precedes it, or
    the braces do not balance. Callers must treat `None` as a violation
    (fail-closed): a renamed or relocated block must never silently pass.
    """
    idx = code.find(marker)
    if idx == -1:
        return None
    start = code.rfind("LaunchedEffect(", 0, idx)
    if start == -1:
        return None
    brace = code.find("{", start)
    if brace == -1 or brace > idx:
        return None
    depth = 0
    i = brace
    n = len(code)
    while i < n:
        c = code[i]
        if c == '"' or c == "'":
            i = _skip_string_literal(code, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return code[start + len("LaunchedEffect(") : brace], code[brace : i + 1]
        i += 1
    return None


def check(root: Path) -> int:
    del errors[:]
    screen_path = root / REL_SCREEN
    nav_path = root / REL_NAV
    move_path = root / REL_MOVE
    store_path = root / REL_STORE
    for p in (screen_path, nav_path, move_path, store_path):
        if not p.is_file():
            print(f"FAIL {p} not found (wrong root?)")
            return 1

    screen = strip_comments(screen_path.read_text(encoding="utf-8"))
    nav = strip_comments(nav_path.read_text(encoding="utf-8"))
    move = strip_comments(move_path.read_text(encoding="utf-8"))
    store = strip_comments(store_path.read_text(encoding="utf-8"))

    # --- 1. markdown image tap handler ---
    if "remember<(String, String) -> Unit>(messages, viewModel.currentSessionId)" not in screen:
        errors.append(
            "ChatScreen.kt: markdown image tap handler must be keyed "
            "`remember<(String, String) -> Unit>(messages, viewModel.currentSessionId)`"
        )
    if re.search(r"resolveMdMediaFile\(\s*context,\s*ref\.source,\s*sessionId\s*\)", screen):
        errors.append(
            "ChatScreen.kt: resolveMdMediaFile must not receive the bare route `sessionId`"
        )
    if not re.search(r"resolveMdMediaFile\(\s*context,\s*ref\.source,\s*viewModel\.currentSessionId\s*\)", screen):
        errors.append(
            "ChatScreen.kt: resolveMdMediaFile must resolve media via `viewModel.currentSessionId`"
        )

    # --- 2. ChatHistoryDrawer select comparisons ---
    if re.search(r"if \(id (!=|==) sessionId\)", screen):
        errors.append(
            "ChatScreen.kt: drawer select must compare against `viewModel.currentSessionId`, "
            "not the bare route `sessionId` (route stays on the draft key after promotion)"
        )
    for op in ("!=", "=="):
        if f"if (id {op} viewModel.currentSessionId)" not in screen:
            errors.append(f"ChatScreen.kt: expected drawer comparison `if (id {op} viewModel.currentSessionId)`")

    # --- 3/4. call-block arguments ---
    for start, end, spec in SCREEN_CALL_SPECS:
        block = extract_call_block(screen, start, end)
        if not block:
            errors.append(f"ChatScreen.kt: call site `{start}` not found")
            continue
        if spec["required"] not in block:
            errors.append(
                f"ChatScreen.kt: `{spec['label']}` must pass `sessionId = viewModel.currentSessionId`"
            )

    # --- 5. AppNavigation route builders resolve through the alias table ---
    for label, needle in (
        ("onOpenTerminal", "Routes.terminal(sessionId = ChatViewModelStore.resolveSessionId(sessionId))"),
        ("onOpenTerminalWithCommand", "sessionId = ChatViewModelStore.resolveSessionId(sessionId)"),
        ("onBrowseChatFiles", "Routes.chatFiles(ChatViewModelStore.resolveSessionId(sessionId))"),
    ):
        if needle not in nav:
            errors.append(
                f"AppNavigation.kt: {label} must resolve the nav-arg id via "
                "`ChatViewModelStore.resolveSessionId(sessionId)` before navigating"
            )
    for label, forbidden in (
        ("onOpenTerminal", "Routes.terminal(sessionId = sessionId)"),
        ("onBrowseChatFiles", "Routes.chatFiles(sessionId)"),
    ):
        if forbidden in nav:
            errors.append(f"AppNavigation.kt: {label} navigates with the bare route `sessionId`")

    # --- 6. MoveToSessionSheet effect key ---
    if "LaunchedEffect(currentSessionId)" not in move:
        errors.append(
            "MoveToSessionSheet.kt: session list effect must be keyed `LaunchedEffect(currentSessionId)`"
        )
    if re.search(r"LaunchedEffect\(Unit\)", move):
        errors.append("MoveToSessionSheet.kt: stale `LaunchedEffect(Unit)` still present")

    # --- 7. the public seam must delegate, resolveKey must stay private ---
    if "fun resolveSessionId(sessionId: String): String" not in store:
        errors.append("ChatViewModelStore.kt: public `resolveSessionId` seam missing")
    if "resolveSessionId(sessionId: String): String =\n        resolveKey(sessionId)" not in store.replace("\r", "") and \
       "resolveSessionId(sessionId: String): String = resolveKey(sessionId)" not in store:
        errors.append(
            "ChatViewModelStore.kt: resolveSessionId must delegate to resolveKey "
            "(single alias table — no second lookup implementation)"
        )
    if "private fun resolveKey(sessionId: String)" not in store:
        errors.append(
            "ChatViewModelStore.kt: `private fun resolveKey(sessionId: String)` form changed "
            "(katex_ready_race_check.py pins it)"
        )

    # --- 8. pinned-shortcut HTML preview effect (deep-audit-1027 ISSUE-01) ---
    # Anchored on the consume call so a renamed/moved block is RED, not skipped.
    effect = extract_effect_block(screen, "consumePendingHtmlPreview()")
    if effect is None:
        errors.append(
            "ChatScreen.kt: pinned-shortcut block anchored on "
            "`consumePendingHtmlPreview()` is not inside a `LaunchedEffect` "
            "(renamed or moved? re-anchor this criterion, do not drop it)"
        )
    else:
        effect_keys, effect_body = effect
        if "viewModel.currentSessionId" not in effect_keys:
            errors.append(
                "ChatScreen.kt: pinned-shortcut LaunchedEffect must be keyed on "
                "`viewModel.currentSessionId` so a promoted draft re-runs it"
            )
        if "pendingHtmlPreviewState" not in effect_keys:
            errors.append(
                "ChatScreen.kt: pinned-shortcut LaunchedEffect must observe the pending "
                "state (`pendingHtmlPreviewState` key + `collectAsState()`), not a "
                "one-shot `.value` read — a hot-path deep link reusing the live "
                "ChatScreen would otherwise never re-run the effect"
            )
        if "collectAsState()" not in screen:
            errors.append("ChatScreen.kt: pinned-shortcut pending state is not collected")
        if "resolvePathForSession(" not in effect_body:
            errors.append(
                "ChatScreen.kt: pinned-shortcut must resolve via "
                "`PRootKernel.resolvePathForSession(activeSessionId, …)`"
            )
        if "resolveHostPath(" in effect_body:
            errors.append(
                "ChatScreen.kt: pinned-shortcut must not resolve through the global "
                "`resolveHostPath` (scans every session tree; needs boot; cross-session reads)"
            )
        if (
            "viewModel.currentSessionId" not in effect_body
            and "ChatViewModelStore.resolveSessionId(" not in effect_body
        ):
            errors.append(
                "ChatScreen.kt: pinned-shortcut must bind the session through "
                "`viewModel.currentSessionId` (or `ChatViewModelStore.resolveSessionId(…)`)"
            )
        if "pending.sessionId != activeSessionId" not in effect_body:
            errors.append(
                "ChatScreen.kt: pinned-shortcut must accept the live session id "
                "(`pending.sessionId != activeSessionId`), not the route arg alone"
            )

    if errors:
        for e in errors:
            print(f"FAIL {e}")
        print(f"\ngate RED: {len(errors)} violation(s)")
        return 1
    print("chat_session_id_binding_check: all criteria satisfied")
    return 0


# ---------------- selftest fixtures (minimal synthetic repo) ----------------

FIXTURE_STORE = '''package com.rikkaminis.app.ui.chat

object ChatViewModelStore {
    private val aliases = mutableMapOf<String, String>()

    @Synchronized
    private fun resolveKey(sessionId: String): String =
        aliases[sessionId] ?: sessionId

    @Synchronized
    fun resolveSessionId(sessionId: String): String =
        resolveKey(sessionId)
}
'''

# Each mutation is (name, fixture-mutator) applied to the GREEN fixture set.
# The mutated tree MUST go red for the gate to have teeth.
MUTATIONS = [
    (
        "MUT-1 ChatInputArea back to bare route id",
        lambda screen, nav, move: (screen.replace(
            "sessionId = viewModel.currentSessionId", "sessionId = sessionId", 1), nav, move),
    ),
    (
        "MUT-2 drawer comparisons back to bare route id",
        lambda screen, nav, move: (re.sub(
            r"if \(id (!=|==) viewModel\.currentSessionId\)", r"if (id \1 sessionId)", screen), nav, move),
    ),
    (
        "MUT-3 AppNavigation onOpenTerminal back to bare route id",
        lambda screen, nav, move: (screen, nav.replace(
            "Routes.terminal(sessionId = ChatViewModelStore.resolveSessionId(sessionId))",
            "Routes.terminal(sessionId = sessionId)"), move),
    ),
    (
        "MUT-4 AppNavigation onBrowseChatFiles back to bare route id",
        lambda screen, nav, move: (screen, nav.replace(
            "Routes.chatFiles(ChatViewModelStore.resolveSessionId(sessionId))",
            "Routes.chatFiles(sessionId)"), move),
    ),
    (
        "MUT-5 MoveToSessionSheet effect key back to Unit",
        lambda screen, nav, move: (screen, nav, move.replace(
            "LaunchedEffect(currentSessionId)", "LaunchedEffect(Unit)")),
    ),
    (
        "MUT-6 resolveMdMediaFile back to bare route id",
        lambda screen, nav, move: (screen.replace(
            "resolveMdMediaFile(context, ref.source, viewModel.currentSessionId)",
            "resolveMdMediaFile(context, ref.source, sessionId)"), nav, move),
    ),
    (
        "MUT-7 ChatAttachment back to bare route id",
        lambda screen, nav, move: (screen.replace(
            "sessionId = viewModel.currentSessionId,\n    sessionTitle = null",
            "sessionId = sessionId,\n    sessionTitle = null"), nav, move),
    ),
    (
        "MUT-8 remember key back to bare route id",
        lambda screen, nav, move: (screen.replace(
            "remember<(String, String) -> Unit>(messages, viewModel.currentSessionId)",
            "remember<(String, String) -> Unit>(messages, sessionId)"), nav, move),
    ),
    (
        "MUT-11 pinned shortcut back to the global resolver",
        lambda screen, nav, move: (screen.replace(
            "PRootKernel.resolvePathForSession(\n        activeSessionId,",
            "PRootKernel.resolveHostPath(\n        activeSessionId,"), nav, move),
    ),
    (
        "MUT-12 pinned shortcut drops the live-session comparison",
        lambda screen, nav, move: (screen.replace(
            "if (pending.sessionId != sessionId && pending.sessionId != activeSessionId) {",
            "if (pending.sessionId != sessionId) {"), nav, move),
    ),
    (
        "MUT-13 pinned shortcut key drops viewModel.currentSessionId",
        lambda screen, nav, move: (screen.replace(
            "LaunchedEffect(sessionId, viewModel.currentSessionId, pendingHtmlPreviewState) {",
            "LaunchedEffect(sessionId, pendingHtmlPreviewState) {"), nav, move),
    ),
    (
        "MUT-14 pinned shortcut forgets the consume anchor",
        lambda screen, nav, move: (screen.replace(
            "consumePendingHtmlPreview()", "consumePreview()", 1), nav, move),
    ),
    (
        "MUT-15 pinned shortcut stops observing the pending state",
        lambda screen, nav, move: (screen.replace(
            "LaunchedEffect(sessionId, viewModel.currentSessionId, pendingHtmlPreviewState) {",
            "LaunchedEffect(sessionId, viewModel.currentSessionId) {"), nav, move),
    ),
]


def _green_fixtures() -> "tuple[str, str, str]":
    screen = (
        "val markdownImageTapHandler = remember<(String, String) -> Unit>(messages, viewModel.currentSessionId) {\n"
        "    val resolved = resolveMdMediaFile(context, ref.source, viewModel.currentSessionId)\n"
        "}\n"
        "if (id != viewModel.currentSessionId) { }\n"
        "if (id == viewModel.currentSessionId) { }\n"
        "ChatInputArea(\n"
        "    viewModel = viewModel,\n"
        "    sessionId = viewModel.currentSessionId,\n"
        ")\n"
        "source = com.rikkaminis.app.webapp.WebAppSource.ChatAttachment(\n"
        "    uri = target.uri,\n"
        "    fileName = target.fileName,\n"
        "    sessionId = viewModel.currentSessionId,\n"
        "    sessionTitle = null,\n"
        ")\n"
        # deep-audit-1027 ISSUE-01: pinned-shortcut effect (criterion 8).
        "val pendingHtmlPreviewState by com.rikkaminis.app.deeplink.DeepLinkCoordinator\n"
        "    .pendingHtmlPreview.collectAsState()\n"
        "LaunchedEffect(sessionId, viewModel.currentSessionId, pendingHtmlPreviewState) {\n"
        "    val pending = pendingHtmlPreviewState ?: return@LaunchedEffect\n"
        "    val activeSessionId = ChatViewModelStore.resolveSessionId(viewModel.currentSessionId)\n"
        "    if (pending.sessionId != sessionId && pending.sessionId != activeSessionId) {\n"
        "        return@LaunchedEffect\n"
        "    }\n"
        "    com.rikkaminis.app.deeplink.DeepLinkCoordinator.consumePendingHtmlPreview()\n"
        "    val file = com.rikkaminis.app.sandbox.PRootKernel.resolvePathForSession(\n"
        "        activeSessionId,\n"
        '        "/var/minis${pending.resourcePath}",\n'
        "        appCtx,\n"
        "    )\n"
        "}\n"
    )
    nav = (
        "onOpenTerminal = {\n"
        "    navController.safeNavigate(\n"
        "        Routes.terminal(sessionId = ChatViewModelStore.resolveSessionId(sessionId)),\n"
        "    )\n"
        "},\n"
        "onOpenTerminalWithCommand = { command ->\n"
        "    navController.safeNavigate(\n"
        "        Routes.terminal(\n"
        "            initCommand = command,\n"
        "            sessionId = ChatViewModelStore.resolveSessionId(sessionId),\n"
        "        ),\n"
        "    )\n"
        "},\n"
        "onBrowseChatFiles = {\n"
        "    navController.safeNavigate(\n"
        "        Routes.chatFiles(ChatViewModelStore.resolveSessionId(sessionId)),\n"
        "    )\n"
        "},\n"
    )
    move = (
        "LaunchedEffect(currentSessionId) {\n"
        "    sessions = withContext(Dispatchers.IO) {\n"
        "        chatRepository.dao.listSessions().filter { it.id != currentSessionId }\n"
        "    }\n"
        "}\n"
    )
    return screen, nav, move


def selftest() -> int:
    import shutil

    failures = []

    def run_tree(screen: str, nav: str, move: str, store: str) -> "tuple[int, list[str]]":
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            for rel in (REL_SCREEN, REL_NAV, REL_MOVE, REL_STORE):
                f = tdp / rel
                f.parent.mkdir(parents=True, exist_ok=True)
            (tdp / REL_SCREEN).write_text(screen, encoding="utf-8")
            (tdp / REL_NAV).write_text(nav, encoding="utf-8")
            (tdp / REL_MOVE).write_text(move, encoding="utf-8")
            (tdp / REL_STORE).write_text(store, encoding="utf-8")
            rc = check(tdp)
            return rc, list(errors)

    screen, nav, move = _green_fixtures()

    # GREEN baseline
    rc, errs = run_tree(screen, nav, move, FIXTURE_STORE)
    if rc != 0:
        failures.append(f"GREEN fixture unexpectedly red: {errs}")
    else:
        print("selftest: GREEN fixture passes")

    # Mutations 1-8 mutate screen/nav/move
    for name, mut in MUTATIONS:
        s2, n2, m2 = mut(screen, nav, move)
        if name == "MUT-9 resolveKey widened to public":
            continue
        rc, errs = run_tree(s2, n2, m2, FIXTURE_STORE)
        if rc == 0:
            failures.append(f"{name}: gate stayed GREEN (no teeth)")
        else:
            print(f"selftest: {name} -> RED ({len(errs)} violation(s)) OK")

    # MUT-9: widened resolveKey must be caught by criterion 7
    bad_store = FIXTURE_STORE.replace(
        "private fun resolveKey", "fun resolveKey"
    )
    rc, errs = run_tree(screen, nav, move, bad_store)
    if rc == 0:
        failures.append("MUT-9 resolveKey widened to public: gate stayed GREEN (no teeth)")
    else:
        print(f"selftest: MUT-9 resolveKey widened to public -> RED ({len(errs)} violation(s)) OK")

    # MUT-10: resolveSessionId reimplemented without delegating (second lookup)
    dup_store = FIXTURE_STORE.replace(
        "fun resolveSessionId(sessionId: String): String =\n        resolveKey(sessionId)",
        "fun resolveSessionId(sessionId: String): String = aliases[sessionId] ?: sessionId",
    )
    rc, errs = run_tree(screen, nav, move, dup_store)
    if rc == 0:
        failures.append("MUT-10 resolveSessionId second implementation: gate stayed GREEN (no teeth)")
    else:
        print(f"selftest: MUT-10 non-delegating resolveSessionId -> RED ({len(errs)} violation(s)) OK")

    if failures:
        print("\nselftest FAILURES:")
        for f in failures:
            print(f"  - {f}")
        print("CHAT_SESSION_ID_BINDING_SELFTEST=FAIL")
        return 1
    print("CHAT_SESSION_ID_BINDING_SELFTEST=PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".", help="repo root")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return check(Path(args.root).resolve())


if __name__ == "__main__":
    sys.exit(main())

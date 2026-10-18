#!/usr/bin/env python3
"""Guard: MediaPlayerManager.releaseSession must release on EVERY path, and
the session-map mutations must not race the terminalize callbacks.

Why this exists (da1022-C): releaseSession wrapped stop() + release() in one
try. stop() throws IllegalStateException once the player has left the
playable states — which is exactly the state the onError terminalize runs
in — so the throw skipped release() and every error path leaked the native
MediaPlayer (codec + AudioTrack handles) until GC. Same-family fix already
existed in InlineMediaPlayer.onDispose (two separate try blocks). On top of
that, play() released the previous session OUTSIDE the lock the callbacks
take (double-release race) and stop() did release-then-remove unlocked (a
concurrent play() re-registering the same id could have its fresh session
removed while its player is still live = unreferenced leak).

Arms (judged on comment/string-stripped code; anchors are code shapes):
  1. releaseSession: `session.player.stop()` and `session.player.release()`
     sit in DIFFERENT try blocks (both guarded, never the same one).
  2. Session.state is `@Volatile` (cross-thread state writes from the
     binder/playback callbacks otherwise have no happens-before edge).
  3. play(): the pre-release `sessions[sessionId]?.let { ... releaseSession
     ... }` sits INSIDE a synchronized(lock) block.
  4. stop(): `sessions.remove(sessionId)` sits INSIDE a synchronized(lock)
     block (atomic with the release against a racing play()).
  5. (da1027-F) `ui/media/InlineMediaPlayer.kt`, when that file is present:
     every `AndroidView(...)` call must carry an `onRelease` that calls
     `stopPlayback()` (5a), the block that hoists the view into a state
     (`videoView = this`) must also clear that reference in its `onRelease`
     (5b), and every dismissal call site `onDismiss()` must sit in a block
     that calls `stopPlayback()` -- i.e. dismissing is a terminating path,
     not a bare `pause()` (5c). Anchors are fail-closed: 0 AndroidView
     blocks / 0 onDismiss() sites => RED. A mini-tree fixture that lacks the
     file is skipped (drift detector, not a target).
"""
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")

TARGET_REL = "src/android/app/src/main/java/com/rikkaminis/app/offload/MediaPlayerManager.kt"

INLINE_PLAYER_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/ui/media/InlineMediaPlayer.kt"
)

MARKER = "MEDIAPLAYER_RELEASE_SELFTEST=PASS"


def strip_comments_and_strings(text: str) -> str:
    """Blank out // comments, /* */ blocks and string literals (brace-safe)."""
    out = []
    i, n = 0, len(text)
    state = None
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if state is None:
            if two == "//":
                state = "line"; out.append("  "); i += 2; continue
            if two == "/*":
                state = "block"; out.append("  "); i += 2; continue
            if c == '"':
                state = "str"; out.append(" "); i += 1; continue
            out.append(c); i += 1; continue
        if state == "line":
            out.append("\n" if c == "\n" else " ")
            if c == "\n":
                state = None
            i += 1; continue
        if state == "block":
            if two == "*/":
                state = None; out.append("  "); i += 2; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        if state == "str":
            if c == "\\":
                out.append("  "); i += 2; continue
            if c == '"':
                state = None; out.append(" "); i += 1; continue
            out.append(" "); i += 1; continue
    return "".join(out)


def brace_span(text: str, open_idx: int) -> tuple:
    """(start, end_exclusive) of the block opened at open_idx."""
    depth, i, n = 1, open_idx + 1, len(text)
    while i < n and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return open_idx, i


def function_body(text: str, decl_pattern: str):
    """Stripped body (incl. braces) of the first function matching the
    declaration pattern, or '' when not found."""
    m = re.search(decl_pattern, text)
    if not m:
        return ""
    brace = text.find("{", m.end())
    if brace < 0:
        return ""
    start, end = brace_span(text, brace)
    return text[start:end]


def try_blocks(body: str):
    """All (start, end) spans of `try {` blocks inside body."""
    spans = []
    for m in re.finditer(r"\btry\s*\{", body):
        spans.append(brace_span(body, body.find("{", m.start())))
    return spans


def enclosing_try(body: str, idx: int):
    """Span of the smallest try block containing idx, or None."""
    best = None
    for s, e in try_blocks(body):
        if s < idx < e and (best is None or (e - s) < (best[1] - best[0])):
            best = (s, e)
    return best


def synchronized_lock_spans(body: str):
    """All (start, end) spans of `synchronized(lock) { ... }` in body."""
    spans = []
    for m in re.finditer(r"synchronized\s*\(\s*lock\s*\)\s*\{", body):
        spans.append(brace_span(body, body.find("{", m.start())))
    return spans


def inside_any(spans, idx: int) -> bool:
    return any(s < idx < e for s, e in spans)


def check_arms(clean: str):
    """Four arms on the comment-stripped file text. Returns problem list."""
    problems = []

    rel_body = function_body(clean, r"private\s+fun\s+releaseSession\s*\(")
    if not rel_body:
        return ["could not locate `private fun releaseSession(...)` in "
                + TARGET_REL + " -- the function may have been renamed; re-point this guard"]

    # arm 1: stop() and release() in different try blocks
    stop_call = re.search(r"session\.player\s*\.\s*stop\s*\(\s*\)", rel_body)
    release_call = re.search(r"session\.player\s*\.\s*release\s*\(\s*\)", rel_body)
    if not stop_call or not release_call:
        problems.append(
            "arm1: releaseSession no longer calls both session.player.stop() and "
            "session.player.release() -- re-point this guard (release() must be "
            "called on every path)")
    else:
        stop_try = enclosing_try(rel_body, stop_call.start())
        release_try = enclosing_try(rel_body, release_call.start())
        if stop_try is None:
            problems.append(
                "arm1: session.player.stop() is not inside a try block -- a throwing "
                "stop() would propagate out of releaseSession and skip release()")
        if release_try is None:
            problems.append(
                "arm1: session.player.release() is not inside a try block -- release "
                "must be guarded so terminalize paths never die mid-release")
        if stop_try is not None and release_try is not None:
            if stop_try == release_try:
                problems.append(
                    "arm1: stop() and release() share ONE try block -- stop() throws "
                    "IllegalStateException once the player left the playable states "
                    "(the onError terminalize), which skips release() and leaks the "
                    "native codec (InlineMediaPlayer.onDispose is the fixed shape)")

    # arm 2: @Volatile on Session.state
    if not re.search(r"@Volatile\s+var\s+state\s*:\s*PlayerState", clean):
        problems.append(
            "arm2: Session.state is not `@Volatile` -- callback threads write state "
            "(onCompletion/onError) while caller threads read it; without a "
            "happens-before edge a reader can hand a released player to pause()/stop()")

    # arm 3: play() pre-release under the lock
    play_body = function_body(clean, r"fun\s+play\s*\(")
    if not play_body:
        problems.append(
            "arm3: could not locate `fun play(...)` -- the pre-release may have moved; "
            "re-point this guard")
    else:
        m = re.search(r"sessions\s*\[\s*sessionId\s*\]\s*\?\.", play_body)
        pre_rel = play_body.find("releaseSession(existing)")
        if m and pre_rel >= 0:
            # the ?.let lambda ends before the first `}` after pre_rel
            if not inside_any(synchronized_lock_spans(play_body), m.start()):
                problems.append(
                    "arm3: play() reads/releases the previous session OUTSIDE "
                    "synchronized(lock) -- the terminalize callbacks release the same "
                    "instance under this lock; unlocked this is a double-release race")
        elif pre_rel < 0:
            problems.append(
                "arm3: play() no longer pre-releases the existing session via "
                "releaseSession(existing) -- if the shape changed, re-point this guard")

    # arm 4: stop() remove under the lock
    stop_body = function_body(clean, r"fun\s+stop\s*\(")
    if not stop_body:
        problems.append(
            "arm4: could not locate `fun stop(...)` -- re-point this guard")
    else:
        m = re.search(r"sessions\s*\.\s*remove\s*\(\s*sessionId\s*\)", stop_body)
        if not m:
            problems.append(
                "arm4: stop() no longer removes via sessions.remove(sessionId) -- "
                "if the shape changed, re-point this guard")
        elif not inside_any(synchronized_lock_spans(stop_body), m.start()):
            problems.append(
                "arm4: sessions.remove(sessionId) in stop() is OUTSIDE synchronized(lock) "
                "-- release-then-remove unlocked races a concurrent play() that "
                "re-registers the id: the fresh session gets dropped from the map "
                "while its player is still live (unreferenced leak)")

    return problems


def android_view_blocks(clean: str):
    """(start, end) spans of every `AndroidView(` argument list (paren-matched)."""
    spans = []
    n = len(clean)
    for m in re.finditer(r"\bAndroidView\s*\(", clean):
        open_idx = clean.find("(", m.start())
        depth, i = 1, open_idx + 1
        while i < n and depth:
            if clean[i] == "(":
                depth += 1
            elif clean[i] == ")":
                depth -= 1
            i += 1
        spans.append((open_idx, i))
    return spans


def enclosing_block(clean: str, idx: int):
    """Smallest brace span containing idx, or None. Strings/comments must
    already be stripped so brace_span's counting is exact."""
    best = None
    for pos, c in enumerate(clean):
        if c != "{":
            continue
        start, end = brace_span(clean, pos)
        if start < idx < end and (best is None or (end - start) < (best[1] - best[0])):
            best = (start, end)
    return best


def check_inline_player(clean):
    """Arm 5 (da1027-F). `clean` = comment/string-stripped InlineMediaPlayer.kt,
    or None when the file is absent (mini-tree fixture -> skip)."""
    problems = []
    if clean is None:
        return problems

    blocks = android_view_blocks(clean)
    if not blocks:
        problems.append(
            "arm5a: found 0 `AndroidView(` call sites in InlineMediaPlayer.kt -- the "
            "anchor is fail-closed; re-point this guard (both the inline player and "
            "the fullscreen player are hosted by AndroidView)")
    for start, end in blocks:
        block = clean[start:end]
        if not re.search(r"\bonRelease\s*=", block):
            problems.append(
                "arm5a: AndroidView at offset %d has no `onRelease =` -- the view is "
                "never released when it leaves composition, so the MediaPlayer and "
                "its hardware codec/AudioTrack handles stay registered until GC"
                % start)
        elif not re.search(r"\bstopPlayback\s*\(", block):
            problems.append(
                "arm5a: AndroidView at offset %d has an `onRelease` that never calls "
                "`stopPlayback()` -- release must stop the track, not just hide the "
                "controller window" % start)
        if re.search(r"\bvideoView\s*=\s*this\b", block):
            # The fullscreen player hoists the view into state that the position
            # poll and the play/pause button read back.
            if not re.search(r"\bvideoView\s*=\s*null\b", block):
                problems.append(
                    "arm5b: AndroidView at offset %d hoists `videoView = this` but its "
                    "`onRelease` never clears the reference (`videoView = null`) -- the "
                    "released view stays reachable and the position poll keeps touching "
                    "a stopped player" % start)

    sites = [m.start() for m in re.finditer(r"\bonDismiss\s*\(\s*\)", clean)]
    if not sites:
        problems.append(
            "arm5c: found 0 `onDismiss()` call sites in InlineMediaPlayer.kt -- the "
            "anchor is fail-closed; re-point this guard (every dismissal branch must "
            "terminate the player)")
    for idx in sites:
        span = enclosing_block(clean, idx)
        if span is None or not re.search(r"\bstopPlayback\s*\(", clean[span[0]:span[1]]):
            problems.append(
                "arm5c: the block containing `onDismiss()` at offset %d does not call "
                "`stopPlayback()` -- dismissing the fullscreen player is a terminating "
                "path, not a pause (the back-key branch reaches the same release only "
                "through onRelease, so a bare pause() there leaks the native player "
                "and lets audio outlive the dialog)" % idx)
    return problems


def run_selftest() -> bool:
    COMPLIANT = '''
object MediaPlayerManager {
    private data class Session(
        val player: MediaPlayer,
        val filePath: String,
        val mediaType: String,
        @Volatile var state: PlayerState = PlayerState.IDLE,
    )
    fun play(sessionId: String, filePath: String): String {
        synchronized(lock) {
            sessions[sessionId]?.let { existing ->
                releaseSession(existing)
            }
        }
        return "x"
    }
    fun stop(sessionId: String): String {
        val removed = synchronized(lock) { sessions.remove(sessionId) }
            ?: return "Error: no session"
        releaseSession(removed)
        return "x"
    }
    private fun releaseSession(session: Session) {
        try {
            if (session.state == PlayerState.PLAYING) {
                session.player.stop()
            }
        } catch (e: Exception) { }
        try {
            session.player.release()
            session.state = PlayerState.STOPPED
        } catch (e: Exception) { }
    }
}
'''
    M1 = COMPLIANT.replace(
        """        try {
            if (session.state == PlayerState.PLAYING) {
                session.player.stop()
            }
        } catch (e: Exception) { }
        try {
            session.player.release()
            session.state = PlayerState.STOPPED
        } catch (e: Exception) { }""",
        """        try {
            if (session.state == PlayerState.PLAYING) {
                session.player.stop()
            }
            session.player.release()
            session.state = PlayerState.STOPPED
        } catch (e: Exception) { }""")
    M2 = COMPLIANT.replace("@Volatile var state", "var state")
    M3 = COMPLIANT.replace(
        """        synchronized(lock) {
            sessions[sessionId]?.let { existing ->
                releaseSession(existing)
            }
        }""",
        """        sessions[sessionId]?.let { existing ->
            releaseSession(existing)
        }""")
    M4 = COMPLIANT.replace(
        "val removed = synchronized(lock) { sessions.remove(sessionId) }\n"
        "            ?: return \"Error: no session\"",
        "val session = sessions[sessionId] ?: return \"Error: no session\"\n"
        "        releaseSession(session)\n"
        "        sessions.remove(sessionId)")

    cases = [
        ("compliant", COMPLIANT, 0),
        ("M1 stop+release same try", M1, 1),
        ("M2 state not volatile", M2, 1),
        ("M3 play pre-release unlocked", M3, 1),
        ("M4 stop remove unlocked", M4, 1),
    ]
    ok = True
    for name, src, want_red in cases:
        problems = check_arms(strip_comments_and_strings(src))
        red = len(problems) > 0
        status = "RED" if red else "GREEN"
        expected = "RED" if want_red else "GREEN"
        mark = "ok" if red == bool(want_red) else "FAIL"
        if mark == "FAIL":
            ok = False
        print(f"  selftest {name}: {status} (expect {expected}) {mark}")
        for p in problems:
            print(f"      - {p}")

    # shape edge: releaseSession gone -> explicit red, not a silent pass
    problems = check_arms(strip_comments_and_strings("object MediaPlayerManager {}"))
    if not problems:
        print("  selftest no-releaseSession: expected explicit red, got GREEN -- FAIL")
        ok = False
    else:
        print("  selftest no-releaseSession: RED (expect RED) ok")

    ok = run_inline_player_selftest() and ok
    return ok


def run_inline_player_selftest() -> bool:
    """Arm 5 fixtures (da1027-F). Each dirty case must go red AND the expected
    sub-arm must be the one that fires."""
    COMPLIANT = '''
@Composable
fun InlineVideoPlayer(filePath: String, modifier: Modifier = Modifier) {
    Surface(modifier = modifier) {
        androidx.compose.ui.viewinterop.AndroidView(
            factory = { ctx ->
                android.widget.VideoView(ctx).apply {
                    setVideoURI(Uri.parse(filePath))
                }
            },
            onRelease = { view ->
                mediaController.hide()
                view.stopPlayback()
            },
            modifier = Modifier.fillMaxWidth(),
        )
    }
}

@Composable
fun MinisFullscreenVideoPlayer(file: File, onDismiss: () -> Unit) {
    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(),
    ) {
        FullscreenVideoContent(file = file, onDismiss = onDismiss)
    }
}

@Composable
private fun FullscreenVideoContent(file: File, onDismiss: () -> Unit) {
    var videoView by remember { mutableStateOf<android.widget.VideoView?>(null) }
    Box(
        modifier = Modifier.pointerInput(Unit) {
            detectVerticalDragGestures(
                onDragEnd = {
                    if (dragOffsetPx > dismissThresholdPx * 0.5f) {
                        try { videoView?.stopPlayback() } catch (_: Throwable) {}
                        onDismiss()
                    } else {
                        dragOffsetPx = 0f
                    }
                },
            )
        },
    ) {
        androidx.compose.ui.viewinterop.AndroidView(
            factory = { ctx ->
                android.widget.VideoView(ctx).apply {
                    setVideoURI(Uri.fromFile(file))
                    videoView = this
                }
            },
            onRelease = { view ->
                try { view.stopPlayback() } catch (_: Throwable) {}
                if (videoView === view) videoView = null
            },
            modifier = Modifier.fillMaxSize(),
        )
        CircleControlButton(
            icon = Icons.Default.Close,
            onClick = {
                try { videoView?.stopPlayback() } catch (_: Throwable) {}
                onDismiss()
            },
        )
    }
}
'''
    FULLSCREEN_ONRELEASE = '''            onRelease = { view ->
                try { view.stopPlayback() } catch (_: Throwable) {}
                if (videoView === view) videoView = null
            },
'''
    GESTURE_STOP = ('                        try { videoView?.stopPlayback() } '
                    'catch (_: Throwable) {}\n                        onDismiss()')
    CLOSE_STOP = ('                try { videoView?.stopPlayback() } '
                  'catch (_: Throwable) {}\n                onDismiss()')

    M5 = COMPLIANT.replace(FULLSCREEN_ONRELEASE, "")
    M6 = COMPLIANT.replace(
        "                if (videoView === view) videoView = null\n",
        "                view.setBackgroundColor(0)\n")
    M7 = COMPLIANT.replace(GESTURE_STOP, GESTURE_STOP.replace("stopPlayback", "pause"))
    M8 = COMPLIANT.replace(CLOSE_STOP, CLOSE_STOP.replace("stopPlayback", "pause"))
    M9 = COMPLIANT.replace("androidx.compose.ui.viewinterop.AndroidView(", "VideoSurface(")
    M10 = COMPLIANT.replace("            onDismiss()\n", "")
    M11 = COMPLIANT.replace("view ->", "view ->").replace(
        "                view.stopPlayback()\n", "")

    cases = [
        ("arm5 compliant", COMPLIANT, None),
        ("arm5a M5 no onRelease on fullscreen block", M5, "arm5a"),
        ("arm5b M6 onRelease does not clear the hoisted ref", M6, "arm5b"),
        ("arm5c M7 gesture back to pause()", M7, "arm5c"),
        ("arm5c M8 close button back to pause()", M8, "arm5c"),
        ("arm5a M9 no AndroidView at all (fail-closed anchor)", M9, "arm5a"),
        ("arm5c M10 no onDismiss() site (fail-closed anchor)", M10, "arm5c"),
        ("arm5a M11 inline onRelease stops releasing", M11, "arm5a"),
    ]
    ok = True
    for name, src, want_key in cases:
        problems = check_inline_player(strip_comments_and_strings(src))
        red = len(problems) > 0
        if want_key is None:
            mark = "ok" if not red else "FAIL"
            print(f"  selftest {name}: {'RED' if red else 'GREEN'} "
                  f"(expect GREEN) {mark}")
            if red:
                ok = False
        else:
            fired = any(p.startswith(want_key) for p in problems)
            mark = "ok" if (red and fired) else "FAIL"
            print(f"  selftest {name}: {'RED' if red else 'GREEN'} "
                  f"(expect RED via {want_key}) {mark}")
            if not (red and fired):
                ok = False
        for p in problems:
            print(f"      - {p}")

    # mini-tree fixture: InlineMediaPlayer.kt absent -> judgement skipped
    if check_inline_player(None):
        print("  selftest arm5 mini tree (no InlineMediaPlayer.kt): expected "
              "GREEN (skipped), got RED -- FAIL")
        ok = False
    else:
        print("  selftest arm5 mini tree (no InlineMediaPlayer.kt): GREEN (skipped) ok")
    return ok


def main() -> int:
    if "--selftest" in sys.argv:
        print("mediaplayer_release_guard selftest:")
        if not run_selftest():
            print("FAIL: selftest")
            return 1
        print(MARKER)
        return 0

    target = ROOT / TARGET_REL
    if not target.exists():
        print(f"FAIL: missing {TARGET_REL}")
        return 1
    problems = check_arms(strip_comments_and_strings(
        target.read_text(encoding="utf-8", errors="replace")))
    inline = ROOT / INLINE_PLAYER_REL
    problems += check_inline_player(
        strip_comments_and_strings(
            inline.read_text(encoding="utf-8", errors="replace"))
        if inline.exists() else None)
    if problems:
        print("FAIL: MediaPlayerManager release/lock gaps")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK: MediaPlayerManager releases on every path (stop/release in "
          "separate try blocks), Session.state is @Volatile, and the "
          "play()/stop() session-map mutations sit inside synchronized(lock); "
          "every AndroidView in InlineMediaPlayer.kt releases via "
          "onRelease/stopPlayback and every onDismiss() branch terminates the "
          "player")
    return 0


if __name__ == "__main__":
    sys.exit(main())

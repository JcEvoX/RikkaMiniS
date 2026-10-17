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
"""
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")

TARGET_REL = "src/android/app/src/main/java/com/rikkaminis/app/offload/MediaPlayerManager.kt"

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
    if problems:
        print("FAIL: MediaPlayerManager release/lock gaps")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK: MediaPlayerManager releases on every path (stop/release in "
          "separate try blocks), Session.state is @Volatile, and the "
          "play()/stop() session-map mutations sit inside synchronized(lock)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

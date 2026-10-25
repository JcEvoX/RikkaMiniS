#!/usr/bin/env python3
"""
Gate: FIX-G (deep-audit-1021 ISSUE-08) — SpeechRecognitionManager active-engine
binding.

Root cause (verified on the baseline tree): `startRecording` resolves
`currentEngine()` and never records the started instance, while
`stopRecording` / `cancelRecording` / `selectLocale` re-resolve
`currentEngine()`. Two mid-session paths redirect them to a different,
never-started engine and leak the real one's AudioRecord / SpeechRecognizer:

  1. an explicit `selectEngine(newId)` (the only mid-session writer of
     `_selectedEngineId`; the report's "refreshAvailability auto-degrade"
     path does not exist — `refreshAvailability` only writes `_isAvailable`);
  2. the internal fallback inside `currentEngine()` itself: when the selected
     engine degrades it silently returns the first available engine WITHOUT
     writing `_selectedEngineId`.

The fix binds `@Volatile activeEngine` to the started instance. Volatile is
sufficient: every site is a single write or a single read, no read-modify-write.

Usage: python3 scripts/scan/speech_active_engine_check.py [repo_root]
       python3 scripts/scan/speech_active_engine_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import os
import re
import sys

SRM_REL = "src/android/app/src/main/java/com/rikkaminis/app/speech/SpeechRecognitionManager.kt"

def strip_comments(src):
    """Blank comments and string/char literal CONTENTS, offsets preserved.

    Literal-aware: strings are scanned BEFORE comments, so a `"https://…"` or a
    `"video/*"` literal can neither be cut as a comment nor open a block comment
    that would blank the rest of the file (the failure mode that once hid 67
    lines of a handler from a sibling gate).
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            if src.startswith('"""', i):
                j = i + 3
                while j < n and not src.startswith('"""', j):
                    if src[j] != "\n":
                        out[j] = " "
                    j += 1
                i = j + 3
                continue
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    out[j] = " "
                    if j + 1 < n and src[j + 1] != "\n":
                        out[j + 1] = " "
                    j += 2
                    continue
                if src[j] == '"':
                    break
                if src[j] != "\n":
                    out[j] = " "
                j += 1
            i = j + 1
            continue
        if c == "'":
            m = re.match(r"'(?:\\.|[^'\\\n])'", src[i:])
            if m:
                for k in range(i, i + m.end()):
                    out[k] = " "
                i += m.end()
                continue
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = i
            while j < n and src[j] != "\n":
                out[j] = " "
                j += 1
            i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = i
            while j < n and not (src[j] == "*" and j + 1 < n and src[j + 1] == "/"):
                if src[j] != "\n":
                    out[j] = " "
                j += 1
            for k in range(j, min(j + 2, n)):
                if src[k] != "\n":
                    out[k] = " "
            i = j + 2
            continue
        i += 1
    return "".join(out)

def balanced_block(src, anchor):
    i = src.find(anchor)
    if i < 0:
        return None
    j = src.find("{", i)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    return None

ELVIS_STOP = "(activeEngine ?: currentEngine())?.stop()"
ELVIS_CANCEL = "(activeEngine ?: currentEngine())?.cancel()"


def cancel_before_clear(name, body):
    """Order check: a function that tears down an in-flight session must cancel
    BEFORE it clears the binding — `activeEngine = null` first makes the cancel
    land on currentEngine() (or on a null), never on the engine that owns the mic
    (wide-1024 witnesses arm2/arm3/arm4)."""
    cancel_at = body.find(".cancel()")
    clear_at = body.find("activeEngine = null")
    if cancel_at < 0 or clear_at < 0:
        return None
    if clear_at < cancel_at:
        return ("SpeechRecognitionManager.kt: %s clears activeEngine BEFORE cancelling — "
                "the cancel() call then lands on currentEngine() (or on a null) instead of "
                "the engine that owns the mic; cancel first, clear after" % name)
    return None

def check_manager(src):
    v = []
    code = strip_comments(src)

    # arm 0 — startRecording must BIND the started instance (wide-1024 W1:
    # deleting `activeEngine = engine` left all five consumer arms green while
    # the latch stayed null forever)
    start_body = balanced_block(code, "fun startRecording")
    if start_body is None:
        v.append("SpeechRecognitionManager.kt: startRecording declaration not found "
                 "(gate anchors moved — deliberate edit required)")
        return v
    binds = [(m.start(), m.group(1))
             for m in re.finditer(r"activeEngine\s*=\s*([A-Za-z_]\w*)", start_body)
             if m.group(1) != "null"]
    start_calls = [(m.start(), m.group(1))
                   for m in re.finditer(r"([A-Za-z_]\w*)\.start\s*\(", start_body)]
    if not start_calls:
        v.append("SpeechRecognitionManager.kt: startRecording no longer starts an engine "
                 "(no `<engine>.start(…)` call — the mic never opens)")
    elif not any(x == var and p < sp for p, x in binds for sp, var in start_calls):
        v.append("SpeechRecognitionManager.kt: startRecording does not bind activeEngine to "
                 "the engine it starts (`activeEngine = <engine>` before `<engine>.start(…)`) — "
                 "the latch stays null forever and every consumer degrades to currentEngine() "
                 "(da1021 ISSUE-08: the real engine leaks on a mid-session switch)")

    # arm 1 — stopRecording must target the bound instance
    stop = balanced_block(code, "fun stopRecording")
    if stop is None:
        v.append("SpeechRecognitionManager.kt: stopRecording declaration not found "
                 "(gate anchors moved — deliberate edit required)")
        return v
    if ELVIS_STOP not in stop:
        v.append("SpeechRecognitionManager.kt: stopRecording no longer targets "
                 "activeEngine (elvis form lost) — a mid-session selectEngine or "
                 "currentEngine() internal fallback would redirect stop() to a "
                 "never-started engine and leak the real one (da1021 ISSUE-08)")
    else:
        # the bare currentEngine()?.stop() spelling must not come back anywhere
        if code.count("currentEngine()?.stop()") > 0:
            v.append("SpeechRecognitionManager.kt: a bare currentEngine()?.stop() "
                     "spelling reappeared outside the elvis form")

    # arm 2 — cancelRecording must target the bound instance and clear it
    cancel = balanced_block(code, "fun cancelRecording")
    if cancel is None:
        v.append("SpeechRecognitionManager.kt: cancelRecording declaration not found")
        return v
    if ELVIS_CANCEL not in cancel:
        v.append("SpeechRecognitionManager.kt: cancelRecording no longer targets "
                 "activeEngine (elvis form lost)")
    if "activeEngine = null" not in cancel:
        v.append("SpeechRecognitionManager.kt: cancelRecording no longer clears "
                 "activeEngine (stale binding survives into the next session)")
    w = cancel_before_clear("cancelRecording", cancel)
    if w:
        v.append(w)

    # arm 3 — selectEngine must tear down an in-flight session
    sel = balanced_block(code, "fun selectEngine")
    if sel is None:
        v.append("SpeechRecognitionManager.kt: selectEngine declaration not found")
        return v
    if "_state.value != RecognitionState.IDLE" not in sel:
        v.append("SpeechRecognitionManager.kt: selectEngine lost the mid-recording "
                 "state check (_state.value != RecognitionState.IDLE)")
    if ".cancel()" not in sel:
        v.append("SpeechRecognitionManager.kt: selectEngine no longer cancels the "
                 "in-flight engine (mic handle leak on mid-recording switch)")
    if "activeEngine = null" not in sel:
        v.append("SpeechRecognitionManager.kt: selectEngine no longer clears "
                 "activeEngine")
    if "_state.value = RecognitionState.IDLE" not in sel:
        v.append("SpeechRecognitionManager.kt: selectEngine no longer resets the "
                 "state machine to IDLE (FINISHING/RECORDING wedge)")
    w = cancel_before_clear("selectEngine", sel)
    if w:
        v.append(w)

    # arm 3b — selectLocale restarts mid-recording too: same cancel-then-clear
    # order (wide-1024 W2 also_blinds arm4 — same family, same shared rule)
    loc = balanced_block(code, "fun selectLocale")
    if loc is None:
        v.append("SpeechRecognitionManager.kt: selectLocale declaration not found")
    elif ".cancel()" in loc and "activeEngine = null" in loc:
        w = cancel_before_clear("selectLocale", loc)
        if w:
            v.append(w)

    # arm 4 — terminal callbacks must clear the binding (anti-悬留)
    fin = balanced_block(code, "override fun onFinal")
    err = balanced_block(code, "override fun onError")
    if fin is None:
        v.append("SpeechRecognitionManager.kt: onFinal listener not found")
    elif "activeEngine = null" not in fin:
        v.append("SpeechRecognitionManager.kt: onFinal no longer clears activeEngine")
    if err is None:
        v.append("SpeechRecognitionManager.kt: onError listener not found")
    elif "activeEngine = null" not in err:
        v.append("SpeechRecognitionManager.kt: onError no longer clears activeEngine")
    n_clears = code.count("activeEngine = null")
    if n_clears < 4:
        v.append("SpeechRecognitionManager.kt: only %d activeEngine = null sites "
                 "(need >= 4: final / error / cancel / selectEngine)" % n_clears)

    # arm 5 — the binding must stay volatile (cross-thread: engine callbacks +
    # main handler); declaration shape pinned so a refactor cannot drop it
    m = re.search(r"@Volatile\s+private var activeEngine:\s*SpeechRecognitionEngine\? = null", code)
    if m is None:
        v.append("SpeechRecognitionManager.kt: activeEngine must be declared "
                 "@Volatile private var activeEngine: SpeechRecognitionEngine? = null "
                 "(engine callbacks and UI calls run on different threads)")

    return v

def read(root, rel):
    with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
        return fh.read()

def run(root):
    if not os.path.isdir(root):
        print("speech_active_engine_check: not a directory: %s" % root)
        return 2
    try:
        src = read(root, SRM_REL)
    except OSError as exc:
        print("speech_active_engine_check: cannot read sources: %s" % exc)
        return 2
    violations = check_manager(src)
    if violations:
        print("FAIL speech_active_engine_check")
        for x in violations:
            print("  " + x)
        return 1
    print("PASS speech_active_engine_check (activeEngine binding wired: "
          "stop/cancel elvis forms + selectEngine teardown + terminal clears "
          "+ volatile)")
    return 0

# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

GOOD = '''
class SpeechRecognitionManager {
    fun selectEngine(id: String) {
        if (_selectedEngineId.value == id) return
        if (_state.value != RecognitionState.IDLE) {
            activeEngine?.cancel()
            activeEngine = null
            _state.value = RecognitionState.IDLE
        }
        _selectedEngineId.value = id
        prefs.edit().putString(KEY_ENGINE_ID, id).apply()
        refreshAvailability()
    }

    @Volatile
    private var activeEngine: SpeechRecognitionEngine? = null

    fun selectLocale(locale: Locale) {
        _locale.value = locale
        if (_state.value == RecognitionState.RECORDING || _state.value == RecognitionState.STARTING) {
            val cb = activeCallbacks
            if (cb != null) {
                (activeEngine ?: currentEngine())?.cancel()
                activeEngine = null
                _state.value = RecognitionState.IDLE
                startRecording(cb.first, cb.second)
            }
        }
    }

    fun startRecording(a: Int, b: Int) {
        val engine = currentEngine()
        activeCallbacks = a to b
        activeEngine = engine
        try {
            engine.start(locale, listener)
        } catch (e: Throwable) {
            activeEngine = null
            _state.value = RecognitionState.IDLE
        }
    }

    fun stopRecording() {
        if (_state.value != RecognitionState.RECORDING) return
        _state.value = RecognitionState.FINISHING
        (activeEngine ?: currentEngine())?.stop()
    }

    fun cancelRecording() {
        (activeEngine ?: currentEngine())?.cancel()
        activeEngine = null
        _state.value = RecognitionState.IDLE
    }

    private val listener = object : Listener {
        override fun onFinal(text: String) {
            activeEngine = null
            _state.value = RecognitionState.IDLE
        }
        override fun onError(error: Int, message: String?) {
            activeEngine = null
            _state.value = RecognitionState.IDLE
        }
    }
}
'''

# M1: stopRecording back to the bare currentEngine()?.stop() spelling
BAD_STOP = GOOD.replace(
    "        _state.value = RecognitionState.FINISHING\n        (activeEngine ?: currentEngine())?.stop()",
    "        _state.value = RecognitionState.FINISHING\n        currentEngine()?.stop()")

# M2: selectEngine without the mid-recording teardown
BAD_SELECT = GOOD.replace(
    """        if (_state.value != RecognitionState.IDLE) {
            activeEngine?.cancel()
            activeEngine = null
            _state.value = RecognitionState.IDLE
        }
        _selectedEngineId.value = id""",
    "        _selectedEngineId.value = id")

# M3: terminal callbacks no longer clear the binding (stale activeEngine)
BAD_CLEAR = GOOD.replace(
    """        override fun onFinal(text: String) {
            activeEngine = null
            _state.value = RecognitionState.IDLE
        }
        override fun onError(error: Int, message: String?) {
            activeEngine = null
            _state.value = RecognitionState.IDLE
        }""",
    """        override fun onFinal(text: String) {
            _state.value = RecognitionState.IDLE
        }
        override fun onError(error: Int, message: String?) {
            _state.value = RecognitionState.IDLE
        }""")

# M4: binding dropped back to a plain field (no @Volatile)
BAD_VOLATILE = GOOD.replace("@Volatile\n    private var activeEngine", "private var activeEngine")

# ── wide-1024 witnesses ─────────────────────────────────────────────────────
# W1: the write half of the latch deleted — every consumer arm stays "wired"
# while `activeEngine` is permanently null.
BAD_NO_BIND = GOOD.replace(
    """        activeCallbacks = a to b
        activeEngine = engine""",
    """        activeCallbacks = a to b""")

# W2: cancelRecording clears the binding BEFORE cancelling it.
BAD_CANCEL_ORDER = GOOD.replace(
    """    fun cancelRecording() {
        (activeEngine ?: currentEngine())?.cancel()
        activeEngine = null""",
    """    fun cancelRecording() {
        activeEngine = null
        (activeEngine ?: currentEngine())?.cancel()""")

# W3: selectEngine clears the binding BEFORE the teardown cancel.
BAD_SELECT_ORDER = GOOD.replace(
    """        if (_state.value != RecognitionState.IDLE) {
            activeEngine?.cancel()
            activeEngine = null
            _state.value = RecognitionState.IDLE
        }
        _selectedEngineId.value = id""",
    """        if (_state.value != RecognitionState.IDLE) {
            activeEngine = null
            activeEngine?.cancel()
            _state.value = RecognitionState.IDLE
        }
        _selectedEngineId.value = id""")

def selftest():
    failures = []
    if check_manager(GOOD):
        failures.append("GOOD fixture flagged: %s" % check_manager(GOOD))
    for name, bad, expect in [
        ("M1 bare stopRecording", BAD_STOP, "stopRecording"),
        ("M2 selectEngine teardown removed", BAD_SELECT, "selectEngine"),
        ("M3 terminal clears removed", BAD_CLEAR, "onFinal"),
        ("M4 volatile dropped", BAD_VOLATILE, "@Volatile"),
        ("W1 startRecording binding deleted", BAD_NO_BIND, "does not bind activeEngine"),
        ("W2 cancelRecording clears before cancel", BAD_CANCEL_ORDER, "cancelRecording clears"),
        ("W3 selectEngine clears before cancel", BAD_SELECT_ORDER, "selectEngine clears"),
    ]:
        v = check_manager(bad)
        if not v:
            failures.append("%s: gate stayed GREEN on a violation (expected %s)" % (name, expect))
        elif expect not in v[0]:
            failures.append("%s: first violation is not the expected one (%s): %s"
                            % (name, expect, v[0]))
    if failures:
        print("FAIL speech_active_engine_check --selftest")
        for x in failures:
            print("  " + x)
        return 1
    print("SPEECH_ACTIVE_ENGINE_SELFTEST=PASS (8 fixtures: 1 clean + 7 violations)")
    return 0

if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(run(sys.argv[1]))

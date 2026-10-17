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
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for line in src.split("\n"):
        cut = None
        in_str = False
        i = 0
        while i < len(line):
            ch = line[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
            i += 1
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)

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

def check_manager(src):
    v = []
    code = strip_comments(src)

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

def selftest():
    failures = []
    if check_manager(GOOD):
        failures.append("GOOD fixture flagged: %s" % check_manager(GOOD))
    for name, bad, expect in [
        ("M1 bare stopRecording", BAD_STOP, "stopRecording"),
        ("M2 selectEngine teardown removed", BAD_SELECT, "selectEngine"),
        ("M3 terminal clears removed", BAD_CLEAR, "onFinal"),
        ("M4 volatile dropped", BAD_VOLATILE, "@Volatile"),
    ]:
        v = check_manager(bad)
        if not v:
            failures.append("%s: gate stayed GREEN on a violation (expected %s)" % (name, expect))
        elif expect == "stopRecording" and "elvis" not in v[0]:
            failures.append("%s: first violation is not the stopRecording one: %s" % (name, v[0]))
        elif expect == "selectEngine" and "selectEngine" not in v[0]:
            failures.append("%s: first violation is not the selectEngine one: %s" % (name, v[0]))
        elif expect == "onFinal" and "onFinal" not in v[0]:
            failures.append("%s: first violation is not the onFinal one: %s" % (name, v[0]))
        elif expect == "@Volatile" and "@Volatile" not in v[0]:
            failures.append("%s: first violation is not the volatile one: %s" % (name, v[0]))
    if failures:
        print("FAIL speech_active_engine_check --selftest")
        for x in failures:
            print("  " + x)
        return 1
    print("SPEECH_ACTIVE_ENGINE_SELFTEST=PASS (5 fixtures: 1 clean + 4 violations)")
    return 0

if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(run(sys.argv[1]))

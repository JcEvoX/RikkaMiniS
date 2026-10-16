#!/usr/bin/env python3
"""[da1016-E] Gate: android-speak language channel + sanitizer wiring.

Mechanism the gate protects (deep-audit-1016c ISSUE-07, P1):

  A. **Language override**: the offload handler used to call
     `tts.setLanguage(tag)` for `--voice` and then `tts.speak(text)` — but
     `TextToSpeechManager.speak` unconditionally ran
     `autoDetectAndSetLanguage(text)` (a zh/en binary guess), so every
     explicit tag was silently overwritten, and the JSON body echoed the
     *requested* tag (`voiceTag ?: "auto"`) as if it had been chosen.
     After the fix the language is resolved in exactly one place:
     `speak()` consults `TtsLanguagePolicy.resolveTargetLocale(explicit,
     text)`; the handler only passes the tag through and reports
     `currentLanguageTag()` (the effective value).

  B. **Sanitizer wiring**: `VoiceTextSanitizer` (spoken-text cleanup:
     markdown out, URL→localized phrase) existed with tests but had ZERO
     production call sites. `speak()` must sanitize BEFORE the engine call
     and treat an empty result as "nothing to speak" (never fall back to
     reading the raw text aloud).

  C. **Streaming preservation**: `speakQueued()` (sentence fragments from
     the live transcription stream) has no explicit language to honor and
     must keep `autoDetectAndSetLanguage` — per-chunk sanitize would break
     cross-chunk link rewrites, so the streaming path stays raw (declared
     residual, not a violation).

Run from the repo root:
`python3 scripts/scan/tts_voice_and_sanitizer_check.py`
exit 0 = PASS, exit 1 = FAIL (violations printed as path[:line]).

`--selftest` builds violating + compliant fixtures and asserts the gate's
own teeth (a gate that cannot fail is worse than no gate).
"""
import argparse
import os
import re
import shutil
import sys
import tempfile

MAIN_ROOT = os.path.join("src", "android", "app", "src", "main", "java")
MANAGER_REL = os.path.join(
    MAIN_ROOT, "com", "rikkaminis", "app", "speech", "TextToSpeechManager.kt"
)
HANDLER_REL = os.path.join(
    MAIN_ROOT, "com", "rikkaminis", "app", "sandbox", "offload", "SpeakOffloadHandler.kt"
)

# Explicit exemption marker for rule B (rare callers that must voice raw text).
SANITIZER_EXEMPT_RE = re.compile(r"tts-sanitizer-exempt:\s*\S.*")


def read(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def function_body(src, signature):
    """Brace-balanced body of the member whose declaration contains `signature`."""
    start = src.find(signature)
    if start < 0:
        return None
    open_curly = src.find("{", start)
    if open_curly < 0:
        return None
    depth = 0
    i = open_curly
    n = len(src)
    while i < n:
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[open_curly : i + 1]
        i += 1
    return None


def run(root, verbose=True):
    failures = []
    manager = read(root, MANAGER_REL)
    handler = read(root, HANDLER_REL)

    if manager is None:
        failures.append(
            "%s: [tts-files] TextToSpeechManager.kt not found" % MANAGER_REL
        )
    else:
        speak = function_body(manager, "fun speak(")
        if speak is None:
            failures.append(
                "%s: [tts-files] TextToSpeechManager.speak() not found" % MANAGER_REL
            )
        else:
            exempt = bool(SANITIZER_EXEMPT_RE.search(speak))
            # ── Rule A: single-point language resolution, explicit honored ──
            if "TtsLanguagePolicy.resolveTargetLocale(explicitLanguage" not in speak:
                failures.append(
                    "%s: [tts-voice] speak() must resolve the utterance language "
                    "through TtsLanguagePolicy.resolveTargetLocale(explicitLanguage, ...) "
                    "- the --voice tag must not be overwritten by auto-detection"
                    % MANAGER_REL
                )
            if "autoDetectAndSetLanguage(" in speak:
                failures.append(
                    "%s: [tts-voice] speak() must not call autoDetectAndSetLanguage "
                    "(that unconditional call was the override bug)"
                    % MANAGER_REL
                )
            # ── Rule B: sanitize before the engine call, empty result bails ──
            if not exempt:
                sanitize_at = speak.find("VoiceTextSanitizer.sanitize(")
                engine_at = speak.find("tts?.speak(")
                if sanitize_at < 0 or engine_at < 0 or sanitize_at > engine_at:
                    failures.append(
                        "%s: [tts-sanitizer] speak() must call "
                        "VoiceTextSanitizer.sanitize() BEFORE tts?.speak() "
                        "(or declare 'tts-sanitizer-exempt: <reason>')"
                        % MANAGER_REL
                    )
                if "if (speakable.isBlank()) return false" not in speak:
                    failures.append(
                        "%s: [tts-sanitizer] speak() must bail on an empty "
                        "sanitize result instead of voicing the raw text"
                        % MANAGER_REL
                    )
        # ── Rule C: streaming path keeps auto-detection ──
        queued = function_body(manager, "fun speakQueued(")
        if queued is not None and "autoDetectAndSetLanguage(" not in queued:
            failures.append(
                "%s: [tts-stream] speakQueued() must keep autoDetectAndSetLanguage "
                "(streaming fragments have no explicit language; per-chunk sanitize "
                "would break cross-chunk link rewrites)" % MANAGER_REL
            )

    if handler is None:
        failures.append(
            "%s: [tts-files] SpeakOffloadHandler.kt not found" % HANDLER_REL
        )
    else:
        # ── Rule A, handler side: pass the tag, never set language itself ──
        if "tts.setLanguage(" in handler:
            failures.append(
                "%s: [tts-voice] SpeakOffloadHandler must not call tts.setLanguage "
                "- the language is resolved in one place (speak); the old "
                "set-then-overwrite pair was the bug" % HANDLER_REL
            )
        if "tts.speak(text, explicitLanguage = voiceTag)" not in handler:
            failures.append(
                "%s: [tts-voice] the --voice path must pass the tag into speak "
                '("tts.speak(text, explicitLanguage = voiceTag)"), not a bare '
                "speak(text) that self-overwrites" % HANDLER_REL
            )
        # ── Rule A, honest result: report the effective language ──
        if 'voiceTag ?: "auto"' in handler:
            failures.append(
                "%s: [tts-voice] the voice field must not echo the requested "
                'tag (found `voiceTag ?: "auto"`)' % HANDLER_REL
            )
        if '.put("voice", effectiveVoice)' not in handler:
            failures.append(
                "%s: [tts-voice] the voice field must report the effective "
                "language (currentLanguageTag()), not the input parameter"
                % HANDLER_REL
            )

    if verbose:
        for line in failures:
            print(line)
    return failures


# ── selftest ────────────────────────────────────────────────────────────────

PRE_FIX_MANAGER = '''
class TextToSpeechManager {
    fun speak(text: String) {
        autoDetectAndSetLanguage(text)
        val params = Bundle()
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, params, utteranceId)
        return true
    }

    fun speakQueued(text: String) {
        autoDetectAndSetLanguage(text)
        tts?.speak(text, TextToSpeech.QUEUE_ADD, params, utteranceId)
    }
}
'''

COMPLIANT_MANAGER = '''
class TextToSpeechManager {
    fun speak(text: String, explicitLanguage: String? = null): Boolean {
        val speakable = VoiceTextSanitizer.sanitize(text, TtsSpokenPhrases.from(appContext))
        if (speakable.isBlank()) return false
        applyLanguage(TtsLanguagePolicy.resolveTargetLocale(explicitLanguage, speakable))
        tts?.speak(speakable, TextToSpeech.QUEUE_FLUSH, params, drain.utteranceId(sessionEpoch))
        return true
    }

    fun speakQueued(text: String) {
        autoDetectAndSetLanguage(text)
        tts?.speak(text, TextToSpeech.QUEUE_ADD, params, utteranceId)
    }
}
'''

# Rule-B violation only: no sanitize call, empty result voiced raw.
SANITIZE_REMOVED_MANAGER = '''
class TextToSpeechManager {
    fun speak(text: String, explicitLanguage: String? = null): Boolean {
        applyLanguage(TtsLanguagePolicy.resolveTargetLocale(explicitLanguage, text))
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, params, utteranceId)
        return true
    }

    fun speakQueued(text: String) {
        autoDetectAndSetLanguage(text)
        tts?.speak(text, TextToSpeech.QUEUE_ADD, params, utteranceId)
    }
}
'''

# Rule-A violation only: explicit channel reverted to the unconditional guess.
AUTO_DETECT_MANAGER = '''
class TextToSpeechManager {
    fun speak(text: String, explicitLanguage: String? = null): Boolean {
        val speakable = VoiceTextSanitizer.sanitize(text, TtsSpokenPhrases.from(appContext))
        if (speakable.isBlank()) return false
        autoDetectAndSetLanguage(speakable)
        tts?.speak(speakable, TextToSpeech.QUEUE_FLUSH, params, utteranceId)
        return true
    }

    fun speakQueued(text: String) {
        autoDetectAndSetLanguage(text)
        tts?.speak(text, TextToSpeech.QUEUE_ADD, params, utteranceId)
    }
}
'''

# Rule-C violation: streaming path lost its auto-detection.
STREAM_BROKEN_MANAGER = '''
class TextToSpeechManager {
    fun speak(text: String, explicitLanguage: String? = null): Boolean {
        val speakable = VoiceTextSanitizer.sanitize(text, TtsSpokenPhrases.from(appContext))
        if (speakable.isBlank()) return false
        applyLanguage(TtsLanguagePolicy.resolveTargetLocale(explicitLanguage, speakable))
        tts?.speak(speakable, TextToSpeech.QUEUE_FLUSH, params, utteranceId)
        return true
    }

    fun speakQueued(text: String) {
        tts?.speak(text, TextToSpeech.QUEUE_ADD, params, utteranceId)
    }
}
'''

# Rule-B exemption arm: raw-text caller declares why.
EXEMPT_MANAGER = '''
class TextToSpeechManager {
    fun speak(text: String, explicitLanguage: String? = null): Boolean {
        // tts-sanitizer-exempt: diagnostic readback must voice the raw payload
        applyLanguage(TtsLanguagePolicy.resolveTargetLocale(explicitLanguage, text))
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, params, utteranceId)
        return true
    }

    fun speakQueued(text: String) {
        autoDetectAndSetLanguage(text)
        tts?.speak(text, TextToSpeech.QUEUE_ADD, params, utteranceId)
    }
}
'''

PRE_FIX_HANDLER = '''
class SpeakOffloadHandler {
    private fun cmdSpeak(args: OffloadArgs): NativeOffloadResult {
        val voiceTag = args.get("voice")
        if (!voiceTag.isNullOrBlank()) {
            tts.setLanguage(Locale.forLanguageTag(voiceTag))
        }
        tts.speak(text)
        return ok().put("voice", voiceTag ?: "auto")
    }
}
'''

COMPLIANT_HANDLER = '''
class SpeakOffloadHandler {
    private fun cmdSpeak(args: OffloadArgs): NativeOffloadResult {
        val voiceTag = args.get("voice")
        val spoke = tts.speak(text, explicitLanguage = voiceTag)
        if (!spoke) {
            return err().put("error", if (engineDown) "tts_unavailable" else "nothing_to_speak")
        }
        val effectiveVoice = tts.currentLanguageTag() ?: "unknown"
        return ok().put("voice", effectiveVoice)
    }
}
'''


def write_fixture(root, rel, body):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def selftest():
    cases = [
        # (label, manager_body, handler_body, expected_failure_codes)
        (
            "pre-fix shape (override + echo + zero sanitize)",
            PRE_FIX_MANAGER,
            PRE_FIX_HANDLER,
            {"tts-voice", "tts-sanitizer"},
        ),
        (
            "compliant shape (policy + sanitize + honest voice)",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            set(),
        ),
        (
            "rule B only: sanitize call removed",
            SANITIZE_REMOVED_MANAGER,
            COMPLIANT_HANDLER,
            {"tts-sanitizer"},
        ),
        (
            "rule A only: explicit channel reverted to auto-detect",
            AUTO_DETECT_MANAGER,
            COMPLIANT_HANDLER,
            {"tts-voice"},
        ),
        (
            "rule A handler only: handler still sets language + echoes tag",
            COMPLIANT_MANAGER,
            PRE_FIX_HANDLER,
            {"tts-voice"},
        ),
        (
            "rule C only: streaming path lost auto-detection",
            STREAM_BROKEN_MANAGER,
            COMPLIANT_HANDLER,
            {"tts-stream"},
        ),
        (
            "rule B exemption: declared raw-text caller passes",
            EXEMPT_MANAGER,
            COMPLIANT_HANDLER,
            set(),
        ),
    ]
    ok = True
    for label, manager, handler, expected in cases:
        root = tempfile.mkdtemp(prefix="ttsgate-")
        try:
            write_fixture(root, MANAGER_REL, manager)
            write_fixture(root, HANDLER_REL, handler)
            failures = run(root, verbose=False)
            got = set()
            for f in failures:
                m = re.search(r"\[(tts-[a-z]+)\]", f)
                if m:
                    got.add(m.group(1))
            else:
                pass
            passed = got == expected
            print(
                "[{}] selftest: {} (expected {}, got {})".format(
                    "PASS" if passed else "FAIL",
                    label,
                    sorted(expected) if expected else "green",
                    sorted(got) if got else "green",
                )
            )
            if not passed:
                for f in failures:
                    print("        %s" % f)
            ok = ok and passed
        finally:
            shutil.rmtree(root, ignore_errors=True)
    print("tts_voice_and_sanitizer_check selftest: " + ("TTS-VOICE GATE ALL ARMS MATCH" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    failures = run(args.root)
    if failures:
        print("tts_voice_and_sanitizer_check: FAIL (%d violation(s))" % len(failures))
        return 1
    print("tts_voice_and_sanitizer_check: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

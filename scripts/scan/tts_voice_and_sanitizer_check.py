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
# [deep-audit-1044 A-06/A-07] The two tables this gate now also protects: the
# SpeechRecognizer error map and the per-call TTS defaults.
SYSTEM_SPEECH_REL = os.path.join(
    MAIN_ROOT, "com", "rikkaminis", "app", "speech", "SystemSpeechRecognitionEngine.kt"
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


def strip_comments(src):
    """Blank out `//` and `/* */` comments (and keep string literals verbatim).

    A criterion must be satisfiable only by code: this file's own precedents are
    comments that name a code ("11 = ERROR_SERVER_DISCONNECTED") and a KDoc that
    quotes the old table — those must neither satisfy nor trip a rule.
    Offsets/newlines are preserved so line numbers stay usable in messages.
    """
    out = []
    i = 0
    n = len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join("\n" if c == "\n" else " " for c in src[i:j]))
            i = j
        elif src[i] == '"':
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(src[i:j])
            i = j
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def split_top_level(text, sep):
    """Split on `sep` occurrences that sit outside (), [] and {}."""
    parts = []
    depth = 0
    cur = []
    i = 0
    while i < len(text):
        if depth == 0 and text.startswith(sep, i):
            parts.append("".join(cur))
            cur = []
            i += len(sep)
            continue
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


ARM_RESULT_RE = re.compile(r'\s*(?:"(?:[^"\\]|\\.)*"|[A-Za-z_][\w.]*)')


def when_arms(body):
    """[(labels_set, result_expr)] for a `when` block body (`{ ... -> ... }`).

    Only the shapes this gate reads are supported: integer labels and an
    identifier or a string-literal result. Anything else yields an empty label
    set, which the callers treat as "arm not covered" (fail-closed).
    """
    inner = body.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    segments = split_top_level(inner, "->")
    arms = []
    rest = segments[0]
    for seg in segments[1:]:
        labels_text = rest
        m = ARM_RESULT_RE.match(seg)
        result = m.group(0).strip() if m else ""
        rest = seg[m.end():] if m else seg
        codes = set()
        for part in split_top_level(labels_text, ","):
            part = part.strip()
            if re.fullmatch(r"\d+", part):
                codes.add(int(part))
        arms.append((codes, result))
    return arms


def arm_result(arms, code):
    """Result expression of the arm covering `code`, or None when uncovered."""
    for codes, result in arms:
        if code in codes:
            return result
    return None


def resolves_to(result, simple_name):
    """`RecognitionError.LANGUAGE_UNSUPPORTED` and `LANGUAGE_UNSUPPORTED` both count."""
    if not result:
        return False
    return result.split(".")[-1].strip() == simple_name


def check_error_table(src):
    """[deep-audit-1044 A-06] SpeechRecognizer error codes must not be shifted.

    The defect: `mapError`/`errorMessage` labelled 11/12 as the API-33 language
    pair, so 11 (ERROR_SERVER_DISCONNECTED, API 31) surfaced as "language not
    supported" and 13 (ERROR_LANGUAGE_UNAVAILABLE) fell through to UNKNOWN —
    which silently disabled `onError`'s on-device→cloud retry (it gates on
    LANGUAGE_UNSUPPORTED). The decision points pinned here:
      10 -> busy, 11 -> network (and never language), 12/13 -> language.
    """
    failures = []
    code_src = strip_comments(src)

    map_body = function_body(code_src, "fun mapError(")
    if map_body is None:
        failures.append(
            "%s: [tts-errorcodes] mapError() not found - the SpeechRecognizer "
            "code table is what keeps the on-device retry alive" % SYSTEM_SPEECH_REL
        )
    else:
        arms = when_arms(map_body)
        r13 = arm_result(arms, 13)
        if not resolves_to(r13, "LANGUAGE_UNSUPPORTED"):
            failures.append(
                "%s: [tts-errorcodes] mapError must map 13 "
                "(ERROR_LANGUAGE_UNAVAILABLE) to LANGUAGE_UNSUPPORTED - the "
                "on-device->cloud retry in onError gates on it (got %r)"
                % (SYSTEM_SPEECH_REL, r13)
            )
        r12 = arm_result(arms, 12)
        if not resolves_to(r12, "LANGUAGE_UNSUPPORTED"):
            failures.append(
                "%s: [tts-errorcodes] mapError must map 12 "
                "(ERROR_LANGUAGE_NOT_SUPPORTED) to LANGUAGE_UNSUPPORTED (got %r)"
                % (SYSTEM_SPEECH_REL, r12)
            )
        r11 = arm_result(arms, 11)
        if r11 is None:
            failures.append(
                "%s: [tts-errorcodes] mapError must cover 11 "
                "(ERROR_SERVER_DISCONNECTED, API 31)" % SYSTEM_SPEECH_REL
            )
        elif resolves_to(r11, "LANGUAGE_UNSUPPORTED"):
            failures.append(
                "%s: [tts-errorcodes] 11 is ERROR_SERVER_DISCONNECTED, not a "
                "language code - a dropped service must not be reported as "
                "'language not supported'" % SYSTEM_SPEECH_REL
            )
        elif not resolves_to(r11, "NETWORK"):
            failures.append(
                "%s: [tts-errorcodes] mapError must map 11 "
                "(ERROR_SERVER_DISCONNECTED) to NETWORK (got %r)"
                % (SYSTEM_SPEECH_REL, r11)
            )
        r10 = arm_result(arms, 10)
        if r10 is None:
            failures.append(
                "%s: [tts-errorcodes] mapError must cover 10 "
                "(ERROR_TOO_MANY_REQUESTS, API 31)" % SYSTEM_SPEECH_REL
            )
        elif resolves_to(r10, "UNKNOWN"):
            failures.append(
                "%s: [tts-errorcodes] mapError must not leave 10 "
                "(ERROR_TOO_MANY_REQUESTS) as UNKNOWN - a saturated recognizer "
                "is a retryable busy, not an unknown fault" % SYSTEM_SPEECH_REL
            )
        elif not resolves_to(r10, "RECOGNIZER_BUSY"):
            failures.append(
                "%s: [tts-errorcodes] mapError must map 10 "
                "(ERROR_TOO_MANY_REQUESTS) to RECOGNIZER_BUSY (got %r)"
                % (SYSTEM_SPEECH_REL, r10)
            )

    msg_body = function_body(code_src, "fun errorMessage(")
    if msg_body is None:
        failures.append(
            "%s: [tts-errorcodes] errorMessage() not found - the user-visible "
            "half of the same table" % SYSTEM_SPEECH_REL
        )
    else:
        arms = when_arms(msg_body)
        texts = {}
        for code in (10, 11, 12, 13):
            result = arm_result(arms, code)
            if result is None:
                failures.append(
                    "%s: [tts-errorcodes] errorMessage must cover %d - otherwise "
                    "the user reads 'Unknown error (%d)' and can do nothing"
                    % (SYSTEM_SPEECH_REL, code, code)
                )
                continue
            if not result.startswith('"'):
                failures.append(
                    "%s: [tts-errorcodes] errorMessage(%d) must be a message "
                    "string (got %r)" % (SYSTEM_SPEECH_REL, code, result)
                )
                continue
            if result.strip('"').startswith("Unknown error"):
                failures.append(
                    "%s: [tts-errorcodes] errorMessage(%d) still falls back to "
                    "'Unknown error'" % (SYSTEM_SPEECH_REL, code)
                )
                continue
            texts[code] = result
        if 12 in texts and 13 in texts and texts[12] == texts[13]:
            failures.append(
                "%s: [tts-errorcodes] 12 (language not supported) and 13 (pack "
                "not downloaded) must read differently - only 13 is fixable by "
                "the user" % SYSTEM_SPEECH_REL
            )
    return failures


def check_per_call_defaults(handler):
    """[deep-audit-1044 A-07] `android-speak` must apply its tuning every call.

    `tts` is a process-wide singleton, so `args.getDouble("x")?.let { ... }`
    means "absent flag = keep the previous call's value": one `--volume 0`
    silenced the rest of the process. The decision pinned here is
    *unconditional assignment with the documented default (1.0)*, not the
    spelling of the resolver — inline `?: 1.0` and a `TtsSpeakTuning.from(...)`
    call both pass.
    """
    failures = []
    code_src = strip_comments(handler)

    if re.search(r'getDouble\(\s*"\s*(?:rate|pitch|volume)\s*"\s*\)\s*\?\.let', code_src):
        failures.append(
            "%s: [tts-defaults] rate/pitch/volume must not be applied "
            "conditionally (`args.getDouble(\"x\")?.let { tts.speechX = ... }`) "
            "- an absent flag then inherits the previous call's value" % HANDLER_REL
        )

    body = function_body(code_src, "fun cmdSpeak(")
    if body is None:
        failures.append(
            "%s: [tts-defaults] cmdSpeak() not found" % HANDLER_REL
        )
        return failures

    for field_name, flag in (
        ("speechRate", "rate"),
        ("speechPitch", "pitch"),
        ("speechVolume", "volume"),
    ):
        advances = re.findall(r"tts\.%s\s*=" % field_name, body)
        if len(advances) != 1:
            failures.append(
                "%s: [tts-defaults] cmdSpeak must assign tts.%s exactly once "
                "per call (found %d) - the manager is a process-wide singleton, "
                "so a skipped assignment leaks the previous call's value"
                % (HANDLER_REL, field_name, len(advances))
            )
            continue
        for line in body.splitlines():
            if not re.search(r"tts\.%s\s*=" % field_name, line):
                continue
            stripped = line.strip()
            if stripped.startswith(("if ", "if(", "when ", "else")) or "?.let" in stripped:
                failures.append(
                    "%s: [tts-defaults] tts.%s is assigned conditionally: %r"
                    % (HANDLER_REL, field_name, stripped)
                )
            break

    for flag in ("rate", "pitch", "volume"):
        if not resolve_default_ok(code_src, flag):
            failures.append(
                "%s: [tts-defaults] the per-call default for --%s must resolve "
                "to 1.0 (inline `?: 1.0` or a named constant equal to 1.0) - the "
                "CLI contract documents 1.0 for all three" % (HANDLER_REL, flag)
            )
    return failures


def resolve_default_ok(src, flag):
    """True when `flag`'s absent-value default is the documented 1.0."""
    patterns = [
        r"\(\s*%s\s*\?:\s*([^)\s]+)\s*\)" % flag,
        r'getDouble\(\s*"\s*%s\s*"\s*\)\s*\?:\s*([^\s]+)' % flag,
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, src):
            value = m.group(1).strip()
            if value in ("1.0", "1.0f", "1f", "1"):
                return True
            const = re.search(
                r"const\s+val\s+%s\s*(?::\s*Double\s*)?=\s*([0-9.]+)" % re.escape(value),
                src,
            )
            if const and const.group(1) in ("1.0", "1"):
                return True
    return False


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

    if handler is not None:
        failures.extend(check_per_call_defaults(handler))

    system_speech = read(root, SYSTEM_SPEECH_REL)
    if system_speech is None:
        failures.append(
            "%s: [tts-files] SystemSpeechRecognitionEngine.kt not found"
            % SYSTEM_SPEECH_REL
        )
    else:
        failures.extend(check_error_table(system_speech))

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
        val tuning = TtsSpeakTuning.from(
            rate = args.getDouble("rate"),
            pitch = args.getDouble("pitch"),
            volume = args.getDouble("volume"),
        )
        tts.speechRate = tuning.rate
        tts.speechPitch = tuning.pitch
        tts.speechVolume = tuning.volume
        val spoke = tts.speak(text, explicitLanguage = voiceTag)
        if (!spoke) {
            return err().put("error", if (engineDown) "tts_unavailable" else "nothing_to_speak")
        }
        val effectiveVoice = tts.currentLanguageTag() ?: "unknown"
        return ok().put("voice", effectiveVoice)
    }
}

internal data class TtsSpeakTuning(val rate: Float, val pitch: Float, val volume: Float) {
    companion object {
        const val DEFAULT = 1.0

        fun from(rate: Double?, pitch: Double?, volume: Double?): TtsSpeakTuning =
            TtsSpeakTuning(
                rate = (rate ?: DEFAULT).toFloat(),
                pitch = (pitch ?: DEFAULT).toFloat(),
                volume = (volume ?: DEFAULT).toFloat(),
            )
    }
}
'''

# A-07 violation 1: the pre-fix conditional application (an absent flag keeps
# whatever the previous invocation left in the process-wide singleton).
CONDITIONAL_TUNING_HANDLER = '''
class SpeakOffloadHandler {
    private fun cmdSpeak(args: OffloadArgs): NativeOffloadResult {
        val voiceTag = args.get("voice")
        args.getDouble("rate")?.let { tts.speechRate = it.toFloat() }
        args.getDouble("pitch")?.let { tts.speechPitch = it.toFloat() }
        args.getDouble("volume")?.let { tts.speechVolume = it.toFloat() }
        val spoke = tts.speak(text, explicitLanguage = voiceTag)
        if (!spoke) {
            return err().put("error", if (engineDown) "tts_unavailable" else "nothing_to_speak")
        }
        val effectiveVoice = tts.currentLanguageTag() ?: "unknown"
        return ok().put("voice", effectiveVoice)
    }
}
'''

# A-07 violation 2: unconditional, but the absent-flag default is not 1.0 —
# the documented contract for a call without `--rate`.
WRONG_DEFAULT_HANDLER = COMPLIANT_HANDLER.replace("rate = (rate ?: DEFAULT)", "rate = (rate ?: 0.0)")

# ── A-06 fixtures: the SpeechRecognizer error table ─────────────────────────

COMPLIANT_SYSTEM = '''
class SystemSpeechRecognitionEngine(private val appContext: Context) {
    companion object {
        internal fun mapError(code: Int): RecognitionError = when (code) {
            SpeechRecognizer.ERROR_NO_MATCH,
            SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> RecognitionError.NO_MATCH
            SpeechRecognizer.ERROR_NETWORK -> RecognitionError.NETWORK
            10 -> RecognitionError.RECOGNIZER_BUSY
            11 -> RecognitionError.NETWORK
            12 -> RecognitionError.LANGUAGE_UNSUPPORTED
            13 -> RecognitionError.LANGUAGE_UNSUPPORTED
            else -> RecognitionError.UNKNOWN
        }

        internal fun errorMessage(code: Int): String = when (code) {
            SpeechRecognizer.ERROR_NO_MATCH -> "No speech recognized"
            10 -> "Too many requests - try again in a moment"
            11 -> "Recognition service disconnected"
            12 -> "Language not supported by the recognizer"
            13 -> "Language pack unavailable - download it in the system speech settings"
            else -> "Unknown error ($code)"
        }
    }
}
'''

# The defect itself: 11/12 read as the API-33 language pair, 13 uncovered.
PRE_FIX_SYSTEM = '''
class SystemSpeechRecognitionEngine(private val appContext: Context) {
    companion object {
        private fun mapError(code: Int): RecognitionError = when (code) {
            SpeechRecognizer.ERROR_NO_MATCH -> RecognitionError.NO_MATCH
            11 /* ERROR_LANGUAGE_NOT_SUPPORTED */,
            12 /* ERROR_LANGUAGE_UNAVAILABLE   */ -> RecognitionError.LANGUAGE_UNSUPPORTED
            else -> RecognitionError.UNKNOWN
        }

        private fun errorMessage(code: Int): String = when (code) {
            11 /* ERROR_LANGUAGE_NOT_SUPPORTED */ -> "Language not supported by the recognizer"
            12 /* ERROR_LANGUAGE_UNAVAILABLE */ -> "Language pack unavailable"
            else -> "Unknown error ($code)"
        }
    }
}
'''

MUT_11_LANGUAGE_SYSTEM = COMPLIANT_SYSTEM.replace(
    "11 -> RecognitionError.NETWORK", "11 -> RecognitionError.LANGUAGE_UNSUPPORTED"
)
MUT_10_UNKNOWN_SYSTEM = COMPLIANT_SYSTEM.replace(
    "10 -> RecognitionError.RECOGNIZER_BUSY", "10 -> RecognitionError.UNKNOWN"
)
MUT_13_UNKNOWN_SYSTEM = COMPLIANT_SYSTEM.replace(
    "13 -> RecognitionError.LANGUAGE_UNSUPPORTED", "13 -> RecognitionError.UNKNOWN"
)
MUT_MSG_13_SYSTEM = COMPLIANT_SYSTEM.replace(
    '            13 -> "Language pack unavailable - download it in the system speech settings"\n',
    "",
)


def write_fixture(root, rel, body):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def selftest():
    cases = [
        # (label, manager_body, handler_body, system_body, expected_failure_codes)
        (
            "pre-fix shape (override + echo + zero sanitize + conditional tuning)",
            PRE_FIX_MANAGER,
            PRE_FIX_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-voice", "tts-sanitizer", "tts-defaults"},
        ),
        (
            "compliant shape (policy + sanitize + honest voice + full code table)",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            set(),
        ),
        (
            "rule B only: sanitize call removed",
            SANITIZE_REMOVED_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-sanitizer"},
        ),
        (
            "rule A only: explicit channel reverted to auto-detect",
            AUTO_DETECT_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-voice"},
        ),
        (
            "rule A handler only: handler still sets language + echoes tag",
            COMPLIANT_MANAGER,
            PRE_FIX_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-voice", "tts-defaults"},
        ),
        (
            "rule C only: streaming path lost auto-detection",
            STREAM_BROKEN_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-stream"},
        ),
        (
            "rule B exemption: declared raw-text caller passes",
            EXEMPT_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            set(),
        ),
        # ── A-06: the error table (teeth, one arm per decision point) ──
        (
            "code table reverted to the pre-fix shift (11/12 language, 13 unknown)",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            PRE_FIX_SYSTEM,
            {"tts-errorcodes"},
        ),
        (
            "code table: 11 (server disconnected) reported as a language failure",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            MUT_11_LANGUAGE_SYSTEM,
            {"tts-errorcodes"},
        ),
        (
            "code table: 10 (too many requests) left as UNKNOWN",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            MUT_10_UNKNOWN_SYSTEM,
            {"tts-errorcodes"},
        ),
        (
            "code table: 13 (language pack unavailable) left as UNKNOWN",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            MUT_13_UNKNOWN_SYSTEM,
            {"tts-errorcodes"},
        ),
        (
            "code table: 13 lost its user-visible message",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            MUT_MSG_13_SYSTEM,
            {"tts-errorcodes"},
        ),
        # ── A-07: per-call defaults (teeth) ──
        (
            "per-call defaults: rate/pitch/volume applied conditionally again",
            COMPLIANT_MANAGER,
            CONDITIONAL_TUNING_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-defaults"},
        ),
        (
            "per-call defaults: absent --rate no longer resolves to 1.0",
            COMPLIANT_MANAGER,
            WRONG_DEFAULT_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-defaults"},
        ),
    ]
    ok = True
    for label, manager, handler, system, expected in cases:
        root = tempfile.mkdtemp(prefix="ttsgate-")
        try:
            write_fixture(root, MANAGER_REL, manager)
            write_fixture(root, HANDLER_REL, handler)
            write_fixture(root, SYSTEM_SPEECH_REL, system)
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

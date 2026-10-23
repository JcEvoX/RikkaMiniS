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

# [dv26/G10] Quick Test clip synthesis (deep-audit-1046 S-10). The TTS card's
# `synthesizeTestClip` is the third engine user in this subsystem; it had the
# cancellation hole the other two don't (a late engine-init callback wrote the
# clip back after the coroutine had already been cancelled and the file deleted).
QUICK_TEST_REL = os.path.join(
    MAIN_ROOT, "com", "rikkaminis", "app", "ui", "components", "QuickTestSheet.kt"
)
CLIP_SIG = "fun synthesizeTestClip("
CLIP_FALLBACK_RE = re.compile(
    r"val\s+(\w+)\s*=\s*(?:java\.io\.)?File\(\s*\w+\.cacheDir"
)
# Statements that touch the engine. WRITE ones put bytes on disk; SETUP ones
# configure the engine and therefore open a window in which a cancellation can
# arrive before the write.
TOUCH_SETUP_RE = re.compile(r"\?\s*\.\s*language\s*=|\?\s*\.\s*setOn\w+\s*\(")
TOUCH_WRITE_RE = re.compile(
    r"\.\s*(?:synthesizeToFile|writeText|writeBytes|appendText|createNewFile|copyTo|renameTo)\s*\("
)
CANCEL_READ_RE = re.compile(r"([A-Za-z_]\w*)\s*(?:\?\.|\.)\s*(?:is[A-Z]\w*|get)\b")

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


# ── [dv26/G10] Quick Test clip: cancellation latch ─────────────────────────
def mask_strings(src):
    """Blank string/char literal bodies, preserving offsets and newlines.

    Brace matching must not read `"quicktest-clip-${System.currentTimeMillis()}.wav"`
    as structure; template bodies are blanked too (their braces are what would
    otherwise shift the depth).
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            triple = src.startswith('"""', i)
            quote = '"""' if triple else '"'
            i += len(quote)
            while i < n:
                if not triple and src[i] == "\\":
                    out[i] = " "
                    if i + 1 < n:
                        out[i + 1] = " "
                    i += 2
                    continue
                if src.startswith(quote, i):
                    i += len(quote)
                    break
                if src[i] != "\n":
                    out[i] = " "
                i += 1
        elif c == "'":
            j = i + 1
            closed = False
            while j < n and j < i + 6:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "'":
                    closed = True
                    break
                j += 1
            if not closed:
                i += 1
                continue
            for k in range(i + 1, j):
                out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def brace_map(src):
    """open index -> close index for balanced braces (strings already masked)."""
    pairs, stack = {}, []
    for i, ch in enumerate(src):
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            pairs[stack.pop()] = i
    return pairs


def enclosing_blocks(pairs, pos):
    """Blocks containing `pos`, innermost first."""
    spans = [(o, c) for o, c in pairs.items() if o < pos < c]
    spans.sort(key=lambda s: s[1] - s[0])
    return spans


def matching_paren(src, open_idx):
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def statement_end(src, pos, pairs):
    """End index of the statement starting at `pos` (brace/ paren aware)."""
    i = pos
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "(":
            j = matching_paren(src, i)
            if j < 0:
                return n
            i = j + 1
            continue
        if ch == "{":
            j = pairs.get(i)
            if j is None:
                return n
            i = j + 1
            continue
        if ch == "\n" or ch == ";":
            return i
        i += 1
    return n


def guard_before(code, pairs, pos, cancel_idents, scope):
    """Nearest cancellation guard that dominates `pos`, or None.

    A guard = `if (<reads a cancellation flag>) { … return … }` (or the
    single-statement form) *inside a block that also contains `pos`* — a guard
    parked inside a sibling callback (e.g. the utterance listener's `onStart`)
    does not dominate the call it looks like it guards.
    The flag read is derived, not spelled: the receiver must be an identifier the
    cancellation path itself mentions, or the coroutine's own `cont`.
    """
    for b_open, b_close in enclosing_blocks(pairs, pos):
        if b_open < scope[0] or b_close > scope[1]:
            continue
        best = None
        for m in re.finditer(r"\bif\s*\(", code):
            if m.start() <= b_open or m.start() >= pos:
                continue
            p_open = m.end() - 1
            p_close = matching_paren(code, p_open)
            if p_close < 0 or p_close >= pos:
                continue
            cond = code[p_open + 1 : p_close]
            if not is_cancel_read(cond, cancel_idents):
                continue
            body_end = statement_end(code, p_close + 1, pairs)
            if body_end > pos:
                continue
            body = code[p_close + 1 : body_end]
            if not re.search(r"\breturn\b", body):
                continue
            # the guard's own innermost block must also contain pos
            guards_blocks = enclosing_blocks(pairs, m.start())
            if guards_blocks and guards_blocks[0][1] < pos:
                continue
            best = (m.start(), body_end)
        if best is not None:
            return best
    return None


def is_cancel_read(cond, cancel_idents):
    for m in CANCEL_READ_RE.finditer(cond):
        if m.group(1) in cancel_idents or m.group(1) == "cont":
            return True
    return False


def check_quick_test_clip(src):
    """[dv26/G10] The Quick Test clip must not outlive its caller.

    C1 (*guard*) — inside the `suspendCancellableCoroutine` block, every engine
    SETUP statement (`language =`, `setOn…Listener(`) needs a cancellation guard
    before it, and every WRITE (`synthesizeToFile(`, file writes) needs a guard
    that is *after the last setup statement* — the init callback and the write are
    separated by the setup work, and that window was the defect: a cancellation
    landing in it reached the write unharmed.
    C2 (*cancel*) — `invokeOnCancellation` must exist inside the block, shut the
    engine down, and re-delete the clip (the engine can have finished the write
    between dispatch and cancellation).
    C3 (*cleanup*) — the `finally` must still delete the clip, and the block that
    hands the result back (`resumeWith`) must drop the file when it ran after a
    cancellation (that is the late-write path: no caller will ever read it).
    """
    failures = []
    code = mask_strings(strip_comments(src))
    body = function_body(code, CLIP_SIG)
    if body is None:
        return [
            "%s: [tts-clip-guard] %s not found — re-derive this criterion instead "
            "of deleting it (the clip's cancellation latch is what keeps a late "
            "engine callback from writing an orphan WAV into cacheDir)"
            % (QUICK_TEST_REL, CLIP_SIG.strip())
        ]

    m = CLIP_FALLBACK_RE.search(body)
    clip_var = m.group(1) if m else None

    coro = re.search(r"suspendCancellableCoroutine\s*\{\s*(\w+)\s*->", code)
    span = None
    if coro is not None:
        pairs_all = brace_map(code)
        open_curly = code.find("{", coro.start())
        close_curly = pairs_all.get(open_curly)
        if close_curly is not None:
            span = (open_curly, close_curly)
    if span is None:
        return [
            "%s: [tts-clip-guard] no `suspendCancellableCoroutine { cont -> … }` "
            "block in synthesizeTestClip — re-derive" % QUICK_TEST_REL
        ]

    block = code[span[0] : span[1] + 1]
    pairs = brace_map(block)

    # identifiers the cancellation path itself mentions ⇒ candidate flags
    cancel_body = ""
    cancel_span = None
    inv = re.search(r"invokeOnCancellation\s*\{", block)
    if inv is not None:
        o = block.find("{", inv.start())
        c = pairs.get(o)
        if c is not None:
            cancel_span = (o, c)
            cancel_body = block[o : c + 1]
    cancel_idents = set(re.findall(r"[A-Za-z_]\w*", cancel_body)) if cancel_body else set()

    def fail(tag, msg):
        failures.append("%s: [%s] %s" % (QUICK_TEST_REL, tag, msg))

    # ── C1: guard dominates every engine statement ──
    touches = []
    for rex, kind in ((TOUCH_SETUP_RE, "setup"), (TOUCH_WRITE_RE, "write")):
        for t in rex.finditer(block):
            touches.append((t.start(), kind, t.group(0).strip()))
    touches.sort()
    for pos, kind, what in touches:
        g = guard_before(block, pairs, pos, cancel_idents, (0, len(block) - 1))
        if g is None:
            fail(
                "tts-clip-guard",
                "`%s` runs without a cancellation guard before it — a callback "
                "that lands after the 15 s timeout / sheet dismissal must return "
                "before touching the engine" % what,
            )
            continue
        if kind == "write":
            between = block[g[1] : pos]
            setup = TOUCH_SETUP_RE.search(between)
            if setup is not None:
                fail(
                    "tts-clip-guard",
                    "`%s` is guarded only across the engine setup "
                    "(`%s`) — the window between the init callback and the write "
                    "must be re-checked, else a cancellation landing in it still "
                    "writes the clip" % (what, setup.group(0).strip()),
                )

    # ── C2: the cancel path shuts the engine down AND re-deletes the clip ──
    if cancel_span is None:
        fail(
            "tts-clip-cancel",
            "no `invokeOnCancellation { … }` in the coroutine block — the engine "
            "leaks and the clip survives cancellation",
        )
    else:
        if "shutdown" not in cancel_body:
            fail("tts-clip-cancel", "`invokeOnCancellation` must shut the engine down")
        if clip_var is None or ("%s.delete()" % clip_var) not in cancel_body.replace(" ", ""):
            fail(
                "tts-clip-cancel",
                "`invokeOnCancellation` must re-delete the clip: the engine can "
                "have finished the write between the dispatch and this cancellation",
            )

    # ── C3: exit-path cleanup + late-callback cleanup ──
    fin = re.search(r"\bfinally\s*\{", body)
    fib = None
    if fin is not None:
        fib = function_body(body[fin.start() :], "finally {")
    if fib is None or clip_var is None or ("%s.delete()" % clip_var) not in fib.replace(" ", ""):
        fail(
            "tts-clip-cleanup",
            "the `finally` block must still delete the clip variable — the "
            "success path reads it and nothing else cleans cacheDir",
        )
    resume = block.find("resumeWith(")
    if resume < 0:
        fail("tts-clip-cleanup", "no `resumeWith(` in the coroutine block — re-derive")
    else:
        host = enclosing_blocks(pairs, resume)
        host_span = host[0] if host else (0, len(block) - 1)
        guarded_delete = False
        for m2 in re.finditer(r"\bif\s*\(", block[host_span[0] : host_span[1] + 1]):
            base = host_span[0]
            p_open = base + m2.end() - 1
            p_close = matching_paren(block, p_open)
            if p_close < 0:
                continue
            if not is_cancel_read(block[p_open + 1 : p_close], cancel_idents):
                continue
            tail = block[p_close + 1 : p_close + 200].replace(" ", "")
            if clip_var is not None and ("%s.delete()" % clip_var) in tail:
                guarded_delete = True
                break
        if not guarded_delete:
            fail(
                "tts-clip-cleanup",
                "the block that hands the result back must drop the clip when it "
                "ran after a cancellation — a late onDone/onError leaves the "
                "orphan WAV in cacheDir",
            )

    return failures


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

    # ── [dv26/G10] the Quick Test clip's cancellation latch ──
    quick_test = read(root, QUICK_TEST_REL)
    if quick_test is None:
        failures.append(
            "%s: [tts-files] QuickTestSheet.kt not found" % QUICK_TEST_REL
        )
    else:
        failures.extend(check_quick_test_clip(quick_test))

    if verbose:
        for line in failures:
            print(line)
    return failures



# ── [dv26/G10] Quick Test clip fixtures ─────────────────────────────────────
COMPLIANT_QUICK = '''
private suspend fun synthesizeTestClip(context: Context, text: String): ByteArray? {
    val file = java.io.File(context.cacheDir, "quicktest-clip-${System.currentTimeMillis()}.wav")
    val cancelled = java.util.concurrent.atomic.AtomicBoolean(false)
    return try {
        kotlinx.coroutines.withTimeoutOrNull(15_000) {
            kotlinx.coroutines.suspendCancellableCoroutine { cont ->
                var tts: TextToSpeech? = null
                fun finish(ok: Boolean) {
                    runCatching { tts?.shutdown() }
                    if (SynthClipPolicy.mustDropOnCallback(cancelled.get())) runCatching { file.delete() }
                    if (cont.isActive) cont.resumeWith(Result.success(ok))
                }
                tts = TextToSpeech(context) { status ->
                    if (!SynthClipPolicy.mayTouchEngine(cancelled.get())) {
                        runCatching { tts?.shutdown() }
                        return@TextToSpeech
                    }
                    if (status != TextToSpeech.SUCCESS) {
                        finish(false)
                        return@TextToSpeech
                    }
                    tts?.language = java.util.Locale.US
                    tts?.setOnUtteranceProgressListener(listener)
                    if (!SynthClipPolicy.maySynthesize(cancelled.get())) {
                        runCatching { tts?.shutdown() }
                        return@TextToSpeech
                    }
                    val result = tts?.synthesizeToFile(text, android.os.Bundle(), file, "quicktest-clip")
                    if (result != TextToSpeech.SUCCESS) finish(false)
                }
                cont.invokeOnCancellation {
                    cancelled.set(true)
                    runCatching { tts?.shutdown() }
                    runCatching { file.delete() }
                }
            }
        }?.takeIf { it }?.let { file.readBytes() }
    } catch (e: Exception) {
        null
    } finally {
        runCatching { file.delete() }
    }
}
'''

# The defect: no cancellation latch at all — the late init callback wrote the
# clip after `finally` had already deleted it.
PRE_FIX_QUICK = '''
private suspend fun synthesizeTestClip(context: Context, text: String): ByteArray? {
    val file = java.io.File(context.cacheDir, "quicktest-clip-${System.currentTimeMillis()}.wav")
    return try {
        kotlinx.coroutines.withTimeoutOrNull(15_000) {
            kotlinx.coroutines.suspendCancellableCoroutine { cont ->
                var tts: TextToSpeech? = null
                fun finish(ok: Boolean) {
                    runCatching { tts?.shutdown() }
                    if (cont.isActive) cont.resumeWith(Result.success(ok))
                }
                tts = TextToSpeech(context) { status ->
                    if (status != TextToSpeech.SUCCESS) {
                        finish(false)
                        return@TextToSpeech
                    }
                    tts?.language = java.util.Locale.US
                    tts?.setOnUtteranceProgressListener(listener)
                    val result = tts?.synthesizeToFile(text, android.os.Bundle(), file, "quicktest-clip")
                    if (result != TextToSpeech.SUCCESS) finish(false)
                }
                cont.invokeOnCancellation { runCatching { tts?.shutdown() } }
            }
        }?.takeIf { it }?.let { file.readBytes() }
    } catch (e: Exception) {
        null
    } finally {
        runCatching { file.delete() }
    }
}
'''

INIT_GUARD = """                    if (!SynthClipPolicy.mayTouchEngine(cancelled.get())) {
                        runCatching { tts?.shutdown() }
                        return@TextToSpeech
                    }
"""

SYNTH_GUARD = """                    if (!SynthClipPolicy.maySynthesize(cancelled.get())) {
                        runCatching { tts?.shutdown() }
                        return@TextToSpeech
                    }
"""

LATE_DROP = "                    if (SynthClipPolicy.mustDropOnCallback(cancelled.get())) runCatching { file.delete() }\n"

CANCEL_DROP = """                    runCatching { file.delete() }
                }
"""


def _mut(rel, old, new, count=1):
    got = rel.count(old)
    if got != count:
        raise AssertionError("fixture anchor hit %d times, expected %d: %r" % (got, count, old[:60]))
    return rel.replace(old, new, count)


# m1: the init callback lost its entry guard (late callback touches the engine)
MUT_NO_INIT_GUARD_QUICK = _mut(COMPLIANT_QUICK, INIT_GUARD, "")
# m2: only the init guard left — the window between setup and the write is open
MUT_NO_SYNTH_GUARD_QUICK = _mut(COMPLIANT_QUICK, SYNTH_GUARD, "")
# m3: the guard moved AFTER synthesizeToFile (looks guarded, guards nothing)
MUT_GUARD_AFTER_SYNTH_QUICK = _mut(
    _mut(COMPLIANT_QUICK, INIT_GUARD, ""),
    '                    val result = tts?.synthesizeToFile(text, android.os.Bundle(), file, "quicktest-clip")\n',
    '                    val result = tts?.synthesizeToFile(text, android.os.Bundle(), file, "quicktest-clip")\n'
    + INIT_GUARD,
)
# m4: cancel path keeps shutdown but dropped the re-delete
MUT_CANCEL_NO_DROP_QUICK = _mut(COMPLIANT_QUICK, CANCEL_DROP, "                }\n")
# m5: the late callback no longer drops the clip it just wrote
MUT_NO_LATE_DROP_QUICK = _mut(COMPLIANT_QUICK, LATE_DROP, "")
# m6: exit-path cleanup removed (success path leaves the clip behind)
MUT_NO_FINALLY_DROP_QUICK = _mut(
    COMPLIANT_QUICK,
    """    } finally {
        runCatching { file.delete() }
    }
""",
    """    }
""",
)

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
        # ── S-10 / G10: the Quick Test clip's cancellation latch (one arm per
        #    decision point, plus the pre-fix shape as the PRE arm) ──
        (
            "clip: pre-fix shape (no latch — late init callback writes an orphan WAV)",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-clip-guard", "tts-clip-cancel", "tts-clip-cleanup"},
            PRE_FIX_QUICK,
        ),
        (
            "clip: compliant latch",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            set(),
            COMPLIANT_QUICK,
        ),
        (
            "clip: init callback lost its entry guard",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-clip-guard"},
            MUT_NO_INIT_GUARD_QUICK,
        ),
        (
            "clip: setup→write window no longer re-checked",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-clip-guard"},
            MUT_NO_SYNTH_GUARD_QUICK,
        ),
        (
            "clip: guard parked after synthesizeToFile",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-clip-guard"},
            MUT_GUARD_AFTER_SYNTH_QUICK,
        ),
        (
            "clip: cancel path dropped the re-delete",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-clip-cancel"},
            MUT_CANCEL_NO_DROP_QUICK,
        ),
        (
            "clip: late callback no longer drops the clip",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-clip-cleanup"},
            MUT_NO_LATE_DROP_QUICK,
        ),
        (
            "clip: exit-path cleanup removed",
            COMPLIANT_MANAGER,
            COMPLIANT_HANDLER,
            COMPLIANT_SYSTEM,
            {"tts-clip-cleanup"},
            MUT_NO_FINALLY_DROP_QUICK,
        ),
    ]
    ok = True
    for case in cases:
        label, manager, handler, system, expected = case[:5]
        quick = case[5] if len(case) > 5 else COMPLIANT_QUICK
        root = tempfile.mkdtemp(prefix="ttsgate-")
        try:
            write_fixture(root, MANAGER_REL, manager)
            write_fixture(root, HANDLER_REL, handler)
            write_fixture(root, SYSTEM_SPEECH_REL, system)
            write_fixture(root, QUICK_TEST_REL, quick)
            failures = run(root, verbose=False)
            got = set()
            for f in failures:
                m = re.search(r"\[(tts-[a-z-]+)\]", f)
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

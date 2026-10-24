#!/usr/bin/env python3
"""Truncated-call guard wiring gate (deep-audit-1018 ISSUE-01 / ISSUE-12c).

## Why this gate exists

The truncated-tool-call guard ([TruncatedToolCallPolicy]) used to consult ONE
truncation source: the protocol finish_reason. A turn cut off by a network EOF
carries NO finish_reason at all — the providers surface that as
`LLMStreamChunk.Finished.truncated`, and OpenAIProvider's EOF detector gated
its Finished emission on text/reasoning only, so a tool-call-ONLY turn (zero
prose) never set the flag either. Net effect: the P0 failure class
"half-streamed JSON arguments dispatched to the executor" sailed through
every layer, and [ToolJsonRepair] Strategy 1 then CLOSED the cut-off object
into a syntactically valid, semantically wrong payload — a silent side effect
instead of a loud refusal. Four wiring points had to change together, and no
mechanical check would have noticed any one of them regressing:

  1. the policy must accept the stream-level flag (3rd parameter) and must
     actually read it in the verdict condition — a parameter that exists but
     is never consulted is indistinguishable from a regression;
  2. the engine dispatch site must FEED the flag (the KDoc of the original
     site admits finishReason was the only input);
  3. OpenAIProvider's EOF guard must count tool-call events (Anthropic's
     `toolChars` counterpart — same family, previously divergent);
  4. the sub-agent loop must refuse through the SAME policy (single source of
     truth) and must keep the tool_use/tool_result pairing;
  5. [ISSUE-12c] the error-shaped continuation must run the same stacking
     guard as its EOF-stub sibling through the shared
     [EofStubReminderStackGuard] — an inline second predicate would drift.

[deep-audit-1045 A-02] The sibling failure of the same family: the Gemini SSE
loop's EOF `else` arm used to send `Finished(lastFinishReason ?: "end_turn")`,
i.e. it INVENTED a stop reason on a fully empty EOF. `failOnSilentEmptyCompletion`
decides by `Finished.stopReason != null`, so the fabricated value made an empty
relay response read as a clean finish and no TransientError was ever raised.
The terminal decision now lives in the pure `geminiEofTerminal(...)`, which
passes an absent finishReason through as null (criterion 6/7 below).

Behaviour is covered by unit tests (TruncatedCallGuardTest /
EofStubStackingGuardTest / GeminiEofTerminalDecisionTest); this gate pins the
WIRING, because none of these sites can execute without the Android runtime.

## Escape hatch

`truncated-guard-ok: <reason>` on the line above a pinned expression.

Usage: python3 scripts/scan/truncated_call_guard_check.py [repo_root] [--selftest]
Exit:  0 = all wiring present, 1 = violation (file:line printed)
"""
import os
import re
import sys

MAIN = "src/android/app/src/main/java/com/rikkaminis/app"
POLICY = MAIN + "/ui/chat/TruncatedToolCallPolicy.kt"
ENGINE = MAIN + "/ui/chat/AgentLoopEngine.kt"
OPENAI = MAIN + "/provider/openai/OpenAIProvider.kt"
ANTHROPIC = MAIN + "/provider/anthropic/AnthropicProvider.kt"
TOOL_EXECUTORS = MAIN + "/ui/chat/ChatToolExecutors.kt"
GEMINI = MAIN + "/provider/gemini/GeminiProvider.kt"
SIGNALS = MAIN + "/provider/openai/OpenAiStreamSignals.kt"
# [dv29 A-01] Anthropic's tool-lifecycle activity weights — the single home of
# "does this SSE event prove the turn produced tool-call progress".
ANTHROPIC_SIGNALS = MAIN + "/provider/anthropic/AnthropicStreamSignals.kt"

C1_SIGNATURE = ("fun rejectionReason(toolName: String, finishReason: String?, "
                "turnTruncated: Boolean = false): String?")
C1_CONDITION = "if (!ceilingDeclared && !turnTruncated) return null"
C1_PREFIX = "[TRUNCATED] Tool '$toolName' was NOT executed:"
C1_PREFIX_COUNT = 2  # ceiling source + EOF source; a third wording is a gate event

C2_ENGINE_CALL = "TruncatedToolCallPolicy.rejectionReason(name, turnFinishReason, turnTruncated)"

C3_OPENAI_GUARD = ("if (!finishedSent && (contentLen > 0 || reasoningLen > 0 "
                   "|| toolCallEventCount > 0)) {")
C3_ANTHROPIC_GUARD = "if (!sawClearFinish && (contentChars > 0 || toolChars > 0)) {"

C4_SUBAGENT_DECL = "internal suspend fun runSubagentLoop("
C4_POLICY_CALL = "TruncatedToolCallPolicy.rejectionReason("
C4_REFUSAL_GATE = "if (truncatedRefusal != null) {"
C4_PAIRING = "content = truncatedRefusal, isError = true"
C4_NO_SECOND_WORDING = '"[TRUNCATED]'

C5_SHARED_GUARD = "EofStubReminderStackGuard.isStaleEofStubReminder("
C5_GUARD_CALLS = 2
C5_OLD_INLINE = "val tailIsEofStubReminder ="

# [deep-audit-1045 A-02] Gemini's EOF terminal decision must be the pure
# function, and it must never fabricate a stop reason for an absent one.
C6_GEMINI_TERMINAL_CALL = "geminiEofTerminal(lastFinishReason, contentChars, toolChars)"
C6_GEMINI_SEND = "send(LLMStreamChunk.Finished(terminal.stopReason, truncated = terminal.truncated))"
C6_GEMINI_PURE_DECL = "internal fun geminiEofTerminal("
C6_GEMINI_PURE_ELSE = "GeminiEofTerminal(stopReason = lastFinishReason, truncated = false)"
C7_FABRICATED = "lastFinishReason ?:"

# [deep-audit-1048 A-01 / A-08] The two criteria below are DERIVED, not pinned
# by name: the counter variable is read out of the pinned EOF guard, the two
# streaming flavours are told apart by the branch that selects them, and the
# error-mapping function is discovered from the call sites. Renaming anything
# must not blind the gate.
C8_COUNTER_VISIBILITY = 2       # distinct feed sites of the guard's counter
C8_ERROR_STATUS_TOKEN = "429"   # the family the status derivation must produce
C8_ERROR_TRANSIENT_TOKEN = "500"

# [dv29 A-01] Anthropic's three tool-lifecycle arms must ALL feed the EOF
# guard's tool counter, and they must do it through the pure weights in
# AnthropicStreamSignals.kt. Nothing below is pinned by symbol name: the arms
# are located by their protocol labels, the counter is read out of the pinned
# EOF guard, and the weights are whatever pure Int functions that module
# declares. Renaming keeps the gate working; dropping a feed does not.
ANTHROPIC_LIFECYCLE_LABELS = [
    '"content_block_start" ->',
    '"content_block_delta" ->',
    '"content_block_stop" ->',
    '"message_delta" ->',   # terminator of the stop arm, not a judged arm
]
ANTHROPIC_TOOL_USE_TOKEN = '"tool_use"'

# Structural anchors for the selftest arms below. They name the SHAPE of the
# fix, so a rewrite of the fix must update them — that is deliberate: an arm
# that silently stops mutating anything would report a false green.
CHAT_FEED = "toolCallEventCount += chatCompletionsToolCallEventWeight(toolCalls.length())"
RESPONSES_FEED = """toolCallEventCount += responsesToolCallEventWeight(
                        eventType = type,
                        itemType = event.optJSONObject("item")?.optString("type"),
                        argsDeltaLen = event.optString("delta", "").length,
                    )"""
CHAT_INLINE_STATUS = """openAiStreamErrorStatus(
                                inlineError.optString("code", ""),
                                inlineError.optString("type", ""),
                            )"""
RESPONSES_STATUS_CALL = """mapHttpError(
                                openAiStreamErrorStatus(code, err?.optString("type", "") ?: ""),
                                err?.toString() ?: event.toString(),
                            )"""

ESCAPE = "truncated-guard-ok:"
SELFTEST_MARK = "TRUNCATED_CALL_GUARD_SELFTEST=PASS"

# [dv29 A-01] Mutators for the Anthropic feed arms. These are NOT criteria —
# they exist so the arms below can dirty the tree in the shape a regression
# would take. The criteria themselves derive everything from structure.
ANTHROPIC_START_ANCHOR = 'val contentBlock = event.optJSONObject("content_block")'
ANTHROPIC_START_FEED = ("toolChars += toolUseStartActivityChars("
                        "currentToolId!!, currentToolName!!)")
ANTHROPIC_STOP_FEED = ("toolChars += toolUseStopActivityChars("
                       "currentToolId!!, currentToolName!!)")
ANTHROPIC_DELTA_FEED = "toolChars += toolInputDeltaActivityChars(partial)"


def strip_kotlin_comments(text):
    """Blank out // and /* */ comments (newlines preserved, so line numbers
    stay valid). Prose must never satisfy a pinned-expression criterion — the
    KDoc of the fix itself quotes the defect's old line."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def line_of(text, needle):
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else 0


def strip_kotlin_comments_keep(text):
    return strip_kotlin_comments(text)


_TOP_FUN = re.compile(r"internal fun (\w+)\s*\(")


def top_level_int_funs(text):
    """Every `internal fun NAME(` in the pure module, with the parameter list
    that follows it. Prose never counts — comments are stripped first."""
    code = strip_kotlin_comments(text)
    out = {}
    for m in _TOP_FUN.finditer(code):
        close = code.find(")", m.end())
        if close < 0:
            continue
        out[m.group(1)] = code[m.end():close]
    return out


_MAP_CALL = re.compile(r"mapHttpError\(\s*(?:(\w+)\s*\(|([^,\n]+))")


def map_http_first_args(text):
    """(callee, raw) for the first argument of every mapHttpError( call.
    callee is set only when the argument is itself a CALL — that is the shape
    the single-source criterion requires."""
    code = strip_kotlin_comments(text)
    out = []
    for m in _MAP_CALL.finditer(code):
        out.append((m.group(1), (m.group(2) or "").strip()))
    return out


def line_of_regex(text, pattern):
    m = re.search(pattern, text)
    return text.count("\n", 0, m.start()) + 1 if m else 0


def load(root):
    paths = [POLICY, ENGINE, OPENAI, ANTHROPIC, TOOL_EXECUTORS, GEMINI, SIGNALS,
             ANTHROPIC_SIGNALS]
    texts = []
    for rel in paths:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            print("❌ truncated-call gate: missing %s" % rel)
            return None
        with open(path, "r", encoding="utf-8") as f:
            texts.append(f.read())
    return tuple(texts)


def escaped_segment(text, needle):
    """True when the line directly above the pinned expression carries the
    escape hatch — deliberate decisions stay possible, but must be written."""
    idx = text.find(needle)
    while idx >= 0:
        line_start = text.rfind("\n", 0, idx)
        prev_end = text.rfind("\n", 0, line_start)
        if ESCAPE in text[prev_end + 1:line_start + 1]:
            return True
        idx = text.find(needle, idx + 1)
    return False


def anthropic_lifecycle_branches(code):
    """{label: body slice} for the three tool-lifecycle arms of the Anthropic
    SSE `when (eventType)`.

    Sliced by SIBLING PROTOCOL LABELS, never by brace pairing: the arms carry
    lambdas and strings that defeat naive brace matching, and a reformatted or
    reordered `when` must not blind the criteria. Comments must already be
    stripped by the caller (the fix's own KDoc names every label).
    """
    pos = [code.find(label) for label in ANTHROPIC_LIFECYCLE_LABELS]
    if any(p < 0 for p in pos):
        return None
    return {ANTHROPIC_LIFECYCLE_LABELS[i]: code[pos[i]:pos[i + 1]]
            for i in range(len(ANTHROPIC_LIFECYCLE_LABELS) - 1)}


def counter_writes(text, counter):
    """Every `counter += …` / `counter = …` site (never `==`)."""
    return [m.start() for m in
            re.finditer(r"\b%s\b\s*\+?=(?!=)" % re.escape(counter), text)]


def anthropic_tool_counter(anth):
    """The EOF guard's TOOL counter, derived rather than pinned.

    The guard compares two counters against zero; the tool one is the counter
    actually written inside a tool-lifecycle arm. Any surviving feed identifies
    it, so deleting ONE feed still lets the other two criteria be judged (the
    deleted one reports red) instead of collapsing into a name-not-found.
    """
    counters = []
    for name in re.findall(r"([A-Za-z_]\w*)\s*>\s*0\b", C3_ANTHROPIC_GUARD):
        if name not in counters:
            counters.append(name)
    branches = anthropic_lifecycle_branches(anth) or {}
    delta = branches.get('"content_block_delta" ->', "")
    inner = delta.find('"input_json_delta" ->')
    regions = []
    if inner >= 0:
        regions.append(delta[inner:])
    regions.append(branches.get('"content_block_stop" ->', ""))
    regions.append(branches.get('"content_block_start" ->', ""))
    for region in regions:
        for name in counters:
            if counter_writes(region, name):
                return name
    return counters[-1] if counters else None


def check(policy, engine, openai, anthropic, tool_executors, gemini, signals,
          anthropic_signals):
    """Return [(criterion, file, line, message)] — empty list means wired."""
    problems = []

    def add(criterion, rel, text, needle, message):
        if not escaped_segment(text, needle):
            problems.append((criterion, rel, line_of(text, needle), message))

    # C1 — the policy knows the stream-level source AND acts on it.
    if C1_SIGNATURE not in policy:
        problems.append(("policy-flag-signature", POLICY,
                         line_of(policy, "fun rejectionReason("),
                         "rejectionReason lost its turnTruncated parameter"))
    elif C1_CONDITION not in policy:
        problems.append(("policy-flag-condition", POLICY,
                         line_of(policy, "fun rejectionReason("),
                         "turnTruncated exists but the verdict condition ignores it"))
    elif policy.count(C1_PREFIX) != C1_PREFIX_COUNT:
        problems.append(("policy-two-sources", POLICY,
                         line_of(policy, C1_PREFIX),
                         "expected %d refusal wordings (%s), found %d — a source lost its message"
                         % (C1_PREFIX_COUNT, C1_PREFIX, policy.count(C1_PREFIX))))

    # C2 — the engine dispatch site feeds the flag.
    if engine.count(C2_ENGINE_CALL) != 1:
        add("engine-feeds-flag", ENGINE, engine, "truncated tool call REFUSED",
            "the engine tool-call guard must call %s (found %d)"
            % (C2_ENGINE_CALL, engine.count(C2_ENGINE_CALL)))

    # C3 — OpenAI's EOF guard counts tool-call events; Anthropic keeps its own.
    if openai.count(C3_OPENAI_GUARD) != 1:
        problems.append(("openai-eof-counts-toolcalls", OPENAI,
                         line_of(openai, "[RC2-truncated-detection]"),
                         "EOF-without-Finished guard must contain: %s (found %d)"
                         % (C3_OPENAI_GUARD, openai.count(C3_OPENAI_GUARD))))
    if anthropic.count(C3_ANTHROPIC_GUARD) != 1:
        problems.append(("anthropic-sibling-pin", ANTHROPIC,
                         line_of(anthropic, "[RC2-truncated-detection]"),
                         "Anthropic EOF guard shape must keep counting toolChars"))

    # C4 — the sub-agent loop refuses through the shared policy, pairing intact.
    loop_start = tool_executors.find(C4_SUBAGENT_DECL)
    if loop_start < 0:
        problems.append(("subagent-loop-missing", TOOL_EXECUTORS, 0,
                         "runSubagentLoop not found"))
    else:
        # slice to the next top-level doc comment after the loop
        nxt = tool_executors.find("\n/**", loop_start)
        loop = tool_executors[loop_start:nxt if nxt > 0 else len(tool_executors)]
        if loop.count(C4_POLICY_CALL) < 1:
            problems.append(("subagent-refusal-wired", TOOL_EXECUTORS,
                             line_of(tool_executors, C4_SUBAGENT_DECL),
                             "runSubagentLoop must consult TruncatedToolCallPolicy.rejectionReason"))
        elif C4_REFUSAL_GATE not in loop:
            problems.append(("subagent-refusal-gate", TOOL_EXECUTORS,
                             line_of(tool_executors, C4_SUBAGENT_DECL),
                             "the refusal branch (if (truncatedRefusal != null)) is gone"))
        elif C4_PAIRING not in loop:
            problems.append(("subagent-refusal-pairing", TOOL_EXECUTORS,
                             line_of(tool_executors, C4_SUBAGENT_DECL),
                             "a refused call must still produce a tool_result (isError = true)"))
        elif C4_NO_SECOND_WORDING in loop:
            problems.append(("subagent-single-source", TOOL_EXECUTORS,
                             line_of(tool_executors, C4_SUBAGENT_DECL),
                             "runSubagentLoop must not hand-write a second [TRUNCATED] wording"))

    # C5 — both stub-reminder injection sites share one stacking guard.
    guard_calls = engine.count(C5_SHARED_GUARD)
    if guard_calls != C5_GUARD_CALLS:
        problems.append(("stack-guard-shared", ENGINE,
                         line_of(engine, "internal object EofStubReminderStackGuard"),
                         "expected %d shared guard call sites, found %d"
                         % (C5_GUARD_CALLS, guard_calls)))
    if C5_OLD_INLINE in engine:
        problems.append(("stack-guard-no-inline-twin", ENGINE,
                         line_of(engine, C5_OLD_INLINE),
                         "the EOF-stub family's second inline predicate is back — use the shared object"))

    # C6/C7 — Gemini's EOF terminal decision. Comments are stripped first: the
    # fix's own KDoc quotes the defect's old line, and prose must never satisfy
    # a pinned-expression criterion.
    code = strip_kotlin_comments(gemini)
    wired = (
        code.count(C6_GEMINI_TERMINAL_CALL) == 1
        and C6_GEMINI_SEND in code
        and C6_GEMINI_PURE_DECL in code
    )
    if not wired:
        problems.append(("gemini-eof-terminal-pure", GEMINI,
                         line_of(code, C6_GEMINI_PURE_DECL),
                         "the SSE EOF path must decide through the pure %s and emit %s "
                         "(found %d verdict call(s))"
                         % (C6_GEMINI_PURE_DECL, C6_GEMINI_SEND,
                            code.count(C6_GEMINI_TERMINAL_CALL))))
    if C6_GEMINI_PURE_DECL in code and (
            C6_GEMINI_PURE_ELSE not in code or C7_FABRICATED in code):
        # An absent finishReason must be passed through, never substituted:
        # failOnSilentEmptyCompletion decides by `Finished.stopReason != null`,
        # so a fabricated "end_turn" turns an empty relay response into a clean
        # finish. File-scope on purpose (the defect lived at the call site and
        # moves with it), anchored on the two variables involved — the
        # non-streaming `finishReason ?: "end_turn"` path uses a different
        # variable and is a separate (registered) decision.
        problems.append(("gemini-eof-passthrough", GEMINI,
                         line_of(code, C7_FABRICATED) if C7_FABRICATED in code
                         else line_of(code, C6_GEMINI_PURE_DECL),
                         "a fabricated stop reason for an absent finishReason is back "
                         "(%s) — it makes failOnSilentEmptyCompletion read an empty EOF as "
                         "a clean finish; the else arm must stay '%s'"
                         % (C7_FABRICATED, C6_GEMINI_PURE_ELSE)))


    # ------------------------------------------------------------------
    # C8/C9 — [dv28 A-01 / A-08] the OpenAI stream's two flavours must BOTH
    # be visible to the EOF guard, and every in-stream error must reach the
    # ONE status derivation.
    #
    # Both criteria are DERIVED from structure, never from a symbol name:
    #   * the counter is whatever identifier the pinned guard compares to 0;
    #   * the "weight" derivations are every pure Int function in the signals
    #     module MINUS the status derivation;
    #   * the status derivation is whatever function is passed as the first
    #     argument of a mapHttpError( call.
    # Renaming any of them keeps the gate working; deleting a wire-up does not.
    # ------------------------------------------------------------------
    ok_code = strip_kotlin_comments(openai)
    sig_code = strip_kotlin_comments(signals)
    escapable = ESCAPE in sig_code

    status_fns = sorted({callee for callee, _raw in map_http_first_args(openai)
                         if callee})
    declared = top_level_int_funs(signals)
    # A "weight" is a derivation the PROVIDER must call: a declared function that
    # the signals module does not itself call (an internal helper like the family
    # table is reachable only through the status derivation).
    weight_fns = sorted({
        name for name in declared
        if name not in status_fns
        and len(re.findall(r"\b%s\s*\(" % re.escape(name), sig_code)) <= 1
    })

    guard_counters = re.findall(r"([A-Za-z_]\w*)\s*>\s*0\b", C3_OPENAI_GUARD)
    feeds = {}
    for name in guard_counters:
        feeds[name] = re.findall(r"\b%s\s*\+=\s*([A-Za-z_]\w*)\s*\(" % name, ok_code)
    call_fed = {name: callees for name, callees in feeds.items() if callees}
    wired_counter = None
    for name, callees in sorted(call_fed.items()):
        if len(callees) >= C8_COUNTER_VISIBILITY and set(callees) <= set(declared):
            wired_counter = name
            break

    if not escapable:
        if wired_counter is None:
            problems.append((
                "openai-count-covers-both-flavours", OPENAI,
                line_of(openai, "[RC2-truncated-detection]"),
                "the EOF guard's counters (%s) are not fed from %d+ sites through the "
                "shared pure derivations in OpenAiStreamSignals — a flavour that stops "
                "feeding the counter silently loses the truncated-turn signal "
                "(found: %s)"
                % (", ".join(guard_counters), C8_COUNTER_VISIBILITY,
                   {k: v for k, v in feeds.items()})))
        else:
            callees = set(feeds[wired_counter])
            missing = sorted(set(weight_fns) - callees)
            extra = sorted(callees - set(weight_fns))
            if missing:
                problems.append((
                    "openai-count-covers-both-flavours", OPENAI,
                    line_of(openai, "[RC2-truncated-detection]"),
                    "these pure weight derivations are declared in the signals module but "
                    "never fed into '%s': %s — a flavour lost its wire-up"
                    % (wired_counter, ", ".join(missing))))
            if extra:
                problems.append((
                    "openai-count-covers-both-flavours", OPENAI,
                    line_of(openai, "[RC2-truncated-detection]"),
                    "'%s' is fed through functions that are not declared in "
                    "OpenAiStreamSignals (%s) — the derivation is no longer single-source"
                    % (wired_counter, ", ".join(extra))))

        # C9 — one status derivation, reachable from both in-stream error arms.
        if len(status_fns) != 1:
            problems.append((
                "openai-stream-error-single-source", OPENAI,
                line_of(openai, "mapHttpError("),
                "expected exactly one status derivation passed to mapHttpError(, found %s — "
                "an in-stream error path is deciding its own family"
                % (status_fns or "none")))
        elif status_fns[0] not in declared:
            problems.append((
                "openai-stream-error-single-source", OPENAI,
                line_of(openai, "mapHttpError("),
                "%s is not a pure top-level function of the signals module" % status_fns[0]))
        else:
            fn = status_fns[0]
            body_start = sig_code.find("internal fun %s(" % fn)
            body = sig_code[body_start:]
            for token, why in ((C8_ERROR_STATUS_TOKEN, "the rate-limit family"),
                               (C8_ERROR_TRANSIENT_TOKEN, "the transient family")):
                if token not in body:
                    problems.append((
                        "openai-stream-error-single-source", SIGNALS,
                        line_of(signals, "internal fun %s(" % fn),
                        "%s does not map %s (%s)" % (fn, token, why)))
            uses = len(re.findall(r"\b%s\s*\(" % re.escape(fn), ok_code))
            if uses < 2:
                problems.append((
                    "openai-stream-error-single-source", OPENAI,
                    line_of(openai, "mapHttpError("),
                    "%s is referenced %d time(s) in the provider — both in-stream error "
                    "arms (chat-completions inline error AND response.failed) must use it"
                    % (fn, uses)))
            failed_arm = ok_code.find('"response.failed" ->')
            if failed_arm >= 0:
                arm = ok_code[failed_arm:failed_arm + 4000]
                close = arm.find("\n                        type ==")
                arm = arm[:close] if close > 0 else arm
                if fn not in arm:
                    problems.append((
                        "openai-stream-error-single-source", OPENAI,
                        line_of(openai, '"response.failed" ->'),
                        "the response.failed arm does not route through %s — a symbolic "
                        "rate-limit code must not be hand-classified into TransientError"
                        % fn))
            elif ok_code.count('"response.failed"') < 1:
                problems.append((
                    "openai-stream-error-single-source", OPENAI, 0,
                    "the Responses terminal-failure arm is gone"))

    # ------------------------------------------------------------------
    # C10 — [dv29 A-01] Anthropic's EOF guard counts tool-call activity, so
    # EVERY tool-lifecycle arm has to feed that counter. It used to be fed by
    # the argument deltas alone: a pure-tool turn (contentChars == 0) cut
    # between the `tool_use` block announcement and the first non-empty delta
    # therefore looked like a completely empty stream, the orphan flush sent
    # `ToolCallComplete(id, name, {})`, and `turnTruncated` stayed false so the
    # executor ran an argument-less call. The Gemini and OpenAI arms of this
    # same family already had this shape (C6/C8) — this is the sibling that was
    # only PINNED, never checked.
    #
    # All four criteria are derived: the arms come from the protocol labels,
    # the counter is read out of the pinned guard, the weights are whatever
    # pure Int functions the signals module declares. Renaming keeps the gate
    # working; dropping a feed, or feeding a non-tool block header, does not.
    # ------------------------------------------------------------------
    anth_code = strip_kotlin_comments(anthropic)
    anth_sig_code = strip_kotlin_comments(anthropic_signals)
    anth_escapable = ESCAPE in anth_sig_code
    if (not anth_escapable) and anth_code.count(C3_ANTHROPIC_GUARD) == 1:
        tool_counter = anthropic_tool_counter(anth_code)
        branches = anthropic_lifecycle_branches(anth_code)
        if branches is None:
            problems.append((
                "anthropic-tool-lifecycle-branches", ANTHROPIC,
                line_of(anth_code, '"content_block_start" ->'),
                "the SSE lifecycle arms (%s) cannot be located — a reordered or "
                "renamed `when (eventType)` would blind the feed criteria below"
                % ", ".join(ANTHROPIC_LIFECYCLE_LABELS)))
        elif tool_counter is not None:
            start = branches['"content_block_start" ->']
            stop = branches['"content_block_stop" ->']
            delta = branches['"content_block_delta" ->']

            # (a) the block announcement is already progress, but ONLY inside
            #     the tool_use guard: counting a text/thinking block header
            #     would steal the fully-empty case from
            #     failOnSilentEmptyCompletion.
            token_at = start.find(ANTHROPIC_TOOL_USE_TOKEN)
            if token_at < 0:
                problems.append((
                    "anthropic-tool-start-feeds-counter", ANTHROPIC,
                    line_of(anth_code, '"content_block_start" ->'),
                    "the content_block_start arm no longer tests %s — the tool block "
                    "announcement cannot feed '%s' any more"
                    % (ANTHROPIC_TOOL_USE_TOKEN, tool_counter)))
            else:
                close = start.find(")", token_at)
                body_open = start.find("{", close) if close >= 0 else -1
                writes = counter_writes(start, tool_counter)
                guarded = [w for w in writes
                           if body_open >= 0 and w > body_open
                           and "{" not in start[body_open + 1:w]
                           and "}" not in start[body_open + 1:w]]
                if not guarded:
                    problems.append((
                        "anthropic-tool-start-feeds-counter", ANTHROPIC,
                        line_of(anth_code, '"content_block_start" ->'),
                        "the tool_use block announcement must feed '%s' (%s) — a "
                        "pure-tool turn cut before its first argument delta would "
                        "otherwise read as an empty stream, and the orphan flush "
                        "would hand an argument-less call to the executor%s"
                        % (tool_counter, ", ".join(
                            ["%s += <weight>()" % tool_counter]),
                           "" if not writes else "; it is written OUTSIDE the "
                           "tool_use guard, so a text/thinking block header would "
                           "be counted as tool activity")))

            # (b) the block completion is progress too: a zero-argument call
            #     that finished streaming still must be flagged when the stream
            #     dies before message_delta.
            if not counter_writes(stop, tool_counter):
                problems.append((
                    "anthropic-tool-stop-feeds-counter", ANTHROPIC,
                    line_of(anth_code, '"content_block_stop" ->'),
                    "the content_block_stop arm completes a tool call but never "
                    "feeds '%s' — a zero-argument call cut before message_delta "
                    "survives as a clean no-finish EOF" % tool_counter))

            # (c) regression guard on the one feed that always existed.
            if not counter_writes(delta, tool_counter):
                problems.append((
                    "anthropic-tool-delta-feeds-counter", ANTHROPIC,
                    line_of(anth_code, '"content_block_delta" ->'),
                    "the content_block_delta arm no longer feeds '%s' — the "
                    "pre-existing mid-argument truncation signal is gone"
                    % tool_counter))

            # (d) single source: every feed goes through a pure weight declared
            #     in the signals module, and every declared weight is used.
            declared = top_level_int_funs(anth_sig_code)
            callees = set(re.findall(
                r"\b%s\b\s*\+=\s*([A-Za-z_]\w*)\s*\(" % re.escape(tool_counter),
                anth_code))
            if not callees:
                problems.append((
                    "anthropic-tool-weights-single-source", ANTHROPIC,
                    line_of(anth_code, C3_ANTHROPIC_GUARD),
                    "'%s' is fed by raw arithmetic instead of the pure weights in "
                    "AnthropicStreamSignals — the decision table is then untestable "
                    "and each arm can drift" % tool_counter))
            else:
                missing = sorted(set(declared) - callees)
                extra = sorted(callees - set(declared))
                if missing:
                    problems.append((
                        "anthropic-tool-weights-single-source", ANTHROPIC,
                        line_of(anth_code, C3_ANTHROPIC_GUARD),
                        "these weights are declared in AnthropicStreamSignals but never "
                        "fed into '%s': %s — a lifecycle arm lost its wire-up"
                        % (tool_counter, ", ".join(missing))))
                if extra:
                    problems.append((
                        "anthropic-tool-weights-single-source", ANTHROPIC,
                        line_of(anth_code, C3_ANTHROPIC_GUARD),
                        "'%s' is fed through functions that AnthropicStreamSignals does "
                        "not declare (%s) — the derivation is no longer single-source"
                        % (tool_counter, ", ".join(extra))))

    return problems


def selftest(root):
    files = load(root)
    if files is None:
        return 1
    policy, engine, openai, anthropic, tool_executors, gemini, signals, \
        anthropic_signals = files

    def on(i, fn):
        # Positional mutator: load() returns 7 texts, and the arms must keep
        # addressing them by position so adding a file cannot silently shift
        # which mutation targets which file.
        def wrapper(*args):
            out = list(args)
            out[i] = fn(out[i])
            return tuple(out)
        return wrapper

    def strip_guard_from_error_branch(s):
        # delete the FIRST guard call (the error-shaped site) → count drops to 1
        i = s.find(C5_SHARED_GUARD)
        if i < 0:
            return s + "\n// sentinel-missing\n"
        return s[:i] + s[i + len(C5_SHARED_GUARD):]

    def drop_subagent_refusal(s):
        i = s.find(C4_REFUSAL_GATE)
        if i < 0:
            return s + "\n// sentinel-missing\n"
        return s.replace(C4_REFUSAL_GATE, "if (false) {", 1)

    arms = [
        ("policy loses the flag parameter", "policy-flag-signature",
         on(0, lambda s: s.replace(C1_SIGNATURE,
                                       "fun rejectionReason(toolName: String, finishReason: String?): String?", 1))),
        ("policy ignores the flag", "policy-flag-condition",
         on(0, lambda s: s.replace(C1_CONDITION, "if (!isTruncatedFinish(finishReason)) return null", 1))),
        ("engine passes a false constant", "engine-feeds-flag",
         on(1, lambda s: s.replace(C2_ENGINE_CALL,
                                       "TruncatedToolCallPolicy.rejectionReason(name, turnFinishReason, false)", 1))),
        ("openai eof guard forgets tool calls", "openai-eof-counts-toolcalls",
         on(2, lambda s: s.replace(C3_OPENAI_GUARD,
                                       "if (!finishedSent && (contentLen > 0 || reasoningLen > 0)) {", 1))),
        ("anthropic sibling guard regresses", "anthropic-sibling-pin",
         on(3, lambda s: s.replace(C3_ANTHROPIC_GUARD,
                                          "if (!sawClearFinish && (contentChars > 0)) {", 1))),
        ("subagent loop drops the refusal gate", "subagent-refusal-gate",
         on(4, drop_subagent_refusal)),
        ("subagent loop skips the policy", "subagent-refusal-wired",
         on(4, lambda s: s.replace(C4_POLICY_CALL, "TruncatedToolCallPolicy.isTruncatedFinish(", 1))),
        ("subagent loop hand-writes the wording", "subagent-single-source",
         on(4, lambda s: s.replace(
             "log(\"[Subagent] turn=$turns truncated tool call REFUSED",
             "log(\"[TRUNCATED] " + "\"[Subagent] turn=$turns truncated tool call REFUSED", 1))),
        ("subagent refusal breaks the pairing", "subagent-refusal-pairing",
         on(4, lambda s: s.replace(C4_PAIRING,
                                               "content = truncatedRefusal, isError = false", 1))),
        ("error-shaped branch loses the stacking guard", "stack-guard-shared",
         on(1, strip_guard_from_error_branch)),
        ("gemini EOF reverts to the fabricated end_turn",
         "gemini-eof-terminal-pure+gemini-eof-passthrough",
         on(5, lambda s: s.replace(C6_GEMINI_SEND,
                                       'send(LLMStreamChunk.Finished(lastFinishReason ?: "end_turn"))', 1))),
        ("gemini pure verdict invents a stop reason", "gemini-eof-passthrough",
         on(5, lambda s: s.replace(
             C6_GEMINI_PURE_ELSE,
             'GeminiEofTerminal(stopReason = lastFinishReason ?: "end_turn", truncated = false)', 1))),
        ("eof-stub branch back to an inline predicate", "stack-guard-no-inline-twin",
         on(1, lambda s: s.replace(
             "                        if (EofStubReminderStackGuard.isStaleEofStubReminder(prevTail)) {",
             "                        val tailIsEofStubReminder = prevTail != null &&\n"
             "                        if (EofStubReminderStackGuard.isStaleEofStubReminder(prevTail)) {", 1))),
        # ---- [dv28 A-01 / A-08] derived criteria arms -------------------
        ("responses arm stops feeding the EOF counter",
         "openai-count-covers-both-flavours",
         on(2, lambda s: s.replace(RESPONSES_FEED, "", 1))),
        ("chat arm stops feeding the EOF counter",
         "openai-count-covers-both-flavours",
         on(2, lambda s: s.replace(CHAT_FEED, "", 1))),
        ("chat inline error coerces the symbolic code to 0",
         "openai-stream-error-single-source",
         on(2, lambda s: s.replace(CHAT_INLINE_STATUS, "inlineError.optInt(\"code\", 0)", 1))),
        ("responses rate limit hand-classified as transient",
         "openai-stream-error-single-source",
         on(2, lambda s: s.replace(
             RESPONSES_STATUS_CALL,
             'if (code == "server_error" || code == "rate_limit_exceeded") {\n'
             '                                throw LLMError.TransientError("[$code] $message")\n'
             '                            }\n'
             '                            throw LLMError.ProviderError("[$code] $message")', 1))),
        # ---- [dv29 A-01] Anthropic tool-lifecycle feed arms --------------
        # Expectations are hand-written from the criterion table above: an arm
        # that silently stops mutating anything must show up as "expected X got
        # ()" instead of a false green.
        ("anthropic block announcement stops feeding the tool counter",
         "anthropic-tool-start-feeds-counter+anthropic-tool-weights-single-source",
         on(3, lambda s: s.replace(ANTHROPIC_START_FEED, "", 1))),
        ("anthropic block completion stops feeding the tool counter",
         "anthropic-tool-stop-feeds-counter+anthropic-tool-weights-single-source",
         on(3, lambda s: s.replace(ANTHROPIC_STOP_FEED, "", 1))),
        ("anthropic announcement feed moves outside the tool_use guard",
         "anthropic-tool-start-feeds-counter",
         on(3, lambda s: s.replace(
             ANTHROPIC_START_ANCHOR,
             'val contentBlock = event.optJSONObject("content_block")\n'
             '                        toolChars += toolUseStartActivityChars("", "")\n'
             '                        if (contentBlock?.safeOptString("type", "") == "tool_use") {',
             1).replace(ANTHROPIC_START_FEED + "\n", "", 1))),
        ("anthropic argument feed reverts to raw arithmetic",
         "anthropic-tool-weights-single-source",
         on(3, lambda s: s.replace(ANTHROPIC_DELTA_FEED,
                                        "toolChars += partial.length", 1))),
    ]

    failed = []
    for name, criterion, mutate in arms:
        mutated = mutate(*files)
        found = check(*mutated)
        fired = sorted(set(c for c, _f, _l, _m in found))
        expected = sorted(criterion.split("+"))
        if fired != expected:
            failed.append("%s: expected %s got %s" % (name, expected, fired))
    # clean tree must be fully green
    if check(*files):
        failed.append("clean tree unexpectedly flagged: %s" % (check(*files)[:2],))
    if failed:
        print("❌ truncated-call gate --selftest failures:")
        for line in failed:
            print("   - %s" % line)
        return 1
    print("✅ truncated-call gate --selftest: %d arms flip exactly their criterion" % len(arms))
    print(SELFTEST_MARK)
    return 0


def main():
    if "--selftest" in sys.argv:
        root = next((a for a in sys.argv[1:] if not a.startswith("-")), ".")
        return selftest(root)
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    files = load(root)
    if files is None:
        return 1
    problems = check(*files)
    if problems:
        print("❌ truncated-call gate findings:")
        for criterion, rel, line, msg in problems:
            print("   - [%s] %s:%d — %s" % (criterion, rel, line, msg))
        return 1
    print("✅ truncated-call gate: policy reads both truncation sources, the engine and the "
          "sub-agent loop feed it, OpenAI's EOF guard counts tool-call events, both "
          "stub-reminder injection sites share one stacking guard, and Gemini's EOF "
          "terminal decision goes through the pure verdict without inventing a stop reason")
    return 0


if __name__ == "__main__":
    sys.exit(main())

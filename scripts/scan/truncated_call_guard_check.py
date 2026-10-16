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

Behaviour is covered by unit tests (TruncatedCallGuardTest /
EofStubStackingGuardTest); this gate pins the WIRING, because none of the four
sites can execute without the Android runtime.

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

ESCAPE = "truncated-guard-ok:"
SELFTEST_MARK = "TRUNCATED_CALL_GUARD_SELFTEST=PASS"


def line_of(text, needle):
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else 0


def load(root):
    paths = [POLICY, ENGINE, OPENAI, ANTHROPIC, TOOL_EXECUTORS]
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


def check(policy, engine, openai, anthropic, tool_executors):
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

    return problems


def selftest(root):
    files = load(root)
    if files is None:
        return 1
    policy, engine, openai, anthropic, tool_executors = files

    def on_policy(fn):
        return lambda p, e, o, a, t: (fn(p), e, o, a, t)

    def on_engine(fn):
        return lambda p, e, o, a, t: (p, fn(e), o, a, t)

    def on_openai(fn):
        return lambda p, e, o, a, t: (p, e, fn(o), a, t)

    def on_anthropic(fn):
        return lambda p, e, o, a, t: (p, e, o, fn(a), t)

    def on_tool_executors(fn):
        return lambda p, e, o, a, t: (p, e, o, a, fn(t))

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
         on_policy(lambda s: s.replace(C1_SIGNATURE,
                                       "fun rejectionReason(toolName: String, finishReason: String?): String?", 1))),
        ("policy ignores the flag", "policy-flag-condition",
         on_policy(lambda s: s.replace(C1_CONDITION, "if (!isTruncatedFinish(finishReason)) return null", 1))),
        ("engine passes a false constant", "engine-feeds-flag",
         on_engine(lambda s: s.replace(C2_ENGINE_CALL,
                                       "TruncatedToolCallPolicy.rejectionReason(name, turnFinishReason, false)", 1))),
        ("openai eof guard forgets tool calls", "openai-eof-counts-toolcalls",
         on_openai(lambda s: s.replace(C3_OPENAI_GUARD,
                                       "if (!finishedSent && (contentLen > 0 || reasoningLen > 0)) {", 1))),
        ("anthropic sibling guard regresses", "anthropic-sibling-pin",
         on_anthropic(lambda s: s.replace(C3_ANTHROPIC_GUARD,
                                          "if (!sawClearFinish && (contentChars > 0)) {", 1))),
        ("subagent loop drops the refusal gate", "subagent-refusal-gate",
         on_tool_executors(drop_subagent_refusal)),
        ("subagent loop skips the policy", "subagent-refusal-wired",
         on_tool_executors(lambda s: s.replace(C4_POLICY_CALL, "TruncatedToolCallPolicy.isTruncatedFinish(", 1))),
        ("subagent loop hand-writes the wording", "subagent-single-source",
         on_tool_executors(lambda s: s.replace(
             "log(\"[Subagent] turn=$turns truncated tool call REFUSED",
             "log(\"[TRUNCATED] " + "\"[Subagent] turn=$turns truncated tool call REFUSED", 1))),
        ("subagent refusal breaks the pairing", "subagent-refusal-pairing",
         on_tool_executors(lambda s: s.replace(C4_PAIRING,
                                               "content = truncatedRefusal, isError = false", 1))),
        ("error-shaped branch loses the stacking guard", "stack-guard-shared",
         on_engine(strip_guard_from_error_branch)),
        ("eof-stub branch back to an inline predicate", "stack-guard-no-inline-twin",
         on_engine(lambda s: s.replace(
             "                        if (EofStubReminderStackGuard.isStaleEofStubReminder(prevTail)) {",
             "                        val tailIsEofStubReminder = prevTail != null &&\n"
             "                        if (EofStubReminderStackGuard.isStaleEofStubReminder(prevTail)) {", 1))),
    ]

    failed = []
    for name, criterion, mutate in arms:
        mutated = mutate(*files)
        found = check(*mutated)
        fired = sorted(set(c for c, _f, _l, _m in found))
        if fired != [criterion]:
            failed.append("%s: expected [%s] got %s" % (name, criterion, fired))
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
          "sub-agent loop feed it, OpenAI's EOF guard counts tool-call events, and both "
          "stub-reminder injection sites share one stacking guard")
    return 0


if __name__ == "__main__":
    sys.exit(main())

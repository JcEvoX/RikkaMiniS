#!/usr/bin/env python3
"""
Compact-pin truth gate (deep-audit-1018 ISSUE-07).

Two properties of the compaction domain that failed silently in production
before [fix/da1018-compact-pin]:

  1. THE PIN COLLECTOR DE-DUPLICATES WITHIN ONE MESSAGE.
     Production USER messages are DOUBLE-FILLED: the main send path puts the
     trimmed text in BOTH `content` and the first `AgentContentPart.Text`
     (ChatViewModel 3713-3714), and every engine-side reminder mirrors it
     (AgentLoopEngine, ChatToolExecutors, EmptyTurnRecovery,
     SanitizeAgentHistory, ChatCompactionLogic :497). The pre-fix collector
     read both copies into the pin list, so every user line was pinned twice:
     the PINNED_USER_MESSAGES_TOKEN_LIMIT budget was consumed at 2x the real
     content (the over-cap walk dropped older real messages early) and the
     summary string that rides every request carried each text twice.
     The fix de-duplicates within a message ONLY — it must keep reading BOTH
     sources, because parts carry text `content` does not (per-image captions,
     "[image ...]" placeholders, the attached-files XML). A content-only or
     parts-only shortcut is a regression this gate also catches.

  2. THE "TOOL-RESULT CARRIER" TEST HAS A SINGLE TRUTH SOURCE.
     `LLMMessage.isToolResultOnly()` (emptiness-guarded: non-empty AND all
     ToolResult) is the predicate. Two sites had hand-rolled the same test in
     the opposite polarity and both evaluated the empty-`contentParts` row —
     the most common prompt shape — the WRONG way:
       * resolveCompactAnchorIdx sole-prompt guard
         `contentParts.any { it !is ToolResult }` — vacuously FALSE on empty,
         so a sole plain-text instruction did NOT abort compaction;
       * injectSummaryIntoPostAnchor injection target
         `!contentParts.all { it is ToolResult }` — also FALSE on empty, so
         the plain instruction never received the inlined summary and it
         landed as a standalone head.
     Both are now `!isToolResultOnly()`. This gate forbids ANY bare
     `contentParts.any/all { ... ToolResult ... }` outside the isToolResultOnly
     definition itself, in CODE (comments are stripped first).

Mechanical criteria:
  1. inside `extractPinnedUserMessages` (CompactPin.kt): a same-message
     de-dup construct is present (LinkedHashSet / distinct() / toSet(), or a
     `seen*` set that is both declared and added to);
  2. the same body still reads BOTH text sources (`msg.content` AND
     `AgentContentPart.Text` parts);
  3. in ChatCompactionLogic.kt code: no `contentParts.any/all { ... }`
     lambda testing `ToolResult` outside the `isToolResultOnly` definition.

Usage: python3 scripts/scan/compact_pin_truth_check.py [repo_root]
       python3 scripts/scan/compact_pin_truth_check.py --selftest
Exit:  0 = guarded, 1 = violation (file:line printed)

Known limits (deliberate): comment/string stripping handles //, /* */ and
double-quoted strings — Kotlin char and triple-quoted string literals are not
special-cased (none occur in the guarded files). The de-dup construct check is
structural, not behavioural: proof that de-dup actually happens lives in
CompactPinDedupTest / ChatCompactionLogicTruthTableTest (JVM) plus the
mutation controls in the fix report.
"""
import os
import re
import sys

MAIN = "src/android/app/src/main/java/com/rikkaminis/app"
PIN_FILE = MAIN + "/conversation/CompactPin.kt"
LOGIC_FILE = MAIN + "/ui/chat/ChatCompactionLogic.kt"
SELFTEST_MARK = "COMPACT_PIN_TRUTH_SELFTEST=PASS"


def strip_comments(text):
    """Blank out // and /* */ comments while respecting double-quoted string
    literals. Returns [(lineno, code), ...] — line numbers are preserved."""
    out = []
    in_block = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        code = ""
        i = 0
        n = len(raw)
        while i < n:
            c = raw[i]
            if in_block:
                if raw.startswith("*/", i):
                    in_block = False
                    i += 2
                else:
                    i += 1
                continue
            if c == '"':
                j = i + 1
                while j < n:
                    if raw[j] == "\\":
                        j += 2
                        continue
                    if raw[j] == '"':
                        break
                    j += 1
                code += raw[i:j + 1]
                i = j + 1
                continue
            if raw.startswith("//", i):
                break  # rest of the line is a comment
            if raw.startswith("/*", i):
                in_block = True
                i += 2
                continue
            code += c
            i += 1
        out.append((lineno, code))
    return out


def extract_function_body(code_lines, name):
    """Top-level fun `name` body: from its declaration line to the next
    column-0 `}`. Returns [(lineno, code), ...] or []."""
    decl = re.compile(r"\bfun\s+" + re.escape(name) + r"\s*\(")
    body = []
    inside = False
    for lineno, line in code_lines:
        if not inside:
            if decl.search(line):
                inside = True
            continue
        if line.rstrip() == "}":  # column-0 closing brace of the top-level fun
            break
        body.append((lineno, line))
    return body


def check_pin(text):
    """Criteria 1 + 2 against CompactPin.kt."""
    problems = []
    code = strip_comments(text)
    body = extract_function_body(code, "extractPinnedUserMessages")
    if not body:
        problems.append(("pin-collector-found",
                         "%s: extractPinnedUserMessages body not found — anchor moved, "
                         "update this gate" % PIN_FILE))
        return problems
    body_text = "\n".join(line for _, line in body)

    has_set_dedup = bool(re.search(r"LinkedHashSet|\.distinct\(\)|\.toSet\(\)", body_text))
    has_seen_set = bool(re.search(r"\bval\s+seen[A-Za-z0-9_]*\s*[:=]", body_text)) and ".add(" in body_text
    if not (has_set_dedup or has_seen_set):
        problems.append(("pin-dedup",
                         "%s:%d: extractPinnedUserMessages collects user text with NO "
                         "same-message de-duplication — production messages are "
                         "double-filled (content == first Text part), so every line "
                         "would pin twice and eat the token cap at 2x real content"
                         % (PIN_FILE, body[0][0])))

    reads_content = re.search(r"\.content\b", body_text) is not None
    reads_text_parts = "AgentContentPart.Text" in body_text
    if not (reads_content and reads_text_parts):
        problems.append(("pin-both-sources",
                         "%s:%d: extractPinnedUserMessages stopped reading BOTH text "
                         "sources (needs `msg.content` AND `AgentContentPart.Text` "
                         "parts) — a content-only/parts-only shortcut loses captions, "
                         "[image ...] placeholders or the attached-files XML"
                         % (PIN_FILE, body[0][0])))
    return problems


def check_logic(text):
    """Criterion 3 against ChatCompactionLogic.kt: the isToolResultOnly()
    definition is the only place allowed to test ToolResult polarity against
    contentParts inline."""
    problems = []
    code = strip_comments(text)
    bare = re.compile(r"contentParts\s*\.\s*(any|all)\s*(?:<[^\n{}]*>)?\s*\{")
    fun_re = re.compile(r"\bfun\s+(?:[A-Za-z0-9_.<>?]+\.)?([A-Za-z0-9_]+)\s*\(")
    cur_fun = None
    for lineno, line in code:
        m = fun_re.search(line)
        if m:
            cur_fun = m.group(1)
        hit = bare.search(line)
        if hit and "ToolResult" in line:
            if cur_fun != "isToolResultOnly":
                problems.append(("single-truth-source",
                                 "%s:%d: bare `contentParts.%s { ... ToolResult ... }` inside "
                                 "`%s` — use isToolResultOnly() (the single truth source; its "
                                 "empty-parts polarity is the da1018 fix)"
                                 % (LOGIC_FILE, lineno, hit.group(1), cur_fun)))
    return problems


def load(root):
    pin_path = os.path.join(root, PIN_FILE)
    logic_path = os.path.join(root, LOGIC_FILE)
    if not os.path.exists(pin_path):
        print("❌ compact-pin truth gate: %s not found" % pin_path)
        return None
    if not os.path.exists(logic_path):
        print("❌ compact-pin truth gate: %s not found" % logic_path)
        return None
    with open(pin_path, encoding="utf-8") as f:
        pin = f.read()
    with open(logic_path, encoding="utf-8") as f:
        logic = f.read()
    return pin, logic


def check(pin, logic):
    return check_pin(pin) + check_logic(logic)


# ─── selftest: each arm must flip exactly its criterion ─────────────────────

PIN_PREFIX = """package com.rikkaminis.app.conversation

import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage

const val PINNED_USER_MESSAGES_TOKEN_LIMIT: Long = 8000L

fun extractPinnedUserMessages(
    messages: List<LLMMessage>,
    tokenLimit: Long = PINNED_USER_MESSAGES_TOKEN_LIMIT,
): List<String> {
"""

LOGIC_PREFIX = """package com.rikkaminis.app.ui.chat

internal fun LLMMessage.isToolResultOnly(): Boolean =
    contentParts.isNotEmpty() && contentParts.all { it is AgentContentPart.ToolResult }

internal fun someSite(history: List<LLMMessage>): Int {
"""


def selftest(_root):
    arms = []
    # Arm 1 — pre-fix pin collector (criteria 1+2, but the discriminator for
    # criterion 1 is that BOTH direct adds have no dedup).
    pre_fix_pin = PIN_PREFIX + """    val texts = ArrayList<String>()
    for (msg in messages) {
        if (msg.role != LLMMessage.Role.USER) continue
        if (msg.content.isNotBlank()) texts.add(msg.content)
        for (part in msg.contentParts) {
            if (part is AgentContentPart.Text && part.text.isNotBlank()) texts.add(part.text)
        }
    }
    return texts
}
"""
    arms.append(("arm1 pre-fix collector flags pin-dedup", pre_fix_pin, LOGIC_PREFIX + "}\n",
                 [("pin-dedup")]))

    # Arm 2 — content-only shortcut WITH a dedup construct: dedup criterion is
    # satisfied but both-sources criterion must fire.
    content_only_pin = PIN_PREFIX + """    val seen = LinkedHashSet<String>()
    for (msg in messages) {
        if (msg.role != LLMMessage.Role.USER) continue
        if (msg.content.isNotBlank()) seen.add(msg.content)
    }
    return seen.toList()
}
"""
    arms.append(("arm2 content-only shortcut flags pin-both-sources", content_only_pin,
                 LOGIC_PREFIX + "}\n", [("pin-both-sources")]))

    # Arm 3 — post-fix shape (fixed collector + compliant logic) must be clean.
    fixed_pin = PIN_PREFIX + """    val texts = ArrayList<String>()
    for (msg in messages) {
        if (msg.role != LLMMessage.Role.USER) continue
        val seenInMessage = LinkedHashSet<String>()
        if (msg.content.isNotBlank()) seenInMessage.add(msg.content)
        for (part in msg.contentParts) {
            if (part is AgentContentPart.Text && part.text.isNotBlank()) seenInMessage.add(part.text)
        }
        texts.addAll(seenInMessage)
    }
    return texts
}
"""
    arms.append(("arm3 fixed shape is clean", fixed_pin, LOGIC_PREFIX + "}\n", []))

    # Arm 4 — bare !all { ToolResult } at the injection site (pre-fix :449).
    arm4_logic = LOGIC_PREFIX + """    val firstTextUserOffset = postAnchor.indexOfFirst {
        it.role == LLMMessage.Role.USER &&
            !it.contentParts.all { p -> p is AgentContentPart.ToolResult }
    }
    return 0
}
"""
    arms.append(("arm4 pre-fix :449 flags single-truth-source", fixed_pin, arm4_logic,
                 [("single-truth-source")]))

    # Arm 5 — bare any { !ToolResult } at the anchor site (pre-fix :124).
    arm5_logic = LOGIC_PREFIX + """    } else if (i == 0 &&
        history[0].role == LLMMessage.Role.USER &&
        history[0].contentParts.any { p -> p !is AgentContentPart.ToolResult } &&
        true
    ) {
        return -1
    }
    return 0
}
"""
    arms.append(("arm5 pre-fix :124 flags single-truth-source", fixed_pin, arm5_logic,
                 [("single-truth-source")]))

    # Arm 6 — comments quoting the old forms must NOT fire (stripper works).
    arm6_logic = LOGIC_PREFIX + """    // Was `contentParts.any { it !is ToolResult }` — vacuously false.
    /* and !it.contentParts.all { p -> p is AgentContentPart.ToolResult } */
    val ok = !history[0].isToolResultOnly()
    return if (ok) 0 else 0
}
"""
    arms.append(("arm6 commented-out old forms do not flag", fixed_pin, arm6_logic, []))

    failed = []
    for name, pin_src, logic_src, expected in arms:
        got = [c for c, _ in check(pin_src, logic_src)]
        if sorted(got) != sorted(expected):
            failed.append("%s (expected %s, got %s)" % (name, expected, got))

    if failed:
        print("❌ compact-pin truth gate --selftest:")
        for line in failed:
            print("   - %s" % line)
        return 1
    print("✅ compact-pin truth gate --selftest: %d arms flip exactly their criterion" % len(arms))
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
        print("❌ compact-pin truth gate findings:")
        for criterion, msg in problems:
            print("   - [%s] %s" % (criterion, msg))
        return 1
    print("✅ compact-pin truth gate: the pin collector de-duplicates within a message "
          "while still reading both text sources, and isToolResultOnly() is the only "
          "tool-result polarity test in the compaction domain")
    return 0


if __name__ == "__main__":
    sys.exit(main())

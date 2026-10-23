#!/usr/bin/env python3
"""deep-audit-1046 S-03 (P1) — Anthropic thinking-budget wiring gate.

## Why this gate exists

`thinking.budget_tokens` is defined RELATIVE to `max_tokens`: the API answers
`thinking.budget_tokens must be less than max_tokens` (a deterministic 400)
whenever the pair is illegal. `buildRequestBody` had two readers of two
different numbers:

    body.put("max_tokens", clampOutboundMaxTokens(maxTokens, effectiveMaxOutputTokens(model)))
    val budgetTokens = thinkingBudget(maxTokens, thinkingLevel)      // RAW, unclamped

Upstream `dynamicMaxTokens()` normally lands in range, so the split stayed
invisible on the happy path — but every out-of-band caller (sub-agent budget
frontmatter, synthesized requests — exactly the callers the clamp's own KDoc
says it defends) above the model ceiling emitted e.g. `max_tokens=8192` with
`budget_tokens=65536` and got a 400. Both numbers are individually "correct";
only the PAIR is illegal, which is why no test of either helper alone saw it.

The same block also had a two-way beta-header split:

    if (body.has("thinking")) { adaptive ? effort-2025-11-24 : interleaved-thinking-2025-05-14 }

`thinking.type="disabled"` is the explicit off-switch we send on adaptive
models (their default is to think — iOS ea86dd8a: max_tokens=256 burned
entirely on thinking_tokens, empty body). Sending it AND announcing a thinking
beta contradicts itself: harmless noise at api.anthropic.com, a 400 on relays
that validate the pairing.

## Criteria (all derived from the comment-stripped source, never from line numbers)

  C1  In the function that writes `max_tokens`, every `thinkingBudget(...)`
      argument is the SAME expression as the `max_tokens` value — one value,
      two readers. (Identical text counts for both spellings the task allows:
      a shared local, or the identical call expression.)
  C2  The `budget_tokens` write site is enclosed in an `if` whose condition
      constrains the budget variable from BOTH sides: `> 0` and `<` some
      other identifier. A budget of 0 or >= max_tokens is a 400, and
      `thinkingBudget()` alone cannot promise either (its `>= maxTokens`
      tighten only fires for `maxTokens > 1`).
  C3  The function that turns `body.has("thinking")` into beta flags reads
      `thinking.type` and distinguishes `"disabled"` (a `== "disabled"` /
      `!= "disabled"` comparison, or a `"disabled" ->` `when` branch). A
      whitelist that simply omits it is NOT accepted: the exclusion must be
      visible where the header is built.

Deliberate strictness (fail-closed): C2 requires the guard to be a
brace-bodied `if` in the same function as the write, and it requires the guard
to be VISIBLE AT THE WRITE SITE rather than trusting `thinkingBudget()`'s
internal tighten. Both are recorded as residuals in the fix report — a
refactor that keeps the contract in a different shape must edit this gate on
purpose rather than silently pass.

Usage: python3 scripts/scan/anthropic_thinking_budget_check.py [ROOT]
       python3 scripts/scan/anthropic_thinking_budget_check.py --selftest
Exit:  0 = clean, 1 = violation(s) or selftest failure, 2 = usage/ROT
"""
import os
import re
import sys

REL = "src/android/app/src/main/java/com/rikkaminis/app/provider/anthropic/AnthropicProvider.kt"
ROT_MARK = "class AnthropicProvider"

# ── Kotlin-aware source scanning ─────────────────────────────────────────────
# Criteria below are matched on comment-stripped text with string literals
# intact (several anchors ARE string literals: put("max_tokens", …)). Comments
# are blanked but their newlines kept, so every line number this gate prints is
# a real line in the original file.

def skip_string(text, i):
    """Index just past the string literal starting at `text[i]` (a quote)."""
    if text.startswith('"""', i):
        j = text.find('"""', i + 3)
        return len(text) if j < 0 else j + 3
    q = text[i]
    i += 1
    while i < len(text) and text[i] != q:
        if text[i] == "\\":
            i += 2
        elif text.startswith("${", i):
            i = skip_template(text, i + 2)
        else:
            i += 1
    return i + 1


def skip_template(text, i):
    """Index just past the `}` matching an already-consumed `${`."""
    depth = 1
    while i < len(text):
        c = text[i]
        if c == '"' or c == "'":
            i = skip_string(text, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def strip_comments(src):
    """Remove line + block comments, leaving string literals and newlines."""
    out = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            j = skip_string(src, i)
            out.append(src[i:j])
            i = j
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            end = n if j < 0 else j + 2
            out.append("\n" * src.count("\n", i, end))
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out)


def brace_body(text, start):
    """Balanced `{...}` starting at `start`; -> (body incl. braces, end index)."""
    depth = 0
    k = start
    while k < len(text):
        c = text[k]
        if c == '"' or c == "'":
            k = skip_string(text, k)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:k + 1], k + 1
        k += 1
    return text[start:], len(text)


def paren_body(text, start):
    """Balanced `(...)` starting at `start`; -> (body incl. parens, end index)."""
    depth = 0
    k = start
    while k < len(text):
        c = text[k]
        if c == '"' or c == "'":
            k = skip_string(text, k)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:k + 1], k + 1
        k += 1
    return text[start:], len(text)


FUN_HEAD_RE = re.compile(r"\bfun\s+(?:<[^>\n]*>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\(")


def iter_functions(text):
    """-> [(name, start, body)] for every block-bodied `fun` in `text`.

    Derived, not listed: no criterion below names the functions it polices, so
    renaming (or splitting) the request builder keeps the gate honest.
    """
    out = []
    for m in FUN_HEAD_RE.finditer(text):
        paren_at = text.find("(", m.end() - 1)
        _sig, after = paren_body(text, paren_at)
        brace_at = text.find("{", after - 1)
        if brace_at < 0:
            continue
        # A `)` followed by `=` / `:` before any `{` is an expression-bodied fun.
        gap = text[after:brace_at] if brace_at > after else ""
        if "=" in gap:
            continue
        body, _end = brace_body(text, brace_at)
        out.append((m.group(1), brace_at, body))
    return out


def split_top_level_args(text, open_paren_index):
    """Split the argument list of the call whose `(` is at `open_paren_index`.

    -> list of raw argument expressions (trimmed); nested calls/brackets safe.
    """
    args = []
    i = open_paren_index + 1
    depth = 0
    cur = []
    while i < len(text):
        c = text[i]
        if c == '"' or c == "'":
            j = skip_string(text, i)
            cur.append(text[i:j])
            i = j
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        if c == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        args.append(tail)
    return args


def find_calls(text, callee_re):
    """-> [(call_start, [args])] for every `<callee>(` in `text`."""
    out = []
    for m in callee_re.finditer(text):
        # The opening paren is the first one after the callee name (the regex
        # may end on a string literal, e.g. put("max_tokens", …), so searching
        # forward from m.end() would land on the NEXT call's paren).
        paren_at = text.find("(", m.start())
        if paren_at < 0:
            continue
        args = split_top_level_args(text, paren_at)
        out.append((m.start(), args))
    return out


MAX_TOKENS_CALL = re.compile(r"\bput\(\s*\"max_tokens\"")
BUDGET_CALL = re.compile(r"\bput\(\s*\"budget_tokens\"")
BUDGET_FN = re.compile(r"(?<![\w.])thinkingBudget\s*\(")
THINKING_HAS = re.compile(r"\bhas\(\s*\"thinking\"\s*\)")
GUARD_GT_ZERO = "\x00gt0"   # placeholder, real regex built per-variable below
IF_RE = re.compile(r"\bif\s*\(")
DISABLED_COMPARE = re.compile(
    r"""(?:==|!=)\s*"disabled"|"disabled"\s*(?:==|!=)|"disabled"\s*->"""
)


def norm(expr):
    return re.sub(r"\s+", "", expr)


def enclosing_ifs(func_body, write_index):
    """-> [condition text] of every brace-bodied `if` whose body holds write_index."""
    out = []
    for m in IF_RE.finditer(func_body):
        paren_at = func_body.find("(", m.end() - 1)
        cond, after = paren_body(func_body, paren_at)
        brace_at = func_body.find("{", after - 1)
        if brace_at < 0:
            continue
        body, end = brace_body(func_body, brace_at)
        if brace_at <= write_index < end:
            out.append(cond[1:-1])
    return out


def check_source(src):
    """-> [(criterion, ok, detail)] on comment-stripped source."""
    stripped = strip_comments(src)
    funcs = iter_functions(stripped)
    results = []

    # ── C1: one value, two readers ──────────────────────────────────────────
    c1_ok = True
    c1_detail = "no max_tokens writer found (update this gate deliberately)"
    checked_any = False
    for name, _start, body in funcs:
        max_calls = find_calls(body, MAX_TOKENS_CALL)
        if not max_calls:
            continue
        checked_any = True
        budget_calls = find_calls(body, BUDGET_FN)
        if len(max_calls) != 1:
            c1_ok = False
            c1_detail = "%s writes max_tokens %d times — cannot derive a single wire value" % (
                name, len(max_calls))
            continue
        max_expr = max_calls[0][1][-1] if max_calls[0][1] else ""
        if not budget_calls:
            c1_ok = False
            c1_detail = "no thinkingBudget(...) call in %s" % name
            continue
        for _pos, args in budget_calls:
            arg = args[0] if args else ""
            if norm(arg) != norm(max_expr):
                c1_ok = False
                c1_detail = "budget derives from `%s` but max_tokens writes `%s`" % (
                    arg.strip(), max_expr.strip())
    if not checked_any:
        c1_ok = False
    results.append((
        "C1 max_tokens and thinkingBudget read the same value",
        c1_ok, c1_detail,
    ))

    # ── C2: the budget write is guarded on both sides ───────────────────────
    c2_ok = True
    c2_detail = "no budget_tokens write site found (update this gate deliberately)"
    seen_write = False
    for name, _start, body in funcs:
        for pos, args in find_calls(body, BUDGET_CALL):
            seen_write = True
            value = args[-1].strip() if args else ""
            if not re.fullmatch(r"[A-Za-z_]\w*", value):
                c2_ok = False
                c2_detail = "budget_tokens written from a non-identifier `%s` — no guard can cover it" % value
                continue
            gt = re.compile(r"\b%s\s*>\s*0\b|\b0\s*<\s*%s\b" % (re.escape(value), re.escape(value)))
            lt = re.compile(r"\b%s\s*<\s*[A-Za-z_]\w*" % re.escape(value))
            conds = enclosing_ifs(body, pos)
            if not conds:
                c2_ok = False
                c2_detail = "budget_tokens written outside any brace-bodied if (%s)" % name
                continue
            if not any(gt.search(c) for c in conds):
                c2_ok = False
                c2_detail = "no `%s > 0` guard encloses the budget_tokens write" % value
            elif not any(lt.search(c) for c in conds):
                c2_ok = False
                c2_detail = "no `%s < <max>` guard encloses the budget_tokens write" % value
    if not seen_write:
        c2_ok = False
    results.append((
        "C2 budget_tokens is written only under `> 0` AND `< max`",
        c2_ok, c2_detail,
    ))

    # ── C3: the beta branch distinguishes thinking.type == "disabled" ───────
    c3_ok = True
    c3_detail = "no function turns body.has(\"thinking\") into beta flags (update this gate deliberately)"
    seen_header = False
    for name, _start, body in funcs:
        m = THINKING_HAS.search(body)
        if not m:
            continue
        seen_header = True
        tail = body[m.start():]
        if not re.search(r"\b(?:opt|get)String\(\s*\"type\"", tail):
            c3_ok = False
            c3_detail = "%s reads body.has(\"thinking\") without reading thinking.type" % name
            continue
        if not DISABLED_COMPARE.search(tail):
            c3_ok = False
            c3_detail = "%s does not distinguish thinking.type == \"disabled\"" % name
    if not seen_header:
        c3_ok = False
    results.append((
        "C3 beta flags distinguish thinking.type == \"disabled\"",
        c3_ok, c3_detail,
    ))

    return results


# ── selftest: one arm per criterion, each red exactly once ──────────────────

GREEN_BODY = '''
class AnthropicProvider {
    private fun buildRequestBody(messages: List<Int>, maxTokens: Int): JSONObject {
        val body = JSONObject()
        val cappedMaxTokens = clampOutboundMaxTokens(maxTokens, effectiveMaxOutputTokens(model))
        body.put("max_tokens", cappedMaxTokens)
        if (thinkingLevel.isEnabled) {
            if (modelUsesAdaptiveThinking(model.id)) {
                body.put("output_config", JSONObject().put("effort", thinkingEffort(thinkingLevel)))
            } else {
                val budgetTokens = thinkingBudget(cappedMaxTokens, thinkingLevel)
                if (budgetTokens > 0 && budgetTokens < cappedMaxTokens) {
                    body.put("thinking", JSONObject().apply {
                        put("type", "enabled")
                        put("budget_tokens", budgetTokens)
                    })
                }
            }
        }
        return body
    }

    private fun buildRequest(bodyStr: String, body: JSONObject): Request {
        val betaFlags = mutableListOf<String>()
        if (body.has("thinking")) {
            when (body.optJSONObject("thinking")?.optString("type")) {
                "adaptive" -> betaFlags.add("effort-2025-11-24")
                "enabled" -> betaFlags.add("interleaved-thinking-2025-05-14")
                "disabled" -> Unit
            }
        }
        return builder.build()
    }
}
'''

# Same-call-expression spelling the task explicitly allows: no shared local,
# the identical clamp expression on both sides.
GREEN_SAME_CALL_BODY = '''
class AnthropicProvider {
    private fun buildRequestBody(messages: List<Int>, maxTokens: Int): JSONObject {
        val body = JSONObject()
        body.put("max_tokens", clampOutboundMaxTokens(maxTokens, effectiveMaxOutputTokens(model)))
        val budgetTokens = thinkingBudget(clampOutboundMaxTokens(maxTokens, effectiveMaxOutputTokens(model)), thinkingLevel)
        if (budgetTokens > 0 && budgetTokens < maxTokens) {
            body.put("budget_tokens", budgetTokens)
        }
        return body
    }

    private fun buildRequest(bodyStr: String, body: JSONObject): Request {
        if (body.has("thinking")) {
            val t = body.optJSONObject("thinking")?.optString("type")
            if (t == "adaptive") betaFlags.add("effort-2025-11-24")
            if (t == "enabled") betaFlags.add("interleaved-thinking-2025-05-14")
            if (t != "disabled") sanity()
        }
        return builder.build()
    }
}
'''

# The exact baseline shape (f7aba218): all three criteria violated.
PRE_BODY = '''
class AnthropicProvider {
    private fun buildRequestBody(messages: List<Int>, maxTokens: Int): JSONObject {
        val body = JSONObject()
        body.put("max_tokens", clampOutboundMaxTokens(maxTokens, effectiveMaxOutputTokens(model)))
        if (thinkingLevel.isEnabled) {
            if (modelUsesAdaptiveThinking(model.id)) {
                body.put("output_config", JSONObject().put("effort", thinkingEffort(thinkingLevel)))
            } else {
                val budgetTokens = thinkingBudget(maxTokens, thinkingLevel)
                if (budgetTokens > 0) {
                    body.put("budget_tokens", budgetTokens)
                }
            }
        }
        return body
    }

    private fun buildRequest(bodyStr: String, body: JSONObject): Request {
        if (body.has("thinking")) {
            val isAdaptive = body.optJSONObject("thinking")?.optString("type") == "adaptive"
            if (isAdaptive) {
                betaFlags.add("effort-2025-11-24")
            } else {
                betaFlags.add("interleaved-thinking-2025-05-14")
            }
        }
        return builder.build()
    }
}
'''

# C1 only: budget still reads the raw request value.
MUT_C1_BODY = GREEN_BODY.replace(
    "thinkingBudget(cappedMaxTokens, thinkingLevel)",
    "thinkingBudget(maxTokens, thinkingLevel)",
)

# C2 only: guard drops the `< max` half (keeps `> 0`).
MUT_C2_BODY = GREEN_BODY.replace(
    "if (budgetTokens > 0 && budgetTokens < cappedMaxTokens)",
    "if (budgetTokens > 0)",
)

# C3 only: back to the two-way adaptive/else split (no "disabled" anywhere).
MUT_C3_BODY = GREEN_BODY.replace(
    '''            when (body.optJSONObject("thinking")?.optString("type")) {
                "adaptive" -> betaFlags.add("effort-2025-11-24")
                "enabled" -> betaFlags.add("interleaved-thinking-2025-05-14")
                "disabled" -> Unit
            }''',
    '''            val isAdaptive = body.optJSONObject("thinking")?.optString("type") == "adaptive"
            if (isAdaptive) {
                betaFlags.add("effort-2025-11-24")
            } else {
                betaFlags.add("interleaved-thinking-2025-05-14")
            }''',
)

# Fail-closed arm: budget written from a literal — no variable to guard.
MUT_LITERAL_BODY = GREEN_BODY.replace(
    "put(\"budget_tokens\", budgetTokens)",
    "put(\"budget_tokens\", 8192)",
)


def _crit_set(src):
    """-> {'C1','C2','C3'} of the criteria that FAIL on `src`."""
    failed = set()
    for crit, ok, _detail in check_source(src):
        if not ok:
            failed.add(crit.split()[0])
    return failed


def selftest():
    fails = []
    # Every expected set below is written from the criterion table above, never
    # derived from the gate's own output.
    arms = [
        ("compliant tree", GREEN_BODY, set()),
        ("compliant tree, same call expression on both sides", GREEN_SAME_CALL_BODY, set()),
        ("baseline (f7aba218) shape", PRE_BODY, {"C1", "C2", "C3"}),
        ("budget reads the raw request value", MUT_C1_BODY, {"C1"}),
        ("guard drops the `< max` half", MUT_C2_BODY, {"C2"}),
        ("beta branch back to two-way adaptive/else", MUT_C3_BODY, {"C3"}),
        ("budget written from a literal", MUT_LITERAL_BODY, {"C2"}),
    ]
    for label, src, expected in arms:
        got = _crit_set(src)
        if got != expected:
            fails.append("arm '%s': expected %s, got %s" % (
                label, sorted(expected) or "no violations", sorted(got) or "no violations"))
    # A mutation must actually change the text it claims to change.
    for label, mutated, base in (
        ("MUT_C1", MUT_C1_BODY, GREEN_BODY),
        ("MUT_C2", MUT_C2_BODY, GREEN_BODY),
        ("MUT_C3", MUT_C3_BODY, GREEN_BODY),
        ("MUT_LITERAL", MUT_LITERAL_BODY, GREEN_BODY),
    ):
        if mutated == base:
            fails.append("arm '%s' did not apply (string replace missed)" % label)
    return fails


def main():
    if "--selftest" in sys.argv:
        fails = selftest()
        for f in fails:
            print("  [FAIL] %s" % f)
        if fails:
            print("SELFTEST FAIL (%d)" % len(fails))
            return 1
        print("ANTHROPIC_THINKING_BUDGET_SELFTEST=PASS")
        return 0

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = args[0] if args else "."
    path = os.path.join(root, REL)
    if not os.path.isfile(path):
        print("FAIL %s not found under root %s" % (REL, root))
        return 2
    src = open(path, encoding="utf-8").read()
    if ROT_MARK not in src:
        print("FAIL %s does not look like the Anthropic provider (no '%s')" % (REL, ROT_MARK))
        return 2

    results = check_source(src)
    failed = [(c, d) for c, ok, d in results if not ok]
    for crit, ok, detail in results:
        if ok:
            print("  [OK] %s" % crit)
        else:
            print("  [FAIL] %s (%s)" % (crit, detail))
    if failed:
        print("anthropic thinking budget gate: RED — the S-03 pair contract is violated")
        return 1
    print("GATE=GREEN anthropic_thinking_budget (%d criteria)" % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())

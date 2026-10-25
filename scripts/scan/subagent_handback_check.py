#!/usr/bin/env python3
"""Sub-agent turn hand-back guard — [deep-audit-1017] FIX-C.

Bug class: `runSubagentLoop` (the spawn_agent loop) may hand a turn BACK to the
model (`continue`) instead of finishing. Two contracts apply to every such
hand-back, and nothing mechanical looked at either:

  1. **Echo** — the turn's VISIBLE text must enter `history` as an ASSISTANT
     message (`appendSubagentTurnText`) before the nudge/reminder is appended.
     `history` is the model's ONLY view of what it already wrote; a hand-back
     that drops it makes the model re-write the same prose on the retry, and
     the sub-agent's persisted result (`resultSb`, read by the main agent as
     its own text) then carries it twice. The residue-refill branch shipped
     without it (main loop's `AgentLoopEngine` has the same invariant).
  2. **Cancellation** — the loop's terminal `catch (…: Exception)` must be
     preceded by a `CancellationException` clause that rethrows, or a user stop
     is reclassified as "Sub-agent '<x>' encountered an error" (and the log
     blames the model). Same shape as the other executors in the file.

Criterion (scoped to `runSubagentLoop`'s body — the two contracts are about
that loop, not about every catch/continue in the codebase):
  C1: every `continue` token is preceded, since the previous `continue`, by
      `appendSubagentTurnText(history,` — or the enclosing hand-back carries an
      explicit `[no-assistant-echo: <reason>]` marker (a deliberate decision,
      not an oversight).
  C2: every `catch (…: Exception | Throwable)` clause is immediately preceded
      by a `catch (…: CancellationException)` clause whose body rethrows.

Deliberate non-goals: `catch (e: Exception)` around non-suspending work (e.g.
`persistBrowserArtifact`'s file IO) is NOT a cancellation hazard and is not
flagged; widening the rule repo-wide would bury real hits under that noise.

Usage: python3 scripts/scan/subagent_handback_check.py [repo_root] [--selftest]
Exit:  0 = contracts hold, 1 = violation (or the detector rotted)
"""
import os
import re
import sys

TARGET = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatToolExecutors.kt"
LOOP_DECL = "internal suspend fun runSubagentLoop("
ECHO_CALL = "appendSubagentTurnText(history,"
ECHO_EXEMPT_MARKER = "[no-assistant-echo:"
CANCELLATION = "CancellationException"
CATCH = re.compile(r"catch\s*\(\s*\w+\s*:\s*([\w.]+)\s*\)")
ECHO_ARGS = re.compile(r"appendSubagentTurnText\s*\(\s*history\s*,\s*([^,)]*)")
CONST_FALSE = re.compile(r"^\s*(?:false|0)\s*$")


def statement_prefix(masked, pos, limit=240):
    """Text from the start of the statement that contains pos."""
    depth = 0
    i = pos - 1
    while i >= 0 and pos - i < limit:
        c = masked[i]
        if c in ")]":
            depth += 1
        elif c in "([":
            depth -= 1
        elif depth == 0 and c in ";{}\n":
            break
        i -= 1
    return masked[i + 1: pos]


def dead_guarded(masked, pos):
    """True when the statement at pos sits under a CONSTANT-FALSE guard.

    Two shapes, both of which keep every call token and execute nothing:
    `if (false) call(…)` on one line and `if (false) { … call(…) … }`.
    """
    m = re.search(r"if\s*\(([^()]*)\)\s*[\w.\[\]]*$", statement_prefix(masked, pos))
    if m and CONST_FALSE.match(m.group(1)):
        return True
    depth = 0
    i = pos - 1
    while i >= 0:
        c = masked[i]
        if c in "}]":
            depth += 1
        elif c in "[{":
            if depth > 0:
                depth -= 1
            else:
                hdr = masked[max(0, i - 300): i]
                m = re.search(r"if\s*\(([^()]*)\)\s*$", hdr)
                if m and CONST_FALSE.match(m.group(1)):
                    return True
        i -= 1
    return False


def brace_span(text, open_idx):
    """(open, close) indices of the balanced {…} at open_idx, or None."""
    depth = 0
    i = open_idx
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return open_idx, i
        i += 1
    return None


def live_throw(block, pos):
    """True when the `throw` at pos can run: no constant-false guard above it and
    no unconditional `return` earlier at the same nesting level."""
    if dead_guarded(block, pos):
        return False
    depth_at = 0
    for ch in block[:pos]:
        if ch in "{":
            depth_at += 1
        elif ch in "}":
            depth_at -= 1
    depth = 0
    for m in re.finditer(r"\breturn\b", block[:pos]):
        depth = 0
        for ch in block[: m.start()]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        if depth != depth_at:
            continue
        if not re.search(r"if\s*\([^()]*\)\s*$", statement_prefix(block, m.start())):
            return False
    return True


def check_c1(raw, masked, start, end):
    viol = []
    hits = continues(masked[start:end])
    if not hits:
        return ["ROT: no `continue` found inside runSubagentLoop — the detector stopped matching"]
    bounds = [start] + [start + h + len("continue") for h in hits]
    for idx, h in enumerate(hits):
        pos = start + h
        window_raw = raw[bounds[idx]:pos]
        window_code = masked[bounds[idx]:pos]
        if ECHO_EXEMPT_MARKER in window_raw:
            continue
        if ECHO_CALL not in window_code:
            line = raw[:pos].count("\n") + 1
            viol.append(
                f"{TARGET}:{line} — `continue` hands the turn back without "
                f"{ECHO_CALL}…)` and without a `{ECHO_EXEMPT_MARKER}` marker"
            )
            continue
        # [wide-1024 12-misc-b] The call being present is not the contract: it has
        # to carry THIS turn's visible text and it has to run.
        #   `appendSubagentTurnText(history, "")`         -> the model still sees
        #       nothing it wrote and restates the prose (da1017 FIX-C verbatim)
        #   `if (false) appendSubagentTurnText(history, text)` -> same effect,
        #       with the call still spelled out for the gate.
        call = None
        for m in re.finditer(r"appendSubagentTurnText\s*\(", window_code):
            call = m
        line = raw[:pos].count("\n") + 1
        if call is None:
            continue
        tail = window_code[call.end() - 1:]
        arg2 = None
        m2 = re.match(r"\(\s*history\s*,\s*([^,)]*)", tail)
        if m2:
            arg2 = m2.group(1).strip()
        if not arg2:
            viol.append(
                f"{TARGET}:{line} — `appendSubagentTurnText(history, …)` passes an empty "
                "or literal turn text (the model never sees what it just wrote, so the "
                "retry restates it and the sub-agent's result carries the prose twice)"
            )
            continue
        if dead_guarded(window_code, call.start()):
            viol.append(
                f"{TARGET}:{line} — the echo call is dead-guarded (`if (false) …`) — it "
                "keeps the call and appends nothing, so the hand-back still drops the "
                "turn text"
            )
    return viol


def check_c2(raw, masked, start, end):
    viol = []
    body = masked[start:end]
    clauses = list(CATCH.finditer(body))
    if not clauses:
        return ["ROT: no catch clause found inside runSubagentLoop — the detector stopped matching"]
    for m in clauses:
        kind = m.group(1)
        if not (kind.endswith("Exception") or kind.endswith("Throwable")):
            continue
        if kind == CANCELLATION:
            continue
        prev = None
        for p in clauses:
            if p.end() <= m.start():
                prev = p
        ok = False
        if prev is not None and prev.group(1).endswith(CANCELLATION):
            brace = body.find("{", prev.end(), m.start())
            span = brace_span(body, brace) if brace >= 0 else None
            if span is not None:
                block = body[span[0]: span[1] + 1]
                # [wide-1024 12-misc-b] The rethrow must be LIVE. `if (false) throw
                # e` (then a fabricated result) matches `\bthrow\s+e\b` and swallows
                # the cooperative cancellation exactly like a missing clause: the
                # user's Stop turns into "Sub-agent '<x>' encountered an error".
                for t in re.finditer(r"\bthrow\b", block):
                    if live_throw(block, t.start()):
                        ok = True
                        break
        if not ok:
            line = raw[:start + m.start()].count("\n") + 1
            viol.append(
                f"{TARGET}:{line} — `catch (…: {kind})` swallows CancellationException "
                f"(no reachable `catch (…: {CANCELLATION}) {{ throw … }}` clause)"
            )
    return viol



def mask(src):
    """Length-preserving mask: comments + string literals → spaces.

    Indices stay valid against the raw text (the exemption markers live in
    comments, so they are read from the raw text at the same offsets).
    Newlines are preserved so line numbers survive.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
        elif c == '"':
            triple = src.startswith('"""', i)
            out[i] = " "
            i += 1
            if triple:
                out[i] = out[i + 1] = " "
                i += 2
            while i < n:
                if src[i] == "\\" and not triple:
                    if src[i] != "\n":
                        out[i] = " "
                    i += 1
                    if i < n:
                        out[i] = " "
                        i += 1
                    continue
                if triple and src.startswith('"""', i):
                    out[i] = out[i + 1] = out[i + 2] = " "
                    i += 3
                    break
                if not triple and src[i] == '"':
                    out[i] = " "
                    i += 1
                    break
                if src[i] == "\n" and not triple:
                    break
                if src[i] != "\n":
                    out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


def loop_body(raw):
    """(start, end) offsets of runSubagentLoop's body, or None."""
    at = raw.find(LOOP_DECL)
    if at < 0:
        return None
    at = raw.find("\n", at)
    end = raw.find("\n}\n", at)
    if end < 0:
        return None
    return at, end


def continues(masked):
    return [m.start() for m in re.finditer(r"\bcontinue\b", masked)]


def scan(root):
    path = os.path.join(root, TARGET)
    if not os.path.isfile(path):
        return ["ROT: missing " + TARGET]
    raw = open(path, encoding="utf-8", errors="replace").read()
    masked = mask(raw)
    span = loop_body(raw)
    if span is None:
        return ["ROT: runSubagentLoop not found in " + TARGET]
    start, end = span
    return check_c1(raw, masked, start, end) + check_c2(raw, masked, start, end)


# ── self-test fixtures ──────────────────────────────────────────────────────

GOOD = """package p
internal suspend fun runSubagentLoop() {
    try {
        while (true) {
            val text = "t"
            if (a) {
                appendSubagentTurnText(history, text)
                history.add(USER)
                continue
            }
            if (b) {
                appendSubagentTurnText(history, text)
                history.add(USER)
                continue
            }
            break
        }
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        return failure
    }
}
"""

MARKED = GOOD.replace("appendSubagentTurnText(history, text)\n                history.add(USER)\n                continue\n            }\n            break",
                      "// [no-assistant-echo: this arm carries no turn text]\n                continue\n            }\n            break")

NO_ECHO = GOOD.replace("appendSubagentTurnText(history, text)\n                history.add(USER)\n                continue\n            }\n            if (b)",
                       "history.add(USER)\n                continue\n            }\n            if (b)")

NO_CANCEL = GOOD.replace("    } catch (e: CancellationException) {\n        throw e\n", "")
NO_THROW = GOOD.replace("    } catch (e: CancellationException) {\n        throw e\n",
                        "    } catch (e: CancellationException) {\n        return quiet\n")
BAD_COMMENT_TRICK = GOOD.replace("appendSubagentTurnText(history, text)\n                history.add(USER)\n                continue\n            }\n            if (b)",
                                 "// appendSubagentTurnText(history,\n                continue\n            }\n            if (b)")


# [wide-1024 12-misc-b] witness shapes — the call/`throw` stays spelled out and
# the effect is gone.
ECHO_EMPTY = GOOD.replace("appendSubagentTurnText(history, text)",
                          'appendSubagentTurnText(history, "")', 1)
ECHO_DEAD = GOOD.replace("appendSubagentTurnText(history, text)",
                         "if (false) appendSubagentTurnText(history, text)", 1)
CANCEL_DEAD = GOOD.replace(
    "    } catch (e: CancellationException) {\n        throw e\n",
    "    } catch (e: CancellationException) {\n        if (false) throw e\n"
    "        return quiet\n",
)


def selftest():
    import tempfile
    cases = [
        ("compliant fixture", GOOD, 0, None),
        ("marker exemption accepted", MARKED, 0, None),
        ("missing echo call", NO_ECHO, 1, "hands the turn back without"),
        ("cancellation catch removed", NO_CANCEL, 1, "swallows CancellationException"),
        ("cancellation catch does not rethrow", NO_THROW, 1, "swallows CancellationException"),
        ("echo call only in a comment", BAD_COMMENT_TRICK, 1, "hands the turn back without"),
        ("echo passes an empty turn text", ECHO_EMPTY, 1,
         "passes an empty or literal turn text"),
        ("echo call dead-guarded", ECHO_DEAD, 1, "the echo call is dead-guarded"),
        ("cancellation rethrow dead-guarded", CANCEL_DEAD, 1,
         "swallows CancellationException"),
    ]
    bad = 0
    for name, src, want, needle in cases:
        root = tempfile.mkdtemp(prefix="handback-selftest-")
        path = os.path.join(root, TARGET)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(src)
        viol = scan(root)
        got = 1 if viol else 0
        flag = "ok " if got == want else "FAIL"
        if got != want or (needle and not any(needle in v for v in viol)):
            bad += 1
            flag = "FAIL"
        print(f"  [{flag}] {name}: exit={got} (want {want}) {viol[:1]}")
    if bad:
        print(f"SELFTEST FAILED ({bad}/{len(cases)})")
        return 1
    print(f"SELFTEST OK ({len(cases)} fixtures)")
    return 0


def main():
    argv = [a for a in sys.argv[1:] if a != "--selftest"]
    if "--selftest" in sys.argv:
        return selftest()
    root = os.path.abspath(
        argv[0] if argv else os.path.join(os.path.dirname(__file__), "..", "..")
    )
    viol = scan(root)
    if viol:
        print("Sub-agent hand-back contract violations:")
        for v in viol:
            print(f"  ✗ {v}")
        return 1
    path = os.path.join(root, TARGET)
    raw = open(path, encoding="utf-8", errors="replace").read()
    masked = mask(raw)
    start, end = loop_body(raw)
    n = len(continues(masked[start:end]))
    print(f"OK: runSubagentLoop's {n} hand-back(s) echo the turn text and rethrow cancellation")
    return 0


if __name__ == "__main__":
    sys.exit(main())

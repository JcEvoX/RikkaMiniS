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
        if ECHO_CALL in window_code or ECHO_EXEMPT_MARKER in window_raw:
            continue
        line = raw[:pos].count("\n") + 1
        viol.append(
            f"{TARGET}:{line} — `continue` hands the turn back without "
            f"{ECHO_CALL}…)` and without a `{ECHO_EXEMPT_MARKER}` marker"
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
            if brace >= 0 and re.search(r"\bthrow\b", body[brace:m.start()]):
                ok = True
        if not ok:
            line = raw[:start + m.start()].count("\n") + 1
            viol.append(
                f"{TARGET}:{line} — `catch (…: {kind})` swallows CancellationException "
                f"(no preceding `catch (…: {CANCELLATION}) {{ throw … }}` clause)"
            )
    return viol


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


def selftest():
    import tempfile
    cases = [
        ("compliant fixture", GOOD, 0),
        ("marker exemption accepted", MARKED, 0),
        ("missing echo call", NO_ECHO, 1),
        ("cancellation catch removed", NO_CANCEL, 1),
        ("cancellation catch does not rethrow", NO_THROW, 1),
        ("echo call only in a comment", BAD_COMMENT_TRICK, 1),
    ]
    bad = 0
    for name, src, want in cases:
        root = tempfile.mkdtemp(prefix="handback-selftest-")
        path = os.path.join(root, TARGET)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(src)
        viol = scan(root)
        got = 1 if viol else 0
        flag = "ok " if got == want else "FAIL"
        if got != want:
            bad += 1
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

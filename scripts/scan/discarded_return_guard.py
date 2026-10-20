#!/usr/bin/env python3
"""Discarded-return guard — RikkaMinis CI scan gate.

## Why this gate exists

Some APIs in this repo are immutable-by-copy: the call returns a NEW object and
the receiver is untouched (ProviderHealthTracker.recordFirstToken is the one that
already bit). A call written in STATEMENT position therefore compiles, returns a
value nobody reads, and silently does nothing — the observer runs, the probe
fires, the log line looks right, and the state never changes.

2026-09-26: `ProviderHealthTraceListener.responseHeadersStart` called
`ProviderHealthTracker.recordFirstToken(it, ms())` as a statement. TTFB was
never recorded, so `budget(realHistory, routeDefault = 30s)` stayed at the
default forever and every route was judged against 30 s. Every gate was green:
it compiled, the four-way sync was complete, the i18n keys matched. A probe
comparing "attempts=1 p50=null recordedTtfbs=[]" against the control run
"recordedTtfbs=[8000]" is what proved it — and no static gate asks that question.

## What it flags

A statement-position call to a function in [WATCHLIST] — the call starts the line
after indentation, optionally through a receiver (`x.`, `x?.`) or a qualified
path. Skipped (they consume the value or are not calls):

  * declarations (`fun recordFirstToken(...)`) and comment lines;
  * assignments / returns / argument positions (the pattern does not match);
  * a call that is the body of a value-consuming lambda — `let`, `run`, `with`,
    `map`, `mapNotNull`, `flatMap`, `fold` — **but only when that lambda's own
    value is used** (assigned, returned, or in argument position). `also` /
    `apply` / `forEach` are never in that list: they return the receiver (or
    Unit), so a value dropped inside them is still dropped. A statement-position
    `x?.let { recordFirstToken(…) }` is flagged: the `let`'s value is dropped,
    which is exactly the shape that shipped (an earlier revision of this gate
    exempted it and could not see its own counter-example — the reverse-control
    run against the pre-fix source is what caught that).

Exempt a line inline with `record-ok: <reason>` (the repo's `debug-ok:`
convention). Adding a watchlist entry is the maintenance path when another
value-returning recorder is discovered.

Usage:
  python3 scripts/scan/discarded_return_guard.py <repo_root>
  python3 scripts/scan/discarded_return_guard.py --self-test
"""

import os
import re
import sys
import tempfile

SRC_SUBDIR = "src/android/app/src/main/java"

# name -> why the returned value must be consumed
WATCHLIST = {
    "recordFirstToken": (
        "returns a COPY carrying the TTFB; the tracker is immutable-by-copy, so a "
        "statement-position call records nothing (2026-09-26 TTFB wire-up defect)"
    ),
}

EXEMPT = "record-ok:"
_CALL = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*\??\.?\s*%s\s*\("
)
_DECL = re.compile(
    r"^\s*(?:@\w+\s+)*(?:public |private |internal |protected |override |suspend "
    r"|inline |operator |abstract |open )*fun\s"
)
_COMMENT = re.compile(r"^\s*(?://|\*|/\*)")
_CONSUMING_LAMBDA = re.compile(
    r"\b(?:let|run|with|map|mapNotNull|flatMap|fold|sumOf)\s*\{\s*$"
)


def _pattern(name):
    return re.compile(_CALL.pattern % re.escape(name))


def _enclosing_open(lines, i):
    """Innermost enclosing block opener above line i (nearest `... {` line)."""
    for j in range(i - 1, -1, -1):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith(("//", "*", "/*")):
            continue
        if stripped.endswith("{"):
            return j, lines[j]
        return -1, ""
    return -1, ""


def _consumes_lambda_value(open_line):
    """True only when the lambda's own value is used.

    `x?.let { … }` written as a STATEMENT drops the lambda's value, so a
    watchlisted call inside it is dropped too — that is the exact shape of the
    2026-09-26 defect, and an earlier version of this gate exempted it and thus
    could not see its own counter-example. A consuming lambda only counts when
    its open line sits in expression position: assigned, returned, or inside an
    argument list.
    """
    if not _CONSUMING_LAMBDA.search(open_line):
        return False
    head = open_line.split("//")[0]
    if "=" in head:
        return True
    if head.strip().startswith("return "):
        return True
    return head.count("(") > head.count(")")


def scan(root):
    violations = []
    base = os.path.join(root, SRC_SUBDIR)
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in sorted(filenames):
            if not name.endswith(".kt"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().split("\n")
            for i, line in enumerate(lines):
                if _DECL.match(line) or _COMMENT.match(line):
                    continue
                _oi, _oline = _enclosing_open(lines, i)
                if _consumes_lambda_value(_oline):
                    continue
                for fn, why in WATCHLIST.items():
                    if not _pattern(fn).match(line):
                        continue
                    if EXEMPT in line or (i > 0 and EXEMPT in lines[i - 1]):
                        continue
                    violations.append((rel, i + 1, line.strip()[:80], fn, why))
    return violations


# ─────────────────────────────────────────────────────────────────────────
# [B-04 deep-audit-1037] queue-drain section
#
# 2026-10 audit: `drainQueuedPrompts` discarded `chatRepository.appendMessage`'s
# returned `MessageEntity` — the merged row got a DB id but the UI bubble, the
# agentHistory entry, and the compaction anchors never learned it. The append
# is a "value MUST be consumed" API by construction; the generic watchlist
# above can't see it (appendMessage is legitimately discarded nowhere, but a
# watchlist entry keyed on the name alone would flag the OTHER six call sites
# that store-and-use it — this section asks the drain-specific questions).
#
#   D1  the drain's appendMessage call must be bound (`val userEntity = …`)
#       and its id must flow into the agentHistory entry (dbMessageId = …);
#   D2  the empty-content guard must exist BEFORE the persist in BOTH queue
#       consumers (drain + inject) — persisting an empty user row is a
#       deterministic provider 400;
#   D3  the UI must converge with the DB: placeholders removed + exactly ONE
#       user bubble carrying the persisted id (N rows vs 1 DB row made the
#       retry/edit cutoff counts disagree).
# ─────────────────────────────────────────────────────────────────────────

QI_FILE = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatQueueInterruption.kt"

def code_only(text):
    """Blank comments and string-literal contents (spaces keep line/col).

    Braces inside strings (JSON builders, string templates) are blanked too,
    so block extraction only sees real code. Handles escapes, char literals
    and triple-quoted raw strings.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if ch == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            seg = text[i:j]
            out.append("".join(c if c == "\n" else " " for c in seg))
            i = j
            continue
        if ch == '"':
            if text[i:i + 3] == '"""':
                j = text.find('"""', i + 3)
                j = n if j < 0 else j + 3
            else:
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] in ('"', "\n"):
                        j += 1
                        break
                    j += 1
            seg = text[i:j]
            out.append("".join(c if c == "\n" else " " for c in seg))
            i = j
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] in ("'", "\n"):
                    j += 1
                    break
                j += 1
            out.append(" " * (j - i))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)

def extract_block(lines, sig_re):
    """Brace-matched block for the first line matching sig_re.

    The body brace is the first `{` at paren level 0 AFTER the parameter list
    closes — lambda defaults inside the parameter list sit at paren level 1
    and are skipped. Returns (start, end) inclusive indices or (-1, -1).
    """
    for i, ln in enumerate(lines):
        if not sig_re.search(ln):
            continue
        paren = 0
        body_line = -1
        j = i
        while j < len(lines) and body_line < 0:
            k = 0
            while k < len(lines[j]):
                c = lines[j][k]
                if c == "(":
                    paren += 1
                elif c == ")":
                    paren -= 1
                elif c == "{" and paren <= 0:
                    body_line = j
                    break
                k += 1
            j += 1
        if body_line < 0:
            return i, len(lines) - 1
        depth = 0
        for j2 in range(body_line, len(lines)):
            depth += lines[j2].count("{") - lines[j2].count("}")
            if depth <= 0:
                return i, j2
        return i, len(lines) - 1
    return -1, -1

def scan_queue_drain(root):
    findings = []
    path = os.path.join(root, QI_FILE)
    if not os.path.exists(path):
        return findings
    code = code_only(open(path, encoding="utf-8", errors="replace").read())
    lines = code.split("\n")

    def body(sig_re):
        i0, i1 = extract_block(lines, sig_re)
        if i0 < 0:
            return None
        return (i0, i1, "\n".join(lines[i0:i1 + 1]))

    drain = body(re.compile(r"suspend fun ChatViewModel\.drainQueuedPrompts\s*\("))
    inject = body(re.compile(r"suspend fun ChatViewModel\.injectQueuedPromptsAsNewTurn\s*\("))

    # D1: bind + dbMessageId in the drain
    if drain is None:
        findings.append(
            "ChatQueueInterruption.kt:?  [B-04 D1] drainQueuedPrompts body NOT FOUND (fail-closed)"
        )
    else:
        d0, d1, dbody = drain
        if not re.search(r"val\s+userEntity\s*=\s*chatRepository\.appendMessage\s*\(", dbody):
            findings.append(
                "ChatQueueInterruption.kt:%d  [B-04 D1] drainQueuedPrompts discards "
                "appendMessage's MessageEntity again — the merged row's DB id never "
                "reaches the UI bubble / agentHistory / compaction anchors" % (d0 + 1)
            )
        elif not re.search(r"dbMessageId\s*=\s*userEntity\.id", dbody):
            findings.append(
                "ChatQueueInterruption.kt:%d  [B-04 D1] the bound userEntity.id does not "
                "flow into the agentHistory entry (dbMessageId = userEntity.id missing) — "
                "the compaction anchors skip this turn" % (d0 + 1)
            )

    # D2: empty-content guard before persist, both consumers
    for label, fn in (("drainQueuedPrompts", drain), ("injectQueuedPromptsAsNewTurn", inject)):
        if fn is None:
            findings.append(
                "ChatQueueInterruption.kt:?  [B-04 D2] %s body NOT FOUND (fail-closed)" % label
            )
            continue
        i0, i1, fbody = fn
        guard = re.search(r"if\s*\(\s*combinedParts\.isEmpty\(\)\s*\)", fbody)
        persist = re.search(r"chatRepository\.appendMessage\s*\(", fbody)
        if not guard or not persist:
            missing = "the combinedParts.isEmpty() guard" if not guard else "the appendMessage persist"
            findings.append(
                "ChatQueueInterruption.kt:%d  [B-04 D2] %s lost %s — an empty user row "
                "persisted is a deterministic provider 400" % (i0 + 1, label, missing)
            )
        elif guard.start() > persist.start():
            findings.append(
                "ChatQueueInterruption.kt:%d  [B-04 D2] %s persists BEFORE the "
                "combinedParts.isEmpty() guard — the guard must precede the persist" % (i0 + 1, label)
            )

    # D3: placeholders removed + exactly one merged bubble with the persisted id
    if drain is None:
        findings.append(
            "ChatQueueInterruption.kt:?  [B-04 D3] drainQueuedPrompts body NOT FOUND (fail-closed)"
        )
    else:
        d0, d1, dbody = drain
        if "removeQueuedPlaceholders(queuedIds)" not in dbody:
            findings.append(
                "ChatQueueInterruption.kt:%d  [B-04 D3] drainQueuedPrompts no longer drops the "
                "placeholder bubbles through the single funnel — the screen keeps N rows where "
                "the DB has 1 and the retry/edit cutoff counts disagree" % (d0 + 1)
            )
        bubble = re.search(r"_messages\.value\s*=\s*_messages\.value\s*\+\s*ChatMessage\(", dbody)
        if not bubble:
            findings.append(
                "ChatQueueInterruption.kt:%d  [B-04 D3] drainQueuedPrompts does not append the "
                "merged user bubble — the persisted turn vanishes from the screen" % (d0 + 1)
            )
        elif not re.search(r"id\s*=\s*userEntity\.id", dbody):
            findings.append(
                "ChatQueueInterruption.kt:%d  [B-04 D3] the merged bubble does not carry the "
                "persisted id (id = userEntity.id) — retry/edit resolve a phantom row" % (d0 + 1)
            )
        n_bubbles = len(re.findall(r"_messages\.value\s*=\s*_messages\.value\s*\+\s*ChatMessage\(", dbody))
        if n_bubbles > 1:
            findings.append(
                "ChatQueueInterruption.kt:%d  [B-04 D3] drainQueuedPrompts appends %d user "
                "bubbles for ONE persisted row — the placeholder rows must be REMOVED, not "
                "kept beside the merged bubble" % (d0 + 1, n_bubbles)
            )
    return findings

FIXTURE = "src/android/app/src/main/java/com/rikkaminis/app/network/Trace.kt"
_Q = "com.rikkaminis.app.diagnostics.ProviderHealthTracker"
FIXTURE_LINES = [
    # 1 caught — the defect that shipped: bare statement call
    "            %s.recordFirstToken(it, ms())" % _Q,
    # 2 pass — consumed by assignment
    "            val updated = %s.recordFirstToken(it, ms())" % _Q,
    # 3 pass — consumed by return
    "            return %s.recordFirstToken(it, ms())" % _Q,
    # 4 caught — safe-call receiver, still dropped
    "        tracker?.recordFirstToken(it, ms())",
    # 5 pass — inline exemption
    "        tracker.recordFirstToken(it, ms()) // record-ok: fixture, unused on purpose",
    # 6 pass — argument position
    "        println(providerHealthTracker.recordFirstToken(it, ms()))",
    # 7 pass — declaration, not a call (false positive seen on the merged tree)
    "    fun recordFirstToken(attempt: Attempt, ttfbMs: Long): Attempt =",
    # 8 pass — body of a consuming lambda (false positive seen on the merged tree)
    "            attempt = attempt?.let {",
    "                %s.recordFirstToken(it, ms())" % _Q,
    "            }",
    # 9 caught — `also` returns the receiver, so the value is still dropped
    "            attempt?.also {",
    "                %s.recordFirstToken(it, ms())" % _Q,
    "            }",
    # 10 caught — the shape that actually shipped: a statement-position `let`,
    #             whose own value is dropped, so the call inside is dropped too
    "        attempt?.let {",
    "            %s.recordFirstToken(it, ms())" % _Q,
    "        }",
    # 11 pass — lambda in argument position: the value flows into the call
    "        consume(list.map {",
    "            %s.recordFirstToken(it, ms())" % _Q,
    "        })",
]
EXPECTED_HITS = {1, 4, 12, 15}


def self_test():
    tmp = tempfile.mkdtemp(prefix="retguard-")
    path = os.path.join(tmp, FIXTURE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(FIXTURE_LINES) + "\n")
    got = {line for _rel, line, _t, _f, _w in scan(tmp)}
    if got != EXPECTED_HITS:
        print("SELF-TEST FAILED")
        print("  expected:", sorted(EXPECTED_HITS))
        print("  got     :", sorted(got))
        return 1
    print(
        "✅ self-test: catches the dropped recordFirstToken (bare + safe-call + "
        "inside `also`), passes assigned / returned / argument / declaration / "
        "consuming-lambda / exempt forms"
    )
    return 0


# ─────────────────────────────────────────────────────────────────────────
# [B-04 deep-audit-1037] drain self-test: one breaker per criterion, expected
# literals hand-written from the rule table above (never derived from a run).
# ─────────────────────────────────────────────────────────────────────────

DRAIN_CLEAN = """package x
class LLMProvider
class ChatViewModel
internal suspend fun ChatViewModel.drainQueuedPrompts(
    provider: LLMProvider,
): String? {
    val queuedIds = queued.map { it.id }.toSet()
    val combinedParts = mutableListOf<String>()
    if (combinedParts.isEmpty()) {
        return null
    }
    val userEntity = chatRepository.appendMessage(sid, "user", userPartsJson)
    ensureTrailingRoleAlternativeBeforeUserAppend()
    agentHistory.add(LLMMessage(
        role = LLMMessage.Role.USER,
        content = userText,
        contentParts = combinedParts,
        dbMessageId = userEntity.id,
    ))
    withContext(Dispatchers.Main) {
        removeQueuedPlaceholders(queuedIds)
        _messages.value = _messages.value + ChatMessage(
            id = userEntity.id,
            role = "user",
        )
    }
    return null
}
internal suspend fun ChatViewModel.injectQueuedPromptsAsNewTurn(
    sid: String,
) {
    val combinedParts = mutableListOf<String>()
    if (combinedParts.isEmpty()) {
        return
    }
    chatRepository.appendMessage(sid, "user", userPartsJson)
}
"""

def drain_self_test():
    import tempfile
    import shutil
    rel = QI_FILE

    def run(files):
        tmp = tempfile.mkdtemp(prefix="drainguard-")
        try:
            for r, text in files.items():
                p = os.path.join(tmp, r)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text)
            return scan_queue_drain(tmp)
        finally:
            shutil.rmtree(tmp)

    clean = {rel: DRAIN_CLEAN}
    results = []
    got = run(clean)
    results.append(("clean fixture exits clean", got == [], got))

    # D1a: statement-position append (the pre-fix shape) — reds D1 binding only.
    n1 = {rel: DRAIN_CLEAN.replace(
        "    val userEntity = chatRepository.appendMessage(sid, \"user\", userPartsJson)",
        "    chatRepository.appendMessage(sid, \"user\", userPartsJson)",
    )}
    got = run(n1)
    results.append((
        "D1a: statement-position append reds D1 binding only",
        len(got) == 1 and "[B-04 D1]" in got[0] and "discards" in got[0],
        got,
    ))

    # D1b: dbMessageId dropped from the agentHistory entry — reds D1 flow only.
    n2 = {rel: DRAIN_CLEAN.replace("        dbMessageId = userEntity.id,\n", "")}
    got = run(n2)
    results.append((
        "D1b: missing dbMessageId reds D1 flow only",
        len(got) == 1 and "[B-04 D1]" in got[0] and "agentHistory" in got[0],
        got,
    ))

    # D2: the empty-content guard removed from the drain — reds D2 (drain) only.
    n3 = {rel: DRAIN_CLEAN.replace(
        "    if (combinedParts.isEmpty()) {\n        return null\n    }\n", "", 1,
    )}
    got = run(n3)
    results.append((
        "D2: drain empty-content guard removed reds D2 only",
        len(got) == 1 and "[B-04 D2]" in got[0],
        got,
    ))

    # D3a: placeholders kept (funnel bypassed) — reds D3 funnel only.
    n4 = {rel: DRAIN_CLEAN.replace("        removeQueuedPlaceholders(queuedIds)\n", "")}
    got = run(n4)
    results.append((
        "D3a: funnel bypass reds D3 only",
        len(got) == 1 and "[B-04 D3]" in got[0] and "single funnel" in got[0],
        got,
    ))

    # D3b: merged bubble not appended — reds D3 bubble only.
    n5 = {rel: DRAIN_CLEAN.replace(
        "        _messages.value = _messages.value + ChatMessage(\n            id = userEntity.id,\n            role = \"user\",\n        )\n",
        "",
    )}
    got = run(n5)
    results.append((
        "D3b: missing merged bubble reds D3 only",
        len(got) == 1 and "[B-04 D3]" in got[0] and "vanishes from the screen" in got[0],
        got,
    ))

    failed = [name for name, ok, got in results if not ok]
    if failed:
        print("DRAIN SELF-TEST FAILED (%d breaker(s))" % len(failed))
        for name, ok, got in results:
            if not ok:
                print("  ✗ " + name)
                for f in got:
                    print("      " + f)
        return 1
    print(
        "✅ drain self-test has teeth: D1 (bind+flow) / D2 (empty guard, both "
        "consumers) / D3 (funnel + merged bubble) each flip red on exactly their "
        "own breaker; clean fixture green"
    )
    return 0

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--self-test":
        rc = self_test()
        rc2 = drain_self_test()
        return 1 if (rc or rc2) else 0
    violations = scan(args[0])
    # [B-04 deep-audit-1037] queue-drain persistence section. Its findings are
    # pre-formatted `file:line  [criterion] reason` strings, so they are
    # reported in their own block — the watchlist tuple format above is left
    # exactly as it was.
    drain_violations = scan_queue_drain(args[0])
    if violations:
        print("❌ 丢弃了必须消费的返回值（看似接线，实则没生效）：")
        for rel, line, snippet, fn, why in violations:
            print("  %s:%d" % (rel, line))
            print("      %s" % snippet)
            print("      %s: %s" % (fn, why))
            print("      修法: 把返回值接下去（val x = ... 后写入/追加），或在行内加 `record-ok: <理由>`。")
        print("")
        print("  共 %d 处。" % len(violations))
    if drain_violations:
        print("❌ 队列排空段：drain 的持久化不变量被打破（B-04）：")
        for msg in drain_violations:
            print("  %s" % msg)
        print("")
        print("  共 %d 处。" % len(drain_violations))
    if violations or drain_violations:
        return 1
    print(
        "✅ discarded-return guard: no watchlisted return value is dropped "
        "(%d watched function(s))" % len(WATCHLIST)
    )
    print("✅ discarded-return guard: queue-drain persistence section green")
    return 0


if __name__ == "__main__":
    sys.exit(main())

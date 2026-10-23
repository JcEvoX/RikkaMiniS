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
import shutil
import sys
import tempfile

SRC_SUBDIR = "src/android/app/src/main/java"

# name -> why the returned value must be consumed
WATCHLIST = {
    "recordFirstToken": (
        "returns a COPY carrying the TTFB; the tracker is immutable-by-copy, so a "
        "statement-position call records nothing (2026-09-26 TTFB wire-up defect)"
    ),
    "copySharedStream": (
        "returns the staging OUTCOME (Staged / Failed); a statement-position call "
        "drops the only signal that a file was written, so the caller goes back to "
        "handing the composer a filename for a file that was never created "
        "(§1044-A08 ghost attachment)"
    ),
}

EXEMPT = "record-ok:"
# Watchlist names that are legitimately called as bare top-level functions
# (no receiver) — for those the optional-receiver pattern must still match a
# statement-position call.
BARE_OK = {"copySharedStream"}
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
    if name in BARE_OK:
        # The receiver is optional for this name: the extracted core is called
        # as a top-level function, and a statement-position call of a
        # top-level function is exactly the shape that must be caught.
        return re.compile(
            r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)*%s\s*\(" % re.escape(name)
        )
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

# ─────────────────────────────────────────────────────────────────────────
# [§1042-A-07 deep-audit-1042] staged-bytes ledger section
#
# A-07: `ShareReceiverActivity.debitStagedBytes` clamped a DISCARDED copy —
# `totalStagedBytes.addAndGet(-bytes).coerceAtLeast(0L)` written as a
# statement. The counter went negative on a double debit and the cumulative
# 500 MB cap under-counted.
#
# Why NOT a generic WATCHLIST entry for coerce*: measured on the real tree
# (2026-10-22, `probe/inject_watchlist.py`), the three names hit 15 sites and
# ALL 15 are legitimate — parameter-list continuation lines, single-expression
# function tails (`= x.coerceIn(...)`), assignment continuations, `.let` tails.
# Worse, the generic statement-position regex cannot even SEE the defect's own
# shape (the receiver is an infix call result, not a simple identifier), so it
# would have been 15 false positives and zero teeth. The specialized criteria
# below anchor the DECISION POINTS instead:
#
#   S1  debitStagedBytes must write the clamped balance back THROUGH the
#       ledger (updateAndGet/set on a receiver named `stagedBytes`), never
#       `.coerce*` on a discarded copy;
#   S2  the AttachText branch must guard the write through the pure
#       admission core (`admitAttachText`) AND credit the ledger after it —
#       a bare `writeText` with no guard is the pre-fix shape;
#   S3  every reference to the counter goes through the ledger — a bare
#       `AtomicLong` named totalStagedBytes in this file means someone
#       re-inlined the raw counter (fail-closed).
# ─────────────────────────────────────────────────────────────────────────

SHARE_FILE = "share/ShareReceiverActivity.kt"
LEDGER_FILE = "share/StagedBytesLedger.kt"

def scan_staged_bytes(root):
    findings = []
    base = os.path.join(root, SRC_SUBDIR, "com", "rikkaminis", "app")
    share_path = os.path.join(base, *SHARE_FILE.split("/"))
    ledger_path = os.path.join(base, *LEDGER_FILE.split("/"))
    if not os.path.isfile(share_path):
        if os.path.isdir(os.path.dirname(share_path)):
            findings.append("%s:?  [A-07 S1] ShareReceiverActivity.kt missing while share/ exists — the staged-bytes criteria lost their anchor (fail-closed)" % SHARE_FILE)
        return findings
    code = code_only(open(share_path, encoding="utf-8", errors="replace").read())
    lines = code.split("\n")

    def body(sig_re):
        i0, i1 = extract_block(lines, sig_re)
        if i0 < 0:
            return None
        return i0, i1, "\n".join(lines[i0:i1 + 1])

    # S1: the debit must write the clamp back through the ledger.
    debit = body(re.compile(r"fun\s+debitStagedBytes\s*\("))
    if debit is None:
        findings.append("%s:?  [A-07 S1] debitStagedBytes body NOT FOUND (fail-closed)" % SHARE_FILE)
    else:
        i0, i1, dbody = debit
        if re.search(r"\.coerce(?:AtLeast|AtMost|In)\s*\(", dbody):
            findings.append(
                "%s:%d  [A-07 S1] debitStagedBytes applies a clamp inside its body — the "
                "2026 defect clamped a DISCARDED copy (`addAndGet(-b).coerceAtLeast(0)` as a "
                "statement); the clamped write-back must live in StagedBytesLedger.debit" % (SHARE_FILE, i0 + 1)
            )
        if not re.search(r"stagedBytes\.debit\s*\(", dbody):
            findings.append(
                "%s:%d  [A-07 S1] debitStagedBytes does not route through the ledger "
                "(`stagedBytes.debit(…)`) — a raw AtomicLong update here can re-ship the "
                "discarded-clamp defect" % (SHARE_FILE, i0 + 1)
            )

    # S3: the raw counter must not be re-inlined in this file.
    if re.search(r"\btotalStagedBytes\b", code):
        for i, ln in enumerate(lines):
            if re.search(r"\btotalStagedBytes\b", ln):
                findings.append(
                    "%s:%d  [A-07 S3] bare `totalStagedBytes` AtomicLong re-inlined — every "
                    "access must go through StagedBytesLedger (fail-closed)" % (SHARE_FILE, i + 1)
                )
                break

    # S2: AttachText — guard + credit around the writeText.
    send = body(re.compile(r"fun\s+handleSingleSend\s*\("))
    if send is None:
        findings.append("%s:?  [A-07 S2] handleSingleSend body NOT FOUND (fail-closed)" % SHARE_FILE)
    else:
        i0, i1, sbody = send
        m = re.search(r"is\s+SingleSendDecision\.AttachText\s*->\s*\{", sbody)
        if not m:
            findings.append(
                "%s:%d  [A-07 S2] handleSingleSend lost the AttachText branch — the text-"
                "staging path moved; re-anchor this criterion" % (SHARE_FILE, i0 + 1)
            )
        else:
            # Brace-match from the branch's own `{` — `code_only` blanked the
            # strings/comments, so braces here are real. Slicing on the next
            # `is … ->` line would break the moment that arm is reordered.
            brace = m.end() - 1
            depth = 0
            end = len(sbody) - 1
            for j in range(brace, len(sbody)):
                if sbody[j] == "{":
                    depth += 1
                elif sbody[j] == "}":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            branch = sbody[brace:end + 1]
            write = re.search(r"writeText\s*\(", branch)
            admit = re.search(r"admitAttachText\s*\(", branch)
            credit = re.search(r"stagedBytes\.credit\s*\(", branch)
            if not write:
                findings.append(
                    "%s:%d  [A-07 S2] the AttachText branch no longer writes the text itself "
                    "(no `writeText`) — the staging path moved; re-anchor this criterion"
                    % (SHARE_FILE, i0 + 1)
                )
            else:
                if not admit:
                    findings.append(
                        "%s:%d  [A-07 S2] the AttachText branch writes text with NO budget guard — "
                        "long EXTRA_TEXT shares bypassed MAX_FILE_BYTES / MAX_TOTAL_BYTES and the "
                        "500 MB cap (the pre-fix shape)" % (SHARE_FILE, i0 + 1)
                    )
                if not re.search(r"attachTextByteCount\s*\(", branch):
                    findings.append(
                        "%s:%d  [A-07 S2] the AttachText branch does not measure the text through "
                        "attachTextByteCount() — the budget counts UTF-8 BYTES, and an inline char "
                        "count under-charges every non-ASCII share" % (SHARE_FILE, i0 + 1)
                    )
                elif admit.start() > write.start():
                    findings.append(
                        "%s:%d  [A-07 S2] the AttachText budget guard runs AFTER the write — a "
                        "post-mortem check cannot keep the forbidden bytes off disk"
                        % (SHARE_FILE, i0 + 1)
                    )
                if not credit:
                    findings.append(
                        "%s:%d  [A-07 S2] the AttachText branch writes text but never credits the "
                        "staged-bytes ledger — the budget under-counts and admits past the cap"
                        % (SHARE_FILE, i0 + 1)
                    )
                elif credit.start() < write.start():
                    findings.append(
                        "%s:%d  [A-07 S2] the AttachText branch credits the ledger BEFORE the "
                        "write — a failed write would leave phantom bytes counted against the cap"
                        % (SHARE_FILE, i0 + 1)
                    )

    # S2b: the admission core itself must exist with the strict > shape.
    if not os.path.isfile(ledger_path):
        findings.append("%s:?  [A-07 S2] StagedBytesLedger.kt missing while the share file "
                        "routes through it (fail-closed)" % LEDGER_FILE)
    else:
        lcode = code_only(open(ledger_path, encoding="utf-8", errors="replace").read())
        if not re.search(r"textBytes\s*>\s*maxFileBytes", lcode) or \
           not re.search(r"currentTotalBytes\s*\+\s*textBytes\s*>\s*maxTotalBytes", lcode):
            findings.append(
                "%s:?  [A-07 S2] admitAttachText lost the strict-`>` guard shape — the "
                "per-file / cumulative caps no longer bound text attachments" % LEDGER_FILE
            )
        # Two accepted write-back idioms — the DECISION is "the clamped balance is
        # written back", not one spelling (same idiom as the fastio NOFOLLOW
        # criterion, which accepts both explicit and flag-based guards):
        #   A: updateAndGet { (it - bytes).coerceAtLeast(0L) }   (atomic lambda)
        #   B: set(total.addAndGet(-bytes).coerceAtLeast(0L))     (one-line set)
        idiom_a = re.search(r"updateAndGet\s*\{[^{}]*coerceAtLeast\s*\(\s*0", lcode)
        idiom_b = re.search(r"\.set\s*\([^()]*addAndGet\s*\(\s*-[^()]*\)[^()]*coerceAtLeast\s*\(\s*0", lcode)
        if not (idiom_a or idiom_b):
            findings.append(
                "%s:?  [A-07 S1] StagedBytesLedger.debit lost the clamp-inside-the-write "
                "(accepted: `updateAndGet { … coerceAtLeast(0L) }` or "
                "`set(total.addAndGet(-bytes).coerceAtLeast(0L))`) — the negative-balance "
                "hole is back" % LEDGER_FILE
            )
    return findings

# ─────────────────────────────────────────────────────────────────────────
# A-08 — the share inbound stream copy (deep-audit-1044)
#
# Neither the watchlist nor the staged-bytes criteria above could see this one:
# the call site was syntactically fine and the value WAS consumed — the helper
# simply returned a staged filename for a file it never created.
#
# `ContentResolver.openInputStream(Uri)` is allowed to return null WITHOUT
# throwing (a remote provider refuses the read, or its process was reaped). The
# pre-fix copy read
#
#     contentResolver.openInputStream(uri)?.use { input ->
#         dest.outputStream().use { output -> … }
#     }
#     name                        ← returned unconditionally
#
# so a null stream skipped the whole `use` block, the destination file was never
# created, and the caller still built a `PendingShare.Item` for it: a ghost
# attachment that reached the composer and never took the "Could not read shared
# file" branch.
#
# The decision pinned here is the ORDER, not the spelling: inside whichever
# function performs the copy, the stream must be null-checked and abandoned
# BEFORE the destination file is created/truncated. Accepted early exits:
#   `?: return …` / `?: throw …`           (elvis — PhotosOffloadHandler shape)
#   `if (x == null) { …; return … }`       (guard clause)
# Delegating the copy to a helper (the §1044 ship shape: copySharedStream) is
# equally fine — every function that BOTH acquires a stream and creates the
# destination must obey the rule, so an inlined regression is caught as well.
# ─────────────────────────────────────────────────────────────────────────

_STREAM_SIG = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:public |private |internal |protected |override |suspend |inline "
    r"|operator |abstract |open )*fun\s+(?:<[^>]*>\s*)?"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
_STREAM_SRC = re.compile(r"openInputStream\s*\(")
_STREAM_PROVIDER_CALL = re.compile(r"(?<![A-Za-z0-9_])openStream\s*\(\s*\)")
# The destination is opened either directly (`dest.outputStream()`) or through
# the injected factory the pure core takes (`openDest()`).
_DEST_CREATE = re.compile(r"outputStream\s*\(|\bopenDest\s*\(\s*\)")
_STREAM_TOKEN = r"(?:openInputStream|openStream|input|source|stream)"
# Elvis early-exit, attached to a stream-ish expression within one short span.
_GUARD_ELVIS = re.compile(_STREAM_TOKEN + r"[\s\S]{0,120}?\?\:\s*(?:return|throw)\b")
# Guard clause: `if (… == null …) { … return … }` where the condition mentions a
# stream-ish name (so an unrelated `if (uri == null)` above it cannot satisfy
# this criterion from a distance).
_GUARD_IF_NULL = re.compile(
    r"if\s*\([^\n]*" + _STREAM_TOKEN + r"[^\n]*==\s*null[^\n]*\)[\s\S]{0,300}?\breturn\b"
)


def _function_bodies(lines):
    """(name, start, end, body) for every brace-bodied `fun` in [lines].

    Expression-bodied functions (`… = expr`) are skipped: they cannot carry the
    create-then-copy shape this criterion judges. Nested declarations are not
    descended into — the enclosing body already contains them textually.
    """
    bodies = []
    i = 0
    n = len(lines)
    while i < n:
        m = _STREAM_SIG.match(lines[i])
        if not m:
            i += 1
            continue
        paren = 0
        j = i
        body_start = -1
        while j < n:
            if j > i and _STREAM_SIG.match(lines[j]):
                break
            found = False
            for ch in lines[j]:
                if ch == "(":
                    paren += 1
                elif ch == ")":
                    paren -= 1
                elif ch == "{" and paren <= 0:
                    found = True
                    break
            if found:
                body_start = j
                break
            j += 1
        if body_start < 0:
            # [deep-audit-1047 B-01] `j` is the NEXT declaration (expression-bodied
            # functions have no body brace): skipping it used to drop the function
            # right after an `= expr` one, so its body was never attributed.
            i = j
            continue
        depth = 0
        end = n - 1
        for j2 in range(body_start, n):
            depth += lines[j2].count("{") - lines[j2].count("}")
            if depth <= 0:
                end = j2
                break
        bodies.append((m.group(1), i, end, "\n".join(lines[i:end + 1])))
        i = end + 1
    return bodies


def _own_acquisition(body):
    """Position of the stream ACQUISITION inside [body], or None.

    An acquisition is an `openInputStream(` / `openStream()` call that is not
    merely passed as a lambda to somebody else. The distinction matters: the
    §1044 ship shape has the Activity hand `openStream = { resolver
    .openInputStream(uri) }` to the core — the Activity does not acquire the
    stream, the core does, and the core is where the null guard must live.

    A lambda is recognised by an `=` whose right-hand side opens a brace
    (`x = { … }`); a plain `val x = call()` is NOT a lambda. Brace depth is
    tracked with a stack so a call inside a lambda body is attributed to the
    lambda, not to the enclosing function.
    """
    stack = []          # one entry per open brace: True = lambda body
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "=":
            j = i + 1
            while j < n and body[j] in " \t\n":
                j += 1
            if j < n and body[j] == "{":
                stack.append(True)
                i = j + 1
                continue
        elif ch == "{":
            stack.append(False)
        elif ch == "}":
            if stack:
                stack.pop()
        elif ch == "o":
            m = _ACQUIRE_RE.match(body, i)
            if m:
                if not any(stack):     # not inside any lambda body
                    return m.start()
                i = m.end()
                continue
        i += 1
    return None


_ACQUIRE_RE = re.compile(
    r"openInputStream\s*\(|(?<![A-Za-z0-9_])openStream\s*\(\s*\)"
)


def _guard_before(body, acquisition, dest):
    """Position of the null early-exit that follows [acquisition], or None.

    Two accepted spellings, both anchored AFTER the acquisition and BEFORE the
    destination is opened:
      * elvis      — `val input = openStream() ?: return null`
      * guard      — `if (input == null) { … return null }`
    """
    window = body[acquisition:dest.start()] if dest.start() > acquisition else ""
    if dest.start() <= acquisition:
        # The destination is opened BEFORE the stream is even acquired — the
        # 0-byte-ghost order. Report it as an ORDER violation, not as a
        # missing guard (the guard may well exist, just too late).
        return dest.start() + 1
    cands = []
    m = re.search(r"\?\:\s*(?:return|throw)\b", window)
    if m:
        cands.append(acquisition + m.end())
    m = re.search(
        r"if\s*\([^\n]*(?:==\s*null|is\s+null)[^\n]*\)\s*\{?[\s\S]{0,300}?\breturn\b",
        window,
    )
    if m:
        cands.append(acquisition + m.end())
    if not cands:
        return None
    return min(cands)


def scan_share_stream_guard(root):
    findings = []
    base = os.path.join(root, SRC_SUBDIR, "com", "rikkaminis", "app")
    share_dir = os.path.join(base, "share")
    share_path = os.path.join(base, *SHARE_FILE.split("/"))
    if not os.path.isdir(share_dir):
        return findings
    if not os.path.isfile(share_path):
        findings.append(
            "%s:?  [A-08 G1] ShareReceiverActivity.kt missing while share/ exists — "
            "the null-stream criteria lost their anchor (fail-closed)" % SHARE_FILE
        )
        return findings

    # The copy may be inlined in the Activity or delegated to a helper in the
    # same package (§1044 ship shape: SharedStreamCopy.kt) — judge every file.
    files = []
    for name in sorted(os.listdir(share_dir)):
        if not name.endswith(".kt"):
            continue
        path = os.path.join(share_dir, name)
        rel = os.path.relpath(path, root)
        text = code_only(open(path, encoding="utf-8", errors="replace").read())
        files.append((rel, text, text.split("\n")))

    package_code = "\n".join(text for _rel, text, _lines in files)

    # G2 — the inbound staging path must still acquire a ContentResolver stream.
    # Without this anchor a "fix" that quietly deletes the staging path would
    # satisfy G1 vacuously.
    if not _STREAM_SRC.search(package_code):
        findings.append(
            "%s:?  [A-08 G2] no `openInputStream(` left in the share package — the inbound "
            "stream-staging path moved or was removed; re-anchor this criterion "
            "(fail-closed)" % SHARE_FILE
        )
        return findings

    # G3 — the copy outcome must be consumed. A statement-position call drops the
    # only signal that a file was written, which is the ghost-attachment shape
    # again; the generic watchlist cannot see a bare top-level call on its own.
    if "fun copySharedStream(" in package_code:
        bound = False
        for _rel, _text, lines in files:
            for ln in lines:
                m = re.search(r"copySharedStream\s*\(", ln)
                if not m:
                    continue
                if re.search(r"\bval\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*", ln[:m.start()]):
                    bound = True
        if not bound:
            findings.append(
                "%s:?  [A-08 G3] the copySharedStream outcome is never bound — a "
                "statement-position call throws away the only signal that a file was "
                "written, and the caller is back to handing out a filename for a file "
                "that may not exist" % SHARE_FILE
            )

    # G1 — guard before the destination file is created.
    #
    # The two shapes that must both satisfy the rule:
    #   (a) a function that OPENS the stream itself (`openInputStream(` in its
    #       own body) and also opens the destination;
    #   (b) the pure core `copySharedStream`, which receives the stream through
    #       the `openStream` factory and opens the destination through
    #       `openDest`.
    # A function that merely INJECTS the stream (e.g. `openStream = {
    # contentResolver.openInputStream(uri) }`) is neither: it does not acquire
    # the stream, it hands the acquisition to the core — which is where the
    # guard now lives. Judging it as if it acquired the stream would flag the
    # §1044 ship shape itself, so the criterion tracks WHERE the acquisition
    # happens instead of counting occurrences.
    saw_dest = False
    for rel, _text, lines in files:
        for name, start, _end, body in _function_bodies(lines):
            dest = _DEST_CREATE.search(body)
            if not dest:
                continue
            saw_dest = True
            # The acquisition must be in THIS function's own body, not inside a
            # lambda the function passes to somebody else.
            acquires = _own_acquisition(body)
            if acquires is None:
                continue          # only injects the stream — the core guards it
            guard = _guard_before(body, acquires, dest)
            if guard is None:
                findings.append(
                    "%s:%d  [A-08 G1] %s() creates the destination file from a stream with NO "
                    "null early-exit — `openInputStream` returns null without throwing when a "
                    "remote provider refuses, so the copy stages nothing and the caller still "
                    "gets a filename (ghost attachment)" % (rel, start + 1, name)
                )
                continue
            if guard > dest.start():
                findings.append(
                    "%s:%d  [A-08 G1] %s() null-checks the stream AFTER creating the destination "
                    "file — the file is created/truncated before the guard can bail out, which is "
                    "the 0-byte-ghost shape; check first, create second"
                    % (rel, start + 1, name)
                )
    if not saw_dest:
        findings.append(
            "%s:?  [A-08 G1] no `outputStream(` in the share package — the staging write path "
            "moved; re-anchor this criterion (fail-closed)" % SHARE_FILE
        )

    # G4 — the copy must have a SUCCESS GATE: the caller may only report a
    # staged file when the core says it staged one. This is the criterion that
    # catches the ghost attachment even if somebody re-inlines the copy in a
    # shape G1 cannot see: a function that returns a filename without ever
    # asking whether the copy succeeded.
    # G4 — the copy must have a SUCCESS GATE. A function that RETURNS A
    # FILENAME (not an outcome) and acquires the stream itself must either
    # gate on the copy result or return null when the stream was absent. The
    # pure core is exempt: it RETURNS the outcome, which IS the gate.
    for rel, _text, lines in files:
        for name, start, _end, body in _function_bodies(lines):
            if _own_acquisition(body) is None:
                continue          # only injects the stream — the core gates it
            if _returns_outcome(body):
                continue          # the outcome IS the success gate
            gated = (
                re.search(r"when\s*\(", body)
                or re.search(r"\bis\s+[A-Za-z_][A-Za-z0-9_.]*\s*->", body)
                or re.search(r"if\s*\([^\n]*==\s*null[^\n]*\)\s*\{?[^\n]*return\s+null", body)
            )
            if not gated:
                findings.append(
                    "%s:%d  [A-08 G4] %s() opens a content stream and returns a filename with NO "
                    "success gate — when the provider hands back no stream the copy stages "
                    "nothing, yet the caller still gets a name for a file that was never created "
                    "(ghost attachment)" % (rel, start + 1, name)
                )
    return findings


def _returns_outcome(body):
    """True when the function RETURNS the copy outcome (its type is a sealed
    outcome, not a filename). Such a function IS the success gate."""
    return bool(re.search(r"Copy\b|SharedStreamCopy|Outcome\b", body))


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

# ─────────────────────────────────────────────────────────────────────────
# [§1042-A-07] staged-bytes self-test — one breaker per criterion, expected
# literal findings hand-written from the rule table (never derived from a run).
# ─────────────────────────────────────────────────────────────────────────

STAGED_LEDGER_CLEAN = """package com.rikkaminis.app.share

import java.util.concurrent.atomic.AtomicLong

internal class StagedBytesLedger(initial: Long = 0L) {
    private val total = AtomicLong(initial)

    fun current(): Long = total.get()

    fun credit(bytes: Long) {
        if (bytes > 0L) total.addAndGet(bytes)
    }

    fun debit(bytes: Long) {
        total.updateAndGet { (it - bytes).coerceAtLeast(0L) }
    }

    fun reset() {
        total.set(0L)
    }
}

internal fun attachTextByteCount(text: String): Long =
    text.toByteArray(Charsets.UTF_8).size.toLong()

internal sealed class AttachTextAdmission {
    object RejectedPerFile : AttachTextAdmission()
    object RejectedCumulative : AttachTextAdmission()
    object Admitted : AttachTextAdmission()
}

internal fun admitAttachText(
    textBytes: Long,
    currentTotalBytes: Long,
    maxFileBytes: Long,
    maxTotalBytes: Long,
): AttachTextAdmission = when {
    textBytes > maxFileBytes -> AttachTextAdmission.RejectedPerFile
    currentTotalBytes + textBytes > maxTotalBytes -> AttachTextAdmission.RejectedCumulative
    else -> AttachTextAdmission.Admitted
}
"""

def _staged_activity(branch, debit_body=None, extra=""):
    """Minimal ShareReceiverActivity slice exercising the A-07 criteria."""
    db = debit_body or "            stagedBytes.debit(bytes)\n"
    return (
        "package com.rikkaminis.app.share\n"
        "\n"
        "class ShareReceiverActivity {\n"
        "    companion object {\n"
        "        private val stagedBytes = StagedBytesLedger()\n"
        "\n"
        "        fun resetStagedBytes() {\n"
        "            stagedBytes.reset()\n"
        "        }\n"
        "\n"
        "        fun debitStagedBytes(bytes: Long) {\n"
        + db +
        "        }\n"
        "    }\n"
        "\n"
        "    private fun handleSingleSend(intent: Intent, out: MutableList<Item>) {\n"
        "        when (val d = decide()) {\n"
        "            is SingleSendDecision.InlineText -> out += Item(d.text)\n"
        + branch +
        "            is SingleSendDecision.Stream -> copy()\n"
        "            SingleSendDecision.Drop -> Unit\n"
        "        }\n"
        "    }\n"
        "\n"
        "    private fun discardStagedAttachments(items: List<Item>) {\n"
        "        for (item in items) {\n"
        "            debitStagedBytes(item.len())\n"
        "        }\n"
        "    }\n"
        + extra +
        "}\n"
    )

STAGED_ATTACH_CLEAN = """            is SingleSendDecision.AttachText -> {
                val textBytes = attachTextByteCount(d.text)
                val admission = admitAttachText(
                    textBytes,
                    stagedBytes.current(),
                    MAX_FILE_BYTES,
                    MAX_TOTAL_BYTES,
                )
                when (admission) {
                    AttachTextAdmission.RejectedPerFile -> log(textBytes)
                    AttachTextAdmission.RejectedCumulative -> log(textBytes)
                    AttachTextAdmission.Admitted -> {
                        File(dir(), name()).writeText(d.text, Charsets.UTF_8)
                        stagedBytes.credit(textBytes)
                        out += Item(name())
                    }
                }
            }
"""

def staged_self_test():
    import tempfile
    import shutil

    rel_a = "share/ShareReceiverActivity.kt"
    rel_l = "share/StagedBytesLedger.kt"

    def run(files):
        tmp = tempfile.mkdtemp(prefix="stagedguard-")
        try:
            for r, text in files.items():
                p = os.path.join(
                    tmp, "src/android/app/src/main/java/com/rikkaminis/app", r
                )
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text)
            return scan_staged_bytes(tmp)
        finally:
            shutil.rmtree(tmp)

    def clean_tree(activity=None, ledger=None):
        return {
            rel_a: activity if activity is not None else _staged_activity(STAGED_ATTACH_CLEAN),
            rel_l: ledger if ledger is not None else STAGED_LEDGER_CLEAN,
        }

    results = []

    # 0: clean fixture → no findings.
    got = run(clean_tree())
    results.append(("clean fixture exits clean", got == [], got))

    # S1a: the pre-fix shape — debitStagedBytes clamps a discarded copy.
    n1 = clean_tree(activity=_staged_activity(
        STAGED_ATTACH_CLEAN,
        debit_body="            totalStagedBytes.addAndGet(-bytes).coerceAtLeast(0L)\n",
        extra="        private val totalStagedBytes = java.util.concurrent.atomic.AtomicLong(0)\n",
    ))
    got = run(n1)
    ok = (len(got) == 3 and
          sum(1 for f in got if "[A-07 S1]" in f and "DISCARDED copy" in f) == 1 and
          sum(1 for f in got if "[A-07 S1]" in f and "route through the ledger" in f) == 1 and
          sum(1 for f in got if "[A-07 S3]" in f) == 1)
    results.append(("S1a: discarded-clamp shape reds S1 (clamp + funnel) and S3", ok, got))

    # S1b: routes to a raw AtomicLong instead of the ledger (no clamp in body).
    n2 = clean_tree(activity=_staged_activity(
        STAGED_ATTACH_CLEAN,
        debit_body="            totalStagedBytes.addAndGet(-bytes)\n",
        extra="        private val totalStagedBytes = java.util.concurrent.atomic.AtomicLong(0)\n",
    ))
    got = run(n2)
    ok = (len(got) == 2 and
          sum(1 for f in got if "[A-07 S1]" in f and "route through the ledger" in f) == 1 and
          sum(1 for f in got if "[A-07 S3]" in f) == 1)
    results.append(("S1b: raw-counter funnel reds S1 funnel + S3", ok, got))

    # S1c: the ledger's write-back replaced by a bare statement clamp.
    bad_ledger = STAGED_LEDGER_CLEAN.replace(
        "        total.updateAndGet { (it - bytes).coerceAtLeast(0L) }\n",
        "        total.addAndGet(-bytes).coerceAtLeast(0L)\n",
    )
    got = run(clean_tree(ledger=bad_ledger))
    ok = (len(got) == 1 and "[A-07 S1]" in got[0] and "clamp-inside-the-write" in got[0])
    results.append(("S1c: ledger statement-clamp reds S1 write-back only", ok, got))

    # S2a: the pre-fix AttachText branch — bare write, no guard, no credit.
    n3 = clean_tree(activity=_staged_activity(
        """            is SingleSendDecision.AttachText -> {
                val name = name()
                File(dir(), name).writeText(d.text, Charsets.UTF_8)
                out += Item(name)
            }
""",
    ))
    got = run(n3)
    ok = (len(got) == 3 and
          sum(1 for f in got if "[A-07 S2]" in f and "NO budget guard" in f) == 1 and
          sum(1 for f in got if "[A-07 S2]" in f and "never credits" in f) == 1 and
          sum(1 for f in got if "[A-07 S2]" in f and "attachTextByteCount()" in f) == 1)
    results.append(("S2a: unguarded AttachText reds the S2 family only (guard/credit/bytes)", ok, got))

    # S2b: guard runs AFTER the write (post-mortem) — reds the order finding.
    n4 = clean_tree(activity=_staged_activity(
        """            is SingleSendDecision.AttachText -> {
                val textBytes = attachTextByteCount(d.text)
                File(dir(), name()).writeText(d.text, Charsets.UTF_8)
                val admission = admitAttachText(textBytes, stagedBytes.current(), MAX_FILE_BYTES, MAX_TOTAL_BYTES)
                stagedBytes.credit(textBytes)
                out += Item(name())
            }
""",
    ))
    got = run(n4)
    ok = (len(got) == 1 and "[A-07 S2]" in got[0] and "AFTER the write" in got[0])
    results.append(("S2b: guard after the write reds the order finding only", ok, got))

    # S2e: char count instead of the UTF-8 byte funnel — reds byteCount only.
    n7 = clean_tree(activity=_staged_activity(
        STAGED_ATTACH_CLEAN.replace(
            "val textBytes = attachTextByteCount(d.text)",
            "val textBytes = d.text.length.toLong()",
        ),
    ))
    got = run(n7)
    ok = (len(got) == 1 and "[A-07 S2]" in got[0] and "attachTextByteCount()" in got[0])
    results.append(("S2e: char-count drift reds the byte-funnel finding only", ok, got))

    # S2c: credit BEFORE the write — reds the phantom-bytes finding only.
    n5 = clean_tree(activity=_staged_activity(
        STAGED_ATTACH_CLEAN.replace(
            "                        File(dir(), name()).writeText(d.text, Charsets.UTF_8)\n"
            "                        stagedBytes.credit(textBytes)\n",
            "                        stagedBytes.credit(textBytes)\n"
            "                        File(dir(), name()).writeText(d.text, Charsets.UTF_8)\n",
        ),
    ))
    got = run(n5)
    ok = (len(got) == 1 and "[A-07 S2]" in got[0] and "BEFORE the write" in got[0])
    results.append(("S2c: credit before the write reds the phantom-bytes finding only", ok, got))

    # S2d: the admission core lost the strict > shape (>= boundary drift).
    bad_ledger2 = STAGED_LEDGER_CLEAN.replace(
        "    textBytes > maxFileBytes -> AttachTextAdmission.RejectedPerFile",
        "    textBytes >= maxFileBytes -> AttachTextAdmission.RejectedPerFile",
    )
    got = run(clean_tree(ledger=bad_ledger2))
    ok = (len(got) == 1 and "[A-07 S2]" in got[0] and "strict-`>` guard" in got[0])
    results.append(("S2d: >= boundary drift reds the admission-shape finding only", ok, got))

    # ROT: the ledger file vanished while the activity routes through it.
    got = run({rel_a: _staged_activity(STAGED_ATTACH_CLEAN)})
    ok = (len(got) == 1 and "[A-07 S2]" in got[0] and "missing while the share file" in got[0])
    results.append(("ROT: missing ledger file is a fail-closed finding", ok, got))

    # ROT: the AttachText branch disappeared from handleSingleSend.
    n6 = clean_tree(activity=_staged_activity(
        """            is SingleSendDecision.AttachText -> {
                out += Item(stageText(d.text))
            }
""",
    ))
    got = run(n6)
    ok = (len(got) == 1 and "[A-07 S2]" in got[0] and "no longer writes the text itself" in got[0])
    results.append(("ROT: delegated-away writeText is a re-anchor finding", ok, got))

    failed = [name for name, ok, got in results if not ok]
    if failed:
        print("STAGED-BYTES SELF-TEST FAILED (%d breaker(s))" % len(failed))
        for name, ok, got in results:
            if not ok:
                print("  ✗ " + name)
                for f in got:
                    print("      " + f)
        return 1
    print(
        "✅ staged-bytes self-test has teeth: S1 (discarded clamp / raw-counter "
        "funnel / ledger write-back), S2 (unguarded write / guard-after-write / "
        "credit-before-write / boundary drift), S3 (re-inlined counter) and the "
        "ROT arms each flip red on exactly their own breaker; clean fixture green"
    )
    return 0


def _share_stream_activity(core):
    """Minimal ShareReceiverActivity slice exercising the A-08 criteria."""
    return (
        "package com.rikkaminis.app.share\n"
        "\n"
        "internal sealed class Copy {\n"
        "    object Staged : Copy()\n"
        "    object Failed : Copy()\n"
        "}\n"
        "\n"
        "class ShareReceiverActivity {\n"
        "    private fun copyUriToStaging(uri: Uri): String? {\n"
        "        val dest = File(name())\n"
        "        return when (val outcome = copySharedStream({ resolver.openInputStream(uri) }, dest)) {\n"
        "            is Copy.Staged -> name()\n"
        "            is Copy.Failed -> null\n"
        "        }\n"
        "    }\n"
        "}\n"
        "\n"
        "internal fun copySharedStream(openStream: () -> InputStream?, openDest: () -> OutputStream): Copy {\n"
        "    return try {\n"
        + core +
        "    } catch (e: Exception) {\n"
        "        Copy.Failed\n"
        "    }\n"
        "}\n"
    )


# The §1044 ship shape: guard, then create the destination, then copy.
STREAM_CORE_CLEAN = (
    "        val input = openStream()\n"
    "            ?: return Copy.Failed\n"
    "        input.use { source ->\n"
    "            openDest().use { output -> source.copyTo(output) }\n"
    "        }\n"
    "        Copy.Staged\n"
)


def share_stream_self_test():
    import shutil
    import tempfile

    rel = os.path.join("share", "ShareReceiverActivity.kt")

    def run(text, extra=None):
        tmp = tempfile.mkdtemp(prefix="sharestream-")
        try:
            pkg = os.path.join(tmp, SRC_SUBDIR, "com", "rikkaminis", "app", "share")
            os.makedirs(pkg, exist_ok=True)
            with open(os.path.join(pkg, "ShareReceiverActivity.kt"), "w", encoding="utf-8") as fh:
                fh.write(text)
            for fname, body in (extra or {}).items():
                with open(os.path.join(pkg, fname), "w", encoding="utf-8") as fh:
                    fh.write(body)
            return scan_share_stream_guard(tmp)
        finally:
            shutil.rmtree(tmp)

    results = []

    # A08-0: the shipped shape (guard → create → copy) is clean.
    got = run(_share_stream_activity(STREAM_CORE_CLEAN))
    results.append(("A08-0: guard-before-create fixture exits clean", got == [], got))

    # A08-1: the pre-fix shape — no null check at all, `use` on the nullable.
    got = run(_share_stream_activity(
        STREAM_CORE_CLEAN.replace("            ?: return Copy.Failed\n", "")
    ))
    ok = (len(got) == 1 and "[A-08 G1]" in got[0] and "NO null early-exit" in got[0]
          and "copySharedStream()" in got[0])
    results.append(("A08-1: missing null guard reds G1 (no early exit)", ok, got))

    # A08-2: guard present but AFTER the destination is created — the 0-byte ghost.
    got = run(_share_stream_activity(
        "        val output = openDest()\n"
        "        val input = openStream()\n"
        "            ?: return Copy.Failed\n"
        "        output.use { sink -> input.use { src -> src.copyTo(sink) } }\n"
        "        Copy.Staged\n"
    ))
    ok = (len(got) == 1 and "[A-08 G1]" in got[0] and "AFTER creating the destination" in got[0])
    results.append(("A08-2: guard after create reds G1 (order)", ok, got))

    # A08-3: the other accepted spelling — a guard clause instead of elvis.
    #        Must stay green: the criterion anchors the decision, not the idiom.
    got = run(_share_stream_activity(
        "        val input = openStream()\n"
        "        if (input == null) {\n"
        "            AppLogger.warning(TAG, \"openInputStream returned null\")\n"
        "            return Copy.Failed\n"
        "        }\n"
        "        input.use { source ->\n"
        "            openDest().use { output -> source.copyTo(output) }\n"
        "        }\n"
        "        Copy.Staged\n"
    ))
    results.append(("A08-3: guard-clause spelling stays green", got == [], got))

    # A08-4: the destination creation disappeared — fail closed, re-anchor.
    got = run(_share_stream_activity(
        STREAM_CORE_CLEAN.replace(
            "            openDest().use { output -> source.copyTo(output) }\n",
            "            source.copyTo(sink)\n",
        )
    ))
    ok = (len(got) == 1 and "[A-08 G1]" in got[0] and "no `outputStream(`" in got[0])
    results.append(("A08-4: vanished write path is a fail-closed finding", ok, got))

    # A08-5: the resolver stream disappeared — the G2 anchor is gone.
    got = run(_share_stream_activity(STREAM_CORE_CLEAN).replace(
        "resolver.openInputStream(uri)", "resolver.stream(uri)"
    ))
    ok = (len(got) == 1 and "[A-08 G2]" in got[0] and "no `openInputStream(`" in got[0])
    results.append(("A08-5: vanished resolver stream is a fail-closed finding", ok, got))

    # A08-6: the extracted outcome must be consumed — a statement-position call
    #        drops the only signal that a file was written.
    tmp = tempfile.mkdtemp(prefix="sharestream-wl-")
    try:
        p = os.path.join(tmp, SRC_SUBDIR, "com", "rikkaminis", "app", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(
                "class ShareReceiverActivity {\n"
                "    fun stage() {\n"
                "        copySharedStream(openStream = { null }, dest = f)\n"
                "    }\n"
                "}\n"
            )
        got = scan(tmp)
        ok = len(got) == 1 and got[0][3] == "copySharedStream"
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(
                "class ShareReceiverActivity {\n"
                "    fun stage() {\n"
                "        val outcome = copySharedStream(openStream = { null }, dest = f)\n"
                "    }\n"
                "}\n"
            )
        control = scan(tmp)
        ok = ok and control == []
    finally:
        shutil.rmtree(tmp)
    results.append(("A08-6: discarded copySharedStream outcome is watchlisted (control green)", ok, got))

    # A08-7: same regression seen by the section itself — the outcome is never
    #        bound, so nothing distinguishes "staged" from "staged nothing".
    #        The Activity acquires the stream ITSELF here (the inlined-regression
    #        shape), so the success gate (G4) is what must catch it.
    got = run(
        "package com.rikkaminis.app.share\n"
        "\n"
        "class ShareReceiverActivity {\n"
        "    private fun copyUriToStaging(uri: Uri): String? {\n"
        "        val dest = File(name())\n"
        "        copySharedStream({ resolver.openInputStream(uri) }, dest)\n"
        "        return name()\n"
        "    }\n"
        "}\n"
        "\n"
        "internal fun copySharedStream(openStream: () -> InputStream?, openDest: () -> OutputStream): Copy {\n"
        + STREAM_CORE_CLEAN +
        "}\n"
    )
    ok = (len(got) == 2 and
          sum(1 for f in got if "[A-08 G3]" in f and "never bound" in f) == 1 and
          sum(1 for f in got if "[A-08 G4]" in f) == 1)
    results.append(("A08-7: unbound copy outcome reds G3 (+G4: no success gate)", ok, got))

    # A08-10: the inlined regression — the function acquires the stream itself
    #         and returns a filename with no success gate (G4).
    got = run(
        "package com.rikkaminis.app.share\n"
        "\n"
        "class ShareReceiverActivity {\n"
        "    private fun copyUriToStaging(uri: Uri): String? {\n"
        "        val dest = File(name())\n"
        "        val input = resolver.openInputStream(uri)\n"
        "        input?.use { source ->\n"
        "            openDest().use { output -> source.copyTo(output) }\n"
        "        }\n"
        "        return name()\n"
        "    }\n"
        "}\n"
    )
    ok = (len(got) == 2 and
          sum(1 for f in got if "[A-08 G4]" in f and "NO success gate" in f) == 1 and
          sum(1 for f in got if "[A-08 G1]" in f) == 1)
    results.append(("A08-10: self-acquiring copy with no success gate reds G4 (+G1)", ok, got))

    # A08-8/A08-9: the shipped layout — the core lives in its own file. The
    # criterion must follow the copy across files (it judges the function that
    # both opens the stream and creates the file, wherever that function sits).
    thin = (
        "package com.rikkaminis.app.share\n"
        "\n"
        "class ShareReceiverActivity {\n"
        "    private fun copyUriToStaging(uri: Uri): String? {\n"
        "        val dest = File(name())\n"
        "        return when (val outcome = copySharedStream({ resolver.openInputStream(uri) }, dest)) {\n"
        "            is Copy.Staged -> name()\n"
        "            is Copy.Failed -> null\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    core_file = (
        "package com.rikkaminis.app.share\n"
        "\n"
        "internal fun copySharedStream(openStream: () -> InputStream?, openDest: () -> OutputStream): Copy {\n"
        "    return try {\n"
        + STREAM_CORE_CLEAN +
        "    } catch (e: Exception) {\n"
        "        Copy.Failed\n"
        "    }\n"
        "}\n"
    )
    got = run(thin, extra={"SharedStreamCopy.kt": core_file})
    results.append(("A08-8: split-file shipped layout exits clean", got == [], got))

    got = run(thin, extra={"SharedStreamCopy.kt": core_file.replace("            ?: return Copy.Failed\n", "")})
    ok = (len(got) == 1 and "[A-08 G1]" in got[0] and "SharedStreamCopy.kt" in got[0]
          and "NO null early-exit" in got[0])
    results.append(("A08-9: unguarded core in a separate file reds G1 on that file", ok, got))

    failed = [name for name, ok, _got in results if not ok]
    if failed:
        print("SHARE-STREAM SELF-TEST FAILED (%d breaker(s))" % len(failed))
        for name, ok, got in results:
            if not ok:
                print("  ✗ " + name)
                for f in got:
                    print("      " + f)
        return 1
    print(
        "✅ share-stream self-test has teeth: A-08 G1 (missing null guard / guard after "
        "create / vanished write path) and G2 (vanished resolver stream) each flip red on "
        "exactly their own breaker, the guard-clause spelling stays green, and a discarded "
        "copySharedStream outcome is caught by the watchlist"
    )
    return 0


# ─────────────────────────────────────────────────────────────────────────
# B-01 — the binary-write verdict (deep-audit-1047)
#
# One family, two channels, and BOTH of them fail WITHOUT throwing:
#
#  * `ContentResolver.openOutputStream(uri)` returns NULL when the provider
#    refuses the write or its process died. `stream?.use { … }` then skips the
#    write entirely and execution falls through to "publish the row" /
#    "hand the URI to another app" / "start the share chooser".
#  * `Bitmap.compress(...)` returns FALSE when the encoder wrote nothing. In
#    statement position (`file.outputStream().use { bmp.compress(…) }`) that
#    verdict is dropped by construction: `use` returns it and nobody reads it.
#
# Measured on the shipped tree (deep-audit-1047 B-01): a failed gallery save
# published an `IS_PENDING=0` MediaStore row for a 0-byte file and toasted
# "saved to album", and the clipboard / share / table-image paths pasted a
# FileProvider URI pointing at a 0-byte png.
#
# The criteria are derived from the DESTINATION, not from a site list:
#
#   B1  when the compressed bytes LEAVE the app or get PUBLISHED (the enclosing
#       function reaches `FileProvider.getUriForFile(` / `MediaStore.` /
#       `contentResolver.openOutputStream(`), the write must route its verdict
#       through `saveOutcome(` — the Android-free decision JVM tests pin.
#       Compression into an in-memory `ByteArrayOutputStream` (model image
#       budget, screenshot ring, debug dumps) is deliberately OUT of scope:
#       nothing is published and the caller's own length check sees the empty
#       array.
#   B2  every `openOutputStream(` site must exit on null (`?: throw|return|
#       error`, or a `requireNotNull` on the value) instead of swallowing it
#       with `?.use`. This one is tree-wide: the sites already shipping the
#       elvis form are green, so the criterion is cheap to keep honest.
# ─────────────────────────────────────────────────────────────────────────

_BIN_COMPRESS = re.compile(r"\.compress\s*\(")
# Tokens that mean "these bytes are handed to somebody outside this process".
_BIN_OUTBOUND = (
    "FileProvider.getUriForFile(",
    "MediaStore.",
    "contentResolver.openOutputStream(",
)
_BIN_OPEN_OUT = re.compile(r"openOutputStream\s*\(")
_BIN_ELVIS_EXIT = re.compile(r"\?\:\s*(?:throw|return|error|require)\b")
_BIN_REQUIRE_NON_NULL = re.compile(r"requireNotNull\s*\(")
_BIN_ROUTING = "saveOutcome("

def _innermost_body(bodies, line_no):
    """Innermost `fun` body whose [start, end] covers [line_no] (or None)."""
    best = None
    for name, start, end, body in bodies:
        if start <= line_no <= end and (best is None or start > best[1]):
            best = (name, start, end, body)
    return best

def scan_binary_write_verdict(root):
    """(violations, sites) — B-01 criteria. Findings are pre-formatted
    `file:line  [Bx] reason` strings, same contract as the sections above."""
    violations = []
    sites = 0
    base = os.path.join(root, SRC_SUBDIR)
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in sorted(filenames):
            if not name.endswith(".kt"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().split("\n")
            bodies = _function_bodies(lines)
            for i, line in enumerate(lines):
                if _COMMENT.match(line):
                    continue
                for _m in _BIN_COMPRESS.finditer(line):
                    holder = _innermost_body(bodies, i)
                    # No body found (e.g. a call inside an object literal the
                    # declaration scan does not descend into): judge the LOCAL
                    # window, never the whole file — a file that mentions
                    # MediaStore somewhere must not make an unrelated in-memory
                    # encode look like an outbound write.
                    body = holder[3] if holder else "\n".join(lines[max(0, i - 20):i + 20])
                    if not any(tok in body for tok in _BIN_OUTBOUND):
                        continue
                    sites += 1
                    if _BIN_ROUTING in body:
                        continue
                    where = ("%s()" % holder[0]) if holder else "the file top level"
                    violations.append(
                        "%s:%d  [B1] %s compresses bytes that leave the app (FileProvider / "
                        "MediaStore / SAF) but never reads the verdict — `compress` returns "
                        "false without throwing, so route it through saveOutcome(…)"
                        % (rel, i + 1, where)
                    )
                found = _BIN_OPEN_OUT.search(line)
                if not found:
                    continue
                if "//" in line[:found.start()]:
                    continue
                sites += 1
                window = "\n".join(lines[i:i + 40])
                if _BIN_ELVIS_EXIT.search(window) or _BIN_REQUIRE_NON_NULL.search(window):
                    continue
                if "?." in line[found.start():]:
                    violations.append(
                        "%s:%d  [B2] openOutputStream(…) is chained with `?.` and the null "
                        "stream is swallowed — the write is skipped and the caller still "
                        "reports success" % (rel, i + 1)
                    )
    return violations, sites

# ── B-01 self-test: one breaker per criterion, plus the destination predicate ──

_BIN_FIXED = (
    "package probe\n"
    "\n"
    "internal fun copyPng(ctx: Context, bmp: Bitmap, cacheDir: File) {\n"
    "    val file = File(cacheDir, \"x.png\")\n"
    "    val encoded = file.outputStream().use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }\n"
    "    if (!saveOutcome(streamOpened = true, compressed = encoded)) {\n"
    "        file.delete()\n"
    "        return\n"
    "    }\n"
    "    val uri = FileProvider.getUriForFile(ctx, \"a.b\", file)\n"
    "    val out = ctx.contentResolver.openOutputStream(uri) ?: throw IOException(\"null\")\n"
    "}\n"
)

_BIN_PRE = (
    "package probe\n"
    "\n"
    "internal fun copyPng(ctx: Context, bmp: Bitmap, cacheDir: File) {\n"
    "    val file = File(cacheDir, \"x.png\")\n"
    "    file.outputStream().use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }\n"
    "    val uri = FileProvider.getUriForFile(ctx, \"a.b\", file)\n"
    "    ctx.contentResolver.openOutputStream(uri)?.use { out -> out.write(1) }\n"
    "}\n"
)

_BIN_IN_MEMORY = (
    "package probe\n"
    "\n"
    "internal fun encodeForModel(bmp: Bitmap): ByteArray {\n"
    "    val baos = ByteArrayOutputStream()\n"
    "    bmp.compress(Bitmap.CompressFormat.JPEG, 85, baos)\n"
    "    return baos.toByteArray()\n"
    "}\n"
)

_BIN_OPEN_ONLY = _BIN_FIXED.replace(
    "    val out = ctx.contentResolver.openOutputStream(uri) ?: throw IOException(\"null\")\n",
    "    ctx.contentResolver.openOutputStream(uri)?.use { out -> out.write(1) }\n",
)

def _bin_fixture(src):
    root = tempfile.mkdtemp(prefix="binwrite_")
    rel = os.path.join(SRC_SUBDIR, "probe", "PngWrite.kt")
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(src)
    return root

def binary_write_self_test():
    """Each arm must flip exactly the criterion built for it."""
    failures = []

    def run(src):
        root = _bin_fixture(src)
        try:
            return scan_binary_write_verdict(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def arm(name, src, expect, forbid=()):
        v, _sites = run(src)
        blob = " ".join(v)
        ok = all(tok in blob for tok in expect) and not any(tok in blob for tok in forbid)
        if not ok:
            failures.append("B-01 %s: want %s / forbid %s, got %s"
                            % (name, list(expect), list(forbid), v))
        else:
            print("[ok] B-01 %s" % name)

    arm("the routed, null-checked write passes", _BIN_FIXED, ())
    arm("the statement-position verdict and the swallowed null stream are both caught",
        _BIN_PRE, ("[B1]", "[B2]"))
    arm("an in-memory ByteArrayOutputStream destination is out of scope",
        _BIN_IN_MEMORY, (), ("[B1]", "[B2]"))
    arm("a routed verdict with a swallowed null stream trips only [B2]",
        _BIN_OPEN_ONLY, ("[B2]",), ("[B1]",))

    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    print("✅ binary-write self-test has teeth: B1 (verdict not routed through saveOutcome) "
          "and B2 (null openOutputStream swallowed) each flip red on exactly their own "
          "breaker, and the in-memory destination class stays green")
    return 0

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--self-test":
        rc = self_test()
        rc2 = drain_self_test()
        rc3 = staged_self_test()
        rc4 = share_stream_self_test()
        rc5 = binary_write_self_test()
        return 1 if (rc or rc2 or rc3 or rc4 or rc5) else 0
    violations = scan(args[0])
    # [B-04 deep-audit-1037] queue-drain persistence section. Its findings are
    # pre-formatted `file:line  [criterion] reason` strings, so they are
    # reported in their own block — the watchlist tuple format above is left
    # exactly as it was.
    drain_violations = scan_queue_drain(args[0])
    # [§1042-A-07 deep-audit-1042] staged-bytes ledger section. Same pre-formatted
    # file:line [criterion] reason contract as the drain section above.
    staged_violations = scan_staged_bytes(args[0])
    # [§1044-A08 deep-audit-1044] share inbound stream section: null stream must
    # not reach the composer as a ghost attachment. Same pre-formatted
    # `file:line [criterion] reason` contract as the sections above.
    stream_violations = scan_share_stream_guard(args[0])
    # [deep-audit-1047 B-01] binary-write verdict section (compress /
    # openOutputStream). Same pre-formatted `file:line [criterion] reason`
    # contract as the sections above.
    binary_violations, _binary_sites = scan_binary_write_verdict(args[0])
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
    if staged_violations:
        print("❌ 分享暂存字节账本段：计数器钳制/守卫不变量被打破（A-07）：")
        for msg in staged_violations:
            print("  %s" % msg)
        print("")
        print("  共 %d 处。" % len(staged_violations))
    if stream_violations:
        print("❌ 分享入站流段：null 流会被当成已暂存文件（A-08）：")
        for msg in stream_violations:
            print("  %s" % msg)
        print("")
        print("  共 %d 处。" % len(stream_violations))
    if binary_violations:
        print("❌ 二进制写入判定段：compress/openOutputStream 的失败结果被丢弃（B-01）：")
        for msg in binary_violations:
            print("  %s" % msg)
        print("")
        print("  共 %d 处。" % len(binary_violations))
    if violations or drain_violations or staged_violations or stream_violations or binary_violations:
        return 1
    print(
        "✅ discarded-return guard: no watchlisted return value is dropped "
        "(%d watched function(s))" % len(WATCHLIST)
    )
    print("✅ discarded-return guard: queue-drain persistence section green")
    print("✅ discarded-return guard: share staged-bytes accounting section green")
    print("✅ discarded-return guard: share inbound stream section green (null stream never staged)")
    print("✅ discarded-return guard: binary-write verdict section green (compress verdict routed, "
          "no swallowed openOutputStream)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

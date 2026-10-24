#!/usr/bin/env python3
"""deep-audit-1021 FIX-H (ISSUE-11, P2) — MinisOpenUrlBroker scheme gate.

Pins the contract fixed by branch fix/da1021-open-url-broker-scheme for
terminal/MinisOpenUrlBroker.kt: BOTH entry points must funnel through the
single `isSupportedScheme` whitelist before anything lands in `_pendingUrl`.

Bug context (audit-0917 half-fix): `offer(uri: Uri)` published whatever a
caller handed it — no scheme check — while the `[audit-0917]` comment sitting
on `offer(uriString: String)` claimed "Same scheme gate as offer(Uri)" and
described the Uri overload accepting file:// / javascript: payloads. The
guard described by the comment had been added to neither overload's call
path as written: the comment described a defect that did not exist on the
String overload (which always had its own gate) and the Uri overload itself
stayed bare.

The fix delegates offer(String) → offer(Uri) so the whitelist is consulted
exactly once. The gate enforces:

  U1  the `offer(uri: Uri)` body consults `isSupportedScheme(` (a bare
      `_pendingUrl.value = uri` revert is RED);
  U2  single gate: the String overload either still gates on
      `isSupportedScheme(parsed.scheme)` or delegates via `offer(parsed)`,
      AND the file contains exactly ONE non-null `_pendingUrl.value =`
      write point (a second inline copy is the next drift site);
  U3  ordering: inside the `offer(uri: Uri)` body the scheme check appears
      BEFORE the `_pendingUrl.value = uri` assignment (an order mutation
      that publishes first is RED);
  U4  the whitelist itself is not weakened: `isSupportedScheme` still
      lowercases, fail-closes on null, and still lists http/https/about/minis.

deep-audit-1027 FIX-G (ISSUE-07, P2) added the consumer half of the same
contract, which lives in a different file:

  U5  `ui/terminal/TerminalScreen.kt`, inside its `LaunchedEffect(pendingUrl)`
      block, must call `MinisOpenUrlBroker.consume()` ONLY inside the
      `if (MinisOpenUrlBroker.isWebScheme(...))` branch. The fullscreen
      terminal only *presents* web schemes; consuming unconditionally ate
      `minis://` deep links / local-file previews before they could sit in
      the broker for the underlying ChatScreen (whose observer deliberately
      defers while `terminalVisible` is true and re-runs when it flips back
      to false). Anchors are fail-closed: a missing/ambiguous block is RED.
      The criterion is skipped when TerminalScreen.kt is absent (mini-tree
      fixtures that only stage the broker file).

deep-audit-1032 FIX-D (ISSUE-05, P1) added the two terminal deep-link entries,
which live in a third and fourth file:

  U6  `MainActivity.kt` — the runtime deep-link dispatcher's
      `is DeepLinkAction.OpenTerminal ->` branch must hand
      `Routes.terminal(…)` a session id that came out of the alias table
      (`ChatViewModelStore.resolveSessionId(…)` / `.activeSessionId`, directly
      or through a local val). A terminal session is not cosmetic: the id
      selects `minis-sessions/<sid>/` bind mounts and `MINIS_CHAT_SESSION_ID`,
      so a bare/absent id binds the terminal to the global workspace (or to the
      orphan `__new__<uuid>` draft key) while the chat it was opened from lives
      under the promoted id. The criterion is anchored on the branch and the
      `Routes.terminal(` call inside it, never on a specific argument spelling:
      `sessionId = <resolved>` and positional `<resolved>` both count.
  U7  `AppNavigation.kt` — same rule for the cold-start dispatcher's
      `is DeepLinkAction.OpenTerminal ->` branch (the same user action's second
      entry point; asymmetry here was the reported "cold has, hot hasn't" gap —
      in the tree both were missing). `.activeSessionId` is accepted because its
      getter already resolves through the alias table.

Both are skipped when their file is absent (mini-tree fixtures).

meta-audit-1024 FIX-M1 (external witness, axis=dataflow) added the *effect*
half of the U1/U3 pair, which U1+U3 alone could not see:

  U8  the `isSupportedScheme(` guard inside `offer(uri: Uri)` must TERMINATE
      control flow, not merely be mentioned. Two accepted shapes:
        (a) negative early exit — an `if (!isSupportedScheme(…))` /
            `isSupportedScheme(…).not()` / `… == false` arm whose body ends
            the function (a `return`, a labelled `return@offer`, or a
            `throw`); negation may also come from a `!` wrapping parentheses.
        (b) positive wrapping — the `_pendingUrl.value = uri` publish sits
            inside the body of an `if (isSupportedScheme(…)) { … }`.
      A negative arm whose consequence is `Unit` / an empty block / any
      non-terminating statement is RED even though `idx_gate < idx_write`
      (U3) and `"isSupportedScheme(" in body` (U1) both still hold — that
      exact patch was externally witnessed keeping this gate green while
      republishing `file://` / `javascript:` / `content://` Uris.
      Fail-closed: unparseable braces/parens or a guard with no `if` at all
      is a violation, never a skip.

Usage: python3 open_url_broker_scheme_check.py [ROOT]   (ROOT = repo root)
       python3 open_url_broker_scheme_check.py --selftest
Exit:  0 = gate green, 1 = violations found (or selftest failures).
"""
import os
import re
import sys

BROKER_REL = "src/android/app/src/main/java/com/rikkaminis/app/terminal/MinisOpenUrlBroker.kt"
TERMINAL_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/terminal/TerminalScreen.kt"


def strip_comments(src):
    """Remove // line comments and /* */ block comments (KDoc included).

    String-literal aware: `//` inside a string (e.g. "file://") must NOT be
    treated as a comment — the whitelist literals live on code lines and
    would otherwise be truncated.
    """
    out = []
    in_block = False
    for line in src.splitlines():
        res = []
        i = 0
        n = len(line)
        quote = None
        while i < n:
            if in_block:
                j = line.find("*/", i)
                if j < 0:
                    i = n
                else:
                    in_block = False
                    i = j + 2
                continue
            c = line[i]
            if quote is not None:
                res.append(c)
                if c == "\\" and i + 1 < n:
                    res.append(line[i + 1])
                    i += 2
                    continue
                if c == quote:
                    quote = None
                i += 1
                continue
            if line.startswith("//", i):
                break
            if line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            if c in ('"', "'"):
                quote = c
            res.append(c)
            i += 1
        out.append("".join(res))
    return "\n".join(out)


def extract_fun(code, decl_re):
    """Extract the body (incl. braces) of the function matching decl_re.

    Fail-closed: returns None when the declaration is missing or ambiguous
    (multiple matches) — the caller reports a violation instead of guessing.
    Brace matching is string-aware via the comment-stripped code.
    """
    m = decl_re.search(code)
    if not m:
        return None
    if decl_re.search(code, m.end()):
        return None  # ambiguous
    # The declaration regex ends with `{` (the function's opening brace) —
    # start brace matching AT it, not at the next `{` (which would live
    # inside the body or a later function and truncate the extract).
    i = m.end() - 1
    if code[i] != "{":
        i = code.find("{", m.end())
        if i < 0:
            return None
    depth = 0
    quote = None
    n = len(code)
    while i < n:
        c = code[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return code[m.start():i + 1]
        i += 1
    return None


RE_OFFER_URI = re.compile(r"\bfun\s+offer\(\s*uri:\s*Uri\s*\)\s*\{")
RE_OFFER_STR = re.compile(r"\bfun\s+offer\(\s*uriString:\s*String\s*\)\s*\{")
RE_IS_SUP = re.compile(r"\bfun\s+isSupportedScheme\(\s*scheme:\s*String\?\s*\)\s*:\s*Boolean\s*\{")
RE_WRITE = re.compile(r"_pendingUrl\.value\s*=\s*(?=\S)(?!null\b)")


def analyse(code_raw):
    """Return [(criterion, ok, detail)]."""
    code = strip_comments(code_raw)
    results = []

    body_uri = extract_fun(code, RE_OFFER_URI)
    if body_uri is None:
        results.append(("U1 offer(uri: Uri) present and unambiguous", False,
                        "declaration missing or ambiguous — fail-closed"))
        return results

    # --- U1: the Uri overload consults the whitelist ---
    ok1 = "isSupportedScheme(" in body_uri
    results.append(("U1 offer(uri: Uri) consults isSupportedScheme", ok1,
                    "scheme gate missing from the Uri overload" if not ok1 else "gate present"))

    # --- U2: single gate, exactly one non-null publish point ---
    body_str = extract_fun(code, RE_OFFER_STR)
    if body_str is None:
        results.append(("U2a offer(uriString: String) present and unambiguous", False,
                        "declaration missing or ambiguous — fail-closed"))
        return results
    gated = "isSupportedScheme(parsed.scheme)" in body_str
    delegates = re.search(r"\boffer\(\s*parsed\s*\)", body_str) is not None
    results.append(("U2a offer(String) funnels through the same gate",
                    gated or delegates,
                    "must gate on isSupportedScheme(parsed.scheme) or delegate to offer(parsed)"))
    n_writes = len(RE_WRITE.findall(code))
    results.append(("U2b exactly one non-null _pendingUrl.value write point", n_writes == 1,
                    "found %d (single shared gate requires exactly 1)" % n_writes))

    # --- U3: publish must come after the check ---
    idx_gate = body_uri.find("isSupportedScheme(")
    m_write = RE_WRITE.search(body_uri)
    idx_write = m_write.start() if m_write else -1
    ok3 = idx_gate >= 0 and idx_write >= 0 and idx_gate < idx_write
    results.append(("U3 scheme check precedes the _pendingUrl publish", ok3,
                    "gate idx=%d write idx=%d" % (idx_gate, idx_write)))

    # --- U4: whitelist itself not weakened ---
    body_sup = extract_fun(code, RE_IS_SUP)
    if body_sup is None:
        results.append(("U4 isSupportedScheme present and unambiguous", False,
                        "whitelist missing or ambiguous — fail-closed"))
        return results
    ok_lower = ".lowercase()" in body_sup
    ok_null = re.search(r"\?\s*:\s*return\s+false", body_sup) is not None
    ok_list = all(s in body_sup for s in ('"http"', '"https"', '"about"', '"minis"'))
    ok4 = ok_lower and ok_null and ok_list
    results.append(("U4 whitelist not weakened (lowercase + null fail-close + 4 schemes)", ok4,
                    "lowercase=%s null-close=%s schemes=%s" % (ok_lower, ok_null, ok_list)))

    # --- U8: the guard must terminate control flow, not merely be mentioned ---
    results.extend(analyse_guard_termination(body_uri, RE_WRITE.search(body_uri)))
    return results


RE_IF = re.compile(r"\bif\s*\(")
# A terminator: `return`, a labelled `return@x`, or `throw` — as the head of a
# statement. Deliberately not a bare `Unit` / assignment / other call.
RE_TERMINATOR = re.compile(r"\b(?:return(?:@[A-Za-z_][A-Za-z0-9_]*)?|throw)\b")
# Negation spellings accepted on the guard call: `!…`, `!(…)`, `….not()`,
# `… == false` (the common Kotlin equivalents; keep the list narrow — the
# point is to accept real code, not to enumerate every exotic form).
RE_NOT_SUFFIX = re.compile(r"\.not\s*\(")
RE_FALSE_SUFFIX = re.compile(r"==\s*false|!=\s*true")


def _skip_ws(text, i):
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    return i


def _preceded_by_not(cond, call_start):
    """True when the call at call_start is negated (`!…`, `!(…)`, `….not()`,
    `… == false`, `… != true`)."""
    j = call_start - 1
    while j >= 0 and cond[j] in " \t\r\n(":
        j -= 1
    if j >= 0 and cond[j] == "!":
        return True
    close = -1
    op = call_start + len("isSupportedScheme")
    while op < len(cond) and cond[op] in " \t\r\n":
        op += 1
    if op < len(cond) and cond[op] == "(":
        close = match_delim(cond, op, "(", ")")
    if close < 0:
        return False
    tail = cond[close + 1:]
    if RE_NOT_SUFFIX.match(tail.lstrip()):
        return True
    if RE_FALSE_SUFFIX.match(tail.lstrip()):
        return True
    return False


def _body_terminates(text):
    """True when the branch text contains a top-level `return` / `return@x` /
    `throw` (a guard arm that ends the function). String-aware, depth-aware:
    a `return` nested inside a lambda of the arm does not count."""
    depth = 0
    quote = None
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif depth == 0:
            prev = text[i - 1] if i > 0 else ""
            if (c == "r" and text.startswith("return", i)
                    and not (prev.isalnum() or prev == "_")):
                return True
            if (c == "t" and text.startswith("throw", i)
                    and not (prev.isalnum() or prev == "_")):
                return True
        i += 1
    return False


def _consequent_span(body, close_idx):
    """Span (start, end_exclusive) of the `if` consequence after the closing
    paren at close_idx — a braced block or a single statement. Returns
    (start, end) or None when there is no consequence (fail-closed at the
    caller)."""
    k = _skip_ws(body, close_idx + 1)
    if k >= len(body):
        return None
    if body[k] == "{":
        e = match_brace(body, k)
        if e < 0:
            return None
        return (k, e + 1)
    e = k
    while e < len(body) and body[e] not in "\n;":
        e += 1
    return (k, e)


def _arm_inner(consequent):
    """The statements of a guard arm: the braced block's interior, or the bare
    single-statement arm as-is."""
    text = consequent.strip()
    if text.startswith("{") and text.endswith("}"):
        return text[1:-1]
    return text


def analyse_guard_termination(body, m_write):
    """U8 — the isSupportedScheme guard must terminate control flow.

    Green iff at least one guard `if` exists and it either
      (a) negates the call and its consequence terminates (`return`/`throw`),
          with the publish NOT inside that (dead) consequence, or
      (b) keeps the call positive and the publish sits inside its block.
    Anything else (Unit / empty block / call not under any `if` / unbalanced
    braces) is a violation — fail-closed.
    """
    label = "U8 isSupportedScheme guard terminates control flow (no Unit/no-op arm)"
    sites = list(re.finditer(r"isSupportedScheme\s*\(", body))
    if not sites:
        # No guard call at all: U1 already owns that shape; keep the criterion
        # list stable so fail-closed fixtures cannot double-count it.
        return []
    idx_write = m_write.start() if m_write else -1
    problems = []
    protected = False
    for m in RE_IF.finditer(body):
        open_idx = m.end() - 1
        close_idx = match_delim(body, open_idx, "(", ")")
        if close_idx < 0:
            problems.append("unbalanced guard condition at offset %d — fail-closed" % open_idx)
            continue
        cond = body[open_idx:close_idx + 1]
        cm = re.search(r"isSupportedScheme\s*\(", cond)
        if not cm:
            continue
        negated = _preceded_by_not(cond, cm.start())
        span = _consequent_span(body, close_idx)
        if span is None:
            problems.append("guard at offset %d has no parsable consequence — fail-closed"
                            % open_idx)
            continue
        bs, be = span
        consequent = body[bs:be]
        if negated:
            # The dead arm must end the function; a publish inside it would be
            # reachable exactly when the scheme is NOT supported.
            inner = _arm_inner(consequent)
            terminates = _body_terminates(inner)
            publish_inside = idx_write >= 0 and bs <= idx_write < be
            if terminates and not publish_inside:
                protected = True
            elif publish_inside:
                problems.append("negated guard arm (offset %d) PUBLISHES instead of "
                                "rejecting — inverted guard" % open_idx)
            else:
                problems.append("negated guard arm (offset %d) does not terminate control "
                                "flow (Unit/no-op/other statement)" % open_idx)
        else:
            wraps = idx_write >= 0 and bs <= idx_write < be
            if wraps:
                protected = True
            else:
                problems.append("positive guard arm (offset %d) neither wraps the publish "
                                "nor terminates — publish reachable without the gate"
                                % open_idx)
    if not problems and not protected:
        problems.append("no isSupportedScheme guard terminates control flow or wraps the "
                        "publish — fail-closed")
    return [(label, not problems, "; ".join(problems) if problems
             else "guard arm terminates / wraps the publish")]

# ------------------------------------------------------- TerminalScreen (U5) ---
RE_LAUNCHED_PENDING = re.compile(r"LaunchedEffect\(\s*pendingUrl\s*\)\s*\{")
RE_WEB_SCHEME_IF = re.compile(r"if\s*\(\s*MinisOpenUrlBroker\.isWebScheme\(")
CONSUME_CALL = "MinisOpenUrlBroker.consume()"

def match_delim(code, open_idx, open_ch, close_ch):
    """Index of the close_ch matching the open_ch at open_idx (string-aware)."""
    depth = 0
    i = open_idx
    n = len(code)
    quote = None
    while i < n:
        c = code[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1

def match_brace(code, open_idx):
    """Index of the '}' matching the '{' at open_idx (string-aware), else -1."""
    return match_delim(code, open_idx, "{", "}")

def analyse_terminal(code_raw):
    """U5 — the fullscreen terminal may only consume URLs it actually presents."""
    code = strip_comments(code_raw)

    m = RE_LAUNCHED_PENDING.search(code)
    if not m:
        return [("U5a LaunchedEffect(pendingUrl) block present", False,
                 "anchor block missing — fail-closed")]
    end_idx = match_brace(code, m.end() - 1)
    if end_idx < 0:
        return [("U5a LaunchedEffect(pendingUrl) block present", False,
                 "unbalanced block braces — fail-closed")]
    block = code[m.end() - 1:end_idx + 1]
    results = [("U5a LaunchedEffect(pendingUrl) block present", True, "block found")]

    mi = RE_WEB_SCHEME_IF.search(block)
    if not mi:
        results.append(("U5b isWebScheme branch inside the block", False,
                        "no `if (MinisOpenUrlBroker.isWebScheme(` anchor — fail-closed"))
        return results
    oi = block.find("{", mi.end())
    if oi < 0:
        results.append(("U5b isWebScheme branch inside the block", False,
                        "gate without a body — fail-closed"))
        return results
    ei = match_brace(block, oi)
    if ei < 0:
        results.append(("U5b isWebScheme branch inside the block", False,
                        "unbalanced gate braces — fail-closed"))
        return results
    results.append(("U5b isWebScheme branch inside the block", True, "gate body found"))

    sites = []
    start = 0
    while True:
        j = block.find(CONSUME_CALL, start)
        if j < 0:
            break
        sites.append(j)
        start = j + 1
    if not sites:
        results.append(("U5c consume() only inside the isWebScheme branch", False,
                        "no consume() call in the LaunchedEffect(pendingUrl) block"))
    else:
        outside = [j for j in sites if not (oi < j < ei)]
        results.append(("U5c consume() only inside the isWebScheme branch", not outside,
                        "consume() sites=%d outside-gate=%d" % (len(sites), len(outside))))
    return results


# ------------------------------------------- terminal deep-link entries (U6/U7) ---
REL_ACTIVITY = "src/android/app/src/main/java/com/rikkaminis/app/MainActivity.kt"
REL_NAV = "src/android/app/src/main/java/com/rikkaminis/app/ui/navigation/AppNavigation.kt"

# Every terminal route goes through `Routes.terminal(`, whatever the caller.
RE_TERMINAL_CALL = "Routes.terminal("
# The deep-link dispatch arm (both files carry exactly one).
RE_OPEN_TERMINAL_BRANCH = re.compile(r"is\s+DeepLinkAction\.OpenTerminal\s*->\s*\{")
# A *resolved* session id: the public alias hop, or the store's already-resolved
# "chat currently on screen" seam. Deliberately not a bare identifier — that is
# exactly the defect (draft keys reaching the terminal as-is).
RE_RESOLVED_EXPR = re.compile(
    r"ChatViewModelStore\s*\.\s*resolveSessionId\s*\(|ChatViewModelStore\s*\.\s*activeSessionId"
)
RE_VAL_BIND = re.compile(r"^\s*(?:val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=")
# Continuation heuristics for a multi-line binding (`val id = a\n    ?.let { … }`).
# Same rule as the 1031-C extractor: keep reading while the previous line ends
# with an operator or the next line starts with one.
_CONT_SUFFIX = ("=", "?.", ".", ",", "&&", "||", "?:", "+")
_CONT_PREFIX = ("?.", ".", "?:", "&&", "||", "->", "+")


def _binding_statement(lines, i):
    """`lines[i]` plus its continuation lines, joined (comment-free input)."""
    out = [lines[i]]
    while i + 1 < len(lines):
        cur = out[-1].rstrip()
        nxt = lines[i + 1].strip()
        if not nxt:
            break
        if cur.endswith(_CONT_SUFFIX) or nxt.startswith(_CONT_PREFIX):
            out.append(nxt)
            i += 1
            continue
        break
    return " ".join(out)


def resolved_local_names(branch):
    """Local vals bound from a resolved-session expression inside `branch`."""
    names = set()
    lines = branch.splitlines()
    for i, line in enumerate(lines):
        m = RE_VAL_BIND.match(line)
        if not m:
            continue
        if RE_RESOLVED_EXPR.search(_binding_statement(lines, i)):
            names.add(m.group(1))
    return names


def analyse_terminal_deeplink(code_raw, label):
    """U6/U7 — a terminal deep-link arm must pass a RESOLVED session id.

    Bound to the decision point (which value the terminal route receives), not
    to one argument spelling: the resolved id may be passed by name
    (`sessionId = <id>`) or positionally, and may be a local val.
    """
    code = strip_comments(code_raw)
    matches = list(RE_OPEN_TERMINAL_BRANCH.finditer(code))
    if len(matches) > 1:
        return [("%sa OpenTerminal branch present" % label, False,
                 "more than one `is DeepLinkAction.OpenTerminal ->` arm — ambiguous, fail-closed")]
    if not matches:
        return [("%sa OpenTerminal branch present" % label, False,
                 "`is DeepLinkAction.OpenTerminal ->` not found — fail-closed")]
    m = matches[0]
    end = match_brace(code, m.end() - 1)
    if end < 0:
        return [("%sa OpenTerminal branch present" % label, False,
                 "unbalanced branch braces — fail-closed")]
    branch = code[m.end() - 1:end + 1]
    results = [("%sa OpenTerminal branch present" % label, True, "branch found")]

    idx = branch.find(RE_TERMINAL_CALL)
    if idx < 0:
        results.append(("%sb branch navigates to the terminal route" % label, False,
                        "no `Routes.terminal(` call inside the branch"))
        return results
    open_idx = idx + len(RE_TERMINAL_CALL) - 1
    close_idx = match_delim(branch, open_idx, "(", ")")
    if close_idx < 0:
        results.append(("%sb branch navigates to the terminal route" % label, False,
                        "unbalanced `Routes.terminal(` call — fail-closed"))
        return results
    args = branch[open_idx:close_idx + 1]
    results.append(("%sb branch navigates to the terminal route" % label, True, "call found"))

    names = resolved_local_names(branch)
    via_call = RE_RESOLVED_EXPR.search(args) is not None
    via_name = False
    if names:
        via_name = re.search(r"\b(?:%s)\b" % "|".join(re.escape(n) for n in sorted(names)), args) is not None
    ok = via_call or via_name
    results.append((
        "%sc terminal sessionId comes from a resolved source" % label, ok,
        "`Routes.terminal(...)` args carry neither a ChatViewModelStore resolve call "
        "(alias hop / activeSessionId) nor a local val bound from one — a bare or "
        "absent id binds the terminal to the draft key / global workspace. args=%r"
        % args.strip()[:160]
    ))
    return results



GOOD = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        if (!isSupportedScheme(uri.scheme)) return
        _pendingUrl.value = uri
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

BAD_NO_GATE = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        _pendingUrl.value = uri
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

BAD_ORDER = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        _pendingUrl.value = uri
        if (!isSupportedScheme(uri.scheme)) return
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

BAD_SECOND_GATE = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        if (!isSupportedScheme(uri.scheme)) return
        _pendingUrl.value = uri
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        // drift: a second inline gate + a second publish point
        if (!isSupportedScheme(parsed.scheme)) return
        _pendingUrl.value = parsed
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""


# ---- meta-audit-1024 FIX-M1 witness arms (U8: guard must terminate) ----------
# The exact external witness: `return` -> `Unit` keeps U1 (substring), U2b (one
# publish point), U3 (gate before publish) and U4 all green, yet the guard is a
# no-op and every scheme lands in _pendingUrl.
BAD_GUARD_UNIT = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        if (!isSupportedScheme(uri.scheme)) Unit
        _pendingUrl.value = uri
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

BAD_GUARD_EMPTY_BLOCK = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        if (!isSupportedScheme(uri.scheme)) { }
        _pendingUrl.value = uri
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

# Inverted guard: the rejected branch does the publishing.
BAD_GUARD_INVERTED = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        if (!isSupportedScheme(uri.scheme)) { _pendingUrl.value = uri }
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

# Precision: a multi-statement guard arm that still ends the function is fine.
GOOD_GUARD_BLOCK_MULTI = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        if (!isSupportedScheme(uri.scheme)) {
            Log.w(TAG, "unsupported scheme: %s", uri.scheme)
            return
        }
        _pendingUrl.value = uri
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

GOOD_GUARD_THROW = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        if (!isSupportedScheme(uri.scheme)) throw IllegalArgumentException("scheme")
        _pendingUrl.value = uri
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

# Precision: the accepted positive-wrapping shape (same decision point, other
# legal spelling).
GOOD_GUARD_POSITIVE_WRAP = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        if (isSupportedScheme(uri.scheme)) {
            _pendingUrl.value = uri
        }
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""

# Precision: `….not()` / `== false` negations are also real guards.
GOOD_GUARD_NOT_CALL = """package probe
class B {
    private val _pendingUrl = MutableStateFlow<Uri?>(null)
    fun offer(uri: Uri) {
        if (isSupportedScheme(uri.scheme).not()) return
        _pendingUrl.value = uri
    }
    fun offer(uriString: String) {
        val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
        offer(parsed)
    }
    fun isSupportedScheme(scheme: String?): Boolean {
        val s = scheme?.lowercase() ?: return false
        return s == "http" || s == "https" || s == "about" || s == "minis"
    }
}
"""


GOOD_TERMINAL = """package probe
@Composable
fun TerminalScreen(pendingUrl: Uri?) {
    var previewUrl by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(pendingUrl) {
        val uri = pendingUrl ?: return@LaunchedEffect
        if (MinisOpenUrlBroker.isWebScheme(uri.scheme)) {
            previewUrl = uri.toString()
            MinisOpenUrlBroker.consume()
        }
    }
}
"""

BAD_TERMINAL_OUTSIDE = """package probe
@Composable
fun TerminalScreen(pendingUrl: Uri?) {
    var previewUrl by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(pendingUrl) {
        val uri = pendingUrl ?: return@LaunchedEffect
        if (MinisOpenUrlBroker.isWebScheme(uri.scheme)) {
            previewUrl = uri.toString()
        }
        MinisOpenUrlBroker.consume()
    }
}
"""

BAD_TERMINAL_NO_CONSUME = """package probe
@Composable
fun TerminalScreen(pendingUrl: Uri?) {
    var previewUrl by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(pendingUrl) {
        val uri = pendingUrl ?: return@LaunchedEffect
        if (MinisOpenUrlBroker.isWebScheme(uri.scheme)) {
            previewUrl = uri.toString()
        }
    }
}
"""

BAD_TERMINAL_NO_ANCHOR = """package probe
@Composable
fun TerminalScreen(pendingUrl: Uri?) {
    val pendingUrl2 = pendingUrl
}
"""

GOOD_TERMINAL_COMMENT_ONLY = """package probe
// LaunchedEffect(pendingUrl) {
//     if (MinisOpenUrlBroker.isWebScheme(uri.scheme)) {
//         MinisOpenUrlBroker.consume()
//     }
// }
@Composable
fun TerminalScreen(pendingUrl: Uri?) {
    val pendingUrl2 = pendingUrl
}
"""

GOOD_ACTIVITY_TERMINAL = """package probe
class MainActivity {
    private val currentChatSessionId: String? = null
    fun handleDeepLink(action: DeepLinkAction) {
        when (action) {
            is DeepLinkAction.OpenTerminal -> {
                val terminalSessionId = currentChatSessionId
                    ?.let { ChatViewModelStore.resolveSessionId(it) }
                nav.navigate(Routes.terminal(action.initCommand, sessionId = terminalSessionId))
            }
            is DeepLinkAction.OpenSession -> {
                nav.navigate(Routes.chat(action.sessionId))
            }
        }
    }
}
"""

# Precision arm: same decision point, a different spelling (positional argument
# + local val), plus an unrelated terminal navigation elsewhere in the file.
GOOD_ACTIVITY_TERMINAL_OTHER_FORM = """package probe
class MainActivity {
    private val currentChatSessionId: String? = null
    fun handleDeepLink(action: DeepLinkAction) {
        when (action) {
            is DeepLinkAction.OpenTerminal -> {
                val id = currentChatSessionId?.let { ChatViewModelStore.resolveSessionId(it) }
                if (id != null) nav.navigate(Routes.terminal(action.initCommand, id))
            }
            else -> {}
        }
    }
    fun unrelated() {
        nav.navigate(Routes.terminal(cmd))
    }
}
"""

BAD_ACTIVITY_TERMINAL_BARE = """package probe
class MainActivity {
    fun handleDeepLink(action: DeepLinkAction) {
        when (action) {
            is DeepLinkAction.OpenTerminal -> {
                nav.navigate(Routes.terminal(action.initCommand))
            }
        }
    }
}
"""

BAD_ACTIVITY_TERMINAL_UNUSED_RESOLVE = """package probe
class MainActivity {
    fun handleDeepLink(action: DeepLinkAction) {
        when (action) {
            is DeepLinkAction.OpenTerminal -> {
                val unused = ChatViewModelStore.resolveSessionId(action.sessionId)
                nav.navigate(Routes.terminal(action.initCommand, sessionId = action.sessionId))
            }
        }
    }
}
"""

BAD_ACTIVITY_TERMINAL_AMBIGUOUS = """package probe
class MainActivity {
    fun handleDeepLink(action: DeepLinkAction) {
        when (action) {
            is DeepLinkAction.OpenTerminal -> {
                val id = ChatViewModelStore.resolveSessionId(action.sessionId)
                nav.navigate(Routes.terminal(action.initCommand, sessionId = id))
            }
            is DeepLinkAction.OpenTerminal -> {
                nav.navigate(Routes.terminal(action.initCommand))
            }
        }
    }
}
"""

BAD_NAV_TERMINAL_MISSING_CALL = """package probe
fun AppNavigation(initialDeepLink: DeepLinkAction) {
    when (initialDeepLink) {
        is DeepLinkAction.OpenTerminal -> {
            navController.safeNavigate()
        }
    }
}
"""

GOOD_NAV_TERMINAL = """package probe
fun AppNavigation(initialDeepLink: DeepLinkAction) {
    when (initialDeepLink) {
        is DeepLinkAction.OpenTerminal -> {
            navController.safeNavigate(
                Routes.terminal(
                    initialDeepLink.initCommand,
                    sessionId = ChatViewModelStore.activeSessionId,
                ),
            )
        }
        is DeepLinkAction.OpenSession -> {
            navController.safeNavigate(Routes.chat(initialDeepLink.sessionId))
        }
    }
}
"""

def selftest():
    failures = []

    def expect(cond, what):
        if not cond:
            failures.append(what)

    def as_dict(res):
        return dict((c, ok) for c, ok, _ in res)

    # good fixture: everything green
    good = as_dict(analyse(GOOD))
    expect(len(good) == 6, "good fixture should produce 6 criteria, got %d" % len(good))
    for c, ok in good.items():
        expect(ok, "good fixture must pass %s" % c)

    # bad1: gate removed from offer(Uri) (the original bug) — U1+U3 red, rest OK
    d = as_dict(analyse(BAD_NO_GATE))
    for c, ok in d.items():
        if c.startswith("U1") or c.startswith("U3"):
            expect(not ok, "BAD_NO_GATE %s must be RED" % c)
        else:
            expect(ok, "BAD_NO_GATE %s must stay OK" % c)

    # bad2: publish before the check (order mutation) — U3 red, U1/U2 stay OK
    d = as_dict(analyse(BAD_ORDER))
    expect(not d["U3 scheme check precedes the _pendingUrl publish"],
           "BAD_ORDER U3 must be RED")
    expect(d["U1 offer(uri: Uri) consults isSupportedScheme"],
           "BAD_ORDER U1 must stay OK (gate text still present)")
    expect(d["U2b exactly one non-null _pendingUrl.value write point"],
           "BAD_ORDER U2b must stay OK")

    # bad3: second inline gate + second publish point — U2b red (drift catcher)
    d = as_dict(analyse(BAD_SECOND_GATE))
    expect(not d["U2b exactly one non-null _pendingUrl.value write point"],
           "BAD_SECOND_GATE U2b must be RED (two publish points)")

    # fail-closed: missing file / garbage input must NOT read as green
    d = as_dict(analyse("class B { }"))
    expect(len(d) == 1 and not d["U1 offer(uri: Uri) present and unambiguous"],
           "garbage input must fail-closed on U1")

    # ---- U8 (guard must TERMINATE control flow) — the external witness ----
    U8 = "U8 isSupportedScheme guard terminates control flow (no Unit/no-op arm)"

    d = as_dict(analyse(BAD_GUARD_UNIT))
    expect(not d[U8], "BAD_GUARD_UNIT U8 must be RED (return mutated to Unit: the "
                      "guard is a no-op, every scheme reaches _pendingUrl)")
    # ...and the OLD criteria must stay green — that is precisely why the old
    # gate was blind to this patch.
    for c, ok in d.items():
        if c == U8:
            continue
        expect(ok, "BAD_GUARD_UNIT %s must stay OK (old criteria cannot see it)" % c)

    d = as_dict(analyse(BAD_GUARD_EMPTY_BLOCK))
    expect(not d[U8], "BAD_GUARD_EMPTY_BLOCK U8 must be RED (empty guard arm)")
    expect(d["U3 scheme check precedes the _pendingUrl publish"],
           "BAD_GUARD_EMPTY_BLOCK U3 must stay OK (order is unchanged)")

    d = as_dict(analyse(BAD_GUARD_INVERTED))
    expect(not d[U8], "BAD_GUARD_INVERTED U8 must be RED (rejected arm publishes)")

    g = as_dict(analyse(GOOD_GUARD_BLOCK_MULTI))
    expect(len(g) == 6 and all(g.values()),
           "GOOD_GUARD_BLOCK_MULTI must be fully green (log + return arm): %s"
           % [c for c, ok in g.items() if not ok])

    g = as_dict(analyse(GOOD_GUARD_THROW))
    expect(len(g) == 6 and all(g.values()),
           "GOOD_GUARD_THROW must be fully green (throw arm): %s"
           % [c for c, ok in g.items() if not ok])

    g = as_dict(analyse(GOOD_GUARD_POSITIVE_WRAP))
    expect(len(g) == 6 and all(g.values()),
           "GOOD_GUARD_POSITIVE_WRAP must be fully green (publish inside the gate): %s"
           % [c for c, ok in g.items() if not ok])

    g = as_dict(analyse(GOOD_GUARD_NOT_CALL))
    expect(len(g) == 6 and all(g.values()),
           "GOOD_GUARD_NOT_CALL must be fully green (.not() negation): %s"
           % [c for c, ok in g.items() if not ok])

    # ---- U5 (TerminalScreen consumer placement) ----
    tgood = as_dict(analyse_terminal(GOOD_TERMINAL))
    expect(len(tgood) == 3, "U5 good fixture should produce 3 criteria, got %d" % len(tgood))
    for c, ok in tgood.items():
        expect(ok, "U5 good fixture must pass %s" % c)

    d = as_dict(analyse_terminal(BAD_TERMINAL_OUTSIDE))
    expect(not d["U5c consume() only inside the isWebScheme branch"],
           "BAD_TERMINAL_OUTSIDE U5c must be RED (consume outside the gate)")
    expect(d["U5a LaunchedEffect(pendingUrl) block present"],
           "BAD_TERMINAL_OUTSIDE U5a must stay OK")
    expect(d["U5b isWebScheme branch inside the block"],
           "BAD_TERMINAL_OUTSIDE U5b must stay OK")

    d = as_dict(analyse_terminal(BAD_TERMINAL_NO_CONSUME))
    expect(not d["U5c consume() only inside the isWebScheme branch"],
           "BAD_TERMINAL_NO_CONSUME U5c must be RED (nothing consumes)")

    d = as_dict(analyse_terminal(BAD_TERMINAL_NO_ANCHOR))
    expect(len(d) == 1 and not d["U5a LaunchedEffect(pendingUrl) block present"],
           "BAD_TERMINAL_NO_ANCHOR must fail-closed on U5a")

    # KDoc quoting the anchor must not satisfy the criterion (comments stripped)
    d = as_dict(analyse_terminal(GOOD_TERMINAL_COMMENT_ONLY))
    expect(not d["U5a LaunchedEffect(pendingUrl) block present"],
           "comment-only anchor must NOT read as green (comments must be stripped)")

    # ---- U6/U7 (terminal deep-link arm carries a resolved session id) ----
    a = as_dict(analyse_terminal_deeplink(GOOD_ACTIVITY_TERMINAL, "U6"))
    expect(len(a) == 3, "U6 good fixture should produce 3 criteria, got %d" % len(a))
    for c, ok in a.items():
        expect(ok, "U6 good fixture must pass %s" % c)

    # precision: positional + local val + an unrelated terminal call elsewhere
    a = as_dict(analyse_terminal_deeplink(GOOD_ACTIVITY_TERMINAL_OTHER_FORM, "U6"))
    expect(len(a) == 3, "U6 precision fixture should produce 3 criteria, got %d" % len(a))
    for c, ok in a.items():
        expect(ok, "U6 precision fixture must pass %s (call form must not matter)" % c)

    # the pre-fix shape: no session id at all — U6c must be RED, U6a/b stay OK
    a = as_dict(analyse_terminal_deeplink(BAD_ACTIVITY_TERMINAL_BARE, "U6"))
    expect(not a["U6c terminal sessionId comes from a resolved source"],
           "BAD_ACTIVITY_TERMINAL_BARE U6c must be RED (no session id in the call)")
    expect(a["U6a OpenTerminal branch present"], "BAD_ACTIVITY_TERMINAL_BARE U6a must stay OK")
    expect(a["U6b branch navigates to the terminal route"],
           "BAD_ACTIVITY_TERMINAL_BARE U6b must stay OK")

    # a resolve call that is NOT the value passed must not read as green
    a = as_dict(analyse_terminal_deeplink(BAD_ACTIVITY_TERMINAL_UNUSED_RESOLVE, "U6"))
    expect(not a["U6c terminal sessionId comes from a resolved source"],
           "BAD_ACTIVITY_TERMINAL_UNUSED_RESOLVE U6c must be RED (resolve call is not the "
           "value handed to Routes.terminal)")

    # two OpenTerminal arms = ambiguous → fail-closed on U6a
    a = as_dict(analyse_terminal_deeplink(BAD_ACTIVITY_TERMINAL_AMBIGUOUS, "U6"))
    expect(len(a) == 1 and not a["U6a OpenTerminal branch present"],
           "BAD_ACTIVITY_TERMINAL_AMBIGUOUS must fail-closed on U6a")

    # no branch at all (garbage input) must not read as green
    a = as_dict(analyse_terminal_deeplink("class X { }", "U6"))
    expect(len(a) == 1 and not a["U6a OpenTerminal branch present"],
           "missing branch must fail-closed on U6a")

    a = as_dict(analyse_terminal_deeplink(BAD_NAV_TERMINAL_MISSING_CALL, "U7"))
    expect(not a["U7b branch navigates to the terminal route"],
           "BAD_NAV_TERMINAL_MISSING_CALL U7b must be RED (branch without the route call)")

    a = as_dict(analyse_terminal_deeplink(GOOD_NAV_TERMINAL, "U7"))
    expect(len(a) == 3, "U7 good fixture should produce 3 criteria, got %d" % len(a))
    for c, ok in a.items():
        expect(ok, "U7 good fixture must pass %s" % c)

    if failures:
        print("OPEN_URL_BROKER_SCHEME_SELFTEST=FAIL")
        for f in failures:
            print("  selftest: %s" % f)
        return 1
    print("OPEN_URL_BROKER_SCHEME_SELFTEST=PASS")
    return 0


def main():
    argv = [a for a in sys.argv[1:]]
    if "--selftest" in argv:
        sys.exit(selftest())
    root = argv[0] if argv else "."
    path = os.path.join(root, BROKER_REL)
    if not os.path.isfile(path):
        print("gate: broker file missing: %s" % path)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        code = f.read()
    results = analyse(code)
    tpath = os.path.join(root, TERMINAL_REL)
    if os.path.isfile(tpath):
        with open(tpath, "r", encoding="utf-8") as f:
            results = results + analyse_terminal(f.read())
    else:
        print("  [--] U5 skipped — %s not staged in this tree" % TERMINAL_REL)
    for label, rel in (("U6", REL_ACTIVITY), ("U7", REL_NAV)):
        dpath = os.path.join(root, rel)
        if os.path.isfile(dpath):
            with open(dpath, "r", encoding="utf-8") as f:
                results = results + analyse_terminal_deeplink(f.read(), label)
        else:
            print("  [--] %s skipped — %s not staged in this tree" % (label, rel))
    failed = [(c, d) for c, ok, d in results if not ok]
    for c, ok, d in results:
        print("  [%s] %s%s" % ("ok" if ok else "RED", c,
                               "" if ok else " — %s" % d))
    if failed:
        print("open_url_broker_scheme_check: %d violation(s)" % len(failed))
        sys.exit(1)
    print("open_url_broker_scheme_check: all %d criteria green" % len(results))
    sys.exit(0)


if __name__ == "__main__":
    main()

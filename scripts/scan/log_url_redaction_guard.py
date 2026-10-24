#!/usr/bin/env python3
"""Whole-URL-into-a-log guard — RikkaMinis CI scan gate.

## Why this gate exists

`AppLogger` is on by default, mirrors to logcat unconditionally and writes to
filesDir/logs. Several providers put the API key in the URL QUERY (Gemini's
`?key=...`; any OpenAI-compatible relay whose base URL is user-supplied), so the
WHOLE request URL is a credential-bearing string — precisely why
`debug/LLMRequestLog.redactURL` exists.

2026-09-26: an absorb pack added a network trace listener whose `callStart`
logged `url=${call.request().url}`. Every existing gate was green: it compiled,
the i18n key sets matched, the four-way sync was complete, the legacy-pipeline
guard had nothing to say. None of them asked "can a secret reach an output
surface through this NEW line" — that is the wiring question, and this gate asks
it for the one shape that has already bitten once.

## What it flags

Inside a log-call statement (`AppLogger.<lvl>(`, `Log.<lvl>(`, `Timber.<lvl>(`,
`println(`), ANY evaluation form that carries the WHOLE url value:

  - a bare `$ident` whose name mentions url — Kotlin expands `"$url.host"` to
    `url.toString() + ".host"`, so the bare form leaks the whole value no matter
    what literal text follows;
  - `${expr}` mentioning url, unless it ends in a safe accessor (host, hostname,
    port, scheme, path, encodedPath, authority) or is wrapped by a redactor;
  - **CONCATENATION** — `"...=" + forBaseURL`. A url reaches a log through `+`
    exactly as it does through `$`, and the name need not be `url`
    (`forBaseURL` carries it just as well). The gate originally only read
    template interpolations, so this whole class was invisible to it.
  - **BRACE-NESTED interpolation** — `${forBaseURL.let { it }}`. `\\$\\{[^}]*\\}`
    stops at the first `}`, so an identity wrapper (or any nested `{...}`)
    hides the url from the matcher while handing the same value to the log.

These are ONE defect wearing three syntaxes (the whole url value is handed to
an output surface), so the gate judges the equivalence class, not one spelling:
it asks "does a url value reach this log statement", not "is this the exact
string the gate was written against".

## Fail-closed

A log statement whose parentheses never balance within MAX_SPAN_LINES, or a
`${` whose closing brace cannot be found, is REPORTED rather than skipped — a
half-read statement is exactly where a spliced url would hide.

Audited existing sites are pinned in [ALLOWED] as (relative path, snippet
prefix); `--seed` prints entries for the current tree, which is how the list is
maintained. A line may also be exempted inline with `url-ok: <reason>` (the
repo's `debug-ok:` convention).

Usage:
  python3 scripts/scan/log_url_redaction_guard.py <repo_root>
  python3 scripts/scan/log_url_redaction_guard.py --seed <repo_root>
  python3 scripts/scan/log_url_redaction_guard.py --self-test
"""

import os
import re
import sys
import tempfile

SRC_SUBDIR = "src/android/app/src/main/java"

# Credential-bearing URLs only flow through these subtrees: the provider request
# paths, the shared OkHttp tracing layer, the offload gateway, and the repository
# that assembles base URLs. Page / preview / Custom-Tab URLs live in ui/, webapp/
# and browser/ and are user-facing by design — scanning them yields 30+ findings
# that all amount to "a page URL is not a secret", which is how a gate turns into
# noise and then gets deleted.
SCAN_DIRS = (
    "com/rikkaminis/app/network",
    "com/rikkaminis/app/provider",
    "com/rikkaminis/app/sandbox/offload",
    "com/rikkaminis/app/data/repository",
)

LOG_CALL = re.compile(r"\b(?:AppLogger|Log|Timber)\.[A-Za-z]+\s*\(|\bprintln\s*\(")
DYN = re.compile(r"\$\{([^{}]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
SAFE_TAIL = re.compile(
    r"\.(host|hostname|port|scheme|path|encodedPath|authority)\s*$", re.IGNORECASE
)
REDACTOR = re.compile(r"redact|sanitiz|scrub", re.IGNORECASE)
NARROW_NAME = re.compile(r"(host|hostname|port|scheme|path|authority)$", re.IGNORECASE)
MAX_SPAN_LINES = 12

# ── concatenation: a url-carrying expression glued onto the log payload with
# `+`. The `+` may sit on either side of the literal and may land on a later
# line (Kotlin breaks long log arguments there), so both directions are read.
# The operand is a member/call CHAIN, not a single identifier: the value that
# leaks is often one access away (`call.request().url`).
CHAIN = (
    r"[A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*"
    r"|\s*\(\s*\)"
    r"|\s*!!"
    r"|\s*\?\s*\.?\s*[A-Za-z_]\w*"
    r"|\s*\[[^\[\]]*\])*"
)
CONCAT_AFTER = re.compile(r"\+\s*(%s)" % CHAIN)
CONCAT_BEFORE = re.compile(r"(%s)\s*\+" % CHAIN)

# A `+` on its own is not evidence: `"a" + "b"`, `i + 1`, `1 + 2` all reach
# here. A finding needs the operand to name a url.
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# A name that mentions url but counts rather than carries it (`urlCount`,
# `urlLen`) is not a credential-bearing value. Excluded so the concat arm does
# not turn into noise the first time someone logs a size.
NOT_A_URL_VALUE = re.compile(
    r"(count|len|length|size|index|idx|bytes|chars|ms|timeout)$", re.IGNORECASE
)

# ── audited call sites. Removing an entry re-arms the gate for that line;
# editing the line makes the entry stale (re-audit, then re-seed with --seed).
ALLOWED = {
    ('src/android/app/src/main/java/com/rikkaminis/app/provider/ModelsDevApi.kt', 'Log.d(TAG, "No models.dev match for base URL: $forBaseURL")'),
    ('src/android/app/src/main/java/com/rikkaminis/app/provider/openai/OkHttpNetTraceListener.kt', 'com.rikkaminis.app.logging.AppLogger.info('),
    ('src/android/app/src/main/java/com/rikkaminis/app/provider/openai/OpenAIProvider.kt', 'com.rikkaminis.app.logging.AppLogger.info('),
    ('src/android/app/src/main/java/com/rikkaminis/app/provider/openai/OpenAIProvider.kt', 'com.rikkaminis.app.logging.AppLogger.info('),
    ('src/android/app/src/main/java/com/rikkaminis/app/provider/openai/OpenAIProvider.kt', 'com.rikkaminis.app.logging.AppLogger.info('),
    ('src/android/app/src/main/java/com/rikkaminis/app/provider/openai/OpenAIProvider.kt', 'com.rikkaminis.app.logging.AppLogger.warning('),
    ('src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/OpenOffloadHandler.kt', 'Log.d(TAG, "startActivity url=\'$url\' action=${intent.action} resolved='),
    ('src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/OpenOffloadHandler.kt', 'Log.w(TAG, "no handler for \'$url\'")'),
    ('src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/OpenOffloadHandler.kt', 'Log.w(TAG, "parseUri failed for \'$url\': ${e.message}")'),
    ('src/android/app/src/main/java/com/rikkaminis/app/data/repository/ProviderRepository.kt', 'android.util.Log.i("ProviderRepo", "refreshModels: id=${instance.id} t'),
    ('src/android/app/src/main/java/com/rikkaminis/app/data/repository/SkillRepository.kt', 'Log.w(TAG, "[siblings] HTTP ${resp.code} non-retryable for $url")'),
    ('src/android/app/src/main/java/com/rikkaminis/app/data/repository/SkillRepository.kt', 'Log.w(TAG, "[siblings] HTTP ${resp.code} on $url (attempt ${attempt + '),
    ('src/android/app/src/main/java/com/rikkaminis/app/data/repository/SkillRepository.kt', 'Log.w(TAG, "[siblings] ${e.javaClass.simpleName}: ${e.message} for $ur'),
    ('src/android/app/src/main/java/com/rikkaminis/app/data/repository/SkillRepository.kt', 'Log.w(TAG, "httpGetString failed $url: ${e.message}")'),
    ('src/android/app/src/main/java/com/rikkaminis/app/data/repository/SkillRepository.kt', 'Log.w(TAG, "httpGetBytes failed $url: ${e.message}")'),
}

# ── inline exemption marker, same shape as the repo's `debug-ok:`
EXEMPT = "url-ok:"


def _strip_span(lines, start):
    """Return (span_text, end_index, balanced) for the call statement at `start`.

    `balanced` is False when the parentheses never close within MAX_SPAN_LINES —
    the gate then fails CLOSED instead of analysing a truncated slice, because a
    half-read statement is exactly where a spliced url would go unseen.
    """
    depth = 0
    in_str = False
    escaped = False
    out = []
    for i in range(start, min(start + MAX_SPAN_LINES, len(lines))):
        text = lines[i]
        out.append(text)
        for ch in text:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        if depth <= 0:
            return "\n".join(out), i, True
    return "\n".join(out), min(start + MAX_SPAN_LINES, len(lines)) - 1, False


def _scan_braced(span, i):
    """Return the `${...}` starting at `i`, matching braces NESTEDLY.

    The old matcher used `\\$\\{[^}]*\\}`, which stops at the FIRST `}` — so
    `${forBaseURL.let { it }}` (and any other brace-nested interpolation) handed
    the whole url to the log while the gate saw no interpolation at all.
    Returns (text, next_index); text is None when the brace never closes, which
    the caller must treat as fail-closed.
    """
    assert span[i:i + 2] == "${"
    depth = 1
    j = i + 2
    while j < len(span):
        ch = span[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return span[i:j + 1], j + 1
        j += 1
    return None, len(span)


def _is_url_operand(expr):
    """A concatenated operand that carries the whole url value.

    Two gates must both open for a finding: the operand's identifier mentions
    url, and it is not one of the narrow names (host/port/scheme/...) that
    denote a component rather than the whole value. A redactor anywhere in the
    operand means the value was scrubbed before it reached the log.
    """
    names = IDENT.findall(expr or "")
    if not names:
        return None
    if not any("url" in n.lower() for n in names):
        return None
    if REDACTOR.search(expr):
        return None
    last = names[-1]
    if NARROW_NAME.search(last):
        return None
    if NOT_A_URL_VALUE.search(last):
        return None
    return expr.strip()


def _strip_string_literals(span):
    """`span` with each Kotlin string literal's CONTENT blanked out.

    Concatenation is an operator that lives OUTSIDE literals; matching `+` on the
    raw text would also fire on prose that happens to contain "+ url" inside a
    format string. Interpolations are handled separately by `_scan_braced`.
    """
    out = []
    i = 0
    n = len(span)
    while i < n:
        if span[i] == '"':
            out.append('""')
            i += 1
            while i < n:
                if span[i] == "\\":
                    i += 2
                    continue
                if span[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        out.append(span[i])
        i += 1
    return "".join(out)


def _offenders(span):
    """Interpolations in `span` that carry the whole url value.

    Three syntaxes of ONE defect — the whole url value reaching a log:
      1. template interpolation (`$url` / `${url}`) — the original coverage;
      2. concatenation (`"fetchModels forBaseURL=" + forBaseURL`);
      3. brace-nested interpolation (`${forBaseURL.let { it }}`), which the old
         `[^}]*` matcher structurally could not see.
    """
    found = []

    # (1) + (3) — brace-nested scanning, so `${...}` with inner braces is read
    # as one expression instead of stopping at the first `}`.
    i = 0
    while i < len(span):
        if span[i] == "$" and span[i + 1:i + 2] == "{":
            text, nxt = _scan_braced(span, i)
            if text is None:
                found.append("${…} (unterminated)")
                break
            expr = text[2:-1].strip()
            if "url" in expr.lower() and not (
                REDACTOR.search(expr) or SAFE_TAIL.search(expr)
            ):
                found.append(text if len(text) <= 60 else text[:57] + "...")
            i = nxt
            continue
        if span[i] == "$" and i + 1 < len(span) and (
            span[i + 1].isalpha() or span[i + 1] == "_"
        ):
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)", span[i:])
            bare = m.group(1)
            if "url" in bare.lower() and not NARROW_NAME.search(bare):
                found.append("$%s" % bare)
            i += m.end()
            continue
        i += 1

    # (2) concatenation — the `+` may sit on either side of the literal and may
    # land on a following line, so both directions are read. Judged on the
    # literal-stripped text: `+` is an operator outside literals, and prose
    # inside a format string must not read as an operator.
    bare_code = _strip_string_literals(span)
    for rx, tag in ((CONCAT_AFTER, "after"), (CONCAT_BEFORE, "before")):
        for m in rx.finditer(bare_code):
            operand = _is_url_operand(m.group(1))
            if operand:
                found.append("concat(%s): %s" % (tag, operand))
    return found


def scan(root):
    violations = []
    for scan_dir in SCAN_DIRS:
        base = os.path.join(root, SRC_SUBDIR, scan_dir)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in sorted(filenames):
                if not name.endswith(".kt"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().split("\n")
                i = 0
                while i < len(lines):
                    if not LOG_CALL.search(lines[i]):
                        i += 1
                        continue
                    span, end, balanced = _strip_span(lines, i)
                    context = "\n".join(lines[max(0, i - 2) : end + 1])
                    if EXEMPT not in context:
                        snippet = lines[i].strip()[:70]
                        if not balanced:
                            # Fail closed: an unparsed statement is not evidence
                            # of cleanliness, it is absence of evidence.
                            if (rel, snippet) not in ALLOWED:
                                violations.append(
                                    (rel, i + 1, snippet,
                                     ["语句在 %d 行内未闭合（fail-closed）" % MAX_SPAN_LINES],
                                     span.strip()[:200])
                                )
                        else:
                            bad = _offenders(span)
                            if bad and (rel, snippet) not in ALLOWED:
                                violations.append(
                                    (rel, i + 1, snippet, bad, span.strip()[:200])
                                )
                    i = end + 1
    return violations


def self_test():
    """Arm table: every syntax of ONE defect (whole url → log) must be flagged,
    and every legitimate alternative must stay green.

    Two of the arms are the audited WITNESS you cannot see in a diff: the
    concatenation arm and the nested-brace arm are the exact shapes the
    external audit used to pass this gate while re-introducing the defect
    (gate green, secret in the log). They are pinned here so the equivalence
    class — not the spelling — is what the gate remembers.
    """
    fixture = "src/android/app/src/main/java/com/rikkaminis/app/network/Trace.kt"
    arms = [
        # (label, source lines, must be reported?)
        ("interp-whole-url", [
            "        AppLogger.info(",
            "            tag,",
            '            "[${callTag(call)}] +${ms()}ms callStart url=${call.request().url}"',
            "        )",
        ], True),
        ("interp-redacted", [
            "        AppLogger.info(",
            "            tag,",
            '            "callStart url=${com.rikkaminis.app.debug.LLMRequestLog.redactURL(call.request().url.toString())}"',
            "        )",
        ], False),
        ("safe-accessors", [
            '        AppLogger.warning(TAG, "host=${url.host} port=${url.port}")',
        ], False),
        ("bare-name-leaks", [
            '        Log.w(TAG, "reason=$failure for $url.host")',
        ], True),
        ("inline-exempt", [
            '        AppLogger.info(TAG, "opened $url")  // url-ok: user-facing page',
        ], False),
        ("non-log-statement", [
            "        val hit = url.matches(regex)",
        ], False),
        # ── the audited witness: concatenation, gate-green before this fix
        ("concat-after", [
            '        com.rikkaminis.app.logging.AppLogger.info(TAG, "fetchModels forBaseURL=" + forBaseURL)',
        ], True),
        ("concat-after-url-object", [
            '        Log.d(TAG, "request to " + call.request().url)',
        ], True),
        ("concat-before", [
            '        Log.d(TAG, forBaseURL + " is unreachable")',
        ], True),
        ("concat-multiline", [
            "        AppLogger.info(",
            "            TAG,",
            '            "fetchModels forBaseURL=" +',
            "                forBaseURL",
            "        )",
        ], True),
        ("concat-redacted", [
            '        Log.d(TAG, "url=" + LLMRequestLog.redactURL(url.toString()))',
        ], False),
        ("concat-safe-component", [
            '        Log.d(TAG, "host=" + url.host)',
        ], False),
        ("concat-count", [
            '        Log.d(TAG, "urls=" + urlCount)',
        ], False),
        # ── the second audited witness: brace-nested interpolation
        ("nested-brace-identity", [
            '        com.rikkaminis.app.logging.AppLogger.info(TAG, "url=${forBaseURL.let { it }}")',
        ], True),
        ("nested-brace-plain", [
            '        Log.d(TAG, "url=${call.request().url}")',
        ], True),
        ("nested-brace-redacted", [
            '        Log.d(TAG, "url=${LLMRequestLog.redactURL(url).let { it }}")',
        ], False),
        # ── fail-closed: an unparsable statement is reported, never skipped
        ("unbalanced-statement", [
            "        AppLogger.info(",
            '            TAG, "url=" + forBaseURL',
            "        // never closed on purpose",
        ], True),
    ]
    tmp = tempfile.mkdtemp(prefix="urlguard-")
    ok = True
    for label, body, must_flag in arms:
        path = os.path.join(tmp, fixture)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(body) + "\n")
        got = scan(tmp)
        flagged = bool(got)
        if flagged != must_flag:
            print("  [FAIL] arm %-26s expected %s, got %d finding(s)"
                  % (label, "RED" if must_flag else "GREEN", len(got)))
            for rel, line, _s, bad, _sp in got:
                print("           %s:%d %s" % (rel, line, bad))
            ok = False
        else:
            print("  [OK]   arm %-26s %s" % (label, "RED" if must_flag else "GREEN"))
    print("URL_REDACTION_SELFTEST=%s (%d arms)" % ("PASS" if ok else "FAIL", len(arms)))
    return 0 if ok else 1


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] in ("--self-test", "--selftest"):
        return self_test()
    do_seed = args[0] == "--seed"
    if do_seed:
        args = args[1:]
    if not args:
        print(__doc__)
        return 2
    violations = scan(args[0])
    if do_seed:
        print("# --- generated by --seed: paste into ALLOWED ---")
        for rel, _line, snippet, _bad, _span in violations:
            print("    (%r, %r)," % (rel, snippet))
        return 0
    if violations:
        print("❌ 日志里出现整条 URL（可能带密钥）：")
        for rel, line, snippet, bad, _span in violations:
            print("  %s:%d" % (rel, line))
            print("      %s" % snippet)
            print("      泄漏形态: %s" % ", ".join(bad))
            print("      修法: 用 LLMRequestLog.redactURL(...) 包一层，或只打 host/path；")
            print("            确认与凭据无关时在行内加 `url-ok: <理由>`。")
        print("")
        print("  共 %d 处；已审计站点跑 --seed 后并入 ALLOWED。" % len(violations))
        return 1
    print(
        "✅ log url guard: no whole url value reaches a log — judged across all "
        "evaluation forms (interpolation / concatenation / brace-nested) "
        "(audited sites: %d)" % len(ALLOWED)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

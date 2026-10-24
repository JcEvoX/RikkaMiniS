#!/usr/bin/env python3
"""
offload_media_b64_wire_check — deep-audit-1022 FIX-A gate
                              + deep-audit-1041 A-06 extension.

Cross-process base64 wire contract (worker -> result.json -> main process):
  - Producer side (ModelExecutionService.kt, media_files encode sites) must use
    android.util.Base64.NO_WRAP, not Base64.DEFAULT (DEFAULT wraps at 76 chars
    + trailing newline).
  - Consumer side (ProviderExecutionGateway.kt) must use the MIME decoder
    (java.util.Base64.getMimeDecoder()), NOT the strict RFC 4648 decoder
    (java.util.Base64.getDecoder()), which throws IllegalArgumentException on
    any wrapped payload.

[dv21-A06] The same contract, one layer out. The original gate only knew about
the gateway and the worker's encode sites, so the OTHER consumers of the same
wire were free to (and did) hand-roll a different decoder:
  - ModelUseOffloadHandler.kt  : bare `Base64.decode(b64, Base64.DEFAULT)`,
                                 unguarded inside `mapNotNull` -> one malformed
                                 attachment threw away the whole run's result;
  - ModelExecutionService.kt   : the streaming entry + parseContentPartsJson
                                 used the STRICT decoder (wrapped payload threw
                                 out of the run, or silently dropped the image);
  - ChatStreamJsonl.kt         : strict decoder, unguarded, out of the chunk
                                 reader.
The rule added here, stated as a decision rather than a spelling:

  C3  no consumer of the offload wire may use the strict RFC 4648 decoder;
  C4  every decode site in those files is either routed through the shared
      `OffloadWireBase64.decode(...)` entry OR lexically inside a
      `try { }` / `runCatching { }` guard (a site that CAN throw must be
      guarded; the typed "refuse instead of guess" sites keep their own
      try/catch and are therefore legal);
  C5  the shared entry itself exists, is tolerant (MIME) and cannot throw.

C1 (gateway) is unchanged and deliberately keeps its own literal
`java.util.Base64.getMimeDecoder()` requirement, so the gateway is out of the
C3/C4 scope — it is already pinned by C1.

Checks run on a comment- AND string-masked view: prose that mentions an old
shape must neither satisfy nor defeat a check, and — unlike a naive
comment-stripper — masking preserves character offsets so the try/runCatching
span arithmetic stays correct even on lines containing `//` inside a literal.

Usage: python3 offload_media_b64_wire_check.py <repo_root>
       python3 offload_media_b64_wire_check.py --selftest
Exit: 0 = pass, 1 = violation, 2 = usage error.
"""
import os
import re
import sys

OFFLOAD = os.path.join(
    "src", "android", "app", "src", "main", "java", "com", "rikkaminis", "app",
    "sandbox", "offload")
GATEWAY_REL = os.path.join(OFFLOAD, "ProviderExecutionGateway.kt")
SERVICE_REL = os.path.join(OFFLOAD, "ModelExecutionService.kt")
HANDLER_REL = os.path.join(OFFLOAD, "ModelUseOffloadHandler.kt")
JSONL_REL = os.path.join(OFFLOAD, "ChatStreamJsonl.kt")
HELPER_REL = os.path.join(OFFLOAD, "OffloadWireBase64.kt")

# Code anchors (not comment anchors): these appear in executable lines only.
MEDIA_ENCODE_ANCHOR = 'put("data", android.util.Base64.encodeToString('
STRICT_DECODER = "Base64.getDecoder()"
MIME_DECODER = "java.util.Base64.getMimeDecoder()"

# [dv21-A06]
HELPER_CALL = "OffloadWireBase64.decode("
HELPER_OBJECT = "object OffloadWireBase64"
HELPER_SIGNATURE = "fun decode(b64: String): ByteArray?"
HELPER_GUARD = "runCatching"
# Every consumer file must still decode something — a file that stopped
# decoding entirely means the anchor moved, not that the contract holds.
CONSUMERS = (HANDLER_REL, SERVICE_REL, JSONL_REL)

# A decode site: the strict/basic/MIME decoder paths, the android one-shot
# decoder, or the shared entry itself. `encodeToString` is not a decode site.
DECODE_SITE_RE = re.compile(
    r"\b(?:android\.util\.|java\.util\.)?Base64\s*\.\s*"
    r"(?:getDecoder|getMimeDecoder|getUrlDecoder)\s*\(\s*\)\s*\.\s*decode\s*\("
    r"|\bBase64\s*\.\s*decode\s*\("
    r"|\bOffloadWireBase64\s*\.\s*decode\s*\("
)


def strip_comments(text):
    """Remove // line comments and /* */ block comments so anchor checks only
    see executable code (comment mentions must not satisfy or defeat a check)."""
    out = []
    for line in text.splitlines():
        code = line
        # strip // comments (naive but sufficient: no base64-string literals
        # in these files contain "//" that matters; anchors are put(...)/getDecoder)
        idx = code.find("//")
        if idx >= 0:
            code = code[:idx]
        out.append(code)
    return "\n".join(out)


def mask_code(text):
    """Offset-preserving mask: comment text and string/char-literal *bodies*
    become spaces (newlines kept). Length and every code offset are unchanged,
    so a caller can locate a decode site AND brace-match a try/runCatching span
    in the same coordinate space."""
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = out[i + 1] = " "
                i += 2
            continue
        if text.startswith('"""', i):
            out[i] = out[i + 1] = out[i + 2] = " "
            i += 3
            while i < n and not text.startswith('"""', i):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = out[i + 1] = out[i + 2] = " "
                i += 3
            continue
        if c == '"' or c == "'":
            quote = c
            out[i] = " "
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                if text[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def guarded_spans(masked):
    """[(start, end)] character spans of `try { ... }` and `runCatching { ... }`
    bodies, computed on a masked view so braces inside literals/comments cannot
    unbalance the walk."""
    spans = []
    for m in re.finditer(r"\b(?:try|runCatching)\s*\{", masked):
        start = masked.index("{", m.start())
        depth = 0
        for i in range(start, len(masked)):
            if masked[i] == "{":
                depth += 1
            elif masked[i] == "}":
                depth -= 1
                if depth == 0:
                    spans.append((start, i + 1))
                    break
    return spans


def find_close(text, open_idx, opener, closer):
    """Index of the [closer] matching text[open_idx], or -1."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


def split_args(text, open_idx):
    """Top-level arguments of the call whose `(` sits at [open_idx] (None if the
    parenthesis never closes — fail-closed callers report that)."""
    close = find_close(text, open_idx, "(", ")")
    if close < 0:
        return None
    args, depth, start = [], 0, open_idx + 1
    for i in range(open_idx + 1, close):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            args.append(text[start:i])
            start = i + 1
    args.append(text[start:close])
    return args


def flag_tokens(arg):
    """The base64 modifier names in one flag expression, qualified or not:
    `android.util.Base64.NO_WRAP or Base64.URL_SAFE` -> ['NO_WRAP','URL_SAFE']."""
    out = []
    for part in re.split(r"\bor\b", arg):
        token = re.sub(r"[\s()]", "", part).split(".")[-1]
        if token:
            out.append(token)
    return out


def statement_tail(text, start, limit=600):
    """Text from [start] to the end of the statement carrying it — a top-level
    `,` / `;` / newline, or a closing bracket that ends the enclosing expression."""
    depth = 0
    for i in range(start, min(len(text), start + limit)):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return text[start:i]
            depth -= 1
        elif c in ",;\n" and depth == 0:
            return text[start:i]
    return text[start:start + limit]


def read(rel):
    with open(rel, "r", encoding="utf-8") as f:
        return f.read()


def check_consumers(root, failures):
    """C3/C4 over the offload-wire consumers."""
    for rel in CONSUMERS:
        label = os.path.basename(rel)
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            failures.append("%s: file missing at %s" % (label, path))
            return
        masked = mask_code(read(path))

        # C3 — the strict RFC 4648 decoder has no business on this wire.
        for hit in re.finditer(re.escape(STRICT_DECODER), masked):
            line = masked[: hit.start()].count("\n") + 1
            failures.append(
                "%s:%d uses the strict %s (throws on a wrapped RFC 2045 "
                "payload); decode through %s or java.util.Base64.getMimeDecoder()"
                % (label, line, STRICT_DECODER, HELPER_CALL.rstrip("(")))

        # C4 — every decode site is helper-routed or lexically guarded.
        sites = list(DECODE_SITE_RE.finditer(masked))
        if not sites:
            failures.append(
                "%s: no base64 decode site found in %d bytes — gate anchor is "
                "stale (update it) or the file stopped decoding the wire"
                % (label, len(masked)))
            continue
        spans = guarded_spans(masked)
        for s in sites:
            if "OffloadWireBase64" in s.group(0):
                continue
            if any(a <= s.start() < b for (a, b) in spans):
                continue
            line = masked[: s.start()].count("\n") + 1
            failures.append(
                "%s:%d decodes base64 outside any guard — an unparseable "
                "payload throws out of the run (route it through %s or wrap it "
                "in try/runCatching)" % (label, line, HELPER_CALL.rstrip("(")))

        # C4b [wide-1024] — the chunk reader's miss path is a FALLBACK, never an
        # abort. `?: throw …` (or `!!`) reads the same two substrings the old rule
        # wanted while turning one empty/malformed b64 block into a dead stream.
        if rel == JSONL_REL:
            for s in re.finditer(re.escape(HELPER_CALL), masked):
                close = find_close(masked, s.end() - 1, "(", ")")
                tail = statement_tail(masked, close + 1 if close > 0 else s.end())
                if re.search(r"\bthrow\b", tail) or "!!" in tail:
                    line = masked[: s.start()].count("\n") + 1
                    failures.append(
                        "ChatStreamJsonl.kt:%d aborts on a decode miss (throw / !!) — "
                        "`OffloadWireBase64.decode(b64)` answers null for an empty or "
                        "truncated payload, and the chunk reader must fall back "
                        "(?: ByteArray(0)) instead of ending the stream" % line)


def check_helper(root, failures):
    """C5 — the shared decode entry exists and cannot throw."""
    path = os.path.join(root, HELPER_REL)
    if not os.path.exists(path):
        failures.append(
            "OffloadWireBase64.kt: file missing at %s (the single decode entry "
            "the consumers are required to route through)" % path)
        return
    masked = mask_code(read(path))
    for anchor in (HELPER_OBJECT, HELPER_SIGNATURE, MIME_DECODER, HELPER_GUARD):
        if anchor not in masked:
            failures.append(
                "OffloadWireBase64.kt: missing `%s` — the shared entry must stay "
                "tolerant (MIME) and non-throwing" % anchor)
    if STRICT_DECODER in masked:
        failures.append("OffloadWireBase64.kt: uses the strict %s" % STRICT_DECODER)
    # C5 dataflow [wide-1024] — `runCatching` only helps if the failure stays a
    # value. `.getOrThrow()` keeps both substrings the old rule looked for
    # (`getMimeDecoder().decode(` and `runCatching`) while re-raising exactly the
    # IllegalArgumentException this entry exists to absorb: one truncated block
    # escapes into every caller's coroutine.
    for bad_re, what in ((r"\bgetOrThrow\s*\(", "`getOrThrow()`"),
                         (r"!!", "`!!`"),
                         (r"\.\s*get\s*\(\s*\)", "`get()`")):
        if re.search(bad_re, masked):
            failures.append(
                "OffloadWireBase64.kt: the guard ends in %s — the shared decode entry "
                "must never throw; a truncated / mis-padded payload has to come back "
                "as null" % what)
    if re.search(r"getOrNull\s*\(", masked) is None:
        failures.append(
            "OffloadWireBase64.kt: the decode guard is not consumed through "
            "`getOrNull()` (fail-closed) — the shared entry's contract is `null` on a "
            "bad payload, and only a null-returning accessor implements it")


def check_tree(root):
    """Return (ok, [failure messages])."""
    failures = []
    gw_path = os.path.join(root, GATEWAY_REL)
    svc_path = os.path.join(root, SERVICE_REL)
    for p, label in ((gw_path, "ProviderExecutionGateway.kt"),
                     (svc_path, "ModelExecutionService.kt")):
        if not os.path.exists(p):
            failures.append("%s: file missing at %s" % (label, p))
            return False, failures

    # 1) Consumer side: gateway must not use the strict RFC 4648 decoder.
    gw_code = strip_comments(read(gw_path))
    if STRICT_DECODER in gw_code:
        failures.append("gateway uses strict %s (throws on wrapped payload); "
                        "use java.util.Base64.getMimeDecoder()" % STRICT_DECODER)
    # 1b) [wide-1024 C1] …and it must actually DECODE with it. `runCatching {
    #     java.util.Base64.getMimeDecoder(); ByteArray(0) }` satisfies every
    #     substring the old rule wanted while carrying zero decoded bytes: the
    #     decoder is constructed, never consumed. Dataflow, not spelling: the
    #     decoder must feed `.decode(`, and the bytes it returns must be what
    #     leaves the guard (a `;` between the decode and the guard's end discards
    #     them just as silently).
    gw_masked = mask_code(read(gw_path))
    gw_spans = guarded_spans(gw_masked)
    mime_sites = list(re.finditer(re.escape(MIME_DECODER), gw_masked))
    if not mime_sites:
        failures.append("gateway does not use java.util.Base64.getMimeDecoder() "
                        "(MIME decoder required for RFC 2045 wrapped input)")
    for m in mime_sites:
        line = gw_masked[: m.start()].count("\n") + 1
        chain = re.match(r"\s*\.\s*decode\s*\(", gw_masked[m.end():])
        if chain is None:
            failures.append(
                "ProviderExecutionGateway.kt:%d constructs the MIME decoder but never "
                "decodes with it — the media payload must be produced by "
                "`getMimeDecoder().decode(b64)`, not by a literal next to it" % line)
            continue
        close = find_close(gw_masked, m.end() + chain.end() - 1, "(", ")")
        if close < 0:
            failures.append("ProviderExecutionGateway.kt:%d: unbalanced "
                            ".decode( … ) on the media wire (fail-closed)" % line)
            continue
        guard = [s for s in gw_spans if s[0] <= m.start() < s[1]]
        if guard:
            block_end = min(guard, key=lambda s: s[1] - s[0])[1]
            if gw_masked[close + 1:block_end - 1].strip():
                failures.append(
                    "ProviderExecutionGateway.kt:%d decodes the payload then drops it "
                    "before the guard ends (the assignment gets something else) — every "
                    "generated image/audio would land as a zero-byte file" % line)

    # 2) Producer side: the media_files encode sites must use NO_WRAP — and the
    #    flag expression must be exactly that. [wide-1024 C2] `NO_WRAP or
    #    URL_SAFE` used to pass both substring tests while rewriting the alphabet
    #    to `-`/`_`, which the consumer's MIME decoder silently DISCARDS (not an
    #    error: a quiet bit shift in every generated image/audio).
    svc_code = strip_comments(read(svc_path))
    svc_masked = mask_code(read(svc_path))
    svc_lines = svc_code.splitlines()
    sites = [(i + 1, ln) for i, ln in enumerate(svc_lines)
             if MEDIA_ENCODE_ANCHOR in ln]
    if len(sites) == 0:
        failures.append("no media encode sites found (anchor %r missing) — "
                        "gate is stale, update the anchor" % MEDIA_ENCODE_ANCHOR)
    encode_sites = list(re.finditer(r"\bBase64\s*\.\s*encodeToString\s*\(", svc_masked))
    if encode_sites is None or not encode_sites:
        failures.append("ModelExecutionService.kt: no Base64.encodeToString( call site "
                        "found (fail-closed) — the media wire producer is unreadable")
    for m in encode_sites:
        lineno = svc_masked[: m.start()].count("\n") + 1
        args = split_args(svc_masked, m.end() - 1)
        if not args or len(args) < 2:
            failures.append("ModelExecutionService.kt:%d: encodeToString has no flag "
                            "argument (fail-closed) — NO_WRAP is a wire contract" % lineno)
            continue
        flags = flag_tokens(args[-1])
        bad = [t for t in flags if t != "NO_WRAP"]
        if not flags or bad:
            detail = ("Base64.DEFAULT wraps at 76 chars + a newline" if "DEFAULT" in bad
                      else "a non-NO_WRAP modifier rewrites the base64 alphabet, and the "
                           "consumer's MIME decoder silently drops those characters")
            failures.append(
                "ModelExecutionService.kt:%d media encode flags are %r — the flag "
                "expression must be exactly NO_WRAP (%s)"
                % (lineno, " or ".join(flags) or args[-1].strip(), detail))

    # 3-5) [dv21-A06] the other consumers of the same wire.
    check_consumers(root, failures)
    check_helper(root, failures)

    return len(failures) == 0, failures


HELPER_COMPLIANT = (
    "package p\n\n"
    "object OffloadWireBase64 {\n"
    "    fun decode(b64: String): ByteArray? =\n"
    "        if (b64.isEmpty()) null\n"
    "        else runCatching { java.util.Base64.getMimeDecoder().decode(b64) }.getOrNull()\n"
    "}\n"
)

FIXTURE_COMPLIANT = {
    GATEWAY_REL: (
        "class G {\n"
        "    val data = runCatching { java.util.Base64.getMimeDecoder().decode(b64) }.getOrNull()\n"
        "}\n"
    ),
    SERVICE_REL: (
        "class S {\n"
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
        "    val a = OffloadWireBase64.decode(x) ?: ByteArray(0)\n"
        "    val b = OffloadWireBase64.decode(y) ?: ByteArray(0)\n"
        "}\n"
    ),
    HANDLER_REL: (
        "class H {\n"
        "    // Base64.decode(b64, Base64.DEFAULT) in prose must not be a decode site\n"
        "    val data = OffloadWireBase64.decode(b64) ?: return@mapNotNull null\n"
        "    val kept = try { Base64.decode(other, Base64.DEFAULT) } catch (e: Throwable) { null }\n"
        "}\n"
    ),
    JSONL_REL: (
        "class J {\n"
        "    val data = OffloadWireBase64.decode(b64) ?: ByteArray(0)\n"
        "}\n"
    ),
    HELPER_REL: HELPER_COMPLIANT,
}

FIXTURE_VIOLATION = {
    GATEWAY_REL: (
        "class G {\n"
        "    // comment mentioning java.util.Base64.getMimeDecoder() must not fool the gate\n"
        "    val data = runCatching { java.util.Base64.getDecoder().decode(b64) }.getOrNull()\n"
        "}\n"
    ),
    SERVICE_REL: (
        "class S {\n"
        '    // put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.DEFAULT))\n'
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
        "}\n"
    ),
    HANDLER_REL: FIXTURE_COMPLIANT[HANDLER_REL],
    JSONL_REL: FIXTURE_COMPLIANT[JSONL_REL],
    HELPER_REL: HELPER_COMPLIANT,
}


def fixture(**overrides):
    """FIXTURE_COMPLIANT with the named files replaced. Keyword names are the
    bare file constants (GATEWAY / SERVICE / HANDLER / JSONL / HELPER), mapped
    onto their repo-relative paths here so a caller cannot silently add a sixth
    bogus file (which is exactly what a name-vs-path mixup looked like)."""
    rel_by_name = {
        "GATEWAY": GATEWAY_REL,
        "SERVICE": SERVICE_REL,
        "HANDLER": HANDLER_REL,
        "JSONL": JSONL_REL,
        "HELPER": HELPER_REL,
    }
    files = dict(FIXTURE_COMPLIANT)
    for name, content in overrides.items():
        if name not in rel_by_name:
            raise AssertionError("unknown fixture target %r" % name)
        files[rel_by_name[name]] = content
    return files


def run_fixture(files, expect_ok, label):
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="b64wire-gate-")
    try:
        for rel, content in files.items():
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        ok, failures = check_tree(tmp)
        if expect_ok and not ok:
            print("FAIL  %s: compliant fixture rejected: %s" % (label, failures))
            return False
        if not expect_ok and ok:
            print("FAIL  %s: violating fixture accepted" % label)
            return False
        print("PASS  %s" % label)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def selftest():
    ok = True
    ok &= run_fixture(FIXTURE_COMPLIANT, True, "selftest: compliant fixture accepted")
    ok &= run_fixture(FIXTURE_VIOLATION, False, "selftest: violation fixture rejected")
    # Comment-anchored NO_WRAP in a commented-out line must NOT satisfy the producer check
    ok &= run_fixture({
        GATEWAY_REL: FIXTURE_COMPLIANT[GATEWAY_REL],
        SERVICE_REL: (
            "class S {\n"
            '    // put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
            "}\n"
        ),
        HANDLER_REL: FIXTURE_COMPLIANT[HANDLER_REL],
        JSONL_REL: FIXTURE_COMPLIANT[JSONL_REL],
        HELPER_REL: HELPER_COMPLIANT,
    }, False, "selftest: commented-out NO_WRAP does not satisfy producer check")
    # Comment-anchored getMimeDecoder must NOT satisfy the consumer check
    ok &= run_fixture({
        GATEWAY_REL: (
            "class G {\n"
            "    // java.util.Base64.getMimeDecoder()\n"
            "    val data = runCatching { java.util.Base64.getDecoder().decode(b64) }.getOrNull()\n"
            "}\n"
        ),
        SERVICE_REL: FIXTURE_COMPLIANT[SERVICE_REL],
        HANDLER_REL: FIXTURE_COMPLIANT[HANDLER_REL],
        JSONL_REL: FIXTURE_COMPLIANT[JSONL_REL],
        HELPER_REL: HELPER_COMPLIANT,
    }, False, "selftest: commented-out getMimeDecoder does not satisfy consumer check")

    # ── [dv21-A06] one arm per new criterion ────────────────────────────────
    # C3: the strict decoder on a consumer, in a file that is otherwise clean.
    ok &= run_fixture(fixture(JSONL=(
        "class J {\n"
        "    val data = OffloadWireBase64.decode(b64) ?: ByteArray(0)\n"
        "    val old = java.util.Base64.getDecoder().decode(legacy)\n"
        "}\n"
    )), False, "selftest: C3 strict decoder on ChatStreamJsonl rejected")
    # C4: unguarded bare decode in the handler (the run-killing shape).
    ok &= run_fixture(fixture(HANDLER=(
        "class H {\n"
        "    val data = Base64.decode(b64, Base64.DEFAULT)\n"
        "}\n"
    )), False, "selftest: C4 unguarded decode in ModelUseOffloadHandler rejected")
    # C4: unguarded decode one level deeper in the worker.
    ok &= run_fixture(fixture(SERVICE=(
        "class S {\n"
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
        "    val a = OffloadWireBase64.decode(x) ?: ByteArray(0)\n"
        "    val b = android.util.Base64.decode(y, android.util.Base64.DEFAULT)\n"
        "}\n"
    )), False, "selftest: C4 unguarded decode in ModelExecutionService rejected")
    # C4 negative control: the SAME site inside try/catch is legal (proves the
    # criterion accepts guards, i.e. it is not a "must call the helper" spelling).
    ok &= run_fixture(fixture(HANDLER=(
        "class H {\n"
        "    val data = try { Base64.decode(b64, Base64.DEFAULT) }"
        " catch (e: IllegalArgumentException) { ByteArray(0) }\n"
        "}\n"
    )), True, "selftest: guarded decode accepted (criterion is not spelling-bound)")
    # C4 negative control: `runCatching` counts as a guard, and a string literal
    # that merely LOOKS like an unguarded decode is masked away.
    ok &= run_fixture(fixture(HANDLER=(
        "class H {\n"
        '    val msg = "Base64.decode(b64, Base64.DEFAULT)"\n'
        "    val data = runCatching { Base64.decode(b64, Base64.DEFAULT) }.getOrNull()\n"
        "}\n"
    )), True, "selftest: runCatching guard accepted, literal text is masked")
    # C4 ROT: a consumer that no longer decodes anything = stale anchor.
    ok &= run_fixture(fixture(JSONL=(
        "class J {\n"
        "    val data = ByteArray(0)\n"
        "}\n"
    )), False, "selftest: C4 stale anchor (no decode site) rejected")
    # C5: helper missing / not tolerant / can throw.
    files = fixture()
    del files[HELPER_REL]
    ok &= run_fixture(files, False, "selftest: C5 missing helper rejected")
    ok &= run_fixture(fixture(HELPER=(
        "object OffloadWireBase64 {\n"
        "    fun decode(b64: String): ByteArray? = java.util.Base64.getDecoder().decode(b64)\n"
        "}\n"
    )), False, "selftest: C5 helper without MIME/runCatching rejected")
    # ── [wide-1024] 门瞎实锤 → 固化臂（每条 = 一份外部模型的见证，原样） ──────
    # C1: the MIME decoder is CONSTRUCTED but nothing is decoded with it.
    ok &= run_fixture(fixture(GATEWAY=(
        "class G {\n"
        "    val data = runCatching { java.util.Base64.getMimeDecoder(); ByteArray(0) }.getOrNull()\n"
        "        ?: return@mapNotNull null\n"
        "}\n"
    )), False, "selftest: C1 MIME decoder constructed but never consumed (wide-1024)")
    # C1: the decoder IS consumed, but the bytes are dropped before the guard ends.
    ok &= run_fixture(fixture(GATEWAY=(
        "class G {\n"
        "    val data = runCatching { java.util.Base64.getMimeDecoder().decode(b64); ByteArray(0) }\n"
        "        .getOrNull() ?: return@mapNotNull null\n"
        "}\n"
    )), False, "selftest: C1 decoded bytes discarded inside the guard (wide-1024)")
    # C2: NO_WRAP or URL_SAFE — both substrings the old scan wanted, wrong alphabet.
    ok &= run_fixture(fixture(SERVICE=(
        "class S {\n"
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP or android.util.Base64.URL_SAFE))\n'
        '    put("data", android.util.Base64.encodeToString(m.data, android.util.Base64.NO_WRAP))\n'
        "    val a = OffloadWireBase64.decode(x) ?: ByteArray(0)\n"
        "    val b = OffloadWireBase64.decode(y) ?: ByteArray(0)\n"
        "}\n"
    )), False, "selftest: C2 NO_WRAP or URL_SAFE rejected (alphabet rewrite, wide-1024)")
    # C5: the shared entry re-raises instead of answering null.
    ok &= run_fixture(fixture(HELPER=(
        "object OffloadWireBase64 {\n"
        "    fun decode(b64: String): ByteArray? =\n"
        "        if (b64.isEmpty()) null\n"
        "        else runCatching { java.util.Base64.getMimeDecoder().decode(b64) }.getOrThrow()\n"
        "}\n"
    )), False, "selftest: C5 shared entry re-raises (getOrThrow, wide-1024)")
    # C4: the chunk reader turns one decode miss into a dead stream.
    ok &= run_fixture(fixture(JSONL=(
        "class J {\n"
        '    val data = (OffloadWireBase64.decode(b64) ?: throw IllegalArgumentException("bad b64")),\n'
        "}\n"
    )), False, "selftest: C4 ChatStreamJsonl throws on a decode miss (wide-1024)")
    if ok:
        print("OFFLOAD_MEDIA_B64_SELFTEST=PASS")
    return 0 if ok else 1

def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        sys.exit(selftest())
    root = None
    if "--root" in args:
        i = args.index("--root")
        if i + 1 < len(args):
            root = args[i + 1]
    elif args:
        root = args[0]
    if not root:
        print("usage: offload_media_b64_wire_check.py <repo_root> | --selftest", file=sys.stderr)
        sys.exit(2)
    ok, failures = check_tree(root)
    for f in failures:
        print("VIOLATION: %s" % f)
    if ok:
        print("OFFLOAD_MEDIA_B64_WIRE: OK (%d media encode sites NO_WRAP, consumer MIME decoder)" %
              len([l for l in strip_comments(read(os.path.join(root, SERVICE_REL))).splitlines()
                   if MEDIA_ENCODE_ANCHOR in l]))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

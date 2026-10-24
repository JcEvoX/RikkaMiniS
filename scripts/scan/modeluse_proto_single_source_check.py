#!/usr/bin/env python3
"""
Gate: FIX-A (deep-audit-1019 ISSUE-01) — the extra_headers / custom-endpoint
envelope dialect must live ONCE, in ModelUseProtocol.kt.

The bug: `parseExtraHeaders` and `parseCustomEndpointPath` still had a
hand-written copy in EACH process (`ModelExecutionService.kt` and
`ModelUseOffloadHandler.kt`) and the copies had drifted — the worker dropped a
present-but-not-an-object `extra_headers` with ZERO warning, accepted any
non-empty `endpoint` as a path (symbolic modes like `chat` were written into
`provider.absoluteEndpointOverride`), and never read `image_endpoint` at all.
The fix collapsed both into `ModelUseProtocol` with one-line delegates at the
call sites (the `parseChatExtraBody` precedent).

Checks assert:
  1. the dialect definitions live in ModelUseProtocol.kt, and every definition
     remaining in a caller file is a one-line DELEGATE into ModelUseProtocol
     (a re-collapsed implementation body fails);
  2. the caller files invoke through the `ModelUseProtocol.` prefix;
  3. the silent `optJSONObject("extra_headers")` branch does not come back in
     either caller file.

[dv14-A4] ISSUE-08 — same defect one layer over (the IMAGE passthrough
dialect: implicit top-level keys minus the reserved set, plus the explicit
`extra_body` / `extra_headers` / `endpoint_path` envelope). It was written
THREE times, each copy carrying its own 17-key `imageReservedKeys` set, and
two of the three copies were already unreachable — one dead, one feeding a
reader nobody called. A set that is never the one being read cannot be kept in
sync by discipline, so:

  4. neither caller may DEFINE the reserved set (`val … imageReservedKeys` /
     `val … IMAGE_PASSTHROUGH_RESERVED_KEYS`) — it lives in ModelUseProtocol;
  5. `parseImagePassthrough` must exist in ModelUseProtocol and the caller
     that owns the image path must actually go through it;
  6. the silent `optJSONObject("extra_body")` read must not come back in a
     caller — a present-but-not-an-object `extra_body` has to be reported.

  7. [dv23-A03] the `model =` argument at EVERY `buildRequestJson(` call site in
     ModelUseOffloadHandler.kt is the ENRICHED model (the value bound from
     `…effectiveModel(`, or that call itself) — never the raw `entry.model`.
     `cmdRun` judged `--output`/input modalities on `effectiveModel` but handed
     `entry.model` to :modelservice, so every models.dev capability field was
     dropped at the process boundary (no `responseModalities` → a `--output`
     run that passed the gate here came back text-only; `maxOutputTokens` fell
     back to the provider default). Derived per call site + counted, so a
     THIRD dispatch point cannot reintroduce the raw hand-off.

Scope note: every check below is limited to ModelUseProtocol.kt and
CALLER_RELS; the gate never judges a file outside them.

Checks run on comment-stripped text, so prose in a KDoc cannot satisfy them.
Usage: python3 scripts/scan/modeluse_proto_single_source_check.py [repo_root]
       python3 scripts/scan/modeluse_proto_single_source_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import re
import sys

SRC = "src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload"
PROTO_REL = SRC + "/ModelUseProtocol.kt"
CALLER_RELS = (
    SRC + "/ModelExecutionService.kt",
    SRC + "/ModelUseOffloadHandler.kt",
)
FUNCS = ("parseExtraHeaders", "parseCustomEndpointPath")
SILENT_BRANCH = 'optJSONObject("extra_headers")'
PREFIX = "ModelUseProtocol."
PROTO_TOKENS = (
    "fun parseExtraHeaders(",
    "fun parseCustomEndpointPath(",
    "extra_headers is not a JSON object",
    'safeOptString("image_endpoint"',
    'startsWith("/")',
    # [dv14-A4] ISSUE-08 tokens
    "fun parseImagePassthrough(",
    "internal val imageReservedKeys",
)

# [dv14-A4] ISSUE-08 — the image passthrough dialect's single source.
IMAGE_PARSER = "parseImagePassthrough"
IMAGE_PARSER_CALL = PREFIX + IMAGE_PARSER + "("
# The caller that owns the image path: `applyImagePassthrough` is called only
# from the worker's image route, and routing itself has one owner
# (`ModelUseProtocol.decideImageRoute`). The app-process handler owns no image
# call at all and must not grow one back.
IMAGE_CALLER_REL = SRC + "/ModelExecutionService.kt"
# A caller DEFINING the set is the defect; merely reading
# `ModelUseProtocol.imageReservedKeys` is fine, so the anchor requires `val`.
IMAGE_KEY_DEF_RE = re.compile(
    r"\bval\s+(?:imageReservedKeys|IMAGE_PASSTHROUGH_RESERVED_KEYS)\b",
)
SILENT_IMAGE_BRANCH = 'optJSONObject("extra_body")'

# ── [dv16-A04] the --provider matcher must live once ───────────────────────
# list / search / run each answered `--provider` differently: list substring-
# matched model.provider+label (case-sensitive), search never read the option
# it documents, run matched label/id exactly. The fix is a single top-level
# `providerMatches(query, label, id, typeName)` every subcommand calls.
PROVIDER_MATCHER = "providerMatches("
PROVIDER_SUBJECTS = ("cmdList", "cmdSearch", "resolveEntry")
PROVIDER_SEARCH_FLAG = 'args.get("provider")'
PROVIDER_FILTER_GUARD = "if (providerFilter != null)"
PROVIDER_HANDLER_REL = CALLER_RELS[1]


def fun_body(text, name):
    """Body of the first `fun <name>(` in [text] (brace-balanced), or None."""
    m = re.search(r"\bfun\s+%s\s*\(" % re.escape(name), text)
    if not m:
        return None
    open_brace = text.find("{", m.end())
    if open_brace < 0:
        return None
    depth = 0
    for i in range(open_brace, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace:i + 1]
    return None


def check_provider(root):
    """[dv16-A04] Every `--provider` reader goes through one matcher.

    Fail-closed: a body that cannot be delimited (member renamed / moved) is a
    violation, not a silent pass.
    """
    v = []
    path = os.path.join(root, PROVIDER_HANDLER_REL)
    name = os.path.basename(PROVIDER_HANDLER_REL)
    if not os.path.exists(path):
        return ["%s: NOT FOUND at %s — cannot verify the --provider matcher" % (name, path)]
    src = code_only(open(path, encoding="utf-8").read())

    if src.count("fun %s" % PROVIDER_MATCHER) != 1:
        v.append(
            "%s: expected exactly ONE 'fun %s' definition — %d found (a second "
            "--provider decision is the pre-fix shape)"
            % (name, PROVIDER_MATCHER.rstrip("("), src.count("fun %s" % PROVIDER_MATCHER))
        )
    for subject in PROVIDER_SUBJECTS:
        body = fun_body(src, subject)
        if body is None:
            v.append(
                "%s: `%s` could not be delimited — fail-closed (member renamed or "
                "moved?)" % (name, subject)
            )
            continue
        if PROVIDER_MATCHER not in body:
            v.append(
                "%s: %s does not call the single %s — its own --provider opinion "
                "is back (list/search/run drift)" % (name, subject, PROVIDER_MATCHER.rstrip("("))
            )
    search = fun_body(src, "cmdSearch")
    if search is not None:
        if PROVIDER_SEARCH_FLAG not in search:
            v.append(
                "%s: cmdSearch does not read %s — the flag is documented and "
                "ignored again" % (name, PROVIDER_SEARCH_FLAG)
            )
        if PROVIDER_FILTER_GUARD not in search:
            v.append(
                "%s: cmdSearch has no provider filter decision point (%s) — the "
                "read flag would be dropped" % (name, PROVIDER_FILTER_GUARD)
            )
    return v


# ── [dv23-A03] the model that crosses into :modelservice must be ENRICHED ──
# `cmdRun` resolves `effectiveModel` (models.dev base + overrides) and judges the
# --output / input modality guards on it, but handed the RAW `entry.model` to
# buildRequestJson. The worker rebuilds LLMModel from that JSON and never calls
# ModelsDevApi (init happens in the app process only), so every capability field
# models.dev supplied died at the boundary.
DISPATCH_REL = CALLER_RELS[1]
DISPATCH_CALL = "buildRequestJson("
# An enrichment outlet: a call into the shared resolver. Matches both
# `ModelsDevApi.effectiveModel(` and a local alias of it.
ENRICH_CALL_RE = re.compile(r"\beffective(?:Entry)?Model\s*\(")
# `val <name> [ : <type> ] = … effectiveModel(` — the binding the fix reuses.
ALIAS_RE = re.compile(
    r"\bval\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[^=\n]+)?=\s*[^=\n]*\beffective(?:Entry)?Model\s*\(",
)
# The raw hand-off shape the fix removed (`entry.model`, `x.model`, …).
RAW_MODEL_ARG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*\.model$")


def _call_args(src, open_idx):
    """Argument text between the '(' at [open_idx] and its matching ')'.

    None when unbalanced (renamed / moved call) — fail-closed at the caller.
    """
    depth = 0
    i = open_idx
    while i < len(src):
        c = src[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1:i]
        i += 1
    return None


def _named_arg(args, name):
    """Value text of the top-level named argument `name = …`, or None."""
    parts, depth, cur = [], 0, []
    for c in args:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    if "".join(cur).strip():
        parts.append("".join(cur))
    for part in parts:
        m = re.match(r"\s*%s\s*=\s*(.+?)\s*$" % re.escape(name), part, re.S)
        if m:
            return m.group(1).strip()
    return None


def _call_end(text, open_idx):
    """Index of the ')' closing the '(' at [open_idx], or -1 (fail-closed)."""
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


# What may follow the enrichment call and still be "the enriched value":
# closing delimiters of enclosing scopes and plain member reads. Anything else
# (a combinator, an elvis, another call) hands over something derived from it.
_PASSTHROUGH_TAIL_RE = re.compile(r"^[)\]\}]*(?:\s*\.\s*[A-Za-z_]\w*)*$")


def _alias_discards(expr):
    """(True, tail) when the `val x = … effectiveModel(…) …` binding keeps only
    a DERIVED value.

    [wide-1024 Rule 7] The alias name was collected from the whole line, so
    `val effectiveModel = ModelsDevApi.effectiveModel(entry).let { entry.model }`
    registered `effectiveModel` as an enrichment outlet while the value bound
    to it is the raw entry again (type-compatible, so nothing complains) and
    every models.dev capability field dies at the process boundary again.
    """
    m = ENRICH_CALL_RE.search(expr)
    if not m:
        return False, ""
    paren = expr.find("(", m.end() - 1)
    if paren < 0:
        return True, "the enrichment call has no argument list"
    end = _call_end(expr, paren)
    if end < 0:
        return True, "the enrichment call could not be delimited (fail-closed)"
    tail = expr[end + 1:].strip()
    if _PASSTHROUGH_TAIL_RE.match(tail):
        return False, ""
    return True, tail


def check_enriched_dispatch(root):
    """[dv23-A03] Every `buildRequestJson(` hand-off resolves the enriched model.

    Derived per call site and counted: the number of compliant `model =`
    arguments must equal the number of call sites, so a THIRD dispatch point
    cannot quietly pass the raw entry again.
    """
    v = []
    path = os.path.join(root, DISPATCH_REL)
    name = os.path.basename(DISPATCH_REL)
    if not os.path.exists(path):
        return ["%s: NOT FOUND at %s — cannot verify the dispatched model" % (name, path)]
    src = code_only(open(path, encoding="utf-8").read())

    aliases = set()
    for m in ALIAS_RE.finditer(src):
        aliases.add(m.group(1))
        line = src.count("\n", 0, m.start()) + 1
        rest = src[m.end():]
        line_end = rest.find("\n")
        binding = src[m.start():m.end() + (line_end if line_end >= 0 else len(rest))]
        discards, tail = _alias_discards(binding)
        if discards:
            v.append(
                "%s:%d: `val %s = …%s` binds a DERIVED value, not the enriched model "
                "(`%s`) — the hand-off looks enriched and every models.dev capability "
                "field is dropped at the process boundary"
                % (name, line, m.group(1), binding.strip()[:60], tail[:40])
            )
    if not aliases:
        v.append(
            "%s: no `val … = …effectiveModel(` binding — the enriched model is never "
            "resolved, so nothing can be handed over (fail-closed)" % name
        )

    sites = [i for i in range(len(src)) if src.startswith(DISPATCH_CALL, i)]
    if not sites:
        v.append(
            "%s: no '%s' call site — fail-closed (dispatch renamed or moved out of "
            "this file?)" % (name, DISPATCH_CALL)
        )

    compliant = 0
    for idx in sites:
        line = src.count("\n", 0, idx) + 1
        args = _call_args(src, idx + len(DISPATCH_CALL) - 1)
        if args is None:
            v.append(
                "%s:%d: '%s' could not be delimited — fail-closed" % (name, line, DISPATCH_CALL)
            )
            continue
        value = _named_arg(args, "model")
        if value is None:
            v.append(
                "%s:%d: '%s' carries no `model =` argument — the worker would be handed "
                "no model at all" % (name, line, DISPATCH_CALL)
            )
            continue
        if RAW_MODEL_ARG_RE.match(value):
            v.append(
                "%s:%d: `model = %s` hands the RAW entry to :modelservice — every "
                "models.dev capability field (output_modalities, max_output_tokens, "
                "reasoning_effort_values, …) is dropped at the process boundary; pass "
                "the enriched model the guards above read" % (name, line, value)
            )
            continue
        if value in aliases or ENRICH_CALL_RE.search(value):
            compliant += 1
            continue
        v.append(
            "%s:%d: `model = %s` is not an enrichment outlet — the argument must be the "
            "value bound from `effectiveModel(...)` (or that call itself)"
            % (name, line, value)
        )
    if sites and compliant != len(sites):
        v.append(
            "%s: %d of %d '%s' call sites hand over the enriched model — every site must"
            % (name, compliant, len(sites), DISPATCH_CALL)
        )
    return v


# ── [dv27-A02] the request body must reach the provider WHOLE on both paths ──
# `buildRequestJson` wrote `thinking_level` for every non-OFF turn and the
# STREAMING path parsed it, but `executeRun` — the non-streaming path — never
# read the key at all. The worker then fell back to `LLMProvider.sendMessage`'s
# default (`ThinkingLevel.OFF`), so every non-streamed offloaded request asked
# the vendor NOT to reason: silently, and on exactly the families where the
# level decides whether the model reasons at all (DeepSeek / Qwen3 / Claude 3.7+).
#
# Two criteria, both DERIVED from the tree, never from a hand-written field list:
#   C1 — no orphan request key. Every request-level key derived from
#        `buildRequestJson`'s write points must be read SOMEWHERE in the worker
#        service. A third field added to the dispatcher and consumed by nobody
#        fails here.
#   C2 — the level reaches the provider call, and both paths hand over the same
#        argument surface. At `executeRun`'s `provider.sendMessage(` and
#        `executeStreamingRun`'s `provider.streamMessage(` the named-argument
#        SETS must be equal, and the `thinkingLevel =` value must trace to a
#        read of `thinking_level` in the same function body (one helper hop
#        allowed). A constant (`thinkingLevel = ThinkingLevel.OFF`) has no such
#        trace and fails; so does a path that drops an argument its sibling
#        passes.
#
# Scope note: C1 is a *service-wide* read check on purpose. Three derived keys
# are legitimately single-path — `input_json` / `passthrough_request` /
# `output_ext` exist only for the non-streaming image & raw-passthrough routes,
# and `streaming` decides the route before either body runs — so a literal
# "consumed in BOTH bodies" rule would be red on a clean tree, i.e. a gate that
# cannot be trusted. The per-path half of the contract is enforced by C2 at the
# call sites, where a drop is unambiguous.
SERVICE_REL = CALLER_RELS[0]
EXEC_DISPATCH_REL = SRC + "/ModelExecutionDispatcher.kt"
# (entry point, provider method) — the pair that must agree.
EXEC_PATHS = (("executeRun", "sendMessage"), ("executeStreamingRun", "streamMessage"))
LEVEL_KEY = "thinking_level"
LEVEL_ARG = "thinkingLevel"
READ_CALLS = (
    "optString", "optInt", "optBoolean", "optLong", "optDouble",
    "optJSONObject", "optJSONArray", "opt",
    "has", "getString", "getInt", "getBoolean", "getLong", "getDouble",
    "getJSONObject", "getJSONArray",
)
KEY_READ_RE = re.compile(
    r'\b(?:%s)\(\s*"([A-Za-z_][A-Za-z0-9_]*)"' % "|".join(READ_CALLS)
)
LEVEL_READ_RE = re.compile(r'\b(?:%s)\(\s*"%s"' % ("|".join(READ_CALLS), LEVEL_KEY))
NESTED_BUILDER_RE = re.compile(r"JSON(?:Object|Array)\(\)\s*\.apply\s*\{")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def _brace_end(text, open_idx):
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1

def _dispatch_request_keys(src):
    """Request-level keys written by `buildRequestJson`, or None (fail-closed).

    A key counts as request-level when its `put("…"` sits inside the function's
    body but NOT inside a nested `JSONObject()/JSONArray().apply { }` builder —
    the nested ones are message / content-part / tool-schema fields, not values
    the worker reads off the request.
    """
    body = fun_body(src, "buildRequestJson")
    if body is None:
        return None
    regions = []
    for m in NESTED_BUILDER_RE.finditer(body):
        b = m.end() - 1
        e = _brace_end(body, b)
        if e > b:
            regions.append((b, e))
    outer = None
    for b, e in regions:
        if all(b <= b2 and e2 <= e for b2, e2 in regions):
            if outer is None or (b <= outer[0] and e >= outer[1]):
                outer = (b, e)
    nested = [r for r in regions if r != outer]
    keys = []
    for m in re.finditer(r'\bput\(\s*"([A-Za-z_][A-Za-z0-9_]*)"', body):
        pos = m.start()
        if any(b < pos < e for b, e in nested):
            continue
        if m.group(1) not in keys:
            keys.append(m.group(1))
    return keys

def _named_args(args):
    """Top-level `name = value` pairs of a call's argument text."""
    out = {}
    parts, depth, cur = [], 0, []
    for c in args:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    if "".join(cur).strip():
        parts.append("".join(cur))
    for part in parts:
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*(.+?)\s*$", part, re.S)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out

LEVEL_CONST_RE = re.compile(r"\bThinkingLevel\s*\.\s*[A-Za-z_]\w*")
# Value-returning combinators: what the block hands back is what the caller
# gets, so a block that ignores its receiver (or returns a constant) has
# replaced the traced value.
VALUE_COMBINATOR_RE = re.compile(r"\.\s*(let|run|takeIf)\s*\{")


def _block_at(text, open_idx):
    """`{...}` starting at [open_idx] (brace-balanced, string literals skipped)."""
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx:i + 1]
        i += 1
    return None


def _post_process_override(expr):
    """(True, why) when [expr] REPLACES the value it post-processes.

    [wide-1024 C2] Finding the wire read inside the expression only proves the
    read is MENTIONED. `thinkingLevelFromWire(req.optString("thinking_level",
    "")).let { ThinkingLevel.OFF }` keeps the read textually while the value
    handed to the provider is a constant — every non-streamed offloaded turn
    would ask the vendor not to reason, which is exactly the defect C2 exists
    for. A value combinator whose block returns a hard-coded level, or ignores
    its receiver entirely, discards the trace (fail-closed when the block
    cannot be delimited).
    """
    for m in VALUE_COMBINATOR_RE.finditer(expr):
        brace = expr.find("{", m.end() - 1)
        blk = _block_at(expr, brace) if brace >= 0 else None
        if blk is None:
            return True, "a `.%s {` block could not be delimited (fail-closed)" % m.group(1)
        inner = blk[1:-1]
        if LEVEL_CONST_RE.search(inner):
            return True, "the `.%s {` block returns a hard-coded level" % m.group(1)
        declares = re.search(r"^\s*([A-Za-z_]\w*)\s*->", inner)
        recv = declares.group(1) if declares else "it"
        if not re.search(r"\b%s\b" % re.escape(recv), inner):
            return True, "the `.%s {` block ignores its receiver" % m.group(1)
    return False, ""


def _traces_to_level(value, body, svc):
    """True when [value] is, or is bound in [body] to, a read of `thinking_level`."""
    if LEVEL_READ_RE.search(value):
        return not _post_process_override(value)[0]
    ident = value.strip()
    if not IDENT_RE.match(ident):
        return False
    m = re.search(r"\bval\s+%s\b[^=\n]*=\s*([^\n]+)" % re.escape(ident), body)
    if not m:
        return False
    rhs = m.group(1)
    if _post_process_override(rhs)[0]:
        return False
    if LEVEL_READ_RE.search(rhs):
        return True
    # one helper hop: `val x = helper(…)` where helper() reads the key
    for hm in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", rhs):
        helper = fun_body(svc, hm.group(1))
        if helper and LEVEL_READ_RE.search(helper):
            return True
    return False

def check_service_wiring(root):
    """[dv27-A02] C1 (no orphan key) + C2 (level reaches both provider calls)."""
    v = []
    svc_path = os.path.join(root, SERVICE_REL)
    svc_name = os.path.basename(SERVICE_REL)
    if not os.path.exists(svc_path):
        return ["%s: NOT FOUND at %s" % (svc_name, svc_path)]
    svc = code_only(open(svc_path, encoding="utf-8").read())
    disp_path = os.path.join(root, EXEC_DISPATCH_REL)
    if not os.path.exists(disp_path):
        # The dialect fixtures model only the protocol files; a tree that ships
        # the real service but no dispatcher is a different (bigger) breakage.
        if "fun executeRun(" in svc:
            return [
                "%s: NOT FOUND at %s — the request body cannot be derived, so the "
                "worker's coverage of it cannot be judged (fail-closed)"
                % (os.path.basename(EXEC_DISPATCH_REL), disp_path)
            ]
        return v
    disp = code_only(open(disp_path, encoding="utf-8").read())
    keys = _dispatch_request_keys(disp)
    if keys is None:
        return [
            "%s: `fun buildRequestJson(` could not be delimited — fail-closed"
            % os.path.basename(EXEC_DISPATCH_REL)
        ]
    if not keys:
        return [
            "%s: no request-level `put(\"…\"` could be derived — fail-closed "
            "(the writer was renamed or its body changed shape)"
            % os.path.basename(EXEC_DISPATCH_REL)
        ]

    # ── C1: every key the dispatcher writes is read by the worker ──
    read_keys = set(KEY_READ_RE.findall(svc))
    for k in keys:
        if k not in read_keys:
            v.append(
                "%s: the dispatcher writes '%s' but no worker site reads it — the "
                "field dies at the process boundary" % (svc_name, k)
            )

    # ── C2: the level reaches BOTH provider calls, bound to its read ──
    surfaces = {}
    for entry, callee in EXEC_PATHS:
        body = fun_body(svc, entry)
        if body is None:
            v.append(
                "%s: `fun %s(` could not be delimited — fail-closed (entry renamed "
                "or moved?)" % (svc_name, entry)
            )
            continue
        token = "provider.%s(" % callee
        idx = body.find(token)
        if idx < 0:
            v.append(
                "%s: `%s` makes no '%s' call — fail-closed"
                % (svc_name, entry, token)
            )
            continue
        args = _call_args(body, idx + len(token) - 1)
        if args is None:
            v.append(
                "%s: the '%s' call in `%s` could not be delimited — fail-closed"
                % (svc_name, token, entry)
            )
            continue
        named = _named_args(args)
        surfaces[entry] = (set(named), token)
        if LEVEL_ARG not in named:
            v.append(
                "%s: `%s` calls %s without `%s =` — the level the dispatcher "
                "serialized is dropped and the provider falls back to its OFF "
                "default (reasoning silently disabled)"
                % (svc_name, entry, token.rstrip("("), LEVEL_ARG)
            )
            continue
        if not _traces_to_level(named[LEVEL_ARG], body, svc):
            v.append(
                "%s: `%s` passes `%s = %s` — that value is not derived from reading "
                "'%s' in this function body (constant or re-typed literal)"
                % (svc_name, entry, LEVEL_ARG, named[LEVEL_ARG], LEVEL_KEY)
            )

    # ── C2b: the two paths hand the provider the same argument surface ──
    if len(surfaces) == 2:
        (a_name, (a_args, a_tok)) , (b_name, (b_args, b_tok)) = list(surfaces.items())
        only_a = sorted(a_args - b_args)
        only_b = sorted(b_args - a_args)
        if only_a:
            v.append(
                "%s: `%s` passes %s to %s but `%s` does not — one path drops an "
                "argument the other hands over"
                % (svc_name, a_name, ", ".join("`%s =`" % x for x in only_a),
                   a_tok.rstrip("("), b_name)
            )
        if only_b:
            v.append(
                "%s: `%s` passes %s to %s but `%s` does not — one path drops an "
                "argument the other hands over"
                % (svc_name, b_name, ", ".join("`%s =`" % x for x in only_b),
                   b_tok.rstrip("("), a_name)
            )
    return v

def code_only(text):
    """Text with comments removed — presence checks must not be satisfiable
    from prose. Block comments (KDoc) first, then line comments, keeping line
    structure so violation reports still point at real lines."""
    out_lines = []
    in_block = False
    for line in text.split("\n"):
        res = []
        i = 0
        n = len(line)
        while i < n:
            if in_block:
                end = line.find("*/", i)
                if end < 0:
                    i = n
                else:
                    in_block = False
                    i = end + 2
                continue
            if line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            if line.startswith("//", i):
                break
            res.append(line[i])
            i += 1
        out_lines.append("".join(res))
    return "\n".join(out_lines)


def lines_of(src, needle):
    return [i + 1 for i, line in enumerate(src.split("\n")) if needle in line]


def find_defs(src):
    """Lines that declare either dialect function in a caller file."""
    out = {}
    for f in FUNCS:
        pat = re.compile(r"(private |internal |public )?fun %s\s*\(" % f)
        out[f] = [i + 1 for i, line in enumerate(src.split("\n")) if pat.search(line)]
    return out


def _decl_body(src, fn, decl_line):
    """The body of `fun <fn>(` declared on [decl_line]: brace body, else the
    expression body (up to the following blank line or declaration)."""
    for m in re.finditer(r"\bfun\s+%s\s*\(" % re.escape(fn), src):
        if src.count("\n", 0, m.start()) + 1 != decl_line:
            continue
        rp = _call_end(src, m.end() - 1)
        if rp < 0:
            return None
        k = rp + 1
        while k < len(src) and src[k] in " \t\r\n":
            k += 1
        if k < len(src) and src[k] == ":":
            while k < len(src) and src[k] not in "={":
                k += 1
        if k >= len(src):
            return None
        if src[k] == "{":
            end = _brace_end(src, k)
            return src[k:end + 1] if end > k else None
        stops = []
        blank = src.find("\n\n", k)
        if blank >= 0:
            stops.append(blank)
        nxt = re.search(r"\n\s*(?:private |internal |public )?fun\s", src[k:])
        if nxt:
            stops.append(k + nxt.start())
        stop = min(stops) if stops else len(src)
        return src[k + 1:stop]
    return None


def is_delegate_def(src, decl_line, fn=None):
    """A caller-file definition is legal only when its BODY is one delegating
    expression into ModelUseProtocol.

    [wide-1024 Rule 1] The old anchor was a 3-line window containing
    `ModelUseProtocol.` and no `for (`/`while (` — so appending a fallback
    (`…parseCustomEndpointPath(inputJson) ?: JSONObject(inputJson)
    .optString("endpoint")`) stayed inside the window and read as a delegate
    while it re-implemented the dialect decision at the call site: a symbolic
    mode like `chat` would be written into `provider.absoluteEndpointOverride`
    again. The body is now delimited and must be exactly one call (member reads
    on the result are allowed, nothing else).
    """
    fn = fn or next((f for f in FUNCS if re.search(
        r"\bfun\s+%s\s*\(" % re.escape(f), "\n".join(src.split("\n")[decl_line - 1:decl_line]))), None)
    if fn is None:
        return False
    body = _decl_body(src, fn, decl_line)
    if body is None:
        return False
    token = PREFIX + fn + "("
    if body.count(token) != 1:
        return False
    at = body.find(token)
    end = _call_end(body, at + len(token) - 1)
    if end < 0:
        return False
    head = body[:at].strip()
    if head not in ("", "return"):
        return False
    return _PASSTHROUGH_TAIL_RE.match(body[end + 1:].strip()) is not None


def check(root):
    v = []
    proto_path = os.path.join(root, PROTO_REL)
    if not os.path.exists(proto_path):
        return ["ModelUseProtocol.kt: NOT FOUND at %s" % proto_path]
    proto = code_only(open(proto_path, encoding="utf-8").read())

    for token in PROTO_TOKENS:
        if token not in proto:
            v.append("ModelUseProtocol.kt: the single dialect is missing '%s'" % token)
    for f in FUNCS:
        for n in lines_of(proto, "fun %s(" % f):
            # definitions inside ModelUseProtocol are the single source; a
            # delegate to itself would be a collapse bug — flag it
            window = "\n".join(proto.split("\n")[n - 1 : n + 2])
            if "ModelUseProtocol.%s(" % f in window:
                v.append("ModelUseProtocol.kt:%d: the dialect delegates to itself" % n)

    for rel in CALLER_RELS:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            v.append("%s: NOT FOUND at %s" % (os.path.basename(rel), path))
            continue
        name = os.path.basename(rel)
        src = code_only(open(path, encoding="utf-8").read())
        for f in FUNCS:
            if "ModelUseProtocol.%s(" % f not in src:
                v.append(
                    "%s: no prefixed call 'ModelUseProtocol.%s(' — the call "
                    "site must go through the single dialect" % (name, f)
                )
        defs = find_defs(src)
        for f, decls in defs.items():
            for n in decls:
                if not is_delegate_def(src, n, f):
                    v.append(
                        "%s:%d: 'fun %s(' is NOT a one-line delegate into "
                        "ModelUseProtocol — a second implementation body is "
                        "back" % (name, n, f)
                    )
        for n in lines_of(src, SILENT_BRANCH):
            v.append(
                "%s:%d: the silent branch '%s' is back — a "
                "present-but-not-an-object value would be dropped with zero "
                "warning" % (name, n, SILENT_BRANCH)
            )
        for n in lines_of(src, SILENT_IMAGE_BRANCH):
            v.append(
                "%s:%d: the silent branch '%s' is back — a "
                "present-but-not-an-object extra_body would be dropped with "
                "zero warning" % (name, n, SILENT_IMAGE_BRANCH)
            )
        for i, line in enumerate(src.split("\n")):
            if IMAGE_KEY_DEF_RE.search(line):
                v.append(
                    "%s:%d: the reserved-key set is DEFINED here again — it "
                    "lives in ModelUseProtocol.kt, next to the single "
                    "%s" % (name, i + 1, IMAGE_PARSER_CALL.rstrip("("))
                )
    # [dv14-A4] ISSUE-08: the image path has ONE parser, and its caller uses it.
    caller_path = os.path.join(root, IMAGE_CALLER_REL)
    if not os.path.exists(caller_path):
        v.append(
            "%s: NOT FOUND at %s"
            % (os.path.basename(IMAGE_CALLER_REL), caller_path)
        )
    else:
        caller_src = code_only(open(caller_path, encoding="utf-8").read())
        if IMAGE_PARSER_CALL not in caller_src:
            v.append(
                "%s: no '%s' — the image passthrough dialect must be parsed by "
                "the single implementation in ModelUseProtocol"
                % (os.path.basename(IMAGE_CALLER_REL), IMAGE_PARSER_CALL)
            )
    return v


GOOD_CALLER = """\
package p
class C {
    private fun cmdList(args: OffloadArgs): NativeOffloadResult {
        val providerFilter = args.get("provider")
        if (providerFilter != null) {
            entries = entries.filter { providerMatches(providerFilter, it.label, it.id, it.typeName) }
        }
        return NativeOffloadResult(0, "")
    }

    private fun cmdSearch(args: OffloadArgs): NativeOffloadResult {
        val providerFilter = args.get("provider")
        if (providerFilter != null) {
            matches = matches.filter { providerMatches(providerFilter, it.label, it.id, it.typeName) }
        }
        return NativeOffloadResult(0, "")
    }

    private fun resolveEntry(idOrName: String, providerFilter: String? = null): ModelEntry? {
        val pool = if (providerFilter.isNullOrEmpty()) all else {
            all.filter { providerMatches(providerFilter, it.label, it.id, it.typeName) }
        }
        return null
    }

    private fun parseExtraHeaders(inputJson: String, warnings: MutableList<String>): Map<String, String> =
        ModelUseProtocol.parseExtraHeaders(inputJson, warnings)

    private fun parseCustomEndpointPath(inputJson: String): String? =
        ModelUseProtocol.parseCustomEndpointPath(inputJson)

    private fun applyImagePassthrough(inputJson: String): Map<String, Any?> =
        ModelUseProtocol.parseImagePassthrough(inputJson).body

    // [dv23-A03] the enriched model is what crosses into :modelservice
    private fun cmdRun(args: OffloadArgs): NativeOffloadResult {
        val effectiveModel = com.rikkaminis.app.provider.ModelsDevApi.effectiveModel(entry)
        val ptRequestJson = ModelExecutionDispatcher.buildRequestJson(
            instance = instance,
            model = effectiveModel,
            messages = messages,
        )
        val remoteRequestJson = ModelExecutionDispatcher.buildRequestJson(
            instance = instance,
            model = effectiveModel,
            messages = messages,
        )
        return NativeOffloadResult(0, "")
    }
}

internal fun providerMatches(query: String, label: String, id: String, typeName: String): Boolean {
    val q = query.trim().lowercase()
    return label.trim().lowercase() == q
}
"""

# Fixture 7's caller: everything else right, the image delegation gone.
GOOD_CALLER_NO_IMAGE = GOOD_CALLER.replace(
    "    private fun applyImagePassthrough(inputJson: String): Map<String, Any?> =\n"
    "        ModelUseProtocol.parseImagePassthrough(inputJson).body\n",
    "",
)

# Fixture 6's caller: a second definition of the reserved-key set.
GOOD_CALLER_REDEFINES_KEYS = GOOD_CALLER.replace(
    "    private fun parseCustomEndpointPath(",
    '    private val imageReservedKeys: Set<String> = setOf("messages")\n'
    "\n"
    "    private fun parseCustomEndpointPath(",
)

BAD_CALLER_COPY = """\
package p
class C {
    private fun parseExtraHeaders(inputJson: String, warnings: MutableList<String>): Map<String, String> {
        val obj = JSONObject(inputJson)
        val eh = obj.optJSONObject("extra_headers") ?: return emptyMap()
        return mapOf("k" to (eh.optString("k")))
    }

    private fun parseCustomEndpointPath(inputJson: String): String? =
        ModelUseProtocol.parseCustomEndpointPath(inputJson)

    private fun applyImagePassthrough(inputJson: String): Map<String, Any?> =
        ModelUseProtocol.parseImagePassthrough(inputJson).body
}
"""

BAD_CALLER_NO_PREFIX = """\
package p
class C {
    private fun parseExtraHeaders(inputJson: String, warnings: MutableList<String>): Map<String, String> =
        parseExtraHeaders(inputJson, warnings)

    private fun parseCustomEndpointPath(inputJson: String): String? =
        ModelUseProtocol.parseCustomEndpointPath(inputJson)

    private fun applyImagePassthrough(inputJson: String): Map<String, Any?> =
        ModelUseProtocol.parseImagePassthrough(inputJson).body
}
"""

GOOD_PROTO = """\
package p
object ModelUseProtocol {
    internal val imageReservedKeys: Set<String> = setOf("messages", "model")

    fun parseExtraHeaders(inputJson: String, warnings: MutableList<String>): Map<String, String> {
        val raw = obj.opt("extra_headers") ?: return emptyMap()
        warnings.add("extra_headers is not a JSON object (actual type: x)")
        return out
    }

    fun parseCustomEndpointPath(inputJson: String): String? {
        val raw = obj.safeOptString("image_endpoint", "")
        return if (raw.startsWith("/")) raw else null
    }

    fun parseImagePassthrough(inputJson: String): ImagePassthroughSpec {
        val body = LinkedHashMap<String, Any?>()
        return ImagePassthroughSpec(body = body)
    }
}
"""


# [dv16-A04] the pre-fix shapes: one subcommand drops the shared matcher, and
# cmdSearch ignores the flag it documents.
GOOD_CALLER_LIST_SUBSTRING = GOOD_CALLER.replace(
    "            entries = entries.filter { providerMatches(providerFilter, it.label, it.id, it.typeName) }",
    "            entries = entries.filter { it.provider.lowercase().contains(providerFilter.lowercase()) }",
)

GOOD_CALLER_SEARCH_NO_FLAG = GOOD_CALLER.replace(
    '        val providerFilter = args.get("provider")\n'
    "        if (providerFilter != null) {\n"
    "            matches = matches.filter { providerMatches(providerFilter, it.label, it.id, it.typeName) }\n"
    "        }\n",
    "",
)

# [dv23-A03] the pre-fix shape: BOTH dispatch points hand the raw entry over.
GOOD_CALLER_RAW_HANDOFF = GOOD_CALLER.replace(
    "model = effectiveModel,",
    "model = entry.model,",
)

# Only the standard path regressed — the passthrough branch stays enriched.
# The gate counts call sites, so a single raw site must already be red.
GOOD_CALLER_ONE_RAW = GOOD_CALLER.replace(
    "            model = effectiveModel,",
    "            model = entry.model,",
)

# A `model =` argument that is neither the alias nor the shared resolver: the
# hand-off bypasses enrichment without looking like `entry.model`.
GOOD_CALLER_FOREIGN_ARG = GOOD_CALLER.replace(
    "            model = effectiveModel,",
    "            model = resolvedElsewhere,",
)

# The enriched model is bound from something other than the shared resolver, so
# there is no alias to hand over (a local re-derivation would be the same bug).
GOOD_CALLER_NO_ALIAS = GOOD_CALLER.replace(
    "        val effectiveModel = com.rikkaminis.app.provider.ModelsDevApi.effectiveModel(entry)",
    "        val effectiveModel = entry.baseModel",
)

# No dispatch call site at all — fail-closed, not silently clean.
GOOD_CALLER_NO_DISPATCH = GOOD_CALLER.replace(
    "    private fun cmdRun(args: OffloadArgs): NativeOffloadResult {",
    "    private fun notRun(args: OffloadArgs): NativeOffloadResult {",
).replace(
    "ModelExecutionDispatcher.buildRequestJson(",
    "ModelExecutionDispatcher.buildBodyJson(",
)

# [wide-1024] Rule 7 witness: the alias is bound to a DERIVED value
# (`.let { entry.model }` hands the raw entry back, type-compatible).
GOOD_CALLER_DISCARDED_ALIAS = GOOD_CALLER.replace(
    "        val effectiveModel = com.rikkaminis.app.provider.ModelsDevApi.effectiveModel(entry)",
    "        val effectiveModel = "
    "com.rikkaminis.app.provider.ModelsDevApi.effectiveModel(entry).let { entry.model }",
)

# [wide-1024] Rule 1 witness: the delegate keeps its `ModelUseProtocol.` call
# and re-decides with an elvis fallback on the same line.
GOOD_CALLER_FALLBACK_DELEGATE = GOOD_CALLER.replace(
    "    private fun parseCustomEndpointPath(inputJson: String): String? =\n"
    "        ModelUseProtocol.parseCustomEndpointPath(inputJson)\n",
    "    private fun parseCustomEndpointPath(inputJson: String): String? =\n"
    "        ModelUseProtocol.parseCustomEndpointPath(inputJson) ?: "
    "JSONObject(inputJson).optString(\"endpoint\").ifEmpty { null }\n",
)

GOOD_CALLER_TWO_MATCHERS = GOOD_CALLER.replace(
    "class C {",
    "class C {\n"
    "    private fun providerMatches(q: String, label: String): Boolean = label == q\n",
)


# ── [dv27-A02] fixtures: the request body must reach the provider whole ──
# The delegate block keeps these fixtures legal for the dialect checks above, so
# a wiring arm isolates check_service_wiring() instead of tripping four gates.
WIRING_DELEGATES = """\
    private fun parseExtraHeaders(inputJson: String, warnings: MutableList<String>): Map<String, String> =
        ModelUseProtocol.parseExtraHeaders(inputJson, warnings)

    private fun parseCustomEndpointPath(inputJson: String): String? =
        ModelUseProtocol.parseCustomEndpointPath(inputJson)

    private fun applyImagePassthrough(inputJson: String): Map<String, Any?> =
        ModelUseProtocol.parseImagePassthrough(inputJson).body
"""

def wiring_service(parse_line=("        val thinkingLevel = "
                               "thinkingLevelFromWire(req.optString(\"thinking_level\", \"\"))\n"),
                   run_level="thinkingLevel",
                   run_drop_max_tokens=False):
    """A worker service whose two execution paths mirror the shipped shape."""
    run_mt = "" if run_drop_max_tokens else "            maxTokens = maxTokens,\n"
    return (
        "package p\nclass ModelExecutionService {\n"
        + WIRING_DELEGATES
        + "\n    private fun executeRun(req: JSONObject): String {\n"
        + "        val messages = req.optJSONArray(\"messages\")\n"
        + "        val maxTokens = req.optInt(\"max_tokens\", 4096)\n"
        + parse_line
        + "        provider.sendMessage(\n"
        + "            messages = messages,\n"
        + run_mt
        + "            thinkingLevel = %s,\n" % run_level
        + "        )\n        return \"\"\n    }\n"
        + "\n    private fun executeStreamingRun(req: JSONObject): String {\n"
        + "        val messages = req.optJSONArray(\"messages\")\n"
        + "        val maxTokens = req.optInt(\"max_tokens\", 4096)\n"
        + "        val thinkingLevel = "
        + "thinkingLevelFromWire(req.optString(\"thinking_level\", \"\"))\n"
        + "        provider.streamMessage(\n"
        + "            messages = messages,\n"
        + "            maxTokens = maxTokens,\n"
        + "            thinkingLevel = thinkingLevel,\n"
        + "        )\n        return \"\"\n    }\n}\n"
    )

def wiring_dispatch(extra_key=None):
    """The dispatcher writer: request-level puts at the top level of one
    builder, plus a nested builder whose keys must NOT be derived."""
    extra = '            put("%s", 1)\n' % extra_key if extra_key else ""
    return (
        "package p\nobject ModelExecutionDispatcher {\n"
        "    fun buildRequestJson(thinkingLevel: ThinkingLevel): String {\n"
        "        return JSONObject().apply {\n"
        "            put(\"messages\", JSONArray().apply { put(JSONObject().apply { "
        "put(\"role\", \"user\") }) })\n"
        "            put(\"max_tokens\", 4096)\n"
        + extra
        + "            if (thinkingLevel != ThinkingLevel.OFF) "
        "put(\"thinking_level\", thinkingLevel.name)\n"
        "        }\n    }\n}\n"
    )

def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


def selftest():
    import tempfile

    ok = True

    def run(root):
        violations = check(root) + check_provider(root) + check_enriched_dispatch(root) + check_service_wiring(root)
        return violations

    # fixture 1: good tree → exit 0
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        for rel in CALLER_RELS:
            write(root, rel, GOOD_CALLER)
        bad = run(root)
        if bad:
            ok = False
            print("SELFTEST-FAIL good fixture must be clean:", *bad, sep="\n  ")

    # fixture 2: a second implementation body pasted back into a caller → must FAIL
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], BAD_CALLER_COPY)
        write(root, CALLER_RELS[1], GOOD_CALLER)
        bad = run(root)
        if not any("NOT a one-line delegate" in b or SILENT_BRANCH in b for b in bad):
            ok = False
            print("SELFTEST-FAIL re-collapsed copy must be caught:", bad)

    # fixture 3: delegate without the ModelUseProtocol. prefix → must FAIL
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER)
        write(root, CALLER_RELS[1], BAD_CALLER_NO_PREFIX)
        bad = run(root)
        if not any("no prefixed call" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL missing-prefix delegate must be caught:", bad)

    # fixture 4: silent optJSONObject branch back in a caller → must FAIL
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER)
        write(root, CALLER_RELS[1], BAD_CALLER_COPY)
        bad = run(root)
        if not any(SILENT_BRANCH in b for b in bad):
            ok = False
            print("SELFTEST-FAIL silent branch must be caught:", bad)

    # fixture 5: comments cannot satisfy the checks
    with tempfile.TemporaryDirectory() as root:
        commented = """\
package p
class C {
    // ModelUseProtocol.parseExtraHeaders( — prose only
    /* private fun parseCustomEndpointPath(inputJson: String): String? = */
    private fun other(x: Int): Int = x
}
"""
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER)
        write(root, CALLER_RELS[1], commented)
        bad = run(root)
        if not any("no prefixed call" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL comment-only references must not count:", bad)

    # [dv14-A4] ISSUE-08 fixture 6: a caller defines the reserved-key set
    # again (the shape both deleted copies had) → must FAIL
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER_REDEFINES_KEYS)
        write(root, CALLER_RELS[1], GOOD_CALLER)
        bad = run(root)
        if not any("reserved-key set is DEFINED here again" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL a second reserved-key set must be caught:", bad)

    # [dv14-A4] ISSUE-08 fixture 7: the image-path caller stops delegating to
    # the single parser → must FAIL (an unused single source is the defect)
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER_NO_IMAGE)
        write(root, CALLER_RELS[1], GOOD_CALLER)
        bad = run(root)
        if not any("must be parsed by the single implementation" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL a caller without the image delegation must be caught:", bad)

    # [dv16-A04] fixture 8: a subcommand rolls its own --provider filter
    # (list's pre-fix substring match) → must FAIL on the shared-matcher arm
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER)
        write(root, CALLER_RELS[1], GOOD_CALLER_LIST_SUBSTRING)
        bad = run(root)
        if not any("cmdList does not call the single providerMatches" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL a hand-rolled list filter must be caught:", bad)

    # [dv16-A04] fixture 9: cmdSearch drops the documented --provider flag
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER)
        write(root, CALLER_RELS[1], GOOD_CALLER_SEARCH_NO_FLAG)
        bad = run(root)
        if not any('cmdSearch does not read args.get("provider")' in b for b in bad):
            ok = False
            print("SELFTEST-FAIL an ignored search flag must be caught:", bad)

    # [dv16-A04] fixture 10: a second matcher definition (the drift source)
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER)
        write(root, CALLER_RELS[1], GOOD_CALLER_TWO_MATCHERS)
        bad = run(root)
        if not any("expected exactly ONE" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL a duplicated matcher must be caught:", bad)

    # [dv16-A04] fixture 11: fail-closed — the handler file is gone
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER)
        bad = run(root) + check_provider(root)
        if not any("cannot verify the --provider matcher" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL a missing handler file must not pass silently:", bad)

    # [dv23-A03] fixtures 12-15: the dispatched model must be the enriched one
    def dispatch_arm(label, caller, needle):
        nonlocal ok
        with tempfile.TemporaryDirectory() as root:
            write(root, PROTO_REL, GOOD_PROTO)
            write(root, CALLER_RELS[0], GOOD_CALLER)
            write(root, CALLER_RELS[1], caller)
            bad = run(root)
            if not any(needle in b for b in bad):
                ok = False
                print("SELFTEST-FAIL %s must be caught:" % label, bad)

    dispatch_arm(
        "a raw `model = entry.model` hand-off (the pre-fix shape)",
        GOOD_CALLER_RAW_HANDOFF,
        "hands the RAW entry to :modelservice",
    )
    dispatch_arm(
        "one enriched site beside one raw site (second dispatch point re-added)",
        GOOD_CALLER_ONE_RAW,
        "hands the RAW entry to :modelservice",
    )
    dispatch_arm(
        "a `model =` argument that is neither the alias nor the shared resolver",
        GOOD_CALLER_FOREIGN_ARG,
        "is not an enrichment outlet",
    )
    dispatch_arm(
        "the enriched model never resolved (no alias binding)",
        GOOD_CALLER_NO_ALIAS,
        "no `val … = …effectiveModel(` binding",
    )
    dispatch_arm(
        "the dispatch call site removed (fail-closed)",
        GOOD_CALLER_NO_DISPATCH,
        "no 'buildRequestJson(' call site",
    )
    # [wide-1024] Rule 7 witness: the alias line keeps `effectiveModel(` but
    # binds a derived value, so the hand-off looks enriched and is not.
    dispatch_arm(
        "an alias bound to a derived value (.let { entry.model })",
        GOOD_CALLER_DISCARDED_ALIAS,
        "binds a DERIVED value",
    )

    # [wide-1024] Rule 1 witness: the delegate re-decides with an elvis.
    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], GOOD_CALLER_FALLBACK_DELEGATE)
        write(root, CALLER_RELS[1], GOOD_CALLER)
        bad = run(root)
        if not any("NOT a one-line delegate" in b for b in bad):
            ok = False
            print("SELFTEST-FAIL a delegate with an elvis fallback must be caught:", bad)

    # [dv27-A02] fixtures 16-20: thinking_level must reach BOTH provider calls,
    # and every key the dispatcher writes must have a reader.
    def wiring_arm(label, service, dispatch, needle):
        nonlocal ok
        with tempfile.TemporaryDirectory() as root:
            write(root, PROTO_REL, GOOD_PROTO)
            write(root, CALLER_RELS[0], service)
            write(root, CALLER_RELS[1], GOOD_CALLER)
            write(root, EXEC_DISPATCH_REL, dispatch)
            bad = check_service_wiring(root)
            if not any(needle in b for b in bad):
                ok = False
                print("SELFTEST-FAIL %s must be caught:" % label, bad)
            elif len(bad) != 1:
                ok = False
                print("SELFTEST-FAIL %s must be the ONLY violation:" % label, bad)

    with tempfile.TemporaryDirectory() as root:
        write(root, PROTO_REL, GOOD_PROTO)
        write(root, CALLER_RELS[0], wiring_service())
        write(root, CALLER_RELS[1], GOOD_CALLER)
        write(root, EXEC_DISPATCH_REL, wiring_dispatch())
        bad = check_service_wiring(root)
        if bad:
            ok = False
            print("SELFTEST-FAIL the wiring-compliant fixture must be clean:", *bad, sep="\n  ")

    wiring_arm(
        "the non-streaming path parses nothing (pre-fix shape: value undefined)",
        wiring_service(parse_line=""),
        wiring_dispatch(),
        "is not derived from reading 'thinking_level'",
    )
    wiring_arm(
        "the non-streaming path passes a constant level",
        wiring_service(run_level="ThinkingLevel.OFF"),
        wiring_dispatch(),
        "is not derived from reading 'thinking_level'",
    )
    # [wide-1024] C2 witness: the wire read stays inside the binding and the
    # value handed to the provider is a constant.
    wiring_arm(
        "the parsed level is replaced after the read (.let { ThinkingLevel.OFF })",
        wiring_service(
            parse_line='        val thinkingLevel = '
                       'thinkingLevelFromWire(req.optString("thinking_level", ""))'
                       '.let { ThinkingLevel.OFF }\n',
        ),
        wiring_dispatch(),
        "is not derived from reading 'thinking_level'",
    )

    wiring_arm(
        "the dispatcher writes a key no worker site reads",
        wiring_service(),
        wiring_dispatch(extra_key="nobody_reads_me"),
        "but no worker site reads it",
    )
    wiring_arm(
        "one path drops an argument its sibling hands over",
        wiring_service(run_drop_max_tokens=True),
        wiring_dispatch(),
        "one path drops an argument the other hands over",
    )

    if ok:
        print("MODELUSE_PROTO_SELFTEST=PASS")
        print("MODELUSE_PROVIDER_MATCHER_SELFTEST=PASS (4 fixtures: 3 violations caught, 1 fail-closed)")
        return 0
    return 1


def main():
    args = [a for a in sys.argv[1:] if a != "--selftest"]
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    if len(args) > 1:
        print(__doc__)
        sys.exit(2)
    root = args[0] if args else os.getcwd()
    violations = check(root) + check_provider(root) + check_enriched_dispatch(root) + check_service_wiring(root)
    if violations:
        print("modeluse_proto_single_source_check: %d violation(s)" % len(violations))
        for v in violations:
            print("  " + v)
        sys.exit(1)
    print("modeluse_proto_single_source_check: clean (dialect single-sourced in ModelUseProtocol)")
    sys.exit(0)


if __name__ == "__main__":
    main()

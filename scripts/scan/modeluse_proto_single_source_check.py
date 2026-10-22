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

    aliases = set(m.group(1) for m in ALIAS_RE.finditer(src))
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


def is_delegate_def(src, decl_line):
    """A caller-file definition is legal only if the declaration and the next
    two lines form a one-line delegate into ModelUseProtocol (expression body
    or `{ return ModelUseProtocol.parse... }` up to a brace that closes within
    these lines)."""
    lines = src.split("\n")
    window = "\n".join(lines[decl_line - 1 : decl_line + 2])
    return PREFIX in window


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
                if not is_delegate_def(src, n):
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

GOOD_CALLER_TWO_MATCHERS = GOOD_CALLER.replace(
    "class C {",
    "class C {\n"
    "    private fun providerMatches(q: String, label: String): Boolean = label == q\n",
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
        violations = check(root) + check_provider(root) + check_enriched_dispatch(root)
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
    violations = check(root) + check_provider(root) + check_enriched_dispatch(root)
    if violations:
        print("modeluse_proto_single_source_check: %d violation(s)" % len(violations))
        for v in violations:
            print("  " + v)
        sys.exit(1)
    print("modeluse_proto_single_source_check: clean (dialect single-sourced in ModelUseProtocol)")
    sys.exit(0)


if __name__ == "__main__":
    main()

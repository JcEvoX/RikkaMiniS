#!/usr/bin/env python3
"""
Gate: ModelsCollection.add provider-field integrity
(deep-audit-1026 ISSUE-04, branch fix/da1026-models-provider-field).

`ModelsCollection.add` (the minis-config / ConfigOffloadHandler surface) used
to persist `provider = instanceId` — the provider instance's raw UUID — while
every other add path (provider.models.add RPC at ProviderMutationMethods:338,
AddCustomModelScreen:85) writes `ProviderType.displayName`. Consequences:
`minis-model-use list` rendered a UUID in the provider field, and
`ModelsDevApi.enrichModel` could never hit `providerKeyMap[model.provider]`
for these rows, dropping them into the order-dependent all-providers scan
(the F-212 failure).

Secondarily: the `model_id` blank gate validated without trim (modelsUpdate
trims — asymmetric), so a whitespace-only id minted a blank composite-key
row; and `providerKeyMap` keyed only the short catalog names ("Google"/"xAI"/
"Kimi") while the displayNames written by all three add surfaces are
"Google Gemini"/"xAI (Grok)"/"Kimi Code" — a lookup that missed for every
custom model of those families.

  Arm 1  ModelsCollection.add:
           a) `provider = instanceId` is GONE (forbidden, code-only body)
           b) persists `instance.providerType.displayName`
           c) the model_id gate trims before validating
         Fail-closed: add() not found ⇒ red.
  Arm 2  ModelsDevApi.providerKeyMap must carry BOTH the three displayName
         keys added by this fix AND all original short keys (idempotence:
         a rewrite that keeps only one family must not pass).

  Arm 3  (deep-audit-1038 A-08) A vendor adapter that reuses
         OpenAIModelsApi.fetchModels for its own vendor base URL must say
         which family the rows belong to — a non-null `providerName = …`
         argument OR a post-fetch `.map { … provider = … }` relabel (both
         accepted; the gate pins the decision, not a spelling). The generic
         openAI adapter is exempt. Fail-closed: no adapter/wiring detected ⇒
         red.
  Arm 4  (deep-audit-1038 A-08) The decision inside OpenAIModelsApi:
         `providerName: String? = null` stays optional, [resolveProviderName]
         keeps both halves of the decision, the row construction routes
         through it, and the cache return path re-labels cached rows.

  Arm 5  (deep-audit-1042 A-04) The four NON-UI answer sites for "what can
         this model do" must route through the shared enriched resolver
         (`ModelsDevApi.effectiveModel` = enrich base first, overrides
         second) instead of reading the raw entry:
           a) ModelsCollection's `models.<id>.contextWindow` /
              `maxOutputTokens` readers (they read `overrides ?: baseModel
              ?: 0`, so 33 of the 39 built-ins answered 0);
           b) the three Debug RPC views — `chat.models.list`,
              `provider.models.list` and `ProviderMutationMethods.entryToJson`
              (`val effective = entry.model` ⇒ null windows / no capability
              flags over the harness while `minis-model-use list` answered
              correctly for the same entry).
         Decision, not spelling: the collection may keep a private alias
         (the parity test pins its literal call count at exactly 1), and the
         debug views may use the fully-qualified name — both accepted. The
         raw `val effective = entry.model` read is not. Fail-closed: a file
         that disappears or loses its reader/alias anchor ⇒ red (ROT).

  Arm 6  (deep-audit-1043 B-07) The four READERS of "what can this model do"
         inside `ChatViewModel` must answer through ONE live resolution
         (`liveEffectiveModel()`), not through re-derived copies of the same
         lookup. `currentModel` is a snapshot of the entry's BASE model, so a
         reader bound to it reports 33 of the 39 built-ins as `HEURISTIC`
         windows with no max-output row, and a vision predicate that never
         normalises modality spellings misreads `image_input` as text-only.
         The gate pins the DECISION: one exit, enriched on both branches, and
         all four readers routed through it.

Usage: python3 scripts/scan/models_provider_field_check.py [root]
       python3 scripts/scan/models_provider_field_check.py --root <root>
       python3 scripts/scan/models_provider_field_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import argparse
import os
import re
import sys
import tempfile

APP = "src/android/app/src/main/java/com/rikkaminis/app"
MC_REL = APP + "/config/collections/ModelsCollection.kt"
MD_REL = APP + "/provider/ModelsDevApi.kt"
OA_REL = APP + "/provider/openai/OpenAIModelsApi.kt"
AD_REL = APP + "/provider/ModelListProviderAdapters.kt"
CDM_REL = APP + "/debug/ChatDebugMethods.kt"
PDM_REL = APP + "/debug/ProviderDebugMethods.kt"
PMM_REL = APP + "/debug/ProviderMutationMethods.kt"
# [dv23-A04] The thinking-level ceiling resolver. It must read the ENRICHED
# model (models.dev), not the raw `entry.model`.
TLC_REL = APP + "/provider/ThinkingLevelCatalog.kt"
CVM_REL = APP + "/ui/chat/ChatViewModel.kt"
# [deep-audit-1044 B-04] The two capability readers that live OUTSIDE
# ChatViewModel.kt — the FE-5 extractions moved them into extension files and
# dv23-B07 only fixed the four members that stayed inside the class, so these
# two kept answering from the raw `currentModel` snapshot.
CEH_REL = APP + "/ui/chat/ChatErrorHandling.kt"
CST_REL = APP + "/ui/chat/ChatSlashTokenExt.kt"

ARM1_FORBIDDEN = ("provider = instanceId",)
ARM1_REQUIRED = ("instance.providerType.displayName",)
MAP_NEW_KEYS = ("Google Gemini", "xAI (Grok)", "Kimi Code")
MAP_SHORT_KEYS = ("Anthropic", "Google", "OpenAI", "OpenRouter",
                 "Antigravity", "xAI", "Kimi")


def code_only(text):
    """Text with line comments (//…) removed — presence checks must not be
    satisfiable from prose in a comment."""
    return "\n".join(line.split("//")[0] for line in text.split("\n"))


def _balanced(src, open_idx, opener="(", closer=")"):
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == opener:
            depth += 1
        elif src[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def function_text(src, header):
    i = src.find(header)
    if i < 0:
        return None
    brace = src.find("{", i)
    if brace < 0:
        return None
    end = _balanced(src, brace, "{", "}")
    return None if end < 0 else src[i:end]


_DECL_OR_DOC = re.compile(
    r"(fun\b|val\b|var\b|internal\b|private\b|public\b|override\b|suspend\b|"
    r"companion\b|object\b|class\b|data\b|/\*\*)")


def fn_text(src, header):
    """Declaration text for EITHER body shape — `{ … }` or `= expression`.

    [function_text] only understands brace bodies; `resolveProviderName` is an
    expression-bodied one-liner (`… = providerName ?: if (…) "Custom" else
    "OpenAI"`), so scanning for `{` grabs the *next* function's braces. Here
    the scan is parenthesis-aware: a `{` at paren depth 0 opens a brace body,
    a newline at depth 0 followed by a new declaration / `}` / KDoc ends an
    expression body.
    """
    i = src.find(header)
    if i < 0:
        return None
    n = len(src)
    depth = 0
    j = i
    while j < n:
        c = src[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "{":
            end = _balanced(src, j, "{", "}")
            return src[i:end] if end > 0 else None
        elif c == "\n" and depth == 0:
            k = j + 1
            while k < n and src[k] in " \t":
                k += 1
            if k >= n or src[k] == "}" or _DECL_OR_DOC.match(src, k):
                return src[i:j]
        j += 1
    return src[i:]


def map_literal_text(src):
    """The `providerKeyMap = mapOf(...)` expression body (code-only)."""
    i = code_only(src).find("providerKeyMap = mapOf(")
    if i < 0:
        return None
    code = code_only(src)
    open_idx = code.find("mapOf(", i) + len("mapOf(") - 1
    end = _balanced(code, open_idx)
    return None if end < 0 else code[open_idx:end]


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def read(rel, root):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def check_arm1(src, rel):
    body = function_text(src, "override fun add(")
    if body is None:
        return [(rel, 0, "1", "override fun add( not found — gate ROT")]
    code = code_only(body)
    out = []
    for token in ARM1_FORBIDDEN:
        if token in code:
            out.append((rel, 0, "1",
                        "ModelsCollection.add persists `%s` — the raw instance "
                        "UUID; write instance.providerType.displayName instead"
                        % token))
    for anchor in ARM1_REQUIRED:
        if anchor not in code:
            out.append((rel, 0, "1",
                        "ModelsCollection.add missing required code anchor `%s`"
                        % anchor))
    # model_id gate must trim before validating. Anchor the declaration line
    # (plus the continuation line, Kotlin breaks after `?.value`) and demand
    # `.trim()` inside that statement. Fail-closed: no declaration ⇒ red.
    lines = code.split("\n")
    decl = None
    for i, ln in enumerate(lines):
        if "val modelIdStr" in ln:
            decl = (ln + " " + (lines[i + 1] if i + 1 < len(lines) else ""))
            break
    if decl is None:
        out.append((rel, 0, "1",
                    "ModelsCollection.add: `val modelIdStr` declaration not "
                    "found — gate ROT"))
    elif ".trim()" not in decl:
        out.append((rel, 0, "1",
                    "ModelsCollection.add: model_id gate does not trim "
                    "(`val modelIdStr` statement lacks `.trim()`) — a "
                    "whitespace-only id passes isEmpty()"))
    return out


def check_arm2(src, rel):
    body = map_literal_text(src)
    if body is None:
        return [(rel, 0, "2", "providerKeyMap = mapOf( not found — gate ROT")]
    out = []
    for key in MAP_NEW_KEYS:
        if ('"%s"' % key) not in body:
            out.append((rel, 0, "2",
                        "providerKeyMap missing displayName key `\"%s\"` "
                        "(custom-model adds persist displayName)" % key))
    for key in MAP_SHORT_KEYS:
        if ('"%s"' % key) not in body:
            out.append((rel, 0, "2",
                        "providerKeyMap lost original short key `\"%s\"` "
                        "(built-in catalog rows and historical data still "
                        "carry it — idempotence broken)" % key))
    return out


def object_blocks(src):
    """[(name, body_offset, body_text)] for `object <Name> : <Iface> { ... }`.

    Body text is a slice of [src] starting at the object header's end, so a
    match offset inside a body maps back to the file with `body_offset + off`.
    """
    out = []
    for m in re.finditer(r"\bobject\s+(\w+)\s*:\s*[\w.]+", src):
        brace = src.find("{", m.end())
        if brace < 0:
            continue
        end = _balanced(src, brace, "{", "}")
        if end < 0:
            continue
        out.append((m.group(1), m.end(), src[m.end():end]))
    return out


def registrations(src):
    """object name -> ProviderType constant, from the registry wiring lines."""
    return {m.group(2): m.group(1)
            for m in re.finditer(
                r"register\(\s*ProviderType\.(\w+)\s*,\s*(\w+)\s*\)", src)}


def openai_call_sites(block):
    """[(start, end, args)] for OpenAIModelsApi.fetchModels(...) in [block].
    None => at least one call's argument list is unbalanced (gate ROT)."""
    out = []
    for m in re.finditer(r"OpenAIModelsApi\.fetchModels\s*\(", block):
        end = _balanced(block, m.end() - 1)
        if end < 0:
            return None
        out.append((m.start(), end, block[m.end():end - 1]))
    return out


def adapter_delivers_name(block, call):
    """Does this call site say which family the rows belong to?

    Two shapes are accepted, so the gate pins the DECISION and not a spelling
    (same spirit as dv15-A2 accepting both `isSymbolicLink` and a NOFOLLOW
    open):

      a) a `providerName = <expr>` argument whose value is not the literal
         `null` (the fetcher takes it), or
      b) a post-fetch relabel — the call chained into a `.map { }` /
         `.mapNotNull { }` that assigns `provider = …` — which is
         behaviourally equivalent and must not be forced into shape (a).
    """
    _start, end, args = call
    m = re.search(r"\bproviderName\s*=\s*([A-Za-z_][\w.]*)", args)
    # `providerName = null` is the defect spelled politely — it re-declares
    # the default and hides it from the reader. The value must name something.
    if m and m.group(1) != "null":
        return True
    tail = block[end:]
    m = re.match(r"\s*\.(map|mapIndexed|mapNotNull)\s*\{", tail)
    if not m:
        return False
    brace = end + m.end() - 1
    b_end = _balanced(block, brace, "{", "}")
    if b_end < 0:
        return False
    return re.search(r"\bprovider\s*=", block[brace:b_end]) is not None


def check_arm3(src, rel):
    """A vendor adapter that reuses OpenAIModelsApi must name its family.

    Only `ProviderType.openAI`'s own adapter is exempt: its rows are either
    official OpenAI or an unknown third-party endpoint, and "Custom"/"OpenAI"
    IS the honest label there. Every other family (Kimi Code) fetches from its
    own vendor base URL, which the fetcher cannot recognise — let it fall
    through to "Custom" and the rows stop resolving through
    ModelsDevApi.providerKeyMap (deep-audit-1038 A-08 / the F-212 failure).
    """
    blocks = object_blocks(src)
    if not blocks:
        return [(rel, 0, "3",
                 "no `object X : ModelListProvider` declaration found — gate ROT")]
    regs = registrations(src)
    if not regs:
        return [(rel, 0, "3",
                 "no `ModelListProviderRegistry.register(ProviderType.X, Y)` "
                 "wiring found — gate ROT (family of an adapter is unknowable)")]
    out = []
    vendor_sites = 0
    for name, off, body in blocks:
        calls = openai_call_sites(body)
        if calls is None:
            out.append((rel, line_of(src, off), "3",
                        "%s: OpenAIModelsApi.fetchModels(...) argument list "
                        "unbalanced — gate ROT" % name))
            continue
        if not calls:
            continue
        if regs.get(name) == "openAI":
            # Generic OpenAI-compatible adapter — Custom/OpenAI is its label.
            continue
        vendor_sites += len(calls)
        for start, _end, _args in calls:
            if adapter_delivers_name(body, (start, _end, _args)):
                continue
            out.append((rel, line_of(src, off + start), "3",
                        "%s delegates to OpenAIModelsApi.fetchModels for its own "
                        "vendor base URL but supplies no provider name — neither "
                        "a `providerName = <expr>` argument nor a post-fetch "
                        "`.map { … provider = … }` relabel. Every returned row is "
                        "stamped \"Custom\", so ModelsDevApi enrichment can never "
                        "hit providerKeyMap for it (dv18-A-08)" % name))
    if vendor_sites == 0 and not out:
        out.append((rel, 0, "3",
                    "no non-openAI adapter delegating to OpenAIModelsApi detected "
                    "— gate ROT (detection lost; this is exactly the shape that "
                    "let the Kimi rows ship as \"Custom\")"))
    return out


def check_arm4(src, rel):
    """The `provider` decision in OpenAIModelsApi: optional, single-homed, wired.

    Three ways the A-08 fix can rot without any adapter changing:
      4a the `providerName` parameter loses its `= null` default (unnamed
         callers can no longer keep the legacy label — the regression guard
         the fix ships with),
      4b [resolveProviderName] loses the legacy half of its decision (the
         "Custom" branch for unnamed third-party base URLs),
      4c the row construction stops routing through [resolveProviderName]
         (the parameter is accepted and then ignored),
      4d the cache path stops re-labelling rows read back from the
         48-hour cache (the parameter only reaches freshly parsed rows).
    """
    code = code_only(src)
    out = []
    head = code.find("suspend fun fetchModels(")
    if head < 0:
        return [(rel, 0, "4", "`suspend fun fetchModels(` not found — gate ROT")]
    sig_end = _balanced(code, head + len("suspend fun fetchModels"))
    if sig_end < 0:
        return [(rel, 0, "4", "fetchModels signature unbalanced — gate ROT")]
    sig = code[head:sig_end]
    if not re.search(r"providerName\s*:\s*String\?\s*=\s*null", sig):
        out.append((rel, 0, "4",
                    "fetchModels no longer declares an optional nullable "
                    "`providerName: String? = null` — adapters that do NOT name "
                    "their family must keep the legacy Custom/OpenAI label "
                    "(dv18-A-08 regression guard)"))
    body = fn_text(code, "suspend fun fetchModels(")
    if body is None:
        out.append((rel, 0, "4", "fetchModels body unbalanced — gate ROT"))
        return out
    helper = fn_text(code, "fun resolveProviderName(")
    if helper is None:
        out.append((rel, 0, "4",
                    "`fun resolveProviderName(...)` not found — the single "
                    "decision point for the row's provider name is gone"))
    else:
        for token, why in (("isCustomBase", "the official/third-party input"),
                           ("providerName", "the caller-supplied family name"),
                           ('"Custom"', "the legacy third-party label"),
                           ('"OpenAI"', "the legacy official label")):
            if token not in helper:
                out.append((rel, 0, "4",
                            "resolveProviderName dropped %s (`%s`) — one of the two "
                            "decisions cannot be made any more (dv18-A-08)"
                            % (why, token)))
    if "provider = resolveProviderName(" not in body:
        out.append((rel, 0, "4",
                    "the model row's `provider =` no longer calls "
                    "resolveProviderName(...) — a named caller would be accepted "
                    "and silently ignored, every row keeping the legacy label "
                    "(dv18-A-08)"))
    if "withProviderName(" not in body:
        out.append((rel, 0, "4",
                    "the cache return path no longer re-labels cached rows "
                    "(`withProviderName(...)`) — for the 48-hour cache TTL a named "
                    "caller still reads rows stamped \"Custom\" (dv18-A-08)"))
    return out


def _resolver_calls(code):
    return len(re.findall(r"ModelsDevApi\.effectiveModel\(", code))

# [dv22-A4] The two raw chains the collection readers used to walk.
MC_RAW_WINDOW = "e.overrides.contextWindow ?: e.baseModel.contextWindow"
MC_RAW_TOKENS = "e.overrides.maxOutputTokens ?: e.baseModel.maxOutputTokens"
MC_ALIAS = re.compile(
    r"private fun effectiveModel\(e: ModelEntry\): LLMModel\s*=\s*"
    r"ModelsDevApi\.effectiveModel\(e\)")

def check_arm5_mc(src, rel):
    """ModelsCollection: numeric readers through the shared resolver.

    The collection is allowed to expose one private alias (the parity test
    pins the literal `ModelsDevApi.effectiveModel(` count at exactly 1, so a
    second direct call would break that fence anyway) — the decision under
    test is that the numeric readers read the ENRICHED model, and that the
    pre-fix `overrides ?: baseModel` chain is gone.
    """
    code = code_only(src)
    out = []
    if "effectiveModel(e).contextWindow" not in code:
        out.append((rel, 0, "5",
                    "models.<id>.contextWindow reader no longer reads the effective "
                    "model (`effectiveModel(e).contextWindow` absent) — it would fall "
                    "back to the raw base row again (dv22-A4)"))
    if "effectiveModel(e).maxOutputTokens" not in code:
        out.append((rel, 0, "5",
                    "models.<id>.maxOutputTokens reader no longer reads the effective "
                    "model (`effectiveModel(e).maxOutputTokens` absent) (dv22-A4)"))
    if MC_RAW_WINDOW in code:
        out.append((rel, 0, "5",
                    "the raw `%s` chain is back — that read skips models.dev, so 33 of "
                    "the 39 built-ins answer 0 (dv22-A4)" % MC_RAW_WINDOW))
    if MC_RAW_TOKENS in code:
        out.append((rel, 0, "5",
                    "the raw `%s` chain is back (dv22-A4)" % MC_RAW_TOKENS))
    if not MC_ALIAS.search(code):
        out.append((rel, 0, "5",
                    "the private `effectiveModel(e: ModelEntry): LLMModel = "
                    "ModelsDevApi.effectiveModel(e)` alias is gone — either the shared "
                    "resolver is no longer used here, or the anchor moved (gate ROT)"))
    n = _resolver_calls(code)
    if n != 1:
        out.append((rel, 0, "5",
                    "ModelsCollection must carry exactly ONE literal "
                    "`ModelsDevApi.effectiveModel(` call (the private alias — the parity "
                    "test pins the same count); found %d. If a second call site is "
                    "genuinely needed, route it through the alias and update this gate."
                    % n))
    return out

def check_arm5_debug(src, rel, view):
    """A Debug RPC view must answer from the enriched effective model."""
    code = code_only(src)
    out = []
    n = _resolver_calls(code)
    if n != 1:
        out.append((rel, 0, "5",
                    "%s must resolve its effective model through exactly one "
                    "`ModelsDevApi.effectiveModel(` call; found %d%s"
                    % (view, n,
                       " — zero means the raw entry is back (null windows, no "
                       "capability flags over the harness)" if n == 0 else "")))
    if re.search(r"val\s+effective(\s*:\s*LLMModel)?\s*=\s*entry\.model", code):
        out.append((rel, 0, "5",
                    "%s still treats the raw `entry.model` as the effective model — "
                    "that read skips models.dev enrichment (dv22-A4)" % view))
    return out

def check_arm5_tlc(src, rel):
    """[dv23-A04] The thinking-level ceiling must resolve off the ENRICHED model.

    `ModelEntry.effectiveMaxThinkingLevel` used to read
    `model.catalogMaxThinkingLevel`, where `model` is only
    `baseModel.withOverrides(overrides)` — a chain that never touches the
    models.dev registry. Every built-in row therefore kept
    `reasoningEffortValues == null` and `supportsReasoning == null`, so the
    data-driven tier of `catalogMaxThinkingLevel` was dead code for them and
    a custom model whose registry row says `reasoning = true` resolved to
    OFF. The ceiling has to come from `ModelsDevApi.effectiveModel(this)`.

    The criterion pins the DECISION (the ceiling reads the enriched model),
    not the spelling: any shape that resolves the ceiling through the shared
    resolver passes, while a raw `model.catalogMaxThinkingLevel` read reds.
    """
    code = code_only(src)
    out = []
    n = _resolver_calls(code)
    if n == 0:
        out.append((rel, 0, "5",
                    "the thinking-level ceiling no longer resolves through "
                    "`ModelsDevApi.effectiveModel(` — it reads the raw "
                    "`entry.model` again, so models.dev-declared effort tiers "
                    "and reasoning flags are invisible to the picker (dv23-A04)"))
    elif n > 1:
        out.append((rel, 0, "5",
                    "the thinking-level ceiling carries %d literal "
                    "`ModelsDevApi.effectiveModel(` calls — exactly one reader "
                    "is expected (the ceiling); route extras through a local "
                    "val (dv23-A04)" % n))
    # The ceiling read itself must not go through the raw `model` chain.
    if re.search(r"\?\s*:\s*model\.catalogMaxThinkingLevel", code):
        out.append((rel, 0, "5",
                    "the ceiling still reads the raw `model.catalogMaxThinkingLevel` "
                    "— that model is only `baseModel.withOverrides(overrides)` and "
                    "skips models.dev entirely (dv23-A04)"))
    if re.search(r"catalogMaxThinkingLevel", code) and n == 0:
        pass  # already reported by the resolver-count finding
    return out


# [deep-audit-1043 B-07] Which readers must route through the single exit, and
# what each one drives — the label is used in the finding so a broken reader is
# identifiable without opening the file.
CVM_READERS = (
    ("currentModelSupportsImages", "vision predicate"),
    ("effectiveContextWindowTokens", "capacity judgment"),
    ("currentModelContextWindowSource", "Token Usage source row"),
    ("currentModelMaxOutputTokens", "Token Usage max-output row"),
    # [deep-audit-1044 B-04] The two remaining in-class capabilities: the
    # thinking toggle and the thinking ceiling. Both read the raw snapshot, so
    # a model whose `supportsReasoning` / effort tiers exist only in the
    # models.dev row reported "no thinking" and a ceiling the wire would clamp.
    ("currentModelSupportsReasoning", "thinking toggle"),
    ("currentModelMaxThinkingLevel", "thinking ceiling"),
)
# The duplicated lookup the four readers each carried before the fix: it reads
# the entry's BASE model, skipping models.dev enrichment.
CVM_RAW_ENTRY = re.compile(
    r"modelEntries\.find\s*\{\s*it\.id\s*==\s*id\s*\}\s*\?\.model")
CVM_DECL = r"^    (?:internal |private |public )?(?:val|fun)\s+%s\b"

def _decl_block(code, name, max_lines=40):
    """The declaration line(s) of member `name`, up to its closing brace or the
    next member declaration. Works for both `get() =` one-liners and `{ }`
    function bodies, so the gate follows the decision, not a line count."""
    m = re.search(CVM_DECL % re.escape(name), code, re.M)
    if not m:
        return None
    tail = code[m.start():].split("\n")
    out = [tail[0]]
    depth = 0
    for line in tail[1:max_lines]:
        if depth <= 0 and len(out) > 1 and (
                line.strip() == ""
                or re.match(r"^    (?:internal |private |public )?(?:val|fun)\s", line)
                or re.match(r"^    (?:/\*\*|// )", line)):
            break
        out.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0 and (line.strip() == "}" or line.rstrip().endswith("?:")):
            break
    return "\n".join(out)

def check_arm6_chat(src, rel):
    """ChatViewModel: every capability / window reader through one live exit."""
    code = code_only(src)
    out = []
    if not re.search(r"(?:private|internal) fun liveEffectiveModel\(\)\s*:\s*LLMModel\?\s*=", code):
        out.append((rel, 0, "6",
                    "the single live-model exit `private|internal fun liveEffectiveModel(): "
                    "LLMModel?` is gone — the four readers are free to re-derive the "
                    "lookup again (dv23-B7)"))
        body = ""
    else:
        body = _decl_block(code, "liveEffectiveModel") or ""
    if body and "ModelsDevApi.effectiveModel(" not in body:
        out.append((rel, 0, "6",
                    "liveEffectiveModel no longer enriches the active entry through "
                    "`ModelsDevApi.effectiveModel(` — without it 33 of the 39 built-ins "
                    "answer with no contextWindow / maxOutputTokens / modalities "
                    "(dv23-B7)"))
    if body and "ModelsDevApi.enrichModel(" not in body:
        out.append((rel, 0, "6",
                    "the snapshot fallback is not enriched (`ModelsDevApi.enrichModel(` "
                    "absent) — a group-resolved turn keeps reporting HEURISTIC and no "
                    "max-output row (dv23-B7)"))
    for name, label in CVM_READERS:
        blk = _decl_block(code, name)
        if blk is None:
            out.append((rel, 0, "6",
                        "reader `%s` (%s) is gone — gate ROT" % (name, label)))
        elif "liveEffectiveModel()" not in blk:
            out.append((rel, 0, "6",
                        "`%s` (%s) no longer reads through `liveEffectiveModel()` — it "
                        "re-derives the model on its own again (dv23-B7)"
                        % (name, label)))
    vision = _decl_block(code, "currentModelSupportsImages") or ""
    if vision and "normalizeModalities()" not in vision:
        out.append((rel, 0, "6",
                    "the vision predicate stopped normalising modality spellings "
                    "(`normalizeModalities()` absent) — a model declaring `image_input` "
                    "is misread as text-only (dv23-B7)"))
    if CVM_RAW_ENTRY.search(code):
        out.append((rel, 0, "6",
                    "the duplicated `modelEntries.find { it.id == id }?.model` lookup is "
                    "back — that read skips models.dev enrichment (dv23-B7)"))
    if re.search(r"currentModel\?\s*\.\s*maxOutputTokens", code):
        out.append((rel, 0, "6",
                    "`currentModel?.maxOutputTokens` is back — the snapshot is the raw "
                    "base model, so the row disappears for 33 built-ins (dv23-B7)"))
    if re.search(r"currentModel\?\s*\.\s*inputModalities", code):
        out.append((rel, 0, "6",
                    "`currentModel?.inputModalities` is back — a raw null falls through "
                    "to `?: true` and vision support is misjudged (dv23-B7)"))
    return out

# [deep-audit-1044 B-04] The two capability readers that were extracted OUT of
# ChatViewModel (FE-5 batches 7/8) — one per file — and therefore escaped
# dv23-B07's four-reader sweep: they still opened with
# `val model = currentModel ?: return …`. Keyed by rels key.
EXT_READERS = {
    "ceh": ("dynamicMaxTokens", "output sizing (remaining-context clamp)"),
    "cst": ("thinkingInfo", "Token Usage Thinking row"),
}

def _block_comment_free(text):
    """[deep-audit-1044] KDoc is prose: it may neither satisfy nor trip a
    judgement. `code_only` only drops `//` lines, so strip `/* … */` too."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)

def _ext_fn(code, name):
    """Body of a top-level `fun ChatViewModel.<name>(…)` extension function."""
    return function_text(code, "fun ChatViewModel.%s(" % name)

def check_arm7_ext_readers(src, rel, key):
    """The out-of-class capability reader of THIS file answers through the live
    exit. Fail-closed per file: the reader disappearing is ROT."""
    name, label = EXT_READERS[key]
    code = _block_comment_free(code_only(src))
    out = []
    blk = _ext_fn(code, name)
    if blk is None:
        out.append((rel, 0, "7",
                    "reader `%s` (%s) is gone — gate ROT" % (name, label)))
    elif "liveEffectiveModel()" not in blk:
        out.append((rel, 0, "7",
                    "`%s` (%s) still opens on the raw `currentModel` snapshot — "
                    "for 33 of the 39 built-ins that snapshot carries no "
                    "models.dev capability data, so the remaining-context clamp "
                    "and the Thinking row silently fall back to the provider "
                    "default (dv24-B04)" % (name, label)))
    return out

def run_checks(root, rels=None):
    rels = rels or {"mc": MC_REL, "md": MD_REL, "oa": OA_REL, "ad": AD_REL,
                    "cdm": CDM_REL, "pdm": PDM_REL, "pmm": PMM_REL,
                    "cvm": CVM_REL, "tlc": TLC_REL,
                    "ceh": CEH_REL, "cst": CST_REL}
    srcs = {k: read(rel, root) for k, rel in rels.items()}
    missing = [(rel, 0, "?", "file not found — gate ROT")
               for k, rel in rels.items() if srcs[k] is None]
    if missing:
        return missing
    checks = []
    checks += check_arm1(srcs["mc"], rels["mc"])
    checks += check_arm2(srcs["md"], rels["md"])
    checks += check_arm3(srcs["ad"], rels["ad"])
    checks += check_arm4(srcs["oa"], rels["oa"])
    checks += check_arm5_mc(srcs["mc"], rels["mc"])
    checks += check_arm5_debug(srcs["cdm"], rels["cdm"], "chat.models.list")
    checks += check_arm5_debug(srcs["pdm"], rels["pdm"], "provider.models.list")
    checks += check_arm5_debug(srcs["pmm"], rels["pmm"],
                               "provider.models.add/update read-back (entryToJson)")
    checks += check_arm5_tlc(srcs["tlc"], rels["tlc"])
    checks += check_arm6_chat(srcs["cvm"], rels["cvm"])
    checks += check_arm7_ext_readers(srcs["ceh"], rels["ceh"], "ceh")
    checks += check_arm7_ext_readers(srcs["cst"], rels["cst"], "cst")
    return checks


# ----------------------------------------------------------------------
# Selftest fixtures — minimal Kotlin-shaped files exercising every arm.
# ----------------------------------------------------------------------
FIX_MC_OK = """object ModelsCollection {
    private fun effectiveModel(e: ModelEntry): LLMModel =
        ModelsDevApi.effectiveModel(e)

    private fun effectiveModalities(e: ModelEntry): List<String> =
        effectiveModel(e).directionalModalities

    fun readers() {
        val cw = effectiveModel(e).contextWindow ?: 0
        val mot = effectiveModel(e).maxOutputTokens ?: 0
    }

    override fun add(payload: ConfigValue): String {
        val modelIdStr = (obj["model_id"] as? ConfigValue.Str)?.value?.trim()
            ?: throw ConfigError.InvalidValue("`model_id` required")
        if (modelIdStr.isEmpty()) throw ConfigError.InvalidValue("`model_id` required")
        val instance = repo.instance(instanceId)
            ?: throw ConfigError.InvalidValue("No provider instance with id $instanceId")
        val model = LLMModel(
            id = modelIdStr,
            provider = instance.providerType.displayName,
        )
        return repo.addEntry(entry)
    }
}
"""

# Pre-fix shape: UUID into provider + untrimmed id gate.
FIX_MC_PREFIX = """override fun add(payload: ConfigValue): String {
    val modelIdStr = (obj["model_id"] as? ConfigValue.Str)?.value
        ?: throw ConfigError.InvalidValue("`model_id` required")
    if (modelIdStr.isEmpty()) throw ConfigError.InvalidValue("`model_id` required")
    if (repo.instance(instanceId) == null) {
        throw ConfigError.InvalidValue("No provider instance with id $instanceId")
    }
    val model = LLMModel(
        id = modelIdStr,
        provider = instanceId,
    )
    return repo.addEntry(entry)
}
"""

# Half-fixed: trims, but still persists the UUID.
FIX_MC_TRIM_ONLY = """override fun add(payload: ConfigValue): String {
    val modelIdStr = (obj["model_id"] as? ConfigValue.Str)?.value?.trim()
        ?: throw ConfigError.InvalidValue("`model_id` required")
    if (modelIdStr.isEmpty()) throw ConfigError.InvalidValue("`model_id` required")
    val model = LLMModel(
        id = modelIdStr,
        provider = instanceId,
    )
    return repo.addEntry(entry)
}
"""

FIX_MD_OK = """object ModelsDevApi {
    private val providerKeyMap = mapOf(
        "Anthropic" to listOf("anthropic"),
        "Google" to listOf("google", "google-vertex"),
        "OpenAI" to listOf("openai"),
        "OpenRouter" to listOf("openrouter"),
        "Antigravity" to emptyList(),
        "xAI" to listOf("xai"),
        "Kimi" to listOf("moonshotai", "moonshotai-cn"),
        "Google Gemini" to listOf("google", "google-vertex"),
        "xAI (Grok)" to listOf("xai"),
        "Kimi Code" to listOf("moonshotai", "moonshotai-cn"),
    )
}
"""

# Pre-fix map: short keys only.
FIX_MD_PREFIX = """object ModelsDevApi {
    private val providerKeyMap = mapOf(
        "Anthropic" to listOf("anthropic"),
        "Google" to listOf("google", "google-vertex"),
        "OpenAI" to listOf("openai"),
        "OpenRouter" to listOf("openrouter"),
        "Antigravity" to emptyList(),
        "xAI" to listOf("xai"),
        "Kimi" to listOf("moonshotai", "moonshotai-cn"),
    )
}
"""

# Idempotence-broken map: displayName keys kept, short keys dropped.
FIX_MD_SHORTKEYS_DROPPED = """object ModelsDevApi {
    private val providerKeyMap = mapOf(
        "Anthropic" to listOf("anthropic"),
        "Google Gemini" to listOf("google", "google-vertex"),
        "OpenAI" to listOf("openai"),
        "OpenRouter" to listOf("openrouter"),
        "Antigravity" to emptyList(),
        "xAI (Grok)" to listOf("xai"),
        "Kimi Code" to listOf("moonshotai", "moonshotai-cn"),
    )
}
"""

# ----------------------------------------------------------------------
# Arm 3 / arm 4 fixtures (deep-audit-1038 A-08).
# ----------------------------------------------------------------------
AD_FIX_OK = """package probe

import com.rikkaminis.app.provider.openai.OpenAIModelsApi

private object OpenAIModelListAdapter : ModelListProvider {
    override suspend fun fetchModels(apiKey: String?, instance: ProviderInstance): List<LLMModel> {
        return OpenAIModelsApi.fetchModels(
            apiKey,
            instance.effectiveBaseURL,
            context = context,
            customUserAgent = instance.customUserAgent,
            azureMode = instance.azureMode,
        )
    }
}

private object KimiModelListAdapter : ModelListProvider {
    override suspend fun fetchModels(apiKey: String?, instance: ProviderInstance): List<LLMModel> {
        return OpenAIModelsApi.fetchModels(
            apiKey,
            baseURL,
            context = context,
            customUserAgent = instance.customUserAgent,
            azureMode = false,
            providerName = ProviderType.kimiCode.displayName,
        )
    }
}

fun registerModelListProviders() {
    ModelListProviderRegistry.register(ProviderType.openAI, OpenAIModelListAdapter)
    ModelListProviderRegistry.register(ProviderType.kimiCode, KimiModelListAdapter)
}
"""

# Vendor adapter silently reverts to the defect: no name anywhere.
AD_NO_NAME = AD_FIX_OK.replace(
    "            providerName = ProviderType.kimiCode.displayName,\n", "")

# Accepted alternative shape: post-fetch relabel instead of the argument.
AD_POSTMAP = AD_NO_NAME.replace(
    "        return OpenAIModelsApi.fetchModels(\n"
    "            apiKey,\n"
    "            baseURL,\n"
    "            context = context,\n"
    "            customUserAgent = instance.customUserAgent,\n"
    "            azureMode = false,\n"
    "        )",
    "        return OpenAIModelsApi.fetchModels(\n"
    "            apiKey,\n"
    "            baseURL,\n"
    "            context = context,\n"
    "            customUserAgent = instance.customUserAgent,\n"
    "            azureMode = false,\n"
    "        ).map { it.copy(provider = ProviderType.kimiCode.displayName) }")

# Explicit `null` is the same defect spelled politely.
AD_NULL_NAME = AD_FIX_OK.replace(
    "providerName = ProviderType.kimiCode.displayName",
    "providerName = null")

# ROT: only the generic adapter remains — detection is gone.
AD_NO_VENDOR = AD_FIX_OK.split("private object KimiModelListAdapter")[0] + """fun registerModelListProviders() {
    ModelListProviderRegistry.register(ProviderType.openAI, OpenAIModelListAdapter)
}
"""

OA_FIX_OK = """package probe

object OpenAIModelsApi {
    suspend fun fetchModels(
        apiKey: String,
        baseURL: String? = null,
        azureMode: Boolean = false,
        providerName: String? = null,
    ): List<LLMModel> {
        val isCustomBase = baseURL != null && !isOfficialOpenAI(baseURL)
        if (shouldConsultCache()) {
            cache.load(cacheKey)?.let {
                return ModelsDevApi.applyGatewayEffortTruth(
                    it.withProviderName(isCustomBase, providerName), baseURL
                )
            }
        }
        return listOf(
            LLMModel(
                id = id,
                provider = resolveProviderName(isCustomBase, providerName),
            )
        )
    }

    internal fun resolveProviderName(isCustomBase: Boolean, providerName: String?): String =
        providerName ?: if (isCustomBase) "Custom" else "OpenAI"
}
"""

# 4a: the parameter lost its nullable default.
OA_NO_DEFAULT = OA_FIX_OK.replace(
    "providerName: String? = null,", "providerName: String?,")

# 4b: the legacy half of the decision is gone.
OA_DECISION_BROKEN = OA_FIX_OK.replace(
    'providerName ?: if (isCustomBase) "Custom" else "OpenAI"',
    'providerName ?: "OpenAI"')

# 4c: parameter accepted, then ignored at the row construction.
OA_WIRING_DROPPED = OA_FIX_OK.replace(
    "provider = resolveProviderName(isCustomBase, providerName),",
    'provider = if (isCustomBase) "Custom" else "OpenAI",')

# 4d: the cache path stops re-labelling cached rows.
OA_CACHE_DROPPED = OA_FIX_OK.replace(
    "it.withProviderName(isCustomBase, providerName), baseURL",
    "it, baseURL")

# ----------------------------------------------------------------------
# Arm 5 fixtures (deep-audit-1042 A-04) — the four non-UI bypass sites.
# ----------------------------------------------------------------------
MC_A5_OK = """package probe

object ModelsCollection {
    private fun effectiveModel(e: ModelEntry): LLMModel =
        ModelsDevApi.effectiveModel(e)

    private fun effectiveModalities(e: ModelEntry): List<String> =
        effectiveModel(e).directionalModalities

    fun readers() {
        val cw = effectiveModel(e).contextWindow ?: 0
        val mot = effectiveModel(e).maxOutputTokens ?: 0
    }
}
"""

# Pre-fix: raw chains, no numeric-reader resolver.
MC_A5_PREFIX = """package probe

object ModelsCollection {
    private fun effectiveModalities(e: ModelEntry): List<String> =
        ModelsDevApi.effectiveModel(e).directionalModalities

    fun readers() {
        val cw = e.overrides.contextWindow ?: e.baseModel.contextWindow ?: 0
        val mot = e.overrides.maxOutputTokens ?: e.baseModel.maxOutputTokens ?: 0
    }
}
"""

# Half-fixed: alias kept, but the raw window chain is back.
MC_A5_RAW_BACK = MC_A5_OK.replace(
    "val cw = effectiveModel(e).contextWindow ?: 0",
    "val cw = e.overrides.contextWindow ?: e.baseModel.contextWindow ?: 0")

# Alias dropped in favour of two direct calls — breaks the parity fence too.
MC_A5_TWO_CALLS = """package probe

object ModelsCollection {
    fun readers() {
        val cw = ModelsDevApi.effectiveModel(e).contextWindow ?: 0
        val mot = ModelsDevApi.effectiveModel(e).maxOutputTokens ?: 0
    }
}
"""

# Alias lost entirely (its own anchor — the ROT arm).
MC_A5_ALIAS_GONE = MC_A5_OK.replace(
    "    private fun effectiveModel(e: ModelEntry): LLMModel =\n"
    "        ModelsDevApi.effectiveModel(e)\n", "")

def _debug_view(effective_line):
    return """package probe

object View {
    fun modelsList() {
        for (entry in visible) {
            %s
            put("contextWindow", effective.contextWindow ?: JSONObject.NULL)
        }
    }
}
""" % effective_line

CDM_A5_OK = _debug_view(
    "val effective = com.rikkaminis.app.provider.ModelsDevApi.effectiveModel(entry)")
CDM_A5_PREFIX = _debug_view("val effective = entry.model")
CDM_A5_EQUIV_RAW = _debug_view(
    "val effective = entry.baseModel.withOverrides(entry.overrides)")

# [dv23-A04] Thinking-level ceiling fixtures.
TLC_A5_OK = """val ModelEntry.effectiveMaxThinkingLevel: ThinkingLevel
    get() = overrides.maxThinkingLevel
        ?: ModelsDevApi.effectiveModel(this).catalogMaxThinkingLevel
"""

TLC_A5_PREFIX = """val ModelEntry.effectiveMaxThinkingLevel: ThinkingLevel
    get() = overrides.maxThinkingLevel ?: model.catalogMaxThinkingLevel
"""

# Same intent, wrong outlet: enrichModel(this.model) skips the overrides
# ordering (effectiveEntryModel enriches the BASE model first) — the shape
# the work order explicitly forbids.
TLC_A5_ENRICH_BASE = """val ModelEntry.effectiveMaxThinkingLevel: ThinkingLevel
    get() = overrides.maxThinkingLevel
        ?: ModelsDevApi.enrichModel(model).catalogMaxThinkingLevel
"""

TLC_A5_DOUBLE = """val ModelEntry.effectiveMaxThinkingLevel: ThinkingLevel
    get() = overrides.maxThinkingLevel
        ?: ModelsDevApi.effectiveModel(this).catalogMaxThinkingLevel

val other: ThinkingLevel
    get() = ModelsDevApi.effectiveModel(this).catalogMaxThinkingLevel
"""


def _report(checks, fixture_name, label):
    if not checks:
        print("  [OK]   %s (%s)" % (fixture_name, label))
        return True
    for rel, line, arm, msg in checks:
        print("  [FAIL] %s arm %s line %s: %s" % (fixture_name, arm, line, msg))
    return False


def _expect_count(fixture_name, checks, expect, label):
    """Fail-closed fixture assert: exactly `expect` findings, each named."""
    if len(checks) != expect:
        print("  [FAIL] %s: expected %d finding(s), got %d — %s"
              % (fixture_name, expect, len(checks), [c[3] for c in checks]))
        return False
    print("  [OK]   %s (%s)" % (fixture_name, label))
    return True


CVM_A6_OK = """class ChatViewModel {
    private fun liveEffectiveModel(): LLMModel? =
        _activeEntryId.value
            ?.let { id -> providerRepository.config.value.modelEntries.find { it.id == id } }
            ?.let { ModelsDevApi.effectiveModel(it) }
            ?: currentModel?.let { ModelsDevApi.enrichModel(it) }

    val currentModelSupportsImages: Boolean
        get() = liveEffectiveModel()?.inputModalities?.normalizeModalities()
            ?.any { it.equals("image", ignoreCase = true) } ?: true

    internal fun effectiveContextWindowTokens(): Int? {
        val liveModel = liveEffectiveModel()
        val window = liveModel?.contextWindowTokens ?: return null
        return window
    }

    val currentModelContextWindowSource: LLMModel.ContextWindowSource?
        get() = liveEffectiveModel()?.contextWindowSource

    val currentModelMaxOutputTokens: Int?
        get() = liveEffectiveModel()?.maxOutputTokens

    val currentModelSupportsReasoning: Boolean
        get() = liveEffectiveModel()?.supportsReasoning != false

    internal val currentModelMaxThinkingLevel: ThinkingLevel
        get() {
            val entry = _activeEntryId.value?.let { id ->
                providerRepository.config.value.modelEntries.find { it.id == id }
            }
            if (entry != null) {
                return entry.effectiveMaxThinkingLevel
            }
            val model = liveEffectiveModel() ?: return ThinkingLevel.XHIGH
            return model.catalogMaxThinkingLevel
        }
}
"""

# Pre-fix shape: no exit, every reader re-derives the lookup, the vision
# predicate never normalises, and the raw snapshot is read directly.
CVM_A6_PREFIX = """class ChatViewModel {
    val currentModelSupportsImages: Boolean
        get() = currentModel?.inputModalities?.any { it.equals("image", ignoreCase = true) } ?: true

    internal fun effectiveContextWindowTokens(): Int? {
        val config = providerRepository.config.value
        val liveModel = _activeEntryId.value
            ?.let { id -> config.modelEntries.find { it.id == id }?.model }
            ?: currentModel
        val window = liveModel?.contextWindowTokens ?: return null
        return window
    }

    val currentModelContextWindowSource: LLMModel.ContextWindowSource?
        get() {
            val config = providerRepository.config.value
            val liveModel = _activeEntryId.value
                ?.let { id -> config.modelEntries.find { it.id == id }?.model }
                ?: currentModel
            return liveModel?.contextWindowSource
        }

    val currentModelMaxOutputTokens: Int?
        get() = currentModel?.maxOutputTokens

    val currentModelSupportsReasoning: Boolean
        get() = currentModel?.supportsReasoning != false

    internal val currentModelMaxThinkingLevel: ThinkingLevel
        get() {
            val entry = _activeEntryId.value?.let { id ->
                providerRepository.config.value.modelEntries.find { it.id == id }
            }
            if (entry != null) {
                return entry.effectiveMaxThinkingLevel
            }
            val model = currentModel ?: return ThinkingLevel.XHIGH
            return model.catalogMaxThinkingLevel
        }
}
"""

CVM_A6_NO_ENRICH = CVM_A6_OK.replace(
    "            ?: currentModel?.let { ModelsDevApi.enrichModel(it) }", "")

CVM_A6_MAXOUT_RAW = CVM_A6_OK.replace(
    """    val currentModelMaxOutputTokens: Int?
        get() = liveEffectiveModel()?.maxOutputTokens""",
    """    val currentModelMaxOutputTokens: Int?
        get() = currentModel?.maxOutputTokens""")

CVM_A6_VISION_NO_NORM = CVM_A6_OK.replace(
    """        get() = liveEffectiveModel()?.inputModalities?.normalizeModalities()
            ?.any { it.equals("image", ignoreCase = true) } ?: true""",
    """        get() = liveEffectiveModel()?.inputModalities
            ?.any { it.equals("image", ignoreCase = true) } ?: true""")

CVM_A6_DUP_LOOKUP = CVM_A6_OK.replace(
    """    val currentModelMaxOutputTokens: Int?""",
    """    val duplicateLookup: LLMModel?
        get() = providerRepository.config.value.modelEntries.find { it.id == id }?.model

    val currentModelMaxOutputTokens: Int?""")

CVM_A6_NO_READER = CVM_A6_OK.replace(
    """    val currentModelMaxOutputTokens: Int?
        get() = liveEffectiveModel()?.maxOutputTokens
""", "")

# [deep-audit-1044 B-04] The two out-of-class readers (extension files).
CEH_A7_OK = """internal fun ChatViewModel.dynamicMaxTokens(provider: LLMProvider, lastContextTokens: Int = 0): Int {
    val model = liveEffectiveModel()
        ?: return minOf(ChatViewModel.GLOBAL_MAX_TOKENS_CEILING, provider.defaultMaxOutputTokens)
    val maxOutputCeiling = minOf(ChatViewModel.GLOBAL_MAX_TOKENS_CEILING, provider.effectiveMaxOutputTokens(model))
    return maxOutputCeiling
}
"""

CST_A7_OK = """fun ChatViewModel.thinkingInfo(): ThinkingInfo? {
    val model = liveEffectiveModel() ?: return null
    val supported = model.supportsReasoning != false
    return ThinkingInfo(supported, true, "--")
}
"""

# Pre-fix shapes: both opened on the raw snapshot.
CEH_A7_RAW = CEH_A7_OK.replace(
    """    val model = liveEffectiveModel()
        ?: return minOf""",
    """    val model = currentModel
        ?: return minOf""")

CST_A7_RAW = CST_A7_OK.replace(
    "val model = liveEffectiveModel() ?: return null",
    "val model = currentModel ?: return null")

# A KDoc that merely MENTIONS the exit must not satisfy the judgement.
CST_A7_KDOC_ONLY = """/**
 * Reads [liveEffectiveModel] -- this prose is not a call.
 */
fun ChatViewModel.thinkingInfo(): ThinkingInfo? {
    val model = currentModel ?: return null
    return ThinkingInfo(model.supportsReasoning != false, true, "--")
}
"""

def selftest():
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        rels = {"mc": "mc.kt", "md": "md.kt", "oa": "oa.kt", "ad": "ad.kt",
                "cdm": "cdm.kt", "pdm": "pdm.kt", "pmm": "pmm.kt",
                "tlc": "tlc.kt",
                "cvm": "cvm.kt", "ceh": "ceh.kt", "cst": "cst.kt"}

        def write(name, text):
            with open(os.path.join(tmp, rels[name]), "w", encoding="utf-8") as f:
                f.write(text)

        # Arm-5 OK baselines for the three debug views.
        pdm_ok = CDM_A5_OK.replace(
            "val effective = com.rikkaminis.app.provider."
            "ModelsDevApi.effectiveModel(entry)",
            "val effective: LLMModel = com.rikkaminis.app.provider."
            "ModelsDevApi.effectiveModel(entry)")
        pmm_ok = CDM_A5_OK  # entryToJson shape: un-annotated `val effective`

        # 1. Fixed shape: clean.
        write("mc", FIX_MC_OK)
        write("md", FIX_MD_OK)
        write("oa", OA_FIX_OK)
        write("ad", AD_FIX_OK)
        write("cdm", CDM_A5_OK)
        write("pdm", pdm_ok)
        write("pmm", pmm_ok)
        write("tlc", TLC_A5_OK)
        write("cvm", CVM_A6_OK)
        write("ceh", CEH_A7_OK)
        write("cst", CST_A7_OK)
        ok &= _report(run_checks(tmp, rels), "fixed", "all arms clean")

        # 2. Pre-fix shape: arm1 (UUID + no trim) and arm2 (no new keys).
        write("mc", FIX_MC_PREFIX)
        write("md", FIX_MD_PREFIX)
        checks = run_checks(tmp, rels)
        a1 = [c for c in checks if c[2] == "1"]
        a2 = [c for c in checks if c[2] == "2"]
        if len(a1) < 3 or not any("instanceId" in c[3] for c in a1) \
                or not any("trim" in c[3] for c in a1):
            print("  [FAIL] pre-fix fixture did not trip arm1 fully (%d)" % len(a1))
            ok = False
        else:
            print("  [OK]   pre-fix fixture trips arm1 (uuid+notrim, %d findings)" % len(a1))
        if len(a2) < 3:
            print("  [FAIL] pre-fix fixture did not trip arm2 (missing displayName keys)")
            ok = False
        else:
            print("  [OK]   pre-fix fixture trips arm2 (%d findings)" % len(a2))
        write("mc", FIX_MC_OK)
        write("md", FIX_MD_OK)

        # 3. Half-fixed: trims but persists UUID — only the UUID finding fires.
        write("mc", FIX_MC_TRIM_ONLY)
        checks = run_checks(tmp, rels)
        a1 = [c for c in checks if c[2] == "1"]
        if len(a1) != 2 or not any("instanceId" in c[3] for c in a1) \
                or any("trim" in c[3] for c in a1):
            print("  [FAIL] trim-only fixture: expected exactly uuid finding, got %s"
                  % [c[3] for c in a1])
            ok = False
        else:
            print("  [OK]   trim-only fixture trips arm1 on the uuid alone (%d)" % len(a1))
        write("mc", FIX_MC_OK)

        # 4. Idempotence broken: short keys dropped — arm2 must fire.
        write("md", FIX_MD_SHORTKEYS_DROPPED)
        checks = run_checks(tmp, rels)
        a2 = [c for c in checks if c[2] == "2"]
        if not a2:
            print("  [FAIL] short-keys-dropped fixture did not trip arm2")
            ok = False
        else:
            print("  [OK]   short-keys-dropped fixture trips arm2 (%d findings)" % len(a2))
        write("md", FIX_MD_OK)

        # 5. ROT control: a repo without the target files must fail loudly.
        with tempfile.TemporaryDirectory() as empty:
            rot = run_checks(empty, rels)
            if not rot:
                print("  [FAIL] ROT control: missing files produced no finding")
                ok = False
            else:
                print("  [OK]   ROT control: missing files reported (%d findings)" % len(rot))

        # ---- Arm 3: vendor adapters must name the family they fetch for ----

        def arm3_of(fixture_name, fixture, expect, label):
            write("ad", fixture)
            checks = [c for c in run_checks(tmp, rels) if c[2] == "3"]
            write("ad", AD_FIX_OK)
            return _expect_count("arm 3 " + fixture_name, checks, expect, label)

        ok &= arm3_of("no-name", AD_NO_NAME, 1,
                      "vendor adapter without a provider name trips exactly one")
        ok &= arm3_of("null-name", AD_NULL_NAME, 1,
                      "`providerName = null` is the same defect")
        ok &= arm3_of("post-map", AD_POSTMAP, 0,
                      "post-fetch relabel accepted (decision, not spelling)")
        ok &= arm3_of("no-vendor", AD_NO_VENDOR, 1,
                      "generic-only adapter set still reds (detection lost = ROT)")

        # ---- Arm 4: the naming decision inside OpenAIModelsApi ----

        def arm4_of(fixture_name, fixture, label):
            write("oa", fixture)
            checks = [c for c in run_checks(tmp, rels) if c[2] == "4"]
            write("oa", OA_FIX_OK)
            return _expect_count("arm 4 " + fixture_name, checks, 1, label)

        ok &= arm4_of("no-default", OA_NO_DEFAULT,
                      "losing the `= null` default reds (legacy callers would break)")
        ok &= arm4_of("decision-broken", OA_DECISION_BROKEN,
                      "dropping the legacy \"Custom\" branch reds")
        ok &= arm4_of("wiring-dropped", OA_WIRING_DROPPED,
                      "accepting providerName and ignoring it reds")
        ok &= arm4_of("cache-dropped", OA_CACHE_DROPPED,
                      "no longer re-labelling cached rows reds")

        # ---- Arm 5: the four non-UI sites route through the resolver ----

        def arm5_of(fixture_name, checks, expect, label):
            return _expect_count("arm 5 " + fixture_name, checks, expect, label)

        # (a) ModelsCollection numeric readers.
        write("mc", MC_A5_PREFIX)
        write("cdm", CDM_A5_OK); write("pdm", pdm_ok); write("pmm", pmm_ok)
        checks = [c for c in run_checks(tmp, rels) if c[2] == "5" and c[0].endswith("mc.kt")]
        # 2 reader findings + 2 raw chains + no alias (the modality call keeps
        # the literal count at 1, so no count finding here) = 5.
        ok &= arm5_of("mc-pre-fix", checks, 5,
                      "raw chains + missing readers + no alias reds")
        write("mc", MC_A5_RAW_BACK)
        checks = [c for c in run_checks(tmp, rels) if c[2] == "5" and c[0].endswith("mc.kt")]
        ok &= arm5_of("mc-raw-back", checks, 2,
                      "the raw window chain trips exactly the pair (missing + present)")
        write("mc", MC_A5_TWO_CALLS)
        checks = [c for c in run_checks(tmp, rels) if c[2] == "5" and c[0].endswith("mc.kt")]
        ok &= arm5_of("mc-two-calls", checks, 2,
                      "two direct calls + lost alias red the parity-fenced count")
        write("mc", MC_A5_ALIAS_GONE)
        checks = [c for c in run_checks(tmp, rels) if c[2] == "5" and c[0].endswith("mc.kt")]
        ok &= arm5_of("mc-alias-gone", checks, 2,
                      "a lost alias is its own ROT (anchor + call count)")
        write("mc", FIX_MC_OK)

        # (b) the three debug views, same parameterised pair of shapes.
        for name, ok_fixture, pre, view in (
                ("cdm", CDM_A5_OK, CDM_A5_PREFIX, "chat.models.list"),
                ("pdm", pdm_ok, CDM_A5_PREFIX, "provider.models.list"),
                ("pmm", pmm_ok, CDM_A5_PREFIX, "entryToJson")):
            write(name, pre)
            checks = [c for c in run_checks(tmp, rels)
                      if c[2] == "5" and c[0].endswith(rels[name])]
            ok &= arm5_of(name + "-pre-fix", checks, 2,
                          "%s reads the raw entry (zero calls + raw val)" % view)
            write(name, CDM_A5_EQUIV_RAW)
            checks = [c for c in run_checks(tmp, rels)
                      if c[2] == "5" and c[0].endswith(rels[name])]
            ok &= arm5_of(name + "-equiv-raw", checks, 1,
                          "an equivalent un-enriched rebuild still reds (decision)")
            write(name, ok_fixture)

        # (c) a second resolver call reds too (count is pinned, ROT-guarded).
        write("cdm", CDM_A5_OK + CDM_A5_OK)
        checks = [c for c in run_checks(tmp, rels)
                  if c[2] == "5" and c[0].endswith("cdm.kt")]
        ok &= arm5_of("cdm-double", checks, 1,
                      "a duplicated view reds on the pinned call count alone")
        write("cdm", CDM_A5_OK)

        # (d) [dv23-A04] the thinking-level ceiling must read the enriched model.
        def tlc_of(fixture_name, fixture, expect, label):
            write("tlc", fixture)
            checks = [c for c in run_checks(tmp, rels)
                      if c[2] == "5" and c[0].endswith("tlc.kt")]
            write("tlc", TLC_A5_OK)
            return _expect_count("arm 5 " + fixture_name, checks, expect, label)

        ok &= tlc_of("tlc-ok", TLC_A5_OK, 0,
                     "the enriched ceiling shape is accepted")
        ok &= tlc_of("tlc-pre-fix", TLC_A5_PREFIX, 2,
                     "the raw `model.catalogMaxThinkingLevel` read reds twice "
                     "(no enriched outlet + the raw chain itself)")
        # enrichModel(model) instead of effectiveModel(this): the raw-chain
        # regex does not match it, but the resolver count is 0 -> the
        # "no enriched outlet" finding fires. Exactly one finding.
        ok &= tlc_of("tlc-enrich-base", TLC_A5_ENRICH_BASE, 1,
                     "enriching the already-overridden model reds (wrong outlet)")
        ok &= tlc_of("tlc-double", TLC_A5_DOUBLE, 1,
                     "a duplicated ceiling resolver reds on the pinned count")
        # ---- Arm 6: ChatViewModel's four readers share one live exit ----

        def arm6_of(fixture_name, fixture, expect, label):
            write("cvm", fixture)
            checks = [c for c in run_checks(tmp, rels) if c[2] == "6"]
            write("cvm", CVM_A6_OK)
            return _expect_count("arm 6 " + fixture_name, checks, expect, label)

        # Pre-fix: missing exit + 4 unrouted readers + un-normalised vision +
        # duplicated lookup + the two raw snapshot reads = 9 findings.
        # 9 findings before dv24-B04; the two readers added to CVM_READERS
        # (`currentModelSupportsReasoning` / `currentModelMaxThinkingLevel`) add
        # one each when they are present in the pre-fix shape ⇒ 11.
        ok &= arm6_of("pre-fix", CVM_A6_PREFIX, 11,
                      "the pre-fix shape reds on every reader and the raw reads")
        ok &= arm6_of("no-enrich-fallback", CVM_A6_NO_ENRICH, 1,
                      "a snapshot fallback without models.dev enrichment reds alone")
        ok &= arm6_of("max-output-raw", CVM_A6_MAXOUT_RAW, 2,
                      "one reader back on the snapshot trips its own paired findings")
        ok &= arm6_of("vision-no-normalise", CVM_A6_VISION_NO_NORM, 1,
                      "dropping normalizeModalities reds the vision predicate alone")
        ok &= arm6_of("duplicate-lookup", CVM_A6_DUP_LOOKUP, 1,
                      "re-introducing the raw `?.model` lookup reds alone")
        ok &= arm6_of("reader-gone", CVM_A6_NO_READER, 1,
                      "a removed reader is ROT, not a silent pass")

        # ---- Arm 7: the two capability readers extracted OUT of the class ----

        def arm7_of(fixture_name, name, fixture, expect, label):
            write(name, fixture)
            checks = [c for c in run_checks(tmp, rels) if c[2] == "7"]
            write(name, CEH_A7_OK if name == "ceh" else CST_A7_OK)
            return _expect_count("arm 7 " + fixture_name, checks, expect, label)

        ok &= arm7_of("dynamic-max-tokens-raw", "ceh", CEH_A7_RAW, 1,
                      "`dynamicMaxTokens` back on the snapshot reds its own reader")
        ok &= arm7_of("thinking-info-raw", "cst", CST_A7_RAW, 1,
                      "`thinkingInfo` back on the snapshot reds its own reader")
        ok &= arm7_of("kdoc-only-mention", "cst", CST_A7_KDOC_ONLY, 1,
                      "a KDoc that merely names the exit cannot satisfy the arm")
        ok &= arm7_of("reader-gone", "cst", "", 1,
                      "a removed out-of-class reader is ROT")

    print("MODELS_PROVIDER_FIELD_SELFTEST=%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("root", nargs="?", default=None)
    ap.add_argument("--root", dest="root_opt", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    root = args.root_opt or args.root
    if not root:
        ap.print_usage(sys.stderr)
        return 2
    if not os.path.isdir(root):
        print("root not found: %s" % root, file=sys.stderr)
        return 2

    checks = run_checks(root)
    if not checks:
        print("models_provider_field_check: OK (6 arms clean)")
        return 0
    for rel, line, arm, msg in checks:
        print("%s:%s [arm %s] %s" % (rel, line, arm, msg))
    print("models_provider_field_check: %d violation(s)" % len(checks))
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Gate: FIX-D (deep-audit-1019 ISSUE-04) — per-provider customUserAgent parity
plus the Gemini tool-payload EOF verdict.

ISSUE-04a (ProviderFactory.kt)
  `ProviderInstance.customUserAgent` is settable for every apiKey provider, but
  `ProviderFactory.create` only forwarded it on `openAI` and on the custom-base
  `anthropic` arm — the `anthropic` else arm, both `gemini` arms, `openRouter`,
  `xAI` and `kimiCode` dropped it silently. The rule: EVERY provider
  construction statement inside the factory carries
  `customUserAgent = instance.customUserAgent` (a null/blank value is harmless —
  applyUserAgentOverride falls back to MinisUserAgent.DEFAULT).

ISSUE-04b (GeminiProvider.kt)
  The class had no `customUserAgent` parameter and both request builders
  hard-coded `.applyUserAgentOverride(null)`, so the user's value was
  unreachable even if the factory had passed it. The rule: the constructor
  declares the override, no `.applyUserAgentOverride(null)` spelling remains,
  and both builders pass the constructor value.

ISSUE-04c (GeminiProvider.kt, EOF verdict)
  The SSE EOF verdict `lastFinishReason == null && contentChars > 0` only saw
  text/thinking chars, so a turn that streamed only `functionCall` parts and
  then lost the connection was classified as a clean end_turn and the
  incomplete tool set was executed without a retry. The rule (Anthropic's
  `toolChars` counterpart): the verdict is the pure `shouldFlagEofTruncated`
  function taking BOTH counters, the streaming loop accumulates tool payload
  chars, and the verdict body still contains both `> 0` terms.

Checks assert PRESENCE of the forwarding/counter wiring rather than the absence
of a spelling where possible, so a refactor that keeps the contract is a
deliberate edit to this gate rather than a silent pass.

Usage: python3 scripts/scan/provider_ua_parity_check.py [repo_root]
       python3 scripts/scan/provider_ua_parity_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import os
import re
import sys

FACTORY_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/provider/ProviderFactory.kt"
)
GEMINI_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/provider/gemini/GeminiProvider.kt"
)
GEMINI_MODELS_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/provider/gemini/GeminiModelsApi.kt"
)
OPENROUTER_MODELS_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/provider/openrouter/OpenRouterModelsApi.kt"
)
ADAPTERS_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/provider/ModelListProviderAdapters.kt"
)

CTOR_RE = re.compile(r"\b(AnthropicProvider|GeminiProvider|OpenAIProvider)\s*\(")
UA_ARG = "customUserAgent ="
NULL_OVERRIDE = ".applyUserAgentOverride(null)"
OVERRIDE_PASSED = ".applyUserAgentOverride(customUserAgent)"
CTOR_PARAM = "private val customUserAgent: String? = null"
VERDICT_CALL_ANCHOR = "shouldFlagEofTruncated("
TOOL_ACCUM = "toolChars += toolCallActivityChars("


def strip_comments(src):
    """Remove /* */ blocks and //-to-EOL comments (quote-aware for // URLs)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for line in src.split("\n"):
        cut = None
        in_str = False
        triple = False
        i = 0
        while i < len(line):
            ch = line[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
            i += 1
        out.append(line if cut is None else line[:cut])
        del triple
    return "\n".join(out)


def blank_strings(src):
    """Replace double-quoted spans with '' so paren balancing cannot be skewed
    by string contents (URLs, JSON samples)."""
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', src)


def balanced_slice(code, open_idx):
    """Given the index of the opening paren, return the inner text of the
    balanced (...) span, or None if unbalanced."""
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return code[open_idx + 1:i]
    return None


def verdict_body(code):
    """Return the body (block or expression) of the shouldFlagEofTruncated
    declaration, or None if the declaration is not found."""
    m = re.search(r"fun\s+shouldFlagEofTruncated\s*\(", code)
    if not m:
        return None
    params = balanced_slice(code, m.end() - 1)
    if params is None:
        return None
    j = m.end() + len(params) + 1  # just past the closing paren
    while j < len(code) and code[j] in " \t\r\n":
        j += 1
    # skip an optional return-type annotation (": Boolean" may itself contain
    # parens) until the body starts: '{' (block) or '=' (expression)
    depth = 0
    k = j
    while k < len(code):
        c = code[k]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and (c == "{" or c == "="):
            break
        k += 1
    if k < len(code) and code[k] == "{":
        bdepth = 0
        for i in range(k, len(code)):
            if code[i] == "{":
                bdepth += 1
            elif code[i] == "}":
                bdepth -= 1
                if bdepth == 0:
                    return code[k:i + 1]
        return None
    if k < len(code) and code[k] == "=":
        # expression body: gather lines until a blank line or a line that
        # starts at column 0 (next top-level declaration)
        lines = code[k:].split("\n")
        acc = [lines[0]]
        for ln in lines[1:]:
            if ln.strip() == "" or not ln[:1].isspace():
                break
            acc.append(ln)
        return "\n".join(acc)
    return None


def constructor_slices(code):
    """Yield (lineno, type, text) for every balanced provider-constructor
    statement in comment/string-stripped code."""
    slices = []
    for m in CTOR_RE.finditer(code):
        start = m.end() - 1  # position of the opening paren
        depth = 0
        end = None
        for i in range(start, len(code)):
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        lineno = code.count("\n", 0, m.start()) + 1
        slices.append((lineno, m.group(1), code[m.start():end + 1]))
    return slices


def check_factory(src):
    v = []
    code = blank_strings(strip_comments(src))
    slices = constructor_slices(code)
    if not slices:
        v.append("ProviderFactory.kt: no provider constructor statements found "
                 "(gate anchors moved — deliberate edit required)")
        return v
    for lineno, ctype, text in slices:
        if UA_ARG not in text:
            v.append(
                "ProviderFactory.kt:%d: %s(...) construction drops "
                "'%s instance.customUserAgent' (ISSUE-04a: the override is "
                "settable on every apiKey provider, null is harmless)"
                % (lineno, ctype, UA_ARG)
            )
    return v


def check_gemini(src):
    v = []
    code = strip_comments(src)

    if CTOR_PARAM not in code:
        v.append(
            "GeminiProvider.kt: constructor no longer declares '%s' "
            "(ISSUE-04b: without the parameter the factory cannot forward the "
            "override)" % CTOR_PARAM
        )
    if NULL_OVERRIDE in code:
        v.append(
            "GeminiProvider.kt:%d: hard-coded '%s' is back "
            "(ISSUE-04b: both builders must pass the constructor value)"
            % (code.split("\n").index(
                next(l for l in code.split("\n") if NULL_OVERRIDE in l)) + 1,
               NULL_OVERRIDE)
        )
    if code.count(OVERRIDE_PASSED) < 2:
        v.append(
            "GeminiProvider.kt: only %d x '%s' (both the non-streaming and the "
            "streaming request builder must pass the override)"
            % (code.count(OVERRIDE_PASSED), OVERRIDE_PASSED)
        )

    # ISSUE-04c: the EOF verdict must be the pure function fed BOTH counters.
    if VERDICT_CALL_ANCHOR not in code:
        v.append(
            "GeminiProvider.kt: SSE EOF verdict no longer routed through '%s' "
            "(ISSUE-04c: the inline condition only saw text/thinking chars)"
            % VERDICT_CALL_ANCHOR
        )
    else:
        call_sites = []
        for m in re.finditer(r"shouldFlagEofTruncated\s*\(", code):
            # skip the definition itself — its parameter list is not a call site
            if re.search(r"fun\s+$", code[max(0, m.start() - 8):m.start()]):
                continue
            call_sites.append(m)
        if not call_sites:
            v.append(
                "GeminiProvider.kt: shouldFlagEofTruncated is defined but never "
                "called (ISSUE-04c: the SSE EOF path no longer applies the "
                "tool-aware verdict)"
            )
        for m in call_sites:
            args = balanced_slice(code, m.end() - 1)
            if args is None:
                continue
            if "toolChars" not in args:
                v.append(
                    "GeminiProvider.kt:%d: shouldFlagEofTruncated(...) call "
                    "does not feed the tool counter (ISSUE-04c: tool-only EOF "
                    "falls back to a clean end_turn)"
                    % (code.count("\n", 0, m.start()) + 1)
                )
    # the verdict body itself must still weigh both counters
    body = verdict_body(code)
    if body is None:
        v.append(
            "GeminiProvider.kt: 'fun shouldFlagEofTruncated(' declaration not "
            "found (the pure verdict was inlined or renamed — deliberate gate "
            "edit required)"
        )
    elif "contentChars > 0" not in body or "toolChars > 0" not in body:
        v.append(
            "GeminiProvider.kt: shouldFlagEofTruncated body no longer "
            "weighs both 'contentChars > 0' and 'toolChars > 0' "
            "(ISSUE-04c: one counter was dropped from the verdict)"
        )
    if TOOL_ACCUM not in code:
        v.append(
            "GeminiProvider.kt: tool-payload accumulation '%s' missing "
            "(ISSUE-04c: without it the verdict's tool counter stays 0 and the "
            "condition is dead)" % TOOL_ACCUM
        )
    return v


def check_gemini_models(src):
    """[da1020 ISSUE-05] GeminiModelsApi: the models-list fetcher accepts and
    applies the per-provider customUserAgent (no hard-coded null override)."""
    v = []
    code = strip_comments(src)
    if "customUserAgent: String? = null" not in code:
        v.append(
            "GeminiModelsApi.kt: fetchModels no longer declares "
            "'customUserAgent: String? = null' (da1020 ISSUE-05: without the "
            "parameter the adapter cannot forward the override)"
        )
    if NULL_OVERRIDE in code:
        v.append(
            "GeminiModelsApi.kt:%d: hard-coded '%s' is back "
            "(da1020 ISSUE-05: the fetcher must apply the customUserAgent "
            "parameter, falling back to the branded default when unset)"
            % (code.split("\n").index(
                next(l for l in code.split("\n") if NULL_OVERRIDE in l)) + 1,
               NULL_OVERRIDE)
        )
    if OVERRIDE_PASSED not in code:
        v.append(
            "GeminiModelsApi.kt: '%s' missing (da1020 ISSUE-05: the request "
            "builder must pass the customUserAgent parameter)" % OVERRIDE_PASSED
        )
    return v


def check_openrouter_models(src):
    """[da1020 ISSUE-05] OpenRouterModelsApi: same UA parity contract as
    GeminiModelsApi."""
    v = []
    code = strip_comments(src)
    if "customUserAgent: String? = null" not in code:
        v.append(
            "OpenRouterModelsApi.kt: fetchModels no longer declares "
            "'customUserAgent: String? = null' (da1020 ISSUE-05: without the "
            "parameter the adapter cannot forward the override)"
        )
    if NULL_OVERRIDE in code:
        v.append(
            "OpenRouterModelsApi.kt:%d: hard-coded '%s' is back "
            "(da1020 ISSUE-05: the fetcher must apply the customUserAgent "
            "parameter)" % (code.split("\n").index(
                next(l for l in code.split("\n") if NULL_OVERRIDE in l)) + 1,
               NULL_OVERRIDE)
        )
    if OVERRIDE_PASSED not in code:
        v.append(
            "OpenRouterModelsApi.kt: '%s' missing (da1020 ISSUE-05: the "
            "request builder must pass the customUserAgent parameter)"
            % OVERRIDE_PASSED
        )
    return v


def check_adapters(src):
    """[da1020 ISSUE-05] Both adapter families forward instance.customUserAgent
    into their ModelsApi call."""
    v = []
    code = strip_comments(src)
    for api in ("GeminiModelsApi.fetchModels", "OpenRouterModelsApi.fetchModels"):
        i = code.find(api + "(")
        if i < 0:
            v.append(
                "ModelListProviderAdapters.kt: %s call not found "
                "(gate anchors moved — deliberate edit required)" % api
            )
            continue
        depth = 0
        end = None
        for k in range(i + len(api), len(code)):
            if code[k] == "(":
                depth += 1
            elif code[k] == ")":
                depth -= 1
                if depth == 0:
                    end = k
                    break
        if end is None:
            v.append("ModelListProviderAdapters.kt: %s unbalanced" % api)
            continue
        args = code[i + len(api):end]
        if UA_ARG + " instance.customUserAgent" not in args:
            v.append(
                "ModelListProviderAdapters.kt:%d: %s call drops '%s' "
                "(da1020 ISSUE-05: the adapter must forward the per-provider "
                "override)"
                % (code.count("\n", 0, i) + 1, api, UA_ARG)
            )
    return v


def read(root, rel):
    path = os.path.join(root, rel)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def run(root):
    if not os.path.isdir(root):
        print("provider_ua_parity_check: not a directory: %s" % root)
        return 2
    try:
        factory = read(root, FACTORY_REL)
        gemini = read(root, GEMINI_REL)
        gemini_models = read(root, GEMINI_MODELS_REL)
        openrouter_models = read(root, OPENROUTER_MODELS_REL)
        adapters = read(root, ADAPTERS_REL)
    except OSError as exc:
        print("provider_ua_parity_check: cannot read sources: %s" % exc)
        return 2
    violations = (check_factory(factory) + check_gemini(gemini)
                  + check_gemini_models(gemini_models)
                  + check_openrouter_models(openrouter_models)
                  + check_adapters(adapters))
    if violations:
        print("FAIL provider_ua_parity_check")
        for v in violations:
            print("  " + v)
        return 1
    print("PASS provider_ua_parity_check (UA forwarded on every provider arm, "
          "Gemini EOF verdict weighs tool payload)")
    return 0


# --------------------------------------------------------------------------
# selftest: every criterion gets its own violating fixture, plus compliant ones
# --------------------------------------------------------------------------

GOOD_FACTORY = """
object ProviderFactory {
    fun create(instance: ProviderInstance, apiKey: String, model: LLMModel): LLMProvider {
        val effectiveKey = apiKey
        return (when (instance.providerType) {
            ProviderType.anthropic -> {
                if (basePath != null) AnthropicProvider(effectiveKey, model, basePath, customUserAgent = instance.customUserAgent)
                else AnthropicProvider(effectiveKey, model, customUserAgent = instance.customUserAgent)
            }
            ProviderType.gemini -> {
                if (basePath != null) GeminiProvider(effectiveKey, model, basePath, customUserAgent = instance.customUserAgent)
                else GeminiProvider(effectiveKey, model, customUserAgent = instance.customUserAgent)
            }
            ProviderType.openAI -> {
                OpenAIProvider(
                    apiKey = effectiveKey,
                    model = model,
                    basePath = "https://api.openai.com/v1",
                    customUserAgent = instance.customUserAgent,
                )
            }
            ProviderType.openRouter -> {
                OpenAIProvider(
                    apiKey = effectiveKey,
                    model = model,
                    basePath = "https://openrouter.ai/api/v1",
                    customUserAgent = instance.customUserAgent,
                )
            }
            else -> OpenAIProvider(
                apiKey = effectiveKey,
                model = model,
                basePath = "https://api.x.ai/v1",
                customUserAgent = instance.customUserAgent,
            )
        })
    }
}
"""

BAD_FACTORY_DROP = GOOD_FACTORY.replace(
    """                OpenAIProvider(
                    apiKey = effectiveKey,
                    model = model,
                    basePath = "https://openrouter.ai/api/v1",
                    customUserAgent = instance.customUserAgent,
                )""",
    """                OpenAIProvider(
                    apiKey = effectiveKey,
                    model = model,
                    basePath = "https://openrouter.ai/api/v1",
                )""",
)

GOOD_GEMINI = """
class GeminiProvider(
    private val apiKey: String,
    override var model: LLMModel = LLMModel.gemini25Flash,
    private val basePath: String = GeminiBasePath.DEFAULT_BASE,
    private val customUserAgent: String? = null,
) : LLMProvider {
    fun send(): LLMResponse {
        val request = Request.Builder()
            .url(url)
            .applyUserAgentOverride(customUserAgent)
            .build()
        return LLMResponse("", "end_turn", null, emptyList())
    }

    fun stream(): Flow<LLMStreamChunk> = callbackFlow {
        val request = Request.Builder()
            .url(url)
            .applyUserAgentOverride(customUserAgent)
            .build()
        var contentChars = 0
        var toolChars = 0
        toolChars += toolCallActivityChars(fcName, fcArgs.toString())
        contentChars += media.data.size
        if (shouldFlagEofTruncated(lastFinishReason, contentChars, toolChars)) {
            send(LLMStreamChunk.Finished(null, truncated = true))
        } else {
            send(LLMStreamChunk.Finished(lastFinishReason ?: "end_turn"))
        }
    }
}

internal fun shouldFlagEofTruncated(
    lastFinishReason: String?,
    contentChars: Int,
    toolChars: Int,
): Boolean = lastFinishReason == null && (contentChars > 0 || toolChars > 0)
"""

BAD_GEMINI_NULL = GOOD_GEMINI.replace(
    ".applyUserAgentOverride(customUserAgent)\n            .build()\n        return LLMResponse",
    ".applyUserAgentOverride(null)\n            .build()\n        return LLMResponse",
)

BAD_GEMINI_NO_PARAM = GOOD_GEMINI.replace(
    "    private val customUserAgent: String? = null,\n", ""
).replace(".applyUserAgentOverride(customUserAgent)", ".applyUserAgentOverride(null)")

BAD_GEMINI_NO_TOOL_VERDICT = GOOD_GEMINI.replace(
    "): Boolean = lastFinishReason == null && (contentChars > 0 || toolChars > 0)",
    "): Boolean = lastFinishReason == null && contentChars > 0",
)

BAD_GEMINI_NO_ACCUM = GOOD_GEMINI.replace(
    "        toolChars += toolCallActivityChars(fcName, fcArgs.toString())\n", ""
)

BAD_GEMINI_NO_CALLSITE = GOOD_GEMINI.replace(
    "if (shouldFlagEofTruncated(lastFinishReason, contentChars, toolChars)) {",
    "if (lastFinishReason == null && contentChars > 0) {",
)

BAD_GEMINI_NO_VERDICT_FN = BAD_GEMINI_NO_CALLSITE.replace(
    """
internal fun shouldFlagEofTruncated(
    lastFinishReason: String?,
    contentChars: Int,
    toolChars: Int,
): Boolean = lastFinishReason == null && (contentChars > 0 || toolChars > 0)
""",
    "",
)


GOOD_GEMINI_MODELS = """
object GeminiModelsApi {
    suspend fun fetchModels(
        apiKey: String,
        context: Context? = null,
        forceRefresh: Boolean = false,
        customUserAgent: String? = null,
    ): List<LLMModel> = withContext(Dispatchers.IO) {
        val builder = Request.Builder()
        builder.url("https://x/models?key=" + apiKey)
        builder.applyUserAgentOverride(customUserAgent)
        emptyList()
    }
}
"""

BAD_GEMINI_MODELS_NULL = GOOD_GEMINI_MODELS.replace(
    "builder.applyUserAgentOverride(customUserAgent)",
    "builder.applyUserAgentOverride(null)",
)

BAD_GEMINI_MODELS_NO_PARAM = GOOD_GEMINI_MODELS.replace(
    "        customUserAgent: String? = null,\n", ""
).replace(
    "builder.applyUserAgentOverride(customUserAgent)",
    "builder.applyUserAgentOverride(null)",
)

GOOD_OPENROUTER_MODELS = """
object OpenRouterModelsApi {
    suspend fun fetchModels(
        apiKey: String,
        context: Context? = null,
        forceRefresh: Boolean = false,
        customUserAgent: String? = null,
    ): List<LLMModel> = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url("https://openrouter.ai/api/v1/models")
            .applyUserAgentOverride(customUserAgent)
            .build()
        emptyList()
    }
}
"""

BAD_OPENROUTER_MODELS_NULL = GOOD_OPENROUTER_MODELS.replace(
    ".applyUserAgentOverride(customUserAgent)",
    ".applyUserAgentOverride(null)",
)

GOOD_ADAPTERS = """
object Adapters {
    fun gemini(instance: ProviderInstance, apiKey: String, context: Context?): List<LLMModel> {
        return GeminiModelsApi.fetchModels(
            apiKey,
            instance.effectiveBaseURL,
            context = context,
            customUserAgent = instance.customUserAgent,
        )
    }

    fun openrouter(instance: ProviderInstance, apiKey: String, context: Context?): List<LLMModel> {
        return OpenRouterModelsApi.fetchModels(
            apiKey,
            context = context,
            customUserAgent = instance.customUserAgent,
        )
    }
}
"""

BAD_ADAPTERS_DROP = GOOD_ADAPTERS.replace(
    """        return OpenRouterModelsApi.fetchModels(
            apiKey,
            context = context,
            customUserAgent = instance.customUserAgent,
        )""",
    """        return OpenRouterModelsApi.fetchModels(
            apiKey,
            context = context,
        )""",
)

def selftest():
    failures = []

    def expect_clean(name, v):
        if v:
            failures.append("%s must be clean, got %s" % (name, v))

    def expect_trip(name, v, needle):
        if not any(needle in item for item in v):
            failures.append("%s did not trip '%s' (got %s)" % (name, needle, v))

    good_factory = check_factory(GOOD_FACTORY)
    good_gemini = check_gemini(GOOD_GEMINI)
    expect_clean("compliant factory fixture", good_factory)
    expect_clean("compliant gemini fixture", good_gemini)

    good_gm = check_gemini_models(GOOD_GEMINI_MODELS)
    good_orm = check_openrouter_models(GOOD_OPENROUTER_MODELS)
    good_ad = check_adapters(GOOD_ADAPTERS)
    expect_clean("compliant gemini-models fixture", good_gm)
    expect_clean("compliant openrouter-models fixture", good_orm)
    expect_clean("compliant adapters fixture", good_ad)

    bad_drop = check_factory(BAD_FACTORY_DROP)
    expect_trip("dropped-openRouter fixture (ISSUE-04a)", bad_drop, "drops")
    expect_trip("dropped-openRouter fixture (ISSUE-04a)", bad_drop, "OpenAIProvider")

    bad_null = check_gemini(BAD_GEMINI_NULL)
    expect_trip("hard-coded-null fixture (ISSUE-04b)", bad_null, "applyUserAgentOverride(null)")

    bad_noparam = check_gemini(BAD_GEMINI_NO_PARAM)
    expect_trip("no-constructor-param fixture (ISSUE-04b)", bad_noparam, "constructor no longer declares")
    expect_trip("no-constructor-param fixture (ISSUE-04b)", bad_noparam, "applyUserAgentOverride(null)")

    bad_verdict = check_gemini(BAD_GEMINI_NO_TOOL_VERDICT)
    expect_trip("verdict-drops-toolchar fixture (ISSUE-04c)", bad_verdict, "no longer weighs both")

    bad_accum = check_gemini(BAD_GEMINI_NO_ACCUM)
    expect_trip("no-tool-accumulation fixture (ISSUE-04c)", bad_accum, "tool-payload accumulation")

    bad_callsite = check_gemini(BAD_GEMINI_NO_CALLSITE)
    expect_trip("inline-verdict fixture (ISSUE-04c)", bad_callsite, "never called")

    bad_nofn = check_gemini(BAD_GEMINI_NO_VERDICT_FN)
    expect_trip("verdict-fn-deleted fixture (ISSUE-04c)", bad_nofn, "no longer routed")
    expect_trip("verdict-fn-deleted fixture (ISSUE-04c)", bad_nofn, "declaration not found")

    bad_gm_null = check_gemini_models(BAD_GEMINI_MODELS_NULL)
    expect_trip("gemini-models hard-coded-null fixture", bad_gm_null, "applyUserAgentOverride(null)")

    bad_gm_noparam = check_gemini_models(BAD_GEMINI_MODELS_NO_PARAM)
    expect_trip("gemini-models no-param fixture", bad_gm_noparam, "no longer declares")

    bad_orm_null = check_openrouter_models(BAD_OPENROUTER_MODELS_NULL)
    expect_trip("openrouter-models hard-coded-null fixture", bad_orm_null, "applyUserAgentOverride(null)")

    bad_ad = check_adapters(BAD_ADAPTERS_DROP)
    expect_trip("adapters-drop fixture", bad_ad, "drops")
    expect_trip("adapters-drop fixture", bad_ad, "OpenRouterModelsApi")

    print("selftest compliant factory        -> %d violation(s)" % len(good_factory))
    print("selftest compliant gemini         -> %d violation(s)" % len(good_gemini))
    print("selftest dropped-openRouter       -> %d violation(s)" % len(bad_drop))
    print("selftest hard-coded-null          -> %d violation(s)" % len(bad_null))
    print("selftest no-constructor-param     -> %d violation(s)" % len(bad_noparam))
    print("selftest verdict-drops-toolchar   -> %d violation(s)" % len(bad_verdict))
    print("selftest no-tool-accumulation     -> %d violation(s)" % len(bad_accum))
    print("selftest inline-verdict           -> %d violation(s)" % len(bad_callsite))
    print("selftest verdict-fn-deleted       -> %d violation(s)" % len(bad_nofn))
    print("selftest gemini-models hard-null  -> %d violation(s)" % len(bad_gm_null))
    print("selftest gemini-models no-param   -> %d violation(s)" % len(bad_gm_noparam))
    print("selftest openrouter-models null   -> %d violation(s)" % len(bad_orm_null))
    print("selftest adapters-drop            -> %d violation(s)" % len(bad_ad))

    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    print("PROVIDER_UA_PARITY_SELFTEST=PASS")
    return 0


def main():
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        return selftest()
    root = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(root)


if __name__ == "__main__":
    sys.exit(main())

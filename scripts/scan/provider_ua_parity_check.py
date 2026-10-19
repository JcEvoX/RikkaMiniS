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
VOICE_PROVIDER_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/provider/voice/VoiceProvider.kt"
)
VOICE_VENDORS_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/provider/voice/VoiceProviderVendors.kt"
)
VOICE_FACTORY_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/provider/voice/VoiceProviderFactory.kt"
)
# -- [da1030 ISSUE-06] OpenAI outbound single-source contract -----------------
OPENAI_PROVIDER_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/provider/openai/OpenAIProvider.kt"
)
OPENAI_MODELS_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/provider/openai/OpenAIModelsApi.kt"
)
OPENAI_AUTH_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/provider/openai/OpenAIAuthHeader.kt"
)
MAIN_SRC_REL = "src/android/app/src/main/java"
API_KEY_LITERAL = 'header("api-key"'

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


def read_optional(root, rel):
    """Drift-detector read: None when the file is absent (mini tree), so a
    fixture that only carries the original outlet files does not turn the new
    voice criteria into a false red. A file that EXISTS is judged fail-closed:
    a missing anchor there is a violation, not a skip. (A deleted voice file
    altogether is a loud compile error — every voice call site breaks — the
    gate's job is the wiring contract, not the file's existence.)"""
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def decl_body(code, decl):
    """Return the balanced {...} of the body that follows `decl` (the first
    opening brace after the declaration — a withContext lambda for
    `executeRequest`, the `.also {}` lambda for an expression-bodied `make`),
    or None when the declaration or a body is not found (fail-closed)."""
    i = code.find(decl)
    if i < 0:
        return None
    j = code.find("{", i)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(code)):
        if code[k] == "{":
            depth += 1
        elif code[k] == "}":
            depth -= 1
            if depth == 0:
                return code[j:k + 1]
    return None


INJECTION_RE = re.compile(
    r"(?<!val )(?<!var )[\w?.]*customUserAgent\s*=\s*instance\.customUserAgent"
)


def check_voice(provider_src, vendors_src, factory_src):
    """[da1029 ISSUE-05] The voice family was the one outbound subsystem with
    zero customUserAgent wiring. Three criteria (fail-closed, comment-stripped):

      V1  VoiceProvider.executeRequest — the single HTTP door every ASR/TTS
          request in the family goes through — applies the override;
      V2  the Xunfei TTS WebSocket request (the one voice request that bypasses
          executeRequest, httpClient.newWebSocket) carries it too;
      V3  VoiceProviderFactory.make wires the instance value in at its entry
          (one post-construction injection covering every build() arm, incl.
          the two early returns and the null arms — per-branch forwarding is
          the deep-audit-1019 ISSUE-04 shape that silently drops new arms).

    provider/ModelsDevApi.kt is deliberately NOT part of this contract: it has
    no instance-level UA by design (TRIAGE §5) and must not be counted as a gap.
    """
    v = []
    code_p = blank_strings(strip_comments(provider_src))
    code_v = blank_strings(strip_comments(vendors_src))
    code_f = blank_strings(strip_comments(factory_src))

    # V1
    if "customUserAgent: String? = null" not in code_p:
        v.append(
            "VoiceProvider.kt: no 'customUserAgent: String? = null' property "
            "(da1029 ISSUE-05: without it the factory has nowhere to inject the "
            "instance override)"
        )
    body = decl_body(code_p, "suspend fun executeRequest(")
    if body is None:
        v.append(
            "VoiceProvider.kt: 'suspend fun executeRequest(' not found (gate "
            "anchors moved — deliberate edit required)"
        )
    elif ".applyUserAgentOverride(customUserAgent)" not in body:
        v.append(
            "VoiceProvider.kt: executeRequest does not apply "
            "'.applyUserAgentOverride(customUserAgent)' (da1029 ISSUE-05: it is "
            "the single HTTP door for every ASR/TTS request in the family)"
        )

    # V2
    url_at = code_v.find(".url(signedUrl)")
    if url_at < 0:
        v.append(
            "VoiceProviderVendors.kt: Xunfei TTS WebSocket request "
            "('.url(signedUrl)') not found (gate anchors moved — deliberate "
            "edit required)"
        )
    else:
        build_at = code_v.find(".build()", url_at)
        if build_at < 0:
            v.append("VoiceProviderVendors.kt: Xunfei WebSocket request statement unbalanced")
        elif ".applyUserAgentOverride(" not in code_v[url_at:build_at]:
            v.append(
                "VoiceProviderVendors.kt: the Xunfei TTS WebSocket request drops "
                "'.applyUserAgentOverride(' (da1029 ISSUE-05: it is the one voice "
                "request that bypasses executeRequest)"
            )

    # V3
    body = decl_body(code_f, "fun make(")
    if body is None:
        v.append(
            "VoiceProviderFactory.kt: 'fun make(' body not found (gate anchors "
            "moved — deliberate edit required)"
        )
    elif not INJECTION_RE.search(body):
        v.append(
            "VoiceProviderFactory.kt: make() does not inject "
            "'instance.customUserAgent' at its entry (da1029 ISSUE-05: one "
            "post-construction injection must cover every build() arm; per-arm "
            "forwarding is the deep-audit-1019 ISSUE-04 shape that drops new arms)"
        )
    return v


def check_openai_outbound(provider_src, models_src, auth_src, adapters_src, main_sources):
    """[da1030 ISSUE-06] The OpenAI provider's outbound requests must all draw
    from the same two chokepoints: the UA override and the key-auth header.

      D1  the `/raw-passthrough` builder (the ModelUse direct route) applies
          `.applyUserAgentOverride(...)` — and does so BEFORE `builder.build()`,
          the same last-wins placement as the chat/responses builders. It was
          the one OpenAI builder that never applied it.

      D2  `header("api-key"` exists in exactly ONE place — the package-level
          `applyOpenAIKeyAuth` in OpenAIAuthHeader.kt — which both
          `OpenAIProvider.applyKeyAuth` and `OpenAIModelsApi` delegate to. The
          models fetch used to hard-code `Authorization: Bearer`, so every
          Azure instance (which authenticates with `api-key`) 401'd, and the
          7-day cache row was keyed on URL+key so the two auth schemes shared
          one row. Both halves are asserted: the parameter is threaded, and the
          cache key carries the scheme.

      D3  every `OpenAIModelsApi.fetchModels(` call in the adapter file passes
          `azureMode =`, with a value that matches that arm's CHAT path — either
          `instance.azureMode` (the OpenAI arm, whose ProviderFactory branch
          forwards isAzure) or the explicit `false` (Kimi, whose chat branch
          never forwards it). Per-call-site forwarding is checked exhaustively
          (not just the first occurrence) because dropping the argument on the
          second arm is exactly the deep-audit-1019 ISSUE-04 shape: silent,
          arm-local, and invisible in a single-site grep. And at least one site
          must forward from the instance, else Azure silently loses its models
          list while still passing the per-site check.
    """
    v = []
    code_p = blank_strings(strip_comments(provider_src))
    code_m = blank_strings(strip_comments(models_src))
    code_a = strip_comments(adapters_src)
    code_auth = strip_comments(auth_src)

    # -- D1 ------------------------------------------------------------------
    body = decl_body(code_p, "suspend fun rawPassthroughRequest(")
    if body is None or "builder.build()" not in body:
        v.append(
            "OpenAIProvider.kt: 'suspend fun rawPassthroughRequest(' body not "
            "found (gate anchors moved — deliberate edit required)"
        )
    else:
        at = body.find(".applyUserAgentOverride(")
        build_at = body.find("builder.build()")
        if at < 0:
            v.append(
                "OpenAIProvider.kt: rawPassthroughRequest never applies "
                "'.applyUserAgentOverride(' (da1030 ISSUE-06 裂缝A: the "
                "/raw-passthrough route is the one OpenAI builder that drops "
                "the per-provider UA, so ModelUse direct calls go out with the "
                "default UA)"
            )
        elif at > build_at:
            v.append(
                "OpenAIProvider.kt: rawPassthroughRequest applies the UA "
                "override AFTER 'builder.build()' (da1030 ISSUE-06 裂缝A: the "
                "override must sit in the builder chain, after extraHeaders/user "
                "headers and before build(), or it never reaches the request)"
            )

    # -- D2a: single source for the api-key header ---------------------------
    if "fun Request.Builder.applyOpenAIKeyAuth(" not in code_auth:
        v.append(
            "OpenAIAuthHeader.kt: 'fun Request.Builder.applyOpenAIKeyAuth(' not "
            "found (da1030 ISSUE-06 裂缝B: this is the single source the chat "
            "builders and the models fetcher both delegate to)"
        )
    if API_KEY_LITERAL not in code_auth:
        v.append(
            "OpenAIAuthHeader.kt: no '%s' literal (da1030 ISSUE-06 裂缝B: the "
            "Azure branch must live here, not in a caller)" % API_KEY_LITERAL
        )
    for rel, text in main_sources:
        if rel == OPENAI_AUTH_REL:
            continue
        if API_KEY_LITERAL in strip_comments(text):
            v.append(
                "%s: hard-coded '%s' outside provider/openai/OpenAIAuthHeader.kt "
                "(da1030 ISSUE-06 裂缝B: the Azure key header has exactly one "
                "source; a second copy is how the models fetcher ended up "
                "Bearer-only)" % (rel, API_KEY_LITERAL)
            )

    # -- D2b: the models fetcher threads + applies azureMode -----------------
    if "azureMode: Boolean = false" not in code_m:
        v.append(
            "OpenAIModelsApi.kt: fetchModels no longer declares "
            "'azureMode: Boolean = false' (da1030 ISSUE-06 裂缝B: without the "
            "parameter the adapter cannot forward ProviderInstance.azureMode)"
        )
    for fn, why in (
        ("buildModelsRequest", "the request is built with the hard-coded Bearer "
                               "path again"),
        ("modelsCacheKey", "the cache key no longer separates the two auth "
                           "schemes, so an Azure and a Bearer instance share a row"),
    ):
        sites = [m for m in re.finditer(re.escape(fn + "("), code_m)
                 if not re.search(r"fun\s+$", code_m[max(0, m.start() - 8):m.start()])]
        if not sites:
            v.append(
                "OpenAIModelsApi.kt: no %s(...) call in fetchModels (da1030 "
                "ISSUE-06 裂缝B: %s)" % (fn, why)
            )
            continue
        for m in sites:
            args = balanced_slice(code_m, m.end() - 1)
            if args is None:
                v.append("OpenAIModelsApi.kt:%d: %s(...) unbalanced"
                         % (code_m.count("\n", 0, m.start()) + 1, fn))
            elif "azureMode" not in args:
                v.append(
                    "OpenAIModelsApi.kt:%d: %s(...) call does not pass "
                    "azureMode (da1030 ISSUE-06 裂缝B: %s)"
                    % (code_m.count("\n", 0, m.start()) + 1, fn, why)
                )

    # -- D3: every adapter call site forwards azureMode ----------------------
    api = "OpenAIModelsApi.fetchModels"
    sites = [m for m in re.finditer(re.escape(api + "("), code_a)
             if not re.search(r"fun\s+$", code_a[max(0, m.start() - 8):m.start()])]
    if not sites:
        v.append(
            "ModelListProviderAdapters.kt: %s call not found (gate anchors "
            "moved — deliberate edit required)" % api
        )
    forwarded_from_instance = 0
    for m in sites:
        args = code_a[m.end() - 1:]
        depth = 0
        end = None
        for k in range(len(args)):
            if args[k] == "(":
                depth += 1
            elif args[k] == ")":
                depth -= 1
                if depth == 0:
                    end = k
                    break
        if end is None:
            v.append("ModelListProviderAdapters.kt: %s unbalanced" % api)
            continue
        args = args[:end]
        line = code_a.count("\n", 0, m.start()) + 1
        if UA_ARG not in args:
            v.append(
                "ModelListProviderAdapters.kt:%d: %s call drops '%s' "
                "(da1030 ISSUE-06: the models fetch still gets no UA)"
                % (line, api, UA_ARG)
            )
        if "azureMode" not in args:
            v.append(
                "ModelListProviderAdapters.kt:%d: %s call drops 'azureMode' "
                "(da1030 ISSUE-06 裂缝B: this arm's Azure instances still fetch "
                "the models list with Bearer auth)" % (line, api)
            )
            continue
        azure_value = re.search(r"azureMode\s*=\s*([A-Za-z0-9_.]+)", args)
        if azure_value is None or azure_value.group(1) not in (
            "instance.azureMode", "false"
        ):
            v.append(
                "ModelListProviderAdapters.kt:%d: %s passes an azureMode value "
                "that is neither `instance.azureMode` nor the explicit `false` "
                "(da1030 ISSUE-06: the value must mirror that arm's chat path — "
                "an invented value silently changes which header goes out)"
                % (line, api)
            )
        elif azure_value.group(1) == "instance.azureMode":
            forwarded_from_instance += 1
    if sites and forwarded_from_instance == 0:
        v.append(
            "ModelListProviderAdapters.kt: no %s call forwards "
            "`azureMode = instance.azureMode` (da1030 ISSUE-06 裂缝B: Azure "
            "instances silently lose their models list while every call site "
            "still looks compliant)" % api
        )
    return v


def collect_main_sources(root):
    """Every .kt under the main source root as (repo-relative path, source).
    Feeds the D2a single-source sweep for the api-key header literal."""
    out = []
    base = os.path.join(root, MAIN_SRC_REL)
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in sorted(filenames):
            if not name.endswith(".kt"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    out.append((rel, fh.read()))
            except OSError:
                continue
    return out

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
    # [da1029 ISSUE-05] voice family — drift-detector read, judged fail-closed
    # when present (see read_optional).
    voice_provider = read_optional(root, VOICE_PROVIDER_REL)
    voice_vendors = read_optional(root, VOICE_VENDORS_REL)
    voice_factory = read_optional(root, VOICE_FACTORY_REL)
    if voice_provider is not None and voice_vendors is not None \
            and voice_factory is not None:
        violations += check_voice(voice_provider, voice_vendors, voice_factory)
    else:
        print("note: voice family sources not present in this tree — "
              "voice-UA criteria skipped (drift-detector semantics)")
    # [da1030 ISSUE-06] OpenAI outbound single-source contract (UA on the
    # passthrough builder + one api-key auth source + azureMode threaded end
    # to end). Fail-closed: a missing file is a violation, not a skip.
    openai_provider = read_optional(root, OPENAI_PROVIDER_REL)
    openai_models = read_optional(root, OPENAI_MODELS_REL)
    openai_auth = read_optional(root, OPENAI_AUTH_REL)
    missing = [rel for rel, src in (
        (OPENAI_PROVIDER_REL, openai_provider),
        (OPENAI_MODELS_REL, openai_models),
        (OPENAI_AUTH_REL, openai_auth),
    ) if src is None]
    if missing:
        violations.append(
            "provider/openai outbound sources missing: %s (da1030 ISSUE-06: the "
            "UA + api-key single-source contract cannot be judged)" % ", ".join(missing)
        )
    else:
        violations += check_openai_outbound(
            openai_provider, openai_models, openai_auth, adapters,
            collect_main_sources(root),
        )
    if violations:
        print("FAIL provider_ua_parity_check")
        for v in violations:
            print("  " + v)
        return 1
    print("PASS provider_ua_parity_check (UA forwarded on every provider arm, "
          "Gemini EOF verdict weighs tool payload, voice family carries the UA, "
          "OpenAI outbound shares one api-key source + one UA chokepoint)")
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

# -- [da1029 ISSUE-05] voice family fixtures: each new criterion gets its own
#    "exactly broken by it" violating fixture plus a compliant one. -----------

GOOD_VOICE_PROVIDER = """
class VoiceProvider(
    val providerId: String,
    val baseURL: String,
    val apiKey: String? = null,
    var customUserAgent: String? = null,
) {
    suspend fun executeRequest(request: Request): ByteArray = withContext(Dispatchers.IO) {
        val effectiveRequest = request.newBuilder()
            .applyUserAgentOverride(customUserAgent)
            .build()
        val call = httpClient.newCall(effectiveRequest)
        emptyList<Byte>()
    }
}
"""

BAD_VOICE_PROVIDER_DROP = GOOD_VOICE_PROVIDER.replace(
    """        val effectiveRequest = request.newBuilder()
            .applyUserAgentOverride(customUserAgent)
            .build()
        val call = httpClient.newCall(effectiveRequest)""",
    """        val call = httpClient.newCall(request)""",
)

BAD_VOICE_PROVIDER_NO_FN = GOOD_VOICE_PROVIDER.replace(
    "suspend fun executeRequest(", "suspend fun sendBytes("
)

GOOD_VOICE_VENDORS = """
class XunfeiVoiceProvider {
    override suspend fun synthesize(request: VoiceOutputRequest): ByteArray {
        val signedUrl = buildSignedURL(host = "tts-api.xfyun.cn")
        return withTimeoutOrNull(TTS_TIMEOUT_MS) {
            suspendCancellableCoroutine { cont ->
                val wsRequest = Request.Builder()
                    .url(signedUrl)
                    .applyUserAgentOverride(customUserAgent)
                    .build()
                val socket = httpClient.newWebSocket(wsRequest, listener)
            }
        }
    }
}
"""

BAD_VOICE_VENDORS_DROP = GOOD_VOICE_VENDORS.replace(
    """                val wsRequest = Request.Builder()
                    .url(signedUrl)
                    .applyUserAgentOverride(customUserAgent)
                    .build()""",
    """                val wsRequest = Request.Builder().url(signedUrl).build()""",
)

BAD_VOICE_VENDORS_ANCHOR_MOVED = GOOD_VOICE_VENDORS.replace(
    ".url(signedUrl)", ".url(signed)"
)

GOOD_VOICE_FACTORY = """
object VoiceProviderFactory {
    fun make(instance: ProviderInstance, rawApiKey: String?): VoiceProvider? =
        build(instance, rawApiKey).also { it?.customUserAgent = instance.customUserAgent }

    private fun build(instance: ProviderInstance, rawApiKey: String?): VoiceProvider? {
        return when (instance.providerType) {
            ProviderType.openAI -> VoiceProvider(instance.id, "https://x", rawApiKey)
            else -> null
        }
    }
}
"""

BAD_VOICE_FACTORY_DROP = GOOD_VOICE_FACTORY.replace(
    "        build(instance, rawApiKey).also { it?.customUserAgent = instance.customUserAgent }",
    "        build(instance, rawApiKey)",
)

BAD_VOICE_FACTORY_NULL = GOOD_VOICE_FACTORY.replace(
    "it?.customUserAgent = instance.customUserAgent",
    "it?.customUserAgent = null",
)

# -- [da1030 ISSUE-06] OpenAI outbound fixtures: D1 (passthrough UA), D2a
#    (single api-key source), D2b (azureMode threaded in the fetcher) and
#    D3 (every adapter call site forwards azureMode). ------------------------

GOOD_OPENAI_PROVIDER = """
class OpenAIProvider(
    private val apiKey: String,
    private val isAzure: Boolean = false,
    private val customUserAgent: String? = null,
) {
    private fun Request.Builder.applyKeyAuth(token: String): Request.Builder =
        applyOpenAIKeyAuth(isAzure, token)

    suspend fun rawPassthroughRequest(
        endpoint: String?,
        method: String,
        headers: Map<String, String>,
    ): ByteArray = withContext(Dispatchers.IO) {
        val builder = Request.Builder().url(url)
        val token = getToken()
        builder.applyKeyAuth(token)
        builder.header("Content-Type", "application/json")
        for ((k, v) in extraHeaders) builder.header(k, v)
        for ((k, v) in headers) builder.header(k, v)
        builder.applyUserAgentOverride(customUserAgent)

        val response = client.newCall(builder.build()).execute()
        response.body!!.bytes()
    }
}
"""

BAD_OPENAI_PROVIDER_DROP = GOOD_OPENAI_PROVIDER.replace(
    "        builder.applyUserAgentOverride(customUserAgent)\n", ""
)

BAD_OPENAI_PROVIDER_AFTER_BUILD = GOOD_OPENAI_PROVIDER.replace(
    """        builder.applyUserAgentOverride(customUserAgent)

        val response = client.newCall(builder.build()).execute()""",
    """        val response = client.newCall(builder.build()).execute()
        val request = builder.applyUserAgentOverride(customUserAgent)""",
)

BAD_OPENAI_PROVIDER_RENAMED = GOOD_OPENAI_PROVIDER.replace(
    "suspend fun rawPassthroughRequest(", "suspend fun rawPassthrough("
)

GOOD_OPENAI_AUTH = """
package com.rikkaminis.app.provider.openai

internal fun Request.Builder.applyOpenAIKeyAuth(
    azure: Boolean,
    token: String,
): Request.Builder =
    if (azure) header("api-key", token) else header("Authorization", "Bearer $token")
"""

BAD_OPENAI_AUTH_NO_FN = GOOD_OPENAI_AUTH.replace(
    "internal fun Request.Builder.applyOpenAIKeyAuth(", "internal fun Request.Builder.keyAuth("
)

GOOD_OPENAI_MODELS = """
object OpenAIModelsApi {
    suspend fun fetchModels(
        apiKey: String,
        baseURL: String? = null,
        forceRefresh: Boolean = false,
        customUserAgent: String? = null,
        azureMode: Boolean = false,
    ): List<LLMModel> = withContext(Dispatchers.IO) {
        val cacheKey = modelsCacheKey(baseURL, apiKey, azureMode)
        val url = buildURL(baseURL)
        val request = buildModelsRequest(url, apiKey, azureMode, customUserAgent)
        emptyList()
    }

    internal fun buildModelsRequest(
        url: String,
        apiKey: String,
        azureMode: Boolean,
        customUserAgent: String?,
    ): Request =
        Request.Builder()
            .url(url)
            .applyOpenAIKeyAuth(azureMode, apiKey)
            .applyUserAgentOverride(customUserAgent)
            .build()

    internal fun modelsCacheKey(baseURL: String?, apiKey: String, azureMode: Boolean): String =
        (baseURL ?: "") + "|" + apiKey + "|" + if (azureMode) "azure" else "bearer"
}
"""

BAD_OPENAI_MODELS_NO_PARAM = GOOD_OPENAI_MODELS.replace(
    "        azureMode: Boolean = false,\n", ""
)

BAD_OPENAI_MODELS_NO_BUILD_CALL = GOOD_OPENAI_MODELS.replace(
    "        val request = buildModelsRequest(url, apiKey, azureMode, customUserAgent)\n",
    "        val request = Request.Builder().url(url).build()\n",
)

BAD_OPENAI_MODELS_KEY_NOT_THREADED = GOOD_OPENAI_MODELS.replace(
    "buildModelsRequest(url, apiKey, azureMode, customUserAgent)",
    "buildModelsRequest(url, apiKey, false, customUserAgent)",
)

BAD_OPENAI_MODELS_CACHE_NOT_THREADED = GOOD_OPENAI_MODELS.replace(
    "modelsCacheKey(baseURL, apiKey, azureMode)",
    "modelsCacheKey(baseURL, apiKey, false)",
)

GOOD_OPENAI_ADAPTERS = """
object OpenAIAdapter_ok {
    fun a(instance: ProviderInstance, apiKey: String, context: Context?): List<LLMModel> {
        return OpenAIModelsApi.fetchModels(
            apiKey,
            instance.effectiveBaseURL,
            context = context,
            customUserAgent = instance.customUserAgent,
            azureMode = instance.azureMode,
            forceRefresh = forceRefresh,
        )
    }

    fun b(instance: ProviderInstance, apiKey: String, context: Context?): List<LLMModel> {
        return OpenAIModelsApi.fetchModels(
            apiKey,
            baseURL,
            context = context,
            customUserAgent = instance.customUserAgent,
            azureMode = false,
            forceRefresh = forceRefresh,
        )
    }
}
"""

BAD_OPENAI_ADAPTERS_DROP_SECOND = GOOD_OPENAI_ADAPTERS.replace(
    """            customUserAgent = instance.customUserAgent,
            azureMode = false,
            forceRefresh = forceRefresh,
        )
    }
}
""",
    """            customUserAgent = instance.customUserAgent,
            forceRefresh = forceRefresh,
        )
    }
}
""",
)

BAD_OPENAI_ADAPTERS_INVENTED_VALUE = GOOD_OPENAI_ADAPTERS.replace(
    "            azureMode = false,\n", "            azureMode = true,\n"
)

BAD_OPENAI_ADAPTERS_ALL_FALSE = GOOD_OPENAI_ADAPTERS.replace(
    "            azureMode = instance.azureMode,\n", "            azureMode = false,\n"
)

BAD_OPENAI_ADAPTERS_NO_SITE = """
object NoOpenAIArm {
    fun a(instance: ProviderInstance): List<LLMModel> = emptyList()
}
"""

GOOD_OTHER_SOURCES = [
    ("src/android/app/src/main/java/com/rikkaminis/app/provider/openai/OpenAIAuthHeader.kt",
     GOOD_OPENAI_AUTH),
    # a sibling provider with a DIFFERENT key header must stay clean
    ("src/android/app/src/main/java/com/rikkaminis/app/provider/anthropic/AnthropicProvider.kt",
     'builder.header("x-api-key", key).header("anthropic-version", "2023-06-01")'),
]

BAD_OTHER_SOURCES_DUPLICATE = GOOD_OTHER_SOURCES + [
    ("src/android/app/src/main/java/com/rikkaminis/app/provider/openai/OpenAIModelsApi.kt",
     'Request.Builder().url(url).header("api-key", apiKey).build()'),
]

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

    good_vp = check_voice(GOOD_VOICE_PROVIDER, GOOD_VOICE_VENDORS, GOOD_VOICE_FACTORY)
    expect_clean("compliant voice fixtures", good_vp)

    bad_vp_drop = check_voice(BAD_VOICE_PROVIDER_DROP, GOOD_VOICE_VENDORS, GOOD_VOICE_FACTORY)
    expect_trip("voice executeRequest-drop fixture (ISSUE-05 V1)", bad_vp_drop,
                "executeRequest does not apply")

    bad_vp_nofn = check_voice(BAD_VOICE_PROVIDER_NO_FN, GOOD_VOICE_VENDORS, GOOD_VOICE_FACTORY)
    expect_trip("voice executeRequest-renamed fixture (ISSUE-05 V1)", bad_vp_nofn,
                "executeRequest(' not found")

    bad_vv_drop = check_voice(GOOD_VOICE_PROVIDER, BAD_VOICE_VENDORS_DROP, GOOD_VOICE_FACTORY)
    expect_trip("xunfei WS-drop fixture (ISSUE-05 V2)", bad_vv_drop,
                "WebSocket request drops")

    bad_vv_moved = check_voice(GOOD_VOICE_PROVIDER, BAD_VOICE_VENDORS_ANCHOR_MOVED,
                               GOOD_VOICE_FACTORY)
    expect_trip("xunfei WS-anchor-moved fixture (ISSUE-05 V2)", bad_vv_moved,
                "not found")

    bad_vf_drop = check_voice(GOOD_VOICE_PROVIDER, GOOD_VOICE_VENDORS, BAD_VOICE_FACTORY_DROP)
    expect_trip("voice factory injection-drop fixture (ISSUE-05 V3)", bad_vf_drop,
                "does not inject")

    bad_vf_null = check_voice(GOOD_VOICE_PROVIDER, GOOD_VOICE_VENDORS, BAD_VOICE_FACTORY_NULL)
    expect_trip("voice factory fake-null wiring fixture (ISSUE-05 V3)", bad_vf_null,
                "does not inject")

    print("selftest compliant voice family   -> %d violation(s)" % len(good_vp))
    print("selftest voice executeRequest-drop-> %d violation(s)" % len(bad_vp_drop))
    print("selftest voice executeRequest-ren -> %d violation(s)" % len(bad_vp_nofn))
    print("selftest xunfei WS-drop           -> %d violation(s)" % len(bad_vv_drop))
    print("selftest xunfei WS-anchor-moved   -> %d violation(s)" % len(bad_vv_moved))
    print("selftest voice factory drop       -> %d violation(s)" % len(bad_vf_drop))
    print("selftest voice factory null       -> %d violation(s)" % len(bad_vf_null))

    # -- [da1030 ISSUE-06] OpenAI outbound arms ----------------------------
    def oai(provider=GOOD_OPENAI_PROVIDER, models=GOOD_OPENAI_MODELS,
            auth=GOOD_OPENAI_AUTH, adapters=GOOD_OPENAI_ADAPTERS,
            others=GOOD_OTHER_SOURCES):
        return check_openai_outbound(provider, models, auth, adapters, others)

    good_oai = oai()
    expect_clean("compliant openai-outbound fixtures", good_oai)
    # precision: the sibling Anthropic x-api-key line is a DIFFERENT header and
    # must not be counted as a second api-key source
    if any("AnthropicProvider.kt" in item for item in good_oai):
        failures.append("compliant openai fixtures: x-api-key flagged as api-key")
    print("selftest compliant openai-outbound-> %d violation(s)" % len(good_oai))

    def expect_single(name, v, needle):
        hits = [i for i in v if needle in i]
        if len(hits) != 1:
            failures.append("%s expected exactly 1 x '%s', got %s" % (name, needle, v))

    bad_p_drop = oai(provider=BAD_OPENAI_PROVIDER_DROP)
    expect_trip("passthrough-UA-drop fixture (D1)", bad_p_drop, "never applies")
    bad_p_after = oai(provider=BAD_OPENAI_PROVIDER_AFTER_BUILD)
    expect_trip("passthrough-UA-after-build fixture (D1)", bad_p_after,
                "AFTER 'builder.build()'")
    bad_p_renamed = oai(provider=BAD_OPENAI_PROVIDER_RENAMED)
    expect_trip("passthrough-anchor-moved fixture (D1)", bad_p_renamed, "body not found")

    bad_auth = oai(auth=BAD_OPENAI_AUTH_NO_FN)
    expect_trip("auth-single-source-missing fixture (D2a)", bad_auth,
                "applyOpenAIKeyAuth(' not found")
    bad_dup = oai(others=BAD_OTHER_SOURCES_DUPLICATE)
    expect_trip("duplicate-api-key-literal fixture (D2a)", bad_dup, "hard-coded")

    bad_m_param = oai(models=BAD_OPENAI_MODELS_NO_PARAM)
    expect_trip("models-no-azureMode-param fixture (D2b)", bad_m_param, "no longer declares")
    bad_m_build = oai(models=BAD_OPENAI_MODELS_NO_BUILD_CALL)
    expect_trip("models-no-build-call fixture (D2b)", bad_m_build,
                "no buildModelsRequest(...) call")
    bad_m_key = oai(models=BAD_OPENAI_MODELS_KEY_NOT_THREADED)
    expect_trip("models-key-not-threaded fixture (D2b)", bad_m_key, "does not pass azureMode")
    expect_single("models-key-not-threaded fixture (D2b)", bad_m_key, "does not pass azureMode")
    bad_m_cache = oai(models=BAD_OPENAI_MODELS_CACHE_NOT_THREADED)
    expect_trip("models-cache-not-threaded fixture (D2b)", bad_m_cache,
                "does not pass azureMode")
    expect_single("models-cache-not-threaded fixture (D2b)", bad_m_cache,
                  "does not pass azureMode")

    bad_ad2 = oai(adapters=BAD_OPENAI_ADAPTERS_DROP_SECOND)
    expect_trip("adapters-second-site-drop fixture (D3)", bad_ad2, "drops 'azureMode'")
    expect_single("adapters-second-site-drop fixture (D3)", bad_ad2, "drops 'azureMode'")
    bad_ad_invented = oai(adapters=BAD_OPENAI_ADAPTERS_INVENTED_VALUE)
    expect_trip("adapters-invented-value fixture (D3)", bad_ad_invented,
                "neither `instance.azureMode`")
    expect_single("adapters-invented-value fixture (D3)", bad_ad_invented,
                  "neither `instance.azureMode`")
    bad_ad_allfalse = oai(adapters=BAD_OPENAI_ADAPTERS_ALL_FALSE)
    expect_trip("adapters-all-false fixture (D3)", bad_ad_allfalse,
                "no OpenAIModelsApi.fetchModels call forwards")
    bad_ad_none = oai(adapters=BAD_OPENAI_ADAPTERS_NO_SITE)
    expect_trip("adapters-no-callsite fixture (D3)", bad_ad_none, "call not found")

    print("selftest openai passthrough drop -> %d violation(s)" % len(bad_p_drop))
    print("selftest openai passthrough order-> %d violation(s)" % len(bad_p_after))
    print("selftest openai auth single-src  -> %d violation(s)" % len(bad_auth))
    print("selftest openai api-key dup      -> %d violation(s)" % len(bad_dup))
    print("selftest openai models no-param  -> %d violation(s)" % len(bad_m_param))
    print("selftest openai models no-build  -> %d violation(s)" % len(bad_m_build))
    print("selftest openai models key thread-> %d violation(s)" % len(bad_m_key))
    print("selftest openai models cache thr -> %d violation(s)" % len(bad_m_cache))
    print("selftest openai adapters 2nd arm -> %d violation(s)" % len(bad_ad2))
    print("selftest openai adapters no-site -> %d violation(s)" % len(bad_ad_none))

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

#!/usr/bin/env python3
"""
FIX-E (da1020 ISSUE-05) JVM harness extractor — fail-closed.

Extracts the REAL production logic (byte-exact, balanced-brace) into a
sandbox-compilable harness:
  1. VoiceProvider.executeRequest (fixed: watcher + executeOrCancel)
  2. parseChatAsrTranscript (fixed: empty-transcript guard)
  3. VoiceProviderException + VoiceInputResponse (from VoiceModels.kt)
  4. applyUserAgentOverride (from UserAgentOverride.kt)
  5. D-shape envelope from OpenRouterModelsApi.fetchModels (catch structure)
Baseline differentials from `git show 942b0111d:<path>`:
  6. baseline executeRequest (bare execute, no watcher)
  7. baseline chat-ASR parse tail (no empty guard)

Every extraction carries presence assertions (fail-closed): a mutation that
removes a marker makes the extractor refuse to build the harness (EXTRACT_FAIL)
instead of silently compiling a stale mirror.
"""
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo/
A = os.path.join(BASE, "src/android/app/src/main/java/com/rikkaminis/app")
BASELINE = "942b0111d"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)))

VP = os.path.join(A, "provider/voice/VoiceProvider.kt")
VM = os.path.join(A, "provider/voice/VoiceModels.kt")
UAO = os.path.join(A, "provider/UserAgentOverride.kt")
OR = os.path.join(A, "provider/openrouter/OpenRouterModelsApi.kt")
GM = os.path.join(A, "provider/gemini/GeminiModelsApi.kt")
AD = os.path.join(A, "provider/ModelListProviderAdapters.kt")


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def git_show(rel_path):
    r = subprocess.run(
        ["git", "-C", BASE, "show", "%s:%s" % (BASELINE, rel_path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise AssertionError("git show %s failed: %s" % (rel_path, r.stderr[:200]))
    return r.stdout


def balanced_block(src, anchor):
    """Return the full declaration text from `anchor` through the balanced
    closing brace, or None."""
    i = src.find(anchor)
    if i < 0:
        return None
    j = src.find("{", i)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    return None


def require(cond, msg):
    if not cond:
        print("EXTRACT_FAIL " + msg)
        sys.exit(1)


def extract_fixed():
    vp = read(VP)

    body = balanced_block(vp, "suspend fun executeRequest")
    require(body is not None, "VoiceProvider.executeRequest not found")
    require("awaitCancellation" in body, "executeRequest: watcher missing (awaitCancellation)")
    require("invokeOnCompletion" in body, "executeRequest: hook missing")
    require("call.cancel()" in body, "executeRequest: call.cancel() missing")
    require("executeOrCancel()" in body, "executeRequest: executeOrCancel missing")
    require("watchDisposable.dispose()" in body, "executeRequest: dispose missing")
    require(".execute()" not in body, "executeRequest: bare .execute() still present")
    # body starts after the declaration header; keep the full text but rename
    fixed_exec = body.replace("suspend fun executeRequest", "suspend fun harnessExecuteRequest", 1)

    parse = balanced_block(vp, "internal fun parseChatAsrTranscript")
    require(parse is not None, "parseChatAsrTranscript not found")
    require("takeIf { it.isNotEmpty() }" in parse, "parseChatAsrTranscript: empty guard missing")
    require('"Empty ASR transcript in chat ASR response"' in parse,
            "parseChatAsrTranscript: guard message missing")

    vm = read(VM)
    exc = balanced_block(vm, "sealed class VoiceProviderException")
    require(exc is not None, "VoiceProviderException not found")
    require("class Parse" in exc, "VoiceProviderException: Parse subclass missing")
    resp = balanced_block(vm, "data class VoiceInputResponse")
    require(resp is not None, "VoiceInputResponse not found")

    uao = read(UAO)
    uaf = balanced_block(uao, "fun Request.Builder.applyUserAgentOverride")
    require(uaf is not None, "applyUserAgentOverride not found")
    require("customUserAgent: String?" in uaf, "applyUserAgentOverride: param missing")

    # D-shape envelope (fixed): val response = try { ... } catch ... }
    ors = read(OR)
    i = ors.find("val response = try {")
    require(i >= 0, "OpenRouterModelsApi: response try envelope not found")
    j = ors.find("\n        try {", i)
    require(j > i, "OpenRouterModelsApi: envelope end not found")
    envelope = ors[i:j]
    try_part = envelope.split("} catch", 1)[0]
    require("executeOrCancel()" in try_part, "D-shape: executeOrCancel not inside try")
    require("kotlinx.coroutines.CancellationException" in envelope, "D-shape: CancellationException catch missing")
    require("throw e" in envelope, "D-shape: cancellation rethrow missing")
    exc_catch = envelope.split("catch (e: Exception)", 1)
    require(len(exc_catch) == 2, "D-shape: Exception catch missing")
    require("return@withContext emptyList()" in exc_catch[1], "D-shape: Exception catch must return emptyList")
    print("D_SHAPE_OK executeOrCancel inside try, CancellationException rethrow, Exception -> emptyList")

    # adapter pass-through
    ads = read(AD)
    for api in ("GeminiModelsApi.fetchModels", "OpenRouterModelsApi.fetchModels"):
        i = ads.find(api + "(")
        require(i >= 0, "adapters: %s call not found" % api)
        depth = 0
        end = None
        for k in range(i + len(api), len(ads)):
            if ads[k] == "(":
                depth += 1
            elif ads[k] == ")":
                depth -= 1
                if depth == 0:
                    end = k
                    break
        require(end is not None, "adapters: %s unbalanced" % api)
        args = ads[i + len(api):end]
        require("customUserAgent = instance.customUserAgent" in args,
                "adapters: %s does not pass customUserAgent" % api)
    print("ADAPTER_UA_OK both adapters pass instance.customUserAgent")

    # GeminiModelsApi UA
    gms = read(GM)
    require(".applyUserAgentOverride(null)" not in gms, "GeminiModelsApi: hard-coded null UA override is back")
    require(".applyUserAgentOverride(customUserAgent)" in gms, "GeminiModelsApi: UA override not parameterized")
    require("customUserAgent: String? = null" in gms, "GeminiModelsApi: customUserAgent param missing")
    print("GEMINI_UA_OK")

    return fixed_exec, parse, exc, resp, uaf


def extract_baseline():
    bvp = git_show("src/android/app/src/main/java/com/rikkaminis/app/provider/voice/VoiceProvider.kt")
    bbody = balanced_block(bvp, "suspend fun executeRequest")
    require(bbody is not None, "baseline executeRequest not found")
    require(".execute()" in bbody, "baseline: bare execute() missing (defect marker)")
    require("executeOrCancel" not in bbody, "baseline: executeOrCancel unexpectedly present")
    bexec = bbody.replace("suspend fun executeRequest", "suspend fun baselineExecuteRequest", 1)

    # baseline chat-ASR parse tail: from the transcribeChatBased function
    bt = balanced_block(bvp, "open suspend fun transcribeChatBased")
    require(bt is not None, "baseline transcribeChatBased not found")
    s = bt.find("val obj = runCatching")
    require(s >= 0, "baseline parse tail start not found")
    e = bt.find("return VoiceInputResponse(text = text)")
    require(e > s, "baseline parse tail end not found")
    tail = bt[s:e + len("return VoiceInputResponse(text = text)")]
    require('?: ""' in tail, "baseline tail: empty-string fallthrough missing (defect marker)")
    require("takeIf" not in tail, "baseline tail: guard unexpectedly present")
    baseline_parse = "fun baselineParseChatAsrText(data: ByteArray): VoiceInputResponse {\n    " + tail.replace("\n", "\n    ") + "\n}"

    bor = git_show("src/android/app/src/main/java/com/rikkaminis/app/provider/openrouter/OpenRouterModelsApi.kt")
    require("val response = client.newCall(request).executeOrCancel()" in bor,
            "baseline OpenRouter: executeOrCancel outside try (defect marker) missing")

    bgm = git_show("src/android/app/src/main/java/com/rikkaminis/app/provider/gemini/GeminiModelsApi.kt")
    require(".applyUserAgentOverride(null)" in bgm,
            "baseline Gemini: hard-coded null UA (defect marker) missing")
    print("BASELINE_MARKERS_OK A/B/C/D defect markers all present on 942b0111d")
    return bexec, baseline_parse


HARNESS_TEMPLATE = '''package probe

// AUTO-GENERATED by extract.py — mirror of production code with minimal stubs.
// The extracted blocks are byte-exact production code; the only non-production
// pieces are the stubs below (sandbox JVM has no android surface).

import kotlinx.coroutines.*
import okhttp3.*
import org.json.JSONObject
import com.rikkaminis.app.provider.executeOrCancel
import java.util.Locale

// --- stubs ---
object Log { fun e(tag: String, msg: String) {} }
object MinisUserAgent { const val DEFAULT = "Minis-Test/1.0 (Android 99; sandbox)" }
var httpClient: okhttp3.Call.Factory = OkHttpClient()
const val TAG = "probe"

// --- real VoiceProviderException (VoiceModels.kt) ---
{EXC}

// --- real VoiceInputResponse (VoiceModels.kt) ---
{RESP}

// --- real applyUserAgentOverride (UserAgentOverride.kt; MinisUserAgent stubbed) ---
{UAF}

// --- real executeRequest body (VoiceProvider.kt, fixed tree) ---
{FIXED_EXEC}

// --- real parseChatAsrTranscript (VoiceProvider.kt, fixed tree) ---
{PARSE}

// --- BASELINE mirrors @ 942b0111d ---
{BASE_EXEC}

{BASE_PARSE}
'''


def main():
    fixed_exec, parse, exc, resp, uaf = extract_fixed()
    base_exec, base_parse = extract_baseline()
    harness = (HARNESS_TEMPLATE
               .replace("{EXC}", exc)
               .replace("{RESP}", resp)
               .replace("{UAF}", uaf)
               .replace("{FIXED_EXEC}", fixed_exec)
               .replace("{PARSE}", parse)
               .replace("{BASE_EXEC}", base_exec)
               .replace("{BASE_PARSE}", base_parse))
    with open(os.path.join(OUT, "Harness.kt"), "w", encoding="utf-8") as fh:
        fh.write(harness)
    print("EXTRACT_OK Harness.kt written")


if __name__ == "__main__":
    main()

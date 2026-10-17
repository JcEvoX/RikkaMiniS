#!/usr/bin/env python3
"""
Gate: FIX-E (deep-audit-1020 ISSUE-05) — voice-chain cancel/guard/fallback shapes.

A (VoiceProvider.kt executeRequest)
  The blocking HTTP read must be cancellable two ways: `executeOrCancel()`
  (cancellation rethrow + ensureActive + cancel-at-completion) and the
  prompt-interruption child watcher (`launch { awaitCancellation() }` whose
  completion hook cancels the Call while the read is still in flight — the
  one-arg invokeOnCompletion hook on the withContext job itself only fires at
  final state, sandbox-verified with kotlinx-coroutines 1.9.0). The bare
  `newCall(request).execute()` spelling must not come back, and the success
  path must dispose the watcher hook (zero cancellation on completion).

B (parseChatAsrTranscript)
  The chat-ASR parse must carry the empty-transcript guard
  (`takeIf { it.isNotEmpty() }` + Parse throw) and `transcribeChatBased` must
  route through it — otherwise a malformed chat response maps to
  text="" and the consumer reports NO_MATCH ("heard nothing") instead of a
  protocol error that advances the fail-over chain.

D (OpenRouterModelsApi.fetchModels)
  `executeOrCancel()` must sit INSIDE the try (CancellationException rethrow,
  Exception -> return@withContext emptyList()): outside, an IOException
  propagated out of fetchModels instead of returning the empty fallback.

Usage: python3 scripts/scan/voice_cancel_guard_check.py [repo_root]
       python3 scripts/scan/voice_cancel_guard_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import os
import re
import sys

VP_REL = "src/android/app/src/main/java/com/rikkaminis/app/provider/voice/VoiceProvider.kt"
OR_REL = "src/android/app/src/main/java/com/rikkaminis/app/provider/openrouter/OpenRouterModelsApi.kt"


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for line in src.split("\n"):
        cut = None
        in_str = False
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
    return "\n".join(out)


def balanced_block(src, anchor):
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


def check_voiceprovider(src):
    v = []
    code = strip_comments(src)

    body = balanced_block(code, "suspend fun executeRequest")
    if body is None:
        v.append("VoiceProvider.kt: executeRequest declaration not found "
                 "(gate anchors moved — deliberate edit required)")
        return v
    if "executeOrCancel()" not in body:
        v.append("VoiceProvider.kt: executeRequest no longer calls "
                 "executeOrCancel() (cancellation rethrow + ensureActive lost)")
    if ".execute()" in body:
        v.append("VoiceProvider.kt:%d: bare '.execute()' is back in "
                 "executeRequest (da1020 ISSUE-05: uncancellable blocking read)"
                 % (code.count("\n", 0, code.find(".execute()")) + 1))
    if "awaitCancellation" not in body:
        v.append("VoiceProvider.kt: prompt-interruption watcher missing "
                 "(launch { awaitCancellation() }) — the one-arg "
                 "invokeOnCompletion hook only fires at final state, so a "
                 "cancelled coroutine would hold the connection through the "
                 "whole 120s readTimeout")
    if "call.cancel()" not in body:
        v.append("VoiceProvider.kt: watcher hook no longer cancels the Call "
                 "(call.cancel() missing)")
    if "watchDisposable.dispose()" not in body:
        v.append("VoiceProvider.kt: watcher hook is no longer disposed on the "
                 "success path (every completed request would cancel its Call)")

    parse = balanced_block(code, "fun parseChatAsrTranscript")
    if parse is None:
        v.append("VoiceProvider.kt: parseChatAsrTranscript declaration not found "
                 "(the chat-ASR parse was inlined or renamed — deliberate edit)")
        return v
    if "takeIf { it.isNotEmpty() }" not in parse:
        v.append("VoiceProvider.kt: parseChatAsrTranscript lost the "
                 "empty-transcript guard (takeIf) — malformed chat responses "
                 "map to text=\"\" and NO_MATCH instead of a protocol error")
    if 'throw VoiceProviderException.Parse("Empty ASR transcript in chat ASR response")' not in parse:
        v.append("VoiceProvider.kt: parseChatAsrTranscript lost the "
                 "empty-transcript Parse throw")
    tcb = balanced_block(code, "suspend fun transcribeChatBased")
    if tcb is None or "parseChatAsrTranscript(" not in tcb:
        v.append("VoiceProvider.kt: transcribeChatBased no longer routes "
                 "through parseChatAsrTranscript (a second inline parse would "
                 "re-open the empty-transcript hole)")
    return v


def check_openrouter(src):
    v = []
    code = strip_comments(src)
    i = code.find("val response = try {")
    if i < 0:
        v.append("OpenRouterModelsApi.kt: response try envelope not found "
                 "(da1020 ISSUE-05: executeOrCancel must sit inside a try)")
        return v
    j = code.find("\n        try {", i)
    if j < 0:
        v.append("OpenRouterModelsApi.kt: envelope end not found")
        return v
    envelope = code[i:j]
    try_part = envelope.split("} catch", 1)[0]
    if "executeOrCancel()" not in try_part:
        v.append("OpenRouterModelsApi.kt: executeOrCancel() is back OUTSIDE the "
                 "try (da1020 ISSUE-05: an IOException would propagate out of "
                 "fetchModels instead of returning the empty fallback)")
    if "kotlinx.coroutines.CancellationException" not in envelope:
        v.append("OpenRouterModelsApi.kt: CancellationException catch missing "
                 "(a cancelled fetch must unwind, not fall back)")
    if "throw e" not in envelope:
        v.append("OpenRouterModelsApi.kt: cancellation rethrow (throw e) missing")
    parts = envelope.split("catch (e: Exception)")
    if len(parts) < 2:
        v.append("OpenRouterModelsApi.kt: Exception catch missing")
    elif "return@withContext emptyList()" not in parts[1]:
        v.append("OpenRouterModelsApi.kt: Exception catch must return the "
                 "empty fallback (return@withContext emptyList())")
    return v


def read(root, rel):
    with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def run(root):
    if not os.path.isdir(root):
        print("voice_cancel_guard_check: not a directory: %s" % root)
        return 2
    try:
        vp = read(root, VP_REL)
        ors = read(root, OR_REL)
    except OSError as exc:
        print("voice_cancel_guard_check: cannot read sources: %s" % exc)
        return 2
    violations = check_voiceprovider(vp) + check_openrouter(ors)
    if violations:
        print("FAIL voice_cancel_guard_check")
        for x in violations:
            print("  " + x)
        return 1
    print("PASS voice_cancel_guard_check (cancel watcher + empty-transcript "
          "guard + openrouter fallback envelope all wired)")
    return 0


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

GOOD_VP = '''
class VoiceProvider {
    companion object {
        val httpClient: OkHttpClient = OkHttpClient()
        private const val TAG = "VoiceProvider"
    }

    suspend fun executeRequest(request: Request): ByteArray = withContext(Dispatchers.IO) {
        val call = httpClient.newCall(request)
        val watchJob = launch { awaitCancellation() }
        val watchDisposable = watchJob.invokeOnCompletion { _ -> call.cancel() }
        try {
            call.executeOrCancel().use { response ->
                val body = response.body?.bytes()
                if (!response.isSuccessful) {
                    throw VoiceProviderException.Http(response.code, body)
                }
                body ?: ByteArray(0)
            }
        } finally {
            watchDisposable.dispose()
            watchJob.cancel()
        }
    }

    open suspend fun transcribeChatBased(request: VoiceInputRequest): VoiceInputResponse {
        val data = executeRequest(request.toRequest())
        return VoiceInputResponse(text = parseChatAsrTranscript(data))
    }
}

internal fun parseChatAsrTranscript(data: ByteArray): String {
    val obj = runCatching { JSONObject(String(data, Charsets.UTF_8)) }.getOrNull()
        ?: throw VoiceProviderException.Parse("Undecodable chat ASR response")
    val text = obj.optJSONArray("choices")
        ?.optJSONObject(0)
        ?.optJSONObject("message")
        ?.optString("content")
        ?.trim()
        ?.takeIf { it.isNotEmpty() }
        ?: throw VoiceProviderException.Parse("Empty ASR transcript in chat ASR response")
    return text
}
'''

BAD_VP_BARE_EXECUTE = GOOD_VP.replace(
    "call.executeOrCancel().use { response ->", "call.execute().use { response ->")

BAD_VP_NO_WATCHER = GOOD_VP.replace(
    "        val watchJob = launch { awaitCancellation() }\n"
    "        val watchDisposable = watchJob.invokeOnCompletion { _ -> call.cancel() }\n", "")

BAD_VP_NO_GUARD = GOOD_VP.replace(
    "        ?.trim()\n"
    "        ?.takeIf { it.isNotEmpty() }\n"
    "        ?: throw VoiceProviderException.Parse(\"Empty ASR transcript in chat ASR response\")\n"
    "    return text",
    "        ?.trim()\n"
    "        ?: \"\"\n"
    "    return text",
).replace("return VoiceInputResponse(text = parseChatAsrTranscript(data))",
          "return VoiceInputResponse(text = data.decodeToString())")

GOOD_OR = '''
object OpenRouterModelsApi {
    suspend fun fetchModels(apiKey: String): List<LLMModel> = withContext(Dispatchers.IO) {
        val request = Request.Builder().url("https://openrouter.ai/api/v1/models").build()
        val response = try {
            client.newCall(request).executeOrCancel()
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            return@withContext emptyList()
        }
        try {
            emptyList()
        } finally {
            response.close()
        }
    }
}
'''

BAD_OR_OUTSIDE_TRY = GOOD_OR.replace(
    """        val response = try {
            client.newCall(request).executeOrCancel()
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            return@withContext emptyList()
        }""",
    "        val response = client.newCall(request).executeOrCancel()",
)

BAD_OR_NO_EXEC_IN_TRY = GOOD_OR.replace(
    "client.newCall(request).executeOrCancel()",
    "client.newCall(request).execute()",
)


def selftest():
    failures = []

    def expect_clean(name, v):
        if v:
            failures.append("%s must be clean, got %s" % (name, v))

    def expect_trip(name, v, needle):
        if not any(needle in item for item in v):
            failures.append("%s did not trip '%s' (got %s)" % (name, needle, v))

    good_vp = check_voiceprovider(GOOD_VP)
    good_or = check_openrouter(GOOD_OR)
    expect_clean("compliant VoiceProvider fixture", good_vp)
    expect_clean("compliant OpenRouter fixture", good_or)

    bad_exec = check_voiceprovider(BAD_VP_BARE_EXECUTE)
    expect_trip("bare-execute fixture", bad_exec, ".execute()")

    bad_watcher = check_voiceprovider(BAD_VP_NO_WATCHER)
    expect_trip("no-watcher fixture", bad_watcher, "watcher missing")

    bad_guard = check_voiceprovider(BAD_VP_NO_GUARD)
    expect_trip("no-empty-guard fixture", bad_guard, "empty-transcript guard")
    expect_trip("no-empty-guard fixture", bad_guard, "parseChatAsrTranscript")

    bad_or = check_openrouter(BAD_OR_OUTSIDE_TRY)
    expect_trip("executeOrCancel-outside-try fixture", bad_or, "must sit inside a try")

    bad_or2 = check_openrouter(BAD_OR_NO_EXEC_IN_TRY)
    expect_trip("execute-inside-try fixture", bad_or2, "OUTSIDE the try")

    print("selftest compliant VoiceProvider    -> %d violation(s)" % len(good_vp))
    print("selftest compliant OpenRouter       -> %d violation(s)" % len(good_or))
    print("selftest bare-execute               -> %d violation(s)" % len(bad_exec))
    print("selftest no-watcher                 -> %d violation(s)" % len(bad_watcher))
    print("selftest no-empty-guard             -> %d violation(s)" % len(bad_guard))
    print("selftest executeOrCancel-outside-try-> %d violation(s)" % len(bad_or))
    print("selftest execute-inside-try         -> %d violation(s)" % len(bad_or2))

    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    print("VOICE_CANCEL_GUARD_SELFTEST=PASS")
    return 0


def main():
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        return selftest()
    root = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(root)


if __name__ == "__main__":
    sys.exit(main())

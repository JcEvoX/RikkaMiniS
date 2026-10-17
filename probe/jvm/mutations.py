#!/usr/bin/env python3
"""
FIX-E mutation negative controls — memory byte backup + sha256 restore check,
NO git restore (the working tree must be committed before running; this driver
never touches git).

Mutations:
  M1  VoiceProvider: call.executeOrCancel().use -> call.execute().use
      expect: voice gate RED (bare .execute() + executeOrCancel lost)
  M2  VoiceProvider: chat-ASR empty guard -> baseline fallthrough (?: "")
      expect: voice gate RED (empty-transcript guard + Parse throw lost)
  M3  GeminiModelsApi: .applyUserAgentOverride(customUserAgent) -> (null)
      expect: UA gate RED (hard-coded null is back)
  M4  OpenRouterModelsApi: fallback envelope -> baseline (executeOrCancel outside try)
      expect: voice gate RED (envelope not found / outside try)
  M5  VoiceProvider: watcher removed (launch { awaitCancellation() } + hook)
      expect: voice gate RED (watcher missing)
  M6  VoiceProvider: watcher hook not disposed (drop watchDisposable.dispose())
      expect: voice gate RED (dispose lost)

Usage: python3 mutations.py --index N   (one per invocation)
       python3 mutations.py --collect   (summary from state json)
"""
import hashlib
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
A = os.path.join(BASE, "src/android/app/src/main/java/com/rikkaminis/app")
VP = os.path.join(A, "provider/voice/VoiceProvider.kt")
GM = os.path.join(A, "provider/gemini/GeminiModelsApi.kt")
OR = os.path.join(A, "provider/openrouter/OpenRouterModelsApi.kt")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mut_state.json")

MUTATIONS = {
    1: (VP, [("call.executeOrCancel().use { response ->", "call.execute().use { response ->")]),
    2: (VP, [(
        "        ?.trim()\n"
        "        ?.takeIf { it.isNotEmpty() }\n"
        "        ?: throw VoiceProviderException.Parse(\"Empty ASR transcript in chat ASR response\")\n"
        "    return text",
        "        ?.trim()\n"
        "        ?: \"\"\n"
        "    return text",
    )]),
    3: (GM, [(".applyUserAgentOverride(customUserAgent)", ".applyUserAgentOverride(null)")]),
    4: (OR, [(
        "        val response = try {\n"
        "            client.newCall(request).executeOrCancel()\n"
        "        } catch (e: kotlinx.coroutines.CancellationException) {\n"
        "            // A cancelled fetch must unwind, not masquerade as a network error\n"
        "            // and hand back the empty list.\n"
        "            throw e\n"
        "        } catch (e: Exception) {\n"
        "            android.util.Log.w(TAG, \"models fetch failed, using empty list: ${e.message}\")\n"
        "            return@withContext emptyList()\n"
        "        }",
        "        val response = client.newCall(request).executeOrCancel()",
    )]),
    5: (VP, [(
        "        val watchJob = launch { awaitCancellation() }\n"
        "        val watchDisposable = watchJob.invokeOnCompletion { _ -> call.cancel() }\n", "",
    )]),
    6: (VP, [(
        "            watchDisposable.dispose()\n", "",
    )]),
}

EXPECT = {
    1: {"voice": ["bare '.execute()'"], "ua": []},
    2: {"voice": ["empty-transcript guard"], "ua": []},
    3: {"voice": [], "ua": ["applyUserAgentOverride(null)"]},
    4: {"voice": ["must sit inside a try"], "ua": []},
    5: {"voice": ["watcher missing"], "ua": []},
    6: {"voice": ["no longer disposed"], "ua": []},
}


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def run_gate(script, root):
    r = subprocess.run(["python3", script, root], capture_output=True, text=True, cwd=BASE)
    return r.returncode, (r.stdout + r.stderr)


def apply(index):
    path, subs = MUTATIONS[index]
    with open(path, "r", encoding="utf-8") as fh:
        orig = fh.read()
    mutated = orig
    for old, new in subs:
        if old not in mutated:
            print("MUT_APPLY_FAIL anchor not found in %s" % path)
            sys.exit(1)
        mutated = mutated.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(mutated)
    return orig, hashlib.sha256(orig.encode("utf-8")).hexdigest()


def restore(path, orig, expected_sha):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(orig)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read() == orig


def main():
    index = None
    collect = False
    for i, a in enumerate(sys.argv[1:]):
        if a == "--index":
            index = int(sys.argv[i + 2])
        if a == "--collect":
            collect = True
    state = {}
    if os.path.exists(STATE):
        with open(STATE) as fh:
            state = json.load(fh)
    if collect:
        all_ok = all(e["ok"] for e in state.values())
        for k in sorted(state, key=int):
            e = state[k]
            print("M%s %s voice=%s ua=%s restored=%s" % (
                k, "PRECISE" if e["ok"] else "MISS",
                e["voice_ok"], e["ua_ok"], e["restored"]))
        print("ALL_MUTATIONS_OK" if all_ok else "ALL_MUTATIONS_MISS")
        return 0 if all_ok else 1

    if index is None:
        print("usage: mutations.py --index N | --collect")
        return 2
    path, _ = MUTATIONS[index]
    orig, orig_sha = apply(index)
    vr, vout = run_gate("scripts/scan/voice_cancel_guard_check.py", ".")
    ur, uout = run_gate("scripts/scan/provider_ua_parity_check.py", ".")
    def gate_ok(rc, out, needles):
        if needles:
            return rc == 1 and any(n in out for n in needles)
        return rc == 0

    voice_ok = gate_ok(vr, vout, EXPECT[index]["voice"])
    ua_ok = gate_ok(ur, uout, EXPECT[index]["ua"])
    restored = restore(path, orig, orig_sha)
    entry = {
        "voice_rc": vr, "ua_rc": ur,
        "voice_ok": voice_ok, "ua_ok": ua_ok,
        "restored": restored, "ok": voice_ok and ua_ok and restored,
    }
    state[str(index)] = entry
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=1)
    print("MUT%s voice_rc=%s ua_rc=%s voice_ok=%s ua_ok=%s restored=%s" % (
        index, vr, ur, voice_ok, ua_ok, restored))
    return 0 if entry["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

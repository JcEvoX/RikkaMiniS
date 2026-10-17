#!/usr/bin/env python3
"""deep-audit-1020 ISSUE-01 (P1) — provider models-cache wiring gate.

Pins the contract fixed by branch fix/da1020-provider-models-cache: the sole
production call site `ProviderRepository.refreshModels` ->
`ModelListProviderRegistry.fetchModels(...)` must forward the application
`Context`, the Registry must keep the `context` parameter and forward it to
every fetcher, the five *ModelsApi adapters must keep passing
`context = context`, and the disk-cache classes must keep their
load / save / invalidate surface (the three things that were 100% inert while
the context was dropped: 7-day disk cache read, 401/403 invalidate, save).

Every *ModelsApi short-circuits on `context != null`
(OpenAIModelsApi:95/166, AnthropicModelsApi:42/117/175, GeminiModelsApi:75/
113/154, OpenRouterModelsApi:39/58/73), so a dropped argument does not fail
anywhere — it silently disables the whole cache chain.

Usage: python3 provider_models_cache_wiring_check.py [ROOT]   (ROOT = repo root)
       python3 provider_models_cache_wiring_check.py --root ROOT
       python3 provider_models_cache_wiring_check.py --selftest
Exit:  0 = gate green, 1 = violations found (or selftest failures).
"""
import os
import re
import sys

REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/ProviderRepository.kt"
REGISTRY_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/ModelListProviderRegistry.kt"
ADAPTERS_REL = "src/android/app/src/main/java/com/rikkaminis/app/provider/ModelListProviderAdapters.kt"
CACHE_REL = "src/android/app/src/main/java/com/rikkaminis/app/provider/ProviderModelsCache.kt"

REPO_ANCHOR = "ModelListProviderRegistry.fetchModels("
ADAPTER_ANCHOR_RE = re.compile(r"\w+ModelsApi\.fetchModels\(")
ADAPTER_PASS_ARG = "context = context"


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def call_args(text, anchor, start=0):
    """Argument list of the FIRST call matching `anchor` at/after `start`.

    Returns (args_list, end_offset) or (None, start) when not found.
    Parenthesis-balanced; string literals skipped.
    """
    i = text.find(anchor, start)
    if i < 0:
        return None, start
    j = i + len(anchor)
    depth = 1
    while j < len(text):
        c = text[j]
        if c == '"':
            j += 1
            while j < len(text) and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth != 0:
        return None, i
    inner = text[i + len(anchor):j]
    return split_top_level(inner), j


def split_top_level(inner):
    """Split an argument list on top-level commas."""
    parts, buf, depth = [], [], 0
    k = 0
    while k < len(inner):
        c = inner[k]
        if c == '"':
            end = k + 1
            while end < len(inner) and inner[end] != '"':
                end += 2 if inner[end] == "\\" else 1
            buf.append(inner[k:end + 1])
            k = end + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        k += 1
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p != ""]


def analyse(repo, registry, adapters, cache):
    """Returns list of (criterion, ok, detail, file, line). ok=True = held."""
    res = []

    # ── 1. the sole production call site must forward the Context ──
    clean = strip_comments(repo)
    count = clean.count(REPO_ANCHOR)
    res.append((
        "ProviderRepository has exactly one ModelListProviderRegistry.fetchModels call site",
        count == 1,
        "found %d" % count,
        REPO_REL,
        clean.count("\n", 0, clean.find(REPO_ANCHOR)) + 1 if count else 0,
    ))
    if count == 1:
        args, _ = call_args(clean, REPO_ANCHOR)
        ok = bool(args) and args[-1] == "context"
        res.append((
            "the production call site forwards `context` as its last argument",
            ok,
            "args=%s" % (args if args else "unparseable"),
            REPO_REL,
            clean.count("\n", 0, clean.find(REPO_ANCHOR)) + 1,
        ))

    # ── 2. the Registry must keep the context parameter and forward it ──
    clean = strip_comments(registry)
    sig_ok = re.search(r"\bcontext:\s*android\.content\.Context\??\b", clean) is not None
    res.append((
        "Registry fetchModels keeps a `context: Context?` parameter",
        sig_ok,
        "parameter declaration %s" % ("present" if sig_ok else "MISSING"),
        REGISTRY_REL,
        clean.count("\n", 0, clean.find("context:")) + 1 if "context:" in clean else 0,
    ))
    fwd = 0
    fwd_sites = 0
    pos = 0
    while True:
        args, pos = call_args(clean, "provider.fetchModels(", pos)
        if args is None:
            break
        fwd_sites += 1
        if args and args[-1] == "context":
            fwd += 1
    res.append((
        "Registry forwards `context` to every provider.fetchModels call",
        fwd_sites > 0 and fwd == fwd_sites,
        "%d/%d call sites forward it" % (fwd, fwd_sites),
        REGISTRY_REL,
        0,
    ))

    # ── 3. every adapter call site must pass `context = context` (or be the
    #       known no-cache XAI site with no argument list) ──
    clean = strip_comments(adapters)
    passing, silent, viol = 0, 0, []
    for m in ADAPTER_ANCHOR_RE.finditer(clean):
        args, _ = call_args(clean, m.group(0), m.start())
        if args is None:
            viol.append((m.group(0), "unparseable argument list"))
            continue
        if ADAPTER_PASS_ARG in args:
            passing += 1
        elif not args:
            silent += 1  # XAI-style no-cache fetcher: accepted
        else:
            viol.append((m.group(0), "args=%s (no `context = context`)" % args))
    res.append((
        "every caching adapter passes `context = context` to its ModelsApi",
        not viol,
        "; ".join("%s %s" % v for v in viol) if viol else
        "%d passing, %d no-cache (XAI)" % (passing, silent),
        ADAPTERS_REL,
        0,
    ))
    res.append((
        "at least one caching adapter per family forwards the context",
        passing >= 4,
        "%d passing sites" % passing,
        ADAPTERS_REL,
        0,
    ))

    # ── 4. the disk-cache classes must keep their surface ──
    for needle, crit in (
        ("fun load(", "ProviderModelsCache.load reachable"),
        ("fun save(", "ProviderModelsCache.save reachable"),
        ("fun invalidate(", "ProviderModelsCache.invalidate reachable"),
        ("fun cacheDir(context", "cache dir is context-scoped (cacheDir(context...))"),
    ):
        i = strip_comments(cache).find(needle)
        res.append((
            crit,
            i >= 0,
            needle,
            CACHE_REL,
            strip_comments(cache).count("\n", 0, i) + 1 if i >= 0 else 0,
        ))

    return res


COMPLIANT_REPO = '''package probe

class Repo(private val context: android.content.Context) {
    suspend fun refreshModels(instance: Any, apiKey: String?, forceRefresh: Boolean) {
        val models = try {
            ModelListProviderRegistry.fetchModels(instance, apiKey, false, forceRefresh, context)
        } catch (e: Exception) {
            emptyList<Any>()
        }
    }
}
'''

COMPLIANT_REGISTRY = '''package probe

object ModelListProviderRegistry {
    suspend fun fetchModels(
        instance: Any,
        apiKey: String?,
        thirdParty: Boolean,
        forceRefresh: Boolean = false,
        context: android.content.Context? = null,
    ): List<Any> {
        if (apiKey == null) return provider.fetchModels(null, instance, thirdParty, forceRefresh, context)
        return provider.fetchModels(apiKey, instance, thirdParty, forceRefresh, context)
    }
}
'''

COMPLIANT_ADAPTERS = '''package probe

object Adapters {
    fun a() = AnthropicModelsApi.fetchModels(x, y, z, f, context = context)
    fun b() = GeminiModelsApi.fetchModels(x, y, z, f, context = context)
    fun c() = OpenAIModelsApi.fetchModels(x, y, z, f, context = context)
    fun d() = OpenAIModelsApi.fetchModels(x2, y2, z2, f2, context = context)
    fun e() = OpenRouterModelsApi.fetchModels(x, y, z, f, context = context)
    fun f() = XAIModelsApi.fetchModels()
}
'''

COMPLIANT_CACHE = '''package probe

class ProviderModelsCache(private val namespace: String) {
    private fun cacheDir(context: android.content.Context) = java.io.File(context.cacheDir, "models-cache/$namespace")

    fun load(context: android.content.Context, key: String): List<Any>? = null

    fun save(context: android.content.Context, key: String, models: List<Any>) {}

    fun invalidate(context: android.content.Context, key: String) {}
}
'''


def fixture_result():
    return analyse(COMPLIANT_REPO, COMPLIANT_REGISTRY, COMPLIANT_ADAPTERS, COMPLIANT_CACHE)


BREAKERS = {}


def _breaker(idx, target_file, pairs, target_criterion):
    BREAKERS[idx] = {"file": target_file, "pairs": pairs, "target": target_criterion}


# B1: the production call site drops the Context again (pre-fix shape)
_breaker(1, "repo", [("forceRefresh, context)", "forceRefresh)")],
         "the production call site forwards `context` as its last argument")
# B2: an adapter silently passes null instead of the context
_breaker(2, "adapters", [("context = context)", "context = null)")],
         "every caching adapter passes `context = context` to its ModelsApi")
# B3: the cache loses its invalidate surface (defeats the 401/403 path)
_breaker(3, "cache", [("fun invalidate(context: android.content.Context, key: String) {}", "")],
         "ProviderModelsCache.invalidate reachable")
# B4: the cache dir stops being context-scoped
_breaker(4, "cache", [('private fun cacheDir(context: android.content.Context) = java.io.File(context.cacheDir, "models-cache/$namespace")',
                       'private fun cacheDir() = java.io.File("/tmp", "models-cache/$namespace")')],
         "cache dir is context-scoped (cacheDir(context...))")
# B5: the Registry loses its context parameter (whole chain collapses)
_breaker(5, "registry", [("        context: android.content.Context? = null,\n", "")],
         "Registry fetchModels keeps a `context: Context?` parameter")


def selftest():
    print("── selftest: compliant fixture must be green, each breaker must go red ──")
    failures = []
    base = fixture_result()
    reds0 = [c for c, ok, _, _, _ in base if not ok]
    if reds0:
        failures.append("compliant fixture is not green: %s" % reds0)
        print("  [FAIL] compliant fixture red on: %s" % reds0)
    else:
        print("  [OK] compliant fixture green on all %d criteria" % len(base))

    for idx, spec in sorted(BREAKERS.items()):
        src = {"repo": COMPLIANT_REPO, "registry": COMPLIANT_REGISTRY,
               "adapters": COMPLIANT_ADAPTERS, "cache": COMPLIANT_CACHE}[spec["file"]]
        mutated = src
        missing = [old for old, _ in spec["pairs"] if old not in mutated]
        if missing:
            failures.append("breaker %d cannot apply (fixture text moved): %s" % (idx, missing))
            print("  [FAIL] breaker %d: anchor not found in fixture" % idx)
            continue
        for old, new in spec["pairs"]:
            mutated = mutated.replace(old, new, 1)
        res = analyse(
            mutated if spec["file"] == "repo" else COMPLIANT_REPO,
            mutated if spec["file"] == "registry" else COMPLIANT_REGISTRY,
            mutated if spec["file"] == "adapters" else COMPLIANT_ADAPTERS,
            mutated if spec["file"] == "cache" else COMPLIANT_CACHE,
        )
        red = [c for c, ok, _, _, _ in res if not ok]
        if spec["target"] not in red:
            failures.append("breaker %d did not turn %r red (red=%s)" % (idx, spec["target"], red))
            print("  [FAIL] breaker %d -> %r stayed green (red=%s)" % (idx, spec["target"], red))
        else:
            extra = "" if len(red) == 1 else " (+%d side)" % (len(red) - 1)
            print("  [OK] breaker %d -> red on %r%s" % (idx, spec["target"], extra))
    return failures


def main():
    if "--selftest" in sys.argv:
        fails = selftest()
        if fails:
            print("SELFTEST FAIL (%d)" % len(fails))
            return 1
        print("PROVIDER_MODELS_CACHE_WIRING_SELFTEST=PASS (%d fixtures)" % (1 + len(BREAKERS)))
        return 0

    root = None
    args = list(sys.argv[1:])
    if "--root" in args:
        root = args[args.index("--root") + 1]
    elif args:
        root = args[0]
    root = root or "."

    files = {}
    for rel in (REPO_REL, REGISTRY_REL, ADAPTERS_REL, CACHE_REL):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            print("FAIL %s not found under root %s" % (rel, root))
            return 1
        files[rel] = open(path, encoding="utf-8").read()

    results = analyse(files[REPO_REL], files[REGISTRY_REL], files[ADAPTERS_REL], files[CACHE_REL])
    failed = [(c, d, f, l) for c, ok, d, f, l in results if not ok]
    for c, ok, d, f, l in results:
        if ok:
            print("  [OK] %s" % c)
        else:
            print("  [FAIL] %s:%s %s — %s" % (os.path.basename(f), l, c, d))

    total = len(results)
    print("Summary: %d passed, %d failed" % (total - len(failed), len(failed)))
    if failed:
        print("provider models-cache wiring gate: RED — the fix contract is violated")
        return 1
    print("GATE=GREEN provider_models_cache_wiring")
    return 0


if __name__ == "__main__":
    sys.exit(main())

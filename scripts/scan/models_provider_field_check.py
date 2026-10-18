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

Usage: python3 scripts/scan/models_provider_field_check.py [root]
       python3 scripts/scan/models_provider_field_check.py --root <root>
       python3 scripts/scan/models_provider_field_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import argparse
import os
import sys
import tempfile

APP = "src/android/app/src/main/java/com/rikkaminis/app"
MC_REL = APP + "/config/collections/ModelsCollection.kt"
MD_REL = APP + "/provider/ModelsDevApi.kt"

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


def run_checks(root, rels=None):
    rels = rels or {"mc": MC_REL, "md": MD_REL}
    srcs = {k: read(rel, root) for k, rel in rels.items()}
    missing = [(rel, 0, "?", "file not found — gate ROT")
               for k, rel in rels.items() if srcs[k] is None]
    if missing:
        return missing
    checks = []
    checks += check_arm1(srcs["mc"], rels["mc"])
    checks += check_arm2(srcs["md"], rels["md"])
    return checks


# ----------------------------------------------------------------------
# Selftest fixtures — minimal Kotlin-shaped files exercising every arm.
# ----------------------------------------------------------------------
FIX_MC_OK = """override fun add(payload: ConfigValue): String {
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


def _report(checks, fixture_name, label):
    if not checks:
        print("  [OK]   %s (%s)" % (fixture_name, label))
        return True
    for rel, line, arm, msg in checks:
        print("  [FAIL] %s arm %s line %s: %s" % (fixture_name, arm, line, msg))
    return False


def selftest():
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        rels = {"mc": "mc.kt", "md": "md.kt"}

        def write(name, text):
            with open(os.path.join(tmp, rels[name]), "w", encoding="utf-8") as f:
                f.write(text)

        # 1. Fixed shape: clean.
        write("mc", FIX_MC_OK)
        write("md", FIX_MD_OK)
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
        print("models_provider_field_check: OK (2 arms clean)")
        return 0
    for rel, line, arm, msg in checks:
        print("%s:%s [arm %s] %s" % (rel, line, arm, msg))
    print("models_provider_field_check: %d violation(s)" % len(checks))
    return 1


if __name__ == "__main__":
    sys.exit(main())

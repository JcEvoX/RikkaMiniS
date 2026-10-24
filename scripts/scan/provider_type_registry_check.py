#!/usr/bin/env python3
"""Gate: [ARCH-03] ProviderType metadata must be COMPILE-TIME exhaustive and
ModelsDevApi.providerKeyMap must actually derive from it.

Background — the F-212 / 1026-ISSUE-04 family (recurred 3 rounds):
`ProviderType` (data/model/ProviderConfig.kt) and the string-keyed
`ModelsDevApi.providerKeyMap` had no compile-time link, so every new vendor
silently fell out of the map:
  * F-212  — xAI / kimiCode were added to the enum and nobody added map keys;
             their 14 models fell through to the order-dependent
             all-providers scan (grok-4.5 took aihubmix's 1,000,000 context,
             kimi-k3 took a 5-tier effort ladder from greenpt).
  * 1026-D — the fix added the short keys but missed the *displayName* keys
             ("Google Gemini" / "xAI (Grok)" / "Kimi Code") that every add
             surface actually persists.
ARCH-03 removes the failure mode entirely: `modelsDevProviderIds` and
`legacyProviderNames` are exhaustive `when (this)` properties on the enum, so
a new vendor fails COMPILATION until it is mapped, and `providerKeyMap` is the
literal table MERGED with the table derived from those properties.

This Python gate cannot run the Kotlin compiler; it locks the two things that
make the compile-time guarantee real, i.e. that the wiring is present and
cannot be quietly dropped:

  Arm 1  ProviderConfig.kt — for `modelsDevProviderIds` and
         `legacyProviderNames`:
           a) both copied property declarations exist and their `when (this)`
              bodies contain NO `else ->` branch (an `else` silently disarms
              the exhaustiveness check — present but toothless)
           b) every `ProviderType` enum constant declared in the file is
              covered by BOTH `when` bodies (this is the arm that fires the
              moment a new vendor is added; the compiler fires in parallel)
         Fail-closed: enum header or either property missing ⇒ red.

  Arm 2  ModelsDevApi.kt:
           a) imports the three ProviderType symbols
           b) `providerKeyMap`'s initializer merges the derived table
              (ProviderType.values()/entries + flatMap + modelsDevProviderIds +
              legacyProviderNames + toMap) — checked on comment-stripped text
              so prose in a KDoc cannot satisfy it
           c) the explicit literal table inside `providerKeyMap = mapOf(...)`
              still carries all ten historical keys (Anthropic / Google /
              OpenAI / OpenRouter / Antigravity / xAI / Kimi / Google Gemini /
              xAI (Grok) / Kimi Code) — the merge must be additive, and the
              literal anchors stay readable for source-text gates and humans.

Usage: python3 scripts/scan/provider_type_registry_check.py [root]
       python3 scripts/scan/provider_type_registry_check.py --root <root>
       python3 scripts/scan/provider_type_registry_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import argparse
import os
import re
import sys
import tempfile

APP = "src/android/app/src/main/java/com/rikkaminis/app"
PC_REL = APP + "/data/model/ProviderConfig.kt"
MD_REL = APP + "/provider/ModelsDevApi.kt"

ENUM_HEADER = "enum class ProviderType(val displayName: String)"
DERIVED_PROPS = ("modelsDevProviderIds", "legacyProviderNames")

MD_IMPORTS = (
    "import com.rikkaminis.app.data.model.ProviderType",
    "import com.rikkaminis.app.data.model.modelsDevProviderIds",
    "import com.rikkaminis.app.data.model.legacyProviderNames",
)
# Merge wiring tokens. The enum-iteration call accepts both spellings so the
# gate does not become a style cop.
MERGE_TOKENS = ("flatMap", "modelsDevProviderIds", "legacyProviderNames", ".toMap()")
ENUM_ITER_RE = re.compile(r"ProviderType\s*\.\s*(?:values\(\)|entries)")
# [wide-1024] A branch whose body is a statically EMPTY list disarms the
# mapping just as effectively as a missing branch: `ProviderType.xAI ->
# emptyList()` keeps the `when` exhaustive (the compiler is happy) while the
# family silently falls back to the order-dependent all-providers scan — the
# exact F-212 failure this file's docstring opens with. A vendor that truly
# has no models.dev row must be listed here, so the decision is explicit.
EMPTY_MAPPING_RE = re.compile(
    r"\b(?:emptyList|emptySet|emptyArray|listOf|setOf|arrayOf)\s*"
    r"(?:<[^<>]*>\s*)?\(\s*\)")
ALLOW_EMPTY_MAPPINGS = frozenset()
# [wide-1024] The derived key set must still carry the displayName keys: the
# add surfaces (`provider.models.add`, AddCustomModelScreen,
# ModelsCollection.add) persist `ProviderType.displayName`, and for
# Gemini / xAI / KimiCode that string differs from both `name` and the
# legacy names, so dropping it is 1026-D all over again.
DERIVED_KEYS_RE = re.compile(r"\.\s*(?:map|mapNotNull|associate)\s*\{")
BOUND_IDS_RE = re.compile(r"\bval\s+(\w+)\s*=\s*[\w.]*\.?\s*modelsDevProviderIds\b")
# Historical literal keys that must survive inside the mapOf(...) body.
BASE_KEYS = ("Anthropic", "Google", "OpenAI", "OpenRouter", "Antigravity",
             "xAI", "Kimi", "Google Gemini", "xAI (Grok)", "Kimi Code")


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


def enum_constants(src):
    """Constant names of `enum class ProviderType(...)`, or None when the
    declaration is gone (gate ROT)."""
    i = src.find(ENUM_HEADER)
    if i < 0:
        return None
    brace = src.find("{", i)
    if brace < 0:
        return None
    end = _balanced(src, brace, "{", "}")
    if end < 0:
        return None
    body = code_only(src[brace + 1:end])
    # Constants live before the first top-level `;`; member declarations
    # (`val …` / `get()` / `fun`) follow it and must not be mistaken for
    # constants.
    semi = body.find(";")
    if semi >= 0:
        body = body[:semi]
    names = []
    for raw in body.split("\n"):
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", raw)
        if m and m.group(1) not in ("get", "set", "val", "var", "fun"):
            names.append(m.group(1))
    return names or None


def when_body(src, prop):
    """Body of the `when (this) { … }` inside `val ProviderType.<prop>`, or
    None when the property (or its `when`) is absent."""
    i = src.find("val ProviderType.%s" % prop)
    if i < 0:
        return None
    w = src.find("when (this)", i)
    if w < 0:
        return None
    brace = src.find("{", w)
    if brace < 0:
        return None
    end = _balanced(src, brace, "{", "}")
    return None if end < 0 else src[brace + 1:end]


BRANCH_RE = re.compile(r"(?:ProviderType\s*\.\s*)?([A-Za-z_]\w*)\s*->")


def branch_texts(body):
    """{constant: branch body} for a `when` body.

    A branch runs from its `->` to the next branch arrow. Bodies in this file
    are single expressions, so arrow-to-arrow slicing is enough; a nested
    lambda arrow shows up as an extra (never consulted) key.
    """
    out = {}
    matches = list(BRANCH_RE.finditer(body))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        name = m.group(1)
        if name in out:          # first occurrence wins (later one is nested)
            continue
        out[name] = body[m.end():end]
    return out


def derived_body(src):
    """Body of the `.flatMap { … }` that iterates the enum inside
    `providerKeyMap`, or None when the derivation is gone."""
    code = code_only(src)
    i = code.find("providerKeyMap")
    if i < 0:
        return None
    iter_m = ENUM_ITER_RE.search(code, i)
    if not iter_m:
        return None
    fm = re.search(r"\.\s*(?:flatMap|map|mapNotNull)\s*\{", code[iter_m.end():])
    if not fm:
        return None
    brace = iter_m.end() + fm.end() - 1
    end = _balanced(code, brace, "{", "}")
    return None if end < 0 else code[brace:end]


def map_literal_text(src):
    """The `providerKeyMap = mapOf(...)` literal body (code-only)."""
    code = code_only(src)
    i = code.find("providerKeyMap")
    if i < 0:
        return None
    open_idx = code.find("mapOf(", i)
    if open_idx < 0:
        return None
    end = _balanced(code, open_idx + len("mapOf(") - 1)
    return None if end < 0 else code[open_idx:end]


def read(rel, root):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def check_arm1(src, rel):
    out = []
    constants = enum_constants(src)
    if constants is None:
        out.append((rel, 0, "1a",
                    "`%s` not found — gate ROT" % ENUM_HEADER))
        return out
    for prop in DERIVED_PROPS:
        body = when_body(src, prop)
        if body is None:
            out.append((rel, 0, "1b",
                        "`val ProviderType.%s` with a `when (this)` body not "
                        "found — the compile-time exhaustiveness contract is "
                        "gone (a new vendor would compile and silently fall out "
                        "of providerKeyMap)" % prop))
            continue
        code = code_only(body)
        if re.search(r"(?<![\w])else\s*->", code):
            out.append((rel, 0, "1c",
                        "`%s` has an `else ->` branch — the `when` is no longer "
                        "exhaustive, so adding a ProviderType silently bypasses "
                        "the mapping instead of failing compilation" % prop))
        for c in constants:
            if not re.search(r"(?:ProviderType\s*\.\s*)?\b%s\s*->" % re.escape(c), code):
                out.append((rel, 0, "1d",
                            "`%s` does not cover ProviderType.%s — a new vendor "
                            "reached the enum without its models.dev mapping "
                            "(F-212 shape)" % (prop, c)))
        # Arm 1e [wide-1024]: covering the constant is not enough — the branch
        # must yield actual ids. `ProviderType.xAI -> emptyList()` keeps the
        # `when` exhaustive and compiles, so every downstream lookup for that
        # family returns nothing and grok-4.5 falls back to the relay scan.
        branches = branch_texts(code)
        for c in constants:
            if c in ALLOW_EMPTY_MAPPINGS:
                continue
            text = branches.get(c)
            if text is None:
                continue  # already reported by 1d
            if EMPTY_MAPPING_RE.search(code_only(text)):
                out.append((rel, 0, "1e",
                            "`%s` maps ProviderType.%s to an EMPTY list — the "
                            "`when` stays exhaustive, so this compiles, and the "
                            "family silently falls out of providerKeyMap (the "
                            "F-212 / 1026 shape: grok-4.5 takes a relay's "
                            "1,000,000 context). Give it its models.dev ids, or "
                            "add it to ALLOW_EMPTY_MAPPINGS in this gate with a "
                            "reason" % (prop, c)))
    return out


def check_arm2(src, rel):
    out = []
    code = code_only(src)
    for imp in MD_IMPORTS:
        if imp not in code:
            out.append((rel, 0, "2a",
                        "missing `%s` — providerKeyMap cannot derive from the "
                        "ProviderType metadata without it" % imp))
    if not ENUM_ITER_RE.search(code):
        out.append((rel, 0, "2b",
                    "providerKeyMap does not iterate the enum "
                    "(ProviderType.values()/entries not found) — the derived "
                    "table was dropped; new vendors fall out of the key map"))
    for token in MERGE_TOKENS:
        if token not in code:
            out.append((rel, 0, "2b",
                        "providerKeyMap merge is missing `%s`" % token))
    # Arm 2d/2e [wide-1024]: the merge tokens can all be present while the
    # derived table carries the wrong KEYS or the wrong VALUES — both are
    # behaviourally identical to no derivation at all, so presence alone is not
    # a judgement.
    body = derived_body(src)
    if body is None:
        out.append((rel, 0, "2b",
                    "the enum-iterating `.flatMap { … }` of providerKeyMap is "
                    "missing — the derived table was dropped"))
    else:
        keys_m = DERIVED_KEYS_RE.search(body)
        keys_expr = body[:keys_m.start()] if keys_m else body
        if "displayName" not in keys_expr:
            out.append((rel, 0, "2d",
                        "the derived key set no longer includes "
                        "`type.displayName` — every add surface persists "
                        "displayName, so \"Google Gemini\" / \"xAI (Grok)\" / "
                        "\"Kimi Code\" rows miss the map (1026-D)"))
        if keys_m is None:
            out.append((rel, 0, "2e",
                        "the derived table never builds key→ids pairs "
                        "(`.map { key -> … }` absent) — nothing is merged"))
        else:
            pair_body = body[keys_m.end():]
            bound = BOUND_IDS_RE.findall(body)
            pairs = re.findall(r"\bto\s+([^\n}]+)", pair_body)
            if not pairs:
                out.append((rel, 0, "2e",
                            "the derived pair expression does not bind a value "
                            "with `to` — the merge yields nothing usable"))
            for value in pairs:
                value = value.strip()
                ok = ("modelsDevProviderIds" in value
                      or value.split(".")[0] in bound)
                if not ok:
                    out.append((rel, 0, "2e",
                                "the derived table maps every key to `%s` "
                                "instead of the family's ids — `val ids = "
                                "type.modelsDevProviderIds` becomes dead and all "
                                "%d vendors fall out of the key map (the "
                                "derivation is present but its result is "
                                "discarded)" % (value, len(BASE_KEYS))))
    body = map_literal_text(src)
    if body is None:
        out.append((rel, 0, "2c",
                    "`providerKeyMap = mapOf(` literal not found — gate ROT"))
    else:
        for key in BASE_KEYS:
            if ('"%s"' % key) not in body:
                out.append((rel, 0, "2c",
                            "providerKeyMap literal lost `\"%s\"` — the merge "
                            "must be additive (static/text gates and the "
                            "readability anchors depend on these literals)"
                            % key))
    return out


def run_checks(root, rels=None):
    rels = rels or {"pc": PC_REL, "md": MD_REL}
    out = []
    pc = read(rels["pc"], root)
    md = read(rels["md"], root)
    if pc is None:
        out.append((rels["pc"], 0, "1a", "file not found — gate ROT"))
    else:
        out.extend(check_arm1(pc, rels["pc"]))
    if md is None:
        out.append((rels["md"], 0, "2a", "file not found — gate ROT"))
    else:
        out.extend(check_arm2(md, rels["md"]))
    return out


# ── fixtures ───────────────────────────────────────────────────────────────
FIX_PC_OK = '''package com.rikkaminis.app.data.model

enum class ProviderType(val displayName: String) {
    anthropic("Anthropic"),
    gemini("Google Gemini"),
    openAI("OpenAI"),
    kimiCode("Kimi Code");

    val builtInModels: List<LLMModel>
        get() = when (this) {
            anthropic -> LLMModel.allAnthropic
            gemini -> LLMModel.allGemini
            openAI -> LLMModel.allOpenAI
            kimiCode -> LLMModel.allKimi
        }
}

val ProviderType.modelsDevProviderIds: List<String>
    get() = when (this) {
        ProviderType.anthropic -> listOf("anthropic")
        ProviderType.gemini -> listOf("google", "google-vertex")
        ProviderType.openAI -> listOf("openai")
        ProviderType.kimiCode -> listOf("moonshotai", "moonshotai-cn")
    }

val ProviderType.legacyProviderNames: List<String>
    get() = when (this) {
        ProviderType.anthropic -> listOf("Anthropic")
        ProviderType.gemini -> listOf("Google")
        ProviderType.openAI -> listOf("OpenAI")
        ProviderType.kimiCode -> listOf("Kimi")
    }

data class LLMModel(val id: String)
'''

# Uncovered-constant fixture: the `when` is exhaustive for the OLD enum, then a
# new vendor is added to the enum only (the exact F-212 / 2031-N shape).
FIX_PC_NEW_VENDOR = FIX_PC_OK.replace(
    '    kimiCode("Kimi Code");',
    '    kimiCode("Kimi Code"),\n    deepSeek("DeepSeek");',
).replace(
    '            kimiCode -> LLMModel.allKimi',
    '            kimiCode -> LLMModel.allKimi',
)

FIX_PC_ELSE_BRANCH = FIX_PC_OK.replace(
    'val ProviderType.legacyProviderNames: List<String>\n    get() = when (this) {\n'
    '        ProviderType.anthropic -> listOf("Anthropic")',
    'val ProviderType.legacyProviderNames: List<String>\n    get() = when (this) {\n'
    '        else -> listOf("Anthropic")',
)

FIX_MD_OK = '''package com.rikkaminis.app.provider

import com.rikkaminis.app.data.model.LLMModel
import com.rikkaminis.app.data.model.ProviderType
import com.rikkaminis.app.data.model.legacyProviderNames
import com.rikkaminis.app.data.model.modelsDevProviderIds

object ModelsDevApi {
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
    ) + ProviderType.values().flatMap { type ->
        val ids = type.modelsDevProviderIds
        (listOf(type.displayName, type.name) + type.legacyProviderNames)
            .distinct()
            .map { key -> key to ids }
    }.toMap()
}
'''

# Pre-ARCH-03 shape: literals only, no derivation.
FIX_MD_NO_DERIVE = '''package com.rikkaminis.app.provider

import com.rikkaminis.app.data.model.LLMModel

object ModelsDevApi {
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
'''

# Comment decoy: the merge tokens exist, but only inside prose/KDoc and a
# commented-out line — code must stay red.
FIX_MD_COMMENT_DECOY = FIX_MD_NO_DERIVE.replace(
    "object ModelsDevApi {",
    '''// ProviderType.values().flatMap { type -> type.modelsDevProviderIds }
// + legacyProviderNames … .toMap()
/* import com.rikkaminis.app.data.model.modelsDevProviderIds */
object ModelsDevApi {''',
)

# Literal key dropped (merge not additive).
FIX_MD_MISSING_KEY = FIX_MD_OK.replace(
    '        "Kimi" to listOf("moonshotai", "moonshotai-cn"),\n', "")

# ── wide-1024 witnesses (the exact knives that used to walk through) ────────
# W1 (Arm 1e, completeness): the `when` stays exhaustive but the family maps
# to an EMPTY list — compiles, and the family silently falls out of the map.
FIX_PC_EMPTY_MAPPING = FIX_PC_OK.replace(
    '        ProviderType.kimiCode -> listOf("moonshotai", "moonshotai-cn")',
    '        ProviderType.kimiCode -> emptyList()')

# W2 (Arm 2d, completeness): the derived key set drops `type.displayName`.
FIX_MD_NO_DISPLAYNAME = FIX_MD_OK.replace(
    "(listOf(type.displayName, type.name) + type.legacyProviderNames)",
    "(listOf(type.name) + type.legacyProviderNames)")

# W3 (Arm 2e, dataflow): the derivation is present, its RESULT is discarded —
# every key maps to `emptyList<String>()` and `val ids` goes unused.
FIX_MD_EMPTY_VALUE = FIX_MD_OK.replace(
    ".map { key -> key to ids }", ".map { key -> key to emptyList<String>() }")


def _report(checks, fixture_name, label):
    if not checks:
        print("  [OK]   %s (%s)" % (fixture_name, label))
        return True
    for rel, line, arm, msg in checks:
        print("  [FAIL] %s arm %s line %s: %s" % (fixture_name, arm, line, msg))
    return False


def selftest():
    ok = True
    rels = {"pc": "ProviderConfig.kt", "md": "ModelsDevApi.kt"}
    with tempfile.TemporaryDirectory() as tmp:
        def write(name, text):
            with open(os.path.join(tmp, rels[name]), "w", encoding="utf-8") as f:
                f.write(text)

        # 1. Clean shape ⇒ no findings at all.
        write("pc", FIX_PC_OK)
        write("md", FIX_MD_OK)
        ok &= _report(run_checks(tmp, rels), "fixed", "all arms clean")

        # 2. Legacy shape (pre-ARCH-03): enum without the metadata properties,
        #    map without the derivation — both files fail-closed.
        write("pc", FIX_PC_OK[:FIX_PC_OK.index("val ProviderType.modelsDevProviderIds")])
        write("md", FIX_MD_NO_DERIVE)
        checks = run_checks(tmp, rels)
        a1 = [c for c in checks if c[2] == "1b"]
        a2 = [c for c in checks if c[2].startswith("2")]
        if len(a1) != 2 or not a2:
            print("  [FAIL] pre-fix fixture: expected 2×arm1b + arm2 findings, got %s"
                  % [(c[2], c[3][:40]) for c in checks])
            ok = False
        else:
            print("  [OK]   pre-fix fixture trips arm1b ×2 + arm2 ×%d" % len(a2))

        # 3. New vendor in enum, not covered by either `when` ⇒ arm1d fires for
        #    BOTH properties (the F-212 shape the Kotlin compiler also rejects).
        write("pc", FIX_PC_NEW_VENDOR)
        write("md", FIX_MD_OK)
        checks = [c for c in run_checks(tmp, rels) if c[2] == "1d"]
        if len(checks) != 2 or not all("deepSeek" in c[3] for c in checks):
            print("  [FAIL] new-vendor fixture: expected 2×arm1d(deepSeek), got %s"
                  % [(c[2], c[3][:60]) for c in checks])
            ok = False
        else:
            print("  [OK]   new-vendor fixture trips arm1d on both properties")

        # 4. `else ->` disarms exhaustiveness ⇒ arm1c fires (and arm1d too).
        write("pc", FIX_PC_ELSE_BRANCH)
        checks = run_checks(tmp, rels)
        if not [c for c in checks if c[2] == "1c"]:
            print("  [FAIL] else-branch fixture did not trip arm1c")
            ok = False
        else:
            print("  [OK]   else-branch fixture trips arm1c")
        write("pc", FIX_PC_OK)

        # 5. Enum header removed ⇒ ROT.
        write("pc", FIX_PC_OK.replace(ENUM_HEADER, "enum class ProviderTypeXX(val displayName: String)"))
        checks = run_checks(tmp, rels)
        if not [c for c in checks if c[2] == "1a"]:
            print("  [FAIL] enum-absent fixture did not trip arm1a (ROT)")
            ok = False
        else:
            print("  [OK]   enum-absent fixture trips arm1a (ROT)")
        write("pc", FIX_PC_OK)

        # 6. Comment decoy: merge tokens only in comments ⇒ arm2b must fire.
        write("md", FIX_MD_COMMENT_DECOY)
        checks = [c for c in run_checks(tmp, rels) if c[2] == "2b"]
        if not checks:
            print("  [FAIL] comment-decoy fixture did not trip arm2b")
            ok = False
        else:
            print("  [OK]   comment-decoy fixture trips arm2b (%d findings)" % len(checks))

        # 7. Literal key dropped ⇒ arm2c fires while arm2b stays green
        #    (precise attribution: additive-ness is a separate property).
        write("md", FIX_MD_MISSING_KEY)
        checks = run_checks(tmp, rels)
        if not [c for c in checks if c[2] == "2c"]:
            print("  [FAIL] dropped-literal-key fixture did not trip arm2c")
            ok = False
        elif [c for c in checks if c[2] == "2b"]:
            print("  [FAIL] dropped-literal-key fixture also tripped arm2b — "
                  "arms are not independent")
            ok = False
        else:
            print("  [OK]   dropped-literal-key fixture trips arm2c alone")
        write("md", FIX_MD_OK)

        # 8. ROT control: empty root.
        with tempfile.TemporaryDirectory() as empty:
            if not run_checks(empty, rels):
                print("  [FAIL] ROT control: missing files produced no finding")
                ok = False
            else:
                print("  [OK]   ROT control: missing files reported")

        # ── wide-1024 witness arms ────────────────────────────────────────
        # W1: exhaustive `when` + EMPTY mapping (the xAI knife) ⇒ arm1e alone.
        write("pc", FIX_PC_EMPTY_MAPPING)
        write("md", FIX_MD_OK)
        checks = run_checks(tmp, rels)
        a1e = [c for c in checks if c[2] == "1e"]
        others = [c for c in checks if c[2] not in ("1e",)]
        if len(a1e) != 1 or not all("kimiCode" in c[3] for c in a1e):
            print("  [FAIL] empty-mapping witness: expected 1×arm1e(kimiCode), got %s"
                  % [(c[2], c[3][:50]) for c in checks])
            ok = False
        elif others:
            print("  [FAIL] empty-mapping witness tripped unrelated arms: %s"
                  % [(c[2], c[3][:50]) for c in others])
            ok = False
        else:
            print("  [OK]   empty-mapping witness trips arm1e alone")
        write("pc", FIX_PC_OK)

        # W2: derivation drops displayName ⇒ arm2d alone.
        write("md", FIX_MD_NO_DISPLAYNAME)
        checks = run_checks(tmp, rels)
        a2d = [c for c in checks if c[2] == "2d"]
        others = [c for c in checks if c[2] != "2d"]
        if not a2d:
            print("  [FAIL] no-displayName witness did not trip arm2d")
            ok = False
        elif others:
            print("  [FAIL] no-displayName witness tripped unrelated arms: %s"
                  % [(c[2], c[3][:50]) for c in others])
            ok = False
        else:
            print("  [OK]   no-displayName witness trips arm2d alone")

        # W3: derivation present, its value discarded ⇒ arm2e alone.
        write("md", FIX_MD_EMPTY_VALUE)
        checks = run_checks(tmp, rels)
        a2e = [c for c in checks if c[2] == "2e"]
        others = [c for c in checks if c[2] != "2e"]
        if not a2e:
            print("  [FAIL] discarded-value witness did not trip arm2e")
            ok = False
        elif others:
            print("  [FAIL] discarded-value witness tripped unrelated arms: %s"
                  % [(c[2], c[3][:50]) for c in others])
            ok = False
        else:
            print("  [OK]   discarded-value witness trips arm2e alone")
        write("md", FIX_MD_OK)

    print("PROVIDER_TYPE_REGISTRY_SELFTEST=%s" % ("PASS" if ok else "FAIL"))
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
        print("provider_type_registry_check: OK (2 arms clean)")
        return 0
    for rel, line, arm, msg in checks:
        print("%s:%s [arm %s] %s" % (rel, line, arm, msg))
    print("provider_type_registry_check: %d violation(s)" % len(checks))
    return 1


if __name__ == "__main__":
    sys.exit(main())

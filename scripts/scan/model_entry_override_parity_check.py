#!/usr/bin/env python3
"""
Gate: model entry override parity (deep-audit-1023 ISSUE-02,
branch fix/da1023-model-override-parity).

ModelEntryDetailScreen's Save used to rebuild ModelOverrides inline and got
four things wrong (all verified in pre-fix):

  1. the modality lists were rebuilt from the toggle switches ALONE, so
     "text" (both sides) and output "video" — dimensions the screen has no
     switch for — were dropped. A written override is non-null, and
     ModelEntry.model replaces the base list wholesale, so a text-out model
     lost "text" from outputModalities: isTextOutput flipped to false (model
     disappears from the new-chat default candidates) and
     isPureImageGenerator flipped to true (pinned to the Images endpoint).
  2. the "did the user change anything?" comparison ran against the RAW base
     list while the switch state is derived through normalizeModalityName() —
     suffixed bases ("image_input" from OpenRouter) "always differed" and
     forced a write on a no-op save, freezing the entry to today's metadata.
  3. `ModelOverrides(...)` was constructed positionally with the six
     screen-bound fields, so maxThinkingLevel (7th field, written by backup
     import / config restore / config RPC) defaulted to null — one Save
     wiped the user's thinking-ceiling override.

The fix moved the derivation into the pure buildModelEntryOverrides()
(ModelEntryOverrideBuilder.kt) built on previous.copy(). This gate pins the
wiring in BOTH files:

  Arm 1 — the screen must call buildModelEntryOverrides( and must NOT
          construct ModelOverrides( itself (the Reset button's
          `overrides = ModelOverrides()` is the one sanctioned blank-slate).
  Arm 2 — the builder must derive from `previous.copy(`, must normalize the
          base lists (normalizeModalityList calls), and must pass every
          previous-owning field through the copy, never a positional
          constructor.
  Arm 3 — non-toggle dimensions must be carried over from base, not rebuilt
          from the switches alone: deriveModalityList( must be fed the base
          list, and the screen's toggle init must keep deriving from the
          normalized override-or-base lists.

Usage: python3 scripts/scan/model_entry_override_parity_check.py [root]
       python3 scripts/scan/model_entry_override_parity_check.py --root <root>
       python3 scripts/scan/model_entry_override_parity_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import argparse
import os
import sys
import tempfile

APP = "src/android/app/src/main/java/com/rikkaminis/app"
ME_REL = APP + "/ui/settings/ModelEntryDetailScreen.kt"
OB_REL = APP + "/ui/settings/ModelEntryOverrideBuilder.kt"

# Arm 1 — screen wiring. Code anchors only (comments are stripped first).
ARM1_ANCHORS = (
    "buildModelEntryOverrides(",
    "previous = overrides",
)

# Arm 2 — builder derivation shape.
ARM2_ANCHORS = (
    "previous.copy(",
    "normalizeModalityList(baseModel.inputModalities)",
    "normalizeModalityList(baseModel.outputModalities)",
    "inputModalities = if (newInputs.toSet() != baseInputs.toSet()) newInputs else null",
    "outputModalities = if (newOutputs.toSet() != baseOutputs.toSet()) newOutputs else null",
)

# Forbidden inside buildModelEntryOverrides: the positional constructor that
# wipes every field this screen does not name (maxThinkingLevel & any future
# 8th field). previous.copy(...) is the only sanctioned construction.
ARM2_FORBIDDEN = ("ModelOverrides(",)

# Arm 3 — base passthrough for non-toggle dimensions.
ARM3_ANCHORS = (
    "deriveModalityList(",
    "name !in toggleNames",
    "for ((name, on) in toggles)",
)

# Forbidden: the OLD inline construction inside the save onClick. Located
# strictly between the Save MinisButton onClick start and the entry.copy(
# that follows, so the Reset button's sanctioned `overrides = ModelOverrides()`
# (outside the save block, further down the file) is not flagged.
SAVE_BLOCK_START = "onClick = {"
SAVE_BLOCK_END = "val updated = if (entry.isCustom) {"
FORBIDDEN_SAVE_SNIPPETS = (
    "val newInputs = buildList",
    "val newOutputs = buildList",
    "ModelOverrides(",
)


def code_only(text):
    """Text with line comments (//…) removed — presence checks must not be
    satisfiable from prose in a comment."""
    return "\n".join(line.split("//")[0] for line in text.split("\n"))


def function_text(src, header):
    i = src.find(header)
    if i < 0:
        return None
    brace = src.find("{", i)
    if brace < 0:
        return None
    depth = 0
    for k in range(brace, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    return None


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def read(rel, root):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def check_anchors(src, rel, body, arm, anchors, what):
    out = []
    code = code_only(body)
    for anchor in anchors:
        if anchor not in code:
            out.append((rel, 0, arm,
                        "%s is missing required code anchor `%s`" % (what, anchor)))
    return out


def save_block(src):
    """The Save button's onClick body: from the first `onClick = {` up to the
    `val updated = if (entry.isCustom) {` that follows it. ModelEntryDetailScreen
    has exactly one such pair (Save) — the Cancel action is `onClick = onBack,`
    and the Reset row's onClick (further down, in the content lambda) has no
    `val updated = if (entry.isCustom) {` marker."""
    i = src.find(SAVE_BLOCK_START)
    if i < 0:
        return None
    end = src.find(SAVE_BLOCK_END, i)
    if end < 0:
        return None
    return src[i:end + len(SAVE_BLOCK_END)]


def run_checks(root, rels=None):
    rels = rels or {"me": ME_REL, "ob": OB_REL}
    srcs = {k: read(rel, root) for k, rel in rels.items()}
    missing = [(rel, 0, "?", "file not found — gate ROT")
               for k, rel in rels.items() if srcs[k] is None]
    if missing:
        return missing
    checks = []

    # Arm 1 — screen save block wiring.
    block = save_block(srcs["me"])
    if block is None:
        checks.append((ME_REL, 0, "1", "save onClick block not found — gate ROT"))
    else:
        checks += check_anchors(srcs["me"], ME_REL, block, "1",
                                ARM1_ANCHORS, "save block")
        code = code_only(block)
        for snippet in FORBIDDEN_SAVE_SNIPPETS:
            if snippet in code:
                checks.append((ME_REL, 0, "1",
                               "save block contains forbidden inline-construction snippet `%s` "
                               "(regression of da1023 ISSUE-02: text/video drop, raw-space compare, "
                               "positional ctor wipes maxThinkingLevel)" % snippet))

    # Arm 2 — builder shape.
    fn = function_text(srcs["ob"], "fun buildModelEntryOverrides(")
    if fn is None:
        checks.append((OB_REL, 0, "2", "buildModelEntryOverrides( not found — gate ROT"))
    else:
        checks += check_anchors(srcs["ob"], OB_REL, fn, "2",
                                ARM2_ANCHORS, "buildModelEntryOverrides")
        code = code_only(fn)
        for snippet in ARM2_FORBIDDEN:
            if snippet in code:
                checks.append((OB_REL, 0, "2",
                               "buildModelEntryOverrides contains positional construction `%s` — "
                               "wipes fields the screen does not own (maxThinkingLevel)" % snippet))

    # Arm 3 — base passthrough in deriveModalityList.
    fn3 = function_text(srcs["ob"], "fun deriveModalityList(")
    if fn3 is None:
        checks.append((OB_REL, 0, "3", "deriveModalityList( not found — gate ROT"))
    else:
        checks += check_anchors(srcs["ob"], OB_REL, fn3, "3",
                                ARM3_ANCHORS, "deriveModalityList")

    return checks


# ----------------------------------------------------------------------
# Selftest fixtures — minimal Kotlin-shaped files exercising every arm.
# ----------------------------------------------------------------------
FIX_ME_OK = """@Composable
fun ModelEntryDetailScreen(instanceId: String, entryId: String, providerRepository: ProviderRepository, onBack: () -> Unit) {
    MinisButton(
        onClick = {
            val newOverrides = buildModelEntryOverrides(
                baseModel = baseModel,
                previous = overrides,
                displayName = displayName,
            )
            val updated = if (entry.isCustom) {
                entry.copy(overrides = newOverrides)
            } else {
                entry.copy(overrides = newOverrides)
            }
            providerRepository.updateEntry(updated)
            onBack()
        },
        enabled = !entry.isCustom || modelId.isNotBlank(),
    ) { Text("save") }
}
"""

FIX_ME_BAD_INLINE_LISTS = """@Composable
fun ModelEntryDetailScreen(instanceId: String, entryId: String, providerRepository: ProviderRepository, onBack: () -> Unit) {
    MinisButton(
        onClick = {
            val newInputs = buildList {
                if (imageInput) add("image")
            }
            val newOutputs = buildList {
                if (imageOutput) add("image")
            }
            val newOverrides = buildModelEntryOverrides(
                baseModel = baseModel,
                previous = overrides,
                displayName = displayName,
            )
            val updated = if (entry.isCustom) {
                entry.copy(overrides = newOverrides)
            } else {
                entry.copy(overrides = newOverrides)
            }
            providerRepository.updateEntry(updated)
            onBack()
        },
        enabled = !entry.isCustom || modelId.isNotBlank(),
    ) { Text("save") }
}
"""

FIX_ME_BAD_BARE_CTOR = """@Composable
fun ModelEntryDetailScreen(instanceId: String, entryId: String, providerRepository: ProviderRepository, onBack: () -> Unit) {
    MinisButton(
        onClick = {
            val newOverrides = ModelOverrides(
                displayName = displayName,
            )
            val updated = if (entry.isCustom) {
                entry.copy(overrides = newOverrides)
            } else {
                entry.copy(overrides = newOverrides)
            }
            providerRepository.updateEntry(updated)
            onBack()
        },
        enabled = !entry.isCustom || modelId.isNotBlank(),
    ) { Text("save") }
}
"""

FIX_OB_OK = """internal fun buildModelEntryOverrides(
    baseModel: LLMModel,
    previous: ModelOverrides,
    displayName: String,
): ModelOverrides {
    val baseInputs = normalizeModalityList(baseModel.inputModalities)
    val baseOutputs = normalizeModalityList(baseModel.outputModalities)
    val newInputs = deriveModalityList(base = baseInputs, toggles = emptyList())
    val newOutputs = deriveModalityList(base = baseOutputs, toggles = emptyList())
    return previous.copy(
        displayName = displayName,
        inputModalities = if (newInputs.toSet() != baseInputs.toSet()) newInputs else null,
        outputModalities = if (newOutputs.toSet() != baseOutputs.toSet()) newOutputs else null,
        maxThinkingLevel = previous.maxThinkingLevel,
    )
}

internal fun deriveModalityList(
    base: List<String>,
    toggles: List<Pair<String, Boolean>>,
): List<String> {
    val toggleNames = toggles.map { it.first }.toSet()
    val out = LinkedHashSet<String>()
    for ((name, on) in toggles) if (on) out.add(name)
    for (raw in base) {
        val name = raw.normalizeModalityName()
        if (name.isNotEmpty() && name !in toggleNames) out.add(name)
    }
    return out.toList()
}
"""

FIX_OB_BAD_POSITIONAL = """internal fun buildModelEntryOverrides(
    baseModel: LLMModel,
    previous: ModelOverrides,
    displayName: String,
): ModelOverrides {
    val baseInputs = normalizeModalityList(baseModel.inputModalities)
    val baseOutputs = normalizeModalityList(baseModel.outputModalities)
    val newInputs = deriveModalityList(base = baseInputs, toggles = emptyList())
    val newOutputs = deriveModalityList(base = baseOutputs, toggles = emptyList())
    return ModelOverrides(
        displayName = displayName,
        inputModalities = if (newInputs.toSet() != baseInputs.toSet()) newInputs else null,
        outputModalities = if (newOutputs.toSet() != baseOutputs.toSet()) newOutputs else null,
        maxThinkingLevel = previous.maxThinkingLevel,
    )
}

internal fun deriveModalityList(
    base: List<String>,
    toggles: List<Pair<String, Boolean>>,
): List<String> {
    val toggleNames = toggles.map { it.first }.toSet()
    val out = LinkedHashSet<String>()
    for ((name, on) in toggles) if (on) out.add(name)
    for (raw in base) {
        val name = raw.normalizeModalityName()
        if (name.isNotEmpty() && name !in toggleNames) out.add(name)
    }
    return out.toList()
}
"""

FIX_OB_BAD_RAW_COMPARE = """internal fun buildModelEntryOverrides(
    baseModel: LLMModel,
    previous: ModelOverrides,
    displayName: String,
): ModelOverrides {
    val baseInputs = baseModel.inputModalities ?: emptyList()
    val baseOutputs = baseModel.outputModalities ?: emptyList()
    val newInputs = deriveModalityList(base = baseInputs, toggles = emptyList())
    val newOutputs = deriveModalityList(base = baseOutputs, toggles = emptyList())
    return previous.copy(
        displayName = displayName,
        inputModalities = if (newInputs.toSet() != baseInputs.toSet()) newInputs else null,
        outputModalities = if (newOutputs.toSet() != baseOutputs.toSet()) newOutputs else null,
        maxThinkingLevel = previous.maxThinkingLevel,
    )
}

internal fun deriveModalityList(
    base: List<String>,
    toggles: List<Pair<String, Boolean>>,
): List<String> {
    val toggleNames = toggles.map { it.first }.toSet()
    val out = LinkedHashSet<String>()
    for ((name, on) in toggles) if (on) out.add(name)
    for (raw in base) {
        val name = raw.normalizeModalityName()
        if (name.isNotEmpty() && name !in toggleNames) out.add(name)
    }
    return out.toList()
}
"""

FIX_OB_BAD_NO_PASSTHROUGH = """internal fun buildModelEntryOverrides(
    baseModel: LLMModel,
    previous: ModelOverrides,
    displayName: String,
): ModelOverrides {
    val baseInputs = normalizeModalityList(baseModel.inputModalities)
    val baseOutputs = normalizeModalityList(baseModel.outputModalities)
    val newInputs = deriveModalityList(base = baseInputs, toggles = emptyList())
    val newOutputs = deriveModalityList(base = baseOutputs, toggles = emptyList())
    return previous.copy(
        displayName = displayName,
        inputModalities = if (newInputs.toSet() != baseInputs.toSet()) newInputs else null,
        outputModalities = if (newOutputs.toSet() != baseOutputs.toSet()) newOutputs else null,
        maxThinkingLevel = previous.maxThinkingLevel,
    )
}

internal fun deriveModalityList(
    base: List<String>,
    toggles: List<Pair<String, Boolean>>,
): List<String> {
    val toggleNames = toggles.map { it.first }.toSet()
    val out = LinkedHashSet<String>()
    for ((name, on) in toggles) if (on) out.add(name)
    return out.toList()
}
"""

FIX_OB_BAD_NO_TOGGLE_LOOP = """internal fun buildModelEntryOverrides(
    baseModel: LLMModel,
    previous: ModelOverrides,
    displayName: String,
): ModelOverrides {
    val baseInputs = normalizeModalityList(baseModel.inputModalities)
    val baseOutputs = normalizeModalityList(baseModel.outputModalities)
    val newInputs = deriveModalityList(base = baseInputs, toggles = emptyList())
    val newOutputs = deriveModalityList(base = baseOutputs, toggles = emptyList())
    return previous.copy(
        displayName = displayName,
        inputModalities = if (newInputs.toSet() != baseInputs.toSet()) newInputs else null,
        outputModalities = if (newOutputs.toSet() != baseOutputs.toSet()) newOutputs else null,
        maxThinkingLevel = previous.maxThinkingLevel,
    )
}

internal fun deriveModalityList(
    base: List<String>,
    toggles: List<Pair<String, Boolean>>,
): List<String> {
    val toggleNames = toggles.map { it.first }.toSet()
    val out = LinkedHashSet<String>()
    for (raw in base) {
        val name = raw.normalizeModalityName()
        if (name.isNotEmpty() && name !in toggleNames) out.add(name)
    }
    return out.toList()
}
"""

# (name, {rel-key: fixture}, expected-arm)
SELFTEST_CASES = [
    ("all-ok", {"me": FIX_ME_OK, "ob": FIX_OB_OK}, None),
    ("me-inline-lists", {"me": FIX_ME_BAD_INLINE_LISTS, "ob": FIX_OB_OK}, "1"),
    ("me-bare-ctor", {"me": FIX_ME_BAD_BARE_CTOR, "ob": FIX_OB_OK}, "1"),
    ("ob-positional-ctor", {"me": FIX_ME_OK, "ob": FIX_OB_BAD_POSITIONAL}, "2"),
    ("ob-raw-compare", {"me": FIX_ME_OK, "ob": FIX_OB_BAD_RAW_COMPARE}, "2"),
    ("ob-no-passthrough", {"me": FIX_ME_OK, "ob": FIX_OB_BAD_NO_PASSTHROUGH}, "3"),
    ("ob-no-toggle-loop", {"me": FIX_ME_OK, "ob": FIX_OB_BAD_NO_TOGGLE_LOOP}, "3"),
]


def selftest():
    rels = {"me": "ModelEntryDetailScreen.kt", "ob": "ModelEntryOverrideBuilder.kt"}
    ok = True
    for name, fixtures, expect_arm in SELFTEST_CASES:
        with tempfile.TemporaryDirectory() as tmp:
            for k, rel in rels.items():
                path = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(fixtures[k])
            violations = run_checks(tmp, rels=rels)
            arms = {v[2] for v in violations}
            if expect_arm is None:
                if violations:
                    ok = False
                    print("  selftest %-18s FAIL: expected clean, got arms %s" % (name, sorted(arms)))
                else:
                    print("  selftest %-18s PASS (clean)" % name)
            else:
                if arms == {expect_arm} and violations:
                    print("  selftest %-18s PASS (red on arm %s)" % (name, expect_arm))
                else:
                    ok = False
                    print("  selftest %-18s FAIL: expected red on arm %s, got arms %s" % (name, expect_arm, sorted(arms)))
    if ok:
        print("MODEL_ENTRY_OVERRIDE_SELFTEST=PASS")
    else:
        print("MODEL_ENTRY_OVERRIDE_SELFTEST=FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root_pos", nargs="?", help="repo root (positional)")
    ap.add_argument("--root", dest="root", help="repo root")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    root = args.root or args.root_pos
    if not root:
        print("usage: model_entry_override_parity_check.py [root] | --root <root> | --selftest")
        return 2
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print("not a directory: %s" % root)
        return 2
    violations = run_checks(root)
    if violations:
        print("model_entry_override_parity_check: %d violation(s)" % len(violations))
        for rel, line, arm, msg in violations:
            print("  [%s] %s:%d %s" % (arm, rel, line, msg))
        return 1
    print("model_entry_override_parity_check: OK (3 arms, 0 violations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Scan self-test — verify the scanners themselves catch what they claim to catch.

Each scanner gets three checks:
  1. CLEAN fixture  → must exit 0
  2. DIRTY fixture  → must exit 1 (the violation class it was built for)
  3. REAL repo tree → must exit 0 (scanners are green in CI today)

Why: a scanner that silently regresses (a regex stops matching after a
refactor, a path moves) fails OPEN — CI stays green while the exact bug class
the scanner was built for (GH#68, P0-pinned-providers, enum crash-on-read…)
walks back in. These fixtures are the ground truth: known-bad trees are
asserted to be caught, so a broken scanner fails LOUD here instead of failing
silent in every future build.

Usage: python3 scripts/scan/test_scan.py [repo_root]
Exit:  0 = all scanners pass all cases, 1 = any failure
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(
    sys.argv[1] if len(sys.argv) > 1 else os.path.join(SCRIPT_DIR, "..", "..")
)

# Relative roots the scanners expect inside a repo tree.
KOTLIN_APP = "src/android/app/src/main/java"
KOTLIN_PKG = os.path.join(KOTLIN_APP, "com/rikkaminis/app")
RES_VALUES = "src/android/app/src/main/res/values"

PASS = 0
FAIL = 0


def run_scanner(script, root, *extra):
    """Run one scanner against root; return (exit_code, combined_output)."""
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, script), root, *extra],
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def make_tree(files):
    """files: {relative_path: content} → tempdir repo root."""
    root = tempfile.mkdtemp(prefix="scan-selftest-")
    for rel, content in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    return root


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")
        if detail:
            print(f"     {detail}")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

FW_MODEL = """package com.rikkaminis.app.data.model

data class ProviderInstance(
    val id: String,
    val label: String,
    val apiKey: String,
)
"""

FW_ENTITY = """package com.rikkaminis.app.data.db

data class ProviderInstanceEntity(
    val id: String,
    val label: String,
    val apiKey: String,
)
"""

FW_MAPPING = """package com.rikkaminis.app.data.db

fun ProviderConfig.toSnapshot(): ProviderConfigSnapshot {
    val instanceRows = instances.mapIndexed { idx, inst ->
        ProviderInstanceEntity(
            id = inst.id,
            label = inst.label,
            apiKey = inst.apiKey,
        )
    }
    return ProviderConfigSnapshot(instanceRows)
}

fun ProviderConfigSnapshot.toProviderConfig(): ProviderConfig {
    val instances = this.instances.map { row ->
        ProviderInstance(
            id = row.id,
            label = row.label,
            apiKey = row.apiKey,
        )
    }.toMutableList()
    return ProviderConfig(instances = instances)
}
"""

# Minimal stubs for the files the scanner reads unconditionally for the
# (inert in this fixture) ModelGroup group.
FW_GROUP_ENTITY_STUB = (
    "package com.rikkaminis.app.data.db\n"
    "// ModelGroup intentionally absent — exercises the parse-error-continue path.\n"
)


def four_way_fixture(model_extra="", entity_extra="", snap_extra="", config_extra=""):
    """Build a four-way-sync fixture with optional per-layer extra fields."""
    model = FW_MODEL.replace("    val apiKey: String,\n", "    val apiKey: String,\n" + model_extra)
    entity = FW_ENTITY.replace(
        "    val apiKey: String,\n", "    val apiKey: String,\n" + entity_extra
    )
    snap_args = "            apiKey = inst.apiKey,\n" + snap_extra
    config_args = "            apiKey = row.apiKey,\n" + config_extra
    mapping = FW_MAPPING.replace("            apiKey = inst.apiKey,\n", snap_args)
    mapping = mapping.replace("            apiKey = row.apiKey,\n", config_args)
    return {
        os.path.join(KOTLIN_PKG, "data/model/ProviderConfig.kt"): model,
        os.path.join(KOTLIN_PKG, "data/db/ProviderInstanceEntity.kt"): entity,
        os.path.join(KOTLIN_PKG, "data/db/ProviderModelGroupEntity.kt"): FW_GROUP_ENTITY_STUB,
        os.path.join(KOTLIN_PKG, "data/db/ProviderConfigMapping.kt"): mapping,
    }


# ── v2 derivation fixtures: families beyond the v1 hardcode ───────────────

def derived_widget_fixture(model_extra=""):
    """A family v1 never knew: WidgetEntity ↔ Widget — derivation must pick
    it up from data/db/ alone (no SYNC_GROUPS entry, no alias pin)."""
    model = (
        "package com.rikkaminis.app.data.model\n"
        "data class Widget(\n"
        "    val id: String,\n"
        "    val name: String,\n"
        + model_extra +
        ")\n"
    )
    entity = (
        "package com.rikkaminis.app.data.db\n"
        "data class WidgetEntity(\n"
        "    val id: String,\n"
        "    val name: String,\n"
        ")\n"
    )
    mapping = (
        "package com.rikkaminis.app.data.db\n"
        "import com.rikkaminis.app.data.model.Widget\n"
        "fun toSnapshot(w: Widget): WidgetEntity =\n"
        "    WidgetEntity(\n"
        "        id = w.id,\n"
        "        name = w.name,\n"
        "    )\n"
        "fun toProviderConfig(e: WidgetEntity): Widget =\n"
        "    Widget(\n"
        "        id = e.id,\n"
        "        name = e.name,\n"
        "    )\n"
    )
    return {
        os.path.join(KOTLIN_PKG, "data/model/Widget.kt"): model,
        os.path.join(KOTLIN_PKG, "data/db/WidgetEntity.kt"): entity,
        os.path.join(KOTLIN_PKG, "data/db/WidgetMapping.kt"): mapping,
    }


def derived_alias_fixture():
    """ModelEntry family: model/ctor arg names differ from entity columns by
    the real FIELD_ALIASES (uuid→id, baseModel→baseModelJson,
    overrides→overridesJson) — alias normalization must keep it GREEN."""
    model = (
        "package com.rikkaminis.app.data.model\n"
        "data class ModelEntry(\n"
        "    val uuid: String,\n"
        "    val baseModel: String,\n"
        "    val overrides: Int,\n"
        "    val isCustom: Boolean,\n"
        ")\n"
    )
    entity = (
        "package com.rikkaminis.app.data.db\n"
        "data class ProviderModelEntryEntity(\n"
        "    val id: String,\n"
        "    val baseModelJson: String,\n"
        "    val overridesJson: Int,\n"
        "    val isCustom: Boolean,\n"
        ")\n"
    )
    mapping = (
        "package com.rikkaminis.app.data.db\n"
        "fun toSnapshot(e: ModelEntry): ProviderModelEntryEntity =\n"
        "    ProviderModelEntryEntity(\n"
        "        id = e.uuid,\n"
        "        baseModelJson = e.baseModel,\n"
        "        overridesJson = e.overrides,\n"
        "        isCustom = e.isCustom,\n"
        "    )\n"
        "fun toProviderConfig(e: ProviderModelEntryEntity): ModelEntry =\n"
        "    ModelEntry(\n"
        "        uuid = e.id,\n"
        "        baseModel = e.baseModelJson,\n"
        "        overrides = e.overridesJson,\n"
        "        isCustom = e.isCustom,\n"
        "    )\n"
    )
    return {
        os.path.join(KOTLIN_PKG, "data/model/ModelEntry.kt"): model,
        os.path.join(KOTLIN_PKG, "data/db/ProviderModelEntryEntity.kt"): entity,
        os.path.join(KOTLIN_PKG, "data/db/ProviderModelEntryMapping.kt"): mapping,
    }


def derived_orphan_fixture():
    """An entity whose model class doesn't exist under KOTLIN — must land in
    UNRESOLVED (visible) and NOT fail the run."""
    orphan = (
        "package com.rikkaminis.app.data.db\n"
        "data class OrphanEntity(\n"
        "    val id: String,\n"
        ")\n"
    )
    return {os.path.join(KOTLIN_PKG, "data/db/OrphanEntity.kt"): orphan}


def derived_pinned_fixture():
    """ThinkingRule family: kind/scope/wireFormat/reasoningEcho have no 1:1
    entity column by design — KNOWN_MISMATCH pins must print WARNING lines
    and keep the run GREEN (exit 0)."""
    model = (
        "package com.rikkaminis.app.provider.thinking\n"
        "data class ThinkingRule(\n"
        "    val id: String,\n"
        "    val label: String,\n"
        "    val kind: String,\n"
        "    val scope: String,\n"
        "    val wireFormat: String,\n"
        "    val reasoningEcho: String,\n"
        ")\n"
    )
    entity = (
        "package com.rikkaminis.app.data.db\n"
        "data class ProviderThinkingRuleEntity(\n"
        "    val id: String,\n"
        "    val label: String,\n"
        "    val scopeKind: String,\n"
        "    val scopePattern: String,\n"
        "    val wireFormatJson: String,\n"
        "    val reasoningEchoJson: String,\n"
        "    val sortOrder: Int,\n"
        ")\n"
    )
    mapping = (
        "package com.rikkaminis.app.provider.thinking\n"
        "fun toEntity(r: ThinkingRule): ProviderThinkingRuleEntity =\n"
        "    ProviderThinkingRuleEntity(\n"
        "        id = r.id,\n"
        "        label = r.label,\n"
        "        scopeKind = r.kind,\n"
        "        scopePattern = r.scope,\n"
        "        wireFormatJson = encodeWireFormat(r.wireFormat),\n"
        "        reasoningEchoJson = encodeEcho(r.reasoningEcho),\n"
        "        sortOrder = 0,\n"
        "    )\n"
        "fun toRule(e: ProviderThinkingRuleEntity): ThinkingRule =\n"
        "    ThinkingRule(\n"
        "        id = e.id,\n"
        "        label = e.label,\n"
        "        kind = e.scopeKind,\n"
        "        scope = e.scopePattern,\n"
        "        wireFormat = decodeWireFormat(e.wireFormatJson),\n"
        "        reasoningEcho = decodeEcho(e.reasoningEchoJson),\n"
        "    )\n"
    )
    return {
        os.path.join(KOTLIN_PKG, "provider/thinking/ThinkingRule.kt"): model,
        os.path.join(KOTLIN_PKG, "data/db/ProviderThinkingRuleEntity.kt"): entity,
        os.path.join(KOTLIN_PKG, "provider/thinking/ThinkingRuleCoding.kt"): mapping,
    }


I18N_CLEAN_KT = (
    "package com.rikkaminis.app\n"
    "val title = R.string.app_name\n"
)

I18N_DIRTY_KT = (
    "package com.rikkaminis.app\n"
    "val title = R.string.app_name\n"
    "val orphan = R.string.never_defined_key\n"
)

I18N_STRINGS = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<resources>\n"
    '    <string name="app_name">Minis</string>\n'
    "</resources>\n"
)


def enum_fixture(dirty=False):
    guarded = (
        "package com.rikkaminis.app\n"
        "fun a(s: String) {\n"
        "    val x = runCatching { Foo.valueOf(s) }.getOrNull()\n"
        "}\n"
        "fun b(s: String) {\n"
        "    try {\n"
        "        val y = Bar.valueOf(s)\n"
        "    } catch (e: IllegalArgumentException) {\n"
        "        return\n"
        "    }\n"
        "}\n"
    )
    bare = (
        "package com.rikkaminis.app\n"
        "fun c(s: String) {\n"
        "    val z = Baz.valueOf(s)\n"
        "}\n" if dirty else ""
    )
    return {
        os.path.join(KOTLIN_PKG, "EnumUsers.kt"): guarded + bare,
    }


def boundary_fixture(dirty=False):
    worker = (
        "package com.rikkaminis.app.sandbox.offload\n"
        "class ModelExecutionService {\n"
        "    fun run() {\n"
        "        provider.streamMessage(request) {}\n"
        "        provider.sendMessage(request) {}\n"
        "    }\n"
        "}\n"
    )
    caller = (
        "package com.rikkaminis.app.ui.chat\n"
        "class ChatViewModel {\n"
        "    fun send() {\n"
        "        viewModel.sendMessage(msg)\n"
        "        // provider.sendMessage(commented) — comments are skipped\n"
        + ("        provider.sendMessage(userMessage)\n" if dirty else "")
        + "    }\n"
        "}\n"
    )
    return {
        os.path.join(KOTLIN_PKG, "sandbox/offload/ModelExecutionService.kt"): worker,
        os.path.join(KOTLIN_PKG, "ui/chat/ChatViewModel.kt"): caller,
    }


def legacy_fixture(dirty=False, escaped=False):
    """The 2026-09-13 incident class: a live file reaching into the dead
    pre-aggregate pipeline (cold-open prewarm's always-empty source list)."""
    legacy = (
        "package com.rikkaminis.app.ui.chat.legacy\n"
        "internal class StableChatRowLedger {\n"
        "    fun record() {}\n"
        "}\n"
    )
    body = "        val ledger: Any? = null\n"
    if dirty:
        body = "        StableChatRowLedger().record()\n"
    elif escaped:
        body = (
            "        // legacy-ok: single-point source function for both pipelines\n"
            "        StableChatRowLedger().record()\n"
            "\n"
            "        val unrelated = 1\n"
        )
    consumer = (
        "package com.rikkaminis.app.ui.chat\n"
        "class ChatViewModel {\n"
        "    fun send() {\n"
        "        // StableChatRowLedger mentioned in a comment — comments are skipped\n"
        + body
        + "    }\n"
        "}\n"
    )
    return {
        os.path.join(KOTLIN_PKG, "ui/chat/legacy/StableChatRowLedger.kt"): legacy,
        os.path.join(KOTLIN_PKG, "ui/chat/ChatViewModel.kt"): consumer,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cases
# ─────────────────────────────────────────────────────────────────────────────

def test_four_way():
    print("━━━ four_way_sync_check ━━━")
    # Clean — all four layers agree.
    root = make_tree(four_way_fixture())
    code, out = run_scanner("four_way_sync_check.py", root)
    check("clean fixture exits 0", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

    # Dirty A — model field missing in Entity (the GH#68 class: saved nowhere).
    root = make_tree(four_way_fixture(model_extra="    val pinned: Boolean = false,\n"))
    code, out = run_scanner("four_way_sync_check.py", root)
    check(
        "model-extra-field caught (exit 1, 'Entity missing')",
        code == 1 and "Entity missing" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # Dirty B — Entity field not passed in toSnapshot (lost on every save).
    root = make_tree(four_way_fixture(entity_extra="    val pinned: Int = 0,\n"))
    code, out = run_scanner("four_way_sync_check.py", root)
    check(
        "toSnapshot-gap caught (exit 1, 'toSnapshot missing')",
        code == 1 and "toSnapshot missing" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # Dirty C — model field not restored in toProviderConfig (lost on load).
    root = make_tree(
        four_way_fixture(
            model_extra="    val pinned: Boolean = false,\n",
            entity_extra="    val pinned: Int = 0,\n",
            snap_extra="            pinned = if (inst.pinned) 1 else 0,\n",
        )
    )
    code, out = run_scanner("four_way_sync_check.py", root)
    check(
        "toProviderConfig-gap caught (exit 1, 'toProviderConfig missing')",
        code == 1 and "toProviderConfig missing" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)


def test_four_way_derived():
    print("━━━ four_way_sync_check v2 (derived families) ━━━")

    # (a) Non-hardcoded family with a single-side extra field → must be RED.
    root = make_tree(derived_widget_fixture(model_extra="    val pinned: Boolean = false,\n"))
    code, out = run_scanner("four_way_sync_check.py", root)
    check(
        "derived non-hardcoded family caught (exit 1, 'Entity missing')",
        code == 1 and "Entity missing" in out and "Widget" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # (b) Alias normalization: model/ctor arg names differ from entity
    # columns by the real FIELD_ALIASES → must NOT be flagged (GREEN).
    root = make_tree(derived_alias_fixture())
    code, out = run_scanner("four_way_sync_check.py", root)
    check(
        "alias-normalized derived family green (exit 0)",
        code == 0 and "ModelEntry — four-way sync check" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # (c) Entity with no model class → UNRESOLVED (visible), exit 0.
    root = make_tree(derived_orphan_fixture())
    code, out = run_scanner("four_way_sync_check.py", root)
    check(
        "orphan entity → UNRESOLVED + exit 0",
        code == 0 and "UNRESOLVED" in out and "OrphanEntity" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # (d) KNOWN_MISMATCH pin: pinned fields → WARNING lines + exit 0.
    root = make_tree(derived_pinned_fixture())
    code, out = run_scanner("four_way_sync_check.py", root)
    warns = out.count("KNOWN-MISMATCH (pinned, by design)")
    check(
        "KNOWN_MISMATCH pin → WARNING lines + exit 0",
        code == 0 and warns >= 4 and "ThinkingRule — four-way sync check" in out,
        f"exit={code} warns={warns}\n{out}",
    )
    shutil.rmtree(root)


# ── v2.1 entity-self fixtures: positional branch + defaulted regex ────────
# [review G2] The positional branch reads defaulted fields via
# extract_defaulted_fields; a bare `[^=]*` ran across lines and swallowed the
# NEXT field's `=`, mis-flagging the previous field ('key' instead of 'value').

POSITIONAL_CTRL = (
    'package com.rikkaminis.app.data.db\n'
    'data class ProviderConfigMetaEntity(\n'
    '    val key: String,\n'
    '    val value: String,\n'
    ')\n'
)
POSITIONAL_DEFT = (
    'package com.rikkaminis.app.data.db\n'
    'data class ProviderConfigMetaEntity(\n'
    '    val key: String,\n'
    '    val value: String = "x",\n'
    ')\n'
)
POSITIONAL_CALL = (
    'package com.rikkaminis.app.data.repository\n'
    'import com.rikkaminis.app.data.db.ProviderConfigMetaEntity\n'
    'fun w() = ProviderConfigMetaEntity("k")\n'
)


def test_four_way_entity_self():
    print("━━━ four_way_sync_check v2.1 (entity-self positional branch) ━━━")

    # (a) Positional entity, no defaults → GREEN.
    root = make_tree({
        os.path.join(KOTLIN_PKG, "data/db/ProviderConfigMetaEntity.kt"): POSITIONAL_CTRL,
        os.path.join(KOTLIN_PKG, "data/repository/W.kt"): POSITIONAL_CALL,
    })
    code, out = run_scanner("four_way_sync_check.py", root)
    check(
        "entity-self positional no-default green (exit 0)",
        code == 0,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # (b) Positional entity with a defaulted field → RED naming the defaulted
    # column ('value'; the old cross-line regex mis-flagged 'key').
    root = make_tree({
        os.path.join(KOTLIN_PKG, "data/db/ProviderConfigMetaEntity.kt"): POSITIONAL_DEFT,
        os.path.join(KOTLIN_PKG, "data/repository/W.kt"): POSITIONAL_CALL,
    })
    code, out = run_scanner("four_way_sync_check.py", root)
    check(
        "entity-self defaulted field caught (exit 1, names 'value')",
        code == 1 and "['value']" in out and "['key']" not in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)


def test_i18n():
    print("━━━ i18n_check ━━━")
    files = {
        os.path.join(RES_VALUES, "strings.xml"): I18N_STRINGS,
        os.path.join(KOTLIN_PKG, "Ui.kt"): I18N_CLEAN_KT,
    }
    root = make_tree(files)
    code, out = run_scanner("i18n_check.py", root)
    check("clean fixture exits 0", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

    files[os.path.join(KOTLIN_PKG, "Ui.kt")] = I18N_DIRTY_KT
    root = make_tree(files)
    code, out = run_scanner("i18n_check.py", root)
    check(
        "orphan R.string key caught (exit 1)",
        code == 1 and "never_defined_key" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # Missing translation is WARNING-only (legacy locales) — must not fail.
    files[os.path.join(KOTLIN_PKG, "Ui.kt")] = I18N_CLEAN_KT
    files[os.path.join(os.path.dirname(RES_VALUES), "values-de", "strings.xml")] = (
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n</resources>\n'
    )
    root = make_tree(files)
    code, out = run_scanner("i18n_check.py", root)
    check(
        "missing translation stays warning-only (exit 0)",
        code == 0 and ("WARNING" in out or "warn" in out.lower()),
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # Split resource files: Android allows several strings*.xml per res folder
    # and this repo uses one (values/strings_db_guard.xml) so the
    # database-downgrade guidance strings do not have to touch the shared
    # strings.xml. A scanner that only reads strings.xml calls those keys
    # orphans and fails the build for resources that exist.
    files[os.path.join(KOTLIN_PKG, "Ui.kt")] = (
        "package com.rikkaminis.app\n"
        "val title = R.string.app_name\n"
        "val hint = R.string.newer_db_title\n"
    )
    files[os.path.join(RES_VALUES, "strings_db_guard.xml")] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<resources>\n"
        '    <string name="newer_db_title">Your data is from a newer version</string>\n'
        "</resources>\n"
    )
    root = make_tree(files)
    code, out = run_scanner("i18n_check.py", root)
    check(
        "key defined in a split strings file is NOT an orphan (exit 0)",
        code == 0,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)


def test_enum_parse():
    print("━━━ enum_parse_safety_check ━━━")
    root = make_tree(enum_fixture(dirty=False))
    code, out = run_scanner("enum_parse_safety_check.py", root)
    check(
        "guarded valueOf (runCatching + multi-line try) exits 0",
        code == 0,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    root = make_tree(enum_fixture(dirty=True))
    code, out = run_scanner("enum_parse_safety_check.py", root)
    check(
        "bare valueOf caught (exit 1)",
        code == 1 and "Baz.valueOf" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)


def test_boundary():
    print("━━━ provider_boundary_guard ━━━")
    root = make_tree(boundary_fixture(dirty=False))
    code, out = run_scanner("provider_boundary_guard.py", root)
    check(
        "worker calls + comment lines are legal (exit 0)",
        code == 0,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    root = make_tree(boundary_fixture(dirty=True))
    code, out = run_scanner("provider_boundary_guard.py", root)
    check(
        "app-process provider.sendMessage caught (exit 1)",
        code == 1 and "ChatViewModel.kt" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)


def test_legacy():
    print("━━━ legacy_pipeline_guard ━━━")
    # Clean — the dead symbol is only mentioned in a comment.
    root = make_tree(legacy_fixture())
    code, out = run_scanner("legacy_pipeline_guard.py", root)
    check(
        "comment-only mention is legal (exit 0)",
        code == 0,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # Dirty — a live file consumes the runtime-dead symbol (fails silently in
    # production; must fail loudly here).
    root = make_tree(legacy_fixture(dirty=True))
    code, out = run_scanner("legacy_pipeline_guard.py", root)
    check(
        "live caller of legacy symbol caught (exit 1)",
        code == 1 and "ChatViewModel.kt" in out and "StableChatRowLedger" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # Escape hatch — `legacy-ok:` justifies the following block (and the
    # justification must survive comment stripping; a blank line ends it).
    root = make_tree(legacy_fixture(escaped=True))
    code, out = run_scanner("legacy_pipeline_guard.py", root)
    check(
        "legacy-ok escape hatch exempts the block (exit 0)",
        code == 0,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)


def test_debug_leak():
    print("━━━ debug_leak_guard (source mode) ━━━")
    debug_cls = (
        "package com.rikkaminis.app.debug\n"
        "class DebugServer\n"
    )
    # Clean — the debug class is allow-listed (MinisApp) / comment-only.
    clean = {
        os.path.join(KOTLIN_PKG, "debug/DebugServer.kt"): debug_cls,
        os.path.join(KOTLIN_PKG, "MinisApp.kt"):
            "if (BuildConfig.DEBUG) { com.rikkaminis.app.debug.DebugServer(this).start() }\n",
        os.path.join(KOTLIN_PKG, "Other.kt"):
            "// mentions com.rikkaminis.app.debug.DebugServer in a comment only\n",
    }
    root = make_tree(clean)
    code, out = run_scanner("debug_leak_guard.py", root)
    check("allow-listed call site + comment-only mention is legal (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

    # Dirty — a NEW unaudited file touches debug code (the M1 failure class:
    # ships the token-free loopback debug server into release with no other
    # gate noticing).
    dirty = dict(clean)
    dirty[os.path.join(KOTLIN_PKG, "SomeNewFile.kt")] = (
        "val x = com.rikkaminis.app.debug.DebugServer()\n"
    )
    root = make_tree(dirty)
    code, out = run_scanner("debug_leak_guard.py", root)
    check(
        "unaudited debug-package call site caught (exit 1)",
        code == 1 and "SomeNewFile.kt" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # Escape hatch — `debug-ok:` justifies the reference.
    escaped = dict(clean)
    escaped[os.path.join(KOTLIN_PKG, "SomeNewFile.kt")] = (
        "// debug-ok: audited single-point entry, guarded at the caller\n"
        "val x = com.rikkaminis.app.debug.DebugServer()\n"
    )
    root = make_tree(escaped)
    code, out = run_scanner("debug_leak_guard.py", root)
    check("debug-ok escape hatch exempts the reference (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

def test_room_migration():
    print("━━━ room_migration_check ━━━")
    code, out = run_scanner("room_migration_check.py", "--self-test")
    check(
        "self-test fixtures pass (unwired + non-contiguous caught, clean passes)",
        code == 0,
        f"exit={code}\n{out}",
    )

def test_process_logging():
    print("━━━ process_logging_gate ━━━")
    code, out = run_scanner("process_logging_gate.py", "--self-test")
    check(
        "self-test fixtures pass (unwired process caught, wired/escaped pass)",
        code == 0,
        f"exit={code}\n{out}",
    )
    # Real-tree shape: a process declared with the escape comment ABOVE the
    # tag (XML forbids comments inside a start tag) must still pass.
    root = make_tree({
        "src/android/app/src/main/AndroidManifest.xml": (
            "<manifest>\n"
            "  <!-- logging-ok: dormant, no request path -->\n"
            '  <service android:process=":sleepyservice" />\n'
            "</manifest>\n"
        ),
    })
    code, out = run_scanner("process_logging_gate.py", root)
    check("escape comment above the tag passes (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)


def test_trace_eval():
    print("━━━ trace_eval_check ━━━")
    # The evaluator gates every golden in tests/traces/golden/, including the
    # fault goldens — a silently broken assertion there would let a removed
    # guard ship green. Fixtures below are the ground truth for its checks.

    clean_case = {"tools": ["file_write"], "terminal_state": "Succeeded", "all_tools_succeed": True}
    clean_lines = [
        {"type": "trace_start"},
        {"type": "turn_start", "turn": 0},
        {"type": "tool_call", "turn": 0, "tool": "file_write"},
        {"type": "tool_result", "turn": 0, "tool": "file_write", "success": True, "output": "ok"},
        {"type": "trace_end", "terminal_state": "Succeeded", "terminal_reason": "completed_normally"},
    ]
    root = make_tree({
        "tests/traces/golden/case.json": json.dumps({"trace": "tests/traces/golden/traces/t.jsonl", "expect": clean_case}),
        "tests/traces/golden/traces/t.jsonl": "".join(json.dumps(l) + "\n" for l in clean_lines),
    })
    code, out = run_scanner("trace_eval_check.py", root)
    check("clean golden exits 0", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

    # Dirty A — tool sequence drift (the truncated-call regression shape:
    # the refused call executed after all, so the sequence grew by one).
    lines = clean_lines[:2] + [
        {"type": "tool_call", "turn": 0, "tool": "file_write"},
        {"type": "tool_result", "turn": 0, "tool": "file_write", "success": False, "output": "invalid JSON"},
    ] + clean_lines[2:]
    root = make_tree({
        "tests/traces/golden/case.json": json.dumps({"trace": "tests/traces/golden/traces/t.jsonl", "expect": clean_case}),
        "tests/traces/golden/traces/t.jsonl": "".join(json.dumps(l) + "\n" for l in lines),
    })
    code, out = run_scanner("trace_eval_check.py", root)
    check(
        "tool-sequence drift caught (exit 1)",
        code == 1 and "tool sequence mismatch" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # Dirty B — some_tool_failed not honoured: every result succeeded, i.e. the
    # offload guard was removed and the stub payload got written.
    all_ok = clean_lines
    root = make_tree({
        "tests/traces/golden/case.json": json.dumps(
            {"trace": "tests/traces/golden/traces/t.jsonl", "expect": {"some_tool_failed": True}}
        ),
        "tests/traces/golden/traces/t.jsonl": "".join(json.dumps(l) + "\n" for l in all_ok),
    })
    code, out = run_scanner("trace_eval_check.py", root)
    check(
        "some_tool_failed with all-succeeded results caught (exit 1)",
        code == 1 and "every tool_result succeeded" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # ...and the positive direction of the same assertion.
    with_failure = clean_lines[:-1] + [
        {"type": "tool_result", "turn": 0, "tool": "file_write", "success": False, "output": "refused"},
    ] + clean_lines[-1:]
    root = make_tree({
        "tests/traces/golden/case.json": json.dumps(
            {"trace": "tests/traces/golden/traces/t.jsonl", "expect": {"some_tool_failed": True}}
        ),
        "tests/traces/golden/traces/t.jsonl": "".join(json.dumps(l) + "\n" for l in with_failure),
    })
    code, out = run_scanner("trace_eval_check.py", root)
    check("some_tool_failed satisfied by a failed result (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

    # Dirty C — run never finalized.
    no_end = [l for l in clean_lines if l["type"] != "trace_end"]
    root = make_tree({
        "tests/traces/golden/case.json": json.dumps({"trace": "tests/traces/golden/traces/t.jsonl", "expect": clean_case}),
        "tests/traces/golden/traces/t.jsonl": "".join(json.dumps(l) + "\n" for l in no_end),
    })
    code, out = run_scanner("trace_eval_check.py", root)
    check(
        "missing trace_end caught (exit 1)",
        code == 1 and "no trace_end event" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # Dirty D — golden pointing at a trace that does not exist (a renamed or
    # never-committed .jsonl must fail loudly, not silently skip).
    root = make_tree({
        "tests/traces/golden/case.json": json.dumps(
            {"trace": "tests/traces/golden/traces/missing.jsonl", "expect": clean_case}
        )
    })
    code, out = run_scanner("trace_eval_check.py", root)
    check(
        "missing trace file caught (exit 1)",
        code == 1 and "trace file missing" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)


def test_extra_days_container():
    print("━━━ extra_days_container_guard ━━━")
    # Ground truth: the guard must accept arrayListOf and reject BOTH wrong
    # containers -- int[] and Kotlin's listOf() (java.util.Arrays$ArrayList).
    # The defect it catches is silent: the framework turns the ClassCastException
    # into a null and the alarm replays as a one-shot.
    good = make_tree({
        "src/MinisApp.kt": (
            "class M {\n"
            "    private fun daysForRepeatMode(r: String): ArrayList<Int>? =\n"
            "        when (r) { \"DAILY\" -> arrayListOf(1, 2, 3, 4, 5, 6, 7); else -> null }\n"
            "    fun f() { daysForRepeatMode(\"DAILY\")?.let {\n"
            "        putExtra(android.provider.AlarmClock.EXTRA_DAYS, it) } }\n"
            "}\n"
        ),
        "src/Handler.kt": (
            "class H {\n"
            "    fun g(m: Int) { when (m) {\n"
            "        1 -> putExtra(AlarmClock.EXTRA_DAYS, arrayListOf(1, 2))\n"
            "        else -> putExtra(AlarmClock.EXTRA_DAYS, arrayListOf(1, 2))\n"
            "    } }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("extra_days_container_guard.py", good)
    check("ArrayList writers pass (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(good)

    for label, ret, value in (
        ("int[]", "IntArray?", "intArrayOf(1, 2, 3, 4, 5, 6, 7)"),
        ("listOf", "List<Int>?", "listOf(1, 2, 3, 4, 5, 6, 7)"),
    ):
        bad = make_tree({
            "src/MinisApp.kt": (
                "class M {\n"
                f"    private fun daysForRepeatMode(r: String): {ret} =\n"
                f"        when (r) {{ \"DAILY\" -> {value}; else -> null }}\n"
                "    fun f() { daysForRepeatMode(\"DAILY\")?.let {\n"
                "        putExtra(android.provider.AlarmClock.EXTRA_DAYS, it) } }\n"
                "}\n"
            ),
            "src/Handler.kt": (
                "class H {\n"
                "    fun g() { putExtra(AlarmClock.EXTRA_DAYS, arrayListOf(1, 2)) }\n"
                "}\n"
            ),
        })
        code, out = run_scanner("extra_days_container_guard.py", bad)
        check(f"{label} writer is caught (exit 1)", code == 1, f"exit={code}\n{out}")
        shutil.rmtree(bad)

def test_stale_closure():
    print("━━━ stale_closure_guard ━━━")
    # Ground truth: the guard must accept rememberUpdatedState and reject a
    # long-lived produceState whose closure reads a plain parameter while that
    # parameter is NOT in the key list. The defect it catches is silent: the
    # producer is never restarted, `snapshotFlow { plainParam }` observes
    # nothing, and the UI freezes at the first composed frame (2026-09-20).
    good = make_tree({
        KOTLIN_PKG + "/ui/Good.kt": (
            "@Composable\n"
            "private fun Good(content: String, isStreaming: Boolean) {\n"
            "    val latestContent by rememberUpdatedState(content)\n"
            "    val d by produceState(initialValue = content, isStreaming) {\n"
            "        snapshotFlow { latestContent }.collect { value = it }\n"
            "    }\n"
            "}\n"
        ),
        # key 里含 content ⇒ 每次变化都重启闭包 ⇒ 安全
        KOTLIN_PKG + "/ui/Keyed.kt": (
            "@Composable\n"
            "private fun Keyed(content: String) {\n"
            "    val d by produceState(initialValue = content, content) {\n"
            "        snapshotFlow { content }.collect { value = it }\n"
            "    }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("stale_closure_guard.py", good)
    check("rememberUpdatedState + keyed param pass (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(good)

    bad = make_tree({
        KOTLIN_PKG + "/ui/Bad.kt": (
            "@Composable\n"
            "private fun Bad(content: String, isStreaming: Boolean) {\n"
            "    val d by produceState(initialValue = content, isStreaming) {\n"
            "        snapshotFlow { content }.collect { value = it }\n"
            "    }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("stale_closure_guard.py", bad)
    check("plain param without rememberUpdatedState is caught (exit 1)", code == 1, f"exit={code}\n{out}")
    shutil.rmtree(bad)

    # 逃生口：带 stale-ok 说明的必须放行
    escaped = make_tree({
        KOTLIN_PKG + "/ui/Esc.kt": (
            "@Composable\n"
            "private fun Esc(content: String) {\n"
            "    // stale-ok: content is immutable for the lifetime of this effect\n"
            "    val d by produceState(initialValue = content) {\n"
            "        snapshotFlow { content }.collect { value = it }\n"
            "    }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("stale_closure_guard.py", escaped)
    check("stale-ok escape hatch passes (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(escaped)

def test_ime_inset():
    print("━━━ ime_inset_guard ━━━")
    # Ground truth: a text field hosted by a bare Scaffold with no imePadding is
    # invisible behind the keyboard (edge-to-edge + adjustResize = the window is
    # NOT resized for the IME). The guard must flag that, accept the fixed shape,
    # and honour both escape hatches. Two false negatives found while building
    # this guard are pinned here so they cannot regress:
    #   - a trailing-lambda `Scaffold { }` (no parens) must count as a host;
    #   - a `"*/*"` string literal must not swallow the rest of the file.
    good = make_tree({
        KOTLIN_PKG + "/ui/Good.kt": (
            "@Composable\n"
            "private fun Good() {\n"
            "    Scaffold { padding ->\n"
            "        Column(Modifier.fillMaxSize().padding(padding).imePadding()) {\n"
            "            OutlinedTextField(value = \"\", onValueChange = {})\n"
            "        }\n"
            "    }\n"
            "}\n"
        ),
        # 保护宿主（SettingsScaffold）⇒ 不在判据内
        KOTLIN_PKG + "/ui/Protected.kt": (
            "@Composable\n"
            "private fun Protected() {\n"
            "    SettingsScaffold(title = \"x\") {\n"
            "        OutlinedTextField(value = \"\", onValueChange = {})\n"
            "    }\n"
            "}\n"
        ),
        # 没有文本输入点的裸 Scaffold ⇒ 不该报
        KOTLIN_PKG + "/ui/NoField.kt": (
            "@Composable\n"
            "private fun NoField() {\n"
            "    Scaffold { padding -> Text(\"hi\") }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("ime_inset_guard.py", good)
    check("fixed + protected + fieldless pass (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(good)

    bad = make_tree({
        KOTLIN_PKG + "/ui/Bad.kt": (
            "@Composable\n"
            "private fun Bad() {\n"
            "    Scaffold { padding ->\n"
            "        Column(Modifier.fillMaxSize().padding(padding)) {\n"
            "            OutlinedTextField(value = \"\", onValueChange = {})\n"
            "        }\n"
            "    }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("ime_inset_guard.py", bad)
    check("bare Scaffold + field, no imePadding is caught (exit 1)", code == 1, f"exit={code}\n{out}")
    shutil.rmtree(bad)

    # 假阴性回归 1：`Scaffold {` 尾随 lambda 形态必须被识别为宿主
    trailing = make_tree({
        KOTLIN_PKG + "/ui/Trailing.kt": (
            "@Composable\n"
            "private fun Trailing() {\n"
            "    Scaffold { padding ->\n"
            "        OutlinedTextField(value = \"\", onValueChange = {})\n"
            "    }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("ime_inset_guard.py", trailing)
    check("trailing-lambda `Scaffold {` counts as host (exit 1)", code == 1, f"exit={code}\n{out}")
    shutil.rmtree(trailing)

    # 假阴性回归 2：文件中的 "*/*" 字符串不得吞掉后续代码
    star = make_tree({
        KOTLIN_PKG + "/ui/Star.kt": (
            "@Composable\n"
            "private fun Star() {\n"
            "    val launcher = remember { \"\" }\n"
            "    Button(onClick = { launcher.launch(\"*/*\") }) { Text(\"pick\") }\n"
            "}\n"
            "\n"
            "@Composable\n"
            "private fun Later() {\n"
            "    Scaffold { padding ->\n"
            "        OutlinedTextField(value = \"\", onValueChange = {})\n"
            "    }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("ime_inset_guard.py", star)
    check("\"*/*\" literal does not swallow later functions (exit 1)", code == 1, f"exit={code}\n{out}")
    shutil.rmtree(star)

    # 逃生口 1：ime-ok
    ok_esc = make_tree({
        KOTLIN_PKG + "/ui/OkEsc.kt": (
            "@Composable\n"
            "private fun OkEsc() {\n"
            "    Scaffold { padding ->\n"
            "        AlertDialog(onDismissRequest = {}) {\n"
            "            // ime-ok: independent Window, platform pans to the field\n"
            "            OutlinedTextField(value = \"\", onValueChange = {})\n"
            "        }\n"
            "    }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("ime_inset_guard.py", ok_esc)
    check("ime-ok escape hatch passes (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(ok_esc)

    # 逃生口 2：ime-unreachable
    un_esc = make_tree({
        KOTLIN_PKG + "/ui/UnEsc.kt": (
            "@Composable\n"
            "private fun UnEsc() {\n"
            "    Scaffold { padding -> Text(\"list\") }\n"
            "    // ime-unreachable: the field lives in the sheet branch below\n"
            "    Sheet { OutlinedTextField(value = \"\", onValueChange = {}) }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("ime_inset_guard.py", un_esc)
    check("ime-unreachable escape hatch passes (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(un_esc)

def test_stream_flow_dispatch():
    print("━━━ stream_flow_dispatch_guard ━━━")
    # Ground truth: a callbackFlow whose body blocks must carry a flowOn, or the
    # producer runs on the COLLECTOR's dispatcher and starves every timer on it
    # — including the flow's own watchdogs. The 2026-09-20 hang: the
    # :modelservice collector is `runBlocking` (no dispatcher), so the TTFB
    # watchdog, the first-data watchdog and the worker's outer timeout all
    # became unreachable at once and a 1.6MB request sat 272s with no byte back.
    good = make_tree({
        KOTLIN_PKG + "/provider/Good.kt": (
            "fun goodStream(): Flow<Int> = callbackFlow<Int> {\n"
            "    val reader = BufferedReader(InputStreamReader(resp.body!!.byteStream()))\n"
            "    val call = client.newCall(req)\n"
            "    val resp = call.execute()\n"
            "    while (reader.readLine() != null) { }\n"
            "    awaitClose { call.cancel() }\n"
            "}.flowOn(Dispatchers.IO)\n"
        ),
        # 链上还有别的 operator，flowOn 不在第一个位置 —— 也算放行
        KOTLIN_PKG + "/provider/Chained.kt": (
            "fun chainedStream(): Flow<String> = channelFlow<Int> {\n"
            "    Thread.sleep(1)\n"
            "}.flowOn(Dispatchers.IO).map { it.toString() }\n"
        ),
        # 体内没有阻塞调用 ⇒ 不归本门管
        KOTLIN_PKG + "/provider/NonBlocking.kt": (
            "fun pureStream(): Flow<Int> = callbackFlow<Int> {\n"
            "    trySend(1)\n"
            "    awaitClose { }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("stream_flow_dispatch_guard.py", good)
    check("dispatched / non-blocking flows pass (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(good)

    # 负例：体内阻塞且链上无 flowOn ⇒ 必须翻红
    bad = make_tree({
        KOTLIN_PKG + "/provider/Bad.kt": (
            "fun badStream(): Flow<Int> = callbackFlow<Int> {\n"
            "    val call = client.newCall(req)\n"
            "    val resp = call.execute()\n"
            "    while (reader.readLine() != null) { }\n"
            "    awaitClose { call.cancel() }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("stream_flow_dispatch_guard.py", bad)
    check("blocking cold flow without flowOn is caught (exit 1)", code == 1, f"exit={code}\n{out}")
    shutil.rmtree(bad)

    # flowOn(Dispatchers.Main) 只是把阻塞搬到 UI 线程，同样要翻红
    main_disp = make_tree({
        KOTLIN_PKG + "/provider/MainDisp.kt": (
            "fun mainStream(): Flow<Int> = callbackFlow<Int> {\n"
            "    val resp = call.execute()\n"
            "    awaitClose { }\n"
            "}.flowOn(Dispatchers.Main)\n"
        ),
    })
    code, out = run_scanner("stream_flow_dispatch_guard.py", main_disp)
    check("flowOn(Dispatchers.Main) is still a violation (exit 1)", code == 1, f"exit={code}\n{out}")
    shutil.rmtree(main_disp)

    # ★ 回归守卫：注释里的花括号/关键字不得干扰作用域配平。
    # 首版判据在这里翻车 —— 我加的解释性注释里写了 `// runBlocking { ... }`，
    # 行注释未掩码 ⇒ 花括号计数失衡 ⇒ match_brace 冲过真正的 } 一路吞掉文件后半段，
    # 反向对照时多报 11 处无关命中（含注释自身）。这个 fixture 把该 bug 钉死。
    comment_braces = make_tree({
        KOTLIN_PKG + "/provider/CommentBrace.kt": (
            "fun commented(): Flow<Int> = callbackFlow<Int> {\n"
            "    // the collector's `runBlocking { ... }` has no dispatcher\n"
            "    // see https://example.com/docs/flow {not a brace}\n"
            "    val resp = call.execute()\n"
            "    awaitClose { }\n"
            "}.flowOn(Dispatchers.IO)\n"
            "\n"
            "fun unrelated(): Int {\n"
            "    val s = \"https://example.com/a//b\"\n"
            "    return 1\n"
            "}\n"
        ),
    })
    code, out = run_scanner("stream_flow_dispatch_guard.py", comment_braces)
    check("comment/string braces do not break scoping (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(comment_braces)

    # 逃生口：带 stream-ok 说明的必须放行
    escaped = make_tree({
        KOTLIN_PKG + "/provider/Esc.kt": (
            "fun escStream(): Flow<Int> = callbackFlow<Int> {\n"
            "    // stream-ok: body runs on a dedicated single-thread executor\n"
            "    val resp = call.execute()\n"
            "    awaitClose { }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("stream_flow_dispatch_guard.py", escaped)
    check("stream-ok escape hatch passes (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(escaped)


def test_prefs_listener_holder():
    print("━━━ prefs_listener_holder_guard ━━━")
    tmp = tempfile.mkdtemp()
    good = os.path.join(tmp, "good")
    os.makedirs(good)

    # Positive: named field holder (the F-240 shape).
    with open(os.path.join(good, "Good.kt"), "w") as f:
        f.write("""
import android.content.SharedPreferences
class Good(private val prefs: SharedPreferences) {
    private val prefsListener =
        SharedPreferences.OnSharedPreferenceChangeListener { _, _ -> refresh() }
    init { prefs.registerOnSharedPreferenceChangeListener(prefsListener) }
    private fun refresh() {}
}
""")
    code, out = run_scanner("prefs_listener_holder_guard.py", good)
    check("named field holder passes (exit 0)", code == 0, f"exit={code}\n{out}")

    # Negative A: the exact hazard -- trailing-lambda call (NO parentheses).
    # This is the shape that shipped in ConfigBuiltins.kt:152, and the shape an
    # earlier version of this guard silently skipped.
    bad = os.path.join(tmp, "bad")
    os.makedirs(bad)
    with open(os.path.join(bad, "Bad.kt"), "w") as f:
        f.write("""
import android.content.SharedPreferences
object Bad {
    fun reg(prefs: SharedPreferences) {
        prefs.registerOnSharedPreferenceChangeListener { _, _ -> refresh() }
    }
    private fun refresh() {}
}
""")
    code, out = run_scanner("prefs_listener_holder_guard.py", bad)
    check("trailing-lambda inline hazard fails (exit != 0)", code != 0, f"exit={code}\n{out}")
    check("hazard is reported as INLINE", "INLINE" in out, out[:400])

    # Negative B: parenthesised anonymous-listener argument, also unheld.
    bad2 = os.path.join(tmp, "bad2")
    os.makedirs(bad2)
    with open(os.path.join(bad2, "Bad2.kt"), "w") as f:
        f.write("""
import android.content.SharedPreferences
object Bad2 {
    fun reg(prefs: SharedPreferences) {
        prefs.registerOnSharedPreferenceChangeListener(
            SharedPreferences.OnSharedPreferenceChangeListener { _, _ -> refresh() })
    }
    private fun refresh() {}
}
""")
    code, out = run_scanner("prefs_listener_holder_guard.py", bad2)
    check("parenthesised anonymous listener fails (exit != 0)", code != 0, f"exit={code}\n{out}")

    # Negative C: named local that is never stored anywhere -- looks held to a
    # naive reader, but nothing keeps it alive past the frame.
    bad3 = os.path.join(tmp, "bad3")
    os.makedirs(bad3)
    with open(os.path.join(bad3, "Bad3.kt"), "w") as f:
        f.write("""
import android.content.SharedPreferences
object Bad3 {
    fun reg(prefs: SharedPreferences) {
        val listener = SharedPreferences.OnSharedPreferenceChangeListener { _, _ -> refresh() }
        prefs.registerOnSharedPreferenceChangeListener(listener)
    }
    private fun refresh() {}
}
""")
    code, out = run_scanner("prefs_listener_holder_guard.py", bad3)
    check("unheld named local fails (exit != 0)", code != 0, f"exit={code}\n{out}")

    # Positive: named local that IS stored into a field.
    good2 = os.path.join(tmp, "good2")
    os.makedirs(good2)
    with open(os.path.join(good2, "Good2.kt"), "w") as f:
        f.write("""
import android.content.SharedPreferences
object Good2 {
    @Volatile private var listener: SharedPreferences.OnSharedPreferenceChangeListener? = null
    fun reg(prefs: SharedPreferences) {
        val l = SharedPreferences.OnSharedPreferenceChangeListener { _, _ -> refresh() }
        listener = l
        prefs.registerOnSharedPreferenceChangeListener(l)
    }
    private fun refresh() {}
}
""")
    code, out = run_scanner("prefs_listener_holder_guard.py", good2)
    check("local stored into a field passes (exit 0)", code == 0, f"exit={code}\n{out}")

    # Escape hatch.
    esc = os.path.join(tmp, "esc")
    os.makedirs(esc)
    with open(os.path.join(esc, "Esc.kt"), "w") as f:
        f.write("""
import android.content.SharedPreferences
object Esc {
    fun reg(prefs: SharedPreferences) {
        // prefs-listener-ok: held by the caller for the screen's lifetime
        prefs.registerOnSharedPreferenceChangeListener { _, _ -> refresh() }
    }
    private fun refresh() {}
}
""")
    code, out = run_scanner("prefs_listener_holder_guard.py", esc)
    check("prefs-listener-ok escape hatch passes (exit 0)", code == 0, f"exit={code}\n{out}")

    # Regression fixtures for the scope-classification bug found 2026-09-21.
    # The guard fused `@Composable` + `fun` across the newline into one token,
    # `\bfun\b` stopped matching, and the function body was classified as a
    # class -- so every local `val listener` inside it looked like a field and
    # reported `held`. Four real sites (ChatScreen, AppearanceScreen,
    # ChatMenuSettingsScreen, MemoryManagementScreen) got that false pass, and
    # deleting their actual holder still passed. These fixtures are the ground
    # truth: both shapes MUST fail when unheld.
    for label, sig in (
        ("annotated multi-line signature",
         "@Composable\nfun Screen(\n    prefs: SharedPreferences,\n    context: Context,\n) {"),
        ("annotated single-line signature",
         "@Composable\nfun Screen(prefs: SharedPreferences, context: Context) {"),
        ("unannotated multi-line signature",
         "fun reg(\n    prefs: SharedPreferences,\n) {"),
    ):
        d = os.path.join(tmp, "shape_" + label.replace(" ", "_"))
        os.makedirs(d)
        with open(os.path.join(d, "Shape.kt"), "w") as f:
            f.write(f"""
import android.content.SharedPreferences
object Shape {{
    {sig}
        val listener = SharedPreferences.OnSharedPreferenceChangeListener {{ _, _ -> refresh() }}
        prefs.registerOnSharedPreferenceChangeListener(listener)
    }}
    private fun refresh() {{}}
}}
""")
        code, out = run_scanner("prefs_listener_holder_guard.py", d)
        check(f"unheld local with {label} fails (exit != 0)", code != 0, f"exit={code}\n{out}")

    # And the DisposableEffect shape these four sites actually use MUST pass --
    # otherwise the fixtures above could be satisfied by a guard that just
    # reports everything as unheld.
    disp = os.path.join(tmp, "dispose_shape")
    os.makedirs(disp)
    with open(os.path.join(disp, "Disp.kt"), "w") as f:
        f.write("""
import android.content.SharedPreferences
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
@Composable
fun rememberThing(
    context: Context,
) {
    val prefs = ChatMenuPrefs.prefs(context)
    DisposableEffect(prefs) {
        val listener = SharedPreferences.OnSharedPreferenceChangeListener { _, _ -> refresh() }
        prefs.registerOnSharedPreferenceChangeListener(listener)
        onDispose { prefs.unregisterOnSharedPreferenceChangeListener(listener) }
    }
}
""")
    code, out = run_scanner("prefs_listener_holder_guard.py", disp)
    check("onDispose-held listener in annotated multi-line fn passes (exit 0)",
          code == 0, f"exit={code}\n{out}")

    # Mutation: remove the onDispose holder -> must now FAIL. This is the exact
    # edit that used to stay green.
    mut = os.path.join(tmp, "dispose_mutated")
    os.makedirs(mut)
    with open(os.path.join(mut, "Mut.kt"), "w") as f:
        f.write("""
import android.content.SharedPreferences
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
@Composable
fun rememberThing(
    context: Context,
) {
    val prefs = ChatMenuPrefs.prefs(context)
    DisposableEffect(prefs) {
        val listener = SharedPreferences.OnSharedPreferenceChangeListener { _, _ -> refresh() }
        prefs.registerOnSharedPreferenceChangeListener(listener)
    }
}
""")
    code, out = run_scanner("prefs_listener_holder_guard.py", mut)
    check("removing the onDispose holder fails (exit != 0)", code != 0, f"exit={code}\n{out}")
    shutil.rmtree(tmp)

def test_claim_release():
    print("━━━ claim_release_guard ━━━")
    # Ground truth = the actual 2026-10-12 regression shape: a claim that
    # flips the flag BEFORE publishing the marker its release gate compares.
    # Check B must catch the pre-flip order and accept the post-flip order.
    base = KOTLIN_PKG + "/ui/chat/"
    claim_good = make_tree({
        base + "Good.kt": (
            "package com.rikkaminis.app.ui.chat\n"
            "class Good {\n"
            "    var streamingClaimToken: Any = Any()\n"
            "    var streamJob: Job? = null\n"
            "    fun send() {\n"
            "        streamingClaimToken = Any()\n"
            "        _isStreaming.value = true\n"
            "        val claimToken = streamingClaimToken\n"
            "        streamJob = launch {\n"
            "            try { loop() } finally {\n"
            "                if (claimToken === streamingClaimToken) { _isStreaming.value = false }\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "    fun sendSecond() {\n"
            "        streamingClaimToken = Any()\n"
            "        _isStreaming.value = true\n"
            "    }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("claim_release_guard.py", claim_good)
    check("claim after mint passes strict mode (exit 0)", code == 0,
          f"exit={code}\n{out}")
    shutil.rmtree(claim_good)

    claim_bad = make_tree({
        base + "Bad.kt": (
            "package com.rikkaminis.app.ui.chat\n"
            "class Bad {\n"
            "    var streamingClaimToken: Any = Any()\n"
            "    fun send() {\n"
            "        _isStreaming.value = true\n"
            "        streamingClaimToken = Any()\n"
            "        val claimToken = streamingClaimToken\n"
            "        launch {} // uses claimToken so the family pairs up\n"
            "        if (claimToken === streamingClaimToken) { _isStreaming.value = false }\n"
            "    }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("claim_release_guard.py", claim_bad)
    check("pre-flip claim (the 10-12 shape) is caught (exit 1)", code == 1,
          f"exit={code}\n{out}")
    check("finding names the after-claim mint", "AFTER the claim" in out, out)
    shutil.rmtree(claim_bad)

    escaped_tree = make_tree({
        base + "Esc.kt": (
            "package com.rikkaminis.app.ui.chat\n"
            "class Esc {\n"
            "    var streamingClaimToken: Any = Any()\n"
            "    fun send() {\n"
            "        // claim-ok: pre-existing turn, marker minted by caller\n"
            "        _isStreaming.value = true\n"
            "        streamingClaimToken = Any()\n"
            "        val claimToken = streamingClaimToken\n"
            "        launch {}\n"
            "        if (claimToken === streamingClaimToken) { _isStreaming.value = false }\n"
            "    }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("claim_release_guard.py", escaped_tree)
    check("claim-ok escape hatch passes (exit 0)", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(escaped_tree)

    # Check A: ungated sibling next to a gated one must be reported.
    sibling_bad = make_tree({
        base + "Sib.kt": (
            "package com.rikkaminis.app.ui.chat\n"
            "class Sib {\n"
            "    var streamEpoch = 0\n"
            "    var curEpoch = 0\n"
            "    fun a() { if (streamEpoch === curEpoch) { _isStreaming.value = false } }\n"
            "    fun b() { _isStreaming.value = false }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("claim_release_guard.py", sibling_bad)
    check("ungated sibling release is caught (exit 1)", code == 1,
          f"exit={code}\n{out}")
    shutil.rmtree(sibling_bad)

    # Take-then-clear hand-off must NOT be reported.
    handoff_ok = make_tree({
        base + "Hand.kt": (
            "package com.rikkaminis.app.ui.chat\n"
            "class Hand {\n"
            "    var pendingContinuation: Any? = null\n"
            "    var gateEpoch = 0\n"
            "    var curGate = 0\n"
            "    fun respond() {\n"
            "        val cont = pendingContinuation ?: return\n"
            "        pendingContinuation = null\n"
            "        _pendingRequest.value = null\n"
            "    }\n"
            "    fun timedOut() { if (gateEpoch === curGate) { pendingContinuation = null } }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("claim_release_guard.py", handoff_ok)
    check("atomic hand-off clears pass strict mode (exit 0)", code == 0,
          f"exit={code}\n{out}")
    shutil.rmtree(handoff_ok)

    # Report mode must never gate: same tree, exit 0.
    report_tree = make_tree({
        base + "Rep.kt": (
            "package com.rikkaminis.app.ui.chat\n"
            "class Rep {\n"
            "    var streamEpoch = 0\n"
            "    var curEpoch = 0\n"
            "    fun a() { if (streamEpoch === curEpoch) { _isStreaming.value = false } }\n"
            "    fun b() { _isStreaming.value = false }\n"
            "}\n"
        ),
    })
    code, out = run_scanner("claim_release_guard.py", report_tree, "--report")
    check("--report prints findings but exits 0", code == 0 and "REPORT-ONLY" in out,
          f"exit={code}\n{out}")
    shutil.rmtree(report_tree)


def test_liveness_static():
    print("━━━ liveness_static_guard ━━━")
    # Self-test fixtures (clean + the A/B/C violation classes).
    code, out = run_scanner("liveness_static_guard.py", "--self-test")
    check(
        "self-test fixtures pass (clean tree passes; A/B/C all caught)",
        code == 0,
        f"exit={code}\n{out}",
    )
    # Real-repo coverage comes from the test_real_repo() scanner list.


def loop_reset_fixture(shape):
    """Minimal repo tree for loop_counter_reset_guard.

    Ground truth = the 2026-10-15 audit2 BUG-01 shape: a numeric counter reset
    whose comment block claims tool-call semantics, placed at the brace depth
    of the `if (toolCalls.isEmpty())` header instead of inside a tool-call
    branch.
    """
    rel = KOTLIN_PKG + "/ui/chat/Loop.kt"
    head = (
        "package com.rikkaminis.app.ui.chat\n"
        "class LoopState { var lengthWallContinues = 0 }\n"
        "class Loop {\n"
        "    fun turn(loopState: LoopState, toolCalls: List<String>) {\n"
    )
    guard = (
        "        if (toolCalls.isEmpty()) {\n"
        "            break\n"
        "        }\n"
    )
    claim = (
        "        // [feat/hermes-tier1] A tool-call turn clears the length-wall\n"
        "        // continuation budget: the model recovered and is doing new work.\n"
    )
    reset = "        loopState.lengthWallContinues = 0\n"
    if shape == "good":
        body = (
            guard
            + "        if (toolCalls.isNotEmpty()) {\n"
            + claim + reset
            + "        }\n"
        )
    elif shape == "bad":
        # The regression: same depth as the isEmpty() header, guarded by nothing.
        body = guard + claim + reset
    elif shape == "no_claim":
        # A reset with no tool-call claim is out of this guard's remit.
        body = guard + "        loopState.lengthWallContinues = 0\n"
    else:
        raise AssertionError(shape)
    text = head + body + "    }\n}\n"
    line_no = text[: text.index(reset)].count("\n") + 1
    return {rel: text}, line_no


def test_loop_counter_reset():
    print("━━━ loop_counter_reset_guard ━━━")
    files, line_no = loop_reset_fixture("good")
    root = make_tree(files)
    code, out = run_scanner("loop_counter_reset_guard.py", root)
    check("reset inside a toolCalls.isNotEmpty() guard passes (exit 0)",
          code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

    files, line_no = loop_reset_fixture("bad")
    root = make_tree(files)
    code, out = run_scanner("loop_counter_reset_guard.py", root)
    check("reset at the isEmpty() header depth is caught (exit 1)", code == 1,
          f"exit={code}\n{out}")
    check(f"finding names the exact file:line ({line_no})",
          f"Loop.kt:{line_no}" in out, out)
    check("finding explains the shared depth", "same depth as" in out, out)
    shutil.rmtree(root)

    files, _ = loop_reset_fixture("no_claim")
    root = make_tree(files)
    code, out = run_scanner("loop_counter_reset_guard.py", root)
    check("reset without a tool-call claim is out of remit (exit 0)",
          code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)


def budget_stop_fixture(shape):
    """Minimal repo tree for budget_stop_break_guard.

    Ground truth = the audit3 F-02 shape (2026-10-15): the agent loop's
    execution-budget gates sit inside the turn body's retry `while`, and their
    bare `break` left only the retry loop — the turn then fell through to the
    success tail (route Success for the refused member, empty assistant entry,
    burned turns against a FINALIZING reducer).
    """
    rel = KOTLIN_PKG + "/ui/chat/Loop.kt"
    head = (
        "package com.rikkaminis.app.ui.chat\n"
        "class Loop {\n"
        "    fun run(maxTurns: Int) {\n"
    )
    tail = "        }\n    }\n}\n"
    if shape == "good":
        outer, inner = "        turnLoop@ for (turn in 0 until maxTurns) {\n", "            while (!done) {\n"
        gate = (
            "                if (!budget.consumeAttempt()) {\n"
            '                    traceObserver.t7BudgetStopReason = "provider_attempt_limit"\n'
            "                    break@turnLoop\n"
            "                }\n"
        )
        closer, marker = "            }\n", "break@turnLoop"
    elif shape == "bad_bare_break":
        # The regression: same shape, but the break leaves only the while.
        outer, inner = "        turnLoop@ for (turn in 0 until maxTurns) {\n", "            while (!done) {\n"
        gate = (
            "                if (!budget.consumeAttempt()) {\n"
            '                    traceObserver.t7BudgetStopReason = "provider_attempt_limit"\n'
            "                    break\n"
            "                }\n"
        )
        closer, marker = "            }\n", "break"
    elif shape == "bad_unlabeled_loop":
        # Same, and the enclosing turn loop carries no label: nothing to break to.
        outer, inner = "        for (turn in 0 until maxTurns) {\n", "            while (!done) {\n"
        gate = (
            "                if (!budget.consumeAttempt()) {\n"
            '                    traceObserver.t7BudgetStopReason = "provider_attempt_limit"\n'
            "                    break\n"
            "                }\n"
        )
        closer, marker = "            }\n", "break"
    elif shape == "shallow_ok":
        # Turn-entry gate (no inner loop): a bare break already leaves the run —
        # this is the production deadline / turn_limit shape.
        outer, inner = "        turnLoop@ for (turn in 0 until maxTurns) {\n", ""
        gate = (
            "            if (budget.isExpired()) {\n"
            '                traceObserver.t7BudgetStopReason = "deadline_reached"\n'
            "                break\n"
            "            }\n"
        )
        closer, marker = "", "break"
    elif shape == "return_only":
        # The branch returns an advisory error instead of ending the run
        # (production: ChatPromptAndTools' tool_call_limit) — out of remit.
        outer, inner = "        turnLoop@ for (turn in 0 until maxTurns) {\n", "            while (!done) {\n"
        gate = (
            "                if (!budget.consumeToolCall()) {\n"
            '                    traceObserver.t7BudgetStopReason = "tool_call_limit"\n'
            '                    return "budget exhausted"\n'
            "                }\n"
        )
        closer, marker = "            }\n", "return"
    else:
        raise AssertionError(shape)
    text = head + outer + inner + gate + closer + tail
    line_no = text[: text.index(marker)].count("\n") + 1
    return {rel: text}, line_no


def test_budget_stop_break():
    print("━━━ budget_stop_break_guard ━━━")
    root = make_tree(budget_stop_fixture("good")[0])
    code, out = run_scanner("budget_stop_break_guard.py", root)
    check("break@turnLoop out of a nested budget gate passes (exit 0)",
          code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

    files, line_no = budget_stop_fixture("bad_bare_break")
    root = make_tree(files)
    code, out = run_scanner("budget_stop_break_guard.py", root)
    check("bare break out of a nested budget gate is caught (exit 1)", code == 1,
          f"exit={code}\n{out}")
    check(f"finding names the exact file:line ({line_no})",
          f"Loop.kt:{line_no}" in out, out)
    check("finding names the required label", "break@turnLoop" in out, out)
    shutil.rmtree(root)

    files, line_no = budget_stop_fixture("bad_unlabeled_loop")
    root = make_tree(files)
    code, out = run_scanner("budget_stop_break_guard.py", root)
    check("nested break with an unlabeled enclosing loop is caught (exit 1)",
          code == 1, f"exit={code}\n{out}")
    check("finding explains the missing label",
          "carries no label" in out, out)
    shutil.rmtree(root)

    root = make_tree(budget_stop_fixture("shallow_ok")[0])
    code, out = run_scanner("budget_stop_break_guard.py", root)
    check("turn-entry gate at for-body depth may use a bare break (exit 0)",
          code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

    root = make_tree(budget_stop_fixture("return_only")[0])
    code, out = run_scanner("budget_stop_break_guard.py", root)
    check("a budget branch that returns instead of breaking is out of remit (exit 0)",
          code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)


def test_katex_bridge_arity():
    """The asset HTML and its Kotlin hosts must agree on JS-bridge arity."""
    print("━━━ katex_bridge_arity_check ━━━")

    def fixture(kotlin_params, html_args="w, h, e, t"):
        return {
            "src/android/app/src/main/assets/katex/katex-render.html": (
                "<script>\n"
                "  function renderMath(latex, displayMode, fontSize, isDark, token) {\n"
                "    AndroidBridge.onRendered(%s);\n"
                "    // prose apostrophes must not break the scanner: the pool's cache\n"
                "  }\n"
                "</script>\n" % html_args
            ),
            "src/android/app/src/main/java/com/rikkaminis/app/ui/markdown/KaTeXView.kt": (
                "package com.rikkaminis.app.ui.markdown\n"
                "class V {\n"
                "    private fun f(wv: Any) {\n"
                "        wv.addJavascriptInterface(object {\n"
                "            @JavascriptInterface\n"
                "            fun onRendered(%s) { }\n"
                "        }, \"AndroidBridge\")\n"
                "    }\n"
                "}\n" % kotlin_params
            ),
        }

    root = make_tree(fixture("width: Int, height: Int, error: String, token: Long"))
    code, out = run_scanner("katex_bridge_arity_check.py", root)
    check("4-arg call site against a 4-arg declaration passes (exit 0)", code == 0, f"exit={code}\n{out}")
    check("table reports the call site arity", "onRendered(4 args)" in out, out)
    check("table reports the host declaration", "[OK]" in out and "[4]" in out, out)
    shutil.rmtree(root)

    # The exact audit3 BUG-05 shape: the sibling host was updated, this one was not.
    root = make_tree(fixture("width: Int, height: Int, error: String"))
    code, out = run_scanner("katex_bridge_arity_check.py", root)
    check("3-arg declaration under a 4-arg call site is caught (exit 1)", code == 1, f"exit={code}\n{out}")
    check("finding names the host file and the declared arity", "KaTeXView.kt" in out and "[3]" in out, out)
    shutil.rmtree(root)

    files = fixture("width: Int, height: Int, error: String")
    files["src/android/app/src/main/java/com/rikkaminis/app/ui/chat/Pool.kt"] = (
        "package com.rikkaminis.app.ui.chat\n"
        "class P {\n"
        "    fun f(wv: Any) {\n"
        "        wv.addJavascriptInterface(JsBridge { w, h, e, t -> }, \"AndroidBridge\")\n"
        "    }\n"
        "    private class JsBridge(private val cb: (Int, Int, String, Long) -> Unit) {\n"
        "        @JavascriptInterface\n"
        "        fun onRendered(width: Int, height: Int, error: String, token: Long) { }\n"
        "    }\n"
        "}\n"
    )
    root = make_tree(files)
    code, out = run_scanner("katex_bridge_arity_check.py", root)
    check("a matching sibling host does not mask a stale one (exit 1)", code == 1, f"exit={code}\n{out}")
    check("both hosts are checked per file", "Pool.kt" in out and "KaTeXView.kt" in out, out)
    shutil.rmtree(root)

    rot = fixture("width: Int, height: Int, error: String, token: Long)")
    rot["src/android/app/src/main/assets/katex/katex-render.html"] = "<script>var x = 1;</script>\n"
    root = make_tree(rot)
    code, out = run_scanner("katex_bridge_arity_check.py", root)
    check("a page with no call site trips the rot guard (exit 1)", code == 1, f"exit={code}\n{out}")
    check("rot guard says the contract is unchecked", "ROT" in out, out)
    shutil.rmtree(root)


    # A host whose registration cannot be parsed must fail loudly instead of
    # dropping out of the host set. Found by this guard's own mutation control:
    # a mangled registration made KaTeXView.kt skip silently, the sibling host
    # still supplied the interface name, and the guard went green on a tree
    # whose declaration no longer matched the page.
    files = fixture("width: Int, height: Int, error: String, token: Long)")
    files["src/android/app/src/main/java/com/rikkaminis/app/ui/chat/Variable.kt"] = (
        "package com.rikkaminis.app.ui.chat\n"
        "class Q {\n"
        "    fun f(wv: Any, name: String) {\n"
        "        wv.addJavascriptInterface(object { }, name)\n"
        "    }\n"
        "}\n"
    )
    root = make_tree(files)
    code, out = run_scanner("katex_bridge_arity_check.py", root)
    check("an unparseable registration fails loudly (exit 1)", code == 1, f"exit={code}\n{out}")
    check("rot guard names the offending file", "Variable.kt" in out and "ROT" in out, out)
    shutil.rmtree(root)


GEMINI_DIR = "src/android/app/src/main/java/com/rikkaminis/app/provider/gemini"


def gemini_fixture(provider_normalizer=None, shared_normalizer=None, sibling_normalizer=None,
                   extra_bypass=False):
    """Build a three-file gemini tree mirroring the live shape."""
    provider_body = provider_normalizer or (
        "    private fun normalizedBasePath(): String = GeminiBasePath.normalize(basePath)\n"
    )
    shared_body = shared_normalizer or (
        "    fun normalize(raw: String?): String {\n"
        "        val base = raw?.trim()?.trimEnd('/')?.takeIf { it.isNotEmpty() } ?: return DEFAULT_BASE\n"
        "        var stripped = base\n"
        "        while (true) {\n"
        "            val next = stripped.removeSuffix(\"/v1beta\").removeSuffix(\"/v1\")\n"
        "            if (next == stripped) break\n"
        "            stripped = next\n"
        "        }\n"
        "        return \"$stripped/v1beta\"\n"
        "    }\n"
    )
    sibling_body = sibling_normalizer or (
        "    fun normalizeBase(baseUrl: String?): String {\n"
        "        val raw = baseUrl?.trim()?.trimEnd('/')?.takeIf { it.isNotEmpty() } ?: return DEFAULT_BASE\n"
        "        val stripped = raw.removeSuffix(\"/v1beta\").removeSuffix(\"/v1\")\n"
        "        return \"$stripped/v1beta\"\n"
        "    }\n"
    )
    bypass = (
        "    fun extra() {\n"
        "        val url = \"${basePath}/models/gemini-2.5-flash:generateContent\"\n"
        "    }\n"
        if extra_bypass else ""
    )
    return {
        GEMINI_DIR + "/GeminiProvider.kt": (
            "package com.rikkaminis.app.provider.gemini\n"
            "class GeminiProvider(private val basePath: String = GeminiBasePath.DEFAULT_BASE) {\n"
            + provider_body
            + "    fun sendMessage() {\n"
            "        val url = \"${normalizedBasePath()}/models/gemini-2.5-flash:generateContent\"\n"
            "    }\n"
            "    fun streamMessage() {\n"
            "        val url = \"${normalizedBasePath()}/models/gemini-2.5-flash:streamGenerateContent\"\n"
            "    }\n"
            + bypass
            + "}\n"
        ),
        GEMINI_DIR + "/GeminiBasePath.kt": (
            "package com.rikkaminis.app.provider.gemini\n"
            "internal object GeminiBasePath {\n"
            "    const val DEFAULT_BASE = \"https://generativelanguage.googleapis.com/v1beta\"\n"
            + shared_body
            + "}\n"
        ),
        GEMINI_DIR + "/GeminiModelsApi.kt": (
            "package com.rikkaminis.app.provider.gemini\n"
            "internal object GeminiModelsApi {\n"
            "    private const val DEFAULT_BASE = \"https://generativelanguage.googleapis.com/v1beta\"\n"
            + sibling_body
            + "}\n"
        ),
    }


def test_gemini_base_version():
    """The Gemini base must end in exactly one /v1beta — per file, both call sites."""
    print("━━━ gemini_base_version_check ━━━")

    root = make_tree(gemini_fixture())
    code, out = run_scanner("gemini_base_version_check.py", root)
    check("all three normalizers present passes (exit 0)", code == 0, f"exit={code}\n{out}")
    check("delegation is reported as the intended shape", "delegates to GeminiBasePath.normalize" in out, out)
    check("both call sites are counted", "routes 2 call site(s)" in out, out)
    shutil.rmtree(root)

    # The live pre-fix shape: strips the version segment, never restores one.
    root = make_tree(gemini_fixture(provider_normalizer=(
        "    private fun normalizedBasePath(): String {\n"
        "        var p = basePath\n"
        "        while (p.endsWith(\"/\")) p = p.dropLast(1)\n"
        "        return p.removeSuffix(\"/v1beta\").removeSuffix(\"/v1\")\n"
        "    }\n"
    )))
    code, out = run_scanner("gemini_base_version_check.py", root)
    check("strip-without-restore is caught (exit 1)", code == 1, f"exit={code}\n{out}")
    check("finding names GeminiProvider.kt", "GeminiProvider.kt" in out, out)
    shutil.rmtree(root)

    # Anti-mask: the provider delegates, so the shared file has to carry the restore.
    root = make_tree(gemini_fixture(shared_normalizer=(
        "    fun normalize(raw: String?): String {\n"
        "        val base = raw?.trim()?.trimEnd('/')?.takeIf { it.isNotEmpty() } ?: return DEFAULT_BASE\n"
        "        var stripped = base\n"
        "        while (true) {\n"
        "            val next = stripped.removeSuffix(\"/v1beta\").removeSuffix(\"/v1\")\n"
        "            if (next == stripped) break\n"
        "            stripped = next\n"
        "        }\n"
        "        return stripped\n"
        "    }\n"
    )))
    code, out = run_scanner("gemini_base_version_check.py", root)
    check("delegation does not launder a broken shared implementation (exit 1)", code == 1, f"exit={code}\n{out}")
    check("finding names GeminiBasePath.kt", "GeminiBasePath.kt" in out, out)
    shutil.rmtree(root)

    # Per-file, not union: the sibling normalizer is the one the list path uses.
    root = make_tree(gemini_fixture(sibling_normalizer=(
        "    fun normalizeBase(baseUrl: String?): String {\n"
        "        val raw = baseUrl?.trim()?.trimEnd('/')?.takeIf { it.isNotEmpty() } ?: return DEFAULT_BASE\n"
        "        return raw.removeSuffix(\"/v1beta\").removeSuffix(\"/v1\")\n"
        "    }\n"
    )))
    code, out = run_scanner("gemini_base_version_check.py", root)
    check("a broken sibling normalizer is caught on its own (exit 1)", code == 1, f"exit={code}\n{out}")
    check("finding names GeminiModelsApi.kt", "GeminiModelsApi.kt" in out, out)
    shutil.rmtree(root)

    # Rot guard: a renamed normalizer must fail loudly, not read as clean.
    root = make_tree(gemini_fixture(provider_normalizer=(
        "    private fun normalizedBase(): String = GeminiBasePath.normalize(basePath)\n"
    )))
    code, out = run_scanner("gemini_base_version_check.py", root)
    check("a renamed normalizer is a hard fail, not a skip (exit 1)", code == 1, f"exit={code}\n{out}")
    check("rot message says the function was not found", "not found" in out, out)
    shutil.rmtree(root)

    # Wiring: one call site that skips the normalizer is the exact comeback shape.
    root = make_tree(gemini_fixture(extra_bypass=True))
    code, out = run_scanner("gemini_base_version_check.py", root)
    check("a call site built from the raw basePath is caught (exit 1)", code == 1, f"exit={code}\n{out}")
    check("finding mentions the raw basePath", "raw basePath" in out, out)
    shutil.rmtree(root)


def test_real_repo():
    print("━━━ real repo tree (must be clean) ━━━")
    for script in (
        "four_way_sync_check.py",
        "i18n_check.py",
        "enum_parse_safety_check.py",
        "provider_boundary_guard.py",
        "legacy_pipeline_guard.py",
        "trace_eval_check.py",
        "extra_days_container_guard.py",
        "stale_closure_guard.py",
        "ime_inset_guard.py",
        "stream_flow_dispatch_guard.py",
        "prefs_listener_holder_guard.py",
        "thinking_wiring_guard.py",
        "liveness_static_guard.py",
        "claim_release_guard.py",
        "loop_counter_reset_guard.py",
        "budget_stop_break_guard.py",
        "katex_bridge_arity_check.py",
    ):
        code, out = run_scanner(script, REPO_ROOT)
        check(f"{script} on real repo exits 0", code == 0, f"exit={code}\n{out[:2000]}")


def xproc_fixture(model_body=None, dispatcher_puts=None, comment_comma=False):
    """Minimal repo tree for xproc_boundary_check."""
    B = "src/android/app/src/main/java/com/rikkaminis/app/"
    if model_body is None:
        model_body = (
            "    val id: String,\n"
            "    val maxOutputTokens: Int? = null,\n"
        )
    if comment_comma:
        # The real shape that broke an earlier version: a comma inside a comment
        # split the field list mid-way and dropped every later field.
        model_body = (
            "    val id: String,\n"
            "    // Some relay gateways, however, do not honour this.\n"
            "    val maxOutputTokens: Int? = null,\n"
        )
    if dispatcher_puts is None:
        dispatcher_puts = (
            '        put("model_id", model.id)\n'
            '        model.maxOutputTokens?.let { put("max_output_tokens", it) }\n'
        )
    # ProviderInstance is always part of the boundary; write its key so the
    # fixture is internally consistent (otherwise the guard correctly reports it).
    fixed_puts = '        put("instance_id", instance.id)\n'
    return {
        B + "data/model/LLMModel.kt":
            "package com.rikkaminis.app.data.model\n\n"
            "data class LLMModel(\n" + model_body + ")\n",
        B + "data/model/ProviderConfig.kt":
            "package com.rikkaminis.app.data.model\n\n"
            "data class ProviderInstance(\n    val id: String,\n)\n",
        B + "sandbox/offload/ModelExecutionDispatcher.kt":
            "package com.rikkaminis.app.sandbox.offload\n\n"
            "class ModelExecutionDispatcher {\n"
            "    fun build(model: LLMModel, instance: ProviderInstance) {\n"
            "        val bundle = Bundle()\n"
            "        bundle.apply {\n" + fixed_puts + dispatcher_puts + "        }\n"
            "    }\n}\n",
        B + "sandbox/offload/ModelExecutionService.kt":
            "package com.rikkaminis.app.sandbox.offload\n\n"
            "class ModelExecutionService {\n"
            '    fun read(b: Bundle) { val id = b.getString("model_id")\n'
            '        val m = b.getInt("max_output_tokens")\n'
            '        val i = b.getString("instance_id") }\n}\n',
    }


def test_xproc_boundary():
    print("━━━ xproc_boundary_check ━━━")
    # Clean — every boundary field is written by the dispatcher.
    root = make_tree(xproc_fixture())
    code, out = run_scanner("xproc_boundary_check.py", root)
    check("clean fixture exits 0", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

    # Dirty — the exact P0 shape: maxOutputTokens never written.
    root = make_tree(xproc_fixture(dispatcher_puts='        put("model_id", model.id)\n'))
    code, out = run_scanner("xproc_boundary_check.py", root)
    check(
        "unwritten field caught (exit 1, MISSING maxOutputTokens)",
        code == 1 and "maxOutputTokens" in out and "MISSING" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # Regression: a comma inside a comment must NOT truncate the field list.
    root = make_tree(xproc_fixture(comment_comma=True,
                                   dispatcher_puts='        put("model_id", model.id)\n'))
    code, out = run_scanner("xproc_boundary_check.py", root)
    check(
        "comment comma does not truncate field list (maxOutputTokens still seen)",
        code == 1 and "maxOutputTokens" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # Bare put() inside apply{} must be recognised (no leading dot).
    root = make_tree(xproc_fixture())
    code, out = run_scanner("xproc_boundary_check.py", root)
    check("bare put() inside apply{} recognised", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)


def thinking_wiring_fixture(requested_arg="requestedThinkingLevelState",
                            requested_binding="val requestedThinkingLevelState by viewModel.requestedThinkingLevel.collectAsState()",
                            current_arg="viewModel.effectiveThinkingLevel",
                            clamp_arg="requested", tap_arg="requested",
                            escape=False):
    """Minimal tree for thinking_wiring_guard: one picker call + its body."""
    pkg = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/"
    esc = " // thinking-wiring-ok: fixture" if escape else ""
    caller = (
        "package com.rikkaminis.app.ui.chat\n"
        "class ChatInputArea {\n"
        "    fun row(viewModel: ChatViewModel) {\n"
        f"        {requested_binding}\n"
        "        ThinkingLevelPicker(\n"
        f"            current = {current_arg},\n"
        f"            requested = {requested_arg},{esc}\n"
        "            availableLevels = viewModel.availableThinkingLevels,\n"
        "            onSelect = { },\n"
        "        )\n"
        "    }\n"
        "}\n"
    )
    body = (
        "package com.rikkaminis.app.ui.chat\n"
        "internal fun ThinkingLevelPicker(\n"
        "    current: ThinkingLevel,\n"
        "    requested: ThinkingLevel,\n"
        "    availableLevels: List<ThinkingLevel>,\n"
        "    onSelect: (ThinkingLevel) -> Unit,\n"
        ") {\n"
        "    val maxAvailable = availableLevels.lastOrNull { it != ThinkingLevel.OFF }\n"
        f"    val isClamped = maxAvailable != null && isCappedBy({clamp_arg}, maxAvailable)\n"
        "    availableLevels.forEach { level ->\n"
        f"        onSelect(thinkingTapTarget(level, {tap_arg}))\n"
        "    }\n"
        "}\n"
    )
    return {pkg + "ChatInputArea.kt": caller, pkg + "ChatComposerWidgets.kt": body}

def test_thinking_wiring():
    print("━━━ thinking_wiring_guard ━━━")
    # Clean.
    root = make_tree(thinking_wiring_fixture())
    code, out = run_scanner("thinking_wiring_guard.py", root)
    check("clean fixture exits 0", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

    # The real 2026-09-22 defect: `requested` fed the effective level. The
    # caller's local is renamed so a spelling-based check would miss it —
    # provenance must follow the `val` binding.
    root = make_tree(thinking_wiring_fixture(
        requested_arg="effectiveThinkingLevelState",
        requested_binding="val effectiveThinkingLevelState by viewModel.thinkingLevel.collectAsState()",
    ))
    code, out = run_scanner("thinking_wiring_guard.py", root)
    check(
        "requested <- effective flow caught through a rename",
        code == 1 and "EFFECTIVE level" in out,
        f"exit={code}\n{out}",
    )
    shutil.rmtree(root)

    # `current` collapsed to the raw choice.
    root = make_tree(thinking_wiring_fixture(current_arg="requestedThinkingLevelState"))
    code, out = run_scanner("thinking_wiring_guard.py", root)
    check("current <- raw choice caught", code == 1 and "current" in out, f"exit={code}\n{out}")
    shutil.rmtree(root)

    # Clamp predicate and tap rule must both key off `requested`.
    root = make_tree(thinking_wiring_fixture(clamp_arg="current"))
    code, out = run_scanner("thinking_wiring_guard.py", root)
    check("isCappedBy(current, ...) caught", code == 1 and "isCappedBy" in out, f"exit={code}\n{out}")
    shutil.rmtree(root)

    root = make_tree(thinking_wiring_fixture(tap_arg="current"))
    code, out = run_scanner("thinking_wiring_guard.py", root)
    check("tap keyed off current caught", code == 1 and "thinkingTapTarget" in out, f"exit={code}\n{out}")
    shutil.rmtree(root)

    # Escape hatch clears it.
    root = make_tree(thinking_wiring_fixture(
        requested_arg="effectiveThinkingLevelState",
        requested_binding="val effectiveThinkingLevelState by viewModel.thinkingLevel.collectAsState()",
        escape=True,
    ))
    code, out = run_scanner("thinking_wiring_guard.py", root)
    check("escape hatch clears the finding", code == 0, f"exit={code}\n{out}")
    shutil.rmtree(root)

    # Rot guard: a tree with no picker call must FAIL, not silently pass.
    root = make_tree({"src/android/app/src/main/java/com/rikkaminis/app/Empty.kt":
                      "package com.rikkaminis.app\n"})
    code, out = run_scanner("thinking_wiring_guard.py", root)
    check("missing picker call fails instead of passing silently", code == 1, f"exit={code}\n{out}")
    shutil.rmtree(root)

def main():
    print("╔══════════════════════════════════════════════════╗")
    print("║  Scan Self-Test (fixture ground truth)          ║")
    print("╚══════════════════════════════════════════════════╝")
    test_four_way()
    test_four_way_derived()
    test_four_way_entity_self()
    test_i18n()
    test_enum_parse()
    test_boundary()
    test_legacy()
    test_debug_leak()
    test_room_migration()
    test_process_logging()
    test_trace_eval()
    test_extra_days_container()
    test_stale_closure()
    test_ime_inset()
    test_stream_flow_dispatch()
    test_prefs_listener_holder()
    test_xproc_boundary()
    test_thinking_wiring()
    test_liveness_static()
    test_claim_release()
    test_loop_counter_reset()
    test_budget_stop_break()
    test_katex_bridge_arity()
    test_gemini_base_version()
    test_real_repo()
    print("")
    print(f"{'❌ FAILURES: ' + str(FAIL) if FAIL else '✅ ALL ' + str(PASS) + ' CASES PASS'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

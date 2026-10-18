#!/usr/bin/env python3
"""
provider_rpc_symmetry_check.py — deep-audit-1025 FIX-I gate.

[da1025-I] Provider RPC 层守卫/字段不对称（debug vs mutation）的三条判据：

  C1  customBaseURL 支持谓词单源化：
      - `val ProviderType.supportsCustomBaseURL` 在全仓恰好定义一次，且位于
        ProviderMutationMethods.kt；
      - ProviderDebugMethods.kt / ProviderMutationMethods.kt 的代码（剥注释后）
        不再含内联 `type == ProviderType.openAI || type == ProviderType.anthropic`
        形状（openAI 在前 — supportedCredentials 的 oauth 行是 anthropic 在前，
        不受此判据约束）；
      - typeSpec（Debug）与 instancesCreate 守卫（Mutation）引用
        supportsCustomBaseURL。
  C2  instancesUpdate 守卫：函数体内引用 supportsCustomBaseURL，且含与
      instancesCreate 逐字一致的 openRouter 守卫消息
      "customBaseURL is not supported for providerType=openRouter"。
  C3  instanceToJson（或其委托的 instanceJsonCore）的 put 键集合 ⊇
      instancesList 每实例 JSONObject 的 put 键集合（fail-closed）。

Selftest：≥4 夹具，每条判据至少一个「恰好被它打破」的形态。
用法：
  python3 provider_rpc_symmetry_check.py <repo_root>   # 真树模式（位置参数）
  python3 provider_rpc_symmetry_check.py --selftest    # 夹具模式
退出码：0 = 全绿；1 = 有判据红；2 = 用法错误。
锚点缺失 = 红（fail-closed），不许静默跳过。
"""

import os
import re
import sys
import json
import shutil
import tempfile

DEBUG_FILE = "ProviderDebugMethods.kt"
MUTATION_FILE = "ProviderMutationMethods.kt"
PREDICATE_DEF_PAT = re.compile(r"val\s+ProviderType\.supportsCustomBaseURL")
# openAI-first order: only the customBaseSupported hardcode uses this order.
INLINE_HARDCODE = "type == ProviderType.openAI || type == ProviderType.anthropic"
OPENROUTER_MSG = "customBaseURL is not supported for providerType=openRouter"
SELFTEST_MARKER = "PROVIDER_RPC_SELFTEST=PASS"


def strip_comments(text):
    """Remove // and /* */ comments while respecting string literals and
    char escapes (a naive `//` split would mangle "https://…" string bodies
    and leave brace extraction unbalanced)."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def find_files(root, name, skip=(".git", "build", "node_modules", "probe", "slice")):
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        if name in filenames:
            hits.append(os.path.join(dirpath, name))
    return sorted(hits)


def extract_function(code, name):
    """Extract a block-body function's full text by balanced-brace matching
    (code must already be comment-stripped). Returns None (fail-closed) if
    the declaration or its closing brace cannot be located."""
    m = re.search(r"\bfun\s+" + re.escape(name) + r"\s*\(", code)
    if not m:
        return None
    start = code.find("{", m.end())
    if start < 0:
        return None
    depth = 0
    i, n = start, len(code)
    in_str = False
    while i < n:
        c = code[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return code[start:i + 1]
        i += 1
    return None


def extract_apply_block(block, marker):
    """Extract the JSONObject().apply { … } block that follows `marker`
    (brace-matched). fail-closed on absence."""
    idx = block.find(marker)
    if idx < 0:
        return None
    start = block.find("{", idx + len(marker) - 1)
    if start < 0:
        return None
    depth = 0
    i, n = start, len(block)
    in_str = False
    while i < n:
        c = block[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return block[start:i + 1]
        i += 1
    return None


PUT_KEY_PAT = re.compile(r'put\("([^"]+)"')


def put_keys(block):
    if block is None:
        return None
    return set(PUT_KEY_PAT.findall(block))


def check_tree(root):
    """Returns (rc, detail dict). rc=0 all green."""
    detail = {"c1": [], "c2": [], "c3": [], "fail_closed": []}

    debug_paths = find_files(root, DEBUG_FILE)
    mut_paths = find_files(root, MUTATION_FILE)
    if len(debug_paths) != 1 or len(mut_paths) != 1:
        detail["fail_closed"].append(
            "expected exactly one %s and one %s, found %d/%d"
            % (DEBUG_FILE, MUTATION_FILE, len(debug_paths), len(mut_paths))
        )
        return 1, detail

    with open(debug_paths[0], encoding="utf-8") as f:
        debug_code = strip_comments(f.read())
    with open(mut_paths[0], encoding="utf-8") as f:
        mut_code = strip_comments(f.read())

    # ---- C1 ----
    all_defs = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "build", "node_modules", "probe", "slice")]
        for fn in filenames:
            if fn.endswith(".kt"):
                p = os.path.join(dirpath, fn)
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        code = strip_comments(f.read())
                except OSError:
                    continue
                if PREDICATE_DEF_PAT.search(code):
                    all_defs.append(p)
    if len(all_defs) != 1:
        detail["c1"].append(
            "predicate definition count = %d (expected exactly 1): %s"
            % (len(all_defs), all_defs)
        )
    elif os.path.basename(all_defs[0]) != MUTATION_FILE:
        detail["c1"].append(
            "predicate defined in %s (expected %s)" % (all_defs[0], MUTATION_FILE)
        )
    for label, code in (("debug", debug_code), ("mutation", mut_code)):
        if INLINE_HARDCODE in code:
            detail["c1"].append("inline openAI||anthropic hardcode present in %s file" % label)

    type_spec = extract_function(debug_code, "typeSpec")
    if type_spec is None:
        detail["fail_closed"].append("typeSpec not found in %s" % DEBUG_FILE)
    elif "supportsCustomBaseURL" not in type_spec:
        detail["c1"].append("typeSpec does not reference supportsCustomBaseURL")

    create_body = extract_function(mut_code, "instancesCreate")
    if create_body is None:
        detail["fail_closed"].append("instancesCreate not found in %s" % MUTATION_FILE)
    elif "supportsCustomBaseURL" not in create_body:
        detail["c1"].append("instancesCreate guard does not reference supportsCustomBaseURL")

    # ---- C2 ----
    update_body = extract_function(mut_code, "instancesUpdate")
    if update_body is None:
        detail["fail_closed"].append("instancesUpdate not found in %s" % MUTATION_FILE)
    else:
        if "supportsCustomBaseURL" not in update_body:
            detail["c2"].append("instancesUpdate guard does not reference supportsCustomBaseURL")
        if OPENROUTER_MSG not in update_body:
            detail["c2"].append(
                "instancesUpdate lacks the create-identical openRouter guard message"
            )

    # ---- C3 ----
    list_body = extract_function(debug_code, "instancesList")
    if list_body is None:
        detail["fail_closed"].append("instancesList not found in %s" % DEBUG_FILE)
        per_instance_keys = None
    else:
        per_inst_block = extract_apply_block(list_body, "arr.put(JSONObject().apply {")
        if per_inst_block is None:
            detail["fail_closed"].append("per-instance JSONObject().apply block not found in instancesList")
            per_instance_keys = None
        else:
            per_instance_keys = put_keys(per_inst_block)
            if not per_instance_keys:
                detail["fail_closed"].append("no put( keys found in instancesList per-instance block")
                per_instance_keys = None

    to_json_body = extract_function(mut_code, "instanceToJson")
    if to_json_body is None:
        detail["fail_closed"].append("instanceToJson not found in %s" % MUTATION_FILE)
        mut_keys = None
    elif "instanceJsonCore(" in to_json_body:
        core_body = extract_function(mut_code, "instanceJsonCore")
        if core_body is None:
            detail["fail_closed"].append(
                "instanceToJson delegates to instanceJsonCore but its body was not found"
            )
            mut_keys = None
        else:
            marker = ("return JSONObject().apply {"
                      if "return JSONObject().apply {" in core_body
                      else "JSONObject().apply {")
            core_block = extract_apply_block(core_body, marker)
            if core_block is None:
                # Expression-body function: extract_function already returned
                # exactly the JSONObject().apply { … } block.
                core_block = core_body
            mut_keys = put_keys(core_block)
            if not mut_keys:
                detail["fail_closed"].append("no put( keys found in instanceJsonCore apply block")
                mut_keys = None
    else:
        mut_keys = put_keys(to_json_body)
        if not mut_keys:
            detail["fail_closed"].append("no put( keys found in instanceToJson body")
            mut_keys = None

    if per_instance_keys is not None and mut_keys is not None:
        missing = per_instance_keys - mut_keys
        if missing:
            detail["c3"].append(
                "instanceToJson put-keys missing %s (required by instancesList)"
                % sorted(missing)
            )

    rc = 0
    for k in ("c1", "c2", "c3", "fail_closed"):
        if detail[k]:
            rc = 1
    return rc, detail


# ---------------------------------------------------------------------------
# Selftest fixtures — each breaks exactly one criterion.
# ---------------------------------------------------------------------------

GOOD_DEBUG = '''package com.rikkaminis.app.debug

import com.rikkaminis.app.data.model.ProviderType
import org.json.JSONArray
import org.json.JSONObject

internal object ProviderDebugMethods {

    fun types(): JSONObject {
        val arr = JSONArray()
        for (type in ProviderType.values()) {
            arr.put(typeSpec(type))
        }
        return JSONObject().put("types", arr)
    }

    private fun typeSpec(type: ProviderType): JSONObject {
        val customBaseSupported = type.supportsCustomBaseURL
        return JSONObject().apply {
            put("id", type.name)
            put("customBaseURLSupported", customBaseSupported)
        }
    }

    fun instancesList(context: android.content.Context, params: JSONObject): JSONObject {
        val arr = JSONArray()
        for (inst in listOf<ProviderInstance>()) {
            arr.put(JSONObject().apply {
                put("id", inst.id)
                put("label", inst.label)
                put("providerType", inst.providerType.name)
                put("credentialType", inst.credentialType.name)
                put("isEnabled", inst.isEnabled)
                put("customBaseURL", inst.customBaseURL ?: JSONObject.NULL)
                put("appendV1Suffix", inst.appendV1Suffix)
                put("useResponsesAPI", inst.useResponsesAPI)
                put("imageEndpointMode", inst.imageEndpointMode.name)
                put("imageEndpointResolved", inst.imageEndpointResolved?.name ?: JSONObject.NULL)
                put("customUserAgent", inst.customUserAgent ?: JSONObject.NULL)
                put("createdAt", inst.createdAt)
                put("hasCredential", false)
                put("modelEntryCount", 0)
            })
        }
        return JSONObject().apply {
            put("count", arr.length())
            put("instances", arr)
        }
    }
}
'''

GOOD_MUTATION = '''package com.rikkaminis.app.debug

import com.rikkaminis.app.data.model.ProviderType
import org.json.JSONObject

internal object ProviderMutationMethods {

    fun instancesCreate(context: android.content.Context, params: JSONObject): JSONObject {
        val type = ProviderType.openAI
        val customBaseURL: String? = null
        // [da1023-C] guard comment (verbatim-ish)
        if (customBaseURL != null && !type.supportsCustomBaseURL) {
            throw RPCException(-32602, "customBaseURL is not supported for providerType=openRouter")
        }
        return JSONObject()
    }

    fun instancesUpdate(context: android.content.Context, params: JSONObject): JSONObject {
        val current = ProviderInstanceFix()
        if (params.has("customBaseURL") && !params.isNull("customBaseURL")) {
            val newBase = params.optString("customBaseURL", "").ifEmpty { null }
            if (newBase != null && !current.providerType.supportsCustomBaseURL) {
                throw RPCException(-32602, "customBaseURL is not supported for providerType=openRouter")
            }
        }
        return instanceToJson(current)
    }

    private fun instanceToJson(inst: com.rikkaminis.app.data.model.ProviderInstanceStub): JSONObject {
        return instanceJsonCore(inst, false, 0)
    }
}

internal fun instanceJsonCore(inst: ProviderInstanceFix, hasCredential: Boolean, entryCount: Int): JSONObject =
    JSONObject().apply {
        put("id", inst.id)
        put("label", inst.label)
        put("providerType", inst.providerType.name)
        put("credentialType", inst.credentialType.name)
        put("isEnabled", inst.isEnabled)
        put("customBaseURL", inst.customBaseURL ?: JSONObject.NULL)
        put("appendV1Suffix", inst.appendV1Suffix)
        put("useResponsesAPI", inst.useResponsesAPI)
        put("imageEndpointMode", inst.imageEndpointMode.name)
        put("imageEndpointResolved", inst.imageEndpointResolved?.name ?: JSONObject.NULL)
        put("customUserAgent", inst.customUserAgent ?: JSONObject.NULL)
        put("createdAt", inst.createdAt)
        put("hasCredential", hasCredential)
        put("modelEntryCount", entryCount)
    }

internal val ProviderType.supportsCustomBaseURL: Boolean
    get() = this != ProviderType.openRouter
'''

# Minimal stand-ins so the fixture compiles conceptually (never compiled here);
# the gate only reads text.
GOOD_MUTATION = GOOD_MUTATION.replace("ProviderInstanceFix", "com.rikkaminis.app.data.model.ProviderInstanceStub")


def write_fixture(debug_text, mutation_text):
    root = tempfile.mkdtemp(prefix="rpcsym-")
    os.makedirs(os.path.join(root, "src", "debug"))
    with open(os.path.join(root, "src", "debug", DEBUG_FILE), "w", encoding="utf-8") as f:
        f.write(debug_text)
    with open(os.path.join(root, "src", "debug", MUTATION_FILE), "w", encoding="utf-8") as f:
        f.write(mutation_text)
    return root


def run_selftest():
    results = []

    # Contrast: good pair must be fully green.
    root = write_fixture(GOOD_DEBUG, GOOD_MUTATION)
    rc, detail = check_tree(root)
    results.append(("CONTRAST-good-green", rc == 0, detail))
    shutil.rmtree(root)

    # F1: typeSpec back to inline hardcode → only C1 red.
    bad = GOOD_DEBUG.replace(
        "val customBaseSupported = type.supportsCustomBaseURL",
        "val customBaseSupported = " + INLINE_HARDCODE,
    )
    root = write_fixture(bad, GOOD_MUTATION)
    rc, detail = check_tree(root)
    ok = rc == 1 and detail["c1"] and not detail["c2"] and not detail["c3"] and not detail["fail_closed"]
    results.append(("F1-inline-hardcode-C1", ok, detail))
    shutil.rmtree(root)

    # F2: update guard deleted entirely → only C2 red.
    bad = GOOD_MUTATION.replace(
        '''        if (params.has("customBaseURL") && !params.isNull("customBaseURL")) {
            val newBase = params.optString("customBaseURL", "").ifEmpty { null }
            if (newBase != null && !current.providerType.supportsCustomBaseURL) {
                throw RPCException(-32602, "customBaseURL is not supported for providerType=openRouter")
            }
        }
''',
        "",
    )
    root = write_fixture(GOOD_DEBUG, bad)
    rc, detail = check_tree(root)
    ok = rc == 1 and detail["c2"] and not detail["c1"] and not detail["c3"] and not detail["fail_closed"]
    results.append(("F2-update-guard-deleted-C2", ok, detail))
    shutil.rmtree(root)

    # F3: imageEndpoint keys removed from instanceJsonCore → only C3 red.
    bad = GOOD_MUTATION.replace(
        '''        put("imageEndpointMode", inst.imageEndpointMode.name)
        put("imageEndpointResolved", inst.imageEndpointResolved?.name ?: JSONObject.NULL)
''',
        "",
    )
    root = write_fixture(GOOD_DEBUG, bad)
    rc, detail = check_tree(root)
    ok = rc == 1 and detail["c3"] and not detail["c1"] and not detail["c2"] and not detail["fail_closed"]
    results.append(("F3-imageEndpoint-keys-removed-C3", ok, detail))
    shutil.rmtree(root)

    # F4: update guard message changed → only C2 red (predicate ref still there).
    bad = GOOD_MUTATION.replace(
        'throw RPCException(-32602, "customBaseURL is not supported for providerType=openRouter")\n            }\n        }\n        return instanceToJson',
        'throw RPCException(-32602, "customBaseURL not allowed here")\n            }\n        }\n        return instanceToJson',
        1,
    )
    root = write_fixture(GOOD_DEBUG, bad)
    rc, detail = check_tree(root)
    ok = rc == 1 and detail["c2"] and not detail["c1"] and not detail["c3"] and not detail["fail_closed"]
    results.append(("F4-guard-message-changed-C2", ok, detail))
    shutil.rmtree(root)

    # F5: predicate definition removed → only C1 red.
    bad = GOOD_MUTATION.replace(
        '''internal val ProviderType.supportsCustomBaseURL: Boolean
    get() = this != ProviderType.openRouter
''',
        "",
    )
    root = write_fixture(GOOD_DEBUG, bad)
    rc, detail = check_tree(root)
    ok = rc == 1 and detail["c1"] and not detail["c2"] and not detail["c3"] and not detail["fail_closed"]
    results.append(("F5-predicate-def-removed-C1", ok, detail))
    shutil.rmtree(root)

    # F6: fail-closed — instanceToJson missing entirely.
    bad = GOOD_MUTATION.replace("private fun instanceToJson(", "private fun instanceToJsonX(")
    bad = bad.replace("return instanceToJson(current)", "return instanceToJsonX(current)")
    root = write_fixture(GOOD_DEBUG, bad)
    rc, detail = check_tree(root)
    ok = rc == 1 and detail["fail_closed"]
    results.append(("F6-fail-closed-missing-json", ok, detail))
    shutil.rmtree(root)

    all_ok = True
    for name, ok, detail in results:
        print("%s %s" % ("PASS" if ok else "FAIL", name))
        if not ok:
            all_ok = False
            print(json.dumps(detail, indent=2))
    if all_ok:
        print(SELFTEST_MARKER)
        return 0
    return 1


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        return run_selftest()
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = sys.argv[1]
    if not os.path.isdir(root):
        print("repo root not a directory: %s" % root)
        return 2
    rc, detail = check_tree(root)
    if rc == 0:
        print("provider_rpc_symmetry: ALL GREEN (C1 predicate single-source, "
              "C2 update guard, C3 key symmetry)")
        return 0
    for k in ("c1", "c2", "c3", "fail_closed"):
        for line in detail[k]:
            print("[%s] %s" % (k.upper(), line))
    return 1


if __name__ == "__main__":
    sys.exit(main())

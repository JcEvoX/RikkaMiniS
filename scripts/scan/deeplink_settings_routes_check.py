#!/usr/bin/env python3
"""da1020-F gate: deep-link settings routes + route-segment encoding.

Usage (repo root, positional arg supported):
    python3 scripts/scan/deeplink_settings_routes_check.py [ROOT]

PASS  = exit 0, prints DEEPLINK_SETTINGS_ROUTES_SELFTEST=PASS after --selftest
FAIL  = exit 1, prints violating 文件:行

Judged invariants:
  1. The settings-path whitelist covers the EMITTER list (minis://settings/
     links the app itself emits, incl. the MCP OAuth round-trip emitted by
     minis-mcp-cli transport/http.py).
  2. Every parameterised Routes segment builder encodes through the single
     helper (seg()) — a raw id would hand nav.navigate a malformed route
     (uncaught IllegalArgumentException).
  3. The unknown-path fallback to Settings home is still present
     (deliberate design — must not regress into an error).
  4. MainActivity's deep-link navigate has a runCatching backstop.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Emitter list: minis://settings/<head> links the app itself emits.
# Sources (extracted 2026-10-17 from main 942b0111d):
#   - src/android/app/src/main/assets/default_mount/usr/local/lib/minis-mcp-cli/transport/http.py:140
#       "[Authorize](minis://settings/mcp-servers/%s)"
#   - Kotlin prompt strings (ChatPromptAndTools.kt / ConfigBridge.kt /
#     EnvVarsCollection.kt / ProvidersCollection.kt):
#       minis://settings/{permissions,environments,memory}
# Re-extract with:
#   find src -type f \( -name '*.kt' -o -name '*.py' \) | xargs grep -rn "minis://settings"
EMITTER_HEADS = ["mcp-servers", "permissions", "environments", "memory"]

REL_DLH = "src/android/app/src/main/java/com/rikkaminis/app/deeplink/DeepLinkHandler.kt"
REL_NAV = "src/android/app/src/main/java/com/rikkaminis/app/ui/navigation/AppNavigation.kt"
REL_MAIN = "src/android/app/src/main/java/com/rikkaminis/app/MainActivity.kt"
REL_HTTP_PY = "src/android/app/src/main/assets/default_mount/usr/local/lib/minis-mcp-cli/transport/http.py"

# Parameterised Routes segment builders that MUST encode their id.
SEG_BUILDERS = [
    "providerDetail", "providerConnection", "modelGroupDetail",
    "addModelsToGroup", "addCustomModel", "modelEntryDetail",
]

VIOLATIONS = []


def code_only(src: str) -> str:
    """Strip comment lines (// line comments, /* */ and KDoc blocks) so a
    gate key living only in a comment cannot fake a pass (COMMON §4)."""
    out = []
    in_block = False
    for line in src.split("\n"):
        s = line.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block = True
            continue
        if s.startswith("*") or s.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def fail(path, msg):
    VIOLATIONS.append(f"{path}: {msg}")


def read(root: Path, rel: str) -> str:
    p = root / rel
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def check(root: Path) -> None:
    dlh = code_only(read(root, REL_DLH))
    nav = code_only(read(root, REL_NAV))
    main = code_only(read(root, REL_MAIN))
    if not dlh:
        fail(REL_DLH, "missing source file")
        return
    if not nav:
        fail(REL_NAV, "missing source file")
    if not main:
        fail(REL_MAIN, "missing source file")

    # --- 1. emitter whitelist coverage ---
    for head in EMITTER_HEADS:
        if head not in dlh:
            fail(REL_DLH, f"emitter head '{head}' not covered by the settings whitelist")
    # live cross-check against the actual Python emitter (SKIP-able: the gate
    # must not depend on asset directory structure at runtime)
    http_py = read(root, REL_HTTP_PY)
    if http_py:
        emitted = sorted(set(re.findall(r"minis://settings/([a-zA-Z0-9_\-]+)", http_py)))
        for head in emitted:
            if head not in dlh:
                fail(REL_HTTP_PY, f"emitted head '{head}' not covered by the settings whitelist")
    else:
        print(f"SKIP: emitter cross-check — {REL_HTTP_PY} not found (hardcoded list still enforced)")

    # --- 2. segment builders encode through the single helper ---
    for name in SEG_BUILDERS:
        m = re.search(rf"fun {name}\(", nav)
        if not m:
            fail(REL_NAV, f"Routes.{name} builder not found")
            continue
        # definition body = from `fun name(` up to the next member boundary
        nxt = re.search(r"\n    fun |\n\}", nav[m.end():])
        body_end = m.end() + nxt.start() if nxt else len(nav)
        if nav[m.start():body_end].count("seg(") == 0:
            fail(REL_NAV, f"Routes.{name} does not encode through seg() (raw id -> malformed route)")

    # --- 3. fallback still present (deliberate design) ---
    if "else -> SettingsDeepLink.Route(Routes.SETTINGS)" not in dlh:
        fail(REL_DLH, "unknown-path fallback to Routes.SETTINGS is missing (deliberate design must not regress)")

    # --- 4. navigate backstop in MainActivity ---
    if "runCatching { nav.navigate(action.route) }" not in main:
        fail(REL_MAIN, "OpenSettingsScreen navigate lacks the runCatching backstop")


# ---------------- selftest fixtures (minimal synthetic repo) ----------------

FIXTURE_DLH = '''package com.rikkaminis.app.deeplink

internal fun resolveSettingsDeepLink(path: String, query: Map<String, String>): SettingsDeepLink {
    val segments = path.trimStart('/').split('/').filter { it.isNotEmpty() }
    val head = segments.firstOrNull() ?: return SettingsDeepLink.Route(Routes.SETTINGS)
    return when (head) {
        "providers" -> SettingsDeepLink.Route(Routes.providerDetail(head))
        "mcp-servers", "mcp_servers" -> SettingsDeepLink.Route(Routes.MCP)
        "permissions" -> SettingsDeepLink.PermissionSettings
        "environments" -> SettingsDeepLink.Route(Routes.ENV_VARS)
        "memory" -> SettingsDeepLink.Route(Routes.MEMORY)
        else -> SettingsDeepLink.Route(Routes.SETTINGS)
    }
}
'''

FIXTURE_NAV = '''package com.rikkaminis.app.ui.navigation

object Routes {
    internal fun seg(v: String): String = android.net.Uri.encode(v)
    fun providerDetail(instanceId: String) = "provider/${seg(instanceId)}"
    fun providerConnection(instanceId: String) = "provider_connection/${seg(instanceId)}"
    fun modelGroupDetail(groupId: String) = "model_group/${seg(groupId)}"
    fun addModelsToGroup(groupId: String) = "add_models_to_group/${seg(groupId)}"
    fun addCustomModel(instanceId: String) = "add_custom_model/${seg(instanceId)}"
    fun modelEntryDetail(instanceId: String, entryId: String) =
        "model_entry/${seg(instanceId)}/${seg(entryId)}"
}
'''

FIXTURE_MAIN = '''package com.rikkaminis.app

private fun handleDeepLink() {
    runCatching { nav.navigate(action.route) }
        .onFailure { nav.navigate(Routes.SETTINGS) }
}
'''


def write_fixture(root: Path, dlh: str, nav: str, main: str) -> None:
    for rel, content in ((REL_DLH, dlh), (REL_NAV, nav), (REL_MAIN, main)):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def selftest() -> None:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # compliant fixture must pass
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if VIOLATIONS:
            ok = False
            print(f"SELFTEST compliant fixture unexpectedly failed: {VIOLATIONS}")

        # fixture B: drop the mcp-servers whitelist entry -> must fail
        write_fixture(root, FIXTURE_DLH.replace('"mcp-servers", "mcp_servers" ->', '->'), FIXTURE_NAV, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("mcp-servers" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture B (drop mcp-servers) did not trip the gate")
        else:
            print("SELFTEST B (drop whitelist entry) RED as expected")

        # fixture C: remove the seg() encoding from providerDetail -> must fail
        bad_nav = FIXTURE_NAV.replace('"provider/${seg(instanceId)}"', '"provider/$instanceId"')
        write_fixture(root, FIXTURE_DLH, bad_nav, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("providerDetail" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture C (raw providerDetail) did not trip the gate")
        else:
            print("SELFTEST C (drop encode) RED as expected")

        # fixture D: fallback turned into an error -> must fail
        bad_dlh = FIXTURE_DLH.replace("else -> SettingsDeepLink.Route(Routes.SETTINGS)", "else -> SettingsDeepLink.Unknown")
        write_fixture(root, bad_dlh, FIXTURE_NAV, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("fallback" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture D (fallback removed) did not trip the gate")
        else:
            print("SELFTEST D (fallback removed) RED as expected")

        # fixture E: navigate backstop removed -> must fail
        bad_main = FIXTURE_MAIN.replace("runCatching { nav.navigate(action.route) }", "nav.navigate(action.route)")
        write_fixture(root, FIXTURE_DLH, FIXTURE_NAV, bad_main)
        VIOLATIONS.clear()
        check(root)
        if not any("runCatching" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture E (backstop removed) did not trip the gate")
        else:
            print("SELFTEST E (backstop removed) RED as expected")

    if not ok:
        sys.exit("DEEPLINK_SETTINGS_ROUTES_SELFTEST=FAIL")


def main() -> None:
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
        print("DEEPLINK_SETTINGS_ROUTES_SELFTEST=PASS")
        return
    root = Path(args[0]) if args else Path.cwd()
    if not root.exists():
        sys.exit(f"FAIL: root {root} does not exist")
    check(root)
    if VIOLATIONS:
        print(f"FAIL: {len(VIOLATIONS)} violation(s)")
        for v in VIOLATIONS:
            print(f"  {v}")
        sys.exit(1)
    print("DEEPLINK_SETTINGS_ROUTES_CHECK=PASS")


if __name__ == "__main__":
    main()

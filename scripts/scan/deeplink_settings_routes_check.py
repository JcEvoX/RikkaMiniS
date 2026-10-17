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
  5. [da1021-C] AppNavigation's cold-start deep-link navigate keeps its
     runCatching backstop.
  6. [da1024-E] No composable block decodes a route argument itself: the Nav
     framework %-decodes each path segment exactly once
     (NavDeepLink.getMatchingPathArguments -> Uri.decode), so a second decode
     rewrites ids carrying a literal '%'. Uri.encode stays legal (builders).
     Scope = the composable blocks, which is where route args are read; a
     decode added elsewhere in this file is out of scope by design (see the
     branch report's negative decisions).
  7. [da1024-E] The MODEL_ENTRY_DETAIL block still reads both args via
     getString and keeps the `?: return@composable` fail-closed guard.
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
# [da1021-C] Extended to ALL 17 path-segment builders (the 6 da1020-F ones +
# the 10 that were still raw-interpolated + the new mirrorCategoryDetail).
# Excluded on purpose: fileBrowser / terminal / filePreview encode through
# their own local `enc` helpers (same android.net.Uri.encode semantics, no
# seg() dependency).
SEG_BUILDERS = [
    "providerDetail", "providerConnection", "modelGroupDetail",
    "addModelsToGroup", "addCustomModel", "modelEntryDetail",
    "scheduledTaskEdit", "skillDetail", "skillFile", "chatFiles",
    "mountedFoldersDetail", "sharedFoldersDetail", "logDetail",
    "sessionStorageDetail", "memoryFileEdit", "chat",
    "mirrorCategoryDetail",
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

# [da1024-E] A second decode of a route argument. Builder-side Uri.encode is
# NOT covered (the nav graph must keep encoding when it builds a route).
DECODE_RE = re.compile(r"(?:android\.net\.|java\.net\.)?(?:Uri|URLDecoder)\.decode\s*\(")

def is_comment_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("//") or s.startswith("*") or s.startswith("/*")

def code_lines(src: str):
    """(1-based lineno, text) for every non-comment line.

    code_only() drops comment lines, so it shifts line numbers; the criteria
    below report positions a human can grep, so they walk the raw lines and
    skip comments themselves.
    """
    return [(i + 1, l) for i, l in enumerate(src.split("\n")) if not is_comment_line(l)]

def composable_blocks(indexed):
    """[(start_lineno, [(lineno, text), ...])] for every `composable(...) { }`.

    A block ends at the first following `}` that sits at the opener's own
    indentation, so the scan cannot bleed into the next composable's body.
    """
    blocks = []
    i = 0
    while i < len(indexed):
        lineno, line = indexed[i]
        if line.strip().startswith("composable("):
            indent = len(line) - len(line.lstrip())
            j = i + 1
            while j < len(indexed):
                text = indexed[j][1]
                if text.strip() == "}" and (len(text) - len(text.lstrip())) == indent:
                    break
                j += 1
            blocks.append((lineno, indexed[i:j + 1]))
            i = j + 1
        else:
            i += 1
    return blocks


def read(root: Path, rel: str) -> str:
    p = root / rel
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def check(root: Path) -> None:
    dlh = code_only(read(root, REL_DLH))
    nav_raw = read(root, REL_NAV)
    nav = code_only(nav_raw)
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

    # --- 5. [da1021-C] cold-start deep-link backstop in AppNavigation ---
    # The cold-start LaunchedEffect(initialDeepLink) branch runs after
    # awaitResumed(), so bare navigate() is safe there (da1020-F reasoning);
    # the runCatching is the never-crash backstop mirroring MainActivity.
    if "runCatching { navController.navigate(initialDeepLink.route) }" not in nav:
        fail(REL_NAV, "cold-start OpenSettingsScreen deep link lacks the runCatching backstop")

    # --- 6. [da1024-E] no second decode of a route argument ---
    # The Nav framework %-decodes every route path segment exactly once
    # (navigation-common 2.8.5 NavDeepLink.getMatchingPathArguments ->
    # `Uri.decode(matcher.group(index + 1))`). Anything the nav graph decodes
    # itself is therefore a SECOND decode: it rewrites an id carrying a literal
    # '%' ("100%25" -> "100%") and turns a bare '%' into U+FFFD — on Android
    # 13+ Uri.decode substitutes the replacement character instead of throwing
    # (android.net.UriCodec.decode(..., throwOnFailure = false)) — so the
    # destination's lookup misses and the screen pops straight back.
    for start, block in composable_blocks(code_lines(nav_raw)):
        for lineno, text in block:
            if DECODE_RE.search(text):
                fail(REL_NAV,
                     f"composable at line {start} decodes a route argument "
                     f"(line {lineno}): the Nav framework already "
                     f"%-decodes each path segment once")

    # --- 7. [da1024-E] MODEL_ENTRY_DETAIL reads both args, fail-closed ---
    entry_blocks = [bl for _, bl in composable_blocks(code_lines(nav_raw))
                    if any("Routes.MODEL_ENTRY_DETAIL" in t for _, t in bl)]
    if not entry_blocks:
        fail(REL_NAV, "MODEL_ENTRY_DETAIL composable block not found")
    else:
        blk = "\n".join(t for _, t in entry_blocks[0])
        for arg in ("instanceId", "entryId"):
            if f'getString("{arg}")' not in blk:
                fail(REL_NAV, f"MODEL_ENTRY_DETAIL does not read '{arg}' via getString")
        if blk.count("?: return@composable") < 2:
            fail(REL_NAV,
                 "MODEL_ENTRY_DETAIL lost its `?: return@composable` fail-closed guard")


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
    fun scheduledTaskEdit(taskId: String?) =
        if (taskId == null) "scheduled_task_edit/-" else "scheduled_task_edit/${seg(taskId)}"
    fun skillDetail(skillId: String) = "skill/${seg(skillId)}"
    fun skillFile(skillId: String, relativePath: String): String {
        val encoded = relativePath.replace("+", "%20")
        return "skill_file/${seg(skillId)}/$encoded"
    }
    fun chatFiles(sessionId: String) = "chat_files/${seg(sessionId)}"
    fun mountedFoldersDetail(mountId: String) = "mounted_folders_detail/${seg(mountId)}"
    fun sharedFoldersDetail(folderId: String) = "shared_folders_detail/${seg(folderId)}"
    fun logDetail(fileName: String) = "log_detail/${seg(fileName)}"
    fun sessionStorageDetail(sessionId: String) = "session_storage/${seg(sessionId)}"
    fun memoryFileEdit(fileName: String, isGlobal: Boolean) = "memory_file/${seg(fileName)}/$isGlobal"
    fun chat(sessionId: String, focusMessageId: String? = null): String {
        val base = "chat/${seg(sessionId)}"
        if (focusMessageId.isNullOrBlank()) return base
        return "$base?focusMessageId=$focusMessageId"
    }
    fun mirrorCategoryDetail(categoryId: String) = "mirror_category/${seg(categoryId)}"
}

suspend fun handleColdStartDeepLink(navController: NavController, initialDeepLink: DeepLinkAction) {
    when (initialDeepLink) {
        is DeepLinkAction.OpenSettingsScreen -> {
            runCatching { navController.navigate(initialDeepLink.route) }
                .onFailure { runCatching { navController.navigate(Routes.SETTINGS) } }
        }
        else -> {}
    }
}

fun NavGraphBuilder.modelEntryDetailGraph(navController: NavController) {
    composable(
        route = Routes.MODEL_ENTRY_DETAIL,
        arguments = listOf(
            navArgument("instanceId") { type = NavType.StringType },
            navArgument("entryId") { type = NavType.StringType },
        ),
    ) { backStackEntry ->
        val instanceId = backStackEntry.arguments?.getString("instanceId")
            ?: return@composable
        val entryId = backStackEntry.arguments?.getString("entryId")
            ?: return@composable
        ModelEntryDetailScreen(instanceId = instanceId, entryId = entryId)
    }
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

        # fixture F: [da1021-C] cold-start backstop removed -> must fail
        bad_nav_cold = FIXTURE_NAV.replace(
            "runCatching { navController.navigate(initialDeepLink.route) }",
            "navController.navigate(initialDeepLink.route)",
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_cold, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("cold-start" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture F (cold-start backstop removed) did not trip the gate")
        else:
            print("SELFTEST F (cold-start backstop removed) RED as expected")

        # fixture G: [da1021-C] raw mirrorCategoryDetail -> must fail
        bad_nav_mirror = FIXTURE_NAV.replace(
            '"mirror_category/${seg(categoryId)}"', '"mirror_category/$categoryId"'
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_mirror, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not any("mirrorCategoryDetail" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture G (raw mirrorCategoryDetail) did not trip the gate")
        else:
            print("SELFTEST G (raw mirrorCategoryDetail) RED as expected")

        # fixture H: [da1024-E] ONE second decode back inside the composable
        # block -> exactly one decode violation, and the fail-closed guard
        # survives (so criterion 7 must stay green: each criterion has a
        # fixture that breaks it and one that does not).
        bad_nav_h = FIXTURE_NAV.replace(
            'val instanceId = backStackEntry.arguments?.getString("instanceId")\n'
            '            ?: return@composable',
            'val instanceId = backStackEntry.arguments?.getString("instanceId")\n'
            '            ?.let { android.net.Uri.decode(it) } ?: return@composable',
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_h, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        dec = [v for v in VIOLATIONS if "decodes a route argument" in v]
        guard = [v for v in VIOLATIONS if "fail-closed guard" in v]
        if len(dec) != 1 or guard:
            ok = False
            print(f"SELFTEST fixture H (one second decode) wrong verdict: {VIOLATIONS}")
        else:
            print("SELFTEST H (one second decode) RED as expected")

        # fixture I: both args decoded again -> two decode violations (the
        # criterion counts sites, it is not satisfied by the first hit).
        bad_nav_i = bad_nav_h.replace(
            'val entryId = backStackEntry.arguments?.getString("entryId")\n'
            '            ?: return@composable',
            'val entryId = backStackEntry.arguments?.getString("entryId")\n'
            '            ?.let { android.net.Uri.decode(it) } ?: return@composable',
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_i, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        dec = [v for v in VIOLATIONS if "decodes a route argument" in v]
        if len(dec) != 2:
            ok = False
            print(f"SELFTEST fixture I (both decoded again) expected 2 violations, got {len(dec)}")
        else:
            print("SELFTEST I (both decoded again) RED as expected")

        # fixture I2: URLDecoder variant of the same defect -> still RED.
        bad_nav_i2 = bad_nav_h.replace(
            "?.let { android.net.Uri.decode(it) } ?: return@composable",
            '?.let { java.net.URLDecoder.decode(it, "UTF-8") } ?: return@composable',
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_i2, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        if not [v for v in VIOLATIONS if "decodes a route argument" in v]:
            ok = False
            print("SELFTEST fixture I2 (URLDecoder double decode) did not trip the gate")
        else:
            print("SELFTEST I2 (URLDecoder double decode) RED as expected")

        # fixture J: fail-closed guard dropped (no decode at all) -> only
        # criterion 7 may fire.
        bad_nav_j = FIXTURE_NAV.replace(
            'arguments?.getString("instanceId")\n            ?: return@composable',
            'arguments?.getString("instanceId") ?: ""',
        ).replace(
            'arguments?.getString("entryId")\n            ?: return@composable',
            'arguments?.getString("entryId") ?: ""',
        )
        write_fixture(root, FIXTURE_DLH, bad_nav_j, FIXTURE_MAIN)
        VIOLATIONS.clear()
        check(root)
        dec = [v for v in VIOLATIONS if "decodes a route argument" in v]
        guard = [v for v in VIOLATIONS if "fail-closed guard" in v]
        if not guard or dec:
            ok = False
            print(f"SELFTEST fixture J (guard dropped) wrong verdict: {VIOLATIONS}")
        else:
            print("SELFTEST J (fail-closed guard dropped) RED as expected")

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

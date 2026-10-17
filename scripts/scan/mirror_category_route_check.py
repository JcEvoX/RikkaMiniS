#!/usr/bin/env python3
"""da1021-C gate: mirror category detail route wiring.

Usage (repo root, positional arg supported):
    python3 scripts/scan/mirror_category_route_check.py [ROOT]

PASS  = exit 0, prints MIRROR_CATEGORY_ROUTE_CHECK=PASS
        (--selftest prints MIRROR_CATEGORY_ROUTE_SELFTEST=PASS)
FAIL  = exit 1, prints violating 文件:行

Judged invariants (fix for "MirrorCategoryDetailScreen had zero call sites"):
  1. `MirrorCategoryDetailScreen(` appears at least twice in the production
     source set (definition + navigation call) — the screen is wired.
  2. AppNavigation's ROOTFS_MANAGEMENT composable passes
     `onMirrorCategoryClick` and navigates via Routes.mirrorCategoryDetail.
  3. MirrorCategory has a key-based resolver (`fromKey`) that returns null on
     an unknown key (fail-closed) — the navigation layer backs out instead of
     crashing on a hand-typed deep link.
  4. The route pattern + builder encode the categoryId through seg().
"""
import re
import sys
import tempfile
from pathlib import Path

REL_NAV = "src/android/app/src/main/java/com/rikkaminis/app/ui/navigation/AppNavigation.kt"
PROD_KT_GLOB = "src/android/app/src/main/**/*.kt"
REL_CAT = "src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/MirrorCategory.kt"

VIOLATIONS = []


def code_only(src: str) -> str:
    """Strip comment lines (// line comments, /* */ and KDoc blocks) so a
    gate key living only in a comment cannot fake a pass (COMMON §3)."""
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


def check(root: Path) -> None:
    # --- arm 1: MirrorCategoryDetailScreen defined AND called (≥2 in prod) ---
    hits = sorted(p for p in root.glob(PROD_KT_GLOB)
                  if "MirrorCategoryDetailScreen(" in code_only(p.read_text(encoding="utf-8")))
    if len(hits) < 2:
        fail("production sources", "MirrorCategoryDetailScreen( appears fewer than 2 times "
             f"(definition + navigation call) — found in {hits or 'nothing'}")

    nav_path = root / REL_NAV
    if not nav_path.exists():
        fail(REL_NAV, "missing source file")
        return
    nav = code_only(nav_path.read_text(encoding="utf-8"))

    # --- arm 2: ROOTFS_MANAGEMENT composable wires onMirrorCategoryClick ---
    if "onMirrorCategoryClick" not in nav:
        fail(REL_NAV, "ROOTFS_MANAGEMENT composable does not pass onMirrorCategoryClick")
    if "Routes.mirrorCategoryDetail(" not in nav:
        fail(REL_NAV, "onMirrorCategoryClick does not navigate via Routes.mirrorCategoryDetail")

    # --- arm 3: fromKey resolver exists, fail-closed (null on unknown key) ---
    cat_path = root / REL_CAT
    if cat_path.exists():
        cat = code_only(cat_path.read_text(encoding="utf-8"))
    else:
        # fallback: search the production tree for the resolver
        cat = ""
        for p in root.glob(PROD_KT_GLOB):
            t = code_only(p.read_text(encoding="utf-8"))
            if "fromKey(" in t and "MirrorCategory" in t:
                cat = t
                REL_CAT_FALLBACK = str(p.relative_to(root))
                break
    if not cat:
        fail(REL_CAT, "MirrorCategory source (with fromKey resolver) not found")
    else:
        if "fun fromKey(" not in cat:
            fail(REL_CAT, "MirrorCategory.fromKey resolver missing (route param cannot resolve)")
        else:
            m = re.search(r"fun fromKey\([^)]*\)([^=]*)=(.*)", cat)
            ret = m.group(0) if m else ""
            if "MirrorCategory?" not in ret:
                fail(REL_CAT, "fromKey must return MirrorCategory? (fail-closed on unknown key)")
            if "firstOrNull" not in cat:
                fail(REL_CAT, "fromKey must resolve via firstOrNull (unknown key -> null)")

    # --- arm 4: route pattern + builder encode through seg() ---
    if 'const val MIRROR_CATEGORY_DETAIL = "mirror_category/{categoryId}"' not in nav:
        fail(REL_NAV, "Routes.MIRROR_CATEGORY_DETAIL pattern missing")
    if 'fun mirrorCategoryDetail(categoryId: String) = "mirror_category/${seg(categoryId)}"' not in nav:
        fail(REL_NAV, "Routes.mirrorCategoryDetail does not encode through seg()")


# ---------------- selftest fixtures (minimal synthetic repo) ----------------

FIXTURE_CAT = '''package com.rikkaminis.app.ui.sandbox

enum class MirrorCategory(
    val key: String,
    val displayName: String,
    val configPath: String,
) {
    ALPINE("alpine", "Alpine APK", "etc/apk/repositories"),
    PIP("pip", "Python pip", "etc/pip/pip.conf"),
    NPM("npm", "Node.js npm", "root/.npmrc");

    companion object {
        fun fromKey(key: String): MirrorCategory? =
            entries.firstOrNull { it.key == key }
    }
}
'''

FIXTURE_SCREEN = '''package com.rikkaminis.app.ui.sandbox

@Composable
fun MirrorCategoryDetailScreen(
    category: MirrorCategory,
    onBack: () -> Unit,
) {
    Text(category.displayName)
}
'''

FIXTURE_NAV = '''package com.rikkaminis.app.ui.navigation

import com.rikkaminis.app.ui.sandbox.MirrorCategory
import com.rikkaminis.app.ui.sandbox.MirrorCategoryDetailScreen

object Routes {
    internal fun seg(v: String): String = android.net.Uri.encode(v)
    const val ROOTFS_MANAGEMENT = "rootfs_management"
    const val MIRROR_CATEGORY_DETAIL = "mirror_category/{categoryId}"
    fun mirrorCategoryDetail(categoryId: String) = "mirror_category/${seg(categoryId)}"
}

@Composable
fun AppNavigationRoot(navController: NavController) {
    composable(Routes.ROOTFS_MANAGEMENT) {
        RootfsManagementScreen(
            onMirrorCategoryClick = { cat ->
                navController.safeNavigate(Routes.mirrorCategoryDetail(cat.key))
            },
        )
    }
    composable(
        Routes.MIRROR_CATEGORY_DETAIL,
        arguments = listOf(navArgument("categoryId") { type = NavType.StringType }),
    ) { backStackEntry ->
        val category = backStackEntry.arguments
            ?.getString("categoryId")
            ?.let { MirrorCategory.fromKey(it) }
        if (category == null) {
            LaunchedEffect(Unit) { navController.safePopBackStack() }
        } else {
            MirrorCategoryDetailScreen(category = category, onBack = { navController.safePopBackStack() })
        }
    }
}
'''


def write_fixture(root: Path) -> None:
    for rel, content in (
        (REL_CAT, FIXTURE_CAT),
        ("src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/MirrorSettingsScreen.kt", FIXTURE_SCREEN),
        (REL_NAV, FIXTURE_NAV),
    ):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def selftest() -> None:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # compliant fixture must pass
        write_fixture(root)
        VIOLATIONS.clear()
        check(root)
        if VIOLATIONS:
            ok = False
            print(f"SELFTEST compliant fixture unexpectedly failed: {VIOLATIONS}")
        else:
            print("SELFTEST A (compliant fixture) GREEN")

        # fixture B: drop the navigation call (screen defined but never called) -> must fail
        write_fixture(root)
        (root / REL_NAV).write_text(
            FIXTURE_NAV.replace("Routes.mirrorCategoryDetail(cat.key)", "Routes.SETTINGS"),
            encoding="utf-8",
        )
        VIOLATIONS.clear()
        check(root)
        if not any("onMirrorCategoryClick" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture B (drop onMirrorCategoryClick) did not trip the gate")
        else:
            print("SELFTEST B (drop onMirrorCategoryClick) RED as expected")

        # fixture C: fromKey not fail-closed (returns a default instead of null) -> must fail
        write_fixture(root)
        (root / REL_CAT).write_text(
            FIXTURE_CAT.replace(
                "fun fromKey(key: String): MirrorCategory? =\n            entries.firstOrNull { it.key == key }",
                "fun fromKey(key: String): MirrorCategory =\n            entries.firstOrNull { it.key == key } ?: MirrorCategory.ALPINE",
            ),
            encoding="utf-8",
        )
        VIOLATIONS.clear()
        check(root)
        if not any("fail-closed" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture C (fromKey not fail-closed) did not trip the gate")
        else:
            print("SELFTEST C (fromKey not fail-closed) RED as expected")

        # fixture D: builder does not encode through seg() -> must fail
        write_fixture(root)
        (root / REL_NAV).write_text(
            FIXTURE_NAV.replace(
                'fun mirrorCategoryDetail(categoryId: String) = "mirror_category/${seg(categoryId)}"',
                'fun mirrorCategoryDetail(categoryId: String) = "mirror_category/$categoryId"',
            ),
            encoding="utf-8",
        )
        VIOLATIONS.clear()
        check(root)
        if not any("seg()" in v for v in VIOLATIONS):
            ok = False
            print("SELFTEST fixture D (raw mirrorCategoryDetail) did not trip the gate")
        else:
            print("SELFTEST D (raw mirrorCategoryDetail) RED as expected")

    if not ok:
        sys.exit("MIRROR_CATEGORY_ROUTE_SELFTEST=FAIL")


def main() -> None:
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
        print("MIRROR_CATEGORY_ROUTE_SELFTEST=PASS")
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
    print("MIRROR_CATEGORY_ROUTE_CHECK=PASS")


if __name__ == "__main__":
    main()

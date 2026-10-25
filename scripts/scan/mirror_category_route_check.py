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


def statement_expr(text: str, pos: int, limit: int = 900) -> str:
    """Expression starting at [pos] (after `=`), honouring Kotlin's
    continuation lines (a line that opens with `?.` / `.` is the same
    statement)."""
    i = pos
    n = min(len(text), pos + limit)
    depth = 0
    start = i
    while i < n:
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif c == "\n" and depth == 0:
            nxt = text[i + 1:].lstrip(" \t")
            if not nxt.startswith(("?.", ".", "?:", "&&", "||", "+")):
                break
        i += 1
    return text[start:i]


def fail(path, msg):
    VIOLATIONS.append(f"{path}: {msg}")


def match_paren(text: str, open_idx: int) -> int:
    """Index of the ')' matching text[open_idx] == '(' (paren-aware)."""
    depth = 0
    for j in range(open_idx, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return -1


def block_at(text: str, brace_idx: int) -> str:
    """Balanced `{ … }` starting at [brace_idx] (string-aware enough for the
    code shapes here: Kotlin string templates keep braces balanced)."""
    depth = 0
    quote = None
    for j in range(brace_idx, len(text)):
        c = text[j]
        if quote is not None:
            if c == quote:
                quote = None
            continue
        if c in ('"', "'"):
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[brace_idx:j + 1]
    return text[brace_idx:]


def call_args(text: str, open_idx: int) -> str:
    """Argument text of the call whose '(' is at [open_idx]."""
    close = match_paren(text, open_idx)
    if close < 0:
        return ""
    return text[open_idx + 1:close]


def composable_block(nav: str, route_expr: str):
    """Body of the `composable(<route_expr>, …) { … }` destination, or None.

    The destination is the unit that must be wired, so every arm below grades a
    block rather than the whole file: `safeNavigate(Routes.mirrorCategoryDetail
    (…))` somewhere else in AppNavigation must not vouch for this screen.
    """
    for m in re.finditer(r"\bcomposable\s*\(", nav):
        args = call_args(nav, m.end() - 1)
        if route_expr not in args:
            continue
        close = match_paren(nav, m.end() - 1)
        if close < 0:
            return None
        brace = nav.find("{", close)
        if brace < 0:
            return None
        return block_at(nav, brace)
    return None


def scope_head(text: str, brace_idx: int) -> str:
    """The receiver chain written immediately before a lambda's '{'."""
    j = brace_idx - 1
    while j >= 0 and text[j] in " \t\n":
        j -= 1
    end = j + 1
    while j >= 0 and (text[j].isalnum() or text[j] in "_?."):
        j -= 1
    return text[j + 1:end]


def in_discarded_lambda(text: str, idx: int) -> bool:
    """True when the expression at [idx] sits in a `.also { … }` / `.apply { … }`
    lambda. Those hand their RECEIVER on and throw the block's value away, so a
    call made there has no effect on the value the surrounding expression
    produces — the exact shape `x.also { fromKey(it) }` / `null.also { … }`
    uses to look like a resolved value while publishing the receiver (null).
    """
    depth = 0
    i = idx
    while i >= 0:
        c = text[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                head = scope_head(text, i)
                if head.endswith(".also") or head.endswith(".apply"):
                    return True
            else:
                depth -= 1
        i -= 1
    return False


def navigates_with_route(body: str, builder: str = "Routes.mirrorCategoryDetail") -> bool:
    """Does [body] hand the route builder's VALUE to a navigation call?

    Accepted (both are real spellings; the second keeps the route in a local):
        navController.safeNavigate(Routes.mirrorCategoryDetail(cat.key))
        val route = Routes.mirrorCategoryDetail(cat.key); navController.navigate(route)
    Rejected: building the route and dropping it (`{ cat -> Routes.mirrorCategoryDetail(cat.key) }`)
    — the tap then navigates nowhere.
    """
    if in_discarded_lambda(body, body.find(builder + "(")) and builder + "(" in body:
        return False
    for m in re.finditer(r"\b(?:safe)?[Nn]avigate\s*\(", body):
        if builder + "(" in call_args(body, m.end() - 1):
            return True
    for m in re.finditer(r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*(?::[^=\n]*)?=\s*"
                         + re.escape(builder) + r"\s*\(", body):
        local = m.group(1)
        for n in re.finditer(r"\b(?:safe)?[Nn]avigate\s*\(", body):
            if re.search(r"\b" + re.escape(local) + r"\b", call_args(body, n.end() - 1)):
                return True
    return False


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

    # --- arm 2: ROOTFS_MANAGEMENT wires the tap to a real navigation ---
    rootfs = composable_block(nav, "Routes.ROOTFS_MANAGEMENT")
    if rootfs is None:
        fail(REL_NAV, "ROOTFS_MANAGEMENT composable destination not found")
    else:
        m_click = re.search(r"onMirrorCategoryClick\s*=\s*\{", rootfs)
        if not m_click:
            fail(REL_NAV, "ROOTFS_MANAGEMENT composable does not pass onMirrorCategoryClick")
        else:
            click_body = block_at(rootfs, rootfs.find("{", m_click.start()))
            if not navigates_with_route(click_body):
                fail(REL_NAV, "onMirrorCategoryClick does not navigate via Routes.mirrorCategoryDetail "
                              "(the builder's value must reach safeNavigate/navigate — building the "
                              "route and dropping it leaves the row tap dead)")

    # --- arm 1b: the detail destination resolves the route arg into `category` ---
    detail = composable_block(nav, "Routes.MIRROR_CATEGORY_DETAIL")
    if detail is None:
        fail(REL_NAV, "MIRROR_CATEGORY_DETAIL composable destination not found")
    else:
        screen_call = re.search(r"MirrorCategoryDetailScreen\s*\(", detail)
        bound = None
        if screen_call:
            mm = re.search(r"\bcategory\s*=\s*([A-Za-z_]\w*)\b",
                           call_args(detail, screen_call.end() - 1))
            if mm:
                bound = mm.group(1)
        if bound is None:
            fail(REL_NAV, "MirrorCategoryDetailScreen is not called with a `category = <ident>` argument")
        else:
            decl = re.search(r"\b(?:val|var)\s+" + re.escape(bound) + r"\s*(?::[^=\n]*)?=\s*", detail)
            if not decl:
                fail(REL_NAV, f"`{bound}` passed to MirrorCategoryDetailScreen has no local declaration "
                              "in the destination (the screen would never receive a resolved category)")
            else:
                expr = statement_expr(detail, decl.end())
                pos = expr.find("MirrorCategory.fromKey(")
                if 'getString("categoryId")' not in expr:
                    fail(REL_NAV, f"`{bound}` is not read from the route argument "
                                  "(`getString(\"categoryId\")` missing) — the deep-link key never "
                                  "reaches the resolver")
                if pos < 0:
                    fail(REL_NAV, f"`{bound}` never goes through MirrorCategory.fromKey — the route "
                                  "key is not resolved")
                elif in_discarded_lambda(expr, pos):
                    fail(REL_NAV, f"the MirrorCategory.fromKey(...) result is thrown away inside "
                                  f".also/.apply, so `{bound}` carries the receiver (null), not the "
                                  "resolved category — the detail screen becomes unreachable")

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
            # [wide-1024 arm3] fail-closed alone is not enough: the predicate
            # must DISCRIMINATE by the argument it was handed. A tautology
            # (`firstOrNull { true }`) still returns null for an unknown key as
            # long as the enum is non-empty — and resolves every known key to
            # the first entry, so "pip"/"npm" would all open the ALPINE list.
            sig = re.search(r"fun fromKey\(\s*([A-Za-z_]\w*)\s*:", cat)
            param = sig.group(1) if sig else "key"
            pred = re.search(r"firstOrNull\s*\{([^{}]*)\}", cat)
            if not pred:
                fail(REL_CAT, "fromKey's firstOrNull predicate is not readable (fail-closed)")
            elif not re.search(
                    r"(?:==|!=|equals|contains|\bin\b)\s*[^;}]*\b" + re.escape(param) + r"\b"
                    r"|\b" + re.escape(param) + r"\b\s*(?:==|!=)", pred.group(1)):
                fail(REL_CAT, f"fromKey's predicate never compares against its `{param}` argument — "
                              "every key would resolve to the same entry (only fail-closed by luck "
                              "of the enum being non-empty)")

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

        # ---- wide-1024 witnesses: shapes that used to slip through ----------
        def expect_red(label: str, nav_text: str = None, cat_text: str = None, needle: str = ""):
            nonlocal ok
            write_fixture(root)
            if nav_text is not None:
                (root / REL_NAV).write_text(nav_text, encoding="utf-8")
            if cat_text is not None:
                (root / REL_CAT).write_text(cat_text, encoding="utf-8")
            VIOLATIONS.clear()
            check(root)
            if not any(needle in v for v in VIOLATIONS):
                ok = False
                print(f"SELFTEST {label} did not trip the gate (needle={needle!r}, got={VIOLATIONS})")
            else:
                print(f"SELFTEST {label} RED as expected")

        # W1 (wide-1024, axis dataflow): the resolver's value is dropped into
        # `.also`, so the published `category` is the receiver (null) and the
        # detail destination pops straight back.
        expect_red(
            "W1 (fromKey result dropped into .also)",
            nav_text=FIXTURE_NAV.replace(
                "        val category = backStackEntry.arguments\n"
                "            ?.getString(\"categoryId\")\n"
                "            ?.let { MirrorCategory.fromKey(it) }",
                "        val category: MirrorCategory? = null.also { MirrorCategory.fromKey(\"\") }",
            ),
            needle="thrown away inside .also/.apply",
        )

        # W2 (wide-1024, axis dataflow): fromKey keeps `firstOrNull` (and stays
        # fail-closed on an empty enum) but the predicate is a tautology, so
        # every category resolves to the first entry.
        expect_red(
            "W2 (fromKey predicate tautology)",
            cat_text=FIXTURE_CAT.replace("entries.firstOrNull { it.key == key }",
                                        "entries.firstOrNull { true }"),
            needle="never compares against its",
        )

        # W3 (wide-1024, axis dataflow): the tap builds the route and drops it —
        # no navigate call, so the row tap does nothing.
        expect_red(
            "W3 (route built then dropped, no navigate)",
            nav_text=FIXTURE_NAV.replace(
                "                navController.safeNavigate(Routes.mirrorCategoryDetail(cat.key))",
                "                Routes.mirrorCategoryDetail(cat.key)",
            ),
            needle="onMirrorCategoryClick does not navigate",
        )

        # W3b: the accepted alternative spelling (route kept in a local, then
        # navigated) must stay GREEN — the arm grades the dataflow, not one
        # spelling of it.
        write_fixture(root)
        (root / REL_NAV).write_text(
            FIXTURE_NAV.replace(
                "                navController.safeNavigate(Routes.mirrorCategoryDetail(cat.key))",
                "                val route = Routes.mirrorCategoryDetail(cat.key)\n"
                "                navController.safeNavigate(route)",
            ),
            encoding="utf-8",
        )
        VIOLATIONS.clear()
        check(root)
        if VIOLATIONS:
            ok = False
            print(f"SELFTEST W3b (route via local) must stay GREEN, got {VIOLATIONS}")
        else:
            print("SELFTEST W3b (route via a local, then navigate) GREEN as expected")

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

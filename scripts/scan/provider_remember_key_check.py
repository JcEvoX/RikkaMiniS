#!/usr/bin/env python3
"""provider_remember_key_check.py — deep-audit-1025 ISSUE-06 [da1025-F]

Gates the Compose remember-key shape in ProviderListScreen.kt and
ManageProviderModelsSheet.kt so derived values always list their derivation
sources as remember keys (stale-snapshot guard).

Criteria (each fail-closed: missing anchor = RED, never silent skip):
  C1  ProviderListScreen  `val providerRows = remember(`   keys must contain
      both `instances` and `modelEntries` (refreshModels changes modelEntries
      without changing instances — missing key freezes modelCount at 0).
  C2  ProviderListScreen  `val reorderableRows = remember(` keys must contain
      `providerRows` (it derives from providerRows, not instances).
  C3  ManageProviderModelsSheet `val instance = remember(` keys must contain
      both `instanceId` and `config` (deleted provider must become null so
      the auto-dismiss guard fires).
  C4  ProviderListScreen  every `onTogglePinned` line: either no `remember(`
      at all, or its keys contain `pinned` (stale-captured pinned guard).

Usage:
  python3 provider_remember_key_check.py <repo_root>   # real-tree mode
  python3 provider_remember_key_check.py --selftest    # fixture mode

Exit codes: 0 = all criteria green, 1 = any criterion red / anchor missing.
Selftest ends with the tamper marker PROVIDER_REMEMBER_SELFTEST=PASS only
when every fixture asserts exactly as expected.
"""

import os
import re
import shutil
import sys
import tempfile

REL_PROVIDER_LIST = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ProviderListScreen.kt"
REL_SHEET = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ManageProviderModelsSheet.kt"


def strip_comments(text):
    """Remove // line comments and /* */ block comments. Keep string literals
    (and everything inside them) intact so anchors inside quotes survive."""
    out = []
    i = 0
    n = len(text)
    in_block = False
    in_string = False
    quote = None
    while i < n:
        c = text[i]
        if in_block:
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                in_string = False
            i += 1
            continue
        if c == '"' or c == "'":
            in_string = True
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            in_block = True
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def remember_keys(line):
    """Extract the key list from a `remember(...)` call on this line, or None
    if the line has no remember call."""
    m = re.search(r"remember\s*\(([^)]*)\)", line)
    if not m:
        return None
    return m.group(1)


def check_provider_list(code):
    """C1, C2, C4. Returns list of (criterion_id, ok, detail)."""
    results = []
    lines = [ln for ln in code.split("\n")]

    # --- C1: providerRows remember keys ---
    c1_line = None
    for ln in lines:
        if "val providerRows" in ln and "remember(" in ln:
            c1_line = ln
            break
    if c1_line is None:
        results.append(("C1", False, "anchor `val providerRows ... remember(` not found (fail-closed)"))
    else:
        keys = remember_keys(c1_line)
        if keys is None:
            results.append(("C1", False, "providerRows line has no remember( call"))
        else:
            ok = "instances" in keys and "modelEntries" in keys
            results.append(("C1", ok, "keys=[%s]" % keys.strip()))

    # --- C2: reorderableRows remember keys ---
    c2_line = None
    for ln in lines:
        if "val reorderableRows" in ln and "remember(" in ln:
            c2_line = ln
            break
    if c2_line is None:
        results.append(("C2", False, "anchor `val reorderableRows ... remember(` not found (fail-closed)"))
    else:
        keys = remember_keys(c2_line)
        if keys is None:
            results.append(("C2", False, "reorderableRows line has no remember( call"))
        else:
            ok = "providerRows" in keys
            results.append(("C2", ok, "keys=[%s]" % keys.strip()))

    # --- C4: every onTogglePinned line ---
    toggle_lines = [ln for ln in lines if "onTogglePinned" in ln and "=" in ln]
    if not toggle_lines:
        results.append(("C4", False, "no `onTogglePinned =` line found (fail-closed)"))
    else:
        bad = []
        for ln in toggle_lines:
            if "remember(" in ln:
                keys = remember_keys(ln)
                if keys is None or "pinned" not in keys:
                    bad.append(ln.strip()[:120])
        results.append(("C4", len(bad) == 0,
                        "all %d onTogglePinned lines ok" % len(toggle_lines) if not bad
                        else "stale remember key(s): %s" % " | ".join(bad)))
    return results


def check_sheet(code):
    """C3. Returns list of (criterion_id, ok, detail)."""
    results = []
    c3_line = None
    for ln in code.split("\n"):
        if re.search(r"val\s+instance\s*=\s*remember\(", ln):
            c3_line = ln
            break
    if c3_line is None:
        results.append(("C3", False, "anchor `val instance = remember(` not found (fail-closed)"))
    else:
        keys = remember_keys(c3_line)
        if keys is None:
            results.append(("C3", False, "instance line has no remember( call"))
        else:
            ok = "instanceId" in keys and "config" in keys
            results.append(("C3", ok, "keys=[%s]" % keys.strip()))
    return results


def run_tree(root):
    pl_path = os.path.join(root, REL_PROVIDER_LIST)
    sh_path = os.path.join(root, REL_SHEET)
    results = []
    missing = []
    if not os.path.isfile(pl_path):
        missing.append(REL_PROVIDER_LIST)
    else:
        with open(pl_path, "r", encoding="utf-8") as f:
            results.extend(check_provider_list(strip_comments(f.read())))
    if not os.path.isfile(sh_path):
        missing.append(REL_SHEET)
    else:
        with open(sh_path, "r", encoding="utf-8") as f:
            results.extend(check_sheet(strip_comments(f.read())))
    for m in missing:
        results.append(("FILE", False, "target file missing: %s (fail-closed)" % m))
    all_ok = all(ok for _, ok, _ in results)
    print("provider_remember_key_check — real tree: %s" % ("GREEN" if all_ok else "RED"))
    for cid, ok, detail in results:
        print("  [%s] %s — %s" % (cid, "PASS" if ok else "FAIL", detail))
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# Selftest fixtures
# ---------------------------------------------------------------------------

FIX_PROVIDER_LIST_CLEAN = """package com.rikkaminis.app.ui.settings

val providerRows: List<ProviderRowData> = remember(instances, config.modelEntries) {
    instances.map { it }
}
val reorderableRows = remember(providerRows) { providerRows.distinctBy { it.instance.id } }
onTogglePinned = remember(row.instance.id, row.instance.pinned) {
    { providerRepository.setInstancePinned(row.instance.id, !row.instance.pinned) }
}
onTogglePinned = remember(row.instance.id, row.instance.pinned) {
    { providerRepository.setInstancePinned(row.instance.id, !row.instance.pinned) }
}
"""

FIX_PROVIDER_LIST_NO_MODEL_ENTRIES = FIX_PROVIDER_LIST_CLEAN.replace(
    "remember(instances, config.modelEntries)", "remember(instances)")

FIX_PROVIDER_LIST_NO_PROVIDER_ROWS_KEY = FIX_PROVIDER_LIST_CLEAN.replace(
    "remember(providerRows) { providerRows.distinctBy { it.instance.id } }",
    "remember(instances) { providerRows.distinctBy { it.instance.id } }")

FIX_PROVIDER_LIST_STALE_PINNED_KEY = FIX_PROVIDER_LIST_CLEAN.replace(
    "remember(row.instance.id, row.instance.pinned)",
    "remember(row.instance.id)")

FIX_PROVIDER_LIST_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""

FIX_SHEET_CLEAN = """package com.rikkaminis.app.ui.settings

val instance = remember(instanceId, config) { config.instances.firstOrNull { it.id == instanceId } }
"""

FIX_SHEET_NO_CONFIG_KEY = FIX_SHEET_CLEAN.replace(
    "remember(instanceId, config)", "remember(instanceId)")

FIX_SHEET_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""


def write_tree(tmpdir, provider_list_text, sheet_text):
    pl_dir = os.path.join(tmpdir, os.path.dirname(REL_PROVIDER_LIST))
    sh_dir = os.path.join(tmpdir, os.path.dirname(REL_SHEET))
    os.makedirs(pl_dir, exist_ok=True)
    os.makedirs(sh_dir, exist_ok=True)
    with open(os.path.join(tmpdir, REL_PROVIDER_LIST), "w", encoding="utf-8") as f:
        f.write(provider_list_text)
    with open(os.path.join(tmpdir, REL_SHEET), "w", encoding="utf-8") as f:
        f.write(sheet_text)


def run_case(name, expect_rc, provider_list_text, sheet_text, failures):
    tmp = tempfile.mkdtemp(prefix="prk-selftest-")
    try:
        write_tree(tmp, provider_list_text, sheet_text)
        # run the gate logic directly on the fixture tree (exit code capture
        # without a subprocess)
        results = []
        with open(os.path.join(tmp, REL_PROVIDER_LIST), encoding="utf-8") as f:
            results.extend(check_provider_list(strip_comments(f.read())))
        with open(os.path.join(tmp, REL_SHEET), encoding="utf-8") as f:
            results.extend(check_sheet(strip_comments(f.read())))
        all_ok = all(ok for _, ok, _ in results)
        rc = 0 if all_ok else 1
        ok = (rc == expect_rc)
        if not ok:
            failures.append("%s: expected rc=%d got rc=%d :: %s" % (
                name, expect_rc, rc,
                "; ".join("%s %s" % (cid, "PASS" if o else "FAIL") for cid, o, _ in results)))
        print("  fixture %-34s expect_rc=%d rc=%d %s" % (name, expect_rc, rc, "OK" if ok else "MISMATCH"))
        return rc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_selftest():
    print("provider_remember_key_check — selftest")
    failures = []
    # clean tree must be green
    run_case("clean", 0, FIX_PROVIDER_LIST_CLEAN, FIX_SHEET_CLEAN, failures)
    # C1: removing modelEntries key must turn the tree red
    run_case("c1-no-modelEntries-key", 1,
             FIX_PROVIDER_LIST_NO_MODEL_ENTRIES, FIX_SHEET_CLEAN, failures)
    # C2: reorderableRows keyed on instances instead of providerRows
    run_case("c2-reorderableRows-stale-key", 1,
             FIX_PROVIDER_LIST_NO_PROVIDER_ROWS_KEY, FIX_SHEET_CLEAN, failures)
    # C3: sheet instance remember missing config
    run_case("c3-sheet-no-config-key", 1,
             FIX_PROVIDER_LIST_CLEAN, FIX_SHEET_NO_CONFIG_KEY, failures)
    # C4: onTogglePinned remember capturing stale pinned
    run_case("c4-stale-pinned-key", 1,
             FIX_PROVIDER_LIST_STALE_PINNED_KEY, FIX_SHEET_CLEAN, failures)
    # fail-closed: missing anchors must be red, never silently green
    run_case("failclosed-missing-provider-anchors", 1,
             FIX_PROVIDER_LIST_MISSING_ANCHOR, FIX_SHEET_CLEAN, failures)
    run_case("failclosed-missing-sheet-anchor", 1,
             FIX_PROVIDER_LIST_CLEAN, FIX_SHEET_MISSING_ANCHOR, failures)
    # comment must not satisfy criteria (KDoc quoting an anchor is not the anchor)
    commented = FIX_PROVIDER_LIST_NO_MODEL_ENTRIES.replace(
        "val providerRows", "// val providerRows") + "\n// onTogglePinned = remember(instances) {}\n"
    run_case("comments-do-not-count", 1,
             commented, FIX_SHEET_CLEAN, failures)
    if failures:
        print("SELFTEST FAILURES:")
        for f in failures:
            print("  " + f)
        print("PROVIDER_REMEMBER_SELFTEST=FAIL")
        return 1
    print("PROVIDER_REMEMBER_SELFTEST=PASS")
    return 0


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        sys.exit(run_selftest())
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(run_tree(sys.argv[1]))


if __name__ == "__main__":
    main()

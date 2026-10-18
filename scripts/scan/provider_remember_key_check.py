#!/usr/bin/env python3
"""provider_remember_key_check.py — deep-audit-1025 ISSUE-06 [da1025-F]
                                        + deep-audit-1026 ISSUE-03 [da1026-C]

Gates the Compose remember-key shape in the provider settings screens so
derived values always list their derivation sources as remember keys
(stale-snapshot guard), and so an async callback never reads a pre-refresh
snapshot.

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
  C5  ProviderConnectionScreen every `rememberSaveable(` call: keyless is RED,
      and every `instance.<field>` it reads must appear in its own key list
      (with only `instance.id` as key, an external write to customBaseURL /
      customUserAgent left the stale text on screen and the focus-loss save
      wrote it back). Anchors: the file must keep one call keyed on
      `instance.customBaseURL` and one on `instance.customUserAgent` — if the
      screen is reworked so those anchors disappear, this gate goes RED on
      purpose and must be re-derived.
  C6  ModelEntryDetailScreen: zero keyless `remember { ... }` in the file
      (a keyless form freezes the first composition's values for the whole
      visit — external RPC / backup-restore edits stayed invisible and Save
      wrote the stale values back).
  C6c ModelEntryDetailScreen: any `remember(...)` whose call site reads a
      config-derived value (`entry.` / `baseModel.` / `overrides.` /
      `effectiveInput` / `effectiveOutput`) must key on `entry.id` or
      `entryId` — a key that never changes (e.g. the screen's own `entryId`
      argument) re-introduces the same freeze while looking keyed.
  C7  ProviderDetailScreen: the refresh toast must report a count read AFTER
      the refresh. C7a: the `refreshResultMessage(` call site exists and is
      downstream of the `refreshModels(` call. C7b: its arguments must not
      read the pre-refresh `allEntries` snapshot unless that value was
      re-assigned on the refresh path. C7c: the refresh path must contain a
      fresh `entriesFor(` read (or an assignment to a mutableStateOf-backed
      `allEntries`).

Usage:
  python3 provider_remember_key_check.py <repo_root>   # real-tree mode
  python3 provider_remember_key_check.py --selftest    # fixture mode

Exit codes: 0 = all criteria green, 1 = any criterion red / anchor missing.
Selftest ends with the tamper marker PROVIDER_REMEMBER_SELFTEST=PASS only
when every fixture asserts exactly as expected (rc AND the failed-criteria
set — a fixture that reds for the wrong reason is a MISMATCH, not a pass).
"""

import os
import re
import shutil
import sys
import tempfile

REL_PROVIDER_LIST = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ProviderListScreen.kt"
REL_SHEET = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ManageProviderModelsSheet.kt"
REL_PROVIDER_DETAIL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ProviderDetailScreen.kt"
REL_CONNECTION = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ProviderConnectionScreen.kt"
REL_ENTRY_DETAIL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ModelEntryDetailScreen.kt"

# C6c — value families a remember initializer can derive from in
# ModelEntryDetailScreen. Reading any of them makes the call site
# config-derived, i.e. it must re-key when the entry identity changes.
DERIVED_SOURCES = ("entry.", "baseModel.", "overrides.", "effectiveInput", "effectiveOutput")
# Dotted paths rooted at the entry / base model / overrides, plus the two bare
# derived modality lists. Note `entry.id` (the identity component) is NOT a
# derivation source: an identity-only key never changes when the entry's
# content changes, so it cannot satisfy C6c on its own.
DERIVED_TOKEN_RE = re.compile(
    r"\b(?:entry|baseModel|overrides)\.[A-Za-z_][A-Za-z0-9_]*"
    r"|\b(?:effectiveInput|effectiveOutput)\b")


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


def iter_remember_calls(code, call="remember"):
    """Every `remember(...)` / `remember { ... }` call site in `code`.

    Returns a list of (line_no, keys_text, span, init_text). keys_text is None
    for the keyless `remember { ... }` form, otherwise the raw argument text
    between the parentheses (may span lines). span is the full statement text
    of the call site; init_text is the lambda body only (the derivation
    source). Balanced-paren / balanced-brace scans mean nested calls in the
    keys and multi-line lambdas are captured whole. `\\b` guards mean
    `rememberSaveable` / `rememberCoroutineScope` are not matched when
    scanning for `remember`, and the bare import line (which has no following
    `(` or `{`) is skipped rather than mis-read as an anchor. Callers must pass
    comment-stripped code."""
    out = []
    for m in re.finditer(r"\b" + call + r"\b", code):
        start = m.start()
        i = m.end()
        while i < len(code) and code[i] in " \t":
            i += 1
        keys = None
        brace = -1
        if i < len(code) and code[i] == "{":
            brace = i
        elif i < len(code) and code[i] == "(":
            depth = 0
            j = i
            while j < len(code):
                if code[j] == "(":
                    depth += 1
                elif code[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            keys = code[i + 1:j]
            k = j + 1
            while k < len(code) and code[k] in " \t":
                k += 1
            if k < len(code) and code[k] == "{":
                brace = k
        else:
            continue
        end = start
        if brace >= 0:
            depth = 0
            k = brace
            while k < len(code):
                if code[k] == "{":
                    depth += 1
                elif code[k] == "}":
                    depth -= 1
                    if depth == 0:
                        end = k + 1
                        break
                k += 1
        if end <= start:
            nl = code.find("\n", start)
            end = len(code) if nl < 0 else nl
        init = code[brace:end] if brace >= 0 else ""
        out.append((code.count("\n", 0, start) + 1, keys, code[start:end], init))
    return out


def call_args(code, pos):
    """Text between the parentheses of the call whose `(` is the first one at
    or after `pos`, or None if there is no balanced paren pair."""
    j = code.find("(", pos)
    if j < 0:
        return None
    depth = 0
    k = j
    while k < len(code):
        if code[k] == "(":
            depth += 1
        elif code[k] == ")":
            depth -= 1
            if depth == 0:
                return code[j + 1:k]
        k += 1
    return None


def find_call_site(code, name):
    """Index of the first `name(` that is a CALL, not a declaration
    (`private fun refreshResultMessage(`) and not a bare mention."""
    idx = -1
    while True:
        idx = code.find(name + "(", idx + 1)
        if idx < 0:
            return -1
        if "fun " not in code[max(0, idx - 40):idx]:
            return idx


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


def check_provider_detail(code):
    """C7a/b/c — the refresh toast must report a count read AFTER the refresh."""
    results = []
    rm_pos = find_call_site(code, "refreshModels")
    msg_pos = find_call_site(code, "refreshResultMessage")
    if rm_pos < 0:
        results.append(("C7", False, "anchor `refreshModels(` call not found (fail-closed)"))
        return results
    if msg_pos < 0:
        results.append(("C7", False, "anchor `refreshResultMessage(` call not found (fail-closed)"))
        return results
    results.append(("C7a", msg_pos > rm_pos,
                    "toast is downstream of the fetch (refreshModels@%d < refreshResultMessage@%d)"
                    % (rm_pos, msg_pos)))
    args = call_args(code, msg_pos) or ""
    path = code[rm_pos:msg_pos + len(args) + 2]
    reassigned = re.search(r"\ballEntries\s*=[^=]", path) is not None
    stale = "allEntries" in args and not reassigned
    results.append(("C7b", not stale,
                    "toast args carry no un-reassigned pre-refresh `allEntries` read" if not stale
                    else "toast reads the pre-refresh snapshot `allEntries` (stale count)"))
    fresh = "entriesFor(" in path
    decl_mutable = re.search(r"(?:val|var)\s+allEntries[^\n]*mutableStateOf", code) is not None
    ok_fresh = fresh or (decl_mutable and reassigned)
    results.append(("C7c", ok_fresh,
                    "refresh path re-reads entriesFor() / reassigns the state-backed list" if ok_fresh
                    else "no fresh count read on the refresh path (entriesFor / allEntries reassignment)"))
    return results


def check_connection(code):
    """C5 / C5a — ProviderConnectionScreen rememberSaveable key completeness."""
    results = []
    calls = iter_remember_calls(code, "rememberSaveable")
    if not calls:
        results.append(("C5", False,
                        "no `rememberSaveable(` call found in ProviderConnectionScreen (fail-closed)"))
        results.append(("C5a", False,
                        "no rememberSaveable keyed on `instance.customBaseURL` / `instance.customUserAgent` (fail-closed)"))
        return results
    bad = []
    for line_no, keys, span, _ in calls:
        if keys is None:
            bad.append("L%d: keyless rememberSaveable { ... }" % line_no)
            continue
        for field in sorted(set(re.findall(r"\binstance\.[A-Za-z_][A-Za-z0-9_]*", span))):
            if field not in keys:
                bad.append("L%d: reads %s but keys=[%s]" % (line_no, field, " ".join(keys.split())))
    results.append(("C5", not bad,
                    "all %d rememberSaveable calls key every instance.<field> they read" % len(calls)
                    if not bad else "; ".join(bad)))
    for anchor in ("instance.customBaseURL", "instance.customUserAgent"):
        keyed = any(keys is not None and anchor in keys for _, keys, _, _ in calls)
        results.append(("C5a", keyed,
                        "keyed anchor `%s` present" % anchor if keyed else
                        "no rememberSaveable keyed on `%s` — anchor gone, re-derive this gate (fail-closed)"
                        % anchor))
    return results


def check_entry_detail(code):
    """C6 / C6c — ModelEntryDetailScreen remember key completeness."""
    results = []
    calls = iter_remember_calls(code, "remember")
    if not calls:
        results.append(("C6", False,
                        "no `remember(` call found in ModelEntryDetailScreen (fail-closed)"))
        return results
    keyless = [ln for ln, keys, _, _ in calls if keys is None]
    results.append(("C6", not keyless,
                    "0 keyless `remember {` in file" if not keyless
                    else "keyless remember at line(s): %s" % ", ".join(str(x) for x in keyless)))
    derived_bad = []
    for line_no, keys, _, init in calls:
        if keys is None:
            continue  # already counted by C6
        sources = sorted(set(DERIVED_TOKEN_RE.findall(init)))
        if not sources:
            continue  # not config-derived (e.g. a pure-UI sheet flag)
        missing = [tok for tok in sources if tok not in keys]
        if missing:
            derived_bad.append("L%d: initializer derives from %s but keys=[%s]"
                               % (line_no, ",".join(missing), " ".join(keys.split())))
    results.append(("C6c", not derived_bad,
                    "every config-derived remember keys on the exact sources it reads"
                    if not derived_bad else "; ".join(derived_bad)))
    return results


CHECKERS = (
    (REL_PROVIDER_LIST, check_provider_list),
    (REL_SHEET, check_sheet),
    (REL_PROVIDER_DETAIL, check_provider_detail),
    (REL_CONNECTION, check_connection),
    (REL_ENTRY_DETAIL, check_entry_detail),
)


def run_tree(root):
    results = []
    for rel, fn in CHECKERS:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            results.append(("FILE", False, "target file missing: %s (fail-closed)" % rel))
            continue
        with open(path, "r", encoding="utf-8") as f:
            results.extend(fn(strip_comments(f.read())))
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


# --- da1026-C fixtures: the three screens the new criteria guard ----------

FIX_PROVIDER_DETAIL_CLEAN = """package com.rikkaminis.app.ui.settings

val allEntries = remember(instanceId, config) { providerRepository.entriesFor(instanceId) }
IconButton(onClick = {
    isRefreshing = true
    scope.launch {
        val result = providerRepository.refreshModels(instance)
        val total = providerRepository.entriesFor(instance.id).size
        Toast.makeText(ctx, refreshResultMessage(result, total, ctx), Toast.LENGTH_SHORT).show()
    }
})
"""

# the pre-fix shape: toast reads the snapshot captured before the fetch ran
FIX_PROVIDER_DETAIL_STALE = """package com.rikkaminis.app.ui.settings

val allEntries = remember(instanceId, config) { providerRepository.entriesFor(instanceId) }
IconButton(onClick = {
    scope.launch {
        val result = providerRepository.refreshModels(instance)
        Toast.makeText(ctx, refreshResultMessage(result, allEntries.size, ctx), Toast.LENGTH_SHORT).show()
    }
})
"""

# no snapshot read in the args, but also no fresh read: still a stale count
FIX_PROVIDER_DETAIL_NO_REREAD = """package com.rikkaminis.app.ui.settings

val allEntries = remember(instanceId, config) { providerRepository.entriesFor(instanceId) }
IconButton(onClick = {
    scope.launch {
        val result = providerRepository.refreshModels(instance)
        val total = 0
        Toast.makeText(ctx, refreshResultMessage(result, total, ctx), Toast.LENGTH_SHORT).show()
    }
})
"""

# the accepted alternative shape: state-backed list re-assigned on the refresh path
FIX_PROVIDER_DETAIL_STATE_BACKED = """package com.rikkaminis.app.ui.settings

var allEntries by remember(instanceId, config) { mutableStateOf(providerRepository.entriesFor(instanceId)) }
IconButton(onClick = {
    scope.launch {
        val result = providerRepository.refreshModels(instance)
        allEntries = providerRepository.entriesFor(instance.id)
        Toast.makeText(ctx, refreshResultMessage(result, allEntries.size, ctx), Toast.LENGTH_SHORT).show()
    }
})
"""

FIX_PROVIDER_DETAIL_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""

FIX_CONNECTION_CLEAN = """package com.rikkaminis.app.ui.settings

fun ProviderConnectionScreen(instanceId: String, instance: ProviderInstance) {
    var customBaseURL by rememberSaveable(instance.id, instance.customBaseURL) { mutableStateOf(instance.customBaseURL ?: "") }
    var customUserAgent by rememberSaveable(instance.id, instance.customUserAgent) { mutableStateOf(instance.customUserAgent ?: "") }
}
"""

FIX_CONNECTION_NO_BASEURL_KEY = FIX_CONNECTION_CLEAN.replace(
    "var customBaseURL by rememberSaveable(instance.id, instance.customBaseURL)",
    "var customBaseURL by rememberSaveable(instance.id)")

FIX_CONNECTION_NO_UA_KEY = FIX_CONNECTION_CLEAN.replace(
    "var customUserAgent by rememberSaveable(instance.id, instance.customUserAgent)",
    "var customUserAgent by rememberSaveable(instance.id)")

FIX_CONNECTION_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""

FIX_ENTRY_DETAIL_CLEAN = """package com.rikkaminis.app.ui.settings

fun ModelEntryDetailScreen(instanceId: String, entryId: String) {
    var modelId by remember(entry.id, baseModel.id) { mutableStateOf(baseModel.id) }
    var displayName by remember(entry.id, baseModel.displayName, overrides.displayName) {
        mutableStateOf(overrides.displayName ?: baseModel.displayName)
    }
    var isHidden by remember(entry.id, entry.isHidden) { mutableStateOf(entry.isHidden) }
    var showQuickTest by remember(entry.id) { mutableStateOf(false) }
    var imageInput by remember(entry.id, effectiveInput) { mutableStateOf("image" in effectiveInput) }
}
"""

FIX_ENTRY_DETAIL_KEYLESS = FIX_ENTRY_DETAIL_CLEAN.replace(
    "var isHidden by remember(entry.id, entry.isHidden)",
    "var isHidden by remember")

# looks keyed, but the key is the screen's own argument: it never changes when
# the entry's content changes, so the freeze survives
FIX_ENTRY_DETAIL_WRONG_KEY = FIX_ENTRY_DETAIL_CLEAN.replace(
    "var isHidden by remember(entry.id, entry.isHidden)",
    "var isHidden by remember(entryId)")

FIX_ENTRY_DETAIL_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""

CLEAN_TREE = {
    REL_PROVIDER_LIST: FIX_PROVIDER_LIST_CLEAN,
    REL_SHEET: FIX_SHEET_CLEAN,
    REL_PROVIDER_DETAIL: FIX_PROVIDER_DETAIL_CLEAN,
    REL_CONNECTION: FIX_CONNECTION_CLEAN,
    REL_ENTRY_DETAIL: FIX_ENTRY_DETAIL_CLEAN,
}


def variant(rel, text):
    files = dict(CLEAN_TREE)
    files[rel] = text
    return files


def write_tree(tmpdir, files):
    for rel, text in files.items():
        d = os.path.join(tmpdir, os.path.dirname(rel))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(tmpdir, rel), "w", encoding="utf-8") as f:
            f.write(text)


def run_case(name, expect_rc, files, failures, expect_fail=()):
    """Run the gate on a fixture tree. A red fixture must red for EXACTLY the
    criteria in expect_fail (no more, no less): a fixture that turns red for
    the wrong reason is a MISMATCH, not a pass — otherwise a criterion that
    quietly broke would still look "covered" by some unrelated redness."""
    tmp = tempfile.mkdtemp(prefix="prk-selftest-")
    try:
        write_tree(tmp, files)
        # run the gate logic directly on the fixture tree (exit code capture
        # without a subprocess)
        results = []
        for rel, fn in CHECKERS:
            path = os.path.join(tmp, rel)
            if not os.path.isfile(path):
                results.append(("FILE", False, "missing"))
                continue
            with open(path, encoding="utf-8") as f:
                results.extend(fn(strip_comments(f.read())))
        rc = 0 if all(ok for _, ok, _ in results) else 1
        failed = set(cid for cid, ok, _ in results if not ok)
        ok = (rc == expect_rc) and (failed == set(expect_fail))
        if not ok:
            failures.append("%s: expected rc=%d failed=%s got rc=%d failed=%s :: %s" % (
                name, expect_rc, sorted(set(expect_fail)), rc, sorted(failed),
                "; ".join("%s %s" % (cid, "PASS" if o else "FAIL") for cid, o, _ in results)))
        print("  fixture %-38s expect_rc=%d rc=%d failed=%-14s %s" % (
            name, expect_rc, rc, ",".join(sorted(failed)) or "-", "OK" if ok else "MISMATCH"))
        return rc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_selftest():
    print("provider_remember_key_check — selftest")
    failures = []
    # clean tree must be green
    run_case("clean", 0, CLEAN_TREE, failures)
    # C1: removing modelEntries key must turn the tree red
    run_case("c1-no-modelEntries-key", 1,
             variant(REL_PROVIDER_LIST, FIX_PROVIDER_LIST_NO_MODEL_ENTRIES), failures, ("C1",))
    # C2: reorderableRows keyed on instances instead of providerRows
    run_case("c2-reorderableRows-stale-key", 1,
             variant(REL_PROVIDER_LIST, FIX_PROVIDER_LIST_NO_PROVIDER_ROWS_KEY), failures, ("C2",))
    # C3: sheet instance remember missing config
    run_case("c3-sheet-no-config-key", 1,
             variant(REL_SHEET, FIX_SHEET_NO_CONFIG_KEY), failures, ("C3",))
    # C4: onTogglePinned remember capturing stale pinned
    run_case("c4-stale-pinned-key", 1,
             variant(REL_PROVIDER_LIST, FIX_PROVIDER_LIST_STALE_PINNED_KEY), failures, ("C4",))
    # fail-closed: missing anchors must be red, never silently green
    run_case("failclosed-missing-provider-anchors", 1,
             variant(REL_PROVIDER_LIST, FIX_PROVIDER_LIST_MISSING_ANCHOR), failures,
             ("C1", "C2", "C4"))
    run_case("failclosed-missing-sheet-anchor", 1,
             variant(REL_SHEET, FIX_SHEET_MISSING_ANCHOR), failures, ("C3",))
    # comment must not satisfy criteria (KDoc quoting an anchor is not the anchor)
    commented = FIX_PROVIDER_LIST_NO_MODEL_ENTRIES.replace(
        "val providerRows", "// val providerRows") + "\n// onTogglePinned = remember(instances) {}\n"
    run_case("comments-do-not-count", 1,
             variant(REL_PROVIDER_LIST, commented), failures, ("C1",))
    # --- da1026-C: C5 (connection screen) ---------------------------------
    run_case("c5-conn-no-baseurl-key", 1,
             variant(REL_CONNECTION, FIX_CONNECTION_NO_BASEURL_KEY), failures, ("C5", "C5a"))
    run_case("c5-conn-no-ua-key", 1,
             variant(REL_CONNECTION, FIX_CONNECTION_NO_UA_KEY), failures, ("C5", "C5a"))
    run_case("failclosed-missing-conn-anchor", 1,
             variant(REL_CONNECTION, FIX_CONNECTION_MISSING_ANCHOR), failures, ("C5", "C5a"))
    # --- da1026-C: C6 / C6c (entry detail screen) --------------------------
    run_case("c6-entry-keyless-remember", 1,
             variant(REL_ENTRY_DETAIL, FIX_ENTRY_DETAIL_KEYLESS), failures, ("C6",))
    # keyed on the screen's own argument: passes C6, must still be caught
    run_case("c6c-entry-wrong-key", 1,
             variant(REL_ENTRY_DETAIL, FIX_ENTRY_DETAIL_WRONG_KEY), failures, ("C6c",))
    run_case("failclosed-missing-entry-anchor", 1,
             variant(REL_ENTRY_DETAIL, FIX_ENTRY_DETAIL_MISSING_ANCHOR), failures, ("C6",))
    # --- da1026-C: C7 (refresh toast count) --------------------------------
    run_case("c7b-detail-stale-snapshot", 1,
             variant(REL_PROVIDER_DETAIL, FIX_PROVIDER_DETAIL_STALE), failures, ("C7b", "C7c"))
    run_case("c7c-detail-no-fresh-read", 1,
             variant(REL_PROVIDER_DETAIL, FIX_PROVIDER_DETAIL_NO_REREAD), failures, ("C7c",))
    # accepted alternative shape (state-backed list re-assigned on the path)
    run_case("c7-detail-state-backed-variant", 0,
             variant(REL_PROVIDER_DETAIL, FIX_PROVIDER_DETAIL_STATE_BACKED), failures)
    run_case("failclosed-missing-detail-anchor", 1,
             variant(REL_PROVIDER_DETAIL, FIX_PROVIDER_DETAIL_MISSING_ANCHOR), failures, ("C7",))
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

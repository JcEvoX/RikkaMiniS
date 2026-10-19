#!/usr/bin/env python3
"""provider_remember_key_check.py — deep-audit-1025 ISSUE-06 [da1025-F]
                                        + deep-audit-1026 ISSUE-03 [da1026-C]
                                        + deep-audit-1027 ISSUE-02/08 [da1027-E]

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
  C8  ModelGroupDetailScreen: every `remember(...)` whose initializer reads a
      `group.<field>` must key on that exact `group.<field>` (with only
      `group.id` as key an external write — minis-config / Debug RPC / backup
      restore — left the old text on screen and the name field's focus-loss
      save wrote it straight back). Anchor: at least one group-derived call
      must exist, else RED (fail-closed — re-derive if the screen is reworked).
  C9  MountDetailScreen: same shape for `entry.<field>` — `canSave` compares
      the edit buffer against the entry, so a field-keyed remember is what
      keeps an external write from inverting the Save button.
  C10 AppNavigation / UsageStatsScreen — the usage page must observe config
      changes. C10a: the `Routes.USAGE_STATS` route block must NOT pass
      `providerConfig = <...>.value` (a one-shot StateFlow snapshot) and must
      contain a `collectAsState()` read. C10b: the `LaunchedEffect(...)` whose
      body calls `computeUsageStats(` must key on both `range` and
      `providerConfig`.
  C11 ThinkingRulesSection [da1028-C] — the two derived values that decide
      what the section shows must be keyed on every source they are derived
      from. C11a: the `remember(...)` whose initializer calls
      `builtInThinkingRulesForDisplay(` must key on `customBaseURL`,
      `appendV1Suffix` (both feed `effectiveBaseURL` inside the repo call) and
      `modelEntries`. C11b: the `remember(...)` calling `firstModelId(` must key
      on `modelEntries`. C11c: the ProviderDetailScreen call site must forward
      `modelEntries = ...` — a section that is never handed the list cannot key
      on it at all. Anchors missing = RED.

  C12 ThinkingRulesSection [da1030-C ISSUE-05] — the custom-rule loader must
      also re-read after writes made OUTSIDE this screen (backup restore,
      repository load, worker restore, `minis-config`). `reloadKey` only moves
      for edits made on the screen itself, so the screen kept rendering the
      list it read when it opened and wrote it back on the next edit —
      reverting the external write. Two arms: (a) the file must subscribe to
      `customRulesRevision` via `collectAsState()`; (b) the `remember(...)`
      whose initializer calls `thinkingRulesWithIds(` must key on that
      revision (allowlist `rulesRevision` / `customRulesRevision`).
      Anchors missing = RED (re-derive rather than delete).

  C13 ThinkingRuleResolver [da1030-C ISSUE-05] — the revision C12 observes is
      only as good as its writers: `setAllCustomRules`, `setCustomRules` and
      `restoreCustomRulesFromJson` must EACH bump `_customRulesRevision`
      (a mutator that skips the bump re-opens the stale-screen hole for
      exactly that write path), and the file-level count of cache writes
      (`customRulesCache = …`) must not exceed the count of bumps — a body with
      TWO write paths (the null / non-null branches of the restore) needs a
      bump on each, which a per-body "≥1 bump" test alone cannot see. The
      `val customRulesRevision` declaration must exist. Fail-closed on a
      missing mutator body.

  C14 ThinkingRuleEditor [da1033-I07] — the dialog's nine form states are seeded
      from `seed`, so each `remember(...)` whose initializer reads a `seed.<field>`
      must carry a key that can tell one seed from another. Without it an external
      write (minis-config / Debug RPC / backup restore) left the dialog on the copy
      it opened with and Save wrote that copy back over the newer rule.
      Graded on the DECISION ("does this form state re-initialise when the rule it
      was seeded from changes"), not the spelling: `remember(seed)` and
      `remember(request.existingId, seed.wireFormat)` both count, a key that never
      moves with the seed (`remember(request.existingId)`) does not. Only the
      initializer counts as a read — counting the key list would be tautological.
      Fail-closed when no remember derives from `seed.` at all (editor reworked).

  C15 ThinkingRulesSection [da1033-I07] — the dialog must be handed the rule as it
      is NOW, not the snapshot `editorRequest` captured when the row was tapped.
      Two arms: (a) the call's `request` argument is seeded from the LIVE columns
      (`customRules` / `customIds`) and that derivation is selected by the captured
      `existingId` — inline, or through a local declared for it; handing over the
      raw captured request is what reverted the external write. (b) a
      `LaunchedEffect` closes the dialog when the resolution says the rule is gone
      (state must not be written during composition). New-rule paths (`isNew` / `existingId == null`) seed from
      the request itself and must NOT be flagged.

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
# da1027-E
REL_GROUP_DETAIL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ModelGroupDetailScreen.kt"
REL_MOUNT_DETAIL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/MountDetailScreen.kt"
REL_APP_NAV = "src/android/app/src/main/java/com/rikkaminis/app/ui/navigation/AppNavigation.kt"
REL_USAGE_STATS = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/UsageStatsScreen.kt"
# da1028-C
REL_THINKING_SECTION = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ThinkingRulesSection.kt"
# da1030-C
REL_THINKING_RESOLVER = "src/android/app/src/main/java/com/rikkaminis/app/provider/thinking/ThinkingRuleResolver.kt"
# da1033-E (I06 / I07)
REL_THINKING_EDITOR = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ThinkingRuleEditor.kt"

# ---- [da1030-C ISSUE-05] C12 / C13 ----
# The collected revision local may be named either way; both spellings are the
# same single source (`ThinkingRuleResolver.customRulesRevision`).
REVISION_KEY_TOKENS = ("rulesRevision", "customRulesRevision")
REVISION_SUB_RE = re.compile(r"customRulesRevision[\s\S]{0,160}?collectAsState\s*\(")
THINKING_LOADER_CALLEE = "thinkingRulesWithIds("
RESOLVER_MUTATORS = ("setAllCustomRules", "setCustomRules", "restoreCustomRulesFromJson")
RESOLVER_REVISION_DECL_RE = re.compile(r"\bval\s+customRulesRevision\b")
RESOLVER_BUMP_RE = re.compile(r"customRulesRevision\.value\s*\+=")
# C14 — the seed-derived form states of ThinkingRuleEditorDialog.
SEED_FIELD_RE = re.compile(r"\bseed\.[A-Za-z_][A-Za-z0-9_]*")

# C15 — live-seed resolution in ThinkingRulesSection.
DIALOG_CALLEE = "ThinkingRuleEditorDialog"  # no paren: find_call_site appends one
# `seed = <local>` inside the dialog call — the local must read the live columns.
HOISTED_SEED_RE = re.compile(r"\bseed\s*=\s*([A-Za-z_][A-Za-z0-9_]*)")
# (c) the close-on-vanished effect and the test that drives it.
CLOSE_EFFECT_RE = re.compile(
    r"LaunchedEffect\s*\([\s\S]{0,300}?\)[\s\S]{0,120}?\{[\s\S]{0,900}?editorRequest\s*=\s*null")
VANISHED_TEST_RE = re.compile(r"<\s*0|==\s*-1|!in\b|==\s*null|isEmpty\(\)|isNotEmpty\(\)")
# A cache WRITE (`customRulesCache = …`), distinct from the field declaration
# (`private var customRulesCache: Map<…> = …`) and from passing it as an arg.
CACHE_WRITE_RE = re.compile(r"\bcustomRulesCache\s*=\s*[A-Za-z_]")

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
# C8 / C9 — the field families the two detail screens edit.
GROUP_FIELD_RE = re.compile(r"\bgroup\.[A-Za-z_][A-Za-z0-9_]*")
ENTRY_FIELD_RE = re.compile(r"\bentry\.[A-Za-z_][A-Za-z0-9_]*")


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

def func_body(code, name):
    """Balanced-brace body of `fun <name>(...) { … }` (string-aware), or None
    (fail-closed) when the declaration or its closing brace cannot be found.
    Called with comment-stripped code."""
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
    """C7a/b/c — the refresh toast must report a count read AFTER the refresh.
    C11c [da1028-C] — the ThinkingRulesSection call site forwards the model
    entry list."""
    results = [check_thinking_rules_call_site(code)]
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


def check_field_keyed_remember(code, field_re, target, cid, screen):
    """C8 / C9 — every `remember(...)` whose initializer reads a
    `<target>.<field>` must key on that exact field.

    Only the initializer counts as a read: the key list itself mentioning the
    field is what we are testing for, so counting the whole statement would
    make the criterion tautological. Keyless (`remember { ... }`) counts as a
    miss whenever the initializer is field-derived.

    Fail-closed anchor: no field-derived remember call at all means the screen
    was reworked and this criterion can no longer see what it guards → RED
    (never a silent skip)."""
    results = []
    calls = iter_remember_calls(code, "remember")
    derived = 0
    bad = []
    for line_no, keys, _span, init in calls:
        sources = sorted(set(field_re.findall(init)))
        if not sources:
            continue
        derived += 1
        if keys is None:
            bad.append("L%d: keyless remember derives from %s" % (line_no, ",".join(sources)))
            continue
        missing = [tok for tok in sources if tok not in keys]
        if missing:
            bad.append("L%d: initializer derives from %s but keys=[%s]"
                       % (line_no, ",".join(missing), " ".join(keys.split())))
    if derived == 0:
        results.append((cid, False,
                        "no remember call in %s derives from %s.<field> (fail-closed: "
                        "screen reworked, re-derive this criterion)" % (screen, target)))
        return results
    results.append((cid, not bad,
                    "all %d %s-derived remember calls key the exact field they read"
                    % (derived, target) if not bad else "; ".join(bad)))
    return results


def check_group_detail(code):
    """C8 — ModelGroupDetailScreen session-defaults / name editors."""
    return check_field_keyed_remember(code, GROUP_FIELD_RE, "group", "C8",
                                      "ModelGroupDetailScreen")


def check_mount_detail(code):
    """C9 — MountDetailScreen userAllowWrite switch."""
    return check_field_keyed_remember(code, ENTRY_FIELD_RE, "entry", "C9",
                                      "MountDetailScreen")


USAGE_STATS_ANCHOR = "composable(Routes.USAGE_STATS)"
NEXT_COMPOSABLE_RE = re.compile(r"composable\(Routes\.")


def check_app_navigation(code):
    """C10a — the usage route must read the provider config reactively."""
    results = []
    pos = code.find(USAGE_STATS_ANCHOR)
    if pos < 0:
        results.append(("C10a", False,
                        "anchor `%s` not found (fail-closed)" % USAGE_STATS_ANCHOR))
        return results
    nxt = NEXT_COMPOSABLE_RE.search(code, pos + len(USAGE_STATS_ANCHOR))
    block = code[pos:nxt.start()] if nxt else code[pos:]
    if "UsageStatsScreen(" not in block:
        results.append(("C10a", False,
                        "anchor `%s` block does not call UsageStatsScreen( (fail-closed)"
                        % USAGE_STATS_ANCHOR))
        return results
    snapshot = re.search(r"providerConfig\s*=\s*[\w.]+\.value\b", block)
    reactive = "collectAsState" in block
    if snapshot is not None:
        results.append(("C10a", False,
                        "route passes a one-shot snapshot: `%s` — config updates never "
                        "reach UsageStatsScreen; use collectAsState()"
                        % " ".join(snapshot.group(0).split())))
        return results
    results.append(("C10a", reactive,
                    "route reads the config via collectAsState()" if reactive else
                    "no `collectAsState()` in the usage route block — the config is not "
                    "observed (fail-closed)"))
    return results


def check_usage_stats(code):
    """C10b — the stats LaunchedEffect must key on the config it consumes."""
    results = []
    hits = [(ln, keys, span, init)
            for ln, keys, span, init in iter_remember_calls(code, "LaunchedEffect")
            if "computeUsageStats(" in init]
    if not hits:
        results.append(("C10b", False,
                        "no LaunchedEffect(...) whose body calls computeUsageStats( "
                        "(fail-closed: anchor gone, re-derive this criterion)"))
        return results
    bad = []
    for line_no, keys, _span, _init in hits:
        if keys is None:
            bad.append("L%d: keyless LaunchedEffect" % line_no)
            continue
        missing = [tok for tok in ("range", "providerConfig") if tok not in keys]
        if missing:
            bad.append("L%d: keys=[%s] missing %s"
                       % (line_no, " ".join(keys.split()), ",".join(missing)))
    results.append(("C10b", not bad,
                    "all %d computeUsageStats effect(s) key on range + providerConfig" % len(hits)
                    if not bad else "; ".join(bad)))
    return results


def check_thinking_rules_call_site(code):
    """C11c — ProviderDetailScreen must hand the section its entry list."""
    pos = find_call_site(code, "ThinkingRulesSection")
    if pos < 0:
        results = ("C11c", False,
                   "anchor `ThinkingRulesSection(` call not found in ProviderDetailScreen "
                   "(fail-closed: section dropped or renamed — re-derive this criterion)")
    else:
        args = call_args(code, pos)
        if args is None:
            results = ("C11c", False,
                       "ThinkingRulesSection( has no balanced argument list (fail-closed)")
        elif re.search(r"\bmodelEntries\s*=", args):
            results = ("C11c", True, "call site forwards `modelEntries = ...`")
        else:
            results = ("C11c", False,
                       "ThinkingRulesSection( is not given `modelEntries` — the section's derived "
                       "keys can never observe a catalog refresh")
    return results

def check_thinking_rules(code):
    """C11a / C11b — ThinkingRulesSection derived remember keys.

    Only the initializer counts as a read (same rule as C8/C9): the key list
    mentioning a field is exactly what is under test, so grading the whole
    statement would be tautological. Keyless counts as a miss whenever the
    initializer is derived. Fail-closed: no remember whose initializer calls
    the anchor callee means the section was reworked → RED."""
    results = []
    targets = (
        ("C11a", "builtInThinkingRulesForDisplay(",
         ("customBaseURL", "appendV1Suffix", "modelEntries"), "built-in rule list"),
        ("C11b", "firstModelId(", ("modelEntries",), "resolution-preview model id"),
    )
    calls = iter_remember_calls(code, "remember")
    for cid, callee, required, label in targets:
        hits = [c for c in calls if callee in c[3]]
        if not hits:
            results.append((cid, False,
                            "no `remember(...)` whose initializer calls %s (fail-closed: the %s "
                            "call was reworked — re-derive this criterion)" % (callee, label)))
            continue
        bad = []
        for line_no, keys, _span, _init in hits:
            if keys is None:
                bad.append("L%d: keyless remember derives from %s" % (line_no, callee))
                continue
            missing = [tok for tok in required if tok not in keys]
            if missing:
                bad.append("L%d: keys=[%s] missing %s"
                           % (line_no, " ".join(keys.split()), ",".join(missing)))
        results.append((cid, not bad,
                        "all %d %s remember call(s) key on %s"
                        % (len(hits), label, " + ".join(required))
                        if not bad else "; ".join(bad)))
    # C12 [da1030-C ISSUE-05] — the loader must observe EXTERNAL rule writes.
    bad = []
    if not REVISION_SUB_RE.search(code):
        bad.append("no `customRulesRevision` → `collectAsState()` subscription (fail-closed: "
                   "the section cannot observe rules written outside this screen)")
    loader_hits = [c for c in calls if THINKING_LOADER_CALLEE in c[3]]
    if not loader_hits:
        bad.append("no `remember(...)` whose initializer calls %s (fail-closed: the loader "
                   "was reworked — re-derive this criterion)" % THINKING_LOADER_CALLEE)
    for line_no, keys, _span, _init in loader_hits:
        if keys is None:
            bad.append("L%d: keyless remember loads the custom rules" % line_no)
            continue
        if not any(tok in keys for tok in REVISION_KEY_TOKENS):
            bad.append("L%d: keys=[%s] missing the rules-revision key (%s)"
                       % (line_no, " ".join(keys.split()), " / ".join(REVISION_KEY_TOKENS)))
    results.append(("C12", not bad,
                    "section subscribes to customRulesRevision and keys the loader on it"
                    if not bad else "; ".join(bad)))
    return results

def check_thinking_resolver(code):
    """C13 [da1030-C ISSUE-05] — every custom-rule mutator bumps the revision.

    Two arms: (a) each named mutator body carries at least one bump, and
    (b) the file-level cache-write count does not exceed the bump count. Arm (b)
    is what catches a multi-path body (the restore's null / non-null branches)
    where one branch lost its bump but the body still looks fine to arm (a).

    Fail-closed per mutator: a missing body means the mutator was renamed or
    moved, so this criterion must be re-derived rather than silently pass."""
    results = []
    bad = []
    if not RESOLVER_REVISION_DECL_RE.search(code):
        bad.append("no `val customRulesRevision` declaration (fail-closed)")
    for name in RESOLVER_MUTATORS:
        body = func_body(code, name)
        if body is None:
            bad.append("%s body not found (fail-closed)" % name)
            continue
        if not RESOLVER_BUMP_RE.search(body):
            bad.append("%s does not bump customRulesRevision" % name)
    writes = len(CACHE_WRITE_RE.findall(code))
    bumps = len(RESOLVER_BUMP_RE.findall(code))
    if bumps < writes:
        bad.append("customRulesCache is written %d time(s) but bumped %d time(s) — a mutation "
                   "path does not notify the UI" % (writes, bumps))
    results.append(("C13", not bad,
                    "all %d custom-rule mutators bump customRulesRevision "
                    "(%d cache writes / %d bumps)" % (len(RESOLVER_MUTATORS), writes, bumps)
                    if not bad else "; ".join(bad)))
    return results

def check_thinking_editor(code):
    """C14 [da1033-I07] — every seed-derived form state re-initialises with its seed.

    Graded on the decision point, not the spelling: a key of `seed` itself, of the
    exact `seed.<field>` the initializer reads, or of `request.seed`, all count; a key
    that cannot move when the seed changes (e.g. `request.existingId` alone) does not.
    Only the initializer counts as a read (same rule as C8/C9/C11) — counting the key
    list would make the criterion tautological. Keyless counts as a miss.

    Fail-closed: no seed-derived remember at all means the editor was reworked and
    this criterion can no longer see what it guards → RED."""
    results = []
    calls = iter_remember_calls(code, "remember")
    derived = 0
    bad = []
    for line_no, keys, _span, init in calls:
        sources = sorted(set(SEED_FIELD_RE.findall(init)))
        if not sources:
            continue
        derived += 1
        if keys is None:
            bad.append("L%d: keyless remember derives from %s" % (line_no, ",".join(sources)))
            continue
        tokens = [t.strip() for t in keys.split(",")]
        seed_keyed = any(t == "seed" or t.endswith(".seed") for t in tokens)
        if not seed_keyed and not all(src in keys for src in sources):
            bad.append("L%d: initializer derives from %s but keys=[%s] cannot tell one seed "
                       "from another" % (line_no, ",".join(sources), " ".join(keys.split())))
    if derived == 0:
        results.append(("C14", False,
                        "no remember call in ThinkingRuleEditor derives from `seed.` "
                        "(fail-closed: the dialog was reworked, re-derive this criterion)"))
        return results
    results.append(("C14", not bad,
                    "all %d seed-derived remember call(s) key on the seed they read" % derived
                    if not bad else "; ".join(bad)))
    return results

def _seed_assignment_text(code, name):
    """The text of `val <name> = …` up to the end of its statement.

    Multi-line initializers (an `if`/`when` expressing the seed) end at the first
    non-blank line indented no deeper than the declaration itself; a single-line one
    ends at its own newline. Returns None when the declaration is absent."""
    m = re.search(r"\bval\s+" + re.escape(name) + r"\s*=", code)
    if not m:
        return None
    line_start = code.rfind("\n", 0, m.start()) + 1
    indent = len(code[line_start:m.start() + len("val " + name)])
    base = code[line_start:].split("\n")[0]
    base_indent = len(base) - len(base.lstrip(" \t"))
    out = [base]
    for line in code[line_start:].split("\n")[1:]:
        if line.strip() and (len(line) - len(line.lstrip(" \t"))) <= base_indent:
            break
        out.append(line)
    return "\n".join(out)

def check_thinking_dialog_seed(code):
    """C15 [da1033-I07] — the editor dialog is handed the LIVE rule, not the snapshot.

    Two arms, both graded on the decision rather than the spelling:

      (a) the `request` handed to `ThinkingRuleEditorDialog(` is seeded from the
          live rule columns (`customRules` / `customIds`) AND that derivation reads
          the captured `existingId` — either inline in the argument, or through a
          local declared for the purpose. Handing over the raw captured request is
          what let Save write the opening snapshot back over an external edit.
      (b) a `LaunchedEffect` closes the dialog when the resolution says the rule is
          gone. Deleting the rule externally must not be undone by Save
          resurrecting it, and state must not be written during composition.

    New-rule paths are deliberately NOT graded: they carry `existingId == null` /
    `isNew` and seed from the request itself, so they cannot be "stale"."""
    results = []
    pos = find_call_site(code, DIALOG_CALLEE)
    if pos < 0:
        results.append(("C15a", False,
                        "anchor `ThinkingRuleEditorDialog(` call not found (fail-closed: "
                        "the dialog was dropped or renamed — re-derive this criterion)"))
        results.append(("C15b", False, "no dialog call site to close (fail-closed)"))
        return results

    args = call_args(code, pos)
    if args is None:
        results.append(("C15a", False,
                        "ThinkingRuleEditorDialog( has no balanced argument list (fail-closed)"))
    else:
        derivation = args
        note = "inline"
        if "customRules" not in args and "customIds" not in args:
            m = HOISTED_SEED_RE.search(args)
            seg = _seed_assignment_text(code, m.group(1)) if m else None
            if seg is not None:
                derivation = seg
                note = "via `val %s =`" % m.group(1)
        live = "customRules" in derivation or "customIds" in derivation
        by_id = "existingId" in derivation
        if live and by_id:
            detail = ("ThinkingRuleEditorDialog( is seeded from the live rules and "
                      "selected by the captured existingId (%s)" % note)
            ok = True
        elif not live:
            detail = ("the `request` handed to ThinkingRuleEditorDialog( does not read the "
                      "live `customRules` / `customIds` — it is the snapshot captured when "
                      "the row was tapped, so Save writes it back over any external edit")
            ok = False
        else:
            detail = ("the dialog is seeded from the live rules but the captured "
                      "`existingId` does not select among them — the dialog would edit "
                      "whichever rule happens to be first")
            ok = False
        results.append(("C15a", ok, detail))

    m = CLOSE_EFFECT_RE.search(code)
    if not m:
        results.append(("C15b", False,
                        "no LaunchedEffect assigning `editorRequest = null` — a rule "
                        "deleted while the dialog is open would be resurrected by Save"))
    elif not VANISHED_TEST_RE.search(m.group(0)):
        results.append(("C15b", False,
                        "the close effect does not test the resolved index for existence "
                        "(`< 0` / `== -1` / `!in`) — it would close the dialog "
                        "unconditionally"))
    else:
        results.append(("C15b", True,
                        "a LaunchedEffect closes the dialog when the live lookup says the "
                        "rule is gone"))
    return results

CHECKERS = (
    (REL_PROVIDER_LIST, check_provider_list),
    (REL_SHEET, check_sheet),
    (REL_PROVIDER_DETAIL, check_provider_detail),
    (REL_CONNECTION, check_connection),
    (REL_ENTRY_DETAIL, check_entry_detail),
    (REL_GROUP_DETAIL, check_group_detail),
    (REL_MOUNT_DETAIL, check_mount_detail),
    (REL_APP_NAV, check_app_navigation),
    (REL_USAGE_STATS, check_usage_stats),
    (REL_THINKING_SECTION, check_thinking_rules),
    (REL_THINKING_RESOLVER, check_thinking_resolver),
    (REL_THINKING_SECTION, check_thinking_dialog_seed),
    (REL_THINKING_EDITOR, check_thinking_editor),
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
ThinkingRulesSection(instance = currentInstance, providerRepository = providerRepository, modelEntries = allEntries)
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
ThinkingRulesSection(instance = currentInstance, providerRepository = providerRepository, modelEntries = allEntries)
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
ThinkingRulesSection(instance = currentInstance, providerRepository = providerRepository, modelEntries = allEntries)
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
ThinkingRulesSection(instance = currentInstance, providerRepository = providerRepository, modelEntries = allEntries)
"""

# [da1028-C] C11c: the section is called without its entry list, so the keys
# inside it can never observe a catalog refresh.
FIX_PROVIDER_DETAIL_NO_SECTION_ARGS = FIX_PROVIDER_DETAIL_CLEAN.replace(
    ", modelEntries = allEntries", "")

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

# --- da1027-E fixtures: the two detail screens + the usage route ----------

FIX_GROUP_DETAIL_CLEAN = """package com.rikkaminis.app.ui.settings

fun ModelGroupDetailScreen(groupId: String) {
    var name by remember(group.id, group.name) { mutableStateOf(group.name) }
    var strategy by remember(group.id, group.strategy) { mutableStateOf(group.strategy) }
    var fallbackStrategy by remember(group.id, group.fallbackStrategy) {
        mutableStateOf(group.fallbackStrategy)
    }
    var defaultThinkingLevel by remember(group.id, group.defaultThinkingLevel) {
        mutableStateOf(group.defaultThinkingLevel)
    }
    var contextLimitTokens by remember(group.id, group.contextLimitTokens) {
        mutableStateOf(group.contextLimitTokens)
    }
    var memberIds by remember(group.memberEntryIds) { mutableStateOf(group.memberEntryIds.toList()) }
    var entryToRemove by remember { mutableStateOf<String?>(null) }
    val groupMaxThinkingLevel = remember(group.memberEntryIds, config.modelEntries) {
        group.memberEntryIds.mapNotNull { entryId -> config.modelEntries.find { it.id == entryId } }
    }
}
"""

# the pre-fix shape: the editor is keyed on the identity only, so an external
# write to `name` left the old text on screen for the focus-loss save to undo
FIX_GROUP_DETAIL_NO_NAME_KEY = FIX_GROUP_DETAIL_CLEAN.replace(
    "remember(group.id, group.name) { mutableStateOf(group.name) }",
    "remember(group.id) { mutableStateOf(group.name) }")

FIX_GROUP_DETAIL_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""

FIX_MOUNT_DETAIL_CLEAN = """package com.rikkaminis.app.ui.settings

fun MountDetailScreen(mountId: String) {
    val entry = entries.firstOrNull { it.id == mountId }
    var allowWrite by remember(entry.id, entry.userAllowWrite) { mutableStateOf(entry.userAllowWrite) }
    var showUnmountConfirm by remember { mutableStateOf(false) }
    val canSave = allowWrite != entry.userAllowWrite
}
"""

FIX_MOUNT_DETAIL_NO_WRITE_KEY = FIX_MOUNT_DETAIL_CLEAN.replace(
    "remember(entry.id, entry.userAllowWrite)", "remember(entry.id)")

FIX_MOUNT_DETAIL_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""

FIX_APPNAV_CLEAN = """package com.rikkaminis.app.ui.navigation

composable(Routes.USAGE_STATS) {
    val providerConfig by providerRepository.config.collectAsState()
    UsageStatsScreen(
        chatDao = chatRepository.dao,
        providerConfig = providerConfig,
        onBack = { navController.safePopBackStack() },
    )
}

composable(Routes.APPEARANCE) {
    AppearanceScreen(onBack = { navController.safePopBackStack() })
}
"""

# the pre-fix shape: a one-shot snapshot; the config flow never recomposes the
# route, so the screen keeps whatever config existed when it was first opened
FIX_APPNAV_SNAPSHOT = """package com.rikkaminis.app.ui.navigation

composable(Routes.USAGE_STATS) {
    UsageStatsScreen(
        chatDao = chatRepository.dao,
        providerConfig = providerRepository.config.value,
        onBack = { navController.safePopBackStack() },
    )
}

composable(Routes.APPEARANCE) {
    AppearanceScreen(onBack = { navController.safePopBackStack() })
}
"""

FIX_APPNAV_MISSING_ANCHOR = """package com.rikkaminis.app.ui.navigation

composable(Routes.APPEARANCE) {
    AppearanceScreen(onBack = { navController.safePopBackStack() })
}
"""

FIX_USAGE_STATS_CLEAN = """package com.rikkaminis.app.ui.settings

@Composable
fun UsageStatsScreen(chatDao: ChatDao, providerConfig: ProviderConfig? = null, onBack: () -> Unit) {
    var range by remember { mutableStateOf(UsageRange.ALL) }
    LaunchedEffect(range, providerConfig) {
        val (total, groups) = withContext(Dispatchers.Default) {
            computeUsageStats(range, chatDao, providerConfig)
        }
        grandTotal = total
    }
}
"""

FIX_USAGE_STATS_NO_CONFIG_KEY = FIX_USAGE_STATS_CLEAN.replace(
    "LaunchedEffect(range, providerConfig) {", "LaunchedEffect(range) {")

FIX_USAGE_STATS_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

@Composable
fun UsageStatsScreen(chatDao: ChatDao, providerConfig: ProviderConfig? = null) {
    var range by remember { mutableStateOf(UsageRange.ALL) }
}
"""

# --- da1028-C fixtures: ThinkingRulesSection derived keys ------------------

FIX_THINKING_CLEAN = """package com.rikkaminis.app.ui.settings

@Composable
fun ThinkingRulesSection(
    instance: ProviderInstance,
    providerRepository: ProviderRepository,
    modelEntries: List<ModelEntry> = emptyList(),
) {
    var reloadKey by remember { mutableStateOf(0) }
    var defaultsExpanded by remember { mutableStateOf(false) }
    val rulesRevision by ThinkingRuleResolver.customRulesRevision.collectAsState()
    val customRulesAndIds = remember(instance.id, reloadKey, rulesRevision) {
        providerRepository.thinkingRulesWithIds(instance.id)
    }
    val builtIns = remember(instance.id, instance.customBaseURL, instance.appendV1Suffix, modelEntries, reloadKey) {
        providerRepository.builtInThinkingRulesForDisplay(instance.id)
    }
    val sampleModelId = remember(instance.id, modelEntries, reloadKey) {
        providerRepository.firstModelId(instance.id)
    }
    // da1033-I07: the dialog edits the LIVE rule, not the tapped snapshot.
    val editorLiveIndex = editorRequest?.existingId?.let { customIds.indexOf(it) } ?: -1
    LaunchedEffect(editorRequest?.existingId, editorLiveIndex) {
        val req = editorRequest
        if (req?.existingId != null && editorLiveIndex < 0) editorRequest = null
    }
    editorRequest?.let { req ->
        val liveSeed = if (req.existingId != null && !req.isNew && editorLiveIndex >= 0) {
            customRules[editorLiveIndex]
        } else {
            req.seed
        }
        ThinkingRuleEditorDialog(
            request = req.copy(seed = liveSeed),
            onDismiss = { editorRequest = null },
            onSave = { rule ->
                providerRepository.saveThinkingRule(instance.id, rule, id = req.existingId)
                editorRequest = null
                reloadKey++
            },
        )
    }
    // New-rule path: no live id to resolve — seeds itself.
    editorRequest = ThinkingRuleEditorRequest(
        existingId = null,
        seed = ThinkingRule(kind = ThinkingRule.Kind.CUSTOM, label = ""),
        isNew = true,
    )
    val customRules = customRulesAndIds.first
    val customIds = customRulesAndIds.second
}
"""

FIX_THINKING_NO_BASEURL_KEY = FIX_THINKING_CLEAN.replace(
    "remember(instance.id, instance.customBaseURL, instance.appendV1Suffix, modelEntries, reloadKey)",
    "remember(instance.id, instance.appendV1Suffix, modelEntries, reloadKey)")

FIX_THINKING_BUILTINS_NO_MODEL_ENTRIES = FIX_THINKING_CLEAN.replace(
    "remember(instance.id, instance.customBaseURL, instance.appendV1Suffix, modelEntries, reloadKey)",
    "remember(instance.id, instance.customBaseURL, instance.appendV1Suffix, reloadKey)")

FIX_THINKING_SAMPLE_NO_MODEL_ENTRIES = FIX_THINKING_CLEAN.replace(
    "remember(instance.id, modelEntries, reloadKey) {\n        providerRepository.firstModelId(instance.id)",
    "remember(instance.id, reloadKey) {\n        providerRepository.firstModelId(instance.id)")

FIX_THINKING_SAMPLE_KEYLESS = FIX_THINKING_CLEAN.replace(
    "remember(instance.id, modelEntries, reloadKey) {\n        providerRepository.firstModelId(instance.id)",
    "remember {\n        providerRepository.firstModelId(instance.id)")

FIX_THINKING_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""

# Precision control: touching a remember this criterion does NOT own (a Room
# loader and two pure-UI flags, one keyless) must leave the gate green —
# otherwise "any keyless remember reds" would masquerade as coverage.
# da1033-I07 (C15) fixtures.

# The pre-fix dialog region: a snapshot request, no live lookup, no close effect.
FIX_THINKING_PREFIX_DIALOG = FIX_THINKING_CLEAN.replace(
    """    val editorLiveIndex = editorRequest?.existingId?.let { customIds.indexOf(it) } ?: -1
    LaunchedEffect(editorRequest?.existingId, editorLiveIndex) {
        val req = editorRequest
        if (req?.existingId != null && editorLiveIndex < 0) editorRequest = null
    }
    editorRequest?.let { req ->
        val liveSeed = if (req.existingId != null && !req.isNew && editorLiveIndex >= 0) {
            customRules[editorLiveIndex]
        } else {
            req.seed
        }
        ThinkingRuleEditorDialog(
            request = req.copy(seed = liveSeed),""",
    """    editorRequest?.let { req ->
        ThinkingRuleEditorDialog(
            request = req,""")

FIX_THINKING_DIALOG_STALE_REQUEST = FIX_THINKING_CLEAN.replace(
    "request = req.copy(seed = liveSeed),", "request = req,")

FIX_THINKING_NO_CLOSE_EFFECT = FIX_THINKING_CLEAN.replace(
    """    LaunchedEffect(editorRequest?.existingId, editorLiveIndex) {
        val req = editorRequest
        if (req?.existingId != null && editorLiveIndex < 0) editorRequest = null
    }
""", "")

# Seeded from the live list but NOT selected by the captured id: the dialog would
# edit whichever rule happens to be first.
FIX_THINKING_SEED_NOT_BY_ID = FIX_THINKING_CLEAN.replace(
    """        val liveSeed = if (req.existingId != null && !req.isNew && editorLiveIndex >= 0) {
            customRules[editorLiveIndex]
        } else {
            req.seed
        }""",
    "        val liveSeed = customRules.first()")

# Precision: the NEW-rule path seeds itself by design (no live id to resolve) —
# a criterion that graded every `ThinkingRuleEditorRequest(` construction would
# red here, so this fixture proves C15 only grades the dialog call site.
FIX_THINKING_NEW_RULE_SEED = FIX_THINKING_CLEAN.replace(
    'seed = ThinkingRule(kind = ThinkingRule.Kind.CUSTOM, label = ""),',
    'seed = builtInRule.copy(kind = ThinkingRule.Kind.CUSTOM, label = "Copy of " + builtInRule.label),')

FIX_THINKING_PRECISION_UNRELATED = FIX_THINKING_CLEAN.replace(
    "    var defaultsExpanded by remember { mutableStateOf(false) }",
    "    var defaultsExpanded by remember { mutableStateOf(false) }\n"
    "    var editorTick by remember { mutableStateOf(0) }",
)

# --- da1030-C ISSUE-05 fixtures: C12 / C13 --------------------------------

FIX_THINKING_NO_REVISION_KEY = FIX_THINKING_CLEAN.replace(
    "remember(instance.id, reloadKey, rulesRevision)",
    "remember(instance.id, reloadKey)")

FIX_THINKING_NO_SUBSCRIPTION = FIX_THINKING_CLEAN.replace(
    "    val rulesRevision by ThinkingRuleResolver.customRulesRevision.collectAsState()\n", "")

FIX_THINKING_RESOLVER_CLEAN = """package com.rikkaminis.app.provider.thinking

object ThinkingRuleResolver {
    @Volatile
    private var customRulesCache: Map<String, List<ThinkingRule>> = emptyMap()

    private val _customRulesRevision = MutableStateFlow(0L)

    val customRulesRevision: StateFlow<Long> = _customRulesRevision.asStateFlow()

    @Synchronized
    fun setAllCustomRules(byInstance: Map<String, List<ThinkingRule>>) {
        customRulesCache = byInstance
        _customRulesRevision.value += 1
    }

    @Synchronized
    fun setCustomRules(instanceId: String, rules: List<ThinkingRule>) {
        customRulesCache = withInstanceRules(customRulesCache, instanceId, rules)
        _customRulesRevision.value += 1
    }

    @Synchronized
    fun restoreCustomRulesFromJson(instanceId: String, field: JSONArray?): Int {
        if (instanceId.isBlank()) return 0
        if (field == null) {
            customRulesCache = withInstanceRules(customRulesCache, instanceId, emptyList())
            _customRulesRevision.value += 1
            return 0
        }
        val rules = (0 until field.length()).mapNotNull { i ->
            val o = field.optJSONObject(i) ?: return@mapNotNull null
            ThinkingRuleCoding.decodeRuleJson(o)
        }
        customRulesCache = withInstanceRules(customRulesCache, instanceId, rules)
        _customRulesRevision.value += 1
        return rules.size
    }

    private fun withInstanceRules(
        cache: Map<String, List<ThinkingRule>>,
        instanceId: String,
        rules: List<ThinkingRule>,
    ): Map<String, List<ThinkingRule>> = cache.toMutableMap().apply {
        if (rules.isEmpty()) remove(instanceId) else put(instanceId, rules)
    }
}
"""

FIX_RESOLVER_NO_BUMP_SETALL = FIX_THINKING_RESOLVER_CLEAN.replace(
    """    fun setAllCustomRules(byInstance: Map<String, List<ThinkingRule>>) {
        customRulesCache = byInstance
        _customRulesRevision.value += 1
    }""",
    """    fun setAllCustomRules(byInstance: Map<String, List<ThinkingRule>>) {
        customRulesCache = byInstance
    }""",
)

FIX_RESOLVER_NO_BUMP_RESTORE = FIX_THINKING_RESOLVER_CLEAN.replace(
    """        customRulesCache = withInstanceRules(customRulesCache, instanceId, rules)
        _customRulesRevision.value += 1
        return rules.size""",
    """        customRulesCache = withInstanceRules(customRulesCache, instanceId, rules)
        return rules.size""",
)

FIX_RESOLVER_NO_DECL = FIX_THINKING_RESOLVER_CLEAN.replace(
    "    val customRulesRevision: StateFlow<Long> = _customRulesRevision.asStateFlow()\n", "")

# --- da1033-I07 (C14) fixtures: ThinkingRuleEditor seed-keyed form states ----

FIX_THINKING_EDITOR_CLEAN = """package com.rikkaminis.app.ui.settings

@Composable
fun ThinkingRuleEditorDialog(request: ThinkingRuleEditorRequest, onDismiss: () -> Unit) {
    val seed = request.seed
    var label by remember(seed) { mutableStateOf(seed.label) }
    var allModels by remember(seed) { mutableStateOf(seed.scope is ThinkingRule.Scope.AllModels) }
    var pattern by remember(seed) {
        mutableStateOf((seed.scope as? ThinkingRule.Scope.ModelPattern)?.pattern ?: "")
    }
    var choice by remember(seed) { mutableStateOf(FormatChoice.from(seed.wireFormat)) }
    var formatChanged by remember(seed) { mutableStateOf(false) }
    var sendOffValue by remember(seed) { mutableStateOf(formatOffValue(seed.wireFormat) != null) }
    var offValue by remember(seed) { mutableStateOf(formatOffValue(seed.wireFormat) ?: "") }
    var path by remember(seed) { mutableStateOf(formatPath(seed.wireFormat) ?: "") }
    var customValues by remember(seed) { mutableStateOf(customEditableValues(seed.wireFormat)) }
    var formatMenuOpen by remember { mutableStateOf(false) }
}
"""

FIX_THINKING_EDITOR_KEYLESS_LABEL = FIX_THINKING_EDITOR_CLEAN.replace(
    "var label by remember(seed) { mutableStateOf(seed.label) }",
    "var label by remember { mutableStateOf(seed.label) }")

FIX_THINKING_EDITOR_FOREIGN_KEY = FIX_THINKING_EDITOR_CLEAN.replace(
    "var offValue by remember(seed) { mutableStateOf(formatOffValue(seed.wireFormat) ?: \"\") }",
    "var offValue by remember(request.existingId) { mutableStateOf(formatOffValue(seed.wireFormat) ?: \"\") }")

# Precision: the decision point is "does the key move with the seed", not the
# spelling — the exact-field key and the two-key form must stay green.
FIX_THINKING_EDITOR_FIELD_KEYED = FIX_THINKING_EDITOR_CLEAN.replace(
    "var label by remember(seed) { mutableStateOf(seed.label) }",
    "var label by remember(request.existingId, seed.label) { mutableStateOf(seed.label) }").replace(
    "var customValues by remember(seed) { mutableStateOf(customEditableValues(seed.wireFormat)) }",
    "var customValues by remember(seed.wireFormat) { mutableStateOf(customEditableValues(seed.wireFormat)) }")

# Precision: a transient UI flag derives from nothing and stays keyless; the
# `formatChanged` latch derives from nothing either, so neither is graded here.
FIX_THINKING_EDITOR_KEYLESS_UI_FLAGS = FIX_THINKING_EDITOR_CLEAN.replace(
    "var formatChanged by remember(seed) { mutableStateOf(false) }",
    "var formatChanged by remember { mutableStateOf(false) }")

FIX_THINKING_EDITOR_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""

CLEAN_TREE = {
    REL_PROVIDER_LIST: FIX_PROVIDER_LIST_CLEAN,
    REL_SHEET: FIX_SHEET_CLEAN,
    REL_PROVIDER_DETAIL: FIX_PROVIDER_DETAIL_CLEAN,
    REL_CONNECTION: FIX_CONNECTION_CLEAN,
    REL_ENTRY_DETAIL: FIX_ENTRY_DETAIL_CLEAN,
    REL_GROUP_DETAIL: FIX_GROUP_DETAIL_CLEAN,
    REL_MOUNT_DETAIL: FIX_MOUNT_DETAIL_CLEAN,
    REL_APP_NAV: FIX_APPNAV_CLEAN,
    REL_USAGE_STATS: FIX_USAGE_STATS_CLEAN,
    REL_THINKING_SECTION: FIX_THINKING_CLEAN,
    REL_THINKING_RESOLVER: FIX_THINKING_RESOLVER_CLEAN,
    REL_THINKING_EDITOR: FIX_THINKING_EDITOR_CLEAN,
}


def variant(rel, text):
    files = dict(CLEAN_TREE)
    files[rel] = text
    return files


def missing(rel):
    """Clean tree minus [rel] — the file-absent (fail-closed) arm."""
    files = dict(CLEAN_TREE)
    del files[rel]
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
             variant(REL_PROVIDER_DETAIL, FIX_PROVIDER_DETAIL_MISSING_ANCHOR), failures,
             ("C7", "C11c"))
    # --- da1027-E: C8 / C9 (field-keyed editors) ---------------------------
    run_case("c8-group-no-name-key", 1,
             variant(REL_GROUP_DETAIL, FIX_GROUP_DETAIL_NO_NAME_KEY), failures, ("C8",))
    run_case("failclosed-missing-group-anchor", 1,
             variant(REL_GROUP_DETAIL, FIX_GROUP_DETAIL_MISSING_ANCHOR), failures, ("C8",))
    run_case("c9-mount-no-write-key", 1,
             variant(REL_MOUNT_DETAIL, FIX_MOUNT_DETAIL_NO_WRITE_KEY), failures, ("C9",))
    run_case("failclosed-missing-mount-anchor", 1,
             variant(REL_MOUNT_DETAIL, FIX_MOUNT_DETAIL_MISSING_ANCHOR), failures, ("C9",))
    # --- da1027-E: C10a / C10b (usage page observes the config) ------------
    run_case("c10a-appnav-snapshot", 1,
             variant(REL_APP_NAV, FIX_APPNAV_SNAPSHOT), failures, ("C10a",))
    run_case("failclosed-missing-appnav-anchor", 1,
             variant(REL_APP_NAV, FIX_APPNAV_MISSING_ANCHOR), failures, ("C10a",))
    run_case("c10b-usage-effect-no-config-key", 1,
             variant(REL_USAGE_STATS, FIX_USAGE_STATS_NO_CONFIG_KEY), failures, ("C10b",))
    run_case("failclosed-missing-usage-anchor", 1,
             variant(REL_USAGE_STATS, FIX_USAGE_STATS_MISSING_ANCHOR), failures, ("C10b",))
    # --- da1028-C: C11a / C11b / C11c (ThinkingRulesSection derived keys) ---
    run_case("c11a-builtins-no-baseurl-key", 1,
             variant(REL_THINKING_SECTION, FIX_THINKING_NO_BASEURL_KEY), failures, ("C11a",))
    run_case("c11a-builtins-no-modelEntries-key", 1,
             variant(REL_THINKING_SECTION, FIX_THINKING_BUILTINS_NO_MODEL_ENTRIES), failures, ("C11a",))
    run_case("c11b-sample-no-modelEntries-key", 1,
             variant(REL_THINKING_SECTION, FIX_THINKING_SAMPLE_NO_MODEL_ENTRIES), failures, ("C11b",))
    run_case("c11b-sample-keyless-remember", 1,
             variant(REL_THINKING_SECTION, FIX_THINKING_SAMPLE_KEYLESS), failures, ("C11b",))
    # precision: a remember this criterion does not own (keyless UI flag + a
    # Room loader) must NOT turn the gate red
    run_case("c11-precision-unrelated-remember-green", 0,
             variant(REL_THINKING_SECTION, FIX_THINKING_PRECISION_UNRELATED), failures)
    run_case("failclosed-missing-thinking-anchor", 1,
             variant(REL_THINKING_SECTION, FIX_THINKING_MISSING_ANCHOR), failures,
             ("C11a", "C11b", "C12", "C15a", "C15b"))
    # --- da1030-C: C12 (external rule writes must re-key the loader) -------
    run_case("c12-loader-no-revision-key", 1,
             variant(REL_THINKING_SECTION, FIX_THINKING_NO_REVISION_KEY), failures, ("C12",))
    run_case("c12-no-revision-subscription", 1,
             variant(REL_THINKING_SECTION, FIX_THINKING_NO_SUBSCRIPTION), failures, ("C12",))
    # --- da1030-C: C13 (every mutator bumps the revision) ------------------
    run_case("c13-setall-no-bump", 1,
             variant(REL_THINKING_RESOLVER, FIX_RESOLVER_NO_BUMP_SETALL), failures, ("C13",))
    run_case("c13-restore-no-bump", 1,
             variant(REL_THINKING_RESOLVER, FIX_RESOLVER_NO_BUMP_RESTORE), failures, ("C13",))
    run_case("c13-no-revision-declaration", 1,
             variant(REL_THINKING_RESOLVER, FIX_RESOLVER_NO_DECL), failures, ("C13",))
    run_case("failclosed-missing-thinking-resolver", 1,
             missing(REL_THINKING_RESOLVER), failures, ("FILE",))
    run_case("c11c-detail-call-site-undrilled", 1,
             variant(REL_PROVIDER_DETAIL, FIX_PROVIDER_DETAIL_NO_SECTION_ARGS), failures, ("C11c",))
    # --- da1033-I07: C15 (the dialog is handed the LIVE rule) ---------------
    run_case("c15-prefix-dialog-snapshot", 1,
             variant(REL_THINKING_SECTION, FIX_THINKING_PREFIX_DIALOG), failures,
             ("C15a", "C15b"))
    run_case("c15-dialog-stale-request", 1,
             variant(REL_THINKING_SECTION, FIX_THINKING_DIALOG_STALE_REQUEST), failures, ("C15a",))
    run_case("c15-no-close-effect", 1,
             variant(REL_THINKING_SECTION, FIX_THINKING_NO_CLOSE_EFFECT), failures, ("C15b",))
    run_case("c15-seed-not-by-id", 1,
             variant(REL_THINKING_SECTION, FIX_THINKING_SEED_NOT_BY_ID), failures, ("C15a",))
    # precision: the new-rule path seeds itself and must NOT be flagged
    run_case("c15-new-rule-seed-precision", 0,
             variant(REL_THINKING_SECTION, FIX_THINKING_NEW_RULE_SEED), failures)
    # --- da1033-I07: C14 (ThinkingRuleEditor seed keys) --------------------
    run_case("c14-editor-keyless-label", 1,
             variant(REL_THINKING_EDITOR, FIX_THINKING_EDITOR_KEYLESS_LABEL), failures, ("C14",))
    run_case("c14-editor-foreign-key", 1,
             variant(REL_THINKING_EDITOR, FIX_THINKING_EDITOR_FOREIGN_KEY), failures, ("C14",))
    run_case("c14-editor-field-keyed-precision", 0,
             variant(REL_THINKING_EDITOR, FIX_THINKING_EDITOR_FIELD_KEYED), failures)
    run_case("c14-editor-keyless-ui-flags-precision", 0,
             variant(REL_THINKING_EDITOR, FIX_THINKING_EDITOR_KEYLESS_UI_FLAGS), failures)
    run_case("failclosed-missing-thinking-editor", 1,
             missing(REL_THINKING_EDITOR), failures, ("FILE",))
    run_case("failclosed-missing-editor-anchor", 1,
             variant(REL_THINKING_EDITOR, FIX_THINKING_EDITOR_MISSING_ANCHOR), failures, ("C14",))
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

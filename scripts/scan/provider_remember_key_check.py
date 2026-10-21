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

  C16 EnvironmentVariablesScreen [da1034-B4] — the Add/Edit sheet is opened for an
      ID (`editEntryId`), and the target is re-resolved from the LIVE list every
      composition. When the entry is deleted externally (`minis-config remove
      envvars.<KEY>` / backup restore) the lookup yields null while `editEntryId`
      stays non-null, so `EnvVarFormSheet` silently drops to new-variable semantics
      (`isEditing == false`) and Save creates a NEW entry under the key the user
      believes they are editing. The derivation must therefore be paired with a
      close effect: a `LaunchedEffect` keyed on BOTH `editEntryId` and the list it
      resolves against (`entries`), which resets `editEntryId` when the target is
      gone. Graded on the DECISION (does it close ONLY when the target vanished),
      not the spelling: an inline existence test (`entries.none { … }`,
      `indexOfFirst(…) < 0`) counts, and so does a call to a file-local helper
      whose own body carries such a test (the helper is the decision point, kept
      pure so a JVM test can run it for real) — the same "decision, not spelling"
      rule as C14/C15. Closing unconditionally does NOT count (that would dismiss
      the sheet on any recomposition), and neither does the neighbouring deep-link
      effect, which also assigns `editEntryId = null` but derives from the pending
      request. Fail-closed when the edit gate / the resolving derivation is gone.

  C17 WebDavConfigDialog [da1034-B4] — the four form states are seeded from
      `initial`, which the screen fills in ASYNCHRONOUSLY (`LaunchedEffect` →
      `webDavStore.load()`, first frame `null` — [deep-audit-1033 I08]). A keyless
      `remember` froze the boxes at the first composition's value, so opening the
      dialog in the null window showed four empty fields and Save wrote those
      empties back over the real config. Every `remember(...)` inside the dialog
      whose initializer reads `initial?.<field>` must key on `initial`; states that
      derive from nothing (`testing` / `testResult` / `confirmClearPassword`) stay
      keyless and are NOT graded. Same "initializer read" rule as C14 (counting the
      key list would be tautological). Fail-closed when the dialog is gone or no
      state derives from `initial.` any more (re-derive rather than delete).

  C20/C21 AddCustomModelScreen [da1039-B3 B-03] — the same母题 one level up:
      the READ half of a stale snapshot. `providerRepository.instance(id)` is a
      one-shot `_config.value.instances.find { … }` read, so an instance deleted
      elsewhere (minis-config RPC / Debug RPC / backup restore / another screen)
      never recomposed this page, its `instance == null` guard never fired, and
      Save persisted a ModelEntry whose `providerInstanceId` no longer exists
      (`addEntry` checks duplicates only). C20 = subscribe to
      `providerRepository.config.collectAsState()` and derive the instance from
      `config.instances` (the one-shot read must be gone). C21 = the save path
      must re-check the parent against the LIVE list before the `addEntry` call,
      because the delete can land between the click and the IO hop.

  C22/C23 ProviderConnectionScreen [da1039-B3 B-04] — the same母题 on the WRITE
      side. `instance` is the composition-time snapshot, so every
      `updateInstance(instance.copy(…))` pushed the whole record back: a
      focus-loss save that ran after a toggle (or after an external write)
      silently reverted the newer fields, and the first-frame
      `onFocusChanged(isFocused = false)` Compose delivers on mount made every
      visit write twice. C22 = a live resolver (`fun currentInstance()` reading
      `config.instances`) must exist and EVERY `providerRepository.updateInstance`
      must go through it, directly or via a local assigned from it (decision, not
      spelling — the snapshot arg is reported by line). C23 = both focus-loss
      savers must route through the pure `pendingConnectionFieldWrite` decision
      (normalise blank → null, skip when unchanged) and the base-URL saver must
      no longer rewrite `customUserAgent` in the same copy.

  C24/C25 the Quick Test target [da1039-B3 B-08] — graded on BOTH screens that
      host the shared `modelEntryPickerItems` (AddModelsToGroupScreen and the
      agent-loop add sheet), so the sibling that never wired the channel cannot
      stay silently different. C24 = the target is held as a `String?` id, the
      picker wiring stores `it.id` (identity, not the object tapped), and the
      sheet is hosted with an `onDismiss` that clears the same state. C25 = a
      `LaunchedEffect` keyed on BOTH the pending id and the list it resolves
      against, whose body calls the vanished test and clears the state; closing
      unconditionally does NOT count (same rule as C16). A frozen `ModelEntry`
      kept feeding "Run again" the config the row had at tap time, and a deleted
      model left the sheet open against a row that no longer exists.

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
# da1034-B4 (B-07 / B-08)
REL_ENVVAR_SCREEN = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/EnvironmentVariablesScreen.kt"
REL_BACKUP_SCREEN = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/BackupSettingsScreen.kt"
# da1034-B4 (B-05 / B-06)
REL_ADD_MODELS = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/AddModelsToGroupScreen.kt"
REL_AGENT_LOOP_SHEETS = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/AgentLoopAddSheets.kt"
REL_MODEL_ENTRY_PICKER = "src/android/app/src/main/java/com/rikkaminis/app/ui/components/ModelEntryPicker.kt"
# da1039-B3 (B-03 / B-04 / B-08): the settings screens that read a config object
# once and then write through it.
REL_ADD_CUSTOM_MODEL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/AddCustomModelScreen.kt"

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

# ---- [da1039-B3] C20..C25: the write-path half of the same母题 -------------
# C20/C21 — AddCustomModelScreen reads its PARENT INSTANCE. `providerRepository
# .instance(id)` is a one-shot snapshot (`_config.value.instances.find { … }`):
# the page never recomposed when the instance was removed externally, so the
# `instance == null` guard never fired and Save minted an orphan ModelEntry.
ADD_CUSTOM_SUBSCRIPTION_RE = re.compile(
    r"providerRepository\s*\.\s*config\s*\.\s*collectAsState\s*\(")
ADD_CUSTOM_DERIVE_RE = re.compile(
    r"\bconfig\s*\.\s*instances\s*\.\s*(?:find|firstOrNull)\s*\{")
ONE_SHOT_INSTANCE_RE = re.compile(r"\bproviderRepository\s*\.\s*instance\s*\(")
# the save path re-checks the parent against the LIVE list before it writes
ADD_CUSTOM_LIVE_GUARD_RE = re.compile(
    r"(?:config\s*\.\s*value\s*\.\s*instances|\.instances\b)[\s\S]{0,160}?"
    r"\.(?:any|none|indexOfFirst)\s*\{[\s\S]{0,200}?instanceId")
ADD_ENTRY_CALL_RE = re.compile(r"\bproviderRepository\s*\.\s*addEntry\s*\(")

# C22/C23 — ProviderConnectionScreen writes back the composition-time snapshot.
LIVE_RESOLVER_NAME = "currentInstance"
LIVE_RESOLVER_DECL_RE = lambda name: re.compile(r"\bfun\s+" + re.escape(name) + r"\s*\(\s*\)")
LIVE_RESOLVER_READ_RE = re.compile(
    r"config\s*\.\s*instances\s*\.\s*(?:find|firstOrNull)\s*\{")
LIVE_LOCAL_RE = lambda name: re.compile(
    r"\bval\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*" + re.escape(name) + r"\(\s*\)")
UPDATE_INSTANCE_RE = re.compile(r"\bproviderRepository\s*\.\s*updateInstance\s*\(")
# the pure decision the two focus-loss savers route through
DIRTY_DECISION_NAME = "pendingConnectionFieldWrite"
DIRTY_DECISION_BODY_RE = re.compile(r"ifBlank\s*\{[\s\S]{0,80}?\}[\s\S]{0,120}?stored")
DIRTY_SKIP_WRITE_RE = re.compile(
    r"\?:\s*return\b|if\s*\([\s\S]{0,60}?==\s*null\s*\)\s*\{?\s*return\b")
SAVER_NAMES = ("saveBaseURLSettings", "saveUserAgentSetting")

# C24/C25 — the Quick Test target must survive an external write/delete.
QUICK_TEST_ID_RE = re.compile(
    r"\bvar\s+quickTestEntryId\b[\s\S]{0,160}?mutableStateOf\s*<\s*String\?\s*>")
QUICK_TEST_RESOLVER_NAME = "liveQuickTestEntry"
QUICK_TEST_VANISHED_NAME = "quickTestTargetVanished"
QUICK_TEST_WIRING_RE = re.compile(
    r"onQuickTest\s*=\s*\{[\s\S]{0,200}?quickTestEntryId\s*=\s*it\.id")
QUICK_TEST_SHEET_RE = re.compile(r"\bQuickTestSheet\s*\(")
QUICK_TEST_DISMISS_RE = re.compile(r"onDismiss\s*=\s*\{[\s\S]{0,80}?quickTestEntryId\s*=\s*null")

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

# ---- [da1034-B4] C16 / C17 ----
# C16 — the Add/Edit sheet of EnvironmentVariablesScreen: the edit gate, the
# derivation that can silently degrade to "new variable", and the close effect.
ENVVAR_EDIT_GATE_RE = re.compile(r"if\s*\(\s*showAddSheet\s*\|\|\s*editEntryId\s*!=\s*null\s*\)")
ENVVAR_EDITING_ANCHOR_RE = re.compile(r"editEntryId\?\.let\s*\{[^}]*entries\.find\s*\{")
ENVVAR_CLOSE_EFFECT_RE = re.compile(
    r"LaunchedEffect\s*\(([^)]*)\)\s*\{[\s\S]{0,900}?\beditEntryId\s*=\s*null")
# The existence test on the close path — inline, or inside a file-local helper the
# effect calls. `\< 0` / `== null` cover the index/lookup spellings the sibling
# criteria (C15b) already use; `.none` is the collection spelling.
VANISHED_EXISTS_RE = re.compile(
    r"\.none\s*[\({]|\.indexOfFirst\s*\(|\.indexOf\s*\(|<\s*0|==\s*-1|!in\b|==\s*null"
    r"|isEmpty\(\)|isNotEmpty\(\)")
IDENT_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# C17 — the WebDav dialog's states seeded from its `initial` argument.
INITIAL_FIELD_RE = re.compile(r"\binitial\?\.[A-Za-z_][A-Za-z0-9_]*")
WEBCONFIG_DIALOG = "WebDavConfigDialog"


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


def _close_condition_tests_vanished(code, span):
    """C16 arm — does this close-effect's condition test the edit target for
    existence? Graded on the decision, not the spelling: an inline collection /
    index test counts, and so does a call to a file-local helper whose own body
    carries such a test (the helper is the decision point, kept pure for the JVM
    test). Closing unconditionally counts as NO."""
    if VANISHED_EXISTS_RE.search(span):
        return True, "inline existence test"
    for name in IDENT_CALL_RE.findall(span):
        body = func_body(code, name)
        if body is not None and VANISHED_EXISTS_RE.search(body):
            return True, "via helper %s()" % name
    return False, "close path carries no existence test"


def check_envvar_screen(code):
    """C16 [da1034-B4] — the env-var edit sheet must close when its target vanishes.

    The sheet is opened for an ID; the target is re-resolved from the live list
    every composition. An external delete (`minis-config remove envvars.<KEY>` /
    backup restore) leaves `editEntryId` non-null while the lookup yields null, so
    the form sheet drops to new-variable semantics and Save creates a NEW entry
    under the key the user believes they are editing.

    Fail-closed arms: the edit gate line and the resolving derivation must both be
    present. The close effect must (a) key on BOTH `editEntryId` and `entries`
    (keying on the id alone would not re-run when the list loses the entry) and
    (b) close only when the target is gone (decision, not spelling — see
    _close_condition_tests_vanished)."""
    results = []
    if not ENVVAR_EDIT_GATE_RE.search(code):
        results.append(("C16", False,
                        "anchor `if (showAddSheet || editEntryId != null)` not found "
                        "(fail-closed: the sheet was reworked, re-derive this criterion)"))
        return results
    if not ENVVAR_EDITING_ANCHOR_RE.search(code):
        results.append(("C16", False,
                        "anchor `editEntryId?.let { … entries.find … }` not found (fail-closed: "
                        "the resolving derivation was reworked, re-derive this criterion)"))
        return results
    best = None
    ok = False
    for m in ENVVAR_CLOSE_EFFECT_RE.finditer(code):
        keys = m.group(1)
        span = m.group(0)
        if "editEntryId" not in keys or "entries" not in keys:
            best = best or ("no close effect keyed on (editEntryId, entries) — the "
                            "deep-link effect also writes `editEntryId = null` but derives "
                            "from the pending request")
            continue
        tested, why = _close_condition_tests_vanished(code, span)
        if tested:
            ok = True
            best = ("a LaunchedEffect keyed on (editEntryId, entries) closes the sheet "
                    "when the live lookup says the entry is gone (%s)" % why)
        else:
            best = "close effect does not test the edit target for existence (%s)" % why
        break
    results.append(("C16", ok, best or "no close effect for the edit sheet"))
    return results


def check_webdav_dialog(code):
    """C17 [da1034-B4] — every state seeded from the dialog's `initial` re-seeds with it.

    `initial` arrives asynchronously (the screen fills it in inside a LaunchedEffect,
    first frame null — [deep-audit-1033 I08]), so a keyless `remember` froze the four
    form fields at the first composition's value and Save wrote those empties back
    over the real config.

    Same "initializer read" rule as C14: only the initializer counts as a read.
    Fail-closed: the dialog body must exist and at least one remember must derive
    from `initial.` (dialog reworked → re-derive rather than silently pass)."""
    results = []
    body = func_body(code, WEBCONFIG_DIALOG)
    if body is None:
        results.append(("C17", False,
                        "no `fun WebDavConfigDialog(` body found (fail-closed: the dialog "
                        "was dropped or renamed, re-derive this criterion)"))
        return results
    derived = 0
    bad = []
    for line_no, keys, _span, init in iter_remember_calls(body, "remember"):
        sources = sorted(set(INITIAL_FIELD_RE.findall(init)))
        if not sources:
            continue  # derives from nothing (testing / testResult …) — not graded
        derived += 1
        if keys is None:
            bad.append("L%d: keyless remember derives from %s" % (line_no, ",".join(sources)))
            continue
        if "initial" not in keys:
            bad.append("L%d: initializer derives from %s but keys=[%s] cannot see an "
                       "async / restore update of the config"
                       % (line_no, ",".join(sources), " ".join(keys.split())))
    if derived == 0:
        results.append(("C17", False,
                        "no remember call in WebDavConfigDialog derives from `initial.` "
                        "(fail-closed: the dialog was reworked, re-derive this criterion)"))
        return results
    results.append(("C17", not bad,
                    "all %d initial-derived remember call(s) key on the config they were "
                    "seeded from" % derived if not bad else "; ".join(bad)))
    return results

# ---- [da1034-B4] C18 / C19 ----
# The picker's collapse state ("which provider sections are folded"). Both
# screens that render `modelEntryPickerItems` seeded their own
#   `val collapsedInstanceIds = remember(config) { mutableStateOf(
#        if (modalityFilter != null) emptySet() else config.instances.map { it.id }.toSet()) }`
# copy, keyed on the whole `ProviderConfig`. The config is a fresh object after
# ANY write (a Quick Test persisting the resolved image endpoint, refreshModels
# landing its result, a DTO → StateFlow re-emit), so an unrelated field change
# wiped the user's expand/collapse choice. C18 grades the two call sites, C19
# the shared single source that replaced them.
COLLAPSED_DECL_RE = re.compile(r"\bval\s+collapsedInstanceIds\s*(?::[^=\n]*)?(?:by|=)\s*")
COLLAPSED_SINGLE_SOURCE = "rememberCollapsedInstanceIds"
# Derivation from the instance LIST. `.instances.size` / `.instances.count` are
# deliberately NOT identity — that is the same mistake as a size-only refresh
# key — while anything else rooted at `<x>.instances` (`.map`, `.filter`,
# `.toSet`, `.firstOrNull`) counts.
INSTANCES_DERIVATION_RE = re.compile(
    r"\.\s*instances\b(?!\s*\.\s*(?:size|count|isNotEmpty|isEmpty)\b)")
VAL_DECL_RE = re.compile(r"\b(?:val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]*)?(?:by|=)\s*")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
IDENT_CALL_ONLY_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def brace_span(text, open_idx):
    """(open, end) of the balanced `{ … }` whose `{` is at [open_idx]."""
    depth, i, n = 1, open_idx + 1, len(text)
    while i < n and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[open_idx:i]


def paren_span(text, open_idx):
    """Index just past the `)` matching the `(` at [open_idx]."""
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def statement_tail(code, pos, limit=600):
    """Text of the expression starting at [pos] up to the end of the statement
    (a newline at zero paren/brace depth that is not a continuation)."""
    i = min(len(code), pos)
    n = min(len(code), pos + limit)
    while i < n and code[i] in " \t\r\n":
        i += 1
    pos = i
    depth = 0
    while i < n:
        c = code[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif c == "\n" and depth == 0:
            nxt = code[i + 1:].lstrip(" \t")
            if not nxt.startswith((".", "?", ":", "&&", "||", ")", "}")):
                break
        i += 1
    return code[pos:i]


def value_sources(code):
    """name -> initializer/body text for file-local `val`/`var` declarations and
    `fun`s, so a key can be resolved to the thing it is derived from."""
    decls = {}
    for m in VAL_DECL_RE.finditer(code):
        decls.setdefault(m.group(1), statement_tail(code, m.end()))
    for m in re.finditer(r"\bfun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", code):
        name = m.group(1)
        tail = code[m.end() - 1:]
        if not tail.startswith("("):
            continue
        end = paren_span(tail, 0)
        rest = tail[end:]
        eq = rest.find("=")
        brace = rest.find("{")
        if eq >= 0 and (brace < 0 or eq < brace):
            decls.setdefault(name, statement_tail(rest, eq + 1))
        elif brace >= 0:
            decls.setdefault(name, brace_span(rest, brace))
    return decls


def derives_from_instances(expr, decls, depth=0, seen=None):
    """Does [expr] derive from an instance LIST, directly or through the
    file-local values / helpers it names? Resolved by NAME, not by a hardcoded
    helper name, so renaming the single source does not silently open the gate
    (da1034-A2 lesson: grade the decision, not the spelling)."""
    if not expr:
        return False
    if INSTANCES_DERIVATION_RE.search(expr):
        return True
    if depth >= 4:
        return False
    seen = seen or set()
    for name in set(IDENT_RE.findall(expr)):
        if name in seen:
            continue
        sub = decls.get(name)
        if sub is None:
            continue
        if derives_from_instances(sub, decls, depth + 1, seen | {name}):
            return True
    return False


def check_collapsed_screen(code):
    """C18 [da1034-B4] — the picker collapse state survives a non-identity config write.

    Graded on the DECISION (does this state re-seed only when the instance set
    changes), not the spelling: delegating to the shared single source counts,
    an inline `remember(config.instances.map { … }.toSet())` counts, and so does
    any local the key list names that resolves back to `<x>.instances`.
    Fail-closed when the declaration is gone (screen reworked)."""
    results = []
    m = COLLAPSED_DECL_RE.search(code)
    if not m:
        results.append(("C18", False,
                        "no `val collapsedInstanceIds = …` declaration (fail-closed: the "
                        "screen was reworked — re-derive this criterion)"))
        return results
    head = code[m.end():].lstrip()
    if head.startswith(COLLAPSED_SINGLE_SOURCE + "("):
        results.append(("C18", True,
                        "seeds the collapse state through the shared single source "
                        "rememberCollapsedInstanceIds(…) instead of a private copy"))
        return results
    if not re.match(r"remember\s*[({]", head):
        results.append(("C18", False,
                        "collapsedInstanceIds is seeded by `%s…` — the gate grades a "
                        "remember(...) seed or the shared helper (fail-closed)"
                        % " ".join(head[:60].split())))
        return results
    _ln, keys, _span, _init = iter_remember_calls(head, "remember")[0]
    if keys is None:
        results.append(("C18", False,
                        "keyless `remember { … }`: the collapse state can never observe "
                        "a provider instance being added or removed"))
        return results
    flat = " ".join(keys.split())
    if derives_from_instances(keys, value_sources(code)):
        results.append(("C18", True,
                        "keys=[%s] resolve back to the instance list, so a config re-emit "
                        "that changes no instance while the user is picking no longer "
                        "resets every open section" % flat))
    else:
        results.append(("C18", False,
                        "keys=[%s] derive from neither the instance list nor anything "
                        "derived from it — an unrelated config write (Quick Test "
                        "persisting imageEndpointResolved, refreshModels landing, a DTO → "
                        "StateFlow emit) resets every open section" % flat))
    return results


def check_collapsed_helper(code):
    """C19 [da1034-B4] — the shared single source keys its state on the id set.

    The screen criterion (C18) accepts delegation, so the real decision lives
    here: the state-producing `remember` (the one whose lambda builds a
    `mutableStateOf`) must key on something derived from the instance list.
    Fail-closed when the helper, its state remember, or any `<x>.instances`
    derivation in the file is gone."""
    results = []
    body = func_body(code, COLLAPSED_SINGLE_SOURCE)
    if body is None:
        results.append(("C19", False,
                        "no `fun rememberCollapsedInstanceIds(` body found (fail-closed: "
                        "the shared single source was dropped or renamed — re-derive this "
                        "criterion)"))
        return results
    decls = value_sources(code)
    states = [(ln, keys) for ln, keys, _span, init in iter_remember_calls(body)
              if "mutableStateOf" in init]
    if not states:
        results.append(("C19", False,
                        "rememberCollapsedInstanceIds no longer produces its state through "
                        "`remember { mutableStateOf(…) }` (fail-closed: re-derive)"))
        return results
    bad = []
    for ln, keys in states:
        if keys is None:
            bad.append("L%d: keyless state — the collapse set is frozen at the first "
                       "composition" % ln)
            continue
        flat = " ".join(keys.split())
        if not derives_from_instances(keys, decls):
            bad.append("L%d: state keys=[%s] are not derivable from the instance list — a "
                       "config re-emit that adds/removes no instance resets the sections"
                       % (ln, flat))
    if not any(v and INSTANCES_DERIVATION_RE.search(v) for v in decls.values()):
        bad.append("nothing in this file derives from `<x>.instances` at all — the state is "
                   "not seeded from the instance set (fail-closed)")
    results.append(("C19", not bad,
                    "the shared collapse state re-seeds only with the instance-id set"
                    if not bad else "; ".join(bad)))
    return results

def _resolver_window(code, name, limit=220):
    """Text right after `fun <name>( )` up to the first blank line (where the
    next declaration starts), so a reverted body cannot borrow a
    `<x>.instances.find {` match from further down the file. None when the
    declaration itself is gone."""
    m = LIVE_RESOLVER_DECL_RE(name).search(code)
    if not m:
        return None
    w = code[m.end():m.end() + limit]
    cut = w.find("\n\n")
    return w if cut < 0 else w[:cut]


def check_add_custom_model(code):
    """C20 / C21 [da1039-B3 B-03] — AddCustomModelScreen must read its parent
    instance reactively and re-check it against the live list before it writes.

    C20 grades the READ decision: subscribe to the config flow and derive the
    instance from `config.instances`. The one-shot `providerRepository.instance(id)`
    read is the defect (`_config.value` snapshot — an external delete never
    recomposes the page, so the `instance == null` guard never fires).
    C21 grades the WRITE decision: the `addEntry` call must be preceded by an
    existence test over the live instance list, because `addEntry` only checks
    for duplicates — a delete landing between the click and the IO hop would
    otherwise persist an entry pointing at a provider instance that is gone.
    Fail-closed when the save path has no `addEntry(` call at all."""
    results = []
    problems = []
    if not ADD_CUSTOM_SUBSCRIPTION_RE.search(code):
        problems.append("no `providerRepository.config.collectAsState()` — the instance is still read "
                        "as a one-shot snapshot")
    if not ADD_CUSTOM_DERIVE_RE.search(code):
        problems.append("the instance is not derived from `config.instances` (fail-closed: re-derive "
                        "this criterion if the screen was reworked)")
    if ONE_SHOT_INSTANCE_RE.search(code):
        problems.append("`providerRepository.instance(` is still called — that read does not subscribe")
    results.append(("C20", not problems,
                    "subscribes to the config flow and derives the parent instance from it"
                    if not problems else "; ".join(problems)))

    entry = ADD_ENTRY_CALL_RE.search(code)
    if not entry:
        results.append(("C21", False,
                        "no `providerRepository.addEntry(` call in the save path (fail-closed: "
                        "re-derive this criterion)"))
        return results
    window = code[max(0, entry.start() - 900):entry.start()]
    guard = ADD_CUSTOM_LIVE_GUARD_RE.search(window)
    results.append(("C21", bool(guard),
                    "the save path re-checks the parent against the live instance list before it writes"
                    if guard else
                    "the write is not preceded by a live-instance existence test — a delete landing "
                    "between the click and the IO hop still persists an orphan ModelEntry"))
    return results


def check_connection_write_path(code):
    """C22 / C23 [da1039-B3 B-04] — ProviderConnectionScreen must write through
    the LIVE instance, and a blur that changed nothing must not write.

    C22 grades the write target: `instance` is the composition-time snapshot, so
    every `updateInstance(instance.copy(…))` pushed the whole record back — a
    focus-loss save running after a toggle (or after an external minis-config /
    RPC / backup-restore write) silently reverted the newer fields. The file must
    declare a live resolver (`fun currentInstance()` reading `config.instances`)
    and every write must go through it — directly or through a local assigned
    from it (decision, not spelling). Fail-closed when the resolver or the write
    calls are gone.
    C23 grades the skip-if-unchanged decision: Compose delivers
    `onFocusChanged(isFocused = false)` on the first frame after mounting, so a
    saver without a dirty test writes (and logs) on every visit. Both savers must
    route through the pure decision helper, and the base-URL saver must no longer
    rewrite `customUserAgent` in the same copy (cross-field write)."""
    results = []
    window = _resolver_window(code, LIVE_RESOLVER_NAME)
    reads_live = bool(window is not None and LIVE_RESOLVER_READ_RE.search(window))
    live_locals = set(LIVE_LOCAL_RE(LIVE_RESOLVER_NAME).findall(code))

    calls = list(UPDATE_INSTANCE_RE.finditer(code))
    if not calls:
        results.append(("C22", False,
                        "no `providerRepository.updateInstance(` call found (fail-closed: re-derive "
                        "this criterion)"))
    else:
        bad = []
        for m in calls:
            args = call_args(code, m.end() - 1) or ""
            if LIVE_RESOLVER_NAME + "(" in args:
                continue
            if any(re.search(r"\b" + re.escape(v) + r"\b", args) for v in live_locals):
                continue
            bad.append("L%d: `%s` is the composition-time snapshot"
                       % (code.count("\n", 0, m.start()) + 1, " ".join(args.split())[:70]))
        if not reads_live:
            bad.insert(0, "no `fun %s()` resolving from `config.instances` (fail-closed: the live "
                          "resolver is gone — re-derive this criterion)" % LIVE_RESOLVER_NAME)
        results.append(("C22", not bad,
                        "all %d updateInstance writes go through %s(), which reads the live instance"
                        % (len(calls), LIVE_RESOLVER_NAME) if not bad else "; ".join(bad)))

    problems = []
    helper = func_body(code, DIRTY_DECISION_NAME)
    if helper is None or not DIRTY_DECISION_BODY_RE.search(helper):
        problems.append("no `%s(` decision that compares the normalised edit against the stored value "
                        "(fail-closed: an always-write helper re-opens the first-frame write)"
                        % DIRTY_DECISION_NAME)
    for name in SAVER_NAMES:
        body = func_body(code, name)
        if body is None:
            problems.append("no `fun %s(` body (fail-closed: re-derive this criterion)" % name)
            continue
        if DIRTY_DECISION_NAME not in body:
            problems.append("%s does not route through %s(" % (name, DIRTY_DECISION_NAME))
        elif not DIRTY_SKIP_WRITE_RE.search(body):
            problems.append("%s ignores the decision (no early return when the value did not change)"
                            % name)
    base_body = func_body(code, SAVER_NAMES[0])
    if base_body is not None and "customUserAgent" in base_body:
        problems.append("%s still writes customUserAgent in the same copy (a base-URL blur rewrites "
                        "an unrelated field)" % SAVER_NAMES[0])
    results.append(("C23", not problems,
                    "both focus-loss savers write only when the field changed, and the base-URL saver "
                    "no longer touches customUserAgent" if not problems else "; ".join(problems)))
    return results


def check_quick_test_target(code):
    """C24 / C25 [da1039-B3 B-08] — the Quick Test sheet must be opened for an
    ID resolved against the live entry list, and must close when that ID stops
    resolving.

    C24 grades the target decision: the state is a `String?` id, the picker
    wiring stores `it.id` (identity, not the object captured at tap time), the
    sheet is hosted, and its `onDismiss` resets the same state. A `ModelEntry`
    snapshot kept feeding "Run again" the config the row had when the lightning
    bolt was tapped.
    C25 grades the close-on-vanished decision: a `LaunchedEffect` keyed on BOTH
    the pending id and the list it resolves against, whose body calls the
    vanished test and clears the state (closing unconditionally does NOT count —
    that would dismiss the sheet on any recomposition). Both screens that host
    the shared picker are graded, so the sibling that never wired the channel
    cannot stay silently different."""
    results = []
    problems = []
    if not QUICK_TEST_ID_RE.search(code):
        problems.append("no `var quickTestEntryId` holding a String? id — the target is still the "
                        "ModelEntry captured when the row was tapped")
    if not QUICK_TEST_WIRING_RE.search(code):
        problems.append("no `onQuickTest = { … quickTestEntryId = it.id }` wiring — the row captures "
                        "the object instead of its identity, or the channel is missing")
    resolved = []
    list_ok = False
    for m in re.finditer(r"\b" + QUICK_TEST_RESOLVER_NAME + r"\s*\(", code):
        args = call_args(code, m.end() - 1) or ""
        if "quickTestEntryId" in args:
            resolved.append(m)
            if "modelEntries" in args:
                list_ok = True
    if not resolved:
        problems.append("`%s(` is never called with the pending id — the sheet keeps the tapped copy"
                        % QUICK_TEST_RESOLVER_NAME)
    elif not list_ok:
        problems.append("`%s(` does not resolve against `<x>.modelEntries`" % QUICK_TEST_RESOLVER_NAME)
    if not QUICK_TEST_SHEET_RE.search(code) or not QUICK_TEST_DISMISS_RE.search(code):
        problems.append("no `QuickTestSheet(` host whose onDismiss clears `quickTestEntryId`")
    results.append(("C24", not problems,
                    "the Quick Test target is held as an id, wired from the row identity and resolved "
                    "against the live entry list" if not problems else "; ".join(problems)))

    close = []
    for line_no, keys, _span, init in iter_remember_calls(code, "LaunchedEffect"):
        if QUICK_TEST_VANISHED_NAME not in init or "quickTestEntryId = null" not in init:
            continue
        flat = keys or ""
        if "quickTestEntryId" not in flat or "modelEntries" not in flat:
            continue
        close.append(line_no)
    results.append(("C25", bool(close),
                    "the sheet closes through a LaunchedEffect keyed on the pending id AND the live "
                    "entry list (L%s)" % ",".join(str(x) for x in close) if close else
                    "no `LaunchedEffect(<pending id>, <entry list>)` that clears the state when "
                    "`%s(` says the target is gone — a deleted model leaves the sheet open"
                    % QUICK_TEST_VANISHED_NAME))
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
    (REL_ENVVAR_SCREEN, check_envvar_screen),
    (REL_BACKUP_SCREEN, check_webdav_dialog),
    (REL_ADD_MODELS, check_collapsed_screen),
    (REL_AGENT_LOOP_SHEETS, check_collapsed_screen),
    (REL_MODEL_ENTRY_PICKER, check_collapsed_helper),
    # da1039-B3 (B-03 / B-04 / B-08)
    (REL_ADD_CUSTOM_MODEL, check_add_custom_model),
    (REL_CONNECTION, check_connection_write_path),
    (REL_ADD_MODELS, check_quick_test_target),
    (REL_AGENT_LOOP_SHEETS, check_quick_test_target),
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

fun pendingConnectionFieldWrite(edited: String, stored: String?): String? {
    val normalized = edited.ifBlank { null }
    return if (normalized == stored) null else normalized
}

fun ProviderConnectionScreen(instanceId: String, instance: ProviderInstance) {
    var customBaseURL by rememberSaveable(instance.id, instance.customBaseURL) { mutableStateOf(instance.customBaseURL ?: "") }
    var customUserAgent by rememberSaveable(instance.id, instance.customUserAgent) { mutableStateOf(instance.customUserAgent ?: "") }

    fun currentInstance(): ProviderInstance =
        config.instances.find { it.id == instanceId } ?: instance

    fun saveBaseURLSettings() {
        val live = currentInstance()
        val pending = pendingConnectionFieldWrite(customBaseURL, live.customBaseURL) ?: return
        providerRepository.updateInstance(live.copy(customBaseURL = pending))
    }

    fun saveUserAgentSetting() {
        val live = currentInstance()
        val pending = pendingConnectionFieldWrite(customUserAgent, live.customUserAgent) ?: return
        providerRepository.updateInstance(live.copy(customUserAgent = pending))
    }

    SingleChoiceSegmentedButtonRow {
        SegmentedButton(onClick = { providerRepository.updateInstance(currentInstance().copy(useResponsesAPI = false)) })
    }
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

# --- da1039-B3 B-04 variants: the write-path criteria (C22 / C23) ---------

# one write site still pushes the composition-time snapshot
FIX_CONNECTION_SNAPSHOT_WRITE = FIX_CONNECTION_CLEAN.replace(
    "providerRepository.updateInstance(currentInstance().copy(useResponsesAPI = false))",
    "providerRepository.updateInstance(instance.copy(useResponsesAPI = false))")

# the resolver is back to returning the snapshot it captured
FIX_CONNECTION_NO_RESOLVER = FIX_CONNECTION_CLEAN.replace(
    """    fun currentInstance(): ProviderInstance =
        config.instances.find { it.id == instanceId } ?: instance""",
    "    fun currentInstance(): ProviderInstance = instance")

# the helper exists but no longer compares anything (always writes)
FIX_CONNECTION_HELPER_ALWAYS_WRITE = FIX_CONNECTION_CLEAN.replace(
    """    val normalized = edited.ifBlank { null }
    return if (normalized == stored) null else normalized""",
    "    return edited.ifBlank { null }")

# the base-URL saver writes unconditionally again (no dirty decision)
FIX_CONNECTION_NO_DIRTY_GUARD = FIX_CONNECTION_CLEAN.replace(
    """        val live = currentInstance()
        val pending = pendingConnectionFieldWrite(customBaseURL, live.customBaseURL) ?: return
        providerRepository.updateInstance(live.copy(customBaseURL = pending))""",
    """        val live = currentInstance()
        providerRepository.updateInstance(live.copy(customBaseURL = customBaseURL.ifBlank { null }))""")

# the cross-field write comes back (a base-URL blur rewrites the UA field)
FIX_CONNECTION_CROSS_FIELD_WRITE = FIX_CONNECTION_CLEAN.replace(
    "providerRepository.updateInstance(live.copy(customBaseURL = pending))",
    """providerRepository.updateInstance(
            live.copy(customBaseURL = pending, customUserAgent = pending)
        )""")

# --- da1039-B3 B-03 fixtures: the parent-instance read (C20 / C21) --------

FIX_ADD_CUSTOM_MODEL_CLEAN = """package com.rikkaminis.app.ui.settings

fun AddCustomModelScreen(instanceId: String) {
    val config by providerRepository.config.collectAsState()
    val instance = config.instances.find { it.id == instanceId }

    if (instance == null) {
        LaunchedEffect(Unit) { onBack() }
        return
    }

    MinisButton(onClick = {
        val entry = ModelEntry(providerInstanceId = instanceId, baseModel = model, isCustom = true)
        scope.launch {
            val parentStillThere = providerRepository.config.value.instances.any { it.id == instanceId }
            if (!parentStillThere) {
                onBack()
                return@launch
            }
            withContext(Dispatchers.IO) { providerRepository.addEntry(entry) }
            onBack()
        }
    })
}
"""

# the pre-fix read: a one-shot snapshot that never recomposes on an external delete
FIX_ADD_CUSTOM_MODEL_SNAPSHOT = FIX_ADD_CUSTOM_MODEL_CLEAN.replace(
    """    val config by providerRepository.config.collectAsState()
    val instance = config.instances.find { it.id == instanceId }""",
    "    val instance = providerRepository.instance(instanceId)")

# read is fixed, the write-time re-check is gone
FIX_ADD_CUSTOM_MODEL_NO_LIVE_GUARD = FIX_ADD_CUSTOM_MODEL_CLEAN.replace(
    """            val parentStillThere = providerRepository.config.value.instances.any { it.id == instanceId }
            if (!parentStillThere) {
                onBack()
                return@launch
            }
""",
    "")

FIX_ADD_CUSTOM_MODEL_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

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

# --- da1034-B4 (C16) fixtures: the env-var edit sheet's disappearance recheck ---

# The real shape: the deep-link effect (which also writes `editEntryId = null`
# but derives from the pending request) sits NEXT TO the close effect, so a
# criterion that merely looked for "some effect nulling editEntryId" would be
# satisfied by the wrong one. The keyless UI flags here are deliberate: C16
# grades the close effect, not every remember in the file.
FIX_ENVVAR_CLEAN = """package com.rikkaminis.app.ui.settings

internal fun editTargetVanished(
    entries: List<EnvVarRepository.EnvVarEntry>,
    editEntryId: String?,
): Boolean {
    if (editEntryId == null) return false
    return entries.none { it.id == editEntryId }
}

@Composable
fun EnvironmentVariablesScreen(envVarRepository: EnvVarRepository, onBack: () -> Unit) {
    val entries by envVarRepository.entries.collectAsState()
    var showAddSheet by remember { mutableStateOf(false) }
    var editEntryId by remember { mutableStateOf<String?>(null) }
    var deleteEntryId by remember { mutableStateOf<String?>(null) }
    val visibleKeys = remember { mutableStateOf(setOf<String>()) }
    val pendingCreate by DeepLinkCoordinator.pendingEnvVarCreate.collectAsState()
    LaunchedEffect(pendingCreate) {
        pendingCreate?.let {
            editEntryId = null
            showAddSheet = true
            DeepLinkCoordinator.consumePendingEnvVarCreate()
        }
    }
    if (showAddSheet || editEntryId != null) {
        LaunchedEffect(editEntryId, entries) {
            if (editTargetVanished(entries, editEntryId)) {
                editEntryId = null
            }
        }
        val editing = editEntryId?.let { id -> entries.find { it.id == id } }
        EnvVarFormSheet(editEntry = editing, envVarRepository = envVarRepository)
    }
}
"""

# The pre-fix tree: the sheet is opened for an id and the target is resolved from
# the live list, but nothing closes it when that lookup stops resolving.
FIX_ENVVAR_PREFIX = FIX_ENVVAR_CLEAN.replace(
    """        LaunchedEffect(editEntryId, entries) {
            if (editTargetVanished(entries, editEntryId)) {
                editEntryId = null
            }
        }
""", "")

# Closes on every recomposition instead of only when the target is gone.
FIX_ENVVAR_UNCONDITIONAL_CLOSE = FIX_ENVVAR_CLEAN.replace(
    """        LaunchedEffect(editEntryId, entries) {
            if (editTargetVanished(entries, editEntryId)) {
                editEntryId = null
            }
        }""",
    """        LaunchedEffect(editEntryId, entries) {
            editEntryId = null
        }""")

# The lookup is against the list, so the effect must re-run when the list moves:
# keyed on the id alone it never notices the entry disappearing.
FIX_ENVVAR_NO_ENTRIES_KEY = FIX_ENVVAR_CLEAN.replace(
    "LaunchedEffect(editEntryId, entries) {", "LaunchedEffect(editEntryId) {")

FIX_ENVVAR_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""

# --- da1034-B4 (C17) fixtures: the WebDav dialog's initial-seeded states --------

FIX_BACKUP_CLEAN = """package com.rikkaminis.app.ui.settings

@Composable
private fun WebDavConfigDialog(
    initial: WebDavConfig?,
    httpClient: OkHttpClient,
    onDismiss: () -> Unit,
    onSave: (WebDavConfig) -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var url by remember(initial) { mutableStateOf(initial?.url.orEmpty()) }
    var username by remember(initial) { mutableStateOf(initial?.username.orEmpty()) }
    var password by remember(initial) { mutableStateOf(initial?.password.orEmpty()) }
    var path by remember(initial) {
        mutableStateOf(initial?.path ?: WebDavConfig.DEFAULT_BACKUP_DIR)
    }
    var testing by remember { mutableStateOf(false) }
    var testResult by remember { mutableStateOf<String?>(null) }
    var confirmClearPassword by remember { mutableStateOf(false) }
    val runTest: () -> Unit = runTest@{
        val cfg = WebDavConfig(
            url = url,
            username = username,
            password = password.ifBlank { initial?.password.orEmpty() },
            path = path,
        )
        if (cfg.url.isBlank() || cfg.username.isBlank()) {
            return@runTest
        }
        scope.launch { testing = true }
    }
}
"""

# The pre-fix shape: four keyless states frozen at the first composition's
# `initial` (which is still null while the async load runs).
FIX_BACKUP_KEYLESS = FIX_BACKUP_CLEAN.replace("remember(initial) {", "remember {")

# Half-fixed: the value boxes re-seed, the credential/path ones do not.
FIX_BACKUP_PARTIAL = FIX_BACKUP_CLEAN.replace(
    "var password by remember(initial) {", "var password by remember {").replace(
    "var path by remember(initial) {", "var path by remember {")

# Keyed, but on something that cannot move when `initial` moves.
FIX_BACKUP_FOREIGN_KEY = FIX_BACKUP_CLEAN.replace(
    "var url by remember(initial) {", "var url by remember(httpClient) {")

FIX_BACKUP_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""

# --- da1034-B4 fixtures: C18 (call sites) / C19 (shared single source) -----

FIX_ADD_MODELS_CLEAN = """package com.rikkaminis.app.ui.settings

@Composable
fun AddModelsToGroupScreen(modalityFilter: PickerModalityFilter? = null) {
    val collapsedInstanceIds = rememberCollapsedInstanceIds(
        config = config,
        defaultExpanded = modalityFilter != null,
    )
    var quickTestEntryId by remember { mutableStateOf<String?>(null) }
    modelEntryPickerItems(
        onQuickTest = { if (!SystemVoiceEntries.isSystemEntryId(it.id)) quickTestEntryId = it.id },
    )
    val quickTestEntry = liveQuickTestEntry(quickTestEntryId, config.modelEntries)
    LaunchedEffect(quickTestEntryId, config.modelEntries) {
        if (quickTestTargetVanished(quickTestEntryId, config.modelEntries)) quickTestEntryId = null
    }
    quickTestEntry?.let { entry ->
        QuickTestSheet(entry = entry, onDismiss = { quickTestEntryId = null })
    }
}
"""

FIX_AGENT_LOOP_SHEETS_CLEAN = """package com.rikkaminis.app.ui.settings

@Composable
fun AgentLoopGroupsSheet(config: ProviderConfig) {
    val collapsedInstanceIds = rememberCollapsedInstanceIds(config)
    var quickTestEntryId by remember { mutableStateOf<String?>(null) }
    modelEntryPickerItems(
        onQuickTest = { if (!SystemVoiceEntries.isSystemEntryId(it.id)) quickTestEntryId = it.id },
    )
    val quickTestEntry = liveQuickTestEntry(quickTestEntryId, config.modelEntries)
    LaunchedEffect(quickTestEntryId, config.modelEntries) {
        if (quickTestTargetVanished(quickTestEntryId, config.modelEntries)) quickTestEntryId = null
    }
    quickTestEntry?.let { entry ->
        QuickTestSheet(entry = entry, onDismiss = { quickTestEntryId = null })
    }
}
"""

FIX_MODEL_ENTRY_PICKER_CLEAN = """package com.rikkaminis.app.ui.components

internal fun providerInstanceIds(config: ProviderConfig): Set<String> =
    config.instances.map { it.id }.toSet()

internal fun collapsedInstanceIdsSeed(instanceIds: Set<String>, defaultExpanded: Boolean): Set<String> =
    if (defaultExpanded) emptySet() else instanceIds

@Composable
fun rememberCollapsedInstanceIds(
    config: ProviderConfig,
    defaultExpanded: Boolean = false,
): MutableState<Set<String>> {
    val instanceIds = remember(config) { providerInstanceIds(config) }
    return remember(instanceIds, defaultExpanded) {
        mutableStateOf(collapsedInstanceIdsSeed(instanceIds, defaultExpanded))
    }
}
"""

# C18 — a screen that kept its own copy keyed on the whole config object.
FIX_ADD_MODELS_BARE_CONFIG_KEY = FIX_ADD_MODELS_CLEAN.replace(
    """    val collapsedInstanceIds = rememberCollapsedInstanceIds(
        config = config,
        defaultExpanded = modalityFilter != null,
    )""",
    """    val collapsedInstanceIds = remember(config) {
        mutableStateOf(
            if (modalityFilter != null) emptySet()
            else config.instances.map { it.id }.toSet(),
        )
    }""")

FIX_ADD_MODELS_KEYLESS = FIX_ADD_MODELS_CLEAN.replace(
    """    val collapsedInstanceIds = rememberCollapsedInstanceIds(
        config = config,
        defaultExpanded = modalityFilter != null,
    )""",
    """    val collapsedInstanceIds = remember {
        mutableStateOf(config.instances.map { it.id }.toSet())
    }""")

# a key that never moves with the instance list (only the voice flag)
FIX_ADD_MODELS_FLAG_ONLY_KEY = FIX_ADD_MODELS_CLEAN.replace(
    """    val collapsedInstanceIds = rememberCollapsedInstanceIds(
        config = config,
        defaultExpanded = modalityFilter != null,
    )""",
    """    val collapsedInstanceIds = remember(modalityFilter != null) {
        mutableStateOf(config.instances.map { it.id }.toSet())
    }""")

FIX_ADD_MODELS_MISSING_ANCHOR = """package com.rikkaminis.app.ui.settings

fun unrelated() {}
"""

# the second call site, same defect (a per-file criterion must cover both)
FIX_AGENT_LOOP_BARE_CONFIG_KEY = FIX_AGENT_LOOP_SHEETS_CLEAN.replace(
    "val collapsedInstanceIds = rememberCollapsedInstanceIds(config)",
    """val collapsedInstanceIds = remember(config) {
        mutableStateOf(config.instances.map { it.id }.toSet())
    }""")

# an accepted alternative shape: the derivation stays inline at the call site
FIX_ADD_MODELS_INLINE_DERIVATION = FIX_ADD_MODELS_CLEAN.replace(
    """    val collapsedInstanceIds = rememberCollapsedInstanceIds(
        config = config,
        defaultExpanded = modalityFilter != null,
    )""",
    """    val instanceIds = config.instances.map { it.id }.toSet()
    val collapsedInstanceIds = remember(instanceIds, modalityFilter != null) {
        mutableStateOf(collapsedInstanceIdsSeed(instanceIds, modalityFilter != null))
    }""")

# a call site that also holds an unrelated keyless remember must stay green
FIX_ADD_MODELS_UNRELATED_REMEMBER = FIX_ADD_MODELS_CLEAN.replace(
    """    val collapsedInstanceIds = rememberCollapsedInstanceIds(
        config = config,
        defaultExpanded = modalityFilter != null,
    )""",
    """    val collapsedInstanceIds = rememberCollapsedInstanceIds(
        config = config,
        defaultExpanded = modalityFilter != null,
    )
    val scratch = remember { mutableStateOf("") }
    val unrelated = remember(config) { config.modelEntries }""")

# C19 — the single source is keyed on the whole config instead of the id set.
FIX_PICKER_STATE_BARE_CONFIG_KEY = FIX_MODEL_ENTRY_PICKER_CLEAN.replace(
    "    return remember(instanceIds, defaultExpanded) {",
    "    return remember(config, defaultExpanded) {")
FIX_PICKER_STATE_KEYLESS = FIX_MODEL_ENTRY_PICKER_CLEAN.replace(
    "    return remember(instanceIds, defaultExpanded) {",
    "    return remember {")

# the id set is only counted — the same mistake as a size-only refresh key
FIX_PICKER_SIZE_ONLY_KEY = FIX_MODEL_ENTRY_PICKER_CLEAN.replace(
    "val instanceIds = remember(config) { providerInstanceIds(config) }",
    "val instanceIds = remember(config) { config.instances.size }")

FIX_PICKER_MISSING_HELPER = """package com.rikkaminis.app.ui.components

internal fun providerInstanceIds(config: ProviderConfig): Set<String> =
    config.instances.map { it.id }.toSet()
"""

# --- da1039-B3 B-08 variants: the Quick Test target (C24 / C25) -----------

# the pre-fix shape: the tapped ModelEntry object, no id, no close effect
FIX_ADD_MODELS_SNAPSHOT_TARGET = FIX_ADD_MODELS_CLEAN.replace(
    """    var quickTestEntryId by remember { mutableStateOf<String?>(null) }""",
    """    var quickTestEntry by remember { mutableStateOf<ModelEntry?>(null) }""").replace(
    "quickTestEntryId = it.id }", "quickTestEntry = it }").replace(
    """    val quickTestEntry = liveQuickTestEntry(quickTestEntryId, config.modelEntries)
    LaunchedEffect(quickTestEntryId, config.modelEntries) {
        if (quickTestTargetVanished(quickTestEntryId, config.modelEntries)) quickTestEntryId = null
    }
""", "").replace(
    "onDismiss = { quickTestEntryId = null }", "onDismiss = { quickTestEntry = null }")

# the sibling keeps the shared picker but never wires the channel
FIX_AGENT_LOOP_NO_QUICK_TEST = FIX_AGENT_LOOP_SHEETS_CLEAN.replace(
    """    var quickTestEntryId by remember { mutableStateOf<String?>(null) }
    modelEntryPickerItems(
        onQuickTest = { if (!SystemVoiceEntries.isSystemEntryId(it.id)) quickTestEntryId = it.id },
    )
    val quickTestEntry = liveQuickTestEntry(quickTestEntryId, config.modelEntries)
    LaunchedEffect(quickTestEntryId, config.modelEntries) {
        if (quickTestTargetVanished(quickTestEntryId, config.modelEntries)) quickTestEntryId = null
    }
    quickTestEntry?.let { entry ->
        QuickTestSheet(entry = entry, onDismiss = { quickTestEntryId = null })
    }
""",
    """    modelEntryPickerItems(
        emptyTextRes = R.string.agent_loop_section_no_available_models,
    )
""")

# no close effect at all: the sheet stays open on a deleted target
FIX_ADD_MODELS_NO_CLOSE_EFFECT = FIX_ADD_MODELS_CLEAN.replace(
    """    LaunchedEffect(quickTestEntryId, config.modelEntries) {
        if (quickTestTargetVanished(quickTestEntryId, config.modelEntries)) quickTestEntryId = null
    }
""", "")

# closing unconditionally does NOT count (it would dismiss on any recomposition)
FIX_ADD_MODELS_UNCONDITIONAL_CLOSE = FIX_ADD_MODELS_CLEAN.replace(
    """    LaunchedEffect(quickTestEntryId, config.modelEntries) {
        if (quickTestTargetVanished(quickTestEntryId, config.modelEntries)) quickTestEntryId = null
    }""",
    """    LaunchedEffect(quickTestEntryId, config.modelEntries) {
        quickTestEntryId = null
    }""")

# keyed on the pending id only: a vanished target that still resolves against a
# stale list is never noticed
FIX_ADD_MODELS_NO_LIST_KEY = FIX_ADD_MODELS_CLEAN.replace(
    "LaunchedEffect(quickTestEntryId, config.modelEntries) {",
    "LaunchedEffect(quickTestEntryId) {")

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
    REL_ENVVAR_SCREEN: FIX_ENVVAR_CLEAN,
    REL_BACKUP_SCREEN: FIX_BACKUP_CLEAN,
    REL_ADD_MODELS: FIX_ADD_MODELS_CLEAN,
    REL_AGENT_LOOP_SHEETS: FIX_AGENT_LOOP_SHEETS_CLEAN,
    REL_MODEL_ENTRY_PICKER: FIX_MODEL_ENTRY_PICKER_CLEAN,
    REL_ADD_CUSTOM_MODEL: FIX_ADD_CUSTOM_MODEL_CLEAN,
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
             variant(REL_CONNECTION, FIX_CONNECTION_MISSING_ANCHOR), failures,
             ("C5", "C5a", "C22", "C23"))
    # --- da1039-B3: C20 / C21 (AddCustomModelScreen parent-instance read) --
    run_case("c20-custom-model-snapshot-read", 1,
             variant(REL_ADD_CUSTOM_MODEL, FIX_ADD_CUSTOM_MODEL_SNAPSHOT), failures, ("C20",))
    run_case("c21-custom-model-no-live-guard", 1,
             variant(REL_ADD_CUSTOM_MODEL, FIX_ADD_CUSTOM_MODEL_NO_LIVE_GUARD), failures, ("C21",))
    run_case("failclosed-missing-custom-model-anchor", 1,
             variant(REL_ADD_CUSTOM_MODEL, FIX_ADD_CUSTOM_MODEL_MISSING_ANCHOR), failures,
             ("C20", "C21"))
    # --- da1039-B3: C22 / C23 (connection write path) -----------------------
    run_case("c22-conn-snapshot-write", 1,
             variant(REL_CONNECTION, FIX_CONNECTION_SNAPSHOT_WRITE), failures, ("C22",))
    run_case("c22-conn-no-live-resolver", 1,
             variant(REL_CONNECTION, FIX_CONNECTION_NO_RESOLVER), failures, ("C22",))
    run_case("c23-conn-helper-always-write", 1,
             variant(REL_CONNECTION, FIX_CONNECTION_HELPER_ALWAYS_WRITE), failures, ("C23",))
    run_case("c23-conn-no-dirty-guard", 1,
             variant(REL_CONNECTION, FIX_CONNECTION_NO_DIRTY_GUARD), failures, ("C23",))
    run_case("c23-conn-cross-field-write", 1,
             variant(REL_CONNECTION, FIX_CONNECTION_CROSS_FIELD_WRITE), failures, ("C23",))
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
    # --- da1034-B4: C16 (env-var edit target disappearance) -----------------
    run_case("c16-envvar-prefix-no-close-effect", 1,
             variant(REL_ENVVAR_SCREEN, FIX_ENVVAR_PREFIX), failures, ("C16",))
    run_case("c16-envvar-unconditional-close", 1,
             variant(REL_ENVVAR_SCREEN, FIX_ENVVAR_UNCONDITIONAL_CLOSE), failures, ("C16",))
    run_case("c16-envvar-close-missing-entries-key", 1,
             variant(REL_ENVVAR_SCREEN, FIX_ENVVAR_NO_ENTRIES_KEY), failures, ("C16",))
    run_case("failclosed-missing-envvar-anchor", 1,
             variant(REL_ENVVAR_SCREEN, FIX_ENVVAR_MISSING_ANCHOR), failures, ("C16",))
    # precision: the clean fixture carries the deep-link effect (which also writes
    # `editEntryId = null`) plus keyless UI flags and stays green — C16 grades the
    # close effect, not every effect / remember in the file.
    # --- da1034-B4: C17 (WebDav dialog seeds) -------------------------------
    run_case("c17-webdav-keyless-seeds", 1,
             variant(REL_BACKUP_SCREEN, FIX_BACKUP_KEYLESS), failures, ("C17",))
    run_case("c17-webdav-partial-keys", 1,
             variant(REL_BACKUP_SCREEN, FIX_BACKUP_PARTIAL), failures, ("C17",))
    run_case("c17-webdav-foreign-key", 1,
             variant(REL_BACKUP_SCREEN, FIX_BACKUP_FOREIGN_KEY), failures, ("C17",))
    run_case("failclosed-missing-backup-anchor", 1,
             variant(REL_BACKUP_SCREEN, FIX_BACKUP_MISSING_ANCHOR), failures, ("C17",))
    run_case("failclosed-missing-backup-file", 1,
             missing(REL_BACKUP_SCREEN), failures, ("FILE",))
    # --- da1034-B4: C18 / C19 (the picker collapse state) -------------------
    run_case("c18-add-models-bare-config-key", 1,
             variant(REL_ADD_MODELS, FIX_ADD_MODELS_BARE_CONFIG_KEY), failures, ("C18",))
    run_case("c18-agent-loop-bare-config-key", 1,
             variant(REL_AGENT_LOOP_SHEETS, FIX_AGENT_LOOP_BARE_CONFIG_KEY), failures, ("C18",))
    run_case("c18-add-models-keyless", 1,
             variant(REL_ADD_MODELS, FIX_ADD_MODELS_KEYLESS), failures, ("C18",))
    run_case("c18-add-models-flag-only-key", 1,
             variant(REL_ADD_MODELS, FIX_ADD_MODELS_FLAG_ONLY_KEY), failures, ("C18",))
    run_case("c18-add-models-inline-derivation", 0,
             variant(REL_ADD_MODELS, FIX_ADD_MODELS_INLINE_DERIVATION), failures)
    run_case("c18-add-models-unrelated-remember", 0,
             variant(REL_ADD_MODELS, FIX_ADD_MODELS_UNRELATED_REMEMBER), failures)
    run_case("failclosed-missing-add-models-anchor", 1,
             variant(REL_ADD_MODELS, FIX_ADD_MODELS_MISSING_ANCHOR), failures, ("C18", "C24", "C25"))
    # --- da1039-B3: C24 / C25 (Quick Test target) ---------------------------
    run_case("c24-c25-add-models-snapshot-target", 1,
             variant(REL_ADD_MODELS, FIX_ADD_MODELS_SNAPSHOT_TARGET), failures, ("C24", "C25"))
    run_case("c24-c25-agent-loop-no-quick-test", 1,
             variant(REL_AGENT_LOOP_SHEETS, FIX_AGENT_LOOP_NO_QUICK_TEST), failures,
             ("C24", "C25"))
    run_case("c25-add-models-no-close-effect", 1,
             variant(REL_ADD_MODELS, FIX_ADD_MODELS_NO_CLOSE_EFFECT), failures, ("C25",))
    run_case("c25-add-models-unconditional-close", 1,
             variant(REL_ADD_MODELS, FIX_ADD_MODELS_UNCONDITIONAL_CLOSE), failures, ("C25",))
    run_case("c25-add-models-no-list-key", 1,
             variant(REL_ADD_MODELS, FIX_ADD_MODELS_NO_LIST_KEY), failures, ("C25",))
    run_case("c19-picker-state-bare-config-key", 1,
             variant(REL_MODEL_ENTRY_PICKER, FIX_PICKER_STATE_BARE_CONFIG_KEY), failures, ("C19",))
    run_case("c19-picker-state-keyless", 1,
             variant(REL_MODEL_ENTRY_PICKER, FIX_PICKER_STATE_KEYLESS), failures, ("C19",))
    run_case("c19-picker-size-only-key", 1,
             variant(REL_MODEL_ENTRY_PICKER, FIX_PICKER_SIZE_ONLY_KEY), failures, ("C19",))
    run_case("failclosed-missing-picker-helper", 1,
             variant(REL_MODEL_ENTRY_PICKER, FIX_PICKER_MISSING_HELPER), failures, ("C19",))
    run_case("failclosed-missing-picker-file", 1,
             missing(REL_MODEL_ENTRY_PICKER), failures, ("FILE",))
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

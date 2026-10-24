#!/usr/bin/env python3
"""envvar_config_group_check — [da1025-E] EnvVarEntry.group unified config-face registration.

Fixes the 6th instance of the field-evap recurrence class (GH#68 -> pinned ->
1021 reasoning -> 1022 OpenMinis compat -> 1023-13 -> 1025): EnvVarEntry gained
a `group` field wired through UI / persistence / backup, but the minis-config
surface was never registered:

  1. EnvVarsCollection.fields(forId) must return groupField(forId)
     (path "envvars.<key>.group") — else `minis-config get envvars.<key>.group`
     -> ConfigError.UnknownPath.
  2. That groupField must actually register path "envvars.$key.group"
     (or the forId variant) — a field listed but pathless is still unreachable.
  3. ConfigBuiltins' `envvars` aggregate summary linkedMapOf must contain a
     "group" key — else `minis-config get envvars` hides the grouping label.

[da1026-H / ISSUE-08] The same recurrence class, one surface over — the two
per-group session defaults were write-only on the `groups` face:
  4. GroupsCollection.add must funnel the CLI `context_limit_tokens` through
     `applyContextLimitWrite` — the exact helper the `contextLimitField`
     writer uses. Without it a CLI-created group keeps `lastContextLimitTokens`
     null, so the first clearing write (CLI 0 / the UI switch) leaves nothing
     for the group screen's OFF->ON toggle to restore and the limit silently
     snaps back to the 128K default.
  5. ConfigBuiltins' `groups` aggregate summary block must expose
     "default_thinking_level" and "context_limit_tokens" — else
     `minis-config get groups` hides both, i.e. an agent can set them at add
     time but never read them back.
  6. That same block must keep its five legacy keys (extraction sanity).
  7. The sibling `providers` summary must keep its "customUserAgent" key
     ([fix/da1020]) — guards against editing the wrong block in that file.

All criteria are fail-closed: if the anchor function/block cannot be extracted
from the (comment-stripped) source, the criterion is RED, never silently
skipped.

[da1028-B / ISSUE-03] Four more criteria pin the two halves of the same
field-evap class on the *backup scope* face:
  8.  ConfigBackup.BACKED_UP_SCOPES must contain "envvars" — else
      `envvars.privacyMode` is dropped from every backup AND refused on the way
      back in ("outside backup scope"), so migrating devices lose the privacy
      switch silently.
  9.  ConfigBuiltins.registerBackground must register path
      "background.overlay" — the same switch exists in Settings → Background
      but `minis-config set background.overlay …` answered "unknown field".
  10. That registration must reference BackgroundSettingsRepository's
      KEY_BG_OVERLAY_ENABLED / DEFAULT_BG_OVERLAY_ENABLED constants and must NOT
      re-spell the raw key literal (the F-238/F-239 single-source rule).
  11. Reverse anchors: the eight legacy scopes are still in BACKED_UP_SCOPES
      and `envvars.privacyMode` is still registered — guards a "fix" that
      swaps something out instead of adding.

Criteria 8–11 only run when the caller supplies a ConfigBackup.kt path; the
legacy mini-tree fixtures (which hold only the three config files) leave the
parameter out and are judged by 1–7 exactly as before.

Usage:
    python3 envvar_config_group_check.py <repo_root>   # real-tree mode
    python3 envvar_config_group_check.py --selftest    # fixture mode

Selftest ends with the tooth marker ENVVAR_GROUP_SELFTEST=PASS (missing marker
= the gate itself is broken / gutted -> red).
"""

import os
import re
import shutil
import sys  # [dv20-a1 A-02] fixture re-anchor
import tempfile

# --------------------------------------------------------------------------
# Comment stripping (a gate's own KDoc routinely quotes its anchors verbatim;
# match against comment-stripped text only).
# --------------------------------------------------------------------------


def strip_comments(src: str) -> str:
    out = []
    i = 0
    n = len(src)
    in_block = False
    in_line = False
    in_str = False
    str_ch = ""
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
                out.append(c)
            i += 1
            continue
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            if c == "\n":
                out.append(c)
            i += 1
            continue
        if in_str:
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(src[i + 1])
                i += 2
                continue
            if c == str_ch:
                in_str = False
            i += 1
            continue
        if c == "/" and nxt == "/":
            in_line = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if c in ('"', "'"):
            in_str = True
            str_ch = c
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# Balanced extraction (block bodies / paren blocks). Fail-closed: returns None
# when the block never balances (truncated source, wrong anchor, etc.).
# --------------------------------------------------------------------------


def extract_braced(text: str, header_re: str) -> str | None:
    """Extract the balanced {...} block that follows the first match of
    header_re. Returns None (fail-closed) if the header is absent or the
    block never balances."""
    m = re.search(header_re, text)
    if not m:
        return None
    i = m.end()
    while i < len(text) and text[i] != "{":
        if text[i] not in " \t\r\n":
            return None  # not a block body (e.g. expression body) -> fail-closed
        i += 1
    if i >= len(text):
        return None
    depth = 0
    start = i
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def extract_paren_block(text: str, start_idx: int) -> str | None:
    """Extract the balanced (...) block starting at the '(' at start_idx."""
    depth = 0
    i = start_idx
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start_idx : i + 1]
        i += 1
    return None


def func_window(text: str, header_re: str, stop_re: str = r"\n    (private |override )?fun |^}") -> str | None:
    """Declaration-text window: from the header match to the next function
    header / class end. Used for expression-body members where brace pairing
    silently grabs the WRONG block (da1025-FIX-F lesson)."""
    m = re.search(header_re, text)
    if not m:
        return None
    rest = text[m.start() :]
    m2 = re.search(stop_re, rest, re.M)
    if not m2:
        return rest
    return rest[: m2.start()]


# --------------------------------------------------------------------------
# The seven criteria. Each returns None (green) or a failure string (red).
# --------------------------------------------------------------------------

ENVVARS_REL = "EnvVarsCollection.kt"
BUILTINS_REL = "ConfigBuiltins.kt"
GROUPS_REL = "GroupsCollection.kt"
BACKUP_REL = "ConfigBackup.kt"


def check_fields_registration(envvars_src: str) -> str | None:
    """Criterion 1: fields(forId)'s return list contains groupField(forId)."""
    text = strip_comments(envvars_src)
    body = extract_braced(text, r"override fun fields\(forId: String\): List<ConfigField>")
    if body is None:
        return "C1 EXTRACT_FAIL: fields(forId) block body not found (fail-closed)"
    if "groupField(forId)" not in body:
        return "C1 RED: fields(forId) return list does not register groupField(forId)"
    return None


def enclosing_braces(text: str, pos: int) -> str:
    """The nearest balanced { … } spanning pos (the predicate/expression the
    site sits in). Returns the tail from the opener when it never closes."""
    depth = 0
    start = 0
    i = pos
    while i >= 0:
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
        i -= 1
    depth = 0
    j = start
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
        j += 1
    return text[start:]

# [wide-1024 12-misc-b] A boolean expression that is constant no matter what
# the input is: `x || !x`, `false && …`, `… && false`, `true || …`. Such a
# predicate keeps every anchor token the criteria look for and decides nothing.
DEAD_PREDICATE_RE = re.compile(
    r"(?:\bfalse\s*&&|&&\s*false\b|\btrue\s*\|\||\|\|\s*true\b|\bif\s*\(\s*false\s*\))"
)
# `x || !x` — the excluded middle reads as "always true" without spelling the
# literal `true`, so a check that only forbids the literal stays blind to it.
DEAD_TAUTOLOGY_RE = re.compile(
    r"([A-Za-z_][\w.]*(?:\([^()]*\))?)\s*\|\|\s*!\s*\1"
)

def check_group_path(envvars_src: str) -> str | None:
    """Criterion 2: a groupField member registers path "envvars.$key.group"
    (or the forId variant) AND its reader returns the stored group."""
    text = strip_comments(envvars_src)
    win = func_window(
        text,
        r"private fun groupField\(",
        stop_re=r"\n    private fun |\n    override fun |^}",
    )
    if win is None:
        return "C2 EXTRACT_FAIL: groupField member not found (fail-closed)"
    if not re.search(r'path\s*=\s*"envvars\.\$(key|forId)\.group"', win):
        return 'C2 RED: groupField does not register path "envvars.$key.group" (or forId variant)'
    # [wide-1024 12-misc-b] The path makes the field ADDRESSABLE; the reader is
    # what makes it ANSWER. `reader = { ConfigValue.Null }` keeps the path, the
    # writer and the UI, and turns the single-path read into a constant.
    body = extract_braced(win, r"reader\s*=")
    if body is None:
        return (
            "C2 EXTRACT_FAIL: groupField's `reader = { … }` lambda not found "
            "(fail-closed: the criterion pins what the reader yields)"
        )
    if not re.search(r"ConfigValue\.Str\(\s*[\w.]+\.group\s*\)", body):
        return (
            "C2 RED: groupField's reader never yields ConfigValue.Str(<entry>.group) — "
            "the group set through the writer (UI / `set`) is invisible on the "
            "single-path read `minis-config get envvars.<key>.group`"
        )
    if "repo." not in body:
        return (
            "C2 RED: groupField's reader does not read the repository — the group must "
            "come from the entry (single source), not from a literal"
        )
    return None


def check_summary_group(builtins_src: str) -> str | None:
    """Criterion 3: the `envvars` aggregate summary block contains a "group" key."""
    text = strip_comments(builtins_src)
    anchor = re.search(r'path\s*=\s*"envvars",', text)
    if not anchor:
        return "C3 EXTRACT_FAIL: envvars summary registration (path = \"envvars\",) not found (fail-closed)"
    # the enclosing r.register( ... ) block: back up to the nearest r.register(
    # opener before the anchor, then balance parens from there.
    reg_start = text.rfind("r.register(", 0, anchor.start())
    if reg_start < 0:
        return "C3 EXTRACT_FAIL: enclosing r.register( not found (fail-closed)"
    paren_idx = text.index("(", reg_start)
    block = extract_paren_block(text, paren_idx)
    if block is None:
        return "C3 EXTRACT_FAIL: envvars summary block never balances (fail-closed)"
    if '"group" to ConfigValue.Str' not in block:
        return "C3 RED: envvars summary linkedMapOf lacks a \"group\" key"
    if '"key" to ConfigValue.Str' not in block:
        return "C3 RED: envvars summary block lost its \"key\" entry (extraction sanity)"
    return None


def registered_block(text: str, anchor_re: str) -> str | None:
    """The enclosing `r.register( ... )` block of the first `anchor_re` match.
    Speculative-free: returns None (fail-closed) when the anchor is absent, no
    `r.register(` precedes it, or the parenthesis block never balances."""
    anchor = re.search(anchor_re, text)
    if not anchor:
        return None
    reg_start = text.rfind("r.register(", 0, anchor.start())
    if reg_start < 0:
        return None
    paren_idx = text.index("(", reg_start)
    return extract_paren_block(text, paren_idx)


def check_add_calls_context_limit_helper(groups_src: str) -> str | None:
    """Criterion 4: GroupsCollection.add funnels the CLI context limit through
    applyContextLimitWrite — the helper that IS the contextLimitField writer's
    body. A CLI-created group that skips it keeps lastContextLimitTokens null
    (see the gate docstring)."""
    text = strip_comments(groups_src)
    body = extract_braced(text, r"override fun add\(payload: ConfigValue\): String")
    if body is None:
        return "C4 EXTRACT_FAIL: GroupsCollection.add body not found (fail-closed)"
    if "applyContextLimitWrite(" not in body:
        return (
            "C4 RED: GroupsCollection.add never calls applyContextLimitWrite — "
            "a CLI-created group loses its limit on the first clearing write"
        )
    # [wide-1024 12-misc-b] Calling the helper is half the contract; the ARG is
    # the value. `applyContextLimitWrite(g, 0)` keeps the call, the helper and
    # the field, and drops the limit the caller passed (lastContextLimitTokens
    # stays 0 = "no limit"), so `minis-config add groups {contextLimitTokens:…}`
    # silently loses the parameter.
    parsed = re.search(
        r'val\s+(\w+)\s*=\s*\(obj\["context_limit_tokens"\]\s*as\?\s*ConfigValue\.Int\)', body)
    if parsed is None:
        return (
            'C4 EXTRACT_FAIL: add() no longer derives the limit from '
            'obj["context_limit_tokens"] (fail-closed: re-derive this criterion, '
            "never drop it)"
        )
    for m in re.finditer(r"applyContextLimitWrite\s*\(", body):
        args = extract_paren_block(body, m.end() - 1)
        if args is None:
            return "C4 EXTRACT_FAIL: applyContextLimitWrite( never balances (fail-closed)"
        if parsed.group(1) not in args:
            return (
                "C4 RED: applyContextLimitWrite is called with a limit that is not the "
                "parsed context_limit_tokens value — a CLI-created group silently loses "
                "the limit it was created with"
            )
    return None


def check_groups_summary_keys(builtins_src: str) -> str | None:
    """Criterion 5: the `groups` aggregate summary block exposes both per-group
    session defaults."""
    text = strip_comments(builtins_src)
    block = registered_block(text, r'path\s*=\s*"groups",')
    if block is None:
        return 'C5 EXTRACT_FAIL: groups summary registration (path = "groups",) not found (fail-closed)'
    if '"default_thinking_level" to' not in block:
        return 'C5 RED: groups summary block lacks a "default_thinking_level" key'
    if '"context_limit_tokens" to' not in block:
        return 'C5 RED: groups summary block lacks a "context_limit_tokens" key'
    return None


def check_groups_summary_legacy_keys(builtins_src: str) -> str | None:
    """Criterion 6: the `groups` summary block still carries its five legacy
    keys — an extraction sanity check that also catches a block rewrite that
    drops them."""
    text = strip_comments(builtins_src)
    block = registered_block(text, r'path\s*=\s*"groups",')
    if block is None:
        return 'C6 EXTRACT_FAIL: groups summary registration (path = "groups",) not found (fail-closed)'
    for key in ('"id" to', '"name" to', '"strategy" to', '"fallback_strategy" to', '"entries" to'):
        if key not in block:
            return f"C6 RED: groups summary block lost its {key} key"
    return None


def check_providers_summary_ua_key(builtins_src: str) -> str | None:
    """Criterion 7 (wrong-block guard, [fix/da1020]): the sibling `providers`
    summary must keep its customUserAgent key while the `groups` block above it
    is edited."""
    text = strip_comments(builtins_src)
    block = registered_block(text, r'path\s*=\s*"providers",')
    if block is None:
        return 'C7 EXTRACT_FAIL: providers summary registration (path = "providers",) not found (fail-closed)'
    if '"customUserAgent" to' not in block:
        return "C7 RED: providers summary block lost its customUserAgent key"
    return None

# --------------------------------------------------------------------------
# [da1028-B / ISSUE-03] Criteria 8-11 — the backup-scope face of the same
# field-evap class.
# --------------------------------------------------------------------------

LEGACY_BACKED_UP_SCOPES = (
    "appearance", "chat", "background", "defaults",
    "soul", "memory", "logs", "runtime",
)

def backed_up_scopes_block(backup_src: str) -> str | None:
    """The `setOf( … )` argument of BACKED_UP_SCOPES. None (fail-closed) when
    the declaration is absent or the parens never balance."""
    text = strip_comments(backup_src)
    m = re.search(r"val\s+BACKED_UP_SCOPES\s*=\s*setOf\(", text)
    if not m:
        return None
    return extract_paren_block(text, m.end() - 1)

def check_backup_scope_envvars(backup_src: str) -> str | None:
    """Criterion 8: BACKED_UP_SCOPES carries "envvars"."""
    block = backed_up_scopes_block(backup_src)
    if block is None:
        return "C8 EXTRACT_FAIL: ConfigBackup.BACKED_UP_SCOPES setOf(…) not found (fail-closed)"
    if '"envvars"' not in block:
        return (
            'C8 RED: BACKED_UP_SCOPES lacks "envvars" — envvars.privacyMode is '
            "dropped from every backup and refused on restore (\"outside backup scope\")"
        )
    return None

def check_overlay_registration(builtins_src: str) -> str | None:
    """Criterion 9: registerBackground registers path "background.overlay"."""
    text = strip_comments(builtins_src)
    body = extract_braced(text, r"fun registerBackground\(r: ConfigRegistry,\s*context: Context\)")
    if body is None:
        return (
            "C9 EXTRACT_FAIL: ConfigBuiltins.registerBackground(r: ConfigRegistry, context: Context) "
            "body not found (fail-closed)"
        )
    if 'path = "background.overlay"' not in body:
        return (
            'C9 RED: registerBackground does not register path "background.overlay" — '
            'the Settings → Background overlay switch is unreachable via minis-config '
            '("unknown field") and is in no backup scope'
        )
    return None

def check_overlay_single_source(builtins_src: str) -> str | None:
    """Criterion 10: the overlay registration references the repository's key +
    default constants and does not re-spell the raw key (F-238/F-239 rule: the
    registry and BackgroundSettingsRepository must name the key identically)."""
    text = strip_comments(builtins_src)
    body = extract_braced(text, r"fun registerBackground\(r: ConfigRegistry,\s*context: Context\)")
    if body is None:
        return (
            "C10 EXTRACT_FAIL: ConfigBuiltins.registerBackground(r: ConfigRegistry, context: Context) "
            "body not found (fail-closed)"
        )
    for const in ("KEY_BG_OVERLAY_ENABLED", "DEFAULT_BG_OVERLAY_ENABLED"):
        if const not in body:
            return (
                f"C10 RED: registerBackground does not reference "
                f"BackgroundSettingsRepository.{const} — a re-typed key/default "
                f"drifts out of that companion silently"
            )
    if '"backgroundOverlayEnabled"' in body:
        return (
            'C10 RED: registerBackground spells the raw key literal '
            '"backgroundOverlayEnabled" instead of the repository constant'
        )
    return None

def check_reverse_anchors(backup_src: str, builtins_src: str) -> str | None:
    """Criterion 11 (reverse anchors): the eight legacy scopes are still in
    BACKED_UP_SCOPES, and `envvars.privacyMode` is still a registered field —
    a fix that swaps one out for the other is not a fix."""
    block = backed_up_scopes_block(backup_src)
    if block is None:
        return "C11 EXTRACT_FAIL: ConfigBackup.BACKED_UP_SCOPES setOf(…) not found (fail-closed)"
    for scope in LEGACY_BACKED_UP_SCOPES:
        if '"%s"' % scope not in block:
            return f'C11 RED: BACKED_UP_SCOPES lost legacy scope "{scope}"'
    if not re.search(r'path\s*=\s*"envvars\.privacyMode"', strip_comments(builtins_src)):
        return 'C11 RED: path = "envvars.privacyMode" is no longer registered in ConfigBuiltins'
    return None


def check_sentinel_remap(backup_src: str) -> str | None:
    """Criterion 12 [dv20-a1 A-02]: the Stage 2 group-member remap keeps the
    virtual System voice ids instead of counting them as dropped members."""
    text = strip_comments(backup_src)
    drops = [m.start() for m in re.finditer(r"else\s+droppedMembers\+\+", text)]
    if not drops:
        return (
            "C12 EXTRACT_FAIL: ConfigBackup group-member remap "
            "(`else droppedMembers++`) not found (fail-closed)"
        )
    for pos in drops:
        window = text[max(0, pos - 400):pos]
        if "SystemVoiceEntries.isSystemEntryId" not in window:
            return (
                "C12 RED: the member remap drops a member whenever the entry map has "
                "no mapping — the virtual System voice ids are never in that map, so "
                "every voice-group member counts as a dropped member; the remap must "
                "consult SystemVoiceEntries.isSystemEntryId (single source)"
            )
        # [wide-1024 12-misc-b] …and the predicate must DECIDE. `takeIf { false &&
        # SystemVoiceEntries.isSystemEntryId(it) }` keeps the call, answers false
        # for every id, and every virtual member is dropped — the exact defect
        # this criterion exists to stop, wearing the anchor as a decoration.
        for call in re.finditer(r"SystemVoiceEntries\.isSystemEntryId\s*\(", window):
            pred = enclosing_braces(window, call.start())
            if DEAD_PREDICATE_RE.search(pred) or DEAD_TAUTOLOGY_RE.search(pred):
                return (
                    "C12 RED: the remap's isSystemEntryId predicate is constant "
                    "(`false && …` / `… || true`) or a tautology (`x || !x`) — it keeps "
                    "the call and "
                    "decides nothing, so every virtual System voice member is still "
                    "counted as dropped"
                )
    if "__builtin_system_speech__" in backup_src:
        return (
            "C12 RED: ConfigBackup spells the System sentinel id literal instead of "
            "going through SystemVoiceEntries — the id must have one home"
        )
    return None

def check_group_member_resolution(builtins_src: str) -> str | None:
    """Criterion 13 [dv20-a1 A-02]: the `groups` config summary resolves the
    virtual System voice entries instead of emitting a "missing" stub."""
    text = strip_comments(builtins_src)
    if "SystemVoiceEntries.resolve(" not in text:
        return (
            "C13 RED: the `groups` config summary never calls "
            "SystemVoiceEntries.resolve — a virtual System voice member is rendered "
            "as a missing model row even though chat resolves it fine"
        )
    # [wide-1024 12-misc-b] Calling it is not resolving the member: the VALUE has
    # to be consumed. `entriesById[id].also { SystemVoiceEntries.resolve(id) }`
    # keeps the anchor token, throws the resolved entry away (the receiver is
    # what flows on) and still renders the member as missing. Two legitimate
    # shapes exist and both are accepted — an Elvis fallback (`a[id] ?: resolve(id)`)
    # and a chain on the result (`ConfigValue.Str(resolve(id)?.id ?: id)`).
    discarded = []
    for call in re.finditer(r"SystemVoiceEntries\.resolve\s*\(", text):
        head = text[: call.start()].rstrip()
        close = extract_paren_block(text, call.end() - 1)
        if close is None:
            return (
                "C13 EXTRACT_FAIL: SystemVoiceEntries.resolve( never balances "
                "(fail-closed)"
            )
        tail = text[call.end() - 1 + len(close):].lstrip()
        consumed_in = head.endswith(("=", "?:", "(", ",", "return", "->"))
        consumed_out = tail[:1] in {"?", "!", ".", ",", ")"} or tail[:2] == "!!"
        if not (consumed_in or consumed_out):
            line = text[: call.start()].count("\n") + 1
            discarded.append(line)
    if discarded:
        return (
            "C13 RED: SystemVoiceEntries.resolve is called as a discarded statement "
            "(ConfigBuiltins.kt:%s) — the resolved virtual entry never reaches the "
            "member row, so a System voice member is still rendered as a missing model"
            % ", ".join(str(n) for n in discarded)
        )
    if "__builtin_system_speech__" in builtins_src:
        return (
            "C13 RED: ConfigBuiltins spells the System sentinel id literal instead of "
            "going through SystemVoiceEntries — the id must have one home"
        )
    return None


def check(envvars_path: str, builtins_path: str, groups_path: str,
          backup_path: str | None = None) -> list[str]:
    with open(envvars_path, encoding="utf-8") as f:
        ev = f.read()
    with open(builtins_path, encoding="utf-8") as f:
        cb = f.read()
    with open(groups_path, encoding="utf-8") as f:
        gr = f.read()
    failures = []
    for name, fn, src in (
        ("C1", check_fields_registration, ev),
        ("C2", check_group_path, ev),
        ("C3", check_summary_group, cb),
        ("C4", check_add_calls_context_limit_helper, gr),
        ("C5", check_groups_summary_keys, cb),
        ("C6", check_groups_summary_legacy_keys, cb),
        ("C7", check_providers_summary_ua_key, cb),
    ):
        r = fn(src)
        if r is not None:
            failures.append(r)

    # Criteria 8-11 (da1028-B). Judged only when the caller hands us the backup
    # source: the three-file mini fixtures predate this arm and keep their
    # one-criterion-per-fixture shape.
    if backup_path is not None:
        with open(backup_path, encoding="utf-8") as f:
            bk = f.read()
        for fn in (
            lambda: check_backup_scope_envvars(bk),
            lambda: check_overlay_registration(cb),
            lambda: check_overlay_single_source(cb),
            lambda: check_reverse_anchors(bk, cb),
            lambda: check_sentinel_remap(bk),
            lambda: check_group_member_resolution(cb),
        ):
            r = fn()
            if r is not None:
                failures.append(r)
    return failures


def locate(root: str) -> tuple[str, str, str, str]:
    """Locate the four production files under repo_root. Fail-closed."""
    ev = cb = gr = bk = None
    for dirpath, _dirnames, filenames in os.walk(root):
        if "build" in dirpath.split(os.sep) or "test" in dirpath.split(os.sep):
            continue
        for fn in filenames:
            if fn == ENVVARS_REL and ev is None:
                ev = os.path.join(dirpath, fn)
            elif fn == BUILTINS_REL and cb is None and "config" in dirpath.split(os.sep):
                cb = os.path.join(dirpath, fn)
            elif fn == GROUPS_REL and gr is None and "config" in dirpath.split(os.sep):
                gr = os.path.join(dirpath, fn)
            elif fn == BACKUP_REL and bk is None and "backup" in dirpath.split(os.sep):
                bk = os.path.join(dirpath, fn)
    if ev is None:
        raise SystemExit("LOCATE_FAIL: EnvVarsCollection.kt not found under " + root)
    if cb is None:
        raise SystemExit("LOCATE_FAIL: ConfigBuiltins.kt not found under " + root)
    if gr is None:
        raise SystemExit("LOCATE_FAIL: GroupsCollection.kt not found under " + root)
    if bk is None:
        raise SystemExit("LOCATE_FAIL: ConfigBackup.kt not found under " + root)
    return ev, cb, gr, bk


# --------------------------------------------------------------------------
# Selftest fixtures. Each fixture is a minimal synthetic source pair crafted to
# have exactly one criterion broken (the "precisely broken by it" requirement).
# --------------------------------------------------------------------------

FIXTURE_ENVVARS_OK = '''package fake

class EnvVarsCollection(private val repo: Repo) : ConfigCollection {
    override fun fields(forId: String): List<ConfigField> {
        val entry = repo.entries.value.firstOrNull { it.key == forId } ?: return emptyList()
        return listOf(
            createdAtField(forId),
            noteField(forId),
            groupField(forId),
            HiddenField(
                path = "envvars.$forId.value",
                displayName = "Value",
            ),
        )
    }

    private fun createdAtField(key: String): ConfigField = ReadOnlyField(
        path = "envvars.$key.createdAt",
        displayName = "Created at",
        valueSchema = ConfigSchema.Str(),
        reader = { ConfigValue.Null },
    )

    private fun noteField(key: String): ConfigField = ClosureField(
        path = "envvars.$key.note",
        displayName = "Note",
        valueSchema = ConfigSchema.Str(maxLength = 500),
        reader = { ConfigValue.Null },
        writer = { v ->
            val e = repo.entries.value.firstOrNull { it.key == key }
                ?: throw ConfigError.UnknownPath("envvars.$key")
            val currentValue = repo.getValue(e.key) ?: ""
            repo.update(e.id, e.key, currentValue, v, e.group)
        },
    )

    private fun groupField(key: String): ConfigField = ClosureField(
        path = "envvars.$key.group",
        displayName = "Group",
        valueSchema = ConfigSchema.Str(maxLength = 200),
        reader = {
            val e = repo.entries.value.firstOrNull { it.key == key }
            if (e == null) ConfigValue.Null else ConfigValue.Str(e.group)
        },
        writer = { v ->
            val e = repo.entries.value.firstOrNull { it.key == key }
                ?: throw ConfigError.UnknownPath("envvars.$key")
            val currentValue = repo.getValue(e.key) ?: ""
            repo.update(e.id, e.key, currentValue, e.note, v)
        },
    )
}
'''

FIXTURE_GROUPS_OK = """package fake

class GroupsCollection(private val repo: Repo) : ConfigCollection {
    override fun add(payload: ConfigValue): String {
        val obj = (payload as? ConfigValue.Obj)?.value
            ?: throw ConfigError.InvalidValue("Expected JSON object")
        val contextLimit = (obj["context_limit_tokens"] as? ConfigValue.Int)?.value
            ?.takeIf { it > 0 }
        val g = ModelGroup(name = "g", contextLimitTokens = contextLimit)
        applyContextLimitWrite(g, contextLimit ?: 0)
        repo.addGroup(g)
        return g.id
    }

    private fun contextLimitField(id: String): ConfigField = ClosureField(
        path = "groups.$id.contextLimitTokens",
        displayName = "Context limit (tokens)",
        reader = { ConfigValue.Null },
        writer = { v ->
            val i = (v as? ConfigValue.Int)?.value ?: throw ConfigError.TypeMismatch("int")
            mutate(id) { applyContextLimitWrite(it, i) }
        },
    )
}
"""

FIXTURE_BUILTINS_OK = '''package fake

object ConfigBuiltins {
    fun register(r: R) {
        r.register(
            ReadOnlyField(
                path = "envvars",
                displayName = "Environment variables (summary)",
                valueSchema = ConfigSchema.Json,
                reader = {
                    ConfigValue.Arr(
                        envVarRepo.entries.value.map { e ->
                            ConfigValue.Obj(
                                linkedMapOf(
                                    "key" to ConfigValue.Str(e.key),
                                    "group" to ConfigValue.Str(e.group),
                                    "note" to ConfigValue.Str(e.note),
                                    "created_at" to ConfigValue.Str(isoFormatter.format(java.time.Instant.ofEpochMilli(e.createdAt))),
                                )
                            )
                        }
                    )
                },
            )
        )

        r.register(
            ReadOnlyField(
                path = "providers",
                displayName = "LLM provider instances (summary)",
                valueSchema = ConfigSchema.Json,
                reader = {
                    ConfigValue.Arr(
                        providerRepo.entries.value.map { inst ->
                            ConfigValue.Obj(
                                linkedMapOf(
                                    "id" to ConfigValue.Str(inst.id),
                                    "label" to ConfigValue.Str(inst.label),
                                    "customUserAgent" to (inst.customUserAgent?.let { ConfigValue.Str(it) } ?: ConfigValue.Null),
                                )
                            )
                        }
                    )
                },
            )
        )

        r.register(
            ReadOnlyField(
                path = "groups",
                displayName = "Model groups (summary)",
                valueSchema = ConfigSchema.Json,
                reader = {
                    ConfigValue.Arr(
                        groupRepo.groups.value.map { g ->
                            ConfigValue.Obj(
                                linkedMapOf(
                                    "id" to ConfigValue.Str(g.id),
                                    "name" to ConfigValue.Str(g.name),
                                    "strategy" to ConfigValue.Str(g.strategy.name),
                                    "fallback_strategy" to ConfigValue.Str(g.fallbackStrategy.name),
                                    "default_thinking_level" to (g.defaultThinkingLevel?.let { ConfigValue.Str(it) } ?: ConfigValue.Null),
                                    "context_limit_tokens" to (g.contextLimitTokens?.let { ConfigValue.Int(it) } ?: ConfigValue.Null),
                                    "entries" to ConfigValue.Arr(g.memberEntryIds.map { memberId ->
                                        ConfigValue.Str(SystemVoiceEntries.resolve(memberId)?.id ?: memberId)
                                    }),
                                )
                            )
                        }
                    )
                },
            )
        )
    }
}
'''


FIXTURE_BACKUP_OK = '''package fake

private fun remapGroupMembers(
    entryIdMap: Map<String, String>,
    srcMembers: List<String>,
    out: MutableList<String>,
): Int {
    var droppedMembers = 0
    for (old in srcMembers) {
        val mapped = entryIdMap[old] ?: old.takeIf { SystemVoiceEntries.isSystemEntryId(it) }
        if (mapped != null) out.add(mapped) else droppedMembers++
    }
    return droppedMembers
}

object ConfigBackup {
    internal val BACKED_UP_SCOPES = setOf(
        "appearance",
        "chat",
        "background",
        "defaults",
        "soul",
        "memory",
        "logs",
        "runtime",
        "envvars",
    )
}
'''

OVERLAY_FN_OK = '''    private fun registerBackground(r: ConfigRegistry, context: Context) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        r.register(
            PrefsBoolField(
                path = "background.overlay",
                displayName = "Background task overlay",
                prefs = prefs,
                key = com.rikkaminis.app.data.repository.BackgroundSettingsRepository
                    .KEY_BG_OVERLAY_ENABLED,
                defaultValue = com.rikkaminis.app.data.repository.BackgroundSettingsRepository
                    .DEFAULT_BG_OVERLAY_ENABLED,
            )
        )
    }

'''

PRIVACY_FN_OK = '''    private fun registerEnvVars(r: ConfigRegistry, context: Context) {
        r.register(
            ClosureField(
                path = "envvars.privacyMode",
                displayName = "Privacy Mode",
                valueSchema = ConfigSchema.Bool,
                reader = { ConfigValue.Bool(true) },
                writer = { v -> Unit },
            )
        )
    }
'''

def inject_members(src: str, members: str) -> str:
    """Append members just before the object's closing brace (fixture sugar:
    the injected text stays inside `object ConfigBuiltins { … }`)."""
    i = src.rstrip().rfind("}")
    return src[:i] + "\n" + members + src[i:]

def full_fixture(builtins: str, backup: str) -> str:
    """A four-source mini tree: criteria 1-7 run on the config fixtures as
    before, 8-11 additionally judge ConfigBuiltins + ConfigBackup."""
    d = tempfile.mkdtemp(prefix="envvar_group_full_")
    os.makedirs(os.path.join(d, "fake", "config", "collections"))
    os.makedirs(os.path.join(d, "fake", "backup"))
    with open(os.path.join(d, "fake", "config", "collections", "EnvVarsCollection.kt"), "w", encoding="utf-8") as f:
        f.write(FIXTURE_ENVVARS_OK)
    with open(os.path.join(d, "fake", "config", "collections", "GroupsCollection.kt"), "w", encoding="utf-8") as f:
        f.write(FIXTURE_GROUPS_OK)
    with open(os.path.join(d, "fake", "config", "ConfigBuiltins.kt"), "w", encoding="utf-8") as f:
        f.write(builtins)
    with open(os.path.join(d, "fake", "backup", "ConfigBackup.kt"), "w", encoding="utf-8") as f:
        f.write(backup)
    return d

def full_check(d: str) -> list[str]:
    return check(
        os.path.join(d, "fake", "config", "collections", "EnvVarsCollection.kt"),
        os.path.join(d, "fake", "config", "ConfigBuiltins.kt"),
        os.path.join(d, "fake", "config", "collections", "GroupsCollection.kt"),
        os.path.join(d, "fake", "backup", "ConfigBackup.kt"),
    )

def fixture_dir() -> str:
    d = tempfile.mkdtemp(prefix="envvar_group_selftest_")
    os.makedirs(os.path.join(d, "fake", "config", "collections"))
    with open(os.path.join(d, "fake", "config", "collections", "EnvVarsCollection.kt"), "w", encoding="utf-8") as f:
        f.write(FIXTURE_ENVVARS_OK)
    with open(os.path.join(d, "fake", "config", "ConfigBuiltins.kt"), "w", encoding="utf-8") as f:
        f.write(FIXTURE_BUILTINS_OK)
    with open(os.path.join(d, "fake", "config", "collections", "GroupsCollection.kt"), "w", encoding="utf-8") as f:
        f.write(FIXTURE_GROUPS_OK)
    return d


def run_fixtures() -> bool:
    ok = True
    d2 = d3 = d4 = d5 = d6 = d7 = d8 = d9 = None

    def expect(label: str, failures: list[str], want: str | None) -> bool:
        if want is None:
            if failures:
                print(f"[SELFTEST] {label}: expected GREEN, got {failures}")
                return False
            print(f"[SELFTEST] {label}: GREEN as expected")
            return True
        if len(failures) != 1 or not failures[0].startswith(want):
            print(f"[SELFTEST] {label}: expected exactly one failure starting with {want!r}, got {failures}")
            return False
        print(f"[SELFTEST] {label}: red as expected ({failures[0]})")
        return True

    def groups_path(d: str) -> str:
        return os.path.join(d, "fake", "config", "collections", "GroupsCollection.kt")

    # FIX-1: complete fixture -> all green
    d = fixture_dir()
    try:
        ok &= expect(
            "FIX-1 complete",
            check(
                os.path.join(d, "fake", "config", "collections", "EnvVarsCollection.kt"),
                os.path.join(d, "fake", "config", "ConfigBuiltins.kt"),
                groups_path(d),
            ),
            None,
        )

        # FIX-2: fields list drops groupField -> exactly C1 red
        d2 = fixture_dir()
        p = os.path.join(d2, "fake", "config", "collections", "EnvVarsCollection.kt")
        src = open(p, encoding="utf-8").read().replace("            groupField(forId),\n", "")
        open(p, "w", encoding="utf-8").write(src)
        ok &= expect(
            "FIX-2 fields-missing",
            check(
                p,
                os.path.join(d2, "fake", "config", "ConfigBuiltins.kt"),
                groups_path(d2),
            ),
            "C1",
        )

        # FIX-3: groupField present in the list but its path is wrong -> exactly C2 red
        d3 = fixture_dir()
        p3 = os.path.join(d3, "fake", "config", "collections", "EnvVarsCollection.kt")
        src3 = open(p3, encoding="utf-8").read().replace('path = "envvars.$key.group",', 'path = "envvars.$key.tag",')
        open(p3, "w", encoding="utf-8").write(src3)
        ok &= expect(
            "FIX-3 path-missing",
            check(
                p3,
                os.path.join(d3, "fake", "config", "ConfigBuiltins.kt"),
                groups_path(d3),
            ),
            "C2",
        )

        # FIX-4: summary block drops the "group" key -> exactly C3 red
        d4 = fixture_dir()
        p4 = os.path.join(d4, "fake", "config", "ConfigBuiltins.kt")
        src4 = open(p4, encoding="utf-8").read().replace('                                    "group" to ConfigValue.Str(e.group),\n', "")
        open(p4, "w", encoding="utf-8").write(src4)
        ok &= expect(
            "FIX-4 summary-missing",
            check(
                os.path.join(d4, "fake", "config", "collections", "EnvVarsCollection.kt"),
                p4,
                groups_path(d4),
            ),
            "C3",
        )

        # FIX-5 (fail-closed control): truncated EnvVarsCollection (no fields
        # block at all) -> C1 must be EXTRACT_FAIL red, never silently green.
        d5 = fixture_dir()
        p5 = os.path.join(d5, "fake", "config", "collections", "EnvVarsCollection.kt")
        open(p5, "w", encoding="utf-8").write("package fake\n\nclass EnvVarsCollection {\n")
        fails5 = check(p5, os.path.join(d5, "fake", "config", "ConfigBuiltins.kt"), groups_path(d5))
        if not any(f.startswith("C1 EXTRACT_FAIL") for f in fails5):
            print(f"[SELFTEST] FIX-5 truncated: expected C1 EXTRACT_FAIL, got {fails5}")
            ok = False
        else:
            print("[SELFTEST] FIX-5 truncated: fail-closed as expected")

        # FIX-6 [da1026-H]: add drops the context-limit helper call -> exactly C4 red
        d6 = fixture_dir()
        p6 = groups_path(d6)
        src6 = open(p6, encoding="utf-8").read().replace(
            "        applyContextLimitWrite(g, contextLimit ?: 0)\n", ""
        )
        open(p6, "w", encoding="utf-8").write(src6)
        ok &= expect(
            "FIX-6 add-missing-helper",
            check(
                os.path.join(d6, "fake", "config", "collections", "EnvVarsCollection.kt"),
                os.path.join(d6, "fake", "config", "ConfigBuiltins.kt"),
                p6,
            ),
            "C4",
        )

        # FIX-7 [da1026-H]: groups summary drops context_limit_tokens -> exactly C5 red
        d7 = fixture_dir()
        p7 = os.path.join(d7, "fake", "config", "ConfigBuiltins.kt")
        src7 = open(p7, encoding="utf-8").read().replace(
            '                                    "context_limit_tokens" to (g.contextLimitTokens?.let { ConfigValue.Int(it) } ?: ConfigValue.Null),\n',
            "",
        )
        open(p7, "w", encoding="utf-8").write(src7)
        ok &= expect(
            "FIX-7 summary-missing-context-limit",
            check(
                os.path.join(d7, "fake", "config", "collections", "EnvVarsCollection.kt"),
                p7,
                groups_path(d7),
            ),
            "C5",
        )

        # FIX-8 [da1026-H]: groups summary drops default_thinking_level -> exactly C5 red
        d8 = fixture_dir()
        p8 = os.path.join(d8, "fake", "config", "ConfigBuiltins.kt")
        src8 = open(p8, encoding="utf-8").read().replace(
            '                                    "default_thinking_level" to (g.defaultThinkingLevel?.let { ConfigValue.Str(it) } ?: ConfigValue.Null),\n',
            "",
        )
        open(p8, "w", encoding="utf-8").write(src8)
        ok &= expect(
            "FIX-8 summary-missing-thinking-level",
            check(
                os.path.join(d8, "fake", "config", "collections", "EnvVarsCollection.kt"),
                p8,
                groups_path(d8),
            ),
            "C5",
        )

        # FIX-9 [da1026-H] (fail-closed control): ConfigBuiltins with no groups
        # registration -> C5 must be EXTRACT_FAIL red, never silently green.
        d9 = fixture_dir()
        p9 = os.path.join(d9, "fake", "config", "ConfigBuiltins.kt")
        open(p9, "w", encoding="utf-8").write("package fake\n\nobject ConfigBuiltins {\n")
        fails9 = check(
            os.path.join(d9, "fake", "config", "collections", "EnvVarsCollection.kt"), p9, groups_path(d9)
        )
        if not any(f.startswith("C5 EXTRACT_FAIL") for f in fails9):
            print(f"[SELFTEST] FIX-9 no-groups-block: expected C5 EXTRACT_FAIL, got {fails9}")
            ok = False
        else:
            print("[SELFTEST] FIX-9 no-groups-block: fail-closed as expected")

        # FIX-10 [da1026-H] (wrong-block guard): the providers summary loses its
        # customUserAgent key -> exactly C7 red (the groups block stays intact).
        d10 = fixture_dir()
        p10 = os.path.join(d10, "fake", "config", "ConfigBuiltins.kt")
        src10 = open(p10, encoding="utf-8").read().replace(
            '                                    "customUserAgent" to (inst.customUserAgent?.let { ConfigValue.Str(it) } ?: ConfigValue.Null),\n',
            "",
        )
        open(p10, "w", encoding="utf-8").write(src10)
        ok &= expect(
            "FIX-10 providers-ua-missing",
            check(
                os.path.join(d10, "fake", "config", "collections", "EnvVarsCollection.kt"),
                p10,
                groups_path(d10),
            ),
            "C7",
        )
    finally:
        shutil.rmtree(d, ignore_errors=True)
        for dd in (d2, d3, d4, d5, d6, d7, d8, d9, d10):
            shutil.rmtree(dd, ignore_errors=True)

    # ── [da1028-B / ISSUE-03] criteria 8-11 ────────────────────────────────
    # Four-source trees: 1-7 judge the config fixtures as always, 8-11 judge
    # the backup scope list + the registerBackground block.

    def expect_prefixes(label: str, failures: list[str], wants: tuple) -> bool:
        """Precision control for arms where a shared precondition legitimately
        trips more than one criterion: the failure list must match `wants`
        exactly (same length, same order, same "Cx KIND" heads)."""
        def head(s: str) -> str:
            return (s.split(":", 1)[0] if ":" in s else s).strip()

        got = tuple(head(f) for f in failures)
        want = tuple(head(w) for w in wants)
        if got != want:
            print(f"[SELFTEST] {label}: expected {want}, got {failures}")
            return False
        print(f"[SELFTEST] {label}: red as expected ({'; '.join(failures)})")
        return True

    BUILTINS_OK4 = inject_members(FIXTURE_BUILTINS_OK, OVERLAY_FN_OK + PRIVACY_FN_OK)
    fds = []

    def full_arm(label: str, builtins: str, backup: str, wants) -> bool:
        d4 = full_fixture(builtins, backup)
        fds.append(d4)
        fails = full_check(d4)
        if not wants:
            if fails:
                print(f"[SELFTEST] {label}: expected GREEN, got {fails}")
                return False
            print(f"[SELFTEST] {label}: GREEN as expected")
            return True
        return expect_prefixes(label, fails, wants)

    ok &= full_arm("FIX-11 complete (all four sources)", BUILTINS_OK4, FIXTURE_BACKUP_OK, ())

    # FIX-12: whitelist drops "envvars" -> exactly C8
    ok &= full_arm(
        "FIX-12 backup-whitelist-no-envvars",
        BUILTINS_OK4,
        FIXTURE_BACKUP_OK.replace('        "envvars",\n', ""),
        ("C8 RED:",),
    )

    # FIX-13: overlay registered under the wrong path -> exactly C9 (the
    # constants are intact, so C10 stays green — arms must not blur together)
    ok &= full_arm(
        "FIX-13 overlay-wrong-path",
        BUILTINS_OK4.replace('path = "background.overlay"', 'path = "background.overlayEnabled"'),
        FIXTURE_BACKUP_OK,
        ("C9 RED:",),
    )

    # FIX-14: key re-spelled as the raw literal instead of the repository
    # constant -> exactly C10
    ok &= full_arm(
        "FIX-14 overlay-key-literal",
        BUILTINS_OK4.replace(
            "                key = com.rikkaminis.app.data.repository.BackgroundSettingsRepository\n"
            "                    .KEY_BG_OVERLAY_ENABLED,",
            '                key = "backgroundOverlayEnabled",',
        ),
        FIXTURE_BACKUP_OK,
        ("C10 RED:",),
    )

    # FIX-15 (fail-closed control): no registerBackground member at all ->
    # C9 and C10 both refuse to judge, never a silent green (the privacy
    # registration stays so this arm isolates the overlay precondition)
    ok &= full_arm(
        "FIX-15 overlay-registration-absent",
        inject_members(FIXTURE_BUILTINS_OK, PRIVACY_FN_OK),
        FIXTURE_BACKUP_OK,
        ("C9 EXTRACT_FAIL:", "C10 EXTRACT_FAIL:"),
    )

    # FIX-16 (fail-closed control): the scope set is gone -> C8 + C11 refuse
    # (the minimal source has no member remap either, so C12 refuses too)
    ok &= full_arm(
        "FIX-16 backup-anchor-absent",
        BUILTINS_OK4,
        "package fake\n\nobject ConfigBackup {\n}\n",
        ("C8 EXTRACT_FAIL:", "C11 EXTRACT_FAIL:", "C12 EXTRACT_FAIL:"),
    )

    # FIX-17 (reverse anchor): a legacy scope is swapped out -> exactly C11
    ok &= full_arm(
        "FIX-17 legacy-scope-swapped-out",
        BUILTINS_OK4,
        FIXTURE_BACKUP_OK.replace('        "runtime",\n', ""),
        ("C11 RED:",),
    )

    # FIX-18 (reverse anchor): the privacy toggle stops being registered ->
    # exactly C11
    ok &= full_arm(
        "FIX-18 privacyMode-unregistered",
        inject_members(FIXTURE_BUILTINS_OK, OVERLAY_FN_OK),
        FIXTURE_BACKUP_OK,
        ("C11 RED:",),
    )

    # ── [dv20-a1 A-02] sentinel-preservation arms ─────────────────────────
    SENTINEL_BACKUP_OK = '''
package fake.config

import com.rikkaminis.app.data.model.SystemVoiceEntries

object ConfigBackup {
    private fun remapMembers(entryIdMap: Map<String, String>, srcMembers: List<String>): Int {
        var droppedMembers = 0
        for (old in srcMembers) {
            val mapped = entryIdMap[old]
                ?: old.takeIf { SystemVoiceEntries.isSystemEntryId(it) }
            if (mapped != null) members.add(mapped) else droppedMembers++
        }
        return droppedMembers
    }
}
'''

    SENTINEL_BUILTINS_OK = '''
package fake.config

import com.rikkaminis.app.data.model.SystemVoiceEntries

object ConfigBuiltins {
    fun groupsSummary(entriesById: Map<String, Any>): List<Any> {
        val entryIds = listOf("a")
        return entryIds.map { entryId ->
            val e = entriesById[entryId] ?: SystemVoiceEntries.resolve(entryId)
            if (e == null) listOf("entry_id", entryId, "missing") else listOf("entry_id", entryId)
        }
    }
}
'''

    def sentinel_arm(label: str, source: str, fn, want) -> bool:
        r = fn(source)
        got = [] if r is None else [r]
        if want is None:
            if got:
                print(f"[SELFTEST] {label}: expected GREEN, got {got}")
                return False
            print(f"[SELFTEST] {label}: GREEN as expected")
            return True
        if len(got) != 1 or not got[0].startswith(want):
            print(f"[SELFTEST] {label}: expected exactly one {want!r}, got {got}")
            return False
        print(f"[SELFTEST] {label}: red as expected ({got[0]})")
        return True

    ok &= sentinel_arm("FIX-19 sentinel-preserving remap", SENTINEL_BACKUP_OK,
                       check_sentinel_remap, None)
    ok &= sentinel_arm(
        "FIX-20 remap drops the sentinels",
        SENTINEL_BACKUP_OK.replace(
            "                ?: old.takeIf { SystemVoiceEntries.isSystemEntryId(it) }\n", ""),
        check_sentinel_remap, "C12 RED:")
    ok &= sentinel_arm("FIX-21 remap anchor absent",
                       "package fake\n\nobject ConfigBackup {\n}\n",
                       check_sentinel_remap, "C12 EXTRACT_FAIL:")
    ok &= sentinel_arm("FIX-22 summary resolves the members", SENTINEL_BUILTINS_OK,
                       check_group_member_resolution, None)
    ok &= sentinel_arm(
        "FIX-23 summary emits the missing stub",
        SENTINEL_BUILTINS_OK.replace(" ?: SystemVoiceEntries.resolve(entryId)", ""),
        check_group_member_resolution, "C13 RED:")
    ok &= sentinel_arm(
        "FIX-24 sentinel literal re-spelled",
        SENTINEL_BUILTINS_OK.replace(
            "        val entryIds = listOf(\"a\")",
            "        val pinned = \"__builtin_system_speech__\"\n"
            "        val entryIds = listOf(\"a\")"),
        check_group_member_resolution, "C13 RED:")

    # ── [wide-1024 12-misc-b] witness arms ────────────────────────────────
    # Each one keeps every token the old criterion matched and drops the EFFECT.
    # FIX-25: the reader answers a constant (the path + writer stay intact).
    d25 = fixture_dir()
    p25 = os.path.join(d25, "fake", "config", "collections", "EnvVarsCollection.kt")
    src25 = open(p25, encoding="utf-8").read().replace(
        "        reader = {\n"
        "            val e = repo.entries.value.firstOrNull { it.key == key }\n"
        "            if (e == null) ConfigValue.Null else ConfigValue.Str(e.group)\n"
        "        },\n",
        "        reader = { ConfigValue.Null },\n",
    )
    open(p25, "w", encoding="utf-8").write(src25)
    ok &= expect(
        "FIX-25 groupField reader answers a constant",
        check(
            p25,
            os.path.join(d25, "fake", "config", "ConfigBuiltins.kt"),
            groups_path(d25),
        ),
        "C2 RED:",
    )

    # FIX-26: the helper is called, with a constant instead of the parsed value.
    d26 = fixture_dir()
    p26 = groups_path(d26)
    src26 = open(p26, encoding="utf-8").read().replace(
        "applyContextLimitWrite(g, contextLimit ?: 0)", "applyContextLimitWrite(g, 0)")
    open(p26, "w", encoding="utf-8").write(src26)
    ok &= expect(
        "FIX-26 context-limit helper fed a constant",
        check(
            os.path.join(d26, "fake", "config", "collections", "EnvVarsCollection.kt"),
            os.path.join(d26, "fake", "config", "ConfigBuiltins.kt"),
            p26,
        ),
        "C4 RED:",
    )

    # FIX-27: isSystemEntryId present but short-circuited to false.
    ok &= sentinel_arm(
        "FIX-27 sentinel predicate dead-guarded",
        SENTINEL_BACKUP_OK.replace(
            "old.takeIf { SystemVoiceEntries.isSystemEntryId(it) }",
            "old.takeIf { false && SystemVoiceEntries.isSystemEntryId(it) }"),
        check_sentinel_remap, "C12 RED:")
    # FIX-27b: the tautology spelling of the same dead predicate.
    ok &= sentinel_arm(
        "FIX-27b sentinel predicate exhausted middle",
        SENTINEL_BACKUP_OK.replace(
            "old.takeIf { SystemVoiceEntries.isSystemEntryId(it) }",
            "old.takeIf { SystemVoiceEntries.isSystemEntryId(it) || "
            "!SystemVoiceEntries.isSystemEntryId(it) }"),
        check_sentinel_remap, "C12 RED:")

    # FIX-28: resolve() called inside .also — the value is thrown away.
    ok &= sentinel_arm(
        "FIX-28 resolve result discarded",
        SENTINEL_BUILTINS_OK.replace(
            " ?: SystemVoiceEntries.resolve(entryId)",
            ".also { SystemVoiceEntries.resolve(entryId) }"),
        check_group_member_resolution, "C13 RED:")

    for dd in fds:
        shutil.rmtree(dd, ignore_errors=True)

    return ok


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        if run_fixtures():
            print("ENVVAR_GROUP_SELFTEST=PASS")
            return 0
        print("ENVVAR_GROUP_SELFTEST=FAIL")
        return 1

    roots = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not roots:
        print("usage: envvar_config_group_check.py <repo_root> | --selftest")
        return 2
    ev, cb, gr, bk = locate(roots[0])
    failures = check(ev, cb, gr, bk)
    if failures:
        for f in failures:
            print(f)
        print(f"envvar_config_group_check: {len(failures)} violation(s)")
        return 1
    print("envvar_config_group_check: OK (13 criteria green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

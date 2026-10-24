#!/usr/bin/env python3
"""deep-audit-1023 ISSUE-03 (P1) — compact-pin secondary-reference gate.

`AgentRuntimeLimitsPrefs.compactModelEntryId` is a *secondary reference* to a
model entry id, held in SharedPreferences rather than in ProviderConfig. Every
per-config cascade that heals the in-config references (modelGroups
.memberEntryIds, agentLoopModelEntryIds, prefs KEY_LAST_USED_ENTRY) is blind to
it, because `saveConfig` only translates ids on its way through
ProviderConfigMapping:

    idMap[entry.uuid] = compositeEntryKey(instanceId, modelId)   (write side)
    uuid = row.id                                                (load side)

A pin that is not rewritten dangles, and `CompactOrchestration` then resolves
it to nothing and silently follows the session chain (INFO-level only) — the
user's explicit "compact with THIS model" choice evaporates with no error.

Pre-fix, ProviderRepository.kt had ZERO references to AgentRuntimeLimitsPrefs:
the in-config cascades existed (lastUsedEntryId was migrated on rename) and the
prefs-level one simply was not part of the family. This gate pins the fixed
shape, in CODE (comments are stripped first):

  Arm 1  `ProviderRepository.updateEntry` — inside the rename branch
         (`if (renamed)`), the pin is migrated through the pure decision
         helper and written back.
  Arm 2  `ProviderRepository.removeEntry` — the deleted id cascades into the
         pin (reset to "" = explicit follow-session), after the agent-loop
         cascade that it mirrors.
  Arm 3  `ProviderRepository.removeInstance` — its own entry cascade (it does
         NOT route through removeEntry) also cleans the pin for the whole
         removed set.
  Arm 4  the pure decision helpers exist as top-level `internal fun`s in the
         repository file (JVM-testable, no Android dependency in the slice).

deep-audit-1024 ISSUE-03 (P1) widened the same family from two removal paths
to four, and from the pin alone to the prefs-held references next to it:

  Arm 5  `ProviderRepository.replaceEntries` — inside the
         non-`suspiciousShrink` `else` branch (a transient API hiccup must
         never nuke user config), the pruned set cascades into the pin AND
         into the per-device references.
  Arm 6  `ProviderRepository.ensureVoiceTemplateModels` — dissolving a stale
         seed entry runs the SAME cascade (group members / agent-loop pins /
         pin / per-device refs); previously it cleaned nothing but the entry
         row itself. [da1031-A] the cascade now compares ENTRY ids
         (`staleEntryIds` — the uuid/composite form every secondary ref is
         stored in); the bare `baseModel.id` spelling made all four routes a
         silent no-op.
  Arm 7  the per-device applier (`clearStaleSecondaryRefs`) clears all three
         of `lastUsedEntryId` / `voiceInputOverrideEntryId` /
         `voiceOutputOverrideEntryId` through the shared `decideStaleRefCleanup`
         predicate — the gate checks the applier BODY, so calling an emptied
         helper does not satisfy arm 6/5.
  Arm 8  `ui/settings/ModelGroupsScreen.kt` — the relocated compact-pin picker
         re-reads `AgentRuntimeLimitsPrefs.KEY_COMPACT_MODEL_ENTRY_ID` from a
         strongly-held `OnSharedPreferenceChangeListener` (the `remember{}`
         snapshot alone left the row showing an id the repository no longer
         resolves after an external write / backup restore / cascade reset).

deep-audit-1031 ISSUE-01 (P1) added arm 9:

  Arm 9  `updateEntry`'s rename branch migrates the two prefs-held voice
         overrides (`voiceInputOverrideEntryId` / `voiceOutputOverrideEntryId`)
         — the same class of per-device secondary reference as
         `lastUsedEntryId`. Judged on the WRITE POINTS (the `== oldKey`
         comparisons and the `= newUuid` rewrites) inside `if (renamed)`, not
         on the function name: a second rename path that forks the migration
         must carry its own write points, and a rename of this function keeps
         the criterion alive as long as the write points stay inside the
         rename branch.

Usage: python3 scripts/scan/compact_entry_pin_migration_check.py [ROOT]
       python3 scripts/scan/compact_entry_pin_migration_check.py --root ROOT
       python3 scripts/scan/compact_entry_pin_migration_check.py --selftest
Exit:  0 = green, 1 = violation(s) (file:line printed) / selftest failure.
"""
import os
import re
import sys

REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/ProviderRepository.kt"

PIN_READ = "AgentRuntimeLimitsPrefs.compactModelEntryId()"
PIN_SAVE = "AgentRuntimeLimitsPrefs.save(context, compactModelEntryId ="
PIN_RESET = 'compactModelEntryId = ""'

UPDATE_ARM = (
    "decideCompactPinMigration(",
    PIN_READ,
    "compactModelEntryId = migrated",
)
REMOVE_ARM = (
    "decideCompactPinCleanup(",
    PIN_READ,
    PIN_RESET,
)
REMOVE_INSTANCE_ARM = (
    "decideCompactPinCleanup(",
    PIN_RESET,
)
HELPER_ARM = (
    "internal fun decideCompactPinMigration(",
    "internal fun decideCompactPinCleanup(",
)

# ── [da1024-C] arms 5-8 ──────────────────────────────────────────────────
UI_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ModelGroupsScreen.kt"

# Arm 5: replaceEntries' prune branch (the else of the suspicious-shrink guard).
REPLACE_SHRINK_GUARD = "if (suspiciousShrink)"
REPLACE_ARM = (
    "decideCompactPinCleanup(",
    PIN_READ,
    PIN_RESET,
    "clearStaleSecondaryRefs(prunedEntryIds)",
)

# Arm 6: ensureVoiceTemplateModels' stale-seed branch. [da1031-A] the cascade
# must compare ENTRY ids (staleEntryIds — the uuid/composite form every
# secondary reference is stored in); bare baseModel.id values made all four
# routes a silent no-op, and the old spelling was pinned by the test below.
VOICE_GUARD = "if (staleSeedIds.isNotEmpty())"
VOICE_ARM = (
    "val staleEntryIds",
    "config.agentLoopModelEntryIds.removeAll { it in staleEntryIds }",
    "group.memberEntryIds.removeAll { it in staleEntryIds }",
    "decideCompactPinCleanup(",
    PIN_READ,
    PIN_RESET,
    "clearStaleSecondaryRefs(staleEntryIds)",
)

# Arm 7: the per-device applier's BODY (so an emptied helper cannot pass).
CLEAR_HEADER = "private fun clearStaleSecondaryRefs("
CLEAR_ARM = (
    "decideStaleRefCleanup(lastUsedEntryId",
    "decideStaleRefCleanup(voiceInputOverrideEntryId",
    "decideStaleRefCleanup(voiceOutputOverrideEntryId",
)
DECIDE_STALE_ARM = "internal fun decideStaleRefCleanup("

# Arm 8: the relocated picker's prefs listener.
UI_ARM = (
    "val listener = SharedPreferences.OnSharedPreferenceChangeListener",
    "AgentRuntimeLimitsPrefs.PREFS",
    "registerOnSharedPreferenceChangeListener(listener)",
    "AgentRuntimeLimitsPrefs.KEY_COMPACT_MODEL_ENTRY_ID",
    "compactModelEntryId = AgentRuntimeLimitsPrefs.compactModelEntryId()",
    "unregisterOnSharedPreferenceChangeListener(listener)",
)

# Code anchors that must appear BEFORE the pin handling inside the same
# function, i.e. the pin edit rides along with the cascade it mirrors.
UPDATE_AFTER = "if (renamed)"
REMOVE_AFTER = "config.agentLoopModelEntryIds.removeAll { it == entryId }"

# Arm 9: updateEntry's rename branch must migrate the two prefs-held voice
# overrides. The criterion hangs on the WRITE POINTS (the `== oldKey`
# comparisons and the `= newUuid` rewrites) inside `if (renamed)` — family
# lesson (deep-audit-1030-E): a second release/clean-up path forks silently
# unless the criterion is bound to the point where the write happens, not to a
# function name a refactor can rename around.
VOICE_RENAME_ARM = (
    "voiceInputOverrideEntryId == oldKey",
    "voiceInputOverrideEntryId = newUuid",
    "voiceOutputOverrideEntryId == oldKey",
    "voiceOutputOverrideEntryId = newUuid",
)


def code_only(text):
    """Strip // line comments so no criterion can be satisfied from prose."""
    return "\n".join(line.split("//")[0] for line in text.split("\n"))


def _balanced(src, open_idx, opener="{", closer="}"):
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == opener:
            depth += 1
        elif src[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def function_text(src, header):
    i = src.find(header)
    if i < 0:
        return None
    brace = src.find("{", i)
    if brace < 0:
        return None
    end = _balanced(src, brace)
    return None if end < 0 else src[i:end]


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


# ── wide-1024: shared shape predicates for the body-level arms ────────────
# A body that is one of these is a DECISION that was replaced by a constant.
CONST_BODY_RE = re.compile(
    r"^(?:null|true|false|0|-1|\"\"|''|"
    r"(?:empty(?:List|Set|Map|Sequence)|listOf|setOf|mapOf|arrayOf)"
    r"\s*(?:<[^()]*>)?\s*\(\s*\))$")
# A condition that can never be true — the branch is dead code.
DEAD_COND_RE = re.compile(r"(?<![\w.])false\b|!\s*true\b|\b0\s*!=\s*0\b|\b1\s*==\s*0\b|"
                          r"\btrue\s*==\s*false\b")
EMPTY_OR_CONST_COLL_RE = re.compile(
    r"^(?:empty(?:List|Set|Sequence|Array)\s*(?:<[^()]*>)?\s*\(\s*\)|"
    r"(?:listOf|setOf|arrayListOf|mutableListOf|sequenceOf)"
    r"\s*(?:<[^()]*>)?\s*\(\s*\))$")
# Any collection DERIVATION — the set is computed from something.
DERIVE_CALL_RE = re.compile(
    r"\.\s*(?:map|mapNotNull|filter|filterNot|asSequence|asIterable|drop|take|"
    r"sortedBy|sortedByDescending|sortedWith|toList|toSet|distinct|plus|minus)\b")


def _stmt_end(text, start):
    """Index of the newline ending the statement at `start` (bracket depth
    aware, so a lambda inside the expression does not cut it short)."""
    depth = 0
    i, n = start, len(text)
    while i < n:
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "\n" and depth <= 0:
            # A chained call (`val x = list\n  .filter { … }\n  .map { … }`)
            # continues on the next line — a naive newline cut would read the
            # derivation as the bare receiver and lose every `.map`/`.filter`.
            j = i + 1
            while j < n and text[j] in " \t":
                j += 1
            if text[j:j + 1] == "." or text[start:i].rstrip().endswith(
                    ("?:", "=", "&&", "||", ",", "+", "(")):
                i += 1
                continue
            return i
        i += 1
    return n


def decl_body(src, header):
    """(body_text, parameter_names) for the declaration at `header`.

    Handles both shapes this file uses: an expression body (`… = if (x) a else
    b`, possibly starting on the next line) and a block body (`…) { … }`).
    Returns (None, params) when neither can be read — callers fail closed."""
    i = src.find(header)
    if i < 0:
        return None, []
    op = src.find("(", i)
    if op < 0:
        return None, []
    cp = _balanced(src, op, "(", ")")
    if cp < 0:
        return None, []
    inner = src[op + 1:cp - 1]
    params = re.findall(r"([A-Za-z_]\w*)\s*:", inner)
    k = cp
    while k < len(src):
        c = src[k]
        if c == "{":
            end = _balanced(src, k, "{", "}")
            return (src[k:end] if end > k else None), params
        if c == "=" and src[k + 1:k + 2] != "=":
            start = k + 1
            while start < len(src) and src[start] in " \t\r\n":
                start += 1
            return src[start:_stmt_end(src, start)], params
        k += 1
    return None, params


def check_body(src, rel, header, arm, anchors, what):
    body = function_text(src, header)
    if body is None:
        return [(rel, 0, arm, header + " not found — gate ROT")]
    code = code_only(body)
    base = src.find(header)
    out = []
    for a in anchors:
        if a not in code:
            out.append((rel, line_of(src, base), arm,
                        "%s is missing code anchor `%s`" % (what, a)))
    return out


def check_order(src, rel, header, arm, first, second, what):
    """`first` must appear before `second` inside the function (code only)."""
    body = function_text(src, header)
    if body is None:
        return [(rel, 0, arm, header + " not found — gate ROT")]
    code = code_only(body)
    base = src.find(header)
    i, j = code.find(first), code.find(second)
    if i < 0:
        return [(rel, line_of(src, base), arm,
                 "%s is missing code anchor `%s`" % (what, first))]
    if j >= 0 and j < i:
        return [(rel, line_of(src, base), arm,
                 "%s: `%s` must come AFTER `%s`" % (what, second, first))]
    return []


def check_inside(src, rel, header, arm, guard, needle, what):
    """`needle` must appear INSIDE the `guard { ... }` block of `header`.

    An index-order check is too weak here: it passes whenever the needle
    merely follows the guard opening, which is also true when the needle sits
    after the guard's closing brace (the migration would then run on every
    update, not just renames).
    """
    body = function_text(src, header)
    if body is None:
        return [(rel, 0, arm, header + " not found — gate ROT")]
    base = src.find(header)
    g = body.find(guard)
    if g < 0:
        return [(rel, line_of(src, base), arm,
                 "%s is missing guard `%s`" % (what, guard))]
    brace = body.find("{", g)
    end = _balanced(body, brace) if brace >= 0 else -1
    if brace < 0 or end < 0:
        return [(rel, line_of(src, base), arm,
                 "%s guard `%s` is not a braced block — gate ROT" % (what, guard))]
    if needle not in code_only(body[brace:end]):
        return [(rel, line_of(src, base), arm,
                 "%s: `%s` must sit INSIDE `%s`" % (what, needle, guard))]
    return []


def check_inside_else_of(src, rel, header, arm, guard, needles, what):
    """`needles` must appear INSIDE the `else { ... }` of `guard`.

    Used for arms 5: index order is too weak (a cleanup placed after the guard
    block still passes), and `check_inside` cannot express "the else branch",
    which is exactly the requirement — the cascade must NOT run when the
    models list shrank suspiciously. The `else` is located by scanning FORWARD
    from `guard`, so an earlier `else` block in the same function cannot
    silently become the pinned one.
    """
    body = function_text(src, header)
    if body is None:
        return [(rel, 0, arm, header + " not found — gate ROT")]
    base = src.find(header)
    g = body.find(guard)
    if g < 0:
        return [(rel, line_of(src, base), arm,
                 "%s is missing guard `%s`" % (what, guard))]
    e = body.find("else", g)
    if e < 0:
        return [(rel, line_of(src, base), arm,
                 "%s: `%s` has no `else` branch — gate ROT" % (what, guard))]
    brace = body.find("{", e)
    end = _balanced(body, brace) if brace >= 0 else -1
    if brace < 0 or end < 0:
        return [(rel, line_of(src, base), arm,
                 "%s: the `else` of `%s` is not a braced block — gate ROT" % (what, guard))]
    block = code_only(body[brace:end])
    return [(rel, line_of(src, base), arm,
             "%s: `%s` must sit INSIDE the `else` of `%s`" % (what, n, guard))
            for n in needles if n not in block]


def check_helper_bodies(src, rel):
    """arm 4 (dataflow): the decision helpers must still DECIDE.

    The pre-fix criterion only proved the three `internal fun decide…(`
    declarations exist. Gutting the body — `= null` on
    decideCompactPinMigration, `= false` on the two cleanups — keeps every
    declaration, keeps all nine call sites, and silently disables all nine
    cascades: the pin and the three per-device references dangle forever.

    Criterion: each helper's BODY must be a real expression (not a constant)
    and must read every parameter it declares (a body that ignores
    `removedEntryIds` cannot be deciding on it). Unparseable body fail-closes."""
    out = []
    for header in HELPER_ARM + (DECIDE_STALE_ARM,):
        body, params = decl_body(src, header)
        name = header.split("fun ", 1)[-1].rstrip("(")
        if body is None:
            out.append((rel, 0, "4", "helper `%s` has no body this gate can read "
                                   "(fail-closed: re-point arm 4)" % name))
            continue
        flat = re.sub(r"\s+", " ", body).strip()
        if CONST_BODY_RE.match(flat):
            out.append((rel, line_of(src, src.find(header)), "4",
                        "helper `%s` returns a CONSTANT (`%s`) — the decision is gone "
                        "while every call site still calls it" % (name, flat)))
            continue
        missing = [p for p in params if not re.search(r"(?<![.\w])%s(?![\w])" % re.escape(p), body)]
        if missing:
            out.append((rel, line_of(src, src.find(header)), "4",
                        "helper `%s` never reads %s — a parameter it declares cannot "
                        "influence the decision (fail-closed)"
                        % (name, ", ".join("`%s`" % p for p in missing))))
    return out


def check_clear_applier(src, rel):
    """arm 7 (dataflow, second half): the applier's branches must be LIVE.

    The pre-fix criterion matched six substrings in the body — the three
    `decideStaleRefCleanup(<ref>` calls and the three `= null` writes. Wrapping
    each condition in `false && …` (`if (false && decideStaleRefCleanup(
    lastUsedEntryId, …))`) keeps every substring, and turns the single
    convergence point that arms 2 / 3 / 5 / 6 funnel through into a no-op:
    after a provider delete, `lastUsedEntryId` and both voice overrides keep
    pointing at entry ids that no longer exist.

    Criterion: every `if` condition in the body that mentions a cleanup
    predicate must not be a dead constant, and each reference must actually be
    assigned `null` somewhere in the body."""
    out = []
    body = function_text(src, CLEAR_HEADER)
    if body is None:
        return [(rel, 0, "7", CLEAR_HEADER + " not found — gate ROT")]
    code = code_only(body)
    base = src.find(CLEAR_HEADER)
    for m in re.finditer(r"\bif\s*\(", code):
        op = m.end() - 1
        close = _balanced(code, op, "(", ")")
        if close < 0:
            out.append((rel, line_of(src, base), "7",
                        "clearStaleSecondaryRefs: an `if (` condition does not balance "
                        "(fail-closed)"))
            continue
        cond = code[op:close]
        if DEAD_COND_RE.search(cond):
            out.append((rel, line_of(src, base + m.start()), "7",
                        "clearStaleSecondaryRefs gates a cleanup on a DEAD condition "
                        "(`%s`) — the branch can never run, so the reference survives "
                        "the delete it was meant to heal"
                        % re.sub(r"\s+", " ", cond)[:90]))
    for ref in ("lastUsedEntryId", "voiceInputOverrideEntryId", "voiceOutputOverrideEntryId"):
        if not re.search(r"(?<![.\w])%s\s*=\s*null\b" % re.escape(ref), code):
            out.append((rel, line_of(src, base), "7",
                        "clearStaleSecondaryRefs never clears `%s` — the reference "
                        "outlives the entries it points at" % ref))
    return out


def check_stale_entry_ids(src, rel):
    """arm 6 (dataflow): the stale-entry-id set must be DERIVED from the tree.

    The pre-fix criterion matched the `val staleEntryIds` declaration and the
    three consumers. Assigning `emptyList<String>()` keeps all four anchors and
    makes the whole cascade a no-op: the seeds are dissolved from
    `config.modelEntries`, but the pin / group members / agent-loop pins are
    told that nothing was removed.

    Criterion: the declaration is read as an EXPRESSION — it must compute the
    set (a collection-derivation call) and must not be a constant."""
    out = []
    body = function_text(src, "fun ensureVoiceTemplateModels(")
    if body is None:
        return [(rel, 0, "6", "fun ensureVoiceTemplateModels( not found — gate ROT")]
    code = code_only(body)
    base = src.find("fun ensureVoiceTemplateModels(")
    m = re.search(r"\bval\s+staleEntryIds\s*=\s*", code)
    if m is None:
        return out  # the anchor loop already reports the missing declaration
    end = _stmt_end(code, m.end())
    expr = code[m.end():end]
    flat = re.sub(r"\s+", " ", expr).strip()
    if EMPTY_OR_CONST_COLL_RE.match(flat):
        out.append((rel, line_of(src, base + m.start()), "6",
                    "`val staleEntryIds` is a CONSTANT (`%s`) — the cascade is told "
                    "that nothing was removed while the seed entries are still "
                    "dissolved, so pin / group members / agent-loop pins / per-device "
                    "refs all dangle" % flat[:70]))
    elif not DERIVE_CALL_RE.search(flat):
        out.append((rel, line_of(src, base + m.start()), "6",
                    "`val staleEntryIds` no longer derives the set from the entry list "
                    "(`%s`) — fail-closed: re-point this arm at the new derivation"
                    % flat[:70]))
    return out


def run_checks(root, rel=REPO_REL):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return [(rel, 0, "?", "file not found — gate ROT")]
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()

    out = []
    out += check_body(src, rel, "fun updateEntry(", "1", UPDATE_ARM, "updateEntry rename branch")
    out += check_inside(src, rel, "fun updateEntry(", "1", UPDATE_AFTER,
                        "decideCompactPinMigration(", "updateEntry rename branch")
    out += check_body(src, rel, "fun removeEntry(", "2", REMOVE_ARM, "removeEntry")
    out += check_order(src, rel, "fun removeEntry(", "2", REMOVE_AFTER,
                       "decideCompactPinCleanup(", "removeEntry")
    out += check_body(src, rel, "fun removeInstance(", "3", REMOVE_INSTANCE_ARM, "removeInstance")
    body_all = code_only(src)
    for a in HELPER_ARM:
        if a not in body_all:
            out.append((rel, 0, "4", "helper missing: `%s`" % a))
    # arm 4 (dataflow): the helpers must still DECIDE — a gutted body keeps
    # every declaration and call site while disabling all nine cascades.
    out += check_helper_bodies(src, rel)
    # ── [da1024-C] ──
    out += check_inside_else_of(src, rel, "fun replaceEntries(", "5",
                                REPLACE_SHRINK_GUARD, REPLACE_ARM, "replaceEntries prune branch")
    for needle in VOICE_ARM:
        out += check_inside(src, rel, "fun ensureVoiceTemplateModels(", "6", VOICE_GUARD,
                            needle, "ensureVoiceTemplateModels stale-seed branch")
    # arm 6 (dataflow): the set must be DERIVED — `emptyList<String>()` keeps
    # every anchor above and turns the cascade into a no-op.
    out += check_stale_entry_ids(src, rel)
    out += check_body(src, rel, CLEAR_HEADER, "7", CLEAR_ARM, "clearStaleSecondaryRefs")
    # arm 7 (dataflow): the applier's branches must be LIVE (`false && …` keeps
    # all six anchors and disables the single convergence point).
    out += check_clear_applier(src, rel)
    if DECIDE_STALE_ARM not in body_all:
        out.append((rel, 0, "7", "helper missing: `%s`" % DECIDE_STALE_ARM))
    for needle in VOICE_RENAME_ARM:
        out += check_inside(src, rel, "fun updateEntry(", "9", UPDATE_AFTER,
                            needle, "updateEntry rename branch voice overrides")
    return out


def run_ui_checks(root, rel=UI_REL):
    """Arm 8: the ModelGroupsScreen prefs listener that keeps the relocated
    compact-pin picker in sync with external writers."""
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return [(rel, 0, "8", "file not found — gate ROT")]
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()
    code = code_only(src)
    return [(rel, line_of(src, code.find("ModelGroupsScreen")), "8",
             "ModelGroupsScreen compact-pin listener is missing code anchor `%s`" % a)
            for a in UI_ARM if a not in code]


def run_all(root):
    return run_checks(root) + run_ui_checks(root)


# ----------------------------------------------------------------------
# Selftest fixtures — Kotlin-shaped snippets exercising every arm.
#
# Every fixture writes BOTH files the gate reads (repository + ModelGroups
# Screen). A fixture that forgot one would light the "file not found — gate
# ROT" arm instead, which is exactly what the ROT control checks.
#
# Each bad fixture is built as a DELTA of the fixed one, so it violates its own
# arm and nothing else; `_report` asserts that (an arm that only ever goes red
# together with others is not evidence of its own criterion).
# ----------------------------------------------------------------------
PR_OK = """class ProviderRepository(private val context: Context) {
    private fun clearStaleSecondaryRefs(removedEntryIds: Collection<String>) {
        if (decideStaleRefCleanup(lastUsedEntryId, removedEntryIds)) lastUsedEntryId = null
        if (decideStaleRefCleanup(voiceInputOverrideEntryId, removedEntryIds)) {
            voiceInputOverrideEntryId = null
        }
        if (decideStaleRefCleanup(voiceOutputOverrideEntryId, removedEntryIds)) {
            voiceOutputOverrideEntryId = null
        }
    }

    fun ensureVoiceTemplateModels() = synchronized(configLock) {
        val staleSeedIds = instanceEntries
            .filter { it.baseModel.isVoiceTemplateSeedShape }
            .map { it.baseModel.id }
            .filter { it !in templateById }
        if (staleSeedIds.isNotEmpty()) {
            val staleEntryIds = instanceEntries
                .filter { it.baseModel.isVoiceTemplateSeedShape }
                .filter { it.baseModel.id !in templateById }
                .map { it.id }
            config.modelEntries.removeAll {
                it.providerInstanceId == instance.id && it.baseModel.id in staleSeedIds
            }
            for (group in config.modelGroups) {
                group.memberEntryIds.removeAll { it in staleEntryIds }
            }
            config.agentLoopModelEntryIds.removeAll { it in staleEntryIds }
            if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), staleEntryIds)) {
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
            }
            clearStaleSecondaryRefs(staleEntryIds)
            changed = true
        }
    }

    fun replaceEntries(instanceId: String, models: List<LLMModel>) = synchronized(configLock) {
        val prunedEntryIds = existingEntryIds - survivingEntryIds
        if (prunedEntryIds.isNotEmpty()) {
            val suspiciousShrink = existing.size >= 4 && models.size * 2 < existing.size
            if (suspiciousShrink) {
                android.util.Log.w("ProviderRepo", "[ModelList] replaceEntries SUSPICIOUS SHRINK")
            } else {
                for (i in config.modelGroups.indices) {
                    config.modelGroups[i].memberEntryIds.removeAll { it in prunedEntryIds }
                }
                config.agentLoopModelEntryIds.removeAll { it in prunedEntryIds }
                if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), prunedEntryIds)) {
                    AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
                }
                clearStaleSecondaryRefs(prunedEntryIds)
            }
        }
        saveConfig(config)
    }

    fun updateEntry(entry: ModelEntry): String? = synchronized(configLock) {
        val renamed = decision.newKey != null
        val newUuid = decision.newKey ?: existing.id
        if (renamed) {
            val oldKey = decision.oldKey
            if (lastUsedEntryId == oldKey) lastUsedEntryId = newUuid
            if (voiceInputOverrideEntryId == oldKey) voiceInputOverrideEntryId = newUuid
            if (voiceOutputOverrideEntryId == oldKey) voiceOutputOverrideEntryId = newUuid
            decideCompactPinMigration(
                pinEntryId = AgentRuntimeLimitsPrefs.compactModelEntryId(),
                oldKey = oldKey,
                newKey = newUuid,
            )?.let { migrated ->
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = migrated)
            }
        }
        saveConfig(config)
        return@synchronized newUuid
    }

    fun removeEntry(entryId: String) = synchronized(configLock) {
        config.modelEntries.removeAll { it.id == entryId }
        config.agentLoopModelEntryIds.removeAll { it == entryId }
        if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), setOf(entryId))) {
            AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
        }
        clearStaleSecondaryRefs(setOf(entryId))
        saveConfig(config)
    }

    fun removeInstance(instanceId: String) = synchronized(configLock) {
        if (removedEntryIds.isNotEmpty()) {
            config.agentLoopModelEntryIds.removeAll { it in removedEntryIds }
            if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), removedEntryIds)) {
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
            }
            clearStaleSecondaryRefs(removedEntryIds)
        }
        saveConfig(config)
    }
}

internal fun decideCompactPinMigration(pinEntryId: String, oldKey: String, newKey: String): String? =
    if (pinEntryId.isNotEmpty() && pinEntryId == oldKey && oldKey != newKey) newKey else null

internal fun decideCompactPinCleanup(pinEntryId: String, removedEntryIds: Collection<String>): Boolean =
    pinEntryId.isNotEmpty() && removedEntryIds.contains(pinEntryId)

internal fun decideStaleRefCleanup(ref: String?, removedEntryIds: Collection<String>): Boolean =
    ref != null && removedEntryIds.contains(ref)
"""

UI_OK = """package com.rikkaminis.app.ui.settings

@Composable
fun ModelGroupsScreen(providerRepository: ProviderRepository) {
    val context = LocalContext.current
    var compactModelEntryId by remember {
        mutableStateOf(AgentRuntimeLimitsPrefs.compactModelEntryId())
    }
    DisposableEffect(context) {
        val listener = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
            if (key == null || key == AgentRuntimeLimitsPrefs.KEY_COMPACT_MODEL_ENTRY_ID) {
                compactModelEntryId = AgentRuntimeLimitsPrefs.compactModelEntryId()
            }
        }
        val pinPrefs = context.applicationContext.getSharedPreferences(
            AgentRuntimeLimitsPrefs.PREFS,
            Context.MODE_PRIVATE,
        )
        pinPrefs.registerOnSharedPreferenceChangeListener(listener)
        onDispose { pinPrefs.unregisterOnSharedPreferenceChangeListener(listener) }
    }
}
"""

_MIGRATION_BLOCK = """            decideCompactPinMigration(
                pinEntryId = AgentRuntimeLimitsPrefs.compactModelEntryId(),
                oldKey = oldKey,
                newKey = newUuid,
            )?.let { migrated ->
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = migrated)
            }
"""

# Regression shape A: the pre-fix world — pin untouched on rename.
PR_BAD_NO_MIGRATION = PR_OK.replace(_MIGRATION_BLOCK, "")

# Regression shape B: pin migrated but on the WRONG side of the rename guard
# (i.e. after the branch closes) — must be rejected as ordering violation.
PR_BAD_ORDER = PR_OK.replace(_MIGRATION_BLOCK, "").replace(
    """                if (lastUsedEntryId == oldKey) lastUsedEntryId = newUuid
            }
""",
    """                if (lastUsedEntryId == oldKey) lastUsedEntryId = newUuid
            }
""" + _MIGRATION_BLOCK,
)

# Regression shape C: removeEntry/removeInstance cascades still missing the pin.
_REMOVE_ENTRY_PIN = """        if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), setOf(entryId))) {
            AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
        }
"""
_REMOVE_INSTANCE_PIN = """            if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), removedEntryIds)) {
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
            }
"""
PR_BAD_NO_CLEANUP = PR_OK.replace(_REMOVE_ENTRY_PIN, "").replace(_REMOVE_INSTANCE_PIN, "")

# Regression shape D: helper removed (inlined at call sites) — JVM slice gone.
PR_BAD_NO_HELPER = PR_OK.replace("internal fun decideCompactPinCleanup", "private fun legacyCleanup")

# Regression shape E: replaceEntries' prune branch cleaned only the in-config
# refs (the ISSUE-03 path-A pre-fix world).
_REPLACE_CLEANUP = """                if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), prunedEntryIds)) {
                    AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
                }
                clearStaleSecondaryRefs(prunedEntryIds)
"""
PR_BAD_NO_REPLACE_CLEANUP = PR_OK.replace(_REPLACE_CLEANUP, "")

# Regression shape F: cleanup present but OUTSIDE the else branch — it would
# also fire on a suspicious shrink, nuking the user's pin on an API hiccup.
PR_BAD_CLEANUP_OUTSIDE_ELSE = PR_OK.replace(
    _REPLACE_CLEANUP + "            }\n",
    "            }\n" + _REPLACE_CLEANUP.replace("\n                ", "\n        "),
)

# Regression shape G: ensureVoiceTemplateModels dissolved the seeds but cleaned
# only the entry rows (the ISSUE-03 path-B pre-fix world).
_VOICE_CASCADE = """            for (group in config.modelGroups) {
                group.memberEntryIds.removeAll { it in staleEntryIds }
            }
            config.agentLoopModelEntryIds.removeAll { it in staleEntryIds }
            if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), staleEntryIds)) {
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
            }
            clearStaleSecondaryRefs(staleEntryIds)
"""
PR_BAD_NO_VOICE_CASCADE = PR_OK.replace(_VOICE_CASCADE, "")

# Regression shape L (da1031-A): the cascade restored to the pre-fix spelling —
# bare baseModel.id values compared against entry-id references (all four
# routes a silent no-op). Arm 6's re-anchored criterion must catch exactly
# this shape, including the missing `val staleEntryIds` declaration.
_VOICE_CASCADE_BARE = """            for (group in config.modelGroups) {
                group.memberEntryIds.removeAll { it in staleSeedIds }
            }
            config.agentLoopModelEntryIds.removeAll { it in staleSeedIds }
            if (decideCompactPinCleanup(AgentRuntimeLimitsPrefs.compactModelEntryId(), staleSeedIds)) {
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = "")
            }
            clearStaleSecondaryRefs(staleSeedIds)
"""
_VOICE_DECL = """            val staleEntryIds = instanceEntries
                .filter { it.baseModel.isVoiceTemplateSeedShape }
                .filter { it.baseModel.id !in templateById }
                .map { it.id }
"""
PR_BAD_BARE_SEED_IDS = PR_OK.replace(_VOICE_DECL, "").replace(_VOICE_CASCADE, _VOICE_CASCADE_BARE)

# Regression shape M (da1031-A): updateEntry renamed but did NOT migrate the
# two prefs-held voice overrides (they dangle and the voice panel silently
# falls back to the group binding).
_VOICE_RENAME_BLOCK = """            if (voiceInputOverrideEntryId == oldKey) voiceInputOverrideEntryId = newUuid
            if (voiceOutputOverrideEntryId == oldKey) voiceOutputOverrideEntryId = newUuid
"""
PR_BAD_NO_VOICE_RENAME = PR_OK.replace(_VOICE_RENAME_BLOCK, "")

# Regression shape N: the voice overrides migrated OUTSIDE `if (renamed)` —
# the migration would fire on every update, not just renames.
PR_BAD_VOICE_RENAME_OUTSIDE = PR_OK.replace(_VOICE_RENAME_BLOCK, "").replace(
    """            )?.let { migrated ->
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = migrated)
            }
        }
""",
    """            )?.let { migrated ->
                AgentRuntimeLimitsPrefs.save(context, compactModelEntryId = migrated)
            }
        }
""" + _VOICE_RENAME_BLOCK,
)

# Regression shape H: the applier is called everywhere but its body is empty —
# a call-site-only arm must not be satisfiable that way.
_APPLIER_BODY = """        if (decideStaleRefCleanup(lastUsedEntryId, removedEntryIds)) lastUsedEntryId = null
        if (decideStaleRefCleanup(voiceInputOverrideEntryId, removedEntryIds)) {
            voiceInputOverrideEntryId = null
        }
        if (decideStaleRefCleanup(voiceOutputOverrideEntryId, removedEntryIds)) {
            voiceOutputOverrideEntryId = null
        }
"""
PR_BAD_EMPTY_APPLIER = PR_OK.replace(_APPLIER_BODY, "")

# ── wide-1024 witnesses (each mechanically reproduced on the real tree:
# pre-fix rc=0, post-fix rc=1) ────────────────────────────────────────────

_MIGRATION_BODY = """internal fun decideCompactPinMigration(pinEntryId: String, oldKey: String, newKey: String): String? =
    if (pinEntryId.isNotEmpty() && pinEntryId == oldKey && oldKey != newKey) newKey else null"""

# W1 (dataflow): the helper is still declared and still called — its body is
# now a constant, so all nine cascades are no-ops.
PR_W_BAD_HELPER_CONST = PR_OK.replace(
    _MIGRATION_BODY,
    """internal fun decideCompactPinMigration(pinEntryId: String, oldKey: String, newKey: String): String? =
    null""")

# W2 (dataflow): every anchor and every `= null` write stays put; each branch
# is simply dead.
PR_W_BAD_DEAD_GUARD = PR_OK.replace(
    "if (decideStaleRefCleanup(",
    "if (false && decideStaleRefCleanup(")

# W3 (dataflow): the declaration survives, the three consumers survive, the
# set is a constant — the cascade is told nothing was removed.
_STALE_IDS_DECL = """            val staleEntryIds = instanceEntries
                .filter { it.baseModel.isVoiceTemplateSeedShape }
                .filter { it.baseModel.id !in templateById }
                .map { it.id }
"""
PR_W_BAD_EMPTY_STALE_IDS = PR_OK.replace(
    _STALE_IDS_DECL, "            val staleEntryIds = emptyList<String>()\n")

# Regression shape I: inline lambda listener — no strong holder (exactly the
# hazard prefs_listener_holder_guard exists for) and nothing unregisters it.
_LISTENER_DECL = """        val listener = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
            if (key == null || key == AgentRuntimeLimitsPrefs.KEY_COMPACT_MODEL_ENTRY_ID) {
                compactModelEntryId = AgentRuntimeLimitsPrefs.compactModelEntryId()
            }
        }
"""
UI_BAD_INLINE = UI_OK.replace(_LISTENER_DECL, "").replace(
    "pinPrefs.registerOnSharedPreferenceChangeListener(listener)",
    """pinPrefs.registerOnSharedPreferenceChangeListener(
            SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
                if (key == AgentRuntimeLimitsPrefs.KEY_COMPACT_MODEL_ENTRY_ID) {
                    compactModelEntryId = AgentRuntimeLimitsPrefs.compactModelEntryId()
                }
            },
        )""",
).replace("        onDispose { pinPrefs.unregisterOnSharedPreferenceChangeListener(listener) }\n", "")

# Regression shape J: named, strongly-held listener that never re-reads the pin
# (registered, survives GC, and still shows the stale value).
UI_BAD_NO_REFRESH = UI_OK.replace(
    "compactModelEntryId = AgentRuntimeLimitsPrefs.compactModelEntryId()\n            }",
    "// refresh removed\n            }",
)

# Regression shape K: listener bound to the wrong prefs file.
UI_BAD_WRONG_PREFS = UI_OK.replace("            AgentRuntimeLimitsPrefs.PREFS,", '            "provider_config",')


def _report(name, label, checks, want_arms, contains=None, exact=True):
    hit = [c for c in checks
           if c[2] in want_arms and (contains is None or contains in c[3])]
    if not hit:
        print("  [FAIL] %s (%s): no finding on arm(s) %s (want text %r)"
              % (name, label, want_arms, contains))
        return False
    if exact:
        extra = sorted({c[2] for c in checks} - set(want_arms))
        if extra:
            print("  [FAIL] %s (%s): also fired unrelated arm(s) %s — the fixture is"
                  " not a single-criterion delta" % (name, label, extra))
            return False
    print("  [OK]   %s (%s): %d finding(s) on arm(s) %s" % (name, label, len(hit), want_arms))
    return True


def selftest():
    ok = True
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        repo_target = os.path.join(tmp, REPO_REL)
        ui_target = os.path.join(tmp, UI_REL)
        os.makedirs(os.path.dirname(repo_target), exist_ok=True)
        os.makedirs(os.path.dirname(ui_target), exist_ok=True)

        def write(repo_text, ui_text=UI_OK):
            with open(repo_target, "w", encoding="utf-8") as f:
                f.write(repo_text)
            with open(ui_target, "w", encoding="utf-8") as f:
                f.write(ui_text)

        # 1) fixed shape → all 8 arms clean (precision control).
        write(PR_OK)
        c = run_all(tmp)
        if c:
            print("  [FAIL] fixed fixture not clean: %s" % (c[0],))
            ok = False
        else:
            print("  [OK]   fixed fixture: all 9 arms clean")

        # 2) pre-fix shape → arm 1 must fire on the missing migration.
        write(PR_BAD_NO_MIGRATION)
        ok &= _report("no-migration", "arm 1", run_all(tmp), ("1",),
                      contains="decideCompactPinMigration(")

        # 3) migration placed OUTSIDE `if (renamed)` → containment violation.
        write(PR_BAD_ORDER)
        ok &= _report("wrong-order", "arm 1", run_all(tmp), ("1",),
                      contains="must sit INSIDE")

        # 4) delete paths untouched → arms 2+3 fire.
        write(PR_BAD_NO_CLEANUP)
        ok &= _report("no-cleanup", "arms 2/3", run_all(tmp), ("2", "3"))

        # 5) pure slice removed → arm 4 fires.
        write(PR_BAD_NO_HELPER)
        ok &= _report("no-helper", "arm 4", run_all(tmp), ("4",))

        # 6) replaceEntries' prune branch untouched → arm 5 fires.
        write(PR_BAD_NO_REPLACE_CLEANUP)
        ok &= _report("no-replace-cleanup", "arm 5", run_all(tmp), ("5",))

        # 7) replaceEntries cleanup outside the else → arm 5 containment fires.
        write(PR_BAD_CLEANUP_OUTSIDE_ELSE)
        ok &= _report("cleanup-outside-else", "arm 5", run_all(tmp), ("5",),
                      contains="INSIDE the `else`")

        # 8) voice-seed migration cleaned nothing but the rows → arm 6 fires.
        write(PR_BAD_NO_VOICE_CASCADE)
        ok &= _report("no-voice-cascade", "arm 6", run_all(tmp), ("6",))

        # 9) applier emptied → arm 7 fires (call sites are still in place).
        write(PR_BAD_EMPTY_APPLIER)
        ok &= _report("empty-applier", "arm 7", run_all(tmp), ("7",))

        # 10) inline listener → arm 8 fires.
        write(PR_OK, UI_BAD_INLINE)
        ok &= _report("ui-inline-listener", "arm 8", run_all(tmp), ("8",))

        # 11) listener that never re-reads the pin → arm 8 fires.
        write(PR_OK, UI_BAD_NO_REFRESH)
        ok &= _report("ui-no-refresh", "arm 8", run_all(tmp), ("8",))

        # 12) listener on the wrong prefs file → arm 8 fires.
        write(PR_OK, UI_BAD_WRONG_PREFS)
        ok &= _report("ui-wrong-prefs", "arm 8", run_all(tmp), ("8",))

        # 14) [da1031-A] arm 6 re-anchored: the bare-seed spelling (all four
        # routes a no-op) must be red, including the missing declaration.
        write(PR_BAD_BARE_SEED_IDS)
        ok &= _report("bare-seed-ids", "arm 6", run_all(tmp), ("6",),
                      contains="staleEntryIds")

        # 15) [da1031-A] updateEntry rename must migrate the voice overrides.
        write(PR_BAD_NO_VOICE_RENAME)
        ok &= _report("no-voice-rename", "arm 9", run_all(tmp), ("9",),
                      contains="voiceInputOverrideEntryId == oldKey")

        # 16) voice overrides migrated outside `if (renamed)` → containment.
        write(PR_BAD_VOICE_RENAME_OUTSIDE)
        ok &= _report("voice-rename-outside", "arm 9", run_all(tmp), ("9",),
                      contains="must sit INSIDE")

        # 18) [wide-1024 W1] helper gutted to a constant → arm 4 fires.
        write(PR_W_BAD_HELPER_CONST)
        ok &= _report("helper-constant-body", "arm 4", run_all(tmp), ("4",),
                      contains="returns a CONSTANT")

        # 19) [wide-1024 W2] applier branches dead-guarded → arm 7 fires.
        write(PR_W_BAD_DEAD_GUARD)
        ok &= _report("applier-dead-guard", "arm 7", run_all(tmp), ("7",),
                      contains="DEAD condition")

        # 20) [wide-1024 W3] stale id set replaced by a constant → arm 6.
        write(PR_W_BAD_EMPTY_STALE_IDS)
        ok &= _report("stale-ids-constant", "arm 6", run_all(tmp), ("6",),
                      contains="is a CONSTANT")

        # 17) ROT control (LAST: it wipes the fixture tree): both files missing
        # must be reported, never silently green (one finding per file, so a
        # half-populated tree cannot pass).
        shutil.rmtree(tmp)
        os.makedirs(tmp)
        c = run_all(tmp)
        rot = [f for f in c if "not found" in f[3]]
        if len(rot) != 2:
            print("  [FAIL] ROT control: missing files produced %d finding(s) (want 2): %s"
                  % (len(rot), c))
            ok = False
        else:
            print("  [OK]   ROT control: both missing files reported")

    print("COMPACT_ENTRY_PIN_MIGRATION_SELFTEST=%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    root = None
    if "--root" in args:
        i = args.index("--root")
        root = args[i + 1] if i + 1 < len(args) else None
    else:
        for a in args:
            if not a.startswith("-"):
                root = a
                break
    if not root:
        print("usage: compact_entry_pin_migration_check.py [ROOT] | --selftest", file=sys.stderr)
        return 2
    if not os.path.isdir(root):
        print("root not found: %s" % root, file=sys.stderr)
        return 2
    checks = run_all(root)
    if not checks:
        print("compact_entry_pin_migration_check: OK (9 arms green)")
        return 0
    for rel, line, arm, msg in checks:
        print("%s:%s [arm %s] %s" % (rel, line, arm, msg))
    print("compact_entry_pin_migration_check: %d violation(s)" % len(checks))
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Gate: FIX-E (deep-audit-1019 ISSUE-05) — a provider the user disabled must
never be used at runtime, and a session-model resolution site must decide
BEFORE it writes provider state.

ChatSessionLifecycle.kt had three resolution sites that missed the app-wide
"disabled providers are skipped" contract (see ProviderRepository
.enabledMemberEntries / resolveProviderFromGroup, and 1019 TRIAGE ISSUE-05):

  1. loadSession "Priority 2" (stored model_id) — wrote currentModel /
     _modelName / _activeEntryId FIRST and only then looked the instance +
     credential up, so a disabled provider or a missing API key left the UI
     claiming "model ready" while currentProvider stayed null (state
     pollution + bypass of the disable switch).
  2. restoreFromBinding "entry" branch — a user pin on an entry whose provider
     was later disabled still created a provider and dispatched requests to
     it (bypass of the disable switch).
  3. applyNewChatDefaultModel — same order problem + `return true` even when
     the credential was missing (caller told "applied" while nothing was).

The fix funnels all three sites through one pure decision,
`decideSessionModelResolution(entry, instance, apiKey)`, and writes state only
on `SessionModelResolution.Use`.

This gate pins three things (presence-asserted, so a legit refactor is a
deliberate edit to this gate rather than a silent pass):

  A. The funnel exists and enforces the isEnabled gate:
     `!instance.isEnabled` + PROVIDER_DISABLED inside its body.
  B. Each of the three sites routes through the funnel (the decision call is
     present in the site's source segment).
  C. ORDER: within each site segment, the decision call must appear BEFORE
     any provider-state write (`currentModel =` / `_modelName.value =` /
     `_activeEntryId.value =` / `_providerName.value =` / `currentProvider =`).

Usage: python3 scripts/scan/session_provider_enabled_check.py [repo_root]
       python3 scripts/scan/session_provider_enabled_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import os
import re
import sys

LIFECYCLE_REL = (
    "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatSessionLifecycle.kt"
)
APP_DIR = "src/android/app/src/main/java/com/rikkaminis/app"
ROUTING_REL = APP_DIR + "/ui/chat/ChatModelRouting.kt"
PROMPT_TOOLS_REL = APP_DIR + "/ui/chat/ChatPromptAndTools.kt"
DEBUG_DIR_REL = APP_DIR + "/debug"

# [deep-audit-1044 B-03] Every `ProviderFactory.create(...)` in the chat layer
# must hand over the ENRICHED model. The raw arguments below are each the
# entry's `baseModel.withOverrides(overrides)` — that never touches models.dev,
# and `:modelservice` (which decides vision / effort tiers / output cap FROM
# that object) never initialises ModelsDevApi either.
RAW_MODEL_ARGS = ("entry.model", "targetEntry.model", "cachedProvider.model")
# The single sources that ARE allowed to answer "which model": the shared
# resolver on the view model, and models.dev itself.
ENRICH_CALLS = ("providerModelFor(", "ModelsDevApi.effectiveModel(")
FACTORY_CALL = "ProviderFactory.create("
# Fail-closed: the three files carried 8 call sites when dv24-B03 was written.
MIN_FACTORY_CALLS = 8

FUNNEL_DEF = "internal fun decideSessionModelResolution("
FUNNEL_ENABLED = "!instance.isEnabled"
FUNNEL_DISABLED_REASON = "UnusableModelReason.PROVIDER_DISABLED"
USE_TYPE = "SessionModelResolution.Use"

STATE_WRITES = (
    "currentModel =",
    "_modelName.value =",
    "_activeEntryId.value =",
    "_providerName.value =",
    "currentProvider =",
)

P2_START = "// Priority 2: fall back to stored model_id"
P2_END = "// Priority 3: fall back to default group"
BINDING_SIG = "internal fun ChatViewModel.restoreFromBinding("
ENTRY_BRANCH_START = '"entry" -> {'
ENTRY_BRANCH_END = "else -> false"
APPLY_SIG = "internal fun ChatViewModel.applyNewChatDefaultModel()"

# ── B-03: the `ProviderFactory.create` model argument ─────────────────────────
# Every create site in the chat layer must hand over the ENRICHED model.
_DECL_HEAD = re.compile(
    r"^[ \t]*(?:internal |private |public |override |suspend )*fun\s", re.M)


def _strip_line_comments(text):
    """`//` comments out — a judgement must be satisfiable only by code."""
    return "\n".join(line.split("//")[0] for line in text.split("\n"))


def _call_args(text, open_idx):
    """Split the argument list whose `(` sits at `open_idx` (None if unbalanced)."""
    depth = 0
    args, cur = [], []
    for i in range(open_idx, len(text)):
        c = text[i]
        if c in "([{":
            depth += 1
            if depth == 1:
                continue
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(cur))
                return args
        elif c == "," and depth == 1:
            args.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    return None


def _enclosing_source(text, pos):
    """From the nearest preceding declaration to `pos` — the window in which a
    `val effectiveModel = providerModelFor(entry)` may have been resolved."""
    last = None
    for m in _DECL_HEAD.finditer(text, 0, pos):
        last = m
    return text[last.start():pos] if last else text[:pos]


def _assigned_from(window, name):
    """Is `name` assigned from one of ENRICH_CALLS inside `window`?"""
    pat = re.compile(r"\b(?:val|var)\s+%s\b[^=\n]*=" % re.escape(name))
    for m in pat.finditer(window):
        rest = window[m.end():]
        # include the continuation line so a wrapped `=\n providerModelFor(` counts
        head = rest.split("\n", 2)
        rhs = head[0] + ("\n" + head[1] if len(head) > 1 else "")
        if any(tok in rhs for tok in ENRICH_CALLS):
            return True
    return False


def check_factory_models(texts, min_calls=None):
    """[deep-audit-1044 B-03] Every `ProviderFactory.create(` in the chat layer
    passes an enriched model. Returns (violations, resolved_call_sites)."""
    min_calls = MIN_FACTORY_CALLS if min_calls is None else min_calls
    v, sites = [], 0
    for rel in sorted(texts):
        code = _strip_line_comments(texts[rel])
        for m in re.finditer(re.escape(FACTORY_CALL), code):
            sites += 1
            args = _call_args(code, m.end() - 1)
            if args is None or len(args) < 3:
                v.append("%s: unparseable %s call (fail-closed)" % (rel, FACTORY_CALL))
                continue
            model_arg = args[2].strip()
            if model_arg in RAW_MODEL_ARGS:
                v.append(
                    "%s: %s passes the raw `%s` — that is the entry's "
                    "baseModel+overrides, which carries no models.dev capabilities, "
                    "and `:modelservice` never initialises ModelsDevApi to fill them "
                    "in (dv24-B03)" % (rel, FACTORY_CALL, model_arg))
                continue
            if any(tok in model_arg for tok in ENRICH_CALLS):
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", model_arg) and \
                    _assigned_from(_enclosing_source(code, m.start()), model_arg):
                continue
            v.append(
                "%s: %s model argument `%s` is not resolved through %s — only the "
                "enriched model answers vision / effort tiers / output cap (dv24-B03)"
                % (rel, FACTORY_CALL, model_arg, " or ".join(ENRICH_CALLS)))
    if sites < min_calls:
        v.append("only %d %s call site(s) across %d file(s) — expected >= %d "
                 "(detection lost = ROT)"
                 % (sites, FACTORY_CALL, len(texts), min_calls))
    return v, sites


def _segment(text, start_needle, end_needle, where):
    """Return the source slice [start_needle .. end_needle), fail-closed."""
    i = text.find(start_needle)
    if i < 0:
        return None
    j = text.find(end_needle, i + len(start_needle))
    if j < 0:
        return None
    return text[i:j]


def _funnel_body(text):
    """Slice the funnel definition from its signature to its closing line."""
    i = text.find(FUNNEL_DEF)
    if i < 0:
        return None
    close = text.find("return SessionModelResolution.Use(entry, instance, apiKey)", i)
    if close < 0:
        return None
    # include the next `}` after the return statement (function brace)
    brace = text.find("}", close)
    if brace < 0:
        return None
    return text[i:brace]


def check_source(text):
    """Return a list of violation strings for the lifecycle source."""
    v = []
    site_segments = {}

    # ── A. funnel exists and enforces the isEnabled gate ──────────────────
    funnel = _funnel_body(text)
    if funnel is None:
        v.append("funnel missing: decideSessionModelResolution not found ( ROT? )")
    else:
        if FUNNEL_ENABLED not in funnel:
            v.append("funnel does not gate on !instance.isEnabled")
        if FUNNEL_DISABLED_REASON not in funnel:
            v.append("funnel does not map a disabled provider to PROVIDER_DISABLED")
        if "apiKey == null" not in funnel:
            v.append("funnel does not reject a missing credential (apiKey == null)")
        if USE_TYPE not in funnel:
            v.append("funnel does not produce SessionModelResolution.Use")

    # ── B/C. three sites: funnel call present, decision before state write ─
    p2 = _segment(text, P2_START, P2_END, "loadSession p2")
    if p2 is None:
        v.append("loadSession Priority 2 segment not found (anchors drifted)")
    else:
        site_segments["loadSession-priority2"] = p2

    binding_head = text.find(BINDING_SIG)
    if binding_head < 0:
        v.append("restoreFromBinding signature not found (anchors drifted)")
    else:
        entry = _segment(text[binding_head:], ENTRY_BRANCH_START, ENTRY_BRANCH_END, "entry")
        if entry is None:
            v.append("restoreFromBinding \"entry\" branch not found (anchors drifted)")
        else:
            site_segments["restoreFromBinding-entry"] = entry

    apply_head = text.find(APPLY_SIG)
    if apply_head < 0:
        v.append("applyNewChatDefaultModel signature not found (anchors drifted)")
    else:
        close = text.find("\n}", apply_head)
        if close < 0:
            v.append("applyNewChatDefaultModel body end not found (anchors drifted)")
        else:
            site_segments["applyNewChatDefaultModel"] = text[apply_head:close]

    for site, seg in site_segments.items():
        call_at = seg.find("decideSessionModelResolution(")
        if call_at < 0:
            v.append("%s: does not route through decideSessionModelResolution()" % site)
            continue
        for token in STATE_WRITES:
            wt = seg.find(token)
            if 0 <= wt < call_at:
                # allow `token` to the right of the call only
                v.append(
                    "%s: state write %r before the decision (must decide-then-write)"
                    % (site, token)
                )
                break

    # ── D. [deep-audit-1044 B-04] applyNewChatDefaultModel must publish the
    # model it just resolved. Its KDoc has always promised
    # "Sets currentModel / currentProvider / the name + activeEntry state
    # flows"; the body never wrote `currentModel`, so a new chat with no
    # default group left it null and three readers degraded silently
    # (ensureSession() persisted model_id = "unknown", thinkingInfo() returned
    # null, dynamicMaxTokens() skipped the remaining-context clamp).
    apply_seg = site_segments.get("applyNewChatDefaultModel")
    if apply_seg is not None and "currentModel =" not in apply_seg:
        v.append(
            "applyNewChatDefaultModel: never writes `currentModel =` — the KDoc "
            "promises it and every reader (session model_id / thinkingInfo / "
            "dynamicMaxTokens) falls back to null without it (dv24-B04)")
    return v


def _read(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def run(root):
    texts = {}
    for rel in (LIFECYCLE_REL, ROUTING_REL, PROMPT_TOOLS_REL):
        text = _read(root, rel)
        if text is None:
            print("session_provider_enabled_check: missing target %s" % rel)
            return 2
        texts[rel] = text
    violations = check_source(texts[LIFECYCLE_REL])
    factory_v, sites = check_factory_models(texts)
    violations += factory_v
    # [deep-audit-1047 B-04] group selection / member resolution sync
    violations += check_group_selection(texts)
    # [dv29-A2] C-SEL-4: the debug/IPC binding write must use the same decision
    debug_texts = {}
    debug_dir = os.path.join(root, DEBUG_DIR_REL)
    if os.path.isdir(debug_dir):
        for name in sorted(os.listdir(debug_dir)):
            if not name.endswith(".kt"):
                continue
            with open(os.path.join(debug_dir, name), "r", encoding="utf-8") as f:
                debug_texts[DEBUG_DIR_REL + "/" + name] = f.read()
    if not debug_texts:
        violations.append(
            "missing target %s/*.kt — C-SEL-4 cannot be judged (fail-closed)"
            % DEBUG_DIR_REL)
    else:
        violations += check_binding_writes(debug_texts)
    if violations:
        for x in violations:
            print("session_provider_enabled_check: %s" % x)
        print("GATE=RED session_provider_enabled (%d violation(s))" % len(violations))
        return 1
    print("session_provider_enabled_check: %d ProviderFactory.create site(s) resolved "
          "through the enriched model" % sites)
    print("GATE=GREEN session_provider_enabled")
    return 0


# ── [deep-audit-1047 B-04] group selection / member resolution sync ────────
#
# Three disconnects, all in the two files that own "which member serves this
# session":
#  C-SEL-1  selectGroup / selectGroupEntry published the display state
#           (_selectedGroupId / _selectedGroupName) BEFORE asking whether the
#           group could serve, so a group resolving to nothing still flipped
#           the top bar and the picker highlight while provider / binding
#           stayed on the old one.
#  C-SEL-2  the same call wrote the RAW requested entry id into
#           pendingEntryOverride / the persisted `lastEntryId` while a comment
#           two lines below declared "_activeEntryId is the truth" — after a
#           fallback the session was bound to a member that had been skipped.
#  C-SEL-3  selectEntry re-derived "instance + key" inline (no disable gate),
#           and both group consumers filtered members by `isEnabled` only, so
#           a key-less member at the head of a group sank the whole group.
#
# Every name below is DERIVED from the tree, not hard-coded: the acceptable
# "usability filter" wrappers come from the signatures that take a
# List<ModelEntry> and consult the funnel, and the names that count as "the
# answer of a resolution" come from the assignments whose right-hand side
# calls one of the resolution entry points. Renaming a helper keeps the gate
# green; deleting it turns every consumer red.


CSEL_GROUP_WRITE = "_selectedGroup"
FUNNEL_CALL = "decideSessionModelResolution("
FUNNEL_USE = "SessionModelResolution.Use"
RESOLVE_CALL = "resolveProviderFromGroup("
ACTIVE_ENTRY = "_activeEntryId"
MEMBERS_CALL = "enabledMemberEntries("

# The three ways a "can this group/member serve?" answer is produced. A local
# name assigned from any of them is accepted as the guard of a group write.
_RESOLVE_SOURCES = (RESOLVE_CALL, "planGroupSelect(", FUNNEL_CALL)

_EMPTY_VALUES = ("null", '""')
_WRITE_RE = re.compile(r"_(?:selectedGroupId|selectedGroupName)\.value\s*=\s*(.*)$", re.M)
_LAST_ENTRY_RE = re.compile(r'lastEntryId\\{0,2}":\\{0,2}"\$([A-Za-z_][A-Za-z0-9_]*)')
_DECL_RE = re.compile(r"^[ \t]*(?:internal |private |public |override |suspend )*fun\s", re.M)
_ASSIGN_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*((?:[^\n=]|\([^\n)]*\)){0,400})")


def _fun_span(text, pos):
    """(start, end) of the top-level `fun` declaration containing `pos`."""
    starts = [m.start() for m in _DECL_RE.finditer(text)]
    prev = None
    nxt = None
    for s in starts:
        if s <= pos:
            prev = s
        elif nxt is None:
            nxt = s
    if prev is None:
        return None
    return (prev, nxt if nxt is not None else len(text))


def _resolution_names(code):
    """Local names that carry the answer of a resolution.

    Derived, not enumerated: anything whose right-hand side calls
    resolveProviderFromGroup / planGroupSelect / decideSessionModelResolution.
    `resolved`, `plan`, `decision` all fall out of this; renaming them keeps
    the gate green, and inlining the resolution into the `if` keeps it green
    too (the guard is then the call itself).
    """
    names = set()
    for m in _ASSIGN_RE.finditer(code):
        rhs = m.group(2)
        if any(src in rhs for src in _RESOLVE_SOURCES):
            names.add(m.group(1))
    return names


def _logical_header(lines, i):
    """The statement that opened the block on line `i` — a condition may be
    wrapped, so walk back until the parentheses balance."""
    seg = lines[i]
    opens = seg.count("(")
    closes = seg.count(")")
    j = i - 1
    while closes > opens and j >= 0 and i - j <= 6:
        seg = lines[j] + "\n" + seg
        opens += lines[j].count("(")
        closes += lines[j].count(")")
        j -= 1
    return seg


def _guarded_by_resolution(code, write_pos, fun_start, names):
    """True when the write may only run after a resolution said yes.

    Indentation-based rather than brace-matched on purpose: these files carry
    `\"\"\"{\"type\":\"group\",...}\"\"\"` literals whose braces would corrupt a
    depth count, while the formatter's indentation is reliable.
    """
    lines = code.split("\n")
    line_no = code.count("\n", 0, write_pos)
    line_start = code.rfind("\n", 0, write_pos) + 1
    own_line = code[line_start:write_pos]
    candidates = [own_line]
    indent = len(lines[line_no]) - len(lines[line_no].lstrip())
    i = line_no - 1
    while i >= 0 and code[:line_start].count("\n") >= 0:
        ln = lines[i]
        if ln.strip():
            ind = len(ln) - len(ln.lstrip())
            if ind < indent and ln.rstrip().endswith("{"):
                candidates.append(_logical_header(lines, i))
                indent = ind
        i -= 1
        if indent <= 0:
            break
    for cand in candidates:
        if "if" not in cand:
            continue
        if FUNNEL_USE in cand:
            return True
        for name in names:
            if re.search(r"\b%s\b" % re.escape(name), cand):
                return True
    return False


def _traces_to_active(code, name, hops=3):
    """Does `name` carry the entry id that actually got resolved?"""
    seen = set()

    def rec(n, h):
        if h <= 0 or n in seen:
            return False
        seen.add(n)
        m = re.search(r"\bval\s+%s\b[^=\n]*=" % re.escape(n), code)
        if not m:
            return False
        stmt = code[m.end():m.end() + 400].split("\n\n")[0]
        if ACTIVE_ENTRY in stmt:
            return True
        for cand in set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b", stmt)):
            if cand in ("val", "let", "null", "true", "false", "return", "it", "is"):
                continue
            if rec(cand, h - 1):
                return True
        return False

    return rec(name, hops)


def _usable_filters(texts):
    """Names of the functions that narrow a List<ModelEntry> through the funnel."""
    names = set()
    for text in texts.values():
        for m in re.finditer(r"\bfun\s+[^\n(]*?([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", text):
            if "List<ModelEntry>" not in m.group(2):
                continue
            body = text[m.end():m.end() + 1500]
            if FUNNEL_CALL in body:
                names.add(m.group(1))
    return names


def _is_resolver(header):
    """True when the enclosing function IS the resolution.

    `resolveProviderFromGroup` publishes the group name on its own success
    path — by the time that line runs it has already established that a member
    can serve, so requiring it to be guarded by a resolution would be
    circular. Same for the funnel itself.
    """
    return RESOLVE_CALL.rstrip("(") in header or FUNNEL_CALL.rstrip("(") in header


def check_group_selection(texts):
    """C-SEL-1/2/3 across the chat layer. Returns a list of violations."""
    v = []
    filters = _usable_filters(texts)
    # the filter names live in whichever file declares them, so resolve them
    # against the whole layer, not per file
    all_code = {rel: _strip_line_comments(t) for rel, t in texts.items()}
    names = set()
    for code in all_code.values():
        names |= _resolution_names(code)

    for rel, code in sorted(all_code.items()):
        # ── C-SEL-1: a group id may only be published once it resolved ─────
        for m in _WRITE_RE.finditer(code):
            rhs = m.group(1).strip()
            if rhs.startswith(_EMPTY_VALUES):
                # clearing the selection is the inverse operation — no gate.
                continue
            span = _fun_span(code, m.start())
            if span is None:
                v.append("%s group write outside any function (ROT?)" % rel)
                continue
            fun_start, _fun_end = span
            header = code[fun_start:code.find("\n", fun_start)]
            if _is_resolver(header):
                continue
            if not _guarded_by_resolution(code, m.start(), fun_start, names):
                v.append(
                    "%s: `%s` publishes a group that nothing established as "
                    "servable — it must sit inside the branch that a "
                    "resolution opened (C-SEL-1)" % (rel, m.group(0).strip()))

        # ── C-SEL-2: one id per call — the persisted id is the resolved one ─
        for m in _LAST_ENTRY_RE.finditer(code):
            name = m.group(1)
            span = _fun_span(code, m.start())
            if span is None:
                continue
            fun_start, fun_end = span
            body = code[fun_start:fun_end]
            if ACTIVE_ENTRY not in body or name.startswith(ACTIVE_ENTRY):
                continue
            if not _traces_to_active(code, name):
                v.append(
                    "%s: persisted `lastEntryId` uses `%s`, which does not come "
                    "from %s — the same call must write one id (C-SEL-2)"
                    % (rel, name, ACTIVE_ENTRY))

        # ── C-SEL-3: single decision point ─────────────────────────────────
        if rel.endswith("ChatModelRouting.kt"):
            sm = re.search(r"\bfun\s+ChatViewModel\.selectEntry\s*\(", code)
            if sm is None:
                v.append("%s: selectEntry not found (anchors drifted)" % rel)
            else:
                span = _fun_span(code, sm.start())
                body = code[span[0]:span[1]]
                if FUNNEL_CALL not in body:
                    v.append(
                        "%s: selectEntry does not route through %s — a "
                        "DISABLED instance carrying a key is activated anyway "
                        "(C-SEL-3)" % (rel, FUNNEL_CALL.rstrip("(")))

        for m in re.finditer(re.escape(MEMBERS_CALL), code):
            pre = code[max(0, m.start() - 80):m.start()]
            if not any((f + "(") in pre for f in filters):
                v.append(
                    "%s: `%s` hands the router members that were never checked "
                    "for a credential — a key-less head member sinks the whole "
                    "group (C-SEL-3)" % (rel, MEMBERS_CALL.rstrip("(")))
    return v


# ── selftest fixtures ────────────────────────────────────────────────────────

CSEL_ROUTING_GOOD = '''fun ChatViewModel.selectGroup(groupId: String) {
    groupRouter.clearHealth()
    val resolved = resolveProviderFromGroup(groupId)
    if (resolved) {
        _selectedGroupId.value = groupId
        _selectedGroupName.value = providerRepository.group(groupId)?.name
        pendingEntryOverride = null
    }
}

fun ChatViewModel.selectGroupEntry(groupId: String, entryId: String) {
    groupRouter.clearHealth()
    val resolved = resolveProviderFromGroup(groupId, entryId)
    val plan = planGroupSelect(resolved = resolved, resolvedId = _activeEntryId.value, requestedId = entryId)
    if (plan.applyUi) {
        val resolvedEntryId = plan.effectiveEntryId ?: return
        _selectedGroupId.value = groupId
        _selectedGroupName.value = providerRepository.group(groupId)?.name
        persistBinding(""{"type":"group","groupId":"$groupId","lastEntryId":"$resolvedEntryId"}"")
        providerRepository.lastUsedEntryId = resolvedEntryId
    }
}

fun ChatViewModel.selectEntry(entryId: String) {
    val entry = config.modelEntries.find { it.id == entryId } ?: return
    val instance = providerRepository.instance(entry.providerInstanceId)
    val apiKey = instance?.let { providerRepository.loadApiKey(it.id) }
    val decision = decideSessionModelResolution(entry, instance, apiKey)
    if (decision !is SessionModelResolution.Use) return
    _selectedGroupId.value = null
    _selectedGroupName.value = ""
}

internal fun ChatViewModel.rotateForNewTurn(label: String) {
    val group = providerRepository.group(gid) ?: return
    val members = usableGroupMembers(providerRepository.enabledMemberEntries(group))
    val targetId = groupRouter.nextLoadBalanceMember(members)
}
'''

# ① the pre-fix shape: display state first, resolution afterwards.
CSEL_ROUTING_PRE = '''fun ChatViewModel.selectGroup(groupId: String) {
    _selectedGroupId.value = groupId
    groupRouter.clearHealth()
    val resolved = resolveProviderFromGroup(groupId)
    if (resolved) {
        pendingEntryOverride = null
    }
}

fun ChatViewModel.selectGroupEntry(groupId: String, entryId: String) {
    _selectedGroupId.value = groupId
    _selectedGroupName.value = providerRepository.group(groupId)?.name ?: ""
    groupRouter.clearHealth()
    val resolved = resolveProviderFromGroup(groupId, entryId)
    if (resolved) {
        persistBinding(""{"type":"group","groupId":"$groupId","lastEntryId":"$entryId"}"")
        _activeEntryId.value?.let { providerRepository.lastUsedEntryId = it }
    }
}

fun ChatViewModel.selectEntry(entryId: String) {
    val entry = config.modelEntries.find { it.id == entryId } ?: return
    val instance = providerRepository.instance(entry.providerInstanceId) ?: return
    val apiKey = providerRepository.loadApiKey(instance.id) ?: return
    _selectedGroupId.value = null
}

internal fun ChatViewModel.rotateForNewTurn(label: String) {
    val group = providerRepository.group(gid) ?: return
    val members = providerRepository.enabledMemberEntries(group)
    val targetId = groupRouter.nextLoadBalanceMember(members)
}
'''

CSEL_LIFECYCLE_GOOD = '''internal fun ChatViewModel.usableGroupMembers(members: List<ModelEntry>): List<ModelEntry> =
    members.filter { member ->
        val instance = providerRepository.instance(member.providerInstanceId)
        decideSessionModelResolution(member, instance, null) is SessionModelResolution.Use
    }

internal fun ChatViewModel.resolveProviderFromGroup(groupId: String): Boolean {
    val group = providerRepository.group(groupId) ?: return false
    val enabledMembers = usableGroupMembers(providerRepository.enabledMemberEntries(group))
    if (enabledMembers.isEmpty()) return false
    return true
}

internal fun ChatViewModel.restoreFromBinding(bindingJson: String?): Boolean {
    val resolved = resolveProviderFromGroup(groupId, lastEntryId)
    if (resolved) _selectedGroupId.value = groupId
    return resolved
}
'''

# ── [dv29-A2] C-SEL-4: a session-model BINDING write must make the same
# decision as the runtime gate ───────────────────────────────────────────────
#
# `session.primaryModel` (ConfigBuiltins) refuses a disabled or key-less
# instance and refuses a group with no usable member. The debug/IPC twin —
# `HeadlessChatRunner.applyModelOverride`, reached by `chat.model.select` and by
# every headless send that carries a model override — checked only
# `instance.isEnabled` on the entry branch and nothing at all on the group
# branch:
#   * a key-less instance wrote the binding, `restoreFromBinding` then refused it
#     (same funnel, different opinion), the resident VM fell back to the default
#     model, and the RPC still returned the refused model's display name — the
#     caller believes a turn ran on a model it never touched;
#   * the group branch wrote `memberEntryIds.first()` as `model_id`, which need
#     not be a member the runtime would ever pick.
# Both are the "two write entries, one decision" shape, so the judgement is
# derived, not spelled:
#   * WRITERS — functions that perform `updateSessionBinding(`;
#   * ENTRIES — functions that call a writer (so a new binding entry is covered
#     the day it is written);
#   * and each entry must reach `decideSessionModelResolution(` (in its own body
#     or within two hops) — the single decision this gate already pins elsewhere;
#   * plus a positional group-member pick (`memberEntryIds.first()` & friends) is
#     banned outright: it names a member nobody established as usable.
BINDING_WRITE_CALL = "updateSessionBinding("
FUNNEL_NAME = "decideSessionModelResolution"
RE_BARE_MEMBER_PICK = re.compile(
    r"memberEntryIds\s*(?:\.\s*(?:first|firstOrNull|last|lastOrNull|get|elementAt)\s*\("
    r"|\[\s*\d+\s*\])"
)


def _all_funs(code):
    """(name, body) for every `fun` in [code], any indentation."""
    out = []
    for m in re.finditer(r"\bfun\s+([A-Za-z_]\w*)\s*\(", code):
        span = _fun_span(code, m.start())
        if span is None:
            continue
        out.append((m.group(1), code[span[0]:span[1]]))
    return out


def _funnel_reachable(body, index, hops=2):
    if FUNNEL_NAME + "(" in body:
        return True
    if hops <= 1:
        return False
    for m in re.finditer(r"\b(\w+)\s*\(", body):
        for nested in index.get(m.group(1), ()):
            if _funnel_reachable(nested, index, hops - 1):
                return True
    return False


def check_binding_writes(texts):
    """C-SEL-4 across `debug/*.kt`. Returns a list of violations."""
    code_by_rel = {rel: _strip_line_comments(t) for rel, t in texts.items()}
    index = {}
    for code in code_by_rel.values():
        for name, body in _all_funs(code):
            index.setdefault(name, []).append(body)
    writers = {
        name
        for name, bodies in index.items()
        if any(BINDING_WRITE_CALL in body for body in bodies)
    }
    if not writers:
        return [
            "debug/*: no function performs `%s` — the binding write path cannot "
            "be judged (fail-closed)" % BINDING_WRITE_CALL.rstrip("(")
        ]
    v = []
    entries = 0
    for rel, code in sorted(code_by_rel.items()):
        for name, body in _all_funs(code):
            if name in writers:
                continue
            if not any(writer + "(" in body for writer in writers):
                continue
            entries += 1
            if not _funnel_reachable(body, index):
                v.append(
                    "%s: `%s` writes the session model binding without routing "
                    "through %s — a disabled instance or a key-less one is "
                    "persisted, the resident VM then refuses the same binding and "
                    "falls back, and the caller is told the refused model was "
                    "used (C-SEL-4)" % (rel, name, FUNNEL_NAME))
            for m in RE_BARE_MEMBER_PICK.finditer(body):
                v.append(
                    "%s: `%s` picks a group member by POSITION (`%s`) when it "
                    "writes `model_id` — that member need not be one the runtime "
                    "would ever pick; take the first USABLE member through %s "
                    "(C-SEL-4)" % (rel, name, m.group(0).strip(), FUNNEL_NAME))
    if entries == 0:
        v.append(
            "debug/*: no session-model binding entry found (nothing calls a "
            "`%s` writer) — the criterion is blind, refusing to pass "
            "(fail-closed)" % BINDING_WRITE_CALL.rstrip("("))
    return v


# ── selftest fixtures ────────────────────────────────────────────────────────

GOOD_FUNNEL = """internal fun decideSessionModelResolution(
    entry: ModelEntry,
    instance: ProviderInstance?,
    apiKey: String?,
): SessionModelResolution {
    if (instance == null) return SessionModelResolution.Unusable(UnusableModelReason.MISSING_INSTANCE)
    if (!instance.isEnabled) return SessionModelResolution.Unusable(UnusableModelReason.PROVIDER_DISABLED)
    if (apiKey == null) return SessionModelResolution.Unusable(UnusableModelReason.MISSING_API_KEY)
    return SessionModelResolution.Use(entry, instance, apiKey)
}
"""

GOOD_P2 = """// Priority 2: fall back to stored model_id
        if (!resolved) {
            val entry = findModelEntry(session.modelId)
            if (entry != null) {
                val instance = providerRepository.instance(entry.providerInstanceId)
                val apiKey = instance?.let { providerRepository.loadApiKey(it.id) }
                val decision = decideSessionModelResolution(entry, instance, apiKey)
                if (decision is SessionModelResolution.Use) {
                    currentModel = entry.model
                    _modelName.value = entry.model.displayName
                }
            }
        }

        // Priority 3: fall back to default group
"""

GOOD_ENTRY = """internal fun ChatViewModel.restoreFromBinding(bindingJson: String?): Boolean {
        return try {
            when ("entry") {
            "entry" -> {
                val entry = providerRepository.config.value.modelEntries.first()
                val instance = providerRepository.instance(entry.providerInstanceId)
                val apiKey = instance?.let { providerRepository.loadApiKey(it.id) }
                val decision = decideSessionModelResolution(entry, instance, apiKey)
                if (decision !is SessionModelResolution.Use) return false
                currentModel = entry.model
                _modelName.value = entry.model.displayName
                true
            }
            else -> false
            }
        } catch (_: Exception) { false }
}
"""

GOOD_APPLY = """internal fun ChatViewModel.applyNewChatDefaultModel(): Boolean {
    val entry = providerRepository.lastUsedVisibleEntry() ?: return false
    val instance = providerRepository.instance(entry.providerInstanceId)
    val apiKey = instance?.let { providerRepository.loadApiKey(it.id) }
    val decision = decideSessionModelResolution(entry, instance, apiKey)
    if (decision !is SessionModelResolution.Use) return false
    currentModel = entry.model
    return true
}
"""

# site writes state BEFORE the decision -> must trip the order check
BAD_P2_ORDER = """// Priority 2: fall back to stored model_id
        if (!resolved) {
            val entry = findModelEntry(session.modelId)
            if (entry != null) {
                currentModel = entry.model
                _activeEntryId.value = entry.id
                val instance = providerRepository.instance(entry.providerInstanceId)
                val apiKey = instance?.let { providerRepository.loadApiKey(it.id) }
                val decision = decideSessionModelResolution(entry, instance, apiKey)
                if (decision is SessionModelResolution.Use) {
                    _modelName.value = entry.model.displayName
                }
            }
        }

        // Priority 3: fall back to default group
"""

# entry branch bypasses the funnel entirely (the pre-fix shape) -> must trip
BAD_ENTRY_BYPASS = """internal fun ChatViewModel.restoreFromBinding(bindingJson: String?): Boolean {
        return try {
            when ("entry") {
            "entry" -> {
                val entry = providerRepository.config.value.modelEntries.first()
                val instance = providerRepository.instance(entry.providerInstanceId) ?: return false
                val apiKey = providerRepository.loadApiKey(instance.id) ?: return false
                currentModel = entry.model
                true
            }
            else -> false
            }
        } catch (_: Exception) { false }
}
"""

# funnel lost the isEnabled gate -> must trip check A
BAD_FUNNEL_NO_ENABLED = """internal fun decideSessionModelResolution(
    entry: ModelEntry,
    instance: ProviderInstance?,
    apiKey: String?,
): SessionModelResolution {
    if (instance == null) return SessionModelResolution.Unusable(UnusableModelReason.MISSING_INSTANCE)
    if (apiKey == null) return SessionModelResolution.Unusable(UnusableModelReason.MISSING_API_KEY)
    return SessionModelResolution.Use(entry, instance, apiKey)
}
"""

# [deep-audit-1044 B-04] The apply site resolved a model but never published it.
BAD_APPLY_NO_CURRENT_MODEL = GOOD_APPLY.replace(
    "    currentModel = entry.model\n", "")

# [deep-audit-1044 B-03] `ProviderFactory.create` model arguments. Eight sites
# across the three chat files when B-03 was written.
FACTORY_OK = {
    "lc": """internal fun ChatViewModel.a() {
    val effectiveModel = providerModelFor(entry)
    currentProvider = ProviderFactory.create(instance, apiKey, effectiveModel, context)
    currentProvider = ProviderFactory.create(instance, apiKey, providerModelFor(targetEntry), context)
    currentProvider = ProviderFactory.create(instance, apiKey, ModelsDevApi.effectiveModel(entry), context)
}
""",
    "rt": """internal fun ChatViewModel.b() {
    val effectiveModel = providerModelFor(targetEntry)
    currentProvider = ProviderFactory.create(instance, apiKey, effectiveModel, context)
    currentProvider = ProviderFactory.create(instance, apiKey, providerModelFor(entry), context)
    currentProvider = ProviderFactory.create(instance, apiKey, providerModelFor(entry), context)
}
""",
    "pt": """internal fun ChatViewModel.c(): LLMProvider {
    return ProviderFactory.create(instance, apiKey, providerModelFor(entry), context)
}

internal fun ChatViewModel.d(): LLMProvider {
    return ProviderFactory.create(instance, apiKey, providerModelFor(entry), context)
}
""",
}

FACTORY_RAW = dict(FACTORY_OK)
FACTORY_RAW["lc"] = FACTORY_OK["lc"].replace(
    "apiKey, effectiveModel, context", "apiKey, entry.model, context", 1)

FACTORY_UNENRICHED = dict(FACTORY_OK)
FACTORY_UNENRICHED["lc"] = FACTORY_OK["lc"].replace(
    "apiKey, effectiveModel, context", "apiKey, entry.baseModel, context", 1)


# [dv29-A2] C-SEL-4 fixtures: the debug-side binding write. Faithful to the
# post-fix production shape (`applyModelOverride` -> `persistBinding`, decision
# in the entry, usable member via the shared helper).
BW_RUNNER_GOOD = """package com.rikkaminis.app.debug

internal object HeadlessChatRunner {
    private fun persistBinding(app: MinisApp, sessionId: String, bindingJson: String, modelId: String) {
        app.chatRepository.updateSessionBinding(sessionId, bindingJson, modelId)
    }

    suspend fun applyModelOverride(app: MinisApp, sessionId: String): String? {
        val cfg = app.providerRepository.config.value
        when (target) {
            is BindingTarget.Entry -> {
                val entry = cfg.modelEntries.firstOrNull { it.id == target.entryId } ?: return null
                val instance = app.providerRepository.instance(entry.providerInstanceId)
                val decision = decideSessionModelResolution(
                    entry, instance, instance?.let { app.providerRepository.loadApiKey(it.id) })
                if (decision !is SessionModelResolution.Use) return null
                persistBinding(app, sessionId, "", entry.baseModel.id)
                entry.model.displayName
            }
            is BindingTarget.Group -> {
                val members = group.memberEntryIds.mapNotNull { mid ->
                    cfg.modelEntries.firstOrNull { it.id == mid }
                }
                val usable = firstUsableGroupEntry(members)
                persistBinding(app, sessionId, "", usable.baseModel.id)
                group.name
            }
            else -> null
        }
    }

    internal fun firstUsableGroupEntry(members: List<ModelEntry>): ModelEntry? {
        return members.firstOrNull { member ->
            val instance = instanceOf(member.providerInstanceId)
            decideSessionModelResolution(
                member, instance, instance?.let { apiKeyOf(it.id) }) is SessionModelResolution.Use
        }
    }
}
"""

# The pre-fix shape: the entry branch checks `isEnabled` only, the group branch
# takes the head member by position.
BW_RUNNER_PRE = BW_RUNNER_GOOD.replace(
    """                val decision = decideSessionModelResolution(
                    entry, instance, instance?.let { app.providerRepository.loadApiKey(it.id) })
                if (decision !is SessionModelResolution.Use) return null""",
    """                if (instance == null || !instance.isEnabled) return null""",
).replace(
    """                val members = group.memberEntryIds.mapNotNull { mid ->
                    cfg.modelEntries.firstOrNull { it.id == mid }
                }
                val usable = firstUsableGroupEntry(members)""",
    """                val usable = group.memberEntryIds.firstOrNull()""",
)

# The write moved out from under any decision: no funnel reachable at all.
BW_RUNNER_NO_FUNNEL = BW_RUNNER_GOOD.replace(
    "decideSessionModelResolution(", "someOtherResolver(").replace(
    "                member, instance, instance?.let { apiKeyOf(it.id) }) is SessionModelResolution.Use",
    "                member, instance, null) is SessionModelResolution.Use")

# No writer at all -> the criterion must fail closed instead of passing blind.
BW_RUNNER_NO_WRITER = BW_RUNNER_GOOD.replace(
    "app.chatRepository.updateSessionBinding(sessionId, bindingJson, modelId)",
    "app.chatRepository.touchSession(sessionId)")


def _fixture(funnel, p2, entry, apply):
    return funnel + "\n" + p2 + "\n" + entry + "\n" + apply + "\n"


def selftest():
    failures = []

    def expect(cond, what):
        if not cond:
            failures.append(what)

    good = check_source(_fixture(GOOD_FUNNEL, GOOD_P2, GOOD_ENTRY, GOOD_APPLY))
    expect(len(good) == 0, "compliant fixture reported %r" % (good,))

    bad_order = check_source(_fixture(GOOD_FUNNEL, BAD_P2_ORDER, GOOD_ENTRY, GOOD_APPLY))
    expect(
        any("before the decision" in x for x in bad_order),
        "order fixture produced no order violation",
    )

    bad_bypass = check_source(_fixture(GOOD_FUNNEL, GOOD_P2, BAD_ENTRY_BYPASS, GOOD_APPLY))
    expect(
        any("restoreFromBinding-entry" in x and "route through" in x for x in bad_bypass),
        "bypass fixture produced no funnel-routing violation",
    )

    bad_gate = check_source(_fixture(BAD_FUNNEL_NO_ENABLED, GOOD_P2, GOOD_ENTRY, GOOD_APPLY))
    expect(
        any("!instance.isEnabled" in x for x in bad_gate),
        "no-enabled-gate fixture produced no isEnabled violation",
    )

    bad_missing = check_source(GOOD_P2)
    expect(
        any("funnel missing" in x for x in bad_missing),
        "empty-source fixture did not fail closed",
    )

    no_current = check_source(
        _fixture(GOOD_FUNNEL, GOOD_P2, GOOD_ENTRY, BAD_APPLY_NO_CURRENT_MODEL))
    expect(
        any("applyNewChatDefaultModel" in x and "currentModel =" in x for x in no_current),
        "apply-without-currentModel fixture produced no B-04 violation",
    )

    def factory_of(name, texts, want, label, min_calls=None):
        v, _sites = check_factory_models(texts, min_calls)
        if len(v) != want:
            print("  [FAIL] factory %s: expected %d finding(s), got %d — %s"
                  % (name, want, len(v), v))
            return False
        print("  [OK]   factory %s (%s)" % (name, label))
        return True

    factory_results = [
        factory_of("enriched", FACTORY_OK, 0,
                   "every create site resolves through the single source"),
        factory_of("raw-entry", FACTORY_RAW, 1,
                   "one raw `entry.model` reds exactly that site"),
        factory_of("unenriched-ident", FACTORY_UNENRICHED, 1,
                   "an identifier not resolved from the source reds"),
        factory_of("rot", {"lc": FACTORY_OK["lc"]}, 1,
                   "too few call sites is ROT, not a silent pass"),
    ]

    print("selftest compliant tree      -> %d violation(s)" % len(good))
    print("selftest state-before-decision-> %d violation(s)" % len(bad_order))
    print("selftest bypassed entry branch-> %d violation(s)" % len(bad_bypass))
    print("selftest funnel w/o isEnabled-> %d violation(s)" % len(bad_gate))
    print("selftest funnel missing      -> %d violation(s)" % len(bad_missing))
    print("selftest apply w/o currentModel-> %d violation(s)" % len(no_current))
    print("selftest factory raw model    -> %d violation(s)"
          % len(check_factory_models(FACTORY_RAW)[0]))

    good_sel = check_group_selection(
        {ROUTING_REL: CSEL_ROUTING_GOOD, LIFECYCLE_REL: CSEL_LIFECYCLE_GOOD})
    expect(len(good_sel) == 0, "compliant group-selection fixture reported %r" % (good_sel,))

    pre_sel = check_group_selection(
        {ROUTING_REL: CSEL_ROUTING_PRE, LIFECYCLE_REL: CSEL_LIFECYCLE_GOOD})
    for crit in ("C-SEL-1", "C-SEL-2", "C-SEL-3"):
        expect(
            any(crit in x for x in pre_sel),
            "pre-fix group-selection fixture produced no %s violation" % crit,
        )
    print("selftest group selection     -> good=%d violation(s), pre=%d"
          % (len(good_sel), len(pre_sel)))

    # [dv29-A2] C-SEL-4: the debug binding write
    bw_good = check_binding_writes({DEBUG_DIR_REL + "/HeadlessChatRunner.kt": BW_RUNNER_GOOD})
    expect(len(bw_good) == 0, "C-SEL-4 compliant fixture reported %r" % (bw_good,))

    bw_pre = check_binding_writes({DEBUG_DIR_REL + "/HeadlessChatRunner.kt": BW_RUNNER_PRE})
    expect(
        any("without routing through" in x for x in bw_pre),
        "C-SEL-4 pre-fix fixture produced no funnel-routing violation",
    )
    expect(
        any("by POSITION" in x for x in bw_pre),
        "C-SEL-4 pre-fix fixture produced no positional-pick violation",
    )

    bw_no_funnel = check_binding_writes(
        {DEBUG_DIR_REL + "/HeadlessChatRunner.kt": BW_RUNNER_NO_FUNNEL})
    expect(
        any("without routing through" in x for x in bw_no_funnel),
        "C-SEL-4 no-funnel fixture produced no violation",
    )

    bw_no_writer = check_binding_writes(
        {DEBUG_DIR_REL + "/HeadlessChatRunner.kt": BW_RUNNER_NO_WRITER})
    expect(
        any("no function performs" in x for x in bw_no_writer),
        "C-SEL-4 no-writer fixture did not fail closed",
    )

    print("selftest C-SEL-4 binding write -> good=%d pre=%d nofunnel=%d nowriter=%d"
          % (len(bw_good), len(bw_pre), len(bw_no_funnel), len(bw_no_writer)))

    if failures or not all(factory_results):
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    print("SESSION_PROVIDER_ENABLED_SELFTEST=PASS")
    return 0


def main():
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        return selftest()
    root = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(root)


if __name__ == "__main__":
    sys.exit(main())

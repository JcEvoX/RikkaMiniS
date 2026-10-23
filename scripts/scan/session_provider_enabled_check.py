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
    if violations:
        for x in violations:
            print("session_provider_enabled_check: %s" % x)
        print("GATE=RED session_provider_enabled (%d violation(s))" % len(violations))
        return 1
    print("session_provider_enabled_check: %d ProviderFactory.create site(s) resolved "
          "through the enriched model" % sites)
    print("GATE=GREEN session_provider_enabled")
    return 0


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

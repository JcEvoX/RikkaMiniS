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
    return v


def run(root):
    path = os.path.join(root, LIFECYCLE_REL)
    if not os.path.isfile(path):
        print("session_provider_enabled_check: missing target %s" % path)
        return 2
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    violations = check_source(text)
    if violations:
        for x in violations:
            print("session_provider_enabled_check: %s: %s" % (LIFECYCLE_REL, x))
        print("GATE=RED session_provider_enabled (%d violation(s))" % len(violations))
        return 1
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

    print("selftest compliant tree      -> %d violation(s)" % len(good))
    print("selftest state-before-decision-> %d violation(s)" % len(bad_order))
    print("selftest bypassed entry branch-> %d violation(s)" % len(bad_bypass))
    print("selftest funnel w/o isEnabled-> %d violation(s)" % len(bad_gate))
    print("selftest funnel missing      -> %d violation(s)" % len(bad_missing))

    if failures:
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

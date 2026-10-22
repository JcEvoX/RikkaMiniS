#!/usr/bin/env python3
"""
Gate: model-entry composite-key guard + canonical id return
(deep-audit-1023 ISSUE-03, branch fix/da1023-entry-key-provider-parity).

Two config entries with the same (providerInstanceId, baseModel.id) collapse
to ONE composite id at the persistence boundary: ProviderConfigMapping writes
`idMap[entry.uuid] = compositeEntryKey(instanceId, modelId)` and the load side
restores `uuid = row.id`. Two such rows therefore share a primary key —
LazyColumn `key = entry.id` throws "Key was already used", and one of the pair
silently vanishes on the next canonicalize round-trip.

The pre-fix code guarded only API-reported entries (`if (!entry.isCustom)`);
custom entries (AddCustomModelScreen / provider.models.add) could mint the
collision. This gate pins the fixed shape:

  Arm 1  addEntry carries the unified guard (decideEntryAdd) and its
         `!entry.isCustom` branch is GONE (the guard is not type-conditional).
  Arm 2  the pure decision helpers exist (same file, JVM-testable).
  Arm 3  ModelsCollection.add returns the canonical persisted id.
  Arm 4  provider.models.add pre-checks duplicates and reports the persisted
         entry (no pre-persist uuid).
  Arm 5  provider.models.update reports the persisted entry (fail-closed on a
         rejected rename instead of claiming success).

Arm 6  ConfigRegistry.resolveField resolves a collection child WITHOUT
       counting dots — the composite id itself contains dots
       (`<instanceId>/gpt-5.5`), so a `split('.', limit = N)` boundary cuts
       every dotted id and answers `unknown_path` for all of its children
       (deep-audit-1041 A-01).
Arm 7  the boundary is delegated to the collection's own id table
       (`childIds()`) — the only authority that knows where an id ends.
Arm 8  no per-child materialisation: a field list must not be built for
       every child id on each lookup.
Arm 9  a near match is a miss: the child field is confirmed against the
       requested path before being returned.

Arms 6–9 share one anchor: the resolver region. It follows the delegation
from `ConfigRegistry.resolveField` into the pure top-level function that
owns the grammar, wherever the KDoc/comments sit (block comments are
stripped first — the old shape is allowed to appear in prose).

Usage: python3 scripts/scan/entry_composite_key_guard_check.py [root]
       python3 scripts/scan/entry_composite_key_guard_check.py --root <root>
       python3 scripts/scan/entry_composite_key_guard_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import argparse
import os
import sys
import tempfile

APP = "src/android/app/src/main/java/com/rikkaminis/app"
PR_REL = APP + "/data/repository/ProviderRepository.kt"
MC_REL = APP + "/config/collections/ModelsCollection.kt"
PM_REL = APP + "/debug/ProviderMutationMethods.kt"
REG_REL = APP + "/config/ConfigRegistry.kt"

# Arm 1 — unified guard wiring inside addEntry.
ARM1_ANCHORS = (
    "decideEntryAdd(",
    "decision.persistedId",
    "?: compositeEntryKey(",
)
# The type-conditional guard is the pre-fix regression, in its textual form.
ARM1_FORBIDDEN = ("!entry.isCustom",)

# Arm 2 — pure decision helpers must exist (top-level, same file).
ARM2_ANCHORS = (
    "internal fun decideEntryAdd(",
    "internal fun decideCompactPinMigration(",
    "internal fun decideCompactPinCleanup(",
)

# Arm 3 — ModelsCollection.add must return the persisted id, keeping the
# agent-facing AlreadyExists pre-check (the typed error surface).
ARM3_ANCHORS = (
    "return repo.addEntry(",
    "ConfigError.AlreadyExists",
)

# Arm 4 — modelsAdd RPC: duplicate pre-check + persisted-entry response.
ARM4_ANCHORS = (
    "already exists",
    "persistedId",
    "RPCException",
)

# Arm 5 — modelsUpdate RPC: canonical id, fail-closed on rejection.
ARM5_ANCHORS = (
    "repo.updateEntry(updated)",
    "persistedId",
    "?: throw RPCException",
)


def code_only(text):
    """Text with comments removed — presence checks must not be satisfiable
    from prose in a comment or a KDoc block.

    Block comments matter here: the fix's own KDoc quotes the pre-fix idiom
    (`split('.', limit = 3)`) to explain why it was wrong, and a gate that
    reads prose would red on its own documentation.

    Newlines inside removed comments are PRESERVED (replaced by a space per
    non-newline char) so every character offset still maps to the same line in
    the original text — findings report real line numbers.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "/*":
            j = text.find("*/", i + 2)
            end = n if j < 0 else j + 2
            out.append("".join("\n" if c == "\n" else " " for c in text[i:end]))
            i = end
        elif two == "//":
            j = text.find("\n", i)
            end = n if j < 0 else j
            out.append(" " * (end - i))
            i = end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _balanced(src, open_idx, opener="(", closer=")"):
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
    end = _balanced(src, brace, "{", "}")
    return None if end < 0 else src[i:end]


def kotlin_functions(text):
    """Yield (name, signature, body) for every `fun` in [text].

    Handles both body forms: `{ … }` (brace-balanced) and `= <expr>` (from the
    `=` up to the first following non-blank line indented at or below the
    `fun`'s own indentation, or EOF). After the fix the two routers in
    ConfigRegistry are exactly one of each, so a matcher that only understood
    block bodies would silently drop the delegating half.
    """
    import re
    out = []
    pat = re.compile(
        r"^([ \t]*)(?:@\w+\s+)?"
        r"(?:(?:private|internal|public|protected|override|suspend|inline|open|final)\s+)*"
        r"fun\s+(?:<[^>]*>\s*)?(\w+)\s*\(", re.M)
    for m in pat.finditer(text):
        indent = len(m.group(1).expandtabs(4))
        params_end = _balanced(text, text.index("(", m.end() - 1), "(", ")")
        if params_end < 0:
            continue
        i = params_end
        while i < len(text) and text[i] in " \t":
            i += 1
        if i < len(text) and text[i] == ":":
            while i < len(text) and text[i] not in "{=":
                i += 1
        if i >= len(text):
            continue
        if text[i] == "{":
            end = _balanced(text, i, "{", "}")
            if end < 0:
                continue
            body = text[i:end]
        elif text[i] == "=":
            pos = i
            while True:
                nl = text.find("\n", pos)
                if nl < 0:
                    body = text[i:]
                    break
                j = nl + 1
                while j < len(text) and text[j] in " \t":
                    j += 1
                if j >= len(text):
                    body = text[i:]
                    break
                if text[j] == "\n":
                    pos = j
                    continue
                if len(text[nl + 1:j].expandtabs(4)) <= indent:
                    body = text[i:nl]
                    break
                pos = j
        else:
            continue
        # the signature must include the return type: callers filter on it
        out.append((m.group(2), text[m.start():i], body))
    return out


def path_router_bodies(src):
    """Comment-stripped bodies of every function that routes a dot-path to a
    `ConfigField?` (i.e. takes `path: String`, returns `ConfigField?`).

    The rule under test is a DECISION, not a spelling: whichever function owns
    "where does the child id end" must be inside this set. Renaming or
    splitting the resolver across two functions keeps the union intact; only
    deleting the decision (no such function at all) makes the set empty, which
    the caller reports as gate ROT.
    """
    code = code_only(src)
    bodies = []
    for _name, sig, body in kotlin_functions(code):
        if "path: String" in sig and "ConfigField?" in sig:
            bodies.append(code_only(body))
    return bodies


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def read(rel, root):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def check_anchors(src, rel, header, arm, anchors, what):
    if header is None:
        code = code_only(src)
        base = 0
    else:
        body = function_text(src, header)
        if body is None:
            return [(rel, 0, arm, header + " not found — gate ROT")]
        code = code_only(body)
        base = src.find(header)
    out = []
    for anchor in anchors:
        if anchor not in code:
            out.append((rel, line_of(src, base) if base >= 0 else 0, arm,
                        "%s is missing required code anchor `%s`" % (what, anchor)))
    return out


def check_absent(src, rel, header, arm, forbidden, what):
    body = function_text(src, header)
    if body is None:
        return [(rel, 0, arm, header + " not found — gate ROT")]
    code = code_only(body)
    base = src.find(header)
    out = []
    for token in forbidden:
        if token in code:
            out.append((rel, line_of(src, base), arm,
                        "%s must not be type-conditional: found `%s`" % (what, token)))
    return out


def check_resolver(src, rel):
    """Arms 6–9 — the collection-child path grammar in ConfigRegistry.

    The anchor is the resolver region: every function that takes
    `path: String` and returns `ConfigField?`. Arms 6–9 each judge one
    DECISION over the union of those bodies, so a rename or a two-function
    delegation keeps passing while the decision itself is what gets tested.
    """
    bodies = path_router_bodies(src)
    if not bodies:
        return [(rel, 0, "6",
                 "no `fun (path: String): ConfigField?` resolver found — gate ROT")]
    whole = "\n".join(bodies)
    lines = src.count("\n")
    out = []

    # Arm 6 — no dot-counting boundary. The pre-fix idiom truncated every
    # dotted child id; any `split('.', limit = N)` reintroduces it (a "fast
    # path" left in front of the id table is the realistic regression).
    import re
    dot_split = re.compile(r"split\(\s*['\"]\.['\"]\s*,\s*limit")
    for m in dot_split.finditer(whole):
        # line number from the original text (comment stripping removes
        # newlines inside block comments, so positions are not comparable);
        # 0 = found only in code we cannot map back, still a finding.
        hit = dot_split.search(src)
        out.append((rel, line_of(src, hit.start()) if hit else 0,
                    "6", "resolver splits the path by counting dots "
                         "(`%s…`) — a composite id containing '.' gets truncated"
                         % m.group(0).strip()))

    # Arm 7 — the boundary comes from the collection's own id table.
    if "childIds()" not in whole:
        out.append((rel, 0, "7",
                    "resolver never consults childIds() — the id boundary must come "
                    "from the collection's id table, not from the path text"))
    else:
        # …and it is used as a PREFIX test of the remainder (the only symmetric
        # way to find where a dotted id ends).
        if not re.search(r"startsWith\(\s*\"?\$\{?\w+\}?\.", whole):
            out.append((rel, 0, "7",
                        "resolver reads childIds() but never prefix-tests it against the "
                        "remainder (`rest.startsWith(\"$id.\")`)"))

    # Arm 8 — no per-child materialisation (building a field list for every
    # child id would make each lookup O(children × fields)).
    for m in re.finditer(r"childIds\(\)\s*\.\s*(map|flatMap|mapNotNull|associate|associateBy)",
                         whole):
        out.append((rel, 0, "8",
                    "resolver materialises fields for every child id "
                    "(`childIds().%s…`) — resolve only the matched id" % m.group(1)))

    # Arm 9 — a near match is a miss: the child's field is confirmed against
    # the requested path before being returned.
    if not re.search(r"\.path\s*==\s*path\b", whole):
        out.append((rel, 0, "9",
                    "resolver returns the first field of a child without confirming "
                    "`.path == path` — a near-match leaf would resolve as the request"))
    return out


def run_checks(root, rels=None):
    rels = rels or {"pr": PR_REL, "mc": MC_REL, "pm": PM_REL, "reg": REG_REL}
    srcs = {k: read(rel, root) for k, rel in rels.items()}
    missing = [(rel, 0, "?", "file not found — gate ROT")
               for k, rel in rels.items() if srcs[k] is None]
    if missing:
        return missing
    checks = []
    checks += check_anchors(srcs["pr"], rels["pr"], "fun addEntry(", "1",
                            ARM1_ANCHORS, "addEntry")
    checks += check_absent(srcs["pr"], rels["pr"], "fun addEntry(", "1",
                           ARM1_FORBIDDEN, "addEntry guard")
    checks += check_anchors(srcs["pr"], rels["pr"], None, "2",
                            ARM2_ANCHORS, "ProviderRepository helpers")
    checks += check_anchors(srcs["mc"], rels["mc"], "override fun add(", "3",
                            ARM3_ANCHORS, "ModelsCollection.add")
    checks += check_anchors(srcs["pm"], rels["pm"], "fun modelsAdd(", "4",
                            ARM4_ANCHORS, "modelsAdd RPC")
    checks += check_anchors(srcs["pm"], rels["pm"], "fun modelsUpdate(", "5",
                            ARM5_ANCHORS, "modelsUpdate RPC")
    checks += check_resolver(srcs["reg"], rels["reg"])
    return checks


# ----------------------------------------------------------------------
# Selftest fixtures — minimal Kotlin-shaped files exercising every arm.
# ----------------------------------------------------------------------
FIX_PR_OK = """fun addEntry(entry: ModelEntry): String = synchronized(configLock) {
    ensureConfigLoaded()
    val config = mutationSnapshot(_config.value)
    val decision = decideEntryAdd(entry.providerInstanceId, entry.baseModel.id, existing)
    if (!decision.allowed) {
        return@synchronized decision.persistedId
    }
    config.modelEntries.add(entry)
    saveConfig(config)
    _config.value.modelEntries.firstOrNull { it.id == entry.id }?.id
        ?: compositeEntryKey(entry.providerInstanceId, entry.baseModel.id)
}

internal fun decideEntryAdd(a: String, b: String, c: List<Pair<String, String>>): EntryAddDecision = TODO()

internal fun decideCompactPinMigration(p: String, o: String, n: String): String? = TODO()

internal fun decideCompactPinCleanup(p: String, r: Collection<String>): Boolean = TODO()
"""
FIX_PR_BAD_TYPECONDITIONAL = """fun addEntry(entry: ModelEntry) = synchronized(configLock) {
    ensureConfigLoaded()
    val config = mutationSnapshot(_config.value)
    if (!entry.isCustom) {
        val exists = config.modelEntries.any { it.baseModel.id == entry.baseModel.id }
        if (exists) return@synchronized
    }
    config.modelEntries.add(entry)
    saveConfig(config)
}

internal fun decideEntryAdd(a: String, b: String, c: List<Pair<String, String>>): EntryAddDecision = TODO()

internal fun decideCompactPinMigration(p: String, o: String, n: String): String? = TODO()

internal fun decideCompactPinCleanup(p: String, r: Collection<String>): Boolean = TODO()
"""

FIX_MC_OK = """override fun add(payload: ConfigValue): String {
    val duplicate = repo.config.value.modelEntries.firstOrNull { it.baseModel.id == modelIdStr }
    if (duplicate != null) {
        throw ConfigError.AlreadyExists("Model already exists")
    }
    val entry = ModelEntry(baseModel = model)
    return repo.addEntry(entry)
}
"""

FIX_MC_BAD_TEMPUUID = """override fun add(payload: ConfigValue): String {
    val entry = ModelEntry(baseModel = model)
    repo.addEntry(entry)
    return entry.id
}
"""

FIX_PM_OK = """fun modelsAdd(context: Context, params: JSONObject): JSONObject {
    val entry = ModelEntry(baseModel = model)
    if (repo.config.value.modelEntries.any { it.baseModel.id == modelId }) {
        throw RPCException(-32602, "model already exists")
    }
    val persistedId = repo.addEntry(entry)
    val persisted = repo.config.value.modelEntries.firstOrNull { it.id == persistedId } ?: entry
    return entryToJson(persisted)
}

fun modelsUpdate(context: Context, params: JSONObject): JSONObject {
    val updated = current.copy(baseModel = newModel)
    val persistedId = repo.updateEntry(updated)
        ?: throw RPCException(-32602, "Entry update rejected")
    val persisted = repo.config.value.modelEntries.firstOrNull { it.id == persistedId } ?: updated
    return entryToJson(persisted)
}
"""

FIX_PM_BAD_STALE = """fun modelsAdd(context: Context, params: JSONObject): JSONObject {
    val entry = ModelEntry(baseModel = model)
    repo.addEntry(entry)
    return entryToJson(entry)
}

fun modelsUpdate(context: Context, params: JSONObject): JSONObject {
    val updated = current.copy(baseModel = newModel)
    repo.updateEntry(updated)
    return entryToJson(updated)
}
"""

# ---- ConfigRegistry resolver fixtures (arms 6–9) ----------------------

# The fixed shape AND the documentation trap in one file: the KDoc quotes the
# pre-fix idiom, so a gate that reads comments would red on its own docs.
FIX_REG_OK = '''package com.rikkaminis.app.config

/**
 * A previous implementation used `path.split('.', limit = 3)` and truncated
 * every dotted id. Do not bring that back.
 */
class ConfigRegistry {
    fun resolveField(path: String): ConfigField? =
        resolveRegistryField(path, fields, collections)
}

internal fun resolveRegistryField(
    path: String,
    fields: Map<String, ConfigField>,
    collections: Map<String, ConfigCollection>,
): ConfigField? {
    fields[path]?.let { return it }
    val coll = collections[path.substringBefore('.', missingDelimiterValue = "")] ?: return null
    val prefix = coll.basePath + "."
    if (!path.startsWith(prefix)) return null
    val rest = path.substring(prefix.length)
    val childId = coll.childIds()
        .filter { rest.startsWith("$it.") }
        .maxByOrNull { it.length }
        ?: return null
    return coll.fields(forId = childId).firstOrNull { it.path == path }
}
'''

# The realistic regression is NOT "revert the whole function" (that trips arm 7
# too, see FIX_REG_PREFIX_SHAPE) but a dot-counting FAST PATH left in front of
# the id table — the shape a future "shortcut the common case" edit produces.
FIX_REG_DOTSPLIT = FIX_REG_OK.replace(
    '''    val childId = coll.childIds()''',
    '''    if (rest.split('.', limit = 2).size != 2) return null
    val childId = coll.childIds()''')

# The pre-fix function, verbatim in shape: dot counting and no id table, so it
# necessarily trips both arms. Kept as a combined control (the pair is the
# defect; arms 6 and 7 are its two separable halves).
FIX_REG_PREFIX_SHAPE = FIX_REG_OK.replace(
    '''    val coll = collections[path.substringBefore('.', missingDelimiterValue = "")] ?: return null
    val prefix = coll.basePath + "."
    if (!path.startsWith(prefix)) return null
    val rest = path.substring(prefix.length)
    val childId = coll.childIds()
        .filter { rest.startsWith("$it.") }
        .maxByOrNull { it.length }
        ?: return null
    return coll.fields(forId = childId).firstOrNull { it.path == path }''',
    '''    val segments = path.split('.', limit = 3)
    if (segments.size != 3) return null
    val coll = collections[segments[0]] ?: return null
    return coll.fields(forId = segments[1]).firstOrNull { it.path == path }''')

# Derived from the right-hand side only: no childIds() at all.
FIX_REG_NO_CHILDIDS = FIX_REG_OK.replace(
    '''    val childId = coll.childIds()
        .filter { rest.startsWith("$it.") }
        .maxByOrNull { it.length }
        ?: return null''',
    '''    val childId = rest.substringBeforeLast('.', "")''')

# Consults childIds() but without a prefix test (an id is `contains`ed).
FIX_REG_NO_PREFIX = FIX_REG_OK.replace(
    '''    val childId = coll.childIds()
        .filter { rest.startsWith("$it.") }
        .maxByOrNull { it.length }
        ?: return null''',
    '''    val childId = coll.childIds().firstOrNull { rest.contains(it) }
        ?: return null''')

# Investigates every child instead of only the matched one.
FIX_REG_MATERIALISE = FIX_REG_OK.replace(
    '''    return coll.fields(forId = childId).firstOrNull { it.path == path }''',
    '''    return coll.childIds()
        .mapNotNull { id -> coll.fields(forId = id).firstOrNull { it.path == path } }
        .firstOrNull()''')

# Returns the child's first field without confirming the requested leaf.
FIX_REG_NEAR_MATCH = FIX_REG_OK.replace(
    '''    return coll.fields(forId = childId).firstOrNull { it.path == path }''',
    '''    return coll.fields(forId = childId).firstOrNull()''')

# ROT: the resolver was renamed to something that no longer takes `path: String`
# returning `ConfigField?` (a signature-level loss of the decision).
FIX_REG_ROT = '''package com.rikkaminis.app.config

class ConfigRegistry {
    fun lookupAnything(selector: String): Any? = null
}
'''



def _report(checks, fixture_name, label):
    if not checks:
        print("  [OK]   %s (%s)" % (fixture_name, label))
        return True
    for rel, line, arm, msg in checks:
        print("  [FAIL] %s arm %s line %s: %s" % (fixture_name, arm, line, msg))
    return False


def selftest():
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        rels = {"pr": "pr.kt", "mc": "mc.kt", "pm": "pm.kt", "reg": "reg.kt"}

        def write(name, text):
            with open(os.path.join(tmp, rels[name]), "w", encoding="utf-8") as f:
                f.write(text)

        write("pr", FIX_PR_OK)
        write("mc", FIX_MC_OK)
        write("pm", FIX_PM_OK)
        write("reg", FIX_REG_OK)
        ok &= _report(run_checks(tmp, rels), "fixed", "all arms clean")

        # Dirty 1: type-conditional guard (the pre-fix shape).
        write("pr", FIX_PR_BAD_TYPECONDITIONAL)
        checks = run_checks(tmp, rels)
        got = [c for c in checks if c[2] == "1"]
        if not any("type-conditional" in c[3] for c in got):
            print("  [FAIL] typec-conditional fixture did not trip the forbidden check")
            ok = False
        else:
            print("  [OK]   typec-conditional fixture trips arm 1 (%d findings)" % len(got))
        write("pr", FIX_PR_OK)

        # Dirty 2: ModelsCollection returns the pre-persist uuid.
        write("mc", FIX_MC_BAD_TEMPUUID)
        checks = run_checks(tmp, rels)
        got = [c for c in checks if c[2] == "3"]
        if not got:
            print("  [FAIL] temp-uuid fixture did not trip arm 3")
            ok = False
        else:
            print("  [OK]   temp-uuid fixture trips arm 3 (%d findings)" % len(got))
        write("mc", FIX_MC_OK)

        # Dirty 3: RPC surfaces report the pre-persist copy.
        write("pm", FIX_PM_BAD_STALE)
        checks = run_checks(tmp, rels)
        got = [c for c in checks if c[2] in ("4", "5")]
        if not got:
            print("  [FAIL] stale-response fixture did not trip arms 4/5")
            ok = False
        else:
            print("  [OK]   stale-response fixture trips arms 4/5 (%d findings)" % len(got))
        write("pm", FIX_PM_OK)

        # Dirty 4–8: the resolver arms. Each fixture must red its OWN arm and
        # only that arm — a fixture that also trips a sibling proves the arms
        # are not separable (and would make the "which decision broke" answer
        # meaningless in CI).
        resolver_fixtures = [
            ("dot-split", FIX_REG_DOTSPLIT, "6", "resolver splits the path by counting dots"),
            ("no-childIds", FIX_REG_NO_CHILDIDS, "7", "never consults childIds()"),
            ("no-prefix-test", FIX_REG_NO_PREFIX, "7", "never prefix-tests"),
            ("materialised", FIX_REG_MATERIALISE, "8", "materialises fields for every child id"),
            ("near-match", FIX_REG_NEAR_MATCH, "9", "without confirming"),
        ]
        for label, text, arm, needle in resolver_fixtures:
            write("reg", text)
            checks = run_checks(tmp, rels)
            got = [c for c in checks if c[2] == arm and needle in c[3]]
            others = [c for c in checks if c not in got]
            if not got:
                print("  [FAIL] %s fixture did not trip arm %s (%r)" % (label, arm, needle))
                ok = False
            elif others:
                print("  [FAIL] %s fixture also tripped %d other arm(s): %s"
                      % (label, len(others), [c[2] for c in others]))
                ok = False
            else:
                print("  [OK]   %s fixture trips arm %s only" % (label, arm))
        write("reg", FIX_REG_OK)

        # ROT control 1: a repo without the target files must fail loudly.
        with tempfile.TemporaryDirectory() as empty:
            rot = run_checks(empty, rels)
            if not rot:
                print("  [FAIL] ROT control: missing files produced no finding")
                ok = False
            else:
                print("  [OK]   ROT control: missing files reported (%d findings)" % len(rot))

        # Combined control: the pre-fix function trips arms 6 AND 7 — the two
        # halves of one defect, each independently pinned above.
        write("reg", FIX_REG_PREFIX_SHAPE)
        checks = run_checks(tmp, rels)
        got = sorted(c[2] for c in checks)
        if got != ["6", "7"]:
            print("  [FAIL] pre-fix shape control: expected arms ['6','7'], got %s" % got)
            ok = False
        else:
            print("  [OK]   pre-fix shape control trips arms 6+7")
        write("reg", FIX_REG_OK)

        # ROT control 2 (fail-closed): a ConfigRegistry whose resolver no
        # longer exists in the `path: String → ConfigField?` shape must be
        # reported, never silently treated as "clean".
        write("reg", FIX_REG_ROT)
        checks = run_checks(tmp, rels)
        got = [c for c in checks if c[2] == "6" and "gate ROT" in c[3]]
        if not got:
            print("  [FAIL] fail-closed control: a missing resolver was not reported")
            ok = False
        else:
            print("  [OK]   fail-closed control: missing resolver reported as gate ROT")
        write("reg", FIX_REG_OK)

    print("ENTRY_COMPOSITE_KEY_SELFTEST=%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("root", nargs="?", default=None)
    ap.add_argument("--root", dest="root_opt", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    root = args.root_opt or args.root
    if not root:
        ap.print_usage(sys.stderr)
        return 2
    if not os.path.isdir(root):
        print("root not found: %s" % root, file=sys.stderr)
        return 2

    checks = run_checks(root)
    if not checks:
        print("entry_composite_key_guard_check: OK (9 arms clean)")
        return 0
    for rel, line, arm, msg in checks:
        print("%s:%s [arm %s] %s" % (rel, line, arm, msg))
    print("entry_composite_key_guard_check: %d violation(s)" % len(checks))
    return 1


if __name__ == "__main__":
    sys.exit(main())

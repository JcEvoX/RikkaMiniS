#!/usr/bin/env python3
"""
Gate: FIX-G (deep-audit-1020 ISSUE-07) — the memory_rollup tool must carry
the same memoryEnabled gate as memory_write, and the provider `add` /
`minis-config get providers` paths must carry `customUserAgent`.

Two halves (TRIAGE ISSUE-07):

  A. `memory_rollup` executor gate. AgentTools.makeAgentTools drops the
     tool from the schema when memory is off, but the executor path had no
     gate — a tool_use block that still arrives (mid-turn /memory toggle,
     replayed block) reached MemoryRollupTool.execute and WROTE
     MEMORY-ROLLUP.md. memory_write had the gate; rollup didn't (asymmetric).
     The fix funnels both executors through the pure decision
     `memoryToolDisabledMessage(memoryEnabled)` with the shared
     `MEMORY_DISABLED_TOOL_TITLE`, and the ChatViewModel wrapper passes
     `_memoryEnabled.value` (covers both dispatch sites: main loop +
     sub-agent loop).

     isWrite NOTE (triage outcome, not a defect): the rollup record keeps
     `isWrite = false`. `MemoryToolRecord.isWrite` is a TOOL-KIND
     discriminator, not a "did this change the disk" flag — it routes
     SessionMemorySheet clicks (Write detail needs `writtenContent`, which
     rollup never sets → an empty page) and labels the row
     "memory_write"/"memory_get", and keys ChatViewModel revoke matching.
     Flipping rollup to `isWrite = true` would regress the UI; a proper fix
     is a third op kind (SessionMemorySheet/MemoryDetailScreens — outside
     this fix's file set).

  B. provider fields. `ProvidersCollection.add` dropped `customUserAgent`
     from the ProviderInstance it built (write field `customUserAgent(id)`
     and the export envelope both carry it), and the
     `minis-config get providers` summary (ConfigBuiltins) omitted it. The
     fix reads it via the pure `resolveAddCustomUserAgent` (same credential
     rule as the write field, shared via `customUserWritable` /
     `customUserAgentWritable`), passes it into the instance, and adds the
     field to the summary (empty override → Null = branded default).

This gate pins PRESENCE of the wiring (a legit refactor is a deliberate
edit to this gate rather than a silent pass):

  1. ChatToolExecutors: both memory executors route through
     `memoryToolDisabledMessage(memoryEnabled)`, the rollup signature takes
     `memoryEnabled: Boolean`, and the rollup record keeps `isWrite = false`
     (write executor keeps `isWrite = true`).
  2. ChatPromptAndTools: the rollup wrapper passes `_memoryEnabled.value`.
  3. ProvidersCollection: `add` resolves the payload UA
     (`resolveAddCustomUserAgent(`) and constructs the instance with
     `customUserAgent = customUserAgent,`; the write field consults the
     shared rule (`customUserAgentWritable(inst.credentialType)`).
  4. ConfigBuiltins: the providers summary linkedMapOf carries
     `"customUserAgent" to`.

Usage: python3 scripts/scan/memory_gate_provider_fields_check.py [repo_root]
       python3 scripts/scan/memory_gate_provider_fields_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import os
import re
import sys

APP = "src/android/app/src/main/java/com/rikkaminis/app/"

EXECUTORS_REL = APP + "ui/chat/ChatToolExecutors.kt"
PROMPT_REL = APP + "ui/chat/ChatPromptAndTools.kt"
COLLECTION_REL = APP + "config/collections/ProvidersCollection.kt"
BUILTINS_REL = APP + "config/ConfigBuiltins.kt"

GATE_CALL = "memoryToolDisabledMessage(memoryEnabled)"
ROLLUP_SIG = "internal fun executeMemoryRollupTool("
WRITE_SIG = "internal fun executeMemoryWriteTool("
WRAPPER_CALL = "executeMemoryRollupTool(repo, _memoryEnabled.value)"
UA_RESOLVE = "resolveAddCustomUserAgent("
UA_CTOR = "customUserAgent = customUserAgent,"
UA_WRITER_RULE = "customUserAgentWritable(inst.credentialType)"
UA_SUMMARY = '"customUserAgent" to (inst.customUserAgent'


def strip_comments(src):
    """Remove /* */ blocks and //-to-EOL comments (quote-aware for // URLs)."""
    out = []
    i, n = 0, len(src)
    in_block = in_line = in_str = 0
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_block:
            if c == "*" and nxt == "/":
                in_block = 0
                i += 2
                continue
            i += 1
            continue
        if in_line:
            if c == "\n":
                in_line = 0
                out.append(c)
            i += 1
            continue
        if in_str:
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if c == '"':
                in_str = 0
            i += 1
            continue
        if c == "/" and nxt == "*":
            in_block = 1
            i += 2
            continue
        if c == "/" and nxt == "/":
            in_line = 1
            i += 2
            continue
        if c == '"':
            in_str = 1
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _balanced(src, open_idx, opener="{", closer="}"):
    """Index one past the matching [closer], or -1 (fail-closed)."""
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == opener:
            depth += 1
        elif src[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _let_block(region, anchor):
    """Body of the `?.let { … }` that follows [anchor] in [region], or None."""
    if region is None:
        return None
    i = region.find(anchor)
    if i < 0:
        return None
    m = re.search(r"\?\.\s*let\s*\{", region[i:])
    if not m:
        return None
    brace = i + m.end() - 1
    end = _balanced(region, brace)
    return None if end < 0 else region[brace + 1:end - 1]


def _call_text(region, anchor):
    """Text inside the parentheses of the first `anchor` call in [region]."""
    if region is None:
        return None
    i = region.find(anchor)
    if i < 0:
        return None
    open_idx = i + len(anchor) - 1
    end = _balanced(region, open_idx, "(", ")")
    return None if end < 0 else region[open_idx + 1:end - 1]


def _split_args(inner):
    """Split a call's argument text on top-level commas."""
    parts, buf, depth = [], [], 0
    for c in inner:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(c)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def segment(src, start, end):
    """Slice from start (inclusive) to end (exclusive); end None = EOF."""
    i = src.find(start)
    if i < 0:
        return None
    if end is None:
        return src[i:]
    j = src.find(end, i)
    if j < 0:
        return None
    return src[i:j]


def check(root, _read=None):
    if _read is None:
        _read = globals()["_read"]
    violations = []

    def need(rel, region, token, label):
        if region is None:
            violations.append("%s: region not found for %s" % (rel, label))
        elif token not in region:
            violations.append("%s: %s missing %r" % (rel, label, token))

    executors = strip_comments(_read(root, EXECUTORS_REL))
    write_region = segment(executors, WRITE_SIG, "internal fun executeMemoryGetTool(")
    rollup_region = segment(executors, ROLLUP_SIG, "internal fun appendSubagentTurnText(")

    need(EXECUTORS_REL, write_region, GATE_CALL, "memory_write executor gate")
    need(EXECUTORS_REL, write_region, "isWrite = true", "memory_write record isWrite")
    need(EXECUTORS_REL, rollup_region, GATE_CALL, "memory_rollup executor gate")
    need(EXECUTORS_REL, rollup_region, "memoryEnabled: Boolean", "memory_rollup signature")
    need(EXECUTORS_REL, rollup_region, "isWrite = false", "memory_rollup record isWrite (tool-kind discriminator, see gate doc)")

    # ── [wide-1024] the gate must TERMINATE the executor ────────────────────
    # `memoryToolDisabledMessage(memoryEnabled)?.let { ToolExecutionResult(…) }`
    # keeps both needles above and throws the rejection away: execution falls
    # through to MemoryRollupTool.execute and writes MEMORY-ROLLUP.md with the
    # feature switched off — ISSUE-07 verbatim, in the fix's own shape.
    for region, label in ((write_region, "memory_write executor"),
                          (rollup_region, "memory_rollup executor")):
        body = _let_block(region, GATE_CALL)
        if body is None:
            violations.append("%s: %s gate has no `?.let { … }` body — the "
                              "rejection cannot be judged (fail-closed)"
                              % (EXECUTORS_REL, label))
        elif "return" not in body:
            violations.append("%s: %s gate BUILDS the rejection but does not "
                              "`return` it — the value is discarded and the tool "
                              "runs anyway (ISSUE-07)" % (EXECUTORS_REL, label))

    prompt = strip_comments(_read(root, PROMPT_REL))
    wrapper = segment(prompt, "internal fun ChatViewModel.executeMemoryRollupTool(): ToolExecutionResult {", "internal fun ChatViewModel.updateAssistantMessage(")
    need(PROMPT_REL, wrapper, WRAPPER_CALL, "memory_rollup wrapper gate value")

    coll = strip_comments(_read(root, COLLECTION_REL))
    add_region = segment(coll, "override fun add(payload: ConfigValue): String {", "override fun remove(id: String) {")
    writer_region = segment(coll, "private fun customUserAgent(id: String): ConfigField =", "private fun apiKeyField(id: String): ConfigField =")
    rule_region = segment(coll, "internal const val CUSTOM_USER_AGENT_OAUTH_MESSAGE: String", None)

    need(COLLECTION_REL, add_region, UA_RESOLVE, "add payload UA resolution")
    need(COLLECTION_REL, add_region, UA_CTOR, "add ProviderInstance carries customUserAgent")

    # ── [wide-1024] …and the resolver must be fed the PAYLOAD ───────────────
    # `resolveAddCustomUserAgent(null, credentialType)` keeps the call, the
    # `customUserAgent = customUserAgent` wiring and the credential rule; what
    # it resolves is now a constant, so `minis-config add` silently drops the
    # field the user passed.
    call_inner = _call_text(add_region, UA_RESOLVE)
    if call_inner is None:
        violations.append("%s: `%s` call is unparseable — the UA source cannot "
                          "be judged (fail-closed)" % (COLLECTION_REL, UA_RESOLVE.strip()))
    else:
        args = _split_args(call_inner)
        first = args[0] if args else ""
        if first == "null" or not first:
            violations.append("%s: add passes `%s` as the UA source — the payload "
                              "field `customUserAgent` never reaches "
                              "resolveAddCustomUserAgent, so every `minis-config "
                              "add providers` loses it" % (COLLECTION_REL, first or "null"))
        elif 'obj["customUserAgent"]' not in first:
            bound = re.search(r"\b([A-Za-z_]\w*)\s*$", first)
            src_decl = None
            if bound:
                src_decl = re.search(
                    r"\b(?:val|var)\s+%s\b[^\n=]*=[^\n]*obj\[\"customUserAgent\"\]"
                    % re.escape(bound.group(1)), add_region or "")
            if src_decl is None:
                violations.append("%s: add resolves the UA from `%s`, which does not "
                                  "read the payload (`obj[\"customUserAgent\"]`) — the "
                                  "call survives, its input does not"
                                  % (COLLECTION_REL, first))

    need(COLLECTION_REL, writer_region, UA_WRITER_RULE, "customUserAgent write field shared rule")

    # ── [wide-1024] …and the OAuth guard must keep its POLARITY ─────────────
    # `if (customUserAgentWritable(inst.credentialType)) throw …` needles the
    # same two strings and inverts the rule: API-key instances are refused,
    # OAuth instances get their UA overwritten.
    if writer_region is not None and not re.search(
            r"if\s*\(\s*!\s*" + re.escape(UA_WRITER_RULE) + r"\s*\)", writer_region):
        violations.append("%s: the customUserAgent writer must guard with "
                          "`if (!customUserAgentWritable(inst.credentialType))` — "
                          "without the `!` the polarity is inverted (API-key "
                          "instances rejected, OAuth instances writable)"
                          % COLLECTION_REL)
    need(COLLECTION_REL, rule_region, "internal fun customUserAgentWritable(credentialType: ProviderCredential): Boolean", "shared UA predicate")

    builtins = strip_comments(_read(root, BUILTINS_REL))
    summary = segment(builtins, 'path = "providers",', 'path = "models",')
    need(BUILTINS_REL, summary, UA_SUMMARY, "providers summary customUserAgent")

    # ── [wide-1024] …and the summary must project the VALUE ─────────────────
    # `inst.customUserAgent?.let { ConfigValue.Null } ?: ConfigValue.Null`
    # matches the prefix anchor and answers null for every instance.
    projected = _let_block(summary, UA_SUMMARY)
    if projected is None:
        violations.append("%s: the providers summary customUserAgent projection "
                          "has no `?.let { … }` body (fail-closed)" % BUILTINS_REL)
    elif "ConfigValue.Str(" not in projected:
        violations.append("%s: the providers summary projects `%s` instead of the "
                          "value — `minis-config get providers` reports "
                          "customUserAgent = null for every instance that has one"
                          % (BUILTINS_REL, projected.strip() or "nothing"))

    return violations


def _read(root, rel):
    with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
        return fh.read()


# ── selftest fixtures ────────────────────────────────────────────────────────

GOOD_GATE = """internal fun executeMemoryWriteTool(
    argsJson: String,
    repo: com.rikkaminis.app.data.repository.MemoryRepository,
    memoryEnabled: Boolean,
    recordSink: (MemoryToolRecord) -> Unit,
): ToolExecutionResult {
    memoryToolDisabledMessage(memoryEnabled)?.let {
        return ToolExecutionResult(it, false, toolTitle = MEMORY_DISABLED_TOOL_TITLE)
    }
    recordSink(MemoryToolRecord(title = result.toolTitle, isWrite = true))
    return ToolExecutionResult(result.output, result.success)
}

internal fun executeMemoryGetTool(
    argsJson: String,
    repo: com.rikkaminis.app.data.repository.MemoryRepository,
    recordSink: (MemoryToolRecord) -> Unit,
): ToolExecutionResult {
    recordSink(MemoryToolRecord(title = result.toolTitle, isWrite = false))
    return ToolExecutionResult(result.output, result.success)
}

internal fun executeMemoryRollupTool(
    repo: com.rikkaminis.app.data.repository.MemoryRepository,
    memoryEnabled: Boolean,
    recordSink: (MemoryToolRecord) -> Unit,
): ToolExecutionResult {
    memoryToolDisabledMessage(memoryEnabled)?.let {
        return ToolExecutionResult(it, false, toolTitle = MEMORY_DISABLED_TOOL_TITLE)
    }
    recordSink(MemoryToolRecord(title = result.toolTitle, isWrite = false))
    return ToolExecutionResult(result.output, result.success)
}

internal fun appendSubagentTurnText(substitutions: List<String>) { }
/**
 * [fix/da1017-subagent-residue] Record a finished turn's VISIBLE text in the
"""

BAD_ROLLUP_NO_GATE = GOOD_GATE.replace(
    """internal fun executeMemoryRollupTool(
    repo: com.rikkaminis.app.data.repository.MemoryRepository,
    memoryEnabled: Boolean,
    recordSink: (MemoryToolRecord) -> Unit,
): ToolExecutionResult {
    memoryToolDisabledMessage(memoryEnabled)?.let {
        return ToolExecutionResult(it, false, toolTitle = MEMORY_DISABLED_TOOL_TITLE)
    }
""",
    """internal fun executeMemoryRollupTool(
    repo: com.rikkaminis.app.data.repository.MemoryRepository,
    recordSink: (MemoryToolRecord) -> Unit,
): ToolExecutionResult {
""",
)

BAD_ROLLUP_ISWRITE_FLIP = GOOD_GATE.replace(
    """    recordSink(MemoryToolRecord(title = result.toolTitle, isWrite = false))
    return ToolExecutionResult(result.output, result.success)
}

internal fun appendSubagentTurnText(substitutions: List<String>) { }""",
    """    recordSink(MemoryToolRecord(title = result.toolTitle, isWrite = true))
    return ToolExecutionResult(result.output, result.success)
}

internal fun appendSubagentTurnText(substitutions: List<String>) { }""",
)

GOOD_WRAPPER = """internal fun ChatViewModel.executeMemoryRollupTool(): ToolExecutionResult {
    val repo = memoryRepository ?: return ToolExecutionResult("Error: Memory not available", false)
    return com.rikkaminis.app.ui.chat.executeMemoryRollupTool(repo, _memoryEnabled.value) { record ->
        _memoryToolRecords.value = _memoryToolRecords.value + record
    }
}

internal fun ChatViewModel.updateAssistantMessage(id: String) { }
"""

BAD_WRAPPER_NO_VALUE = GOOD_WRAPPER.replace(
    "executeMemoryRollupTool(repo, _memoryEnabled.value)",
    "executeMemoryRollupTool(repo)",
)

GOOD_ADD = """override fun add(payload: ConfigValue): String {
    val azureMode = (obj["azureMode"] as? ConfigValue.Bool)?.value ?: false
    val customUserAgent = resolveAddCustomUserAgent(
        (obj["customUserAgent"] as? ConfigValue.Str)?.value,
        credentialType,
    )
    val instance = ProviderInstance(
        id = java.util.UUID.randomUUID().toString(),
        customUserAgent = customUserAgent,
    )
    repo.addInstance(instance)
    return instance.id
}

override fun remove(id: String) {
    throw ConfigError.PermissionDenied("remove is denied")
}
"""

BAD_ADD_NO_UA = GOOD_ADD.replace(
    """    val customUserAgent = resolveAddCustomUserAgent(
        (obj["customUserAgent"] as? ConfigValue.Str)?.value,
        credentialType,
    )
""",
    "",
).replace("customUserAgent = customUserAgent,\n", "")

GOOD_WRITER = """private fun customUserAgent(id: String): ConfigField =
    ClosureField(
        path = "providers.$id.customUserAgent",
        writer = { v ->
            val inst = repo.instance(id) ?: throw ConfigError.InvalidValue("Provider not found")
            if (!customUserAgentWritable(inst.credentialType)) {
                throw ConfigError.InvalidValue(CUSTOM_USER_AGENT_OAUTH_MESSAGE)
            }
            mutate(id) { it.copy(customUserAgent = s.ifEmpty { null }) }
        },
    )

private fun apiKeyField(id: String): ConfigField =
    ClosureField(path = "providers.$id.apiKey")
"""

BAD_WRITER_INLINE_RULE = GOOD_WRITER.replace(
    "if (!customUserAgentWritable(inst.credentialType)) {",
    "if (inst.credentialType != ProviderCredential.apiKey) {",
)

GOOD_RULE = """// ─── [fix/da1020-memory-gate-provider-fields] Shared UA credential rule ──
internal const val CUSTOM_USER_AGENT_OAUTH_MESSAGE: String =
    "Custom User-Agent is only supported for third-party API-key providers."

internal fun customUserAgentWritable(credentialType: ProviderCredential): Boolean =
    credentialType == ProviderCredential.apiKey
"""

BAD_RULE_NO_PREDICATE = GOOD_RULE.replace(
    """
internal fun customUserAgentWritable(credentialType: ProviderCredential): Boolean =
    credentialType == ProviderCredential.apiKey
""",
    "",
)

GOOD_SUMMARY = """r.register(
    ReadOnlyField(
        path = "providers",
        reader = {
            ConfigValue.Arr(
                instances.map { inst ->
                    ConfigValue.Obj(
                        linkedMapOf(
                            "id" to ConfigValue.Str(inst.id),
                            "azureMode" to ConfigValue.Bool(inst.azureMode),
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
        path = "models",
        reader = { ConfigValue.Arr(emptyList()) },
    )
)
"""

BAD_SUMMARY_NO_UA = GOOD_SUMMARY.replace(
    '                            "customUserAgent" to (inst.customUserAgent?.let { ConfigValue.Str(it) } ?: ConfigValue.Null),\n',
    "",
)

# ── [wide-1024] witnesses: the knives that used to walk through this gate ──
# W1 (dataflow): the rollup rejection is BUILT and discarded — no `return`.
BAD_ROLLUP_NO_RETURN = GOOD_GATE.replace(
    """    memoryToolDisabledMessage(memoryEnabled)?.let {
        return ToolExecutionResult(it, false, toolTitle = MEMORY_DISABLED_TOOL_TITLE)
    }
    recordSink(MemoryToolRecord(title = result.toolTitle, isWrite = false))""",
    """    memoryToolDisabledMessage(memoryEnabled)?.let {
        ToolExecutionResult(it, false, toolTitle = MEMORY_DISABLED_TOOL_TITLE)
    }
    recordSink(MemoryToolRecord(title = result.toolTitle, isWrite = false))""")

# W2 (dataflow): the resolver is fed a constant instead of the payload.
BAD_ADD_NULL_UA = GOOD_ADD.replace(
    '        (obj["customUserAgent"] as? ConfigValue.Str)?.value,',
    "        null,")

# W3 (dataflow): the OAuth guard keeps its two needles and loses its `!`.
BAD_WRITER_POLARITY = GOOD_WRITER.replace(
    "if (!customUserAgentWritable(inst.credentialType)) {",
    "if (customUserAgentWritable(inst.credentialType)) {")

# W4 (dataflow): the summary keeps the anchor and projects Null.
BAD_SUMMARY_NULL_VALUE = GOOD_SUMMARY.replace(
    '"customUserAgent" to (inst.customUserAgent?.let { ConfigValue.Str(it) } ?: ConfigValue.Null),',
    '"customUserAgent" to (inst.customUserAgent?.let { ConfigValue.Null } ?: ConfigValue.Null),')


def _fixture(check_fn, executors, prompt, collection, builtins):
    def fake_read(root_unused, rel):
        if rel == EXECUTORS_REL:
            return executors
        if rel == PROMPT_REL:
            return prompt
        if rel == COLLECTION_REL:
            return collection
        if rel == BUILTINS_REL:
            return builtins
        raise AssertionError("unexpected rel %s" % rel)

    return check_fn(None, _read=fake_read)


def selftest():
    good_exec, bad_no_gate, bad_flip = GOOD_GATE, BAD_ROLLUP_NO_GATE, BAD_ROLLUP_ISWRITE_FLIP
    good_prompt, bad_wrapper = GOOD_WRAPPER, BAD_WRAPPER_NO_VALUE
    good_coll = GOOD_WRITER + "\n" + GOOD_RULE + "\n" + GOOD_ADD
    bad_coll_gate = BAD_WRITER_INLINE_RULE + "\n" + GOOD_RULE + "\n" + GOOD_ADD
    bad_coll_rule = GOOD_WRITER + "\n" + BAD_RULE_NO_PREDICATE + "\n" + GOOD_ADD
    good_builtins, bad_builtins = GOOD_SUMMARY, BAD_SUMMARY_NO_UA

    good = _fixture(check, good_exec, good_prompt, good_coll, good_builtins)
    no_gate = _fixture(check, bad_no_gate, good_prompt, good_coll, good_builtins)
    flip = _fixture(check, bad_flip, good_prompt, good_coll, good_builtins)
    no_value = _fixture(check, good_exec, bad_wrapper, good_coll, good_builtins)
    bad_coll_no_ua = GOOD_WRITER + "\n" + GOOD_RULE + "\n" + BAD_ADD_NO_UA
    no_ua = _fixture(check, good_exec, good_prompt, bad_coll_no_ua, good_builtins)
    writer_inline = _fixture(check, good_exec, good_prompt, bad_coll_gate, good_builtins)
    rule_missing = _fixture(check, good_exec, good_prompt, bad_coll_rule, good_builtins)
    summary_missing = _fixture(check, good_exec, good_prompt, good_coll, bad_builtins)

    # [wide-1024] witness fixtures — each knife must trip on its own.
    w1 = _fixture(check, BAD_ROLLUP_NO_RETURN, good_prompt, good_coll, good_builtins)
    bad_coll_null_ua = GOOD_WRITER + "\n" + GOOD_RULE + "\n" + BAD_ADD_NULL_UA
    w2 = _fixture(check, good_exec, good_prompt, bad_coll_null_ua, good_builtins)
    bad_coll_polarity = BAD_WRITER_POLARITY + "\n" + GOOD_RULE + "\n" + GOOD_ADD
    w3 = _fixture(check, good_exec, good_prompt, bad_coll_polarity, good_builtins)
    w4 = _fixture(check, good_exec, good_prompt, good_coll, BAD_SUMMARY_NULL_VALUE)

    print("selftest compliant tree          -> %d violation(s)" % len(good))
    print("selftest rollup w/o gate         -> %d violation(s)" % len(no_gate))
    print("selftest rollup isWrite flipped  -> %d violation(s)" % len(flip))
    print("selftest wrapper w/o gate value  -> %d violation(s)" % len(no_value))
    print("selftest add w/o customUserAgent -> %d violation(s)" % len(no_ua))
    print("selftest writer inline rule      -> %d violation(s)" % len(writer_inline))
    print("selftest shared rule missing     -> %d violation(s)" % len(rule_missing))
    print("selftest summary w/o customUserAgent -> %d violation(s)" % len(summary_missing))
    print("selftest W rollup built-not-returned -> %d violation(s)" % len(w1))
    print("selftest W add fed null            -> %d violation(s)" % len(w2))
    print("selftest W writer polarity flipped -> %d violation(s)" % len(w3))
    print("selftest W summary projects Null   -> %d violation(s)" % len(w4))

    ok = True
    if good:
        print("SELFTEST FAIL compliant tree must be clean")
        ok = False
    if not no_gate:
        print("SELFTEST FAIL rollup gate removal must trip")
        ok = False
    if not flip:
        print("SELFTEST FAIL rollup isWrite flip must trip")
        ok = False
    if not no_value:
        print("SELFTEST FAIL wrapper gate-value removal must trip")
        ok = False
    if not no_ua:
        print("SELFTEST FAIL add UA removal must trip")
        ok = False
    if not writer_inline:
        print("SELFTEST FAIL writer inline rule must trip")
        ok = False
    if not rule_missing:
        print("SELFTEST FAIL shared rule removal must trip")
        ok = False
    if not summary_missing:
        print("SELFTEST FAIL summary UA removal must trip")
        ok = False
    # [wide-1024] the four witnessed knives must each land.
    if not w1:
        print("SELFTEST FAIL witness: rollup rejection built but not returned must trip")
        ok = False
    if not w2:
        print("SELFTEST FAIL witness: add fed `null` instead of the payload must trip")
        ok = False
    if not w3:
        print("SELFTEST FAIL witness: inverted customUserAgentWritable guard must trip")
        ok = False
    if not w4:
        print("SELFTEST FAIL witness: summary projecting ConfigValue.Null must trip")
        ok = False
    if not ok:
        for v in (good + no_gate + flip + no_value + no_ua + writer_inline + rule_missing + summary_missing + w1 + w2 + w3 + w4):
            print("  " + v)
        return 1

    print("MEMORY_GATE_PROVIDER_FIELDS_SELFTEST=PASS")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    root = args[0] if args else os.getcwd()
    violations = check(root)
    if violations:
        print("FAIL: %d violation(s):" % len(violations))
        for v in violations:
            print("  " + v)
        return 1
    print("memory_gate_provider_fields_check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())

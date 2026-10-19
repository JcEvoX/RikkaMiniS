#!/usr/bin/env python3
"""parts_json_funnel_check.py — [fix/da1019-partsjson-funnel] scan gate.

Issue: three paths bypassed the existing parts_json / reasoning_content
ceilings, so an oversized row could re-enter the database and abort the next
read with SQLiteBlobTooBigException (Issue #17, 2 MB CursorWindow):

  1. READ  - loadMessagePage's unbounded branch called the DAO directly,
             bypassing the tolerant pager's row-by-row degradation.
  2. WRITE - updateMessageParts wrote the payload verbatim (no capPartsJsonForRow).
  3. IMPORT- ConfigBackup.importChatSections constructed MessageEntity straight
             from the payload; neither column was capped.

Judgements (each must hold in the live tree):

  [R1] In every production source, `dao.loadMessagesPage(` and
       `dao.loadMessagesPageInRange(` appear ONLY inside ChatRepository.kt's
       single `loadPageSlice` funnel (1 occurrence each, inside its body).
  [R2] ChatRepository.kt has exactly 4 `loadPageTolerant(` occurrences
       (1 definition + 3 call sites) and all three paged entries route
       through it (loadMessagesInner / loadMessagePage / loadMessagePageRaw).
  [R3] loadPageRowByRow also routes through loadPageSlice (no direct DAO page).
  [R4] updateMessageParts writes through the ceiling:
       `dao.updateMessageParts(id, capPartsJsonForRow(partsJson))`.
  [R5] The reasoning ceiling is a single companion funnel
       (`capReasoningContentForRow`) used by appendMessage (no inline
       re-derivation of the byte comparison outside the companion).
  [R6] ConfigBackup.importChatSections caps both columns through the
       companion funnels before dao.insertMessage.

[da1032-B] The same file lives on the READ side of the protocol too — the
debug RPC view that mirrors `parts_json` back out to callers. Three judgments
pin that surface (each judged on the DECISION POINT, not on a call shape):

  [R7] ChatDebugMethods.messageToJson classifies every real ContentPart
       serial name — `toolUse` / `toolResult` / `mediaRef` (the @SerialName
       set in data/model/ContentPart.kt). Any one missing means those parts
       are silently dropped from `chat.messages.list` (toolCalls /
       attachments permanently empty). Comments are stripped first, so a KDoc
       that merely mentions the name does not satisfy the judgment.
  [R8] `chat.sessions.list` and `chat.sessions.get` publish ONE session
       shape: both route through the single `sessionCoreJson` mapping point,
       which carries `pinnedAt` + `lastMessagePreview`. Re-inlining either
       field in a caller is a FAIL — that is exactly how the two views drifted
       apart the first time.
  [R9] Model-reference resolution exists ONCE: the composite predicate
       (`it.baseModel.id == modelId || it.id == modelId`) lives in
       ChatDebugMethods.resolveModelDisplay, is reached from >= 2 call sites
       (incl. the mutation side), and no other copy of the bare predicate is
       re-derived in the debug package.

  --selftest  proves the gate would fail on the pre-fix shapes (negative
  fixtures inline), so an emptied or stubbed tree cannot masquerade as green.
"""
import sys
import os
import re

OK, FAIL = "PASS", "FAIL"

PARTS_RE = re.compile(r"dao\.loadMessagesPage\(")
RANGE_RE = re.compile(r"dao\.loadMessagesPageInRange\(")

# [da1032-B] read-side surface (debug RPC view of the same parts_json rows).
DEBUG_REL = "src/android/app/src/main/java/com/rikkaminis/app/debug/ChatDebugMethods.kt"
MUTATION_DEBUG_REL = "src/android/app/src/main/java/com/rikkaminis/app/debug/ChatMutationMethods.kt"

# The @SerialName set of data/model/ContentPart.kt — the only three keys that
# a real persisted parts_json row can carry besides "text".
PART_SERIAL_NAMES = ('"toolUse"', '"toolResult"', '"mediaRef"')
SESSION_PARITY_FIELDS = ('put("pinnedAt"', 'put("lastMessagePreview"')
SHARED_SESSION_MAPPER = "sessionCoreJson("
COMPOSITE_MODEL_PREDICATE = "it.id == modelId"
BARE_MODEL_PREDICATE_RE = re.compile(r"modelEntries\.firstOrNull\s*\{[^}]*baseModel\.id\s*==")


def strip_kotlin_comments(text):
    """Blank out line/block comments (blocks nest in Kotlin) and leave every
    other byte — newlines included — exactly where it was.

    Template-aware: `"${...}"` switches back to code mode (so a `//` inside a
    template expression is still a comment) and returns to the string at the
    matching `}`, so a quote inside a template cannot end the enclosing
    literal. Raw strings can hold quotes and newlines.
    """
    out = []
    i, n = 0, len(text)
    # stack of [mode, brace-depth-for-templates]
    stack = [["code", 0]]
    while i < n:
        c = text[i]
        mode = stack[-1][0]
        if mode == "string":
            if c == "\\":
                out.append(text[i:i + 2]); i += 2
            elif c == '"':
                stack.pop(); out.append(c); i += 1
            elif c == "$" and i + 1 < n and text[i + 1] == "{":
                stack.append(["template", 0]); out.append("${"); i += 2
            else:
                out.append(c); i += 1
            continue
        if mode == "raw":
            if text.startswith('"""', i):
                stack.pop(); out.append('"""'); i += 3
            else:
                out.append(c); i += 1
            continue
        if mode == "template":
            if c == "{":
                stack[-1][1] += 1; out.append(c); i += 1; continue
            if c == "}":
                if stack[-1][1] == 0:
                    stack.pop()
                else:
                    stack[-1][1] -= 1
                out.append(c); i += 1; continue
            # otherwise fall through to code handling
        # code / template-expression body
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        elif text.startswith("/*", i):
            j = i + 2
            depth = 1
            while j < n and depth:
                if text.startswith("/*", j):
                    depth += 1; j += 2
                elif text.startswith("*/", j):
                    depth -= 1; j += 2
                else:
                    j += 1
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:j])); i = j
        elif text.startswith('"""', i):
            stack.append(["raw", 0]); out.append('"""'); i += 3
        elif c == '"':
            stack.append(["string", 0]); out.append(c); i += 1
        elif c == "'":
            j = i + 1
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(text[i:j]); i = j
        else:
            out.append(c); i += 1
    return "".join(out)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def extract_function(text, sig):
    """Body of the function matching sig, via paren-then-brace matching that
    skips string literals and triple-quoted strings (a KDoc example like
    {"type":"group",…} must not hand us a brace that belongs to a comment)."""
    start = text.find(sig)
    if start < 0:
        return None
    p = text.find("(", start)
    if p < 0:
        return None
    depth = 0
    j = p
    in_str = in_triple = False
    while j < len(text):
        c = text[j]
        if in_triple:
            if text.startswith('"""', j):
                in_triple = False
                j += 2
        elif in_str:
            if c == "\\":
                j += 1
            elif c == '"':
                in_str = False
        elif text.startswith('"""', j):
            in_triple = True
            j += 2
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    i = text.find("{", j)
    if i < 0:
        return None
    depth = 0
    j = i
    in_str = in_triple = False
    while j < len(text):
        c = text[j]
        if in_triple:
            if text.startswith('"""', j):
                in_triple = False
                j += 2
        elif in_str:
            if c == "\\":
                j += 1
            elif c == '"':
                in_str = False
        elif text.startswith('"""', j):
            in_triple = True
            j += 2
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
        j += 1
    return None


def region_between(text, start_sig, end_sig_regex):
    """Text from start_sig to the next top-level-ish declaration after it.
    Line-order regions (not brace matching): expression bodies like
    `= if (...) { … } else { … }` close their first brace mid-body, so a
    brace-matched region would silently stop inside the body."""
    start = text.find(start_sig)
    if start < 0:
        return None
    m = re.search(end_sig_regex, text[start + 10:])
    if m:
        return text[start:start + 10 + m.start()]
    return text[start:]


def check_debug_surface(root):
    """[R7]-[R9] — the read-side debug RPC view of the same persisted rows.

    Returns its own findings list so `--selftest` can grade these arms on
    isolated fixtures without the [R1]-[R6] noise.
    """
    findings = []
    dbg_path = os.path.join(root, DEBUG_REL)
    mut_path = os.path.join(root, MUTATION_DEBUG_REL)
    for p in (dbg_path, mut_path):
        if not os.path.exists(p):
            return [f"[R7] FAIL missing file: {os.path.relpath(p, root)}"]
    dbg = strip_kotlin_comments(read(dbg_path))
    mut = strip_kotlin_comments(read(mut_path))

    # [R7] messageToJson must classify every real ContentPart serial name.
    body = extract_function(dbg, "fun messageToJson(")
    if body is None:
        findings.append(
            "[R7] FAIL messageToJson not found in ChatDebugMethods (fail-closed: this arm "
            "pins the parts_json read-side classification)")
    else:
        missing = [s for s in PART_SERIAL_NAMES if s not in body]
        if missing:
            findings.append(
                f"[R7] FAIL messageToJson cannot classify {', '.join(missing)} — those parts "
                "are dropped from chat.messages.list (toolCalls / attachments stay empty)")
        else:
            findings.append(
                "[R7] PASS messageToJson classifies toolUse / toolResult / mediaRef")

    # [R8] one session shape, reached from both RPCs through one mapper.
    list_body = extract_function(dbg, "fun sessionsList(")
    get_body = extract_function(dbg, "fun sessionsGet(")
    mapper = extract_function(dbg, "fun sessionCoreJson(") if SHARED_SESSION_MAPPER in dbg else None
    if list_body is None or get_body is None:
        findings.append("[R8] FAIL sessionsList / sessionsGet not found in ChatDebugMethods")
    elif mapper is None:
        findings.append(
            "[R8] FAIL no shared session mapper: chat.sessions.list and chat.sessions.get "
            "build their shapes independently and will drift again")
    else:
        routed = SHARED_SESSION_MAPPER in list_body and SHARED_SESSION_MAPPER in get_body
        carried = [f for f in SESSION_PARITY_FIELDS if f not in mapper]
        re_inlined = [f for f in SESSION_PARITY_FIELDS if f in get_body or f in list_body]
        if not routed:
            which = "sessionsList" if SHARED_SESSION_MAPPER not in list_body else "sessionsGet"
            findings.append(f"[R8] FAIL {which} does not route through {SHARED_SESSION_MAPPER[:-1]}")
        elif carried:
            findings.append(
                f"[R8] FAIL shared mapper is missing {', '.join(carried)} — the field parity "
                "this arm pins is gone")
        elif re_inlined:
            findings.append(
                f"[R8] FAIL {', '.join(re_inlined)} re-inlined in a caller alongside the shared "
                "mapper — two copies of the session shape, free to drift apart")
        else:
            findings.append(
                "[R8] PASS sessionsList + sessionsGet share one session shape "
                "(pinnedAt / lastMessagePreview single-sourced)")

    # [R9] model-reference resolution exists once.
    definition = extract_function(dbg, "fun resolveModelDisplay(")
    if definition is None:
        findings.append("[R9] FAIL resolveModelDisplay missing from ChatDebugMethods")
    elif COMPOSITE_MODEL_PREDICATE not in definition:
        findings.append(
            "[R9] FAIL resolveModelDisplay lost the composite predicate "
            "(it.id == modelId) — composite entry ids stop resolving")
    else:
        dbg_calls = len(re.findall(r"resolveModelDisplay\(", dbg)) - 1   # minus the definition
        mut_calls = len(re.findall(r"resolveModelDisplay\(", mut))
        local_copy = BARE_MODEL_PREDICATE_RE.search(mut)
        if local_copy:
            findings.append(
                "[R9] FAIL ChatMutationMethods re-derives its own modelEntries predicate "
                "— the same decision now lives in two places")
        elif mut_calls < 1 or dbg_calls + mut_calls < 2:
            findings.append(
                f"[R9] FAIL shared resolver call sites: dbg={dbg_calls} mut={mut_calls} "
                "(want >= 2 in total, incl. the mutation side)")
        else:
            findings.append(
                f"[R9] PASS single resolver reached from {dbg_calls + mut_calls} call sites")
    return findings


def find_production_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        if "/.git" in dirpath or "/test/" in dirpath or "/androidTest/" in dirpath:
            continue
        if "/src/main/" not in dirpath.replace(os.sep, "/"):
            continue
        for fn in filenames:
            if fn.endswith(".kt"):
                out.append(os.path.join(dirpath, fn))
    return out


def check(root):
    findings = []
    src = "src/android/app/src/main/java/com/rikkaminis/app"
    chatrepo_path = os.path.join(root, src, "data/repository/ChatRepository.kt")
    backup_path = os.path.join(root, src, "backup/ConfigBackup.kt")
    for p in (chatrepo_path, backup_path):
        if not os.path.exists(p):
            return FAIL, [f"missing file: {p}"]
    chatrepo = read(chatrepo_path)
    backup = read(backup_path)

    # [R1] the paged DAO shapes have exactly one call site each, inside loadPageSlice
    other_hits = []
    for path in find_production_files(root):
        t = read(path)
        if path == chatrepo_path:
            continue
        for regex in (PARTS_RE, RANGE_RE):
            for m in regex.finditer(t):
                other_hits.append(f"{os.path.relpath(path, root)}:{t[:m.start()].count(chr(10)) + 1}")
    if other_hits:
        findings.append(f"[R1] FAIL paged DAO call outside ChatRepository: {other_hits}")
    else:
        findings.append("[R1] PASS no paged DAO call outside ChatRepository")

    n_page = len(PARTS_RE.findall(chatrepo))
    n_range = len(RANGE_RE.findall(chatrepo))
    slice_region = region_between(
        chatrepo, "private suspend fun loadPageSlice(", r"\n    (private |internal )?(suspend )?fun ")
    rbr_region = region_between(
        chatrepo, "private suspend fun loadPageRowByRow(", r"\n    (private |internal )?(suspend )?fun ")
    if slice_region is None or rbr_region is None:
        findings.append("[R1] FAIL loadPageSlice/loadPageRowByRow not found in ChatRepository")
    elif n_page == 1 and n_range == 1 and PARTS_RE.search(slice_region) and RANGE_RE.search(slice_region):
        findings.append("[R1] PASS loadPageSlice is the single funnel (1 + 1 DAO calls)")
    else:
        findings.append(
            f"[R1] FAIL paged DAO calls: page={n_page} range={n_range}, "
            f"page-in-slice={bool(slice_region and PARTS_RE.search(slice_region))}, "
            f"range-in-slice={bool(slice_region and RANGE_RE.search(slice_region))}"
        )

    # [R2] tolerant pager: 1 definition + 3 call sites, all entries routed
    tolerant_count = len(re.findall(r"loadPageTolerant\(", chatrepo))
    if tolerant_count == 4:
        findings.append("[R2] PASS loadPageTolerant = 1 definition + 3 call sites")
    else:
        findings.append(f"[R2] FAIL loadPageTolerant occurrences = {tolerant_count}, expected 4")
    for site in ("loadPageTolerant(sessionId, offset, limit)", "loadPageTolerant(sessionId, offset, limit, startMs, endMs)"):
        if site in chatrepo:
            findings.append(f"[R2] PASS routed: `{site}`")
        else:
            findings.append(f"[R2] FAIL missing routing: `{site}`")

    # [R3] row-by-row fallback also funnels
    if rbr_region is None:
        findings.append("[R3] FAIL loadPageRowByRow not found")
    elif PARTS_RE.search(rbr_region) or RANGE_RE.search(rbr_region):
        findings.append("[R3] FAIL loadPageRowByRow calls the DAO directly")
    else:
        findings.append("[R3] PASS loadPageRowByRow routes through the funnel")

    # [R4] updateMessageParts writes through the ceiling
    upd_region = region_between(
        chatrepo, "suspend fun updateMessageParts(", r"\n    (private |internal )?(suspend )?fun ")
    if upd_region is None:
        findings.append("[R4] FAIL updateMessageParts not found")
    elif "dao.updateMessageParts(id, capPartsJsonForRow(partsJson))" in upd_region:
        findings.append("[R4] PASS updateMessageParts caps what reaches the DAO")
    else:
        findings.append("[R4] FAIL updateMessageParts writes the payload verbatim")

    # [R5] reasoning ceiling is a single companion funnel
    funnel = extract_function(chatrepo, "internal fun capReasoningContentForRow(")
    if funnel is None:
        findings.append("[R5] FAIL capReasoningContentForRow missing from companion")
    else:
        findings.append("[R5] PASS capReasoningContentForRow present")
    append = extract_function(chatrepo, "suspend fun appendMessage(")
    if append is None:
        findings.append("[R5] FAIL appendMessage not found")
    elif "capReasoningContentForRow(reasoningContent)" in append and \
            "reasoningContent?.let" not in append:
        findings.append("[R5] PASS appendMessage routes reasoning through the funnel")
    else:
        findings.append("[R5] FAIL appendMessage re-derives the reasoning ceiling inline")

    # [R6] import caps both columns
    ics = extract_function(backup, "internal suspend fun importChatSections(")
    if ics is None:
        findings.append("[R6] FAIL importChatSections not found in ConfigBackup")
    else:
        ok1 = 'ChatRepository.capPartsJsonForRow(m.optString("partsJson"))' in ics
        ok2 = 'ChatRepository.capReasoningContentForRow(m.optString("reasoningContent"))' in ics
        verbatim = re.search(r"partsJson = m\.optString\(\"partsJson\"\)", ics)
        if ok1 and ok2 and not verbatim:
            findings.append("[R6] PASS import caps both columns before insertMessage")
        else:
            findings.append(
                f"[R6] FAIL import wiring: partsJson={ok1} reasoning={ok2} verbatim={bool(verbatim)}"
            )

    # [da1032-B] read-side debug RPC view of the very same rows
    findings.extend(check_debug_surface(root))

    if any(f.startswith(("FAIL", "[R")) and " FAIL " in f for f in findings):
        return FAIL, findings
    return OK, findings


def build_fixture(root, chatrepo_body, configbackup_body):
    """Lay out a minimal fake tree shaped like the production sources."""
    src = os.path.join(root, "src/android/app/src/main/java/com/rikkaminis/app")
    os.makedirs(os.path.join(src, "data/repository"), exist_ok=True)
    os.makedirs(os.path.join(src, "backup"), exist_ok=True)
    with open(os.path.join(src, "data/repository/ChatRepository.kt"), "w", encoding="utf-8") as f:
        f.write(chatrepo_body)
    with open(os.path.join(src, "backup/ConfigBackup.kt"), "w", encoding="utf-8") as f:
        f.write(configbackup_body)
    return root


PREFIX_REPO = """class Repo {
    suspend fun loadMessagePage(sessionId: String, offset: Int, limit: Int,
                                maxChars: Int, startMs: Long?, endMs: Long?): List<MessageEntity> {
        val rows = if (startMs == null && endMs == null) {
            dao.loadMessagesPage(sessionId, offset, limit)
        } else {
            dao.loadMessagesPageInRange(sessionId, offset, limit, startMs, endMs)
        }
        return rows.mapNotNull { e -> extract(e) }
    }

    suspend fun updateMessageParts(id: String, partsJson: String) =
        dao.updateMessageParts(id, partsJson)

    suspend fun appendMessage(sessionId: String, partsJson: String, reasoningContent: String?): Unit {
        val capped = partsJson
        val cappedReasoning = reasoningContent?.let { rc ->
            if (rc.toByteArray(Charsets.UTF_8).size > MAX_MESSAGE_PARTS_JSON_BYTES) {
                buildTruncatedText(rc)
            } else {
                rc
            }
        }
    }
}
"""

PREFIX_IMPORT = """internal suspend fun importChatSections(
    chatRepo: ChatRepository,
    chatMessagesArr: JSONArray?,
    skipped: MutableList<String>,
): Pair<Int, Int> {
    val message = MessageEntity(
        partsJson = m.optString("partsJson"),
    )
    return 0 to 0
}
"""


def build_debug_fixture(root, debug_body, mutation_body):
    """Lay out the two debug RPC files [R7]-[R9] read."""
    debug_dir = os.path.join(root, "src/android/app/src/main/java/com/rikkaminis/app/debug")
    os.makedirs(debug_dir, exist_ok=True)
    with open(os.path.join(debug_dir, "ChatDebugMethods.kt"), "w", encoding="utf-8") as f:
        f.write(debug_body)
    with open(os.path.join(debug_dir, "ChatMutationMethods.kt"), "w", encoding="utf-8") as f:
        f.write(mutation_body)
    return root


# --- [R7]-[R9] fixtures ------------------------------------------------------
# All three judgments read the same two shipped files; the fixtures below are
# minimally-shaped stand-ins whose only job is to make one arm flip.

DEBUG_GREEN = """package com.rikkaminis.app.debug

internal object ChatDebugMethods {
    private fun provider(context: Context): ProviderRepository = TODO()

    /**
     * KDoc that names "mediaRef" and "toolUse" without dispatching on them —
     * the [R7] judgment must ignore it (comments are stripped).
     */
    suspend fun sessionsList(context: Context, params: JSONObject): JSONObject {
        val provRepo = provider(context)
        for (session in all) {
            arr.put(sessionCoreJson(session, provRepo))
        }
        return JSONObject().apply { put("count", emitted) }
    }

    suspend fun sessionsGet(context: Context, params: JSONObject): JSONObject {
        val provRepo = provider(context)
        val messageCount = repo.messageCount(sessionId)
        return sessionCoreJson(session, provRepo).apply {
            put("messageCount", messageCount)
        }
    }

    private fun sessionCoreJson(session: ChatSessionEntity, provRepo: ProviderRepository): JSONObject =
        JSONObject().apply {
            put("id", session.id)
            put("title", session.title ?: JSONObject.NULL)
            put("modelId", session.modelId)
            put("modelName", resolveModelDisplay(provRepo, session.modelId))
            put("category", session.category ?: JSONObject.NULL)
            put("pinnedAt", session.pinnedAt ?: JSONObject.NULL)
            put("lastMessagePreview", session.lastMessage ?: JSONObject.NULL)
            put("createdAt", session.createdAt)
        }

    internal fun messageToJson(m: MessageEntity, includeTools: Boolean, includeReasoning: Boolean): JSONObject {
        val obj = JSONObject()
        val parts = org.json.JSONArray(m.partsJson)
        if (parts != null) {
            obj.put("parts", parts)
            val sb = StringBuilder()
            val tools = JSONArray()
            val attachments = JSONArray()
            for (i in 0 until parts.length()) {
                val p = parts.optJSONObject(i) ?: continue
                when (p.optString("type")) {
                    "text" -> sb.append(p.optString("value", ""))
                    "toolUse", "tool_call", "tool_use" -> if (includeTools) tools.put(p)
                    "toolResult", "tool_result" -> if (includeTools) tools.put(p)
                    "mediaRef", "image", "file", "attachment" -> attachments.put(p)
                }
            }
            if (sb.isNotEmpty()) obj.put("content", sb.toString())
            if (tools.length() > 0) obj.put("toolCalls", tools)
            if (attachments.length() > 0) obj.put("attachments", attachments)
        }
        return obj
    }

    fun modelsList(context: Context, params: JSONObject): JSONObject {
        val inputs = effective.inputModalities ?: emptyList()
        return JSONObject().apply {
            put("supportsImages", "image" in inputs)
            put("kind", "text")
        }
    }

    internal fun resolveModelDisplay(repo: ProviderRepository, modelId: String): String {
        val cfg = repo.config.value
        val entry = cfg.modelEntries.firstOrNull { it.baseModel.id == modelId || it.id == modelId }
            ?: return modelId
        return entry.model.displayName
    }
}
"""

MUTATION_GREEN = """package com.rikkaminis.app.debug

internal object ChatMutationMethods {
    private fun app(context: Context): MinisApp =
        context.applicationContext as? MinisApp ?: throw RPCException(-32000, "no app")

    private fun resolveDisplay(context: Context, modelId: String): String {
        return ChatDebugMethods.resolveModelDisplay(app(context).providerRepository, modelId)
    }
}
"""

MUTATION_LOCAL_PREDICATE = """package com.rikkaminis.app.debug

internal object ChatMutationMethods {
    private fun app(context: Context): MinisApp =
        context.applicationContext as? MinisApp ?: throw RPCException(-32000, "no app")

    private fun resolveDisplay(context: Context, modelId: String): String {
        val cfg = app(context).providerRepository.config.value
        val entry = cfg.modelEntries.firstOrNull { it.baseModel.id == modelId } ?: return modelId
        return entry.model.displayName
    }
}
"""

# [R7] line-comment-only mention of a serial name the dispatch does not handle
DEBUG_LINE_COMMENT_ONLY = DEBUG_GREEN.replace(
    '                val p = parts.optJSONObject(i) ?: continue\n',
    '                // handles "mediaRef" via the attachment branch below\n'
    '                val p = parts.optJSONObject(i) ?: continue\n',
).replace('"mediaRef", "image", "file", "attachment" ->', '"image", "file", "attachment" ->')

DEBUG_BLOCK_COMMENT_ONLY = DEBUG_GREEN.replace(
    '                val p = parts.optJSONObject(i) ?: continue\n',
    '                /* "toolUse" and "toolResult" land in tools */\n'
    '                val p = parts.optJSONObject(i) ?: continue\n',
).replace('"toolUse", "tool_call", "tool_use" ->', '"tool_call", "tool_use" ->') \
    .replace('"toolResult", "tool_result" ->', '"tool_result" ->')

DEBUG_MISSING_TOOLUSE = DEBUG_GREEN.replace(
    '"toolUse", "tool_call", "tool_use" ->', '"tool_call", "tool_use" ->')

# [R8] the pre-fix shape: sessionsGet builds its own object, no shared mapper
DEBUG_SESSIONS_INLINE = """package com.rikkaminis.app.debug

internal object ChatDebugMethods {
    suspend fun sessionsList(context: Context, params: JSONObject): JSONObject {
        return JSONObject().apply { put("count", emitted) }
    }

    suspend fun sessionsGet(context: Context, params: JSONObject): JSONObject {
        return JSONObject().apply {
            put("id", session.id)
            put("title", session.title ?: JSONObject.NULL)
            put("messageCount", messageCount)
        }
    }
}
"""

# [R8] mapper exists but a caller re-inlines one of the shared fields anyway
DEBUG_REINLINED_FIELD = DEBUG_GREEN.replace(
    """        return sessionCoreJson(session, provRepo).apply {
            put("messageCount", messageCount)
        }""",
    """        return sessionCoreJson(session, provRepo).apply {
            put("pinnedAt", session.pinnedAt ?: JSONObject.NULL)
            put("messageCount", messageCount)
        }""")

# [R8] mapper exists but lost the field parity this arm pins
DEBUG_MAPPER_DROPS_FIELD = DEBUG_GREEN.replace(
    '            put("lastMessagePreview", session.lastMessage ?: JSONObject.NULL)\n', '')

# [R9] the composite arm dropped -> composite entry ids stop resolving
DEBUG_BARE_PREDICATE = DEBUG_GREEN.replace(
    'it.baseModel.id == modelId || it.id == modelId', 'it.baseModel.id == modelId')


def selftest():
    import tempfile
    import shutil
    bad = 0

    def expect(root, name, wanted, verdict_wanted):
        nonlocal bad
        verdict, findings = check(root)
        hits = [f for f in findings if any(w in f for w in wanted)]
        if verdict_wanted == "FAIL" and (verdict != FAIL or not hits):
            print(f"  SELFTEST {name}: FAIL (gate did not catch the pre-fix shape)")
            bad += 1
        elif verdict_wanted == "FAIL":
            print(f"  SELFTEST {name}: OK (gate RED: {', '.join(h.split(']')[0] + ']' for h in hits)})")
        elif verdict != verdict_wanted:
            print(f"  SELFTEST {name}: FAIL (gate verdict {verdict}, wanted {verdict_wanted})")
            bad += 1
        else:
            print(f"  SELFTEST {name}: OK (gate {verdict})")

    # negative fixture: the pre-fix tree must be RED on R1/R2/R4/R6
    tmp = tempfile.mkdtemp(prefix="funnel-neg-")
    try:
        root = build_fixture(tmp, PREFIX_REPO, PREFIX_IMPORT)
        expect(root, "pre-fix-shapes", ["[R1] FAIL", "[R2] FAIL", "[R4] FAIL", "[R6] FAIL"], "FAIL")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- [R7]-[R9]: the debug RPC read surface -------------------------------
    def debug_arm(name, debug_body, mutation_body, tag, want):
        """Grade ONE of the three read-side arms on an isolated fixture tree."""
        nonlocal bad
        tmp = tempfile.mkdtemp(prefix="debug-arm-")
        try:
            build_debug_fixture(tmp, debug_body, mutation_body)
            hits = [f for f in check_debug_surface(tmp) if f.startswith(tag)]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not hits:
            print(f"  SELFTEST {name}: FAIL ({tag} produced no finding — arm is empty)")
            bad += 1
            return
        wrong = [h for h in hits if want not in h]
        if wrong:
            print(f"  SELFTEST {name}: FAIL (wanted {want}, got {wrong[0]})")
            bad += 1
        else:
            print(f"  SELFTEST {name}: OK ({hits[0].split(']')[1].strip()[:60]})")

    # [R7] the shipped classification is green; a KDoc mention alone is not
    debug_arm("r7-real-serial-names", DEBUG_GREEN, MUTATION_GREEN, "[R7]", "PASS")
    debug_arm("r7-missing-toolUse", DEBUG_MISSING_TOOLUSE, MUTATION_GREEN, "[R7]", "FAIL")
    debug_arm("r7-name-only-in-line-comment", DEBUG_LINE_COMMENT_ONLY, MUTATION_GREEN, "[R7]", "FAIL")
    debug_arm("r7-name-only-in-block-comment", DEBUG_BLOCK_COMMENT_ONLY, MUTATION_GREEN, "[R7]", "FAIL")

    # [R8] one shape, one mapper, no re-inlined field
    debug_arm("r8-shared-mapper", DEBUG_GREEN, MUTATION_GREEN, "[R8]", "PASS")
    debug_arm("r8-sessionsGet-inline-copy", DEBUG_SESSIONS_INLINE, MUTATION_GREEN, "[R8]", "FAIL")
    debug_arm("r8-caller-re-inlines-pinnedAt", DEBUG_REINLINED_FIELD, MUTATION_GREEN, "[R8]", "FAIL")
    debug_arm("r8-mapper-drops-field", DEBUG_MAPPER_DROPS_FIELD, MUTATION_GREEN, "[R8]", "FAIL")

    # [R9] one resolver, composite predicate intact, reached from both sides
    debug_arm("r9-single-resolver", DEBUG_GREEN, MUTATION_GREEN, "[R9]", "PASS")
    debug_arm("r9-mutation-local-predicate", DEBUG_GREEN, MUTATION_LOCAL_PREDICATE, "[R9]", "FAIL")
    debug_arm("r9-composite-arm-dropped", DEBUG_BARE_PREDICATE, MUTATION_GREEN, "[R9]", "FAIL")

    # the whole gate must still flip RED on the pre-fix read surface
    tmp = tempfile.mkdtemp(prefix="funnel-read-neg-")
    try:
        build_fixture(tmp, PREFIX_REPO, PREFIX_IMPORT)
        build_debug_fixture(tmp, DEBUG_GREEN.replace(
            '"toolUse", "tool_call", "tool_use" ->', '"tool_call", "tool_use" ->'),
            MUTATION_LOCAL_PREDICATE)
        verdict, findings = check(tmp)
        hit = [f for f in findings if " FAIL " in f]
        if verdict == FAIL and any(f.startswith("[R7]") for f in hit) and \
                any(f.startswith("[R9]") for f in hit):
            print("  SELFTEST read-surface-prefix-tree: OK (gate RED on R7 + R9)")
        else:
            print("  SELFTEST read-surface-prefix-tree: FAIL (gate did not catch the read side)")
            bad += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # the gate must be able to fail: an empty tree has no ChatRepository at all
    verdict, _ = check("/nonexistent-root-xyz")
    if verdict == OK:
        print("  SELFTEST empty-tree: FAIL (gate passed a nonexistent root)")
        bad += 1
    else:
        print("  SELFTEST empty-tree: OK (fails closed)")
    return 1 if bad else 0


def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:
        n = selftest()
        print("PARTSJSON_FUNNEL_SELFTEST=" + ("PASS" if n == 0 else "FAIL"))
        sys.exit(n)
    root = argv[0] if argv else "."
    verdict, findings = check(root)
    for f in findings:
        print("  " + f)
    print(f"parts_json_funnel_check: {verdict}")
    sys.exit(0 if verdict == OK else 1)


if __name__ == "__main__":
    main()

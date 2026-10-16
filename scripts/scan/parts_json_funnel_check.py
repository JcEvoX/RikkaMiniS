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

  --selftest  proves the gate would fail on the pre-fix shapes (negative
  fixtures inline), so an emptied or stubbed tree cannot masquerade as green.
"""
import sys
import os
import re

OK, FAIL = "PASS", "FAIL"

PARTS_RE = re.compile(r"dao\.loadMessagesPage\(")
RANGE_RE = re.compile(r"dao\.loadMessagesPageInRange\(")


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

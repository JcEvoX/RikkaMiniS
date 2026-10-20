#!/usr/bin/env python3
"""Guard for the two browser-history defects the store and its sheet carry.

A. (da1022-C) BrowserHistoryStore.save() must not iterate the live `entries`
   list outside its monitor.
   Why: save() did `for (entry in entries)` unlocked while record()/clear()
   mutate `entries` under synchronized(entries). save() is itself called from
   both of them right AFTER releasing the monitor, so a record() on another
   thread mid-iteration threw ConcurrentModificationException (swallowed by
   save()'s catch -> the write silently never lands) or persisted a torn view
   of the list. [fix/audit-s6m2] only pinned pruneOldLocked(); this guard
   extends the contract to save().

B. (deep-audit-v14 ISSUE-08) The history sheet must read the store once per
   refresh, and the bucket headers must come from the resources.
   Why: `remember(searchQuery, historyStore.getEntries().size)` called
   getEntries() — a full sort + copy — three times per recomposition (the key,
   the body, the Clear button's `enabled`), and a size-only key cannot see an
   equal-count replacement (a record() that also prunes one entry out of the
   7-day window leaves the count identical, so the list went stale). The bucket
   headers were English literals ("Today" / "Yesterday" / "Results") baked into
   the store and the sheet, which bypassed all 7 shipped locales.

--- View contract (each arm names the view it grades; see da1034-A2) --------

  * code view  = comments AND string literals blanked
                 -> arms A1, A2, B1 (code shapes; a comment or a log message
                    must never satisfy a shape)
  * literal view = comments blanked, string literals KEPT
                 -> arm B2 (the defect IS a literal; the code view cannot see
                    the difference between `"Today"` and `todayLabel`)
  * resource view = the res/values*/strings.xml files on disk
                 -> arm B3 (key parity across locales)

Arms:
  A1. save() contains NO iteration over the live `entries` symbol (for/forEach/
      index loop) OUTSIDE a synchronized(entries) block. Iterating a local
      snapshot is the compliant shape.
  A2. save() takes its snapshot via `synchronized(entries) { entries.toList() }`
      (loose regex: any whitespace; `toMutableList()` also accepted).
  B1. The derived `entries` map in the sheet is keyed on something that can
      tell an equal-count replacement apart (an entry identity, a refresh
      counter, or a call to a helper that reads one), and it references the
      store snapshot memo rather than reading the store in its own key.
      Fail-closed when the declaration, the memo, or both are gone.
  B2. Neither file carries an English bucket literal.
  B3. Every locale that ships the `browser_history_*` family carries all three
      bucket keys (the frozen i18n_check only WARNS about a locale that is
      missing active keys, so parity needs its own teeth).
"""
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")

STORE_REL = "src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserHistory.kt"
SHEET_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/browser/BrowserHistorySheet.kt"
RES_REL = "src/android/app/src/main/res"
BUCKET_KEYS = ("browser_history_today", "browser_history_yesterday", "browser_history_results")

MARKER = "BROWSER_HISTORY_SNAPSHOT_SELFTEST=PASS"

SNAPSHOT_RE = re.compile(
    r"synchronized\s*\(\s*entries\s*\)\s*\{\s*entries\s*\.\s*to(?:Mutable)?List\s*\(\s*\)\s*\}")
LIVE_ITER_RE = re.compile(
    r"\bfor\s*\(\s*[^)]*\bin\s+entries\s*\)|entries\s*\.\s*forEach\b|"
    r"entries\s*\[\s*\d|entries\s*\.\s*map\s*\{|entries\s*\.\s*filter\s*\{")

# A key can tell an equal-count replacement apart only if it reads an element
# identity or a refresh counter. Graded on that decision, not on a spelling:
# an inline `snapshot.firstOrNull()?.id` and `historySnapshotKey(snapshot)` are
# both accepted, the latter by resolving the helper one level down.
IDENTITY_RE = re.compile(
    r"(?i)\b[A-Za-z_][A-Za-z0-9_]*(?:tick|revision|version)[A-Za-z0-9_]*\b"
    r"|\bfirst(?:OrNull)?\s*\("
    r"|\blast(?:OrNull)?\s*\("
    r"|\bgetOrNull\s*\(\s*0\s*\)"
    r"|\.\s*id\b")

BUCKET_LITERAL_RE = re.compile(r'"(?:Today|Yesterday|Results)"')
KEY_DECL = "entries"
BUCKET_KEY_RE = re.compile(r'name="(browser_history_[A-Za-z0-9_]+)"')


def strip_comments_and_strings(text: str) -> str:
    """Blank out // comments, /* */ blocks and string literals (brace-safe)."""
    out = []
    i, n = 0, len(text)
    state = None
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if state is None:
            if two == "//":
                state = "line"; out.append("  "); i += 2; continue
            if two == "/*":
                state = "block"; out.append("  "); i += 2; continue
            if c == '"':
                state = "str"; out.append(" "); i += 1; continue
            out.append(c); i += 1; continue
        if state == "line":
            out.append("\n" if c == "\n" else " ")
            if c == "\n":
                state = None
            i += 1; continue
        if state == "block":
            if two == "*/":
                state = None; out.append("  "); i += 2; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        if state == "str":
            if c == "\\":
                out.append("  "); i += 2; continue
            if c == '"':
                state = None; out.append(" "); i += 1; continue
            out.append(" "); i += 1; continue
    return "".join(out)


def strip_comments_only(text: str) -> str:
    """Blank out comments but KEEP string literals (the literal view)."""
    out = []
    i, n = 0, len(text)
    state = None
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if state is None:
            if two == "//":
                state = "line"; out.append("  "); i += 2; continue
            if two == "/*":
                state = "block"; out.append("  "); i += 2; continue
            if c == '"':
                state = "str"; out.append(c); i += 1; continue
            out.append(c); i += 1; continue
        if state == "line":
            out.append("\n" if c == "\n" else " ")
            if c == "\n":
                state = None
            i += 1; continue
        if state == "block":
            if two == "*/":
                state = None; out.append("  "); i += 2; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        if state == "str":
            if c == "\\":
                out.append(c)
                out.append(text[i + 1:i + 2])
                i += 2
                continue
            out.append(c)
            if c == '"':
                state = None
            i += 1
            continue
    return "".join(out)


def brace_span(text: str, open_idx: int) -> tuple:
    depth, i, n = 1, open_idx + 1, len(text)
    while i < n and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return open_idx, i


def function_body(text: str, decl_pattern: str):
    m = re.search(decl_pattern, text)
    if not m:
        return ""
    brace = text.find("{", m.end())
    if brace < 0:
        return ""
    start, end = brace_span(text, brace)
    return text[start:end]


def statement_tail(text: str, pos: int, limit: int = 600) -> str:
    """Expression body of a one-liner: text from [pos] up to the end of the
    statement (a newline at zero paren/brace depth that is not a continuation)."""
    depth, i, n = 0, pos, min(len(text), pos + limit)
    while i < n and text[i] in " \t\r\n":
        i += 1
    pos = i
    while i < n:
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif c == "\n" and depth == 0:
            nxt = text[i + 1:].lstrip(" \t")
            if not nxt.startswith((".", "?", ":", "&&", "||", ")", "}")):
                break
        i += 1
    return text[pos:i]


def function_bodies(text: str) -> dict:
    """name -> body text for every `fun name(...)` in [text].

    Handles BOTH block bodies (`{ ... }`) and expression bodies
    (`= <expr>`), because the helpers this resolves are written both ways and
    a missing body would silently degrade the criterion to fail-closed red.
    """
    bodies = {}
    for m in re.finditer(r"\bfun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text):
        s, e = paren_span(text, m.end() - 1)
        rest = text[e:]
        eq = rest.find("=")
        brace = rest.find("{")
        if eq >= 0 and (brace < 0 or eq < brace):
            bodies[m.group(1)] = statement_tail(rest, eq + 1)
        elif brace >= 0:
            bs, be = brace_span(rest, brace)
            bodies[m.group(1)] = rest[bs:be]
    return bodies


def paren_span(text: str, open_idx: int) -> tuple:
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return open_idx, i + 1
        i += 1
    return open_idx, n


def remember_key(sheet: str, name: str):
    """Key text of `val <name> = remember( ... )`.

    Returns the key text, or None for a keyless `remember {`, or the string
    "MISSING" when the declaration is not found at all.
    """
    m = re.search(
        r"\bval\s+" + re.escape(name) + r"\s*(?::[^=\n]*)?(?:by|=)\s*remember\s*", sheet)
    if not m:
        return "MISSING"
    tail = sheet[m.end():]
    if not tail.startswith("("):
        return None
    s, e = paren_span(tail, 0)
    return tail[s + 1:e - 1]


def snapshot_memo(sheet: str):
    """(name, keys) of `val <name> = remember(<keys>) { ... getEntries() ... }`."""
    for m in re.finditer(
            r"\bval\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]*)?=\s*remember\s*\(", sheet):
        name = m.group(1)
        tail = sheet[m.end() - 1:]
        s, e = paren_span(tail, 0)
        keys = tail[s + 1:e - 1]
        rest = tail[e:]
        nxt = rest.lstrip()
        if not nxt.startswith("{"):
            continue
        body_start = len(tail) - len(nxt)
        bstart, bend = brace_span(tail, body_start)
        if "getEntries" in tail[bstart:bend]:
            return name, keys
    return None, None


def reads_identity(expr: str, bodies: dict, depth: int = 0) -> bool:
    """Does [expr] read an element identity / refresh counter, directly or
    through one of the file-local helpers it calls?"""
    if IDENTITY_RE.search(expr):
        return True
    if depth >= 3:
        return False
    for name in set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expr)):
        body = bodies.get(name)
        if body and reads_identity(body, bodies, depth + 1):
            return True
    return False

# ── B1, reversed (dv15-B3) ────────────────────────────────────────────────
#
# The old B1 arm asked a spelling question: "does the snapshot memo's key text
# mention a tick/revision/version identifier?" (IDENTITY_RE). The defect shape
# answered it perfectly:
#
#     var historyRefreshTick by remember { mutableStateOf(0) }
#     val allEntries = remember(historyRefreshTick) { historyStore.getEntries() }
#
# ...a panel-local counter that only this sheet's own callbacks advance, so a
# record() from another browser session (or the 7-day retention prune) never
# invalidated the read. The gate called that green: the criterion pinned the
# defect as legal, same family as `mcp_oauth_lifecycle_check` in the 1035 round.
#
# B1 now judges the DECISION instead of the spelling:
#   (1) the key must NOT name panel-local state declared in this file
#       (`var x by remember { … }` / `remember { mutableStateOf(…) }`) — such a
#       signal can only be advanced from inside the sheet, which is the defect;
#   (2) the key must trace to the store: it reads `<store>.<member>` itself, or
#       names an identifier whose declaration reads one, or names a State-typed
#       parameter / a `collectAsState()`-derived local (a State the store hands
#       in — accepted by contract, and the only branch whose owner this file
#       cannot see);
#   (3) when the trace lands on `<store>.<member>`, the store file must declare
#       that member as observable state (`StateFlow` / `MutableStateFlow` /
#       `State`), so "keyed on a store constant that can never change" cannot
#       pass either.
# Accepted spellings of the same decision (all three are exercised in the
# selftest):
#     val allEntries = remember(historyStore.revision) { historyStore.getEntries() }
#     val revision by historyStore.revision.collectAsState()
#     val allEntries = remember(revision) { historyStore.getEntries() }
# Rejected:
#     var historyRefreshTick by remember { mutableStateOf(0) }; remember(historyRefreshTick) { … }
DECL_RE = re.compile(
    r"\b(?:val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]*)?\s*(?:by|=)\s*"
    r"([^\n]*(?:\n\s*\.[^\n]*)*)")
PANEL_LOCAL_STATE_RES = (
    r"\b(?:val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]*)?\s*by\s+remember\s*\{",
    r"\b(?:val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]*)?\s*=\s*remember(?:Saveable)?\s*"
    r"\{[^}]*\bmutable[A-Za-z]*State\w*Of\b",
    r"\b(?:val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]*)?\s*(?:by|=)\s*"
    r"mutable[A-Za-z]*State\w*Of\b",
)
OBSERVABLE_STATE_RE = re.compile(
    r"\bval\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
    r"(?:MutableStateFlow|StateFlow|MutableState|State)\s*[<(]")

def store_receiver(text: str):
    """The store the sheet reads, taken from `X.getEntries()`."""
    m = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*getEntries\s*\(", text)
    return m.group(1) if m else None

def store_member_refs(text: str, receiver: str):
    """`<receiver>.<member>` names read inside [text]."""
    return set(re.findall(
        r"\b" + re.escape(receiver) + r"\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)", text))

def panel_local_state_names(sheet: str):
    """Identifiers this file declares as its own remember/mutableStateOf state."""
    names = set()
    for pat in PANEL_LOCAL_STATE_RES:
        names |= set(re.findall(pat, sheet))
    return names

def derived_state_names(sheet: str):
    """Locals bound to a flow/State read (`… collectAsState()`)."""
    names = set()
    for m in DECL_RE.finditer(sheet):
        if "collectAsState" in m.group(2):
            names.add(m.group(1))
    return names

def state_typed_params(sheet: str):
    """Parameters typed as Compose State — a signal the caller (the store
    owner) hands in."""
    names = set()
    for m in re.finditer(r"fun\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", sheet):
        tail = sheet[m.end() - 1:]
        s, e = paren_span(tail, 0)
        for pm in re.finditer(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:Mutable)?State\s*<", tail[s + 1:e - 1]):
            names.add(pm.group(1))
    return names

def observable_store_members(store: str):
    """Members the store file declares as observable state."""
    return set(OBSERVABLE_STATE_RE.findall(store))

def memo_key_problem(sheet: str, memo_keys: str, store: str):
    """None when the snapshot key is owned by store-provided state, else the
    reason it is not — see the B1 block comment for the criterion."""
    receiver = store_receiver(sheet)
    if receiver is None:
        return ("has no store to belong to (the sheet never calls `x.getEntries()`) "
                "-- re-point this criterion")
    ids = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", memo_keys))

    local = sorted(ids & panel_local_state_names(sheet))
    if local:
        return ("is panel-local state this file declares (`" + local[0] + "` via "
                "remember/mutableStateOf) -- only this sheet's own callbacks can "
                "advance it, so a record() from another session or the retention "
                "prune cannot invalidate the read")

    members = store_member_refs(memo_keys, receiver)
    via_binding = set()
    for m in DECL_RE.finditer(sheet):
        if m.group(1) in ids:
            refs = store_member_refs(m.group(2), receiver)
            if refs:
                via_binding.add(m.group(1))
                members |= refs
    direct = bool(store_member_refs(memo_keys, receiver))
    provided = ids & (derived_state_names(sheet) | state_typed_params(sheet))
    if not (direct or via_binding or provided):
        return ("does not read state the store owns -- key it on e.g. "
                "`historyStore.<revision>` or a local "
                "`val r by historyStore.<revision>.collectAsState()`")

    observable = observable_store_members(store)
    if members and not (members & observable):
        return ("reads " + ", ".join(sorted(receiver + "." + m for m in members))
                + ", but the store declares none of those as observable state "
                  "(`StateFlow`/`MutableStateFlow`/`State`) -- a key that can "
                  "never change cannot invalidate anything")
    return None


def sync_entries_spans(body: str):
    spans = []
    for m in re.finditer(r"synchronized\s*\(\s*entries\s*\)\s*\{", body):
        spans.append(brace_span(body, body.find("{", m.start())))
    return spans


def check_arms(clean: str):
    """A1/A2 — the save() snapshot contract (code view)."""
    problems = []
    save_body = function_body(clean, r"private\s+fun\s+save\s*\(")
    if not save_body:
        return ["A0: could not locate `private fun save(...)` in " + STORE_REL
                + " -- the function may have been renamed; re-point this guard"]

    # arm A1: no iteration over the live `entries` outside the monitor
    spans = sync_entries_spans(save_body)
    for m in LIVE_ITER_RE.finditer(save_body):
        if not any(s < m.start() < e for s, e in spans):
            problems.append(
                "A1: save() iterates the live `entries` list at offset "
                + str(m.start())
                + " outside synchronized(entries) -- record()/clear() mutate the "
                  "same list under the monitor, so this is a CME/torn-persist "
                  "(save() runs unlocked right after both record() and clear())")
            break

    # arm A2: locked shallow-copy snapshot present
    if not SNAPSHOT_RE.search(save_body):
        problems.append(
            "A2: save() does not take its snapshot via "
            "`synchronized(entries) { entries.toList() }` -- the lock must cover "
            "the copy (and only the copy; JSON building + file write stay outside)")

    return problems


def check_sheet_arms(sheet: str, bodies: dict, store: str = ""):
    """B1 — the derived list is memoized once per store-owned refresh signal.

    The memo key criterion is the reversed one: it must be owned by state the
    store provides, not by panel-local `remember` state that happens to be
    named `…tick`/`…revision` — see the B1 block comment above for the exact
    judgment and the spellings it accepts.
    """
    problems = []

    keys = remember_key(sheet, KEY_DECL)
    if keys == "MISSING":
        return ["B1: no `val " + KEY_DECL + " = remember(...)` in " + SHEET_REL
                + " -- the derived list was reworked; re-derive this criterion"]
    if keys is None:
        return ["B1: `val " + KEY_DECL + "` is keyed by a bare `remember { ... }` "
                "-- it can never observe the store changing"]

    memo_name, memo_keys = snapshot_memo(sheet)
    if memo_name is None:
        problems.append(
            "B1: no `val <snapshot> = remember(<store-owned refresh signal>) { "
            "historyStore.getEntries() }` -- the store is read inside a key instead of "
            "once per refresh, so every recomposition re-sorts the whole history")
    else:
        if memo_name not in keys:
            problems.append(
                "B1: the `" + KEY_DECL + "` key list (%s) does not reference the snapshot "
                "memo `%s`, so it is not derived from the memoized read" % (keys.strip(), memo_name))
        key_problem = memo_key_problem(sheet, memo_keys, store)
        if key_problem:
            problems.append(
                "B1: the snapshot memo `" + memo_name + "` is keyed on `%s`, which %s"
                % (memo_keys.strip(), key_problem))

    if not reads_identity(keys, bodies):
        problems.append(
            "B1: the `" + KEY_DECL + "` key list `" + keys.strip() + "` carries no element "
            "identity or refresh counter -- a replacement that leaves the count unchanged "
            "is invisible to this key")

    return problems


def check_bucket_literals(sheet_lit: str, store_lit: str):
    """B2 — no English bucket literal survives in either file (literal view)."""
    problems = []
    for rel, text in ((SHEET_REL, sheet_lit), (STORE_REL, store_lit)):
        for m in BUCKET_LITERAL_RE.finditer(text):
            problems.append(
                "B2: " + rel + " still hardcodes the English bucket header "
                + m.group(0) + " at offset " + str(m.start())
                + " -- bucket headers must come from stringResource (7 locales ship "
                  "the browser_history_* family)")
            break
    return problems


def locale_dirs(res_dir: Path):
    """Every values*/ dir that ships a strings.xml, with its definition set."""
    out = []
    if not res_dir.is_dir():
        return out
    for folder in sorted(res_dir.iterdir()):
        if not folder.is_dir() or not folder.name.startswith("values"):
            continue
        strings = folder / "strings.xml"
        if not strings.is_file():
            continue
        text = strings.read_text(encoding="utf-8", errors="replace")
        keys = set(BUCKET_KEY_RE.findall(text))
        out.append((folder.name, keys))
    return out


def check_locale_keys(root: Path):
    """B3 — key parity for the bucket family across every shipped locale."""
    problems = []
    dirs = locale_dirs(root / RES_REL)
    if not dirs:
        return ["B3: no res/values*/strings.xml found under " + str(root / RES_REL)
                + " -- the resource tree moved; re-point this guard"]
    carriers = [(name, keys) for name, keys in dirs if "browser_history_" in "".join(keys)]
    if not carriers:
        return ["B3: no locale defines any browser_history_* key -- family renamed?"]
    for name, keys in carriers:
        missing = [k for k in BUCKET_KEYS if k not in keys]
        if missing:
            problems.append(
                "B3: " + name + "/strings.xml is missing " + ", ".join(missing)
                + " -- the bucket headers must be translated in every locale that ships "
                  "the browser_history_* family (the frozen i18n_check only warns)")
    return problems


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

STORE_COMPLIANT = '''
class BrowserHistoryStore {
    private val entries = mutableListOf<Entry>()
    // [dv15-B3 fixture] the observable mutation counter the sheet keys on
    val revision: StateFlow<Int> = MutableStateFlow(0)
    fun record(url: String, title: String) {
        synchronized(entries) { entries.add(Entry(url = url, title = title)) }
        save()
    }
    fun groupedByDay(todayLabel: String, yesterdayLabel: String): Map<String, List<Entry>> {
        return getEntries().groupBy { entry -> browserHistoryDayLabel(entry.timestamp, 0L, todayLabel, yesterdayLabel) }
    }
    private fun save() {
        try {
            val snapshot = synchronized(entries) { entries.toList() }
            val array = JSONArray()
            for (entry in snapshot) {
                array.put(JSONObject())
            }
            file.writeText(array.toString())
        } catch (e: Exception) { }
    }
}
fun browserHistoryDayLabel(ts: Long, now: Long, todayLabel: String, yesterdayLabel: String): String {
    return if (ts == now) todayLabel else yesterdayLabel
}
'''

SHEET_COMPLIANT = '''
@Composable
fun BrowserHistorySheet(historyStore: BrowserHistoryStore, onDismiss: () -> Unit) {
    var searchQuery by remember { mutableStateOf("") }
    val todayLabel = stringResource(R.string.browser_history_today)
    val yesterdayLabel = stringResource(R.string.browser_history_yesterday)
    val resultsLabel = stringResource(R.string.browser_history_results)
    val revision by historyStore.revision.collectAsState()
    val allEntries = remember(revision) { historyStore.getEntries() }
    val entries = remember(searchQuery, todayLabel, yesterdayLabel, resultsLabel, historySnapshotKey(allEntries)) {
        if (searchQuery.isBlank()) historyStore.groupedByDay(todayLabel, yesterdayLabel)
        else mapOf(resultsLabel to historyStore.search(searchQuery))
    }
    MinisTextButton(onClick = { historyStore.clear() }, enabled = allEntries.isNotEmpty())
}
'''
# the key helper lives with the store, as it does in the real tree
STORE_KEY_HELPER = '''
internal fun historySnapshotKey(entries: List<BrowserHistoryStore.Entry>): Pair<Int, String?> =
    entries.size to entries.firstOrNull()?.id
'''

SHEET_PREFIX = '''
@Composable
fun BrowserHistorySheet(historyStore: BrowserHistoryStore, onDismiss: () -> Unit) {
    var searchQuery by remember { mutableStateOf("") }
'''

SHEET_PREFIX_FIXED = SHEET_PREFIX + '''
    val revision by historyStore.revision.collectAsState()
    val allEntries = remember(revision) { historyStore.getEntries() }
'''

# [dv15-B3] the shape the reversed B1 must reject: the memo key is a counter
# this sheet owns, so nothing the store does on its own can advance it.
SHEET_DEFECT_TICK = SHEET_PREFIX + '''
    var historyRefreshTick by remember { mutableStateOf(0) }
    val allEntries = remember(historyRefreshTick) { historyStore.getEntries() }
    val entries = remember(searchQuery, historySnapshotKey(allEntries)) {
        mapOf("x" to historyStore.search(searchQuery))
    }
}
'''

# [dv15-B3] accepted spelling: the key reads the store member directly.
SHEET_DIRECT_STORE_KEY = SHEET_PREFIX + '''
    val allEntries = remember(historyStore.revision) { historyStore.getEntries() }
    val entries = remember(searchQuery, historySnapshotKey(allEntries)) {
        mapOf("x" to historyStore.search(searchQuery))
    }
}
'''

# [dv15-B3] near-miss: the key names a store member that is NOT observable.
SHEET_NONOBSERVABLE_KEY = SHEET_PREFIX + '''
    val allEntries = remember(historyStore.entriesVersion) { historyStore.getEntries() }
    val entries = remember(searchQuery, historySnapshotKey(allEntries)) {
        mapOf("x" to historyStore.search(searchQuery))
    }
}
'''

# the store variant that pairs with SHEET_NONOBSERVABLE_KEY: same shape, but the
# counter is a plain Int, so a key reading it can never change.
STORE_PLAIN_COUNTER = STORE_COMPLIANT.replace(
    "    val revision: StateFlow<Int> = MutableStateFlow(0)",
    "    val entriesVersion: Int = 0")

# the pre-fix shapes, quoted from the audit
SHEET_PREFIX_BROKEN = SHEET_PREFIX + '''
    val entries = remember(searchQuery, historyStore.getEntries().size) {
        if (searchQuery.isBlank()) historyStore.groupedByDay()
        else mapOf("Results" to historyStore.search(searchQuery))
    }
    MinisTextButton(onClick = { historyStore.clear() }, enabled = historyStore.getEntries().isNotEmpty())
}
'''


def _selftest_store_arms():
    cases = [
        ("A1 compliant", STORE_COMPLIANT, []),
        ("A2 snapshot removed (loop over the local)",
         STORE_COMPLIANT.replace(
             "val snapshot = synchronized(entries) { entries.toList() }\n"
             "            ", ""), ["A2"]),
        ("A3 iterates the live entries list",
         STORE_COMPLIANT.replace("for (entry in snapshot) {", "for (entry in entries) {"), ["A1"]),
        ("A4 unlocked snapshot",
         STORE_COMPLIANT.replace(
             "val snapshot = synchronized(entries) { entries.toList() }",
             "val snapshot = entries.toList()"), ["A2"]),
        ("A5 compliant variant toMutableList",
         STORE_COMPLIANT.replace(
             "val snapshot = synchronized(entries) { entries.toList() }",
             "val snapshot = synchronized(entries) { entries.toMutableList() }"), []),
    ]
    ok = True
    for name, src, want in cases:
        problems = check_arms(strip_comments_and_strings(src))
        got = sorted({p.split(":")[0] for p in problems})
        mark = "ok" if got == sorted(want) else "FAIL"
        if mark == "FAIL":
            ok = False
        print("  selftest %-46s red=%-12s expect=%-12s %s"
              % (name, got or "[]", sorted(want) or "[]", mark))
        for p in problems:
            print("      - " + p)

    problems = check_arms(strip_comments_and_strings("class BrowserHistoryStore {}"))
    if not problems:
        print("  selftest A0 no-save: expected explicit red, got GREEN -- FAIL")
        ok = False
    else:
        print("  selftest %-46s red=%-12s expect=%-12s ok"
              % ("A0 no-save", sorted({p.split(":")[0] for p in problems}), "['A0']"))
    return ok


def _selftest_sheet_arms():
    store_clean = strip_comments_and_strings(STORE_COMPLIANT + STORE_KEY_HELPER)
    plain_clean = strip_comments_and_strings(STORE_PLAIN_COUNTER + STORE_KEY_HELPER)
    bodies = function_bodies(store_clean)
    cases = [
        ("B1 compliant (store-owned signal, collected)",
         SHEET_COMPLIANT, store_clean, []),
        ("B2 compliant (key reads the store member directly)",
         SHEET_DIRECT_STORE_KEY, store_clean, []),
        ("B3 compliant (inline identity read)",
         SHEET_PREFIX_FIXED + '''
    val entries = remember(searchQuery, allEntries.size, allEntries.firstOrNull()?.id) {
        mapOf("x" to historyStore.search(searchQuery))
    }
}
''', store_clean, []),
        ("B4 pre-fix shape (store read inside the key)",
         SHEET_PREFIX_BROKEN, store_clean, ["B1"]),
        ("B5 memo present, size-only key",
         SHEET_PREFIX_FIXED + '''
    val entries = remember(searchQuery, allEntries.size) {
        mapOf("x" to historyStore.search(searchQuery))
    }
}
''', store_clean, ["B1"]),
        ("B6 memo keyed on a constant (no refresh signal)",
         SHEET_PREFIX + '''
    val allEntries = remember { historyStore.getEntries() }
    val entries = remember(searchQuery, allEntries.firstOrNull()?.id) {
        mapOf("x" to historyStore.search(searchQuery))
    }
}
''', store_clean, ["B1"]),
        ("B7 keyless derived list",
         SHEET_PREFIX_FIXED + '''
    val entries = remember {
        mapOf("x" to historyStore.search(searchQuery))
    }
}
''', store_clean, ["B1"]),
        ("B8 panel-local tick as the memo key (dv15-B3 defect)",
         SHEET_DEFECT_TICK, store_clean, ["B1"]),
        ("B9 memo keyed on a non-observable store member",
         SHEET_NONOBSERVABLE_KEY, plain_clean, ["B1"]),
    ]
    ok = True
    for name, src, store_src, want in cases:
        clean = strip_comments_and_strings(src)
        problems = check_sheet_arms(clean, bodies, store_src)
        got = sorted({p.split(":")[0] for p in problems})
        mark = "ok" if got == sorted(want) else "FAIL"
        if mark == "FAIL":
            ok = False
        print("  selftest %-46s red=%-12s expect=%-12s %s"
              % (name, got or "[]", sorted(want) or "[]", mark))
        for p in problems:
            print("      - " + p)

    problems = check_sheet_arms(
        strip_comments_and_strings("fun other() {}"), bodies, store_clean)
    if not problems:
        print("  selftest B0 no-declaration: expected explicit red, got GREEN -- FAIL")
        ok = False
    else:
        print("  selftest %-46s red=%-12s expect=%-12s ok"
              % ("B0 no-declaration", sorted({p.split(":")[0] for p in problems}), "['B1']"))
    return ok


def _selftest_literals():
    ok = True
    clean_sheet = strip_comments_only('Text(stringResource(R.string.browser_history_today))\n')
    dirty_sheet = strip_comments_only('mapOf("Results" to historyStore.search(q))\n')
    clean_store = strip_comments_only('else -> "$month/$day"\n')
    dirty_store = strip_comments_only('a -> "Today"\nb -> "Yesterday"\n')
    # a folded comment mentioning the literals must NOT trip the arm
    comment_only = strip_comments_only('// legacy: "Today" / "Yesterday"\nval x = 1\n')
    cases = [
        ("B2 literals clean", check_bucket_literals(clean_sheet, clean_store), 0),
        ("B3 sheet carries \"Results\"", check_bucket_literals(dirty_sheet, clean_store), 1),
        ("B4 store carries \"Today\"/\"Yesterday\"",
         check_bucket_literals(clean_sheet, dirty_store), 1),
        ("B5 literals only inside a comment", check_bucket_literals(comment_only, clean_store), 0),
    ]
    for name, problems, want in cases:
        got = len(problems)
        mark = "ok" if got == want else "FAIL"
        if mark == "FAIL":
            ok = False
        print("  selftest %-46s red=%-12d expect=%-12d %s" % (name, got, want, mark))
        for p in problems:
            print("      - " + p)
    return ok


def _selftest_locale_keys():
    import tempfile
    ok = True
    full = "".join('<string name="%s">x</string>' % k for k in BUCKET_KEYS)
    cases = [
        ("B3 all locales complete", {"values": full, "values-de": full}, 0),
        ("B4 one locale missing a key",
         {"values": full, "values-de": full.replace("browser_history_results", "browser_history_other")}, 1),
    ]
    for name, files, want in cases:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / RES_REL
            for loc, body in files.items():
                d = base / loc
                d.mkdir(parents=True)
                (d / "strings.xml").write_text(
                    '<resources>%s<string name="browser_history_search">s</string></resources>' % body,
                    encoding="utf-8")
            problems = check_locale_keys(Path(tmp))
        got = len(problems)
        mark = "ok" if got == want else "FAIL"
        if mark == "FAIL":
            ok = False
        print("  selftest %-46s red=%-12d expect=%-12d %s" % (name, got, want, mark))
        for p in problems:
            print("      - " + p)
    with tempfile.TemporaryDirectory() as tmp:
        problems = check_locale_keys(Path(tmp))
    if not problems:
        print("  selftest B5 no-res-tree: expected explicit red, got GREEN -- FAIL")
        ok = False
    else:
        print("  selftest %-46s red=%-12d expect=%-12d ok"
              % ("B5 no-res-tree", len(problems), 1))
    return ok


def run_selftest() -> bool:
    ok = True
    ok = _selftest_store_arms() and ok
    ok = _selftest_sheet_arms() and ok
    ok = _selftest_literals() and ok
    ok = _selftest_locale_keys() and ok
    return ok


def main() -> int:
    if "--selftest" in sys.argv:
        print("browser_history_snapshot_check selftest:")
        if not run_selftest():
            print("FAIL: selftest")
            return 1
        print(MARKER)
        return 0

    problems = []
    store_path = ROOT / STORE_REL
    sheet_path = ROOT / SHEET_REL
    if not store_path.exists():
        print(f"FAIL: missing {STORE_REL}")
        return 1
    if not sheet_path.exists():
        print(f"FAIL: missing {SHEET_REL}")
        return 1

    store_raw = store_path.read_text(encoding="utf-8", errors="replace")
    sheet_raw = sheet_path.read_text(encoding="utf-8", errors="replace")
    store_code = strip_comments_and_strings(store_raw)
    sheet_code = strip_comments_and_strings(sheet_raw)

    problems += check_arms(store_code)
    problems += check_sheet_arms(sheet_code, function_bodies(store_code + sheet_code), store_code)
    problems += check_bucket_literals(strip_comments_only(sheet_raw), strip_comments_only(store_raw))
    problems += check_locale_keys(ROOT)

    if problems:
        print("FAIL: browser history store/sheet contracts")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK: BrowserHistoryStore.save() snapshots entries inside "
          "synchronized(entries) and never iterates the live list outside "
          "the monitor (JSON build + file write stay outside the lock); "
          "BrowserHistorySheet reads the store once per refresh and keys its "
          "derived list on a refresh signal the STORE owns (a panel-local "
          "tick/revision counter is rejected), with the bucket headers "
          "localized in all locales that ship the family")
    return 0


if __name__ == "__main__":
    sys.exit(main())

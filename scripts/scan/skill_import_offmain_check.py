#!/usr/bin/env python3
"""
Gate: FIX-E (deep-audit-1023 ISSUE-05, P1) — the Skills sheet PASTE import must
not run the repository write on the Main dispatcher.

The URL tab already wraps its repository call in withContext(Dispatchers.IO);
the paste branch carried the [audit-0917] comment demanding the same wrap while
the code never had it — importFromContent (parse + SQLite write + SKILL.md
write) ran on Main, the ANR the URL branch avoids.

Fix contract asserted here:
  arm1  the paste branch's importFromContent(pasteContent) call is lexically
        inside a `withContext(Dispatchers.IO) { ... }` block (brace-depth scan,
        comments stripped, string templates masked)
  arm2  there is exactly one importFromContent(pasteContent) call site, so the
        wrap cannot be satisfied by an orphan copy elsewhere in the file

deep-audit-1032 ISSUE-06 (P1) arms added to the same gate (E owns only this
gate; no new gate, no new SELFTEST marker):
  arm3  every `reloadFromDisk()` call in the Skills screen is OFF Main — either
        lexically inside a `withContext(Dispatchers.IO)` block *in that file*,
        or routed through a repository method that wraps internally. The
        criterion binds the DECISION POINT (the thread context the call runs
        on), not one spelling of it: moving the wrap into the repository is an
        equivalent fix and must stay green (1031-D lesson).
  arm4  same for the MCP screen (`mcpRepository.reloadFromDisk()`).
        Both screens are asserted — fixing only one is the shape this arm
        exists to catch.
  arm5  the skill editor's remember keys: `editName` must carry keys (an
        un-keyed draft deadlocks the name dialog), `editContent` keys must
        include `updatedAt` (otherwise initialContent re-derives while
        editContent keeps the pre-edit snapshot forever), and the shape anchor
        `val initialContent = remember` must still be present in the file
        (skill_frontmatter_roundtrip_check.py pins that literal).

deep-audit-1040 B-04 (P1) arms added to the same gate (B2 owns only this gate;
no new gate, no new SELFTEST marker):
  arm7  every call to a repository / Keystore-store method that performs disk
        I/O runs OFF Main — in the MCP settings screen AND in both per-session
        sheets (SessionSkillsSheet / SessionMcpsSheet). "Performs disk I/O" is
        derived from the callee's own body (brace-matched, comments stripped,
        one delegation hop) instead of from a literal method list, so a
        brand-new IO method invoked bare is caught while a genuinely pure one
        (`exportServerJSON` / `previewImport` — JSONObject work only) stays
        legal on Main. Fail-closed twice: a (file, call-prefix) pair with no
        call site at all means the anchor lost its subject, and a called method
        with no declaration to judge cannot be proven pure.
  arm8  the OAuth seeding effect's keys must COVER the draft remember's keys.
        `remember(editServer)` + `LaunchedEffect(editServer?.id)` resets the
        draft to "" whenever an edit changes the server but keeps its id, and
        the reader never re-runs — the form then shows an empty client secret
        while storage still holds one.

deep-audit-1041 B-03 (P1) arm added to the same gate (B3 owns only this
gate; no new gate, no new SELFTEST marker):
  arm9  every call to an `EnvVarRepository` method that touches disk runs OFF
        Main in `EnvironmentVariablesScreen` — the sixth screen of this family
        (the other five are arms 6/7). "Touches disk" is derived, not listed:
        `add`/`update`/`delete` reach `saveMetadata().writeText(...)` on
        `env-vars.json`, and `getValue` reads a field that DECLARES itself as a
        SharedPreferences store (`_prefs_fields`), so a Keystore-backed read is
        caught while a genuinely pure `isValidKey` / `isDuplicateKey` stays
        legal on Main. `produceState` is deliberately NOT an IO opener: its body
        runs in the composition's coroutine context (Main), which is exactly the
        half-fix this arm exists to catch.

Limits (deliberate, documented so they are not mistaken for coverage):
  * the repository-side alternative resolves the method body plus ONE
    delegation hop (`fun reloadFromDisk() = load()` / `{ loadAll() }`), not a
    whole call-graph fixpoint. An unresolvable chain counts as "not wrapped"
    (fail-closed) — keep the call-site wrap in that case.
  * arms 3/4 only look at the two Settings screens. The chat/backup callers of
    reloadFromDisk run on non-suspend paths (buildSystemPrompt) and are out of
    this gate's scope; see fix-reports/FIX-E.md §7.

Usage: python3 scripts/scan/skill_import_offmain_check.py [repo_root]
       python3 scripts/scan/skill_import_offmain_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage
"""
import os
import re
import sys

SM_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/SkillsManagementScreen.kt"
CALL = "skillRepository.importFromContent(pasteContent)"
OPENER = "withContext(Dispatchers.IO)"

# [deep-audit-1032 ISSUE-06] The two Settings screens re-scan the repositories
# on entry; both used to do it inline on the Main dispatcher.
MCP_REL = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings/MCPIntegrationsScreen.kt"
SKILL_REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/SkillRepository.kt"
MCP_REPO_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/MCPRepository.kt"

# (screen file, call expression, repository file, repository entry point)
RELOAD_SITES = (
    (SM_REL, "skillRepository.reloadFromDisk()", SKILL_REPO_REL, "reloadFromDisk"),
    (MCP_REL, "mcpRepository.reloadFromDisk()", MCP_REPO_REL, "reloadFromDisk"),
)

# Editor remember keys (arm5) live in the same screen file.
EDITOR_REL = SM_REL
EDITOR_KEYS = (
    ("editName", None),
    ("editContent", "updatedAt"),
)
INITIAL_LITERAL = "val initialContent = remember"

SELFTEST_MARK = "SKILL_IMPORT_OFFMAIN_SELFTEST=PASS"


def strip_comments(text):
    """Remove // and /* */ comments; strings are kept (length-preserving)."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            if text.startswith('"""', i):
                j = text.find('"""', i + 3)
                j = n if j < 0 else j + 3
            else:
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] in ('"', "\n"):
                        j += 1
                        break
                    j += 1
            out.append(text[i:j])
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def mask_strings(text):
    """Replace string-literal and char-literal CONTENTS with 'x'
    (length-preserving) so brace matching survives `${...}` templates."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            if text.startswith('"""', i):
                j = text.find('"""', i + 3)
                j = n if j < 0 else j + 3
            else:
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] in ('"', "\n"):
                        j += 1
                        break
                    j += 1
            out.append('x' * (j - i))
            i = j
        elif c == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] in ("'", "\n"):
                    j += 1
                    break
                j += 1
            out.append('x' * (j - i))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def inside_io_wrap(text_nc, text_masked):
    """True when the paste call site sits inside a withContext(Dispatchers.IO)
    block. Brace-depth stack over the masked text; each open brace remembers
    the code text immediately preceding it."""
    call_idx = text_nc.find(CALL)
    if call_idx < 0:
        return False, "call site not found"
    if text_nc.count(CALL) != 1:
        return False, "expected exactly 1 call site, found %d" % text_nc.count(CALL)
    stack = []
    for i, ch in enumerate(text_masked[:call_idx]):
        if ch == "{":
            head = text_nc[:i].rstrip()
            head = head[:-1].rstrip() if head.endswith("{") else head
            tail = head[-len("withContext(Dispatchers.IO)"):] if len(head) >= len(OPENER) else ""
            stack.append(tail == OPENER)
        elif ch == "}":
            if stack:
                stack.pop()
    if any(stack):
        return True, ""
    return False, "call site is not lexically inside %s { ... }" % OPENER


def _io_wrapped_at(text_nc, text_masked, idx, openers=(OPENER,)):
    """True when code position `idx` sits inside an opener-established block.

    Brace-depth stack over the masked text; each open brace remembers the
    code text immediately preceding it (same trick as inside_io_wrap).
    """
    stack = []
    for i, ch in enumerate(text_masked[:idx]):
        if ch == "{":
            head = text_nc[:i].rstrip()
            head = head[:-1].rstrip() if head.endswith("{") else head
            stack.append(any(head.endswith(o) for o in openers))
        elif ch == "}":
            if stack:
                stack.pop()
    return any(stack)


IO_OPENERS = (OPENER, "launch(Dispatchers.IO)")

REPO_CALL_RE = re.compile(r"skillRepository\.([A-Za-z_]\w*)\s*\(")

def check_all_repo_calls(files):
    """arm6 [deep-audit-1039 B-05]: EVERY repository call in the Skills screen
    runs OFF Main.

    The file had four bare sites (delete from the list confirm, listSkillFiles
    inside a composition-phase remember, delete + update on the detail screen)
    while thirteen siblings were wrapped — the same defect class graded one
    call site at a time, which is how the four were missed. The criterion is
    the DECISION POINT (the dispatcher the call runs on): a site passes when it
    is lexically inside `withContext(Dispatchers.IO)` / `launch(Dispatchers.IO)`
    or when the repository method itself wraps internally (arm 3/4's
    alternative). Fail-closed when the file has no repository call at all —
    that means the anchor lost its subject rather than "nothing to judge".

    `skillRepository.skills` (the StateFlow property) is not a call and is
    excluded by construction: the pattern requires `(` right after the name.
    """
    text = files.get(SM_REL)
    if text is None:
        return ["missing file %s" % SM_REL]
    nc = strip_comments(text)
    masked = mask_strings(nc)
    repo_text = files.get(SKILL_REPO_REL)
    hits = list(REPO_CALL_RE.finditer(nc))
    if not hits:
        return ["%s: no `skillRepository.<method>(` call site found — the arm6 "
                "anchor lost its subject (fail-closed)" % SM_REL]
    failures = []
    for m in hits:
        method = m.group(1)
        line = nc[:m.start()].count("\n") + 1
        if _io_wrapped_at(nc, masked, m.start(), IO_OPENERS):
            continue
        alt = False
        if repo_text is not None:
            alt, _ = repo_method_wraps_io(repo_text, method)
        if not alt:
            failures.append(
                "%s: bare (Main-thread) `skillRepository.%s(` at line %d — wrap it "
                "in withContext(Dispatchers.IO), or move the IO into the repository "
                "method (the criterion is the dispatcher the call runs on, not the "
                "spelling)" % (SM_REL, method, line))
    return failures


def all_calls_offline(text_nc, text_masked, call):
    """True when EVERY occurrence of `call` sits inside an OPENER block."""
    idxs = []
    start = 0
    while True:
        j = text_nc.find(call, start)
        if j < 0:
            break
        idxs.append(j)
        start = j + 1
    if not idxs:
        return False, "no call site found (%s)" % call
    bad = [
        text_nc[:i].count("\n") + 1
        for i in idxs
        if not _io_wrapped_at(text_nc, text_masked, i)
    ]
    if bad:
        return False, "bare (Main-thread) call site(s) at line(s) %s" % bad
    return True, ""


def _match_brace(masked, j):
    """Index just past the `}` matching the `{` at `j` (masked text)."""
    depth = 0
    i = j
    while i < len(masked):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(masked)


def repo_method_wraps_io(text, name, hops=1):
    """True when `fun <name>(...)` wraps its work inside OPENER.

    Handles a block body (brace-matched, comments stripped, strings masked)
    and an expression body — either the wrap itself (`fun f(): X =
    withContext(Dispatchers.IO) { ... }`, the shape every `suspend fun` in
    SkillRepository uses) or ONE delegation hop (`fun f() = load()`). Not a
    call-graph fixpoint — fail-closed when the chain does not resolve.
    """
    nc = strip_comments(text)
    masked = mask_strings(nc)
    m = re.search(r"\bfun\s+%s\s*\(" % re.escape(name), nc)
    if not m:
        return False, "no `fun %s(` declaration" % name
    i = m.end()  # first char after '('
    depth = 1
    while i < len(masked) and depth:
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            depth -= 1
        i += 1
    # The body starts at the first `{` after the parameter list; an `=`
    # before it (and not inside the params) means an expression body. The
    # return-type annotation `: Foo` sits between the two and is skipped by
    # searching rather than by consuming it.
    body_open = masked.find("{", i)
    assign = -1
    stop = body_open if body_open >= 0 else len(masked)
    k = i
    while k < stop:
        if masked[k] == "=" and masked[k - 1:k] not in ("=", "!", "<", ">") \
                and masked[k + 1:k + 2] != "=":
            assign = k
            break
        if masked[k] == "\n":
            break
        k += 1
    if assign >= 0:
        line = nc[assign:].split("\n", 1)[0]
        if OPENER in line:
            return True, ""
        call = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if call and hops > 0:
            return repo_method_wraps_io(text, call.group(1), hops - 1)
        return False, "`%s` expression body is not inside %s" % (name, OPENER)
    if body_open < 0:
        return False, "`%s`: body form not recognised" % name
    body = nc[body_open:_match_brace(masked, body_open)]
    if OPENER in body:
        return True, ""
    if hops > 0:
        callees = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", body)
        for callee in callees:
            if callee == name:
                continue
            ok, _ = repo_method_wraps_io(text, callee, hops - 1)
            if ok:
                return True, ""
    return False, "`%s` body is not inside %s" % (name, OPENER)


def check_paste(text):
    """arms 1-2: the paste import's repository write must be off Main."""
    nc = strip_comments(text)
    masked = mask_strings(nc)
    ok, detail = inside_io_wrap(nc, masked)
    return [] if ok else ["paste import not on IO — %s" % detail]


def check_reload(files):
    """arms 3-4: every reloadFromDisk() call in the two screens is OFF Main.

    Offline = the call site is lexically inside an OPENER block in its own
    file, OR the repository entry point wraps internally (decision point =
    the thread the call runs on, not the spelling).
    """
    failures = []
    for screen_rel, call, repo_rel, repo_fn in RELOAD_SITES:
        text = files.get(screen_rel)
        if text is None:
            failures.append("missing file %s" % screen_rel)
            continue
        nc = strip_comments(text)
        masked = mask_strings(nc)
        ok, detail = all_calls_offline(nc, masked, call)
        if ok:
            continue
        repo_text = files.get(repo_rel)
        alt = False
        if repo_text is not None:
            alt, _ = repo_method_wraps_io(repo_text, repo_fn)
        if not alt:
            failures.append("%s: reload not off Main — %s" % (screen_rel, detail))
    return failures


# [deep-audit-1040 B-04] The MCP settings screen plus the two per-session
# sheets. Same defect class as arm6 one file over.
MCP_OAUTH_REL = ("src/android/app/src/main/java/com/rikkaminis/app/mcp/oauth/"
                 "MCPOAuthStore.kt")
SHEET_SKILLS_REL = ("src/android/app/src/main/java/com/rikkaminis/app/ui/chat/"
                    "SessionSkillsSheet.kt")
SHEET_MCPS_REL = ("src/android/app/src/main/java/com/rikkaminis/app/ui/chat/"
                  "SessionMcpsSheet.kt")

# [deep-audit-1041 B-03] The env-var screen — the sixth screen of the family.
ENVVAR_SCREEN_REL = ("src/android/app/src/main/java/com/rikkaminis/app/ui/settings/"
                     "EnvironmentVariablesScreen.kt")
ENVVAR_REPO_REL = ("src/android/app/src/main/java/com/rikkaminis/app/data/repository/"
                   "EnvVarRepository.kt")

# (screen file, callee prefix, file that declares those methods)
B04_CALL_SITES = (
    (MCP_REL, "mcpRepository.", MCP_REPO_REL),
    (MCP_REL, "MCPOAuthStore.", MCP_OAUTH_REL),
    (SHEET_SKILLS_REL, "skillRepository.", SKILL_REPO_REL),
    (SHEET_MCPS_REL, "mcpRepository.", MCP_REPO_REL),
)

B03_CALL_SITES = (
    (ENVVAR_SCREEN_REL, "envVarRepository.", ENVVAR_REPO_REL),
)

# Tokens that mean "this body reaches the disk (or Keystore-backed prefs)".
IO_MARKERS = (
    "File(", "rawQuery(", "execSQL(", "readText(", "writeText(", "readLines(",
    "mkdirs(", "deleteRecursively(", "renameTo(", "listFiles(", "walkTopDown(",
    "openFileOutput(", "openFileInput(", "contentResolver",
    "EncryptedSharedPreferences", "SharedPreferences", ".edit()", "prefs(",
    "save()", "deleteBridgeFile(", "writeBridgeFile(", "reconcileForServer(",
)


PREFS_ACCESS_SUFFIXES = (".getString(", ".getInt(", ".getBoolean(",
                          ".contains(", ".remove(", ".edit()")


def _prefs_fields(text_nc):
    """Field names whose DECLARATION is a (encrypted) SharedPreferences store.

    Derived from the declaration rather than hard-coded, so renaming
    `encryptedPrefs` cannot silently disarm the arm, and a `getString` on a
    JSONObject / Bundle is still read as pure. Matches both the typed form
    (`val x: SharedPreferences by lazy { ... }`) and the inferred form
    (`val x = ...EncryptedSharedPreferences...` / `...EncryptedPrefsFactory...`).
    """
    names = set()
    for m in re.finditer(
            r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*:[^=\n]*\bSharedPreferences\b",
            text_nc):
        names.add(m.group(1))
    for m in re.finditer(
            r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*=[^\n]*?"
            r"(?:EncryptedSharedPreferences|EncryptedPrefsFactory)",
            text_nc):
        names.add(m.group(1))
    return names


def _fun_body_nc(text_nc, masked, name):
    """(body text, 'block'|'expr') for `fun <name>(...)`; (None, reason) if the
    declaration is absent or its body form is not recognised."""
    m = re.search(r"\bfun\s+%s\s*\(" % re.escape(name), text_nc)
    if not m:
        return None, "no `fun %s(` declaration in this file" % name
    i = m.end()
    depth = 1
    while i < len(masked) and depth:
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            depth -= 1
        i += 1
    body_open = masked.find("{", i)
    stop = body_open if body_open >= 0 else len(masked)
    k = i
    while k < stop:
        if masked[k] == "=" and masked[k - 1:k] not in ("=", "!", "<", ">") \
                and masked[k + 1:k + 2] != "=":
            return text_nc[k:].split("\n", 1)[0], "expr"
        if masked[k] == "\n":
            break
        k += 1
    if body_open < 0:
        return None, "`%s`: body form not recognised" % name
    return text_nc[body_open:_match_brace(masked, body_open)], "block"


def method_io_state(text, name, hops=2, _seen=None):
    """('io'|'pure'|'unknown', reason) — does `fun <name>` reach the disk?

    Direct evidence is an IO_MARKERS token in the body; otherwise follow up to
    `hops` delegation steps into callees declared in the same file. Callees
    that are NOT declared here (stdlib collectors, renames) end the walk rather
    than counting as I/O, so a body that merely maps/filters is not misread.
    The caller treats 'unknown' as I/O for the *top-level* call site only —
    that is the fail-closed half of this arm.
    """
    seen = set() if _seen is None else _seen
    if name in seen:
        return "pure", "recursion"
    seen.add(name)
    nc = strip_comments(text)
    masked = mask_strings(nc)
    body, kind = _fun_body_nc(nc, masked, name)
    if body is None:
        return "unknown", kind
    for marker in IO_MARKERS:
        if marker in body:
            return "io", "`%s` body uses `%s`" % (name, marker)
    # A read through a field that declares itself as a SharedPreferences store is
    # disk-backed even when the body carries no IO_MARKERS token (`getValue`'s
    # whole body is `encryptedPrefs.getString(key, null)`). Derived from the
    # declaration so neither the field name nor the method name is hard-coded.
    for field in _prefs_fields(nc):
        for suffix in PREFS_ACCESS_SUFFIXES:
            if field + suffix in body:
                return "io", "`%s` reads the prefs-backed store `%s`" % (name, field)
    if hops > 0:
        # Only follow BARE calls (`save()`, `parseImportRoot(x)`). A member call
        # (`list.add(…)`, `root.keys()`) must not resolve to a same-named
        # declaration in the repository — that name collision is exactly how
        # `previewImport` got misread as I/O through `MutableList.add` →
        # `fun add(server)` → `save()`.
        for cm in re.finditer(r"(?<![.\w])([A-Za-z_][A-Za-z0-9_]*)\s*\(", body):
            callee = cm.group(1)
            if callee == name:
                continue
            state, why = method_io_state(text, callee, hops - 1, seen)
            if state == "io":
                return "io", why
    return "pure", "`%s` (%s body) has no I/O marker" % (name, kind)


def check_b04_offmain(files):
    """arm7: every disk-I/O repository / store call in the B-04 files is OFF Main.

    The criterion is the decision point (does this call touch disk, and on
    which dispatcher does it run), so the repository-side alternative accepted
    by arms 3/4 also passes: a call site may be bare when the callee wraps its
    own work in `withContext(Dispatchers.IO)`.
    """
    return check_offmain_calls(files, B04_CALL_SITES, "B-04")


def check_b03_offmain(files):
    """arm9 [deep-audit-1041 B-03]: the env-var screen's repository calls are
    OFF Main — same criterion as arm7, applied to the sixth screen of the family.

    Runs only when the map MODELS this screen (either key present). The arm1-8
    fixtures do not, and reporting them red would be a false alarm that hides
    their own regressions. On the real tree `run_checks` seeds both keys (with
    value None when a read failed), so an unreadable file is still fail-closed.
    """
    if ENVVAR_SCREEN_REL not in files and ENVVAR_REPO_REL not in files:
        return []
    return check_offmain_calls(files, B03_CALL_SITES, "B-03")


def check_offmain_calls(files, sites, label):
    """Shared criterion for arms 7 and 9: a call passes when it sits inside an
    IO opener, or when the callee wraps its own work (repository-side fix).

    Fail-closed twice: a (file, prefix) pair with no call site at all means the
    anchor lost its subject, and a called method with no declaration to judge
    cannot be proven pure.
    """
    failures = []
    for screen_rel, prefix, decl_rel in sites:
        text = files.get(screen_rel)
        if text is None:
            failures.append("missing file %s" % screen_rel)
            continue
        nc = strip_comments(text)
        masked = mask_strings(nc)
        hits = list(re.finditer(re.escape(prefix) + r"([A-Za-z_]\w*)\s*\(", nc))
        if not hits:
            failures.append(
                "%s: no `%s<method>(` call site found — the %s anchor lost its "
                "subject (fail-closed)" % (screen_rel, prefix, label))
            continue
        decl = files.get(decl_rel)
        if decl is None:
            failures.append(
                "missing file %s (cannot judge the I/O-ness of the `%s` calls in "
                "%s)" % (decl_rel, prefix, screen_rel))
            continue
        for m in hits:
            method = m.group(1)
            state, why = method_io_state(decl, method)
            if state == "pure":
                continue
            if _io_wrapped_at(nc, masked, m.start(), IO_OPENERS):
                continue
            alt, _ = repo_method_wraps_io(decl, method)
            if alt:
                continue
            line = nc[:m.start()].count("\n") + 1
            failures.append(
                "%s: `%s%s(` at line %d runs on Main while its callee performs "
                "disk I/O (%s) — wrap it in withContext(Dispatchers.IO), or move "
                "the IO into the repository/store method (the criterion is the "
                "dispatcher the call runs on, not the spelling)"
                % (screen_rel, prefix, method, line, why))
    return failures


def _launched_effects(nc, masked):
    """[(key tokens, body text)] for every `LaunchedEffect(...) { ... }`."""
    out = []
    for m in re.finditer(r"\bLaunchedEffect\s*\(", nc):
        start = m.end()
        depth = 1
        i = start
        while i < len(masked) and depth:
            if masked[i] == "(":
                depth += 1
            elif masked[i] == ")":
                depth -= 1
            i += 1
        if depth:
            continue
        keys = [k.strip() for k in nc[start:i - 1].split(",") if k.strip()]
        op = masked.find("{", i)
        if op < 0:
            continue
        out.append((keys, nc[op:_match_brace(masked, op)]))
    return out


SEED_DRAFT_MARKERS = (
    "var oauthClientSecret by remember",
    "var oauthAuthorizedSeed by remember",
)


def check_seed_effect_keys(files):
    """arm8: a seed draft's `remember` keys must be COVERED by the keys of the
    LaunchedEffect that seeds it (direction matters: a narrower effect stops
    re-running on a change the draft still re-derives on).
    """
    text = files.get(MCP_REL)
    if text is None:
        return ["missing file %s" % MCP_REL]
    nc = strip_comments(text)
    masked = mask_strings(nc)
    found = False
    for marker in SEED_DRAFT_MARKERS:
        idx = nc.find(marker)
        if idx < 0:
            continue
        found = True
        j = idx + len(marker)
        while j < len(nc) and nc[j] in " \t":
            j += 1
        keys = [] if nc[j:j + 1] == "{" else _remember_keys(nc, marker)
        if keys is None:
            keys = []
        if not keys:
            return ["%s: `%s` carries no remember key — the draft never re-seeds "
                    "when the edited server changes" % (MCP_REL, marker)]
        subject = marker.split(" by ")[0].replace("var ", "")
        effects = [(k, b) for k, b in _launched_effects(nc, masked)
                   if "%s =" % subject in b]
        if len(effects) != 1:
            return ["%s: expected exactly 1 LaunchedEffect seeding `%s`, found %d "
                    "— fail-closed" % (MCP_REL, subject, len(effects))]
        missing = [k for k in keys if k not in effects[0][0]]
        if missing:
            return ["%s: `%s` is keyed %s but the LaunchedEffect seeding it is "
                    "keyed %s (missing %s) — an editServer change that keeps the "
                    "id resets the draft and the reader never re-runs"
                    % (MCP_REL, marker, keys, effects[0][0], missing)]
    if not found:
        return ["%s: neither OAuth seed draft (`%s`) was found — fail-closed"
                % (MCP_REL, "` / `".join(SEED_DRAFT_MARKERS))]
    return []


def check_editor_keys(files):
    """arm5: the skill editor's remember keys + shape anchor literal."""
    text = files.get(EDITOR_REL)
    if text is None:
        return ["missing file %s" % EDITOR_REL]
    nc = strip_comments(text)
    failures = []
    for var, require in EDITOR_KEYS:
        decl = re.search(r"\bvar\s+%s\s+by\s+remember" % var, nc)
        if not decl:
            failures.append(
                "%s: `var %s by remember` declaration not found" % (EDITOR_REL, var))
            continue
        keys = re.match(r"\s*\(([^)]*)\)", nc[decl.end():])
        if not keys:
            failures.append("%s: `%s` remember has no key" % (EDITOR_REL, var))
            continue
        k = keys.group(1).strip()
        if k in ("", "Unit"):
            failures.append(
                "%s: `%s` remember key list is empty/Unit" % (EDITOR_REL, var))
        elif require and require not in k:
            failures.append(
                "%s: `%s` remember keys lack `%s`" % (EDITOR_REL, var, require))
    if INITIAL_LITERAL not in nc:
        failures.append(
            "%s: shape anchor `%s` missing" % (EDITOR_REL, INITIAL_LITERAL))
    return failures


def _remember_keys(nc, marker):
    """Ordered key tokens of the `remember(...)` call that follows `marker`.

    Paren-matched, so a nested call inside a key (a `collectAsState()` source)
    cannot truncate the list the way a first-`)` scan would. Returns None when
    the marker or a balanced key list is absent (caller reports fail-closed).
    """
    idx = nc.find(marker)
    if idx < 0:
        return None
    op = nc.find("(", idx + len(marker))
    if op < 0:
        return None
    depth = 0
    i = op
    while i < len(nc):
        c = nc[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        return None
    return [k.strip() for k in nc[op + 1:i].split(",") if k.strip()]


def check_editor_key_alignment(files):
    """The editor draft must re-derive on everything the content it seeds from
    re-derives on (initialContent keys ⊆ editContent keys).

    binds the DECISION POINT (which changes re-seed the draft), not a literal
    key list: any future key added to one remember and not the match is red.
    Direction is deliberate — an EXTRA key on the draft is not a defect, a
    MISSING one is (the draft then freezes on a stale snapshot).
    """
    text = files.get(EDITOR_REL)
    if text is None:
        return ["missing file %s" % EDITOR_REL]
    nc = strip_comments(text)
    initial = _remember_keys(nc, INITIAL_LITERAL)
    edit = _remember_keys(nc, "var editContent by remember")
    if not initial or not edit:
        return ["%s: could not read the initialContent/editContent remember key "
                "lists — fail-closed" % EDITOR_REL]
    missing = [k for k in initial if k not in edit]
    if missing:
        return ["%s: `editContent` remember keys are missing %s that "
                "`initialContent` re-derives from — the editor draft freezes on "
                "the pre-read snapshot and Save writes it back over an "
                "out-of-band edit" % (EDITOR_REL, missing)]
    return []


def _read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return fh.read()


def run_checks_map(files):
    """All arms over a {rel_path: text} map (real tree or fixtures)."""
    failures = []
    sm = files.get(SM_REL)
    if sm is None:
        failures.append("missing file %s" % SM_REL)
    else:
        failures.extend(check_paste(sm))
    failures.extend(check_reload(files))
    failures.extend(check_all_repo_calls(files))
    failures.extend(check_editor_keys(files))
    failures.extend(check_editor_key_alignment(files))
    failures.extend(check_b04_offmain(files))
    failures.extend(check_b03_offmain(files))
    failures.extend(check_seed_effect_keys(files))
    return failures


def run_checks(repo_root):
    files = {}
    for rel in (SM_REL, MCP_REL, SKILL_REPO_REL, MCP_REPO_REL, MCP_OAUTH_REL,
                SHEET_SKILLS_REL, SHEET_MCPS_REL, ENVVAR_SCREEN_REL,
                ENVVAR_REPO_REL):
        try:
            files[rel] = _read(repo_root, rel)
        except OSError:
            pass
    # Seed the arm9 anchors explicitly: an unreadable file must read as
    # "missing file" (fail-closed), not as "this fixture set does not model the
    # screen" (which is how `check_b03_offmain` treats an absent key).
    for rel in (ENVVAR_SCREEN_REL, ENVVAR_REPO_REL):
        files.setdefault(rel, None)
    return run_checks_map(files)


# ── selftest fixtures ────────────────────────────────────────────────────

GOOD_SM = '''package com.rikkaminis.app.ui.settings

fun sample(pasteContent: String, scope: CoroutineScope, onDismiss: () -> Unit) {
    scope.launch {
        try {
            isLoading = true
            val result = withContext(Dispatchers.IO) {
                skillRepository.importFromContent(pasteContent)
            }
            if (result != null) onDismiss()
        } catch (e: Exception) {
            errorText = "Error: ${e.message}"
        } finally {
            isLoading = false
        }
    }
}
'''

BAD_MAIN_THREAD = GOOD_SM.replace(
    """            val result = withContext(Dispatchers.IO) {
                skillRepository.importFromContent(pasteContent)
            }""",
    "            val result = skillRepository.importFromContent(pasteContent)")

BAD_DOUBLE_SITE = GOOD_SM + '''
fun sample2(pasteContent: String) {
    val result = skillRepository.importFromContent(pasteContent)
}
'''


def run_checks_text(text):
    """arms 1-2 only — kept for the two original paste fixtures."""
    return check_paste(text)


# ── deep-audit-1032 ISSUE-06 fixtures (arms 3-5) ─────────────────────────

def _sm_text(
    paste_io=True,
    reload_io=True,
    edit_name_keyed=True,
    edit_content_keys="skillId, relativePath, skill.updatedAt, loadedDiskBase",
    initial_keys="skillId, relativePath, skill.updatedAt, loadedDiskBase",
    initial_literal=True,
    extra="",
):
    paste = (
        "            val r = withContext(Dispatchers.IO) {\n"
        "                skillRepository.importFromContent(pasteContent)\n"
        "            }"
    ) if paste_io else "            val r = skillRepository.importFromContent(pasteContent)"
    reload_ = (
        "    LaunchedEffect(Unit) {\n"
        "        withContext(Dispatchers.IO) {\n"
        "            skillRepository.reloadFromDisk()\n"
        "        }\n"
        "    }"
    ) if reload_io else (
        "    LaunchedEffect(Unit) {\n"
        "        skillRepository.reloadFromDisk()\n"
        "    }"
    )
    edit_name = (
        "    var editName by remember(skill.id, skill.name) { mutableStateOf(skill.name) }"
    ) if edit_name_keyed else "    var editName by remember { mutableStateOf(skill.name) }"
    initial = (
        '    val initialContent = remember(' + initial_keys + ') { "" }'
    ) if initial_literal else (
        '    val contentInit = remember(skillId, relativePath) { "" }'
    )
    return (
        "package com.rikkaminis.app.ui.settings\n\n"
        "fun sample(pasteContent: String) {\n"
        "    scope.launch {\n" + paste + "\n    }\n"
        + reload_ + "\n"
        + edit_name + "\n"
        + initial + "\n"
        "    var editContent by remember(" + edit_content_keys + ") { mutableStateOf(initialContent) }\n"
        "    var unrelatedDialog by remember { mutableStateOf(false) }\n"
        + extra + "}\n"
    )


def _mcp_text(reload_io=True, oauth_io=True, seed_aligned=True, extra=""):
    body = (
        "        withContext(Dispatchers.IO) {\n"
        "            mcpRepository.reloadFromDisk()\n"
        "        }\n"
    ) if reload_io else "        mcpRepository.reloadFromDisk()\n"
    oauth = (
        "    scope.launch {\n"
        "        withContext(Dispatchers.IO) {\n"
        "            MCPOAuthStore.setClientSecret(context, id, \"\")\n"
        "        }\n"
        "    }\n"
    ) if oauth_io else "    MCPOAuthStore.setClientSecret(context, id, \"\")\n"
    effect_key = "editServer" if seed_aligned else "editServer?.id"
    return (
        "package com.rikkaminis.app.ui.settings\n\n"
        "fun sample() {\n    LaunchedEffect(Unit) {\n" + body + "    }\n"
        + oauth
        + "    var oauthClientSecret by remember(editServer) { mutableStateOf(\"\") }\n"
        + "    LaunchedEffect(" + effect_key + ") {\n"
        + "        val s = withContext(Dispatchers.IO) {\n"
        + "            MCPOAuthStore.clientSecret(context, id)\n"
        + "        }\n"
        + "        oauthClientSecret = s ?: \"\"\n"
        + "    }\n"
        + extra
        + "}\n"
    )


REPO_BARE = '''package com.rikkaminis.app.data.repository

private fun loadAll() {
    db.rawQuery("SELECT * FROM skills", null)
    filesDir.listFiles()
}

fun reloadFromDisk() {
    loadAll()
}
'''

REPO_WRAPS = '''package com.rikkaminis.app.data.repository

private fun loadAll() {
    withContext(Dispatchers.IO) {
        db.rawQuery("SELECT * FROM skills", null)
    }
}

fun reloadFromDisk() {
    loadAll()
}
'''

MCP_REPO_BARE = '''package com.rikkaminis.app.data.repository

fun load() {
    val parsed = readServersFile()
    _servers.value = parsed
}

fun reloadFromDisk() = load()
'''

MCP_REPO_WRAPS = '''package com.rikkaminis.app.data.repository

fun load() {
    withContext(Dispatchers.IO) {
        val parsed = readServersFile()
    }
}

fun reloadFromDisk() = load()
'''

# ── deep-audit-1040 B-04 fixtures ────────────────────────────────────────
# The chat-side sheets write their per-session overrides through a SQLite
# `INSERT OR REPLACE` / `DELETE` per row, and seed from one `rawQuery` per row.
OAUTH_STORE_FIXTURE = '''package com.rikkaminis.app.mcp.oauth

private fun prefs(context: Context): SharedPreferences =
    EncryptedSharedPreferences.create(context, FILE)

fun clientSecret(context: Context, server: String): String? =
    prefs(context).getString(secretKey(server), null)

fun setClientSecret(context: Context, server: String, secret: String?) {
    prefs(context).edit().putString(secretKey(server), secret).apply()
}
'''


def _sheet_skills_text(seed_io=True, write_io=True):
    seed = (
        "        val seeds = withContext(Dispatchers.IO) {\n"
        "            ids.associateWith { skillRepository.isEnabledForSession(it, sessionId) }\n"
        "        }\n"
    ) if seed_io else (
        "        val seeds = ids.associateWith {\n"
        "            skillRepository.isEnabledForSession(it, sessionId)\n"
        "        }\n"
    )
    write = (
        "    scope.launch {\n"
        "        withContext(Dispatchers.IO) {\n"
        "            skillRepository.setSessionOverride(sessionId, \"a\", true)\n"
        "        }\n"
        "    }\n"
    ) if write_io else (
        "    skillRepository.setSessionOverride(sessionId, \"a\", true)\n"
    )
    return (
        "package com.rikkaminis.app.ui.chat\n\n"
        "fun sample(skills: List<String>, sessionId: String, scope: CoroutineScope) {\n"
        "    val overrides = mutableStateMapOf<String, Boolean>()\n"
        "    LaunchedEffect(skills, sessionId) {\n"
        "        val ids = skills.map { it }\n"
        + seed
        + "        mergeSessionOverrides(\n"
        + "            liveIds = ids,\n"
        + "            overrides = overrides,\n"
        + "            lookup = { seeds[it] ?: false },\n"
        + "        )\n"
        "    }\n"
        + write
        + "}\n"
    )


def _sheet_mcps_text(seed_io=True, write_io=True):
    seed = (
        "        val seeds = withContext(Dispatchers.IO) {\n"
        "            missing.associateWith { mcpRepository.isEnabledForSession(it, sessionId) }\n"
        "        }\n"
    ) if seed_io else (
        "        val seeds = missing.associateWith {\n"
        "            mcpRepository.isEnabledForSession(it, sessionId)\n"
        "        }\n"
    )
    write = (
        "    scope.launch {\n"
        "        withContext(Dispatchers.IO) {\n"
        "            mcpRepository.setSessionOverride(sessionId, \"a\", true)\n"
        "        }\n"
        "    }\n"
    ) if write_io else (
        "    mcpRepository.setSessionOverride(sessionId, \"a\", true)\n"
    )
    return (
        "package com.rikkaminis.app.ui.chat\n\n"
        "fun sample(servers: List<String>, sessionId: String, scope: CoroutineScope) {\n"
        "    val overrides = mutableStateMapOf<String, Boolean>()\n"
        "    LaunchedEffect(servers, sessionId) {\n"
        "        val missing = servers.filter { it !in overrides }\n"
        + seed
        + "        for (id in missing) overrides[id] = seeds[id] ?: false\n"
        "    }\n"
        + write
        + "}\n"
    )


# Two declarations of the same entry points, differing only in where the disk
# access sits: `MCP_REPO_FIX` keeps it in the method bodies (so a bare call site
# is a real Main-thread I/O), `MCP_REPO_WRAPS_WRITE` wraps it internally (the
# equivalent fix arms 3/4 accept).
MCP_REPO_FIX = '''package com.rikkaminis.app.data.repository

fun exportServerJSON(server: MCPServerConfig): String {
    val o = JSONObject().put(server.id, entryToJson(server))
    return o.toString(2)
}

private fun entryToJson(s: MCPServerConfig): JSONObject {
    val o = JSONObject()
    o.put("command", s.command)
    return o
}

fun previewImport(text: String): Int = parseRoot(text).size

private fun parseRoot(text: String): List<String> {
    val root = JSONObject(text.trim())
    return root.keys().asSequence().toList()
}

fun setEnabled(id: String, enabled: Boolean) {
    save()
}

fun setSessionOverride(sessionId: String, mcpId: String, enabled: Boolean) {
    db.execSQL("INSERT OR REPLACE INTO mcp_session_overrides VALUES (?, ?, ?)", null)
}

fun isEnabledForSession(mcpId: String, sessionId: String): Boolean {
    val cursor = db.rawQuery("SELECT is_enabled FROM mcp_session_overrides", null)
    cursor.close()
    return false
}

private fun save() {
    mcpDir.mkdirs()
}
'''

MCP_REPO_WRAPS_WRITE = '''package com.rikkaminis.app.data.repository

fun setEnabled(id: String, enabled: Boolean) {
    withContext(Dispatchers.IO) {
        save()
    }
}

private fun save() {
    mcpDir.mkdirs()
}
'''

SKILL_REPO_FIX = '''package com.rikkaminis.app.data.repository

fun isEnabledForSession(skillId: String, sessionId: String): Boolean {
    val cursor = db.rawQuery("SELECT is_enabled FROM session_skill_overrides", null)
    cursor.close()
    return false
}

fun setSessionOverride(sessionId: String, skillId: String, enabled: Boolean) {
    db.execSQL("INSERT OR REPLACE INTO session_skill_overrides VALUES (?, ?, ?)", null)
}
'''


def _files(sm=None, mcp=None, skill_repo=None, mcp_repo=None, sheet_skills=None,
           sheet_mcps=None, oauth_store=None):
    return {
        SM_REL: _sm_text() if sm is None else sm,
        MCP_REL: _mcp_text() if mcp is None else mcp,
        SKILL_REPO_REL: REPO_BARE if skill_repo is None else skill_repo,
        MCP_REPO_REL: MCP_REPO_BARE if mcp_repo is None else mcp_repo,
        SHEET_SKILLS_REL: (_sheet_skills_text() if sheet_skills is None
                           else sheet_skills),
        SHEET_MCPS_REL: _sheet_mcps_text() if sheet_mcps is None else sheet_mcps,
        MCP_OAUTH_REL: OAUTH_STORE_FIXTURE if oauth_store is None else oauth_store,
    }

# ── arm9 [deep-audit-1041 B-03] fixtures ─────────────────────────────────
# `_b03_files()` = the common fixture map plus the env-var screen/declaration.
# Kept separate from `_files()` on purpose: folding these two entries into the
# shared map would make every arm1-8 fixture arm9-red (fail-closed on a missing
# screen) and mask real regressions in those arms.
ENVVAR_REPO_FIXTURE = '''package com.rikkaminis.app.data.repository

class EnvVarRepository {
    private val _entries = MutableStateFlow<List<String>>(emptyList())

    private val encryptedPrefs: SharedPreferences by lazy {
        EncryptedPrefsFactory.safeCreate(context, "env_var_values")
    }

    private val metadataFile: File
        get() = File(context.filesDir, "env-vars.json")

    fun isValidKey(key: String): Boolean = KEY_REGEX.matches(key)

    fun isDuplicateKey(key: String): Boolean =
        _entries.value.any { it.equals(key, ignoreCase = true) }

    fun getValue(key: String): String? = encryptedPrefs.getString(key, null)

    fun add(key: String, value: String): Boolean {
        _entries.value = _entries.value + key
        encryptedPrefs.edit().putString(key, value).apply()
        saveMetadata()
        return true
    }

    fun update(id: String, key: String, value: String): Boolean {
        encryptedPrefs.edit().putString(key, value).apply()
        saveMetadata()
        return true
    }

    fun delete(id: String) {
        encryptedPrefs.edit().remove(id).apply()
        saveMetadata()
    }

    private fun saveMetadata() {
        metadataFile.writeText("[]")
    }
}
'''

# Same repository, prefs store renamed: the arm must judge the DECLARATION, so a
# rename cannot disarm it.
ENVVAR_REPO_RENAMED = ENVVAR_REPO_FIXTURE.replace("encryptedPrefs", "secureStore")

# Same repository whose `getValue` reads a plain in-memory map. `getValue` is
# then genuinely pure and a bare call site on Main must stay legal — the
# precision twin of the arm above.
ENVVAR_REPO_PURE_READ = (
    ENVVAR_REPO_FIXTURE
    .replace(
        '''    private val encryptedPrefs: SharedPreferences by lazy {
        EncryptedPrefsFactory.safeCreate(context, "env_var_values")
    }
''',
        "    private val valueCache = mutableMapOf<String, String>()\n")
    .replace(
        "fun getValue(key: String): String? = encryptedPrefs.getString(key, null)",
        "fun getValue(key: String): String? = valueCache[key]")
    .replace("encryptedPrefs.edit().putString(key, value).apply()",
             "valueCache[key] = value")
    .replace("encryptedPrefs.edit().remove(id).apply()",
             "valueCache.remove(id)")
)

# The repository-side alternative the criterion accepts: `add` wraps its own
# work, so a bare call site resolves through `repo_method_wraps_io`.
ENVVAR_REPO_WRAPS = ENVVAR_REPO_FIXTURE.replace(
    '''    fun add(key: String, value: String): Boolean {
        _entries.value = _entries.value + key
        encryptedPrefs.edit().putString(key, value).apply()
        saveMetadata()
        return true
    }
''',
    '''    fun add(key: String, value: String): Boolean {
        withContext(Dispatchers.IO) {
            saveMetadata()
        }
        _entries.value = _entries.value + key
        return true
    }
''')

ENVVAR_SCREEN_TEMPLATE = '''package com.rikkaminis.app.ui.settings

@Composable
fun EnvironmentVariablesScreen(envVarRepository: EnvVarRepository) {
    val scope = rememberCoroutineScope()
    val isValid = envVarRepository.isValidKey(normalizedKey)
    val isDuplicate = envVarRepository.isDuplicateKey(normalizedKey, excludeId = null)
%(body)s
}
'''

CLEAN_BODY = '''    LaunchedEffect(Unit) {
        withContext(Dispatchers.IO) { envVarRepository.delete("a") }
    }
    val display by produceState("", 1) {
        value = withContext(Dispatchers.IO) { envVarRepository.getValue("k") ?: "" }
    }
    scope.launch {
        withContext(Dispatchers.IO) { envVarRepository.add("k", "v") }
    }
'''

BARE_WRITE_BODY = CLEAN_BODY.replace(
    "        withContext(Dispatchers.IO) { envVarRepository.add(\"k\", \"v\") }\n",
    "        envVarRepository.add(\"k\", \"v\")\n")

BARE_READ_BODY = CLEAN_BODY.replace(
    "        value = withContext(Dispatchers.IO) { envVarRepository.getValue(\"k\") ?: \"\" }\n",
    "        value = envVarRepository.getValue(\"k\") ?: \"\"\n")

PURE_ONLY_BODY = ""

# No repository CALL at all (only the StateFlow): the anchor lost its subject.
NO_CALLS_SCREEN = '''package com.rikkaminis.app.ui.settings

@Composable
fun EnvironmentVariablesScreen(envVarRepository: EnvVarRepository) {
    val entries by envVarRepository.entries.collectAsState()
    Text(entries.size.toString())
}
'''

def _b03_files(body=CLEAN_BODY, repo=None, screen=None):
    files = _files()
    files[ENVVAR_SCREEN_REL] = (
        ENVVAR_SCREEN_TEMPLATE % {"body": body} if screen is None else screen)
    files[ENVVAR_REPO_REL] = ENVVAR_REPO_FIXTURE if repo is None else repo
    return files


B03_FIXTURES = [
    # (label, files, expect_red, needle)
    ("b03 clean: every env-var IO call off Main",
     _b03_files(), False, None),
    ("b03 bare write (add) on Main",
     _b03_files(body=BARE_WRITE_BODY), True, "`envVarRepository.add(` at line"),
    ("b03 bare read (getValue) inside produceState",
     _b03_files(body=BARE_READ_BODY), True, "`envVarRepository.getValue(` at line"),
    ("b03 precision: pure key checks alone stay legal on Main",
     _b03_files(body=PURE_ONLY_BODY), False, None),
    ("b03 precision: the prefs field is judged from its declaration",
     _b03_files(body=BARE_READ_BODY, repo=ENVVAR_REPO_RENAMED),
     True, "`envVarRepository.getValue(` at line"),
    ("b03 precision: a pure getValue over a plain map stays legal",
     _b03_files(body=BARE_READ_BODY, repo=ENVVAR_REPO_PURE_READ), False, None),
    ("b03 alt branch: the repository method wraps internally",
     _b03_files(body=BARE_WRITE_BODY, repo=ENVVAR_REPO_WRAPS), False, None),
    ("b03 fail-closed: no declaration to judge the callee",
     _b03_files(body=CLEAN_BODY + '    scope.launch { envVarRepository.wipeAll() }\n'),
     True, "no `fun wipeAll(` declaration"),
    ("b03 fail-closed: screen without repository calls",
     _b03_files(screen=NO_CALLS_SCREEN), True, "lost its subject"),
    ("b03 fail-closed: declaration file is empty",
     _b03_files(body=CLEAN_BODY, repo=""),
     True, "no `fun isValidKey(` declaration"),
]

# ── arm6 [deep-audit-1039 B-05] fixtures ─────────────────────────────────
# Extra call sites appended to the task body (inside the function, before the
# closing brace), so arms 1-5 stay in their clean shape while arm6 sees them.
REPO_WRAPS_LIST = '''package com.rikkaminis.app.data.repository

fun listSkillFiles(skillId: String): List<String> {
    withContext(Dispatchers.IO) {
        File(skillsDir, skillId).walkTopDown().toList()
    }
    return emptyList()
}
'''

EXTRA_WRAPPED = (
    "    scope.launch {\n"
    "        withContext(Dispatchers.IO) { skillRepository.delete(\"a\") }\n"
    "    }\n"
    "    scope.launch {\n"
    "        withContext(Dispatchers.IO) { skillRepository.listSkillFiles(\"a\") }\n"
    "    }\n"
    "    val enabled = skillRepository.skills\n"
)
EXTRA_BARE_DELETE = "    skillRepository.delete(\"a\")\n"
EXTRA_BARE_SETENABLED = "    skillRepository.setEnabled(\"a\", true)\n"
EXTRA_BARE_LIST = "    val files = skillRepository.listSkillFiles(\"a\")\n"
EXTRA_LAUNCH_DISPATCHER = (
    "    scope.launch(Dispatchers.IO) { skillRepository.delete(\"a\") }\n"
)
EXTRA_FLOW_ONLY = "    val enabled = skillRepository.skills\n"

ARM6_FIXTURES = [
    # (label, files, expect_red, needle)
    ("arm6 clean: extra call sites wrapped",
     _files(sm=_sm_text(extra=EXTRA_WRAPPED)), False, None),
    ("arm6 bare delete on Main",
     _files(sm=_sm_text(extra=EXTRA_BARE_DELETE)), True,
     "bare (Main-thread) `skillRepository.delete(` at line"),
    ("arm6 bare setEnabled on Main",
     _files(sm=_sm_text(extra=EXTRA_BARE_SETENABLED)), True,
     "bare (Main-thread) `skillRepository.setEnabled(` at line"),
    ("arm6 bare call whose repo method wraps internally",
     _files(sm=_sm_text(extra=EXTRA_BARE_LIST), skill_repo=REPO_WRAPS_LIST), False, None),
    ("arm6 precision: launch(Dispatchers.IO) is off Main too",
     _files(sm=_sm_text(extra=EXTRA_LAUNCH_DISPATCHER)), False, None),
    ("arm6 precision: the StateFlow property is not a call site",
     _files(sm=_sm_text(extra=EXTRA_FLOW_ONLY)), False, None),
]

def _arm6_fail_closed() -> list:
    """A screen with no repository CALL at all must be red (fail-closed)."""
    only_flow = ("package com.rikkaminis.app.ui.settings\n\n"
                 "fun sample() {\n" + EXTRA_FLOW_ONLY + "}\n")
    return check_all_repo_calls({SM_REL: only_flow})


ISSUE06_FIXTURES = [
    # (label, files, expect_red, needle)
    ("clean: both screens wrapped (branch A)",
     _files(), False, None),
    ("precision: paste IO block does not cover reload",
     _files(sm=_sm_text(paste_io=True, reload_io=False)), True, "bare (Main-thread)"),
    ("MCP screen bare on Main",
     _files(mcp=_mcp_text(reload_io=False)), True, "MCPIntegrationsScreen"),
    ("alt branch: repo method wraps (call site bare)",
     _files(sm=_sm_text(reload_io=False), skill_repo=REPO_WRAPS), False, None),
    ("alt branch: MCP repo wraps via 1 delegation hop",
     _files(mcp=_mcp_text(reload_io=False), mcp_repo=MCP_REPO_WRAPS), False, None),
    ("commented-out bare reload must not count",
     _files(sm=_sm_text(extra="    // skillRepository.reloadFromDisk()\n")), False, None),
    ("paste arm not satisfied by the reload IO block",
     _files(sm=_sm_text(paste_io=False, reload_io=True)), True, "paste import not on IO"),
    ("editName un-keyed (dialog snapshot)",
     _files(sm=_sm_text(edit_name_keyed=False)), True, "`editName` remember has no key"),
    ("editName keyed with Unit (no usable key)",
     _files(sm=_sm_text(edit_name_keyed=False).replace(
         "    var editName by remember {",
         "    var editName by remember(Unit) {")), True, "empty/Unit"),
    ("editContent keys lack updatedAt",
     _files(sm=_sm_text(edit_content_keys="skillId, relativePath")),
     True, "lack `updatedAt`"),
    ("editContent drops loadedDiskBase (B-04 pre-fix shape)",
     _files(sm=_sm_text(edit_content_keys="skillId, relativePath, skill.updatedAt")),
     True, "editor draft freezes"),
    ("precision: extra editor-only key is not a defect",
     _files(sm=_sm_text(edit_content_keys="skillId, relativePath, skill.updatedAt, loadedDiskBase, draftExtra")),
     False, None),
    ("precision: alignment follows the key, not its position",
     _files(sm=_sm_text(initial_keys="loadedDiskBase, skill.updatedAt, skillId, relativePath")),
     False, None),
    ("initialContent shape anchor removed",
     _files(sm=_sm_text(initial_literal=False)), True, "shape anchor"),
]


# ── arm7/arm8 [deep-audit-1040 B-04] fixtures ────────────────────────────
B04_FIXTURES = [
    # (label, files, expect_red, needle)
    ("b04 clean: every disk-IO call off Main",
     _files(), False, None),
    ("b04 bare mcpRepository.setEnabled on Main",
     _files(mcp=_mcp_text(extra='    mcpRepository.setEnabled("a", true)\n'),
            mcp_repo=MCP_REPO_FIX),
     True, "`mcpRepository.setEnabled(` at line"),
    ("b04 bare MCPOAuthStore.setClientSecret on Main",
     _files(mcp=_mcp_text(extra='    MCPOAuthStore.setClientSecret(context, "a", "")\n')),
     True, "`MCPOAuthStore.setClientSecret(` at line"),
    ("b04 precision: pure exportServerJSON stays legal on Main",
     _files(mcp=_mcp_text(extra="    val j = mcpRepository.exportServerJSON(server)\n"),
            mcp_repo=MCP_REPO_FIX),
     False, None),
    ("b04 precision: pure previewImport stays legal on Main",
     _files(mcp=_mcp_text(extra="    val n = mcpRepository.previewImport(text)\n"),
            mcp_repo=MCP_REPO_FIX),
     False, None),
    ("b04 precision: a member call does not resolve to a same-named fun",
     _files(mcp=_mcp_text(extra="    val n = mcpRepository.previewImport(text)\n"),
            mcp_repo=MCP_REPO_FIX + "\nprivate fun note() { out.add(1) }\n"),
     False, None),
    ("b04 alt branch: the repo method wraps internally",
     _files(mcp=_mcp_text(extra='    mcpRepository.setEnabled("a", true)\n'),
            mcp_repo=MCP_REPO_WRAPS_WRITE),
     False, None),
    ("b04 fail-closed: no declaration to judge",
     _files(mcp=_mcp_text(extra="    mcpRepository.wipeEverything()\n"),
            mcp_repo=MCP_REPO_FIX),
     True, "no `fun wipeEverything(` declaration"),
    ("b04 fail-closed: screen without repository calls",
     _files(mcp="package x\n\nfun a() {}\n"),
     True, "lost its subject"),
    ("b04 sheet bare write on Main (skills)",
     _files(sheet_skills=_sheet_skills_text(write_io=False), skill_repo=SKILL_REPO_FIX),
     True, "SessionSkillsSheet"),
    ("b04 sheet bare seed read on Main (skills)",
     _files(sheet_skills=_sheet_skills_text(seed_io=False), skill_repo=SKILL_REPO_FIX),
     True, "rawQuery("),
    ("b04 sheet bare write on Main (mcps)",
     _files(sheet_mcps=_sheet_mcps_text(write_io=False), mcp_repo=MCP_REPO_FIX),
     True, "SessionMcpsSheet"),
    ("b04 sheet bare seed read on Main (mcps)",
     _files(sheet_mcps=_sheet_mcps_text(seed_io=False), mcp_repo=MCP_REPO_FIX),
     True, "SessionMcpsSheet"),
    ("b04 arm8: seed effect keyed narrower than the draft",
     _files(mcp=_mcp_text(seed_aligned=False)),
     True, "seeding it is keyed"),
    ("b04 arm8: un-keyed draft never re-seeds",
     _files(mcp=_mcp_text().replace(
         "var oauthClientSecret by remember(editServer) {",
         "var oauthClientSecret by remember {")),
     True, "carries no remember key"),
]


def selftest():
    ok = True
    f = run_checks_text(GOOD_SM)
    print("selftest clean tree: %s" % ("PASS" if not f else "FAIL %s" % f))
    ok &= not f

    for label, text in [
        ("bare Main-thread call", BAD_MAIN_THREAD),
        ("second call site outside IO", BAD_DOUBLE_SITE),
    ]:
        f = run_checks_text(text)
        red = len(f) > 0
        print("selftest dirty %-30s: %s %s" % (label, "RED" if red else "NOT-RED", f[:1]))
        ok &= red

    for label, files, expect_red, needle in ISSUE06_FIXTURES:
        f = run_checks_map(files)
        red = len(f) > 0
        good = (red == expect_red)
        if good and expect_red and needle:
            good = any(needle in x for x in f)
        print("selftest 1032-06 %-46s: %s%s" % (
            label, "RED" if red else "CLEAN",
            "" if good else "  UNEXPECTED %s" % f[:2]))
        ok &= good

    for label, files, expect_red, needle in ARM6_FIXTURES:
        f = run_checks_map(files)
        red = len(f) > 0
        good = (red == expect_red)
        if good and expect_red and needle:
            good = any(needle in x for x in f)
        print("selftest 1039-B05 %-46s: %s%s" % (
            label, "RED" if red else "CLEAN",
            "" if good else "  UNEXPECTED %s" % f[:2]))
        ok &= good

    f = _arm6_fail_closed()
    red = len(f) > 0
    good = red and "no `skillRepository.<method>(` call site found" in f[0]
    print("selftest 1039-B05 %-46s: %s%s" % (
        "fail-closed when no repository call exists", "RED" if red else "CLEAN",
        "" if good else "  UNEXPECTED %s" % f[:2]))
    ok &= good

    for label, files, expect_red, needle in B04_FIXTURES:
        f = run_checks_map(files)
        red = len(f) > 0
        good = (red == expect_red)
        if good and expect_red and needle:
            good = any(needle in x for x in f)
        print("selftest 1040-B04 %-46s: %s%s" % (
            label, "RED" if red else "CLEAN",
            "" if good else "  UNEXPECTED %s" % f[:2]))
        ok &= good

    for label, files, expect_red, needle in B03_FIXTURES:
        f = run_checks_map(files)
        red = len(f) > 0
        good = (red == expect_red)
        if good and expect_red and needle:
            good = any(needle in x for x in f)
        print("selftest 1041-B03 %-46s: %s%s" % (
            label, "RED" if red else "CLEAN",
            "" if good else "  UNEXPECTED %s" % f[:2]))
        ok &= good

    print(SELFTEST_MARK if ok else "SKILL_IMPORT_OFFMAIN_SELFTEST=FAIL")
    return 0 if ok else 1


def main():
    # Argv conventions in this tree: test_scan.py passes `[script, ROOT]` (real
    # tree) or `[script, --selftest]`; gate_run.sh passes
    # `[script, --selftest, ROOT]`. Scan for the flag instead of indexing argv
    # (indexing silently treated "--selftest" as the repo root → red gate).
    args = [a for a in sys.argv[1:] if a]
    if "--selftest" in args:
        sys.exit(selftest())
    repo = next((a for a in args if not a.startswith("-")), ".")
    failures = run_checks(repo)
    if failures:
        # Each finding names its own file — the check now covers two screens,
        # so a single fixed header path would point at the wrong file.
        print("FAIL skill_import_offmain_check: %d finding(s)" % len(failures))
        for f in failures:
            print("  - %s" % f)
        sys.exit(1)
    print("OK %s (15 arms: paste x2, reload-off-main x2, all-repo-calls-off-main x1, "
          "editor-keys x3, editor-key-alignment x1, B-04 io-call-off-main x1 "
          "(MCP screen + both sheets), B-04 seed-effect-keys x1, "
          "B-03 io-call-off-main x1 (env-var screen), fixture gates)"
          % SM_REL)
    sys.exit(0)


if __name__ == "__main__":
    main()

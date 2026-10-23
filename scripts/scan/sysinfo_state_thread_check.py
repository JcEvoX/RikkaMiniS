#!/usr/bin/env python3
"""CI scan gate — system-notice coalesce window has ONE guarded home
(deep-audit-1016c FIX-G / ISSUE-10(A), P2).

The bug this pins: the pending system-notice window (iconKind / firstId /
payload / blocks) used to live in four loose `pendingSysInfo*` fields on
ChatViewModel. Two different threads reached them — Main (`checkContextBeforeSend`,
`/compact`, `revertCompact`, `setProviderName`) and the agent loop's IO coroutine
(`ChatContextWindow.trimContextHistoryWindow` ← `AgentLoopEngine` :658 / :864) —
with no mutual exclusion. Interleaved appends and flushes lost notices, emitted a
ChatMessage whose id belonged to one window while its toolBlocks belonged to
another, and could tear the backing ArrayList on the UI thread.

Nothing in the Kotlin compiler or in a single-threaded unit test can see this:
the shape only breaks when two threads meet. So the gate asserts the *shape of
the convergence*, not the presence of a comment:

  A. SINGLE HOME — any production declaration named `*sysinfo*` must live inside
     the SysInfoCoalescer class body. The only exception is a scheduling handle
     typed `Job?` (the timer), which is safe unlocked because every cancel
     precedes the cancelling thread's own re-assign. A `MutableList`-flavoured
     sysinfo declaration is a violation even as a `val`.
  B. GUARDED ACCESS — inside that class, every reference to its mutable state
     (auto-discovered from its own `private var/val` declarations, so renames do
     not evade the gate) must sit inside a `synchronized(...)` region or inside a
     `*Locked` helper.
  C. PUBLISH UNDER LOCK — the publish callback must be invoked from inside the
     lock (either directly, or through a `*Locked` helper). Publishing outside it
     re-introduces the lost-update the lock exists to prevent.
  D. NO RAW HANDOUT — the drained block list must be a copy behind a read-only
     `List<T>` type; handing the live MutableList out would let a caller mutate
     window state that is supposed to be private.
  E. CONVERGENCE POINT — `appendSystemInfo` must route through the coalescer and
     must not touch `pendingSysInfo*` again.

[deep-audit-1045 B-03] Two more shapes, in the Storage page:

  F. OFF-MAIN PUBLISH — no `var … by remember` Compose state may be assigned
     inside a `withContext(Dispatchers.IO)` / `(Dispatchers.Default)` block.
     Call arguments are not assignments. The state set is derived from the
     file's own declarations, so renames do not evade the gate.
  G. SINGLE INVALIDATION — the process-lifetime scan cache must have exactly one
     non-private clearing entry, and every ViewModel entry that drives a
     mutating `RootfsManager` operation (mutating = touches disk / publishes
     install state, transitively through member calls) must call it BEFORE the
     mutation. Otherwise a rootfs install / repair / reset leaves the cached
     `shellSize` in place for the life of the process.

A probe that cannot find its subject FAILS (exit 1) instead of reporting success:
a missing class means the convergence point was deleted, not that the tree is
clean.

Usage:
    python3 scripts/scan/sysinfo_state_thread_check.py <repo_root>
    python3 scripts/scan/sysinfo_state_thread_check.py --selftest
"""

import os
import re
import sys
import tempfile

PKG = "src/android/app/src/main/java/com/rikkaminis/app"
CHAT = PKG + "/ui/chat"
CLASS_NAME = "SysInfoCoalescer"
SELFTEST_MARKER = "SYSINFO-STATE SELFTEST=PASS"

# Words that mark a line as a comment rather than code (KDoc/line/block).
def blank_comments(text):
    """Replace comment and string-literal *contents* with spaces, keeping every
    newline and every code character in place (line numbers stay valid)."""
    out = []
    i, n = 0, len(text)
    in_line, in_block, in_str = False, False, False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
                out.append(ch)
            else:
                out.append(" ")
        elif in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                out.append("  ")
                i += 1
            else:
                out.append("\n" if ch == "\n" else " ")
        elif in_str:
            if ch == "\\":
                out.append("  ")
                i += 1
            elif ch == '"':
                in_str = False
                out.append(ch)
            else:
                out.append("\n" if ch == "\n" else " ")
        else:
            if ch == "/" and nxt == "/":
                in_line = True
                out.append("  ")
                i += 1
            elif ch == "/" and nxt == "*":
                in_block = True
                out.append("  ")
                i += 1
            elif ch == '"':
                in_str = True
                out.append(ch)
            else:
                out.append(ch)
        i += 1
    return "".join(out)


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def find_class_span(text, name):
    """(start_line, end_line) 0-based inclusive for `class <name>` (brace balanced)."""
    code = blank_comments(text)
    m = re.search(r"^\s*(?:internal |private |public )?class\s+%s\b" % re.escape(name),
                  code, re.M)
    if not m:
        return None
    start = code[: m.start()].count("\n")
    depth, opened = 0, False
    for idx, ch in enumerate(code[m.start():]):
        if ch == "{":
            depth += 1
            opened = True
        elif ch == "}":
            depth -= 1
        if opened and depth == 0:
            end = code[: m.start() + idx].count("\n")
            return start, end
    return None


def sync_regions(code):
    """Set of line numbers inside a `synchronized(...)` brace region (1-based)."""
    inside = set()
    stack = []  # (depth_owner_bool) per open brace
    pending_sync = False
    line = 1
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if ch == "\n":
            line += 1
        elif ch == "(":
            pending_sync = False
        elif ch == "s":
            m = re.match(r"synchronized\s*\(", code[i:])
            if m:
                pending_sync = True
                i += m.end() - 1
        elif ch == "{":
            stack.append(pending_sync)
            pending_sync = False
        elif ch == "}":
            if stack:
                stack.pop()
        if any(stack):
            inside.add(line)
        i += 1
    return inside


def fun_spans(code, *, class_lo, class_hi):
    """{name: (lo, hi)} 1-based inclusive spans of class-level `fun`s."""
    spans, lines = {}, code.split("\n")
    for idx in range(class_lo - 1, min(class_hi, len(lines))):
        m = re.match(r"^    (?:private |internal |public |override )*fun\s+(\w+)", lines[idx])
        if not m:
            continue
        depth, opened, end = 0, False, idx
        for j in range(idx, min(class_hi, len(lines))):
            depth += lines[j].count("{") - lines[j].count("}")
            if "{" in lines[j]:
                opened = True
            if opened and depth == 0:
                end = j
                break
        spans[m.group(1)] = (idx + 1, end + 1)
    return spans


def iter_prod_kt(repo_root):
    base = os.path.join(repo_root, "src/android/app/src/main/java")
    for dirpath, _dirs, files in os.walk(base):
        if os.sep + "test" in dirpath:
            continue
        for fn in files:
            if fn.endswith(".kt"):
                yield os.path.join(dirpath, fn)


DECL = re.compile(
    r"^\s*(?:private |internal |protected |public |@Volatile )*(var|val)\s+"
    r"(\w*[sS]ys[Ii]nfo\w*)\s*(?::\s*([^=\n]+))?(=)?", re.M)


def rule_single_home(repo_root, class_file, class_span):
    """A: sysinfo state lives only inside the class (Job? handle excepted)."""
    problems = []
    for path in iter_prod_kt(repo_root):
        text = read(path)
        if text is None:
            continue
        rel = os.path.relpath(path, repo_root)
        in_class = rel == class_file
        for m in DECL.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            if in_class and class_span and class_span[0] + 1 <= line_no <= class_span[1] + 1:
                continue
            decl_line = text.split("\n")[line_no - 1].strip()
            if re.search(r":\s*Job\?", decl_line):
                continue  # scheduling handle: documented exception
            if CLASS_NAME in decl_line:
                continue  # the coalescer instance itself
            kind = m.group(1)
            why = "mutable collection" if re.search(r"MutableList|mutableListOf|ArrayList",
                                                    decl_line) else kind
            problems.append("%s:%d [A single-home] %s declaration `%s` outside %s"
                            % (rel, line_no, why, m.group(2), CLASS_NAME))
    return problems


def rule_guarded_access(class_text, class_span, code):
    """B + C + D on the coalescer source."""
    problems = []
    lo, hi = class_span
    lines = class_text.split("\n")
    code_lines = code.split("\n")
    inside = sync_regions(code)
    spans = fun_spans(code, class_lo=lo + 1, class_hi=hi + 1)
    locked_lines = set()
    for name, (a, b) in spans.items():
        if name.endswith("Locked"):
            locked_lines.update(range(a, b + 1))

    # mutable state = the class's own private var/val declarations
    state = []
    for idx in range(lo, hi + 1):
        m = re.match(r"^    private (?:var|val)\s+(\w+)", lines[idx])
        if m:
            state.append((m.group(1), idx + 1))
    if not state:
        problems.append("%s:%d [B guarded-access] no private state found in %s"
                        % ("", lo + 1, CLASS_NAME))

    for name, decl_line in state:
        pat = re.compile(r"\b%s\b" % re.escape(name))
        for idx in range(lo, hi + 1):
            line_no = idx + 1
            if line_no == decl_line:
                continue
            src = code_lines[idx]
            if not pat.search(src):
                continue
            # another member's declaration (e.g. `val blocks: List<T>,` in the
            # Window class) is not an access to this class's mutable state
            if re.match(r"^\s*(?:private |internal |protected |public )?(?:val|var)\s+\w+",
                        src):
                continue
            if "synchronized" in src or line_no in inside or line_no in locked_lines:
                continue
            problems.append("[B guarded-access] %s:%d reads/writes `%s` outside the lock: %s"
                            % ("", line_no, name, lines[idx].strip()))

    # C: every publish() invocation must be under the lock (or inside a *Locked helper)
    for idx in range(lo, hi + 1):
        line_no = idx + 1
        src = code_lines[idx]
        m = re.search(r"\bpublish(Locked)?\s*\(", src)
        if not m:
            continue
        if re.search(r"fun\s+\w+\s*\(\s*publish\s*:", src) or "publish:" in src:
            continue  # the parameter declaration
        if "synchronized" in src or line_no in inside or line_no in locked_lines:
            continue
        problems.append("[C publish-under-lock] line %d calls publish outside the lock: %s"
                        % (line_no, lines[idx].strip()))

    # D: the drained list must be handed out as a copy behind a read-only type
    if not re.search(r"val\s+blocks\s*:\s*List<", class_text):
        problems.append("[D no-raw-handout] Window.blocks is not declared as List<T>")
    if not window_ctor_args(class_text):
        problems.append("[D no-raw-handout] no Window(...) construction found")
    for args in window_ctor_args(class_text):
        m = re.search(r"blocks\s*=\s*([^,)]+)", args)
        if m is None:
            problems.append("[D no-raw-handout] Window(...) without blocks: %s" % args.strip())
        elif not re.search(r"blocks\.(toList|toMutableList)\s*\(", m.group(1)):
            problems.append("[D no-raw-handout] window built with a live list: %s"
                            % m.group(0).strip())
    return problems


def window_ctor_args(class_text):
    """Argument texts of every `Window(...)` *construction* (declaration skipped)."""
    out = []
    for m in re.finditer(r"(?<!class )\bWindow(?:<[^>]*>)?\(", class_text):
        start = m.end() - 1
        depth = 0
        for j in range(start, len(class_text)):
            if class_text[j] == "(":
                depth += 1
            elif class_text[j] == ")":
                depth -= 1
                if depth == 0:
                    out.append(class_text[start:j + 1])
                    break
    return out


def rule_convergence(repo_root, class_file):
    """E: appendSystemInfo routes through the coalescer, not loose fields."""
    problems = []
    for path in iter_prod_kt(repo_root):
        text = read(path)
        if text is None:
            continue
        rel = os.path.relpath(path, repo_root)
        code = blank_comments(text)
        m = re.search(r"fun\s+\w*appendSystemInfo\s*\(", code)
        if not m:
            continue
        start = code[: m.start()].count("\n")
        depth, opened, end = 0, False, start
        for j in range(start, min(start + 120, len(code.split("\n")))):
            line = code.split("\n")[j]
            depth += line.count("{") - line.count("}")
            if "{" in line:
                opened = True
            if opened and depth == 0:
                end = j
                break
        body = "\n".join(code.split("\n")[start: end + 1])
        if not re.search(r"\b\w*[Cc]oalescer\s*\.\s*\w+\s*\(", body):
            problems.append("%s:%d [E convergence] appendSystemInfo does not route "
                            "through a coalescer instance" % (rel, start + 1))
        if "pendingSysInfo" in body:
            problems.append("%s:%d [E convergence] appendSystemInfo still touches "
                            "pendingSysInfo*" % (rel, start + 1))
    return problems


def run(repo_root):
    problems = []
    class_file, class_text, class_span = None, None, None
    for path in iter_prod_kt(repo_root):
        text = read(path)
        if text is None:
            continue
        span = find_class_span(text, CLASS_NAME)
        if span:
            class_file = os.path.relpath(path, repo_root)
            class_text, class_span = text, span
            break
    if class_file is None:
        print("FAIL: no class %s in production sources — the convergence point "
              "was deleted, not fixed" % CLASS_NAME)
        return 1
    print("state home: %s:%d-%d" % (class_file, class_span[0] + 1, class_span[1] + 1))
    problems += rule_single_home(repo_root, class_file, class_span)
    problems += rule_guarded_access(class_text, class_span, blank_comments(class_text))
    problems += rule_convergence(repo_root, class_file)
    problems += rule_storage_state_off_io(repo_root)
    problems += rule_storage_cache_invalidation(repo_root)

    if problems:
        print("")
        for p in problems:
            print("  VIOLATION %s" % p)
        print("")
        print("== %d sysinfo window problem(s) ==" % len(problems))
        print("The system-notice window is shared by Main and the agent loop's IO")
        print("thread; unguarded state there loses notices and can tear the list on")
        print("the UI thread. Keep the state inside %s (locked) and route" % CLASS_NAME)
        print("appendSystemInfo through it.")
        return 1
    print("== sysinfo coalesce window has a single guarded home ==")
    print("== storage page: background dispatchers publish nothing; rootfs "
          "operations invalidate the cached scan ==")
    return 0



# ─── [deep-audit-1045 B-03] storage page: off-Main state + cache invalidation ─
#
# The Storage page recomputes a process-lifetime snapshot (rootfs lstat walk,
# DB size, per-session recursive sizing, orphan scan) and keeps it in
# `StorageSnapshotCache` so a re-entry does not repeat the storm. Two shapes
# went wrong and neither is visible to a single-threaded test:
#
#   F. OFF-MAIN PUBLISH — the composable holds its rows in `var … by remember`
#      state, and six of those variables (plus three in the per-session detail
#      screen) were assigned INSIDE `withContext(Dispatchers.IO)`. Compose state
#      has no cross-thread happens-before with the frame rendering it, and two
#      overlapping reloads can interleave assignments, so rows / totals / cache
#      can each come from a different scan. The criterion derives the state set
#      from the file's own `var … by remember` declarations (renames do not
#      evade it) and flags any ASSIGNMENT to one of them inside a
#      `Dispatchers.IO/Default` block. Call arguments (`name = value` inside a
#      call's parentheses) are not assignments and are skipped.
#
#   G. SINGLE INVALIDATION — the cache must have one non-private clearing entry,
#      and every ViewModel entry that drives a mutating `RootfsManager`
#      operation must call it BEFORE the mutation. Without it the cached
#      `shellSize` outlives the operation that rewrote the tree (the rescan path
#      reuses the cached totals, it does not re-walk the rootfs), i.e. the shell
#      row is pinned for the life of the process. The mutating member set is
#      derived from `RootfsManager` itself (a disk/state mutation, or a call to
#      one), so a renamed or newly added lifecycle operation is covered on the
#      day it lands.

STORAGE_SCREEN = PKG + "/ui/settings/StorageManagementScreen.kt"
ROOTFS_VM = PKG + "/ui/sandbox/RootfsManagementViewModel.kt"
ROOTFS_MANAGER = PKG + "/sandbox/RootfsManager.kt"

OFF_MAIN_CALL = re.compile(r"withContext\(\s*Dispatchers\.(?:IO|Default)\s*\)\s*\{")
REMEMBER_VAR = re.compile(r"^\s*var\s+(\w+)\s*(?::\s*[^=\n]+)?\s+by\s+remember\b", re.M)
CACHE_CLEAR = "StorageSnapshotCache.snapshot = null"

# Signals that a RootfsManager member mutates the rootfs or its published state.
MUTATE_SIGNS = (
    r"installState\.value\s*=",
    r"deleteRecursively",
    r"\.delete\(",
    r"mkdirs\(",
    r"copyTo\(",
    r"writeText\(",
    r"\.renameTo\(",
)


def brace_span(code, brace_idx):
    """(first line, last line) 1-based of the `{`-block at [brace_idx]."""
    depth = 0
    for j in range(brace_idx, len(code)):
        ch = code[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (code.count("\n", 0, brace_idx) + 1, code.count("\n", 0, j) + 1)
    return None


def fun_body_span(code, after_params_idx):
    """Span of a member's `{`-body, or None for an expression / abstract body.

    Scanning from just after the parameter list's `(`: the first `=` at paren
    depth 0 means `fun f(): T = expr`, which has no body to inspect. Assuming
    the next `{` is the body instead would make such a member swallow the NEXT
    member's body — and a read-only member that swallowed a mutating one would
    be classified as mutating.
    """
    depth, i = 1, after_params_idx
    while i < len(code):
        ch = code[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                j = i + 1
                while j < len(code) and code[j] not in "={":
                    j += 1
                if j >= len(code):
                    return None
                if code[j] == "{":
                    return brace_span(code, j)
                # `= expr`. The house shape for a suspend member is
                # `= withContext(Dispatchers.IO) { … }`, so the mutation lives in
                # that trailing lambda — take its span. A genuinely bodiless
                # expression (`= true`) gets just its own line, and a member that
                # starts before any `{` ends the search (otherwise this would
                # swallow the NEXT member's body, which is how a read-only
                # member would be mistaken for a mutating one).
                k = code.find("{", j)
                eq_line = code.count("\n", 0, j)
                if k < 0:
                    return (eq_line + 1, eq_line + 1)
                seg = code[j:k]
                if re.search(r"\n\s{0,4}(?:(?:private|internal|public|override|suspend|inline)\s+)*"
                             r"(?:fun|val|var|class|object)\s+\w+", seg):
                    return (eq_line + 1, eq_line + 1)
                return brace_span(code, k)
        i += 1
    return None


def paren_depths(lines):
    """Paren depth at the START of each line (0 = statement position)."""
    out, depth = [], 0
    for ln in lines:
        out.append(depth)
        depth += ln.count("(") - ln.count(")")
        if depth < 0:
            depth = 0
    return out


def off_main_blocks(code):
    out = []
    for m in OFF_MAIN_CALL.finditer(code):
        span = brace_span(code, m.end() - 1)
        if span:
            out.append(span)
    return out


def rule_storage_state_off_io(repo_root):
    """F: no Compose state is assigned from a background dispatcher."""
    text = read(os.path.join(repo_root, STORAGE_SCREEN))
    if text is None:
        return ["%s [F storage-off-main] file missing — the subject of this "
                "criterion was moved or deleted" % STORAGE_SCREEN]
    code = blank_comments(text)
    names = sorted(set(REMEMBER_VAR.findall(code)))
    if not names:
        return ["%s [F storage-off-main] no `var ... by remember` state found — "
                "the subject of this criterion was deleted" % STORAGE_SCREEN]
    blocks = off_main_blocks(code)
    if not blocks:
        return ["%s [F storage-off-main] no withContext(Dispatchers.IO|Default) "
                "block found — the scans moved, not the writes" % STORAGE_SCREEN]
    lines = code.split("\n")
    depths = paren_depths(lines)
    problems = []
    for lo, hi in blocks:
        shadowed = set()
        for ln in range(lo, hi + 1):
            stripped = lines[ln - 1].strip()
            decl = re.match(r"(?:val|var)\s+(\w+)\b", stripped)
            if decl:
                shadowed.add(decl.group(1))  # a local of the same name wins
                continue
            if depths[ln - 1] > 0:
                continue  # inside a call's parentheses: `name = value` is an argument
            for name in names:
                if name in shadowed:
                    continue
                if re.match(r"%s\s*(?:\+=|-=|=)(?!=)" % re.escape(name), stripped):
                    problems.append("[F storage-off-main] %s:%d assigns Compose state "
                                    "`%s` on a background dispatcher: %s"
                                    % (STORAGE_SCREEN, ln, name, stripped))
    return problems


def invalidation_entry(text):
    """(name, span) of the file-level non-private cache-clearing entry."""
    code = blank_comments(text)
    for m in re.finditer(r"^(?:internal |public )?fun\s+(\w+)\s*\(\s*\)\s*\{", code, re.M):
        span = brace_span(code, m.end() - 1)
        if not span:
            continue
        body = "\n".join(code.split("\n")[span[0] - 1: span[1]])
        if CACHE_CLEAR in body:
            return m.group(1), span
    return None


def mutating_manager_members(manager_text):
    """RootfsManager's non-private members that mutate disk / published state.

    Transitive: a member that only calls a mutating one (e.g. `autoRepair` →
    `reset`) is mutating too, so a new lifecycle op cannot slip past the gate
    by delegating.
    """
    code = blank_comments(manager_text)
    bodies = {}
    for m in re.finditer(r"^    (?!private )(?:internal |public )?(?:suspend )?fun\s+(\w+)\s*\(",
                         code, re.M):
        span = fun_body_span(code, m.end())
        if not span:
            continue
        bodies[m.group(1)] = "\n".join(code.split("\n")[span[0] - 1: span[1]])
    mut = {n for n, b in bodies.items() if any(re.search(s, b) for s in MUTATE_SIGNS)}
    changed = True
    while changed:
        changed = False
        for name, body in bodies.items():
            if name in mut:
                continue
            if any(re.search(r"\b%s\s*\(" % re.escape(m), body) for m in mut):
                mut.add(name)
                changed = True
    return mut


def lifecycle_invalidation_problems(vm_text, entry_name, mutators):
    code = blank_comments(vm_text)
    lines = code.split("\n")
    problems, seen = [], []
    for m in re.finditer(r"^    (?:private |internal |public |override )?fun\s+(\w+)\s*\(",
                         code, re.M):
        span = fun_body_span(code, m.end())
        if not span:
            continue
        name = m.group(1)
        body = lines[span[0] - 1: span[1]]
        calls = [(idx, c.group(1)) for idx, ln in enumerate(body)
                 for c in re.finditer(r"\bmanager\.(\w+)\s*\(", ln)]
        mut = [(idx, c) for idx, c in calls if c in mutators]
        if not mut:
            continue
        seen.append(name)
        inval = [idx for idx, ln in enumerate(body)
                 if re.search(r"\b%s\s*\(" % re.escape(entry_name), ln)]
        if not inval:
            problems.append("[G cache-invalidation] %s:%d (%s) mutates the rootfs but never "
                            "invalidates the cached storage scan"
                            % (ROOTFS_VM, span[0], name))
        elif inval[0] > mut[0][0]:
            problems.append("[G cache-invalidation] %s:%d (%s) invalidates the cached storage "
                            "scan only AFTER %s() — an interruption between the two would leave "
                            "the stale total pinned" % (ROOTFS_VM, span[0], name, mut[0][1]))
    if not seen:
        problems.append("[G cache-invalidation] no ViewModel entry drives a mutating "
                        "RootfsManager call — the lifecycle moved, so this criterion saw "
                        "nothing to check")
    return problems


def rule_storage_cache_invalidation(repo_root):
    """G: one clearing entry, called before every rootfs mutation."""
    screen = read(os.path.join(repo_root, STORAGE_SCREEN))
    if screen is None:
        return ["%s [G cache-invalidation] file missing — the subject of this "
                "criterion was moved or deleted" % STORAGE_SCREEN]
    entry = invalidation_entry(screen)
    if entry is None:
        return ["%s [G cache-invalidation] no non-private entry clears the cached "
                "scan — nothing outside this file can invalidate it" % STORAGE_SCREEN]
    entry_name = entry[0]
    problems = []
    sites = [i + 1 for i, ln in enumerate(screen.split("\n"))
             if ln.strip().startswith(CACHE_CLEAR)]
    if len(sites) != 1:
        problems.append("[G cache-invalidation] %s:%s clears the cache from %d places "
                        "(single writer expected: only inside %s())"
                        % (STORAGE_SCREEN, ",".join(str(s) for s in sites), len(sites),
                           entry_name))
    vm = read(os.path.join(repo_root, ROOTFS_VM))
    mgr = read(os.path.join(repo_root, ROOTFS_MANAGER))
    if vm is None or mgr is None:
        problems.append("[G cache-invalidation] %s or %s missing — cannot verify who "
                        "invalidates the storage snapshot" % (ROOTFS_VM, ROOTFS_MANAGER))
        return problems
    mutators = mutating_manager_members(mgr)
    if not mutators:
        problems.append("[G cache-invalidation] no mutating RootfsManager member found — "
                        "the operations moved, not the invalidation")
        return problems
    return problems + lifecycle_invalidation_problems(vm, entry_name, mutators)


# ─── fixtures / selftest ─────────────────────────────────────────────────────

GOOD_COALESCER = """package com.rikkaminis.app.ui.chat

internal class SysInfoCoalescer<T>(private val idOf: (T) -> String) {

    internal class Window<T>(
        val firstId: String,
        val blocks: List<T>,
    )

    private class WindowState(val iconKind: String, val firstId: String)

    private val lock = Any()
    private var current: WindowState? = null
    private val blocks = ArrayList<T>()

    fun append(block: T, iconKind: String, publish: (Window<T>) -> Unit) {
        synchronized(lock) {
            if (current != null && current?.iconKind != iconKind) {
                publishLocked(publish)
            }
            if (current == null) {
                current = WindowState(iconKind = iconKind, firstId = idOf(block))
            }
            blocks.add(block)
        }
    }

    fun flush(publish: (Window<T>) -> Unit) {
        synchronized(lock) {
            publishLocked(publish)
        }
    }

    private fun publishLocked(publish: (Window<T>) -> Unit) {
        val state = current ?: return
        val drained = Window(firstId = state.firstId, blocks = blocks.toList())
        current = null
        blocks.clear()
        publish(drained)
    }
}
"""

GOOD_VM = """package com.rikkaminis.app.ui.chat

class ChatViewModel {
    private val sysInfoCoalescer = SysInfoCoalescer<String>(idOf = { it })
    private var sysInfoFlushJob: Job? = null

    internal fun appendSystemInfo(text: String, iconKind: String) {
        sysInfoCoalescer.append(text, iconKind) { window -> publish(window) }
        sysInfoFlushJob?.cancel()
    }

    private fun flushPendingSysInfo() {
        sysInfoCoalescer.flush { window -> publish(window) }
    }

    private fun publish(window: SysInfoCoalescer.Window<String>) {
        println(window.firstId)
    }
}
"""


GOOD_STORAGE = """package com.rikkaminis.app.ui.settings

private object StorageSnapshotCache {
    @Volatile
    var snapshot: Snapshot? = null
}

internal fun invalidateStorageSnapshot() {
    StorageSnapshotCache.snapshot = null
}

@Composable
fun StorageManagementScreen() {
    var shellSize by remember { mutableLongStateOf(0L) }
    var orphanInfo by remember {
        mutableStateOf<Report?>(null)
    }

    fun reload(force: Boolean = false) {
        scope.launch {
            val cached = StorageSnapshotCache.snapshot
            if (cached != null) {
                shellSize = cached.shellSize
                orphanInfo = cached.orphanInfo
            }
            val scanned = withContext(Dispatchers.IO) {
                val report = scan()
                Snapshot(
                    shellSize = report.totalBytes,
                    orphanInfo = report.orphans,
                )
            }
            StorageSnapshotCache.snapshot = scanned
            shellSize = scanned.shellSize
            orphanInfo = scanned.orphanInfo
        }
    }
}
"""

GOOD_VM_ROOTFS = """package com.rikkaminis.app.ui.sandbox

class RootfsManagementViewModel {
    fun install(ctx: Context) {
        val manager = RootfsManager.getInstance(ctx)
        invalidateStorageSnapshot()
        viewModelScope.launch {
            manager.installIfNeeded()
        }
    }

    fun repairRootfs(ctx: Context) {
        val manager = RootfsManager.getInstance(ctx)
        viewModelScope.launch {
            val initial = withContext(Dispatchers.IO) {
                manager.verifyIntegrity()
            }
            if (initial.healthy) return@launch
            invalidateStorageSnapshot()
            val repaired = manager.autoRepair()
        }
    }
}
"""

GOOD_MANAGER = """package com.rikkaminis.app.ui.sandbox

class RootfsManager {
    fun verifyIntegrity(): Boolean = true

    suspend fun installIfNeeded() {
        _installState.value = RootfsInstallState.Preparing
    }

    suspend fun reset() {
        rootfsDir.deleteRecursively()
        installIfNeeded()
    }

    suspend fun autoRepair(): Boolean {
        reset()
        return true
    }
}
"""


def make_tree(files):
    root = tempfile.mkdtemp(prefix="sysinfo-state-selftest-")
    for rel, content in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    return root


def selftest():
    arms = []
    good = {
        CHAT + "/ChatViewModel.kt": GOOD_VM + "\n" + GOOD_COALESCER,
        STORAGE_SCREEN: GOOD_STORAGE,
        ROOTFS_VM: GOOD_VM_ROOTFS,
        ROOTFS_MANAGER: GOOD_MANAGER,
    }
    root = make_tree(good)
    arms.append(("compliant fixture is green", run(root) == 0))
    _rm(root)

    bad = dict(good)
    bad[CHAT + "/ChatViewModel.kt"] = (
        GOOD_VM.replace("    private var sysInfoFlushJob: Job? = null\n",
                        "    private var sysInfoFlushJob: Job? = null\n"
                        "    private val pendingSysInfoBlocks = mutableListOf<String>()\n"))
    root = make_tree(bad)
    arms.append(("loose state outside the class is red", run(root) == 1))
    _rm(root)

    bad = dict(good)
    bad[CHAT + "/ChatViewModel.kt"] = (GOOD_VM + "\n" + GOOD_COALESCER).replace(
        "        synchronized(lock) {\n            publishLocked(publish)\n        }",
        "        publishLocked(publish)")
    root = make_tree(bad)
    arms.append(("publish outside the lock is red", run(root) == 1))
    _rm(root)

    bad = dict(good)
    bad[CHAT + "/ChatViewModel.kt"] = (GOOD_VM + "\n" + GOOD_COALESCER).replace(
        "blocks = blocks.toList()", "blocks = blocks")
    root = make_tree(bad)
    arms.append(("handing out the live list is red", run(root) == 1))
    _rm(root)

    bad = dict(good)
    bad[CHAT + "/ChatViewModel.kt"] = GOOD_COALESCER + "\n" + GOOD_VM.replace(
        "        sysInfoCoalescer.append(text, iconKind) { window -> publish(window) }",
        "        pendingSysInfoBlocks.add(text)")
    root = make_tree(bad)
    arms.append(("unrouted appendSystemInfo is red", run(root) == 1))
    _rm(root)

    bad = dict(good)
    bad[CHAT + "/ChatViewModel.kt"] = GOOD_VM.replace(
        "    private var sysInfoFlushJob: Job? = null",
        "    private var sysInfoWide: MutableList<String> = mutableListOf()")
    root = make_tree(bad)
    arms.append(("mutable-collection sysinfo state is red", run(root) == 1))
    _rm(root)

    root = make_tree({CHAT + "/ChatViewModel.kt": "package x\nclass A\n"})
    arms.append(("missing coalescer class fails loudly", run(root) == 1))
    _rm(root)

    # ── [deep-audit-1045 B-03] storage page ────────────────────────────────

    bad = dict(good)
    bad[STORAGE_SCREEN] = GOOD_STORAGE.replace(
        "                val report = scan()\n",
        "                shellSize = 1L\n                val report = scan()\n")
    root = make_tree(bad)
    arms.append(("state written on the IO dispatcher is red", run(root) == 1))
    _rm(root)

    named = dict(good)
    named[STORAGE_SCREEN] = GOOD_STORAGE.replace(
        "                val report = scan()\n",
        "                val report = scan()\n"
        "                val ignored = Snapshot(\n"
        "                    shellSize = report.totalBytes,\n"
        "                )\n")
    root = make_tree(named)
    arms.append(("a call argument inside IO is not a state write", run(root) == 0))
    _rm(root)

    bad = dict(good)
    bad[STORAGE_SCREEN] = GOOD_STORAGE.replace(
        "internal fun invalidateStorageSnapshot", "private fun invalidateStorageSnapshot")
    root = make_tree(bad)
    arms.append(("a private cache-clearing entry is red", run(root) == 1))
    _rm(root)

    bad = dict(good)
    bad[STORAGE_SCREEN] = GOOD_STORAGE + (
        "\ninternal fun clearAgain() {\n"
        "    StorageSnapshotCache.snapshot = null\n"
        "}\n")
    root = make_tree(bad)
    arms.append(("a second cache-clearing site is red", run(root) == 1))
    _rm(root)

    bad = dict(good)
    bad[ROOTFS_VM] = GOOD_VM_ROOTFS.replace("        invalidateStorageSnapshot()\n", "")
    root = make_tree(bad)
    arms.append(("a rootfs mutation that never invalidates is red", run(root) == 1))
    _rm(root)

    bad = dict(good)
    bad[ROOTFS_VM] = GOOD_VM_ROOTFS.replace(
        "        invalidateStorageSnapshot()\n"
        "        viewModelScope.launch {\n"
        "            manager.installIfNeeded()\n"
        "        }",
        "        viewModelScope.launch {\n"
        "            manager.installIfNeeded()\n"
        "        }\n"
        "        invalidateStorageSnapshot()")
    root = make_tree(bad)
    arms.append(("invalidating only after the mutation is red", run(root) == 1))
    _rm(root)

    bad = dict(good)
    bad.pop(STORAGE_SCREEN, None)
    root = make_tree(bad)
    arms.append(("deleting the storage screen fails loudly", run(root) == 1))
    _rm(root)

    print("")
    print("selftest arms:")
    ok = True
    for name, passed in arms:
        print("  %s %s" % ("PASS" if passed else "FAIL", name))
        ok = ok and passed
    if not ok:
        print("SELFTEST FAIL")
        return 1
    print(SELFTEST_MARKER)
    return 0


def _rm(root):
    import shutil
    shutil.rmtree(root, ignore_errors=True)


def main(argv):
    args = [a for a in argv[1:] if a != "--selftest"]
    if "--selftest" in argv:
        return selftest()
    root = os.path.abspath(args[0]) if args else os.path.abspath(".")
    return run(root)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

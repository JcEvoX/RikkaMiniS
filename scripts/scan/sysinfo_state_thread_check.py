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
    return 0


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
    good = {CHAT + "/ChatViewModel.kt": GOOD_VM + "\n" + GOOD_COALESCER}
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

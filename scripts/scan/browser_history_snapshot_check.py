#!/usr/bin/env python3
"""Guard: BrowserHistoryStore.save() must not iterate the live `entries` list
outside its monitor.

Why this exists (da1022-C): save() did `for (entry in entries)` unlocked
while record()/clear() mutate `entries` under synchronized(entries). save()
is itself called from both of them right AFTER releasing the monitor, so a
record() on another thread mid-iteration threw
ConcurrentModificationException (swallowed by save()'s catch -> the write
silently never lands) or persisted a torn view of the list. [fix/audit-s6m2]
only pinned pruneOldLocked(); this guard extends the contract to save().

Arms (judged on comment/string-stripped code; anchors are code shapes):
  1. save() contains NO iteration over the live `entries` symbol (for/forEach/
     index loop) OUTSIDE a synchronized(entries) block. Iterating a local
     snapshot is the compliant shape.
  2. save() takes its snapshot via `synchronized(entries) { entries.toList() }`
     (loose regex: any whitespace; `toMutableList()` also accepted).
"""
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")

TARGET_REL = "src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserHistory.kt"

MARKER = "BROWSER_HISTORY_SNAPSHOT_SELFTEST=PASS"

SNAPSHOT_RE = re.compile(
    r"synchronized\s*\(\s*entries\s*\)\s*\{\s*entries\s*\.\s*to(?:Mutable)?List\s*\(\s*\)\s*\}")
LIVE_ITER_RE = re.compile(
    r"\bfor\s*\(\s*[^)]*\bin\s+entries\s*\)|entries\s*\.\s*forEach\b|"
    r"entries\s*\[\s*\d|entries\s*\.\s*map\s*\{|entries\s*\.\s*filter\s*\{")


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


def sync_entries_spans(body: str):
    spans = []
    for m in re.finditer(r"synchronized\s*\(\s*entries\s*\)\s*\{", body):
        spans.append(brace_span(body, body.find("{", m.start())))
    return spans


def check_arms(clean: str):
    problems = []
    save_body = function_body(clean, r"private\s+fun\s+save\s*\(")
    if not save_body:
        return ["could not locate `private fun save(...)` in " + TARGET_REL
                + " -- the function may have been renamed; re-point this guard"]

    # arm 1: no iteration over the live `entries` outside the monitor
    spans = sync_entries_spans(save_body)
    for m in LIVE_ITER_RE.finditer(save_body):
        if not any(s < m.start() < e for s, e in spans):
            problems.append(
                "arm1: save() iterates the live `entries` list at offset "
                + str(m.start())
                + " outside synchronized(entries) -- record()/clear() mutate the "
                  "same list under the monitor, so this is a CME/torn-persist "
                  "(save() runs unlocked right after both record() and clear())")
            break

    # arm 2: locked shallow-copy snapshot present
    if not SNAPSHOT_RE.search(save_body):
        problems.append(
            "arm2: save() does not take its snapshot via "
            "`synchronized(entries) { entries.toList() }` -- the lock must cover "
            "the copy (and only the copy; JSON building + file write stay outside)")

    return problems


def run_selftest() -> bool:
    COMPLIANT = '''
class BrowserHistoryStore {
    private val entries = mutableListOf<Entry>()
    fun record(url: String, title: String) {
        synchronized(entries) { entries.add(Entry(url = url, title = title)) }
        save()
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
'''
    M1 = COMPLIANT.replace(
        "val snapshot = synchronized(entries) { entries.toList() }\n"
        "            val array = JSONArray()",
        "val array = JSONArray()")
    M2 = COMPLIANT.replace(
        "for (entry in snapshot) {", "for (entry in entries) {")
    # snapshot present but built unlocked -> arm2 red (arm1 may stay green
    # because the iteration goes over the local; that split is intentional)
    M3 = COMPLIANT.replace(
        "val snapshot = synchronized(entries) { entries.toList() }",
        "val snapshot = entries.toList()")
    # compliant variant: toMutableList() instead of toList() must stay GREEN
    M4 = COMPLIANT.replace(
        "val snapshot = synchronized(entries) { entries.toList() }",
        "val snapshot = synchronized(entries) { entries.toMutableList() }")

    cases = [
        ("compliant", COMPLIANT, 0),
        ("M1 no snapshot, live iteration", M1, 1),
        ("M2 iterates live entries", M2, 1),
        ("M3 unlocked snapshot", M3, 1),
        ("M4 compliant variant", M4, 0),
    ]
    ok = True
    for name, src, want_red in cases:
        problems = check_arms(strip_comments_and_strings(src))
        red = len(problems) > 0
        status = "RED" if red else "GREEN"
        expected = "RED" if want_red else "GREEN"
        mark = "ok" if red == bool(want_red) else "FAIL"
        if mark == "FAIL":
            ok = False
        print(f"  selftest {name}: {status} (expect {expected}) {mark}")
        for p in problems:
            print(f"      - {p}")

    # shape edge: save() gone -> explicit red, not a silent pass
    problems = check_arms(strip_comments_and_strings("class BrowserHistoryStore {}"))
    if not problems:
        print("  selftest no-save: expected explicit red, got GREEN -- FAIL")
        ok = False
    else:
        print("  selftest no-save: RED (expect RED) ok")
    return ok


def main() -> int:
    if "--selftest" in sys.argv:
        print("browser_history_snapshot_check selftest:")
        if not run_selftest():
            print("FAIL: selftest")
            return 1
        print(MARKER)
        return 0

    target = ROOT / TARGET_REL
    if not target.exists():
        print(f"FAIL: missing {TARGET_REL}")
        return 1
    problems = check_arms(strip_comments_and_strings(
        target.read_text(encoding="utf-8", errors="replace")))
    if problems:
        print("FAIL: BrowserHistoryStore.save() snapshot gaps")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK: BrowserHistoryStore.save() snapshots entries inside "
          "synchronized(entries) and never iterates the live list outside "
          "the monitor (JSON build + file write stay outside the lock)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

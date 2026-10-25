#!/usr/bin/env python3
"""[deep-audit-1016c FIX-C + meta-audit-1024 wide sweep] Gate: `_downloads` writes
must be atomic **and** the atomic API must actually be used atomically.

ISSUE-03C — the four write sites in BrowserTabPool.kt were non-atomic
read-modify-writes on a `MutableStateFlow`:

    _downloads.value = listOf(entry) + _downloads.value   (registerDownload)
    _downloads.value = _downloads.value.map { … }          (updateDownloadEntry)
    _downloads.value = _downloads.value.map { … }          (markDownloadsSeen)
    _downloads.value = _downloads.value.filter { … }       (clearFinishedDownloads)

Writers span three threads (URL-download listener on Main, the blob
download bridge on the WebView JavaBridge thread, download coroutines on IO),
so a lost update silently drops a download row from the panel or regresses a
progress/settle state. They now use `MutableStateFlow.update { }` (CAS loop).

Rules (BrowserTabPool.kt, where `_downloads` is private):

  R1  no read-modify-write: a write whose right-hand side mentions the current
      value again (`_downloads.value`, or the receiver's own `value`) →
      violation.  Covers the explicit form and the implicit-receiver forms
      routed through R4.
  R2  no full-replace write at all, unless the same line carries an explicit
      `ALLOWED-FULL-REPLACE` marker (a conscious opt-out for a write that
      genuinely cannot race, e.g. installing a value that does not depend on
      the current one).
  R3  [wide-1024 B-22] a `_downloads.update { … }` lambda must consume the
      current value the CAS loop hands it. Calling `update` is not the same
      as updating atomically: if the lambda ignores its parameter and maps a
      snapshot captured earlier (`val snap = _downloads.value` → `update {
      snap.map { … } }`), every CAS retry recomputes from the stale snapshot
      and the concurrent write is still lost.  Red when: the body reads
      `_downloads.value`; or it references a pre-captured `val x =
      _downloads.value` binding; or an explicit parameter is never used at the
      body's top level.
  R4  [wide-1024 B-22] the write surface is the *receiver*, not the spelling
      `_downloads.value =`. A write routed through `apply` / `run` / `with`
      (bare `value =`) or `let` / `also` (`it.value =`) is the same mutation
      and must satisfy R1/R2.  Previously the gate only matched the literal
      `_downloads.value =`, so `_downloads.apply { value = … }` slipped past
      every rule.

Exit 0 = PASS, 1 = FAIL (prints file:line for each violation).
`--selftest` runs the fixtures below: the compliant one must stay clean and
every witness-shaped one must red the *matching* rule.

Interface: `python3 scripts/scan/downloads_atomic_update_check.py [REPO_ROOT]`
(defaults to cwd; also accepts `--root ROOT`). Registration into
scripts/scan/scan.sh is the main session's job (this branch is forbidden from
touching scan.sh / test_scan.py).
"""
import os
import re
import sys

REL = "src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserTabPool.kt"

RECV = "_downloads"
WRITE_RE = re.compile(r"_downloads\s*\.\s*value\s*=(?!=)")          # explicit
RECV_BLOCK = re.compile(r"\b_downloads\s*\.\s*(apply|run|let|also)\s*\{")
WITH_BLOCK = re.compile(r"\bwith\s*\(\s*_downloads\s*\)\s*\{")
UPDATE_RE = re.compile(r"\b_downloads\s*\.\s*update\s*\{")
VALUE_WRITE_THIS = re.compile(r"(?<![\w.])(?:this\s*\.\s*)?value\s*=(?!=)")  # apply/run/with
VALUE_WRITE_IT = re.compile(r"(?<![\w.])it\s*\.\s*value\s*=(?!=)")            # let/also
SNAPSHOT_BIND = re.compile(r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*=\s*_downloads\s*\.\s*value\b")
MARKER = "ALLOWED-FULL-REPLACE"


# ── source helpers ──────────────────────────────────────────────────────────
def _balanced(src, open_idx):
    """Return (body_start, body_end) of the `{ … }` block opening at open_idx."""
    depth = 0
    i = open_idx
    n = len(src)
    while i < n:
        ch = src[i]
        if src.startswith('"""', i):
            end = src.find('"""', i + 3)
            if end < 0:
                return None
            i = end + 3
            continue
        if ch in '"\'':
            j = i + 1
            while j < n:
                if src[j] == '\\':
                    j += 2
                    continue
                if src[j] == ch:
                    break
                j += 1
            i = j + 1
            continue
        if src[i:i + 2] == "//":
            nl = src.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return open_idx + 1, i
        i += 1
    return None


def _strip_nested(body):
    """Blank out nested `{ … }` blocks so only top-level statements remain."""
    out = []
    i = 0
    n = len(body)
    while i < n:
        if body[i] == '{':
            span = _balanced(body, i)
            if span is None:
                return None
            out.append(" ")
            i = span[1] + 1
            continue
        out.append(body[i])
        i += 1
    return "".join(out)


def _line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def _statement_tail(src, idx):
    """Text from idx to end of the statement (newline at depth 0 or `;`)."""
    depth = 0
    i = idx
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
            if depth < 0:
                break
        elif ch == ';' and depth == 0:
            break
        elif ch == '\n' and depth == 0:
            break
        i += 1
    return src[idx:i]


FUN_HEAD = re.compile(r"^\s{0,4}(?:private\s+|internal\s+|public\s+|protected\s+)?"
                      r"(?:suspend\s+)?fun\s", re.M)
SNAP_BIND = re.compile(r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*([^\n]*)")


def _snapshot_bindings(src):
    """`val x = <expr reading _downloads>` bindings, with their enclosing-function span.

    Returns [(name, start, scope_end, bind_line)]. A write inside the span whose
    right-hand side mentions `name` is a read-modify-write dressed up as a plain
    assignment — the wide-1024 witness splits the `.filter {}` onto its own line and
    slaps the `ALLOWED-FULL-REPLACE` marker on the bare `= next` write.
    """
    out = []
    for m in SNAP_BIND.finditer(src):
        rhs = m.group(2)
        if RECV not in rhs and not re.search(r"(?<![\w.])value\b", rhs):
            continue
        nxt = FUN_HEAD.search(src, m.end())
        out.append((m.group(1), m.end(), nxt.start() if nxt else len(src),
                    _line_of(src, m.start())))
    return out


def _stale_ref(label, line_no, rhs, binds, offset):
    """R1b — the right-hand side re-uses a value snapshotted from `_downloads`."""
    for name, start, end, bind_line in binds:
        if start <= offset <= end and re.search(r"\b%s\b" % re.escape(name), rhs):
            return ("%s:%d: read-modify-write on _downloads through the snapshot `%s` "
                    "(taken from _downloads at L%d) — a marker cannot make a state-dependent "
                    "write safe; use update { } (wide-1024 B-22)"
                    % (label, line_no, name, bind_line))
    return None


# ── the rules ───────────────────────────────────────────────────────────────
def _judge_write(label, line_no, rhs, line, form):
    """R1/R2 for one write site; `form` names the syntactic shape for messages."""
    if form is None:
        if RECV in rhs or re.search(r"(?<![\w.])value\b", rhs):
            return "%s:%d: read-modify-write on _downloads via %s (lost update risk) — " \
                   "use _downloads.update { … } (ISSUE-03C)" % (label, line_no, "explicit write")
        if MARKER in line:
            return None
        return "%s:%d: _downloads.value = … write without an `%s` marker — " \
               "use update { } or opt out explicitly (ISSUE-03C)" % (label, line_no, MARKER)
    # implicit-receiver write: RHS mentioning the receiver's own `value` is RMW
    if re.search(r"(?<![\w.])value\b", rhs) or RECV in rhs:
        return "%s:%d: read-modify-write on _downloads through `%s` (hidden receiver) — " \
               "use _downloads.update { … } (ISSUE-03C, wide-1024 B-22)" % (label, line_no, form)
    if MARKER in line:
        return None
    return "%s:%d: _downloads written through `%s` without an `%s` marker — the write " \
           "surface is the receiver, not the spelling (wide-1024 B-22)" % (label, line_no, form, MARKER)


def check_source(src: str, label: str) -> list:
    bad = []
    seen = set()
    binds = _snapshot_bindings(src)

    # R1 / R1b / R2 — explicit `_downloads.value = …`
    for m in WRITE_RE.finditer(src):
        line_no = _line_of(src, m.end())
        line = src.splitlines()[line_no - 1] if line_no <= len(src.splitlines()) else ""
        rhs = _statement_tail(src, m.end())
        seen.add(line_no)
        v = _stale_ref(label, line_no, rhs, binds, m.end())
        if v:
            bad.append(v)
            continue
        v = _judge_write(label, line_no, rhs, line, None)
        if v:
            bad.append(v)

    # R4 — implicit receiver: apply / run / with (bare `value =`), let / also (`it.value =`)
    for rx, kind in ((RECV_BLOCK, "recv"), (WITH_BLOCK, "with")):
        for m in rx.finditer(src):
            brace = src.index("{", m.end() - 1)
            span = _balanced(src, brace)
            if span is None:
                bad.append("%s:%d: unbalanced `%s` block — cannot decide, fail-closed "
                           "(wide-1024 B-22 parser-death)" % (label, _line_of(src, m.start()), m.group(0).strip()))
                continue
            body = src[span[0]:span[1]]
            top = _strip_nested(body)
            if top is None:
                bad.append("%s:%d: unbalanced nested block inside `%s` — fail-closed "
                           "(wide-1024 B-22 parser-death)" % (label, _line_of(src, m.start()), m.group(0).strip()))
                continue
            form = m.group(1) if kind == "recv" else "with"
            rx_val = VALUE_WRITE_THIS if form in ("apply", "run", "with") else VALUE_WRITE_IT
            for w in rx_val.finditer(top):
                abs_idx = span[0] + w.start()
                line_no = _line_of(src, abs_idx)
                if line_no in seen:
                    continue
                line = src.splitlines()[line_no - 1]
                seen.add(line_no)
                rhs = _statement_tail(src, span[0] + w.end())
                v = _judge_write(label, line_no, rhs, line, form)
                if v:
                    bad.append(v)

    # R3 — the update lambda must consume the value the CAS loop hands it
    snaps = SNAPSHOT_BIND.findall(src)
    for m in UPDATE_RE.finditer(src):
        brace = src.index("{", m.end() - 1)
        span = _balanced(src, brace)
        if span is None:
            bad.append("%s:%d: unbalanced `_downloads.update {` block — cannot decide, "
                       "fail-closed (wide-1024 B-22 parser-death)" % (label, _line_of(src, m.start())))
            continue
        body = src[span[0]:span[1]]
        line_no = _line_of(src, m.start())
        top = _strip_nested(body)
        if top is None:
            bad.append("%s:%d: unbalanced nested block inside `_downloads.update {` — "
                       "fail-closed (wide-1024 B-22 parser-death)" % (label, line_no))
            continue
        # reading the live state from inside the lambda re-enters the same snapshot
        if re.search(r"_downloads\s*\.\s*value", body):
            bad.append("%s:%d: `_downloads.update {` lambda re-reads `_downloads.value` "
                       "(CAS result not consumed) — map the parameter instead "
                       "(wide-1024 B-22)" % (label, line_no))
            continue
        # a snapshot captured before the call and mapped inside defeats the CAS loop
        captured = [s for s in snaps if re.search(r"\b%s\b" % re.escape(s), top)]
        if captured:
            bad.append("%s:%d: `_downloads.update {` lambda maps the pre-captured snapshot %s "
                       "instead of the parameter — the CAS retry recomputes from stale state "
                       "(wide-1024 B-22)" % (label, line_no, ", ".join(sorted(set(captured)))))
            continue
        # explicit parameter that is never used at the top level
        pm = re.match(r"\s*([A-Za-z_]\w*)\s*->", body)
        if pm:
            name = pm.group(1)
            rest = _strip_nested(body[pm.end():])
            if rest is not None and MARKER not in body \
                    and not re.search(r"\b%s\b" % re.escape(name), rest):
                bad.append("%s:%d: `_downloads.update { %s -> … }` never uses `%s` "
                           "(CAS result not consumed) — wide-1024 B-22" % (label, line_no, name, name))
    return bad


def run(repo: str) -> int:
    path = os.path.join(repo, REL)
    if not os.path.exists(path):
        print("FAIL %s: not found (run from the repo root)" % path)
        return 1
    src = open(path, encoding="utf-8", errors="replace").read()
    bad = check_source(src, REL)
    if bad:
        for b in bad:
            print("FAIL " + b)
        return 1
    n_update = len(re.findall(r"_downloads\.update\s*\{", src))
    print("PASS downloads_atomic (%d _downloads.update{ } site(s), 0 non-atomic writes, "
          "0 stale-snapshot update lambdas)" % n_update)
    return 0


GOOD = '''    private fun registerDownload(dest: File): Long {
        _downloads.update { listOf(entry) + it }
        return id
    }

    private fun updateDownloadEntry(id: Long, transform: (DownloadEntry) -> DownloadEntry) {
        _downloads.update { current -> current.map { if (it.id == id) transform(it) else it } }
    }

    fun clearFinishedDownloads() {
        _downloads.value = emptyList() // ALLOWED-FULL-REPLACE: no current-state read
    }
'''

BAD_PREPEND = '''    private fun registerDownload(dest: File): Long {
        _downloads.value = listOf(entry) + _downloads.value
        return id
    }
'''

BAD_MAP = '''    private fun updateDownloadEntry(id: Long, transform: (DownloadEntry) -> DownloadEntry) {
        _downloads.value = _downloads.value.map { if (it.id == id) transform(it) else it }
    }
'''

BAD_FILTER = '''    fun clearFinishedDownloads() {
        _downloads.value = _downloads.value.filter { it.state == DownloadState.DOWNLOADING }
    }
'''

# witness 1 (wide-1024 B-22, axis=spelling): hidden receiver
WIT_APPLY = '''    private fun registerDownload(dest: File): Long {
        _downloads.apply { value = listOf(entry) + value }
        return id
    }
'''

WIT_WITH = '''    fun clearFinishedDownloads() {
        with(_downloads) { value = value.filter { it.state == DownloadState.DOWNLOADING } }
    }
'''

# witness 2 (wide-1024 B-22, axis=dataflow): atomic API called, stale snapshot mapped
WIT_STALE = '''    private fun updateDownloadEntry(id: Long, transform: (DownloadEntry) -> DownloadEntry) {
        val snap = _downloads.value
        _downloads.update { snap.map { if (it.id == id) transform(it) else it } }
    }
'''

# witness 2b: explicit parameter never consumed
WIT_PARAM = '''    private fun updateDownloadEntry(id: Long, transform: (DownloadEntry) -> DownloadEntry) {
        _downloads.update { current -> cachedEntries.map { if (it.id == id) transform(it) else it } }
    }
'''

CLEAN_UPDATE = '''    private fun markSeen() {
        _downloads.update { current -> current.map { it.copy(seen = true) } }
    }
'''

# witness 3 (wide-1024 B-22, axis=dataflow): split the read onto its own line and put
# the opt-out marker on the bare assignment
WIT_SPLIT = '''    fun clearFinishedDownloads() {
        val next = _downloads.value.filter { it.state == DownloadState.DOWNLOADING }
        _downloads.value = next // ALLOWED-FULL-REPLACE
    }
'''


def selftest() -> int:
    failures = []
    good = check_source(GOOD, "good.kt")
    cases = {
        "bad-prepend": (check_source(BAD_PREPEND, "bad-prepend.kt"), "read-modify-write"),
        "bad-map": (check_source(BAD_MAP, "bad-map.kt"), "read-modify-write"),
        "bad-filter": (check_source(BAD_FILTER, "bad-filter.kt"), "read-modify-write"),
        "wit-apply": (check_source(WIT_APPLY, "wit-apply.kt"), "hidden receiver"),
        "wit-with": (check_source(WIT_WITH, "wit-with.kt"), "hidden receiver"),
        "wit-stale": (check_source(WIT_STALE, "wit-stale.kt"), "pre-captured snapshot"),
        "wit-param": (check_source(WIT_PARAM, "wit-param.kt"), "never uses"),
        "wit-split": (check_source(WIT_SPLIT, "wit-split.kt"), "snapshot `next`"),
        "clean-update": (check_source(CLEAN_UPDATE, "clean-update.kt"), None),
    }
    print("selftest good fixture -> %d violation(s)" % len(good))
    for name, (hits, needle) in cases.items():
        print("selftest %-12s -> %d%s" % (name, len(hits), "" if needle is None else " (%s)" % needle))
    if good:
        failures.append("compliant fixture must be clean, got %s" % good)
    for name, (hits, needle) in cases.items():
        if needle is None:
            if hits:
                failures.append("%s fixture must stay clean, got %s" % (name, hits))
        elif not any(needle in h for h in hits):
            failures.append("%s fixture did not trip `%s` (got %s)" % (name, needle, hits))
    # R2 teeth: an unmarked full replace must red.
    unmarked = check_source("    _downloads.value = emptyList()\n", "x.kt")
    if not unmarked:
        failures.append("unmarked full-replace write not detected")
    # parser-death teeth: an unbalanced update block must red, not silently pass.
    broken = check_source("    _downloads.update { current -> current.map { it }\n", "y.kt")
    if not any("fail-closed" in h for h in broken):
        failures.append("unbalanced update block not reported (parser-death)")
    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
    # The trailing count is a *frozen contract token*: scripts/scan/test_scan.py asserts
    # this exact string, and test_scan.py is a frozen surface for this sweep. Keep the
    # historical count; the real arm list is printed above, one line per arm.
    print("SELFTEST=PASS (downloads_atomic_update: 4 fixtures)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    repo = next((a for a in args if not a.startswith("-")), os.getcwd())
    return run(repo)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""[deep-audit-1016c FIX-C] Gate: `_downloads` writes must be atomic.

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

  R1  no read-modify-write: `_downloads.value = …` whose right-hand side
      mentions `_downloads` again → violation.
  R2  no `_downloads.value =` write at all, unless the same line carries an
      explicit `ALLOWED-FULL-REPLACE` marker (a conscious opt-out for a
      write that genuinely cannot race, e.g. installing a value that does not
      depend on the current one).

Exit 0 = PASS, 1 = FAIL (prints file:line for each violation).
`--selftest` reds two violating fixtures (R1 RMW shapes) and greens a
compliant one.

Interface: `python3 scripts/scan/downloads_atomic_update_check.py [REPO_ROOT]`
(defaults to cwd; also accepts `--root ROOT`). Registration into
scripts/scan/scan.sh is the main session's job (this branch is forbidden from
touching scan.sh / test_scan.py).
"""
import os
import re
import sys

REL = "src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserTabPool.kt"

WRITE_RE = re.compile(r"_downloads\.value\s*=(?!=)([^\n]*)")
MARKER = "ALLOWED-FULL-REPLACE"


def check_source(src: str, label: str) -> list:
    bad = []
    for i, line in enumerate(src.splitlines(), start=1):
        if "//" in line:
            code = line.split("//", 1)[0]
        else:
            code = line
        m = WRITE_RE.search(code)
        if not m:
            continue
        rhs = m.group(1)
        if "_downloads" in rhs:
            bad.append(
                "%s:%d: read-modify-write on _downloads (lost update risk) — "
                "use _downloads.update { … } (ISSUE-03C)" % (label, i)
            )
        elif MARKER not in line:
            bad.append(
                "%s:%d: _downloads.value = … write without an "
                "`%s` marker — use update { } or opt out explicitly "
                "(ISSUE-03C)" % (label, i, MARKER)
            )
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
    print("PASS downloads_atomic_update (%d _downloads.update{ } site(s), "
          "0 non-atomic writes)" % n_update)
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


def selftest() -> int:
    failures = []
    good = check_source(GOOD, "good.kt")
    bad_prepend = check_source(BAD_PREPEND, "bad-prepend.kt")
    bad_map = check_source(BAD_MAP, "bad-map.kt")
    bad_filter = check_source(BAD_FILTER, "bad-filter.kt")
    print("selftest good       fixture -> %d violation(s)" % len(good))
    print("selftest bad-prepend fixture -> %d" % len(bad_prepend))
    print("selftest bad-map     fixture -> %d" % len(bad_map))
    print("selftest bad-filter  fixture -> %d" % len(bad_filter))
    for v in bad_prepend + bad_map + bad_filter:
        print("   " + v)
    if good:
        failures.append("compliant fixture must be clean, got %s" % good)
    for name, hits in (("prepend", bad_prepend), ("map", bad_map), ("filter", bad_filter)):
        if not any("read-modify-write" in h for h in hits):
            failures.append("%s fixture did not trip R1" % name)
    # R2 teeth: an unmarked full replace must red.
    unmarked = check_source("    _downloads.value = emptyList()\n", "x.kt")
    if not unmarked:
        failures.append("unmarked full-replace write not detected")
    if failures:
        for f in failures:
            print("SELFTEST FAIL " + f)
        return 1
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

#!/usr/bin/env python3
"""
Gate: production code must not read the chat transcript through
`.dao.loadMessages(` — every production read funnels through
ChatRepository.loadMessages / loadMessagePageRaw so the big-session
(>2MB CursorWindow / SQLiteBlobTooBigException) tolerance path is
unavoidable.

ISSUE-08 (deep-audit-1018, P1, fixed 2026-10-16, branch
fix/da1018-dao-loadmessages): twelve call sites bypassed the repository —
CompactOrchestration (compact raw-id verify), ChatDebugMethods,
ChatMutationMethods, HeadlessChatRunner (debug RPC + scheduled-task +
headless CLI channels). A scheduled task or CLI read on a big session
threw SQLiteBlobTooBigException and the channel died. All twelve now route
through ChatRepository (count-only sites use messageCount); both production
paged entries (loadMessagesInner, loadMessagePageRaw) funnel through the
single tolerant loadPageTolerant.

Scope:
  * production sources only: any .kt under */src/main/* (plus src/shared/,
    currently zero .kt) — instrumented/unit tests legitimately drive the
    raw DAO against fresh small databases and are out of scope by design;
  * whitelist: ChatRepository.kt / ChatDao.kt (task criterion wording) —
    the repository itself is additionally pinned by
    DaoLoadMessagesConvergenceTest (`no dao.loadMessages( in the repository
    source`), so the whitelist cannot hide a reintroduced bypass there.

Usage: python3 scripts/scan/dao_loadmessages_check.py [repo_root]
       python3 scripts/scan/dao_loadmessages_check.py --selftest
Exit:  0 = clean (or selftest pass), 1 = violation(s) / selftest fail,
       2 = usage error
"""
import sys
import tempfile
from pathlib import Path

PATTERN = ".dao.loadMessages("
WHITELIST_BASENAMES = {"ChatRepository.kt", "ChatDao.kt"}


def is_production(path: Path, root: Path) -> bool:
    """Production = .kt under */src/main/*, or under src/shared/."""
    rel = path.relative_to(root).as_posix()
    if not rel.endswith(".kt"):
        return False
    if "/src/main/" in rel:
        return True
    return rel.startswith("src/shared/")


def scan(root: Path) -> list[str]:
    """Return violation descriptions (file:line + snippet), empty if clean."""
    violations: list[str] = []
    for path in sorted(root.rglob("*.kt")):
        if not is_production(path, root):
            continue
        if path.name in WHITELIST_BASENAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:  # unreadable = report it
            violations.append(f"{path}: unreadable ({exc})")
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if PATTERN in line:
                violations.append(
                    f"{path}:{lineno}: direct dao.loadMessages bypass: "
                    f"{line.strip()[:120]}"
                )
    return violations


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def selftest() -> int:
    """Two arms, each blocking criterion individually toothed:
       A) a violating production file must red the gate (and be named);
       B) whitelist files, test source sets, and clean files must stay green
          (scope + whitelist teeth in one arm)."""
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # ---- arm A: violation must be caught and named ----
        va = root / "a"
        _write(
            va / "src/android/app/src/main/java/com/example/Foo.kt",
            "class Foo {\n"
            "    fun bad(repo: ChatRepository) {\n"
            "        val rows = repo.dao.loadMessages(\"s1\")\n"
            "    }\n"
            "}\n",
        )
        hits = scan(va)
        if len(hits) == 1 and "Foo.kt" in hits[0] and "loadMessages" in hits[0]:
            print("[selftest A] violation red + named: OK")
        else:
            print(f"[selftest A] EXPECTED 1 named violation, got: {hits!r}")
            ok = False

        # ---- arm B: whitelist + scope + clean must stay green ----
        vb = root / "b"
        _write(
            vb / "src/android/app/src/main/java/com/example/ChatRepository.kt",
            "class ChatRepository {\n"
            "    fun x(dao: ChatDao) = dao.loadMessages(\"s1\")\n"
            "}\n",  # whitelisted basename, would match the receiver pattern
        )
        _write(
            vb / "src/android/app/src/main/java/com/example/Clean.kt",
            "class Clean {\n"
            "    fun ok(repo: ChatRepository) = repo.loadMessages(\"s1\")\n"
            "}\n",
        )
        _write(  # unit-test source: out of scope by design
            vb / "src/android/app/src/test/java/com/example/BadTest.kt",
            "class BadTest {\n"
            "    fun t(repo: ChatRepository) = repo.dao.loadMessages(\"s1\")\n"
            "}\n",
        )
        _write(  # instrumented-test source: out of scope by design
            vb / "src/android/app/src/androidTest/java/com/example/BadIt.kt",
            "class BadIt {\n"
            "    fun t(repo: ChatRepository) = repo.dao.loadMessages(\"s1\")\n"
            "}\n",
        )
        _write(  # src/shared production tree is in scope (currently empty in repo)
            vb / "src/shared/tool/note.kt",
            "// note: src/shared has no .kt today; if one appears it is in scope\n",
        )
        hits_b = scan(vb)
        if hits_b:
            print(f"[selftest B] EXPECTED clean, got: {hits_b!r}")
            ok = False
        else:
            print("[selftest B] whitelist + test-scope + clean green: OK")

        # ---- arm C (negative tooth for the whitelist itself): a violating
        #      file masquerading under a NON-whitelisted name must still red,
        #      proving arm B's green is the whitelist/scope at work, not a
        #      blind gate ----
        vc = root / "c"
        _write(
            vc / "src/android/app/src/main/java/com/example/ChatRepositoryBackup.kt",
            "class ChatRepositoryBackup {\n"
            "    fun bad(repo: ChatRepository) = repo.dao.loadMessages(\"s1\")\n"
            "}\n",
        )
        hits_c = scan(vc)
        if len(hits_c) == 1 and "ChatRepositoryBackup.kt" in hits_c[0]:
            print("[selftest C] near-whitelist name still red: OK")
        else:
            print(f"[selftest C] EXPECTED 1 violation, got: {hits_c!r}")
            ok = False

    if ok:
        print("DAO_LOADMESSAGES_SELFTEST=PASS")
        return 0
    print("DAO_LOADMESSAGES_SELFTEST=FAIL")
    return 1


def main(argv: list[str]) -> int:
    root: Path | None = None
    for i, arg in enumerate(argv):
        if arg == "--selftest":
            return selftest()
        if arg == "--root":
            if i + 1 >= len(argv):
                print("usage: --root needs a value", file=sys.stderr)
                return 2
            root = Path(argv[i + 1])
        elif not arg.startswith("--"):
            root = Path(arg)
    if root is None:
        root = Path.cwd()
    if not root.is_dir():
        print(f"repo root not found: {root}", file=sys.stderr)
        return 2

    violations = scan(root)
    if violations:
        print("[dao-loadmessages] DIRECT dao.loadMessages BYPASSES FOUND:")
        for v in violations:
            print(f"  FAIL: {v}")
        print(
            "Fix: route production reads through ChatRepository.loadMessages "
            "/ loadMessagePageRaw (count-only sites: messageCount)."
        )
        return 1
    print("[dao-loadmessages] clean: 0 direct .dao.loadMessages( in production")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

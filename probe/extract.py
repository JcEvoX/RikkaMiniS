#!/usr/bin/env python3
"""[da1020-C] Extract the pure pre-parse / join pieces from the real source
files into a JVM harness. Fail-closed: every structural anchor (start AND
end) must match exactly once, otherwise the extractor aborts (EXTRACT-FAIL)
instead of producing a stale harness that would mask a real regression.
"""
import sys
from pathlib import Path

SRC = Path("src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload")
OUT = Path("probe/jvm/src/com/rikkaminis/app/sandbox/offload")

PKG = "package com.rikkaminis.app.sandbox.offload\n\n"


def read(rel: str) -> str:
    p = Path(rel)
    if not p.is_file():
        print(f"EXTRACT-FAIL: {rel} not found")
        sys.exit(2)
    return p.read_text(encoding="utf-8")


def unique(s: str, needle: str, label: str) -> None:
    n = s.count(needle)
    if n != 1:
        print(f"EXTRACT-FAIL: anchor not unique ({n}x) for {label}: {needle!r}")
        sys.exit(2)


def slice_between(src: str, start: str, end: str, label: str) -> str:
    """Extract from the start of the KDoc directly above [start] (if any) to
    the end of the line containing [end]. Both anchors must be unique."""
    unique(src, start, label + ".start")
    unique(src, end, label + ".end")
    at = src.index(start)
    # include an immediately preceding KDoc block
    head = src[:at]
    kdoc_end = head.rstrip()
    if kdoc_end.endswith("*/"):
        kstart = head.rfind("/**")
        if kstart != -1 and "//" not in head[kstart:]:
            at = kstart
    e = src.index(end, at)
    # include through the end of the LAST line of the end anchor (the anchor
    # may span two lines, e.g. `... \n        }` closing braces)
    line_end = src.find("\n", e + len(end))
    if line_end == -1:
        print(f"EXTRACT-FAIL: end anchor at EOF for {label}")
        sys.exit(2)
    return src[at:line_end] + "\n"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    # 1. OffloadArgs — full real file, no changes.
    offload_args = read(SRC / "OffloadArgs.kt")
    if "internal class OffloadArgs" not in offload_args:
        print("EXTRACT-FAIL: OffloadArgs class missing")
        sys.exit(2)
    (OUT / "OffloadArgs.kt").write_text(offload_args, encoding="utf-8")

    # 2. OffloadOutput slice: the OUTPUT_FLAGS val only (the full file pulls
    # in AppLogger; the gate pins the real literal, so stub drift is caught).
    oo = read(SRC / "OffloadOutput.kt")
    flags_line = 'val OUTPUT_FLAGS: Set<String> = setOf("compact", "quiet", "q")'
    unique(oo, flags_line, "OffloadOutput.OUTPUT_FLAGS")
    (OUT / "OffloadOutput.kt").write_text(
        PKG + "internal object OffloadOutput {\n    " + flags_line + "\n}\n",
        encoding="utf-8")

    # 3. ConfigOffloadHandler.resolveMaybeRelative — the single join impl.
    cfg = read(SRC / "ConfigOffloadHandler.kt")
    resolve_fn = slice_between(cfg,
        "internal fun resolveMaybeRelative(path: String, cwd: String?): String =",
        "    } else path",
        "resolveMaybeRelative")
    (OUT / "ConfigJoin.kt").write_text(PKG + resolve_fn, encoding="utf-8")

    # 4. BrowserUseOffloadHandler.resolveMaybeRelativeAll.
    bu = read(SRC / "BrowserUseOffloadHandler.kt")
    all_fn = slice_between(bu,
        "internal fun resolveMaybeRelativeAll(paths: List<String>, cwd: String?): List<String> =",
        "    paths.map { resolveMaybeRelative(it, cwd) }",
        "resolveMaybeRelativeAll")
    (OUT / "BrowserJoin.kt").write_text(PKG + all_fn, encoding="utf-8")

    # 5. Fastio pieces (verbatim from the companion; wrapped in an object so
    # they compile standalone — the companion is public so this is the same
    # access shape the real tests get).
    fs = read(SRC / "FastioOffloadHandler.kt")
    shorts = slice_between(fs,
        "private val SHORT_LONG_OPTIONS = setOf(",
        '"-max-entries", "-ignore-case",\n        )',
        "SHORT_LONG_OPTIONS")
    longopt = slice_between(fs,
        "internal fun normalizeLongOptionSpelling(argv: List<String>): List<String> =",
        'argv.map { if (it in SHORT_LONG_OPTIONS) "-$it" else it }',
        "normalizeLongOptionSpelling")
    tarshorts = slice_between(fs,
        "internal fun normalizeTarShorts(argv: List<String>): List<String>",
        "            return out\n        }",
        "normalizeTarShorts")
    boolflags = slice_between(fs,
        "internal fun fastioBooleanFlags(isTarCommand: Boolean): Set<String>",
        '                setOf("recursive", "force", "ignore-case")\n            }',
        "fastioBooleanFlags")
    subtok = slice_between(fs,
        "internal fun firstSubcommandToken(rawArgv: List<String>): String?",
        "            return null\n        }",
        "firstSubcommandToken")
    # sanity: the expander guard must call the shared helper in the REAL source
    if 'firstSubcommandToken(argv) != "tar"' not in tarshorts:
        print("EXTRACT-FAIL: normalizeTarShorts guard does not call the shared helper")
        sys.exit(2)
    if '"create", "extract", "gzip", "verbose",' not in boolflags:
        print("EXTRACT-FAIL: fastioBooleanFlags tar verbs missing")
        sys.exit(2)
    if "OffloadOutput.OUTPUT_FLAGS" not in subtok:
        print("EXTRACT-FAIL: firstSubcommandToken does not read the global flag set")
        sys.exit(2)
    (OUT / "FastioPieces.kt").write_text(
        PKG + "internal object FastioPieces {\n"
        + shorts + "\n" + longopt + "\n" + tarshorts + "\n"
        + boolflags + "\n" + subtok + "\n}\n",
        encoding="utf-8")

    print("EXTRACT-OK: 5 sources ->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())

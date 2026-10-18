#!/usr/bin/env python3
"""envvar_config_group_check — [da1025-E] EnvVarEntry.group unified config-face registration.

Fixes the 6th instance of the field-evap recurrence class (GH#68 -> pinned ->
1021 reasoning -> 1022 OpenMinis compat -> 1023-13 -> 1025): EnvVarEntry gained
a `group` field wired through UI / persistence / backup, but the minis-config
surface was never registered:

  1. EnvVarsCollection.fields(forId) must return groupField(forId)
     (path "envvars.<key>.group") — else `minis-config get envvars.<key>.group`
     -> ConfigError.UnknownPath.
  2. That groupField must actually register path "envvars.$key.group"
     (or the forId variant) — a field listed but pathless is still unreachable.
  3. ConfigBuiltins' `envvars` aggregate summary linkedMapOf must contain a
     "group" key — else `minis-config get envvars` hides the grouping label.

All criteria are fail-closed: if the anchor function/block cannot be extracted
from the (comment-stripped) source, the criterion is RED, never silently
skipped.

Usage:
    python3 envvar_config_group_check.py <repo_root>   # real-tree mode
    python3 envvar_config_group_check.py --selftest    # fixture mode

Selftest ends with the tooth marker ENVVAR_GROUP_SELFTEST=PASS (missing marker
= the gate itself is broken / gutted -> red).
"""

import os
import re
import shutil
import sys
import tempfile

# --------------------------------------------------------------------------
# Comment stripping (a gate's own KDoc routinely quotes its anchors verbatim;
# match against comment-stripped text only).
# --------------------------------------------------------------------------


def strip_comments(src: str) -> str:
    out = []
    i = 0
    n = len(src)
    in_block = False
    in_line = False
    in_str = False
    str_ch = ""
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
                out.append(c)
            i += 1
            continue
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            if c == "\n":
                out.append(c)
            i += 1
            continue
        if in_str:
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(src[i + 1])
                i += 2
                continue
            if c == str_ch:
                in_str = False
            i += 1
            continue
        if c == "/" and nxt == "/":
            in_line = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if c in ('"', "'"):
            in_str = True
            str_ch = c
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# Balanced extraction (block bodies / paren blocks). Fail-closed: returns None
# when the block never balances (truncated source, wrong anchor, etc.).
# --------------------------------------------------------------------------


def extract_braced(text: str, header_re: str) -> str | None:
    """Extract the balanced {...} block that follows the first match of
    header_re. Returns None (fail-closed) if the header is absent or the
    block never balances."""
    m = re.search(header_re, text)
    if not m:
        return None
    i = m.end()
    while i < len(text) and text[i] != "{":
        if text[i] not in " \t\r\n":
            return None  # not a block body (e.g. expression body) -> fail-closed
        i += 1
    if i >= len(text):
        return None
    depth = 0
    start = i
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def extract_paren_block(text: str, start_idx: int) -> str | None:
    """Extract the balanced (...) block starting at the '(' at start_idx."""
    depth = 0
    i = start_idx
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start_idx : i + 1]
        i += 1
    return None


def func_window(text: str, header_re: str, stop_re: str = r"\n    (private |override )?fun |^}") -> str | None:
    """Declaration-text window: from the header match to the next function
    header / class end. Used for expression-body members where brace pairing
    silently grabs the WRONG block (da1025-FIX-F lesson)."""
    m = re.search(header_re, text)
    if not m:
        return None
    rest = text[m.start() :]
    m2 = re.search(stop_re, rest, re.M)
    if not m2:
        return rest
    return rest[: m2.start()]


# --------------------------------------------------------------------------
# The three criteria. Each returns None (green) or a failure string (red).
# --------------------------------------------------------------------------

ENVVARS_REL = "EnvVarsCollection.kt"
BUILTINS_REL = "ConfigBuiltins.kt"


def check_fields_registration(envvars_src: str) -> str | None:
    """Criterion 1: fields(forId)'s return list contains groupField(forId)."""
    text = strip_comments(envvars_src)
    body = extract_braced(text, r"override fun fields\(forId: String\): List<ConfigField>")
    if body is None:
        return "C1 EXTRACT_FAIL: fields(forId) block body not found (fail-closed)"
    if "groupField(forId)" not in body:
        return "C1 RED: fields(forId) return list does not register groupField(forId)"
    return None


def check_group_path(envvars_src: str) -> str | None:
    """Criterion 2: a groupField member registers path "envvars.$key.group"
    (or the forId variant)."""
    text = strip_comments(envvars_src)
    win = func_window(
        text,
        r"private fun groupField\(",
        stop_re=r"\n    private fun |\n    override fun |^}",
    )
    if win is None:
        return "C2 EXTRACT_FAIL: groupField member not found (fail-closed)"
    if re.search(r'path\s*=\s*"envvars\.\$(key|forId)\.group"', win):
        return None
    return 'C2 RED: groupField does not register path "envvars.$key.group" (or forId variant)'


def check_summary_group(builtins_src: str) -> str | None:
    """Criterion 3: the `envvars` aggregate summary block contains a "group" key."""
    text = strip_comments(builtins_src)
    anchor = re.search(r'path\s*=\s*"envvars",', text)
    if not anchor:
        return "C3 EXTRACT_FAIL: envvars summary registration (path = \"envvars\",) not found (fail-closed)"
    # the enclosing r.register( ... ) block: back up to the nearest r.register(
    # opener before the anchor, then balance parens from there.
    reg_start = text.rfind("r.register(", 0, anchor.start())
    if reg_start < 0:
        return "C3 EXTRACT_FAIL: enclosing r.register( not found (fail-closed)"
    paren_idx = text.index("(", reg_start)
    block = extract_paren_block(text, paren_idx)
    if block is None:
        return "C3 EXTRACT_FAIL: envvars summary block never balances (fail-closed)"
    if '"group" to ConfigValue.Str' not in block:
        return "C3 RED: envvars summary linkedMapOf lacks a \"group\" key"
    if '"key" to ConfigValue.Str' not in block:
        return "C3 RED: envvars summary block lost its \"key\" entry (extraction sanity)"
    return None


def check(envvars_path: str, builtins_path: str) -> list[str]:
    with open(envvars_path, encoding="utf-8") as f:
        ev = f.read()
    with open(builtins_path, encoding="utf-8") as f:
        cb = f.read()
    failures = []
    for name, fn, src in (
        ("C1", check_fields_registration, ev),
        ("C2", check_group_path, ev),
        ("C3", check_summary_group, cb),
    ):
        r = fn(src)
        if r is not None:
            failures.append(r)
    return failures


def locate(root: str) -> tuple[str, str]:
    """Locate the two production files under repo_root. Fail-closed."""
    ev = cb = None
    for dirpath, _dirnames, filenames in os.walk(root):
        if "build" in dirpath.split(os.sep) or "test" in dirpath.split(os.sep):
            continue
        for fn in filenames:
            if fn == ENVVARS_REL and ev is None:
                ev = os.path.join(dirpath, fn)
            elif fn == BUILTINS_REL and cb is None and "config" in dirpath.split(os.sep):
                cb = os.path.join(dirpath, fn)
    if ev is None:
        raise SystemExit("LOCATE_FAIL: EnvVarsCollection.kt not found under " + root)
    if cb is None:
        raise SystemExit("LOCATE_FAIL: ConfigBuiltins.kt not found under " + root)
    return ev, cb


# --------------------------------------------------------------------------
# Selftest fixtures. Each fixture is a minimal synthetic source pair crafted to
# have exactly one criterion broken (the "precisely broken by it" requirement).
# --------------------------------------------------------------------------

FIXTURE_ENVVARS_OK = '''package fake

class EnvVarsCollection(private val repo: Repo) : ConfigCollection {
    override fun fields(forId: String): List<ConfigField> {
        val entry = repo.entries.value.firstOrNull { it.key == forId } ?: return emptyList()
        return listOf(
            createdAtField(forId),
            noteField(forId),
            groupField(forId),
            HiddenField(
                path = "envvars.$forId.value",
                displayName = "Value",
            ),
        )
    }

    private fun createdAtField(key: String): ConfigField = ReadOnlyField(
        path = "envvars.$key.createdAt",
        displayName = "Created at",
        valueSchema = ConfigSchema.Str(),
        reader = { ConfigValue.Null },
    )

    private fun noteField(key: String): ConfigField = ClosureField(
        path = "envvars.$key.note",
        displayName = "Note",
        valueSchema = ConfigSchema.Str(maxLength = 500),
        reader = { ConfigValue.Null },
        writer = { v ->
            val e = repo.entries.value.firstOrNull { it.key == key }
                ?: throw ConfigError.UnknownPath("envvars.$key")
            val currentValue = repo.getValue(e.key) ?: ""
            repo.update(e.id, e.key, currentValue, v, e.group)
        },
    )

    private fun groupField(key: String): ConfigField = ClosureField(
        path = "envvars.$key.group",
        displayName = "Group",
        valueSchema = ConfigSchema.Str(maxLength = 200),
        reader = { ConfigValue.Null },
        writer = { v ->
            val e = repo.entries.value.firstOrNull { it.key == key }
                ?: throw ConfigError.UnknownPath("envvars.$key")
            val currentValue = repo.getValue(e.key) ?: ""
            repo.update(e.id, e.key, currentValue, e.note, v)
        },
    )
}
'''

FIXTURE_BUILTINS_OK = '''package fake

object ConfigBuiltins {
    fun register(r: R) {
        r.register(
            ReadOnlyField(
                path = "envvars",
                displayName = "Environment variables (summary)",
                valueSchema = ConfigSchema.Json,
                reader = {
                    ConfigValue.Arr(
                        envVarRepo.entries.value.map { e ->
                            ConfigValue.Obj(
                                linkedMapOf(
                                    "key" to ConfigValue.Str(e.key),
                                    "group" to ConfigValue.Str(e.group),
                                    "note" to ConfigValue.Str(e.note),
                                    "created_at" to ConfigValue.Str(isoFormatter.format(java.time.Instant.ofEpochMilli(e.createdAt))),
                                )
                            )
                        }
                    )
                },
            )
        )
    }
}
'''


def fixture_dir() -> str:
    d = tempfile.mkdtemp(prefix="envvar_group_selftest_")
    os.makedirs(os.path.join(d, "fake", "config", "collections"))
    with open(os.path.join(d, "fake", "config", "collections", "EnvVarsCollection.kt"), "w", encoding="utf-8") as f:
        f.write(FIXTURE_ENVVARS_OK)
    with open(os.path.join(d, "fake", "config", "ConfigBuiltins.kt"), "w", encoding="utf-8") as f:
        f.write(FIXTURE_BUILTINS_OK)
    return d


def run_fixtures() -> bool:
    ok = True
    d2 = d3 = d4 = d5 = None

    def expect(label: str, failures: list[str], want: str | None) -> bool:
        if want is None:
            if failures:
                print(f"[SELFTEST] {label}: expected GREEN, got {failures}")
                return False
            print(f"[SELFTEST] {label}: GREEN as expected")
            return True
        if len(failures) != 1 or not failures[0].startswith(want):
            print(f"[SELFTEST] {label}: expected exactly one failure starting with {want!r}, got {failures}")
            return False
        print(f"[SELFTEST] {label}: red as expected ({failures[0]})")
        return True

    # FIX-1: complete fixture -> all green
    d = fixture_dir()
    try:
        ok &= expect(
            "FIX-1 complete",
            check(
                os.path.join(d, "fake", "config", "collections", "EnvVarsCollection.kt"),
                os.path.join(d, "fake", "config", "ConfigBuiltins.kt"),
            ),
            None,
        )

        # FIX-2: fields list drops groupField -> exactly C1 red
        d2 = fixture_dir()
        p = os.path.join(d2, "fake", "config", "collections", "EnvVarsCollection.kt")
        src = open(p, encoding="utf-8").read().replace("            groupField(forId),\n", "")
        open(p, "w", encoding="utf-8").write(src)
        ok &= expect(
            "FIX-2 fields-missing",
            check(
                p,
                os.path.join(d2, "fake", "config", "ConfigBuiltins.kt"),
            ),
            "C1",
        )

        # FIX-3: groupField present in the list but its path is wrong -> exactly C2 red
        d3 = fixture_dir()
        p3 = os.path.join(d3, "fake", "config", "collections", "EnvVarsCollection.kt")
        src3 = open(p3, encoding="utf-8").read().replace('path = "envvars.$key.group",', 'path = "envvars.$key.tag",')
        open(p3, "w", encoding="utf-8").write(src3)
        ok &= expect(
            "FIX-3 path-missing",
            check(
                p3,
                os.path.join(d3, "fake", "config", "ConfigBuiltins.kt"),
            ),
            "C2",
        )

        # FIX-4: summary block drops the "group" key -> exactly C3 red
        d4 = fixture_dir()
        p4 = os.path.join(d4, "fake", "config", "ConfigBuiltins.kt")
        src4 = open(p4, encoding="utf-8").read().replace('                                    "group" to ConfigValue.Str(e.group),\n', "")
        open(p4, "w", encoding="utf-8").write(src4)
        ok &= expect(
            "FIX-4 summary-missing",
            check(
                os.path.join(d4, "fake", "config", "collections", "EnvVarsCollection.kt"),
                p4,
            ),
            "C3",
        )

        # FIX-5 (fail-closed control): truncated EnvVarsCollection (no fields
        # block at all) -> C1 must be EXTRACT_FAIL red, never silently green.
        d5 = fixture_dir()
        p5 = os.path.join(d5, "fake", "config", "collections", "EnvVarsCollection.kt")
        open(p5, "w", encoding="utf-8").write("package fake\n\nclass EnvVarsCollection {\n")
        fails5 = check(p5, os.path.join(d5, "fake", "config", "ConfigBuiltins.kt"))
        if not any(f.startswith("C1 EXTRACT_FAIL") for f in fails5):
            print(f"[SELFTEST] FIX-5 truncated: expected C1 EXTRACT_FAIL, got {fails5}")
            ok = False
        else:
            print("[SELFTEST] FIX-5 truncated: fail-closed as expected")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        for dd in (d2, d3, d4, d5):
            shutil.rmtree(dd, ignore_errors=True)

    return ok


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        if run_fixtures():
            print("ENVVAR_GROUP_SELFTEST=PASS")
            return 0
        print("ENVVAR_GROUP_SELFTEST=FAIL")
        return 1

    roots = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not roots:
        print("usage: envvar_config_group_check.py <repo_root> | --selftest")
        return 2
    ev, cb = locate(roots[0])
    failures = check(ev, cb)
    if failures:
        for f in failures:
            print(f)
        print(f"envvar_config_group_check: {len(failures)} violation(s)")
        return 1
    print("envvar_config_group_check: OK (3 criteria green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

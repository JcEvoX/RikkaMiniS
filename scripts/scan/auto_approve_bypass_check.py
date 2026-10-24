#!/usr/bin/env python3
"""
auto_approve_bypass_check — [auto-approve] structural gate.

Guarantees the "auto-approve mode" overlays stay behind their designed single
points of decision, and only there:

  1. ConfigBridge.performWriteBatch must consult ConfigAutoApprovePolicy.
     (A future edit that removes the consult would silently re-ask for every
     write — or, worse, an edit that consults nothing and skips everything
     would silently apply SENSITIVE rows.)
  2. ConfigConfirmationGate.requestConfirmation must have EXACTLY one call
     site in production code (ConfigBridge). A second call site would be a
     dialog path the auto-approve policy never sees.
  3. OffloadPermissionManager.getLevel must resolve through
     OffloadPermissionLevelPolicy (the pure precedence table), not inline
     ad-hoc overlay logic — AND the resolved value must actually BE the
     return value (see C5).
  4. AutoApproveStore must NOT be reachable from the ConfigRegistry builtins:
     the mode is UI-only, the agent must never be able to flip it via
     minis-config (that would be self-authorization).
  5. [meta-audit-1024 FIX-M1, axis=dataflow] every
     `OffloadPermissionLevelPolicy.resolve(` call inside getLevel must be
     CONSUMED as the return value — either `return OffloadPermissionLevelPolicy
     .resolve(...)`, or bound to a local (`val lvl = …`) that a later
     `return <lvl>` hands back. A bare, discarded call followed by an
     unconditional `return <level>` keeps C3 green (the symbol is still in the
     body) while voiding every user setting: all offload tools would get that
     one level. That patch was externally witnessed passing this gate.
     Fail-closed: a call whose statement cannot be attributed (unbalanced
     parens/braces) is a violation, not a skip. Zero calls stays C3's business.

Run:  python3 scripts/scan/auto_approve_bypass_check.py <repo-root>
Exit: 0 = all criteria hold, 1 = violation found (prints which).
"""
import os
import re
import shutil
import sys
import tempfile

CONFIG_BRIDGE = "src/android/app/src/main/java/com/rikkaminis/app/config/ConfigBridge.kt"
PERM_MANAGER = "src/android/app/src/main/java/com/rikkaminis/app/offload/OffloadPermissionManager.kt"
CONFIG_REGISTRY = "src/android/app/src/main/java/com/rikkaminis/app/config/ConfigRegistry.kt"
CONFIG_BUILTINS = "src/android/app/src/main/java/com/rikkaminis/app/config/ConfigBuiltins.kt"

PROD_JAVA = "src/android/app/src/main/java"


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def strip_kotlin_comments(src):
    """Remove // and /* */ comments while preserving line structure, so a
    criterion mentioned only in prose cannot satisfy the gate (same family as
    the 1039 A-04 false red)."""
    out = []
    in_block = False
    in_string = False
    string_ch = ""
    i = 0
    while i < len(src):
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        if in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            if ch == "\n":
                out.append("\n")
            i += 1
            continue
        if in_string:
            out.append(ch)
            if ch == "\\":
                if nxt:
                    out.append(nxt)
                    i += 2
                    continue
            elif ch == string_ch:
                in_string = False
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < len(src) and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if ch in "\"'":
            in_string = True
            string_ch = ch
        out.append(ch)
        i += 1
    return "".join(out)


def function_body(src, signature_re):
    """Return the body text of the first function matching signature_re
    (brace-balanced from the signature's opening brace)."""
    m = re.search(signature_re, src)
    if not m:
        return None
    open_idx = src.find("{", m.end() - 1)
    if open_idx < 0:
        return None
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx:i]
    return None


POLICY_CALL = "OffloadPermissionLevelPolicy.resolve"
RE_POLICY_CALL = re.compile(re.escape(POLICY_CALL) + r"\s*\(")
# A statement head that CONSUMES the value.
RE_RETURN_HEAD = re.compile(r"^(?:return(?:@[A-Za-z_][A-Za-z0-9_]*)?|throw)\b")
# `val name = ` / `var name: Type = ` — the local-val spelling that is only
# acceptable when a later `return name` actually hands the value back.
RE_LOCAL_BIND = re.compile(r"^(?:val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]+)?=")


def string_mask(src):
    """Per-character bitmap: 1 = inside a string literal."""
    mask = bytearray(len(src))
    quote = None
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if quote is not None:
            mask[i] = 1
            if c == "\\" and i + 1 < n:
                mask[i + 1] = 1
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            mask[i] = 1
        i += 1
    return mask


def statement_start(src, mask, idx):
    """Start offset of the statement containing `idx`.

    Backwards scan that treats `{`/`}`/`;` — AND a newline at nesting depth 0
    (Kotlin statements are newline-delimited) — as statement boundaries; any
    unbalanced opener ends the walk. Newlines INSIDE parens/braces (a wrapped
    argument list) are not boundaries. Never returns -1: on an unattributable
    call the head simply will not look like a consumer, which is the
    fail-closed direction.
    """
    depth = 0
    i = idx - 1
    while i >= 0:
        if mask[i]:
            i -= 1
            continue
        c = src[i]
        if depth == 0 and c in ";\n{}":
            return i + 1
        if c in ")]}":
            depth += 1
        elif c in "([{":
            depth -= 1
            if depth < 0:
                return i + 1
        i -= 1
    return 0


def check_policy_return(level_body):
    """C5 — every resolve() call in getLevel must be the returned value.

    Zero calls is C3's criterion (presence), so this returns [] there instead
    of double-counting the same defect.
    """
    calls = [m.start() for m in RE_POLICY_CALL.finditer(level_body)]
    if not calls:
        return []
    mask = string_mask(level_body)
    violations = []
    for idx in calls:
        head = level_body[statement_start(level_body, mask, idx):].lstrip()
        if RE_RETURN_HEAD.match(head):
            continue
        returned = False
        bind = RE_LOCAL_BIND.match(head)
        if bind:
            name = re.escape(bind.group(1))
            returned = re.search(
                r"\breturn\s+(?:@[A-Za-z_][A-Za-z0-9_]*\s+)?%s\b" % name,
                level_body,
            ) is not None
        if returned:
            continue
        violations.append(
            "C5: getLevel discards the OffloadPermissionLevelPolicy.resolve() result "
            "(call at offset %d, statement head %r) — the policy value must BE the "
            "returned value, or a local val that a later `return` hands back; a "
            "discarded call lets an unconditional level override every user setting"
            % (idx, head.splitlines()[0][:70] if head else "")
        )
    return violations


def prod_files(root):
    base = os.path.join(root, PROD_JAVA)
    hits = []
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            if name.endswith(".kt"):
                hits.append(os.path.join(dirpath, name))
    return hits


def check(root):
    failures = []

    bridge = strip_kotlin_comments(read(os.path.join(root, CONFIG_BRIDGE)))
    manager = strip_kotlin_comments(read(os.path.join(root, PERM_MANAGER)))

    # --- Criterion 1: the write path must consult the policy -----------------
    batch_body = function_body(bridge, r"fun\s+performWriteBatch\s*\(")
    if batch_body is None:
        failures.append("C1: performWriteBatch not found in ConfigBridge")
    elif "ConfigAutoApprovePolicy.shouldSkip" not in batch_body:
        failures.append(
            "C1: performWriteBatch no longer consults ConfigAutoApprovePolicy — "
            "the auto-approve mode would be dead or bypassed"
        )

    # --- Criterion 2: the confirmation gate keeps exactly one call site ------
    call_sites = []
    for path in prod_files(root):
        src = strip_kotlin_comments(read(path))
        if "ConfigConfirmationGate.requestConfirmation(" in src:
            call_sites.append(os.path.relpath(path, root))
    if len(call_sites) != 1 or not call_sites[0].endswith("ConfigBridge.kt"):
        failures.append(
            "C2: ConfigConfirmationGate.requestConfirmation call sites = "
            f"{call_sites}, expected exactly [config/ConfigBridge.kt]"
        )

    # --- Criterion 3: getLevel must resolve through the pure policy ----------
    level_body = function_body(manager, r"fun\s+getLevel\s*\(")
    if level_body is None:
        failures.append("C3: getLevel not found in OffloadPermissionManager")
    elif "OffloadPermissionLevelPolicy.resolve" not in level_body:
        failures.append(
            "C3: getLevel no longer resolves through OffloadPermissionLevelPolicy — "
            "auto-approve overlays would be ad-hoc and untestable"
        )
    else:
        # --- Criterion 5: the resolved level must be what getLevel returns ---
        failures.extend(check_policy_return(level_body))

    # --- Criterion 4: the mode must be invisible to the agent's config surface
    for name, path in (("ConfigRegistry", CONFIG_REGISTRY), ("ConfigBuiltins", CONFIG_BUILTINS)):
        src = strip_kotlin_comments(read(os.path.join(root, path)))
        if re.search(r"auto[_-]?approve", src, re.IGNORECASE):
            failures.append(
                f"C4: {name} references auto-approve — the mode must not be "
                "registered/exposed via minis-config (self-authorization)"
            )
    # Also: the store must not register itself anywhere under config/ registry
    # surface (belt for the suspenders above).
    for path in prod_files(root):
        rel = os.path.relpath(path, root)
        if "config" not in rel:
            continue
        if rel.endswith(("AutoApproveStore.kt",)):
            continue
        src = strip_kotlin_comments(read(path))
        if "registerHidden" in src and re.search(r"auto[_-]?approve", src, re.IGNORECASE):
            failures.append(f"C4b: hidden registration of auto-approve found in {rel}")

    return failures


# ---------------------------------------------------------------------------
# Self-test: mutation arms. Each arm breaks one criterion on a temp copy of the
# two focal files; the gate must flip red for exactly that criterion.
# ---------------------------------------------------------------------------

ARMS = {
    "compliant": None,
    "policy_consult_removed": (
        CONFIG_BRIDGE,
        [("ConfigAutoApprovePolicy.shouldSkip(", "false || ConfigAutoApprovePolicy.__gone(")],
    ),
    "second_call_site": (
        "src/android/app/src/main/java/com/rikkaminis/app/debug/DebugRPCHandler.kt",
        [("skipConfirmation = true,", "skipConfirmation = true; ConfigConfirmationGate.requestConfirmation(null)")],
    ),
    "getlevel_inlined": (
        PERM_MANAGER,
        [("OffloadPermissionLevelPolicy.resolve", "__inlined_resolve")],
    ),
    "registry_exposure": (
        CONFIG_BUILTINS,
        [("internal object ConfigBuiltins {", "internal object ConfigBuiltins {\n    private val autoApproveProbe = 0")],
    ),
    # meta-audit-1024 FIX-M1 — the exact external witness: the resolve call is
    # KEPT (so C3's symbol check stays green), but its value is dropped and an
    # unconditional level is returned ⇒ every user setting (NOT_ALLOWED /
    # ASK_ONCE) is void for all offload tools.
    "getlevel_result_discarded": (
        PERM_MANAGER,
        [
            ("        return OffloadPermissionLevelPolicy.resolve(",
             "        OffloadPermissionLevelPolicy.resolve("),
            ("        )\n    }\n\n    fun setLevel",
             "        )\n        return PermissionLevel.BYPASS\n    }\n\n    fun setLevel"),
        ],
    ),
    # Precision arm: binding the value and returning it later is the other
    # accepted spelling (must stay green — the criterion is about consumption,
    # not about one syntactic form).
    "getlevel_via_local_val": (
        PERM_MANAGER,
        [
            ("        return OffloadPermissionLevelPolicy.resolve(",
             "        val resolvedLevel = OffloadPermissionLevelPolicy.resolve("),
            ("        )\n    }\n\n    fun setLevel",
             "        )\n        return resolvedLevel\n    }\n\n    fun setLevel"),
        ],
    ),
    # Regression arm for a false GREEN found by the fixer's own probe: a
    # discarded resolve() followed by `return <other local>` must be RED. It is
    # red only if the statement scan stops at the depth-0 newline — walking
    # further back swallows the previous `val stored = …` line and reads its
    # name as "the bound value that is returned".
    "getlevel_discard_return_other_val": (
        PERM_MANAGER,
        [
            ("        return OffloadPermissionLevelPolicy.resolve(",
             "        OffloadPermissionLevelPolicy.resolve("),
            ("        )\n    }\n\n    fun setLevel",
             "        )\n        return stored ?: info.defaultLevel\n    }\n\n    fun setLevel"),
        ],
    ),
}

SELFTEST_TAGS = {
    "compliant": "AUTO_APPROVE_BYPASS_SELFTEST compliant arm: OK",
    "policy_consult_removed": "AUTO_APPROVE_BYPASS_SELFTEST policy_consult_removed arm: OK",
    "second_call_site": "AUTO_APPROVE_BYPASS_SELFTEST second_call_site arm: OK",
    "getlevel_inlined": "AUTO_APPROVE_BYPASS_SELFTEST getlevel_inlined arm: OK",
    "registry_exposure": "AUTO_APPROVE_BYPASS_SELFTEST registry_exposure arm: OK",
    "getlevel_result_discarded": "AUTO_APPROVE_BYPASS_SELFTEST getlevel_result_discarded arm: OK",
    "getlevel_via_local_val": "AUTO_APPROVE_BYPASS_SELFTEST getlevel_via_local_val arm: OK",
    "getlevel_discard_return_other_val": "AUTO_APPROVE_BYPASS_SELFTEST getlevel_discard_return_other_val arm: OK",
}


def run_arm(root, arm_name):
    edits = ARMS[arm_name]
    if edits is None:
        return check(root)
    path, replacements = edits
    src_path = os.path.join(root, path)
    original = read(src_path)
    tmp_path = src_path + ".mutated"
    mutated = original
    for old, new in replacements:
        if old not in mutated:
            return ["arm %s: anchor %r not found" % (arm_name, old)]
        mutated = mutated.replace(old, new, 1)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(mutated)
        os.replace(tmp_path, src_path)
        return check(root)
    finally:
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(original)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    root = args[0] if args else "."
    if "--selftest" in sys.argv[1:]:
        tmp = tempfile.mkdtemp(prefix="auto_approve_gate_")
        try:
            # Copy only what check() reads: the two focal files + prod tree for
            # the call-site walk. Copying the whole tree is simplest and safe.
            shutil.copytree(root, tmp, ignore=shutil.ignore_patterns(".git", "build", ".gradle"), dirs_exist_ok=True)
            ok = True
            for arm_name, expected_failures in [
                ("compliant", 0),
                ("policy_consult_removed", 1),
                ("second_call_site", 1),
                ("getlevel_inlined", 1),
                ("registry_exposure", 1),
                ("getlevel_result_discarded", 1),
                ("getlevel_via_local_val", 0),
                ("getlevel_discard_return_other_val", 1),
            ]:
                failures = run_arm(tmp, arm_name)
                tag = SELFTEST_TAGS[arm_name]
                if arm_name == "compliant":
                    if failures:
                        print(f"{tag} — FAIL: {failures}")
                        ok = False
                    else:
                        print(f"{tag} (0 violations)")
                else:
                    if len(failures) != expected_failures:
                        print(f"{tag} — FAIL: expected {expected_failures} violation(s), got {len(failures)}: {failures}")
                        ok = False
                    else:
                        if failures:
                            print(f"{tag} ({len(failures)} violation, red as designed)")
                        else:
                            print(f"{tag} (0 violations — accepted spelling)")
            if not ok:
                print("AUTO_APPROVE_BYPASS_SELFTEST: FAIL")
                return 1
            print("AUTO_APPROVE_BYPASS_SELFTEST: PASS")
            return 0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    failures = check(root)
    if failures:
        print("AUTO_APPROVE_BYPASS_CHECK: RED")
        for f in failures:
            print("  -", f)
        return 1
    print("AUTO_APPROVE_BYPASS_CHECK: OK (all 5 criteria hold)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""collection_copy_on_mutate_check — deep-audit-1023 FIX-D (ISSUE-04, P1) gate.

Pins the config-collections write contract: a `mutate` helper must NEVER
persist a mutated LIVE object from `_config.value` — the object handed back
by `repo.group(id)` / `repo.instance(id)` / `repo.entry(id)` is the exact
instance concurrent readers (UI model picker, group detail screen reorder,
`groupsList` / `groups.update` RPCs) are iterating. Three compliant shapes,
mirroring the sibling idioms that already existed before this gate:

  arm0 (delegated copy core — GroupsCollection since [fix/da1023-D]):
        the body delegates to the shared pure helper `mutatedGroupCopy`
        and persists ITS RETURN (`repo.updateGroup(mutatedGroupCopy(g,
        apply))`). The helper's own body (GroupWriteRules.kt) must
        materialise a detached copy (`.copy(` with the mutable member
        re-instantiated via `toMutableList()`), hand THE COPY to `apply(...)`,
        and return THE COPY — a hollowed helper (`apply(g); return g`) is a
        violation.
  arm1 (in-place style, `apply: (T) -> Unit` — legacy shape, still accepted):
        the body must materialise a detached copy FIRST
        (`val working = g.copy(...)` with any shared mutable member
        re-instantiated), hand THE COPY to `apply(...)`, and persist THE
        COPY via `repo.update*(...)`. Persisting the originally-fetched
        identifier, or copying only after `apply`, is a violation.
  arm2 (functional style, `apply: (T) -> T` — Providers/ModelsCollection):
        the persisted value must be the lambda's return, i.e. the update
        call takes the invocation inline (`repo.updateInstance(apply(x))`).
        Ignoring the return and re-persisting the loaded object violates.

History: GroupsCollection.mutate was the third entry of the
[fix/audit-b18 / T9-L2] family (ProviderRepository.updateGroup and
ProviderMutationMethods.groupsUpdate were already converted) — this gate
exists so a fourth entry or a revert of the Groups fix cannot land silently.

Usage:
  python3 collection_copy_on_mutate_check.py <repo_root>   # real tree
  python3 collection_copy_on_mutate_check.py --selftest    # fixtures

Selftest marker: COLLECTION_COPY_ON_MUTATE_SELFTEST=PASS
"""

import os
import re
import sys
import tempfile
import glob

COLLECTIONS_DIR = "src/android/app/src/main/java/com/rikkaminis/app/config/collections"
COPY_HELPER = "mutatedGroupCopy"

# [wide-1024 arm-scope] The visibility modifier is DERIVED, not spelled: the
# pre-write-funnel shape pinned `private fun mutate(` literally, so widening the
# helper to `internal` (a legitimate-looking refactor) dropped the whole
# function out of the scan while `check_tree`'s aggregate `seen` list stayed
# non-empty thanks to the sibling collections. `check_tree` now also holds the
# per-file declaration count equal to the analysed count (fail-closed).
SIG_PAT = re.compile(
    r"(?:(?:private|internal|public|protected|override|suspend)\s+)*"
    r"fun\s+mutate\s*\(\s*id:\s*String\s*,\s*(\w+)\s*:\s*\(\s*(\w+)\s*\)\s*->\s*(\w+)\s*\)"
)
UPDATE_PAT = re.compile(r"repo\.update\w*\s*\(")


def update_call_args(code):
    """(offset, whole argument text) for every `repo.update*()` call.

    Paren-balanced on purpose: the old `[^)\n]+` capture stopped at the FIRST
    `)`, so `apply(inst).let { inst }` and `apply(inst)` produced the same
    prefix and the arm2 prefix match could not tell them apart."""
    out = []
    for m in UPDATE_PAT.finditer(code):
        i = code.index("(", m.start())
        depth = 0
        for k in range(i, len(code)):
            if code[k] == "(":
                depth += 1
            elif code[k] == ")":
                depth -= 1
                if depth == 0:
                    out.append((m.start(), code[i + 1:k]))
                    break
    return out


def call_is_whole(argtext, name):
    """True when [argtext] is EXACTLY one `name(...)` call — nothing appended.

    `apply(inst)` passes, `apply(inst).let { inst }` does not: the trailing
    `.let` discards the value the call produced, which is the shape that made
    every ProvidersCollection write vanish while the gate stayed green."""
    s = argtext.strip()
    if not s.startswith(name + "("):
        return False
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return s[i + 1:].strip() == ""
    return False


def apply_args(body):
    """Argument identifier of every `apply(...)` call in [body]."""
    out = []
    for m in re.finditer(r"\bapply\s*\(", body):
        i = m.end() - 1
        depth = 0
        for k in range(i, len(body)):
            if body[k] == "(":
                depth += 1
            elif body[k] == ")":
                depth -= 1
                if depth == 0:
                    out.append(body[i + 1:k].strip())
                    break
    return out


def strip_comments(src):
    """Remove // line comments and /* */ block comments (KDoc included)."""
    out = []
    i, n = 0, len(src)
    in_block = in_line = False
    while i < n:
        if in_block:
            if src.startswith("*/", i):
                in_block = False
                i += 2
            else:
                i += 1
        elif in_line:
            if src[i] == "\n":
                in_line = False
                out.append("\n")
            i += 1
        elif src.startswith("//", i):
            in_line = True
            i += 2
        elif src.startswith("/*", i):
            in_block = True
            i += 2
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def extract_braced_fun(code, sig_pat):
    """Yield the brace-matched body for every regex match of `sig_pat`."""
    results = []
    for m in sig_pat.finditer(code):
        start = m.start()
        depth = 0
        i = start
        seen_open = False
        while i < len(code):
            if code[i] == "{":
                depth += 1
                seen_open = True
            elif code[i] == "}":
                depth -= 1
                if seen_open and depth == 0:
                    break
            i += 1
        results.append(code[start : i + 1])
    return results


def extract_mutate_bodies(code):
    """Yield (param_name, ret_type, body) for every `private fun mutate`."""
    out = []
    for body in extract_braced_fun(code, SIG_PAT):
        m = SIG_PAT.search(body)
        param, ret_type = m.group(1), m.group(3)
        out.append((param, ret_type, body))
    return out


def extract_helper_body(code, name):
    """Return the body of `fun <name>(...)` (top-level or member), or None."""
    pat = re.compile(r"fun\s+" + re.escape(name) + r"\s*\(")
    bodies = extract_braced_fun(code, pat)
    return bodies[0] if bodies else None


def check_copy_core(rel, core_body, what, require_return=False):
    """Shared arm0/arm1 copy-shape check. Returns violation or None.

    [wide-1024 arm0] A `.copy(` + `apply(copy)` prefix is not the contract:
    the copy must be the value that LEAVES the function. `apply(live); return
    live` alongside a decoy working copy (the witnessed shape) kept the old
    criteria green because they only ever asked "does a copy exist and is it
    handed to apply somewhere"."""
    copy_m = re.search(r"val\s+(\w+)\s*=\s*\w+\.copy\(", core_body)
    if copy_m is None:
        return (
            f"{rel}: {what} is hollowed — no `.copy(` working copy inside "
            f"(live-object mutation of _config.value, ISSUE-04 regression)"
        )
    copy_var = copy_m.group(1)
    args = apply_args(core_body)
    if not args:
        return f"{rel}: {what} never hands the working copy `{copy_var}` to apply()"
    stray = [a for a in args if a != copy_var]
    if stray:
        return (
            f"{rel}: {what} hands apply() the LIVE object (`{stray[0]}`) beside the "
            f"working copy — concurrent readers iterate the live instance "
            f"(ISSUE-04 ConcurrentModification / lost emission)"
        )
    if "toMutableList()" not in core_body:
        return (
            f"{rel}: {what} copy must re-materialise the mutable member "
            f"(toMutableList) — a data-class copy shares its backing list"
        )
    if require_return:
        returns = re.findall(r"\breturn\s+([A-Za-z_][A-Za-z0-9_]*)\b", core_body)
        if not returns:
            return (
                f"{rel}: {what} has no `return` statement (fail-closed) — cannot "
                f"verify the copy is what leaves the function"
            )
        if returns[-1] != copy_var:
            return (
                f"{rel}: {what} returns `{returns[-1]}`, not the working copy "
                f"`{copy_var}` — the caller is handed the live object again "
                f"(copy-on-write collapsed back into in-place mutation)"
            )
    return None


def check_mutation_site(rel, param, ret_type, body, helpers=None):
    """Return a violation string or None."""
    # arm0: delegated copy core (mutatedGroupCopy) — check BEFORE the
    # `apply(` invocation check, since the lambda is passed, not invoked.
    if COPY_HELPER + "(" in body:
        upd = update_call_args(body)
        if not upd:
            return f"{rel}: mutate never persists via repo.update*()"
        if not call_is_whole(upd[0][1], COPY_HELPER):
            return (
                f"{rel}: delegated mutate must persist the helper's return "
                f"(`repo.update*({COPY_HELPER}(…))` and nothing else), got "
                f"`{upd[0][1].strip()}`"
            )
        hbody = (helpers or {}).get(COPY_HELPER)
        if hbody is None:
            return (
                f"{rel}: `{COPY_HELPER}` definition not found under "
                f"{COLLECTIONS_DIR} — the delegate target moved; update this gate"
            )
        return check_copy_core(rel, hbody, f"`{COPY_HELPER}`", require_return=True)
    invoke = param + "("
    invoke_idx = body.find(invoke)
    if invoke_idx < 0:
        return f"{rel}: mutate lambda `{param}` is never invoked — dead helper?"
    upd = update_call_args(body)
    if ret_type == "Unit":
        # arm1: copy-before-apply; persist & apply THE COPY
        v = check_copy_core(rel, body, "in-place mutate `(T) -> Unit`")
        if v:
            return v
        copy_m = re.search(r"val\s+(\w+)\s*=\s*\w+\.copy\(", body)
        copy_var = copy_m.group(1)
        if copy_m.start() > invoke_idx:
            return (
                f"{rel}: `.copy(` happens AFTER `{invoke}` — the lambda mutates "
                f"the live object first"
            )
        invoke_arg = re.compile(re.escape(invoke) + r"\s*(\w+)\s*\)").search(body)
        if invoke_arg is None or invoke_arg.group(1) != copy_var:
            return (
                f"{rel}: `{param}()` must receive the working copy `{copy_var}`, "
                f"not the live object"
            )
        ups = update_call_args(body)
        if not ups:
            return f"{rel}: mutate never persists via repo.update*()"
        if ups[0][1].strip() != copy_var:
            return (
                f"{rel}: repo.update*() persists `{ups[0][1].strip()}` — must "
                f"persist the working copy `{copy_var}`"
            )
        return None
    # arm2: functional style — persist the lambda's return
    # [wide-1024 arm2] The persisted expression must be EXACTLY `apply(x)`:
    # `apply(inst).let { inst }` matched the old prefix check and threw the
    # new instance away, silently discarding every write.
    ups = update_call_args(body)
    if not ups:
        return f"{rel}: mutate never persists via repo.update*()"
    if not call_is_whole(ups[0][1], param):
        got = ups[0][1].strip()
        if not got.startswith(invoke):
            return (
                f"{rel}: functional mutate calls the lambda but persists "
                f"`{got}` — the loaded object is re-persisted and the mutation "
                "is silently dropped"
            )
        return (
            f"{rel}: functional mutate must persist the lambda's return "
            f"(`repo.update*({invoke})` and nothing else), got `{got}` "
            "— a trailing `.let`/`.also` discards the mutation"
        )
    return None


def check_tree(root):
    violations = []
    seen = []
    pattern = os.path.join(root, COLLECTIONS_DIR, "*.kt")
    files = sorted(glob.glob(pattern))
    if not files:
        return [f"no collections found under {COLLECTIONS_DIR}"]
    stripped = {}
    for path in files:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        src = open(path, encoding="utf-8", errors="replace").read()
        stripped[rel] = strip_comments(src)
    # helper definitions are cross-file (GroupWriteRules.kt)
    helpers = {}
    for rel, code in stripped.items():
        hbody = extract_helper_body(code, COPY_HELPER)
        if hbody is not None:
            helpers[COPY_HELPER] = hbody
    for rel, code in stripped.items():
        declared = len(re.findall(r"\bfun\s+mutate\s*\(", code))
        bodies = extract_mutate_bodies(code)
        if declared != len(bodies):
            violations.append(
                f"{rel}: {declared} `fun mutate` declaration(s) but {len(bodies)} "
                "analysed — a mutate site slipped out of the scan's signature "
                "grammar (visibility/spelling drift, fail-closed)"
            )
        for param, ret_type, body in bodies:
            seen.append(f"{rel}:{ret_type}")
            v = check_mutation_site(rel, param, ret_type, body, helpers)
            if v:
                violations.append(v)
    if not seen:
        violations.append(
            "no `fun mutate` found in config/collections — the write "
            "funnel changed shape, update this gate"
        )
    return violations


# --- fixtures -------------------------------------------------------------

FIX_DELEGATE_OK = """package com.rikkaminis.app.config.collections

class GroupsCollection {
    private fun mutate(id: String, apply: (ModelGroup) -> Unit) {
        val g = group(id) ?: throw ConfigError.UnknownPath("groups.$id")
        repo.updateGroup(mutatedGroupCopy(g, apply))
    }
}

internal fun mutatedGroupCopy(g: ModelGroup, apply: (ModelGroup) -> Unit): ModelGroup {
    val working = g.copy(memberEntryIds = g.memberEntryIds.toMutableList())
    apply(working)
    return working
}
"""

FIX_DELEGATE_HOLLOW = """package com.rikkaminis.app.config.collections

class GroupsCollection {
    private fun mutate(id: String, apply: (ModelGroup) -> Unit) {
        val g = group(id) ?: throw ConfigError.UnknownPath("groups.$id")
        repo.updateGroup(mutatedGroupCopy(g, apply))
    }
}

internal fun mutatedGroupCopy(g: ModelGroup, apply: (ModelGroup) -> Unit): ModelGroup {
    apply(g)
    return g
}
"""

FIX_DELEGATE_NO_COPY_SHARING = """package com.rikkaminis.app.config.collections

class GroupsCollection {
    private fun mutate(id: String, apply: (ModelGroup) -> Unit) {
        val g = group(id) ?: throw ConfigError.UnknownPath("groups.$id")
        repo.updateGroup(mutatedGroupCopy(g, apply))
    }
}

internal fun mutatedGroupCopy(g: ModelGroup, apply: (ModelGroup) -> Unit): ModelGroup {
    val working = g.copy()
    apply(working)
    return working
}
"""

# [wide-1024 arm0] the witnessed shape: a real working copy is materialised and
# handed to apply(), then the LIVE object is mutated and returned beside it.
FIX_DELEGATE_APPLIES_LIVE = """package com.rikkaminis.app.config.collections

class GroupsCollection {
    private fun mutate(id: String, apply: (ModelGroup) -> Unit) {
        val g = group(id) ?: throw ConfigError.UnknownPath("groups.$id")
        repo.updateGroup(mutatedGroupCopy(g, apply))
    }
}

internal fun mutatedGroupCopy(g: ModelGroup, apply: (ModelGroup) -> Unit): ModelGroup {
    val working = g.copy(memberEntryIds = g.memberEntryIds.toMutableList())
    apply(working)
    apply(g)
    return g
}
"""

# [wide-1024 arm2] the persisted expression is not the lambda's return: the
# `.let { inst }` suffix keeps the prefix `apply(inst)` the old arm matched.
FIX_FUNCTIONAL_DISCARDS_RETURN = """package com.rikkaminis.app.config.collections

class ProvidersCollection {
    private fun mutate(id: String, apply: (ProviderInstance) -> ProviderInstance) {
        val inst = repo.instance(id) ?: throw ConfigError.UnknownPath("providers.$id")
        repo.updateInstance(apply(inst).let { inst })
    }
}
"""

# [wide-1024 scope] `internal` is not `private`: the old signature grammar left
# the whole function unscanned while the aggregate `seen` list stayed non-empty.
FIX_SCOPE_INTERNAL_LIVE = """package com.rikkaminis.app.config.collections

class ModelsCollection {
    internal fun mutate(id: String, apply: (ModelEntry) -> ModelEntry) {
        val e = entry(id) ?: throw ConfigError.UnknownPath("models.$id")
        apply(e)
        repo.updateEntry(e)
    }
}
"""

FIX_SCOPE_INTERNAL_OK = """package com.rikkaminis.app.config.collections

class ModelsCollection {
    internal fun mutate(id: String, apply: (ModelEntry) -> ModelEntry) {
        val e = entry(id) ?: throw ConfigError.UnknownPath("models.$id")
        repo.updateEntry(apply(e))
    }
}
"""

FIX_INPLACE_OK = """package com.rikkaminis.app.config.collections

class GroupsCollection {
    private fun mutate(id: String, apply: (ModelGroup) -> Unit) {
        val g = group(id) ?: throw ConfigError.UnknownPath("groups.$id")
        val working = g.copy(memberEntryIds = g.memberEntryIds.toMutableList())
        apply(working)
        repo.updateGroup(working)
    }
}
"""

FIX_INPLACE_DIRTY = """package com.rikkaminis.app.config.collections

class GroupsCollection {
    private fun mutate(id: String, apply: (ModelGroup) -> Unit) {
        val g = group(id) ?: throw ConfigError.UnknownPath("groups.$id")
        apply(g)
        repo.updateGroup(g)
    }
}
"""

FIX_INPLACE_LATE_COPY = """package com.rikkaminis.app.config.collections

class GroupsCollection {
    private fun mutate(id: String, apply: (ModelGroup) -> Unit) {
        val g = group(id) ?: throw ConfigError.UnknownPath("groups.$id")
        apply(g)
        val working = g.copy(memberEntryIds = g.memberEntryIds.toMutableList())
        repo.updateGroup(working)
    }
}
"""

FIX_INPLACE_PERSIST_LIVE = """package com.rikkaminis.app.config.collections

class GroupsCollection {
    private fun mutate(id: String, apply: (ModelGroup) -> Unit) {
        val g = group(id) ?: throw ConfigError.UnknownPath("groups.$id")
        val working = g.copy(memberEntryIds = g.memberEntryIds.toMutableList())
        apply(working)
        repo.updateGroup(g)
    }
}
"""

FIX_FUNCTIONAL_OK = """package com.rikkaminis.app.config.collections

class ProvidersCollection {
    private fun mutate(id: String, apply: (ProviderInstance) -> ProviderInstance) {
        val inst = repo.instance(id) ?: throw ConfigError.UnknownPath("providers.$id")
        repo.updateInstance(apply(inst))
    }
}
"""

FIX_FUNCTIONAL_DIRTY = """package com.rikkaminis.app.config.collections

class ProvidersCollection {
    private fun mutate(id: String, apply: (ProviderInstance) -> ProviderInstance) {
        val inst = repo.instance(id) ?: throw ConfigError.UnknownPath("providers.$id")
        apply(inst)
        repo.updateInstance(inst)
    }
}
"""


def run_fixture(name, content, expect_ok):
    with tempfile.TemporaryDirectory() as tmp:
        dst_dir = os.path.join(tmp, COLLECTIONS_DIR)
        os.makedirs(dst_dir)
        with open(os.path.join(dst_dir, "Fixture.kt"), "w") as f:
            f.write(content)
        v = check_tree(tmp)
        ok = not v
        status = "PASS" if ok == expect_ok else "FAIL"
        print(f"  [{status}] fixture {name}: violations={v}")
        return status == "PASS"


def selftest():
    print("collection_copy_on_mutate_check selftest:")
    ok = True
    ok &= run_fixture("delegate compliant (GroupWriteRules shape)", FIX_DELEGATE_OK, True)
    ok &= run_fixture("delegate helper hollowed (apply(g); return g)", FIX_DELEGATE_HOLLOW, False)
    ok &= run_fixture("delegate copy without re-materialise", FIX_DELEGATE_NO_COPY_SHARING, False)
    ok &= run_fixture("in-place compliant (fixed Groups shape)", FIX_INPLACE_OK, True)
    ok &= run_fixture("in-place live-object (pre-fix ISSUE-04)", FIX_INPLACE_DIRTY, False)
    ok &= run_fixture("in-place copy AFTER apply", FIX_INPLACE_LATE_COPY, False)
    ok &= run_fixture("in-place persists live object despite copy", FIX_INPLACE_PERSIST_LIVE, False)
    ok &= run_fixture("functional compliant (Providers shape)", FIX_FUNCTIONAL_OK, True)
    ok &= run_fixture("functional ignoring lambda return", FIX_FUNCTIONAL_DIRTY, False)
    # ── wide-1024 witness arms (each was green before this branch) ──
    ok &= run_fixture("helper also mutates + returns the LIVE object",
                      FIX_DELEGATE_APPLIES_LIVE, False)
    ok &= run_fixture("functional persists apply(x).let { x } (return discarded)",
                      FIX_FUNCTIONAL_DISCARDS_RETURN, False)
    ok &= run_fixture("scope: `internal fun mutate` mutating the live object",
                      FIX_SCOPE_INTERNAL_LIVE, False)
    ok &= run_fixture("scope control: `internal fun mutate` stays clean",
                      FIX_SCOPE_INTERNAL_OK, True)
    print("COLLECTION_COPY_ON_MUTATE_SELFTEST=" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if len(sys.argv) < 2:
        print("usage: python3 collection_copy_on_mutate_check.py <repo_root>", file=sys.stderr)
        sys.exit(2)
    violations = check_tree(sys.argv[1])
    if violations:
        for v in violations:
            print("FAIL:", v)
        sys.exit(1)
    print("collection_copy_on_mutate_check: OK (all mutate sites persist a copy)")


if __name__ == "__main__":
    main()

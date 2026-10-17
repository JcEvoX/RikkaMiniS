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

SIG_PAT = re.compile(
    r"private fun mutate\(id: String, (\w+): \((\w+)\) -> (\w+)\)"
)
UPDATE_PAT = re.compile(r"repo\.update\w*\(\s*([^)\n]+)\)")


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


def check_copy_core(rel, core_body, what):
    """Shared arm0/arm1 copy-shape check. Returns violation or None."""
    copy_m = re.search(r"val\s+(\w+)\s*=\s*\w+\.copy\(", core_body)
    if copy_m is None:
        return (
            f"{rel}: {what} is hollowed — no `.copy(` working copy inside "
            f"(live-object mutation of _config.value, ISSUE-04 regression)"
        )
    copy_var = copy_m.group(1)
    apply_call = re.search(r"apply\(\s*(\w+)\s*\)", core_body)
    if apply_call is None or apply_call.group(1) != copy_var:
        return f"{rel}: {what} must hand the working copy `{copy_var}` to apply()"
    if "toMutableList()" not in core_body:
        return (
            f"{rel}: {what} copy must re-materialise the mutable member "
            f"(toMutableList) — a data-class copy shares its backing list"
        )
    return None


def check_mutation_site(rel, param, ret_type, body, helpers=None):
    """Return a violation string or None."""
    # arm0: delegated copy core (mutatedGroupCopy) — check BEFORE the
    # `apply(` invocation check, since the lambda is passed, not invoked.
    if COPY_HELPER + "(" in body:
        upd = UPDATE_PAT.search(body)
        if upd is None:
            return f"{rel}: mutate never persists via repo.update*()"
        if not upd.group(1).strip().startswith(COPY_HELPER + "("):
            return (
                f"{rel}: delegated mutate must persist the helper's return "
                f"(`repo.update*({COPY_HELPER}(…)`), got `{upd.group(1).strip()}`"
            )
        hbody = (helpers or {}).get(COPY_HELPER)
        if hbody is None:
            return (
                f"{rel}: `{COPY_HELPER}` definition not found under "
                f"{COLLECTIONS_DIR} — the delegate target moved; update this gate"
            )
        return check_copy_core(rel, hbody, f"`{COPY_HELPER}`")
    invoke = param + "("
    invoke_idx = body.find(invoke)
    if invoke_idx < 0:
        return f"{rel}: mutate lambda `{param}` is never invoked — dead helper?"
    upd = UPDATE_PAT.search(body)
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
        if upd is None:
            return f"{rel}: mutate never persists via repo.update*()"
        if upd.group(1).strip() != copy_var:
            return (
                f"{rel}: repo.update*() persists `{upd.group(1).strip()}` — must "
                f"persist the working copy `{copy_var}`"
            )
        return None
    # arm2: functional style — persist the lambda's return
    if upd is None:
        return f"{rel}: mutate never persists via repo.update*()"
    if not re.match(r"\s*" + re.escape(invoke), upd.group(1)):
        return (
            f"{rel}: functional mutate must persist the lambda's return "
            f"(`repo.update*({invoke}…)`), got `{upd.group(1).strip()}`"
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
        for param, ret_type, body in extract_mutate_bodies(code):
            seen.append(f"{rel}:{ret_type}")
            v = check_mutation_site(rel, param, ret_type, body, helpers)
            if v:
                violations.append(v)
    if not seen:
        violations.append(
            "no `private fun mutate` found in config/collections — the write "
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

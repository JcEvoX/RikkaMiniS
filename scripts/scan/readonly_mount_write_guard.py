#!/usr/bin/env python3
"""
Read-only-mount write guard — every host-file write whose target comes from a
caller-supplied path parameter must consult `isLinuxPathUnderReadOnlyMount`.

Bug class ([deep-audit-1016] FIX-C 2b, live since T219): the guard exists and
`tools/FileWriteTool` / `tools/FileEditTool` call it, but `minis-model-use
--output` wrote through `shapeRawPassthrough` / `writeRunOutput` with no
read-only check at all — so a `--output` pointing into a user's read-only
mounted folder wrote the file anyway, while the same path refused via
`file_write`. Two write sites, one contract, and nothing mechanical looked at
both. (The audit report also claimed `FastioOffloadHandler` already checked;
it does not — it refuses *any* external-mount write, a superset of the
read-only rule, which is why it is exempt below WITH evidence rather than
silently passed.)

Criterion: for every function that (a) declares a path parameter from
PATH_PARAMS and (b) writes a host file, the function body must call
`isLinuxPathUnderReadOnlyMount(` — unless the file is in EXEMPT with a reason
and an evidence pattern that must still be present (an exemption that rots is
a failure, not a pass).

Usage: python3 scripts/scan/readonly_mount_write_guard.py [repo_root]
Exit:  0 = every site guarded (or exempt with evidence), 1 = unguarded site
"""
import os
import re
import sys

GUARD = "isLinuxPathUnderReadOnlyMount("

PATH_PARAMS = re.compile(
    r"^(outputPath|linuxPath|guestPath|destPath|dstPath|dest|dst|hostPath|targetPath|archivePath|outPath)$"
)

# A call that puts bytes on the host filesystem.
WRITE_CALL = re.compile(
    r"\.(writeBytes|writeText|appendText|write)\(|newOutputStream\(|createNewFile\("
    r"|FileOutputStream\(|Files\.(copy|write|createDirectories)\("
)

FUN_DECL = re.compile(
    r"^\s*(?:(?:internal|private|public|protected|override|suspend|inline|operator|"
    r"tailrec|open|abstract|final|external)\s+)*fun\s+(\w+)\s*[(<]"
)

# rel path -> (reason, [(evidence file rel or None for "this file", regex, min hits)])
#
# Each entry must earn its keep TWICE: the reason is the argument, and the
# evidence pattern must still be present in the named file — an exemption whose
# justification rotted (the refusal it leans on was deleted) is reported as a
# violation, not silently tolerated. `None` means "this file".
EXEMPT = {
    "sandbox/PRootKernel.kt": (
        "defines the guard itself (`isLinuxPathUnderReadOnlyMount`)",
        [(None, r"fun isLinuxPathUnderReadOnlyMount\(", 1)],
    ),
    "sandbox/offload/FastioOffloadHandler.kt": (
        "refuses any external-mount write (`isExternalMount`) — a superset of the "
        "read-only rule, so the read-only guard is unreachable by construction",
        [(None, r"isExternalMount", 3)],
    ),
    "sandbox/offload/FastioCopy.kt": (
        "tree-copy engine: every caller reaches it through FastioOffloadHandler, which "
        "refuses any external-mount destination before the copy starts",
        [("sandbox/offload/FastioOffloadHandler.kt", r"isExternalMount", 3)],
    ),
    "sandbox/offload/FastioTar.kt": (
        "tar engine: the extract entry point runs after FastioOffloadHandler's "
        "external-mount refusal on the destination",
        [("sandbox/offload/FastioOffloadHandler.kt", r"isExternalMount", 3)],
    ),
    "data/MountedFoldersStore.kt": (
        "`probeWritable` discovers OS-level writability by *attempting* a write; "
        "refusing it would make every mount report non-writable",
        [(None, r"fun probeWritable\(", 1)],
    ),
    "sandbox/offload/PhotosOffloadHandler.kt": (
        "its destinations are app-private (`filesDir/photos-export`), never a "
        "caller-supplied guest path",
        [(None, r"photos-export", 1)],
    ),
}


def strip_comments(text):
    """Comments first: prose apostrophes / braces otherwise unbalance the slice."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def strip_strings(line):
    out, i, quote = [], 0, None
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
        out.append(c)
        i += 1
    return "".join(out)


def functions(text):
    """[(name, params, body)] for every `fun` declared in `text`."""
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        m = FUN_DECL.match(lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1)
        # Signature runs until the parameter list closes.
        paren, j = 0, i
        sig = []
        seen_open = False
        while j < len(lines):
            s = strip_strings(lines[j])
            paren += s.count("(") - s.count(")")
            if "(" in s:
                seen_open = True
            sig.append(lines[j])
            if seen_open and paren <= 0:
                break
            j += 1
        sig_text = "\n".join(sig)
        pm = re.search(r"\((.*?)\)", sig_text, flags=re.S)
        params = pm.group(1) if pm else ""
        # Body: brace-balanced from the signature's last line.
        bal, k, seen_brace, body = 0, j, False, []
        while k < len(lines):
            s = strip_strings(lines[k])
            bal += s.count("{") - s.count("}")
            if "{" in s:
                seen_brace = True
            body.append(lines[k])
            if seen_brace and bal <= 0:
                break
            k += 1
        out.append((name, params, "\n".join(body)))
        i = max(k, j) + 1
    return out


def read(root, rel):
    full = os.path.join(root, "src/android/app/src/main/java/com/rikkaminis/app", rel)
    try:
        return open(full, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def path_param_names(params):
    names = []
    for part in params.split(","):
        nm = part.strip().split(":")[0].strip()
        if PATH_PARAMS.match(nm):
            names.append(nm)
    return names


def scan(root):
    """Returns (violations, sites_checked, files_scanned, exempt_used)."""
    app = os.path.join(root, "src/android/app/src/main/java/com/rikkaminis/app")
    violations, checked, files = [], 0, 0
    exempt_used = {}
    for dirpath, _dirs, filenames in os.walk(app):
        for fn in sorted(filenames):
            if not fn.endswith(".kt"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, app).replace(os.sep, "/")
            raw = open(full, encoding="utf-8", errors="replace").read()
            text = strip_comments(raw)
            files += 1
            for name, params, body in functions(text):
                if not path_param_names(params):
                    continue
                if not WRITE_CALL.search(body):
                    continue
                checked += 1
                if GUARD in body:
                    continue
                if rel in EXEMPT:
                    reason, evidence = EXEMPT[rel]
                    rotted = []
                    for ev_rel, pattern, need in evidence:
                        target = text if ev_rel is None else read(root, ev_rel)
                        hits = len(re.findall(pattern, target))
                        if hits < need:
                            rotted.append(f"{ev_rel or rel}: /{pattern}/ {hits} < {need}")
                    if rotted:
                        violations.append(
                            f"{rel}:{name} — exempt ('{reason}') but its evidence rotted: "
                            + "; ".join(rotted)
                        )
                    else:
                        exempt_used[rel] = reason
                    continue
                violations.append(
                    f"{rel}:{name}({params.strip()[:60]}) — writes a host file from a path "
                    f"parameter without calling {GUARD}"
                )
    return violations, checked, files, exempt_used


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", ".."))
    violations, checked, files, exempt_used = scan(root)
    if files < 20:
        print(f"ROT: only {files} Kotlin files under com/rikkaminis/app — the source tree moved?")
        return 1
    if checked < 3:
        print(f"ROT: only {checked} path-parameter write sites found — the detector stopped matching?")
        return 1
    if violations:
        print("Unguarded host-file writes:")
        for v in violations:
            print(f"  ✗ {v}")
        return 1
    print(f"OK: {checked} path-parameter write site(s) guarded ({files} files scanned)")
    for rel, reason in sorted(exempt_used.items()):
        print(f"  · exempt {rel}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

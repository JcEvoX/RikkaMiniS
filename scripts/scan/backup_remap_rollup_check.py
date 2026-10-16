#!/usr/bin/env python3
"""
Gate: a restore must land every id reference it re-minted, and a backup must
carry the distilled memory layer (deep-audit-1018, ISSUE-09, branch
fix/da1018-backup-remap).

Three silent-in-production defect classes, all "the payload looks complete but
one field/artifact points at something that no longer exists on the target":

  A. PINNED COMPACTION MODEL (remapDefaultsIds)
     Import mints fresh entry uuids (Stage 1) and rewrites the `defaults.*`
     id-bearing fields from the source install's uuids. `runtime.compactModelEntryId`
     was not among them. Its consumer treats an unresolvable pin as
     "not reachable — follow the session chain" with ONE info line
     (CompactOrchestration), so a restore onto another install silently
     un-pinned the user's compaction model with no user-visible signal.

  B. SESSION MODEL BINDING (importChatSections)
     Session rows persist `{"type":"group","groupId":…,"lastEntryId":…}` /
     `{"type":"entry","entryId":…}` built from the SOURCE install's uuids.
     Writing `s.optString("modelBinding")` through verbatim made every restored
     session fail ChatViewModel.restoreFromBinding → silent degradation to the
     stale modelId (group lost, so no fallback chain either).

  C. DISTILLED MEMORY (buildSections + deleteFile)
     `MemoryRepository.listAllFiles()` excludes MEMORY-ROLLUP.md on purpose
     (settings list / memory_get must not surface it). Packing `memoryFiles`
     from that listing alone therefore never carried the distilled rules — the
     one artifact a device transfer cannot regenerate. Symmetrically,
     deleteFile guarded GLOBAL.md / SOUL.md but not the rollup.

Registered gates assert the ROUTING/PRESENCE of the decision, not the absence
of a spelling, so a refactor that keeps the contract is a deliberate edit here.

Usage: python3 scripts/scan/backup_remap_rollup_check.py [root]
       python3 scripts/scan/backup_remap_rollup_check.py --root <root>
       python3 scripts/scan/backup_remap_rollup_check.py --selftest
Exit:  0 = clean, 1 = violation(s), 2 = usage/ROT
"""
import argparse
import os
import re
import sys

CFG_REL = "src/android/app/src/main/java/com/rikkaminis/app/backup/ConfigBackup.kt"
MEM_REL = "src/android/app/src/main/java/com/rikkaminis/app/data/repository/MemoryRepository.kt"

COMPACT_PATH = '"runtime.compactModelEntryId"'
ROLLUP_ARG = "com.rikkaminis.app.workspace.MemoryRollupEngine.ROLLUP_FILE"
REMAP_SESSION_CALL = "remapSessionBindingJson("
MEM_PACK_CALL = "memoryBackupEntries("
ROLLUP_GUARD = "if (name == ROLLUP_FILE) return false"
SAVE_BY_NAME = "memoryRepo.saveFile(name, content)"


def code_only(text):
    """Text with line comments (//…) removed — presence checks below must not be
    satisfiable from prose in a comment."""
    return "\n".join(line.split("//")[0] for line in text.split("\n"))


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def _balanced(src, open_idx, opener="(", closer=")"):
    """Index just past the closer matching the opener at [open_idx] (-1 if none)."""
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == opener:
            depth += 1
        elif src[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def function_text(src, header):
    """Text of the declaration starting at [header], up to its closing brace."""
    i = src.find(header)
    if i < 0:
        return None
    brace = src.find("{", i)
    if brace < 0:
        return None
    end = _balanced(src, brace, "{", "}")
    return None if end < 0 else src[i:end]


def check_remap_defaults(src):
    """A: remapDefaultsIds' `when (path)` must route runtime.compactModelEntryId."""
    body = function_text(src, "fun remapDefaultsIds(")
    if body is None:
        return [(CFG_REL, 0, "remapDefaultsIds( not found — gate ROT")]
    when = body.find("when (path)")
    if when < 0:
        return [(CFG_REL, 0, "remapDefaultsIds no longer branches on `path` — gate ROT")]
    end = _balanced(body, body.find("{", when), "{", "}")
    branch = body[when:end if end > 0 else len(body)]
    routed = False
    for line in branch.split("\n"):
        code = line.split("//")[0]
        if COMPACT_PATH in code and "->" in code:
            if "entryIdMap" in code.split("->", 1)[1]:
                routed = True
    if not routed:
        return [(
            CFG_REL,
            line_of(src, src.find("fun remapDefaultsIds(")),
            "remapDefaultsIds does not route " + COMPACT_PATH + " to entryIdMap"
            " — a restored install silently loses its pinned compaction model",
        )]
    return []


def check_import_chat_sections(src):
    """B: the session binding write must route through remapSessionBindingJson."""
    body = function_text(src, "internal suspend fun importChatSections(")
    if body is None:
        return [(CFG_REL, 0, "importChatSections( not found — gate ROT")]
    code = code_only(body)
    if REMAP_SESSION_CALL not in code:
        return [(
            CFG_REL,
            line_of(src, src.find("internal suspend fun importChatSections(")),
            "importChatSections writes modelBinding without " + REMAP_SESSION_CALL +
            " — restored sessions keep the source install's uuids",
        )]
    if re.search(r"modelBinding\s*=\s*s\.optString\(", code):
        return [(
            CFG_REL,
            line_of(src, src.find("internal suspend fun importChatSections(")),
            "modelBinding still written straight from the payload (s.optString) — "
            "the remap call is bypassed",
        )]
    # The call site must actually hand over the maps it was given.
    call = code[code.find(REMAP_SESSION_CALL):]
    call = call[: _balanced(call, call.find("(")) if "(" in call else len(call)]
    for arg in ("groupIdMap", "entryIdMap"):
        if arg not in call:
            return [(
                CFG_REL,
                line_of(src, src.find("internal suspend fun importChatSections(")),
                "importChatSections calls " + REMAP_SESSION_CALL + " without " + arg,
            )]
    return []


def check_memory_pack(src):
    """C1: the backup's memory section must pack the rollup as well."""
    body = function_text(src, "fun buildSections(")
    if body is None:
        return [(CFG_REL, 0, "buildSections( not found — gate ROT")]
    if MEM_PACK_CALL not in code_only(body):
        return [(
            CFG_REL,
            line_of(src, src.find("fun buildSections(")),
            "buildSections does not use " + MEM_PACK_CALL +
            " — memoryFiles falls back to listAllFiles() and drops the rollup",
        )]
    fn = function_text(src, "fun memoryBackupEntries(")
    if fn is None:
        return [(CFG_REL, 0, "memoryBackupEntries( not found — gate ROT")]
    if "ROLLUP_BACKUP_FILE" not in code_only(fn):
        return [(
            CFG_REL,
            line_of(src, src.find("fun memoryBackupEntries(")),
            "memoryBackupEntries does not carry ROLLUP_BACKUP_FILE",
        )]
    const = re.search(r"const val ROLLUP_BACKUP_FILE: String =\s*\n?\s*([^\n]+)", src)
    if const is None or ROLLUP_ARG not in const.group(1):
        return [(
            CFG_REL,
            line_of(src, src.find("const val ROLLUP_BACKUP_FILE")) if "const val ROLLUP_BACKUP_FILE" in src else 0,
            "ROLLUP_BACKUP_FILE is not sourced from " + ROLLUP_ARG +
            " (single source of truth for the on-disk name)",
        )]
    return []


def check_restore_writeback(src):
    """C2: Stage 6 must write payload entries by NAME (a whitelist drops rollup)."""
    body = function_text(src, "suspend fun import(")
    if body is None:
        return [(CFG_REL, 0, "import( not found — gate ROT")]
    if SAVE_BY_NAME not in code_only(body):
        return [(
            CFG_REL,
            line_of(src, src.find("suspend fun import(")),
            "memory restore no longer writes payload entries by name (" + SAVE_BY_NAME +
            ") — a rollup entry in the payload would be dropped",
        )]
    return []


def check_delete_guard(src):
    """C3: deleteFile must refuse the rollup product file."""
    body = function_text(src, "fun deleteFile(name: String)")
    if body is None:
        return [(MEM_REL, 0, "deleteFile( not found — gate ROT")]
    if ROLLUP_GUARD not in code_only(body):
        return [(
            MEM_REL,
            line_of(src, src.find("fun deleteFile(name: String)")),
            "deleteFile has no " + ROLLUP_GUARD + " guard — the distilled memory "
            "layer can be deleted (and is lost on the next backup/restore)",
        )]
    return []


def check(root, raw_cfg=None, raw_mem=None):
    if raw_cfg is None:
        with open(os.path.join(root, CFG_REL), encoding="utf-8") as f:
            raw_cfg = f.read()
    if raw_mem is None:
        with open(os.path.join(root, MEM_REL), encoding="utf-8") as f:
            raw_mem = f.read()
    out = []
    out += check_remap_defaults(raw_cfg)
    out += check_import_chat_sections(raw_cfg)
    out += check_memory_pack(raw_cfg)
    out += check_restore_writeback(raw_cfg)
    out += check_delete_guard(raw_mem)
    return out


def selftest(root):
    """Each criterion must go RED on its own targeted mutation of the real tree.

    Fixtures are derived from the repository sources (never hand-written), so
    the self-test cannot pass on a tree whose shape the gate no longer matches.
    """
    with open(os.path.join(root, CFG_REL), encoding="utf-8") as f:
        cfg = f.read()
    with open(os.path.join(root, MEM_REL), encoding="utf-8") as f:
        mem = f.read()

    def on_cfg(fn):
        return lambda c, m: fn(c)

    def on_mem(fn):
        return lambda c, m: fn(m)

    arms = []

    def arm(name, crit, cfg=cfg, mem=mem):
        arms.append((name, crit, cfg, mem))

    def sub_once(text, old, new):
        assert text.count(old) >= 1, "fixture anchor missing: " + old
        return text.replace(old, new, 1)

    # 0. pristine must be green.
    arm("pristine", None)

    # 1. drop the compact path branch.
    arm(
        "M1 compact branch removed",
        on_cfg(check_remap_defaults),
        cfg=sub_once(cfg, '"defaults.agentLoopEntries", "runtime.compactModelEntryId" -> entryIdMap',
                     '"defaults.agentLoopEntries" -> entryIdMap'),
    )
    # 2. compact path present but only in a comment.
    arm(
        "M2 compact path demoted to comment",
        on_cfg(check_remap_defaults),
        cfg=sub_once(cfg, '"defaults.agentLoopEntries", "runtime.compactModelEntryId" -> entryIdMap',
                     '"defaults.agentLoopEntries" -> entryIdMap // runtime.compactModelEntryId'),
    )
    # 2b. compact path routed to the WRONG map.
    arm(
        "M2b compact routed to groupIdMap",
        on_cfg(check_remap_defaults),
        cfg=sub_once(cfg, '"defaults.agentLoopEntries", "runtime.compactModelEntryId" -> entryIdMap',
                     '"defaults.agentLoopEntries" -> entryIdMap\n            "runtime.compactModelEntryId" -> groupIdMap'),
    )
    # 3. binding written verbatim again.
    arm(
        "M3 binding not remapped",
        on_cfg(check_import_chat_sections),
        cfg=sub_once(
            cfg,
            """modelBinding = remapSessionBindingJson(
                        s.optString("modelBinding").ifEmpty { null },
                        groupIdMap = groupIdMap,
                        entryIdMap = entryIdMap,
                    ),""",
            'modelBinding = s.optString("modelBinding").ifEmpty { null },',
        ),
    )
    # 4. maps not handed over at the call site.
    arm(
        "M4 maps dropped from call site",
        on_cfg(check_import_chat_sections),
        cfg=sub_once(cfg, """                        groupIdMap = groupIdMap,
                        entryIdMap = entryIdMap,
""", ""),
    )
    # 5. memory packing falls back to the plain listing.
    arm(
        "M5 memory pack loses rollup",
        on_cfg(check_memory_pack),
        cfg=sub_once(cfg, "val entries = memoryBackupEntries(", "val entries = listAllFilesOnly("),
    )
    # 6. rollup name re-spelled instead of sourced from the engine const.
    arm(
        "M6 rollup name re-spelled",
        on_cfg(check_memory_pack),
        cfg=sub_once(cfg, ROLLUP_ARG, '"MEMORY-ROLLUP.md"'),
    )
    # 7. restore write-back replaced by a name whitelist.
    arm(
        "M7 restore whitelist drops rollup",
        on_cfg(check_restore_writeback),
        cfg=sub_once(cfg, SAVE_BY_NAME, 'memoryRepo.saveFile("GLOBAL.md", content)'),
    )
    # 8. rollup delete guard removed.
    arm(
        "M8 delete guard removed",
        on_mem(check_delete_guard),
        mem=sub_once(mem, "        " + ROLLUP_GUARD + "\n", ""),
    )

    failures = []
    for name, crit, c, m in arms:
        violations = check(root, raw_cfg=c, raw_mem=m)
        if crit is None:
            if violations:
                failures.append("%s: expected GREEN, got %r" % (name, violations))
            continue
        expected = crit(c, m)
        if not expected:
            failures.append("%s: criterion did not fire (dead tooth)" % name)
            continue
        missing = [v for v in expected if v not in violations]
        if missing:
            failures.append("%s: expected violation missing: %r" % (name, missing))
        collateral = [v for v in violations if v not in expected]
        if collateral:
            failures.append("%s: collateral violation %r" % (name, collateral))

    if failures:
        for f in failures:
            print("SELFTEST FAIL: " + f)
        return 1
    print("selftest: %d arms matched (pristine green + %d targeted mutations)"
          % (len(arms), len(arms) - 1))
    print("BACKUP_REMAP_ROLLUP_SELFTEST=PASS")
    return 0


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--root", dest="root_opt")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    root = args.root_opt or args.root
    if not os.path.isdir(os.path.join(root, "src/android/app/src/main/java/com/rikkaminis/app")):
        print("ROT: %s does not look like the RikkaMinis root" % root)
        return 2
    if args.selftest:
        return selftest(root)
    violations = check(root)
    if violations:
        for rel, line, msg in violations:
            print("%s:%s: %s" % (rel, line, msg))
        print("backup_remap_rollup_check: %d violation(s)" % len(violations))
        return 1
    print("backup_remap_rollup_check: OK (compact pin remapped, binding remapped, "
          "rollup packed + guarded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

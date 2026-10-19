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

  D. VOICE GROUP BINDINGS (ConfigBuiltins.registerDefaults + remapDefaultsIds)
     [da1031-A] `defaults.voiceInputGroup` / `defaults.voiceOutputGroup` are
     group-id references exactly like `defaults.primaryGroup`, but they were
     missing from BOTH the registry (so exportJSON() never carried them in the
     backup's `fields` section) and `remapDefaultsIds` (so a restore that did
     carry them left them pointing at the source install's group id). Either
     way NULL on the target — the voice panel silently falls back to the
     System engine. Both halves are needed: registering without remapping
     heals nothing, remapping without registering never sees a value.

  E. THINKING-RULE RE-ATTACH KEY (ConfigBackup.import Stage 1 vs Stage 1.5)
     [da1031-A] rules ride a (providerType, providerLabel) key. Stage 1 used
     to register with the label importInstanceJSON RESOLVED (auto-renamed to
     "$label ($suffix)" on a collision) while Stage 1.5 looks up the backup's
     ORIGINAL providerLabel — on any install that already had the provider,
     the whole thinking-rules group was silently dropped. Both sides must
     spell the backup's own label.

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
BUILTINS_REL = "src/android/app/src/main/java/com/rikkaminis/app/config/ConfigBuiltins.kt"

COMPACT_PATH = '"runtime.compactModelEntryId"'
ROLLUP_ARG = "com.rikkaminis.app.workspace.MemoryRollupEngine.ROLLUP_FILE"
REMAP_SESSION_CALL = "remapSessionBindingJson("
MEM_PACK_CALL = "memoryBackupEntries("
ROLLUP_GUARD = "if (name == ROLLUP_FILE) return false"
SAVE_BY_NAME = "memoryRepo.saveFile(name, content)"

# ── [da1031-A] ISSUE-03: the two voice-group bindings ────────────────────
# D. VOICE GROUP BINDINGS (registry + remapDefaultsIds + thinking-rule key)
#    `defaults.voiceInputGroup` / `defaults.voiceOutputGroup` are group-id
#    references exactly like `defaults.primaryGroup`, but they were missing
#    from BOTH the registry (so ConfigRegistry.exportJSON() never carried them
#    in the backup's `fields` section) and `remapDefaultsIds` (so a restore
#    that did carry them left them pointing at the source install's group id).
#    NULL on the target either way — the voice panel silently falls back to the
#    System engine. Both halves are needed: registering without remapping heals
#    nothing, remapping without registering never sees a value.
VOICE_GROUP_PATHS = ("defaults.voiceInputGroup", "defaults.voiceOutputGroup")
VOICE_GROUP_READERS = ("repo.voiceInputGroupId ?: \"\"", "repo.voiceOutputGroupId ?: \"\"")

# E. THINKING-RULE RE-ATTACH KEY (Stage 1 registration vs Stage 1.5 lookup)
#    Rules ride a (providerType, providerLabel) key. Stage 1 used to register
#    the key with the label importInstanceJSON RESOLVED (auto-renamed to
#    "$label ($suffix)" on a collision), while Stage 1.5 looks up the backup's
#    original providerLabel — so on any install that already had the provider,
#    the whole thinking-rules group was dropped. Both sides must spell the
#    backup's own label.
STAGE1_KEY_CALL = "providerKeyToId["
STAGE1_KEY_END = "] = instId"
STAGE15_LOOKUP = 'providerKeyToId["$type\\u0000$label"]'


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


def check_voice_group_remap(src):
    """D2: remapDefaultsIds must route both voice group paths to groupIdMap."""
    body = function_text(src, "fun remapDefaultsIds(")
    if body is None:
        return [(CFG_REL, 0, "remapDefaultsIds( not found — gate ROT")]
    when = body.find("when (path)")
    if when < 0:
        return [(CFG_REL, 0, "remapDefaultsIds no longer branches on `path` — gate ROT")]
    end = _balanced(body, body.find("{", when), "{", "}")
    branch = body[when:end if end > 0 else len(body)]
    out = []
    for path in VOICE_GROUP_PATHS:
        routed = False
        for line in branch.split("\n"):
            code = line.split("//")[0]
            if ('"%s"' % path) in code and "->" in code:
                if "groupIdMap" in code.split("->", 1)[1]:
                    routed = True
        if not routed:
            out.append((
                CFG_REL,
                line_of(src, src.find("fun remapDefaultsIds(")),
                "remapDefaultsIds does not route \"%s\" to groupIdMap — a restored "
                "install keeps the source install's group id and the voice binding dangles" % path,
            ))
    return out


def check_voice_defaults_registered(src_builtins):
    """D1: registerDefaults must register both voice group bindings, reading
    and writing the repository properties (a registered field that does not
    touch the repo would export "" forever)."""
    body = function_text(src_builtins, "fun registerDefaults(")
    if body is None:
        return [(BUILTINS_REL, 0, "registerDefaults( not found — gate ROT")]
    code = code_only(body)
    base = line_of(src_builtins, src_builtins.find("fun registerDefaults("))
    out = []
    for path, reader in zip(VOICE_GROUP_PATHS, VOICE_GROUP_READERS):
        if ('path = "%s"' % path) not in code:
            out.append((
                BUILTINS_REL,
                base,
                "\"%s\" is not registered in ConfigBuiltins.registerDefaults — "
                "the backup `fields` section cannot carry it and a restore drops "
                "the voice binding" % path,
            ))
        elif reader not in code:
            out.append((
                BUILTINS_REL,
                base,
                "\"%s\" is registered but its reader does not read %s — the field "
                "exports a constant instead of the live binding" % (path, reader.split(" ?:")[0].strip()),
            ))
    return out


def _provider_key_assignment(src):
    """The `providerKeyToId[…] = instId` Stage-1 assignment text (code only)."""
    body = function_text(src, "    suspend fun import(")
    if body is None:
        return None
    i = body.find(STAGE1_KEY_CALL)
    if i < 0:
        return None
    j = body.find(STAGE1_KEY_END, i)
    if j < 0:
        return None
    return code_only(body[i:j])


def check_thinking_rule_key(src):
    """E: Stage 1 must key by the BACKUP's original label (Stage 1.5 looks the
    (providerType, providerLabel) pair up — never the rename result), and the
    Stage 1.5 lookup must keep the same key shape."""
    body = function_text(src, "    suspend fun import(")
    if body is None:
        return [(CFG_REL, 0, "import( not found — gate ROT")]
    base = line_of(src, src.find("    suspend fun import("))
    key = _provider_key_assignment(src)
    if key is None:
        return [(CFG_REL, base,
                 "Stage 1 no longer registers " + STAGE1_KEY_CALL + "…" + STAGE1_KEY_END +
                 " — thinking rules can never re-attach to a restored provider")]
    if "resolvedLabel" in key:
        return [(CFG_REL, base,
                 "Stage 1 keys the thinking-rule lookup by the auto-renamed label "
                 "(resolvedLabel) — Stage 1.5 looks up the backup's original "
                 "(providerType, providerLabel) pair, so a label-conflict import "
                 "silently loses its whole thinking-rules group")]
    if "+ label" not in key:
        return [(CFG_REL, base,
                 "Stage 1 provider key no longer uses the backup's original label")]
    if STAGE15_LOOKUP not in code_only(body):
        return [(CFG_REL, base,
                 "Stage 1.5 no longer looks up " + STAGE15_LOOKUP +
                 " — the two sides of the (providerType, providerLabel) contract diverged")]
    return []


def check(root, raw_cfg=None, raw_mem=None, raw_builtins=None):
    if raw_cfg is None:
        with open(os.path.join(root, CFG_REL), encoding="utf-8") as f:
            raw_cfg = f.read()
    if raw_mem is None:
        with open(os.path.join(root, MEM_REL), encoding="utf-8") as f:
            raw_mem = f.read()
    if raw_builtins is None:
        with open(os.path.join(root, BUILTINS_REL), encoding="utf-8") as f:
            raw_builtins = f.read()
    out = []
    out += check_remap_defaults(raw_cfg)
    out += check_voice_group_remap(raw_cfg)
    out += check_voice_defaults_registered(raw_builtins)
    out += check_thinking_rule_key(raw_cfg)
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
    with open(os.path.join(root, BUILTINS_REL), encoding="utf-8") as f:
        bui = f.read()

    def on_cfg(fn):
        return lambda c, m, b, _fn=fn: _fn(c)

    def on_mem(fn):
        return lambda c, m, b, _fn=fn: _fn(m)

    def on_bui(fn):
        return lambda c, m, b, _fn=fn: _fn(b)

    arms = []

    def arm(name, crit, cfg=cfg, mem=mem, bui=bui):
        arms.append((name, crit, cfg, mem, bui))

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

    # ── [da1031-A] arms 9-13 ─────────────────────────────────────────────
    # 9. voice group binding dropped from remapDefaultsIds' group branch.
    arm(
        "M9 voiceInputGroup not routed",
        on_cfg(check_voice_group_remap),
        cfg=sub_once(cfg, '"defaults.voiceInputGroup", ', ""),
    )
    # 9b. routed to the WRONG map.
    arm(
        "M9b voiceOutputGroup routed to entryIdMap",
        on_cfg(check_voice_group_remap),
        cfg=sub_once(cfg,
                     '"defaults.voiceOutputGroup", "defaults.agentLoopGroups" -> groupIdMap',
                     '"defaults.agentLoopGroups" -> groupIdMap\n        "defaults.voiceOutputGroup" -> entryIdMap'),
    )
    # 10. registration dropped (path re-spelled → resolveField/exportJSON miss).
    arm(
        "M10 voiceOutputGroup unregistered",
        on_bui(check_voice_defaults_registered),
        bui=sub_once(bui, 'path = "defaults.voiceOutputGroup",', 'path = "defaults.voiceOutputGroupUnused",'),
    )
    # 10b. registered but reading a constant instead of the repo property.
    arm(
        "M10b voiceInputGroup reader detached",
        on_bui(check_voice_defaults_registered),
        bui=sub_once(bui, 'reader = { ConfigValue.Str(repo.voiceInputGroupId ?: "") },',
                     'reader = { ConfigValue.Str("") },'),
    )
    # 11. Stage 1 keyed by the auto-renamed label again (the ISSUE-03-B defect).
    arm(
        "M11 stage1 keyed by resolvedLabel",
        on_cfg(check_thinking_rule_key),
        cfg=sub_once(cfg, 'obj.optString("providerType", "") + "\\u0000" + label',
                     'obj.optString("providerType", "") + "\\u0000" + resolvedLabel'),
    )
    # 11b. Stage 1.5 lookup diverged from the Stage 1 key.
    arm(
        "M11b stage15 lookup diverged",
        on_cfg(check_thinking_rule_key),
        cfg=sub_once(cfg, 'providerKeyToId["$type\\u0000$label"]',
                     'providerKeyToId["$type\\u0000" + resolvedLabelFor(label)]'),
    )
    # 11c. Stage 1 key registration removed entirely.
    arm(
        "M11c stage1 registration removed",
        on_cfg(check_thinking_rule_key),
        cfg=sub_once(cfg, STAGE1_KEY_CALL, "providerKeyMap["),
    )

    failures = []
    for name, crit, c, m, b in arms:
        violations = check(root, raw_cfg=c, raw_mem=m, raw_builtins=b)
        if crit is None:
            if violations:
                failures.append("%s: expected GREEN, got %r" % (name, violations))
            continue
        expected = crit(c, m, b)
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

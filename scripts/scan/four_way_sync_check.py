#!/usr/bin/env python3
"""
Four-way sync check v2.1 — Model ↔ Entity ↔ to-DB ↔ from-DB field consistency.

Background: Adding fields to a data class and forgetting to update any of the
other layers causes silent data loss (GH#68 image-endpoint, ProviderInstance.pinned,
F-199 thinking-rule id read-side drop). Lint/compilation don't catch this.

v1 hardcoded 2 families (ProviderInstance, ModelGroup) — the other 8 Room
entities' field gaps were unchecked. v2 DERIVES the families from
data/db/*Entity.kt: for every Room entity it resolves the domain model class
and the constructor sites that build each side, then checks the same three
gap classes:

  1. Model has but Entity missing  → field saved nowhere, lost on every save
  2. Entity has but to-DB site doesn't set it → saved as NULL/default forever
  3. Model has but from-DB site doesn't restore it → lost on every load

Families that can't be resolved (no model data class, no constructor sites)
are printed in an UNRESOLVED list — visible, exit 0, NOT failures. The
coverage table makes the blind spot explicit instead of silent.

v2.1 (2026-10-13, review D-1): v2's honest-UNRESOLVED design left the chat
core tables (ChatSessionEntity / MessageEntity / …) silently unchecked —
real-tree run printed "checked 4 · unresolved 6" next to "ALL CLEAN ✅".
Those families are their own domain shape (no `ChatSession`/`Message` data
class): ENTITY_SELF_FAMILIES now checks every column is written by SOME
constructor site, with audited pins for DAO-written lifecycle columns.
The Done line now carries the coverage counts so a "clean" can never again
be read without its denominator.

Usage: python3 scripts/scan/four_way_sync_check.py <repo_root>
Exit code: 0 = clean (or unresolved-only), 1 = real field gaps found
"""
import os
import re
import sys

KOTLIN = "src/android/app/src/main/java/com/rikkaminis/app/"
DB_DIR = "data/db"

# ── Naming-convention breaks (audited pins, each with a reason line) ──────
# Entity name → domain model data class name. Only needed when the model
# class name is NOT the entity name minus the "Entity" suffix.
FAMILY_MODEL_ALIASES = {
    # The "Provider" prefix belongs to the table name, not the domain class:
    # the model is ThinkingRule (provider/thinking/ThinkingRule.kt). This
    # family is checked because F-199 (persisted rule id dropped on the read
    # side of toRule) was a real bug in exactly this family.
    "ProviderThinkingRuleEntity": "ThinkingRule",
    # Same prefix break: the domain classes are ModelEntry / ModelGroup
    # (data/model/ProviderConfig.kt); the entity names carry the table
    # prefix. Both were v1-hardcoded families — v2 must keep them checked.
    "ProviderModelEntryEntity": "ModelEntry",
    "ProviderModelGroupEntity": "ModelGroup",
}

# ── Field aliases: model field ↔ entity column (1:1 renames, audited) ─────
FIELD_ALIASES = {
    "ProviderInstance": {
        # [T-multi-api-key] Credential list is List<ProviderCredentialMeta> on
        # the model but a JSON blob column (credentialsJson) in Room — same
        # shape as ModelGroup.memberEntryIdsJson.
        "credentials": "credentialsJson",
    },
    "ModelGroup": {
        # List<String> memberEntryIds serialized into a JSON blob column.
        "memberEntryIds": "memberEntryIdsJson",
    },
    "ModelEntry": {
        # LLMModel object serialized into baseModelJson; overrides object into
        # overridesJson; the model's `uuid` becomes the composite-key `id`
        # column ("{instanceId}/{modelId}" — see compositeEntryKey).
        "baseModel": "baseModelJson",
        "overrides": "overridesJson",
        "uuid": "id",
    },
}

# ── KNOWN_MISMATCH pins: (group, field) → reason ──────────────────────────
# Fields that have NO 1:1 entity counterpart by design (transform / blob
# serialization). Printed as a WARNING line every run so the known gap stays
# visible instead of silently swallowed; never counted as a failure.
KNOWN_MISMATCH = {
    ("ThinkingRule", "kind"): (
        "sealed Kind is transformed into scopeKind (+scopePattern) via "
        "toEntity's `when` — no 1:1 column by design"
    ),
    ("ThinkingRule", "scope"): (
        "sealed Scope (AllModels/ModelPattern) serialized into the "
        "scopeKind/scopePattern column pair via toEntity's `when`"
    ),
    ("ThinkingRule", "wireFormat"): (
        "ThinkingWireFormat object serialized into the wire_format_json blob "
        "(wireFormatJson) via ThinkingRuleCoding.encodeWireFormat"
    ),
    ("ThinkingRule", "reasoningEcho"): (
        "ReasoningEchoPolicy serialized into the reasoning_echo_json blob "
        "(reasoningEchoJson) via ThinkingRuleCoding.encodeEcho"
    ),
}

# Entity-internal columns that legitimately have no model counterpart.
LEGIT_ENTITY_ONLY = {"sortOrder"}

# ── ENTITY_SELF_FAMILIES: entities checked against their OWN ctor sites ──
# v2 left these UNRESOLVED (no `ChatSession`/`Message` domain data class
# exists — the entity IS the domain shape). v2.1 checks every column is
# written by SOME constructor site. The set is AUDITED-PINNED (each name has
# a reason); an entity NOT listed and WITHOUT a domain data class still lands
# in UNRESOLVED visibly, so a new chat-table-shaped entity can't slip through
# silently.
#
# DAO_WRITTEN pins: lifecycle columns no constructor ever passes, written by
# @Query UPDATE statements instead. Audited per entity (grepped ChatDao.kt).
ENTITY_SELF_FAMILIES = {
    # Chat core tables — the exact family "field saved nowhere" historically
    # bites (session metadata, message parts/reasoning/usage columns).
    "ChatSessionEntity",
    "MessageEntity",
    # Single-site entities where a dropped ctor arg would silently default.
    "CompactMarkerEntity",
    "ProviderAgentLoopIdEntity",
    "ProviderConfigMetaEntity",   # positional ctor sites (KEY, value) — handled
    "WebAppShortcutEntity",
}
POSITIONAL_ONLY_SITES = {
    # ProviderConfigMetaEntity(KEY, value): every site is positional, so
    # named-arg extraction sees 0 args. Each site assigns fields by ORDER;
    # a no-named-args site is treated as "covers all non-defaulted fields".
    "ProviderConfigMetaEntity",
}
DAO_WRITTEN = {
    "MessageEntity": {
        "updatedAt",          # ChatDao: UPDATE messages SET … updated_at
        "streamInterruptCount",  # ChatDao: stream_interrupt_count = … + 1
    },
    "ChatSessionEntity": {
        "updatedAt",          # ChatDao: UPDATE sessions SET updated_at
    },
}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def balanced(src, start, open_ch, close_ch):
    """Return index of the close_ch matching src[start] == open_ch, or -1."""
    depth = 0
    i = start
    while i < len(src):
        c = src[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def extract_data_class_fields(src, cls_name):
    """Fields (val/var names) of `data class <cls_name>( ... )` — ctor only."""
    m = re.search(r"data\s+class\s+" + re.escape(cls_name) + r"\s*\(", src)
    if not m:
        return None
    end = balanced(src, m.end() - 1, "(", ")")
    if end < 0:
        return None
    body = src[m.end():end]
    return set(re.findall(r"\b(?:val|var)\s+(\w+)\s*:", body))


def extract_defaulted_fields(src, cls_name):
    """Fields of `data class <cls_name>` that declare a default (`= …`).
    A fully positional ctor call can legally omit only these."""
    m = re.search(r"data\s+class\s+" + re.escape(cls_name) + r"\s*\(", src)
    if not m:
        return set()
    end = balanced(src, m.end() - 1, "(", ")")
    if end < 0:
        return set()
    body = src[m.end():end]
    # [review G2] `[^=\n]*` — a bare `[^=]*` runs across lines and swallows the
    # NEXT field's `=`, mis-flagging the previous field as defaulted (e.g.
    # `val key: String,\nval value: String = "x"` reported 'key' instead of
    # 'value'), which silently voids the positional-branch gate.
    return set(re.findall(r"\b(?:val|var)\s+(\w+)\s*:[^=\n]*=\s", body))


def collect_ctor_sites(files, cls_name):
    """All `cls_name(` call sites across files, skipping the file that
    declares the class itself (the data-class header would otherwise be
    parsed as a call and default values as named args).

    Returns (named_args_union, sites) where sites = [(relpath, line, args)].
    """
    decl = re.compile(r"\bclass\s+" + re.escape(cls_name) + r"\b")
    call = re.compile(r"(?<![\w])" + re.escape(cls_name) + r"\s*\(")
    args = set()
    sites = []
    for rel, src in sorted(files.items()):
        if decl.search(src):
            continue
        for m in call.finditer(src):
            end = balanced(src, m.end() - 1, "(", ")")
            if end < 0:
                continue
            body = src[m.end():end]
            site_args = set(re.findall(r"\b(\w+)\s*=\s*", body))
            args.update(site_args)
            sites.append((rel, src.count("\n", 0, m.start()) + 1, site_args))
    return args, sites


def discover_entities(db_dir_rel, root):
    """Every `data class *Entity` in data/db/ → {entity_name: fields}."""
    entities = {}
    db_path = os.path.join(root, KOTLIN, db_dir_rel)
    if not os.path.isdir(db_path):
        return entities
    for fn in sorted(os.listdir(db_path)):
        if not fn.endswith(".kt"):
            continue
        src = read(os.path.join(db_path, fn))
        for m in re.finditer(r"data\s+class\s+(\w*Entity)\s*\(", src):
            name = m.group(1)
            fields = extract_data_class_fields(src, name)
            if fields is not None:
                entities[name] = fields
    return entities


def load_kotlin_files(root):
    files = {}
    base = os.path.join(root, KOTLIN)
    for dirpath, dirnames, filenames in os.walk(base):
        for fn in filenames:
            if fn.endswith(".kt"):
                rel = os.path.relpath(os.path.join(dirpath, fn), base)
                files[rel] = read(os.path.join(dirpath, fn))
    return files


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    problems = 0
    files = load_kotlin_files(root)
    entities = discover_entities(DB_DIR, root)

    checked = []
    entity_self_checked = []
    unresolved = []  # (entity, reason)
    for entity_name in sorted(entities):
        entity_fields = entities[entity_name]

        # ── v2.1: entity-self families (no domain data class exists) ──
        if entity_name in ENTITY_SELF_FAMILIES:
            site_args, sites = collect_ctor_sites(files, entity_name)
            if not sites:
                unresolved.append(
                    (entity_name, "entity-self family but no constructor "
                     "site found — columns unchecked")
                )
                continue
            entity_self_checked.append(entity_name)
            dao = DAO_WRITTEN.get(entity_name, set())
            positional = entity_name in POSITIONAL_ONLY_SITES
            if positional:
                # Every site assigns by ORDER — a site with zero extractable
                # named args covers all non-defaulted fields by position.
                entity_src = read(os.path.join(
                    root, KOTLIN, DB_DIR, entity_name + ".kt"))
                defaulted = extract_defaulted_fields(entity_src, entity_name)
                site_args = entity_fields - defaulted
                print(f"\n═══ {entity_name} — entity-self check "
                      f"(positional ctor sites) ═══")
            else:
                print(f"\n═══ {entity_name} — entity-self check "
                      f"(no domain data class) ═══")
            print(f"  entity: {DB_DIR}/{entity_name}.kt · {len(sites)} ctor site(s)"
                  + (f" · DAO-written pins: {sorted(dao)}" if dao else ""))
            print(f"  Entity ({len(entity_fields)}): {sorted(entity_fields)}")
            if not positional:
                partial = {
                    f: (sum(1 for _, _, a in sites if f in a), len(sites))
                    for f in sorted(entity_fields & site_args)
                    if sum(1 for _, _, a in sites if f in a) < len(sites)
                }
                for f, (n, total) in partial.items():
                    print(f"  ℹ set by {n}/{total} site(s) (OK if the other sites "
                          f"rely on the column default): {f}")
            never = entity_fields - site_args - dao
            if never:
                problems += 1
                print(f"  ❌ Column never written by ANY constructor site and not "
                      f"DAO-written-pinned (saved as its default forever — if that "
                      f"is intended, pin it in DAO_WRITTEN): {sorted(never)}")
            else:
                print(f"  ✅ Every column written by some constructor site "
                      f"(or pinned DAO-written)")
            continue

        model_name = entity_name[: -len("Entity")]
        model_name = FAMILY_MODEL_ALIASES.get(entity_name, model_name)

        model_fields = None
        model_file = None
        for rel, src in sorted(files.items()):
            if re.search(r"data\s+class\s+" + re.escape(model_name) + r"\s*\(", src):
                model_fields = extract_data_class_fields(src, model_name)
                model_file = rel
                break
        if model_fields is None:
            unresolved.append(
                (entity_name, f"no data class '{model_name}' under {KOTLIN} "
                 "(domain shape not derivable — family unchecked; if the entity "
                 "IS its own domain shape, pin it in ENTITY_SELF_FAMILIES)")
            )
            continue

        todb_args, todb_sites = collect_ctor_sites(files, entity_name)
        fromdb_args, fromdb_sites = collect_ctor_sites(files, model_name)
        if not todb_sites:
            unresolved.append(
                (entity_name, "no constructor site writes the entity row "
                 "(mapping direction not found) — family unchecked")
            )
            continue
        if not fromdb_sites:
            unresolved.append(
                (entity_name, f"no constructor site builds '{model_name}' "
                 "back from rows (load direction not found) — family unchecked")
            )
            continue

        checked.append(entity_name)
        aliases = FIELD_ALIASES.get(model_name, {})
        print(f"\n═══ {model_name} — four-way sync check (derived) ═══")
        print(f"  model: {model_file} · entity: {DB_DIR}/{entity_name}.kt · "
              f"to-db: {len(todb_sites)} site(s) · from-db: {len(fromdb_sites)} site(s)")
        print(f"  Model ({len(model_fields)}): {sorted(model_fields)}")
        print(f"  Entity ({len(entity_fields)}): {sorted(entity_fields)}")

        missing_entity = set()
        for f in model_fields:
            if f in entity_fields:
                continue
            alias = aliases.get(f)
            if alias and alias in entity_fields:
                continue
            if (model_name, f) in KNOWN_MISMATCH:
                continue  # printed as WARNING below
            missing_entity.add(f)
        missing_todb = entity_fields - todb_args
        missing_fromdb = model_fields - fromdb_args
        missing_todb -= LEGIT_ENTITY_ONLY
        missing_fromdb -= LEGIT_ENTITY_ONLY

        for (grp, fld), reason in sorted(KNOWN_MISMATCH.items()):
            if grp == model_name and fld in model_fields:
                print(f"  ⚠ KNOWN-MISMATCH (pinned, by design): {grp}.{fld} — {reason}")

        if not missing_entity and not missing_todb and not missing_fromdb:
            print(f"  ✅ All four layers consistent")
        else:
            if missing_entity:
                problems += 1
                print(f"  ❌ Model has but Entity missing (data lost on save): {sorted(missing_entity)}")
            if missing_todb:
                problems += 1
                print(f"  ❌ Entity has but toSnapshot missing (to-DB site doesn't set it — lost on every save): {sorted(missing_todb)}")
            if missing_fromdb:
                problems += 1
                print(f"  ❌ Model has but toProviderConfig missing (from-DB site doesn't restore it — lost on every load): {sorted(missing_fromdb)}")

    # ── Coverage table: the blind spot must be printed, not silent ─────────
    print(f"\n{'='*60}")
    print(f"Coverage: derived {len(entities)} entities · checked {len(checked)} "
          f"+ entity-self {len(entity_self_checked)} "
          f"(total {len(checked) + len(entity_self_checked)}) "
          f"· unresolved {len(unresolved)}")
    if unresolved:
        print("UNRESOLVED (visible, not failures — families NOT field-checked):")
        for name, reason in unresolved:
            print(f"  - {name}: {reason}")
    if not entities:
        print("!! No Room entities found — scanner is blind on this tree.")
    print(f"Done: {'PROBLEMS FOUND: ' + str(problems) if problems else 'ALL CLEAN ✅'} "
          f"[checked {len(checked) + len(entity_self_checked)}/{len(entities)}]")

    if not entities:
        return 1  # fail loud: a tree with no derivable families is a rot signal
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

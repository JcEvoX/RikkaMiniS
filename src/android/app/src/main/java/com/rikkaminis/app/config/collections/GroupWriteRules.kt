package com.rikkaminis.app.config.collections

import com.rikkaminis.app.config.ConfigError
import com.rikkaminis.app.config.ConfigValue
import com.rikkaminis.app.data.model.ModelGroup

/**
 * [fix/da1023-D / ISSUE-04] Pure write rules for GroupsCollection, extracted
 * as top-level internal functions so the CLI unit test can drive them
 * without a ProviderRepository (Context-bound, unconstructible on the JVM).
 * GroupsCollection is the only production caller of each; the write-funnel
 * wiring is pinned by scripts/scan/collection_copy_on_mutate_check.py.
 */

/**
 * Copy-on-mutate core: detach a LIVE ModelGroup, apply the edit on the copy,
 * return the working copy for the caller to persist. `group(id)` hands back
 * the LIVE object inside `_config.value.modelGroups`, and every field the
 * collection writes is a `var` — so an in-place `apply(g)` let a concurrent
 * reader (group detail screen, model picker, `groupsList` / `groups.update`
 * RPCs) observe a half-applied edit, and a throw after `apply` left memory
 * changed while the disk stayed old. Both sibling collections persist a NEW
 * object (ProvidersCollection.mutate, ModelsCollection.mutate), as do the
 * two entries converted by [fix/audit-b18 / T9-L2] (ProviderRepository.
 * updateGroup, ProviderMutationMethods.groupsUpdate); this was the third
 * entry of that family, left behind.
 *
 * `memberEntryIds` is the only MUTABLE member of ModelGroup, and a
 * data-class copy shares its backing list, so the copy must re-materialise
 * it; every other field is an immutable value (String / enum / Int?) and is
 * isolated by the copy itself.
 */
internal fun mutatedGroupCopy(g: ModelGroup, apply: (ModelGroup) -> Unit): ModelGroup {
    val working = g.copy(memberEntryIds = g.memberEntryIds.toMutableList())
    apply(working)
    return working
}

/**
 * [fix/da1023-D / ISSUE-04] Single decode point for a member-entry-id list
 * (the `groups.<id>.entries` writer and the `add` payload — `ConfigSchema.Array`
 * and `ConfigSchema.Json` do NOT validate element types, so the writer owns
 * the invariants). Three rules the old inline `mapNotNull` broke:
 *  * a non-string element is malformed input, not "absent": refuse it
 *    instead of dropping it silently. Same flavour as [fix/audit-0917-b9]
 *    in ModelsCollection (modalities), where a typo'd element wiped the
 *    whole override because the drop funnelled into the "empty = clear"
 *    branch;
 *  * an empty id is refused too (previously `add` dropped it quietly while
 *    `entries` rejected it later as an unknown uuid — one rule now);
 *  * duplicates collapse to their first occurrence. The group detail
 *    screen keys its reorderable list by entry id
 *    (ModelGroupDetailScreen.kt:308 `itemsIndexed(memberIds, key = { _, id -> id })`),
 *    and a repeated LazyColumn key throws `IllegalArgumentException` for
 *    the whole screen. `groups.<id>.entries.append` goes through the
 *    bridge's array path, which never checks for an existing occurrence,
 *    so the dedup has to sit at this write funnel.
 */
internal fun parseMemberEntryIds(items: List<ConfigValue>): List<String> {
    val out = ArrayList<String>(items.size)
    for (item in items) {
        val s = (item as? ConfigValue.Str)?.value
            ?: throw ConfigError.TypeMismatch("array entries must be strings")
        if (s.isEmpty()) throw ConfigError.InvalidValue("entry id must not be empty")
        if (s !in out) out.add(s)
    }
    return out
}

/**
 * [fix/da1023-D] Context-limit write semantics. Setting a positive limit also
 * refreshes the "last selected" memory the UI restores from when the
 * Limit-Context switch is toggled OFF→ON — without this, a CLI write
 * followed by an off/on round-trip in the group screen resurrected a STALE
 * limit (or the 128K default). Clearing (0) keeps the memory, mirroring the
 * UI's OFF branch which stashes rather than erases.
 */
internal fun applyContextLimitWrite(g: ModelGroup, i: Int) {
    g.contextLimitTokens = if (i == 0) null else i
    if (i > 0) g.lastContextLimitTokens = i
}

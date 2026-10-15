package com.rikkaminis.app.accessibility

import android.view.accessibility.AccessibilityNodeInfo
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

/**
 * [audit3-F-12] The eviction policy's view of one registry entry: its id, when
 * it was inserted, and in which order relative to its siblings. Deliberately
 * free of `AccessibilityNodeInfo` so [selectEvictions] can run (and be tested)
 * on a plain JVM.
 */
internal data class RegistryEntryMeta(val id: String, val createdAt: Long, val ordinal: Long)

/**
 * [audit3-F-12] Pure eviction policy for [NodeRegistry]: which ids to drop.
 *
 * Two independent bounds, decided in one pass:
 *
 *  1. **TTL** — anything older than [ttlMs] goes. This rule is unchanged, and
 *     [NodeRegistry.get] still refuses expired entries individually, so
 *     amortising the sweep can never hand a caller a dead id.
 *  2. **Capacity** — at most [maxEntries] survivors are kept, and the ones
 *     dropped are the OLDEST first. The pre-fix registry had no ceiling at
 *     all: a burst of `ui dump` calls inside one TTL window grew the map
 *     without limit, and every retained `AccessibilityNodeInfo` pins the
 *     framework node cache behind it.
 *
 * Age order comes from the insertion [ordinal], not from `createdAt`: a
 * 500-node tree is walked within a few milliseconds, so dozens of entries
 * share a timestamp, and a timestamp sort would evict an arbitrary subset of
 * one tree instead of whole trees. Ordinals are unique, so the order — and
 * therefore the returned list — is total and deterministic.
 *
 * No Android types in this function on purpose: `NodeRegistryEvictionTest`
 * exercises it, and the mutation negative controls, without a device.
 */
internal fun selectEvictions(
    entries: List<RegistryEntryMeta>,
    now: Long,
    ttlMs: Long,
    maxEntries: Int,
): List<String> {
    val expired = ArrayList<String>()
    val live = ArrayList<RegistryEntryMeta>(entries.size)
    for (e in entries) {
        if (now - e.createdAt > ttlMs) expired.add(e.id) else live.add(e)
    }
    // `coerceAtLeast` is load-bearing: with a degenerate cap, `overflow` would
    // exceed `live.size` and the oldest-first slice below would throw, taking
    // the whole sweep (and the insert that triggered it) down with it.
    val cap = maxEntries.coerceAtLeast(0)
    if (live.size <= cap) return expired
    live.sortBy { it.ordinal }
    val overflow = live.size - cap
    val doomed = ArrayList<String>(expired.size + overflow)
    doomed.addAll(expired)
    for (i in 0 until overflow) doomed.add(live[i].id)
    return doomed
}

/**
 * [audit3-F-12] Whether a [NodeRegistry] sweep is due on this insertion: once
 * every [sweepEveryPuts] inserts, or immediately when the map reaches
 * [maxEntries].
 *
 * The size test is what bounds memory — a single tree larger than the cap has
 * to be trimmed on the spot, not [sweepEveryPuts] inserts later (the check runs
 * BEFORE the insert, so a map at the ceiling is trimmed rather than grown). The
 * counter is what stops the sweep from running on every insert: it used to,
 * which made one `ui dump` of N nodes cost O(N^2) map iterations.
 *
 * The two conditions only amortise because the sweep trims to a LOW-WATER mark
 * below [maxEntries] ([NodeRegistry.KEEP_AFTER_SWEEP]): a sweep that landed
 * exactly on the ceiling would be due again on the very next insert. See the
 * measured arms in `NodeRegistryEvictionTest`.
 */
internal fun shouldSweep(
    putsSinceSweep: Int,
    size: Int,
    sweepEveryPuts: Int,
    maxEntries: Int,
): Boolean = putsSinceSweep >= sweepEveryPuts || size >= maxEntries

/**
 * Short-lived cache mapping opaque string IDs (e.g. "a3f2") to live
 * AccessibilityNodeInfo references. AccessibilityNodeInfo has no durable
 * identity across calls — `ui dump` allocates fresh objects every time
 * the framework re-enumerates the tree, but the AI agent expects to
 * reference a node by id seconds later in `tap node a3f2`.
 *
 * Entries expire after [TTL_MS], when the map reaches [MAX_ENTRIES] (oldest
 * first, by [selectEvictions]), or when [clear] is called (e.g. on service
 * destroy).
 */
class NodeRegistry {
    private data class Entry(
        val node: AccessibilityNodeInfo,
        val createdAt: Long,
        val ordinal: Long,
    )

    private val map = ConcurrentHashMap<String, Entry>()
    private val seq = AtomicLong(0)

    /** Feeds [Entry.ordinal]: strictly monotonic, never wrapped like [seq]. */
    private val inserts = AtomicLong(0)

    /** Inserts since the last sweep; the amortising counter of [shouldSweep]. */
    private val putsSinceSweep = AtomicInteger(0)

    fun put(node: AccessibilityNodeInfo): String {
        val now = System.currentTimeMillis()
        // [audit3-F-12] This used to be an unconditional `evictExpired()`: one
        // full traversal of the map PER INSERT. `walkNode` registers every
        // node of the tree, so a dump of N nodes cost sum(1..N) = O(N^2)
        // iterator steps (and repeated dumps inside one TTL window stacked M
        // live entries on top of that). Sweeping every SWEEP_EVERY_PUTS inserts
        // — or whenever the map hits the ceiling — makes a dump O(N) while
        // still bounding both the age and the size of what is kept.
        if (shouldSweep(putsSinceSweep.incrementAndGet(), map.size, SWEEP_EVERY_PUTS, MAX_ENTRIES)) {
            putsSinceSweep.set(0)
            sweep(now)
        }
        // [audit-0917] nextId() wraps at 2^20 (0xFFFFF) — after 1,048,576
        // allocations in one service lifetime a recycled id would overwrite a
        // LIVE entry, so `tap node <id>` could hit a node from a stale tree.
        // Loop until an unused id is found; the probe is cheap and the wrap
        // only matters on very long-running services.
        // Unchanged: this guard is why ids stay unique for as long as entries
        // stay live, and the capacity ceiling above does not weaken it (evicted
        // ids are gone, so a wrap onto one of them is a fresh insert).
        var id = nextId()
        var guard = 0
        while (map.containsKey(id) && guard++ < 64) {
            id = nextId()
        }
        map[id] = Entry(node, now, inserts.incrementAndGet())
        return id
    }

    fun get(id: String): AccessibilityNodeInfo? {
        val e = map[id] ?: return null
        if (System.currentTimeMillis() - e.createdAt > TTL_MS) {
            map.remove(id)
            return null
        }
        return e.node
    }

    fun clear() {
        map.clear()
        putsSinceSweep.set(0)
    }

    /**
     * Apply [selectEvictions] to the live map and drop what it names. O(size),
     * but only on the checkpoints [shouldSweep] decides about — that
     * amortisation is the whole point of [audit3-F-12], so the entry snapshot
     * below is cheap *because* it is rare.
     *
     * Trims to [KEEP_AFTER_SWEEP], NOT to [MAX_ENTRIES]: the ceiling is a
     * trigger, and a sweep that lands exactly on it would be due again on the
     * very next insert — putting the O(N^2) right back into the steady state.
     */
    private fun sweep(now: Long) {
        if (map.isEmpty()) return
        val metas = ArrayList<RegistryEntryMeta>(map.size)
        for ((id, e) in map) metas.add(RegistryEntryMeta(id, e.createdAt, e.ordinal))
        for (id in selectEvictions(metas, now, TTL_MS, KEEP_AFTER_SWEEP)) {
            map.remove(id)
        }
    }

    private fun nextId(): String {
        return java.lang.Long.toString(seq.incrementAndGet() and 0xFFFFFL, 36).padStart(4, '0')
    }

    companion object {
        const val TTL_MS = 60_000L

        /**
         * [audit3-F-12] Sweep at least once per this many inserts. 512 is a
         * whole screen of nodes (the complex app pages that made this hot run
         * 300-800), so the worst case is one traversal per tree instead of one
         * per node, while expiry still lands within 512 inserts of the TTL.
         */
        const val SWEEP_EVERY_PUTS = 512

        /**
         * [audit3-F-12] Ceiling on live entries. The trigger is checked before
         * the insert, so the map never holds more than this many. 4,096 is ~5
         * full dumps of an 800-node tree; past it the agent is acting on trees
         * it has not looked at in minutes anyway, and the framework node caches
         * they pin are the memory this bound releases.
         */
        const val MAX_ENTRIES = 4_096

        /**
         * [audit3-F-12] Where a sweep trims back to — one batch below
         * [MAX_ENTRIES], so hitting the ceiling buys [SWEEP_EVERY_PUTS] inserts
         * of headroom instead of re-triggering on the very next one.
         *
         * The headroom is not cosmetic. Without it the ceiling condition
         * (`size >= MAX_ENTRIES`, checked before the insert) is true for EVERY
         * insert once the registry is full, and each of those runs a full
         * O(size) sweep: on the model of this loop, 12 dumps of 800 nodes cost
         * 22,568,311 entry visits when trimming to the ceiling versus 59,384
         * with the low-water mark — the O(N^2) this fix removes, back in a
         * steadier form. The pair buys the steady-state invariant
         * `KEEP_AFTER_SWEEP <= size <= MAX_ENTRIES`.
         */
        const val KEEP_AFTER_SWEEP = MAX_ENTRIES - SWEEP_EVERY_PUTS
    }
}

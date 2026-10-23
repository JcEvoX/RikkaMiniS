package com.rikkaminis.app.config

import kotlinx.coroutines.runBlocking

/**
 * [dv24-A05] The write-through of the `session.*` config writers.
 *
 * A config write has TWO destinations and they are not interchangeable:
 *
 *  - the Room row (`sessions.thinking_override` / `sessions.model_binding`) —
 *    the only cross-restart store;
 *  - the resident view model — the only live surface. The chat view model
 *    reads both fields exactly once, inside `loadSession()`, and the view-model
 *    store keeps that instance alive for the whole process, so a write that
 *    touches only the row is invisible to the chat that is open — and the next
 *    in-app picker tap writes that stale memory back over the row.
 *
 * The order is deliberate: the row first (a write that reports success must
 * have landed), the VM second. If the VM-side entry throws, the value is
 * already persisted and the UI is one load behind — the opposite order would
 * report a failure for a write that did land.
 *
 * The VM-side entries are the SAME ones the UI uses (`setThinkingLevel`,
 * `restoreFromBinding` / `applyDefaultModelFallback`), so there is no second
 * state path to drift.
 *
 * This object is deliberately Android-free and free of the view-model type, so
 * the decision can be compiled and pinned in a plain JVM unit test
 * (`SessionConfigTargetTest`) — same shape as `ClipboardClearAction`.
 */
internal object SessionConfigWriteThrough {

    /**
     * How a `session.*` write has to be delivered.
     *
     * - [Route.ResidentViewModel] — a live VM owns the session: persist the row
     *   AND push the value into the VM.
     * - [Route.RowOnly] — no VM in memory (the chat screen was popped): the row
     *   is all there is, and it is what the next `loadSession()` will read.
     * - [Route.DraftWithoutRow] — neither: the id is a `__new__…` draft that
     *   was never materialised, so `UPDATE sessions … WHERE id = sid` matched
     *   0 rows and the value would evaporate silently. The caller has to be
     *   told, because the write *reported* success.
     */
    sealed class Route {
        object ResidentViewModel : Route()
        object RowOnly : Route()
        object DraftWithoutRow : Route()
    }

    /**
     * The pure decision: which route a write takes.
     *
     * A live VM outranks the row — when the chat is on screen the VM is the
     * surface the user reads, and it is the surface that would otherwise
     * clobber the row.
     */
    fun route(hasLiveViewModel: Boolean, hasPersistedRow: Boolean): Route = when {
        hasLiveViewModel -> Route.ResidentViewModel
        hasPersistedRow -> Route.RowOnly
        else -> Route.DraftWithoutRow
    }

    /**
     * Deliver one `session.*` write.
     *
     * @param vm the resident view model, or null when there is none.
     * @param persist the Room write (a suspend DAO call) — runs FIRST.
     * @param hasPersistedRow whether the row EXISTS for this session. It is a
     *   suspend lambda because it has to be evaluated AFTER the write: the
     *   draft case is exactly "the UPDATE matched nothing", and only the row
     *   can answer that.
     * @param refresh the resident-VM write; only called when a VM exists.
     * @param onDraftWithoutRow called when the value could not be retained
     *   anywhere durable (no row, no VM) — the caller logs it; it must not be
     *   silent, because the write *reported* success.
     */
    fun <VM> deliver(
        vm: VM?,
        persist: suspend () -> Unit,
        hasPersistedRow: suspend () -> Boolean,
        refresh: (VM) -> Unit,
        onDraftWithoutRow: () -> Unit = {},
    ): Route {
        runBlocking { persist() }
        val row = runBlocking { hasPersistedRow() }
        val r = route(hasLiveViewModel = vm != null, hasPersistedRow = row)
        when (r) {
            Route.ResidentViewModel -> refresh(vm!!)
            Route.RowOnly -> Unit
            Route.DraftWithoutRow -> onDraftWithoutRow()
        }
        return r
    }
}

/**
 * [dv25-A05] What an RPC model override (`debug.HeadlessChatRunner
 * .applyModelOverride`) has to write for a given call.
 *
 * The override parameter is per-CALL; the session binding is per-SESSION. They
 * used to be conflated: with neither `modelEntryId` nor `modelGroupId` the
 * runner unconditionally fell back to `defaultPrimaryGroupId`, i.e. a caller
 * that passed no model at all *reset* an existing session's binding to the
 * new-chat default. That is not an override, it is a silent rebind — the
 * `chat.prompt` RPC path passes both parameters as null unless the caller
 * asked for one, so every second prompt on a session pinned to a non-default
 * model moved it back.
 *
 * The default group is a NEW-SESSION default (the in-app "new chat, no model
 * picked" path), so it may only apply when the session has no binding of its
 * own yet. [Preserve] is an explicit outcome, not an absent one: the caller
 * must skip the write entirely rather than write the same value back.
 */
internal sealed class BindingTarget {
    /** Explicit `entry:<uuid>` request — always wins. */
    data class Entry(val entryId: String) : BindingTarget()

    /** Explicit `group:<id>` request, or the new-session default group. */
    data class Group(val groupId: String) : BindingTarget()

    /** No request, but the session already has a binding: leave it alone. */
    object Preserve : BindingTarget()

    /** No request, no existing binding, no default group: nothing to write. */
    object None : BindingTarget()
}

/**
 * The pure decision behind [BindingTarget].
 *
 * @param existingBinding the session's persisted `model_binding` JSON, read
 *   from the row (NOT from a caller flag): the caller cannot know whether the
 *   session was created earlier in this same RPC conversation, so the answer
 *   has to come from the only cross-restart store.
 */
internal fun resolveBindingTarget(
    requestedEntryId: String?,
    requestedGroupId: String?,
    existingBinding: String?,
    defaultGroupId: String?,
): BindingTarget {
    requestedEntryId?.takeIf { it.isNotEmpty() }?.let { return BindingTarget.Entry(it) }
    requestedGroupId?.takeIf { it.isNotEmpty() }?.let { return BindingTarget.Group(it) }
    if (!existingBinding.isNullOrBlank()) return BindingTarget.Preserve
    defaultGroupId?.takeIf { it.isNotEmpty() }?.let { return BindingTarget.Group(it) }
    return BindingTarget.None
}

/**
 * [dv25-A06] Where a `session.*` config reader takes its value from.
 *
 * A write lands on two surfaces (see [SessionConfigWriteThrough]); a read that
 * only consults the row therefore reports a different value than the one the
 * write just reported as accepted. `session.thinkingLevel` and
 * `session.primaryModel` do NOT share the same precedence, because the two
 * fields mean different things on the two surfaces — see the two functions.
 */
internal enum class SessionConfigReadSource { ResidentViewModel, PersistedRow, Absent }

/**
 * `session.thinkingLevel`: the resident level wins.
 *
 * The row is the cross-restart copy and both surfaces are equal after a
 * write-through, so the live value is the honest answer ("what the picker
 * shows"). One exception: a resident VM that has not finished adopting the row
 * yet (`sessionLoaded == false`) still holds the cross-conversation seed, which
 * says nothing about THIS session — until it loads, the row is the answer.
 * With no row at all (a draft the write landed on) the VM is the only holder,
 * so it wins even while unloaded.
 */
internal fun thinkingLevelReadSource(
    hasLiveViewModel: Boolean,
    vmAdoptedRow: Boolean,
    hasPersistedRow: Boolean,
): SessionConfigReadSource = when {
    hasLiveViewModel && (vmAdoptedRow || !hasPersistedRow) -> SessionConfigReadSource.ResidentViewModel
    hasPersistedRow -> SessionConfigReadSource.PersistedRow
    hasLiveViewModel -> SessionConfigReadSource.ResidentViewModel
    else -> SessionConfigReadSource.Absent
}

/**
 * `session.primaryModel`: the row wins whenever it exists.
 *
 * Unlike the thinking level, the two surfaces do not hold the same fact. The
 * row holds the EXPLICIT binding (`entry:<uuid>` / `group:<id>` / empty =
 * "no pin"); the VM holds the model currently IN USE, which is also non-empty
 * when the pin is empty and the model came from the loadSession fallback
 * chain. Reading the VM would therefore invent a pin the user never made (and
 * an agent's read-modify-write would persist it). The VM may only answer when
 * there is no row — i.e. a draft, where the VM is the only place the write
 * landed.
 */
internal fun modelBindingReadSource(
    hasLiveViewModel: Boolean,
    hasPersistedRow: Boolean,
): SessionConfigReadSource = when {
    hasPersistedRow -> SessionConfigReadSource.PersistedRow
    hasLiveViewModel -> SessionConfigReadSource.ResidentViewModel
    else -> SessionConfigReadSource.Absent
}

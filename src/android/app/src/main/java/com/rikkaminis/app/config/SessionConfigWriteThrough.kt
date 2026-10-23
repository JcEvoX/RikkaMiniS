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

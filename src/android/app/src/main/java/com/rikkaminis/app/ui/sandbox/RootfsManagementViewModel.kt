package com.rikkaminis.app.ui.sandbox

import android.content.Context
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.rikkaminis.app.R
import com.rikkaminis.app.logging.AppLogger
import com.rikkaminis.app.sandbox.RootfsInstallState
import com.rikkaminis.app.sandbox.RootfsManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

data class RootfsManagementUiState(
    val isInstalled: Boolean = false,
    val isProcessing: Boolean = false,
    val statusMessage: String = "",
    val resultMessage: String? = null,
    val lastOperationSuccess: Boolean = false,
    val rootfsSize: Long = 0L,
    val rootfsPath: String = "",
    /** Current install phase + 0..1 progress (null when not installing). */
    val installProgress: Float? = null,
)

/**
 * [deep-audit-1041 B-02] The failure terminal shared by the three lifecycle
 * entry points (`install` / `resetRootfs` / `repairRootfs`). Kept pure so the
 * contract is testable without an Android runtime.
 *
 * All three used to inline this same `copy(...)` inside their own `catch`,
 * which is exactly why all three could miss the step that has to follow it —
 * see [RootfsManagementViewModel.settleRootfsFailure].
 */
internal fun rootfsFailureState(
    current: RootfsManagementUiState,
    message: String,
): RootfsManagementUiState = current.copy(
    isProcessing = false,
    lastOperationSuccess = false,
    resultMessage = message,
    installProgress = null,
)

/**
 * [deep-audit-1042 B-05] Is the frame a collector has just received the
 * StateFlow's REPLAY of an already-known terminal, or a live event?
 *
 * `RootfsManager.installState` is a StateFlow, so a new collector's very first
 * frame is always the flow's current value. And the manager never leaves a
 * terminal: all eight of its write sites publish `Installed` / `Failed` and
 * none of them resets to `Idle`, so "current value" at the moment a new
 * operation starts is the PREVIOUS operation's verdict. Applying that replay
 * was two defects at once:
 *
 *  - the stale `Installed` arm cleared the `installProgress = 0f` the caller had
 *    just published, so the bar vanished for the whole apk-repair phase
 *    (Stages 1/2/2.5/2.6 publish nothing — only the Stage-3 `reset()` does);
 *  - the stale `Failed` arm set `isProcessing = false`, which is exactly the
 *    flag the screen disables Repair/Reset on (`enabled = !state.isProcessing`),
 *    so the in-flight guard inverted and a second VM migration could be
 *    started on top of the first, with the old failure text rendered over the
 *    new operation's eventual success.
 *
 * Gated on the FIRST frame and on instance identity (`===`) on purpose:
 * `RootfsManager` constructs a fresh `Failed(...)` for every verdict it
 * publishes, so a live failure is never the same instance as the stale one
 * even when the texts match, and a live re-publication of the `Installed`
 * singleton is still applied once [firstFrame] has been consumed.
 *
 * The alternative spelling — `installState.dropWhile { it === prior }` — was
 * rejected: `dropWhile` keeps dropping *leading* frames while its predicate
 * holds, so it swallows more than the replay (a legitimate re-issued terminal
 * of the same identity, and with an equality-based predicate any later `Idle`),
 * and the special case never ends. One frame, decided once, is the whole
 * contract.
 *
 * Only terminals are treated as replays: a non-terminal current value means an
 * operation really is in flight, and its progress must be mirrored.
 *
 * [deep-audit-1043 B-01] [priorTerminal] is nullable because the SECOND caller
 * has no prior value to offer: the repair settlement (see [rootfsRepairState])
 * reads the flow exactly once, so its read is a first frame too, but a caller
 * that never captured the pre-operation value must not be credited with having
 * compared against it — `null` means "nothing to compare", and the answer is
 * `false` (treat the frame as live). That is the fail-closed direction: the
 * pre-B-01 behaviour of surfacing a `Failed` is what a caller who forgot to
 * capture still gets.
 */
internal fun isStaleInstallReplay(
    firstFrame: Boolean,
    state: RootfsInstallState,
    priorTerminal: RootfsInstallState?,
): Boolean = firstFrame &&
    priorTerminal != null &&
    state === priorTerminal &&
    (state is RootfsInstallState.Installed || state is RootfsInstallState.Failed)

/**
 * [deep-audit-1042 B-05] The settlement of [RootfsManagementViewModel.repairRootfs]:
 * the verdict the manager published outranks the boolean `autoRepair()` returned.
 *
 * `autoRepair()`'s last stage is a full `reset()`, and `installIfNeeded()`
 * reports its disk-space gate by publishing `Failed(rootfs_insufficient_space)`
 * and RETURNING — it never throws. The old settlement branched on `repaired`
 * alone and wrote `rootfs_msg_repair_missing` unconditionally, so a refusal for
 * lack of storage was announced as "repair failed — still missing: …", i.e.
 * the sandbox was reported as corrupt when the real reason was free space.
 * `install` / `resetRootfs` already settle through the published terminal
 * (see [RootfsManagementViewModel.settleRootfsOperation]); repair must consult
 * the same source.
 *
 * Kept pure (like [rootfsFailureState]) so the three-way decision runs on a
 * JVM without an Android runtime. The refused arm reuses [rootfsFailureState]
 * rather than re-listing the fields — the same transition was already written
 * out twice in this file, and a third copy is exactly how the two got out of
 * step in the first place.
 *
 * [deep-audit-1043 B-01] A `Failed` the repair did not publish is not its
 * verdict. `RootfsManager.installState` never leaves a terminal (eight publish
 * sites, none resets to `Idle`), and `autoRepair()` publishes nothing at all in
 * Stages 1/2/2.5/2.6 — only the Stage-3 `reset()` does. So when a previous
 * operation left a `Failed` on the flow, "repair in place succeeded" arrived
 * here as `terminal == that stale Failed` and the unconditional first arm
 * reported the old error (e.g. a disk-space refusal from yesterday) over a
 * repair that had just restored every critical file, and reported it as
 * `lastOperationSuccess = false`.
 *
 * [priorTerminal] is the value the flow was holding when the operation started;
 * the arm now asks [isStaleInstallReplay] whether the terminal it just read is
 * that SAME INSTANCE. Instance identity, never text: `RootfsManager` builds a
 * fresh `Failed` for every verdict it publishes, so a genuine failure during
 * this repair — including one with the identical message — is a different
 * instance and still wins. A caller that passes no [priorTerminal] keeps the
 * pre-fix behaviour (surface the failure), which is the safe default.
 */
internal fun rootfsRepairState(
    current: RootfsManagementUiState,
    terminal: RootfsInstallState,
    repaired: Boolean,
    repairedMessage: String,
    stillMissingMessage: String,
    priorTerminal: RootfsInstallState? = null,
): RootfsManagementUiState = when {
    terminal is RootfsInstallState.Failed &&
        !isStaleInstallReplay(firstFrame = true, state = terminal, priorTerminal = priorTerminal) ->
        rootfsFailureState(current, terminal.error)
    repaired -> current.copy(
        isProcessing = false,
        lastOperationSuccess = true,
        resultMessage = repairedMessage,
        installProgress = null,
    )
    else -> current.copy(
        isProcessing = false,
        lastOperationSuccess = false,
        resultMessage = stillMissingMessage,
        installProgress = null,
    )
}

class RootfsManagementViewModel : ViewModel() {

    private val _uiState = MutableStateFlow(RootfsManagementUiState())
    val uiState: StateFlow<RootfsManagementUiState> = _uiState.asStateFlow()

    private var progressJob: Job? = null

    /**
     * Subscribe to the manager's installState and mirror progress + status
     * text into [_uiState]. The job is cancelled by whoever settles the
     * operation ([settleRootfsOperation] / [settleRootfsFailure] / the repair
     * terminal), and any still-running one is cancelled here before a new
     * subscription starts, so two operations can never share a collector.
     */
    private fun observeInstallProgress(manager: RootfsManager, ctx: android.content.Context) {
        progressJob?.cancel()
        progressJob = viewModelScope.launch {
            // [deep-audit-1042 B-05] The value the flow already holds when the
            // subscription starts. The first frame a StateFlow collector gets is
            // exactly this value, and `RootfsManager` never resets to `Idle`, so
            // it is the PREVIOUS operation's terminal — a replay, not an event.
            // Skipping it is what keeps the progress bar the caller just asked
            // for on screen and keeps `isProcessing` from being cleared by a
            // failure that already happened (see [isStaleInstallReplay]).
            val priorTerminal = manager.installState.value
            var firstFrame = true
            manager.installState.collect { state ->
                val replay = isStaleInstallReplay(firstFrame, state, priorTerminal)
                firstFrame = false
                if (replay) return@collect
                when (state) {
                    is RootfsInstallState.Idle -> Unit
                    is RootfsInstallState.Preparing ->
                        _uiState.value = _uiState.value.copy(
                            statusMessage = ctx.getString(R.string.rootfs_msg_preparing),
                            installProgress = 0f,
                        )
                    is RootfsInstallState.Extracting ->
                        _uiState.value = _uiState.value.copy(
                            statusMessage = ctx.getString(R.string.rootfs_msg_extracting, (state.progress * 100).toInt()),
                            installProgress = state.progress,
                        )
                    is RootfsInstallState.Finalizing ->
                        _uiState.value = _uiState.value.copy(
                            statusMessage = ctx.getString(R.string.rootfs_msg_finalizing),
                            installProgress = 1f,
                        )
                    is RootfsInstallState.Installed -> {
                        // [fix/render-ui F-277] No `progressJob?.cancel()` here.
                        // Cancelling the job that is currently running this
                        // collector from inside `collect {}` is self-cancellation:
                        // it made "who re-subscribes on the next install()"
                        // depend on how far this emission got before the
                        // cancellation landed. The cancellation belongs to the
                        // code that KNOWS the operation is over — the settlement
                        // terminals, which run outside this collector.
                        _uiState.value = _uiState.value.copy(installProgress = null)
                    }
                    is RootfsInstallState.Failed -> {
                        // [dv20-A4-B03] This branch used to share the `Installed`
                        // arm, so a failure only cleared the progress bar. The
                        // disk-space gate reports itself by publishing
                        // `Failed(rootfs_insufficient_space)` and RETURNING —
                        // it does not throw — so `install()` used to fall
                        // through to `lastOperationSuccess = true` and the
                        // screen announced "installed successfully" over an
                        // untouched rootfs. Surface the manager's own message
                        // (existing error codes pass through unchanged) and
                        // clear the success flag here, where the verdict is
                        // published rather than assumed.
                        _uiState.value = _uiState.value.copy(
                            isProcessing = false,
                            lastOperationSuccess = false,
                            resultMessage = state.error,
                            installProgress = null,
                        )
                    }
                }
            }
        }
    }

    fun refresh(ctx: Context) {
        val manager = RootfsManager.getInstance(ctx)

        _uiState.value = _uiState.value.copy(
            isInstalled = manager.isInstalled,
            rootfsPath = manager.rootfsDir.absolutePath,
            // [deep-audit-1041 B-02] The size read below only runs for an
            // INSTALLED rootfs, so the previous value used to survive a wipe: a
            // reset that had already deleted the directory (or a repair that
            // emptied it before failing) left the screen announcing "Not
            // installed" next to the old ~200 MB disk-usage figure. Zero it on
            // the way out instead — deliberately NOT by clearing it first and
            // refilling it asynchronously, which would flash 0 on every refresh
            // of a healthy rootfs.
            rootfsSize = if (manager.isInstalled) _uiState.value.rootfsSize else 0L,
        )

        if (manager.isInstalled) {
            viewModelScope.launch {
                try {
                    val size = manager.getRootfsSize()
                    _uiState.value = _uiState.value.copy(rootfsSize = size)
                } catch (_: Exception) { }
            }
        }
    }

    /**
     * [dv20-A4-B03] Read the manager's terminal state to decide whether the
     * operation actually succeeded, instead of assuming success because the
     * `suspend` call returned normally.
     *
     * `RootfsManager.installIfNeeded()` reports its disk-space gate by
     * publishing `RootfsInstallState.Failed(...)` and returning — it never
     * throws. Awaiting it therefore proves nothing about the outcome, and the
     * old unconditional `lastOperationSuccess = true` turned "refused, nothing
     * extracted" into a green "installed successfully" line. The published
     * terminal state is the verdict; [failureFallback] covers the shape where
     * the coroutine returns without a terminal emission at all (treated as
     * not-installed rather than guessed at).
     */
    private fun settleRootfsOperation(
        manager: RootfsManager,
        successMessage: String,
        failureFallback: String,
    ) {
        // [deep-audit-1042 B-05] The operation is over, so the collector that
        // mirrored its progress has nothing left to mirror. `StateFlow.collect`
        // never returns on its own — before this, the job stayed subscribed
        // until the next `observeInstallProgress()` or until the ViewModel was
        // cleared, mirroring installState changes this screen did not start.
        progressJob?.cancel()
        when (val terminal = manager.installState.value) {
            is RootfsInstallState.Installed ->
                _uiState.value = _uiState.value.copy(
                    isProcessing = false,
                    lastOperationSuccess = true,
                    resultMessage = successMessage,
                    installProgress = null,
                )
            is RootfsInstallState.Failed ->
                // The manager's own message (e.g. the existing
                // `rootfs_insufficient_space` resource) passes through.
                // [deep-audit-1042 B-05] Same transition the `catch` arms use
                // (rootfsFailureState) — it was written out twice in this file,
                // and a third copy in `repairRootfs` would have been how it
                // drifted next.
                _uiState.value = rootfsFailureState(_uiState.value, terminal.error)
            else ->
                _uiState.value = _uiState.value.copy(
                    isProcessing = false,
                    lastOperationSuccess = false,
                    resultMessage = failureFallback,
                    installProgress = null,
                )
        }
    }

    /**
     * [deep-audit-1041 B-02] The single failure terminal for the three
     * lifecycle entry points. `catch` arms used to stop at the `copy(...)`
     * below, which is only enough when the failing operation left the rootfs
     * untouched — and none of the three is that operation: `reset()` deletes
     * the directory BEFORE re-installing, and `autoRepair()` falls back to
     * exactly that reset in its last stage. So a throw mid-extract left the
     * screen rendering a rootfs that no longer exists: green "Installed", the
     * "Install" button hidden because `!isInstalled` was false, and (before the
     * size fix in [refresh]) the old disk figure beside it. Re-read the manager
     * here rather than keep trusting the pre-operation snapshot.
     *
     * [fix/render-ui F-277] The raw exception message goes to the log only —
     * it is never rendered as the user-facing result line.
     */
    private fun settleRootfsFailure(ctx: Context, tag: String, messageRes: Int, e: Exception) {
        // [deep-audit-1042 B-05] The operation that this failure terminal ends is
        // over — its progress collector must not survive it (see
        // [settleRootfsOperation]; StateFlow.collect never ends on its own).
        progressJob?.cancel()
        AppLogger.error("RootfsManagement", "$tag failed: ${e.message}")
        _uiState.value = rootfsFailureState(_uiState.value, ctx.getString(messageRes))
        refresh(ctx)
    }

    fun install(ctx: Context) {
        _uiState.value = _uiState.value.copy(
            isProcessing = true,
            statusMessage = ctx.getString(R.string.rootfs_msg_installing),
            resultMessage = null,
            installProgress = 0f,
        )

        val manager = RootfsManager.getInstance(ctx)
        observeInstallProgress(manager, ctx)
        viewModelScope.launch {
            try {
                manager.installIfNeeded()
                // [dv20-A4-B03] Derive the verdict from what the manager
                // actually published — see `rootfsOperationVerdict`.
                settleRootfsOperation(
                    manager,
                    successMessage = ctx.getString(R.string.rootfs_msg_installed),
                    failureFallback = ctx.getString(R.string.rootfs_msg_install_failed),
                )
                refresh(ctx)
            } catch (e: Exception) {
                settleRootfsFailure(ctx, "install", R.string.rootfs_msg_install_failed, e)
            }
        }
    }

    fun resetRootfs(ctx: Context) {
        _uiState.value = _uiState.value.copy(
            isProcessing = true,
            statusMessage = ctx.getString(R.string.rootfs_msg_resetting),
            resultMessage = null,
            installProgress = 0f,
        )

        val manager = RootfsManager.getInstance(ctx)
        observeInstallProgress(manager, ctx)
        viewModelScope.launch {
            try {
                manager.reset()

                // [dv20-A4-B03] `reset()` ends in `installIfNeeded()`, so the
                // same silent-failure path applies here.
                settleRootfsOperation(
                    manager,
                    successMessage = ctx.getString(R.string.rootfs_msg_reset_complete),
                    failureFallback = ctx.getString(R.string.rootfs_msg_reset_failed),
                )
                refresh(ctx)
            } catch (e: Exception) {
                settleRootfsFailure(ctx, "reset", R.string.rootfs_msg_reset_failed, e)
            }
        }
    }

    /**
     * Verify critical rootfs files and repair in place when anything is
     * missing. Less destructive than [resetRootfs]: runs `apk fix` inside the
     * guest to restore bash/readline/ncurses, only falling back to a full
     * reset when apk itself is unusable. The terminal falls back to /bin/sh
     * independently if repair still leaves bash broken.
     *
     * [deep-audit-1027 ISSUE-05] Both integrity reads are hoisted into
     * `withContext(Dispatchers.IO)`: `verifyIntegrity()` reads
     * `.integrity_manifest` and stats seven critical files, and this method is
     * reached straight from the screen's `onClick` — on Main it blocked a frame
     * per read (the second `withContext` was previously a `viewModelScope.launch`
     * whose `Main.immediate` body ran the read inline).
     */
    fun repairRootfs(ctx: Context) {
        val manager = RootfsManager.getInstance(ctx)
        viewModelScope.launch {
            val initial = withContext(Dispatchers.IO) {
                manager.verifyIntegrity()
            }
            if (initial.healthy) {
                _uiState.value = _uiState.value.copy(
                    lastOperationSuccess = true,
                    resultMessage = ctx.getString(R.string.rootfs_msg_healthy),
                )
                return@launch
            }

            _uiState.value = _uiState.value.copy(
                isProcessing = true,
                statusMessage = ctx.getString(R.string.rootfs_msg_repairing),
                resultMessage = null,
                installProgress = 0f,
            )
            // [deep-audit-1041 B-02] Stage 3 of `autoRepair()` is a full
            // `reset()`, i.e. the same re-extraction `install`/`resetRootfs`
            // subscribe to. Without the subscription the determinate progress
            // bar this method just asked for sat frozen at 0% for the whole
            // (minutes-long) wipe + extract, because nobody was collecting
            // `manager.installState`.
            // [deep-audit-1043 B-01] The terminal the flow was ALREADY holding
            // when this repair started. `RootfsManager` never leaves a terminal
            // and `autoRepair()` publishes nothing before Stage 3, so without
            // this snapshot the settlement below cannot tell "yesterday's
            // operation failed" from "this repair failed" — and the stale
            // `Failed` would outrank the health this repair just restored.
            // Captured before the subscription (which reads the same value for
            // its own, unrelated job: gating frames, not adjudicating a
            // verdict) and before `autoRepair()`, i.e. before anything this
            // operation does can publish.
            val priorTerminal = manager.installState.value
            observeInstallProgress(manager, ctx)
            try {
                val repaired = manager.autoRepair()
                val after = withContext(Dispatchers.IO) {
                    manager.verifyIntegrity()
                }
                // [deep-audit-1042 B-05] Settle through the manager's PUBLISHED
                // verdict, the same source `install` / `resetRootfs` read in
                // [settleRootfsOperation]: `autoRepair()`'s Stage 3 is a full
                // `reset()`, and `installIfNeeded()` reports its disk-space gate
                // by publishing `Failed(rootfs_insufficient_space)` and returning
                // normally — so `repaired == false` alone cannot tell "still
                // corrupt" from "refused". The old branch wrote the
                // `rootfs_msg_repair_missing` text unconditionally, which
                // announced a storage refusal as rootfs corruption.
                progressJob?.cancel()
                _uiState.value = rootfsRepairState(
                    _uiState.value,
                    manager.installState.value,
                    repaired,
                    ctx.getString(R.string.rootfs_msg_repaired),
                    ctx.getString(R.string.rootfs_msg_repair_missing, after.missing.joinToString(", ")),
                    // [deep-audit-1043 B-01] Hand the pre-operation terminal over so
                    // the helper can tell an inherited `Failed` from one this
                    // repair published.
                    priorTerminal = priorTerminal,
                )
                refresh(ctx)
            } catch (e: Exception) {
                settleRootfsFailure(ctx, "repair", R.string.rootfs_msg_repair_failed, e)
            }
        }
    }
}

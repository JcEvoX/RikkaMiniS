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

class RootfsManagementViewModel : ViewModel() {

    private val _uiState = MutableStateFlow(RootfsManagementUiState())
    val uiState: StateFlow<RootfsManagementUiState> = _uiState.asStateFlow()

    private var progressJob: Job? = null

    /**
     * Subscribe to the manager's installState and mirror progress + status
     * text into [_uiState]. Cancelled on completion so we don't leak a job
     * across multiple install() calls.
     */
    private fun observeInstallProgress(manager: RootfsManager, ctx: android.content.Context) {
        progressJob?.cancel()
        progressJob = viewModelScope.launch {
            manager.installState.collect { state ->
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
                        // cancellation landed. The terminal state is reached
                        // exactly once per install, so the job simply ends on
                        // its own — and `observeInstallProgress` cancels any
                        // previous job before starting a new one.
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
        when (val terminal = manager.installState.value) {
            is RootfsInstallState.Installed ->
                _uiState.value = _uiState.value.copy(
                    isProcessing = false,
                    lastOperationSuccess = true,
                    resultMessage = successMessage,
                    installProgress = null,
                )
            is RootfsInstallState.Failed ->
                _uiState.value = _uiState.value.copy(
                    isProcessing = false,
                    lastOperationSuccess = false,
                    // The manager's own message (e.g. the existing
                    // `rootfs_insufficient_space` resource) passes through.
                    resultMessage = terminal.error,
                    installProgress = null,
                )
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
            observeInstallProgress(manager, ctx)
            try {
                val repaired = manager.autoRepair()
                val after = withContext(Dispatchers.IO) {
                    manager.verifyIntegrity()
                }
                _uiState.value = _uiState.value.copy(
                    isProcessing = false,
                    lastOperationSuccess = repaired,
                    resultMessage = if (repaired) {
                        ctx.getString(R.string.rootfs_msg_repaired)
                    } else {
                        ctx.getString(R.string.rootfs_msg_repair_missing, after.missing.joinToString(", "))
                    },
                    installProgress = null,
                )
                refresh(ctx)
            } catch (e: Exception) {
                settleRootfsFailure(ctx, "repair", R.string.rootfs_msg_repair_failed, e)
            }
        }
    }
}

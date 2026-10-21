package com.rikkaminis.app.browser

import android.content.ComponentCallbacks2
import android.content.Context
import android.content.res.Configuration
import android.os.Message
import android.util.Log
import android.webkit.WebView
import com.rikkaminis.app.service.TrimPolicy
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import com.rikkaminis.app.R
import org.json.JSONObject
import java.io.File
import java.util.Date
import java.util.concurrent.ConcurrentHashMap

/**
 * Manages up to 3 browser tabs for the agent, mirroring iOS BrowserTabPool.
 * All tabs share the same cookie store by default on Android.
 */

/** [fix/audit-s4h1] Raised when the pool is at MAX_TABS and no tab freed up within the bounded wait. */
class BrowserTabPoolExhaustedException(message: String) : Exception(message)

class BrowserTabPool(private val context: Context) : ComponentCallbacks2 {

    companion object {
        private const val TAG = "BrowserTabPool"
        /** [T7-L5] Tab cap. UI surfaces must read this instead of a literal 3. */
        const val MAX_TABS = 3
        private const val IDLE_CHECK_INTERVAL_MS = 60_000L  // 60 seconds
        /** Default idle timeout — matches iOS BrowserTabPool.idleTimeout (15 minutes). */
        const val DEFAULT_IDLE_TIMEOUT_MINUTES = 15
        /** SharedPreferences key for the user-configurable idle timeout. */
        const val PREF_IDLE_TIMEOUT_MINUTES = "idle_timeout_minutes"
        /** Minimum permitted idle timeout (minutes). Protects against runaway eviction. */
        const val MIN_IDLE_TIMEOUT_MINUTES = 1
        /** Maximum permitted idle timeout (minutes). */
        const val MAX_IDLE_TIMEOUT_MINUTES = 240

        /** Global custom viewport width SharedPreferences key (0 = use UA default). */
        const val PREF_GLOBAL_VIEWPORT_WIDTH = "browser_custom_viewport_width"
        /** Global custom viewport height SharedPreferences key (0 = use UA default). */
        const val PREF_GLOBAL_VIEWPORT_HEIGHT = "browser_custom_viewport_height"

        /**
         * [audit-RC11] Sanitize a download filename before it reaches the
         * filesystem. Returns null when the name is unusable (empty after
         * trim, > 200 chars, or it smuggles any path component) so callers can
         * refuse or fall back to a safe default. Defense-in-depth companion to
         * PRootKernel path normalization: a download name is itself untrusted
         * input from the page and must never be able to escape the workspace
         * directory (e.g. "../../shell" or "/etc/passwd").
         */
        internal fun sanitizeDownloadName(name: String): String? {
            val n = name.trim()
            if (n.isEmpty() || n.length > 200) return null
            // Path separators (POSIX & Windows), and the NUL delimiter, would
            // turn a "filename" into a path that escapes the download dir.
            if (n.any { it == '/' || it == '\\' || it == '\u0000' }) return null
            // Belt-and-braces: any resolve that sees a parent component means a
            // path, not a bare name.
            if (File(n).parent != null) return null
            return n
        }

        /**
         * [T-browser-use-per-tab-serial-android] Max time a browser_use call
         * waits to acquire the per-tab-id serial lock before giving up. This is
         * ONLY the lock-acquisition wait (waiting for another tool's operation
         * on the SAME explicit tab id to finish) — it is NOT the browser task
         * timeout. Once the lock is held the actual page operation keeps its
         * existing per-action timeout (BrowserUseManager), untouched.
         */
        private const val TAB_SERIAL_WAIT_TIMEOUT_MS = 60_000L

        /**
         * [T-android-browser-download-ux] Files-app-style middle truncation
         * (iOS v3 String.middleTruncated): keeps the head and the tail so the
         * extension stays visible. Used for the human-facing chat notices;
         * panel rows truncate adaptively in Compose.
         */
        fun middleTruncated(name: String, head: Int = 22, tail: Int = 10): String {
            if (name.length <= head + tail + 1) return name
            return name.take(head) + "…" + name.takeLast(tail)
        }

        /**
         * [fix/sweep1014-b17a] Pure decision core for restoring persisted tab
         * state. Lives in the companion so it can be extracted verbatim for
         * JVM testing (the file itself has Android imports and cannot compile
         * under kotlinc alone).
         *
         * - [TabRestorePlan.nextTabId] resumes above the max persisted id AND
         *   the max live tab id — starting at 0 after a restart made the first
         *   new tab collide with saved ids (a blank tab consumed
         *   savedURLs[0], or hit nothing when tab 0 was closed last run, and
         *   the rest were only claimed in creation order, if ever).
         * - Saved ids already owned by a live tab are dropped (the live tab
         *   wins) — covers a pool re-pointed to a session whose save file
         *   overlaps the live tab ids.
         * - At most (maxTabs - live) tabs are restored per pass; remaining
         *   saved entries stay in savedURLs for a later run.
         * - selectedTabId falls back to the first existing/restored tab when
         *   it points at an id that was closed before the last save.
         */
        internal data class TabRestorePlan(
            val nextTabId: Int,
            val restoreIds: List<Int>,
            val droppedIds: List<Int>,
            val selectedTabId: Int,
        )

        internal fun planTabRestore(
            savedIds: List<Int>,
            liveIds: List<Int>,
            selectedTabId: Int,
            maxTabs: Int = MAX_TABS,
        ): TabRestorePlan {
            val next = maxOf(savedIds.maxOrNull() ?: -1, liveIds.maxOrNull() ?: -1) + 1
            val slots = (maxTabs - liveIds.size).coerceAtLeast(0)
            val restore = mutableListOf<Int>()
            val dropped = mutableListOf<Int>()
            for (id in savedIds) {
                if (id in liveIds) {
                    dropped.add(id)
                } else if (restore.size < slots) {
                    restore.add(id)
                }
                // else: over the tab cap — leave the entry in savedURLs for a later run
            }
            val all = liveIds + restore
            val selected = if (all.contains(selectedTabId)) selectedTabId else (all.firstOrNull() ?: selectedTabId)
            return TabRestorePlan(next, restore, dropped, selected)
        }

        /**
         * [T-browser-implicit-tab-inuse-until-load-android] Grace window a tab
         * stays `inUse` after an implicit-tab (tab_id-less) action completes.
         * `navigate` already suspends until onPageFinished / its 30s timeout, so
         * inUse is held through the page load itself; this grace then keeps it
         * held a further 15s, re-armed on every subsequent action to that tab.
         * During the grace window acquireTab(null) sees inUse=true and fans out
         * to a fresh tab (createTab) instead of trampling the busy one — so N
         * back-to-back tab-less navigates open N independent tabs rather than
         * all overwriting tab 0. A same-task follow-up chain re-arms the timer
         * each call, so the tab survives between consecutive operations.
         */
        private const val IMPLICIT_TAB_GRACE_MS = 15_000L

        /**
         * [T-browser-implicit-tab-inuse-until-load-android] When all tabs are
         * busy and the pool is at MAX_TABS, an implicit-tab acquire waits up to
         * this long (polling) for a tab to free instead of trampling a busy one.
         * Bounded so a stuck tab can't hang the agent forever — after the window
         * it falls back to the least-recently-active tab.
         */
        private const val IMPLICIT_TAB_WAIT_MS = 20_000L
        private const val IMPLICIT_TAB_WAIT_POLL_MS = 250L

        /**
         * [T-android-trim-memory] Threshold note: [ComponentCallbacks2.onTrimMemory]
         * tiers (MODERATE=5, LOW=10, CRITICAL=15, UI_HIDDEN=20, BACKGROUND=40+).
         * We treat any level >= RUNNING aggressive enough to shed idle tabs;
         * the selected tab is always the last to go because the agent's JS
         * evaluation state lives in it (window.innerWidth etc. after
         * set_viewport / reloadAndWait).
         */
        private const val TRIM_MAX_IDLE_TABS_TO_KEEP = 1
    }

    /**
     * [T-browser-use-per-tab-serial-android] Per-tab-id serial locks. Parallel
     * tool execution can fire two browser_use calls at the same explicit tab id;
     * letting both drive the one WebView corrupts state, so calls targeting the
     * SAME existing tab id run one-at-a-time through that tab's Mutex. Different
     * tab ids keep their own Mutex and still run concurrently. Calls with NO
     * tab_id are deliberately NOT funneled through a shared lock — they fan out
     * to separate (free / freshly-created) tabs in [acquireTab] so two
     * tab-less navigates run in parallel instead of deadlocking on one tab.
     */
    private val tabLocks = ConcurrentHashMap<Int, Mutex>()

    private fun lockForTab(id: Int): Mutex = tabLocks.getOrPut(id) { Mutex() }

    data class Tab(
        val id: Int,
        val manager: BrowserUseManager,
        var inUse: Boolean = false,
        var lastActivityDate: Date = Date(),
        /**
         * Flag set by `createTab` when a tab is created with no initial URL.
         * `acquireTab` / `newTab` consume it and call `manager.loadBlankPage()`
         * from their suspend contexts so `window.innerWidth/Height` reflects
         * the session viewport instead of the `about:blank` 980px fallback.
         */
        var needsInitialBlankPage: Boolean = false,
        /**
         * [T-browser-implicit-tab-inuse-until-load-android] Pending job that
         * clears [inUse] after the post-action grace window. Cancelled and
         * re-armed by [armImplicitGraceRelease] on every implicit-tab action so
         * a same-task follow-up keeps the tab held; fires once the tab has been
         * idle for [IMPLICIT_TAB_GRACE_MS]. Excluded from equals/hashCode/copy
         * since it's transient scheduling state, not tab identity.
         */
        var inUseGraceJob: Job? = null,
    )

    private val _tabs = MutableStateFlow<List<Tab>>(emptyList())
    val tabs: StateFlow<List<Tab>> = _tabs.asStateFlow()

    private val _selectedTabId = MutableStateFlow(0)
    val selectedTabId: StateFlow<Int> = _selectedTabId.asStateFlow()

    private val _agentBusy = MutableStateFlow(false)

    /**
     * [da1040-B2] Reactive "some tab is executing an agent action" signal.
     *
     * `tabs` cannot carry this: a busy flip mutates [Tab.inUse] IN PLACE
     * (audit-b21 — replacing the instance with a `copy()` breaks the identity
     * link to in-flight actions, whose release paths then mutate the object the
     * pool no longer holds), so the list handed to the StateFlow stays
     * content-equal to the previous one and `AbstractList.equals` makes the
     * StateFlow swallow the emission. A UI reading a plain getter therefore
     * composed once and never recomposed when the agent started or stopped
     * working — the busy affordances stayed frozen for the whole session.
     * This flow is re-derived (never guessed) by [publishAgentBusy].
     */
    val agentBusy: StateFlow<Boolean> = _agentBusy.asStateFlow()

    /**
     * [da1040-B2] Single carrier of the busy predicate. Re-derives from the
     * live tab list, so every writer stays a statement of fact ("this tab is
     * in use") and the flag cannot drift from [Tab.inUse].
     */
    private fun publishAgentBusy() {
        _agentBusy.value = _tabs.value.any { it.inUse }
    }

    /**
     * [da1040-B2] The ONLY writer of [Tab.inUse]. Keeping the write behind one
     * funnel is what makes "busy flips are observable" a property of the class
     * instead of a rule every future call site has to remember — the old shape
     * (`tab.inUse = …` at five sites, plus a list refill that StateFlow
     * suppresses) is how the signal got lost in the first place.
     */
    private fun setTabInUse(tab: Tab, inUse: Boolean) {
        tab.inUse = inUse
        publishAgentBusy()
    }

    /** Currently selected tab's manager, or the first tab's if none selected — mirrors iOS activeManager. */
    val activeManager: BrowserUseManager?
        get() {
            val tabs = _tabs.value
            if (tabs.isEmpty()) return null
            return tabs.firstOrNull { it.id == _selectedTabId.value }?.manager ?: tabs.first().manager
        }

    private val _userAgentProfile = MutableStateFlow(UserAgentProfile.MOBILE_CHROME)
    val currentUserAgentProfile: StateFlow<UserAgentProfile> = _userAgentProfile.asStateFlow()
    private var userAgentProfile: UserAgentProfile
        get() = _userAgentProfile.value
        set(value) { _userAgentProfile.value = value }
    private var customUserAgentString: String? = null

    private var sessionId: String? = null
    private val savedURLs = mutableMapOf<Int, String>()

    /**
     * Global custom viewport. `0` means "use the UA profile default".
     * Persisted across launches via SharedPreferences. Session-level overrides
     * ([sessionViewportWidth]/[sessionViewportHeight]) shadow this.
     * Mirrors iOS `BrowserTabPool.customViewportWidth/Height`.
     */
    private val _customViewportWidth = MutableStateFlow(0)
    val customViewportWidth: StateFlow<Int> = _customViewportWidth.asStateFlow()
    private val _customViewportHeight = MutableStateFlow(0)
    val customViewportHeight: StateFlow<Int> = _customViewportHeight.asStateFlow()

    /**
     * Per-session viewport override set via `set_viewport`. Both > 0 means
     * active; shadows global custom and UA profile default. Persisted in the
     * session's tab JSON file, not SharedPreferences — matches iOS
     * `BrowserTabPool.sessionViewportWidth/Height`.
     */
    private val _sessionViewportWidth = MutableStateFlow(0)
    val sessionViewportWidth: StateFlow<Int> = _sessionViewportWidth.asStateFlow()
    private val _sessionViewportHeight = MutableStateFlow(0)
    val sessionViewportHeight: StateFlow<Int> = _sessionViewportHeight.asStateFlow()

    // [fix/sweep1014-b17a] Written by loadSavedState (which may run off-main
    // via lazy pool access from tool execution); read on Main by createTab —
    // @Volatile for cross-thread visibility.
    @Volatile
    private var nextTabId = 0

    private val evictionScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var evictionJob: Job? = null

    /**
     * Current idle-eviction timeout in milliseconds. Read from SharedPreferences
     * in `init`, updatable at runtime via [setIdleTimeoutMinutes] so settings
     * changes take effect without restarting the pool. Mirrors iOS
     * `BrowserTabPool.idleTimeout` (hardcoded 15 min on iOS; made configurable here).
     */
    @Volatile
    private var idleTimeoutMs: Long = DEFAULT_IDLE_TIMEOUT_MINUTES * 60_000L

    /**
     * [T-browser-risk-control] Timestamp (System.currentTimeMillis) of the last
     * throttled browser action. Used by [computeThrottleDelayMs] to calculate the
     * deficit between consecutive actions. -1L means "no previous action tracked".
     */
    @Volatile
    private var lastActionTimeMs: Long = -1L

    init {
        // Restore persisted User-Agent profile from SharedPreferences so pool-owned
        // WebViews start in the correct mode (Mobile vs Desktop) and
        // captureWebViewBitmap() uses the matching viewport.
        val prefs = context.getSharedPreferences("browser_prefs", Context.MODE_PRIVATE)
        val profileName = prefs.getString("user_agent_profile", null)
        if (profileName != null) {
            runCatching { UserAgentProfile.valueOf(profileName) }.getOrNull()?.let {
                _userAgentProfile.value = it
            }
        }
        customUserAgentString = prefs.getString("custom_user_agent", null)?.ifEmpty { null }

        // Restore global custom viewport (0 = unset → fall back to UA profile).
        // Mirrors iOS `BrowserCustomViewport{Width,Height}` UserDefaults keys.
        _customViewportWidth.value = prefs.getInt(PREF_GLOBAL_VIEWPORT_WIDTH, 0).coerceAtLeast(0)
        _customViewportHeight.value = prefs.getInt(PREF_GLOBAL_VIEWPORT_HEIGHT, 0).coerceAtLeast(0)

        // Load user-configured idle timeout (default 15 min, matches iOS).
        val storedMinutes = prefs.getInt(PREF_IDLE_TIMEOUT_MINUTES, DEFAULT_IDLE_TIMEOUT_MINUTES)
        idleTimeoutMs = storedMinutes.coerceIn(MIN_IDLE_TIMEOUT_MINUTES, MAX_IDLE_TIMEOUT_MINUTES) * 60_000L

        // Start idle tab eviction timer (60-second interval, matching iOS)
        evictionJob = evictionScope.launch {
            while (isActive) {
                delay(IDLE_CHECK_INTERVAL_MS)
                withContext(Dispatchers.Main) { evictIdleTabs() }
            }
        }

        // [T-android-trim-memory] Register for system low-memory callbacks.
        // WebViews are expensive (50-100 MB per tab). When the OS signals
        // pressure we release idle tabs before the process is killed.
        // Unregistered in [dispose].
        context.applicationContext.registerComponentCallbacks(this)
    }

    /**
     * Current idle timeout in minutes. Exposed for Settings UIs that want to
     * read the active value without parsing SharedPreferences themselves.
     */
    val idleTimeoutMinutes: Int
        get() = (idleTimeoutMs / 60_000L).toInt()

    /**
     * Update the idle-eviction timeout. Persists to SharedPreferences and
     * takes effect on the next eviction tick (within [IDLE_CHECK_INTERVAL_MS]).
     * Values outside [MIN_IDLE_TIMEOUT_MINUTES]..[MAX_IDLE_TIMEOUT_MINUTES] are clamped.
     */
    fun setIdleTimeoutMinutes(minutes: Int) {
        val clamped = minutes.coerceIn(MIN_IDLE_TIMEOUT_MINUTES, MAX_IDLE_TIMEOUT_MINUTES)
        idleTimeoutMs = clamped * 60_000L
        val prefs = context.getSharedPreferences("browser_prefs", Context.MODE_PRIVATE)
        prefs.edit().putInt(PREF_IDLE_TIMEOUT_MINUTES, clamped).apply()
        Log.i(TAG, "Idle timeout set to $clamped min")
    }

    // -- Session --

    fun setSession(sessionId: String) {
        // [fix/dv17-1 B-06] Capture the OLD id before it is overwritten: a pool
        // bound to a draft session (`__new__<uuid>`) carries download records
        // whose destination is frozen under the draft workspace. When the pool
        // is re-pointed at the promoted session the draft tree is already gone
        // (migrateDraftResources moved the bytes and deleted the directory), so
        // those records' "open" buttons died at their file.exists() guard with
        // zero feedback. Re-point the paths through the atomic update — a
        // non-draft rebind (previous == null, or same id) is a byte-identical
        // no-op, exactly the behavior before this change.
        val previous = this.sessionId
        this.sessionId = sessionId
        if (previous != null && previous != sessionId) {
            remapDownloadsToSession(oldSessionId = previous, newSessionId = sessionId)
        }
        // [audit-RC13] Pin every live tab's WebView to this chat session so
        // minis:// subresource interception resolves files through
        // resolveSessionHostPath (session-scoped) instead of the global
        // cross-session fallback (BrowserUseManager previously had no
        // session context at all — file_upload already had it via
        // BrowserActionInput.sessionId). loadSavedState() only restores
        // saved URLs, never creates managers, so this stamp covers every
        // live tab; tabs created later inherit via createTab/handleNewWindow.
        _tabs.value.forEach { it.manager.minisSessionId = sessionId }
        loadSavedState()
    }

    /**
     * [deep-audit-1016 9c] Re-point this pool at [sessionId] *without* loading
     * that session's persisted tab state.
     *
     * The shared shell pool (`MinisApp.sharedBrowserTabPool`) is built once at
     * app start and its session is pinned there to the placeholder
     * "minis-browser-use", so every `minis-browser-use` call used to resolve
     * minis:// through the placeholder (`PRootKernel.resolvePathForSession`)
     * and download into `minis-sessions/minis-browser-use/workspace` — the
     * caller's own files were invisible to the session that asked for them.
     * Live A/B measured before this fix: `minis-browser-use navigate --url
     * minis://workspace/probe.html` answered `File not found:
     * workspace/probe.html`, while the built-in browser_use tool (a
     * per-ChatViewModel pool, bound through [setSession]) loaded the identical
     * URL and reported its title.
     *
     * Deliberately NOT [setSession]: that one also runs [loadSavedState], whose
     * job is to materialise the *new* session's `browser_tabs/<sid>.json` — it
     * schedules [restoreSavedTabs] and re-applies that session's viewport
     * override. Both are right at a real session boundary (a pool belongs to
     * one session for its lifetime) and wrong here: the CLI driver is a
     * long-lived cross-session pool, so loading state on every call would drag
     * the chat session's saved tabs into the driver's own tab list.
     *
     * Concurrency: the pool stays one shared instance, so a binding is
     * last-writer-wins — two chat sessions running `minis-browser-use`
     * simultaneously share one tab list and one binding, exactly as they
     * already shared one tab list. Per-session pools would remove that
     * coupling but change the pool's identity and lifetime (out of scope).
     */
    fun bindSessionContextOnly(sessionId: String) {
        val decision = BrowserSessionBinding.decideRebind(this.sessionId, sessionId)
        if (!decision.rebind) return
        this.sessionId = sessionId
        _tabs.value.forEach { it.manager.minisSessionId = sessionId }
        if (decision.resetViewport) {
            _sessionViewportWidth.value = 0
            _sessionViewportHeight.value = 0
        }
        Log.i(TAG, "Bound shared browser pool to session ${sessionId.take(12)}")
    }

    // -- Downloads --

    /** UI state for the browser sheet's download banner. progress < 0 = indeterminate. */
    data class DownloadUiState(val filename: String, val progress: Float)

    private val _activeDownload = MutableStateFlow<DownloadUiState?>(null)
    val activeDownload: StateFlow<DownloadUiState?> = _activeDownload.asStateFlow()

    /**
     * [T-android-browser-download-ux] Session-scoped download registry — the
     * Android port of iOS BrowserDownloadCenter (T-browser-download-ux v1-v3).
     * Replaces the single-slot banner state as the source of truth: every
     * download becomes an entry with live progress, a terminal state, a
     * cancel handle, and a `seen` flag driving the toolbar badge. The pool is
     * 1:1 with a chat session, so the registry lives here instead of a global
     * center keyed by a mutable session id.
     */
    enum class DownloadState { DOWNLOADING, COMPLETED, FAILED }

    data class DownloadEntry(
        val id: Long,
        val filename: String,
        val destination: File?,
        val bytesDone: Long,
        /** <= 0 means unknown (indeterminate). */
        val totalBytes: Long,
        val state: DownloadState,
        val failureReason: String? = null,
        val startedAt: Long,
        /** Terminal entries only: false until the user opens the panel. */
        val seen: Boolean = false,
    )

    /** Newest-first. */
    private val _downloads = MutableStateFlow<List<DownloadEntry>>(emptyList())
    val downloads: StateFlow<List<DownloadEntry>> = _downloads.asStateFlow()

    private val downloadJobs = java.util.concurrent.ConcurrentHashMap<Long, Job>()
    private val nextDownloadId = java.util.concurrent.atomic.AtomicLong(1)

    /**
     * Posts a user-visible download notice into the owning chat. Wired by
     * ChatViewModel to appendSystemInfo; may be invoked from any thread.
     */
    var onDownloadEvent: ((String) -> Unit)? = null

    private val downloadScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private fun registerDownload(dest: File, totalBytes: Long): Long {
        val id = nextDownloadId.getAndIncrement()
        val entry = DownloadEntry(
            id = id,
            filename = dest.name,
            destination = dest,
            bytesDone = 0L,
            totalBytes = totalBytes,
            state = DownloadState.DOWNLOADING,
            startedAt = System.currentTimeMillis(),
        )
        // [deep-audit-1016c ISSUE-03C] read-modify-write on a StateFlow races
        // across writer threads (URL-download listener on Main, blob bridge on
        // the JavaBridge thread, download coroutines on IO) — a lost update
        // here makes a concurrently-registered download vanish from the panel
        // or a progress/settle write regress. `update {}` is the atomic CAS.
        _downloads.update { listOf(entry) + it }
        _activeDownload.value = DownloadUiState(dest.name, if (totalBytes > 0) 0f else -1f)
        return id
    }

    private fun updateDownloadEntry(id: Long, transform: (DownloadEntry) -> DownloadEntry) {
        _downloads.update { current -> current.map { if (it.id == id) transform(it) else it } }
    }

    private fun updateDownloadProgress(id: Long, name: String, copied: Long, total: Long) {
        updateDownloadEntry(id) { it.copy(bytesDone = copied, totalBytes = total) }
        _activeDownload.value = DownloadUiState(
            name,
            if (total > 0) (copied.toFloat() / total).coerceIn(0f, 1f) else -1f,
        )
    }

    private fun settleDownload(id: Long, state: DownloadState, reason: String? = null, bytes: Long? = null) {
        downloadJobs.remove(id)
        updateDownloadEntry(id) {
            it.copy(
                state = state,
                failureReason = reason,
                bytesDone = bytes ?: it.bytesDone,
            )
        }
        // [fix/sweep1014-b17b] Only drop the banner when nothing else is still
        // in flight — with concurrent downloads, settling one must not hide a
        // still-active one (there is no PAUSED state in this enum, so
        // DOWNLOADING is the only in-flight state). Otherwise switch the
        // banner to the remaining active download until its own progress
        // updates take over.
        _activeDownload.value = _downloads.value
            .firstOrNull { it.state == DownloadState.DOWNLOADING }
            ?.let { entry ->
                DownloadUiState(
                    entry.filename,
                    if (entry.totalBytes > 0) (entry.bytesDone.toFloat() / entry.totalBytes).coerceIn(0f, 1f) else -1f,
                )
            }
    }

    /** Cancel an in-flight download; the coroutine's CancellationException
     *  path deletes the partial file and settles the entry as cancelled. */
    fun cancelDownload(id: Long) {
        downloadJobs[id]?.cancel()
    }

    /** In-flight + unviewed terminal entries — the toolbar badge number
     *  (mirrors iOS BrowserDownloadCenter.badgeCount). */
    fun downloadBadgeCount(entries: List<DownloadEntry>): Int =
        entries.count { it.state == DownloadState.DOWNLOADING || !it.seen }

    /**
     * [fix/dv17-1 B-06] Re-point every download record that lived under
     * [oldSessionId]'s workspace onto [newSessionId]'s workspace.
     *
     * Called only from [setSession] on a real id change. Both directories are
     * built from the same shape as [sessionWorkspaceDirOf] — deliberately
     * WITHOUT mkdirs: the old directory is usually already deleted (recreating
     * it would resurrect the draft tree), and the new one is created by
     * migrateDraftResources / the next download.
     *
     * ponytail: 只做「路径前缀重指」，不迁字节、不校验新路径是否存在 | 天花板:
     * 若迁移走的是跨设备 copy 回退（而非 rename），一个正处于 DOWNLOADING 的
     * 条目其已打开的流仍写在旧 inode 上，重指后的路径会指向别处 | 升级触发:
     * 真机上出现「晋升后有下载记录但打开仍失败」（rename 语义失效被实测到）。
     */
    private fun remapDownloadsToSession(oldSessionId: String, newSessionId: String) {
        val oldDir = sessionWorkspaceDirOf(oldSessionId)
        val newDir = sessionWorkspaceDirOf(newSessionId)
        _downloads.update { current ->
            remapDownloadDestinations(
                entries = current,
                oldDir = oldDir,
                newDir = newDir,
                pathOf = { entry -> entry.destination },
                withPath = { entry, path -> entry.copy(destination = path) },
            )
        }
    }

    /** Panel opened — terminal entries stop counting toward the badge. */
    fun markDownloadsSeen() {
        _downloads.update { current ->
            current.map { if (it.state != DownloadState.DOWNLOADING) it.copy(seen = true) else it }
        }
    }

    /** "Clear" in the panel: drops ALL finished records (completed AND failed,
     *  iOS v3 semantics) — in-flight rows untouched. */
    fun clearFinishedDownloads() {
        _downloads.update { current -> current.filter { it.state == DownloadState.DOWNLOADING } }
    }


    /** The session's /var/minis/workspace/ host directory — downloads land here
     *  so the agent can read and operate on them in follow-up turns. */
    private fun sessionWorkspaceDir(): File? {
        val sid = sessionId ?: return null
        return sessionWorkspaceDirOf(sid).apply { mkdirs() }
    }

    /** [fix/dv17-1 B-06] Single source of the workspace path shape, shared by
     *  the live lookup above and the id-keyed remap in [remapDownloadsToSession]
     *  (which must NOT mkdirs: it names the *old*, usually deleted directory).
     *  Session does not require the file. */
    private fun sessionWorkspaceDirOf(sessionId: String): File =
        File(File(File(context.filesDir, "minis-sessions"), sessionId), "workspace")

    /** name.ext → name-1.ext → name-2.ext … until unused. */
    private fun uniqueFile(dir: File, name: String): File {
        var f = File(dir, name)
        if (!f.exists()) return f
        val base = name.substringBeforeLast('.', name)
        val ext = name.substringAfterLast('.', "")
        var i = 1
        while (f.exists()) {
            f = File(dir, if (ext.isEmpty()) "$base-$i" else "$base-$i.$ext")
            i++
        }
        return f
    }

    /** Stream an http/https download into the session workspace with progress. */
    internal fun startUrlDownload(
        url: String,
        userAgent: String?,
        contentDisposition: String?,
        mimeType: String?,
        contentLength: Long,
    ) {
        val dir = sessionWorkspaceDir() ?: run {
            Log.w(TAG, "download rejected: no session bound to pool")
            return
        }
        val name = sanitizeDownloadName(
            android.webkit.URLUtil.guessFileName(url, contentDisposition, mimeType)
        ) ?: "download-${System.currentTimeMillis()}"
        val dest = uniqueFile(dir, name)
        onDownloadEvent?.invoke("Downloading ${middleTruncated(dest.name)}…")
        val id = registerDownload(dest, contentLength)
        val job = downloadScope.launch {
            try {
                val conn = java.net.URL(url).openConnection() as java.net.HttpURLConnection
                conn.connectTimeout = 15_000
                conn.readTimeout = 30_000
                conn.instanceFollowRedirects = true
                userAgent?.takeIf { it.isNotEmpty() }?.let { conn.setRequestProperty("User-Agent", it) }
                // Reuse the WebView's cookies so authenticated downloads work.
                android.webkit.CookieManager.getInstance().getCookie(url)?.let {
                    conn.setRequestProperty("Cookie", it)
                }
                val total = if (contentLength > 0) contentLength else conn.contentLengthLong
                conn.inputStream.use { input ->
                    dest.outputStream().use { out ->
                        val buf = ByteArray(64 * 1024)
                        var copied = 0L
                        while (true) {
                            // Makes cancelDownload() actually stop mid-stream
                            // instead of copying to completion.
                            ensureActive()
                            val n = input.read(buf)
                            if (n < 0) break
                            out.write(buf, 0, n)
                            copied += n
                            updateDownloadProgress(id, dest.name, copied, total)
                        }
                    }
                }
                conn.disconnect()
                finishDownload(id, dest)
            } catch (e: kotlinx.coroutines.CancellationException) {
                // User cancel is not a failure notice-worthy event: delete the
                // partial file, settle as cancelled, keep the chat quiet.
                Log.i(TAG, "download cancelled: ${dest.name}")
                runCatching { dest.delete() }
                settleDownload(id, DownloadState.FAILED, reason = "Cancelled")
            } catch (t: Throwable) {
                Log.w(TAG, "download failed: ${dest.name} — ${t.message}")
                runCatching { dest.delete() }
                settleDownload(id, DownloadState.FAILED, reason = t.message ?: "error")
                onDownloadEvent?.invoke("Download failed: ${middleTruncated(dest.name)} — ${t.message}")
            }
        }
        downloadJobs[id] = job
    }

    /** Persist decoded blob: bytes (from the JS bridge) into the workspace. */
    internal fun saveBlobDownload(data: ByteArray, filename: String, mimeType: String?) {
        val dir = sessionWorkspaceDir() ?: run {
            Log.w(TAG, "blob download rejected: no session bound to pool")
            return
        }
        // [audit-RC11] The filename comes from page-controlled JS (`<a download>`
        // attribute / fetch blob name). Refuse outright if it carries any path
        // component or is otherwise unusable — never let it escape the download dir.
        val safe = sanitizeDownloadName(filename) ?: run {
            Log.w(TAG, "blob download rejected: unsafe filename '${filename.take(80)}'")
            return
        }
        // guessFileName on a blob: URL yields "downloadfile.bin" — refine the
        // extension from the blob's actual MIME type when we have one.
        var name: String = safe
        if ((name.endsWith(".bin") || !name.contains('.')) && mimeType != null) {
            android.webkit.MimeTypeMap.getSingleton().getExtensionFromMimeType(mimeType)?.let { ext ->
                name = name.substringBeforeLast('.') + "." + ext
            }
        }
        val dest = uniqueFile(dir, name)
        val id = registerDownload(dest, data.size.toLong())
        val job = downloadScope.launch {
            try {
                dest.writeBytes(data)
                finishDownload(id, dest)
            } catch (e: kotlinx.coroutines.CancellationException) {
                runCatching { dest.delete() }
                settleDownload(id, DownloadState.FAILED, reason = "Cancelled")
            } catch (t: Throwable) {
                Log.w(TAG, "blob download save failed: ${t.message}")
                runCatching { dest.delete() }
                settleDownload(id, DownloadState.FAILED, reason = t.message ?: "error")
                onDownloadEvent?.invoke("Download failed: ${middleTruncated(dest.name)} — ${t.message}")
            }
        }
        downloadJobs[id] = job
    }

    private fun finishDownload(id: Long, dest: File) {
        val size = dest.length()
        settleDownload(id, DownloadState.COMPLETED, bytes = size)
        val sizeText = android.text.format.Formatter.formatShortFileSize(context, size)
        Log.i(TAG, "download finished: ${dest.name} ($sizeText) → ${dest.absolutePath}")
        // [T-android-browser-download-ux] iOS v3 semantics: the human-facing
        // notice is just name+size (middle-truncated) — the old full
        // "/var/minis/workspace/… — minis://workspace/…" path+link tail
        // wrapped badly in the bubble, and path navigation is the downloads
        // panel's job now.
        onDownloadEvent?.invoke("Downloaded ${middleTruncated(dest.name)} ($sizeText)")
    }

    /** Route a manager's download callbacks into this pool's workspace saver. */
    private fun wireDownloadHandlers(manager: BrowserUseManager) {
        manager.onDownloadStart = { url, ua, cd, mime, len -> startUrlDownload(url, ua, cd, mime, len) }
        manager.onBlobDownloadData = { data, name, mime -> saveBlobDownload(data, name, mime) }
    }

    // -- Agent Execution --

    /**
     * Execute a browser action. Routes to the correct tab, acquires it, and executes.
     */
    /**
     * @param singleTab [T-browser-readaction-follow-tab-and-yolo-android] YOLO
     *   mode: when true, ALL tab-less actions (including navigate) bypass the
     *   implicit-tab fan-out and target [selectedTabId] serially. This is the
     *   headless / CLI / RPC driver path — a single agent runs a strictly
     *   SERIAL sequence (navigate → execute_js → navigate → …) and expects
     *   "operate on the page I just navigated to", so fan-out (which exists to
     *   keep an agent's CONCURRENT navigates from stomping each other) is wrong.
     *   The UI / in-app agent-tool path leaves this false. Mirrors iOS
     *   `BrowserTabPool.execute(action:singleTab:)`. Explicit tab_id always
     *   routes to that tab regardless of this flag.
     */
    suspend fun execute(
        input: BrowserActionInput,
        singleTab: Boolean = false,
    ): BrowserActionResult {
        // [browser-rss P4] 打点定位：每个 action 执行前后各读一次主进程 VmRSS，
        // 把增量归因到具体 action（navigate/screenshot/click/…）。WebView 渲染
        // 进程是主进程 native/mmap 之外的另一大内存来源，但这条 browser 路径
        // 不经 offload socket，之前没有任何 RSS 归因——这是「泄漏涨在哪个
        // handler」的最后一块盲区。零副作用，不影响 action 结果。
        val rssBeforeKb = BrowserRssProbe.rssKb()
        val result = when (input.action) {
            BrowserAction.NEW_TAB -> newTab(input.url)
            BrowserAction.CLOSE_TAB -> closeTab(input.tabId)
            BrowserAction.LIST_TABS -> listTabs()
            BrowserAction.SET_VIEWPORT -> handleSetViewport(input)
            // [da1030-B] The UA is a pool-wide setting persisted to the same
            // `browser_prefs` keys the settings sheet writes, so route it
            // through the pool: the single-tab fallback path below applied it
            // to one tab and skipped the viewport replay, leaving the other
            // tabs (and the sheet) on the old profile.
            BrowserAction.SET_USER_AGENT -> handleSetUserAgent(input)
            else -> {
                // [T-browser-use-per-tab-serial-android] Serialize per explicit
                // tab id. Only an explicit tab_id that names an EXISTING tab can
                // be contended by two concurrent tools — that's the trampling
                // case, so those calls take that tab's Mutex (waiting up to
                // TAB_SERIAL_WAIT_TIMEOUT_MS for the prior op to release). A
                // null tab_id (or one pointing at a not-yet-created tab) is left
                // unlocked so concurrent tab-less calls fan out to separate tabs
                // via acquireTab and run in parallel rather than deadlocking.
                val serialTabId = input.tabId?.takeIf { reqId ->
                    _tabs.value.any { it.id == reqId }
                }
                if (serialTabId != null) {
                    executeSerialized(serialTabId, input)
                } else {
                    // [T-browser-readaction-follow-tab-and-yolo-android]
                    // Decide whether this tab-less action may fan out to a fresh
                    // tab or must stick to the current (selected) tab:
                    //
                    //  - YOLO / singleTab: NOTHING fans out — serial driver wants
                    //    every action on the one tab it's been navigating.
                    //  - operate-current-page actions (execute_js, get_text,
                    //    click, scroll, screenshot, …): follow selectedTabId even
                    //    in the default agent path. Fanning these out to a fresh/
                    //    other tab was the #612 bug (`navigate A → execute_js` ran
                    //    on a blank or stale tab because navigate's tab was still
                    //    in its inUse grace window).
                    //  - opens-new-page actions (navigate, fetch): keep the
                    //    grace-based fan-out so an agent's concurrent navigates
                    //    still open distinct tabs (#595) instead of trampling.
                    val mustFollowSelected = singleTab || !input.action.opensNewPage
                    if (mustFollowSelected && _tabs.value.isNotEmpty()) {
                        // Route to the selected tab under its serial lock. If the
                        // selected id was evicted, fall back to the most-recent
                        // existing tab so we still operate on a real page rather
                        // than spawning a blank one.
                        val sel = _selectedTabId.value
                        val targetId = if (_tabs.value.any { it.id == sel }) sel
                            else _tabs.value.maxByOrNull { it.lastActivityDate }!!.id
                        executeSerialized(targetId, input)
                    } else {
                        // Empty pool (first call) OR an opens-new-page action in
                        // the default agent path → original acquire/fan-out.
                        runAcquiredAction(input, implicitTab = true)
                    }
                }
            }
        }
        val rssAfterKb = BrowserRssProbe.rssKb()
        BrowserRssProbe.record(input.action.value, rssBeforeKb, rssAfterKb)
        return result
    }

    /**
     * [T-browser-use-per-tab-serial-android] Run [input] against an existing
     * tab id under that tab's serial Mutex. Waits at most
     * [TAB_SERIAL_WAIT_TIMEOUT_MS] to acquire the lock; on timeout returns a
     * guidance error telling the model to open a new tab and retry there
     * (rather than keep contending the busy one). The wait timeout is purely
     * the lock-acquisition wait — once the lock is held, the underlying page
     * operation runs with its own existing timeout, unchanged.
     */
    private suspend fun executeSerialized(tabId: Int, input: BrowserActionInput): BrowserActionResult {
        val mutex = lockForTab(tabId)
        val result = withTimeoutOrNull(TAB_SERIAL_WAIT_TIMEOUT_MS) {
            mutex.withLock {
                // [T-browser-readaction-follow-tab-and-yolo-android] Pin
                // acquisition to the locked tab id. The follow-selected and
                // YOLO paths resolve a target tab id whose lock we hold here,
                // but input.tabId may be null (tab-less call) — without this
                // override acquireTab(null) would re-run the fan-out and could
                // land on a DIFFERENT tab than the one we locked, defeating
                // both the serialization and the follow-tab fix.
                runAcquiredAction(input, implicitTab = false, acquireTabId = tabId)
            }
        }
        return result ?: BrowserActionResult.error(
            context.getString(
                R.string.browser_use_tab_busy_timeout,
                tabId,
                (TAB_SERIAL_WAIT_TIMEOUT_MS / 1000L).toInt(),
            ),
        )
    }

    /**
     * Acquire a tab (creating / selecting per [acquireTab]) and run the action.
     * Shared by the serialized (explicit existing tab id) and tab-less
     * execution paths. [T-browser-use-per-tab-serial-android]
     *
     * [deep-audit-1016c ISSUE-03A] Both paths end up holding the executed
     * tab's per-tab mutex around the WebView-driving call: `executeSerialized`
     * holds it before acquiring, and the implicit path takes it here around
     * `tab.manager.execute` — see the lock comment in the body.
     *
     * @param implicitTab true when the caller did NOT pre-lock the tab
     *   (`executeSerialized` passes false — Kotlin Mutex is not reentrant, so
     *   re-taking the tab lock here would self-deadlock).
     * @param acquireTabId [T-browser-readaction-follow-tab-and-yolo-android]
     *   when non-null, acquire exactly this tab id instead of `input.tabId` —
     *   used by [executeSerialized] so a tab-less follow-selected / YOLO action
     *   acquires the same tab whose serial lock the caller already holds.
     */
    private suspend fun runAcquiredAction(
        input: BrowserActionInput,
        implicitTab: Boolean,
        acquireTabId: Int? = null,
    ): BrowserActionResult {
        // [fix/audit-s4h1] acquireTab can now throw BrowserTabPoolExhausted
        // instead of silently hijacking a busy tab — surface it as an error
        // result (recoverable, visible) rather than letting it propagate out
        // of the browser action path.
        val tab = try {
            acquireTab(acquireTabId ?: input.tabId)
        } catch (e: BrowserTabPoolExhaustedException) {
            return BrowserActionResult.error(e.message ?: "all browser tabs busy")
        } ?: return BrowserActionResult.error("Failed to acquire browser tab")
        return try {
            // [deep-audit-1016c ISSUE-03A] The unlocked (implicit-tab) entry path
            // takes the ACQUIRED tab's per-tab mutex around the one call that
            // drives the WebView. Previously only `executeSerialized` took that
            // mutex, so an explicit `executeSerialized(T)` and a tab-less action
            // that fanned out to T could overlap: (1) the explicit path holds
            // `lockForTab(T)` before its own `acquireTab` flips `inUse=true`, a
            // Main-dispatcher gap in which the fan-out still sees T free; and
            // (2) `acquireTab`'s explicit-existing-id branch returns T without
            // checking `inUse`, so an explicit action walks straight onto a tab a
            // tab-less action is already driving. Locking here closes both: any
            // execute on T — explicit or implicit — now holds T's own mutex.
            //
            // `implicitTab == false` means the caller is `executeSerialized`,
            // which ALREADY holds `lockForTab(tab.id)`; Kotlin's Mutex is not
            // reentrant, so re-taking it here would self-deadlock.
            //
            // Deadlock freedom: each path holds at most one per-tab mutex at a
            // time (executeSerialized: lock → acquireTab, which takes no tab
            // lock; implicit: acquireTab → lock), so there is no circular wait.
            // The wait is bounded by the same timeout/policy as
            // [executeSerialized] — a stuck action must not hang a tab-less
            // caller forever.
            val executed = if (implicitTab) {
                withTimeoutOrNull(TAB_SERIAL_WAIT_TIMEOUT_MS) {
                    lockForTab(tab.id).withLock { tab.manager.execute(input) }
                }
            } else {
                tab.manager.execute(input)
            }
            val result = executed ?: return BrowserActionResult.error(
                context.getString(
                    R.string.browser_use_tab_busy_timeout,
                    tab.id,
                    (TAB_SERIAL_WAIT_TIMEOUT_MS / 1000L).toInt(),
                ),
            )

            // [T-browser-risk-control] Detect risk challenges (Cloudflare,
            // captcha, rate limits, etc.) from the current page title and URL.
            // Body text is intentionally omitted — fetching it via JS on every
            // action is too expensive. The title + URL patterns already cover
            // the common cases (Cloudflare "Attention Required!" titles,
            // captcha "Security check" titles, etc.).
            val riskChallenge = BrowserRiskControl.detectChallenge(
                title = tab.manager.pageTitle.value,
                currentUrl = tab.manager.currentURL.value,
            )

            // [T-browser-risk-control] Compute throttle delay for the next
            // action. Only applies to visual-change actions (navigate, click,
            // type, scroll_and_collect). Non-throttled actions get 0 delay.
            val throttleDelayMs = if (BrowserRiskControl.shouldThrottle(input.action)) {
                val baseDelay = BrowserRiskControl.baseThrottleDelayMs(
                    action = input.action,
                    rawUrl = tab.manager.currentURL.value,
                )
                BrowserRiskControl.computeThrottleDelayMs(
                    baseDelayMs = baseDelay,
                    elapsedSinceLastActionMs = if (lastActionTimeMs >= 0L)
                        System.currentTimeMillis() - lastActionTimeMs else null,
                    jitterMs = 0L,
                )
            } else 0L

            // Apply throttle delay before returning — this spreads out
            // consecutive page-interactive actions so the target server
            // sees a more human-like request cadence.
            if (throttleDelayMs > 0L) {
                delay(throttleDelayMs)
            }

            // Track the timestamp of this throttled action for the next call.
            if (BrowserRiskControl.shouldThrottle(input.action)) {
                lastActionTimeMs = System.currentTimeMillis()
            }

            // [T-android-browser-result-tab-id] Stamp the VERIFIED tab id — the
            // id of the tab we actually acquired and dispatched on, NOT the
            // global selectedTabId, which the fan-out branch in acquireTab
            // overwrites mid-flight when it spawns a fresh tab for a concurrent
            // navigate. Without this the agent had to guess tab_id for its
            // follow-up reads/scrolls and routinely picked the wrong tab.
            val stamped = stampTabId(result.copy(pageURL = tab.manager.currentURL.value), tab.id)

            // Stamp risk challenge info onto the result so the agent can react.
            //
            // [fix/dv17-1 B-01] Surfacing it through the structured fields
            // alone was not enough: nothing in the tree read
            // riskChallengeDetected / riskChallengeKind / recommendedNextAction
            // (ChatToolExecutors takes `result.text` and nothing else), so a
            // challenge page reached the model as undistinguished text and it
            // retried blindly. Merge the one-line notice into `text` — the
            // only exit the agent can read — via the pure renderer in
            // BrowserRiskControl, using the same "append a line" shape as
            // stampTabId. The fields stay written (contract unchanged); the
            // else branch appends nothing, so the common no-challenge path is
            // byte-identical to before.
            if (riskChallenge != null) {
                val notice = BrowserRiskControl.challengeNoticeLine(
                    kind = riskChallenge.kind,
                    recommendedNextAction = riskChallenge.recommendedNextAction,
                    throttleDelayMs = throttleDelayMs,
                )
                val text = if (notice != null && !stamped.text.contains(notice)) {
                    val needsNewline = stamped.text.isNotEmpty() && !stamped.text.endsWith("\n")
                    stamped.text + (if (needsNewline) "\n" else "") + notice
                } else {
                    stamped.text
                }
                stamped.copy(
                    text = text,
                    riskChallengeDetected = true,
                    riskChallengeKind = riskChallenge.kind,
                    recommendedNextAction = riskChallenge.recommendedNextAction,
                    throttleDelayMs = throttleDelayMs,
                )
            } else {
                stamped.copy(throttleDelayMs = throttleDelayMs)
            }
        } finally {
            tab.lastActivityDate = Date()
            if (implicitTab) {
                // [T-browser-implicit-tab-inuse-until-load-android] Keep the tab
                // marked inUse through a grace window instead of releasing it the
                // instant the action returns. The action itself already ran to
                // completion (navigate suspended until the page finished loading),
                // so this grace exists purely to repel a near-simultaneous
                // tab-less acquire — without it, three back-to-back navigates each
                // clear inUse before the next acquireTab(null) runs and all land on
                // tab 0. armImplicitGraceRelease re-arms on each action so a
                // same-task follow-up chain holds the tab; it flips inUse=false
                // after IMPLICIT_TAB_GRACE_MS of inactivity.
                armImplicitGraceRelease(tab)
            } else {
                // Explicit tab_id path keeps the original immediate release — its
                // serialization is the per-tab Mutex (executeSerialized), so a
                // grace hold here would only delay the model's own next deliberate
                // op on that same tab. [T-browser-use-per-tab-serial-android]
                tab.inUseGraceJob?.cancel()
                tab.inUseGraceJob = null
                setTabInUse(tab, false)
            }
            updateTabs()
            saveState()
        }
    }

    /**
     * [T-android-browser-result-tab-id] Stamp [targetId] onto a tab-contextual
     * result so the agent can see exactly which tab it just operated on, with no
     * need to guess for follow-up reads/scrolls. Sets the structured [tabId]
     * field and appends a `  tab_id: <N>` line to the human-readable text in the
     * same indent style as the existing `Title:` / `Viewport:` lines. Idempotent
     * — if the text already mentions `tab_id:` (e.g. newTab embeds it in its
     * narrative), skip the append.
     */
    private fun stampTabId(result: BrowserActionResult, targetId: Int): BrowserActionResult {
        val needsLine = !result.text.contains("tab_id:")
        val text = if (needsLine) {
            val needsNewline = result.text.isNotEmpty() && !result.text.endsWith("\n")
            result.text + (if (needsNewline) "\n" else "") + "  tab_id: $targetId"
        } else {
            result.text
        }
        return result.copy(text = text, tabId = targetId)
    }

    /**
     * [T-browser-implicit-tab-inuse-until-load-android] Hold [tab] inUse for a
     * grace window after an implicit-tab action, re-arming on each call. The
     * tab stays inUse=true now; a freshly-launched job flips it false after
     * [IMPLICIT_TAB_GRACE_MS] unless another action re-arms first. Runs on the
     * Main dispatcher so the inUse mutation is consistent with acquireTab (which
     * reads/writes inUse on Main).
     */
    private fun armImplicitGraceRelease(tab: Tab) {
        setTabInUse(tab, true)
        tab.inUseGraceJob?.cancel()
        tab.inUseGraceJob = evictionScope.launch {
            delay(IMPLICIT_TAB_GRACE_MS)
            withContext(Dispatchers.Main) {
                setTabInUse(tab, false)
                tab.inUseGraceJob = null
                tab.lastActivityDate = Date()
                updateTabs()
            }
        }
    }

    /**
     * Acquire a tab for agent use. Creates tab 0 if none exist.
     *
     * Mirrors iOS BrowserTabPool behavior: even when the model explicitly
     * sends `tab_id: 0` (strict-schema providers like OpenAI Responses API
     * always populate every field), fall back to the default tab when no
     * such tab exists yet, instead of returning null. Otherwise the very
     * first browser_use call in a session fails with "Failed to acquire
     * browser tab" because the model can't know that tab 0 hasn't been
     * lazily created.
     */
    private suspend fun acquireTab(requestedTabId: Int? = null): Tab? = withContext(Dispatchers.Main) {
        var currentTabs = _tabs.value.toMutableList()

        // Find requested tab, falling back to default-or-create when the
        // requested id doesn't exist (covers `tab_id: 0` against an empty pool).
        val tab = if (requestedTabId != null && currentTabs.any { it.id == requestedTabId }) {
            currentTabs.first { it.id == requestedTabId }
        } else {
            // [T-browser-use-per-tab-serial-android] No explicit (existing)
            // tab id: prefer a tab that is NOT already in use, and create a new
            // one when every existing tab is busy (up to MAX_TABS). This runs on
            // the Main dispatcher so concurrent tab-less calls resolve here one
            // after another — the first claims a free tab and marks it in-use,
            // the second then skips it and lands on a different / freshly-created
            // tab. That lets two parallel tab-less navigates open two tabs and
            // run concurrently instead of both trampling tab 0. A sequential
            // follow-up (e.g. screenshot after navigate) still reuses the same
            // tab because it's no longer in use by then.
            // [T-browser-implicit-tab-inuse-until-load-android] When every tab is
            // busy (inUse, incl. the post-action grace hold) AND we're at
            // MAX_TABS so a new tab can't be created, do NOT overwrite a busy tab
            // — that's the trampling this task forbids. Wait (bounded) for a tab
            // to free up; only fall back to reusing the least-recently-active tab
            // if nothing frees within the wait window.
            var picked = currentTabs.firstOrNull { !it.inUse } ?: createTab(currentTabs)
            if (picked == null) {
                val waitDeadline = IMPLICIT_TAB_WAIT_MS
                var waited = 0L
                while (waited < waitDeadline) {
                    delay(IMPLICIT_TAB_WAIT_POLL_MS)
                    waited += IMPLICIT_TAB_WAIT_POLL_MS
                    currentTabs = _tabs.value.toMutableList()
                    picked = currentTabs.firstOrNull { !it.inUse } ?: createTab(currentTabs)
                    if (picked != null) break
                }
            }
            // [fix/audit-s4h1] The old fallback was `picked ?: currentTabs
            // .firstOrNull()` — the LOWEST-ID (= oldest-created) tab, which at
            // this point is necessarily still inUse=true (any free tab would
            // have been picked above), i.e. exactly the trampling the comment
            // above forbids: one session's navigate/execute_js stomps another
            // session's in-flight action. Fail explicitly instead: the caller
            // surfaces an "all tabs busy" error, which is recoverable, while
            // a silently hijacked tab is not.
            if (picked == null) {
                throw BrowserTabPoolExhaustedException(
                    "all ${currentTabs.size} tab(s) busy after waiting ${IMPLICIT_TAB_WAIT_MS}ms"
                )
            }
            picked
        }

        if (tab != null) {
            // Re-acquiring a tab that was sitting in its post-action grace window
            // cancels the pending release — it's actively in use again now.
            tab.inUseGraceJob?.cancel()
            tab.inUseGraceJob = null
            setTabInUse(tab, true)
            tab.lastActivityDate = Date()
            _selectedTabId.value = tab.id
            _tabs.value = currentTabs
        }

        // Apply the pending blank-page load from createTab() now that we're
        // in a suspend context. Must happen BEFORE returning so the agent's
        // first JS evaluation on this tab sees `document.body` populated.
        if (tab != null && tab.needsInitialBlankPage) {
            tab.needsInitialBlankPage = false
            tab.manager.loadBlankPage()
        }
        tab
    }

    private fun createTab(tabs: MutableList<Tab>, url: String? = null, explicitId: Int? = null): Tab? {
        if (tabs.size >= MAX_TABS) return null

        // [fix/sweep1014-b17a] explicitId: the restore path rebuilds a saved
        // tab with its original id so the persisted selectedTabId resolves
        // against it; normal allocation keeps using the monotonic counter.
        val id = explicitId ?: nextTabId++
        val webView = WebView(context)
        val manager = BrowserUseManager(webView, userAgentProfile)
        // [audit-RC13] New tabs inherit the pool's session pin so minis://
        // subresource interception resolves session-scoped too.
        manager.minisSessionId = sessionId
        if (userAgentProfile == UserAgentProfile.CUSTOM && !customUserAgentString.isNullOrEmpty()) {
            manager.setUserAgent(userAgentProfile, customUserAgentString)
        }
        // Honor the resolved viewport (session override > global custom > UA default).
        val (vpW, vpH) = resolvedViewportSize()
        manager.applyViewport(vpW, vpH)

        // Setup window.open / close handlers
        manager.onNewWindow = { resultMsg -> handleNewWindow(resultMsg) }
        manager.onCloseWindow = { handleCloseWindow(manager) }
        wireDownloadHandlers(manager)

        val tab = Tab(id = id, manager = manager)
        tabs.add(tab)
        _tabs.value = tabs.toList()

        // Load saved URL or provided URL. When neither is supplied the tab
        // is marked `needsInitialBlankPage` so the caller (acquireTab /
        // newTab / handleNewWindow) can issue a blank-page load once we're
        // back in a suspend context — a fresh tab with no page loaded
        // reports WebView's hardcoded 980px fallback, hiding the session
        // viewport override until the first real navigation.
        val loadUrl = url ?: savedURLs.remove(id)
        if (loadUrl != null) {
            manager.loadURL(loadUrl)
        } else {
            tab.needsInitialBlankPage = true
        }

        Log.i(TAG, "Created tab $id (total: ${tabs.size})")
        return tab
    }

    // -- Tab Management Actions --

    private suspend fun newTab(url: String?): BrowserActionResult = withContext(Dispatchers.Main) {
        val currentTabs = _tabs.value.toMutableList()
        if (currentTabs.size >= MAX_TABS) {
            return@withContext BrowserActionResult.error("Maximum $MAX_TABS tabs reached")
        }
        val tab = createTab(currentTabs, url)
        if (tab == null) {
            return@withContext BrowserActionResult.error("Failed to create new tab")
        }
        _selectedTabId.value = tab.id
        if (tab.needsInitialBlankPage) {
            tab.needsInitialBlankPage = false
            tab.manager.loadBlankPage()
        }
        saveState()
        // [T-android-browser-result-tab-id] Set the structured tabId. Text isn't
        // restamped here — newTab's own narrative already names the tab id below.
        BrowserActionResult(
            text = "Opened new tab ${tab.id}" + (if (url != null) " at $url" else "") +
                ". Use tab_id: ${tab.id} to target this tab.",
            tabId = tab.id,
        )
    }

    private suspend fun closeTab(tabId: Int?): BrowserActionResult = withContext(Dispatchers.Main) {
        val id = tabId ?: _selectedTabId.value
        val currentTabs = _tabs.value.toMutableList()
        val idx = currentTabs.indexOfFirst { it.id == id }
        if (idx < 0) return@withContext BrowserActionResult.error("Tab $id not found")

        destroyTab(currentTabs, currentTabs[idx])
        _tabs.value = currentTabs

        // Select next tab
        if (currentTabs.isNotEmpty() && _selectedTabId.value == id) {
            _selectedTabId.value = currentTabs.first().id
        }
        saveState()
        BrowserActionResult(text = "Closed tab $id")
    }

    private fun listTabs(): BrowserActionResult {
        val lines = _tabs.value.map { tab ->
            val marker = if (tab.id == _selectedTabId.value) "*" else " "
            val title = tab.manager.pageTitle.value.ifEmpty { "(blank)" }
            val url = tab.manager.currentURL.value.ifEmpty { "about:blank" }
            "$marker Tab ${tab.id}: $title — $url"
        }
        return if (lines.isEmpty()) {
            BrowserActionResult(text = "No open tabs")
        } else {
            BrowserActionResult(text = lines.joinToString("\n"))
        }
    }

    // -- window.open / close --

    private fun handleNewWindow(resultMsg: Message) {
        val currentTabs = _tabs.value.toMutableList()
        if (currentTabs.size >= MAX_TABS) {
            Log.w(TAG, "window.open rejected: max tabs reached")
            return
        }
        val id = nextTabId++
        val newWebView = WebView(context)
        val manager = BrowserUseManager(newWebView, userAgentProfile)
        // [audit-RC13] window.open tabs inherit the session pin too.
        manager.minisSessionId = sessionId
        if (userAgentProfile == UserAgentProfile.CUSTOM && !customUserAgentString.isNullOrEmpty()) {
            manager.setUserAgent(userAgentProfile, customUserAgentString)
        }
        val (vpW, vpH) = resolvedViewportSize()
        manager.applyViewport(vpW, vpH)
        manager.onNewWindow = { msg -> handleNewWindow(msg) }
        manager.onCloseWindow = { handleCloseWindow(manager) }
        wireDownloadHandlers(manager)

        val tab = Tab(id = id, manager = manager)
        currentTabs.add(tab)
        _tabs.value = currentTabs
        _selectedTabId.value = id
        // [dv16-B7] window.open has no follow-up save path — persist now or
        // the new tab (and the selected-id switch) resurrects from the stale
        // state file on reload. Same position as closeTabFromUI: after all
        // state assignments, before anything else consumes the result.
        saveState()

        // Send the WebView transport back
        val transport = resultMsg.obj as? WebView.WebViewTransport
        transport?.webView = newWebView
        resultMsg.sendToTarget()

        Log.i(TAG, "window.open → created tab $id")
    }

    private fun handleCloseWindow(manager: BrowserUseManager) {
        val currentTabs = _tabs.value.toMutableList()
        val idx = currentTabs.indexOfFirst { it.manager === manager }
        if (idx >= 0) {
            val closedId = currentTabs[idx].id
            destroyTab(currentTabs, currentTabs[idx])
            _tabs.value = currentTabs
            if (_selectedTabId.value == closedId && currentTabs.isNotEmpty()) {
                _selectedTabId.value = currentTabs.first().id
            }
            // [dv16-B7] window.close triggers no later save — persist now or
            // the closed tab (and the stale selectedTabId) stay in the state
            // file and resurrect on reload. destroyTab already removed the
            // URL from savedURLs, so the persisted JSON drops it too.
            saveState()
            Log.i(TAG, "window.close → destroyed tab $closedId")
        }
    }

    // -- UI Tab Actions --

    /** Select a tab by ID (user tapped on tab chip). */
    fun selectTab(id: Int) {
        if (_tabs.value.any { it.id == id }) {
            _selectedTabId.value = id
        }
    }

    /** Create a new tab from the UI (user tapped + button). */
    suspend fun newTabFromUI(): Tab? = withContext(Dispatchers.Main) {
        val currentTabs = _tabs.value.toMutableList()
        if (currentTabs.size >= MAX_TABS) return@withContext null
        val tab = createTab(currentTabs) ?: return@withContext null
        _selectedTabId.value = tab.id
        if (tab.needsInitialBlankPage) {
            tab.needsInitialBlankPage = false
            tab.manager.loadBlankPage()
        }
        saveState()
        tab
    }

    /** Close a tab from the UI (user tapped X on tab chip). */
    suspend fun closeTabFromUI(tabId: Int) = withContext(Dispatchers.Main) {
        val currentTabs = _tabs.value.toMutableList()
        val idx = currentTabs.indexOfFirst { it.id == tabId }
        if (idx < 0) return@withContext
        destroyTab(currentTabs, currentTabs[idx])
        _tabs.value = currentTabs
        if (_selectedTabId.value == tabId && currentTabs.isNotEmpty()) {
            _selectedTabId.value = currentTabs.first().id
        }
        saveState()
    }

    /**
     * Select an existing tab whose current URL matches [url] (host + path,
     * ignoring trailing slash and query/fragment differences). If no tab
     * matches, create a new tab loaded with [url]. Returns the resolved tab,
     * or null if the pool is at capacity and creation failed.
     *
     * Mirrors the "if it's in the pool show it, otherwise reload" UX flow for the
     * tool-call preview's globe button — the user expects to land on the
     * agent's existing tab if it's still around, otherwise spawn a new one
     * rather than clobber an unrelated tab.
     */
    fun selectOrCreateTabForURL(url: String): Tab? {
        if (url.isBlank()) return _tabs.value.firstOrNull()
        val target = normalizeUrlForMatch(url)
        val currentTabs = _tabs.value.toMutableList()
        val match = currentTabs.firstOrNull { normalizeUrlForMatch(it.manager.currentURL.value) == target }
        if (match != null) {
            _selectedTabId.value = match.id
            match.lastActivityDate = Date()
            updateTabs()
            return match
        }
        val tab = createTab(currentTabs, url) ?: return null
        _selectedTabId.value = tab.id
        saveState()
        return tab
    }

    private fun normalizeUrlForMatch(raw: String): String {
        if (raw.isEmpty()) return ""
        return try {
            val uri = android.net.Uri.parse(raw)
            val scheme = uri.scheme?.lowercase() ?: ""
            val host = uri.host?.lowercase() ?: ""
            val path = (uri.path ?: "").trimEnd('/')
            "$scheme://$host$path"
        } catch (_: Exception) {
            raw.trimEnd('/')
        }
    }

    /**
     * Ensure at least one tab exists (for user-facing browser sheet).
     * Does NOT mark as inUse. Must be called on the main thread.
     *
     * Also sets the selected tab id so the sheet's `selectedTab` lookup
     * resolves on first composition, and persists state.
     */
    fun ensureTabForUI(): Tab {
        val currentTabs = _tabs.value.toMutableList()
        val existing = currentTabs.firstOrNull()
        if (existing != null) {
            if (_selectedTabId.value != existing.id &&
                currentTabs.none { it.id == _selectedTabId.value }) {
                _selectedTabId.value = existing.id
            }
            return existing
        }
        val tab = createTab(currentTabs)!!
        _selectedTabId.value = tab.id
        saveState()
        return tab
    }

    // -- User Agent --

    /** Set user agent from UI settings. Applies to all existing tabs and reloads them. */
    fun setUserAgentFromUI(profile: UserAgentProfile, customUA: String? = null) {
        userAgentProfile = profile
        customUserAgentString = customUA
        for (tab in _tabs.value) {
            tab.manager.setUserAgent(profile, customUA)
        }
        // `setUserAgent` resets each tab's layout to the new UA profile's
        // default viewport. Re-apply the resolved viewport so a session or
        // global custom override isn't silently clobbered by a UA switch.
        // applyViewportToAllTabs is suspend because it awaits tab reloads;
        // fire-and-forget since this is called from the UI thread.
        evictionScope.launch { applyViewportToAllTabs() }
    }

    // -- Release --

    fun releaseAllTabs() {
        // [fix/audit-b21 / T7-L3] `it.copy(inUse = false)` replaced every Tab
        // with a new instance, which broke the identity link to in-flight
        // actions: their release paths (`tab.inUse = false`, the in-use grace
        // job, lastActivityDate bookkeeping) mutate the OLD object, so the pool
        // kept a copy that never saw those updates. Emit a fresh list — needed
        // for StateFlow to fire — but keep the live Tab instances.
        _tabs.value = _tabs.value.map { tab ->
            setTabInUse(tab, false)
            tab
        }
        // [da1040-B2] The refill above is content-equal to the previous list, so
        // `tabs` still emits nothing (that is the point of keeping the live
        // instances); the busy flag is the observable half, and re-deriving it
        // once past the swap publishes the settled value instead of the
        // intermediate one seen mid-map.
        publishAgentBusy()
        saveState()
    }

    /**
     * [T-android-trim-memory] Central tab teardown. Removes the tab from the
     * supplied list and destroys its WebView so the renderer process actually
     * frees memory (50-100 MB/tab). Must be called on the main thread (WebView
     * APIs are main-thread only) — all call sites ([closeTab], [closeTabFromUI],
     * [handleCloseWindow], [evictIdleTabs], trim callbacks) run on Main.
     */
    private fun destroyTab(tabs: MutableList<Tab>, tab: Tab) {
        tab.inUseGraceJob?.cancel()
        tab.inUseGraceJob = null
        tabs.remove(tab)
        // [fix/audit-s4m3/s4m4] Tab ids are monotonic and never reused, so
        // anything keyed by tab id leaks forever once the tab is gone:
        // savedURLs entries were only consumed on createTab (recreate-with-id)
        // and persisted into the state JSON; tabLocks Mutexes were never
        // removed at all. Both are "only-grow" tables on long soak.
        savedURLs.remove(tab.id)
        tabLocks.remove(tab.id)
        tab.manager.destroy()
    }

    /**
     * [T-android-trim-memory] Permanently dispose the pool: unregister the
     * low-memory callback and destroy every tab's WebView. Call when the owning
     * ViewModel is cleared / the app no longer needs browser tabs. After this
     * the pool must not be used again (create a fresh instance instead).
     *
     * [dv14-B2] dispose() must NOT re-persist tab state: each tab already
     * saved its state in real time on navigate/create/close/evict, and the
     * owning session's files may have just been deleted (SessionFileStore
     * deleteSessionFiles) — an unconditional saveState() here resurrected
     * the deleted session's browser_tabs/<sid>.json (plaintext URLs) on
     * disk.
     */
    fun dispose() {
        evictionJob?.cancel()
        evictionJob = null
        runCatching { context.applicationContext.unregisterComponentCallbacks(this) }
            .onFailure { Log.w(TAG, "dispose(): unregister failed: ${it.message}") }
        val current = _tabs.value
        if (current.isNotEmpty()) {
            val mutable = current.toMutableList()
            for (tab in current) {
                destroyTab(mutable, tab)
                Log.i(TAG, "dispose(): destroyed tab ${tab.id}")
            }
            _tabs.value = emptyList()
        }
    }

    // -- ComponentCallbacks2 (low-memory handling) --

    /** Required by [ComponentCallbacks2]; the pool has no config handling. */
    override fun onConfigurationChanged(newConfig: Configuration) = Unit

    /**
     * [T-android-trim-memory] Android invokes this when the system is low on
     * memory. WebViews are the pool's biggest consumer (one renderer process
     * per tab, 50-100 MB each), so we sacrifice idle tabs before the OS kills
     * the whole process. Tiers driven by [TrimPolicy]:
     *  - RUNNING_CRITICAL (15): drop everything except the selected tab —
     *    the agent's active context survives a near-kill.
     *  - RUNNING_MODERATE/LOW (5/10) + MODERATE/COMPLETE (60/80): drop idle tabs.
     *  - UI_HIDDEN/BACKGROUND (20/40): [fix-trim-memory-semantics] drop ONLY
     *    long-idle tabs. The old code routed UI_HIDDEN(20)+ into the LOW(10)
     *    branch and killed every idle tab on a mere background switch —
     *    a background switch isn't memory-critical, so we keep recently-used
     *    and selected tabs alive (2026-08-20 real-device: browser tabs killed
     *    on every app-background).
     * Tabs in use (an agent action is mid-flight) and the selected tab are
     * protected so ongoing work isn't corrupted mid-evaluation. Dropped tab
     * URLs are saved to [savedURLs] so state persists and a later implicit tab
     * acquire can restore them, mirroring [evictIdleTabs].
     */
    override fun onTrimMemory(level: Int) {
        if (_tabs.value.isEmpty()) return
        val policy = TrimPolicy.browserTabKillPolicy(level)
        val idleTimeoutMs = idleTimeoutMs
        val now = System.currentTimeMillis()
        val isLongIdle = { tab: Tab -> !tab.inUse && (now - tab.lastActivityDate.time) >= idleTimeoutMs }
        val victims = when (policy) {
            TrimPolicy.BrowserTabKillPolicy.DROP_ALL_BUT_SELECTED -> {
                // Keep only the selected tab. [fix/audit-s4h2] Also keep tabs
                // with an agent action mid-flight: the class docs promise
                // "Tabs in use ... are protected so ongoing work isn't
                // corrupted mid-evaluation", but this branch previously only
                // filtered on id != selected — destroying a busy tab's WebView
                // under TRIM_MEMORY_RUNNING_CRITICAL, mid-evaluateJavascript.
                _tabs.value.filter { it.id != _selectedTabId.value && !it.inUse }
            }
            TrimPolicy.BrowserTabKillPolicy.DROP_ALL_IDLE -> {
                // Drop all idle tabs (foreground low pressure / overall pressure).
                _tabs.value.filter { !it.inUse }
            }
            TrimPolicy.BrowserTabKillPolicy.DROP_LONG_IDLE_ONLY -> {
                // Background / UI-hidden: drop only long-idle tabs, keep
                // recently-used ones so returning to the app preserves view state.
                _tabs.value.filter { isLongIdle(it) }
            }
        }
        if (victims.isEmpty()) return
        Log.i(TAG, "onTrimMemory($level): destroying ${victims.size} tab(s)")
        val currentTabs = _tabs.value.toMutableList()
        for (tab in victims) {
            val url = tab.manager.currentURL.value
            destroyTab(currentTabs, tab)
            // [dv14-B2] Assign AFTER destroyTab: destroyTab removes the tab's
            // savedURLs entry, so an assignment made before it was silently
            // wiped and the evicted tab's URL was lost.
            rememberEvictedUrl(url)?.let { savedURLs[tab.id] = it }
            Log.i(TAG, "Trimmed tab ${tab.id} (level $level)")
        }
        _tabs.value = currentTabs
        if (currentTabs.isNotEmpty() && currentTabs.none { it.id == _selectedTabId.value }) {
            _selectedTabId.value = currentTabs.first().id
        }
        saveState()
    }

    /** [T-android-trim-memory] Old-style callback; map to the CRITICAL tier. */
    override fun onLowMemory() {
        onTrimMemory(ComponentCallbacks2.TRIM_MEMORY_RUNNING_CRITICAL)
    }

    // -- Idle Eviction (call from a timer) --

    fun evictIdleTabs() {
        val now = System.currentTimeMillis()
        val currentTabs = _tabs.value.toMutableList()
        val timeoutMs = idleTimeoutMs
        val toRemove = currentTabs.filter { !it.inUse && (now - it.lastActivityDate.time) >= timeoutMs }
        for (tab in toRemove) {
            val url = tab.manager.currentURL.value
            destroyTab(currentTabs, tab)
            // [dv14-B2] Same ordering fix as onTrimMemory: the URL must be
            // stored after destroyTab, which removes the savedURLs entry.
            rememberEvictedUrl(url)?.let { savedURLs[tab.id] = it }
            Log.i(TAG, "Evicted idle tab ${tab.id}")
        }
        if (toRemove.isNotEmpty()) {
            _tabs.value = currentTabs
            if (currentTabs.isNotEmpty() && currentTabs.none { it.id == _selectedTabId.value }) {
                _selectedTabId.value = currentTabs.first().id
            }
            saveState()
        }
    }

    // -- Viewport API (mirrors iOS BrowserTabPool) --

    /**
     * Resolved viewport for new WebViews. Priority (matches iOS
     * `resolvedViewportSize()`): session override > global custom > UA profile default.
     */
    fun resolvedViewportSize(): Pair<Int, Int> {
        if (_sessionViewportWidth.value > 0 && _sessionViewportHeight.value > 0) {
            return _sessionViewportWidth.value to _sessionViewportHeight.value
        }
        if (_customViewportWidth.value > 0 && _customViewportHeight.value > 0) {
            return _customViewportWidth.value to _customViewportHeight.value
        }
        return userAgentProfile.viewportSize
    }

    /** True if a session or global custom viewport is active. */
    fun hasCustomViewport(): Boolean =
        (_sessionViewportWidth.value > 0 && _sessionViewportHeight.value > 0) ||
            (_customViewportWidth.value > 0 && _customViewportHeight.value > 0)

    /**
     * Set the app-wide custom viewport. Persists to SharedPreferences so it
     * survives app restarts. Mirrors iOS `setGlobalViewport(width:height:)`.
     */
    suspend fun setGlobalViewport(width: Int, height: Int) {
        _customViewportWidth.value = width.coerceAtLeast(0)
        _customViewportHeight.value = height.coerceAtLeast(0)
        val prefs = context.getSharedPreferences("browser_prefs", Context.MODE_PRIVATE)
        prefs.edit()
            .putInt(PREF_GLOBAL_VIEWPORT_WIDTH, _customViewportWidth.value)
            .putInt(PREF_GLOBAL_VIEWPORT_HEIGHT, _customViewportHeight.value)
            .apply()
        applyViewportToAllTabs()
    }

    /**
     * Set the session-scoped viewport override. Persisted into the session's
     * tab JSON (not SharedPreferences) so a set_viewport survives restarts but
     * doesn't leak to other sessions. Mirrors iOS `setSessionViewport`.
     *
     * Awaits tab reloads so a follow-up get_page_info sees the new
     * `window.innerWidth/Height`.
     */
    suspend fun setSessionViewport(width: Int, height: Int) {
        _sessionViewportWidth.value = width.coerceAtLeast(0)
        _sessionViewportHeight.value = height.coerceAtLeast(0)
        applyViewportToAllTabs()
        saveState()
    }

    /**
     * Clear the session viewport override so tabs fall back to the global
     * setting. Does not touch SharedPreferences. Mirrors iOS
     * `resetSessionViewport`. Awaits tab reloads.
     */
    suspend fun resetSessionViewport() {
        _sessionViewportWidth.value = 0
        _sessionViewportHeight.value = 0
        applyViewportToAllTabs()
        saveState()
    }

    /**
     * Re-apply the current resolved viewport to every live tab's manager.
     * A resize alone doesn't refresh `window.innerWidth/Height` — Android's
     * WebView snapshots the CSS viewport at load time — so reload every tab
     * after the new layout has been applied. Reload unconditionally, even
     * for `about:blank`: a blank tab reports `window.innerWidth=980` (the
     * Android no-viewport fallback) regardless of container size, so
     * leaving it alone would make `set_viewport` look like a no-op on a
     * fresh tab. A blank reload has no network cost. Matches iOS
     * `applyViewportToAllTabs`, which rebuilds the WKWebView and re-navigates.
     */
    private suspend fun applyViewportToAllTabs() {
        val (w, h) = resolvedViewportSize()
        withContext(Dispatchers.Main) {
            for (tab in _tabs.value) {
                tab.manager.applyViewport(w, h)
                // Await navigation so a follow-up `get_page_info` reads
                // the post-reload `window.innerWidth/Height` instead of
                // the stale pre-reload values.
                tab.manager.reloadAndWait()
            }
        }
    }

    private suspend fun handleSetViewport(input: BrowserActionInput): BrowserActionResult = withContext(Dispatchers.Main) {
        if (input.reset) {
            resetSessionViewport()
            val (w, h) = resolvedViewportSize()
            return@withContext BrowserActionResult(text = "Viewport reset to default (${w}x$h)")
        }
        val w = input.viewportWidth
        val h = input.viewportHeight
        if (w == null || h == null || w <= 0 || h <= 0) {
            return@withContext BrowserActionResult.error(
                "set_viewport requires positive --width and --height, or --reset to restore defaults"
            )
        }
        setSessionViewport(w, h)
        BrowserActionResult(text = "Viewport set to ${w}x$h (session override)")
    }

    /**
     * [da1030-B] Pool-wide `set_user_agent`. A tool call carries only the
     * profile name, so the stored custom UA string (typed in the settings
     * sheet, restored from prefs) is reused verbatim for
     * [UserAgentProfile.CUSTOM] instead of being cleared — switching away from
     * CUSTOM keeps it stored so switching back restores the same UA.
     *
     * Mirrors [handleSetViewport]: publishes both the live tabs and the
     * persisted setting, then replays the *resolved* viewport, because
     * `setUserAgent` resets each tab's layout to the new profile's default and
     * would otherwise silently clobber a session/global custom override.
     */
    private suspend fun handleSetUserAgent(input: BrowserActionInput): BrowserActionResult = withContext(Dispatchers.Main) {
        val newProfile = input.userAgent
            ?: return@withContext BrowserActionResult.error("set_user_agent requires --user-agent")
        val customUA = if (newProfile == UserAgentProfile.CUSTOM) customUserAgentString else null
        userAgentProfile = newProfile
        for (tab in _tabs.value) {
            tab.manager.setUserAgent(newProfile, customUA)
        }
        // Same keys the settings sheet writes — one meaning, one source of
        // truth, so a sheet reopened after an agent-driven switch seeds from
        // the new profile instead of the stale prefs snapshot.
        val prefs = context.getSharedPreferences("browser_prefs", Context.MODE_PRIVATE)
        val editor = prefs.edit().putString("user_agent_profile", newProfile.name)
        if (newProfile == UserAgentProfile.CUSTOM && !customUA.isNullOrEmpty()) {
            editor.putString("custom_user_agent", customUA)
        }
        editor.apply()
        applyViewportToAllTabs()
        val (w, h) = resolvedViewportSize()
        BrowserActionResult(text = "Switched to ${newProfile.value} (${w}x$h)")
    }

    // -- Disk Persistence --

    private fun saveState() {
        val sid = sessionId ?: return
        try {
            val dir = File(context.filesDir, "browser_tabs")
            dir.mkdirs()
            val file = File(dir, "$sid.json")
            val json = JSONObject()
            val urlsJson = JSONObject()
            for ((id, url) in mergedTabUrls(
                _tabs.value.map { Pair(it.id, it.manager.currentURL.value) },
                savedURLs,
            )) {
                urlsJson.put(id.toString(), url)
            }
            json.put("tabURLs", urlsJson)
            json.put("selectedTabId", _selectedTabId.value)
            // Persist session viewport override alongside tab URLs so reopening
            // the session restores the override. Mirrors iOS `PersistedTabs`.
            if (_sessionViewportWidth.value > 0 && _sessionViewportHeight.value > 0) {
                json.put("sessionViewportWidth", _sessionViewportWidth.value)
                json.put("sessionViewportHeight", _sessionViewportHeight.value)
            }
            file.writeText(json.toString())
        } catch (e: Exception) {
            Log.w(TAG, "Failed to save tab state: ${e.message}")
        }
    }

    private fun loadSavedState() {
        val sid = sessionId ?: return
        try {
            val file = File(context.filesDir, "browser_tabs/$sid.json")
            if (!file.exists()) {
                // [rv1014-E5] No state file for the pointed session — still drop
                // savedURLs residue from the previously pointed session (the
                // b17a clear below only covered the file-exists branch), so a
                // re-pointed pool doesn't carry the old session's tabs into
                // saveState or implicit-tab restore.
                savedURLs.clear()
                // [deep-audit-1016 9d] Same reason for the session viewport:
                // the override belongs to the session we are leaving, and this
                // branch used to leave it applied, so the next session silently
                // inherited a size it never set.
                _sessionViewportWidth.value = 0
                _sessionViewportHeight.value = 0
                return
            }
            val json = JSONObject(file.readText())
            // [fix/sweep1014-b17a] savedURLs is session-scoped (mirrors the
            // per-session file below) — drop entries left over from a
            // previously pointed session so a re-pointed pool doesn't restore
            // the old session's tabs into the new one.
            savedURLs.clear()
            val urlsJson = json.optJSONObject("tabURLs")
            if (urlsJson != null) {
                val keys = urlsJson.keys()
                while (keys.hasNext()) {
                    val key = keys.next()
                    savedURLs[key.toInt()] = urlsJson.getString(key)
                }
                _selectedTabId.value = json.optInt("selectedTabId", 0)
            }
            // [fix/sweep1014-b17a] Resume the id counter above every persisted
            // id (and any live tab id) so tabs created after load never
            // collide with the restored ones, and fix a stale selectedTabId
            // (points at a tab closed before the last save) eagerly so the
            // browser sheet doesn't flash a dangling selection before the
            // rebuild runs.
            val plan = planTabRestore(savedURLs.keys.toList(), _tabs.value.map { it.id }, _selectedTabId.value)
            nextTabId = plan.nextTabId
            _selectedTabId.value = plan.selectedTabId
            // Rebuild the saved tabs so restoration no longer depends on new
            // tabs happening to walk over the persisted ids. Async on Main —
            // loadSavedState may run off-main (lazy pool access from tool
            // execution) and WebView APIs are main-thread only.
            if (savedURLs.isNotEmpty()) {
                evictionScope.launch { withContext(Dispatchers.Main) { restoreSavedTabs() } }
            }
            // Restore session viewport override. 0/missing = no override; fall
            // back to the global custom viewport / UA profile default.
            val w = json.optInt("sessionViewportWidth", 0)
            val h = json.optInt("sessionViewportHeight", 0)
            // [deep-audit-1016 9d] Always write the restored override, including
            // the "none" case: the old `if (w > 0 && h > 0)` guard left a
            // previous session's size in place whenever the new session had no
            // (or a degenerate 0) override, so a re-pointed pool kept a viewport
            // the new session never set. 0/0 means "no override" and falls back
            // to the global custom viewport / UA profile default.
            val restoredViewport = BrowserSessionBinding.viewportForLoadedSession(w, h)
            _sessionViewportWidth.value = restoredViewport.width
            _sessionViewportHeight.value = restoredViewport.height
            if (restoredViewport.hasOverride) {
                // Re-apply to any live tabs that predate load (rare). At
                // session-load time we're not in a suspend context and the
                // usual case has zero live tabs, so fire-and-forget is
                // adequate — the override is already stored and new tabs
                // will pick it up via `resolvedViewportSize()`.
                if (_tabs.value.isNotEmpty()) {
                    evictionScope.launch { applyViewportToAllTabs() }
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to load tab state: ${e.message}")
        }
    }

    /**
     * [fix/sweep1014-b17a] Rebuild tabs for entries left in [savedURLs] by the
     * previous run, keeping their original ids so the persisted selectedTabId
     * resolves against them. Runs on Main (WebView APIs are main-thread only)
     * — scheduled from [loadSavedState], which may be called off-main.
     * [nextTabId] was already seeded above the max persisted/live id in
     * [loadSavedState], so tabs created in the meantime never collide with
     * the restored ones.
     */
    private fun restoreSavedTabs() {
        val currentTabs = _tabs.value.toMutableList()
        val plan = planTabRestore(savedURLs.keys.toList(), currentTabs.map { it.id }, _selectedTabId.value)
        var restored = 0
        for (id in plan.restoreIds) {
            val url = savedURLs[id] ?: continue
            val tab = createTab(currentTabs, url, explicitId = id) ?: break
            savedURLs.remove(id)
            restored++
        }
        for (id in plan.droppedIds) {
            // A live tab already owns this id — the live tab wins; drop the
            // stale entry so it stops re-persisting into the state JSON.
            savedURLs.remove(id)
        }
        if (restored > 0) {
            saveState()
        }
        Log.i(TAG, "Restored $restored tab(s) from saved state")
    }

    private fun updateTabs() {
        _tabs.value = _tabs.value.toList()
        // [da1040-B2] Membership and in-place bookkeeping both land here, and
        // neither can reach a collector through `tabs` (content-equal list).
        // Re-deriving the busy flag on every publication keeps "is the agent
        // working" observable even when only `lastActivityDate` moved.
        publishAgentBusy()
    }
}

/**
 * [deep-audit-1016 9c/9d] Pure decision core for the shared shell browser
 * pool's session binding and for the session-scoped viewport.
 *
 * Split out of [BrowserTabPool] so both rules are JVM-testable without a
 * WebView or a Context — the same reason
 * [com.rikkaminis.app.ui.chat.AgentLoopExitDecision] lives on its own.
 */
internal object BrowserSessionBinding {

    /** What re-pointing a pool from [current] to [next] implies. */
    data class Rebind(
        /** False when the pool is already pointed at [next] (no-op call). */
        val rebind: Boolean,
        /**
         * True when the session-scoped viewport override must be dropped: the
         * stored value belongs to the session being left, and this path
         * deliberately does not load the new session's state.
         */
        val resetViewport: Boolean,
    )

    /**
     * Re-binding to the SAME session must be a no-op: the pool's tabs, its
     * persisted state and its viewport override all already belong to that
     * session, and a repeated `minis-browser-use` call inside one turn must not
     * throw the override away (`set_viewport` … `get_page_info` is a multi-call
     * sequence).
     *
     * A null [current] (pool never bound) counts as a change.
     */
    fun decideRebind(current: String?, next: String): Rebind {
        if (current == next) return Rebind(rebind = false, resetViewport = false)
        return Rebind(rebind = true, resetViewport = true)
    }

    /** A session viewport override; `0 × 0` means "no override". */
    data class Viewport(val width: Int, val height: Int) {
        val hasOverride: Boolean get() = width > 0 && height > 0
    }

    /**
     * The override a freshly loaded session state yields, given the two
     * persisted values. Anything that is not a complete positive pair — absent
     * keys, a degenerate 0, a corrupt negative — collapses to `0 × 0` rather
     * than being skipped by the caller, because "the new session has no
     * override" and "keep the previous session's override" must not be the same
     * code path.
     */
    fun viewportForLoadedSession(persistedWidth: Int, persistedHeight: Int): Viewport {
        val w = persistedWidth.coerceAtLeast(0)
        val h = persistedHeight.coerceAtLeast(0)
        return if (w > 0 && h > 0) Viewport(w, h) else Viewport(0, 0)
    }
}

/**
 * [dv14-B2] Pure merge used by saveState(): live tab URLs first (blank URLs
 * skipped), then the evicted-tab entries — a later entry wins on id conflict,
 * matching the previous JSONObject put-over-put behaviour. Kept as a pure
 * top-level function so the savedURLs→disk face is JVM-testable.
 */
internal fun mergedTabUrls(
    liveUrls: List<Pair<Int, String>>,
    evictedUrls: Map<Int, String>,
): Map<Int, String> {
    val out = LinkedHashMap<Int, String>()
    for ((id, url) in liveUrls) {
        if (url.isNotEmpty()) out[id] = url
    }
    out.putAll(evictedUrls)
    return out
}

/**
 * [dv14-B2] The evicted tab's URL is only worth remembering when it is
 * non-blank — pure form of the `if (url.isNotEmpty())` guard used by the
 * trim / idle-evict paths (stored AFTER destroyTab, see the call sites).
 */
internal fun rememberEvictedUrl(url: String): String? = url.takeIf { it.isNotEmpty() }

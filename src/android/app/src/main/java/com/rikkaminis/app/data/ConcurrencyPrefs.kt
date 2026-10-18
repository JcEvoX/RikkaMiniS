package com.rikkaminis.app.data

import android.content.Context
import android.content.SharedPreferences

/**
 * [native-rss-tool-guard / D-2] App-level persisted "max concurrent agent-loop
 * sessions" preference. Phase 0 hard-coded the cross-session concurrency cap
 * to 2 at three aligned places (SessionConcurrencyManager, ExecutionCoordinator,
 * NativeOffloadServer) to keep the shared main-process native/mmap RSS from
 * climbing to the 5.8–6.0GB SIGABRT seen on 2026-08-19. D-2 makes that cap
 * configurable + observable so the user can run with 2 for a while, read the
 * slot occupancy, then evaluate widening to 3.
 *
 * Mirrors the FastModePrefs pattern: [prime] warms a volatile cache from
 * persisted state once at app startup (MinisApp.onCreate); the context-free [maxConcurrentSessions] is then safe to call from any layer
 * (sandbox singletons etc.) that has no Context. The value is read once at
 * prime time and held until the next process start, so a change applies the
 * next time the app process launches — safe, no runtime slot resizing.
 *
 * [ARCH-02] The cache is also **self-syncing**: [prime] registers an
 * object-owned, strongly-referenced [selfSyncListener] on the prefs file, so
 * any direct SharedPreferences write (PrefsIntField via minis-config, backup
 * restore, the agent) refreshes [cachedMaxSessions] immediately — visibility
 * no longer depends on an external module remembering to re-prime. The
 * runtime effect statement above is unchanged: the sized semaphores keep the
 * bound they took at process start; this only makes the persisted value
 * *visible* to the context-free readers.
 *
 * Deliberately a SINGLE cap shared by all three coordinated gates. Phase 0
 * aligned them to the same bound on purpose; a single knob keeps them aligned
 * (changing one without the other two would silently let one path burst past
 * the budget the rest of the memory machinery was sized for).
 */
object ConcurrencyPrefs {
    const val PREFS = "minis_concurrency_prefs"
    const val KEY_MAX_CONCURRENT_SESSIONS = "maxConcurrentSessions"

    /**
     * Bounds. [MIN] floors at 1 (the slot controller requires strictly
     * positive). [MAX] is a soft upper sanity bound — effectively "uncapped"
     * for any realistic on-device use: with several concurrent agent sessions
     * the real backstop is no longer this knob but the process-RSS hard gate
     * and the heavy-command serializer in [ExecutionCoordinator], which stay
     * in force regardless of how many slots the user opens here.
     */
    const val MIN = 1
    const val MAX = 16

    /** Default matches Phase 0's hard-coded cap (2 aligned gates). */
    const val DEFAULT = 2

    @Volatile
    private var cachedMaxSessions: Int = DEFAULT

    @Volatile
    private var primed = false

    /**
     * [ARCH-02] Self-owned, strongly-referenced re-priming handle.
     *
     * `PrefsIntField` (minis-config), a backup restore and the agent all write
     * this prefs file **straight to SharedPreferences** — they never call
     * [setMaxConcurrentSessions]. Before ARCH-02 the only thing that refreshed
     * [cachedMaxSessions] after such a write was an external listener mounted in
     * `ConfigBuiltins`; if that registration is ever missed (or the listener is
     * collected, see the F-134 WeakHashMap note in `prefs_listener_holder_guard`),
     * every context-free reader keeps serving the value taken at startup.
     *
     * Holding the listener on the object that owns the cache makes the cache
     * self-refreshing: [prime] is the only thing anyone has to remember to call,
     * and it is already called from `MinisApp.onCreate`.
     *
     * The `SharedPreferences` instance handed to the callback is used directly —
     * no `Context` is retained, so there is no dead-field leak (F-235).
     *
     * Guard: `scripts/scan/prefs_listener_holder_guard.py`.
     */
    @Volatile
    private var listenerRegistered = false

    private val selfSyncListener =
        SharedPreferences.OnSharedPreferenceChangeListener { changed, _ ->
            cachedMaxSessions = clamp(changed.getInt(KEY_MAX_CONCURRENT_SESSIONS, DEFAULT))
        }

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** Capture the app context and warm the cache. Call from MinisApp.onCreate. */
    fun prime(context: Context) {
        val p = prefs(context)
        // Frameworks de-duplicate by listener identity too, but keeping the
        // registration to once per process makes the intent explicit and skips
        // the monitor on the hot path (prime is called again on every write by
        // the external re-primers too).
        if (!listenerRegistered) {
            synchronized(this) {
                if (!listenerRegistered) {
                    p.registerOnSharedPreferenceChangeListener(selfSyncListener)
                    listenerRegistered = true
                }
            }
        }
        cachedMaxSessions = clamp(p.getInt(KEY_MAX_CONCURRENT_SESSIONS, DEFAULT))
        primed = true
    }

    /** Context-free read for sandbox singletons / coordinators. */
    fun maxConcurrentSessions(): Int = cachedMaxSessions

    /**
     * Persist a new cap and warm the cache. Takes effect on the next process
     * start (lazy controller / semaphores are sized at init, deliberately not
     * resized at runtime — see class doc).
     */
    fun setMaxConcurrentSessions(context: Context, value: Int) {
        val v = clamp(value)
        cachedMaxSessions = v
        prefs(context).edit().putInt(KEY_MAX_CONCURRENT_SESSIONS, v).apply()
    }

    /** Exposed for tests: whether [prime] has run in this process. */
    fun isPrimed(): Boolean = primed

    private fun clamp(v: Int): Int = v.coerceIn(MIN, MAX)
}

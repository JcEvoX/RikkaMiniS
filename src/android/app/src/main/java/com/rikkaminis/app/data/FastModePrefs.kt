package com.rikkaminis.app.data

import android.content.Context
import android.content.SharedPreferences

/**
 * [T-codex-fast-mode] App-level persisted Fast Mode toggle (mirrors iOS
 * UserDefaults key `codexFastModeEnabled`, commit fb671083). Unlike Enhanced
 * Cache the flag IS durable and shared across sessions (user-confirmed on
 * iOS): switching to another chat reads the same enabled state.
 *
 * The provider layer has no Context, and iOS deliberately reads the flag at
 * REQUEST-BUILD time so a flip applies to the very next request of an ongoing
 * session — including offload / title-gen calls that never pass through the
 * ChatViewModel. To reproduce that, [prime] captures the value once at app
 * startup (MinisApp.onCreate) and warms a volatile cache; the context-free
 * [isEnabled] is then safe to call from any request builder.
 *
 * [F-235] The class used to also retain an `appContext` field that was written
 * by [prime] and never read anywhere (isEnabled reads `cachedEnabled`). It has
 * been removed: `ConcurrencyPrefs` cited "Mirrors the FastModePrefs pattern:
 * [prime] captures the application context" as its template, so leaving the
 * dead field in place was propagating the defect to the next copy.
 *
 * [ARCH-02] Self-syncing: [prime] registers an object-owned, strongly-held
 * [selfSyncListener], mirroring ConcurrencyPrefs. Today every writer goes
 * through [setEnabled] (which warms the cache), so the listener is structural
 * insurance for the first direct-SharedPreferences writer (backup restore /
 * a future `chat.*` ConfigBuiltins path) rather than a live desync fix.
 */
object FastModePrefs {
    private const val PREFS = "minis_fast_mode_prefs"
    private const val KEY_ENABLED = "codexFastModeEnabled"

    @Volatile
    private var cachedEnabled: Boolean = false

    /**
     * [ARCH-02] Self-owned, strongly-referenced re-priming handle — the same
     * contract as `ConcurrencyPrefs.selfSyncListener`.
     *
     * Today every write goes through [setEnabled] (which warms the cache
     * itself), so this listener is insurance rather than a live fix: the flag
     * is registered in no `ConfigBuiltins` path, so it currently has **no**
     * external re-primer at all. The first time anyone adds a `chat.*`/backup
     * path that writes `codexFastModeEnabled` straight to SharedPreferences
     * (the exact shape that desynced the concurrency cap in 1026 ISSUE-01),
     * the request builders would keep reading a stale cache until process
     * restart. Owning the listener in the object that owns the cache removes
     * that dependency on whoever writes next.
     *
     * The callback's `SharedPreferences` argument is used directly; no
     * `Context` is retained (F-235: the dead `appContext` field that used to
     * live here is documented above).
     *
     * Guard: `scripts/scan/prefs_listener_holder_guard.py`.
     */
    @Volatile
    private var listenerRegistered = false

    private val selfSyncListener =
        SharedPreferences.OnSharedPreferenceChangeListener { changed, _ ->
            cachedEnabled = changed.getBoolean(KEY_ENABLED, false)
        }

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** Warm the cache from persisted state. Called from MinisApp.onCreate. */
    fun prime(context: Context) {
        val p = prefs(context)
        if (!listenerRegistered) {
            synchronized(this) {
                if (!listenerRegistered) {
                    p.registerOnSharedPreferenceChangeListener(selfSyncListener)
                    listenerRegistered = true
                }
            }
        }
        cachedEnabled = p.getBoolean(KEY_ENABLED, false)
    }

    /**
     * Context-free read for request builders. Falls back to the warm cache
     * value (false before [prime] has ever run — matches an un-toggled fresh
     * install, and prime runs before any request can be built).
     */
    fun isEnabled(): Boolean {
        return cachedEnabled
    }

    fun setEnabled(context: Context, enabled: Boolean) {
        cachedEnabled = enabled
        prefs(context).edit().putBoolean(KEY_ENABLED, enabled).apply()
    }
}

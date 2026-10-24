package com.rikkaminis.app.data.repository

import android.content.Context
import android.content.SharedPreferences
import com.rikkaminis.app.config.PrefsWriteBus
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * T180-bg-notif: persistence for background-related toggles. Currently
 * only "Task Notifications" lives here; iOS exposes the same toggle in
 * `EnhancedBackgroundSettingsView` bound to
 * `BackgroundKeepAliveManager.backgroundNotificationsEnabled`.
 *
 * Default value is `true` to match iOS, where the toggle ships ON so
 * Live Activity and task-completion notifications work out-of-the-box
 * on first install. The user can opt out from Settings.
 */
class BackgroundSettingsRepository(context: Context) {

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    /**
     * [FIX-6 / F-240] Single source of truth for this prefs file + its keys.
     *
     * `ConfigBuiltins.registerBackground` registers fields against this same
     * file, so the registry (minis-config, in-app backup) and this repository
     * must name the keys identically. They previously did not: the registry
     * wrote `background_notifications_enabled` / `enhanced_background_execution`
     * while every reader in the app used the keys below — so a `minis-config
     * set background.notifications false` landed in a key nobody reads and the
     * notification kept firing (audit F-238/F-239/F-240). Exposing the keys
     * here lets the registry reference them instead of re-typing strings.
     */
    companion object {
        const val PREFS_NAME = "background_settings"
        const val KEY_TASK_NOTIFICATIONS = "taskNotificationsEnabled"
        const val DEFAULT_TASK_NOTIFICATIONS = true
        const val KEY_BG_OVERLAY_ENABLED = "backgroundOverlayEnabled"
        // [da1028-B / ISSUE-03] Same single-source rule as the two above: the
        // registry (ConfigBuiltins.registerBackground) registers
        // `background.overlay` against this key, so the default has to be one
        // declaration — a `false` literal on one side and `true` on the other
        // is exactly the F-238/F-239 drift shape this companion was created
        // to end. Defaults to OFF: the overlay needs SYSTEM_ALERT_WINDOW.
        const val DEFAULT_BG_OVERLAY_ENABLED = false
        const val KEY_BG_OVERLAY_X = "backgroundOverlayX"
        const val KEY_BG_OVERLAY_Y = "backgroundOverlayY"
    }

    /** Cached toggle values + their single refresh path ([BackgroundToggleCache]). */
    private val toggles = BackgroundToggleCache(prefs)

    /**
     * Live state of the toggle. Compose surfaces collect this so flipping
     * the switch in Settings is reflected immediately at every consumer
     * (notifier, FG service status text, etc).
     */
    val taskNotificationsEnabled: StateFlow<Boolean> get() = toggles.taskNotificationsEnabled

    fun setTaskNotificationsEnabled(value: Boolean) {
        prefs.edit().putBoolean(KEY_TASK_NOTIFICATIONS, value).apply()
        toggles.setTaskNotificationsEnabled(value)
    }

    /**
     * T-bg-overlay phase 2: "show floating tool-status overlay while the
     * app is backgrounded" toggle. Defaults to OFF — the overlay needs
     * SYSTEM_ALERT_WINDOW which is a separate system permission flow, so
     * we won't surface anything until the user opts in.
     */
    val backgroundOverlayEnabled: StateFlow<Boolean> get() = toggles.backgroundOverlayEnabled

    fun setBackgroundOverlayEnabled(value: Boolean) {
        prefs.edit().putBoolean(KEY_BG_OVERLAY_ENABLED, value).apply()
        toggles.setBackgroundOverlayEnabled(value)
    }

    /**
     * Last persisted overlay position (window x/y in pixels) from the
     * previous drag. -1 means "no remembered position — let the overlay
     * controller pick a default near the bottom-left, 10 dp from each
     * edge" ([T-bg-overlay-polish]).
     */
    fun getOverlayX(): Int = prefs.getInt(KEY_BG_OVERLAY_X, -1)
    fun getOverlayY(): Int = prefs.getInt(KEY_BG_OVERLAY_Y, -1)
    fun setOverlayPosition(x: Int, y: Int) {
        prefs.edit().putInt(KEY_BG_OVERLAY_X, x).putInt(KEY_BG_OVERLAY_Y, y).apply()
    }

    /**
     * [FIX-6 / F-239 refresh layer] Keep the StateFlows in sync when the prefs
     * file is written *outside* this repository — `minis-config set
     * background.notifications …` and a backup-restore both write the raw key.
     * Without a refresh, each StateFlow keeps its construction-time value and
     * every consumer (`BackgroundTaskNotifier`, `ConfigConfirmNotifier`, the
     * Settings switch) serves the stale boolean until process restart.
     *
     * AOSP keeps listeners in a `WeakHashMap`, so the handle must be a field
     * (F-134); the application-context-scoped prefs instance lives as long as
     * the process, so there is nothing to unregister.
     *
     * [dv29-A4] This listener alone was NOT enough on the device: with the app
     * running, `minis-config set background.overlay true` persisted the value
     * (`get` read it back) while `AgentForegroundService` kept logging
     * `enabled=false` for minutes — same pid, so the notification simply never
     * reached this repository's handle. [BackgroundToggleCache.writeSink] is the
     * delivery path the field writers push through; this listener stays as the
     * fallback for writes that do not go through a `Prefs*Field` (UI setters,
     * raw-key restores). Both funnel into
     * [BackgroundToggleCache.applyWrite], so there is exactly one key →
     * StateFlow mapping — and the sink is built *inside* the cache so the
     * "refresh from the writer's handle" property is unit-tested rather than
     * re-stated at each wiring site.
     */
    init {
        prefs.registerOnSharedPreferenceChangeListener(toggles.prefsListener)
        PrefsWriteBus.register(toggles.writeSink)
    }
}

/**
 * [dv29-A4] The two cached toggles plus the ONE function that refreshes them
 * from an external write.
 *
 * Split out of [BackgroundSettingsRepository] for a specific reason: the
 * property the device failed on is "the refresh reads the *writer's* handle,
 * not a cached one", and a `Context`-bound repository cannot be exercised in a
 * plain JVM test. Here it can (`BackgroundToggleCacheTest`), with two fake
 * handles over the same keys — one stale — exactly the shape observed on the
 * device.
 *
 * Reads from `source` (the handle passed by the caller), never from `prefs`
 * (this cache's own handle): under a shared instance the two are the same
 * object, under two instances only `source` is guaranteed to hold the new value.
 */
internal class BackgroundToggleCache(prefs: SharedPreferences) {

    private val _taskNotificationsEnabled =
        MutableStateFlow(
            prefs.getBoolean(
                BackgroundSettingsRepository.KEY_TASK_NOTIFICATIONS,
                BackgroundSettingsRepository.DEFAULT_TASK_NOTIFICATIONS,
            ),
        )
    private val _backgroundOverlayEnabled =
        MutableStateFlow(
            prefs.getBoolean(
                BackgroundSettingsRepository.KEY_BG_OVERLAY_ENABLED,
                BackgroundSettingsRepository.DEFAULT_BG_OVERLAY_ENABLED,
            ),
        )

    val taskNotificationsEnabled: StateFlow<Boolean> = _taskNotificationsEnabled.asStateFlow()
    val backgroundOverlayEnabled: StateFlow<Boolean> = _backgroundOverlayEnabled.asStateFlow()

    /**
     * Passive fallback: a SharedPreferences change callback on the handle this
     * cache was built with (UI setters writing prefs directly, a raw-key backup
     * restore). Held as a val so the weak listener map cannot drop it (F-134).
     */
    val prefsListener: SharedPreferences.OnSharedPreferenceChangeListener =
        SharedPreferences.OnSharedPreferenceChangeListener { changed, key ->
            applyWrite(changed, key)
        }

    /**
     * [dv29-A4] Active delivery: the config field writers hand us the value they
     * just applied, together with **their** [SharedPreferences] handle. Built
     * here (not at the repository wiring site) so the "refresh from the writer's
     * handle" property lives next to [applyWrite] and is covered by
     * `BackgroundToggleCacheTest` — a one-line `applyWrite(prefs, key)` at the
     * call site would silently reintroduce the device stale-value shape.
     */
    val writeSink: PrefsWriteBus.Sink = PrefsWriteBus.Sink { written, key -> applyWrite(written, key) }

    /** In-app setter path (Settings switches): the writer already owns the state. */
    fun setTaskNotificationsEnabled(value: Boolean) {
        _taskNotificationsEnabled.value = value
    }

    fun setBackgroundOverlayEnabled(value: Boolean) {
        _backgroundOverlayEnabled.value = value
    }

    /**
     * Refresh from a write that went through [source] (a SharedPreferences
     * change listener or a [PrefsWriteBus] sink — both land here).
     *
     * Unrelated keys are ignored; re-setting an unchanged value is a no-op for
     * a StateFlow, so a spurious key match from another prefs file cannot
     * corrupt the state.
     */
    fun applyWrite(source: SharedPreferences, key: String?) {
        when (backgroundToggleForKey(key)) {
            BackgroundToggle.TaskNotifications ->
                _taskNotificationsEnabled.value =
                    source.getBoolean(
                        BackgroundSettingsRepository.KEY_TASK_NOTIFICATIONS,
                        BackgroundSettingsRepository.DEFAULT_TASK_NOTIFICATIONS,
                    )
            BackgroundToggle.BackgroundOverlay ->
                _backgroundOverlayEnabled.value =
                    source.getBoolean(
                        BackgroundSettingsRepository.KEY_BG_OVERLAY_ENABLED,
                        BackgroundSettingsRepository.DEFAULT_BG_OVERLAY_ENABLED,
                    )
            null -> Unit
        }
    }
}

/**
 * [dv29-A4] Which cached toggle a raw prefs key feeds — the pure decision behind
 * [BackgroundToggleCache.applyWrite], kept top-level so the mapping is testable
 * off-device.
 *
 * Keys come from the [BackgroundSettingsRepository] companion constants: a
 * second string copy here would be the same drift shape (F-238/F-239) that
 * companion exists to end.
 */
internal enum class BackgroundToggle { TaskNotifications, BackgroundOverlay }

internal fun backgroundToggleForKey(key: String?): BackgroundToggle? = when (key) {
    BackgroundSettingsRepository.KEY_TASK_NOTIFICATIONS -> BackgroundToggle.TaskNotifications
    BackgroundSettingsRepository.KEY_BG_OVERLAY_ENABLED -> BackgroundToggle.BackgroundOverlay
    else -> null
}

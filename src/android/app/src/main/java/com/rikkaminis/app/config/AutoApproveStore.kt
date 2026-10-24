package com.rikkaminis.app.config

import android.content.Context
import android.content.SharedPreferences
import androidx.annotation.VisibleForTesting
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * "Auto-approve" mode: the user's explicit opt-in to stop being asked for
 * every routine agent action.
 *
 * Three independent flags, all defaulting to OFF:
 *
 *  - [enabled]                     — the master switch. When ON, a
 *    `minis-config` write whose fields are all [ConfigRisk.NORMAL] is applied
 *    without showing the confirmation dialog, and agent tools whose
 *    permission level is `ASK_ONCE` behave as `BYPASS`.
 *  - [sensitiveEnabled]            — extends the skip to
 *    [ConfigRisk.SENSITIVE] fields (provider `apiKey` / `baseUrl` /
 *    `oauthToken` …). OFF by default on purpose: with it ON the agent can
 *    silently repoint a provider's credentials at another server, which is
 *    irreversible once the real key has been sent.
 *  - [privilegedToolsEnabled]      — lifts the two privileged automation
 *    CLIs (`a11y_cli`, `shizuku_cli`) from their `NOT_ALLOWED` default to
 *    `BYPASS`. Kept separate from the master switch because they drive other
 *    apps and read on-screen content.
 *
 * [ConfigRisk.DESTRUCTIVE] writes are never auto-approved, at any flag
 * combination — see [com.rikkaminis.app.config.confirm.ConfigAutoApprovePolicy].
 *
 * Security model (why this can't be turned on by the agent itself):
 *  - This object is deliberately NOT registered in `ConfigRegistry`, so
 *    `minis-config get/set` has no path that reaches these values.
 *  - The values live in the app's private SharedPreferences
 *    (`/data/data/<pkg>/shared_prefs`), which the PRoot guest cannot read or
 *    write — the same boundary that makes the confirmation gate meaningful in
 *    the first place.
 *
 * Mirrors the shape of [MinisConfigPermissionStore] (StateFlow + eagerly
 * persisted, with pre-init toggles queued rather than dropped).
 */
object AutoApproveStore {
    private const val PREFS = "auto_approve_prefs"

    @VisibleForTesting internal const val KEY_ENABLED = "enabled"
    @VisibleForTesting internal const val KEY_SENSITIVE = "sensitive_enabled"
    @VisibleForTesting internal const val KEY_PRIVILEGED = "privileged_tools_enabled"

    private const val DEFAULT_ENABLED = false
    private const val DEFAULT_SENSITIVE = false
    private const val DEFAULT_PRIVILEGED = false

    private var prefs: SharedPreferences? = null

    /**
     * Toggles that arrived before [init]/[install]. Keyed by preference name
     * so the three flags queue independently — a single "pending value" field
     * would let the last toggle before init overwrite the other two.
     */
    private val pending = HashMap<String, Boolean>()

    private val _enabled = MutableStateFlow(DEFAULT_ENABLED)
    val enabled: StateFlow<Boolean> = _enabled.asStateFlow()

    /** Hot-path read used by the offload bridge and the config write path. */
    val isEnabled: Boolean get() = _enabled.value

    private val _sensitiveEnabled = MutableStateFlow(DEFAULT_SENSITIVE)
    val sensitiveEnabled: StateFlow<Boolean> = _sensitiveEnabled.asStateFlow()

    val isSensitiveEnabled: Boolean get() = _sensitiveEnabled.value

    private val _privilegedToolsEnabled = MutableStateFlow(DEFAULT_PRIVILEGED)
    val privilegedToolsEnabled: StateFlow<Boolean> = _privilegedToolsEnabled.asStateFlow()

    val isPrivilegedToolsEnabled: Boolean get() = _privilegedToolsEnabled.value

    /** Call once early — typically from MinisApp.onCreate. Idempotent. */
    fun init(context: Context) {
        install(context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE))
    }

    /**
     * Test seam: same effect as [init] but without a Context. Kept `internal`
     * so the production surface stays the single [init] entry point.
     */
    @VisibleForTesting
    internal fun install(store: SharedPreferences) {
        if (prefs != null) return
        prefs = store
        if (pending.isNotEmpty()) {
            val editor = store.edit()
            for ((key, value) in pending) editor.putBoolean(key, value)
            editor.apply()
            pending.clear()
        }
        _enabled.value = store.getBoolean(KEY_ENABLED, DEFAULT_ENABLED)
        _sensitiveEnabled.value = store.getBoolean(KEY_SENSITIVE, DEFAULT_SENSITIVE)
        _privilegedToolsEnabled.value = store.getBoolean(KEY_PRIVILEGED, DEFAULT_PRIVILEGED)
    }

    /** UI toggle. Persists eagerly. */
    fun setEnabled(value: Boolean) {
        write(KEY_ENABLED, value)
        _enabled.value = value
    }

    fun setSensitiveEnabled(value: Boolean) {
        write(KEY_SENSITIVE, value)
        _sensitiveEnabled.value = value
    }

    fun setPrivilegedToolsEnabled(value: Boolean) {
        write(KEY_PRIVILEGED, value)
        _privilegedToolsEnabled.value = value
    }

    /**
     * Turning the master switch off also drops the two sub-flags: leaving
     * "sensitive" or "privileged" armed behind an OFF master would make the
     * next enable silently broader than the user last saw in the UI.
     */
    fun disableAll() {
        setEnabled(false)
        setSensitiveEnabled(false)
        setPrivilegedToolsEnabled(false)
    }

    private fun write(key: String, value: Boolean) {
        val store = prefs
        if (store == null) {
            pending[key] = value
        } else {
            store.edit().putBoolean(key, value).apply()
        }
    }
}

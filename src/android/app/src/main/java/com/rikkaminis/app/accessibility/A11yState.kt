package com.rikkaminis.app.accessibility

import android.content.Context
import android.provider.Settings

/**
 * [FIX-6 / F-243] Single source of truth for "is our accessibility service
 * actually able to receive events right now?".
 *
 * Two settings screens previously carried byte-identical private copies of this
 * check (`OffloadPermissionScreen.isA11yServiceEnabled` and
 * `SystemPermissionsScreen.isAccessibilityEnabled`), and **both** ignored the
 * system-level accessibility master switch. On Android the enabled-services
 * list can still name a service while accessibility is switched off entirely
 * (`Settings.Secure.ACCESSIBILITY_ENABLED == 0`), in which case the service is
 * bound but receives no events — so both screens could report "enabled" while
 * a11y-driven tools silently did nothing.
 *
 * Kept as an object next to [MinisAccessibilityService] so the two screens (and
 * any future caller) share one definition; the duplication is what let the two
 * copies drift from the intended semantics in the first place.
 */
object A11yState {

    /**
     * True only when accessibility is globally ON **and** our service is listed
     * as enabled.
     *
     * [Settings.Secure.ACCESSIBILITY_ENABLED] is a 0/1 flag that is absent on
     * some OEM builds; a missing value is treated as ON so the check degrades
     * to the pre-existing list-only behaviour rather than reporting "disabled"
     * on a device that never wrote the flag.
     *
     * Single source of truth for the *listing* half: the entry matching lives in
     * [AccessibilityRecoveryManager.isRevokedIn] — the same predicate the repair
     * path ([AccessibilityRecoveryManager.isGrantRevoked]) uses. This used to be
     * a local `split(':').any { it.equals("<pkg>/<fq class>") }`, which only
     * accepted the fully-qualified `pkg/pkg.Class` spelling while the framework
     * may normalize to `pkg/.Class` — so this screen could report "disabled" for
     * a healthy, connected service, and the two callers of this fact drifted
     * apart. Do NOT reintroduce a local match here.
     */
    fun isServiceEnabled(context: Context): Boolean {
        if (!isMasterSwitchOn(context)) return false
        val enabled = Settings.Secure.getString(
            context.contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ) ?: return false
        return !AccessibilityRecoveryManager.isRevokedIn(
            enabled,
            context.packageName,
            MinisAccessibilityService::class.java.name,
        )
    }

    /**
     * System-wide accessibility master switch. Absent (null) counts as ON —
     * see [isServiceEnabled].
     */
    fun isMasterSwitchOn(context: Context): Boolean {
        val raw = Settings.Secure.getString(
            context.contentResolver,
            Settings.Secure.ACCESSIBILITY_ENABLED,
        ) ?: return true
        return raw != "0"
    }
}

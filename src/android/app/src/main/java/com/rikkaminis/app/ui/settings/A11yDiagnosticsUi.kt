package com.rikkaminis.app.ui.settings

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.provider.Settings
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.BatteryAlert
import androidx.compose.material.icons.outlined.Build
import androidx.compose.material.icons.outlined.RestartAlt
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import com.rikkaminis.app.R
import com.rikkaminis.app.accessibility.A11yState
import com.rikkaminis.app.accessibility.AccessibilityRecoveryManager
import com.rikkaminis.app.accessibility.MinisAccessibilityService
import com.rikkaminis.app.power.PowerOptimizationManager
import kotlinx.coroutines.launch

/**
 * [dv15-B2 / deep-audit-1036 B-08] Shared accessibility diagnostics: the
 * observation that feeds [classifyA11yStatus], the two self-rescue cards, and
 * the system-settings intent they both fall back to.
 *
 * Before this file the *reachable* screen (`Routes.PERMISSIONS` →
 * [OffloadPermissionScreen]) carried only the `a11yRestricted` card, while the
 * two cards that matter when the grant really breaks — the force-stop repair
 * (`[T-android-a11y-force-stop-recovery]`) and the OEM keep-alive guidance
 * (`[T-android-a11y-miui-service-failure]`) — lived only in
 * [SystemPermissionsScreen], which nothing navigates to. A user hitting the
 * broken state saw "enabled" and no way out. Both screens now render the same
 * composables; neither keeps a private copy (the fork *was* the defect).
 */

/**
 * Reads the four observations [classifyA11yStatus] needs. Kept next to the
 * cards rather than inside [A11yStatus] so the decision itself stays free of
 * Android types and JVM-testable.
 */
internal fun a11yStatusOf(context: Context): A11yStatus = classifyA11yStatus(
    masterSwitchOn = A11yState.isMasterSwitchOn(context),
    listedInSettings = A11yState.isServiceEnabled(context),
    serviceConnected = MinisAccessibilityService.getInstance() != null,
    everGranted = AccessibilityRecoveryManager.hasEverBeenGranted(context),
)

/**
 * [T-android-restricted-settings] The one settings deeplink both screens use.
 * Was a byte-identical private copy in each of them; falls back to the settings
 * home when an OEM hides the accessibility page.
 */
internal fun openAccessibilitySettings(context: Context) {
    try {
        context.startActivity(
            Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
        )
    } catch (_: Throwable) {
        try {
            context.startActivity(
                Intent(Settings.ACTION_SETTINGS).apply {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                }
            )
        } catch (_: Throwable) {}
    }
}

/**
 * [T-android-a11y-force-stop-recovery] Repair affordance, shown ONLY when the
 * grant was previously held and is now missing from Settings.Secure — the
 * force-stop signature. Hidden in the healthy case and on a fresh install (see
 * [AccessibilityRecoveryManager.hasEverBeenGranted], which reaches this card
 * through [classifyA11yStatus]) so the screen stays clean.
 *
 * [shizukuReady] is passed in rather than collected here so each screen keeps
 * its own readiness source (the live one reads the reactive snapshot).
 */
@Composable
internal fun A11yRevokedRepairSection(revoked: Boolean, shizukuReady: Boolean) {
    if (!revoked) return
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var repairing by remember { mutableStateOf(false) }
    var repairFailed by remember { mutableStateOf(false) }
    SettingsSection(
        header = stringResource(R.string.a11y_repair_section_header),
        footer = stringResource(
            if (shizukuReady) R.string.a11y_repair_footer_shizuku
            else R.string.a11y_repair_footer_manual
        ),
    ) {
        SettingsRow(
            icon = Icons.Outlined.Build,
            iconColor = Color(0xFFFF3B30),
            title = stringResource(
                if (shizukuReady) R.string.a11y_repair_row_shizuku
                else R.string.a11y_repair_row_manual
            ),
            subtitle = when {
                repairing -> stringResource(R.string.a11y_repair_in_progress)
                repairFailed -> stringResource(R.string.a11y_repair_failed)
                else -> stringResource(R.string.a11y_repair_row_sub)
            },
            onClick = {
                if (!shizukuReady) {
                    openAccessibilitySettings(context)
                    return@SettingsRow
                }
                if (repairing) return@SettingsRow
                repairing = true
                repairFailed = false
                scope.launch {
                    val ok = AccessibilityRecoveryManager
                        .repairWithShizuku(context)
                    repairing = false
                    repairFailed = !ok
                    // On success the calling screen's polling loop clears
                    // `revoked` within a second and this whole section
                    // disappears on its own.
                }
            },
            showDivider = false,
        )
    }
}

/**
 * [T-android-a11y-miui-service-failure] OEM keep-alive guidance. Only shown
 * when the service is degraded ("此服务出现故障" — enabled in Settings but the
 * process was killed by the ROM) AND the vendor is one known to enforce
 * autostart on top of stock Android. Routes the two canonical remediations
 * through the existing [PowerOptimizationManager] (which already catches a
 * missing Activity and falls back). Stays hidden on Pixel / stock Android
 * (Vendor.OTHER).
 */
@Composable
internal fun A11yOemKeepAliveSection(degraded: Boolean) {
    if (!degraded) return
    val context = LocalContext.current
    val activity = context as? Activity ?: return
    if (!PowerOptimizationManager.needsOemAutostartGuidance()) return
    val vendor = PowerOptimizationManager.Vendor.current().displayName
    SettingsSection(
        header = stringResource(R.string.system_permissions_a11y_oem_header),
        footer = stringResource(R.string.system_permissions_a11y_oem_footer, vendor),
    ) {
        SettingsRow(
            icon = Icons.Outlined.RestartAlt,
            iconColor = Color(0xFFFF9500),
            title = stringResource(R.string.system_permissions_a11y_oem_autostart),
            subtitle = stringResource(R.string.system_permissions_a11y_oem_autostart_sub),
            onClick = {
                if (!PowerOptimizationManager.openOemAutostartSettings(activity)) {
                    PowerOptimizationManager.openAppDetailsSettings(activity)
                }
            },
        )
        SettingsRow(
            icon = Icons.Outlined.BatteryAlert,
            iconColor = Color(0xFFFF9500),
            title = stringResource(R.string.system_permissions_a11y_oem_battery),
            subtitle = stringResource(R.string.system_permissions_a11y_oem_battery_sub),
            onClick = {
                if (!PowerOptimizationManager.requestBatteryOptimizationExemption(activity)) {
                    PowerOptimizationManager.openAppDetailsSettings(activity)
                }
            },
            showDivider = false,
        )
    }
}

package com.rikkaminis.app.util

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import androidx.core.net.toUri

private const val TAG = "PackageLaunchUtils"

/**
 * [T-reverse-workbench] Package-manager helpers for the reverse-engineering
 * workbench card, ported from the 玄星/XuanXing 二开 `ContextUtil`.
 *
 * The card lets the user launch the three local RE backends (MT 管理器 / SOMCP /
 * ProxyPin) with one tap, or jump to a store if the app isn't installed.
 */

/** Whether [packageName] is installed. Never throws (a restricted/renamed
 *  package simply reads as "not installed"). */
fun Context.isPackageInstalled(packageName: String): Boolean = runCatching {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        packageManager.getPackageInfo(packageName, PackageManager.PackageInfoFlags.of(0))
    } else {
        @Suppress("DEPRECATION")
        packageManager.getPackageInfo(packageName, 0)
    }
    true
}.getOrDefault(false)

/**
 * Launch the app for [packageName]; when it isn't installed, fall back to the
 * store listing (`market://`, then the web Play page).
 *
 * @return true when an installed app was launched; false when we fell through to
 *   a store listing (or nothing resolved).
 */
fun Context.launchOrOpenMarket(packageName: String): Boolean {
    packageManager.getLaunchIntentForPackage(packageName)?.let { launch ->
        runCatching {
            startActivity(launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return true
        }.onFailure { Log.w(TAG, "launch failed for $packageName: ${it.message}") }
    }
    runCatching {
        startActivity(
            Intent(Intent.ACTION_VIEW, "market://details?id=$packageName".toUri())
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }.onFailure {
        runCatching {
            startActivity(
                Intent(
                    Intent.ACTION_VIEW,
                    "https://play.google.com/store/apps/details?id=$packageName".toUri()
                ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        }.onFailure { e -> Log.w(TAG, "no store for $packageName: ${e.message}") }
    }
    return false
}
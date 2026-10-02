package com.rikkaminis.app.util

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.Settings

/**
 * [feat/advanced-features] 玄星二开：外部存储「全部文件访问」辅助。
 *
 * 逆向流程常要读写 /sdcard、/storage 下别的 App 的 APK / SO（也包括 MT 管理器的
 * Android/data 分析缓存）。Android 11+ 走 MANAGE_EXTERNAL_STORAGE 特殊权限，只能
 * 从系统设置页授予，没有运行时弹窗 —— 所以这里只做「检测 + 跳转」。
 *
 * 判定口径与 [com.rikkaminis.app.ui.settings.MountedFoldersScreen] 保持一致：
 * Android 11+ 看 [Environment.isExternalStorageManager]；Android 10 看旧版
 * READ_EXTERNAL_STORAGE；更低版本不需要授权。
 */
fun hasAllFilesAccess(context: Context): Boolean = when {
    Build.VERSION.SDK_INT >= Build.VERSION_CODES.R -> Environment.isExternalStorageManager()
    Build.VERSION.SDK_INT == Build.VERSION_CODES.Q ->
        context.checkSelfPermission(android.Manifest.permission.READ_EXTERNAL_STORAGE) ==
            PackageManager.PERMISSION_GRANTED
    else -> true
}

/**
 * 跳到系统的「所有文件访问」授权页。
 * 部分 OEM（HarmonyOS / EMUI）没有 per-app 页面，回退到应用详情页。
 * Android 10 及以下没有该页面，直接忽略（由调用方走旧版运行时权限）。
 */
fun openAllFilesAccessSettings(context: Context) {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return
    runCatching {
        context.startActivity(
            Intent(
                Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                Uri.parse("package:${context.packageName}"),
            ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
    }.onFailure {
        runCatching {
            context.startActivity(
                Intent(
                    Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:${context.packageName}"),
                ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
        }
    }
}
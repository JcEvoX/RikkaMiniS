package com.rikkaminis.app.util

import android.os.Environment
import java.io.File

/**
 * [feat/advanced-features] 玄星二开：MT 管理器 MCP 分析缓存。
 *
 * MT 管理器通过 MCP 解包 APK 时会把中间产物写到 Android/data/<MT 包名>/mcp 下，
 * 分析次数多了会堆到几个 GB。这些只是分析用的临时文件，删掉不影响 MT 管理器本身，
 * 下次分析会自动重建。
 *
 * 覆盖 MT 正式版与 Canary 版两个包名。读该目录需要「全部文件访问」
 * (MANAGE_EXTERNAL_STORAGE)；无权限时返回 0 / 清理为空操作，由 UI 引导授权。
 */
private val MT_MCP_CACHE_DIRS = listOf(
    "Android/data/bin.mt.plus/mcp",
    "Android/data/bin.mt.plus.canary/mcp",
)

/** 统计 MT 分析缓存占用的字节数（无权限 / 目录不存在时计 0）。 */
fun getMtMcpCacheSize(): Long {
    val root = Environment.getExternalStorageDirectory() ?: return 0L
    var total = 0L
    for (rel in MT_MCP_CACHE_DIRS) {
        val dir = File(root, rel)
        if (dir.isDirectory) {
            total += runCatching {
                dir.walkBottomUp().filter { it.isFile }.sumOf { it.length() }
            }.getOrDefault(0L)
        }
    }
    return total
}

/**
 * 清理 MT 分析缓存：删除 mcp 目录下的内容，保留 mcp 目录本身。
 * 返回清理前统计到的占用（近似释放量；含删不掉的项时略有高估）。
 * 需先有「全部文件访问」。
 */
fun clearMtMcpCache(): Long {
    val root = Environment.getExternalStorageDirectory() ?: return 0L
    val freed = getMtMcpCacheSize()
    for (rel in MT_MCP_CACHE_DIRS) {
        val dir = File(root, rel)
        if (dir.isDirectory) {
            runCatching {
                dir.listFiles()?.forEach { child ->
                    if (child.isDirectory) child.deleteRecursively() else child.delete()
                }
            }
        }
    }
    return freed
}
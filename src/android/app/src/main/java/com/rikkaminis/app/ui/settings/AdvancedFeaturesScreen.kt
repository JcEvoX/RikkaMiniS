package com.rikkaminis.app.ui.settings

import android.widget.Toast
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import com.rikkaminis.app.R
import com.rikkaminis.app.data.AgentRuntimeLimitsPrefs
import com.rikkaminis.app.util.clearMtMcpCache
import com.rikkaminis.app.util.getMtMcpCacheSize
import com.rikkaminis.app.util.hasAllFilesAccess
import com.rikkaminis.app.util.openAllFilesAccessSettings
import java.util.Locale
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * [feat/advanced-features] 玄星二开「高级功能」页。
 *
 * 集中放置那些"会改变智能体行为、默认关闭、必须用户显式打开"的开关，与 Runtime Limits
 * （纯数值调参）分开，避免把有副作用的主动行为混进调参页。
 *
 * 搬入项：
 *   1. 持续工作（continuous work）—— 主动续轮。
 *   2. 存储：全部文件访问授权 + MT 分析缓存清理（逆向流程的外部存储前置条件）。
 *
 * 与 RuntimeLimitsScreen 的批量 Save 不同：本页只有少量开关，改动即持久化（写 prefs +
 * 刷新 AgentRuntimeLimitsPrefs 的进程内缓存），无需 Save 按钮，也就不存在"旧快照回滚"
 * 的问题。
 */
@Composable
fun AdvancedFeaturesScreen(onBack: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var continuousWorkEnabled by remember {
        mutableStateOf(AgentRuntimeLimitsPrefs.continuousWorkEnabled())
    }
    var continuousWorkMaxRounds by remember {
        mutableStateOf(AgentRuntimeLimitsPrefs.continuousWorkMaxRounds())
    }

    // ── 存储 ────────────────────────────────────────────────────────────────
    var hasAllFiles by remember { mutableStateOf(hasAllFilesAccess(context)) }
    // -1 = 统计中；其余为字节数。
    var mtCacheSize by remember { mutableStateOf(-1L) }
    var showClearConfirm by remember { mutableStateOf(false) }

    // 进页面时异步统计一次 MT 缓存占用。
    LaunchedEffect(Unit) {
        mtCacheSize = withContext(Dispatchers.IO) {
            runCatching { getMtMcpCacheSize() }.getOrDefault(0L)
        }
    }
    // 从系统设置授权页返回后重新判定权限，避免开关停留在旧状态。
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                hasAllFiles = hasAllFilesAccess(context)
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    if (showClearConfirm) {
        AlertDialog(
            onDismissRequest = { showClearConfirm = false },
            title = { Text(stringResource(R.string.advanced_features_mt_cache_clear_title)) },
            text = {
                Text(
                    stringResource(
                        R.string.advanced_features_mt_cache_clear_msg,
                        formatCacheSize(mtCacheSize),
                    )
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    showClearConfirm = false
                    scope.launch {
                        val freed = withContext(Dispatchers.IO) {
                            runCatching { clearMtMcpCache() }.getOrDefault(0L)
                        }
                        mtCacheSize = withContext(Dispatchers.IO) {
                            runCatching { getMtMcpCacheSize() }.getOrDefault(0L)
                        }
                        val msg = if (freed > 0) {
                            context.getString(
                                R.string.advanced_features_mt_cache_cleared,
                                formatBytes(freed),
                            )
                        } else {
                            context.getString(R.string.advanced_features_mt_cache_nothing)
                        }
                        Toast.makeText(context, msg, Toast.LENGTH_SHORT).show()
                    }
                }) {
                    Text(stringResource(R.string.advanced_features_mt_cache_clear_confirm))
                }
            },
            dismissButton = {
                TextButton(onClick = { showClearConfirm = false }) {
                    Text(stringResource(R.string.common_cancel))
                }
            },
        )
    }

    SettingsScaffold(
        title = stringResource(R.string.advanced_features_title),
        onBack = onBack,
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(bottom = 24.dp),
        ) {
            LimitsSectionCard(title = stringResource(R.string.advanced_features_section_work)) {
                LimitsSwitchRow(
                    title = stringResource(R.string.advanced_features_continuous_work),
                    subtitle = stringResource(R.string.advanced_features_continuous_work_desc),
                    checked = continuousWorkEnabled,
                    onCheckedChange = {
                        continuousWorkEnabled = it
                        AgentRuntimeLimitsPrefs.save(context, continuousWorkEnabled = it)
                    },
                    showDivider = true,
                )
                LimitsSliderRow(
                    title = stringResource(R.string.advanced_features_continuous_work_rounds),
                    subtitle = stringResource(R.string.advanced_features_continuous_work_rounds_desc),
                    value = continuousWorkMaxRounds,
                    min = AgentRuntimeLimitsPrefs.CONTINUOUS_WORK_ROUNDS_MIN,
                    max = AgentRuntimeLimitsPrefs.CONTINUOUS_WORK_ROUNDS_MAX,
                    onCommit = {
                        continuousWorkMaxRounds = it
                        AgentRuntimeLimitsPrefs.save(context, continuousWorkMaxRounds = it)
                    },
                    showDivider = false,
                )
            }
            LimitsSectionFooter(stringResource(R.string.advanced_features_work_footer))

            LimitsSectionCard(title = stringResource(R.string.advanced_features_section_storage)) {
                LimitsSwitchRow(
                    title = stringResource(R.string.advanced_features_all_files_access),
                    subtitle = stringResource(
                        if (hasAllFiles) {
                            R.string.advanced_features_all_files_access_granted
                        } else {
                            R.string.advanced_features_all_files_access_desc
                        }
                    ),
                    checked = hasAllFiles,
                    onCheckedChange = { on ->
                        // 这是系统特殊权限，只能在设置页授予，没有运行时弹窗 —— 打开开关就跳转。
                        if (on && !hasAllFilesAccess(context)) {
                            openAllFilesAccessSettings(context)
                        }
                    },
                    showDivider = true,
                )
                LimitsActionRow(
                    title = stringResource(R.string.advanced_features_mt_cache),
                    subtitle = if (mtCacheSize < 0) {
                        stringResource(R.string.advanced_features_mt_cache_scanning)
                    } else {
                        stringResource(
                            R.string.advanced_features_mt_cache_desc,
                            formatBytes(mtCacheSize),
                        )
                    },
                    actionText = stringResource(R.string.advanced_features_mt_cache_clear_confirm),
                    onClick = {
                        if (!hasAllFilesAccess(context)) {
                            openAllFilesAccessSettings(context)
                        } else {
                            showClearConfirm = true
                        }
                    },
                    showDivider = false,
                )
            }
            LimitsSectionFooter(stringResource(R.string.advanced_features_storage_footer))
        }
    }
}

/** -1 表示仍在统计中。 */
private fun formatCacheSize(size: Long): String =
    if (size < 0) "…" else formatBytes(size)

/** 紧凑的人类可读大小，用于设置页副标题。 */
private fun formatBytes(bytes: Long): String = when {
    bytes >= 1024L * 1024L * 1024L ->
        String.format(Locale.getDefault(), "%.2f GB", bytes / 1024.0 / 1024.0 / 1024.0)
    bytes >= 1024L * 1024L ->
        String.format(Locale.getDefault(), "%.1f MB", bytes / 1024.0 / 1024.0)
    bytes >= 1024L ->
        String.format(Locale.getDefault(), "%.0f KB", bytes / 1024.0)
    else -> "$bytes B"
}
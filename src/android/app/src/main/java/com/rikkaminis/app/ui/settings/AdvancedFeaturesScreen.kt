package com.rikkaminis.app.ui.settings

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.rikkaminis.app.R
import com.rikkaminis.app.data.AgentRuntimeLimitsPrefs

/**
 * [feat/advanced-features] 玄星二开「高级功能」页。
 *
 * 集中放置那些"会改变智能体行为、默认关闭、必须用户显式打开"的开关，与 Runtime Limits
 * （纯数值调参）分开，避免把有副作用的主动行为混进调参页。
 *
 * 首个搬入项：持续工作（continuous work）。后续的 AI 悬浮球等开关也挂在这里。
 *
 * 与 RuntimeLimitsScreen 的批量 Save 不同：本页只有少量开关，改动即持久化（写 prefs +
 * 刷新 AgentRuntimeLimitsPrefs 的进程内缓存），无需 Save 按钮，也就不存在"旧快照回滚"
 * 的问题。
 */
@Composable
fun AdvancedFeaturesScreen(onBack: () -> Unit) {
    val context = LocalContext.current

    var continuousWorkEnabled by remember {
        mutableStateOf(AgentRuntimeLimitsPrefs.continuousWorkEnabled())
    }
    var continuousWorkMaxRounds by remember {
        mutableStateOf(AgentRuntimeLimitsPrefs.continuousWorkMaxRounds())
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
        }
    }
}
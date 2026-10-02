package com.rikkaminis.app.ui.chat

/**
 * [feat/continuous-work] 玄星二开「持续工作」端口。
 *
 * 模型主动收尾（本轮没有工具调用）但这一轮确实用工具干过活、且回复里没有完成标志时，
 * 自动往历史里追加一条"继续"提示让它接着做，最多 N 轮后用硬上限兜底，避免无限烧 token。
 *
 * 与 [VerificationStopPolicy] / [ToolCallResiduePolicy] 同款：纯判定 + 文本构造，不依赖
 * Android，JVM 可测。
 */
object ContinuousWorkPolicy {

    /**
     * 完成标志：模型明确表示收工就不再自动续。
     * 取值沿用玄星二开原版（中英混排，大小写不敏感）。
     */
    private val DONE_MARKERS = listOf(
        "[任务完成]",
        "[DONE]",
        "任务已完成",
        "全部完成",
        "已完成所有",
        "没有更多",
        "无需继续",
    )

    /** 极短回复多半是异常收尾，不值得续。 */
    private const val MIN_REPLY_LENGTH = 4

    fun looksDone(text: String): Boolean =
        DONE_MARKERS.any { text.contains(it, ignoreCase = true) }

    /**
     * 是否应该自动续一轮。
     *
     * @param ranToolThisRun 本次 run 是否真的调用过工具。纯聊天/打招呼/纯问答不续 ——
     *   否则会像 bug 一样在欢迎语后面乱发"继续"（玄星原版的核心判据）。
     * @param finalText 本轮模型可见文本。
     * @param roundsUsed 本 run 已经自动续过的轮数。
     * @param maxRounds 上限；小于等于 0 视为关闭。
     * @param terminalError 已经给用户报过失败（length wall / 确定性空回复 / 复发中止）的
     *   run 不允许被续命复活 —— 与 verify nudge / residue refill 沿用同一门禁。
     */
    fun shouldContinue(
        ranToolThisRun: Boolean,
        finalText: String,
        roundsUsed: Int,
        maxRounds: Int,
        terminalError: Boolean,
    ): Boolean {
        if (terminalError) return false
        if (maxRounds <= 0) return false
        if (roundsUsed >= maxRounds) return false
        if (!ranToolThisRun) return false
        val text = finalText.trim()
        if (text.length < MIN_REPLY_LENGTH) return false
        if (looksDone(text)) return false
        return true
    }

    /**
     * 注入历史的 synthetic USER 提示。与引擎内其它 reminder（length wall / EOF stub /
     * verify nudge）保持一致，统一用英文 system-reminder 措辞。
     */
    fun continuePrompt(): String =
        "<system-reminder>Auto-continue: this run has already executed tools and the task " +
            "may not be finished. Continue with the remaining work now — issue the next tool " +
            "call(s) or give the concrete final answer. Do not reply with only a plan or a " +
            "status summary. If ALL work is genuinely complete, end your reply with [DONE] so " +
            "no further auto-continue is triggered.</system-reminder>"
}
package com.rikkaminis.app.conversation

/**
 * [fix/prewindow-budget-1015] 校准样本窗口的纯逻辑核 —— 无 Android 依赖，
 * JVM 单测直接覆盖（AuditP1ContextBudgetTest）。
 *
 * 语义（2026-10-15 T321 现场校准过的设计决定）：
 *  - 现场证据（GLM 直连 bodyLen 4.81MB 报 920K vs 中转同 payload 报 191K，
 *    4.8×）表明各端点记账差异是**系统性**的：tokenizer、消息模板展开、中转
 *    包装都会整体缩放。校准的意义正是把这个系统性比值学进来，所以比值落在
 *    [DISTORTION_BAND] 带内的样本**一律收**（哪怕 3×）——那不是垃圾，那是
 *    端点的真实口径。
 *  - [DISTORTION_BAND] 带外才算失真：带外的比值（>3× 或 <⅓×）大概率是
 *    单次异常（比如把别的会话的 usage 记错行）而非系统口径，拒收并标记，
 *    防止污染窗口。
 *  - 系数取窗口中位数：单次离群（缓存命中突降、异常截断）不拖偏系数。
 */
class CalibrationStore(
    private val windowSize: Int = DEFAULT_WINDOW_SIZE,
    private val minSamples: Int = DEFAULT_MIN_SAMPLES,
    private val distortionBand: Double = DEFAULT_DISTORTION_BAND,
) {
    private val windows = HashMap<String, ArrayDeque<Double>>()

    companion object {
        /** 滑动窗口容量。太小易被单次异常污染，太大学习太慢。 */
        const val DEFAULT_WINDOW_SIZE = 8

        /** 学习就绪所需最少样本数。 */
        const val DEFAULT_MIN_SAMPLES = 3

        /** provider 上报 / 本地估算 落在此带外判失真（本现场 4.8× 会被拦）。 */
        const val DEFAULT_DISTORTION_BAND = 3.0
    }

    enum class ObserveResult { IGNORED, DISTORTED, LEARNING, READY }

    fun observe(key: String, providerUsage: Long, localEstimate: Long): ObserveResult {
        if (providerUsage <= 0 || localEstimate <= 0) return ObserveResult.IGNORED
        val ratio = providerUsage.toDouble() / localEstimate.toDouble()
        if (ratio < 1.0 / distortionBand || ratio > distortionBand) return ObserveResult.DISTORTED
        val dq = synchronized(windows) { windows.getOrPut(key) { ArrayDeque() } }
        synchronized(dq) {
            dq.addLast(ratio)
            while (dq.size > windowSize) dq.removeFirst()
        }
        return if (isReady(key)) ObserveResult.READY else ObserveResult.LEARNING
    }

    /** 当前校准系数；样本不足返回 1.0（保守：宁可早压缩，不可爆窗口）。 */
    fun ratio(key: String): Double {
        val dq = synchronized(windows) { windows[key] } ?: return 1.0
        val sorted = synchronized(dq) { dq.sorted() }
        if (sorted.size < minSamples) return 1.0
        val mid = sorted.size / 2
        return if (sorted.size % 2 == 1) sorted[mid]
        else (sorted[mid - 1] + sorted[mid]) / 2.0
    }

    fun isReady(key: String): Boolean {
        val dq = synchronized(windows) { windows[key] } ?: return false
        return synchronized(dq) { dq.size >= minSamples }
    }

    fun sampleCount(key: String): Int {
        val dq = synchronized(windows) { windows[key] } ?: return 0
        return synchronized(dq) { dq.size }
    }

    fun snapshot(): Map<String, List<Double>> =
        synchronized(windows) { windows.mapValues { (_, dq) -> synchronized(dq) { dq.toList() } } }

    fun restore(data: Map<String, List<Double>>) {
        synchronized(windows) {
            windows.clear()
            for ((k, v) in data) windows[k] = ArrayDeque(v.takeLast(windowSize))
        }
    }

    fun clear() = synchronized(windows) { windows.clear() }
}

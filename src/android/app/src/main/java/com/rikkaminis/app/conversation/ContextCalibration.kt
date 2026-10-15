package com.rikkaminis.app.conversation

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONObject

/**
 * [fix/prewindow-budget-1015] per-(providerType, baseUrl, model) 的
 * provider-usage / local-estimate 校准系数。
 *
 * 动机：同一份 payload，deepseek 中转（127.0.0.1:8788 本地缓存代理）与 GLM
 * 直连对 provider 上报差 4.8×（191,856 vs 920,577）。中转包装、各家
 * tokenizer、消息模板展开都系统性改变"usage 与本地估算的比值"。一个全局
 * 系数无法同时贴合两个端点，所以校准 key 刻意带 baseUrl —— 用户改一次中
 * 转地址，校准作废重学的代价（一段保守期）远小于新旧端点混用同一个系数的
 * 代价（本次事故）。
 *
 * 数据流：
 *   1. 每次成功 usage 回包，调用方在 [observe] 里带上一份
 *      [ContextEstimator] 对同一 wire 请求的本地估算；
 *   2. 偏差超出 [DISTORTION_BAND] 的读数判失真：不进样本、不作校准、不进
 *      决策（防止把别家的口供学成本地系数）；
 *   3. 合格样本进滑动窗口（最多 [WINDOW_SIZE] 个），系数 = 样本中位数；
 *   4. 系数持久化到 SharedPreferences，进程冷启动即生效。
 *
 * 未校准（样本不足）时 [ratio] 返回 1.0：宁可让本地估算偏保守（早压缩），
 * 不可偏乐观（爆窗口）。
 */
object ContextCalibration {

    /** 滑动窗口容量。太小易被单次异常污染，太大学习太慢。 */
    const val WINDOW_SIZE = 8

    /** provider 上报 / 本地估算 落在此带外判失真（本现场 4.8× 会被拦）。 */
    const val DISTORTION_BAND = 3.0

    /** 学习就绪所需最少样本数。 */
    const val MIN_SAMPLES = 3

    private const val PREFS = "minis_context_calibration"
    private const val KEY = "calibration_v1"

    private data class Entry(val samples: ArrayDeque<Double>)

    // 进程内缓存；ContextCalibration 只在主进程使用（modelservice worker 不
    // 做预算决策），prime 一次即可。volatile 保证跨线程可见。
    @Volatile
    private var store: HashMap<String, ArrayDeque<Double>>? = null

    @Volatile
    private var prefs: SharedPreferences? = null

    /** App 启动时调用一次（MinisApp.onCreate）。 */
    fun prime(context: Context) {
        val p = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        prefs = p
        val restored = HashMap<String, ArrayDeque<Double>>()
        val obj = runCatching { JSONObject(p.getString(KEY, "{}") ?: "{}") }.getOrNull()
        obj?.let { json ->
            val keys = json.keys()
            while (keys.hasNext()) {
                val k = keys.next()
                val arr = json.optJSONArray(k) ?: continue
                val dq = ArrayDeque<Double>()
                for (i in 0 until arr.length()) {
                    val v = arr.optDouble(i, Double.NaN)
                    if (!v.isNaN()) dq.addLast(v)
                }
                if (dq.isNotEmpty()) restored[k] = dq
            }
        }
        store = restored
    }

    /** 校准 key：providerType + baseUrl + model id。 */
    fun keyFor(providerType: String, baseUrl: String, modelId: String): String =
        "${providerType}|${baseUrl.trimEnd('/')}|$modelId"

    /**
     * 喂入一次成功读数。返回本次判定：
     * DISTORTED（不进样本）/ LEARNING（进了样本但未就绪）/ READY（就绪）。
     */
    fun observe(key: String, providerUsage: Long, localEstimate: Long): ObserveResult {
        if (providerUsage <= 0 || localEstimate <= 0) return ObserveResult.IGNORED
        val ratio = providerUsage.toDouble() / localEstimate.toDouble()
        if (ratio < 1.0 / DISTORTION_BAND || ratio > DISTORTION_BAND) {
            return ObserveResult.DISTORTED
        }
        val s = store ?: return ObserveResult.IGNORED
        val dq = synchronized(s) { s.getOrPut(key) { ArrayDeque() } }
        synchronized(dq) {
            dq.addLast(ratio)
            while (dq.size > WINDOW_SIZE) dq.removeFirst()
        }
        persist()
        return if (isReady(key)) ObserveResult.READY else ObserveResult.LEARNING
    }

    /** 当前校准系数；样本不足或未 prime 时 1.0（保守）。 */
    fun ratio(key: String): Double {
        val s = store ?: return 1.0
        val dq = synchronized(s) { s[key] } ?: return 1.0
        if (dq.size < MIN_SAMPLES) return 1.0
        val sorted = synchronized(dq) { dq.sorted() }
        val mid = sorted.size / 2
        return if (sorted.size % 2 == 1) sorted[mid]
        else (sorted[mid - 1] + sorted[mid]) / 2.0
    }

    /** 校准是否就绪（决定 UI 是否标注"本地估算"）。 */
    fun isReady(key: String): Boolean {
        val s = store ?: return false
        val dq = synchronized(s) { s[key] } ?: return false
        return synchronized(dq) { dq.size >= MIN_SAMPLES }
    }

    /** 样本数（面板/诊断用）。 */
    fun sampleCount(key: String): Int {
        val s = store ?: return 0
        val dq = synchronized(s) { s[key] } ?: return 0
        return synchronized(dq) { dq.size }
    }

    private fun persist() {
        val s = store ?: return
        val p = prefs ?: return
        val json = JSONObject()
        synchronized(s) {
            for ((k, dq) in s) {
                val arr = org.json.JSONArray()
                synchronized(dq) { dq.forEach { arr.put(it) } }
                json.put(k, arr)
            }
        }
        p.edit().putString(KEY, json.toString()).apply()
    }

    /** 测试与换端点后强制重学。 */
    fun resetForTest() {
        store = HashMap()
        prefs?.edit()?.remove(KEY)?.apply()
    }

    enum class ObserveResult { IGNORED, DISTORTED, LEARNING, READY }
}

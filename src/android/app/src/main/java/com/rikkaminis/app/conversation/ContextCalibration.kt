package com.rikkaminis.app.conversation

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject

/**
 * [fix/prewindow-budget-1015] 校准的 Android 持久化壳。样本窗口逻辑与常量
 * 单一事实源在 [CalibrationStore]（纯 Kotlin 可 JVM 测试），本对象只负责：
 *  - 进程内单例接线（引擎循环是唯一 observe 写入方，主进程使用）；
 *  - SharedPreferences 持久化（key 前缀 v1，结构变更整键作废重学）。
 *
 * 校准 key 见 [keyFor]：providerType + baseUrl + model。带 baseUrl 是刻意的
 * ——同一模型经中转（127.0.0.1:8788 缓存代理）与直连的记账口径差 4.8×
 * （T321 现场），key 不带端点维度就无法区分；用户改一次中转地址校准作废
 * 重学的代价（一段保守期）远小于新旧端点混用一个系数的代价（本次事故）。
 */
object ContextCalibration {

    const val WINDOW_SIZE = CalibrationStore.DEFAULT_WINDOW_SIZE
    const val MIN_SAMPLES = CalibrationStore.DEFAULT_MIN_SAMPLES
    const val DISTORTION_BAND = CalibrationStore.DEFAULT_DISTORTION_BAND

    private const val PREFS = "minis_context_calibration"
    private const val KEY = "calibration_v1"

    @Volatile
    private var store: CalibrationStore? = null

    @Volatile
    private var prefs: SharedPreferences? = null

    /** App 启动时调用一次（MinisApp.onCreate，主进程与 worker 都调）。 */
    fun prime(context: Context) {
        val p = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        prefs = p
        val s = CalibrationStore()
        val obj = runCatching { JSONObject(p.getString(KEY, "{}") ?: "{}") }.getOrNull()
        obj?.let { json ->
            val data = HashMap<String, List<Double>>()
            val keys = json.keys()
            while (keys.hasNext()) {
                val k = keys.next()
                val arr = json.optJSONArray(k) ?: continue
                val list = ArrayList<Double>(arr.length())
                for (i in 0 until arr.length()) {
                    val v = arr.optDouble(i, Double.NaN)
                    if (!v.isNaN()) list.add(v)
                }
                if (list.isNotEmpty()) data[k] = list
            }
            s.restore(data)
        }
        store = s
    }

    fun keyFor(providerType: String, baseUrl: String, modelId: String): String =
        "$providerType|${baseUrl.trimEnd('/')}|$modelId"

    fun observe(key: String, providerUsage: Long, localEstimate: Long): CalibrationStore.ObserveResult {
        val s = store ?: return CalibrationStore.ObserveResult.IGNORED
        val result = s.observe(key, providerUsage, localEstimate)
        if (result != CalibrationStore.ObserveResult.IGNORED &&
            result != CalibrationStore.ObserveResult.DISTORTED
        ) persist()
        return result
    }

    fun ratio(key: String): Double = store?.ratio(key) ?: 1.0
    fun isReady(key: String): Boolean = store?.isReady(key) ?: false
    fun sampleCount(key: String): Int = store?.sampleCount(key) ?: 0

    private fun persist() {
        val s = store ?: return
        val p = prefs ?: return
        val json = JSONObject()
        for ((k, samples) in s.snapshot()) {
            val arr = JSONArray()
            samples.forEach { arr.put(it) }
            json.put(k, arr)
        }
        p.edit().putString(KEY, json.toString()).apply()
    }

    /** 测试与诊断用：强制重学。 */
    fun resetForTest() {
        store?.clear()
        prefs?.edit()?.remove(KEY)?.apply()
    }
}

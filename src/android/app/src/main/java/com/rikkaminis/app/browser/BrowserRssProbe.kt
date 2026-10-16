package com.rikkaminis.app.browser

import android.util.Log
import java.io.File
import java.util.concurrent.ConcurrentHashMap

/**
 * 纯观测打点：记录每个 browser_use action 执行前后的主进程 VmRSS 增量。
 *
 * 背景（P4，2026-08-20）：`browser_use` 走 [BrowserTabPool.execute]（主进程
 * WebView 池），**不经** `NativeOffloadServer.handleClient`，所以 sandbox 侧的
 * [com.rikkaminis.app.sandbox.OffloadRssProbe] 覆盖不到它。WebView 渲染进程是
 * 主进程 native/mmap 之外的另一大内存来源（每 tab 50–100MB，Chromium 独立
 * renderer），此前它一直是「泄漏涨在哪个 handler」的最后一块盲区。
 *
 * 本类与 OffloadRssProbe 同构：零副作用、按 action 名归因、可 JVM 单测。
 * 打点只是旁路观测，与 [com.rikkaminis.app.service.MemoryPressureGate] 的准入
 * 逻辑无关——绝不改变 action 的执行路径或结果。
 */
object BrowserRssProbe {
    private const val TAG = "BrowserRssProbe"

    /** 单 action 累计正 delta 超过该值即判定「可疑泄漏源」并 WARN 一次。 */
    private const val LEAK_SUSPECT_KB = 1_024L * 1024L  // 1 GiB 累计

    // [fix/da1019-rssprobe-lock-scope] private → internal：决策纯函数
    // [applyAndDecideLocked] 的签名要暴露它，JVM 并发装置也要直接驱动该函数
    // （对齐 OffloadRssProbe 的同款修复形态）。
    internal data class Stats(
        var count: Int = 0,
        var totalDeltaKb: Long = 0L,
        var peakDeltaKb: Long = 0L,
        var lastDeltaKb: Long = 0L,
        var leaked: Boolean = false,
    ) {
        fun averageDeltaKb(): Long = if (count == 0) 0L else totalDeltaKb / count
    }

    /**
     * [fix/da1019-rssprobe-lock-scope] [applyAndDecideLocked] 的决策快照：record
     * 在锁内拿到它，锁外只据它做副作用（Log）。数值字段是「本次调用后」的读数——
     * 观测日志与告警文案同源，不再各自撕裂地读 Stats 的可变字段。
     */
    internal data class BrowserRssDecision(
        val totalDeltaKb: Long,
        val count: Int,
        val avgDeltaKb: Long,
        val peakDeltaKb: Long,
        val warnLeak: Boolean,
    )

    // action 名 → 聚合统计（进程级累计，与 RSS 生命周期对齐）。
    private val byAction = ConcurrentHashMap<String, Stats>()

    /** 读当前进程 VmRSS（kB）。读失败返回 0（安全侧：不触发降级/告警）。 */
    fun rssKb(): Long = try {
        parseVmRssKb(File("/proc/self/status").readText())
    } catch (t: Throwable) {
        0L
    }

    /**
     * 从 /proc/self/status 文本解析 VmRSS（kB）。无 VmRSS 行返回 0。
     * 独立成纯函数以便 JVM 单测（对齐项目「抽纯函数零回归」路线）。
     */
    internal fun parseVmRssKb(statusText: String): Long {
        val line = statusText.lineSequence().firstOrNull { it.startsWith("VmRSS:") } ?: return 0L
        return line.substringAfter(":").trim().substringBefore(" kB").trim().toLongOrNull() ?: 0L
    }

    /**
     * 记录一次 action 执行的 VmRSS 增量（kB）。delta = afterKb - beforeKb。
     * delta 可能为负（WebView 渲染进程被回收 / GC 释放了映射），如实记录。
     * 零副作用：所有聚合与日志都无法影响 action 的结果。
     */
    fun record(action: String, beforeKb: Long, afterKb: Long) {
        val deltaKb = afterKb - beforeKb
        val st = byAction.computeIfAbsent(action) { Stats() }
        // [fix/da1019-rssprobe-lock-scope] 唯一临界区：自增 + 峰值 + LEAK-SUSPECT
        // 赢家置位全部在锁内（[applyAndDecideLocked]）完成，并把读数捕获进局部
        // 快照 d；锁外只消费 d 做副作用（Log）。修复前 record 全程无锁——
        // 复合自增（count/totalDeltaKb/peakDeltaKb）丢更新（累计被系统性低估、
        // 该触发的 LEAK-SUSPECT 不触发），且 `!st.leaked` 与 `st.leaked = true`
        // 之间有窗口，两个并发 action 会让本该「只报一次」的 WARN 重复打印。
        // 并发来源：BrowserTabPool 的多 tab 池在主进程并发执行 action。
        val d = synchronized(st) { applyAndDecideLocked(st, deltaKb) }

        // 每次调用一行观测日志（压测时可按 action 名 grep 归类）
        Log.i(
            TAG,
            "[browser-rss] action=$action Δ=${deltaKb}kB " +
                "before=${beforeKb / 1024}MB after=${afterKb / 1024}MB " +
                "cum=${d.totalDeltaKb / 1024}MB(×${d.count}, avg=${d.avgDeltaKb / 1024}MB)"
        )

        // 累计正增量越过嫌疑阈值 → 判定为可疑泄漏源，WARN 一次（赢家语义）
        if (d.warnLeak) {
            Log.w(
                TAG,
                "[browser-rss] LEAK-SUSPECT action=$action totalDelta=${d.totalDeltaKb / 1024}MB " +
                    "over ${d.count} calls (peak=${d.peakDeltaKb / 1024}MB) — 候选 WebView native OOM 源头"
            )
        }
    }

    /**
     * [fix/da1019-rssprobe-lock-scope] 一次临界区里完成「自增 + 阈值判定 +
     * 赢家置位」的纯逻辑（对齐 OffloadRssProbe.applyAndDecideLocked 的形状，
     * JVM 可直测；并发守恒臂见 BrowserRssProbeLockScopeTest）。
     *
     * **只允许在 `synchronized(st)` 内调用**——调用方负责持锁；本函数保证
     * 「判定」与它所依据的数值之间不可能被另一个 action 插入：
     * - leaked 只由第一个跨 [LEAK_SUSPECT_KB] 的调用置位（赢家语义 ⇒ WARN 恰一次）；
     * - 返回的 [BrowserRssDecision] 携带同步的 count / total / avg / peak 读数，
     *   锁外格式化日志时不会读到撕裂值。
     */
    internal fun applyAndDecideLocked(st: Stats, deltaKb: Long): BrowserRssDecision {
        st.count += 1
        st.totalDeltaKb += deltaKb
        st.lastDeltaKb = deltaKb
        if (deltaKb > st.peakDeltaKb) st.peakDeltaKb = deltaKb

        // 读数快照：观测日志与 LEAK-SUSPECT 文案同源。
        val totalAfterKb = st.totalDeltaKb
        val countNow = st.count
        val peakNow = st.peakDeltaKb

        val warnLeak = !st.leaked && totalAfterKb >= LEAK_SUSPECT_KB
        if (warnLeak) st.leaked = true

        return BrowserRssDecision(
            totalDeltaKb = totalAfterKb,
            count = countNow,
            avgDeltaKb = if (countNow == 0) 0L else totalAfterKb / countNow,
            peakDeltaKb = peakNow,
            warnLeak = warnLeak,
        )
    }

    /** 打印全量聚合汇总（压测结束 / 定时 dump 用）。返回汇总文本。 */
    fun summary(): String {
        if (byAction.isEmpty()) {
            Log.i(TAG, "[browser-rss] summary: (no browser actions executed)")
            return "(no data)"
        }
        // [fix/da1019-rssprobe-lock-scope] 锁内取一份快照（Stats.copy()），锁外
        // 格式化：与并发 record 交错时每行读数自洽（cum/count/avg/peak 同源），
        // 不再打印撕裂读数、排序键也不再是撕裂值。
        val snapshot = byAction.entries.map { (name, st) ->
            name to synchronized(st) { st.copy() }
        }
        val sb = StringBuilder("browser-rss summary:\n")
        snapshot
            .sortedByDescending { it.second.totalDeltaKb }
            .forEach { (name, st) ->
                sb.append(
                    "  $name: cum=${st.totalDeltaKb / 1024}MB " +
                        "count=${st.count} avg=${st.averageDeltaKb() / 1024}MB " +
                        "peak=${st.peakDeltaKb / 1024}MB last=${st.lastDeltaKb / 1024}MB" +
                        (if (st.leaked) " [LEAK-SUSPECT]" else "")
                ).append('\n')
            }
        Log.i(TAG, "[browser-rss] $sb")
        return sb.toString()
    }

    /** 压测自检用：重置聚合（生产不调用）。 */
    fun reset() {
        byAction.clear()
    }
}

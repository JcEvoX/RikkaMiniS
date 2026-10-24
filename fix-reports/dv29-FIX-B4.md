# dv29-FIX-B4 — 会话切换/恢复时视口未重放（B-04）

## 1 身份链

| 项 | 值 |
|---|---|
| 基线 main | `b82aa8dc`（worktree `git rev-parse --short HEAD` 实测一致） |
| 分支 | `fix/dv29-b4-browser-viewport-reset` |
| 提交序列 | `bbae0ada` 生产（`BrowserTabPool.kt` +74/−14）· `29bb5ded` 测试（`BrowserSessionBindingTest.kt` +317/−8）· `55d898d8` 门（`browser_cookie_ui_refresh_check.py` +315/−2） |
| 分支尖端 / CI 绿 sha | `55d898d8dcb82580f76ec517c587cc9b21030feb`（run #2599 / id `40069167170` / job `120798672281`，attempt 1 success） |
| 三方 sha | 本地 `git rev-parse HEAD` == `git ls-remote origin` == CI `run.head_sha`：三者逐字符一致（见 §5） |
| 改动量 | 3 文件 **+706 / −24**；门总数 **111 不变**、夹具 **342 不变**（`scan.sh` / `test_scan.py` 零触碰） |
| 影响面 | 生产 1 文件（`browser/BrowserTabPool.kt`）· 测试 1 文件 · 门 1 文件（`browser_cookie_ui_refresh_check.py`，任务书二选一） |

## 2 缺陷与先证

### 2.1 工单结论：**成立**（三条路径全部复现，机制链逐环闭合）

报告行号回树核对：`BrowserTabPool.kt:493-496` / `:1975-1977` / `:2020-2031` 在基线树上**逐行吻合，0 行号漂移**；`BrowserUseManager.kt:1964`（字段声明）/`:1980`（唯一写点）/`:1238`（navigate 重放）/`:995`（`minis://` meta 注入）/`:1404`（全页截图）/`:550`（容器宽度变化时重算 initialScale）同样全部对上，**本工单无失真项**。

**可执行机制链**：

1. 触发输入 —— 会话 A 通过 `browser_use` 的 `set_viewport` 设自定义视口（`BrowserTabPool.setSessionViewport`，`:1815-1819` 原文：两个 flow 写值 + `applyViewportToAllTabs()`）。
2. 载荷落点 —— `applyViewportToAllTabs()`（`:1845`）对每个存活标签调 `BrowserUseManager.applyViewport(w, h)`，后者 `:1980` 写 `lastAppliedViewport = cssWidth to cssHeight`。**这才是 WebView 的实际尺寸，flow 只是池的意图。**
3. 没走的动作 —— 三条「会话切换/恢复」路径只把 flow 归零：
   - `bindSessionContextOnly` 的 `if (decision.resetViewport) { _sessionViewportWidth.value = 0; _sessionViewportHeight.value = 0 }`（基线 `:493-496`，清零点**无任何** `applyViewportToAllTabs()`）；
   - `loadSavedState` 的 `!file.exists()` 分支（基线 `:1975-1977`，置 0 后直接 `return`）；
   - `loadSavedState` 的 file-exists 分支（基线 `:2023-2031`）：重放被 `if (restoredViewport.hasOverride)` 门控，**从有覆盖的会话切到 0×0 会话时不重放**。
4. 消费者拿到错值 —— 存活标签的 `lastAppliedViewport` 仍是 A 的尺寸，并被三处无条件重放：`navigate` 在 `loadUrl` 前（`:1238`）、`minis://` 文档注入 meta viewport（`:995`）、`full_page` 截图（`:1404`）；`:550` 还会用旧宽度重算 initialScale。
5. 用户可见后果 —— 会话 B 从未设过视口，但它借用 `MinisApp.sharedBrowserTabPool` 里的存活标签时，页面布局宽度、`window.innerWidth/Height`、全页截图尺寸全部锁在 A 的尺寸上，且每次导航都重新锁一次。

**为什么必须重放而不能只清 flow**：`applyViewport` 是唯一能覆盖 `lastAppliedViewport` 的入口（全文件 1 处写点 `:1980`）；清 flow 只影响**之后新建**的标签与 `resolvedViewportSize()` 的返回值，对已存活标签零作用。

### 2.2 判级：**P1 成立（不降级）**

可达性三查：①UI / 工具入口存在 —— `browser_use` 的 `set_viewport` 与 `reset_viewport` 是文档化动作；②调用点全仓计数 —— `bindSessionContextOnly` 1 处（`BrowserUseOffloadHandler.kt:128`，每次 offload CLI 调用都执行）、`setSession` 2 处（`ChatSessionEntryExt.kt:302`、`ChatViewModel.kt:1619`）、`loadSavedState` 仅由 `setSession` 调用；③旁路 —— 共享池是进程级单例，**多会话交替使用内置浏览器工具即触发**（不需要 debug 通道）。属于「常用路径可感知降级」而非 latent。

### 2.3 同族核查（改前普查，`grep "_sessionViewportWidth.value ="` → 5 个写点，全在 `BrowserTabPool.kt`）

| # | 位置 | 改前判定 | 改后处理 |
|---|---|---|---|
| 1 | `setSessionViewport` | 正常（紧随 `applyViewportToAllTabs()`） | 不动 |
| 2 | `resetSessionViewport` | 正常（同上，且 `suspend` 可 await） | 不动 |
| 3 | `bindSessionContextOnly` | **异常**（只置 0） | 收口到 `clearSessionViewportOverride()` |
| 4 | `loadSavedState` 的 `!file.exists()` | **异常**（只置 0 后 return） | 收口到同一函数 |
| 5 | `loadSavedState` 的 file-exists | **异常**（`hasOverride` 门控） | 去掉门控 + 无条件 `replayViewportToLiveTabs()` |

第 4 处（本单以外的第六个写点）不存在 ⇒ 「三条路径」计数准确。

### 2.4 既有测试栅栏（改前实测）

- `BrowserSessionBindingTest`：`bindSessionContextOnly` 体上钉了 `_sessionViewportWidth.value = 0` / `_sessionViewportHeight.value = 0` 字面量，`loadSavedState` 的 no-file 分支同样 —— 这两条**与任务书要求的收口直接冲突**（见 §5.4 的处置与理由）。
- `BrowserTabPoolDisposeScopeTest`：`dispose()` 取消全部 `= CoroutineScope(` 字段、`saveState` / `restoreSavedTabs` 的 `isDisposed` 守卫 —— 未触碰，全绿。
- `BrowserUserAgentPoolSyncTest`：要求 `handleSetUserAgent` 体内有 `applyViewportToAllTabs()` —— 未触碰（该路径不写 flow），全绿。
- `BrowserTabPoolSaveStateTest` / `BrowserTabPoolBusyPublishTest`：与视口无关，全绿。

## 3 修法（阶梯第 6 级：最小实现 + 一个收口点）

**停在第 6 级，因为**：不需要新 API、不需要动 `BrowserUseManager`（`applyViewport` 已存在且正是唯一写点）、不需要新数据层；缺陷就是「三条路径少了一次调用」，所以加法是：一个私有收口函数 + 一个私有重放函数，把三条路径各改一行。

```kotlin
private fun clearSessionViewportOverride() {
    _sessionViewportWidth.value = 0
    _sessionViewportHeight.value = 0
    replayViewportToLiveTabs()
}

private fun replayViewportToLiveTabs() {
    if (isDisposed) return            // dispose 后不重放（任务书硬约束）
    if (_tabs.value.isEmpty()) return // 空池不启协程（既有快路径）
    evictionScope.launch { applyViewportToAllTabs() }
}
```

- **幂等**：重复清零写同一对值、重放同一「解析后尺寸」；`applyViewportToAllTabs` 只 `reload` 已有标签，**不重建 WebView**（`createTab` 不在其内）。
- **不阻塞主线程**：`bindSessionContextOnly` / `loadSavedState` 是普通函数，无法 suspend，所以重放走既有 `evictionScope`（Default）→ `applyViewportToAllTabs` 内部自己 `withContext(Dispatchers.Main)`。没有新增 `runBlocking`。
- **保住对外契约**：`applyViewport(w,h)` 未改；`_sessionViewport*` 的 StateFlow 语义未改（仍只在真正改变时发布）；`evictIdleTabs` / `evictionScope` 生命周期未改；`dispose()` 未改。
- **第三路径的语义**：`if (restoredViewport.hasOverride)` 整块删除，改为无条件重放 —— "恢复的是 0×0 会话"不是"无事可做"，重放**解析后**尺寸正好双向覆盖（有覆盖 → 重申覆盖；无覆盖 → 重申全局/UA 默认，从而覆盖掉陈旧的 `lastAppliedViewport`）。

刻意简化（按 COMMON 打 ponytail）：

```
ponytail: 重放是 fire-and-forget，不等待 reload 完成 | 天花板: 切换会话后的第一个动作可能仍读到旧视口（一次性，直到派发的协程跑到） | 升级触发: 真机/回归报告出现"切会话后第一个 navigate/screenshot 仍是旧尺寸"
```

## 4 覆盖面（同族 / 调用点普查计数）

| 面 | 普查口径 | 计数 | 结论 |
|---|---|---|---|
| `_sessionViewportWidth/Height.value` 写点 | `grep -n "_sessionViewport\(Width\|Height\)\.value ="` 主源 | 改前 5 处 → 改后 **8 条写语句 / 4 个成员**（`setSessionViewport`、`resetSessionViewport`、`clearSessionViewportOverride`、`loadSavedState`） | 清零点从 3 个成员收敛到 **1 个私有成员** |
| `applyViewportToAllTabs(` 调用点 | 同上 | 7 处（含定义 1） | 新增 1 处（`replayViewportToLiveTabs`），其余不动 |
| 「清零但未重放」路径 | 逐成员核对「最后一条写 < 最后一次重放」 | **改前 3 → 改后 0** | 门 R1 从此恒守 |
| `bindSessionContextOnly` 调用点 | 全仓 grep | 1（`BrowserUseOffloadHandler.kt:128`） | 每次 offload 调用都走修好的路径 |
| `setSession` 调用点 | 全仓 grep | 2（`ChatSessionEntryExt.kt:302`、`ChatViewModel.kt:1619`） | 两条都经 `loadSavedState` 两分支收口 |
| `lastAppliedViewport` 写点 | `BrowserUseManager.kt` | **1**（`:1980`，`applyViewport` 内） | 说明「必须重放」而非「必须改字段」 |
| `lastAppliedViewport` 重放点 | 同上 | 4（`:1238` navigate · `:995` meta 注入 · `:1404` 全页截图 · `:550` 容器宽度变化） | 与报告一致，链条闭合 |
| 门 selftest 臂 | `--selftest` OK 行 | 33 → **40**（+7 条 V9） | 原有 33 臂全绿，未放宽任何判据 |
| 门 / 夹具总数 | `scan.sh` / `test_scan.py` | **111 / 342 不变** | 0 新建门，冻结面零触碰 |

## 5 验证四层

### 5.1 L1 静态门（本门 + 邻门）

- `browser_cookie_ui_refresh_check.py .` → **OK**（真树绿）。
- `--selftest` → **40 臂全 OK**，`BROWSER_COOKIE_UI_REFRESH_SELFTEST=PASS`（rc=0）。V9 的 7 条臂：合规片绿；「收口点只清零不重放」「第四处自己清零」「绑定时不再清零」「恢复分支不重放」「恢复分支用 hasOverride 门控」「重放整个消失」各**恰红其判据**（期望集手写自判据表，不从运行结果派生）。
- 邻门 4/4 rc=0：`browser_tabs_lifecycle_check`、`browser_tab_exec_serial_check`、`downloads_atomic_update_check`、`mediaplayer_release_guard`；`browser_tabs_lifecycle_check --selftest` 亦 rc=0。
- 任务书要求「门选一个、另一个不许动」：**选 `browser_cookie_ui_refresh_check.py`**，因为该门**已经拥有 viewport / session-override 家族（V1–V8）且已经读取 `BrowserTabPool.kt`**（V7 是它的第一个跨文件判据）；把「清零必须到达标签」放进同一家族，比塞进 `browser_tabs_lifecycle_check`（管的是标签生命周期/持久化）在语义上更靠近。`browser_tabs_lifecycle_check.py` **未改一字**（`git diff --name-only` 可核）。

### 5.2 L2 邻域/调用点

- `grep -rln "clearSessionViewportOverride\|replayViewportToLiveTabs" src/` → 仅 `BrowserTabPool.kt`（+ 测试与门按名引用）：收口函数没有被别处误用。
- `applyViewport(` 的调用点未增（仍是池侧 2 处 + `BrowserUseManager` 内部）⇒ 没有引入第三条重放通道。

### 5.3 L3 真 JVM 装置（沙箱 `kotlinc 2.0.21` + JUnit 4.13.2）

池的 WebView/Context 不能在沙箱构造，但**被测判据本身是纯源形状 + 纯决策核**，所以用「`BrowserTabPool.kt` 尾部纯声明切片 + 测试类」编译（切片 `probe/l3/prep.py`，3288 B，从 `internal object BrowserSessionBinding` 前的 KDoc 起）。

- **POST 臂（修正树）**：`BrowserSessionBindingTest` **OK (22 tests)**（原 18 + 新 4）。
- **POST 臂（浏览器源形状五族，user.dir = 模块目录）**：`BrowserSessionBindingTest` + `BrowserTabPoolDisposeScopeTest` + `BrowserTabPoolSaveStateTest` + `BrowserTabPoolBusyPublishTest` + `BrowserUserAgentPoolSyncTest` → **OK (44 tests)**。
- **PRE 臂（`b82aa8dc` 原文件 + 新测试）**：**6 红**，且恰好是钉本缺陷的那 6 条 —— `every session viewport write is followed by a replay`（点名 `bindSessionContextOnly` 写了不重放）、`loadSavedState … no-override paths`（无文件分支不走收口）、`the collection point clears and replays`（找到 3 个非 suspend 清零点）、`a disposed pool never replays`（无收口点）、`the viewport replay criteria have teeth`、`bindSessionContextOnly rebinds …`。**其余 16 条仍绿**＝半径精确，没有把不相关的形状一起拖红。
- **外部变异臂（独立进程、每臂独立树副本、源文件全程不动，`arms.sh`）**：m1 收口点去掉重放 → junit_rc=1（4 红）；m2 恢复分支去掉重放 → rc=1（3 红）；m3 `releaseAllTabs` 加一处裸清零 → rc=1（4 红）。三臂全红，判据有牙。

### 5.4 既有测试的改动（单列，含理由）

两处**必须是语义等价、锚点换位**的改动（任务书「既有断言语义不许改」）：

1. `bindSessionContextOnly rebinds without loading persisted state` —— 原断言要求函数体内出现 `_sessionViewportWidth.value = 0` / `_sessionViewportHeight.value = 0` 字面量。这与任务书 §3「三条清零路径统一收口到一个私有函数」**直接矛盾**（收口后函数体里不再有这两条语句）。改为断言 `if (decision.resetViewport) {` + `clearSessionViewportOverride()`，**语义不变**（"a session change must drop the previous viewport override"），并把"收口点确实置 0 + 重放"移到新用例 `the collection point clears and replays` 里单独钉住。**保留原字面量断言 = 把缺陷形状焊死在测试里**，所以这是唯一正确的处置。
2. `loadSavedState clears a stale viewport on both no-override paths` 的 arm 1 —— 同上，`noFile` 分支的字面量断言改为 `clearSessionViewportOverride()`；arm 2（`idxWidth > idxDecision`、`idxHeight > idxWidth`、禁止 `if (w > 0 && h > 0) {`）**一字未动**，另**新增**两条：重放必须在写值之后；不得再用 `hasOverride` 门控。

新增 4 条用例（全部写死字面量、期望不从生产常量派生）：`every session viewport write is followed by a replay`（派生成员集 + 派生重放集，不写死函数名）、`the collection point clears and replays`、`a disposed pool never replays`、`the viewport replay criteria have teeth`（5 条变异臂各自要求可观察）。

### 5.5 L4 分支 CI（run #2599 · job 120798672281 · 判读 19/19 GREEN）

| 判据 | 结果 |
|---|---|
| 三方 sha | 本地 `git rev-parse HEAD` == `git ls-remote origin` == run.head_sha == job.head_sha = `55d898d8dcb82580f76ec517c587cc9b21030feb`，**逐字符一致** |
| run / attempt / conclusion | #2599 / `40069167170` / attempt 1 / **success** |
| 步骤 | 26 步，`not_success = []`；唯一 skipped = 第 23 步 `Publish to Releases`（**从 steps API 判定**，main 门控；日志里没有这行） |
| 扫描汇总 | 9 条含 `Summary:` 的行，8 条可解析：`(25,0) (5,0) (13,0) (33,0) (10,0) (22,0) (10,0)` + **`(111,0)`** ⇒ 恰一条 `111 passed, 0 failed`，且**任何 Summary 行的 failed 都是 0** |
| 夹具套件 | `ALL 342 CASES PASS` 恰 1 次 |
| 门头 | `[n/111]` 共 111 条，编号连续 1..111，显示名互不重复 |
| 编译错 / 游离 FAILED | ` e: ` 0 条；行尾 `FAILED` 0 条 |
| 编译警告 | 全仓 376 条；**本支三个文件 13 条，全部落在本支未触碰的行**（基线感知判定 `probe/ci/warn_scope.py`：把每条警告的行号与本支 `git diff -U0` 的新增行区间求交，13 条全是 `[base]`、**NEW = 0**）。这 13 条是 main 既有的 `TRIM_MEMORY_*` 弃用、`Condition is always 'true'`（`BrowserTabPool.kt:1225/1239/1746/1747`）与 `readRepoFile` 的 `File(dir, relative)` 可空性（`BrowserSessionBindingTest.kt:672`）——后者与 1042-R2 登记的 `BrowserTabPoolBusyPublishTest.kt:36:47` 同族，是既有 P2 |
| 新用例 | 4 条新用例逐条 **PASSED**（`every session viewport write is followed by a replay` / `the collection point clears and replays` / `a disposed pool never replays` / `the viewport replay criteria have teeth`） |
| 本门 | `browser_cookie_ui_refresh_check OK on real tree` + `BROWSER_COOKIE_UI_REFRESH_SELFTEST=PASS` 均在册 |

**源码树等价声明**：本次绿 run 之后只追加 `fix-reports/dv29-FIX-B4.md`，因此
`git diff --name-only 55d898d8 HEAD -- . ':(exclude)fix-reports'` 输出为空 ⇒ CI 结论对当前代码树成立。

**判读器自伤（已闭环，形态可复用）**：`warn_scope.py` 首版正则写成 `:(\d+):(\d+):`（要求行号后还有第三个冒号），而 Gradle 警告行是 `…kt:1225:13 Condition is…` ⇒ 0 命中、"NEW warnings 0" 的**假绿**。修法是先取真实样本再写判据（这是本仓第 N 次同族教训）；另 `judge.py` 首版把「本支文件 0 警告」当硬判据，也是错的——**main 本身就带警告**，判据必须是「新增 0」而不是「绝对 0」。

## 6 残留（R 条目，每条带触发条件 + 触发后方案）

| # | 残留 | 触发条件 | 触发后方案 |
|---|---|---|---|
| R1 | 切会话后**第一个动作**可能仍读到旧视口：重放是 fire-and-forget，`bindSessionContextOnly` 返回后立刻执行的动作可能先于重放协程 | 真机/回归出现「多会话交替下首次 navigate/screenshot 仍是上一会话尺寸」 | 把重放做成可 await：`BrowserUseOffloadHandler` 在 execute 前 `awaitViewportReplay()`（需在 offload 面加一个挂起点，跨本支边界） |
| R2 | 重放会给**所有**存活标签 `reloadAndWait()`（含 `inUse` 的），切会话瞬间可能打断正在进行的页面 | 用户报告「切会话/后台任务导致当前页重载」 | 与 `setSessionViewport` 同源问题（改前就有）；收敛为只重放「非 inUse 或视口确实变化」的标签 |
| R3 | `clearSessionViewportOverride` 不以 `isDisposed` 为提前条件（只在下游 `replayViewportToLiveTabs` 检查） | 有人在 dispose 后直接调它 | 把 `isDisposed` 提前到收口函数首行（当前无此调用点；两者等价） |
| R4 | 门 R2 判据是「恰好一个非 suspend 清零点」，若将来出现**合法的**第二个非 suspend 清零路径（例如新增一个需要同步语义的 API） | 新增此类路径时门变红 | 按判据表把它并入收口函数，或把该路径标 `suspend` 并直接 await（判据本身不变） |
| R5 | 门 R1 用「最后一次写之后必须有一次重放」而非逐写点配对：同一成员内「写-重放-写」的交错形态不会被判红 | 出现单成员内多写多放的复杂分支 | 升级为逐写点「其后到成员结束必须有重放」 |
| R6 | 真机行为级未验：三个方向的可见效果（A→B 后 B 的 `window.innerWidth` 回到默认 / 全页截图尺寸正确 / `reset_viewport` 之后不再回弹） | 下次真机窗口 | 用 `browser_use` 的 `set_viewport` → 切会话 → `get_page_info` 读 `window.innerWidth` 做对照 |

## 7 未验证边界

- **无真机运行证据**：沙箱内无法构造 WebView，`lastAppliedViewport` 的实际覆盖只能用源码链条证明（唯一写点 `BrowserUseManager.kt:1980`），端到端读数留给 R6。
- **多会话并发**：`bindSessionContextOnly` 是 last-writer-wins（池只有一个绑定），修好后仍是「后到者赢」，但每赢一次都会重放 —— 并发场景的最终视口取决于最后一次绑定，属于既有设计，不在本单范围。
- **`full_page` 截图路径**：`:1404` 读 `lastAppliedViewport` 并在结束时回写，重放发生在导航前，理论上不影响截图尺寸；但未做真机对照。

## 8 附装置路径

| 装置 | 路径 |
|---|---|
| L3 切片器 | `probe/l3/prep.py`（从池文件尾部生成 `PoolTail.kt`） |
| L3 外部变异臂 | `probe/l3/arms.sh`（m1/m2/m3，每臂独立树 + 独立进程） |
| L3 日志 | `probe/l3/{kotlinc,pre-kotlinc}.log`、`junit1..4.log`、`pre-junit.log`、`m1..m3/junit.log` |
| PRE 树 | `probe/l3/pre/`（`git show b82aa8dc:…BrowserTabPool.kt` 字节写回，`sha256 11fe27618ed2a049…`，未用 `git checkout --`） |
| 门 V9 拼接脚本（可复现） | `probe/splice_v9.py` + `probe/v9_block.py` |
| CI 判读 | `probe/ci/{ci_fetch.sh,judge.py,warn_scope.py}` + `probe/ci/job-120798672281.log`（1,466,349 B）/ `runs.json` / `jobs.json` |

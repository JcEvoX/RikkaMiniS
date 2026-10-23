# FIX-10 · deep-audit-1044（v23 波）· 工单 B-07（P1）

- **分支**：`fix/dv24-b4-browser-dispose-scope`
- **面**：B 面（`browser/**`）
- **生产文件**：`src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserTabPool.kt`
- **门（独占）**：`scripts/scan/browser_tabs_lifecycle_check.py`（C4 扩 → 新增 C4e / C4f；`POOL_COMPLIANT` 夹具同步）
- **测试（新增）**：`src/android/app/src/test/java/com/rikkaminis/app/browser/BrowserTabPoolDisposeScopeTest.kt`
- **基线**：main `f94413f52fa5403c903902f6b1d2ae10cb9bc906`
- **门/夹具计数**：109 / 336 **不变**（0 新建门，扩既有门；`scan.sh` / `test_scan.py` 冻结面 **0 触碰**）

---

## §1 缺陷与根因

### 症状（原话）
用户删掉一个会话（`ChatHistoryDrawer` → `chatRepository.deleteSession(id)` → `SessionFileStore.deleteSessionFiles` 删 `filesDir/browser_tabs/<sid>.json` → `ChatViewModelStore.release(id)` → `ChatViewModel.onCleared()` → `_browserTabPoolRef?.dispose()`）之后，**刚被删掉的 `<sid>.json` 又出现在磁盘上**，且内存持续增长（WebView 没被回收）。

### 根因假设
`BrowserTabPool.dispose()` 只释放了「标签页」这一半，没有释放「异步面」：两个实例自有的 `CoroutineScope`、下载任务表、待恢复队列、聊天回调全都活得比池子久。取消 scope 只能挡住**尚未开始**的工作；已经派发到 Main 的块取消不回来 —— 那个块恰好会重建 WebView 并落盘。

### 证据（真树实测，行号以基线 `f94413f5` 为准）

| 位置 | 事实 |
|---|---|
| `:328` | `private val evictionScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)` —— 实例自有，全仓仅此一处声明 |
| `:532` | `private val downloadScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)` —— 同上 |
| `:523` | `private val downloadJobs = java.util.concurrent.ConcurrentHashMap<Long, Job>()` |
| `:530` | `var onDownloadEvent: ((String) -> Unit)? = null` —— 绑定到 `ChatViewModel`（`ChatViewModel.kt:1578` `it.onDownloadEvent = { text -> … appendSystemInfo(text, "info") }`） |
| `:1603-1621` | `fun dispose()` 实测**只**做：`evictionJob?.cancel()` + `unregisterComponentCallbacks` + 遍历 `destroyTab` + `_tabs.value = emptyList()` + `publishAgentBusy()`。`evictionScope` / `downloadScope` / `downloadJobs` / `savedURLs` / `onDownloadEvent` 在 dispose 体内 **0 命中** |
| `:1959` | `evictionScope.launch { withContext(Dispatchers.Main) { restoreSavedTabs() } }` —— `loadSavedState()` 内 |
| `:1998-2017` | `restoreSavedTabs()`：遍历 `savedURLs` → `createTab(...)` → 尾部 `if (restored > 0) saveState()` |
| `:1871` | `private fun saveState()` —— 写 `filesDir/browser_tabs/<sid>.json` |
| `:2995`（`ChatViewModel.kt:4242`） | `onCleared()` → `_browserTabPoolRef?.dispose()` |

### 触发链（可指认）
1. 用户打开一个**带持久化标签页**的会话 → `loadSavedState()` 读 `<sid>.json` → `savedURLs` 非空 → `evictionScope.launch { withContext(Dispatchers.Main) { restoreSavedTabs() } }` **排进主线程队列**。
2. 用户立刻删会话 → `<sid>.json` 被删 → `onCleared()` → `dispose()`。**此时队列里的 `restoreSavedTabs()` 还没跑**（或正在跑）。
3. `restoreSavedTabs()` 在**已销毁的池**上 `createTab(...)` ⇒ 新建 WebView，而 `dispose()` 的 destroy 循环**早已结束** ⇒ 这些 WebView 永远不会被销毁（每个 50-100 MB 渲染进程 + 它们持有的 Context ⇒ 内存持续增长）。
4. 尾部 `if (restored > 0) saveState()` ⇒ **把刚被删的 `<sid>.json`（含明文 URL）写回磁盘** ⇒ 击穿 dv14-B2 / 1034-B2 的「已删会话不得复活」不变量。
5. 并行面：`downloadScope` 里的下载协程继续往 `filesDir/minis-sessions/<sid>/workspace/downloads/` 写，并通过**未置空的** `onDownloadEvent` 回调**已销毁的 VM**（`appendSystemInfo` 写 `_messages`）。

### 门盲区（实测）
`browser_tabs_lifecycle_check.py:255-275` 的 `check_pool_source`（C4）只查两件事：`dispose()` 体内**不得**出现 `saveState(`、**必须**保留 `destroyTab(`。**不查作用域取消** ⇒ 本缺陷在门眼里完全不可见。

---

## §2 修法

### 阶梯定位：**第 7 级（显式销毁闩锁 + 释放全部自有异步面）**

按 `change-ladder` 七级逐条排除：

| 级 | 内容 | 本次判定 |
|---|---|---|
| 1 | 真要做吗 | 要。触发路径是「删会话」这一**常用操作**，且后果是「已删数据复活 + 内存泄漏 + 向已死 VM 回调」= P1 |
| 2 | 代码库里已有现成的？ | 搜过：无任何 dispose 作用域清理的既有单源（`evictionJob?.cancel()` 只覆盖 ticker）。`releaseAllTabs()`（`:1562`）只重算 busy 旗标 + 落盘，**不是**释放面 |
| 3 | 标准库？ | 无（Kotlin 没有「作用域自动随对象死亡」的机制） |
| 4 | 平台原生？ | 无 |
| 5 | 已装依赖？ | 无 |
| 6 | 一行能改？ | 不能 —— 需要「取消 scope + 取消已注册 job + 清表 + 断回调 + 闩锁 + 两处守卫」六件事 |
| 7 | **最小实现** | **停在这里**：一个 `@Volatile` 闩锁 + `dispose()` 内 6 行释放 + 两处入口守卫。**不引入新抽象层、不引入新缓存、不引入新状态机** |

### 具体改动（生产文件，`BrowserTabPool.kt`）

1. **新增闩锁**（`:329-339`）：
   ```kotlin
   @Volatile
   private var isDisposed = false
   ```
   `@Volatile` 是**硬需求**：`dispose()` 由 `ChatViewModel.onCleared()` 在其线程调用，而读它的 `restoreSavedTabs()` 跑在 Main 线程 —— 跨线程可见性不保证就等于没闩。

2. **`dispose()` 重写**（`:1629-1647`）：入口 `if (isDisposed) return` + `isDisposed = true`（**幂等**：二次调用直接返回，释放半只跑一次），随后依次
   - `evictionScope.cancel()` / `downloadScope.cancel()`（scope 级：挡未开始的工作，并 detach 子 job）
   - `for (job in downloadJobs.values) job.cancel()` + `downloadJobs.clear()`（job 级：注册表是池自己的索引，取消 scope 不等于取消已注册的 job）
   - `savedURLs.clear()`（待恢复表指向即将消失的会话）
   - `onDownloadEvent = null`（回调指向已死的 VM）
   - 原有 `evictionJob?.cancel()` / `unregisterComponentCallbacks` / destroy 循环 / `publishAgentBusy()` **一行未动**（保住 C4b 与 dv22-B2 的 `[dv22-B2] publishAgentBusy()` 锚点）。

3. **`saveState()` 入口守卫**（`:1919`）：`if (isDisposed) return`，**放在 `browser_tabs` 触碰之前** —— 这是「已删文件复活」的最后一道闸。

4. **`restoreSavedTabs()` 入口守卫**（`:2052`）：`if (isDisposed) return`，**放在第一个 `createTab(` 之前** —— 取消 scope 挡不住已派发到 Main 的块，这正是闩锁覆盖的窗口。

### 被否决的方案与理由

| 方案 | 否决理由 |
|---|---|
| 只在 `dispose()` 里 `evictionScope.cancel()`，不加闩锁 | **挡不住本缺陷**：第 1959 行的块是 `launch { withContext(Dispatchers.Main) { … } }` —— 一旦 `withContext` 已经把块派发到 Main，取消父 scope **无法撤回**它。这是最核心的一点，也是门判据 C4f 存在的理由 |
| 用 `evictionScope.isActive` 代替闩锁 | 同族但**不覆盖 `downloadScope`**，且 `isActive` 语义是「scope 是否可派发新任务」，不是「池是否已死」；`downloadScope` 取消后仍有已在 IO 上跑的块。语义不匹配 ⇒ 用显式 `isDisposed` |
| 用 `sessionId == null` 之类的既有状态位 | `sessionId` 是会话 id，dispose 时它**并不为 null**（共享池 + 借用会话的场景下甚至明确指向某个 id），语义不同且已被 C11 的 `isContextOnlySession` 占用同族语义面。**新加独立闩锁**避免与既有判据纠缠 |
| 让 `dispose()` 调 `saveState()`「先落盘再销毁」 | **直接违反 C4a**（dv14-B2 的既有不变量），且正是本缺陷的成因 |
| 把「哪些要释放」抽成返回 List 的纯函数便于 JVM 测 | 抽不出可测收益：真正的不变量是「dispose 之后不再有异步工作碰这个池」，那是**顺序 + 时序**属性，纯函数表达不了。本仓既有形状测试惯例（`BrowserTabPoolSaveStateTest` / `BrowserTabPoolBusyPublishTest`）已是本面的标准做法 ⇒ 见 §5 L3 |
| 在 `dispose()` 里清 `savedURLs` / 置空 `onDownloadEvent` 之外，还去删已写出的下载文件 | 半径外：文件回收是独立议题（见 §7 R2） |

### `ponytail` 标记
`ponytail: 只取消「池自有的两个 scope + 已注册的下载 job + 待恢复表 + 聊天回调」，不做下载文件回收、不做 WebView 之外的资源审计 | 天花板: 若将来新增第三个实例自有 scope，dispose 不会自动覆盖（门 C4e 会红，但生产代码不会自愈） | 升级触发: 池内新增 `CoroutineScope(` 字段时门 C4e 转红`

---

## §3 改动清单

| 文件 | 增删 | 本支改动的行区间（基线 `f94413f5` → HEAD） |
|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserTabPool.kt` | **+55 / −0** | `@@ -325,6 +325,19 @@` 闩锁字段 `:329-339`；`@@ -1599,8 +1612,37 @@` dispose 体重写 `:1612-1647`；`@@ -1869,6 +1911,12 @@` saveState 守卫 `:1911-1919`；`@@ -1996,6 +2044,12 @@` restoreSavedTabs 守卫 `:2044-2052`；另有 `:13` 新增 `import kotlinx.coroutines.cancel`（CI 首跑编译错后补，见 §5 L4） |
| `src/android/app/src/test/java/com/rikkaminis/app/browser/BrowserTabPoolDisposeScopeTest.kt` | **+253 / −0** | 新建文件 |
| `scripts/scan/browser_tabs_lifecycle_check.py` | **+252 / −3** | 头文档 `@@ -44,6 +44,31 @@`；辅助函数 `@@ -255,6 +280,60 @@`；C4e/C4f 判据 `@@ -297,6 +376,84 @@`；`POOL_COMPLIANT` 夹具同步 `@@ -362,6 +519,14 @@` / `@@ -370,6 +535,10 @@` / `@@ -382,6 +551,24 @@` / `@@ -529,8 +716,8 @@`（C4a 变异锚点随夹具尾移动）；新变异夹具 `@@ -580,6 +767,61 @@`；新臂注册 `@@ -1308,6 +1550,13 @@`；OK 文案 `@@ -1460,7 +1709,7 @@` |

**合计 3 文件，+560 / −3。** 冻结面 `scripts/scan/scan.sh`、`scripts/scan/test_scan.py`：**0 触碰**。未改 `ui/**`，未改 `BrowserUseManager.kt`，未改 `AndroidManifest.xml` / `build.gradle*` / workflow。

---

## §4 门判据与带牙证明

### C4e —— dispose() 必须取消池自有的每一个 scope

- **判定对象**：`val … = CoroutineScope(` 的**字段集合从文件自身派生**（`_c4_scope_fields`），不是一个写死的成员清单 ⇒ 将来加第三个 scope 自动被覆盖（写死成员清单 = 门自身的盲区，deep-audit-1041 §8.7 第 11 条）。
- **取消怎么算**：`dispose()` 体内出现的 `X.cancel()` 记为「已取消 X」；同时沿**同文件块体函数的传递闭包**追踪（`_c4_cancel_closure`），所以 `dispose()` → `destroyScopes()` → `evictionScope.cancel()` 这种重构**判绿**，而**真没取消**判红。
- **fail-closed**：池里一个 `CoroutineScope(` 字段都找不到 ⇒ 红（判据不会因为「结构变了」而静默放过）。

### C4f —— 销毁闩锁 + 两个异步续体的守卫

1. `dispose()` 体内必须存在一个「`X = true` 且 `if (X) return`」的闩锁对（`_c4_guard_pos` 认 `if (X) return` 与 `if (X) { … return … }` 两种形态）；
2. 该闩锁必须声明为 `@Volatile`（正则 `@Volatile\b[\s\S]{0,160}?\bvar X\b`）；
3. 闩锁的检查必须**早于** `dispose()` 的第一个释放动作（`cancel(` / `unregisterComponentCallbacks` / `destroyTab(`）⇒ 二次 dispose 必须是 no-op；
4. `saveState()` 体内必须 `if (X) return`，且**早于** `browser_tabs` 触碰点；
5. `restoreSavedTabs()` 体内必须 `if (X) return`，且**早于**第一个 `createTab(`。

### 判据锚的是「决定」不是「拼写」
- scope 集合**派生**、取消**可经 helper** ⇒ 实现怎么重构都不失明；
- 闩锁**由行为反推**（找 `X = true` 且 `if (X) return` 的那个 X），不是钉 `isDisposed` 这个名字 ⇒ 改名不失明；
- 守卫的**位置**被钉（早于触碰点），但**写法**不钉。

### 带牙臂（新增 7 臂，门臂总数 46 → **53**）

| # | 臂 | 改了什么 | 红在哪条判据 | 精确性 |
|---|---|---|---|---|
| 1 | C4e dispose stops cancelling the eviction scope | 删 `evictionScope.cancel()` | `C4e: dispose() never cancels 'evictionScope' …` | 恰红 C4e（其余判据仍绿） |
| 2 | C4e dispose stops cancelling the download scope | 删 `downloadScope.cancel()` | `C4e: dispose() never cancels 'downloadScope' …` | 恰红 C4e |
| 3 | **C4e cancellation through a helper stays green** | 把两行 cancel 挪进 `destroyScopes()` helper | **绿**（负控：证明判据锚决定不锚写法） | 0 命中 |
| 4 | C4f restoreSavedTabs drops its disposal guard | 删 restore 的守卫 | `C4f: restoreSavedTabs does not short-circuit on 'isDisposed' …` | 恰红 C4f |
| 5 | C4f saveState drops its disposal guard | 删 saveState 的守卫 | `C4f: saveState does not short-circuit on 'isDisposed' …` | 恰红 C4f |
| 6 | C4f disposal latch is not volatile | 删 `@Volatile` | `C4f: the disposal latch 'isDisposed' is not @Volatile …` | 恰红 C4f |
| 7 | C4f dispose never latches | 删闩锁字段 + 删守卫 | `C4f: dispose() never latches a disposal flag …` | 恰红 C4f |

**自检实测**：`python3 scripts/scan/browser_tabs_lifecycle_check.py --selftest` ⇒ `OK` 行 **53**、FAIL **0**、末行 `BROWSER_TABS_SELFTEST=PASS` + `BROWSER_TABS_RISK_NOTICE_AND_REMAP_SELFTEST=PASS`。

**真树**：`python3 scripts/scan/browser_tabs_lifecycle_check.py .` ⇒ rc=0。

### 自伤与修复（1 处）
- **C4a 变异臂锚点漂移**：我把 `POOL_COMPLIANT` 的 dispose 尾部之后插了 `saveState()` / `restoreSavedTabs()`，而 C4a 变异臂原本靠 `…_tabs.value = emptyList()\n        }\n    }\n\n    override fun onTrimMemory` 这段**后继文本**定位插入点 ⇒ 夹具一变，锚点失配，臂变成「没改任何东西」而**假绿**（selftest 报 `expected red, got green`）。修法：把锚点改为紧跟 dispose 的新后继（`    private fun saveState() {`）。**这就是「夹具一改、变异臂必须重新对位」**（同族 1043 波：判据锚形态）。

### 同门未变红确认
`browser_cookie_ui_refresh_check.py`（v22 波 V8，**本支未改**）在本树上仍 rc=0；`browser_tab_exec_serial_check.py` / `downloads_atomic_update_check.py` / `claim_release_guard.py` / `skill_restore_session_cleanup_check.py` / `browser_history_snapshot_check.py` 全部 rc=0。

---

## §5 验证四层

### L1 门
| 项 | 结果 |
|---|---|
| 本门真树 | `browser_tabs_lifecycle_check OK (criteria: … + C4e disposal cancels every pool-owned scope + C4f the disposal latch guards saveState/restoreSavedTabs + …)` rc=**0** |
| 本门 selftest | 53 臂 **OK / 0 FAIL**，`BROWSER_TABS_SELFTEST=PASS` |
| 全量 `scan.sh` | `Summary: 109 passed, 0 failed`，rc=**0**（**门数 109 未变**） |
| 邻门（本池子的其他读者） | `browser_cookie_ui_refresh_check` / `browser_tab_exec_serial_check` / `downloads_atomic_update_check` / `claim_release_guard` / `skill_restore_session_cleanup_check` / `browser_history_snapshot_check` **6/6 rc=0** |

### L2 邻门
按**概念**在 `scripts/scan/` 反查 `grep -rn "BrowserTabPool" scripts/scan/*.py` ⇒ 命中 5 道（`browser_tabs_lifecycle_check` 本门 / `browser_cookie_ui_refresh_check` / `browser_tab_exec_serial_check` / `downloads_atomic_update_check` / `claim_release_guard`），全部 rc=0（**门脚本 0 触碰**）。

### L3 真跑（沙箱 kotlinc 2.0.21 + 真 JUnitCore）

**装置**：`/opt/kotlinc/bin/kotlinc` + `/opt/jvm-libs/junit-4.13.2.jar` + `hamcrest-core-1.3.jar` + `kotlin-stdlib-2.0.21.jar`（Gradle 缓存），**单次编译**（stub + 源 + 测试一次传入），运行 classpath 含 kotlin-stdlib。
**注意**：既有测试用 `readRepoFile("app/src/main/java/…")` 相对路径 ⇒ `user.dir` 必须是 `<repo>/src/android`（Gradle 惯例），否则 5 个既有用例会因找不到文件而假红（**已实测并修正运行目录**）。

| 臂 | 运行 | 结果 |
|---|---|---|
| **POST**（修复树） | `BrowserTabPoolDisposeScopeTest` | `OK (4 tests)` rc=0 |
| **PRE**（基线 blob `git show f94413f5:…/BrowserTabPool.kt`，独立目录 `/tmp/fix10/pre`） | 同上 | `Tests run: 4, Failures: 4` —— **4 条全红，且红点正是缺陷点** |
| 既有测试（POST） | `BrowserTabPoolSaveStateTest` + `BrowserTabPoolBusyPublishTest` | `OK (10 tests)` rc=0（**未回归**） |

**PRE 臂精确红的断言名**（逐条，非「一起红」）：
1. `disposeCancelsEveryPoolOwnedScope` → `AssertionError: dispose() never cancels 'evictionScope' — work already queued on that scope keeps running against a destroyed pool (a Main-dispatched restoreSavedTabs rebuilt WebViews nobody will destroy and re-persisted a just-deleted session's browser_tabs file)`
2. `disposalLatchIsVolatile` → `AssertionError: the disposal latch is missing entirely`
3. `bothAsyncContinuationsGuardOnTheLatch` → `AssertionError: saveState() does not short-circuit on the disposal latch — the continuation keeps working against a pool whose owner is gone`
4. `mutationsAreCaught` → `AssertionError: dispose() never cancels 'evictionScope' …`（自检族在基线树上同样红）

**外部变异臂**（在**独立副本树** `/tmp/fix10/m1`、`/tmp/fix10/m2` 上做，源文件全程不动，**未用 `git checkout --`**）：
| 臂 | 改动 | 结果 |
|---|---|---|
| m1 | 删 `evictionScope.cancel()` | `Tests run: 4, Failures: 2` —— 红在 `disposeCancelsEveryPoolOwnedScope` + `mutationsAreCaught`，**其余 2 条仍绿**（精度局部） |
| m2 | 删 `saveState()` 的闩锁守卫 | `Tests run: 4, Failures: 2` —— 红在 `bothAsyncContinuationsGuardOnTheLatch` + `mutationsAreCaught`，**其余 2 条仍绿** |

**内建变异自检**（`mutationsAreCaught`，在测试内对源码副本逐项删除）：5 个臂 —— 删 `evictionScope.cancel()` / 删 dispose 闩锁 / 删 saveState 守卫 / 删 restoreSavedTabs 守卫 / 去掉 `@Volatile` —— 要求 **caught == 5**；POST 绿、PRE 红（见上）。

### L4 CI
- 分支：`fix/dv24-b4-browser-dispose-scope`
- **run id（绿）**：`39893017433`（`gh-actions-dispatch` 手动触发 `build-apk.yml`，event = `workflow_dispatch`，2026-10-23T03:04:20Z）
- **run id（首跑红，已闭环）**：`39892349035` @ `903dc5bf` —— `compileReleaseKotlin` 报 `BrowserTabPool.kt:1636/1637 Unresolved reference 'cancel'`（`kotlinx.coroutines.cancel` 扩展未 import）。补 import（与 `AgentForegroundService` / `ProviderSpeechRecognitionEngine` 同惯例）后重推 ⇒ 绿。**这是「Python 门全绿 ≠ 编译通过」的又一实例：沙箱 JVM 装置只编译测试文件、不编译生产类** ⇒ 生产编译错误只能在 CI 层暴露。
- `head_sha` = `744cca3eae1dd2963ecc7d774d8b12b9c50cda8a`（== 本地 `git rev-parse HEAD` == `git ls-remote origin <branch>`，**三方逐字符一致**）
- **conclusion = success**；job id `120216922170`；steps 唯一 skipped = **`Publish to Releases`**（main 门控，分支上正确跳过）——**结论取自 steps API，不是日志文本 grep**
- **日志判读（1,393,622 B）**：
  | 判据 | 结果 |
  |---|---|
  | `Summary: 109 passed, 0 failed` | **恰一条**，全部 Summary 行集合 = `{25/0, 13/0, 10/0, 22/0, 5/0, 109/0}`，**无非零 failed** |
  | 门头 | `\[N/109\]` 出现 **109** 次，序号 **1..109** 连续，门名 **109 互异** |
  | `ALL 336 CASES PASS` | **恰一条**（夹具数未变） |
  | 编译错 `e: file:///` | **0** |
  | 游离 `FAILED`（不含 PASSED 子串） | **0** |
  | 新测试 | `BrowserTabPoolDisposeScopeTest` **4 条逐条 PASSED**（`bothAsyncContinuationsGuardOnTheLatch` / `disposeCancelsEveryPoolOwnedScope` / `disposalLatchIsVolatile` / `mutationsAreCaught`） |
  | 门注册 | `browser_tabs_lifecycle_check real tree green` 在册；`BROWSER_TABS_SELFTEST=PASS` 在册 |

### 三源取二
1. 沙箱实跑：门 53 臂带牙 + JVM PRE/POST 双臂（4 红 / 4 绿）+ 2 个外部变异臂 —— **独立于 CI 的证据**；
2. CI 绿（编译 + 全量门 + 336 夹具 + 单测）；
3. 邻门 6/6 + 既有 10 个既有用例绿。
⇒ **三源取二满足**（CI 绿 + 沙箱实跑两条独立成立）。

### 夹具注册表（`test_scan.py`）
本门在 `test_scan.py` 的注册行**未改**（`("browser_tabs_lifecycle_check", "BROWSER_TABS_SELFTEST=PASS", (), (REPO_ROOT,))`），标记串未变 ⇒ 夹具表**无需改**（这也是 COMMON §1「冻结面 0 触碰」的自然结果）。实测该表的 DA1025 分组单跑 ⇒ 末尾 `✅ browser_tabs_lifecycle_check real tree green`。

---

## §6 任务书失真

| 条 | 任务书说法 | 真树事实 | 处置 |
|---|---|---|---|
| 1 | 门 `browser_tabs_lifecycle_check.py:255-275` `check_pool_source` | 位置对（`check_pool_source` 在基线 `:258`），但它读的是**基线**文件；本支扩判据后该函数移到 `:376` 起 | 报告中一律以 HEAD 行号标注 |
| 2 | `:1603-1621` `fun dispose()` | 真树 `dispose()` 起于 `:1603`（基线），本支改后起于 `:1629` | 已按 HEAD 重取（见 §3） |
| 3 | 「`dispose()` 内补：`evictionScope.cancel()`、`downloadScope.cancel()`」 | 与真树一致；但**任务书没提 `downloadJobs` 的 job 级取消** —— 只 cancel scope 不会取消已注册 job 的**收尾逻辑**（`settleDownload` 写 StateFlow、`onDownloadEvent` 回调） | 已补（成本≈0、同函数同一段） |
| 4 | 「`restoreSavedTabs()` 与 `saveState()` 入口加 `if (isDisposed) return`」 | 采纳；但任务书没要求 **`@Volatile`**（只说 `@Volatile private var isDisposed`，在 §3 修法方向 1 有写）—— 保留 | 已按任务书 |
| 5 | 「`:1959` `evictionScope.launch { withContext(Dispatchers.Main) { restoreSavedTabs() } }`」 | 行号对（基线 `:1959`），**机制**成立：`withContext(Dispatchers.Main)` 一旦派发，父 scope 的 `cancel()` 撤不回来 | 修法据此设计（闩锁而非只 cancel） |
| 6 | 「§5 推荐把销毁判定抽成 internal 纯函数」 | 抽不出可测收益：真正的不变量是时序/顺序属性，纯函数表达不了；本仓本面已有形状测试惯例（`BrowserTabPoolSaveStateTest` / `BrowserTabPoolBusyPublishTest` 都是源码形状断言） | 采**退路**（形状测试 + 门带牙），并在 §2 写明理由 |
| 7 | 「`browser_cookie_ui_refresh_check.py` ……**不得改它**」 | 已遵守：该门 0 触碰，且本树仍 rc=0 | ✅ |
| 8 | 门「同步更新门内 `POOL_COMPLIANT` 夹具（`:372-383` 附近）」 | 真树 `POOL_COMPLIANT` 起于 `:359`、dispose 段在 `:372-383` 附近 —— **行号对** | 已同步 |

**另记 1 条（非任务书失真，属本支自伤）**：C4a 变异臂的插入点锚在 dispose 之后的后继文本上，夹具一改就失配 ⇒ 臂变假绿。已修（§4「自伤与修复」）。

---

## §7 未做 / 残留

| # | 项 | 触发条件 | 触发后方案 | 搁置理由 |
|---|---|---|---|---|
| **R1** | **真机行为级**：加载一个带持久化标签页的会话 → **立即删除该会话** → 检查 ①`filesDir/browser_tabs/<sid>.json` **不再重现**；②内存无持续增长（`adb shell dumpsys meminfo`，对比删除前后）；③无「下载完成后弹到已消失的聊天里」 | 下次真机窗口 | 一次开齐（与本批其它支的 R1 合并跑） | 沙箱无 Android 运行时；**不假装验证过** |
| **R2** | `downloadScope` 已写出的**下载文件**不回收（`filesDir/minis-sessions/<sid>/workspace/downloads/` 里已落盘的部分文件在会话删除后可能残留） | 用户反馈「删会话后磁盘没释放」或出现孤儿下载目录 | 在 `SessionFileStore.deleteSessionFiles` 的会话目录删除链里统一回收（**跨面**，属数据层） | 半径外：那是**会话目录**的删除链，不在浏览器池面内；本支只保证「池不再往那个目录写」 |
| **R3** | `BrowserTabPool` 的**共享实例**（`MinisApp.sharedBrowserTabPool`）永不 `dispose()` ⇒ 其 `evictionScope` / `downloadScope` 是进程级的，本来就该活着 | 若将来给共享池加 dispose 路径 | 同一套闩锁即可（`isDisposed` 是实例字段，共享池不 dispose 就不受影响） | 现状正确：共享池是进程级单例，dispose 语义不适用 |
| **R4** | 闩锁**只**覆盖 `saveState` / `restoreSavedTabs` 两个续体；其它可能在 dispose 后碰池子的路径（如 `handleNewWindow` / `handleCloseWindow` 的 `saveState()`）靠 `saveState` 自己的守卫间接覆盖 | 将来新增第四个「dispose 后仍可能执行」的续体 | 门 C4f 的「两个续体」判据扩为派生集合 | 现状：这两个续体是**唯一**在 dispose 后还能越过 Main 派发闸的（`withContext(Dispatchers.Main)` 竞态）；其余路径同步执行，dispose 后不会再被调用 |
| **R5** | `onDownloadEvent` 被置空后，正在 `downloadScope` 上跑完的下载会**静默丢弃**最终事件（用户看不到「下载完成」） | 用户反馈「下载完成没有通知」 | 把终态事件写入 `_downloads` StateFlow（已有）并让 UI 在池 dispose 时不读 | 池已死、VM 已死，事件本就无处可去；丢弃优于回调已死的 VM |
| **R6** | `dispose()` 里的 `downloadJobs` 遍历 cancel 不保证**正在写文件**的协程立刻停（cancel 是协作式的，IO 阻塞点不立即响应） | 真机出现「删会话后下载文件仍在增长」 | 下载写盘改走可取消的通道（`ensureActive` 检查点） | IO 取消的协作式语义是平台事实；本支不引入新抽象层 |
| **R7** | 门 C4f 的「两个续体」是**写死的两个函数名** | 池里新增第三个异步续体 | 把续体集合改为从「在 dispose 之后仍可能被派发」派生 | 当前池的异步续体只有这两个（`restoreSavedTabs` 与 `saveState`）；写死成员清单是门的固有局限（C10 已有同款「派生」做法，本条留待扩展） |
| **R8** | `scan.sh` / `test_scan.py` 冻结面**未改** ⇒ 本门新增的 7 个臂只在 `--selftest` 里跑，`test_scan.py` 的 DA1025 分组只断言「selftest 标记存在 + 真树绿」 | 需要更细的逐臂断言 | 改 `test_scan.py` 需主会话授权（冻结面） | COMMON §1 明确冻结 |

---

## §8 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f94413f52fa5403c903902f6b1d2ae10cb9bc906` |
| 本地 HEAD | `744cca3eae1dd2963ecc7d774d8b12b9c50cda8a` |
| `git ls-remote origin fix/dv24-b4-browser-dispose-scope` | `744cca3eae1dd2963ecc7d774d8b12b9c50cda8a` |
| CI `head_sha` | `744cca3eae1dd2963ecc7d774d8b12b9c50cda8a` |
| **四方逐字符一致** | ✅ |
| CI run id（绿） | **39893017433**（`build-apk.yml`，`gh-actions-dispatch` 手动触发，event = `workflow_dispatch`，2026-10-23T03:04:20Z） |
| CI run id（首跑红，已闭环） | `39892349035` @ `903dc5bf` —— 编译错 `Unresolved reference 'cancel'`（缺 `kotlinx.coroutines.cancel` import） |
| CI job id | `120216922170` |
| CI conclusion | **success**（唯一 skipped = `Publish to Releases`，main 门控） |
| 提交序列 | `479efc2a` 生产（+54/−0）→ `86ca5219` 测试（+253/−0）→ `903dc5bf` 门（+252/−3）→ `744cca3e` 补 import（+1/−0） |
| 门/夹具计数 | **109 / 336 不变**（0 新建门；`scan.sh` / `test_scan.py` 0 触碰） |
| 合并状态 | **未合并 main**（停止节点 = 分支 CI 绿；起手权在用户） |

### 一次自伤（已闭环，登记在此备查）
`file_write` 覆盖 `shared/backlog.md`（整份被我的 19 行替换）—— **与 1043 波 FIX-9 同款事故的第三次复发**（该工具自带 `shared_append.sh` 正是为此而生，我漏用了）。恢复路径：最新快照 `backlog.md.bak-20261023-030818`（356 段）→ 补回被快照漏掉的 FIX-3 段 → 用 `tools/shared_append.sh` 追加本支段（段数守卫 357 → 358）。**backlog 现为 358 段，无内容丢失**。

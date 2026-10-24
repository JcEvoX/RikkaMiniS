# dv29-FIX-B2 — 后台协程直写 Compose `mutableStateOf`（B-02）

## 1 身份链

| 项 | 值 |
|---|---|
| 基线 main | `b82aa8dc`（worktree `git rev-parse --short HEAD` 逐字符一致，开工时 `git status --porcelain` 为空） |
| 分支 | `fix/dv29-b2-offmain-compose-state` |
| 提交序列 | `0a72a3ae` 生产修复（3 文件 +41/−7）→ `1261bd2c` 新测试（1 文件 +276）→ `10e544ad` 门扩展（1 文件 +459/−48）→ `82373a43` 测试去掉一条可空性警告（1 文件 +3/−1）→ 本报告（仅 `fix-reports/`） |
| 代码面 sha（CI 绿） | `82373a435b25672c14ff562fc363252af8d8e3a2`（本地 == `git ls-remote` == CI `head_sha` 三方逐字符一致） |
| 门/夹具总数 | **111 / 342 不变**（0 新建门；`scan.sh` / `test_scan.py` 零触碰） |

## 2 缺陷与先证

### 2.1 三条工单逐条回树核实

| 报告断言 | 真树 | 判定 |
|---|---|---|
| `ImageGalleryViewer.kt:320-325,356-361` 在 `applicationScope`（IO）的 `finally` 里写 `sharing`/`saving` | `:324` `sharing = false`、`:360` `saving = false`（修复前）；`scope` 来自 `:119-120` `(context.applicationContext as? MinisApp)?.applicationScope ?: rememberCoroutineScope()` | **成立**（行号精确） |
| `BackupSettingsScreen.kt:881,916-918` 在 `applicationScope.launch` 里写 `errorMessage` | `:918` `errorMessage = context.getString(R.string.webdav_server_not_configured)`，位于 `:881 application.applicationScope.launch {` 的 `try` 体内、`:894 withContext(Dispatchers.IO)` 之后 | **成立**（行号精确） |
| `ChatScreen.kt:3426-3430` 在 `launch(Dispatchers.Default)` 里写 `lastColdPrewarmMs` | `:3426 launch(Dispatchers.Default) {`；`:3430 lastColdPrewarmMs = prewarmMs` | **成立**（行号精确） |
| 判据 F 只识别 `withContext(Dispatchers.IO\|Default)`，漏 `applicationScope.launch` / `launch(Dispatchers.Default)` | 修复前 `OFF_MAIN_CALL`（:467）只有 `withContext\(\s*Dispatchers\.(IO\|Default)\s*\)\s*\{`；`off_main_writes`（:633）只按它取块 | **成立** |

报告失真清单：**无**。四个行号、三处符号、`OFF_MAIN_CALL` 正则全部与真树逐字符吻合；唯一的口径补充是「3 处「实为 **4 个写点**（画廊两个标志各一处）」。

### 2.2 机制链（可执行）

1. `MinisApp.kt:83` `val applicationScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)` —— 无 Looper 的 IO 线程池。
2. 三个消费方把「完成后复位/报错/发布计时」写在普通赋值里，协程体跑在 IO / Default 上 ⇒ 赋值发生在后台线程。
3. 被写对象是 `var … by remember { mutableStateOf(…) }`（`ImageGalleryViewer:319/354`、`BackupSettingsScreen:113`、`ChatScreen:3218`），即 Compose snapshot state。
4. 与主线程重组并发时读取/写入该 state 可抛 `IllegalStateException: Reading a state that was created after the snapshot was taken`（进程崩溃）；即便不抛，后台写也会让「按钮禁用旗标」在帧间读到撕裂值。
5. 门失明 ⇒ 该形状自 1045 起一直被 F 判据视为「干净」。

### 2.3 判级

维持 **P1**（报告判级）。触发路径可指认：分享/保存图片、WebDAV 立即备份未配置服务器、会话冷启动预热的完成时刻——都是常用路径的可见交互（不是一次性迁移、不是 debug 通道）。**可达性三查**：①UI 入口存在（三处都在常用屏）；②调用点全仓计数见 §4；③无旁路通道需要论证（本就是 UI 路径）。

## 3 修法（阶梯）

**生产侧停在第 6 级（最小实现）**：不新增抽象、不引入 `Snapshot.withMutableSnapshot`、不改 UI 结构，三处各加一次 Main hop：

- `ImageGalleryViewer`：两个 `finally` 内改
  `withContext(NonCancellable + Dispatchers.Main.immediate) { sharing/saving = false }`。
  `NonCancellable` 是刻意的：`finally` 的语义是「无论成功失败都复位」，被取消时若裸 `withContext` 会立刻抛 `CancellationException` 而跳过复位（这正是报告要求保住的那条语义）。
- `BackupSettingsScreen`：把「读 `webDavConfig` + 未配置时报 `errorMessage`」并进同一个 hop
  `val cfg = withContext(Dispatchers.Main) { … }`。原注释一边宣称 "Resolve config on Main"，一边跑在 IO 上；读也是 Compose state，所以读和写一起搬，一次切换。
- `ChatScreen`：预热计算仍留在 `Dispatchers.Default`，只把发布搬进
  `withContext(Dispatchers.Main.immediate) { lastColdPrewarmMs = prewarmMs }`（把整段搬回 Main 会把解析重新压回首帧路径）。

**门侧停在第 4 级（扩既有门，不新建）**：判据 F 的「离主线程块」集合从派生得到——`withContext(IO|Default)` + `launch(IO|Default)` + `<scope>.launch`，其中 scope 的 dispatcher 由树派生（`CoroutineScope(…)` 构造里带 `Dispatchers.IO|Default`，外加每文件的别名解析），Main 隐含的 scope 由**创建它的 API**（`rememberCoroutineScope()` / `viewModelScope` / `lifecycleScope` / `MainScope()`）与**显式 `Dispatchers.Main` 实参**双重排除。嵌套按**最内层块**判定：`withContext(Dispatchers.Main) { … }` 嵌在离主线程 launch 里就是安全——正是本次修法的形状。逃生口 `// offmain-state-ok: <理由>`（沿用已有 `unreachable-defense:` 的同族风格）。

**刻意保留（§7 P3 记）**：`e.also` 式的单表达式写法没有采用，改成显式 `val current = …; if (…) …; current`，因为门的行级判据要能一眼看出「读和写在同一个 hop 内」。

## 4 覆盖面（同族普查）

派生口径（`sysinfo_state_thread_check.py` 自身的派生结果，非手数）：

| 量 | 修复前口径（只认 `withContext`） | 本支口径（+ launch 型） |
|---|---|---|
| 生产 `.kt` 文件 | 638 | 638 |
| 声明 `remember` 状态的文件 | 77 | 77 |
| 其中同时含离主线程块的文件（= 判据 F 作用域） | **30** | **45**（+15，无一个被丢） |
| 违规写点 | 未检出（门失明） | **4** → 修复后 **0** |

`+15` 的新增覆盖面（全部为绿的合规文件，仅列表以备下次同族排查）：
`MainActivity.kt` · `ui/browser/BrowserSettingsSheet.kt` · `ui/browser/BrowserSheet.kt` · `ui/chat/ChatHistoryDrawer.kt` · `ui/chat/ChatMiscViews.kt` · `ui/components/ImageGalleryViewer.kt` · `ui/onboarding/OnboardingModelSelectionScreen.kt` · `ui/settings/A11yDiagnosticsUi.kt` · `ui/settings/ModelGroupDetailScreen.kt` · `ui/settings/MountDetailScreen.kt` · `ui/settings/MountedFoldersScreen.kt` · `ui/settings/OffloadPermissionScreen.kt` · `ui/settings/ScheduledTaskEditScreen.kt` · `ui/settings/ScheduledTasksScreen.kt` · `ui/settings/SystemPermissionsScreen.kt`

**同族第 4、5 处：0 处**（新口径下全树只剩这 4 个写点，全部在本支文件内并已修）⇒ 无需登记 R 条目。派生出的「离主线程 scope 名」共 6 个（`applicationScope`/`backgroundScope`/`downloadScope`/`evictionScope`/`recoveryScope`/`scope`），其中 5 个属非 UI 层、其文件不含 Compose 状态。

`BackupSettingsScreen` 内部另有一族：`errorMessage` 共 17 个写点，逐个回看后只有 `:918` 落在应用作用域；其余在 `scope.launch`（`:129 rememberCoroutineScope`，Main 隐含）或 `withContext(Dispatchers.Main)` 内——这一轮由新判据自动复核（全树 0 命中即证据）。

## 5 验证四层

### L1 静态门（本门）

- 真树：`python3 scripts/scan/sysinfo_state_thread_check.py .` → **rc=0**（30 s，PRoot 税）。
- selftest：`--selftest` → **32 臂全 PASS** + 标记 `SYSINFO-STATE SELFTEST=PASS`；原 22 臂全绿未改，新增 10 臂：

| 新臂 | 断言 |
|---|---|
| the three launch-shaped screens are green as written | 三个新夹具本身 0 违规 |
| a state reset in an app-scope finally is red (one point only) | 只红 `sharing` 一点，且断言在 `finally` 内 |
| the save-album reset is red on its own point only | 只红 `saving` 一点 |
| errorMessage written from an app-scope launch is red | 只红 `errorMessage` 一点 |
| launch(Dispatchers.Default) writing remember state is red | 只红 `lastColdPrewarmMs` 一点 |
| reverting all four sites reddens exactly those four | 名字集合恰为 `{sharing,saving,errorMessage,lastColdPrewarmMs}` |
| the offmain-state-ok escape hatch silences one site | 逃生口生效且只影响该点 |
| an explicit Main dispatcher is not off-main | `launch(Dispatchers.Main.immediate)` 不误红 |
| a Main-backed application scope is not off-main | scope 改由 Main 支撑后不误红（排除「把所有 launch 一律算 off-main」） |
| deleting a launch-shaped anchor file is red | 删掉锚点文件 → 响亮失败，不是静默缩面 |

- **PRE 基线树**（`probe/pre-tree` = 全量源码副本 + 三个文件回滚到 `b82aa8dc`）跑同一门 → **rc=1**，四条违规逐字命中真树行号 `ImageGalleryViewer.kt:324` / `:360`、`BackupSettingsScreen.kt:918`、`ChatScreen.kt:3430`。

### L2 邻门

三个文件被 13 道门引用，逐门 `python3 scripts/scan/<门>.py .` 全 **rc=0**：
`mediaplayer_release_guard` · `webview_holder_url_source_check` · `backup_streaming_export_check` · `provider_remember_key_check` · `readonly_mount_write_guard` · `chat_session_id_binding_check` · `chat_ui_interaction_check` · `deeplink_settings_routes_check` · `file_provider_paths_coverage_check` · `file_uri_parse_guard_check` · `legacy_pipeline_guard` · `session_sheet_id_binding_check` · `stale_closure_guard`。

### L3 真 kotlinc + JUnit（沙箱实跑）

- 装置：`/opt/kotlinc/bin/kotlinc 2.0.21`（JRE 17）+ `/opt/jvm-libs/junit-4.13.2.jar` + `hamcrest-core-1.3.jar`，`-classpath` 显式带；`java org.junit.runner.JUnitCore`。
- **POST（本支树）**：`OK (6 tests)`。
- **PRE（基线树，`-Duser.dir=probe/pre-tree`）**：`Tests run: 6, Failures: 5` —— 4 条 hop 臂全红，`galleryWorkStaysAppScoped` 因其 `NonCancellable` 子断言是本次新增属性而红，`backupFailureStillNotifies` 保持绿（正确：该臂锁的是**未改动**的行为，PRE 本来就该绿）。
- 这组对照证明两半都不空转：只把 hop 加对，`launch(Dispatchers.Default)` 与 `?.applicationScope` 两个「仍然离主 / 仍然应用作用域」的断言会拦住「干脆全搬回 Main」的偷懒修法。

### L4 分支 CI

- run **`40071869246`** / job **`120806858674`**，`event=workflow_dispatch`，**attempt 1**，`conclusion=success`；`head_sha = 82373a435b25672c14ff562fc363252af8d8e3a2`（与本地 HEAD、`git ls-remote` 逐字符一致）。
- 步骤：24 步，**not-success = []**；唯一 `skipped` = 步骤 23 `Publish to Releases`（**从 steps API 判定**，日志里无该行）= main 门控。
- 日志判读（`probe/judge_ci.py` 对 `probe/job2.log` 1,462,213 B / 17,606 行）：
  - `Summary:` 行 8 条，其中恰一条带框线 `║  Summary: 111 passed, 0 failed`，**其余 7 条 failed 全为 0**；
  - `ALL 342 CASES PASS` 恰 1 次（无其它 `ALL <n> CASES PASS`）；
  - 门头 `━━━ [n/111] ` 共 **111** 条、编号 1..111 无重复；
  - 编译错 ` e: ` **0**；编译警告 ` w: ` **376**（上一轮同一提交集为 377 —— 本支最后一条提交正是削掉那一条）；
  - 含 `FAILED` 的行 6 条，**逐条确认都是 `PASSED` 用例名的子串**（游离 0）；
  - 本支新测试 **6/6 PASSED**（`OffMainComposeStateWriteTest > …` 逐条在册）；
  - 本门两行在册：`✅ sysinfo_state_thread_check selftest has teeth (SYSINFO-STATE SELFTEST=PASS)` + `✅ sysinfo_state_thread_check real tree green`。
- **警告对照（本支文件单独查）**：` w: ` 里命中本支四个文件的只有 **3 条**，全部是 `ChatScreen.kt:2973:60 'val Icons.Filled.Undo …' is deprecated`（既有、与本次改动行无关）；新增测试文件与另两个生产文件 **0 条**。
- **源码树等价**：本报告与上述绿 sha 之间只差 `fix-reports/dv29-FIX-B2.md` —— `git diff --name-only 82373a43 HEAD -- . ':(exclude)fix-reports'` 输出 **0 行**。`fix-reports/` 不在 `build-apk.yml` 的 paths 内、不参与构建。
- **第一轮 CI**（`40070250132` / job `120801944291` @ `10e544ad`，同样 success）暴露了本支唯一一处自伤：新测试 `repoFile` 把 `System.getProperty("user.dir")`（平台类型 `String!`）直接喂给 `File(...)`，CI 输出 ` w: …OffMainComposeStateWriteTest.kt:54:31 Java type mismatch`。**CI 绿不等于无警告**——`?: "."` 修掉后重新 dispatch，第二轮才把警告数从 377 降回 376。


## 6 残留（R 条目，每条带触发条件 + 触发后方案）

- **R1 真机行为级未验**（三处 Main hop 的实际效果：分享/保存按钮禁用旗标不残留、WebDAV 未配置提示即时出现、冷启动预热耗时仍被记录）。触发条件 = 下次真机窗口；触发后方案 = 连点分享/保存各 3 次看胶囊按钮不复位卡死；备份屏在未配置 WebDAV 时点「立即上传」看内联错误文案；开新会话进首屏后查 `PerfLongCtx coldPrewarm.done` 行仍带 `prewarmMs=`。
- **R2 `applicationScope` 的 IO 绑定是「派生自单点声明」**。触发条件 = 有人把 `MinisApp.applicationScope` 改成 `Dispatchers.Main` 或换成别的名字；触发后方案 = 门会自动跟随（同名声明改 Main → 该 scope 不再算 off-main；改名 → 新名字由同一派生逻辑覆盖），但 `EXPECTED_OFF_MAIN_SCOPE` 的三条新锚点需要人工复核是否仍成立（改 Main 后三个文件将不再「含离主线程块」→ 门红并提示）。
- **R3 判据只认「赋值」不认「读」**（`webDavConfig` 的后台读同理不安全，本支在修写的时候顺手把它一起搬进 hop，但门不检查读）。触发条件 = 再次出现「后台读 Compose 状态」的用户可见异常（如 `IllegalStateException: Reading a state…`）；触发后方案 = 把 F 判据扩成「离主线程块内不得读 `remember` 派生变量」（需先做误报普查：`derivedStateOf`、`snapshotFlow`、`currentState` 都会踩）。
- **R4 `scope` 作为全局派生名有潜在误报面**：`ui/` 内若出现「`val scope = <某个 IO scope 的别名>` 但语义是 Main」的写法会被误判。当前全树 0 误报（45 个文件全绿、32 臂全绿）。触发条件 = 未来某支门红但人工判定是误报；触发后方案 = 在该行加 `offmain-state-ok: <理由>`，或把别名声明写成 `rememberCoroutineScope()` 使 per-file 遮蔽生效。
- **R5 门把 `launch { … }`（无参与者、无显式 dispatcher）视为「继承外层」**，因此外层不是离主块时不会被检查。触发条件 = 出现 `GlobalScope.launch { … }` 一类隐含离主的写法；触发后方案 = 把 `GlobalScope` 加入派生根（它本身不是 `CoroutineScope(…)` 构造，需单独派生）。
- **R6 `NonCancellable` 的取舍**：`sharing`/`saving` 的复位在取消时仍执行（保语义），代价是取消后仍要做一次 Main 投递。触发条件 = 真机上出现「退出画廊后按钮短暂禁用」的观感问题；触发后方案 = 去掉 `NonCancellable`（在 `Dispatchers.Main.immediate` 上取消即抛，复位被跳过，但对象已随组合销毁，无实际影响）。
- **R7 `PerfLongCtx.step` 仍在 Default 线程**（未搬 Main，避免给诊断路径加一次切换）。触发条件 = 若该 step 内部触碰 Compose/主线程独占资源；触发后方案 = 一并搬进同一个 hop（代价可忽略）。

## 7 未验证边界

- **未在真机验证**（无本次窗口，见 R1）——三处 hop 的**线程语义**由 kotlinc 编译 + 源形状 + 门三级覆盖，但「用户可见行为不变」只能真机确认。
- **未跑本地全量 `scan.sh` / `test_scan.py`**（COMMON §5：施工期不跑，分支 CI 会跑）。本地只跑了本门 + 13 道邻门。
- **未验证 Compose 侧是否曾真的崩过**：本支的证据链是「形状不安全 + 竞态可解释」，不是「已复现崩溃」。崩溃是否发生取决于重组与后台写的时序，日志里未见 `Reading a state that was created after the snapshot was taken`（`/var/minis/logs` 全目录 `grep -rl 'created after the snapshot was taken'` = 0 个文件命中）——所以这是**消除了一个未触发的崩溃面**，而不是修一个已发生的事故（不影响 P1 判级：同一屏的高频交互路径上存在可解释的崩溃面）。

## 8 附：装置与产物

- 门：`scripts/scan/sysinfo_state_thread_check.py`（判据 F 重写 + 10 臂；`EXPECTED_OFF_MAIN_SCOPE` 5 → 8 条锚点）。
- 测试：`src/android/app/src/test/java/com/rikkaminis/app/ui/OffMainComposeStateWriteTest.kt`（6 用例，纯 JVM，自实现字符串感知的 `codeOnly`）。
- 探针目录（worktree 私有，`.gitignore` 覆盖 `probe/`）：
  - `probe/census.py` —— 判据原型（先在真树算「命中面」，再动门）。
  - `probe/pre-tree/` —— 基线树（638 个 `.kt` 全量副本；三个文件回滚到 `b82aa8dc`），门与 JVM 测试的 PRE 臂都跑它。
  - `probe/f_block.py` —— 落进门里的判据实现原文（可 diff 复核）。
  - `probe/jvm-out/`、`probe/kotlinc.log` —— L3 产物。
  - `probe/ci-wait.log` —— L4 原始输出。

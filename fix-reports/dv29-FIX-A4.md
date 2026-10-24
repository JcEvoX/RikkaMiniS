# dv29-FIX-A4 — 外部写盘到不了运行中的消费者：`minis-config set` 的写后送达

基线 main `b82aa8dc` · 分支 `fix/dv29-a4-prefs-write-through` · 工单 A-04（真机 P1，§2597-VERIFY-2 R4）
门：`scripts/scan/prefs_listener_holder_guard.py`（只扩，0 新建门）· 停止节点 = 分支 CI 绿

---

## 1 缺陷与真树取证（自己跑出的行号）

### 症状原话（用户/验证会话报告）

> `minis-config set background.overlay true` → `minis-config get` 读回 `true`，但运行中的
> `AgentForegroundService` 日志仍 `enabled=false`；反向亦然；`am force-stop` 重启后两边一致。

### 真树链路（修前，逐环自己的行号）

| 环 | 位置（基线 `b82aa8dc`） |
|---|---|
| 干的写路径 | `ConfigBuiltins.kt:1428` `registerBackground(r, context)` → `:1483` `key = …KEY_BG_OVERLAY_ENABLED` → `PrefsFields.kt:34-37` `override fun write(...)` → `prefs.edit().putBoolean(key,b).apply()` |
| prefs 句柄 | `BackgroundSettingsRepository.kt:21-22` `context.applicationContext.getSharedPreferences("background_settings", MODE_PRIVATE)`；registry 侧同一句柄（`ConfigRegistry.init(this…)` → `registerInto(r, context)`，`this` = `MinisApp`） |
| 活消费者 | `MinisApp.kt:887` → `BackgroundSettingsRepository.kt:75-80` StateFlow → `AgentForegroundService.kt:343` `combine(…)` → `:416` `applyOverlayState(state)` 日志 |
| 唯一刷新层 | `BackgroundSettingsRepository.kt:110-123` `prefsListener` + `init { prefs.registerOnSharedPreferenceChangeListener(prefsListener) }` |

### 真机复现（本次自己跑的，app `1.0.0+2597`）

```
13:46:45.976 ToolChain[VM](23883)   ToolCallComplete name=shell_execute args={"command":"minis-config set background.overlay true …
13:46:46.140 NativeOffloadServer(23883) recv pid=23768 name='minis-config' argc=4 argv=[minis-config, set, background.overlay, true] cwd=/tmp
13:46:46.126 AgentForegroundService(23883) applyOverlayState fg=false enabled=false perm=true …
13:46:46.405 AgentForegroundService(23883) applyOverlayState fg=false enabled=false perm=true …
13:46:46.494 AgentForegroundService(23883) applyOverlayState fg=false enabled=false …
13:46:48.408 AgentForegroundService(23883) applyOverlayState fg=true  enabled=false …
13:46:50.403 AgentForegroundService(23883) applyOverlayState …            enabled=false …
（写入后紧跟 `minis-config get background.overlay` → "true"）
```

**同一 pid 23883**：CLI handler 与 FG service 同进程 ⇒ 不是跨进程读不到。

---

## 2 先证：断点结论 + 六条候选逐条判决

### 结论 = **(a) 断点定位在「外部写盘 → 运行中消费者」这一段唯一的送达通路上，且该通路在真机上没有送达**

- 写入确实生效且落在 **registry 字段自己的 prefs 句柄**上：写后 `get` 读回 `true`（同一 `PrefsBoolField` 的 `prefs`）。
- 消费者确实**没拿到**值：`enabled=false` 在写入后 2.3 s / 4.5 s / 数分钟内每次重算都成立。
- 两个方向都复现（`true→false` 亦然），且**本次又复现两次**（`13:46` 写入窗口、`13:55:30` 二次写入窗口）。
- 因此「写盘成功 + 活消费者旧值」成立。缺的那一环是**送达**，而不是写、不是读、不是采样。

### 候选逐条判决

| # | 候选 | 判决 | 证据 |
|---|---|---|---|
| 1 | 写到的不是同一个 prefs 文件/实例 | **代码层面排除，运行时无法直接证伪**（见下方「有界未知」） | 同名 `"background_settings"`、同 `MODE_PRIVATE`、同包、同进程（pid 23883）；两侧都从 Application context 取（`ConfigRegistry.init(this)` 与 `MinisApp:887` 的 `this`）；AOSP `ContextImpl` 的 prefs 缓存是进程级静态缓存 |
| 2 | listener 未被强引用 / 被 GC（F-134 形状） | **排除** | `prefsListener` 是实例字段（修前 `:110`）；`BackgroundSettingsRepository(` 全仓**仅 1 处构造**（`MinisApp.kt:887`），进程级单例 ⇒ 强引用稳定存在 |
| 3 | listener 注册后被摘掉 | **排除** | 全仓 `unregisterOnSharedPreferenceChangeListener` 命中 **14 处**，逐条核对：13 处是 Compose `onDispose` 摘**自己刚注册的** listener（`MainActivity` / `ChatActionState` / `ChatScreen` / `AppearanceScreen` / `ChatMenuSettingsScreen` / `ChatTuningScreen`×2 / `LogManagementScreen` / `MemoryManagementScreen` / `ModelGroupsScreen` / `RuntimeLimitsScreen`×3），第 14 处是 `EncryptedPrefsFactory` 的 no-op 桩。**没有任何一处能拿到 repository 的私有 listener**（`unregister` 需同一实例） |
| 4 | listener 触发了但 key 不匹配 | **排除** | 注册字段的 key 与 listener 的 `when` 都引用 `BackgroundSettingsRepository.KEY_BG_OVERLAY_ENABLED`（`ConfigBuiltins.kt:1483` / 修前 `:118`），伴生常量单源（F-238/F-240 已收口）；本次新增 `BackgroundToggleKeyTest` 用**字面量** `"backgroundOverlayEnabled"` 钉住映射 |
| 5 | 不是 prefs 问题，而是消费者没被驱动 / 日志只在边沿打（采样错位） | **排除** | `applyOverlayState` 在 `distinctUntilChanged()` 之后对**每次状态变化**都打一行（`AgentForegroundService.kt:416+`）：`13:46:46.126 → .405 → .494 → 13:46:48.408 → 13:46:50.403` 连续 5 行说明上游持续重算，而每次重算 `enabled` 都是 `false` ⇒ 不是「只在边沿打」、也不是「根本没重算」，是**输入流里的值一直是 false** |
| 6 | 写路径没走到 `PrefsBoolField.write`（collection/closure/override 层） | **排除** | `ConfigBridge` 的 `performWriteBatch` 对 `ConfigField` 一律 `r.field.write(r.newValue)`（`ConfigBridge.kt:599`）；返回体的 `old` 值来自同一字段的 `read()`（写前 false）、写后 `get` 为 true；`background.overlay` 是 `PrefsBoolField`（`ConfigBuiltins.kt:1470-1490`），非 collection、非 closure、无 override 层 |

### 有界未知（记录在案，不进修复路径）

**「listener 未触发」与「registry / repository 各持一个 `SharedPreferences` 实例」这两种子因，在设备上无法在不改码的前提下区分**：

- 若是子因 A（callback 未触发）⇒ 候选 1 成立、2/3/4 已排除，却与「同实例 ⇒ callback 必发」矛盾；
- 若是子因 B（两个实例）⇒ 一切都自洽，但与 AOSP 的进程级静态 prefs 缓存矛盾。

要定死只需一次仪表化构建（debug 包，两行日志）：两侧各打 `System.identityHashCode(prefs)` + listener 内部自增计数器。**本支不改码去区分它**——因为修法对两种子因不变（见 §3），继续区分只增加装置而不改变结论。

---

## 3 修法（改动阶梯）

**停在第 2/6 级**：代码库里已有同形状先例 `config/SessionConfigWriteThrough.kt`（`[dv24-A05]`：`session.*` 写进 Room 行，驻留 VM 仍用 `loadSession()` 时的值 ⇒ 改成写路径主动送达）。本次做的是把同一原理搬到 prefs 层，新增 1 个机制文件 + 3 处接线，不新建抽象层。

| 层 | 改动 |
|---|---|
| 机制 | **新** `config/PrefsWriteBus.kt`（106 行）：`fun interface Sink { onPrefsWrite(prefs, key) }` + `register/unregister/publish`。`publish` 把**写入方的 prefs 句柄**与 key 一起交给 sink —— 这条设计决定是关键的：sink 若读自己缓存的句柄，在「两实例」子因下仍是旧值；读交来的句柄在两种子因下都正确。异常被包含（写已经落盘，一个消费者的 bug 不得把成功的配置写变成失败，也不得饿死后面的 sink），失败走 `onSinkFailure`（默认 `AppLogger.warning`，可注入以便 JVM 测试观测） |
| 写路径 | `config/fields/PrefsFields.kt`：四个 `write()` 末尾 `PrefsWriteBus.publish(prefs, key)`（Bool/Int/String/IntCodedEnum）。pub 在 `apply()` **之后**（sink 在回调时就能读到新值，`m3` 变异臂钉住这个顺序）。其它使用方行为不变：`read()` 未动、写入语义未动，只多一次进程内通知；无 sink 时 `publish` 直接 return |
| 消费者 | `data/repository/BackgroundSettingsRepository.kt`：抽出 `internal class BackgroundToggleCache`（两个 StateFlow + 唯一的 key→state 映射 `applyWrite(source, key)` + 生产侧的 `prefsListener` / `writeSink`），repository 只负责 `register` 两者。**sink 与 listener 都建在 cache 内部**——若留在 repository 接线处，一行 `applyWrite(prefs, key)` 就能悄悄退回到「读自己的句柄」，而这正是真机上的失效形状；放在 cache 内部后这条缺口**结构上不可表达**，且被 `BackgroundToggleCacheTest` 覆盖。被动 listener 保留为兜底（UI setter / 原始 key 恢复等不走 field 的写） |
| 未动 | `ConfigBuiltins.kt`（键名/默认值继续只来自伴生常量）、`ConfigRegistry.kt`（revert/audit 语义不变：`revert` 走 `field.write(old)` ⇒ 现在也会送达，是收益不是风险）、UI/Service 类型未进 config 层（`PrefsWriteBus` 只 import `android.content.SharedPreferences` + `logging.AppLogger`）、无广播/跨进程方案 |

**保住的三条**：`setBackgroundOverlayEnabled()` 直改 StateFlow 的路径（`BackgroundToggleCacheTest.inAppSetterStillDrivesTheFlow`）；`background.notifications` 语义（同一条映射，键与默认值不变）；`PrefsIntField`/`PrefsStringField` 行为（只加 publish，`PrefsWriteBusTest.intAndStringWritersPublishToo` 断言写入结果不变）。

---

## 4 改动清单

```
 scripts/scan/prefs_listener_holder_guard.py                     | 283 +++++-
 src/android/app/src/main/java/…/config/PrefsWriteBus.kt         | 106 ++++  (新)
 src/android/app/src/main/java/…/config/fields/PrefsFields.kt    |  14 +
 src/android/app/src/main/java/…/repository/BackgroundSettingsRepository.kt | 165 +++--
 src/android/app/src/test/java/…/config/FakeSharedPreferences.kt |  88 ++++  (新)
 src/android/app/src/test/java/…/config/PrefsWriteBusTest.kt     | 178 ++++  (新)
 src/android/app/src/test/java/…/repository/BackgroundToggleCacheTest.kt | 106 ++++ (新)
 src/android/app/src/test/java/…/repository/BackgroundToggleKeyTest.kt  |  44 ++++  (新)
 8 files changed, 951 insertions(+), 33 deletions(-)
```

生产代码净增 ~180 行（含 KDoc），测试 416 行，门 283 行。

---

## 5 门与判据（只扩一门）

`prefs_listener_holder_guard.py` 新增 **C10a/C10b**（全部从树派生，无成员清单、无行号）：

- **C10a**：每个 `class Prefs*Field` 的 `override fun write()` 体必须以 `PrefsWriteBus.publish(` 收尾 ⇒ 新变体只 `apply()` 就红。附带兜底：若全树 `publish(` 归零而 writer 仍在 ⇒ 报「锚点被改名/移除」。
- **C10b**：对「`MutableStateFlow(<prefs>.getBoolean(<KEY>, …))` 快照」与「`Prefs*Field(key = …<KEY>)` 注册」的**交集 key**，必须有 `PrefsWriteBus.Sink { }` 体外貌**提到该 key**（允许一跳委派：sink 调用的函数体提到 key —— 本支 `refreshFromWrite` 就是这种委派，判据按一跳解析，不逼 `when (key)` 内联）。⇒ 删掉接线红；key 拼错红（`(?<!\w)KEY(?!\w)` 边界，`KEY_X_V2` 不算覆盖）。
- 漂移检测：快照 key 存在但全树无 sink ⇒ 红（write-through 半边被删）。

**带牙 5 臂**（原有 20 臂全绿，共 25）：合规树绿（"is delivered by the sink at"）· sink 缺失红 · **key 拼错红** · writer 只 apply 不 publish 红 · 精度控制（快照 key 未注册 ⇒ 不属本判据范围，绿）。

真树结果：`OK: write-through delivery green (2 [dv29-a4] delivery line(s))` —— 交集正好是 `background.overlay` / `background.notifications` 两个 key（全仓 `MutableStateFlow(prefs.get…)` 只有这两处，`BackgroundSettingsRepository.kt:53/75`）。注册字段总数 70，其中快照型 2 ⇒ 判据范围自证收敛，不是「漏覆盖」。

---

## 6 验证四层

**L1 门 + 夹具（本地）**
- 本门真树：`rc=0`，五段 OK（19 处 listener 强引用 / 7 处 re-priming / 3 个自同步 cache / 3 处 batch-save / **2 处 write-through**）；`--selftest`：`PREFS_PRIMING_SELFTEST=PASS`（25/25 臂）。
- 全量 `gate_run.sh --full --chunks 8`：**`ran=111 pass=111 fail=0 sandbox=0 envretry=0 verdict=GREEN`**（3.5 min，`gate-runs/20261024T063513Z`）。
- `test_scan.py`：**`ALL 342 CASES PASS`**。

**L2 邻门**：全量已覆盖 111 门，其中直接相邻的 `envvar_config_group_check`（同 ConfigRegistry）、`chat_menu_prefs_listener_check`（同 prefs+StateFlow 形状）、`offload_permission_reset_sync_check`（同 CLI handler）、`browser_*` / `mcp_oauth_lifecycle_check`（同 StateFlow 快照）全绿。

**L3 真 kotlinc 2.x + JUnit4 双臂**（生产文件真编译，android.jar + coroutines；`AppLogger` 用 8 行 shim，因为它只经默认失败钩子被触达）：
- **POST**：`OK (12 tests)`（PrefsWriteBusTest 6 + BackgroundToggleCacheTest 5 + BackgroundToggleKeyTest 3 中本组合跑 12）。
- **PRE**（基线 `PrefsFields.kt`，无 publish）：`Tests run: 6, Failures: 4` —— 恰好是四个「送达」用例（bool 写入送达 / int+string 送达 / 两实例消费者恢复 / 异常包含），另 2 例（幂等、空 bus）本就与接线无关。
- **m1（sink 变死代码）**：恰好 1 红 = `overlayRefreshesFromWritersHandleEvenWhenOwnHandleIsStale`（真机形状）。
- **m2（publish 静默吞异常）**：恰好 1 红 = `throwingSinkDoesNotBreakWriteOrStarveLaterSinks`。
- **m3（publish 挪到 apply 之前）**：恰好 1 红 = `consumerRecoversEvenWhenItsOwnHandleIsStale`（sink 在回调时读到旧值）。
- 附：结构改造后「repository 把 `prefs` 而不是 `written` 传下去」这个变异**已无法表达**（cache 不持有自己的句柄），这是本轮最值钱的一步。

**L4 CI**（分支 dispatch，`workflow_dispatch`，attempt 1）：run **`40072215333`** / job **`120807918366`** → **success**。

- **三方 sha 逐字符一致**：本地 HEAD = `ls-remote` = `run.head_sha` = **`03840e3ff8020cdbab9d8ffbf460ee603b4198b2`**（绿的那棵树就是本报告所在提交的父提交；报告回填提交只动 `fix-reports/`）。
- 步骤：26 步中**唯一非 success = 第 23 步 `Publish to Releases`（skipped，main 门控）**。
- 门：门头 `[1/111] … [111/111]` 共 **111 条且唯一**；`Summary: 111 passed, 0 failed` 恰 1 条，其余 7 条步骤级 Summary 全 `0 failed`；`ALL 342 CASES PASS` 恰 1 条。
- 本支新增输出在册：`OK: write-through delivery green (2 [dv29-a4] delivery line(s))` + 两行送达明细（`BackgroundSettingsRepository.kt:142 KEY_TASK_NOTIFICATIONS` / `:149 KEY_BG_OVERLAY_ENABLED` → sink `:177`）。
- 新用例 15 条逐条 PASSED（PrefsWriteBusTest 6 / BackgroundToggleCacheTest 6 / BackgroundToggleKeyTest 3）。
- 编译错 ` e: ` **0**；含 `FAILED` 的行 **0 条不以 PASSED 结尾**（游离 0）；本支五个文件**零编译警告**。
- 日志 1,470,334 B（`/tmp/a4probe/ci/0_build.txt` 判读）。

---

## 7 残留与失真

| # | 项 | 级别 | 触发条件 / 说明 |
|---|---|---|---|
| R1 | **子因未定死**（callback 未触发 vs 两个 prefs 实例） | P3 记录 | 需要 debug 包 + `identityHashCode(prefs)` 两侧日志；修法对该区分不变。若将来发现是「两个实例」，则 `PrefsWriteBus.publish` 的「交写入方句柄」设计已经覆盖，无需再改 |
| R2 | **其它 prefs 字段的消费者未逐一接线** | P2 | 现在只有 2 个快照型 key 被 C10b 覆盖；新增一个「StateFlow 快照 + registry 写」的字段时，门会变红并指出缺 sink —— 这是判据的设计意图，不是漏洞 |
| R3 | `PrefsWriteBus` 是进程级单例，注册后不注销 | P3 | `BackgroundSettingsRepository` 进程级单例 ⇒ 与进程同寿；若将来有短生命周期消费者注册，需在 `close()` 里 `unregister`（`PrefsWriteBus.unregister` 已提供并有用例） |
| R4 | 真机行为级未验 | P2 待验 | 装上新包后：`minis-config set background.overlay true` → `AgentForegroundService` 应立刻打 `enabled=true`（当前基线是 `false` 直到重启）；`background.notifications false` → `bg-notify` 应打 `skipped …=false`。判据与本次取证同一条日志入口 |
| R5 | 任务书 §2 候选 3 写「unregister 命中数应 0」 | 失真（已记录） | 实测 14 处，全部是 Compose `onDispose` 摘自己的 listener + 1 处 no-op 桩。判据不该是「0 处」，而是「没有一处能摘到该 listener」——报告按后者取证 |
| R6 | 任务书 §4 说「原有六臂」 | 失真（已记录） | 该门 selftest 在 dv22 之后已扩到 20 臂（docstring 里的 "six" 也是旧数），本轮改为「每判据一臂」的写法并更新 docstring |

---

## 8 身份链与复盘

**身份链**：基线 main `b82aa8dc` → 4 提交（`4d41f8d5` 生产 · `686de04f` 测试 · `b073e894` 门 · `03840e3f` 报告）→ 分支 `fix/dv29-a4-prefs-write-through` → **CI run `40072215333` / job `120807918366` success @ `03840e3f`** → 报告回填提交只动 `fix-reports/`（`git diff --name-only 03840e3f HEAD -- . ':(exclude)fix-reports'` 为空 ⇒ 源码树与绿 sha 逐字节等价）。

**本轮可复用的三条**：
1. **「同一实例」是假设，不是事实** —— 只要送达依赖「A 写的回调能到 B」，就必须让 A 把值交给 B，而不是让 B 去读它自己的缓存。`config/SessionConfigWriteThrough.kt` 是同一形状的第一次（Room 行 ↔ 驻留 VM），这次是第二次（prefs ↔ 缓存 StateFlow）。同族第二次 ⇒ 修在共享层（阶梯纪律），所以落在 `PrefsFields` 而不是 `BackgroundSettingsRepository` 里打补丁。
2. **把「选择哪个句柄」结构上消灭掉** > 用测试守住它：sink 建在 cache 内部后，接线处想写错也没有第二个句柄可写。变异臂 m1 从「能写但被抓」变成「写不出来」，比加断言更便宜。
3. **日志判据要挑「每次状态变化都打」的那种**：`applyOverlayState` 因为 `distinctUntilChanged` 后逐次打印，才能既证明「组合流在跑」（排除采样错位/没驱动），又证明「值是旧的」。若换成一个只在边沿打印的日志，这条就会误判成 (c)。

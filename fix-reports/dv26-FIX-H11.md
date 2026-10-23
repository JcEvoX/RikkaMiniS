# FIX-H11 — ScheduledTasksStore 无 revision 流：列表脱钩 + 编辑屏复活已删任务（S-11，P1）

**分支** `fix/dv26-h11-sched-store-revision` · **基线 main** `f7aba218` · **worktree** `/var/minis/shared/work/deep-audit-1046/h11`

---

## 1. 缺陷与真树取证（基线 f7aba218）

三层同源，全部静默：**Store 是「读一次」的**（每个访问器都是一发 `load`），所以
「谁在这一发之后改了文件」这件事，屏内没有任何通道能知道。

| # | 位置 | 取证 |
|---|---|---|
| D1 | `scheduled/ScheduledTasksStore.kt` | `grep -n 'revision'` = **0** 命中。全部读写走 `load` / `mutate` 一次性快照，无任何变更流。 |
| D2 | `ui/settings/ScheduledTasksScreen.kt:63-76` | `produceState<ScheduledTasksFile?>(null, context, reloadKey)`，`reloadKey` 只在 `Lifecycle.Event.ON_RESUME` 自增。⇒ 页面停留期间 `ScheduledTaskRunner` 写入的 `lastRunDate` / `lastResult` / `failStreak` / `suspendedByFuse` **看不见**：后台跑完仍显示「从未运行」，熔断后 **Resume 按钮不出现**（`ScheduledTaskRow` 的 `if (task.suspendedByFuse)` 读的是这份陈旧快照）。 |
| D3 | `ui/settings/ScheduledTaskEditScreen.kt:80-100` | 只有一发 `LaunchedEffect(taskId)`。⇒ 编辑期间任务被 `deleteTask`（或 `importDocument` 整份替换掉），表单照旧可编辑，`recordMissing` 永不置位。 |
| D4 | `scheduled/ScheduledTaskPolicy.kt:208-222 mergeUpsertTask` | `val idx = file.tasks.indexOfFirst { it.id == task.id }` → `if (idx < 0) return file.copy(tasks = file.tasks + task)`。**不区分「新建」与「编辑」**：编辑侧的 `idx < 0` 唯一含义是「目标已消失」，而这里把它当成新建 append ⇒ 已删任务连同表单快照的运行态（`enabled = prior.enabled`、旧 `lastRunDate`、已清的 `failStreak`）**被写回 `scheduled-tasks.json`**。 |

**报告失真核对**：TRIAGE §2 第 6 条记「`ScheduledTasksScreen.kt:63-76` 的 `produceState(..., reloadKey)` 真树成立」，本轮独立复核成立（行号亦对得上 `:63-65`）；报告称编辑屏「连 ON_RESUME 都没有」亦属实（该屏无任何 lifecycle 观察者）。无新增失真。

**为什么这是 P1 不是 P2**：D4 是**写坏用户数据**（删掉的定时任务会自己回来，且带着与当前状态不符的运行态——恢复后的任务可能显示「今天已跑过」而当天根本没跑），D2 让**熔断这个安全机制在 UI 上不可见**（任务是停的，但用户看不出为什么停、也没有 Resume 入口，只能离开再进页面）。两者都在常用路径上。

---

## 2. 修法（改动阶梯）

**停在第 6 级（「能一行」的下一步：一个 StateFlow + 两处订阅 + 一个参数）**，理由逐条：

- 第 1 级（真要做吗）：D4 会造成数据错误、D2 会让熔断不可见，都要做。
- 第 2 级（代码库里已有）：`ScheduledTasksStore` 已有**唯一写漏斗** `writeLocked`，`ScheduledTaskTransitions` 已有 `mergeUpsertTask` 这个「编辑保存的唯一语义入口」——两个既有结构正好承载「一处递增」「一处守卫」，不需要新概念。
- 第 3/4 级（标准库 / 平台原生）：`MutableStateFlow` + Compose `collectAsState` / `produceState(keys)` 就是平台原生的变更传播方式；不引入 Flow 之外的机制。
- 第 5 级（已装依赖）：无新依赖。
- 第 6 级（能一行）：`produceState` 多一个 key、`LaunchedEffect` 多一个 key、`mergeUpsertTask` 多一个默认参数——三处都是「改一行」量级。

**具体形状**：

1. **`ScheduledTasksStore`**：`private val _revision = MutableStateFlow(0L)` + `val revision: StateFlow<Long>`。递增点**只有一处**，在 `writeLocked` 里 —— 那是全部 8 个写访问器（`mutate` / `importDocument` / `completeRun` / `crashFailure` / …）唯一的落盘通道，所以新加写函数**不可能忘**（对比：在 6 个写方法里各写一次，第 7 个必然漏）。
   递增**在写之后**，并且由 `ScheduledTaskRevisionPolicy.nextRevision(current, writeSucceeded)` 决定 —— `AtomicFileWrite.write` 抛异常时文件没变，此时递增会让所有打开的屏去重读一份不可能变过的文档（重试环里就是白刷 UI）。`writeLocked` 因此把 `try` 的结果收成 `wrote: Boolean`。
   递增在**锁内**（`writeLocked` 本来就在 `lock.withLock` 里）⇒ 不会乱序；`_revision.value` 是单线程写的（锁保护），无丢失更新。
2. **`ScheduledTasksScreen`**：`val storeRevision by ScheduledTasksStore.revision.collectAsState()`，把它加进 `produceState` 的 key 集。**保留** `reloadKey` 与 `ON_RESUME` —— 那是第二条保险（本屏自己的点击 `reloadKey++`，因为 revision 的 bump 比点击反馈晚一帧）。
3. **`ScheduledTaskEditScreen`**：首个 `LaunchedEffect(taskId)` 语义**一字未动**（仍负责首载 + 首帧 not-found），末尾置 `loadedOnce = true`；新增 `LaunchedEffect(taskId, storeRevision)` 作为「跟随 store」：
   - `loadedOnce == false` ⇒ 直接返回（首页帧的主权归首载 effect，不让刷新抢跑出一个空表单）；
   - `shouldMarkMissing(loadedOnce, stillExists = fresh != null)` ⇒ `recordMissing = true`（复用既有 `da1034-B2/B-04` 的 not-found 契约：显式屏 + 唯一出口，Save/Delete 不可达）；
   - 目标仍在 ⇒ **只刷新 `original` 的运行半**（`refreshRunState`：`enabled` / `lastRunDate` / `lastResult` / `failStreak` / `suspendedByFuse`）。**绝不写 title/prompt/windowStart/windowEnd** —— 这是本单最容易做坏的一处：后台跑一次就重写用户正在敲的标题，会让编辑变成不可能完成的任务。
   - **保存路径**：`upsertTask(..., allowInsert = isNew)`；返回文档里若 `none { it.id == effectiveId }` ⇒ `recordMissing = true`（不 `onDone()`），即「保存被拒」有可见后果，而不是静默成功关闭。
4. **`mergeUpsertTask(file, task, allowInsert: Boolean = true)`**：`idx < 0` 时 `if (!allowInsert) return file`（**同一个实例**）。这一点很关键：`mutate` 只在 `next !== current` 时写盘 ⇒ 被拒的保存在磁盘层面**完全不发生**，也不会 bump revision（否则等于拿一个没写成的保存去刷新所有屏）。
   默认值保持 `true`，既有 `ScheduledTaskMergeUpsertTest` 的 8 处调用与门的字面锚点全部不动。

**没做的（刻意）**：没有重写 Store 的锁策略（`Mutex` + `writeLocked` 漏斗原样保留，`scheduled_store_release_guard.py` 的 LOCK ORDER 判据仍绿）；没有把 revision 做成「文档版本号」（人读不出意义，还会诱使人去比较大小）；没有给编辑屏加 ON_RESUME 观察者（那是列表屏的机制，编辑屏要的是「目标还在不在」，靠 ticker 更准）。

---

## 3. 改动清单

| 文件 | 增/删 | 内容 |
|---|---|---|
| `scheduled/ScheduledTaskPolicy.kt` | +72/−2 | `mergeUpsertTask(..., allowInsert)` 守卫；`refreshRunState`；新 `object ScheduledTaskRevisionPolicy { nextRevision, shouldMarkMissing }`（android-free，供 JVM 测） |
| `scheduled/ScheduledTasksStore.kt` | +39/−3 | `_revision` / `revision` / `writeLocked` 落盘后递增（唯一递增点）/ `upsertTask(..., allowInsert = true)` |
| `ui/settings/ScheduledTasksScreen.kt` | +10/−1 | `collectAsState` + `produceState` key 加 `storeRevision`（`reloadKey`、`ON_RESUME` 保留） |
| `ui/settings/ScheduledTaskEditScreen.kt` | +50/−2 | ticker 订阅、`loadedOnce`、跟随 effect（gone → recordMissing；在 → 只刷运行半）、Save 走 `allowInsert = isNew` + 拒绝信号 |
| `src/test/.../ScheduledTasksStoreRevisionTest.kt` | +166 | 新增，11 个 JVM 用例 |
| `scripts/scan/scheduled_store_release_guard.py` | +576/−1 | C1–C4 四判据 + 19 个带牙臂 + `SCHEDULED_REVISION_SELFTEST` 标记 |

**合计 6 文件 +913/−9**（生产 4 文件 +171/−8 · 测试 +166 · 门 +576/−1）。冻结面 `scan.sh` / `test_scan.py` **0 触碰**；门数 **109 不变**（扩既有独占门，0 新建门）。

---

## 4. 门与判据（扩 `scheduled_store_release_guard.py`）

四条判据全部**派生**，不写死成员清单（`_state_flow_name()` 从 Store 取 ticker 名，`_collect_var()` 从屏取收集变量名，`_paren_args()` 从签名取参数）：

| 判据 | 判什么 | 为什么必须是它 |
|---|---|---|
| **C1** | 存在 `MutableStateFlow` 备用字段，且**以同名 StateFlow 暴露**（`private val` 不算暴露）；每个递增点都必须落在**含写入**的函数内，且**写入在递增之前**；文件内**每个 `AtomicFileWrite.write(` 都必须处在会递增的函数里** | 「有条流」不够：`private` 的流等于没有；递增在写前 = 写失败也通知（重读一份没变的文档）；某个写函数漏递增 = 那条写对所有屏不可见（第 7 个写方法就是这么漏的） |
| **C2** | 列表屏收集该 ticker（名字来自 Store），且 `produceState` 的 **key 集包含收集变量**；`ON_RESUME` → `reloadKey++` 仍在 | 收集了不 key = 什么都不会重跑；只留 ticker 删掉 ON_RESUME = 从编辑屏/后台切回来不重读（ticker 只在写时才动） |
| **C3** | 存在以 ticker 为 key 的 `LaunchedEffect`，体内 (a) 置 `recordMissing = true`，(b) 询问目标是否仍存在（`none { it.id ==` / `firstOrNull { it.id ==` / `shouldMarkMissing(` 家族），且 (c) **不赋值任何表单文本字段**（title/prompt/windowStart/windowEnd） | (a)(b) 是缺陷本体；(c) 是「别把修复做成新 bug」——跟随 effect 里写 title 就是用后台快照盖掉用户输入 |
| **C4** | 含 `file.tasks + task` 的函数必须有 `Boolean` 参数，`idx < 0` 分支必须**引用**该参数，且该分支里必须有一条 `return file`（**不带 `.`**，即返回原文档） | 「有参数」不等于「参数有效」；「拒绝」必须是「返回原 document 实例」，否则 store 仍会写盘 + 递增 |

**带牙臂（19 条，每条只红恰好一条）**：

- C1：无 ticker / ticker 未暴露（`private`）/ 递增移到写前 / 有写函数逃出漏斗 —— 各 1 条。
- C2：不收集 / ticker 不进 produceState key / ON_RESUME 重读被删 —— 各 1 条。
- C3：不收集 / 无 ticker effect / 体内不置 recordMissing / 体内不问存在性 / 体内写了 title —— 各 1 条。
- C4：无条件 append / 无 allow-insert 参数 / 参数不被引用 / 拒绝时仍写文档（`return file.copy(...)`）—— 各 1 条。
- 另加 4 条「合规树全绿」臂（C1/C2/C3/C4 各一）。

**PRE 臂**：`git show f7aba218:` 抽四文件到 `probe/base/` → 门 **rc=1，恰 4 条红**（C1/C2/C3/C4），且既有 S1/S2/S3 与「两条释放路径」判据在该树上**仍绿**（说明新判据没有污染旧判据）。

**给主会话的注册信息**（冻结面由你统一处理）：
- 门脚本：`scripts/scan/scheduled_store_release_guard.py`（**既有门，已在 `scan.sh` 第 N 位**，无需新增注册行）。
- 新增 selftest 指纹标记：`SCHEDULED_REVISION_SELFTEST=PASS`。
- 建议夹具行（`test_scan.py`）：断言 `--selftest` 输出含该标记 + 真树 rc=0（既有 `scheduled_store_release_guard.py on real repo exits 0` 已覆盖真树那一半）。

---

## 5. 验证四层

### L1 门层

- 本门真树：`python3 scripts/scan/scheduled_store_release_guard.py .` → **rc=0**，输出含三条既有 OK + 四条新 OK（`C1 change ticker` / `C2 list keys on it` / `C3 editor follows it` / `C4 insert is opt-in`）+ `OK: 2 removal function(s) release the runner (importDocument, deleteTask) at top level.`
- `--selftest` → **`SCHEDULED_REVISION_SELFTEST=PASS`**（19 臂全中），既有三个标记同时 `PASS`（`SCHEDULED_MERGE_SELFTEST` / `SCHEDULED_CANSAVE_SELFTEST` / `SCHEDULED_EDITGUARD_SELFTEST`），`--selftest` rc=0。
- PRE 臂：见 §4（4 红）。
- 全量：`sh scripts/scan/scan.sh` → **109 passed, 0 failed**（`probe/scan-full.log`，尾部 `Summary: 109 passed, 0 failed`；门头 `[1/109]..[109/109]`）。
- 夹具：`python3 scripts/scan/test_scan.py` → **`✅ ALL 336 CASES PASS`**（`probe/test_scan.log`）。
  - **过程中的一次真修**：首跑该套件时 `scheduled_store_release_guard` 的两条既有夹具红 —— 它们用**合成树**（只放 `deleteTask`/`importDocument` 形状的 Store，无任何 `AtomicFileWrite.write`）验证「释放路径」。C1 在这种树上不适用（没有写，就没有「写后递增」可判），因此判据改为**无写入即 SKIP**（打印一行 SKIP，不产生 violation）。这不是为了夹具而放宽真树判据：真树与 PRE 树都含写调用，仍然走完整判据（PRE 4 红可证）。

### L2 邻门层（按概念取 7 道真实邻门）

`schedule_date_source_check` / `browser_destroy_wakeup_check` / `deeplink_settings_routes_check` / `router_lock_check` / `stream_flow_dispatch_guard` / `dao_loadmessages_check` / `offload_cli_args_check` → **7/7 rc=0**。
（按概念 `grep -rln 'ScheduledTask\|scheduled' scripts/scan/*.py` 取；任务书未点名邻门。）

### L3 装置层（真 kotlinc 2.0.21 + JUnitCore，`probe/l3/`）

驱动拆两层：`run.sh` 编排子进程、`mutate.py` 只做字节编辑（python 内嵌 `subprocess.run` 会撞沙箱 S28 `Errno 38`）。所有变异施加在**每臂独立的源文件副本**上，真源全程不动。

| 臂 | 结果 |
|---|---|
| **POST**（真树 `ScheduledTask.kt` + `ScheduledTaskPolicy.kt` + 新测试） | `OK (11 tests)` |
| **PRE**（基线两文件 + 复现测试） | **RED / 2 reds**：`a save for a deleted task must not resurrect it`、`a save for a deleted task must not change the document at all` |
| **PRECOMP**（把 POST 测试拿去编基线源） | **COMPILE-FAIL**（`ScheduledTaskRevisionPolicy` / `allowInsert` 在基线不存在）→ 证明这两个符号是本次新增的机制，不是既有 |
| **m1** `nextRevision` 写失败也递增 | RED，恰 1 红：`a failed write leaves the revision untouched` |
| **m2** `shouldMarkMissing` 忽略 `loadedOnce` | RED，恰 1 红：`a change arriving before the initial load decides nothing` |
| **m3** 删掉 `if (!allowInsert) return file` | RED，恰 1 红：`an edit whose target vanished is refused and the document is untouched`（`assertSame` 失败 = 会写盘） |
| **m4** `refreshRunState` 顺手抄 `title` | RED，恰 1 红：`a change refresh moves the run state and nothing else` |
| **m5** `allowInsert` 默认改 false | RED，恰 1 红：`a brand-new task still inserts through the default` |
| **SOURCES-UNTOUCHED** | `yes`（sha 前后一致，未用 `git checkout --`） |

**既有测试回归**（同一套 kotlinc/JUnit 装置直跑，证明加参数没破坏 1030-08 契约）：
`ScheduledTaskMergeUpsertTest` → `OK (9 tests)`；`ScheduledTaskPolicyTest` → OK；`ScheduledTasksCodecTest` → OK。

### L4 CI 层

`run 39948641772` · `job 120397403422` · attempt 1 · **conclusion `success`**。

| 判据 | 读数 |
|---|---|
| 身份 | 本地 HEAD == `git ls-remote` == `run.head_sha` = `1c01d809f93bd445cf1d18f3ef06dcf37f9d25fb` **逐字符一致** |
| 步骤 | 26 步，`steps-not-success: none`，唯一 skipped = `Publish to Releases`（main 门控 ✅） |
| 门摘要 | 含 `Summary: 109 passed, 0 failed` 的行**恰一条**，且无任何非零 failed 摘要 |
| 门头 | `[1/109] .. [109/109]` 连续 |
| 夹具 | `ALL 336 CASES PASS` |
| 编译错 | `Z e:` 0 行 |
| 游离 FAILED | 0（`endswith(" FAILED")` 判据；`FAILED` 出现在用例名里那些行全部以 `PASSED` 结尾） |
| 新增用例 | `ScheduledTasksStoreRevisionTest > …` **11 条逐条 PASSED** |
| 新判据在册 | `OK (C1 change ticker)` 与 `OK (C4 insert is opt-in)` 两行在日志里 |
| 日志 | 1,435,614 B（`probe/ci/job-120397403422.log`） |

判读器 `probe/ci/judge.py`：身份与步骤**一律读 API JSON**（`run.head_sha` / `steps[].conclusion`），不从日志文本猜；日志下载两跳（302 的 Location 是预签名 URL，第二跳不带 token），下载失败时复用已存日志。

> 本报告自身的提交会把 HEAD 推走。届时以「源码树等价」表述：`git diff --name-only 1c01d809 HEAD -- . ':(exclude)fix-reports'` 为空（`fix-reports/` 不在 workflow paths 内、不参与构建）。

---

## 6. 残留与负决策

| # | 残留 | 触发条件（何时复活） |
|---|---|---|
| R1 | **真机行为级未验**：①后台跑完一次后列表屏**留在前台**能看到 `lastResult` 变化、熔断第 3 次失败后 **Resume 按钮即时出现**；②编辑表单开着时，从列表删掉该任务 → 表单转 not-found 屏；③同一窗口内点 Save 不复活、且文件里确实没有该任务。 | 下次真机窗口（本批 `§1046-VERIFY`）。判据：②③ 必须同窗口内做，否则删完再点 Save 会命中「重新打开」的正常路径。 |
| R2 | **另三个「第四类」屏明确不修**：`MemorySettingsScreen` / 日志屏 / `BackupSettingsScreen` 同样是「读一次 + 只在少数动作后重读」的形状，但它们的后台写入方是**用户自己触发的导出/导入**（同屏内同步反馈），没有「另一个组件持续写」的第二写者。 | 复活条件：其中任一屏出现**第二个写入者**（如自动备份在后台更新 `lastBackupAt`、内存治理在后台改预算），即同族复发，按本单形状修（Store 加流 + 屏 key）。 |
| R3 | 编辑屏的跟随 effect **只认 `id`**：`importDocument` 恢复同一 id 但内容不同时会走「目标仍在」分支（只刷运行半，spec 仍取表单）。这是刻意的——编辑中的 spec 属于用户。 | 复活条件：出现「恢复备份时期望编辑屏跟着换成备份里的 spec」的产品需求。 |
| R4 | `mergeUpsertTask` 的 `allowInsert` 默认 `true`，所以**这层保护在 call site**；将来若有新调用方（除编辑屏外）传默认值做「编辑」，就会重新出现复活路径。 | 复活条件：`grep -rn 'upsertTask('` 出现第二个调用点；届时要么默认改 `false`（并显式在新建处传 true），要么给调用点加判据。 |
| R5 | C1 的「无写入即 SKIP」：真树若被人删掉全部落盘调用，C1 会 SKIP 而不是红。 | 复活条件：`grep -c 'AtomicFileWrite.write(' ScheduledTasksStore.kt` 归零（那时另有更严重的故障先暴露）。 |
| R6 | C3 只判「跟随 effect 内不赋值表单字段」，判不出「首载 effect 被改成也跟随 ticker」（那会让首帧反复覆盖）。 | 复活条件：有人把首载 `LaunchedEffect(taskId)` 的 key 改成含 ticker。 |

**负决策（不修，登记理由）**：不做「文档版本号 / 时间戳」式 revision（值无可读语义，还会诱人去比大小）；不给编辑屏加 ON_RESUME 重读（它要的是「目标在不在」，ticker 更准）；不在 8 个写访问器里各写一次递增（第 7 个必漏）。

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f7aba218` |
| 生产提交 | `629a19f9` |
| 测试提交 | `5a3b677a` |
| 门提交 | `1c01d809` |
| 本地 HEAD | `1c01d809f93bd445cf1d18f3ef06dcf37f9d25fb` |
| `git ls-remote` | `1c01d809f93bd445cf1d18f3ef06dcf37f9d25fb` |
| CI run / job | `39948641772` / `120397403422`（attempt 1，`success`） |
| CI head_sha | `1c01d809f93bd445cf1d18f3ef06dcf37f9d25fb`（三方逐字符一致） |
| 源码树等价 | 报告提交后：`git diff --name-only 1c01d809 HEAD -- . ':(exclude)fix-reports'` 为空 |

**未合并 main**（含 `.kt` 验证面，合并起手权在用户）。

---

## 8. 自证成本与不确定项

- **成本**：3 次本地全量门套件（首跑撞沙箱 S28 `Errno 38` 风暴两次，静置 60–120 s 后重跑才拿到干净读数；其间不报假红）+ 10 次 kotlinc 编译（POST/PRE/PRECOMP/5 变异/3 回归）+ 1 轮分支 CI。
- **不确定项 1（已自查闭合）**：`produceState` 的 key 重载有 `key1` / `key2` / `key3` 三个版本；改动前是 `(null, context, reloadKey)` = 2 个 key，现在是 `(null, context, reloadKey, storeRevision)` = 3 个 key，正好落在 `key3` 重载上，不需要 `vararg`。CI 编译通过即实证（若哪天要 4 个 key，就得改用 `remember(...)` 包一层）。
- **不确定项 2**：`collectAsState()` 在 `ScheduledTasksStore` 这种 `object` 上取 `.revision` 是进程级单例流，**不会**因为屏离开而泄漏（`collectAsState` 随 composition 取消订阅）。
- **不确定项 3**：编辑屏的两个 `LaunchedEffect` 在首帧是并发起跑的；我靠 `loadedOnce` 让跟随 effect 首帧直接返回。若实测出现「首帧 not-found 闪烁」，改法是让跟随 effect 也 `await` 首载（`snapshotFlow`），本单不预先加复杂度。

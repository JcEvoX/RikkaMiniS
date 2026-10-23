# FIX-7 · deep-audit-1045（v24 波）· B-03（P1）

- **分支**：`fix/dv25-b3-storage-io-offmain`
- **基线**：main `1d44ec905b8f5d21a215694e9a13910c74e1f645`（门 109 / 夹具 336）
- **工单**：B-03 —— 存储页在 IO 线程裸写 Compose state；`StorageSnapshotCache` 没有外部失效通道，rootfs 安装 / 修复 / 重置后 shell 总量被进程级钉死
- **停止节点**：分支 CI 绿（不合并 main）

---

## §1 缺陷与根因

### 1.1 症状与根因（两句可复核的话）

> **症状**：装完 / 重置 rootfs 再回到「存储」页，「Terminal Shell」一行仍然显示上一次的数字（空 rootfs 上装 200 MB 仍读 0 B），只有杀进程才刷新。
> **根因**：缓存有一份、失效通道没有 —— 唯一能改 `StorageSnapshotCache` 的是本文件里的 3 个写点，而 `RootfsManagementViewModel`（另一个包）**全仓 0 次引用**；再加上 rescan 分支的 rootfs 门是 `StorageSnapshotCache.snapshot != null`，它把「缓存存在」当成「rootfs 没变」，于是超期重扫也只是把旧值抄进新快照。
> **证据**（基线树实测）：
> - `grep -rn "StorageSnapshotCache" src/android/app/src/main/java | wc -l` = 6，全部在 `ui/settings/StorageManagementScreen.kt`；
> - `grep -rn "StorageSnapshotCache\|invalidate" src/.../ui/sandbox/RootfsManagementViewModel.kt` = 0；
> - 基线 `StorageManagementScreen.kt:199` `if (StorageSnapshotCache.snapshot == null) { …rootfs 扫描… }`，:265 `StorageSnapshotCache.snapshot = StorageSnapshot(shellSize = shellSize, …)` —— `shellSize` 读的是 **Compose state**（:134 `var shellSize by remember { mutableLongStateOf(0L) }`），即「把上一屏发布出来的值当成这次扫描的结果」。

同族的第二半（同一段代码里的线程所有权）：**11 处 Compose state 赋值落在 `withContext(Dispatchers.IO)` 内**（基线实测行号）：

| 位置 | 基线行 | 写入的 state |
|---|---|---|
| 主扫描块 | `:204` `:210` `:214` `:223` `:230` `:245` | `shellSize` `shellBreakdown` `dbSize` `sessionCount` `orphanInfo` `sessions` |
| `SessionStorageDetailScreen.reload` | `:528` `:529` `:530` | `session` `minisSize` `mediaSize` |
| 清除确认回调（`AlertDialog` 的 lambda 里） | `:637` `:638` | `minisSize` `mediaSize` |

Compose state 与渲染帧之间没有跨线程 happens-before；两次重叠 `reload()` 的赋值可以互相穿插（一次扫描的部分字段 + 另一次扫描的其余字段），而缓存快照与 state 也可能来自不同次扫描。

### 1.2 为什么「9 处」是错的（→ §6）

任务书与 TRIAGE 都写「9 处」，实际是 **11 处**：清除确认回调（`AlertDialog(confirmButton = { … })` 的 lambda 内）的 2 处赋值被漏算。这不是纯计数问题 —— 它同时暴露了我第一版门的盲区（见 §4.2），并已在门里修掉。

---

## §2 修法

**停在第 6 级（能一行就不加层）/ 第 2 级（代码库里已有）**——本批共同母题是「接既有单源」：
- 线程形态**不用新造**：`LogManagementScreen:131-148`、`UsageStatsScreen:102-112` 就是「IO 计算 → Main 发布」，照抄形态；
- 失效通道**不用新造**：`CacheSettingsScreen` / `UsageStatsScreen` / `BackupSettingsScreen` 各有自己的失效路径，本页只是缺。

**一句话结论**：停在第 2 级 —— 把 IO 块降级为「只算」，让它 `return` 一个 `StorageSnapshot` 值；把缓存清除收敛成**一个** `internal` 顶层函数，由 VM 的三个 mutate 入口在 mutation 之前调用。新鲜度判定抽成纯决策 `decideScan()` 仅为可测（见下）。

### 2.1 生产侧（3 文件）

1. **`StorageManagementScreen.kt`**
   - `reload()`：IO 块内只做 `RootfsUsageScanner.scan` / `databaseSize` / `dao.listSessions` / `scanMediaOnce` / `scanOrphanSessionDirs` / 并行 sizing，产出 `StorageSnapshot` **值**返回；Main 侧一次性写 6 个 state + 缓存（同一次扫描，单一来源）。
   - `SessionStorageDetailScreen.reload()`、清除确认回调：同样 `Triple` / `Pair` 返回值再上屏。
   - `private object StorageSnapshotCache` 之后新增 **唯一清除点**：
     ```kotlin
     internal fun invalidateStorageSnapshot() { StorageSnapshotCache.snapshot = null }
     ```
     文件内原有 2 处 `StorageSnapshotCache.snapshot = null`（孤儿回收、会话清除）改为调用它 ⇒ **全文件只剩 1 个清除语句**。
2. **`StorageScanPolicy.kt`（新文件，56 行，无 Compose / 无 Android 类型）**：`StorageScanDecision{Reuse, RescanDeferred, RescanNow}` + `decideScan(cachedAt, force, now, staleMs)`。语义与基线内联判断逐条等价（窗口内复用 / `force` 立即扫 / 无快照立即扫 / 超期「先渲染再延后重扫」），边界 `>=` 包含 stale 侧。
3. **`RootfsManagementViewModel.kt`**：`install()` / `resetRootfs()` / `repairRootfs()` 各一处 `invalidateStorageSnapshot()`，**都在 mutation 之前**；`repairRootfs` 的调用放在 `if (initial.healthy)` 早退**之后**（无损伤的修复不该让存储页白白丢掉缓存，那缓存正是把 12.5 万条目走查挡在转场之外的东西）。

### 2.2 顺带修掉的同族 latent（不另立工单，因为它就在被重写的这 3 行里）

基线 `reload(force = true)` 撞上热缓存时走 `else` 分支（骨架）而不发布缓存，但 IO 里 `snapshot != null` 又跳过 rootfs 扫描，于是把这个屏幕实例的 **state 初值 0** 写进新快照 —— 即「强制刷新把 shell 行清零」。现在一律**从缓存携带**（`StorageSnapshotCache.snapshot?.let{…} ?: 扫描`），并且**在延迟窗口之后重读缓存**（而不是复用 `decideScan` 之前抓的那个引用），否则延迟期间落地的失效会被吃掉。

### 2.3 被否决的方案

| 方案 | 否决理由 |
|---|---|
| 把 `StorageSnapshotCache` 提成 `internal object` 并让 VM 直接 `StorageSnapshotCache.snapshot = null` | 缓存持有者仍是 `ui.settings` 的私有实现；直接暴露 `snapshot` setter 会让「谁能改缓存」变成开放式，且第二个清除点正是下一处漏失效的土壤。暴露**动作**而不是**状态**，并用门把「单清除点」钉住。 |
| `shouldRescan(...): Boolean`（任务书建议） | 三个转移在**时序**上不同（立即扫 / 先渲染再延后扫 / 复用），布尔会把时序决策推回 composable，而 composable 恰恰不可 JVM 测。改为三值 `decideScan`。 |
| `decideInvalidate(event)`（任务书建议） | 该谓词恒为真（三个入口都必失效）⇒ 纯属间接层，违反「能一行」。真正的决策（repair 健康早退不失效）由**调用位置**表达，并由门的**行序判据**钉住（§4.1 G）。 |
| 在 `settleRootfsOperation` 成功终态里统一失效 | 半径更大（还覆盖不到 `reset()` 先删目录再重建的失败路径），且 gate 需要调用图闭包才能看见；放在每个入口的 mutation 之前，失败路径也安全（崩溃在 mutation 与失效之间 = 永远不会发生）。 |
| 把 11 处 IO 写改成 `StateFlow` / `snapshot` 包一层 | 会改动整屏状态形态（radius 爆炸），且与既有兄弟屏形态不一致。 |

**硬约束自查**：未引入 `runBlocking` / `GlobalScope`；`Semaphore(8)` 扇出上限一字未动；IO 块内无 `persist` / `invalidate` 之外的副作用（IO 块内现在**只有**读与计算）；`ponytail:` 注释保留；`SNAPSHOT_STALE_MS` / `RESCAN_DELAY_MS` 常量与理由注释未动；orphan 仍然只测量不删除。

---

## §3 改动清单

`git diff --stat 1d44ec90..HEAD`：**5 文件 +1012 / −44**

| 文件 | 增删 | 行区间（新树） | 性质 |
|---|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/ui/settings/StorageManagementScreen.kt` | +120 / −43 | `reload()` 173-321 · `invalidateStorageSnapshot()` 128-153 · 详情 `reload()` 528-545 · 清除回调 700-716 | 生产 |
| `src/android/app/src/main/java/com/rikkaminis/app/ui/settings/StorageScanPolicy.kt` | +56 / −0 | 全文（新文件） | 生产（纯决策） |
| `src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/RootfsManagementViewModel.kt` | +21 / −0 | `install` 358-373 · `resetRootfs` 394-401 · `repairRootfs` 467-476 | 生产 |
| `src/android/app/src/test/java/com/rikkaminis/app/ui/settings/StorageScanPolicyTest.kt` | +350 / −0 | 全文（新文件） | 测试 |
| `scripts/scan/sysinfo_state_thread_check.py` | +473 / −1 | 判据 F/G `:99-330` · `fun_body_span` `:150` · fixtures `:430+` · selftest 臂 `:560+` | 门（独占） |

`scripts/scan/scan.sh` 与 `scripts/scan/test_scan.py` **零触碰**；`strings.xml` / manifest / gradle / workflows **零触碰**；**0 新建门**（门 109 → 109）、夹具 336 → 336。

**提交（4 个）**

| sha | 内容 |
|---|---|
| `f9811f46` | 生产：IO 只算 / Main 发布 + 单一失效通道 |
| `6e42e12f` | 测试：`StorageScanPolicyTest`（纯决策 + 形状 + 负面控制） |
| `44f06cd9` | 门：F/G 两条判据（selftest 6 → 14 臂） |
| `d4d6886c` | 门 + 测试：修「括号上下文」判定盲区（selftest 14 → 15 臂，用例 14 → 15） |

> 第 4 个提交是自伤修补，不在原计划内：首版门用「行首括号深度 > 0 ⇒ 命名实参」排除调用实参，把**嵌在实参里的 lambda 体**一起排除了，于是基线树只报 9 条（漏 `:637/:638`）。详 §4.2。

---

## §4 门判据与带牙证明

门：`scripts/scan/sysinfo_state_thread_check.py`（**本支独占**，只在既有门上扩，不新建门）。

### 4.1 判据（全部从真树派生，不写死名字）

**F. OFF-MAIN PUBLISH**
- 状态集 = 文件自身的 `var <name> [by] remember` 集合（改名不失明）；
- 背景块 = `withContext(Dispatchers.IO|Default) { … }`（括号配平定位）；
- 规则：块内出现对状态集的**语句级赋值**（`name =` / `+=` / `-=`，排除 `==`）即红；
- 排除两类误报：①所在行首最内层括号是 `(` ⇒ 命名实参；②同块内先声明了同名局部（shadow）⇒ 跳过后续。
- fail-closed：文件缺失 / 无 `remember` state / 无背景块 —— 各自报红，不允许「找不到就通过」。

**G. SINGLE INVALIDATION**
- 入口：文件级**非 private** `fun x() { … StorageSnapshotCache.snapshot = null … }`，名字从源码派生（当前 `invalidateStorageSnapshot`）；
- 单点：全文件以 `StorageSnapshotCache.snapshot = null` 开头的语句必须**恰好 1 条**（多一条即红）；
- 调用方：`RootfsManagementViewModel` 中每个「调用了 mutate 成员」的函数，必须在**第一处 mutate 调用之前**出现该入口调用；
- mutate 集从 `RootfsManager` 自身派生：成员体内有 `installState.value =` / `deleteRecursively` / `.delete(` / `mkdirs(` / `copyTo(` / `writeText(` / `renameTo(` 之一，或**传递地**调用这样的成员。实测派生结果（真树）：
  `autoRepair, dumpApkWorld, ensureSessionDirs, installIfNeeded, refreshDns, reset, restoreApkWorld, retryFailedApkWorld` → VM 里命中的生命周期入口 = `install` / `resetRootfs` / `repairRootfs`（`refresh` 只调 `getRootfsSize`，只读，正确不要求）。
- fail-closed：找不到任何 mutate 成员 / 找不到任何生命周期入口 / 文件缺失。

**函数体定位**：`fun_body_span()` 区分块体与表达式体。真树里绝大多数成员是 `= withContext(Dispatchers.IO) { … }` 的表达式体，首版「取签名后第一个 `{`」会把**下一个成员的身体**算进来，从而把只读成员误判成 mutate —— 这个坑在真树上当场红过，函数体定位因此改成：沿参数表括号回到深度 0，遇到 `{` 才是块体；遇到 `=` 则取该表达式尾随 lambda 的 span（并在遇到下一个成员声明时停止）。

### 4.2 两条判据的牙齿（selftest 15 臂，逐臂「改什么 → 红在哪条」）

| # | 臂 | 精确红在 |
|---|---|---|
| 1 | 合规夹具 | 全绿（`SYSINFO-STATE SELFTEST=PASS`） |
| 2-6 | *既有 5 臂*（loose state / publish outside lock / live list / unrouted append / mutable-collection） | 既有 A–E 判据 |
| 7 | 缺失 SysInfoCoalescer 类 | fail-closed |
| 8 | IO 块内写 `shellSize` | F |
| 9 | **lambda 实参内写 state**（`AlertDialog(confirmButton = { withContext(IO) { shellSize = … } })`） | F —— **首版逻辑在该形状上静默变绿**，本臂就是为它补的 |
| 10 | IO 块内的命名实参 `shellSize = report.totalBytes`（正控） | 不红（F 不误报实参） |
| 11 | 清除入口改 `private` | G（入口不可达） |
| 12 | 加第二个清除点 | G（单点） |
| 13 | VM 删掉 `invalidateStorageSnapshot()` | G（缺失） |
| 14 | VM 把失效移到 mutation 之后 | G（行序） |
| 15 | 删掉 `StorageManagementScreen.kt` | F + G fail-closed |

**基线树（PRE）实测 11 条 F 违规 + 1 条 G 违规**，行号 `204/210/214/223/230/245/528/529/530/637/638` —— 与 JVM 装置 `pre_shape` 臂报出的行号**逐条一致**（两套独立实现互证）。

---

## §5 验证四层

### L1 门（本地）

```
python3 scripts/scan/sysinfo_state_thread_check.py .        → rc=0（真树绿）
python3 scripts/scan/sysinfo_state_thread_check.py --selftest → SYSINFO-STATE SELFTEST=PASS（15 臂）
python3 scripts/scan/sysinfo_state_thread_check.py ../golden  → rc=1，11×F + 1×G（PRE）
sh scripts/scan/scan.sh                                       → Summary: 109 passed, 0 failed
python3 scripts/scan/test_scan.py                             → ✅ ALL 336 CASES PASS
```

### L2 邻门（按概念反查，非照抄任务书名）

`grep -rln "RootfsManagementViewModel|StorageManagementScreen|StorageSnapshot|RootfsManager" scripts/scan/*.py` → 3 门；语义邻居各 1 道：

| 门 | 关系 | rc |
|---|---|---|
| `readonly_mount_write_guard.py` | 锚定 `RootfsManagementViewModel`（我只增调用、不改既有锚点行形态） | 0 |
| `stream_flow_dispatch_guard.py` | 同一母题：派发/线程所有权 | 0 |
| `skill_import_offmain_check.py` | 同一母题：off-Main IO 形态 | 0 |

### L3 真 `kotlinc` 2.0.21 + `JUnitCore` 双臂 + 6 变异臂

装置：`probe/l3/`（`prep_arms.py` 生成臂素材，`arm.sh` 单臂一次调用，臂间 `delay`；每臂独立目录，**源文件全程不变**）。classpath 显式带 `kotlin-stdlib` + `junit:hamcrest`。

| 臂 | 构建 | 结果 |
|---|---|---|
| `post`（本支树） | compile 0 | **OK (15 tests)** |
| `pre`（基线 blob，无 `StorageScanPolicy.kt`） | **compile 1** | `unresolved reference 'StorageScanDecision'` / `'decideScan'` —— 新鲜度决策在基线里**没有可测的家**（内联在 composable 里） |
| `pre_shape`（**本支策略** + 基线 screen/VM） | compile 0 | **8 红 / 15**：`noComposeStateIsAssignedInsideABackgroundBlock` 报 `[204:shellSize, 210:shellBreakdown, 214:dbSize, 223:sessionCount, 230:orphanInfo, 245:sessions, 528:session, 529:minisSize, 530:mediaSize, 637:minisSize, 638:mediaSize]`（= 门的 11 条，逐条一致）· 单清除点 0 ⇒ 红 · 无外部入口 ⇒ 红 · install/reset/repair 缺失效 ⇒ 3 红 · 发布顺序 ⇒ 红 |
| `m1`（`>=` → `>`） | 0 | **1 红**：`theWindowIsInclusiveOnTheStaleSide` |
| `m2`（`force ||` 去掉） | 0 | **1 红**：`aForcedReloadNeverReusesTheSnapshot` |
| `m3`（时钟回退 → 重扫） | 0 | **1 红**：`aBackwardsClockReadsAsFresh` |
| `m4`（把 `dbSize` 的赋值搬回 IO 块） | 0 | **1 红**：`noComposeStateIsAssignedInsideABackgroundBlock` |
| `m5`（删 `install()` 的失效） | 0 | **1 红**：`installingInvalidatesBeforeItMutates` |
| `m6`（加第二个清除点） | 0 | **1 红**：`theCacheHasExactlyOneClearingSite` |

> 诚实标注：`decideScan` 的 5 条用例是**回归**证据（基线与新式在这 5 个输入上等价），**不是**缺陷证据 —— B-03 的缺陷是线程所有权，缺陷证据在 L1 的 PRE 门运行与 `pre_shape` 臂（11 处 off-Main 写、0 个失效入口）。两条正控（命名实参不误报、lambda 实参内必报）保证检测器本身带牙，而不是靠被它检查的代码自我证明。

### L4 分支 CI

见 §8。

---

## §6 任务书失真

| # | 任务书原文 | 真树 | 处置 |
|---|---|---|---|
| 1 | 「9 处 Compose 裸写落在 `withContext(Dispatchers.IO)` 内」；「清除回调（`:635-639`）**3 处**」 | **11 处**：主扫描 6 + 详情 reload 3 + 清除回调 **2**（那 3 行里第三行是 `StorageSnapshotCache.snapshot = null`，不是 Compose state） | 全 11 处都修了；同时在门里补了这个形状（§4.2 臂 9）。TRIAGE 的「9 处」同样少算 2 处。 |
| 2 | 「把 `StorageSnapshotCache` 提到可被 `RootfsManagementViewModel` 调用的位置（同包 internal object 或把 `invalidate()` 暴露为 internal 顶层函数）」 | 两层都在不同包（`ui.settings` vs `ui.sandbox`），「同包」不成立 | 走第二个选项：顶层 `internal fun invalidateStorageSnapshot()`，缓存对象保持 `private`。 |
| 3 | 「install / repair / reset / **uninstall** / clear 成功路径」 | `ui/sandbox/**` 与 `StorageManagementScreen.kt` 里 **0 处** uninstall（`grep -rn uninstall` 空）；rootfs 卸载能力不存在 | 按「mutation 之前」实现（而不是「成功路径」），理由见 §2.3：`reset()` 先删目录，失败路径同样已经把 shell 树改掉了。uninstall 落地当天由门的 mutate 集派生自动覆盖（无需改门）。 |
| 4 | 「`shouldRescan(cached, force, now, staleMs): Boolean` + `decideInvalidate(event)`」 | 前者折成三值 `decideScan`（时序不同），后者是恒真谓词 | §2.3 逐条给了否决理由。 |
| 5 | 「`StorageManagementScreen.kt:189-274` 主扫描块 … `:245` sessions」 | 行号全部对得上（`withContext` 在 `:189`，`sessions =` 在 `:245`） | 无失真，记一条「锚点准确」。 |
| 6 | 「`SessionStorageDetailScreen.reload`（`:527-531`）3 处 + 清除回调（`:635-639`）3 处」 | 详情 reload 是 `:527-531`（3 处 ✓）；清除回调真实是 `:635-640`，IO 块内 2 处 state + 1 处缓存写 | 见 #1。 |

---

## §7 残留（R）

- **R1（真机行为级，必验）**：装包 / 重置 / 修复 rootfs 后**返回存储页**，「Terminal Shell」数字应立即变化（不再需要杀进程）；同一路径下 < 3 分钟的二次进入不得出现骨架屏闪烁。触发条件：下次真机窗口。验证法：设置 → 存储 → 记下 shell 数字 → 进 rootfs 管理 → 安装/重置 → 返回存储 → 比对。
- **R2（多实例竞态，窄）**：两份屏幕实例同时 `reload()` 时，仍可能「后发布覆盖先发布」（缓存被后者覆盖）。已收窄到「一次扫描内部自洽」（不再出现字段跨扫描混拼），但跨实例顺序未定义。触发条件：同一进程里同时存在两个 Storage 页实例且都触发 rescan（当前导航是单栈，不可达）。触发后方案：给 `reload()` 加单调 `scanId`，只发布 `scanId >= lastPublished` 的结果。
- **R3（延迟窗口内失效 → 已处理，留监控）**：`RESCAN_DELAY_MS` 期间若发生失效，现在靠「IO 前重读缓存」兜住；若将来有人把这个重读改成复用捕获引用，缓存会重新被钉住。触发条件：该行被改动。触发后方案：门 F 不覆盖此形状，需在 `StorageScanPolicyTest` 加「失效后不得携带旧 totals」的用例。
- **R4（门的判据形态边界）**：F 只看**行首**赋值（`name = …`）。写成单行 `if (x) shellSize = 1` 的赋值不会被抓。触发条件：出现单行控制流内联赋值。触发后方案：把语句扫描换成「按 `;`/`{`/`}` 切分后再匹配行内赋值」，代价是复杂度上升 —— 按修复门登记不修（P2）。
- **R5（`StorageSnapshot` 仍在同一文件私有）**：`scanned` 的发布是 8 条独立赋值，将来若新增字段忘了发布，会静默不显示（不会被门抓到）。触发条件：给 `StorageSnapshot` 加字段。触发后方案：改成一次 `data class UiSnapshot` 整体 `mutableStateOf`（同时覆盖 R2 的顺序问题）。
- **R6（`refresh()` 只读，正确但不设防）**：若将来 `refresh` 开始调用 mutate 成员，门的 mutate 集派生会自动要求失效（无需改门）—— 这条是「已验证的正面性质」，登记以免后人误以为漏检。
- **R7（沙箱 S28 未触发）**：本轮 `scan.sh` 全量一次跑通（未 SIGTERM），`test_scan` 也一次跑通 —— 与 1044 波「需分块」不同，记录以免后人把「必须分块」当硬纪律。

---

## §8 身份链

| 环节 | 值 |
|---|---|
| 基线 | main `1d44ec905b8f5d21a215694e9a13910c74e1f645` |
| 分支 | `fix/dv25-b3-storage-io-offmain` |
| 源码树提交（CI 验证对象） | **`d4d6886c114f55263eb487060cf782000c480a30`** |
| 远端同点 | `git ls-remote origin` = `d4d6886c…`（逐字符一致） |
| CI run | **#2541 / run `39916839435` / job `120292573919` / attempt 1 / success** |
| run `head_sha` | `d4d6886c…`（与本地逐字符一致） |
| 步骤 | 26 步，`non-success = [(23, "Publish to Releases", "skipped")]` —— 唯一 skip 是 main 门控，**Publish 未执行 = 分支不产资产** |
| 日志 | 1,413,757 B（`probe/l3/job-120292573919.log`） |

**CI 判读 14/14 ALL GREEN**（`probe/l3/judge_ci.py`，判据全部先取真样本再写：`Summary: 109 passed, 0 failed` 恰一条且无非零；门头 `[n/109]` 共 109 条且互异；`ALL 336 CASES PASS`；`Z e: ` = 0；`sysinfo_state_thread_check selftest has teeth (SYSINFO-STATE SELFTEST=PASS)` 在册；`sysinfo_state_thread_check real tree green` 在册（= 判据 F/G 在 CI 真树上跑绿）；新用例 **15/15 PASSED**、0 FAILED；行尾 ` FAILED` 结果标记 0 条）。

**两次判读自伤（已修，留档）**：①teeth 见证行真形态是 `sysinfo_state_thread_check selftest has teeth (…)`（**无 `.py`**），按任务习惯写 `….py` 直接假红；②「游离 FAILED」不能按词匹配 —— 日志里有用例名叫 `phase FAILED maps to Failed`，行尾结果是 `PASSED`。判据改为「行尾最后一个词是 `FAILED`」。这是「写判据前先 grep 一条真样本」的又一次自证。

**报告回填后的源码树等价性**：报告提交只加 `fix-reports/FIX-7.md`（不在 `build-apk.yml` 的 paths 内，不参与构建）。
`git diff --name-only d4d6886c HEAD -- . ':(exclude)fix-reports'` 输出为空 ⇒ 当前 HEAD 的源码树与 CI 验证过的树**逐字节一致**，CI 结论对当前代码成立。

**未合并 main**（含 `.kt` 验证面，合并起手权在用户）。真机行为级欠账见 §7-R1，统一进 `§1045-VERIFY`。


# FIX-5 · deep-audit-1045（v24 波）· 第 5 支 · B-01 P1 · 首启自动测速的过期执行体覆盖用户手拨镜像

**分支** `fix/dv25-b1-mirror-autodetect-ownership`
**基线** main `1d44ec905b8f5d21a215694e9a13910c74e1f645`（`ls-remote` 自核一致）
**工单** B-01（面 B，P1）· 门独占：`scripts/scan/mirror_vm_thread_check.py`（0 新建门，门数 109 不变）
**停止节点** 分支 CI 绿；**未合并 main**（含 `.kt` 验证面，起手权在用户）

---

## §1 缺陷与根因

### 症状原话（报告 B-01）

> `autoDetectOnceIfNeeded` 的**过期执行体**在用户已发起测试后仍继续结算：`runTest` 的 `currentJob?.cancel()` 只取消了 `runAllTests` 的内部 job，`autoDetectOnceIfNeeded` 自己的 `scope.launch` 仍存活，`currentJob?.join()` 对已取消 job 立即返回 → 随后按 `results` 覆盖 `selectedMirrorId` / `useCustomMirror` / prefs / rootfs 配置，把用户刚拨的镜像默默改回。

真实触发面（R1）：`rootfs.freshInstall=true` 的首次启动会自动跑一次全类测速并自动应用最快镜像；用户在这几十秒里手拨某个类别的镜像（`MirrorSettingsScreen:913` 的行点击，或该行的「测试」按钮），首次自动结算落地时把这次手拨覆盖掉——而且不只是 id：还会把 `useCustomMirror` 置 `true` 并重写 rootfs 配置文件。

### 根因假设

三个条件同时成立才构成这个 bug，缺一条都不成立：

1. **检测协程没有所有权位**：基线 `:478` 是裸 `scope.launch { … }`，它的 Job 从未写进任何字段 ⇒ `runTest`/`runAllTests` 的 `currentJob?.cancel()` 取消不到它。
2. **`join()` 等的是「内层 job」，不是「检测完成」**：基线 `:480-481` `runAllTests(context); currentJob?.join()`。内层 job 被取消后 `join()` **立刻正常返回**（取消 ≠ 异常），于是「等测速跑完」这个语义在最关键的时刻提前放行。
3. **结算循环没有「已选」守卫**：基线 `:484` `for (cat in MirrorCategory.entries)` 无条件遍历，只要 `results[cat]` 里有成功项且非官方就写。同一文件的用户发起路径**都有**这个守卫——`:307` 和 `:371` 都是 `autoSelectFastest(sorted) { selectedMirrorId[it] != null }`。检测路径是唯一绕过它的入口。

而 `selectMirror`（基线 `:427-435`）是**同步**写 `selectedMirrorId` + `persist`（`applyMirror` 才 offload），所以用户手拨的落点与自动结算的落点完全重叠 ⇒ 后写者赢，而自动结算恰好是后写者。

### 真树 Before 原文（`git show 1d44ec90:…/MirrorSettingsScreen.kt`）

```kotlin
    fun autoDetectOnceIfNeeded(context: Context) {                       // :471
        …
        scope.launch {                                                    // :478 ① 无所有权位
            // Reuse the normal test flow, then await its completion via currentJob.
            runAllTests(context)                                          // :480
            currentJob?.join()                                            // :481 ② 取消即返回
            val toApply = mutableListOf<MirrorCategory>()
            withContext(Dispatchers.Main) {
                for (cat in MirrorCategory.entries) {                     // :484 ③ 无守卫
                    val fastest = results[cat]?.firstOrNull { it.isSuccess } ?: continue
                    if (fastest.mirror.isOfficial) continue
                    selectedMirrorId[cat] = fastest.mirror.id             // :488 覆盖用户手拨
                    useCustomMirror[cat] = true                           // :489 顺带翻转开关
                    persist(context, cat)                                 // :490
                    toApply += cat
                }
            }
            for (cat in toApply) applyMirror(context, cat)                // :503 重写 rootfs 配置
            Log.i(TAG, "Auto mirror detection applied")
        }
    }
```

对照（同文件的既有守卫，两处一字不差的形态）：

```kotlin
                    for ((cat, fastest) in autoSelectFastest(sorted) { selectedMirrorId[it] != null }) {   // :307 runAllTests
                    for ((cat, fastest) in autoSelectFastest(sorted) { selectedMirrorId[it] != null }) {   // :371 runTest
```

### ≥1 条证据

1. **门在基线树上精确红两条**（`probe/l1/base`，只放基线原文）：`arm 5 … does not skip already-selected categories` + `arm 6 … no ownership slot (bare scope.launch)`，**arm 1–4 全绿**（`zero`，见 §5 L1-PRE）。⇒ 红点正是本轮要修的两件事，不是门的误伤。
2. **`selectMirror` 原文**（上）：用户手拨写的是同一张 `selectedMirrorId` 表 + 同一份 prefs，证明两者的落点重叠，「后写者赢」成立。
3. **`join()` 语义**：`Job.cancel()` 后 `join()` 正常返回（不是抛 `CancellationException`），这是 ② 成立的机制；本轮把它写进门 arm 6 的吊销判据里（检测 job 必须被**取消**，而不是靠 join 收尾）。

---

## §2 修法

### 停在阶梯第 6 级（接既有守卫形态 + 既有单源），不是第 1–5 级

- 第 2 级「代码库里已有」：守卫形态与判定单源都已存在（`autoSelectFastest(sorted) { selectedMirrorId[it] != null }`，同一文件两处），不是新造机制。
- 第 6 级「写一行/最小实现」：新增的纯决策函数只有两行表达式（委托 `autoSelectFastest` + 过滤官方）。
- 没有新增全局状态总线、没有新抽象、没有改 `selectMirror` 的写语义。

### 被否决的方案与理由

| 方案 | 否决理由 |
|---|---|
| 只在 `join()` 后加 `if (results.isEmpty()) return` | `results` 由内层运行写满，取消了也非空；且完全不覆盖「用户手拨发生在内层运行**完成之后**」这个窗口 |
| 加 `@Volatile private var autoDetecting: Boolean`，入口置 false | 需要两处写入之间的原子性（置位与启动之间可插队），且与文件既有的 `currentJob === self` 身份判据风格不一致，还多一个可与 job 不一致的真相源 |
| 让检测继续调 `runAllTests`，在 `runAllTests` 里判断「调用者是不是检测协程」 | 非 suspend 函数里拿不到 `coroutineContext`（`currentCoroutineContext()` 是 suspend）；只能靠 `ThreadLocal` 之类的隐式契约，跨线程易碎 |
| 直接删掉二次结算循环（报告 §9 建议） | 见下方「关于报告建议删循环」 |
| 把 `selectMirror` 也改成取消检测 | 方向反了：用户手拨只该赢一格（那一类），不该把整轮检测连同其它类一起打断；守卫天然做到「只让这一类」 |

**关于报告建议删循环**：报告认为「`runAllTests` 已经自动选了，二次循环多余」。这一点在**修复后**确实成立（见 §7 R1：守卫 + 内层同款守卫 ⇒ 结算循环在正常路径上是构造性 no-op）。本支仍保留循环，理由：任务书 §3/§4 把「带守卫的结算循环」钉成门锚点（arm 5），删掉会让该判据失去落点、并把「万一内层结算前提变化」的安全网一起删掉；删循环属于独立决策，起手权在用户/下一轮评估。**本项已如实登记为 R1，不假装修法顺手清掉了它。**

### 修法本体

1. **所有权位**（`:198` 起）：`@Volatile private var autoDetectJob: Job? = null`。
2. **用户入口抢占**（`runAllTests` / `runTest` 首行）：`autoDetectJob?.cancel()`。取消是**主防线**——被取消的检测协程停在 `join()` 上，`join()` 抛 `CancellationException`，结算永不发生。
3. **检测自带所有权 + LAZY 发布**：`val job = scope.launch(start = CoroutineStart.LAZY) { … }; autoDetectJob = job; job.start()`——先发布再启动，保证协程体内的 `autoDetectJob !== self` 判据不会读到半初始化的字段。
4. **结算守卫**（纯决策 `decideAutoApply`）：已选类别跳过（拒绝「后写者赢」），官方源永不自动选（first-success 语义与基线一致）。检测侧不再遍历 `MirrorCategory.entries`。
5. **结算前归属判据**：`if (autoDetectJob !== self) return@withContext`，且放在 `withContext(Dispatchers.Main)` 块的**最前**——与用户 `selectMirror` 的写同在主线程，块内不会被打断，「检查完就写」是原子的。
6. **用户运行优先**：检测体一进来若 `currentJob?.isActive == true`（用户已经发起了一轮），检测直接放弃并记日志，而不是把用户那轮 `cancel` 掉。
7. **反向保护**（`runAllTests`/`runTest` 的结算块）：`if (currentJob !== self) return@withContext`——被取代的旧运行不得把旧结果写进新运行的地图。配套把两处启动改成 LAZY + 显式 `start()`，否则「协程体读 `currentJob`」与「`currentJob = job` 赋值」之间有竞态窗口（本判据比既有的 `finally` 判据更早触发，所以必须排掉）。
8. **复用而非取消**：测试体下沉到 `private fun startAllTests(context)`，`runAllTests` 变成「先抢占检测，再启动」的两行入口；检测调 `startAllTests`，否则它会取消自己所在的 job。

---

## §3 改动清单

| 文件 | 增/删 | 内容 |
|---|---:|---|
| `src/android/app/src/main/java/com/rikkaminis/app/ui/sandbox/MirrorSettingsScreen.kt` | +98 / −8 | 所有权位、入口抢占、LAZY 发布、结算归属判据、`decideAutoApply`、`startAllTests` 拆分 |
| `src/android/app/src/test/java/com/rikkaminis/app/ui/sandbox/MirrorAutoDetectDecisionTest.kt`（新增） | +170 | 8 条纯 JVM 决策用例 |
| `src/android/app/src/test/java/com/rikkaminis/app/ui/sandbox/MirrorSpeedTestShapeTest.kt` | +100 / −2 | E4 跟随下沉后的测试体（仍要求入口可达）；新增 E5（所有权）与 E6（守卫） |
| `scripts/scan/mirror_vm_thread_check.py` | +410 / −74 | 新增 arm 5/6/7；arm 3 兼容共享体；selftest 6 → 12 臂 |

合计 **4 文件 +778 / −84**；`scan.sh` / `test_scan.py` / `.github/` / gradle / manifest **零触碰**；门数 **109 → 109**（0 新建门）；夹具 **336 → 336**。

---

## §4 门判据与带牙证明

### 新增判据（全部从树派生，先剥注释）

| 臂 | 判据 | 派生方式（不锚函数名/不锚拼写） |
|---|---|---|
| **5** | 结算循环必须让「已选类别」出局 | 先找「写 `selectedMirrorId[…]=` 的 `for` 块」；内联判据 = 块内出现**读取**型已选标记（`selectedMirrorId[x] != null` / `alreadySelected` / `hasPriorSelection(` / `.keys`）**且**有跳过（`continue` / `return@…`）；否则沿循环头部的调用走最多两跳，要求第 1 跳读到已选、第 2 跳执行跳过。**结算自身的写入不算「读取」**（首版把写入当标记，导致无守卫形态假绿，见 §6） |
| **6** | 检测协程必须发布到可取消槽位，且两个用户入口都取消它 | 从 `autoDetectOnceIfNeeded` 体内找 `<槽> = ….launch`；若左侧是 `val/var` 本地，则继续找 `<字段> = <本地>` 才算槽位（**只有本地 val = 没有槽位**，正是本缺陷形态）；再要求 `@Volatile private var <槽>` 且 `runAllTests` / `runTest` 体内都有 `<槽>?.cancel()`；LAZY 发布还要求 `<本地>.start()` |
| **7** | 结算在写之前做归属检查 | 取 `autoDetectOnceIfNeeded` 内含 `selectedMirrorId[…]=` 的 `withContext(Dispatchers.Main)` 块，要求出现 `<槽> !==` / `<槽> ===` / `isActive` |

### 带牙臂（`--selftest`，12 条，逐条断言「红在它该红的那条」）

| 臂 | 变异 | 期望 |
|---|---|---|
| A | 合规形态（= 线上形态的合成镜像） | 全绿 |
| V1 | 删 `useCustomMirror[cat] = true` | arm 3 |
| V2 | `applyMirror` 挪回 Main 块 | arm 2 |
| V3 | `ensureLoaded` 去掉 `Snapshot.withMutableSnapshot` | arm 1 |
| V4 | `retryApkWorld` 去掉 `Snapshot.withMutableSnapshot` | arm 4 |
| V5 | 决策不再读已选（`{ false }`） | arm 5 |
| V5b | 第 2 跳丢掉 `hasPriorSelection … return@` | arm 5 |
| V6 | 检测退回裸 `scope.launch`（无槽位） | arm 6 |
| V7 | `runTest` 忘记 `<槽>?.cancel()` | arm 6 |
| V8 | 结算丢掉归属判据 | arm 7 |
| V9 | 发货前基线形态（两部分拼接） | arm 5 + arm 6（arm 1–4 仍绿） |
| V10 | 槽位去掉 `@Volatile` | arm 6 |

`V9` 与真树基线（`git show 1d44ec90:…`）**同样只红两条**，两处独立取证一致。

---

## §5 验证四层

### L1 门

- `mirror_vm_thread_check.py .` → **`MIRROR_VM_THREAD_CHECK=PASS`**（真树 rc=0）
- `--selftest` → **12/12**，`MIRROR_VM_THREAD_SELFTEST=PASS`
- 基线树（只放 `1d44ec90` 原文）→ **恰 2 条**：arm 5 + arm 6（arm 1–4 绿）
- 全量 `sh scripts/scan/scan.sh` → **109 passed / 0 failed**（`probe/l1/scan.log`）
- `python3 scripts/scan/test_scan.py` → **ALL 336 CASES PASS**（`probe/l1/test_scan.log`，337 条 ✅）

### L2 邻门（按概念反查，非照抄任务书）

`grep -rln "MirrorSettingsScreen" scripts/scan/*.py` 只命中 2 道（本门 + `mirror_category_route_check.py`），故按**概念**扩到并发/IO/rootfs 面，6/6 rc=0：

`mirror_category_route_check` · `discarded_return_guard` · `stream_flow_dispatch_guard` · `readonly_mount_write_guard` · `skill_import_offmain_check` · `skill_file_save_result_check`

### L3 真 JVM（kotlinc 2.0.21 + JUnit 4.13.2 + 真 kotlinx-coroutines-core-jvm 1.9.0）

**(a) 纯决策四臂**（切片取自真树，`probe/l3/`）：

| 臂 | 结果 |
|---|---|
| **POST**（线上 `decideAutoApply`） | `OK (8 tests)` |
| **PRE**（基线等价实现：遍历 entries、只看成功与官方，**完全不读已选**） | `Tests run: 8, Failures: 3` —— 恰是三条「用户已选」用例 |
| **M1**（删官方过滤） | `Failures: 1` —— 仅 `never auto-selects the official CDN` |
| **M2**（守卫取反 `it !in alreadySelected`） | `Failures: 6` |

**(b) 生产切片真编译**（额外一层，`probe/l3/prod/`）：把 `MirrorSpeedTestViewModel` 对象体 + `MirrorCatalog` + 模型类 + 两个纯函数**逐字节**抽出，配 Compose/android/okhttp 桩 + 真协程库编译：

- `kotlinc … src/Prod.kt + stubs` → **rc=0，0 error / 0 warning**（含 LAZY 启动、`start()`、`return@withContext` 标签、`currentJob !== self`、`autoDetectJob !== self`、`isActive`、`results.toMap()`、`selectedMirrorId.keys`）
- `MirrorSpeedTestShapeTest` 在该产物上跑：**POST（w5）6/6 OK**；**PRE（基线树 cwd）恰 E5 + E6 红、E1–E4 绿**

### L4 CI

**分支 CI #2540**（run `39916511775` / job `120291145940`，`event=workflow_dispatch`）attempt 1 **success**，`head_sha = 6e929cd3` 与本地 HEAD 逐字符一致；26 步 `non-success = []`，唯一 skipped = 第 23 步 `Publish to Releases`（main 门控，分支上本应 skip）；`started 07:14:52Z → completed 07:27:47Z`，日志 **1,408,960 B**。

`judge_generic.py` → **ALL GENERIC CRITERIA GREEN**：

| 判据 | 读数 |
|---|---|
| `Summary` 行 | 9 条，非零 0 条，恰一条 `109 passed, 0 failed` |
| 夹具 | `✅ ALL 336 CASES PASS` ×1 |
| 门头 | 109 条，序号 1..109 连续、门名 109 互异 |
| 编译错 | `Z e: ` **0** |
| 游离 FAILED | 6 条含 `FAILED` 的行，逐条确认全是 PASSED 用例名子串；游离 0 |

本支专属证据（逐条在册）：

| 证据 | 日志位置 |
|---|---|
| `MIRROR_VM_THREAD_CHECK=PASS`（门真树） | `:759` |
| `✅ mirror_vm_thread_check selftest has teeth (MIRROR_VM_THREAD_SELFTEST=PASS)` | `:1210` |
| `✅ mirror_vm_thread_check real tree green` | `:1211` |
| `MirrorAutoDetectDecisionTest` **8/8 PASSED** | 8 条用例名逐条在册 |
| `MirrorSpeedTestShapeTest` **6/6 PASSED**（E1–E6，含新增 E5/E6） | 6 条在册 |

> 判据取样纪律：新增用例的 PASSED 行带 CI 时间戳前缀，锚行首会 0 命中——判据用 `MirrorAutoDetectDecisionTest > .* PASSED`，不锚行首（同族第 N 次）。

---

## §6 任务书失真

| 任务书说法 | 真树 |
|---|---|
| `:200` 字段 / `:256-330` `runAllTests` / `:331-…` `runTest` / `:471-505` `autoDetectOnceIfNeeded` | 真值 `:198` / `:255-328` / `:330-393` / `:471-505` —— 偏差 1–2 行，不影响定位 |
| `:478` 裸 launch、`:480-481` runAllTests + join、`:483-497` 无守卫循环、`:503` commit、`:913` UI 入口 | **全部逐字命中** |
| §4「自测 4 个新臂（删守卫 / 裸 launch / 基线 / 合规）」 | 实际需要 **12 臂**才覆盖到精确性：`runTest` 忘记 cancel 与槽位非 `@Volatile` 是两个独立回归面，只做 4 臂会让「改了 runAllTests 忘了 runTest」静默通过 |
| §5「PRE 臂（基线等价实现）红在已选类仍被返回」 | 成立，PRE 精确 3 红 |

**本轮自伤（已闭环）**：门与形状测试的槽位派生首版都用了 `(\w+)\s*=\s*scope\.launch`，把 `val job = scope.launch` 这个**本地**当成了所有权槽 ⇒ 线上形态被判红（门首跑 FALSE RED）。修法 = 认「本地 + 后续 `<字段> = <本地>`」两段式；同款逻辑同时修在门与 JVM 形状臂里，避免两处判据漂移。另：门的「已选标记」首版把 **写入** `selectedMirrorId[…]` 也算成读取 ⇒ 无守卫形态假绿（V9 只红一条），修法 = 只认读取型标记。

---

## §7 未做 / 残留

| # | 残留 | 触发条件 / 备注 |
|---|---|---|
| **R1** | **结算循环修复后是构造性 no-op**：`runAllTests` 用同一守卫 `{ selectedMirrorId[it] != null }` 已经把所有未选类别写好并 `applyMirror` 完，检测再结算时 `alreadySelected` = 全集 ⇒ `decideAutoApply` 返回空。候选简化 = 删掉二次循环（只留 `join()` + 日志）与 `decideAutoApply` | 未做：任务书 §3/§4 把「带守卫的结算循环」钉成门锚点；删循环会改变门的形态，属独立决策。**触发**：下次动 `[da1022-F]` 的 applyMirror 拆分、或有人提议简化本文件时，一并把门 arm 5 改成「不得有二次覆写」判据 |
| **R2** | 真机行为级未验 | 需 `rootfs.freshInstall=true` 的首启窗口手拨镜像。触发：下次真机窗口，按 `§1045-VERIFY` 一次开齐（本支为其中一条） |
| **R3** | `selectMirror` 不取消检测，只靠 arm 5 的守卫 + 主线程串行 | 若将来 `selectMirror` 改成非 Main 写（例如把 `selectedMirrorId` 写入挪进 IO），守卫的「检查-写入原子性」失效 |
| **R4** | 「官方源恰好最快」的类别被整类丢弃（`autoSelectFastest` 取 first-success 后再过滤官方），基线同款 | 刻意保留（改语义 = 越出本缺陷半径）。触发：若用户报「首启有类别没被自动选」，先看这一条 |
| **R5** | `autoDetectJob` 只在 `autoDetectOnceIfNeeded` 内取消一次；`freshInstall` 标志在启动前已删除 ⇒ 第二次调用早退，不会重入 | 无 |
| **R6** | 两处启动由 eager 改 LAZY + 显式 `start()`（为排掉 `currentJob` 发布前的竞态） | 对行为无差异；门 arm 6 有 `.start()` 判据，避免将来退回「LAZY 但忘了 start」的死协程 |
| **R7** | 沙箱侧未复现 S28（全量门一次跑通） | 无 |

---

## §8 身份链

```
基线 main          1d44ec905b8f5d21a215694e9a13910c74e1f645
生产改动           4bc3ff25  fix(mirror): first-boot auto-detection must not overwrite a manual pick (dv25-B1)
测试                e6d9a354  test(mirror): B-01 decision arms + auto-detection ownership shape arms
门                  6e929cd3  gate(mirror): arms 5-7 pin auto-detection ownership + user-selection precedence
CI 验证的 sha       6e929cd333cb3aa183533c640ef538490e99d9fd   ← 本地 == ls-remote == run.head_sha（逐字符一致）
分支尖端（报告后）  （见 §8.2：只追加 fix-reports/FIX-5.md）
```

### §8.1 CI

- run **2540** / run_id `39916511775` / job `120291145940` / attempt **1** / **success**
- `head_sha 6e929cd3…`（三方逐字符一致）
- 26 步 `bad = []`；唯一 skipped = `Publish to Releases`（main 门控）
- 日志 1,408,960 B；`judge_generic.py` **ALL GREEN**（明细见 §5 L4）

### §8.2 报告回填后的源码树等价声明

报告 `fix-reports/FIX-5.md` 在 CI 绿之后才写入并提交，`fix-reports/` 不在 workflow `paths` 内、不参与构建。因此按惯例**声明源码树等价**而不是重跑一轮 CI：

```
git diff --name-only 6e929cd3 HEAD -- . ':(exclude)fix-reports'   → 空
```

即 CI 验证过的那棵树（`src/` + `scripts/`）就是当前分支的源码树。

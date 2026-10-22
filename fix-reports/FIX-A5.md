# FIX-A5 — §1042-A-05（P1）`HeadlessChatRunner` 四入口缺 `sessionLoaded` 屏障

**分支** `fix/dv22-a5-headless-session-ready` · **基线** `5dd278dae155c1e15f5171386479151603da3d91`（main）
**工作树** `/var/minis/shared/work/deep-audit-1042/a5/`（私有 worktree，未 clone、未切分支、未碰 golden）
**规模** 3 文件 +776/−4（生产 1 / 测试 1 / 门 1）；**门 109 → 109（0 新建门，criterion 14 为扩展）/ 夹具 336 不变**；`scan.sh` / `test_scan.py` 冻结面 0 触碰。

---

## 1 缺陷与触发路径（真实调用链）

### 1.1 窗口的来源：`loadSession` 是两步发布

`ChatSessionLifecycle.loadSession`（`ui/chat/ChatSessionLifecycle.kt`）把「就绪」拆成两个时间点：

| 步骤 | 位置 | 发布的东西 |
|---|---|---|
| 步骤 1 | `:423` `_activeEntryId.value = entry.id` | 绑定条目（RPC 层一直等的是它） |
| 步骤 2 | `:477` `withContext(Dispatchers.IO) { … }` | 真正读消息、重建 `_messages` |
| 步骤 2 的 finally | `:786` `_sessionLoaded.value = true` | 消息列表可用的信号 |

`HeadlessChatRunner` 的四个变更入口在修前**只等 `activeEntryId`**（真树行号，全仓 `activeEntryId.first` 6 处：`209 / 228 / 357 / 417 / 494 / 584`；`sessionLoaded.first` 仅 `253` 一处，即 `send` 的 5s 兜底）。步骤 1 到步骤 2-finally 之间是**消息列表还空着的窗口**。

### 1.2 窗口里的四个入口各自怎么坏

| 入口 | 真树行 | 窗口内发生什么 | 后果 |
|---|---|---|---|
| `retry` | `:332` | `ChatViewModel.retryFromMessage:3185` → `:3199 val index = messages.indexOfFirst { it.id == messageId }` → `if (index < 0) return` | 静默 return，什么都不做 |
| `rerunFromToolBlock` | `:406` | `ChatViewModel.rerunFromToolBlock:2956` 的 `asstIdx < 0` / `blockIdx < 0` → `return false`；**退化分支**（同一轮内无前置内容）改为 `retryFromMessage(precedingUser)` 后 `return true` —— 而那条路径照样会被 `index < 0` 打掉 | 无流启动 |
| `compact` | `:481` | 压缩器遍历的是内存历史，此刻为空 | 结构性地读成「无可压缩」 |
| `revertCompact` | `:580` | `ChatViewModel.revertCompact:2331` 读 `_cachedLatestMarker`，`CompactBoundaryMarkerHolder.kt:94` 注释写明「before the first load completes it is null, so callers treat null as no boundary」 | 静默 noop，但 `done` 事件无条件发 |

### 1.3 为什么是「假成功」而不是「无反应」

`retry` / `rerunFromToolBlock` 的 `wait=true` 分支：

```kotlin
val finished = withTimeoutOrNull(timeoutMs) {
    if (vm.isStreaming.value) { vm.isStreaming.first { !it } }
    true
} ?: false
```

没有流起来时 `vm.isStreaming.value == false`，**整块立即返回 `true`** ⇒ `status = "Completed"`，`responseText` 取的是 `loadMessages(...).lastOrNull { role == "assistant" }` —— 也就是**上一条** assistant 文本。调用方（调度器 / RPC 客户端 / e2e）看到的是「重试成功、模型回复了」，而实际上一条 LLM 请求都没发。

---

## 2 修法（停在第几级阶梯、为什么）

**停在阶梯第 3 级（标准库/既有形态）**：不新增抽象、不加依赖、不加配置项，照同文件 `:253` 既有形状复制到四个入口。

四入口统一补屏障（位置：各自 `viewModel(...)` 之后、任何变更调用之前）：

```kotlin
val sessionReady = withContext(Dispatchers.Default) {
    withTimeoutOrNull(5000L) { vm.sessionLoaded.first { it } }
}
```

三条硬约束（都是任务书点明、且门/测试都钉住的判据）：

1. **必须 `Dispatchers.Default`**：加载在 `viewModelScope`（Main.immediate）上恢复，直接在 Main 上 `first {}` 会与它互相等待 = 死锁。`prompt` 的 `:238-253` 注释已经记过同款结论。
2. **必须有超时**：无界等待会把 RPC 连接永久挂住。
3. **超时不抛，转成结构化判定**：超时只是「不知道」，不是「成功」。

各入口的判定：

| 入口 | 超时后 | 变更没起来 |
|---|---|---|
| `retry` | `PromptResult(status="Error", responseText="session_not_loaded_in_5s")` | `retry_dropped_before_stream`（`wait` 与 `!wait` 两个分支都不再落到 `Completed`/`Retrying`） |
| `rerunFromToolBlock` | 同上信封 | `rerun_dropped_before_stream`（新增：`accepted == true` 不等于「流起来了」——退化分支委托给 `retryFromMessage`，那条路仍可静默掉落） |
| `compact` | `CompactResult(status="Error", error="session_not_loaded_in_5s")` | 未改动（既有 `NoOp`/`compact_skipped_no_change` 判据本来就是诚实的） |
| `revertCompact` | `RPCException(-32000, "session_not_loaded_in_5s: <sid>")` | 未改动（`revertCompact` 内部把 busy/nothing 都走 system-info，语义已表达） |

**`started` 守卫的依据**：`retryFromMessage` / `rerunFromToolBlock` 都在**同步**路径上申领 `_isStreaming`（`ChatViewModel:3260` 附近的 `T145` / `T-claim-token-vs-stale-finally` 注释），所以「调用后立刻在同一条 Main 上读 `vm.isStreaming.value`」等价于「这次调用到底有没有起来」。这与同文件 `prompt` 的 `send_dropped_before_stream`（`da1016 B-7b`）是同一族判据，复用了既有状态词（不新造状态字符串，`status != "Running"` 的调度器契约不变）。

**`prompt` 的既有 5s 兜底一字未动**（`sessionReady == null` 时只 `Log.w` 后继续），因为对 `send` 来说「修前行为是地板不是新失败模式」这条理由仍然成立；四个变更入口的语义相反（变更是有目标对象的，目标没就绪就不能假装做了）。

---

## 3 门扩展（判据 + 带牙证明）

**门**：`scripts/scan/chat_session_id_binding_check.py`（本支独占），新增 **criterion 14**（`+271` 行）。

### 3.1 判据（认决定点，不认写法）

入口集**从函数体派生**，不写死成员数（§8.7 第 11 条教训）：

```
RUNNER_STATE_MUTATORS = vm.sendMessage / retryFromMessage / rerunFromToolBlock /
                        compactBefore / revertCompact / runCompactNow
```

对每个成员函数体内的每个变更调用，要求三件事：

1. **位置**：该调用之前（同一函数体内）必须出现 `sessionLoaded.first`。
2. **离主线程**：那次等待必须落在某个 `withContext(Dispatchers.Default) { … }` 的块内。
3. **有界**：该块内必须有 `withTimeoutOrNull(`。

另有**账本齿**：变更调用若不再位于任何成员函数体内（被重构到顶层/被改名），直接红 —— 不是静默跳过。

真树读数（`python3` 直接调 `_runner_member_spans` / `_session_barrier_violations`）：成员 15 个，派生入口集 `prompt / retry / rerunFromToolBlock / compact / revertCompact`（6 个调用点：`compact` 有 `compactBefore` + `runCompactNow` 两条路径），`violations: []`。

### 3.2 带牙证明（selftest，5 个新臂 + 1 个 vacuous 声明）

| 臂 | 变异 | 结果 |
|---|---|---|
| MUT-B1 | 整段屏障拆掉 | RED，1 violation（点名 `retry` + `vm.retryFromMessage`） |
| MUT-B2 | 等待跑在 `Dispatchers.Unconfined`（= 调用方调度器，生产路径即 Main） | RED，1 violation（BOUNDED OFF-MAIN 判据） |
| MUT-B3 | 去掉 `withTimeoutOrNull` | RED，1 violation（同判据） |
| MUT-B4 | 屏障挪到变更调用之后 | RED，1 violation（位置判据） |
| MUT-B5 | **新增**一个无屏障的 `compact` 入口（派生集齿） | RED，1 violation（点名 `compact` + `vm.runCompactNow`） |
| vacuous | 无任何变更调用的夹具（criterion 13 那套） | GREEN（声明向后兼容） |

`--selftest` 全绿：`CHAT_SESSION_ID_BINDING_SELFTEST=PASS`（criterion 1–14 全臂在位）。真树：`chat_session_id_binding_check: all criteria satisfied`（rc=0）。

> MUT-B5 首次实现踩到一个装置自伤：用 `str.replace(尾部锚)` 时尾部模式实际上在其它嵌套位置也匹配，结果新函数插到了 `retry` **内部**（`spans` 显示 `('compact', 1775, 1856)` 落在 `retry` 的 1187–1862 里），门正确地没红——因为那个位置的 `compact` 前缀确实含屏障。改成 `rfind` 定位真正类尾后红。**这是夹具写错，不是判据盲**，记在 R3。

---

## 4 验证四层证据

### L1 门 + 夹具

- `gate_run.sh --full`（`GATE_RUN_DIR=…/gate-runs/20261022T101439Z`）：**108 道门 pass=108 fail=0 sandbox=0**（进度行 `... 108 门已跑 (pass=108 fail=0 sandbox=0)`，108 份门日志落盘）。
  - ⚠️ 该 run 的 `test_scan.py` 步骤被沙箱 ENOSYS 打断（`MISSING scripts/scan/test_scan.py` + `.gate.lock: Function not implemented`，注册表 S28 已知形态，非本支缺陷）；`summary.tsv` 因此没写出。补齐见下条。
  - 第二次 `--full` 重跑与并发会话 a3 的锁冲突（`gate_run.sh --status` 显示 `pid=4967 mode=full repo=…/a3`，`lock=busy`），未强行抢锁。
- `python3 scripts/scan/test_scan.py`（单独跑）：**`✅ ALL 336 CASES PASS`**（`probe/test_scan.log`）。
- 本门 `--selftest`：`CHAT_SESSION_ID_BINDING_SELFTEST=PASS`。
- 本门真树：`all criteria satisfied`，rc=0。

### L2 邻门

**任务书列的 7 个邻门名在真树中不存在**（属别批次命名；`ls scripts/scan | grep -i` 逐个确认 0 命中）。按语义改跑真实存在的相邻门，**11/11 rc=0**：

```
turn_tail_finalize_order            rc=0  PASS (finalize after every continue branch)
compact_decision_gate_check         rc=0  ✅ compact-decision gate ...
compact_entry_pin_migration_check   rc=0  OK (9 arms green)
compact_pin_truth_check             rc=0  ✅ compact-pin truth gate ...
stream_flow_dispatch_guard          rc=0  ✅ every blocking cold-flow body dispatched off the collector
claim_release_guard                 rc=0  ✅ no ungated release or unminted claim
discarded_return_guard              rc=0  ✅ queue-drain persistence section green
marker_boundary_check               rc=0  ✅ internalScanMarker requires a parsed exit code
loop_persist_slice_check            rc=0  PASS (persistence slices start at loopState.unpersistedBlockStart)
chat_ui_interaction_check           rc=0  CHAT-UI-INTERACTION GATE OK
session_sheet_id_binding_check      rc=0  OK (4 session-scoped call blocks ...)
```

### L3 真编译真跑（`probe/l3/`，双臂）

装置：`run.sh`（驱动）+ `gen_mutants.py`（变异生成，全部从**修复后的树**派生）+ `pre/`（`git show main:…` 的基线副本）。测试类用 `kotlinc` 编译一次，靠**假树根作 cwd** 让 `sourceFile()` 走到该臂的副本（`ChatViewModel` 无 Android-free seam，契约只能钉生产文本——与同包 `HeadlessChatCancelStoreWiringTest` 同姿态）。

**臂 1 JVM（10 个用例）**

| 臂 | 期望 | 实得 |
|---|---|---|
| POST（本支树） | 全绿 | `OK (10 tests)` |
| PRE（main 树） | 恰红（缺陷真存在） | `Tests run: 10, Failures: 6` — 含 `every mutating entry awaits the sessionLoaded latch off Main before mutating` + `a cold session settles as a structured error, never as a fake success` |
| m1 `compact` 无屏障 | 红 | 2 failures（正例 + 派生集负控） |
| m2 `rerun` 等待跑到调用方调度器 | 红 | 1 failure（正例） |
| m3 `revertCompact` 无屏障 | 红 | 1 failure（正例） |
| m4 `compact` 等待无超时 | 红 | 2 failures（正例 + 派生集负控） |

**PRE 6 红的构成**（可归因）：2 条是**正例**（屏障契约 + 错误信封），4 条是**负控**（在缺屏障的基线上，那些「变异必须让它失败」的负控失去了它们要破坏的目标，因此自身失败——这正是负控该有的行为，`runCatching { check() }.isFailure` 形态）。

**臂 2 门（同一批假树根跑 criterion 14）**

| 臂 | rc | violation |
|---|---|---|
| POST | 0 | — |
| PRE | 1 | 5（retry / rerunFromToolBlock / compact(×2 路径) / revertCompact） |
| m1 | 1 | 2（`compactBefore` + `runCompactNow`） |
| m2 | 1 | 1（`rerunFromToolBlock` 非有界离主线程） |
| m3 | 1 | 1（`revertCompact`） |
| m4 | 1 | 2（`compact` 无界） |

驱动末行：**`ALL L3 ARMS MATCH`**（`probe/l3/out.log`）。

**变异还原纪律**：全部变异只写 `probe/arm-*/` 假树，**生产文件一次都没被覆盖**；收口后 `git status --short` 为空（`probe/` 在共享 `.git/info/exclude` 里）。

### L4 CI

见下节。

---

## 5 CI 判读

- 分支：`fix/dv22-a5-headless-session-ready`
- 本地 HEAD == `git rev-parse HEAD` == `54a3531888fa87f1dd3922e6f7caf40f90566b22`
- 远端：`sh gh_sync.sh push` → OK（`[new branch] fix/dv22-a5-headless-session-ready -> fix/dv22-a5-headless-session-ready`）
- dispatch：`gh-actions-dispatch --workflow build-apk.yml --ref fix/dv22-a5-headless-session-ready` → `204; run created at 2026-10-22T10:26:13Z`
- run：`https://github.com/logicflow-GYW/RikkaMinis/actions/runs/39763081224`

**结论：attempt 1 success，8/8 判据 ALL GREEN。**

**最终 run（对应分支尖端 HEAD，即交付 SHA）**

| 项 | 读数 |
|---|---|
| run / job | `39771233041` / `119799011771`（26 步） |
| conclusion | `success`（`at 2026-10-22T11:49:53Z`） |
| **head_sha 三方** | 本地 `b71265e74bb98ffc6dba46d4f6927b7c2657e738` == `git ls-remote` == CI job `head_sha` = **三者逐字符一致** |
| 步骤 | `not_ok_steps=[]` · 唯一 `skipped_steps=['Publish to Releases']`（main 门控 ✓，分支不该发版） |
| 门头 | `[1/109] … [109/109]`，序号连续、**109 个互异门名** |
| 门汇总 | `Summary: 109 passed, 0 failed`（`grep -oE 'Summary: [0-9]+ passed, [0-9]+ failed' \| sort \| uniq -c` 共 8 条，**无任何非零 failed**） |
| 夹具 | `✅ ALL 336 CASES PASS` |
| 本门 | `✅ chat_session_id_binding_check selftest has teeth (CHAT_SESSION_ID_BINDING_SELFTEST=PASS)` · `✅ chat_session_id_binding_check real tree green`（另有单独 `all criteria satisfied` 一处） |
| 新测试 | `HeadlessChatRunnerSessionReadyBarrierTest` **10/10 逐条 PASSED**（含 7 条负控） |
| 编译错 | `Z e: ` **0** |
| 游离 FAILED | **0**（含 FAILED 但不含 PASSED 的行 = 0） |
| 日志 | `probe/ci/job-tip.log`，1,364,537 B / 16,307 行 |

**同一套源码的前三轮 run**（都 `success`，用于证明红绿与「报告回填」这一非代码动作无关，且判读可复现）：
`39763322068` / job `119772879607`（`head_sha=877d4870…`，`probe/ci/job-119772879607.log`）；
`39766531437` / job `119783259705`（`head_sha=b85beef1…`，`probe/ci/job-final.log`）；
`39769224007` / job `119792252375`（`head_sha=7f27d080…`，`probe/ci/job-final2.log`）。
`877d4870 → b71265e7` 的全部差异经 `git diff --name-only … ':(exclude)fix-reports'` 核实为 **空** —— 四轮 CI 验的是同一棵源码树（`HeadlessChatRunner.kt` / 测试 / 门三文件逐字节相同），后三轮只是把「报告回填后的新 HEAD 也要有绿的 run」补齐。

**交付源码树 = CI 在 `b71265e7` 验的那棵树的逐字节副本。** 本报告在 `b71265e7` 之后的提交只改动 `fix-reports/FIX-A5.md`，可用一条命令复核：

```sh
git diff --name-only b71265e7 HEAD -- . ':(exclude)fix-reports'   # 输出为空
```

**判读装置两处踩坑（登记在 R8）**：
1. `Summary:` 行的权威形态是 `║  Summary: 109 passed, 0 failed`（带框线前缀），首版用 `strip().startswith("Summary:")` 导致 `Summary lines=0` 的假阴性 —— 改为「收集全部含 `Summary:` 的行，断言其中一条为 109/0 且无任何非零 failed」。
2. 取数用 `python3` + 两跳（job 日志 302 到 objects 主机，二跳**不带 token**），不走裸 curl —— 本机代理偶发把 HTTPS 降级成明文导致 rc=0 空输出。

---

## 6 与工单的偏差（报告失真按真树口径写）

| 项 | 任务书 | 真树 | 处置 |
|---|---|---|---|
| 四入口行号 | `retry(:332)` / `rerunFromToolBlock(:397)` / `compact(:460)` / `revertCompact(:529)` | `332` / `406` / `481` / `580` | 按真树；`397`/`460`/`529` 是 KDoc 起始行，函数体在 `406`/`481`/`580` |
| `activeEntryId.first` 6 处 | `209/228/357/417/494/584` | 完全一致 | 无偏差 |
| `sessionLoaded.first` 1 处 | `:253` | 一致 | 无偏差 |
| 邻门清单（7 个） | `turn_stop_fallthrough_check.py` / `turn_termination_no_escape.py` / `pending_batch_stop_check.py` / `stream_state_persistence_check.py` / `compact_footer_check.py` / `compact_inflight_defer_check.py` / `chat_tuning_wire_check.py` | **7 个全部不存在**（`ls scripts/scan` 逐名 0 命中） | 改跑 11 个真实存在的语义邻门（§4 L2），全部 rc=0 |
| `ChatViewModel.retryFromMessage` 行 | `:3185-3201` | `fun` 在 `3185`，`index < 0` 在 `3199` | 一致 |
| `CompactBoundaryMarkerHolder.kt:94` 注释 | 引用正确 | 一致 | 无偏差 |

**另一处需说明的偏差（本支自加，非工单要求）**：`rerunFromToolBlock` 的 `started` 守卫。工单只要求 `retry` 不得把「没起来」当 `Completed`；真树显示 `rerunFromToolBlock` 的退化分支 `return true` 后会委托给 `retryFromMessage`，而那条路径同样可静默掉落 —— 同一假成功形态，成本≈0（同一函数内），按 COMMON §1.3 的「就在你改的那个函数里且成本≈0」处理。

---

## 7 残留 R1..Rn（每条带触发条件）

- **R1 真机行为级未验（P2）**：本支的四条判定都要真设备上的 `ChatViewModel` 才能端到端跑到。触发条件 = 下次真机窗口；触发后方案 = 用 `chat.prompt` 建会话后**立即**发 `chat.retry`（同一 RPC 连接、不等待），断言 `responseText == "session_not_loaded_in_5s"` 或 `retry_dropped_before_stream`，且**不得**出现 `Completed` + 空/上一条响应。
- **R2 `sessionLoaded` 5s 上限是硬编码（P2）**：与同文件既有的三个 5s/3s 上限同族，未抽常量。触发条件 = 出现「真机上大历史会话首次加载 > 5s 导致 retry 报 `session_not_loaded_in_5s`」的报告；触发后方案 = 抽 `SESSION_READY_TIMEOUT_MS` 并显式命名理由（硬件校准纪律）。
- **R3 夹具构造用字符串拼接（P2）**：`_RUNNER_BARRIER_MUTATIONS` 的 MUT-B5 首次用 `str.replace(尾部锚)` 把新函数插进了 `retry` 内部（见 §3.2 末）。触发条件 = 下次往这套夹具加臂时；触发后方案 = 统一改为「先 `rfind` 定位类尾再插入」的 helper，禁止裸 `replace` 做结构插入。
- **R4 `compact` / `revertCompact` 没有 `started` 类守卫（P3，刻意）**：两者都不以 `Completed` 表达「起来了」——`compact` 有 `NoOp`/`compact_skipped_no_change`，`revertCompact` 走 system-info（busy / nothing-to-revert）。触发条件 = 出现「compact 报了 Completed 但没写 marker」的报告；触发后再评估是否要对称守卫。
- **R5 `prompt` 的屏障仍是 warn-only（P3，刻意）**：`send` 的语义是「可以把消息交给 VM」，修前行为是地板（`fix/send-dispatch-race` 的既有决定）。触发条件 = 出现「dispatch 后 agent 没看到任务」的第 4 次报告（前三记在 backlog §76）；触发后方案 = 把 `prompt` 的 `sessionReady == null` 也升为结构化错误。
- **R6 门判据不覆盖 `SessionSheet` / UI 侧同族（P3）**：UI 侧入口（`ChatScreen` 的按钮）本来就在屏上、用户只有在列表已渲染后才能点，窗口不成立。触发条件 = 出现 UI 侧误点即静默失败的复现；触发后再评估。
- **R7 `seedMemberCount` 类「成员数写死」在别的门里仍存在（P3）**：本支只把 criterion 14 做成派生；全仓其它 108 道门是否还有写死成员数未普查。触发条件 = 下一次「新增成员静默不红」的实例；触发后方案 = 用 `scan_debt.py code` 扫一遍。
- **R8 CI 判读脚本的汇总行形态（P3）**：首版用 `strip().startswith("Summary:")` 匹配，实际权威形态是带框线前缀的 `║  Summary: 109 passed, 0 failed`，导致 `Summary lines=0` 假阴性（其余判据当场标出真读数，未误判为绿）。触发条件 = 下次把这段判读装置复用到别的批次时；触发后方案 = 统一改为「全量收集含 `Summary:` 的行 + 断言其中一条满足 ran/0 + 无非零 failed」，并抽成共享 `judge_ci.py`。
- **R9 本支未能拿到 `gate_run --full` 的单份 summary（P2，只影响证据形态）**：两次尝试分别遇到 ENOSYS（test_scan 步骤被打断）与并发会话 a3 持锁（`gate_run.sh --status` 显示 `pid=4967 mode=full repo=…/a3`），未抢锁。证据以「108 道门逐门 PASS 的进度行 + 单独跑 `test_scan.py` 的 `ALL 336 CASES PASS` + 分支 CI 的 `109 passed, 0 failed`」三条独立读数替代。触发条件 = 下次收口期抢到空闲窗口；触发后方案 = 补跑一次完整 `--full` 留 summary.tsv。

---

## 8 影响面（用户可见行为变化）

| 变化 | 用户可能感知 |
|---|---|
| `chat.retry` / `chat.rerunFromToolBlock` 冷会话（VM 刚建）时可能多等最多 5s | 极短；只在 RPC 驱动 + 会话刚建时发生，正常使用时 VM 早已 loaded，屏障零成本（`StateFlow.first { true }` 立即返回） |
| 同上场景不再返回 `Completed` + 旧文本，改返回 `Error` + 明确原因 | **这是改善**：调用方（调度器 / e2e / 自动化）不再被假成功误导。`ScheduledTaskRunner` 对 `status != "Running"` 一律 settle 成 `RESULT_ERROR`，行为契约没变 |
| `chat.compact` 冷会话时可能返回 `Error / session_not_loaded_in_5s`（原为 `NoOp / compact_skipped_no_change`） | 两者都是「没压缩」，但新的那个说了真话 |
| `chat.compact.revert` 冷会话时抛 `RPCException(-32000)`（原为 5s 后返回 `afterMarkerCount == beforeMarkerCount`） | 从「挂 5 秒后含糊地告诉你没变」变成「立刻告诉你 VM 还没加载」 |
| `status` 词汇表：新增 `session_not_loaded_in_5s` / `retry_dropped_before_stream` / `rerun_dropped_before_stream` | 与既有 `no_provider_resolved_in_5s` / `send_dropped_before_stream` 同族（都是 `status="Error"` + `responseText` 带原因），不新增状态词 |

# dv29-FIX-B1 · 四条重试/重跑入口的「认领壳 + 撤销守卫」（B-01，P1）

## 1 身份链

| 项 | 值 |
|---|---|
| 基线 main | `b82aa8dc`（worktree `git rev-parse --short HEAD` 一致） |
| 分支 | `fix/dv29-b1-turn-entry-claim-shell` |
| 提交序列 | `2c8cf520` 生产 4 文件 · `f0f5c30d` 新测试 · `e790af95` 门扩展 · `0609443a` 测试警告修复（← CI 绿于此） |
| 绿 sha（三方逐字符） | 本地 == `git ls-remote` == CI `head_sha` = `0609443ad85cbdcd1fee00dbba937878f46054d4` |
| CI | run `40071461073` / run #2607 / job `120805599160`，attempt 1（首跑 run `40069734261` @ `e790af95` 也绿，但日志里本支测试有 1 条可空性警告 ⇒ 修掉后重跑） |
| 改动量 | 生产 4 文件 +135/−69 · 新测试 1 文件 +239 · 门 `claim_release_guard.py` +530/−86 |
| 门/夹具计数 | **门 111 / 夹具 342 不变**（只扩既有门，`scan.sh` / `test_scan.py` 零触碰） |

## 2 缺陷与先证

### 2.1 机制链（可执行、逐环有证据）

1. 用户在 `retryLast` / `rerunFromToolBlock` / `retryFromMessage` / `switchModelAndRerun` 之后进入 **准备窗口**（DB 清理、历史重建、`systemPromptForSession()`、OAuth 刷新）。
2. 这四个入口的外层 `viewModelScope.launch(Dispatchers.IO)` **没有赋给 `sendShellJob`**（`ChatRetryOps.kt:59`、`ChatViewModel.kt:3175/3422`、`ChatModelRouting.kt:306`），而 `streamJob` 此时仍指向**上一回合**的 job（或为 null）。
3. 用户点 Stop → `cancelStream()`（`ChatViewModel.kt:4113`）只取消 `streamJob` / `compactJob` / `sendShellJob` 三个已存句柄 —— 准备窗口那支协程**不在其中**，继续跑。
4. 准备窗口跑完 → `streamJob = launch(...)`（`ChatRetryOps.kt:97`、`ChatTurnPersistence.kt:386`）照常发车，发起真实 LLM 请求；`_isStreaming` 在被 Stop 清成 false 之后又被这个回合的事件覆盖。
5. `switchModelAndRerun` 更糟：认领（`_isStreaming.value = true`）写在**launch 内部**（`:350`），即三次 DB 往返之后 —— 整个准备窗口里入口守卫读到的都是 false（第二个回合可以进来），且 Phase 1 只 `streamJob?.cancel()`，前任的 setup 壳照样活着，能在我们认领之后自己 `streamJob = launch`（一个认领两条流）。
6. 用户可见后果：Stop 之后仍然收到回答 / 计费 / 消息被"复活"；`HeadlessChatRunner.isStreaming.first{!it}` 会把这种半死回合判成"回合结束"。

### 2.2 先证清单（任务书 §2 六条，逐条结论）

| # | 结论 | 证据 |
|---|---|---|
| 1 | **7 处认领点全表成立** | 见 §4 矩阵（修前/修后）。逐条判级：`sendMessage`/`resume` 正常（同步认领 + 壳受管 + 守卫），`retryLast`/`rerunFromToolBlock`/`retryFromMessage`/`switchModelAndRerun` 缺陷（壳不受管；前三个还缺守卫），`resumeQueueAfterCancel` 正常（见 R2） |
| 2 | **成立** | `cancelStream()` = `streamJob?.cancel()` + `compactJob?.cancel()` + `sendShellJob?.cancel()` + epoch 门控的 `_isStreaming=false`。前四者取消的只是**已存句柄**；未被存的外层协程不在任何句柄里 ⇒ "外层未被取消 ⇒ 内层照常起"闭合 |
| 3 | **成立** | 修前 `ChatTurnPersistence.kt:383-386`：`val launchEpoch = streamingClaimEpoch` / `val claimToken = streamingClaimToken` 位于 `systemPromptForSession()` **之后**，再与同名字段比较 = 自比恒真。门 C-B8c 在基线树上把这条判红（`[C-B8c] …:386`） |
| 4 | **部分成立，含失真** | 修前 `provider == null` 早退在认领**之前**（无需释放，正确）；launch 内的 DB 失败 `catch` 也 `return@launch` 于认领之前。**失真 D1**：报告称 `:299` 会先置 `_isStreaming.value = false` —— 真树无此行（flag 在窗口内确实读 false，但那是被取消的前任 `streamJob` 的 finally 清的）。**失真 D2**：报告称 `:332-335` 有 `messages.isEmpty()` 早退分支 —— 该函数内不存在此分支（只有 DB 失败 catch）。修后认领提到 launch 之前 ⇒ 这些早退路径**必须**由 `finally` 兜底（已加，`ChatModelRouting.kt:380-395`） |
| 5 | **成立（且多一处盲区）** | `SHELL_SCOPE` 硬编码单文件（修前 `:244`）；`STREAM_JOB_ASSIGN_RE = \bstreamJob\s*=\s*launch\s*\(` 匹配不到 `streamJob = viewModelScope.launch(`（`ChatTurnPersistence.kt:386`，已实测：基线树上该 site 无报）。**新发现第三处**：`FUN_ANY_RE = \bfun\s+([A-Za-z_]\w*)\s*\(` 也匹配不到扩展接收者 `fun ChatViewModel.retryLast(` ⇒ 即使把 scope 改成目录派生，四个入口所在文件的所有函数体依然"不存在" |
| 6 | **成立（计数逐个数）** | `retryLast` 9 · `rerunFromToolBlock` 10 · `retryFromMessage` 8 · `switchModelAndRerun` 5 —— 与报告一致 |

### 2.3 报告失真清单

| 编号 | 报告说法 | 真树 | 影响 |
|---|---|---|---|
| D1 | `ChatModelRouting.kt:299` 先调 `_isStreaming.value = false` | 该行不存在；flag 由被取消前任的 `streamJob` finally 清 | 机制成立，手段失真 |
| D2 | `:332-335` 有 `messages.isEmpty()` 早退分支 | 无此分支（只有 DB 失败 catch + `_error` 赋值） | 早退成员表需重写 |
| D3 | `ChatViewModel.kt:3911` sendMessage **已有** `sendEpoch != streamingClaimEpoch \|\| claimToken !== streamingClaimToken` 守卫 | **没有**。sendMessage 只有"壳受管"这一层防线（`:3848` 存 `sendShellJob`，子 job 随父取消），守卫站点实际在 `:3919`（修前）且无撤销检查 | 报告据此把 sendMessage 判为"正常"，实际是**第 5 处同族缺口**（本次一并补上） |

## 3 修法（阶梯第 5 级）

**「停在第 5 级（复用代码库已有机制），因为」**：本仓的回合状态机已经把这套三层纪律跑通并写进注释（`[T-stale-finally-vs-new-claim]` / `[T-claim-token-vs-stale-finally]` / `[B-08 deep-audit-1048]`）：① 同步认领；② 准备壳存进 `sendShellJob`；③ 起流前比 epoch+token。`sendMessage`（壳 + 无守卫）与 `resume`（壳 + 守卫）就是两个现成范本。本次只是把同一形态**推广到四个入口**、把共享尾部的认领身份**参数化**，并为 `sendMessage` 补上它缺的第二层。没有新机制、没有新字段、没有新依赖、没有新超时常量。

具体改动：

1. `ChatRetryOps.kt:65` — `retryLast` 外层 launch 存进 `sendShellJob`；`:106-116` 起流前加撤销守卫（`!isActive || !_isStreaming.value || sendEpoch != … || claimToken !== …`）。
2. `ChatViewModel.kt:3177` / `:3425` — `rerunFromToolBlock` / `retryFromMessage` 外层 launch 存进 `sendShellJob`，并把调用方捕获的 `(sendEpoch, claimToken)` 传给尾部（`:3303` / `:3478`）。
3. `ChatModelRouting.kt` — Phase 1 补 `sendShellJob?.cancel()`（`:291`）；认领（token → flag → `streamEpoch++` → `streamingClaimEpoch` + 两个局部捕获）**整体提到** launch 之前（`:316-336`）；壳存进 `sendShellJob`（`:341`）；DB 失败早退与尾部调用包进 `try/finally`，无 `streamLaunched` 时按本认领身份释放（`:380-395`）。
4. `ChatTurnPersistence.kt:358-386` — `runRerunStreamTail(initialProvider, label, sendEpoch: Long, claimToken: Any)`：删掉函数内的字段重读，改为 `val launchEpoch = sendEpoch`；起流前加守卫，撤销时 `return false`（调用方 finally 释放）。
5. `ChatViewModel.kt:3918-3934` — `sendMessage` 起流前补同形守卫（第二层；第一层是受管壳 + 结构化并发）。

**没动的**：`sendMessage`/`resume` 既有 `sendShellJob` 守卫、`finally` 的 `if (!streamLaunched && epoch/token 匹配) _isStreaming = false` 释放语义、`resumeQueueAfterCancel` 的"认领后零挂起起 streamJob"形态、`cancelStream()` 已发布语义（epoch 置 -1 + token 换新）。

## 4 覆盖面

### 4.1 7 处认领点修前/修后矩阵

| # | 认领点（修前→修后行号） | 函数 | 同步认领 | 准备壳受管 | 起流前撤销守卫 | 判定 |
|---|---|---|---|---|---|---|
| 1 | `ChatRetryOps.kt:51 → :52` | `retryLast` | ✅（修前即同步） | ❌ → ✅ `:65` | ❌ → ✅ `:106` | 缺陷 → 修 |
| 2 | `ChatViewModel.kt:3167 → :3167` | `rerunFromToolBlock` | ✅ | ❌ → ✅ `:3177` | ❌ → ✅ 尾部（传参） | 缺陷 → 修 |
| 3 | `ChatViewModel.kt:3414 → :3416` | `retryFromMessage` | ✅ | ❌ → ✅ `:3425` | ❌ → ✅ 尾部（传参） | 缺陷 → 修 |
| 4 | `ChatModelRouting.kt:350 → :326` | `switchModelAndRerun` | ❌（三次 DB 挂起后）→ ✅ | ❌ → ✅ `:341` | ❌ → ✅ 尾部（传参）+ Phase 1 取消前任壳 | 缺陷 → 修 |
| 5 | `ChatViewModel.kt:3793 → :3796` | `sendMessage` | ✅ | ✅ `:3848` | ❌（报告误记已有）→ ✅ `:3918` | 缺陷（第二层缺失）→ 修 |
| 6 | `ChatViewModel.kt:4216 → :4233` | `resume` | ✅ | ✅ `:4269` | ✅ `:4281` | 正常（未动） |
| 7 | `ChatQueueInterruption.kt:515` | `resumeQueueAfterCancel` | 协程内，但前置 `if (_isStreaming.value) return@launch` 机会式复查；认领→起流之间无挂起点 | 故意不受管（它是"停之后"的续跑） | 原子块（无挂起 ⇒ 无撤销窗口） | 正常（未动，见 R2） |

### 4.2 同族普查（机械计数）

- `_isStreaming.value = true`：全仓 7 处，全在 `ui/chat/`（即上表）。
- `streamJob =`：`ui/chat/` 5 处（`ChatQueueInterruption:530` · `ChatRetryOps:103` · `ChatTurnPersistence:386` · `ChatViewModel:3927/4293`），全部经 C-B8c 检查。
- `sendShellJob = viewModelScope.launch(`：修后 6 处（新增 4），唯一未受管的是队列排水（R2）。
- 调用点：`retryLast` 9 · `rerunFromToolBlock` 10 · `retryFromMessage` 8 · `switchModelAndRerun` 5 —— 与报告一致；`runRerunStreamTail` 3 处调用点全部改成 4 参（测试 `the tail is always called with the caller's captured identity` 钉死）。
- 派生作用域实测：`ChatModelRouting.kt` · `ChatQueueInterruption.kt` · `ChatRetryOps.kt` · `ChatTurnPersistence.kt` · `ChatViewModel.kt`（5 文件，全部含认领或起流形状）。

## 5 验证四层

**L1 门（本门真树）**：`python3 scripts/scan/claim_release_guard.py .` → exit 0，`✅ Claim/release guard: no ungated release…`。selftest **35 臂全绿**（原 27 臂保持 + 新 8 臂：派生作用域扫描 split-out 文件红 / 修后绿 / 删守卫红 / 守卫自比红 / 尾部重读红 / 尾部传参绿 / 间接壳不受管红 / 间接壳受管绿）。

**L2 邻门 + 冻结夹具**：引用被改文件的 20 道邻门全 rc=0（`backup_remap_rollup_check` · `chat_session_id_binding_check` · `chat_ui_interaction_check` · `compact_decision_gate_check` · `compact_pin_truth_check` · `debug_leak_guard` · `discarded_return_guard` · `file_provider_paths_coverage_check` · `katex_ready_race_check` · `memory_gate_provider_fields_check` · `models_provider_field_check` · `open_url_broker_scheme_check` · `provider_boundary_guard` · `provider_rpc_symmetry_check` · `router_lock_check` · `session_provider_enabled_check` · `skill_restore_session_cleanup_check` · `stale_closure_guard` · `sysinfo_state_thread_check` · `thinking_wiring_guard`）。冻结的 `test_scan.py::test_claim_release` 7/7 通过（含 `claim-ok` 逃生口夹具 —— 它逼出了一个真实的 ROT 口径错误，见 §6 R5）。

**L3 真 kotlinc 2.0.21 + JUnitCore 双臂**（装置 `probe/l3/`）：
- POST（本分支树）：`OK (5 tests)`。
- PRE（基线 `b82aa8dc` 未改树，同一份 class 文件）：**5/5 失败**，每条断言各自命中不同缺陷：`tail: sendEpoch must be a parameter` / `switchModel: setup shell missing` / `ChatViewModel.kt: missing call runRerunStreamTail(…, sendEpoch, claimToken)` / `retryLast must hold its setup work in sendShellJob` / `retryLast: no sendEpoch != streamingClaimEpoch comparison`。

**L3b 门对真树双臂**：同一份门脚本跑基线树 → **8 findings**，逐条对应本支四条工单：
`[C-B8] ChatModelRouting.kt:350 switchModelAndRerun` · `[C-B8b] ChatModelRouting.kt:306` · `[C-B8b] ChatRetryOps.kt:59 retryLast` · `[C-B8b] ChatViewModel.kt:3175 rerunFromToolBlock` · `[C-B8b] ChatViewModel.kt:3422 retryFromMessage` · `[C-B8c] ChatRetryOps.kt:97` · `[C-B8c] ChatTurnPersistence.kt:386` · `[C-B8c] ChatViewModel.kt:3919 sendMessage`。本支树 → 0 findings。

**L4 CI**：run `40071461073` / run #2607 / job `120805599160`，attempt 1，`head_sha = 0609443a…`（与本地 HEAD 逐字符一致；判读日志 `probe/logs/job-120805599160.log`，1,466,466 B）：
- 步骤表 **26 步、bad=[]、唯一 skipped = `Publish to Releases`**（第 23 步，main 门控；**取自 steps API** —— skipped 步骤在日志里没有任何行）。
- `━━━ [n/111] ━━━` 门头 **111 个且唯一**（pre-build scan gate success）。
- `Summary: 111 passed, 0 failed` **恰一条**；其余 6 条步骤级 Summary 全部 `failed=0`。
- `ALL 342 CASES PASS` **恰一条**。
- 编译错 ` e: ` **0**；**本支文件编译警告 0**（首跑 `e790af95` 有 1 条 `TurnEntryClaimShellTest.kt:229` 可空性警告，已修并重跑 —— CI 绿 ≠ 无警告）。
- 新增 `TurnEntryClaimShellTest` **5/5 PASSED**（逐条在册）。
- 含 `FAILED` 的 6 行逐条核对：全是 `… PASSED` 用例名子串（游离 0）。
- 绿 sha 之后只追加 `fix-reports/` 提交 ⇒ 源码树等价：`git diff --name-only 0609443a… HEAD -- . ':(exclude)fix-reports'` 为空（见 §1 提交序列）。

## 6 残留（每条带触发条件 + 触发后方案）

| # | 残留 | 触发条件 | 触发后方案 |
|---|---|---|---|
| R1 | `CompactCancelOnStopTest::sendMessage shell and compactAll store their jobs` 的注释"only the sendMessage launch … stores the shell job"已过期（现在 6 处） | 下次触碰该测试文件 | 把注释改成"the first shell assignment"；断言（`indexOf` 取首处=rerunFromToolBlock，仍在 `awaitAutoCompactIfNeeded()` 之前）无需改，本次刻意不改既有测试 |
| R2 | 队列排水 `resumeQueueAfterCancel` 的壳故意不受管：Stop 之后再立刻 Stop（200ms 窗口内）不会停掉排水 | 出现"用户 Stop 后又被自动回答"的抱怨 | 给排水一个可继承的取消 token，或把 `delay(200)` 提到认领之前；该文件是 DISPATCH §2 只读交叉点，需主会话统一改 |
| R3 | `sendMessage` 新增的守卫是**行为变更**：claim 被撤销时该回合 abort（消息已落库、无回答、无错误） | 日志出现"send shell aborted before stream"后用户报"消息没回" | 与 `resume` 现有语义一致（同一 abort 形态）；若需保消息必答，改为"释放 claim + 标记可重试"而非静默 abort |
| R4 | 门判"函数体内提前捕获"用 `val\|var NAME =` 正则，不认跨行声明（`val sendEpoch\n = …`） | 门对合法代码红（假红，不会假绿） | 把捕获集合换成声明扫描器（复用 `collect()` 的 DECL_RE 思路） |
| R5 | C-B8b-ROT 口径：只在"有条目把 launch 存进 job 字段、却没有任何 `streamJob =`"时 ROT。树里只有认领、不建流（`claim-ok` 夹具形态）不 ROT | `streamJob` 被重命名，同时某个条目仍在存 `sendShellJob` —— 已覆盖；若连壳也改名则只剩 C-B8 报警 | 出现"两条都没报但实际改名"时，把 ROT 锚到 `_isStreaming` 家族 + 任何 `= …launch` 形状 |
| R6 | 派生作用域限定在 `ui/chat/*.kt`：回合入口若搬到别的包（headless runner / 新目录）门失明 | 新增 `ui/chat` 之外的回合入口 | scope 派生改为"全仓含 `_isStreaming.value = true` 的 .kt"（当前这么做会把 `ChatQueueInterruption` 同族以外的沙箱文件也纳入，噪声大，故收窄） |
| R7 | `_params_before` 只看 `fun` 前 800 字符；超长注解/默认参数签名会取不到参数 | 出现超长签名导致 C-B8c 假红 | 改成从 `fun` 位置正向解析签名括号 |
| R8 | 沙箱不能跑 Android 依赖测试；`sendMessage`/`resume` 的整链行为只在 CI 单测 + 真机可验 | 真机窗口 | 见 §7 |

## 7 未验证边界

- **真机行为级**（沙箱无 Android 运行时）：在重试/切模型后的**准备窗口**（DB 清理、prompt 构建）点 Stop，确认日志出现 `retryLast shell aborted before stream` / `… tail aborted before stream`，且之后**没有**新的 `streamJob ENTER` 与 LLM 请求行。反向对照：不点 Stop 时正常出流。
- **结构化并发依赖**：`sendMessage` 的撤销守卫假设"子 `streamJob` 随 `sendShellJob` 取消"；这一条已有 `[fix/compact-cancel-on-stop-1002]` KDoc 与既有测试支撑，本次未新增实证。
- **`isActive` 在扩展文件中的解析**：只由 CI 编译 + 单测证明（`ChatRetryOps.kt` 新增 `import kotlinx.coroutines.isActive`）。

## 8 附装置路径

```
probe/b8_new.py           新 C-B8 / C-B8b / C-B8c 实现（拼接源）
probe/splice1.py          门替换脚本（python 只做字节级拼接）
probe/add_arms.py         8 个新 selftest 臂与夹具（拼接源）
probe/l3/post/*.class     L3 编译产物（POST 臂）
probe/pre.out / post.out  门对真树 PRE / POST 两次输出
probe/logs/job-120805599160.log  CI 判读日志（1.47 MB）
probe/logs/job-120800390515.log  首跑日志（含那条已修的警告）
```
（`probe/` 在 worktree 私有，被 `.gitignore` 覆盖，不入库。）

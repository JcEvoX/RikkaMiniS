# dv29-FIX-A1 — Anthropic 流式工具调用的 EOF 截断计数器缺口（A-01）

## 1 身份链

| 项 | 值 |
|---|---|
| 基线 main | `b82aa8dc`（worktree `git rev-parse --short HEAD` 一致） |
| 分支 | `fix/dv29-a1-anthropic-tool-truncation` |
| 提交 1 | `ef9edc06` fix(dv29-a1): feed Anthropic EOF guard from all three tool lifecycle events（生产 2 文件 + 测试 1 + 门 1） |
| 提交 2 | `828ff4ee` fix(dv29-a1): anchor the Anthropic feed criteria on a line that survives comment stripping（只改门） |
| 最终 commit | `828ff4ee50aaf5c97915e506867781eb0b2c88d4` |
| 分支 CI | run **#2602** / id `40069749029` / job `120800435130`，head_sha `828ff4ee50aaf5c97915e506867781eb0b2c88d4`（本地 / `ls-remote` / `run.head_sha` 三方逐字符一致），event=workflow_dispatch，attempt **1**，conclusion **success** |
| 过程数据点 | run #2598 / id `40068328497` @ `ef9edc06` 亦 success（提交 1 的独立验证，非最终判据） |
| 门 / 夹具 | 门 **111 不变**（0 新建门，扩 `truncated_call_guard_check.py` 的 4 条新判据 + 4 条新 selftest 臂：17 → 21）；夹具 **342 不变**（`scan.sh` / `test_scan.py` 零触碰，夹具只断 `TRUNCATED_CALL_GUARD_SELFTEST=PASS` 标记，本支保留） |

改动规模：`git diff --stat main...HEAD` 见 §8 末尾实测输出（4 文件：2 生产 / 1 测试 / 1 门）。

## 2 缺陷与先证

### 2.1 工单机制复核（逐条「成立/不成立 + 证据」）

| # | 先证项 | 结论 | 证据 |
|---|---|---|---|
| 1 | `toolChars` 全部出现位置，递增点只有 `input_json_delta` | **成立** | 基线树 `grep -n "toolChars"` 仅 3 处：`:302` 声明 `var toolChars = 0`、`:383` 递增（在 `"input_json_delta"` → `if (partial.isNotEmpty() && currentToolId != null)` 体内）、`:497` 守卫读取。无第四处。 |
| 2 | `content_block_start(tool_use)` / `content_block_stop` / `[B11]` EOF 孤儿冲洗三处都不递增 | **成立** | `:347-357` start 分支只赋 `currentToolId/currentToolName`、清 `toolInputBuffer`、重置 `lastLoggedInputChars`、`send(ToolUseStart)`；`:397-410` stop 分支只解析 args + `send(ToolCallComplete)` + 置空；`:474-488` B11 冲洗块同样只 `AppLogger.warning` + `send(ToolCallComplete)`。三处全文无 `toolChars`。 |
| 3 | 门只 pin 守卫字面量，无「生命周期分支是否喂计数器」判据 | **成立** | `truncated_call_guard_check.py:255-258` 的全部判据是 `anthropic.count(C3_ANTHROPIC_GUARD) != 1`。**更强证据（PRE 对照臂）**：把基线树的 provider 灌进**旧门**跑，旧门 verdict = GREEN（17 臂全过）——即该缺陷在这道门存在时完全不可见，不是「判据弱」而是「没有判据」。 |
| 4 | 对照臂：`OpenAiStreamSignals` 的两个 weight 与 `Gemini.toolCallActivityChars` 已处理 | **成立** | `OpenAiStreamSignals.kt:33-36` `chatCompletionsToolCallEventWeight` / `:39-60` `responsesToolCallEventWeight`，在 `OpenAIProvider` 的 4 个生产点喂 `toolCallEventCount`（`:700` chat delta、`:829` output_item.added、`:854` function_call_arguments.delta、`:884` output_item.done），`:965` 守卫读取；`GeminiProvider.kt:770-771` `toolCallActivityChars(name, argsJson) = name.length + argsJson.length` 在 `:310` 喂 `toolChars`（整块 functionCall 下发，无分片）。两者都是「整块事件也算进度」的形状，本支照抄。 |
| 5 | `turnTruncated=false` 时策略放行，链真的闭合（空参调用被实弹执行） | **成立** | `TruncatedToolCallPolicy.kt:87` `if (!ceilingDeclared && !turnTruncated) return null`；`AgentLoopEngine.kt:1586` `turnTruncated = chunk.truncated`；`:3528` `TruncatedToolCallPolicy.rejectionReason(name, turnFinishReason, turnTruncated)`；`LLMProvider.kt` 的 `failOnSilentEmptyCompletion` 把 `ToolUseStart` / `ToolCallComplete` 记为 `sawContent = true` ⇒ 空参调用既不被「空响应」拦、也不被截断策略拦，直接进执行器。 |

判级：**确认 P1**（不升不降）。可达性三查：UI 入口 = 任意 Anthropic 或 Messages 兼容中继的多步工具回合（设置里 `providerType=anthropic` 即走该 provider）；调用点 = `ProviderFactory.kt:73-74` 唯一构造点（官方 + 自定义 basePath 两臂）；旁路 = 无（debug RPC 也走同一 provider）。触发条件 = 上游在工具块宣告后、首个非空 `input_json_delta` 前断流（中继超时 / 移动网络切换）。

### 2.2 报告失真清单

- **行号 0 失真**：报告点名的 `:302` / `:347-357` / `:375-394` / `:397-410` / `:474-488` / `:497` 与基线树逐一对上。
- **符号名 0 失真**：`toolChars`、`C3_ANTHROPIC_GUARD`、`failOnSilentEmptyCompletion`、`TruncatedToolCallPolicy.rejectionReason` 全部真实存在。
- **需要补一句判断（非失真）**：报告把「零参工具 `content_block_stop` 后断流」与「start 后无 delta 断流」并列为两条独立触发路径。实测修复后**前者已被 start feed 覆盖**（start 一定先发生且已 >0），stop feed 属纵深防御（防将来有人把 start feed 挪走、或某中继只发 stop 不发 start 的畸形流）。任务书要求「两个分支至少都喂到」，本支按任务书两个都喂，但报告不应把它记成「第二条独立缺口」——这是判级依据的细化，不是机制误判。

## 3 修法

**停在第 2 级（代码库里已有），因为**：本仓已有同形模块 —— `provider/openai/OpenAiStreamSignals.kt`（纯 Int 权重函数 + provider 侧 `+=`）与 `GeminiProvider.toolCallActivityChars` + `geminiEofTerminal`。照抄形状、不发明新抽象，也不引入任何依赖（第 3 级不需要）。

1. **新文件** `provider/anthropic/AnthropicStreamSignals.kt`（同包，provider 无需 import）：三个纯函数
   - `toolUseStartActivityChars(id, name) = maxOf(id.length, name.length, 1)` —— 地板 1 是关键：零参工具的名字/ID 可能为空，但「宣告」本身就是进度。
   - `toolInputDeltaActivityChars(partialJson) = if (isEmpty) 0 else length` —— 空串是中继 keep-alive，不算进度（与既有 `if (partial.isNotEmpty() && …)` 守卫同义，只是把判定移进纯函数）。
   - `toolUseStopActivityChars(id, name) = maxOf(id.length, name.length, 1)`。
   - 三个函数刻意不互相调用（`stop` 不委托 `start`）：门的「每个声明的权重都必须被 provider 调用」判据要求声明与调用一对一，互相调用会让其中一个从派生集合里消失。
2. **`AnthropicProvider.kt`**：三处 `+=` 分别落在 `content_block_start(tool_use)` 体内（在 `if` 花括号内、`send(ToolUseStart)` 前）、`input_json_delta` 分支（原 `toolChars += partial.length` 改为走纯函数）、`content_block_stop` 的工具完成体内（置空 `currentToolId` 之前）。
3. **必须保住的都保住**：`:517` 的 `C3_ANTHROPIC_GUARD` 字面量**逐字符未动**（`grep -c` = 1）；`sawClearFinish` 的 `message_delta` 判定未动；`message_stop` 之后未加任何路径；非 `tool_use` 的 `content_block_start`（text/thinking 空块头）**不喂任何计数器** ⇒ 完全空流仍是 `contentChars == 0 && toolChars == 0`，交给 `failOnSilentEmptyCompletion`。
4. **门扩展（`truncated_call_guard_check.py`，唯一门，0 新建门）**：新增 4 条**派生**判据
   - `anthropic-tool-lifecycle-branches`：三个协议分支必须能被按 **sibling 协议标签**切片（`"content_block_start" ->` / `"content_block_delta" ->` / `"content_block_stop" ->` / `"message_delta" ->`），不按花括号配对（分支体里有 lambda/字符串，配对会误切；且 `when` 臂重排不能让门失明）。
   - `anthropic-tool-start-feeds-counter`：start 分支内**必须在 `tool_use` 守卫的 if 体内**出现对该计数器的写（写了但在守卫外 = 红，因为那会把 text/thinking 块头也计成工具活动）。
   - `anthropic-tool-stop-feeds-counter` / `anthropic-tool-delta-feeds-counter`：stop / delta 分支必须各有写（后者是既有能力，防回归）。
   - `anthropic-tool-weights-single-source`：所有 `+=` 必须走 `AnthropicStreamSignals` 声明过的纯函数，且声明过的每个权重都必须被调用（少一个 = 某条生命周期掉了接线）。
   - **计数器名本身是派生的**：从 `C3_ANTHROPIC_GUARD` 里正则取 `X > 0` 的名字，取**在工具生命周期分支里被写过**的那一个 ⇒ 改名不失明，删掉任意一处 feed 时另外两处仍能命名计数器（删一处只红一条 feed + single-source，不会退化成「找不到名字」）。
   - 4 条新 selftest 带牙臂（期望集手写）：删 start feed / 删 stop feed / start feed 移出 `tool_use` 守卫 / delta feed 退回裸 `partial.length`。

**日志面未改动**（不新增日志、不改既有 `AppLogger.warning` 文案）——本支的观测靠下游既有的 `truncated tool call REFUSED` 行，不需要新增日志面（也避免密钥/载荷外泄面扩大）。

## 4 覆盖面（同族 / 调用点普查）

| 普查项 | 命令 | 结果 |
|---|---|---|
| Messages 协议的其它实现者 | `grep -rn "input_json_delta" src/android/app/src/main/` | **只有 AnthropicProvider 一处**（另两处命中是 Gemini 的 KDoc 引用） ⇒ 无兄弟实现漏修 |
| `toolChars` 站点分布 | `grep -rn "toolChars" src/android/app/src/main/java/com/rikkaminis/app/provider/` | AnthropicProvider **6** 处（`:307` 声明 / `:367` `:396` `:424` 三喂 / `:514` 注释 / `:517` 守卫读）、AnthropicStreamSignals 1、GeminiProvider 11、OpenAIProvider 1（`inputTokens` 无关变量） |
| provider 构造点 | `grep -rn "AnthropicProvider(" src/android/app/src/main/` | `ProviderFactory.kt:73-74` 两行（官方直连 / 自定义 basePath 各一臂）⇒ 一次修改全覆盖，无第二入口 |
| 新权重的调用点 | `grep -rn "ActivityChars" src/…/main/` | 3 处，全在 AnthropicProvider（`:367` `:396` `:424`），无未被消费的声明 |
| 门影响面 | `grep -rln "AnthropicProvider.kt" scripts/scan/` | 5 门：本门 + `anthropic_thinking_budget_check` / `debug_leak_guard` / `provider_ua_parity_check` / `tool_result_image_branch_check`，四个邻门全部 rc=0（§5 L2） |

**跨层完整性**：本条不新增字段、不改持久化、不改 IPC 序列化 ⇒ 无「四处同步」面。改的是同一函数内的判定输入集合，唯一的跨层面是「provider 产出的 `TurnTruncated` → engine 的 `turnTruncated` → policy 的拒绝」——三者都已有既有测试与门（C1/C2）。

## 5 验证四层

### L1 本门（真树 + 带牙 + PRE 对照）

```
python3 scripts/scan/truncated_call_guard_check.py .            → rc=0(✅ GREEN)
python3 scripts/scan/truncated_call_guard_check.py . --selftest → ✅ 21 arms flip exactly their criterion
                                                                  TRUNCATED_CALL_GUARD_SELFTEST=PASS
```

- **PRE1（门盲区实证）**：旧门（`git show HEAD~1:` 版本，17 臂）+ **基线树 provider** ⇒ `✅ GREEN`。即缺陷在旧门下完全不可见。
- **PRE2（新判据有牙）**：新门 + 基线树 provider ⇒ `❌ rc=1`，恰好 3 条且行号精确指向缺口：`anthropic-tool-start-feeds-counter` @ `AnthropicProvider.kt:347`、`anthropic-tool-stop-feeds-counter` @ `:397`、`anthropic-tool-weights-single-source` @ `:497`。
- **变体臂（4 条，期望集手写、非 from 运行结果）**：删 start feed ⇒ `{start-feeds, single-source}`；删 stop feed ⇒ `{stop-feeds, single-source}`；start feed 移到 `tool_use` 守卫外 ⇒ 恰 `{start-feeds}`（single-source 仍绿，因为纯函数仍被调用——这正是「分支内有写」不等于「守卫生效」的分辨力）；delta feed 退回 `toolChars += partial.length` ⇒ 恰 `{single-source}`。
- 原有 17 臂全部保持绿（含 `anthropic-sibling-pin`：守卫字面量被改写时新判据**跳过**、只由原臂报红，避免一条回归报四条噪音）。

### L2 邻门

`anthropic_thinking_budget_check` / `debug_leak_guard` / `provider_ua_parity_check` / `tool_result_image_branch_check` 在修改后的树上 **4/4 rc=0**（`GATE=GREEN anthropic_thinking_budget (5 criteria)`、`PASS provider_ua_parity_check (… Gemini EOF verdict weighs tool payload …)` 等）。

### L3 沙箱 JVM（真 kotlinc + JUnitCore）

```
/opt/kotlinc/bin/kotlinc -version → kotlinc-jvm 2.0.21 (JRE 17.0.18)
kotlinc -classpath junit-4.13.2.jar:hamcrest-core-1.3.jar -d probe/jvm/out \
    AnthropicStreamSignals.kt AnthropicStreamSignalsTest.kt   → rc=0
JUnitCore com.rikkaminis.app.provider.AnthropicStreamSignalsTest → OK (12 tests)
```

变异臂（每臂在 `probe/mut/<arm>/` 的**独立副本**上编译运行，源文件全程不动，前后 `sha256 60d34f10…` 一致）：

| 臂 | 变异 | 结果 |
|---|---|---|
| m1 | start 地板去掉（`maxOf(…,1)` → `id.length + name.length`） | **RED，8 条用例失败** |
| m2 | 空 `partial_json` 也计进度（`length + 1`） | **RED，5 条失败** |
| m3 | stop 地板去掉 | **RED，8 条失败** |
| post | 未变异 | `OK (12 tests)` |

期望值全部是手写字面量（`13` / `1` / `7` / `6` / `10` / `20` / `2`），没有一条从被测常量推导。

### L4 分支 CI

run **#2602** / `40069749029` / job `120800435130` / head_sha `828ff4ee…` / event=workflow_dispatch / attempt **1** / conclusion **success**（日志 `probe/ci/job-40069749029.log`，1,467,790 B，逐条判读）：

```
Summary: 111 passed, 0 failed      （带框线前缀；全日志唯一一条 111/0，其余 6 条步骤级
                                    Summary 分别 5/10/10/13/22/25/33 passed，failed 全 0）
ALL 342 CASES PASS                 恰 1 条
门头 [n/111]                       111 个，名称去重后仍 111（无重号/缺号）
steps                              26 步，not_ok = []；唯一 skipped = 步骤 23「Publish to Releases」= main 门控
e:  编译错 0 条                    w:  本支三文件相关警告 0 条（日志共 376 条既有警告，均非本支文件）
游离 FAILED                        0 条（行尾匹配）
AnthropicStreamSignalsTest         12/12 PASSED
门自检                             「truncated_call_guard_check selftest has teeth
                                    (TRUNCATED_CALL_GUARD_SELFTEST=PASS)」+「real tree green」两条都在册
```

三方 sha：本地 `git rev-parse HEAD` = `828ff4ee50aaf5c97915e506867781eb0b2c88d4`、`git ls-remote origin` 分支尖端、`run.head_sha` 逐字符一致。

## 6 残留（R 条目）

| # | 残留 | 触发条件 | 触发后方案 |
|---|---|---|---|
| R1 | **真机行为级未验**：无法在真机稳定构造「`content_block_start` 后断流」。本支的端到端证据是「门 + 编译 + 既有字面锚」，不含一次真实的截断重试 | 中继超时 / 移动网络切换导致真实断流；或用户报告「空参工具被实弹执行」 | 起本地可注入断流的中继（在 `content_block_start` 帧后关连接）跑一次纯工具回合，查日志出现 `Finished(truncated=true)` 与 `runAgentLoop turn=… truncated tool call REFUSED` |
| R2 | `toolChars` 语义由「字符数」扩为「活动权重」（含 id/name 长度与地板 1） | 未来若有人拿 `toolChars` 做数值比较（阈值/统计/日志比率） | 重命名为 `toolActivity` 并同步门常量 `C3_ANTHROPIC_GUARD` 与测试；当前唯一消费方是 `> 0`，语义漂移无行为影响 |
| R3 | `[B11]` EOF 孤儿冲洗块**不**喂计数器，依赖 start feed 已 ≥1 | 若将来 start feed 被移除/搬走 | 门 `anthropic-tool-start-feeds-counter` 会直接红（PRE2 已实测），届时在 B11 处补 `toolUseStopActivityChars` |
| R4 | **行为变更（预期内）**：只发 `[DONE]`/EOF 而缺 `message_delta` 的工具回合现在判 truncated（修复前判 clean）。若某中继省略 `message_delta`（协议规定它是终止事件，正常中继不会省），正常工具回合会被判截断并触发一次重试 | 用户报告「完整工具回合被重试/模型重复调用同一工具」 | 短期：查日志 `→ ToolCallComplete` 后是否紧跟 `message_delta`；中期：把「工具块已完成 + 之后只收到 `[DONE]`」记为第二类 clean 终止（需同时扩门与 C3 字面量，属独立决策） |
| R5 | stop feed 在解剖学上是冗余的（start feed 已保证 >0），保留是按任务书「两个分支至少都喂到」的纵深防御 | 若评审判定为「多余装置」 | 删除 stop feed + 对应纯函数 + 门判据与 2 条臂（一次性 4 处同步，人工核对即可） |

## 7 未验证边界

- **provider 接线无法在 JVM 复现**：SSE 循环依赖 `android.util.Log`、OkHttp `ResponseBody.byteStream()`、协程 `CoroutineScope`，沙箱内不可执行 ⇒ 「信号真的喂给了 `:517` 守卫」这半边**只由门（4 条判据 + 4 条带牙臂 + PRE2 实测）+ CI 编译 + 既有 `TruncatedCallGuardTest` 的守卫字面锚覆盖**。本报告不声称端到端验证过。
- **真机一次真实断流**：未做（R1）。
- **多工具并发块 / Anthropic 一次响应多个 tool_use 块**：门与测试覆盖的是「每个块的 start/stop 都喂」，未对「N 个块交错」做装置化验证（协议不分片交错，风险低）。
- **编译级验证**：本地无 Android SDK 全量编译（`probe` 只编了纯逻辑模块）；provider 文件的编译正确性由 L4 CI 的 `compileReleaseKotlin` 覆盖。

## 8 附装置路径

- 门自检装置：`scripts/scan/truncated_call_guard_check.py --selftest`（21 臂，自带 dirty/clean 变异，无外部夹具）
- PRE 对照树：`/tmp/pre-tree/`（8 个文件：当前树 7 个 + 基线 `AnthropicProvider.kt`；由 `git show HEAD~1:` 生成，可随时重建，未入库）
- JVM 装置：`probe/jvm/out`（编译产物）· `probe/jvm/arms.sh`（三变异臂编排：每臂独立副本 + 独立 kotlinc/java 进程）· `probe/mut/{m1-start-floor,m2-delta-keepalive,m3-stop-floor}/{src,out,junit.log}`（`probe/` 在 `.gitignore:141`，未入库）
- 改动范围（实测）：`git diff --stat main...HEAD`

```
scripts/scan/truncated_call_guard_check.py         | 226 +++++++++++++++++++-
 .../app/provider/anthropic/AnthropicProvider.kt    |  34 +++-
 .../provider/anthropic/AnthropicStreamSignals.kt   |  73 +++++++      （新增）
 .../app/provider/AnthropicStreamSignalsTest.kt     | 124 +++++++++++   （新增）
 4 files changed, 447 insertions(+), 10 deletions(-)
```

- CI 取数与判读脚本：`probe/ci/fetch.sh`（run/jobs API + 日志落盘，token 只引用不打印）· 日志 `probe/ci/job-40069749029.log`

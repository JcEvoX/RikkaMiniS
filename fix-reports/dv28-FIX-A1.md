# dv28-FIX-A1 — OpenAI 流式两条路径的一致性（A-01 EOF 截断守卫盲区 + A-08 流内错误映射）

- 支：`a1`　分支：`fix/dv28-a1-openai-toolcall-eof`
- 基线：`eea9f407f9e0e79f4902eeef1f84e1550ee5c29c`（`git log --oneline -2` 复核，HEAD 的父提交链即为它）
- 工单：**A-01（P1）+ A-08（P1）**
- 独占生产面：`provider/openai/OpenAIProvider.kt`（+ 本支新建的 `provider/openai/OpenAiStreamSignals.kt`）
- 独占门：`scripts/scan/truncated_call_guard_check.py`（`scan.sh` / `test_scan.py` 冻结面零触碰）

---

## 1. 缺陷与真树取证

### A-01 —— 「本回合产出了工具调用增量」只有 Chat Completions 一条路在喂

基线树实测（`grep -n toolCallEventCount` 全 8 处）：

| 行 | 语义 |
|---|---|
| `:867` | `var toolCallEventCount = 0`（声明） |
| `:988` | `if (tcLen > 0) toolCallEventCount += tcLen` ← **全文件唯一写点**，在 `delta != null` 的 Chat Completions 分支里 |
| `:1306` `:1407` `:1413` | 仅日志插值 |
| `:1375` | EOF 守卫 `if (!finishedSent && (contentLen > 0 \|\| reasoningLen > 0 \|\| toolCallEventCount > 0))` |

Responses 分支（`response.output_item.added` / `response.function_call_arguments.delta` /
`response.output_item.done` / `response.failed` / `response.completed`）**0 处递增**。

⇒ 纯工具调用的 Responses 回合被网络 EOF 截断时 `contentLen == reasoningLen == toolCallEventCount == 0`，
守卫读成「空回合」⇒ 不发 `Finished(truncated = true)` ⇒ 下游 `turnTruncated` 恒 false ⇒
`TruncatedToolCallPolicy` 不触发，半截 `function_call` 参数对象进执行器。
门 `C3_OPENAI_GUARD` 只逐字钉守卫形状，不判「每条产出工具调用增量的路径是否都喂了这个计数」⇒ 盲区。

### A-08 —— 流内错误码失真（两条）

- Chat Completions（基线 `:1185-1194`）：`val code = inlineError.optInt("code", 0)` 后
  `mapHttpError(code, event.toString())`。OpenAI 兼容网关的流内错误是
  `{"error":{"code":"rate_limit_exceeded"}}`（字符串）或只有 `type` 无 `code` ⇒ `optInt` 压成 0 ⇒
  `mapHttpError(0)` 落到末尾 `ProviderError`（**不可重试、不可 fallback**）。
- Responses（基线 `:1109-1114`）：`server_error` 与 `rate_limit_exceeded` 手写进同一个
  `TransientError` ⇒ 限流被当成「同模型重试」，不退避也不换路。
- 对照：`AnthropicProvider.kt:1358-1368 anthropicStreamErrorStatus` 已经把
  `rate_limit_error → 429` 等映射成状态码再走 `mapHttpError`，OpenAI 侧缺这层。

**先证补充（对任务书的两处校正）**：
1. 任务书 §1 写 `toolCallEventCount` 唯一写点在 `:988`，真树一致（`:867` 是声明）。
2. 任务书 §1 说 Chat 分支在 `:1185-1194`；真树该段实际在 `:1206` 起（基线），修法落点以真树为准。

---

## 2. 修法

**停在第 7 级（最小新增纯函数），因为**：两条需求的核心都是「一个决定被两处各自实现」——
A-01 是「这个 SSE 载荷算不算工具调用增量」，A-08 是「这个字符串错误码属于哪个 HTTP 族」。
把决定搬进新函数会多一个文件，但把决定留在 provider 里就必然有两个副本（下一个 flavour 出现时第三次复发）。
两个函数都 Android 无关、不依赖 org.json，因此能被真 kotlinc + JUnit 直接跑——
这正是 A-01 的判定在 SSE 循环里根本无法执行的部分。任务书 §3 也明确允许这条路径。

### A-01 —— 同一计数器，两条路径都喂，权重决定单源

新文件 `provider/openai/OpenAiStreamSignals.kt`（Android 无关、无 org.json）：

```kotlin
internal fun chatCompletionsToolCallEventWeight(toolCallsInDelta: Int): Int =
    if (toolCallsInDelta > 0) toolCallsInDelta else 0

internal fun responsesToolCallEventWeight(eventType: String, itemType: String?, argsDeltaLen: Int): Int = when {
    eventType == "response.output_item.added" && itemType == "function_call" -> 1   // 首次证明本回合有工具调用
    eventType == "response.output_item.done"  && itemType == "function_call" -> 1
    eventType == "response.function_call_arguments.delta" -> if (argsDeltaLen > 0) 1 else 0
    else -> 0
}
```

两个喂点（互斥的两条流式路径，都在 `rawStreamMessage` 内）：

- Responses 分支入口 `:1012`（`if (isResponsesAPI) {` 内第一段）
- Chat Completions 的 `delta.tool_calls` 处理处 `:1295`

保留变量名 `toolCallEventCount`（守卫逐字被门钉住，且它仍是「事件计数」语义，
日志 `[T321] toolCallEvents=` 读数含义不变）。**守卫条件形状一字未改**，
`[RC2-truncated-detection]` 锚点、`failOnSilentEmptyCompletion`、usage/`service_tier` 日志块全部未动。

### A-08 —— 错误码 → HTTP 族，单源

同文件新增：

```kotlin
internal fun openAiStreamErrorStatus(codeToken: String, typeToken: String): Int {
    val numeric = codeToken.trim().toIntOrNull()
    if (numeric != null && numeric in 400..599) return numeric   // 中继直接给数字码时原样保留
    openAiErrorFamilyStatus(codeToken)?.let { return it }
    openAiErrorFamilyStatus(typeToken)?.let { return it }
    return 0                                                     // 认不出 ⇒ 保持历史 ProviderError
}

internal fun openAiErrorFamilyStatus(token: String): Int?   // rate_limit*→429 / insufficient_quota→429
                                                            // authentication*|invalid_api_key→401 / permission*→403
                                                            // not_found*|model_not_found→404 / context_length*|invalid_request*|invalid_prompt→400
                                                            // server_error|api_error|overloaded_error|internal*→500
```

两处流内错误都改成 `mapHttpError(openAiStreamErrorStatus(code, type), <body>)`：

- Chat Completions：`code` 用 `optString("code","")` 读（**字符串形态被消费**），`type` 读 `type`；
  `optInt("code", 0)` 形态消失。
- Responses `response.failed`：`mapHttpError(openAiStreamErrorStatus(code, err.type), err?.toString() ?: event.toString())`
  ⇒ `rate_limit_exceeded → 429 → RateLimited`（构造器 `RateLimited(retryAfterMs = null)`，与 HTTP 429 同款），
  `server_error → 500 → TransientError`（保持既有语义）。

顺带（同一语句块、成本 0）：删掉该 arm 里既有的死变量 `val msg`（基线即无消费者，`throw` 用的是 `event.toString()`）。

---

## 3. 改动清单（基线 `eea9f407` → HEAD）

| 文件 | 类型 | +/− |
|---|---|---|
| `provider/openai/OpenAIProvider.kt` | 生产 | +43 / −10 |
| `provider/openai/OpenAiStreamSignals.kt` | 生产（新） | +105 / −0 |
| `provider/openai/OpenAiStreamSignalsTest.kt` | 测试（新，18 用例） | +222 / −0 |
| `scripts/scan/truncated_call_guard_check.py` | 门（扩既有） | +231 / −34 |
| 合计 | 4 文件 | **+601 / −44** |

门 **111 / 夹具 342 不变**（0 新建门；`scan.sh` 与 `test_scan.py` 未触碰）。

---

## 4. 门与判据（扩 `truncated_call_guard_check.py`，两条派生判据）

判据**不锚任何符号名**：计数器从被钉住的守卫里读出来，权重函数是「signals 模块声明且自己没调用的纯 Int 函数」，
状态派生函数是「被当作 `mapHttpError(` 第一个实参的那个调用」。改名不失明，删接线必红。

1. **`openai-count-covers-both-flavours`**
   - 守卫读的每个 `X > 0` 标识符中，必须存在**一个**计数变量：`X += <fn>(` 形式 ≥ 2 处（`C8_COUNTER_VISIBILITY = 2`）、
     每处 `<fn>` 都在 signals 模块里声明（单源）、且 signals 模块里声明的**每个权重函数都被喂**（缺一即「某条 flavour 掉了接线」）。
2. **`openai-stream-error-single-source`**
   - `mapHttpError(` 的第一个实参**恰好一个**是函数调用，且该函数在 signals 模块里声明；
   - 该函数体里 `429` 与 `500` 都在（限流族可达、transient 族可达）；
   - 该函数在 provider 内被引用 ≥ 2 次（两条流内错误臂都要用它）；
   - `response.failed` 臂内必须出现该函数（符号化限流码不许被手工判成 `TransientError`）。
   - `optInt("code", 0)` 的**消费形态**被上面第一条覆盖（status 实参不是调用即红），因此没有再加「字面缺席」判据——
     那会退化成拼写锁，且会禁掉「数字码合法读取」这个正确形态。

**selftest 17 臂（每条恰红自己）**：原有 13 臂重写为位置化 mutator（`load()` 现在返回 7 份文本，
按位置寻址，加文件不会让某条臂悄悄改错文件），新增 4 臂：
① 删 Responses 侧计数 ② 删 Chat 侧计数（都只红 `…-both-flavours`）③ chat 臂改回 `optInt("code", 0)` ④
`rate_limit_exceeded` 改回手写 `TransientError`（都只红 `…-single-source`）。合规树绿 = 第 17 条隐含臂。

---

## 5. 验证四层

### L1 门层

- 本门真树：`python3 scripts/scan/truncated_call_guard_check.py .` → rc=0 ✅
- `--selftest` → `TRUNCATED_CALL_GATE... 17 arms flip exactly their criterion` + `TRUNCATED_CALL_GUARD_SELFTEST=PASS` ✅
- **PRE 臂**（`git show eea9f407:.../OpenAIProvider.kt` + 当前 signals 模块，`probe/pre/`）→ rc=1，恰好两条：
  `openai-count-covers-both-flavours`（`toolCallEventCount` 喂点集合为空）与
  `openai-stream-error-single-source`（找不到任何状态派生函数）。✅（probe 树已删，见 §8）
- 全量门：`scan.sh` 实际驱动 **109** 个 `scripts/scan/*.py` + **2** 个内联门（`[2/111] i18n`、`[7/111] Native crash-field host test`）。
  本环境下 `sh scripts/scan/scan.sh` 一次性起 111 个进程会触发 PRoot 的 ENOSYS 风暴
  （`python3: Function not implemented`，93 处假红，与本次改动无关），故改用**节流驱动**
  `probe/l1/gate_run.py`（门清单与参数从 `scan.sh` 文本派生，逐门串行、失败重试），分四段跑：
  `1–28` / `29–56` / `57–84` / `85–109` → **109 passed, 0 failed**；两个内联门单独跑：
  i18n orphan=**0**、`crash_fields_host_test.sh` rc=0 ⇒ **111/0**。
- `test_scan.py`（342 夹具）：本环境跑到 `deep-audit-1018` 段（含本门两条夹具
  「selftest has teeth」+「real tree green」）**无 FAIL 输出**后被 ENOSYS 打断；
  本门两条夹具已单独以等价命令验证（`--selftest` 打印独有收尾标记 + 真树 rc=0）。
  **完整的 `ALL 342 CASES PASS` 以 L4 CI 日志为准**（CI 里同一句话是硬判据）。

### L2 邻门层（7/7 rc=0）

`provider_ua_parity_check` · `thinking_wiring_guard` · `tool_result_image_branch_check` ·
`debug_leak_guard` · `log_url_redaction_guard` · `anthropic_thinking_budget_check` ·
`modeluse_proto_single_source_check` —— 逐个 rc=0（`/tmp/l2_*.log`）。

### L3 装置层（真 kotlinc 2.0.21 + 真 JUnitCore，无切片、无桩）

`probe/l3/run.sh` 直接编译**真** `OpenAiStreamSignals.kt` + **真**测试文件
（`-classpath kotlin-stdlib:junit:hamcrest`，编译期与运行期同一 CP）。

- POST：`OK (18 tests)`，编译 0 警告 ✅
- 变异（各恰红其判据，测试名逐条核对）：
  - `m1` announcement 权重改 0 → 红 `a responses function call announcement counts as progress`
  - `m2` `rate_limit → 0` → 红 `a symbolic rate limit code maps to 429` + `the type token is consulted only when…`
  - `m3` 空 delta 也算进度 → 红 `a responses argument delta counts as progress only when it carries text`
  - `m4` 数字码分支返回 0 → 红 `a numeric code is kept verbatim`
  - `m5` 不咨询 `type` → 红 `a symbolic rate limit code maps to 429` + `the type token is consulted only when…`
- 「纯工具调用 + EOF ⇒ truncated」的 JVM 证明：
  `a tool-call-only responses turn cut by eof is judged truncated` —— 用真实权重函数喂出
  `contentLen=0 / reasoningLen=0 / toolCallEventCount=2`，对守卫自己的析取式求值为 true，
  **并断言 `OpenAIProvider.kt` 里仍是那句逐字守卫**（守卫不被抽成 helper，避免同一事实两个家）；
  反向臂 `an empty responses turn cut by eof is still not judged truncated` 保持 false（半径精确，没把空回合也判成截断）。
- 还原：变异只施加在 `probe/l3/run/src/*.kt` 的**副本**上，真树文件全程未被写（`git status` 干净）。

### L4 CI 层

run **`40050571513`** / job **`120742109664`** / **attempt 1** / `event=workflow_dispatch`
（分支 push 不触发 CI，按 COMMON §3.2 手动 dispatch）/ `head_sha = 07159efe…`（与本地 HEAD、`api .../branches` 三方逐字符一致）
→ **completed / success**，26 步，唯一 skipped = `Publish to Releases`（main 门控），`bad_steps = []`。

日志判读（`probe/ci/judge.py`，日志 1,463,122 B / 17,562 行）：

| 判据 | 实测 |
|---|---|
| `Summary: 111 passed, 0 failed` | 恰一条（其余为各测试套件自己的 `N passed, 0 failed`） |
| `ALL 342 CASES PASS` | True |
| 门头 `[N/111]` | **111** 条，去重仍 111，范围 1..111 |
| 编译错 `^ e: ` | 0 |
| 游离 `FAILED`（非 `PASSED` 行） | 0 |
| 本支 4 个文件的编译/测试警告 | **0** |
| 本门 selftest 收尾标记 | `TRUNCATED_CALL_GUARD_SELFTEST=PASS` 在册 |
| 本支 18 条新用例 | **18/18 PASSED**（含 `…cut by eof is judged truncated PASSED` 与反向臂 `…is still not judged truncated PASSED`） |

---

## 6. 残留与失真

- **失真 1（对任务书）**：A-08 的 Chat 分支行号 `:1185-1194` 与真树不符（真树 `:1206` 起，偏移 21 行）。
  修法落点以真树为准。
- **失真 2（对任务书 §4.3 的臂数）**：任务书要求「至少 4 条臂」；实际给了 4 条新臂 + 13 条既有臂重写（共 17），
  其中 C6 给了两臂（Responses 侧 / Chat 侧），因为只测一侧会漏掉「另一侧被删」这一半。
- **R1（真机/真网关欠账）**：用会返回字符串码的网关（OpenRouter 之类）在流内触发 429，
  验证确实退避/换路而不是 `ProviderError` 直接失败；以及一次纯工具调用的 Responses 回合被客户端断网，
  验证 `turnTruncated` 为 true 且执行器拒绝半截参数。**触发条件**：下一次真机窗口或用户报告「工具参数半截/payload 不完整」。
- **R2（`retryAfterMs` 语义）**：流内错误没有 header，`openAiStreamErrorStatus` 只产出状态码，
  `RateLimited(retryAfterMs = null)` ⇒ 走默认退避。若某个网关把 `Retry-After` 放进事件体，
  需要另行读取。**触发条件**：出现「限流后首次重试间隔过短」的实测记录。
- **R3（同族其它 provider）**：Gemini / Anthropic 的流内错误映射不在本条半径内（Anthropic 已有 `anthropicStreamErrorStatus`；
  Gemini 未审计）。**触发条件**：下一波审计把 Gemini 流内错误列为工单。
- **R4（门 C6 的派生面）**：判据要求「signals 模块里声明的每个权重函数都被喂」，因此**新增第三条流式 flavour 时**
  若把权重函数写进 signals 模块却忘了接线，门会红；但若新 flavour 完全绕开该模块（自己内联判断），
  门看不见。**触发条件**：仓库出现第三种 SSE 解析分支（如新的 `responses-v2`）。
- **R5（`{"error": "字符串"}` 形态）**：流内错误若把 `error` 写成**字符串**而非对象，
  `event.optJSONObject("error")` 返回 null，本修法不覆盖（基线也不覆盖），该事件会被静默跳过。
  **触发条件**：日志里出现「200 流内错误文本但会话继续到 EOF」的样本。
- **R6（`response.incomplete` 与 `response.failed` 的分工）**：本次只改 `failed`；`incomplete` 仍是 `ProviderError`。
  **触发条件**：用户在 `max_output_tokens` 截断场景里报告「不可重试但其实是可续」。
- **R7（数字码 + 字符串码并存）**：`openAiStreamErrorStatus` 优先数字码；若网关同时给
  `code: 200` 与 `type: "rate_limit_error"`，数字码不在 400..599 会被跳过、回落 `type`（已覆盖，仅记录口径）。

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 | `eea9f407f9e0e79f4902eeef1f84e1550ee5c29c` |
| 生产提交 | `a6d5cd21` |
| 测试提交 | `1d9ac1cf` |
| 门提交 | `07159efe`（= CI head_sha） |
| CI run | **`40050571513`**（attempt 1，`completed / success`） |
| CI job | **`120742109664`**（26 步，`bad_steps = []`） |
| 三方 sha | 本地 HEAD `07159efe` / `api .../branches/fix%2Fdv28-a1-openai-toolcall-eof` / `run.head_sha` **逐字符一致** |
| 源码树等价 | 报告提交后 `git diff --name-only 07159efe..HEAD -- . ':(exclude)fix-reports'` 为空（COMMON §3.6） |

CI 判据（全部要齐，`COMMON §3.5`）：`Summary: 111 passed, 0 failed` 恰一条、`ALL 342 CASES PASS`、
门头 `[1/111]..[111/111]` 互异、编译错 `Z e: ` 0、游离 FAILED 0、26 步唯一 skipped = `Publish to Releases`、
**本支 4 个文件的编译/测试警告 0**。

---

## 8. 自证成本与不确定项

- 成本：3 个代码提交 + 1 个报告提交；门改造（位置化 mutator）占 diff 的 ~40%（+231/−34），
  这部分是「加文件后臂不能改错文件」的一次性投入，与缺陷数量无关。
- 不确定项①：`test_scan.py` 未能在本环境跑完（ENOSYS 打断），342 全绿依赖 CI；若 CI 红在夹具层，
  第一嫌疑是 `load()` 新加的第七份文本（signals）——本门的 `--selftest` 已覆盖该路径。
- 不确定项②：`OpenAIProvider.kt` 本地只能做「语法/未解析引用过滤」检查（无 Android SDK），
  编译警告数只能由 CI 给；已去掉基线即存在的死变量 `msg`，避免新增「未使用」告警。
- 不确定项③：`responsesToolCallEventWeight` 对 `output_item.added` 与 `output_item.done` 各计 1、
  对每个 argument delta 计 1，因此「事件计数」在 Responses 侧是「≥ 调用数」。守卫只与 0 比较，
  日志读者须知这个口径差异（已写进函数 KDoc）。

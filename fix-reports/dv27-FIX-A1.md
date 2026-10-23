# dv27 FIX-A1 — 一条线上的两个数：请求体的数对契约，与「声明了却没人消费」的载体

> 报告文件名带波次前缀（`dv27-FIX-A1.md`）：1046 波实测 `FIX-A2.md` 撞了 dv22 的历史报告，覆盖掉了旧证据。
> 本报告随分支提交；改报告不改源码时，用
> `git diff --name-only <绿sha>..HEAD -- . ':(exclude)fix-reports'` 为空来声明源码树等价，不必再跑一轮 CI。

## 1. 缺陷与真树取证

工单 A-01 与 A-03。基线 `88ef9b27`（dv26 并集 `42af0ae6` 的父 + docs）。

### A-01 —— `max_tokens` 与 `thinking_budget` 是两个读者、两个数

`OpenAIProvider.buildRequestBody`（真树 `:1958` 写 `max_tokens`，`:1967` 调注入器）：

```
:1958  body.put("max_tokens", safeMaxTokens)          // safeMaxTokens = clampOutboundMaxTokens(maxTokens, …)
:1967  echoPolicy = injectThinkingParams(body, thinkingLevel, maxTokens)   // ← 原始请求值
```

`thinking_budget` 的语义是**相对于** `max_tokens` 的（`budget < max_tokens`，越界即确定性 400）。
两个数各自合法，**只有数对非法**——所以任何只测其中一个 helper 的测试都看不见。
越界只在 clamp 真正生效时出现，也就是 `clampOutboundMaxTokens` 自己的 KDoc 点名的
「out-of-band 调用方」（sub-agent 预算 frontmatter、合成请求）——它们恰恰是 clamp 要防的那批。

同一族的第二半在 `ThinkingRuleResolver.emit`（真树 `:729`）：

```
if (budget > 0 && ctx.maxTokens > 0) {      // ← 嵌套在 maxTokens > 0 里
    if (ctx.maxTokens < 2) { budget = 0 } else { …ceiling… }
}
```

provider 把线上值 floor 到 1（`clampOutboundMaxTokens`），所以线上 `max_tokens` **永远 ≥ 1**；
`ctx.maxTokens > 0` 这个外层条件在生产路径上恒真、于是恒真——**`< 2` 这条唯一有效的保护被一个恒真的
外层条件变成了"只在 maxTokens<=0 时才生效"**，而生产路径上那个值恰好被 clamp 抬走了。
`maxTokens <= 0` 时整个 clamp 被跳过，**原始 budget** 直接出网。

> 1046 波已经在 Anthropic 侧修过同一族（`anthropic_thinking_budget_check.py` 的 C1/C2）：
> 同一份 KDoc、同一个「相对 max_tokens 定义」的语义，只是那次只修了 Anthropic 一家。
> 这次是**第二家没被加固**——与 1046-F9 的「两份实现只有一份被加固」同族。

### A-03 —— 「声明了却没人消费」的载体，与互斥分支

1. `MessagePayloadFilter.hasWirePayload`（真树 `:38`）把 `imageParts.isNotEmpty()` 列为
   「这个 turn 还有 payload，别删」。**但整个 provider 包里没有任何一处读 `msg.imageParts`**
   （`grep -rn "imageParts" provider/` 命中点全是 `LLMProvider` 的形参 `imageParts`（请求级），
   没有一处是 `msg.imageParts`）。
   ⇒ predicate 承诺「保住这个 turn」，序列化器却不知道拿这些字节怎么办 → 无文本的 turn
   以 `content: ""` 出网。
2. OpenAI **Responses** 构建器（真树 `:3172` 与 `:3200`）：请求级图片与消息级音频分处
   **互斥的 `else if` 两个分支**。同时带两者的 turn 只走图片分支，**音频被静默丢弃**。
   Chat Completions 侧（`:2225`）一直是「图片 || 音频」合并分支——**同一 provider 的两个 flavour 形态不一致**。

### 门为什么当初看不见

- `tool_result_image_branch_check.py` 的 `PAYLOAD_MEDIA_ARMS` 只检查 predicate 自己有没有列出
  媒体 arm——**只检查「声明」，不检查「消费」**。这正是「声明了却没人消费」能存活的原因。
- 门的作用域是「处理 tool-result 的文件」，图片/音频载体不在其内。

## 2. 修法（改动阶梯）

**停在第 2 级（代码库里已有）**——两处都不需要新抽象：

- **A-01 provider 半**：`injectThinkingParams(body, thinkingLevel, safeMaxTokens)`。
  一行，把第二个读者换成第一个读者的那个值。不新增任何概念。
- **A-01 resolver 半**：把 `< 2` 这条 guard **提到** `budget > 0` 外层的**兄弟位置**，
  语义从「`maxTokens>0` 时才 clamp」变成「**任何** maxTokens 都 clamp」。
  守卫的**可达公式**（`ceiling` 的算法）逐字未动——只换了它被谁包着。
- **A-03**：新增 `LegacyMediaPlan`（纯顶层、Android 无关）作为三家序列化器共享的
  「这一轮带哪些图 / 这轮算不算媒体轮」的唯一答案，并让 Responses 的两个 `else if` 臂
  **合并为一个分支**（图片 → 音频 → 文本，与 Chat 侧同序）。

> 为什么不是第 1 级（真不需要新东西）？`hasWirePayload` 已经在 provider 顶层，
> 新增一个同层的 `LegacyMediaPlan` 是它的镜像而不是它的重复：
> 前者回答「这个 turn 还有没有东西」，后者回答「这个 turn 带哪些图」——**两个问题**。
> 而「Responses 的图片与音频分支」不是新概念，是**已有概念的分叉**，把它合回 Chat 侧已有的形状。

## 3. 改动清单

| 文件 | 性质 | 说明 |
|---|---|---|
| `provider/openai/OpenAIProvider.kt` | 生产 | A-01：注入器改喂钳制值；A-03：Chat 侧读 `msg.imageParts`、Responses 侧合并图片/音频分支 |
| `provider/thinking/ThinkingRuleResolver.kt` | 生产 | A-01：`< 2` guard 提出 `maxTokens > 0` |
| `provider/LegacyMediaPlan.kt` | 生产（新增） | A-03：共享的「图/媒体」决策 |
| `provider/anthropic/AnthropicProvider.kt` | 生产 | A-03：读 `msg.imageParts` |
| `provider/gemini/GeminiProvider.kt` | 生产 | A-03：读 `msg.imageParts` |
| `provider/thinking/ThinkingBudgetPairTest.kt` | 测试（新增） | A-01 数对契约（纯 JVM） |
| `provider/LegacyMediaPlanTest.kt` | 测试（新增） | A-03 分布（纯 JVM） |
| `scripts/scan/anthropic_thinking_budget_check.py` | 门 | 新增 C-OAI-1 / C-OAI-2 |
| `scripts/scan/tool_result_image_branch_check.py` | 门 | 新增 C-IMG-1 / C-IMG-2 |

**门数 111 / 夹具 340 不变**（扩既有门，`scan.sh` 与 `test_scan.py` **零触碰**——冻结面未动）。

## 4. 门与判据（全部派生，不锚拼写）

### `anthropic_thinking_budget_check.py`

- **C-OAI-1**：在 OpenAIProvider.kt 里，写 `max_tokens` / `max_completion_tokens` 的那个值
  必须**就是**同函数内 `injectThinkingParams(...)` 的第 3 个实参。
  派生自 `put(...)` 的实参 token 本身 ⇒ 改局部变量名不红，改回原始形参即红。
- **C-OAI-2**：`ThinkingRuleResolver.kt` 里的 `maxTokens < 2 → budget = 0` guard
  **不得**嵌在 `maxTokens > 0` 块内（按花括号深度判定）。
- 自检臂（7 条，每条恰红其判据）：正例 / 注入器拿原始值 / 完全没有注入调用 /
  两个 `max_tokens` 写点只接上一个 / guard 不嵌套 / guard 被删。

### `tool_result_image_branch_check.py`

- **C-IMG-1**：从 predicate **函数体**派生载体表（`<carrier>.isNotEmpty()` 臂），
  要求 provider 包里**写媒体 wire part 的文件**中至少有一处读 `.carrier`。
  字节量估算型读取（`LLMProvider.approxInputBytes`）**不算**——那正是老门失明的原因。
- **C-IMG-2**：**锚在两个写点上**（不锚载体名）：`input_audio` 写点若落在
  与「写图片部件」互为 `else`/`else if` 兄弟的分支里 ⇒ 红。
  合并后的形状（两个写点同块可达）才合规。
- 自检臂（4 条）：合并分支绿 / 载体无人读 → C-IMG-1 红 / 拆回互斥分支 → C-IMG-2 红 / 复原后转绿。

> 判据**都剥注释**（1046-F9 实测：自己写的 KDoc 里引用旧形状会当场变红）。
> 剥离时**保留字符串字面量**（媒体标记本身就是字面量），但另有一路「连字面量一起剥」
> 用于括号/花括号配平——两套函数分开，各司其职。

## 5. 验证四层

### L1 门层

- 本门真树：`anthropic_thinking_budget_check.py .` → **GATE=GREEN（5 criteria）**；
  `tool_result_image_branch_check.py .` → **✅ GREEN**。
- `--selftest`：thinking-budget 门 `ANTHROPIC_THINKING_BUDGET_SELFTEST=PASS`；
  image 门 19 条臂全 `[OK]`。
- **PRE 臂**（`git show 88ef9b27:` 抽出的基线树 `probe/base/`）：
  - thinking-budget 门 **RED**：C-OAI-1 红（`thinking budget derives from maxTokens but
    max_tokens writes safeMaxTokens`）+ C-OAI-2 红（`guard nests inside budget > 0 &&
    ctx.maxTokens > 0`）。
  - image 门 **RED**：C-IMG-1 红（`no media serializer … ever reads .imageParts`）
    + C-IMG-2 红（`openai/OpenAIProvider.kt:3193 writes input_audio in an else branch
    whose sibling writes an image part`）。
- 全量：`sh scripts/scan/scan.sh` → **Summary: 111 passed, 0 failed**；
  `python3 scripts/scan/test_scan.py` → **✅ ALL 340 CASES PASS**。

### L2 邻门层

按**概念**（不是任务书点名）grep 到的 14 道真实邻门，逐个 rc=0：
`provider_boundary_guard` · `provider_write_rules_check` · `thinking_wiring_guard` ·
`offload_media_b64_wire_check` · `provider_ua_parity_check` ·
`thinking_rule_editor_preserve_check` · `provider_type_registry_check` ·
`provider_models_cache_wiring_check` · `provider_import_id_roundtrip_check` ·
`provider_remember_key_check` · `provider_rpc_symmetry_check` ·
`session_provider_enabled_check` · `memory_gate_provider_fields_check` ·
`models_provider_field_check` —— **14/14 rc=0**。

### L3 装置层（真 kotlinc + 真 JUnitCore）

切片：`probe/l3/` —— `provider/thinking/*.kt` 是**真树逐字节副本**，
`ThinkingLevel` 从 `ProviderConfig.kt` **机械抽取**（`make_slice.py` 按花括号切片），
只对 Room entity 与 logger 打桩（解析器只用到它们的字段名）。

| 臂 | 结果 |
|---|---|
| POST `PairWiringTest` | **OK (1 test)** |
| PRE（同样代码，喂原始值） | **RED**：`ceiling=8192 maxTokens=32768 -> wire=8192 budget=28672`（7 个越界） |
| PRE（基线树 `probe/base` 的 resolver） | `PostWiringTest` GREEN / `PairGuardNestingTest` GREEN / `PairNonPositiveTest` **RED** |
| mut1（把 guard 重新塞回 `maxTokens > 0`） | `PairNonPositiveTest` **RED** |
| mut2（删掉 `< 2` guard） | `PairGuardNestingTest` **RED** + `PairNonPositiveTest` **RED** |
| mut3（删掉 ceiling 的 `< max` 一半） | **三臂全绿**——见下方诚实记录 |
| mut4（完全不 clamp budget） | `PostWiringTest` **RED** + `PairGuardNestingTest` **RED** |
| `PairGuardNestingTest` | **OK (3 tests)** |
| `MergeDistributionTest` | **OK (4 tests)** —— 合并后没有任何一轮丢载体；PRE 形态在同一个断言里红 |

> 诚实记录两条**装置层发现**：
> ① **mut3 三臂全绿**——删掉 ceiling 的 `< max` 一半（`minOf(a, b)` → 只留 `a`）在
>   本装置的输入域里**没有产生任何越界数对**（`maxOf(2048, max/8)` 这一项本来就 ≤ `max-1`，
>   所以那一半是冗余的）。结论：**这一半不是承重代码**，装置没抓到它不是装置的缺陷，
>   但也说明「每个变异都该红」不是可断言的前提——mut3 是一条**无牙变异**，如实登记。
> ② **provider 侧三个序列化器依赖 OkHttp / `android.util.Base64` / Room / 日志**，
> / Room / 日志，沙箱内编不出完整 provider 包（1565 个 unresolved error）。
> 因此 L3 覆盖的是**决策层**（数对契约、载体分布），序列化器本体由**门 + 真树变异**覆盖，
> 变异体在 `probe/l3/muts/`（每个变异一个目录，源文件全程不动，跑前跑后 sha256 一致）。

### L4 CI 层

**绿 run = `39997759383`**（job `120565626959`，attempt 1，head_sha `1d7ef15e`）。

- 三方 sha 逐字符一致：本地 HEAD == `git ls-remote` == `run.head_sha` = `1d7ef15e2dd4247320e10b8da2ea1e55eb106551`。
- 26 步：唯一非 success = 步骤 23 `Publish to Releases` = **skipped**（main 门控，非 main 分支必须如此）。
- 日志判读：`Summary: 111 passed, 0 failed` **恰一条**（另 6 条 Summary 是各门自己的计数，
  `10/13/22/25/33/5 passed, 0 failed`，无非零 failed）；`✅ ALL 340 CASES PASS` 恰一条；
  门头 `[1/111]..[111/111]` **111 个序号连续且门名互异**；编译错 `Z e: ` **0**；
  游离 FAILED **0**（6 条含 `FAILED` 的行逐条确认全是**用例名子串** + `PASSED`）。
- 本支文件编译警告 **0**（含本支改过的 `OpenAIToolResultImagesTest.kt`——见下）。
- 新测试逐条 PASSED：`LegacyMediaPlanTest` 4 条、`ThinkingBudgetPairTest` 4 条。

**两次红的真相（都是真错，不是 flake）**：

1. **run `39992888827`（job `120548867960`）失败** — 步骤 15 `Run unit tests (full suite)` 红，
   唯一失败用例 `OpenAIToolResultImagesTest > responses serializer wires both
   tool-result images and top-level imageParts FAILED`。
   **根因**：那是一条**源形状测试**，它钉的是旧拼写 `entries = imageParts.map`；
   合并后的分支喂的是派生列表 `legacyImages`（请求级 + 消息级）⇒ 断言失配。
   **修法**：断言改钉**派生列表**，并额外钉住该列表由两个载体构成
   （`(if (attachTopLevelImages) imageParts else emptyList()) + msg.imageParts`）——
   **源形状测试应当钉「形状」而不是「拼写」**，这次是我们自己打破了这条规矩（1046 波记过同族）。
2. **run `39994858886`（job `120555634432`）绿但不干净** — 日志里本支改过的文件带 **1 条既有警告**
   `OpenAIToolResultImagesTest.kt:266:47 Java type mismatch: inferred type is
   'kotlin.String?', but 'kotlin.String' was expected`（`System.getProperty("user.dir")` 可空）。
   该警告**在本支之前就存在**（上一次 run 的同一行也有），但既然本支动了这个文件，
   就地修掉（`?: "."`）并重跑——**CI 绿 ≠ CI 干净**（1042-A6 / 1045-FIX-8 同族）。
   最终绿 run `39997759383` 的**本支文件警告 = 0**。

## 6. 残留与负决策

- **R1（真机行为级）**：A-01 的两条路径（OpenAI 官方 / 兼容 relay 的 `thinking_budget`）
  需要真机发一次请求确认服务端不再 400。触发条件：下次真机窗口。
- **R2（`maxTokens==2` 时的 `budget==1`）**：guard 的 `maxOf(1, …)` 下限让 2 配 1——
  数对合法，但「预算 1 token」是无意义的请求。**A-01 的半径之外**（那是既有公式的语义），
  登记不修。触发条件：有人报告「budget 恒等于 1」。
- **R3（`msg.imageParts` 无生产者）**：A-03 的三个序列化器现在会读它，但**当前没有任何
  生产路径把 per-message 图片放进 `LLMMessage.imageParts`**——`hasWirePayload` 提前为它
  开了门。所以**所有活路径逐字节不变**，本次修的是「契约已经存在、实现没跟上」，
  不是「线上正在丢图」。触发条件：真出现 per-message 图片的生产者时复核接线。
- **R4（Chat 侧 `content` 文本块的位置）**：合并后 Chat 侧文本块仍在图片/音频**之后**，
  与旧形状一致（字节级不变）。触发条件：某服务端要求文本在前。
- **R5（门的作用域仍是「provider 包」）**：`C-IMG-1/2` 只看 `provider/` 包。
  若将来新增第四家 provider 而忘了接 `LegacyMediaPlan`，C-IMG-1 会因「无人读载体」而红——
  **前提是新 provider 也写媒体部件**；不写媒体的 provider 不在射程内。触发条件：新增 provider。
- **R6（`RequestSanitizers.kt` 的豁免在 PRE 树上被判 stale）**：那是 PRE 树只抽了 5 个文件
  造成的**装置假象**（豁免要求文件仍处理 tool-result），真树该门 GREEN。已在 §5 说明。
- **R7（C-OAI-2 是形状锁）**：它要求 guard **不嵌套**。一次把公式搬进 `maxTokens>0` 的重构会红，
  即使语义等价——刻意 fail-closed，要改必须改门。触发条件：改 resolver 的 guard 结构时。

## 7. 身份链

- 基线：`88ef9b2774cf14a0c983d5cb138db0e052ea7a83`
- 提交链（3 个）：
  1. `eeff57707903ef57fa559362d50d2e94f3a77096` — 生产 + 门 + 测试（CI run `39992888827` **红**：源形状测试）
  2. `2878a229ec838ea03ba984840782cf991e08430e` — 修源形状测试 + 报告（CI run `39994858886` **绿但不干净**：1 条既有警告）
  3. `1d7ef15e2dd4247320e10b8da2ea1e55eb106551` — 清警告（CI run `39997759383` **绿且干净**）
- 分支：`fix/dv27-a1-provider-wire-parity`
- CI：run `39997759383` / job `120565626959` / attempt 1 / head_sha `1d7ef15e`
  （本地 HEAD == `ls-remote` == `run.head_sha` 三方逐字符一致）
- 门数：111（0 新建，2 扩既有）· 夹具：340（不变）· 冻结面 `scan.sh`/`test_scan.py`：零触碰
- 报告：`fix-reports/dv27-FIX-A1.md`（本文件，随分支提交）

## 8. 自证成本与不确定项

- **成本**：provider 侧改 4 个文件、2 个门；L3 装置是本波最贵的一块——
  为了能真跑 `ThinkingRuleResolver`，写了一个 `make_slice.py` 做**机械抽取**（不是手抄），
  并给 Room entity / logger 打桩。**打桩是本装置唯一的不可信面**：桩与真实实现的差异
  只可能影响「解析器读字段名」这件事，不影响任何被测表达式。
- **不确定项 1**：C-IMG-2 的 `else` 链判定是**括号/花括号深度**启发式，
  对 `when` 表达式的 else 链不适用（真树该处用 `if`，故成立）。
  触发条件：provider 改用 `when` 表达媒体分支时门会失明——需改门。
- **不确定项 2**：`LegacyMediaPlan` 是新增的共享层；三家序列化器都改成走它，
  但「走了它」不等于「顺序也一致」——顺序（图片 → 音频 → 文本）目前是**每家各自写死**的，
  门只看分支互斥，不看顺序。触发条件：有人调整任一家的顺序时，三家可能分叉。
- **不确定项 3**：A-01 的真实可达性依赖「out-of-band 调用方存在且会超模型上限」。
  本仓内的 sub-agent 预算路径**本轮未逐条追**（半径之外）；可证的是
  「两个读者在 clamp 生效时不一致」这个事实本身，以及它一旦触发即 400。

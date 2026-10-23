# FIX-B3（deep-audit-1046 / S-03，P1）— Anthropic 思考预算必须基于已钳制的 max_tokens

- 分支：`fix/dv26-b3-anthropic-thinking-budget`
- 基线：main `f7aba218`（worktree `git log --oneline -1` 父提交即它）
- 停止节点：本支 CI 绿（**未合并 main**，起手权在用户）
- 任务书：`/var/minis/shared/work/deep-audit-1046/fix-tasks/FIX-B3.md`

---

## 1. 缺陷与真树取证（基线 f7aba218）

两处缺陷，同一个形状：**一个值被两个读者读了两个数**。

### 1.1 `budget_tokens` 相对 `max_tokens` 定义，但两者基数不同

`AnthropicProvider.kt`（基线行号）：

```
:573  body.put("max_tokens", clampOutboundMaxTokens(maxTokens, effectiveMaxOutputTokens(model)))
:621  val budgetTokens = thinkingBudget(maxTokens, thinkingLevel)          // 裸值
:1098 thinkingBudget：HIGH → minOf(maxTokens, 65536)；XHIGH/MAX/ULTRA → maxTokens；
      末尾 clamped = minOf(cap, maxTokens)，clamped >= maxTokens && maxTokens > 1 → maxTokens-1
```

`thinkingBudget()` 只看 `maxTokens` 这一个入参，**从不参考模型上限**；而出口的
`max_tokens` 是钳制后的值。于是 `max_tokens` 与 `budget_tokens` 分属两套基数：

- 上游 `dynamicMaxTokens()` 正常路径已在范围内 ⇒ 缺陷不可见（这也是它能活到现在的原因）；
- **带外调用者**（子代理 frontmatter 预算、合成请求 —— 正是那段 clamp 的 KDoc 自己写着
  要防的对象）把 maxTokens 配到模型上限之上时：`max_tokens=8192` 而
  `budget_tokens=65536`，Anthropic 返回确定性 400
  `thinking.budget_tokens must be less than max_tokens`。
- `LLMProvider.sendMessage/streamMessage` **不钳 maxTokens**（只钳 thinkingLevel，
  `LLMProvider.kt:96-99/141-145`）⇒ 那两个数字不会在进 provider 之前被对齐。

**关键点**：两个数单看都"对"（各自都在自己那套基数里合法），只有**配对**非法。
这就是为什么单独对 `thinkingBudget()` 写单测永远测不出来（见 §5 L3 的 PRE 臂）。

### 1.2 beta 头是两路，`thinking.type=="disabled"` 落进了 else

```
:640  body.put("thinking", {"type": "disabled"})    // 4.6+ 自适应模型「关思考」的显式表达
:1173 if (body.has("thinking")) {
:1174     val isAdaptive = optString("type") == "adaptive"
:1175     if (isAdaptive) effort-2025-11-24 else interleaved-thinking-2025-05-14
      }
```

`disabled` 不是 adaptive ⇒ 命中 else ⇒ **给一个「请别思考」的 body 带上
interleaved-thinking beta**。语义自相矛盾：对 api.anthropic.com 是噪声，对会校验
pairing 的中转是 400。

### 1.3 取证（回真树，不采信报告行号）

| 取证项 | 命令/位置 | 结果 |
|---|---|---|
| 两读者异源 | 基线树 `grep -n 'put("max_tokens"\|thinkingBudget('` | `put("max_tokens", clampOutboundMaxTokens(...))` vs `thinkingBudget(maxTokens, …)` |
| 上游不钳 maxTokens | `LLMProvider.kt:96-99` | `sendMessageClamped(..., maxTokens, ..., clampThinkingLevel(thinkingLevel))` — maxTokens 原样透传 |
| 预算不参考模型上限 | `thinkingBudget` 全函数 | 入参只有 `maxTokens`/`level` |
| disabled 走 else | `buildRequest` 基线 | 二路 if/else，无 `"disabled"` 分支 |
| 报告失真 | 任务书称 `:540` | 真树 `:532`（不修，仅登记） |

---

## 2. 修法（改动阶梯）

**停在第 6 级（能一行就一行 / 最小实现），理由**：缺陷本体是两个实参的取值来源和
一个二路分支的缺失，代码库里已有钳制函数 `clampOutboundMaxTokens` 与
`effectiveMaxOutputTokens`，平台语义（Anthropic 的 400 契约）也是既定的 ——
不需要新抽象、不需要新依赖、不需要改 `thinkingBudget()` 的算法本身。
**否决了任务书建议的「抽 `AnthropicThinkingPlan` 纯对象」**：那会把一次
"接线对齐"升级成"重构 thinking 分派"，与本支 §2.4「不要顺手重构」冲突；
纯函数需求改由 L3 的**真函数字节切片**满足（§5 L3）。

三处改动（全部落在 `buildRequestBody` / `buildRequest` 内）：

1. **同一值两读者**：新增 `val cappedMaxTokens = clampOutboundMaxTokens(...)`，
   `body.put("max_tokens", cappedMaxTokens)` 与 `thinkingBudget(cappedMaxTokens, …)`
   共用它（而不是把同一个表达式抄两遍 —— 抄两遍今天等价，明天有人改一处就又分叉）。
2. **预算守卫收紧**：`if (budgetTokens > 0 && budgetTokens < cappedMaxTokens)`。
   理由不是防御性编程，是**存在性**：`maxTokens == 1` 时不存在合法预算
   （`thinkingBudget` 的 `>= maxTokens → maxTokens-1` 收紧只在 `maxTokens > 1` 生效），
   此时写 `budget_tokens=1` 配 `max_tokens=1` 仍是 400。宁可**不写 thinking 字段**：
   老协议的默认就是"不思考"，省略是诚实降级，而不是发一个非法值换 400。
3. **beta 头三路**：`when (type) { "adaptive" → effort；"enabled" → interleaved；
   "disabled" → Unit }`。显式写出 `"disabled"` 分支（而不是靠 whitelist 自然落空），
   让"这里做过判断"在源码上可见 —— 否则下一个人读到这里会以为漏了。

### 必须保住的东西（逐条核对未动）

- `output_config.effort` 的 4.6+ 自适应路径（`:615-619`）—— 一字未改；
- `modelRejectsTemperature` 的 `temperature=1` 逻辑 —— 未改；
- OAuth / custom endpoint 的 header 分支（`isCustomEndpoint`）—— 未改；
- `clampOutboundMaxTokens` / `effectiveMaxOutputTokens` 语义 —— 未改；
- `thinkingBudget()` 的算法 —— **一行未改**（只补 KDoc 说明入参必须是线上值）。
  L3 的切片器会字节比对 live 与基线的该函数，若将来被改，装置当场停（§5 L3）。

### 不得引入的东西（逐条核对没有）

- `max_tokens` 没有改回未钳制；
- `disabled` 情形**没有**删掉 `thinking` 字段（KDoc 里 iOS ea86dd8a 的事故记录：
  自适应模型默认开思考，省略 = 用 256 个 token 全烧在 thinking 上、正文为空）。

---

## 3. 改动清单

| 类型 | 文件 | 变更 |
|---|---|---|
| 生产 | `src/android/app/src/main/java/com/rikkaminis/app/provider/anthropic/AnthropicProvider.kt` | +44 / −9（三处修复 + 注释） |
| 测试 | `src/android/app/src/test/java/com/rikkaminis/app/provider/AnthropicThinkingBudgetWireTest.kt` | 新增 185 行 / 7 用例 |
| 门 | `scripts/scan/anthropic_thinking_budget_check.py` | 新增 580 行 / 3 判据 / selftest 7 臂 |
| 报告 | `fix-reports/FIX-B3.md` | 本文件 |

**门冻结面 0 触碰**：`scripts/scan/scan.sh` 与 `scripts/scan/test_scan.py` 未改
（新门注册由主会话收口时统一做）。**门总数不变**：本支不新建已注册门，
scan.sh 仍是 109（新门未注册，见 §4 的注册建议）。

---

## 4. 门与判据

### 4.1 为什么新建而不是扩既有门

`grep -rln 'AnthropicProvider' scripts/scan/` 命中 4 道：`debug_leak_guard.py`（日志脱敏
清单里逐字列了该文件）、`provider_ua_parity_check.py`（per-provider UA 转发）、
`truncated_call_guard_check.py`（截断调用守卫）、`tool_result_image_branch_check.py`
（tool_result 图片分支）。**没有一道的主题是 thinking 预算/线路协议配对**，且没有一道
"独占"该文件（都只是顺带解析）。按 COMMON §4「优先扩既有门」的前提是
"该文件已被某道门独占扫描"，这里不成立 ⇒ 新建 `anthropic_thinking_budget_check.py`。

### 4.2 三条判据（全部派生，不锚行号/函数名）

- **C1 两读者同源**：在**写 `max_tokens` 的那个函数体**里，每个
  `thinkingBudget(...)` 的第一实参，必须与 `put("max_tokens", X)` 的 `X`
  **规范化为同一表达式串**。函数名从"含 `put(\"max_tokens\"` 的函数"派生，
  所以改名/拆分不失明。两种允许拼写都过：共用局部值、或同一个调用表达式。
- **C2 守卫双边**：`put("budget_tokens", V)` 的 `V` 必须是**标识符**（fail-closed：
  写字面量就无从守卫 ⇒ 直接红），且必须被某个**花括号 if** 包住，该条件里同时出现
  `V > 0`（或反向 `0 < V`）与 `V < 某标识符`。刻意收紧：不认"预算函数内部自己收紧"
  作为替代 —— 守卫必须**在写入点可见**（该收紧在 `maxTokens <= 1` 时并不成立，
  见 §2 第 2 条）。这条严格性登记为残留 R2。
- **C3 disabled 三分**：把 `body.has("thinking")` 翻成 beta 标志的那个函数，必须在
  `has("thinking")` 之后的区域内**读 `thinking.type`** 并**与 `"disabled"` 比较**
  （`==`/`!=` 比较或 `"disabled" ->` 的 when 分支均可，不认字面量顺序）。

### 4.3 selftest（7 臂，各只红恰好其判据）

| 臂 | 期望红 | 实测 |
|---|---|---|
| 合规树 | 无 | 无 |
| 合规树（两侧写成同一调用表达式） | 无 | 无 |
| 基线（f7aba218）形态 | C1,C2,C3 | C1,C2,C3 |
| 预算读裸值 | C1 | C1 |
| 守卫丢掉 `< max` 半边 | C2 | C2 |
| beta 退回两路（无 disabled） | C3 | C3 |
| 预算写字面量 | C2 | C2 |

另校验**变异确实落地**（`str.replace` 未命中即红，防"变异没生效却报绿"）。

> 自伤记录：首版 selftest 6 臂全红 —— `find_calls()` 里用 `text.find("(", m.end()-1)`
> 定位实参括号，而 `put("max_tokens"…)` 的正则结束在**字符串字面量末尾**，
> 于是找到了**下一个调用的** `(`（真树上表现为 `max_tokens writes 'stream'`）。
> 修法：从 `m.start()` 向后找 `(`（callee 名后紧邻的就是调用括号）。

### 4.4 建议注册行（主会话收口时统一做）

```sh
# --- 110. Anthropic 思考预算 / max_tokens 配对 ---
echo "━━━ [110/110] anthropic_thinking_budget_check ━━━"
if python3 scripts/scan/anthropic_thinking_budget_check.py "$ROOT"; then
    PASS=$((PASS + 1)); echo ""
else
    RC=1; FAIL=$((FAIL + 1)); echo ""
fi
```

- `--selftest` 指纹标记：`ANTHROPIC_THINKING_BUDGET_SELFTEST=PASS`
  （`test_scan.py` 夹具断言该行出现 + 真树 rc=0）
- CLI 形态：位置参数 `[ROOT]`（与多数门一致，`gate_run.sh` 的位置参数优先路径直接可用）

### 4.5 PRE 臂（对基线文件，不是对 HEAD）

```
$ python3 scripts/scan/anthropic_thinking_budget_check.py probe/base   # probe/base 只放 git show f7aba218: 的原文件
  [FAIL] C1 … (budget derives from `maxTokens` but max_tokens writes `clampOutboundMaxTokens(maxTokens, effectiveMaxOutputTokens(model))`)
  [FAIL] C2 … (no `budgetTokens < <max>` guard encloses the budget_tokens write)
  [FAIL] C3 … (buildRequest does not distinguish thinking.type == "disabled")
rc=1
```

三条判据在基线树上**各红一次**，说明判据抓的正是这次修的东西，不是空转。

---

## 5. 验证四层

### L1 门层

| 项 | 证据 |
|---|---|
| 本门真树 | `GATE=GREEN anthropic_thinking_budget (3 criteria)` rc=0 |
| 本门 selftest | `ANTHROPIC_THINKING_BUDGET_SELFTEST=PASS`（7 臂，见 §4.3） |
| PRE 臂 | `probe/base` 基线树 rc=1，C1/C2/C3 各红一次（§4.5） |
| 全量门 | `sh scripts/scan/scan.sh` → **`Summary: 109 passed, 0 failed`**，109 个门头、`Function not implemented` 0 行，rc=0 |
| 夹具 | `python3 scripts/scan/test_scan.py` → **`✅ ALL 336 CASES PASS`** |
| 全量（逐门器） | `gate_run.sh --full` → `ran=110 pass=110 fail=0 sandbox=0 envretry=0 verdict=GREEN`（110 = 109 已注册 + 本支新门，因未注册 scan.sh 不跑它） |

> 沙箱症状自报：本次 L1 前两次尝试整链假红（`python3: Function not implemented`
> 扩散到 `grep`/`cat`/`tail`，summary 里 54 道门报 MISSING —— 那是**文件解析层**
> 被 ENOSYS 打断，不是门红）。按 S28 纪律静置后重跑，第三次 `envretry=0` 全绿，
> 且 `Function not implemented` 计数 0。前两次的日志保留在 `probe/l1-gate-full.log`、
> `probe/l1-full.log`，如实记录未删除。

### L2 邻门层（12 道，全部 rc=0）

按**概念**（不是任务书点名的名字）`grep -rln 'AnthropicProvider|thinkingBudget|max_tokens|anthropic-beta|ThinkingLevel|clampOutbound' scripts/scan/`：

`thinking_wiring_guard` · `thinking_rule_editor_preserve_check` · `provider_ua_parity_check` ·
`truncated_call_guard_check` · `tool_result_image_branch_check` · `debug_leak_guard` ·
`model_entry_override_parity_check` · `provider_rpc_symmetry_check` · `provider_remember_key_check` ·
`openminis_compat_field_parity_check` · `models_provider_field_check` · `provider_import_id_roundtrip_check`

12/12 rc=0（无一道因本次改动变红；改的是请求体字段取值，没有任何门钉它的字面形态）。

**引用面普查**：`grep -rln 'thinkingBudget' scripts/scan/` = 仅本门（1/110）⇒ 无第二处需要同步的判据。

### L3 装置层（真 kotlinc + JUnit，5 臂）

沙箱编不了 `AnthropicProvider`（Android + OkHttp + 整个 provider 面），所以按 COMMON §5
的降级路径做**两层装置**：

1. `thinkingBudget` 本身 —— **字节切片**（`probe/l3/slice.py`，括号/字符串/注释感知），
   不是转写；切片后断言仍含 `ThinkingLevel.OFF`/`XHIGH`/`minOf(`，并**逐字节比对 live 与
   基线两份切片**（本次相同 ⇒ 修复没碰该函数；将来被碰则装置当场停）。
2. 接线决策 —— `probe/l3/Wire.kt` 的 `plan()`/`betaFor()`，PRE/POST 两个变体；
   由 `WireBridgeTest` 从**真实源码文本**重新推导两个读者，断言 live 树两者同源、
   基线树两者异源 —— 这样"探针模型代表真实接线"有硬证据，不是自说自话。

| 臂 | 期望红 | 实测（`sh probe/l3/run.sh`） |
|---|---|---|
| POST（修复后形态） | 无 | `failed=none` |
| PRE（基线形态） | everyPairIsLegal / noThinkingWhenNoLegalBudget / betaIsThreeWay / thinkingSurvivesWhenALegalBudgetExists | 4 条全中 |
| m1 预算读裸值（守卫保留 `< capped`） | thinkingSurvivesWhenALegalBudgetExists | 恰 1 条 |
| m2 守卫丢 `< capped` 半边 | everyPairIsLegal / noThinkingWhenNoLegalBudget | 恰 2 条（同一判据的两面：配对非法 + 退化上限也照发） |
| m3 disabled 仍发 interleaved | betaIsThreeWay | 恰 1 条 |

工具链：`kotlinc-jvm 2.0.21 (JRE 17)`，`-classpath junit-4.13.2:hamcrest-core-1.3`。
**m1 的意义**：它证明"只用裸值算但保留 `< capped` 过滤"是另一种失败（预算被过滤掉 ⇒
思考静默消失），所以修复必须**两件事一起做**（换基数 + 保留守卫），只做一半都不算修好。

> 自伤记录（三处，均已闭环）：
> ① `prep.py` 的 `MUTATIONS` 元组被摊平成一个字段，`for label, mutation, switch_variant, exp`
> 把它们错位解包 —— 结果**三个变异臂全变成了 PRE 臂**（三臂红集完全相同）。
> 是"臂红了"这个事实先暴露了它：**不同变异给出完全相同的红集 = 驱动坏了**，不是代码坏了。
> ② PRE 臂按注释里的 `Variant.POST` 字样替换，把**文档注释**改了而赋值行没改 ⇒ 假绿。
> 改法：锚定赋值行 `private val V = Variant.POST`。
> ③ 首次切片器对**散文撇号**（`HIGH's min(...)`）当成字符字面量开头，吞掉函数收尾的
> `}` ⇒ 切片越界到文件末尾（195 行）。改法：`is_char_literal()` 要求闭合引号在 8 字符内。

### L4 CI 层

见 §7 身份链（`run` / `job` / `head_sha`）与 §5.5 判读。

---

## 6. 残留与负决策

| # | 残留 | 触发条件（何时复活） |
|---|---|---|
| R1 | **真机行为级未验**：带外 caller 真的把 maxTokens 配到上限之上时，线上不再 400；`disabled` 情形中转不再 400 | 下次真机窗口：用一条 `max_tokens` 超模型上限的子代理/合成请求打 Anthropic 直连与一个兼容中转；或在 modelservice 日志里看到 `budget_tokens must be less than max_tokens` |
| R2 | **C2 刻意严格**：要求守卫是花括号 `if` 且**在写入点**可见；不认"预算函数内部已收紧"作替代 | 有人把守卫改成表达式体（`if (v > 0 && v < m) body.put(...)` 无花括号）或把守卫搬进 helper —— 那时门会红，属**刻意编辑**而非静默放过 |
| R3 | **C1 接受"同一调用表达式"拼写**（任务书 §4 明确允许） | 若将来判定"必须共用局部值"，把 C1 的 `norm(arg) == norm(max_expr)` 改为"必须是标识符且该标识符有 `val` 声明" |
| R4 | `thinkingBudget()` 的 `>= maxTokens && maxTokens > 1` 内部收紧**保留**（与调用点守卫冗余） | 只有当调用点守卫被移除或改写时才成为唯一防线；届时 C2 会先红 |
| R5 | **门未注册**（`scan.sh` / `test_scan.py` 冻结面）⇒ 分支 CI 不跑本门 | 主会话收口阶段按 §4.4 注册行 + 指纹标记注册；注册后 `test_scan.py` 夹具数 336 → 337 |
| R6 | **自适应模型 + `disabled` 的 beta 现在为空**：若有中转其实**要求** interleaved beta 才接受 `disabled` | 收到这类中转的 400（错误信息指向 beta/thinking）时，把 C3 改成"三路 + 中转白名单"，并在本门加一条白名单臂 |
| R7 | 报告失真仅登记不修：任务书称 `:540`，真树 `:532` | 无（纯行号漂移） |
| R8 | `cappedMaxTokens` 只在本函数内可见；`streamMessageClamped` 与 `sendMessageClamped` 两条路径共用同一 `buildRequestBody` ⇒ 无需分别改 | 若将来拆出第二个请求体构造器，C1 的"含 `put(\"max_tokens\"` 的函数"派生会自动覆盖它（这是判据按结构派生的收益） |

**负决策（刻意不做）**：
1. **不抽 `AnthropicThinkingPlan` 纯对象**（任务书 §5 的建议）——理由见 §2；纯函数需求
   由字节切片满足。
2. **不改 `LLMProvider.sendMessage` 去钳 maxTokens**（真正的上游统一钳制）——那是跨
   provider 的公共面，本支只做 Anthropic 这一层；且 `clampOutboundMaxTokens` 的 KDoc
   明确写了它就是给带外 caller 兜底的最后一层，改成上游钳制会让这层"看上去冗余"而被删。
3. **不删 `thinkingBudget()` 内部收紧**——见 R4。

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f7aba218`（merge: docs/dv25-fix-reports） |
| 生产提交 | `8e3d361d` |
| 测试提交 | `22cb622a` |
| 门提交 `c2c86207` | **= CI 验过的树**（`head_sha` 三方逐字符一致） |
| 报告提交 | 见分支 HEAD（只改 `fix-reports/`，不在 workflow paths 内，不触发新一轮 CI） |
| CI run | `39947709325`（#2559，attempt 1，event=workflow_dispatch） |
| CI job | `120394331754`（`build`，attempt 1，`success`） |
| CI head_sha | `c2c86207c85aa8c4ceefa79030452aa02c74da5f` |
| 源码树等价 | `git diff --name-only c2c86207 HEAD -- . ':(exclude)fix-reports'` 输出为空 ⇒ CI 结论对报告提交后的代码树成立 |

### L4 判读（25 项步骤：23 跑 + 1 skipped + Post 步骤）

- `not_ok_steps=[]`（步骤级全部 `success`）；唯一 `skipped` = 步骤 23 `Publish to Releases`
  （**来自 steps API JSON，不是从日志文本猜** —— main 门控，符合预期）
- 日志 1,434,625 B；`https://github.com/logicflow-GYW/RikkaMinis/actions/runs/39947709325`
- `Summary: 109 passed, 0 failed` **恰一条**（另有若干门自带的 5/10/13/16/22/25 条，无一条 failed≠0）
- `✅ ALL 336 CASES PASS` 恰一条
- 门头 `[1/109] … [109/109]` 序号连续、109 个门名互异
- 编译错 `e:` / ` Z e: ` **0**；`w:` 共 379 条但**没有一条**指向 `AnthropicProvider.kt`
  或本支新增测试类（即本支零新增警告 —— 避免"CI 绿但带警告"那种隐性欠账）
- 含 `FAILED` 的 6 行逐条确认**全是 PASSED 用例名子串**（`slot denied produces FAILED…`、
  `reason PERSISTENCE_FAILED…` 等），无游离 `FAILED`
- 新增测试类 `AnthropicThinkingBudgetWireTest` **7 条用例逐条 PASSED**（7/7 在册）
- 本支新增门**不在** CI 日志里（未注册）——按 COMMON §3 属预期，由 §5 L1 的
  「直接跑门 + selftest + PRE 臂」补足

---

## 8. 自证成本与不确定项

**成本**：3 次提交、1 轮 CI（`PENDING_RUN_ID`）；本地重活 = 2 次失败的全量尝试（ENOSYS）
+ 1 次成功的 `scan.sh` + 1 次 `gate_run --full` + 5 臂 kotlinc（约 5×25 s）+ 12 道邻门直跑。
门 580 行 / 测试 185 行 / 生产 44 行 —— 装置/生产比 ≈ 17:1，偏高，理由是**配对类缺陷
无法用单点单测覆盖**（两个数各自合法），必须有一层"把两个读者一起看"的装置；
门与 L3 探针是同一件事的两种便宜实现，任一条都能独立抓到本次缺陷。

**不确定项**：
1. **真机未验（R1）**：这是"该 400 是否真的消失"的最终证据，本支只到接线层。
2. **中转行为未知（R6）**：`disabled` 不带 beta 是"语义上正确"，但第三方中转是否
   恰好依赖该 beta 无法在沙箱证明。
3. **C2 的形状依赖**：门要求花括号 if（R2），是刻意 fail-closed；如果团队后来偏好
   表达式体守卫，门需要一并改（这是"判据锚形状"的已知代价，登记在案）。
4. **`max_tokens==1` 的现实性**：该分支是构造出来的退化边界（真实模型上限不会只有 1），
   但它是"守卫必须双边"的**最短证明**——只用 `> 0` 时它会漏，L3 的 m2 臂正是靠它红的。

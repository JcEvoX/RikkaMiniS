# FIX-8 — deep-audit-1043：B-02（P1）`ThinkingRulesSection` 组合期读 Room · B-06（P1）组成员解析多副本 · B-07（P1）`ChatViewModel` 四个模型读取入口漏富化

**分支**：`fix/dv23-b2-capability-single-source`（基线 main `79ef6d59`）
**提交**：`d8dca1e0`（B-02）· `84ab8bb6`（B-06）· `90381f3c`（B-07）
**规模**：12 文件 **+1117 / −101**（生产 6 · 测试 3 · 门 2 · 表面表 1）
**门**：109 道不变（0 新建，两道既有门扩展：`provider_remember_key_check.py` C34 + `models_provider_field_check.py` Arm 6）
**夹具**：336 例不变
**合并**：未合并 main（含 .kt 验证面，起手权在用户）

---

## 1 缺陷与触发路径

### B-02 — 组合期同步读 Room

`ThinkingRulesSection` 在 `@Composable` 函数体内同步读 `ProviderRepository`（Room），并在同一个组合帧里把读回值当作 remember key 派生 UI 状态。后果两条：

1. **主线程 Room 查询**：每次重组都触发一次数据库读，首帧必读；在设置页快速切换 Provider 实例时读放大。
2. **键错误 → 状态陈旧**：派生键在数据尚未加载完成时算出一个"空"值，数据到达后键不变，于是 `remember` 不重算 —— 用户在弹窗里改的思考等级在下一次进入时不显示。

门原先的判据只检查"有没有读"，没有检查"读发生在哪个线程"，所以**这个缺陷是绿的**。

### B-06 — 同一个组员解析写四遍

`ModelGroup.memberEntryIds` 存 id；每个读者都要回答"这个 id 是哪个条目"。修复前：

- `ModelGroupsScreen.groupTopModalities`（组卡片的能力徽标）
- `ModelGroupsScreen` `GroupRow` 的成员预览
- `AgentLoopModelsScreen`（组内模型列表）
- `AgentLoopAddSheets`（添加成员时判重）

四处各自写 `config.modelEntries.find { it.id == id }`。默认 Voice 输入组**只含 System 哨兵**（`__builtin_system_speech__/system-asr-*`，这就是零配置可用的原因），这些 id 永不在 `modelEntries` 里 ⇒ 四处在下面各自失明：组卡片无徽标、成员预览空白、"0 models" 红色计数，而组详情页（走另一条路径）显示正常 —— 同一份数据两种回答。

### B-07 — 四个读取器漏接富化，且两个只读静态快照

`LLMModel.allModels` 的 39 个内置模型里只有 6 个 Anthropic 模型自带 `contextWindow` / `maxOutputTokens`，其余 **33 个依赖 models.dev 富化**。`ChatViewModel` 里四个读取器：

| 读取器 | 修复前 | 后果 |
|---|---|---|
| `currentModelSupportsImages` | 只读快照 `currentModel?.inputModalities`，且不归一 | 纯文本模型（`o3`、`grok-3-mini`）因 `null` 落到 `?: true` → 加图片不提示不支持；声明 `image_input` 的模型被 `equals("image")` 判为不支持 |
| `effectiveContextWindowTokens()` | `find { … }?.model`（不出富化），不折 overrides | 33 个内置模型走 ID 启发式 → 被判 `HEURISTIC` |
| `currentModelContextWindowSource` | 同上 | `TokenUsageSheet` 弹红色"上下文窗口只是猜测"警告 |
| `currentModelMaxOutputTokens` | 只读快照，连 `_activeEntryId` 都不查 | 「最大输出 Token」整行消失；改覆盖值后不回显 |

`currentModel` 快照由 `updateCurrentModel` 写入 `newEntry.model` —— **条目基座模型**：无 models.dev 富化、无 `ModelOverrides` 折叠。这是"读静态快照"形状（背景包形状 22）在 ViewModel 侧的直系。

附加后果：Group-Priority 分支按 `contextWindowSource == HEURISTIC` 判定，误判会让用户组限额**绕过** `minOf(window, groupLimit)` 的物理上限钳制。

---

## 2 修法（阶梯停在哪一级）

### B-02 — 第 7 级（最小实现）

`produceState(initialValue = …)` 在 `Dispatchers.IO` 上读，加 `rulesLoaded` 标志：未加载完成时派生状态不参与决策。四处写回调与弹窗保存回调改 `withContext(Dispatchers.IO)`；两个弹窗的"关闭后重建"判定加 `rulesLoaded` 守卫；状态持有者在实例 id 变化时重置。

顺带修一处智能转换：`sampleModelId` 是委托 `var`，`!= null` 后无法智能转换 ⇒ 先读入局部 `val sampleModelForPreview`（与 `kotlin-compile-gotchas` 记录的形态一致）。

### B-06 — 第 7 级

`SystemVoiceEntries` 新增三个单源：`isKnownGroupMemberId`（id 是否合法）、`resolveIn(persistedEntries, memberId)`（解析条目，System 哨兵 fallback 到合成条目）、`isEnabledGroupMember(persistedEntries, enabledInstanceIds, memberId)`（当前可用性）。四处改走单源。

**一个刻意的例外（写进 KDoc）**：`ProviderRepository.enabledMemberEntries` **不**改走 System 源。它只有两个调用方（`ChatSessionLifecycle:1173`、`ChatModelRouting:418`），都在聊天路径上，立即要 `instance()` + `loadApiKey()`；System 条目永远给不出这两样。若让它们经 System 源解析，不可实例化的条目会进入聊天选组、破坏混合语音组 ⇒ 那是把"UI 显示"的修复变成"聊天发不出去"的回归。因此改为显式排除 + 文档，而不是统一解析。

### B-07 — 第 7 级

```kotlin
private fun liveEffectiveModel(): LLMModel? =
    _activeEntryId.value
        ?.let { id -> providerRepository.config.value.modelEntries.find { it.id == id } }
        ?.let { ModelsDevApi.effectiveModel(it) }          // 富化 + 折 overrides
        ?: currentModel?.let { ModelsDevApi.enrichModel(it) }   // 组解析轮次：快照也富化
```

四个读取器全部经由它；`currentModelSupportsImages` 另加 `.normalizeModalities()`（把 `image_input` 归一成 `image`，与语音侧同一套约定）。

**保住的既有决策**：
- Group-Priority 策略一字未动（仍是"猜出来的窗口 + 用户显式组限额 ⇒ 用组限额"）；
- 未知模型 `?: true` 的宽松默认保留（`inputModalities` 富化后仍为 `null` 时不阻断附件）；
- `LLMModel.contextWindowSource` 纯属性定义未动；
- `ModelEntryDetailScreen` 的保存差分基线未动（不把富化值写回，`UiModelCapabilitySourceTest` 的保存半径守卫仍绿）。

**半径纪律**：`currentModelSupportsReasoning`（同族但工单未列）**未改** —— 它读快照是有意的（reasoning 支持由条目 override 优先，另有 `currentModelMaxThinkingLevel` 走条目路径）。我在初版里误把它接进单源，已撤回，避免超出触发路径。

---

## 3 门扩展（判据 + 带牙证明）

### C34 — `provider_remember_key_check.py`：组员解析必须咨询单源

- 判据形态：对三个表面文件（`ModelGroupsScreen.kt`、`AgentLoopModelsScreen.kt`、`AgentLoopAddSheets.kt`）派生"组员解析站点"（`for (x in group.memberEntryIds)` 与流式链两种形态，跨行匹配），每个站点体内必须出现 System 单源调用；**纯聚合站点**（`.minByOrNull` 之类数值归约，不解析身份）显式排除，并且"有读取但零决议站点"判**通过**（不是 fail-closed）——否则合法的纯聚合形态会被误红。
- 真树：`[C34] PASS` ×3（2 / 1 / 1 个站点，聚合排除 2 / 0 / 1）。
- 带牙 4 臂：非单源红 · 缺读取红 · 纯聚合绿 · 修前形态红。期望集手写自规则表，不从运行结果推导。

### C11a/C11b/C12 — `provider_remember_key_check.py`：组合线程判定

重写 `check_thinking_rules`，判据从"有没有读"改成"派生状态作键 + **非组合线程**生产"：`derived_state_calls` 派生调用点、`_replace_exactly` 做精确替换。夹具更新为修复后形态，新增 **3 个 C12 带牙臂**（组合期直接读 / 缺 `rulesLoaded` 守卫 / 写回调留在主线程）。

**基线对照**：三个目标文件在**修复前**全部红、修复后全部绿（同一判据，正反两向都验过）。

### Arm 6 — `models_provider_field_check.py`：四个读取器共用一个活解析出口

判据全部锚**决定点**，不锚拼写：

1. `private fun liveEffectiveModel(): LLMModel?` 必须存在；
2. 其函数体内必须有 `ModelsDevApi.effectiveModel(`（真富化）；
3. 且必须有 `ModelsDevApi.enrichModel(`（快照回退也富化）；
4. 四个读取器（`currentModelSupportsImages` / `effectiveContextWindowTokens` / `currentModelContextWindowSource` / `currentModelMaxOutputTokens`）各自的声明块内必须有 `liveEffectiveModel()`；块提取按"声明到闭括号或下一个成员声明/空行"，两种写法（`get() =` 单行 / `{ }` 函数体）都接受；
5. 视觉谓词块内必须有 `normalizeModalities()`；
6. 禁止 `modelEntries.find { it.id == id }?.model`（重复的裸解析）；
7. 禁止 `currentModel?.maxOutputTokens` / `currentModel?.inputModalities`（裸快照读）。

**带牙 6 臂**（每臂只红自己的判据）：pre-fix 全形态 9 红 · 缺 enrichModel 回退 1 红 · 单个读取器回快照 2 红 · 视觉不归一 1 红 · 重引裸解析 1 红 · 删掉一个读取器 1 红（ROT，不是静默通过）。

真树：`OK (6 arms clean)`。`--selftest` → `MODELS_PROVIDER_FIELD_SELFTEST=PASS`。

**一个自伤**（记进 §7 R6）：`_decl_block` 初版只按"下一行是成员声明/注释"断块，单表达式 getter 会把**下一个成员**吞进来、继承它的 `liveEffectiveModel()` ⇒ 假阴性。补"空行也断块"后归零。

---

## 4 验证四层证据

| 层 | 内容 | 结果 |
|---|---|---|
| L1 | `sh scripts/scan/scan.sh`（全量门） | **`Summary: 109 passed, 0 failed`**，rc=0 |
| L1 | `python3 scripts/scan/test_scan.py`（夹具） | **`ALL 336 CASES PASS`**，rc=0 |
| L1 | `provider_remember_key_check.py --selftest` | `PROVIDER_REMEMBER_SELFTEST=PASS`（含 C34 4 臂 + C12 3 臂） |
| L1 | `models_provider_field_check.py --selftest` | `MODELS_PROVIDER_FIELD_SELFTEST=PASS`（含 Arm 6 6 臂） |
| L2 | 两道门真树 | C34 `PASS` ×3；Arm 6 `OK (6 arms clean)` |
| L3a | **B-02 真 kotlinc + JUnitCore 双臂**（`probe/l3/run.sh`，PRE/POST 换盘） | **PRE 6 红（2 条对照保持绿）/ POST 8 绿** |
| L3b | **B-06 JVM 臂**（`probe/l3/run-b06.sh`：真实生产类 `ProviderConfig.kt` / `LLMModel.kt` / `SystemVoiceEntries.kt` / `VoiceModality.kt` / `ProviderCredentialMeta.kt` + 三个注解桩，单次编译） | **`OK (10 tests)`** |
| L3c | B-02 编译器形状验证（`probe/l3/shape/`） | `Broken.kt`（委托 var 直接 `!= null`）rc=1 报 `smart cast … is impossible`；`Fixed.kt` rc=0 |
| L4 | 分支 CI | 见 §5 |

**B-06 的 10 条用例**（全部字面量期望，不从被测常量推导）：持久条目解析到自身 · System 哨兵在 `modelEntries` 为空时仍能解析成三个已知显示名 · 未知 id 解析为 `null` · 持久条目覆盖同名哨兵 · **零启用实例下三个哨兵都判可用**（缺陷最锐利形态）· 持久成员按实例启用判定 · 实例消失判不可用 · 未知成员判不可用 · **默认 Voice 组数出 2 个可用成员**（修复前是 0，就是那句红色 `No usable models`）· 解析与可用性对身份判断一致。

**为什么 B-07 没有 Kotlin 用例**（刻意，写清以免读者误判）：`ChatViewModel` 依赖整个 Android/Compose/Repository 图，沙箱无法真编译；而一个"源码形状"测试与 Arm 6 完全重复（判据同源），属装置过量。B-07 的验证面 = Arm 6（决策级、6 臂带牙）+ CI `compileReleaseKotlin`（语法/类型权威）。这是按"装置规模 ∝ 影响面"做的取舍，不是遗漏。

---

## 5 CI 判读

（本节在分支 CI 跑完后回填：`head_sha` 三方逐字符、步骤结论、`Summary` 行、门头连续性、`ALL 336`、编译错计数、游离 `FAILED` 计数、新用例逐条 `PASSED`。）

---

## 6 与工单的偏差（一律以真树为准）

1. **B-07 函数名**：工单给的 `liveEffectiveModel()` 已采用；初版我写成 `liveModelOrNull()`，读回工单后改正。
2. **B-07 回退分支**：工单要求 `?: currentModel?.let { ModelsDevApi.enrichModel(it) }`，初版漏了 `enrichModel`，已补。
3. **B-07 半径**：初版顺手把 `currentModelSupportsReasoning` 也接进单源（工单未列），已撤回。
4. **B-06 的 `enabledMemberEntries`**：工单建议"统一经单源解析"，真树核查后改为**显式排除 + 文档**（理由见 §2），这是与工单的实质分歧，不是遗漏。
5. **C34 尾部语义**：初版 ard`fail-closed`（零站点即红），导致合法的纯聚合形态误红 ⇒ 改为"有读取但无决议站点 = 通过"，并补一个绿臂把这条语义钉住。

---

## 7 残留

- **R1 真机行为级（P1 欠账）**：B-02 设置页思考规则在数据加载完成后正确回显、无主线程卡顿；B-06 默认 Voice 组的卡片徽标 / 成员预览 / 计数；B-07 绑定 33 个内置模型后 `TokenUsageSheet` 不再弹 `HEURISTIC` 警告、最大输出行出现、改覆盖值返回后回显。触发条件：下次真机窗口一次开齐。
- **R2（P2）**：`currentModelMaxThinkingLevel` 仍是"条目 overrides 优先 + 快照兜底"的第三条路径，未并入 `liveEffectiveModel()`（语义不同，且不在工单范围）。触发条件：若后续报告指出 reasoning 上限同样误报，则并入。
- **R3（P2）**：`AgentLoopModelsScreen` / `AgentLoopAddSheets` 各只有 1 个解析站点，C34 的单文件阈值是"≥1 站点即验"；若将来这两处增加第二类聚合读，需要重跑门确认没被误判为决议站点。触发条件：这两个文件新增 `memberEntryIds` 消费点。
- **R4（P2，门自伤已闭环）**：`_decl_block` 的分块规则依赖"成员间有空行"这一格式约定（真树与夹具都满足）。若某天代码被格式化工具压成无空行，Gate Arm 6 会静默变弱。触发条件：引入自动格式化或见到成员紧邻。
- **R5（P2）**：C34 的 `_is_member_resolution_site` 用词表（`_RESOLUTION_TOKENS`）识别"是否在解析身份"，新出现的解析 API 名需要补词表。触发条件：新增第三种组员解析写法。
- **R6（P3，已修）**：`_decl_block` 初版吞下一个成员造成假阴性 —— 已由"空行断块"修复，此处只作留痕。
- **R7（P3）**：B-06 测试里的三个 System 显示名（`System Recognition (Online)` 等）是字面量，若产品改名会红。这是有意的：测试要钉住用户可见文案，而非跟随常量。触发条件：改名时同步改测试（红的正是它）。

---

## 8 影响面（用户可见行为变化）

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 打开设置 → 思考规则（切 Provider 实例） | 可能显示上一实例的等级 / 主线程读库 | 加载完成后正确回显；读库在 IO |
| 默认 Voice 输入组卡片 | 无能力徽标、成员预览空、`0 models` 红字 | 显示 ASR 在线/离线两个成员及徽标 |
| 绑定 Gemini 2.5 Pro / GPT-5.2 / Grok 4.5 / Kimi K3 后看 Token 用量 | 红色"窗口只是猜测"、无最大输出行 | 显示权威窗口与最大输出；来源不再是 `HEURISTIC` |
| 同上，模型组设了限额 | 误判 HEURISTIC ⇒ 绕过物理上限钳制 | 显式窗口 ⇒ 走 `minOf(window, groupLimit)` |
| 给 `o3` / `grok-3-mini` 加图片附件 | 不提示不支持 | 正确提示 |
| 给声明 `image_input` 的模型加图片 | 误判不支持 | 判为支持 |
| 改 `maxOutputTokens` 覆盖值后返回聊天页 | 仍旧值 | 立即生效 |

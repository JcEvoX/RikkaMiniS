# FIX-1 · deep-audit-1044（v23 波） — A-01（P1）语音/默认选型读裸 `ModelEntry.model`

- **分支**：`fix/dv24-a1-voice-effective-model`
- **基线**：main `f94413f5`（`f94413f52fa5403c903902f6b1d2ae10cb9bc906`）
- **工单**：A-01（P1，第二道门已核证成立）
- **停止节点**：分支 CI 绿（本报告 §8）。**未合并 main**。

---

## §1 缺陷与根因

**根因一句话**：`ModelEntry.model` 只是 `baseModel.withOverrides(overrides)`（`ProviderConfig.kt:408-409`），**从不查 models.dev**；而 `LLMModel` 目录里 39 个内置模型的 `inputModalities`/`outputModalities` 全为 `null`（`LLMModel.allModels` 构造如此），按「缺省 = 文本进/文本出」约定（`LLMModel.isTextOutput` 的 KDoc、`LLMModel.directionalModalities` 的逐方向兜底），**任何模态问题问裸模型，对所有多模态内置模型都答「不支持」**。全仓单一事实源是 `ModelsDevApi.effectiveModel(entry)` = `enrich(baseModel).withOverrides(overrides)`（`ModelsDevApi.kt:201`，排序契约在 `effectiveEntryModel`（`ProviderConfig.kt:448`））。

`ProviderRepository` 有 **7 处**、`ProviderSpeechRecognitionEngine` 有 **1 处**把能力判断问在了裸模型上（真树 `f94413f5` Before 原文，行号为基线行号）：

| # | 位置 | Before 原文 | 后果 |
|---|---|---|---|
| 1 | `ProviderRepository.kt:2070` `resolveVoiceInputChoice` | `if (!inst.isEnabled \|\| !entry.model.hasAudioInput) return null` | 注册表声明能收音频的多模态模型被从语音输入候选中丢弃 |
| 2 | `:2124` `resolveVoiceInputCandidates` | 同上 | 检测时链路报 `No provider voice-input model configured` → 录音不可用 |
| 3 | `:2208` `resolveVoiceOutputChoice` | `… !entry.model.hasAudioOutput) return null` | 语音输出（TTS）同样被丢 |
| 4 | `:2256` `hasVoiceModels` | `it.providerInstanceId == instanceId && it.model.hasVoiceModality` | 语音服务阴影列表把每一行都画成 `0 models` |
| 5 | `:2326` `shadowVoiceProviders` | `inputModels = entries.filter { it.model.hasAudioInput }` | 阴影行无输入模型可选 |
| 6 | `:2327` 同上 | `outputModels = entries.filter { it.model.hasAudioOutput }` | 同上（输出侧） |
| 7 | `:1283` `newestProviderNewestTextEntry` | `… && !it.isHidden && it.model.isTextOutput }` | `outputModalities == null` ⇒ `isTextOutput` 默认 true ⇒ **纯生图模型可能被选成新会话默认主模型** |
| 8 | `ProviderSpeechRecognitionEngine.kt:232` | `resolvedModel = entry.model,` | 厂商适配器（`VoiceProvider.transcribe` 的 `VoiceInputRequest`）拿到的是未富化模型：它读的模态/能力与「为什么选它」不是同一份事实 |

**证据（三条，互相独立）**：
1. **真树机械取证**：`grep -n "\.model\.\(hasAudioInput\|hasAudioOutput\|hasVoiceModality\|isTextOutput\)"` 在基线 `ProviderRepository.kt` 命中 7 行（上表 1-7），ASR 引擎命中 `resolvedModel = entry.model`。
2. **执行级取证**（本支 §5 L3 layer 1，真 `kotlinc`+JUnit 跑真 `effectiveEntryModel` + 真谓词）：对内置 `gemini-3-pro-preview`（目录里两个模态字段都是 `null`），`entry.model.hasAudioInput == false`，`effectiveEntryModel(entry, devEnrich).hasAudioInput == true`；对 `outputModalities` 缺省的自定义行，`entry.model.isTextOutput == true` 而富化后 `false`。
3. **同族对照（证明单源存在且已合规）**：`ModelsCollection.kt:274`、`ModelUseOffloadHandler.kt:190,1023`、`ModelUseManager.kt:68`、`ChatDebugMethods.kt:265`、`ProviderDebugMethods.kt:137`、`ProviderMutationMethods.kt:554`、`ThinkingLevelCatalog.kt:139`、`ui/components/ModelEntryPicker.kt:476` 都已走 `ModelsDevApi.effectiveModel` —— 本支是把剩下的 8 个入口接上，不是发明新机制。

---

## §2 修法

**阶梯第 6 级：接既有单源（funnel）**。停在这一级的可复核句：**「停在第 6 级，因为全仓已有唯一入口 `ModelsDevApi.effectiveModel`，且 8 个同族调用点已证明它能覆盖两种模态方向与 override 排序，本次只需把剩下的 8 个入口接上去。」**

改动：
1. `ProviderRepository.kt` 加一个私有 funnel：`private fun effectiveEntryModel(entry: ModelEntry): LLMModel = ModelsDevApi.effectiveModel(entry)`（30 行 KDoc 记下「为什么不是 `ModelEntry.model`」「为什么不能写成 `enrichModel(entry.model)`」「为什么不能写回 `baseModelJson`/`overridesJson`」），7 处改为读它。
2. `ProviderSpeechRecognitionEngine.kt:232` 改 `resolvedModel = ModelsDevApi.effectiveModel(entry)`（该文件无 entry 级 funnel，直接调单源，与 `MemberPicker` 之外的 Debug RPC 写法一致）。

**被否决的方案**：
- **把富化结果写回 `baseModel`（持久化固化）**：跨越「派生 vs 事实」边界 —— 注册表会更新、用户 override 语义会被冻结成 base 值，且 `baseModelJson` 一旦被富化就无法区分「用户写的」与「注册表给的」。这是任务书 §3.3 明令禁止项，也是 `FourWaySync` 门的存在理由。
- **新增一个 `ModelEntry.effectiveModel` 属性**（放 `data/model`）：看起来更省调用点，但它会让「读派生值」变成隐式无感行为（`entry.model` 仍在、两个都像事实），并且要在 Compose 重组中隐式做 IO（`loadRegistry` 冷启动会读盘）。既有 8 处已证明「显式命名 `effective*` 调用」是这套代码库的既定风格（`ModelEntryPicker.effectiveModelOf`、`ModelsCollection.effectiveModel`），跟随现状优于新增属性。
- **`:1283` 写「排除 image 模型」特判**：任务书点名不要写特判，且富化后 `isTextOutput` 语义已正确（`directionalModalities` 逐方向兜底，纯生图模型不会拿到 `text_output`）。写特判 = 把注册表事实复制一份到选型逻辑里，将来必漂。

**刻意保留（不动）**：
- `ProviderRepository.kt` 的 `voiceTemplate?.mockModels` 种子判定（`replaceEntries`，基线 `:1397`）：seeds 是 `LLMModel` 值、没有 `ModelEntry`，**无处可富化**；富化它属于错误修法（本支门 arm 6 会把这种「好心」判红）。
- `it.model.id`（`:1211`）与 ASR 里 `entry.model.displayName`（日志文案）：身份/展示，不是能力决定。

---

## §3 改动清单

| 文件 | 增/删 | 本支行区间（改后行号） |
|---|---|---|
| `data/repository/ProviderRepository.kt` | +38 / −7 | 导入 `+34`；funnel `+1264..1293`；站点 `1314`、`2101`、`2155`、`2239`、`2287`、`2357-2358` |
| `speech/ProviderSpeechRecognitionEngine.kt` | +10 / −1 | 导入 `+10`；`resolvedModel` `+233..241` |
| `test/.../data/repository/VoiceModalityEffectiveModelTest.kt`（新增） | +460 / −0 | 新文件（12 个 `@Test`） |
| `scripts/scan/model_entry_override_parity_check.py` | +563 / −13 | arm 4-7 常量/实现/夹具（见 §4） |

合计 **4 文件 +1071 / −21**（`git diff --numstat f94413f5..HEAD`）。
**门数 109 / 夹具 336 不变**（0 新建门；只扩既有门；`scripts/scan/scan.sh` 与 `test_scan.py` 零触碰）。

提交：`4bce8f71` 生产 · `67ed74b8` 测试 · `fe6ab36e` 门（报告单独提交）。

---

## §4 门判据与带牙证明

门：`scripts/scan/model_entry_override_parity_check.py`，新增 **arm 4-7**（原有 arm 1-3 属 `ModelEntryOverrideBuilder`，未改判据）。

**判据锚「决定」不锚拼写**：arm 4 不匹配 `entry.model.hasAudioInput` 这个字符串，而是**解析接收者链**——凡是 `.hasAudioInput/.hasAudioOutput/.hasVoiceModality/.isTextOutput` 的接收者不是 `effective*` 函数调用 ⇔ 违规。因此 `entry.model.x` / `it.model.x` / 局部别名 `val m = entry.model` 之后再读 / 未来新增的第 9 个入口，一律同样红；反过来，把单源改名成任何 `effectiveXxx(` 仍绿（改名不失明，删单源会红）。

- **arm 4**（`ProviderRepository.kt`）：(a) 非种子函数内不得有未富化的能力读；(b) funnel 定义必须存在且**真的**走 `ModelsDevApi.effectiveModel(`（自证式重推导会红）；(c) 决策台账 6 个函数（`newestProviderNewestTextEntry` / `resolveVoiceInputChoice` / `resolveVoiceInputCandidates` / `resolveVoiceOutputChoice` / `hasVoiceModels` / `shadowVoiceProviders`）必须存在且仍读对应谓词（**删判据或改名 ⇒ 红，不许静默缩小**）。
- **arm 5**（ASR 引擎）：`resolvedModel =` 必须存在、其值必须是 `effective*(` 调用；文件内能力读亦须富化。
- **arm 6**（唯一豁免）：`?.mockModels` 锚只允许 **1 个**函数拥有；该函数必须保留能力读，且**不得**走 funnel（seeds 无 entry）。
- **arm 7**（漂移）：`VoiceModality.kt` 里 `val LLMModel.<x>: Boolean` 且正文涉 audio 的，必须被本门收录（或显式列入 `NON_SELECTION_PREDICATES`）——新增音频谓词不能靠「没写进清单」逃逸。

**注释剥离**：门用真正的 Kotlin 注释屏蔽器（行注释 + 块注释 + 保留字符串/原始字符串），所以**我写进生产代码的注释不会命中判据**，URL 里的 `//` 也不会吞掉后面的代码（1043 FIX-10 的同族教训）。

**带牙表（15 条夹具，逐条精确红且只红该臂；`python3 probe/l1/arm_messages.py` 产出原文）**：

| 变异 | 红在哪条判据 |
|---|---|
| `me-inline-lists` | [1] `save block contains forbidden inline-construction snippet 'val newInputs = buildList'`（既有臂，回归确认） |
| `ob-positional-ctor` | [2] `buildModelEntryOverrides contains positional construction 'ModelOverrides('`（既有臂） |
| `pr-bare-modality-read`（= 基线 7 处形态） | [4] ×7：`hasAudioInput` off `entry.model` ×2、`hasAudioInput` off `it.model`、`hasAudioOutput` ×2、`hasVoiceModality`、`isTextOutput` |
| `pr-funnel-is-raw`（funnel 写成 `= entry.model`） | [4] `the effectiveEntryModel( funnel does not route through ModelsDevApi.effectiveModel(` |
| `pr-gate-dropped`（删 `resolveVoiceOutputChoice` 的守卫行） | [4] `resolveVoiceOutputChoice no longer reads hasAudioOutput` |
| `pr-seed-enriched`（种子走 funnel） | [6] `replaceEntries routes the template seeds through the effective-model funnel` |
| `pr-two-seed-owners`（第二个函数也用 `?.mockModels`） | [6] `the seed carve-out is claimed by 2 functions (replaceEntries, templateSeedCount)` |
| `asr-raw-resolved-model` | [5] `the transcription request hands over entry.model` |
| `asr-no-resolved-model`（字段改名/删除） | [5] `resolvedModel = not found in the VoiceInputRequest construction` |
| `vm-new-audio-predicate`（新增 `hasAudioStream`） | [7] `new audio predicate hasAudioStream is not covered by this gate` |

**PRE 臂（对基线 blob）**：`python3 probe/l1/gate_pre_arm.py`（把 5 个文件按 `f94413f5` 取基线/工作树组成临时树）⇒ `model_entry_override_parity_check: 9 violation(s)`：7 条裸读（`:2070/:2124/:2326/:2208/:2327/:2256/:1283`）+ `effectiveEntryModel( funnel is gone` + `[5] the transcription request hands over entry.model`，rc=1。**与本支声明的 8 个缺陷点逐点对应**。

---

## §5 验证四层结果

### L1 门
- 本门真树：`model_entry_override_parity_check: OK (7 arms, 0 violations)`，rc=0。
- `--selftest`：`MODEL_ENTRY_OVERRIDE_SELFTEST=PASS`，15/15 夹具（7 条既有 + 8 条新）。
- 持锁运行器（多会话并发纪律）：`gate_run.sh --gate … --selftest` ⇒ `ran=1 pass=1 fail=0 sandbox=0 envretry=0 verdict=GREEN`（`gate-runs/20261023T024827Z`）。

### L2 邻门
- 按概念反查（`grep -rn "ProviderRepository.kt" scripts/scan/`）后 `gate_run.sh --subset`：**8/8 rc=0** —— `compact_entry_pin_migration` / `entry_composite_key_guard` / `entry_id_rename_integrity` / `log_url_redaction_guard` / `model_entry_override_parity` / `openminis_compat_field_parity` / `provider_import_id_roundtrip` / `provider_models_cache_wiring`（`gate-runs/20261023T024838Z`）。
- 另跑 5 道语义邻门（含任务书/记忆里可能被点名的同族）：`provider_remember_key_check` / `modeluse_proto_single_source_check` / `voice_cancel_guard_check` / `models_provider_field_check` / `thinking_wiring_guard` ⇒ **5/5 PASS**。
- 任务书要求「扩 `voice_cancel_guard_check.py` 若其管辖 ASR 引擎」：**该门不管辖** `ProviderSpeechRecognitionEngine.kt`（其 `VP_REL`/`OR_REL`/`VENDORS_REL` 只有 `VoiceProvider.kt` / `OpenRouterModelsApi.kt` / `VoiceProviderVendors.kt`，主题是 cancel/guard 形态），故 **未改它**，ASR 的判据落在 arm 5（见 §6-3）。

### L3 真跑（真 `kotlinc` + 真 `JUnitCore`，双臂 + 5 变异臂）
装置：`probe/l3/run.sh` —— 编译**真** `LLMModel.kt` / `ProviderConfig.kt` / `VoiceModality.kt` / `ProviderCredentialMeta.kt`（+ 一个 `androidx.compose.runtime.Stable` 注解桩 + 真 `kotlinx-serialization-core` jar）与本支新测试，一次编译，`user.dir` 决定读哪棵树。

| 臂 | 结果 |
|---|---|
| **POST**（工作树） | `OK (12 tests)` |
| **PRE**（`git show f94413f5:` 取基线两个 blob，其余同树） | **精确 5 红**：`the transcription request carries the effective model`（`the transcription request hands over the raw entry model` + `resolvedModel is not resolved through ModelsDevApi.effectiveModel(`）、`the ProviderRepository funnel is the single source, not a re-derivation`（`the effectiveEntryModel( funnel is gone`）、`every ProviderRepository capability decision goes through the effective model`（6 个函数 × 2 条：`reads a modality off the raw entry model` + `does not consult the effective model`）、以及两条依赖「shipped source 干净」前提的带牙自检（锚点消失即红）。**layer 1 的 7 条行为用例在 PRE 臂仍绿** —— 正确：决策核心（`effectiveEntryModel` 契约）本来就对，缺陷在接线。 |
| 变异 `m1-input-gate`（语音输入守卫退回 `entry.model`） | 红 `every … capability decision …` + 带牙自检 |
| 变异 `m2-voice-pred`（`hasVoiceModality` 退回 `it.model`） | 红 `every … capability decision …` + 带牙自检 |
| 变异 `m3-funnel-raw`（funnel `= entry.model`） | 红 `the ProviderRepository funnel is the single source` + 带牙自检 |
| 变异 `m4-seed-enriched`（种子走 funnel） | 红 `the voice template seeds keep their raw modality read` + 带牙自检 |
| 变异 `m5-asr-raw`（`resolvedModel = entry.model`） | 红 `the transcription request carries the effective model` + 带牙自检 |

> 变异臂里「带牙自检」连带红是**设计如此**：它先断言「shipped 源码按预期形态存在」，变异树里该前提不成立 ⇒ 它自己红。正向意义：带牙自检不能被「源码变了」绕过。所有变异都在 `probe/l3/mut-*` 私有副本树上做，**工作树从未进入变异态**（未用 `git checkout --`）。

**为什么 layer 2 用源码探针而不是实例化 `ProviderRepository`**：`ProviderRepository(private val context: Context)` 需要真 Android Context（Room + SharedPreferences + EncryptedSharedPreferences），沙箱/单测都构造不出来（同一理由见 `ProviderThinkingRulePublishOrderTest` 的 KDoc）。按 COMMON §4「抽不出来就写清不可 JVM 化 + 用门带牙替代，不要硬造装置」，这里把行为面压到 `effectiveEntryModel` + 真谓词（真跑），把接线圈到源码探针 + 门（带牙）。

### L4 分支 CI
**run `39891898960` / job `120213372981` attempt 1 `success`**（`workflow_dispatch`），`head_sha` 与本地/远端逐字符一致 —— 详见 §8。
本支在 CI 上的在册证据：`model_entry_override_parity_check: OK (7 arms, 0 violations)` · `✅ … selftest has teeth (MODEL_ENTRY_OVERRIDE_SELFTEST=PASS)` · `✅ … real tree green` · **新增 12 条用例逐条 PASSED** · `SystemVoiceMemberResolutionTest` **10/10 PASSED**（任务书要求「v22 波新增用例保持全绿」，实测在册）。

---

## §6 任务书失真

1. **`:1283` 已给出「纯生图模型可能被选成默认主模型」的机制，但对 `LLMModel.allModels` 而言是 latent**：本支用**自定义行 + 注册表行**证明缺陷可达（`outputModalities` 缺省 = 文本出，注册表说 image）。内置 39 条里没有「纯生图」条目，所以该点在生产上的触发面是「用户自建/同步来的 image 模型」。判级不受影响（P1 的另 7 处有直接触发面）。
2. **任务书称 7 处 + 第 8 处，实测行号全部对得上**（`:2070/:2124/:2208/:2256/:2326/:2327/:1283` + ASR `:232`），仅因中间插入 funnel 而整体下移 30 行左右 —— 这不是失真，仅记录以便下批按新行号检索。
3. **任务书 §4 要求「扩 `voice_cancel_guard_check.py`（若其管辖 `ProviderSpeechRecognitionEngine.kt`）」**：先读了该门现状 —— **不**管辖（只管 `VoiceProvider.kt` / `OpenRouterModelsApi.kt` / `VoiceProviderVendors.kt`）。按「门独占 + 不扩别人主题的门」纪律，未改它；ASR 判据放 `model_entry_override_parity_check.py` arm 5（该门本就是「model entry ↔ 能力/override 一致性」的家，且本批归本支独占）。
4. **任务书 §4 建议扩展 `ModelUseModalityParityTest`**：未扩展它。理由：该测试属 dv21-A05（`minis-model-use` / config collection 三个读者），主题是「同一事实三处读」。本支是「剩余 8 个读者接上单源」且对象是 `ProviderRepository`／ASR，扩展它会把这支的验证塞进别人的测试文件（下批按 prefix 检索会误归族）。改为新文件 `VoiceModalityEffectiveModelTest`（命名全仓唯一，已核 `grep -rl`）。**`SystemVoiceMemberResolutionTest` 未改动，12 个既有用例不受影响**（本支不触碰 `SystemVoiceEntries`）。
5. **任务书未提到的第 9 个读点**：`it.model.id`（`:1211`）与 ASR 的 `entry.model.displayName`（日志）——**不是**能力决定，属半径外，未改（见 §7 R2）。

---

## §7 未做 / 残留（每条带触发条件）

- **R1 真机行为级（必须真机，别假装验过）**：①把 models.dev 声明音频输入的多模态模型（如 Gemini 系）选进 Voice Input 组 → 长按录音应能出文字，不再弹 `No provider voice-input model configured`；②设置 → 语音服务阴影列表：该实例行应显示 ≥1 个输入/输出模型，不再是 `0 models`；③新会话默认模型：自定义的纯生图模型（无 `outputModalities`）不再被选为默认。**触发条件 = 下次真机窗口**（汇总进主会话 `§1044-VERIFY`）。
- **R2 展示类裸读未并入**：`ProviderRepository.kt:1211` `it.model.id`、ASR `entry.model.displayName`。这是身份/文案，不是能力决定；`displayName` 若将来要在 UI 上体现注册表规范名，才需要改。**触发条件 = 出现「模型显示名与 models.dev 不一致」的用户报告**（触发后方案：改用 `effectiveEntryModel(entry).displayName`，并给 arm 5 加一条判据）。
- **R3 冷启动窗口内富化仍为 no-op**：`ModelsDevApi.loadRegistry()` 在「无内存缓存 + 无磁盘缓存 + 无内置 asset」时返回 `null`（并踢后台 refresh），`enrichModel` 原样返回模型 —— 与本支修法前的行为一致（不回归）。**触发条件 = 真机首启后立刻录音/选模型时仍观察到「0 models」**（触发后方案：把 registry 首刷完成作为语音组解析的前置，或让 `VOCED` 解析走 `enrichModels` 的语言级缓存；不得在本支半径内新增缓存层）。
- **R4 `shadowVoiceProviders` 的富化成本**：`hasVoiceModels(inst.id)` 在实例循环里被调用（`ProviderRepository.kt:2330`、`:2370`），每次遍历该实例全部 entry 并逐条富化（注册表 map 查找 + `copy`）。典型配置（10 实例 × 数十条）下是数百次 O(1) 查找，Compose 侧无感；但没有按 revision 缓存。**触发条件 = 语音服务页在超大配置（≥500 entry）出现滚动卡顿**（触发后方案：`effectiveModel` 结果按 `(config.revision, entry.uuid)` 记忆化，或改为每实例一次 `enrichModels` 批量）。
- **R5 arm 7 的漂移判据只覆盖 `VoiceModality.kt`**：`isTextOutput` 定义在 `LLMModel.kt`，若将来在 `LLMModel.kt` 新增音频谓词，本门不会自动发现。**触发条件 = 有人在 `LLMModel.kt` 加 `val LLMModel.x: Boolean` 且涉 audio**（方案：把 arm 7 的推导源扩到 `LLMModel.kt`）。
- **R6 arm 4 的接收者链**只回看一层调用（`foo.bar(x).baz.hasAudioInput` 这类链式属性读只判定最后的调用名）。当前树无此形态。**触发条件 = 出现「富化后再取子对象判模态」的写法**（方案：接收者链解析改为逐段校验）。
- **R7 `hasVoiceModels` 语义未改**：`it.model.hasVoiceModality` 判的是「音频输入或输出」，富化后语义与 KDoc（"ANY model entry with an audio modality"）一致，未动。**无触发条件（刻意保持）**。
- **R8 CI 判读器形态**：本支判读用 `run.json`/`jobs.json` API 取 `head_sha` 与 `steps[].conclusion`（不从日志文本猜 skip），门头/Summary 判据沿用 1043 波定型的真形态正则。**无触发条件（做法本身是纪律）**。

---

## §8 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f94413f52fa5403c903902f6b1d2ae10cb9bc906`（`git rev-parse` 于开工时实测，与 COMMON/TRIAGE 声明逐字符一致） |
| 本地 HEAD（CI 验过的那棵树） | `fe6ab36e5b2a6a210ad82ed0dd3872debef18462` |
| `git ls-remote origin fix/dv24-a1-voice-effective-model` | `fe6ab36e5b2a6a210ad82ed0dd3872debef18462` |
| CI `run.head_sha` | `fe6ab36e5b2a6a210ad82ed0dd3872debef18462` |
| **四方核对** | **逐字符一致 ✅** |
| CI run / job | **run `39891898960`**（`workflow_dispatch`，attempt 1）/ **job `120213372981`**（`build`） |
| conclusion | **success** |
| 步骤 | 26 步，`not-ok = []`，唯一 `skipped = Publish to Releases`（main 门控，分支上预期） |
| job 日志 | 1,394,526 B（`logs/fix1-job120213372981.log`） |

**判读（先取 API JSON 判身份与步骤，日志只判内容）**：
- `logs/judge_identity.py` → `sha match: True`、`steps: 26 not-ok: []`、`skipped: ['Publish to Releases']`。
- `logs/judge_generic.py` → `ALL GENERIC CRITERIA GREEN`：
  - `Summary lines: 9, non-zero: 0, 109/0: 1`（恰一条 `Summary: 109 passed, 0 failed`）
  - `ALL 336 CASES PASS lines: 1`
  - `gate headers: 109, distinct nums: 109, distinct names: 109`（门头 `[n/109]` 1..109 连续且门名互异）
  - `compile errors ('Z e: '): 0`
  - `FAILED lines: 6, stray (no PASSED on same line): 0`（6 条逐条确认全是以 `PASSED` 结尾的既有用例名子串：`slot denied produces FAILED …` / `reason PERSISTENCE_FAILED …` / `phase FAILED …` / `terminal FAILED …` / `reason EXECUTION_FAILED …` / `the fixed tail still reaches a legal FAILED terminal`）
- **本支证据在册**：`model_entry_override_parity_check: OK (7 arms, 0 violations)`（日志 775 行）· `✅ model_entry_override_parity_check selftest has teeth (MODEL_ENTRY_OVERRIDE_SELFTEST=PASS)`（1218 行）· `✅ model_entry_override_parity_check real tree green`（1219 行）· **新增 12 条用例逐条 `PASSED`**（`VoiceModalityEffectiveModelTest > …`，日志 6032–6054 行）。
- **三源取二满足**：①CI 绿（`compileReleaseKotlin` 是 Kotlin 语法的唯一权威）＋②沙箱真 `kotlinc`+JUnit 双臂/五变异（§5 L3）＋③本门真树绿 + 15 夹具带牙。

**分支 HEAD 与 CI 绿 sha 的关系**：报告以 `docs(dv24-a1)` 单独提交进 `fix-reports/FIX-1.md`（`fix-reports/` 不在 workflow `paths` 内、不参与构建），因此该提交后源码树与 CI 验过的 `fe6ab36e` 逐字节等价 —— 判据 `git diff --name-only fe6ab36e HEAD -- . ':(exclude)fix-reports'` 输出为空（比追 HEAD 重跑一轮 CI 便宜且更硬，1042/1043 定型做法）。

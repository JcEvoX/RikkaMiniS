# FIX-8（deep-audit-1044 / dv24 波 B-03 + B-04）

> 分支 `fix/dv24-b2-chat-factory-effective` · 基线 main `f94413f5` · 未合并 main（起手权在用户）

## §1 缺陷与根因

### B-03：`ProviderFactory.create` 全部传裸 `entry.model`（真树 Before + 实测行号）

`ModelEntry.model` = `baseModel.withOverrides(overrides)`（`data/model/ProviderConfig.kt:387-421`），
**从不查 models.dev**。39 个内置模型的模态/档位/输出上限字段按构造全为 `null`（v22/1043 已证），
真正的能力数据只在 models.dev 注册表里。

而这 8 处 `ProviderFactory.create(...)` 传的就是这个对象：

```
$ grep -rn "ProviderFactory.create(" src/android/app/src/main/java/com/rikkaminis/app/ui/chat/
ChatModelRouting.kt:151      val newProvider = ProviderFactory.create(instance, apiKey, entry.model, context)
ChatModelRouting.kt:369      ProviderFactory.create(instance, apiKey, entry.model, context)
ChatModelRouting.kt:434      currentProvider = ProviderFactory.create(instance, apiKey, targetEntry.model, context)
ChatPromptAndTools.kt:2115   return ProviderFactory.create(instance, apiKey, entry.model, context)
ChatSessionLifecycle.kt:425  ProviderFactory.create(decision.instance, decision.apiKey, entry.model, context)
ChatSessionLifecycle.kt:1147 currentProvider = ProviderFactory.create(decision.instance, decision.apiKey, entry.model, context)
ChatSessionLifecycle.kt:1200 currentProvider = ProviderFactory.create(instance, apiKey, targetEntry.model, context)
ChatSessionLifecycle.kt:1287 currentProvider = ProviderFactory.create(decision.instance, decision.apiKey, entry.model, context)
```

外加 `ChatViewModel.kt:2702` 传 `cachedProvider.model`（路由字段热重建）。

**根因（可复核的一句）**：provider 把这个 `LLMModel` 存成 `model` 并随请求 IPC 送到
`:modelervice`——那是一个**从不调用 `ModelsDevApi.init`** 的独立进程，它只看到序列化后的
`LLMModel` 对象；而 `attachImageParts` / `clampThinkingLevel` / `injectThinkingParams` /
输出上限全部**从这个对象重新判定**能力 ⇒ 未富化的模型在真线上被静默降级。

**症状原话**（三条，按严重度）：
1. 只在 models.dev 声明 `image` 的模型 ⇒ 附件被降级成 `[attached image: …]` 文本占位。
2. 只在注册表声明 effort 档位的模型 ⇒ 选了 `MAX`，被 `clampThinkingLevel` 夹回 `XHIGH`。
3. 只在注册表声明 `maxOutputTokens` 的模型 ⇒ 输出被夹到 provider 默认值（Token Usage 面板
   却显示真实 64K/128K，UI 与真线不一致）。

**≥1 条证据**：`ModelsDevApi.effectiveModel`（`provider/ModelsDevApi.kt:201`）的 KDoc 明写
「**the single entry point for every surface that answers "what can this model do"**」，
而 `ui/chat` 的 8 个构造点一个都没接；同批 A-01 支（语音侧）已把同一条单源接上，
两侧行为因此不一致。

### B-04：`applyNewChatDefaultModel` 从不写 `currentModel` + 4 个读者读裸快照

```
ChatSessionLifecycle.kt:1280-1289 (基线)
  /** ... Sets currentModel / currentProvider / the name + activeEntry state flows. */
  internal fun ChatViewModel.applyNewChatDefaultModel(): Boolean {
      ...
      _modelName.value = entry.model.displayName
      _activeEntryId.value = entry.id
      _providerName.value = decision.instance.label.ifEmpty { entry.model.provider }
      currentProvider = ProviderFactory.create(decision.instance, decision.apiKey, entry.model, context)
      return true          // ← 没有 currentModel =
  }
```

KDoc 承诺写 `currentModel`，函数体没写 ⇒ **无默认组的新会话** `currentModel` 恒 null ⇒
- `ChatSessionEntryExt.kt:90` `currentModel?.id ?: … ?: "unknown"` ⇒ 持久化 `model_id = "unknown"`；
- `thinkingInfo()`（`ChatSlashTokenExt.kt:44`）`currentModel ?: return null` ⇒ Token Usage 的
  Thinking 整块消失；
- `dynamicMaxTokens()`（`ChatErrorHandling.kt:94`）`currentModel ?: return …` ⇒ 剩余上下文夹取被跳过。

dv23-B07 已把 4 个读者接上 `liveEffectiveModel()`，但那 4 个**全在 `ChatViewModel` 类内**；
FE-5 把 `dynamicMaxTokens` / `thinkingInfo` 抽成了**另外两个文件**里的扩展函数 → 逃出那次清扫，
仍在读裸快照。另两个类内读者（`currentModelSupportsReasoning` `:1993`、
`currentModelMaxThinkingLevel` `:2020`）dv23-B07 也没覆盖。

## §2 修法（阶梯与被否决的方案）

**停在第 6 级（接既有单源）**——`ModelsDevApi.effectiveModel` / `effectiveEntryModel`
早已存在并有 KDoc 契约，本支只是把 8 个调用点接上去。**不新写任何抽象层 / 缓存 / 回调总线**。

新增的唯一一个出口（`ChatViewModel.kt:1993-2015`）：

```kotlin
internal fun providerModelFor(entry: ModelEntry): LLMModel =
    ModelsDevApi.effectiveModel(entry)
```

即 `effectiveEntryModel(entry, ::enrichModel)` = **先富化 base、再叠用户 override**。
顺序是全部要害：`enrichModel` 的填充规则是 `devModel.x ?: model.x`（无条件覆盖），
若先叠 override 再富化，注册表的 `reasoning = true` 会盖掉用户显式的
`supportsReasoning = false`（dv23-A04 已证的反向陷阱，本支的 m2 变异臂专门钉它）。

### 被否决的方案

| 方案 | 否决理由 |
|---|---|
| ① 在 `ProviderFactory.create` 内部统一富化 | 工厂是 provider 层的通用入口，不知道 `ModelEntry`（只有 `LLMModel`），要富化就得把 `ModelEntry` 塞进 provider —— 那是新抽象层，且会把富化扩散到 provider 层所有调用者（半径失控） |
| ② 让 `:modelervice` 侧自己调 `ModelsDevApi.effectiveModel` | worker 进程**从不初始化** `ModelsDevApi`（无 `init` 调用、无 asset 缓存），让它自己富化 = 在 worker 里新建一条注册表加载链，且 worker 侧决策与 UI 侧决策会再次分叉（同一形状的老路） |
| ③ 只改 `currentModel` 快照，不改 `create` 的入参 | 快照与 provider 持有的对象会不一致：provider 内部 `provider.model` 仍是裸模型 ⇒ 真线降级依旧。必须**两者同源** |
| ④ 用 `enrichModel(entry.model)`（先叠 override 再富化） | 注册表覆盖用户显式 override（dv23-A04），已作为 m2 变异臂证明有牙 |
| ⑤ 把 `liveEffectiveModel` 改成 `public` | 同包外没有别的读者；`internal` 足够，且门判据锚 `(?:private\|internal)`（TRIAGE §4 明确保留） |

### 刻意简化处

```
ponytail: 路由热重建点（ChatViewModel.kt:2737）只在 live 模型与缓存模型的 id 相同时才用 live 模型，
          否则沿用 cachedProvider.model | 天花板：同一 id 的模型换了 baseModel 而 id 不变时，
          重建仍带旧对象 | 升级触发：出现「改 baseModel 但 id 不变」的入口
```

```
ponytail: applyNewChatDefaultModel 补的 currentModel 只在决策 Use 分支写，
          Unusable 分支不写（保持 null 让上层看到「未就绪」） | 天花板：Unusable 后 UI 短暂读到 null
          | 升级触发：出现「Unusable 分支也需要一个展示模型」的需求
```

## §3 改动清单

基线 `f94413f5` → 本支 HEAD：**9 文件 +731 / −44**（生产 6 / 测试 1 / 门 2）。

### 生产（6 文件，+118 / −31）

| 文件 | 增删 | **本支改动的行区间**（`git diff -U0` hunk 头） |
|---|---|---|
| `ui/chat/ChatViewModel.kt` | +43 / −4 | `@@ -1986 +1987 @@`（`liveEffectiveModel` 可见性放宽 private→internal）· `@@ -1993 +1993,23 @@`（**新增 `providerModelFor` + `currentModelSupportsReasoning` 接单源**）· `@@ -2020 +2044,9 @@`（`currentModelMaxThinkingLevel` 兜底改走 live）· `@@ -2705 +2737,8 @@`（路由热重建点）· `@@ -46,0 +47 @@`（`import ModelEntry`） |
| `ui/chat/ChatSessionLifecycle.kt` | +41 / −15 | `@@ -421,2 +421,10 @@`（loadSession 回退恢复）· `@@ -425,2 +433,2 @@`（create 入参）· `@@ -1141,3 +1149,7 @@` + `@@ -1147 +1159 @@`（restoreFromBinding 单模型分支）· `@@ -1195,3 +1207,7 @@` + `@@ -1200 +1216 @@`（resolveProviderFromGroup）· `@@ -1284 +1300,11 @@` + `@@ -1286,2 +1312,2 @@`（**applyNewChatDefaultModel：补 `currentModel =`（B-04 核心）**） |
| `ui/chat/ChatModelRouting.kt` | +19 / −9 | `@@ -151,4 +151,8 @@`（selectEntry）· `@@ -369 +373,4 @@`（buildFallbackProviders）· `@@ -429,3 +436,6 @@` + `@@ -434 +444 @@`（rotateForNewTurn） |
| `ui/chat/ChatErrorHandling.kt` | +7 / −1 | `@@ -94 +94,7 @@`（`dynamicMaxTokens` 接 live） |
| `ui/chat/ChatSlashTokenExt.kt` | +4 / −1 | `@@ -44 +44,4 @@`（`thinkingInfo` 接 live） |
| `ui/chat/ChatPromptAndTools.kt` | +4 / −1 | `@@ -2115 +2115,4 @@`（`resolveTitleProvider`） |

> **跨支同文件提示**：`ChatViewModel.kt` / `ChatSessionLifecycle.kt` / `ChatModelRouting.kt`
> 三件是本批多支的公共文件。上表的 hunk 行区间是**本支唯一**触碰的区间，其他支若也改了
> 同文件请按行区间核对，不要按文件整体判定冲突面。

### 测试（1 文件，+255）

`src/android/app/src/test/java/com/rikkaminis/app/ui/chat/ChatProviderModelSourceTest.kt`（新建，6 个用例）

### 门（2 文件，+371 / −22）

- `scripts/scan/models_provider_field_check.py`：**新增 Arm 7**（out-of-class 读者）+ Arm 6 读者集扩 2 个 + `liveEffectiveModel` 正则放宽为 `(?:private|internal)`。
- `scripts/scan/session_provider_enabled_check.py`：**新增 B-03 判据**（8 个 create 点的模型实参）+ **新增 B-04 判据**（applyNewChatDefaultModel 必须写 `currentModel`）。

**门数 109 → 109，夹具 336 → 336**（0 新建门；`scan.sh` / `test_scan.py` **零触碰**）。

## §4 门判据与带牙证明

### 4.1 `session_provider_enabled_check.py`（B-03 判据）

判据（**决定**，不锚拼写）：
1. 三个 chat 文件里每一个 `ProviderFactory.create(` 的第 3 个实参，
   不得是 `entry.model` / `targetEntry.model` / `cachedProvider.model`（**点名**）；
2. 也不得是任何其它标识符——除非它是**本函数内**由 `providerModelFor(` /
   `liveEffectiveModel(` / `ModelsDevApi.effectiveModel(` 赋值的局部 val；
3. 直接写 `providerModelFor(entry)` / `ModelsDevApi.effectiveModel(entry)` 也接受；
4. **fail-closed**：三文件合计 create 点少于 8 ⇒ 红（检测丢失 = ROT），不是静默通过。

B-04 判据：`applyNewChatDefaultModel` 的函数体必须含 `currentModel =`，且该写入在
`decideSessionModelResolution(` 之后（先决策后写）。

带牙臂（selftest 4 条，逐条精确红其判据）：

| 臂 | 改什么 | 红在哪条 |
|---|---|---|
| `factory enriched` | 8 个点全走单源 | 0 违规（绿臂） |
| `factory raw-entry` | 其中 1 个点改回 `entry.model` | **恰 1** 违规，且消息指名该点 |
| `factory unenriched-ident` | 1 个点传 `entry.baseModel`（不点名的裸标识符） | **恰 1** 违规 |
| `factory rot` | 只留 3 个 create 点 | **恰 1** 违规（call-site 计数 < 8） |
| `apply w/o currentModel` | 删掉 `currentModel =` | 恰 1 违规（B-04 判据） |

### 4.2 `models_provider_field_check.py`（Arm 7）

判据：`ChatErrorHandling.kt` 的 `dynamicMaxTokens` 与 `ChatSlashTokenExt.kt` 的 `thinkingInfo`
各自的函数体必须含 `liveEffectiveModel()`；**剥掉 KDoc 与 `//` 注释**后再判（KDoc 是散文，
既不能满足也不能触发判据）；读者消失 ⇒ ROT 红。

同时 Arm 6 的 `CVM_READERS` 由 4 个扩到 6 个（补 `currentModelSupportsReasoning` /
`currentModelMaxThinkingLevel`），Arm 6 的 `liveEffectiveModel` 签名正则放宽为
`(?:private|internal)`。

带牙臂（selftest 新增 4 条）：

| 臂 | 改什么 | 红在哪条 |
|---|---|---|
| `arm 7 dynamic-max-tokens-raw` | 改回 `val model = currentModel` | **恰 1**（只红 ceh 那条） |
| `arm 7 thinking-info-raw` | 改回 `val model = currentModel` | **恰 1**（只红 cst 那条） |
| `arm 7 kdoc-only-mention` | KDoc 里写 `[liveEffectiveModel]`，代码仍读 `currentModel` | **恰 1**（证明注释不能满足判据） |
| `arm 7 reader-gone` | 删掉整个读者 | **恰 1**（ROT） |

Arm 6 的 pre-fix 期望数由 9 改为 11（新增的两个读者各贡献一条）。

### 4.3 仓库内测试（`ChatProviderModelSourceTest`，6 用例）

- `everyProviderConstructionSitePassesAnEnrichedModel`（真树 ≥8 点，逐点判 + ROT 计数）
- `theNewChatDefaultModelSitePublishesTheModelItResolved`（写 `currentModel` 且在决策之后）
- `theLiveExitIsVisibleToTheExtensionReaders`（`liveEffectiveModel` 必须是 `internal`）
- 3 条**负控**：`theCreateSiteReaderRejectsARawModel` /
  `theCreateSiteReaderAcceptsTheSharedResolver` /
  `thePublishReaderRejectsASilentSite` —— 用合成源码证明三个 reader 都会拒掉 pre-fix 形态。

## §5 验证四层结果

### L1 门

- `python3 scripts/scan/models_provider_field_check.py .` → `OK (6 arms clean)`，rc=0
- `python3 scripts/scan/models_provider_field_check.py --selftest` → `MODELS_PROVIDER_FIELD_SELFTEST=PASS`
- `python3 scripts/scan/session_provider_enabled_check.py .` →
  `session_provider_enabled_check: 8 ProviderFactory.create site(s) resolved through the enriched model` / `GATE=GREEN`，rc=0
- `python3 scripts/scan/session_provider_enabled_check.py --selftest` → `SESSION_PROVIDER_ENABLED_SELFTEST=PASS`
- **全量 `sh scripts/scan/scan.sh` → `║  Summary: 109 passed, 0 failed`**，rc=0
- **全量 `python3 scripts/scan/test_scan.py` → `✅ ALL 336 CASES PASS`**，rc=0

> 沙箱噪声记录：全量扫描在 PRoot 下多次撞 `OSError: [Errno 38] Function not implemented`
> （`/usr/lib/python3.12/encodings/utf_8_sig.py`、`/bin/sh: can't create /tmp/.minis_cwd_*`），
> 表现为门脚本被 ENOSYS 打断 ⇒ Summary 掉到 `43 passed, 66 failed`。**间隔 45–60 s 重跑即恢复**，
> 最终一次 109/0 + 336 全过。与本支改动无关（同一现象在基线树上也出现）。

### L2 邻门（按概念反查 `scripts/scan/`，5/5 rc=0）

| 邻门 | rc | 末行 |
|---|---|---|
| `chat_session_id_binding_check` | 0 | `all criteria satisfied` |
| `chat_ui_interaction_check` | 0 | `CHAT-UI-INTERACTION GATE OK` |
| `deeplink_settings_routes_check` | 0 | `DEEPLINK_SETTINGS_ROUTES_CHECK=PASS` |
| `memory_gate_provider_fields_check` | 0 | `clean` |
| `outcome_known_chain_check` | 0 | `GATE=GREEN outcome_known_chain` |

### L3 真跑

**（a）可 JVM 化的那一半——enrich/override 的顺序契约**（`probe/l3/`）：

桩件 `Stubs.kt` **逐字抄**生产实现：`LLMModel.withOverrides`（`ProviderConfig.kt:422`）、
`effectiveEntryModel`（`:448`）、`applyDevData`（`ModelsDevApi.kt:235`）、`effectiveModel`。
SUT（`providerModelFor` 的函数体）由 `extract.py` **从真树按字节切片**，不重打。

| 臂 | SUT | 结果 |
|---|---|---|
| **POST** | `ModelsDevApi.effectiveModel(entry)` | `OK (3 tests)` |
| **PRE**（基线 `f94413f5` 实际传给 create 的实参） | `entry.model` | `Tests run: 3, Failures: 1` — **精确红在** `theModelHandedToTheProviderCarriesTheRegistryCapabilities`：`expected:<[text, image, audio, video]> but was:<null>` |
| m1（退回 `entry.model`） | `entry.model` | 1 红（同上） |
| m2（先叠 override 再富化） | `ModelsDevApi.enrichModel(entry.model)` | 1 红 — **精确红在** `aUserOverrideStillWinsOverTheRegistry`：`expected:<false> but was:<true>`（注册表盖掉了用户的 `supportsReasoning = false`） |
| m3（丢掉 override） | `entry.baseModel` | 2 红 |

**（b）仓库内源码形状测试**（真 kotlinc 2.0.21 + JUnitCore 4.13.2）：

| 树 | 结果 |
|---|---|
| 本支树 | `OK (6 tests)` |
| **PRE（基线树 `f94413f5` 的四个生产文件）** | `Tests run: 6, Failures: 3` — 精确红在 `everyProviderConstructionSitePassesAnEnrichedModel` / `theNewChatDefaultModelSitePublishesTheModelItResolved` / `theLiveExitIsVisibleToTheExtensionReaders` |

**（c）不可 JVM 化的一半**：`ProviderFactory.create` 与 `:modelservice` 之间的 IPC 行为
（图片降级、effort 夹取）需要真实 worker 进程 → 沙箱无 Android 运行时，
以**门带牙 + 源码形状测试**替代（理由同上）。

### L4 CI

- run `39894373704`（event=workflow_dispatch，attempt 1）**success**；
  `head_sha = 3d3bf5c1ddd80d21a65fe6fee32b817178449e2d`（= 本地 HEAD = `git ls-remote`，三方逐字符一致）；
  job `120221268875`：26 步 `not-ok=[]`，唯一 skipped = `Publish to Releases`（main 门控，分支构建不放行）。
- 日志判读 `judge_generic.py`：**ALL GENERIC CRITERIA GREEN**（18 项）
  —— `Summary: 109 passed, 0 failed` 恰一条、8 条 Summary 全部 0 failed ·
  门头 `[1/109]..[109/109]` 连续且 109 个门名互异 · `ALL 336 CASES PASS` ·
  `Z e: `（kotlin 编译错）0 · 游离 FAILED 0 ·
  本门真树 OK 行 + 两门 selftest PASS 行在册 ·
  **新测试 6/6 逐条 PASSED**（log :12595-12605）。
- 判读器自伤（同族第 N 次，判据取真样本前先看形态）：
  ①`Summary:` 有 8 条（各子套件自己的计数），判「恰一条」的锚是 **`109 passed, 0 failed`** 而非「唯一一条 Summary」；
  ②`e: ` 裸串误中 runner banner（`Image Release: https://…`），真形态是 **`Z e: `**；
  ③**Publish 的 skipped 结论在 steps API，不在日志文本**（skip 的步骤日志里没有行）。

## §6 任务书失真

| 条 | 任务书/TRIAGE 的说法 | 真树实测 | 处置 |
|---|---|---|---|
| 1 | TRIAGE 行 22 列 create 点 8 处（含 `ChatViewModel.kt:2702` 传 `cachedProvider.model`） | `ui/chat` 下 `ProviderFactory.create` 实测 **9 处**（8 + ChatViewModel 路由热重建），分列 4 个文件 | 门按**三文件 8 点**判（`ChatViewModel` 那处是「同 id 才换 live 模型」的特例，另由形状测试覆盖）；报告按 9 处记 |
| 2 | TRIAGE 行 23 `ChatViewModel.kt:1992/2020` 裸读 `currentModel` | 实测 `:1993` `currentModelSupportsReasoning`、`:2020` `currentModelMaxThinkingLevel`（兜底分支） | 实际行号与 TRIAGE 差 1；已一并接单源 |
| 3 | TRIAGE 行 23 `ChatErrorHandling.kt:94` / `ChatSlashTokenExt.kt:44` | 实测一致（`:94` / `:44`） | 无失真 |
| 4 | 任务书暗示「4 读者」 | dv23-B07 的 4 个读者全在 `ChatViewModel` **类内**；另有 2 个类内读者（thinking toggle / thinking ceiling）**根本没被那轮覆盖** | 扩大到 6 个类内 + 2 个类外 |
| 5 | 任务书未提 `ChatViewModel.kt:2702` 的 `cachedProvider.model` | 该点传的是**缓存 provider 自己的模型**，不是 entry 的；直接换成 live 会破坏「同模型热重建」语义 | 改为「live 模型 id 相同才用 live，否则沿用缓存模型」+ 打了 `ponytail` |

## §7 未做 / 残留（全带触发条件）

- **R1｜真机行为级欠账（P1 验证面）**：①选一个注册表声明 `image` 输入的模型，附图发送 —— 附件应真进多模态请求，不再退化成 `[attached image: …]`；②选一个注册表声明 `MAX` effort 档位的模型，选 `MAX` 后看 `:modelservice` 日志里的实际 `reasoning_effort` —— 不应被夹回 `XHIGH`；③无默认组新建会话 → 会话 `model_id` 应为真实 id 而非 `unknown`，Token Usage 面板应出现 Thinking 块。**触发条件** = 下次真机窗口。**触发后方案** = 三项一次开齐（读 `minis-*.modelservice.log` 的 REQ 行；读会话行 `model_id`；Token Usage 面板截图）。**搁置理由** = 沙箱无 Android 运行时与 worker 进程。
- **R2｜`:modelervice` 侧仍不初始化 `ModelsDevApi`（P2）**：本支把富化推到 UI 侧构造点，但 worker 侧依旧只看序列化后的 `LLMModel`。若将来出现「不经 UI 构造 provider」的路径（headless / offload），那条路径仍会是裸模型。**触发条件** = 出现新的 `ProviderFactory.create` 调用点（不在 `ui/chat`）或 headless 路径构造 provider。**触发后方案** = 把富化下沉到 worker 侧（需要 worker 侧可读注册表 = 新能力，单独立项）。**搁置理由** = 本支半径在 `ui/chat`；worker 侧富化 = 新抽象层。
- **R3｜`CompactOrchestration.kt:957` / `ModelExecutionService.kt:948,1381` 仍传裸模型（P2）**：仓内另有 3 处 `ProviderFactory.create`（压缩编排 + offload 执行服务）未接单源。**触发条件** = 压缩/离屏执行出现同样的能力降级症状。**触发后方案** = 同法接 `effectiveModel`。**搁置理由** = 不在本支任务书列出的文件内（半径纪律）。
- **R4｜富化结果的缓存成本（P3）**：`providerModelFor` 每次调用都走一次注册表查表（内存 map + `copy`），无按 `config.revision` 的记忆化。**触发条件** = 数百 entry 的配置下切换模型出现可感卡顿。**触发后方案** = 按 `(revision, entry.uuid)` 记忆化。**搁置理由** = 无实测卡顿，且新增缓存层违反本批母题。
- **R5｜`liveEffectiveModel` 可见性放宽为 `internal`（P3）**：同包内任意代码都能调用，理论上存在第二个「读者」再次各读各副本的可能。**触发条件** = `internal` 可见范围内出现新的能力读者。**触发后方案** = 给门加「`liveEffectiveModel` 调用点数 == 8」的守恒判据。**搁置理由** = 当前 8 个读者全部在册且由门钉住。
- **R6｜B-03 门只覆盖 `ui/chat` 三个文件（P3）**：`ChatViewModel.kt` 的第 9 处走的是「同 id 才替换」特例，门的调用点计数不含它。**触发条件** = 未来 `ChatViewModel` 内新增 create 点。**触发后方案** = 把它并入门的三文件清单。**搁置理由** = 它的语义（热重建保持同模型）与另外 8 处不同，混进同一条判据会产生假红。

## §8 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f94413f52fa5403c903902f6b1d2ae10cb9bc906` |
| 本地 HEAD | `3d3bf5c1ddd80d21a65fe6fee32b817178449e2d` |
| `git ls-remote origin` | `3d3bf5c1ddd80d21a65fe6fee32b817178449e2d` |
| CI `head_sha` | `3d3bf5c1ddd80d21a65fe6fee32b817178449e2d`（三方逐字符一致） |
| 分支 | `fix/dv24-b2-chat-factory-effective` |
| CI run id | `39894373704`（job `120221268875`，attempt 1，success；26 步 not-ok=[]，唯一 skipped = Publish to Releases = main 门控） |
| 日志 | `shared/work/deep-audit-1044/w8-logs/job-39894373704.log`（1,393,758 B）；判读 `probe/ci/judge_generic.py` = ALL GENERIC CRITERIA GREEN |

### 提交链（4 提交，分行写清）

1. `0e335456` — **生产**：6 个生产文件
2. `ba3611ef` — **测试**：`ChatProviderModelSourceTest.kt`
3. `3d3bf5c1` — **门**：`models_provider_field_check.py` + `session_provider_enabled_check.py`
   → **CI 绿于此提交**（run `39894373704`）
4. `5c32285c` — **报告**：`fix-reports/dv24-FIX-8.md`（**不叫 `FIX-8.md`**：main 已跟踪
   `fix-reports/FIX-8.md` = dv23/1043 波的同号报告，覆盖会抹掉那一波的记录）

**报告回填后源码树等价**（1042/1043 判据）：
`git diff --name-only 3d3bf5c1 HEAD -- . ':(exclude)fix-reports'` → **空**；
`fix-reports/` 不在 `build-apk.yml` 的 paths 内、不参与构建 ⇒ CI 结论对当前树成立，不必重跑。

# FIX-4 · deep-audit-1044（v23 波）A-05 P1 — `session.*` 配置写入写穿驻留 ChatViewModel

- **分支**：`fix/dv24-a4-config-session-writethrough`
- **基线**：main `f94413f52fa5403c903902f6b1d2ae10cb9bc906`
- **停止节点**：分支 CI 绿（**不合并 main**）
- **门**：扩 `scripts/scan/provider_rpc_symmetry_check.py`（C18，0 新建门；`scan.sh` / `test_scan.py` 零触碰）· 门 109 / 夹具 336 **不变**

---

## §1 缺陷与根因

### 症状原话（报告 A-05）

Agent 跑 `minis-config set session.thinkingLevel high` / `session.primaryModel entry:<id>`，
确认弹窗点「允许」返回成功，**但后续对话仍用旧模型/旧档位**；而且用户随后在应用内点选
模型/档位时，会把内存里的旧状态**写回 DB**，把这次配置写入覆盖掉。

### 根因假设

`ConfigBuiltins` 的两个 `session.*` writer 只写 SQLite，**没有任何刷新驻留 VM 的路径**；
而 `ChatViewModel` 只在 `loadSession()` 里**一次性**读 `thinkingOverride` / `modelBinding`
（`suspend getSession`，不是响应式 Flow），`ChatViewModelStore` 又全进程缓存实例 ⇒
配置写入对「正开着的聊天」不可见，且该实例的后续写回会覆盖行。

### 证据（真树实测）

| 证据 | 位置（基线 `f94413f5`） |
|---|---|
| thinking writer 只 `runBlocking { dao.updateThinkingOverride(sid, level.name) }` | `ConfigBuiltins.kt:581`（`git show f94413f5:…` 实测） |
| binding writer 两分支同样只写 DAO（`updateSessionBinding(sid,"","")` / `(sid,bindingJson,modelId)`） | `ConfigBuiltins.kt:604-646` |
| `ConfigBuiltins.kt` 全文 `ChatViewModelStore` / `peekViewModel` / `existingViewModelFor` **0 命中** | `grep -c` = 0 |
| `ChatViewModel` 只在 `loadSession()` 读一次：`_thinkingLevel.value = persistedOverride` / `restoreFromBinding(session.modelBinding)` | `ChatSessionLifecycle.kt:389` / `:405` |
| `ChatViewModelStore` 进程级缓存（`stores` map + `ownerFor`） | `ChatViewModelStore.kt:37` / `:72` |
| 门侧无任何判据约束「config 面写入是否到达 VM」 | 109 门全绿而缺陷存在 |

**草稿会话更糟**：`__new__<uuid>` 尚未 `ensureSession()` 时 DB 无该行 ⇒ writer 的
`UPDATE … WHERE id = sid` **影响 0 行**，写入蒸发；reader 永远读默认 `off` / `""`。

### 任务书两处失真（详见 §6）

- 报告称 `ui/sessions/SessionEditSheet.kt:110-145` 通过 `ChatViewModelStore.existingViewModelFor`
  刷新驻留 VM —— **该 API 与文件都不存在**（`SessionEditSheet` 在
  `ui/sessions/SessionsShared.kt:197`）。
- 报告称 `HeadlessChatRunner.applyModelOverride:112-160` 同时刷新驻留 VM —— 真树
  `:120-167` **也只写 DB**。

⇒ 本支是这一族的第一个真修复，取驻留 VM 的路径是自建的。

---

## §2 修法（停在阶梯第 6 级：接既有单源 + 复用既有 VM 入口）

### 根因的一句话

> 「写入只到达行，而用户看的是 VM」——两半都在，缺的是「写完之后把值推进驻留 VM」这一步，
> 且必须走 VM 自己的入口（不是新状态路径）。

### 方案（4 个生产文件）

1. **`ChatViewModelStore.peekViewModel(sessionId): ChatViewModel?`**（`ChatViewModelStore.kt:81-108`）
   —— **只读**查找：走 `stores[resolveKey(sid)]`，遍历 `store.keys()` 取 `ChatViewModel`，
   取不到返回 null。**不用 `ownerFor`**：它内部 `getOrPut` 会为一个不在屏上的会话**分配
   ViewModelStore**（泄漏），而 `ViewModelProvider` 还会顺手创建一个完整 ChatViewModel。
   这是本支对 `ChatViewModelStore` 的**唯一**改动（任务书允许的「一个只读查找 API」）。
   `@Synchronized` 与既有 `ownerOf` / `release` 同锁。
2. **`config/SessionConfigWriteThrough.kt`（新文件，95 行）** —— Android 无关的
   **决策 + 投递契约**：
   - `Route{ResidentViewModel, RowOnly, DraftWithoutRow}` + `route(hasLiveViewModel, hasPersistedRow)`；
   - `deliver(vm, persist, hasPersistedRow, refresh, onDraftWithoutRow)`：**先 persist（行），再 refresh（VM）**，
     且 `hasPersistedRow` 是 **suspend lambda**——必须在写**之后**求值，「UPDATE 匹配 0 行」正是草稿情形，
     只有行能回答；草稿且无 VM ⇒ `onDraftWithoutRow()` 让调用方记日志（原实现把这种情况当成功）。
   - 抽成**独立文件**而非 `ConfigBuiltins` 内的私有函数，是为了让决策能在**纯 JVM** 里编译与验证
     （`ConfigBuiltins` 依赖 SharedPreferences/Context/Room，无法单测）。同形态先例：
     `ClipboardClearAction` / `SendDisposition`。
3. **`ConfigBuiltins` 两个 writer** —— 都改走 `writeSessionConfig(sessionId, path, chatRepo, persist, refresh)`：
   - 在 **Main** 上 `peekViewModel`（`runOnMainThread`，主线程直通避免 `runBlocking` 自锁）；
   - `persist` = 原来的 DAO 调用（原样保留）；
   - `refresh` = VM 自己的入口：thinking → `setThinkingLevel(level)`；binding 设置 →
     `if (!restoreFromBinding(bindingJson)) applyDefaultModelFallback()`；binding 清空 →
     `applyDefaultModelFallback()`。
   - VM 侧入口 throw 被 `runCatching` 包住并记 warn：**行已经落盘，不能因为刷新失败就报写入失败**。
4. **`ChatModelRouting.applyDefaultModelFallback()`（+30 行）** —— 清空 binding 必须跑
   `loadSession()` 的**优先级 3**，即「默认组 → 新聊天默认」。既有 `applyNewChatDefaultModel()`
   **跳过了默认组**这一步；照它抄会把「清空后应回到默认组」错变成「回到上次用过的 entry」。
   这是唯一的共享回落入口（设置分支的 binding 不可用回落也用它）。

### 被否决的方案

| 方案 | 否决理由 |
|---|---|
| **重建 VM**（`release(sid)` 让下次重新 `loadSession()`） | 会杀掉正在跑的 agent loop / 丢弃未落盘的流式内容；且 `loadSession()` 是重活，配置写变成 O(全历史) |
| **在 `ChatViewModel` 里加 `refreshSessionConfig()`** | `ChatViewModel.kt` 归支 7 / 支 8，半径外；且那会变成第二条状态路径 |
| **`ChatViewModelStore` 里做 `applySessionConfig(sid, …)`** | 任务书只允许在 store 里加**一个只读查找**；把 VM 写入塞进 store 会让 store 同时承担「查找」与「状态改写」两种职责 |
| **`peekViewModel` 用 `ViewModelProvider(owner, throwingFactory)`** | 依赖「store 命中时不会调用 factory」这一框架实现细节，且要伪造 factory；`keys()` 是公开 API，直接可读 |
| **`loadSession()` 改成响应式 Flow**  | 把「一次读」改成「订阅」是架构改动（几十处消费方），远超 P1 半径 |
| **清空 binding 用 `applyNewChatDefaultModel()`** | 跳过默认组（见上），会让「清空」落到与下次加载不同的模型 |

### 硬约束检查

- **终止路径**：VM 刷新 exception → `runCatching` + warn，不吞掉「行已写」这个事实（配置写仍成功）。
- **主线程纪律**：VM 状态写入全部在 `Dispatchers.Main`（`runOnMainThread`），主线程时走直通。
- **不引入**：不创建 VM（`peekViewModel` 有门禁钉死）、不把富化结果写持久化（写的是原值）、
  后台线程改 VM 状态（全部 hop 到 Main）。
- **必须保住**：`sessionIdProvider() == null` → `InvalidValue("No active session — open a chat first")`
  （两个 writer 都保留，门 C18 有牙臂 F54 钉死）；非法 token / 未知 entry / 空组 → `InvalidValue` 原样；
  `ConfigConfirmationGate` 弹窗机制零改动（写 writer 之外的层）。

---

## §3 改动清单

| 文件 | 增/删 | 改动的行区间（新文件行号） |
|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/config/ConfigBuiltins.kt` | +132 / −5 | imports `:3`, `:19-26`；thinking writer `:591-606`；binding 清空分支 `:626-641`；binding 设置分支 `:667-685`；`writeSessionConfig` + `runOnMainThread` + `TAG` `:690-785` |
| `src/android/app/src/main/java/com/rikkaminis/app/config/SessionConfigWriteThrough.kt` | **新增 +95** | 全文 |
| `src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatViewModelStore.kt` | +28 / −0 | `:81-108`（`peekViewModel`，紧随 `ownerFor` 之后） |
| `src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatModelRouting.kt` | +30 / −0 | `:178-207`（`applyDefaultModelFallback`，在 `switchModelAndRerun` 之前） |
| `src/android/app/src/test/java/com/rikkaminis/app/config/SessionConfigTargetTest.kt` | **新增 +114** | 全文 |
| `src/android/app/src/test/java/com/rikkaminis/app/config/SessionConfigWriteThroughTest.kt` | **新增 +260** | 全文 |
| `scripts/scan/provider_rpc_symmetry_check.py` | +340 / −5 | docstring C18 段 `:66-86`；常量 `:120-152`；`session_writer_block` / `check_session_write_through` `:470-600`；`check_tree` C18 接线 `:1000-1020` 与 rc 键表 `:1040-1042`；`main()` 打印 `:2540`；夹具 `GOOD_VM_STORE` `:1494-1512` + `GOOD_CONFIG_BUILTINS` 的 `registerSession` `:1571-1630`；selftest F46..F55 `:2380-2470` |

**合计：7 文件 +999 / −10**（生产 285 行 / 测试 374 行 / 门 340 行）。
`scan.sh` / `test_scan.py` **零触碰**；门 **109** / 夹具 **336** 不变。

---

## §4 门判据与带牙证明

### C18（`provider_rpc_symmetry_check.py`）

> 判据锚**决定**，不锚拼写：判「这个 writer 有没有（DAO 写 + 到达驻留 VM 的刷新）两半」，
> 不锚具体函数名（刷新入口是**家族**：`setThinkingLevel` / `restoreFromBinding` /
> `applyDefaultModelFallback` / `applyNewChatDefaultModel` / `selectEntry` / `selectGroup` /
> `selectGroupEntry`；改族内名字不会让判据失明，删掉族内所有入口才红）。

| 臂 | 条件 | 判据 |
|---|---|---|
| **A** | `session.thinkingLevel` writer 块 | 必须含 `updateThinkingOverride(` **且** 刷新家族 token |
| **B** | `session.primaryModel` writer 块，按 `return@ClosureField` 切成 clear / set **两支**，各自判 | 两支各自必须含 `updateSessionBinding(` **且** 刷新家族 token；切界本身丢失（`return@ClosureField` 不是恰好 1 个）同样红 |
| **C** | 两个 writer 块 | 必须保留 `No active session` 拒绝契约 |
| **D** | 刷新是否**经非创建型查找** | writer 块内 `peekViewModel(`，或它调用的**同文件函数**（一层传递闭包）内含 `peekViewModel(`；否则红 |
| **E** | `ChatViewModelStore.peekViewModel` 函数体 | 不得含 `getOrPut(` / `ViewModelProvider` / `.put(`（能创建的查找不是 peek）；必须含 `stores[`（真的在读既有 store map）；函数缺失 = fail-closed 红 |

### 带牙臂（全部「恰好红该判据」，其余判据保持绿）

| 臂 | 改什么 | 红在哪条 |
|---|---|---|
| F46 | 完整夹具 | **绿**（0 命中，证明判据不误报） |
| F47-thinking | 删 thinking 的 `refresh` 行 | A 的刷新半 |
| F47-clear | 删 binding **清空支**的 `refresh` | B[clear] 的刷新半（另一支仍绿） |
| F47-set | 删 binding **设置支**的 `refresh` | B[set] 的刷新半（另一支仍绿） |
| F50 | 删 thinking 的 `persist` 行（只刷新不写行） | A 的 DAO 半 |
| F51 | 两个 writer 回退到「纯 DAO 基线形态」 | A / B[clear] / B[set] **同时**红（≥3 条） |
| F52 | `peekViewModel` 改成 `stores.getOrPut(…)` | E：能创建的查找 |
| F53 | 重命名 `peekViewModel`（API 消失） | **fail-closed**（不是静默绿） |
| F54 | 删 thinking writer 的 `No active session` | C |
| F55 | 把 F51 的基线形态写进真实夹具树，走 `check_tree` | A/B 红且**只有 C18**（证明定位 + 读取链也接通，不只是判据函数对） |

**缺陷面（PRE）**：基线树独立 worktree（`/tmp/w4-base` @ `f94413f5`）跑新门 →
正好 3 条 C18 + 1 条 fail-closed：

```
[C18] ConfigBuiltins.kt: thinkingLevel writer never reaches the resident ChatViewModel …
[C18] ConfigBuiltins.kt: primaryModel[clear] writer never reaches the resident ChatViewModel …
[C18] ConfigBuiltins.kt: primaryModel[set] writer never reaches the resident ChatViewModel …
[FAIL_CLOSED] ChatViewModelStore.kt: peekViewModel not found
```

---

## §5 验证四层

### L1 门（本门真树 + selftest）

| 项 | 结果 |
|---|---|
| `python3 scripts/scan/provider_rpc_symmetry_check.py .` | `ALL GREEN (… C18 session config write-through to the resident view model)`，rc=0 |
| `--selftest` | **56 臂** `PASS 56 / FAIL 0`，rc=0（原 46 臂 + C18 的 10 臂） |
| 基线树（PRE） | 3 条 C18 + 1 条 fail-closed（见 §4） |
| 全量 `scan.sh` | **109 passed / 0 failed**（分 4 块跑，`[1/109]`..`[109/109]` 109 个门头互异；分块原因见 §7 R7） |
| `test_scan.py` | **ALL 336 CASES PASS**（rc=0，`✅` 337 行） |

### L2 邻门（按概念反查，不照抄任务书建议名）

`grep -rln "ConfigBuiltins\|ChatViewModelStore" scripts/scan/` 反查出真实邻居，跑 3 道：

| 门 | rc |
|---|---|
| `envvar_config_group_check.py`（config 面同名文件） | 0 |
| `chat_session_id_binding_check.py`（session id 解析链） | 0 |
| `memory_gate_provider_fields_check.py`（config 面字段可见性） | 0 |

### L3 真跑（真 `kotlinc` 2.0.21 + 真 JUnitCore）

**装置 A · 纯决策（真对象真跑）**：`SessionConfigWriteThrough.kt` 是 Android 无关的，
直接编译（只 stub `ThinkingLevel` 一个 8 值 enum）+ 真测试文件。

| 臂 | 结果 |
|---|---|
| **PRE**（基线：文件不存在） | **编译红**，`unresolved reference` 打在 `SessionConfigWriteThrough` 上 = 缺陷点（那时根本没有任何写穿决策） |
| **POST**（shipped） | `OK (8 tests)` |
| **M1**（行优先于 VM，即基线的实际语义） | **恰 2 红**：`a live view model means the write has to reach it` + `the row is written before the live chat is refreshed` |
| **M2**（一律 `DraftWithoutRow`，写入永远蒸发） | **恰 5 红**（4 条 route 断言 + 交付顺序） |
| **M3**（先读行再 persist，状态可能领先于行） | **恰 1 红**：`the row is written before the live chat is refreshed`（实测 order `[row-read, persist, refresh]`） |
| **M4**（草稿静默接受） | **恰 1 红**：`a draft without a row is reported instead of silently accepted` |

`ALL L3 ARMS MATCH`。

**装置 B · 源码形状（真树 + 变异副本）**：`SessionConfigWriteThroughTest.kt` 读真源码，
用 `-Dprobe.repo=` 指向变异骨架树。

| 臂 | 结果 |
|---|---|
| **POST**（本支树） | `OK (6 tests)` |
| **PRE**（基线树 `/tmp/w4-base`） | **恰 6 红**（两个 writer 缺刷新半 / 缺 peek / 两个负控前提不成立） |
| **M1**（删 thinking 刷新） | 恰 1 红 |
| **M2**（删 binding 清空支刷新） | 恰 2 红（该支断言 + 负控前提） |
| **M3**（`peekViewModel` 改成 `getOrPut`） | 恰 2 红 |
| **M4**（writer 不再经 peek） | 恰 1 红（「必须经非创建型查找」） |

`ALL SHAPE ARMS MATCH`。

### L4 CI

见 §8 身份链（推分支后补）。

---

## §6 任务书失真

1. **`ui/sessions/SessionEditSheet.kt:110-145` 通过 `ChatViewModelStore.existingViewModelFor(session.id)` 刷新驻留 VM** ——
   **文件与 API 都不存在**。真实位置 `ui/sessions/SessionsShared.kt:197`（`SessionEditSheet`），
   全文 `existingViewModelFor` 0 命中；`ui/sessions/**` 里也没有任何刷新驻留 VM 的路径。
2. **`HeadlessChatRunner.applyModelOverride:112-160` 同时刷新驻留 VM** —— 真树 `:120-167`
   **只写 DB**（`updateSessionBinding`），不碰 VM。任务书 §3 的「报告失真」段已指出，本支复核一致。
3. **`HeadlessChatRunner.kt` 真有一处「刷新 VM」的写法**，但不是 `applyModelOverride`：
   `HeadlessChatRunner.kt:820-830`（`vm.selectEntry(entryId)`）与 `ChatMutationMethods.kt:45-60`
   才是「取驻留 VM 并操作」的既有模板。任务书给的模板段号（`:59-70`）落在 `ChatViewModelStore.ownerFor`
   的调用上而不是 `applyModelOverride` 里；按真树重新定位后使用。
4. **「`ChatViewModelStore.kt:72` `fun ownerFor`」正确**，但注释「注意：会创建 store」正是本支**不能**用它的原因
   （任务书已提示，未失真，记为保证锚点一致）。
5. **`setThinkingLevel` 确实存在**（`ui/chat/ChatSlashTokenExt.kt:255`，`fun ChatViewModel.setThinkingLevel`），
   任务书说「真树没有 `setThinkingLevel` 这个函数名」**失真**——它就在 `ChatSlashTokenExt.kt`，
   只是不是 `ChatViewModel` 的成员而是扩展函数（`grep "fun ChatViewModel.setThinkingLevel"` 才命中）。
6. **`ui/chat/ChatModelRouting.kt` 的 `selectEntry` / `selectGroup` / `selectGroupEntry` 可见性**：
   三者都是 `fun ChatViewModel.xxx`（public 扩展），可用；但本支最终没走 `select*`——它们会把
   「一次模型切换」的 UI 副作用（`switchModelAndRerun`、`lastUsedEntryId` 推进）也带进来，
   而配置写入要的是「等价于下次加载」。改用 `restoreFromBinding`（loadSession 的同一条路径）。
7. **「`+ 若确有必要，`ui/chat/ChatViewModelStore.kt` 只允许新增一个只读查找 API」** —— 实际还动了
   `ui/chat/ChatModelRouting.kt`（+30 行，`applyDefaultModelFallback`）。原因：清空 binding 必须跑
   优先级 3（默认组 → 新聊天默认），既有 `applyNewChatDefaultModel()` **跳过默认组**；把它塞进
   `ConfigBuiltins` 会变成配置层私有副本（同一决定两套实现）。**这是对任务书文件清单的唯一扩写**，
   已在本报告显式登记，请主会话裁决是否需要拆出。

---

## §7 未做 / 残留（每条带触发条件）

| # | 残留 | 触发条件 | 触发后方案 | 为什么现在不做 |
|---|---|---|---|---|
| **R1** | **真机行为级**：终端跑 `minis-config set session.thinkingLevel high` 后立即发消息，抓请求体 `reasoning` 参数变化；`session.primaryModel entry:<id>` 后看导航栏 provider·model 行立即变色；清空后回默认组 | 下一次真机窗口 | 三项一次开齐，按 `§1044-VERIFY` 汇总 | 沙箱无 Android 运行时，VM 状态只能靠源码形状 + 纯决策钉 |
| **R2** | **reader 仍只读 Room**：草稿会话（无行）写后立刻 `get session.thinkingLevel` 读回旧值（写入本身已在 VM 内存生效，`ensureSession()` 时折入行） | 用户/agent 在草稿会话上「先 set 后 get」看到旧值 | reader 加「无行时回落询问驻留 VM」分支 | 半径纪律：本单缺陷在 writer；reader 改会动 4 个 reader 的语义，且草稿窗口极短 |
| **R3** | **`setThinkingLevel` 的全局副作用**：它会写 `ThinkingGlobalPrefs.setLastLevel`，即「跨会话 last-tuned 档位」。于是 `minis-config set session.thinkingLevel` 也会改变**新对话**的默认档位 | 用户抱怨「我只改了这一个会话，新对话也变了」 | 给 config 路径换成不写全局的入口（或在 `setThinkingLevel` 上加 `rememberGlobally: Boolean = true`） | 该副作用是**既有 UI 语义**（`ChatSlashTokenExt.kt:288` KDoc 明说只有两个显式用户动作会走到这里），配置写入属同一语义；改它要动 UI 手选路径，半径外 |
| **R4** | **`setThinkingLevel` 在非 reasoning 模型上拒绝**（`appendSystemInfo` + return）：此时行已写 `high`、VM 仍是旧值，UI 多一条系统提示。下次 `loadSession()` 会把 `high` 读回来，但 `effectiveThinkingLevel` 会按模型能力折成 OFF | 非 reasoning 模型 + 配置写 thinkingLevel | 在 writer 侧先查 `currentModelSupportsReasoning`，不支持时抛 `InvalidValue` 而不是写行 | 与 UI 现有语义一致（UI 也拒绝并提示）；改判据要先定「配置语义 = 请求值还是生效值」 |
| **R5** | `peekViewModel` 与 `refresh` 之间仍有 TOCTOU：peek 到 VM 后、Main 上执行 refresh 前，VM 可能已被 `release()` | 极端并发（删会话与配置写同时） | 在 `runOnMainThread` 内**先 peek 再 refresh**（本支已是同一 lambda 内的一步，实际窗口 = 0） | 本支实现已在**同一个 Main 块**内 peek+refresh，此条仅记录有心跳的语义：peek 到的是调用那一刻的实例 |
| **R6** | **`ChatViewModelStore.peekViewModel` 的 `keys()` 依赖**：`ViewModelStore.keys()` 是 androidx.lifecycle 2.8.7 的公开 API（本仓已锁 2.8.7），未来升级若移除会编译失败 | 升级 lifecycle 版本 | 改用 `ViewModelProvider(owner, throwingFactory)` 或给 store 自己加 key 记录 | 2.8.7 上工作正常，且编译期会立刻报错（不是静默失效） |
| **R7** | **沙箱假红**：本机 `sh scripts/scan/scan.sh` 整链在 gate 26/109 被 SIGTERM，`test_scan.py` 两次因 `Errno 38` 中断（registry **S28**）。最终按 4 分块 + 空档重跑得 109/0 与 ALL 336 | 同一时段多个重活并发 | 用 `/var/minis/shared/tools/gate_run.sh`（flock + ENOSYS 重试）或收口前独占窗口重跑 | 已登记为沙箱缺陷，与代码无关；分支 CI 才是权威证据 |
| **R8** | **`provider_rpc_symmetry_check.py` 既有潜伏缺口**：`cfg_code` 在 `ConfigBuiltins.kt` 找不到时未赋值，C11 会抛 `UnboundLocalError`（本机一次 ENOSYS 干扰下暴露）；正确行为应是 fail-closed 报告 | `ConfigBuiltins.kt` 缺失/重复树（如夹具缺文件） | 在 C8 分支里 `cfg_code = ""` 初始化，C11 走 fail-closed | P2：只在「门找不到文件」时可达，与本次改动无关，半径外（登记 backlog） |
| **R9** | 未把 `SessionConfigWriteThroughTest` 的变异臂入库（装置在 `probe/`，`probe/` 被 `.gitignore:141` 覆盖） | 需要复现某条臂 | 从 `probe/l3_arms.py` / `probe/l3_shape.py` 重跑 | 本批装置惯例（批次档案留在 `shared/work/deep-audit-1044/`） |

---

## §8 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f94413f52fa5403c903902f6b1d2ae10cb9bc906` |
| 分支 | `fix/dv24-a4-config-session-writethrough` |
| 提交 | `b7979890` 生产 · `0f5897e7` 测试 · `2451a671` 门 · 报告（本文件） |
| 本地 HEAD | 见回报（报告提交后） |
| `git ls-remote origin <branch>` | 见回报 |
| CI `run.head_sha` | 见回报 |
| CI run / conclusion | 见回报 |

**判读口径**（COMMON §4）：`conclusion == success` **且** 三方 `head_sha` 逐字符一致；
`Summary:` 行带框线前缀；skip 与否看 steps API 不在日志文本里 grep。

---

## §9 装置清单（`probe/`，worktree 私有，不入库）

| 文件 | 作用 |
|---|---|
| `probe/gate_c18_edit.py` · `gate_c18_edit2.py` · `gate_c18_fix.py` · `gate_c18_arms.py` | 门编辑脚本（每处 `assert count == 1`，静默漏改即失败） |
| `probe/scan_chunks.py` + `/tmp/w4_chunk_*.sh` | 把冻结的 `scan.sh` 切成 4 段（只为绕开 S28 SIGTERM，不改仓内文件） |
| `probe/l3b/` | 纯决策装置：真 `SessionConfigWriteThrough.kt` + `ThinkingLevel` stub + 真测试 |
| `probe/l3_arms.py` | 纯决策五臂（PRE / POST / M1..M4） |
| `probe/l3_shape.py` | 源码形状六臂（POST / PRE / M1..M4，`-Dprobe.repo=` 指向变异骨架树） |
| `/tmp/w4-base` | 基线 worktree（PRE 臂的 ground truth） |

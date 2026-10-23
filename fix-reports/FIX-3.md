# FIX-3 · deep-audit-1045（v24 波）· A-05 + A-06：RPC 模型覆盖把已 pin 的会话重新绑定 + `session.*` 读写面不对称

**分支** `fix/dv25-a3-headless-session-config`（基线 main `1d44ec90`），单提交 `df76515e`；
6 文件 **+890/−56**（生产 4 / 测试 2 新增）；**门 109 → 109、夹具 336 → 336**（0 新建门，扩
`chat_session_id_binding_check.py` criterion 16 + `provider_rpc_symmetry_check.py` C19）；
`scan.sh` / `test_scan.py` 冻结面零触碰。**CI #2546 / run `39917174738` / job `120293133939`
attempt 1 success**。未合并 main（含 `.kt` 验证面，起手权在用户）。

---

## §1 缺陷与根因

### A-05（P1）：`chat.prompt` / `chat.retry` 不带模型时把已绑定的会话重绑到「默认组」

**症状原话**：用户在一个会话里手动选了一个模型（或组），下一次通过 RPC 发消息（`chat.prompt`
/ `chat.retry` 不带 `modelEntryId` / `modelGroupId`），会话静默回到默认主组；界面上模型名
有时还停在旧值上（写只到行、没到驻留 VM）。

**根因（一句话）**：「没有指定模型」被当成「还没选模型 ⇒ 套新建会话默认组」，而
「新建会话默认」是**新建**语义，不是**覆盖**语义。

**证据（基线 blob `1d44ec90`，行号与任务书一致）**：

- `debug/HeadlessChatRunner.kt:126-131`（基线）：
  ```kotlin
  val resolvedGroupId = modelGroupId ?: run {
      if (modelEntryId != null) return@run null
      cfg.defaultPrimaryGroupId?.takeIf { gid -> cfg.modelGroups.any { it.id == gid } }
  }
  ```
  两个参数都为 null（= `chat.prompt` / `chat.retry` 未带模型时的**常态**）⇒ 直接落到
  `defaultPrimaryGroupId`，**全程没有读会话自身的 `model_binding`**。
- `debug/ChatMutationMethods.kt:158`（基线注释）：`// Apply model override (per-call only;
  iOS docs say session binding is untouched — but the existing ChatViewModel doesn't expose a
  per-call override, so we update + restore around the call.)` —— 调用方明确声明「每轮都传」，
  与「绑定是会话级」的事实冲突，注释本身就是缺陷的自白。
- 同函数 `:150` 只写 `app.chatRepository.updateSessionBinding(...)`，全文件 `peekViewModel`
  0 命中 ⇒ 行写了、屏上没变（1044-A05 已经把这条通道建好，本支只是没接）。

### A-06（P1）：`session.*` 的读面只读行、写面写两处

**症状原话**：`minis-config set session.thinkingLevel high` 之后 `get` 回 `off`；对一个
草稿会话（没有行）设模型，读回永远是空串；`set session.primaryModel entry:<禁用实例的
entry>` 报成功，会话实际用的却是别的模型。

**根因**：写入是双面（Room 行 + 驻留 `ChatViewModel`，`SessionConfigWriteThrough` 的
`route` 三岔），读取是单面（只 `dao.getSession`）。

**证据（基线 blob）**：

- `config/ConfigBuiltins.kt:552-566`：`session.thinkingLevel` reader = `runBlocking {
  chatRepo.dao.getSession(sid) }` + `thinkingOverride`，全块 `peekViewModel` 0 命中。
- 同文件 `:600-609`：`session.primaryModel` reader = `formatBinding(session?.modelBinding)`，
  同样 0 命中。
- 写面（`:596` / `:670` → `writeSessionConfig`）走 `peekViewModel` + `restoreFromBinding`，
  与读面完全不同源。
- 可用性：entry 分支只判 `cfg.modelEntries.find`（存在性），group 分支只判
  `memberEntryIds.firstOrNull()`（存在性）——而运行时的单源判据
  `decideSessionModelResolution`（`ChatSessionLifecycle.kt:1090-1105`）会因
  `PROVIDER_DISABLED` / `MISSING_API_KEY` / `MISSING_INSTANCE` 拒绝同一条绑定 ⇒
  「配置报成功、会话拒绝恢复」。

### 门为什么没拦住

- `chat_session_id_binding_check.py` 的否定性判据只覆盖 `prompt` 的 sessionId 绑定与
  「丢弃发送」契约，没有覆盖 `applyModelOverride` 的**写语义**（谁该被写）。
- `provider_rpc_symmetry_check.py` C18（1044-A05 新增）只判**写**侧的「行 + 驻留 VM」合取，
  没有对应的**读**侧判据 —— 同一份对称要求只落了一半，这正是本缺陷的形态。

---

## §2 修法（阶梯第 2/6/7 级，逐条写明停在哪一级）

### A-05：停在**第 6 级「能一行」→ 第 2 级「代码库里已有」**

判据从「调用方传了什么」改成「会话行里有什么」，并把决定抽成纯函数（与 1044-A05 的
`route` 同文件同风格）：

- `config/SessionConfigWriteThrough.kt` 新增 `BindingTarget`（`Entry` / `Group` /
  `Preserve` / `None`）+ `resolveBindingTarget(requestedEntryId, requestedGroupId,
  existingBinding, defaultGroupId)`：显式请求恒胜 → 已有绑定 ⇒ `Preserve` → 默认组 ⇒ `Group`
  → 都没有 ⇒ `None`（空串/空白 `modelBinding` 视作无绑定，与「写空 = 清除 pin」的既有契约一致）。
- `HeadlessChatRunner.applyModelOverride` 改为：读行 → 调纯函数 → 分支执行。
  `Preserve` **不写**（写回同值是伪修复：会把「清除」与「保留」两件事搅在一起）。
- 行读取**内联在决策实参里**（`existingBinding = app.chatRepository.dao.getSession(sessionId)?.modelBinding`），
  这样「被读的值」与「被用的值」在文本上是同一个表达式 —— 门可以锚这条流，而不是锚一个
  可以被丢掉又留着的局部变量。
- 写入下沉 `persistBinding` → `SessionConfigWriteThrough.deliver(...)`（1044-A05 的同一通道，
  **未新写一套**）：先 `updateSessionBinding` 落行，再 `peekViewModel`（非创建型）刷新；
  刷新用 `restoreFromBinding`（`loadSession` 的同一个入口，天然带 enabled / credential 闸门），
  失败回落 `applyDefaultModelFallback`，异常只记日志（行已落盘，UI 落后一拍不该反报 RPC 失败）。
  草稿会话（无行、无 VM）记 `AppLogger.warning`，不再静默 0 行更新。

**否决**：① `modelGroupId == null && modelEntryId == null` 时**直接 return**（= 永远不套默认组：
新会话会保留 `ensureSession` 播种的「第一个可见 entry」，用户设的默认组失效）；
② 把 `Preserve` 也写回同值（掩盖「清除 pin」语义，且多余写）；③ 在 `ChatMutationMethods`
加 `isNewSession` 标志（调用方无法知道会话是不是上一轮 RPC 刚建的，且把单源判据换成信任
入参）。

### A-06：停在**第 2 级「代码库里已有」**

- 读：`config/SessionConfigWriteThrough.kt` 新增 `SessionConfigReadSource` +
  两个**分别**判定的纯函数（细则见下），`ConfigBuiltins` 两个 reader 按判定取值。
- 写：entry 分支前置 `requireUsableEntry` → `decideSessionModelResolution(entry, instance,
  loadApiKey(instance.id))`（**运行时同一个决定**），不 `Use` 就抛 `ConfigError.InvalidValue`
  并把人话原因附上（missing instance / disabled / no api key）；group 分支改成
  `firstUsableGroupMember(group)`：成员按「存在 + 可用」筛，全不可用就 fail-closed
  （`resolveProviderFromGroup` 同样会返回 false），`model_id` 取第一个**可用**成员
  （原来是第一个成员，可能正好是那个不可用的）。

### 两处关键设计（都是任务书没写的，且都能被单测钉住）

1. **读优先级不能两个字段一刀切**。
   - `session.thinkingLevel`：**驻留 VM 优先**（行是跨重启副本，write-through 之后两面相等；
     屏上是真相）。唯一例外：VM 尚未 adopt 该行（`sessionLoaded == false`）时它手里还是
     跨会话默认种子，此时行才是答案；**没有行**（草稿）时 VM 是唯一持有者，即便未 adopt 也用
     VM。
   - `session.primaryModel`：**行优先**。行存的是**显式 pin**（`entry:` / `group:` / 空 = 无
     pin），VM 存的是**正在用的模型** —— pin 为空时它也非空（`loadSession` 的优先级 3 链会给
     一个）。读 VM 会**发明一个用户没做过的 pin**，agent 的读-改-写会把假 pin 落盘。
     VM 只在**没有行**（草稿）时出面，因为那时它是唯一持有者。
   任务书 §2 的「reader 增 VM 优先读」如果照字面套到 `primaryModel` 上，就是把上面这条
   陷阱写进代码 —— 见 §6。
2. **可用性校验必须复用 `decideSessionModelResolution`**，不能自己写第二份「enabled + key」
   判断（同族复发纪律：两处判据 = 一次漂移后静默失效）。唯一代价是 writer 会读一次
   `loadApiKey`（见 R4）。

---

## §3 改动清单

| 文件 | 性质 | 行数 | 要点 |
|---|---|---|---|
| `config/SessionConfigWriteThrough.kt` | 生产（纯决策） | +107 | `BindingTarget` / `resolveBindingTarget` / `SessionConfigReadSource` / `thinkingLevelReadSource` / `modelBindingReadSource`；文件保持 Android 无关 |
| `debug/HeadlessChatRunner.kt` | 生产 | +170/−? | `applyModelOverride` 守卫 + `persistBinding` + `applyBindingToResident` + `TAG` + imports |
| `config/ConfigBuiltins.kt` | 生产 | +170/−? | 两个 reader 改双面读 + `residentViewModel` / `residentBindingToken` / `requireUsableEntry` / `firstUsableGroupMember` / `unusableReasonMessage` |
| `debug/ChatMutationMethods.kt` | 生产（注释） | +10/−? | `prompt` / `retry` 两处注释改成新语义（**无行为改动**） |
| `test/…/config/SessionBindingTargetTest.kt` | 测试（新） | +190 | 纯决策 11 用例：`Preserve` 是核心、空串=无绑定、三字段优先级 |
| `test/…/config/SessionConfigReadThroughTest.kt` | 测试（新） | +330 | 真源码文本上的接线 10 用例（6 正向 + 4 负控，负控改**副本**并断言同一判据转红） |
| `scripts/scan/chat_session_id_binding_check.py` | 门（扩） | +272 | criterion 16 + 夹具 `applyModelOverride` + 6 带牙臂 |
| `scripts/scan/provider_rpc_symmetry_check.py` | 门（扩） | +217 | C19（读侧）+ 夹具 readers + 5 带牙臂 |

---

## §4 门判据与带牙证明

### 门 A：`provider_rpc_symmetry_check.py` C19（读侧，新增判据，未动 C1–C18）

`check_session_read_through(cfg_code)`：对 `session.thinkingLevel` / `session.primaryModel`
两个 reader 块各判三条**行为**：

1. 可达驻留 VM 的非创建型查询（`peekViewModel(`）：直接出现，或经**一层同文件 helper**
   （本支是 `residentViewModel`）；
2. 行仍是 no-VM 回落（`getSession(` 可达）—— 只留 VM 会把「没有活会话」的读变成默认值；
3. `ResidentViewModel ->` 分支必须**产出值**（`.value` / `!!.` / 调用三选一）：peek 完返回
   常量 = 同一缺陷换壳。

不可抽取 ⇒ `fail_closed`（改名要重新锚定，不许删判据）。

**带牙臂（selftest F56–F60，逐条只红 C19）**：

| 臂 | 变异 | 结果 |
|---|---|---|
| F56 | 绿夹具（reader 双面） | 全清 |
| F57 | thinking reader 的 VM 分解改成 `val vm: ChatViewModel? = null` | 只红「never reaches the resident」 |
| F58 | thinking reader 删掉 `runBlocking { … getSession(sid) }` | 只红「dropped the row fallback」 |
| F59 | primaryModel reader 的 resident 分支 → `""` | 只红「answers a CONSTANT」 |
| F60 | 上述两个变异叠加（= 基线形态） | 只红 C19（2 条） |

**真树 vs 基线树**：`check_session_read_through(strip_comments(git show 1d44ec90:…ConfigBuiltins.kt))`
= **4 条 violation / 0 fail-closed**（两个字段各「never reaches」+「no resident arm」）；
真树 = 0。

### 门 B：`chat_session_id_binding_check.py` criterion 16（新增判据）

`_apply_override_violations(code)`：对 `suspend fun applyModelOverride(` 的函数体判四条：

1. 读**持久化绑定**（`modelBinding`）—— 不读就等于「无模型 = 套默认组」；
2. 决定**委派**给 `resolveBindingTarget(`（语义由纯单测钉住，门只需证明没绕过）；
3. **行的值就是决策的实参**（在 `resolveBindingTarget(…)` 的括号切片里必须出现
   `modelBinding`）—— 读出来再传 null 是同一缺陷换壳；
4. 写面：`updateSessionBinding(` 可达 **且** `peekViewModel(` 可达 **且** 刷新家族
   （`restoreFromBinding(` / `applyDefaultModelFallback(` / `selectEntry(` / `selectGroup(` /
   `applyNewChatDefaultModel(`）可达 —— 允许 **2 层同文件 hop**（
   `applyModelOverride` → `persistBinding` → `applyBindingToResident`）。
   另外 `defaultPrimaryGroupId` 必须仍在（新会话路径不许被顺手删掉）。
   函数缺失 ⇒ fail-closed。

**带牙臂（selftest MUT-O1..O6，逐条恰好 1 条 violation）**：

| 臂 | 变异 | 命中判据 |
|---|---|---|
| O1 | 行读了但传 `existingBinding = null` | ③「not what the decision is fed」 |
| O2 | 决策改成内联 `if (existing.isNullOrBlank()) default…` | ②「no longer routes」 |
| O3 | 删 `if (vm != null) applyBindingToResident(…)` | ④「peeked but never applied」 |
| O4 | 删 `peekViewModel` 那一行（只写行） | ④「never reaches the resident」 |
| O5 | `defaultGroupId = null` | 「dropped the default primary group」 |
| O6 | 基线形态（无 `modelBinding`、无条件默认组） | ①「never reads the persisted binding」 |

**真树 vs 基线树**：`_apply_override_violations(strip_comments(git show 1d44ec90:…HeadlessChatRunner.kt))`
= **2 条**（① + ④）；真树 = 0。

---

## §5 验证四层

### L1 门（真树 + selftest + 全量）

- 门 A：真树 `ALL GREEN (… C18, C19 session config read-through…)`；`--selftest`
  **`PROVIDER_RPC_SELFTEST=PASS`**（C18 既有的 15 臂 + F56–F60 全 PASS）。
- 门 B：真树 `all criteria satisfied`；`--selftest`
  **`CHAT_SESSION_ID_BINDING_SELFTEST=PASS`**（既有 13/14/15 臂 + MUT-O1..O6）。
- 全量：`sh scripts/scan/scan.sh` → **`Summary: 109 passed, 0 failed`**（`probe/l3/scan-full2.log`，
  rc=0，`❌` 0 条，无 FAIL 行）；`python3 scripts/scan/test_scan.py .` → **`ALL 336 CASES PASS`**。
  门数 109、夹具 336 **未变**（本支只扩既有门的判据与自带 selftest 臂）。

### L2 邻门（按概念反查，非照抄任务书名）

`envvar_config_group_check`（同一 `ConfigBuiltins` 配置面）/ `debug_leak_guard`（`debug/`
包）/ `dao_loadmessages_check`（headless 读取面）/ `prefs_listener_holder_guard`（
`ConfigBuiltins` 的 listener 持有）/ `parts_json_funnel_check`（headless parts 漏斗）
→ **5/5 rc=0**。

### L3 真跑（真 kotlinc 2.0.21 + 真 JUnitCore 4.13.2）

装置 `probe/l3/`（`mutate.py` / `arm.sh` / `pure_arm.sh` / `judge_ci.py`，worktree 私有，
`.gitignore:141` 覆盖）。**纯决策臂**（`probe/l3/pure_arm.sh`）：

| 臂 | 结果 |
|---|---|
| `baseline`（`git show 1d44ec90:…SessionConfigWriteThrough.kt` + 新测试） | **编译红**：`unresolved reference 'BindingTarget' / 'resolveBindingTarget'`（决策尚不存在 = 硬证据） |
| `post`（真文件） | **OK (11 tests)** |
| `m8`（`if (false) return BindingTarget.Preserve`） | **2 红**：`no request on an already bound session preserves the binding` / `an explicit empty request id is not a request` |

**接线臂**（`probe/l3/arm.sh`，10 用例）：

| 臂 | 变异 | 结果 |
|---|---|---|
| POST | 真树 | **OK (10 tests)** |
| PRE | `probe/pre/`（基线两文件） | **10/10 红**（6 条正向判据 + 4 条负控无法施加自身变异） |
| M1 | 删 `existingBinding = …modelBinding,` 实参 | 恰 2 红（守卫 + 其负控） |
| M2 | 删 `refresh = { resident -> … },` | 恰 2 红（刷新 + 其负控） |
| M3 | 实参改成 `existingBinding = null,`（读了不传） | 恰 2 红（守卫 + 其负控） |
| M4 | thinking reader 的 VM 分解改 `val vm: ChatViewModel? = null` | 恰 2 红 |
| M5 | 删 `requireUsableEntry(providerRepo, entry, "entry:$uuid")` | 恰 2 红 |
| M6 | `firstUsableGroupMember(...)` 换成「直接取第一个成员」 | 恰 2 红 |
| M7 | primaryModel resident 分支 → `""` | **恰 1 红** |

### L4 CI

`#2546` / run `39917174738` / job `120293133939` / attempt 1 **success**，**三方 sha 逐字符一致**
（本地 `HEAD` == `git ls-remote` == `run.head_sha` == `df76515edbf48b2322f8ba21fa81179c2f315126`）。
`probe/l3/judge_ci.py`（**身份与步骤读 API JSON，不从日志猜**）判读：

- 26 步，`conclusion != success/skipped` 的步 = **[]**；唯一 skipped = `Publish to Releases`
  （main 门控 ✓）；
- `Summary: 109 passed, 0 failed` 恰 **1** 条，无任何 `… N failed` 行；门头 `[n/109]` **109** 条
  且**门名互异**；`ALL 336 CASES PASS` ✓；编译错 `e: ` **0**；
- 新增用例：`SessionBindingTargetTest` **11 PASSED / 0 FAILED**、
  `SessionConfigReadThroughTest` **10 PASSED / 0 FAILED**；游离 `FAILED` 行 **0**；
- 本支两道门在日志中的实证：`chat_session_id_binding_check: all criteria satisfied`（行 795）、
  `✅ chat_session_id_binding_check selftest has teeth (…=PASS)`（行 1235）+ `real tree green`、
  `provider_rpc_symmetry: ALL GREEN (… C19 …)`（行 908）、`✅ provider_rpc_symmetry_check
  selftest has teeth (PROVIDER_RPC_SELFTEST=PASS)`（行 1259）；
- 日志 1,411,996 B（`probe/l3/job-120293133939.log`）。

---

## §6 任务书失真

- **D1（会把人带进沟里，最重要）**：任务书 §2「reader 增 VM 优先读（有 VM 读 VM 现值，无 VM
  回落 Room），语义 = 读屏上真实值」对 `session.thinkingLevel` 成立，对
  `session.primaryModel` **不成立**。后者的两个面**不是同一个事实**：行 = 显式 pin，VM =
  正在用的模型（pin 为空时也非空，由 `loadSession` 优先级 3 填）。照字面做会让
  `get session.primaryModel` 在「无 pin」的会话上报一个 `entry:<fallback>`，agent 的
  读-改-写会把这个假 pin 落盘 —— 属于「修一个 bug 引入另一个」。本支按字段分别定优先级
  （`thinkingLevelReadSource` / `modelBindingReadSource`），并把两条都写进纯单测
  （`an unloaded resident vm defers to the row` / `the model binding answers from the row
  whenever it exists`）。
- **D2（表述过宽）**：任务书 §2 的「校验 entry/group 的**可用性**」没说清按哪个判据。本支取
  `decideSessionModelResolution`（运行时单源）。若按任务书字面自写一份「enabled + 有 key」，
  就会产生第二份判据（同族漂移风险），已否决并写入 §2。
- **D3（事实核对，均属实）**：任务书点名的三处生产文件（`HeadlessChatRunner.kt` /
  `ChatMutationMethods.kt` / `ConfigBuiltins.kt`）与「可加 `SessionConfigWriteThrough.kt`」、
  两道门名（`provider_rpc_symmetry_check.py` 的 C18、`chat_session_id_binding_check.py`）、
  「复用 1044-A05 的 `deliver(vm, persist, hasPersistedRow, refresh, …)`」**逐条对上真树**
  （`deliver` 在 `SessionConfigWriteThrough.kt:73-90`）。本支**没有**任务书失真级的
  行号/函数名错误，D1 是「规则本身不适合这个字段」。

---

## §7 未做 / 残留（每条带触发条件）

- **R1｜真机行为级（P1 验证面）**：① 会话 A 里手动选一个非默认模型 → 用 `chat.prompt`（不带
  `modelEntryId`）连发 3 条 ⇒ 模型不变；② 同一会话 `chat.retry` ⇒ 模型不变；③ 新会话
  首次 `chat.prompt` ⇒ 落默认组（新语义不能被误伤）；④ `minis-config set session.thinkingLevel
  high` 后立刻 `get` ⇒ `high`；打开着的聊天屏档位同步变；⑤ 草稿会话设 `primaryModel` 后
  `get` ⇒ 同一个 `entry:` / `group:`；⑥ `set session.primaryModel entry:<禁用实例>` ⇒ 报
  可读错误、行不变。**触发条件** = 下次真机窗口。**触发后方案** = 汇总进 `§1045-VERIFY`
  一次开齐。**搁置理由** = 沙箱无 Android 运行时：`peekViewModel` 走 `ViewModelStore`、
  `restoreFromBinding` 依赖 Compose/StateFlow 时序，JVM 里只能钉决策与接线。
- **R2｜`thinkingLevel` 的「VM 已 adopt」依赖 `sessionLoaded`（P3）**：如果哪一天草稿也会跑
  `loadSession()` 并置 `sessionLoaded = true`，`thinkingLevelReadSource` 的第一个分支就会把
  「草稿优先读 VM」这条退化成「行优先」，而草稿没有行 ⇒ 落到 `Absent`（读默认值）。
  **触发条件** = 有人让 `__new__` 会话也走 `loadSession()`。**触发后方案** = 给
  `thinkingLevelReadSource` 加 `isDraft` 入参并在单测补一拍。**搁置理由** = 当前草稿不跑
  `loadSession`（`ChatScreen` 用 store 里的独立 VM），且纯单测已把两种分支都钉住。
- **R3｜草稿的 `entry:<activeEntryId>` 可能「报告」一个非显式 pin（P2）**：无行时
  `residentBindingToken` 取 `activeEntryId`，而它也可能来自回落链。写回同值等价，但
  「空 = 无 pin」这条契约对草稿不成立。**触发条件** = 有消费者在草稿上依赖「空 = 无 pin」。
  **触发后方案** = VM 侧补一个显式绑定标记（`_bindingJson`），`residentBindingToken` 改读它。
  **搁置理由** = 改 VM 状态面超出本支半径（且是 1044-A05 的 `route` 之外的新面）。
- **R4｜writer 每次 `set session.primaryModel` 会读一次 `loadApiKey`（P2 性能）**：加密 prefs
  读在 RPC 线程（IO）上，单次毫秒级。**触发条件** = 出现「`minis-config set` 变慢」报告，
  或某天 `loadApiKey` 变成 suspend/网络调用。**触发后方案** = 把「instance 可用性」缓存进
  `ProviderRepository.config` 的派生视图（一处算、多处读）。**搁置理由** = 配置写入是低频
  人工操作，缓存会引入失效面。
- **R5｜无 key 的 provider（本地 LLM / 自建）现在会被 writer 拒绝（P2 行为，方向有意）**：
  `restoreFromBinding` 本来就拒绝（`MISSING_API_KEY`），所以「能 pin 上」以前是假象 ——
  写进去、会话立刻丢弃、界面回落到别的模型。**触发条件** = 用户报「本地模型不能 pin」。
  **触发后方案** = 让 `decideSessionModelResolution` 认「该 provider 类型不需要 key」
  （单源改一处，writer 自动跟随）。**搁置理由** = 单源在 `ChatSessionLifecycle.kt`（本支
  禁区），且当前无 key provider 本来就 pin 不住。
- **R6｜group 分支的 `model_id` 从「第一个成员」改成「第一个可用成员」（P3 行为改进）**：
  优先展示与实际一致。**触发条件** = 有人依赖「model_id == 第一个成员」的旧行为（如会话列表
  副标题的稳定性）。**触发后方案** = 保留原语义 + 只做校验。**搁置理由** = 旧语义会把不可用
  成员的模型名写进列表，缺陷性质。
- **R7｜门的盲区：`?.let { null }` 这类「读了但喂等价 null」（P3）**：criterion 16 判的是
  「实参切片里出现 `modelBinding`」，`existingBinding = row?.modelBinding?.let { null }`
  能过。**触发条件** = 出现一次「读了但明显丢弃」的改造。**触发后方案** = 增加「实参表达式
  不得以 `null` 字面量收尾」的判据（等价于语法层面的轻量求值）。**搁置理由** = 装置比例：
  现实回归形态是「删守卫 / 传 null 字面量 / 删刷新」，三种都已带牙（O1/O3/O4/O6）。
- **R8｜门 A 的「resident 分支产出值」判据是宽松三选一（P3）**：`.value` / `!!.` / `(`
  任一命中即算。`ConfigValue.Str("")` 这种也能过。**触发条件** = 出现「peek 之后套一层
  常量包装」的改造。**触发后方案** = 改成「分支体内必须出现 VM 接收者的成员访问」。
  **搁置理由** = 三选一已覆盖全部现实写法，收紧会引入拼写依赖。
- **R9｜装置未入库（P3，批次惯例）**：`probe/l3/` 留在工作树（`.gitignore:141` 覆盖）。
  **触发条件** = 需复现本支 L3/L4 证据。**触发后方案** = 按 §5 的表重建（`mutate.py` 的
  7 个锚点 + `pure_arm.sh` 3 臂 + `judge_ci.py`）。

### 本支自伤（都当场闭环，值得跨批复用）

1. **新增代码会被「别的门」拦**：`thinkingLevel` 的多行
   `runCatching {` + 换行 `ThinkingLevel.valueOf(raw)` 触发了 `enum_parse_safety_check`
   的**行级**判据（`valueOf` 那一行本身必须含 `runCatching|getOrElse|getOrNull`）⇒ 全量扫描
   第 3 关当场红。改成单行 `else runCatching { ThinkingLevel.valueOf(raw) }.getOrDefault(...)`
   即绿。**规则级教训：只跑本门 + 邻门不够，新代码必须跑一次全量 `scan.sh`**（本支正是靠
   全量扫描抓到的；上一波同族也有先例）。
2. **Raw string 不认 `\$`**：Kotlin 原始字符串里 `"entry:\$uuid"` 仍会插值（`\$` 不是转义），
   测试因此编译红；正解是字符拼接 `"entry:" + '$' + "uuid"`。
3. **`sleep` 写在 shell 命令里撞 stall-guard**（180 s 无输出即杀）：等待 CI 必须用工具的
   `delay` 参数，不是命令行里的 `sleep`。
4. **首版手臂锚点失效**：`applyModelOverride` 从「局部变量 + 传参」改成「实参内联」后，三条
   变异臂的锚点全部失配（`ARM … anchor count=0`）。变异脚本对锚点做 `assert count == 1` 是
   必须的 —— 静默跳过会让「已变异」成为假象。

---

## §8 身份链

| 项 | 值 |
|---|---|
| 基线 main | `1d44ec905b8f5d21a215694e9a13910c74e1f645`（`git ls-remote` 自核） |
| 分支 | `fix/dv25-a3-headless-session-config` |
| 提交 | `df76515edbf48b2322f8ba21fa81179c2f315126`（单提交：生产 4 + 测试 2 + 门 2） |
| 本地 HEAD / 远端 / CI `head_sha` | 三者逐字符一致 = `df76515e…` |
| CI | `#2546` / run `39917174738` / job `120293133939` / attempt 1 / **success** |
| 门 / 夹具 | 109 / 336（**均未变**；0 新建门，扩 2 道既有门） |
| 冻结面 | `scan.sh`、`test_scan.py`、`.github/`、gradle、manifest **0 触碰** |
| 证据文件 | `probe/l3/{scan-full2.log,test-scan.log,gate-a-selftest.log,gate-b-selftest.log,arm.sh,pure_arm.sh,mutate.py,judge_ci.py,ci-judge.json,job-120293133939.log}` |
| 停止节点 | **分支 CI 绿 = 达成**；未合并 main（起手权在用户） |

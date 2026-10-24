# FIX-A2 — V-1 + A-02 + A-03 + A-04（P0/P1）headless 发消息缺会话存在性判定 · 插入重试误吞 FK · 绑定/选型/压缩三处「假成功」

**分支** `fix/dv29-a2-headless-binding-and-fk-crash` · **基线** `b82aa8dc2266bca71653c4ed017dee0679752605`（main）
**工作树** `/var/minis/shared/work/deep-audit-1049/a2/`（私有 worktree，未 clone、未切 golden）
**规模** 主代码 3 文件 **+338/−16**（`data/repository/ChatRepository.kt` +110/−1 · `debug/HeadlessChatRunner.kt` +223/−15 · `debug/ChatMutationMethods.kt` +5/−0）· 测试 4 文件 **+834/−0**（27 个 `@Test`）· 门 2 文件 **+770/−13**（均为既有门扩展，**0 新建门 ⇒ 门 111 / 夹具 342 双不变**）· 本报告不入库
**交付 SHA**：代码提交 `d8987b88`；分支尖端与它的差异仅在 `fix-reports/`（报告回填，不参与构建、不在 workflow `paths` 内）

---

## 1 缺陷与触发路径（真实调用链）

### 1.1 V-1：进程被杀 —— 一个 FK 失败被当成「排序位竞争」重试了第二次

真机两份 crash 日志（`/var/minis/logs/crash-2026-10-24_12-57-02.log` / `_12-57-21.log`，各带同一 stack），12:57:02 那份的时序是逐行可读的：

```
:8    android.database.sqlite.SQLiteConstraintException: FOREIGN KEY constraint failed (code 787 SQLITE_CONSTRAINT_FOREIGNKEY)
:193  W/Minis.HeadlessChatRunner  applyModelOverride: session '00000000-…' has no persisted row and no resident view model — the binding write matched 0 rows
:202  I/Minis.ChatRepository       appendMessage: enter session=00000000-… role=user partsLen=32
:205  I/Minis.ChatRepository       appendMessage: constraint-retry re-order=0 (original=0)   ← 重试了，且排到了同一个 0
:208  E/a                          … SQLiteConstraintException: FOREIGN KEY constraint failed (code 787 …)   ← 第二次抛，逃出 catch → ACRA → 进程死
```

链条：

| 环节 | 位置（基线真树） | 行为 |
|---|---|---|
| ① 校验缺失 | `HeadlessChatRunner.applyModelOverride`（`debug/HeadlessChatRunner.kt`） | 自己已经**识别**出「会话无持久行且无驻留 VM」并打了 warning，然后**继续**走 `persistBinding` |
| ② 写入 0 行 | 同上 → `SessionConfigWriteThrough.deliver` | `hasPersistedRow` 为假 ⇒ `DraftWithoutRow` 路由，`updateSessionBinding` 命中 0 行（**写盘成功但什么都没写**，也是第二种「假成功」） |
| ③ 无校验地追加 | `HeadlessChatRunner.prompt` → `ChatViewModel.sendMessage` → `ChatRepository.appendMessage` | 直接往一个不存在的 `session_id` 插消息 |
| ④ 约束误判 | `ChatRepository.appendMessage` 的 `catch (e: SQLiteConstraintException)` | 把**任何**约束异常都当成 `unique(session_id, sort_order)` 竞争：`nextSortOrder` 对不存在的会话返回 **0**（`:205` 实测 `re-order=0 (original=0)`），用同样的参数重放同样的插入 → 同样的 FK 失败 |
| ⑤ 逃逸 | 同上 | 第二次 `insertMessage` 的异常**不在** catch 内 ⇒ 协程里未捕获 ⇒ ACRA `ForegroundExceptionHandler` 结束进程 |

`messages.session_id` 是外键（`data/db/MessageEntity.kt` 的 `@ForeignKey(entity = ChatSessionEntity::class, onDelete = CASCADE)`）。**FK 失败 ⟺ 父行不存在**，这是结构性的：再读一次 `nextSortOrder` 也不能让它成立。

两条都能复现（`--session <不存在的 id>`），且是**同一个用户级操作两次**杀进程。

### 1.2 A-02：会话模型绑定的 IPC 写入口没走 `session.primaryModel` 的同一判据

`ConfigBuiltins` 的 `session.primaryModel` 写入器对每个条目调 `decideSessionModelResolution`（禁用实例 / 无 key / 实例缺失一律拒绝），组分支取 `firstUsableGroupMember`。`HeadlessChatRunner.applyModelOverride` 是它的 IPC 双胞胎，却只判：

* Entry 分支：`instance.isEnabled`（**不查 API key**）；
* Group 分支：**什么都不判**，`model_id` 直接取 `memberEntryIds.first()`（附录里那行 `firstMember?.baseModel?.id ?: ""`）。

后果是「写进去、被自己拒绝、却报告成功」：`restoreFromBinding` 用同一判据把绑定判为不可用并回落到默认模型，而 `applyModelOverride` 返回的是那个**被拒绝**条目的 display name —— 调用方从此以为这一轮跑在 X 上。

### 1.3 A-03：`selectModel` 把「上一条模型名」当成本次结果返回

`ChatViewModel.selectEntry` 对未知/禁用/无 key 的目标**静默 return**（这是 UI 契约，点按依赖它），而 `HeadlessChatRunner.selectModel` 无条件 `vm.modelName.value to vm.thinkingLevel.value.name` —— 拒绝时返回的是**上一个**模型名，紧挨着调用方刚传进来的 id。对自动化来说，这比报错更坏：它会带着错误认知继续跑。

### 1.4 A-04：`compact` 把「作业结束」当成「压缩成功」

`CompactOrchestration.compactAll`：`:226` `_isCompacting.value = true` → 干活 → `:230` `finally { _isCompacting.value = false }`。所以 flag 落回 `false` 只说明**作业结束**，不说明折了任何东西。三条路都落在同一个 `false` 上而不写 marker：

| 失败路径 | 位置 | 结局 |
|---|---|---|
| 模型请求抛异常 | 摘要在 `generateCompactSummaryWithSplitting` 里 | 异常 → 无 marker |
| 摘要为空 | `:294` `if (summary.isEmpty()) … return@launch` | 无 marker |
| 锚点校验失败 | `:141` `anchorIdx < 0` / `:165` `effectiveStartIdx > anchorIdx` / `:221` 空范围 | 在 `:226` 之前 return，**flag 根本没翻** |

`HeadlessChatRunner.compact` 的判定是 `changed = summary != prior && !isBlank`，然后 `else -> "Completed"`（注释原文「marker may have written even if string equal」）—— 于是前两条路明确报 `Completed`。`marker` 才是成功产物：`ChatDao.insertCompactMarker`（每个成功一次新 uuid），且 `:412` 起已有 [audit-0917] 守卫把「marker 没落库」单独当失败处理。

---

## 2 先证（动手前的四条回树核实）

1. **真机**：两份 crash 日志逐行对齐（§1.1 表格里的 `:193/:202/:205/:208` 是 `grep -n` 实测行号），第 7 行为 `FOREIGN KEY constraint failed (code 787)`，第 205 行证明「重试拿到了 0」。
2. **A-02/A-03/A-04 的代码面**：`applyModelOverride` 的 `isEnabled`-only 判断、`memberEntryIds.firstOrNull()` 取 `model_id`、`selectModel` 无条件返回 `vm.modelName.value`、`compact` 的 `else -> "Completed"` —— 四处都在基线真树里读到原文。
3. **等价性**：`nextSortOrder` 对不存在会话返回 0（真机日志 `re-order=0 (original=0)` 直接给证）；`MessageEntity` 的 FK 定义 + `onDelete=CASCADE` 说明父行缺失是结构性的（`git grep ForeignKey` 命中）。
4. **A-04 的可指认性**：三条失败路径在 `CompactOrchestration.kt` 各有行号（§1.4 表），且「marker 落库 = 成功」有既有代码背书（`:412` 的 [audit-0917] 注释 + `insertCompactMarker` 的每成功一次新 uuid）。

---

## 3 修法（停在第几级阶梯、为什么）

| # | 改动 | 阶梯 | 为什么停在这里 |
|---|---|---|---|
| 1 | `appendMessage`：`catch` 内先分类（`classifyMessageInsertConstraint(e.message)`），只有 `SORT_ORDER` 与 `UNKNOWN` 才重放；**其余分支 `throw MessageInsertRejectedException(conflict, sessionId, e)`**；重放自身的失败也被包成同一个类型 | 第 7 级（最小实现） | 标准库/平台原生都不提供「这条约束能不能重试」的信息，唯一判据是 SQLite 的 message；「分类 + 有界重放 + 二次失败也分类」是让**任何**输入都不会以裸约束异常逃逸的最小形状 |
| 2 | 重试的二次失败也包成 `MessageInsertRejectedException`（`runtime` 的第一个异常仍是 cause） | 同上 | 「不要只是重试一次失败就算了」：第二次失败必须带分类，否则崩溃点只是从第 1 次挪到第 2 次 |
| 3 | `UNKNOWN` 只允许**一次有界重放** | 同上 | message 缺失（例如某些测试运行时）时无法判断，重放一次后无论成败都分类抛出；真实 SQLite 永远给 message，所以生产上 FK 从不重放 |
| 4 | 新增 `HeadlessChatRunner.requireSendableSession`（读行 `getSession` → 无行再 `peekViewModel` → 都空则 `RPCException(-32602, "session_not_found: …")`），在 `prompt` / `retry` / `rerunFromToolBlock` / `selectModel` / `applyModelOverride` 与屏侧 `ChatMutationMethods.uiPrompt` 上**在 `viewModel(...)` 之前**调用 | 第 2 级（库内已有形态） | 与 `persistBinding` 的 `hasPersistedRow` 同源事实（0 行），但方向相反：那里是「无行就别假装写了」，这里是「无行就别发」。草稿（`__new__…`）靠「驻留 VM」半边合法通过 |
| 5 | `applyModelOverride` 两分支都走 `decideSessionModelResolution`；组分支取 `firstUsableGroupEntry`（同一个判据）；两者不可用时 `RPCException(-32602, "… is not usable — $reason")` | 第 2 级 | 判据已存在（`session.primaryModel` 用的就是它）；这里只是把 IPC 写入口接回同一来源，`reason` 直接用共享枚举名，避免两套词表 |
| 6 | `selectModel`：先 `decideSessionModelResolution` 预判（拒绝 → `-32602`），`vm.selectEntry` 后读 `vm.activeEntryId.value` 校验，不等则 `-32000 model_switch_not_applied: requested=… active=…` | 第 6 级（能用一行） | 校验对象是**可观测状态**而不是「调用返回了」，这是本项目反复吃过的教训（清理型/生效型缺陷同族） |
| 7 | `compact`：动手前快照 `latestCompactMarker(sessionId)?.id`，结束后再读一次；`settleCompactOutcome(finished, markerLanded, summaryChanged)` 纯函数定 `status/error` | 第 6 级 | 成功产物已经存在（marker 行），不需要新状态、不需要改 `ChatViewModel`（那是本支的半径外文件） |
| 8 | 门扩展：`chat_session_id_binding_check` criterion 18（发/绑入口必须有无行早退，判据从「函数体调用 `getSession(`+`peekViewModel(`+`throw RPCException(`」派生守卫）+ criterion 19（约束重试只对 UNIQUE，`sort_order` 限定，拒绝分支必须在重放之前）；`session_provider_enabled_check` C-SEL-4（绑定写入口必须可达 `decideSessionModelResolution`，且禁止 `memberEntryIds.first()` 这类位置取人） | — | 两道门都从树派生成员集合（新入口自动纳入），并把「台词文案」排除在判据外 |

**回归控制**：`AppendMessageSortOrderRetryTest`（[RC15] 的冻结套件）一字未改，仍然覆盖「竞争 → 重试成功」；新增 `MessageInsertRetryGateTest` 又写了一条同类正例。

---

## 4 根因层

* **V-1 = 「一个 catch 认领了一类异常」**。`SQLiteConstraintException` 是**一类**（UNIQUE / FK / NOT NULL / CHECK…），代码按**一个**（排序位竞争）处理。同一族此前已有一次代价更低的表现（1046-B03「同一次调用的两个参数各读自己的副本」），这次代价是两次进程被杀。
* **更深一层 = 「校验做了，结论没用」**。`applyModelOverride` 自己算出「0 行」并写进日志，然后继续 —— 观测不等于守卫。这是本项目第二次见到「warn 完继续跑」（第一次是 1042-A5 的 warn-only 5s 兜底），区别是那次兜底是刻意的，这次不是。
* **A-02/A-03 同根**：同一决策在**两处实现**（`ConfigBuiltins` 一套、IPC 一套），第二套只实现了第一套的一部分（漏 key、漏整组）。A-03 是它的孪生：入口无法区分「开关成功」与「被静默拒绝」，于是用**自己的入参**当答案。
* **A-04 = 「状态位被当成事实」**。`isCompacting=false` 是「作业结束」，被读成「压缩成功」。项目里同类已登记形状 39（1046-D7「清理/移除调用形似生效」）—— 两者的共同点是**判据锚在调用/状态转移上，而不是锚在被改变的持久产物上**。
* **门为何没拦住**：criterion 14 只钉「所有入口都在 `sessionLoaded` 之后变更」，没有钉「会话存在」；`session_provider_enabled_check` 只钉 `session.primaryModel`（配置面），没覆盖 `debug/` 的 IPC 写入口。**门的作用域是手写目录清单**时必漏第二份实现 —— 这是 1046-F9 已经记过的同族。

---

## 5 证据

### L1 门（本树 + 齿）

| 门 | 真树 | selftest | 新增臂 |
|---|---|---|---|
| `chat_session_id_binding_check.py` | `all criteria satisfied`（rc=0） | `CHAT_SESSION_ID_BINDING_SELFTEST=PASS` | criterion 18：4 臂（入口掉守卫 / 绑定入口掉守卫 / 守卫失去 RPC 拒绝 / 守卫不再看驻留 VM）+ green 臂；criterion 19：green 臂（带 repo 夹具）+ 4 臂（重放一切 / 分类器漏 FK / 分类器接受任意 UNIQUE / 拒绝分支挪到重放之后）+ 缺锚臂（`appendMessage` 改名 ⇒ 必须红） |
| `session_provider_enabled_check.py` | `GATE=GREEN`（8 处 `ProviderFactory.create` 仍经 enriched model） | `SESSION_PROVIDER_ENABLED_SELFTEST=PASS` | C-SEL-4：compliant / pre-fix（两条违规同时命中）/ 无 funnel / 无 writer（fail-closed）4 臂 |

**PRE 臂（新门跑在基线树上，`git archive b82aa8dc src` → `/tmp/dv29-base`）**：

```
FAIL debug/*.kt: no session-existence guard anywhere in the package … (5 send/bind entries)
FAIL ChatRepository.kt: the constraint catch does not discriminate the UNIQUE sort_order race …
FAIL ChatRepository.kt: the constraint catch has no non-retry exit (`throw `) …
gate RED: 3 violation(s)
```

```
session_provider_enabled_check: `applyModelOverride` writes the session model binding without routing through decideSessionModelResolution …
session_provider_enabled_check: `applyModelOverride` picks a group member by POSITION (`memberEntryIds.firstOrNull(`) …
GATE=RED session_provider_enabled (2 violation(s))
```

⇒ 两条新判据都**指向本案的具体缺陷**，不是泛泛的形状检查。

### L2 邻门 / 全量

* 逐门直跑 **111/111 rc=0**（`calendar_events_nullable_predicate_check` / `tts_voice_and_sanitizer_check` 需 `--root` 形态，单独复跑 OK）。
* `python3 scripts/scan/test_scan.py` → **ALL 342 CASES PASS**（rc=0；门 111 / 夹具 342 与 main 相同 = 0 新建门）。
* 说明：`sh scan.sh` 整链在本窗口撞到沙箱 S28（`Function not implemented` 扩散到 `tail`/`/tmp/.minis_cwd_*`），按注册表健康路径改为逐门直跑 + 单独跑 test_scan。

### L3 沙箱 JVM（真 kotlinc 2.0.21 + 真 JUnit 4.13.2）

装置 `probe/make_slice.py`：把**生产文件的字节**切片编译（不重写逻辑），只给需要 Android 运行时的类型打桩。

| 臂 | 内容 | 结果 |
|---|---|---|
| POST | `HeadlessChatRunnerDecisionTest`(8) + `Dv29HeadlessWiringShapeTest`(11) + `MessageInsertConstraintClassifyTest`(5) 编译并运行 | **OK (24 tests)** |
| PRE（形状） | 同一份形状测试跑在基线文本上（`-Duser.dir=/tmp/dv29-pre`，两文件取自 `b82aa8dc`） | **11/11 fail**（4 条正臂如实指出「修复不存在」） |
| 负控 | 每个形状臂都带「改回旧形状必须红」的变异（去掉共享判据 / 位置取人 / 去掉 post-check / 去掉预校验 / 去掉 before 快照 / 入口先建 VM / 守卫不读行） | 全绿（`mutate()` 断言锚点确实命中，锚点丢失即失败） |

**踩到的两次自伤（都当场闭环，值得记）**：①形状测试第一版被**自己写的注释**打红 —— 「禁止 `else -> "Completed"`」命中的是解释该修复的注释，位置取人的正则命中的是记录该禁法的注释 ⇒ 现在测试先 `stripCommentLines`；②`funBody` 的 `String.replace(old,new,1)` 在 Kotlin 里没有 count 参数（`replaceFirst` 才对）。

### L4 分支 CI

**两次 run（第一次红得有价值）**

| # | run / job | head_sha | 结果 | 说明 |
|---|---|---|---|---|
| 1 | `40070578165` / `120802924546` | `d8987b88` | **failure** | `Run unit tests (full suite)` 红，日志里**唯一**一条编译错：`MessageInsertConstraintTest.kt:191:45 Argument type mismatch: actual type is MessageEntity, but String! was expected` —— `private val message: String` 被 `insertMessage(message: MessageEntity)` 的形参遮蔽，`SQLiteConstraintException(message)` 收到实体。`d8987b88` 上其余 25 步全绿（含 pre-build scan gate） |
| 2 | `40071184871` / `120804762518` | `0d0b7c80` | **success** | attempt 1，`event=workflow_dispatch` |

**run 2 判读（日志 1,469,779 B）**

* `head_sha = 0d0b7c8006f86aae080726d1cf8f5896d033b3ee`，与本地 `git rev-parse HEAD` **逐字符一致**。
* 26 个步骤：`bad=[]`，唯一 `skipped` = 步骤 23 `Publish to Releases`（main 门控）。
* 扫描门：`Summary: 111 passed, 0 failed` **恰一条**，`[n/111]` 门头去重后 **111** 个（1..111 连续）。
* 夹具：`ALL 342 CASES PASS` 恰一条。
* 编译错：`e: file://` **0 条**。
* 新用例逐条 PASSED（27/27）：`MessageInsertConstraintClassifyTest` 5 · `MessageInsertRetryGateTest` 3 · `Dv29HeadlessWiringShapeTest` 11 · `HeadlessChatRunnerDecisionTest` 8。
* 本支改动文件**编译警告 0**（`w: file:///…/(ChatRepository|HeadlessChatRunner|ChatMutationMethods|Dv29|HeadlessChatRunnerDecision|MessageInsert)` 无命中）。
* 游离 `FAILED` 行 0（含 `FAILED` 的行全是 PASSED 用例名的子串）。

**★ 第一次红的形态值得记**：沙箱 L3 装置**跑不到**这个文件（`ChatRepository` 依赖 Room/Android 太多，由 `probe/make_slice.py` 排除），所以「假 DAO 的构造参数名与接口方法形参**同名**」这类纯 Kotlin 解析错误只在 CI 暴露。教训：**CI-only 的测试文件，形状之外还要人工过一遍「同名遮蔽」与接口签名**（本支另做了 55 个 `ChatDao` 方法的签名逐条比对脚本，结论：全命中、无多余）。

---

## 6 刻意没做的

1. **不给 `compact` 加失败原因通道**。要知道「是异常、是空摘要、还是锚点无效」，得在 `ChatViewModel` 里加状态（本支半径外）。现在的判据是「有没有留下折过的证据」，`error` 统一为 `compact_failed:nothing_folded` —— 它足以把「假成功」变成「失败」，但不足以诊断原因（`AppLogger` 里各分支都有自己的日志）。
2. **锚点无效仍然报 `NoOp` 而不是 `Error`**。`anchorIdx < 0` / 已压缩 / 空范围都在 `_isCompacting` 翻起之前返回，`compact` 的现有分支把它读成 `NoOp` —— 这是**诚实的**：什么都没折，也没有声称成功。把它改成 `Error` 会把「本来就不需要压」误报成故障。
3. **`marker` 落库失败但摘要已在内存生效时仍判 `Completed`**。`:435` 已有 [audit-0917] 守卫在播报「marker 未持久化，摘要仅在内存」，且那次确实折了（界面已灰化、摘要已生效）；判 `Error` 会把「可见地成功了」报成失败。持久性损失由既有守卫承担，不在本支扩面。
4. **不给 `ChatViewModel.selectEntry` 加返回值**。静默拒绝是 UI 契约（点按依赖它），改成抛/返回会波及屏侧调用点；入口侧自己判 + 读可观测状态更便宜。
5. **不合并 `ConfigBuiltins.firstUsableGroupMember` 与 `firstUsableGroupEntry`**。前者是 `private`，且 `ConfigBuiltins.kt` 是 A4 的独占文件（本支不碰）。两份循环共享**判据**（`decideSessionModelResolution`），差异只剩「取哪个 List」。
6. **不改 `SessionConfigWriteThrough.deliver` 的 `DraftWithoutRow` 语义**（warn 后继续）。它现在被 `requireSendableSession` 前置挡住，改它属于半径外。
7. **不给 `minis-sessions-cli` 加存在性预检**。RPC 侧的 `-32602` 已经是结构化答案，CLI 只需透传。

---

## 7 验证覆盖与缺口（逐条）

| 条目 | 覆盖方式 | 缺口 |
|---|---|---|
| V-1 ①（只对 UNIQUE 重试） | `MessageInsertConstraintClassifyTest`（5 臂，字面量）+ criterion 19（真树绿 / 4 条负控 / 缺锚红） | 真机端到端「同一命令不再崩」未跑（需真机窗口） |
| V-1 ②（FK 不再以裸异常逃逸） | `MessageInsertRetryGateTest`：FK 臂断言**抛的是 `MessageInsertRejectedException`**、`sessionId` 保留、`conflict != SORT_ORDER`、插入次数 ≤2；二次失败臂断言重放失败也被分类 | ⚠️ **该文件只在 CI（Gradle + Room）跑**，沙箱无法编译 `ChatRepository`（Room/Android 依赖太多）。沙箱里能跑的是分类器本身（纯函数）与形状 |
| V-1 ③（入口存在性判定） | criterion 18（派生入口集 + 派生守卫）+ `Dv29HeadlessWiringShapeTest`（5 个入口的 gate-before-VM 顺序 + 屏侧入口 + 守卫三要素）+ 2 条负控 | 同上：RPC 端到端「`send --session <missing>` 返回错误而非崩」待真机 |
| A-02 | `HeadlessChatRunnerDecisionTest`（拒绝原因 token 3 臂 / 组内跳过禁用与无 key / 全不可用 / 空组）+ C-SEL-4（真树绿 + pre-fix 两违规）+ 形状臂 2 条 + 2 条负控 | 「写进去后 `restoreFromBinding` 不再回落」需真机 |
| A-03 | 形状臂（预判在 `selectEntry` 之前、`activeEntryId` 读取在返回名之前、`model_switch_not_applied` 存在）+ 2 条负控 | 同上：真机点选/ RPC 选型未端到端 |
| A-04 | `settleCompactOutcome` 4 臂字面量（marker / 摘要 / 两者皆无 → `Error`+`compact_failed:nothing_folded` / 未完成 → `Timeout`）+ 形状臂（marker 前快照 + `settleCompactOutcome`）+ 2 条负控 | ① 三条失败路径**共用**同一个 `error` 文案（原因不可辨，§6.1）；② 「模型抛异常」这条没有独立臂——它靠「无 marker、无摘要变化」这一对事实被覆盖，不是靠异常类型识别；③ 锚点无效归 `NoOp`（§6.2），未按 `Error` 验 |

**方法学说明**：把「成功」锚在**持久产物**（marker 行）而不是 flag 转移，是本支最值钱的判据形态；它同时解释了为什么 fake 装置必须让 `insertCompactMarker` **不**被调用（否则任何实现都能通过）。

---

## 8 残留与触发条件（等窗口，不修）

| # | 残留 | P 级 | 触发条件 / 说明 |
|---|---|---|---|
| R1 | 「FK ⇒ 父行不存在」只在 message 里读，不做事后事实核对（`getSession` 复查） | P2 | 复核一次要多一次 DB 往返；真实 SQLite 必给 message。若将来出现「message 被平台吞掉」的日志证据 ⇒ 升级为事实核对 |
| R2 | `UNKNOWN` 允许一次有界重放 | P3 | 真实路径不可达（无 message 不可发生）。它的存在只为「无法判断时不崩」 |
| R3 | 其他会话写入路径（`SessionForkManager.insertCompactMarker` / 衰减清理 / 导入）仍可能在父行消失时抛裸约束异常 | P2 | 触发条件：出现「非 appendMessage 路径的 FK 崩溃」日志。本支按半径纪律只修触发路径（appendMessage） |
| R4 | `requireSendableSession` 对草稿靠「驻留 VM」放行，若将来有「无屏草稿」路径 | P2 | 触发条件：出现 `session_not_found: __new__…` 的误报日志 |
| R5 | `firstUsableGroupEntry` 与 `ConfigBuiltins.firstUsableGroupMember` 两处循环 | P3 | A4 合入后若 `ConfigBuiltins` 有改动窗口 ⇒ 可合并为同一 helper |
| R6 | 真机行为级（V-1 端到端 / A-02 回落 / A-03 选型 / A-04 压缩） | P1-验证 | 下次真机窗口一次开齐；判据已写在 §7 |
| R7 | `chat_session_id_binding_check` criterion 18 的入口集按「VM 接收者派生的 `sendMessage/retryFromMessage/rerunFromToolBlock` + 绑定写函数调用者」派生 | P3 | 若将来出现新的追加消息 API（例如 `insertAssistant`）⇒ 门不会自动覆盖。触发条件：新增写消息的 VM 方法时重估判据 |

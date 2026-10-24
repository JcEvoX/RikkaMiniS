# dv28-FIX-A7 — `HeadlessChatRunner` 把上一轮的助手回复当本轮结果（A-07 · P1）

> 基线 `eea9f407` · 分支 `fix/dv28-a7-headless-stale-assistant`
> 提交 `4a3fb994`（生产+测试）/ `33db64e6`（门）· CI 见 §7

## 1. 缺陷与真树取证

三处「本轮结束」的收尾用同一句取答案（自跑 `grep -n` 取证，非沿用报告行号）：

| 入口 | 收尾取答案（基线） |
|---|---|
| `prompt` | `HeadlessChatRunner.kt:428` `val lastAssistant = msgs.lastOrNull { it.role == "assistant" }` |
| `retry` | `:545` 同上 |
| `rerunFromToolBlock` | `:664` 同上 |

`isStreaming` 翻回 false 只表示「没有流在跑」，**不表示本轮产出了助手消息**。真树证据链：

1. `ChatTurnPersistence.persistAssistantTurn`（`:62`）在 `parts.isEmpty()` 时 **`return null`** —— 空回合**不落任何助手行**；每条助手行由 `ChatRepository.appendMessage`（`:594`）以 `id = UUID.randomUUID().toString()` **新增**，故「本轮的行」有全新 id。
2. 失败回合**不会**新增助手行：`updateLastAssistantError` 只给**上一轮**那行贴 `error_info` 贴纸（`MessageEntity.errorInfo` 的 KDoc：「Terminal error sticker for an assistant turn」），行 id 不变。
3. 因此基线在三处都取到「整份 transcript 里最后一条 assistant」= 第 N-1 轮的答案，并把它当本轮 `status = "Completed"` 返回；`SessionsOffloadHandler`（`:323-356`）把 `responseText` 原样交给 `sendErrorReason`，RPC/CLI 侧无从分辨。

与 1044-A-03 同形：那次挡的是「send 被前置门丢弃」（`started == false`），这次是 **send 真的启动了、真的结束了，但没有产出本轮助手行**。注意 `lastOrNull{ role == "assistant" }` 会跳过末尾 user 行，所以「末尾是 user」本身不能当判据。

`grep -rn '"Completed"' src/android/app/src/ scripts/` 取证（改 status 文案前的要求）：唯一读点是
`SessionsOffloadHandler.sendSucceeded`（`:475`）`status == "Completed" || status == "Running"`，以及 `offload_cli_args_check.py` 里对同一表达式的判据。⇒ 新增状态词会改契约；本支**复用既有 `"Error"`**（与 `send_dropped_before_stream` 同一信封家族），只在 `responseText` 里给机器原因。

## 2. 修法（改动阶梯第 2 级：代码库里已有同类决策点）

**停在第 2 级，因为**：本轮身份不需要新机制 —— 仓内已有「信封 + 机器原因」的统一出口（`droppedSendResult()`，1044-A-03 建）和 Android 无关纯决策文件的惯例（`TurnAnswerPick.kt` 照 `ThinkingLevelWire.kt` 的形态）。没有引入新依赖、新配置项、新交互。

选的方案（任务书选项 1 的 id 版本）：

- **发车前拍快照**：`turnAnswerSnapshot(app, sessionId)` = 当前 transcript 里**助手行的 id 集合**；
- **收尾只接受快照之外的行**：`turnAnswerIndex(rows, priorAnswerIds)` 从尾部倒扫，返回第一条 `role == "assistant" && id !in prior` 的下标，无则 `-1`；
- **状态由「等到了吗 + 本轮有答案吗」两维决定**：`settleTurn(finished, answered)` → `Timeout` / `Completed` / `Error + no_answer_this_turn`。

**为什么是 id 集合，不是位置/上一条 id**（写进 KDoc 与报告，是本方案与任务书建议的 `turnStartIndex` 的偏离）：

- `retry` 会先删 `deletedCount` 行（`ChatViewModel.kt:3455` 附近 `deleteMessagesAfter`），`rerunFromToolBlock` 会按 block 边界截断 —— 发车时记下的 `msgs.size` 在下标意义上**被本轮自己改掉**；
- 更糟的是「上一条 assistant 的 id 不同即可」：截断**删掉的正是上一条答案**，于是「最后一条 assistant」会滑到更早的一轮 → 该判据会接受**更旧**的答案。集合成员关系对追加与删除都成立。
- 单测 `a truncated transcript cannot resurrect an older answer` 就是这条的反例臂。

保住的不变量：`!wait` 分支与 `started == false` 的信封一字未动；`withTimeoutOrNull(timeoutMs)` 语义未动（超时仍 `Timeout`，且 `responseText` 现在只可能来自本轮）；`PromptResult` 字段与 `SessionsOffloadHandler` 的消费契约未动；既有 6 个 `Headless*` 测试的锚点未动（`droppedSendResult()` 的 `GUARD_BLOCK` 保持原文）。

## 3. 改动清单

`git diff --stat eea9f407..HEAD`（4 文件 +497/−17）：

| 文件 | +/− | 角色 |
|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/debug/TurnAnswerPick.kt` | +83 | **新**：Android 无关的纯决策（`TurnAnswerRow` / `assistantIdsOf` / `turnAnswerIndex` / `settleTurn` / `NO_TURN_ANSWER_REASON`） |
| `.../debug/HeadlessChatRunner.kt` | +55/−13 | 三处快照 + 三处收尾 + `turnAnswerSnapshot` 私有 helper |
| `src/android/app/src/test/java/com/rikkaminis/app/debug/TurnAnswerPickTest.kt` | +124 | **新**：9 个 JVM 用例 |
| `scripts/scan/chat_session_id_binding_check.py` | +235/−4 | 判据 17 + 4 条带牙臂 + 夹具更新 |
| `fix-reports/dv28-FIX-A7.md` | — | 本报告 |

**门数 / 夹具数不变**（111 / 342，0 新建门）；`scan.sh` / `test_scan.py` 冻结面零触碰。

## 4. 门与判据（`chat_session_id_binding_check.py` · criterion 17）

派生轴（不写死变量名/成员清单/行号）：

1. **入口集** = 主体含 `isStreaming.first { !it }` 的每个成员 → 第 4 个结算入口自动被覆盖（selftest MUT-H4 实测）。
2. **本轮标识** = 在「发车点之前」由调用初始化的任意 `val`（发车点 = 等待标记之前最后一个 `recv.call(`）。
3. **答案选择必须消费该标识**：等待之后的调用若 ≥2 实参且其中之一是上面的绑定 → 通过；否则判「无法归因到本轮」。
4. **状态必须区分「本轮无产出」**：等待之后必须出现 `!= null` / `== null` / `isEmpty()` 之类的缺席判定。
5. **裸 role 扫描本身即红**：`lastOrNull { … "assistant" … }` 且 lambda 内无 id 差集（`!in` / `contains`）→ 独立一条违规（这正是 PRE 形状）。

每条违规文案都写明后果与修法方向，不写「criterion 17 failed」。

## 5. 验证四层

### L1 门层（实测）
- 本门真树：`python3 scripts/scan/chat_session_id_binding_check.py .` → `all criteria satisfied` rc=0；`gate_run.sh --gate … --repo .` → `ran=1 pass=1 fail=0 verdict=GREEN`。
- `--selftest` → `CHAT_SESSION_ID_BINDING_SELFTEST=PASS`，criteria 13/14/15/16 原有臂全绿，新增 4 臂各恰红其判据：
  - `MUT-H1`（答案选择改回裸 role 扫描）→ RED 1 条，文案含 `WITHOUT an id test`；
  - `MUT-H2`（快照挪到发车之后）→ RED 1 条，`never feeds this turn's identity`；
  - `MUT-H3`（状态只看 wait）→ RED 1 条，`derives its settled status from the wait alone`；
  - `MUT-H4`（**新增第 4 个**结算入口 + 裸扫描）→ RED 2 条，其中含 `rerunAll() settles its answer`。
- **PRE 臂**（`probe/make_pre.sh` 建树：其余文件取真树，**只把 runner 换成 `git show eea9f407:…`**）→ RED **恰 3 条**，逐条点名 `prompt()` / `retry()` / `rerunFromToolBlock()`。
- **单点变异**（PRE 树上再换回修复版 runner，**只**把 `retry` 的答案选择改回裸扫描）→ RED **恰 1 条**且只点名 `retry()`：牙齿精确到入口粒度，不是「整体一红」。
- 夹具一致性：判据 15 的 `drain` 锚点仍取 `loadMessages(` 首次出现（快照读走 `turnAnswerSnapshot` 私有 helper，**不在 `prompt` 体内出现 `loadMessages(`**），故 `HeadlessPromptDropContractTest.droppedGuardDecides` 与 criterion 15 均无需改判据即继续成立。
- 全量：见 L4（本地全量门交由分支 CI，理由见 §6-R6）。

### L2 邻门层（实测 rc=0，10 道）
按概念 `grep -rln "HeadlessChatRunner\|SessionsOffload\|isStreaming" scripts/scan/*.py` 取真实邻门：
`chat_ui_interaction_check` · `claim_release_guard` · `dao_loadmessages_check` · `debug_leak_guard` · `parts_json_funnel_check` · `offload_cli_args_check` · `schedule_date_source_check` · `stale_closure_guard` · `turn_tail_finalize_order` · 以及本门 → **10/10 rc=0**（逐个 `gate_run.sh --gate`，明细见 `/var/minis/shared/tools/gate-runs/20261024T01*/summary.tsv`）。

### L3 装置层（真 kotlinc 2.0.21 + 真 JUnitCore；未降级）
`probe/l3.sh`：生产切片按字节复制，测试类用真文件；编译期/运行期 classpath 分开对齐（`-classpath junit:hamcrest`；运行时 `out.jar:kotlin-stdlib:junit:hamcrest`）。

| 臂 | 结果 |
|---|---|
| POST（真 `TurnAnswerPick.kt`） | **OK (9 tests)** |
| PRE（= 基线语义：取最后一条 assistant） | RED 2：`a turn that wrote no assistant row is not the previous turn's answer` / `a truncated transcript cannot resurrect an older answer` |
| m1（取**第一条**新行而非最后一条） | RED 1：`a multi-round turn reports its last row, not its first` |
| m2（差集判据反转 `contains`） | RED 5（含三条身份用例 + 两条正例） |
| m3（无答案也报 `Completed`） | RED 1：`a settled turn with no answer is a structured error` |

自伤一次并当场闭环：初版 `assertTrue("a2" !in prior)` 写错了对象（应是「不在**截断后的** transcript 里」），POST 首跑即红并修；这正是「测试期望写死字面量、不与被测常量互推」的用处。

### L4 CI 层
见 §7（`head_sha` 三方逐字符 + 26 步 + `Summary` 恰一条 + `ALL 342` + 本支文件警告 0）。

## 6. 残留与失真

- **R1 真机欠账（P1）**：需一次 `minis-sessions send` 打到 provider 侧中途失败（或手工 `chat.cancel`），确认返回 `status=Error / responseText=no_answer_this_turn`、CLI 退出码非 0、`response_text` 字段不再出现上一轮答案。触发条件：下一次真机窗口（本波全部支线 R1 一次开齐时）。
- **R2 状态文案契约**：已核 `"Completed"` 读点（§1）。新增的 `no_answer_this_turn` 只出现在 `responseText`，`sendErrorCode` 仍返回 `SEND_FAILED`、退出码仍 `EXIT_FAILURE` —— 若将来有脚本按 reason 文本判分支，需要同步。触发条件：出现第一个按 reason 文本分流的消费方。
- **R3 第 4 个结算入口**：criterion 17 的入口集是从 `isStreaming.first { !it }` 派生的，`compact` / `resume` 这类**不以 isStreaming 收尾**的入口不在覆盖内（它们本来也不返回本轮答案）。触发条件：新增任何「等流结束再取答案」的入口（派生会自动覆盖，只要它沿用 `isStreaming.first { !it }`）。
- **R4 超时路径**：已带身份（`Timeout` + 只取本轮行），但**不做二次等待**：若 DB 落行与 `isStreaming=false` 之间存在极短竞态（`persistAssistantTurn` 在 `_isStreaming=false` 之前 await 完成，故当前实现不存在该窗口），会退化报 `no_answer_this_turn` 而不是等一会儿。触发条件：真机出现「明明有回复却报 no_answer_this_turn」的日志。
- **R5 每轮多一次全量 transcript 读**：快照用 `chatRepository.loadMessages(sessionId)`（未新增 DAO 查询，保持任务书约定的独占生产面）。长会话（>500 行）下每次 prompt/retry/rerun 多一次读；与排水期的那次读同量级。触发条件：长会话 RPC 时延出现可测回归。
- **R6 本地全量门未跑完**：沙箱本轮处 ENOSYS 风暴（`Function not implemented` 扩散到 `grep`/`sh`/`sleep`，registry S28 的并发窗口变体，同时有 9 支会话在跑），`scan.sh` 整链中途假红；按 registry 的健康路径改为「本门 + 邻门 + 变异负控本地跑，全量证据取分支 CI job 日志」。**这不是把假红写成真红**：CI 会跑 `scan.sh` + `test_scan.py` 全量。
- **R7 报告基线外的失真**：任务书写 `:428/:545/:664` 三处，实测基线三处行号为 `:428/:545/:664` ✓（逐行 `grep -n` 核对一致）；任务书点名的邻门全部存在（未出现 1047/1046 波那种「邻门名不存在」失真）。

## 7. 身份链

- 基线 `eea9f407f9e0e79f4902eeef1f84e1550ee5c29c`（main，`git log --oneline -2` 实测）
- 提交：`4a3fb994`（生产 + 测试）→ `33db64e6`（门）
- 分支 `fix/dv28-a7-headless-stale-assistant`
- CI：run **`40050292104`** / job **`120741243842`**（dispatch 2026-10-24T01:32:54Z，attempt 1）—— 判读 **ALL GREEN**（`probe/ci_judge_a7.py`，17 项）：
  - run/job conclusion success，26 步 `not_ok=[]`，唯一 skipped = `Publish to Releases`（main 门控）；
  - **代码面等价**：`git diff --name-only 33db64e6..HEAD -- . ':(exclude)fix-reports'` 为空（报告提交后 HEAD 已前移，见 §9）；
  - **远端 == 本地**：`ls-remote` 与本地 HEAD 逐字符一致（报告提交已推送）；
  - `Summary: 111 passed, 0 failed` 恰 1 条、坏 Summary 0；门头 `[1/111]..[111/111]` 互异 111 个；
  - `ALL 342 CASES PASS` 恰 1 处；` e: ` 编译错 0；游离 FAILED 0；
  - 本门 teeth（`CHAT_SESSION_ID_BINDING_SELFTEST=PASS`）+ real tree green 双在册；
  - `TurnAnswerPickTest` 9/9 PASSED；本支 4 文件警告 0。
  - 日志 1,457,364 B 存 `probe/logs/job-40050292104.log`。
  - 报告回填提交后代码面不变（见下方源码树等价声明）。

## 8. 自证成本与不确定项

- 真树取证 6 次 `grep -n`/`sed -n`；门 3 次迭代（首版漏了「夹具 prompt 没有发车调用 → 无快照 → 派生不出身份」，改为给夹具补 `sendMessage` 后才形成可判形状）；L3 5 臂 2 轮（第 1 轮 1 处自伤）。
- 不确定项：**真机上「本轮无产出」的触发频率未知**（审计报告给的是代码级路径）。修完后若该分支从未被触发，说明这条是「契约缺口」而非线上丢答案 —— 报告如实登记为 R1 的行为面待验，不宣称线上已修复用户可见症状。

## 9. 源码树等价声明

报告回填提交在 CI 绿之后，故 CI 验过的树是 `33db64e6`。实测：

```
$ git diff --name-only 33db64e6..HEAD
fix-reports/dv28-FIX-A7.md
$ git diff --name-only 33db64e6..HEAD -- . ':(exclude)fix-reports'
(空)
```

⇒ `src/` 与 `scripts/` 与 CI 绿树**逐字节一致**，差异仅本报告 `.md`（`fix-reports/` 不在 `build-apk.yml` 的 paths 内、不参与任何构建步骤）。CI 结论对当前代码树成立，无需为报告回填再跑一轮。

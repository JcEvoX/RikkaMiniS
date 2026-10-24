# dv28-FIX-B3 — `ChatViewModel.resume()` claim 时序违反同步声明契约（B-08）

支 **B3** / 工单 **B-08（P1）** / 分支 `fix/dv28-b3-resume-claim-timing`
基线 `eea9f407f9e0e79f4902eeef1f84e1550ee5c29c`

---

## 1. 缺陷与真树取证（自己跑出的行号，不采信报告）

基线树上（`git show eea9f407:…ChatViewModel.kt`）实测：

| 事实 | 命令 | 结果 |
|---|---|---|
| `_isStreaming.value = true` 的 4 个入口写点 | `grep -n "_isStreaming.value = true"` | `3166`（rerunFromToolBlock）/ `3413`（retryFromMessage）/ `3792`（sendMessage）/ **`4227`（resume）** |
| 只有 `resume()` 的 claim 在协程内 | 同上 + 首个 `launch(` 偏移 | resume 的第一个 `launch(` 是 `:4215` 的 reminder 落库协程 ⇒ claim 在它之后 |
| 该 shell 未被持有 | `grep -n "sendShellJob"` | 3 处：声明 `:1340`、`sendMessage` 赋值 `:3844`、`cancelStream` 取消 `:4112`；**resume 无赋值** |
| 阻塞点位置 | `:4222 val baseSystemPrompt = systemPromptForSession()` | 实体在 `:643`：缓存未命中即 `buildSystemPrompt()`（SoulStore / 记忆 / 技能目录读盘） |
| 门盲区 | `claim_release_guard.py` Check B | 只判「mint 早于 flip」，不判「flip 早于首个 `launch(`」，也不判 shell 是否被持有 |

**结论（三条同时成立，缺一不可）**：
1. `resume()` 的 `viewModelScope.launch(Dispatchers.IO)`（`:4221`）**没有**赋给 `sendShellJob`。
2. claim（`streamingClaimToken` / `_isStreaming` / epoch）在 `systemPromptForSession()` **之后**、且整段在 IO 协程里 ⇒ 从 `resume()` 返回至 claim 生效之间是**开放窗口**，`sendMessage :3751` / `retryFromMessage :3369` / `rerunFromToolBlock :3106` 的 `if (_isStreaming.value)` 守卫全部读到 false。
3. `cancelStream()`（`:4096`）在该窗口内 `streamJob?.cancel()`（此时 `streamJob == null`）与 `sendShellJob?.cancel()`（也为 null）都打不到东西 ⇒ 取消之后 setup 协程照跑并起流（「Stop 后诈尸」）。

**同族对照**：其余三个入口（send / retry / rerun）+ `ChatRetryOps.retryLast` 全部是「同步 claim → shell」，只有 resume 一个漏。

---

## 2. 修法（改动阶梯）

**停在第 2 级，因为**：`sendMessage` 已经把「同步 claim → shell 持有 → stale 检查 → 未起流则释放」这五步序列写好了，resume 的差异只是时序错位；复用它比新造一个抽象（如「TurnShell」类型）便宜，也避免与 3 个已合规入口分叉。第 3–7 级不需要（无标准库/平台 API 可用，也不能靠配置表达时序）。

具体：
1. **claim 提前到同步区**（`_forceScrollToBottom` 之后、`historyEndsWithAssistant` 之前）：`streamingClaimToken = Any(); _isStreaming.value = true; streamEpoch++; streamingClaimEpoch = streamEpoch; val sendEpoch; val claimToken` —— 顺序逐字对齐 retry / rerun 的既有形态（mint 先于 flip）。
2. **`sendShellJob = viewModelScope.launch(Dispatchers.IO)`**，体内先 `systemPromptForSession()`，再 stale 检查 `if (!isActive || !_isStreaming.value || sendEpoch != streamingClaimEpoch || claimToken !== streamingClaimToken) return@launch`（理由见 §6 失真 1：任务书称 sendMessage 有同款检查，**真树没有**）。
3. **外层 `finally` 释放**：`var streamLaunched = false` → 起流前置 true → finally 里 `if (!streamLaunched)` 且 `sendEpoch == streamingClaimEpoch && claimToken === streamingClaimToken` 才置 `_isStreaming.value = false`（逐字对齐 `sendMessage :3995-4003` 的既有形态；任务书把它描述成 `streamJob == null` 条件，实际是 `!streamLaunched`）。
4. **`appendMessage` 保守留在原处，但移到同步 claim 之后**（二选一的保守支）：窗口内取消就不会留下「未落库的 reminder 消息」；不动它还能保住「reminder 落库与起流解耦」的既有语义（激进并入 shell 会让 appendMessage 的失败连带吃掉 try 流）。
5. **`_isStreaming` 另外 6 处写点一行未动**（`grep -c "_isStreaming.value = false"` = 7 → 含本波新增的 setup-abort 释放；其余 4 个入口 0 改动）。

`[T-stale-finally-vs-new-claim]` 标记保留（claim 侧与 finally 侧各一处），`_canResume` 置 false 的时机不变（仍在 claim 之前）。

---

## 3. 改动清单

| 文件 | 增/删 | 说明 |
|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatViewModel.kt` | +138 / −66 | 生产：claim 前移 + shell 持有 + stale 检查 + setup-abort 释放 + `import kotlinx.coroutines.isActive` |
| `scripts/scan/claim_release_guard.py` | +372 / −2 | 门：新增 C-B8 / C-B8b（+ 各一条 ROT 臂）+ 9 个 selftest 夹具 |
| `src/android/app/src/test/java/com/rikkaminis/app/service/SessionActivityStreamErrorTest.kt` | +2 / −2 | **夹具源的隐式契约**：该测试用 `source.replace("catch (e: Exception) {\n<24 空格>AppLogger.error(…resume runAgentLoop…")` 构造变异臂，resume 体多一层 `try` 后该串失配 ⇒ 变异臂会静默变假绿，故同步缩进（1047-A3 同族） |

合计 **3 文件 +512 / −70**；**门 111 / 夹具 342 不变**（0 新建门，扩既有门）；`scan.sh` / `test_scan.py` 冻结面 **零触碰**。

---

## 4. 门与判据（扩 `claim_release_guard.py`，0 新建门）

**C-B8（同步声明）** —— 主体**派生**而非写死：
- 遍历 `ChatViewModel.kt` 的每个 `fun`（花括号匹配取函数体，注释/字符串已在 `load()` 阶段剥离，行号不变）；
- 凡函数体里出现 `_isStreaming.value = true` 即视为「回合入口」；
- 该写点的字符偏移必须**早于**同函数体内首个 `launch(`，否则红；
- `suspend fun` 跳过（它本身已在协程里 ⇒ 「claim 早于首个 launch」无意义，这正是 `ChatQueueInterruption.resumeQueueAfterCancel:516` 这类合法续接的形态）；
- 一行都找不到 claim 写点 ⇒ `C-B8-ROT`（改名/搬走时门大声失效，而不是静默放行）；
- 逃生口沿用既有的 `claim-ok: <reason>`（与 Check B 同一个 hatch）。

**C-B8b（shell 持有）** —— 判据锚**两个写点的包含关系**：
- 找 `streamJob = launch(` 站点，向上取**最近的外层 `launch(`** = 「setup shell」；
- 该 shell 必须是某个字段赋值的 RHS（`<field> = <receiver>.launch(…)`），且 `<field>` 在文件里至少被读一次（`sendShellJob?.cancel()`）；
- 无外层 `launch(` 的站点（suspend 续接，如 `ChatQueueInterruption:530` / `runRerunStreamTail`）不算 shell，不报；
- 文件里一个 `streamJob = launch(` 都没有 ⇒ `C-B8b-ROT`。

**带牙臂（selftest 27 夹具，其中 21 红 / 6 绿）**，本波新增 9 条：

| 臂 | 期望 |
|---|---|
| 修复后形态（claim 在前 + shell 持有） | exit 0 |
| 缺陷形态（claim 在 setup 协程内 + shell 未持有） | 红 2 条：C-B8 + C-B8b，**且 Check B 不红**（基线树本来通过 Check B） |
| m1 claim 滑回已持有的 shell 内 | **只**红 C-B8 |
| m2 去掉 `sendShellJob =` | **只**红 C-B8b |
| m3 留 shell 但无人读该字段 | 红 C-B8b（「nothing reads that field」） |
| `suspend fun` 里 claim 在 launch 之后 | 绿（precision：合法续接） |
| claim 写点消失 | C-B8-ROT |
| `streamJob = launch(` 消失 | C-B8b-ROT |

**PRE 臂**（`probe/base/` = 基线 `ChatViewModel.kt` 单文件树）：`rc=1`，**恰 2 条 finding** —— `[C-B8] :4227`、`[C-B8b] :4221`，其余判据 0 条（基线树本就过 Check A/B/C1/C3）。真实行号与审计报告一致。

---

## 5. 验证四层

**L1（门层）**
- 本门真树：`python3 scripts/scan/claim_release_guard.py .` → `✅ … no ungated release …` **rc=0**；
- `--selftest` → **`SELFTEST=PASS (27 fixtures: 21 breakers red, compliant/good arms green)`**；
- PRE 臂：见 §4，2 条精确 finding；
- 全量门：**110/110 PASS，fail=0，verdict=GREEN**（`probe/l1_all.log` 1–68 + `l1_p2.log` 69–92 + `l1_p3.log` 93–100 + `l1_p4.log` 101–110；`scan.sh` 的第 111 项 = `test_scan.py`，见下）；
- 夹具：**342 / 0 fail**（`test_scan.py` 342 例在本机被 ENOSYS 反复腰斩，改用等价分段执行并求和：A 143 + B 150（38+44+38+30）+ C 49 = **342**，各组 FAIL=0，日志 `probe/fx_*.log`）。分段执行只改变**调用分组**，不改任何夹具判据。

**L2（邻门）** `grep -rln "ChatViewModel" scripts/scan/*.py` 得 20 道真实邻门（任务书点名的 5 道全在其中），逐门 **20/20 rc=0**：`backup_remap_rollup` / `chat_session_id_binding` / `chat_ui_interaction` / `claim_release_guard` / `compact_decision_gate` / `compact_pin_truth` / `debug_leak_guard` / `discarded_return_guard` / `file_provider_paths_coverage_check` / `katex_ready_race` / `memory_gate_provider_fields` / `models_provider_field` / `open_url_broker_scheme` / `provider_boundary_guard` / `provider_rpc_symmetry` / `router_lock` / `session_provider_enabled` / `skill_restore_session_cleanup` / `stale_closure_guard` / `sysinfo_state_thread_check`。

**L3（装置层，降级 + 真树字节变异）**
- **降级理由**：`ChatViewModel` 是 God class（4757 行、Android/Compose/Room 依赖密集），沙箱无 Android SDK，无法编译；本波的修复**不引入任何决策函数**（没有可抽的纯判定 —— stale 检查是 `_isStreaming` / epoch / token 三个已有字段的合取，抽出去只会得到 `Boolean` 形参的镜像函数，属于为装置而装置）。因此 L3 = 「门 + 真树字节变异」，逐臂 sha256 对拍。
- 变异（`probe/l3_mutations.py`，内存字节快照还原，**未用 `git checkout --`**；文件 sha256 全程 `ea141d2f…`）：

| 臂 | 结果 |
|---|---|
| m1 把 `_isStreaming.value = true` 挪进 setup 协程（prompt 构建之后） | 门 rc=1，findings 恰 `['C-B8']`，sha 还原一致 |
| m2 去掉 `sendShellJob =` | 门 rc=1，findings 恰 `['C-B8b']`，sha 还原一致 |
| m3 删掉 stale-claim 提前返回（**反臂**：这是行为正确性不是形状） | 门 **rc=0**（如实证明判据半径，未假装有牙），sha 还原一致 |

**唯一不可信面**：`ChatViewModel.resume()` 的**运行期**行为（真机双流/诈尸）在本环境不可执行 —— 见 R1。

**L4（CI 层）** 见 §7。

---

## 6. 残留与失真

**失真（报告/任务书 vs 真树）**
1. 任务书称「体内先 `systemPromptForSession()`，然后 stale 检查 `if (!_isStreaming.value || streamEpoch != sendEpoch) return@launch`（与 `sendMessage :3909` 完全同构）」——**真树没有这个检查**：全文件 `grep "streamEpoch != sendEpoch"` **0 命中**，`:3909` 实际是 `ensureTrailingRoleAlternativeBeforeUserAppend()`。我的实现按语义补齐（`!isActive || !_isStreaming.value || sendEpoch != streamingClaimEpoch || claimToken !== streamingClaimToken`），并在报告里标明它是**新增**而非照抄；不补也不改变门判据（m3 证明门不看它）。
2. 任务书称 finally 形态是「`streamJob == null && streamingClaimToken === claimToken`」——真树是 `if (!streamLaunched)` 外层 + `sendEpoch == streamingClaimEpoch && claimToken === streamingClaimToken` 内层，我按真树实现。
3. 任务书「`resume()`（`:4177`）」等行号与基线差 1（真树 `fun resume()` 在 `:4178`），属正常漂移。

**残留**
- **R1（真机欠账，最高优先）**：缓存失效后点 Resume 立刻点发送 ⇒ 不得双流；窗口内点 Stop ⇒ 不得复活（`_isStreaming` 应保持 false 且不出现新的 assistant 增量）。**触发条件**：下一次真机窗口。
- **R2**：`appendMessage` 是否该并入 shell（本波取保守支）。**触发条件**：出现「reminder 与流式乱序 / reminder 丢失」的真实报告。
- **R3**：C-B8 对「已在协程内的合法续接」只按 `suspend fun` 豁免识别。**触发条件**：该文件内出现非 suspend 的协程内续接入口（届时需扩判据为「写入点是否位于某 `launch(` 的花括号内」）。
- **R4**：`cancelStream()` 是否也该等待 shell 收尾（本波不改）。**触发条件**：出现「取消后仍有一次系统提示词构建」的性能/耗电报告。
- **R5（同族，本波刻意不修）**：`ChatRetryOps.retryLast:51` 的 setup shell 同样未持有（bare `viewModelScope.launch`，`streamJob` 在 `:97` 才建）；`ChatRetryOps.retryFromMessage` / `ChatViewModel.rerunFromToolBlock` 的 shell 也未持有（setup 期只做 DB 读，无 prompt 构建）。C-B8b 的作用域被限定在 `ChatViewModel.kt`（本波独占生产面），故这三处不进判据。**触发条件**：出现「retry 期间点 Stop 仍会发一次请求」的真机报告，或下一波触碰 `ChatRetryOps.kt`。登记 backlog。
- **R6**：C-B8b 的「字段被读」判据只认「文件内出现该标识符且非声明/非赋值」，不区分读点是否在取消路径上。**触发条件**：`sendShellJob` 被挪到非取消路径读取（守卫会退化）。登记 backlog。

---

## 7. 身份链

- 基线：`eea9f407f9e0e79f4902eeef1f84e1550ee5c29c`（main，`git log --oneline -2` 父提交链上确认）
- 分支：`fix/dv28-b3-resume-claim-timing`
- 提交 / CI：见 `git log` 与本支回报（`run id` / `job id` / `head_sha` 三方逐字符一致）

---

## 8. 自证成本与不确定项

- 成本：沙箱本轮处在 8 个并行会话的高压窗口，`scan.sh` / `test_scan.py` 单进程全量**三次被 ENOSYS 腰斩**（`Function not implemented`，见 `/var/minis/shared/sandbox-limits.md` S28 变体），故改为「分段执行 + 重试」，并以分支 CI 的**单次全量 job 日志**作为最终全量证据（这正是 S28 变体③推荐的纪律）。
- 不确定项：①真机行为（R1）；②`isActive` 之外没有第二道「取消后不再起流」的结构性保证 —— 若 `launch` 在已取消的作用域里仍执行体（Kotlin 语义上不会），stale 检查是唯一防线；③`SessionActivityStreamErrorTest` 的变异臂依赖源码缩进，已在本次同步，但**任何后续再给 resume 加一层缩进都会再次静默失配**（建议未来把该臂改成结构无关的定位，登记 backlog）。

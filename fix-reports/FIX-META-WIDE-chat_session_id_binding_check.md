# FIX-META-WIDE · `chat_session_id_binding_check.py`

## 0. 门与基线

- 分支：`fix/meta-wide-05-chat-session` ｜ worktree：`/var/minis/shared/work/meta-wide/wt-05-chat-session`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`（开工时 `git status --porcelain` 干净、sha 逐字符一致）
- 报告来源：组 A（`/var/minis/shared/work/meta-audit/reports/wide-1024/A/Report_A.md`），机械复证 4 条门瞎实锤 / 1 条攻击被挡 / 0 条无效
- 本报告只动本门脚本：**生产 .kt 零改动**、冻结面（`scan.sh` / `test_scan.py`）零触碰、门数不变（112）

## 1. 轴

四条见证同轴：**dataflow** —— 门的判据锚「正确的 API / 正确的比较出现在效果体里」，不锚「被绑定的值就是那个 API 产出的值」。四条里三条的逃逸形状都用了同一个载体：把真值读出来、丢掉，再把一个常量/过期值绑到名字上，而那个名字周围仍然原样出现判据要找的子串。

## 2. 见证（find→replace 已机械复证：打上补丁后本门 rc=0）

| # | 判据 | 文件 | 逃逸形状 | 用户可见后果 |
|---|---|---|---|---|
| C8 | pendingHtmlPreview_activeSessionId | `ui/chat/ChatScreen.kt` | `val activeSessionId = sessionId.also { ChatViewModelStore.resolveSessionId(viewModel.currentSessionId) }` —— 调用子串原样出现，但绑定的值是过期的路由参数 | 新建草稿会话首轮生成 HTML 自动预览后，`pending.sessionId != activeSessionId` 用 `__new__<uuid>` 比较，预览弹窗被静默丢弃 |
| C13 | HeadlessChatRunner_viewModelForCancel | `debug/HeadlessChatRunner.kt` | `ownerFor(...).getOrNull()?.takeIf { false } ?: return null` —— `ownerFor(` 子串在，值恒 null | UI 创建的会话流式生成时，外部 `cancel` 永远拿到 null，停止不了正在生成的 UI 会话 |
| C16 | HeadlessChatRunner_applyModelOverride_existingBinding | `debug/HeadlessChatRunner.kt` | `existingBinding = null.also { ...getSession(sessionId)?.modelBinding }` —— 行上有 `existingBinding =` 且子串在，实参值是 null | headless 续聊一个绑定了专属模型的会话（未传 `--model`）时，绑定被静默抹平、强行套全局默认模型 |
| C14 | HeadlessChatRunner_await_sessionLoaded | `debug/HeadlessChatRunner.kt` | `vm.sessionLoaded.first { true }` —— 门只匹配 `vm\.sessionLoaded\.first\s*\{` 前缀，不读谓词 | 等待在初始 `false` 上立即返回（返回的是 `false` 非超时 `null`），下游超时检查也不触发，带病抢跑后又被异步完成的 load 覆盖状态 |

攻击被挡住（不动）：C6 (MoveToSessionSheet_key)，锚 anchor，改动后 rc=1。

## 3. 修法（判据从「存在」升级到「值/效果达成」）

全部判据改动落在本门脚本，共享助手 `_stmt_text()`（语句边界含深度 0 换行、括号/字符串字面量感知）：

1. **C8 → `_active_session_binding_violations()`**：读 `val/var activeSessionId =` 的 RHS（语句边界截断），两条判据：①RHS 头部是裸路由参数 `sessionId` → 红（`sessionId.also { resolver(...) }` 里 resolver 是mention 不是值）；②RHS 不从 `ChatViewModelStore.resolveSessionId(` / `ChatViewModelStore.activeSessionId` / `viewModel.currentSessionId` 派生 → 红（fail-closed）。绑定缺失 → fail-closed 红。
2. **C13 → `_owner_binding_violations()`**：`val owner =` 语句必须 (a) 来自 `ownerFor(`（值不是 mention），(b) 不含常量假谓词过滤器（`takeIf { false }` / `takeUnless { true }` / `takeIf { !true }` → 红），(c) 值必须到达 `ViewModelProvider(owner, …)`（`re.search(r"ViewModelProvider\s*\(\s*owner\b")`，跨行 `\s*` 吃掉换行）。绑定缺失 → fail-closed 红。
3. **C16 → `_named_arg_value()`**：bracket/字符串感知地读 `existingBinding =` 的**实参值**（不是整张参数表）：值头部是 `null`/`false`/`0` 字面量 → 红（行读被埋进表达式）；值不含 `modelBinding` → 红。整表 containment 兜底保留（positional 拼写仍被旧判据覆盖）。
4. **C14 → `_wait_predicate_violation()`**：bracket 匹配 `.first { … }` lambda 体，派生参数名（隐式 `it` 或 `(x: Boolean) ->`），**谓词体必须引用该参数** —— `{ true }` / `{ false }` 不引用 → 红；`{ it }` / `{ it == true }` / `{ ready -> ready }` 全部接受。lambda 解析失败 → fail-closed 红。

判据链自证：每条新臂断言「红恰好来自本判据」（锚点名在案）。

## 4. 四层证据

- **L1 本门**：真树 `python3 scripts/scan/chat_session_id_binding_check.py .` → rc=0（加固前后同绿）；`--selftest` → PASS，新增 4 臂全部按预期（C8/C13/C16/C14 各红 1 条且锚点名在案）。selftest 收尾标记逐字符未动。
- **L2 见证反攻**（唯一有效证据，两套独立脚本同跑）：`probe-05/l2_witnesses.py` + `probe-05/l2b_before_after.py` —— 10 条见证（本门 4 条）逐条：**同一条补丁在基线门（`git show 564c8f3e:...`）上 rc=0（瞎）→ 在加固门上 rc=1（抓住）**，restore 后 sha256 逐字节一致，且新红行精确点名本判据（C8「bare route arg」/ C13「constant-false」/ C16「not what the decision is fed」/ C14「ignores the value it waits for」）。
- **L3 邻门**：`session_sheet_id_binding_check` rc=0、`loop_persist_slice_check` rc=0。
- **L4 全量**：本地 `scan.sh` 受沙箱 S28 限制（壳内 ENOSYS，跑满 112 门不现实；36 门分段全绿 + 逐门直跑复核）——最终全量证据 = 分支 CI job 日志（CI 本就跑 `scan.sh` + `test_scan.py`，本门 marker 在 test_scan 注册表 :2862，新臂随 CI 被执行）。

## 5. CI run

- run `40134913784`（job `120994279978`）/ `fix/meta-wide-05-chat-session` / attempt 1 / **conclusion=success**
- `head_sha = 4677e42804b837920633457ba659b0eea0b30b5b` 与本地 `git rev-parse HEAD` 逐字符一致（本地 / API run / job 所属提交三方）
- 26 步：唯一 skipped = `Publish to Releases`（main 门控），其余 25 步 success，`not_ok = []`
- 日志判读（job log 1,484,366 B）：
  - `Summary: 112 passed, 0 failed`（带框线前缀）**恰 1 条**；另有 7 条子套件 Summary（28/5/14/36/10/22/10 passed）不是总账
  - `ALL 344 CASES PASS` 恰 1 处（夹具全过）
  - 门头 `━━━ [N/112]` 112 行、112 个互异显示名（门数未变）
  - ` e: ` 0 处（无编译诊断）
  - 含 `FAILED` 的 6 行逐条确认全是 **PASSED 用例名子串**（如 `slot denied produces FAILED without calling provider PASSED`），不是失败
  - 本门 marker `CHAT_SESSION_ID_BINDING_SELFTEST=PASS` 在日志中恰 1 次 ⇒ 本门 `--selftest`（含本次新增的抗见证臂）在 CI 里真的被执行
- 说明：本地全量 `scan.sh` 受沙箱 S28（壳内 ENOSYS）限制未能一次跑完（分段 36 门全绿 + 崩溃门逐门直跑复核 rc=0）；`scan.sh` 与 `test_scan.py` 的全量证据以上述 CI 读数为准（CI 本就执行这两条命令）。

## 6. 残留（三件）

- **触发条件**：其它门出现同族「判据只认调用/子串、不读绑定值」的形态（`.also {}` / `.takeIf {}` 掩埋真值）。
- **触发后方案**：把 `_stmt_text` + `_named_arg_value` + 「RHS 头部判据」抽成共享 helper（`scripts/scan/_stmtutil.py`），各门复用，避免三份近似拷贝漂移。
- **搁置理由**：本波硬约束不许动别的门的脚本；当前只有本支三门的判据需要这套取值逻辑，抽公共层是跨门改动，等第二个需求方出现再做。

另：C14 的见证补丁会同时被门内**多个** await 站点抓住（`prompt` 等派生入口共享同一谓词判据）——这是判据按派生入口生效的预期行为，不是误伤；报告保留原样不缩窄。

## 7. 复现

```bash
cd /var/minis/shared/work/meta-wide/wt-05-chat-session
python3 scripts/scan/chat_session_id_binding_check.py .            # 真树 rc=0
python3 scripts/scan/chat_session_id_binding_check.py . --selftest # 含 4 条新臂
python3 /var/minis/shared/work/meta-wide/probe-05/l2_witnesses.py "$PWD"   # 10/10 CAUGHT
python3 /var/minis/shared/work/meta-wide/probe-05/l2b_before_after.py      # old rc=0 -> new rc=1
```

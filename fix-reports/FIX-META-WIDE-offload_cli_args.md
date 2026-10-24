# FIX-META-WIDE · offload_cli_args_check（门见证加固）

- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`（wide-1024 波基线）
- 分支：`fix/meta-wide-04-offload`
- 范围：只改本门脚本；生产 `.kt` 零改动；`scan.sh` / `test_scan.py` 冻结面零触碰

## 1. 门

`scripts/scan/offload_cli_args_check.py` —— 离屏 CLI 参数契约门（单文件 40 余条判据）。本波加固的是其中三组：

| 判据组 | 契约 |
|---|---|
| `S1` | `FastioSearch.grepTree` 在 `limit <= 0` 时必须**在开 walk 之前**终结（答 `matchesCapped` + return），否则零预算也整树读取 |
| `S2`–`S5` | `sessions send` 的 `cmdSend` 必须**用**成功集做决策：失败分支先于 `emit("send")` 返回非零 |
| `L1`–`L4` | `android-clipboard clear` 必须**真的**走到平台 `clearPrimaryClip()`：`clearAction()` 的入参是 API 29 谓词，且两条写入臂可区分 |

## 2. 轴

三条见证同族，但落在三个不同的轴上：

- **dataflow / 决策点**（S1、S2）：旧判据问的是「这段源码里出现过 `limit <= 0` / `sendSucceeded(` 吗」，答对不等于**决策生效**。`if (limit <= 0 && false) { … }`、`if (false && !sendSucceeded(status))` 都保住全部子串，条件被编译期折叠，分支永不进入。
- **order → 终结性**（S1、S2）：守卫分支必须**离开当前函数**（`return`/`throw`），只设标志位或只包 error JSON 都会继续往下走。
- **anchor → 运行期可达性**（L2）：`clearPrimaryClip(` 仍在树里，但决策被钉成字面量 `false`，API 29+ 永远走空串臂——源码可达 ≠ 运行期可达。

## 3. 见证（外部模型给的原始刀，逐字）

### 见证 1 · S1
`FastioSearch.kt:206`
```
    if (limit <= 0) {
        out.matchesCapped = true
        return
    }
→  if (limit <= 0 && false) {
        out.matchesCapped = true
    }
```
旧门为什么绿：`check_fastio_search` 在 `<walk_at>` 前缀里只做两次子串测试（`limit\s*<=\s*0`、`matchesCapped = true`），两条都还在。
真实后果：`fastio search --max-matches 0` 仍然 `Files.walkFileTree` 整根目录并逐个文件读到 `MAX_GREP_FILE_BYTES`，只是最后答「已截断」——零预算的语义（不打开任何文件）完全丢失。

### 见证 2 · S2–S5
`SessionsOffloadHandler.cmdSend`
```
    if (!sendSucceeded(status)) {
→  if (false && !sendSucceeded(status)) {
```
旧门为什么绿：S2 只问 `sendSucceeded(` 在不在 `cmdSend` 体里；S3 只问 `errorEnvelope(` 在不在 gate 之后；S4 问 `sendExitFor(` 在不在；S5 问 gate 是否在 `emit(` 之前。四问全中。
真实后果：被拒的 send（Failed/Cancelled）落到成功路径——`emit("send", …)` + `exit 0`，调用方（agent）以为消息已投递，用户那边什么都没收到。

### 见证 3 · L1–L4
`ClipboardOffloadHandler.doClear`
```
    when (clearAction(Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)) {
→  when (clearAction(false)) {
```
旧门为什么绿：L2 只断言 `clearPrimaryClip(` 出现在 `doClear` 体里。
真实后果：Android 10+ 全走 `EmptyStringFallback`。写空串要么被平台拒绝（日志 `clear refused` 之外还要再看 payload：`cleared = true` 照样发）要么留下 `itemCount = 1` 的空条目，`status` 继续答 `has_content = true`。「清空剪贴板」对用户表现为「还在」。

## 4. 修法

新增三个解析辅助（本文件内，无共享库）：

- `_paren_close()` —— 括号配平；配不上 → -1（调用方报红，fail-closed）
- `_dead_condition()` —— 条件是否被常量折叠成 false：首项 `false` / `!true`，或 `&&` 链里出现裸 `false` 操作数
- `_if_site(text, cond_re)` —— 定位「条件里含 X 的 `if`」并切出它的**分支体**（支持块体与单语句体）

三处判据改写：

| 位置 | 旧 | 新 |
|---|---|---|
| `check_fastio_search` | 前缀子串 ×2 | 定位 `limit <= 0` 的决策点 → 条件不得常量折叠 → 分支体必须含 `return|throw`；定位不到 → 红（fail-closed） |
| `check_sessions_send_status` | `sendSucceeded(` 子串 | 调用在 → 必须存在由它决定的 `if` → 条件不得折叠 → 分支必须 `return|throw`；调用在但无决策点 → 红 |
| `check_clipboard_clear` | `clearPrimaryClip(` 子串 | 仍要求平台调用在树里 → 再取 `clearAction()` 实参 → 必须是 `Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q` 或其绑定的局部 `val`（`_is_sdk_q_decision`），其余一律红 |

selftest 新增 5 条臂（全部由见证原样固化 + 2 条等价形态）：

- `the success check is dead-collapsed (`false && !sendSucceeded(status)`)` → `{S2}`
- `the refusal branch falls through instead of returning` → `{S2}`
- `clearAction is fed a constant instead of the SDK predicate` → `{L2}`
- `the short circuit is dead-collapsed (`limit <= 0 && false`)` → `{S1}`
- `the guard sets the flag and falls into the walk` → `{S1}`

收尾标记 `OFFLOAD_CLI_ARGS_SELFTEST=PASS` 原样保留（`test_scan.py` 全等断言的冻结面）。

## 5. 四层证据

**L1 本门**
- 真树：`python3 scripts/scan/offload_cli_args_check.py .` → rc=0（PASS 行含 `grep budget stops a spent root before any file is opened`、`clipboard clear reaches clearPrimaryClip behind the shared four-guard shape`、`sessions … refuse invalid`）
- selftest：`--selftest` → `OFFLOAD_CLI_ARGS_SELFTEST=PASS`，rc=0（含新增 5 臂 + 既有全部臂）

**L2 见证反攻（唯一有效证据）** —— 见 §8 脚本，逐条：打补丁 → **旧门（`git show HEAD:` 版）rc=0** → **新门 rc=1** → 恢复 → sha256 一致

| 见证 | old rc | new rc | restore |
|---|---|---|---|
| S1 fastio grepTree budget guard | 0 | 1 | True |
| S2–S5 sessions send success gate | 0 | 1 | True |
| L1–L4 clipboard clearAction argument | 0 | 1 | True |

**L3 邻门**：`offload_host_path_check` / `offload_relative_path_unify_check` / `offload_media_b64_wire_check` 各自 rc=0

**L4 全量**
- `bash scripts/scan/scan.sh` → `Summary: 112 passed, 0 failed`
- `python3 scripts/scan/test_scan.py`（全量 344）：本机**两次**被沙箱 ENOSYS 窗口腰斩（registry S28：`subprocess.wait` → `OSError [Errno 38] Function not implemented`，同窗口还有别的会话在跑 `--full`）。改用两条互补证据：①**本地**只跑本支三门所属夹具组 `test_da1019_gates` / `test_da1022_gates` / `test_da1024_gates`（含 `selftest has teeth` 全等标记断言 + 真树绿）→ 全 PASS；②**CI job 日志** → `ALL 344 CASES PASS` 恰一条（干净机器，见 §6）。门数 112 / 夹具 344 均未变。

## 6. CI

run 40135342177 (#2635) · conclusion=success · job 120995575313 · head_sha=23fd18ec28008ec6757983ef82a98819a9e56010（与本地 HEAD 逐字符比对见 §5/§8）
判读要点：`Summary: 112 passed, 0 failed` 恰一条；`ALL 344 CASES PASS`；唯一 `skipped` = Publish（main 门控）。

机械判读（job 日志 1,484,240 B 逐条核过）：
- 门头 112 条、编号 1..112 **互异**、分母一致 = `112`
- `Summary: 112 passed, 0 failed` 恰 **1** 条
- `ALL 344 CASES PASS` 恰 **1** 条
- 编译错误（行首 ` e: `）= **0**
- 含 `FAILED` 的 6 行逐条确认全是 **PASSED 用例名的子串**（如 `slot denied produces FAILED without calling provider PASSED`）
- 本门 selftest 标记在 CI 日志中出现 **1** 次（门自证在 CI 里有人见证）
- 发布步骤未执行（分支门控，日志中无 Release 上传段）

## 7. 残留（三件套）

1. **`if` 被包装成等价但非字面的死条件**（如 `val never = false; if (never)`）
   - 触发条件：出现「决策条件被局部常量间接折叠」的实际提交
   - 触发后方案：`_dead_condition` 增加一层同文件 `val` 常量传播（只追一跳，避免全量数据流分析）
   - 搁置理由：本波零生产代码改动，且真实代码里无此形态；现在写等于给未发生的形态建装置。
2. **`clearAction` 实参改成三目/工具函数**（如 `isAtLeastQ(Build.VERSION.SDK_INT)`）
   - 触发条件：有人把谓词提进 util 后本门变红
   - 触发后方案：`_is_sdk_q_decision` 扩「谓词函数名 → 函数体含 SDK_INT >= Q」一跳解析
   - 搁置理由：方向安全（变红 = 有人来看），且此形态尚无实例。
3. **`emit(` 顺序判据（S5）仍基于首次出现位置**
   - 触发条件：`emit("send")` 被提进 helper 或提前到 switch 之前
   - 触发后方案：改为「从 gate 到函数尾的所有路径必须经过 return」的控制流判据
   - 搁置理由：S5 在本波见证里未被击穿（见证击穿的是 S2 的决策性），按半径纪律不动。

## 8. 复现

```
cd /var/minis/shared/work/meta-wide/wt-04-offload
python3 probe/l2_verify.py            # 11/11 CAUGHT（本支三门合并脚本）
python3 scripts/scan/offload_cli_args_check.py . --selftest
python3 scripts/scan/offload_cli_args_check.py .
bash scripts/scan/scan.sh 2>&1 | tail -3
```

单条见证的等价手写复现：把 §3 的 `→` 左侧替换成右侧，跑门；恢复用 `git checkout -- <file>` 并核对 `sha256sum`。

> `probe/` 未入库（`.gitignore` 覆盖）；`l2_verify.py` 留在本机同名 worktree 的 `probe/` 下，等价手写复现 = 「打补丁 → `git show HEAD:scripts/scan/<门>.py > /tmp/old.py` 跑旧门（期望 rc=0）→ 跑新门（期望 rc≠0）→ `git checkout -- <file>` + `sha256sum` 核对」。

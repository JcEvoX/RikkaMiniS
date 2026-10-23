# FIX-A3 — MCP OAuth `.secret` 移交链断裂（A-05）＋ 门表达式体截断盲区（A-06）

- 分支：`fix/dv27-a3-mcp-secret-handoff`
- 基线 main：`88ef9b2774cf14a0c983d5cb138db0e052ea7a83`（`git log --oneline -1` 实测，父提交为 `42af0ae6`）
- 工单：deep-audit-1047 / **A-05**（P1；`signOut` 分支按 P0 类凭据损失）+ **A-06**（P1）
- 独占门：`scripts/scan/mcp_oauth_lifecycle_check.py`、`scripts/scan/skill_import_offmain_check.py`
- 门数/夹具数：**111 / 340 不变**（本批 0 新建门，`scan.sh` / `test_scan.py` 零触碰）

---

## 1. 缺陷与真树取证

### 1.1 A-05 — 移交文件只在 UI 弹窗被消费（形状 30/10）

`mcp/oauth/MCPOAuthStore.kt` 的 `.secret` 移交契约写在 `importPendingClientSecret` 的 KDoc 里：
在客侧跑的 CLI 写不了加密存储，所以把客户端密钥放到 `<oauth-dir>/<name>.secret`，**宿主「读取或授权时」应导入并删除**。

真树取证（基线 `88ef9b27`）：

| 事实 | 取证命令 | 结果 |
|---|---|---|
| 唯一消费者是编辑弹窗 | `grep -rn "importPendingClientSecret" src/android/app/src/main` | 定义 1 处 + **仅 1 处调用**：`MCPIntegrationsScreen.kt:415` |
| `clientSecret` 只读存储 | `sed -n '74,75p' MCPOAuthStore.kt` | `fun clientSecret(...) = prefs(context).getString(secretKey(server), null)` |
| `signOut` 先删 bridge | `sed -n '135,138p' MCPOAuthStore.kt` | `remove(tokensKey)` → `deleteBridgeFile(...)` |
| bridge 删除会连 `.secret` 一起删 | `sed -n '446,453p' MCPOAuthStore.kt` | `File(bridgeDir(context), "$safe.secret").delete()` |

三条断链的后果（逐条可指认触发路径）：

1. **`reconcileForServer`（`:266`）** 读 `clientSecret` 得到**旧值或 null**，再把它回写进 `<server>.json` 桥接文件 ⇒ 客侧 CLI 拿到的是过期密钥；
2. **`MCPOAuthController.exchangeCode`（`:144`、`:185`）** 直调 `MCPOAuthStore.clientSecret`，换票表单漏 `client_secret` ⇒ **机密客户端 401**（`buildTokenExchangeBody` 的 `if (!clientSecret.isNullOrEmpty())` 分支根本进不去）；
3. **`signOut`（`:152`）** 在未导入时直接 `deleteBridgeFile` ⇒ 用户点一次「退出登录」就**永久销毁一个无法再推导的凭据**（只能回供应商重新申请）。`signOut` 的 KDoc 明写「keep the client secret」，这是**凭据损失**，不是清理缺口 —— 按 P0 类处理。

### 1.2 A-06 — 门在首个换行截断表达式体（判据盲区）

`skill_import_offmain_check.py` 的 `_fun_body_nc`（基线 474–500 行）关键一行：

```python
return text_nc[k:].split("\n", 1)[0], "expr"
```

跨行表达式体（`fun clientSecret(...): String? =` + 下一行才是表达式）被截成 `"="`，`method_io_state` 因此判 `'pure'`，`check_b04_offmain` 对**主线程裸调 `MCPOAuthStore.clientSecret` / `isAuthorized` 报 0 违规**。

复现（本支亲自跑，非采信报告）：

```
# 基线源 + 基线门脚本
BASE-OLD clientSecret ('pure', '`clientSecret` (expr body) has no I/O marker')
BASE-OLD isAuthorized ('pure', '`isAuthorized` (expr body) has no I/O marker')
```

同族影响面（真树实测）：`isAuthorized`（值在下一行）、`MCPOAuthStore.prefs`、`SkillRepository.importFromGitHub / updateFromURL / httpGetString / httpGetBytes / readBundledAssetHashes` 等 `= withContext(Dispatchers.IO) { … }` 形态同样被截断；`repo_method_wraps_io` 因只看 `kind == "block"`，对 `= withContext(...)` 恒 False。

---

## 2. 修法（走改动阶梯）

### A-05：停在第 2 级（代码库里已有该能力，收敛到单一出口）

没有新造第二份实现，也没有在每个调用点各写一遍导入：

1. **`clientSecret` 收敛为唯一出口** —— 先 `importPendingClientSecret`，有值即返回；否则回退 `prefs`。于是 `reconcileForServer` 与 `exchangeCode` **三处调用点一行未改**，全部自动继承交接语义（这正是「收敛出口」而不是「逐点补丁」的判据）。
2. **`signOut` 在 `deleteBridgeFile` 之前导入** —— 保证「待移交密钥先入库，再清桥接文件」。
3. **`purge` 刻意不导入** —— 删服务器时不该把密钥写回加密存储，否则留下已删服务器的孤儿凭据（违反既有门 S3 精神）。

保住了 `importPendingClientSecret` 的三条原契约（未动一行）：幂等、非法 server 名拒绝拼路径（`[sweep1014-D4a]`）、**导入成功才删文件**（`[audit-0917]`）。

### A-06：停在第 6 级（解析器最小重写，不重写整门）

只改「表达式体怎么取」这一个决策点，其余判据零改动：

- 新增 `_expr_body_nc(text_nc, masked, k)`：从 `=` 起**跳过空白与换行**，再按三条规则取整段表达式
  1. 花括号头形态（`withContext(...) { }` / `try { }` / `when { }` / `runCatching { }`）⇒ 用既有 `_match_brace` 取完整块；
  2. 否则按**括号深度归零 + 续行判定**读取（下一非空行以 `.` `?.` `?:` `&&` `||` 等续行符，或以 `catch` / `finally` / `else` 开头则继续）；
  3. **遇到声明边界立即停止** ⇒ 表达式体永不吞掉下一个函数（这是背景包 §6 #111/#157 已记过的假阳性形态）。
- `_fun_body_nc` 改为「参数表结束后看 body FORM」：`{` → block、`=` → expr，并允许声明跨行续（`:` / `=` / `{` / `where` 开头的下一行）。
- `kind` 返回值语义不变（`"expr"` / `"block"`），`repo_method_wraps_io` / `method_io_state` / 既有四套精度夹具全部零改动。

### 修复后状态变化的函数清单（如实记录，任务书 §2 要求）

| 函数 | 基线判定 | 修复后 | 真树是否新增红点 |
|---|---|---|---|
| `MCPOAuthStore.clientSecret` | pure（且 A-05 后自身带 IO） | **io** | 否 —— 唯二调用点（`MCPIntegrationsScreen:415`、`exchangeCode:144/185`）本就在 `Dispatchers.IO` / 屏幕侧 `withContext(Dispatchers.IO)` 内 |
| `MCPOAuthStore.isAuthorized` | pure（**盲区假阴性**） | **io** | 否 —— `MCPIntegrationsScreen:419` 已在 `withContext(Dispatchers.IO)` 内 |
| `MCPOAuthStore.tokens` | io | io | 无变化 |
| `MCPOAuthStore.prefs` | pure（表达式体截断） | pure（真·无 IO 标记；其 IO 由调用者 `prefs(...)` 标记承担） | 否 |
| `SkillRepository.importFromGitHub / updateFromURL` 等 `= withContext(...)` | 被截断 ⇒ `repo_method_wraps_io` False | 可识别为 IO 包装 | 否（这些函数本来是「仓库侧自行包装」的合法分支，见门 arm3/arm4） |

**全量 `scan.sh` 实测 0 新增红点**（111 passed / 0 failed，见 §5）。

---

## 3. 改动清单

`3 文件 +420 / −11`（生产 1 / 门 2）：

| 文件 | 增/删 | 内容 |
|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/mcp/oauth/MCPOAuthStore.kt` | +27 / −2 | `clientSecret` 收敛为单一出口（含 KDoc 说明为何收敛，而非逐点补丁）；`signOut` 在删 bridge 前导入 |
| `scripts/scan/mcp_oauth_lifecycle_check.py` | +220 / −0 | 新增 `check_secret_handoff`（C5a/C5b/C6）+ `_imports_pending_secret` / `_first_pos` 派生工具 + `_secret_handoff_readers`（把 `.secret` 交接读取者从令牌桥读取者中剥离）+ 3 个带牙夹具 + 4 条 selftest 断言 + `run()` 接线 |
| `scripts/scan/skill_import_offmain_check.py` | +182 / −9 | `_expr_body_nc` 新增、`_fun_body_nc` 重写为 body-FORM 判定、`OAUTH_STORE_FIXTURE` 扩为「多行表达式体 + 纯单行表达式体」双形态、3 条 B-04 带牙/精度夹具、5 条 A-06 解析器单元臂 |

未触碰冻结面：`scripts/scan/scan.sh`、`scripts/scan/test_scan.py`（`git diff --name-only` 为空）。

---

## 4. 门与判据（派生，不锚拼写）

### 4.1 `mcp_oauth_lifecycle_check.py` 新增 C5/C6

| 判据 | 内容 | 为什么这样派生 |
|---|---|---|
| **C5a** | `clientSecret` 的**函数体或其跳委派**必须含 `importPendingClientSecret(` | 判据锚「单一出口是否继承导入」，不锚行号、不锚调用点清单 —— 三个读者自动被覆盖，新增读者也自动覆盖 |
| **C5b** | `signOut` 体内 `importPendingClientSecret(` 的位置必须**早于** `deleteBridgeFile(` | 锚**语句顺序**而非「两个调用都存在」：顺序倒置时文件已被删，导入变成 no-op，这正是「形似生效」的形态（背景包 §8.7 第 16 条） |
| **C6** | `purge` 的调用闭包内**不得**出现 `importPendingClientSecret(` | 反向守卫：删除路径把密钥写回存储 = 孤儿凭据，与 signOut 的「必须导入」是同一判据的两面 |

**带牙臂（4 条，全部带手写期望集）**：

```
selftest compliant secret-handoff fixture              -> CLEAN
clientSecret without the handoff import                -> RED "does not import the pending"
signOut importing after the bridge delete              -> RED "BEFORE it imports"
purge importing the handoff                            -> RED "imports the pending `.secret` handoff"
```

### 4.2 `skill_import_offmain_check.py` 新增 A-06 臂

**B-04 带牙 + 精度臂（3 条）**：

```
dv27-A06 bare MCPOAuthStore.isAuthorized on Main       : RED
dv27-A06 bare MCPOAuthStore.clientSecret on Main       : RED
dv27-A06 precision: a pure expression body stays legal : CLEAN
```

第三条是**精度守卫**：真纯函数（单行表达式体 `validateServerName`）在主线程调用**不得**报红 —— 防「为修盲区而把纯度判反」的次生问题。

**A-06 解析器单元臂（5 条，期望值手写字面量，不从被测常量推导）**：

```
single-line expression body                                 : PASS
multi-line expression body                                  : PASS
chained continuation line                                   : PASS
expression body does not swallow the next function          : PASS
block-bodied function is a block                            : PASS
```

第四条是任务书点名的「不得吞下一函数」臂：`fun d(): Int = foo(4)` 之后紧跟 `fun e() { … }`，断言 `d` 的 body **不含** `fun e`。

**`.secret` 与令牌桥的读取者分离**（修门时报的一个真问题）：`importPendingClientSecret` 读的是 `<server>.secret`，与令牌桥 `<server>.json` 同在 bridge 目录、同样用 `readText(`，结构视图无法区分 ⇒ 新增 `_secret_handoff_readers`（在保留字符串内容的视图上按 `.secret` 判），从 C1–C3 的读取者集合中剔除。不分离会把 C2 判到**错误的文件**上（实测让「从不读桥」夹具报出 C2 假红）。

---

## 5. 验证四层证据

### L1 门层

| 项 | 结果 |
|---|---|
| `mcp_oauth_lifecycle_check.py .` 真树 | `PASS`，rc=0 |
| `skill_import_offmain_check.py .` 真树 | `OK …（15 arms …）`，rc=0 |
| 两门 `--selftest` | `MCP_OAUTH_LIFECYCLE_SELFTEST=PASS` / `SKILL_IMPORT_OFFMAIN_SELFTEST=PASS`，rc=0 |
| 全量 `sh scripts/scan/scan.sh` | **`Summary: 111 passed, 0 failed`**（rc=0，日志 `probe/l1/scan.log`） |
| `python3 scripts/scan/test_scan.py` | **`✅ ALL 340 CASES PASS`**（rc=0，日志 `probe/l1/test_scan.log`） |

**PRE 臂（基线树必红）**：`git worktree add probe/base-tree 88ef9b27` 后

```
C5/C6 对基线树（新门 + 基线源）：
  FAIL mcp_oauth_lifecycle_check
    - `clientSecret` does not import the pending `.secret` handoff — …
    - `signOut` does not import the pending `.secret` handoff — it deletes the bridge file …
A-06（基线门 + 基线源）：clientSecret / isAuthorized 双双 'pure' → 盲区复现
A-06（新门 + 基线源）：双双 'io' → 判据确实由解析器修复带来，不是源改动带来的巧合
```

> 辨析（必须写清，否则 PRE 不成立）：只跑「新解析器 + 基线源」不算 PRE —— 那是修好后的读数。PRE 的有效形态是**基线门脚本 + 基线源**（上表第二行），该形态实测 `'pure'`，与任务书描述一致。

> 完成后已 `git worktree remove --force probe/base-tree`。**这是必要时序**：`parts_json_funnel_check` 会把树内任何 `…/ChatRepository.kt` 副本当成真源码扫，第一轮全量门曾因此报 1 条**自造假红**（`probe/base-tree/src/.../ChatRepository.kt:470`）。清理后重跑 111/0。

### L2 邻门层

任务书点名的 3 个邻门名在真树**不存在**（`skill_file_save_result` / `file_uri_parse_guard` / `share_dir_prune` 均缺 `_check` 后缀），按概念重找后跑 **13 道真门全部 rc=0**：

```
offload_cli_args_check              rc=0
skill_restore_session_cleanup_check rc=0
provider_boundary_guard             rc=0
xproc_boundary_check                rc=0
readonly_mount_write_guard          rc=0
log_url_redaction_guard             rc=0
skill_file_save_result_check        rc=0
file_uri_parse_guard_check          rc=0
share_dir_prune_check               rc=0
entry_composite_key_guard_check     rc=0
offload_host_path_check             rc=0
offload_permission_reset_sync_check rc=0
skill_frontmatter_roundtrip_check   rc=0
```

### L3 装置层

**降级说明（任务书允许，理由必须写实）**：`MCPOAuthStore` 无法在沙箱 JVM 里驱动 —— 它要 `EncryptedSharedPreferences`（Keystore）与客侧 bind-mount 目录；仓库既有约定就是**不伪造 Context**（`MCPOAuthLifecycleTest` 的类注释逐字记录这一点）。因此 A-05 的行为级证据走「**门 + 真树字节级变异**」，A-06 的解析器走**python 级单元臂**（真跑，断言手写字面量）。

**A-05 真树变异臂（3/3，`probe/l3/mutate.py`）**：

| 臂 | 变异 | 门结果 | 还原 |
|---|---|---|---|
| m1 | `clientSecret` 去掉 handoff 导入，回到单行表达式体 | rc=1，`RED(expected)`（"does not import the pending"） | yes |
| m2 | `signOut` 把导入挪到 `deleteBridgeFile` 之后 | rc=1，`RED(expected)`（"BEFORE it imports"） | yes |
| m3 | `purge` 加一行导入 | rc=1，`RED(expected)`（"imports the pending `.secret` handoff"） | yes |

```
sha before=a80d0f6b460c6ec3eceb730b5a6d18c5962b45e78fab65f37c18a09b6da9628e
sha after =a80d0f6b460c6ec3eceb730b5a6d18c5962b45e78fab65f37c18a09b6da9628e
L3 ALL ARMS MATCH
```

每条臂**各只红其自己的判据**（三臂对应三条独立判据，无串扰），且**源文件字节级还原**（脚本 finally 兜底 + sha256 逐字符比对）。

**A-06 解析器臂**：5 条，见 §4.2，全部 PASS。断言的是**字面量**（`"foo(2)"`、`".bar()"`、`"fun e"`），不从被测常量推导 —— 否则常量一改期望跟着变，变异会伪装成 PASS。

### L4 CI 层

见 §7（提交后回填 run id / job id / head_sha）。

---

## 6. 残留与负决策

| 编号 | 内容 | 触发条件 |
|---|---|---|
| **R1** | 真机行为级未验：CLI 种 `.secret` → UI 授权 → 换票成功；`signOut` 后密钥仍在（`prefs` 有值、文件已删） | 下次真机窗口 |
| **R2** | `.secret` 文件权限：CLI 侧 `chmod 600` 由 CLI 保证，宿主导入后仅 `delete()`，不做权限复核 | 若发现移交文件权限宽于 600 且含真实密钥 |
| **R3** | `_expr_body_nc` 仍不认「链式跨多行 `?:` 块」中的某些极端形态（`if (x)\n  a\nelse b` 这类回退到首行的截断）；真树当前 0 例 | 若某函数判定从 io 退回 pure 且其体含跨行 `if/else` 表达式 |
| **R4** | `clientSecret` 现在是 IO 函数，若未来有**新**调用点写在 Main（Compose 组合体直接调）会被 arm7 拦下，但拦的是「有 IO 的调用点」而非「导入语义」 | 新增 `MCPOAuthStore.clientSecret(` 调用点且不在 IO 上下文 |
| **R5** | `_secret_handoff_readers` 按字符串 `.secret` 判，纯属对**文件名契约**的锚定；若移交文件名改变（`.secret` → 其它后缀），需同步改此处 | `.secret` 后缀被重命名 |

**负决策（刻意不做）**：
- **不在 `purge` 里加导入**（任务书明令）：删除服务器时写回密钥 = 孤儿凭据。
- **不把 `importPendingClientSecret` 从 `MCPOAuthStore` 抽到独立文件**：`_secret_handoff_readers` 与 C5/C6 都以「同一文件内的调用闭包」为判据基础，抽出会让判据失去可派生性，收益为零。
- **不为 A-05 引入 `oauthSecretReadPlan(kind)` 之类的纯逻辑序列**：那会为了可测性新增一层生产抽象，而真正的决策点（谁先谁后）已被 C5b 按语句顺序钉死、被 m2 变异臂证伪过。装置规模与影响面成正比即可（修复门纪律）。

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 main | `88ef9b2774cf14a0c983d5cb138db0e052ea7a83` |
| 分支 | `fix/dv27-a3-mcp-secret-handoff` |
| 提交 | 见下（提交后回填） |
| CI run / job | 见下（提交后回填） |

---

## 8. 自证成本与不确定项

- **自伤 #1（已闭环）**：首轮全量门报 `110 passed / 1 failed`，失败项是我自己留下的 `probe/base-tree/`（PRE 臂基线 worktree）被 `parts_json_funnel_check` 当源码扫。**教训：树内 probe 副本会被「按文件名遍历」的门误判为真源码**，PRE 臂用完必须立刻 `git worktree remove`。
- **自伤 #2（已闭环）**：把 `EXPR_PARSER_ARMS` 用 shell heredoc 写进 python 源时 `\\n` 塌成真实换行 ⇒ `SyntaxError: unterminated string literal`。改用「按行号重写 + `chr(92)+"n"` 拼转义」的脚本修好。**教训：往 python 源里写含转义的字面量，不要经 shell heredoc 二次转义**。
- **自伤 #3（已闭环）**：改 `GOOD_STORE` 夹具（加 `clientSecret`/`importPendingClientSecret`）后，`BAD_STORE_NO_BRIDGE_DELETE` 的 `replace` 源串失效（signOut 文本已变）⇒ 该臂变成「无变异」的假绿。修法：同步夹具源串。**教训：`.replace()` 型夹具的源串是隐式契约，生产形态一改全部失效，且失效表现为「夹具不再脏」而非报错。**
- **不确定项**：`clientSecret` 由纯函数变 IO 函数后，未来若有调用点遗忘 IO 包装，arm7 报的是「磁盘 IO 在 Main」而非「移交语义丢失」—— 两者判据不同，本支只保证了前者（R4）。

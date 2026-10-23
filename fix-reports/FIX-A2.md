# FIX-A2 — deep-audit-1046 S-02（MCP OAuth 桥接文件反向同步）

> 支 A2 / 工单 S-02 / 分支 `fix/dv26-a2-oauth-bridge-refresh` / 基线 main `f7aba218`

---

## 1. 缺陷与真树取证（基线 `f7aba218`）

`MCPOAuthStore` 与沙箱桥接文件 `/var/minis/mcp-servers/oauth/<server>.json` 是**同一份凭据的两个副本**，但只有宿主→沙箱一个方向。

真树证据：

| 位置 | 事实 |
|---|---|
| `MCPOAuthStore.kt:197-205` | `decideReconcileAction(urlTransport, oauth, hasTokens)` → `PURGE` / `REFRESH_BRIDGE` / `KEEP`（报告称 `RE_MATERIALIZE`，真树已改名，机制未变） |
| `MCPOAuthStore.kt:238-266` | `REFRESH_BRIDGE` 分支只读 `stored = tokens(context, server)`（`EncryptedSharedPreferences`）就调 `materializeBridgeFile(...)` ⇒ 把桥接文件覆写回**初始快照** |
| `grep -n 'readText' MCPOAuthStore.kt`（基线） | `:95`（读 `.secret` 交接文件）、`:340/:350`（rename 回退 `tmp.readText()`）——**0 处读回 token** |
| `transport/http.py:211-255` | 沙箱内 `minis-mcp-cli` 的 `_oauth_refresh` 在刷新成功后调 `_save_oauth_tokens(self.server_name, new_tokens)`（`:255`），重写桥接文件的 `access_token` / `expires_at`；**开启 Refresh Token Rotation 时还重写 `refresh_token`**（`:247-249`） |

触发链：用户在设置页保存该 server 的**任意其它字段**（timeout / header / 描述 / clientSecret）→ `MCPRepository.update`（`:397-407`）→ `reconcileOAuth`（`:426`）→ `MCPOAuthStore.reconcileForServer` → `REFRESH_BRIDGE` → 用宿主初始快照覆盖桥接文件。

后果：
- **最坏**：轮换后被旧 refresh_token 覆盖 ⇒ 沙箱内下次刷新 `invalid_grant` ⇒ 授权永久失效（用户必须重新走浏览器授权）。
- **常见**：每次保存多一次无谓的刷新往返（沙箱拿到旧 access_token，先 401 再刷新）。

「两个副本」这一判断在真树另有硬证据：`MCPOAuthStore.bridgeDir()` = `<filesDir>/minis-global/mcp-servers/oauth`，即宿主 bind-mount 给沙箱 `/var/minis/mcp-servers` 的同一目录。

---

## 2. 修法（走改动阶梯）

**停在第 6 级（最小充分实现：新写一个纯逻辑文件 + 在既有写路径接一处），因为**：

- 第 1 级「真要做吗」：要。凭据双副本的单向同步 = 数据单向流动，轮换令牌被旧值覆盖属 P1（触发路径明确：保存任一字段即触发）。
- 第 2 级「代码库里已有」：**没有**。全仓无任何桥接 token 读回路径（`grep readText` 见上）。
- 第 3 级「标准库」：`JSONObject` + `File.readText` 已有，无需新依赖。
- 第 4 级「平台原生」：无（这是应用内双向同步问题）。
- 第 5 级「已装依赖」：无新增。
- 第 6 级「能一行」：不能——需要「读回 → 判定谁更新 → 回写 prefs → 再 materialize」四步，且判定逻辑必须能脱离 Context 测。
- 第 7 级（最小实现）：达成。**新增 1 个 Android 无关纯逻辑文件 + 1 处接线 + 1 个纯逻辑测试文件 + 扩 1 道既有门**。

### 修法要点

1. **新增 `MCPOAuthBridgeMerge.kt`（Android 无关）**：`promoteTokensFromBridge(stored, bridge): TokenPromotion?`
   - 输入两个快照（宿主三元组 / 桥接三元组），输出「晋升后的三元组」或 `null`（不晋升）。
   - **两条守卫各司其职**：
     - **到期排序**：桥接到期秒 < 宿主到期秒 ⇒ **整条不晋升**（含轮换半边）。这是「宿主刚授权、桥接是旧副本」的形状，晋升它就是**回滚**。
     - **token 不等**：access_token 不同 ⇒ 晋升。**同秒刷新**的到期时间完全相同，只有不等比较能捕获它——把 `>` 写成 `>=`/`>` 的边界错误会让它静默失效。
     - **refresh_token 不等**单独守卫：轮换场景下 access_token 可能重复（未过期时轮换），但 refresh_token 已被换掉 ⇒ 必须晋升，否则下次刷新 `invalid_grant`。
   - 桥接**缺失 / 空白 access_token / 损坏** ⇒ 一律不晋升，**绝不**清空宿主合法 token（那会把「一次白费刷新」升级成「用户被登出」，是更重的反向 bug）。
2. **`MCPOAuthStore` 接线**：
   - `readBridgeTokenSnapshot()`：读桥接文件取三元组，`runCatching` 包裹，损坏只记 warning（**不记 token**，日志只有 server 名）。
   - `promoteFromBridge()`：**先 `setTokens` 持久化晋升结果**，再把返回的 tokens 交给调用方 materialize——只读不写会把回归推迟一次保存（下一次保存又读回被覆盖的三元组）。
   - 接线点选在 `REFRESH_BRIDGE` 分支**写桥接之前**（一处覆盖两条触发路径）。
3. **权威源不混**：桥接文件只提供 token 三元组；`client_id` / `token_endpoint` / `client_secret` / `resource` 永远以宿主 `cfg` + prefs 为准——`readBridgeTokenSnapshot` 连这些字段都不解析（门 C3 钉住）。
4. **幂等 + 失败静默**：晋升判定是纯函数，无副作用；文件读失败 `runCatching` 返回 null，走「不晋升」的原有路径。

### 必须保住 / 不得引入（逐条自查）

| 要求 | 结果 |
|---|---|
| `decideReconcileAction` 纯函数签名 + 三分支语义 | 一字未动（门 S2 仍绿） |
| `PURGE` 走 `clearAll`/`purge` | 一字未动 |
| `ArtifactBackupScope` 不备份 `mcp-servers/oauth/` | 一字未动（门 A1 仍绿） |
| `materializeBridgeFile` 原子写（tmp + rename） | 一字未动 |
| 桥接损坏/缺失时**不得**清空 prefs 中合法 `StoredTokens` | 纯函数对 null / 空 access_token 一律返回 null；`promoteFromBridge` 只在晋升成功时 `setTokens` |
| token 不得进日志 | `AppLogger.warning` 只带 server 名与异常消息（不含 token）；`info` 也不带 token |
| 不得写成主线程 IO | `reconcileForServer` 的三条触发路径全在 IO 线程：UI 保存 `withContext(Dispatchers.IO)`（`MCPIntegrationsScreen.kt:889-899`）、粘贴导入 `withContext(Dispatchers.IO)`（`:1079-1081`）、备份导入 `withContext(Dispatchers.Default)`（`BackupSettingsScreen.kt:345`）。新增的 `file.readText()` 与既有 `materializeBridgeFile` 的写文件在同一 IO 上下文中，未新增主线程 IO |

---

## 3. 改动清单

| 文件 | 类型 | 行数 |
|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/mcp/oauth/MCPOAuthBridgeMerge.kt` | 生产（新增，Android 无关纯策略） | +113 |
| `src/android/app/src/main/java/com/rikkaminis/app/mcp/oauth/MCPOAuthStore.kt` | 生产（读回 + 晋升 + 接线） | +79 / −4 |
| `src/android/app/src/test/java/com/rikkaminis/app/mcp/oauth/MCPOAuthBridgeMergeTest.kt` | 测试（新增，11 例） | +160 |
| `scripts/scan/mcp_oauth_lifecycle_check.py` | 门（扩既有独占门，C1/C2/C3 + 8 带牙臂） | +约 430 |

门数 / 夹具数**不变**（109 门 / 336 夹具，扩的是既有门，未改 `scan.sh` / `test_scan.py` 冻结面）。

---

## 4. 门与判据（扩 `scripts/scan/mcp_oauth_lifecycle_check.py`）

新增一组判据 **[dv26-A2]**（失败即红，源不可读 = 违规，fail-closed）：

- **C1** 物化桥接的那个函数，必须**经调用闭包**读到桥接文件（`readText(` + 桥接路径标记）**并**把晋升结果持久化（`setTokens(`）。两个阈值缺一不可：只读不写 = 回归推迟一次保存；只写不读 = 空操作。
  - 派生要点：**写者不算读者**——`materializeBridgeFile` 的 rename 回退里有 `tmp.readText()`，把它算成「读回」会让本门在**修复前**的树上直接绿（正是它要抓的形状）。
- **C2** 晋升规则必须存在**到期排序**守卫 + **access_token 不等**比较 + **refresh_token 不等**比较。字段名从**读回处**派生（哪个 JSON key 喂给哪个构造参数），不认字面拼写；局部别名（`val a = bridge.accessToken.trim()`）可解析回桥接字段。
- **C3** 读回方**不得**解析授权字段（`client_id` / `token_endpoint` / `client_secret` / `resource`，由 `materializeBridgeFile` 写入的键减去 token 三元组派生）。桥接是**令牌载体**，不是配置来源。

**带牙臂（8 条，各只红恰好一条）**：基线形态（materialize 不读）· 完全不读文件 · 读了但不持久化 · 读回方解析出 endpoint · 无排序守卫 · 无 access 比较 · 无 refresh 比较 · 策略文件缺失。另有**合规绿臂**。

**PRE 臂**：`git show f7aba218:…` 抽出基线树构 `probe/base/`（基线全部文件）与 `probe/base2/`（基线 + 新策略文件），门在两棵上**都红**（见 §5）。

---

## 5. 验证四层证据

### L1 门层

- 本门真树：`python3 scripts/scan/mcp_oauth_lifecycle_check.py .` → **rc=0**
  `PASS mcp_oauth_lifecycle_check (… a token the in-guest CLI refreshed is read back, promoted and persisted before the bridge is re-materialized; 17 form field state(s) keyed on the entry)`
- 本门 `--selftest` → **`MCP_OAUTH_LIFECYCLE_SELFTEST=PASS`**，新臂行：
  `selftest dv26-A2 bridge-promotion arms (no-read/no-file-read/not-persisted/reader-endpoint/no-ordering/no-access-cmp/no-refresh-cmp/no-policy) -> 1/1/1/1/1/1/1/1 violation(s)`（8 臂各恰好 1 条）
- **PRE 臂（基线树）**：
  - `probe/base`（f7aba218 全部基线文件）→ rc=1：`cannot read the bridge-merge sources … a token the in-guest CLI refreshed can be overwritten by the host's pre-refresh copy again`
  - `probe/base2`（基线 + 新策略文件，隔离 C1）→ rc=1：`C1 reconcileForServer (line 218) materializes the bridge without reading it through any helper …` + `C2 the bridge reader builds no snapshot type …`
- **全量门**：`sh scripts/scan/scan.sh` → **`Summary: 109 passed, 0 failed`**（rc=0）
  - 注：首次跑撞 PRoot `Function not implemented` 风暴（42 passed / 67 failed 假红），静置后重跑全绿——按 COMMON §5 不报假红。
- **夹具**：`python3 scripts/scan/test_scan.py` → **`✅ ALL 336 CASES PASS`**（rc=0）

### L2 邻门层（按概念反查，非任务书点名）

`grep -rln 'MCPIntegrationsScreen\|MCPOAuthStore\|MCPRepository' scripts/scan/*.py` → 7 道真实邻门，逐个真树跑：

| 门 | rc |
|---|---|
| `mcp_oauth_lifecycle_check.py` | 0 |
| `skill_import_offmain_check.py`（主线程 IO 禁令，覆盖本支新增的 `readText`） | 0 |
| `chat_ui_interaction_check.py` | 0 |
| `ime_inset_guard.py` | 0 |
| `session_sheet_id_binding_check.py` | 0 |
| `share_dir_prune_check.py` | 0 |
| `skill_restore_session_cleanup_check.py` | 0 |

外加 `offload_cli_args_check.py` rc=0。

### L3 装置层（真 kotlinc + 真 JUnitCore，5 臂）

装置：`probe/l3/gen.py`（字节级生成每臂，锚点断言 `count==1`，源文件全程不动）+ 每臂独立进程编译运行。

| 臂 | 变异 | 结果 |
|---|---|---|
| POST | 无 | **OK (11 tests)** |
| PRE | 整条规则体被替换为 `= null`（修复前语义） | **RED 6 例**（后期 / 同秒 / 轮换 / 无 refresh / 双未知到期 / trim） |
| m1 | `<` → `<=`（同秒不再晋升） | **RED 3 例**（同秒 / 轮换 / 双未知到期）——恰好命中边界例 |
| m2 | 删掉 refresh_token 不等比较（轮换半边失效） | **RED 1 例**（`a rotated refresh token is promoted even when the access token repeats`） |
| m3 | 删掉排序守卫（旧桥接可回滚） | **RED 2 例**（`an older bridge never rolls the host back` / `a bridge with no expiry cannot override a host copy that has one`） |

**结论**：每条变异恰红其判据，且 POST 全绿 ⇒ 装置有牙、期望值不是从被测常量推导。

### L4 CI 层

见 §7 身份链。

---

## 6. 残留与负决策

| # | 残留 | 触发条件（复活条件） |
|---|---|---|
| R1 | 晋升只发生在**保存路径**（`reconcileForServer`）。若用户从不保存任何 MCP server 的字段，宿主 store 里的 token 会一直是初始快照，直到下一次保存才被刷新过的副本取代 | 宿主侧出现「token 过期后仅靠沙箱内刷新续命」的可见症状（宿主 UI 显示已授权但实际调用 401）时复活 |
| R2 | 晋升的判定用**到期秒**排序；桥接与宿主到期秒相同时靠 token 不等区分。若服务端把 `expires_at` 写成本地时钟秒而宿主用的是别的时基，比较无意义 | 抓到跨端 `expires_at` 语义不一致的实测证据（同一 token 在两侧差一个固定偏移）时复活 |
| R3 | `readBridgeTokenSnapshot` 只在 `REFRESH_BRIDGE` 分支被调用；`KEEP`（已配置但无 token）分支不读桥接 | 出现「宿主无 token 但沙箱有可用 token」而 UI 不显示已授权的实报时复活（此时应由宿主主动授权，不该盲升） |
| R4 | 门的 C1 依赖调用闭包（≤ 全部可达函数）。若未来把读回放到**另一个模块**（跨文件），本门看不见 | 晋升逻辑被搬到 `MCPOAuthStore.kt` 以外的文件时，门的 C1 必须改锚 |
| R5 | 门的 C2 靠「字段名从读回处派生」。若读回方改为先 `optString` 到局部变量再构造，派生仍成立；但若改成读整个 JSONObject 后按需取字段，派生会失效 | 读回侧重构时同步改 C2 的派生逻辑 |
| R6 | `MaterializeBridgeFile` 写端仍信任宿主侧 payload（未加校验）。桥接文件被沙箱内进程恶意改写 `client_id` / `token_endpoint` 时，宿主仍会按自己保存的 cfg 重新物化（覆盖），但**在两次保存之间**沙箱可自行为主 | 出现「沙箱内进程篡改桥接文件即可改写刷新目标」的安全评估结论时复活 |
| R7 | 宿主 store 里的 refresh_token 仍是**加密存储**的长期凭据；本次修复只保证它不被更旧的副本覆盖，未引入过期清理 | 需要「宿主侧凭据轮换/到期清理」策略时复活（与 R1 同族） |
| R8 | 桥接文件的读取未做 symlink / 权限校验（只 `validateServerName` + `readText`）。桥接目录由应用自己创建在 `filesDir` 下 | 沙箱可写该目录 ⇒ 需要考虑恶意桥接文件；当前只信任其内容的三元组且只做「更晚才晋升」 |

**负决策（刻意不做）**：

- **不做双向合并器**（把桥接与 prefs 当两个可任意合并的文档）：权威源必须清晰——端点/客户端凭据永远在宿主，令牌三元组单向晋升。做成通用 merge 会让 `client_id` 之类字段出现两个来源，正是 C3 要防的。
- **不在 `tokens()` / `isAuthorized()` 热路径上做晋升**：这两个是 UI 与 reconcile 都会调的读路径，在那里读文件会把一次文件 IO 塞进 `isAuthorized` 的调用频次（`MCPIntegrationsScreen` 每次重组都调），且 `isAuthorized` 的语义是「宿主是否已授权」——让它偷偷改写凭据是更大的意外。
- **不新增门文件**：判据挂在已扫 `MCPOAuthStore.kt` 的独占门 `mcp_oauth_lifecycle_check.py` 上（COMMON §4 优先扩既有门），新建门还要主会话在并集阶段注册 + 重编号 + 补夹具，成本不成比例。

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f7aba218`（`d91fb8bc2e9c49af19a07356c9c74b4f`） |
| 父提交 | `f7aba218` ✔（`git log --oneline -1` 确认） |
| 分支 | `fix/dv26-a2-oauth-bridge-refresh` |
| 提交（代码+门） | `d95f41c3` |
| CI run | `39949090022`（run #2562，event=workflow_dispatch） |
| CI job | `120398876541`（attempt 1，**`conclusion=success`**） |
| head_sha | `d95f41c37c8f991e5773b70080dea6727a757cac` |
| 三方 sha 一致 | 本地 HEAD（`d95f41c3`）== `git ls-remote` == `run.head_sha` ✔ |
| attempt | 1 |
| 报告提交 | `e35408cb`（源码树与 CI 绿树等价：`git diff --name-only d95f41c3 HEAD -- . ':(exclude)fix-reports'` = **0 文件**） |

### L4 CI 判读（8/8 全绿，日志 1,430,496 B）

| 判据 | 结果 |
|---|---|
| 26 个步骤非 success | **0**（`not_ok_steps=[]`） |
| 唯一 skipped | `23 Publish to Releases`（main 门控）✔ |
| `Summary: 109 passed, 0 failed` | 恰 **1** 条（其余 Summary 均为 `0 failed`） |
| `ALL 336 CASES PASS` | 恰 1 条 |
| 门头 `[1/109]..[109/109]` | 109 条，序号连续，门名互异 109 |
| 编译错 `Z e: ` | **0** |
| 游离 FAILED | **0**（所有含 `FAILED` 的行都是 `PASSED` 用例名子串） |
| 本门真树 OK 行 | 在册（日志 :846，含新增的 "a token the in-guest CLI refreshed is read back, promoted and persisted …"） |
| 本门 selftest 牙线 | 在册（:1264 `MCP_OAUTH_LIFECYCLE_SELFTEST=PASS` + :1265 real tree green） |
| 新增 11 用例 | **11/11 逐条 PASSED** |

---

## 8. 自证成本与不确定项

1. **本支最大的自伤（已闭环）**：门的第一版把「写者」当「读者」——`materializeBridgeFile` 的 rename 回退里有 `tmp.readText()`，于是**基线树也会绿**，判据形同虚设。修法 = 写者排除。**这属通用形态：任何以「读了文件」为判据的门，必须先排除「写流程里的读」。**
2. **第二处自伤**：判据的派生一度用了**剥字符串**的视图，而桥接 key 名（`access_token` 等）正是字符串字面量 ⇒ 真树上「派生不出字段名」假红。修法 = 结构判据用剥注释视图、字面量派生用保留字符串视图，两套视图并存。
3. **第三处自伤（语义级）**：排序守卫最初写成「双方都有到期才比较」。L3 的 m1 臂暴露出这会让**服务端不返回 `expires_in`** 的 provider 永远晋升不了；而 CLI 的 `_save_oauth_tokens` 在无 `expires_in` 时**保留原值**，所以「桥接无到期」意味着「CLI 从未刷新过」⇒ 桥接一定更旧 ⇒ 应当**整条拒绝**（含轮换半边，因为被拒绝的桥接里的 refresh_token 正是被换掉的那一个）。最终形态：`bridgeSeconds < storedSeconds ⇒ 整条不晋升`，且到期为 0 视为最旧。改这一条时同步改了 L3 期望，**期望值写死字面量、不从被测常量推导**。
4. **不确定项**：R2（跨端 `expires_at` 时基）没有实测证据，只在代码层看到两端都用 Unix 秒；R1（宿主 token 长期不刷新）无真机验证——**真机行为级欠账见 backlog §1046-A2**。
5. **未做**：真机端到端（沙箱内 CLI 实际刷新 → 宿主保存 → 观察沙箱拿到的是新 token 而非旧快照）需要沙箱内跑一次真实 MCP OAuth 服务器，本支无环境，留作 §1046-A2 的 R1 真机欠账。

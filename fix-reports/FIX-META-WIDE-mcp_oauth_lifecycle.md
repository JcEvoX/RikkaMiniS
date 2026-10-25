# FIX-META-WIDE-mcp_oauth_lifecycle — 门见证加固（wide-1024 支 07）

## 1. 门

`scripts/scan/mcp_oauth_lifecycle_check.py` — MCP OAuth 凭据单收敛点（写路径 / 桥文件提升 / UI 表单），基线 `564c8f3e`。

## 2. 轴

| 见证 | 轴 | 判据锚缺陷 |
|---|---|---|
| R4（`urlTransport = true`） | dataflow | 只验「写路径到达收敛点」，从不读收敛点**实参** |
| C2（比较符翻转） | dataflow | 只验「体内存在任意顺序比较符」，不读守卫方向 |
| U3（谓词掏空） | dataflow | 只验「`none {` 前缀存在」，不读谓词体比较对象 |

## 3. 见证（9 条实锤中本门的 3 条，全部已机械复证）

1. **R4**：`MCPRepository.kt` 的 `reconcileOAuth` 把 `urlTransport = persistsAsUrlTransport(server)` 换成 `urlTransport = true` 后门仍绿。后果：STDIO 化的服务被判为 URL 传输，`reconcileForServer` 跳过 PURGE，`mcp-servers/oauth/<id>.json` 明文 Bearer Token 永久残留。
2. **C2**：`MCPOAuthBridgeMerge.kt` 把 `if (bridgeSeconds < storedSeconds) return null` 翻成 `>=` 后门仍绿。后果：沙箱侧刷新出的新 token 永远被丢弃，宿主拿过期 token 反复刷新 → `invalid_grant`。
3. **U3**：`MCPIntegrationsScreen.kt` 把 `none { it.id == server.id }` 换成 `none { it.id == "" }` 后门仍绿。后果：谓词恒真拦掉正常编辑（或恒假放行），已删除服务的 OAuth 状态被写回。

被挡住（判据承重，未动）：C5b（order 轴）。

## 4. 修法（生产代码 0 行改动，只改判据 + 固化臂）

- **R4** → 实参级 dataflow：枚举文件内每个 `MCPOAuthStore.reconcileForServer(` 站点，取 `urlTransport =` 实参；字面量 `true/false` 直接红；标识符回溯同文件 `val` 绑定；调用必须落在「函数体同时读 `isStdio` 与 `.url`」的谓词上（按函数体认定，不按名字）。`_expression_body` 覆盖表达式体谓词。无实参 / 无法配平 → fail-closed 红。
- **C2** → 方向 + 守卫形状：比较符必须出现在内层 `if (...)` 内（`_if_guard_around`），早退守卫必须落在「较旧侧」（`<`/`<=`，bridge 在左），正向守卫必须落在「较新侧」（`>`/`>=`）；其余形态（不在 `if` 内 / 两侧都不匹配）fail-closed 红。`!=`/`==` 的 token 比较仍按原判据（C2 原有三臂不变）。
- **U3** → 谓词体绑定：`setClientSecret(` 的第二个实参（被写入凭据的 id 表达式）必须与守卫谓词比较的元素 id 是**同一表达式**（归一化后 `param.id == id_expr`，lambda 参数名支持 `it` 以外命名）。`it.id == ""`（不引用被写入 id）与 `it.id != it.id`（自比较）都红。守卫正则同时收紧为 `none\s*\{`（原 `none \{` 错过改排版）。

新增 selftest 臂 3 条：`hard-coded transport fixture`（恰红 R4 一条）/ `flipped ordering guard fixture`（恰红 C2，含「exits early on the NEWER side」）/ `hollow presence-predicate fixture`。GOOD_REPO 夹具补上真实文件已有的 `persistsAsUrlTransport` 谓词（新判据按函数体读它）。

## 5. 四层证据

- L1 本门真树 `rc=0`（17 个 form field keyed，全判据绿）；`--selftest` → `MCP_OAUTH_LIFECYCLE_SELFTEST=PASS`（原 27 组臂全绿 + 新 3 组）。
- L2 反攻：3/3 CAUGHT（patch 前 rc=0 → patch 后 rc=1，restore 后 sha256 一致），见 `probe/reattack.py` 输出。
- L3 邻门：`shizuku_args_check`、`offload_cli_args_check` 各 rc=0。
- L4 全量：`scan.sh` → `Summary: 112 passed, 0 failed`；`test_scan.py` 344 例 0 失败（分批跑，见 §6 CI 段说明）。

## 6. CI run

分支 `fix/meta-wide-07-mcp-tools` @ `d1a9093124ea485ab90b80c167d8dbcf5fdc1ccd`（`head_sha` 与本地 HEAD 逐字符一致，attempt 1）。
run `40135996652`（#2637）→ **conclusion = success**，job `120997514627`，26 步唯一 skipped = `Publish to Releases`（main 门控）。
日志判读：`Summary: 112 passed, 0 failed` 恰 1 条 · `ALL 344 CASES PASS` 恰 1 条 · ` e: ` 0 · 含 `FAILED` 的 6 行逐条确认全为 PASSED 用例名子串。
本门自证在 CI 内被见证：`✅ mcp_oauth_lifecycle_check selftest has teeth (MCP_OAUTH_LIFECYCLE_SELFTEST=PASS)`（日志 1334 行）

## 7. 残留

- **R4 只认「谓词体读两事实」这一种形状**：若将来把 transport 判定改成查表/常量派生（如 `TRANSPORT.kind`），判据会误红。触发条件：真树在 R4 上红且代码语义正确。触发后方案：把「读 isStdio+url」扩成等价类（表驱动判定）。搁置理由：当前唯一实现是谓词，等价类无样本。
- **C2 只认 `if` 守卫一种承载**：`val newer = …; if (!newer) return null`（先绑定再判）会红。触发条件：真树出现该重构形态。触发后方案：谓词绑定回溯（同 R4 的 val 回溯）。搁置理由：现树无此形态，先绑定再判会引入新的假绿面。
- **U3 谓词只认 `==`/`!=` 比较**：`contains`/`any { }` 等价写法会红。触发条件：真树出现。触发后方案：扩比较符等价类。搁置理由：同上，无样本。

## 8. 复现

- 反攻脚本：`python3 /var/minis/shared/work/meta-wide/ops-07/reattack.py`（9/9 CAUGHT，restore 全 True）。
- 单门：`python3 scripts/scan/mcp_oauth_lifecycle_check.py .` ／ `--selftest`。
- 补丁/固化脚本：`/var/minis/shared/work/meta-wide/ops-07/patch_mcp_oauth.py`、`freeze_mcp_arms.py`。

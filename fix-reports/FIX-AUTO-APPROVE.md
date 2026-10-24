# FIX 报告：免确认模式 + 运行时权限一次批量授予（2026-10-24）

**分支**：`feat/auto-approve-mode` @ `42cc8b73`（CI run `40088536274` / job `120856296651` success，唯一 skipped = Publish 主门控）。**未合并 main**（含 .kt 验证面，起手权在用户）。

## 1. 缺陷（用户诉求）

用户经常被「要我点确认」打断：真机 2026-10-24 日志显示 minis-config 确认弹窗 3 次 120s 超时（12:40 / 12:46 / 13:08），agent 在后台写配置时无人响应弹窗，每次白等 120 秒后 `exit=124`。另 android-* 工具会在使用时逐个要权限。

## 2. 根因层

确认门本身没有问题（队列 / 超时 / 后台通知语义都正确），缺的是**用户级别的批量授权意愿的表达通道**——每次写入都要求逐条显式同意，没有「我信任常规操作」的模式。

## 3. 修法（按风险分层，不做一刀切）

新开关「免确认模式」（`AutoApproveStore`，三旗全默认关，UI-only）：

| 层级 | 行为 |
|---|---|
| NORMAL（14 项：日常开关/调参/外观） | 直接生效，不弹窗 |
| SENSITIVE（22 项：provider apiKey/baseUrl/oauthToken 等） | 默认仍确认；显式子开关才放行 |
| DESTRUCTIVE（2 项） | 任何组合下都确认 |

三处接线，各一个决定点：
1. `ConfigBridge.performWriteBatch` → `ConfigAutoApprovePolicy.shouldSkip()`（纯函数）。**`ConfigConfirmationGate` 内部一行未动**。空的 `Resolved.field`（集合动词如 `envvars.<key>`）按 SENSITIVE 兜底——不假设未分级的写入是常规操作。
2. `OffloadPermissionManager.getLevel` → `OffloadPermissionLevelPolicy.resolve()`（纯函数）。**用户手动设过的行永远优先于模式**（stored != null 直接返回）；`ASK_ONCE` 主开关放行；`a11y_cli`/`shizuku_cli` 独立子开关（不跟主开关绑定）。
3. `OffloadPermissionScreen` → 顶部新增「免确认模式」区块（长按外的确认对话框列清单）+「一次授予全部系统权限」按钮（`RequestMultiplePermissions` 批量申请 7 组权限；平台无"默认允许"API，只能减少询问时刻不能消除系统弹窗——文案如实说明）。

## 4. 安全边界

- `AutoApproveStore` **不注册进 ConfigRegistry**，agent 无法经 `minis-config` 自我放权（C4 门钉死）。
- 存 app 私有 SharedPreferences（沙箱实测 `/data/data` 不可达）。
- 每次跳过的写入仍写一行 `AppLogger.info`（含条数/风险/actor），配置变更审计保留。
- disableAll 关主开关时连带清两个子旗——防「下次开启时隐式继承旧子旗」。

## 5. 身份链

- 本地 HEAD == ls-remote == run.head_sha = `42cc8b73f8f1feeb47ee1fd4afeaacbf0f214a31`（逐字符一致）。
- 2 提交：`6482ade5`（实现）/ `42cc8b73`（@JvmStatic 编译错修复）。

## 6. 验证四层

- **L1 本地全量**：`scan.sh` **112 门 GREEN**（111 旧 + 1 新）/ `test_scan.py` **ALL 344 CASES PASS**（342 旧 + 2 新夹具函数）。
- **L2 新单测**：4 类 20 用例（`ConfigAutoApprovePolicyTest` 6 / `AutoApproveStoreTest` 4 / `OffloadPermissionLevelPolicyTest` 6 / `RuntimePermissionBatchTest` 4）本地 kotlinc+JUnit **OK (20 tests)**；期望值全为写死字面量。
- **L3 门带牙**：`auto_approve_bypass_check` 4 判据 + 5 变异臂（掏空判定 / 第二调用点 / 内联 getLevel / 注册表暴露 → 各自变红；compliant 臂 0 违规）selftest PASS。
- **L4 CI**：run `40088536274` success，26 步唯一 skipped=Publish（main 门控）；`Summary: 112 passed, 0 failed` 权威行恰一条；`ALL 344 CASES PASS` 恰一条；门头 112/112 全在；`Z e:` 0；游离 FAILED 0；新测试逐条 PASSED；**触碰文件警告 0**。

## 7. 首轮红（1 次，真错）

`AutoApproveStoreTest.kt:31` `@JvmStatic` 注解在类体 `@BeforeClass` 函数上（只能注解 companion 成员）→ 修复：删除该函数（复位已由 `@Before` 覆盖），提交 `42cc8b73` 重跑即绿。同族先例：kotlinc 严格性（backtick 测试名含 `.` 1046-F9 / 可空性警告 1042-A6）——**本地 kotlinc 编译测试源已是标准动作**，本次因补写 Store 测试时未先过本地编译直接推送。教训已并入流程：**所有新测试文件必须先本地 kotlinc 编译过再 commit**。

## 8. 文件清单

- 新增生产 4：`config/AutoApproveStore.kt`（157 行）/ `config/confirm/ConfigAutoApprovePolicy.kt`（46）/ `offload/OffloadPermissionLevelPolicy.kt`（52）/ `offload/RuntimePermissionBatch.kt`（75）
- 新增测试 4：`ConfigAutoApprovePolicyTest` / `AutoApproveStoreTest` / `OffloadPermissionLevelPolicyTest` / `RuntimePermissionBatchTest`
- 新增门 1：`scripts/scan/auto_approve_bypass_check.py`（约 280 行，含 5 变异臂 selftest）
- 修改 6：`ConfigBridge.kt`（+37/−2）/ `OffloadPermissionManager.kt`（+17/−6）/ `OffloadPermissionScreen.kt`（+137）/ `MinisApp.kt`（+4）/ `scan.sh`（+12 注册 +111 行重编号）/ `test_scan.py`（+18）
- strings 7 语言 × 17 条

## 9. 残留与边界

- **R1（真机行为级）**：免确认开启后，后台触发 `minis-config set` 不再出现 `ConfigConfirmGate timeout`，且日志有 `auto-approve: dialog skipped` 行——需真机验证。
- **R2（真机行为级）**：「一次授予全部系统权限」点击后系统弹窗逐组出现、拒绝某组后 Toast 计数正确、再次点击不再弹已授予组。
- **R3（平台约束，非缺陷）**：运行时权限的「一次批量授予」不能消除系统弹窗本身，只能把 N 次分散询问压缩为 1 次集中询问；特殊权限（无障碍/通知访问/电池优化）仍需去系统设置手点。
- **R4（刻意设计）**：DESTRUCTIVE 2 项永不可免确认——写死在判定函数，无旗标可绕过。
- **R5（待观察）**：SENSITIVE 子开关若被大量开启，agent 静默改凭据的风险回归——观察用户实际使用后再决定是否加「开启时警告通知」。

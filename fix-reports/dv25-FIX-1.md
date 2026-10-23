# dv25-FIX-1 · A-01（P1）MCP OAuth 读侧不认客机 CLI 写的 camelCase

> 本报告由主会话在收口时按 `backlog §1045-A01` + 分支提交记录 + CI 判读重建 —— 支 1 会话未落盘报告文件（编号与 1044 重号，原计划不入仓库）。

## 1 身份链

- 基线 main `1d44ec905b8f5d21a215694e9a13910c74e1f645`
- 分支 `fix/dv25-a1-mcp-oauth-keycase`，3 提交：`6bb83a20` 生产 / `143a88be` 测试 / `46b059be` 门
- 3 文件 **+651/−6**（生产 `MCPOAuthConfig.kt` +29 / 新测试 `MCPOAuthConfigTest.kt` 187 / 门 `mcp_oauth_lifecycle_check.py` +441）
- CI run `39914426611`（#2535）success，`head_sha=46b059be` 与本地/远端**逐字符一致（EXACT）**
- 门 109 / 夹具 336 不变；`scan.sh` / `test_scan.py` 冻结面零触碰

## 2 缺陷本体（回真树取证）

`MCPOAuthConfig.fromJson` 只读 snake_case（`client_id` / `authorization_endpoint` / `token_endpoint` / `redirect_uri`），而客机内 `minis-mcp-cli add --oauth-*`（`main.py:543-553`）写的是 camelCase。`fromJson` 是两端**唯一汇流点**（`MCPRepository.parseEntry:323`）⇒ CLI 写的块解析成全空 ⇒ `isConfigured == false`。又因 CLI **不写 `createdAt`** ⇒ `load()` 每次置 `needsPersist` ⇒ `save()` 经 `entryToJson`（`MCPRepository.kt:362-364`）的 `s.oauth?.takeIf { it.isConfigured }` 判 false ⇒ **oauth 块整块不落盘** ⇒ 用户配的 OAuth 端点在下次读盘/备份导入时静默消失（两个入口同源：CLI add → `reloadFromDisk`；备份导入 → `importJSON`）。

## 3 修法（阶梯第 6 级：接既有单源）

键表 `listOf(<snake_case>, <camelCase>)` + `firstNonBlank` 走查，主字段优先、别名兜底 —— 与 `MCPRepository.parseStartupTimeout:334-350` 的 primary-then-alias 同形。`toJson` 仍写 snake_case（旧配置/备份不变）、`isConfigured` 三非空判据与 `mode = "static"` 默认一字未动。禁区零触碰：`MCPOAuthStore` / `MCPRepository.kt` / CLI。

## 4 门（扩既有 `mcp_oauth_lifecycle_check.py`，0 新建门）

`check_config` 五条判据，成员集从真树派生：C1 覆盖（CLI 键集现场解析 `main.py` 的 oauth 字典与 `oauth["k"]=v`，guest 新增键自动红）/ C2 方向（snake 在前）/ C3 接线（别名表必须真被 `fromJson` 读到）/ C4 写侧只写 snake_case / C5「无端点=噪声」的 null 路径必须还在（必需属性从 `isConfigured` getter 作用域内派生）。11 个带牙臂违规数 `4/4/4/4/1/1/1/3/1/1/1`，各精确红一条判据组。

## 5 验证四层

- L1 本门真树 rc=0 + `--selftest` PASS + PRE 臂基线 blob 恰 4 条 C1 红 + `scan.sh` **109/0** + `test_scan` **ALL 336**
- L2 邻门 3/3 rc=0（`parts_json_funnel_check` / `provider_import_id_roundtrip_check` / `backup_streaming_export_check`）
- L3 真 kotlinc + JUnitCore 六臂：POST `OK (9 tests)` / PRE 精确 4 红 / m1 表翻转 1 红 / m2 删别名 4 红 / m3 `toJson` camel 2 红 / m4 删 null 守卫 1 红
- L4 CI #2535：26 步 non-success `[]`、唯一 skipped=Publish、`109 passed, 0 failed` 恰一条、门头 1..109 互异、`ALL 336`、`Z e: ` 0、游离 FAILED 0、`MCPOAuthConfigTest` 9/9 PASSED

## 6 报告失真 / 偏离

报告只写「CLI 写 camelCase 读不到」；真树取证后**触发面更宽**：`load()` 的 `needsPersist` 无条件回写使 oauth 块被整块抹掉（不只是读不到），且备份导入是第二个同源入口。

## 7 残留

R1 真机行为级（CLI 加 OAuth server → 刷新/备份导入后端点不蒸发）· R2 `needsPersist` 无条件重写（P2 既有设计）· R3 CLI 将来写 camelCase `scopes` 别名会自动红（派生集特性）· R4 `firstNonBlank` 用 `optString` 的宽容（P3）· R5 门 CLI 键集靠字面量解析（P3，方向安全）· R6 `mode="static"` 无 dynamic 实现（P3 设计空位）· R7 旧备份手写 camelCase 现在能读到（预期变化）· R8 门自伤：C5 全文件扫 `isNotBlank` 造出假属性 `it`（已修：限定 getter 作用域）· R9 L3 装置 kotlin-stdlib 必须显式上运行期 classpath（已修）· R10 门自伤：C1/C2 抢同一形态（已修：划清形态归属）。全带触发条件，见 `backlog §1045-A01`。

## 8 影响面

客机 CLI 写的 OAuth 配置不再在刷新/备份导入后消失；App 自身 snake_case 配置与旧备份行为完全不变。schema / 依赖 / 7+1 语言零触碰。

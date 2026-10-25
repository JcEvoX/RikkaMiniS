# FIX-META-WIDE · memory_gate_provider_fields_check.py（wide-1024 门见证加固 · 支 03）

- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`；分支 `fix/meta-wide-03-provider-registry`；加固提交 `d29cae00`
- 报告来源：组 A（`/var/minis/shared/work/meta-audit/reports/wide-1024/A/Report_A.md`）
- 机械复证：4 条门瞎实锤 / 1 条攻击被挡 / 0 条无效

## 1. 门
`scripts/scan/memory_gate_provider_fields_check.py`。守卫三个生产文件：`ui/chat/ChatToolExecutors.kt`（executeMemoryWriteTool / executeMemoryRollupTool 的禁用门必须终止 return）、`config/collections/ProvidersCollection.kt`（addCustomUserAgent resolver 必须被喂 payload；customUserAgent 写器 OAuth 守卫极性）、`config/ConfigBuiltins.kt`（providers summary 必须投影 inst.customUserAgent 真实值）。

## 2. 轴
dataflow ×4（无 return 终止、resolver 空参、守卫极性反转、summary 恒 Null）。

## 3. 见证（4 条，全部 PRE=BLIND → POST=CAUGHT）

### 见证 1 — C3 `executeMemoryRollupTool_gate`（dataflow · also_blinds=C2 executeMemoryWriteTool_gate）
```kotlin
// ChatToolExecutors.kt: memoryToolDisabledMessage(memoryEnabled)?.let { return ToolExecutionResult(...) } 删掉 return
```
- 门为何瞎：只查 `memoryToolDisabledMessage(memoryEnabled)` / `MEMORY_DISABLED_TOOL_TITLE` / `MemoryRollupTool.execute(repo)` 子串，不查 `?.let` 块内是否 `return`。
- 后果：记忆禁用时 rollup 构造的 ToolExecutionResult 被丢弃，继续执行 `MemoryRollupTool.execute(repo)` 写盘（ISSUE-07 复现）。

### 见证 2 — C7 `ProvidersCollection_add_customUserAgent`（dataflow）
```kotlin
// ProvidersCollection.kt: resolveAddCustomUserAgent((obj["customUserAgent"] as? ConfigValue.Str)?.value, credentialType) 第一参改 null
```
- 门为何瞎：只查 `resolveAddCustomUserAgent(` 调用与 `customUserAgent = customUserAgent` 子串，不查第一参是否从入参提取。
- 后果：`minis-config add providers` 一次性创建路径 customUserAgent 静默蒸发。

### 见证 3 — C8 `ProvidersCollection_oauth_guard`（dataflow）
```kotlin
// ProvidersCollection.kt: if (!customUserAgentWritable(inst.credentialType)) 反转为 if (customUserAgentWritable(...))
```
- 门为何瞎：只查窗口内出现 `customUserAgentWritable(inst.credentialType)` 与 `CUSTOM_USER_AGENT_OAUTH_MESSAGE`，不查逻辑非 `!`。
- 后果：API_KEY 实例正常 set 被拒、OAUTH 实例反被放行覆写 UA（极性反转）。

### 见证 4 — C9 `ConfigBuiltins_providers_summary_customUserAgent`（dataflow）
```kotlin
// ConfigBuiltins.kt: "customUserAgent" to (inst.customUserAgent?.let { ConfigValue.Str(it) } ?: ConfigValue.Null) 中 Str(it) 改 Null
```
- 门为何瞎：只查前缀 `"customUserAgent" to (inst.customUserAgent`，不查 let 块内构造值。
- 后果：顶层 `minis-config get providers` 批量读取 customUserAgent 恒 null（单路径 reader 仍正常，掩盖缺陷）。

### 攻击被挡住（判据承重，不动不删）
- `C6 (ChatPromptAndTools_rollup_forwards_flag)`（anchor）：patch 前 rc=0 / patch 后 rc=1。

## 4. 修法（生产代码 0 行，只改门判据）
- `_balanced` 括号配平 helper。
- 写/rollup 门必须 TERMINATE：`?.let` 块内必须含 `return`（C3/C2 族）。
- resolver 必须被喂 payload：`resolveAddCustomUserAgent(` 第一参必须从 `obj["customUserAgent"]` 提取（C7）。
- OAuth 守卫极性：`customUserAgentWritable(inst.credentialType)` 必须以 `!` 出现在条件里（C8）。
- summary 必须投影真实值：let 块内必须是 `ConfigValue.Str(it)`（C9）。
- witness 夹具 w1（BAD_ROLLUP_NO_RETURN）等 8 夹具接入 `--selftest`：MEMORY_GATE_PROVIDER_FIELDS_SELFTEST=PASS（8 夹具全命中）。

## 5. 四层证据
- L1 selftest：`MEMORY_GATE_PROVIDER_FIELDS_SELFTEST=PASS`（8 夹具）。
- L2 双臂：`probe-03/witness_pre.py`（基线树）→ BLIND；`probe-03/witness_attack.py`（修复树）→ CAUGHT；restore 后 worktree clean。存档 `probe-03/logs/L2_pre_baseline.txt` / `L2_post_fixed.txt`。
- L3 真树：`python3 scripts/scan/memory_gate_provider_fields_check.py .` rc=0。
- L4 CI：run #2631 / id `40134841119` / head_sha `d29cae000c81e3d229c6aac6ea4d0ef77ba514b0`（与本地 HEAD 逐字符一致）/ job `120994057972` success / attempt 1。本地 `scan.sh` 全量因沙箱缺陷（`/usr/bin/python3: Function not implemented`，见 `/var/minis/shared/sandbox-limits.md`）在 [22/112] 中断，未能本地完整跑完 112 门；L4 全量以 CI 内全量 scan.sh + test_scan.py 为权威证据。

## 6. CI run
- run #2631 / id `40134841119` / head_sha `d29cae000c81e3d229c6aac6ea4d0ef77ba514b0`（与本地 HEAD 逐字符一致）/ job `120994057972` success / attempt 1
- `Summary: 112 passed, 0 failed` 恰一条；`ALL 344 CASES PASS` 恰一条；` e: ` 0；26 步 skipped=['Publish to Releases']（main 门控），not_ok=[]

## 7. 残留（登记不修）
- 判据按 `?.let { … return … }` 形态锚定；`?: return` elvis 形态会被判 fail-closed 红（响亮不静默，不漏判）。触发条件：合法重构为 elvis 形态导致真树红。触发后方案：接受 elvis-throw 终止形态。搁置理由：fail-closed 方向安全，等真实发生再扩。

## 8. 复现
```bash
cd /var/minis/shared/work/meta-wide/wt-03-provider-registry
python3 scripts/scan/memory_gate_provider_fields_check.py .           # L3 真树 rc=0
python3 scripts/scan/memory_gate_provider_fields_check.py --selftest  # L1 MEMORY_GATE_PROVIDER_FIELDS_SELFTEST=PASS
python3 ../probe-03/witness_pre.py                                    # L2 PRE: BLIND
python3 ../probe-03/witness_attack.py                                 # L2 POST: CAUGHT
```

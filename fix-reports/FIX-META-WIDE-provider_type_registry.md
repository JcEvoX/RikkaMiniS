# FIX-META-WIDE · provider_type_registry_check.py（wide-1024 门见证加固 · 支 03）

- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`；分支 `fix/meta-wide-03-provider-registry`；加固提交 `d29cae00`
- 报告来源：组 A（`/var/minis/shared/work/meta-audit/reports/wide-1024/A/Report_A.md`）
- 机械复证：3 条门瞎实锤 / 1 条攻击被挡 / 0 条无效

## 1. 门
`scripts/scan/provider_type_registry_check.py`。守卫两个生产文件：`data/model/ProviderConfig.kt`（`ProviderType.modelsDevProviderIds` 的 when 映射）与 `provider/ModelsDevApi.kt`（`providerKeyMap` 派生链）。

## 2. 轴
completeness ×2（分支映射空表、键集丢列）+ dataflow ×1（派生值被掏空）。

## 3. 见证（3 条，全部 PRE=BLIND → POST=CAUGHT）

### 见证 1 — Arm 1d `required_non_empty_mappings`（completeness · also_blinds=Arm 1c）
```kotlin
// ProviderConfig.kt: find: ProviderType.xAI -> listOf("xai")  replace: ProviderType.xAI -> emptyList()
```
- 门为何瞎：docstring 点名 F-212，但 Arm 1d 必非空抽检名单漏掉 `xAI`。
- 后果：xAI 的 Grok 模型退回全表扫描，匹配到第三方聚合商的错误上下文窗口/思考档位（F-212 100% 复现）。

### 见证 2 — Arm 2b `providerKeyMap_derives_from_ProviderType`（completeness）
```kotlin
// ModelsDevApi.kt: flatMap lambda 中删除 type.displayName，键集只剩 name + legacyProviderNames
```
- 门为何瞎：只检查 `ProviderType.values()` / `modelsDevProviderIds` / `legacyProviderNames` 三个 token 子串，漏查 `displayName`。
- 后果：`"Google Gemini"` / `"Kimi Code"` 等 displayName 键全部失配（1026-D 原样复现）。

### 见证 3 — Arm 2c `no_re_hardcoded_vendor_keys`（dataflow · also_blinds=Arm 2b）
```kotlin
// ModelsDevApi.kt: .map { key -> key to ids }  replace: .map { key -> key to emptyList<String>() }
```
- 门为何瞎：只查 `.toMap()` 等子串出现，不查绑定到 `key` 的值是 `ids` 还是空表。
- 后果：全部厂商键映射到空列表，models.dev 元数据关联全丢，退化为全库盲扫。

### 攻击被挡住（判据承重，不动不删）
- `Arm 1c (when_has_no_else_and_covers_all)`（completeness）：patch 前 rc=0 / patch 后 rc=1。

## 4. 修法（生产代码 0 行，只改门判据）
- `map_literal_text` + `BRANCH_RE`：从 when 分支体提取字面量映射文本。
- Arm 1d：每个 `ProviderType.* ->` 分支映射必须非空（`emptyList()`/`emptyMap()` 即红），不再依赖手工抽检名单。
- Arm 2b：`providerKeyMap` 键集必须含 `displayName`（缺列即红）。
- Arm 2c：`.map { key -> … }` 绑定值必须是 `modelsDevProviderIds` 的局部绑定，字面量空表即红。
- 3 条 wide-1024 见证臂 + `FIX_MD_MISSING_KEY` 夹具接入 `--selftest`；修 IndentationError。

## 5. 四层证据
- L1 selftest：`PROVIDER_TYPE_REGISTRY_SELFTEST=PASS`（OK 2 arms clean）。
- L2 双臂：`probe-03/witness_pre.py`（基线树）打补丁后门 rc=0 → BLIND；`probe-03/witness_attack.py`（修复树）同补丁 rc≠0 → CAUGHT；restore 后 worktree clean。存档 `probe-03/logs/L2_pre_baseline.txt` / `L2_post_fixed.txt`（全 17 条）。
- L3 真树：`python3 scripts/scan/provider_type_registry_check.py .` rc=0。
- L4 CI：run #2631 / id `40134841119` / head_sha `d29cae000c81e3d229c6aac6ea4d0ef77ba514b0`（与本地 HEAD 逐字符一致）/ job `120994057972` success / attempt 1。本地 `scan.sh` 全量因沙箱缺陷（`/usr/bin/python3: Function not implemented`，见 `/var/minis/shared/sandbox-limits.md`）在 [22/112] 中断，未能本地完整跑完 112 门；L4 全量以 CI 内全量 scan.sh + test_scan.py 为权威证据。

## 6. CI run
- run #2631 / id `40134841119` / head_sha `d29cae000c81e3d229c6aac6ea4d0ef77ba514b0`（与本地 HEAD 逐字符一致）/ job `120994057972` success / attempt 1
- `Summary: 112 passed, 0 failed` 恰一条；`ALL 344 CASES PASS` 恰一条；` e: ` 0；26 步 skipped=['Publish to Releases']（main 门控），not_ok=[]

## 7. 残留（登记不修）
- 判据锚生产文件路径与常量名（`ProviderConfig.kt` / `ModelsDevApi.kt` / `modelsDevProviderIds` / `displayName` token）；生产侧改名或搬移 → fail-closed 红（响亮不静默）。触发条件：真树误红或生产重构改名。触发后方案：判据加别名表 / 从树派生目标文件。搁置理由：fail-closed 方向安全，等真实发生再扩。

## 8. 复现
```bash
cd /var/minis/shared/work/meta-wide/wt-03-provider-registry
python3 scripts/scan/provider_type_registry_check.py .           # L3 真树 rc=0
python3 scripts/scan/provider_type_registry_check.py --selftest  # L1 PROVIDER_TYPE_REGISTRY_SELFTEST=PASS
python3 ../probe-03/witness_pre.py                               # L2 PRE: BLIND（基线 564c8f3e）
python3 ../probe-03/witness_attack.py                            # L2 POST: CAUGHT（修复树 d29cae00）
```

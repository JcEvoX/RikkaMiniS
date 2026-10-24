# FIX-META-WIDE · models_provider_field_check.py（wide-1024 门见证加固 · 支 03）

- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`；分支 `fix/meta-wide-03-provider-registry`；加固提交 `d29cae00`
- 报告来源：组 B（`/var/minis/shared/work/meta-audit/reports/wide-1024/B/Report_B.md`）
- 机械复证：3 条门瞎实锤 / 1 条攻击被挡 / 0 条无效

## 1. 门
`scripts/scan/models_provider_field_check.py`。守卫三个生产文件：`provider/ModelListProviderAdapters.kt`（providerName 必须是族名不是实例 UUID）、`config/collections/ModelsCollection.kt`（provider 字段求值源）、`ui/chat/ChatViewModel.kt`（liveEffectiveModel 富化链可达性）。

## 2. 轴
dataflow ×3（右值被换、右值被掏、富化链短路）。

## 3. 见证（3 条，全部 PRE=BLIND → POST=CAUGHT）

### 见证 1 — Arm 3（providerName 值源）
```kotlin
// ModelListProviderAdapters.kt: providerName = ProviderType.kimiCode.displayName  replace: providerName = instance.id
```
- 门为何瞎：`adapter_delivers_name` 正则只判右值 `!= "null"`，`instance.id` 完全通过。
- 后果：Kimi Code 模型 provider 写成实例 UUID，`ModelsDevApi.enrichModel` 查表失败（1026 ISSUE-04 复现）。

### 见证 2 — Arm 1（ModelsCollection.add 值源）
```kotlin
// ModelsCollection.kt: provider = instance.providerType.displayName  replace: provider = instance.providerType.displayName.let { instance.id }
```
- 门为何瞎：check_arm1 只查（1）无字面量 `"provider = instanceId"`（2）含 `instance.providerType.displayName` 子串；`.let { instance.id }` 同时满足两条。
- 后果：落盘 `ModelEntry.baseModel.provider` 变实例 UUID。

### 见证 3 — Arm 6（liveEffectiveModel 富化链短路）
```kotlin
// ChatViewModel.kt: liveEffectiveModel() 单表达式体改为 if (true) currentModel else ...（富化链落死分支）
```
- 门为何瞎：check_arm6 只查 `ModelsDevApi.effectiveModel(`/`enrichModel(` 调用存在于函数体，不查可达性。
- 后果：用户能力覆写与 ModelsDev 元数据全部失效。

### 攻击被挡住（判据承重，不动不删）
- `Arm 4`（completeness）：patch 前 rc=0 / patch 后 rc=1。

## 4. 修法（生产代码 0 行，只改门判据）
- value-flow helpers：`value_expression`（解右值真实表达式，剥掉 .let/.also 丢弃块）+ `INSTANCE_ID_RE`（识别 `instance.id`/`instanceId` 值）。
- `_names_family`：Arm 3 判定族名（`ProviderType.*.displayName` / 字面量）vs 实例身份，`instance.id` 即红。
- Arm 1 升级：右值必须求值为 displayName（`.let { instance.id }` 即红）；`ARM1_FORBIDDEN` 判 `"provider = " + raw`（修 trim-only 重复报告）。
- Arm 6 升级：富化链必须可达——`if (true)` 等恒条件短路即红，`_activeEntryId.value` 必须被咨询。
- 3 条 witness 夹具与 witness arms 接入 `--selftest`；修 IndentationError 与 `CVM_A6_OK` NameError（夹具定义顺序）。

## 5. 四层证据
- L1 selftest：`MODELS_PROVIDER_FIELD_SELFTEST=PASS`（OK 6 arms clean，含 arm 6 dead-chain）。
- L2 双臂：`probe-03/witness_pre.py`（基线树）→ BLIND；`probe-03/witness_attack.py`（修复树）→ CAUGHT；restore 后 worktree clean。存档 `probe-03/logs/L2_pre_baseline.txt` / `L2_post_fixed.txt`。
- L3 真树：`python3 scripts/scan/models_provider_field_check.py .` rc=0。
- L4 CI：run #2631 / id `40134841119` / head_sha `d29cae000c81e3d229c6aac6ea4d0ef77ba514b0`（与本地 HEAD 逐字符一致）/ job `120994057972` success / attempt 1。本地 `scan.sh` 全量因沙箱缺陷（`/usr/bin/python3: Function not implemented`，见 `/var/minis/shared/sandbox-limits.md`）在 [22/112] 中断，未能本地完整跑完 112 门；L4 全量以 CI 内全量 scan.sh + test_scan.py 为权威证据。

## 6. CI run
- run #2631 / id `40134841119` / head_sha `d29cae000c81e3d229c6aac6ea4d0ef77ba514b0`（与本地 HEAD 逐字符一致）/ job `120994057972` success / attempt 1
- `Summary: 112 passed, 0 failed` 恰一条；`ALL 344 CASES PASS` 恰一条；` e: ` 0；26 步 skipped=['Publish to Releases']（main 门控），not_ok=[]

## 7. 残留（登记不修）
- `value_expression` 只剥一跳 .let/.also；多层 scope 链（`x.let{...}.also{...}` 嵌套）新形态可能漏判。触发条件：真树出现多层 scope 链站点。触发后方案：递归剥 scope + 深度上限 + fail-closed。搁置理由：现树无此形态，装置比例不当。

## 8. 复现
```bash
cd /var/minis/shared/work/meta-wide/wt-03-provider-registry
python3 scripts/scan/models_provider_field_check.py .           # L3 真树 rc=0
python3 scripts/scan/models_provider_field_check.py --selftest  # L1 MODELS_PROVIDER_FIELD_SELFTEST=PASS
python3 ../probe-03/witness_pre.py                              # L2 PRE: BLIND
python3 ../probe-03/witness_attack.py                           # L2 POST: CAUGHT
```

# FIX-META-WIDE · session_provider_enabled_check.py（wide-1024 门见证加固 · 支 03）

- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`；分支 `fix/meta-wide-03-provider-registry`；加固提交 `d29cae00`
- 报告来源：组 A（`/var/minis/shared/work/meta-audit/reports/wide-1024/A/Report_A.md`）
- 机械复证：4 条门瞎实锤 / 1 条攻击被挡 / 0 条无效

## 1. 门
`scripts/scan/session_provider_enabled_check.py`。守卫三个生产文件：`ui/chat/ChatSessionLifecycle.kt`（loadSession Priority 2/3 与 applyNewChatDefaultModel/restoreFromBinding 的模型绑定写点顺序与值源）、`ui/chat/ChatModelRouting.kt`（selectGroupEntry 值源）、`debug/HeadlessChatRunner.kt`（applyModelOverride 拒绝分支与 binding 写点）。

## 2. 轴
order ×1（写点逃出 Use 分支）+ dataflow ×3（enrichment 丢弃、plan 结果丢弃、拒绝分支恒短路）。

## 3. 见证（4 条，全部 PRE=BLIND → POST=CAUGHT）

### 见证 1 — C `loadSession Priority 2`（order）
```kotlin
// ChatSessionLifecycle.kt: _activeEntryId.value = entry.id 插到 decision 之后、if (decision is Use) 之前（逃出 Use 分支）
```
- 门为何瞎：只查 pre_decision 切片无状态写入 + 写点索引 > m_call.end()，不查写点是否落在 `is Use` 分支内。
- 后果：禁用 provider / 无 Key 的模型写入 `_activeEntryId`，污染顶栏模型选择器高亮与会话状态。

### 见证 2 — B-03 `providerModelFor enrichment`（dataflow）
```kotlin
// ChatSessionLifecycle.kt: val effectiveModel = providerModelFor(entry) 改为 val effectiveModel = entry.model.also { providerModelFor(entry) }
```
- 门为何瞎：只查 `providerModelFor(entry)` 存在 + `currentModel = effectiveModel`，不查富化结果是否被消费。
- 后果：新会话发消息丢失该 provider 全部自定义 ThinkingRule。

### 见证 3 — C-SEL-2 `selectGroupEntry`（dataflow）
```kotlin
// ChatModelRouting.kt: val resolvedEntryId = plan.effectiveEntryId ?: return 改为 val resolvedEntryId = entryId.also { plan.effectiveEntryId }
```
- 门为何瞎：只查 `plan.effectiveEntryId` 子串出现在 `if (plan.applyUi)` 块内。
- 后果：未验证的 entryId 直接写入 `_activeEntryId` 与会话持久化绑定。

### 见证 4 — C-SEL-4 `HeadlessChatRunner.applyModelOverride`（dataflow）
```kotlin
// HeadlessChatRunner.kt: if (decision !is SessionModelResolution.Use) 改为 if (false && decision !is SessionModelResolution.Use)
```
- 门为何瞎：子串/宽松正则搜索三个 token，`false &&` 保留全部子串。
- 后果：headless 入口强行绑定禁用 provider 模型并激活。

### 攻击被挡住（判据承重，不动不删）
- `C-SEL-1 (selectModelGroup)`（order）：patch 前 rc=0 / patch 后 rc=1。

## 4. 修法（生产代码 0 行，只改门判据）
- `_call_args` / `_assigned_from` helpers：判定赋值右值真实表达式（剥 .also{} 丢弃块），富化/plan 结果必须被消费。
- 写点分支围合：`_activeEntryId.value = …` 必须落在 `is Use` 分支内（order 轴升级为分支归属判定）。
- `check_binding_writes` 扩展覆盖 HeadlessChatRunner 的 `persistBinding`/`updateSessionBinding` 写点。
- 恒条件短路守卫：`if (false && …)` 等常量短路拒绝分支即红。
- G5-W1..W4 witness arms 接入 `--selftest`：SESSION_PROVIDER_ENABLED_SELFTEST=PASS。

## 5. 四层证据
- L1 selftest：`SESSION_PROVIDER_ENABLED_SELFTEST=PASS`。
- L2 双臂：`probe-03/witness_pre.py`（基线树）→ BLIND；`probe-03/witness_attack.py`（修复树）→ CAUGHT；restore 后 worktree clean。存档 `probe-03/logs/L2_pre_baseline.txt` / `L2_post_fixed.txt`。
- L3 真树：`python3 scripts/scan/session_provider_enabled_check.py .` rc=0。
- L4 CI：run #2631 / id `40134841119` / head_sha `d29cae000c81e3d229c6aac6ea4d0ef77ba514b0`（与本地 HEAD 逐字符一致）/ job `120994057972` success / attempt 1。本地 `scan.sh` 全量因沙箱缺陷（`/usr/bin/python3: Function not implemented`，见 `/var/minis/shared/sandbox-limits.md`）在 [22/112] 中断，未能本地完整跑完 112 门；L4 全量以 CI 内全量 scan.sh + test_scan.py 为权威证据。

## 6. CI run
- run #2631 / id `40134841119` / head_sha `d29cae000c81e3d229c6aac6ea4d0ef77ba514b0`（与本地 HEAD 逐字符一致）/ job `120994057972` success / attempt 1
- `Summary: 112 passed, 0 failed` 恰一条；`ALL 344 CASES PASS` 恰一条；` e: ` 0；26 步 skipped=['Publish to Releases']（main 门控），not_ok=[]

## 7. 残留（登记不修）
- binding 写点清单（`persistBinding`/`updateSessionBinding`）为手工登记；未来新增第三个 binding 写 helper 会绕过 `check_binding_writes`。触发条件：新增 binding 写 helper 或真树出现未覆盖写点。触发后方案：从树派生写点清单。搁置理由：当前仅两处，派生装置比例不当。

## 8. 复现
```bash
cd /var/minis/shared/work/meta-wide/wt-03-provider-registry
python3 scripts/scan/session_provider_enabled_check.py .           # L3 真树 rc=0
python3 scripts/scan/session_provider_enabled_check.py --selftest  # L1 SESSION_PROVIDER_ENABLED_SELFTEST=PASS
python3 ../probe-03/witness_pre.py                                 # L2 PRE: BLIND
python3 ../probe-03/witness_attack.py                              # L2 POST: CAUGHT
```

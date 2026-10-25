# FIX-META-WIDE · provider_models_cache_wiring_check.py（wide-1024 门见证加固 · 支 03）

- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`；分支 `fix/meta-wide-03-provider-registry`；加固提交 `d29cae00`
- 报告来源：组 B（`/var/minis/shared/work/meta-audit/reports/wide-1024/B/Report_B.md`）
- 机械复证：3 条门瞎实锤 / 1 条攻击被挡 / 0 条无效

## 1. 门
`scripts/scan/provider_models_cache_wiring_check.py`。守卫两个生产文件：`data/repository/ModelListProviderRegistry.kt`（fetchModels 必须把调用方 `context` 转发给 5 个 *ModelsApi）与 `provider/ProviderModelsCache.kt`（load / save / invalidate 必须真实读写 7 天磁盘缓存）。

## 2. 轴
dataflow ×3（context 遮蔽、load 短路、invalidate 短路）。

## 3. 见证（3 条，全部 PRE=BLIND → POST=CAUGHT）

### 见证 1 — Criterion 2（Registry forwards context）
```kotlin
// ModelListProviderRegistry.kt: fetchModels 函数体首行插入 val context: android.content.Context? = null（遮蔽形参）
```
- 门为何瞎：只查参数声明 + 每个调用点最后一参 == "context"，不查符号绑定作用域。
- 后果：5 个 *ModelsApi 收到 null context，7 天磁盘缓存读取/写入与 401/403 失效清理全跳过（deep-audit-1020 ISSUE-01 原样复现）。

### 见证 2 — Criterion 4a（ProviderModelsCache.load）
```kotlin
// ProviderModelsCache.kt: fun load 首行插入 if (true) return null
```
- 门为何瞎：只查 `"fun load("` 子串存在。
- 后果：缓存永远 miss，弱网/离线直接网络拉取失败。

### 见证 3 — Criterion 4c（ProviderModelsCache.invalidate）
```kotlin
// ProviderModelsCache.kt: fun invalidate 首行插入 if (true) return
```
- 门为何瞎：只查 `"fun invalidate("` 子串，不查函数体是否真的 `.delete()`。
- 后果：401/403 后脏缓存文件不删，旧模型列表继续展示。

### 攻击被挡住（判据承重，不动不删）
- `Criterion 2 (Registry forwards context)`（dataflow）：patch 前 rc=0 / patch 后 rc=1。

## 4. 修法（生产代码 0 行，只改门判据）
- `strip_comments`（判据在剥离注释后的源上匹配）+ `decl_slice`。
- Criterion 2 升级：fetchModels 函数体首行出现同名局部 `context` 声明（遮蔽形参）即红。
- Criterion 4a/4c 升级：`fun load(` 体必须真实执行 `keyFile(...)` 读路径；`fun invalidate(` 体必须含 `.delete()`；`fun save(` 同族加判。
- `cacheDir` 必须来自 `context.cacheDir`。
- 更新 `COMPLIANT_CACHE` 夹具 + B3 breaker 接入 `--selftest`（9 fixtures）。

## 5. 四层证据
- L1 selftest：`PROVIDER_MODELS_CACHE_WIRING_SELFTEST=PASS`（9 fixtures）。
- L2 双臂：`probe-03/witness_pre.py`（基线树）→ BLIND；`probe-03/witness_attack.py`（修复树）→ CAUGHT；restore 后 worktree clean。存档 `probe-03/logs/L2_pre_baseline.txt` / `L2_post_fixed.txt`。
- L3 真树：`python3 scripts/scan/provider_models_cache_wiring_check.py .` rc=0。
- L4 CI：run #2631 / id `40134841119` / head_sha `d29cae000c81e3d229c6aac6ea4d0ef77ba514b0`（与本地 HEAD 逐字符一致）/ job `120994057972` success / attempt 1。本地 `scan.sh` 全量因沙箱缺陷（`/usr/bin/python3: Function not implemented`，见 `/var/minis/shared/sandbox-limits.md`）在 [22/112] 中断，未能本地完整跑完 112 门；L4 全量以 CI 内全量 scan.sh + test_scan.py 为权威证据。

## 6. CI run
- run #2631 / id `40134841119` / head_sha `d29cae000c81e3d229c6aac6ea4d0ef77ba514b0`（与本地 HEAD 逐字符一致）/ job `120994057972` success / attempt 1
- `Summary: 112 passed, 0 failed` 恰一条；`ALL 344 CASES PASS` 恰一条；` e: ` 0；26 步 skipped=['Publish to Releases']（main 门控），not_ok=[]

## 7. 残留（登记不修）
- 判据锚缓存类表面（`fun load(`/`fun invalidate(`/`fun save(` 签名）与 `keyFile` 助手名；缓存类重构改名 → fail-closed 红。触发条件：真树误红。触发后方案：从树派生缓存类定义。搁置理由：fail-closed 方向安全，等真实发生再扩。

## 8. 复现
```bash
cd /var/minis/shared/work/meta-wide/wt-03-provider-registry
python3 scripts/scan/provider_models_cache_wiring_check.py .           # L3 真树 rc=0
python3 scripts/scan/provider_models_cache_wiring_check.py --selftest  # L1 PROVIDER_MODELS_CACHE_WIRING_SELFTEST=PASS
python3 ../probe-03/witness_pre.py                                     # L2 PRE: BLIND
python3 ../probe-03/witness_attack.py                                  # L2 POST: CAUGHT
```

# FIX-META · M2-b — `browser_tab_exec_serial_check.py`（轴：dataflow / 锁接收者身份）

- **分支**：`fix/meta-audit-m2-binding`
- **基线**：main `20a9fbadac1b0a78e3013e299c1694eaa2812e2e`
- **工单**：元审计环第一波 · M2-binding（12/12 实锤中的第 2 门）
- **停止节点**：分支 CI 绿（§6）。**未合并 main**，生产代码一行未动。

---

## §1 门与轴

| 项 | 值 |
|---|---|
| 门 | `scripts/scan/browser_tab_exec_serial_check.py` |
| 轴 | **dataflow**（`withLock` 的接收者是不是 per-tab mutex） |
| 见证文件 | `src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserTabPool.kt` |

## §2 见证摘要

```diff
         val mutex = lockForTab(tabId)          # ← 原样留着，成为死局部变量
         val result = withTimeoutOrNull(TAB_SERIAL_WAIT_TIMEOUT_MS) {
-            mutex.withLock {
+            Mutex().withLock {
```

门仍 rc=0。缺陷后果：每次 `executeSerialized(tabId, …)` 在**一次性新建的 Mutex** 上加锁，
目标 tab 的 `lockForTab(tabId)` 从未被获取，随后以 `implicitTab = false` 走 `runAcquiredAction`
的**无锁 else 分支**直接驱动 WebView ⇒ 显式 tab 路径与隐式 fan-out 落到同一 tab 时并发重叠
（ISSUE-03A 的窗口原样复活：导航与 JS 执行互相踩踏）。

**为什么门是瞎的**：`executeSerialized` 段只做**子串共存**判据 ——
`if "lockForTab(" not in s_src or "withLock" not in s_src: 红`。两个词都还在，于是绿。
它不问「`withLock` 的接收者是谁」，也不问这个接收者是不是 `lockForTab` 的返回值。

## §3 现状判据 → 新判据

### 现状

```python
s_src = src[ser[0]:ser[1]]
if "lockForTab(" not in s_src or "withLock" not in s_src:   # ← 同一函数体里两个词各出现一次即通过
    bad.append(...)
```

### 新判据（三处，全部「接收者身份」而非「词共存」）

1. **`executeSerialized` 的 `withLock` 接收者必须是 `lockForTab(…)` 的结果**
   （`val x = lockForTab(…)` + `x.withLock`，或直链 `lockForTab(…).withLock`）。
   解析规则：`withLock` 前向回溯取接收者表达式 —— `)` 结尾 ⇒ 平衡括号回溯出**被调用函数名**
   （`Mutex()` / `kotlinx.coroutines.sync.Mutex()` / `newMutex()` 全部落到「非 lockForTab」⇒ 红）；
   标识符结尾 ⇒ 必须在**同一函数体**内存在 `val|var x = lockForTab(`，否则红。
2. **implicit arm（`if (implicitTab) { … }`）同样按接收者判** —— 原来那里是
   `"lockForTab(" in arm and "withLock" in arm`，与 §2 同一个洞，只是换了个位置。
3. **fail-closed**：接收者解析不出（非常规形态）⇒ 红，不当作通过。

旧的「`tab.manager.execute(` 必须落在锁词法作用域内（或 else 臂）」判据**原样保留**，一条没动。

## §4 改了什么（diff 骨架）

```
scripts/scan/browser_tab_exec_serial_check.py | 187 +++++++++++++++++++-
```

- 新增 `withlock_receivers()`（平衡括号 + 后向名解析）、`receiver_is_per_tab()`（绑定查证）
- `executeSerialized` 段：子串共存 → 接收者身份（三类诊断信息：不可解析 / 非 per-tab 工厂 / 标识符未绑定）
- implicit arm 段：同一判据
- selftest 增 4 臂；**收尾 marker 保持原文**（见 §7 R1）

## §5 四层验证

### L1 — 本门真树 + selftest

```
$ python3 scripts/scan/browser_tab_exec_serial_check.py .
PASS browser_tab_exec_serial (runAcquiredAction's WebView-driving execute sits under a per-tab mutex)   # rc=0

$ python3 scripts/scan/browser_tab_exec_serial_check.py --selftest
SELFTEST=PASS (browser_tab_exec_serial: 3 fixtures)
SELFTEST_ARMS=PASS (browser_tab_exec_serial: +4 receiver-identity arms)
```

新 4 臂：

| 臂 | 期望 | 实测 |
|---|---|---|
| `witness: Mutex().withLock receiver`（见证原文） | 红 | `:23 executeSerialized locks on Mutex.withLock — a Mutex(...) result is not the per-tab mutex` |
| `witness: implicit arm on Mutex()`（同缺陷换位置） | 红 | `:9 the implicit arm locks on Mutex.withLock — not lockForTab(tab.id)` |
| `direct chain lockForTab(id).withLock`（直链形态） | **绿** | 0 violations（保证新判据不误伤合法写法） |
| `non-per-tab factory receiver`（`newMutex().withLock`） | 红 | `:23 … a newMutex(...) result is not the per-tab mutex` |

原有 3 个夹具（`GOOD` / `BAD_NO_LOCK` / `BAD_WRONG_SPAN`）与 `stripped` 臂全部维持原结论。

### L2 — 机械复证

```
$ python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
      /var/minis/shared/work/meta-audit/probe-m2/reports/report-B.md \
      --repo /var/minis/shared/work/meta-audit/wt-m2
── browser_tab_exec_serial_check.py
   [1] -        rc=1  restore=True  → ❌ 门抓住了（见证无效）
        FAIL src/android/…/BrowserTabPool.kt:917: executeSerialized locks on `Mutex.withLock` — a `Mutex(...)` result is not …
```

`restore=True`。修前同一条命令 `rc=0 → GATE_BLIND_CONFIRMED`。

### L3 — 全量门禁 + 全量夹具

```
GATE_RUN mode=full ran=112 pass=112 fail=0 sandbox=0 envretry=0 verdict=GREEN
python3 scripts/scan/test_scan.py → ✅ ALL 344 CASES PASS
```

### L4 — CI（§6）

## §6 CI

| 项 | 值 |
|---|---|
| run | **40102170254**（https://github.com/logicflow-GYW/RikkaMinis/actions/runs/40102170254） |
| job | `120896393210`（build），attempt 1，`conclusion=success` |
| `head_sha` | **`0fcb8505541b3fb0e41218033d9b1ed98bcbefd6`** — 与本地 `git rev-parse HEAD` 逐字符一致 |
| 26 步 | `bad=[]`，唯一 `skipped` = 步骤 23 `Publish to Releases`（main 门控，分支上应跳） |
| 门禁 | 门头 `[1/112] … [112/112]`，恰一条 `Summary: 112 passed, 0 failed`（其余 Summary 行是各门内部小计） |
| 夹具 | 恰一条 `ALL 344 CASES PASS` |
| 编译错 | ` e: ` 计数 **0**，游离 `FAILED` 0 条 |
| 本支两门 | `PASS browser_tab_exec_serial (…)` / `✅ browser_tab_exec_serial_check selftest has teeth` / `✅ … real tree green`；`✅ file_provider_paths_coverage_check selftest has teeth (FILE_PROVIDER_PATHS_SELFTEST=PASS)` / `✅ … real tree green` |
| 日志 | 1,483,218 B（判读脚本：`grep -c` 三条 + jobs API 取步骤结论） |

## §7 残留登记（不动手）

| # | 残留 | 为什么不动 |
|---|---|---|
| R1 | **冻结面的 marker 是精确子串**：`test_scan.py:2697` 断言 `'SELFTEST=PASS (browser_tab_exec_serial: 3 fixtures)'` 出现在 selftest 输出里。本支最初把汇总行改成 `… 3 fixtures + 4 binding arms)`，全量夹具立刻红一条。处置 = 保留原文 marker 行 + 臂数另起一行（`SELFTEST_ARMS=PASS …`），不改冻结面 | `test_scan.py` 本轮禁改；已写入报告作为「给门加臂」的通用纪律 |
| R2 | 接收者解析只覆盖两种语法形态（括号调用 / 标识符）；`this.let { it.withLock }` 之类间接形态会被 fail-closed 判红 | 宁红不绿；触发条件 = 真树出现合法但被判红的写法 |
| R3 | `runAcquiredAction` 的 **else 臂**仍被视为「调用方已持锁」而免检 —— 若将来有新的调用方走 `implicitTab = false` 却没持锁，本门看不见 | 这是既有评测面的设计（Kotlin Mutex 不可重入），要判它需要跨函数调用图；触发条件 = 新增第三个 `runAcquiredAction(…, implicitTab = false)` 调用点 |
| R4 | `lockForTab` 的实参是否就是**目标 tab** 仍未验证（只验「接收者是 lockForTab 的返回值」）。`lockForTab(otherId).withLock` 过门 | 需要 tabId 的数据流；当前真树两处都传对，触发条件 = 出现 `lockForTab(` 的第三个调用点 |

## §8 复现命令

```bash
cd /var/minis/shared/work/meta-audit/wt-m2
python3 scripts/scan/browser_tab_exec_serial_check.py .            # rc=0
python3 scripts/scan/browser_tab_exec_serial_check.py --selftest   # 3 fixtures + 4 arms PASS
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
    /var/minis/shared/work/meta-audit/probe-m2/reports/report-B.md --repo "$PWD"   # 本门 rc=1
```

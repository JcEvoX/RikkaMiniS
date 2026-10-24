# FIX-META-rss-probe-lock-scope — 元审计见证固化（axis=order）

- 基线：`20a9fbadac1b0a78e3013e299c1694eaa2812e2e`（main）
- 分支：`fix/meta-audit-m5-scope-order`
- 门：`scripts/scan/rss_probe_lock_scope_check.py`
- 轴：**order**（顺序）—— 锁有没有包住决策，而不是锁的名字在不在
- 生产代码：**0 行改动**（本轮只修判据 + 固化见证）

## 1. 门在防什么

`OffloadRssProbe` / `BrowserRssProbe` / `ProviderRssProbe` 三个 offload RSS 探针共享一个 `record()`：它把「本次采样增量」喂给 `applyAndDecideLocked(st, deltaKb)`，由后者做三件必须原子的事——累加 `count/totalDeltaKb`、判 LEAK-SUSPECT 置位、阈值归零（governance hook 只允许触发一次）。探针会被两种 worker（宿主 offload 线程 + 沙箱采样协程）并发调用，所以整段必须在一个临界区内。

门要防的假绿：**锁在，但没包住决策**。

## 2. 见证（外部对抗审计给出，已机械复证）

```json
{"file":"src/android/app/src/main/java/com/rikkaminis/app/sandbox/OffloadRssProbe.kt",
 "find":"        val d = synchronized(st) { applyAndDecideLocked(st, deltaKb) }",
 "replace":"        synchronized(st) {}\n        val d = applyAndDecideLocked(st, deltaKb)"}
```

打上这个补丁：`record` 体内 `synchronized(` 仍然恰好出现 1 次，5 条禁止字面量仍然一条不在体内，`applyAndDecideLocked` 的函数体仍是原样 —— **旧判据 4/4 全绿，rc=0**。而实际上决策调用已经在临界区外无锁执行：两个 worker 并发时 `Stats` 自增丢失、LEAK-SUSPECT 重复告警、governance hook 重复触发（ISSUE-11 重现）。

## 3. 旧判据 → 新判据

旧（3 条，全部是**计数/子串**，看不见位置）：

| 判据 | 为什么瞎 |
|---|---|
| `record` 体内 5 条禁止字面量（`totalDeltaKb = 0L` 等）一条都不出现 | 判的是「没有裸归零」，不判调用在哪 |
| `record` 体内 `synchronized(` 恰为 1 次 | **空块也计数** —— `synchronized(st) {}` 与真块在计数上等价 |
| `applyAndDecideLocked` 函数体含归零/置位语句 | 判被调函数内部，不判调用点 |

新判据 `DECISION_LOCKED_CRIT = "record calls applyAndDecideLocked inside its synchronized block"`：

1. `synchronized_blocks(body)` —— 括号/花括号感知地解析 `synchronized(expr) { … }`，返回每块的 `(lock_expr, body_start, body_end)` 偏移；字符串字面量跳过（否则 `"{".` 这类内容会把括号计数带偏）；解析不出就**不产出该块**。
2. 找出 `record` 体内每一个 `applyAndDecideLocked(` 调用点偏移。
3. 断言**每一个**调用点都落在某个块体区间内。
4. **fail-closed 两条**：一个块都解析不出 → 红（无法确认，不许放行）；一次调用都没有 → 红（决策被内联或删掉，契约失效）。

即：从「锁出现了吗」变成「决策在不在这把锁里面」。

## 4. 见证固化（`--selftest` 新臂）

见证不是一次性验证，是永久回归臂。`WITNESS_ARMS` 与 `BREAKERS` 分开维护（frozen `test_scan.py` 的 marker `SELFTEST=PASS (5 fixtures)` 钉住了 breaker 计数，见证臂单独一行 marker，不去改冻结面）：

| 臂 | 形态 | 期望 |
|---|---|---|
| `decoy-empty-block` | `synchronized(st) {}` + 解锁调用（**外部见证原形**） | 红（恰好 1 条） |
| `decision-call-removed` | 把调用内联进块体、`applyAndDecideLocked(` 不再出现 | 红（fail-closed） |
| `breaker 1..4`（既有） | 去锁 / 语义漂移等 | 保持原期望 |

```
SELFTEST=PASS (5 fixtures)
WITNESS_ARM_ORDER=PASS (2 arm(s): decoy lock + inlined decision)
```

（`breaker 3`「record 无临界区」现在多 1 条 side red —— 同一棵树上新判据也报「解析不出块」，属于正确冗余，不是误伤。）

## 5. 四层证据

| 层 | 内容 | 结果 |
|---|---|---|
| L1 | 三门真树 + `--selftest` | `rss GATE=GREEN`（28 条判据 27 pass / 0 fail）；selftest 5 fixtures + 2 witness arms 全 PASS |
| L2 | `verify_witness.py report-B` 机械复证 | `rss_probe_lock_scope_check` → **`GATE_CAUGHT_WITNESS`**，`restore=True`（树逐字节复原） |
| L3 | 全量门 + 夹具 | `sh scripts/scan/scan.sh` → **`Summary: 112 passed, 0 failed`**（exit 0）；`test_scan.py` → **`ALL 344 CASES PASS`**（exit 0） |
| L4 | 分支 CI | run **#2622**（详见 §6） |

L2 的判据细节（这是本门唯一能区分「真修好」与「换了个说法」的读数）：同一份见证补丁，修前 `rc=0 → GATE_BLIND_CONFIRMED`，修后 `rc=1 → GATE_CAUGHT_WITNESS`，且 `restore=True` 证明门跑完把生产文件还原、工作树无残留。

## 6. CI

- 分支 push 不触发（`build-apk.yml` 的 `paths` 只认 `src/android/**`、`src/shared/**`、`deps/**` 与 workflow 自身），故手动 dispatch。
- run **#2622** / ref `fix/meta-audit-m5-scope-order` / `head_sha = f64914d216bc80dc74263f36ba65ae1b525ba289`（与本地 HEAD 逐字符一致）
- 期望读数：`Summary: 112 passed, 0 failed` 恰一条、`ALL 344 CASES PASS` 恰一条、`Publish` 是唯一 skipped（main 门控）、` e: ` 0。

## 7. 残留登记（不动手，交主会话入 backlog）

- **R1 同族未加牙**：`sumObserved`/其它探针若有第二处「计数在锁外」的形态，本判据只覆盖 `record` 一个函数体（`function_body(src, RECORD)` 取首个 `fun record(`）。触发条件：出现第二个 `record` 重载需要判锁，或探针家族新增成员。
- **R2 判据锚函数名**：`DECISION` 常量（`applyAndDecideLocked`）改名会让判据变成「无调用 → 红」。这是**刻意 fail-closed**（宁可误红，不可静默失明），改名时按报错提示同步常量。
- **R3 单行 `synchronized` 表达式体**：`val d = synchronized(st) { f() }` 这种单行块，块体区间就是 `f()` 本身，判据按位置仍成立；但若被写成 `synchronized(st) { f() }` 后额外用 `also { }` 串联，位置判定退化为「调用在块内」，不追副作用是否外泄。触发条件：出现串联写法。
- **R4 三兄弟文件漂移**：`BrowserRssProbe` / `ProviderRssProbe` 的 `record` 只有第一重载被分析（`ProviderRssProbe` 有 `record(ProbeRecord)` 与 `record(kind, before, after)` 两个）。触发条件：便捷重载里出现决策调用。
- **R5 判据名含 `%s` 格式化**：`DECISION_LOCKED_CRIT` 由 `DECISION` 拼接而成，selftest 的 target 比对依赖同一个常量——如果有人把判据名在别处硬编码字符串，改名即静默失配。触发条件：新增引用点。

## 8. 复现命令

```bash
cd /var/minis/shared/work/meta-audit/wt-m5
# L1
python3 scripts/scan/rss_probe_lock_scope_check.py .        # GATE=GREEN
python3 scripts/scan/rss_probe_lock_scope_check.py --selftest
# L2（机械复证：修前 rc=0 BLIND / 修后 rc=1 CAUGHT，restore=True）
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/report-B.md --repo "$PWD"
# L3
sh scripts/scan/scan.sh && python3 scripts/scan/test_scan.py
```

单点手工复现（把见证打回真树，跑门，再还原）：

```bash
F=src/android/app/src/main/java/com/rikkaminis/app/sandbox/OffloadRssProbe.kt
cp "$F" /tmp/rss.bak
python3 - "$F" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding="utf-8").read()
old = "        val d = synchronized(st) { applyAndDecideLocked(st, deltaKb) }"
new = "        synchronized(st) {}\n        val d = applyAndDecideLocked(st, deltaKb)"
assert s.count(old) == 1
open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
PY
python3 scripts/scan/rss_probe_lock_scope_check.py .   # 应 rc=1，报 "1/1 call site(s) OUTSIDE every synchronized block body"
cp /tmp/rss.bak "$F"
```

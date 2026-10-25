# FIX-META-WIDE-mirror_vm_thread_check

- 分支：`fix/meta-wide-09-browser-webview`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 报告来源：组 A（wide-1024 宽扫，25 门/席）
- 生产代码改动：**0 行**（本波零 `.kt` 改动：只改门判据 + 自证臂）
- 冻结面：`scripts/scan/scan.sh` / `scripts/scan/test_scan.py` 未触碰

## 1. 门

`scripts/scan/mirror_vm_thread_check.py`

锁 `MirrorSettingsScreen` 的三类并发契约：`SnapshotStateMap` 写入必须
在可变快照事务里（arm 1 / arm 4）、`applyMirror`（磁盘 IO）不得落在主线程块（arm 2 / 3）、
首次启动自动测速必须「所有权检查 + 已选优先」（arm 5-7）。本次 arm 1/2/5/7 从
「文本存在」改到「行为成立」。

## 2. 轴

| 见证 | 轴 | 新判据 |
|---|---|---|
| Arm 1 事务里只剩 dummy 读、真写入落到块外 | scope | 把 `Snapshot.withMutableSnapshot { }` 块整体屏蔽后，函数体内**不允许残留任何** `selectedMirrorId[...] =` / `useCustomMirror[...] =` |
| Arm 2 `if (false) { … applyMirror(…) }` | dataflow | 每个 `applyMirror(` 调用点必须**可达**：包围块 head 不得是常量假（`if/while (false\|true)`、`&& false`、`\|\| true`、`.take(0)`、`emptyList()`） |
| Arm 5 `emptySet().also { selectedMirrorId.keys }` | dataflow | 携带「已选集合」的本地，其初始化值必须**发布**对 `selectedMirrorId` 的读取（落在 `.also`/`.apply` 内 = 携带空集）；一个都没有 fail-closed |
| Arm 7 `if (slot === self && false) return@…` | dataflow | 所有权检查的**条件可满足**（同常量假判据）且位置在首次写入之前 |

## 3. 见证（3 条，机械复证结论：打上补丁旧门 rc=0 = 门瞎）

| # | 见证书 | 文件 | 旧判据为何绿 |
|---|---|---|---|
| 1 | `Arm 1 (ensureLoaded_snapshot_guard)` | `ui/sandbox/MirrorSettingsScreen.kt` | 只在事务块**内**断言两个 map 名字都出现；块外裸露的同一循环不查 ⇒ 后台线程（boot 路径 Dispatchers.IO）跨线程裸写 |
| 2 | `Arm 2 (no_applyMirror_on_main)` | 同上 | 只查 `applyMirror(` 出现在函数体内、且不在 Main 块内；`if (false) { for (…) applyMirror(…) }` 两条都满足，落盘永远不发生 |
| 3 | `Arm 5 (autoDetect_prior_selection_wins)` | 同上 | Arm 5 只查 `selectedMirrorId.keys` 与 `autoSelectFastest(` 出现，Arm 7 只查 `autoDetectJob` / `coroutineContext[Job]` / `return@withContext`；`&& false` + `emptySet().also { … }` 让两条契约同时失效而全部关键字仍在 |

## 4. 修法

1. **Arm 1（作用域）**：算出事务块区间并屏蔽，剩余文本里搜
   `(selectedMirrorId|useCustomMirror)\[[^\]]*\]\s*=` —— 「事务只覆盖一半写入」
   与「事务里只做 dummy 读」是同一缺陷的两种拼写。
2. **Arm 2/3（可达性）**：新增 `dead_context()`：向上取所有包围块 head，判定常量假
   （`if (false)`、`while (false)`、`&& false`、`|| true`、`.take(0)`、
   `emptyList()/emptySet()/emptyMap()`），并额外识别无花括号写法
   （`if (false) applyMirror(…)`）。
3. **Arm 5（值消费）**：`prior_selection_value_problem()` —— 凡初始化表达式里读取
   `selectedMirrorId` 的本地，若该读取落在 `.also`/`.apply` 内即红
   （`emptySet().also { selectedMirrorId.keys }` 携带的是空集，等于没有守卫）。
4. **Arm 7（守卫活性）**：用括号配平取出 `if (…)` 条件，要求条件里出现 slot 或
   `isActive`、可满足（非常量假）、且位置早于首次 `selectedMirrorId[...] =`。

自证臂：W11 事务外写入必红、W12 常量假包裹必红、W13 守卫取反 + 空已选集合必红
（两条同时报）、W14 等价活守卫（`!self.isActive`）必绿。

## 5. 四层证据

### L1 本门

```
python3 scripts/scan/mirror_vm_thread_check.py .              # rc=0, 真树全绿
python3 scripts/scan/mirror_vm_thread_check.py . --selftest   # 全臂符合预期（新臂红 / clean 臂绿）
```

V1-V10 既有臂全部保留 + **wide-1024 四条**（W11 / W12 / W13 / W14 clean）。
收尾标记 `MIRROR_VM_THREAD_SELFTEST=PASS` 未改动。

### L2 见证反攻（判据链）

L2 用的**不是**我重写的补丁，而是任务书里逐字节的见证原文（脚本：`probe/witness_lib.py`，
按任务书解析 `### 见证` 块 → 打补丁 → 跑门 → 恢复 → sha256 校验）。

```
修复前同一补丁 rc=0（门瞎） → 修复后 rc≠0（门抓住） ⇒ 当初那把刀被挡住了
```

| 见证书 | 轴 | 修复后 rc | restore | 判决 |
|---|---|---|---|---|
| `Arm 1 (ensureLoaded_snapshot_guard)` | scope | 1 | True | `GATE_CAUGHT_WITNESS` |
| `Arm 2 (no_applyMirror_on_main)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |
| `Arm 5 (autoDetect_prior_selection_wins)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |

判定：**本门 3/3 全抓住**，restore 全 True，工作树零污染（本支 5 门合计 **16/16**，逐条结果见 `probe/l2-*.json`）。

### L3 邻门（各 rc=0）

- `mirror_category_route_check.py` → rc=0
- `katex_ready_race_check.py` → rc=0

### L4 全量（CI job 日志逐条判读）

- run `40134859565`（#2632）attempt 1 → **success**；job `120994111288`，26 步，
  `not_ok=[]`，唯一 skipped = `Publish to Releases`（main 门控，符合预期）
- `head_sha = 5f38473157a2c5291ecf6d0619ddc53b0d5af1b6`，与本地 `git rev-parse HEAD` 逐字符一致
- `Summary: 112 passed, 0 failed` 恰一条（合并日志 `0_build.txt` 与
  `build/3_Run pre-build scan gate.txt` 各一次，是同一份输出的两份落盘）
- 门头 `[n/112]`：112 个唯一编号，最大 112（无重号、无缺号）
- `✅ ALL 344 CASES PASS` 恰一条
- Kotlin 编译错误 ` e: `：**0 行**；本仓门输出 `[FAIL]`：**0 行**
- 含 `FAILED` 的 12 行逐条核对：**全部**是 `PASSED` 用例名里的 `FAILED` 子串
  （例：`slot denied produces FAILED without calling provider PASSED`），无一真失败
- 五道门的 `…_SELFTEST=PASS` 标记在 CI 日志中各出现两次（合并日志 + 该步骤单文件），
  说明本轮新增的见证臂在 CI 里真的执行了
- 本支 5 道门在 CI 中的门头：`[62/112] D/ISSUE-04 webview holder url source`、
  `[58/112] G/ISSUE-07 katex ready race + alias lock`、
  `[69/112] C/ISSUE-03 mirror category detail route wiring`、
  `[78/112] C/ISSUE-08 history save snapshot`、
  `[81/112] F/ISSUE-06 mirror vm thread/state`、

## 6. CI run

- run id：`40134859565`
- conclusion：`success`
- head_sha：`5f38473157a2c5291ecf6d0619ddc53b0d5af1b6`（与本地 `git rev-parse HEAD` 逐字符一致）
- 说明：本文件（`fix-reports/`）在绿 sha 之后单独提交，源码树与绿 sha 等价：
  `git diff --name-only 5f38473157a2c5291ecf6d0619ddc53b0d5af1b6..HEAD -- . ':(exclude)fix-reports'` 为空。

## 7. 残留（P2 登记，不修）

1. **常量假只识字面量** —— 触发条件：写成 `if (BuildConfig.DEBUG_NOT || false)`
   之外的命名常量（`if (ALWAYS_FALSE)`）；触发后方案：做一次同文件常量表求值；
   搁置理由：本仓无此形态，且这类「命名常量假」在代码评审中显眼。
2. **Arm 2/3 只判 `applyMirror` 可达** —— 触发条件：把落盘换成另一个写入口
   （`writeMirrorConfig`）；触发后方案：把判据锚到「写 `/etc/apk/repositories` 的
   调用链」而不是单个函数名（arm 3 已按 `useCustomMirror + persist` 双证据锚定）；
   搁置理由：改名会同时打红 arm 2/3 的「does not call applyMirror」，不会静默。
3. **Arm 1 只覆盖 ensureLoaded** —— 触发条件：把同一批写入复制到新的加载函数；
   触发后方案：改成「全文件所有 SnapshotStateMap 写入必须被某个事务覆盖」的派生扫描；
   搁置理由：arm 4 已覆盖 retryApkWorld，其余写入点在主线程，本波随时限只修被见证的
   arm（半径纪律）。

## 8. 复现

```
cd /var/minis/shared/work/meta-wide/wt-09-browser-webview
python3 scripts/scan/mirror_vm_thread_check.py . --selftest
python3 /var/minis/shared/work/meta-wide/probe/witness_lib.py replay \
    /var/minis/shared/work/meta-wide/task-09-browser-webview.md /var/minis/shared/work/meta-wide/wt-09-browser-webview --gate mirror_vm_thread_check.py
bash scripts/scan/scan.sh | tail -3
python3 scripts/scan/test_scan.py | tail -2
```

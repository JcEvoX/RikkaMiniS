# FIX-META-WIDE · discarded_return_guard（wide-1024 / 支 11-misc-a）

- 分支：`fix/meta-wide-11-misc-a`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`（main，工作树干净、sha 逐字符一致）
- 本支范围：`scripts/scan/discarded_return_guard.py` 三门（D2 / S2 / G3）
- 生产代码：**零改动**（三条见证都是「为门造的补丁」，不是在修业务缺陷）

## 1. 门

`scripts/scan/discarded_return_guard.py`（2221 → 2503 行），冻结标记 `--self-test` 的收尾输出未改动。
本门盯的是「丢弃返回值」这一族（A-07 staged bytes / A-08 copySharedStream / B-04 queue drain）。
新增判据三条：

| 判据 | 位置 | 原判据问的问题 | 补后问的问题 |
|---|---|---|---|
| D2 | `scan_queue_drain` | `combinedParts.isEmpty()` 守卫**存在且在 persist 之前** | 守卫**终结了控制流吗**（块内必须有 `return` / `throw`） |
| S2 | `scan_staged_bytes` + 新 `_admission_gate_gaps` | 预算守卫被**调用**了吗、write 在 credit 之后了吗 | 准入决定**被当作分支条件消费**了吗，write 落在 Admitted 臂内吗 |
| G3 | `scan_share_stream_guard` + 新 `_copy_outcome_gaps` | `val x = copySharedStream(` 被**绑定**了吗 | 绑定的结果**分支**了吗，Failed 臂**失败**了吗 |

## 2. 轴

- D2 = **dataflow**（守卫存在但效果未达成：不终结 ⇒ 空 user 行照样落库）
- S2 = **dataflow**（决定被绑定但结果未消费：`val admission = …` 保留、`when` 删除）
- G3 = **dataflow**（结果被绑定、`Failed` 标签与日志都在，但该臂**返回了成功值**）

三条同一族：**「调用了正确 API」≠「用对了」**，判据锚的是"某物存在"，而缺陷在于"该物的输出没有被消费"。

## 3. 见证（外部模型产出，已逐条复现）

| # | 见证 | 补丁要点 | 修复前 | 修复后 | restore |
|---|---|---|---|---|---|
| D2 | 空内容守卫只剩日志 | 删 `return null` + `removeQueuedPlaceholders`，保留 `AppLogger.warning` | rc=0（瞎） | rc=1 | True |
| S2 | 准入决定被丢弃 | `when (admission)` 整块换成内联 write/credit，`admitAttachText` 调用保留 | rc=0（瞎） | rc=1 | True |
| G3 | 失败臂交出文件名 | `is SharedStreamCopy.Failed` 臂 `null` → `name` | rc=0（瞎） | rc=1 | True |

证据形态：同一 find→replace 在基线上 rc=0（门是瞎的），在本支 rc≠0（门抓住），
且每次 restore 后 sha256 与原文一致（无残留污染）。

## 4. 修法

### D2 —— 守卫必须终结
旧判据只做两件事：`re.search(r"if\s*\(\s*combinedParts\.isEmpty\(\)\s*\)", fbody)` 找守卫、比较它与 `appendMessage` 的位置。
补丁把守卫块体（brace 配平）取出来，要求其中出现 `return` / `throw`：
块非空不算数，日志语句更不算数 —— `if (empty) { log(...) }` 之后控制流直接落到 `appendMessage`，
落一条空 user 行，所有 provider 回 400。
解析失败（块不闭合）同样计违规，消息里写明 fail-closed。

### S2 —— 准入决定必须被消费
新增 `_admission_gate_gaps(branch)`：

1. 从 `val <v> = admitAttachText(` 派生变量名（不写死 `admission`）；
2. `<v>` 在 branch 内出现次数 < 2 ⇒ 违规「BOUND and never consumed」；
3. 有 `when (<v>) {`：用 `_brace_block` + `_arrow_arms`（按顶层 `->` 切臂）拆成臂，
   `Admitted` 臂必须含 write/credit，其余臂**不得**含 write/credit；
4. 无 `when` 但有一个条件引用了 `<v>` 的 `if`：按同一规则判"write 在允许侧"；
5. 一个都没派生出来 ⇒ 违规（不是跳过）。

### G3 —— 失败臂必须失败
新增 `_copy_outcome_gaps(text)`：

1. `_when_subject_block` 反向跳到 `when (` 配平后取块体（真树是 `when (\n val outcome = copySharedStream(…)\n) {` 这种主体跨行形态）；
2. `_arrow_arms` 找出 `Failed` 臂；
3. 该臂体必须含 `null` / `return` / `throw` —— 否则违规，并把"它返回的是 Staged 臂同一个值"点名（`name`）：
   复制没落盘，调用方拿到一个不存在的文件名 = ghost attachment；
4. 没有 Failed 臂 / 读不出块体 ⇒ 违规（fail-closed）。

绑定缺失（statement 位置调用）**不**由本条重复报：那是 G3 既有判据与 A08-7 臂的领地（避免双计）。

### 新增 selftest 臂（都是本支见证的固化）
- `D2b`：守卫保留、`return` 删除 → 恰红 D2（消息含 "does not TERMINATE"）
- `S2b`：守卫在 write 之后（原臂）现同时命中「顺序」+「未消费」两条（该 fixture 本就双缺陷，断言已按双红更新）
- `S2c`：**只**未消费（write 在前、`when` 删除）→ 恰红一条
- `A08-6b`：Failed 臂返回 `name()` → 恰红 G3（消息含 "instead of failing" / "ghost attachment"）

## 5. 四层证据

- **L1 本门**：`python3 scripts/scan/discarded_return_guard.py .` → rc=0（真树绿）；
  `--self-test` → 三个子套件全绿（drain / staged / share-stream，含新增 4 臂）。
- **L2 见证反攻**：见 §3，3/3 → rc=1，restore=True（`probe-11/witness_run.py`，sha256 逐文件比对）。
- **L3 邻门**（与改动文件语义相邻）：`claim_release_guard` / `stale_closure_guard` / `file_provider_paths_coverage_check` / `readonly_mount_write_guard` 各 rc=0。
- **L4 全量**：见 §6（由 `gate_run.sh --full` 与分支 CI 双重取证；本地壳内 `scan.sh` 裸跑会撞 S28 并发窗口 ENOSYS，故走持锁运行器）。

## 6. CI

- 本地：`gate_run.sh --full`（全局 flock + ENOSYS 重试），结果回填于本节末尾。
- 分支 CI：dispatch 后填 `run id / conclusion / head_sha`（head_sha 必须与本地 `git rev-parse HEAD` 逐字符一致）。

## 7. 残留

1. **R1 · G3 只认带主体的 `when (<v>) {`**
   - 触发条件：有人把 `when (val outcome = …)` 改写成 subjectless `when { … is SharedStreamCopy.Failed -> … }`。
   - 触发后方案：`_copy_outcome_gaps` 加 subjectless 分支解析（臂 header 里直接判断 `is … Failed`）。
   - 搁置理由：真树当前 0 处该写法，且读不出块体时本条 fail-closed 报红（不会静默放行）。

2. **R2 · S2 的臂切分以顶层 `->` 为界**
   - 触发条件：`Admitted` 臂体内出现同为块级深度的 `->`（例如臂内写一个无花括号的 lambda 参数箭头）。
   - 触发后方案：臂内 lambda 一律要求带花括号（Kotlin 惯例），否则按括号深度 2 处理。
   - 搁置理由：真树与四条夹具均无该形态；风险方向是**误红**（更严），不是放行。

3. **R3 · G3 只审第一个 `copySharedStream` 绑定**
   - 触发条件：包内出现第二个 copy 站点（另一个 handler 复用 copySharedStream）。
   - 触发后方案：改为遍历所有 `val x = copySharedStream(` 绑定并逐个判臂。
   - 搁置理由：当前 1 处；且 `SHARE_STREAM_WATCHLIST` 与 G4 成功门对新增站点仍会红。

4. **R4 · D2 只认 `combinedParts.isEmpty()` 这一拼写**
   - 触发条件：守卫改名（`if (parts.isEmpty())`）或改 elvis 形式。
   - 触发后方案：改为「persist 之前、对 combined 容器判空的任意条件」派生。
   - 搁置理由：改名同时会命中本门既有的 "guard missing" 判据（fail-closed 红），不会静默。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-11-misc-a
    python3 scripts/scan/discarded_return_guard.py .              # rc=0
    python3 scripts/scan/discarded_return_guard.py --self-test    # 三套件全绿
    python3 /var/minis/shared/work/meta-wide/probe-11/witness_run.py . \
        /var/minis/shared/work/meta-wide/probe-11/witnesses.json --only discarded_return_guard.py
    # → 3/3 caught, 0 miss（每条 restore=True）

见证原文：

    # D2 —— ChatQueueInterruption.kt
    -            withContext(Dispatchers.Main) {
    -                removeQueuedPlaceholders(queuedIds)
    -            }
    -            return null
    +            withContext(Dispatchers.Main) {
    +                removeQueuedPlaceholders(queuedIds)
    +            }

    # S2 —— ShareReceiverActivity.kt
    -                when (admission) { …Admitted -> { writeText; credit; out += … } }
    +                val name = …
    +                File(...).writeText(d.text, Charsets.UTF_8)
    +                stagedBytes.credit(textBytes)
    +                out += PendingShare.Item(...)

    # G3 —— ShareReceiverActivity.kt
    -            is SharedStreamCopy.Failed -> { AppLogger.warning(...); null }
    +            is SharedStreamCopy.Failed -> { AppLogger.warning(...); name }

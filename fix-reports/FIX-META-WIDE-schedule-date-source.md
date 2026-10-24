# FIX-META-WIDE · schedule_date_source_check（wide-1024 / 支 11-misc-a）

- 分支：`fix/meta-wide-11-misc-a`（基线 main `564c8f3e`）
- 生产代码：**零改动**
- 本支范围：`scripts/scan/schedule_date_source_check.py` 三条判据（scheduler 计日 / freebusy 裸日期 / strict 解析）

## 1. 门

门盯「日期来源」：跨午夜续跑的计日必须取**调度日**而不是**落定日**；`--start D --end D` 必须包含整天；
用户输入的日期必须用**严格模式**解析（越界拒绝，不许 rollover）。
三条判据原本都是"**锚点存在**"型：函数被路由、helper 被调用、标志被写上。

| 判据 | 原判据 | 补后判据 |
|---|---|---|
| `violations_scheduler` | `internal object ScheduledRunDating {` 存在 + `finalizeRun` 路由到 `ScheduledRunDating.gateDateOnSettle(` | `gateDateOnSettle` 的**返回表达式**必须是调度日参数（不得是 settle 参数、不得读墙钟） |
| `violations_calendar` | freebusy 体里有 `alignBareDateEnd(` | 该调用的**第二个实参**必须是调用方**解析过的**日期字符串（不是字面量/空串） |
| `violations_strict_date_parser` | 体里有 `isLenient = false` | **有效终态**是严格：任何 `isLenient = true` / `setLenient(true)` 覆盖即红 |

## 2. 轴

- 计日 = **dataflow**（决定走对函数，但返回值换了一个标识符）
- 裸日期 = **dataflow**（helper 被调用，但喂进去的不是调用方的值）
- strict = **order**（先 false 后 true，后写覆盖前写；"存在"判据对此完全无感）

## 3. 见证

| # | 见证 | 补丁要点 | 修复前 | 修复后 | restore |
|---|---|---|---|---|---|
| S1 | 计日返回落定日 | `gateDateOnSettle(…): String = scheduledDate` → `= settleDate` | rc=0 | rc=1 | True |
| S2 | freebusy 用空串对齐 | `alignBareDateEnd(it, endStr, zone)` → `alignBareDateEnd(it, "", zone)` | rc=0 | rc=1 | True |
| S3 | 严格模式被重新打开 | 在 `isLenient = false` 之后追加 `isLenient = true` | rc=0 | rc=1 | True |

## 4. 修法

新增四个解析辅助：

- `brace_body_after(src, start)` —— 从声明处配平形参表，跳过返回类型（含 `: () -> Unit` 这类嵌套括号），
  取 `{…}` 块体；表达式体（`= expr`）则取到行尾。返回 `None` 表示读不出来 ⇒ 调用方 fail-closed。
- `param_names(src, start)` —— 顶层形参名表（`scheduledDate` / `settleDate` 都从这里**派生**，不写死）。
- `returned_expressions(body)` —— 所有 `return X` 的 X；表达式体时取 `= …` 尾巴。
- `align_call_gaps(scope_body, label, where)` —— 对每个 `alignBareDateEnd(` 取实参。

### S1 · 计日
`tech = "fun gateDateOnSettle"` 定位后取体，逐个返回表达式判：

- 含第 2 个形参名（settle）⇒ 违规：「returns the SETTLE day」——23:55 起跑、00:06 落定会把**明天**写进 `lastRunDate`，次日整天退化 ALREADY_RAN（ISSUE-09A 原形）；
- 含 `now()` / `currentTimeMillis` / `LocalDate.now` ⇒ 违规（落定时重读时钟）；
- 既不含调度日参数名 ⇒ 违规（credits the wrong day）。

### S2 · 裸日期对齐
`alignBareDateEnd` 的第 2 个实参：

- 以 `"` 开头 或 抽不出标识符 ⇒ 违规（字面量/空串永远不匹配 `YYYY-MM-DD`，整天窗口被静默跳过）；
- 否则取表达式里**最后一个标识符**（`endRaw ?: ""` 这种惯用法取 `endRaw`），
  并要求同一作用域里存在 `…parse…(<ident>` 调用（`parse` / `parseDate` / `parseDateInZone` / `parseUserDate` 都算）。
  接不上 ⇒ 违规「the value aligned is not the caller's date string」。

真树两处：`doFreebusy`（`endStr`，由 `parseDateInZone(endStr, …)` 解出）与 `resolveRangePure`（`endRaw`，由 `parse(endRaw, …)` 解出）。

### S3 · 严格解析的有效终态
`STRICT_PARSE_FLAG` 缺失仍按原文报红（原有牙保持）。补后：收集所有
`isLenient = (true|false)` 与 `setLenient((true|false)` 的写入点 ——

- 出现任何 `true` ⇒ 违规「re-enables LENIENT mode」；
- 全部为 `false` 也要求**最后一次**写入是 `false`（防止未来出现别的覆盖形态）。

## 5. 四层证据

- **L1**：`python3 scripts/scan/schedule_date_source_check.py .` → rc=0；
  `--selftest` → `SELFTEST=PASS (schedule-date-source: 2 files x 2 arms, teeth on every criterion)`（收尾标记原样）。
  `cases` 由 18 增到 21（本支 3 条），每条都断言"因**自己那条**规则变红"（不是碰巧）。
- **L2**：§3 表 3/3 → rc=1，restore=True。
- **L3 邻门**：`calendar_events_nullable_predicate_check --root .` PASS、`offload_cli_args_check` PASS、`scheduled_store_release_guard` rc=0。
- **L4**：见 §6。

## 6. CI

- 本地全量：`gate_run.sh --full`（全局 flock + ENOSYS 重试）111 道 python 门 **PASS=111 / FAIL=0**；
  首轮 `test_scan.py` 撞并发窗口 ENOSYS（`browser_tab_exec_serial_check` 一条 case 误红，单独重跑该门 `--selftest` 即 PASS），
  锁内重跑 → **`ALL 344 CASES PASS`**（0 条 ❌）。裸跑 `scan.sh` 会撞 S28（多会话并发 → PRoot 返回 ENOSYS），故一律走持锁运行器。
- 分支 CI：**run #2639 / id `40136718049` / job `120999670424`**，`conclusion=success`，attempt 1，
  `head_sha=d6911e894b086d7ebbac18e42692b757f40cfe4a`（与本地 `git rev-parse HEAD` 逐字符一致）。
  26 步里唯一非 success = `Publish to Releases`（skipped，main 门控，符合预期）。
  日志逐条核（1,484,200 B）：`Summary: 112 passed, 0 failed` **恰 1 条**；子套件 5/10/14/22/28/36 passed 各 1 条（非主 Summary）；
  `ALL 344 CASES PASS` 恰 1 条；编译错误 ` e: ` **0 条**；6 条含 `FAILED` 的行逐条确认全是 **PASSED 用例名子串**；
  本门的分节/收尾标记在册。

## 7. 残留

1. **R1 · S1 只判「返回表达式里有没有 settle 标识符」**
   - 触发条件：写成条件表达式 `= if (outcome == "failed") settleDate else scheduledDate`。
   - 触发后方案：改成"命中集必须 ⊆ {scheduledDate}"的表达式级分析（或明确允许非主路径分支）。
   - 搁置理由：真树是纯表达式；且该形态本身就是可疑写法，误红方向安全（会被人当 bug 抓）。

2. **R2 · S2 取表达式最后一个标识符**
   - 触发条件：出现 `endRaw ?: defaultEnd` 这类"回退到一个变量"的写法。
   - 触发后方案：改为"表达式里第一个被同作用域 parse 过的标识符"。
   - 搁置理由：真树是 `endRaw ?: ""`（空串回退），当前判据正好覆盖；新形态会误红而非放行。

3. **R3 · S3 只认 `isLenient` / `setLenient`**
   - 触发条件：改用 `DateFormat` 别名的 `lenient = true`（Kotlin 对 Java setter 的属性化访问就是 `isLenient`，故风险极低）。
   - 触发后方案：把属性名候选表扩成 `{isLenient, lenient}`。
   - 搁置理由：`STRICT_PARSE_FLAG`（`isLenient = false`）缺失时判据本身就红，不会静默放行。

4. **R4 · 计日判据未覆盖 `completeRun` 之外的调用点**
   - 触发条件：新增第二个 `gateDateOnSettle(` 调用方且传入的不是调度日（例如传当前日期）。
   - 触发后方案：从调用点派生实参来源（第 1 实参必须与 `nextRunAt`/`lastRunAt` 同源）。
   - 搁置理由：当前唯一调用点已在 `finalizeRun` 内且实参来自 store 的 `nextRunAt`；扩到调用点级属新判据，本波只做已验证的轴。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-11-misc-a
    python3 scripts/scan/schedule_date_source_check.py .              # rc=0
    python3 scripts/scan/schedule_date_source_check.py --selftest     # SELFTEST=PASS
    python3 /var/minis/shared/work/meta-wide/probe-11/witness_run.py . \
        /var/minis/shared/work/meta-wide/probe-11/witnesses.json --only schedule_date_source_check.py
    # → 3/3 caught, 0 miss

# FIX-META-WIDE-outcome_known_chain_check

分支 `fix/meta-wide-12-misc-b` ｜ 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71` ｜ 轴 `dataflow / spelling / order`（逐条见 §2）
本波**零生产代码改动**：只改门判据 + 把见证固化成自带臂；`scripts/scan/scan.sh` /
`scripts/scan/test_scan.py`（冻结面）、`backlog.md`、`credibility.md` 未触碰。


## 1. 门
- 脚本 `scripts/scan/outcome_known_chain_check.py`，5 条判据（L1 重包 / L2 声明 / L3 数据类 /
  L2→L3 单跳映射 / L4 T7 喂参），守护 `outcomeKnown` 这条「截断重放是否可知」的跨层链。
- 收尾标记 `OUTCOME_KNOWN_CHAIN_SELFTEST=PASS (n fixtures)`（未改动）。

## 2. 轴
| # | 判据 | 轴 | 旧判据为何瞎 |
|---|---|---|---|
| 1 | CRIT_L1 重包携带 | dataflow | 子串 `outcomeKnown = result.outcomeKnown` 是它自身的前缀，`… || true` 照样命中 |
| 2 | CRIT_MAP 单跳映射 | dataflow | 同上；`ToolExecutionResult(… outcomeKnown = outcomeKnown).copy(outcomeKnown = true)` 把值一跳后写死 |
| 3 | CRIT_L4 T7 喂参 | dataflow | 正向正则 `[^)]*resultKnown = result.outcomeKnown` 命中；`\|\| !result.outcomeKnown`（排中律 = 恒 true）绕过 `= true` 禁令 |

## 3. 见证
- W1 → `outcomeKnown = result.outcomeKnown || true,`
- W2 → `…ToolExecutionResult(…, outcomeKnown = outcomeKnown,).copy(outcomeKnown = true)`
- W3 → `resultKnown = result.outcomeKnown || !result.outcomeKnown`

## 4. 修法
1. **`rhs_is_exact()`**：值必须**终止**在赋值处——`<field> = <value>` 之后只允许 `,` / `)` / 空白 / 行尾；
   `||`、`&&`、`?:`、`.copy(` 一律不算。
2. **CRIT_L1 / CRIT_L4** 改用 `rhs_is_exact()`（L4 先按 `ToolFinished(` 取实参文本再判）。
3. **CRIT_MAP** 把判据限定在**映射函数体**（`fun ToolResultShell.toToolExecutionResult()` 到空行），
   并加「值不得被紧随的 `copy/also/let/apply/run` 重写」——单跳是这条判据的本体。

## 5. 四层证据

### L1 本门
- 真树：`python3 scripts/scan/outcome_known_chain_check.py .` → rc=0
- 自证：`python3 scripts/scan/outcome_known_chain_check.py --selftest` → 收尾标记 `OUTCOME_KNOWN_CHAIN_SELFTEST=PASS (9 fixtures)`

### L2 见证反攻（`verify_witnesses.py`，逐条打补丁 → 门红 → 复原）
```
[CAUGHT] #1 CRIT_L1  rc=1 restore=True
[CAUGHT] #2 CRIT_MAP rc=1 restore=True
[CAUGHT] #3 CRIT_L4  rc=1 restore=True
```
**修复前同一批见证全部 rc=0（BLIND=20）**，修复后按门计：`CAUGHT=3 BLIND=0 ANCHOR_MISS=0 restore_all=True`。
判据链成立：同一把刀（外部给的、原样未改）当初打穿门，现在被挡住。

### L3 邻门（直跑，逐条 rc=0）
| 邻门 | 结果 |
|---|---|
| `mirror_category_route_check` | `MIRROR_CATEGORY_ROUTE_CHECK=PASS` |
| `provider_remember_key_check` | `[C-SV2b] PASS` |
| `models_provider_field_check` | `OK (6 arms clean)` |
| `memory_gate_provider_fields_check` | `clean` |
| `discarded_return_guard` | `encode verdict section green` |
| `provider_models_cache_wiring_check` | `GATE=GREEN` |
（本支六门互为邻门，六门同时绿见 §L1。）

### L4 全量
本地 `scan.sh` 在施工窗口内两次撞上沙箱 S28（`Function not implemented` 扩散到 busybox，
见 `shared/sandbox-limits.md`），按注册表纪律**不再本机重跑全量**，全量证据取分支 CI job
日志（`Summary: 112 passed, 0 failed` 恰一条 + `ALL 344 CASES PASS`）。逐门直跑（本门 6/6）
与邻门直跑（§L3）在本地完成，未被 S28 波及。


## 6. CI
- run **40134937808**（#2634，attempt 1）`conclusion=success`，job `120994351104`
- `head_sha = 64582201877c1849ca878a1a6e712c4e3d5549d5` —— 与绿时的本地 `git rev-parse HEAD` 逐字符一致
- 判读（1,484,201 B job 日志逐条核过）：
  - `Summary: 112 passed, 0 failed` **恰一条**；其余 7 条 `Summary:` 是子套件（28/5/14/36/10/22/10 passed）
  - `ALL 344 CASES PASS` 恰一条（= 本支六门的新增臂全部在 CI 里被 `test_scan.py` 断言）
  - 编译错误 ` e: ` = 0
  - 26 步里唯一 `skipped` = `Publish to Releases`（main 门控）
  - 本门在 CI 里的门头：`[46/112] B/ISSUE-04 outcomeKnown chain`


## 7. 残留
- **R1** `rhs_is_exact` 只看赋值表达式自身；若在**另一处**（映射函数之外）再 `copy(outcomeKnown=…)`
  覆盖，L1 判据看不到。／触发条件：新增中转层。／触发后方案：全链扫描 `outcomeKnown =` 的每个写点。
  ／搁置理由：设计上 `outcomeKnown` 只有三段（重包 / 映射 / 数据类），加中转就会破 L2 声明面。
- **R2** CRIT_MAP 的函数体窗口以上下空行为界；若映射函数被压成一行且与下一成员无空行，
  窗口会吞到下一成员（可能误红 `copy`）。／触发条件：格式化去掉空行。／触发后方案：括号/Brace 配平取函数体。
  ／搁置理由：ktfmt 保持空行；误红方向不静默。
- **R3** L4 取 `ToolFinished(` 之后第一个 `)` 作为实参结束（不配平嵌套括号）；实参里若嵌套调用含 `)`
  会截断。／触发条件：实参加函数调用。／触发后方案：括号配平。／搁置理由：截断一般使 `rhs_is_exact`
  不成立 → 红，不会静默放行。

## 8. 复现
### 复现
```sh
# 本门
python3 scripts/scan/outcome_known_chain_check.py .
python3 scripts/scan/outcome_known_chain_check.py --selftest

# 见证反攻（20 条，逐条打补丁 → 门红 → 字节级复原）
python3 /var/minis/shared/work/meta-wide/probe-12b/parse_witnesses.py
python3 /var/minis/shared/work/meta-wide/probe-12b/verify_witnesses.py "$PWD" --only outcome_known_chain_check.py --out after
```

装置脚本在 `/var/minis/shared/work/meta-wide/probe-12b/`（`parse_witnesses.py` 解析任务书的
find/replace 块，`verify_witnesses.py` 打补丁 + 跑门 + sha256 复原）。

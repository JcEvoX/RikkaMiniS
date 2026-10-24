# FIX-META-WIDE-marker_boundary_check

- 分支：`fix/meta-wide-06-turn-loop-speech`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 报告来源：wide-1024 组 A（`Report_A.md`）· 3 条门瞎实锤
- 结论：3/3 见证由 rc=0 翻成 rc≠0，真树仍绿，生产代码零改动

## §1 门

`scripts/scan/marker_boundary_check.py` — 守 offload 输出流「按标记切块」的正确性：

| 判据 | 守什么 |
|---|---|
| Criterion 0 | **同一性绑定**：解析器给出的 `exitCode` 必须就是守卫所测的那个变量，且守卫必须先把 `exitCode >= 0` 判成立（命中标记真的被找到），再据此切片。三者（hit / 守卫 / parse 结果）必须绑到同一个值上。 |
| Criterion 1 | 守卫条件不许落在常量或无关变量上（`idx`、`len`、字面量），否则「命中」由常量担保 |
| Criterion 2 | 上限常量（块大小 cap）必须真的参与边界比较，且不许被顶层 `||` 削弱（`x > CAP || x > 10_000_000` 等于取消上限） |

## §2 轴

| 见证 | 判据 | 轴 | 备注 |
|---|---|---|---|
| W1 | Criterion 1 `constant_guard` | dataflow | 守卫测常量，parse 结果被偷偷塞进返回 |
| W2 | Criterion 2 `cap_not_weakened` | dataflow | 上限被 `||` 短路掉 |
| W3 | Criterion 1 `guard_on_positional_hit` | order | 守卫测 `idx`（位置参数第 0 位）而非 `exitCode`（第 2 位） |

## §3 见证（补丁 → 旧门 → 新门）

| 见证 | 补丁要点 | 旧门 | 新门 | 新门读数 |
|---|---|---|---|---|
| W1 | 守卫换成 `if (true)`，真实 `exitCode >= 0` 藏进返回值构造 | rc=0 | rc=1 | `MarkerScan…:1160: … precedes the hit — the guard is on a constant / unrelated value` |
| W2 | cap 比较改成 `end - start > CAP \|\| end - start > 9_999_999` | rc=0 | rc=1 | `:1175: the CAP bound is weakened by a disjunction (\`\|\|\`) — …` |
| W3 | 守卫改成 `if (idx >= 0)`（`idx` 是位置参数第 0 位，非 exitCode） | rc=0 | rc=1 | `:1167: … the guard tests 'idx', not the parsed exit code` |

旧门瞎的原因：Criterion 1 是**词法**判据——「文件里出现过 `exitCode >= 0`」即通过，
不管它出现在哪、被测的是谁；Criterion 2 只要求 cap 常量**出现在**比较式里，
`A || B` 的短路语义不看；Criterion 0 的正则只认命名实参 `hit = …, exitCode = …`，
真树用位置实参，判据直接空转（0 命中 = 不报违规）。

## §4 修法

1. **位置实参映射（Criterion 0）**：解析 `MarkerScanResult(hit, beforeMarker, exitCode, n)`
   的数据类字段序，把每个实参映射到字段名；命名实参走名字，位置实参按序。解析失败
   fail-closed 报违规。
2. **绑定同一性（Criterion 0/1）**：从 `val r = MarkerScanResult(…)` 起，要求
   - 元组解构或 `.exitCode` 取出的**同一个标识符**出现在守卫条件里；
   - 守卫条件是 `>= 0` 形态（`> -1` / `!= -1` 等价接受）；
   - 否则报「守卫测的是别的值 / 常量」。
3. **上限不被削弱（Criterion 2）**：cap 常量必须作为比较的**一侧**出现（`<` `<=` `>` `>=`
   任一侧均可），且该比较式的顶层不许有 `||`；有则报「weakened by a disjunction」，
   同时报「does not bound anything」。
4. **掩码先于解析**：字符串/注释先抹平，避免标记字面量（本身就是被扫描的文本）干扰
   守卫判定的括号配平。

## §5 四层证据

- **L1**：真树 `rc=0`；`--selftest` → 8 条 fixture，逐条按「期望违规条数 + 期望消息片段」
  断言（如 `("unused cap constant is red", CAP_UNUSED_FIXTURE, 1, ["does not use"])`、
  `("W1 constant guard is red", …, 2, [...])`、`("W2 weakened cap is red", …, 2, ["weakened by a disjunction", "does not bound anything"])`），
  收尾标记 `SELFTEST=PASS`。
- **L2**：3/3 `CAUGHT rc=1` + `restore=True`（见 §3）。
- **L3 邻门**：`loop_persist_slice_check.py` rc=0、`process_logging_gate.py` rc=0。
- **L4**：`scan.sh` 112 passed / 0 failed；`test_scan.py` 46 个夹具函数逐进程 rc=0
  ——本门除标记断言（`("marker_boundary_check", "SELFTEST=PASS", …)`）另有
  夹具对断言 `("marker_boundary_check", "OK_FIXTURE", "PRE_FIX_FIXTURE", "pre-fix boundary hit")`，
  即「修复前必须红、修复后必须绿」两头都被钉住；CI `ALL 344 CASES PASS`。

## §6 CI run

run `40135353367`（#2636）/ job `120995610275` / **success**（attempt 1）/
head_sha `f6d9dc84bb8e79c695ac0edde57571bb08e2f7fa` == 本地 HEAD / 26 步 `not_ok=[]`、
唯一 skipped = `Publish to Releases`（main 门控）/ `Summary: 112 passed, 0 failed` /
`ALL 344 CASES PASS` / ` e: ` 0 / 含 FAILED 的 12 行全是 PASSED 用例名子串。

## §7 残留（三条，各带三件）

**R1 · 位置实参映射依赖数据类字段序**
- 触发条件：`MarkerScanResult` 字段重排（如把 `exitCode` 挪到第 1 位）。
- 触发后方案：字段序不写死，改成运行时解析数据类声明（`data class MarkerScanResult(...)`
  的构造参数列表）再建索引表；解析失败 fail-closed。
- 搁置理由：本门只作用一处数据类；重排会让位置映射错位并**报红**（守卫测错值），
  是可感知失败而非静默放行。

**R2 · 守卫拼写只接受 `>= 0` / `> -1` / `!= -1`**
- 触发条件：**合法**改写为 `exitCode in 0..` 或 `exitCode.compareTo(0) >= 0`。
- 触发后方案：加这两个形态（`in 0..` 区间上界 ±1 语义、`compareTo` 单侧）。
- 搁置理由：真树是 `>= 0`；其它拼写会 fail-closed 报红并附原文，人一眼能判。

**R3 · Criterion 1 的 legacy 分支仍锚字面名 `exitCode`**
- 触发条件：局部变量改名（`val code = r.exitCode`，语义完全正确）。
- 触发后方案：legacy 分支退役，全部走 Criterion 0 的派生式（名字无关，只认数据流）。
- 搁置理由：legacy 分支是为「pre-fix 夹具必须红」保留的历史形态（`test_scan.py:2671`
  的三元断言钉住）；退役必须与那条夹具同步改，属主会话收口动作，本支不动冻结面。

## §8 复现

```bash
cd /var/minis/shared/work/meta-wide/wt-06-turn-loop-speech
python3 scripts/scan/marker_boundary_check.py .            # rc=0
python3 scripts/scan/marker_boundary_check.py --selftest   # 8 fixtures, SELFTEST=PASS
python3 /var/minis/shared/work/meta-wide/w06_probe.py marker_boundary
```

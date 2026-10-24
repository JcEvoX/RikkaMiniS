# FIX-META-WIDE-turn_tail_finalize_order

- 分支：`fix/meta-wide-06-turn-loop-speech`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 报告来源：wide-1024 组 A（`Report_A.md`）· 3 条门瞎实锤
- 结论：3/3 见证由 rc=0 翻成 rc≠0，真树仍绿，生产代码零改动

## §1 门

`scripts/scan/turn_tail_finalize_order.py` — 守 AgentLoopEngine 回合尾（`toolCalls.isEmpty()`
分支）的两条不变量：

1. **收尾不可跳过**：`updateAssistantMessage(id, accumulatedText, false, blocks)`（把本回合
   那一行标成「不再流式」）必须恰好一次，且它和 `break` 之间不许有 `continue` 分支。
   一旦跳过，后面继续往同一个 assistant id 写的 delta 会被
   `StaleStreamPublishPolicy.DROP_FINALIZED` 丢掉，气泡冻住。
2. **可见性判定只能用本回合的值**：`TurnVisibleContent.*` 的输入必须是每回合切片，
   不许是 run 级累加器（`loopState.accumulatedText` / `allToolBlocks`，只增不减，
   从第一个工具回合起恒为真，空回合恢复永不触发）。

## §2 轴

| 见证 | 判据 | 轴 | also_blinds |
|---|---|---|---|
| W1 | R2 `no_continue_after_finalize` | spelling | R1 `single_finalize_in_tail` |
| W2 | R4 `hasVisibleContent_forbids_loopState_accumulators` | dataflow | R3 `uses_TurnVisibleContent_helpers` |
| W3 | R3 `uses_TurnVisibleContent_helpers` | dataflow | R4 |

## §3 见证（补丁 → 旧门 → 新门）

| 见证 | 补丁要点 | 旧门 | 新门 | 新门读数 |
|---|---|---|---|---|
| W1 | 收尾块之后插 `if (turn < 10) { continue }` | rc=0 | rc=1 | `AgentLoopEngine.kt:3302: a continue branch can bypass the turn-tail finalize` |
| W2 | `val fullText = loopState.accumulatedText`，再喂给 `hasVisibleContent(fullText, …)` | rc=0 | rc=1 | `:3070: hasVisibleContent is fed the run-scoped accumulator 'fullText'` |
| W3 | `isEmptyTurn(loopState.accumulatedText, loopState.allToolBlocks, …)` | rc=0 | rc=1 | `:3076: isEmptyTurn is fed the run-scoped accumulator …`（两个实参各一条） |

旧门为什么瞎：
- W1：`continue` 判据是**行形状**正则（`\n[ \t]*continue[ \t]*(\n|//)`），假定关键字独占一行；
  `if (…) { continue }` 与 `if (…) continue` 全都不匹配。同理 `break` 也是行锚定。
- W2：只对 `hasVisibleContent` 那个**赋值语句的文本**做 `loopState.accumulatedText` 子串检查，
  别名洗白后文本上一点痕迹都没有。
- W3：判据只覆盖 `hasVisibleContent` 一个调用点，而真正拥有恢复 `continue` 的是
  `isEmptyTurn`——声明遍历没派生到「所有 TurnVisibleContent 调用」。

## §4 修法

**W1 · token 级控制流（spelling → 形态等价类）**
在掩码后的代码上按 token 搜索：`(?<![\w.$])continue\b` / `(?<![\w.$])break\b`，
区间取「收尾调用起点 → 其后第一个 break token」。任何拼写（一行式、花括号式、
`continue@label`）都算。注释与字符串内容先被掩码，所以注释里的 "continue" 不会误报。

**W2/W3 · 别名污染 + 全调用点覆盖（dataflow + completeness）**
1. 污染源定义为两个 run 级累加器；
2. 别名传播做成**不动点**：`val X = <污染表达式>` 或 `val X = <已污染标识符>` 即污染；
3. `drop(turnStartBlockIndex)` 显式豁免——它是**正确的**每回合切片（真树正是这么写的）；
   其它 `drop(...)` / `filter(...)` / 直接传累加器都算污染；
4. 判据从 `hasVisibleContent` 扩到**所有** `TurnVisibleContent.<fn>(…)` 调用，逐实参判定；
   括号解析失败 fail-closed 报违规。

## §5 四层证据

- **L1**：真树 `rc=0`；`--selftest` → 5 条 fixture（good 0 违规 / bad 3 / W1 1 / W2 1 / W3 3），
  收尾标记 `SELFTEST PASS` 原样保留（`test_scan.py:2642` 全等断言）。
- **L2**：3/3 `CAUGHT rc=1` + `restore=True`（见 §3 表）。
- **L3 邻门**：`loop_persist_slice_check.py` rc=0、`chat_ui_interaction_check.py` rc=0。
- **L4**：`scan.sh` → 112 passed / 0 failed；`test_scan.py` 46 个夹具函数逐个独立进程 rc=0；
  CI 日志 `ALL 344 CASES PASS`。

## §6 CI run

run `40137671947`（#2642）/ job `121002546481` / conclusion **success**（attempt 1）/
head_sha `98a65e7bf3df82da26feeee4d294a042c2a95f71` == 本地 HEAD / 26 步 `not_ok=[]`、
唯一 skipped = `Publish to Releases`（main 门控）/ `Summary: 112 passed, 0 failed` /
`ALL 344 CASES PASS` / ` e: ` 0 次 / 含 FAILED 的 6 行逐条确认全是 PASSED 用例名子串。


## §7 残留（三条，各带三件）

**R1 · 污染追踪只认单行 `val` 初始化**
- 触发条件：出现跨行初始化（`val fullText =\n    loopState.accumulatedText`）、
  解构（`val (a, b) = …`）或 `let/also` 链式洗白。
- 触发后方案：初始化式读取改成「从 `=` 起读到语句结束（深度 0 换行或 `;`）」，并递归
  跟踪 `let/also/run` lambda 内的接收者。
- 搁置理由：真树两处调用点都是直传或单行别名；跨行别名写法写不出可信见证，
  且现判据是 fail-open 的窄洞（只漏不误伤），登记不修。

**R2 · continue 区间是「收尾 → 其后第一个 break」的整段**
- 触发条件：在这段区间内合法插入一个**嵌套循环**，其内部有 `continue`。
- 触发后方案：区间内按花括号深度过滤——只统计深度 ≤ 收尾调用深度的 `continue`。
- 搁置理由：真树该区间只有 `if (AgentLoopExitDecision…) { … }` 与 `break`，无嵌套循环；
  偏严方向安全（报红而不是放行）。

**R3 · 收尾锚点是字面调用形状**
- 触发条件：收尾调用被抽成 helper（`finalizeTurnRow(loopState)`）。
- 触发后方案：锚点改成「函数体内出现 `updateAssistantMessage(` 且第三实参为 `false`」的
  派生式，而不是固定实参文本。
- 搁置理由：真树只有一处调用；抽 helper 时门会 fail-closed 报「expected exactly 1
  turn-tail finalize, found 0」，属于可感知的显式失败，不是静默放行。

## §8 复现

```bash
cd /var/minis/shared/work/meta-wide/wt-06-turn-loop-speech
python3 scripts/scan/turn_tail_finalize_order.py .            # rc=0
python3 scripts/scan/turn_tail_finalize_order.py --selftest   # 5 fixtures, SELFTEST PASS

# W1：一行式 continue
python3 - <<'PY'
import hashlib, subprocess
p = "src/android/app/src/main/java/com/rikkaminis/app/ui/chat/AgentLoopEngine.kt"
s = open(p).read()
find = "                    host.updateAssistantMessage(loopState.assistantId, loopState.accumulatedText, false, loopState.allToolBlocks)\n                }\n                if (AgentLoopExitDecision"
assert s.count(find) == 1
h = hashlib.sha256(s.encode()).hexdigest()
repl = find.replace("}\n                if (AgentLoopExitDecision", "}\n                if (turn < 10) { continue }\n                if (AgentLoopExitDecision")
open(p, "w").write(s.replace(find, repl))
print("rc =", subprocess.run(["python3", "scripts/scan/turn_tail_finalize_order.py", "."]).returncode)
open(p, "w").write(s)
print("restore =", hashlib.sha256(open(p, "rb").read()).hexdigest() == h)
PY
```

15 条见证统一复现：`python3 /var/minis/shared/work/meta-wide/w06_probe.py turn_tail`。

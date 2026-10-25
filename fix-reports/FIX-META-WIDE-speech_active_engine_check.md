# FIX-META-WIDE-speech_active_engine_check

- 分支：`fix/meta-wide-06-turn-loop-speech`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 报告来源：wide-1024 组 A（`Report_A.md`）· 3 条门瞎实锤
- 结论：3/3 见证由 rc=0 翻成 rc≠0，真树仍绿，生产代码零改动

## §1 门

`scripts/scan/speech_active_engine_check.py` — 守 SpeechRecognizerManager 的「引擎代际」不变量：

| 判据 | 守什么 |
|---|---|
| arm1 | `stopRecording` 必须停**启动的那个**引擎（`activeEngine`），不是新 `create()` 一个 |
| arm2 | `cancelRecording` 必须**先** `cancel()` 再清指针（反过来 = 取消打在 null 上，识别器留在开麦状态） |
| arm3 | `selectEngine` / `selectLocale` 切引擎时必须先 `cancel()` 旧的再清指针，否则旧引擎继续出结果 |
| arm0（本波新增） | `activeEngine = <x>` 与 `<x>.start(` 必须是**同一个标识符**，且赋值在 start 之前——证明「存下的就是启动的」 |

## §2 轴

| 见证 | 判据 | 轴 | also_blinds |
|---|---|---|---|
| W1 | arm1 `stopRecording_uses_activeEngine` | dataflow | arm2–arm5 |
| W2 | arm2 `cancelRecording_cancels_before_clear` | order | arm4 |
| W3 | arm3 `selectEngine_cancels_activeEngine` | order | arm4 |

## §3 见证（补丁 → 旧门 → 新门）

| 见证 | 补丁要点 | 旧门 | 新门 | 新门读数 |
|---|---|---|---|---|
| W1 | `startRecording` 改成 `SpeechRecognizer.create…` 后赋值给局部，`activeEngine` 存另一个实例 | rc=0 | rc=1 | `FAIL SpeechRecognizerManager.kt: does not bind 'activeEngine' to the engine it starts` |
| W2 | `cancelRecording` 里把 `activeEngine = null` 提到 `cancel()` 之前 | rc=0 | rc=1 | `…: cancelRecording clears 'activeEngine' BEFORE cancelling it` |
| W3 | `selectEngine` 清指针但不 cancel | rc=0 | rc=1 | `…: selectEngine clears 'activeEngine' without cancelling it first` |

旧门瞎的原因：
- W1：arm1 只查 `stopRecording` 里**出现** `activeEngine` 这个词（加 `?.stopListening()` /
  `?.cancel()` / `?.destroy()` / `?:` 四个形状之一），启动侧完全不看 ⇒ 「存的不是启的那个」
  这条最致命的分叉没人守。
- W2/W3：判据是「函数体内是否存在 `cancel()`」+「是否存在 `activeEngine = null`」两个
  **存在性**，两者的**先后**不判；把清指针提到 cancel 前面就全绿。

## §4 修法

1. **arm0 · 绑定⇄启动同一身份（dataflow）**：在绑定函数体内找 `activeEngine\s*=\s*(\w+)`，
   再要求同一函数里存在 `<同标识符>.start(`（点号直接接 `start`），并比较两个 token 的偏移：
   赋值必须在 start 之前。找不到绑定 / 找不到 start / 标识符不同 / 顺序反 ⇒ 各报一条。
2. **arm3b · cancel-before-clear（order）**：抽 `cancel_before_clear(body, name)` 共享助手，
   对 `cancelRecording` / `selectEngine` / `selectLocale` 三个入口统一判「首个 `.cancel()`
   的偏移 < 首个 `activeEngine = null` 的偏移」；有 clear 无 cancel、有 cancel 无 clear
   都算违规（fail-closed）。
3. **strip_comments 升级为字面量感知**：先扫 `"…"` / `'…'` 字面量再扫注释，避免
   `"video/*"` 里的 `/*` 开块注释把后续代码抹白（同族坑在 M6 已实证会吞掉 67 行函数体）。

## §5 四层证据

- **L1**：真树 `rc=0`；`--selftest` → 8 条 fixture（1 条 clean + 7 条违规），
  收尾标记前缀 `SELFTEST` 保留、括号说明随夹具数更新为 8。
- **L2**：3/3 `CAUGHT rc=1` + `restore=True`。
- **L3 邻门**：`voice_cancel_guard_check.py` rc=0、`a11y_handler_contract_check.py` rc=0。
- **L4**：`scan.sh` 112 passed / 0 failed；`test_scan.py` 46 个夹具函数逐进程 rc=0；
  CI `ALL 344 CASES PASS`。

## §6 CI run

run `40137671947`（#2642）/ job `121002546481` / **success**（attempt 1）/
head_sha `98a65e7bf3df82da26feeee4d294a042c2a95f71` == 本地 HEAD / 26 步 `not_ok=[]`、
唯一 skipped = `Publish to Releases`（main 门控）/ `Summary: 112 passed, 0 failed` /
`ALL 344 CASES PASS` / ` e: ` 0 / 含 FAILED 的 6 行全是 PASSED 用例名子串。

## §7 残留（三条，各带三件）

**R1 · arm0 要求 `.start(` 的接收者就是被绑定的那个标识符**
- 触发条件：把启动抽成 helper（`startEngine(engine)`）或链式
  （`engine.also { activeEngine = it }.start()`）。
- 触发后方案：把「start 的接收者」从字面 `<id>.start(` 扩成「同函数内 `<id>` 作为实参传入
  且该 helper 体内调用 `.start(`」一跳解析；链式形态识别 `also/apply` 绑定。
- 搁置理由：真树是直连写法；抽 helper 时门会报「does not bind」= fail-closed 显式失败，
  不是静默放行。

**R2 · cancel-before-clear 用「函数体内首次出现位置」比较**
- 触发条件：正确的两阶段拆除（cancel 放进一个延后执行的 block/lambda，清指针在前，
  真正 cancel 在 `invokeOnCompletion` 里）。
- 触发后方案：位置比较改成「控制流序」——按花括号深度与 lambda 边界给 token 编序，
  lambda 体内的 cancel 视为「在 clear 之后但仍算 cancel 覆盖」，只判「是否完全缺席」。
- 搁置理由：真树三个入口都是同步直线代码；该形态写不出可信见证，且偏严方向安全。

**R3 · strip_comments 不处理原始字符串**
- 触发条件：代码里出现 Kotlin 原始字符串 `"""…"""`（三引号）且内含引号或注释符号。
- 触发后方案：字面量扫描加三引号分支（`"""` 配对，可跨行）。
- 搁置理由：本门作用域三个文件当前无三引号字面量；若出现，最多造成「多抹白一段」⇒
  fail-closed 报红，不会静默放行。

## §8 复现

```bash
cd /var/minis/shared/work/meta-wide/wt-06-turn-loop-speech
python3 scripts/scan/speech_active_engine_check.py .            # rc=0
python3 scripts/scan/speech_active_engine_check.py --selftest   # 8 fixtures, SELFTEST…PASS

# W3：selectEngine 清指针不 cancel
python3 /var/minis/shared/work/meta-wide/w06_probe.py speech_active
```

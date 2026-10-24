# FIX-META-WIDE · overlay_show_hide_latch_check（wide-1024 / 支 11-misc-a）

- 分支：`fix/meta-wide-11-misc-a`（基线 main `564c8f3e`）
- 生产代码：**零改动**
- 本支范围：`scripts/scan/overlay_show_hide_latch_check.py`（C1 锁存 / C2 post 守卫 / C3 门控形态）

## 1. 门

门盯工具悬浮层的 show/hide 竞态：意图（`desiredShown`）与实体（`isShown`）之间隔着一次 `mainHandler.post`，
所以"入口写意图 → post 里按意图决定 attach"是唯一正确的时序。
三条判据原本都是"某物在该处出现过"型。

| 判据 | 原判据 | 补后判据 |
|---|---|---|
| C1 | 意图字段在 show/hide 里被写过目标值，且写点**在 post 之前** | 同一入口内**不得**再写反值（`hide()` 不许把意图重新锁存成 `true`，`show()` 同理） |
| C2 | post 的 Runnable 里有一个 `if (…意图…)` 且尾部是 `{` 或 `return` | 该守卫的条件**不得被字面量决定**（`&& false` / `|| true` / `!true`） |
| C3 | hide() 被 `if (…)` 门控且条件里出现实体字段（**同行前缀**或**上一行**） | 门控条件从**块头**反查（`if (…) {` / `when (…) {` / 同行 `if` / 同行 `takeIf`） |

## 2. 轴

- C1 = **dataflow**（写对了一次，随后自己又抹平）
- C2 = **spelling**（子串判据无法区分"真守卫"与"被字面量短路的守卫"）
- C3 = **spelling**（判据认的是行内拼写 `) hide()`，Kotlin 的块形态 `) {` + 下一行 `hide()` 全漏）

## 3. 见证

| # | 见证 | 补丁要点 | 修复前 | 修复后 | restore |
|---|---|---|---|---|---|
| A | hide() 把意图重新锁存成 true | 在 `desiredShown = false` 后插入 `desiredShown = true` | rc=0 | rc=1 | True |
| B | post 守卫被字面量短路 | `if (!shouldRunShowPost(desiredShown)) return@post` → `… && false) return@post` | rc=0 | rc=1 | True |
| C | 块形态的 isShown 门控 | `controller.hide()` 包进 `if (controller.isShown) { … }` | rc=0 | rc=1 | True |

## 4. 修法

### C1 · 反值锁存
在既有位置判据之后追加两条：`assign_positions(show_f[0], intent, "false")` 与
`assign_positions(hide_f[0], intent, "true")` 非空即违规。
消息点明后果：`hide()` 重新锁存 `true` ⇒ 排队中的 attach 存活；`show()` 重新锁存 `false` ⇒ 意图被丢。

### C2 · 守卫不得被字面量决定
`literal_decides(cond)`：按 `&&` / `||` 切分，任一操作数为裸 `true`/`false`/`!true`/`!false` ⇒ 该条件由字面量决定。
守卫定位保持原样（条件提到意图 + 尾部 `{`/`return`），只把 `guard` 位置与条件一起记下来，命中字面量即单独报一条。

### C3 · 门控形态改成块头派生
新增三个函数：

- `paren_cond_at(text, close_idx)` —— 反向配平，取 `)` 对应的 `(` 内的条件文本；
- `enclosing_conds(text, pos)` —— 反向扫描花括号，遇到未闭合的 `{` 就把它的头（`if` / `while` / `when`）作为包围条件；
- `hide_gates(src)` —— 对每个 `hide()` **调用**（跳过声明）收集：同行 `if (…)` 前缀、同行 `takeIf`/`takeUnless`、全部包围块头条件。

判据：任一条件里出现实体字段（`isShown` / 从 `attach(` 派生）⇒ 违规。
这样 `if (controller.isShown) {` + 下一行 `controller.hide()` 与单行写法一视同仁；
`when (controller.isShown) { true -> controller.hide() }` 也顺带覆盖。

### 新增 selftest 臂
- `c1-hide-relatch-true`
- `c2-guard-literal-false`
- `c3-service-braced-gate`

收尾标记 `OVERLAY_SHOW_HIDE_LATCH_SELFTEST=PASS` 原样未动。

## 5. 四层证据

- **L1**：`python3 scripts/scan/overlay_show_hide_latch_check.py .` → rc=0；
  `--selftest` → 11 条臂全绿（原 8 + 本支 3），含原有的单行门控臂 c5/c6/c7 仍红。
- **L2**：§3 表 3/3 → rc=1，restore=True。
- **L3 邻门**：`prefs_listener_holder_guard`（同 `AgentForegroundService.kt`）rc=0、`browser_destroy_wakeup_check` rc=0、
  `liveness_static_guard` rc=0。
- **L4**：见 §6。

## 6. CI

- 本地全量：`gate_run.sh --full`（flock + ENOSYS 重试），本节末回填。
- 分支 CI：dispatch 后回填 run id / conclusion / head_sha。

## 7. 残留

1. **R1 · C3 不处理 `else { hide() }`（条件取反）**
   - 触发条件：写成 `if (isShown) { … } else { controller.hide() }`。
   - 触发后方案：`enclosing_conds` 对 `else` 块返回**取反**后的条件。
   - 搁置理由：真树的两处 `hide()` 都在"无权限/状态不符"分支里，不存在该形态；该写法会被 C1/C2 之外的其他判据盯上（且方向是漏判而非误红 —— 登记待触发）。

2. **R2 · C3 的 `takeIf` 只认同行**
   - 触发条件：链式换行 `controller\n    .takeIf { it.isShown }\n    ?.hide()`。
   - 触发后方案：按语句（回溯到行首非续行）取整条链，而不是单行前缀。
   - 搁置理由：真树无该写法；且链式 takeIf 后的 hide 通常本就无害（意图层已判）。

3. **R3 · `enclosing_conds` 的花括号反向扫描不区分字符串/注释**
   - 触发条件：块内出现含 `{`/`}` 的字符串字面量（例如日志模板）。
   - 触发后方案：先跑一次 `strip_comments`+字面量占位（与 discarded_return_guard 的 `code_only` 同款），再做括号扫描。
   - 搁置理由：本门三处扫描文件里未出现该形态；即使错位，结果是**漏判**（登记）或**误红**（可当场发现），不会静默放大成假绿。

4. **R4 · C1 只判"反值写"，不判"写两次同值"**
   - 触发条件：`desiredShown = true; desiredShown = true`（无意义但无害）。
   - 触发后方案：无需处理（不构成缺陷）。
   - 搁置理由：负决策 —— 刻意不判，避免把"冗余赋值"当缺陷（会制造误红）。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-11-misc-a
    python3 scripts/scan/overlay_show_hide_latch_check.py .              # rc=0
    python3 scripts/scan/overlay_show_hide_latch_check.py --selftest     # 全绿
    python3 /var/minis/shared/work/meta-wide/probe-11/witness_run.py . \
        /var/minis/shared/work/meta-wide/probe-11/witnesses.json --only overlay_show_hide_latch_check.py
    # → 3/3 caught, 0 miss

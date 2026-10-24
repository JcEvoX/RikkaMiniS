# FIX-META-WIDE · a11y_handler_contract_check（wide-1024 / 支 11-misc-a）

- 分支：`fix/meta-wide-11-misc-a`（基线 main `564c8f3e`）
- 生产代码：**零改动**
- 本支范围：`scripts/scan/a11y_handler_contract_check.py` 三条判据（tapText / --visible-only / slot 释放守卫）

## 1. 门

门盯「handler 契约」：五条 handler 的动作是否真的执行、`--visible-only` 是否真的被消费、
无障碍槽位的所有权守卫是否真的能拒绝晚到的清理。
新增的三条判据都落在**同一类缺口**上：API 被调用了，但它的**结果没人用**。

| 判据 | 函数 | 原判据 | 补后判据 |
|---|---|---|---|
| W1 | `scan_handler`（tapText） | 文本里有 `TapTextFlow.plan(` 且没有旧的反模式串 | `plan()` 的**第二个实参**必须是 `performAction(` 调用的观察值；禁止语句位置的裸 `performAction(` |
| W2 | `scan_visible_only_read` | `VisibleOnlyFlag.resolve(` 与三分类存在 | `walkNode(...)` 的 **visibleOnly 实参**必须是 resolve 出来的那个局部变量 |
| W3 | `release_guard_gaps` | 释放点前 8 行窗口里有 `promptToken != ` | 该比较**不得被字面量决定**（`false &&` / `|| true` / `!true`） |

## 2. 轴

- W1 = **dataflow**（观察值被替换成常量 `true`：seam 在、观察没了）
- W2 = **dataflow**（flag 被正确解析进局部，walker 却收了字面量 `true`）
- W3 = **dataflow + spelling**（子串判据看不见布尔短路，`false &&` 让"比较存在"毫无意义）

## 3. 见证

| # | 见证 | 补丁要点 | 修复前 | 修复后 | restore |
|---|---|---|---|---|---|
| W1 | tapText 伪造 selfClickSucceeded | `plan(n.isClickable, n.performAction(…))` → 先裸调用 `performAction`，再 `plan(n.isClickable, true)` | rc=0 | rc=1 | True |
| W2 | uiDump 硬编码 `--visible-only` | `walkNode(…, visibleOnly, compact, arr)` → `walkNode(…, true, compact, arr)` | rc=0 | rc=1 | True |
| W3 | 槽位守卫被 `false &&` 短路 | `if (promptToken != token) return null` → `if (false && promptToken != token) return null` | rc=0 | rc=1 | True |

## 4. 修法

新增两个解析辅助（本文件通用）：

- `call_args_of(text, name)` —— 对每次 `name(` 做 **括号配平**取实参，再 `split_top_level` 按顶层逗号切；
  嵌套调用（`n.performAction(ACTION_CLICK)`）因此仍是一个实参（旧判据用 `in` 判断子串，正是因为无法定位实参位置）。
- `literal_decides(cond)` —— 按 `&&` / `||` 切分条件，任一操作数为裸 `true` / `false` / `!true` / `!false`
  即视为"该条件的值由字面量决定"。

### W1 · tapText
`plans = call_args_of(body, "TapTextFlow.plan")`：无调用 ⇒ 沿用原来的「no longer decides through」违规；
每个调用要求 `len(pargs) >= 2`（否则报 shape 丢失，fail-closed），且 `pargs[1]` 必须含 `performAction(`。
再加一条：`performAction(` 出现在**语句位置**（行首无接收者赋值）⇒ 违规 —— 那条路径上 ACTION_CLICK 的
Boolean 直接被丢弃，`CLICKED`/`UNCHANGED` 的区分随之消失。

### W2 · uiDump
从 `val <v> = when (val read = VisibleOnlyFlag.resolve(` 派生 `<v>`（退化路径：取 `resolve` 之前最近的 `val`）；
`walkNode` 调用必须存在（否则 fail-closed：walker 改名了，判据要重锚），实参数 ≥ 7，
**第 5 个实参**必须是 `<v>`（允许 `visibleOnly = <v>` 命名实参形态）。

### W3 · 槽位释放
在原有 8 行窗口内，找出所有引用 `promptToken` 的 `if (…)` 条件，`literal_decides` 为真即违规：
`if (false && promptToken != token) return null` 保留了子串、保留了早退形态，却让令牌比较**永不生效**，
于是晚到的 watcher 会清掉新 waiter 的槽位。

### 新增 selftest 臂
- `handler/tapText fabricates the self-click outcome`（W1）
- `handler/uiDump hardcodes the walker's visibleOnly`（W2）
- `slot/guard short-circuited by a literal`（W3）

为让 W2 有可判的载体，`clean_handler` 夹具的 uiDump 体补了一行 `walkNode(svc.nodeRegistry, root, 0, depth, visibleOnly, true, arr)`
（夹具是门的被测对象，不是生产代码；其余 20 余条臂全部保持绿）。收尾标记 `selftest passed` 原样未动。

## 5. 四层证据

- **L1**：`python3 scripts/scan/a11y_handler_contract_check.py .` → rc=0；
  `--selftest` → 41 条 case 全绿（原 38 + 本支 3），打印 `selftest passed`。
- **L2**：§3 表 3/3 → rc=1，restore=True。
- **L3 邻门**：`share_dir_prune_check`（同文件 AccessibilityOffloadHandler）rc=0；`prefs_listener_holder_guard`（同服务面）rc=0；
  `overlay_show_hide_latch_check`（同一"意图 vs 实际"族，本支同改）rc=0。
- **L4**：见 §6。

## 6. CI

- 本地全量：`gate_run.sh --full`（flock + ENOSYS 重试），本节末回填。
- 分支 CI：dispatch 后回填 run id / conclusion / head_sha。

## 7. 残留

1. **R1 · W1 不允许「先存局部再传」**
   - 触发条件：有人重构成 `val ok = n.performAction(ACTION_CLICK)` + `plan(n.isClickable, ok)`。
   - 触发后方案：允许**一跳**局部绑定（把「实参是 performAction」放宽为「实参是该局部，且该局部由 performAction 初始化」）。
   - 搁置理由：真树为直传；且该形态是**误红**（更严），不会静默放行。

2. **R2 · W2 按实参位置（第 5 位）认定**
   - 触发条件：`walkNode` 签名改序/加参。
   - 触发后方案：改按参数名映射（`visibleOnly =`），或从 `fun walkNode(` 的形参表派生位置。
   - 搁置理由：shape 检查已 fail-closed（实参数 < 7 直接红），不会静默跳过。

3. **R3 · W3 只在 8 行窗口内找守卫**
   - 触发条件：所有权检查被移到距释放点 8 行以外（例如先算布尔再释放）。
   - 触发后方案：从"包含释放点的函数体"派生守卫，而不是固定行窗。
   - 搁置理由：当前真树两处都在窗口内；移动后本条会报 "not ownership-guarded"（误红方向，安全）。

4. **R4 · tapText 之外的 handler 未加同类「观察值被常量替换」判据**
   - 触发条件：其他 handler 也出现 `X(observed)` → `X(true)` 形态的见证。
   - 触发后方案：把 `call_args_of` + 常量实参检测抽成 `handler 契约` 通用臂（每个 `plan(`/`decide(` 类 seam 一条）。
   - 搁置理由：本波只对已验证的见证施工，未验证的形态不凭空加判据（避免装置比例失衡）。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-11-misc-a
    python3 scripts/scan/a11y_handler_contract_check.py .              # rc=0
    python3 scripts/scan/a11y_handler_contract_check.py --selftest     # 全绿
    python3 /var/minis/shared/work/meta-wide/probe-11/witness_run.py . \
        /var/minis/shared/work/meta-wide/probe-11/witnesses.json --only a11y_handler_contract_check.py
    # → 3/3 caught, 0 miss

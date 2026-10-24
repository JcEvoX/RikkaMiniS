# FIX-META-WIDE-subagent_handback_check

分支 `fix/meta-wide-12-misc-b` ｜ 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71` ｜ 轴 `dataflow / spelling / order`（逐条见 §2）
本波**零生产代码改动**：只改门判据 + 把见证固化成自带臂；`scripts/scan/scan.sh` /
`scripts/scan/test_scan.py`（冻结面）、`backlog.md`、`credibility.md` 未触碰。


## 1. 门
- 脚本 `scripts/scan/subagent_handback_check.py`，2 条判据（C1 hand-back 回显 / C2 取消传播），
  守护 `runSubagentLoop` 的「模型看不到自己刚写的东西 → 复读」与「Stop 被当成普通异常吞掉」两族回归。
- 收尾标记 `SELFTEST OK (n fixtures)`（未改动）。

## 2. 轴
| # | 判据 | 轴 | 旧判据为何瞎 |
|---|---|---|---|
| 1 | C1 回显文本 | dataflow | 只判 `appendSubagentTurnText(history` 子串在场；`(history, "")` 保留调用、回显空串 |
| 2 | C1 执行性 | order | 同上；`if (false) appendSubagentTurnText(history, text)` 保留全部 token，永不执行 |
| 3 | C2 取消重抛 | dataflow | 只判 `\bthrow\s+<var>\b` 子串在场；`if (false) throw e` 命中正则、随后伪造结果返回 |

## 3. 见证
- W1 → `appendSubagentTurnText(history, "")`
- W2 → `if (false) appendSubagentTurnText(history, text)`
- W3 → `catch (e: CancellationException) { if (false) throw e; …伪造结果… }`

## 4. 修法
1. **C1 读实参**：解析 `appendSubagentTurnText(history, <arg2>, …)` 的第二实参；在
   `code_only`+字面量掩码后为空（空串/常字面量）即红。
2. **C1 判执行性**：新增 `dead_guarded()`——看语句前缀的 `if (常量false)` 与外层块头的
   `if (常量false)`，命中即红（`0` 与 `false` 同义）。
3. **C2 判可达**：抽出 `catch (…: CancellationException) { … }` 块，逐 `throw` 判 `live_throw()`
   ——常量假守卫之上、或同层已有无条件 `return` 之前的 `throw` 都不算重抛；一个可达重抛都没有 →
   按「吞掉取消」红（与「没有该 clause」同一条消息，避免两套语义）。

## 5. 四层证据

### L1 本门
- 真树：`python3 scripts/scan/subagent_handback_check.py .` → rc=0
- 自证：`python3 scripts/scan/subagent_handback_check.py --selftest` → 收尾标记 `SELFTEST OK (9 fixtures)`

### L2 见证反攻（`verify_witnesses.py`，逐条打补丁 → 门红 → 复原）
```
[CAUGHT] #1 C1 (continue_branches_echo_and_alternation)    rc=1 restore=True
[CAUGHT] #2 C1 (continue_branches_echo_and_alternation)    rc=1 restore=True
[CAUGHT] #3 C2 (cancellation_caught_and_rethrown)          rc=1 restore=True
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
  - 本门在 CI 里的门头：`[31/112] Sub-agent turn hand-back guard`


## 7. 残留
- **R1** 「空/字面量」判据把 `text.ifEmpty { "…" }` 之类表达式当作非空（它确实带了 turn 文本，
  可接受），但 `appendSubagentTurnText(history, "" + text)` 这类拼接同样算非空——真形态是
  「文本必须来自本回合」，只能靠数据流追踪。／触发条件：出现拼接/兜底表达式。／触发后方案：
  要求实参的根标识符等于循环体里 `val text = …` 绑定的名字。／搁置理由：我第一版做过「必须绑定」，
  但它需要跨 continue 的作用域分析（实测在合规夹具上误红），先退到字面量级。
- **R2** `dead_guarded` 只认常量 `false`/`0`；`if (false || x)`、`if (0 > 1)` 未覆盖。
  ／触发条件：出现等价常量写法。／触发后方案：布尔求值器。／搁置理由：见证只用 `if (false)`。
- **R3** `live_throw` 的「先无条件 return」按花括号深度 + 语句前缀判，带标签的 `return@run`
  可能被判成条件返回之外的无条件返回。／触发条件：catch 块里出现 lambda + 标签返回。
  ／触发后方案：标签感知的语句分类。／搁置理由：现状是直白 `throw e` / `return quiet`。
- **R4** 只校验第 2 实参，不校验实参个数（真实签名 3 参：可见文本 + reasoning）。／触发条件：
  签名变化或参数换位。／触发后方案：参数位解析 + 名字对表。／搁置理由：第 2 参就是可见文本，
  换位会同时让本判据红（新第 2 参不是 text 声明）。

## 8. 复现
### 复现
```sh
# 本门
python3 scripts/scan/subagent_handback_check.py .
python3 scripts/scan/subagent_handback_check.py --selftest

# 见证反攻（20 条，逐条打补丁 → 门红 → 字节级复原）
python3 /var/minis/shared/work/meta-wide/probe-12b/parse_witnesses.py
python3 /var/minis/shared/work/meta-wide/probe-12b/verify_witnesses.py "$PWD" --only subagent_handback_check.py --out after
```

装置脚本在 `/var/minis/shared/work/meta-wide/probe-12b/`（`parse_witnesses.py` 解析任务书的
find/replace 块，`verify_witnesses.py` 打补丁 + 跑门 + sha256 复原）。

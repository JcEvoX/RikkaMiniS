# FIX-META-WIDE · collection_copy_on_mutate_check（wide-1024 / 支 11-misc-a）

- 分支：`fix/meta-wide-11-misc-a`（基线 main `564c8f3e`）
- 生产代码：**零改动**
- 本支范围：`scripts/scan/collection_copy_on_mutate_check.py`（写漏斗三条判据 + 扫描面派生）

## 1. 门

门盯 `config/collections/` 的**写漏斗**：每次 mutate 必须在 `.copy()` 出来的工作副本上改，
并且把**那个副本**持久化（ISSUE-04 的 copy-on-write 契约）。

| 判据 | 原判据 | 补后判据 |
|---|---|---|
| arm0（delegate helper） | helper 里有 `.copy(`、`apply(<工作副本>)`、`toMutableList()`，调用点以 `mutatedGroupCopy(` **开头** | 上述 + `apply` 的**每个**实参都是工作副本 + **`return` 的必须是工作副本**；调用点的持久化表达式必须**恰好**是一次 helper 调用 |
| arm2（functional） | 持久化表达式以 `apply(` **开头**（`re.match` 前缀） | 持久化表达式必须**恰好**是 `repo.update*(apply(x))`，尾部不得再挂 `.let` / `.also` |
| scope（扫描面） | `private fun mutate(` 字面签名；聚合 `seen` 非空即可 | 可见性从**树派生**（`private/internal/public/protected/override/suspend` 任意组合）；**逐文件**声明数必须等于分析数 |

## 2. 轴

- arm0 = **dataflow**（副本被造、被喂给 apply，但离开函数的是活对象）
- arm2 = **spelling + dataflow**（前缀匹配把 `apply(x)` 与 `apply(x).let { x }` 看成同一个东西）
- scope = **scope / completeness**（一个文件里的 mutate 消失，聚合计数仍非空 ⇒ 静默）

## 3. 见证

| # | 见证 | 补丁要点 | 修复前 | 修复后 | restore |
|---|---|---|---|---|---|
| A | helper 同时改活对象并返回它 | `mutatedGroupCopy` 内加 `apply(g)` + `return g` | rc=0 | rc=1 | True |
| B | 持久化时丢掉 lambda 的返回 | `repo.updateInstance(apply(inst))` → `repo.updateInstance(apply(inst).let { inst })` | rc=0 | rc=1 | True |
| C | mutate 改成 `internal` 并丢弃 apply 结果 | `private fun mutate` → `internal`，体改为 `apply(e); repo.updateEntry(e)` | rc=0 | rc=1 | True |

## 4. 修法

### 结构化解析替代字符串前缀
- `UPDATE_PAT` 只负责找调用起点；实参文本改由 `update_call_args(code)` **括号配平**取出。
  旧正则 `[^)\n]+` 在第一个 `)` 处截断，`apply(inst)` 与 `apply(inst).let { inst }` 因此得到同一前缀 —— 这正是 arm2 瞎掉的机制。
- `call_is_whole(argtext, name)`：实参必须是**恰好一个** `name(...)` 调用（配平后余下必须为空）。
  arm0 的持久化位置用它（`mutatedGroupCopy(...)` 之外不许有别的东西），arm2 用 `call_is_whole(arg, apply)`。
- `apply_args(body)`：`apply(` 的实参标识符表；`check_copy_core` 要求**没有**一个实参不是工作副本
  （旧判据只 `re.search` 第一个 `apply(`，`apply(working)` 在前就永远绿）。

### 返回值是契约
`check_copy_core(..., require_return=True)`（仅 arm0 的 helper 用，arm1 `Unit` 函数不适用）：
`return` 的最后一个标识符必须等于工作副本名；没有 `return` ⇒ fail-closed 违规。

### 扫描面派生 + 逐文件完整性
`SIG_PAT` 的可见性修饰符组改为 `(?:(?:private|internal|public|protected|override|suspend)\s+)*`；
`check_tree` 对每个文件比较 `len(re.findall(r"\bfun\s+mutate\s*\(", code))` 与 `len(extract_mutate_bodies(code))`，
不相等 ⇒ 违规并在消息里写明 "a mutate site slipped out of the scan's signature grammar (visibility/spelling drift, fail-closed)"。
这样"某个文件里的 mutate 被改名/改可见性"不会再被兄弟 collection 的 `seen` 掩盖。

### 新增 selftest 臂
- `helper also mutates + returns the LIVE object`（A）
- `functional persists apply(x).let { x } (return discarded)`（B）
- `scope: internal fun mutate mutating the live object`（C）
- `scope control: internal fun mutate stays clean`（证明放宽可见性不会误红合法写法）

收尾标记 `COLLECTION_COPY_ON_MUTATE_SELFTEST=PASS` 原样未动。

## 5. 四层证据

- **L1**：`python3 scripts/scan/collection_copy_on_mutate_check.py .` → rc=0；
  `--selftest` → 14 条 `run_fixture` 臂全绿（原 10 + 本支 4）。
- **L2**：§3 表 3/3 → rc=1，restore=True。
- **L3 邻门**：`entry_composite_key_guard_check` rc=0、`models_provider_field_check` rc=0、
  `provider_rpc_symmetry_check` ALL GREEN、`provider_write_rules_check` ALL GREEN、`deeplink_settings_routes_check` PASS。
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

1. **R1 · arm0 只认 `val <x> = <y>.copy(` 形态**
   - 触发条件：helper 改写成链式 `= g.copy(...).apply { … }`（表达式体）或 `return g.copy(...) .also {}`。
   - 触发后方案：把 `check_copy_core` 的"工作副本"识别扩到链式与表达式体（`= <expr>` 的末值）。
   - 搁置理由：真树三个 collection 全是 `val working = …` 形态；链式形态会命中 "hollowed"（误红，安全）。

2. **R2 · `apply` 只按名字识别**
   - 触发条件：lambda 形参改名为 `mutate` / `f`（`fun mutate(id, f: (T) -> T)`）。
   - 触发后方案：形参名已在 `SIG_PAT` 的 group(1) 里（`param`），把 `apply_args` 改为按 `param` 名匹配即可。
   - 搁置理由：`check_mutation_site` 已经用 `param` 判 arm2 的 `invoke`，但 arm1 的 `apply(` 仍是字面量 —— 三个 collection 都用 `apply`，改名会因 `apply_args` 空 ⇒ "never hands the working copy to apply()"（误红）。

3. **R3 · 逐文件计数只数 `fun mutate(` 这一族名**
   - 触发条件：写漏斗函数整体改名（`mutateApply`）。
   - 触发后方案：把计数器改为"含 `repo.update*` 且接收 `(T) -> T` lambda 的函数"，从树派生。
   - 搁置理由：改名后 `seen` 归零会命中既有的 "no `fun mutate` found … update this gate"（fail-closed 红），不会静默。

## 8. 复现

    cd /var/minis/shared/work/meta-wide/wt-11-misc-a
    python3 scripts/scan/collection_copy_on_mutate_check.py .              # rc=0
    python3 scripts/scan/collection_copy_on_mutate_check.py --selftest     # 全绿
    python3 /var/minis/shared/work/meta-wide/probe-11/witness_run.py . \
        /var/minis/shared/work/meta-wide/probe-11/witnesses.json --only collection_copy_on_mutate_check.py
    # → 3/3 caught, 0 miss

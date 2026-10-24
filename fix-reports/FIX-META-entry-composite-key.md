# FIX-META：`entry_composite_key_guard_check.py` — spelling 轴（Arm 6 只认 `split('.', limit)` 一种拼写）

- 分支：`fix/meta-audit-m3-spelling`
- 基线：`20a9fbadac1b0a78e3013e299c1694eaa2812e2e`
- 门：`scripts/scan/entry_composite_key_guard_check.py`（1 文件，**生产代码 0 行改动**，新增门 0 道）
- 轴：`spelling` —— 判据钉的是「哪一种 API 拼写」，而缺陷是「复合 id 的边界是从路径文本里读出来的」

## 1. 这改变什么用户行为 / 防什么假绿

> **防的假绿**：把 `rest` 按点截断成 childId（`rest.substringBefore('.')`），再套一个 `id in coll.childIds()` 的成员判断、`rest.startsWith("$id.")` 前缀判断 → Arm 7/8/9 全部满足、Arm 6 只认 `split('.', limit)` ⇒ 门绿。而 `models.<instanceId>/gpt-5.5.isHidden` 会被截成 `…/gpt-5`，childIds 查不到 ⇒ **全部 `unknown_path`**（1041 A-01 精确重现）。
>
> **改后**：Arm 6 判的是「resolver 里有没有从文本按点截断 **id 边界**」——除了取 collection 键（`collections[…]`，collection id 按约定不带点）之外，任何按点截断都报；Arm 7 补正向判据「childId 的来源表达式必须咨询 `childIds()`」。

## 2. 见证（外部审计给出，已被本门复核）

```json
{"file":"src/android/app/src/main/java/com/rikkaminis/app/config/ConfigRegistry.kt",
 "find":"    val childId = coll.childIds()\n        .filter { rest.startsWith(\"$it.\") }\n        .maxByOrNull { it.length }\n        ?: return null",
 "replace":"    val childId = rest.substringBefore('.')\n        .takeIf { id -> id in coll.childIds() && rest.startsWith(\"$id.\") }\n        ?: return null",
 "why_gate_green":"Arm 6 只拦 split(\".\", limit)，Arm 7 只查 startsWith 正则，Arm 8 只拦 childIds().(map|flatMap|…)，Arm 9 查 .path == path。等价按点截断的 substringBefore('.') 不在等价类里。",
 "why_defect_back":"substringBefore('.') 在第一个点截断 ⇒ models.<id>/gpt-5.5.isHidden 解析成 gpt-5 ⇒ childIds 查不到 ⇒ 全部 unknown_path（1041 A-01 重现）。"}
```

- 修前复证：`verify_witness.py report-B.md` → `rc=0 · GATE_BLIND_CONFIRMED`
- 修后复证：同命令 → **`rc=1 · GATE_CAUGHT_WITNESS · restore=True`**，命中行精确：
  `…/config/ConfigRegistry.kt:156 [arm 6] child id is derived by cutting the path remainder at a dot (substringBefore('.'))`
- 另做一轮**庭外变异**（不在见证里，验证等价类不是只为见证量身定制）：把 childId 改成「先 `val head = rest.substringBefore('.')`、再用 `firstOrNull { it == head }`」——中间变量 + 完全不同的调用于是 Arm 6 仍报红（`ConfigRegistry.kt:156`），Arm 7 因前缀测试被删也报红；回滚后 `rc=0`。

## 3. 现状判据 → 新判据

| Arm | 修前 | 修后 |
|---|---|---|
| 6 边界来源 | 只拦 `split('.' , limit …)`（一种拼写） | **位置规则**：resolver 体内任何按点截断，除 `collections[…]`（取 collection 键，唯一合法处）外全报。等价类：`split('.')`（带/不带 limit、`[0]`/`firstOrNull`）、`substringBefore/After(Last)('.')`、`takeWhile { it != '.' }`、`indexOf/lastIndexOf('.')` + 切片，以及「先存进中间变量再用来比较」的变体（按位置判，不看数据流所以自然覆盖） |
| 6 fail-closed | — | resolver 里**根本没有** `fields(…)` 的 id 交接 ⇒ 报红；childId 名字追不到任何产生式 ⇒ 报红 |
| 7 id 表 | 只查「整段里有 `childIds()`」+ `startsWith` 正则 | 保留两条，**新增正向判据**：产生 childId 的表达式本身必须咨询 `childIds()`（`val childId = <expr>` 的 `<expr>`，或 `forId = <expr>` 内联表达式）。只看「别处出现过 childIds()」的假阳性被堵住 |
| 8 / 9 | 不变 | 不变（本轮只修被证伪的轴） |
| 命中定位 | `src` 里重新搜一次模式 ⇒ 会指到**合法**的 `path.substringBefore('.')`（collection 键）那一行 | `path_router_bodies()` 现在返回 `(body, src_offset)`，`src_index()` 把 `whole` 里的偏移映射回 `src` ⇒ 报真实行号 `:156`，不再指错代码 |

实现要点：

- `path_router_bodies()` 返回 `[(body_code, src_offset)]`（`code_only` 逐字符保偏移，所以映射是精确的）。
- `_expr_after()`：从 `=` 起取**整条语句**，遇深度 0 换行时看下一非空字符是否续行（`.` / `?:` / 运算符）——`val childId = coll.childIds()\n .filter {…}` 因此被读成**一个**表达式（修前形态跨 4 行）。
- `_child_id_sources()`：锚在**消费点** `\.fields\s*\(`（named `forId = x` 与 positional `fields(x)` 都认），再回溯到 `val/var` 绑定或 lambda 参数，返回 `(expr, from_id_table)`；两者都追不到时给 `(None, …)` → fail-closed。
- `DOT_TRUNC` 常量表集中列出等价类，新增变体只加一行。

## 4. 见证固化为 selftest 臂

`--selftest` 从 5 条 resolver fixture 扩到 **8 条**，每条要求「只红自己那个 Arm」（臂可分离性），并输出 `ENTRY_COMPOSITE_KEY_SELFTEST=PASS`：

| fixture | 期望 | 说明 |
|---|---|---|
| `fixed` | ALL CLEAN | 真树形态 |
| `dot-split` | arm 6 | 修前形态（`split('.', limit = 2)` 快路径） |
| **`substringBefore`** | arm 6 | **见证原文逐字**（保留 childIds 成员判断 + 前缀判断 + `.path == path`） |
| `split-without-limit` | arm 6 | `split(".").firstOrNull()` |
| `substringBeforeLast` | arm 6 | `substringBeforeLast('.')` |
| `no-childIds` | arm 7 | 不看 id 表（**改写见 §7 R1**） |
| `no-prefix-test` | arm 7 | 有 id 表但不做前缀测试 |
| `materialised` | arm 8 | `childIds().mapNotNull { … }` |
| `near-match` | arm 9 | 不确认 `.path == path` |
| `pre-fix shape` 控制 | 6+7 | 1041 原始形态 |
| ROT / fail-closed 控制 | red | 缺文件 / 缺 resolver |

## 5. 四层证据

- **L1（本门）**
  - 真树：`entry_composite_key_guard_check: OK (9 arms clean)`，`rc=0`
  - selftest：`ENTRY_COMPOSITE_KEY_SELFTEST=PASS`（14 行断言全 OK），`rc=0`
  - 真树改动后**没有出现新命中**：唯一按点截断在 `ConfigRegistry.kt:152` 的 `collections[path.substringBefore('.', …)]`（取 collection 键，按位置规则被正确豁免）。
- **L2（机械复证，必过）**：见 §2，`GATE_CAUGHT_WITNESS`，`restore=True`。
- **L3（全量）**：`sh scripts/scan/scan.sh` → **`Summary: 112 passed, 0 failed`**（exit 0）；`python3 scripts/scan/test_scan.py` → **`ALL 344 CASES PASS`**（345 ✅ / 0 ❌）。本门在 `test_scan.py` 里的登记（`ENTRY_COMPOSITE_KEY_SELFTEST=PASS` 标记断言 + 真树绿）未改动、仍通过。
- **L4（CI）**：见 §6。

## 6. CI

- run：**#2624**（`run_id 40102323086`，`job 120896842322`），attempt 1，**success**
- `head_sha` = `f915282a9c98f3e09b070dbb2a44ba751895f8af`，与本地 HEAD 逐字符一致
- 26 个步骤，`NOT_OK = []`，唯一 `skipped` = **第 23 步 Publish to Releases**（main 门控）
- 门：`Summary: 112 passed, 0 failed` **恰一条**；门头 `━━━ [N/112] ━━━` 112 条、编号 1..112 **互异无重号**
- 夹具：`ALL 344 CASES PASS` **恰一条**；本门的 `ENTRY_COMPOSITE_KEY_SELFTEST=PASS` 标记断言在册（1 条），真树分支 `entry_composite_key_guard_check: OK (9 arms clean)` 同时出现在 CI 日志里
- 编译错 `e: ` **0 条**；含 `FAILED` 的行逐条确认全是用例名/断言文本子串，无游离失败
- 日志体积 1,478,843 B

## 7. 残留登记（本支不动手）

| # | 残留 | 判级 | 触发条件 |
|---|---|---|---|
| R1 | 原 `no-childIds` fixture 用的 `rest.substringBeforeLast('.', "")` **本身就属 dot-truncation 类**，Arm 6 升级后它同时红 6+7，破坏「一条 fixture 只红一个 Arm」的可分离性。处置：fixture 改为不含按点截断、但仍不看 id 表的形态（`rest.takeIf { it.isNotBlank() }`），截断变体移入新的 3 条 fixture。**覆盖度净增**（原 1 条 → 现 4 条），不是削弱 | P3 | 若将来有人质疑该 fixture 凭据 |
| R2 | Arm 6 的位置规则把 `collections[…]` 之外**所有**按点截断都当缺陷：若将来 resolver 里合法地截断一个**展示用**字符串（非 id 边界），会假红 | P3 | 首次出现此类合法截断时改为「按 sink 类型」判定 |
| R3 | 等价类未覆盖 `removeRange(...)` / `subSequence` 等罕见切片 API；`substring(0, x)` 只有 x 来自 `indexOf('.')` 时才被看见 | P2 | 首个真实变体出现时加进 `DOT_TRUNC` 表（一行） |
| R4 | Arm 8 仍用正则 `childIds().(map\|flatMap\|…)`，若改用 `for (id in coll.childIds()) { … }` 物化则漏 | P2 | 首次出现该写法 |
| R5 | Arm 7 的正向判据只看「产生式里出现 `childIds()` 字样」——`val table = coll.childIds()` 后 `table.firstOrNull { … }` 会被算作不咨询（方向偏严 = 假红，不是假绿） | P3 | 若出现该合法写法再扩一跳数据流 |

## 8. 复现命令

```sh
cd /var/minis/shared/work/meta-audit/wt-m3

# L1 本门
python3 scripts/scan/entry_composite_key_guard_check.py --root .
python3 scripts/scan/entry_composite_key_guard_check.py --selftest

# L2 见证复证（必须 GATE_CAUGHT_WITNESS）
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/report-B.md \
  --repo /var/minis/shared/work/meta-audit/wt-m3

# L3 全量
sh scripts/scan/scan.sh
python3 scripts/scan/test_scan.py
```

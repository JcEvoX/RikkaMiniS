# FIX-META-WIDE-envvar_config_group_check

分支 `fix/meta-wide-12-misc-b` ｜ 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71` ｜ 轴 `dataflow / spelling / order`（逐条见 §2）
本波**零生产代码改动**：只改门判据 + 把见证固化成自带臂；`scripts/scan/scan.sh` /
`scripts/scan/test_scan.py`（冻结面）、`backlog.md`、`credibility.md` 未触碰。


## 1. 门
- 脚本 `scripts/scan/envvar_config_group_check.py`，13 条判据，守护 `EnvVarEntry.group` 的
  配置面注册、`minis-config add groups` 的 context-limit 走线、备份 scope、以及 System 虚拟
  语音条目在备份 remap / groups 摘要两侧的存活。
- 收尾标记 `ENVVAR_GROUP_SELFTEST=PASS`（未改动）。

## 2. 轴
| # | 判据 | 轴 | 旧判据为何瞎 |
|---|---|---|---|
| 1 | C2 groupField 契约 | dataflow | 只判 `path = "envvars.$key.group"` 存在；`reader = { ConfigValue.Null }` 保留 path/writer/UI，单路径读变成常量 |
| 2 | C4 contextLimit 走线 | dataflow | 只判 `applyContextLimitWrite(` 被调用；`applyContextLimitWrite(g, 0)` 保留调用/helper/字段，丢掉 CLI 传的值 |
| 3 | C12 备份保活虚拟语音 id | order / dataflow | 只判 400 字符窗口里出现 `SystemVoiceEntries.isSystemEntryId`；`takeIf { false && … }` 保留调用与 decides nothing |
| 4 | C13 groups 摘要解析虚拟条目 | dataflow | 只判 `SystemVoiceEntries.resolve(` 存在；`entriesById[id].also { resolve(id) }` 把解析结果丢掉 |

## 3. 见证
见证原文见 `task-12-misc-b.md` 本支 §「需要加固的门」。
- W1 `reader = { val e = …; if (e == null) ConfigValue.Null else ConfigValue.Str(e.group) }` → `reader = { ConfigValue.Null }`
- W2 `applyContextLimitWrite(g, contextLimit ?: 0)` → `applyContextLimitWrite(g, 0)`
- W3 `old.takeIf { SystemVoiceEntries.isSystemEntryId(it) }` → `old.takeIf { false && … }`
- W4 `val e = entriesById[entryId] ?: SystemVoiceEntries.resolve(entryId)` → `…also { resolve(entryId) }`

## 4. 修法
1. **C2**：path 之后继续判 reader 的**产出**——括号配平取 `reader = { … }` 体，要求
   `ConfigValue.Str(<x>.group)` 且读 `repo.`（单一来源）。取不到 → EXTRACT_FAIL 红。
2. **C4**：先从 `val <p> = (obj["context_limit_tokens"] as? ConfigValue.Int)…` 派生解析变量，
   再要求每个 `applyContextLimitWrite(…)` 的第二实参引用 `<p>`；解析变量派生不出 → fail-closed。
3. **C12**：谓词必须**真判**——取 `isSystemEntryId(` 所在的最内层 `{ … }`，命中常量折叠
   （`false &&` / `&& false` / `true ||` / `|| true` / `if (false)`）或排中律（`x || !x`）即红。
4. **C13**：解析结果必须被**消费**——调用点前后各看一格：head 以 `=` / `?:` / `(` / `,` / `return` /
   `->` 结尾，或 tail 以 `?.` / `!!` / `.` / `,` / `)` 开场；两侧都不成立 = 语句被丢弃 → 红。
   （同时接受 `a[id] ?: resolve(id)` 与 `ConfigValue.Str(resolve(id)?.id ?: id)` 两种合法形态。）

## 5. 四层证据

### L1 本门
- 真树：`python3 scripts/scan/envvar_config_group_check.py .` → rc=0
- 自证：`python3 scripts/scan/envvar_config_group_check.py --selftest` → 收尾标记 `ENVVAR_GROUP_SELFTEST=PASS`

### L2 见证反攻（`verify_witnesses.py`，逐条打补丁 → 门红 → 复原）
```
[CAUGHT] #1 C2  (EnvVarsCollection_groupField_contract)     rc=1 restore=True
[CAUGHT] #2 C4  (GroupsCollection_contextLimit_wiring)      rc=1 restore=True
[CAUGHT] #3 C12 (ConfigBackup_preserves_system_voice_entries) rc=1 restore=True
[CAUGHT] #4 C13 (ConfigBuiltins_resolves_system_voice_entries) rc=1 restore=True
```
**修复前同一批见证全部 rc=0（BLIND=20）**，修复后按门计：`CAUGHT=4 BLIND=0 ANCHOR_MISS=0 restore_all=True`。
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
（分支 CI 见本目录 `FIX-META-WIDE-INDEX.md` 的 run 表；`head_sha` 与 `git rev-parse HEAD`
逐字符一致才认绿。）

## 7. 残留
- **R1** C2 要求 `ConfigValue.Str(<x>.group)` 这一种包装拼写：将来若引入统一值构造器
  （`ConfigValue.of(e)`）→ 误红。／触发条件：引入新值包装 API。／触发后方案：判「读 `e.group`
  且该读值流入返回值」而非包装名。／搁置理由：当前只有 `Str/Int/Bool/Null/Obj/Arr` 六个构造器。
- **R2** C4 只认 `(obj["context_limit_tokens"] as? ConfigValue.Int)?.value` 这一种解析拼写；
  抽成 `obj.intOrNull("context_limit_tokens")` → EXTRACT_FAIL 红。／触发条件：抽出解析 helper。
  ／触发后方案：允许一跳同文件委托（`obj.<helper>(…)` 的白名单）。／搁置理由：fail-closed 方向安全，
  且抽 helper 时门会红并把判据一起改。
- **R3** 死谓词检测覆盖常量折叠 + 排中律；`&& someAlwaysTrue()` / 双层否定 `!!x` 未覆盖。
  ／触发条件：出现该写法。／触发后方案：小布尔求值器（字面量 + 已声明 const val）。／搁置理由：无生产者。
- **R4** C13 的消费判定是字符级：`val x = run { resolve(id) }` 会被判 disc（head 以 `{` 结尾、
  tail 以 `}` 开场）→ 误红。／触发条件：把解析包进 `run`/`let` 再返回。／触发后方案：把 lambda
  末表达式算作返回值。／搁置理由：当前无此写法，且误红方向不静默。

## 8. 复现
### 复现
```sh
# 本门
python3 scripts/scan/envvar_config_group_check.py .
python3 scripts/scan/envvar_config_group_check.py --selftest

# 见证反攻（20 条，逐条打补丁 → 门红 → 字节级复原）
python3 /var/minis/shared/work/meta-wide/probe-12b/parse_witnesses.py
python3 /var/minis/shared/work/meta-wide/probe-12b/verify_witnesses.py "$PWD" --only envvar_config_group_check.py --out after
```

装置脚本在 `/var/minis/shared/work/meta-wide/probe-12b/`（`parse_witnesses.py` 解析任务书的
find/replace 块，`verify_witnesses.py` 打补丁 + 跑门 + sha256 复原）。

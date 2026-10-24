# FIX-META-WIDE-openminis_compat_field_parity_check

分支 `fix/meta-wide-12-misc-b` ｜ 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71` ｜ 轴 `dataflow / spelling / order`（逐条见 §2）
本波**零生产代码改动**：只改门判据 + 把见证固化成自带臂；`scripts/scan/scan.sh` /
`scripts/scan/test_scan.py`（冻结面）、`backlog.md`、`credibility.md` 未触碰。


## 1. 门
- 脚本 `scripts/scan/openminis_compat_field_parity_check.py`，4 臂（provider / session /
  group / delegated-run-config），守护 OpenMinis 备份**导出键集 → 转换器写回**的对齐。
- 收尾标记 `OPENMINIS_COMPAT_FIELD_PARITY_SELFTEST=PASS`（未改动）。

## 2. 轴
| # | 臂 | 轴 | 旧判据为何瞎 |
|---|---|---|---|
| 1 | Arm 1 `appendV1Suffix` 三态 | order / dataflow | 只禁默认 false 读；`if (has(k) && optBoolean(k)) put(k, optBoolean(k))` 保留守卫/写入/往返，显式 `false` 被跳过 |
| 2 | Arm 2 provider 字段 | dataflow | 判据是「字面量是否出现在转换器正文里」——`inst.optString("customUserAgent", "")` 读一句就满足，写回整行删掉也绿 |
| 3 | Arm 3 session 字段 | dataflow | 同上：`opt("pinnedAt")` 读点留着，`o.put("pinnedAt", …)` 删掉仍绿 |

## 3. 见证
- W1 → `if (inst.has("appendV1Suffix") && inst.optBoolean("appendV1Suffix")) { … }`
- W2 → `inst.optString("customUserAgent", "")`（整段 `.takeIf{}.let{ put }` 删除）
- W3 → `val pinnedRaw = rec.opt("pinnedAt")` 保留，`isoToMillis(pinnedRaw)….let { o.put("pinnedAt", it) }` 删除

## 4. 修法
1. **写入判据替换占位判据**：`missing_keys`（字面量在场）→ `missing_writes`（`put("<k>", <v>)` 调用在场），
   实参按括号配平读到分隔逗号。常量实参只在**语句自测该键**（`if (… "k" …) put("k", true)`）时接受，
   否则视为「写了个与源无关的值」。
2. **标识符形写入**（`for (k in arrayOf("a","b")) { o.put(k, …) }`）单列识别——真实树的
   `maxThinkingLevel` 等走这条，否则写入判据会朝另一个方向变成拼写依赖。
3. **Arm 1 加守卫判据**：定位 `put("appendV1Suffix", …)` 的守卫条件（同一行前缀 or 外层 `if` 头），
   条件里出现 `optBoolean("appendV1Suffix")` 即红——三态透传必须只由 `has(…)` 决定。

## 5. 四层证据

### L1 本门
- 真树：`python3 scripts/scan/openminis_compat_field_parity_check.py .` → rc=0
- 自证：`python3 scripts/scan/openminis_compat_field_parity_check.py --selftest` → 收尾标记 `OPENMINIS_COMPAT_FIELD_PARITY_SELFTEST=PASS`

### L2 见证反攻（`verify_witnesses.py`，逐条打补丁 → 门红 → 复原）
```
[CAUGHT] #1 Arm 1 (appendV1Suffix polarity)                rc=1 restore=True
[CAUGHT] #2 Arm 2 (provider & modelEntry field parity)     rc=1 restore=True
[CAUGHT] #3 Arm 3 (chat session field parity)              rc=1 restore=True
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
（分支 CI 见本目录 `FIX-META-WIDE-INDEX.md` 的 run 表；`head_sha` 与 `git rev-parse HEAD`
逐字符一致才认绿。）

## 7. 残留
- **R1** 守卫条件读取是「最近 `if` 头 + 语句前缀」：`when (optBoolean(k)) { true -> put(…) }`
  未覆盖。／触发条件：改用 `when` 分派极性。／触发后方案：解析 `when` 分支条件。／搁置理由：现状是 `if`。
- **R2** 写入判据接受「常量实参 + 语句自测该键」，所以 `if (inst.has(k)) put(k, DEFAULT_TRUE)`
  （常量经别名而非字面量）会漏。／触发条件：引入常量别名表达极性默认值。／触发后方案：解析同文件
  `const val` / 顶层 `val` 字面量。／搁置理由：本文件无此写法。
- **R3** `identifier_written_keys` 只认同体 allow-list（`for (k in arrayOf(...))`）；若 allow-list
  抽成常量传参，标识符形写入识别失效 → 该键会被判「没写」。／触发条件：抽出 `ALLOW_KEYS` 常量。
  ／触发后方案：一跳同文件常量解析。／搁置理由：现状同体，且误红方向会当场暴露。

## 8. 复现
### 复现
```sh
# 本门
python3 scripts/scan/openminis_compat_field_parity_check.py .
python3 scripts/scan/openminis_compat_field_parity_check.py --selftest

# 见证反攻（20 条，逐条打补丁 → 门红 → 字节级复原）
python3 /var/minis/shared/work/meta-wide/probe-12b/parse_witnesses.py
python3 /var/minis/shared/work/meta-wide/probe-12b/verify_witnesses.py "$PWD" --only openminis_compat_field_parity_check.py --out after
```

装置脚本在 `/var/minis/shared/work/meta-wide/probe-12b/`（`parse_witnesses.py` 解析任务书的
find/replace 块，`verify_witnesses.py` 打补丁 + 跑门 + sha256 复原）。

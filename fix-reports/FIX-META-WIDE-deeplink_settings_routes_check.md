# FIX-META-WIDE-deeplink_settings_routes_check

分支 `fix/meta-wide-12-misc-b` ｜ 基线 `564c8f3eef683d50d7c90ae6fc7b21ce87972d71` ｜ 轴 `dataflow / spelling / order`（逐条见 §2）
本波**零生产代码改动**：只改门判据 + 把见证固化成自带臂；`scripts/scan/scan.sh` /
`scripts/scan/test_scan.py`（冻结面）、`backlog.md`、`credibility.md` 未触碰。


## 1. 门
- 脚本 `scripts/scan/deeplink_settings_routes_check.py`，11 条判据，守护
  deeplink → 设置页路由（`AppNavigation.kt` 的 `Routes` 构建器 / 冷启动 `when` 分派、`MainActivity`）
  与设置屏的「深链预填 → 表单种子」链路。
- 收尾标记 `DEEPLINK_SETTINGS_ROUTES_SELFTEST=PASS`（未改动）。

## 2. 轴
| # | 判据 | 轴 | 旧判据为何瞎 |
|---|---|---|---|
| 1 | C2 `routes_seg_encoding` | spelling / completeness | 判据是 `count("seg(") == 0`：**一个编码兄弟就够**，`"model_entry/${seg(instanceId)}/$entryId"` 里的裸复合 id 全绿——而复合 entryId（携带 `/`）正是这条判据存在的理由 |
| 2 | C8 `envvar_screen_consumes_pending` | spelling | 只匹配 `LaunchedEffect(Unit)`；`LaunchedEffect(true)` 语义等价（常量键 = 永不重跑）却放行 |
| 3 | C10 `envvar_dialog_remember_keys` | spelling | 只要求键列表**非空**；`remember(Unit)` 是常量键，等价的「永远不重种子」 |
| 4 | C11 `open_alarm_list_intent` | dataflow | 只要求分支里出现 `AlarmClock.ACTION_SHOW_ALARMS`；`showAlarms.action`（读属性、不启动）保留全部 token，卡片点击静默无反应 |

## 3. 见证
四条见证原文见 `/var/minis/shared/work/meta-wide/task-12-misc-b.md`（本支 §「需要加固的门」）。
- W1 `modelEntryDetail(instanceId, entryId) = "model_entry/${seg(instanceId)}/$entryId"`
- W2 `LaunchedEffect(pendingCreate) { … consumePendingEnvVarCreate() }` → `LaunchedEffect(true) { … }`
- W3 `var keyText by remember(editEntry?.id, editEntry?.key, prefillKey)` → `remember(Unit)`
- W4 `context.startActivity(showAlarms)` → `showAlarms.action`

## 4. 修法
1. **C2**：不再数 `seg(`，改为**逐插值判**——取每个构建器定义里的字符串字面量，切出 `?` 之前的
   路径段，解析每个 `$…` 洞：洞体必须是 `seg(…)`，或它的根标识符**不是**该构建器的 `String`
   形参（Boolean 开关 / 预编码局部量 `encoded` / `base` 均放行）。`seg` 仍是唯一口径。
2. **C8**：先派生 `by …pendingEnvVarCreate.collectAsState()` 的状态变量名，再扫描**所有**
   `LaunchedEffect(<键>)` 块：块内消费 `consumePendingEnvVarCreate()` 时，键必须**读**那个状态
   （`Unit`/`true`/`0` 一律红）。另加 fail-closed：消费调用不在任何「键住被观察状态」的
   effect 内 → 红（删掉 effect 也算破）。
3. **C10**：`remember(…)` 的键必须覆盖初值的**全部根标识符**（一跳：`editEntry?.key ?: prefillKey`
   要求 `editEntry` 与 `prefillKey` 都在键里），常量键因此必红；括号配平抽取，`remember(f(x))` 不再截断。
4. **C11**：括号配平取 `is DeepLinkAction.OpenAlarmList -> { … }` 分支体，从 `val x = …Intent(…)`
   派生 Intent 变量，要求分支内 `startActivity(x)`；取不到 → fail-closed 红。

## 5. 四层证据

### L1 本门
- 真树：`python3 scripts/scan/deeplink_settings_routes_check.py .` → rc=0
- 自证：`python3 scripts/scan/deeplink_settings_routes_check.py --selftest` → 收尾标记 `DEEPLINK_SETTINGS_ROUTES_SELFTEST=PASS`

### L2 见证反攻（`verify_witnesses.py`，逐条打补丁 → 门红 → 复原）
```
[CAUGHT] #1 C2  (routes_seg_encoding)              rc=1 restore=True
[CAUGHT] #2 C8  (envvar_screen_consumes_pending)  rc=1 restore=True
[CAUGHT] #3 C10 (envvar_dialog_remember_keys)     rc=1 restore=True
[CAUGHT] #4 C11 (open_alarm_list_intent)          rc=1 restore=True
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
- run **40134937808**（#2634，attempt 1）`conclusion=success`，job `120994351104`
- `head_sha = 64582201877c1849ca878a1a6e712c4e3d5549d5` —— 与绿时的本地 `git rev-parse HEAD` 逐字符一致
- 判读（1,484,201 B job 日志逐条核过）：
  - `Summary: 112 passed, 0 failed` **恰一条**；其余 7 条 `Summary:` 是子套件（28/5/14/36/10/22/10 passed）
  - `ALL 344 CASES PASS` 恰一条（= 本支六门的新增臂全部在 CI 里被 `test_scan.py` 断言）
  - 编译错误 ` e: ` = 0
  - 26 步里唯一 `skipped` = `Publish to Releases`（main 门控）
  - 本门在 CI 里的门头：`[64/112] F/ISSUE-06 deeplink settings routes`


## 7. 残留
- **R1** 逐插值判据把 `seg(...)` 当唯一编码器拼写：将来若引入别名（`fun enc(v) = seg(v)`）并写
  `$enc(x)`，判据会红。／触发条件：出现别名编码器声明。／触发后方案：判据改成「派生编码器集合
  （`seg` + 值为 `seg(...)` 的别名）」。／搁置理由：本文件 KDoc 明确要求**单一口径**，别名本身
  就是要被拒绝的写法，红了正好逼回头改判据。
- **R2** C8 只判键是否**直接读**状态；`LaunchedEffect(key = remember { pendingCreate })` 之类间接键
  未覆盖（当前无生产者）。／触发条件：出现间接键写法。／触发后方案：键表达式的数据流追踪一跳。
  ／搁置理由：无生产者；且真正落地时 Compose 语义仍要求 key 求值读到状态。
- **R3** C10 的根集合是标识符级：若初值改成 `mutableStateOf(seedFor(editEntry, prefillKey))`，
  `prefillKey` 不在**根**上（根是 `seedFor`）→ 该半被漏。／触发条件：引入初值 helper。
  ／触发后方案：允许一跳同文件委托（读 helper 体的根集合）。／搁置理由：当前 4 个字段都是直读。
- **R4** C11 的 Intent 变量派生只认 `val x = …Intent(`；`startActivity(Intent(…ACTION_SHOW_ALARMS))`
  直传形态走宽松兜底（要求实参里含 `ACTION_SHOW_ALARMS`），不校验 flags。／触发条件：改成直传。
  ／触发后方案：解析 `startActivity(` 实参并配平。／搁置理由：现状是 `val` 形态，直传兜底不会漏启动。

## 8. 复现
### 复现
```sh
# 本门
python3 scripts/scan/deeplink_settings_routes_check.py .
python3 scripts/scan/deeplink_settings_routes_check.py --selftest

# 见证反攻（20 条，逐条打补丁 → 门红 → 字节级复原）
python3 /var/minis/shared/work/meta-wide/probe-12b/parse_witnesses.py
python3 /var/minis/shared/work/meta-wide/probe-12b/verify_witnesses.py "$PWD" --only deeplink_settings_routes_check.py --out after
```

装置脚本在 `/var/minis/shared/work/meta-wide/probe-12b/`（`parse_witnesses.py` 解析任务书的
find/replace 块，`verify_witnesses.py` 打补丁 + 跑门 + sha256 复原）。

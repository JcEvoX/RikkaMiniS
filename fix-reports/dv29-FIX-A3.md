# dv29-FIX-A3 — offload 分页参数静默回退/负数告警 + market fallback 畸形 URL（A-05 · A-07）

## 1 身份链

| 项 | 值 |
|---|---|
| 基线 main | `b82aa8dc`（worktree `git rev-parse --short HEAD` 逐字符一致） |
| 分支 | `fix/dv29-a3-offload-page-size` |
| worktree | `/var/minis/shared/work/deep-audit-1049/a3` |
| 提交序列 | `db7c1dff` 生产 → `0ee8dc6d` 测试 → `39886a34` 门 → `7a7a40a3` 报告（**CI 绿于此**）→ 报告回填（仅 `fix-reports/`） |
| 门/夹具计数 | **111 门 / 342 夹具不变**（0 新建门，只扩 `offload_cli_args_check.py`；`scan.sh` / `test_scan.py` 零触碰） |
| 改动面 | 生产 6（1 新建 `OffloadPageSize.kt`）· 测试 2（新建）· 门 1 · 报告 1 |

## 2 缺陷与先证

### 2.1 A-05 成立（P1）—— 五个 handler 的分页参数矩阵（修前 / 修后）

修前值全部来自**真机实测**（2026-10-24，app `1.0.0+2597`，release 包；`proot info: … exit=N` 取真实退出码，S34 口径）：

| handler · 子命令 | 读取点（修前） | 默认/上限 | 非法值行为（修前，实测） | 修后 |
|---|---|---|---|---|
| `android-calendar list` | `:304` `getInt("limit") ?: getInt("max") ?: DEFAULT_LIMIT` | 50 / 无上限 | `--limit -5` → **exit 0**、`events:[]`、`_warning:"Results truncated by --limit. Returned -5 of 0 total records."`（`total > limit` 对任何 total 都成立 ⇒ 空窗口被播报成"被截断"） | exit 2 + `--limit expects an integer >= 1, got -5` |
| `android-photos list` | `:179` `(getInt("limit") ?: getInt("max") ?: 20).coerceIn(1,100)` | 20 / 100 | `--limit 0`/`-1` → exit 0、**count 1**（夹到 1）；`--limit abc` → exit 0、count 20（默认）；`--limit 100000` → exit 0、count 100（静默截断） | exit 2，范围 1..100 |
| `android-photos near` | `:283` 同上 | 20 / 100 | `--limit 0` → exit 0、空页 + `hint`，与"附近没有照片"不可区分 | exit 2，范围 1..100 |
| `android-photos album` | `:533` `(… ?: 50).coerceIn(1,200)` | 50 / 200 | 同族（`--id/--name` 缺失会先报 exit 2，故需合法选择器才能观察到） | exit 2，范围 1..200 |
| `android-notification list` | `:559` `get("max")?.toIntOrNull()?.coerceAtLeast(1) ?: 50` | 50 / 无 | `--max 0` → exit 0、**count 1 / total_active 11**；`--max abc` → exit 0、count 11 | exit 2 + `--max expects an integer >= 1, got 0` |
| `minis-sessions-cli list/search/messages` | `:425-427` `getInt("limit") ?: DEFAULT_LIMIT` + `coerceIn(1, MAX_LIMIT)` | 50 / 100 | `--limit 0` → count **1**；`--limit 1000` → count **100**；`--limit abc` → count 50 | INVALID_ARGS 信封 + exit 2 + HELP（复用了既有 `InvalidDateArg` 的同一条通道） |
| `android-contacts list/search` | `:607` `resolvePageSize`（1048-A06） | 50(20) / 500 | `--max 0` → **exit 2** + `--max expects an integer between 1 and 500, got 0` | **不变**（本支的契约基准，语义原样保留） |

**机制链（可执行）**：`--limit/-max` 被 `getInt` 读 ⇒「写错但读不出」与「没写」合并为 `null` ⇒ 落到默认值；或值被 `coerceIn(1,…)` 夹住 ⇒ 给调用方的 `count` 是**别人问的数字**而不是它得到的数字 ⇒ agent 无法区分"你要 0 我给 1"和"这里只有 1 条"。这正是 1048-A06 在 contacts 上已经判定的形态，只是没扩散到同族。

**报告失真 1 条**：任务书 §2.3 提到的「`CALENDAR` 常量（:170）」**真树不存在**——`:142-161` 是 `CONTACTS/CONFIG/…/PLAYER` 文件名常量，`:170-179` 是 `PAYLOAD_FILES` 列表里的一个裸字符串（只被 `check_offload_json` 使用，与分页无关）。报告「门只扫 Contacts」这一半**准确**：`check_contacts_page_size(contacts_src)` 的唯一入参就是 contacts 源文件。

### 2.2 A-07 成立（P2）—— market fallback 畸形 URL

- `OpenOffloadHandler.maybeFallback`（修前 `:124-134`）：`Uri.parse(url).getQueryParameter("id") ?: url.substringAfter("details?id=").substringBefore('&')`；`substringAfter` 未给 `missingDelimiterValue`，无分隔符时**返回 receiver 本身** ⇒ `market://search?q=foo` ⇒ `pkg = "market://search?q=foo"` ⇒ `https://appgallery.huawei.com/app/market://search?q=foo` 真的被 `tryOpen` 打开。
- **调用条件**：`maybeFallback` 只在 `tryOpen` 的 `catch (ActivityNotFoundException)` 里调用；`url.startsWith("market://") && OsCompat.isHuawei` 才进 fallback。
- **递归深度（报告措辞修正）**：原实现**只有一层** —— 重定向目标是 `https:`，第二次 `tryOpen` 失败时 `market://` 判断不成立 ⇒ 直接落到 `no_handler` 信封。所以「递归打开」应读作「一次畸形跳转」，无止递归。本支把它**结构上钉成一层**（`MAX_FALLBACK_HOPS = 1`），不再依赖"目标是 https"这个偶然事实。
- **未成立半边**：本机是 Redmi（`OsCompat.isHuawei == false`），真机无法触发；该支证据为静态 + 纯函数单测（§5 L3）。

## 3 修法（阶梯第 2 级 + 第 6 级）

**停在第 2 级（代码库里已有）：`ContactsOffloadHandler.resolvePageSize` 的契约已经是正确的，本支把它提升为单源实现，其余四个同族改走它** —— 不新写第二份解析器、不新引依赖、不加配置项。

- 新建 `sandbox/offload/OffloadPageSize.kt`：`PageSizeSurface`（契约声明：`handlerFile` / `names` / `default` / `max` / `legacyShortMax`）+ `PageSizeOutcome.resolve(args, surface, default)` + `OffloadPageSize.SURFACES` 注册表。
- 细节契约（与 contacts 逐字兼容）：`--max abc` → `got 'abc'`；`0/-5`（能解析成整数）→ `got 0` / `got -5`（无引号，与既有断言一致）；超上限 → 拒绝不平移；裸标志 → `--max expects …`（无 `got`）；`-n` 存活（contacts）→ `(short form of --max)`。
- 每 handler 只留一行 `when`：`Invalid -> return NativeOffloadResult(2, "<tool> <sub>: ${size.message}\n")`；Sessions 走既有异常通道 `InvalidPageSize` → `INVALID_ARGS` 信封 + exit 2 + HELP。
- **上限归一处**：contacts 的 `MAX_PAGE_SIZE` 由 `const val 500` 改为 `requireNotNull(OffloadPageSize.CONTACTS.max)`（既有测试断言 500 仍成立，且不再有第二个 500）。
- **默认值**：`default` 作为参数传入而非全局唯一值 —— `contacts list=50 / search=20`、`photos album=50 / list,near=20` 各取己值，注册表不需要重复条目。
- **为什么不给 calendar / notification 加人为上限**：calendar 的结果集已被时间窗约束且两遍扫描报 `total_available`，notification 是设备活动通知全集（实测 11 条）；冒然加上限会把"能做到的请求"变成错误 ⇒ 用 `max = null`（只要求 ≥1），并在 `PageSizeSurface` KDoc 写明理由（`ponytail` 第 3 段在 KDoc 里：失效条件 = 出现真实的超大窗口/超大通知集）。
- **A-07**：新增纯函数 `marketPackageOf(url): String?`（手写 query 解析，避免 `Uri` ⇒ 可离机测），并给 fallback 加 `hop` 上限 `MAX_FALLBACK_HOPS = 1`。
- **一处刻意的形态瑕疵（记录）**：`ContactsOffloadHandler.resolvePageSize` 保留 `PageSize`（嵌套类型）作为"面"，把共享 `PageSizeOutcome` 映射回它 —— 因为 `OffloadCliArgsTest` 的既有断言全部锚在 `ContactsOffloadHandler.PageSize`（禁用改既有断言语义）。解析/校验逻辑仍是单源，映射只有 4 行；触发合并的条件见 §6 R5。

## 4 覆盖面

- **调用点普查**：`PageSizeOutcome.resolve(` 全仓 **7 处 / 5 个 handler**（contacts 1、calendar 1、photos 3、notification 1、sessions 1）；`grep -rn 'getInt("limit")\|getInt("max")\|coerceIn(1,' sandbox/offload/*Handler.kt` 在 5 个注册 handler 内**只剩注释**（`code_only` 已剥）。
- **注册表覆盖**：6 个 surface ⇄ 5 个 handler 文件（photos 两个 surface 共用一个文件），`SURFACES` 全列；gate PG0 双向查（声明未注册 / 注册未声明 / 文件不存在）。
- **同族未修（半径外，登记 §6）**：`Accessibility:350`、`Debug:210`、`Fastio:314/418`、`Speech:165`、`Weather:63/64`、`ConfigBridge:132` 仍保留 `?: default` / `coerceIn` 形态。
- **门的作用域是派生的**：PG 系列不写死文件名 —— 从 `PageSizeSurface(handlerFile = …)` + `SURFACES` 求作用域，从 `PageSizeOutcome.resolve(` 派生消费方，从 `_exit2_producers` + `throw InvalidPageSize(` 派生"能拒绝"的判据。

## 5 验证四层

### L1 本门（`scripts/scan/offload_cli_args_check.py`）＋真树

- 真树：`python3 scripts/scan/offload_cli_args_check.py .` → **PASS（rc=0）**。
- 自检：`--selftest` → **OFFLOAD_CLI_ARGS_SELFTEST=PASS**；新增 **13 臂**（期望集手写，不 from 运行结果派生）：非数字坍缩 `{PG3}` · 裸标志落默认 `{PG3}` · 下界删除 `{PG3}` · 上界不比较 `{PG3}` · 第二条默认路径 `{PG3}` · handler 退回 `getInt ?: default` `{PG1,PG2}` · handler 自己再夹一次 `{PG2}` · 收集方把拒绝当默认页 `{PG4}` · `-n` 拒绝不再按 surface 门控 `{PG5}` · `-n` 拒绝整体删除 `{PG5}` · 声明 surface 未注册 `{PG0}` · 注册 surface 指向不存在的文件 `{PG0}` · `-n` 别名改写丢失 `{PG5}`；绿臂 = 0 findings。原有全部臂保持绿。
- 残留的 CN 族脚手架（`_contacts_page_resolver` / `GREEN_CONTACTS_PAGE` / 7 条 CN 臂）随契约迁移整体退场，语义由 PG3/PG4 接续（`CN1` 的 lossy getInt、`CN2` 的 `-n`、`CN3` 的双界、`CN4` 的 caller 必拒）。

### L2 邻门 10/10 rc=0

`debug_leak_guard` · `discarded_return_guard` · `file_provider_paths_coverage_check` · `log_url_redaction_guard` · `offload_host_path_check` · `readonly_mount_write_guard` · `router_lock_check` · `schedule_date_source_check` · `share_dir_prune_check` · `calendar_events_nullable_predicate_check --root .`（该门只吃 `--root`，位置参数会 exit 2 —— 已知接口形态差异，非本支引入）。

### L3 真 kotlinc + JUnitCore（沙箱实跑，非 CI）

- POST：`OffloadPageSizeTest` **OK (10 tests)**；`MarketUrlFallbackTest` **OK (3 tests)**。
- 变异臂（源文件字节副本 + 单点替换，测试文件一字未改）：
  - **m1** 上界比较删除（`value < 1 || (surface.max != null && …)` → `value < 1`）→ **4 failing**（contacts 501 / photos 100000 / album 201 / sessions 1000 四条上界用例，逐条对应）；
  - **m2** 裸标志拒绝循环删除 → **3 failing**（contacts 裸 `--max` / calendar 裸 `--limit` / notification 裸 `--max`）；
  - **m3** market 解析回到"缺分隔符返回 receiver"（`return null` → `return url`）→ **1 failing**（`market://search?q=foo` 期望 null 实得整串）。
- 切片一致性：`marketPackageOf` 由脚本从真文件按花括号匹配切出（382 B，脚本内断言与源逐字节同源），PASS 是对**出厂代码**的陈述。

### L4 分支 CI —— **run `40069595695` / job `120799966638` success（attempt 1）**

- 身份三方逐字符一致：本地 HEAD = `ls-remote` = `run.head_sha` = **`7a7a40a3ffbf9aa9a3788fd913678de14e007688`**。
- 26 步全部 success，**唯一 skipped = `Publish to Releases`**（main 门控；从 steps API 判，日志里查不到 —— skipped 步骤没有任何输出行）。
- 日志 1,463,221 B 判读（`probe/l3/judge_ci.py`，14 项全 OK）：`Summary: 111 passed, 0 failed` 恰一条且其余 7 条步骤级 Summary 全 `0 failed` · `ALL 342 CASES PASS` 恰一条 · 门头 `[1/111]..[111/111]` 共 111 且唯一有序 · ` e: ` 0 · 游离 `FAILED` 0 · `OffloadPageSizeTest` / `MarketUrlFallbackTest` 无失败行且逐条在册。
- **警告对比**：本支触碰过的 9 个文件里只有 **1 类**警告 —— `PhotosOffloadHandler.kt:1241 getLatLong(FloatArray) is deprecated`（×3）。判定为**既有**：`git diff b82aa8dc..HEAD -- PhotosOffloadHandler.kt | grep -c getLatLong` = **0**，而 `git show b82aa8dc:…PhotosOffloadHandler.kt | grep -c getLatLong` = **1**（基线即有，只是行号被本支前移 9 行）。
- **报告回填后的源码树等价**：本报告与 §1 的提交序列之后的 `docs(offload)` 提交只动 `fix-reports/*.md`（不在 `build-apk.yml` 的 paths 内），`git diff --name-only 7a7a40a3 HEAD -- . ':(exclude)fix-reports'` 若输出为空即证明 CI 结论对当前代码树成立。

## 6 残留（每条带触发条件 + 触发后方案）

- **R1 真机行为级未验**：`android-calendar list --limit -5` / `android-photos list --limit 0` / `minis-sessions-cli list --limit 1000` / `android-notification list --max 0` 应改为 exit 2 + 一行用法文本（修前实测见 §2.1）。触发条件 = 本支合入 main 并发布下一版 APK；触发后方案 = 按 §2.1 表格逐条跑，比对 exit 码与文本。
- **R2 A-07 真机不可验**：本机 `OsCompat.isHuawei == false`。触发条件 = 手上有 HMS 设备 / 用户报告华为机 `android-open market://…`；触发后方案 = `market://search?q=foo` 应得 `no_handler` 信封（exit 1），**不得**出现 appgallery 链接。
- **R3 同族未修（半径外，P1/P2 混合）**：`AccessibilityOffloadHandler:350`（`ui dump --limit`，`?: 50`）、`SpeechOffloadHandler:165`（`--max` 候选数，`?: 3`）、`FastioOffloadHandler:314/418`（find/grep 命中上限，`coerceIn(1, HARD_MATCH_LIMIT)`；该工具本来就有 `truncated` 字段如实上报）、`DebugOffloadHandler:210`（只透传、且 DEBUG-only）、`WeatherOffloadHandler:63/64`（`--hours/--days`，非分页语义）。触发条件 = 下一次分页/上限审计波次；触发后方案 = 各自 `PageSizeSurface` 注册一行 + 调用点改一行（机制已就位，成本 ~10 行/处）。
- **R4 `minis-config get --page-size` 仍静默默认 + 夹取**（`config/ConfigBridge.kt:132`：`if (pageSize > 0) pageSize else DEFAULT_PAGE_SIZE).coerceIn(1, MAX_PAGE_SIZE)`）。触发条件 = 有人真的用 `--page-size 0` / 超大值 / 或该 CLI 变成 agent 常用路径；触发后方案 = 复用 `PageSizeOutcome`，`ConfigBridge` 自带 `MAX_PAGE_SIZE = 100`（`config/` 面属 A4 支领地，本支不碰）。
- **R5 contacts 的 `PageSize` 面（映射层）**：等某个波次允许改 `OffloadCliArgsTest` 的既有断言时，把面向类型直接换成 `PageSizeOutcome`，删掉 4 行映射。触发条件 = 下一次要动 `OffloadCliArgsTest` 的断言；触发后方案 = 全量替换 `ContactsOffloadHandler.PageSize` → `PageSizeOutcome`。
- **R6 sessions 的 `--offset` 仍在静默夹取**（`:309` `(getInt("offset") ?: 0).coerceAtLeast(0)`：负数变 0）。工单只点名 `--limit`（A-05 半径），故仅登记。触发条件 = 有人真用负 offset / 分页审计扩到 offset；触发后方案 = 同一解析器加一个 `resolveOffset`（拒绝负数，`Invalid` 走同一条 INVALID_ARGS 通道）。
- **R7 gate 的 `code_only` 不是字符串感知的**：`//` 出现在字符串里（`"market://"`）时会把该行截断、括号失配，进而污染后续 `_statement_ifs` 扫描（本次首跑就因此让 `H3` 在 `OpenOffloadHandler` 上假红一次，把 helper 移到文件末尾即恢复）。触发条件 = 又有新代码在 help 守卫**之前**引入含 `//` 的字符串字面量；触发后方案 = 给 `code_only` 加字符串/字符字面量跳过（改动面覆盖全部 111 门，须单独立项 + 全量回归）。
- **R8 session `--limit` HELP 文案改了两行**（补 "outside that range is a usage error, exit 2"）：无测试钉该文案（`grep 'Max results'` 0 命中），但下游若有 prompt 复用该文本需注意。

## 7 未验证边界

- 生产代码的**整体编译**只由 CI 覆盖：沙箱里 `Calendar/Photos/Notification/Sessions/Open/Contacts` 的依赖链会牵出 `BuildConfig`、androidx、kotlinx-coroutines，本地编译链不可闭合（L3 只覆盖新解析器 + 纯函数切片）。
- calendar / notification 的 `max = null`（无上限）是**刻意的现状保持**，不是遗漏：修前它们也没有上限，本支只加了"拒绝 <1 与非数字"。若将来要求全 CLI 一致的有界分页，那是一条独立决策（会改变"批量索取能成功"的既有行为）。
- 华为 fallback 的真实跳转（AppGallery 页面内容）未验，只验到"不再拼出畸形 URL"。

## 8 装置

- 批次目录：`/var/minis/shared/work/deep-audit-1049/a3/probe/l3/`
  - `slice_market.py`（按花括号切片 + 同源断言）· `MarketUrlSlice.kt`
  - `mutate.py`（m1/m2/m3 单点变异）· `mut/m1..m3/`
  - `arm0.sh`（POST 臂：真源 + 真测试）
  - `new_check.py` / `new_fixtures.py` / `new_arms.py` / `splice_gate.py`（门的三段拼接，锚文本不锚行号）
- `probe/` 在 `.gitignore:141`，worktree 私有，不入库。

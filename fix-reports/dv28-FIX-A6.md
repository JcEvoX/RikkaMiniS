# dv28-FIX-A6 — `android-contacts` 页大小契约：拼错的值不再被当成「没拼」

- 波次：deep-audit-1048 / 支 A6（工单 A-06）
- 基线：`eea9f407`（main = dv27 并集）
- 分支：`fix/dv28-a6-contacts-max-args`
- 生产面：`src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/ContactsOffloadHandler.kt`
- 门：`scripts/scan/offload_cli_args_check.py`（本支独占，**0 新建门**；门数 111 / 夹具 342 不变）
- 测试：`src/android/app/src/test/java/com/rikkaminis/app/sandbox/offload/OffloadCliArgsTest.kt`

---

## §1 一句话

`--max` / 旧别名 `-n` 的解析把「拼了但用不了」与「没拼」合并成同一个值：`--max abc` 静默回默认页、`--max 0`/负数返回空页且 exit 0、`search -n abc Alice` 去搜 "abc" 并丢掉 "Alice"。本支把解析结果改成三态（可用 / 拒绝）并在两个收集点把拒绝落成 exit 2。

## §2 缺陷（回真树取证）

| # | 触发 | 修复前行为 | 机制 |
|---|---|---|---|
| 1 | `android-contacts list --max abc` | 列出 50 条，**exit 0** | `listMax` 走 `getInt("max")`：拼了但读不出 ⇒ `null` ⇒ 与「没拼」同路，落到 `default` |
| 2 | `android-contacts search -n abc Alice` | 按名字 **"abc"** 搜索、丢掉 "Alice"，页大小 20，exit 0 | `bindLegacyShortMax` 只把**数字**的 `-n N` 改写成 `--max N`；非数字时 `-n` 变裸 flag、`abc` 落进 `positional`，而 `searchQueryOf` 读 `positional[1]` |
| 3 | `android-contacts list --max 0` / `--max -5` / `-n 0` | 返回 `[]`，**exit 0** | 0/负数原样交给 `collectContactPage`，`while (rows.size < max)` 一次都不进 |
| 4 | `android-contacts list --max` （无值）、`--max=` | 默认页，exit 0 | `OffloadArgs` 把「无后继 token」的 `--max` 记成**布尔 flag**，`values` 里没有 → `getInt` 仍返回 `null` |

三条路径同一个形状：**把「用户拼了、但 handler 不能用」折叠成「用户没拼」**，于是错误以「成功」的形式回到调用方（agent 会把空页读成"没有联系人"）。

## §3 修法（改动阶梯：第 6 级「能一行为之则一行」→ 落在第 5 级）

- **不做**新框架、不改 `OffloadArgs`（共享解析器，本支半径外）、不动 `get`/`delete` 的 id 槽。
- `listMax(…): Int` → `resolvePageSize(parsedArgs, default): PageSize`，`PageSize` 是 `ContactsOffloadHandler` 的**类内** sealed 类型（`Ok(value)` / `Invalid(message)`）。之所以是类内而不是 companion 内：companion 里嵌套的类型在外部只能写 `ContactsOffloadHandler.Companion.PageSize`，测试与门都要一层别扭（本支第一版就把它放在 companion 里，被本地编译当场否掉，见 §6）。
- 三种拼法各自一个出口：
  - `--max V` / `--max=V`：读**原始字符串**（不用 `getInt`），`toIntOrNull()` 失败或不在 `1..MAX_PAGE_SIZE` ⇒ `Invalid`（消息带实际值）；
  - 裸 `--max`（无值）：`hasFlag("max")` ⇒ `Invalid`；
  - 存活的 `-n`：`hasFlag("n")` ⇒ `Invalid`（**fail-closed**：`-n` 文档上就是页大小，非数字就是用法错，见 §6-2）；
  - 都没拼：`Ok(default)` —— 唯一能回答默认值的路径。
- 两个收集点 `doList` / `doSearch` 用 `when (val size = resolvePageSize(...))`，`Invalid` 分支 `return pageSizeRefusal(sub, message)` = `NativeOffloadResult(2, "android-contacts $sub: <range>…\n$HELP_TEXT")`。
- `doSearch` 把页大小解析**前置到查询解析之前**：`search -n abc Alice` 的查询槽本来是歧义的（"abc" 在里面），先拒绝再谈查询，语义上没有"先搜错再报错"的中间态。
- `HELP_TEXT` 写明 `1..500`、默认 50/20、`-n` 是 `--max` 短形式、非法值是 exit 2（不再"静默默认/空页"）。
- 新增 `MAX_PAGE_SIZE = 500`：页大小是 provider+Binder 成本（每页每 data kind 两条批量查询），过界属于用法错，**钳制会以成功回报一个没被尊重的请求**（与 `OffloadArgs.getBool` 的 refuse-instead-of-guess 同调）。
- **永久不砍清单**：输入校验（范围+负数）、终止路径（拒绝必落 exit 2 + help）、跨层完整性（`HELP_TEXT` 与实现同步）、硬件/成本常量显式命名——全在。

## §4 门（`offload_cli_args_check.py`，+384/−2，0 新建门）

新增判据 `check_contacts_page_size()`，锚点全部**派生**而非硬拼：

| id | 判什么 | 派生方式 |
|---|---|---|
| CN0 | 恰好一个页大小解析器 | 「参数里有 `default` 且函数体读 `max` 这个 CLI 键」——键名是 CLI 契约本身，不是内部拼写 |
| CN1 | 默认值只能回答「没拼」 | ①禁止 `getInt(` （折叠点）；②必须检查裸 `--max`（`hasFlag("max")`）；③恰好一处 `return <variant>(default)`，且拒绝构造必须在它**之前** |
| CN2 | 存活的 `-n` 必须被拒 | 函数体必须有 `hasFlag("n")`，且其后必须构造拒绝；禁止从 `positional[1]` 反读别名的值 |
| CN3 | 页大小必须正、必须有界 | 闭包（解析器 + 它调用的一跳 helper）里必须有正数下界比较；上界常量从 `const val *MAX*` 声明派生并要求被比较 |
| CN4 | 每个页收集点必须过解析器、且能落 exit 2 | 调用点从 `collectContactPage(` 派生（第三个调用点自动覆盖）；exit-2 生产者从「函数体含 `NativeOffloadResult(2`」派生，表达式体也能识别 |

**带牙臂 7 条**（期望集合手写自 CN 表，绝不从运行结果推导）：`m-getInt`→{CN1}、`m-bare-max`→{CN1}、`m-drop-n`→{CN2}、`m-no-lower`→{CN3}、`m-default-page`→{CN4}、`m-wrong-resolver`→{CN4}、`m-no-default-param`→{CN0}；另加「绿夹具必须 0 命中」控制臂。

**PRE 臂**：拿 `eea9f407` 的真文件跑同一函数 ⇒ 红 **CN1 / CN2 / CN3 / CN4**（4 条，工单要求的 CN1、CN3 都在其中）。

**旧判据同步**：C9（解析器签名）与 V1b（`doSearch` 必须复用解析器）跟着改名从 `listMax` → `resolvePageSize`；C9 的带牙臂与 V1 两条变异臂的锚串同步更新（否则会静默不匹配 ⇒ 假绿）。

## §5 验证四层

**L1 本门**（真树）：`offload_cli_args_check.py .` → `OK`（0 命中）；`--selftest` → `OFFLOAD_CLI_ARGS_SELFTEST=PASS`（7 带牙臂 + 绿控制臂全部符合）。

**L1 全量门**：跨会话负载高峰（8 个兄弟会话同时跑 `gate_run --full`，load ≈ 7）导致整批 runner 反复撞 `Function not implemented`(ENOSYS) 与 `can't create /tmp/.minis_cwd`，`scan.sh` 一次都跑不完整。改按 `scan.sh` 的**有序调用清单**逐门直跑（含 `--root` 形态的两门），**109/109 rc=0**；`test_scan.py` → **ALL 342 CASES PASS**（夹具数未变）。111 门的权威计数以 CI 的 `Summary: 111 passed, 0 failed` 为准（§5-CI）。

**L2 邻门 8/8 rc=0**：`offload_permission_reset_sync_check` · `offload_host_path_check` · `offload_relative_path_unify_check` · `a11y_handler_contract_check` · `debug_leak_guard` · `schedule_date_source_check` · `log_url_redaction_guard` · `file_provider_paths_coverage_check`。

**L3 真 kotlinc 2.0.21 + JUnit**（装置见 §8）：
- **测试切片**（真实 handler 的 companion 体逐字抽出 + `OffloadArgs.kt` 原文 + 两个桩）：
  - POST `PageSizePostTest` **OK (9 tests)**：`--max abc` → `--max expects an integer between 1 and 500, got 'abc'`；`--max 0` / `-5` → 拒绝；`search -n abc Alice` → 拒绝且 `positional[1]` 仍是 "abc"（证明拒绝发生在查询槽被消费之前）；`search -n 10 Alice` → 查询 "Alice" + 页大小 10；`list` → 50、`search` → 20；`get 42` / `get -n 10` → id 42 / 10；`--max 500` OK、`501` 拒绝；裸 `--max` / `--max=` 拒绝。
  - PRE `PageSizePreTest`（基线 companion 切片）**OK (4 tests)**：把缺陷值写死断言——`--max abc` ⇒ 50、`--max 0` ⇒ 0、`-5` ⇒ -5、`search -n abc Alice` ⇒ 查询槽 "abc" + 页大小 20、`--max 501` ⇒ 501。**基线真的就是审计说的那样**，不是报告臆测。
  - 变异 5 臂，各**恰红**其判据对应的用例：m1(getInt) 红「unreadable」+「bare --max」两条同类用例；m2(丢 `-n` 分支) 红 `search -n abc Alice` 一条；m3(允许 0) 红 `0/-5` 一条；m4(去掉上界) 红 `500/501` 一条；m5(裸 `--max` 直落默认) 红 `bare --max` 一条。**反向**：每臂其余 4~8 条用例保持绿 ⇒ 装置有牙且半径精确。
- **真实处理器 + 真实单测的编译/运行夹具**（`probe/compile/`：真 `ContactsOffloadHandler.kt` + `OffloadArgs.kt` + `OffloadOutput.kt` + 逐字抽出的 `resolveMaybeRelative` + 12 个 Android/AppLogger/权限/协程桩）：`kotlinc` 编译 **rc=0**，`java … JUnitCore OffloadCliArgsTest` → **OK (57 tests)**（含本支新增 9 条与全部源形状探针）。**对照臂**：同一份新测试对基线 handler 编译 → 失败（`unresolved reference 'PageSize'` 等）——旧 API 连"表达这个契约"都做不到，这正是 fail-closed 的形态。

**L4 CI**：见 §5-CI。

## §5-CI

**run `40049408945` / job `120738469252`**，attempt 1，event `workflow_dispatch`，conclusion **success**（01:21:23Z 创建 → 01:41:13Z 完成，约 20 min）。

- **身份链三方逐字符**：本地 HEAD == `ls-remote` origin/fix/dv28-a6-contacts-max-args == `run.head_sha` = `97e5da76b93803b51a68b68175c8af675b6e68d6`。
- **26 步**：not_ok = `[(23, 'Publish to Releases', 'skipped')]` —— 唯一 skipped 就是 main 门控的 Publish，其余 25 步全 success（步骤结论取自 steps API，不从日志文本猜）。
- **日志判读 12/12 ALL GREEN**（`probe/ci/judge_a6.py`，日志 1,461,848 B）：
  - boxed 总摘要 `║  Summary: 111 passed, 0 failed` 恰一条 @:985 —— 单门自带的 `Summary: N passed` 行不是总数（判读器第一版把 8 条 Summary 全收集进来报「8 条」，改为只认 boxed + `111 passed` 形态）；
  - `✅ ALL 342 CASES PASS` 恰一条 @:1378；
  - 门头 `[1/111]..[111/111]` 连续、门名 111 个互异（第一版按「第一个空格前的词」取名 ⇒ 85 unique 假红；改为取到 ` ━` 为止）；
  - 本门见证：`offload_cli_args_check selftest has teeth (OFFLOAD_CLI_ARGS_SELFTEST=PASS)` @:1234 + 真树 OK 行；
  - Kotlin 编译错（` e: ` 行）= 0；游离 FAILED 行 = 0；
  - **本支 3 文件（`ContactsOffloadHandler.kt` / `OffloadCliArgsTest.kt` / `offload_cli_args_check.py`）编译警告 0**；
  - 新页大小用例 9/9 逐条 PASSED；`OffloadCliArgsTest` **57/57 PASSED**（含 20 条既有 `-n`/dispatch 用例与 12 条 negative control）。
- **报告回填的源码树等价**：`git diff --name-only 97e5da76 HEAD -- . ':(exclude)fix-reports'` = 空 ⇒ CI 结论对当前源码树成立（报告提交不改代码面）。
- **门数 111 / 夹具 342 不变**（0 新建门；`scan.sh`/`test_scan.py` 施工期冻结面零触碰）。

## §6 与任务书的偏离、以及任务书自身的失真

1. **偏离：`PageSize` 是类内嵌套类型，不是 companion 成员。** 任务书只要求「允许改成密封结果」。第一版放在 `companion object` 里，本地编译夹具当场报 `unresolved reference 'PageSize'`（companion 内嵌套类型对外须写 `ContactsOffloadHandler.Companion.PageSize`）。挪到类内后 `ContactsOffloadHandler.PageSize` 直接可用，测试与门都自然。**教训**：本地编译夹具在 push 前抓到了这个，省一轮 CI。
2. **偏离（需要在报告里论证）：`search -n Alice` 现在**是用法错****（任务书默认建议 fail-closed，我选了它）。理由：`-n` 在 `HELP_TEXT` 里就是 `--max` 的短形式，"`-n` 后面跟一个不是数字的东西"没有第二种合理意图；把它解释成「裸 flag + 真实查询词」会让**同一个 token 既能是页大小又能是查询词**，而这正是 `search -n abc Alice` 静默搜错的那个歧义。代价：以前 `search -n Alice` 能搜到 "Alice"，现在 exit 2；`bindLegacyShortMax` 的「只改写数字」契约原样保留（改成 `--max Alice` 会把错误意图写进 argv，且会破坏那条契约）。
3. **任务书的 L3 清单**要求 `get 42` → id 42 ✓、`list` 无参 → 50 ✓、`search` 无参 → 20 ✓、`--max 0/-5` 拒绝 ✓、`search -n abc Alice` 拒绝 ✓、`search -n 10 Alice` → 查询 "Alice" + max 10 ✓。
4. **任务书要求「PRE 臂必须红 C-CN1/C-CN3」**：本支的门里对应 CN1/CN3，PRE 实测红 CN1/CN2/CN3/CN4 四条（更严，不是更松）。
5. **任务书说「neighbor 门跑这 5 个」**：`offload_permission_reset_sync_check` / `offload_host_path_check` / `schedule_date_source_check` / `debug_leak_guard` / `a11y_handler_contract_check` 全部存在且 rc=0（另加 3 个语义邻门同样 rc=0）。

## §7 残留（带触发条件）

- **R1（P1，真机行为级）**：真机上 `minis-contacts list --max abc` → exit 2 且 help 出现；`--max 0` 同样；`search -n Alice` → exit 2。触发条件：下次真机窗口，一次开齐（本支无真机权限）。
- **R2（P2）`-n` 分支的消息不指名具体 token**：`search -n abc Alice` 的报错只说 `-n expects an integer …`，不说 "接了 'abc'"。原因是解析后 `positional[1]` 在两形态（`search -n abc Alice` 与 `search Alice -n`）下不同，无法从 `OffloadArgs` 反推真实 token，指名就会造假。触发条件：若真机反馈"看不懂错误"，再引入 argv 级错误码（需要 `bindLegacyShortMax` 返回诊断）。
- **R3（P2）CN1 的「拒绝在默认值之前」是源码顺序判据**：把解析器改写成 `return PageSize.Ok(default)` 在前、拒绝在后的**等价**形态（例如 `elvis` 链）会被判红。触发条件：真出现等价但顺序不同的写法，改成数据流判据。
- **R4（P2）上界常量靠名字派生（`*MAX*`）**：`DEFAULT_LIST_MAX` 也命中该模式，于是「没有上界常量」这一半在只剩 `DEFAULT_LIST_MAX` 时不会红，只会红「从未被比较」那一半。触发条件：若将来出现第二个 `*MAX*` 常量，收紧为「必须在比较里出现且 ≥2」。
- **R5（P2）`collectContactPage` 之外的成本入口不在本判据内**：本支只判页收集点。触发条件：若 contacts handler 新增别的按 `max` 取数的路径，扩 CN4 的派生源。
- **R6（P3）** `searchQueryOf` 仍读 `positional[1]`：本支把 `-n` 的歧义在解析器里掐死了，但函数本身没变（改它属于半径外）。

## §8 装置与复现

全部在 worktree 的 `probe/`（`.gitignore` 覆盖，仅本机）：

```
probe/l3/extract.py            # 从真文件里逐字抽出 companion 体 → JVM 可编译切片
probe/l3/fix/src/*.kt          # 切片（ContactsPure.kt 由 extract.py 生成）+ OffloadArgs 原文 + 2 桩
probe/l3/fix/test/PageSizePostTest.kt
probe/l3/base/src/…            # 基线切片（eea9f407）
probe/l3/base/test/PageSizePreTest.kt
probe/l3/mutate.py             # 5 条变异（带 applied 断言，改不上就报错而不是假绿）
probe/l3/arm.sh                # 一次一条臂（PRoot 下不循环跑子进程）
probe/l3/run.sh                # POST/PRE 臂
probe/compile/gen.py           # 真文件 + 12 个 Android 桩 → 编译并运行真实单测
```

复现：
```sh
cd <worktree>
python3 probe/l3/extract.py . probe/l3/fix/src ContactsPure
probe/l3/run.sh fix                       # OK (9 tests)
python3 scripts/scan/offload_cli_args_check.py .          # 0 命中
python3 scripts/scan/offload_cli_args_check.py --selftest  # SELFTEST=PASS
python3 probe/compile/gen.py . probe/compile/src
kotlinc -nowarn -cp junit:hamcrest:json -d out probe/compile/src/*.kt \
        src/android/app/src/test/java/com/rikkaminis/app/sandbox/offload/OffloadCliArgsTest.kt
java -cp out:junit:hamcrest:json:kotlin-stdlib org.junit.runner.JUnitCore \
        com.rikkaminis.app.sandbox.offload.OffloadCliArgsTest   # OK (57 tests)
```

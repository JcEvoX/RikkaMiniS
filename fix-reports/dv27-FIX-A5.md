# FIX-A5 — 5 个 offload handler 的 `--help` 恒返回退出码 2（A-07，P1）

支 `fix/dv27-a5-offload-help-exit`　基线 main `88ef9b27`　波次 deep-audit-1047
工单 A-07　独占门 `scripts/scan/offload_cli_args_check.py`（0 新建门）

---

## ① 缺陷与真树取证

### 1.1 五处同构站点（真树逐字核对，非照抄报告）

```
src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/
  ModelUseOffloadHandler.kt:45-47
  AlarmOffloadHandler.kt:46-48
  PhotosOffloadHandler.kt:137-139
  SpeechOffloadHandler.kt:68-70
  AccessibilityOffloadHandler.kt:108-110      // 本文件用 TOP_HELP，未改名
```

五处形态完全一致（`grep -rn "positional.isEmpty()) 2 else 0"` 精确 5 命中）：

```kotlin
if (args.hasFlag("h", "help") || args.positional.isEmpty()) {
    return NativeOffloadResult(if (args.positional.isEmpty()) 2 else 0, HELP)
}
```

### 1.2 根因：`--help` 是 flag，`positional` 对它也为空

`OffloadArgs`（`OffloadArgs.kt:49`）把 `booleanFlags + OffloadOutput.OUTPUT_FLAGS` 合并后解析，
而 `OffloadOutput.OUTPUT_FLAGS = setOf("compact", "quiet", "q", "help", "h")`（`OffloadOutput.kt:54`）。
⇒ `tool --help` 的 `flags = {help}`、**`positional = []`**，与裸跑 `tool` 的可观测状态完全相同。

于是上面那行的判定被拆成两半互相矛盾：
- 守卫（guard）把「有 help **或** positional 空」并成一个条件；
- 退出码却只在 **positional 空**上分叉 ⇒ help 场景落进 `2` 分支，
  而 `else 0` 需要「positional 非空 **且** 守卫为真」= 只有 `tool --help sub` 才成立。

实测语义（JVM 装置 `probe/l3`，真 `OffloadArgs` + 真 `OUTPUT_FLAGS` 字面量）：
`tool --help` ⇒ 2、`tool --help list` ⇒ 0、裸 `tool` ⇒ 2。同一条命令族的两种 help 调用给出不同退出码。

### 1.3 后果

`set -e` 或 `cmd --help && …` 的探测链在 `--help` 处直接中断（退出码 2 = 用法错误）；
agent 侧把「查帮助」读成失败，会转向猜测参数或放弃工具，而帮助文本其实**已经打印出来了**。

### 1.4 同族对照（证明这是少数派写法，不是口径分歧）

- 15 个 handler 写成 `if (args.hasFlag("h", "help")) return NativeOffloadResult(0, HELP)`
  （Device / Fastio / Open / Speak / Weather / Calendar / Clipboard / Config / Location /
  Notification / Player / Sessions …）；
- `DebugOffloadHandler.kt:58-60` 用**令牌计数**做分隔符：
  `if (request.argv.size <= 1 || args.hasFlag("h","help") && args.positional.isEmpty())`
  + `if (request.argv.size <= 1) 2 else 0` —— 正确，因为 `argv.size <= 1` 与 help 不相容；
- `ContactsOffloadHandler.kt:53` / `BrowserUseOffloadHandler.kt:71` 也是令牌计数分隔符，
  且**裸调刻意退 0**（打印 help 即成功），属既有契约，本单半径不动。

### 1.5 为什么五个文件里只有这五个错

不是遗漏了「顺手改」——这五个的守卫恰好把 help 与「positional 空」并列，
而退出码又锚在后者；其余 handler 要么只看 help 标志，要么用 `argv.size` 分隔。

---

## ② 修法（改动阶梯）

**停在第 6 级（能一行），因为**：五处退出码表达式完全同构，每处只需把「判定 positional」
换成「判定 help 标志」，`if (args.hasFlag("h", "help")) 0 else 2` 一行改完；
`HELP` / `TOP_HELP` 文本、子命令分支、`EXIT_*` 常量族一字未动。

被否决的两级：
- 第 7 级「抽公共 helper」（如 `helpExitCode(hasHelp)`）：会新增 API 面而换不到可测性——
  可测性已经由门的派生判据（H1/H2，全族扫描）+ JVM 切片装置（真 `OffloadArgs` + 真 handler 前言）提供，
  5 行重复比新 API 便宜（同任务书 §2 的判断，此处复述结论并给出验证依据）。
- 改 `OffloadArgs` 的 flag 语义（让 `--help` 进 positional）：全局解析器，影响 21 个 handler
  与所有 `hasFlag("help")` 站点，代价与收益完全不成比例。

**必须保住的契约（逐条验证见 §5）**：
- 裸调 `<tool>` 无参数仍 **exit 2**（`OffloadCliArgsTest` 既有契约 + 本单 H2 钉住）；
- `--help` / `-h` / `--help sub` 均 **exit 0** 且帮助文本不变；
- `AccessibilityOffloadHandler` 的 `TOP_HELP` 常量名不改。

修后形态（五处一致）：

```kotlin
        // `--help` is a flag, so `positional` is empty for it too: the exit
        // code has to be decided on the flag, or `--help` answers 2.
        if (args.hasFlag("h", "help") || args.positional.isEmpty()) {
            return NativeOffloadResult(if (args.hasFlag("h", "help")) 0 else 2, HELP)
        }
```

---

## ③ 改动清单

| 类 | 文件 | 行数 |
|---|---|---|
| 生产 | `sandbox/offload/{ModelUse,Alarm,Photos,Speech,Accessibility}OffloadHandler.kt` | +15 / −5 |
| 测试 | `src/test/.../offload/OffloadCliArgsTest.kt` | +78 |
| 门 | `scripts/scan/offload_cli_args_check.py` | +406 / −2 |
| 报告 | `fix-reports/dv27-FIX-A5.md` | 本文件 |

合计 **7 文件 +499 / −7**（不含报告）。门 **111 / 夹具 340 不变**（0 新建门，
`scan.sh` / `test_scan.py` 冻结面 0 触碰）。

提交：
1. `5ec73d8f` fix：五处退出码（生产）
2. `77248c41` test：三条 test（根因行为 + 源码形状 + 负控）
3. `dba13f46` gate：H1/H2/H3 判据 + 9 臂 selftest

---

## ④ 门与判据（扩 `offload_cli_args_check.py`，0 新建门）

判据轴 = **从决策点派生，不锚拼写/行号/文件清单**：扫描 `sandbox/offload/*.kt` 与
`com/rikkaminis/app/offload/*.kt` 中**语句位置**的每个 `if (...)`，条件是 help 探针
（`hasFlag(...)` 的参数里含 `"help"`）的，取其**被守卫的提前返回**的退出码表达式来判。

### H1 —— help 必须能到达 0

退出码表达式必须在「有 help 标志」时求值为 0：
- 字面 `0`，或 `const val … = 0` 的名字（`EXIT_OK`，从全族源码派生常量表）→ 通过；
- `if (C) A else B` 且 **C 探 help 标志** ⇒ A（help 侧）必须为 0（`!hasFlag(...)` 自动翻转）；
- `if (C) A else B` 且 C **不**探 help ⇒ C 必须是**令牌计数分隔符**
  （`argv.size` / `rawArgs.isEmpty` …）。**positional 空不是分隔符**——`--help` 是 flag，
  它的 positional 同样为空 ⇒ 红，报错文案直接写根因；
- 其它（变量、函数调用、无 else 的条件）⇒ 红（fail-closed：判不了的退出码不构成「help 能用」的证据）。

### H2 —— 裸调契约仍是 2

当退出码随条件变化、且守卫**还能在没有 help 的情况下被进入**（守卫里除 help 探针外仍有 `||` 分支）时，
服务裸调的那一侧必须是 2。守卫合并但退出码为字面 0 的（Contacts / BrowserUse，
分隔符是令牌计数、help 侧仍可达）不在本族，不判。

### H3 —— 派生本身 fail-closed

- 全族派生到的站点数 < 15 ⇒ 红（树里现为 21 站点；防止改名/抽函数把规则悄悄收窄成空集）；
- 某个**探了 help 的文件**里找不到可判的提前返回 ⇒ 红。

### 实现要点（两处易错的派生细节）

- **必须只认语句位置的 `if`**：`NativeOffloadResult(if (help) 0 else 2, HELP)` 里那个 `if`
  也是 `if (`，朴素 `\bif\s*\(` 扫描会把它当成守卫、把表达式尾巴当成「守卫块」——
  于是**修复本身会被判成缺陷**（本轮实测：2 个假红 + 1 个「无退出码站点」）。
  改为括号栈扫描，只接受栈顶为 `{` 或空的 `if`；字符串字面量整体跳过。
- **注释先剥**（`code_only`）：KDoc 里引用旧形态不得变红，注释也不得满足规则。

### 带的牙（selftest 9 臂，期望集手写自规则表，不从运行结果推导）

| 臂 | 期望 |
|---|---|
| 四种可接受写法同处一树（条件式 / 字面 0 / `const val` / 令牌分隔符） | 无 finding |
| 退出码改回 positional 空 | `{H1}` |
| help 侧字面改为 2 | `{H1}` |
| 裸调侧改 0 | `{H2}` |
| 裸调侧改 1 | `{H2}` |
| `const val EXIT_OK` 改成 2 | `{H1}` |
| 退出码换成不可判的表达式 | `{H1}` |
| help 守卫块里没有提前返回 | `{H3}` |
| 站点数低于地板（用默认 `min_sites` 跑小夹具） | `{H3}` |

绿夹具 `GREEN_HELP_SHAPES` 同时充当**精度臂**：`DebugOffloadHandler` 的令牌计数写法、
`Contacts`/`BrowserUse` 的「合并守卫 + 字面 0」写法在夹具里各占一个 class，全部必须绿。

---

## ⑤ 验证四层

### L1 门层

| 项 | 结果 |
|---|---|
| 本门真树 | `offload_cli_args_check: PASS`（rc=0），PASS 横幅已加上 H1/H2/H3 一族 |
| `--selftest` | `OFFLOAD_CLI_ARGS_SELFTEST=PASS`（含新增 9 臂，共 9+既有臂全绿） |
| PRE 臂 | `probe/pre/` = 现树 + 5 个 handler 换成 `git show 88ef9b27:` 的版本 ⇒ rc=1，**恰 5 条 H1**，一条一个文件 |
| 全量门 | `python3 scripts/scan/scan.sh` ⇒ **Summary: 111 passed, 0 failed** |
| 夹具 | `python3 scripts/scan/test_scan.py` ⇒ **ALL 340 CASES PASS** |

真树变异（造违规→红→按备份还原，源文件 sha256 前后一致）：

| 臂 | 操作 | 结果 |
|---|---|---|
| MUT-1 | 只把 `AlarmOffloadHandler.kt` 回退成基线 | **恰 1 条** `H1: AlarmOffloadHandler.kt: exit code decided on positional emptiness` |
| MUT-2 | 只把 `PhotosOffloadHandler.kt` 的裸调侧 `2`→`0` | **恰 1 条** `H2: PhotosOffloadHandler.kt: a bare invocation must still exit 2 → 0` |

### L2 邻门层（任务书点名的 8 道，真树全部存在）

```
offload_host_path_check                rc=0   22 handler + 2 dispatcher, 0 violations
offload_relative_path_unify_check      rc=0
offload_media_b64_wire_check           rc=0   OK (2 media encode sites NO_WRAP)
modeluse_proto_single_source_check     rc=0   clean
a11y_handler_contract_check            rc=0
schedule_date_source_check             rc=0   OK
calendar_events_nullable_predicate_check rc=0（只吃 `--root <dir>`，位置参数 rc=2 → 按 1046 波同形已知）
readonly_mount_write_guard             rc=0
```

### L3 装置层（真 kotlinc 2.0.21 + JUnit 4.13.2）

装置 `probe/l3/`：把五个 handler **真实的 `handle()` 前言**（从 `override fun handle(...)` 到
help/裸调提前返回块的结束）按语句位置切出来，接**真实的 `OffloadArgs.kt`** +
一个只含 `OUTPUT_FLAGS` 的 `OffloadOutput` 桩（集合字面量**从真文件正则取出**，不是手写），
编成 20 条 JUnit 用例（每工具 4 条：`--help` / `-h` / 裸调 / `list` 非提前返回）。

| 臂 | 用例数 | 失败数 | 失败的恰好是 | 判定 |
|---|---|---|---|---|
| POST（现树切片） | 20 | 0 | — | MATCH |
| PRE（`88ef9b27` 切片） | 20 | **10** | 五个工具的 `--help` 与 `-h` | MATCH |
| m1（Alarm help 侧 → 2） | 20 | 2 | `android-alarm --help` / `-h` | MATCH |
| m2（Photos 裸调侧 → 0） | 20 | 1 | `android-photos bare invocation exits 2` | MATCH |
| m3（Speech 守卫去掉 positional 半） | 20 | 1 | `android-speech bare invocation exits 2`（该调用不再是提前返回） | MATCH |

`ALL L3 ARMS MATCH`。

**另加一条「in-repo 测试可编译」臂**（`probe/l3/behaviour.py`）：把
`OffloadCliArgsTest` 新增的三条 test + `helpExitDecidesOnFlag` + `codeOnly/source/sourceFile`
**逐字**抽出，配真 `OffloadArgs` 独立编译运行 ⇒ **compile: clean (0 warnings)** / `OK (3 tests)`。
这样 CI 不是第一次看见这段测试代码（1046-F9 的教训：源码形状测试本可在本地跑）。

### L4 CI 层

见 §⑦。

---

## ⑥ 残留与负决策

| # | 残留 | 触发条件 |
|---|---|---|
| R1 | **真机行为级欠账**：五工具 `android-{model-use,alarm,photos,speech,a11y} --help; echo $?` 应全为 0，且裸跑仍为 2 | 下次真机窗口一次开齐 |
| R2 | `android-shizuku-cli` 的 `service --help` 仍会实弹执行（backlog 已登记，**不属本单半径**，此处仅交叉引用） | 用户报 `android-shizuku-cli service --help` 有副作用时 |
| R3 | `DebugOffloadHandler` 保留两段式（合并守卫 + 第二次 `if (hasFlag) return 0`）：正确但比单条件复杂，本单半径不动 | 若「统一 21 个 handler 的 help 形态」成为独立工单 |
| R4 | `Contacts` / `BrowserUse` 的「裸调也退 0」是刻意契约（令牌计数分隔符，help 侧仍可达），H2 不判它们 | 若将来产品决定「所有 android-* 裸调统一 exit 2」，需另开单并同时改这两处 |
| R5 | 门 H3 的 15 站点地板：handler 大批合并/改名会让门红（fail-closed 设计，不是误报） | 真做该目录重构时，按新形态重 derive 判据，**不要删规则** |
| R6 | 门只覆盖 `hasFlag(... "help" ...)` 形态的 help 探针；若某 handler 改用别的读法（如自建 `wantsHelp` 属性） | 出现非 `hasFlag` 的 help 探针时扩派生怕（按同一决策点判，不看拼写） |

负决策（本轮刻意不做，都有理由）：
- 不抽 `helpExitCode()` 生产 helper —— 见 §②；
- 不改 `OffloadArgs` 的 flag 语义 —— 全局解析器，21 个 handler 受影响；
- 不动 `EXIT_OK` 常量族与 `HELP` 文本 —— 与本缺陷无关，动了只是把半径撑大。

---

## ⑦ 身份链

| 项 | 值 |
|---|---|
| 基线 | `88ef9b2774cf14a0c983d5cb138db0e052ea7a83`（父提交 `42af0ae6`，与 COMMON §0 一致） |
| 提交 1（生产） | `5ec73d8f` |
| 提交 2（测试） | `77248c41` |
| 提交 3（门） | `dba13f46` |
| 分支 | `fix/dv27-a5-offload-help-exit` |
| 本地 HEAD | `dba13f46c1cbc59e5926febc9c177b81f88e5be2` |
| 远端 | 与本地逐字符一致（`git ls-remote`） |
| CI run / job | 见文末「CI 结论」块 |
| 源码树等价声明 | 报告提交只动 `fix-reports/`，用 `git diff --name-only <绿sha>..HEAD -- . ':(exclude)fix-reports'` 输出为空声明等价 |

---

## ⑧ 自证成本与不确定项

### 8.1 本轮自伤（全部当场闭环，形态可复用）

1. **朴素 `\bif\s*\(` 把「修复表达式里的 `if`」当成守卫** —— 于是修好的树报了 2 条 `--help exits 2`
   和 1 条「help 守卫没有退出码站点」。这是最有价值的一次自伤：判据的第一步（找到守卫）
   锚的是拼写而不是位置。修法 = 括号栈扫描 + 只认语句位置，并把它写成 KDoc 里的一句话。
2. **切片收尾索引错位**：`slice = code[start:cl + 1 + len(region)]` 少了 `)` 与 `{` 之间的空白 ⇒
   切片缺 `}`，kotlinc 报一串「unresolved reference」。修法 = 先跳过空白再按 `len(region)` 截。
3. **`code_only` 的索引用在原始 src 上**：剥注释改变了长度，`src.index(...)` 的偏移量拿去切
   `code` ⇒ 「no help/bare early exit found」。修法 = 一律在剥注释后的文本上取索引。
4. **`const val` 名字没解析**：`ConfigOffloadHandler` 的 `EXIT_OK` 被当成「不可判表达式」假红。
   修法 = 从全族源码派生 `const val NAME = <int>` 表，再查表（派生，不写死常量名）。
5. **`guard_rest` 用了内层条件的探针区间去切外层守卫** —— 真树恰好两者文本相同所以无害，
   属潜伏 bug，已改为用外层守卫自己的探针区间。
6. **selftest 的「变异没落地」哨兵与「期望 H3」撞车**：那条臂期望的就是 `{H3}`，于是被误报
   「mutation did not apply」。修法 = 用「变异后文本 == 原文」判落地，不看结果集。
7. 小项：kotlinc 的 `public fun` 暴露 `internal` 返回类型（改 `internal fun`）、
   JUnitCore 需要 FQN 类名、失败用例名带 `(类名)` 后缀需剥离、臂间比较要按集合不按顺序。

### 8.2 不确定项

- **`--help` 的退出码在真机侧没有直接证据**：L3 跑的是真 `OffloadArgs` + 真 handler 前言的切片，
  不是真机进程。raw exit code 由 native 侧 `OffloadSocket` 转发，本单未改该层；
  R1 已把「五工具 `--help; echo $?`」列为真机欠账。
- **门的 15 站点地板**是经验值（现 21）：它只防「派生收窄成空集」，不承诺覆盖率；
  真覆盖率由「每个探 help 的文件都必须有可判站点」这一条兜（与站点数无关的部分）。
- **H2 只对「退出码随条件变化」的站点生效**：这是刻意收窄，避免把 Contacts/BrowserUse 的
  既有「裸调退 0」契约变成红（R4 记录了升级路径）。

---

## CI 结论

（本节在 CI 出结论后回填：run / job / head_sha / 26 步 / 唯一 skipped / Summary / ALL 340 / 编译错 / 警告 / 新用例逐条）

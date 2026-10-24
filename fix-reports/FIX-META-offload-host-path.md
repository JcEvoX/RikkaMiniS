# FIX-META-offload-host-path — `File(表达式实参)` 与「消毒必须发生」

周期：meta-audit-1024 收口波，支 M6（`fix/meta-audit-m6-taint`，worktree `shared/work/meta-audit/wt-m6`）
基线：main `20a9fbadac1b0a78e3013e299c1694eaa2812e2e`（工作树干净）
生产代码：**一行未动**（全部改动在门脚本内）

## 1. 门

`scripts/scan/offload_host_path_check.py`（1874 行 → 2391 行）。多 handler 共用判据：
R1（`*OffloadHandler.kt` 的 raw host `File`）、R2（`Intent.parseUri` 站点）、R3（player 会话路径接线）、
R4（shizuku）、R5（debug 文件）、R6（debug exec 作用域）、R7（photos import 消毒）。门数 **112 不变**
（同门扩展，0 新建门），`scan.sh` 零触碰，`test_scan.py` 零触碰（冻结面，见 §4.5）。

**这改变什么用户行为 / 防什么假绿**：它不改变任何 app 行为——它改变的是「门绿」的含义。改前
「22 handler 全绿」只证明「每个 `File(...)` 的实参长得不像裸路径」；改后还要求
「photos import 这个入口真的把 guest 路径解析成了宿主路径，并且打开的就是解析结果」。
防的假绿 = 一个把任意宿主绝对路径（含 app 私有目录）拷进公开 MediaStore 的 import 路径，
在门全绿的情况下长期存在。

## 2. 轴（失效轴 = spelling，判据锚错了决定点）

R1 的三条红名单全是**一个实参的形状**：

| 判据 | 正则 | 覆盖 |
|---|---|---|
| FILE_DIRECT_RE | `File(…get("path")…)` | 内联直读 |
| FILE_LET_RE | `File(it)` | `?.let {}` |
| FILE_IDENT_RE | `\bFile\(\s*([A-Za-z_]\w*)\s*\)` | 裸标识符 |

而 R1 声称的义务是**数据流**的：到达 `File()` 的值必须经过 guest→host 解析。于是两类形态整条
绕过：**① 表达式实参**（`File(rawPath.trim())` / `File(Paths.get(p).toString())` / `File("" + p)`）；
**②「消毒器在但结果没被用」**（`val resolved = resolvePathForSession(...)` 之后打开别的表达式）。
R1 对 ① 全 miss；对 ② 更是结构性失明——它只在 `File(...)` 处看形状，从不过问「这个入口到底有没有
做过转换」。

**连带发现（同门的输入层盲点，本支顺路修，证据见 §5.3）**：`strip_comments` 是逐行扫描，字符串里的
`/*` 会开一个**块注释**。`PhotosOffloadHandler.handleImport` 的 MIME 查找 `"video/*"` / `"image/*"`
（第 845 行）把其后 **67 行**（values、`contentResolver.insert`、`openOutputStream`、
`FileInputStream(src)` 读点本身）全部抹白——**该门的 R1 一直在审一个缺了半截函数体的 handler**，
任何栽在 845 行之后的 `File(rawPath)` 对它都是不可见的。这不是 R1 判据的错，是喂给它的文本错了。

## 3. 见证摘要（report-A 席位，已机械复证）

```json
{"file":".../offload/PhotosOffloadHandler.kt",
 "find":"        val src = PRootKernel.resolvePathForSession(request.sessionId, guestPath, context)\n            ?: return importPathError(...)",
 "replace":"        val src = File(rawPath.trim())",
 "why_gate_green":"R1 三条正则全按参数形状匹配，表达式实参全 miss；且该文件没有「必须调用 resolvePathForSession」的独立要求",
 "why_defect_back":"rawPath 当宿主 java.io.File 打开 ⇒ agent 传 /data/data/com.rikkaminis.app/... 即可把 app 私有文件拷进公开 MediaStore（R1 宿主读逃逸重现）"}
```

改前复证：`verify_witness.py` → `rc=0 GATE_BLIND_CONFIRMED`（门瞎实锤）；改后 → `rc=1
GATE_CAUGHT_WITNESS, restore=True`，两条违规独立打印（§5.2）。

## 4. 现状判据 → 新判据

### 4.1 R1b：`File(<expression>)`（全局，只扩展实参读取方式）

`_file_call_args(line)` 括号配平取出每个 `File(...)` 的**实参表达式**；若实参内出现 args 污染标识符
（`tainted_from_args` 的 `raw` 态）且实参内**没有** session 级解析器调用 ⇒ 红。
已被三条旧形状覆盖的实参（裸标识符 / `it` / 内联 `get("path")`）**跳过**——同一构造仍只报一条，
旧臂计数（A08 的 4 条、A07 的 6 条）逐条不变。

### 4.2 R7：photos import「消毒必须发生」（半径 = 该文件）

`scan_photos_import_sanitisation(root)`，三判据逐入口：

- **S1**：入口体内必须有 session 级解析器调用（`RESOLVER_CALL_RE`）。消毒必须**发生在本入口**，
  不能只存在于文件别处。
- **S2**：解析器的**返回值必须是被打开的值**——绑定标识符必须出现在宿主打开点
  （`FileInputStream(src)` 等）；解析器在、结果没流到打开点 ⇒ 红。
- **S3（fail-closed）**：入口**派生不出来**（「读 args 路径操作数 ∧ 写 MediaStore」的函数不存在，
  即改名/搬走）或 `PhotosOffloadHandler.kt` 缺失 ⇒ 红，绝不静默跳过。

**作用域派生，不写死函数名**：入口 = 函数体同时满足 `ARG_ASSIGN_RE`（读 args 路径/文件类操作数）
与 `IMPORT_SINK_RE`（`contentResolver.insert(` / `MediaStore.*.Media.`）。真树因此只命中
`handleImport`；第二条 import 入口落地当天自动被覆盖，改名不失明、删 helper 会红。

### 4.3 `_body_opener()`：函数体定位（R7 的必要前置）

`_brace_body()` 无法区分**表达式体函数**（`fun f(x: Int) = ...`）：它会去找下一个 `{`，
从而把其后**全部**函数声明吞进一个 body（`covered` 一次跳到底），枚举整段消失
（实测 `_fun_declarations` 只吐出 2/29 个函数，`handleImport` 不在其中）。
新增 `_body_opener()`：从参数表右括号起扫，深度 0 处先遇 `=` ⇒ 表达式体（无 body），
先遇 `{` ⇒ 花括号体；同时修掉括号深度初值（idx 落在 `(` 之后，应从 1 起）。

### 4.4 `strip_comments`：字面量感知（输入正确性，不改任何判据逻辑）

```
_CODE_TOKEN_RE = """…""" | "…" | '…' | // … | /* … */ | /* …(未终止,到 EOF)
```
先 tokenise 字面量，再剥注释；块注释替换为等量换行 ⇒ **行号不变**。修复依据是 §2 末的 67 行实证。
修完全部 8 组旧臂 + 真树逐条复测，**无任何 verdict 翻转**（旧行为在真树上本来也是绿的——正因为被
抹掉的那半截没人查）。

### 4.5 `main()`：打印扩展判据违规

此前扩展判据（player/shizuku/debug/exec）只贡献计数不打印，门红时只见「N violation(s)」不见内容。
改后逐条 `❌ …` 打印——纯输出层，判据不变。

### 4.6 未触碰

`scan.sh` 零改动；`test_scan.py` 零改动（任务书 §3.2 冻结面——本支一度加了 M6 注册块，按纪律
回滚并由主会话在并集收口时统一注册，见 commit `468c8c51`）。旧 marker
`SELFTEST=PASS (7 fixtures: 4 violations caught, 3 clean trees green)` **原样保留**，夹具断言照旧。

## 5. 四层证据

### 5.1 L1 本门 + 全量（最终树）

| 项 | 结果 |
|---|---|
| `--selftest` | 全绿：旧 8 组臂原样 + **M6 9 臂**（2 clean + 7 dirty/fail-closed） |
| M6 marker | `M6_TAINT_SELFTEST=PASS (9 fixtures: 7 dirty/fail-closed arms red on their own criterion, 2 clean trees green)` |
| 真树 | `offload_host_path_check . ` → rc=0，22 handler；`handleImport` 入口在册，打开点 `FileInputStream(src)`，29 个函数体派生成功 |
| 全量门 | `sh scripts/scan/scan.sh` → **Summary: 112 passed, 0 failed**（scan_rc=0） |
| 夹具 | `python3 scripts/scan/test_scan.py` → **ALL 344 CASES PASS**（冻结面未动，仍是 344） |

M6 九臂（期望计数是**手写字面量**，不从被测常量推导）：

| 臂 | 期望 | 钉住 |
|---|---|---|
| dirty `File(rawPath.trim())` | 1 | R1b 表达式实参（见证形态） |
| dirty `File(Paths.get(rawPath).toString())` | 1 | R1b：值被重建仍算未消毒 |
| dirty `"video/*"` 字面量之后 | 1 | 注释剥离不得抹掉后续代码 |
| clean `File(<resolved>.absolutePath)` | 0 | 解析后的表达式**必须**放行（防误红） |
| clean photos import 正常形态 | 0 | S1+S2 同时满足 |
| dirty 见证（消毒被换成 `File(rawPath.trim())`） | 1 | S1「消毒必须发生」 |
| dirty 解析器在但结果没被打开 | 1 | S2「结果必须是被打开的值」 |
| fail-closed 入口不可派生 | 1 | S3 |
| fail-closed 文件缺失 | 1 | S3 |

### 5.2 L2 机械复证（必过）

```
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/report-A.md --repo /var/minis/shared/work/meta-audit/wt-m6
```
`offload_host_path_check.py → rc=1 restore=True → GATE_CAUGHT_WITNESS`，两条违规**同时**打印
（双轴独立命中，同一次见证不靠单点）：

```
❌ .../PhotosOffloadHandler.kt:815: `File(rawPath.trim())` derives from the guest path argument (rawPath)
   without passing through a guest→host resolver — no raw host File
❌ .../PhotosOffloadHandler.kt: `handleImport` never resolves the guest path through a session-scoped
   guest→host resolver — the file that gets imported is a raw host path
```

其余 5 门输出照旧（门瞎实锤），未受本支影响。结果快照：`probe/m6-witness-results.{json,md}`。

### 5.3 L3 变异轮（绿 ≠ 有牙）

`probe/m6_mutations.py`：把门复制到 `probe/mut/`，逐条摘除一项新能力后跑 `--selftest`，
断言**只有钉它的臂翻红**（`C=` 字节级编辑，源文件全程不动）：

| 变异 | 翻红的臂 | 结果 |
|---|---|---|
| m1 删 R1b（表达式实参） | 3 条（`.trim()` / `Paths.get` / 字面量后） | CAUGHT |
| m2 删 S1（消毒必须发生） | 见证臂（且只有它） | CAUGHT |
| m3 删 S2（结果必须被打开） | 结果臂 | CAUGHT |
| m4 删整个 R7 | 4 条（见证/结果/不可派生/文件缺失） | CAUGHT |
| m5 撤字面量 tokenise | 字面量臂 | CAUGHT |

`M6_MUTATIONS=PASS (5/5 mutations caught by exactly their own arms)`，0 条意外红、0 条该红不红。

### 5.4 L4 CI

见 §6。判读口径（不只信 conclusion）：`head_sha` 与本地 HEAD 逐字符一致 · 26 步
`not_ok=[]`、唯一 `skipped` = 步骤 23 `Publish to Releases`（main 门控）· `Summary: 112 passed,
0 failed` **恰一条** · `ALL 344 CASES PASS` · 门头 `[1/112]…[112/112]` 112 个互异 ·
`Z e: ` 0 · 含 `FAILED` 的行逐条确认全是 **PASSED** 用例名子串 · `M6_TAINT_SELFTEST=PASS` 在册。

## 6. CI run

- run **#2622** / id `40103566987` / job `120900186310`，attempt 1，**success**，event=`workflow_dispatch`
- head_sha `__HEAD__`（本地 HEAD 逐字符一致）
- 日志 1,478,xxx B；门头 112 互异；`Summary: 112 passed, 0 failed` 恰一条；`ALL 344 CASES PASS`；`Z e:` 0
- 前一轮（含 test_scan 改动）run #2621 / `78528834` 亦 success，已按冻结面回滚，最终树不含该改动

## 7. 残留登记（供主会话统一登记 backlog / credibility，本支未写）

| # | 残留 | 触发条件 | 触发后方案 | 搁置理由 |
|---|---|---|---|---|
| R1 | 污点分析**单跳**：`args.get("path")` → 局部变量 → 表达式已覆盖；跨函数（存字段再读出）不追踪 | 出现「字段里存 guest 路径再打开」的缺陷或新 witness | 扩污点闭包到字段赋值站点 | 真树 0 处该形态；扩闭包要引入跨函数数据流推断，判据面积远大于现状风险（修复门 P2） |
| R2 | R1b **行级**扫描：跨行 `File(\n rawPath\n)` 不可见 | 首次出现跨行打开点 | 改用文件内已有的 `_statement_window` 做语句级取参 | 当前 Kotlin 风格全是单行构造；升级要先处理语句边界回归 |
| R3 | R7 的「结果被使用」判定为**行级** + 打开点 token 集合：多行调用中 ident 与打开点分行会假红 | 首次 CI 红含 R7 且复核为假红 | 同 R2 改语句级 | 真树打开点是单行 `FileInputStream(src).use {`，假红未出现前不预设复杂度 |
| R4 | R7 半径 = `PhotosOffloadHandler.kt`：speech/share/player 等 handler 未强制「消毒必须发生」 | 任一 handler 出现「resolver 在别处、本入口直接打开文件」的 witness | 把入口派生泛化到该文件（派生规则已与文件名解耦，只差作用域清单） | 半径纪律：其他 handler 已有各自接线判据，无实证盲点前不铺开 |
| R5 | `strip_comments` 不识别 `"""` 内的嵌套引号边界（Kotlin 语法上不成立，真树 0 处）；未终止块注释按「到 EOF」处理与旧行为一致 | 首次因剥离导致判据红/假绿 | 补 `"""` 语料 + 一条 selftest 臂 | 现有 tokenise 已覆盖 `"…"` / `'…'` / 三引号字面量 |
| R6 | 门臂期望计数是与新判据绑定的**手写字面量** | 下次修改 R1/R7 判据形态 | 同步改臂期望 + 重跑 `probe/m6_mutations.py` | 字面量正是「自指陷阱」防线的要求（期望不从未测常量推导），不能自动同步 |
| R7 | `scan_photos_import_sanitisation` 只在 `main()` 调用（不在 `check_tree`）：全部旧 selftest 夹具树里没有 photos 文件，若放进 `check_tree` 会一次性打红 40+ 旧臂 | 若未来有工具绕过 `main()` 直接调 `check_tree` 验证 | 把该判据搬进 `check_tree` 并给夹具补 photos 文件（或用「文件缺失 ⇒ skip」+ 单独 fail-closed 臂） | CI 走 `scan.sh` → `main()`，现有调用面已覆盖；改造会触碰全部旧夹具 |
| R8 | 可信度台账（`credibility.md`）由主会话更新：本门从「C（无外部见证）」升到「**B+ / 已被外部见证证伪且见证转正为 selftest 臂**」的依据 = §5.2 + §5.3 | 主会话收口时 | 按四层证据升级 | 本支不写共享台账 |

## 8. 复现命令

```bash
cd /var/minis/shared/work/meta-audit/wt-m6

# L1 本门
python3 scripts/scan/offload_host_path_check.py .                 # 期望 rc=0
python3 scripts/scan/offload_host_path_check.py . --selftest       # 期望 M6_TAINT_SELFTEST=PASS

# L1 全量
sh scripts/scan/scan.sh                                            # 期望 Summary: 112 passed, 0 failed
python3 scripts/scan/test_scan.py                                  # 期望 ALL 344 CASES PASS

# L2 见证复证
python3 /var/minis/skills/meta-audit-witness/scripts/verify_witness.py \
  /var/minis/shared/work/meta-audit/reports/report-A.md --repo "$PWD"   # 期望 GATE_CAUGHT_WITNESS

# L3 变异轮
python3 probe/m6_mutations.py                                      # 期望 M6_MUTATIONS=PASS (5/5)

# L4 CI
GITHUB_TOKEN=… python3 probe/judge_ci.py                           # head_sha / steps / Summary / marker
```

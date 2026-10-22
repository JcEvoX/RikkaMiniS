# FIX-A2 — §1042-A-02（P1）`FastioTar.tarCreate` 顶层根循环三缺口

**分支**：`fix/dv22-a2-fastio-tar-toplevel` · **基线**：main `5dd278dae155c1e15f5171386479151603da3d91`
**提交**：`d4aa69cf`（生产） → `c573481f`（测试） → `9535d2fc`（门）· HEAD `9535d2fc663a5860d994bd3b5adb90d440835c14`
**文件**：3 个 · **+430/−1**（生产 1 / 测试 1 新增 / 门 1 扩展）· 门 109 / 夹具 336 **不变**（0 新建门，0 改 `scan.sh`/`test_scan.py`）

---

## 1 缺陷与触发路径

### 真实调用链

`minis-fastio tar -c` → `FastioOffloadHandler.handleTar` → `tarCreateInto`（`FastioOffloadHandler.kt:751`）→
`tarCreate(sink, members, tarEntryBudget(args), outcome)`（`FastioTar.kt:81-115`）→ 顶层 `for ((path, memberPrefix) in roots)` 循环。

`members` 由用户命令行给出，**可以不是目录**（`tar -cf a.tar f1 f2` 的普通文件、符号链接、FIFO 都在 `roots` 里）。

### 三个缺口（真树复核，基线 `5dd278da`）

| # | 缺口 | 真树位置 | 后果 |
|---|---|---|---|
| ① | 顶层**没有预算检查** | `FastioTar.kt:92-107`（`writeLink` / `writeRegular` 两个分支） | 顶层根的 `writeRegular`/`writeLink` 内部各 `out.visited++`，**无人比较 `maxEntries`** ⇒ 条目数越过预算，且 `out.truncated` 永远是 false ⇒ handler 报「完整归档」 |
| ② | `writeTree` 的**预算终止布尔被丢弃** | `FastioTar.kt:104`（`writeTree(...)` 裸调用） | `writeTree` 的 `false` 语义是「预算耗尽，调用方必须停止整趟遍历」（其 KDoc `:145-147` 明写），基线丢弃它 ⇒ 耗尽预算的树之后，**后续根继续被写进归档** |
| ③ | 顶层**非目录项无条件 `writeRegular`** | `FastioTar.kt:102-104` | `writeRegular` → `Files.newInputStream(file)`（`:225`）对 **FIFO 永久阻塞**（无写端 ⇒ `open()` 永挂）⇒ worker 线程死掉，本请求再无响应 |

### 与 `writeTree` 内部的对照（为什么只有顶层漏了）

`writeTree` 三个子分支都有 `if (charge(out, maxEntries)) return false`（`:173` / `:179` / `:193`），且子项非普通文件时走 `out.skippedSpecial++`（`:198`）——**树内**的三个缺口都不存在。顶层循环是唯一绕过预算与特例处理的一段。

`charge(` 全文 5 处（`FastioTar.kt:173/179/193` + `FastioOffloadHandler.kt:428/470`），`tarCreate` 体内 **0 处**（与任务书一致）。

### 触发面

`tarEntryBudget(args)` 是从用户参数派生的条目上限（`--max-entries`）。触发需要**根列表里含非目录项**（或目录树把预算耗尽后还有后续根）——即 `tar -cf out.tar FILE1 DIR ...` 这类命令。基线默认 `DEFAULT_MAX_ENTRIES = 2,000,000`，所以「①越预算」在默认参数下难遇，但**用户显式传小预算即可当场复现**；③的 FIFO 挂死与预算无关，**任何预算下都能复现**。

---

## 2 修法（阶梯第 7 级：最小实现）

七级阶梯：①真要做吗（要做，③是 worker 线程死锁）②代码库里已有（`writeTree` 已有全套预算与特例处理，可作范式）③标准库（无）④平台原生（无）⑤已装依赖（无）⑥能一行（不能，三缺口）⑦**最小实现**（停在这里）。

### 改法（`FastioTar.kt:81-127`）

```kotlin
if (out.visited >= maxEntries) {
    out.truncated = true
    break
}
```

放在循环体**最前**（该轮任何写调用之前）。

**只比较、不 charge**——这是本改法最容易写错的一处：`writeRegular`（`:232`）/ `writeLink`（`:276`）内部自带 `out.visited++`，`writeTree` 经 `charge()` 也自带 `visited++`。若在顶层再 `charge()` 一次，同一条目被计 **2 次**，走一半预算就停（`maxEntries=3` 的冻结契约 `FastioTarTest.kt:663-673` 会当场红）。故顶层门只 `if (out.visited >= maxEntries)`。

**②**：改成 `if (!writeTree(sink, path, name, path, maxEntries, out)) break`——`false` 即「预算耗尽，停止整趟遍历」，与 `writeTree` KDoc 的契约一致。

**③**：顶层非目录项先判 `Files.isRegularFile(path, NOFOLLOW_LINKS)`；不是普通文件（**FIFO / socket / 设备**）⇒ `out.skippedSpecial++` + 记名（`MAX_REPORTED_SKIPS` 上限）+ `continue`，**不打开、不读**。与 `writeTree` 子项分支（`:196-199`）同形，与 `tarCreate` 自身 KDoc（`:76-79`）「不是普通文件/目录/符号链接的都要计入 `skippedSpecial`」对齐。

符号链接分支语义不动（照旧 `writeLink`），只多了循环头的预算门覆盖它。

### 不动的锚点

- `FastioTarTest.kt` **一字未动**（`git diff HEAD~3 --stat` 显示该文件 0 改动；冻结断言 `maxEntries=3L → truncated==true && visited==3L` 原样在册）
- `charge(` 5 处现有语义未动
- `tarValidate` / `tarExtract` 未动
- `GuestPathMapper` 不解析末段的地基未动
- `writeRegular` / `writeLink` 内部实现未动

---

## 3 门扩展（`scripts/scan/fastio_tar_bounds_check.py`，本支独占）

新增 `check_toplevel_roots(src)`，**只读 `FastioTar.kt`**（不碰 `FastioOffloadHandler.kt` 的既有判据面），三条决定点判据：

| 判据 | 决定点 | 绑什么 |
|---|---|---|
| (a) 预算门 | `TOPLEVEL_GATE_BLOCK_RE` 命中 `if (… .visited >= maxEntries) {…}` | 块内必须 `truncated = true` 且有 `break`；且该门**位置必须早于**本轮第一个写调用（`writeLink`/`writeRegular`/`writeTree`） |
| (a′) 不得 charge | 正则 `\bcharge\(` 在 `tarCreate` 体内 | 顶层 charge = 双计，必须红 |
| (b) `writeTree` 返回值被消费 | `!\s*writeTree\(` | 裸调用即红 |
| (c) 顶层非目录先判普通文件 | `Files.isRegularFile(` 且位置早于 `writeRegular(` | 缺失即红；另需 `skippedSpecial++` |

**带牙证明（selftest 8 臂，全部只红对应判据）**：

| 夹具 | 形态 | 期望 |
|---|---|---|
| `GOOD_TOPLEVEL` | 合规 | 0 violation |
| `BAD_TOPLEVEL_NO_GATE` | 无预算门 | 红「no entry-budget gate」 |
| `BAD_TOPLEVEL_NO_TRUNCATED` | 门存在但不报截断 | 红「must set out.truncated」 |
| `BAD_TOPLEVEL_CONTINUE` | 门存在但 `continue` 而非 `break` | 红「must break the loop」 |
| `BAD_TOPLEVEL_GATE_AFTER_WRITE` | 门挪到第一个写调用之后 | 红「AFTER the first write」 |
| `BAD_TOPLEVEL_CHARGES` | 顶层 `charge()` | 红「charges the budget itself」 |
| `BAD_TOPLEVEL_IGNORED_RETURN` | 丢弃 `writeTree` 返回值 | 红「discards writeTree's return value」 |
| `BAD_TOPLEVEL_UNCHECKED_SPECIAL` | 无 `isRegularFile` 判定 | 红「without checking Files.isRegularFile」 |

`--selftest` 输出：`selftest compliant top-level -> 0 violation(s)` / `selftest top-level shapes -> 1 / 1 / 1 / 1 / 2 / 1 / 2 / 1 violation(s)` / `FASTIO_TAR_BOUNDS_SELFTEST=PASS`（rc=0）。

既有判据未被打破：`check_write_regular` / `check_create_traversal` / `check_handler` / `check_archive_open_guard` 的夹具**一字未改**，其自检数与改前一致（`compliant writeRegular -> 0`、五形 2/1/1/1/1）。

### 门对基线树的红（反向验证）

把基线 `5dd278da` 的 `FastioTar.kt` 放进 `probe/prefix-tree/`（其余文件取现树）跑本门：

```
VIOLATION tarCreate's root loop has no entry-budget gate — …
VIOLATION tarCreate discards writeTree's return value — …
VIOLATION tarCreate writes a top-level non-directory without checking Files.isRegularFile — …
VIOLATION a top-level special file (FIFO/socket/device) is not counted in skippedSpecial — …
GATE=RED fastio_tar_bounds   (rc=1)
```

4 条判据在基线树上**全部命中**，改动后真树 `GATE=GREEN`（rc=0）。

### 一处判据工程决定（已固化在门里）

首版判据用 `\bcharge\(` 扫 `tarCreate` 源码，被**我自己写进生产代码的注释**（「`writeTree` via `charge()`」）命中，真树当场误红。修法 = 判据读**剥掉注释后的源码**（新增 `strip_kotlin_comments()`，逐字符抹白、保行数）。先例同族（1039 A-04：门判据跑 `strip_comments`，否则会惩罚自己的文档）。

---

## 4 验证四层证据

### L1 门

```
$ sh /var/minis/shared/tools/gate_run.sh --full --repo . --chunks 4 --retries 3
  ... 108 门已跑 (pass=108 fail=0 sandbox=0)
GATE_RUN mode=full repo=/var/minis/shared/work/deep-audit-1042/a2 ran=109 pass=109 fail=0 sandbox=0 envretry=0 verdict=GREEN
  明细: /var/minis/shared/tools/gate-runs/20261022T103822Z/summary.tsv
```

**ran=109 pass=109 fail=0 sandbox=0 envretry=0 verdict=GREEN**。

```
$ python3 scripts/scan/test_scan.py
✅ ALL 336 CASES PASS
```

本门单跑：`python3 scripts/scan/fastio_tar_bounds_check.py --selftest` → `FASTIO_TAR_BOUNDS_SELFTEST=PASS`（rc=0）；
`python3 scripts/scan/fastio_tar_bounds_check.py .` → `GATE=GREEN fastio_tar_bounds`（rc=0）。

### L2 邻门（7/7 rc=0）

```
offload_relative_path_unify_check.py rc=0
offload_cli_args_check.py           rc=0
browser_tabs_lifecycle_check.py      rc=0
offload_host_path_check.py           rc=0
offload_media_b64_wire_check.py      rc=0
offload_permission_reset_sync_check.py rc=0
fastio_tar_bounds_check.py           rc=0
```

### L3 真编译真跑（装置 `probe/`，worktree 私有，已被 `.gitignore:141` 覆盖）

装置形态：`kotlinc`（2.0.21）编译**真 `FastioTar.kt`**（不是切片）+ 一个 26 行的 `BudgetShim.kt`（逐字抄自 `FastioOffloadHandler.kt:1337-1351` 的 `EntryBudget`/`charge`/`WalkTotals`，避免把 handler 的 Android 依赖拖进 JVM）+ 两份测试类（`FastioTarTest` 冻结套件 + 新增 `FastioTarTopLevelTest`），JUnit 4.13.2 跑。

**PRE 臂**（生产文件 = 基线 `5dd278da` 原件）：

```
FastioTarTopLevelTest pre rc=1 :: Tests run: 5,  Failures: 4
FastioTarTest           pre rc=0 :: OK (31 tests)      ← 冻结套件在基线上也是绿的
```

PRE 精确红的 4 条（缺陷真的存在）：

| 测试 | PRE 失败信息 |
|---|---|
| 顶层多根预算不双计 | `AssertionError`（visited 越过 maxEntries，truncated 未置位） |
| 写树耗尽预算停整表 | `AssertionError: expected:<3> but was:<4>`（后续根仍被写） |
| 顶层 FIFO 不被读入 | `AssertionError: tarCreate hung reading a top-level FIFO`（**耗时 30.156 s** = 真的挂死，不是断言假象） |
| 顶层符号链接受预算约束 | `AssertionError` |

**POST 臂**（= 本支生产文件）：

```
FastioTarTopLevelTest post rc=0 :: OK (5 tests)
FastioTarTest           post rc=0 :: OK (31 tests)      ← 冻结套件 31 条全绿，一字未动
```

**变异臂**（`probe/mutations.py`：从 POST 生产文件做**唯一一次**精确字符串替换生成变异体，逐臂编译 + 跑新测试 + 跑冻结套件 + 跑门）：

| 变异 | 新测试 | 冻结套件 | 门 |
|---|---|---|---|
| m1 去掉预算门 | 红 2 条（多根预算 / 符号链接） | OK (31) | 红 1 条「no entry-budget gate」 |
| m2 丢弃 `writeTree` 返回值 | **OK (5)** | OK (31) | 红 1 条「discards writeTree's return value」 |
| m3 顶层非目录不判 `isRegularFile` | 红 1 条（FIFO） | OK (31) | 红 2 条 |
| m4 顶层改成 `charge()`（双计） | 红 5 条 | **红 1 条** | 红 2 条 |
| m5 门存在但不报截断 | 红 2 条 | OK (31) | 红 1 条「must set out.truncated」 |
| m6 特例不计 `skippedSpecial` | 红 1 条（FIFO） | OK (31) | 红 1 条「is not counted in skippedSpecial」 |

- **m4 的冻结套件红**：证明「顶层也 charge」确实破坏出厂语义（冻结契约 `maxEntries=3L → visited==3L`），即双计不是无害写法。
- **m2 的 JUnit 侧全绿，是一个诚实的发现，不是装置漏判**：在**已修**的树里，丢弃 `writeTree` 返回值与预算门**输出等价**——`charge()` 失败时不 `++`（`FastioOffloadHandler.kt:1344-1348`），故失败瞬间 `visited == maxEntries` 恒成立 ⇒ 下一轮的门必触发。承重的是门本身；丢弃返回值只在**无门的基线树**里才是独立缺陷（PRE 臂「写树耗尽预算停整表」精确红已证）。门仍保留该判据：它锁的是「返回值必须被消费」这一契约，防止将来有人删门却留下裸调用。
- 变异还原用**字节快照**（`probe/src/FastioTar.kt` 复制回 POST 原件 + sha256 比对），**不用 `git checkout --`**（会把未提交的生产改动一起回滚）；脚本末尾输出 `probe copy restored, sha match: True`。

### L4 CI

见 §5。

---

## 5 CI 判读

| 项 | 值 |
|---|---|
| run | **`39768460593`**（`https://github.com/logicflow-GYW/RikkaMinis/actions/runs/39768460593`）· 前一轮 `39765536676`（head_sha `9535d2fc`，**代码内容完全相同**，只少一个 `.md` 报告提交） |
| job | `119789714737`（build）· **conclusion = success** · 日志 1,359,620 B（`probe/ci/job-119789714737.log`） |
| dispatch | 手动 dispatch（`gh_sync.sh gh-actions-dispatch --workflow build-apk.yml --ref fix/dv22-a2-fastio-tar-toplevel`），2026-10-22T11:09:27Z；11:09:32 起 → 11:27:08 完 |
| 分支 | `fix/dv22-a2-fastio-tar-toplevel` |
| head_sha | `f8b1590d3d0f96f2a6fe8ade13a311be3e79f28e`（= 分支 HEAD，含报告提交） |
| 三方一致 | 本地 HEAD == `git ls-remote` == CI `head_sha`（逐字符） |

**判读 14/14 GREEN**（装置 `probe/ci/judge_ci.py`，判据只取两源：门真树 OK 行 + `test_scan` 的 `✅ <gate> selftest has teeth` 行）：

```
PASS head_sha 三方一致
PASS steps failed=[] (not_ok=[])
PASS 唯一 skipped = Publish main 门控 (['Publish to Releases'])
PASS Summary: 109 passed, 0 failed (1/9 条 Summary 合规)
PASS 全部 Summary 无非零 failed (0 处)
PASS 门头序号 1..109 连续 (109 个)
PASS 门名互异 109 (109 个互异)
PASS ALL 336 CASES PASS
PASS 编译错 `Z e:` = 0
PASS 游离 FAILED = 0 (0)
PASS 门真树 OK 行在册
PASS test_scan teeth 行在册
PASS 新测试 FastioTarTopLevelTest 用例 PASSED 在册 (5 行)
PASS 冻结套件 FastioTarTest PASSED 在册 (31 行)
```

日志内留痕（逐条）：

```
GATE=GREEN fastio_tar_bounds
  ✅ fastio_tar_bounds_check selftest has teeth (FASTIO_TAR_BOUNDS_SELFTEST=PASS)
  ✅ fastio_tar_bounds_check real tree green

FastioTarTopLevelTest > a top-level file inside the budget is archived PASSED
FastioTarTopLevelTest > a top-level symlink gets the budget gate too PASSED
FastioTarTopLevelTest > a top-level fifo is counted and never read PASSED
FastioTarTopLevelTest > a tree that exhausts the budget stops the whole root list PASSED
FastioTarTopLevelTest > top-level files stop at the budget without double counting PASSED
```

冻结套件 `FastioTarTest` 31 条 PASSED 在册（抽样：`a truncated archive is refused rather than half-extracted PASSED` / `a name too long for the prefix split falls back to a GNU long-name record PASSED` / `a name that fits the 100-byte field is stored verbatim PASSED`）。

**判读装置的一处自伤（已修，记录在案）**：首版判据把 Summary 行写成「以 `Summary:` **行首**开头」、门头写成「以 `[` 开头」，实际日志形态是「`时间戳 ║  Summary: …`」与「`时间戳 ━━━ [N/109] 门名 ━━━`」⇒ 3 条判据假红（`0/0 条 Summary 合规` / `门头 0 个`）。修法 = 按真样本重取形态（正则 `[N/109]` / 含 `Summary:` 子串 + 非零 failed 计数扫描），**不照抄臆想句式**（同族第 N 次，见 1039 A-4）。修后 14/14。

**三源取二**：CI 绿（run `39768460593` success，14/14 判读）+ 本地独立实跑（L1 全量 109 门 GREEN + 336 夹具 + L3 pre/post/变异三臂真编译真跑）——两条独立证据成立。

**停止节点达成（分支 CI 绿）**，**未合并 main**（含 `.kt` 验证面，合并起手权在用户）。

---

## 6 与工单的偏差（按真树口径）

| 工单 | 实际 | 说明 |
|---|---|---|
| `tarCreate` 在 `:70-115` | **`:81-115`**（KDoc `:68-80`） | 行号偏移 11 行，真树为准 |
| `charge(` 5 处 173/179/193/428/470 | **一致**（`FastioTar.kt:173/179/193`，`FastioOffloadHandler.kt:428/470`） | — |
| 计数修正：`skippedSpecial` 实数 6 | **确认 6**（`FastioTar.kt:60` 声明、`:78` KDoc、`:137` KDoc、`:198` 写入、`:389` extract 声明、`:517`/`:541` extract 写入 —— 其中「实数」指**声明+写入点**共 6 处，`:78`/`:137` 是 KDoc 提及） | 与工单一致 |
| 修法 3：顶层非目录「先判 `isRegularFile`」 | 采用工单的 `isRegularFile` 判定，但**额外**加 `skippedSpecial++` 与记名 | 工单原文只说「或 `!isRegularFile` → 计入 `out.skippedSpecial++`」，本实现取后者（含记名，与 `writeTree` 子项分支 `:196-199` 同形） |
| 修法 1：「不额外 +1，避免与内部 `visited++` 双计」 | 采用；门额外把「顶层出现 `charge(`」列为**违规**（不是可接受写法） | 判据的严格化：可接受形状只有一种（只比较） |
| `writeRegular`（`:232`）/`writeLink`（`:276`）内部自带 `out.visited++` | **确认**（`FastioTar.kt:232` / `:276`；`writeRegular` 实际 `out.visited++` 在 `:239`） | 计数语义是**既有**的（writeTree 子文件被 charge 一次 + writeRegular 再 +1，故冻结用例 `maxEntries=3` 下 3 个条目 = 1 目录 + 1 子文件，见 L3「预算语义」注释） |
| 门扩展范围 | 扩 `check_toplevel_roots`，**新增 `strip_kotlin_comments()`** | 工单未提；因判据若不剥注释会被生产代码自己的注释命中（见 §3 末） |

---

## 7 残留 R（每条带触发条件）

| # | 内容 | 触发条件 |
|---|---|---|
| R1 | **真机行为级未验**：`minis-fastio tar -c` 端到端（顶层普通文件 / 符号链接 / FIFO / 预算截断四条路径）在真机上跑一遍，确认 handler 的 `skipped_special` / `truncated` 字段真出现在 JSON 里 | 下次真机窗口（与 §1042-VERIFY 批一起） |
| R2 | `writeTree` 内部对**子项**的 `charge` 与 `writeRegular` 内部 `visited++` 仍**双重计数**（既有语义，冻结契约依赖它，本支未动） | 若将来要改预算语义（让 `maxEntries` 表示「真实条目数」），必须同时改 `FastioTarTest.kt:663-673` 的冻结断言 —— 触发条件：有人提「预算算得不准」 |
| R3 | 顶层 `writeRegular` 分支仍**不单独 charge**：顶层普通文件靠 `writeRegular` 内部的 `visited++` 计数，门只比较 | 若将来 `writeRegular` 改为不计数（重构），顶层普通文件会绕过预算 —— 触发条件：有人改 `writeRegular` 的计数逻辑 |
| R4 | 门判据 (b)「`writeTree` 返回值被消费」在**已修树**里与预算门输出等价（m2 实证），属**契约锁**而非行为锁 | 若将来删掉预算门，该判据即成为唯一防线 —— 触发条件：有人提议删除预算门 |
| R5 | 顶层非目录的「非普通文件」只判 `isRegularFile`（NOFOLLOW），**未**覆盖 `isOther`（socket/设备节点）之外的其它异常类型 | 若宿主出现新的特殊文件类型，行为仍是「计入 skippedSpecial 不读入」，安全 —— 触发条件：出现新的特殊文件类型需要单独报告 |
| R6 | 任务书要求「顶层 FIFO 写入不进 → `skippedSpecial++`」，本实现同时**记名**（`skippedNames`）。`skippedNames` 在 extract 侧有 `MAX_REPORTED_SKIPS=20` 上限，create 侧同限 | 无需动作，记录口径统一 |

---

## 8 影响面（用户可见行为变化）

**有变化的四条**（`minis-fastio tar -c`）：

1. **预算现在覆盖顶层根**：以前顶层根可以越过 `--max-entries`；现在到顶即停，并在返回 JSON 里置 `truncated=true`。⇒ 用户显式传小预算时，归档内容变少但**诚实**（以前是「超预算且不告诉你」）。
2. **顶层根的 `visited` 计数不双计**：顶层普通文件/符号链接每个记 1（与树内子文件口径不同——树内子文件记 2，见 R2；本支未动既有语义）。
3. **顶层 FIFO/socket/设备节点不再挂死 worker**：以前 `tar -cf a.tar myfifo` 会让 worker 线程永久阻塞（本请求无响应）；现在计入 `skipped_special` 并跳过。
4. **写树耗尽预算后，后续根不再被写进归档**（以前会写）。

**无变化的**：

- 目录根的常规用法（`tar -cf a.tar dir`）行为不变——`writeTree` 未动。
- `tar -xf` 侧（`tarValidate`/`tarExtract`）未动。
- 归档格式、成员名规则（`tarMemberPrefix` / `tarMemberName`）未动。
- `FastioTarTest` 31 条冻结用例全绿。

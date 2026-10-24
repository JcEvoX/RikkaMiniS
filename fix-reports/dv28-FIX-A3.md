# dv28-FIX-A3 — 记忆 rollup 候选资格：内容维必须收进单源判定（P1）

支 a3 · 工单 A-03 · 分支 `fix/dv28-a3-rollup-candidacy` · 基线 main `eea9f407f9e0e79f4902eeef1f84e1550ee5c29c`

---

## 1. 缺陷与真树取证

### 1.1 缺陷形态：同一条「候选资格」被写成两条，第二条多一维

`MemoryRollupEngine.isRollupCandidate` 自 dv15-A4 起被声明为**两侧共用的唯一口径**，它的 KDoc 写着「A candidate for both sides — the executor's picker and the prompt-side size hint — so the hint can only ever name a file the executor would not answer with SKIPPED_ALREADY」。它实际只判两维：日期形状 + 是否已有 rollup 段。

执行端在它之外**又加了一维**：

| 位置（基线 `eea9f407`） | 代码 | 判的维 |
|---|---|---|
| `MemoryRollupEngine.kt:91` | `fun isRollupCandidate(fileName, byteSize, todayStr, rollupContent)` | 日期形状 / 非空 / 未蒸馏 |
| `MemoryRollupRunner.kt:136` | 调上面的谓词 | 同上 |
| `MemoryRollupRunner.kt:141` | `entries.none { classify(it) != RollupClass.TRANSIENT }` → `return@mapNotNull null` | **「还有没有值得蒸馏的条目」**（内容维） |
| `MemoryRollupRunner.kt:81` | `entries.filter { classify(it) != TRANSIENT }` → `NOTHING_TO_DISTILL` | 同上（第二处写法） |
| `MemoryRepository.kt:156` | 提示端调上面那个**四参**谓词 | 只有前两维 —— **内容维丢失** |

于是提示端的「最大未蒸馏日报」会把一份**全是在飞待办**的大日志算成候选，而执行端对同一份文件回 `NOTHING_TO_DISTILL`。`ChatPromptAndTools.kt:1489` 用 `rollupBytes >= 50_000L` 做建议门，所以只要那份纯瞬态日志 ≥ 50 KB，**每一轮**系统提示都会建议 `memory_rollup`，每次调用都答「无事可做」。

### 1.2 真树取证（本人跑出，不采信报告行号）

- 基线三文件的行号（`grep -n`，`probe/l3b/pre-src/` 由 `git show eea9f407:<path>` 抽出）：`MemoryRollupRunner.kt:81`、`:141` 是两处瞬态过滤；`MemoryRollupEngine.kt:91` 是谓词定义、`:300` 是 `buildRollupText` 的渲染过滤；`MemoryRepository.kt:138`（`dailyLogSizes` 定义）、`:156`（四参调用）。
- **行为级取证（Android 无关，真类真跑）**：把基线三文件 + 本支新回归测试一起用 `kotlinc 2.0.21` 编译并跑 JUnitCore，新用例 `dailyLogSizes_excludesPureTransientLog` **恰好红一条**，失败信息就是缺陷原话：

  ```
  1) dailyLogSizes_excludesPureTransientLog(com.rikkaminis.app.data.repository.MemoryRepositoryTest)
  java.lang.AssertionError: the hint must name a log the executor would actually roll up
      at MemoryRepositoryTest.dailyLogSizes_excludesPureTransientLog(MemoryRepositoryTest.kt:454)
  Tests run: 25,  Failures: 1
  ```

  同一棵基线上执行端测试 `MemoryRollupRunnerTest` 是 `OK (12 tests)` —— 缺陷**只在提示端**，与报告一致。
- **门层取证**：新判据在基线树上恰好两条违规（一条 C-R1、一条 C-R2）：

  ```
  MemoryRollupRunner.kt:81: … re-implements the transient filter (2 site(s), first at line 81); the shared candidate predicate cannot reach any transient filter (engine filter sites: buildRollupText) — …
  MemoryRepository.kt:138: dailyLogSizes cannot reach the transient filter - the system prompt can still name a log the executor would answer with NOTHING_TO_DISTILL
  ```

### 1.3 为什么老门看不见

老判据 F 只锚「提示端调了共享谓词」+「谓词表达了日期形状 / 今日边界 / 已蒸馏」+「执行端不重写日期正则」。**它问的是「两侧用的是不是同一个函数」，不问「这个函数是否含决策所需的全部维度」** —— 声明与实现不一致，正好落在门的盲区里。

---

## 2. 修法（改动阶梯）

**停在第 2 级（代码库里已有），因为**：候选判定（`isRollupCandidate`）、瞬态分类（`classify`）、可蒸馏过滤（`buildRollupText` 里已有一条）在仓库里全都存在；本次不是新增机制，而是把**已经存在的两条判定并成一条**——把内容维从执行端搬进共享谓词，并把瞬态过滤抽成唯一出口 `distillableEntries`。没有新增依赖、没有新增配置项、没有新建门。

- `MemoryRollupEngine.isRollupCandidate` 增第 5 参 `readContent: () -> String?`（**必填，无默认值** —— 编译期强制两侧都传；给默认值就等于给「只判前两维」留后门）。内容读取放在**便宜维之后**，只对已通过日期 / 已蒸馏两条的日志付 I/O 代价；`null`（读不出）= 非候选（执行端也无法蒸馏读不出的文件，这与执行端原语义一致）。
- 新增 `MemoryRollupEngine.distillableEntries(entries)` = `filter { classify(it) != TRANSIENT }`，成为**唯一**的「可蒸馏」出口：谓词用它判「有没有东西可蒸馏」，`buildRollupText` 也改用它（输出逐字节不变）。此前「瞬态过滤」在引擎里有 2 处写法（`:300` 渲染、`:141` 选择），现在 1 处。
- `MemoryRollupRunner.pickLargestEligibleDate`：删掉本地的抽取 + 分类，改传内容闭包；`runOnce` 用 `distillableEntries` 取稳定条目（`NOTHING_TO_DISTILL` 的 Outcome 契约与日志一行未动）。
- `NOTHING_TO_DISTILL` 的**可达性刻意保留**：自动选路现在保证「被选中的日志至少有 1 条可蒸馏条目」，但显式 `memory_rollup <date>` 绕过选路、以及选路的**兜底分支**（无合格日志时退回最大非空旧日志）仍会进入该分支 —— 语义是「你点名要蒸馏一份纯瞬态日志」⇒ 明确回「无事可做」而不是静默 no-op。
- **底退路（`isPastDailyLog` 兜底）逐字保留**，未收进单源：它只决定「没有合格日志时 rollup 该不该给出一个明确的 NOTHING_TO_DISTILL」，不产生候选，也不会让提示指向纯瞬态日志（提示端没有这条退路，`dailyLogSizes` 过滤后为空 ⇒ `null` ⇒ 不给建议）。

---

## 3. 改动清单

| 文件 | 变化 | 说明 |
|---|---|---|
| `src/.../workspace/MemoryRollupEngine.kt` | +32 / −6 | 谓词增内容维（KDoc 写明维度与理由）、新增 `distillableEntries`、`buildRollupText` 复用 |
| `src/.../workspace/MemoryRollupRunner.kt` | +19 / −8 | picker 传内容闭包、删本地两处瞬态过滤、`NOTHING_TO_DISTILL` 可达性注释 |
| `src/.../data/repository/MemoryRepository.kt` | +11 / −2 | 提示端传内容闭包（KDoc 记两轮的失真史） |
| `src/.../data/repository/MemoryRepositoryTest.kt` | +35 / −0 | 新增 `dailyLogSizes_excludesPureTransientLog`（60 KB 纯瞬态 vs 小可蒸馏） |
| `src/.../workspace/MemoryRollupRunnerTest.kt` | +30 / −1 | 新增 `pureTransientLargerLog_isSkippedForSmallerDistillableOne` |
| `scripts/scan/backup_remap_rollup_check.py` | +273 / −2 | 扩既有门：C-R1 / C-R2 + 3 条带牙臂 |

合计 6 文件 **+392 / −25**。**门 111 / 夹具 342 不变**（0 新建门；`scan.sh` / `test_scan.py` 冻结面零触碰）。

---

## 4. 门与判据（扩 `backup_remap_rollup_check.py`，未新建门）

判据全部**派生，不锚拼写**：

- **过滤点识别**：`FILTER_EXPR = (?:==|!=)\s*[名字链]\.TRANSIENT` —— 只认**比较**。`classify` 自己 `return RollupClass.TRANSIENT` 不算过滤点（否则分类器本身会把自己判成「第二处」）。改分类器名、改枚举成员名、换限定写法都不失明。
- **调用图可达性**：`kotlin_fun_map` 按 `fun <name>(` + 花括号配平切函数体，`fun_closure` 做调用闭包。判据不问「某个名字有没有出现」，只问「这个谓词的闭包能不能到达那个过滤点」。
- **先剥注释再扫**：新增 `strip_kotlin_comments`（行注释 + 块注释，长度不变地抹成空格），所以 KDoc 里写 `RollupClass.TRANSIENT` 不会被当成过滤点。

| 判据 | 内容 | 失败信息（首句） |
|---|---|---|
| **C-R1** | 内容维必须住进共享谓词的闭包里，且**两侧都不得自带一份**瞬态过滤（同一行可写 `// unreachable-defense:` 豁免） | 侧内复制 + 谓词不可达，合并成**一条**违规（附最精确行号） |
| **C-R2** | `dailyLogSizes` 的过滤链必须**到达**含 TRANSIENT 的判定（一跳 helper 内） | 「the system prompt can still name a log the executor would answer with NOTHING_TO_DISTILL」 |

**带牙臂（selftest 29 臂：pristine 绿 + 28 变异）新增 3 条，逐条核过「红的正是该判据」**（`probe/arm_matrix.py` 输出，见 §5 L1）：

| 臂 | 变异 | 期望 | 实测 |
|---|---|---|---|
| M21 | 谓词丢内容维（`entries` 原样返回）+ 执行端自带一份过滤 | 红 C-R1 + C-R2 | **2 条**（C-R1 点 `MemoryRollupRunner.kt:154`、C-R2 点 `MemoryRepository.kt:138`）|
| M22 | 引擎保留过滤，执行端**再塞回一份** | 红 C-R1（唯一性破了），C-R2 仍绿 | **1 条**（C-R1）|
| M23 | 提示端在共享谓词旁**自带一份**过滤 | 红 C-R1，C-R2 仍绿（可达性未变） | **1 条**（C-R1 点 `MemoryRepository.kt:162`）|
| PRE | 基线三文件 | 红 C-R1 + C-R2，各一条 | **2 条**（见 §1.2）|

判据能分辨「维度丢了」与「维度重复」：M21 两种原因合并成一条违规（不制造两条指向同一决策的噪音），M22 只报重复。

---

## 5. 验证四层

### L1 门层

- 本门真树：`backup_remap_rollup_check: OK (…)` `rc=0`；持锁跑 `gate_run.sh --gate … --selftest` → `ran=1 pass=1 fail=0 sandbox=0 envretry=0 verdict=GREEN`。
- `--selftest`：`selftest: 29 arms matched (pristine green + 28 targeted mutations)` + `BACKUP_REMAP_ROLLUP_SELFTEST=PASS`。
- **PRE 臂**：`probe/base/` 基线三文件 → 2 violation（C-R1 / C-R2 各一），`rc=1`。**用完即删**（probe 树里的真源码副本会被按文件名遍历的门误当源码，1047-A3 教训）。
- **全量门**：`sh scripts/scan/scan.sh` 在本壳里两次被 ENOSYS 打断（`claim_release_guard.py:243` 读文件 `Function not implemented`），按 COMMON §5 改**逐门直跑**（`probe/run_gates.sh`，每门带 3 次 ENOSYS 重试）：**110 个门脚本全 PASS / 0 FAIL / 0 SANDBOX**（`probe/logs/gates.tsv`），第 111 项是 `scan.sh:39` 的内联 i18n 检查，单独带 `$ROOT` 执行 → `✅ No orphan keys (1565 refs, 1574 defs)`。合计 **111 / 0**。
- `test_scan.py`（342 夹具）：本壳跑两次均被沙箱在 `subprocess.wait` 处 `OSError: [Errno 38] Function not implemented` 打断（registry S28 原症状，非夹具问题，`probe/logs/test_scan.log` 里停在第 40 项之后、无 FAIL 行）。**证据改由 L4 CI job 日志给出**（CI 同一步跑同一命令）。

### L2 邻门层

按概念找的真实邻门（任务书未点名邻门），逐个持锁跑 `gate_run.sh --gate`：**7/7 rc=0**

`memory_gate_provider_fields_check` · `memory_ui_wiring_check` · `tool_title_error_branch_check` · `readonly_mount_write_guard` · `deeplink_settings_routes_check` · `session_provider_enabled_check` · `outcome_known_chain_check`

（前 4 道是「引用了 MemoryRepository / MemoryRollup / GLOBAL.md」的门，后 3 道是「引用 `ChatPromptAndTools`（提示端消费方）」的门。）

### L3 装置层（两套，都是真跑）

**L3-A：引擎切片（Android 无关，`probe/l3/`）** —— `MemoryRollupEngine.kt` 本身零 import，直接 `kotlinc 2.0.21` 编译 + JUnitCore：

| 臂 | 结果 |
|---|---|
| POST（本支引擎 + 10 用例） | `OK (10 tests)` |
| PRE（基线引擎 + 驱动复刻基线提示端过滤） | 红 1：`pureTransientLog_isNotCandidate_POST_contract`（即缺陷复现） |
| m1 内容维删除（`return text.isNotEmpty()`） | 红 {`pureTransient60k_isNotCandidate`, `candidacyAgreesWithRollupText`} |
| m2 读不出也当候选（`?: return true`） | 红 {`unreadableContent_isNotCandidate`} |
| m3 内容维退化成「有条目就算」（`extractEntries(text).isNotEmpty()`） | 红 {`pureTransient60k_isNotCandidate`, `candidacyAgreesWithRollupText`} |

m1/m3 是同一件事的两种写法（删掉 vs 退化），都**恰好红**在「内容维缺失」的两条断言上，无附带红；m2 只红读不出那一条。另有 `candidacyAgreesWithRollupText`（候选资格 ⟺ 该日志能渲染出非空 rollup）作为单源契约断言，m1/m3 下同时红=契约确实咬住。

**L3-B：真类切片（生产代码三文件 + 真测试，`probe/l3b/`）** —— 只给 `android.util.Log` / `SoulStore` / `AgentRuntimeLimitsPrefs` 打桩（生产 defaults），其余逐字节真身：

| 臂 | 结果 |
|---|---|
| POST `MemoryRepositoryTest` | `OK (25 tests)`（含新回归用例） |
| POST `MemoryRollupRunnerTest` | `OK (12 tests)` |
| PRE（基线三文件 + 同一批测试） `MemoryRepositoryTest` | **红 1**：`dailyLogSizes_excludesPureTransientLog`（§1.2 原文） |
| PRE `MemoryRollupRunnerTest` | `OK (12 tests)` —— 执行端本来就是对的；本支新增的执行端用例是**形状锁**（防将来执行端丢内容维），在 PRE 上也绿，已在 §6/R2 如实标注 |

编译期与运行期 classpath 分开对齐（`-cp` 给 stdlib+junit+hamcrest，`java -cp out:<同三者>`），避免了「编译过、运行 NoClassDefFoundError」的假红（1047-A2 教训）。

### L4 CI 层

见 §7（身份链）。判读项：`run.head_sha` 三方逐字符 / 26 步无 failure（唯一 skipped = `Publish to Releases`）/ `Summary: 111 passed, 0 failed` 恰一条 / `ALL 342 CASES PASS` / 编译错 0 / 游离 FAILED 0 / **本支 6 个文件编译警告 0**。

---

## 6. 残留与失真

**失真（任务书 vs 真树）**

- **S1**：任务书 POST 期望写「`length < 50 KB ⇒ false`」。真树里 **50 KB 阈值不属于候选资格** —— 它是 `ChatPromptAndTools.kt:1489` 的建议门（`rollupBytes >= 50_000L`）。候选资格只判日期 / 已蒸馏 / 内容，**与体积无关**（既有测试 `dailyLogSizes_namesLargestBySize` 用 500 B 与 5 KB 两份日志断言最大的那个）。按真树为准：L3 里用 `smallDistillableLog_isCandidate` 明确钉住「小但可蒸馏 ⇒ 候选」，并在用例注释里点明阈值住在提示构造器。
- **S2**：任务书 C-R2 写「直接或经一跳 helper」。真树上 `dailyLogSizes → isRollupCandidate → distillableEntries` 是**两跳**，判据按「调用闭包（不限跳数、限定在这三个文件内）」实现，比字面更严也更准确（多一跳 helper 不会假绿）。

**残留（各带触发条件）**

- **R1 真机行为级**：在有 60 KB 纯瞬态日报 / 24 篇日志的真实设备上，系统提示不再建议 rollup、`memory_rollup` 不再出现「建议→NOTHING_TO_DISTILL」循环。触发条件：下次真机窗口（本批 §R1 统一开窗）。
- **R2 执行端新用例是形状锁**：`pureTransientLargerLog_isSkippedForSmallerDistillableOne` 在基线树上也绿（执行端本来就有内容维）；它的价值是「将来执行端丢维度时立刻红」。触发条件：若有人质疑它不做行为区分 ⇒ 读本行。
- **R3 提示端 I/O 增量**：`dailyLogSizes` 现在会对**通过便宜两维**的日报读一次全文（当前记忆目录 24 篇 / 2.0 MB ⇒ 每轮 +数 ms 级读；且 `dailyLogSizeSummary()` 与 `largestDailyLogBytes()` 是两个独立调用 ⇒ 一輪提示构造里读两遍）。刻意不做缓存（缓存是新状态层，超出本单半径）。触发条件：若真机出现「发消息明显变慢」且 profiling 指向记忆目录读取 ⇒ 先把两个调用合并成一次（`ChatPromptAndTools.kt:1453-1454`），再谈缓存。
- **R4 读不出 = 非候选**：读失败按「无内容」处理（基线执行端 `?: return@mapNotNull null` 同义）。若将来出现「文件权限异常但仍应被点名」的需求，需另立一维。触发条件：出现「日志明明有内容却从不被建议」的报障。
- **R5 门的作用域是三文件**：判据只在 `MemoryRollupEngine/Runner/Repository` 三个文件内派生闭包。若将来多出第四个消费者（例如新的 UI 页展示「待蒸馏」），门不会自动覆盖它。触发条件：新增任何调用 `isRollupCandidate` 的文件时，把该文件加进门的作用域常量。
- **R6 `unreachable-defense:` 豁免只认同行标记**：跨行注释不认。触发条件：若真出现合法豁免被误红。

---

## 7. 身份链

- 基线：`eea9f407f9e0e79f4902eeef1f84e1550ee5c29c`（`git log --oneline -2` 确认父链）。
- 提交：见回报消息（生产 → 测试 → 门 → 报告；报告提交只含 `fix-reports/*.md`，按 COMMON §3.6 用 `git diff --name-only <绿sha>..HEAD -- . ':(exclude)fix-reports'` 为空声明源码树等价）。
- CI：`run <id>` / `job <id>` / `attempt 1` / `head_sha` 三方逐字符一致（见回报消息）。

---

## 8. 自证成本与不确定项

- 本支三次撞 ENOSYS（`scan.sh` ×2、`test_scan.py` ×2、`gate_run --full` ×2，含一次「MISSING 全部门」的假象 —— 成因是壳内 `cd` 的临时文件创建失败，`--repo` 用绝对路径即可复现/排除）。全部归因为沙箱症状，未写成门红。
- 不确定项：CI 是唯一跑通完整 `test_scan.py` 342 夹具的证据源（本壳跑不完），若 CI 在这一步红，需要按夹具名回本支装置复现（本支只改了 2 个测试文件，未触碰任何门的夹具表）。
- 判据口径自评：C-R1 是「唯一出口 + 两侧共用」，C-R2 是「提示端确实到达内容维」。两条都不写死函数名/枚举名，改名字不失明；但**两者都无法阻止「有人把内容维写成一个新函数并在三处各调一次」** —— 那种形态会被 M22 型臂抓住（唯一性），但若三处都调同一个新函数，门认为合规（这其实也是单源，可接受）。

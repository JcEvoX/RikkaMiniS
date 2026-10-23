# FIX-B3 — 模型组详情屏对系统语音虚拟成员的三处断层（B-05，P1）

- 支：B3　分支：`fix/dv27-b3-model-group-system-voice`
- 基线 main：`88ef9b2774cf14a0c983d5cb138db0e052ea7a83`（父提交 `42af0ae6`，两者 `src/` + `scripts/` 逐字节一致）
- 独占门：`scripts/scan/provider_remember_key_check.py`（0 新建门，门 111 / 夹具 340 不变）

---

## 1. 缺陷与真树取证

### 1.1 数据前提

`Voice Input` / `Voice Output` 两个默认组的成员**全部**是系统语音哨兵 id
（`SystemVoiceIds.BUILTIN_PROVIDER_ID = "__builtin_system_speech__"` +
`/system-asr-online` `/system-asr-offline` `/system-tts`）。这些条目是**虚拟**的：

- 永远不写进 `config.modelEntries`（`SystemVoiceEntries.kt` 头注释逐字说明）；
- 没有 `ProviderInstance`——provider 就是设备本身；
- 只能由 `SystemVoiceEntries.resolve` / `resolveIn` 合成。

`SystemVoiceEntries.resolveIn(persistedEntries, memberId) = persistedEntries.find { it.id == memberId } ?: resolve(memberId)`
是 dv25/1043-B06 就立好的**单一来源**，兄弟屏全部已对齐
（`ModelGroupsScreen.kt:532,589,614`、`ModelEntryPicker.kt:206,401`、
`AgentLoopModelsScreen.kt:219`、`AgentLoopAddSheets.kt:161,294`、
`AddModelsToGroupScreen.kt:151`、`ChatModelPickerSheet.kt:645…`、
`debug/ProviderDebugMethods.kt:263-265`）。

### 1.2 真树五处（报告报 3 处，真树 4 处缺陷 + 1 处形态待统一）

`ui/settings/ModelGroupDetailScreen.kt` @ 基线 `88ef9b27`：

| 行 | 现状 | 后果 |
|---|---|---|
| `:318-319` | `config.modelEntries.find { it.id == entryId } ?: SystemVoiceEntries.resolve(entryId)` | 结果**正确**，但是手写的两步形态，是后两处漏掉 `?:` 的模板 |
| `:492-495` | `remember(...) { … config.modelEntries.find { it.id == entryId }?.effectiveMaxThinkingLevel }` | **报告没报的第 4 处**：System 成员在思维上限 roll-up 里恒为 null（行为上恰好等价，因为它们是纯音频、无 reasoning 档位——但形态与缺陷同源） |
| `:662` | `val removingEntry = config.modelEntries.find { it.id == removingId }` | 移除确认弹窗丢模型名 → 退化成通用文案 `remove_this_model_fallback` |
| `:368-370` + `:375/:386` | `val instanceLabel = instance?.label` → `Text(instanceLabel ?: "")` | System 成员没有 instance ⇒ **每一行**副标题渲染空字符串 |
| `:418-449` | CostTier 菜单无条件渲染，`onClick` 调 `providerRepository.updateEntry(entry.copy(costTier = …))` | `updateEntry` 内部按 id 在 `config.modelEntries` 里 `indexOfFirst`，虚拟条目得到 −1 ⇒ **静默丢弃**，勾选无反应 |

触发面：上述五处对默认语音组成员 **100% 命中**（这类组没有持久成员）。

### 1.3 门盲区（本轮的核心成因，已在真树核实）

```
$ grep -n 'REL_GROUP_DETAIL' scripts/scan/provider_remember_key_check.py
2401:    (REL_GROUP_DETAIL, check_group_detail),          # 只有 C8（remember 键）
```

而 `check_active_entry_single_source`（C35，判据文本正是「id-keyed lookup into
`modelEntries` 必须咨询 System 单一来源」）**只挂载在 `REL_MODEL_PICKER_SHEET`**
——判据存在、语义完全覆盖本屏、但从未挂上来。用探针把 C35 直接作用在基线文件上：

```
[C35] FAIL — id-keyed lookup into `modelEntries` without the System source
              at line(s) 482, 652
```

（行号是 `strip_comments()` 之后的编号：该门剥块注释时**会吃掉换行**，
基线 `:318` 那处因为同一语句里已有 `SystemVoiceEntries.` 而豁免，
被点名的两处正是 `:492` 与 `:662`。）

### 1.4 报告/任务书的失真（三条，均按真树口径重取）

1. 任务书 §2 要求「复用既有键 `R.string.model_picker_system_provider`」——真树
   **0 命中**（`grep -rn model_picker_system_provider src/android/app/src/main/res/` 空）。
   实际存在的 System provider 名键是 **`R.string.voice_provider_system`**
   （`values/` `values-zh` `values-zh-rTW` `values-ja` `values-ko` `values-de` `values-ru`
   七份都在，`AddModelsToGroupScreen.kt:131` 就在用它）。→ 采用后者，**零 i18n 变更**。
2. 任务书称兄弟实现 `ModelEntryPicker.kt:206/:401`——该文件在 `ui/components/`，不在
   `ui/settings/`（路径失真，行为描述正确）。
3. 任务书报 3 处、实际漏了 `:492-495`（思维上限 roll-up）。这一处是**挂上 C35 之后
   自己抓出来的**，见 §4 的 C35 真树结果。

---

## 2. 修法（改动阶梯）

**停在第 2 级（代码库里已有的单一来源），因为**：这项决定（「持久条目，否则 System 虚拟条目」）
在 dv25/1043-B06 就被收敛成 `SystemVoiceEntries.resolveIn`，本屏只是没接上——
没有新的决策要发明，任何一个新 helper 都是第二次分叉。

- **第 2 级 · 复用 `resolveIn`**：`:325`（成员行）、`:703`（移除弹窗）、`:532`（思维上限）三处。
- **第 6 级 · 一行守卫**：`:530` 加 `.filterNot { SystemVoiceEntries.isSystemEntryId(it) }`。
  这是**刻意保留既有语义**：System 成员是纯音频、无 reasoning 档位，把它们解析进
  roll-up 会去读一个 `system` 模型根本不存在的 models.dev 行，可能把上限抬高。
  显式排除让结果与 dv27 之前**逐字节相同**，同时让「排除」成为代码里读得出来的意图，
  而不是「查不到所以恰好被过滤」的副作用。
- **第 7 级 · 三个纯决策**（`SystemVoiceMemberPresentation.kt`，74 行）**只为本单 §4 的
  L3 要求而存在**：三处判断原来都内联在 Compose 里，纯 JVM 测不到。
  抽成 `memberSubtitle(instanceLabel, isSystemEntry, systemLabel)` /
  `canEditCostTier(isSystemEntry)` / `removalDisplayName(entryName, fallback)`
  ——String/Boolean 进出，**不 import Android、不 import 数据层**（`ProviderConfig.kt`
  带 `@Stable`/`@Serializable`，纯 kotlinc 编不了），照本仓 `SendDisposition` /
  `AlbumSelector` 的形态。

守住的红线（任务书 §2.4/§2.5）：

- `remember(groupId, config.revision)` / `remember(group.memberEntryIds, config.modelEntries)`
  的派生键结构**一字未动**（C8 仍报 `all 7 group-derived remember calls key the exact field they read`）；
- 普通条目的拖拽排序、CostTier 切换、移除路径全部保留（门 m5 的变异臂证明守卫只加在
  CostTier 段、「从组中移除」仍对所有成员可见）；
- **零新增语言键**（`i18n_check.py` 全绿：`✅ every registered locale ships strings behind it (7: en, zh, zh-TW, ja, ko, de, ru)`）；
- 没有动 `SystemVoiceEntries.kt` 本身。

---

## 3. 改动清单

`git diff --stat 88ef9b27..HEAD`（4 文件 **+715 / −39**）：

| 文件 | +/− | 性质 |
|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/ui/settings/ModelGroupDetailScreen.kt` | +80/−37 | 生产（五处接线） |
| `src/android/app/src/main/java/com/rikkaminis/app/ui/settings/SystemVoiceMemberPresentation.kt` | +74/−0 | 生产（新增纯决策，L3 载体） |
| `src/android/app/src/test/java/com/rikkaminis/app/ui/settings/ModelGroupDetailSystemVoiceTest.kt` | +295/−0 | 测试（新增，12 用例） |
| `scripts/scan/provider_remember_key_check.py` | +266/−2 | 门（挂载 + 两条新判据 + 夹具/臂） |

生产侧逐处：

1. `:325` `val entry = SystemVoiceEntries.resolveIn(config.modelEntries, entryId)`
   ——取代手写 `find ?: resolve` 两步形态；
2. `:378-382` `val instanceLabel = SystemVoiceMemberPresentation.memberSubtitle(instance?.label, SystemVoiceEntries.isSystemEntryId(entry.id), stringResource(R.string.voice_provider_system))`
   ——副标题走决策；随后两处 `Text(instanceLabel ?: "")` → `Text(instanceLabel)`；
3. `:428` 用 `if (SystemVoiceMemberPresentation.canEditCostTier(SystemVoiceEntries.isSystemEntryId(entry.id))) { … }`
   **包住** CostTier 标题 + 四档 + `HorizontalDivider`（写调用在块内），
   「从组中移除」留在块外；
4. `:530-533` 思维上限 roll-up 走 `filterNot { isSystemEntryId }` + `resolveIn`；
5. `:703-712` 移除弹窗走 `resolveIn` + `removalDisplayName(entryName, fallback)`。

---

## 4. 门与判据（扩 `provider_remember_key_check.py`，0 新建门）

### 4.1 挂载（本单的核心）

```python
(REL_GROUP_DETAIL, check_active_entry_single_source),   # C35
(REL_GROUP_DETAIL, check_elvis_after_else_null),        # C36
(REL_GROUP_DETAIL, check_system_voice_cost_tier_write), # C-SV1
(REL_GROUP_DETAIL, check_system_voice_member_label),    # C-SV2a / C-SV2b
```

C36 先跑后挂（真树 PASS，不盲挂）；C32 `check_picker_search_gating` 在同一文件上
**fail-closed 红**（它要求 picker 的 `val isCollapsed = …` 段决策，本屏不是 picker）
⇒ **不挂**，登记为负决策（R7）。

### 4.2 两条新判据（均从函数体/调用关系派生，不锚行号与成员清单）

**C-SV1 `check_system_voice_cost_tier_write`** — 每个 `updateEntry(`/`updateEntries(`
调用点必须落在**某个包围块**里，而该块的 header 测试 System 身份。
实现：一次 `{`/`}` 扫描得到全部块区间 → 对调用点取全部**包含它**的块
（本屏写点在第 4 层嵌套：菜单 lambda → `forEach` → `onClick` → `if (tier …)`），
逐个取 header（回溯到上一个 `}`/`{`/`;` 边界，最多 8 行非空行，够看跨行 `if (`）。
header 接受 `isSystemEntryId` / `canEditCostTier` / `entry in …modelEntries` 等
任何拼写的同一决定；若 header 用的是**裸局部 flag**（`isSystemEntry`），
则要求本文件里存在 `isSystemEntry = … isSystemEntryId(` 的派生——**名字不是派生**。

**C-SV2 `check_system_voice_member_label`** — 两条断言：

- **C-SV2a**：文件里任何 `?: ""` 都不允许。用户可见 label 退化成空串就是缺陷本身，
  没有正确用法（基线两处 `Text(instanceLabel ?: "")` 即病灶）。
- **C-SV2b**：在「列组内成员」的屏（含 `memberEntryIds`）上，每一个 provider label 读
  （`….?.label`）所在的**语句**必须咨询 System 单一来源（`SystemVoiceEntries.` 或
  `SystemVoiceMemberPresentation.`）。范围用「包含该读的函数体是否读 `.instances`」
  圈定——`ContextLimitSlider` 里的 `steps.firstOrNull { … }?.label` 是滑杆档位标签，
  与 ProviderInstance 无关，**实测会误报**，故按函数体排除（这条是写完第一版后
  真树跑红 L795 抓出来的，不是预想的）。

### 4.3 带牙臂（夹具 121 → 126，每臂只红一条）

| 夹具 | 改动 | 期望红 | 实测 |
|---|---|---|---|
| `clean` | — | 无 | rc=0 failed=`-` ✅ |
| `c35-group-remove-persisted-only` | 移除弹窗退回 `config.modelEntries.find` | C35 | rc=1 failed=`C35` ✅ |
| `csv1-cost-tier-unguarded` | 守卫换成 `if (tierSelectable)` | C-SV1 | rc=1 failed=`C-SV1` ✅ |
| `csv1-guard-flag-not-derived` | 守卫读裸 flag，且删掉派生 | C-SV1 | rc=1 failed=`C-SV1` ✅ |
| `csv2-subtitle-empty-fallback` | 另加一处 `?: ""` | C-SV2a | rc=1 failed=`C-SV2a` ✅ |
| `csv2-subtitle-no-system-source` | 副标题退回 `instance?.label ?: systemLabel` | C-SV2b | rc=1 failed=`C-SV2b` ✅ |
| `failclosed-missing-group-anchor`（既有，改期望） | 文件重写成无关函数 | C8 + C35 | rc=1 failed=`C35,C8` ✅ |

夹具同步：`FIX_GROUP_DETAIL_CLEAN` 从基线形态改成**修复后形态**（否则 clean 臂会被
新判据判红），共 7 个 `remember` 派生调用保持不变（C8 仍 `all 7 …`）。

### 4.4 PRE 臂（基线文件）

`git show 88ef9b27:…/ModelGroupDetailScreen.kt` 落到 `probe/l3/pre-tree/`，四条判据全红：

```
[C35] FAIL   — … at line(s) 482, 652
[C-SV1] FAIL — entry write(s) at line(s) 418 run unguarded …
[C-SV2a] FAIL — label falls back to "" at line(s) 366, 374
[C-SV2b] FAIL — provider-label read at line(s) 355 never consults the System single source
RESULT RED(4)
```

**如实记一条**：C35 在**修复后**的真树上报的是
`no persisted-only id lookup remains — every entry resolution routes through the System single source`
（`sites=0`）。因为 `resolveIn(modelEntries, id)` 是**参数传入**，`_PERSISTED_LOOKUP_RE`
（`modelEntries.find|first|filter|…`）看不到它。也就是说：挂载后的 C35 对本文件是
**防回归锁**（有人再写下 `config.modelEntries.find { it.id == … }` 立刻红），
本次三处缺陷的**证词**由 PRE 臂提供，不是由修复后树的 PASS 提供。这一点写进 R2。

---

## 5. 验证四层

### L1 门层

- 本门真树：`python3 scripts/scan/provider_remember_key_check.py .` → **rc=0**，
  `[C-SV1] PASS — 1 entry write(s), each inside a block guarded on System identity`、
  `[C-SV2a] PASS`、`[C-SV2b] PASS — 1 provider-label read(s), all through the System single source`、
  C35/C36/C8 全 PASS。
- selftest：`python3 … --selftest` → **`PROVIDER_REMEMBER_SELFTEST=PASS`**，126 夹具全 `OK`，0 MISMATCH。
- PRE 臂：见 §4.4（RED(4)）。
- 全量：`sh scripts/scan/scan.sh` → **`║  Summary: 111 passed, 0 failed`**（rc=0，日志
  `probe/scan_full.log`；门头 `[1/111]..[111/111]` 共 111 个互异编号；日志里含 `FAIL` 的行 0 条）。
- 夹具套件：`python3 scripts/scan/test_scan.py` → **`✅ ALL 340 CASES PASS`**（rc=0）。

### L2 邻门层

按**概念**（`grep -rln 'ModelGroup\|model_group' scripts/scan/*.py`）找真实邻门，逐个 rc=0：

`collection_copy_on_mutate_check` · `compact_entry_pin_migration_check` ·
`deeplink_settings_routes_check` · `envvar_config_group_check` · `four_way_sync_check` ·
`mcp_oauth_lifecycle_check`（本屏被它扫到，真树 PASS）· `prefs_listener_holder_guard` ·
`provider_rpc_symmetry_check` · `xproc_boundary_check` · `i18n_check`（**零语言键证明**：
`Done: ALL CLEAN ✅`，7 个 locale 全通）

→ **10/10 rc=0**。任务书 §4 点名的 `model_group_*` 类门在真树**不存在**（按概念重找，
失真记入 §1.4 同族）。

### L3 装置层（真 kotlinc + 真 JUnitCore，12 用例）

装置 `probe/l3/`（worktree 私有，`.gitignore` 覆盖）：`arm.sh`（决策臂）、`tree_arm.sh`
（树臂：门 + JVM 双层）、`gen_mutants.py`、`gen_tree_muts.py`、`gate_on_tree.py`。
`kotlinc -no-stdlib -classpath kotlin-stdlib:junit:hamcrest`，`java org.junit.runner.JUnitCore`。

| 臂 | 内容 | 结果 |
|---|---|---|
| **POST** | 真 helper + 真测试，cwd=真树 | `OK (12 tests)`，`kotlinc_warnings=0` |
| **PRE-dec** | `helper_pre.kt`（三条决策回到基线形态） | 红恰好 3：`a1_` `a4_` `a6_` |
| **PRE-src** | 真 helper + JUnit 跑在 `probe/l3/pre-tree/`（基线屏文件） | 红恰好 5：`s1_` `s2_` `s3_` `s4_` `s5_`，决策臂全绿 |
| **PRE-both** | 两者同时 | 红 8（3 + 5），不重叠 |
| **m1** | `memberSubtitle = instanceLabel ?: systemLabel`（丢掉 isSystemEntry） | 红仅 `a3_`（陈旧持久成员误报成 System） |
| **m2** | `canEditCostTier = true` | 红仅 `a4_` |
| **m3** | `removalDisplayName = entryName ?: ""` | 红仅 `a7_` |
| **m4**（树） | 移除弹窗退回 persisted-only | 门红 C35（1 条）；JVM 红 `s5_` `s2_` |
| **m5**（树） | 守卫换成 `if (tierSelectable)` | 门红 C-SV1（1 条）；JVM 红 `s3_` |
| **m6**（树） | 成员行副标题退回 `?: ""` | 门红 C-SV2a（1 条）；JVM 红 `s1_` |

变异只施加在 `probe/` 下的**副本**上（`gen_tree_muts.py` 打印
`source sha before/after: 23f28cd6042c31ed 23f28cd6042c31ed` 证明源文件未动），
未使用 `git checkout --` 回滚。

`s3_costTierWriteIsGuarded` 这条臂第一版写成「写点前 600 字符窗口里出现
`canEditCostTier(`」——真树当场红（写点在守卫之后 900+ 字符），改成**结构化**判定：
从守卫自己的 `{` 起做字符串/`${}` 感知的括号配对，再问写点是否落在区间内。
这是「判据锚决定点、别锚距离」的一次现场自证。

### L4 CI 层

| 项 | 值 |
|---|---|
| run / job | **39992098984** / **120546147641**，attempt **1**，event `workflow_dispatch` |
| 结论 | `completed / success` |
| `head_sha` 三方 | 本地 `77ba33fc9e77…` == `run.head_sha` == `ls-remote` **逐字符一致** |
| 步骤 | 26 步 **not_ok=[]**，唯一 skipped = `Publish to Releases`（main 门控，故未污染 release 资产） |
| 日志判读（`probe/ci/judge.py`，1,450,488 B） | **19/19 ALL GREEN** |

判读的 19 条：`║  Summary: 111 passed, 0 failed` 恰一条且带框线前缀 · 无任何非零 failed 的 Summary ·
`✅ ALL 340 CASES PASS` 恰一条 · 门头 `[1/111]..[111/111]` 111 个互异编号 · 编译错 `e: ` 0 ·
游离 FAILED 0（含 `FAILED` 的 4 行逐条确认全是 PASSED 用例名里的词，例如
`RealAgentAdapterSkeletonTest > slot denied produces FAILED without calling provider PASSED`）·
**本支四文件编译警告 0** · 新测试类 **12/12 PASSED**（逐条在日志里）· 门输出里
`C-SV1` / `C-SV2a` / `C-SV2b` 各有真树 PASS 行 · `run_attempt==1` · 步骤结论无 failure。

CI 同时证明了两件沙箱证不了的事：`compileReleaseKotlin` 接受跨行 `if (` 守卫（§8 不确定项 1），
`testReleaseUnitTest` 真跑了新测试类（不是「编译通过但没执行」）。


---

## 6. 残留与负决策（全部带触发条件，已落 backlog `§1047-B3`）

| # | 内容 | 级别 | 触发条件 |
|---|---|---|---|
| R1 | 真机行为级欠账：进默认语音组看 ①副标题显示「系统/System」②移除弹窗显示真实模型名 ③虚拟条目菜单里没有成本档位、但「从组中移除」仍在 | P1 | 下次真机窗口（本单动的是 UI 渲染，JVM/门只能钉决策与接线） |
| R2 | **C35 在修复后真树上 `sites=0`**（`resolveIn` 是参数传入，正则看不见）⇒ 它对本文件是防回归锁，不是当前缺陷的证词 | P2 | 该门再次被当作「已覆盖」的证据引用时；或有人把 `resolveIn` 内联回 `find` 而 C35 未红 |
| R3 | `check_picker_search_gating`（C32）挂本文件会 fail-closed 红（本屏不是 picker），故不挂 | P2 | 有人把本屏改成 picker 形态，或 C32 的 fail-closed 改成 scope-aware |
| R4 | C34 `check_group_member_resolution` 未挂本屏：它锚 `memberEntryIds` 提及，而本屏经**本地** `memberIds` 迭代 ⇒ 挂上是空跑（`7 aggregate-only site(s) — nothing to grade`） | P2 | 本屏改用 `memberEntryIds` 直接解析，或 C34 增加 `itemsIndexed(memberIds…)` 形态 |
| R5 | 非 System 成员的 provider 实例被删后，副标题仍为空（`memberSubtitle` 的 else 分支）——**既有行为**，超出本单半径 | P2 | 用户报告「成员行没有副标题」 |
| R6 | 同屏 `" · Provider disabled"` 是硬编码英文（既有 i18n 欠账，未动） | P3 | 下一轮本地化清理 |
| R7 | 报告失真三条（不存在的 `model_picker_system_provider` 键 / `ModelEntryPicker.kt` 路径 / 漏报 `:492-495`） | P3 | 下一波派发前重锚时若再次出现同类 | 
| R8 | 门 `strip_comments()` 会吃掉块注释的换行 ⇒ 报出的行号是**剥离后**的（`:492` → `482`）。既有行为，改动它会动到所有判据的行号口径 | P2 | 需要与源码逐行对齐时（本轮报告已两种口径并列） |

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 main | `88ef9b2774cf14a0c983d5cb138db0e052ea7a83` |
| 提交 | `7454e11f` 生产 → `b46af699` 测试 → `77ba33fc` 门（HEAD） |
| 分支 tip | `77ba33fc` = 本地 HEAD = `origin` 分支 |
| CI | run **39992098984** / job **120546147641** / run_number 2569 / attempt **1** / `head_sha` `77ba33fc…` / event `workflow_dispatch` / `conclusion=success`，唯一 skipped = `Publish to Releases` |

**停止节点：分支 CI 绿。未合并 main、未删分支、未动他人支。**

---

## 8. 自证成本与不确定项

- **自证成本**：门 selftest 126 夹具（+5）、10 个 JVM 臂（4 决策/树 × 双源 + 3 纯函数变异
  + 3 树变异）、1 次全量 scan + 1 次 test_scan、1 次 CI。
- **不确定项 1**：`compileReleaseKotlin` 未在本地验证（沙箱无 Android SDK，Compose 编不了）。
  `if (\n …canEditCostTier(…)\n) {` 这种跨行条件是合法 Kotlin，但**只有 CI 能证明**——
  CI 若在此处红，形态改成单行守卫即可（判据不依赖写法，见 §4.2）。
- **不确定项 2**：`stringResource(R.string.voice_provider_system)` 在 `itemsIndexed` 的
  item lambda 里读——与文件内既有的多处 `stringResource` 同类用法一致，
  不是新形态；若 lint 报 composable 调用位置问题，提到屏幕顶部声明即可。
- **不确定项 3**：C-SV2b 的「函数体含 `.instances`」这一范围判据是**实证后收窄**的
  （真树先红 `ContextLimitSlider:795`）；若将来把 provider label 读搬到不读 `.instances`
  的 helper 里，该判据会静默失效（登记 R4 同族风险）。

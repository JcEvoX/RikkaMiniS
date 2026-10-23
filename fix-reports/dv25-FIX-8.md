# dv25-FIX-8 · B-04（P1）模型选择器群组头部 active-entry 解析漏接单源 + `else null ?:` 优先级吞并

> 本报告由主会话收口时按 `backlog §1045-B04` + 分支提交 + CI 判读重建（支 8 会话未落盘报告文件）。

## 1 身份链

- 基线 main `1d44ec90`；分支 `fix/dv25-b4-picker-member-resolve`，3 提交（`28464e09` 生产 / `b3d9a1b3` 测试 / `95d84870` 门）
- 4 文件 **+446/−9**；门 109 / 夹具 336 不变（0 新建门，既有门 `provider_remember_key_check.py` 扩 C35/C36）
- CI run `39915172412`（#2537，job `120287019732`）success，`head_sha=95d84870` 三方逐字符一致（EXACT）

## 2 缺陷本体

`ChatModelPickerSheet.kt` 群组头部解析「选中群组的 active entry」用**落库列表裸 `find`**，而紧邻两行的成员解析用**单源 `SystemVoiceEntries.resolveIn`** —— 同一个「持久化条目优先、System 哨兵次之」被写成两种拼写。默认 Voice Input/Output 群组**只由哨兵组成**（哨兵从不持久化）⇒ 裸 `find` 对哨兵 id 必然 null ⇒ 群组头部退化「N models / No linked models」，而下方成员行却能正确列出同一模型。

同时 `if (…) { … } else null ?: fallback` 的 `?:` 在 Kotlin 中**比 if 分支绑定更紧**，elvis 只挂到 `else` 分支 ⇒ 选中分支（唯一要紧的分支）根本没有回退。IDE 不报错、编译器不警告。

## 3 修法（阶梯第 7 级：抽纯核 + 单源注入）

抽纯核 `ui/chat/PickerGroupResolution.kt`（**泛型 `<T>`、无 Compose / 无 Android 依赖**）的 `pickerHeaderEntry` / `pickerDisplayMembers`，**单源由调用方注入**（本文件不可能长出第二份拼写）；两处调用点改走纯函数；`?:` 从 if 分支上摘下来落到整个表达式。既有契约全保：System 哨兵 `nonTextLabelFor` 展示契约、`activeEntryId == null` 时列表全展示、`remember` 键（C33 判据）一字未动；未引入 `modelEntries + SystemVoiceEntries.list` 二次拼接（任务书禁止）。

## 4 门（扩既有 `provider_remember_key_check.py`，0 新建门）

- **C35** `check_active_entry_single_source`：从树派生「id-keyed 落库查找」站点（`_PERSISTED_LOOKUP_RE` + `_ID_KEY_RE`），每站必须经 `SystemVoiceEntries.` 家族；fail-closed（既无落库查找又无 System 引用 ⇒ 红）。
- **C36** `check_elvis_after_else_null`：`\belse\s+null\s*\?:` 一律红（加括号 `else (null ?: x)` 同样红，锚决定点不锚拼写）。
- 新增工具 `_statement_around()`：既有 `_member_block()` 只向前走，遇到「token 在调用中段」会把语句截半并漏掉下一行的单源调用 ⇒ 改为**双向**语句边界扫描。
- 3 条新带牙臂各精确红一条判据；既有 `c34-picker-no-system-fallback` / `failclosed-sheet-missing-anchor` 期望集按新增判据更新。

## 5 验证四层

- L1 本门真树 rc=0（61 PASS，含 C35/C36 各一条）+ `--selftest` PASS（119 fixture）+ **PRE 臂基线树确实红**（`golden/` rc=1：`[C35] FAIL … line(s) 582, 742`、`[C36] FAIL … line(s) 584`）
- L2 概念反查邻门 3/3 rc=0（`chat_ui_interaction_check` / `entry_id_rename_integrity_check` / `provider_boundary_guard`）
- L3 真 kotlinc + JUnitCore 四臂：**POST 直接编译生产文件本身** `OK (11 tests)` / PRE（基线决策逐字复刻）**11 run / 4 failures**（哨兵 active entry、失效 active id 回退、哨兵-only 群组回退、成员空列表回退哨兵）/ m1 4 红 / m2 1 红
- L4 CI #2537：26 步 non-success `[]`、唯一 skipped=Publish、门头 109 连续互异、恰一条 `109 passed, 0 failed`、`ALL 336`、`Z e: ` 0、游离 FAILED 0、`PickerGroupResolutionTest` 10/10 PASSED

## 6 三条规则级收获

1. **凡「token 出现在表达式中段」的判据，站点切分必须双向**；单向（`_member_block` 只向前）会系统性漏判。
2. **需要 L3 硬证据的纯核优先泛型化 + 依赖注入**（直接依赖 `ModelEntry` 会拖进 kotlinx-serialization，沙箱编不了 ⇒ 只能测副本）。
3. **`else null ?:` 是独立于语义的语言级缺陷形状**，值得单立判据（不违反类型契约、无警告，只有运行语义坏掉）。

## 7 残留

R1 真机行为级（默认 Voice 组头部显示哨兵名、成员行不再 No linked models）· R2/R3 `pickerHeaderEntry` 不检查 active 归属（P2，沿用基线语义）· R4 `_statement_around` 是启发式行/括号扫描（P3）· R5 C35 只认 `modelEntries.` 前缀（P3）· R6 fail-closed 条件（P3）· R7 纯核目前仅两个调用点（首要目的是可测性）· R8 `ChatScreen.kt` 侧 active 解析归支 9（禁区）。全带触发条件。

## 8 影响面

模型选择器群组头部与成员行统一走 System 单源 ⇒ 默认 Voice 组不再显示「2 models (unlinked)」/「No linked models」。未改：`ChatScreen.kt`、`SystemVoiceEntries.kt`、`ModelGroup`/`ModelEntry`、`remember` 键、`strings.xml`、schema、依赖、7+1 语言。

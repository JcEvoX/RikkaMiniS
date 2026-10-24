# dv28-FIX-B1 — deep-audit-1048 支线 B1（UI 交接五条：B-01 / B-03 / B-04 / B-05 / B-06）

- 分支：`fix/dv28-b1-ui-handoff`
- 基线：`eea9f407f9e0e79f4902eeef1f84e1550ee5c29c`（审计报告 v26-A/B 声明值，实测 `git ls-remote` 一致）
- 生产提交 `99322a05` / 门提交 `89c63058`
- 门 **111** / 夹具 **342** 不变（本支 0 新建门，全部扩既有门）

---

## 1. 缺陷与真树取证（每条都先在真树上取一次证据，不采信报告行号）

### B-01「IO 线程里写 Compose state」（5 处方，真树实测 4 文件 / 31 处写点）

判定轴：`withContext(Dispatchers.IO|Default) { … }` 块内对 `remember` 状态的赋值 = 违规；**赋值右侧本身就是 `withContext(...)` 调用**（`x = withContext(IO){…}`）不算——那次写在块返回后、在调用者线程上发生。

| 文件 | 真树命中 | 形态 |
|---|---|---|
| `ui/sandbox/FilePreviewScreen.kt` | **16** 处（TextPreview 3 / MarkdownPreview 2 / PdfPreview 2 / CsvPreview 4 / JsonPreview 3 / ArchivePreview 2） | `content = decodeUtf8Capped(...)`、`rows = slice.rows`、`entries = out.sortedBy{…}` … |
| `webapp/AddToHomeSheet.kt` | **8** 处 | `copiedFile = target`、`parsedTitle = parseHtmlTitle(target)`、`webpageIconPath = …`、`copyFailed = true` ×2 … |
| `ui/settings/SoulSettingsScreen.kt` | **5** 处 | `name / preservedEmoji / style / lang / body` 全在 IO 块里赋值（`loaded = true` 在外，写点位置正确） |
| `ui/settings/LogManagementScreen.kt` | **2** 处 | `LogDetailScreen` 的 `loadError = …`（文件不存在 / 读取失败两臂） |

真树取证命令（可复跑）：`python3 probe/measure_f_scope3.py .`（脚本在 `probe/`，产出与上表逐条一致；`x = withContext(…)` 12 处已按语义排除，例如 `MoveToSessionSheet.kt:68`、`BackupSettingsScreen.kt:160`、`StorageManagementScreen.kt:222`）。

`LogManagementScreen.kt:131` 的 `val (daily, crash, total) = withContext(IO){…}` 与 `FilePreviewScreen:189` 的 `produceState` 同款（`value = withContext(...)`）——**本来就是正确形态，未动**。

### B-03「artifactWriter 返回值被丢弃」（`ui/chat/ChatToolExecutors.kt`）

同期截图分支 `:154-163` 消费了返回值（`if (persistPath != null) { … minis_url … }`），fetch 分支 `:169-172` 是裸调用：

```kotlin
artifactWriter(fetchName, fetchData)                 // 返回值丢弃
linuxPathToMinisURL("/var/minis/browser/$fetchName") // 无条件追加
```

⇒ 写盘失败时仍把 `minis_url: /var/minis/browser/<name>` 交给 agent，指向不存在的文件；下一次 `read_image` / 读文件才失败，指向错误的层。

### B-04「分享出口静默 + chooser 缺 NEW_TASK」（3 文件 / 5 个失败出口）

真树取证（`grep -n "createChooser\|startActivity\|?: return"`）：

| 文件 | 失败出口（静默） | chooser NEW_TASK |
|---|---|---|
| `ui/settings/ProviderDetailScreen.kt` `exportProviderInstance` | `?: return`（暂存写失败）+ `return`（FileProvider 失败）+ 裸 `startActivity` | 有 |
| `ui/settings/MCPIntegrationsScreen.kt` `shareMcpServerJson` | 同上两条 | 有 |
| `ui/settings/LogManagementScreen.kt` `buildLogShareChooser` / `shareLogFile` | `?: return null` → `shareLogFile` 的 `?: return` | **两处 `Intent.createChooser` 都没有**（`:649` / `:673`，`startActivity` 在 `:680`） |

三处的 `startActivity` 都没有 `runCatching`：无分享目标时 `ActivityNotFoundException` 直接逃出协程。
**报告失真**：任务书说「另两处 `:687`/`:1145` 有 NEW_TASK」——`LogManagementScreen` 全文只有两个 chooser（`:649`、`:673`），`:687` 属于 `ProviderDetailScreen`、`:1145` 属于 `MCPIntegrationsScreen`。行号归属错误，判读不受影响（见 §6）。

### B-05「存储体积缓存漏失效」（2 条写入路径）

- `ui/sandbox/FileBrowserViewModel.kt:409 deleteItem`：`val deleted = …` → `if (!deleted) { … return@launch }` → `loadItems()`（`:456`），**无 `invalidateStorageSnapshot()`**；全仓 7 处 `invalidateStorageSnapshot` 引用里没有它。
- 同族第二面（任务书要求先证）：`ui/chat/ChatHistoryDrawer.kt:422 deleteSessionAndCleanup` → `:449 chatRepository.deleteSession(id)`，而 `ChatRepository.deleteSession` **确实删盘**：`ChatRepository.kt:216-258` 在删行之后调 `store.deleteSessionFiles(id)`（注释明写「Deleting a session means deleting the whole session, not just its text」），删的正是 `minis-sessions/<id>`（workspace / attachments / offloads / browser）；而 Storage 页按会话递归量这些目录（`StorageManagementScreen.kt:299-310` 的 `sessionFiles.sessionSubdirSizes(session.id)`）。⇒ **真属同族，一并补**。

### B-06「Add to Home 无在飞守卫、无关闭闩锁」（`webapp/AddToHomeSheet.kt`）

- 主操作 `enabled = copiedFile != null && titleText.isNotBlank()`（`:465`）——收面无 `isPinning`；点击后 `create(…)` + `ShortcutPinner.pin(…)` 全程按钮可点 ⇒ 同帧/次帧二次点击会再插一行、再 pin 一次，且第一次的 `cleanupCopiedAttachment()` 会把副本从第二次脚下删掉。
- `onDismissRequest` / Cancel / `onDispose` 三处拆除路径都只清文件，**没有任何「面板已关」状态**；`coroutineScope` 的 launch 在面板离开组合后照常跑完 ⇒ 用户点了取消，仍可能插行 + 上桌面图标。

---

## 2. 修法（走改动阶梯，逐条给「停在第 N 级」）

| 工单 | 停在哪级 | 为什么 |
|---|---|---|
| B-01 | **第 7 级（最小实现）** | 4 个文件各自把「IO 块内赋值」改成「IO 块返回结果 → 语句返回后在 Main 赋值」；语义一字未改（`runCatching` 保留了原来的 `catch (Exception)` 覆盖面），未引入新抽象。 |
| B-03 | **第 2 级（照抄库内既有形态）** | fetch 臂按同一文件里**已经正确**的截图臂逐字对齐（`if (persistPath != null)` 才追加 URL），失败臂给一句文本说明。 |
| B-04 | **第 2 级 + 第 5 级（平台原生）** | 复用既有 toast 通道与既有字符串 `R.string.file_share_failed_toast`（7 个语言文件里都有，**0 新增字符串**）；`FLAG_ACTIVITY_NEW_TASK` 用平台常量，不新造 flag 逻辑。 |
| B-05 | **第 2 级（复用既有单点）** | 调既有的 `invalidateStorageSnapshot()`（`StorageManagementScreen.kt:140` 的单一清理入口），不新增缓存 API。 |
| B-06 | **第 1 级（新增一个纯函数文件）** | 决策必须可测：`WebAppPinGate.kt` 只有两个布尔函数、零 Android/Compose 依赖 ⇒ 沙箱 kotlinc 真编译真跑（§5 L3）。内联布尔表达式在「无反 Android 环境」里只能靠门看形态，测不出真值表。 |

四文件共享的一处刻意取舍：`runCatching` 沿用了原 `catch (e: Exception)` 的覆盖面（也吞 `CancellationException`，与改前一致）——**不顺手「修」取消语义**（半径纪律：触发路径只有「IO 线程写 state」这一条）。

---

## 3. 改动清单

`git diff eea9f407..89c63058`：**14 文件 +1503 / −186**（生产 10 / 门 4）。

| 文件 | 类型 | 说明 |
|---|---|---|
| `ui/sandbox/FilePreviewScreen.kt` | 生产 | 6 个预览：IO 只产出值，`fold` 在 Main 发布 |
| `ui/settings/SoulSettingsScreen.kt` | 生产 | `LaunchedEffect` 解析结果回 Main 再落 5 个 state |
| `ui/settings/LogManagementScreen.kt` | 生产 | `LogOpenResult` 承载 open 结果；两处 chooser 加 NEW_TASK；`shareLogFile` 返回 Boolean + `runCatching`；调用点 toast |
| `webapp/AddToHomeSheet.kt` | 生产 | `StagedCopy` 承载暂存结果（IO→Main）；`isPinning` / `sheetClosed`；`finally` 释放 |
| `webapp/WebAppPinGate.kt` | 生产（新） | `pinActionEnabled` / `pinShouldCommit`（纯函数） |
| `ui/chat/ChatToolExecutors.kt` | 生产 | fetch 臂消费 `artifactWriter` 判定 |
| `ui/settings/ProviderDetailScreen.kt` | 生产 | 返回 Boolean + NEW_TASK + `runCatching`；调用点 toast |
| `ui/settings/MCPIntegrationsScreen.kt` | 生产 | 同上 |
| `ui/sandbox/FileBrowserViewModel.kt` | 生产 | 成功分支 `invalidateStorageSnapshot()`（import + 调用） |
| `ui/chat/ChatHistoryDrawer.kt` | 生产 | 删会话后无条件失效（回收可能部分失败，缓存照样过期） |
| `scripts/scan/sysinfo_state_thread_check.py` | 门 | F 作用域派生 + holder 形态；新增 C-INV |
| `scripts/scan/share_dir_prune_check.py` | 门 | 新增 arm6（C-NT / C-SILENT） |
| `scripts/scan/webapp_asset_url_cleanup_check.py` | 门 | 新增 arm13 / arm14；arm3 锚点由「单行拼写」改为「块内派生」 |
| `scripts/scan/tool_title_error_branch_check.py` | 门 | 新增 C-ART |

`scripts/scan/scan.sh` 与 `scripts/scan/test_scan.py` **零触碰**（冻结面）；门数 111、夹具 342 不变。

---

## 4. 门与判据（全部「派生，不锚拼写」；每条都有带牙臂 + 合规臂）

### 4.1 `sysinfo_state_thread_check.py`

**(a) F 的作用域改成派生**（原来是硬编码 `STORAGE_SCREEN` 单文件）：

```python
def off_main_state_files(repo_root):   # 任何同时具备 remember 状态 + 离主线程块的 .kt
```

真树实测派生集合正好是 5 个文件（Storage / FilePreview / LogManagement / Soul / AddToHomeSheet），除 Storage 外的 4 个正是本支要修的。另设 `EXPECTED_OFF_MAIN_SCOPE` **覆盖锚点**：派生集合必须 ⊇ 该 5 项，否则「文件被删/改名」会变成静默缩面 ⇒ 红（删 `SoulSettingsScreen.kt` 的臂验证）。

**(b) 覆盖 `val x = remember { mutableStateOf(…) }` 形态**：新增 `REMEMBER_HOLDER` + 对 `<name>.value = …` 的判定；委托形态 `var x by remember` 照旧判 `x = …`。两种形态各有独立带牙臂。

**(c) 新增 C-INV（B-05）**：从行为派生两条写入路径（`FileBrowserViewModel` 里「删文件且重新列目录」的成员、`ChatHistoryDrawer` 里「调 `deleteSession`」的成员），要求：

1. 必须调 `invalidateStorageSnapshot(`；
2. 若函数内有 `if (!deleted) { … }` 判定，调用点必须在**该块之后**（失败分支调用 ⇒ 红）；
3. 调用不得落在 `catch` 臂里。

新增 7 条 selftest 臂（合规离主线程屏 / IO 内写委托态 / IO 内写 holder / 删锚点文件 / 删除不失效 / 失败分支失效 / 删会话不失效），原 15 条臂全保留。

### 4.2 `share_dir_prune_check.py` — 新 arm6

- **C-NT**：每个 `Intent.createChooser(` 必须带 `FLAG_ACTIVITY_NEW_TASK`（语句内，或 `<chooser>.addFlags(…)` 同函数）；
- **C-SILENT**：调用 `startActivity` 的函数必须**声明非 Unit 返回值**、无裸 `return` / `return null`、`startActivity` 必须在 `runCatching`/`try` 内；且**每个调用点必须赋值给局部**，且其所在块内要有可见反馈（toast / snackbar / AppLogger）。

6 条 selftest 臂（合规 / Unit 返回值+裸出口 / 缺 NEW_TASK / startActivity 无包裹 / 调用点丢返回值 / 失败不可见）。

### 4.3 `webapp_asset_url_cleanup_check.py` — 新 arm13 / arm14

- **arm13（C-PIN）**：从 `Button(` 的 `enabled` 表达式里派生「引用了某个 `remember` 布尔」，并要求该按钮自己的 `onClick` 在**置 true 之前**读过它（读-判-置同帧）；
- **arm14（C-CLOSE）**：拆除钩子（`onDismissRequest` / `onDispose` / 调 `onDismiss()` 且不含 `.launch` 的 onClick）里置 true 的 `remember` 布尔，必须在 pin 协程里**IO 之后、`create(` 与 `ShortcutPinner.pin(` 之前**被消费（顺序判：守卫放到 `create(` 之后 ⇒ 红）。

5 条 selftest 臂，其中一条是**改名臂**（`isPinning→busy` / `sheetClosed→gone` 仍须绿），证明判据不认拼写。

**既有 arm3 的一处必要改动**（在报告里明示，不是放宽）：原来要求 `onDispose { cleanupCopiedAttachment() }` 这一整行字面量；B-06 需要在同一块里加闩锁语句，所以改成「`onDispose` 块体内含 `cleanupCopiedAttachment()`」——判据意图（所有拆卸路径汇到同一个钩子）不变，去掉了拼写锁。

### 4.4 `tool_title_error_branch_check.py` — 新 C-ART

`artifactWriter(` 的每个调用点：①必须 `val X = …` 赋值；②`X` 必须与 `null` 比较；③该守卫块内必须有 `minis_url` 的追加。三者缺一即红；文件里完全没有调用点也红（主体移动）。

4 条 selftest 臂（合规 / 丢弃返回值 / URL 在守卫外 / 从不判空）。

---

## 5. 验证四层

### L1 门层

| 项 | 结果 |
|---|---|
| 本支 4 门真树 | `sysinfo_state_thread_check` rc=0 / `share_dir_prune_check` rc=0 / `webapp_asset_url_cleanup_check` rc=0（PASS all 15 arms）/ `tool_title_error_branch_check` rc=0 |
| 4 门 `--selftest` | 均 PASS（`SYSINFO-STATE SELFTEST=PASS` / `SHARE_DIR_PRUNE_SELFTEST=PASS` + `SHARE_HANDOFF_SELFTEST=PASS` / `WEBAPP_ASSET_URL_CLEANUP_SELFTEST=PASS` / `TOOL_TITLE_ERROR_BRANCH_SELFTEST=PASS`） |
| 全量门 | **109/109 门 rc=0**（`probe/run_gates_inproc.py`，逐门输出见 `probe/gates_inproc2.log`）＋ 剩余 2 个非门步骤单跑：`[2/111] i18n`（`✅ No orphan keys (1565 refs, 1574 defs)`）、`[7/111] Native crash-field host test`（`ALL PASS`）⇒ 合计 111 步全绿 |
| 夹具 | `ALL 342 CASES PASS`（`probe/run_test_scan_inproc.py`：保留 test_scan 的原夹具与断言，仅把 `run_scanner` 换成进程内 import+exec，等价于 `python3 <gate> <root>`） |

> 环境说明（不影响判据）：沙箱此刻 `fork/exec` 间歇性报 `Function not implemented`，直接跑 `sh scripts/scan/scan.sh` 会得到 90+ 条 ENOSYS 假红（实测三次：`14 passed/97 failed`、`21/90`、随后 python 进程被杀）。因此改用「单进程内 import+exec 每个门」的等价跑法，argv 从 `scan.sh` 的调用行解析（含 `--root` 变体）。**canonical 的 `Summary: 111 passed, 0 failed` 由 L4 CI 提供。**

### L1′ PRE 臂（基线 `eea9f407` 的源文件构树，四门必须红）

| 门 | PRE 结果 |
|---|---|
| sysinfo | rc=1，**33 条**：`[F off-main-state]` **31** 条（FilePreview 16 / AddToHomeSheet 8 / Soul 5 / LogManagement 2）+ `[C-INV]` **2** 条（`FileBrowserViewModel.kt:409 deleteItem`、`ChatHistoryDrawer.kt:422 deleteSessionAndCleanup`） |
| share | rc=1，arm6：`C-NT` **2** 条（LogManagement 两个 chooser）+ `C-SILENT` **14** 条（3 个函数各「无判定 / 裸 return ×2 / startActivity 未包裹 / 调用点丢弃返回值」） |
| webapp | rc=1，`FAIL arms=arm13,arm14`（正是 B-06 两条） |
| tool_title | rc=1，`artifactWriter() result discarded — … artifactWriter(fetchName, fetchData)` |

PRE 树用完即删（`probe/pre` 已清理，仅留 `probe/pre-logs/`）。

### L2 邻门层（按概念重找，全部 rc=0）

`provider_remember_key_check`（同一屏的 remember 键）· `mirror_vm_thread_check`（线程/状态）· `skill_import_offmain_check`（离主线程 IO）· `file_provider_paths_coverage_check`（FileProvider 路径）· `log_url_redaction_guard`（日志分享）· `session_sheet_id_binding_check`（会话 sheet）· `skill_restore_session_cleanup_check`（会话清理）· `provider_rpc_symmetry_check` — **8/8 rc=0**（见 `probe/gates_inproc2.log`）。

### L3 装置层

**(a) 真 kotlinc + JUnitCore（B-06，`probe/l3/run.sh`）**：源文件 `webapp/WebAppPinGate.kt` 真编译（kotlinc 2.0.21 / Java 17），运行 classpath 显式带 `kotlin-stdlib.jar`：

- **POST**：`OK (6 tests)`；
- **PRE 臂（缺陷真实性）**：测试内把改前谓词逐字写出 `legacyEnabled(hasTarget, hasTitle) = hasTarget && hasTitle`，断言它在「在飞」时仍为 `true`，而同场景下 `pinActionEnabled(...) = false`；
- **变异**：m1 去掉 `inFlight` 项 → 2 条测试红；m2 去掉 `closed` 项 → 2 条红；m3 `pinShouldCommit = inFlight` → 1 条红；m4 `= !closed` → 1 条红；
- 源文件 sha256 前后 `3fcf1532…eeb9b` 一致（变异只在 `probe/l3/tree` 副本上做）。

**(b) 真树字节变异（B-01/B-03/B-04/B-05，Compose/Android 绑定，降级理由：`startActivity`/`FileProvider`/`withContext` 状态写都无法在无 Android SDK 的沙箱里运行）**：`probe/mutate_tree.py` 逐条「造违规 → 红 → 字节级还原 → sha256 对拍」：

| 臂 | 变异 | 门 | 结果 |
|---|---|---|---|
| B-01 / F | Soul 的 IO 块内插 `name = "x"` | sysinfo | rc=1，`[F off-main-state] … SoulSettingsScreen.kt:112 assigns Compose state 'name'`，sha 同 |
| B-05 / C-INV | 删 `invalidateStorageSnapshot()` | sysinfo | rc=1，`[C-INV] … FileBrowserViewModel.kt:410 (deleteItem) deletes … without invalidating`，sha 同 |
| B-03 / C-ART | fetch 臂退回裸调用 + `if (true)` | tool_title | rc=1，`artifactWriter() result discarded`，sha 同 |
| B-04 / C-NT | 删 chooser 的 `addFlags(NEW_TASK)` | share | rc=1，`[C-NT]`，sha 同 |
| B-04 / C-SILENT | `shareLogFile` 去掉 Boolean 返回 | share | rc=1，`[C-SILENT]`，sha 同 |

`ALL ARMS MATCH`（5/5），全程未用 `git checkout -- <file>`。

### L4 CI 层

**run `40053301653`（#2594）/ job `120750556417` / attempt 1 / `success`**，`head_sha` = 本地 HEAD = `git ls-remote` = `f182c766`。26 步 `not_ok=[]`，唯一 `skipped` = `Publish to Releases`（main 门控）。日志判读（`probe/judge_b1.py`，14 项全 PASS）：

- 权威行 `║  Summary: 111 passed, 0 failed` 恰一条；另有 8 条**分组** Summary（25/5/13/33/10/22/10/111），全部 `0 failed`
- `ALL 342 CASES PASS` 恰一条；编译错（` e: `）0 条；游离 FAILED（行尾 FAILED）0 条
- 门头 `[1/111]…[111/111]` 共 111 条、互异、连续
- 本支扩展的四门均有见证行（sysinfo 2 / share 3 / webapp 2 / tooltitle 3）
- **警告对基线主 CI（run `40035181763` / job `120692773417`）按（文件, 文本）对齐：新增 0、减少 0；两侧各 191 条**

前两轮红（`#2590` 编译错、`#2593` 源形状测试失配）与本轮绿的身份链、根因、修法见 §7 的「红→绿的两轮记录」。

---

## 6. 残留与失真

**报告/真树失真（以真树为准）**

1. B-04 说「三处 `:680` / `:687` / `:1147` 有 NEW_TASK」——`:687`、`:1147` 不在 `LogManagementScreen.kt`（属于 `ProviderDetailScreen.kt` / `MCPIntegrationsScreen.kt`）。真树里 `LogManagementScreen` 只有两个 chooser（`:649`、`:673`）且都没有该 flag，`startActivity` 在 `:680`。
2. B-01 说「LogManagementScreen 3 处」——真树只有 1 处违规（`:480` 的 `LogDetailScreen`）；`:131` 已是正确形态，`:271` 是普通 suspend 调用。
3. B-01 任务书要求 F 也覆盖 `_uiState.value = `（StateFlow 写法）——**未采纳，理由**：`MutableStateFlow` 的 `value` 赋值线程安全（跨线程有 happens-before），与「Compose 快照状态无 happens-before」不是同一缺陷类；把它纳入会在真树把 `FileBrowserViewModel.deleteItem` 的既有合法写法判红（`_uiState.value = _uiState.value.copy(...)` 在 `Dispatchers.IO` 内 3 处）。`.value =` 作为**写法**已被 `val x = remember { mutableStateOf(…) }` 的 holder 形态覆盖。
4. B-05 说「`ChatHistoryDrawer.kt:449`」——`:449` 是 `chatRepository.deleteSession(id)` 调用行，函数本身在 `:422`（判据按函数体派生，不受影响）。

**残留（每条带触发条件）**

- **R1 真机行为级欠账**（全部 5 条）：见 §7 表格；触发条件 = 下次真机窗口一次开齐。
- **R2 F 判据的派生面**：只覆盖 `withContext(Dispatchers.IO|Default) { … }` 块；`produceState`、`viewModelScope.launch(Dispatchers.IO) { }`、`withContext(NonCancellable)` 里的状态写尚未纳入（真树目前无违规）。触发条件 = 报告或真机出现「改完不刷新/串帧」类症状且落在这些形态上。
- **R3 `AddToHomeSheet` 的 pin 时长**：`isPinning` 现在只由 `finally` 释放，`ShortcutPinner.pin` 是 IPC，若某 ROM 上永不返回则按钮长期禁用（比原来的「可重复点」更安全但仍不理想）。触发条件 = 真机反馈「加桌面卡住」。
- **R4 `StorageSnapshotCache` 的 3 分钟窗口**：本支只补失效调用，未重估窗口策略（缓存仍可能把「他处写入」的新数据挡在 3 分钟内）。触发条件 = 出现「删了/加了文件，存储页数字仍不动」的真机报告。
- **R5 `exportProviderInstance` 等三个 share 路径仍只回 Boolean**：区分不了「写盘失败 / FileProvider 坏 / 无目标应用」，toast 文案统一。触发条件 = 需要远程排障时（改成 enum + 分支文案）。
- **R6 `shareLogFile` 的 `?: return false`**：文件不存在与「文件在但读不出」都落到 false（与改前的 `?: return` 语义等价），未细分。
- **R7 arm13/arm14 只看 `AddToHomeSheet.kt`**：同一个 sheet 若将来拆成多个 composable 文件，arm14 依赖「同一文件内 launch + guard」的假设会失效，届时需把 arm14 的作用域改成「按 `ShortcutPinner.pin(` 的调用者派生」。触发条件 = 该文件拆分时。
- **R9 本支改过的文件上仍有 3 条**既有**警告（不修）**：`MCPIntegrationsScreen.kt:346`（`LocalClipboardManager` 弃用）、`ProviderDetailScreen.kt:832`（`Icons.Filled.VolumeUp` 弃用）、`SharedCacheExportPruneTest.kt:175`（`File(System.getProperty("user.dir"))` 可空）。证据：与基线主 CI 日志按（文件, 文本）对齐后**新增 0**，三条都在 `eea9f407` 上已存在（`SharedCacheExportPruneTest` 那行的 blame 指向更早的 FIX-G 波次）。按半径纪律只登记不修——修它们是别的波的 P2 清理，与 B1 的触发路径无关。触发条件 = 该文件下次被本支同族改动顺带触碰，或 CI 把弃用警告升级为错误。
- **R10 L4 判读脚本的头一版有两处假红（已修，记形态）**：①`Summary` 行按「恰一条」判会红——本波 `scan.sh` 除权威带框线行外还打 8 条分组汇总，正确判据是「**权威行恰一条** + 所有汇总 `failed == 0`」；②门名去重按「`[n/111]` 后第一个词」取会红——`A/ISSUE-01 …` 这类历史门共享前缀，不同门被截成同名，正确判据是「取 `]` 到行尾整段标题」。同族先例 1047（判据按文件名硬拼 → 假红）。**规则：判据先在真样本上跑出形态，再写死条件。**
- **R11 两条 JVM 装置在沙箱编不出来**：`LogShareBoundedReadTest` / `AddToHomeSafBoundedReadTest` 需要 Android 依赖（`probe/shape3` 编译 rc=1，7 条 unresolved），只能由 CI 兜底；本轮 CI 绿即两条都通过。触发条件 = 沙箱侧要脱离 CI 单独验这两个门的形状断言时。
- **R8 `EXPECTED_OFF_MAIN_SCOPE` 是显式锚点**：新屏自动纳入（派生），但**改名/删文件**需要同步更新该清单，否则门红。触发条件 = 门报 `no longer pairs Compose state with an off-main block` 且确认为改名。

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 | `eea9f407f9e0e79f4902eeef1f84e1550ee5c29c` |
| 生产提交 | `99322a05` |
| 门提交 | `89c63058` |
| CI 绿提交（源码树） | `f182c76678aebaef8fafcb0d35ee2a896b50a47b` |
| 报告提交 | 本文件所在提交（其后只有 `fix-reports/` 改动 ⇒ 源码树仍等于 CI 绿树） |
| CI run | `40053301653`（#2594，attempt 1，`success`） |
| job id | `120750556417`（`success`） |
| `run.head_sha` | `f182c76678aebaef8fafcb0d35ee2a896b50a47b` |
| 结论 | 分支 CI 绿 = 确定性停止节点；**未合并 main**（起手权在用户） |

**红→绿的两轮记录（都是真错，无一次 flake）**

| 轮次 | run | 症状 | 根因 | 修法 |
|---|---|---|---|---|
| 1 | `40018363023`（#2590） | `compileReleaseKotlin` / KSP 报 `AddToHomeSheet.kt:260 Function declaration must have a name` | 改写 pin 块时旧 `withContext` 的收尾 `}` 残留在树里，KSP 把 sheet 后半段当成顶层声明 | `0e7c0228` 删掉多余大括号；同时用 kotlinc 对 10 个改动文件做**语法级**复检（只看 `Expecting` / `must have a name`，忽略依赖缺失）：0 条 |
| 2 | `40051225370`（#2593） | `testReleaseUnitTest` 2 failed：`SharedCacheExportPruneTest > provider export … FAILED` / `> mcp share … FAILED` | 源形状测试钉的是**调用拼写**：`contains("scope.launch { exportProviderInstance(")`；B-04 要让调用点消费返回值（`val launched = exportProviderInstance(…)`），拼写一变两臂即红——**改动本身是对的，判据是错的** | `f182c766` 改钉**形状**：`insideLaunchBlock(src, call)`（该调用落在某个 `launch { … }` 块内）；真值钉「启动而非内联」的原意，返回值消费由 `share_dir_prune_check` 的 arm6 C-SILENT 守 |

**L4 判读口径**（COMMON §3，逐条已在本轮日志上验过）：`run.head_sha` == 本地 HEAD == `git ls-remote`（分支名 `%2F` 编码）逐字符；26 步无 failure（唯一 skipped 只允许 `Publish to Releases`）；`Summary: 111 passed, 0 failed` 恰一条（本波 `scan.sh` 另打 8 条**分组** Summary，均为 `0 failed`）；`ALL 342 CASES PASS`；编译错 `Z e: ` 0；游离 FAILED 0；门头 `[1/111]…[111/111]` 连续且显示名互异；**警告对基线主 CI `40035181763`（job `120692773417`）按（文件, 文本）对齐：新增 0 / 减少 0，两侧各 191 条**。

判读脚本与原始产物：`probe/judge_b1.py`（14 项判据）+ `probe/warn_delta.py` + `probe/judge_green.txt` + `probe/job-120750556417.log` / `probe/job-baseline-main.log`。

## 8. 自证成本与不确定项

- **工时分布**：判据实现 ≈ 55%（4 个门 / 22 条新判据与臂）、生产改动 ≈ 20%、装置（PRE 树 / L3 / 真树变异 / 进程内跑法）≈ 25%。
- **不确定项 1（已收敛）**：本支生产改动全是 Compose/Android 面，沙箱**不能编译**。预判的两个风险点（`FilePreviewScreen` 的局部 `data class` 跨越 `withContext` 边界、`LogOpenResult` 的类型推断）**都没成为事故**——反倒是预判之外的一处**多余大括号**（改写残留）让首轮 KSP 直接失败。已按「先做语法级复检」补齐：`kotlinc` 只取 `Expecting` / `must have a name` 类错误（忽略依赖缺失）⇒ 10 个文件 0 条。局部类也顺手提升为文件私有 `ArchiveEntry`，去掉这一层不确定。
- **不确定项 2（已收敛）**：`SoulStore.load` 返回 `SoulFile?`（`agent/SoulStore.kt:390`），`?: SoulMDParser.parse(...)` 分支成立，CI 编译通过。
- **不确定项 3**：arm13 依赖「`Button(enabled = …)` 的 enabled 表达式里出现 `isPinning` 标识符」；改用 `pinActionEnabled(...)`（具名实参）后仍是标识符，判据成立；但若将来改成 `enabled = uiState.canPin`（ViewModel 派生），本判据会失去对象 ⇒ 届时按 R7 的口径改造。
- **证据可复跑清单**：`probe/measure_f_scope3.py`（F 作用域实测）· `probe/run_gates_inproc.py`（111 步里的 109 门）· `probe/step27.sh`（其余 2 步）· `probe/run_test_scan_inproc.py`（342 夹具）· `probe/pre.sh` + `probe/pre-logs/`（PRE 臂原始输出）· `probe/l3/run.sh`（kotlinc 6 用例 + 4 变异）· `probe/mutate_tree.py`（5 条真树变异）。

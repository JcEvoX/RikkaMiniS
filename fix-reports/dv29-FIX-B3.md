# dv29-FIX-B3 — 外部分享/打印生命周期 + 输入框双写漏斗（B-03 · B-05 · B-06）

基线 main `b82aa8dc` · 分支 `fix/dv29-b3-share-print-composer` · 3 提交 · CI run #2605。

## 1. 缺陷与真树取证（自己跑出的行号，不采信报告）

### B-03 六入口矩阵（先证）

| 入口 | context 来源 | 标志位置（修前） | catch 反馈（修前） |
|---|---|---|---|
| `ChatThinkingBadgeUI.kt:196-239`（exportCurrentChat） | `LocalContext`（Activity，经 `ChatScreen.kt:4890` 传入） | **chooser 无标志**（只给 chooser 挂了 GRANT_READ，NEW_TASK 全缺） | 有（Toast `export_progress_failed`） |
| `FilePreviewScreen.kt:1248-1268`（openExternally） | Activity | **内层有、chooser 无**（`Intent.createChooser(intent, "Open with…")` 裸调用） | 有（AppLogger + Toast `filepreview_no_app`） |
| `FilePreviewScreen.kt:1355-1372`（shareFile） | Activity | **chooser 无标志**（内层只有 GRANT_READ） | 有 |
| `BrowserDownloadsSheet.kt:237-263`（openDownloadedFile） | Activity | **内层有、chooser 无** | **仅 `Log.w`——用户不可见** |
| `ChatInputArea.kt:1448-1476`（附件打开） | Activity（compose 内联） | **全无**（直发 ACTION_VIEW，无 chooser） | 有但**硬编码英文**（"No app available to open this attachment."，不在 strings） |
| `WebAppActivity.kt:505-548`（shareWebAppFile） | Activity | chooser 有 NEW_TASK ✓ | **仅 `AppLogger.warning`——用户不可见** |
| `InlineMediaPlayer.kt:875-908`（shareMediaFile） | Activity | chooser 有 NEW_TASK ✓ | **`catch (_: Throwable) {}` 空吞** |

取证要点：chooser 不继承内层 flags ⇒ 4 处 chooser 裸启动；Activity context 虽不抛（`startActivity` 从 Activity 免 NEW_TASK），但该豁免在调用点不可见——同一 `openExternally(context, …)` 从 Service/Application 调用即抛 `AndroidRuntimeException`（Error 类，非 catchable）。**门盲区实证**：`share_dir_prune_check.py` arm6 的 `SHARE_FILES` 硬编码 3 文件（Provider/MCP/LogScreen），其余 13 个 `createChooser` 文件全部不受覆盖；基线树跑新判据 8 处红（上表 6 入口，BadgeUI 双计）。

### B-05 打印槽（先证）

`printWebView` 是模块级槽（KDoc 引文：「The one live reference … `createPrintDocumentAdapter` hands the framework an adapter that reads this WebView lazily for as long as the job is spooling」）；`releasePrintWebView()` 的 KDoc 写「Called on a load failure, **and at the start of the next print**」，而 `grep releasePrintWebView` 全文件仅 2 处调用点——**「下一次打印开始」从未发生**：`printFile` 直接 `printWebView = webView` 覆写。三个后果：①被替换视图的 `onPageFinished` 稍后仍 `printManager.print(...)` ⇒ **第二个打印弹窗**，且其 `releasePrintWebView(view)` 因恒等检查为假成 no-op ⇒ 弹窗与渲染器双双孤儿；②旧渲染器活到进程死；③退出预览屏零兜底。**printFile KDoc 与 release KDoc 是一对互相矛盾的文档**（一个说下次打印不销毁，一个说下次打印会调用）。

### B-06 双写漏斗（先证）

`commitComposerBody`（`:427-437`）同时写 `viewModel.setInputText(text)` **和** `inputFieldValue = TextFieldValue(text, selection=at)` 并 `lastTrueCaretEnd = at`——这就是 dv24/B-05 修「消息编辑预填丢 caret」的漏斗。6 处出口只走 VM 半边：

| 出口 | 位置 | 修前写法 | 走漏斗后 |
|---|---|---|---|
| ToggleSlashMenu | `:459`（修后 `:474`） | `viewModel.setInputText(next)` | `commitComposerBody(next, null)` |
| DismissSlashMenu（命令臂） | `:465`（修后 `:479`） | `viewModel.setInputText(viewModel.dismissSlashMenu(field))` | `commitComposerBody(…, null)` |
| 斜杠弹窗 onDismissRequest | `:752`（修后 `:768`） | 同上 | 同上 |
| executeSlashCommand | `:851`（修后 `:869`） | `viewModel.setInputText(viewModel.executeSlashCommand(…))` | `commitComposerBody(…, null)` |
| selectMention（点选） | `:1082`（修后 `:1104`） | `setInputText(newText, caretOverride = newCaret)` | `commitComposerBody(newText, newCaret)` |
| executeSelectedMention（回车） | `:1772`（修后 `:1798`） | 同上 | 同上 |

失败机制：>8 字符 IME 突发缓冲 150 ms ⇒ 窗口内 `inputFieldValue` **领先** `viewModel.inputText` ⇒ `setInputText(newText)` 其中 `newText == viewModel.inputText.value` 是**等值写**，`MutableStateFlow` 丢弃 ⇒ `LaunchedEffect(inputText)` 的 `!=` 守卫不触发 ⇒ 字段保留用户刚划掉的斜杠前缀；`caretOverride` 路径还会把 `_pendingCaret` 悬空给下一次无关输入。

## 2. 修法（改动阶梯）

- **B-03 停在第 2 级（抽共享 helper，任务书明示）**：新 `util/ExternalIntent.kt`（`internal object`，`start` / `share` 两个入口 + 私有 `launch`）——标志挂**在真正被启动的 intent 上**、`launch` 无条件 `AppLogger.warning`（logcat 可诊断）+ `onFailure` 回调给调用方上屏；`catch (Throwable)` 刻意宽于 `ActivityNotFoundException`（`SecurityException`/`RuntimeException` 都会逃出窄 catch）。调用方保留内容所有权（grant flag 留在各自 intent）。**文案零新增**：`filepreview_no_app` / `file_share_failed_toast` / `export_progress_failed` / `filepreview_open_externally` 在 7 语 strings 全部已存在（逐文件 grep = 7×3），复用即满足「7+1 语言同步」，零翻译零新文件。`openExternally` 的 chooser 标签从硬编码 "Open with…" 换成既有 `filepreview_open_externally`（顺手去一处英文字面量，标题文案变化已记 §6）。
- **B-05 第 5 级**：`releasePrintWebViewIfNotSpooling()` = `shouldReleasePrintSlot(hasSlot, slotSpooling)`（纯函数）——**只释放未 spool 的实例**（spool 中的视图被框架懒读，销毁 = 空白纸，这正是旧 KDoc 的顾虑）；`onPageFinished` 加恒等守卫 `printWebView !== view`（被替换实例 → `destroyPrintWebView(view)` + 返回，不再拉打印）；`FilePreviewScreen` 顶部加 `DisposableEffect(Unit)`，`onDispose` 调同一释放器。`printWebViewSpooling` 状态位随槽位置位/复位。**两条矛盾 KDoc 都改写**（ponytail 更新为「单槽，打印中不销毁」+ 升级触发不变）。
- **B-06 第 6 级**：`commitComposerBody` 签名 `(String, Int) -> Unit` → `(String, Int?) -> Unit`（`null` = 无 caret 意图，保 `lastTrueCaretEnd`，与镜像对 4 处出口的原语义完全一致）；caret 决策抽纯函数 `composerCommitCaret(caret, lastTrueCaretEnd, length)`（clamp 到 `[0, length]`，`length<=0` 归 0——shift-delete 可能把记住的 caret 留在替换文本之后，`TextRange` 会抛）。6 处出口统一走漏斗；`viewModel.setInputText` 的等值抑制**一字未动**；Toggle/execute 两处保留显式 `cancelPendingImeBurst()`（幂等，且是既有 E3 判据的锚）。

## 3. 改动清单

`git diff --stat b82aa8dc..HEAD`：13 文件 **+1008/−58**（生产 8：ExternalIntent.kt 新建 + ChatThinkingBadgeUI/FilePreviewScreen/BrowserDownloadsSheet/ChatInputArea/WebAppActivity/InlineMediaPlayer/MarkdownText；测试 2：FilePreviewPrintLifecycleTest.kt 新建 6 用例 + ComposerCommandHandoffTest +4 用例；门 3：share +356 / print +214 / chat_ui +145）。`scan.sh`/`test_scan.py` diff = 0 行（冻结面未动，机械核对）。

**半径外改动（1 处，已记录理由）**：`ui/markdown/MarkdownText.kt` 的空 catch 补一行 `AppLogger.warning`——不在任务书文件清单，但树派生判据要求它成立（基线树 C-FEEDBACK 红）、无行为变化、该文件无其它支认领。这是「让派生判据成立」的最小必要改动，不是顺带清扫。

## 4. 门与判据（三个都只扩，0 新建门）

- **`share_dir_prune_check.py`**：arm6 的 3 文件白名单保留（严格判据 C-NT/C-SILENT 原样），**新增派生面**：`derive_handoff_files()` 走 `src/android/app/src/main/java` 收「`Intent.createChooser(` 或（`startActivity(` ∧ `FLAG_GRANT_READ_URI_PERMISSION`）」的 .kt（POST 树 14 文件 / 7 chooser 站点 / 17 startActivity 站点，绿色运行打印统计行）；判据 C-NT（chooser 语句/`<chooser>.addFlags`/直发意图的声明区必须带 NEW_TASK；helper 返回的 chooser 跳过——C-NT 在构造点已覆盖）+ C-FEEDBACK（URI 交接的 try/catch 体非空且含 Toast/Snackbar/AppLogger/Log）。带牙 9 臂（compliant ×2 / precision ×2 / chooser 无标志 / 空 catch / 仅注释 catch / println catch / fail-closed 空树）+ 统计行。
- **`filepreview_print_bounded_check.py`**：新增 `check_print_slot`——槽位声明恰一次 / 释放器存在且 `printWebView = null` + `destroy()`（直接或经 `destroyPrintWebView(view)` helper，helper 体必须真销毁）/ 每个覆写点前 1200 字符内必须有 `releasePrintWebView*` 调用 / `onDispose` 必须调释放器 / `onPageFinished` 到 `printManager.print(` 之间必须有 `printWebView !== view`。真树 presence 检查放 `run()`（夹具源只到 buildPrintHtml，合法停在不判）。带牙 7 臂（clean + 6 变异）。
- **`chat_ui_interaction_check.py`**：新增 `composer_body_funnel_findings`（B06）——漏斗声明（`(String, Int?) -> Unit`）必须存在且体含 burst 取消（`BURST_DROP_RE`，接受内联 `imeBurstJob?.cancel()`，与 E3 的等价口径一致）+ VM 写 + 字段写；出口**从解析调用派生**（`dismissSlashMenu`/`executeSlashCommand`/`showSlashMenuOverInput`）+ 任何 `caretOverride` 写；每个出口所在块必须调漏斗且不得裸调 `setInputText`。带牙 5 臂（出口直写 / 漏斗丢字段写 / 漏斗保 burst / caret 标记旁路 / 漏斗声明没了）。夹具 67 → **72**。
- **PRE/POST 对照（判据带牙的独立证据）**：基线树（`git show b82aa8dc:` 源）跑新判据——share 8 红（= 6 入口，行号与上表一致）/ print 3 红（覆写不释放 + onDispose 缺失 + 无恒等守卫）/ chat_ui 7 红（5 直写 + 2 caretOverride）；POST 树三门全绿。
- **★ 派生集的自伤（已修）**：首版用门内共享 `strip_comments` 判文件归属——它对部分 Kotlin 形状**丢文本**（BrowserDownloadsSheet.kt 12352 B 剥完剩 9840 B，`createChooser` 全没）⇒ 派生集静默缩面（BrowserDownloads 不在集内）。修法 = 派生面改用保偏移剥离器 `_strip_comments_keep_offsets`（同时修正了行号：共享剥离器把 MainActivity 真实 :421 报成 :380）。
- 臂数只增不减：share 10→20、print 18→25、chat_ui 67→72 夹具；门 111 不变。

## 5. 验证四层

- **L1 本门真树**：三门 `python3 scripts/scan/<g>.py .` 全 rc=0（share「arm1..arm6 green」+ 统计行、print PASS ×2、chat_ui GATE OK）；三门 `--selftest` 全 PASS（SHARE_DIR_PRUNE / FILEPREVIEW_PRINT_BOUNDED / CHAT_UI_INTERACTION (72 fixtures)）；`scan.sh`/`test_scan.py` diff 0。
- **L2 邻门子集**：`gate_run.sh --subset` 因全局锁排队被取消（另一会话的全量 111 门 + test_scan 在跑，A4 会话随后又开全量）——**未跑成，缺口交给分支 CI**（CI 跑 scan.sh 全量 + test_scan 全量）。已跑成的邻门证据：`filepreview_print_bounded` 的 LogManagementScreen 判定 PASS（读边相邻）。
- **L3 沙箱 JVM 装置（`probe/l3/`）**：`extract.py` 从生产文件**按声明切片**两个纯函数（`shouldReleasePrintSlot`/`isCurrentPrintView`/`composerCommitCaret`）+ 真 `kotlinc`（/opt/kotlinc，2.x）编译切片 + `ComposerTextOps.kt` + **真实测试类两份** → `JUnitCore` **OK (21 tests)**（print 6 + composer 15，其中 11 条既有断言零改动）。变异 4 臂各红且**只红预期用例**：m1 谓词反转 3 红（真值表计数用例抓住）/ m2 恒等→相等 1 红（等值 peer 用例）/ m3 去 clamp 1 红 / m4 null 兜底改 0 1 红。**身份测试的自伤**：首版用 `StringBuilder`（不覆写 equals，`==` 也是引用比较）⇒ m2 不红——换 `mutableListOf`（等值不恒等）后 m2 红恰好 1。
- **L4 分支 CI**：run **#2605**（id `40070943088`，workflow build-apk.yml，dispatch @ `a183d691`，job `120804033167`）**success**，attempt 1，日志 1,463,186 B。判读（`probe/ci/judge.py`，API 取 head_sha/steps + 日志两跳下载）：
  - 26 步 `not_ok=[]`，唯一 `skipped = Publish to Releases`（main 门控，日志里不出现该行——只能从 steps API 取）；
  - 门头 `[1/111] … [111/111]` 连续且唯一（111 门，本批 0 新建）；
  - `Summary: 111 passed, 0 failed` 恰一条，其余 6 条步骤级 Summary（5/10/13/22/25/33 + 10）**全部 0 failed**；
  - `ALL 342 CASES PASS` 1 条（test_scan 夹具数不变）；
  - 编译错 0、本支文件警告 0、游离 FAILED 0；
  - 新用例逐条 PASSED：`FilePreviewPrintLifecycleTest` 6/6、`ComposerCommandHandoffTest` 15/15（含新增 4，既有 11 条语义未改）。
  - 源码树等价：`git diff --name-only a183d691 HEAD` = `fix-reports/dv29-FIX-B3.md` 一个文件（`.md` 不参与构建、不在 workflow paths 内）。
- **PRE 臂（JVM 侧不可行，门侧已做）**：新纯函数修前不存在 ⇒ 编译期红无意义；判据级 PRE = 基线树跑新门（见 §4）。

## 6. 残留与失真

- **真机行为级（等真机窗口）**：①无 share 目标设备上 4 入口出现本地化 Toast；②同一文件连续点两次打印不出现第二弹窗；③IME 突发窗口内划掉斜杠前缀不残留；④退出预览屏后打印相关 WebView 不驻留（需内存报告口径）。
- **刻意保留（ponytail 已更新）**：spool 期间再点打印，前一 WebView 无法安全销毁（留到 GC）——升级触发 = PrintManager 提供作业完成回调或内存报告；单槽模型本身不变。
- **chooser 标签文案变化**：`openExternally` 的 "Open with…" → `filepreview_open_externally`（"Open externally"），用户可见变化，无功能影响。
- **判据盲区（登记）**：helper 返回的 chooser（`LogManagementScreen:726` 经 `buildTextShareChooser` 返回）在 startActivity 站点无法静态解析——C-NT 在**构造点**已覆盖（其 apply 块带 NEW_TASK）；若有人把构造点也挪进更深 helper，C-NT 的 `<chooser>.addFlags` 同函数判据仍接得住。`_handoff_is_uri_handooff` 的 25 行回看窗口是启发式（chooser 构造距 startActivity 超过 25 行则漏判——C-NT 语句级判据仍覆盖 chooser 类）。
- **L2 邻门缺口**：锁排队未跑成（§5），由 CI 全量补。

## 7. 身份链

基线 main `b82aa8dc` → 4 提交：`c5f211bc`（生产 8 文件）/ `ed2405d0`（测试 2 文件）/ `a183d691`（门 3 文件）**← CI 绿于此** / `419ff840` + 本次回填（报告 `.md`）→ push `origin/fix/dv29-b3-share-print-composer` → CI run **#2605**（2026-10-24T06:37:36Z）success。三方 sha：本地 `a183d691` == `ls-remote` == `run.head_sha` = `a183d691a1c026cf40ef2aa27f6ce312979ed00f`。**停在分支，未合并 main。**`git diff --name-only` 三段各自独立（生产/测试/门不混）；worktree `probe/` 已 gitignore（`:141`），未入库。

## 8. 自证成本与不确定项

- **三门 + JVM + 变异 + PRE 全部本地可复现**：`probe/l3/{extract.py, arm.sh}`（每臂独立进程，S28 纪律——loop 式 shell 第 2 臂起全线 ENOSYS，本轮 m1 首跑即复现，改单臂进程后 4/4 通过）、`probe/balance.py`（括号配平，7 生产文件全平；ComposerCommandHandoffTest 的 -3 是检查器对字符串模板的已知误报，kotlinc 实编译通过）。
- **不确定**：`ExternalIntent` 的 `catch (Throwable)` 比修前窄 catch 宽——若某入口依赖「非 ActivityNotFound 异常向上抛」，现在会被吞成 log + false 返回；逐入口核对过：6 处修前 catch 要么 `Throwable`/`Exception` 全捕，要么只捕 ActivityNotFound（后者是 ChatInputArea，行为面 = 多捕了 Security/RuntimeException，恰好是本轮要防的逃逸）。
- **报告行号口径**：全部来自 POST 树（`git show`/sed 复核）；PRE 树行号在 §4 标注来源（基线树判据输出）。

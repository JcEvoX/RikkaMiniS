# FIX-B1 — 预览/导出三条守卫链（B-01 + B-02 + B-03，全 P1）

> 支：`fix/dv27-b1-preview-export-guards`　基线 main：`88ef9b27`
> worktree：`/var/minis/shared/work/deep-audit-1047/b1/`
> 独占门：`scripts/scan/filepreview_print_bounded_check.py`、`scripts/scan/discarded_return_guard.py`
> **未合并 main**（含 `.kt` 验证面，起手权在用户）

---

## 1. 身份与改动集

**身份链**：基线 `88ef9b27` → 生产提交 → 测试提交 → 门提交（CI 绿于此）→ 报告提交。

**文件集（9 改 + 4 新）**：

| 类别 | 文件 | 改动 |
|---|---|---|
| 生产 | `ui/components/FullscreenImageViewer.kt` | +50/−9 —— Q+ / pre-Q 保存相册、剪贴板复制、分享 三处 |
| 生产 | `ui/sandbox/FilePreviewScreen.kt` | +25/−9 —— SAF 另存为假成功、`PdfPreview` 键集 |
| 生产 | `ui/chat/ChatMiscViews.kt` | +14/−2 —— 表格图复制 |
| 生产 | `ui/chat/StreamingMarkdownText.kt` | +12/−2 —— 流式表格图复制 |
| 生产 | `webapp/AddToHomeSheet.kt` | +50/−9 —— 图标源流级上限 |
| 生产 | `ui/settings/BackupSettingsScreen.kt` | +58/−22 —— 备份导入流级上限 + query 下沉 IO |
| 生产（新） | `util/SaveOutcome.kt` | +46 —— Android 无关的成败判决 |
| 门 | `scripts/scan/discarded_return_guard.py` | +222 —— B-01 二进制写入判定段 |
| 门 | `scripts/scan/filepreview_print_bounded_check.py` | +368 —— C-SAF-1/2 + C-PDF-KEY |
| 测试（新） | `util/SaveOutcomeTest.kt` | 5 用例 |
| 测试（新） | `ui/sandbox/FilePreviewPdfKeyAndSaveTest.kt` | 5 用例 |
| 测试（新） | `webapp/AddToHomeSafBoundedReadTest.kt` | 3 用例 |
| 测试（改） | `util/BoundedDecodeCallSitesTest.kt` | +2/−2 —— 源码锚点随 B-02 改写同步 |

**门数不变（111）**：`scan.sh` / `test_scan.py` **零触碰**（本波新规：冻结面由主会话在并集阶段统一注册）。

---

## 2. 缺陷（真树取证）

### B-01 平台第二种失败通道未守卫（形状 17/32）

同一条形状的两种失败通道，**都不抛异常**：

1. `ContentResolver.openOutputStream(uri)` 返回 **null**（provider 拒绝写 / 进程被回收）→ `stream?.use { … }` 整段写盘被跳过；
2. `Bitmap.compress(...)` 返回 **false**（编码器什么都没写）→ 语句位置调用（`file.outputStream().use { bmp.compress(…) }`）里 `use` 的返回值无人读取，判决被**构造性地丢弃**。

| 站点 | 旧行为 |
|---|---|
| `FullscreenImageViewer.saveToGallery` Q+ 分支 | null 流 / false 压缩 ⇒ **不进 catch** ⇒ 孤儿 `IS_PENDING=1` 行被 `update(IS_PENDING=0)` **正式发布**成 0 字节条目，函数仍返回 `true` ⇒ `ImageGalleryViewer.kt:346` 弹「已保存到相册」 |
| 同上 pre-Q 分支 | false 压缩 ⇒ `Pictures/Minis/` 留 0 字节 png（画廊显示破图） |
| `FilePreviewScreen` SAF 另存为 | `openOutputStream(uri)?.use { copyTo }` 之后无条件 `true` ⇒ **假成功 Toast** |
| `copyBitmapToClipboard` / `shareImage` / `ChatMiscViews` 表格复制 / `StreamingMarkdownText` 流式表格复制 | `compress` 判决丢弃 ⇒ 0 字节 png 被包成 FileProvider URI 进剪贴板 / 分享面板 + 假成功 Toast |

### B-02 SAF 读取无流级上限 + 主线程 query（形状 16/20）

1. `AddToHomeSheet.loadBitmapFromUri`：`openInputStream(uri)?.use { it.readBytes() }` —— **整文件先进堆**，再交给 `ImageDecoding.decodeBytesBounded`。那个上限是**像素**上限（采样解码），对**字节**已经躺在堆里这件事无能为力 ⇒ 用户选几百 MB 图即 OOM。
2. `BackupSettingsScreen`：`contentResolver.query(...)`（跨进程 IPC）在 `scope.launch{}`（Main.immediate）内、`withContext(Dispatchers.IO)` 块**外**；拒绝条件只有 `OpenableColumns.SIZE != null && > MAX_PAYLOAD_BYTES` ⇒ provider 不报 size（或虚报）时直接 `it.readBytes()` 无上限。
3. 门盲区：`_saf_classify` 把「**声明大小预检**」判成 bounded —— 流本身无界也算过；`SAF_UI_DIR` 硬编码 `…/app/ui`，`webapp/` 整个目录没被扫过。

### B-03 `PdfPreview` 每页 `produceState` 漏绑数据源键（形状 22）

```kotlin
val page by produceState<Bitmap?>(initialValue = renderer.cached(idx), key1 = idx) { … }
```

顶层 `error` / `pageCount` / `renderer` 全都 `remember(item.file)`，唯独页级状态只以 `idx` 为键，且有 `if (value == null)` 门控 ⇒ Compose 复用槽位换 PDF 时，`idx` 不变 ⇒ 生产者不重跑 **且** 旧位图被当成「已加载」⇒ 上一份文档的页留在屏上。

---

## 3. 修法

### B-01（第 2 级：两条失败通道并入同一条失败路径）

新增 `util/SaveOutcome.kt`：

```kotlin
internal fun saveOutcome(streamOpened: Boolean, compressed: Boolean): Boolean =
    streamOpened && compressed
```

**为什么是纯函数而不是每站点两个 `?: return false`**：两个半边不对称（有的站点流一定开、有的压缩一定发生），把它们写成一个「合取」才有可测的语义，而 Android 无关的合取可以钉在 JVM 用例上。调用点仍各自持有 Context / File 管道。

- **Q+ 保存**：`openOutputStream(uri) ?: throw IOException(...)`；`out.use { if (!bitmap.compress(…)) throw IOException("compress failed") }` ⇒ 两条通道都进既有 `catch` ⇒ `[audit-0917]` 的 `contentResolver.delete(uri)` 生效 ⇒ **孤儿行不再被发布**（`IS_PENDING` 只在成功路径被置 0）。
- **pre-Q**：false 压缩 ⇒ `file.delete()` + 抛（与 Q+ 的删除对称）。
- **SAF 另存为**：`} ?: throw java.io.IOException("openOutputStream returned null")` ⇒ null 流进同一个 catch ⇒ 失败 Toast，不再是假成功。
- **4 处 `compress`**：`val encoded = …use { bmp.compress(…) }` → `if (!saveOutcome(streamOpened = true, compressed = encoded)) { file.delete(); throw IOException("png encode failed") }` ⇒ 失败文案走**既有**键（`image_copy_failed_toast` / `markdown_table_image_copy_failed_toast`），**未新增语言键**。

**保住的契约**：`pruneShareDir(shareDir)` 调用位置不变（`share_dir_prune_check.py` 钉死）；Toast 线程调度不变（Main）；`[audit-0917]` 删除契约不变。

### B-02（第 2 级：复用既有单源 `readAtMost`）

- **`AddToHomeSheet`**：`readAtMost(input, MAX_ICON_SOURCE_BYTES)`（流级上限，**读一个字节就停**）→ `ImageDecoding.decodeBytesBounded(bytes, maxEdge)`。上限取 **32 MiB** = 仓库既有的「最大的裸用户文件仍接受」值（`FileReadTool.MAX_INPUT_BYTES` / `MAX_SKILL_IMPORT_BYTES`），**不是凭空造的 16MB**；解码侧的 `ICON_MAX_EDGE_PX = 192` 不动（像素与字节两个上限各管一件事）。
- **`BackupSettingsScreen`**：`query` 下沉到**同一个** `withContext(Dispatchers.IO)` 块内；读字节改 `readSafBytes(resolver, uri, ConfigBackup.MAX_PAYLOAD_BYTES.toLong())` ⇒ 声明大小预检**保留**为廉价早拒，但**流本身**也封顶（`SafRead.TooLarge` → 既有 `backup_import_too_large` 文案，`SafRead.Failed` → `errRead`）。原来的第二个读点（`openInputStream(uri)?.use { it.readBytes() }`）**删掉**，只留一个读点。
- **门**：`SAF_SCAN_DIRS` 改为派生（`ui/` + `webapp/`），并新增 **C-SAF-1 覆盖断言**（带站点的文件必须在扫描域内）；`_saf_classify` 删掉「declared-size precheck ⇒ bounded」这条豁免，**只有带上限常量的流级读取**才判 bounded。

### B-03（第 6 级：键集与取值两行）

```kotlin
val page by produceState<Bitmap?>(
    initialValue = renderer.cached(idx),
    key1 = idx,
    key2 = item.file,
) { if (value == null) { value = withContext(Dispatchers.IO) { renderer.render(idx) } } }
```

**保住**：LRU 复用（`initialValue` 仍先取 cache）、IO 线程渲染、顶层三个 `remember(item.file)`。
**门的判据派生**：`REMEMBER_ANCHORS` 不再手写两行 —— 主体集 = 真树里所有 `private fun *Preview(item: FileItem)`；每个 `produceState` 调用判「key 集必须含 `item.file` / `renderer` / `item`」，key 集从**命名实参**读。

---

## 4. 门与判据

### `discarded_return_guard.py`（新增段：**二进制写入判定**）

- **B1**：当压缩出的字节**离开进程**（所在函数触及 `FileProvider.getUriForFile(` / `MediaStore.` / `contentResolver.openOutputStream(`）时，判决必须经 `saveOutcome(` 路由。**目的地谓词**排除了「压进内存 `ByteArrayOutputStream`」这一类（模型图像预算 / 截图环 / debug dump）—— 那类不发布、调用方自己的长度检查看得见空数组。
- **B2**：每个 `openOutputStream(` 站点必须以 elvis 退出（`?: throw|return|error`）或 `requireNotNull`，不许 `?.use` 吞掉 null。**全树**判据。
- selftest **4 臂**（合规 / 两条通道同时错 / 内存目的地必须绿 / 只错 B2 时只红 B2），收尾标记 `binary-write self-test has teeth`。

### `filepreview_print_bounded_check.py`

- **C-SAF-1（作用域派生）**：带站点的文件必须在扫描域内 ⇒ 把域缩回 `ui/` 会红（**这是本条判据存在的理由**）。
- **C-SAF-2（流级上限）**：仅声明大小预检 ⇒ 红。
- **C-PDF-KEY（派生）**：每个 `produceState` 的 key 集必须含其数据源。
- selftest 新增 **3 + 2 臂**（webapp 无界读 / 域缩回 `ui/` / `key2` 去掉 / preview 主体消失须 fail-closed）。

**PRE 臂**（`git show 88ef9b27:` 抽 12 个生产文件到独立树）：两门在其上**红** —— discarded 门红 2 处（B-01 的 `compress` 判决丢弃 ×1、`openOutputStream` 吞 null ×1），filepreview 门红 5 处（B-02 webapp 无界读、BackupSettings 无界读、`PdfPreview` 键集缺源、`AddToHomeSheet` 无上限、`BackupSettingsScreen` 无上限）。

---

## 5. 验证四层

### L1 门与夹具

- `filepreview_print_bounded_check.py .` ⇒ **rc=0**（真树：FilePreviewScreen / LogManagementScreen / SafImportRead 全 PASS，`AddToHomeSheet` 两条 bounded 判定登记在册）
- `discarded_return_guard.py .` ⇒ **rc=0**（五段全绿，含新的 binary-write 段）
- `filepreview_print_bounded_check.py --selftest` ⇒ `FILEPREVIEW_PRINT_BOUNDED_SELFTEST=PASS`（含 3 条新臂）
- `discarded_return_guard.py --self-test` ⇒ 4 臂 + `binary-write self-test has teeth`
- **PRE 臂**：基线树两门红（B-01 两处 + B-02 三处 + B-03 一处）
- `sh scripts/scan/scan.sh .` ⇒ **`Summary: 111 passed, 0 failed`**（111 门头，ENOSYS 0 行）
- `python3 scripts/scan/test_scan.py` ⇒ **`✅ ALL 340 CASES PASS`**（首次跑撞沙箱 S28 `OSError: [Errno 38] Function not implemented`，静置后重跑取得干净读数；**这是沙箱缺陷，不是假红**）

### L2 概念邻门 **9/9 rc=0**

`browser_destroy_wakeup_check` / `file_preview_url_encode_check` / `webview_holder_url_source_check` / `webapp_asset_url_cleanup_check` / `backup_streaming_export_check` / `readonly_mount_write_guard` / `provider_remember_key_check` / `chat_ui_interaction_check`（B4 独占，只读）/ `share_dir_prune_check`（既有锚点只读）

### L3 真 kotlinc 2.0.21 + JUnit

**臂 1（行为面 · 真函数）**：`SaveOutcomeTest` **OK (5 tests)** —— 真编译真跑 `saveOutcome`，期望值全是**字面量**（不从被测常量推导）。

**臂 2（源码锚点面）**：`FilePreviewPdfKeyAndSaveTest` + `AddToHomeSafBoundedReadTest` + 既有 `BoundedDecodeCallSitesTest` ⇒ **OK (13 + 3 tests)**。

**臂 3（真树变异）** —— 每个变异**恰好红一条判据**，源文件字节不动（变异施加在 `probe/l3/mut*` 的副本上）：

| 臂 | 变异 | 结果 |
|---|---|---|
| m0 | 现状 | 两门 clean |
| m1 | `PdfPreview` 去掉 `key2 = item.file` | filepreview 红 1 条（C-PDF-KEY），discarded clean |
| m2 | `AddToHomeSheet` 改回 `it.readBytes()` | filepreview 红 1 条（C-SAF-2） |
| m3 | `BackupSettingsScreen` 改回「声明大小 + readBytes」 | filepreview 红 1 条（C-SAF-2） |
| n1 | 剪贴板 `compress` 改回语句位置 | discarded 红 1 条（**B1**） |
| n2 | SAF 另存为去掉 elvis | discarded 红 1 条（**B2**） |

**降级路径（写明理由）**：`FullscreenImageViewer` / `FilePreviewScreen` / `AddToHomeSheet` / `BackupSettingsScreen` 是 Compose + Android 代码，沙箱**无 android.jar、无 Compose 编译器插件**，不能直编。因此：
- 判决逻辑（`saveOutcome`）抽成 Android 无关纯函数 ⇒ **真跑**；
- 接线正确性由「**门 + 真树变异**」承担（上面 5 个臂）；
- 源码形状由「源码锚点测试 + 负控复原」承担（每个锚点测试都带 pre-fix 形态的**负控**，负控不被拒即失败）。

### L4 CI

见 §6。

---

## 6. 报告与登记

- 报告：`fix-reports/dv27-FIX-B1.md`（本文）
- backlog：`## §1047-B1`（真机欠账 + 文案复用决定 + 豁免复核）
- **未合并 main**（含 `.kt` 验证面，起手权在用户）

---

## 7. 残留（各带触发条件 + 触发后方案 + 搁置理由）

- **R1（P1，真机）** ①保存相册失败（provider 拒写 / 编码失败）时**不产生 0 字节条目、不弹「已保存到相册」**；②剪贴板/分享失败时**不粘贴 0 字节 png**；③选 500MB 图**不 OOM**；④备份导入一个 `SIZE` 为 null 的超大文件**报「文件过大」而不是 OOM**；⑤同一槽位连续打开两份 PDF **不串页**。**触发条件** = 下次真机窗口（与 `§1046-VERIFY` 一次开齐）。**触发后方案** = 按 ①→⑤ 顺序；④ 必须用 provider 不报 size 的文件（普通文件管理器都会报 size ⇒ 用 `content://` 手写 provider 或 ADB 推的虚拟文档）。**搁置理由** = 沙箱无 Android 运行时（MediaStore / ContentResolver / Compose 重组不可模拟）。
- **R2（P2）失败文案复用决定**：4 处 `compress` 站点复用**既有**键 `image_copy_failed_toast`（带 `%1$s` 错误串）/ `markdown_table_image_copy_failed_toast`（无参）——**未新增语言键**，7+1 语言文件零改动。**触发条件** = 未来要给「保存相册失败」一个独立文案。**触发后方案** = 新增 `save_to_gallery_failed_toast` 并同步 8 个语言文件。**搁置理由** = 现文案已能表达失败，且新增语言键的成本是 8 个文件的同步风险（跨层完整性纪律）。
- **R3（P2）`SAF_EXEMPTIONS` 剩余豁免复核**：现存豁免（`ChatPromptAndTools` 聊天图片附件的无上限读）**本波刻意不修**（FIX-11 半径，见门输出里的 `[B-07 exempt]` 登记），触发条件 = 出现「多百 MB 图片分享导致 OOM」的报告。**触发后方案** = 按本段形状修（`readAtMost` + 上限常量 + 门豁免转 bounded）。**搁置理由** = 超出本支半径，且它有独立的历史豁免登记。
- **R4（P3，刻意）pre-Q 分支不删行只删文件**：Q+ 有 MediaStore 行可删，pre-Q 只有文件可删，两者语义不同（「删行」vs「删文件」）。**触发条件** = 要求两者完全对称。**触发后方案** = 统一成「产出物删除」抽象。**搁置理由** = pre-Q 上 `Pictures/Minis/` 的 0 字节文件在画廊里不可见（无 MediaStore 行），危害低于 Q+。
- **R5（P2，门）`_saf_classify` 的「声明大小预检」已从「bounded」降级为「不充分」**，但**没有**把它变成一条独立的红判据（`BackupSettingsScreen` 现在两条都有）。**触发条件** = 出现第三个「只做声明预检」的站点。**触发后方案** = 加判据「声明预检必须与流级上限同时出现」。**搁置理由** = C-SAF-2 已经把这类站点判红（流级上限缺失即红），再加一条会重复。
- **R6（P3，工具）`_function_bodies` 的表达式体函数跳步**：`discarded_return_guard.py` 的 `_function_bodies` 以前在表达式体函数后 `i = j + 1`，会把**下一个**声明整条跳过 ⇒ 它后面的函数没有归属体，判据只能落到「整个文件」这个粗粒度上 ⇒ 内存目的地的压缩被误判成「出站写入」。本波已修（`i = j`），但同类跳步逻辑在别的门里可能还在。**触发条件** = 某个门报出「file top level」这种归属异常。**触发后方案** = 逐门审计 `_function_bodies` 的调用方。**搁置理由** = 已在本门修掉；跨门普查超出本支半径。
- **R7（P3）`blank_literals` 只处理 `"` 与 `'` 两种字面量**，raw string（`"""`）只做了不精确的处理。**触发条件** = 某个 Kotlin 文件的 raw string 里含有未转义的 `{`/`}` 且影响本门判据。**触发后方案** = 补 raw string 的完整词法。**搁置理由** = 本仓 raw string 内无影响本门判据的花括号。

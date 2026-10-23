# FIX-A4 — 导出附件计数失配（A-04）+ FileEditTool 缺编码守卫（A-08）

> 分支 `fix/dv27-a4-exporter-toolguards`　基线 main `88ef9b27`　提交 `c5fb19c9`
> 门：`parts_json_funnel_check.py`（R11 / R12）· `tool_title_error_branch_check.py`（C-EDIT-ENC / C-EDIT-BYTES）
> 冻结面 `scan.sh` / `test_scan.py` **零触碰**；门数 111、夹具 340 均不变（0 新建门）。

---

## 1. 缺陷本体

### 1.1 A-04（P1）导出附件计数恒为 0

`share/ChatExporter.kt` 的 `countAttachments(partsJson)` 只按 part **名字**分派，比对的是
迁移前的旧名（`"image"` / `"image_url"` / `"video"` / `"video_url"`）。

而今天落库的每一行 parts_json 携带的是 `data/model/ContentPart.kt` 的 `@SerialName`：

- `"mediaRef"`（`value` 为 `MediaRef`：`id` / `relativePath` / `mimeType` / `originalFileName` / `linuxPath`）；
- `"toolResult"`，其 `value` 可带一个 `mediaRef`（`ToolResult.mediaRef`）。

旧名一个都不匹配 ⇒ `Summary.imageAttachments` / `videoAttachments` 与导出 `session.json`
里的 `image_attachments` / `video_attachments` **恒为 0**：计数器存在、按批累加、写进 zip，
但从来没有一次被加过。

已对齐的兄弟读者（口径参照）：`ChatRepository.kt:683`、`:1060`、`ConversationHistoryTool:122`、
`ChatDebugMethods.kt:175`、`ChatViewModelMessageParser.kt:86`、`ChatTranscriptRebuild.kt:65`。

### 1.2 A-08（P1）`file_edit` 无编码守卫 + 字节数口径错

`FileWriteTool` 有未配对代理项守卫，`FileEditTool` 没有。于是 `file_edit` 写入的内容被 JVM
编码器**静默替换**成 `?`（`String.toByteArray(UTF_8)` 不抛异常，只替换），文件带着乱码落盘，
同时 `AtomicFileWrite` 自己的期望字节数检查打出「字节数对不上」的告警。

更糟的是：**旧的守卫本身不可达**——它是对 `content.toByteArray(UTF_8)` 的 try/catch，
而该调用永不抛异常。它读起来像一道校验，实际上什么也没拦。

字节数口径：`newContent.length` 是 UTF-16 码元数，CJK 场景最多偏小 3 倍，却标注为 `bytes`。

---

## 2. 修法

### 2.1 A-04（第 2 级：单点分支补齐，保留旧兼容）

抽出 Android 无关的纯函数 `share/ChatPartsAttachmentCount.kt`：

- `countPartsAttachments(arr: JSONArray): Pair<Int, Int>`
- `isVideoMediaRef(media: JSONObject?): Boolean`

分类口径与兄弟读者一致：**只有 `video/` 前缀算视频，其余（含 MIME 缺失/无法识别）算图片**——
这正是 `ChatRepository.extractTextPreview` 的默认归类。

旧名 `"image"` / `"image_url"` / `"video"` / `"video_url"` **全部保留**（迁移前的行必须继续计数）。

`ChatExporter.countAttachments` 现在只负责解析 + 委托；`catch` 兜底 `0 to 0`、
`exportToZip` 内 `withContext(Dispatchers.IO)` / `File(context.cacheDir, "shared")` /
`pruneShareDir(` / `ZipOutputStream(` 的相对顺序（`share_dir_prune_check.py:291-312` 钉死）、
JSON 导出保留 `partsJson` 原文 —— 一律未动。

### 2.2 A-08（第 2 级：镜像孪生工具，共享一份守卫）

新增 `tools/UnencodableContentGuard.kt`：

- `isUnpairedSurrogateFailure(content: String): Boolean` —— 用
  `CodingErrorAction.REPORT` 配置的 UTF-8 编码器，**真正**拒绝未配对代理项；
- `UNENCODABLE_CONTENT_MESSAGE` —— 两工具共用的拒绝文案（逐字相同）。

`FileWriteTool` 与 `FileEditTool` 现在都走这一个 helper（不是第三份内联副本）。
`FileEditTool` 的成功文案改报 `file.length()`（UTF-8 字节）。

---

## 3. 门与判据

### 3.1 `parts_json_funnel_check.py`

- **R11 / C-EXP**：每个「按 part 名区分 image vs video」的 parts_json 读取面，
  **必须在自己的分派臂上**出现 `"mediaRef"`。
  - 判据锚「分派点」而非「字符串出现」——只在 `toolResult` 臂里做
    `value.optJSONObject("mediaRef")` 的读取面**提到**了那个名字，却从未分类任何 mediaRef 行，
    子串判据会放它过关，而它统计的正是零。
  - 派生面：按「分派 `type` + 区分 image/video」从真树推导，不写死函数名单。
- **R12 / C-EXP-NESTED**：分派了 `"toolResult"` 的媒体统计面，**必须读那个臂里的嵌套 mediaRef**。
  - R11 只看顶层分派臂；没有 R12，一个面可以满足 R11 却把工具产出的每一张截图都丢掉。
  - 判据锚「臂的范围」：臂外的 mediaRef 读取不算数（那是另一个臂的分类逻辑）。

带牙臂：删 `"mediaRef"` 分派臂 ⇒ 只红 R11；删 `toolResult` 臂内的嵌套读取 ⇒ 只红 R12；
mediaRef 读取挪到臂外 ⇒ 红 R12；第二个独立读取面也丢掉现代名 ⇒ 判据红在**那个**函数上并指名。

### 3.2 `tool_title_error_branch_check.py`

- **C-EDIT-ENC**：写盘调用之前必须存在 UTF-8 有效性守卫，**且该守卫对同一份内容生效**。
  - 守卫的认定锚在 `onMalformedInput(CodingErrorAction.REPORT)` 这个**配置**上，
    而不是「函数体里出现过 `CodingErrorAction.REPORT` 这个词」——只 REPORT 不可映射字符、
    对 malformed 字符 REPLACE 的编码器，仍会把要拦的代理项替换成 `?`，词面判据会放它过关。
  - 守卫 helper 的认定来自**函数体**，不认函数名；允许一跳委托；注释在判定前被剥除。
- **C-EDIT-BYTES**：上报的 `bytes` 必须来自写入文件的 `file.length()`，不得来自
  `String.length` / `.count()`。

带牙臂：删守卫 ⇒ 只红 C-EDIT-ENC；守卫作用于另一份内容 ⇒ 只红 C-EDIT-ENC；
`file.length()` 改回 `newContent.length` ⇒ 只红 C-EDIT-BYTES；改 `.count()` ⇒ 只红 C-EDIT-BYTES；
守卫委托给**另一个名字**的 helper（仍配置 REPORT）⇒ 保持绿（判据认配置不认名字）；
KDoc 里只提到 `CodingErrorAction.REPORT` ⇒ 红（注释被剥除）。

---

## 4. 验证四层

### L1（门 + 全量）

| 项 | 结果 |
|---|---|
| `parts_json_funnel_check.py .` | PASS（R11 PASS · R12 PASS） |
| `parts_json_funnel_check.py --selftest` | `PARTSJSON_FUNNEL_SELFTEST=PASS`（含 4 条新臂） |
| `tool_title_error_branch_check.py .` | `OK (7 files)` rc=0 |
| `tool_title_error_branch_check.py --selftest` | `TOOL_TITLE_ERROR_BRANCH_SELFTEST=PASS`（含 7 条 A-08 臂） |
| PRE 臂（基线树 `88ef9b27` 完整 `src/main`，625 个 .kt） | 两门**精确红**：R11 FAIL（1/1 面只认旧名）· C-EDIT-ENC ×2（FileWriteTool + FileEditTool） |
| `scripts/scan/scan.sh` 全量 | `Summary: 111 passed, 0 failed` |
| `scripts/scan/test_scan.py` | `✅ ALL 340 CASES PASS` |

### L2（概念邻门，10/10 rc=0）

`share_dir_prune_check` · `readonly_mount_write_guard` · `tool_result_image_branch_check` ·
`offload_cli_args_check` · `file_uri_parse_guard_check` · `downloads_atomic_update_check` ·
`file_provider_paths_coverage_check` · `provider_write_rules_check` ·
`skill_file_save_result_check` · `file_preview_url_encode_check`

> 任务书点名的 `atomic_write_check` 在真树**不存在**（最近的真实门是
> `downloads_atomic_update_check`）；`file_uri_parse_guard` 的真名带 `_check` 后缀。

### L3（真 kotlinc + JUnit）

- 编译：`kotlinc` 2.0.21，2 生产文件 + 2 测试文件，rc=0。
- `ChatPartsAttachmentCountTest` → **OK (17 tests)**；
  `UnencodableContentGuardTest` → **OK (9 tests)**。
- 期望值全部**手写字面量**，不从被测实现推导。
- 变异臂（真树副本，逐臂跑门，各恰红其判据）：

| 臂 | 变异 | 红 |
|---|---|---|
| m1 | 删 `"mediaRef"` 分派臂 | R11 |
| m2 | 删 `FileEditTool` 的编码守卫 | C-EDIT-ENC |
| m3 | `file.length()` → `newContent.length()` | C-EDIT-BYTES |
| m4 | `onMalformedInput(REPORT)` → `REPLACE` | C-EDIT-ENC |
| m5 | 删 `toolResult` 臂内的嵌套 mediaRef 读取 | R12 |

### L4（CI）

- run `39997488480` / job `120564713152`，`head_sha = c5fb19c9`（与本地逐字符一致）。
- 结论见 §5。

---

## 5. CI

- run `39997488480` / job `120564713152` — **completed success**，attempt 1。
- `head_sha = c5fb19c9`（三方逐字符一致：本地 HEAD · `ls-remote` · `run.head_sha`）。
- 26 步中**唯一 skipped = 步骤 23 `Publish to Releases`**（main 门控，分支必然 skip），
  其余全部 `success`；**失败步骤 0**。
- 关键步骤：`Run pre-build scan gate` success · `Run unit tests (full suite)` success ·
  `Compile instrumented tests` success · `Build APK with Gradle` success ·
  `Verify APK signature` / `Verify APK contents` success · `Upload APK Artifact` success。

> 三源取二满足：①CI 绿（steps API 结论层）②L1 本地同树全量 111 门 + 340 夹具
> ③L3 沙箱真 kotlinc + JUnit（17 + 9 用例）。三条是**互相独立**的证据链。

---

## 6. 判据被自身变异抓出（三处，本轮最值钱的部分）

这三处不是「测试写错」，是**门的判据本身有洞**，由变异臂当场暴露：

1. **R11 用子串判「提到了 mediaRef」** ⇒ m1（删掉整个 mediaRef 分派臂）仍绿。
   根因：`toolResult` 臂里的 `optJSONObject("mediaRef")` 恰好满足子串检查。
   修法：判据锚**分派臂**（同行有 `->` 或 `==`）。
2. **C-EDIT-ENC 用词面判「出现过 `CodingErrorAction.REPORT`」** ⇒ m4
   （malformed 改成 REPLACE、unmappable 仍是 REPORT）仍绿。
   根因：另一处 `onUnmappableCharacter(REPORT)` 满足了词面检查。
   修法：判据锚 `onMalformedInput(CodingErrorAction.REPORT)` 这个**配置**。
3. **R11 只看顶层分派臂** ⇒ m5（删掉 `toolResult` 臂内的嵌套读取）仍绿。
   根因：工具产出的媒体被整类漏计，与 A-04 同型但换了入口。
   修法：新增 R12，锚**臂的范围**。

**可复用结论**：判据的「锚点」必须落在**决定点**上，不能落在「提及点」上。
一个判据如果只问「有没有出现过某个词」，它必然会被「在别处恰好出现同一个词」骗过。

---

## 7. 环境坑（本轮踩到）

- **Kotlin 块注释可嵌套**：KDoc 里写 `` `video/*` `` 会开启一个内层注释，导致外层 KDoc
  提前闭合 ⇒ kotlinc 报 `Unclosed comment`（行号指向文件末尾，极难定位）。
  修法：KDoc 里不写 `/*` 序列。
- **JUnit classpath 缺 kotlin-stdlib**：`java -cp ... org.junit.runner.JUnitCore` 报
  `NoClassDefFoundError: kotlin/jvm/internal/Intrinsics` ⇒ 必须把
  `/opt/kotlinc/lib/kotlin-stdlib.jar` 加进 classpath。
- **PRE 基线树不能放在 worktree 内**：门按 `root/src/android/app/src/main/**` 扫描，
  把基线副本放在 `probe/base/` 下会被当成生产文件扫进去，真树因此变红（假红）。
  修法：基线树放到 worktree **之外**。
- **PRE 臂必须是完整 `src/main`**：只抽 3 个文件会让门因「缺文件 fail-closed」而红，
  那不是判据本身红。修法：`git archive 88ef9b27 src/android/app/src/main/java | tar -x`。
- **装置目录被沙箱回收**：`probe/` 在 worktree 内曾被清空；重装置时改放
  `/var/minis/shared/work/deep-audit-1047/` 下的同级目录。

---

## 8. 残留（登记在 backlog §1047-A4）

- R1 真机行为级（导出含图会话后 `session.json` 的计数非 0；`file_edit` 写入含未配对
  代理项的内容时**报错且不落盘**，而不是落盘乱码）。
- R2 `countAttachments` 对未知 MIME 的归类口径（当前：非 `video/` 一律算图片，
  与 `ChatRepository.extractTextPreview` 对齐）。
- R3 DESIGN-06 的 `FileEditTool` bytes 半条已销（另两条——空文件 `showing 1-0 of 0`、
  ReadImage 缩放前尺寸——仍登记在案）。
- R4 R11/R12 的派生面都靠「按 part 名区分 image/video」这一不变式；哪天出现一个
  **不按 part 名**区分媒体类型的新读取面，它不会被这两条判据覆盖。

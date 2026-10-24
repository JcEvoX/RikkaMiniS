# dv28-FIX-A2 — 位图编码返回值无人消费（A-02 + B-02，形状 40）

- 支：a2　分支：`fix/dv28-a2-compress-verdict`　基线：`eea9f407`
- 工单：A-02（P1）+ B-02（P1）——A 面 offload/tool 落盘，B 面 UI 预览/截图
- 停在第 2 级（复用既有 `util/saveOutcome`），因为：编码失败语义早已存在（`SaveOutcome.kt:15` 的 KDoc 逐字写着这个坑），本缺的唯一缺口是「返回值没人读」，抽新函数只会多一层壳。

---

## 1. 缺陷与真树取证

`Bitmap.compress(...)` 在编码器没写出任何字节时**返回 `false`，不抛异常**。处于语句位置的调用点把返回值丢弃 ⇒ 调用方无法区分「真图片」与「空文件」，而每条链路都按成功往下走。

真树实测（`grep -rn "\.compress(" src/android/app/src/main/`，剥注释后逐点判父级语法）：

| # | 站点（真树行） | 位置形态 | 丢弃后的实际后果 |
|---|---|---|---|
| 1 | `sandbox/offload/PhotosOffloadHandler.kt:741`（`copyResized`） | 语句位置，写 `dst.outputStream()` | 0 字节文件落进相册/导出目录，`export` 仍答 `size_bytes: 0` + 成功 |
| 2 | `sandbox/offload/AccessibilityOffloadHandler.kt:459`（`uiScreenshot`） | 语句位置，写 `baos` | 空 base64 图像载荷，`success=true` |
| 3 | `tools/ReadImageTool.kt:100`（`execute`） | 语句位置，写临时文件 | 0 字节 `image/jpeg` 交给模型，工具报成功 |
| 4 | `browser/BrowserUseManager.kt:1422`（`screenshot` 内 resize 段） | 语句位置，写文件 | 截图证据链断开：base64 与 `imageFilePath` 都指向空 |
| 5 | `browser/BrowserUseManager.kt:1541`（`saveBitmapToFile`） | 语句位置，写文件 | 同上；路径被发布给 agent |
| 6 | `ui/chat/ChatPromptAndTools.kt:754`（`resizeJpegToMaxEdge`） | 语句位置，返回 ByteArray | 空图上传给模型 |
| 7 | `ui/chat/ChatPromptAndTools.kt:1536`（`resizeImageBytes`） | 语句位置，返回 ByteArray | 同上 |

半径外的第 8 处：`provider/ImageBudget.kt:110`（`compressBytes`）——**实测它同样丢弃判定**（见 §6 失真 D1），但票据明令「不在本条半径内不许动」，本轮只加 `record-ok:` 说明并登记 §1048-A2 R2。另有两条 debug 通道（`debug/DebugRPCHandler.kt:241`、`debug/DebugScreenshotRing.kt:65`）同样丢弃，同属半径外，`record-ok:` + R5。

**门盲区的真树取证**：`discarded_return_guard.py` 改动前只认三种「字节出网」token（`FileProvider.getUriForFile(` / `MediaStore.` / `contentResolver.openOutputStream(`），且在**最内层函数体**里找 ⇒ ①「包一层写文件」的 `saveBitmapToFile`、②「返回 ByteArray 给上层」的两个 `resize*`、③直接 `compress` 到 `baos` 的 a11y 通道，全部漏扫。PRE 臂实测（§5 L1）在基线树上原本 0 命中。

---

## 2. 修法

全部按「编码判定 → 既有失败出口」路由，**没有新造一套失败语义**：

| 站点 | 失败出口（既有形状） |
|---|---|
| `copyResized` | `throw java.io.IOException(...)` —— 该 helper 本来就用 IOException 报失败（`copyStream` 同形），`export` 调用方 `catch` 后答 `export_failed` |
| `uiScreenshot` | `return err(args, "ENCODE_FAILED", "...")` —— 用本文件既有的 `err()`（`:1176`） |
| `ReadImageTool.execute` | `return ToolExecutionResult("Error: Cannot encode image: $path", false, toolTitle = toolTitle)` —— 与同文件其他失败分支同形 |
| `screenshot`（resize 段） | `return BrowserActionResult.error("Failed to encode screenshot (JPEG encoder wrote no bytes)")`，先 `bitmap.recycle()` |
| `saveBitmapToFile` | 返回类型改 `File?`，编码失败 **删掉 0 字节文件** 后 `return null`；三个读文件名的调用点改为 `file?.absolutePath`，`if (file == null) result else result.copy(...)` |
| `resizeJpegToMaxEdge` / `resizeImageBytes` | `if (saveOutcome(...)) bytes else null` —— 两个函数的 KDoc 本就承诺「失败返回 null，调用方用原图」，null 是既有分支 |

**保住的原契约**（逐条核对）：`PhotosOffloadHandler` 的只读挂载守卫与 `--max` 参数（`offload_cli_args_check` 只读扫描通过）；`AccessibilityOffloadHandler` 的 `OFFLOAD_PERMISSION` / `OffloadGate.allow("a11y_cli", …)`；`BrowserUseManager` 的 `_isLoading` / 导航串行化；`ChatPromptAndTools` 的 `[T-android-browser-toolresult-guard]` 截断块。压缩质量/尺寸参数一字未动；`SaveOutcome` 零改动。

---

## 3. 改动清单

```
 scripts/scan/discarded_return_guard.py                       | 373 ++++++++-
 src/android/.../browser/BrowserUseManager.kt                 |  47 ++-
 src/android/.../debug/DebugRPCHandler.kt                     |   6 +   (record-ok)
 src/android/.../debug/DebugScreenshotRing.kt                 |   5 +   (record-ok)
 src/android/.../provider/ImageBudget.kt                      |   6 +   (record-ok)
 src/android/.../sandbox/offload/AccessibilityOffloadHandler.kt | 10 +-
 src/android/.../sandbox/offload/PhotosOffloadHandler.kt      |  11 +-
 src/android/.../tools/ReadImageTool.kt                       |  10 +-
 src/android/.../ui/chat/ChatPromptAndTools.kt                |  15 +-
 9 files changed, 468 insertions(+), 15 deletions(-)
```

生产 5 文件（7 处编码判定）+ 半径外 3 文件（注释）+ 门 1 文件。**没有新增测试文件**：本缺陷的判定函数（`saveOutcome`）与它的 JUnit 落点（`SaveOutcomeTest`，5 例）在 1047-B01 已入库，本轮复用它做 L3 支撑臂而不是复制一套（见 §5 L3）。

---

## 4. 门与判据（扩 `discarded_return_guard.py`，0 新建门）

新增 `scan_encode_verdict(root)` 段，两条判据都从**语法位置**派生，不锚拼写：

- **C-B2 编码判定必须被消费**：每个 `.compress(` 调用点若处于**语句位置**（返回值没被 `val/var =`、`if (…)`、`?:`、`return`、`&&`/`||`、实参传递消费）⇒ 红。
  - 实现要点：`_enc_enclosing_open` 找最近块开符时**同时认 `… {` 与 lambda 箭头 `… ->`**（否则 `val encoded = file.outputStream().use { … }` 形态会被误判成语句位置——这是本门自身在 pre2 臂上暴露出来的解析缺口）。
- **C-B3 返回非 Unit 的 helper 必须能表达失败**：函数体内含 `.compress(` 且返回类型非 Unit/非可空 ⇒ 必须存在 `return null` / 可空返回类型 / `throw` / 失败结果构造之一，否则红（`resizeJpeg`、`saveBitmapToFile` 这类「包一层」的 helper 的专用判据）。
- **`record-ok:` escape** 复用本门既有机制：注释里 `record-ok:` + 理由（支持多行注释块，`_enc_exempt` 向前回看 4 行）。
- **带牙自检** `encode_verdict_self_test()`（6 臂）：dirty-C-B2（语句位置 compress）恰红 C-B2；dirty-C-B3（非 Unit helper 无失败出口）恰红 C-B3；clean 四形态（`use` 绑定 / 可空返回 / throw / `record-ok:`）全绿。收尾标记 `encode-verdict self-test has teeth: …`。
- 门串联：`--self-test` 返回值串成 `rc2..rc6`；真树扫描段打印 `✅ discarded-return guard: encode verdict section green (every compress verdict reaches a failure exit)`。
- **门数 111 / 夹具 342 不变**（0 新建门；`discarded_return_guard` 已在 1047 并集阶段注册进 `test_scan.py`，flag 是**连字符** `--self-test`）。

---

## 5. 验证四层

### L1 门层
- 本门真树：rc=0，三段全绿（drain / binary-write / **encode verdict**）。
- `--self-test`：rc=0，五个 `has teeth` 标记齐（含新增 `encode-verdict self-test has teeth: C-B2 … C-B3 …`）。
- **PRE 臂**（`probe/base/pre` = `git show eea9f407:` 抽 5 个基线文件）：rc=1，**10 条违规**，其中 C-B2 恰为票据点名的 **7 处、逐处精确到行**：
  `AccessibilityOffloadHandler.kt:459` / `PhotosOffloadHandler.kt:741` / `ReadImageTool.kt:100` / `BrowserUseManager.kt:1422` / `BrowserUseManager.kt:1541` / `ChatPromptAndTools.kt:754` / `ChatPromptAndTools.kt:1536`；另 3 条 C-B3（`ReadImageTool.kt:42`、`BrowserUseManager.kt:1363`、`BrowserUseManager.kt:1537`）= 同一批 helper 的第二半，判据独立命中。
- **PRE2 臂**（基线 `ImageBudget.kt` 单独）：rc=1，2 条（`:110 [B2]`、`:80 [B3]`）⇒ 票据「它已合规」的前提不成立，见 §6 D1。
- 全量 `scan.sh`（111 门）：沙箱长跑被回收两次（`exit -1`，属 §7 沙箱缺陷），改为**与 scan.sh 同构的三段切块**（`probe/chunks/c1..c3.sh`，切块脚本逐字复制 scan.sh 的门块 + Summary 尾，只把 `ROOT=` 钉死）：
  `c1`（门 1–40）**rc=0 / 0 FAIL / 0 Traceback**；`c2`（41–80）**rc=0 / 0 FAIL / 0 Traceback**；`c3`（81–111）**rc=0 / 0 FAIL / 0 Traceback** ⇒ **111/111 绿**。切块负一次运行撞 ENOSYS（`OSError: [Errno 38]` on `SkillRestorePolicy.kt`），静置 60 s 重跑即净 —— 按规程未记成真红。
- `test_scan.py`（342 夹具）：整跑同样被回收，改按 `main()` 的 45 个 `test_*` 调用切 5 块（`probe/ts/chunk0*.py`，进程内 `import test_scan` 直接调函数，聚合模块级 `PASS/FAIL`）：**25 + 48 + 70 + 120 + 79 = 342 PASS / 0 FAIL**。其中 `discarded_return_guard selftest has teeth (binary-write self-test has teeth)` 与 `real tree green` 两条见证在册 ⇒ 新增的 encode 段在 CI 判据里不是隐形件。

### L2 邻门层
按概念取 9 道（票据点名 7 道全在 + 2 道同概念加取），逐个 rc=0：
`offload_cli_args_check` / `a11y_handler_contract_check` / `file_provider_paths_coverage_check` / `readonly_mount_write_guard` / `tool_title_error_branch_check` / `share_dir_prune_check` / `log_url_redaction_guard` / `offload_media_b64_wire_check`（a11y 截图 base64 的出网形态）/ `offload_host_path_check`（Photos 路径）—— **9/9 rc=0**。

### L3 装置层
**降级说明**：7 处站点全在 `Bitmap`/`BitmapFactory` 上，5 个生产文件拉进整个 Android 面（Room/Compose/OkHttp），无法在纯 JVM 编译 ⇒ 主臂按规程降级为「门 + 真树字节变异」；能抽的纯决策（`saveOutcome`）用**真生产文件 + 真随仓测试**跑支撑臂。

- **主臂（真树字节变异，进程内）** `probe/mutate_real.py`：对 7 个站点各做一次「判定被丢弃」的字节级变异 → 门禁 ⇒ 再字节级还原 ⇒ 再扫；**8 臂全中**（7 站点 + record-ok escape 臂）：
  ```
  [ok] photos copyResized         B2=[('PhotosOffloadHandler.kt','copyResized')]
  [ok] a11y uiScreenshot          B2=[('AccessibilityOffloadHandler.kt','uiScreenshot')]
  [ok] read_image execute         B2=[('ReadImageTool.kt','execute')] B3=[…]
  [ok] browser screenshot         B2=[('BrowserUseManager.kt','screenshot')] B3=[…]
  [ok] browser saveBitmapToFile   B2=[('BrowserUseManager.kt','saveBitmapToFile')] B3=[…]
  [ok] chat resizeJpegToMaxEdge   B2=[('ChatPromptAndTools.kt','resizeJpegToMaxEdge')]
  [ok] chat resizeImageBytes      B2=[('ChatPromptAndTools.kt','resizeImageBytes')]
  [ok] record-ok escape           B2=[('…/ImageBudget.kt','compressBytes')]
  post-restore: B2=0 B3=0
  ALL ARMS MATCH
  ```
  还原用**内存字节备份**，不用 `git checkout --`；`post-restore B2=0 B3=0` 即 sha 等价的行为证据（逐次 arm 亦打印还原 sha 对拍）。变异前有守卫：树不绿就 `REFUSING`（开发期真触发过一次，见 §6 D2）。
- **支撑臂** `probe/l3/run.sh`（kotlinc 2.0.21 + JUnitCore，`/opt/jvm-libs` 的 junit/hamcrest + 显式 `kotlin-stdlib.jar` 运行期 classpath）：编译**真生产** `util/SaveOutcome.kt` + **真随仓** `util/SaveOutcomeTest.kt`：
  - POST：`OK (5 tests)`
  - MUT（把判定退化成只认 `compressed` 忽略 `streamOpened`）：`Tests run: 5, Failures: 2` —— `aNullOutputStreamIsReportedAsFailure`、`successIsTheConjunctionNotEitherHalf`
  - 收尾 `ALL ARMS MATCH (post green / mut red)` ⇒ 7 处站点路由进去的那个判定**确实是有牙的**，不是把失败塞进一个恒真的洞。

### L4 CI 层
见 §7。

---

## 6. 残留与失真

**失真（报告 vs 真树）**
- **D1（重要）**：票据 §2 写「`ImageBudget.kt:110` 属另一族：先证它是否已判返回值」并暗示它合规。真树取证：PRE2 臂 2 条违规（`:110 [B2]` 丢弃判定、`:80 [B3]` helper 无法表达失败）⇒ 它**同样**丢弃判定，票据的「可能已合规」前提不成立。按票据不动，`record-ok:` 明写属于半径外 + 登记 R2。**判据：`record-ok` 不是「这处没问题」，是「这处有问题但不在本条半径」**——两者在门里同形，区别只在理由文本。
- **D2（我自己踩的）**：`mutate_real.py` 首版用 subprocess，沙箱在 waitpid 中途 `exit -1` ⇒**生产文件停在变异态**，下一次扫描把污染态当基线存下来（`REFUSING: the tree is not green before mutating (1 B2 / 1 B3)`）。改为**纯进程内**（只做字节读写 + 直接 import 门模块）后 8 臂稳定。**规程来源**：变异驱动必须自带「变异前树必须绿」的断言 + 每次 arm 后立即还原，不能靠 arm 之间共享状态。
- **D3（票据措辞）**：票据表格把 `ChatPromptAndTools.kt:754` 标为「`resizeImageBytes`? 见下」并自注不确定；真树：`:754` 是 **`resizeJpegToMaxEdge`**，`:1536` 是 **`resizeImageBytes`**（两者都被 PRE 臂按真树函数名报出）。行号与函数名以真树为准。
- **D4（判读装置自伤，两条 needle + 一条对齐，全部当场闭环）**：
  ①`Summary: 111 passed, 0 failed` 的判据不能写「日志里的 Summary 行集合 == 该行」——日志另有 7 条按测试组统计的 Summary（`25/5/13/33/10/22/10 passed, 0 failed`），正确判据是**111/0 出现次数恰为 1 且所有 Summary 行 failed==0**。
  ②`--self-test` 的**原始 stdout 不进 CI 日志**（`test_scan` 只在断言行打印注册标记），所以「`encode-verdict self-test has teeth`」在日志里恒 0 命中 —— 这不是「新臂没跑」，是 needle 选错了层。新臂的 CI 见证 = 注册行 + `--self-test` 返回 0（encode 段返回 rc6 参与返回码串联，臂红必使夹具红）。此坑 1042-C6 已记，本轮换了个门又踩一次 ⇒ **判据要挑「CI 里真的会被打印的那一层」**。
  ③警告比对必须按（文件名, 警告文本），**不能按行号**：首版按行号比对报 4 条「新增」，逐条看全是同一批警告的漂移（`BrowserUseManager:151→152`、`:152→153`、`AccessibilityOffloadHandler:1157→1165`、`PhotosOffloadHandler:1213→1222`），按文本对齐后 **新增 0 / 减少 0**。与 1047 并集阶段的同一条结论互为复现。

**残留（backlog §1048-A2）**
- **R1 真机欠账**：构造编码失败（极小内存 / 不可写目录 / insert 后写入失败），验证①相册导出答失败且不留 0 字节文件 ②a11y 截图返回 `ENCODE_FAILED` 而非 `success=true` 空载荷 ③`read_image` 报错 ④浏览器截图报错且不带空 `imageFilePath` ⑤chat 预览回落到原图。**触发条件**：下次真机窗口或用户报告「相册里有打不开的图片 / 模型说看不到图但工具成功」。
- **R2 `ImageBudget` 家族**：`compressBytes(baos)` 形态（内存字节而非文件）是否该纳入 C-B2/B3；本轮 `record-ok:` 豁免。**触发条件**：下次改压缩链 / 改 ImageBudget 的任何一行。
- **R3 `record-ok:` 机制在本门的语义边界**：它对 C-B2/B3 一视同仁豁免，无法区分「刻意保留」与「待修」。**触发条件**：任一门在真树出现 ≥3 处 `record-ok:`（现在 3 处恰在阈值上，本届不扩）。
- **R4 其他二进制写出面**：WAV（录音/合成）、TXT/CSV 导出、`MediaStore` 流写出是否同族（返回值/短写丢弃）。**触发条件**：下一次审计覆盖录音或导出面时。
- **R5 两条 debug 通道**（`DebugRPCHandler:241`、`DebugScreenshotRing:65`）：同族但纯调试面，`record-ok:` 保留。**触发条件**：debug RPC 的截图成为用户可见产物时。

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 main | `eea9f407f9e0e79f4902eeef1f84e1550ee5c29c` |
| 分支 | `fix/dv28-a2-compress-verdict` |
| 代码提交 | `a8ae907d`（9 文件 +468/−15：生产 5 / record-ok 3 / 门 1） |
| 报告提交 | `44d11704`（仅 `fix-reports/dv28-FIX-A2.md`） |
| CI run | **`40051049467`** / `#2589` / attempt 1 / event `workflow_dispatch` / conclusion **success** |
| CI job | **`120743586851`**（26 步，`bad=[]`，唯一 skipped = `Publish to Releases` = main 门控） |
| head_sha 三方 | run `44d117048ee3731020d3eed1485a64de934d6451` == 本地 HEAD == `git ls-remote origin` 逐字符一致 |

**CI 日志判读（`probe/ci/job.log`，1,456,245 B，装置 `probe/ci/verify_ci.py`）**：
- `Summary: 111 passed, 0 failed` 恰一条（日志另有 7 条按测试组统计的 Summary 行，全为 `0 failed`；判据只认 111/0 的出现次数为 1）；
- `ALL 342 CASES PASS` ×1；`discarded_return_guard selftest has teeth (binary-write self-test has teeth)` ×1；`discarded_return_guard real tree green` ×1；`encode verdict section green` ×1；
- Kotlin 编译错 0；游离 `FAILED` 行 0（含 FAILED 的行逐条确认都是 PASSED 用例名子串）；
- **本支改过的 8 个文件编译警告：按（文件名, 警告文本）与 main 基线对齐后 新增 0**（基线 4 / 本支 4，逐条同文；首版按行号比对报出 4 条「新增」，实为行号漂移：`BrowserUseManager:151→152`、`:152→153`、`AccessibilityOffloadHandler:1157→1165`、`PhotosOffloadHandler:1213→1222`）。
- 装置自伤（见 §6 D4）：首版把「selftest 原始 stdout 里的 `encode-verdict self-test has teeth`」当 CI 判据，但 `test_scan` 只在断言行打印注册标记、**不转发门自身的 stdout**（1042-C6 已记），故该 needle 恒 0。正确见证 = 注册行 + `--self-test` 返回 0（我的 encode 段返回 rc6 参与串联 ⇒ 臂失败必使夹具红）。

源码树等价声明：本 §7 回填仅触及 `fix-reports/`，`git diff --name-only 44d11704..HEAD -- . ':(exclude)fix-reports'` 输出为空 ⇒ CI 结论对当前代码树成立。

---

## 8. 自证成本与不确定项

**成本**：全量 `scan.sh` 在本沙箱**无法一次跑完**（两次被回收，第三次切块才拿到 111/0），切块与 `test_scan` 切块合计约 25 分钟机时；变异臂 8 次 ×（变异+扫+还原+扫）约 4 分钟；L3 编译两轮约 90 s。CI 那一轮是唯一有独立环境的验证。

**不确定项**
1. 切块跑 111 门 = 门集合等价，但**不是** scan.sh 原进程的 Summary 计数（我按「每块 rc=0 + 0 FAIL」逐块判定，未做一次完整 Summary 行）。CI 里是一次完整跑，以 CI 的 `Summary: 111 passed, 0 failed` 为准。
2. 同理 `test_scan` 的 342 是按 5 块 `PASS` 求和（25+48+70+120+79），非单进程 `ALL 342 CASES PASS` 行；CI 会给单进程结论。
3. `_enc_enclosing_open` 认 `->` 是靠「最近块开符」启发式，`when` 分支里的 lambda（`x -> { … compress … }`）未被单独构造臂覆盖（真树 0 例）。若未来出现，门会**误红**（fail-closed，可接受）而不是漏红。
4. `saveBitmapToFile` 返回 `File?` 后，`BrowserUseManager` 内是否还有第三处消费者未改成可空——本轮按 `grep -rn "saveBitmapToFile("` 逐个核过（3 处），但门没有「调用点必须处理 null」的判据（C-B3 只管 helper 自身能否表达失败）。**登记为 R6 候选**：触发条件 = 下次该 helper 新增调用点。

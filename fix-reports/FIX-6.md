# FIX-6 · deep-audit-1044（v23 波）· FIX-6 支 · A-08 P1 · 分享入站 null 流产生幽灵附件

**分支** `fix/dv24-a6-share-null-stream`
**基线** main `f94413f52fa5403c903902f6b1d2ae10cb9bc906`
**工单** A-08（面 A，P1）· 门独占：`scripts/scan/discarded_return_guard.py`
**停止节点** 分支 CI 绿；**未合并 main**（含 `.kt` 验证面，起手权在用户）

---

## §1 缺陷与根因

### 症状原话

从任何来源 app（相册、文件管理器、Telegram）分享一个文件到 RikkaMinis：若来源 app 的
`ContentProvider` 在被读取时崩溃/被回收/拒绝服务，RikkaMinis 会在输入框上方挂出一个
**附件卡片**，但点开没有内容；也没有任何「无法读取分享文件」的提示，附件随后静默消失。

### 根因假设

`ContentResolver.openInputStream(Uri)` **规范允许返回 null 而不抛异常**（远端 provider
进程死亡、被拒绝、RemoteException 被吞）。旧代码写成
`contentResolver.openInputStream(uri)?.use { … }`：**整个 `use` 块被跳过**，随后无条件
`return name` ⇒ 函数返回一个**从未创建过**的文件名。

### 真树 Before 原文（`git show f94413f5:…/ShareReceiverActivity.kt`）

```kotlin
373:    private fun copyUriToStaging(uri: Uri, mimeType: String): String? {
...
383:            contentResolver.openInputStream(uri)?.use { input ->
384:                dest.outputStream().use { output ->
385:                    val buf = ByteArray(8192)
386:                    var total = 0L
387:                    val deadline = System.currentTimeMillis() + IO_TIMEOUT_MS
388:                    while (true) {
...
409:                    stagedBytes.credit(total)
410:                }
411:            }
412:            name                       // ← 无 `?: return null`
413:        } catch (e: Exception) {
414:            AppLogger.warning(TAG, "copyUriToStaging($uri): ${e.message}")
415:            try { dest.delete() } catch (_: Exception) {}
416:            null
417:        }
418:    }
```

### ≥1 条证据

1. **调用链**（真树行号）：`handleSingleSend:330` / `handleMultipleSend:343` /
   `handleView:363` 三处都是 `copyUriToStaging(uri, type)?.let { out +=
   PendingShare.Item(Kind.ATTACHMENT, it) }` ⇒ `staged != null` ⇒ 直接造出
   `PendingShare.Item`。`PendingShare.save` → `launchMainActivity(withSharedContent = true)`
   ⇒ 幽灵附件进入 composer。三处调用点都**没有**「Could not read shared file」的错误分支
   （该 Toast 在 `handleSingleSend` 的 `SingleSendDecision.Drop` 附近，null 路径根本走不到）。
2. **同仓对照**：全仓 10 处 `openInputStream(` 调用，其余 9 处都判空 ——
   `sandbox/offload/PhotosOffloadHandler.kt:707-715`（`if (input == null) return
   CopyOutcome.Fail(...)`）、`:721-722`（`?: return CopyOutcome.Fail`）、
   `util/ImageDecodeSizing.kt:125,137`（`?: return null`）、
   `debug/ChatMutationMethods.kt:124`、`ui/chat/ChatAttachmentManager.kt:112`、
   `ui/settings/ProviderListScreen.kt:233`、`ui/webapp/AddToHomeSheet.kt:195-197`。
   **本仓已有「判空早退」的既有形态可接**（阶梯第 6 级的依据）。
3. **门探针（PRE 臂）**：把基线 blob 放进临时树跑新写的门段，红在
   `[A-08 G1] copyUriToStaging() creates the destination file from a stream with NO
   null early-exit`（`share/ShareReceiverActivity.kt:373`）。

---

## §2 修法

### 停在阶梯第 6 级（接既有守卫形态），不是第 1~5 级

- 已有可接的东西：`PhotosOffloadHandler` 的「取流先判空」形态、`StagedBytesLedger`
  这条**已存在的纯核 + JVM 测试**通道（§1042-A-07 留下的）、以及 `decideSingleSend`
  / `admitAttachText` 这条「纯函数化后由 Activity 注入」的既有做法。
- 不新写抽象层 / 缓存 / 回调总线：拷贝循环本身**逐行保留**，只把「取流 + 建文件 +
  记账」三件事搬到纯核，Activity 只做注入。

### 被否决的方案与理由

| 方案 | 否决理由 |
|---|---|
| **在原函数里加 `?: return null`**（最小改法，最省） | 能修掉缺陷，但**无法 JVM 化**：`copyUriToStaging` 依赖 `ContentResolver` / `Context`，沙箱里连 `kotlinc` 都编不出 Activity，只能退化成「源码形状测试 + 门带牙」——即任务书 §4 允许的退化路径。而本仓**已经有** `StagedBytesLedger.kt` 这条纯核通道，代价只是一次抽取，证据强得多。 |
| **纯核收 `dest: File` 参数**（任务书 §4 给的签名草案） | 撞**邻门** `readonly_mount_write_guard.py`：它的 `PATH_PARAMS` 表里含 `dest`，任何「从路径参数写宿主文件」的函数必须调 `isLinuxPathUnderReadOnlyMount(`。任务书 §1 明令**不得**改那个门（属相邻面、本波无人用）。→ 改成**目标以工厂注入**（`openDest: () -> OutputStream` / `deleteDest: () -> Unit`），核心**完全不含路径**：写目标仍由调用方拥有（app 私有暂存目录），语义不变，且门零触碰。 |
| **保留 `dest: File` 并给邻门加豁免** | 越界：本波不改 `readonly_mount_write_guard.py`（COMMON §1 门独占纪律）。 |
| **在 `PendingShare` 侧加「文件存在性」校验** | 改数据契约（`PendingShare.Item` 的语义）= 跨层改动 + 三处调用点都要改，违反任务书 §3「不得改 `PendingShare` 的数据结构与调用方签名」。 |
| **在调用方补日志（任务书 §3 允许的三处）** | 不需要：根因在拷贝函数，日志补在调用方只会**掩盖**信号（幽灵附件照样产生）。修法把日志留在失败分支本身。 |

### 修法本体

1. 新增 `share/SharedStreamCopy.kt`（与 `StagedBytesLedger.kt` 同构的**纯核**）：
   - `copySharedStream(openStream, openDest, deleteDest, stagedBytesNow, credit,
     maxFileBytes, maxTotalBytes, ioTimeoutMs, now): SharedStreamCopy`
   - **顺序即修复**：`val input = openStream() ?: return abort(OpenStreamNull)`
     **在** `openDest()` **之前** ⇒ null 流不会留下 0 字节文件。
   - 全部副作用注入（流工厂 / 目标工厂 / 记账 / 时钟），核心**不碰任何 Android API**。
   - 结果是 `SharedStreamCopy.Staged(bytes)` / `Failed(reason, detail)`，
     `reason ∈ {OpenStreamNull, TimedOut, ExceededPerFile, ExceededCumulative, ReadFailed}`。
2. `ShareReceiverActivity.copyUriToStaging` 变成薄壳：注入
   `openStream = { contentResolver.openInputStream(uri) }` /
   `openDest = { dest.outputStream() }` / `deleteDest = { dest.delete() }` /
   `stagedBytesNow = { stagedBytes.current() }` / `credit = { stagedBytes.credit(it) }`，
   按 outcome 返回 `name`（仅 `Staged`）或 `null`。
3. **既有守卫全部保留**（逐条核对，缺一即回归）：
   `IO_TIMEOUT_MS` 超时删半成品 · `MAX_FILE_BYTES` / `MAX_TOTAL_BYTES` 拦截并删半成品 ·
   `stagedBytes.credit(total)` 记账 · catch 分支删除半成品并返回 null。
   唯一的行为变化 = **null 流现在返回 null**（原来返回 name）。

---

## §3 改动清单

| 文件 | 增删 | 本支改动的行区间 |
|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/share/ShareReceiverActivity.kt` | +75 / −37 | `copyUriToStaging` 体 **`:368-421`**（旧 `:368-418`）；KDoc 改写 `:368-382`；`describe()` 扩展函数 `:413-421`；顶部 import 段**未动**（`InputStream` 随纯核搬走，已回退） |
| `src/android/app/src/main/java/com/rikkaminis/app/share/SharedStreamCopy.kt`（**新增**） | +121 / −0 | 全文件 `:1-121` |
| `src/android/app/src/test/java/com/rikkaminis/app/share/SharedStreamCopyTest.kt`（**新增**） | +311 / −0 | 全文件 `:1-311` |
| `scripts/scan/discarded_return_guard.py` | +600 / −2 | 判据段 **`:540-780`**（`_own_acquisition` / `_guard_before` / `_returns_outcome` / `scan_share_stream_guard`）；watchlist 表 `:63-72`（加 `copySharedStream` + `BARE_OK`）；`_pattern()` `:78-86`；自测段 **`:1290-1480`**（`share_stream_self_test` + 10 个夹具臂）；`main()` `:1500-1540` |

**合计** 4 文件 **+1068 / −39**。**未触碰**：`scan.sh`（冻结面）、`test_scan.py`（冻结面）、
`PendingShare.kt`、`MainActivity.kt`、`ui/**`、任何 `strings.xml`、`AndroidManifest.xml`、
`build.gradle*`、workflow。**门数 109 / 夹具 336 均不变**（0 新建门，扩既有门）。

**同文件跨支提示**：`share/ShareReceiverActivity.kt` 与 `share/` 目录在本仓被多门引用
（`discarded_return_guard` / `file_provider_paths_coverage_check` /
`readonly_mount_write_guard` / `share_dir_prune_check`），但**只有本支改了它的内容**；
若并集时另一支也改同一文件，须以本支的 `copyUriToStaging` 段为准（其余行区间互不重叠）。

---

## §4 门判据与带牙证明

扩 `scripts/scan/discarded_return_guard.py`，新增**分享入站流段**（既有门，独占）：

| 判据 | 内容 | 锚定什么 |
|---|---|---|
| **G1** | 同一个 `share/` 函数里，**自己取流**（`openInputStream(` / `openStream()`，lambda 感知的 `_own_acquisition`）且**建目标**（`outputStream(` / `openDest()`）时，必须有 null 早退，**且早退位置在建目标之前** | **顺序决定点**（先判空后建文件），不锚写法：elvis（`?: return …`）与 guard clause（`if (x == null) { … return … }`）都绿 |
| **G2** | `share/` 包里必须仍有 `openInputStream(`（锚点存在性） | fail-closed：路径被搬走时不会静默放行 |
| **G3** | `copySharedStream` 的 outcome 必须被 `val` 绑定在调用点 | 丢弃 outcome = 把「是否真的写了文件」这个唯一信号扔掉 |
| **G4** | 自己取流**且返回文件名**（不是返回 outcome）的函数，必须有成功闸门（`when` / `is … ->` / `if (… == null) return null`） | 返回 outcome 的纯核豁免（outcome 本身就是闸门） |
| **watchlist** | 加 `copySharedStream`；因它是顶层函数，pattern 允许**无接收者**（新增 `BARE_OK` 表） | 语句位置的调用会把 outcome 扔掉 |

**带牙臂（`--self-test` 内 10 个夹具，逐臂精确红）**：

| 臂 | 改什么 | 红在哪条 |
|---|---|---|
| A08-0 | 干净夹具（先判空 → 建目标 → 拷贝） | 0 命中（绿） |
| A08-1 | 删掉判空早退 | `[A-08 G1] … NO null early-exit` |
| A08-2 | 判空移到建目标**之后** | `[A-08 G1] … null-checks the stream AFTER creating the destination file` |
| A08-3 | 换成 guard clause 写法 | **仍绿**（判据锚决定点不锚写法） |
| A08-4 | 目标创建消失 | `[A-08 G1] no outputStream( … (fail-closed)` |
| A08-5 | `openInputStream` 消失 | `[A-08 G2] no openInputStream( … (fail-closed)` |
| A08-6 | 语句位置调用 `copySharedStream` | watchlist 命中（控制臂：绑定后 0 命中） |
| A08-7 | outcome 未绑定 | `[A-08 G3] … never bound` + `[A-08 G4]` |
| A08-8 | **真树形态**（Activity 注入 + 核心在另一个文件） | 0 命中（绿）—— 证明判据跟得住抽取后的布局 |
| A08-9 | 核心在**另一个文件**里且无判空 | `[A-08 G1]` 报在**那个文件**上 |
| A08-10 | 自己取流 + 无成功闸门（内联回归形态） | `[A-08 G4]` + `[A-08 G1]` |

---

## §5 验证四层

### L1 门

- 本门真树：`python3 scripts/scan/discarded_return_guard.py .` → **rc=0**，四段全绿
  （watchlist 2 项 / queue-drain / staged-bytes / **share inbound stream**）。
- 本门 `--self-test`：四段全 PASS，新增段
  `✅ share-stream self-test has teeth: A-08 G1 (missing null guard / guard after create /
  vanished write path) and G2 (vanished resolver stream) each flip red on exactly their own
  breaker, the guard-clause spelling stays green, and a discarded copySharedStream outcome is
  caught by the watchlist`。
- **PRE 臂（基线树）**：把 `git show f94413f5:…` 的 blob 放进临时树跑本门段 → 红
  `[A-08 G1] copyUriToStaging() creates the destination file from a stream with NO null
  early-exit`（`share/ShareReceiverActivity.kt:373`）。**基线树确实红**。
- **全量门**：`gate_run.sh --full` 跑到 **104/104 门 pass=0 fail=0**（末段 `test_scan`
  撞沙箱 ENOSYS 窗口，见 §7 R6），`test_scan.py` 单独重跑 → **ALL 336 CASES PASS**。
  **门 109 / 夹具 336 不变。**

### L2 邻门（按概念反查，非照抄任务书）

`grep -rln "ShareReceiverActivity|StagedBytesLedger|share/" scripts/scan/*.py` →
`discarded_return_guard`（本门）· `file_provider_paths_coverage_check` ·
`readonly_mount_write_guard` · `share_dir_prune_check`。**逐门直跑 4/4 rc=0**。

> **这一层抓到一个真问题**：第一版纯核签名带 `dest: File`，`readonly_mount_write_guard`
> 当场红：`✗ share/SharedStreamCopy.kt:copySharedStream(…) — writes a host file from a path
> parameter without calling isLinuxPathUnderReadOnlyMount(`。任务书 §1 禁止改那个门 ⇒ 改
> 签名（目标改工厂注入）而不是改门。**邻门在本次是有牙齿的**。

### L3 真 JVM（真 kotlinc 2.x + 真 JUnit 4.13.2）

装置：`probe/l3/`（`make_arms.py` 只做字节级编辑、`arm.sh` 独占子进程），
core + `StagedBytesLedger.kt` + `SharedStreamCopyTest.kt` 三文件真编译真跑。

| 臂 | 结果 |
|---|---|
| **POST** | `OK (12 tests)` — 全绿 |
| **PRE**（null 流当成功：`?: return SharedStreamCopy.Staged(0L)`，即缺陷形态） | **精确 3 红**，其余 9 绿：<br>`a null stream fails instead of reporting a staged file`<br>`a null stream opens no destination and leaves no ghost file`<br>`credit is never called for a failed copy` |
| **m1**（判空在**建目标之后** ⇒ 0 字节幽灵文件） | 精确 2 红：`a null stream opens no destination and leaves no ghost file` + `bytes are written and credited once with the exact count` |
| **m2**（abort 不删半成品） | 精确 4 红：per-file cap / cumulative cap / io deadline / read failure 四条「删半成品」断言 |
| **m3**（不记账） | 精确 3 红：两条「恰好填满上限」+ 一条「精确记账」 |
| **m4**（per-file cap 由 `>` 变 `>=`） | 精确 1 红：`a share exactly at the per file cap is admitted`（边界语义） |

**期望值全部手写字面量**（不從被测常量推导），PRE 臂红点 = 缺陷点本身。

### L4 CI

run **39894730889** / job **120222409741** attempt 1 **success**；26 步 `failed=[]`，唯一
skipped = `Publish to Releases`（main 门控）；`Summary: 109 passed, 0 failed` 恰一条；
门头 1..109 连续且门名互异；`ALL 336 CASES PASS`；编译错 `Z e: ` 0；游离 FAILED 0；
`SharedStreamCopyTest` **12/12 逐条 PASSED**。详见 §8。

**三源取二满足**：CI 绿 + 本地门全绿（104/104 + 336 夹具）+ 沙箱 JVM 装置（PRE 精确红 / 4 变异精确红）。

---

## §6 任务书失真

1. **任务书 §2 给的「真树已核实」行号对不上**：写 `ShareReceiverActivity.kt:380-415`
   （`copyUriToStaging`），真树基线是 **`:373-418`**（`copyUriToStaging`），`openInputStream`
   在 **`:383`**。**机制成立，行号偏移 7 行**。
2. **任务书 §2 说常量是 `MAX_FILE_BYTES(50MB)` / `MAX_TOTAL_BYTES(100MB)`**，
   真树基线是 **100 MB / 500 MB**（`ShareReceiverActivity.kt:43-44`：
   `MAX_FILE_BYTES = 100L*1024*1024`、`MAX_TOTAL_BYTES = 500L*1024*1024`）。
   任务书把 `§1042-A-07` 报告里描述过的另一组数值搬了过来。**不影响本支修法**
   （两个常量都是注入参数，测试用字面量 5/95/100）。
3. **任务书 §4 给的签名草案 `copySharedStream(openStream, dest: File, stagedBytes,
   maxFileBytes, maxTotalBytes, ioTimeoutMs, now): Boolean/String?` 会撞邻门**：
   `dest` 在 `readonly_mount_write_guard.py` 的 `PATH_PARAMS` 表内
   （`:87-89`：`^(outputPath|linuxPath|guestPath|destPath|dstPath|dest|dst|…)$`），
   而任务书 §1 又禁止改那个门。**本支改为 `openDest` / `deleteDest` 工厂注入**，
   核心零路径（§2 已论证）。任务书 §4「若抽取成本过高允许退化」的前提不适用 ——
   抽取本身可行，只有**签名**需要调整。
4. **任务书 §1 说的门「如需可加 `file_provider_paths_coverage_check.py` 轻判据」**：
   未加。该门管的是 FileProvider 路径覆盖，与「null 流」无关；判据放进
   `discarded_return_guard` 的既有 share 段（同一文件、同一话题、同一门）更合适，
   也符合「门独占」纪律。
5. **任务书 §2 说的同仓对照点 `debug/ChatMutationMethods.kt:124`** 存在且确实判空；
   `ui/webapp/AddToHomeSheet.kt:195-197` 亦然。**对照成立**。

---

## §7 未做 / 残留

- **R1｜真机行为级（P1，验证面）**：用一个会返回 null 流的 `ContentProvider`（或卸载来源
  app 后再分享）验证：不再产生幽灵附件卡片，且 `AppLogger` 打出
  `copyUriToStaging(<uri>): openInputStream returned null — nothing to stage`。
  **触发条件** = 下次真机窗口。**触发后方案** = 真机装一个可配置「返回 null 流」的
  测试 provider，或用 `adb shell pm uninstall` 来源 app 后从残留 chooser 分享。
  **搁置理由** = 沙箱无法造真实跨进程 provider 行为。
- **R2｜`handleView` 的 `contentResolver.getType(uri)` 仍可能抛（P2）**：ACTION_VIEW 路径
  在 `copyUriToStaging` **之前**调 `getType(uri)`，远端 provider 崩溃时这里会抛
  `SecurityException` / `DeadObjectException`，由 `extractItems` 的 `catch (e: Throwable)`
  兜住 ⇒ 整批分享被丢弃（**不是幽灵附件**，方向安全，但会静默丢单）。**触发条件** =
  用户报告「分享某个来源后什么都没发生」。**触发后方案** = 给 `getType` 加
  `runCatching { … }.getOrNull()`，范围限于 `handleView` 一行。
  **搁置理由** = 与本单缺陷不同向（不会产生幽灵），且 §3「不得改调用方签名」边界之外
  的行为改动，按半径纪律登记不修。
- **R3｜`openInputStream` 抛 `FileNotFoundException` 的路径（P3，行为未变）**：抛异常时
  走既有 catch 分支（删半成品 + 返回 null + warn 日志），**本次未改**。**触发条件** =
  出现该异常被误报的日志。**触发后方案** = 无。**搁置理由** = 既有行为已正确。
- **R4｜门 G1 的 `_own_acquisition` 只认 `= {` 形态的 lambda（P3，规则级）**：写成
  `openStream = someRef::method`（可调用引用）时，调用点会被判成「自己取流」。
  **触发条件** = 有人在 `share/` 里用可调用引用注入流。**触发后方案** = `_own_acquisition`
  加 `::` 形态豁免。**搁置理由** = 当前无此用法，且误判方向是**多报**（更安全）。
- **R5｜门 G4 的豁免靠类型名包含 `Copy`/`Outcome`（P3，规则级）**：若纯核改名
  （如 `Result`），G4 会开始误判核心。**触发条件** = `copySharedStream` 改名。
  **触发后方案** = 改成锚「函数返回类型不是 String」而非名字。**搁置理由** = 当前唯一。
- **R6｜本地全量门在沙箱跑不完（环境，S28，非本支缺陷）**：`gate_run.sh --full` 跑到
  104/104 全绿，末段 `test_scan` 时撞 PRoot ENOSYS（`Function not implemented`）——
  注册表已登记的沙箱症状。**触发条件** = 收口期需要本地全量证据。
  **触发后方案** = 空闲窗口重跑 `test_scan.py`（本支已单独重跑，`ALL 336 CASES PASS`）。
  **搁置理由** = 分支 CI 跑同一份 `scan.sh` + `test_scan.py`。
- **R7｜门自伤：首版判据把「注入 lambda 里的取流」当成「函数自己取流」（已修，规则级）**：
  首版按出现次数判 ⇒ **把 §1044 的 ship 形态自己判红**（Activity 只是把取流**注入**给
  核心，真正取流的是核心）。修法 = `_own_acquisition` 做**栈式 lambda 感知**
  （`= {` 开启的栈帧内的调用不归属外层函数）。
  **触发条件** = 任何「判断某调用归属哪个函数」的新门。**触发后方案** = 一律做作用域追踪，
  不做出现次数统计。**搁置理由** = 本支已修，留作跨批规则。
- **R8｜门自伤：首版 G4 把纯核也判红（已修，规则级）**：纯核**返回 outcome**，
  outcome 就是成功闸门，但首版 G4 只看「有没有 `when` / `is ->`」⇒ 误判。
  修法 = 加 `_returns_outcome` 豁免（返回类型含 `Copy`/`Outcome`）。
  **触发条件** = 再写「成功闸门」类判据。**触发后方案** = 豁免条件锚**返回类型**，
  不锚函数体形状。**搁置理由** = 本支已修。
- **R9｜`SharedStreamCopy.kt` 是新文件（信息项）**：主仓其他支若也往 `share/` 加文件，
  并集时不会撞（文件名唯一），但 `share/` 目录的**门**会把它一起纳入判读（这是设计意图）。

---

## §8 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f94413f52fa5403c903902f6b1d2ae10cb9bc906` |
| 本地 HEAD | `efc21523f200047e00c38adfeda5e330015cb68f` |
| `git ls-remote origin fix/dv24-a6-share-null-stream` | `efc21523f200047e00c38adfeda5e330015cb68f` |
| CI `head_sha` | 见 §8.1 |

提交链（4 提交，分行写清「生产 / 测试 / 门 / 签名修正」）：

| sha | 内容 |
|---|---|
| `07c314d0` | 生产：Activity 走纯核 + `SharedStreamCopy.kt` 新增（首版签名带 `dest: File`） |
| `79126a95` | 测试：`SharedStreamCopyTest.kt`（11 例） |
| `7b256666` | 门：`discarded_return_guard` 新增 share-inbound-stream 段 + watchlist |
| `efc21523` | 签名修正：核心改为工厂注入（零路径）+ 门的作用域追踪（`_own_acquisition`） |

### §8.1 CI

| 项 | 值 |
|---|---|
| run id | **39894730889**（`Build RikkaMinis Android APK`；`gh_sync.sh` 打印的 `#2528` 是其内部序号） |
| dispatch | `gh-actions-dispatch --workflow build-apk.yml --ref fix/dv24-a6-share-null-stream`（2026-10-23T03:23:10Z，204） |
| job id | **120222409741**（`build`，attempt 1） |
| conclusion | **success** |
| head_sha 三方 | `efc21523f200047e00c38adfeda5e330015cb68f` == 本地 `git rev-parse HEAD` == `git ls-remote origin fix/dv24-a6-share-null-stream` == `run.head_sha`（逐字符一致） |
| steps | **26 步全 success**，`failed=[]`，唯一 skipped = `Publish to Releases`（main 门控） |
| 日志判读 | `Summary: 109 passed, 0 failed` 恰一条（其余 6 条 Summary 为其他分组的 5/10/13/22/25 passed, 0 failed，无非零）· 门头 `[1/109]..[109/109]` **序号连续 109、门名互异 109** · `ALL 336 CASES PASS` 1 条 · 编译错 `Z e: ` **0** · 游离 FAILED **0** · `share inbound stream section green` 在册 · `SharedStreamCopyTest` **12/12 逐条 PASSED** · 日志 1,394,357 B |

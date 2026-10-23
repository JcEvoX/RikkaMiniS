# FIX-G10 — TTS 测试片合成：取消后仍写文件（deep-audit-1046 / S-10，P1）

- **基线** `f7aba218`（`merge: docs/dv25-fix-reports`）
- **分支** `fix/dv26-g10-tts-cancel-guard`
- **worktree** `/var/minis/shared/work/deep-audit-1046/g10/`
- **停止节点** 本支 CI 绿（未合并 main）

---

## 1. 缺陷与真树取证

### 1.1 缺陷本体（基线 `QuickTestSheet.kt`，函数 `synthesizeTestClip`）

| 基线行 | 内容 | 问题 |
|---|---|---|
| `:682` | `kotlinx.coroutines.withTimeoutOrNull(15_000) {` | 15 s 超时会**取消**整个协程，引擎初始化常常还没回来 |
| `:687` | `if (cont.isActive) cont.resumeWith(Result.success(ok))` | 只守卫了 **resume** |
| `:689` | `tts = android.speech.tts.TextToSpeech(context) { status -> … }` | 初始化回调体**无任何取消读取**：`language =`、`setOnUtteranceProgressListener(`、`synthesizeToFile(` 全裸 |
| `:704` | `val result = tts?.synthesizeToFile(text, Bundle(), file, "quicktest-clip")` | 这是**写盘**调用；取消后照常执行 |
| `:707` | `cont.invokeOnCancellation { runCatching { tts?.shutdown() } }` | 取消路径只 shutdown，**不删文件** |
| `:714` | `runCatching { file.delete() }`（`finally`） | 删除发生在取消**之后立刻**，而引擎的写入可能在它之后才落地 |

⇒ 时序失效链（真树可指认，不需要推测）：

1. `:682` 超时 或 调用方（`:546` 的 TRANSCRIPTION 分支，跑在 sheet 的 `viewModelScope`）被取消；
2. `:707` shutdown 引擎（`OnInitListener` 可能尚未回调、也可能正在队列里）；
3. `:714` `finally` 删除 `quicktest-clip-<ts>.wav`；
4. `:689` 的初始化回调**随后**才被投递（系统 TTS 初始化是异步 binder 回调，取消不撤销已投递的 looper 消息）；
5. `:704` `synthesizeToFile` 重新把 `quicktest-clip-<ts>.wav` 写进 `cacheDir`，**此后无任何代码删除它** ⇒ 进程生命周期内永久残留，且 `finally` 已经跑完，再也不会被清理。

### 1.2 取证（不采信报告行号，全部回真树取）

- `grep -rn 'synthesizeTestClip' src/android/app/src --include=*.kt` ⇒ 生产调用点**只有 1 处**（`:546`），函数本体 `:688`（基线 `:679`）；既有测试 `QuickTestEffectiveModelTest.kt:103` 只把它当 `performTest` 体的**边界标记**（正则 `"\nprivate suspend fun synthesizeTestClip("`），本支未改签名 ⇒ 该测试不受影响（已核）。
- 基线 `grep -n 'isActive'`（同文件）⇒ 仅 `:687` 一处，且只包 resume ⇒ **回调体无取消读取**，门 C1 在基线树上 3 处报警（`language =` / `setOnUtteranceProgressListener(` / `synthesizeToFile(`）。
- 基线 `grep -n 'invokeOnCancellation'` ⇒ `:707` 仅 `shutdown()`，无 `file.delete()` ⇒ C2 红。
- 基线 `:704` 在 `finish()` 之外，而 `finish()`（`:685-688`）是 `resumeWith` 的唯一宿主且**不删文件** ⇒ C3 红。
- 报告与真树的两处差异（已按真树口径改写）：报告写 `:678-716`（真 `:679-716`，漂 1 行）；报告称「`OnInitListener` 无守卫」属实，但**未指出取消路径也不删文件**（这是第二个缺口，单独一条判据 C2 钉住）。

### 1.3 受影响面（为什么是 P1 而不是 P2）

取消是**常用路径**：15 s 超时（卡住的引擎）与 sheet 关闭（用户切走）都会走 `invokeOnCancellation`。每次命中在 `cacheDir` 留下一个 WAV（内容为一句英文测试语音，十几 KB 起），且没有任何 GC 机制。

---

## 2. 修法（改动阶梯）

**停在第 7 级（最小实现），因为**：仓库里没有等价的「取消闩锁」helper（`grep -rln 'invokeOnCancellation' scripts/scan/` 只有 a11y 门、`grep -rln 'suspendCancellableCoroutine'` 只有 2 个与 TTS 无关的文件）；Kotlin 标准库 + 已装 `kotlinx-coroutines` 提供 `AtomicBoolean`；平台没有「取消后禁止写」的原生开关；一行写法放不下三处 guard + 两处 delete；因此按最小实现落地：**一个无状态纯策略 object + 一个 AtomicBoolean 闩锁 + 三处 guard / 三处 delete**，不重写 TTS 封装、不动调用方 UI。

### 2.1 形状

```kotlin
val cancelled = java.util.concurrent.atomic.AtomicBoolean(false)   // 只有取消路径翻它

fun finish(ok: Boolean) {
    runCatching { tts?.shutdown() }
    if (SynthClipPolicy.mustDropOnCallback(cancelled.get())) runCatching { file.delete() }  // 迟到回调 = 迟到写入
    if (cont.isActive) cont.resumeWith(Result.success(ok))
}

tts = TextToSpeech(context) { status ->
    if (!SynthClipPolicy.mayTouchEngine(cancelled.get())) { shutdown; return@TextToSpeech }   // 入口守卫
    … language / progress listener …
    if (!SynthClipPolicy.maySynthesize(cancelled.get())) { shutdown; return@TextToSpeech }    // 窗口重查
    val result = tts?.synthesizeToFile(…)                                                     // 唯一的写盘点
    …
}

cont.invokeOnCancellation {
    cancelled.set(true)
    runCatching { tts?.shutdown() }
    runCatching { file.delete() }        // 取消路径再删一次（写入可能已在途）
}
```

三个删除点各自对应一种「片文件比调用方活得久」的路径：退出路径（`finally`，成功读完才删）、取消路径（在途写入）、迟到回调（写入在删除之后才落地）。

### 2.2 否决的方案（写下来是为了下一个人不要再走一遍）

| 方案 | 否决理由 |
|---|---|
| 用 `cont.isActive` 当闩锁 | 它在 `invokeOnCancellation` **之后**才翻转，且 `finish()` 成功 resume 后同样变 false —— 与「是否被取消」不是同一个语义；回调里读它会误判成「已取消」而丢掉正常结果 |
| 把 `finally` 延后到引擎回调结束 | 延长协程/引擎生命周期，15 s 上限失效；任务书亦明确不推荐 |
| `clearDraft` 式「把删除挪到更后面」 | 删除点再多也追不上任意晚的写入；必须**从源头禁止写入** |
| 重写 TTS 封装 / 改调用方 | 超出半径，且 `TextToSpeechManager` 是 `android-speak` 通道的公共面，动它要过的门与回归面远大于本缺陷 |

### 2.3 必须保住的语义（逐条自检）

| 约束（任务书 §3） | 现状 |
|---|---|
| `withTimeoutOrNull(15_000)` 15 s 上限 | 未动 |
| `finish(ok)` 的 `tts.shutdown()` | 未动（迟到回调路径也仍在 `finish` 里 shutdown） |
| 返回 `ByteArray?`（null = 引擎失败） | 未动；`catch` 分支原样 |
| 成功路径 `file.readBytes()` | 未动（成功路径不触发任何删除，直到 `finally`） |
| 不引入主线程阻塞等待 | 未动（全部是 looper 回调 + `AtomicBoolean` 读） |
| 不把 `shutdown()` 移出取消路径 | 保留在 `invokeOnCancellation` |
| 不吞取消异常后继续合成 | 合成点前置 `maySynthesize` 守卫 |

---

## 3. 改动清单

`git diff --stat f7aba218 HEAD`（HEAD = `188858b6`）：

| 文件 | +/− | 类别 |
|---|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/ui/components/SynthClipPolicy.kt` | +43 | 生产（新文件，Android 无关纯策略，供 JVM 装置与单测驱动） |
| `src/android/app/src/main/java/com/rikkaminis/app/ui/components/QuickTestSheet.kt` | +34 / −3 | 生产（`synthesizeTestClip` 闩锁 + 三处 guard / 三处 delete + KDoc 记录失效链） |
| `src/android/app/src/test/java/com/rikkaminis/app/ui/components/SynthClipPolicyTest.kt` | +120 | 测试（4 个生命周期序列，随 CI `testReleaseUnitTest` 跑） |
| `scripts/scan/tts_voice_and_sanitizer_check.py` | +536 / −1 | 门（扩既有 TTS 门：3 条判据 + 7 个新臂） |

**合计 4 文件 +730 / −3**。门数不变（**扩既有门，0 新建**，`scan.sh` / `test_scan.py` **零触碰**）。

---

## 4. 门与判据

挂点选择：`grep -rln 'QuickTestSheet' scripts/scan/` ⇒ 只有 `provider_remember_key_check.py`（C15b/C16「编辑目标消失」契约，语义无关）；按**概念**找最近的域门 ⇒ `tts_voice_and_sanitizer_check.py`（TTS 子系统的既有门：语言通道、sanitizer 接线、逐调用默认值、错误码表）。本缺陷是**同一个 TTS 子系统的第三个引擎用户**（`TextToSpeechManager` / `SpeakOffloadHandler` / Quick Test clip），故扩该门而不新建第 110 道门。

### 4.1 三条判据（轴 = 派生，不锚拼写）

| id | 判据 | 派生方式 |
|---|---|---|
| `tts-clip-guard` | C1：`suspendCancellableCoroutine` 块内，**每个**引擎 SETUP 语句（`?.language =`、`?.setOn*Listener(`）之前必须有取消守卫；**每个** WRITE（`synthesizeToFile(` / `writeText(` / `writeBytes(` / `createNewFile(` / `copyTo(` / `renameTo(`）之前必须有守卫，且该守卫必须在**最后一个 SETUP 语句之后**（这两者之间的窗口正是缺陷所在） | 守卫里的「取消状态」不写死名字：条件必须读一个布尔（`.is[A-Z]…(` / `.get(`）且**接收者**是取消路径自己提到过的标识符（或协程的 `cont`）⇒ 灯光改名为 `aborted` / `gate` 都不失明。守卫必须与调用**同一/外层块**（嵌在兄弟回调 `onStart { … }` 里的守卫不算 —— 它支配不到那个调用），且守卫体里必须有早退 `return` |
| `tts-clip-cancel` | C2：块内 `invokeOnCancellation` 必须存在，体内**既有** `shutdown` **又有** 片文件删除（派发与取消之间引擎可能已经写完 ⇒ 取消路径必须再删一次） | 片文件变量从 `val <name> = File(context.cacheDir, …)` 派生，不写死 `file` |
| `tts-clip-cleanup` | C3：`finally` 仍必须删除片文件；且 `resumeWith(` 所在的块里必须有「取消状态为真 ⇒ 删除片文件」的守卫分支（迟到 `onDone`/`onError` 就是迟到写入） | `resumeWith` 是「把结果交回调用方」的结构锚点 ⇒ 交回处同时负责丢弃无人接收的文件 |

实现细节（防第 N 次「判据被注释/字符串骗过」）：先 `strip_comments`（**保留换行**，行号可用），再 `mask_strings`（把字符串/字符量（含 `"""`）**逐字符抹成空格、保留偏移与换行**）—— 片文件路径是模板字符串 `"quicktest-clip-${System.currentTimeMillis()}.wav"`，不抹掉的话花括号会打乱配对，整个块定位全错。文件缺失 = 红（fail-closed，不静默跳过）。

### 4.2 带牙臂（22 臂，`--selftest` 全绿 = `TTS-VOICE GATE ALL ARMS MATCH`）

既有 15 臂不变；新增 7 臂，**每个变异只红它自己那一条**：

| 臂 | 期望红 | 实测 |
|---|---|---|
| **PRE**：基线形状（无闩锁） | `{guard, cancel, cleanup}` | ✅ 三条全红 |
| 合规形状 | 全绿 | ✅ |
| 去掉回调入口守卫 | `{guard}` | ✅ 恰 1 |
| 只留入口守卫（窗口不重查） | `{guard}` | ✅ 恰 1 |
| 守卫挪到 `synthesizeToFile` **之后** | `{guard}` | ✅ 恰 1 |
| 取消路径保留 shutdown、去掉再删 | `{cancel}` | ✅ 恰 1 |
| 迟到回调不再丢弃片文件 | `{cleanup}` | ✅ 恰 1 |
| 去掉 `finally` 的删除 | `{cleanup}` | ✅ 恰 1 |

PRE 臂的基线文件用 `git show f7aba218:<path>` 抽取到 `probe/base/QuickTestSheet.kt`，**直接喂真判据**（不经夹具改写）。

---

## 5. 验证四层

### L1 门层

| 证据 | 值 |
|---|---|
| 本门真树 | `python3 scripts/scan/tts_voice_and_sanitizer_check.py` → `PASS`（rc=0） |
| `--selftest` | 22 臂全 PASS，收尾 `TTS-VOICE GATE ALL ARMS MATCH` |
| PRE 臂（真基线文件） | 5 条违规（C1 三处、C2 一处、C3 一处） |
| 全量门 | **108/108 PASS**（逐门直跑，`probe/l1/done/` 逐门留痕）；`scan.sh` 的第 109 步就是 `test_scan.py` 本身 |
| 夹具 | `python3 scripts/scan/test_scan.py` → `✅ ALL 336 CASES PASS`（rc=0），其中一行 `✅ tts_voice_and_sanitizer_check selftest has teeth (TTS-VOICE GATE ALL ARMS MATCH)` |

> 环境说明（不掩饰）：本地全量两次被 PRoot `Function not implemented`（sandbox-limits **S28**，7 支会话并行窗口）打断（`gate_run --full` 跑到 64/109 被回收）。健康路径 = **可续跑逐门运行器**（`probe/l1/run_gates.sh`，每门落一个 done 标记，重入跳过已完成），第一轮 75 门、第二轮补齐至 108/108、0 红，`test_scan.py` 单独跑 `ALL 336`。失败行**全部**带 `Function not implemented`，无一条是门判据红。

### L2 邻门层（按概念找的真实邻门，逐个 rc=0）

`a11y_handler_contract_check`（唯一另一个用 `invokeOnCancellation` 的门）、`provider_ua_parity_check`（另一个用 `suspendCancellableCoroutine` 的门）、`discarded_return_guard`（本次新增的 `runCatching { … }` 丢弃形态）、`provider_remember_key_check`（**扫同一文件**，确认 C15b/C16 未被我改动影响）、`mediaplayer_release_guard` / `share_dir_prune_check`（同目录 `ui/components` 的资源释放类门）—— **6/6 rc=0**。

### L3 装置层（真 kotlinc 2.0.21 + JUnitCore，5 臂）

装置把 `SynthClipPolicy`（生产文件，sha256 `e808ba4a…` 与仓库逐字节一致）与**仓库自己的测试文件** `SynthClipPolicyTest.kt` 一起编译运行；测试只模拟**事件顺序**（init 回调 / 写盘行 / 取消 / 迟到写入 / 进度回调），每一步的决策都由生产策略函数给出，期望值是字面量。

| 臂 | 内容 | 结果 |
|---|---|---|
| POST | 真策略 | `OK (4 tests)` |
| **PRE** | 基线语义复刻（无闩锁：`mayTouchEngine/maySynthesize` 恒真、`mustDropOnCallback` 恒假） | **3 红**（迟到 init / init 窗口内取消 / 取消后迟到写入），恰是三条缺陷路径 |
| m1 | `mayTouchEngine` → 恒真 | **恰 1 红**：`lateInitCallbackNeverTouchesTheEngineOrDispatchesTheWrite` |
| m2 | `maySynthesize` → 恒真 | **恰 1 红**：`cancelInsideTheInitWindowStillAbortsTheWrite` |
| m3 | `mustDropOnCallback` → 恒假 | **恰 1 红**：`writeThatLandsAfterCancellationIsDroppedByTheCallback` |

（PRE 臂用「复刻基线语义」而非基线文件：基线**没有**这个策略文件，其语义就是「每步都放行」。这一降级在报告内明写。）

### L4 CI 层（分支 `fix/dv26-g10-tts-cancel-guard`）

| 判据 | 值 |
|---|---|
| run / job | `39947618231` / `120394031065`，`run_attempt=1`，job `build` **success** |
| head_sha 三方逐字符 | 本地 HEAD = `git ls-remote` = `run.head_sha` = `188858b6968400ab35ebc57664dbc12ba8f77593` |
| 步骤 | 26 步，`not_ok_steps=[]`；唯一 skipped = `Publish to Releases`（main 门控，符合预期） |
| 门汇总 | `║  Summary: 109 passed, 0 failed`（9 条 `Summary:` 行全部 failed=0，无一条非零） |
| 门头 | `[1/109] … [109/109]` **序号连续、109 个门名互异** |
| 夹具 | `✅ ALL 336 CASES PASS` |
| 本门在册 | `tts_voice_and_sanitizer_check: PASS` + `✅ … selftest has teeth (TTS-VOICE GATE ALL ARMS MATCH)` + `✅ … real tree green` |
| 新测试 | `SynthClipPolicyTest` 4 条逐条 `PASSED`（`normalCompletion…` / `lateInitCallback…` / `cancelInsideTheInitWindow…` / `writeThatLands…`），由 `./gradlew testReleaseUnitTest` 真跑 |
| 编译错 `Z e: ` | 0 |
| 游离 FAILED | 6 条含 `FAILED` 的行**逐条**确认全是 `PASSED` 用例名的子串（0 条游离） |
| 日志 | 1,433,207 B（`probe/ci/job-120394031065.log`） |

---

## 6. 残留与负决策

| id | 残留 | 触发条件（复活时怎么处理） |
|---|---|---|
| **R1** | 本修复**之前**已经在 `cacheDir` 里产生的孤儿 `quicktest-clip-*.wav` 不主动清理 | 真机上 `ls cacheDir` 见到多份 `quicktest-clip-*.wav` 时，补一次性启动清理（按前缀 + mtime 超 1 天删），不要在合成路径里加扫描 |
| **R2** | 若引擎在 `shutdown()` 后**既完成了写盘、又再也不回调**（`onDone`/`onError` 都不来），最后一次删除之后落地的文件仍会残留 | 真机复现「取消后 cacheDir 仍出现新片文件」时，把删除改为「延迟删除 + 下次进入合成前按前缀清扫」（即 R1 的机制） |
| **R3** | 15 s 超时仍是硬编码字面量（既有形状，未命名常量、无理由注释） | 有设备在 15 s 内完成不了初始化（真机日志见 `onInit` 晚到）时，提为具名常量并写理由 |
| **R4** | 门 C1 只认「守卫 + 早退」形态；若将来改写成 `if (alive) { synthesizeToFile(…) }` 的正向包裹，会被判红 | 采用正向包裹写法时，给 C1 补第二形态（调用位于取消检查的正分支体内）——先扩判据再改代码 |
| **R5** | `SynthClipPolicy` 是无状态 `object`；若将来需要去抖/重试，必须改成实例 | 出现「同一 sheet 连点两次」的重复合成需求时 |
| **R6** | L3 测试只建模事件顺序，不建模线程可见性（`AtomicBoolean` 提供的语义） | 若把闩锁换成普通 `var`/`@Volatile` 且跨线程读写，测试不会红 —— 由 C1 的「接收者必须是取消路径提到的标识符」+ 代码评审兜底 |
| **R7** | 门的「引擎 SETUP」集合只认 `?.language =` 与 `?.setOn*Listener(`；若换用 `setAudioAttributes` / `setSpeechRate` 等初始化调用，窗口判据会漏 | 给 `synthesizeTestClip` 增加其它引擎配置调用时，先扩 `TOUCH_SETUP_RE` |
| **R8** | 未做真机行为验证（本支无设备） | 下次真机窗口：①切断引擎（或用超短超时）触发取消 ②`ls cacheDir` 确认无新增 `quicktest-clip-*.wav` ③正常路径仍能转写成功 |

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f7aba218`（`git log --oneline -1` 确认父提交） |
| 生产 + 测试提交 | `73215401` `[dv26/G10] TTS test clip: no side effect after cancellation` |
| 门提交 | `188858b6` `[dv26/G10] tts gate: pin the clip's cancellation latch` |
| CI run / job | `39947618231` / `120394031065`（workflow `build-apk.yml`，ref `fix/dv26-g10-tts-cancel-guard`，attempt 1，success） |
| CI head_sha | `188858b6968400ab35ebc57664dbc12ba8f77593` = 本地 HEAD = `git ls-remote`（三方逐字符一致） |
| 判读 | 26 步 `not_ok=[]`，唯一 skipped = `Publish to Releases`；`109 passed, 0 failed`；`ALL 336`；`Z e:` 0；游离 FAILED 0；新增 4 用例逐条 PASSED（详见 §5 L4） |
| 报告提交 | 本文件在 CI 绿之后追加（仅 `fix-reports/`，不改源码树）⇒ `git diff --name-only <CI 绿 sha> HEAD -- . ':(exclude)fix-reports'` 为空 |

---

## 8. 自证成本与不确定项

**本轮自伤（当场闭环，形态可复用）**：

1. **协程块起点取错**：首版写 `code.find("{", coro.end() - 1)`（`coro` 已匹配到 `->`），取到的是 `fun finish(…) {` 的那个花括号 ⇒ C1 的 touch 列表为空、C2 报「块内没有 `invokeOnCancellation`」、C3 用错块判定。真树一跑就红（3 条误报），当场定位为 `find` 起点，改为 `coro.start()`。**教训**：把「匹配到的锚点之后第一个 `{`」当块起点前，先确认锚点本身**在**那个块之外。
2. **测试断言顺序错**：`writeThatLandsAfterCancellationIsDroppedByTheCallback` 初版在「迟到写入落地」之后断言「取消路径已删」，POST 臂立刻红 —— 断言写在了事件顺序的反面。改为「迟到写入确实落地（`assertTrue`）→ 回调把它丢弃（`assertFalse`）」，这才是缺陷的形状。
3. **模拟器把 abort 当继续**：初版 harness 无条件调 `synthLine()`，于是 m2（`maySynthesize` 恒真）连带红了「迟到 init」场景（2 红），臂不再精确。改为 `initCallback(): Boolean`（返回是否继续），m2 回到**恰 1 红**。
4. **删掉冗余真值表测试**：`policyIsAMonotoneLatch` 直接断言三个策略函数，任何变异都会连带红它 ⇒ 变异臂不精确，且它对策略输出的覆盖已被四个生命周期场景全覆盖，故删除。
5. **沙箱 S28 风暴**：全量门两次被 `Function not implemented` 打断（含 `gate_run --full` 到 64/109 时整壳被回收），改为可续跑逐门运行器 + 分轮补齐；另有一次 `tail`/`cat` 自身 ENOSYS（`/dev/null` 都打不开），说明是环境而非命令问题。

**不确定项**：

- 真的引擎在 `shutdown()` 之后是否还会投递 `onInit`/`onDone`，取决于具体 TTS 实现（Google TTS / 小米引擎）。本修复在**两种实现下都安全**：来得早 → 入口守卫拦住；来得晚 → 迟到写入被 `finish` 丢弃。
- `synthesizeToFile` 的写入时机（立即 / 完成后）在不同实现上不同，故删除点覆盖「取消时」与「回调时」两处，而非只留 `finally`。
- CI 层能否通过，取决于 Compose 侧编译（本支改动只在函数体内，未引入新 import、未动签名），最终以 CI 绿为准。

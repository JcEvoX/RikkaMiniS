# FIX-D7 — 两处 WebView `onRelease` 用空操作 `removeCallbacks(null)` 且无销毁闩锁（S-07 / P1）

- 支：D7 ／ 工单 **S-07**
- 分支：`fix/dv26-d7-webview-release-latch`
- 基线：main `f7aba218`（`d91fb8bc2e9c49af19a07356c9c74b4f`）
- 生产文件：`ui/markdown/KaTeXView.kt`、`ui/sandbox/FilePreviewScreen.kt`（+ 新增共享 `ui/preview/WebViewReleaseLatch.kt`）
- 门：扩既有 `scripts/scan/browser_destroy_wakeup_check.py`（**未新建门，未改 `scan.sh` / `test_scan.py`**）

---

## ① 缺陷与真树取证（基线 f7aba218）

三条互不相同、但同属「WebView 已销毁，队列里的活照跑」这一族：

**D1（两处同形）`removeCallbacks(null)` 是空操作。**
基线 `KaTeXView.kt:390-393` 与 `FilePreviewScreen.kt:490-494` 的 `onRelease` 都是

```kotlin
wv.removeCallbacks(null)
wv.stopLoading()
wv.destroy()
```

AOSP `android.view.View.removeCallbacks(Runnable action)` 的实现是
`if (action != null) { … } return false;` —— **传 null 直接返回、一个都不删**。
基线注释（`KaTeXView.kt:384-389`）恰恰把它当成了「drops everything queued on this view」，
即这条 teardown 自诞生起就没有取消过任何东西。

**D2（KaTeX 专属）100 ms 延迟捕获块无销毁守卫。**
`KaTeXView.kt:312-333`：`post { … postDelayed({ draw(canvas); KaTeXRendererCache.cache.put(cacheKey, …) }, 100) }`。
块内不读任何「已销毁」状态。首次成功渲染会立刻把 `renderedBitmap` 置位、
AndroidView 退出组合 → `onRelease` → `destroy()`；而 100 ms 后的块照常执行：
`draw(canvas)` 在已销毁的 WebView 上画出**空白位图**，然后
`KaTeXRendererCache.cache.put(cacheKey, entry)` 把它写进**进程级** LRU 缓存。
后果不是「这一次白」，而是**该公式在此后整个进程生命周期内都是空白**——
`remember(cacheKey) { cache.get(cacheKey) }` 命中这条毒条目，直接走 `Image` 分支，永不再渲染。

**D3（预览屏专属）排队中的 `loadUrl` 无处可取消。**
`FilePreviewScreen.kt:483`：`post { loadUrl(targetUrl) }`（注释自述是为了等首次布局）。
view 在首次布局前被关闭时，该 runnable 留在 `HandlerActionQueue` 里；
`removeCallbacks(null)` 删不掉它，一旦后续 attach / 或队列在 destroy 后被冲刷，
就把文件 URL 灌进一个已销毁的 WebView。

**对照物（不是「普遍写法」）**：`ui/preview/WebViewHolder.kt:437` 与
`browser/BrowserUseManager.kt:2849-2852` 在 dv25-B02 已改成「先 detach → 再 `removeCallbacks`」，
KDoc 明确写了 attached 视图上 `removeCallbacks(null)` 会连带清掉**共享 window handler**
的 null-token 消息。⇒ 顺序语义必须一起抄。

**任务书失真（已就地更正，见 §6 R8）**：任务书 §4 称
「`katex_ready_race_check.py`（该门已扫 `KaTeXView.kt`）」——真树里该门只扫
`ui/chat/KatexWebViewPool.kt` 与 `ui/chat/ChatViewModelStore.kt`（`KATEX` / `STORE` 两个常量），
**从未扫过 `KaTeXView.kt`**。真正扫描该文件的是 `katex_bridge_arity_check.py`。

---

## ② 修法（改动阶梯：**停在第 2 级「代码库里已有」+ 第 7 级「最小共享实现」**）

**第 1 级（真要做吗）**：P1 已判，且「毒缓存条目」的后果是永久性的，不修不可接受。

**第 2 级（已有）**：dv25-B02 给出了「latch → detach → 清队列 → stop → destroy」的既有约定
（`WebViewHolder.destroy()` / `BrowserUseManager` 的 recycle 路径）。本轮是这条约定的
**第 3、4 个调用点**——按「同族复发 ≥2 次 → 修在共享层」的纪律，顺序语义收敛到
`ui/preview/WebViewReleaseLatch.kt` 的 `releasePlan(attached)`（两份 listOf，纯函数、可 JVM 测），
两个 host 各自 `for (step in releasePlan(...)) when (step) { … }` 执行，
**不再各写一遍顺序**（FIX-10 的教训：同一契约两份实现 = 漂移面）。

**为什么不引入 `removeCallbacks(null)` 的替代 bulk 清理**：没有正确的 bulk 形式可抄
（attached 时 `null` 语义不明、detached 时它照样不删）。所以本轮把它**整体删除**，
改为**按身份移除**（`View.removeCallbacks(runnable)` 是唯一真正生效的形式），
并用闩锁兜住「注册与销毁之间的窗口」。

**新增（唯一新文件）`ui/preview/WebViewReleaseLatch.kt`（119 行，Android 无关）**：
- `released: AtomicBoolean` + `release(): List<Runnable>`（CAS 幂等，只第一次返回待删清单）
- `track(task)`：登记具名 runnable；**release 之后登记的直接丢弃**（防复活）
- `canRunDeferred()` / `isReleased`：延迟块的第一句守卫
- `pendingCount`：注册表可观测（测试用，见 §5 的 m4 臂）
- `enum ReleaseStep` + `releasePlan(attached)`：attached 时 `DETACH → DROP_DEFERRED → STOP_LOADING → DESTROY`，
  已 detach 时省掉第一步。

**两处 host 的改法**：
- `KaTeXView.kt`：闩锁建在 `key(cacheKey, rendererEpoch) { … }` **内部**（key 变 ⇒ 新闩锁，
  旧闩锁绝不交给下一个 WebView）；`onRendered`（JS 桥线程入口）读闩锁早退；
  `post { … }` 与 100 ms 捕获块（改成具名 `val capture = Runnable { … }` + `track`）**各自首句**读闩锁；
  `onRelease` 换成 `releaseLatch.release()` + `releasePlan` 循环。
- `FilePreviewScreen.kt`（`HtmlPreview`）：同一形状；排队的 `post { loadUrl }` 改成具名
  `initialLoad` + `track` + 首句守卫。

**刻意没做**（半径纪律）：`WebViewHolder.kt` / `BrowserUseManager.kt` 那两处仍保留
`removeCallbacks(null)`（它们已有 detach-先行，且那两行的 no-op 性质属既有 P2，见 §6 R1）；
不重写 `AndroidView` 结构、不动 `renderedBitmap` 替换机制、不动 `KatexCaptureLimit`。

---

## ③ 改动清单（5 文件，+898 / −20）

| 文件 | 变更 | 说明 |
|---|---|---|
| `ui/preview/WebViewReleaseLatch.kt` | **新增** +119 | 闩锁 + 具名登记表 + `releasePlan` |
| `ui/markdown/KaTeXView.kt` | +66 / −14 | 闩锁作用域、`onRendered` 早退、两跳守卫、具名 capture、`onRelease` 重写 |
| `ui/sandbox/FilePreviewScreen.kt` | +38 / −5 | `HtmlPreview` 的 `initialLoad` 具名 + 守卫 + `onRelease` 重写 |
| `src/test/.../ui/preview/WebViewReleaseLatchTest.kt` | **新增** +151 | 9 条 JVM 用例（行为 + 计划顺序） |
| `scripts/scan/browser_destroy_wakeup_check.py` | +524 / −1 | 新增 17 条判据 + 13 个自检臂 + PRE 夹具 |

冻结面（`scripts/scan/scan.sh`、`scripts/scan/test_scan.py`）**0 触碰**；
门本就已注册（`scan.sh:927` / `test_scan.py:2748`），**无需新建门、无需注册**。

---

## ④ 门与判据

扩既有 `browser_destroy_wakeup_check.py`（C4 段本就是「teardown 清队列」契约，
语义最近；不新建门 ⇒ 不碰注册面）。新增 `analyse_release_latch()` 与 `analyse_release_policy()`，
**每个 host 6 条 + 每块延迟体 1 条 + 每个具名 runnable 1 条 + 策略 4 条**，真树共 **33 条判据**（原 16 条）：

| 判据 | 轴（派生，不锚拼写） |
|---|---|
| 释放路径动手前先闩锁 | `onRelease` 体内 `.release()` 的位置 < 任何 `removeCallbacks(` / `releasePlan(` / `stopLoading()` / `destroy()` |
| 用派生的 teardown 计划 | 体内出现 `releasePlan(` |
| 按身份移除延迟活 | 体内存在 `removeCallbacks(<非 null 实参>)` |
| 仍会 stop 再 destroy | `stopLoading()` 位置 < `destroy()`，两者都在 |
| （防御）`removeCallbacks(null)` 若回归，必须在 detach 之后 | 体内 `removeView(`/`detach(` 位置 < `removeCallbacks(null)` |
| 每个延迟块第一句读闩锁 | 从 `post {` / `postDelayed({` / `val x = Runnable {` 派生出**全部**块，逐块看首句 `if (…canRunDeferred()/isReleased…) return…`（守卫写在活之后 = 红） |
| 每个具名 runnable 都登记过 | 从 `val <name> = Runnable {` 派生名字，要求 `track(<name>)` |
| 策略：两份步骤表 + attached 先 detach 后 destroy + detached 无 detach + 闩锁 API 在场 | 从 `listOf(ReleaseStep.…)` 派生 |

**自检臂 40 个（原 27 + 新 13）**：合规绿臂、PRE 基线形状臂（红 6 条）、
以及「每判据一臂」——去掉捕获守卫 / 守卫挪到活之后 / 守卫被注释掉 /
不登记 runnable / 不闩锁 / 手工内联顺序 / 换回 `removeCallbacks(null)` /
删掉 destroy 分支 / 预览侧去守卫 / 计划丢 detach / 两份计划塌成一份。
每条臂的**期望红集由判据表手写**（不从运行结果推导），实际全部对上。
`--selftest` 打印 `BROWSER_DESTROY_WAKEUP_SELFTEST=PASS`。

---

## ⑤ 验证四层

### L1 门层
- 本门真树：`Summary: 33 passed, 0 failed` + `GATE=GREEN browser_destroy_wakeup`
- `--selftest`：**40 臂全对**，`BROWSER_DESTROY_WAKEUP_SELFTEST=PASS`
- **PRE 臂**（`git show f7aba218:` 五个文件 → `probe/base/root/`，门在基线上跑）：
  `rc=1`，**11 条红**，逐条落在 `KaTeXView.kt`（5）+ `FilePreviewScreen.kt`（5）+ 基线两个未守卫块：
  `release path touches the WebView before latching it` ×2、
  `does not use the derived teardown plan` ×2、`does not remove the deferred work by identity` ×2、
  `clears view callbacks with removeCallbacks(null) before detaching` ×2、
  `deferred block at line {312,315,483} does not stand down on release` ×3。
- 全量：**108 门 per-gate 直跑，106 PASS + 2 门（`calendar_events_nullable_predicate_check` /
  `tts_voice_and_sanitizer_check`）需 `--root` 形态，用 `--root` 跑同样 PASS ⇒ 108/0**
  （`probe/l1/run_gates.py`，抗 ENOSYS 重试；`scan.sh` 整链在本机遇 S28 风暴假红，见 §6 R9）
- `test_scan.py`：**`✅ ALL 336 CASES PASS`**（`probe/l1/run_test_scan.py` 重试后 rc=0）

### L2 邻门层（按概念找，不按任务书点名）
`grep -rln 'KaTeXView\|FilePreviewScreen\|WebViewHolder\|BrowserUseManager' scripts/scan/` 命中 9 道，
逐个 rc=0：`file_preview_url_encode_check` / `filepreview_print_bounded_check` /
`katex_bridge_arity_check` / `katex_ready_race_check` / `webview_holder_url_source_check` /
`browser_tabs_lifecycle_check` / `file_uri_parse_guard_check` / `browser_cookie_ui_refresh_check` /
`webapp_asset_url_cleanup_check`（另加 `offload_host_path_check` / `claim_release_guard`）= **11/11 rc=0**。

### L3 装置层（真 kotlinc 2.x + JUnitCore，`probe/l3/run_arms.py`）
| 臂 | rc | 结果 |
|---|---|---|
| POST（真 `WebViewReleaseLatch.kt` + 真测试类） | 0 | `OK (9 tests)` |
| PRE（闩锁失效 + release 不交回清单 = 缺陷语义） | 1 | 3 红：`deferred work stands down` / `release hands back …` / `release is idempotent` |
| m1 去掉守卫（`canRunDeferred` 恒真） | 1 | 恰 1 红（守卫用例） |
| m2 计划顺序颠倒（stop 先于 drop） | 1 | 2 红（attached 顺序 + drop 先于 stop） |
| m3 `release()` 不交回清单 | 1 | 2 红（按身份移除 + 幂等） |
| m4 双次 teardown 重复移除（去 CAS、不清表） | 1 | 2 红（幂等 + 注册表未排空） |
| m5 `track()` 无视 release | 1 | 恰 1 红（release 后登记被丢弃） |

**真树变异臂**（`probe/l3/realtree_mut.py`，复制真文件到 scratch root 施加单点变异）：
M1 删捕获守卫 ⇒ 恰 1 红；M2 删 `track(capture)` ⇒ 恰 1 红；
M3 把 `removeCallbacks(null)` 塞回内联（未 detach） ⇒ 恰 1 红；
M4 预览侧把 `releasePlan` 换成手写 `listOf` ⇒ 恰 1 红。
全程 `sha256` 前后一致（源树未被改动）。

**降级说明**：`KaTeXView.kt` / `FilePreviewScreen.kt` 依赖 Compose + Android WebView，
沙箱内无法编译（无 Android SDK 的 Compose 编译器插件），故 UI 侧以
「门 + 真树变异 + PRE 基线树」作装置；**决策逻辑（闩锁语义、计划顺序）已抽成 Android 无关
纯类并用真 kotlinc + JUnit 跑**，不是纯文本断言。

### L4 CI 层
见 §7（run id / job id / head_sha 三方一致 / 26 步结论）。

---

## ⑥ 残留与负决策（全部带触发条件）

- **R1（P2，登记不修）`WebViewHolder.kt:437` / `BrowserUseManager.kt:2852` 仍调
  `removeCallbacks(null)`**：同样是 no-op，但这两处已有「detach 先行」，且
  `browser_destroy_wakeup_check` 的 C4 判据与 `JsDispatchDecisionTest` 都**要求**它存在。
  改它要同时改门 + 测试 = 半径外。**触发条件**：下次改动这两处任一 teardown 时一并对齐
  到「按身份移除」，届时同步删两条判据与一条测试断言。
- **R2（P3）`onPageFinished` 内的 `evaluateJavascript` 未加闩锁**：页面回调只会由活 WebView 触发
  （`SafeWebViewClient` 转发链在 destroy 后不再投递）。**触发条件**：若出现「destroy 后仍有
  onPageFinished」的真机日志（`grep 'renderMath'` 出现在 destroy 之后），再加守卫。
- **R3（P3）`initialLoad` 与 `capture` 之外的 `post` 站点**：本轮只覆盖文件内现存的两个延迟块；
  门会**派生**全部 `post {` / `postDelayed({` / `Runnable {` 块，新增未守卫块会当场红。
  **触发条件**：门红即视为该残留复活。
- **R4（P3）真实行为级未验**：`releasePlan` 的 DETACH 分支在 Compose 里是否总能拿到非空 parent，
  取决于 `onRelease` 的调用时机（已 detach 时自动走无 detach 分支，两种都安全）。
  **触发条件**：真机 R1（见下）通过即视为闭环。
- **R5（负决策，不修）不给 `View` 加「批量清队列」helper**：没有正确的 bulk 形式；
  引入只会制造第二套语义。登记为负决策，复活条件 = 平台出现官方的 view-scoped clear API。
- **R6（P2）`removeCallbacks(null)` 的空操作性质未在代码库统一说明**：本轮只在两处新注释里写明。
  全局澄清需要动 BUM / WebViewHolder（= R1）。
- **R7（观测）`CacheEntry.bitmap` 毒条目的历史实例无从检测**：修复后新进程不再产生，
  但旧进程里被污染的条目会随进程结束消失，无需迁移。
- **R8（任务书失真）** 见 §1 末：`katex_ready_race_check.py` 并不扫描 `KaTeXView.kt`；
  已改扩语义最近的 `browser_destroy_wakeup_check.py`（其 C4 段本就是该契约）。
- **R9（环境）`scan.sh` 整链在本机报 `python3: Function not implemented`（registry S28，
  并发窗口放大）**，`Summary` 行是假红；改用 `probe/l1/run_gates.py` 逐门直跑取得
  108/0 的证据，`test_scan.py` 同样以重试方式取得 ALL 336。
- **真机行为级欠账 `§1046-D7`**：R1 公式滚出视口再滚回不空白（毒缓存不再产生）。

---

## ⑦ 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f7aba218`（`d91fb8bc2e9c49af19a07356c9c74b4f`） |
| 生产/测试/门提交 | `cd4ce3bb7c9d32061bed8e2f387c4d14fdb0c0a6` |
| CI 验过的提交 | `121e255e9257d7ea8f2b06fc88d73871e6f482e2`（当时 HEAD ＝ `ls-remote` 三方逐字符一致） |
| 本报告提交 | 在 `121e255e` 之上**仅 `fix-reports/` 变更**：`git diff --name-only 121e255e HEAD -- . ':(exclude)fix-reports'` = **0 文件**（已核）⇒ CI 验过的源码树就是当前源码树 |

### ⑦.1 CI 证据（两次独立绿，均为 26 步）

**Run A `39947373561` / job `120393218837` / attempt 1 / head_sha `cd4ce3bb` / `success`**
**Run B `39951345298` / job `120406293477` / attempt 1 / head_sha `121e255e` / `success`** ← 含报告文件的最新 HEAD

两 run 逐条判读结论一致：

- 步骤：26 步，非 success **唯一一条 = 23 `Publish to Releases` `skipped`**（main 门控，分支上不发布）。
- 门头：`[1/109] … [109/109]` **序号连续 109 条**。
- `Summary` 行 9 条，其中恰一条 `Summary: 109 passed, 0 failed`，另本门 `Summary: 33 passed, 0 failed`；
  **无任何非零 failed**。
- 本门三行在册：`GATE=GREEN browser_destroy_wakeup`、
  `✅ browser_destroy_wakeup_check selftest has teeth (BROWSER_DESTROY_WAKEUP_SELFTEST=PASS)`、
  `✅ browser_destroy_wakeup_check real tree green`。
- `✅ ALL 336 CASES PASS`（`test_scan`）。
- 编译错误 `Z e: ` **0 行**；含 `FAILED` 的 6 行逐条确认**全是 PASSED 用例名子串**，游离失败 0。
- 新测试类 `WebViewReleaseLatchTest` **9 条用例逐条 `PASSED`**。
- 日志大小：Run A 1,432,673 B / Run B 1,436,227 B（两跳取数、不带 token；`probe/ci/job-*.log`）。

## ⑧ 自证成本与不确定项

- 本轮最大不确定项是**「`removeCallbacks(null)` 是 no-op」这一事实的可验证性**：无法在沙箱里读到
  AOSP 源码，结论来自任务书 + AOSP 公开实现（`if (action != null) { … } return false;`）。
  **但修法对这条事实不敏感**：无论 null 是否生效，「按身份移除 + 闩锁」都是更强的形态，
  而门钉的是更强的形态。若该事实被证伪，回归的只是「R1 的两行是否也该跟着改」的优先级。
- 装置成本：新增门代码 524 行、自检臂 13 个。按修复门纪律审视——这是 **P1 + 永久性用户可见后果
  （公式全生命周期空白）**，且两个 host 同形复发（≥2 次），共享层修复 + 一门覆盖两处，
  装置规模与影响面匹配。
- 未被本装置覆盖的部分：Compose 组合生命周期本身（`onRelease` 的触发时机）——
  由 L4 编译 + 真机 R1 兜底。

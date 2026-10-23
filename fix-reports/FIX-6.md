# FIX-6 · deep-audit-1045（v24 波）· B-02（P1）

- **分支**：`fix/dv25-b2-webview-lifecycle`
- **基线**：main `1d44ec905b8f5d21a215694e9a13910c74e1f645`
- **工单**：B-02（P1）· WebView 生命周期三面
- **生产文件**：`browser/BrowserUseManager.kt` · `ui/preview/WebViewHolder.kt`
- **门（独占）**：`scripts/scan/browser_destroy_wakeup_check.py`（扩既有门，0 新建门）

---

## 1. 缺陷与根因（真树 Before 原文 + 实测行号）

B-02 报的是一条「destroy 之后回调仍可能打到 WebView」的族。回真树核实后，
它落在**三个互相独立**的面上，根因各不相同（不是同一个 bug 的三种说法）。

### 1.1 blob: 下载的 JS 注入进了永不排空的队列（P1 主症）

基线 `BrowserUseManager.kt:564-589`：

```kotlin
    private fun fetchBlobDownload(blobUrl: String, contentDisposition: String?, mimeType: String?) {
        val guessedName = android.webkit.URLUtil.guessFileName(blobUrl, contentDisposition, mimeType)
        // [audit2-A2] Arm the one-shot bridge credential for THIS fetch: the
        …
        """.trimIndent()
        webView.post { webView.evaluateJavascript(js, null) }
    }
```

**根因**：`View.post` 在 **DETACHED** 视图上只会把 runnable 塞进 `HandlerActionQueue`，
等一次 attach 才回放（`View.post` → `mAttachInfo.mHandler.post` 失败 →
`mRunQueue` 暂存）。agent 驱动的池 tab（`BrowserTabPool`）**整段生命周期都不 attach 到
Window**——它是离屏 headless 渲染器。⇒ 这条注入**永远不执行**。

**症状**：blob: 下载既没有落地文件，也**没有任何报错**——`evaluateJavascript` 的
value 回调根本没被调用；同时 `:566` 已经武装的一次性桥凭据（`blobDownloadActiveToken`）
悬空到下一次 fetch 才被覆盖。

**证据**：`webView.post` 在本文件里是**孤例**——其余所有主线程跳转都用
`Handler(Looper.getMainLooper()).post`（`:1829` setUserAgent）或既有的
`postAndAwait` 形态（`:1970-1971` / `:2006-2007` 带 timeout + `removeCallbacks`）。
`BrowserTabPool` 全文 0 处把池 tab 挂进 Window。

### 1.2 两个 `setUserAgent` 入口 + holder 的桌面切换没有 destroyed 闸门

基线 `BrowserUseManager.kt:1822-1842`：

```kotlin
    /** Set user agent from UI settings (public, non-result). */
    fun setUserAgent(profile: UserAgentProfile, customUA: String? = null) {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            Handler(Looper.getMainLooper()).post { setUserAgent(profile, customUA) }
            return
        }
        currentProfile = profile
        …
        if (ua != null) {
            webView.settings.userAgentString = ua
        }
        applyViewport()
        if (_currentURL.value.isNotEmpty()) {
            webView.reload()
        }
    }
```

代理侧重载（基线 `:1894-1913`）同样没有闸门。`WebViewHolder.kt:340-351`
（`toggleDesktopMode`）写 `webView.settings.userAgentString = DESKTOP_UA` 后
`reload()`，同样没有闸门。

**根因**：本文件 `destroyed` 这个位（`:2508`，`private var destroyed = false`）
**只有 destroy 自己读**——写 UA 的三个面一个都不问它。于是「比 tab 活得久的调用方」
（池的 `BrowserTabPool.kt:1253/1360/1551/1896` 切 UA、holder 的工具栏切换）会对
**已销毁**的 WebView 写 settings / reload ⇒
`Attempt to perform WebView method on a destroyed WebView`（Android 抛异常，非静默）。

### 1.3 两处 `destroy()` 漏清视图回调队列

基线 `BrowserUseManager.kt:2722-2771`（尾段）与 `WebViewHolder.kt:415-421`：

```kotlin
    fun destroy() {
        if (!destroyed.compareAndSet(false, true)) return
        try {
            detach()
            webView.stopLoading()
            webView.loadUrl("about:blank")
            webView.destroy()
```

**根因**：`onPageFinished` 会 `view.postDelayed({ … evaluateJavascript("resize") }, 80)`
（`BrowserUseManager.kt:802`）——**销毁后仍在队列里**的回调一旦被回放，就对着
已销毁的 renderer 画。参照实现 **KaTeXView.kt:390-393** 与
**FilePreviewScreen.kt:491** 都有 `removeCallbacks(null)`，且 KDoc 明写
「callback drew into a destroyed WebView」。本仓两处 destroy 漏了它。

---

## 2. 修法（停在第几级 / 被否决方案）

**停在阶梯第 6 级（接既有单源）**——三处都不是新机制：

| 面 | 既有单源 | 修法 |
|---|---|---|
| 1.1 | 本文件既有的主线程跳转通道（`:1829` 的 `Handler(Looper.getMainLooper()).post`） | `webView.post { … }` → `runJsOnMain("blob-download") { webView.evaluateJavascript(js, null) }`；`runJsOnMain` 是新写的小方法，但**它复用的就是既有通道**，不是新机制 |
| 1.2 | 本文件既有的 `destroyed` 位（`:2508`） | 三个写 UA 的入口首行问它；代理侧重载把拒绝写进答案槽 |
| 1.3 | KaTeXView / FilePreviewScreen 的三步 teardown | `stop → detach → removeCallbacks(null) → destroy` |

**被否决的方案**：

- **改用 `webView.post` 之外但同样入 View 队列的写法**（如 `view.postDelayed`）：同样不排空，无效。
- **给 blob 下载改走 `evaluateJavascript` 直调（不加 post）**：`DownloadListener` 回调不保证在主线程，
  WebView 方法非主线程即抛 `CalledFromWrongThreadException`。
- **以 `isAttachedToWindow` 作为闸门**：池 tab 永不 attach ⇒ 所有 headless 下载全废。**否决**——
  判据轴是「队列会不会排空」，不是「有没有窗口」。
- **在 `destroy()` 之后调 `removeCallbacks(null)`**：为时已晚，队列里的 runnable 仍可能在
  detach→destroy 的窗口内被回放；且任务书明确禁止（顺序：stop → 清回调 → destroy）。
- **`removeCallbacks(null)` 放在 detach 之前**：**否决且是安全理由**——`View.removeCallbacks(null)`
  在**仍 ATTACHED** 的视图上会连带清掉共享 window handler 的 null-token 消息（影响同一 window
  的其它视图）。必须先 detach（清掉 attachInfo）再清回调，此时只清本视图的 run queue。

**一句话根因**：*离屏 tab 没有窗口可挂 ⇒ View 队列不排空；而「已销毁」这个事实没有出口，
三个写面一个都不问它；teardown 又漏了队列清空。*

---

## 3. 改动清单

| 文件 | 增/删 | 行区间（改后） |
|---|---:|---|
| `src/android/app/src/main/java/com/rikkaminis/app/browser/BrowserUseManager.kt` | +149/−10 | `:564-610`（fetchBlobDownload 闸门 + 注入改道）、`:612-650`（`runJsOnMain` / `runJsNow`）、`:1873-1890`（公共 setUserAgent 闸门）、`:1954-2000`（代理侧重载闸门 + `destroyedTabResult`）、`:2805-2856`（destroy 清回调）、`:3040-3066`（`JsDispatch` / `jsDispatchFor` 纯决策） |
| `src/android/app/src/main/java/com/rikkaminis/app/ui/preview/WebViewHolder.kt` | +18/−0 | `:340-349`（toggleDesktopMode 闸门）、`:424-441`（destroy 清回调） |
| `src/android/app/src/test/java/com/rikkaminis/app/browser/JsDispatchDecisionTest.kt` | +244（新增） | 全文 |
| `scripts/scan/browser_destroy_wakeup_check.py` | +473/−16 | 判据常量 `:39-63`、`skip_string`/`strip_comments`/`brace_body`/`iter_functions`、`analyse_lifecycle`、9 个带牙臂 + 1 个基线臂 |

合计 **4 文件 +884/−26**。门数 **109 → 109**（0 新建门），夹具 **336 → 336**（未动
`scan.sh` / `test_scan.py` 冻结面）。

**刻意简化点**：

- `ponytail: blob 下载的注入用「一次性主线程 hop」而非「带 await 的 Deferred 通道」 |
  天花板：注入失败（如页面已卸载）时只是没有文件，桥凭据超时回收 |
  升级触发：若出现「下载静默失败」且日志无 `JS dispatch failed`，改走 `postAndAwaitJs` 形态。
- `ponytail: `removeCallbacks(null)` 一次清空本视图全部回调，而非逐个持有 Runnable 句柄 |
  天花板：无法区分「本视图的回调」与「别人的」 |
  升级触发：若某天需要在 destroy 后仍执行某个回调，改持句柄。

---

## 4. 门判据与带牙证明

门：`scripts/scan/browser_destroy_wakeup_check.py`（**独占**，扩既有门）。

### 4.1 判据（全部从真树派生，不写死成员清单）

| # | 判据 | 派生方式 |
|---|---|---|
| L1 | 武装一次性桥凭据的函数必须**先问 destroyed 再武装** | `ARM_RE = /blobDownloadActiveToken\s*=\s*(?!null)\w/` 定位函数 |
| L2 | 该函数体内不得出现 `webView.post` / `webView.postDelayed` | `VIEW_POST_RE` |
| L3 | manager 自有 WebView 上不得有任何 View 队列调度 | 全文 `VIEW_POST_RE` 扫描（基线红于 `:589`） |
| L4 | 运行时 UA 写入面（写 `settings.userAgentString` **且** `reload()`）必须问 destroyed | 遍历两个文件的**全部** fun，命中 `UA_WRITE` ∧ `reload(` |
| L5 | 两个 `destroy()` 体内 `removeCallbacks(null)` 必须在 `webView.destroy()` **之前** | 函数体顺序 |
| L6 | 同上，且必须在 detach/removeView **之后** | 函数体顺序 |

**L4 的精度控制**：构造期 `configureWebView`（`BrowserUseManager.kt:155`
`if (ua != null) userAgentString = ua`）**不 reload** ⇒ 天然豁免，不需要白名单——
判据按「写 UA ∧ reload」合取，构造期写入不满足合取。

### 4.2 带牙臂（selftest 10 臂，逐臂「改什么 → 红在哪条」）

| 臂 | 改什么 | 期望红（手写，不从运行结果推导） |
|---|---|---|
| clean | — | 0 红 |
| breaker-1 | 注入改回 `webView.post { … }` | L2 + L3 |
| breaker-2 | 删掉 `blobDownloadActiveToken = token` | 「no function arms the one-shot blob bridge credential」 |
| breaker-3 | 删掉 fetch 里的 destroyed 闸门 | L1 |
| precision-1 | 把闸门注释掉 | L1（注释不算闸门） |
| breaker-4 | 删掉 manager 公共 setUserAgent 的闸门 | L4(setUserAgent) |
| breaker-5 | 删掉 holder toggleDesktopMode 的闸门 | L4(toggleDesktopMode) |
| precision-2 | 加一个「构造期写 UA 但不 reload」的函数 | 0 红 |
| breaker-6 | 删掉 manager destroy 的 removeCallbacks | L5 + L6(manager) |
| breaker-7 | 删掉 holder destroy 的 removeCallbacks | L5 + L6(holder) |
| breaker-8 | 把 removeCallbacks 提到 detach 之前 | L6(manager) |
| baseline | **基线形态**（一次性还原 1.1/1.2/1.3） | 9 条生命周期判据**全红** |

**基线形态臂是全波最有说服力的一条**：它不是逐条打补丁，而是把基线树原文
（`webView.post { … }` / 无闸门的 UA 写入 / 无 removeCallbacks 的 destroy）
一次性塞回夹具，要求**恰好 9 条**判据红且不多不少——多红一条说明判据有重叠，
少红一条说明有盲区。

**自检通过**：`python3 scripts/scan/browser_destroy_wakeup_check.py --selftest` →
`BROWSER_DESTROY_WAKEUP_SELFTEST=PASS`（27 臂全绿）。

### 4.3 词法器修复（顺带修，门自身的历史缺陷）

原 `strip_comments` / `brace_body` / `function_body` 的字符串扫描只认简单字符串，
遇到 **Kotlin raw string**（`"""…"""`）与 **`"${…}"` 模板**会提前闭合 ⇒
吞掉整段函数体 ⇒ 派生判据读到错文本。实测症状：本门首版真树跑出
`the user-agent switch (interceptMinisURL) writes settings/reload…`——把
`shouldInterceptUrlLoading` 当成 UA 写入面。

修法：字符串扫描分三类（raw string / 模板 / 简单字符串）分别跳过；
模板内部再递归跳字符串。另：`strip_comments` 现在**保留换行**，使判据报告的行号
仍是原文件行号（基线树的 `View post queue` 现在精确报 `:589`）。

---

## 5. 验证四层结果

### L1 门

| 项 | 结果 |
|---|---|
| 本门真树 | `GATE=GREEN browser_destroy_wakeup`，`Summary: 16 passed, 0 failed` |
| 本门 selftest | `BROWSER_DESTROY_WAKEUP_SELFTEST=PASS`（27 臂） |
| **基线树**（`git show 1d44ec90:<path>` 三文件抽出到 `probe/base/`） | `Summary: 6 passed, **10 failed**` —— 10 条红，涵盖全部 9 条生命周期判据 + 1 条 View 队列扫描；`View post queue` 精确报 `:589` |
| 全量门 | `sh scripts/scan/scan.sh` → **`Summary: 109 passed, 0 failed`**，门头 109 个且连续，无 ENOSYS |
| 夹具 | `python3 scripts/scan/test_scan.py` → **`✅ ALL 336 CASES PASS`** |

### L2 邻门（按概念反查，非照抄任务书名）

`grep -rln "BrowserUseManager\|WebViewHolder" scripts/scan/*.py` → 7 道真实门，**7/7 rc=0**：

`browser_cookie_ui_refresh_check`（UA-pool 接线）· `browser_tabs_lifecycle_check`（池 teardown）
· `claim_release_guard`（销毁期结算）· `file_uri_parse_guard_check`（holder URL 解析）
· `webapp_asset_url_cleanup_check` · `webview_holder_url_source_check`（holder currentUrl）
· `browser_tab_exec_serial_check`（WebView 驱动串行）

### L3 真跑（真 kotlinc + JUnitCore）

纯决策 `jsDispatchFor` 抽到 `BrowserUseManager.kt` 尾部（与 `MinisMainDocGate`、
`decodeDataUrlPayload` 同放——这正是那两个 precedent 的存在理由），
真编译真跑，**7 个用例**：

| 臂 | 装置 | 结果 |
|---|---|---|
| **POST** | 当前树 | `OK (7 tests)` |
| **PRE（编译臂）** | 切片为空（决策在修复前不存在） | **编译红**：`unresolved reference 'JsDispatch'` / `'jsDispatchFor'` — 硬证据：决策此前不存在 |
| **pretree（基线树）** | `git show 1d44ec90:` 的两个生产文件 + 真切片 | `Tests run: 7, Failures: 3`：blob 闸门、UA 闸门、两处 destroy 回调清空 —— **三条缺陷各红一次** |
| m1 | 删 fetch 的 destroyed 闸门 | 恰 1 红（blob 闸门） |
| m2 | 删 manager 公共 setUserAgent 闸门 | 恰 1 红（UA 闸门） |
| m3 | 注入改回 `webView.post` | 恰 1 红（blob 闸门断言里的 View 队列检查） |
| m4 | 删 holder toggleDesktopMode 闸门 | 恰 1 红（UA 闸门） |
| m5 | 删 manager destroy 的 removeCallbacks | 恰 1 红（destroy 回调清空） |
| m6 | 删 manager destroy 的 detach | 恰 1 红（同一断言里的 detach 顺序检查） |
| m7 | 删 holder destroy 的 removeCallbacks | 恰 1 红（destroy 回调清空） |

装置 `probe/l3/`（`slice.py` 只做字节级切片与变异 · `arm.sh` 独占子进程）。
**未用 `git checkout --`**：每个臂是独立副本树，源文件全程不动。
变异通过**生产树副本**施加，测试代码不变 ⇒ 每次红都指向生产面。

### L4 CI

**#2550**（run `39920342153` / job `120303791910` / attempt 1，event=workflow_dispatch）：

| 判据 | 结果 |
|---|---|
| run 身份 | `head_sha=42686631113afe15f08923780c326ad5407bd022` == 本地 HEAD == `ls-remote`（三方逐字符一致） |
| 步骤 | 26 步，`not_ok=[]`，唯一 skipped = `Publish to Releases`（main 门控，符合分支预期） |
| 通用判读 | `judge_generic.py` → **ALL GENERIC CRITERIA GREEN**：9 条 Summary 中恰一条 `109 passed, 0 failed`、无非零；`ALL 336 CASES PASS` 恰一条；门头 `[n/109]` 序号 1..109 连续且 109 个门名互异；编译错 `Z e: ` **0**；含 `FAILED` 的 6 行逐条确认全是 PASSED 用例名子串（游离 FAILED 0） |
| 本门 | `Summary: 16 passed, 0 failed` + `GATE=GREEN browser_destroy_wakeup`（log:654/655）；`✅ browser_destroy_wakeup_check selftest has teeth (BROWSER_DESTROY_WAKEUP_SELFTEST=PASS)`（:1174）；`✅ browser_destroy_wakeup_check real tree green`（:1175） |
| 新测试 | `JsDispatchDecisionTest` **7 条逐条 PASSED、FAILED 0**（log:4382+） |
| 编译警告 | 新测试 0 条（`JsDispatchDecisionTest.kt:` 命中 0）——#2548 首次暴露的 `Java type mismatch: String? vs String` 已在 `42686631` 修掉，#2550 复验 |

**#2547 / #2548 说明**：#2547 是拆分提交前的 dispatch，被强推作废（cancelled）；
#2548 绿于 `0635d5a2`，但暴露了新测试的一处编译警告（CI 绿 ≠ 无警告，1042-A6 同族教训）⇒
修掉后重跑得 #2550。**两次 dispatch 的日志判读都基于 steps API（`conclusion`），不是日志文本**。

---

## 6. 任务书失真

| # | 任务书原文 | 真树 | 处置 |
|---|---|---|---|
| 1 | 「`:589` `webView.post { … }`——离屏**池** WebView 未 attach 到 Window 时 `View.post` 只是入 HandlerActionQueue，**永不执行**且返回 true ⇒ **调用方等 JS 结果，永久挂起**（blob: 下载路径）」 | 注入确实永不执行 ✓，但**不是「永久挂起」**：`:589` 的 `evaluateJavascript(js, null)` 传的是 **null valueCallback**，**没有任何调用方等结果**。真实症状是「下载静默不落地 + 一次性桥凭据悬空」，不是挂起 | 按真树改写报告 §1.1；修法不变（换通道） |
| 2 | 「`setUserAgent`（`:1823-1842`）：跨线程 post 有，但本体**无 `destroyed` 守卫** ⇒ 对已销毁池 WebView 写 `settings.userAgentString` / `reload()`」 | 行号与机制 ✓ | — |
| 3 | 「`destroy()`（`:2722-2771`）与 `WebViewHolder.destroy()`（`:415-421`）**漏 `removeCallbacks(null)`**」 | 行号与机制 ✓ | — |
| 4 | 「同文件其他入口用 `postAndAwait`/Deferred 形态（参照 `:1971`/`:2007` 的 timeout + removeCallbacks 模式）」 | `:1970-1971`/`:2006-2007` 是**导航等待**的 Deferred + timeout，不是 JS 执行通道；文件里**不存在** `postAndAwaitJs` 之类现成 JS-await 通道 | 改为复用同文件的**主线程 hop 形态**（`:1829`）；不新造 await 通道（见 §2 ponytail） |
| 5 | 「`:249-254` 的 `view.postDelayed` resize 回调」 | 真实位置是 `BrowserUseManager.kt:802`（manager 侧 onPageFinished）；holder 侧 resize 回调在 `:249` 附近的确存在（holder 自己也 postDelayed） | 两边都处理；holder 的 `removeCallbacks(null)` 一并清掉 |

---

## 7. 残留（R）

| # | 残留 | 触发条件 | 触发后方案 | 搁置理由 |
|---|---|---|---|---|
| **R1** | **真机行为级未验**：blob: 下载在离屏池 tab 上是否真的落地文件（不再静默丢） | 拿到真机窗口，用 agent 驱动一次 blob: 下载（`<a download>` 指向 blob URL） | 看 `BrowserUseManager` 日志有无 `JS dispatch failed`；看池 workspace 有无文件 | 沙箱无 WebView，只能真机验；CI/门/沙箱 JVM 三面已证机制 |
| **R2** | **真机行为级未验**：切 UA 后立刻销毁 tab，确不崩 | 真机上：切桌面模式 → 立刻关 tab / 切 UA → 立刻关 tab | 看有无 `Attempt to perform WebView method on a destroyed WebView` | 同上 |
| **R3** | **真机行为级未验**：池回收后无残留回调 | 真机上反复开关浏览器 tab 20 次，看有无 renderer 进程泄漏 | 若泄漏，查 `BrowserTabPool` 的 evict 路径是否绕过了 manager.destroy | 同上 |
| R4 | `runJsOnMain` 对注入失败只记日志（`JS dispatch failed`），不重试也不回答案槽 | blob 下载静默失败且日志出现该行 | 给 blob 通道接 Deferred + timeout（形状同 `:1970-1971`） | 半径纪律：任务书只要求换通道，未要求重试；成本 > 0 |
| R5 | `runJsOnMain` 的一次性 hop 不等待 JS 执行结果（仍是 fire-and-forget） | 需要「下载是否真的成功」进答案槽 | 同 R4 | 同上 |
| R6 | 公共 `setUserAgent`（`BrowserTabPool` 4 个调用点）遇已销毁 tab 时**静默 return**，调用方拿不到信号 | 池的 UA 同步路径需要知道「这次切换没生效」 | 让它返回一个布尔，由池侧决定是否重试 | 公共 API 改签名会波及 4 个调用点，超半径；代理侧重载已给答案槽 |
| R7 | `configureWebView` 构造期写 UA 仍无闸门（不需要：构造期对象必然是活的） | 若将来把 configureWebView 改成复用既有 WebView | 给该函数也加闸门 | 门判据按「写 UA ∧ reload」合取，构造期不满足，天然豁免 |
| R8 | 门判据 L1 依赖 `blobDownloadActiveToken = token` 这一**字面形态** | 该字段被改名/改写形式 | 从 ARM_RE 扩到「赋值给非 null 的桥凭据字段」的派生式 | 任务书第 9 条「纯核抽取收工厂/流不收路径」的同族；当前名字是本仓既有契约，字段改名会同时被 review 捕获 |
| R9 | 本支新增的 `JsDispatch` / `jsDispatchFor` 是**本文件内**的内部符号（`internal`），未进任何公共 API 面 | 无 | 无 | 符合 precedent（`MinisMainDocGate` 同为 internal object） |
| R10 | `WebViewHolder.toggleDesktopMode` 加闸门后，桌面模式状态 `desktopMode` **不再翻转**（早退发生在翻转前） | 用户在 WebView 已销毁后点桌面模式 | 无（UI 已不在） | 保持「不写死状态」与销毁顺序一致；翻转一个已死视图的状态没有意义 |
| R11 | 沙箱长跑 `scan.sh` 首次跑遭遇 S28（`Function not implemented` 批量报） | 沙箱负载高时跑全量门 | 静置后重跑即绿（本次第二次跑 rc=0，日志 60,552 B、ENOSYS 0 行） | 已知沙箱缺陷，非本次改动引入 |

---

## 8. 身份链

| 项 | 值 |
|---|---|
| 基线 main | `1d44ec905b8f5d21a215694e9a13910c74e1f645` |
| 本地 HEAD | `42686631113afe15f08923780c326ad5407bd022`（= `ls-remote` = CI `head_sha`，三方逐字符一致）|
| 远端分支 | `fix/dv25-b2-webview-lifecycle` @ `0635d5a2`（`git ls-remote` 自核一致） |
| 提交链 | `34aa5aba` 生产 → `dd70f0ca` 测试 → `0635d5a2` 门 → `9d6f1dca` 报告 → `42686631` 修新测试编译警告 |
| CI run | **`build-apk.yml` #2550**（run id `39920342153` / job `120303791910` / attempt 1，event=workflow_dispatch，head_sha `42686631113afe15f08923780c326ad5407bd022`）|
| 门 / 夹具 | 109 / 336（不变，0 新建门，冻结面 `scan.sh` / `test_scan.py` 0 触碰） |
| 停止节点 | **已达成**：#2550 `conclusion=success`，26 步 `not_ok=[]`，唯一 skipped = `Publish to Releases`（main 门控），`head_sha` 与本地 HEAD 逐字符一致 |

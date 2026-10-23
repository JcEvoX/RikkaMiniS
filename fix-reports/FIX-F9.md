# FIX-F9 — `BrowserExternalSchemeHandler` 的 `intent://` 消毒缺口 + 扫描门盲区（S-09，P1）

- 批次：deep-audit-1046（v24-S 单路报告）· 支 F9
- 基线 main：`f7aba218`（`d91fb8bc2e9c49af19a07356c9c74b4f`）
- 分支：`fix/dv26-f9-intent-scheme-sanitize`
- 停止节点：本支 CI 绿（不合并 main）

---

## 1. 缺陷与真树取证

`src/android/app/src/main/java/com/rikkaminis/app/ui/browser/BrowserExternalSchemeHandler.kt`
把**网页给的** `intent://` URL 直接 `Intent.parseUri` 后 `startActivity`，消毒只有一行
`intent.selector = null`。对照 `sandbox/offload/OpenOffloadHandler.kt`（`android-open`，同样由
模型/页面提供输入）的完整序列，缺 4 项 + 1 项注释失真：

| # | 缺口 | 基线取证（`f7aba218`） | 后果 |
|---|---|---|---|
| 1 | 未剥 URI 授权 flags、未清 `clipData` | `BrowserExternalSchemeHandler.kt:113-122` 只有 `:122 intent.selector = null`；全文件 `grep -n 'removeFlags\|clipData'` = **0** | 网页可让被拉起的 Activity 拿到对 `com.rikkaminis.app.fileprovider`（覆盖 `minis-global/`、`minis-sessions/`、`shared/`）的 read/write/persistable 授权；`clipData` 可夹带任意 URI + 授权 |
| 2 | 未剥**同包** `component` / `package` 目标 | 同上，全文件 0 处同包判定 | 同 UID 内部启动不受 `exported="false"` 保护 ⇒ 网页可拉起本应用的内部 Activity |
| 3 | 大小写 `INTENT://` 未归一 | `:113 Intent.parseUri(url, URI_INTENT_SCHEME)` 传的是原文；`:87` 只 lower 了用于分支判断的局部变量 `scheme` | `parseUri` 内部 `startsWith("intent:")` 区分大小写 ⇒ 混写大小写被静默降级成对原始字符串的 `ACTION_VIEW`，用户看到 `ERR_UNKNOWN_URL_SCHEME` / 死链（同一缺陷 1041-FIX-A6 已在 offload 侧修过一次） |
| 4 | `browser_fallback_url` 未校验 scheme | `:126 val fallback = intent.getStringExtra("browser_fallback_url")` → `:138-145 Intent(ACTION_VIEW, fallback.toUri())` 直接启 | `intent://…#Intent;S.browser_fallback_url=intent://…;end`，或 `file:///data/data/<pkg>/databases/minis.db`：消毒刚剥掉的逃逸被 fallback 原样重建 |
| 5 | KDoc 注释失真（§8.7 #14） | 文件头 `:22` 声称 `We strip FLAG_ACTIVITY_NEW_DOCUMENT`，全文件 0 处该 flag | 读者以为有这道防护；实际没有 |
| 6 | **门盲区**（本条的根因层） | `grep -rln 'BrowserExternalSchemeHandler' scripts/scan/` = **0**；`scan_parse_uri` 的调用点来自 `check_tree` 里 `for name in sorted(os.listdir(offload_dir))`，即**手写目录清单** `sandbox/offload/` | 全仓第二个 `Intent.parseUri` 调用点对这一族判据完全不可见。盲区不是「这个文件」，是「范围是人手列的」 |

**为什么成因是「两份实现」**：offload 侧在 1016c / 1041-A6 两轮里被反复加固（grant flags /
clipData / component / package / selector / 大小写各一次），而 browser 侧那份拷贝从未被同一族判据
读到过 —— 与 1045 波 B-06（`mergeSessionOverrides` 有两份、只有一份带守卫）同族。

---

## 2. 修法（改动阶梯）

**停在第 2 级「代码库里已有」，因为** 需要的消毒序列已经存在且在 offload 侧被 6 轮加固过；
缺的不是新逻辑，是**把它变成唯一实现并让门看见所有调用点**。第 7 级「另写一个 browser 专用的
最小消毒」正是本缺陷的成因（第二份实现），被否决。

1. `util/IntentSanitizePolicy.kt`（新，**Android 无关**）：三项决策 + scheme 归一
   - `isSelfTarget(target, selfPackage)` —— 判「这个 component/package 目标是不是我们自己」
   - `allowedFallbackUrl(raw)` —— fallback 只放行 http(s)
   - `lowercaseIntentScheme(url)` —— 从 `sandbox/offload` 包迁来（同一语义，两个调用点共享）
   放 Android-free 处的理由：这三条是**要钉住的判定**，纯函数才能用真 JVM 测试跑（L3）。
2. `util/IntentSanitizer.kt`（新）：唯一的消毒序列（`removeFlags(GRANT_*)` → `clipData = null` →
   同包 `component` / `package` 剥离 → `selector = null`），Android 侧薄翻译。
3. `OpenOffloadHandler.tryOpen`：内联序列 → `IntentSanitizer.sanitize(this, context)`；删除本地
   `lowercaseIntentScheme`（迁走）。**行为不变**（序列逐行搬过去，顺序与判据一致）。
4. `BrowserExternalSchemeHandler.handleIntentScheme`：
   `Intent.parseUri(lowercaseIntentScheme(url), …)` → `IntentSanitizer.sanitize(this, context)` →
   `IntentSanitizePolicy.allowedFallbackUrl(rawFallback)`，非 http(s) 的 fallback 记 INFO 日志后丢弃。
   `CATEGORY_BROWSABLE` / `FLAG_ACTIVITY_NEW_TASK` 保留；第三方 `package` / `component` 目标保留
   （拉微信/支付宝/Play Store 是功能面，`isSelfTarget` 只判本包，`*` 回归臂 m2 钉住这一点）。
5. KDoc 删除 `NEW_DOCUMENT` 失真声明，改写为实际行为（含「为什么第三方包名保留」）。

---

## 3. 改动清单（8 文件 +847/−95）

| 文件 | 性质 |
|---|---|
| `src/android/app/src/main/java/com/rikkaminis/app/util/IntentSanitizePolicy.kt` | 新 · 生产（Android-free，62 行） |
| `src/android/app/src/main/java/com/rikkaminis/app/util/IntentSanitizer.kt` | 新 · 生产（56 行） |
| `src/android/app/src/main/java/com/rikkaminis/app/ui/browser/BrowserExternalSchemeHandler.kt` | 改 · 生产（+52/−14 量级） |
| `src/android/app/src/main/java/com/rikkaminis/app/sandbox/offload/OpenOffloadHandler.kt` | 改 · 生产（内联序列 → 共享调用，−64/+20 量级） |
| `src/android/app/src/test/java/com/rikkaminis/app/util/IntentSanitizePolicyTest.kt` | 新 · 测试（5 用例，纯 JVM） |
| `src/android/app/src/test/java/com/rikkaminis/app/ui/browser/BrowserExternalSchemeShapeTest.kt` | 新 · 测试（4 用例，源形状） |
| `src/android/app/src/test/java/com/rikkaminis/app/sandbox/offload/PhotosImportShapeTest.kt` | 改 · 测试（重锚定到「委派 + 共享体」） |
| `scripts/scan/offload_host_path_check.py` | 改 · 门（+471）：R2 scope 派生 + 8 个 F9 臂 |

冻结面 `scripts/scan/scan.sh`、`scripts/scan/test_scan.py` **零触碰**；门总数仍 **109**、夹具仍 **336**（扩既有门，未新建门）。

---

## 4. 门与判据（`offload_host_path_check.py` R2）

### 4.1 判据的轴改成「派生」

| 判据 | 派生方式（不锚拼写） |
|---|---|
| **scope** | `_main_src_files(root)` 走发布树 `src/android/app/src/main/java/**/*.kt`，**不再有手写文件/目录清单** |
| 调用点必须消毒 | 每个 `Intent.parseUri(` 的语句窗口（括号配平）必须：①含 scheme 归一（`[Ll]owercase`）②含全量 strip，**或**调用「按函数体派生出的 sanitizer」（`derive_sanitizer_names`：body 里 `removeFlags(` + 四个 `FLAG_GRANT_*` + `R2_STRIPS` 全部成立） |
| dispatcher 必须接消毒 | 文件同时含 `intent:`/`android-app` 字面量 + `startActivity(` ⇒ 必须调用派生出的 sanitizer，或自身有完整的 inline 窗口（`hardened_inline`）。**同一文件已因站点被判红时不再叠加第二条消息**（一处缺陷一条报告） |
| fallback 必须网关 | `getStringExtra("browser_fallback_url")` 的调用必须是 `allowedFallbackUrl(<read>)` 包裹、或读出的 ident 之后被 `allowedFallback*` / `startsWith("http")` 网关住 |

「按 body 派生 sanitizer」是本轮最有价值的一条：**抽成 helper 不会让门失明，掏空 helper 会让所有
调用点重新变红**（臂 7 就是这条的证明）。同族反面先例：1045-B06 的封装让 `prefs_listener_holder_guard`
当场判红。

### 4.2 带牙臂（8 个，各臂断言 problem 数是**字面量**且消息含指定 needle）

| 臂 | 期望 | 结果 |
|---|---|---|
| clean：共享 sanitizer + 两个消费方 | 0 | PASS |
| dirty：browser 退化成只 `selector = null`（基线形态） | 1 | PASS |
| dirty：页面原始大小写进 `parseUri` | 1 | PASS |
| dirty：**第三个**裸 `Intent.parseUri` 调用点 | 1 | PASS |
| dirty：`intent://` dispatcher 从不消毒 | 1 | PASS |
| dirty：`browser_fallback_url` 无 scheme 网关直启 | 1 | PASS |
| dirty：掏空共享 sanitizer（少一个 grant flag）→ 两个调用点同时失守（传递性） | 2 | PASS |
| clean：inline 消毒（无共享 helper） | 0 | PASS |

`F9_INTENT_SCOPE_SELFTEST=PASS (8 fixtures: 6 dirty arms red on their own criterion, 2 clean trees green)`

> 注：`test_scan.py` 是冻结面，新 marker 未注册进它的夹具表 —— 收口时由主会话统一 register（本支已把 marker 打出来）。既有 marker `SELFTEST=PASS (7 fixtures: 4 violations caught, 3 clean trees green)` 一字未改，`ALL 336 CASES PASS` 已复核。

---

## 5. 验证四层

### L1 门层 ✅

- 本门真树：`offload_host_path_check: 22 handler file(s) + 2 intent dispatcher(s) scanned, 0 violations — OFFLOAD-HOST-PATH GATE OK (2 Intent.parseUri site(s), 1 browser_fallback_url read(s))` → rc=0
- `--selftest`：rc=0，`F9_INTENT_SCOPE_SELFTEST=PASS (8 fixtures …)`，其余既有块 `SELFTEST=PASS (7 fixtures…)` / `A08_R1_BLINDSPOT_SELFTEST=PASS` / `DEBUG_SESSION_PATH_SELFTEST=PASS` / `DEBUG_EXEC_SCOPE_SELFTEST=PASS` 全绿
- **PRE 臂（基线树）**：`scratch-f9/base-tree`（`git worktree add … f7aba218`）上 rc=1，且两条错误**精确指向**：
  - `BrowserExternalSchemeHandler.kt:113: Intent.parseUri without hardening — missing scheme normalisation, removeFlags(...), FLAG_GRANT_READ/WRITE/PERSISTABLE/PREFIX_URI_PERMISSION, clipData = null, same-package component strip, same-package package strip, selector = null`
  - `BrowserExternalSchemeHandler.kt:126: page-supplied browser_fallback_url is dispatched without an http(s) scheme gate`
  同一门在改造后的工作树上 rc=0 ⇒ 判据既不空转也不误红。
- 全量 `sh scripts/scan/scan.sh`：**`Summary: 109 passed, 0 failed`**（rc=0）
- `python3 scripts/scan/test_scan.py`：**`✅ ALL 336 CASES PASS`**（rc=0）

### L2 邻门层 ✅ 8/8 rc=0

按**概念**找（不按任务书点名）：`grep -rln 'BrowserExternalSchemeHandler|OpenOffloadHandler|Intent.parseUri|intent://' scripts/scan/` 与 `grep -rln 'startActivity|Intent(' scripts/scan/` 取并集后逐道跑：
`log_url_redaction_guard` / `offload_cli_args_check` / `deeplink_settings_routes_check` /
`file_provider_paths_coverage_check` / `file_uri_parse_guard_check` / `share_dir_prune_check` /
`browser_tabs_lifecycle_check` / `browser_destroy_wakeup_check` —— 全部 rc=0。

### L3 装置层 ✅ 真 kotlinc + JUnitCore，七臂

决策抽成 Android 无关纯函数（`IntentSanitizePolicy`），测试类 `IntentSanitizePolicyTest`（5 用例）真 JVM 跑：

| 臂 | 结果 |
|---|---|
| POST（发布版策略） | `OK (5 tests)` |
| **PRE**（`f7aba218` 行为：`isSelfTarget → false`、fallback 原样返回、scheme 不归一） | **恰 3 红**：`a same-package target is ours…` / `a fallback that is not http(s)…` / `only the scheme prefix is lower-cased…`。`third-party survives` 与 `fallback only survives as web url` **保持绿** ⇒ 半径精确（基线本来就没误清第三方） |
| m1 `isSelfTarget` 恒 false | 恰 1 红（同包未剥） |
| m2 `isSelfTarget` 只要非 null 就剥 | 恰 1 红（**第三方被误清** = 功能面回归） |
| m3 fallback 不网关 | 恰 1 红 |
| m4 整串 lowercase（payload 被改写） | 恰 1 红 |
| m5 完全不归一 | 恰 1 红 |

另外用 `android.jar` + `core-ktx` 桩独立编译 `IntentSanitizer.kt` + `IntentSanitizePolicy.kt` +
`BrowserExternalSchemeHandler.kt` → **rc=0，0 error**（`probe/l3/ktcompile2.log`）。

### L4 CI 层

见 §7（run / job / head_sha）。

---

## 6. 残留与负决策

| # | 残留 / 负决策 | 触发条件（何时复活） |
|---|---|---|
| R1 | **真机行为级欠账**：恶意 `intent://` 链接不外泄授权 / 切大小写 `INTENT://` 仍能拉起应用 / 非 http(s) 的 `browser_fallback_url` 被丢弃而不是死链 / 第三方包名（微信/支付宝/Play Store）仍能拉起 | 下次真机窗口；或用户报告「点了 intent 链接没反应 / 打开到了奇怪页面」 |
| R2 | `allowedFallbackUrl` 只做 scheme 前缀判定，不做 host 黑白名单（`http://127.0.0.1` 之类仍可当 fallback） | 若出现「fallback 打向本地端口 / 内网地址」的实际利用面需求 |
| R3 | 门对 `isSelfTarget` 的接受拼写是白名单式（`.packageName` / `selfPackage` / `isSelfTarget(` / `shouldStripSelfTarget(`）；换第三个名字（如 `isOwnPackage(`）会**误红**（fail-closed 而非漏报） | 若有人改名导致门红且确认非缺陷 ⇒ 扩 `PACKAGE_GUARD` 白名单 |
| R4 | `Intent.parseUri` 的 lowercase 判据锚在窗口内出现 `[Ll]owercase` 这个词，不校验「归一的确实是 parseUri 的入参」 | 若出现「归一了 A 却把 B 传给 parseUri」的真实写法 |
| R5 | 门扫的是 `main` source set；`src/test` 里 `assertTrue(body.contains("Intent.parseUri(…"))` 一类字符串字面量**故意不扫**（`strip_comments` 不剥字符串，会把测试夹具当真调用点） | 若将来有真实调用点落在 test source set（当前 0） |
| R6 | `IntentSanitizer` 只清「本包」`component`/`package`；同 UID 的兄弟包（如 `com.rikkaminis.app.debug`）视为第三方保留 | 若发布包与调试包同 UID 共存于同一设备并出现跨包拉起风险 |
| R7 | `PROBE` 装置目录（`probe/`）在本支 worktree 内、`.gitignore:141` 已覆盖，未入库 | 无（批次惯例） |
| R8 | 任务书 §1 说门名 `offload_host_path_check` 的 R2 只扫 `OFFLOAD_REL` —— 属实；但任务书建议的「每个调用点必须满足既有判据集（含 lowercase）」中 lowercase **此前并不存在于 R2**（是本支新增的判据），报告已按真树口径写 | 已在本报告更正；无后续 |

---

## 7. 身份链

| 项 | 值 |
|---|---|
| 基线 main | `f7aba218`（`d91fb8bc2e9c49af19a07356c9c74b4f`，`ls-remote` 与本地一致） |
| 生产/测试/门提交 | `57203319` |
| 报告提交 | 见分支尖端（源码树与 CI 绿 sha 等价，`git diff --name-only <绿sha> HEAD -- . ':(exclude)fix-reports'` 为空） |
| CI run | 见下（dispatch 于 2026-10-23T11:32:23Z） |

---

## 8. 自证成本与不确定项

- **自伤 1**：第一次把「基线树」`git worktree add` 到 `probe/base-tree`（仓库内）⇒ 全量门
  `parts_json_funnel_check` 把基线源码的 `ChatRepository.kt` 当成本树违规，**108 passed / 1 failed**。
  修法：基线树移到 `scratch-f9/`（worktree 之外）。
- **自伤 2**：`python3 scripts/scan/scan.sh`（它是 `#!/bin/sh`）⇒ SyntaxError。正确调用是 `sh scripts/scan/scan.sh`。
- **自伤 3**：L3 首版把策略与测试分两次 kotlinc 编译 ⇒ `error: cannot access 'fun lowercaseIntentScheme': it is internal`。
  Gradle 里 test source set 是 main 的 friend module，独立 kotlinc 没有这层关系 ⇒ 改为「策略 + 测试同一次编译」，
  不把 `internal` 降级成 `public` 去迁就装置。
- **自伤 4**：JUnit 运行漏 `kotlin-stdlib.jar` ⇒ 所有臂 `NoClassDefFoundError: kotlin/jvm/internal/Intrinsics`（5/5 全红 = 装置坏而非缺陷红，靠「POST 也全红」当场识别）。
- **不确定项**：`PhotosImportShapeTest` 是既有源形状测试，本支把它的断言从「tryOpen 里有内联 strip」改成「tryOpen 委派 + 共享体有全量 strip」。这不是放宽而是**加强**（覆盖了唯一实现），但属于对既有测试的语义修改，已在报告显式登记，供主会话复核。

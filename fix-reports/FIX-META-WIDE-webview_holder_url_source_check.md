# FIX-META-WIDE-webview_holder_url_source_check

- 分支：`fix/meta-wide-09-browser-webview`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 报告来源：组 A（wide-1024 宽扫，25 门/席）
- 生产代码改动：**0 行**（本波零 `.kt` 改动：只改门判据 + 自证臂）
- 冻结面：`scripts/scan/scan.sh` / `scripts/scan/test_scan.py` 未触碰

## 1. 门

`scripts/scan/webview_holder_url_source_check.py`

锁 `WebViewHolder` 的 URL 单一来源契约（`currentUrl` 必须是
`file://` 侧的值）、移动/桌面视口策略、以及 `ImageGalleryViewer` 的沉浸式窗口契约
（W6）。基线判据 23 条，本次 **+2**（W5f ×2、W6e），修 1 条（W1 从「调用了映射器」
改成「发布了映射结果」）。

## 2. 轴

| 见证 | 轴 | 新判据 |
|---|---|---|
| W1 `currentUrl` 映射被丢弃 | dataflow | W1 要求 RHS 的**值**是 `fromAssetLoaderUrl(...) ?: url`：映射调用落在 `.also { }` 里（返回接收者）即红 |
| W5 视口策略由 `initialScale > 0` 派生 | dataflow | W5f：移动分支每个赋值必须**拷贝策略自身的同名字段**（`mobile.useWideViewPort`），不能从另一个字段派生谓词 |
| W6 控制器由 null 窗口构造 | dataflow | W6e：`WindowInsetsControllerCompat(...)` 的构造前缀必须引用从宿主 Activity 取出的窗口本地变量 |

## 3. 见证（3 条，机械复证结论：打上补丁旧门 rc=0 = 门瞎）

| # | 见证书 | 文件 | 旧判据为何绿 |
|---|---|---|---|
| 1 | `W1 (onPageStarted_maps_fromAssetLoaderUrl)` | `ui/preview/WebViewHolder.kt` | 只查 RHS 里出现 `fromAssetLoaderUrl(` / `url` / `assetRootDir` 三个子串 + 负向正则禁行尾 `= url`；`url.also { fromAssetLoaderUrl(url, assetRootDir) }` 三个子串齐备、行尾是 `}` ⇒ 绿，而发布出去的是原始 asset-loader URL |
| 2 | `W5a..W5e (desktop_and_mobile_viewport_settings)` | 同上 | 只查 else 分支里出现 `mobileViewportSettings()` / `useWideViewPort =` / `loadWithOverviewMode =` / `setInitialScale(`；不查右值 ⇒ `mobile.initialScale > 0`（`initialScale` 恒 0 ⇒ false）双双关掉视口，反而绕过了 `= false` 负向正则 |
| 3 | `W6a..W6d (ImageGalleryViewer_immersive_window)` | `ui/components/ImageGalleryViewer.kt` | 只分别查 `(view.context as? Activity)?.window`、`WindowInsetsControllerCompat(`、`controller?.hide(` 三个子串；`(null as Window?)?.let { … }` 三者都在 ⇒ 绿，控制器恒 null，沉浸式静默失效 |

## 4. 修法

三条判据全部改到「值有没有到达使用点」：

1. **W1（值消费）**：新增 `in_discarded_lambda()` —— 从调用点向上找包围的 lambda，
   若其 head 以 `.also` / `.apply` 结尾，则该 lambda 返回的是**接收者**，内部计算结果
   被丢弃。W1 现在要求：映射调用存在 + 不在丢弃型 lambda 内 + RHS 是 elvis
   （`?:` 兜底仍在）。等价写法 `currentUrl = (fromAssetLoaderUrl(...) ?: url)` 仍绿。
2. **W5f（新增两条，每个字段一条）**：先定位 `toggleDesktopMode` 的 `else` 分支
   （`block_from()` 从 `else` 自己的 `{` 起算 —— 用 header-marker 切片会把闭合 `}`
   算进去、只切出头部一行，实测会造成「空判据假绿」），再要求该分支里
   `useWideViewPort =` / `loadWithOverviewMode =` 的右值严格是
   `<策略本地>.同名字段`（允许括号、允许取反 —— 极性归 W5e 管）。
3. **W6e（新增）**：绑定窗口的 `val <win> = … as? android.app.Activity …` 与构造
   `WindowInsetsControllerCompat` 的绑定要**串起来**；控制器绑定搜索遇到下一条声明
   即停止（否则上一行正确的 `val window = …` 会替下一条错误绑定背书）。

自证臂（全部固化进 `--selftest`）：W1 丢弃型 lambda 必红、括号版 elvis 必绿、
W5 `initialScale > 0` 派生必红（且 W5a/W5c/W5d/W5e 保持绿）、W6 null 窗口构造器必红
（且 W6a/W6b/W6c/W6d 保持绿）。

## 5. 四层证据

### L1 本门

```
python3 scripts/scan/webview_holder_url_source_check.py .              # rc=0, 真树全绿
python3 scripts/scan/webview_holder_url_source_check.py . --selftest   # 全臂符合预期（新臂红 / clean 臂绿）
```

自证臂数：good / bad1-3 / W5×4 / W6×2 / W7×6 + **wide-1024 四条**
（`mapper discarded via .also=RED`、`viewport derived from initialScale=RED`、
`controller built from null window=RED`，另加两条 clean 对照：括号版 elvis、
`if (d != null)` 等）。收尾标记 `WEBVIEW_HOLDER_URL_SOURCE_SELFTEST=PASS` 未改动。

### L2 见证反攻（判据链）

L2 用的**不是**我重写的补丁，而是任务书里逐字节的见证原文（脚本：`probe/witness_lib.py`，
按任务书解析 `### 见证` 块 → 打补丁 → 跑门 → 恢复 → sha256 校验）。

```
修复前同一补丁 rc=0（门瞎） → 修复后 rc≠0（门抓住） ⇒ 当初那把刀被挡住了
```

| 见证书 | 轴 | 修复后 rc | restore | 判决 |
|---|---|---|---|---|
| `W1 (onPageStarted_maps_fromAssetLoaderUrl)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |
| `W5a..W5e (desktop_and_mobile_viewport_settings)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |
| `W6a..W6d (ImageGalleryViewer_immersive_window)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |

判定：**本门 3/3 全抓住**，restore 全 True，工作树零污染（本支 5 门合计 **16/16**，逐条结果见 `probe/l2-*.json`）。

### L3 邻门（各 rc=0）

- `browser_history_snapshot_check.py` → rc=0
- `browser_tabs_lifecycle_check.py` → rc=0

### L4 全量（CI job 日志逐条判读）

- run `40134859565`（#2632）attempt 1 → **success**；job `120994111288`，26 步，
  `not_ok=[]`，唯一 skipped = `Publish to Releases`（main 门控，符合预期）
- `head_sha = 5f38473157a2c5291ecf6d0619ddc53b0d5af1b6`，与本地 `git rev-parse HEAD` 逐字符一致
- `Summary: 112 passed, 0 failed` 恰一条（合并日志 `0_build.txt` 与
  `build/3_Run pre-build scan gate.txt` 各一次，是同一份输出的两份落盘）
- 门头 `[n/112]`：112 个唯一编号，最大 112（无重号、无缺号）
- `✅ ALL 344 CASES PASS` 恰一条
- Kotlin 编译错误 ` e: `：**0 行**；本仓门输出 `[FAIL]`：**0 行**
- 含 `FAILED` 的 12 行逐条核对：**全部**是 `PASSED` 用例名里的 `FAILED` 子串
  （例：`slot denied produces FAILED without calling provider PASSED`），无一真失败
- 五道门的 `…_SELFTEST=PASS` 标记在 CI 日志中各出现两次（合并日志 + 该步骤单文件），
  说明本轮新增的见证臂在 CI 里真的执行了
- 本支 5 道门在 CI 中的门头：`[62/112] D/ISSUE-04 webview holder url source`、
  `[58/112] G/ISSUE-07 katex ready race + alias lock`、
  `[69/112] C/ISSUE-03 mirror category detail route wiring`、
  `[78/112] C/ISSUE-08 history save snapshot`、
  `[81/112] F/ISSUE-06 mirror vm thread/state`、

## 6. CI run

- run id：`40134859565`
- conclusion：`success`
- head_sha：`5f38473157a2c5291ecf6d0619ddc53b0d5af1b6`（与本地 `git rev-parse HEAD` 逐字符一致）
- 说明：本文件（`fix-reports/`）在绿 sha 之后单独提交，源码树与绿 sha 等价：
  `git diff --name-only 5f38473157a2c5291ecf6d0619ddc53b0d5af1b6..HEAD -- . ':(exclude)fix-reports'` 为空。

## 7. 残留（P2 登记，不修）

1. **W5f 只判移动分支的两次赋值** —— 触发条件：将来把两次赋值收进
   `applyViewport(mobile)` 助手（判据会 fail-closed 红，不会假绿）；触发后方案：把
   判据扩成「助手函数体内拷贝同名字段」一跳委托；搁置理由：当前无此重构，且方向安全
   （红而非绿）。
2. **W6e 只认 `val <win> = … as? Activity … ` 形态** —— 触发条件：改用
   `findActivity()` 之类的 helper 或 `with(activity) { }`；触发后方案：按「窗口来源
   派生出 Activity 上下文」而不是字面 cast 认定；搁置理由：本仓三处窗口来源都直取
   Activity。
3. **W1 要求映射与 elvis 在同一 RHS** —— 触发条件：拆成两行两条语句
   （`val mapped = fromAssetLoaderUrl(...); currentUrl = mapped ?: url`）；触发后方案：
   做一次本地变量传播；搁置理由：真树是单表达式，拆分形态目前不存在。

## 8. 复现

```
cd /var/minis/shared/work/meta-wide/wt-09-browser-webview
python3 scripts/scan/webview_holder_url_source_check.py . --selftest
python3 /var/minis/shared/work/meta-wide/probe/witness_lib.py replay \
    /var/minis/shared/work/meta-wide/task-09-browser-webview.md /var/minis/shared/work/meta-wide/wt-09-browser-webview --gate webview_holder_url_source_check.py
bash scripts/scan/scan.sh | tail -3
python3 scripts/scan/test_scan.py | tail -2
```

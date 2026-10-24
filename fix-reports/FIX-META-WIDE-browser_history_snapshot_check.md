# FIX-META-WIDE-browser_history_snapshot_check

- 分支：`fix/meta-wide-09-browser-webview`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 报告来源：组 A（wide-1024 宽扫，25 门/席）
- 生产代码改动：**0 行**（本波零 `.kt` 改动：只改门判据 + 自证臂）
- 冻结面：`scripts/scan/scan.sh` / `scripts/scan/test_scan.py` 未触碰

## 1. 门

`scripts/scan/browser_history_snapshot_check.py`

锁浏览器历史的两个契约：A 面 `BrowserHistoryStore.save()` 只能在
`synchronized(entries)` 里触碰活动列表（快照在锁内拷贝、JSON 与落盘在锁外）；B 面
`BrowserHistorySheet` 必须按「store 拥有的刷新信号」记忆化读取，且日桶标题来自
7 种语言的资源。本门原来 A2/B1/B2/B3 四组；本次 **+1 组生产侧判据（B1p）**，
两条判据从拼写改到标识符/实参。

## 2. 轴

| 见证 | 轴 | 新判据 |
|---|---|---|
| A1 `for (entry in entries.iterator())` | spelling | A1 改判**标识符**：`save()` 内除 `synchronized(entries)` 的参数外，`entries` 的任何出现都必须在同一个监视器块内（`.iterator()` / `.subList()` / `entries[0]` 一律无法逃逸） |
| B1 `bumpRevision` 写成 `.update { it }` | dataflow | 新增 B1p：刷新信号的写入必须发布**与旧值不同**的值（恒等 lambda ⇒ StateFlow 抑制相等值 ⇒ 永不发射） |
| B2 `groupedByDay("To" + "day", …)` | spelling | B2 改判**实参来源**：两个标签必须是绑定到 `R.string.browser_history_today` / `_yesterday` 的本地（拆字面量、换名绑定、顺序互换各有判据） |

## 3. 见证（3 条，机械复证结论：打上补丁旧门 rc=0 = 门瞎）

| # | 见证书 | 文件 | 旧判据为何绿 |
|---|---|---|---|
| 1 | `A1 (save_takes_synchronized_snapshot)` | `browser/BrowserHistory.kt` | 负向正则只认 `for (x in entries)` / `entries.forEach` / `entries[` / `entries.map {`；`for (entry in entries.iterator())` 在 `entries` 后紧跟 `.iterator()` ⇒ 失配，锁外遍历活动列表（CME + 丢写）直通 |
| 2 | `B1 (sheet_observes_history_revision)` | 同上 | 只验证 store 声明 `val revision: StateFlow<Int>` 且 sheet 以它为键；`bumpRevision` 写成 `.update { it }` 时观测值从不变化，面板 `remember(revision)` 永不重算 |
| 3 | `B2 (sheet_i18n_day_labels)` | `ui/browser/BrowserHistorySheet.kt` | 只查 `stringResource(...)` 声明存在 + 负向正则禁完整字面量 `"Today"` / `"Yesterday"`；`"To" + "day"` 两块都不命中，7 语言全部退化成英文 |

## 4. 修法

1. **A1（标识符轴）**：新增 `live_entries_reads()` —— 把 `synchronized(entries) { … }`
   块与监视器参数本身屏蔽成空格，剩下任何 `\bentries\b` 出现即违规。这样「换个读法」
   再也躲不过（`.iterator()` / `.subList()` / 下标 / `map` / `filter` 全覆盖）。
2. **B1p（新增，生产者侧）**：`revision_holder_names()` 从声明的初始化/取值器里收集
   承载体（`_revision`、`revisionPlaceholder` 这类间接层一并算），再扫所有
   `.update/.emit/.tryEmit/.value =/compareAndSet` 写入点；写入值经
   `IDENTITY_LAMBDA_RE` 归一后若是「把读到的值原样写回」（`{ it }` / `{ x -> x }`）即红；
   一个写入点都没有也红（fail-closed）。
3. **B2（实参轴）**：`string_resource_bindings()` 收集 `val X = stringResource(R.string.Y)`，
   然后按位置校验 `groupedByDay(today, yesterday)` 两个实参：必须是本地标识符、必须
   绑定到对应键、顺序不得互换；调用点消失同样 fail-closed 红。

自证臂：`.iterator()` 必红（A1）、`.update { it }` 必红（B1p）、无生产者必红（B1p）、
拆字面量必红（B2）、换名绑定必绿、顺序互换必红、调用消失必红。

## 5. 四层证据

### L1 本门

```
python3 scripts/scan/browser_history_snapshot_check.py .              # rc=0, 真树全绿
python3 scripts/scan/browser_history_snapshot_check.py . --selftest   # 全臂符合预期（新臂红 / clean 臂绿）
```

部分（store 侧）A1-A5 + A0 fail-closed；sheet 侧 B1-B9 + B0；字面量 B2-B5；
多语言 B3-B5；**wide-1024 七条**（W-A1 / W-B1p ×2 / W-B2 ×3 + 一条 clean 对照）。
收尾标记 `BROWSER_HISTORY_SNAPSHOT_SELFTEST=PASS` 未改动。

### L2 见证反攻（判据链）

L2 用的**不是**我重写的补丁，而是任务书里逐字节的见证原文（脚本：`probe/witness_lib.py`，
按任务书解析 `### 见证` 块 → 打补丁 → 跑门 → 恢复 → sha256 校验）。

```
修复前同一补丁 rc=0（门瞎） → 修复后 rc≠0（门抓住） ⇒ 当初那把刀被挡住了
```

| 见证书 | 轴 | 修复后 rc | restore | 判决 |
|---|---|---|---|---|
| `A1 (save_takes_synchronized_snapshot)` | spelling | 1 | True | `GATE_CAUGHT_WITNESS` |
| `B1 (sheet_observes_history_revision)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |
| `B2 (sheet_i18n_day_labels)` | spelling | 1 | True | `GATE_CAUGHT_WITNESS` |

判定：**本门 3/3 全抓住**，restore 全 True，工作树零污染（本支 5 门合计 **16/16**，逐条结果见 `probe/l2-*.json`）。

### L3 邻门（各 rc=0）

- `webview_holder_url_source_check.py` → rc=0
- `browser_cookie_ui_refresh_check.py` → rc=0

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

1. **B1p 只认「原样写回」这一种空转** —— 触发条件：写入值写成复杂恒等
   （`{ if (it > 5) it else it }`）；触发后方案：做常量折叠/符号比较，或改成运行时
   `distinctUntilChanged` 行为断言；搁置理由：静态判定复杂恒等等价于写一个求值器，
   收益远小于成本，且当前形态是唯一被实证过的。
2. **A1 只认标识符名字 `entries`** —— 触发条件：`entries` 被改名或经
   `this.entries` 之外的另一引用（如 `@PublishedApi internal val rows`）暴露；
   触发后方案：从字段声明派生「活动列表」的别名集合；搁置理由：真树单一字段，改名会
   同时打红 A0（找不到 save/字段），不会静默。
3. **B2 依赖 stringResource 直接绑定** —— 触发条件：标签经 helper 返回
   （`val labels = historyLabels()`）；触发后方案：允许一跳本地 helper 并校验其返回
   两个键；搁置理由：当前无此抽象。

## 8. 复现

```
cd /var/minis/shared/work/meta-wide/wt-09-browser-webview
python3 scripts/scan/browser_history_snapshot_check.py . --selftest
python3 /var/minis/shared/work/meta-wide/probe/witness_lib.py replay \
    /var/minis/shared/work/meta-wide/task-09-browser-webview.md /var/minis/shared/work/meta-wide/wt-09-browser-webview --gate browser_history_snapshot_check.py
bash scripts/scan/scan.sh | tail -3
python3 scripts/scan/test_scan.py | tail -2
```

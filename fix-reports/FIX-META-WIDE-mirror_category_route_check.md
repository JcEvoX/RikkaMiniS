# FIX-META-WIDE-mirror_category_route_check

- 分支：`fix/meta-wide-09-browser-webview`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`
- 报告来源：组 A（wide-1024 宽扫，25 门/席）
- 生产代码改动：**0 行**（本波零 `.kt` 改动：只改门判据 + 自证臂）
- 冻结面：`scripts/scan/scan.sh` / `scripts/scan/test_scan.py` 未触碰

## 1. 门

`scripts/scan/mirror_category_route_check.py`

锁「镜像分类详情页真的可达」：屏幕有调用点、`ROOTFS_MANAGEMENT` 的
分类点击真的导航、`fromKey` 解析器对未知键 fail-closed、路由经 `seg()` 编码。
本次 arm 1/2/3 全部从「子串存在」改到「值到达使用点」。

## 2. 轴

| 见证 | 轴 | 新判据 |
|---|---|---|
| arm2 路由构造后被丢弃 | dataflow | 在 `composable(Routes.ROOTFS_MANAGEMENT)` 块内取 `onMirrorCategoryClick` 的 lambda，要求路由值**流入** `safeNavigate/navigate`（允许先绑本地再导航一跳） |
| arm3 `fromKey` 谓词恒真 | dataflow | `firstOrNull { }` 谓词必须**与入参 `key` 比较**（`==`/`!=`/`equals`/`contains`/`in`）；恒真谓词会让所有分类都解析成第一项 |
| arm1 解析结果被 `.also` 丢弃 | dataflow | 传给屏幕的 `category` 必须由 `getString("categoryId")` 经 `MirrorCategory.fromKey(...)` 得出，且该调用不得落在 `.also`/`.apply` 内（返回接收者 ⇒ null ⇒ 详情页立刻 pop） |

## 3. 见证（3 条，机械复证结论：打上补丁旧门 rc=0 = 门瞎）

| # | 见证书 | 文件 | 旧判据为何绿 |
|---|---|---|---|
| 1 | `arm2 (ROOTFS_MANAGEMENT_passes_onMirrorCategoryClick)` | `ui/navigation/AppNavigation.kt` | 只查整文件出现 `onMirrorCategoryClick` 与 `Routes.mirrorCategoryDetail(`；`{ cat -> Routes.mirrorCategoryDetail(cat.key) }` 两串齐备，导航一次都没发生 ⇒ 点卡片无反应 |
| 2 | `arm3 (MirrorCategory_fromKey_resolver)` | `ui/sandbox/MirrorCategory.kt` | 只查 `fun fromKey(...)` 返回 `MirrorCategory?` 且含 `firstOrNull`；`{ true }` 满足（枚举非空 ⇒ 未知键仍返回 null，"fail-closed" 碰巧成立）⇒ 所有分类都打开 ALPINE |
| 3 | `arm1 (MirrorCategoryDetailScreen_wired)` | `ui/navigation/AppNavigation.kt` | 只查 `MirrorCategoryDetailScreen(` 出现 ≥2 次 + 目标块内出现 `MirrorCategory.fromKey` 与屏幕调用；`val category: MirrorCategory? = null.also { MirrorCategory.fromKey("") }` 三者都在 ⇒ 页面 100% 不可达 |

## 4. 修法

1. **arm2**：新增 `composable_block()`（按 `composable(<route>, …) { }` 取目的地块，
   避免别处的导航替这个屏幕背书）+ `navigates_with_route()`：路由表达式必须是
   `safeNavigate/navigate` 的实参，或先绑本地再作为实参（两种真实拼写都接受）。
2. **arm3**：从 `fun fromKey(<param>:` 取参数名，要求谓词里出现与 `param` 的比较；
   同时保留「返回可空 + 用 firstOrNull」两条原判据。
3. **arm1b**：取屏幕调用的 `category = <ident>` 实参 → 找该本地声明 → 语句级表达式
   （处理 `?.` 续行）必须同时含 `getString("categoryId")` 与
   `MirrorCategory.fromKey(`，且 fromKey 的返回值不得被丢弃型 lambda 吞掉。

自证臂：W1（`.also` 丢弃 fromKey 结果）必红、W2（谓词恒真）必红、W3（构造后丢弃、
无导航）必红、W3b（路由绑本地后导航）必绿。

## 5. 四层证据

### L1 本门

```
python3 scripts/scan/mirror_category_route_check.py .              # rc=0, 真树全绿
python3 scripts/scan/mirror_category_route_check.py . --selftest   # 全臂符合预期（新臂红 / clean 臂绿）
```

A-D 既有臂 + **wide-1024 四条**（W1 / W2 / W3 / W3b clean）。
收尾标记 `MIRROR_CATEGORY_ROUTE_SELFTEST=PASS` 未改动。

### L2 见证反攻（判据链）

L2 用的**不是**我重写的补丁，而是任务书里逐字节的见证原文（脚本：`probe/witness_lib.py`，
按任务书解析 `### 见证` 块 → 打补丁 → 跑门 → 恢复 → sha256 校验）。

```
修复前同一补丁 rc=0（门瞎） → 修复后 rc≠0（门抓住） ⇒ 当初那把刀被挡住了
```

| 见证书 | 轴 | 修复后 rc | restore | 判决 |
|---|---|---|---|---|
| `arm2 (ROOTFS_MANAGEMENT_passes_onMirrorCategoryClick)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |
| `arm3 (MirrorCategory_fromKey_resolver)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |
| `arm1 (MirrorCategoryDetailScreen_wired)` | dataflow | 1 | True | `GATE_CAUGHT_WITNESS` |

判定：**本门 3/3 全抓住**，restore 全 True，工作树零污染（本支 5 门合计 **16/16**，逐条结果见 `probe/l2-*.json`）。

### L3 邻门（各 rc=0）

- `mirror_vm_thread_check.py` → rc=0
- `deeplink_settings_routes_check.py` → rc=0

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

1. **arm2 只接受同文件可解析的导航调用** —— 触发条件：导航封装到别的文件
   （`navigateToMirrorCategory(navController, key)`）；触发后方案：允许一跳跨文件委托并
   校验 helper 体内确实调用 navigate；搁置理由：方向安全（红而非绿），且当前无此封装。
2. **arm1b 只认 `getString("categoryId")`** —— 触发条件：参数读取换成
   `backStackEntry.arguments?.getString("categoryId")` 之外的封装（如 `navArgs()`）；
   触发后方案：按「路由参数名 categoryId 的读取点」派生；搁置理由：真树一种形态。
3. **arm3b 只要求谓词含比较** —— 触发条件：谓词写成
   `{ it.key == key.uppercase() }`（比较了但与入参大小写不匹配）；触发后方案：无法
   静态判定，改由 `MirrorCategoryTest` 的行为断言兜底（已存在）；搁置理由：静态判据
   到此为止，行为面已有单测。

## 8. 复现

```
cd /var/minis/shared/work/meta-wide/wt-09-browser-webview
python3 scripts/scan/mirror_category_route_check.py . --selftest
python3 /var/minis/shared/work/meta-wide/probe/witness_lib.py replay \
    /var/minis/shared/work/meta-wide/task-09-browser-webview.md /var/minis/shared/work/meta-wide/wt-09-browser-webview --gate mirror_category_route_check.py
bash scripts/scan/scan.sh | tail -3
python3 scripts/scan/test_scan.py | tail -2
```

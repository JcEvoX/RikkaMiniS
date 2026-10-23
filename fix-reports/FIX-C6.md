# FIX-C6 — 悬浮窗 show/hide 缺意图闩锁（deep-audit-1046 / S-06 / P1）

- 分支：`fix/dv26-c6-overlay-show-hide-latch`
- 基线：`f7aba218`（`git log --oneline -1` 父提交实测）
- 生产文件：`service/ToolOverlayController.kt`、`service/AgentForegroundService.kt`、新增 `service/ToolOverlayPolicy.kt`
- 门：新增 `scripts/scan/overlay_show_hide_latch_check.py`（两文件此前被 0 道门扫描）
- 测试：新增 `src/android/app/src/test/java/com/rikkaminis/app/service/ToolOverlayLatchTest.kt`（8 用例）

## ① 缺陷与真树取证

`ToolOverlayController.show()` 把贴窗动作投进 `mainHandler`（`:211`），`isShown = true` 只有在那个 Runnable 执行到 `attach()` 末尾（`:268`）时才置位。也就是说 **`isShown` 比"用户想不想看"落后至少一个主线程队列跳**。

三处 hide 站点全部以它为门：

| 站点 | 基线形态 | 语义 |
|---|---|---|
| `ToolOverlayController.kt:178` | `if (isShown) hide()` | 无悬浮窗权限的早退分支 |
| `AgentForegroundService.kt:476` | `if (controller.isShown) controller.hide()` | Gate 1 早退（回前台 / 关开关 / 无权限 / 相机抑制） |
| `AgentForegroundService.kt:532` | 同上 | 非忙碌且无 completion linger 的收尾 |

⇒ 在 `show()` 的 post 尚未执行、`isShown` 仍为 false 的窗口内，任何一次状态翻转都三处全跳；随后队列里的 `attach()` 照常贴窗，且**没有新状态帧再来清除它** ⇒ 悬浮胶囊盖在 RikkaMinis 前台或相机预览上，直到下一次工具事件。这是"一个异步置位的字段被当成意图用"的经典形状（与 1042-B5 的 `installState` 重放、1045-B05 的 `_fastModeEnabled` 构造期快照同族）。

真树核对（`f7aba218`）：

```
$ grep -n 'isShown' ToolOverlayController.kt AgentForegroundService.kt
ToolOverlayController.kt:139  var isShown: Boolean = false        ← 声明（非意图）
ToolOverlayController.kt:178  if (isShown) hide()                  ← 读门控 ①
ToolOverlayController.kt:214  isShown = false                      ← hide 的 post 块内
ToolOverlayController.kt:268  isShown = true                       ← attach 末尾
ToolOverlayController.kt:447  ... shown=${controller.isShown}"     ← 日志
AgentForegroundService.kt:476 if (controller.isShown) controller.hide()   ← 读门控 ②
AgentForegroundService.kt:532 if (controller.isShown) controller.hide()   ← 读门控 ③
```

报告称 `:540`，真树是 `:532`（漂 8 行，机制不变）。

## ② 修法（改动阶梯：**停在第 7 级「最小实现」**，因为……）

停在第 7 级的理由：代码库里没有既成的"意图闩锁"抽象可复用（第 2 级落空）；`StateFlow`/`MutableStateFlow`（第 3/4 级）会引入一个新的发布-订阅面并需要生命周期管理，而这里只需要一个**跨线程可见的布尔**；控制器结构（post 纪律、幂等 detach、懒 attach）本身没有缺陷，重写只会扩大风险面。所以是「一个 `@Volatile` 字段 + 两个入口的一次赋值 + post 内一次读取 + 三处调用点去门控」。

1. **意图闩锁**：新增 `@Volatile private var desiredShown`。
   - `show()`：权限检查通过后、`mainHandler.post` 之前 `desiredShown = true`；
   - `hide()`：入口**无条件** `desiredShown = false`（在 post 之前）；
   - `show()` 投递的 Runnable **首行** `if (!shouldRunShowPost(desiredShown)) return@post`。
2. **三处调用点去门控**：`:178` 的 `if (isShown) hide()` → `hide()`；`AgentForegroundService` 两处 `if (controller.isShown) controller.hide()` → `controller.hide()`（`hide()` 本体 `view == null` 早退，天然幂等，零开销）。
3. **`isShown` 保留但只表示真实贴窗**（日志 `:447` 仍在读它）。KDoc 明确写「不要拿它当意图」。
4. **决策抽成纯函数**（`ToolOverlayPolicy.kt`）：`resolveOverlayIntent(wantShown, permission, foreground, camera, hasPending)` 与 `shouldRunShowPost(desired)`。`AgentForegroundService` 的 `shouldShow` 改走前者（语义逐条等价，`state.enabled` 仍在外面 AND —— 它同时门控权限提示）。

**明确没做**（避免把最小实现撑成重构）：
- 没有把 `show()` 的 post 改成 `postAtFrontOfQueue` 或 `removeCallbacks` —— 贴窗顺序由闩锁管，队列位置不是变量；
- 没有在 `hide()` 里同步 `detach()`（会破坏 post 顺序，且非主线程碰 `WindowManager` 会崩）；
- 没有动 `lingerJob` / `hasCompletionPending` 语义（`lingerJob` 目前只有声明与 cancel/null，从未 launch —— 见 ⑥ R3，按半径纪律不碰）；
- 没有把两字段合并成一个"能两用"的状态（这正是原缺陷形状，C4 专门钉死）。

## ③ 改动清单

| 文件 | 性质 | 变化 |
|---|---|---|
| `service/ToolOverlayController.kt` | 生产 | `+49/−`（意图字段 + 两入口赋值 + post 守卫 + 三处注释） |
| `service/AgentForegroundService.kt` | 生产 | `+22/−6`（`shouldShow` 走纯函数 + 两处 hide 去门控） |
| `service/ToolOverlayPolicy.kt` | 生产（新） | `+43`（两个纯决策函数） |
| `scripts/scan/overlay_show_hide_latch_check.py` | 门（新） | `+488`（4 判据 + 10 臂 selftest） |
| `src/test/.../service/ToolOverlayLatchTest.kt` | 测试（新） | `+211`（8 用例） |

合计 **5 文件 +807/−6**（生产 3 / 测试 1 / 门 1）；`验证面 0 触碰`（`scan.sh`、`test_scan.py` 未改）。

## ④ 门与判据

新建 `scripts/scan/overlay_show_hide_latch_check.py`（两文件此前 `grep -rln 'AgentForegroundService\|ToolOverlayController' scripts/scan/` = 空，所以没有既存独占门可扩）。

**判据全部派生、不锚拼写**（字段名可改、守卫写法可改、行号可漂）：

| 判据 | 派生方式 | 红条件 |
|---|---|---|
| **C1** | 在 `show()` 体内找 `X = true`、`hide()` 体内找同名的 `X = false` ⇒ X = 意图位 | 派生不出 ⇒ 红；任一赋值出现在 `.post` **之后** ⇒ 红（不是"入口"） |
| **C2** | 取 `show()` 投递的 Runnable 块；找条件里引用意图位、且 then 分支是 `return` 的 `if` | 没有守卫 ⇒ 红；守卫位置在 `attach(` 之后 ⇒ 红 |
| **C3** | 真实状态位 = **在 `attach()` 体内被赋 true** 的字段；枚举两文件所有 `hide()` 调用点，取同行（或上一行 `if (...)` + 单语句体）的 `if` 条件 | 条件引用真实状态位 ⇒ 红（不论是否取反） |
| **C4** | 意图位 vs 真实状态位 | 缺一个 ⇒ 红；两者同一 ⇒ 红 |

剥注释用**逐字符扫描**（不是正则），并且**字符串体一并抹白、换行全部保留** —— 报告行号因此永远对得上原文（对 C2 的 `"show() attach() — view was null"` 这类含括号的字符串免疫；这是 1039-A2「门判据必须剥注释」与 1036 系列「字符串扫描分三类」的合并教训）。

**selftest = 10 个检查，每个都是对真树文件的字节级变异**（不维护第二份夹具，臂不可能与生产漂移）：
`ok-arm`（真树绿）+ `c1-latch-after-post` / `c1-hide-late` / `c2-no-guard` / `c2-guard-after-attach` / `c3-controller-noperm` / `c3-service-gate1` / `c3-service-idle` / `c4-merged-fields` + `missing-file`（fail-closed）。
每个变异臂断言「红集合恰等于期望集合」（`reds == sorted(expect)`），所以"只红其判据"是机械判定，不是叙述。指纹标记：`OVERLAY_SHOW_HIDE_LATCH_SELFTEST=PASS`。

**建议注册行**（交主会话，本支未改 `scan.sh` / `test_scan.py`）：

```sh
# --- N. overlay show/hide intent latch ---
echo "━━━ [N/110] Overlay show/hide intent latch ━━━"
if python3 scripts/scan/overlay_show_hide_latch_check.py "$ROOT"; then
    PASS=$((PASS + 1))
else
    RC=1
    FAIL=$((FAIL + 1))
fi
```

`test_scan.py` 夹具：`st_extra` 走 `--selftest` + 真树绿，`teeth_marker = "OVERLAY_SHOW_HIDE_LATCH_SELFTEST=PASS"`。

**PRE 臂**（`git show f7aba218:` 两文件 → `probe/base/`）：门报 **4 条 violation，其中 C3 恰 3 条**（`ToolOverlayController.kt:178` / `AgentForegroundService.kt:476` / `:532` —— 正是任务书说的"三处调用点"），另 3 条是 C1/C2/C4（基线根本没有闩锁，三条判据都必须亮）。任务书写"必须红 3 条"是按调用点计的，实际读数更完整，报告按实际记。

## ⑤ 验证四层

**L1 门层**
- 真树：`python3 scripts/scan/overlay_show_hide_latch_check.py` → `rc=0`，`OK: 4 criteria clean — … [intent field = desiredShown] / [guard on desiredShown precedes attach()] / [no hide() gated on isShown] / [intent=desiredShown / real=isShown]`
- selftest：`10/10`，`OVERLAY_SHOW_HIDE_LATCH_SELFTEST=PASS`
- PRE 臂：`rc=1`，4 条 violation（C3 恰 3 条，见 ④）
- 全量（**沙箱侧为 S28 假红，不采信**）：持锁 `gate_run --exec 'sh scripts/scan/scan.sh; python3 scripts/scan/test_scan.py'` 落到 `probe/full2.log` —— `Summary: 25 passed, 84 failed` + `TS_RC=127`。按 registry S28 的判据逐行核对：日志里 `Function not implemented` 行 **85 条**（84 个门的 `python3` exec 各一条 + `sh: python3` 一条），**84 个 failed 与 ENOSYS 行一一对应，没有任何一条失败不带该字样** ⇒ 全是沙箱症状，不是代码红。16 支并发施工把并发窗口拉满，`gate_run --full` 两次在后段被沙箱回收（进度停在 16/48 门）。
- 全量（**权威读数 = 分支 CI job 日志**）：`Summary: 109 passed, 0 failed` 恰一条、`✅ ALL 336 CASES PASS` 恰一条、门头 `[1/109]…[109/109]` 序号连续且 109 个门名互异、编译错 `Z e:` 0、游离 `FAILED` 0 —— 见 L4。这与 COMMON §5.1「施工期本地只跑本门 + 邻门，全量交给 CI」一致；新门未注册（本批冻结 `scan.sh`/`test_scan.py`），所以 CI 的 109 门不含本门，本门的证据在 L1 前三条与 L2/L3。

**L2 邻门层**（按概念找，不照任务书名）：8 道真实邻门全 `rc=0`
`stale_closure_guard` · `claim_release_guard`（异步位门控同族）· `router_lock_check`（同 service 包）· `browser_tabs_lifecycle_check` · `browser_destroy_wakeup_check`（都是"post 队列 + 视图生命周期"）· `a11y_handler_contract_check`（Handler 契约）· `mirror_vm_thread_check`（线程归属）· `skill_import_offmain_check`（off-main 纪律）。

**L3 装置层**（真 `kotlinc 2.0.21` + 真 `JUnitCore`）

两套装置，都在沙箱里真编译真跑：

1. **仓库内单测 `ToolOverlayLatchTest.kt`（8 用例）+ 7 个生产突变**
   驱动分层（`mutate.py` 只做字节编辑，`run_arm.sh` 独占子进程 —— 注册表 S28「python 内嵌 subprocess 的 waitpid 炸 Errno 38」的健康路径）：
   | 臂 | 突变的**生产**代码 | 期望红 | 实测 |
   |---|---|---|---|
   | POST | 无 | 全绿 | `OK (8 tests)` ✅ |
   | M1 | `shouldRunShowPost` 恒 true（= 基线"post 无条件 attach"） | `t1` + `t5` | 2 红 ✅ |
   | M2 | `shouldRunShowPost` 恒 false（post 永不上屏） | `t2 t3 t4 t5` | 4 红 ✅ |
   | M3 | `resolveOverlayIntent` 去掉 completion linger | `t7` | 1 红 ✅ |
   | M4 | 去掉 foreground veto | `t6` | 1 红 ✅ |
   | M5 | 去掉 camera veto | `t6` | 1 红 ✅ |
   | M6 | 去掉 permission veto | `t6` | 1 红 ✅ |
   每个突变只改 `ToolOverlayPolicy.kt` 副本，**测试文件一字未改**（自指陷阱规避：期望值是字面量，不从被测常量推导）。

2. **队列时序装置 `probe/l3/build.py`（5 臂）**：把 `ToolOverlayController` 的 post 队列转写为纯 JVM 模型（`OverlaySim`），由**真生产函数**驱动。
   | 臂 | 形状 | 期望红 | 实测 |
   |---|---|---|---|
   | POST | 发布的闩锁 | 全绿 | `OK (5 tests)` ✅ |
   | PRE | 基线：无闩锁，attach 无条件跑 | `t1` | 1 红 ✅ |
   | M1 | post 守卫读 `isShown`（真实状态） | `t1` | 1 红 ✅ |
   | M2 | 守卫放在 `attach()` **之后** | `t1` | 1 红 ✅ |
   | M3 | `hide()` 不明确意图位 | `t1` | 1 红 ✅ |

   **降级说明（按 COMMON §5 要求写明）**：`ToolOverlayController` 的 `WindowManager` / `View` / `Handler` 半边依赖 Android 框架，沙箱无 SDK 无法 `kotlinc`。因此"接线"（哪个入口写闩锁、守卫相对 `attach()` 的位置）由**门的 C1/C2 在真树上钉住 + 10 个字节级变异臂取证**；"决策"（该不该上屏 / 该不该响应这个 post）由真 JVM 单测 + 7 个生产突变取证。两层合起来覆盖了原缺陷的两个组成部分。

**L4 CI 层**：run `39945776622` = **#2554**，attempt 1，`conclusion=success`，job `120387958727`（26 步，`not_ok_steps=[]`，唯一 skipped = `Publish to Releases` = main 门控）。判读（`probe/ci-job.log`，1,434,380 B）：
- `Summary: 109 passed, 0 failed` 恰一条（9 条 Summary 里其余 8 条是子检查的 25/5/13/16/10/22/10 passed 与 `0 violation(s) found`，全部 0 failed）；
- `✅ ALL 336 CASES PASS` 恰一条；
- 门头 `[1/109]…[109/109]` 序号连续、109 个门名互异；
- 编译错 `Z e:` 0、游离 `FAILED` 0（含 `FAILED` 的行逐条确认全是 `PASSED` 用例名子串）；
- **本支新增的 8 个用例逐条 PASSED**：`hideInsideTheQueueHopCancelsThePendingAttach` / `showThenDrainAttaches` / `lastIntentWins` / `withdrawnHideDoesNotSuppressALaterShow` / `doubleHideIsHarmless` / `shouldRunShowPostFollowsTheLatch` / `resolveOverlayIntentRefusesForegroundAndCamera` / `resolveOverlayIntentKeepsTheCompletionLinger`。
- 三方 sha 逐字符一致：本地 HEAD（报告提交前 `9c00ff0d`）= `git ls-remote` = `run.head_sha` `9c00ff0d913d62d976d8095cd4021b039e58267d`；**报告提交后源码树与 CI 绿树等价**（`git diff --name-only 9c00ff0d HEAD -- . ':(exclude)fix-reports'` = 空）。

## ⑥ 残留与负决策（全部带触发条件）

- **R1（真机行为级，未验）**：`§1046-C6` —— 悬浮胶囊"工具运行中切回前台"不留在聊天界面上 / "开着相机时"不出现在预览上 / 关开关立刻消失。**触发条件**：下一次真机窗口（三场景各 1 次）。未验理由：需要真机 + SYSTEM_ALERT_WINDOW + 真工具调用，沙箱无法覆盖。
- **R2**：`desiredShown` 与 `isShown` 的**跨线程可见性**靠两个 `@Volatile` 保证，但 `show()/hide()` 目前只从 `Dispatchers.Main` 的 collector 调用；若将来有非主线程调用者，"赋值顺序 = 队列顺序"的前提要重新论证。**触发条件**：出现第 3 个 `show()`/`hide()` 调用点（门 C3 会枚举到它，但不会再论证排序）。
- **R3（负决策，不修）**：`AgentForegroundService.lingerJob` 只有声明 + `cancel()`/`null`，**从未 launch**（`:474/:482`）。KDoc 与注释仍描述"4 秒停留"，与代码不符。按半径纪律（不修触发路径外的死码）不碰。**触发条件**：任何一次想改 linger 时长/语义的需求（届时先决定"复活 linger"还是"删字段 + 改注释"）。
- **R4（负决策，不修）**：`show()` 的 post 若在 `desiredShown` 于同跳内被翻转两次（show→hide→show），会 attach→detach→attach 一次（L3 `t3` 已钉住最终状态正确）。理论上可合并成一次贴窗，但收益是可省一次 `addView/removeView`，代价是引入"合并窗口"这一新状态。**触发条件**：真机日志出现该闪动的实证（如悬浮窗白闪）。
- **R5**：门 C3 只识别"同一行的 `if (...) hide()`"与"上一行 `if (...)` + 单语句体"两种守卫形态；`if (x) { … } else …` 或经局部布尔中转的守卫会漏。**触发条件**：出现第三种守卫写法（届时按形态扩 C3，selftest 补臂）。
- **R6**：门 C1 的"入口"判据 = 赋值位置在**同一函数体内第一个 `.post`** 之前；若将来 `show()` 在 post 之后再补一次赋值（合法但冗余），门会判红（偏严）。**触发条件**：真出现这种写法且被判定为合法时，放宽为"至少一次在 post 之前"。

## ⑦ 身份链

| 项 | 值 |
|---|---|
| 基线 | `f7aba218`（父提交实测） |
| 生产 + 测试 + 门 | `9c00ff0d`（一次提交） |
| 报告提交 | 见回报（在其后，源码树与 CI 绿树等价） |
| CI run id | `39945776622`（= #2554），attempt 1，`success` |
| CI job id | `120387958727` |
| CI head_sha | `9c00ff0d913d62d976d8095cd4021b039e58267d`（本地 == `ls-remote` == CI 逐字符） |
| 步骤 | 26 步，`not_ok_steps=[]`，唯一 skipped = `Publish to Releases`（main 门控） |
| 分支 | `fix/dv26-c6-overlay-show-hide-latch` |

## ⑧ 自证成本与不确定项

**成本**：门 488 行 + selftest 10 臂（其中 8 臂是对真树的字节变异）；JVM 装置两套 12 个用例 + 7 个生产突变；L2 八道邻门。总耗时主要在等待并发窗口（见下）。

**不确定项 / 自伤（如实记）**：
1. **C3 首版判据有全假阴性**：词法器写成 `(?<![.\w])isShown` —— 把 `controller.isShown`（成员访问）也排除掉了，导致两道服务侧臂"期望红、实际绿"。修法 = 边界改为 `(?<!\w)…(?!\w)`。**教训（与 1045-B02 同族）**：判据的**词法器是判据的一部分**，"排除成员访问"和"排除词内子串"是两个不同的意图，别写成一个后视。
2. **L3 首版 `lastIntentWins` 期望写错**（写 `attachCount == 1`，实际 show→hide→show 必然 2 次 attach）。这条是**测试期望错，不是生产错** —— 已按真实时序改成 2 并注明原因；值得注意的是它当场被抓到（说明该断言确实有牙）。
3. **`mutate.py` 首版用 python 内嵌 `subprocess.run` 驱动 kotlinc**，在沙箱撞 `OSError: [Errno 38] Function not implemented`（registry S28 已知变体）。改分层驱动（python 只编辑字节、shell 独占子进程）后 7/7 跑通。**没有**把 Errno 38 当成"臂没过"。
4. **全量 `scan.sh` 在沙箱侧不可用**：本批 16 支并发施工，`sh scripts/scan/scan.sh` 持续在子步骤炸 `Function not implemented`（`Summary: 25 passed, 84 failed`，而 ENOSYS 行 85 条 ⇒ 每条失败都带该字样，判据成立 = 假红），`gate_run.sh --full` 两次跑不到一半被沙箱回收。⇒ 全量证据改由**分支 CI job 日志**提供（CI 里 `scan.sh` 就是 109 门、`test_scan.py` 就是 336 例），这与 COMMON §5.1「施工期本地只跑本门 + 邻门，全量交给 CI」一致。**未把沙箱症状记成代码红**。
5. **报告与任务书的一处口径差**：任务书说 PRE 臂"必须红 3 条"，实测 4 条（C3 三条 + 基线的 C1/C2/C4 —— 基线根本没有闩锁）。按实际记，不按任务书回填。

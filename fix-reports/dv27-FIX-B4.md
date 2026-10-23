# FIX-B4 — 会话删除的清理顺序 与 工具栏借用剪贴板后的归还（deep-audit-1047 / B-06 + B-07）

- **支**：`fix/dv27-b4-session-delete-clipboard`
- **基线**：main `88ef9b27`（docs(dev-history): refresh entry count 2182 -> 2192）
- **尖端**：`56f16a61`（`fix(dv27-b4): release the view model before deleting a session; always restore the borrowed clipboard`）
- **改���**：8 文件 +798/−12（生产 2 改 + 2 新 / 门 2 扩 / 测试 2 新）
- **门数**：111（不变，0 新建门）；**夹具**：340（不变）
- **CI**：run `39996499456` / job `120561280684` attempt 1 **success**
- **停点**：分支 CI 绿 = 本轮确定性停止节点。**未合并 main**（含 `.kt` 验证面，合并起手权在用户）

---

## 1. 缺陷与真树取证

### B-06（P1）删除会话时先删目录、后释放 ViewModel ⇒ 死掉的会话自己长回来

真树 `src/android/app/src/main/java/com/rikkaminis/app/ui/chat/ChatHistoryDrawer.kt` 的 `deleteSessionAndCleanup`：

```kotlin
try {
    chatRepository.deleteSession(id)     // ← 先删行 + 递归删会话目录
} catch (t: Throwable) { ... } finally {
    ChatViewModelStore.release(id)      // ← 后释放常驻 ViewModel
    SessionBadgeStore.clear(id)
}
```

`chatRepository.deleteSession(id)` 会递归删掉 `minis-sessions/<id>/…`；而**常驻的 `ChatViewModel` 此刻还活着**，它的 `streamJob` 与 `BrowserTabPool` 仍在写：

- 写入落进刚被删掉的目录 ⇒ **删掉的会话以空壳目录的形式复活**（`minis-sessions/<id>/` 与 `browser_tabs/<id>.json` 被重新创建）；
- `release` 取消 `viewModelScope` ⇒ 被取消的 job 的 `finally` 在 **`SessionBadgeStore.clear(id)` 之后**才发布 overlay excerpt / 置会话非活跃 ⇒ **已删除的会话重新点亮徽标 / 浮层摘录**。

对照面：同一仓里 headless 入口 `ChatMutationMethods.delete` **本来就是先取消再删**（`src/android/app/src/main/java/com/rikkaminis/app/debug/ChatMutationMethods.kt`）—— 两条删除路径顺序不一致，本身就是缺陷信号。

### B-07（P1）借用剪贴板后只「有才还」⇒ 空剪贴板的用户被留下我们的选区文本

真树 `src/android/app/src/main/java/com/rikkaminis/app/ui/chat/MinisMarkdownTextToolbar.kt` 的 `withSelection`：

```kotlin
val original = runCatching { cm.primaryClip }.getOrNull()
try {
    onCopyRequested.invoke()        // 把选区写进系统剪贴板（Compose 无只读选区 API）
    …
} finally {
    if (original != null) runCatching { cm.setPrimaryClip(original) }
}
```

「还回原样」只在**借出前确实有东西**时发生。对剪贴板为空（或读取失败，因为快照读的是 `runCatching { cm.primaryClip }`）的用户，`original == null` ⇒ 什么都不做 ⇒ **我们借去的选区文本留在系统剪贴板里**：功能契约之外的后遗症，且**跨进程存活**——别的应用和下一次粘贴都会看到。

---

## 2. 修法（改动阶梯）

### B-06：把顺序收敛成一份可被 JVM 测的纯函数（阶梯第 7 级 · 最小实现）

新增 `ui/chat/SessionDeletePlan.kt`（Android 无关，纯顶层）：

```kotlin
internal enum class SessionDeleteStep { RELEASE_VIEW_MODEL, DELETE_ROW, CLEAR_BADGE }

internal fun sessionDeletePlan(): List<SessionDeleteStep> = listOf(
    SessionDeleteStep.RELEASE_VIEW_MODEL,
    SessionDeleteStep.DELETE_ROW,
    SessionDeleteStep.CLEAR_BADGE,
)
```

`ChatHistoryDrawer.deleteSessionAndCleanup` 改为**执行**这份计划，而不是在原地重述顺序：

```kotlin
for (step in sessionDeletePlan()) {
    when (step) {
        SessionDeleteStep.RELEASE_VIEW_MODEL ->
            withContext(Dispatchers.Main) { ChatViewModelStore.release(id) }
        SessionDeleteStep.DELETE_ROW -> chatRepository.deleteSession(id)
        SessionDeleteStep.CLEAR_BADGE -> SessionBadgeStore.clear(id)
    }
}
```

保留项（**刻意不动**）：
- 既有 `[fix/audit-0917-b9]` 的**无条件清理**语义（任何一步抛异常都进 `finally` 重放两个幂等步骤）——本次只是把「谁先谁后」固定下来，没削弱它；
- `ChatViewModelStore.release` 必须回 Main 线程（动 Compose 状态），与 `ChatViewModel.deleteSession` 同款。

### B-07：决策也收敛成一份纯函数，并**复用 offload 侧既有口径**（阶梯第 2 级 · 代码库里已有）

任务书要求「不要发明第三套」。真树里权威口径是 `ClipboardOffloadHandler` / `ClipboardClearAction`（dv22-A6 A-06 引入）：`atLeastQ` 由调用点传、API 26–28 用空串兜底。

新增 `ui/chat/ClipboardRestorePlan.kt`，四动作纯函数：

```kotlin
internal enum class ClipboardRestoreAction {
    RESTORE_ORIGINAL, CLEAR_PLATFORM, CLEAR_EMPTY_STRING, NOOP,
}

internal fun clipboardRestoreAction(hadOriginal: Boolean, atLeastQ: Boolean) = when {
    hadOriginal -> ClipboardRestoreAction.RESTORE_ORIGINAL
    atLeastQ    -> ClipboardRestoreAction.CLEAR_PLATFORM
    else        -> ClipboardRestoreAction.CLEAR_EMPTY_STRING
}
```

调用点（toolbar）：

```kotlin
when (clipboardRestoreAction(
    hadOriginal = snapshot != null,
    atLeastQ = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q,
)) {
    ClipboardRestoreAction.RESTORE_ORIGINAL -> snapshot?.let { cm.setPrimaryClip(it) }
    ClipboardRestoreAction.CLEAR_PLATFORM   -> cm.clearPrimaryClip()
    ClipboardRestoreAction.CLEAR_EMPTY_STRING -> cm.setPrimaryClip(ClipData.newPlainText("", ""))
    ClipboardRestoreAction.NOOP -> Unit
}
```

**取舍说明（任务书要求）**：UI 侧没有直接复用 `ClipboardOffloadHandler.clearAction(...)` 而是**镜像同一判据**，原因是 `ClipboardOffloadHandler` 是 `class ClipboardOffloadHandler(private val context: Context)`，其 `clearAction` 是**实例方法**且带着 handler 依赖；UI 侧要的是**无状态决策**。做法与 `ClipboardOffloadHandler.clearAction(atLeastQ)` 的既有约定完全一致（`atLeastQ` 由调用点传、平台判断不进纯函数），因此**不是第三套判据**，而是同一判据的第二个调用点。

---

## 3. 改动清单（8 文件 +798/−12）

| 文件 | 性质 | 说明 |
|---|---|---|
| `ui/chat/SessionDeletePlan.kt` | 新增 +53 | 纯函数：删除三步的有序计划 |
| `ui/chat/ClipboardRestorePlan.kt` | 新增 +69 | 纯函数：剪贴板归还的四动作决策 |
| `ui/chat/ChatHistoryDrawer.kt` | 改 +36/−12 | 删除路径改为执行 `sessionDeletePlan()` |
| `ui/chat/MinisMarkdownTextToolbar.kt` | 改 +32/−12 | `finally` 改为**总是**归还（无则清空） |
| `scripts/scan/skill_restore_session_cleanup_check.py` | 门扩 +166 | 新增 `check_drawer_delete_order` + 接线 |
| `scripts/scan/chat_ui_interaction_check.py` | 门扩 +240 | 新增 `clipboard_restore_findings`（C-CLIP-1）+ 4 臂 |
| `ui/chat/SessionDeletePlanTest.kt` | 新增 +108 | 5 用例（含负控） |
| `ui/chat/ClipboardRestorePlanTest.kt` | 新增 +106 | 6 用例（含负控） |

**冻结面零触碰**：`scripts/scan/scan.sh`、`scripts/scan/test_scan.py` **未改**（门数 111 / 夹具 340 保持不变）。

---

## 4. 门与判据

### 门1 `skill_restore_session_cleanup_check.py` — C-DEL-1 / C-DEL-2

`check_drawer_delete_order(drawer_src, plan_src=None)`：从**计划文件**的 `sessionDeletePlan()` 读出权威顺序，再要求 drawer 的删除路径**按该顺序**执行。

- **C-DEL-1**：删除路径必须遍历 `sessionDeletePlan()`，且计划文件里必须存在该函数并返回有序列表；找不到 ⇒ 报 **ROT**（门自身需要跟着改名），不是静默通过。
- **C-DEL-2**：`RELEASE_VIEW_MODEL` 必须排在 `DELETE_ROW` **之前**。

**实现期踩坑（真错，已闭环）**：门最初用 `ENUM_CONST_RE = r"(?<![.\w])([A-Z][A-Z0-9_]{2,})(?![.\w])"` 解析计划里的 `SessionDeleteStep.RELEASE_VIEW_MODEL` ⇒ 限定名被拆成 `SessionDeleteStep` + `RELEASE_VIEW_MODEL`，**解析出空顺序**，真树立刻红：

```
FAIL skill_restore_session_cleanup_check
  ChatHistoryDrawer.kt: the delete path iterates 'sessionDeletePlan()' but no plan function of that name
  returning an ordered list was found in the drawer or in its sibling plan file (ROT — update this gate)
```

修法 = 允许**限定名**形态（`(?:[A-Za-z_]\w*\.)?([A-Z][A-Z0-9_]{2,})`），两种写法都解析 ⇒ 改名不失明。**教训**：门解析枚举常量必须同时吃「裸常量」与「限定常量」，否则改名即失明（与 1046 批「门只认字面写法 = 委派重构后整族回归静默」同族）。

### 门2 `chat_ui_interaction_check.py` — C-CLIP-1

`clipboard_restore_findings(mdtoolbar_c)`：**行为判据**，不钉拼写。

- `withSelection` 必须有 `finally`（借出必归还）；
- `finally` 块里必须**同时**有「把原 clip 写回」（`setPrimaryClip`）与「把剪贴板清空」（`clearPrimaryClip` / `newPlainText`）两个动作；
- 两者必须在**互斥分支**里（`when` 分臂 / `if … else`）—— 否则「借出前是空的」那一路仍然被跳过，**正是本判据存在的缺陷形状**。

**4 个带牙 selftest 臂**（期望集手写，不从运行结果推导）：
1. 旧形态 `if (original != null) …` ⇒ 红（restore+clear 都在但不互斥）
2. 删掉 clear 动作 ⇒ 红
3. 删掉 restore 动作 ⇒ 红
4. 整个 `finally` 被删 ⇒ 红

夹具侧把 `MDTOOLBAR_F` 扩成含 `withSelection` 借还形态的完整样例（原本只有一个 toolbar host），`selftest` 夹具数 63 → **67**。

---

## 5. 验证四层

### L1 本门真树 + selftest + 全量

- 门1 真树 `rc=0`（`PASS skill_restore_session_cleanup_check`），selftest `SKILL_RESTORE_SESSION_CLEANUP_SELFTEST=PASS`
- 门2 真树 `rc=0`（`CHAT-UI-INTERACTION GATE OK`），selftest `CHAT_UI_INTERACTION_SELFTEST=PASS (67 fixtures: clean + 66 violation/precision)`
- **PRE 臂（基线树 `88ef9b27` 的 worktree `../b4-pre`）**：两门**都红** ⇒ 新判据在缺陷树上确实咬得住
- 全量 `scripts/scan/scan.sh` ⇒ **111 passed, 0 failed**
- 全量 `scripts/scan/test_scan.py` ⇒ **ALL 340 CASES PASS**

### L2 概念邻门（4/4 rc=0）

`chat_session_id_binding_check` / `browser_tabs_lifecycle_check` / `browser_destroy_wakeup_check` / `offload_cli_args_check` —— 全部 rc=0，未引入邻面回归。

### L3 真 kotlinc + JUnit（沙箱 JVM）

工具链：`/opt/kotlinc/bin/kotlinc`（2.0.21）+ `/opt/jvm-libs/junit-4.13.2.jar` + `/opt/jvm-libs/hamcrest-core-1.3.jar`；**运行 classpath 必须含 `/opt/kotlinc/lib/kotlin-stdlib.jar`**（漏了会 `NoClassDefFoundError: kotlin/jvm/internal/Intrinsics`，第一次跑就踩了）。

- **POST**：`SessionDeletePlanTest` + `ClipboardRestorePlanTest` ⇒ **OK (11 tests)**
- **变异臂**（`probe/l3/mut/mutate.py`，每臂独立目录、源树全程不动）：
  | 臂 | 变异 | 结果 |
  |---|---|---|
  | m1 | `DELETE_ROW` 提到 `RELEASE_VIEW_MODEL` 之前 | **RED**（2 failures） |
  | m2 | 空剪贴板时解析成 `RESTORE_ORIGINAL` | **RED**（2 failures） |
  | m3 | 空剪贴板且非 Q 时解析成 `RESTORE_ORIGINAL` | **RED**（2 failures） |

**★ 本轮自伤（已闭环）**：m2 首版变异写成了「加一行注释」的**空变异** ⇒ 编译通过、测试全绿，输出 `OK (11 tests)`。这是**假绿**：变异没实际改到行为。修法 = 变异必须落在**被断言的决策分支**上（改成另一个枚举分支），并核对 `old` 串在源文件中真实命中。**教训：变异臂的输出必须与「该臂应红」同向；若绿，先怀疑变异没生效，而不是怀疑装置。**

### L4 CI

**run `39996499456` / job `120561280684` / attempt 1 / `success`**

- `head_sha` = `56f16a61d841a2ffa5b888d2cf163f0d750e9b33`，与本地 HEAD、`git ls-remote origin refs/heads/fix/dv27-b4-session-delete-clipboard` **三方逐字符一致**
- 26 步里唯一非 success = 步骤 23 `Publish to Releases` = **skipped**（main 门控，符合预期）
- `Summary: 111 passed, 0 failed` **恰一条**；门头 `[1/111]..[111/111]` **连续互异 111**
- `ALL 340 CASES PASS` **恰一条**
- 编译错 ` e: ` **0**；游离 FAILED **6 行**，逐条确认**全是 PASSED 用例名子串**（判读陷阱第 N 次复核）
- 新测试 **11 条逐条 PASSED**（`SessionDeletePlanTest` 5 + `ClipboardRestorePlanTest` 6）
- 日志 1,448,761 B

**★ 环境坑（新形态）**：下载 job 日志时，GitHub API 返回 302 跳到 S3 签名 URL，**跳转后的请求不能带 `Authorization` 头**（否则 S3 拒签）。正确姿势 = 自定义 `HTTPRedirectHandler` 抛异常拿到 `Location`，**再不带 auth 头**取该 URL。

---

## 6. 残留与负决策

- **R1（P1，真机）** B-06：真机删除一个正在流式输出的会话，确认 (a) `minis-sessions/<id>/` **不再复活**，(b) 徽标/浮层摘录**不再点亮**。**触发条件** = 下次真机窗口（与 `§1046-VERIFY` 一次开齐）。**触发后方案** = 复现步骤见 §1。**搁置理由** = 沙箱无真机。
- **R2（P1，真机）** B-07：真机把剪贴板**清空**后点「add to input」，确认系统剪贴板**回到空**（而非留下选区文本）；再放一段原文本后重复，确认原文本**逐字保留**。**触发条件** = 同上。**搁置理由** = 同上。
- **R3（P2）`NOOP` 分支在生产路径不可达**：`withSelection` 里 `cm` 在借出前就拿到了，`hadOriginal` 只区分「有/无快照」，不会走到「拿不到 manager」。保留 `NOOP` 是为了让**未来调用方**无法悄悄退化成「什么都不做」。**触发条件** = 出现第二个借用剪贴板的调用方。**触发后方案** = 决定 `NOOP` 是删掉还是接线。**搁置理由** = 当前唯一调用方不可达。
- **R4（P2）C-CLIP-1 的互斥判定是启发式**（花括号深度 + `when` 分臂正则），对 `if` 链 / 表达式体 `when` 覆盖有限。**触发条件** = toolbar 改用其他分支形态。**触发后方案** = 补形态识别。**搁置理由** = 当前形态成立且 4 臂带牙。
- **R5（P2，门）C-DEL-1 的 ROT 提示依赖「计划文件里存在同名函数」**——若计划文件被改名而 drawer 未同步，门报 ROT 而非「顺序错了」，需要人看提示。**触发条件** = 计划函数改名。**触发后方案** = 把 ROT 提示升级为自动跟随。**搁置理由** = ROT 已是显式提示，不会静默通过。
- **R6（P3）`sessionDeletePlan()` 返回 `List` 而非 sealed 控制流**：三步各自可能抛异常，计划本身不表达「失败后继续/中止」语义 —— 那仍由 `finally` 的无条件重放承担。**触发条件** = 步骤数或语义变化。**触发后方案** = 升级为 sealed 控制流。**搁置理由** = 当前三步的异常语义与既有 `finally` 约定一致。
- **R7（P3，刻意）`ClipboardRestorePlan` 与 offload 侧 `ClipboardClearAction` 是两处同判据实现**，靠 KDoc + 门共同约束，非同一份代码。**触发条件** = offload 侧改判据。**触发后方案** = 合并成一份共享决策。**搁置理由** = offload 侧在 `sandbox/offload` 包、UI 侧在 `ui/chat` 包，且 offload 侧带 Context 依赖；跨包共享纯决策文件需要另立一轮抽象，**超出本支半径**。

---

## 7. 身份链

```
88ef9b27  (基线 main: docs(dev-history) 2182 → 2192)
   └─ 56f16a61  fix(dv27-b4): release the view model before deleting a session;
                always restore the borrowed clipboard
```

三方 sha 逐字符一致：本地 HEAD == `git ls-remote origin` == CI `run.head_sha` = `56f16a61d841a2ffa5b888d2cf163f0d750e9b33`。

---

## 8. 自证成本与不确定项

- **未做**：真机行为级验证（R1 / R2），沙箱无真机。
- **CI 日志的「游离 FAILED」**：6 行全部逐条核对为 PASSED 用例名子串，非真实失败；但仍是**日志文本启发式**，不是结构化 API 判定。
- **L3 的 PRE 臂**：本次 PRE 臂走的是**门**（基线树两门红）；**JVM 单测的 PRE 臂**以**负控用例**形式内嵌（`negative control - …`），没有另跑一份基线树的 JVM 二进制。理由：两个计划文件在基线树里**不存在**，基线树无可编译的对应物。
- **装置位置**：`probe/l1/`（门补丁脚本、全量扫描日志、CI 日志）、`probe/l3/`（kotlinc 产物、三臂变异）、`../b4-pre`（基线 worktree）。均为 worktree 私有，不入库。

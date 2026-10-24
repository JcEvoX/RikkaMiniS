# FIX-META-WIDE · `session_sheet_id_binding_check.py`

## 0. 门与基线

- 分支：`fix/meta-wide-05-chat-session` ｜ worktree：`/var/minis/shared/work/meta-wide/wt-05-chat-session`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`（干净、sha 一致）
- 报告来源：组 A（wide-1024），机械复证 3 条门瞎实锤 / 1 条攻击被挡 / 0 条无效
- 只改本门脚本：生产 `.kt` 零改动，`scan.sh` / `test_scan.py` 零触碰，门数与夹具数不变

## 1. 轴

三条全是 **dataflow**：门的每一处判据都成功验证了「调用存在 / 键列表正确 / 传参在」，而缺陷把**实参的值**换掉。三处击中的正是本门自己的命题（"会话 id 必须绑到真正消费它的位置"），却恰好漏掉了参数值本身 —— 调用端（ChatScreen 传 `viewModel.currentSessionId`）和键（`LaunchedEffect(…, sessionId)`）都是对的。

## 2. 见证（find→replace 已机械复证：打上补丁后本门 rc=0）

| # | 判据 | 文件 | 逃逸形状 | 用户可见后果 |
|---|---|---|---|---|
| C5 | SessionSkillsSheet_mergeSessionOverrides | `ui/chat/SessionSkillsSheet.kt` | `mergeSessionOverrides(liveIds = emptyList(), …)` —— 合并助手被调用、`lookup` 也在，只是走 0 个 id | 打开「当前会话技能」面板时所有已开启的开关回显为关闭（种子全丢） |
| C6 | SessionMcpsSheet_sessionId_binding | `ui/chat/SessionMcpsSheet.kt` | `isEnabledForSession(it, "__new__")` —— 调用点/键都对，只有查询实参是孤儿草稿键 | 真实会话的 MCP 开关配置永远读不回来（每次都查 `__new__` 那行） |
| C4 | SessionsShared_SessionEditSheet_LaunchedEffect_keys | `ui/sessions/SessionsShared.kt` | 效果键全对，体里删掉 `liveSession.title?.let { title = it }` | 重新生成标题后弹窗输入框仍是旧标题，点保存把新标题覆盖回去 |

攻击被挡住（不动）：C7 (SessionMcpsSheet_no_unguarded_override_write)，锚 anchor。

## 3. 修法

1. **C5 → `_judge_merge_live_ids()`**：读 merge 家族的 `liveIds` 实参**值**（`_call_args` 命名实参解析；无命名则取首个位置实参）。红三类：①字面空集合（`emptyList()` / `listOf()` / `emptySet()` / `mutableListOf()` …，`EMPTY_LITERAL_RE`）；②裸标识符但本效果体里从未计算过（「交给合并器的 id 列表不是这里算出来的」）；③在本体里算过却再没被消费（`liveIds` 只出现一次 = 算了就扔）。内联表达式（`skills.map { it.id }`）允许 —— 它不是字面量。
2. **C6 → `_judge_seed_read_args()`**：效果体内每个 `isEnabledForSession(…)` 调用的**最后一个实参**必须引用 `sessionId`（本门在调用端/键上已经钉死这个名字）。`"__new__"` / `draftId` / 任何别的名字 → 红。
3. **C4 → `_mirrors_state()`**：从 SessionEditSheet 体内 bracket 匹配 `LaunchedEffect(<liveSession.id>…)` 的**块体**（`_live_seed_effect_block`，解析失败 fail-closed），断言 `liveSession.title → title` 与 `liveSession.category → selectedCategory` 两条数据流各有一种合法拼写：直接赋值、作用域函数镜像（`?.let { title = it }`）、一跳本地绑定（`val t = liveSession.title` … `title = t`）。

两处判据落在**共享** `check_seeding_guard()`（两个 sheet 一份契约，判据也只有一份）。

## 4. 四层证据

- **L1**：真树 `python3 scripts/scan/session_sheet_id_binding_check.py .` → rc=0（加固前后同绿，判据未误伤）；`--selftest` → PASS；新增 4 臂（3 见证各红恰好 1 条并点名锚，1 条 clean 反臂 `title = liveSession.title ?: title` 保持绿）。既有自检全部保持绿（含 B-06 三种守卫拼写与 precision 计数）。
- **L2**：`probe-05/l2b_before_after.py` —— C5/C6/C4 三条逐条：基线门 rc=0 → 加固门 rc=1，restore sha256 一致；新红行分别是「the merge helper is handed `liveIds = emptyList()`」「the seeding read calls `isEnabledForSession(…, "__new__")`」「the liveSession-keyed effect no longer mirrors `liveSession.title` into `title`」。
- **L3**：`chat_session_id_binding_check` rc=0、`chat_menu_prefs_listener_check` rc=0。
- **L4**：分支 CI（`scan.sh` 112/0 + `test_scan.py` ALL 344；本门 marker 注册于 test_scan :2831）。

## 5. CI run

dispatch 后回填（CI run id / conclusion / head_sha 与本地 HEAD 逐字符比对）。

## 6. 残留（三件）

- **触发条件**：seed 读取换了签名（`isEnabledForSession` 增/删参数导致 `sessionId` 不再在末位）。
- **触发后方案**：把「末位实参」改成「按形参名定位」——从 repository 侧 `fun isEnabledForSession(` 的形参表派生 id 形参下标，再取对应实参。
- **搁置理由**：当前两个 repository 的签名一致且 id 都在末位；按形参名派生要跨文件读 repository（本门目前只读 4 个 UI 文件），属扩作用域，等签名真的变了再做。

- **触发条件**：`_mirrors_state` 遇到「通过 `LaunchedEffect` 外部 helper 写状态」（`seedFrom(liveSession)`）的合法重构。
- **触发后方案**：允许一跳同文件 helper —— 解析 `seedFrom(` 的目标函数体，在 helper 体内判镜像（与 M5 的 R11「允许一跳同文件委托」同一手法）。
- **搁置理由**：真树仍是内联拼写；现在放宽会削弱「体被掏空」的核心判据，等重构发生再补（当前形态是 fail-closed 红，不会静默放行）。

## 7. 复现

```bash
cd /var/minis/shared/work/meta-wide/wt-05-chat-session
python3 scripts/scan/session_sheet_id_binding_check.py .            # rc=0
python3 scripts/scan/session_sheet_id_binding_check.py . --selftest # 含 4 条新臂
python3 /var/minis/shared/work/meta-wide/probe-05/l2_witnesses.py "$PWD"
```

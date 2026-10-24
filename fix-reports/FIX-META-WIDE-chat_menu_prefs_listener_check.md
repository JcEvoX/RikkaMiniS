# FIX-META-WIDE · `chat_menu_prefs_listener_check.py`

## 0. 门与基线

- 分支：`fix/meta-wide-05-chat-session` ｜ worktree：`/var/minis/shared/work/meta-wide/wt-05-chat-session`
- 基线：`564c8f3eef683d50d7c90ae6fc7b21ce87972d71`（干净、sha 一致）
- 报告来源：组 A（wide-1024），机械复证 3 条门瞎实锤 / 1 条攻击被挡 / 0 条无效
- 只改本门脚本；生产 `.kt` 零改动，冻结面零触碰

## 1. 轴

三条分两轴：**dataflow**（arm2、arm7）与 **spelling**（arm5）。

- arm2/arm7 的判据停在「这三个调用在守卫块里」「这个效果键是 `writeVersion` 且体内有 `SoulStore.load(`」——都只证明**动作发生过**，不证明**结果被使用**。
- arm5 的判据是正则 `_writeVersion\.value\s*(\+=\s*1|\+\+|=\s*[^=])`：作者写 `=\s*[^=]` 是为了排除 `==`，但自赋值 `= _writeVersion.value` 完美落在这一类里 —— 只认拼写、不认值变没变。

## 2. 见证（find→replace 已机械复证：打上补丁后本门 rc=0）

| # | 判据 | 文件 | 逃逸形状 | 用户可见后果 |
|---|---|---|---|---|
| arm2 | listener_rereads_three_orders | `ui/settings/ChatMenuSettingsScreen.kt` | 三个 resolve 调用原样保留、仍在 `draggingEntry == null` 里，但返回值全丢（不赋给 `menuOrder` / `footerOrder` / `attachOrder`） | 外部（`minis-config` / 备份恢复）改 `chatMenu.*Order` 后三个可拖拽列表不刷新，再拖一次把陈旧顺序写回去（da1021-D 复发） |
| arm5 | SoulStore_writeVersion_bumps | `agent/SoulStore.kt` | `refreshCache()` 里 `_writeVersion.value = _writeVersion.value` —— 值不变，StateFlow 去重不发射 | 聊天记忆面板直接改写 SOUL.md 后，Settings → Soul 编辑器不重载，继续显示旧正文并可能覆盖回去（da1028-B / ISSUE-04 复发） |
| arm7 | SoulSettingsScreen_loads_on_io_writes_on_main | `ui/settings/SoulSettingsScreen.kt` | 在 `LaunchedEffect(writeVersion)` 首行加 `if (loaded) return@LaunchedEffect` —— 键、`withContext(Dispatchers.IO)`、`SoulStore.load(`、状态的发布位置全在原位 | 效果退化成 `LaunchedEffect(Unit)`：首次进入正常，运行期所有外部写入被早退吞掉（da1028-B 复发） |

三条的 also_blinds 关系（报告原值）：arm2 → arm3；arm7 → arm6。加固后 arm2 的落空写入会同时让 arm3 红（守卫块里的读没被消费，等价于该块没起到刷新作用），这是预期冗余。

攻击被挡住（不动）：arm8 (LogManagementScreen_registers_and_unregisters_listener)，锚 anchor。

## 3. 修法

1. **arm2 → `result_consumed()`**：对 `ChatMenuPrefs.<read>` 的每个调用点，取 `_stmt_start()` 派生的**语句头**（语句边界含深度 0 换行 —— Kotlin 几乎不用分号，缺这条会把上一条语句卷进来，正是 M1 自查抓到的假绿形态），接受两种消费：①语句头里有 `<目标 State> =`（链式 `.filter {}` 允许）；②绑定到本地 `val x = …` 且同块稍后 `menuOrder = x`（一跳委托，正当重构）。未消费 → arm2 红并点名三个目标里缺哪个。
2. **arm3 → `guarded_reads()` 收紧**：守卫块内不仅要出现三个读，还要求**每个读的消费也发生在块内**；块括号失配 → fail-closed 红。
3. **arm5/arm6 → `bumps_write_version()`**：不再问「有没有写」，而问「值有没有变」——复合赋值 / 前后自增直接算变；`= RHS` 形式读 `_rhs_text()`（深度 0 截断）并排除「同一计数器自读」形态（`_NOOP_SELF_RE`，含 `.also{}` / `.let{}` / `.apply{}` / `run{}` / `takeIf{}` 这些空转包装）。`_writeVersion.value = _writeVersion.value + 1` 仍绿。
4. **arm7 → 早退判据**：`SoulStore.load(` 之前出现 `return@LaunchedEffect` → 红「效果在重读之前就退出，等价于一次性加载」。

## 4. 四层证据

- **L1**：真树 `python3 scripts/scan/chat_menu_prefs_listener_check.py .` → rc=0（加固前后同绿）；`--selftest` → `CHAT_MENU_PREFS_LISTENER_SELFTEST=PASS`，新增 5 臂：3 条见证各红且锚点名在案 + 2 条 clean 反臂（`val resolvedOrder = …; menuOrder = resolvedOrder.filter{}` 与 `_writeVersion.value = _writeVersion.value + 1`）保持绿；既有 8 条 arm1-8 与 no-listener fail-closed 臂全绿。
- **L2**：`probe-05/l2b_before_after.py` —— arm2/arm5/arm7 逐条：基线门 rc=0 → 加固门 rc=1，restore sha256 一致；新红行：arm2「calls the order re-read(s) … but discards the result」、arm5「refreshCache(…) no longer bumps _writeVersion」、arm7「returns before re-reading the file (`return@LaunchedEffect` ahead of SoulStore.load)」。
- **L3**：`session_sheet_id_binding_check` rc=0、`prefs_listener_holder_guard` rc=0。
- **L4**：分支 CI（`scan.sh` 112/0 + `test_scan.py` ALL 344；本门 marker 注册于 test_scan :2808）。

## 5. CI run

dispatch 后回填（CI run id / conclusion / head_sha 与本地 HEAD 逐字符比对）。

## 6. 残留（三件）

- **触发条件**：arm5 的写入被改成「通过另一个 StateFlow/字段间接递增」（`_writeVersion.value = nextVersion()`）。
- **触发后方案**：判据从「RHS 不含自读」升级为「RHS 引用了本计数器之外的任何东西」——即 `_NOOP_SELF_RE` 反转为白名单（允许调用/字面量/其它标识符），并补一条 clean 臂。
- **搁置理由**：真树只有 `+= 1` 与 `= x + 1` 两种；现在写白名单会把「`= 0` 重置」也放进来（重置同样可能不发射），需要先决定重置算不算 bump，等它真的出现再定。

- **触发条件**：`draggingEntry` 守卫换成等价拼写（`if (draggingEntry != null) return` 早退式）。
- **触发后方案**：`guarded_reads()` 增加早退形态（把「守卫之后到块尾」作为有效区间）。
- **搁置理由**：真树是 `if (draggingEntry == null) { … }`；早退拼写在 `OnSharedPreferenceChangeListener` lambda 里会与 reader 语义纠缠（lambda 内 `return@OnSharedPreferenceChangeListener`），需要小心的括号模型，等重构发生再补（当前形态 fail-closed，不静默放行）。

## 7. 复现

```bash
cd /var/minis/shared/work/meta-wide/wt-05-chat-session
python3 scripts/scan/chat_menu_prefs_listener_check.py .            # rc=0
python3 scripts/scan/chat_menu_prefs_listener_check.py . --selftest # 含 5 条新臂
python3 /var/minis/shared/work/meta-wide/probe-05/l2_witnesses.py "$PWD"
```
